"""Small control-plane task records and state types; raw CSV and generated code are not stored in state.


"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


# Schema version for migration and compatibility checks.
SCHEMA_VERSION = "1.0"
# Finding origin: fixed detection or future dynamic probing.
IssueSource = Literal["fixed", "future_dynamic"]


class TaskState(StrEnum):
    """Task lifecycle states."""
    UPLOADED = "UPLOADED"              # Uploaded.
    QUEUED = "QUEUED"                  # Queued.
    PROFILING = "PROFILING"            # Profiling.
    AWAITING_DECISIONS = "AWAITING_DECISIONS" # Waiting for user approval.
    SPEC_FROZEN = "SPEC_FROZEN"        # Cleaning specification frozen.
    GENERATING = "GENERATING"          # Generating cleaning source.
    EXECUTING = "EXECUTING"            # Executing in an isolated container.
    REPAIRING = "REPAIRING"            # Attempting code repair.
    VALIDATING = "VALIDATING"          # Independently verifying results.
    VALIDATED = "VALIDATED"            # Validated but not yet published; not final success.
    PUBLISHING = "PUBLISHING"          # Publishing final artifacts.
    SUCCEEDED = "SUCCEEDED"            # Task succeeded.
    FAILED = "FAILED"                  # Task failed.
    CANCELLED = "CANCELLED"            # Task cancelled.


@dataclass(frozen=True)
class TaskRecord:
    """Core task record."""
    task_id: str          # Unique task ID.
    owner_id: str         # Owner ID.
    goal: str             # Business goal.
    state: TaskState      # Current state.
    input_sha256: str     # SHA-256 of the input file.
    input_bytes: int      # Input file size in bytes.
    version: int          # Record version for optimistic locking or change tracking.
    created_at: str       # Creation time in ISO format.


@dataclass(frozen=True)
class TaskEvent:
    """Task event record for progress history."""
    sequence: int         # Event sequence number.
    task_id: str          # Associated task ID.
    kind: str             # Event type, usually a state transition.
    created_at: str       # Event time.
