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
        raise ValueError("The configured execution adapter is not implemented or injected")
    if settings.llm_provider != "disabled" and model_client is None:
        raise ValueError("The configured model adapter is not implemented or injected")
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
        raise ValueError("The model provider is not implemented; no automatic fallback is allowed")
    if __import__("os").environ.get("AI_AGENT_LLM_ALLOW_EXTERNAL") != "true":
        raise ValueError("Explicitly authorize AI_AGENT_LLM_ALLOW_EXTERNAL=true, including executed source during repair")
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
