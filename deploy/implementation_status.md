# 实施状态与本轮验收

本文保留历史记录；当前结论以本轮交付及末尾实测为准，不修改尚未完成的 Vultr/生产验收目标。

## 当前本地交付

上传/固定 A、浏览器人工审批、真实 DeepSeek 完整源码、新 C、有限真实受控修复、新 D、快照/发布/下载及本地维护已实现。`SUCCEEDED` 才能下载，`VALIDATED` 不再是图终态。API/Runner 不解析 CSV、不运行模型源码、不持 Docker SDK。

- 默认锁定环境回归：**152 passed, 17 skipped**；跳过包括 Docker/真实模型/浏览器显式开关和宿主不允许创建 symlink 的测试。
- 最终重建镜像上的真实 Docker/模型集合：**15 passed，128.21 秒**，含 6 项 PID/后台子进程/总 CPU/取消/强杀/超时注入、2 条 Graph、4 条模拟模型、3 条真实模型。
- 完整浏览器移动视口用例：pytest **1 passed，18.31 秒**，完成真实上传、日期审批、真实生成、新 C/D、下载与重载；但 Shell 工具因宿主 NVIDIA 文件访问返回权限告警，不能记为工具层无告警通过。内置浏览器另验证零变更路径、事件和下载按钮。
- 锁定独立环境 `pip check`、JS 语法及 `git diff --check` 通过。沙箱传递依赖与 Windows 控制层具有版本/hash lock；不装入共享系统 Python。
- 实际交付 CSV、validation、manifest 已从浏览器成功任务经 Publisher 复核导出到项目根 `local_acceptance_*`。永久摘要、哈希与失败记录见 `本地闭环验收报告_2026-11-03.md`。

后置边界：Vultr 正式验收、公网/生产认证与限流、敏感数据合规、多机高可用、常驻清扫、全账户日费用封顶和未来 B。当前 TTL/备份采用人工停服维护，不宣称覆盖所有断电窗口或内核攻击；浏览器工具权限告警如上保留。

## 历史基线（本轮推进前）

| 路线图  | 本轮代码与测试                                                                       | 仍未完成                              |
| ------- | ------------------------------------------------------------------------------------ | ------------------------------------- |
| D0/D1   | 测试中有人工列出的日期期望值、合法负数退款、缺失、非法日期、重复行；新增错误输出反例 | 演示审批 UI、更多业务金样本和压力测量 |
| D2      | 规范/引用哈希、完整裁决、DB 审批/outbox 事务和固定镜像运行 manifest                  | 完整证据签发/发布                     |
| I2 本地 | 真实 A/C/D、禁网/只读/tmpfs/日志、超时/OOM 测试；受信收集与回收                      | Vultr、PID/CPU/并发取消/完整崩溃验收  |
| I3      | 单容器五类固定检测；入库前校验版本、完整工具集、输入、动作/范围和问题集合            | 工具级故障恢复/版本镜像保留           |
| I4 单机 | SQLite checkpoint、持久作业/outbox、interrupt/resume、审批幂等、独立 Runner/worker   | 多机租约、完整故障窗口和审批 UI       |
| I5/I7   | DeepSeek 适配器、匿名生成、持久调用预算；两组真实模型金样本及真实 Docker 模拟自愈    | 真实 DeepSeek 自愈、Vultr 证据        |
| I6 核心 | 新 D 调度、逐格验证、原件/规范/候选/报告哈希绑定；VALIDATED 未发布状态               | 候选内容冻结、发布重算哈希和原子发布  |

API 上传后仍为 `UPLOADED`，start/review/decisions 只写队列与审批，不解析 CSV。Runner 可注入 DeepSeek 或默认禁用；worker 独立执行。已有本地真实 DeepSeek 日期/填空金样本证据，自愈测试仍使用模拟模型。全部保留可直接验证；模型未配置时获准修改明确 FAILED。D 通过为 `VALIDATED`，发布/下载仍未完成。

## 冻结的初始语义

- CSV 解析版本 `csv-utf8-v1`：UTF-8（可含 BOM）、逗号、标准引号，拒绝 NUL、重复/空表头及坏记录；不推断数值或改变字面值。
- 缺失只指 `""`，不包括空格、`NA`；字面值填充最长 1024 字符，MVP 不新增以 `= + - @` 开头的值。原件中已有这些值并不自动删除或转义，下载仍须提示电子表格风险。
- 日期规则版本 `1.0`：完整位数的三种白名单格式，不启发式解析；非法/不匹配日期保持原值。
- 每列只裁决一个动作，同列检测结果合并成一张卡片；可选择日期规范化或填空之一，不自动串联。至少保留一个未修改列。现有列并不天然构成唯一业务键，仍需 D 按原顺序逐格验算。
- 规范哈希是内容绑定，不是审批签名。`freeze_spec()` 的发现集必须来自可信 A 结果；未来 HTTP 接口不得接受用户提交发现集代替真实结果。

