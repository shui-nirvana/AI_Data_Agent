"""Publication fault tests using explicitly synthetic CSV fixtures."""

import hashlib
import json
import threading
from uuid import uuid4

import pytest

from test_workflow import HEADERS, TestGenerator, awaiting, setup, simulated_worker, submission
from backend.artifacts import publication
from backend.artifacts.publication import ArtifactIntegrityError, Publisher, write_atomic
from backend.orchestration.runner import GraphRunner
from backend.orchestration.store import WorkflowConflict, WorkflowStore


def validated(setup, monkeypatch):
    root, api, task_id = setup
    review = awaiting(setup)
    api.post(f"/api/tasks/{task_id}/decisions", json=submission(review), headers=HEADERS)
    GraphRunner(root).run_once()
    job = simulated_worker(root)
    with monkeypatch.context() as patch:
        patch.setattr(Publisher, "publish", lambda *args: None)
        GraphRunner(root).run_once()
    store = WorkflowStore(root)
    assert store.task(task_id)["state"] == "VALIDATED"
    return store, job


def test_publish_download_evidence_and_replay(setup, monkeypatch):
    root, api, task_id = setup
    store, _ = validated(setup, monkeypatch)
    Publisher(store).publish(task_id)
    Publisher(store).publish(task_id)
    output = api.get(f"/api/tasks/{task_id}/artifacts/output", headers=HEADERS)
    assert output.status_code == 200
    assert output.headers["x-content-sha256"] == hashlib.sha256(output.content).hexdigest()
    assert output.headers["cache-control"] == "no-store"
    evidence = api.get(f"/api/tasks/{task_id}/evidence", headers=HEADERS).json()
    assert len(evidence["artifacts"]) == 3
    assert evidence["validation"]["verdict"] == "passed"
    assert str(root) not in json.dumps(evidence)
    assert api.get(f"/api/tasks/{task_id}/artifacts/output").status_code == 401
    assert api.get(f"/api/tasks/{uuid4()}/artifacts/output", headers=HEADERS).status_code == 404
    assert api.get(f"/api/tasks/{task_id}/artifacts/unknown", headers=HEADERS).status_code == 409
    api.post(f"/api/tasks/{task_id}/cancel", headers=HEADERS)
    assert store.task(task_id)["state"] == "SUCCEEDED"


@pytest.mark.parametrize("target", ["candidate", "input", "report", "spec"])
def test_tamper_prevents_registration(setup, monkeypatch, target):
    root, _, task_id = setup
    store, job = validated(setup, monkeypatch)
    sources = json.loads(job["sources_json"])
    from pathlib import Path
    paths = {"candidate": Path(sources["candidate.csv"]),
             "input": Path(sources["data.csv"]),
             "spec": Path(sources["spec.json"]),
             "report": root / "worker_jobs" / job["job_id"] / "result" / "validation.json"}
    path = paths[target]
    path.chmod(0o600)
    path.write_bytes(b"{}" if target in ("report", "spec") else b"tampered")
    with pytest.raises(ValueError):
        Publisher(store).publish(task_id)
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM workflow_artifacts").fetchone()[0] == 0


def test_cancel_wins_before_publish(setup, monkeypatch):
    _, api, task_id = setup
    store, _ = validated(setup, monkeypatch)
    api.post(f"/api/tasks/{task_id}/cancel", headers=HEADERS)
    with pytest.raises(WorkflowConflict):
        Publisher(store).publish(task_id)
    assert api.get(f"/api/tasks/{task_id}/artifacts/output", headers=HEADERS).status_code == 409


def test_partial_files_remain_private_and_replay_completes(setup, monkeypatch):
    root, api, task_id = setup
    store, _ = validated(setup, monkeypatch)
    original = publication.write_atomic
    calls = []

    def fail_second(*args):
        calls.append(args)
        if len(calls) == 2:
            raise OSError("synthetic disk failure")
        return original(*args)

    with monkeypatch.context() as patch:
        patch.setattr(publication, "write_atomic", fail_second)
        with pytest.raises(OSError):
            Publisher(store).publish(task_id)
    assert store.task(task_id)["state"] == "VALIDATED"
    assert (root / "published" / task_id / "cleaned.csv").exists()
    assert api.get(f"/api/tasks/{task_id}/artifacts/output", headers=HEADERS).status_code == 409
    Publisher(store).publish(task_id)
    output = root / "published" / task_id / "cleaned.csv"
    output.chmod(0o600)
    output.write_bytes(b"tampered after publication")
    assert api.get(f"/api/tasks/{task_id}/artifacts/output", headers=HEADERS).json()["detail"] == "ARTIFACT_INTEGRITY"


