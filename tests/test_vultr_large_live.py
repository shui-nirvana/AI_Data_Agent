"""Opt-in normal 50,000-row Vultr workflow; no injected faults."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip("langgraph")
docker = pytest.importorskip("docker")

from fastapi.testclient import TestClient
from backend.app import create_app
from backend.llm.client import ModelGateway
from backend.llm.generator import LLMCodeGenerator
from backend.llm.vultr import VultrInferenceClient
from backend.orchestration.runner import GraphRunner
from backend.orchestration.store import WorkflowStore
from live_explanation_audit import audit_explanation


live_only = pytest.mark.skipif(
    any(os.environ.get(name) != value for name, value in (
        ("AI_AGENT_RUN_DOCKER_TESTS", "1"),
        ("AI_AGENT_RUN_LIVE_LLM_TESTS", "1"),
        ("AI_AGENT_RUN_LARGE_LIVE_LLM_TESTS", "1"),
        ("AI_AGENT_LLM_ALLOW_EXTERNAL", "true"),
    )),
    reason="Explicit real Docker, external model, and large sample opt-in required",
)

PROJECT = Path(__file__).resolve().parents[1]
SAMPLE_SHA256 = "c98ed8d51c9ae8aa0c71e8f72841d8255914339b9d9b3094841d45bed63b4892"


def sample_stats(container, label, previous):
    stats = container.stats(stream=False, one_shot=True)
    memory = stats.get("memory_stats", {})
    cpu = stats.get("cpu_stats", {})
    current = (cpu.get("cpu_usage", {}).get("total_usage"), cpu.get("system_cpu_usage"))
    last = previous.get(container.id)
    online = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage", []))
    cpu_percent = None
    if all(type(value) is int for value in current):
        previous[container.id] = current
        if last is not None and online:
            cpu_delta = current[0] - last[0]
            system_delta = current[1] - last[1]
            if cpu_delta >= 0 and system_delta > 0:
                cpu_percent = round(cpu_delta / system_delta * online * 100, 2)
    return {"step": label, "memory_bytes": memory.get("usage"),
            "memory_limit_bytes": memory.get("limit"), "cpu_percent": cpu_percent}


def test_one_shot_stats_use_two_samples_for_cpu():
    class Container:
        id = "container"

        def __init__(self):
            self.count = 0

        def stats(self, *, stream, one_shot):
            assert stream is False and one_shot is True
            self.count += 1
            return {"memory_stats": {"usage": 100, "limit": 200},
                    "cpu_stats": {"cpu_usage": {"total_usage": self.count * 10},
                                  "system_cpu_usage": self.count * 100, "online_cpus": 4}}

    container, previous = Container(), {}
    first = sample_stats(container, "profile_a", previous)
    second = sample_stats(container, "profile_a", previous)
    assert first["memory_bytes"] == 100 and first["cpu_percent"] is None
    assert second["cpu_percent"] == 40.0 and second["memory_limit_bytes"] == 200


@live_only
def test_vultr_large_normal_workflow(tmp_path):
    sample_path = Path(os.environ.get("AI_AGENT_LARGE_SAMPLE", PROJECT / "synthetic_large_valid.csv"))
    images = Path(os.environ.get("AI_AGENT_DOCKER_IMAGES", ".runtime/docker/images.json")).resolve()
    api = TestClient(create_app(data_dir=tmp_path, demo_token="test-secret"))
    headers = {"X-Demo-Token": "test-secret", "Content-Type": "text/csv",
               "X-Task-Goal": "Normalize order date and fill empty email only"}
    model_client = VultrInferenceClient.from_environment(os.environ.get("AI_AGENT_LLM_MODEL"))
    generator = LLMCodeGenerator(tmp_path, ModelGateway(model_client))
    started = time.perf_counter()
    timings = []
    samples = []
    sample_errors = []
    task_id = None
    store = None

    def runner(label):
        start = time.perf_counter()
        assert GraphRunner(tmp_path, generator).run_once()
        timings.append({"step": label, "seconds": round(time.perf_counter() - start, 3)})
        print("STATE", label, store.task(task_id)["state"], flush=True)

    def worker(label):
        stop = threading.Event()

        def monitor():
            previous = {}
            client = None
            try:
                client = docker.from_env(timeout=5)
                while not stop.is_set():
                    containers = client.containers.list(filters={"label": f"ai-data-agent.task={task_id}"})
                    for container in containers:
                        try:
                            samples.append(sample_stats(container, label, previous))
                        except (docker.errors.APIError, json.JSONDecodeError) as exc:
                            sample_errors.append({"step": label, "error": type(exc).__name__})
                    stop.wait(0.25)
            except Exception as exc:
                sample_errors.append({"step": label, "error": type(exc).__name__})
            finally:
                if client is not None:
                    client.close()

        env = {k: v for k, v in os.environ.items()
               if k not in ("DEEPSEEK_API_KEY", "VULTR_SERVERLESS_INFERENCE_API_KEY", "VULTR_API_KEY")}
        env["AI_AGENT_LLM_PROVIDER"] = "disabled"
        env["AI_AGENT_LLM_ALLOW_EXTERNAL"] = "false"
        env.pop("AI_AGENT_VULTR_SYSTEM_PROXY_ENABLED", None)
        thread = threading.Thread(target=monitor, daemon=True)
        start = time.perf_counter()
        thread.start()
        try:
            result = subprocess.run(
                [sys.executable, "-m", "workers.sandbox_manager.queue_worker", "--once",
                 "--data-dir", str(tmp_path), "--images", str(images)],
                cwd=PROJECT, env=env, capture_output=True, text=True, timeout=150,
            )
            assert result.returncode == 0, result.stderr
            assert "job processed=True" in result.stdout
        finally:
            stop.set()
            thread.join(timeout=8)
            if thread.is_alive():
                sample_errors.append({"step": label, "error": "MonitorJoinTimeout"})
            timings.append({"step": label, "seconds": round(time.perf_counter() - start, 3)})

    report = {"sample_sha256": SAMPLE_SHA256, "provider": "vultr",
              "approval": "simulated via API", "fault_injected": False}
    try:
        sample = sample_path.read_bytes()
        assert hashlib.sha256(sample).hexdigest() == SAMPLE_SHA256
        created = api.post("/api/tasks", content=sample, headers=headers)
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        report["task_id"] = task_id
        store = WorkflowStore(tmp_path)
        base = f"/api/tasks/{task_id}"
        assert api.post(base + "/start", headers=headers).status_code == 202
        runner("schedule_profile")
        worker("profile_a")
        runner("review_ready")
        review = api.get(base + "/review", headers=headers).json()
        profile = review["profile"]
        assert profile["row_count"] == 50000
        assert profile["missing_by_column"]["email"] == 10000
        assert profile["date_formats_by_column"]["order_date"]["YYYY/MM/DD"] == 10000
        assert profile["duplicate_rows"] == 10000
        assert profile["numeric_by_column"]["amount"]["negative_count"] == 10000
        assert {issue["column"] for issue in review["issues"]} == {"order_date", "email", "amount"}
        choices = []
        for issue in review["issues"]:
            col = issue["column"]
            decision = {"issue_id": issue["issue_id"], "column": col}
            if col == "order_date":
                decision.update(action="normalize_date", predicate_id="parseable_nonstandard_date",
                                params={"input_formats": ["YYYY/MM/DD"]})
            elif col == "email":
                decision.update(action="fill_null_with_literal", predicate_id="empty_string",
                                params={"literal": "UNKNOWN"})
            else:
                decision.update(action="preserve", predicate_id="all_cells", params={})
            choices.append(decision)
        report["simulated_approval"] = choices
        assert api.post(base + "/decisions", json={
            "task_version": review["task_version"], "decisions": choices
        }, headers=headers).status_code == 200
        runner("generate")
        for index in range(5):
            if store.task(task_id)["state"] in ("FAILED", "SUCCEEDED", "CANCELLED"):
                break
            worker(f"execution_{index + 1}")
            runner(f"advance_{index + 1}")
        evidence = api.get(base + "/evidence", headers=headers).json()
        report["evidence"] = evidence
        if os.environ.get("AI_AGENT_AUDIT_VALIDATION_EXPLANATION") == "1" and evidence.get("validation"):
            report["explanation_audit"] = {}
            start = time.perf_counter()
            try:
                audit_explanation(api, base, headers, tmp_path, task_id,
                                  model_client, report["explanation_audit"])
            finally:
                timings.append({"step": "validation_explanation",
                                "seconds": round(time.perf_counter() - start, 3)})
        assert evidence["state"] == "SUCCEEDED"
        stages = [item["stage"] for item in evidence["attempts"]]
        assert stages[0] == "PROFILE_A" and stages[-1] == "VERIFY_D"
        assert 1 <= len(stages[1:-1]) <= 3 and set(stages[1:-1]) == {"CLEAN_C"}
        assert all(code["provider"] == "vultr" for code in evidence["codes"])
        validation = evidence["validation"]
        assert validation["verdict"] == "passed"
        assert validation["changed_cells"] == 20000 and validation["mismatch_count"] == 0
        downloads = {}
        for artifact in ("output", "validation", "manifest"):
            response = api.get(base + "/artifacts/" + artifact, headers=headers)
            assert response.status_code == 200
            digest = hashlib.sha256(response.content).hexdigest()
            assert digest == response.headers["x-content-sha256"]
            downloads[artifact] = {"bytes": len(response.content), "sha256": digest}
        report["downloads"] = downloads
    finally:
        report["total_seconds"] = round(time.perf_counter() - started, 3)
        report["timings"] = timings
        report["resource_samples"] = samples
        report["resource_sample_errors"] = sample_errors
        if task_id and store:
            report["state"] = store.task(task_id)["state"]
        evidence_path = tmp_path / "vultr-large-normal-evidence.json"
        evidence_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("EVIDENCE_PATH", evidence_path, flush=True)
        print("SUMMARY", json.dumps({"state": report.get("state"), "timings": timings,
                                     "resource_samples": len(samples), "errors": sample_errors},
                                    ensure_ascii=False), flush=True)
