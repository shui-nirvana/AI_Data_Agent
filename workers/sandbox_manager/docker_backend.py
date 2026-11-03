"""通用 Linux Docker 开发适配器。只在 worker 运行，不由 API 自动加载。

submit 同步占用独立 worker，而非 HTTP 请求。租约/重启对账尚未实现，
因此目前仅用于受控本地烟测；不能据此开放公网任意代码执行。
"""

import hashlib
import io
import json
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


def archive_file(container, path: str, max_bytes: int) -> bytes:
    """只接受指定文件的单一普通 tar 成员，不解压任意路径/链接到宿主。"""
    chunks, _ = container.get_archive(path)
    payload = bytearray()
    for chunk in chunks:
        payload.extend(chunk)
        if len(payload) > max_bytes + 65536:
            raise ValueError("容器文件传输超过配额")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        members = archive.getmembers()
        if len(members) != 1:
            raise ValueError("容器返回了非单文件产物")
        member = members[0]
        if (not member.isfile() or member.name != Path(path).name
                or member.size > max_bytes):
            raise ValueError("容器文件类型、名称或大小违反契约")
        stream = archive.extractfile(member)
        if stream is None:
            raise ValueError("容器产物不可读取")
        return stream.read(max_bytes + 1)


def collect_snapshot(client, container, stage: Stage, limits) -> dict[str, bytes]:
    """可信收集器先停止后台进程，再流式传回有界普通文件 tar。"""
    execution = client.api.exec_create(
        container.id, ["python", "-m", "images.collector", stage.value],
        stdout=True, stderr=False, user="10001:10001",
    )["Id"]
    chunks = client.api.exec_start(execution, stream=True)
    payload = bytearray()
    for chunk in chunks:
        payload.extend(chunk)
        if len(payload) > limits.output_bytes + 2 * 65536 + 65536:
            raise ValueError("收集传输超过配额")
    result = client.api.exec_inspect(execution)
    if result["Running"] or result["ExitCode"] != 0:
        raise ValueError("可信收集器失败")
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
                raise ValueError("收集文件不合约")
            files[member.name] = archive.extractfile(member).read(quota + 1)
    if sum(
        len(files[name]) for name in OUTPUTS[stage] if name in files
    ) > limits.output_bytes:
        raise ValueError("产物总大小超限")
    return files


