"""模型 HTTP/提示词/源码版本测试；不外发真实密钥，不在宿主运行生成源码。"""

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
from backend.llm.client import Message, ModelGateway, ModelRequest, ModelResponse, ModelUnavailableError
from backend.llm.deepseek import DeepSeekClient
from backend.llm.generator import LLMCodeGenerator, SourcePolicyError, SourceProtocolError, check_source
from images.profiler.main import scan


def call(coro):
    return asyncio.run(coro)


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
    with pytest.raises(ValueError, match="授权"):
        build_generator(tmp_path, settings)
    monkeypatch.setenv("AI_AGENT_LLM_ALLOW_EXTERNAL", "true")
    with pytest.raises(ModelUnavailableError, match="缺失"):
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
    with pytest.raises(ModelUnavailableError, match="不存在"):
        DeepSeekClient.from_environment(key_file=tmp_path / ".env.deepseek")


def prepare(root):
    # 敏感标记只在本地测试 CSV；不把该行/列名/字面值传给模型。
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
    for private in ("private_date", "SECRET_LITERAL", "SECRET_PERSON", "SECRET_OWNER"):
        assert private not in system.content + user.content


def test_format_regeneration_and_total_budget(tmp_path):
    spec, spec_path, profile_path = prepare(tmp_path)
    model = MockModel(["not JSON", output(spec, "def broken("), output(spec)])
    generator = LLMCodeGenerator(tmp_path, ModelGateway(model))
    version = call(generator.generate(str(spec_path), str(profile_path)))
    assert len(model.calls) == 3
    with pytest.raises(ModelUnavailableError, match="BUDGET"):
        call(generator.repair(version, "RUNTIME_ERROR", "PRIVATE_DATA traceback"))
    assert len(model.calls) == 3


def test_repair_same_spec_new_source_and_no_raw_logs(tmp_path):
    spec, spec_path, profile_path = prepare(tmp_path)
    model = MockModel([output(spec), output(spec, "import csv\n# repaired full source\n")])
    generator = LLMCodeGenerator(tmp_path, ModelGateway(model))
    old = call(generator.generate(str(spec_path), str(profile_path)))
    new = call(generator.repair(old, "RUNTIME_ERROR", "PRIVATE_ROW DEEPSEEK_API_KEY=secret"))
    assert new.spec_sha256 == old.spec_sha256 and new.code_sha256 != old.code_sha256
    assert new.generation_kind == "llm_repair"
    assert "PRIVATE_ROW" not in str(model.calls[-1]) and "DEEPSEEK_API_KEY=secret" not in str(model.calls[-1])
    with pytest.raises(ValueError):
        call(generator.repair(new, "D_REJECTED", ""))


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
