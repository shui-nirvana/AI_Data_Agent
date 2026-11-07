"""Vultr Serverless Inference adapter; only the trusted runner holds the key."""

import asyncio
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import getproxies

from backend.llm.client import ModelRequest, ModelResponse, ModelUnavailableError

ENDPOINT = "https://api.vultrinference.com/v1/chat/completions"
KEY_NAME = "VULTR_SERVERLESS_INFERENCE_API_KEY"
KEY_FILE = Path(__file__).resolve().parents[2] / ".env.vultr"


def system_proxy() -> str | None:
    """Use only an explicitly authorized local proxy, not arbitrary proxy environment settings."""
    enabled = os.environ.get("AI_AGENT_VULTR_SYSTEM_PROXY_ENABLED", "false")
    if enabled == "false":
        return None
    if enabled != "true":
        raise ModelUnavailableError("The Vultr proxy setting must be true or false")
    value = getproxies().get("https")
    if not value:
        raise ModelUnavailableError("The Vultr proxy is enabled, but no HTTPS system proxy was found")
    value = value if "://" in value else "http://" + value
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
                 and parsed.port is not None and 1 <= parsed.port <= 65535
                 and parsed.username is None and parsed.password is None
                 and parsed.path in ("", "/") and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ModelUnavailableError("Vultr only permits a local HTTP proxy without credentials")
    return value


def load_key_file(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise ModelUnavailableError("The Vultr inference key file is missing or is not a regular file")
        with path.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ModelUnavailableError("The Vultr inference key file exceeds the size limit")
        lines = raw.decode("utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        raise ModelUnavailableError("Cannot read the Vultr inference key file") from None
    values = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name.strip() != KEY_NAME:
            raise ModelUnavailableError("The Vultr key file may only contain the inference API key")
        value = value.strip()
        if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
            value = value[1:-1]
        values.append(value)
    if len(values) != 1:
        raise ModelUnavailableError("The Vultr key file must contain exactly one inference API key")
    return values[0]


class VultrInferenceClient:
    def __init__(self, api_key: str, model: str | None, *, transport=None, proxy=None):
        if (not isinstance(api_key, str) or not api_key or api_key != api_key.strip()
                or any(ord(c) < 33 or ord(c) > 126 for c in api_key)):
            raise ModelUnavailableError("The Vultr inference API key is missing or invalid")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", model):
            raise ModelUnavailableError("Configure a valid Vultr inference model ID")
        self._api_key, self.model, self._transport, self._proxy = api_key, model, transport, proxy

    @classmethod
    def from_environment(cls, model: str | None = None, *, key_file: Path | None = None):
        if KEY_NAME in os.environ:
            key = os.environ[KEY_NAME]
        else:
            key = load_key_file(key_file if key_file is not None else KEY_FILE)
        return cls(key, model, proxy=system_proxy())

    async def complete(self, request: ModelRequest) -> ModelResponse:
        import httpx

        payload = {
            "model": self.model,
            "messages": [message.model_dump() for message in request.messages],
            "max_tokens": request.max_output_tokens,
            "reasoning": {"enabled": False},
            "stream": False,
        }
        try:
            async with asyncio.timeout(request.timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=request.timeout_seconds, follow_redirects=False,
                    trust_env=False, transport=self._transport, proxy=self._proxy,
                ) as client:
                    async with client.stream(
                        "POST", ENDPOINT, json=payload,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                    ) as response:
                        if response.status_code != 200:
                            raise ModelUnavailableError(
                                f"Vultr Inference HTTP {response.status_code}",
                                safe_code=f"MODEL_HTTP_{response.status_code}",
                            )
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > 1_000_000:
                                raise ModelUnavailableError(
                                    "Vultr Inference response exceeds the size limit", safe_code="MODEL_RESPONSE_TOO_LARGE")
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError()
            choices = data["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError()
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ValueError()
            message = choice["message"]
            if not isinstance(message, dict):
                raise ValueError()
            if message.get("tool_calls") or message.get("role") != "assistant":
                raise ValueError()
            reason = {"stop": "completed", "length": "length", "content_filter": "refused"}.get(
                choice["finish_reason"]
            )
            if reason is None or not isinstance(data["model"], str) or len(data["model"]) > 200:
                raise ValueError()
            text = message.get("content")
            if message.get("refusal"):
                reason = "refused"
            if reason == "completed" and (not isinstance(text, str) or not text.strip()):
                raise ValueError()
            usage = data.get("usage") or {}
            if not isinstance(usage, dict):
                raise ValueError()
            return ModelResponse(
                request_id=request.request_id, provider="vultr", model=data["model"],
                text=text if isinstance(text, str) and text else "[no complete output]",
                finish_reason=reason, input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )
        except ModelUnavailableError:
            raise
        except (httpx.ConnectTimeout, httpx.ConnectError):
            raise ModelUnavailableError(
                "Vultr Inference connection failed; no automatic retry was made", safe_code="MODEL_CONNECT_FAILED") from None
        except (TimeoutError, httpx.TimeoutException):
            raise ModelUnavailableError(
                "Vultr Inference timed out; no automatic retry was made", safe_code="MODEL_TIMEOUT") from None
        except httpx.HTTPError:
            raise ModelUnavailableError(
                "Vultr Inference transport failed; no automatic retry was made", safe_code="MODEL_TRANSPORT_FAILED") from None
        except (KeyError, TypeError, ValueError):
            raise ModelUnavailableError(
                "Vultr Inference response format is invalid", safe_code="MODEL_INVALID_RESPONSE") from None
