"""Local supervision and offline maintenance, independent of cloud providers."""

import argparse
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import UUID, uuid4
import zipfile

from backend.orchestration.store import WorkflowStore, process_lock
from backend.artifacts.publication import redirected

SERVICES = ("api", "runner", "worker")
# A single graph invocation may spend 3 x 90s on bounded model format retries.
STALE_SECONDS = 360


def heartbeat(root: Path, service: str):
    if service not in SERVICES:
        raise ValueError("Unknown service")
    directory = root / "services"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{service}.json"
    temporary = directory / f"{service}.{os.getpid()}.part"
    temporary.write_text(json.dumps({"pid": os.getpid(), "seen": time.time()}), encoding="ascii")
    os.replace(temporary, path)


def readiness(root: Path):
    result = {}
    for service in SERVICES:
        try:
            path = root / "services" / f"{service}.json"
            if redirected(path) or path.stat().st_size > 4096:
                raise ValueError("Invalid heartbeat")
            data = json.loads(path.read_text(encoding="ascii"))
            age = time.time() - data["seen"]
            result[service] = {"ready": 0 <= age < STALE_SECONDS, "age_seconds": round(age, 1)}
        except (OSError, ValueError, TypeError, KeyError):
            result[service] = {"ready": False}
    return {"ready": all(s["ready"] for s in result.values()), "services": result}


def offline(root: Path, stack: ExitStack):
    for name in ("local-supervisor", "graph-runner", "docker-worker"):
        stack.enter_context(process_lock(root, name))
    if any(item["ready"] for item in readiness(root)["services"].values()):
        raise RuntimeError("Stop services and wait for stale heartbeats before maintenance")


