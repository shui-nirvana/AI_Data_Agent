"""Contract and profiler tests covering logical isolation boundaries.

"""

from uuid import uuid4
import pytest
from backend.jobs.contracts import JobRequest, Stage
from images.profiler.main import scan
from images.profiler.registry import registered_tools
from workers.sandbox_manager.runner import SandboxManager


def test_fixed_profiler_is_read_only_and_versioned(tmp_path):
    """Verify the profiler is read-only and reports tool versions."""
    csv_file = tmp_path / "input.csv"
    content = "amount,date\n-3,2026/10/31\n-3,2026/10/31\n5,\n"
    csv_file.write_text(content, encoding="utf-8")
    
    profile, issues = scan(csv_file)
    
    # Verify the original file is unchanged.
    assert csv_file.read_text(encoding="utf-8") == content
    # Verify basic statistics.
    assert profile["row_count"] == 3
    assert profile["duplicate_rows"] == 1
    assert profile["missing_by_column"]["date"] == 1
    # Verify every registered tool ran.
    assert {item["tool_id"] for item in profile["tool_runs"]} == {
        tool.tool_id for tool in registered_tools()
    }
    # Verify finding origin markers.
    assert all(issue["source"] == "fixed" for issue in issues)
    # Verify fixed tools do not currently offer automatic deletion.
    assert not any("delete" in issue.get("actions", []) for issue in issues)


def test_profiler_rejects_malformed_rows(tmp_path):
    """Verify the profiler rejects malformed rows."""
    csv_file = tmp_path / "input.csv"
    csv_file.write_text("a,b\n1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        scan(csv_file)


def test_jobs_require_frozen_and_hash_bound_references():
    """Verify job-request integrity and digest binding."""
    task_id, attempt_id = str(uuid4()), str(uuid4())
    digest = "a" * 64
    
    # C and D require a specification digest.
    with pytest.raises(ValueError, match="冻结"):
        JobRequest(task_id, attempt_id, Stage.CLEAN_C, digest)
        
    # D requires a candidate digest.
    with pytest.raises(ValueError, match="候选"):
        JobRequest(task_id, attempt_id, Stage.VERIFY_D, digest, spec_sha256=digest)
        
    # Verify SandboxManager fails explicitly without a runtime.
    request = JobRequest(task_id, attempt_id, Stage.PROFILE_A, digest)
    with pytest.raises(NotImplementedError, match="容器运行时"):
        SandboxManager().submit(request)
