"""显式启用的真实 Docker/Graph 集成测试，全部使用人工无敏感金样本。

AI_AGENT_RUN_DOCKER_TESTS=1 时运行；worker 始终是独立子进程。
清洗分支注入测试源码生成器，不调用真实模型，不冒充比赛 LLM 证据。
"""

import hashlib
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
from backend.domain.spec import FrozenSpec
from backend.llm.generator import CodeVersion
from backend.orchestration.runner import GraphRunner
from backend.orchestration.store import WorkflowStore
from workers.sandbox_manager.smoke_test import TEST_CODE

pytestmark = pytest.mark.skipif(os.environ.get("AI_AGENT_RUN_DOCKER_TESTS") != "1",
                                reason="Real Docker checks require explicit opt-in")


@pytest.mark.parametrize("change", [False, True])
def test_real_graph_approval_restart_and_independent_worker(tmp_path, change):
    manifest = Path(os.environ.get("AI_AGENT_DOCKER_IMAGES", ".runtime/docker/images.json")).resolve()
    assert manifest.is_file(), "Build the job image before enabling Docker tests"
    api = TestClient(create_app(data_dir=tmp_path, demo_token="test-secret"))
    headers = {"X-Demo-Token": "test-secret"}
    response = api.post("/api/tasks", content=b"id,date,amount\n1,2026/10/31,-3\n2,,5\n",
                        headers={**headers, "Content-Type": "text/csv", "X-Task-Goal": "Test"})
    task_id = response.json()["task_id"]
    assert api.post(f"/api/tasks/{task_id}/start", headers=headers).status_code == 202
    generator_calls = []

    class TestSourceGenerator:
        async def generate(self, spec_ref, profile_ref):
            generator_calls.append(True)
            spec = FrozenSpec.model_validate_json(Path(spec_ref).read_bytes())
            source = tmp_path / "test-source.py"
            source.write_text(TEST_CODE, encoding="utf-8")
            return CodeVersion(spec.spec_sha256, hashlib.sha256(source.read_bytes()).hexdigest(),
                               str(source), "test_source_not_llm")

    def runner():
        assert GraphRunner(tmp_path, TestSourceGenerator()).run_once()

    def worker():
        process = subprocess.run([
            sys.executable, "-m", "workers.sandbox_manager.queue_worker", "--once",
            "--data-dir", str(tmp_path), "--images", str(manifest),
        ], capture_output=True, text=True, timeout=120)
        assert process.returncode == 0, process.stderr
        assert "job processed=True" in process.stdout

    runner()
    worker()
    runner()
    review = api.get(f"/api/tasks/{task_id}/review", headers=headers).json()
    assert WorkflowStore(tmp_path).claim_job() is None
    body = {
        "task_version": review["task_version"],
        "decisions": [{
            "issue_id": issue["issue_id"], "column": issue["column"],
            "action": "normalize_date" if change and issue["column"] == "date" else "preserve",
            "predicate_id": "parseable_nonstandard_date" if change and issue["column"] == "date" else "all_cells",
            "params": {"input_formats": ["YYYY/MM/DD"]}
            if change and issue["column"] == "date" else {},
        } for issue in review["issues"]],
    }
    assert api.post(f"/api/tasks/{task_id}/decisions", json=body, headers=headers).status_code == 200
    assert api.post(f"/api/tasks/{task_id}/decisions", json=body, headers=headers).status_code == 200
    runner()
    if change:
        worker()
        runner()
    worker()
    runner()
    store = WorkflowStore(tmp_path)
    assert store.task(task_id)["state"] == "VALIDATED"
    validation = json.loads(store.flow(task_id)["validation_json"])
    assert validation["changed_cells"] == int(change)
    assert len(generator_calls) == int(change)
    with store.db() as db:
        jobs = db.execute("SELECT * FROM workflow_jobs ORDER BY rowid").fetchall()
    assert [job["stage"] for job in jobs] == (
        ["PROFILE_A", "CLEAN_C", "VERIFY_D"] if change else ["PROFILE_A", "VERIFY_D"]
    )
    assert len({job["job_id"] for job in jobs}) == len(jobs)
    for job in jobs:
        execution = json.loads((tmp_path / "worker_jobs" / job["job_id"] / "execution.json").read_text())
        assert execution["exit_code"] == 0 and execution["state"] == "finished"
    assert not GraphRunner(tmp_path).run_once()
