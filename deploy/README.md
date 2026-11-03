# Docker 本地测试与开发部署包

## 当前边界

已有固定镜像、独立 Docker 队列 worker、Graph Runner/审批/outbox、单机文件锁和重启失败式对账，仅用于非敏感开发数据。没有生产 Compose、多机租约或完整服务安装器。Docker socket 权限接近宿主管理员权限，只给 worker，不能挂进 FastAPI/Graph Runner，也不能对公网暴露。

已在本机 Docker Desktop/Linux 构建真实镜像，通过 A/C/D 烟测、五项隔离/压力检查，以及 Graph 审批重启后驱动独立 worker 的两条集成路径；尚无 Vultr 执行证据，也不是完整安全认证。

## 本地准备

1. 手动安装 Docker Desktop，按安装器要求启用 WSL 2/虚拟化，必要的重启由用户完成。启动 Docker Desktop，选择 Linux containers。
2. 在项目根目录运行以下命令。`docker info` 必须显示 Linux、seccomp 和内存/Swap/PID 限额能力；缺少能力时适配器明确拒绝。

```powershell
docker version
docker info
python -m pip install -e ".[worker,test]"
python -m pytest -q
python -m workers.sandbox_manager.build_images --target local --platform linux/amd64
python -m workers.sandbox_manager.smoke_test
python -m workers.sandbox_manager.isolation_test
```

若 Windows 安装后 IDE 还没有刷新 PATH，当前终端可补入 Docker 工具目录。SDK 不自动使用 CLI context，需要时显式选择 Desktop Linux 命名管道；仅在构建/worker 终端设置：

```powershell
$env:PATH = "$env:ProgramFiles\Docker\Docker\resources\bin;$env:PATH"
$env:DOCKER_HOST = "npipe:////./pipe/dockerDesktopLinuxEngine"
docker --context desktop-linux info
```

本机引擎的 archive API 无法读取 tmpfs 文件，因此产物由固定镜像内的可信 `images.collector` 收集：停止遗留子进程、拒绝链接/非普通文件、按固定名字和总限额传回 tar，worker 再暂停/回收容器。没有增加宿主可写挂载或容器权限；D 核验的是保存下来的候选字节。

ARM64 开发机选择 `--platform linux/arm64`；跨架构构建依赖引擎的模拟支持，速度和验收结果不可等同原生执行。当前使用 Python 3.12 基础镜像摘要、固定顶层 Python 依赖版本；传递依赖尚未全量 hash lock。执行阶段不拉取镜像、不安装依赖，仅接受已构建的不可变本地镜像 ID。

正常烟测生成无敏感 CSV，运行三个全新 A/C/D 容器：A 检测、测试规范选择日期动作、C 执行人工源码、D 独立验证。这不是线上审批事务，也不是 LLM 演示。成功时打印 D 改动数；任何作业失败会抛异常并留下该次私有 `execution.json`。

证据默认位于 `.runtime/docker/`：`images.json`、每次烟测的 `smoke.json`、每个 attempt 的 `execution.json`、限长日志、固定产物。D 非零退出的有效报告保存在 `diagnostics/`，不能当作成功候选或发布文件。日志可能包含测试数据，不能直接外发给模型。

## 隔离与压力检查

默认隔离入口检查非 root、无有效 capabilities、no-new-privileges、seccomp、只读输入/根、无 socket、无额外宿主标记挂载、外网连接失败、output/tmp 容量、日志截断及每次容器回收。无额外挂载的标记测试不等于证明能抵御任意宿主文件读取或内核逃逸。

仅在专用开发机运行可选压力测试，会主动触发 C 内存限额，并等待约 60 秒墙钟超时：

```powershell
python -m workers.sandbox_manager.isolation_test --stress
```

报告为每次测试目录的 `isolation.json`；已完成的检查也会在后续检查失败时保留。PID 耗尽、总 CPU、真实并发取消、恶意子进程、worker 被杀/重启、Docker API 卡住及多任务并发仍需目标机单独验收。SDK 的调用超时不等于已实现崩溃后的自动清理。

## Vultr 开发包

在本地构建匹配目标 VM CPU 的 Linux 镜像，例如 Vultr x86_64：

```powershell
python -m workers.sandbox_manager.build_images --target vultr --platform linux/amd64 --export --output .runtime/vultr
```

产物：

- `job-image.tar`：A/C/D 共用的固定镜像，各次作业仍使用新容器。
- `application.zip`：白名单应用/worker/前端源码、部署说明、示例配置，不含用户 CSV、任务目录、真实 `.env`、缓存或 Git。
- `images.json`：不可变镜像 ID、基础摘要、CPU 架构和目标标识；`tested_on_target=false`。
- zip 内 `deployment.json`：同一目标配置，供查看；烟测读取外部 `images.json`。

在目标 Linux VM 安装 Docker Engine/Python，检查 CPU 架构；解压源码后将 `images.json` 和镜像 tar 放在项目根目录，再运行：

```bash
docker load --input job-image.tar
python3 -m venv .venv
.venv/bin/python -m pip install ".[worker,test]"
.venv/bin/python -m workers.sandbox_manager.smoke_test --images images.json
.venv/bin/python -m workers.sandbox_manager.isolation_test --images images.json --stress
```

单机 Graph Runner/API/worker 的启动和审批示例见 `graph_runner.md`；安装编排依赖时增加 `.[workflow]`。源码包不含 pytest 测试文件，云端生产验收需另保留金样本/测试套件。

镜像 tar 支持离线导入，但 Python worker 安装仍需包源网络或另外准备 wheelhouse，不是全离线安装器。宿主不会执行 CSV 解析或 C 源码。若目标镜像 ID 不匹配、不是 Linux 或能力不足，拒绝运行，不自动降级。

`--target aws` 只改变部署标识，可供相同 Linux VM 流程使用，不代表已实现 AWS 云调度。标识和包名不构成部署成功证据；本地通过后仍需在 Vultr 保存上述验收记录。当前脚本保守地不自动改写平台验收为 true。

## 公网开放前

补齐真实认证/上传限流、分布式租约与完整崩溃对账、真实供应商/自愈联调、D 候选内容冻结和发布哈希。DeepSeek 适配器/匿名源码生成/有限自愈已实现并做模拟模型 + 真容器回归，真实 DeepSeek 尚未验收，配置见 `deepseek.md`。只有 D 通过且发布字节一致的候选才可下载；当前 `VALIDATED` 不开放下载。容器共享宿主内核，不能承诺绝对隔离。
