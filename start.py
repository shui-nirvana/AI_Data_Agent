"""Windows local demo launcher. It never executes uploaded data or model code."""

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
import webbrowser


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
VENV = ROOT / ".venv"
VENV_PYTHON = VENV / "Scripts" / "python.exe"
IMAGES = RUNTIME / "docker" / "images.json"
DEPENDENCY_MARKER = VENV / ".ai-agent-dependencies.json"
IMAGE_MARKER = RUNTIME / "docker" / "launcher-image.json"
WINDOWS_DOCKER_HOST = "npipe:////./pipe/dockerDesktopLinuxEngine"
MIN_PYTHON = (3, 11)
LOCK_TESTED_PYTHON = (3, 12)


class LaunchError(Exception):
    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage


def step(name, message):
    print(f"[{name}] {message}", flush=True)


def run(command, stage, hint, *, env=None, timeout=None, quiet=False):
    try:
        result = subprocess.run(
            [str(part) for part in command], cwd=ROOT, env=env, timeout=timeout,
            stdout=subprocess.PIPE if quiet else None,
            stderr=subprocess.PIPE if quiet else None,
            text=True, encoding="utf-8", errors="replace", check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LaunchError(stage, hint) from exc
    if result.returncode:
        raise LaunchError(stage, hint)
    return result.stdout.strip() if quiet else ""


def digest_files(paths):
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(ROOT).as_posix()):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def dependency_fingerprint():
    return digest_files([ROOT / "deploy/control-windows.lock", ROOT / "pyproject.toml"])


def image_fingerprint():
    paths = [
        ROOT / ".dockerignore", ROOT / "deploy/Dockerfile.job",
        ROOT / "deploy/requirements-job.lock", ROOT / "backend/__init__.py",
        ROOT / "workers/sandbox_manager/build_images.py",
    ]
    for directory in (ROOT / "backend/domain", ROOT / "images"):
        paths.extend(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)
    return digest_files(paths)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def check_python_version(version, stage="Python"):
    if tuple(version[:2]) < MIN_PYTHON:
        current = ".".join(str(part) for part in version[:2])
        raise LaunchError(stage, f"当前 Python {current} 低于最低要求 3.11，请更新 Python 后重新运行 python start.py。")


def check_platform():
    if os.name != "nt":
        raise LaunchError("环境", "此入口当前只支持 Windows；Linux/Vultr 尚未使用此脚本验收。")
    check_python_version(sys.version_info)
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "linux/amd64"
    if machine in ("arm64", "aarch64"):
        return "linux/arm64"
    raise LaunchError("环境", "不支持当前 CPU 架构，请使用 AMD64 或 ARM64。")


def check_docker(env):
    if not shutil.which("docker"):
        raise LaunchError("Docker", "找不到 Docker 命令。请安装 Docker Desktop 并重新打开终端。")
    system = run(
        ["docker", "info", "--format", "{{.OSType}}"], "Docker",
        "无法连接 Docker Linux 引擎。请启动 Docker Desktop 并确认 Linux 容器可用。",
        env=env, timeout=25, quiet=True,
    )
    if system != "linux":
        raise LaunchError("Docker", "当前 Docker 不是 Linux 引擎，请切换到 Linux 容器。")


