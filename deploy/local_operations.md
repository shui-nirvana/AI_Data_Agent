# 本地运行与维护

本页仅面向受控单机、非敏感演示。API/Runner 不解析 CSV 或执行生成源码；只有独立 worker 使用 Docker。进程分离不等于不同系统用户的权限隔离，公网与生产部署需另行设计服务账户及权限。

## 安装与启动

Windows 推荐使用独立虚拟环境，避免污染系统 Python：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --require-hashes -r deploy/control-windows.lock
python -m pip install --no-deps -e .
$env:DOCKER_HOST = "npipe:////./pipe/dockerDesktopLinuxEngine"
python -m workers.sandbox_manager.build_images --target local --platform linux/amd64
$env:AI_AGENT_DEMO_TOKEN = "your-private-local-token"
# 修改分支需要模型；全部保留不需要
$env:AI_AGENT_LLM_PROVIDER = "deepseek"
$env:AI_AGENT_LLM_MODEL = "deepseek-flash"
$env:AI_AGENT_LLM_ALLOW_EXTERNAL = "true"
python -m backend.local_runtime start --data-dir .runtime --images .runtime/docker/images.json
```

密钥仅放项目根 `.env.deepseek`，或使用 Runner 的环境变量，文件不入 Git/镜像。监督器不会向 API/worker 子进程传递 `DEEPSEEK_API_KEY`，作业容器仅获固定标志。作业依赖在 `requirements-job.lock` 全量固定版本/hash；控制层锁是 Windows/Python 3.12 实测版本，不作为 Linux/其他 Python 版本的已验收锁。基础镜像使用 digest；升级锁或镜像后重做测试。

## 停止与就绪

另一个终端：

```powershell
python -m backend.local_runtime status --data-dir .runtime
python -m backend.local_runtime stop --data-dir .runtime
```

浏览器访问 `http://127.0.0.1:8000/`。上传后显式启动；检测完成后逐列审批，默认保留，至少一列不修改。成功下载 `output`、`validation` 和 `manifest`。刷新后 URL 带任务 ID，重新输入令牌并点击加载。

`/health` 只表明 API 存活；带演示令牌的 `/ready` 对三进程心跳判断，缺失/超过 360 秒返回 503。宽限覆盖最多三次 90 秒格式重生成，不代表即时探测 Docker 或供应商健康。任一子进程退出或心跳过期，监督器停止整组服务，不隐式重启模型调用。正常停止后运行回收专用 worker；回收失败时退出非零，需检查 Docker，不能宣称容器已清理。监督器自身强杀仍需重启 worker 对账。

独立回收不消费新作业：

```powershell
python -m workers.sandbox_manager.queue_worker --data-dir .runtime --images .runtime/docker/images.json --recover-only
```

旧 running 作业先按 attempt 标签回收，再标记失败；不自动重跑旧 C。单机 OS 锁不等于多机租约；API/Runner/worker 必须使用同一绝对目录。

## 容量与预算

上传默认最多 10 MiB、最多 20 个非终态任务、保留至少 128 MiB 磁盘余量，分别用 `AI_AGENT_MAX_UPLOAD_BYTES`、`AI_AGENT_MAX_ACTIVE_TASKS`、`AI_AGENT_MIN_FREE_DISK_BYTES` 配置。容量不足返回 429/507，不留下上传文件。活动任务上限在任务入库事务内再次核对。

C 固定内存/PID/tmpfs/墙钟上限及 cgroup 总 CPU 预算，不能由 HTTP/模型放宽。模型每任务最多三次调用、每次最多 4096 输出 token/90 秒；失败/不确定调用不重发。页面显示提供方报告的 token，缺失为 null；货币价格、余额和实际账单不猜测。不存在全账户日费用自动封顶或生产级磁盘监控，操作员须核对提供方账单和磁盘。

## 离线备份与恢复

先停服。监督模式正常停止会移除心跳；手动启动的进程退出后须等待旧心跳过期。维护入口拒绝活动服务或占用的 Runner/worker/监督器锁。禁止同时启动 API。

```powershell
python -m backend.local_runtime backup --data-dir .runtime --archive E:\backups\agent-private.zip
# 原目录先由操作员移到其他名称，确认无服务；恢复只允许原绝对路径且目标不存在
python -m backend.local_runtime restore --data-dir .runtime --archive E:\backups\agent-private.zip
```

备份目标父目录须已存在，且不能在 runtime 内。备份含上传数据、规范、源码和日志，是私有敏感材料，不是可公开的部署包；使用访问权限/外部加密保护。恢复拒绝路径穿越、链接、重复成员、超过 2 GB/十万成员及损坏 SQLite。数据库/checkpoint 内持久化绝对文件引用，因此不支持迁移到另一目录；备份 ZIP 不是签名或业务真实性证明，仅恢复受信备份。

## 过期文件清理

```powershell
# 默认仅列出，不删除
python -m backend.local_runtime cleanup --data-dir .runtime --days 7
# 审核列出的任务后执行
python -m backend.local_runtime cleanup --data-dir .runtime --days 7 --apply
```

只清理最后事件超过期限的 `SUCCEEDED/FAILED/CANCELLED`，且没有 running/queued 作业。删除原件、规范/源码、快照、输出和作业日志；保留 DB 审计元数据，删除下载登记。活动/待审批任务不删。拒绝链接和目录跳转；文件系统与 DB 不是同一事务，清理中断可重新执行，不让残留文件变成可下载产物。当前由操作员定期离线执行，不是常驻 TTL 守护；清理后无法复现旧原始数据，需先备份。

## 验收命令

```powershell
python -m pytest -q
$env:AI_AGENT_RUN_DOCKER_TESTS = "1"
python -m pytest tests/test_local_safety_docker.py tests/test_workflow_docker.py tests/test_llm_docker.py -q
# 以下会产生真实模型费用
$env:AI_AGENT_RUN_LIVE_LLM_TESTS = "1"
$env:AI_AGENT_LLM_ALLOW_EXTERNAL = "true"
python -m pytest tests/test_llm_docker.py -k live -q
$env:AI_AGENT_RUN_BROWSER_TESTS = "1"
python -m playwright install chromium --only-shell
# 默认独立 Chromium headless shell；可显式配置 AI_AGENT_BROWSER_CHANNEL
python -m pytest tests/test_browser.py -q
```

PID/后台进程测试使用固定受信故障驱动，仅在测试中绕过 Python 审计以检验相同 Docker 策略；不是生产绕过入口。Docker 超时为 API 异常注入，不等同真实永久挂死。没有内核逃逸、安全认证、多 VM 高可用或 Vultr 实测声明。
