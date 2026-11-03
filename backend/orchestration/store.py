"""单机 SQLite 编排存储。事务只存元数据，不解析 CSV、不持有运行时权限。"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from backend.domain.models import TaskState
from backend.domain.spec import Decision, freeze_spec
from backend.domain.store import TaskStore, _now
from backend.jobs.contracts import JobRequest

TERMINAL = (TaskState.CANCELLED, TaskState.FAILED, TaskState.SUCCEEDED)


class WorkflowConflict(ValueError):
    """过期审批、任务取消或不允许的状态迁移。"""


class WorkflowStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.root.chmod(0o700)
        self.tasks = TaskStore(self.root / "tasks.sqlite3")
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workflows (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
                    profile_json TEXT, issues_json TEXT, spec_json TEXT, code_json TEXT,
                    validation_json TEXT
                );
                CREATE TABLE IF NOT EXISTS workflow_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedup_key TEXT UNIQUE NOT NULL,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    handled INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS workflow_jobs (
                    job_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    stage TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_dir TEXT,
                    error_code TEXT,
                    UNIQUE(task_id, stage)
                );
            """)
            # executescript 会结束入口事务，显式重新加锁以原子执行 DDL 迁移。
            db.execute("BEGIN IMMEDIATE")
            # 从旧的每阶段唯一作业迁移为每阶段/轮次唯一，保留旧 attempt 与结果。
            columns = {row["name"] for row in db.execute("PRAGMA table_info(workflow_jobs)")}
            if "attempt_no" not in columns:
                db.execute("ALTER TABLE workflow_jobs RENAME TO workflow_jobs_v1")
                db.execute("""CREATE TABLE workflow_jobs(
                    job_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    stage TEXT NOT NULL, request_json TEXT NOT NULL, sources_json TEXT NOT NULL,
                    state TEXT NOT NULL, result_dir TEXT, error_code TEXT,
                    attempt_no INTEGER NOT NULL CHECK(attempt_no BETWEEN 1 AND 3),
                    evidence_json TEXT, UNIQUE(task_id,stage,attempt_no))""")
                db.execute("""INSERT INTO workflow_jobs
                    SELECT *,1,NULL FROM workflow_jobs_v1""")
                db.execute("DROP TABLE workflow_jobs_v1")
            db.execute("""CREATE TABLE IF NOT EXISTS workflow_codes(
                task_id TEXT NOT NULL REFERENCES tasks(task_id), attempt_no INTEGER NOT NULL,
                code_json TEXT NOT NULL, PRIMARY KEY(task_id,attempt_no))""")
            db.execute("""CREATE TABLE IF NOT EXISTS workflow_artifacts(
                task_id TEXT NOT NULL REFERENCES tasks(task_id), artifact_id TEXT NOT NULL,
                sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, media_type TEXT NOT NULL,
                PRIMARY KEY(task_id,artifact_id))""")

    @contextmanager
    def db(self):
        with self.tasks._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def wake(db, task_id: str, key: str):
        db.execute("INSERT OR IGNORE INTO workflow_outbox(dedup_key, task_id) VALUES (?, ?)",
                   (key, task_id))

    @staticmethod
    def transition(db, task_id: str, state: TaskState):
        row = db.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None or (row["state"] in TERMINAL and row["state"] != state):
            raise WorkflowConflict("任务不存在或已结束")
        if row["state"] != state:
            db.execute("UPDATE tasks SET state=?, version=version+1 WHERE task_id=?",
                       (state, task_id))
            db.execute("INSERT INTO events(task_id,kind,created_at) VALUES(?,?,?)",
                       (task_id, state, _now()))

    def start(self, task_id: str, owner: str):
        with self.db() as db:
            task = db.execute("SELECT * FROM tasks WHERE task_id=? AND owner_id=?",
                              (task_id, owner)).fetchone()
            if task is None:
                raise WorkflowConflict("任务未找到")
            flow = db.execute("SELECT task_id FROM workflows WHERE task_id=?", (task_id,)).fetchone()
            if flow:
                if task["state"] in TERMINAL:
                    raise WorkflowConflict("任务已结束")
                return
            if task["state"] != TaskState.UPLOADED:
                raise WorkflowConflict("仅能启动已上传任务")
            db.execute("INSERT INTO workflows(task_id) VALUES (?)", (task_id,))
            self.transition(db, task_id, TaskState.QUEUED)
            self.wake(db, task_id, f"start:{task_id}")

    def flow(self, task_id: str):
        with self.db() as db:
            row = db.execute("SELECT * FROM workflows WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise WorkflowConflict("任务尚未启动编排")
        return dict(row)

    def task(self, task_id: str):
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise WorkflowConflict("任务不存在")
        return dict(row)

    def check_active(self, task_id: str):
        if self.task(task_id)["state"] in TERMINAL:
            raise WorkflowConflict("任务已结束")

    def next_event(self):
        with self.db() as db:
            row = db.execute("SELECT * FROM workflow_outbox WHERE handled=0 ORDER BY id LIMIT 1").fetchone()
        return dict(row) if row else None

    def ack(self, event_id: int):
        with self.db() as db:
            db.execute("UPDATE workflow_outbox SET handled=1 WHERE id=?", (event_id,))

    def schedule(self, job: JobRequest, sources: dict[str, str], state: TaskState, attempt_no: int = 1):
        """同轮次节点重放不重复投递；C/D 最多三轮，A 只能一轮。"""
        if attempt_no not in (1, 2, 3) or (job.stage == "PROFILE_A" and attempt_no != 1):
            raise WorkflowConflict("作业尝试超出预算")
        with self.db() as db:
            existing = db.execute("SELECT * FROM workflow_jobs WHERE task_id=? AND stage=? AND attempt_no=?",
                                  (job.task_id, job.stage, attempt_no)).fetchone()
            if existing:
                return dict(existing)
            self.transition(db, job.task_id, state)
            from dataclasses import asdict
            db.execute("""INSERT INTO workflow_jobs
                       (job_id,task_id,stage,request_json,sources_json,state,attempt_no)
                       VALUES(?,?,?,?,?,?,?)""",
                       (job.attempt_id, job.task_id, job.stage,
                        json.dumps(asdict(job)), json.dumps(sources), "queued", attempt_no))
            row = db.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (job.attempt_id,)).fetchone()
        return dict(row)

    def job(self, job_id: str):
        with self.db() as db:
            row = db.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def claim_job(self):
        with self.db() as db:
            # 取消任务的排队作业不再执行。
            db.execute("""UPDATE workflow_jobs SET state='cancelled'
                          WHERE state='queued' AND task_id IN
                          (SELECT task_id FROM tasks WHERE state IN ('CANCELLED','FAILED'))""")
            row = db.execute("SELECT * FROM workflow_jobs WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE workflow_jobs SET state='running' WHERE job_id=?", (row["job_id"],))
        return dict(row)

    def finish_job(self, job_id: str, state: str, result_dir: str | None, error_code: str | None,
                   evidence: dict | None = None):
        if state not in ("finished", "failed", "cancelled"):
            raise ValueError("无效作业状态")
        with self.db() as db:
            job = db.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None or job["state"] != "running":
                raise WorkflowConflict("作业已结束或回报过期")
            task = db.execute("SELECT state FROM tasks WHERE task_id=?", (job["task_id"],)).fetchone()
            if task["state"] in TERMINAL:
                state, result_dir = "cancelled", None
            db.execute("UPDATE workflow_jobs SET state=?,result_dir=?,error_code=?,evidence_json=? WHERE job_id=?",
                       (state, result_dir, error_code, json.dumps(evidence) if evidence else None, job_id))
            self.wake(db, job["task_id"], f"job:{job_id}")

    def accept_profile(self, task_id: str, profile: dict, issues: list):
        with self.db() as db:
            self.transition(db, task_id, TaskState.AWAITING_DECISIONS)
            db.execute("UPDATE workflows SET profile_json=?,issues_json=? WHERE task_id=?",
                       (json.dumps(profile), json.dumps(issues), task_id))

    def approve(self, task_id: str, owner: str, version: int, decisions: tuple[Decision, ...]):
        """审批与恢复 outbox 同事务；重复相同提交幂等，不重新冻结或执行 C。"""
        with self.db() as db:
            task = db.execute("SELECT * FROM tasks WHERE task_id=? AND owner_id=?",
                              (task_id, owner)).fetchone()
            flow = db.execute("SELECT * FROM workflows WHERE task_id=?", (task_id,)).fetchone()
            if task is None or flow is None or not flow["profile_json"]:
                raise WorkflowConflict("任务尚未完成 A 检测")
            profile, issues = json.loads(flow["profile_json"]), json.loads(flow["issues_json"])
            spec = freeze_spec(task_id=task_id, input_sha256=task["input_sha256"], owner_id=owner,
                               task_version=version, columns=tuple(profile["columns"]),
                               decisions=decisions, issues=tuple(issues))
            if task["state"] == TaskState.CANCELLED:
                raise WorkflowConflict("任务已取消")
            if flow["spec_json"]:
                from backend.domain.spec import FrozenSpec
                previous = FrozenSpec.model_validate_json(flow["spec_json"])
                if previous != spec:
                    raise WorkflowConflict("规范已经冻结，不允许修改")
                return previous
            if task["state"] != TaskState.AWAITING_DECISIONS or task["version"] != version:
                raise WorkflowConflict("审批状态或版本过期")
            db.execute("UPDATE workflows SET spec_json=? WHERE task_id=?",
                       (spec.model_dump_json(), task_id))
            self.transition(db, task_id, TaskState.SPEC_FROZEN)
            self.wake(db, task_id, f"approval:{task_id}")
        return spec

    def save_code(self, task_id: str, code: dict, attempt_no: int = 1):
        with self.db() as db:
            self.transition(db, task_id, TaskState.GENERATING if attempt_no == 1 else TaskState.REPAIRING)
            db.execute("INSERT OR IGNORE INTO workflow_codes VALUES(?,?,?)",
                       (task_id, attempt_no, json.dumps(code)))
            db.execute("UPDATE workflows SET code_json=? WHERE task_id=?", (json.dumps(code), task_id))

    def code(self, task_id: str, attempt_no: int):
        with self.db() as db:
            row = db.execute("SELECT code_json FROM workflow_codes WHERE task_id=? AND attempt_no=?",
                             (task_id, attempt_no)).fetchone()
            # 旧 checkpoint/任务的首版代码继续使用，不重新调用模型。
            if row is None and attempt_no == 1:
                row = db.execute("SELECT code_json FROM workflows WHERE task_id=?", (task_id,)).fetchone()
        return json.loads(row["code_json"]) if row and row["code_json"] else None

    def repairing(self, task_id: str):
        with self.db() as db:
            self.transition(db, task_id, TaskState.REPAIRING)

    def generating(self, task_id: str):
        with self.db() as db:
            self.transition(db, task_id, TaskState.GENERATING)

    def validated(self, task_id: str, report: dict):
        with self.db() as db:
            self.transition(db, task_id, TaskState.VALIDATED)
            db.execute("UPDATE workflows SET validation_json=? WHERE task_id=?",
                       (json.dumps(report), task_id))

    def fail(self, task_id: str, code: str):
        with self.db() as db:
            task = db.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task and task["state"] not in TERMINAL:
                self.transition(db, task_id, TaskState.FAILED)
                db.execute("INSERT INTO events(task_id,kind,created_at) VALUES(?,?,?)",
                           (task_id, f"ERROR:{code}", _now()))


@contextmanager
def process_lock(root: Path, name: str):
    """本地 OS 文件锁：进程崩溃自动释放；不以到期租约并发运行同一个图。"""
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{name}.lock").open("a+b") as stream:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if __import__("os").name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def new_job(task_id: str, stage, digest: str, **hashes) -> JobRequest:
    return JobRequest(str(UUID(task_id)), str(uuid4()), stage, digest, **hashes)
