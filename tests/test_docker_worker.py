"""Docker SDK 替身测试，不证明真实引擎或云端隔离已经通过验收。"""

import hashlib
import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace
from uuid import uuid4
import zipfile

import pytest

pytest.importorskip("docker", reason="Docker worker tests require the worker optional dependency")

from backend.domain.spec import freeze_spec
from backend.jobs.contracts import JobRequest, Stage
from backend.jobs.execution import ExecutionReceipt
from backend.jobs.policy import LIMITS
from workers.sandbox_manager.build_images import bundle_sources
from workers.sandbox_manager.docker_backend import DockerExecutionBackend, archive_file

IMAGE = "sha256:" + "a" * 64


def tar_payload(name, data=b"{}", kind=tarfile.REGTYPE):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.size = len(data) if kind == tarfile.REGTYPE else 0
        member.linkname = "/host-secret"
        archive.addfile(member, io.BytesIO(data) if member.isfile() else None)
    return stream.getvalue()


class Container:
    id = "fake-container"

    def __init__(self):
        self.files = {
            "/output/profile.json": b"{}",
            "/output/issues.json": b"[]",
            "/output/validation.json": b'{"verdict":"rejected"}',
            "/tmp/stdout.log": b"x" * 70000,
            "/tmp/stderr.log": b"warning",
        }
        self.attrs = {"State": {"Running": True, "OOMKilled": False}}
        self.removed = False
        self.paused = False
        self.cleanup_error = False

    def start(self):
        pass

    def reload(self):
        pass

    def pause(self):
        self.paused = True

    def kill(self):
        self.attrs["State"]["Running"] = False

    def remove(self, force):
        assert force
        self.removed = True
        if self.cleanup_error:
            raise RuntimeError("private daemon detail")

    def get_archive(self, path):
        return [tar_payload(Path(path).name, self.files[path])], {}


class Client:
    def __init__(self):
        self.container = Container()
        self.images = SimpleNamespace(get=lambda _: SimpleNamespace(id=IMAGE))
        self.containers = SimpleNamespace(create=self.create)
        self.api = SimpleNamespace(
            exec_create=self.exec_create, exec_start=self.exec_start,
            exec_inspect=lambda handle: {"Running": False, "ExitCode": 0}
            if handle == "collector" else self.progress,
        )
        self.progress = {"Running": False, "ExitCode": 0}
        self.callback = None
        self.created = []
        self.info_data = {
            "OSType": "linux", "SecurityOptions": ["name=seccomp,profile=builtin"],
            "MemoryLimit": True, "SwapLimit": True, "PidsLimit": True,
        }

    def info(self):
        return self.info_data

    def create(self, image, **kwargs):
        assert image == IMAGE
        self.created.append(kwargs)
        return self.container

    def exec_create(self, container_id, command, **kwargs):
        assert container_id == self.container.id
        if command[2] == "images.collector":
            assert kwargs == {"stdout": True, "stderr": False, "user": "10001:10001"}
            self.collector_stage = Stage(command[3])
            return {"Id": "collector"}
        assert command[:3] == ["python", "-m", "images.job_entry"]
        assert kwargs == {"stdout": False, "stderr": False, "user": "10001:10001"}
        return {"Id": "exec"}

    def exec_start(self, handle, detach=False, stream=False):
        if handle == "collector":
            from workers.sandbox_manager.docker_backend import OUTPUTS
            assert stream
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode="w") as archive:
                for name in (*OUTPUTS[self.collector_stage], "stdout.log", "stderr.log"):
                    path = ("/tmp/" if name.endswith(".log") else "/output/") + name
                    data = self.container.files[path]
                    if name.endswith(".log"):
                        data = data[:65536]
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
            return [payload.getvalue()]
        assert detach
        if self.callback:
            self.callback()


@pytest.fixture
def prepared(tmp_path):
    client = Client()
    backend = DockerExecutionBackend(tmp_path / "jobs", dict.fromkeys(Stage, IMAGE), client)
    source = tmp_path / "source.csv"
    source.write_bytes(b"id,value\n1,a\n")
    job = JobRequest(str(uuid4()), str(uuid4()), Stage.PROFILE_A,
                     hashlib.sha256(source.read_bytes()).hexdigest())
    backend.prepare(job, {"data.csv": source})
    return backend, client, job, source


def run(prepared):
    backend, _, job, _ = prepared
    receipt = backend.submit(job, LIMITS[job.stage])
    return backend.status(receipt)


