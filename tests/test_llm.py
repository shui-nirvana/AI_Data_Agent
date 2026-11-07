"""Model HTTP, prompt, and source-version tests; never transmit real keys or execute generated source on the host."""

import asyncio
from dataclasses import asdict
import hashlib
import json
import sqlite3
from uuid import uuid4

import httpx
import pytest

from backend.bootstrap import build_generator
from backend.config import IntegrationSettings
from backend.domain.spec import Action, ActionParams, Decision, freeze_spec
from backend.jobs.execution import ExecutedSource
from backend.llm.client import Message, ModelGateway, ModelRequest, ModelResponse, ModelUnavailableError
from backend.llm.deepseek import DeepSeekClient
from backend.llm.generator import (
    LLMCodeGenerator, SourcePolicyError, SourceProtocolError, check_source, parse_model_json,
)
from images.profiler.main import scan


def call(coro):
    return asyncio.run(coro)


def executed(version):
    from pathlib import Path

    context = json.loads((Path(version.source_ref).parent / "context.json").read_text())
    return ExecutedSource(version.source_ref, version.code_sha256,
                          context["spec_ref"], context["profile_ref"])


def request():
    return ModelRequest(request_id="test", messages=(Message(role="user", content="test"),))


def provider_response(reason="stop", text="source"):
    return {"model": "deepseek-flash", "choices": [
        {"finish_reason": reason, "message": {"role": "assistant", "content": text}}
    ], "usage": {"prompt_tokens": 15, "completion_tokens": 30}}


def test_deepseek_environment_and_fixed_request(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    client = DeepSeekClient.from_environment()

    def handler(req):
        assert str(req.url) == "https://api.deepseek.com/chat/completions"
        assert req.headers["Authorization"] == "Bearer test-key-not-real"
        body = json.loads(req.content)
        assert body["thinking"] == {"type": "disabled"} and body["stream"] is False
        assert body["max_tokens"] == 4096
        assert "test-key-not-real" not in req.content.decode()
        return httpx.Response(200, json=provider_response())

    client._transport = httpx.MockTransport(handler)
    result = call(ModelGateway(client).complete(request()))
    assert result.provider == "deepseek" and result.input_tokens == 15


@pytest.mark.parametrize("scenario", ["401", "429", "redirect", "oversize", "length", "refused",
                                      "empty", "tool", "resource", "malformed", "timeout"])
def test_deepseek_fail_closed_no_implicit_retry(scenario):
    calls = []

    def handler(req):
        calls.append(True)
        if scenario in ("401", "429"):
            return httpx.Response(int(scenario), text="private-message test-key")
        if scenario == "redirect":
            return httpx.Response(302, headers={"Location": "https://other.example/"})
        if scenario == "oversize":
            return httpx.Response(200, content=b"x" * 1_000_001)
        if scenario == "malformed":
            return httpx.Response(200, json={})
        if scenario == "timeout":
            raise httpx.ReadTimeout("private-message")
        reasons = {"length": "length", "refused": "content_filter", "tool": "tool_calls",
                   "resource": "insufficient_system_resource"}
        return httpx.Response(200, json=provider_response(reasons.get(scenario, "stop"), ""))

    client = DeepSeekClient("test-key", transport=httpx.MockTransport(handler))
    with pytest.raises(ModelUnavailableError) as exc:
        call(ModelGateway(client).complete(request()))
    assert "private-message" not in str(exc.value) and "test-key" not in str(exc.value)
    assert len(calls) == 1


def test_bootstrap_requires_provider_permission_and_key(tmp_path, monkeypatch):
    settings = IntegrationSettings(llm_provider="deepseek")
    monkeypatch.delenv("AI_AGENT_LLM_ALLOW_EXTERNAL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    key_file = tmp_path / ".env.deepseek"
    key_file.write_text("DEEPSEEK_API_KEY=\n")
    monkeypatch.setattr("backend.llm.deepseek.KEY_FILE", key_file)
    with pytest.raises(ValueError, match="authorize"):
        build_generator(tmp_path, settings)
    monkeypatch.setenv("AI_AGENT_LLM_ALLOW_EXTERNAL", "true")
    with pytest.raises(ModelUnavailableError, match="missing"):
        build_generator(tmp_path, settings)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    assert isinstance(build_generator(tmp_path, settings), LLMCodeGenerator)
    with pytest.raises(ValueError):
        build_generator(tmp_path, IntegrationSettings(llm_provider="unknown"))


@pytest.mark.parametrize("content", [
    "DEEPSEEK_API_KEY=file-test-key\n",
    '# local secret\nDEEPSEEK_API_KEY="file-test-key"\n',
    "DEEPSEEK_API_KEY='file-test-key'\n",
])
def test_key_file_loaded_without_changing_environment(tmp_path, monkeypatch, content):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / ".env.deepseek"
    path.write_text(content, encoding="utf-8")
    client = DeepSeekClient.from_environment(key_file=path)
    assert client._api_key == "file-test-key"
    assert "DEEPSEEK_API_KEY" not in __import__("os").environ


def test_environment_key_precedes_file_and_empty_does_not_fallback(tmp_path, monkeypatch):
    path = tmp_path / ".env.deepseek"
    path.write_text("DEEPSEEK_API_KEY=file-test-key\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-test-key")
    assert DeepSeekClient.from_environment(key_file=path)._api_key == "environment-test-key"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    with pytest.raises(ModelUnavailableError):
        DeepSeekClient.from_environment(key_file=path)


@pytest.mark.parametrize("content", [
    "", "OTHER=example\n", "DEEPSEEK_API_KEY=a\nDEEPSEEK_API_KEY=b\n",
    "DEEPSEEK_API_KEY=\n", "DEEPSEEK_API_KEY=" + "x" * 4096,
])
def test_key_file_invalid_fails_without_leaking_content(tmp_path, monkeypatch, content):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / ".env.deepseek"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ModelUnavailableError) as exc:
        DeepSeekClient.from_environment(key_file=path)
    assert content not in str(exc.value) if content else True


def test_key_file_missing_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ModelUnavailableError, match="missing"):
        DeepSeekClient.from_environment(key_file=tmp_path / ".env.deepseek")


