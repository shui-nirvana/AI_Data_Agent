"""Worker policy and scheduling; never fall back to unsandboxed execution.


"""

from backend.jobs.contracts import JobRequest, Stage
from backend.jobs.execution import (
    DisabledExecutionBackend, ExecutionBackend, ExecutionReceipt, ExecutionStatus,
)
from backend.jobs.policy import LIMITS, SandboxLimits


class SandboxManager:
    """Scheduling facade independent of Vultr and AWS; only trusted startup code injects adapters."""

    def __init__(self, backend: ExecutionBackend | None = None) -> None:
        self.backend = backend if backend is not None else DisabledExecutionBackend()

    def submit(self, job: JobRequest) -> ExecutionReceipt:
        """Submit a job request for sandbox execution.




        """
        if job.stage not in LIMITS:
            raise ValueError("未知的作业阶段")
            
        receipt = self.backend.submit(job, LIMITS[job.stage])
        if (receipt.task_id, receipt.attempt_id) != (job.task_id, job.attempt_id):
            raise ValueError("执行适配器回执与任务不匹配")
        return receipt

    def status(self, receipt: ExecutionReceipt) -> ExecutionStatus:
        result = self.backend.status(receipt)
        if result.receipt != receipt:
            raise ValueError("执行适配器返回了其他作业的状态")
        return result

    def cancel(self, receipt: ExecutionReceipt) -> None:
        self.backend.cancel(receipt)
