"""部署位置与服务后端分开配置；此模块不导入任何云或模型 SDK。"""

import os

from pydantic import BaseModel, ConfigDict, Field


class IntegrationSettings(BaseModel):
    """名称只用于选择/记录适配器，不是该平台已经通过验收的声明。"""

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
