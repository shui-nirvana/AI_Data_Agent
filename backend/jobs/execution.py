"""应用到受信调度服务的端口，不包含云 SDK、宿主路径或容器命令。"""

from dataclasses import dataclass
from typing import Literal, Protocol

from backend.jobs.contracts import JobRequest
from backend.jobs.policy import SandboxLimits


@dataclass(frozen=True)
class ExecutionReceipt:
    """调度回执不是成功证据；handle 只是适配器管理的不透明引用。"""

    task_id: str
    attempt_id: str
    handle: str


@dataclass(frozen=True)
class ExecutionStatus:
    receipt: ExecutionReceipt
    state: Literal["queued", "running", "finished", "failed", "cancelled"]
    exit_code: int | None = None
    timed_out: bool = False
    limit_reason: str | None = None
    result_ref: str | None = None


class ExecutionBackend(Protocol):
    """本机受信调度/RPC/远程 worker 都实现此端口，应用不直接握有 socket。

    实现必须幂等投递、固定镜像与入口、落实全部隔离限额、验证归属并回收。
    Protocol 只约定 API，不证明安全能力；每个平台仍需独立隔离验收。
    """

    def submit(self, job: JobRequest, limits: SandboxLimits) -> ExecutionReceipt: ...
    def status(self, receipt: ExecutionReceipt) -> ExecutionStatus: ...
    def cancel(self, receipt: ExecutionReceipt) -> None: ...


class DisabledExecutionBackend:
    """缺适配器时明确失败，不回退到控制层 subprocess 或本机 CSV 解析。"""

    def submit(self, job: JobRequest, limits: SandboxLimits) -> ExecutionReceipt:
        raise NotImplementedError("尚未接入并验收容器运行时适配器")

    def status(self, receipt: ExecutionReceipt) -> ExecutionStatus:
        raise NotImplementedError("执行适配器未配置")

    def cancel(self, receipt: ExecutionReceipt) -> None:
        raise NotImplementedError("执行适配器未配置")