def prepare_dependencies(env):
    if not VENV_PYTHON.exists():
        if VENV.exists() and any(VENV.iterdir()):
            raise LaunchError("Python 环境", ".venv 已存在但缺少 Python，请检查该目录；脚本不会删除它。")
        step("Python 环境", "创建项目专用 .venv。")
        run([sys.executable, "-m", "venv", VENV], "Python 环境", "创建虚拟环境失败。", env=env)
    version = run(
        [VENV_PYTHON, "-c", "import sys; print('.'.join(map(str, sys.version_info[:2])))"],
        "Python 环境", "无法读取 .venv 的 Python 版本。", env=env, quiet=True,
    )
    try:
        venv_version = tuple(int(part) for part in version.split("."))
        if len(venv_version) != 2 or any(part < 0 for part in venv_version):
            raise ValueError("invalid Python version")
    except ValueError as exc:
        raise LaunchError("Python 环境", "无法识别 .venv 的 Python 版本，请检查已有环境。") from exc
    if venv_version < MIN_PYTHON:
        raise LaunchError("Python 环境", f".venv 使用 Python {version}，低于最低要求 3.11；请检查并更新该环境，脚本不会删除它。")
    if venv_version != LOCK_TESTED_PYTHON:
        step("Python 环境", f".venv 使用 Python {version}；依赖锁基于 3.12 验证，此版本兼容性尚未验证。")
    fingerprint = dependency_fingerprint()
    marker = read_json(DEPENDENCY_MARKER)
    if marker != {"fingerprint": fingerprint, "python": version}:
        step("依赖", "首次安装或依赖变更：安装锁定依赖，可能需要联网。")
        run([VENV_PYTHON, "-m", "pip", "install", "--require-hashes", "-r",
             ROOT / "deploy/control-windows.lock"], "依赖", "锁定依赖安装失败，请检查 Python 版本兼容性、网络和磁盘空间；该锁基于 Python 3.12 验证。", env=env)
        run([VENV_PYTHON, "-m", "pip", "install", "--no-deps", "-e", ROOT],
            "依赖", "项目安装失败，请检查 Python 构建工具和网络。", env=env)
        run([VENV_PYTHON, "-m", "pip", "check"], "依赖", "依赖检查失败，请检查 .venv。", env=env)
        write_json(DEPENDENCY_MARKER, {"fingerprint": fingerprint, "python": version})
    else:
        run([VENV_PYTHON, "-m", "pip", "check"], "依赖",
            "已安装依赖不一致，请检查 .venv；脚本不会删除已有环境。", env=env, quiet=True)
    run([VENV_PYTHON, "-c",
         "import fastapi,uvicorn,pydantic,docker,httpx,langgraph.checkpoint.sqlite"],
        "依赖", "必要依赖无法导入，请检查 .venv。", env=env, quiet=True)
    step("依赖", "项目虚拟环境已就绪。")


def image_is_current(fingerprint, target_platform, env):
    marker, manifest = read_json(IMAGE_MARKER), read_json(IMAGES)
    if not isinstance(marker, dict) or not isinstance(manifest, dict):
        return False
    images = manifest.get("images")
    if (marker.get("fingerprint") != fingerprint or marker.get("platform") != target_platform
            or marker.get("images") != images or manifest.get("platform") != target_platform
            or not isinstance(images, dict) or set(images) != {"PROFILE_A", "CLEAN_C", "VERIFY_D"}):
        return False
    if any(not isinstance(image, str) or not image.startswith("sha256:") for image in images.values()):
        return False
    for image in set(images.values()):
        try:
            run(["docker", "image", "inspect", image], "镜像", "镜像不存在。",
                env=env, timeout=25, quiet=True)
        except LaunchError:
            return False
    return True


def prepare_images(target_platform, env, target="local"):
    fingerprint = image_fingerprint()
    if not image_is_current(fingerprint, target_platform, env):
        step("镜像", "首次使用、镜像缺失或源码有变化：构建隔离镜像，可能需要联网。")
        run([VENV_PYTHON, "-m", "workers.sandbox_manager.build_images",
             "--target", target, "--platform", target_platform],
            "镜像", "镜像构建失败，请检查 Docker、网络、磁盘与构建输出。", env=env)
        manifest = read_json(IMAGES)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("images"), dict):
            raise LaunchError("镜像", "构建后未找到有效镜像清单。")
        write_json(IMAGE_MARKER, {"fingerprint": fingerprint, "platform": target_platform,
                                  "images": manifest["images"]})
        if not image_is_current(fingerprint, target_platform, env):
            raise LaunchError("镜像", "镜像清单或本机镜像校验失败。")
    step("镜像", f"隔离镜像就绪（{target_platform}）。")


