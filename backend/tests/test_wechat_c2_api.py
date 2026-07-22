from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.exc import IntegrityError

WORKER_CLIENT_ROOT = Path(__file__).resolve().parents[2] / "worker-client"
if str(WORKER_CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKER_CLIENT_ROOT))
OMNIAUTO_ROOT = WORKER_CLIENT_ROOT / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import build_message_observations_v3
from chejin_worker_client.models import WechatReadTarget as WorkerWechatReadTarget
from chejin_worker_client.wechat_c2 import build_message_ingest_payload as build_worker_message_ingest_payload

from app.contracts.c2 import c2_contract_v3, contract_revision, contract_sha256
from app.core.database import Base, engine
from app.main import app
from app.models.base import utcnow
from app.models.c3 import Conversation, MessageBatch, ReplyAction
from app.models.sales import Sales
from app.models.task import Task
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.core.database import SessionLocal
from app.services import wechat_service


client = TestClient(app)
HEADERS = {
    "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
    "X-Operator-Name": "Ops Tester",
    "X-Operator-Role": "admin",
}


def _v3_contract_fields() -> dict:
    return {
        "contract_version": 3,
        "contract_revision": contract_revision(),
        "contract_sha256": contract_sha256(),
        "observation_schema_version": int(c2_contract_v3()["observation_schema_version"]),
    }


def _v3_raw_fields(source_message_key: str) -> dict:
    return {
        **_v3_contract_fields(),
        "source_message_key": source_message_key,
    }


def _binding_authorization_revision(binding_id: str) -> str:
    with SessionLocal() as db:
        binding = db.get(WechatSessionBinding, binding_id)
        assert binding is not None
        return wechat_service._authorization_revision(binding)


def _v3_message(
    source_key: str,
    *,
    role: str,
    message_type: str,
    content: str | None,
    screen_order: int,
    raw_extra: dict | None = None,
) -> dict:
    row_kind = {
        "text": "text_bubble",
        "voice": "voice_transcript",
        "system": "system_message",
        "image": "image_bubble",
    }[message_type]
    role_source = "parent_voice" if message_type == "voice" else "system" if role == "system" else "same_row_avatar"
    observation = {
        "schema_version": 3,
        "observation_id": f"observation:{source_key}",
        "row_kind": row_kind,
        "sender_role": role,
        "sender_role_source": role_source,
        "message_type": message_type,
        "voice_state": "transcribed" if message_type == "voice" else "not_voice",
        "source_message": {
            "id": source_key,
            "type": message_type,
            "sender_role": role,
            "content": content,
        },
    }
    if content:
        observation["content_clean"] = content
    if message_type == "voice":
        observation["parent_voice_anchor_key"] = f"anchor:{source_key}"
        observation["source_message"]["voice_anchor_stable_key"] = f"anchor:{source_key}"
    return {
        "dedupe_key": source_key,
        "source_message_key": source_key,
        "sender_role_hint": role,
        "message_type": message_type,
        "content": content,
        "item_state": "completed",
        "flow_state": "completed",
        "message_position": {
            "screen_order": screen_order,
            "frame_source": "final_read",
            "order_source": "observation_index_fallback",
        },
        "raw_payload": {
            **_v3_raw_fields(source_key),
            "observation": observation,
            **(raw_extra or {}),
        },
    }


def _v3_ingest_payload(
    binding: dict,
    remark_code: str,
    *,
    read_run_id: str,
    messages: list[dict],
    rpa_session_key: str | None = None,
) -> dict:
    observations = [
        message["raw_payload"]["observation"]
        for message in messages
        if isinstance(message.get("raw_payload"), dict)
        and isinstance(message["raw_payload"].get("observation"), dict)
    ]
    return {
        **_v3_contract_fields(),
        "read_run_id": read_run_id,
        "conversation_id": binding["conversation_id"],
        "remark_code": remark_code,
        "rpa_session_key": binding.get("rpa_session_key") if rpa_session_key is None else rpa_session_key,
        "authorization_revision": _binding_authorization_revision(binding["id"]),
        "messages": messages,
        "evidence": {
            "contract_revision": contract_revision(),
            "contract_sha256": contract_sha256(),
            "observation_schema_version": int(c2_contract_v3()["observation_schema_version"]),
            "authoritative_frame_source": "final_read",
            "observations": observations,
        },
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
    assert len(read_target["authorization_revision"]) == 32

    admin_binding = client.get(f"/api/conversations/{binding['conversation_id']}/wechat-binding", headers=HEADERS)
    assert admin_binding.status_code == 200
    assert admin_binding.json()["data"]["remark_code"] == remark_code


def test_authorization_revision_ignores_scan_refresh_and_changes_with_binding_permission():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    first = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding_id = first.json()["data"]["bindings"][0]["id"]
    with SessionLocal() as db:
        initial_revision = db.get(WechatSessionBinding, binding_id).authorization_revision

    refresh_payload = _scan_payload(remark_code)
    refresh_payload["scan_id"] = "scan-authorization-refresh"
    refreshed = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=refresh_payload,
        headers=_worker_headers(worker),
    )
    assert refreshed.status_code == 200
    with SessionLocal() as db:
        assert db.get(WechatSessionBinding, binding_id).authorization_revision == initial_revision

    revoked_payload = _scan_payload(None)
    revoked_payload["scan_id"] = "scan-authorization-revoked"
    revoked = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=revoked_payload,
        headers=_worker_headers(worker),
    )
    assert revoked.status_code == 200
    with SessionLocal() as db:
        assert db.get(WechatSessionBinding, binding_id).authorization_revision > initial_revision


