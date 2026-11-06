import asyncio
import json
import os
from urllib.request import getproxies_environment

import httpx
import pytest

from backend.bootstrap import build_generator
from backend.config import IntegrationSettings
from backend.llm.client import Message, ModelGateway, ModelRequest, ModelUnavailableError
from backend.llm.generator import LLMCodeGenerator
from backend.llm.vultr import KEY_NAME, VultrInferenceClient, system_proxy


def request():
    return ModelRequest(request_id="test-request", messages=(Message(role="user", content="hello"),))


def response(reason="stop", text="source"):
    return {"model": "example-model", "choices": [
        {"finish_reason": reason, "message": {"role": "assistant", "content": text}}
    ], "usage": {"prompt_tokens": 15, "completion_tokens": 30}}


def test_vultr_sends_bounded_request_to_fixed_endpoint():
    def handler(req):
        assert str(req.url) == "https://api.vultrinference.com/v1/chat/completions"
        assert req.headers["Authorization"] == "Bearer test-key"
        body = json.loads(req.content)
        assert body == {"model": "example-model", "messages": [{"role": "user", "content": "hello"}],
                        "max_tokens": 4096, "reasoning": {"enabled": False}, "stream": False}
        return httpx.Response(200, json=response())

    client = VultrInferenceClient("test-key", "example-model", transport=httpx.MockTransport(handler))
    result = asyncio.run(ModelGateway(client).complete(request()))
    assert (result.provider, result.model, result.input_tokens, result.output_tokens) == (
        "vultr", "example-model", 15, 30)


def test_vultr_proxy_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AI_AGENT_VULTR_SYSTEM_PROXY_ENABLED", raising=False)
    monkeypatch.setattr("backend.llm.vultr.getproxies", lambda: pytest.fail("proxy must not be read"))
    assert system_proxy() is None


def test_proxy_switch_is_not_mistaken_for_a_proxy_address(monkeypatch):
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("AI_AGENT_VULTR_SYSTEM_PROXY_ENABLED", "true")
    assert getproxies_environment() == {}


@pytest.mark.parametrize("value", ["http://127.0.0.1:7890", "localhost:7890", "http://[::1]:1234"])
def test_vultr_proxy_requires_explicit_opt_in_and_local_address(monkeypatch, value):
    monkeypatch.setenv("AI_AGENT_VULTR_SYSTEM_PROXY_ENABLED", "true")
    monkeypatch.setenv(KEY_NAME, "test-key")
    monkeypatch.setattr("backend.llm.vultr.getproxies", lambda: {"https": value})
    client = VultrInferenceClient.from_environment("example-model")
    assert client._proxy == (value if "://" in value else "http://" + value)


@pytest.mark.parametrize("value", [
    None, "http://remote.example:7890", "http://user:password@localhost:7890",
    "socks5://localhost:7890", "http://localhost", "http://localhost:7890/path",
    "http://localhost:7890?query=x", "http://127.0.0.1:99999",
])
def test_vultr_proxy_rejects_untrusted_or_invalid_addresses(monkeypatch, value):
    monkeypatch.setenv("AI_AGENT_VULTR_SYSTEM_PROXY_ENABLED", "true")
    monkeypatch.setattr("backend.llm.vultr.getproxies", lambda: {"https": value} if value else {})
    with pytest.raises(ModelUnavailableError):
        system_proxy()


def test_vultr_proxy_rejects_invalid_switch(monkeypatch):
    monkeypatch.setenv("AI_AGENT_VULTR_SYSTEM_PROXY_ENABLED", "yes")
    with pytest.raises(ModelUnavailableError):
        system_proxy()