def test_old_completed_checkpoint_is_published_on_restart(setup, monkeypatch):
    _, _, task_id = setup
    store, _ = validated(setup, monkeypatch)
    while GraphRunner(store.root).run_once():
        pass
    assert store.task(task_id)["state"] == "SUCCEEDED"


def test_atomic_write_rejects_escape_and_parent_link(tmp_path):
    with pytest.raises(ArtifactIntegrityError):
        write_atomic(tmp_path, tmp_path.parent / "outside.csv", b"test")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit symlink creation")
    with pytest.raises(ArtifactIntegrityError):
        write_atomic(tmp_path, link / "new" / "output.csv", b"test")
    assert not (outside / "new").exists()


def test_registration_failure_rolls_back_and_replay_finishes(setup, monkeypatch):
    _, api, task_id = setup
    store, _ = validated(setup, monkeypatch)
    transition = store.transition
    def fail_success(db, task_id, state):
        if state == "SUCCEEDED":
            raise OSError("synthetic registration failure")
        return transition(db, task_id, state)
    with monkeypatch.context() as patch:
        patch.setattr(store, "transition", fail_success)
        with pytest.raises(OSError):
            Publisher(store).publish(task_id)
    assert store.task(task_id)["state"] == "VALIDATED"
    assert api.get(f"/api/tasks/{task_id}/artifacts/output", headers=HEADERS).status_code == 409
    Publisher(store).publish(task_id)
    assert store.task(task_id)["state"] == "SUCCEEDED"


@pytest.mark.parametrize("mutation", ["c_candidate", "code_binding", "d_attempt"])
def test_c_code_and_d_attempt_are_bound(setup, monkeypatch, mutation):
    root, api, task_id = setup
    review = awaiting(setup)
    api.post(f"/api/tasks/{task_id}/decisions", json=submission(review, True), headers=HEADERS)
    generator = TestGenerator(root)
    GraphRunner(root, generator).run_once()
    c_job = simulated_worker(root)
    GraphRunner(root, generator).run_once()
    d_job = simulated_worker(root)
    with monkeypatch.context() as patch:
        patch.setattr(Publisher, "publish", lambda *args: None)
        GraphRunner(root, generator).run_once()
    store = WorkflowStore(root)
    if mutation == "c_candidate":
        (root / "worker_jobs" / c_job["job_id"] / "result" / "cleaned.csv").write_bytes(b"tampered")
    else:
        code = store.code(task_id, 1)
        with store.db() as db:
            if mutation == "d_attempt":
                db.execute("UPDATE workflow_jobs SET attempt_no=2 WHERE job_id=?", (d_job["job_id"],))
            else:
                code["code_sha256"] = "0" * 64
                db.execute("UPDATE workflow_codes SET code_json=? WHERE task_id=?", (json.dumps(code), task_id))
    with pytest.raises(ArtifactIntegrityError):
        Publisher(store).publish(task_id)


def test_concurrent_cancel_waits_for_publication_commit(setup, monkeypatch):
    _, _, task_id = setup
    store, _ = validated(setup, monkeypatch)
    writing, release, cancelling = threading.Event(), threading.Event(), threading.Event()
    original = publication.write_atomic
    errors = []
    results = []

    def pause_write(root, path, data):
        if path.name == "cleaned.csv":
            writing.set()
            assert release.wait(3)
        return original(root, path, data)

    def publish():
        try:
            Publisher(store).publish(task_id)
        except Exception as error:
            errors.append(error)

    def cancel():
        cancelling.set()
        results.append(store.tasks.cancel(task_id, "demo"))

    with monkeypatch.context() as patch:
        patch.setattr(publication, "write_atomic", pause_write)
        publisher = threading.Thread(target=publish)
        publisher.start()
        assert writing.wait(3)
        cancellation = threading.Thread(target=cancel)
        cancellation.start()
        assert cancelling.wait(3)
        release.set()
        publisher.join(5)
        cancellation.join(5)
    assert not publisher.is_alive() and not cancellation.is_alive() and not errors
    assert results[0].state == "SUCCEEDED"