def test_scan_result_blocks_same_remark_code_claimed_by_multiple_sessions():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("虾丸子大", "13896676678", {"remark_code": "CJR8S5K3"})
    payload = _scan_payload("CJR8S5K3", rpa_session_key="wx-target")
    payload["sessions"][0]["display_name"] = "CJR8S5K3 虾丸子大"
    payload["sessions"].append(
        {
            "rpa_session_key": "wx-other-chat",
            "display_name": "聿安的家",
            "remark_code_candidates": ["CJR8S5K3"],
            "row_fingerprint": "fingerprint-other-chat",
            "unread_hint": False,
            "last_message_preview": "CJR8S5K3虾丸子大人：蛹者",
            "ocr_confidence": 0.99,
        }
    )

    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert scan.status_code == 200
    data = scan.json()["data"]
    assert data["bound_count"] == 0
    assert data["needs_review_count"] == 2
    assert len({item["id"] for item in data["bindings"]}) == 2
    assert {item["display_name"] for item in data["bindings"]} == {"CJR8S5K3 虾丸子大", "聿安的家"}
    assert all(item["bind_status"] == "needs_review" for item in data["bindings"])
    assert all(item["error_code"] == "SESSION_REMARK_CODE_MULTIPLE_SESSIONS" for item in data["bindings"])
    assert all(item["can_ingest_messages"] is False for item in data["bindings"])

    targets = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert targets.status_code == 200
    assert targets.json()["data"]["targets"] == []


def test_scan_result_ignores_group_excluded_remark_candidate_when_private_chat_matches():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("虾丸子大", "13896676678", {"remark_code": "CJR8S5K3"})
    payload = _scan_payload("CJR8S5K3", rpa_session_key="wx-private-chat")
    payload["sessions"][0]["display_name"] = "虾丸子大-CJR8S5K3"
    payload["sessions"].append(
        {
            "rpa_session_key": "wx-group-chat",
            "display_name": "销售讨论-CJR8S5K3(5)",
            # OmniAuto/Worker already classified this title as group, so it is
            # intentionally excluded from the backend's short-code candidates.
            "remark_code_candidates": [],
            "row_fingerprint": "fingerprint-group-chat",
            "unread_hint": True,
            "last_message_preview": "群聊消息",
            "ocr_confidence": 0.99,
        }
    )

    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert scan.status_code == 200
    data = scan.json()["data"]
    assert data["bound_count"] == 1
    private_binding = next(item for item in data["bindings"] if item["rpa_session_key"] == "wx-private-chat")
    group_binding = next(item for item in data["bindings"] if item["rpa_session_key"] == "wx-group-chat")
    assert private_binding["bind_status"] == "bound"
    assert private_binding["remark_code"] == "CJR8S5K3"
    assert group_binding["bind_status"] != "bound"
    assert group_binding["can_ingest_messages"] is False


def test_scan_result_does_not_reuse_soft_deleted_remark_binding():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("虾丸子大", "13896676678", {"remark_code": "CJR8S5K3"})
    first = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload("CJR8S5K3", rpa_session_key="wx-old"),
        headers=_worker_headers(worker),
    )
    assert first.status_code == 200
    deleted_binding_id = first.json()["data"]["bindings"][0]["id"]

    with SessionLocal() as db:
        deleted_binding = db.get(WechatSessionBinding, deleted_binding_id)
        assert deleted_binding is not None
        deleted_binding.deleted_at = utcnow()
        deleted_binding.bind_status = "disabled"
        deleted_binding.listen_status = "disabled"
        deleted_binding.allow_listening = False
        deleted_binding.rpa_session_key = f"wx-old#retired#{deleted_binding.id}"
        db.commit()

    payload = _scan_payload("CJR8S5K3", rpa_session_key="wx-new")
    payload["scan_id"] = "scan-after-soft-delete"
    rescan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert rescan.status_code == 200
    binding = rescan.json()["data"]["bindings"][0]
    assert binding["id"] != deleted_binding_id
    assert binding["rpa_session_key"] == "wx-new"
    assert binding["bind_status"] == "bound"
    assert binding["can_ingest_messages"] is True


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
    binding = scan.json()["data"]["bindings"][0]
    conversation_id = binding["conversation_id"]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-001",
        messages=[_v3_message("msg-001", role="customer", message_type="text", content="你好", screen_order=1)],
    )
    payload["evidence"]["screenshot"] = "local://message.png"
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


