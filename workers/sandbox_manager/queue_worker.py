"""Independent Docker queue worker with runtime privileges; it does not run LangGraph."""

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
from backend.llm.diagnostics import verified_diagnostic
from backend.llm.generator import check_source
from backend.orchestration.contracts import read_json
from backend.orchestration.store import WorkflowStore, process_lock
from workers.sandbox_manager.docker_backend import DockerExecutionBackend, UnreclaimedSandbox
from workers.sandbox_manager.watchdog import reclaim


def marker_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


class QueueWorker:
    def __init__(self, root: Path, images: dict[Stage, str], backend=None):
        self.store = WorkflowStore(root)
        self.backend = backend if backend is not None else DockerExecutionBackend(
            self.store.root / "worker_jobs", images
        )

    def recover_interrupted(self):
        """Under the single-worker lock, reclaim old running attempts before failing them; never rerun old C."""
        with self.store.db() as db:
            rows = db.execute("SELECT job_id, task_id FROM workflow_jobs WHERE state='running'").fetchall()
        for row in rows:
            directory = self.store.root / "worker_jobs" / row["job_id"]
            pending = directory / "creation.pending"
            if (marker_exists(directory) and not marker_exists(directory / "watchdog.ready")
                    and not marker_exists(directory / "watchdog.done")):
                # Old versions and interrupted watchdog startup cannot prove
                # whether the Docker create request was dispatched.
                pending.touch(exist_ok=True)
            reclaim(self.backend.client, row["task_id"], row["job_id"])
            if marker_exists(pending):
                raise UnreclaimedSandbox(
                    f"Container creation for job {row['job_id']} is uncertain; "
                    "restart Docker and confirm manually before reconciliation"
                )
            if marker_exists(directory):
                (directory / "watchdog.done").touch()
            self.store.finish_job(row["job_id"], "failed", None, "WORKER_RESTARTED")

    def resolve_uncertain(self, attempt_id: str):
        """Call only after the operator confirms Docker restarted and old create requests are no longer in flight."""
        from uuid import UUID
        attempt_id = str(UUID(attempt_id))
        row = self.store.job(attempt_id)
        directory = self.store.root / "worker_jobs" / attempt_id
        marker = directory / "creation.pending"
        if row is None or row["state"] != "running" or not marker.is_file():
            raise ValueError("No uncertain container creation is awaiting confirmation for this job")
        reclaim(self.backend.client, row["task_id"], attempt_id)
        marker.unlink()
        (directory / "watchdog.done").touch()
        self.store.finish_job(attempt_id, "failed", None, "CREATION_UNCERTAIN_RECONCILED")

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
                            # The submit finally block must still reclaim; the reporting transaction rejects success after cancellation.
                            pass

            watcher = None
            try:
                sources = {name: Path(ref).resolve()
                           for name, ref in json.loads(stored["sources_json"]).items()}
                if any(not path.is_relative_to(self.store.root) for path in sources.values()):
                    raise ValueError("Job source is outside the private directory")
                if self.store.task(job.task_id)["state"] == TaskState.CANCELLED:
                    self.store.finish_job(job.attempt_id, "cancelled", None, None)
                    return True
                self.backend.prepare(job, sources)
                if job.stage == Stage.CLEAN_C:
                    snapshot = self.backend.root / job.attempt_id / "input" / "clean.py"
                    with snapshot.open("rb") as stream:
                        source = stream.read(32769)
                    check_source(source.decode("utf-8"))
                watcher = threading.Thread(target=observe_cancellation, daemon=True)
                watcher.start()
                receipt = self.backend.submit(job, LIMITS[job.stage])
                status = self.backend.status(receipt)
                if status.result_ref and read_json(Path(status.result_ref)).get("error_code") == "CLEANUP_FAILED":
                    raise UnreclaimedSandbox("Container was not reclaimed; job remains running for reconciliation after restart")
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
                        raise ValueError("Execution report does not match the job")
                    category = manifest.get("runtime_category")
                    # Do not repair by default; only explicit C runtime failures or missing output after normal exit may enter repair.
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
                    snapshot_ref = manifest.get("executed_source_ref")
                    snapshot_hash = manifest.get("executed_source_sha256")
                    if job.stage == Stage.CLEAN_C and snapshot_ref is not None:
                        expected = self.backend.root / job.attempt_id / "input" / "clean.py"
                        snapshot = Path(snapshot_ref)
                        if (snapshot != expected or snapshot.is_symlink() or not snapshot.is_file()
                                or not snapshot.resolve().is_relative_to(self.store.root)):
                            raise ValueError("Executed source snapshot does not match the job")
                        with snapshot.open("rb") as stream:
                            code = stream.read(262145)
                        if len(code) > 262144 or hashlib.sha256(code).hexdigest() != snapshot_hash:
                            raise ValueError("Executed source snapshot hash does not match")
                        evidence["executed_source_ref"] = str(snapshot)
                        evidence["executed_source_sha256"] = snapshot_hash
                    elif error_code in ("RUNTIME_ERROR", "MISSING_OUTPUT"):
                        raise ValueError("Executed source snapshot for the failed job is missing")
                    if error_code == "RUNTIME_ERROR":
                        try:
                            source = code.decode("utf-8")
                        except UnicodeDecodeError:
                            source = ""
                        diagnostic = verified_diagnostic(manifest.get("runtime_diagnostic"), source)
                        if diagnostic is not None:
                            evidence["runtime_diagnostic"] = diagnostic
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
    parser = argparse.ArgumentParser(description="Independent persistent Docker job queue worker")
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("AI_AGENT_DATA_DIR", ".runtime")))
    parser.add_argument("--images", type=Path, default=Path(".runtime/docker/images.json"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--recover-only", action="store_true", help="Reclaim old attempts without consuming jobs")
    parser.add_argument("--resolve-uncertain", metavar="ATTEMPT_ID")
    parser.add_argument("--confirm-daemon-restarted", action="store_true")
    args = parser.parse_args()
    configuration = read_json(args.images)
    worker = QueueWorker(args.data_dir,
                         {Stage(key): value for key, value in configuration["images"].items()})
    if args.resolve_uncertain:
        if not args.confirm_daemon_restarted:
            parser.error("--resolve-uncertain requires --confirm-daemon-restarted")
        with process_lock(worker.store.root, "docker-worker"):
            worker.resolve_uncertain(args.resolve_uncertain)
        return
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