def prepare(root):
    # The sensitive marker stays in the local test CSV; do not send the row, column name, or literal to the model.
    source = root / "data.csv"
    source.write_bytes(b"private_person,private_date\nSECRET_PERSON,2026/10/31\nx,\n")
    profile, issues = scan(source)
    task_id = str(uuid4())
    date_issue = next(i for i in issues if i["column"] == "private_date")
    spec = freeze_spec(
        task_id=task_id, input_sha256=profile["input_sha256"], owner_id="SECRET_OWNER",
        task_version=1, columns=tuple(profile["columns"]),
        decisions=(Decision(issue_id=date_issue["issue_id"], column="private_date", action=Action.FILL_NULL_WITH_LITERAL,
                            predicate_id="empty_string", params=ActionParams(literal="SECRET_LITERAL"),
                            actor_id="SECRET_OWNER", task_version=1),), issues=tuple(issues),
    )
    spec_path, profile_path = root / "spec.json", root / "profile.json"
    spec_path.write_text(spec.model_dump_json())
    profile_path.write_text(json.dumps(profile))
    return spec, spec_path, profile_path


class MockModel:
    def __init__(self, texts):
        self.texts, self.calls = iter(texts), []

    async def complete(self, req):
        self.calls.append(req)
        result = next(self.texts)
        if isinstance(result, Exception):
            raise result
        return ModelResponse(request_id=req.request_id, provider="mock", model="mock",
                             text=result, finish_reason="completed", input_tokens=10, output_tokens=20)


def output(spec, source="import csv\n# genuine model response test double, never executed\n"):
    return json.dumps({"schema_version": "1.0", "spec_sha256": spec.spec_sha256, "source": source})


def test_model_failure_records_only_safe_code_and_never_retries(tmp_path):
    spec, spec_path, profile_path = prepare(tmp_path)
    model = MockModel([ModelUnavailableError("private-provider-detail", safe_code="MODEL_HTTP_401",
                                             input_tokens=10, output_tokens=20)])
    generator = LLMCodeGenerator(tmp_path, ModelGateway(model))
    with pytest.raises(ModelUnavailableError):
        call(generator.generate(str(spec_path), str(profile_path)))
    ledger = tmp_path / "workflows" / str(spec.task_id) / "llm" / "calls.sqlite3"
    with sqlite3.connect(ledger) as db:
        rows = db.execute("SELECT status,error_code,code_json FROM calls").fetchall()
    assert len(rows) == 1 and rows[0][:2] == ("failed", "MODEL_HTTP_401")
    assert json.loads(rows[0][2]) == {"input_tokens": 10, "output_tokens": 20}
    with pytest.raises(ModelUnavailableError, match="MODEL_CALL_UNCERTAIN_OR_FAILED"):
        call(generator.generate(str(spec_path), str(profile_path)))
    assert len(model.calls) == 1


