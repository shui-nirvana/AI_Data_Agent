"""SQLite task and event metadata storage for the API and independent orchestration store.


"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from backend.domain.models import TaskEvent, TaskRecord, TaskState


def _now() -> str:
    """Return the current UTC time in ISO format."""
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    """Manage task database operations."""
    def __init__(self, path: Path) -> None:
        """Initialize the SQLite database and ensure its schema exists.




        """
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                -- 任务表：存储任务基本信息
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    state TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    input_bytes INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                -- 事件表：存储任务状态变更记录
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _connect(self):
        """Commit or roll back and close each connection; do not hold one during approval."""
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row # Enable column-name access to rows.
        db.execute("PRAGMA foreign_keys=ON") # Enable foreign key constraints.
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(
        self, task_id: str, owner_id: str, goal: str, digest: str, size: int,
        max_active: int | None = None,
    ) -> TaskRecord:
        """Create a task and initial event with its owner, goal, input digest, and byte size.








        """
        created_at = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if max_active is not None and db.execute(
                "SELECT count(*) FROM tasks WHERE state NOT IN ('SUCCEEDED','FAILED','CANCELLED')"
            ).fetchone()[0] >= max_active:
                raise OverflowError("TASK_CAPACITY")
            db.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, owner_id, goal, TaskState.UPLOADED, digest, size, 1, created_at),
            )
            db.execute(
                "INSERT INTO events(task_id, kind, created_at) VALUES (?, ?, ?)",
                (task_id, TaskState.UPLOADED, created_at),
            )
        return self.get(task_id, owner_id)

    def get(self, task_id: str, owner_id: str) -> TaskRecord | None:
        """Get a task by ID and owner.

        """
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE task_id=? AND owner_id=?",
                (task_id, owner_id),
            ).fetchone()
        if row is None:
            return None
        return TaskRecord(
            task_id=row["task_id"],
            owner_id=row["owner_id"],
            goal=row["goal"],
            state=TaskState(row["state"]),
            input_sha256=row["input_sha256"],
            input_bytes=row["input_bytes"],
            version=row["version"],
            created_at=row["created_at"],
        )

    def events(self, task_id: str, after: int) -> list[TaskEvent]:
        """Get task events after the exclusive sequence number.





        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE task_id=? AND sequence>? ORDER BY sequence LIMIT 100",
                (task_id, after),
            ).fetchall()
        return [
            TaskEvent(row["sequence"], row["task_id"], row["kind"], row["created_at"])
            for row in rows
        ]

    def cancel(self, task_id: str, owner_id: str) -> TaskRecord | None:
        """Cancel the task unless it has already reached a terminal state.

        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM tasks WHERE task_id=? AND owner_id=?",
                (task_id, owner_id),
            ).fetchone()
            if row is None:
                return None
            # Only non-terminal tasks may be cancelled.
            if row["state"] not in (TaskState.CANCELLED, TaskState.SUCCEEDED, TaskState.FAILED):
                db.execute(
                    "UPDATE tasks SET state=?, version=version+1 WHERE task_id=?",
                    (TaskState.CANCELLED, task_id),
                )
                db.execute(
                    "INSERT INTO events(task_id, kind, created_at) VALUES (?, ?, ?)",
                    (task_id, TaskState.CANCELLED, _now()),
                )
        return self.get(task_id, owner_id)