def test_image_observation_is_not_ingested_before_vision_is_enabled():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("图片客户", "13896676681")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    conversation_id = binding["conversation_id"]
    text_message = _v3_message(
        "text-after-image",
        role="customer",
        message_type="text",
        content="这辆车还有吗？",
        screen_order=2,
    )
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-image-observation",
        messages=[text_message],
    )
    payload["evidence"]["observations"].insert(
        0,
        {
            "schema_version": 3,
            "observation_id": "image-observation",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "source_message": {"id": "image-observation", "type": "image"},
        },
    )
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200
    assert response.json()["data"]["ingested_count"] == 1
    with SessionLocal() as db:
        messages = db.query(MessageEvent).filter(MessageEvent.conversation_id == conversation_id).all()
        assert len(messages) == 1
        assert messages[0].message_type == "text"
        assert messages[0].content == "这辆车还有吗？"


def test_v3_rejects_ingestible_observation_omitted_by_worker():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("完整性客户", "13896676682")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    included = _v3_message(
        "included-text",
        role="customer",
        message_type="text",
        content="第一条",
        screen_order=1,
    )
    omitted = _v3_message(
        "omitted-text",
        role="customer",
        message_type="text",
        content="第二条不能被漏掉",
        screen_order=2,
    )
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-observation-omitted",
        messages=[included],
    )
    payload["evidence"]["observations"].append(omitted["raw_payload"]["observation"])

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MESSAGE_OBSERVATION_MAPPING_INCOMPLETE"
    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).count() == 0


@pytest.mark.parametrize(
    ("recognition", "expected_warning"),
    [
        (None, "IMAGE_RECOGNITION_RESULT_INVALID"),
        ("failed", "IMAGE_RECOGNITION_RESULT_INVALID"),
        ({}, "IMAGE_RECOGNITION_RESULT_INVALID"),
        ({"status": "随便写"}, "IMAGE_RECOGNITION_RESULT_INVALID"),
        ({"status": "succeeded", "success": False}, "IMAGE_RECOGNITION_RESULT_INVALID"),
        ({"status": "succeeded", "error_code": "IMAGE_MODEL_TIMEOUT"}, "IMAGE_RECOGNITION_RESULT_INVALID"),
        ({"status": "succeeded", "error_code": ""}, None),
        ({"status": "failed"}, "IMAGE_RECOGNITION_FAILED"),
        ({"status": "failed", "error_code": "provider timeout"}, "PROVIDER_TIMEOUT"),
        ({"status": "failed", "success": True}, "IMAGE_RECOGNITION_RESULT_INVALID"),
    ],
)
def test_image_recognition_result_validation(recognition, expected_warning):
    assert wechat_service._image_recognition_warning_code({"image_recognition": recognition}) == expected_warning
    assert wechat_service._image_recognition_warning_code({}) == "IMAGE_RECOGNITION_RESULT_INVALID"