def test_anonymous_prompt_hash_source_and_restart_cache(tmp_path):
    spec, spec_path, profile_path = prepare(tmp_path)
    model = MockModel([output(spec)])
    generator = LLMCodeGenerator(tmp_path, ModelGateway(model))
    version = call(generator.generate(str(spec_path), str(profile_path)))
    assert version.generation_kind == "llm_generate" and version.request_id
    assert hashlib.sha256(__import__("pathlib").Path(version.source_ref).read_bytes()).hexdigest() == version.code_sha256
    external = "".join(m.content for m in model.calls[0].messages)
    for secret in ("private_person", "private_date", "SECRET_PERSON", "SECRET_OWNER", "SECRET_LITERAL",
                   str(tmp_path), spec.input_sha256):
        assert secret not in external
    again = call(LLMCodeGenerator(tmp_path, ModelGateway(model)).generate(str(spec_path), str(profile_path)))
    assert asdict(again) == asdict(version) and len(model.calls) == 1


def test_scanner_facts_are_distinct_from_runtime_approved_spec(tmp_path):
    spec, spec_path, profile_path = prepare(tmp_path)
    model = MockModel([output(spec)])
    call(LLMCodeGenerator(tmp_path, ModelGateway(model)).generate(str(spec_path), str(profile_path)))
    system, user = model.calls[0].messages
    task = json.loads(user.content)["task"]
    assert task["context_version"] == "1.1"
    assert task["row_count"] == 2 and task["column_count"] == 2
    assert task["aggregates"][1] == {
        "column_index": 1, "missing_count": 1, "date_formats": {"YYYY/MM/DD": 1}
    }
    assert task["approved_rules"] == [{
        "column_index": 1, "action": "fill_null_with_literal",
        "predicate_id": "empty_string", "rule_version": "1.0", "input_formats": [],
        "on_unparseable": "preserve", "literal_source": "runtime spec decision params.literal",
    }]
    assert "do not exist in /input/spec.json" in system.content
    assert "decisions is a LIST" in system.content
    assert "Scanner counts and observed date formats are facts, not authorization" in system.content
    assert "decision.params.literal" in system.content
    assert "open and Path.open do not accept strict" in system.content
    assert 'open("/input/data.csv", "r", encoding="utf-8-sig", newline="")' in system.content
    assert "csv.reader(f, strict=True)" in system.content
    assert "organize the program into clearly named functions" in system.content
    assert "unexpected implementation exceptions visible" in system.content
    assert "bounded exception type, source line and function" in system.content
    assert "Do not print" in system.content
    for private in ("private_date", "SECRET_LITERAL", "SECRET_PERSON", "SECRET_OWNER"):
        assert private not in system.content + user.content


def test_format_regeneration_and_total_budget(tmp_path):
    spec, spec_path, profile_path = prepare(tmp_path)
    model = MockModel(["not JSON", output(spec, "def broken("), output(spec)])
    generator = LLMCodeGenerator(tmp_path, ModelGateway(model))
    version = call(generator.generate(str(spec_path), str(profile_path)))
    assert len(model.calls) == 3
    with pytest.raises(ModelUnavailableError, match="BUDGET"):
        call(generator.repair(version, "RUNTIME_ERROR", "PRIVATE_DATA traceback", executed(version)))
    assert len(model.calls) == 3


@pytest.mark.parametrize("prefix,suffix", [
    ("```json\n", "\n```"), (" \n```JSON\r\n", "\r\n```\n"),
])
def test_single_json_fence_is_accepted_without_extra_call(tmp_path, prefix, suffix):
    spec, spec_path, profile_path = prepare(tmp_path)
    model = MockModel([prefix + output(spec) + suffix])
    result = call(LLMCodeGenerator(tmp_path, ModelGateway(model)).generate(str(spec_path), str(profile_path)))
    assert result.spec_sha256 == spec.spec_sha256
    assert len(model.calls) == 1


@pytest.mark.parametrize("text", [
    'explanation\n```json\n{}\n```',
    '```json\n{}\n```\nextra',
    '```python\n{}\n```',
    '```json\n{}\n```\n```json\n{}\n```',
    '```json\n{}\n',
])
def test_json_fence_rejects_extra_or_incomplete_content(text):
    with pytest.raises(ValueError):
        parse_model_json(text)


