"""
契约和画像扫描器测试，验证隔离执行的逻辑边界。
"""

from uuid import uuid4
import pytest
from backend.jobs.contracts import JobRequest, Stage
from images.profiler.main import scan
from images.profiler.registry import registered_tools
from workers.sandbox_manager.runner import SandboxManager


def test_fixed_profiler_is_read_only_and_versioned(tmp_path):
    """验证固定画像扫描器是只读的，且正确报告工具版本"""
    csv_file = tmp_path / "input.csv"
    content = "amount,date\n-3,2026/10/31\n-3,2026/10/31\n5,\n"
    csv_file.write_text(content, encoding="utf-8")
    
    profile, issues = scan(csv_file)
    
    # 验证原文件未被修改
    assert csv_file.read_text(encoding="utf-8") == content
    # 验证基础统计
    assert profile["row_count"] == 3
    assert profile["duplicate_rows"] == 1
    assert profile["missing_by_column"]["date"] == 1
    # 验证所有注册工具都已运行
    assert {item["tool_id"] for item in profile["tool_runs"]} == {
        tool.tool_id for tool in registered_tools()
    }
    # 验证问题来源标记
    assert all(issue["source"] == "fixed" for issue in issues)
    # 验证固定工具目前不提供自动删除动作
    assert not any("delete" in issue.get("actions", []) for issue in issues)


def test_profiler_rejects_malformed_rows(tmp_path):
    """验证扫描器能拒绝格式畸形的行（防御畸形文件）"""
    csv_file = tmp_path / "input.csv"
    csv_file.write_text("a,b\n1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        scan(csv_file)


def test_jobs_require_frozen_and_hash_bound_references():
    """验证作业请求的完整性检查，确保哈希绑定"""
    task_id, attempt_id = str(uuid4()), str(uuid4())
    digest = "a" * 64
    
    # C 和 D 阶段必须有规范哈希
    with pytest.raises(ValueError, match="冻结"):
        JobRequest(task_id, attempt_id, Stage.CLEAN_C, digest)
        
    # D 阶段必须有候选产物哈希
    with pytest.raises(ValueError, match="候选"):
        JobRequest(task_id, attempt_id, Stage.VERIFY_D, digest, spec_sha256=digest)
        
    # 验证 SandboxManager 在未接入运行时会正确报错
    request = JobRequest(task_id, attempt_id, Stage.PROFILE_A, digest)
    with pytest.raises(NotImplementedError, match="容器运行时"):
        SandboxManager().submit(request)