def test_v3_ingest_uses_canonical_content_and_rejects_expired_authorization_revision():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        db.commit()

    targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    revision = targets.json()["data"]["targets"][0]["authorization_revision"]
    payload = {
        **_v3_contract_fields(),
        "read_run_id": "read-v3",
        "conversation_id": binding["conversation_id"],
        "remark_code": remark_code,
        "rpa_session_key": "wx-row-1",
        "authorization_revision": revision,
        "messages": [
            {
                "dedupe_key": "v3-voice-key",
                "source_message_key": "v3-voice-source",
                "sender_role_hint": "self",
                "message_type": "voice",
                "content": "我马上回去。",
                "item_state": "completed",
                "flow_state": "completed",
                "message_position": {
                    "screen_order": 2,
                    "visual_top": 240,
                    "visual_bottom": 308,
                    "frame_source": "final_read",
                },
                "raw_payload": {
                    **_v3_raw_fields("v3-voice-source"),
                    "voice_transcription": "后端不应改用这里的旧值",
                    "observation": {
                        "schema_version": 3,
                        "observation_id": "v3-voice-observation",
                        "row_kind": "voice_transcript",
                        "sender_role": "self",
                        "sender_role_source": "parent_voice",
                        "message_type": "voice",
                        "voice_state": "transcribed",
                        "content_clean": "我马上回去。",
                        "parent_voice_anchor_key": "voice:self:4:v3",
                        "source_message": {
                            "id": "v3-voice-observation",
                            "type": "voice",
                            "content": "我马上回去。",
                            "voice_anchor_stable_key": "voice:self:4:v3",
                        },
                    },
                },
            },
        ],
    }
    payload["evidence"] = {
        "contract_revision": contract_revision(),
        "contract_sha256": contract_sha256(),
        "observation_schema_version": int(c2_contract_v3()["observation_schema_version"]),
        "authoritative_frame_source": "final_read",
        "observations": [payload["messages"][0]["raw_payload"]["observation"]],
    }
    accepted = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )
    assert accepted.status_code == 200
    assert accepted.json()["data"]["ingested_count"] == 1
    assert accepted.json()["data"]["ignored_count"] == 0
    wrong_contract = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json={**payload, "read_run_id": "read-v3-wrong-contract", "contract_sha256": "0" * 64},
        headers=_worker_headers(worker),
    )
    assert wrong_contract.status_code == 409
    assert wrong_contract.json()["code"] == "MESSAGE_CONTRACT_SHA256_MISMATCH"
    with SessionLocal() as db:
        event = db.query(MessageEvent).filter(MessageEvent.dedupe_key == "v3-voice-key").one()
        assert event.content == "我马上回去。"
        assert event.raw_payload["message_position"] == {
            "screen_order": 2,
            "visual_top": 240,
            "visual_bottom": 308,
            "frame_source": "final_read",
        }
        binding_row = db.get(WechatSessionBinding, binding["id"])
        binding_row.allow_listening = False
        binding_row.listen_status = "disabled"
        binding_row.authorization_revision += 1
        db.commit()
        binding_row.allow_listening = True
        binding_row.listen_status = "listening"
        binding_row.authorization_revision += 1
        db.commit()

    payload["read_run_id"] = "read-v3-stale"
    payload["messages"][0]["dedupe_key"] = "v3-stale-key"
    payload["messages"][0]["source_message_key"] = "v3-stale-source"
    stale = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "MESSAGE_AUTHORIZATION_REVISION_EXPIRED"


def test_worker_v3_five_second_voice_transcript_is_accepted_by_backend():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("五秒语音客户", "13896676682")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        db.commit()
    targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    target_payload = targets.json()["data"]["targets"][0]
    worker_target = WorkerWechatReadTarget.from_api(target_payload)
    voice_anchor_key = "voice:customer:5:v16-101"
    transcript = "我想看看这辆车"
    sidecar_messages = [
        {
            "id": "voice-transcript-five-seconds",
            "source_adapter": "win32_ocr",
            "type": "voice",
            "sender_role": "customer",
            "content": transcript,
            "voice_duration": 5,
            "voice_anchor_stable_key": voice_anchor_key,
            "bubble_rect": [420, 220, 700, 264],
            # Real bound transcripts retain the parent bubble's avatar
            # evidence. The sidecar must still emit parent_voice.
            "avatar_alignment": {"role": "customer"},
            "sender_role_evidence": ["avatar_row_structure_confirmed"],
        }
    ]
    observations = build_message_observations_v3(sidecar_messages)
    assert observations[0]["sender_role_source"] == "parent_voice"
    worker_payload = build_worker_message_ingest_payload(
        worker_target,
        {
            "ok": True,
            **_v3_contract_fields(),
            "authoritative_frame_source": "final_read",
            "observations": observations,
            "voice_transcription": {
                "state": "voice_transcribe_completed",
                "attempt_count": 1,
                "quality_flags": [],
                "transcribed_messages": sidecar_messages,
            },
        },
    )

    assert len(worker_payload["messages"]) == 1
    worker_message = worker_payload["messages"][0]
    assert worker_message["message_type"] == "voice"
    assert worker_message["content"] == transcript
    assert worker_message["raw_payload"]["observation"]["row_kind"] == "voice_transcript"
    assert worker_message["raw_payload"]["observation"]["sender_role_source"] == "parent_voice"

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=worker_payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200
    assert response.json()["data"]["ingested_count"] == 1
    assert response.json()["data"]["ignored_count"] == 0

    untrusted_message = {
        **worker_message,
        "dedupe_key": "voice-transcript-untrusted-role-source",
        "source_message_key": "voice-transcript-untrusted-role-source",
        "raw_payload": {
            **worker_message["raw_payload"],
            **_v3_raw_fields("voice-transcript-untrusted-role-source"),
            "observation": {
                **worker_message["raw_payload"]["observation"],
                "sender_role_source": "same_row_avatar",
            },
        },
    }
    untrusted_response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json={**worker_payload, "read_run_id": "read-v3-untrusted-voice-role", "messages": [untrusted_message]},
        headers=_worker_headers(worker),
    )
    assert untrusted_response.status_code == 409
    assert untrusted_response.json()["code"] == "MESSAGE_ROW_ROLE_SOURCE_UNTRUSTED"

    with SessionLocal() as db:
        event = db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).one()
        assert event.message_type == "voice"
        assert event.sender_role == "customer"
        assert event.content == transcript
        assert event.raw_payload["observation"]["row_kind"] == "voice_transcript"
        assert event.raw_payload["voice_transcription_meta"]["message"]["voice_duration"] == 5


