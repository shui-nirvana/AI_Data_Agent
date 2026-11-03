"""云平台无关的执行安全要求；由 worker 的具体运行时适配器落实。"""

from dataclasses import dataclass
from types import MappingProxyType

from backend.jobs.contracts import Stage


@dataclass(frozen=True)
class SandboxLimits:
    """固定资源配额是应用策略，不能由模型或浏览器提交。"""

    memory_mb: int
    cpu_seconds: int
    pids: int
    output_bytes: int
    timeout_seconds: int


LIMITS = MappingProxyType({
    Stage.PROFILE_A: SandboxLimits(256, 30, 32, 1_000_000, 45),
    Stage.CLEAN_C: SandboxLimits(512, 45, 32, 12_000_000, 60),
    Stage.VERIFY_D: SandboxLimits(256, 30, 32, 1_000_000, 45),
})
