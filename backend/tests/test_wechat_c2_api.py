from fastapi.testclient import TestClient

from app.core.database import Base, engine
from app.main import app
from app.models.base import utcnow
from app.models.c3 import Conversation, MessageBatch
from app.models.task import Task
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.core.database import SessionLocal


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


def test_scan_result_binds_unique_remark_code_and_read_targets_waits_for_state_machine_reason():
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
    assert targets.json()["data"]["targets"] == []

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        conversation.last_ai_reply_at = utcnow()
        db.commit()

    state_targets = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert state_targets.status_code == 200
    read_target = state_targets.json()["data"]["targets"][0]
    assert read_target["conversation_id"] == binding["conversation_id"]
    assert read_target["lead_id"] == binding["lead_id"]
    assert read_target["remark_code"] == remark_code
    assert read_target["rpa_session_key"] == "wx-row-1"
    assert read_target["display_name"] == remark_code
    assert "last_ingested_at" in read_target
    assert read_target["read_reason"] == "recent_ai_sent"
    assert read_target["row_fingerprint"] == "fingerprint-wx-row-1"
    assert read_target["ocr_confidence"] == 0.98

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
    assert binding["error_code"] == "SESSION_REMARK_CODE_DUPLICATED"
    assert "reason_code" not in binding
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
    assert binding["error_code"] == "SESSION_REMARK_CODE_NOT_FOUND"
    assert "reason_code" not in binding

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
    assert ingest.json()["trace_id"]


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
    assert duplicated.json()["data"]["results"][0]["ingest_result"] == "duplicated"
    assert "ingest_status" not in duplicated.json()["data"]["results"][0]
    assert "conversation_status" not in duplicated.json()["data"]
    assert duplicated.json()["data"]["next_action"] == "none"
    assert duplicated.json()["trace_id"]

    messages = client.get(f"/api/conversations/{conversation_id}/messages", headers=HEADERS)
    assert messages.status_code == 200
    assert len(messages.json()["data"]["items"]) == 1
    with SessionLocal() as db:
        assert db.query(MessageBatch).count() == 0


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

    repeated_payload = _scan_payload(remark_code)
    repeated_payload["scan_id"] = "scan-002"
    repeated = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=repeated_payload, headers=_worker_headers(worker))
    assert repeated.status_code == 200
    assert repeated.json()["data"]["bindings"][0]["bind_status"] == "already_bound"
    assert repeated.json()["data"]["bound_count"] == 1


def test_duplicate_scan_id_returns_first_result_without_rebinding():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    first_payload = _scan_payload(remark_code, rpa_session_key="wx-row-1")
    first = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=first_payload, headers=_worker_headers(worker))
    assert first.status_code == 200
    first_binding = first.json()["data"]["bindings"][0]

    duplicate_payload = _scan_payload(remark_code, rpa_session_key="wx-row-2")
    duplicate_payload["sessions"][0]["display_name"] = "不应覆盖"
    duplicate = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=duplicate_payload, headers=_worker_headers(worker))
    assert duplicate.status_code == 200
    duplicate_data = duplicate.json()["data"]
    assert duplicate_data["bindings"][0]["id"] == first_binding["id"]
    assert duplicate_data["bindings"][0]["rpa_session_key"] == "wx-row-1"

    with SessionLocal() as db:
        bindings = db.query(WechatSessionBinding).filter(WechatSessionBinding.lead_id == first_binding["lead_id"]).all()
        assert len(bindings) == 1


def test_scan_failed_returns_error_code_and_trace_id():
    worker = _create_worker()
    payload = _scan_payload(None)
    payload["sessions"] = []
    payload["scan_failed"] = True
    payload["error_code"] = "WECHAT_WINDOW_NOT_READY"

    response = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=payload, headers=_worker_headers(worker))
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["error_code"] == "WECHAT_WINDOW_NOT_READY"
    assert body["trace_id"]


def test_read_targets_excludes_closed_and_rejected_conversations_but_allows_degraded_state_targets():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "closed"
        db.commit()
    targets = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert targets.status_code == 200
    assert targets.json()["data"]["targets"] == []

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        conversation.last_ai_reply_at = None
        binding_row = db.get(WechatSessionBinding, binding["id"])
        binding_row.listen_status = "degraded"
        binding_row.unread_hint = False
        db.commit()
    degraded = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert degraded.status_code == 200
    assert degraded.json()["data"]["targets"][0]["read_reason"] == "waiting_user_reply"

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "rejected"
        db.commit()
    rejected = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert rejected.status_code == 200
    assert rejected.json()["data"]["targets"] == []