def test_message_ingest_rejects_v2_before_any_source_processing():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    conversation_id = scan.json()["data"]["bindings"][0]["conversation_id"]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json={
            "contract_version": 2,
            "read_run_id": "read-source-conflict",
            "conversation_id": conversation_id,
            "remark_code": remark_code,
            "messages": [
                {
                    "dedupe_key": "text-key",
                    "source_message_key": "source-same-message",
                    "sender_role_hint": "self",
                    "message_type": "text",
                    "content": "同一条消息",
                    "item_state": "completed",
                    "flow_state": "completed",
                },
                {
                    "dedupe_key": "voice-key",
                    "source_message_key": "source-same-message",
                    "sender_role_hint": "customer",
                    "message_type": "voice",
                    "content": "同一条消息",
                    "item_state": "completed",
                    "flow_state": "completed",
                    "raw_payload": {"voice_transcription": "同一条消息"},
                },
            ],
        },
        headers=_worker_headers(worker),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MESSAGE_CONTRACT_V3_REQUIRED"
    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(MessageEvent.conversation_id == conversation_id).count() == 0


def test_message_ingest_rejects_v2_before_identity_processing():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676679")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    conversation_id = scan.json()["data"]["bindings"][0]["conversation_id"]

    missing_remark = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json={
            "contract_version": 2,
            "read_run_id": "read-v2-no-remark",
            "conversation_id": conversation_id,
            "messages": [],
        },
        headers=_worker_headers(worker),
    )
    assert missing_remark.status_code == 409
    assert missing_remark.json()["code"] == "MESSAGE_CONTRACT_V3_REQUIRED"

    missing_source = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json={
            "contract_version": 2,
            "read_run_id": "read-v2-no-source",
            "conversation_id": conversation_id,
            "remark_code": remark_code,
            "messages": [
                {
                    "dedupe_key": "v2-no-source",
                    "sender_role_hint": "customer",
                    "message_type": "text",
                    "content": "你好",
                    "item_state": "completed",
                    "flow_state": "completed",
                }
            ],
        },
        headers=_worker_headers(worker),
    )
    assert missing_source.status_code == 409
    assert missing_source.json()["code"] == "MESSAGE_CONTRACT_V3_REQUIRED"


def test_customer_v3_voice_is_deduped_and_collectable_for_c3():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    first = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-voice-001",
            messages=[
                _v3_message(
                    "voice-worker-key-001",
                    role="customer",
                    message_type="voice",
                    content="我想看看 SUV",
                    screen_order=1,
                    raw_extra={"voice_transcription": "我想看看 SUV", "voice_duration_seconds": 5},
                )
            ],
        ),
        headers=_worker_headers(worker),
    )
    assert first.status_code == 200
    first_data = first.json()["data"]
    assert first_data["ingested_count"] == 1
    message_event_id = first_data["results"][0]["message_event_id"]
    assert first_data["results"][0]["dedupe_key"] == "voice-worker-key-001"

    duplicated = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-voice-002",
            messages=[
                _v3_message(
                    "voice-worker-key-001",
                    role="customer",
                    message_type="voice",
                    content="我想看看 SUV",
                    screen_order=1,
                    raw_extra={"voice_transcription": "我想看看 SUV", "voice_duration_seconds": 5},
                )
            ],
        ),
        headers=_worker_headers(worker),
    )
    assert duplicated.status_code == 200
    assert duplicated.json()["data"]["duplicated_count"] == 1
    assert duplicated.json()["data"]["results"][0]["error_code"] == "MESSAGE_INGEST_DUPLICATED"

    collected = client.post(
        f"/api/internal/conversations/{binding['conversation_id']}/message-batches/collect",
        json={"trigger_message_event_id": message_event_id, "trace_id": "trace-voice-customer"},
        headers=HEADERS,
    )
    assert collected.status_code == 200
    assert collected.json()["data"]["batch_status"] == "collecting"

    with SessionLocal() as db:
        message = db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).one()
        batch = db.get(MessageBatch, collected.json()["data"]["batch_id"])
        assert message.message_type == "voice"
        assert message.content == "我想看看 SUV"
        assert message.raw_payload["voice_transcription"] == "我想看看 SUV"
        assert batch.message_event_ids == [message.id]


