from fastapi.testclient import TestClient

from app.contracts.c2 import c2_contract_v3, contract_revision, contract_sha256
from app.core.database import Base, SessionLocal, engine
from app.main import app
from app.models.c3 import Conversation, MessageBatch, ReplyAction
from app.models.wechat import WechatSessionBinding
from app.services.wechat_service import _authorization_revision


client = TestClient(app)
HEADERS = {
    "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
    "X-Operator-Name": "Ops Tester",
    "X-Operator-Role": "admin",
}
FORBIDDEN_RESPONSE_FIELDS = {
    "runtime_status",
    "current_task_id",
    "client_bind_status",
    "status_flow",
    "executor_status",
    "lead_status",
    "reason_code",
    "ingest_status",
    "notify_status",
}


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _assert_no_forbidden_fields(value):
    if isinstance(value, dict):
        forbidden = FORBIDDEN_RESPONSE_FIELDS.intersection(value.keys())
        assert not forbidden, f"deprecated response fields leaked: {forbidden}"
        for item in value.values():
            _assert_no_forbidden_fields(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_forbidden_fields(item)


def _create_worker() -> dict:
    response = client.post(
        "/api/workers",
        json={"worker_name": "C3 Worker", "device_name": "Windows PC", "platform": "windows", "enabled": True},
        headers=HEADERS,
    )
    assert response.status_code == 200
    worker = response.json()["data"]
    bind = client.post(
        f"/api/workers/{worker['id']}/client-bind",
        json={"worker_token": worker["worker_token"], "client_instance_id": "client-c3"},
    )
    assert bind.status_code == 200
    heartbeat = client.post(
        f"/api/workers/{worker['id']}/heartbeat",
        json={
            "client_instance_id": "client-c3",
            "run_status": "running",
            "rpa_component_status": "ready",
            "wechat_status": "logged_in",
            "running_status": "idle",
        },
        headers={"X-Worker-Token": worker["worker_token"]},
    )
    assert heartbeat.status_code == 200
    return worker


def _worker_headers(worker: dict) -> dict:
    return {"X-Worker-Token": worker["worker_token"], "X-Client-Instance-Id": "client-c3"}


def _create_sales(worker_id: str) -> str:
    response = client.post(
        "/api/sales",
        json={"sales_name": "张伟", "enabled": True, "sort_order": 10, "worker_id": worker_id},
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()["data"]["id"]


def _create_lead(name: str = "王先生", phone: str = "13896676678", remark_code: str = "C3TEST01") -> dict:
    response = client.post(
        "/api/leads",
        json={"customer_name": name, "phones": [phone], "remark": "预算 10 万", "custom_fields": {"remark_code": remark_code}},
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()["data"]


def _scan(worker: dict, remark_code: str = "C3TEST01") -> dict:
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json={
            "scan_id": "scan-c3-001",
            "sidecar_run_id": "sidecar-c3-001",
            "started_at": "2026-06-23T10:00:00+08:00",
            "finished_at": "2026-06-23T10:00:02+08:00",
            "sessions": [
                {
                    "rpa_session_key": "wx-c3-row-001",
                    "display_name": f"{remark_code} 王先生",
                    "remark_code_candidates": [remark_code],
                    "row_fingerprint": "wx-c3-row-fp-001",
                    "unread_hint": True,
                    "last_message_preview": "你好",
                    "ocr_confidence": 0.98,
                }
            ],
        },
        headers=_worker_headers(worker),
    )
    assert response.status_code == 200
    return response.json()["data"]["bindings"][0]


def _setup_bound_conversation() -> tuple[dict, dict]:
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead()
    binding = _scan(worker)
    return worker, binding


def _ingest(worker: dict, conversation_id: str, dedupe_key: str, content: str) -> str:
    return _ingest_with_role(worker, conversation_id, dedupe_key, content, "customer")


def _ingest_with_role(worker: dict, conversation_id: str, dedupe_key: str, content: str, role: str) -> str:
    with SessionLocal() as db:
        binding = db.query(WechatSessionBinding).filter(WechatSessionBinding.conversation_id == conversation_id).one()
        remark_code = binding.remark_code
        authorization_revision = _authorization_revision(binding)
    raw_payload = {
        "contract_version": 3,
        "contract_revision": contract_revision(),
        "contract_sha256": contract_sha256(),
        "observation_schema_version": int(c2_contract_v3()["observation_schema_version"]),
        "source_message_key": dedupe_key,
        "observation": {
            "schema_version": 3,
            "observation_id": f"observation:{dedupe_key}",
            "row_kind": "text_bubble",
            "sender_role": role,
            "sender_role_source": "same_row_avatar",
            "message_type": "text",
            "voice_state": "not_voice",
            "content_clean": content,
            "source_message": {"id": dedupe_key, "type": "text", "sender_role": role, "content": content},
        },
    }
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json={
            "contract_version": 3,
            "contract_revision": contract_revision(),
            "contract_sha256": contract_sha256(),
            "observation_schema_version": int(c2_contract_v3()["observation_schema_version"]),
            "read_run_id": f"read-{dedupe_key}",
            "conversation_id": conversation_id,
            "remark_code": remark_code,
            "rpa_session_key": "wx-c3-row-001",
            "authorization_revision": authorization_revision,
            "messages": [
                {
                    "dedupe_key": dedupe_key,
                    "source_message_key": dedupe_key,
                    "sender_role_hint": role,
                    "message_type": "text",
                    "content": content,
                    "item_state": "completed",
                    "flow_state": "completed",
                    "message_position": {"screen_order": 1, "frame_source": "final_read"},
                    "raw_payload": raw_payload,
                }
            ],
            "evidence": {
                "contract_revision": contract_revision(),
                "contract_sha256": contract_sha256(),
                "observation_schema_version": int(c2_contract_v3()["observation_schema_version"]),
                "authoritative_frame_source": "final_read",
                "observations": [raw_payload["observation"]],
            },
        },
        headers=_worker_headers(worker),
    )
    assert response.status_code == 200
    result = response.json()["data"]["results"][0]
    assert result["ingest_result"] == "ingested"
    assert "ingest_status" not in result
    return result["message_event_id"]


def _collect(conversation_id: str, message_event_id: str) -> dict:
    response = client.post(
        f"/api/internal/conversations/{conversation_id}/message-batches/collect",
        json={"trigger_message_event_id": message_event_id, "trace_id": "trace-c3-test"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()["data"]


def _generate(batch_id: str) -> dict:
    response = client.post(f"/api/internal/message-batches/{batch_id}/generate", json={}, headers=HEADERS)
    assert response.status_code == 200
    return response.json()["data"]


def test_collect_merges_customer_messages_into_one_active_batch():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-001", "你好")
    m2 = _ingest(worker, binding["conversation_id"], "msg-c3-002", "预算 15 万")

    first = _collect(binding["conversation_id"], m1)
    second = _collect(binding["conversation_id"], m2)

    assert second["batch_id"] == first["batch_id"]
    assert second["batch"]["message_count"] == 2
    assert second["batch_status"] == "collecting"


def test_generate_creates_one_reply_action_and_one_chat_reply_task_idempotently():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-003", "我想看看 SUV")
    batch = _collect(binding["conversation_id"], m1)

    first = _generate(batch["batch_id"])
    second = _generate(batch["batch_id"])

    assert first["decision"] == "send_reply"
    assert first["reply_action_id"] == second["reply_action_id"]
    assert first["task_id"] == second["task_id"]
    tasks = client.get("/api/tasks?task_type=chat_reply", headers=HEADERS).json()["data"]["items"]
    assert len(tasks) == 1
    assert tasks[0]["reply_action_id"] == first["reply_action_id"]
    detail = client.get(f"/api/tasks/{first['task_id']}", headers=HEADERS).json()["data"]
    _assert_no_forbidden_fields(detail)
    assert detail["c3"]["message_batch"]["id"] == batch["batch_id"]
    assert detail["c3"]["message_batch"]["status"] == "reply_action_created"
    assert detail["c3"]["reply_action"]["id"] == first["reply_action_id"]
    assert detail["c3"]["reply_action"]["status"] == "queued"
    assert detail["c3"]["sent_ack"] is None
    assert detail["c3"]["handoff_event"] is None


def test_c3_does_not_expose_duplicate_worker_task_claim_endpoint():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-003-legacy-claim", "我想看看 SUV")
    generated = _generate(_collect(binding["conversation_id"], m1)["batch_id"])

    legacy = client.post(
        f"/api/worker/tasks/{generated['task_id']}/claim",
        json={"worker_id": worker["id"], "current_step": "chat_reply_claimed"},
        headers=_worker_headers(worker),
    )

    assert legacy.status_code == 404


def test_new_customer_message_supersedes_old_reply_action_before_send():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-004", "我想看轿车")
    old = _generate(_collect(binding["conversation_id"], m1)["batch_id"])
    m2 = _ingest(worker, binding["conversation_id"], "msg-c3-005", "再补充一下，要 SUV")

    new_batch = _collect(binding["conversation_id"], m2)
    new_action = _generate(new_batch["batch_id"])

    old_task = client.get(f"/api/tasks/{old['task_id']}", headers=HEADERS).json()["data"]
    assert old_task["status"] == "cancelled"
    assert old_task["reply_action_id"] == old["reply_action_id"]
    assert "events" in old_task
    assert "status_flow" not in old_task
    assert new_action["reply_action_id"] != old["reply_action_id"]


def test_customer_message_ingest_supersedes_unsent_reply_action_even_before_collect():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-004-a", "我想看轿车")
    old = _generate(_collect(binding["conversation_id"], m1)["batch_id"])

    _ingest(worker, binding["conversation_id"], "msg-c3-004-b", "再补一句，要白色")

    old_task = client.get(f"/api/tasks/{old['task_id']}", headers=HEADERS).json()["data"]
    assert old_task["status"] == "cancelled"
    with SessionLocal() as db:
        old_action = db.get(ReplyAction, old["reply_action_id"])
        assert old_action.status == "superseded"


def test_generating_batch_is_superseded_when_new_customer_message_arrives():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-generating-001", "我想看轿车")
    old_batch = _collect(binding["conversation_id"], m1)
    with SessionLocal() as db:
        batch = db.get(MessageBatch, old_batch["batch_id"])
        batch.status = "generating"
        batch.active = True
        db.commit()

    m2 = _ingest(worker, binding["conversation_id"], "msg-c3-generating-002", "补充一下，要 SUV")
    new_batch = _collect(binding["conversation_id"], m2)

    assert new_batch["batch_id"] != old_batch["batch_id"]
    with SessionLocal() as db:
        old = db.get(MessageBatch, old_batch["batch_id"])
        new = db.get(MessageBatch, new_batch["batch_id"])
        assert old.status == "superseded"
        assert old.active is False
        assert old.error_code == "MESSAGE_BATCH_SUPERSEDED"
        assert new.active is True


def test_sales_manual_reply_cancels_unsent_reply_action_and_disables_ai():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-004-c", "我想看轿车")
    old = _generate(_collect(binding["conversation_id"], m1)["batch_id"])

    _ingest_with_role(worker, binding["conversation_id"], "msg-c3-sales-001", "我是销售，稍后联系您", "self")

    old_task = client.get(f"/api/tasks/{old['task_id']}", headers=HEADERS).json()["data"]
    assert old_task["status"] == "cancelled"
    with SessionLocal() as db:
        old_action = db.get(ReplyAction, old["reply_action_id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        assert old_action.status == "cancelled"
        assert conversation.status == "sales_replied_waiting_user"
        assert conversation.ai_enabled is False


def test_claim_send_is_single_owner_and_sent_ack_is_idempotent():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-006", "想了解 15 万 SUV")
    generated = _generate(_collect(binding["conversation_id"], m1)["batch_id"])
    task_id = generated["task_id"]
    reply_action_id = generated["reply_action_id"]

    claim_task = client.post(
        f"/api/tasks/{task_id}/claim",
        json={"worker_id": worker["id"], "current_step": "chat_reply_claimed"},
        headers=_worker_headers(worker),
    )
    assert claim_task.status_code == 200

    first_claim_send = client.post(
        f"/api/reply-actions/{reply_action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=_worker_headers(worker),
    )
    assert first_claim_send.status_code == 200
    send_data = first_claim_send.json()["data"]

    conflict = client.post(
        f"/api/reply-actions/{reply_action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=_worker_headers(worker),
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "REPLY_ACTION_CLAIM_CONFLICT"

    ack_payload = {
        "send_token": send_data["send_token"],
        "task_id": task_id,
        "worker_id": worker["id"],
        "client_instance_id": "client-c3",
        "send_result": "sent",
        "reply_text_hash": send_data["reply_text_hash"],
        "sidecar_run_id": "sidecar-send-001",
    }
    ack = client.post(f"/api/reply-actions/{reply_action_id}/sent-ack", json=ack_payload, headers=_worker_headers(worker))
    assert ack.status_code == 200
    assert ack.json()["data"]["task"]["status"] == "completed"
    assert ack.json()["data"]["task"]["result_code"] == "chat_reply_sent"
    completed_detail = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["data"]
    _assert_no_forbidden_fields(completed_detail)
    assert completed_detail["c3"]["reply_action"]["status"] == "sent"
    assert completed_detail["c3"]["sent_ack"]["send_result"] == "sent"
    assert completed_detail["c3"]["sent_ack"]["sidecar_run_id"] == "sidecar-send-001"
    worker_detail = client.get(f"/api/workers/{worker['id']}", headers=HEADERS).json()["data"]
    assert worker_detail["running_status"] == "idle"
    assert worker_detail["current_task"] is None
    assert worker_detail["current_step"] is None

    duplicated = client.post(f"/api/reply-actions/{reply_action_id}/sent-ack", json=ack_payload, headers=_worker_headers(worker))
    assert duplicated.status_code == 200
    assert duplicated.json()["data"]["duplicated"] is True
    assert duplicated.json()["data"]["error_code"] == "SEND_ACK_DUPLICATED"


def test_pre_send_refresh_new_customer_message_supersedes_sending_reply_action():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-pre-send-001", "想了解 15 万 SUV")
    generated = _generate(_collect(binding["conversation_id"], m1)["batch_id"])
    task_id = generated["task_id"]
    reply_action_id = generated["reply_action_id"]

    claim_task = client.post(
        f"/api/tasks/{task_id}/claim",
        json={"worker_id": worker["id"], "current_step": "chat_reply_claimed"},
        headers=_worker_headers(worker),
    )
    assert claim_task.status_code == 200
    claim_send = client.post(
        f"/api/reply-actions/{reply_action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=_worker_headers(worker),
    )
    assert claim_send.status_code == 200
    send_data = claim_send.json()["data"]

    m2 = _ingest(worker, binding["conversation_id"], "msg-c3-pre-send-002", "我又改主意了，想看新能源")
    new_batch = _collect(binding["conversation_id"], m2)
    new_action = _generate(new_batch["batch_id"])

    with SessionLocal() as db:
        old_action = db.get(ReplyAction, reply_action_id)
        assert old_action.status == "superseded"
    old_task = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["data"]
    assert old_task["status"] == "cancelled"
    assert new_action["reply_action_id"] != reply_action_id
    worker_detail = client.get(f"/api/workers/{worker['id']}", headers=HEADERS).json()["data"]
    assert worker_detail["running_status"] == "idle"
    assert worker_detail["current_task"] is None
    assert worker_detail["current_step"] is None

    ack_payload = {
        "send_token": send_data["send_token"],
        "task_id": task_id,
        "worker_id": worker["id"],
        "client_instance_id": "client-c3",
        "send_result": "sent",
        "reply_text_hash": send_data["reply_text_hash"],
        "sidecar_run_id": "sidecar-send-stale",
    }
    stale_ack = client.post(f"/api/reply-actions/{reply_action_id}/sent-ack", json=ack_payload, headers=_worker_headers(worker))
    assert stale_ack.status_code == 409
    assert stale_ack.json()["code"] == "REPLY_ACTION_CLAIM_CONFLICT"

    binding_after = client.get(f"/api/conversations/{binding['conversation_id']}/wechat-binding", headers=HEADERS).json()["data"]
    assert "conversation_status" not in binding_after
    assert "ai_enabled" not in binding_after


def test_sent_ack_unknown_marks_unknown_result_and_prevents_auto_resend():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-unknown-001", "想了解 15 万 SUV")
    generated = _generate(_collect(binding["conversation_id"], m1)["batch_id"])
    task_id = generated["task_id"]
    reply_action_id = generated["reply_action_id"]

    claim_task = client.post(
        f"/api/tasks/{task_id}/claim",
        json={"worker_id": worker["id"], "current_step": "chat_reply_claimed"},
        headers=_worker_headers(worker),
    )
    assert claim_task.status_code == 200
    claim_send = client.post(
        f"/api/reply-actions/{reply_action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=_worker_headers(worker),
    )
    assert claim_send.status_code == 200
    send_data = claim_send.json()["data"]

    ack = client.post(
        f"/api/reply-actions/{reply_action_id}/sent-ack",
        json={
            "send_token": send_data["send_token"],
            "task_id": task_id,
            "worker_id": worker["id"],
            "client_instance_id": "client-c3",
            "send_result": "unknown",
            "reply_text_hash": send_data["reply_text_hash"],
            "error_code": "SEND_RESULT_UNKNOWN",
        },
        headers=_worker_headers(worker),
    )
    assert ack.status_code == 200
    data = ack.json()["data"]
    assert data["reply_action"]["status"] == "unknown_send_result"
    assert data["task"]["status"] == "failed"
    assert data["task"]["error_code"] == "SEND_RESULT_UNKNOWN"
    assert data["task"]["failure_remark"] == "发送结果未知，需人工确认"
    assert data["task"]["available_actions"] == []
    worker_detail = client.get(f"/api/workers/{worker['id']}", headers=HEADERS).json()["data"]
    assert worker_detail["running_status"] == "idle"
    assert worker_detail["current_task"] is None
    assert worker_detail["current_step"] is None

    resend = client.post(
        f"/api/reply-actions/{reply_action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=_worker_headers(worker),
    )
    assert resend.status_code == 409
    assert resend.json()["code"] == "REPLY_ACTION_CLAIM_CONFLICT"
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status != "waiting_user_reply"
        assert conversation.reply_count == 0


def test_handoff_decision_disables_ai_and_does_not_create_chat_reply_task():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-007", "你们最低价是多少")
    generated = _generate(_collect(binding["conversation_id"], m1)["batch_id"])

    assert generated["decision"] == "handoff"
    assert generated["handoff_event_id"]
    tasks = client.get("/api/tasks?task_type=chat_reply", headers=HEADERS).json()["data"]["items"]
    assert tasks == []
    binding_after = client.get(f"/api/conversations/{binding['conversation_id']}/wechat-binding", headers=HEADERS).json()["data"]
    assert "ai_enabled" not in binding_after
    assert "conversation_status" not in binding_after
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.ai_enabled is False
        assert conversation.status == "waiting_sales_reply"
        assert conversation.handoff_reason_code == "HANDOFF_REQUIRED"


def test_formal_api_responses_do_not_leak_deprecated_fields():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-field-scan-001", "我想了解 SUV")
    generated = _generate(_collect(binding["conversation_id"], m1)["batch_id"])

    responses = [
        client.get(f"/api/workers/{worker['id']}", headers=HEADERS),
        client.get(f"/api/conversations/{binding['conversation_id']}/wechat-binding", headers=HEADERS),
        client.get(f"/api/conversations/{binding['conversation_id']}/messages", headers=HEADERS),
        client.get(f"/api/tasks/{generated['task_id']}", headers=HEADERS),
        client.post(f"/api/internal/message-batches/{generated['batch']['id']}/generate", json={}, headers=HEADERS),
    ]
    for response in responses:
        assert response.status_code == 200
        _assert_no_forbidden_fields(response.json())
