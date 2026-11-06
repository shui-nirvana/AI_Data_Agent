"""Deterministic watchdog tests; real Docker checks are separately opt-in."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

docker = pytest.importorskip("docker", reason="Watchdog tests require the worker optional dependency")

from workers.sandbox_manager import watchdog


def test_reclaim_requires_exact_name_and_both_labels():
    task, attempt = str(uuid4()), str(uuid4())
    removed = []

    def container(name, task_label, attempt_label):
        return SimpleNamespace(
            name=name,
            labels={"ai-data-agent.task": task_label, "ai-data-agent.attempt": attempt_label},
            remove=lambda force: removed.append(name),
        )

    exact = f"ai-data-agent-{attempt}"
    candidates = [container(exact, task, attempt),
                  container(exact + "-other", task, attempt),
                  container(exact, str(uuid4()), attempt),
                  container(exact, task, str(uuid4()))]
    client = SimpleNamespace(containers=SimpleNamespace(list=lambda **kwargs: candidates))
    watchdog.reclaim(client, task, attempt)
    assert removed == [exact]


def test_watchdog_process_does_not_inherit_model_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("VULTR_SERVERLESS_INFERENCE_API_KEY", "private-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    monkeypatch.setenv("AI_AGENT_DEMO_TOKEN", "private-test-token")
    captured = {}

    def popen(command, **kwargs):
        captured.update(kwargs["env"])
        (tmp_path / "watchdog.ready").touch()
        return SimpleNamespace(poll=lambda: None, wait=lambda timeout: 0)

    monkeypatch.setattr(watchdog.subprocess, "Popen", popen)
    guard = watchdog.Watchdog(tmp_path, str(uuid4()), str(uuid4()), 100)
    guard.complete()
    assert "private-test-key" not in captured.values()
    assert "private-test-token" not in captured.values()
    assert (tmp_path / "watchdog.done").exists()


@pytest.mark.parametrize("marker_failure", [False, True])
def test_watchdog_retries_daemon_errors_and_delayed_creation(tmp_path, monkeypatch, marker_failure):
    task, attempt = str(uuid4()), str(uuid4())
    removed = []
    calls = []
    pings = []
    container = SimpleNamespace(
        name=f"ai-data-agent-{attempt}",
        labels={"ai-data-agent.task": task, "ai-data-agent.attempt": attempt},
        remove=lambda force: removed.append(force),
    )

    def list_containers(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("daemon temporarily unavailable")
        return [] if len(calls) == 2 else [container]

    def ping():
        pings.append(1)
        if len(pings) == 1:
            raise TimeoutError("daemon temporarily unavailable")
        return True

    client = SimpleNamespace(ping=ping, containers=SimpleNamespace(list=list_containers))
    monkeypatch.setattr(docker, "from_env", lambda **kwargs: client)
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: 10)
    original_touch = Path.touch

    def touch(path, *args, **kwargs):
        if marker_failure and path.name == "watchdog.timeout":
            raise OSError("simulated disk failure")
        return original_touch(path, *args, **kwargs)

    monkeypatch.setattr(Path, "touch", touch)

    def wait(_):
        if removed:
            (tmp_path / "watchdog.done").touch()

    monkeypatch.setattr(watchdog.time, "sleep", wait)
    watchdog.run(tmp_path, task, attempt, 5)
    assert (tmp_path / "watchdog.ready").exists()
    assert (tmp_path / "watchdog.timeout").exists() is not marker_failure
    assert len(calls) == 3 and removed == [True]
    assert len(pings) == 2


def test_watchdog_refuses_invalid_identity(tmp_path):
    with pytest.raises(ValueError):
        watchdog.run(tmp_path, "not-a-uuid", str(uuid4()), 0)
    assert not (tmp_path / "watchdog.ready").exists()