class DockerExecutionBackend:
    """固定镜像/入口/限额，输出 tmpfs 硬容量上限，无宿主可写挂载。"""

    def __init__(self, root: Path, images: dict[Stage, str], client=None):
        if set(images) != set(Stage) or any(
            not re.fullmatch(r"sha256:[0-9a-f]{64}", image) for image in images.values()
        ):
            raise ValueError("A/C/D 必须使用本地不可变 Docker 镜像 ID")
        if client is None:
            import docker
            client = docker.from_env(timeout=15)
        self.client, self.images = client, dict(images)
        info = client.info()
        if info.get("OSType") != "linux" or not any(
            "seccomp" in item for item in info.get("SecurityOptions", [])
        ):
            raise RuntimeError("需要启用 seccomp 的 Linux Docker 引擎")
        if not all(info.get(key) for key in ("MemoryLimit", "SwapLimit", "PidsLimit")):
            raise RuntimeError("Docker 引擎缺少内存/Swap/PID 限额能力")
        # 不自动 pull，不在执行阶段解析漂移标签。
        for image in set(images.values()):
            if client.images.get(image).id != image:
                raise ValueError("本地镜像 ID 不匹配")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._prepared = {}
        self._results = {}
        self._active = {}
        self._cancelled = set()
        self._lock = threading.Lock()

    def prepare(self, job: JobRequest, sources: dict[str, Path]) -> None:
        """受信 worker 复制输入快照。来源路径不得来自 HTTP/LLM 参数。"""
        if set(sources) != set(INPUTS[job.stage]):
            raise ValueError("作业输入文件集不匹配")
        directory = self.root / job.attempt_id / "input"
        content = {}
        for name, source in sources.items():
            if source.is_symlink() or not source.is_file():
                raise ValueError("作业来源必须是普通文件")
            with source.open("rb") as stream:
                data = stream.read(INPUT_LIMITS[name] + 1)
            if len(data) > INPUT_LIMITS[name]:
                raise ValueError("作业输入超过配额")
            content[name] = data
        if hashlib.sha256(content["data.csv"]).hexdigest() != job.input_sha256:
            raise ValueError("输入快照哈希不匹配")
        if job.stage != Stage.PROFILE_A:
            spec = FrozenSpec.model_validate_json(content["spec.json"])
            if (spec.spec_sha256 != job.spec_sha256 or str(spec.task_id) != job.task_id
                    or spec.input_sha256 != job.input_sha256):
                raise ValueError("规范与作业不匹配")
        for name, digest in (("clean.py", job.code_sha256),
                             ("candidate.csv", job.candidate_sha256)):
            if name in content and hashlib.sha256(content[name]).hexdigest() != digest:
                raise ValueError("源码或候选哈希不匹配")
        directory.mkdir(parents=True, exist_ok=False)
        try:
            for name, data in content.items():
                path = directory / name
                path.write_bytes(data)
                path.chmod(0o444)
            directory.chmod(0o555)
        except Exception:
            # 只清理本次创建的目录，不删除已有尝试或用户来源文件。
            directory.chmod(0o700)
            for path in directory.iterdir():
                path.chmod(0o600)
            shutil.rmtree(directory)
            raise
        self._prepared[job.attempt_id] = job

    def submit(self, job: JobRequest, limits) -> ExecutionReceipt:
        """本地镜像运行一次；完成后删除容器，重复已完成投递返回同一回执。"""
        if limits != LIMITS[job.stage] or self._prepared.get(job.attempt_id) != job:
            raise ValueError("作业未准备或资源策略不匹配")
        receipt = ExecutionReceipt(job.task_id, job.attempt_id, f"docker-{job.attempt_id}")
        with self._lock:
            if receipt.handle in self._results:
                return receipt
            if receipt.handle in self._active:
                raise RuntimeError("该尝试已经执行中")
            self._active[receipt.handle] = None
        container = None
        started = time.monotonic()
        exit_code, timed_out, limit_reason = None, False, None
        collected, artifact_hashes = {}, {}
        state = "failed"
        error_code = None
        runtime_category = None
        try:
            import docker
            container = self.client.containers.create(
                self.images[job.stage],
                command=["python", "-c", "import time; time.sleep(86400)"],
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
                name=f"ai-data-agent-{job.attempt_id}",
            )
            with self._lock:
                self._active[receipt.handle] = container
            container.start()
            with self._lock:
                if receipt.handle in self._cancelled:
                    raise RuntimeError("作业已取消")
            execution = self.client.api.exec_create(
                container.id, ["python", "-m", "images.job_entry", job.stage.value],
                stdout=False, stderr=False, user="10001:10001",
            )["Id"]
            self.client.api.exec_start(execution, detach=True)
            while True:
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
                time.sleep(0.1)
            container.reload()
            if container.attrs["State"].get("OOMKilled"):
                limit_reason = "oom"
            with self._lock:
                cancelled = receipt.handle in self._cancelled
            if not timed_out and not cancelled and container.attrs["State"].get("Running"):
                snapshot = collect_snapshot(self.client, container, job.stage, limits)
                # 收集器已停止遗留子进程，收集完成后冻结整个容器再落盘/回收。
                container.pause()
                logs = self.root / job.attempt_id / "logs"
                logs.mkdir(exist_ok=True)
                for name in ("stdout.log", "stderr.log"):
                    if name in snapshot:
                        (logs / name).write_bytes(snapshot[name])
                if "runtime.json" in snapshot:
                    runtime_category = json.loads(snapshot["runtime.json"]).get("category")
                    if runtime_category not in ("completed", "runtime_error", "security_violation",
                                                "restricted_or_input_error", "unknown_error"):
                        raise ValueError("无效运行诊断类别")
                collected = {name: snapshot[name] for name in OUTPUTS[job.stage] if name in snapshot}
            if exit_code == 0 and not timed_out and limit_reason is None:
                if set(collected) == set(OUTPUTS[job.stage]):
                    state = "finished"
            if job.stage == Stage.CLEAN_C and state == "finished" and runtime_category != "completed":
                state = "failed"
            # 非零退出的 D 报告也保留为诊断，但失败文件绝不成为成功候选。
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
            error_code = type(exc).__name__  # 不泄露 Docker 原始异常/用户内容。
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    state, error_code = "failed", "CLEANUP_FAILED"
            with self._lock:
                if receipt.handle in self._cancelled and error_code != "CLEANUP_FAILED":
                    state = "cancelled"
                self._active[receipt.handle] = None
        manifest = {
            "task_id": job.task_id, "attempt_id": job.attempt_id, "stage": job.stage.value,
            "input_sha256": job.input_sha256, "spec_sha256": job.spec_sha256,
            "code_sha256": job.code_sha256, "candidate_sha256": job.candidate_sha256,
            "image_id": self.images[job.stage], "exit_code": exit_code,
            "timed_out": timed_out, "limit_reason": limit_reason, "state": state,
            "elapsed_seconds": time.monotonic() - started, "artifact_hashes": artifact_hashes,
            "error_code": error_code,
            "runtime_category": runtime_category,
        }
        manifest_path = self.root / job.attempt_id / "execution.json"
        # 结果持久化失败不能返回成功；活跃标记持续到状态提交，防止重复投递。
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
        raise ValueError("作业尚未提交")

    def _check_receipt(self, receipt: ExecutionReceipt) -> None:
        if receipt.handle != f"docker-{receipt.attempt_id}":
            raise ValueError("非法回执")
        job = self._prepared.get(receipt.attempt_id)
        if job is None or job.task_id != receipt.task_id:
            raise ValueError("回执与作业归属不匹配")

    def cancel(self, receipt: ExecutionReceipt) -> None:
        # 本地烟测阶段只支持本进程的取消；持久化跨进程取消由后续租约实现。
        self._check_receipt(receipt)
        with self._lock:
            container = self._active.get(receipt.handle)
            if receipt.handle in self._active:
                self._cancelled.add(receipt.handle)
        if container is not None:
            try:
                container.kill()
            except Exception:
                # 完成/删除与取消可能竞争；submit 的 finally 仍负责强制回收。
                with self._lock:
                    if receipt.handle in self._active:
                        raise
