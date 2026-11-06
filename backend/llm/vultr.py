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
        raise ModelUnavailableError("Vultr 代理开关只能为 true 或 false")
    value = getproxies().get("https")
    if not value:
        raise ModelUnavailableError("已启用 Vultr 代理，但未发现 HTTPS 系统代理")
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
        raise ModelUnavailableError("Vultr 只允许无凭据的本地 HTTP 代理")
    return value


def load_key_file(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise ModelUnavailableError("Vultr 推理密钥文件不存在或不是普通文件")
        with path.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ModelUnavailableError("Vultr 推理密钥文件超过大小限制")
        lines = raw.decode("utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        raise ModelUnavailableError("无法读取 Vultr 推理密钥文件") from None
    values = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name.strip() != KEY_NAME:
            raise ModelUnavailableError("Vultr 密钥文件只能包含推理 API Key")
        value = value.strip()
        if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
            value = value[1:-1]
        values.append(value)
    if len(values) != 1:
        raise ModelUnavailableError("Vultr 密钥文件必须包含唯一的推理 API Key")
    return values[0]


class VultrInferenceClient:
    def __init__(self, api_key: str, model: str | None, *, transport=None, proxy=None):
        if (not isinstance(api_key, str) or not api_key or api_key != api_key.strip()
                or any(ord(c) < 33 or ord(c) > 126 for c in api_key)):
            raise ModelUnavailableError("Vultr 推理 API Key 缺失或格式无效")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", model):
            raise ModelUnavailableError("请明确配置有效的 Vultr 推理模型 ID")
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
                                    "Vultr Inference 响应超过配额", safe_code="MODEL_RESPONSE_TOO_LARGE")
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
                "Vultr Inference 连接失败；未自动重试", safe_code="MODEL_CONNECT_FAILED") from None
        except (TimeoutError, httpx.TimeoutException):
            raise ModelUnavailableError(
                "Vultr Inference 调用超时；未自动重试", safe_code="MODEL_TIMEOUT") from None
        except httpx.HTTPError:
            raise ModelUnavailableError(
                "Vultr Inference 传输失败；未自动重试", safe_code="MODEL_TRANSPORT_FAILED") from None
        except (KeyError, TypeError, ValueError):
            raise ModelUnavailableError(
                "Vultr Inference 响应协议无效", safe_code="MODEL_INVALID_RESPONSE") from None