## 历史待办顺序（本轮推进前）

1. 本地 P0：候选内容寻址冻结、D 绑定、发布前重算哈希、原子发布/鉴权下载及崩溃对账。
2. 本地 P0：启动、审批、状态/错误/验证摘要和下载的浏览器闭环；完成真实并发取消、PID/总 CPU、恶意子进程、进程强杀和 Docker API 阻塞验收。
3. 本地 P1：扩充真实供应商金样本和受控真实模型修复联调；完善进程监督、备份、TTL、容量与费用观测。
4. 本地闭环通过后在 Vultr 重做隔离/压力/图集成和性能验收。当前单机文件锁和旧 running 先回收后失败，不是多机高可用协议。

详细步骤及验收矩阵见 `AI_Data_Agent_详细设计与实施路线图_v2.md` 第 9 节；上述均为待完成项。

本机 Docker Desktop 4.97.0/Linux Engine 29.9.2 已启动并实测，运行在 WSL 2 内核。本轮没有 Vultr 凭据或云端运行证据；单元测试与真实 Docker 证据分别记录。

## 平台与模型解耦增量

已增加 `ExecutionBackend`（提交/状态/取消）、固定资源策略、`ModelClient/ModelGateway`（统一消息/响应/完成原因）、平台/模型独立配置及启动依赖注入。FastAPI 默认服务全部 disabled，不导入 worker 或具体 SDK；业务图与 CSV 验证规则不引用云 API。

Vultr/AWS 是部署位置，不是运行时；已有本地 Docker 实测及 DeepSeek HTTP 适配器，尚无云端部署验收。API 配置未注入后端仍明确报错，Graph 通过 DB 队列交给独立 worker，不向 API 注入 socket。SQLite/本地私有文件尚未抽象成多 VM 存储，迁移对象存储/远程队列需补一致性协议。

替换测试使用两个内存执行替身、两个模型替身；验证默认拒绝执行、跨部署标识应用不变、错误回报拒绝及截断/拒绝模型输出阻断。替身测试不代表云部署或真实模型成功；真实模型证据见下方 2026-11-03 记录。AWS 实测后置，不放宽既有隔离/审批/D 验证边界。

## Docker 开发增量

- A/C/D 同一固定镜像、各次新容器；镜像构建记录 Python 基础摘要与本地 image ID，显式 `linux/amd64` 或 `linux/arm64`。依赖构建时安装，传递依赖全量 hash lock 待补。
- stdout/stderr 放入受限 tmpfs，每条最多收集 64 KiB；可信收集器停止遗留进程，读取固定普通文件，通过有界 tar 回传，拒绝链接/目录/越界/超限，收集后暂停/回收容器。该引擎 archive API 看不到 tmpfs，不能用原先的 get_archive 方案。
- 输入/规范/源码/候选绑定哈希；执行退出码、超时、OOM 回报和产物集合联合判定。D 拒绝报告保留为诊断，清理或写盘失败不得返回成功。
- SDK 替身测试覆盖固定隔离参数、幂等/取消、超时/OOM、缺产物、写盘/回收失败、tar 契约及打包白名单。真实隔离入口已通过只读/禁网/tmpfs/日志/超时/内存压力检查。
- 导出包是源码 zip + 镜像 tar + 目标 manifest，不是完整部署/全离线安装器，也不会因选择 Vultr 就自动把 `tested_on_target` 改成 true。

Docker 适配器本身仍是同步 submit；队列 worker 另提供持久作业、单机锁、取消观察和重启失败式回收。回收失败会停止消费并保留 running，不视为已清理。多机租约、完整故障对账/独立清扫守护待补，不能用于公网任意代码执行。说明见 `deploy/README.md` 和 `deploy/graph_runner.md`。

### 2026-11-02 较早验证记录（安装引擎前）

- `python -m pytest -q`：68 passed；仍有一条 Starlette/httpx 弃用警告。
- `compileall` 与 build/smoke/isolation CLI 帮助入口通过；隔离测试源码只做编译检查，未在宿主执行。
- Docker SDK 连接检查失败：Windows Docker 命名管道不存在（CreateFile error 2）。未构建/导出真实镜像或运行容器；包导出仅有 SDK 替身测试证据。
- Docker Desktop 由用户手动安装，启动 Linux 引擎后按 `deploy/README.md` 完成真实测试，再记录本地及 Vultr 证据。

### 2026-11-02 本地真实验收与 Graph 增量

