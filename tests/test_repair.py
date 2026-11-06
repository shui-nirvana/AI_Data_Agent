"""Test durable repair budgets, restart replay, and stop conditions without executing model source on the host."""

import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("langgraph")

from backend.llm.generator import CodeVersion
from backend.orchestration.runner import GraphRunner
from backend.orchestration.store import WorkflowStore
from test_workflow import setup, awaiting, submission, simulated_worker, TestGenerator


class RepairGenerator(TestGenerator):
    def __init__(self, root):
        super().__init__(root)
        self.repairs = []
        self.diagnostics = []
        self.executed_sources = []

    async def repair(self, previous, error_code, redacted_log, executed_source):
        self.repairs.append(error_code)
        self.diagnostics.append(redacted_log)
        self.executed_sources.append((executed_source, Path(executed_source.source_ref).read_bytes()))
        path = self.root / f"repaired-{len(self.repairs)}.py"
        path.write_bytes(f"# repaired version {len(self.repairs)}\n".encode())
        return CodeVersion(previous.spec_sha256, hashlib.sha256(path.read_bytes()).hexdigest(),
                           str(path), "test_repair_not_llm")


def ready(setup):
    root, api, task_id = setup
    review = awaiting(setup)
    assert api.post(f"/api/tasks/{task_id}/decisions", json=submission(review, True),
                    headers={"X-Demo-Token": "test-secret"}).status_code == 200
    generator = RepairGenerator(root)
    GraphRunner(root, generator).run_once()
    return generator


def fail_c(root, error_code="RUNTIME_ERROR", category="runtime_error", **changes):
    store = WorkflowStore(root)
    job = store.claim_job()
    request = json.loads(job["request_json"])
    source = Path(json.loads(job["sources_json"])["clean.py"]).read_bytes()
    snapshot = root / "worker_jobs" / job["job_id"] / "input" / "clean.py"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(source)
    evidence = {
        "task_id": job["task_id"], "attempt_id": job["job_id"],
        "code_sha256": request["code_sha256"], "spec_sha256": request["spec_sha256"],
        "input_sha256": request["input_sha256"], "exit_code": 1,
        "timed_out": False, "limit_reason": None, "runtime_category": category,
        "executed_source_ref": str(snapshot),
        "executed_source_sha256": hashlib.sha256(source).hexdigest(),
    }
    evidence.update(changes)
    store.finish_job(job["job_id"], "failed", None, error_code, evidence)
    return job


def test_two_repairs_restart_and_distinct_attempts(setup):
    root, _, task_id = setup
    generator = ready(setup)
    first = fail_c(root)
    GraphRunner(root, generator).run_once()
    # Replay the first failure wake while C2 is queued; do not call repair or create C2 again.
    store = WorkflowStore(root)
    with store.db() as db:
        db.execute("UPDATE workflow_outbox SET handled=0 WHERE dedup_key=?", (f"job:{first['job_id']}",))
    GraphRunner(root, generator).run_once()
    second = fail_c(root, "MISSING_OUTPUT", "completed", exit_code=0)
    GraphRunner(root, generator).run_once()
    third = simulated_worker(root)
    GraphRunner(root, generator).run_once()
    d = simulated_worker(root)
    GraphRunner(root, generator).run_once()
    assert len({first["job_id"], second["job_id"], third["job_id"], d["job_id"]}) == 4
    assert [first["attempt_no"], second["attempt_no"], third["attempt_no"], d["attempt_no"]] == [1, 2, 3, 3]
    assert generator.calls == 1 and generator.repairs == ["RUNTIME_ERROR", "MISSING_OUTPUT"]
    assert store.task(task_id)["state"] == "SUCCEEDED"
    with store.db() as db:
        codes = db.execute("SELECT code_json FROM workflow_codes").fetchall()
    assert len({json.loads(c["code_json"])["code_sha256"] for c in codes}) == 3


def test_runtime_diagnostic_follows_bound_failed_attempt(setup):
    root, _, _ = setup
    generator = ready(setup)
    diagnostic = {"exception_type": "TypeError", "line": 1, "function": "<module>"}
    fail_c(root, runtime_diagnostic=diagnostic)
    GraphRunner(root, generator).run_once()
    assert generator.diagnostics == [diagnostic]


def test_repair_uses_failed_execution_snapshot_after_original_changes(setup):
    root, _, _ = setup
    generator = ready(setup)
    job = fail_c(root)
    original = Path(json.loads(job["sources_json"])["clean.py"])
    original.write_bytes(b"# changed after execution\n")
    GraphRunner(root, generator).run_once()
    snapshot, content = generator.executed_sources[0]
    assert content != original.read_bytes()
    assert snapshot.sha256 == hashlib.sha256(content).hexdigest()
    assert snapshot.source_ref != str(original)


