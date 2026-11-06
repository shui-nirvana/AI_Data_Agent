"""API regression tests for uploads, persistence, authentication, and cancellation.

"""

from fastapi.testclient import TestClient
from backend.app import create_app


def test_page_discloses_goal_and_repair_data_boundaries(tmp_path):
    with client(tmp_path) as api:
        response = api.get("/")
    assert response.status_code == 200
    html = response.text
    assert 'aria-describedby="goal-help"' in html
    assert 'id="goal-help"' in html
    assert "业务目标目前仅作为任务描述保存，不会自动转换成清洗规则" in html
    assert "请在后续审批中逐列选择并确认清洗动作和参数" in html
    assert "自愈时还会发送失败作业刚刚实际执行的完整源码" in html
    assert "执行源码可能与模型原始输出不同" in html
    assert "若执行源码中包含这些内容或其他敏感信息，也会随源码一起发送" in html
    assert "本次解释请求只发送" in html
    assert "清洗生成与自愈的外发范围见上方隐私说明" in html
    assert "不提供多用户隔离" in html
    assert "共享屏幕或截图前请检查内容" in html
    assert html.index('id="explanation-section"') < html.index('id="evidence"')
    assert '<details>' in html


def test_sensitive_responses_are_not_cached_even_on_errors(tmp_path):
    with client(tmp_path) as api:
        task_id = upload(api).json()["task_id"]
        headers = {"X-Demo-Token": "test-secret"}
        for path, auth in [
            (f"/api/tasks/{task_id}", headers),
            (f"/api/tasks/{task_id}/events", headers),
            (f"/api/tasks/{task_id}/review", headers),
            (f"/api/tasks/{task_id}/evidence", headers),
            (f"/api/tasks/{task_id}/validation-explanation", headers),
            (f"/api/tasks/{task_id}/validation-explanation", {}),
            ("/ready", {}),
        ]:
            assert api.get(path, headers=auth).headers["cache-control"] == "no-store"
        assert api.post(f"/api/tasks/{task_id}/validation-explanation",
                        headers=headers).headers["cache-control"] == "no-store"


def client(tmp_path, limit=100):
    """Create a FastAPI test client."""
    return TestClient(create_app(data_dir=tmp_path, demo_token="test-secret", max_upload_bytes=limit))


def upload(api, body=b"date,amount\n2026/10/31,-3\n", token="test-secret"):
    """Simulate a CSV upload request."""
    return api.post(
        "/api/tasks",
        content=body,
        headers={
            "Content-Type": "text/csv",
            "X-Demo-Token": token,
            "X-Task-Goal": "Preserve refunds",
        },
    )


def test_upload_is_durable_but_does_not_execute(tmp_path):
    """Verify upload persistence without automatic execution."""
    with client(tmp_path) as api:
        result = upload(api)
        assert result.status_code == 202
        task = result.json()
        assert task["state"] == "UPLOADED"
        assert len(task["input_sha256"]) == 64
        
        # Verify the file was written to disk.
        stored = (tmp_path / "inputs" / f"{task['task_id']}.csv").read_bytes()
        assert stored == b"date,amount\n2026/10/31,-3\n"
        
        # Verify execution remains disabled.
        assert api.get("/health").json()["execution_enabled"] is False
        
        # Verify the API returns task details and initial events.
        headers = {"X-Demo-Token": "test-secret"}
        assert api.get(f"/api/tasks/{task['task_id']}", headers=headers).json() == task
        events = api.get(f"/api/tasks/{task['task_id']}/events", headers=headers).json()
        assert [event["kind"] for event in events["events"]] == ["UPLOADED"]

    # Verify persistence after recreating the client.
    with client(tmp_path) as api:
        assert api.get(f"/api/tasks/{task['task_id']}", headers=headers).json() == task
        # An unstarted or unprofiled task cannot be approved; downloads are not yet available.
        assert api.post(f"/api/tasks/{task['task_id']}/decisions", headers=headers).status_code == 409
        assert api.get(f"/api/tasks/{task['task_id']}/artifacts/output", headers=headers).status_code == 409
        assert api.get(f"/api/tasks/{task['task_id']}/validation-explanation", headers=headers).json()["status"] == "not_requested"
        assert api.post(f"/api/tasks/{task['task_id']}/validation-explanation", headers=headers).status_code == 409


def test_rejects_unauthorized_and_oversize_without_persisting(tmp_path):
    """Verify invalid authentication and oversized uploads are rejected without leftover files."""
    with client(tmp_path, limit=10) as api:
        # Authentication failure.
        assert upload(api, b"a\n1\n", token="wrong").status_code == 401
        # Oversized file.
        assert upload(api).status_code == 413
        # Empty file.
        assert upload(api, b"").status_code == 400
        # Ensure the inputs directory is empty.
        assert list((tmp_path / "inputs").iterdir()) == []


def test_cancel_is_idempotent_and_events_are_ordered(tmp_path):
    """Verify cancellation idempotence and event order."""
    with client(tmp_path) as api:
        task_id = upload(api).json()["task_id"]
        headers = {"X-Demo-Token": "test-secret"}
        # First cancellation.
        assert api.post(f"/api/tasks/{task_id}/cancel", headers=headers).json()["state"] == "CANCELLED"
        # Repeated cancellation leaves the version and state unchanged.
        assert api.post(f"/api/tasks/{task_id}/cancel", headers=headers).json()["version"] == 2
        
        events = api.get(f"/api/tasks/{task_id}/events", headers=headers).json()["events"]
        assert [event["kind"] for event in events] == ["UPLOADED", "CANCELLED"]
        # Verify event cursor queries.
        assert len(api.get(f"/api/tasks/{task_id}/events?after={events[0]['sequence']}", headers=headers).json()["events"]) == 1


def test_percent_encoded_chinese_goal_is_preserved(tmp_path):
    """Verify that a URL-encoded Chinese goal is decoded correctly."""
    with client(tmp_path) as api:
        response = api.post(
            "/api/tasks",
            content=b"date\n2026/10/31\n",
            headers={
                "Content-Type": "text/csv",
                "X-Demo-Token": "test-secret",
                "X-Task-Goal": "%E4%BF%9D%E7%95%99%E9%80%80%E6%AC%BE", # URL encoding of the Chinese test goal.
            },
        )
        assert response.status_code == 202
        assert response.json()["goal"] == "保留退款"
