"""Real local safety checks with fixed test code, never model/provider claims."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest

pytest.importorskip("docker")
from backend.domain.spec import freeze_spec
from backend.jobs.contracts import JobRequest, Stage
from backend.jobs.execution import ExecutionReceipt
from backend.jobs.policy import LIMITS
from backend.orchestration.store import WorkflowStore, new_job
from workers.sandbox_manager.docker_backend import DockerExecutionBackend, UnreclaimedSandbox
from workers.sandbox_manager.queue_worker import QueueWorker
from workers.sandbox_manager.watchdog import reclaim

pytestmark = pytest.mark.skipif(os.environ.get("AI_AGENT_RUN_DOCKER_TESTS") != "1",
                                reason="Requires explicit real Docker opt-in")


def prepared(root, code):
    manifest = Path(os.environ.get("AI_AGENT_DOCKER_IMAGES", ".runtime/docker/images.json")).resolve()
    images = {Stage(k): v for k, v in json.loads(manifest.read_text())["images"].items()}
    source = root / "source.csv"
    source.write_bytes(b"id,value\n1,a\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    task_id = str(uuid4())
    spec = freeze_spec(task_id=task_id, input_sha256=digest, owner_id="demo",
                       task_version=1, columns=("id", "value"), decisions=(), issues=())
    spec_path = root / "spec.json"
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")
    code_path = root / "fixed-test.py"
    code_path.write_text(code, encoding="utf-8", newline="")
    job = JobRequest(task_id, str(uuid4()), Stage.CLEAN_C, digest, spec.spec_sha256,
                     hashlib.sha256(code.encode()).hexdigest())
    backend = DockerExecutionBackend(root / "attempts", images)
    backend.prepare(job, {"data.csv": source, "spec.json": spec_path, "clean.py": code_path})
    return backend, job, manifest


def reclaimed(backend, job):
    assert not backend.client.containers.list(all=True, filters={
        "label": f"ai-data-agent.attempt={job.attempt_id}"})


def test_real_runtime_diagnostic_excludes_exception_message(tmp_path):
    source = ("def failing_function():\n"
              "    raise TypeError('PRIVATE_ROW_AND_SECRET')\n"
              "failing_function()\n")
    backend, job, _ = prepared(tmp_path, source)
    status = backend.status(backend.submit(job, LIMITS[job.stage]))
    assert status.state == "failed" and status.exit_code == 1
    manifest = json.loads(Path(status.result_ref).read_text(encoding="utf-8"))
    assert manifest["runtime_category"] == "runtime_error"
    assert manifest["runtime_diagnostic"] == {
        "exception_type": "TypeError", "line": 2, "function": "failing_function"
    }
    assert "PRIVATE_ROW_AND_SECRET" not in json.dumps(manifest)
    reclaimed(backend, job)


def test_real_changed_snapshot_is_rejected_before_start(tmp_path):
    backend, job, _ = prepared(tmp_path, "raise RuntimeError()\n")
    snapshot = backend.root / job.attempt_id / "input" / "clean.py"
    snapshot.chmod(0o600)
    snapshot.write_bytes(b"raise RuntimeError('altered')\n")
    status = backend.status(backend.submit(job, LIMITS[job.stage]))
    manifest = json.loads(Path(status.result_ref).read_text(encoding="utf-8"))
    assert status.state == "failed" and manifest["error_code"] == "ValueError"
    assert manifest["runtime_diagnostic"] is None
    assert not (backend.root / job.attempt_id / "result").exists()
    reclaimed(backend, job)


def test_real_open_strict_error_has_only_fixed_reason(tmp_path):
    source = ("from pathlib import Path\n"
              "def main():\n"
              "    Path('/input/data.csv').open('r', strict=True)\n"
              "main()\n")
    backend, job, _ = prepared(tmp_path, source)
    status = backend.status(backend.submit(job, LIMITS[job.stage]))
    manifest = json.loads(Path(status.result_ref).read_text(encoding="utf-8"))
    assert status.state == "failed" and manifest["runtime_diagnostic"] == {
        "exception_type": "TypeError", "line": 3, "function": "main",
        "reason": "OPEN_STRICT_UNSUPPORTED",
    }
    assert "unexpected keyword" not in json.dumps(manifest)
    reclaimed(backend, job)


@pytest.mark.parametrize("mode", ["pid_exhaustion", "background", "aggregate_cpu"])
def test_kernel_limits_with_trusted_fault_driver(tmp_path, mode):
    """Bypass Python audit only in the test exec driver; Docker policy is unchanged."""
    backend, job, _ = prepared(tmp_path, "# fixed test placeholder\n")
    if mode == "aggregate_cpu":
        code = "import os\nfor _ in range(4):\n    if os.fork() == 0:\n        while True: pass\nwhile True: pass\n"
    elif mode == "background":
        code = """
