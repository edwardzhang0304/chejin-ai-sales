from fastapi.testclient import TestClient

from app.core.database import Base, engine
from app.main import app


client = TestClient(app)
HEADERS = {
    "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
    "X-Operator-Name": "Ops Tester",
    "X-Operator-Role": "admin",
}
DEPRECATED_TASK_TOP_LEVEL_FIELDS = {
    "customer_name",
    "lead_name",
    "lead_wechat",
    "executor_name",
    "worker_name",
    "started_at",
    "result_at",
}


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _create_worker(name: str = "Mac Worker A") -> dict:
    response = client.post(
        "/api/workers",
        json={"worker_name": name, "device_name": "Mac mini", "platform": "mac", "enabled": True},
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()["data"]


def _create_sales(name: str = "张伟", worker_id: str | None = None) -> str:
    payload = {"sales_name": name, "enabled": True, "sort_order": 10}
    if worker_id:
        payload["worker_id"] = worker_id
    response = client.post("/api/sales", json=payload, headers=HEADERS)
    assert response.status_code == 200
    return response.json()["data"]["id"]


def _create_lead(name: str = "王先生", phone: str = "13896676678") -> dict:
    response = client.post(
        "/api/leads",
        json={"customer_name": name, "phones": [phone], "remark": "预算 10 万"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()["data"]


def _first_task() -> dict:
    response = client.get("/api/tasks")
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert items
    return items[0]


def test_assigned_lead_creates_pending_add_friend_task_when_sales_has_worker():
    worker = _create_worker()
    sales_id = _create_sales(worker_id=worker["id"])
    lead = _create_lead()

    task = _first_task()

    assert task["task_type"] == "add_friend"
    assert task["status"] == "pending"
    assert task["result_code"] is None
    assert task["error_code"] is None
    assert task["block_code"] is None
    assert task["lead_id"] == lead["id"]
    assert task["sales_id"] == sales_id
    assert task["worker_id"] == worker["id"]
    assert "cancel" in {item["code"] for item in task["available_actions"]}


def test_task_list_and_detail_do_not_return_deprecated_flat_display_fields():
    worker = _create_worker()
    _create_sales(worker_id=worker["id"])
    _create_lead()

    list_item = _first_task()
    detail = client.get(f"/api/tasks/{list_item['id']}", headers=HEADERS).json()["data"]

    assert not DEPRECATED_TASK_TOP_LEVEL_FIELDS.intersection(list_item.keys())
    assert not DEPRECATED_TASK_TOP_LEVEL_FIELDS.intersection(detail.keys())
    assert list_item["business_object"]["lead"]["customer_name"] == "王先生"
    assert list_item["execution"]["worker"]["worker_name"] == worker["worker_name"]
    assert detail["business_object"]["lead"]["customer_name"] == "王先生"
    assert detail["execution"]["worker"]["worker_name"] == worker["worker_name"]


def test_assigned_lead_without_worker_creates_blocked_task_and_binding_unblocks_it():
    worker = _create_worker()
    sales_id = _create_sales()
    _create_lead()

    blocked = _first_task()
    assert blocked["status"] == "blocked"
    assert blocked["block_code"] == "SALES_WORKER_NOT_BOUND"

    detail_before = client.get(f"/api/sales/{sales_id}").json()["data"]
    assert detail_before["blocking_task_count"] == 1

    bind = client.post(f"/api/sales/{sales_id}/worker-binding", json={"worker_id": worker["id"]}, headers=HEADERS)
    assert bind.status_code == 200

    detail = client.get(f"/api/tasks/{blocked['id']}").json()["data"]
    assert detail["status"] == "pending"
    assert detail["worker_id"] == worker["id"]
    assert detail["block_code"] is None
    assert "unblocked" in [event["event_type"] for event in detail["events"]]


def test_task_list_filters_and_searches_by_name_task_id_and_phone_suffix():
    worker = _create_worker()
    _create_sales(worker_id=worker["id"])
    _create_lead(name="赵女士", phone="13912345678")
    task = _first_task()

    by_name = client.get("/api/tasks?keyword=赵女士").json()["data"]
    by_suffix = client.get("/api/tasks?keyword=5678").json()["data"]
    by_id = client.get(f"/api/tasks?keyword={task['id']}").json()["data"]
    by_status = client.get("/api/tasks?status=pending&task_type=add_friend").json()["data"]

    assert by_name["total"] == 1
    assert by_suffix["total"] == 1
    assert by_id["total"] == 1
    assert by_status["total"] == 1


def test_task_list_metrics_are_calculated_from_full_filtered_result_not_current_page():
    worker = _create_worker()
    _create_sales(worker_id=worker["id"])
    _create_lead(name="客户一", phone="13912345671")
    _create_lead(name="客户二", phone="13912345672")
    _create_lead(name="客户三", phone="13912345673")
    tasks = client.get("/api/tasks?page=1&page_size=10", headers=HEADERS).json()["data"]["items"]

    failed_task = tasks[0]
    cancelled_task = tasks[1]
    claim = client.post(f"/api/tasks/{failed_task['id']}/claim", json={"worker_id": worker["id"]}, headers=HEADERS)
    assert claim.status_code == 200
    failed = client.post(
        f"/api/tasks/{failed_task['id']}/fail",
        json={"error_code": "WECHAT_WINDOW_NOT_FOUND", "failure_step": "opening_add_contact"},
        headers=HEADERS,
    )
    assert failed.status_code == 200
    cancelled = client.post(f"/api/tasks/{cancelled_task['id']}/cancel", json={"reason": "运营取消"}, headers=HEADERS)
    assert cancelled.status_code == 200

    page = client.get("/api/tasks?page=1&page_size=1", headers=HEADERS).json()["data"]

    assert page["total"] == 3
    assert len(page["items"]) == 1
    assert page["metrics"] == {
        "blocked": 0,
        "pending": 1,
        "running": 0,
        "completed_today": 0,
        "failed_today": 1,
    }


def test_running_task_can_update_step_complete_invite_sent_and_terminal_cannot_cancel():
    worker = _create_worker()
    _create_sales(worker_id=worker["id"])
    _create_lead()
    task = _first_task()

    claimed = client.post(
        f"/api/tasks/{task['id']}/claim",
        json={"worker_id": worker["id"], "current_step": "searching_contact"},
        headers=HEADERS,
    )
    assert claimed.status_code == 200
    assert claimed.json()["data"]["status"] == "running"

    step = client.post(
        f"/api/tasks/{task['id']}/step",
        json={"current_step": "sending_invite", "remark": "已打开添加通讯录窗口"},
        headers=HEADERS,
    )
    assert step.status_code == 200
    assert step.json()["data"]["current_step"] == "sending_invite"

    completed = client.post(f"/api/tasks/{task['id']}/invite-sent", json={"remark": "邀请已发送"}, headers=HEADERS)
    assert completed.status_code == 200
    data = completed.json()["data"]
    assert data["status"] == "completed"
    assert data["result_code"] == "invite_sent"
    assert "invite_sent" != data["status"]

    rejected = client.post(f"/api/tasks/{task['id']}/cancel", json={"reason": "误操作"}, headers=HEADERS)
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "TASK_CANCEL_NOT_ALLOWED"

    events = client.get(f"/api/tasks/{task['id']}/events").json()["data"]["items"]
    assert {"claimed", "step_updated", "completed"}.issubset({event["event_type"] for event in events})
    assert client.get("/api/operation-logs?event_type=task_claimed").json()["data"]["total"] == 0
    assert client.get("/api/operation-logs?event_type=task_step_updated").json()["data"]["total"] == 0
    assert client.get("/api/operation-logs?event_type=task_completed").json()["data"]["total"] == 0
    worker_detail = client.get(f"/api/workers/{worker['id']}", headers=HEADERS).json()["data"]
    assert worker_detail["running_status"] == "idle"
    assert worker_detail["current_task"] is None
    assert worker_detail["current_step"] is None


def test_failed_task_has_no_retry_action_or_retry_endpoint_and_releases_worker():
    worker = _create_worker()
    _create_sales(worker_id=worker["id"])
    _create_lead()
    task = _first_task()

    client.post(f"/api/tasks/{task['id']}/claim", json={"worker_id": worker["id"]}, headers=HEADERS)
    failed = client.post(
        f"/api/tasks/{task['id']}/fail",
        json={"error_code": "WECHAT_WINDOW_NOT_FOUND", "failure_step": "opening_add_contact", "failure_remark": "窗口未出现"},
        headers=HEADERS,
    )
    assert failed.status_code == 200
    failed_data = failed.json()["data"]
    assert failed_data["status"] == "failed"
    assert failed_data["error_code"] == "WECHAT_WINDOW_NOT_FOUND"
    assert failed_data["result_code"] is None
    assert "retry" not in {item["code"] for item in failed_data["available_actions"]}

    retry = client.post(f"/api/tasks/{task['id']}/retry", json={"remark": "人工触发重试"}, headers=HEADERS)
    assert retry.status_code == 404
    worker_detail = client.get(f"/api/workers/{worker['id']}", headers=HEADERS).json()["data"]
    assert worker_detail["running_status"] == "idle"
    assert worker_detail["current_task"] is None
    assert worker_detail["current_step"] is None


def test_task_comment_writes_note_and_event():
    worker = _create_worker()
    _create_sales(worker_id=worker["id"])
    _create_lead()
    task = _first_task()

    comment = client.post(f"/api/tasks/{task['id']}/comments", json={"content": "运营备注：客户要求下午处理"}, headers=HEADERS)
    events = client.get(f"/api/tasks/{task['id']}/events").json()["data"]["items"]
    detail = client.get(f"/api/tasks/{task['id']}").json()["data"]

    assert comment.status_code == 200
    assert comment.json()["data"]["content"] == "运营备注：客户要求下午处理"
    assert "comment_added" in [event["event_type"] for event in events]
    assert detail["notes"][0]["content"] == "运营备注：客户要求下午处理"
