# DeepSeek 接入与有限自愈

## 本地密钥文件

项目根目录的 `.env.deepseek` 是本机私有文件，将实际密钥填在等号后：

```dotenv
DEEPSEEK_API_KEY=你的密钥
```

不要将密钥发到聊天或提交 Git。文件已被 `.gitignore` 和 `.dockerignore` 排除，开发源码包也不包含它；这不等于加密，仍需限制本机文件访问，避免同步到共享目录或备份中。

Runner 启动 DeepSeek 时只读取这一项，不自动加载其他 `.env` 配置，也不会将密钥写入环境变量。若进程已设置 `DEEPSEEK_API_KEY`，环境变量优先；即使它为空也会报错，不会静默切换到文件。文件修改后需要重启 Runner。

## 启用模型

仅在启动 **Graph Runner** 的终端设置以下非密钥配置：

```powershell
python -m pip install -e ".[llm,workflow,worker,test]"
$env:AI_AGENT_LLM_PROVIDER = "deepseek"
$env:AI_AGENT_LLM_MODEL = "deepseek-flash"
$env:AI_AGENT_LLM_ALLOW_EXTERNAL = "true"
# 密钥从项目根目录 .env.deepseek 读取；已有环境变量时优先使用环境变量。
python -m backend.orchestration.runner --data-dir .runtime
```

`DeepSeekClient.from_environment()` 优先读取环境变量，否则只读取项目根目录的 `.env.deepseek`。缺失/空值会在启动时明确报错；不会搜索其他文件或切换模型。`.env.example` 只作示例，普通 `.env` 不自动加载。

Windows `$env:` 仅对当前终端及其子进程生效，其他已打开终端不会自动继承。用系统设置或 `setx` 设置后需新开终端；可用以下命令只检查是否存在：

```powershell
[bool]$env:DEEPSEEK_API_KEY
```

API 终端保持 `AI_AGENT_LLM_PROVIDER=disabled`，不加载有密钥的 DeepSeek 客户端；worker 不需要密钥，清洗容器只注入固定作业标志。不要把密钥复制到 API 请求、任务规范、源码、checkpoint、镜像或开发包。

当前固定请求地址 `https://api.deepseek.com/chat/completions`。依据 2026-11-02 官方快速开始，默认 `deepseek-flash`；支持 `deepseek-v4-flash` 和 `deepseek-v4-pro`（后者费用可能更高），不自动切换。非思考模式、非流式生成；HTTP 传输内部有界读取，不跟随重定向/代理环境、无隐式重试。每次输出上限 4096 tokens、总时限 90 秒。模型别名可能漂移，审计保存实际返回 model。

## 模型看到什么

只给系统固定读写协议、批准动作、匿名列索引、聚合计数和格式信息。不提供原始 CSV、行级样例、真实列名、用户身份、目标自由文本、宿主路径、真实填充值。匿名摘要的 `approved_rules` 不是容器文件结构；提示词另外提供无真实数据的 `spec.json` 结构示例，要求 C 在运行时从只读规范的 `columns` 和 `decisions` 读取真实列名、动作和参数。扫描统计只提供背景，不能授权额外操作。

`AI_AGENT_LLM_ALLOW_EXTERNAL=true` 是受控单用户演示的操作员授权；页面另要求审批/隐私确认，但不等于生产多用户同意或合规保证。匿名计数仍是外发信息；只使用无敏感金样本，不公开服务。修复只发送上一版模型源码和枚举故障码，不外发原始 stdout/stderr、异常消息和 CSV 值。

模型返回完整 JSON：

```json
{
  "schema_version": "1.0",
  "spec_sha256": "冻结规范的哈希",
  "source": "模型为本任务编写的完整 Python 源码"
}
```

控制层检查 JSON、规范绑定、32 KiB 源码大小、AST 语法和明显禁用能力，不执行/导入源码。AST 是保守协议检查，不是安全证明；实际代码必须在新 C 沙箱运行并经过新 D。网络、进程、环境访问、动态 eval/exec、未登记依赖等明显请求停止自动修复。

## 自愈策略