def test_fixed_isolation_parameters_and_idempotence(prepared):
    backend, client, job, source = prepared
    status = run(prepared)
    assert status.state == "finished"
    assert client.container.paused and client.container.removed
    options = client.created[0]
    assert options["network_mode"] == "none" and options["network_disabled"]
    assert options["read_only"] and options["user"] == "10001:10001"
    assert options["cap_drop"] == ["ALL"]
    assert options["security_opt"] == ["no-new-privileges:true"]
    assert options["mem_limit"] == options["memswap_limit"] == "256m"
    assert options["pids_limit"] == LIMITS[job.stage].pids
    assert options["nano_cpus"] == 1_000_000_000
    assert options["ulimits"][0]["Name"] == "cpu"
    assert all(mount["mode"] == "ro" for mount in options["volumes"].values())
    assert set(options["tmpfs"]) == {"/tmp", "/output"}
    assert f"size={LIMITS[job.stage].output_bytes}" in options["tmpfs"]["/output"]
    assert options["log_config"]["Type"] == "none"
    assert (backend.root / job.attempt_id / "logs/stdout.log").stat().st_size == 65536
    assert backend.submit(job, LIMITS[job.stage]) == status.receipt
    assert len(client.created) == 1
    # 输入快照不随受信来源之后的变化而漂移。
    source.write_bytes(b"changed")
    assert (backend.root / job.attempt_id / "input/data.csv").read_bytes() != source.read_bytes()


@pytest.mark.parametrize("failure", ["missing", "nonzero", "oom", "cleanup", "write"])
def test_failures_never_return_success(prepared, monkeypatch, failure):
    backend, client, job, _ = prepared
    if failure == "missing":
        del client.container.files["/output/profile.json"]
    elif failure == "nonzero":
        client.progress["ExitCode"] = 1
    elif failure == "oom":
        client.container.attrs["State"] = {"Running": False, "OOMKilled": True}
    elif failure == "cleanup":
        client.container.cleanup_error = True
    else:
        original = Path.write_bytes

        def fail_result(path, data):
            if path.parent.name == "result":
                raise OSError("disk full")
            return original(path, data)

        monkeypatch.setattr(Path, "write_bytes", fail_result)
    status = run(prepared)
    assert status.state == "failed"
    assert client.container.removed
    manifest = json.loads(Path(status.result_ref).read_text())
    assert manifest["state"] == "failed"
    if failure == "oom":
        assert status.limit_reason == "oom"
    if failure == "cleanup":
        assert manifest["error_code"] == "CLEANUP_FAILED"


def test_timeout_removes_container(prepared, monkeypatch):
    _, client, _, _ = prepared
    client.progress["Running"] = True
    ticks = iter([0, 1000, 1001])
    monkeypatch.setattr("workers.sandbox_manager.docker_backend.time.monotonic",
                        lambda: next(ticks))
    status = run(prepared)
    assert status.state == "failed" and status.timed_out
    assert status.limit_reason == "wall_timeout"
    assert client.container.removed and not client.container.paused


def test_running_query_and_cancellation(prepared):
    backend, client, job, _ = prepared
    receipt = ExecutionReceipt(job.task_id, job.attempt_id, f"docker-{job.attempt_id}")

    def cancel_running():
        assert backend.status(receipt).state == "running"
        with pytest.raises(RuntimeError, match="执行中"):
            backend.submit(job, LIMITS[job.stage])
        backend.cancel(receipt)

    client.callback = cancel_running
    status = run(prepared)
    assert status.state == "cancelled"
    assert client.container.removed and not client.container.paused
    backend.cancel(receipt)
    assert backend.status(receipt) == status


