# AI Data Agent

License: [MIT](LICENSE).

沙箱优先的数据处理 Agent：本地固定检测、浏览器审批、DeepSeek 真正编写源码、新 Docker 容器执行、有限自愈、独立验证、原子发布和鉴权下载已实现。真实模型受控修复已通过；**Vultr 正式运行和生产开放验收仍后置**。仅用于受控单机非敏感演示。
实施路径请参考 `AI_Data_Agent_详细设计与实施路线图_v2.md`。

## 当前已实现功能

- **Web 工作台**：上传、显式启动、固定检测、默认保留的逐列审批、隐私确认、取消、状态事件、尝试/模型用量、验证摘要与下载；URL 中只保留任务 ID，刷新后重新输入令牌加载。
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
- **安全交付**：D 读取内容寻址快照；发布前复核原件、规范、C/D/代码与哈希绑定，原子写文件、事务登记后进入 `SUCCEEDED`。下载重新校验哈希，返回已校验字节。
- **本地运行管理**：三进程 fail-stop 监督、鉴权就绪探针、旧容器回收入口、离线备份/原路径恢复、过期终态文件清理、上传磁盘余量与活动任务上限；沙箱依赖和 Windows 控制层依赖具有 hash lock。

## 后置边界

Vultr 的隔离、性能和实际账单验收尚未运行。多用户认证、公网代理/限流、敏感数据合规、多 VM 租约、高可用、独立常驻清扫及 B/Excel/JSON 均不属于当前本地演示交付。单机队列串行运行；worker 重启先回收旧容器再失败，不自动重跑旧 C。`VALIDATED`/`PUBLISHING` 不允许下载，只有登记后的 `SUCCEEDED` 可以。
真实受控修复是在真实生成后人工注入 `RuntimeError`，随后由 DeepSeek 修复，并非模型自然失败率证据。D 拒绝继续停止，不能以改规则实现“自愈”。

**警告**：上传后停在 `UPLOADED`；需显式启动编排并运行独立 Runner/worker。API 不解析 CSV 或执行源码。固定源码烟测与模拟模型测试不能冒充真实 LLM；没有模型时，获准修改分支明确失败，全部保留分支可直接经 D 验证。请勿使用敏感文件或开放公网。MVP 暂不包含 B。

## 本地启动步骤

1. **安装依赖**：

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   # Windows 本地已验收锁；不要装入其他项目共享的系统 Python
   python -m pip install --require-hashes -r deploy/control-windows.lock
   python -m pip install --no-deps -e .
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

另开终端启动 API。页面上传后点击“启动固定检测”，等待审批、逐列选择动作并冻结；完成后下载。也可使用 `start/review/decisions/evidence/artifacts` API。完整示例见 `deploy/graph_runner.md`。

配置好令牌、镜像及模型后，也可以单命令启动和停止整组服务：

```powershell
python -m backend.local_runtime start --data-dir .runtime --images .runtime/docker/images.json
python -m backend.local_runtime status --data-dir .runtime
python -m backend.local_runtime stop --data-dir .runtime
```

`/health` 仅表示 API 存活；带 `X-Demo-Token` 的 `/ready` 检查三进程最近心跳。备份、恢复、清理及局限见 `deploy/local_operations.md`。

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

详细进度与验收缺口见 `deploy/implementation_status.md`，本地实施顺序和验收矩阵见路线图第 9 节。

## 本地验收范围

1. **交付**：零变更、重复发布、发布中断重放、发布前/后篡改、取消先后顺序、旧 `VALIDATED` checkpoint 恢复与鉴权下载。
2. **浏览器**：逐列动作、白名单日期、填充值、隐私确认、事件游标、尝试/模型来源、验证和 CSV 风险提示。
3. **Docker**：真实 A/C/D、模拟模型修复/拒绝，以及 PID 耗尽、总 CPU、后台子进程、并发取消、worker 强杀回收；Docker API 超时采用注入异常验证拒绝交付，不声称实测引擎永久挂死。
4. **模型**：真实日期、填空和明确注入故障后的真实修复。曾发生日期标签误作 `strftime` 格式而被 D 拒绝，已改提示并保留失败记录；少量通过不代表普遍可靠。
5. **运行管理**：就绪/监督、离线备份恢复、终态文件清理和容量拒绝。token 用量可见，货币费用不猜测，实际账单仍由操作员核对。

默认回归与外发/Docker/浏览器测试分开记录；最新命令、计数及实际证据见 `deploy/implementation_status.md`。跳过项不计为通过，当前实现不宣称覆盖所有断电窗口或内核攻击。

## 平台与模型解耦

- `backend/config.py` 将部署位置、执行后端、模型提供方/模型名分开配置，默认 `local/disabled/disabled`；部署 Vultr 时设置 `AI_AGENT_DEPLOYMENT_PLATFORM=vultr`，该标识不启用执行。
- `backend/bootstrap.py` 在受信启动时注入 `ApplicationDependencies`；FastAPI 仅持有应用端口，不导入 worker、云 SDK 或模型 SDK。
- `backend/jobs/execution.py` 定义固定作业的提交/状态/取消端口；`workers/sandbox_manager/runner.py` 注入具体执行适配器，配额来自平台无关的 `backend/jobs/policy.py`。
- `backend/llm/client.py` 定义模型请求/响应及网关；具体提供方负责 SDK/认证/超时与错误映射，`CodeGenerator` 的审批和源码规则不随供应商变动。
- API 启动仍默认 disabled；配置其他后端但未注入实现会明确报错。Docker 适配器仅由独立 worker 加载；测试替身不执行容器、不联网、不冒充真实模型。

AWS EC2 后续可复用通用 Linux/OCI worker；AWS 托管执行、对象存储和多 VM 队列均未实现。更换平台/模型必须重做隔离、隐私、成本和金样本验收，不能仅靠配置宣称完成迁移。比赛部署仍使用 Vultr。
