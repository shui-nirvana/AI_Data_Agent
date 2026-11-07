"""Generic Linux Docker adapter, loaded only by workers and never automatically by the API."""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import threading
import time

from backend.domain.spec import FrozenSpec
from backend.jobs.contracts import JobRequest, Stage
from backend.jobs.execution import ExecutionReceipt, ExecutionStatus
from backend.jobs.policy import LIMITS
from backend.llm.diagnostics import source_diagnostic
from workers.sandbox_manager.watchdog import Watchdog

INPUTS = {
    Stage.PROFILE_A: ("data.csv",),
    Stage.CLEAN_C: ("data.csv", "spec.json", "clean.py"),
    Stage.VERIFY_D: ("data.csv", "spec.json", "candidate.csv"),
}
OUTPUTS = {
    Stage.PROFILE_A: ("profile.json", "issues.json"),
    Stage.CLEAN_C: ("cleaned.csv",),
    Stage.VERIFY_D: ("validation.json",),
}
INPUT_LIMITS = {"data.csv": 10485760, "spec.json": 262144,
                "clean.py": 262144, "candidate.csv": 12000000}


class UnreclaimedSandbox(RuntimeError):
    """Container creation or cleanup is uncertain; retain the job and stop the worker."""


def archive_file(container, path: str, max_bytes: int) -> bytes:
    """Accept exactly one named regular tar member; never extract arbitrary paths or links onto the host."""
    chunks, _ = container.get_archive(path)
    payload = bytearray()
    for chunk in chunks:
        payload.extend(chunk)
        if len(payload) > max_bytes + 65536:
            raise ValueError("Container file transfer exceeds the size limit")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        members = archive.getmembers()
        if len(members) != 1:
            raise ValueError("Container returned an artifact that is not a single file")
        member = members[0]
        if (not member.isfile() or member.name != Path(path).name
                or member.size > max_bytes):
            raise ValueError("Container file type, name, or size violates the contract")
        stream = archive.extractfile(member)
        if stream is None:
            raise ValueError("Container artifact cannot be read")
        return stream.read(max_bytes + 1)


def collect_snapshot(client, container, stage: Stage, limits) -> dict[str, bytes]:
    """The trusted collector stops background processes before streaming a bounded tar of regular files."""
    execution = client.api.exec_create(
        container.id, ["python", "-m", "images.collector", stage.value],
        stdout=True, stderr=False, user="10001:10001",
    )["Id"]
    chunks = client.api.exec_start(execution, stream=True)
    payload = bytearray()
    for chunk in chunks:
        payload.extend(chunk)
        if len(payload) > limits.output_bytes + 2 * 65536 + 65536:
            raise ValueError("Collector transfer exceeds the size limit")
    result = client.api.exec_inspect(execution)
    if result["Running"] or result["ExitCode"] != 0:
        raise ValueError("Trusted collector failed")
    allowed = set(OUTPUTS[stage]) | {"stdout.log", "stderr.log"}
    if stage == Stage.CLEAN_C:
        allowed.add("runtime.json")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        for member in archive.getmembers():
            quota = 4096 if member.name == "runtime.json" else (
                65536 if member.name.endswith(".log") else limits.output_bytes
            )
            if (member.name not in allowed or member.name in files or not member.isfile()
                    or member.size > quota):
                raise ValueError("Collected file violates the contract")
            files[member.name] = archive.extractfile(member).read(quota + 1)
    if sum(
        len(files[name]) for name in OUTPUTS[stage] if name in files
    ) > limits.output_bytes:
        raise ValueError("Total artifact size exceeds the limit")
    return files