def test_fenced_json_still_enforces_source_policy(tmp_path):
    spec, spec_path, profile_path = prepare(tmp_path)
    model = MockModel(["```json\n" + output(spec, "import os\n") + "\n```"])
    with pytest.raises(SourcePolicyError):
        call(LLMCodeGenerator(tmp_path, ModelGateway(model)).generate(str(spec_path), str(profile_path)))
    assert len(model.calls) == 1


@pytest.mark.parametrize("bad_response,expected_error", [
    ("not JSON", "SOURCE_FORMAT"),
    ("bad_syntax", "SYNTAX_ERROR"),
])
def test_repair_format_retry_preserves_original_failure_across_restart(tmp_path, bad_response, expected_error):
    spec, spec_path, profile_path = prepare(tmp_path)
    source = "def failing():\n    open('/input/data.csv', strict=True)\nfailing()\n"
    invalid = output(spec, "def broken(") if bad_response == "bad_syntax" else bad_response
    model = MockModel([output(spec, source), invalid, output(spec, "# repaired\n")])
    paused = True

    def check_active(_):
        if paused and len(model.calls) == 2:
            raise ValueError("paused")

    generator = LLMCodeGenerator(tmp_path, ModelGateway(model), check_active=check_active)
    old = call(generator.generate(str(spec_path), str(profile_path)))
    diagnostic = {"exception_type": "TypeError", "line": 2, "function": "failing",
                  "reason": "OPEN_STRICT_UNSUPPORTED"}
    with pytest.raises(ValueError, match="paused"):
        call(generator.repair(old, "RUNTIME_ERROR", diagnostic, executed(old)))
    ledger = tmp_path / "workflows" / str(spec.task_id) / "llm" / "calls.sqlite3"
    with sqlite3.connect(ledger) as db:
        invalid = db.execute("SELECT status,error_code,code_json FROM calls WHERE status='invalid'").fetchone()
    assert invalid[:2] == ("invalid", expected_error)
    assert json.loads(invalid[2]) == {"input_tokens": 10, "output_tokens": 20}
    paused = False
    call(generator.repair(old, "RUNTIME_ERROR", diagnostic, executed(old)))
    assert len(model.calls) == 3
    first, retry = [json.loads(item.messages[-1].content) for item in model.calls[1:]]
    for body in (first, retry):
        assert body["previous_source"] == source
        assert body["implementation_error_code"] == "RUNTIME_ERROR"
        assert body["runtime_diagnostic"] == diagnostic
        assert "put strict=True on csv.reader" in body["instruction"]
    assert "response_error_code" not in first
    assert retry["response_error_code"] == expected_error


def test_repair_same_spec_new_source_and_no_raw_logs(tmp_path):
    spec, spec_path, profile_path = prepare(tmp_path)
    model = MockModel([output(spec), output(spec, "import csv\n# repaired full source\n")])
    generator = LLMCodeGenerator(tmp_path, ModelGateway(model))
    old = call(generator.generate(str(spec_path), str(profile_path)))
    new = call(generator.repair(old, "RUNTIME_ERROR", "PRIVATE_ROW DEEPSEEK_API_KEY=secret", executed(old)))
    assert new.spec_sha256 == old.spec_sha256 and new.code_sha256 != old.code_sha256
    assert new.generation_kind == "llm_repair"
    assert "PRIVATE_ROW" not in str(model.calls[-1]) and "DEEPSEEK_API_KEY=secret" not in str(model.calls[-1])
    with pytest.raises(ValueError):
        call(generator.repair(new, "D_REJECTED", "", executed(new)))


