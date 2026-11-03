"""受信组合入口：在启动时注入适配器，而不是在业务中按云/模型分支。"""

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
    """目前只内置 disabled；具体适配器由未来受信启动模块提供。

    切换到 AWS 只改变 deployment_platform，不会自动切换执行方式。
    配置一个未注入的执行/模型后端时直接失败，不默认联网或本机执行。
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
    """仅 Graph Runner 启动时选择模型；API 默认仍不构造带密钥的客户端。"""
    from backend.llm.generator import LLMCodeGenerator, UnconfiguredGenerator
    settings = settings if settings is not None else IntegrationSettings.from_environment()
    if settings.llm_provider == "disabled":
        return UnconfiguredGenerator()
    if settings.llm_provider != "deepseek":
        raise ValueError("模型提供方未实现，不自动切换")
    if __import__("os").environ.get("AI_AGENT_LLM_ALLOW_EXTERNAL") != "true":
        raise ValueError("请显式授权 AI_AGENT_LLM_ALLOW_EXTERNAL=true（仅匿名规范与聚合统计）")
    from backend.llm.deepseek import DeepSeekClient
    from backend.orchestration.store import WorkflowStore
    return LLMCodeGenerator(
        root, ModelGateway(DeepSeekClient.from_environment(settings.llm_model)),
        check_active=WorkflowStore(root).check_active,
    )
