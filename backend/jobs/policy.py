"""Cloud-independent execution safety requirements enforced by the worker runtime adapter."""

from dataclasses import dataclass
from types import MappingProxyType

from backend.jobs.contracts import Stage


@dataclass(frozen=True)
class SandboxLimits:
    """Fixed resource limits are application policy, not model or browser inputs."""

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