- 最终完整回归启用 `AI_AGENT_RUN_DOCKER_TESTS=1`：**90 passed**，含两条真实图/独立 worker 集成路径；本次无弃用警告。默认未启用 Docker 时两条真实测试跳过，不能记作真实通过。
- 锁定并实测 `langgraph==1.2.19`、`langgraph-checkpoint-sqlite==3.1.3`；CLI 与 Python 编译检查通过。单机 OS 锁、稳定 thread_id、短 DB 事务关闭、审批/outbox 原子性、重复事件/审批、过期/取消/旧回报拒绝均有回归。
- 实际固定镜像 ID 为 `sha256:580782dc41422f045b053dcb8c2dd32b08f83f3003d84aaef92ccc5deef86f40`，配置在 `.runtime/docker/images.json`。后续源码/依赖变化需重建，不能把这个 ID 当作永久最新版本。
- 正常 A/C/D 证据：`.runtime/docker/smoke/f001fd88-fd0f-4d69-9022-22cde0790fda/smoke.json`；D 改动数为 1，原件哈希不变，各阶段退出 0。
- 五项实际隔离证据：`.runtime/docker/isolation/dba453e4-5c37-439b-b543-ceae7db89fdf/isolation.json`。boundaries/tmpfs_quota/log_truncation/wall_timeout/memory_limit 全部通过；超时约 60 秒，OOM 退出 137，各次测试容器均回收。
- 图集成使用 API 真实上传/启动/审批、持久化 checkpoint、独立子进程 Docker worker。保留分支 A/D；测试生成器分支 A/C/D；重复审批不新建 C；最终 VALIDATED，未发布。集成运行数据在 pytest 私有临时目录中。
- Docker archive API 的 tmpfs 不可见是实际运行发现的问题，已替换为镜像内受信收集器，不增加可写宿主挂载。停止后台进程/有界 tar 路径和类型校验已有实现，恶意子进程与 PID/CPU 等完整攻击测试仍需补。
- 没有真实 LLM、修复循环或 Vultr 运行证据；本地通过不等于云端上线验收。前端只上传/查询，start/review/decisions 通过 API。

### 2026-11-02 DeepSeek 与有限自愈增量

- `DeepSeekClient.from_environment()` 读取 `DEEPSEEK_API_KEY`；Runner 启动时根据 provider/model/外发开关注入，API 不自动加载秘密。固定官方 HTTPS 地址、非思考输出、4096 token/90 秒、响应限额、无重定向/代理环境/隐式重试。
- 通用 `LLMCodeGenerator` 发送匿名列索引/动作/计数，不发送真实列名/身份/用户目标/行/填充值/原始日志。C 运行时读实际规范；模型写完整源码，控制层只检查 AST/JSON，不执行模型源码。
- 调用账本持久化 started/completed/invalid/failed、请求/提示词/源码哈希、实际模型与用量；不确定/失败调用不重复计费重发。每任务 3 次模型调用（格式重生成也计入）；源码最多 32 KiB。
- 作业表自动迁移为阶段/轮次唯一，保留旧作业。每轮代码保存到 workflow_codes；C 最多 3 次，新源码/新容器。只有明确运行实现错误和正常退出缺输出进入修复；安全/权限/输入/资源/收集/回收/worker 重启及 D 拒绝停止。
- 固定 C 入口产生粗粒度诊断和额外审计钩子，吞掉越权异常仍拒绝。钩子与同 UID 文件诊断不是任意恶意代码的认证证明，不能替代 Docker 隔离/独立 D/攻击验收。
- 全量实测：**129 passed, 2 skipped**。6 条真实 Docker 集成（原有 2 条 + 新的运行错误修复/缺输出修复/安全拒绝/D 拒绝 4 条）通过；新增模型协议与持久化预算测试通过。两条真实 DeepSeek API 金样本测试因密钥不可见/未启用 live 授权跳过。最后非外发回归 **123 passed, 8 skipped**，不启用 Docker 时全部真实集成跳过。
- 本轮只检查密钥是否存在，未打印/复制秘密；当前进程、User、Machine 的 `DEEPSEEK_API_KEY` 都不可见，未发起实际 DeepSeek 调用，不能宣称真实供应商联调或真实模型自愈通过。
- 重建镜像 ID：`sha256:28fd7dd45433bcf2528546a4fab69afe409da275cfb2a0841c115d86281a3754`。对应当前 C/收集器增量，旧镜像不能覆盖新诊断协议。Python 编译/Runner CLI/pip check 通过。
- 新镜像再次通过 A/C/D 烟测与五项隔离/压力检查，最新证据 `.runtime/docker/isolation/355ec06e-bc2c-4c15-a02f-12f3be925fc6/isolation.json`。boundaries 现在要求安全违规后失败（即使异常被脚本吞掉），不把该项当作底层 seccomp/capabilities 独立验证。真实测试容器无遗留。
- 使用说明见 `deploy/deepseek.md`；原子发布、完整审批隐私 UI、多机恢复与 Vultr 验收仍待完成。

