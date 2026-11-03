"""
API 回归测试，验证上传、持久化、鉴权和任务取消功能。
"""

from fastapi.testclient import TestClient
from backend.app import create_app


def client(tmp_path, limit=100):
    """创建用于测试的 FastAPI 客户端"""
    return TestClient(create_app(data_dir=tmp_path, demo_token="test-secret", max_upload_bytes=limit))


def upload(api, body=b"date,amount\n2026/10/31,-3\n", token="test-secret"):
    """辅助函数：模拟 CSV 文件上传请求"""
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
    """验证上传是持久化的，但目前不会触发自动执行"""
    with client(tmp_path) as api:
        result = upload(api)
        assert result.status_code == 202
        task = result.json()
        assert task["state"] == "UPLOADED"
        assert len(task["input_sha256"]) == 64
        
        # 验证文件是否正确存入磁盘
        stored = (tmp_path / "inputs" / f"{task['task_id']}.csv").read_bytes()
        assert stored == b"date,amount\n2026/10/31,-3\n"
        
        # 验证执行开关处于关闭状态
        assert api.get("/health").json()["execution_enabled"] is False
        
        # 验证可以通过 API 获取任务详情和初始事件
        headers = {"X-Demo-Token": "test-secret"}
        assert api.get(f"/api/tasks/{task['task_id']}", headers=headers).json() == task
        events = api.get(f"/api/tasks/{task['task_id']}/events", headers=headers).json()
        assert [event["kind"] for event in events["events"]] == ["UPLOADED"]

    # 验证重启后（重新创建客户端）数据依然存在
    with client(tmp_path) as api:
        assert api.get(f"/api/tasks/{task['task_id']}", headers=headers).json() == task
        # 未启动/未检测任务不能审批；下载仍未接入。
        assert api.post(f"/api/tasks/{task['task_id']}/decisions", headers=headers).status_code == 409
        assert api.get(f"/api/tasks/{task['task_id']}/artifacts/output", headers=headers).status_code == 501


def test_rejects_unauthorized_and_oversize_without_persisting(tmp_path):
    """验证非法鉴权和超大文件会被拒绝，且不会在磁盘留下残留"""
    with client(tmp_path, limit=10) as api:
        # 鉴权失败
        assert upload(api, b"a\n1\n", token="wrong").status_code == 401
        # 文件超大
        assert upload(api).status_code == 413
        # 文件为空
        assert upload(api, b"").status_code == 400
        # 确保 inputs 目录为空
        assert list((tmp_path / "inputs").iterdir()) == []


def test_cancel_is_idempotent_and_events_are_ordered(tmp_path):
    """验证取消操作的幂等性以及事件流的顺序"""
    with client(tmp_path) as api:
        task_id = upload(api).json()["task_id"]
        headers = {"X-Demo-Token": "test-secret"}
        # 首次取消
        assert api.post(f"/api/tasks/{task_id}/cancel", headers=headers).json()["state"] == "CANCELLED"
        # 再次取消（版本号不增加，状态保持）
        assert api.post(f"/api/tasks/{task_id}/cancel", headers=headers).json()["version"] == 2
        
        events = api.get(f"/api/tasks/{task_id}/events", headers=headers).json()["events"]
        assert [event["kind"] for event in events] == ["UPLOADED", "CANCELLED"]
        # 验证事件游标查询
        assert len(api.get(f"/api/tasks/{task_id}/events?after={events[0]['sequence']}", headers=headers).json()["events"]) == 1


def test_percent_encoded_chinese_goal_is_preserved(tmp_path):
    """验证通过 URL 编码传输的中文目标能被正确解析"""
    with client(tmp_path) as api:
        response = api.post(
            "/api/tasks",
            content=b"date\n2026/10/31\n",
            headers={
                "Content-Type": "text/csv",
                "X-Demo-Token": "test-secret",
                "X-Task-Goal": "%E4%BF%9D%E7%95%99%E9%80%80%E6%AC%BE", # "保留退款" 的编码
            },
        )
        assert response.status_code == 202
        assert response.json()["goal"] == "保留退款"