- C 首次 + 至多 2 次修复，最多 3 个不同 attempt/新容器/新源码版本。每个有效候选对应新 D。
- 仅固定 C 入口明确报告的实现运行错误、正常退出但缺 `cleaned.csv` 可进入修复；语法/JSON 协议故障可在模型调用预算内重新生成。
- 任务最多 **3 次模型调用**，包括格式重生成和执行修复，因此格式失败可能耗尽后续修复预算。每次最多 4096 输出 tokens，单任务最多 12288 输出预算；输入和供应商计费另计。认证/限流/超时/截断/拒绝不会无声重试。
- 超时/OOM、安全违规、权限/输入错误、收集/写盘/回收错误、失联 worker、未绑定的回报默认停止。D 拒绝绝不进入修复，也不修改已批准规范。
- C 的审计钩子额外记录明显网络/进程/文件违规，即使脚本吞掉异常也按违规处理。钩子和错误类别不能视为对任意恶意 Python 的认证证明，容器共享内核且还需完整攻击验收。
- 固定收集器保留有界诊断；原始日志只存本地，不交给模型。worker 回报安全类别/退出码/哈希，Graph 再检查归属和预算。
- C3 再失败则 FAILED；D 通过后进入 `VALIDATED -> PUBLISHING -> SUCCEEDED`。只有发布器重新核对哈希并登记的字节可以鉴权下载；D 拒绝报告以安全摘要展示，不进入修复。

## 持久化与重启

`workflows/<task_id>/llm/` 包含内容绑定的源码和私有 context 文件。`calls.sqlite3` 在调用前保存 started，完成后保存源码版本、请求 ID、提供方/实际模型、提示词哈希、用量；不保存密钥/请求头/原始供应商报错。网络调用期间 DB 连接关闭。

调用已完成时重放返回旧源码；调用期间或收到响应到持久化之间崩溃时，started/failed 记录阻断自动重发，任务明确失败，避免猜测外部幂等/重复费用。当前不提供不确定调用的自动恢复，需检查后新建任务。作业表已自动迁移为 `(task_id, stage, attempt_no)` 唯一约束，旧任务/attempt 保留。`workflow_codes` 保存每轮源码，不仅覆盖最后一版。

## 测试

无外发回归：

```powershell
python -m pytest -q
```

真实 Docker + 模拟模型响应的自愈、缺输出、安全/D 拒绝测试：

```powershell
python -m workers.sandbox_manager.build_images --target local --platform linux/amd64
$env:DOCKER_HOST = "npipe:////./pipe/dockerDesktopLinuxEngine"
$env:AI_AGENT_RUN_DOCKER_TESTS = "1"
python -m pytest tests/test_llm_docker.py -q
```

真实 DeepSeek + 新 C/D 三组金样本（会外发匿名信息并消耗 API 费用）：

```powershell
$env:DOCKER_HOST = "npipe:////./pipe/dockerDesktopLinuxEngine"
$env:AI_AGENT_RUN_DOCKER_TESTS = "1"
$env:AI_AGENT_RUN_LIVE_LLM_TESTS = "1"
$env:AI_AGENT_LLM_ALLOW_EXTERNAL = "true"
# live 测试从环境变量或 .env.deepseek 读取密钥，环境变量优先。
# 本测试自行注入客户端，API 使用 disabled，因此不要在该测试进程设置 provider=deepseek。
$env:AI_AGENT_LLM_PROVIDER = "disabled"
python -m pytest tests/test_llm_docker.py -q -k live
```

三组分别是日期规范化、明确填空，以及真实生成后人工注入运行错误、由真实模型修复再经新 C/D 发布。注入版本标记 `test_fault_injected_after_llm`，不能称为自然模型失败；自然 D 拒绝不自动修复。提示词明确 `YYYY-MM-DD` 是协议标签而不是 `strftime` 参数，合法日期用 `date.isoformat()`，完整匹配用 `re.fullmatch`。实测含先前日期拒绝与后续通过记录，见 `implementation_status.md`。

官方资料：[快速开始](https://api-docs.deepseek.com/)、[Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion)。