def test_read_targets_only_returns_v06_state_machine_reasons_and_recall_precheck_creates_no_follow_up():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    allowed = {"recall_precheck", "recent_ai_sent", "waiting_user_reply", "waiting_sales_reply"}

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "recall_precheck"
        db.commit()

    response = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert response.status_code == 200
    targets = response.json()["data"]["targets"]
    assert targets[0]["read_reason"] == "recall_precheck"
    assert all(item["read_reason"] in allowed for item in targets)

    with SessionLocal() as db:
        assert db.query(Task).filter(Task.task_type == "follow_up").count() == 0


def test_read_targets_degrades_bound_binding_without_remark_code():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        binding_row.remark_code = None
        binding_row.bind_status = "bound"
        binding_row.listen_status = "listening"
        binding_row.allow_listening = True
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "recall_precheck"
        db.commit()

    response = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert response.status_code == 200
    assert response.json()["data"]["targets"] == []

    admin_binding = client.get(f"/api/conversations/{binding['conversation_id']}/wechat-binding", headers=HEADERS)
    assert admin_binding.status_code == 200
    data = admin_binding.json()["data"]
    assert data["bind_status"] == "needs_review"
    assert data["listen_status"] == "degraded"
    assert data["allow_listening"] is False
    assert data["error_code"] == "C2_TARGET_REMARK_CODE_MISSING"


def test_read_targets_allows_missing_row_fingerprint_and_omits_optional_field():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    payload = _scan_payload(remark_code)
    del payload["sessions"][0]["row_fingerprint"]
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=payload, headers=_worker_headers(worker))
    assert scan.status_code == 200
    binding = scan.json()["data"]["bindings"][0]

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        conversation.last_ai_reply_at = utcnow()
        db.commit()

    response = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert response.status_code == 200
    target = response.json()["data"]["targets"][0]
    assert target["conversation_id"] == binding["conversation_id"]
    assert target["remark_code"] == remark_code
    assert target["rpa_session_key"] == "wx-row-1"
    assert target["display_name"] == remark_code
    assert "row_fingerprint" not in target


def test_read_targets_uses_conversation_and_remark_code_even_without_local_locator():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        binding_row.rpa_session_key = ""
        binding_row.display_name = ""
        binding_row.bind_status = "bound"
        binding_row.listen_status = "listening"
        binding_row.allow_listening = True
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "recall_precheck"
        db.commit()

    response = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert response.status_code == 200
    targets = response.json()["data"]["targets"]
    assert len(targets) == 1
    assert targets[0]["conversation_id"] == binding["conversation_id"]
    assert targets[0]["remark_code"] == remark_code
    assert targets[0]["rpa_session_key"] == ""
    assert targets[0]["display_name"] == ""
    assert targets[0]["read_reason"] == "recall_precheck"

    admin_binding = client.get(f"/api/conversations/{binding['conversation_id']}/wechat-binding", headers=HEADERS)
    data = admin_binding.json()["data"]
    assert data["bind_status"] == "bound"
    assert data["listen_status"] == "listening"
    assert data["allow_listening"] is True
    assert data["error_code"] is None