def test_repair_uses_modified_execution_not_generated_version(setup):
    root, _, task_id = setup
    generator = ready(setup)
    store = WorkflowStore(root)
    with store.db() as db:
        job = db.execute("SELECT * FROM workflow_jobs WHERE task_id=? AND stage='CLEAN_C'",
                         (task_id,)).fetchone()
    generated_hash = json.loads(job["request_json"])["code_sha256"]
    source = Path(json.loads(job["sources_json"])["clean.py"])
    modified = b"def clean():\n    raise RuntimeError('controlled')\nclean()\n"
    source.write_bytes(modified)
    fail_c(root, runtime_diagnostic={"exception_type": "RuntimeError", "line": 2,
                                    "function": "clean"})
    GraphRunner(root, generator).run_once()
    snapshot, content = generator.executed_sources[0]
    assert content == modified
    assert snapshot.sha256 != generated_hash
    assert generator.diagnostics[0]["function"] == "clean"


def test_repair_rejects_snapshot_from_another_attempt(setup):
    root, _, task_id = setup
    generator = ready(setup)
    other = root / "unrelated.py"
    other.write_bytes(b"# unrelated\n")
    fail_c(root, executed_source_ref=str(other),
           executed_source_sha256=hashlib.sha256(other.read_bytes()).hexdigest())
    GraphRunner(root, generator).run_once()
    assert generator.repairs == []
    assert WorkflowStore(root).task(task_id)["state"] == "FAILED"


def test_old_failure_without_snapshot_stops_repair(setup):
    root, _, task_id = setup
    generator = ready(setup)
    fail_c(root, executed_source_ref=None, executed_source_sha256=None)
    GraphRunner(root, generator).run_once()
    assert generator.repairs == []
    assert WorkflowStore(root).task(task_id)["state"] == "FAILED"


def test_modified_execution_still_undergoes_source_policy(setup):
    from backend.jobs.contracts import Stage
    from test_docker_worker import Client, IMAGE
    from workers.sandbox_manager.docker_backend import DockerExecutionBackend
    from workers.sandbox_manager.queue_worker import QueueWorker

    root, _, task_id = setup
    ready(setup)
    store = WorkflowStore(root)
    with store.db() as db:
        job = db.execute("SELECT * FROM workflow_jobs WHERE task_id=? AND stage='CLEAN_C'",
                         (task_id,)).fetchone()
    source = Path(json.loads(job["sources_json"])["clean.py"])
    source.write_text("import subprocess\n", encoding="utf-8")
    client = Client()
    backend = DockerExecutionBackend(root / "worker_jobs", dict.fromkeys(Stage, IMAGE), client)
    assert QueueWorker(root, backend.images, backend).run_once()
    failed = store.job(job["job_id"])
    assert failed["state"] == "failed" and failed["error_code"] == "SourcePolicyError"
    assert not client.created


def test_repair_stops_if_execution_snapshot_changed(setup):
    root, _, task_id = setup
    generator = ready(setup)
    job = fail_c(root)
    snapshot = root / "worker_jobs" / job["job_id"] / "input" / "clean.py"
    snapshot.write_bytes(b"# changed after execution\n")
    GraphRunner(root, generator).run_once()
    assert generator.repairs == []
    assert WorkflowStore(root).task(task_id)["state"] == "FAILED"


def test_missing_output_does_not_forward_a_runtime_diagnostic(setup):
    root, _, _ = setup
    generator = ready(setup)
    fail_c(root, "MISSING_OUTPUT", "completed", exit_code=0,
           runtime_diagnostic={"exception_type": "TypeError", "line": 1, "function": "fake"})
    GraphRunner(root, generator).run_once()
    assert generator.diagnostics == [None]


def test_third_c_failure_stops(setup):
    root, _, task_id = setup
    generator = ready(setup)
    for _ in range(3):
        fail_c(root)
        GraphRunner(root, generator).run_once()
    assert len(generator.repairs) == 2
    assert WorkflowStore(root).task(task_id)["state"] == "FAILED"
    assert WorkflowStore(root).claim_job() is None


@pytest.mark.parametrize("changes", [
    {"timed_out": True}, {"limit_reason": "oom"}, {"runtime_category": "security_violation"},
    {"runtime_category": "restricted_or_input_error"}, {"code_sha256": "a" * 64},
    {"spec_sha256": "a" * 64}, {"attempt_id": "other"},
])
def test_limits_security_or_unbound_evidence_stop(setup, changes):
    root, _, task_id = setup
    generator = ready(setup)
    fail_c(root, **changes)
    GraphRunner(root, generator).run_once()
    assert generator.repairs == []
    assert WorkflowStore(root).task(task_id)["state"] == "FAILED"


def test_d_rejection_never_triggers_repair(setup):
    root, _, task_id = setup
    generator = ready(setup)
    simulated_worker(root)
    GraphRunner(root, generator).run_once()
    simulated_worker(root, failed=True)
    GraphRunner(root, generator).run_once()
    assert generator.repairs == []
    assert WorkflowStore(root).task(task_id)["state"] == "FAILED"
