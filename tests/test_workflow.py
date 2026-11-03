"""持久化工作流金样本测试。模拟 worker 只解析这些人工测试 CSV。"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langgraph.checkpoint.sqlite")

from fastapi.testclient import TestClient
from backend.app import create_app
from backend.domain.spec import Decision, FrozenSpec
from backend.jobs.contracts import Stage
from backend.llm.generator import CodeVersion
from backend.orchestration.contracts import validate_profile
from backend.orchestration.runner import GraphRunner
from backend.orchestration.store import WorkflowConflict, WorkflowStore, process_lock
from images.profiler.main import scan
from images.validator.verify import verify

HEADERS = {"X-Demo-Token": "test-secret"}


@pytest.fixture
def setup(tmp_path):
    api = TestClient(create_app(data_dir=tmp_path, demo_token="test-secret"))
    response = api.post("/api/tasks", content=b"id,date,amount\n1,2026/10/31,-3\n",
                        headers={**HEADERS, "Content-Type": "text/csv", "X-Task-Goal": "Test"})
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    assert api.post(f"/api/tasks/{task_id}/start", headers=HEADERS).status_code == 202
    return tmp_path, api, task_id


def simulated_worker(root: Path, failed=False):
    store = WorkflowStore(root)
    job = store.claim_job()
    if job is None:
        return None
    if failed:
        store.finish_job(job["job_id"], "failed", None, "TEST_FAILURE")
        return job
    sources = {name: Path(ref) for name, ref in json.loads(job["sources_json"]).items()}
    output = root / "worker_jobs" / job["job_id"] / "result"
    output.mkdir(parents=True)
    if job["stage"] == Stage.PROFILE_A:
        profile, issues = scan(sources["data.csv"])
        profile["status"] = "completed"
        (output / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
        (output / "issues.json").write_text(json.dumps(issues), encoding="utf-8")
    elif job["stage"] == Stage.CLEAN_C:
        # 人工列出的期望，不在宿主执行任何生成源码。
        (output / "cleaned.csv").write_bytes(b"id,date,amount\n1,2026-10-31,-3\n")
    else:
        spec = FrozenSpec.model_validate_json(sources["spec.json"].read_bytes())
        report = verify(sources["data.csv"], sources["candidate.csv"], spec)
        (output / "validation.json").write_text(json.dumps(report), encoding="utf-8")
    store.finish_job(job["job_id"], "finished", str(output), None)
    return job


def awaiting(setup):
    root, api, task_id = setup
    assert GraphRunner(root).run_once()
    job = simulated_worker(root)
    assert job["stage"] == Stage.PROFILE_A
    assert GraphRunner(root).run_once()
    review = api.get(f"/api/tasks/{task_id}/review", headers=HEADERS)
    assert review.status_code == 200
    assert api.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["state"] == "AWAITING_DECISIONS"
    return review.json()


def submission(review, change=False):
    return {
        "task_version": review["task_version"],
        "decisions": [{
            "issue_id": issue["issue_id"], "column": issue["column"],
            "action": "normalize_date" if change and issue["column"] == "date" else "preserve",
            "predicate_id": "parseable_nonstandard_date" if change and issue["column"] == "date" else "all_cells",
            "params": {"input_formats": ["YYYY/MM/DD"]}
            if change and issue["column"] == "date" else {},
        } for issue in review["issues"]],
    }


def test_durable_interrupt_restart_and_zero_change(setup):
    root, api, task_id = setup
    review = awaiting(setup)
    assert (root / "checkpoints.sqlite3").exists()
    assert not GraphRunner(root).run_once()  # 审批等待无活动执行。
    assert WorkflowStore(root).claim_job() is None
    body = submission(review)
    url = f"/api/tasks/{task_id}/decisions"
    approved = api.post(url, json=body, headers=HEADERS)
    assert approved.status_code == 200
    assert api.post(url, json=body, headers=HEADERS).json() == approved.json()
    assert GraphRunner(root).run_once()
    job = simulated_worker(root)
    assert job["stage"] == Stage.VERIFY_D
    assert GraphRunner(root).run_once()
    assert api.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["state"] == "VALIDATED"
    assert api.get(f"/api/tasks/{task_id}/artifacts/output", headers=HEADERS).status_code == 501
    # 重放全部旧 outbox，不触发新的 A/C/D，也不能回退状态。
    store = WorkflowStore(root)
    with store.db() as db:
        db.execute("UPDATE workflow_outbox SET handled=0")
    while GraphRunner(root).run_once():
        pass
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM workflow_jobs").fetchone()[0] == 2
    assert store.claim_job() is None


class TestGenerator:
    __test__ = False

    def __init__(self, root):
        self.root, self.calls = root, 0

    async def generate(self, spec_ref, profile_ref):
        self.calls += 1
        spec = FrozenSpec.model_validate_json(Path(spec_ref).read_bytes())
        path = self.root / "test-generated.py"
        path.write_bytes(b"# test-only source, never executed by this unit test\n")
        return CodeVersion(spec.spec_sha256, hashlib.sha256(path.read_bytes()).hexdigest(),
                           str(path), "test_not_llm")


def test_generation_port_c_and_d_are_distinct_and_replay_is_idempotent(setup):
    root, api, task_id = setup
    review = awaiting(setup)
    assert api.post(f"/api/tasks/{task_id}/decisions", json=submission(review, True),
                    headers=HEADERS).status_code == 200
    generator = TestGenerator(root)
    assert GraphRunner(root, generator).run_once()
    # 恢复 wake 重放时 C 尚未完成，不能重新生成或新建 C。
    with WorkflowStore(root).db() as db:
        db.execute("UPDATE workflow_outbox SET handled=0 WHERE dedup_key LIKE 'approval:%'")
    assert GraphRunner(root, generator).run_once()
    c_job = simulated_worker(root)
    assert c_job["stage"] == Stage.CLEAN_C
    assert GraphRunner(root, generator).run_once()
    d_job = simulated_worker(root)
    assert d_job["stage"] == Stage.VERIFY_D and c_job["job_id"] != d_job["job_id"]
    assert GraphRunner(root, generator).run_once()
    assert generator.calls == 1
    assert WorkflowStore(root).task(task_id)["state"] == "VALIDATED"


def test_missing_model_fails_without_host_fallback(setup):
    root, api, task_id = setup
    review = awaiting(setup)
    api.post(f"/api/tasks/{task_id}/decisions", json=submission(review, True), headers=HEADERS)
    GraphRunner(root).run_once()
    assert WorkflowStore(root).task(task_id)["state"] == "FAILED"
    assert WorkflowStore(root).claim_job() is None


@pytest.mark.parametrize("mutation", ["version", "identity", "missing", "extra", "cancel"])
def test_approval_rejects_stale_or_untrusted_requests(setup, mutation):
    root, api, task_id = setup
    review = awaiting(setup)
    body = submission(review)
    if mutation == "version":
        body["task_version"] -= 1
    elif mutation == "identity":
        body["decisions"][0]["actor_id"] = "admin"
    elif mutation == "missing":
        body["decisions"].pop()
    elif mutation == "extra":
        body["issues"] = review["issues"]
    else:
        api.post(f"/api/tasks/{task_id}/cancel", headers=HEADERS)
    response = api.post(f"/api/tasks/{task_id}/decisions", json=body, headers=HEADERS)
    assert response.status_code == (409 if mutation in ("version", "cancel") else 422)
    assert WorkflowStore(root).flow(task_id)["spec_json"] is None


def test_cancel_blocks_late_result_and_new_jobs(setup):
    root, api, task_id = setup
    GraphRunner(root).run_once()
    store = WorkflowStore(root)
    job = store.claim_job()
    api.post(f"/api/tasks/{task_id}/cancel", headers=HEADERS)
    store.finish_job(job["job_id"], "finished", "ignored", None)
    assert store.job(job["job_id"])["state"] == "cancelled"
    GraphRunner(root).run_once()
    assert store.task(task_id)["state"] == "CANCELLED"
    with pytest.raises(WorkflowConflict):
        store.finish_job(job["job_id"], "finished", "ignored", None)


def test_sandbox_failure_and_bad_profile_are_not_zero_issues(setup):
    root, _, task_id = setup
    GraphRunner(root).run_once()
    simulated_worker(root, failed=True)
    GraphRunner(root).run_once()
    assert WorkflowStore(root).task(task_id)["state"] == "FAILED"
    original = root / "inputs" / f"{task_id}.csv"
    profile, issues = scan(original)
    profile["status"] = "completed"
    validate_profile(profile, issues, profile["input_sha256"])
    profile["tool_runs"].pop()
    with pytest.raises(ValueError, match="工具"):
        validate_profile(profile, issues, profile["input_sha256"])


def test_queue_worker_recovery_fails_old_attempt_without_rerun(setup):
    from workers.sandbox_manager.queue_worker import QueueWorker
    root, _, task_id = setup
    GraphRunner(root).run_once()
    store = WorkflowStore(root)
    job = store.claim_job()
    removed = []
    orphan = SimpleNamespace(remove=lambda force: removed.append(force))
    backend = SimpleNamespace(client=SimpleNamespace(
        containers=SimpleNamespace(list=lambda **_: [orphan])))
    worker = QueueWorker(root, {}, backend=backend)
    assert worker.run_once() is False
    assert removed == [True]
    assert store.job(job["job_id"])["state"] == "failed"
    GraphRunner(root).run_once()
    assert store.task(task_id)["state"] == "FAILED"


def test_process_lock_prevents_two_runners(setup):
    root, _, _ = setup
    with process_lock(root, "graph-runner"):
        with pytest.raises(OSError):
            GraphRunner(root).run_once()


def test_approval_outbox_error_rolls_back_spec_and_state(setup, monkeypatch):
    root, _, task_id = setup
    review = awaiting(setup)
    store = WorkflowStore(root)
    choices = tuple(Decision(**choice, actor_id="demo", task_version=review["task_version"])
                    for choice in submission(review)["decisions"])

    def failed_outbox(*args):
        raise OSError("transaction failure")

    monkeypatch.setattr(store, "wake", failed_outbox)
    with pytest.raises(OSError):
        store.approve(task_id, "demo", review["task_version"], choices)
    assert store.flow(task_id)["spec_json"] is None
    assert store.task(task_id)["state"] == "AWAITING_DECISIONS"


def test_cleanup_failure_stops_worker_and_keeps_attempt_for_recovery(setup):
    from backend.jobs.execution import ExecutionStatus
    from workers.sandbox_manager.queue_worker import QueueWorker, UnreclaimedSandbox
    root, _, task_id = setup
    GraphRunner(root).run_once()
    manifest = root / "failed-cleanup.json"
    manifest.write_text('{"error_code":"CLEANUP_FAILED"}')
    calls = []
    backend = SimpleNamespace(
        root=root / "worker_jobs",
        client=SimpleNamespace(containers=SimpleNamespace(list=lambda **_: [])),
        prepare=lambda *args: None,
        submit=lambda job, limits: calls.append(job) or "receipt",
        status=lambda receipt: ExecutionStatus(receipt, "failed", result_ref=str(manifest)),
    )
    worker = QueueWorker(root, {}, backend)
    with pytest.raises(UnreclaimedSandbox):
        worker.run_once()
    with WorkflowStore(root).db() as db:
        assert db.execute("SELECT state FROM workflow_jobs").fetchone()[0] == "running"
    assert worker.run_once() is False  # 重启式对账后失败，不重复执行源码。
    assert len(calls) == 1


def test_profile_omitted_findings_are_rejected(setup):
    root, _, task_id = setup
    profile, issues = scan(root / "inputs" / f"{task_id}.csv")
    profile["status"] = "completed"
    with pytest.raises(ValueError, match="不完整"):
        validate_profile(profile, issues[:-1], profile["input_sha256"])
