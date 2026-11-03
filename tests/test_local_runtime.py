"""Offline operations and liveness tests; never execute model code on the host."""

import json
import os
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.local_runtime import backup, cleanup, heartbeat, readiness, restore
from backend.orchestration.store import WorkflowStore
from test_api import upload


def test_readiness_requires_all_services_and_auth(tmp_path):
    with TestClient(create_app(data_dir=tmp_path, demo_token="test-secret")) as api:
        assert api.get("/ready").status_code == 401
        assert api.get("/ready", headers={"X-Demo-Token": "test-secret"}).status_code == 503
        for service in ("api", "runner", "worker"):
            heartbeat(tmp_path, service)
        assert readiness(tmp_path)["ready"]
        assert api.get("/ready", headers={"X-Demo-Token": "test-secret"}).status_code == 200
        with pytest.raises(RuntimeError):
            backup(tmp_path, tmp_path.parent / "active-backup.zip")


def test_offline_backup_restore_requires_original_path(tmp_path):
    root = tmp_path / "runtime"
    api = TestClient(create_app(data_dir=root, demo_token="test-secret"))
    task = upload(api).json()
    archive = tmp_path / "private-backup.zip"
    backup(root, archive)
    with pytest.raises(ValueError):
        restore(archive, tmp_path / "relocated")
    root.rename(tmp_path / "old-runtime")
    restore(archive, root)
    assert WorkflowStore(root).task(task["task_id"])["input_sha256"] == task["input_sha256"]
    assert (root / "inputs" / f"{task['task_id']}.csv").read_bytes() == b"date,amount\n2026/10/31,-3\n"
    with pytest.raises(ValueError):
        restore(archive, root)


def test_restore_rejects_path_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escape", "unsafe")
    with pytest.raises(ValueError):
        restore(archive, tmp_path / "new-runtime")
    assert not (tmp_path / "escape").exists()


def test_cleanup_is_dry_run_and_keeps_active_tasks(tmp_path):
    api = TestClient(create_app(data_dir=tmp_path, demo_token="test-secret"))
    done, active = upload(api).json(), upload(api).json()
    api.post(f"/api/tasks/{done['task_id']}/cancel", headers={"X-Demo-Token": "test-secret"})
    store = WorkflowStore(tmp_path)
    with store.db() as db:
        db.execute("UPDATE events SET created_at='2000-01-01T00:00:00+00:00'")
    assert cleanup(tmp_path, 7)["eligible_tasks"] == [done["task_id"]]
    completed_file = tmp_path / "inputs" / f"{done['task_id']}.csv"
    assert completed_file.exists()
    assert cleanup(tmp_path, 7, True)["applied"]
    assert not completed_file.exists()
    assert (tmp_path / "inputs" / f"{active['task_id']}.csv").exists()
    assert store.task(done["task_id"])["state"] == "CANCELLED"


def test_admission_limit_does_not_leave_uploaded_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_AGENT_MAX_ACTIVE_TASKS", "1")
    api = TestClient(create_app(data_dir=tmp_path, demo_token="test-secret"))
    assert upload(api).status_code == 202
    assert upload(api).status_code == 429
    assert len(list((tmp_path / "inputs").iterdir())) == 1
    monkeypatch.setattr("backend.api.tasks.shutil.disk_usage",
                        lambda _: type("Usage", (), {"free": 0})())
    assert upload(api).status_code == 507
