"""真实 Docker 执行生成器产物。默认跳过；live 另需显式授权和配置密钥。"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("docker")

from fastapi.testclient import TestClient
from backend.app import create_app
from backend.llm.client import ModelGateway, ModelResponse
from backend.llm.deepseek import DeepSeekClient
from backend.llm.generator import LLMCodeGenerator
from backend.orchestration.runner import GraphRunner
from backend.orchestration.store import WorkflowStore
from workers.sandbox_manager.smoke_test import TEST_CODE

pytestmark = pytest.mark.skipif(os.environ.get("AI_AGENT_RUN_DOCKER_TESTS") != "1",
                                reason="Requires explicit real Docker opt-in")


@pytest.mark.parametrize("mode", ["repair", "missing", "security", "d_reject", "live_date", "live_fill"])
def test_generator_and_new_containers(tmp_path, mode):
    live = mode.startswith("live")
    if live and (os.environ.get("AI_AGENT_RUN_LIVE_LLM_TESTS") != "1"
                 or os.environ.get("AI_AGENT_LLM_ALLOW_EXTERNAL") != "true"):
        pytest.skip("Live DeepSeek test requires explicit external/cost opt-in")
    manifest = Path(os.environ.get("AI_AGENT_DOCKER_IMAGES", ".runtime/docker/images.json")).resolve()
    api = TestClient(create_app(data_dir=tmp_path, demo_token="test-secret"))
    headers = {"X-Demo-Token": "test-secret", "Content-Type": "text/csv", "X-Task-Goal": "Test"}
    task = api.post("/api/tasks", content=b"id,date,amount\n1,2026/10/31,-3\n2,,5\n", headers=headers).json()
    task_id = task["task_id"]
    api.post(f"/api/tasks/{task_id}/start", headers=headers)
    responses = []

    class MockModel:
        async def complete(self, request):
            context = json.loads(request.messages[-1].content)
            spec_hash = context["task"]["spec_sha256"]
            source = TEST_CODE
            if not responses:
                if mode == "repair":
                    source = "raise RuntimeError('PRIVATE_ROW must not be sent to model')\n"
                elif mode == "missing":
                    source = "# no output\n"
                elif mode == "security":
                    source = "open('/etc/passwd').read()\n"
                elif mode == "d_reject":
                    source = TEST_CODE.replace("row[1] =", "row[2] =")
            responses.append(request)
            assert "PRIVATE_ROW must not be sent" not in request.messages[-1].content or len(responses) > 1
            return ModelResponse(request_id=request.request_id, provider="test_mock_not_deepseek",
                                 model="test", finish_reason="completed",
                                 text=json.dumps({"schema_version": "1.0", "spec_sha256": spec_hash, "source": source}))

    client = DeepSeekClient.from_environment(os.environ.get("AI_AGENT_LLM_MODEL")) if live else MockModel()
    generator = LLMCodeGenerator(tmp_path, ModelGateway(client))

    def runner():
        assert GraphRunner(tmp_path, generator).run_once()

    def worker():
        # worker 子进程明确不继承模型密钥，Docker Env 更仅含固定作业标志。
        environment = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        process = subprocess.run([
            sys.executable, "-m", "workers.sandbox_manager.queue_worker", "--once",
            "--data-dir", str(tmp_path), "--images", str(manifest),
        ], capture_output=True, text=True, timeout=120, env=environment)
        assert process.returncode == 0, process.stderr
        assert "job processed=True" in process.stdout

    runner()
    worker()
    runner()
    review = api.get(f"/api/tasks/{task_id}/review", headers=headers).json()
    decisions = []
    for issue in review["issues"]:
        if issue["column"] == "date":
            fill = mode == "live_fill"
            decisions.append({
                "issue_id": issue["issue_id"], "column": "date",
                "action": "fill_null_with_literal" if fill else "normalize_date",
                "predicate_id": "empty_string" if fill else "parseable_nonstandard_date",
                "params": {"literal": "UNKNOWN"} if fill else {"input_formats": ["YYYY/MM/DD"]},
            })
        else:
            decisions.append({"issue_id": issue["issue_id"], "column": issue["column"],
                              "action": "preserve", "predicate_id": "all_cells", "params": {}})
    assert api.post(f"/api/tasks/{task_id}/decisions",
                    json={"task_version": review["task_version"], "decisions": decisions},
                    headers=headers).status_code == 200
    runner()
    store = WorkflowStore(tmp_path)
    # C 可能触发最多两次合法修复；每次有候选才启动 D。
    for _ in range(4):
        if store.task(task_id)["state"] in ("FAILED", "VALIDATED", "CANCELLED"):
            break
        worker()
        runner()
    assert store.task(task_id)["state"] == ("FAILED" if mode in ("security", "d_reject") else "VALIDATED")
    with store.db() as db:
        jobs = db.execute("SELECT * FROM workflow_jobs ORDER BY rowid").fetchall()
    stages = [job["stage"] for job in jobs]
    if live:
        assert stages[0] == "PROFILE_A" and stages[-1] == "VERIFY_D"
        assert 1 <= len(stages[1:-1]) <= 3 and set(stages[1:-1]) == {"CLEAN_C"}
    else:
        assert stages == (["PROFILE_A", "CLEAN_C"] if mode == "security" else
                          ["PROFILE_A", "CLEAN_C", "CLEAN_C", "VERIFY_D"] if mode in ("repair", "missing")
                          else ["PROFILE_A", "CLEAN_C", "VERIFY_D"])
    assert len({job["job_id"] for job in jobs}) == len(jobs)
    if not live:
        assert len(responses) == (2 if mode in ("repair", "missing") else 1)
    else:
        code = json.loads(store.flow(task_id)["code_json"])
        assert code["provider"] == "deepseek" and code["generation_kind"] in ("llm_generate", "llm_repair")
        assert json.loads(store.flow(task_id)["validation_json"])["changed_cells"] == 1
