"""提供方无关的模型调用端口。模型 SDK、认证和响应解析留在适配器。"""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=131072)


class ModelRequest(BaseModel):
    """调用方只传已授权/脱敏的内容；端口本身不会自动完成数据脱敏。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    request_id: str = Field(min_length=1, max_length=200)
    messages: tuple[Message, ...] = Field(min_length=1, max_length=20)
    max_output_tokens: int = Field(default=4096, ge=1, le=16384)
    timeout_seconds: int = Field(default=60, ge=1, le=120)


class ModelResponse(BaseModel):
    """实际模型与用量由适配器归一化；不携带密钥/供应商原始报错。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    request_id: str
    provider: str
    model: str
    text: str = Field(min_length=1, max_length=262144)
    finish_reason: Literal["completed", "length", "refused"]
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class ModelClient(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class ModelUnavailableError(RuntimeError):
    """配置或能力不足；上层不得偷偷切换提供方造成未授权的数据外发。"""


class DisabledModelClient:
    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise ModelUnavailableError("尚未配置并授权具体模型适配器")


class ModelGateway:
    """应用只依赖此端口；截断/拒绝响应不可冒充完整清洗源码。"""

    def __init__(self, client: ModelClient | None = None) -> None:
        self.client = client if client is not None else DisabledModelClient()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self.client.complete(request)
        # 重新验证，防止适配器返回未经构造器校验的模型对象。
        response = ModelResponse.model_validate(response.model_dump())
        if response.request_id != request.request_id:
            raise ValueError("模型响应与请求不匹配")
        if response.finish_reason != "completed":
            raise ModelUnavailableError("模型未返回完整输出，禁止进入清洗执行")
        return response