def test_manifest_write_failure_leaves_failed_status(prepared, monkeypatch):
    backend, client, job, _ = prepared
    original = Path.write_text

    def fail_manifest(path, *args, **kwargs):
        if path.name == "execution.json":
            raise OSError("disk full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_manifest)
    with pytest.raises(OSError):
        run(prepared)
    receipt = ExecutionReceipt(job.task_id, job.attempt_id, f"docker-{job.attempt_id}")
    assert backend.status(receipt).state == "failed"
    assert client.container.removed


def test_invalid_inputs_do_not_create_attempt(prepared):
    backend, _, job, source = prepared
    wrong = JobRequest(job.task_id, str(uuid4()), Stage.PROFILE_A, "b" * 64)
    with pytest.raises(ValueError, match="哈希"):
        backend.prepare(wrong, {"data.csv": source})
    assert not (backend.root / wrong.attempt_id).exists()
    with pytest.raises(ValueError, match="文件集"):
        backend.prepare(wrong, {"unexpected.csv": source})
    with pytest.raises(ValueError, match="资源策略"):
        backend.submit(job, LIMITS[Stage.CLEAN_C])
    with pytest.raises(ValueError, match="归属"):
        backend.status(ExecutionReceipt(str(uuid4()), job.attempt_id, f"docker-{job.attempt_id}"))


def test_spec_code_and_candidate_bindings_and_failed_d_report(prepared, tmp_path):
    backend, client, a_job, source = prepared
    spec = freeze_spec(task_id=a_job.task_id, input_sha256=a_job.input_sha256,
                       owner_id="demo", task_version=1, columns=("id", "value"),
                       decisions=(), issues=())
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")
    code = tmp_path / "clean.py"
    code.write_bytes(b"print('test')\n")
    job = JobRequest(a_job.task_id, str(uuid4()), Stage.CLEAN_C, a_job.input_sha256,
                     spec.spec_sha256, "b" * 64)
    with pytest.raises(ValueError, match="源码或候选"):
        backend.prepare(job, {"data.csv": source, "spec.json": spec_path, "clean.py": code})
    job = JobRequest(a_job.task_id, str(uuid4()), Stage.VERIFY_D, a_job.input_sha256,
                     spec.spec_sha256, candidate_sha256="b" * 64)
    with pytest.raises(ValueError, match="源码或候选"):
        backend.prepare(job, {"data.csv": source, "spec.json": spec_path,
                              "candidate.csv": source})
    job = JobRequest(a_job.task_id, str(uuid4()), Stage.VERIFY_D, a_job.input_sha256,
                     "b" * 64, candidate_sha256=a_job.input_sha256)
    with pytest.raises(ValueError, match="规范"):
        backend.prepare(job, {"data.csv": source, "spec.json": spec_path,
                              "candidate.csv": source})
    job = JobRequest(a_job.task_id, str(uuid4()), Stage.VERIFY_D, a_job.input_sha256,
                     spec.spec_sha256, candidate_sha256=a_job.input_sha256)
    backend.prepare(job, {"data.csv": source, "spec.json": spec_path, "candidate.csv": source})
    client.progress["ExitCode"] = 1
    status = backend.status(backend.submit(job, LIMITS[job.stage]))
    assert status.state == "failed"
    assert (backend.root / job.attempt_id / "diagnostics/validation.json").exists()
    assert not (backend.root / job.attempt_id / "result").exists()


@pytest.mark.parametrize("capability", ["OSType", "SecurityOptions", "MemoryLimit",
                                       "SwapLimit", "PidsLimit"])
def test_engine_capabilities_required(tmp_path, capability):
    client = Client()
    client.info_data[capability] = False
    if capability == "SecurityOptions":
        client.info_data[capability] = []
    with pytest.raises(RuntimeError):
        DockerExecutionBackend(tmp_path, dict.fromkeys(Stage, IMAGE), client)


def test_drifting_image_tags_rejected(tmp_path):
    with pytest.raises(ValueError, match="不可变"):
        DockerExecutionBackend(tmp_path, dict.fromkeys(Stage, "image:latest"), Client())


@pytest.mark.parametrize("name,kind", [
    ("../cleaned.csv", tarfile.REGTYPE), ("/cleaned.csv", tarfile.REGTYPE),
    ("cleaned.csv", tarfile.SYMTYPE), ("cleaned.csv", tarfile.LNKTYPE),
    ("cleaned.csv", tarfile.DIRTYPE), ("wrong.csv", tarfile.REGTYPE),
])
def test_archive_rejects_paths_and_links(name, kind):
    container = SimpleNamespace(get_archive=lambda _: ([tar_payload(name, kind=kind)], {}))
    with pytest.raises(ValueError):
        archive_file(container, "/output/cleaned.csv", 100)


def test_archive_size_and_member_count():
    payload = tar_payload("cleaned.csv", b"abcd")
    container = SimpleNamespace(get_archive=lambda _: ([payload], {}))
    assert archive_file(container, "/output/cleaned.csv", 4) == b"abcd"
    with pytest.raises(ValueError):
        archive_file(container, "/output/cleaned.csv", 3)
    container = SimpleNamespace(get_archive=lambda _: ([b"x" * 65541], {}))
    with pytest.raises(ValueError, match="传输"):
        archive_file(container, "/output/cleaned.csv", 4)
    container = SimpleNamespace(get_archive=lambda _: ([b"\0" * 10240], {}))
    with pytest.raises(ValueError, match="单文件"):
        archive_file(container, "/output/cleaned.csv", 4)


def test_source_package_excludes_data_and_secrets(tmp_path):
    for name in ("backend", "workers", "images", "web", "deploy", ".runtime"):
        (tmp_path / name).mkdir()
    for name in ("pyproject.toml", "README.md", ".env.example"):
        (tmp_path / name).write_text("example")
    for name in ("backend/app.py", "deploy/Dockerfile.job", "web/index.html",
                 ".env", ".env.deepseek", ".runtime/private.csv", "backend/secret.env",
                 "backend/data.csv", "backend/private.json"):
        (tmp_path / name).write_text("example")
    target = tmp_path / "application.zip"
    bundle_sources(tmp_path, target, {"deployment_platform": "vultr", "tested_on_target": False})
    with zipfile.ZipFile(target) as archive:
        assert set(archive.namelist()) == {
            "pyproject.toml", "README.md", ".env.example", "backend/app.py",
            "deploy/Dockerfile.job", "web/index.html", "deployment.json",
        }
        assert not json.loads(archive.read("deployment.json"))["tested_on_target"]


def test_platform_packaging_records_architecture_without_cloud_claim(tmp_path, monkeypatch):
    import docker
    from workers.sandbox_manager import build_images

    calls = {}
    base = SimpleNamespace(attrs={"RepoDigests": ["python@sha256:" + "b" * 64]})
    image = SimpleNamespace(id=IMAGE, attrs={"Architecture": "arm64"},
                            save=lambda **_: [b"fake image archive"])

    def pull(name, **kwargs):
        calls["pull"] = (name, kwargs)
        return base

    def build(**kwargs):
        calls["build"] = kwargs
        return image, []

    client = SimpleNamespace(info=lambda: {"OSType": "linux"},
                             images=SimpleNamespace(pull=pull, build=build))
    monkeypatch.setattr(docker, "from_env", lambda **_: client)
    monkeypatch.setattr("sys.argv", ["build_images", "--target", "vultr", "--platform",
                                    "linux/arm64", "--output", str(tmp_path), "--export"])
    build_images.main()
    assert calls["pull"][1]["platform"] == "linux/arm64"
    assert calls["build"]["platform"] == "linux/arm64"
    manifest = json.loads((tmp_path / "images.json").read_text())
    assert manifest["architecture"] == "arm64"
    assert manifest["deployment_platform"] == "vultr"
    assert not manifest["tested_on_target"] and not manifest["execution_exposed_by_api"]
    assert (tmp_path / "job-image.tar").read_bytes() == b"fake image archive"
    with zipfile.ZipFile(tmp_path / "application.zip") as archive:
        assert "workers/sandbox_manager/docker_backend.py" in archive.namelist()
        assert not any(name.startswith(".runtime/") for name in archive.namelist())


def test_isolation_test_sources_compile_without_host_execution():
    from workers.sandbox_manager.isolation_test import BOUNDARIES, LOG_FLOOD, QUOTA, STRESS

    for code in (BOUNDARIES, LOG_FLOOD, QUOTA, *STRESS.values()):
        compile(code, "<container-only-test>", "exec")


@pytest.mark.parametrize("name,kind", [
    ("../cleaned.csv", tarfile.REGTYPE), ("cleaned.csv", tarfile.SYMTYPE),
    ("cleaned.csv", tarfile.LNKTYPE), ("cleaned.csv", tarfile.DIRTYPE),
    ("extra.csv", tarfile.REGTYPE),
])
def test_collector_transport_rejects_noncontract_files(name, kind):
    from workers.sandbox_manager.docker_backend import collect_snapshot
    client = SimpleNamespace(api=SimpleNamespace(
        exec_create=lambda *args, **kwargs: {"Id": "collector"},
        exec_start=lambda *args, **kwargs: [tar_payload(name, kind=kind)],
        exec_inspect=lambda _: {"Running": False, "ExitCode": 0},
    ))
    with pytest.raises(ValueError):
        collect_snapshot(client, SimpleNamespace(id="container"), Stage.CLEAN_C, LIMITS[Stage.CLEAN_C])
