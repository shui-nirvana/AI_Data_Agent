"""Configure deployment location separately from service backends without importing cloud or model SDKs."""

import os

from pydantic import BaseModel, ConfigDict, Field


class IntegrationSettings(BaseModel):
    """Names select and record adapters; they do not assert platform acceptance."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    deployment_platform: str = Field(default="local", pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    execution_backend: str = Field(default="disabled", pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    llm_provider: str = Field(default="disabled", pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    llm_model: str | None = Field(default=None, min_length=1, max_length=200)

    @classmethod
    def from_environment(cls):
        return cls(
            deployment_platform=os.environ.get("AI_AGENT_DEPLOYMENT_PLATFORM", "local"),
            execution_backend=os.environ.get("AI_AGENT_EXECUTION_BACKEND", "disabled"),
            llm_provider=os.environ.get("AI_AGENT_LLM_PROVIDER", "disabled"),
            llm_model=os.environ.get("AI_AGENT_LLM_MODEL") or None,
        )
