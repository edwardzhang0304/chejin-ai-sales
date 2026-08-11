from fastapi.testclient import TestClient

from app.core.database import Base, engine
from app.main import app


client = TestClient(app)
HEADERS = {
    "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
    "X-Operator-Name": "Ops Tester",
    "X-Operator-Role": "admin",
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


def _create_sales(name: str = "张伟") -> str:
    response = client.post(
        "/api/sales",
        json={"sales_name": name, "phone": "13900000001", "enabled": True, "sort_order": 10},
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()["data"]["id"]


def test_worker_token_only_visible_in_create_and_detail_not_list():
    worker = _create_worker()
    worker_id = worker["id"]
    assert worker_id
    assert len(worker_id) == 36
    assert worker["worker_token"].startswith("wkt_")

    list_payload = client.get("/api/workers").json()["data"]["items"][0]
    assert "worker_token" not in list_payload

    detail = client.get(f"/api/workers/{worker_id}").json()["data"]
    assert detail["worker_token"] == worker["worker_token"]


def test_worker_heartbeat_requires_valid_token_and_updates_status():
    worker = _create_worker()
    worker_id = worker["id"]

    invalid = client.post(
        f"/api/workers/{worker_id}/heartbeat",
        json={"running_status": "idle"},
        headers={"X-Worker-Token": "bad-token"},
    )
    assert invalid.status_code == 401
    assert invalid.json()["code"] == "WORKER_TOKEN_INVALID"

    response = client.post(
        f"/api/workers/{worker_id}/heartbeat",
        json={"running_status": "running", "current_task": "manual-check-001", "client_binding_state": "bound"},
        headers={"X-Worker-Token": worker["worker_token"]},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["online_status"] == "online"
    assert data["running_status"] == "running"
    assert data["current_task"] == "manual-check-001"
    assert data["last_heartbeat_at"]


def test_sales_can_bind_replace_and_clear_worker_with_constraints():
    worker_a = _create_worker("Worker A")
    worker_b = _create_worker("Worker B")
    sales_a = _create_sales("张伟")
    sales_b = _create_sales("王敏")

    bound = client.post(
        f"/api/sales/{sales_a}/worker-binding",
        json={"worker_id": worker_a["id"]},
        headers=HEADERS,
    )
    assert bound.status_code == 200
    assert bound.json()["data"]["current_worker"]["id"] == worker_a["id"]

    conflict = client.post(
        f"/api/sales/{sales_b}/worker-binding",
        json={"worker_id": worker_a["id"]},
        headers=HEADERS,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "WORKER_ALREADY_BOUND"

    replaced = client.post(
        f"/api/sales/{sales_a}/worker-binding",
        json={"worker_id": worker_b["id"]},
        headers=HEADERS,
    )
    assert replaced.status_code == 200
    assert replaced.json()["data"]["current_worker"]["id"] == worker_b["id"]

    cleared = client.delete(f"/api/sales/{sales_a}/worker-binding", headers=HEADERS)
    assert cleared.status_code == 200
    assert cleared.json()["data"]["current_worker"] is None


def test_disabled_worker_cannot_be_bound_but_offline_enabled_worker_can_be_bound():
    worker = _create_worker("Offline Enabled Worker")
    sales_id = _create_sales("张伟")

    bound = client.post(
        f"/api/sales/{sales_id}/worker-binding",
        json={"worker_id": worker["id"]},
        headers=HEADERS,
    )
    assert bound.status_code == 200
    assert bound.json()["data"]["current_worker"]["online_status"] == "offline"

    client.delete(f"/api/sales/{sales_id}/worker-binding", headers=HEADERS)
    disabled = client.post(f"/api/workers/{worker['id']}/disable", headers=HEADERS)
    assert disabled.status_code == 200

    rejected = client.post(
        f"/api/sales/{sales_id}/worker-binding",
        json={"worker_id": worker["id"]},
        headers=HEADERS,
    )
    assert rejected.status_code == 400
    assert rejected.json()["code"] == "WORKER_DISABLED_CANNOT_BIND"


def test_sales_detail_returns_worker_and_placeholder_blocking_task_count():
    worker = _create_worker()
    sales_id = _create_sales("张伟")
    client.post(f"/api/sales/{sales_id}/worker-binding", json={"worker_id": worker["id"]}, headers=HEADERS)

    detail = client.get(f"/api/sales/{sales_id}").json()["data"]

    assert detail["current_worker"]["id"] == worker["id"]
    assert detail["worker_status"]["id"] == worker["id"]
    assert detail["today_assignment_count"] == 0
    assert detail["blocking_task_count"] == 0


def test_reset_worker_binding_rotates_token_and_old_token_fails():
    worker = _create_worker()
    worker_id = worker["id"]
    old_token = worker["worker_token"]
    client.post(
        f"/api/workers/{worker_id}/heartbeat",
        json={"running_status": "running", "current_task": "task-in-progress"},
        headers={"X-Worker-Token": old_token},
    )

    reset = client.post(f"/api/workers/{worker_id}/reset-binding", json={"force": True}, headers=HEADERS)
    assert reset.status_code == 200
    data = reset.json()["data"]
    assert data["worker_token"] != old_token
    assert data["has_running_task"] is True
    assert data["warning"]
    assert data["client_binding_state"] == "reset_required"

    old_heartbeat = client.post(
        f"/api/workers/{worker_id}/heartbeat",
        json={"running_status": "idle"},
        headers={"X-Worker-Token": old_token},
    )
    assert old_heartbeat.status_code == 401

    bound = client.post(
        f"/api/workers/{worker_id}/client-bind",
        json={"worker_token": data["worker_token"], "client_instance_id": "client-after-reset"},
    )
    assert bound.status_code == 200

    new_heartbeat = client.post(
        f"/api/workers/{worker_id}/heartbeat",
        json={"running_status": "idle", "current_task": None, "client_instance_id": "client-after-reset"},
        headers={"X-Worker-Token": data["worker_token"]},
    )
    assert new_heartbeat.status_code == 200
