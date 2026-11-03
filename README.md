# AI Data Agent

沙箱优先的数据处理 Agent 项目：固定检测/审批规范/独立验证已有，**本地 Docker、持久化 Graph Runner、DeepSeek 适配器和有限自愈已编码**；真实 DeepSeek 与 Vultr 验收、发布尚未完成。
实施路径请参考 `AI_Data_Agent_详细设计与实施路线图_v2.md`。

## 当前已实现功能

- **最小 Web 页面**：支持带有鉴权的、受限大小的原始 CSV 上传。
- **持久化控制平面**：SQLite 存储任务/事件/作业/outbox；上传计算 SHA-256，worker 输入快照只读挂载。
- **任务管理**：支持任务查询、事件游标跟踪和取消操作。
- **固定检测 A 核心**：结构、空字符串、完全重复行、白名单日期格式、严格十进制数值范围；同列发现合并为一张卡片。只检测、不改原件。
- **审批与恢复**：完整问题集、服务端身份、任务版本、输入/规范哈希、同列冲突校验；规范与恢复 outbox 同事务，重复相同审批幂等。
- **独立验证 D 核心**：重新读取原件/候选，逐格核验日期规范化、明确字面值填空及保护单元格；零变更要求字节哈希一致。报告不泄漏原始值。
- **作业契约**：A/C/D 的 UUID、阶段与十六进制哈希校验。
- **Docker 开发 worker**：固定不可变镜像 ID、新容器、只读输入/根、非 root、禁网、资源限额；受信收集器停止遗留子进程并有界读取 tmpfs，收集后暂停/回收容器。
- **Graph Runner**：独立进程使用 SQLite checkpoint 和 `interrupt()`；通过持久作业队列驱动独立 worker，审批等待不占容器/HTTP/DB 连接。Runner/API 均不导入 Docker。
- **LLM 与自愈**：提供方无关生成器使用匿名规范生成完整源码；DeepSeek 从环境或本地私有文件读取密钥，错误响应不隐式重试。每任务最多 3 次模型调用、3 次 C；运行实现/缺输出错误可修复，安全/资源/D 拒绝停止。
- **本地测试与开发包**：独立 A/C/D 测试源码烟测、隔离检查入口，支持 local/vultr/aws 部署标识与显式 Linux CPU 架构，导出源码 zip/镜像 tar/manifest。

## 尚未实现（待办）

真实 DeepSeek API 联调、Vultr 验收、产物原子发布/下载和完整审批 UI 尚未完成。模型/自愈已实现，并以模型替身和真实 Docker 测试，不能算作真实供应商证据。当前单机队列用 OS 文件锁串行执行，worker 重启后旧作业失败，不重跑旧 C；多机租约/完整恢复待补。`VALIDATED` 尚未发布，不等同 `SUCCEEDED`。

**警告**：上传后停在 `UPLOADED`；需显式启动编排并运行独立 Runner/worker。API 不解析 CSV 或执行源码。真实容器与集成烟测使用人工测试源码，不冒充 LLM；没有模型时，获准修改分支明确失败，全部保留分支可直接经 D 验证。请勿使用敏感文件或开放公网。MVP 暂不包含 B。

## 本地启动步骤

1. **安装依赖**：

   ```powershell
   python -m pip install -e ".[test]"
   ```

2. **设置环境变量并启动**：

   ```powershell
   # 设置一个私有的演示令牌
   $env:AI_AGENT_DEMO_TOKEN = "your-private-local-token"
   # 启动 FastAPI 服务
   python -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8000
   ```

3. **测试交互**：
   访问 `http://127.0.0.1:8000/`，输入相同的令牌并上传一个测试用 CSV。
   _注意：本地任务数据保存在 `.runtime/` 目录中（已在 .gitignore 中排除）。_

## 本地 Docker 测试

手动安装 Docker Desktop 并启动 Linux 容器引擎；确认 `docker info` 返回 `OSType: linux`。SDK 不等于 Docker 引擎，不要给 FastAPI 挂载 socket。

```powershell
python -m pip install -e ".[worker,test]"
python -m pytest -q
python -m workers.sandbox_manager.build_images --target local --platform linux/amd64
python -m workers.sandbox_manager.smoke_test
python -m workers.sandbox_manager.isolation_test
```

ARM64 主机改用 `--platform linux/arm64`。构建需联网，作业执行禁网。已在本机 Docker Desktop/Linux 实际构建并通过 A/C/D、禁网/只读/tmpfs/日志、超时和 OOM 检查；不代表已完成 Vultr 或所有内核隔离验收。Windows PATH 尚未刷新时的处理见 `deploy/README.md`。

## 独立 Graph Runner

安装 `python -m pip install -e ".[worker,workflow,test]"`，构建镜像后，在两个终端分别运行：