def test_equal_voice_transcripts_with_distinct_anchor_keys_are_both_ingested():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-equal-voice-anchors",
            messages=[
                _v3_message(
                    f"{binding['conversation_id']}:voice-anchor-a",
                    role="customer",
                    message_type="voice",
                    content="好的",
                    screen_order=1,
                ),
                _v3_message(
                    f"{binding['conversation_id']}:voice-anchor-b",
                    role="customer",
                    message_type="voice",
                    content="好的",
                    screen_order=2,
                ),
            ],
        ),
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200
    assert response.json()["data"]["ingested_count"] == 2
    with SessionLocal() as db:
        messages = db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).all()
        assert len(messages) == 2
        assert {message.dedupe_key for message in messages} == {
            f"{binding['conversation_id']}:voice-anchor-a",
            f"{binding['conversation_id']}:voice-anchor-b",
        }


def test_sales_voice_transcription_disables_ai_without_triggering_message_batch():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-sales-voice",
            messages=[
                _v3_message(
                    "sales-voice-worker-key",
                    role="self",
                    message_type="voice",
                    content="我来跟进",
                    screen_order=1,
                )
            ],
        ),
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200
    assert response.json()["data"]["ingested_count"] == 1
    with SessionLocal() as db:
        message = db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).one()
        conversation = db.get(Conversation, binding["conversation_id"])
        assert message.sender_role == "self"
        assert message.message_type == "voice"
        assert conversation.status == "sales_replied_waiting_user"
        assert conversation.ai_enabled is False
        assert db.query(MessageBatch).count() == 0
        assert db.query(ReplyAction).count() == 0


def test_voice_transcription_dict_metadata_does_not_become_message_content():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-voice-dict-meta",
            messages=[
                _v3_message(
                    "sales-voice-dict-meta",
                    role="self",
                    message_type="voice",
                    content="你中午回家吃饭不？",
                    screen_order=1,
                    raw_extra={
                        "voice_transcription": {
                            "state": "voice_transcribe_completed",
                            "attempt_count": 1,
                            "raw": {"transcribed_messages": [], "after_screenshot_path": "C:/tmp/after.png"},
                        }
                    },
                )
            ],
        ),
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200
    assert response.json()["data"]["ingested_count"] == 1
    with SessionLocal() as db:
        message = db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).one()
        assert message.message_type == "voice"
        assert message.content == "你中午回家吃饭不？"
        assert "voice_transcribe_completed" not in message.content


def test_voice_transcription_failures_are_rejected_and_create_no_reply_action():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    error_codes = [
        "VOICE_TRANSCRIBE_FAILED",
        "VOICE_TRANSCRIBE_CLICK_FAILED",
        "VOICE_TRANSCRIBE_LOCK_TIMEOUT",
        "VOICE_TRANSCRIBE_EMPTY",
        "VOICE_MESSAGE_UNCONFIRMED",
        "TARGET_NOT_CONFIRMED_FOR_VOICE_TRANSCRIBE",
    ]
    for index, error_code in enumerate(error_codes):
        response = client.post(
            f"/api/workers/{worker['id']}/wechat/messages/ingest",
            json=_v3_ingest_payload(
                binding,
                remark_code,
                read_run_id=f"read-voice-failed-{index}",
                messages=[
                    _v3_message(
                        f"voice-failed-{index}",
                        role="customer",
                        message_type="voice",
                        content="未完成语音",
                        screen_order=1,
                        raw_extra={"error_code": error_code, "voice_duration_seconds": 5},
                    )
                ],
            ),
            headers=_worker_headers(worker),
        )
        assert response.status_code == 409
        assert response.json()["code"] == error_code

    empty = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-voice-empty-duration-only",
            messages=[
                _v3_message(
                    "voice-empty",
                    role="customer",
                    message_type="voice",
                    content='5"',
                    screen_order=1,
                )
            ],
        ),
        headers=_worker_headers(worker),
    )
    assert empty.status_code == 409
    assert empty.json()["code"] == "VOICE_TRANSCRIBE_INVALID_CONTENT"
    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).count() == 0
        assert db.query(MessageBatch).count() == 0
        assert db.query(ReplyAction).count() == 0


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

    changed_locator_payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-changed-locator",
        rpa_session_key="wx-row-after-search",
        messages=[_v3_message("msg-locator-change", role="customer", message_type="text", content="短码搜索后读到的新消息", screen_order=1)],
    )
    first = client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=changed_locator_payload, headers=_worker_headers(worker))
    assert first.status_code == 200
    assert first.json()["data"]["ingested_count"] == 1

    empty_locator_payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-empty-locator",
        rpa_session_key="",
        messages=[_v3_message("msg-empty-locator", role="customer", message_type="text", content="没有稳定本地定位键", screen_order=1)],
    )
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


def test_message_ingest_rejects_mismatched_observed_remark_code():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]

    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-wrong-target",
        messages=[_v3_message("wrong-target-message", role="customer", message_type="text", content="不应写入错误会话", screen_order=1)],
    )
    payload["remark_code"] = "ANOTHER_CHAT"
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MESSAGE_TARGET_IDENTITY_MISMATCH"
    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).count() == 0


