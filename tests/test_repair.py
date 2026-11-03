"""持久化修复预算、重启重放、停止判据；模型源码绝不在宿主执行。"""

import hashlib
import json

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

    async def repair(self, previous, error_code, redacted_log):
        self.repairs.append(error_code)
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
    evidence = {
        "task_id": job["task_id"], "attempt_id": job["job_id"],
        "code_sha256": request["code_sha256"], "spec_sha256": request["spec_sha256"],
        "input_sha256": request["input_sha256"], "exit_code": 1,
        "timed_out": False, "limit_reason": None, "runtime_category": category,
    }
    evidence.update(changes)
    store.finish_job(job["job_id"], "failed", None, error_code, evidence)
    return job


def test_two_repairs_restart_and_distinct_attempts(setup):
    root, _, task_id = setup
    generator = ready(setup)
    first = fail_c(root)
    GraphRunner(root, generator).run_once()
    # 重放首轮失败 wake，C2 排队中；不得再次调用 repair 或新建 C2。
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
    assert store.task(task_id)["state"] == "VALIDATED"
    with store.db() as db:
        codes = db.execute("SELECT code_json FROM workflow_codes").fetchall()
    assert len({json.loads(c["code_json"])["code_sha256"] for c in codes}) == 3


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
