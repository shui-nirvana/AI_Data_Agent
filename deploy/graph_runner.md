# 单机 Graph Runner 运行说明

## 三个独立进程

FastAPI 只上传/查询/审批；Graph Runner 消费 outbox、驱动 LangGraph；Docker worker 消费固定作业、收集产物/回报。同一私有目录包含业务 DB、checkpoint 和任务文件，当前只支持本机可信文件系统，不是多机共享存储协议。API/Runner 不应获得 Docker socket 权限。

安装与构建（已有匹配版本镜像可以跳过构建）：

```powershell
python -m pip install -e ".[worker,workflow,test]"
python -m workers.sandbox_manager.build_images --target local --platform linux/amd64
```

三个终端分别运行；每个进程使用同一绝对目录或从同一项目根目录启动：

```powershell
# 终端 1：API 不需要 Docker 环境配置
$env:AI_AGENT_DEMO_TOKEN = "your-private-local-token"
python -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8000
```

```powershell
# 终端 2：Graph Runner 不加载 Docker SDK，不直接调用 CSV 解析器
python -m backend.orchestration.runner --data-dir .runtime
```

```powershell
# 终端 3：只有 worker 使用 Docker
# Windows 上 SDK 不自动跟随 CLI context，必要时显式指定 Linux 引擎：
$env:DOCKER_HOST = "npipe:////./pipe/dockerDesktopLinuxEngine"
python -m workers.sandbox_manager.queue_worker --data-dir .runtime --images .runtime/docker/images.json
```

两个后台命令都支持 `--once`，只处理一条事件/作业后退出，便于逐步测试。后台无工作时短轮询；审批期间没有挂起 HTTP、占用的容器或保持打开的 DB 事务。API `/health` 的 `execution_enabled=false` 表示 API 本身不执行代码，不是后台 Runner 的健康信号。

## PowerShell 全部保留示例

在第四个终端运行无敏感金样本。此路径不需要模型，A 发现项全部选择 preserve，经过真实 D 确认字节未变：

```powershell
$headers = @{ "X-Demo-Token" = "your-private-local-token"; "X-Task-Goal" = "Preserve all values" }
$csv = [System.Text.Encoding]::UTF8.GetBytes("id,date,amount`n1,2026/10/31,-3`n")
$task = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/tasks" -Headers $headers -ContentType "text/csv" -Body $csv
$id = $task.task_id
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/tasks/$id/start" -Headers $headers
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/tasks/$id" -Headers $headers
```

查询显示 `AWAITING_DECISIONS` 后，再执行：

```powershell
$review = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/tasks/$id/review" -Headers $headers
$choices = @($review.issues | ForEach-Object {
    @{ issue_id = $_.issue_id; column = $_.column; action = "preserve"; predicate_id = "all_cells"; params = @{} }
})
$approval = @{ task_version = $review.task_version; decisions = $choices } | ConvertTo-Json -Depth 10
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/tasks/$id/decisions" -Headers $headers -ContentType "application/json" -Body $approval
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/tasks/$id" -Headers $headers
```

最终状态应为 `VALIDATED`，表示独立 D 通过，但发布/下载还未实现。服务端绑定 actor，客户端不能提交 actor_id、原始发现集、镜像、命令或挂载路径。请求最大 256 KiB，必须完整裁决本任务的发现集合；过期版本/取消/修改已冻结规范返回 409，非法参数返回 422。相同原始版本和相同决策的重复提交返回同一规范哈希。

## 代码生成分支

`GraphRunner(root, generator=...)` 接收提供方无关的 `CodeGenerator` 业务端口。生成器返回 `CodeVersion`，源码必须位于本任务私有根目录内并与 code/spec 哈希匹配；Runner 不执行它，只投递 C。每个候选都在新 D 容器验证。

CLI 默认 `UnconfiguredGenerator`。在 Runner 终端设置 `AI_AGENT_LLM_PROVIDER=deepseek`、`AI_AGENT_LLM_ALLOW_EXTERNAL=true` 和 `DEEPSEEK_API_KEY` 后，启动模块注入真实 DeepSeek 客户端和通用代码生成器；API/worker 保持无模型权限。完整启动和自愈说明见 `deepseek.md`。没有配置时获准修改明确失败，不回退本机执行或模板。

真实集成回归：

```powershell
$env:DOCKER_HOST = "npipe:////./pipe/dockerDesktopLinuxEngine"
$env:AI_AGENT_RUN_DOCKER_TESTS = "1"
python -m pytest tests/test_workflow_docker.py -q
```

测试清洗分支注入人工测试源码生成器，并由独立子进程 worker 真正运行 C；这是执行/编排证据，不是 LLM 生成证据。另一分支测试全部保留 A/D；测试目录在 pytest 临时目录内，不复用真实任务。

## 持久化与失败策略

- `tasks.sqlite3`：任务/事件、workflow、审批快照、作业和 outbox。
- `checkpoints.sqlite3`：LangGraph 游标，稳定 `thread_id=task_id`，只保存小型引用和哈希。
- `workflows/<task_id>/`：受信画像/发现 JSON、规范文件；`worker_jobs/<attempt_id>/`：只读快照、执行记录、私有候选/诊断与截断日志。
- OS 文件锁串行化本机 Runner 和 worker。A 一轮，C 最多三轮，每个候选新 D；同阶段/同轮次唯一 attempt。重复节点查旧作业/代码，不再次运行旧 C。模型调用前记 started，失败/不确定调用不自动重发。
- 审批和 outbox 同业务事务，checkpoint 提交后才 ack 事件；重复事件可重放。取消后的回报不能更新成功状态。
- worker 正常运行时观察 DB 取消并请求终止容器。worker 异常退出后，容器可能留存到 worker 重启；重启先按已记录 attempt 标签强制回收，再把旧 running 作业置为失败，不自动重跑。
- 若回收失败，worker 停止消费，作业保留 running 供再次对账；不能把孤儿容器当作已清理。需要进程监督/告警和人工检查，尚无独立清扫守护进程。
- 节点错误以异常类型的固定码记录，不外发字段/原始错误内容；修复只发上一版模型源码和枚举故障。Runner 默认禁用 LangSmith tracing。SQLite 连接在每次短事务后关闭。

仍需在目标 VM 验证进程强杀、真实并发取消、PID/总 CPU、恶意子进程、Docker API 阻塞及多任务情况；当前不能用于敏感数据或公网任意代码执行。