def test_message_ingest_allows_changed_or_empty_rpa_session_key_and_dedupes_by_conversation():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    changed_locator_payload = {
        "read_run_id": "read-changed-locator",
        "conversation_id": binding["conversation_id"],
        "rpa_session_key": "wx-row-after-search",
        "messages": [{"dedupe_key": "msg-locator-change", "sender_role_hint": "customer", "message_type": "text", "content": "短码搜索后读到的新消息"}],
    }
    first = client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=changed_locator_payload, headers=_worker_headers(worker))
    assert first.status_code == 200
    assert first.json()["data"]["ingested_count"] == 1

    empty_locator_payload = {
        "read_run_id": "read-empty-locator",
        "conversation_id": binding["conversation_id"],
        "messages": [{"dedupe_key": "msg-empty-locator", "sender_role_hint": "customer", "message_type": "text", "content": "没有稳定本地定位键"}],
    }
    second = client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=empty_locator_payload, headers=_worker_headers(worker))
    assert second.status_code == 200
    assert second.json()["data"]["ingested_count"] == 1

    duplicated = client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=changed_locator_payload, headers=_worker_headers(worker))
    assert duplicated.status_code == 200
    assert duplicated.json()["data"]["duplicated_count"] == 1
    assert duplicated.json()["data"]["results"][0]["error_code"] == "MESSAGE_INGEST_DUPLICATED"
    with SessionLocal() as db:
        messages = db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).order_by(MessageEvent.dedupe_key).all()
        assert [message.dedupe_key for message in messages] == ["msg-empty-locator", "msg-locator-change"]
        assert [message.rpa_session_key for message in messages] == ["", "wx-row-after-search"]


def test_message_ingest_read_target_failures_are_ignored_and_do_not_trigger_ai():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    for failure in ["target_not_confirmed", "search_not_found", "search_ambiguous"]:
        response = client.post(
            f"/api/workers/{worker['id']}/wechat/messages/ingest",
            json={
                "read_run_id": f"read-{failure}",
                "conversation_id": binding["conversation_id"],
                "rpa_session_key": "wx-row-maybe-stale",
                "messages": [
                    {
                        "dedupe_key": f"msg-{failure}",
                        "sender_role_hint": "customer",
                        "message_type": "text",
                        "content": "这条不能触发 AI",
                        "raw_payload": {"read_result": failure},
                    }
                ],
            },
            headers=_worker_headers(worker),
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["ignored_count"] == 1
        assert data["results"][0]["ingest_result"] == "ignored"
        assert data["results"][0]["error_code"] == failure.upper()

    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).count() == 0
        assert db.query(MessageBatch).count() == 0


def test_message_ingest_duplicate_key_is_conversation_scoped_across_worker_change():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]
    payload = {
        "read_run_id": "read-001",
        "conversation_id": binding["conversation_id"],
        "rpa_session_key": "wx-row-1",
        "messages": [{"dedupe_key": "msg-cross-worker", "sender_role_hint": "customer", "message_type": "text", "content": "你好"}],
    }
    first = client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=payload, headers=_worker_headers(worker))
    assert first.status_code == 200

    worker_b = _create_worker()
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        binding_row.worker_id = worker_b["id"]
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.worker_id = worker_b["id"]
        db.commit()

    duplicated = client.post(f"/api/workers/{worker_b['id']}/wechat/messages/ingest", json=payload, headers=_worker_headers(worker_b))
    assert duplicated.status_code == 200
    data = duplicated.json()["data"]
    assert data["duplicated_count"] == 1
    assert data["results"][0]["ingest_result"] == "duplicated"
    assert data["results"][0]["error_code"] == "MESSAGE_INGEST_DUPLICATED"
    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).count() == 1


def test_message_ingest_ignores_unknown_sender_and_closed_conversation():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    unknown = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json={
            "read_run_id": "read-unknown",
            "conversation_id": binding["conversation_id"],
            "rpa_session_key": "wx-row-1",
            "messages": [{"dedupe_key": "msg-unknown", "sender_role_hint": "unknown", "message_type": "text", "content": "?"}],
        },
        headers=_worker_headers(worker),
    )
    assert unknown.status_code == 200
    assert unknown.json()["data"]["ignored_count"] == 1
    assert unknown.json()["data"]["results"][0]["error_code"] == "MESSAGE_SENDER_ROLE_UNCLEAR"

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "closed"
        db.commit()

    closed = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json={
            "read_run_id": "read-closed",
            "conversation_id": binding["conversation_id"],
            "rpa_session_key": "wx-row-1",
            "messages": [{"dedupe_key": "msg-closed", "sender_role_hint": "customer", "message_type": "text", "content": "还在吗"}],
        },
        headers=_worker_headers(worker),
    )
    assert closed.status_code == 200
    assert closed.json()["data"]["ignored_count"] == 1
    assert closed.json()["data"]["results"][0]["error_code"] == "CONVERSATION_STATUS_NOT_LISTENABLE"
    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).count() == 0


