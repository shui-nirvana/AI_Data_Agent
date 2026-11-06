"""Opt-in real model/Docker test; only the copied first source is fault-injected."""

import ast
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("docker")

from fastapi.testclient import TestClient
from backend.app import create_app
from backend.llm.client import ModelGateway
from backend.llm.deepseek import DeepSeekClient
from backend.llm.generator import LLMCodeGenerator
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


def injected_copy(original: bytes):
    """Insert one fixed fault inside a model function called by module-level code."""
    text = original.decode("utf-8")
    tree = ast.parse(text)
    definitions = [item for item in tree.body if isinstance(item, ast.FunctionDef)]
    invoked = {
        node.func.id for item in tree.body if not isinstance(item, (ast.FunctionDef, ast.ClassDef))
        for node in ast.walk(item)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    definitions = sorted((item for item in definitions if item.name in invoked),
                         key=lambda item: item.name != "main")
    lines = text.splitlines(keepends=True)
    for function in definitions:
        if not function.body or function.body[0].lineno <= function.lineno:
            continue
        index = function.body[0].lineno - 1
        body_line = lines[index]
        indent = body_line[:len(body_line) - len(body_line.lstrip())]
        if not indent or "\n" in indent or "\r" in indent:
            continue
        newline = "\r\n" if body_line.endswith("\r\n") else "\n"
        fault = f"{indent}raise RuntimeError('CONTROLLED_TEST_FAULT'){newline}"
        changed = ("".join(lines[:index]) + fault + "".join(lines[index:])).encode("utf-8")
        assert changed.replace(fault.encode("utf-8"), b"", 1) == original
        return changed, function.name, index + 1
    raise AssertionError("No suitable directly invoked function; no fault was injected")


def assert_repair_requests(evidence, root):
    attempts = evidence["result"]["attempts"]
    failed = {item["executed_source_sha256"]: item for item in attempts
              if item["stage"] == "CLEAN_C" and item["state"] == "failed"}
    ledger = root / "workflows" / evidence["task_id"] / "llm" / "calls.sqlite3"
    with sqlite3.connect(ledger) as db:
        records = {request_id: (status, error) for request_id, status, error in
                   db.execute("SELECT request_id,status,error_code FROM calls")}
    calls = evidence["calls"]
    assert 1 <= len(calls) <= 3
    assert len(records) == len(calls)
    assert {entry["request"]["request_id"] for entry in calls} == set(records)
    completed = {code["request_id"] for code in evidence["result"]["codes"]}
    assert completed == {request_id for request_id, (status, _) in records.items()
                         if status == "completed"}
    assert calls[0]["request"]["request_id"] == evidence["result"]["codes"][0]["request_id"]
    first = calls[0]["request"]["messages"]
    first_body = json.loads(first[-1]["content"])
    response_errors = {digest: None for digest in failed}
    seen = set()
    for entry in calls[1:]:
        messages = entry["request"]["messages"]
        body = json.loads(messages[-1]["content"])
        assert first[0] == messages[0] and first_body["task"] == body["task"]
        source = body["previous_source"].encode("utf-8")
        digest = hashlib.sha256(source).hexdigest()
        attempt = failed[digest]
        seen.add(digest)
        snapshot = root / "worker_jobs" / attempt["job_id"] / "input" / "clean.py"
        assert source == snapshot.read_bytes()
        assert body["implementation_error_code"] == attempt["error_code"]
        assert body.get("response_error_code") == response_errors[digest]
        if body["implementation_error_code"] == "RUNTIME_ERROR" and attempt["runtime_diagnostic"] is not None:
            assert body.get("runtime_diagnostic") == attempt["runtime_diagnostic"]
        elif "runtime_diagnostic" in body:
            assert body["runtime_diagnostic"] == attempt["runtime_diagnostic"]
        status, error = records[entry["request"]["request_id"]]
        if status == "invalid":
            assert error in ("SOURCE_FORMAT", "SYNTAX_ERROR")
            response_errors[digest] = error
        else:
            assert status == "completed"
    assert seen == set(failed)


@pytest.mark.parametrize("format_retry,keep_diagnostic", [(False, False), (True, False), (True, True)])
def test_repair_evidence_accepts_model_format_retries(tmp_path, format_retry, keep_diagnostic):
    task_id, job_id = "task", "failed-job"
    source = "def main():\n    raise RuntimeError()\nmain()\n"
    digest = hashlib.sha256(source.encode()).hexdigest()
    diagnostic = {"exception_type": "RuntimeError", "line": 2, "function": "main"}
    snapshot = tmp_path / "worker_jobs" / job_id / "input" / "clean.py"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(source.encode())
    ledger = tmp_path / "workflows" / task_id / "llm" / "calls.sqlite3"
    ledger.parent.mkdir(parents=True)
    records = [("initial", "completed", None)]
    if format_retry:
        records.append(("invalid", "invalid", "SOURCE_FORMAT"))
    records.append(("repair", "completed", None))
    with sqlite3.connect(ledger) as db:
        db.execute("CREATE TABLE calls(request_id TEXT, status TEXT, error_code TEXT)")
        db.executemany("INSERT INTO calls VALUES(?,?,?)", records)

    def entry(request_id, body):
        return {"request": {"request_id": request_id, "messages": [
            {"role": "system", "content": "same prompt"},
            {"role": "user", "content": json.dumps(body)},
        ]}}

    task = {"approved_rules": []}
    first_body = {"task": task}
    repair_body = {"task": task, "previous_source": source,
                   "implementation_error_code": "RUNTIME_ERROR",
                   "runtime_diagnostic": diagnostic}
    calls = [entry("initial", first_body)]
    if format_retry:
        calls.append(entry("invalid", repair_body))
        repair_body = {"task": task, "previous_source": source,
                       "implementation_error_code": "RUNTIME_ERROR",
                       "response_error_code": "SOURCE_FORMAT"}
        if keep_diagnostic:
            repair_body["runtime_diagnostic"] = diagnostic
    calls.append(entry("repair", repair_body))
    evidence = {"task_id": task_id, "calls": calls, "result": {
        "attempts": [{"stage": "CLEAN_C", "state": "failed", "job_id": job_id,
                      "executed_source_sha256": digest, "error_code": "RUNTIME_ERROR",
                      "runtime_diagnostic": diagnostic}],
        "codes": [{"request_id": "initial"}, {"request_id": "repair"}],
    }}
    if format_retry and not keep_diagnostic:
        with pytest.raises(AssertionError):
            assert_repair_requests(evidence, tmp_path)
        return
    assert_repair_requests(evidence, tmp_path)
    snapshot.write_bytes(b"wrong execution source")
    with pytest.raises(AssertionError):
        assert_repair_requests(evidence, tmp_path)


@live_only
def test_large_real_function_repair(tmp_path):
    evidence = {"calls": [], "task_id": None}
    evidence_path = tmp_path / "controlled-experiment-evidence.json"
    sample_path = Path(os.environ.get("AI_AGENT_LARGE_SAMPLE", PROJECT / "synthetic_large_valid.csv"))
    images = Path(os.environ.get("AI_AGENT_DOCKER_IMAGES", ".runtime/docker/images.json")).resolve()
    api = TestClient(create_app(data_dir=tmp_path, demo_token="test-secret"))
    headers = {"X-Demo-Token": "test-secret", "Content-Type": "text/csv",
               "X-Task-Goal": "Normalize order date and fill empty email only"}

    class Observer:
        """Observe the genuine gateway request and response without changing either."""

        def __init__(self):
            provider = os.environ.get("AI_AGENT_TEST_LIVE_PROVIDER", "deepseek")
            if provider == "deepseek":
                self.client = DeepSeekClient.from_environment("deepseek-flash")
            elif provider == "vultr":
                from backend.llm.vultr import VultrInferenceClient
                self.client = VultrInferenceClient.from_environment(os.environ.get("AI_AGENT_LLM_MODEL"))
            else:
                raise ValueError("Unknown live test provider")

        async def complete(self, request):
            entry = {"request": request.model_dump(mode="json")}
            evidence["calls"].append(entry)
            response = await self.client.complete(request)
            entry["response"] = response.model_dump(mode="json")
            return response

    observer = Observer()
    generator = LLMCodeGenerator(tmp_path, ModelGateway(observer))

    class InjectOnce:
        async def generate(self, spec_ref, profile_ref):
            version = await generator.generate(spec_ref, profile_ref)
            original_path = Path(version.source_ref)
            original = original_path.read_bytes()
            assert hashlib.sha256(original).hexdigest() == version.code_sha256
            assert len(evidence["calls"]) == 1, "First response was not a valid source"
            copy = original_path.parent / "controlled-execution-copy.py"
            with copy.open("xb") as stream:
                stream.write(original)
            original_path.chmod(0o444)
            evidence["injection"] = {
                "original_source_ref": str(original_path),
                "original_sha256": version.code_sha256,
                "fault_source_ref": str(copy),
            }
            return replace(version, source_ref=str(copy))

        async def repair(self, *args):
            return await generator.repair(*args)

    test_generator = InjectOnce()

    def runner():
        assert GraphRunner(tmp_path, test_generator).run_once()
        print("STATE", store.task(task_id)["state"], flush=True)

    def worker():
        env = {k: v for k, v in os.environ.items()
               if k not in ("DEEPSEEK_API_KEY", "VULTR_SERVERLESS_INFERENCE_API_KEY", "VULTR_API_KEY")}
        env["AI_AGENT_LLM_PROVIDER"] = "disabled"
        env["AI_AGENT_LLM_ALLOW_EXTERNAL"] = "false"
        env.pop("AI_AGENT_VULTR_SYSTEM_PROXY_ENABLED", None)
        result = subprocess.run([
            sys.executable, "-m", "workers.sandbox_manager.queue_worker",
            "--once", "--data-dir", str(tmp_path), "--images", str(images),
        ], cwd=PROJECT, env=env, capture_output=True, text=True, timeout=150)
        assert result.returncode == 0, result.stderr
        assert "job processed=True" in result.stdout

    try:
        sample = sample_path.read_bytes()
        assert hashlib.sha256(sample).hexdigest() == SAMPLE_SHA256
        created = api.post("/api/tasks", content=sample, headers=headers)
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        evidence["task_id"] = task_id
        store = WorkflowStore(tmp_path)
        base = f"/api/tasks/{task_id}"
        assert api.post(base + "/start", headers=headers).status_code == 202
        runner()
        worker()
        runner()
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
        evidence["simulated_approval"] = choices
        assert api.post(base + "/decisions", json={
            "task_version": review["task_version"], "decisions": choices
        }, headers=headers).status_code == 200
        runner()
        injection = evidence["injection"]
        changed, function, line = injected_copy(Path(injection["original_source_ref"]).read_bytes())
        path = Path(injection["fault_source_ref"])
        path.write_bytes(changed)
        injection.update(fault_sha256=hashlib.sha256(changed).hexdigest(),
                         function=function, line=line)
        print("FROZEN_AND_INJECTED", json.dumps(injection), flush=True)
        for _ in range(5):
            if store.task(task_id)["state"] in ("FAILED", "SUCCEEDED", "CANCELLED"):
                break
            worker()
            runner()
        evidence["result"] = api.get(base + "/evidence", headers=headers).json()
        evidence["events"] = api.get(base + "/events", headers=headers).json()["events"]
        print("EVIDENCE", json.dumps(evidence["result"], ensure_ascii=False), flush=True)
        if os.environ.get("AI_AGENT_AUDIT_VALIDATION_EXPLANATION") == "1" and evidence["result"].get("validation"):
            evidence["explanation_audit"] = {}
            audit_explanation(api, base, headers, tmp_path, task_id,
                              observer.client, evidence["explanation_audit"])
        assert evidence["result"]["state"] == "SUCCEEDED"
        attempts = evidence["result"]["attempts"]
        stages = [attempt["stage"] for attempt in attempts]
        assert stages[0] == "PROFILE_A" and stages[-1] == "VERIFY_D"
        assert 2 <= len(stages[1:-1]) <= 3 and set(stages[1:-1]) == {"CLEAN_C"}
        injection = evidence["injection"]
        assert hashlib.sha256(Path(injection["original_source_ref"]).read_bytes()).hexdigest() == injection["original_sha256"]
        expected_diagnostic = {"exception_type": "RuntimeError", "line": injection["line"],
                               "function": injection["function"]}
        assert attempts[1]["runtime_diagnostic"] == expected_diagnostic
        assert attempts[1]["executed_source_sha256"] == injection["fault_sha256"]
        assert evidence["result"]["codes"][0]["code_sha256"] == injection["original_sha256"]
        assert injection["fault_sha256"] != injection["original_sha256"]
        assert_repair_requests(evidence, tmp_path)
        validation = evidence["result"]["validation"]
        assert validation["verdict"] == "passed"
        assert validation["changed_cells"] == 20000 and validation["mismatch_count"] == 0
        downloads = {}
        for artifact in ("output", "validation", "manifest"):
            response = api.get(base + "/artifacts/" + artifact, headers=headers)
            assert response.status_code == 200
            digest = hashlib.sha256(response.content).hexdigest()
            assert digest == response.headers["x-content-sha256"]
            downloads[artifact] = {"bytes": len(response.content), "sha256": digest}
        evidence["downloads"] = downloads
        print("DOWNLOADS", json.dumps(downloads), flush=True)
    finally:
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print("EVIDENCE_PATH", evidence_path, flush=True)