def test_message_ingest_read_target_failures_are_rejected_and_do_not_trigger_ai():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    for failure in ["target_not_confirmed", "search_not_found", "search_ambiguous"]:
        response = client.post(
            f"/api/workers/{worker['id']}/wechat/messages/ingest",
            json=_v3_ingest_payload(
                binding,
                remark_code,
                read_run_id=f"read-{failure}",
                rpa_session_key="wx-row-maybe-stale",
                messages=[
                    _v3_message(
                        f"msg-{failure}",
                        role="customer",
                        message_type="text",
                        content="这条不能触发 AI",
                        screen_order=1,
                        raw_extra={"read_result": failure},
                    )
                ],
            ),
            headers=_worker_headers(worker),
        )
        assert response.status_code == 409
        assert response.json()["code"] == failure.upper()

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
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-001",
        messages=[_v3_message("msg-cross-worker", role="customer", message_type="text", content="你好", screen_order=1)],
    )
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


def test_message_ingest_rejects_unknown_sender_and_closed_conversation():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    unknown = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json={
            **_v3_ingest_payload(
                binding,
                remark_code,
                read_run_id="read-unknown",
                messages=[_v3_message("msg-unknown", role="customer", message_type="text", content="?", screen_order=1)],
            ),
            "messages": [
                {
                    **_v3_message("msg-unknown", role="customer", message_type="text", content="?", screen_order=1),
                    "sender_role_hint": "unknown",
                }
            ],
        },
        headers=_worker_headers(worker),
    )
    assert unknown.status_code == 409
    assert unknown.json()["code"] == "MESSAGE_ROW_SENDER_ROLE_MISMATCH"

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "closed"
        db.commit()

    closed = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-closed",
            messages=[_v3_message("msg-closed", role="customer", message_type="text", content="还在吗", screen_order=1)],
        ),
        headers=_worker_headers(worker),
    )
    assert closed.status_code == 409
    assert closed.json()["code"] == "CONVERSATION_STATUS_NOT_LISTENABLE"
    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).count() == 0


def test_only_contract_self_role_can_pause_ai_for_sales_side_message():
    for role in ["self", "sales", "sales_candidate"]:
        setup_function()
        worker = _create_worker()
        _create_sales(worker["id"])
        _create_lead("王先生", "13896676678")
        remark_code = _pull_remark_code(worker)
        scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
        binding = scan.json()["data"]["bindings"][0]

        payload = _v3_ingest_payload(
            binding,
            remark_code,
            read_run_id=f"read-{role}",
            messages=[
                {
                    **_v3_message(
                        f"msg-{role}",
                        role="self",
                        message_type="text",
                        content="我是销售，稍后联系您",
                        screen_order=1,
                        raw_extra={"sender_role_confidence": 0.87},
                    ),
                    "sender_role_hint": role,
                }
            ],
        )
        payload["evidence"]["sender_role_evidence"] = {"source": "omniauto_v3_contract"}
        response = client.post(
            f"/api/workers/{worker['id']}/wechat/messages/ingest",
            json=payload,
            headers=_worker_headers(worker),
        )

        if role != "self":
            assert response.status_code == 409
            assert response.json()["code"] == "MESSAGE_ROW_SENDER_ROLE_MISMATCH"
            continue
        assert response.status_code == 200
        assert response.json()["data"]["ingested_count"] == 1
        with SessionLocal() as db:
            message = db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).one()
            conversation = db.get(Conversation, binding["conversation_id"])
            assert message.sender_role == role
            assert message.raw_payload["sender_role_confidence"] == 0.87
            assert message.evidence["sender_role_evidence"]["source"] == "omniauto_v3_contract"
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


def test_scan_result_reuses_disabled_binding_with_messages_instead_of_recreating():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("许聪", "13896676680", {"remark_code": "CJTEST01"})

    first_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-old")
    first = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=first_payload, headers=_worker_headers(worker))
    assert first.status_code == 200
    first_binding = first.json()["data"]["bindings"][0]

    with SessionLocal() as db:
        row = db.get(WechatSessionBinding, first_binding["id"])
        assert row is not None
        row.bind_status = "disabled"
        row.listen_status = "disabled"
        row.allow_listening = False
        row.error_code = "MANUAL_TEST_DISABLED"
        db.add(
            MessageEvent(
                conversation_id=row.conversation_id,
                binding_id=row.id,
                worker_id=worker["id"],
                rpa_session_key=row.rpa_session_key,
                read_run_id="read-disabled",
                dedupe_key="msg-disabled",
                sender_role="customer",
                message_type="text",
                content="已有消息引用这条 binding",
            )
        )
        db.commit()

    rescan_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-new")
    rescan_payload["scan_id"] = "scan-disabled-rescan"
    rescan_payload["sessions"][0]["display_name"] = "CJTEST01许聪"
    rescan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=rescan_payload, headers=_worker_headers(worker))

    assert rescan.status_code == 200
    binding = rescan.json()["data"]["bindings"][0]
    assert binding["id"] == first_binding["id"]
    assert binding["bind_status"] == "disabled"
    assert binding["can_ingest_messages"] is False

    with SessionLocal() as db:
        rows = db.query(WechatSessionBinding).filter(WechatSessionBinding.worker_id == worker["id"], WechatSessionBinding.remark_code == "CJTEST01").all()
        assert len(rows) == 1
        message = db.query(MessageEvent).filter(MessageEvent.binding_id == first_binding["id"]).one()
        assert message.content == "已有消息引用这条 binding"


