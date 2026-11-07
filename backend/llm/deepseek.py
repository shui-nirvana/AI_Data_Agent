"""DeepSeek HTTP adapter; credentials come from the environment or a local file and never enter task records or containers."""

import asyncio
import json
import os
from pathlib import Path

from backend.llm.client import ModelRequest, ModelResponse, ModelUnavailableError

ENDPOINT = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
MODELS = {"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"}
KEY_FILE = Path(__file__).resolve().parents[2] / ".env.deepseek"


def load_key_file(path: Path) -> str:
    """Read a key in the fixed format without executing the file or writing the key to the environment."""
    try:
        if path.is_symlink() or not path.is_file():
            raise ModelUnavailableError("The DeepSeek key file is missing or is not a regular file")
        with path.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ModelUnavailableError("The DeepSeek key file exceeds the size limit")
        lines = raw.decode("utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        raise ModelUnavailableError("Cannot read the DeepSeek key file") from None
    values = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name.strip() != "DEEPSEEK_API_KEY":
            raise ModelUnavailableError("The DeepSeek key file may only contain DEEPSEEK_API_KEY")
        value = value.strip()
        if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
            value = value[1:-1]
        values.append(value)
    if len(values) != 1:
        raise ModelUnavailableError("The DeepSeek key file must contain exactly one DEEPSEEK_API_KEY")
    return values[0]


class DeepSeekClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, *, transport=None):
        if not api_key or api_key != api_key.strip() or any(ord(c) < 33 or ord(c) > 126 for c in api_key):
            raise ModelUnavailableError("DEEPSEEK_API_KEY is missing or invalid")
        if model not in MODELS:
            raise ModelUnavailableError("The DeepSeek model is not in the verified supported list")
        self._api_key, self.model, self._transport = api_key, model, transport

    @classmethod
    def from_environment(cls, model: str | None = None, *, key_file: Path | None = None):
        # An explicitly set environment variable takes precedence, including an empty value that must fail.
        if "DEEPSEEK_API_KEY" in os.environ:
            key = os.environ["DEEPSEEK_API_KEY"]
        else:
            key = load_key_file(key_file if key_file is not None else KEY_FILE)
        return cls(key, model or DEFAULT_MODEL)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        import httpx

        payload = {
            "model": self.model,
            "messages": [m.model_dump() for m in request.messages],
            "max_tokens": request.max_output_tokens,
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        try:
            # Do not follow redirects, trust proxy environment settings, or retry implicitly.
            async with asyncio.timeout(request.timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=request.timeout_seconds, follow_redirects=False,
                    trust_env=False, transport=self._transport,
                ) as client:
                    async with client.stream(
                        "POST", ENDPOINT, json=payload,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                    ) as response:
                        if response.status_code != 200:
                            raise ModelUnavailableError(f"DeepSeek HTTP {response.status_code}")
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > 1_000_000:
                                raise ModelUnavailableError("DeepSeek response exceeds the size limit")
            data = json.loads(content)
            choices = data["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError()
            choice = choices[0]
            message = choice["message"]
            if message.get("tool_calls") or message.get("role") != "assistant":
                raise ValueError()
            reason = {"stop": "completed", "length": "length", "content_filter": "refused"}.get(
                choice["finish_reason"]
            )
            if reason is None or not isinstance(data["model"], str) or len(data["model"]) > 200:
                raise ValueError()
            text = message.get("content")
            if reason == "completed" and (not isinstance(text, str) or not text.strip()):
                raise ValueError()
            usage = data.get("usage") or {}
            # Do not read or store reasoning_content, raw provider errors, or request headers.
            return ModelResponse(
                request_id=request.request_id, provider="deepseek", model=data["model"],
                text=text if isinstance(text, str) and text else "[no complete output]",
                finish_reason=reason, input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )
        except ModelUnavailableError:
            raise
        except (TimeoutError, httpx.HTTPError):
            raise ModelUnavailableError("DeepSeek connection or overall timeout failed; no automatic retry was made") from None
        except (KeyError, TypeError, ValueError):
            raise ModelUnavailableError("DeepSeek response format is invalid") from None
