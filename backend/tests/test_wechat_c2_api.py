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


def _create_worker() -> dict:
    response = client.post(
        "/api/workers",
        json={"worker_name": "Windows Worker", "device_name": "Windows PC", "platform": "windows", "enabled": True},
        headers=HEADERS,
    )
    assert response.status_code == 200
    worker = response.json()["data"]
    bind = client.post(
        f"/api/workers/{worker['id']}/client-bind",
        json={"worker_token": worker["worker_token"], "client_instance_id": "client-a"},
    )
    assert bind.status_code == 200
    heartbeat = client.post(
        f"/api/workers/{worker['id']}/heartbeat",
        json={
            "client_instance_id": "client-a",
            "run_status": "running",
            "rpa_component_status": "ready",
            "wechat_status": "logged_in",
            "running_status": "idle",
            "current_step": "wechat_scan_idle",
            "local_lock_summary": {"locked": False, "owner": None},
        },
        headers={"X-Worker-Token": worker["worker_token"]},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["data"]["current_step"] == "wechat_scan_idle"
    return worker


def _worker_headers(worker: dict) -> dict:
    return {"X-Worker-Token": worker["worker_token"], "X-Client-Instance-Id": "client-a"}


def _create_sales(worker_id: str) -> str:
    response = client.post(
        "/api/sales",
        json={"sales_name": "张伟", "enabled": True, "sort_order": 10, "worker_id": worker_id},
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()["data"]["id"]


def _create_lead(name: str, phone: str, custom_fields: dict | None = None) -> dict:
    response = client.post(
        "/api/leads",
        json={"customer_name": name, "phones": [phone], "remark": "预算 10 万", "custom_fields": custom_fields or {}},
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()["data"]


def _first_task() -> dict:
    response = client.get("/api/tasks", headers=HEADERS)
    assert response.status_code == 200
    return response.json()["data"]["items"][0]


def _pull_remark_code(worker: dict) -> str:
    pull = client.get(f"/api/workers/{worker['id']}/tasks/pull", headers=_worker_headers(worker))
    assert pull.status_code == 200
    return pull.json()["data"]["task"]["remark_code"]


def _scan_payload(remark_code: str | None, *, rpa_session_key: str = "wx-row-1") -> dict:
    candidates = [remark_code] if remark_code else []
    return {
        "scan_id": "scan-001",
        "sidecar_run_id": "sidecar-001",
        "wechat_account_hint": "wx-main",
        "started_at": "2026-06-22T10:00:00+08:00",
        "finished_at": "2026-06-22T10:00:02+08:00",
        "sessions": [
            {
                "rpa_session_key": rpa_session_key,
                "display_name": remark_code or "未知客户",
                "remark_code_candidates": candidates,
                "row_fingerprint": f"fingerprint-{rpa_session_key}",
                "unread_hint": True,
                "last_message_preview": "你好",
                "ocr_confidence": 0.98,
            }
        ],
        "evidence": {"screenshot": "local://scan.png"},
    }


def test_scan_result_binds_unique_remark_code_and_read_targets_returns_bound_session():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)

    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    assert scan.status_code == 200
    data = scan.json()["data"]
    assert data["next_action"] == "none"
    assert data["bound_count"] == 1
    binding = data["bindings"][0]
    assert binding["bind_status"] == "bound"
    assert binding["listen_status"] == "listening"
    assert binding["can_ingest_messages"] is True

    targets = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert targets.status_code == 200
    assert targets.json()["data"]["next_action"] == "none"
    assert targets.json()["data"]["targets"][0]["conversation_id"] == binding["conversation_id"]

    admin_binding = client.get(f"/api/conversations/{binding['conversation_id']}/wechat-binding", headers=HEADERS)
    assert admin_binding.status_code == 200
    assert admin_binding.json()["data"]["remark_code"] == remark_code


def test_scan_result_with_conflicting_remark_code_goes_needs_review_and_has_no_read_target():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678", {"remark_code": "CJ-CONFLICT"})
    _create_lead("李女士", "13896676679", {"remark_code": "CJ-CONFLICT"})

    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload("CJ-CONFLICT"),
        headers=_worker_headers(worker),
    )
    assert scan.status_code == 200
    binding = scan.json()["data"]["bindings"][0]
    assert binding["bind_status"] == "needs_review"
    assert binding["reason_code"] == "SESSION_REMARK_CODE_DUPLICATED"
    assert binding["can_ingest_messages"] is False

    targets = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert targets.json()["data"]["targets"] == []


def test_scan_result_without_remark_code_stays_unbound_and_message_ingest_is_rejected():
    worker = _create_worker()
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(None),
        headers=_worker_headers(worker),
    )
    assert scan.status_code == 200
    binding = scan.json()["data"]["bindings"][0]
    assert binding["bind_status"] == "unbound"
    assert binding["reason_code"] == "SESSION_REMARK_CODE_NOT_FOUND"

    ingest = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json={
            "read_run_id": "read-001",
            "conversation_id": binding["conversation_id"],
            "rpa_session_key": "wx-row-1",
            "messages": [{"dedupe_key": "msg-001", "sender_role_hint": "customer", "message_type": "text", "content": "你好"}],
        },
        headers=_worker_headers(worker),
    )
    assert ingest.status_code == 409
    assert ingest.json()["code"] == "MESSAGE_CONVERSATION_NOT_BOUND"


