"""Opaque job references and validation; clients and models must never set Docker parameters directly.


"""

from dataclasses import dataclass
from enum import StrEnum
import re
from uuid import UUID


class Stage(StrEnum):
    """Job execution stages."""
    PROFILE_A = "PROFILE_A" # Stage A: profiling with fixed tools.
    CLEAN_C = "CLEAN_C"     # Stage C: execute model-generated cleaning source.
    VERIFY_D = "VERIFY_D"   # Stage D: independently verify the result.


@dataclass(frozen=True)
class JobRequest:
    """Job request sent to the worker."""
    task_id: str          # Task ID.
    attempt_id: str       # Attempt ID, distinguishing repair retries.
    stage: Stage          # Execution stage.
    input_sha256: str     # Input file digest.
    spec_sha256: str | None = None      # Frozen specification digest, required for C and D.
    code_sha256: str | None = None      # Cleaning source digest, required for C.
    candidate_sha256: str | None = None # Candidate artifact digest, required for D.

    def __post_init__(self) -> None:
        """Validate request integrity and required fields."""
        UUID(self.task_id)
        UUID(self.attempt_id)
        if not isinstance(self.stage, Stage):
            raise ValueError("Unknown job stage")
        
        # Length alone does not prove a digest; reject non-hexadecimal references and invalid optional digests.
        if not re.fullmatch(r"[0-9a-f]{64}", self.input_sha256):
            raise ValueError("A valid input file SHA-256 hash is required")
        for digest in (self.spec_sha256, self.code_sha256, self.candidate_sha256):
            if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Job digests must be SHA-256 hashes")
            
        # Check stage-specific required parameters.
        if self.stage != Stage.PROFILE_A and not self.spec_sha256:
            raise ValueError("Stages C and D require a frozen specification hash")
            
        if self.stage == Stage.CLEAN_C and not self.code_sha256:
            raise ValueError("Stage C requires a recorded source code hash")
            
        if self.stage == Stage.VERIFY_D and not self.candidate_sha256:
            raise ValueError("Stage D must be bound to a specific candidate artifact hash")
