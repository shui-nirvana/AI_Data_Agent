"""仅用内存测试替身验证端口替换，不代表任何云/模型已接入。"""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.bootstrap import build_dependencies
from backend.config import IntegrationSettings
from backend.jobs.contracts import JobRequest, Stage
from backend.jobs.execution import ExecutionReceipt, ExecutionStatus
from backend.jobs.policy import LIMITS
from backend.llm.client import (
    Message, ModelGateway, ModelRequest, ModelResponse, ModelUnavailableError,
)
from workers.sandbox_manager.runner import SandboxManager


class MemoryExecutor:
    """无容器、无 subprocess，仅记录门面调用及资源策略。"""

    def __init__(self, name):
        self.name = name
        self.jobs = []
        self.cancelled = []

    def submit(self, job, limits):
        self.jobs.append((job, limits))
        return ExecutionReceipt(job.task_id, job.attempt_id, f"{self.name}-handle")

    def status(self, receipt):
        return ExecutionStatus(receipt, "queued")

    def cancel(self, receipt):
        self.cancelled.append(receipt)


class MemoryModel:
    def __init__(self, provider="test"):
        self.provider = provider

    async def complete(self, request):
        return ModelResponse(
            request_id=request.request_id, provider=self.provider, model="test-model",
            text="print('test only')", finish_reason="completed",
        )


@pytest.mark.parametrize("name", ["worker-one", "worker-two"])
def test_execution_backend_is_replaceable(name):
    executor = MemoryExecutor(name)
    manager = SandboxManager(executor)
    job = JobRequest(str(uuid4()), str(uuid4()), Stage.PROFILE_A, "a" * 64)
    receipt = manager.submit(job)
    assert executor.jobs == [(job, LIMITS[Stage.PROFILE_A])]
    assert manager.status(receipt).state == "queued"
    manager.cancel(receipt)
    assert executor.cancelled == [receipt]


@pytest.mark.parametrize("platform", ["local", "vultr", "aws"])
def test_platform_does_not_enable_execution(tmp_path, platform):
    services = build_dependencies(IntegrationSettings(deployment_platform=platform))
    with TestClient(create_app(data_dir=tmp_path, demo_token="test", dependencies=services)) as api:
        assert api.get("/health").json()["execution_enabled"] is False
        assert api.app.state.dependencies.settings.deployment_platform == platform
    job = JobRequest(str(uuid4()), str(uuid4()), Stage.PROFILE_A, "a" * 64)
    with pytest.raises(NotImplementedError, match="容器运行时"):
        services.execution.submit(job, LIMITS[job.stage])


@pytest.mark.parametrize("provider", ["test-one", "test-two"])
def test_model_client_can_be_injected_without_provider_sdk(provider):
    request = ModelRequest(request_id="test", messages=(Message(role="user", content="test"),))
    response = asyncio.run(ModelGateway(MemoryModel(provider)).complete(request))
    assert response.request_id == "test"
    assert response.model == "test-model"
    assert response.provider == provider
    with pytest.raises(ModelUnavailableError):
        asyncio.run(ModelGateway().complete(request))


@pytest.mark.parametrize("finish_reason", ["length", "refused"])
def test_partial_model_output_never_counts_as_completed(finish_reason):
    class PartialModel:
        async def complete(self, request):
            return ModelResponse(request_id=request.request_id, provider="test", model="test",
                                 text="partial", finish_reason=finish_reason)
    request = ModelRequest(request_id="test", messages=(Message(role="user", content="test"),))
    with pytest.raises(ModelUnavailableError, match="完整"):
        asyncio.run(ModelGateway(PartialModel()).complete(request))


def test_unknown_configured_adapters_fail_closed():
    with pytest.raises(ValueError, match="执行适配器"):
        build_dependencies(IntegrationSettings(execution_backend="not-implemented"))
    with pytest.raises(ValueError, match="模型适配器"):
        build_dependencies(IntegrationSettings(llm_provider="not-implemented"))


def test_wrong_job_receipt_is_rejected():
    class WrongExecutor(MemoryExecutor):
        def submit(self, job, limits):
            return ExecutionReceipt(str(uuid4()), job.attempt_id, "wrong")
    with pytest.raises(ValueError, match="不匹配"):
        SandboxManager(WrongExecutor("test")).submit(
            JobRequest(str(uuid4()), str(uuid4()), Stage.PROFILE_A, "a" * 64)
        )