import os, time
from pathlib import Path
for _ in range(4):
    if os.fork() == 0:
        time.sleep(90)
        os._exit(0)
Path('/output/cleaned.csv').write_bytes(Path('/input/data.csv').read_bytes())
Path('/tmp/runtime.json').write_text('{"category":"completed"}')
"""
    else:
        code = """
import os, errno, time
from pathlib import Path
assert Path('/sys/fs/cgroup/pids.max').read_text().strip() == '32'
blocked = False
for _ in range(64):
    try:
        child = os.fork()
    except OSError as error:
        assert error.errno == errno.EAGAIN
        blocked = True
        break
    if child == 0:
        time.sleep(90)
        os._exit(0)
assert blocked
Path('/output/cleaned.csv').write_bytes(Path('/input/data.csv').read_bytes())
Path('/tmp/runtime.json').write_text('{"category":"completed"}')
"""
    create = backend.client.api.exec_create

    def fixed_exec(container_id, command, **options):
        if command[:3] == ["python", "-m", "images.job_entry"]:
            command = ["python", "-c", code]
        return create(container_id, command, **options)

    backend.client.api.exec_create = fixed_exec
    receipt = backend.submit(job, LIMITS[job.stage])
    status = backend.status(receipt)
    if mode == "aggregate_cpu":
        assert status.state == "failed" and status.limit_reason == "cpu_budget"
    elif mode == "pid_exhaustion":
        # Assertions in the fault driver passed, but full PID pressure may prevent
        # Docker from starting its collector. That must fail closed, not publish.
        assert status.exit_code == 0
        if status.state == "failed":
            assert not (backend.root / job.attempt_id / "result").exists()
    else:
        assert status.state == "finished"
    reclaimed(backend, job)


def test_real_concurrent_cancel_reclaims_container(tmp_path):
    backend, job, _ = prepared(tmp_path, "import time\nwhile True: time.sleep(1)\n")
    result = []
    thread = threading.Thread(target=lambda: result.append(backend.submit(job, LIMITS[job.stage])))
    thread.start()
    deadline = time.monotonic() + 20
    receipt = ExecutionReceipt(job.task_id, job.attempt_id, f"docker-{job.attempt_id}")
    while time.monotonic() < deadline:
        with backend._lock:
            active = backend._active.get(receipt.handle)
        if active is not None:
            break
        time.sleep(0.1)
    assert active is not None
    backend.cancel(receipt)
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert backend.status(result[0]).state == "cancelled"
    reclaimed(backend, job)


def test_docker_call_timeout_fails_closed_and_reclaims(tmp_path):
    backend, job, _ = prepared(tmp_path, "# fixed source\n")
    def timeout(*args, **kwargs):
        raise TimeoutError("simulated unavailable Docker API")
    backend.client.api.exec_inspect = timeout
    status = backend.status(backend.submit(job, LIMITS[job.stage]))
    assert status.state == "failed"
    reclaimed(backend, job)


def test_real_create_succeeds_but_response_is_lost(tmp_path, monkeypatch):
    backend, job, _ = prepared(tmp_path, "# fixed source\n")
    manager_type = type(backend.client.containers)
    create = manager_type.create

    def lost_response(manager, *args, **kwargs):
        create(manager, *args, **kwargs)
        raise TimeoutError("simulated lost response")

    monkeypatch.setattr(manager_type, "create", lost_response)
    directory = backend.root / job.attempt_id
    try:
        with pytest.raises(UnreclaimedSandbox):
            backend.submit(job, LIMITS[job.stage])
        assert (directory / "creation.pending").exists()
        containers = backend.client.containers.list(all=True, filters={
            "label": f"ai-data-agent.attempt={job.attempt_id}"})
        assert len(containers) == 1
        assert containers[0].status == "created"
        reclaim(backend.client, job.task_id, job.attempt_id)
        reclaimed(backend, job)
        assert (directory / "creation.pending").exists()
    finally:
        reclaim(backend.client, job.task_id, job.attempt_id)
        (directory / "watchdog.done").touch()


def test_killed_worker_is_reconciled_without_rerunning_c(tmp_path):
    backend, job, manifest = prepared(tmp_path, "while True:\n    pass\n")
    store = WorkflowStore(tmp_path)
    store.tasks.create(job.task_id, "demo", "Synthetic crash test", job.input_sha256, 15)
    store.start(job.task_id, "demo")
    sources = {name: str(backend.root / job.attempt_id / "input" / name)
               for name in ("data.csv", "spec.json", "clean.py")}
    queued = new_job(job.task_id, Stage.CLEAN_C, job.input_sha256,
                     spec_sha256=job.spec_sha256, code_sha256=job.code_sha256)
    store.schedule(queued, sources, "EXECUTING")
    environment = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    process = subprocess.Popen([sys.executable, "-m", "workers.sandbox_manager.queue_worker",
                                "--once", "--data-dir", str(tmp_path), "--images", str(manifest)],
                               env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        containers = []
        while time.monotonic() < deadline:
            containers = backend.client.containers.list(all=True, filters={
                "label": f"ai-data-agent.attempt={queued.attempt_id}"})
            if containers and not (tmp_path / "worker_jobs" / queued.attempt_id / "creation.pending").exists():
                break
            time.sleep(0.1)
        assert containers
        assert not (tmp_path / "worker_jobs" / queued.attempt_id / "creation.pending").exists()
        process.kill()
        process.wait(timeout=5)
        worker = QueueWorker(tmp_path, backend.images)
        assert not worker.run_once()
        stored = store.job(queued.attempt_id)
        assert stored["state"] == "failed" and stored["error_code"] == "WORKER_RESTARTED"
        assert not backend.client.containers.list(all=True, filters={
            "label": f"ai-data-agent.attempt={queued.attempt_id}"})
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for container in backend.client.containers.list(all=True, filters={
                "label": f"ai-data-agent.attempt={queued.attempt_id}"}):
            container.remove(force=True)


def test_independent_watchdog_removes_container_after_worker_is_killed(tmp_path):
    backend, job, manifest = prepared(tmp_path, "while True:\n    pass\n")
    store = WorkflowStore(tmp_path)
    store.tasks.create(job.task_id, "demo", "Synthetic watchdog test", job.input_sha256, 15)
    store.start(job.task_id, "demo")
    sources = {name: str(backend.root / job.attempt_id / "input" / name)
               for name in ("data.csv", "spec.json", "clean.py")}
    queued = new_job(job.task_id, Stage.CLEAN_C, job.input_sha256,
                     spec_sha256=job.spec_sha256, code_sha256=job.code_sha256)
    store.schedule(queued, sources, "EXECUTING")
    environment = {
        key: value for key, value in os.environ.items()
        if key not in ("DEEPSEEK_API_KEY", "VULTR_API_KEY", "VULTR_SERVERLESS_INFERENCE_API_KEY")
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "workers.sandbox_manager.queue_worker",
         "--once", "--data-dir", str(tmp_path), "--images", str(manifest)],
        env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    filters = {"label": f"ai-data-agent.attempt={queued.attempt_id}"}
    try:
        deadline = time.monotonic() + 25
        running = False
        while time.monotonic() < deadline and process.poll() is None:
            containers = backend.client.containers.list(all=True, filters=filters)
            if containers:
                containers[0].reload()
                running = bool(containers[0].attrs.get("ExecIDs"))
                if running:
                    break
            time.sleep(0.1)
        assert running and (tmp_path / "worker_jobs" / queued.attempt_id / "watchdog.ready").exists()
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + LIMITS[Stage.CLEAN_C].timeout_seconds + 20
        while time.monotonic() < deadline:
            if not backend.client.containers.list(all=True, filters=filters):
                break
            time.sleep(0.5)
        assert not backend.client.containers.list(all=True, filters=filters)
        assert (tmp_path / "worker_jobs" / queued.attempt_id / "watchdog.timeout").exists()
        assert store.job(queued.attempt_id)["state"] == "running"
        QueueWorker(tmp_path, backend.images).recover_interrupted()
        assert store.job(queued.attempt_id)["error_code"] == "WORKER_RESTARTED"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for container in backend.client.containers.list(all=True, filters=filters):
            container.remove(force=True)
        directory = tmp_path / "worker_jobs" / queued.attempt_id
        if directory.exists():
            (directory / "watchdog.done").touch()
