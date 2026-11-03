"""
SQLite 任务元数据存储，供 API 与独立编排存储使用。
该模块负责管理任务和事件的持久化，使用 SQLite 作为后端存储。
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from backend.domain.models import TaskEvent, TaskRecord, TaskState


def _now() -> str:
    """返回当前 UTC 时间的 ISO 格式字符串"""
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    """任务存储管理类，封装了所有数据库操作"""
    def __init__(self, path: Path) -> None:
        """
        初始化存储并确保表结构存在。
        
        参数:
            path: SQLite 数据库文件路径。
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
        """事务结束时提交/回滚并关闭连接，不在审批等待期间持有连接。"""
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row # 支持通过列名访问结果
        db.execute("PRAGMA foreign_keys=ON") # 启用外键约束
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(
        self, task_id: str, owner_id: str, goal: str, digest: str, size: int,
        max_active: int | None = None,
    ) -> TaskRecord:
        """
        在数据库中创建新任务和初始事件。
        
        参数:
            task_id: 唯一任务 ID。
            owner_id: 所有者 ID。
            goal: 任务目标。
            digest: 文件 SHA-256 哈希。
            size: 文件大小（字节）。
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
        """
        根据 ID 和所有者获取任务详情。
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
        """
        获取指定序号之后的任务事件。
        
        参数:
            task_id: 任务 ID。
            after: 起始序号（不包含该序号）。
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
        """
        如果任务尚未完成或失败，则将其标记为已取消。
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM tasks WHERE task_id=? AND owner_id=?",
                (task_id, owner_id),
            ).fetchone()
            if row is None:
                return None
            # 仅允许取消非终态的任务
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
