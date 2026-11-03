"""独立 Docker 队列 worker。只有本进程持有运行时权限；不运行 LangGraph。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import threading
import time

from backend.domain.models import TaskState
from backend.jobs.contracts import JobRequest, Stage
from backend.jobs.execution import ExecutionReceipt
from backend.jobs.policy import LIMITS
from backend.orchestration.contracts import read_json
from backend.orchestration.store import WorkflowStore, process_lock
from workers.sandbox_manager.docker_backend import DockerExecutionBackend


class UnreclaimedSandbox(RuntimeError):
    """容器回收未证实成功，必须停止 worker，不能继续消费下一作业。"""


class QueueWorker:
    def __init__(self, root: Path, images: dict[Stage, str], backend=None):
        self.store = WorkflowStore(root)
        self.backend = backend if backend is not None else DockerExecutionBackend(
            self.store.root / "worker_jobs", images
        )

    def recover_interrupted(self):
        """单 worker 文件锁内，旧 running 作业先回收后失败，不重新执行旧 C。"""
        with self.store.db() as db:
            rows = db.execute("SELECT job_id FROM workflow_jobs WHERE state='running'").fetchall()
        for row in rows:
            containers = self.backend.client.containers.list(
                all=True, filters={"label": f"ai-data-agent.attempt={row['job_id']}"}
            )
            for container in containers:
                container.remove(force=True)
            self.store.finish_job(row["job_id"], "failed", None, "WORKER_RESTARTED")

    def run_once(self) -> bool:
        with process_lock(self.store.root, "docker-worker"):
            self.recover_interrupted()
            stored = self.store.claim_job()
            if stored is None:
                return False
            payload = json.loads(stored["request_json"])
            payload["stage"] = Stage(payload["stage"])
            job = JobRequest(**payload)
            receipt = ExecutionReceipt(job.task_id, job.attempt_id, f"docker-{job.attempt_id}")
            stop = threading.Event()

            def observe_cancellation():
                while not stop.wait(0.2):
                    if self.store.task(job.task_id)["state"] == TaskState.CANCELLED:
                        try:
                            self.backend.cancel(receipt)
                        except Exception:
                            # submit 的 finally 仍需回收；回报事务拒绝取消后的成功。
                            pass

            watcher = None
            try:
                sources = {name: Path(ref).resolve()
                           for name, ref in json.loads(stored["sources_json"]).items()}
                if any(not path.is_relative_to(self.store.root) for path in sources.values()):
                    raise ValueError("作业来源不在私有目录")
                if self.store.task(job.task_id)["state"] == TaskState.CANCELLED:
                    self.store.finish_job(job.attempt_id, "cancelled", None, None)
                    return True
                self.backend.prepare(job, sources)
                watcher = threading.Thread(target=observe_cancellation, daemon=True)
                watcher.start()
                receipt = self.backend.submit(job, LIMITS[job.stage])
                status = self.backend.status(receipt)
                if status.result_ref and read_json(Path(status.result_ref)).get("error_code") == "CLEANUP_FAILED":
                    raise UnreclaimedSandbox("容器未回收，作业保留 running 供重启对账")
                evidence = None
                error_code = None if status.state == "finished" else "SANDBOX_FAILED"
                if status.result_ref:
                    manifest = read_json(Path(status.result_ref))
                    if (manifest.get("task_id") != job.task_id or manifest.get("attempt_id") != job.attempt_id
                            or manifest.get("code_sha256") != job.code_sha256
                            or manifest.get("input_sha256") != job.input_sha256
                            or manifest.get("spec_sha256") != job.spec_sha256
                            or manifest.get("candidate_sha256") != job.candidate_sha256
                            or manifest.get("stage") != job.stage.value):
                        raise ValueError("运行回报归属不匹配")
                    category = manifest.get("runtime_category")
                    # 默认不修复。仅 C 的明确运行故障/正常退出缺文件可进入新源码分支。
                    if (job.stage == Stage.CLEAN_C and status.state == "failed"
                            and not manifest.get("timed_out") and manifest.get("limit_reason") is None
                            and manifest.get("error_code") is None):
                        if category == "runtime_error" and manifest.get("exit_code") == 1:
                            error_code = "RUNTIME_ERROR"
                        elif category == "completed" and manifest.get("exit_code") == 0:
                            error_code = "MISSING_OUTPUT"
                    evidence = {
                        "task_id": job.task_id, "attempt_id": job.attempt_id,
                        "code_sha256": job.code_sha256, "input_sha256": job.input_sha256,
                        "spec_sha256": job.spec_sha256, "exit_code": manifest.get("exit_code"),
                        "timed_out": manifest.get("timed_out"),
                        "limit_reason": manifest.get("limit_reason"),
                        "runtime_category": category,
                        "image_id": manifest.get("image_id"),
                        "elapsed_seconds": manifest.get("elapsed_seconds"),
                    }
                    if (job.stage == Stage.VERIFY_D and status.state == "failed"
                            and manifest.get("exit_code") == 1
                            and manifest.get("limit_reason") is None
                            and not manifest.get("timed_out") and manifest.get("error_code") is None):
                        path = self.backend.root / job.attempt_id / "diagnostics" / "validation.json"
                        if path.is_file() and path.stat().st_size <= 1_000_000:
                            data = path.read_bytes()
                            report = json.loads(data)
                            if (hashlib.sha256(data).hexdigest() == manifest.get("artifact_hashes", {}).get("validation.json")
                                    and report.get("verdict") == "rejected"
                                    and report.get("spec_sha256") == job.spec_sha256
                                    and report.get("output_sha256") == job.candidate_sha256):
                                error_code = "VALIDATION_REJECTED"
                                evidence["validation"] = {k: report.get(k) for k in (
                                    "schema_version", "validator_version", "verdict",
                                    "input_sha256", "spec_sha256", "output_sha256",
                                    "changed_cells", "mismatch_count", "checks")}
                result_dir = str(self.backend.root / job.attempt_id / "result") if status.state == "finished" else None
                self.store.finish_job(job.attempt_id, status.state, result_dir,
                                      error_code, evidence)
            except UnreclaimedSandbox:
                raise
            except Exception as exc:
                current = self.store.job(job.attempt_id)
                if current["state"] == "running":
                    self.store.finish_job(job.attempt_id, "failed", None, type(exc).__name__)
            finally:
                stop.set()
                if watcher is not None:
                    watcher.join(timeout=1)
            return True


def main():
    parser = argparse.ArgumentParser(description="独立 Docker 持久作业队列 worker")
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("AI_AGENT_DATA_DIR", ".runtime")))
    parser.add_argument("--images", type=Path, default=Path(".runtime/docker/images.json"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--recover-only", action="store_true", help="Reclaim old attempts without consuming jobs")
    args = parser.parse_args()
    configuration = read_json(args.images)
    worker = QueueWorker(args.data_dir,
                         {Stage(key): value for key, value in configuration["images"].items()})
    if args.recover_only:
        with process_lock(worker.store.root, "docker-worker"):
            worker.recover_interrupted()
        return
    if args.once:
        print(f"job processed={worker.run_once()}")
        return
    try:
        from backend.local_runtime import heartbeat
        while True:
            heartbeat(worker.store.root, "worker")
            if not worker.run_once():
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
