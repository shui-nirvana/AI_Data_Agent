"""Provider-independent model port; SDKs, authentication, and response parsing belong in adapters."""

from typing import Literal, Protocol
import re

from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=131072)


class ModelRequest(BaseModel):
    """Callers supply only authorized, sanitized content; this port does not sanitize automatically."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    request_id: str = Field(min_length=1, max_length=200)
    messages: tuple[Message, ...] = Field(min_length=1, max_length=20)
    max_output_tokens: int = Field(default=4096, ge=1, le=16384)
    timeout_seconds: int = Field(default=60, ge=1, le=120)


class ModelResponse(BaseModel):
    """Adapters normalize model and usage metadata without exposing credentials or raw provider errors."""

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
    """Missing configuration or capability; callers must not silently switch providers and transmit data without authorization."""

    def __init__(self, message: str, *, safe_code: str | None = None,
                 input_tokens: int | None = None, output_tokens: int | None = None):
        if safe_code is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", safe_code):
            raise ValueError("Invalid safe model error code")
        super().__init__(message)
        self.safe_code = safe_code
        self.input_tokens = input_tokens if type(input_tokens) is int and 0 <= input_tokens <= 2**31 - 1 else None
        self.output_tokens = output_tokens if type(output_tokens) is int and 0 <= output_tokens <= 2**31 - 1 else None


class DisabledModelClient:
    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise ModelUnavailableError("No model adapter has been configured and authorized")


class ModelGateway:
    """The application depends on this port; truncated or refused responses cannot be treated as complete cleaning source."""

    def __init__(self, client: ModelClient | None = None) -> None:
        self.client = client if client is not None else DisabledModelClient()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self.client.complete(request)
        # Revalidate to reject adapter objects that bypassed constructor validation.
        response = ModelResponse.model_validate(response.model_dump())
        if response.request_id != request.request_id:
            raise ValueError("Model response does not match the request")
        if response.finish_reason != "completed":
            raise ModelUnavailableError("Model output is incomplete; cleaning execution is blocked",
                                        safe_code=("MODEL_OUTPUT_LENGTH" if response.finish_reason == "length"
                                                   else "MODEL_OUTPUT_REFUSED"),
                                        input_tokens=response.input_tokens, output_tokens=response.output_tokens)
        return response