def test_same_remark_code_duplicate_active_binding_is_rejected_and_history_stays_canonical():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("许聪", "13896676680", {"remark_code": "CJTEST01"})

    first_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-history")
    first_payload["sessions"][0]["display_name"] = "CJTEST01 许聪"
    first = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=first_payload, headers=_worker_headers(worker))
    assert first.status_code == 200
    canonical = first.json()["data"]["bindings"][0]

    with SessionLocal() as db:
        canonical_row = db.get(WechatSessionBinding, canonical["id"])
        assert canonical_row is not None
        db.add(
            MessageEvent(
                conversation_id=canonical_row.conversation_id,
                binding_id=canonical_row.id,
                worker_id=worker["id"],
                rpa_session_key=canonical_row.rpa_session_key,
                read_run_id="read-history",
                dedupe_key="msg-history",
                sender_role="customer",
                message_type="text",
                content="这条消息决定 canonical conversation",
                ingested_at=utcnow(),
            )
        )
        canonical_row.last_ingested_at = utcnow()
        db.commit()
        duplicate = WechatSessionBinding(
            worker_id=worker["id"],
            lead_id=canonical_row.lead_id,
            sales_id=canonical_row.sales_id,
            display_name="CJTEST01 许聪",
            rpa_session_key="wx-row-empty",
            row_fingerprint="fingerprint-empty",
            bind_status="bound",
            listen_status="listening",
            allow_listening=True,
            remark_code="CJTEST01",
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    rescan_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-empty")
    rescan_payload["scan_id"] = "scan-duplicate-empty"
    rescan_payload["sessions"][0]["display_name"] = "CJTEST01许聪"
    rescan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=rescan_payload, headers=_worker_headers(worker))

    assert rescan.status_code == 200
    binding = rescan.json()["data"]["bindings"][0]
    assert binding["id"] == canonical["id"]
    assert binding["conversation_id"] == canonical["conversation_id"]
    assert binding["bind_status"] == "already_bound"

    with SessionLocal() as db:
        canonical_row = db.get(WechatSessionBinding, canonical["id"])
        assert canonical_row is not None
        assert canonical_row.conversation_id == canonical["conversation_id"]
        assert canonical_row.rpa_session_key == "wx-row-empty"
        messages = db.query(MessageEvent).filter(MessageEvent.binding_id == canonical["id"]).all()
        assert [message.content for message in messages] == ["这条消息决定 canonical conversation"]


def test_same_remark_code_reuses_canonical_binding_when_sales_moves_to_another_worker():
    worker_a = _create_worker()
    sales_id = _create_sales(worker_a["id"])
    _create_lead("许聪", "13896676680", {"remark_code": "CJTEST01"})

    first = client.post(
        f"/api/workers/{worker_a['id']}/wechat/sessions/scan-result",
        json=_scan_payload("CJTEST01", rpa_session_key="wx-row-worker-a"),
        headers=_worker_headers(worker_a),
    )
    assert first.status_code == 200
    first_binding = first.json()["data"]["bindings"][0]

    worker_b = _create_worker()
    with SessionLocal() as db:
        sales = db.get(Sales, sales_id)
        assert sales is not None
        sales.worker_id = worker_b["id"]
        db.commit()

    migrated_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-worker-b")
    migrated_payload["scan_id"] = "scan-worker-b"
    migrated = client.post(
        f"/api/workers/{worker_b['id']}/wechat/sessions/scan-result",
        json=migrated_payload,
        headers=_worker_headers(worker_b),
    )

    assert migrated.status_code == 200
    migrated_binding = migrated.json()["data"]["bindings"][0]
    assert migrated_binding["id"] == first_binding["id"]
    assert migrated_binding["conversation_id"] == first_binding["conversation_id"]
    assert migrated_binding["worker_id"] == worker_b["id"]
    assert migrated_binding["rpa_session_key"] == "wx-row-worker-b"

    targets_a = client.get(f"/api/workers/{worker_a['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker_a))
    targets_b = client.get(f"/api/workers/{worker_b['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker_b))
    assert targets_a.status_code == 200
    assert targets_b.status_code == 200
    assert targets_a.json()["data"]["targets"] == []
