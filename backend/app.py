"""
创建本地演示控制平面；目前尚未连接执行器。
该模块负责初始化 FastAPI 应用、配置持久化存储、挂载静态文件并设置全局约束。
"""

import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api.tasks import task_router
from backend.artifacts.files import InputFiles
from backend.bootstrap import ApplicationDependencies, build_dependencies
from backend.domain.store import TaskStore


def create_app(
    data_dir: Path | None = None,
    demo_token: str | None = None,
    max_upload_bytes: int | None = None,
    dependencies: ApplicationDependencies | None = None,
) -> FastAPI:
    """
    应用工厂函数。
    
    参数:
        data_dir: 任务数据和持久化文件的存储目录。默认使用 .runtime
        demo_token: 演示用的访问令牌。若未提供，则从环境变量 AI_AGENT_DEMO_TOKEN 读取。
        max_upload_bytes: 最大允许上传的 CSV 大小。默认 10MB。
        dependencies: 仅由受信启动代码注入的平台无关服务；不从 HTTP 接收。
    """
    services = dependencies if dependencies is not None else build_dependencies()
    # 验证令牌安全设置，防止使用默认或不安全的配置
    token = demo_token or os.environ.get("AI_AGENT_DEMO_TOKEN")
    if not token or token == "replace-with-a-long-random-secret":
        raise RuntimeError("在启动控制平面之前，请设置 AI_AGENT_DEMO_TOKEN 环境变量")

    # 配置根数据目录
    root = data_dir or Path(os.environ.get("AI_AGENT_DATA_DIR", ".runtime"))
    
    # 获取上传限制
    limit = max_upload_bytes or int(os.environ.get("AI_AGENT_MAX_UPLOAD_BYTES", "10485760"))
    if limit <= 0:
        raise ValueError("上传限制必须为正数")

    # 创建必要的目录结构并限制 Linux/Unix 下的权限
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        root.chmod(0o700) # 仅所有者可读写执行

    from backend.local_runtime import heartbeat, readiness

    @asynccontextmanager
    async def lifespan(app):
        async def pulse():
            while True:
                heartbeat(root, "api")
                await asyncio.sleep(2)
        job = asyncio.create_task(pulse())
        try:
            yield
        finally:
            job.cancel()
            try:
                await job
            except asyncio.CancelledError:
                pass
            (root / "services" / "api.json").unlink(missing_ok=True)

    app = FastAPI(title="AI Data Agent", version="0.1.0", lifespan=lifespan)
    app.state.dependencies = services
    
    # 初始化存储层和文件管理层
    store = TaskStore(root / "tasks.sqlite3")
    inputs = InputFiles(root / "inputs")
    
    # 注册任务路由
    app.include_router(task_router(store, inputs, token, limit))

    @app.get("/health")
    def health():
        """健康检查接口，用于监控服务状态"""
        return {"status": "scaffold", "execution_enabled": False}

    @app.get("/ready")
    def ready(x_demo_token: str | None = Header(None)):
        import hmac
        if x_demo_token is None or not hmac.compare_digest(token, x_demo_token):
            raise HTTPException(401, "无效的演示令牌")
        result = readiness(root)
        return JSONResponse(result, status_code=200 if result["ready"] else 503)

    # 挂载前端 Web 页面
    web = Path(__file__).resolve().parent.parent / "web"
    app.mount("/", StaticFiles(directory=web, html=True), name="web")
    
    return app