def test_repair_includes_only_source_verified_runtime_diagnostic(tmp_path):
    spec, spec_path, profile_path = prepare(tmp_path)
    source = "def failing():\n    raise TypeError('fixed test message')\nfailing()\n"
    model = MockModel([output(spec, source), output(spec, "import csv\n# repaired\n")])
    generator = LLMCodeGenerator(tmp_path, ModelGateway(model))
    old = call(generator.generate(str(spec_path), str(profile_path)))
    diagnostic = {"exception_type": "TypeError", "line": 2, "function": "failing"}
    new = call(generator.repair(old, "RUNTIME_ERROR", diagnostic, executed(old)))
    body = json.loads(model.calls[-1].messages[-1].content)
    assert body["previous_source"] == source
    assert model.calls[0].messages[0] == model.calls[-1].messages[0]
    assert "failed execution attempt" in body["instruction"]
    assert "not necessarily the model's original response" in body["instruction"]
    assert body["runtime_diagnostic"] == diagnostic
    assert "untrusted hint" in body["instruction"]
    assert "SECRET_PERSON" not in str(model.calls[-1])
    assert "SECRET_LITERAL" not in str(model.calls[-1])
    assert asdict(call(generator.repair(old, "RUNTIME_ERROR", diagnostic, executed(old)))) == asdict(new)
    assert len(model.calls) == 2
    with pytest.raises(ValueError, match="model request"):
        call(generator.repair(old, "RUNTIME_ERROR", {
            **diagnostic, "function": "PRIVATE_ROW"
        }, executed(old)))


def test_repair_rejects_source_modified_after_version_was_recorded(tmp_path):
    spec, spec_path, profile_path = prepare(tmp_path)
    model = MockModel([output(spec)])
    generator = LLMCodeGenerator(tmp_path, ModelGateway(model))
    old = call(generator.generate(str(spec_path), str(profile_path)))
    from pathlib import Path
    Path(old.source_ref).write_text("# unapproved modification\n", encoding="utf-8")
    with pytest.raises(ValueError, match="executed source hash does not match"):
        call(generator.repair(old, "RUNTIME_ERROR", None, executed(old)))
    assert len(model.calls) == 1


def test_repair_uses_registered_execution_version_not_original_response(tmp_path):
    from dataclasses import replace
    from pathlib import Path

    spec, spec_path, profile_path = prepare(tmp_path)
    original = "def main():\n    pass\nmain()\n"
    executed_text = "def main():\n    raise RuntimeError()\nmain()\n"
    model = MockModel([output(spec, original), output(spec, "# repaired\n")])
    generator = LLMCodeGenerator(tmp_path, ModelGateway(model))
    first = call(generator.generate(str(spec_path), str(profile_path)))
    copy = Path(first.source_ref).parent / "registered-execution.py"
    copy.write_bytes(executed_text.encode("utf-8"))
    registered = replace(first, source_ref=str(copy),
                         code_sha256=hashlib.sha256(executed_text.encode("utf-8")).hexdigest())
    Path(first.source_ref).write_bytes(b"# generated file changed after execution\n")
    first = replace(first, request_id=None, source_ref=str(tmp_path / "unavailable-original.py"))
    diagnostic = {"exception_type": "RuntimeError", "line": 2, "function": "main"}
    call(generator.repair(first, "RUNTIME_ERROR", diagnostic, executed(registered)))
    body = json.loads(model.calls[-1].messages[-1].content)
    assert body["previous_source"] == executed_text != original
    assert body["runtime_diagnostic"] == diagnostic


def test_repair_sends_the_same_checked_bytes_even_if_source_changes_later(tmp_path):
    from pathlib import Path

    spec, spec_path, profile_path = prepare(tmp_path)
    source = "def failing():\r\n    raise RuntimeError()\r\nfailing()\r\n"
    model = MockModel([output(spec, source), output(spec, "# repaired\n")])

    class ChangeAfterCheck(LLMCodeGenerator):
        async def _generate(self, spec, safe, directory, identity, previous, error_code,
                            diagnostic=None, executed_source=None):
            if previous:
                Path(previous.source_ref).write_bytes(b"# changed after validation\n")
            return await super()._generate(spec, safe, directory, identity, previous, error_code,
                                           diagnostic, executed_source)

    generator = ChangeAfterCheck(tmp_path, ModelGateway(model))
    old = call(generator.generate(str(spec_path), str(profile_path)))
    diagnostic = {"exception_type": "RuntimeError", "line": 2, "function": "failing"}
    call(generator.repair(old, "RUNTIME_ERROR", diagnostic, executed(old)))
    body = json.loads(model.calls[-1].messages[-1].content)
    assert body["previous_source"].encode("utf-8") == source.encode("utf-8")
    assert body["runtime_diagnostic"] == diagnostic