def choose_model(mode, allow_external, env):
    if mode is None:
        print("\n选择验证模式：", flush=True)
        print("  1. 使用 DeepSeek：可能产生费用。")
        print("  2. 不使用模型：仅可验证全部保留的 A/D 路径。")
        print("  3. 使用 Vultr Serverless Inference：可能产生费用。")
        print("  模型接收匿名规则/统计；发生自愈时还会接收失败作业的执行源码。")
        try:
            choice = input("请输入 1、2 或 3（默认 2）：").strip() or "2"
        except EOFError as exc:
            raise LaunchError("模型配置", "需要交互选择模式；也可指定 --mode preserve。") from exc
        if choice not in ("1", "2", "3"):
            raise LaunchError("模型配置", "请选择 1、2 或 3。")
        mode = {"1": "deepseek", "2": "preserve", "3": "vultr"}[choice]
        allow_external = choice != "2"
    if mode in ("deepseek", "vultr") and not allow_external:
        raise LaunchError("模型配置", "真实模型需要明确同意外发和费用；请交互选择或加 --allow-external。")
    if mode == "preserve":
        env["AI_AGENT_LLM_PROVIDER"] = "disabled"
        env["AI_AGENT_LLM_ALLOW_EXTERNAL"] = "false"
        env.pop("DEEPSEEK_API_KEY", None)
        env.pop("VULTR_SERVERLESS_INFERENCE_API_KEY", None)
        step("模型配置", "已关闭模型；获批修改的任务不会自动执行。")
        return
    if mode == "vultr":
        env.pop("DEEPSEEK_API_KEY", None)
        if not env.get("AI_AGENT_LLM_MODEL"):
            if not sys.stdin.isatty():
                raise LaunchError("模型配置", "请通过 AI_AGENT_LLM_MODEL 指定 Vultr 模型 ID。")
            env["AI_AGENT_LLM_MODEL"] = input("请输入 Vultr 模型 ID：").strip()
        key_name = "VULTR_SERVERLESS_INFERENCE_API_KEY"
        if key_name not in env and not (ROOT / ".env.vultr").is_file():
            if not sys.stdin.isatty():
                raise LaunchError("模型配置", "缺少密钥：请配置私有 .env.vultr 或在交互终端输入。")
            key = getpass.getpass("请输入 Vultr 推理 API Key（不回显、不保存；留空取消）：")
            if not key:
                raise LaunchError("模型配置", "未提供密钥，启动已取消。")
            env[key_name] = key
        env["AI_AGENT_LLM_PROVIDER"] = "vultr"
        env["AI_AGENT_LLM_ALLOW_EXTERNAL"] = "true"
        run([VENV_PYTHON, "-c",
             "import os; from backend.llm.vultr import VultrInferenceClient; "
             "VultrInferenceClient.from_environment(os.environ['AI_AGENT_LLM_MODEL'])"],
            "模型配置", "密钥或模型配置无效，请检查环境变量及 .env.vultr；不会打印密钥。",
            env=env, quiet=True)
        step("模型配置", "密钥格式已检查；尚未连接模型，真实任务可能产生费用。")
        return
    env.pop("VULTR_SERVERLESS_INFERENCE_API_KEY", None)
    if "DEEPSEEK_API_KEY" not in env and not (ROOT / ".env.deepseek").is_file():
        if not sys.stdin.isatty():
            raise LaunchError("模型配置", "缺少密钥：请配置私有 .env.deepseek 或在交互终端输入。")
        key = getpass.getpass("请输入 DeepSeek 密钥（不回显、不保存；留空取消）：")
        if not key:
            raise LaunchError("模型配置", "未提供密钥，启动已取消。")
        env["DEEPSEEK_API_KEY"] = key
    env["AI_AGENT_LLM_PROVIDER"] = "deepseek"
    env["AI_AGENT_LLM_MODEL"] = env.get("AI_AGENT_LLM_MODEL") or "deepseek-flash"
    env["AI_AGENT_LLM_ALLOW_EXTERNAL"] = "true"
    run([VENV_PYTHON, "-c",
         "import os; from backend.llm.deepseek import DeepSeekClient; "
         "DeepSeekClient.from_environment(os.environ['AI_AGENT_LLM_MODEL'])"],
        "模型配置", "密钥或模型配置无效，请检查环境变量及 .env.deepseek；不会打印密钥。",
        env=env, quiet=True)
    step("模型配置", "密钥格式已检查；尚未连接模型，真实任务可能产生费用。")