### 2026-11-03 提示契约与真实模型联调

- 模型输入保留匿名扫描计数、日期格式和批准动作；增加谓词/规则版本，明确这些统计不是修改授权。提示词以脱敏结构示例区分消息内 `approved_rules` 与容器规范中的 `columns`、`decisions` 及 `params`，不外发原始列名、行、填充值或用户身份。
- 无外发回归：`134 passed, 8 skipped`；真实 Docker、模拟模型的自愈与拒绝检查：`4 passed`。
- 在独立临时测试目录运行真实 DeepSeek + Docker：日期规范化、字面值填空各一例，`2 passed`；均为真实模型初次生成、新 C 和新 D，D 分别验证 1 个单元格变更、0 个不匹配，状态为 `VALIDATED`。审批由测试脚本提交，非浏览器人工审批；测试后未发现本项目容器残留。
- 两次真实任务未触发修复，不能证明真实模型自愈，也不能据此估计整体可靠性；之前真实任务的策略拒绝和 D 拒绝仍保留在 `本地联调报告_2026-11-03.md`，本次不覆盖先前失败证据。
- 临时证据目录：`C:\Users\User\AppData\Local\Temp\ai-agent-prompt-live-178bf74c-9603-4abd-aa0e-381ff36ff9d7`，可能被系统清理。模型与用量分别为 `deepseek-flash`、959/1241 和 951/1183 输入/输出 tokens；未测试 Vultr 或发布下载。

### 推进前本地完整运行差距核对

- 对照当前 API、Graph、worker、模型和 Web 代码，发布仍未实现，artifact 接口返回 501，页面仅上传/查询；目标顺序及故障验收见路线图第 9 节。
- 重新运行默认不外发回归：`134 passed, 8 skipped`。本次未运行真实 Docker 或 DeepSeek，历史实测仍以各条原始记录为准。

### 2026-11-03 本地闭环交付增量

- `publication.py` 冻结候选、核对 D/C/代码/规范/原件、原子写入及事务登记；API 下载核对文件 hash/size 后返回已读取字节。覆盖篡改、轮次错绑、磁盘/登记失败重放、并发取消和旧 VALIDATED checkpoint 恢复。
- Web 支持显式启动、事实与完整逐列裁决、默认保留、日期格式与填充值、隐私确认、事件游标、attempt/模型用量、D 拒绝摘要及下载。任务 ID 可经 URL 恢复，令牌不落持久存储。
- worker 回报镜像/时间/固定哈希归属，D 拒绝保持安全摘要且不修复；新增 cgroup 总 CPU（包含子进程）检查。PID 满载无法启动收集器时失败关闭；正常有限后台进程经可信收集器停止并回收。
- 本地 `local_runtime` 提供 fail-stop 三进程监督、就绪、停止/专用回收、离线备份/原路径恢复、终态文件清理。上传容量在 DB 事务内控制活动任务数量，磁盘余量不足拒绝。控制服务与 worker 仍为单机串行、受控演示。
- 本轮真实日期曾因 `strftime("YYYY-MM-DD")` 输出标签而被 D 拒绝；较早测试 1 failed、2 passed，未发布。提示明确 `date.isoformat()`/`re.fullmatch` 后三例复测通过，最终新镜像再通过三例。失败代码/候选 hash 已永久记录，不用后续通过覆盖失败。
- 最终真实受控修复任务：`4ea9c2c5-ac9d-4ac5-91d5-0c51b1bbd3a9`，真实生成后另文件注入运行故障，C1 退出 1；真实模型修复，C2 和新 D 退出 0、成功发布。初次/修复输入输出 token 为 1093/1097、2179/1088；不是自然模型失败率证据。
- 浏览器成功任务：`c6d754b1-951c-4f4e-a8e5-6edb0caa90de`，模型 1094/814 token；390×844 下保留非法日期/空值/负数，D 1 改动、0 不匹配。输出 hash `ee35aa401f09beeb3c987a2e08fdee157507d041223062c31e6fcefe6eabf302`，产物已导出。pytest 断言通过但宿主工具权限告警未消除，不隐瞒为 clean pass。
- 新固定镜像：`sha256:b2682db251e6ecb1ae965c57b6a1d6b51a4bbfde3cd578ab4c66c6ea8d9e0061`。基础 digest 固定，作业依赖使用 `--require-hashes`；Windows 控制环境独立验收。操作见 `local_operations.md`。
- 末批任务临时证据在 `C:\Users\User\AppData\Local\Temp\pytest-of-User\pytest-41`（Docker/模型）和 `pytest-42`（浏览器），可能自动清理。永久报告与导出非敏感文件保留关键证据，不含密钥。本轮未 commit/push。
