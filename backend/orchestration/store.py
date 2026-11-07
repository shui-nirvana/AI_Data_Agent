"""Single-host SQLite orchestration store. Transactions store metadata only, without CSV parsing or runtime privileges."""

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
    """Stale approval, task cancellation, or an invalid state transition."""


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
            # executescript ends the initial transaction; reacquire the lock for atomic DDL migration.
            db.execute("BEGIN IMMEDIATE")
            # Migrate stage uniqueness to stage/round uniqueness while preserving old attempts and results.
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
            db.execute("""CREATE TABLE IF NOT EXISTS validation_explanations(
                task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
                status TEXT NOT NULL, facts_json TEXT NOT NULL,
                result_json TEXT, error_code TEXT)""")

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
            raise WorkflowConflict("Task does not exist or has already ended")
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
                raise WorkflowConflict("Task not found")
            flow = db.execute("SELECT task_id FROM workflows WHERE task_id=?", (task_id,)).fetchone()
            if flow:
                if task["state"] in TERMINAL:
                    raise WorkflowConflict("Task has already ended")
                return
            if task["state"] != TaskState.UPLOADED:
                raise WorkflowConflict("Only uploaded tasks can be started")
            db.execute("INSERT INTO workflows(task_id) VALUES (?)", (task_id,))
            self.transition(db, task_id, TaskState.QUEUED)
            self.wake(db, task_id, f"start:{task_id}")

    def flow(self, task_id: str):
        with self.db() as db:
            row = db.execute("SELECT * FROM workflows WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise WorkflowConflict("Task orchestration has not started")
        return dict(row)

    def task(self, task_id: str):
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise WorkflowConflict("Task does not exist")
        return dict(row)

    def check_active(self, task_id: str):
        if self.task(task_id)["state"] in TERMINAL:
            raise WorkflowConflict("Task has already ended")

    def next_event(self):
        with self.db() as db:
            row = db.execute("SELECT * FROM workflow_outbox WHERE handled=0 ORDER BY id LIMIT 1").fetchone()
        return dict(row) if row else None

    def ack(self, event_id: int):
        with self.db() as db:
            db.execute("UPDATE workflow_outbox SET handled=1 WHERE id=?", (event_id,))

    def schedule(self, job: JobRequest, sources: dict[str, str], state: TaskState, attempt_no: int = 1):
        """Do not redispatch a replayed node; allow at most three C/D rounds and one A round."""
        if attempt_no not in (1, 2, 3) or (job.stage == "PROFILE_A" and attempt_no != 1):
            raise WorkflowConflict("Job attempt budget exceeded")
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
            # Do not execute queued jobs for cancelled tasks.
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
            raise ValueError("Invalid job status")
        with self.db() as db:
            job = db.execute("SELECT * FROM workflow_jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None or job["state"] != "running":
                raise WorkflowConflict("Job has ended or its report is stale")
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
        """Commit approval and resume outbox together; identical submissions are idempotent and do not rerun C."""
        with self.db() as db:
            task = db.execute("SELECT * FROM tasks WHERE task_id=? AND owner_id=?",
                              (task_id, owner)).fetchone()
            flow = db.execute("SELECT * FROM workflows WHERE task_id=?", (task_id,)).fetchone()
            if task is None or flow is None or not flow["profile_json"]:
                raise WorkflowConflict("Task has not completed detection A")
            profile, issues = json.loads(flow["profile_json"]), json.loads(flow["issues_json"])
            spec = freeze_spec(task_id=task_id, input_sha256=task["input_sha256"], owner_id=owner,
                               task_version=version, columns=tuple(profile["columns"]),
                               decisions=decisions, issues=tuple(issues))
            if task["state"] == TaskState.CANCELLED:
                raise WorkflowConflict("Task has been cancelled")
            if flow["spec_json"]:
                from backend.domain.spec import FrozenSpec
                previous = FrozenSpec.model_validate_json(flow["spec_json"])
                if previous != spec:
                    raise WorkflowConflict("The specification is frozen and cannot be modified")
                return previous
            if task["state"] != TaskState.AWAITING_DECISIONS or task["version"] != version:
                raise WorkflowConflict("Approval state or version is stale")
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
            # Reuse initial source from old checkpoints and tasks without another model call.
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

    def request_validation_explanation(self, task_id: str, owner: str):
        from backend.llm.validation_explanation import validation_facts
        with self.db() as db:
            task = db.execute("SELECT * FROM tasks WHERE task_id=? AND owner_id=?",
                              (task_id, owner)).fetchone()
            if task is None:
                raise WorkflowConflict("Task does not exist")
            existing = db.execute("SELECT status FROM validation_explanations WHERE task_id=?",
                                  (task_id,)).fetchone()
            if existing:
                return existing["status"]
            if task["state"] not in (TaskState.SUCCEEDED, TaskState.FAILED):
                raise WorkflowConflict("Task has not ended")
            flow = db.execute("SELECT validation_json FROM workflows WHERE task_id=?", (task_id,)).fetchone()
            job = db.execute("""SELECT * FROM workflow_jobs WHERE task_id=? AND stage='VERIFY_D'
                              ORDER BY attempt_no DESC LIMIT 1""", (task_id,)).fetchone()
            if not flow or not job:
                raise WorkflowConflict("No D validation report is available to explain")
            if flow["validation_json"] and job["state"] == "finished":
                report = json.loads(flow["validation_json"])
            elif job["state"] == "failed" and job["error_code"] == "VALIDATION_REJECTED":
                evidence = json.loads(job["evidence_json"] or "{}")
                report = evidence.get("validation")
            else:
                raise WorkflowConflict("No D validation report is available to explain")
            request = json.loads(job["request_json"])
            if (not isinstance(report, dict) or request.get("task_id") != task_id
                    or request.get("input_sha256") != task["input_sha256"]
                    or report.get("spec_sha256") != request.get("spec_sha256")
                    or report.get("output_sha256") != request.get("candidate_sha256")
                    or report.get("verdict") != ("passed" if job["state"] == "finished" else "rejected")):
                raise WorkflowConflict("The D validation report binding is invalid")
            try:
                facts = validation_facts(report)
            except ValueError as exc:
                raise WorkflowConflict("The D validation report is invalid") from exc
            input_match = report.get("input_sha256") == request["input_sha256"]
            if facts["checks"][0]["passed"] != input_match:
                raise WorkflowConflict("The D validation report binding is invalid")
            db.execute("INSERT INTO validation_explanations(task_id,status,facts_json) VALUES(?,?,?)",
                       (task_id, "pending", json.dumps(facts, ensure_ascii=True)))
            self.wake(db, task_id, f"explain:{task_id}")
            return "pending"

    def validation_explanation(self, task_id: str):
        from backend.domain.spec import Action, FrozenSpec
        from backend.llm.validation_explanation import clarify_byte_check
        with self.db() as db:
            row = db.execute("""SELECT status,result_json,error_code,facts_json FROM validation_explanations
                              WHERE task_id=?""", (task_id,)).fetchone()
            task = db.execute("SELECT owner_id,input_sha256 FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            flow = db.execute("SELECT spec_json FROM workflows WHERE task_id=?", (task_id,)).fetchone()
            job = db.execute("""SELECT request_json FROM workflow_jobs WHERE task_id=? AND stage='VERIFY_D'
                              ORDER BY attempt_no DESC LIMIT 1""", (task_id,)).fetchone()
        if row is None:
            return {"status": "not_requested"}
        result = json.loads(row["result_json"]) if row["result_json"] else None
        if row["status"] == "completed" and isinstance(result, dict):
            all_preserved = None
            try:
                spec = FrozenSpec.model_validate_json(flow["spec_json"])
                request = json.loads(job["request_json"])
                facts = json.loads(row["facts_json"])
                if (str(spec.task_id) == task_id and spec.owner_id == task["owner_id"]
                        and spec.input_sha256 == task["input_sha256"]
                        and request.get("task_id") == task_id
                        and request.get("input_sha256") == spec.input_sha256
                        and request.get("spec_sha256") == spec.spec_sha256):
                    all_preserved = all(d.action == Action.PRESERVE for d in spec.decisions)
                    byte_passed = next(c["passed"] for c in facts["checks"]
                                       if c["id"] == "zero_change_bytes")
                    if not all_preserved and byte_passed is not True:
                        all_preserved = None
            except (AttributeError, TypeError, ValueError, KeyError, StopIteration):
                pass
            result["text"] = clarify_byte_check(result.get("text"), all_preserved)
        return {"status": row["status"], "result": result, "error_code": row["error_code"]}

    def claim_validation_explanation(self, task_id: str):
        with self.db() as db:
            row = db.execute("SELECT * FROM validation_explanations WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row["status"] in ("completed", "failed"):
                return None
            if row["status"] == "started":
                db.execute("""UPDATE validation_explanations SET status='failed',
                           error_code='EXPLANATION_CALL_UNCERTAIN' WHERE task_id=?""", (task_id,))
                return None
            if row["status"] != "pending":
                raise WorkflowConflict("Invalid explanation request state")
            db.execute("UPDATE validation_explanations SET status='started' WHERE task_id=?", (task_id,))
            return json.loads(row["facts_json"])

    def finish_validation_explanation(self, task_id: str, result=None, error_code=None):
        with self.db() as db:
            updated = db.execute("""UPDATE validation_explanations SET status=?,result_json=?,error_code=?
                                 WHERE task_id=? AND status='started'""",
                                 ("failed" if error_code else "completed",
                                  json.dumps(result, ensure_ascii=True) if result is not None else None,
                                  error_code, task_id))
            if updated.rowcount != 1:
                raise WorkflowConflict("Explanation request state is stale")

    def fail(self, task_id: str, code: str):
        with self.db() as db:
            task = db.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task and task["state"] not in TERMINAL:
                self.transition(db, task_id, TaskState.FAILED)
                db.execute("INSERT INTO events(task_id,kind,created_at) VALUES(?,?,?)",
                           (task_id, f"ERROR:{code}", _now()))


@contextmanager
def process_lock(root: Path, name: str):
    """Local OS file lock released on process crash; do not run the same graph concurrently using an expiring lease."""
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