def port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def fresh_services(root, started):
    for name in ("api", "runner", "worker"):
        data = read_json(root / "services" / f"{name}.json")
        if not isinstance(data, dict) or not isinstance(data.get("seen"), (int, float)):
            return False
        if data["seen"] < started:
            return False
    return True


def wait_ready(process, root, port, token, started, timeout=60):
    deadline = time.monotonic() + timeout
    opener = build_opener(ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LaunchError("服务启动", "服务提前退出，请检查上方进程输出。")
        try:
            request = Request(f"http://127.0.0.1:{port}/ready", headers={"X-Demo-Token": token})
            with opener.open(request, timeout=2) as response:
                data = json.load(response)
            if isinstance(data, dict) and data.get("ready") is True and fresh_services(root, started):
                return
        except (HTTPError, URLError, OSError, ValueError, TimeoutError):
            pass
        time.sleep(0.25)
    raise LaunchError("服务启动", "等待 API、Runner、worker 就绪超时，请检查上方进程输出。")


def show_status(root, port):
    if not VENV_PYTHON.exists():
        raise LaunchError("状态", "尚未创建项目虚拟环境，请先运行 python start.py。")
    env = dict(os.environ)
    env.pop("DEEPSEEK_API_KEY", None)
    env.pop("VULTR_SERVERLESS_INFERENCE_API_KEY", None)
    env.pop("VULTR_API_KEY", None)
    env.pop("AI_AGENT_DEMO_TOKEN", None)
    raw = run([VENV_PYTHON, "-m", "backend.local_runtime", "status", "--data-dir", root],
              "状态", "无法查询本地服务心跳，请检查虚拟环境及数据目录。",
              env=env, timeout=15, quiet=True)
    try:
        state = json.loads(raw)
        services = state["services"]
        if (not isinstance(services, dict) or type(state["ready"]) is not bool
                or any(not isinstance(services.get(name), dict)
                       or type(services[name].get("ready")) is not bool
                       for name in ("api", "runner", "worker"))):
            raise ValueError("invalid status")
    except (ValueError, TypeError, KeyError):
        raise LaunchError("状态", "服务心跳结果无效，请检查启动终端。") from None

    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            data = response.read(4097)
        health = json.loads(data) if len(data) <= 4096 else None
        api_reachable = (isinstance(health, dict) and health.get("status") == "scaffold"
                         and health.get("execution_enabled") is False)
    except (HTTPError, URLError, OSError, ValueError, TimeoutError):
        api_reachable = False

    step("API HTTP", f"127.0.0.1:{port}/health {'可访问' if api_reachable else '不可用或返回异常'}（未鉴权）。")
    for name in ("api", "runner", "worker"):
        item = services[name]
        age = item.get("age_seconds")
        age_text = f"，{age} 秒前" if isinstance(age, (int, float)) else ""
        step(name, f"最近心跳{'有效' if item['ready'] else '缺失或过期'}{age_text}。")
    print("状态基于 HTTP 和最近心跳，不证明进程实时存活、Docker 或模型可用；启动时会另行鉴权检查 /ready。",
          flush=True)
    if not api_reachable or not state["ready"] or not all(
        services[name]["ready"] for name in ("api", "runner", "worker")
    ):
        raise LaunchError("状态", "服务尚未全部就绪，请查看启动终端；若使用自定义端口，请同时指定 --port。")
    step("状态", "API 可访问，三项最近心跳有效。")


def request_stop(root, timeout=110):
    services = root / "services"
    if not root.is_dir() or not any((services / f"{name}.json").exists()
                                    for name in ("api", "runner", "worker")):
        raise LaunchError("停止", "未发现服务心跳；没有发出停止请求，请确认监督器是否仍在运行。")
    marker = root / "stop.request"
    marker.touch(exist_ok=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not marker.exists() and not any((services / f"{name}.json").exists()
                                           for name in ("api", "runner", "worker")):
            return
        time.sleep(0.25)
    raise LaunchError("停止", "未确认服务停止及容器回收，请检查启动终端和 Docker；未强制终止。")


def launch(port, mode, allow_external, open_browser=True):
    step("环境", "检查 Windows、Python 与 Docker。")
    target_platform = check_platform()
    env = dict(os.environ)
    secret_names = ("DEEPSEEK_API_KEY", "VULTR_SERVERLESS_INFERENCE_API_KEY", "VULTR_API_KEY")
    inherited_keys = {name: env.pop(name) for name in secret_names if name in env}
    env.setdefault("DOCKER_HOST", WINDOWS_DOCKER_HOST)
    env["AI_AGENT_EXECUTION_BACKEND"] = "disabled"
    check_docker(env)
    if not port_available(port):
        raise LaunchError("端口", f"127.0.0.1:{port} 已被占用，请关闭占用程序或使用 --port。")
    prepare_dependencies(env)
    prepare_images(target_platform, env)
    for name in ("DEEPSEEK_API_KEY", "VULTR_SERVERLESS_INFERENCE_API_KEY"):
        if name in inherited_keys:
            env[name] = inherited_keys[name]
    choose_model(mode, allow_external, env)
    token = secrets.token_urlsafe(24)
    env["AI_AGENT_DEMO_TOKEN"] = token
    command = [str(VENV_PYTHON), "-m", "backend.local_runtime", "start",
               "--data-dir", str(RUNTIME), "--images", str(IMAGES), "--port", str(port)]
    step("服务启动", "启动 API、Runner 和 Docker worker；请保持本窗口打开。")
    started = time.time()
    try:
        process = subprocess.Popen(command, cwd=ROOT, env=env)
    except OSError as exc:
        raise LaunchError("服务启动", "无法启动本地监督器。") from exc
    try:
        wait_ready(process, RUNTIME, port, token, started)
        url = f"http://127.0.0.1:{port}/"
        step("就绪", "三进程心跳已就绪。模型连通性和业务任务尚未验证。")
        print(f"页面：{url}")
        print(f"本次演示令牌（在页面输入）：{token}", flush=True)
        print("请手动上传、启动检测、审批并观察证据；关闭请按 Ctrl+C。", flush=True)
        if open_browser:
            try:
                if not webbrowser.open(url):
                    print("未能自动打开浏览器，请手动访问上面的地址。", flush=True)
            except Exception:
                print("未能自动打开浏览器，请手动访问上面的地址。", flush=True)
        result = process.wait()
        if result:
            raise LaunchError("运行", "监督器异常退出，请检查上方输出和 Docker 回收状态。")
    except (KeyboardInterrupt, LaunchError) as original:
        if process.poll() is None:
            (RUNTIME / "stop.request").touch(exist_ok=True)
            try:
                process.wait(timeout=110)
            except subprocess.TimeoutExpired as exc:
                raise LaunchError("停止", "监督器未及时退出，请检查 Docker；未强制终止。") from exc
        if process.returncode:
            if isinstance(original, LaunchError):
                raise LaunchError(original.stage, f"{original} 监督器返回非零，请检查回收状态。") from original
            raise LaunchError("停止", "监督器或容器回收失败，请检查上方输出和 Docker。")
        raise


def main():
    parser = argparse.ArgumentParser(description="AI Data Agent Windows local demo launcher")
    parser.add_argument("command", nargs="?", choices=("start", "stop", "status"), default="start")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--mode", choices=("deepseek", "vultr", "preserve"))
    parser.add_argument("--allow-external", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        if args.command == "start":
            launch(args.port, args.mode, args.allow_external, not args.no_browser)
        elif args.command == "stop":
            request_stop(RUNTIME)
            step("停止", "监督器已退出并完成回收流程。")
        else:
            show_status(RUNTIME, args.port)
    except KeyboardInterrupt:
        step("中断", "操作已中断；若服务已启动，请检查监督器和 Docker 回收状态。")
    except LaunchError as exc:
        print(f"[失败：{exc.stage}] {exc}", file=sys.stderr, flush=True)
        return 1
    except OSError:
        print("[失败：文件访问] 无法读写项目文件，请检查文件是否完整、权限及磁盘空间。",
              file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