def test_message_ingest_is_idempotent_by_dedupe_key_and_returns_next_action_none():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    conversation_id = scan.json()["data"]["bindings"][0]["conversation_id"]

    payload = {
        "read_run_id": "read-001",
        "conversation_id": conversation_id,
        "rpa_session_key": "wx-row-1",
        "messages": [{"dedupe_key": "msg-001", "sender_role_hint": "customer", "message_type": "text", "content": "你好"}],
        "evidence": {"screenshot": "local://message.png"},
    }
    first = client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=payload, headers=_worker_headers(worker))
    assert first.status_code == 200
    assert first.json()["data"]["ingested_count"] == 1
    assert first.json()["data"]["next_action"] == "none"

    duplicated = client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=payload, headers=_worker_headers(worker))
    assert duplicated.status_code == 200
    assert duplicated.json()["data"]["ingested_count"] == 0
    assert duplicated.json()["data"]["duplicated_count"] == 1
    assert duplicated.json()["data"]["results"][0]["error_code"] == "MESSAGE_INGEST_DUPLICATED"
    assert duplicated.json()["data"]["next_action"] == "none"

    messages = client.get(f"/api/conversations/{conversation_id}/messages", headers=HEADERS)
    assert messages.status_code == 200
    assert len(messages.json()["data"]["items"]) == 1


def test_repeated_scan_after_worker_restart_keeps_single_binding_and_returns_already_bound():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    first = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    assert first.json()["data"]["bindings"][0]["bind_status"] == "bound"

    restarted_heartbeat = client.post(
        f"/api/workers/{worker['id']}/heartbeat",
        json={"client_instance_id": "client-a", "run_status": "running", "rpa_component_status": "ready", "running_status": "idle"},
        headers={"X-Worker-Token": worker["worker_token"]},
    )
    assert restarted_heartbeat.status_code == 200

    repeated = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    assert repeated.status_code == 200
    assert repeated.json()["data"]["bindings"][0]["bind_status"] == "already_bound"
    assert repeated.json()["data"]["bound_count"] == 1


def test_same_worker_same_remark_code_updates_existing_binding_even_display_name_or_session_key_changes():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("许聪", "13896676680", {"remark_code": "CJTEST01"})

    first_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-old")
    first_payload["sessions"][0]["display_name"] = "CJTEST01 许聪"
    first = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=first_payload, headers=_worker_headers(worker))
    assert first.status_code == 200
    first_binding = first.json()["data"]["bindings"][0]
    assert first_binding["bind_status"] == "bound"

    renamed_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-new")
    renamed_payload["scan_id"] = "scan-002"
    renamed_payload["sessions"][0]["display_name"] = "CJTEST01许聪"
    renamed_payload["sessions"][0]["row_fingerprint"] = "fingerprint-renamed"
    renamed = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=renamed_payload, headers=_worker_headers(worker))
    assert renamed.status_code == 200
    renamed_binding = renamed.json()["data"]["bindings"][0]
    assert renamed_binding["bind_status"] == "already_bound"
    assert renamed_binding["id"] == first_binding["id"]
    assert renamed_binding["conversation_id"] == first_binding["conversation_id"]
    assert renamed_binding["display_name"] == "CJTEST01许聪"
    assert renamed_binding["rpa_session_key"] == "wx-row-new"
    assert renamed_binding["reason_code"] is None

    bindings = client.get(f"/api/leads/{first_binding['lead_id']}/wechat-bindings", headers=HEADERS)
    assert bindings.status_code == 200
    assert len(bindings.json()["data"]["items"]) == 1