def test_known_open_strict_reason_adds_fixed_hint(tmp_path):
    spec, spec_path, profile_path = prepare(tmp_path)
    source = "def failing():\n    open('/input/data.csv', strict=True)\nfailing()\n"
    model = MockModel([output(spec, source), output(spec, "# repaired\n")])
    generator = LLMCodeGenerator(tmp_path, ModelGateway(model))
    old = call(generator.generate(str(spec_path), str(profile_path)))
    diagnostic = {"exception_type": "TypeError", "line": 2, "function": "failing",
                  "reason": "OPEN_STRICT_UNSUPPORTED"}
    call(generator.repair(old, "RUNTIME_ERROR", diagnostic, executed(old)))
    body = json.loads(model.calls[-1].messages[-1].content)
    assert body["runtime_diagnostic"] == diagnostic
    assert "put strict=True on csv.reader" in body["instruction"]


@pytest.mark.parametrize("diagnostic", [
    {"exception_type": "TypeError", "line": 2, "function": "PRIVATE_ROW"},
    {"exception_type": "TypeError", "line": 2, "function": "failing", "message": "PRIVATE_ROW"},
    {"exception_type": "PRIVATE_ROW", "line": 2, "function": "failing"},
    "PRIVATE_ROW traceback",
])
def test_repair_drops_untrusted_diagnostics(tmp_path, diagnostic):
    spec, spec_path, profile_path = prepare(tmp_path)
    source = "def failing():\n    raise TypeError()\nfailing()\n"
    model = MockModel([output(spec, source), output(spec, "# repaired\n")])
    generator = LLMCodeGenerator(tmp_path, ModelGateway(model))
    old = call(generator.generate(str(spec_path), str(profile_path)))
    call(generator.repair(old, "RUNTIME_ERROR", diagnostic, executed(old)))
    body = json.loads(model.calls[-1].messages[-1].content)
    assert "runtime_diagnostic" not in body
    assert "PRIVATE_ROW" not in str(model.calls[-1])


def test_failed_or_uncertain_model_call_not_rebilled(tmp_path):
    spec, spec_path, profile_path = prepare(tmp_path)
    model = MockModel([ModelUnavailableError("offline")])
    generator = LLMCodeGenerator(tmp_path, ModelGateway(model))
    with pytest.raises(ModelUnavailableError):
        call(generator.generate(str(spec_path), str(profile_path)))
    with pytest.raises(ModelUnavailableError, match="UNCERTAIN"):
        call(generator.generate(str(spec_path), str(profile_path)))
    assert len(model.calls) == 1
    directory = tmp_path / "workflows" / str(spec.task_id) / "llm"
    with sqlite3.connect(directory / "calls.sqlite3") as db:
        db.execute("UPDATE calls SET status='started'")
    with pytest.raises(ModelUnavailableError, match="UNCERTAIN"):
        call(generator.generate(str(spec_path), str(profile_path)))
    assert len(model.calls) == 1


@pytest.mark.parametrize("source", ["import socket\n", "import os\n", "eval('x')", "import csv\ncsv.__dict__",
                                  "from json import *"])
def test_capability_requests_not_auto_repaired(source):
    with pytest.raises(SourcePolicyError):
        check_source(source)


def test_static_protocol_not_host_execution():
    check_source("import csv\nraise RuntimeError('must not execute')\n")
    with pytest.raises(SourceProtocolError):
        check_source("x" * 32769)


def test_spec_change_in_response_stops_without_retry(tmp_path):
    spec, spec_path, profile_path = prepare(tmp_path)
    wrong = json.loads(output(spec))
    wrong["spec_sha256"] = "a" * 64
    model = MockModel([json.dumps(wrong), output(spec)])
    generator = LLMCodeGenerator(tmp_path, ModelGateway(model))
    with pytest.raises(SourcePolicyError, match="SPEC_MISMATCH"):
        call(generator.generate(str(spec_path), str(profile_path)))
    assert len(model.calls) == 1


def test_cancel_during_bad_response_blocks_next_model_call(tmp_path):
    spec, spec_path, profile_path = prepare(tmp_path)
    cancelled = []

    class CancelModel(MockModel):
        async def complete(self, req):
            cancelled.append(True)
            return await super().complete(req)

    def check_active(task_id):
        assert task_id == str(spec.task_id)
        if cancelled:
            raise ValueError("cancelled")

    model = CancelModel(["not JSON", output(spec)])
    generator = LLMCodeGenerator(tmp_path, ModelGateway(model), check_active=check_active)
    with pytest.raises(ValueError, match="cancelled"):
        call(generator.generate(str(spec_path), str(profile_path)))
    assert len(model.calls) == 1
