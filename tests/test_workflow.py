"""Durable workflow golden tests; the worker double parses only these hand-written test CSVs."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langgraph.checkpoint.sqlite")

from fastapi.testclient import TestClient
from backend.app import create_app
from backend.domain.spec import Decision, FrozenSpec
from backend.jobs.contracts import Stage
from backend.llm.generator import CodeVersion
from backend.llm.client import ModelGateway, ModelResponse
from backend.llm.validation_explanation import ValidationExplainer
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
        # Hand-written expectations; do not execute generated source on the host.
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
    assert not GraphRunner(root).run_once()  # No execution remains active while waiting for approval.
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
    assert api.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["state"] == "SUCCEEDED"
    assert api.get(f"/api/tasks/{task_id}/artifacts/output", headers=HEADERS).content == (
        root / "inputs" / f"{task_id}.csv").read_bytes()
    # Replaying old outbox entries must not create new A/C/D jobs or roll back state.
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
    # Replaying a resume wake before C completes must not regenerate source or create another C.
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
    assert WorkflowStore(root).task(task_id)["state"] == "SUCCEEDED"


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
    orphan = SimpleNamespace(
        name=f"ai-data-agent-{job['job_id']}",
        labels={"ai-data-agent.attempt": job["job_id"], "ai-data-agent.task": task_id},
        remove=lambda force: removed.append(force),
    )
    backend = SimpleNamespace(client=SimpleNamespace(
        containers=SimpleNamespace(list=lambda **_: [orphan])))
    worker = QueueWorker(root, {}, backend=backend)
    assert worker.run_once() is False
    assert removed == [True]
    assert store.job(job["job_id"])["state"] == "failed"
    GraphRunner(root).run_once()
    assert store.task(task_id)["state"] == "FAILED"


def test_ambiguous_creation_reconciles_late_container_without_auto_resolving(setup):
    from workers.sandbox_manager.queue_worker import QueueWorker, UnreclaimedSandbox
    root, _, task_id = setup
    GraphRunner(root).run_once()
    store = WorkflowStore(root)
    job = store.claim_job()
    directory = root / "worker_jobs" / job["job_id"]
    directory.mkdir(parents=True)
    (directory / "creation.pending").touch()
    removed = []
    orphan = SimpleNamespace(
        name=f"ai-data-agent-{job['job_id']}",
        labels={"ai-data-agent.attempt": job["job_id"], "ai-data-agent.task": task_id},
        remove=lambda force: removed.append(force),
    )
    containers = []
    backend = SimpleNamespace(client=SimpleNamespace(
        containers=SimpleNamespace(list=lambda **_: list(containers))))
    worker = QueueWorker(root, {}, backend)
    with pytest.raises(UnreclaimedSandbox):
        worker.recover_interrupted()
    assert store.job(job["job_id"])["state"] == "running"
    containers.append(orphan)
    with pytest.raises(UnreclaimedSandbox):
        worker.recover_interrupted()
    assert removed == [True]
    assert (directory / "creation.pending").exists()
    containers.clear()
    # This explicit operation stands in for an operator-confirmed daemon restart.
    worker.resolve_uncertain(job["job_id"])
    assert store.job(job["job_id"])["error_code"] == "CREATION_UNCERTAIN_RECONCILED"
    assert not (directory / "creation.pending").exists()
    assert (directory / "watchdog.done").exists()


def test_worker_does_not_finish_job_after_ambiguous_create(setup):
    from test_docker_worker import Client, FakeWatchdog, IMAGE
    from workers.sandbox_manager.docker_backend import DockerExecutionBackend
    from workers.sandbox_manager.queue_worker import QueueWorker, UnreclaimedSandbox
    root, _, _ = setup
    GraphRunner(root).run_once()
    client = Client()
    client.containers.list = lambda **kwargs: []

    def lost_response(*args, **kwargs):
        raise TimeoutError("simulated lost response")

    client.containers.create = lost_response
    backend = DockerExecutionBackend(root / "worker_jobs", dict.fromkeys(Stage, IMAGE),
                                     client, watchdog_factory=FakeWatchdog)
    worker = QueueWorker(root, backend.images, backend)
    with pytest.raises(UnreclaimedSandbox):
        worker.run_once()
    with worker.store.db() as db:
        job = db.execute("SELECT * FROM workflow_jobs").fetchone()
    assert job["state"] == "running"
    assert (root / "worker_jobs" / job["job_id"] / "creation.pending").exists()
    with pytest.raises(UnreclaimedSandbox):
        worker.run_once()


def test_legacy_running_attempt_without_watchdog_requires_confirmation(setup):
    from workers.sandbox_manager.queue_worker import QueueWorker, UnreclaimedSandbox
    root, _, _ = setup
    GraphRunner(root).run_once()
    store = WorkflowStore(root)
    job = store.claim_job()
    directory = root / "worker_jobs" / job["job_id"]
    directory.mkdir(parents=True)
    backend = SimpleNamespace(client=SimpleNamespace(
        containers=SimpleNamespace(list=lambda **_: [])))
    worker = QueueWorker(root, {}, backend)
    with pytest.raises(UnreclaimedSandbox):
        worker.recover_interrupted()
    assert (directory / "creation.pending").exists()
    assert store.job(job["job_id"])["state"] == "running"


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
    assert worker.run_once() is False  # Fail after restart reconciliation without rerunning source.
    assert len(calls) == 1


def test_profile_omitted_findings_are_rejected(setup):
    root, _, task_id = setup
    profile, issues = scan(root / "inputs" / f"{task_id}.csv")
    profile["status"] = "completed"
    with pytest.raises(ValueError, match="不完整"):
        validate_profile(profile, issues[:-1], profile["input_sha256"])


class ExplanationModel:
    def __init__(self, invalid=False, fail=False):
        self.calls, self.invalid, self.fail = [], invalid, fail

    async def complete(self, request):
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("private provider error")
        facts = json.loads(request.messages[-1].content)
        result = {"items": [{"check": item["id"], "style": "plain"} for item in facts["checks"]]}
        return ModelResponse(request_id=request.request_id, provider="fake", model="fake",
                             text="invalid" if self.invalid else json.dumps(result),
                             finish_reason="completed", input_tokens=20, output_tokens=12)


def finish_preserved(setup):
    root, api, task_id = setup
    review = awaiting(setup)
    assert api.post(f"/api/tasks/{task_id}/decisions", json=submission(review), headers=HEADERS).status_code == 200
    assert GraphRunner(root).run_once()
    assert simulated_worker(root)["stage"] == Stage.VERIFY_D
    assert GraphRunner(root).run_once()
    assert WorkflowStore(root).task(task_id)["state"] == "SUCCEEDED"


def test_on_demand_explanation_is_private_idempotent_and_separate_from_d(setup):
    root, api, task_id = setup
    finish_preserved(setup)
    url = f"/api/tasks/{task_id}/validation-explanation"
    assert api.get(url).status_code == 401
    assert api.post(url).status_code == 401
    assert api.get(url, headers=HEADERS).json()["status"] == "not_requested"
    before = api.get(f"/api/tasks/{task_id}/evidence", headers=HEADERS).json()
    output = api.get(f"/api/tasks/{task_id}/artifacts/output", headers=HEADERS).content
    model = ExplanationModel()
    runner = GraphRunner(root, explainer=ValidationExplainer(ModelGateway(model)))
    assert not runner.run_once()
    assert len(model.calls) == 0
    assert api.post(url, headers=HEADERS).json()["status"] == "pending"
    assert api.post(url, headers=HEADERS).json()["status"] == "pending"
    assert runner.run_once()
    result = api.get(url, headers=HEADERS).json()
    assert result["status"] == "completed"
    assert "D 独立验证结论：通过" in result["result"]["text"]
    assert "实际变更 0 格，不匹配 0 格" in result["result"]["text"]
    assert "本次全部保留，要求输入与输出的完整字节相同" in result["result"]["text"]
    assert result["result"]["input_tokens"] == 20
    assert len(model.calls) == 1
    sent = model.calls[0].messages[-1].content
    for forbidden in ("amount", "date", "2026", "Preserve refunds", task_id):
        assert forbidden not in sent
    assert api.post(url, headers=HEADERS).json()["status"] == "completed"
    with WorkflowStore(root).db() as db:
        db.execute("UPDATE workflow_outbox SET handled=0 WHERE dedup_key=?", (f"explain:{task_id}",))
    assert runner.run_once()
    assert not runner.run_once()
    assert len(model.calls) == 1
    assert api.get(f"/api/tasks/{task_id}/evidence", headers=HEADERS).json() == before
    assert api.get(f"/api/tasks/{task_id}/artifacts/output", headers=HEADERS).content == output


@pytest.mark.parametrize("change_cells", [False, True])
def test_byte_check_uses_approved_rules_not_change_count(setup, change_cells):
    root, api, task_id = setup
    review = awaiting(setup)
    body = submission(review, True)
    if not change_cells:
        for decision in body["decisions"]:
            if decision["action"] == "normalize_date":
                decision["params"]["input_formats"] = ["DD-MM-YYYY"]
    assert api.post(f"/api/tasks/{task_id}/decisions", json=body, headers=HEADERS).status_code == 200
    assert GraphRunner(root, TestGenerator(root)).run_once()
    if change_cells:
        simulated_worker(root)
    else:
        store = WorkflowStore(root)
        job = store.claim_job()
        result_dir = root / "worker_jobs" / job["job_id"] / "result"
        result_dir.mkdir(parents=True)
        (result_dir / "cleaned.csv").write_bytes((root / "inputs" / f"{task_id}.csv").read_bytes())
        store.finish_job(job["job_id"], "finished", str(result_dir), None)
    assert GraphRunner(root).run_once()
    simulated_worker(root)
    assert GraphRunner(root).run_once()
    model = ExplanationModel()
    url = f"/api/tasks/{task_id}/validation-explanation"
    assert api.post(url, headers=HEADERS).status_code == 202
    assert GraphRunner(root, explainer=ValidationExplainer(ModelGateway(model))).run_once()
    result = api.get(url, headers=HEADERS).json()["result"]["text"]
    assert "本次批准了修改，不要求输入与输出的完整字节相同" in result
    assert f"实际变更 {1 if change_cells else 0} 格" in result
    assert "all_preserved" not in model.calls[0].messages[-1].content


def test_existing_explanation_is_corrected_and_unbound_spec_does_not_guess(setup):
    root, api, task_id = setup
    finish_preserved(setup)
    url = f"/api/tasks/{task_id}/validation-explanation"
    assert api.post(url, headers=HEADERS).status_code == 202
    model = ExplanationModel()
    assert GraphRunner(root, explainer=ValidationExplainer(ModelGateway(model))).run_once()
    store = WorkflowStore(root)
    with store.db() as db:
        row = db.execute("SELECT result_json FROM validation_explanations WHERE task_id=?", (task_id,)).fetchone()
        result = json.loads(row["result_json"])
        result["text"] = "全保留字节检查：通过。"
        db.execute("UPDATE validation_explanations SET result_json=? WHERE task_id=?",
                   (json.dumps(result), task_id))
    assert "本次全部保留" in api.get(url, headers=HEADERS).json()["result"]["text"]
    with store.db() as db:
        db.execute("UPDATE workflows SET spec_json=? WHERE task_id=?", ("{}", task_id))
    text = api.get(url, headers=HEADERS).json()["result"]["text"]
    assert "仅全保留时要求" in text
    assert "本次全部保留" not in text and "本次批准了修改" not in text
    assert len(model.calls) == 1


def test_concurrent_requests_share_one_explanation_event(setup):
    root, api, task_id = setup
    finish_preserved(setup)
    url = f"/api/tasks/{task_id}/validation-explanation"
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: api.post(
            url, headers=HEADERS, json={"verdict": "invented"}), range(4)))
    assert all(response.status_code == 202 and response.json()["status"] == "pending"
               for response in responses)
    with WorkflowStore(root).db() as db:
        assert db.execute("SELECT count(*) FROM validation_explanations").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM workflow_outbox WHERE dedup_key=?",
                          (f"explain:{task_id}",)).fetchone()[0] == 1
    model = ExplanationModel()
    assert GraphRunner(root, explainer=ValidationExplainer(ModelGateway(model))).run_once()
    assert len(model.calls) == 1
    facts = json.loads(model.calls[0].messages[-1].content)
    assert facts["verdict"] == "passed"


def test_missing_model_explanation_leaves_completed_task_available(setup):
    root, api, task_id = setup
    finish_preserved(setup)
    url = f"/api/tasks/{task_id}/validation-explanation"
    api.post(url, headers=HEADERS)
    assert GraphRunner(root).run_once()
    assert api.get(url, headers=HEADERS).json()["error_code"] == "EXPLANATION_MODEL_UNAVAILABLE"
    assert api.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["state"] == "SUCCEEDED"
    assert api.get(f"/api/tasks/{task_id}/artifacts/output", headers=HEADERS).status_code == 200


def test_unbound_d_report_is_not_sent_to_model(setup):
    root, api, task_id = setup
    finish_preserved(setup)
    store = WorkflowStore(root)
    with store.db() as db:
        report = json.loads(db.execute("SELECT validation_json FROM workflows WHERE task_id=?",
                                       (task_id,)).fetchone()[0])
        report["spec_sha256"] = "0" * 64
        db.execute("UPDATE workflows SET validation_json=? WHERE task_id=?", (json.dumps(report), task_id))
    url = f"/api/tasks/{task_id}/validation-explanation"
    assert api.post(url, headers=HEADERS).status_code == 409
    assert api.get(url, headers=HEADERS).json()["status"] == "not_requested"


@pytest.mark.parametrize("invalid", [False, True])
def test_explanation_failure_does_not_change_task_or_retry(setup, invalid):
    root, api, task_id = setup
    finish_preserved(setup)
    url = f"/api/tasks/{task_id}/validation-explanation"
    before = api.get(f"/api/tasks/{task_id}/evidence", headers=HEADERS).json()
    model = ExplanationModel(invalid=invalid, fail=not invalid)
    runner = GraphRunner(root, explainer=ValidationExplainer(ModelGateway(model)))
    assert api.post(url, headers=HEADERS).status_code == 202
    assert runner.run_once()
    response = api.get(url, headers=HEADERS).json()
    assert response["status"] == "failed"
    assert response["error_code"] == ("INVALID_MODEL_EXPLANATION" if invalid else "EXPLANATION_MODEL_UNAVAILABLE")
    assert "private provider error" not in json.dumps(response)
    if invalid:
        assert response["result"]["input_tokens"] == 20
    assert api.post(url, headers=HEADERS).json()["status"] == "failed"
    assert not runner.run_once() and len(model.calls) == 1
    assert api.get(f"/api/tasks/{task_id}/evidence", headers=HEADERS).json() == before
    assert api.get(f"/api/tasks/{task_id}/artifacts/output", headers=HEADERS).status_code == 200


def test_interrupted_explanation_never_repeats_external_call(setup):
    root, api, task_id = setup
    finish_preserved(setup)
    url = f"/api/tasks/{task_id}/validation-explanation"
    api.post(url, headers=HEADERS)
    assert WorkflowStore(root).claim_validation_explanation(task_id)
    model = ExplanationModel()
    runner = GraphRunner(root, explainer=ValidationExplainer(ModelGateway(model)))
    assert runner.run_once()
    result = api.get(url, headers=HEADERS).json()
    assert result["status"] == "failed" and result["error_code"] == "EXPLANATION_CALL_UNCERTAIN"
    assert not model.calls and not runner.run_once()
    assert api.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["state"] == "SUCCEEDED"


@pytest.mark.parametrize("structural", [False, True])
def test_explanation_never_overrides_rejected_d(setup, structural):
    root, api, task_id = setup
    review = awaiting(setup)
    assert api.post(f"/api/tasks/{task_id}/decisions", json=submission(review, True), headers=HEADERS).status_code == 200
    assert GraphRunner(root, TestGenerator(root)).run_once()
    store = WorkflowStore(root)
    clean = store.claim_job()
    assert clean["stage"] == Stage.CLEAN_C
    result_dir = root / "worker_jobs" / clean["job_id"] / "result"
    result_dir.mkdir(parents=True)
    candidate = (b"id,date,amount,extra\n1,2026/10/31,-3,x\n" if structural
                 else b"id,date,amount\n1,2026/10/31,-3\n")
    (result_dir / "cleaned.csv").write_bytes(candidate)
    store.finish_job(clean["job_id"], "finished", str(result_dir), None)
    assert GraphRunner(root).run_once()
    job = store.claim_job()
    assert job["stage"] == Stage.VERIFY_D
    sources = {name: Path(ref) for name, ref in json.loads(job["sources_json"]).items()}
    spec = FrozenSpec.model_validate_json(sources["spec.json"].read_bytes())
    report = verify(sources["data.csv"], sources["candidate.csv"], spec)
    assert report["verdict"] == "rejected"
    store.finish_job(job["job_id"], "failed", None, "VALIDATION_REJECTED", {"validation": report})
    assert GraphRunner(root).run_once()
    assert store.task(task_id)["state"] == "FAILED"
    url = f"/api/tasks/{task_id}/validation-explanation"
    model = ExplanationModel()
    assert api.post(url, headers=HEADERS).status_code == 202
    assert GraphRunner(root, explainer=ValidationExplainer(ModelGateway(model))).run_once()
    text = api.get(url, headers=HEADERS).json()["result"]["text"]
    assert "D 独立验证结论：未通过" in text
    if structural:
        assert "未进行逐格比较" in text and "不匹配 0 格" not in text
        assert "mismatch_count" not in model.calls[0].messages[-1].content
    else:
        assert "不匹配 1 格" in text
    assert store.task(task_id)["state"] == "FAILED"
    assert api.get(f"/api/tasks/{task_id}/artifacts/output", headers=HEADERS).status_code == 409