def test_sales_side_sender_roles_pause_ai_and_preserve_omniauto_evidence_without_triggering_batch():
    for role in ["self", "sales", "sales_candidate"]:
        setup_function()
        worker = _create_worker()
        _create_sales(worker["id"])
        _create_lead("王先生", "13896676678")
        remark_code = _pull_remark_code(worker)
        scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
        binding = scan.json()["data"]["bindings"][0]

        response = client.post(
            f"/api/workers/{worker['id']}/wechat/messages/ingest",
            json={
                "read_run_id": f"read-{role}",
                "conversation_id": binding["conversation_id"],
                "rpa_session_key": "wx-row-1",
                "messages": [
                    {
                        "dedupe_key": f"msg-{role}",
                        "sender_role_hint": role,
                        "message_type": "text",
                        "content": "我是销售，稍后联系您",
                        "raw_payload": {
                            "sender_role": role,
                            "sender_role_confidence": 0.87,
                            "sender_role_evidence": {"bubble_side": "right"},
                        },
                    }
                ],
                "evidence": {"sender_role_evidence": {"source": "omniauto_v16"}},
            },
            headers=_worker_headers(worker),
        )

        assert response.status_code == 200
        assert response.json()["data"]["ingested_count"] == 1
        with SessionLocal() as db:
            message = db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).one()
            conversation = db.get(Conversation, binding["conversation_id"])
            assert message.sender_role == role
            assert message.raw_payload["sender_role_confidence"] == 0.87
            assert message.evidence["sender_role_evidence"]["source"] == "omniauto_v16"
            assert conversation.status == "sales_replied_waiting_user"
            assert conversation.ai_enabled is False
            assert db.query(MessageBatch).count() == 0


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
    assert "reason_code" not in renamed_binding
    assert "conversation_status" not in renamed_binding
    assert "ai_enabled" not in renamed_binding

    bindings = client.get(f"/api/leads/{first_binding['lead_id']}/wechat-bindings", headers=HEADERS)
    assert bindings.status_code == 200
    assert len(bindings.json()["data"]["items"]) == 1


def test_same_remark_code_session_key_change_retires_stale_binding_with_messages_instead_of_deleting():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("许聪", "13896676680", {"remark_code": "CJTEST01"})

    first_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-old")
    first_payload["sessions"][0]["display_name"] = "CJTEST01 许聪"
    first = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=first_payload, headers=_worker_headers(worker))
    assert first.status_code == 200
    first_binding = first.json()["data"]["bindings"][0]

    with SessionLocal() as db:
        stale = WechatSessionBinding(
            worker_id=worker["id"],
            display_name="旧行残留",
            rpa_session_key="wx-row-new",
            row_fingerprint="stale-fingerprint",
            bind_status="bound",
            listen_status="listening",
            allow_listening=True,
            remark_code="CJSTALE",
        )
        db.add(stale)
        db.flush()
        db.add(
            MessageEvent(
                conversation_id=stale.conversation_id,
                binding_id=stale.id,
                worker_id=worker["id"],
                rpa_session_key=stale.rpa_session_key,
                read_run_id="read-stale",
                dedupe_key="msg-stale",
                sender_role="customer",
                message_type="text",
                content="旧 binding 已经被消息引用",
            )
        )
        db.commit()
        stale_id = stale.id

    renamed_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-new")
    renamed_payload["scan_id"] = "scan-002"
    renamed_payload["sessions"][0]["display_name"] = "CJTEST01许聪"
    renamed = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=renamed_payload, headers=_worker_headers(worker))
    assert renamed.status_code == 200
    renamed_binding = renamed.json()["data"]["bindings"][0]
    assert renamed_binding["id"] == first_binding["id"]
    assert renamed_binding["rpa_session_key"] == "wx-row-new"

    with SessionLocal() as db:
        stale_row = db.get(WechatSessionBinding, stale_id)
        message = db.query(MessageEvent).filter(MessageEvent.binding_id == stale_id).one()
        assert stale_row is not None
        assert stale_row.bind_status == "disabled"
        assert stale_row.listen_status == "disabled"
        assert stale_row.deleted_at is not None
        assert stale_row.rpa_session_key.startswith("wx-row-new#retired#")
        assert stale_row.error_code == "SESSION_BINDING_REPLACED_BY_REMARK_CODE"
        assert message.content == "旧 binding 已经被消息引用"