```powershell
python -m backend.orchestration.runner --data-dir .runtime
python -m workers.sandbox_manager.queue_worker --data-dir .runtime --images .runtime/docker/images.json
```

另开终端启动 API，上传后调用 `POST /api/tasks/{task_id}/start`；等待 `AWAITING_DECISIONS`，通过 `GET /api/tasks/{task_id}/review` 取得问题和版本，再提交 `/decisions`。页面目前只上传/查询，审批按 API 操作。完整 PowerShell 示例见 `deploy/graph_runner.md`。

### 启用 DeepSeek

把密钥放入项目根目录的 `.env.deepseek`，或设置 `DEEPSEEK_API_KEY` 环境变量（环境变量优先）。在 Runner 终端运行；普通 `.env` 不自动加载，API/worker 不需要密钥：

```powershell
python -m pip install -e ".[llm,worker,workflow,test]"
$env:AI_AGENT_LLM_PROVIDER = "deepseek"
$env:AI_AGENT_LLM_MODEL = "deepseek-flash"
$env:AI_AGENT_LLM_ALLOW_EXTERNAL = "true"
python -m backend.orchestration.runner --data-dir .runtime
```

模型只收到匿名列索引、动作和聚合计数，真实列名/填充值运行时读取；不外发原始行/日志。调用会产生费用，缺密钥或外发授权时明确失败。完整协议、预算、自愈和实测命令见 `deploy/deepseek.md`。API 终端保持 `AI_AGENT_LLM_PROVIDER=disabled`。

真实 Docker + Graph 集成回归（人工测试源码，需 Docker 已启动）：

```powershell
$env:AI_AGENT_RUN_DOCKER_TESTS = "1"
python -m pytest tests/test_workflow_docker.py -q
```

Vultr 开发部署包：

```powershell
python -m workers.sandbox_manager.build_images --target vultr --platform linux/amd64 --export --output .runtime/vultr
```

这将生成 `images.json`、`job-image.tar` 和 `application.zip`，不含任务数据或真实 `.env`。它不是完整 Web/Graph 服务安装器，目标标识不代表 Vultr 已验收；目标 VM 的 CPU 架构必须匹配。导入步骤、压力检查和剩余验收见 `deploy/README.md`。

## 开发说明

- 第一个上传接口接受 `Content-Type: text/csv` 的**原始请求体**，配合 `X-Task-Goal` 和 `X-Demo-Token` 请求头。
- 中文目标需用 `encodeURIComponent` 百分号编码；演示令牌为共享本地身份，不是多用户认证。`.env.example` 不自动加载。
- 此临时接口仍不同于目标 multipart 契约；公网前须完成代理上传限额、限流、真实认证和隔离验收。
- 运行测试：`python -m pytest -q`。
- 环境要求：Python 3.11+。

## 本轮规则边界与下一步

CSV：UTF-8 可含 BOM、逗号分隔、严格记录列数、非空唯一表头，保留字面值；缺失仅为空字符串，空格/NA 不自动视为空。
日期仅支持明确指定的 `YYYY-MM-DD`、`YYYY/MM/DD`、`DD-MM-YYYY`；非法日期/未知格式保留，不猜时区或地区格式。
每列一项动作，至少保留一列；无法建立可核查对应关系的复杂操作不支持。D 验证只能证明规则与位置结果，不能证明业务规则天然正确。

详细进度与验收缺口见 `deploy/implementation_status.md`。下一步用可见密钥跑真实 DeepSeek 两组金样本及修复，补候选冻结/原子发布、真实并发取消/崩溃/子进程验收，在 Vultr 重测隔离与性能。

## 平台与模型解耦

- `backend/config.py` 将部署位置、执行后端、模型提供方/模型名分开配置，默认 `local/disabled/disabled`；部署 Vultr 时设置 `AI_AGENT_DEPLOYMENT_PLATFORM=vultr`，该标识不启用执行。
- `backend/bootstrap.py` 在受信启动时注入 `ApplicationDependencies`；FastAPI 仅持有应用端口，不导入 worker、云 SDK 或模型 SDK。
- `backend/jobs/execution.py` 定义固定作业的提交/状态/取消端口；`workers/sandbox_manager/runner.py` 注入具体执行适配器，配额来自平台无关的 `backend/jobs/policy.py`。
- `backend/llm/client.py` 定义模型请求/响应及网关；具体提供方负责 SDK/认证/超时与错误映射，`CodeGenerator` 的审批和源码规则不随供应商变动。
- API 启动仍默认 disabled；配置其他后端但未注入实现会明确报错。Docker 适配器仅由独立 worker 加载；测试替身不执行容器、不联网、不冒充真实模型。

AWS EC2 后续可复用通用 Linux/OCI worker；AWS 托管执行、对象存储和多 VM 队列均未实现。更换平台/模型必须重做隔离、隐私、成本和金样本验收，不能仅靠配置宣称完成迁移。比赛部署仍使用 Vultr。
