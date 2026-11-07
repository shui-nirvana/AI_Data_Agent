"""Application port for trusted scheduling, without cloud SDKs, host paths, or container commands."""

from dataclasses import dataclass
from typing import Literal, Protocol

from backend.jobs.contracts import JobRequest
from backend.jobs.policy import SandboxLimits


@dataclass(frozen=True)
class ExecutionReceipt:
    """A receipt is not proof of success; the handle is an opaque adapter-managed reference."""

    task_id: str
    attempt_id: str
    handle: str


@dataclass(frozen=True)
class ExecutedSource:
    """Reference and digest of the failed attempt's executed source snapshot, not necessarily the original model output."""

    source_ref: str
    sha256: str
    spec_ref: str
    profile_ref: str


@dataclass(frozen=True)
class ExecutionStatus:
    receipt: ExecutionReceipt
    state: Literal["queued", "running", "finished", "failed", "cancelled"]
    exit_code: int | None = None
    timed_out: bool = False
    limit_reason: str | None = None
    result_ref: str | None = None


class ExecutionBackend(Protocol):
    """Trusted local schedulers, RPC, and remote workers implement this port. Implementations must enforce idempotence, fixed images and entrypoints, isolation, ownership, and cleanup; the protocol alone does not prove security.



    """

    def submit(self, job: JobRequest, limits: SandboxLimits) -> ExecutionReceipt: ...
    def status(self, receipt: ExecutionReceipt) -> ExecutionStatus: ...
    def cancel(self, receipt: ExecutionReceipt) -> None: ...


class DisabledExecutionBackend:
    """Fail explicitly without an adapter; never fall back to control-plane subprocesses or local CSV parsing."""

    def submit(self, job: JobRequest, limits: SandboxLimits) -> ExecutionReceipt:
        raise NotImplementedError("No container runtime adapter has been configured and validated")

    def status(self, receipt: ExecutionReceipt) -> ExecutionStatus:
        raise NotImplementedError("Execution adapter is not configured")

    def cancel(self, receipt: ExecutionReceipt) -> None:
        raise NotImplementedError("Execution adapter is not configured")