class DockerExecutionBackend:
    """Use fixed images, entrypoints, and limits, with a hard output tmpfs cap and no writable host mounts."""

    def __init__(self, root: Path, images: dict[Stage, str], client=None, watchdog_factory=Watchdog):
        if set(images) != set(Stage) or any(
            not re.fullmatch(r"sha256:[0-9a-f]{64}", image) for image in images.values()
        ):
            raise ValueError("A/C/D must use local immutable Docker image IDs")
        if client is None:
            import docker
            client = docker.from_env(timeout=15)
        self.client, self.images = client, dict(images)
        self.watchdog_factory = watchdog_factory
        info = client.info()
        if info.get("OSType") != "linux" or not any(
            "seccomp" in item for item in info.get("SecurityOptions", [])
        ):
            raise RuntimeError("A Linux Docker engine with seccomp enabled is required")
        if not all(info.get(key) for key in ("MemoryLimit", "SwapLimit", "PidsLimit")):
            raise RuntimeError("Docker engine lacks memory, swap, or PID limit support")
        # Do not pull automatically or resolve mutable tags during execution.
        for image in set(images.values()):
            if client.images.get(image).id != image:
                raise ValueError("Local image ID does not match")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._prepared = {}
        self._clean_snapshot_hashes = {}
        self._results = {}
        self._active = {}
        self._cancelled = set()
        self._lock = threading.Lock()

    def prepare(self, job: JobRequest, sources: dict[str, Path]) -> None:
        """Copy input snapshots in a trusted worker; source paths must not come from HTTP or model parameters."""
        if set(sources) != set(INPUTS[job.stage]):
            raise ValueError("Job input file set does not match")
        directory = self.root / job.attempt_id / "input"
        content = {}
        for name, source in sources.items():
            if source.is_symlink() or not source.is_file():
                raise ValueError("Job source must be a regular file")
            with source.open("rb") as stream:
                data = stream.read(INPUT_LIMITS[name] + 1)
            if len(data) > INPUT_LIMITS[name]:
                raise ValueError("Job input exceeds the size limit")
            content[name] = data
        if hashlib.sha256(content["data.csv"]).hexdigest() != job.input_sha256:
            raise ValueError("Input snapshot hash does not match")
        if job.stage != Stage.PROFILE_A:
            spec = FrozenSpec.model_validate_json(content["spec.json"])
            if (spec.spec_sha256 != job.spec_sha256 or str(spec.task_id) != job.task_id
                    or spec.input_sha256 != job.input_sha256):
                raise ValueError("Specification does not match the job")
        if ("candidate.csv" in content
                and hashlib.sha256(content["candidate.csv"]).hexdigest() != job.candidate_sha256):
            raise ValueError("Candidate hash does not match")
        directory.mkdir(parents=True, exist_ok=False)
        try:
            for name, data in content.items():
                path = directory / name
                path.write_bytes(data)
                path.chmod(0o444)
            directory.chmod(0o555)
        except Exception:
            # Clean only directories created by this attempt, never prior attempts or user source files.
            directory.chmod(0o700)
            for path in directory.iterdir():
                path.chmod(0o600)
            shutil.rmtree(directory)
            raise
        if "clean.py" in content:
            self._clean_snapshot_hashes[job.attempt_id] = hashlib.sha256(content["clean.py"]).hexdigest()
        self._prepared[job.attempt_id] = job

    def _checked_clean_snapshot(self, job: JobRequest) -> bytes:
        path = self.root / job.attempt_id / "input" / "clean.py"
        if path.is_symlink() or not path.is_file():
            raise ValueError("Executed source snapshot is not a regular file")
        with path.open("rb") as stream:
            code = stream.read(INPUT_LIMITS["clean.py"] + 1)
        if (len(code) > INPUT_LIMITS["clean.py"]
                or hashlib.sha256(code).hexdigest() != self._clean_snapshot_hashes.get(job.attempt_id)):
            raise ValueError("Executed source snapshot hash does not match")
        return code

    def submit(self, job: JobRequest, limits) -> ExecutionReceipt:
        """Run a local image once and remove its container; a repeated completed submission returns the same receipt."""
        if limits != LIMITS[job.stage] or self._prepared.get(job.attempt_id) != job:
            raise ValueError("Job is not prepared or its resource policy does not match")
        receipt = ExecutionReceipt(job.task_id, job.attempt_id, f"docker-{job.attempt_id}")
        with self._lock:
            if receipt.handle in self._results:
                return receipt
            if receipt.handle in self._active:
                raise RuntimeError("This attempt is already running")
            self._active[receipt.handle] = None
        container = None
        started = time.monotonic()
        deadline = started + limits.timeout_seconds
        directory = self.root / job.attempt_id
        pending = directory / "creation.pending"
        watchdog = None
        dispatched = False
        creation_confirmed = False
        unreclaimed = False
        exit_code, timed_out, limit_reason = None, False, None
        collected, artifact_hashes = {}, {}
        state = "failed"
        error_code = None
        runtime_category, runtime_diagnostic = None, None
        executed_source = None
        try:
            import docker
            if job.stage == Stage.CLEAN_C:
                self._checked_clean_snapshot(job)
            watchdog = self.watchdog_factory(directory, job.task_id, job.attempt_id, deadline)
            # A durable marker is written before issuing a potentially ambiguous API call.
            with pending.open("x", encoding="ascii") as marker:
                marker.write("creation dispatched or interrupted\n")
                marker.flush()
                os.fsync(marker.fileno())
            if not watchdog.alive() or time.monotonic() >= deadline:
                pending.unlink()
                raise RuntimeError("Sandbox watchdog is not running or deadline expired")
            dispatched = True
            container = self.client.containers.create(
                self.images[job.stage],
                command=["python", "-c", f"import time; time.sleep({limits.timeout_seconds})"],
                entrypoint=[], working_dir="/app", user="10001:10001",
                network_mode="none", network_disabled=True, read_only=True,
                cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
                mem_limit=f"{limits.memory_mb}m", memswap_limit=f"{limits.memory_mb}m",
                nano_cpus=1_000_000_000, pids_limit=limits.pids,
                ulimits=[docker.types.Ulimit(name="cpu", soft=limits.cpu_seconds,
                                             hard=limits.cpu_seconds)],
                volumes={str(self.root / job.attempt_id / "input"):
                         {"bind": "/input", "mode": "ro"}},
                tmpfs={"/output": f"rw,noexec,nosuid,nodev,size={limits.output_bytes},uid=10001,gid=10001,mode=0700",
                       "/tmp": "rw,noexec,nosuid,nodev,size=1048576,uid=10001,gid=10001,mode=0700"},
                environment={"AI_AGENT_SANDBOX_JOB": job.stage.value},
                labels={"ai-data-agent.attempt": job.attempt_id, "ai-data-agent.task": job.task_id},
                log_config=docker.types.LogConfig(type="none"),
                restart_policy={"Name": "no"},
                name=f"ai-data-agent-{job.attempt_id}",
            )
            pending.unlink()
            creation_confirmed = True
            with self._lock:
                self._active[receipt.handle] = container
            if not watchdog.alive() or time.monotonic() >= deadline:
                raise RuntimeError("Sandbox watchdog is not running or deadline expired")
            container.start()
            with self._lock:
                if receipt.handle in self._cancelled:
                    raise RuntimeError("Job was cancelled")
            execution = self.client.api.exec_create(
                container.id, ["python", "-m", "images.job_entry", job.stage.value],
                stdout=False, stderr=False, user="10001:10001",
            )["Id"]
            self.client.api.exec_start(execution, detach=True)
            last_cpu_check = 0.0
            while True:
                if not watchdog.alive():
                    raise RuntimeError("Sandbox watchdog exited")
                progress = self.client.api.exec_inspect(execution)
                if not progress["Running"]:
                    exit_code = progress["ExitCode"]
                    break
                with self._lock:
                    cancelled = receipt.handle in self._cancelled
                if cancelled:
                    break
                if time.monotonic() - started > limits.timeout_seconds:
                    timed_out, limit_reason = True, "wall_timeout"
                    break
                if time.monotonic() - last_cpu_check >= 0.5:
                    # cgroup accounting includes all descendants, unlike per-process rlimit.
                    usage = container.stats(stream=False)["cpu_stats"]["cpu_usage"]["total_usage"]
                    if type(usage) is not int or usage < 0:
                        raise ValueError("CPU accounting unavailable")
                    last_cpu_check = time.monotonic()
                    if usage >= limits.cpu_seconds * 1_000_000_000:
                        limit_reason = "cpu_budget"
                        break
                time.sleep(0.1)
            container.reload()
            if container.attrs["State"].get("OOMKilled"):
                limit_reason = "oom"
            with self._lock:
                cancelled = receipt.handle in self._cancelled
            if not timed_out and limit_reason is None and not cancelled and container.attrs["State"].get("Running"):
                snapshot = collect_snapshot(self.client, container, job.stage, limits)
                # After the collector stops leftover subprocesses and finishes collection, pause the container before persisting or reclaiming it.
                container.pause()
                executed_source = self._checked_clean_snapshot(job) if job.stage == Stage.CLEAN_C else None
                logs = self.root / job.attempt_id / "logs"
                logs.mkdir(exist_ok=True)
                for name in ("stdout.log", "stderr.log"):
                    if name in snapshot:
                        (logs / name).write_bytes(snapshot[name])
                if "runtime.json" in snapshot:
                    runtime = json.loads(snapshot["runtime.json"])
                    runtime_category = runtime.get("category")
                    if runtime_category not in ("completed", "runtime_error", "security_violation",
                                                "restricted_or_input_error", "unknown_error"):
                        raise ValueError("Invalid runtime diagnostic category")
                    if (job.stage == Stage.CLEAN_C and runtime_category == "runtime_error"
                            and exit_code == 1):
                        try:
                            source = executed_source.decode("utf-8")
                        except UnicodeDecodeError:
                            source = ""
                        runtime_diagnostic = source_diagnostic(runtime.get("diagnostic"), source)
                collected = {name: snapshot[name] for name in OUTPUTS[job.stage] if name in snapshot}
            if exit_code == 0 and not timed_out and limit_reason is None:
                if set(collected) == set(OUTPUTS[job.stage]):
                    state = "finished"
            if job.stage == Stage.CLEAN_C and state == "finished" and runtime_category != "completed":
                state = "failed"
            # Retain a D report from a nonzero exit for diagnostics only; never publish it as a successful candidate.
            if collected:
                target = self.root / job.attempt_id / (
                    "result" if state == "finished" else "diagnostics"
                )
                target.mkdir(exist_ok=False)
                for name, data in collected.items():
                    (target / name).write_bytes(data)
                    artifact_hashes[name] = hashlib.sha256(data).hexdigest()
        except Exception as exc:
            state = "failed"
            error_code = type(exc).__name__  # Do not expose raw Docker errors or user content.
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except docker.errors.NotFound:
                    pass
                except Exception:
                    unreclaimed = True
            if (directory / "watchdog.timeout").exists() or time.monotonic() >= deadline:
                state, timed_out, limit_reason = "failed", True, "wall_timeout"
            if dispatched and not creation_confirmed:
                unreclaimed = True
            if watchdog is not None and not watchdog.alive():
                state, error_code = "failed", "WATCHDOG_FAILED"
            if watchdog is not None and not unreclaimed:
                watchdog.complete()
            with self._lock:
                if receipt.handle in self._cancelled and not unreclaimed:
                    state = "cancelled"
                self._active[receipt.handle] = None
        if unreclaimed:
            raise UnreclaimedSandbox(f"Container creation or cleanup for job {job.attempt_id} is unconfirmed; the job is retained for reconciliation")
        manifest = {
            "task_id": job.task_id, "attempt_id": job.attempt_id, "stage": job.stage.value,
            "input_sha256": job.input_sha256, "spec_sha256": job.spec_sha256,
            "code_sha256": job.code_sha256, "candidate_sha256": job.candidate_sha256,
            "image_id": self.images[job.stage], "exit_code": exit_code,
            "timed_out": timed_out, "limit_reason": limit_reason, "state": state,
            "elapsed_seconds": time.monotonic() - started, "artifact_hashes": artifact_hashes,
            "error_code": error_code,
            "runtime_category": runtime_category,
            "runtime_diagnostic": runtime_diagnostic,
            "executed_source_ref": (
                str(self.root / job.attempt_id / "input" / "clean.py")
                if executed_source is not None and error_code is None else None
            ),
            "executed_source_sha256": (
                hashlib.sha256(executed_source).hexdigest()
                if executed_source is not None and error_code is None else None
            ),
        }
        manifest_path = self.root / job.attempt_id / "execution.json"
        # Persistence failure must not return success; retain the active marker until status is committed to prevent duplicate dispatch.
        try:
            with self._lock:
                if receipt.handle in self._cancelled and error_code != "CLEANUP_FAILED":
                    state = "cancelled"
                manifest["state"] = state
                manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                self._results[receipt.handle] = ExecutionStatus(
                    receipt, state, exit_code, timed_out, limit_reason, str(manifest_path)
                )
                self._active.pop(receipt.handle, None)
        except Exception:
            with self._lock:
                self._results[receipt.handle] = ExecutionStatus(
                    receipt, "failed", exit_code, timed_out, limit_reason
                )
                self._active.pop(receipt.handle, None)
            raise
        return receipt

    def status(self, receipt: ExecutionReceipt) -> ExecutionStatus:
        self._check_receipt(receipt)
        with self._lock:
            result = self._results.get(receipt.handle)
            if result is not None:
                return result
            if receipt.handle in self._active:
                return ExecutionStatus(receipt, "running")
        raise ValueError("Job has not been submitted")

    def _check_receipt(self, receipt: ExecutionReceipt) -> None:
        if receipt.handle != f"docker-{receipt.attempt_id}":
            raise ValueError("Invalid receipt")
        job = self._prepared.get(receipt.attempt_id)
        if job is None or job.task_id != receipt.task_id:
            raise ValueError("Receipt does not match the job")

    def cancel(self, receipt: ExecutionReceipt) -> None:
        # This adapter supports cancellation within its process; durable cross-process cancellation is handled by the queue worker.
        self._check_receipt(receipt)
        with self._lock:
            container = self._active.get(receipt.handle)
            if receipt.handle in self._active:
                self._cancelled.add(receipt.handle)
        if container is not None:
            try:
                container.kill()
            except Exception:
                # Completion/removal can race with cancellation; submit still reclaims in finally.
                with self._lock:
                    if receipt.handle in self._active:
                        raise