def backup(root: Path, destination: Path):
    root = root.resolve()
    destination = destination.resolve()
    if destination.is_relative_to(root):
        raise ValueError("Backup must be outside the runtime directory")
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError("Backup destination must be a new file in an existing directory")
    temporary = destination.with_name(f".{uuid4()}.part")
    with ExitStack() as stack:
        offline(root, stack)
        try:
            with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("_backup.json", json.dumps({"schema_version": "1.0",
                                                            "original_root": str(root)}))
                for path in root.rglob("*"):
                    if redirected(path):
                        raise ValueError("Links are not allowed in backups")
                    if path.is_file() and path.suffix != ".lock" and "services" not in path.relative_to(root).parts:
                        archive.write(path, path.relative_to(root).as_posix())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def restore(source: Path, destination: Path):
    """Restore only into a new directory; never overwrite an existing runtime."""
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError("Restore destination must be a new directory")
    temporary = destination.parent / f".restore-{uuid4()}"
    try:
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > 100000 or sum(m.file_size for m in members) > 2_000_000_000:
                raise ValueError("Backup exceeds restore limits")
            names = set()
            for member in members:
                parts = member.filename.split("/")
                if (member.filename in names or any(p in ("", ".", "..") for p in parts)
                        or "\\" in member.filename or ":" in member.filename
                        or member.is_dir() or (member.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ValueError("Unsafe backup member")
                names.add(member.filename)
            metadata = json.loads(archive.read("_backup.json"))
            if (metadata.get("schema_version") != "1.0"
                    or destination.resolve() != Path(metadata["original_root"]).resolve()):
                raise ValueError("Restore must use the original absolute runtime path")
            temporary.mkdir()
            for member in members:
                if member.filename == "_backup.json":
                    continue
                target = temporary.joinpath(*member.filename.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as incoming, target.open("xb") as output:
                    shutil.copyfileobj(incoming, output)
            # Paths persisted in DB/checkpoints are absolute; relocation is not supported.
            if not (temporary / "tasks.sqlite3").is_file():
                raise ValueError("Backup is missing the task database")
            with closing(__import__("sqlite3").connect(temporary / "tasks.sqlite3")) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Invalid backup database")
            os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def cleanup(root: Path, days: int, apply: bool = False):
    if days < 1:
        raise ValueError("Retention must be at least one day")
    store = WorkflowStore(root)
    cutoff = time.time() - days * 86400
    from datetime import datetime
    with ExitStack() as stack:
        offline(store.root, stack)
        with store.db() as db:
            rows = db.execute("""SELECT t.task_id,MAX(e.created_at) AS last_event
                FROM tasks t JOIN events e USING(task_id)
                WHERE t.state IN ('SUCCEEDED','FAILED','CANCELLED')
                AND NOT EXISTS(SELECT 1 FROM workflow_jobs j WHERE j.task_id=t.task_id
                               AND j.state IN ('running','queued'))
                GROUP BY t.task_id""").fetchall()
            ids = [r["task_id"] for r in rows if datetime.fromisoformat(r["last_event"]).timestamp() < cutoff]
            if apply:
                for task_id in ids:
                    task_id = str(UUID(task_id))
                    job_ids = [str(UUID(r[0])) for r in db.execute(
                        "SELECT job_id FROM workflow_jobs WHERE task_id=?", (task_id,))]
                    paths = [store.root / "inputs" / f"{task_id}.csv"]
                    paths += [store.root / name / task_id for name in ("workflows", "snapshots", "published")]
                    paths += [store.root / "worker_jobs" / job_id for job_id in job_ids]
                    for path in paths:
                        if redirected(path) or any(redirected(p) for p in path.parents if p != store.root):
                            raise ValueError("Cleanup refuses links")
                        if path.is_dir():
                            if any(redirected(p) for p in path.rglob("*")):
                                raise ValueError("Cleanup refuses nested links")
                            for p in path.rglob("*"):
                                p.chmod(0o700 if p.is_dir() else 0o600)
                            path.chmod(0o700)
                            shutil.rmtree(path)
                        elif path.exists():
                            path.chmod(0o600)
                            path.unlink()
                    db.execute("DELETE FROM workflow_artifacts WHERE task_id=?", (task_id,))
                    db.execute("UPDATE workflow_outbox SET handled=1 WHERE task_id=?", (task_id,))
        return {"eligible_tasks": ids, "applied": apply, "metadata_retained": True}


def supervise(root: Path, images: Path, port: int):
    """Fail-stop supervision. No implicit model retry or re-execution of old C."""
    root = root.resolve()
    children = []
    with process_lock(root, "local-supervisor"):
        stop_request = root / "stop.request"
        stop_request.unlink(missing_ok=True)
        common = dict(os.environ, AI_AGENT_DATA_DIR=str(root))
        if not common.get("AI_AGENT_DEMO_TOKEN"):
            raise ValueError("Set AI_AGENT_DEMO_TOKEN before starting")
        commands = [
            ("api", [sys.executable, "-m", "uvicorn", "backend.app:create_app", "--factory",
                     "--host", "127.0.0.1", "--port", str(port)]),
            ("runner", [sys.executable, "-m", "backend.orchestration.runner", "--data-dir", str(root)]),
            ("worker", [sys.executable, "-m", "workers.sandbox_manager.queue_worker",
                        "--data-dir", str(root), "--images", str(images.resolve())]),
        ]
        try:
            for name, command in commands:
                environment = dict(common)
                if name != "runner":
                    environment.pop("DEEPSEEK_API_KEY", None)
                    environment["AI_AGENT_LLM_PROVIDER"] = "disabled"
                    environment["AI_AGENT_LLM_ALLOW_EXTERNAL"] = "false"
                children.append(subprocess.Popen(command, env=environment))
            started = time.monotonic()
            while True:
                if stop_request.exists():
                    break
                if any(child.poll() is not None for child in children):
                    raise RuntimeError("A service exited; stopping the local stack")
                if time.monotonic() - started > STALE_SECONDS and not readiness(root)["ready"]:
                    raise RuntimeError("A service is stale; stopping the local stack")
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
            for child in children:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            environment = dict(common)
            environment.pop("DEEPSEEK_API_KEY", None)
            environment["AI_AGENT_LLM_PROVIDER"] = "disabled"
            subprocess.run(commands[-1][1] + ["--recover-only"], env=environment, timeout=90, check=True)
            for service in SERVICES:
                (root / "services" / f"{service}.json").unlink(missing_ok=True)
            stop_request.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "stop", "status", "backup", "restore", "cleanup"))
    parser.add_argument("--data-dir", type=Path, default=Path(".runtime"))
    parser.add_argument("--images", type=Path, default=Path(".runtime/docker/images.json"))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.command == "start":
        supervise(args.data_dir, args.images, args.port)
    elif args.command == "stop":
        if not args.data_dir.is_dir():
            parser.error("Runtime directory does not exist")
        (args.data_dir / "stop.request").touch(exist_ok=True)
    elif args.command == "status":
        print(json.dumps(readiness(args.data_dir), indent=2))
    elif args.command == "cleanup":
        print(json.dumps(cleanup(args.data_dir, args.days, args.apply), indent=2))
    elif not args.archive:
        parser.error("--archive is required")
    elif args.command == "backup":
        backup(args.data_dir, args.archive)
    else:
        restore(args.archive, args.data_dir)


if __name__ == "__main__":
    main()