@pytest.mark.parametrize("scenario", [
    "401", "429", "redirect", "oversize", "length", "refused", "empty", "tool",
    "malformed", "timeout", "refusal_field", "invalid_choice", "invalid_message", "invalid_usage",
])
def test_vultr_fails_closed_without_retry_or_raw_error(scenario):
    calls = []

    def handler(req):
        calls.append(req)
        if scenario == "timeout":
            raise httpx.ReadTimeout("test-key sensitive error")
        if scenario in ("401", "429"):
            return httpx.Response(int(scenario), text="test-key sensitive error")
        if scenario == "redirect":
            return httpx.Response(302, headers={"Location": "https://example.com/"})
        if scenario == "oversize":
            return httpx.Response(200, content=b"x" * 1_000_001)
        if scenario == "malformed":
            return httpx.Response(200, content=b"{")
        if scenario == "invalid_choice":
            return httpx.Response(200, json={"choices": [None]})
        data = response("length" if scenario == "length" else
                        "content_filter" if scenario == "refused" else "stop",
                        "" if scenario == "empty" else "source")
        if scenario == "tool":
            data["choices"][0]["message"]["tool_calls"] = [{"id": "x"}]
        if scenario == "refusal_field":
            data["choices"][0]["message"]["refusal"] = "private refusal"
        if scenario == "invalid_message":
            data["choices"][0]["message"] = []
        if scenario == "invalid_usage":
            data["usage"] = "invalid"
        return httpx.Response(200, json=data)

    client = VultrInferenceClient("test-key", "example-model", transport=httpx.MockTransport(handler))
    with pytest.raises(ModelUnavailableError) as exc:
        asyncio.run(ModelGateway(client).complete(request()))
    assert len(calls) == 1
    assert "test-key" not in str(exc.value)
    assert "sensitive error" not in str(exc.value)
    if scenario in ("401", "429"):
        assert exc.value.safe_code == f"MODEL_HTTP_{scenario}"
    elif scenario == "timeout":
        assert exc.value.safe_code == "MODEL_TIMEOUT"
    elif scenario == "length":
        assert exc.value.safe_code == "MODEL_OUTPUT_LENGTH"
        assert (exc.value.input_tokens, exc.value.output_tokens) == (15, 30)
    elif scenario in ("refused", "refusal_field"):
        assert exc.value.safe_code == "MODEL_OUTPUT_REFUSED"


def test_vultr_key_file_and_environment_precedence(tmp_path, monkeypatch):
    path = tmp_path / ".env.vultr"
    path.write_text('VULTR_SERVERLESS_INFERENCE_API_KEY="file-key"\n', encoding="utf-8")
    monkeypatch.delenv(KEY_NAME, raising=False)
    assert VultrInferenceClient.from_environment("example-model", key_file=path)._api_key == "file-key"
    assert KEY_NAME not in __import__("os").environ
    monkeypatch.setenv(KEY_NAME, "env-key")
    assert VultrInferenceClient.from_environment("example-model", key_file=path)._api_key == "env-key"
    monkeypatch.setenv(KEY_NAME, "")
    with pytest.raises(ModelUnavailableError):
        VultrInferenceClient.from_environment("example-model", key_file=path)


@pytest.mark.parametrize("content", [
    "", "OTHER=secret\n", "VULTR_SERVERLESS_INFERENCE_API_KEY=a\n"
    "VULTR_SERVERLESS_INFERENCE_API_KEY=b\n", "VULTR_SERVERLESS_INFERENCE_API_KEY=" + "x" * 4096,
])
def test_vultr_invalid_key_files_fail_without_leaking(tmp_path, monkeypatch, content):
    monkeypatch.delenv(KEY_NAME, raising=False)
    path = tmp_path / ".env.vultr"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ModelUnavailableError) as exc:
        VultrInferenceClient.from_environment("example-model", key_file=path)
    assert content not in str(exc.value) if content else True


@pytest.mark.parametrize("model", [None, "", " ", "https://example.com", "m\nheader", "x" * 201])
def test_vultr_requires_an_explicit_valid_model(model):
    with pytest.raises(ModelUnavailableError):
        VultrInferenceClient("test-key", model)


def test_bootstrap_vultr_requires_consent_and_does_not_fallback(tmp_path, monkeypatch):
    settings = IntegrationSettings(llm_provider="vultr", llm_model="example-model")
    monkeypatch.setenv(KEY_NAME, "test-key")
    monkeypatch.delenv("AI_AGENT_LLM_ALLOW_EXTERNAL", raising=False)
    with pytest.raises(ValueError, match="授权"):
        build_generator(tmp_path, settings)
    monkeypatch.setenv("AI_AGENT_LLM_ALLOW_EXTERNAL", "true")
    generator = build_generator(tmp_path, settings)
    assert isinstance(generator, LLMCodeGenerator)
    assert isinstance(generator.gateway.client, VultrInferenceClient)
