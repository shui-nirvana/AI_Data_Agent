"""DeepSeek HTTP 适配器；密钥来自环境或本地文件，不进入业务记录或容器。"""

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
    """只读取固定格式的密钥，不执行文件内容，也不向环境写入密钥。"""
    try:
        if path.is_symlink() or not path.is_file():
            raise ModelUnavailableError("DeepSeek 密钥文件不存在或不是普通文件")
        with path.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ModelUnavailableError("DeepSeek 密钥文件超过大小限制")
        lines = raw.decode("utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        raise ModelUnavailableError("无法读取 DeepSeek 密钥文件") from None
    values = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name.strip() != "DEEPSEEK_API_KEY":
            raise ModelUnavailableError("DeepSeek 密钥文件只能包含 DEEPSEEK_API_KEY")
        value = value.strip()
        if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
            value = value[1:-1]
        values.append(value)
    if len(values) != 1:
        raise ModelUnavailableError("DeepSeek 密钥文件必须包含唯一的 DEEPSEEK_API_KEY")
    return values[0]


class DeepSeekClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, *, transport=None):
        if not api_key or api_key != api_key.strip() or any(ord(c) < 33 or ord(c) > 126 for c in api_key):
            raise ModelUnavailableError("DEEPSEEK_API_KEY 缺失或格式无效")
        if model not in MODELS:
            raise ModelUnavailableError("DeepSeek 模型不在已核对的支持列表中")
        self._api_key, self.model, self._transport = api_key, model, transport

    @classmethod
    def from_environment(cls, model: str | None = None, *, key_file: Path | None = None):
        # 明确设置的环境变量优先（包括空值，空值会报错而非偷偷切换密钥）。
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
            # 不跟随重定向、不读代理环境、无隐式重试，避免秘密外送和重复费用。
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
                                raise ModelUnavailableError("DeepSeek 响应超过配额")
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
            # 不读取或保存 reasoning_content、原始供应商报错、请求头。
            return ModelResponse(
                request_id=request.request_id, provider="deepseek", model=data["model"],
                text=text if isinstance(text, str) and text else "[no complete output]",
                finish_reason=reason, input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )
        except ModelUnavailableError:
            raise
        except (TimeoutError, httpx.HTTPError):
            raise ModelUnavailableError("DeepSeek 连接或总时限失败；未自动重试") from None
        except (KeyError, TypeError, ValueError):
            raise ModelUnavailableError("DeepSeek 响应协议无效") from None
