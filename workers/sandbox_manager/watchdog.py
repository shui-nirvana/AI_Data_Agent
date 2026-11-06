"""Independent, per-attempt Docker deadline and reconciliation watchdog."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import UUID


def matching_containers(client, task_id: str, attempt_id: str):
    """Never remove a container solely because its name partially matches."""
    name = f"ai-data-agent-{attempt_id}"
    containers = client.containers.list(all=True, filters={
        "name": name,
        "label": [f"ai-data-agent.attempt={attempt_id}", f"ai-data-agent.task={task_id}"],
    })
    return [
        container for container in containers
        if container.name == name
        and container.labels.get("ai-data-agent.attempt") == attempt_id
        and container.labels.get("ai-data-agent.task") == task_id
    ]


def reclaim(client, task_id: str, attempt_id: str):
    import docker
    for container in matching_containers(client, task_id, attempt_id):
        try:
            container.remove(force=True)
        except docker.errors.NotFound:
            pass


class Watchdog:
    def __init__(self, directory: Path, task_id: str, attempt_id: str, deadline: float):
        environment = {
            key: value for key, value in os.environ.items()
            if key.upper() in {
                "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
                "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_CONTEXT",
                "PYTHONPATH", "VIRTUAL_ENV",
            }
        }
        self.directory = directory
        self.process = subprocess.Popen(
            [sys.executable, "-m", "workers.sandbox_manager.watchdog",
             str(directory), task_id, attempt_id, str(deadline)],
            env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        startup_deadline = time.monotonic() + 15
        while not (directory / "watchdog.ready").exists():
            if self.process.poll() is not None or time.monotonic() >= startup_deadline:
                if self.process.poll() is None:
                    self.process.terminate()
                    self.process.wait(timeout=5)
                raise RuntimeError("Sandbox watchdog failed to start")
            time.sleep(0.05)
        if self.process.poll() is not None:
            raise RuntimeError("Sandbox watchdog exited during startup")

    def alive(self):
        return self.process.poll() is None

    def complete(self):
        (self.directory / "watchdog.done").touch()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # The done marker remains durable; the watchdog will exit when unblocked.
            pass


def run(directory: Path, task_id: str, attempt_id: str, deadline: float):
    import docker
    UUID(task_id)
    UUID(attempt_id)
    startup_deadline = time.monotonic() + 12
    while True:
        try:
            client = docker.from_env(timeout=2)
            client.ping()
            break
        except Exception:
            if time.monotonic() >= startup_deadline:
                raise
            time.sleep(0.5)
    (directory / "watchdog.ready").touch()
    while not (directory / "watchdog.done").exists():
        if time.monotonic() >= deadline:
            try:
                (directory / "watchdog.timeout").touch()
            except OSError:
                pass
            try:
                reclaim(client, task_id, attempt_id)
            except Exception:
                # Docker may be unavailable or a create request may still be in flight.
                # Continue reconciling until a trusted worker explicitly confirms safety.
                pass
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("task_id")
    parser.add_argument("attempt_id")
    parser.add_argument("deadline", type=float)
    args = parser.parse_args()
    run(args.directory, args.task_id, args.attempt_id, args.deadline)


if __name__ == "__main__":
    main()
