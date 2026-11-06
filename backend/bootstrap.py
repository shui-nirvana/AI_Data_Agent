"""Trusted composition root: inject adapters at startup instead of branching on cloud or model in business logic."""

from dataclasses import dataclass
from pathlib import Path

from backend.config import IntegrationSettings
from backend.jobs.execution import DisabledExecutionBackend, ExecutionBackend
from backend.llm.client import ModelClient, ModelGateway


@dataclass(frozen=True)
class ApplicationDependencies:
    settings: IntegrationSettings
    execution: ExecutionBackend
    model: ModelGateway


def build_dependencies(
    settings: IntegrationSettings | None = None, *,
    execution: ExecutionBackend | None = None,
    model_client: ModelClient | None = None,
) -> ApplicationDependencies:
    """Only disabled is built in. A deployment label does not enable execution; reject unregistered adapters without falling back to network or local execution.



    """
    settings = settings if settings is not None else IntegrationSettings.from_environment()
    if settings.execution_backend != "disabled" and execution is None:
        raise ValueError("配置的执行适配器尚未实现或注入")
    if settings.llm_provider != "disabled" and model_client is None:
        raise ValueError("配置的模型适配器尚未实现或注入")
    return ApplicationDependencies(
        settings=settings,
        execution=execution if execution is not None else DisabledExecutionBackend(),
        model=ModelGateway(model_client),
    )


def build_generator(root: Path, settings: IntegrationSettings | None = None):
    """Select a model only in the Graph Runner; the API does not construct a credentialed client by default."""
    from backend.llm.generator import LLMCodeGenerator, UnconfiguredGenerator
    settings = settings if settings is not None else IntegrationSettings.from_environment()
    if settings.llm_provider == "disabled":
        return UnconfiguredGenerator()
    if settings.llm_provider not in ("deepseek", "vultr"):
        raise ValueError("模型提供方未实现，不自动切换")
    if __import__("os").environ.get("AI_AGENT_LLM_ALLOW_EXTERNAL") != "true":
        raise ValueError("请显式授权 AI_AGENT_LLM_ALLOW_EXTERNAL=true（包括修复时的执行源码）")
    if settings.llm_provider == "deepseek":
        from backend.llm.deepseek import DeepSeekClient
        client = DeepSeekClient.from_environment(settings.llm_model)
    else:
        from backend.llm.vultr import VultrInferenceClient
        client = VultrInferenceClient.from_environment(settings.llm_model)
    from backend.orchestration.store import WorkflowStore
    return LLMCodeGenerator(
        root, ModelGateway(client),
        check_active=WorkflowStore(root).check_active,
    )
