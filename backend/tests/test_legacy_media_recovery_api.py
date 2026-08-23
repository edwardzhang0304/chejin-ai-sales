from fastapi.testclient import TestClient

from app.core.database import Base, SessionLocal, engine
from app.main import app
from app.models.audit import OperationLog
from app.models.c3 import Conversation, HandoffEvent, MessageBatch, ReplyAction
from app.models.task import Task
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker


client = TestClient(app)
ADMIN_HEADERS = {
    "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
    "X-Operator-Name": "Ops Tester",
    "X-Operator-Role": "admin",
}


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def create_bound_running_worker(name: str) -> tuple[dict, dict[str, str]]:
    created = client.post(
        "/api/workers",
        json={
            "worker_name": name,
            "device_name": "Windows UAT",
            "platform": "windows",
            "enabled": True,
        },
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 200, created.text
    worker = created.json()["data"]
    bound = client.post(
        f"/api/workers/{worker['id']}/client-bind",
        json={
            "worker_token": worker["worker_token"],
            "client_instance_id": "legacy-recovery-client",
        },
    )
    assert bound.status_code == 200, bound.text
    heartbeat = client.post(
        f"/api/workers/{worker['id']}/heartbeat",
        json={
            "client_instance_id": "legacy-recovery-client",
            "run_status": "running",
            "rpa_component_status": "ready",
            "wechat_status": "logged_in",
            "running_status": "idle",
            "current_task": None,
        },
        headers={"X-Worker-Token": worker["worker_token"]},
    )
    assert heartbeat.status_code == 200, heartbeat.text
    return worker, {
        "X-Worker-Token": worker["worker_token"],
        "X-Client-Instance-Id": "legacy-recovery-client",
    }


def start_read_flow(
    worker: dict,
    headers: dict[str, str],
    flow_id: str,
    *,
    flow_kind: str = "c2_read",
) -> None:
    response = client.post(
        f"/api/workers/{worker['id']}/inflight-flow/start",
        json={"flow_id": flow_id, "flow_kind": flow_kind},
        headers=headers,
    )
    assert response.status_code == 200, response.text


def seed_conversation(worker_id: str, conversation_id: str) -> None:
    with SessionLocal() as db:
        db.add(
            Conversation(
                conversation_id=conversation_id,
                worker_id=worker_id,
                status="ai_active",
                ai_enabled=True,
            )
        )
        db.add(
            WechatSessionBinding(
                conversation_id=conversation_id,
                worker_id=worker_id,
                display_name="CJLEGACY",
                remark_code="CJLEGACY",
                rpa_session_key="wx:legacy:recovery",
                row_fingerprint="legacy-row",
                bind_status="bound",
                listen_status="listening",
                allow_listening=True,
            )
        )
        db.commit()


def test_customer_known_legacy_terminal_creates_one_handoff_without_message():
    worker, headers = create_bound_running_worker("Legacy handoff Worker")
    conversation_id = "11111111-2222-3333-4444-000000000931"
    flow_id = "read-legacy-customer-known"
    digest = "a" * 64
    seed_conversation(worker["id"], conversation_id)
    start_read_flow(worker, headers, flow_id)
    payload = {
        "flow_id": flow_id,
        "legacy_record_digest": digest,
        "resolution": "legacy_identity_unresolved_handoff",
        "conversation_id": conversation_id,
        "record_summary": {
            "journal_count": 1,
            "ledger_count": 0,
            "action_journal_count": 0,
            "outbox_count": 0,
            "action_kinds": ["voice"],
        },
    }

    first = client.post(
        f"/api/workers/{worker['id']}/legacy-media-recovery/settle",
        json=payload,
        headers=headers,
    )
    second = client.post(
        f"/api/workers/{worker['id']}/legacy-media-recovery/settle",
        json=payload,
        headers=headers,
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["data"]["confirmed"] is True
    assert first.json()["data"]["flow_released"] is True
    assert second.json()["data"]["duplicated"] is True
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0
        assert db.query(MessageBatch).count() == 1
        assert db.query(ReplyAction).count() == 0
        assert db.query(Task).count() == 0
        assert db.query(HandoffEvent).count() == 1
        assert (
            db.query(HandoffEvent).one().handoff_reason_code
            == "LEGACY_MEDIA_IDENTITY_UNRESOLVED"
        )
        assert db.get(Conversation, conversation_id).status == "waiting_sales_reply"
        assert db.query(OperationLog).filter(
            OperationLog.event_type
            == "worker_legacy_media_recovery_settled"
        ).count() == 1
        assert db.get(Worker, worker["id"]).inflight_flow_state == {}


def test_owner_unknown_legacy_terminal_is_one_review_incident_without_handoff():
    worker, headers = create_bound_running_worker("Legacy incident Worker")
    flow_id = "read-legacy-owner-unknown"
    digest = "b" * 64
    start_read_flow(worker, headers, flow_id)
    payload = {
        "flow_id": flow_id,
        "legacy_record_digest": digest,
        "resolution": "legacy_owner_unknown_incident",
        "conversation_id": None,
        "record_summary": {
            "journal_count": 0,
            "ledger_count": 2,
            "action_journal_count": 0,
            "outbox_count": 0,
            "action_kinds": ["voice", "image"],
        },
    }

    first = client.post(
        f"/api/workers/{worker['id']}/legacy-media-recovery/settle",
        json=payload,
        headers=headers,
    )
    second = client.post(
        f"/api/workers/{worker['id']}/legacy-media-recovery/settle",
        json=payload,
        headers=headers,
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["data"]["manual_review_required"] is True
    assert second.json()["data"]["duplicated"] is True
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0
        assert db.query(MessageBatch).count() == 0
        assert db.query(ReplyAction).count() == 0
        assert db.query(Task).count() == 0
        assert db.query(HandoffEvent).count() == 0
        logs = db.query(OperationLog).filter(
            OperationLog.event_type
            == "worker_legacy_media_owner_unknown"
        ).all()
        assert len(logs) == 1
        assert logs[0].extra_metadata["error_code"] == (
            "LEGACY_MEDIA_OWNER_UNKNOWN"
        )
        assert logs[0].extra_metadata["review_status"] == (
            "pending_manual_review"
        )
        assert db.get(Worker, worker["id"]).inflight_flow_state == {}

    operation_logs = client.get(
        "/api/operation-logs",
        params={"event_type": "worker_legacy_media_owner_unknown"},
        headers=ADMIN_HEADERS,
    )
    assert operation_logs.status_code == 200, operation_logs.text
    item = operation_logs.json()["data"]["items"][0]
    assert item["result"] == "failed"
    assert item["event_name"] == "旧媒体归属待人工检查"
    assert "需要人工检查" in item["summary"]


def test_stale_conversation_never_creates_handoff_for_an_unowned_customer():
    worker, headers = create_bound_running_worker("Legacy stale owner Worker")
    flow_id = "read-legacy-stale-conversation"
    start_read_flow(worker, headers, flow_id)

    response = client.post(
        f"/api/workers/{worker['id']}/legacy-media-recovery/settle",
        json={
            "flow_id": flow_id,
            "legacy_record_digest": "c" * 64,
            "resolution": "legacy_identity_unresolved_handoff",
            "conversation_id": "11111111-2222-3333-4444-000000000999",
            "record_summary": {
                "journal_count": 1,
                "ledger_count": 0,
                "action_journal_count": 0,
                "outbox_count": 0,
                "action_kinds": ["image"],
            },
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["resolution"] == "legacy_owner_unknown_incident"
    assert result["conversation_id"] is None
    assert result["manual_review_required"] is True
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0
        assert db.query(MessageBatch).count() == 0
        assert db.query(HandoffEvent).count() == 0
        assert db.query(ReplyAction).count() == 0
        assert db.query(Task).count() == 0


def test_lost_response_retry_reuses_terminal_even_if_binding_later_appears():
    worker, headers = create_bound_running_worker("Legacy retry Worker")
    conversation_id = "11111111-2222-3333-4444-000000000998"
    flow_id = "read-legacy-lost-response"
    digest = "d" * 64
    start_read_flow(worker, headers, flow_id)
    payload = {
        "flow_id": flow_id,
        "legacy_record_digest": digest,
        "resolution": "legacy_identity_unresolved_handoff",
        "conversation_id": conversation_id,
        "record_summary": {"journal_count": 1, "action_kinds": ["voice"]},
    }

    first = client.post(
        f"/api/workers/{worker['id']}/legacy-media-recovery/settle",
        json=payload,
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert first.json()["data"]["resolution"] == (
        "legacy_owner_unknown_incident"
    )

    # Simulate a client that lost the first HTTP response while the binding
    # was repaired independently. The identical retry must reuse the durable
    # incident instead of changing terminal kind or returning 409 forever.
    seed_conversation(worker["id"], conversation_id)
    second = client.post(
        f"/api/workers/{worker['id']}/legacy-media-recovery/settle",
        json=payload,
        headers=headers,
    )

    assert second.status_code == 200, second.text
    assert second.json()["data"]["duplicated"] is True
    assert second.json()["data"]["resolution"] == (
        "legacy_owner_unknown_incident"
    )
    with SessionLocal() as db:
        assert db.query(OperationLog).filter(
            OperationLog.event_type
            == "worker_legacy_media_owner_unknown"
        ).count() == 1
        assert db.query(MessageEvent).count() == 0
        assert db.query(MessageBatch).count() == 0
        assert db.query(HandoffEvent).count() == 0
        assert db.query(ReplyAction).count() == 0
        assert db.query(Task).count() == 0


def test_legacy_endpoint_never_clears_a_non_c2_flow():
    worker, headers = create_bound_running_worker("Legacy wrong flow Worker")
    flow_id = "task-flow-must-survive"
    start_read_flow(worker, headers, flow_id, flow_kind="task")

    response = client.post(
        f"/api/workers/{worker['id']}/legacy-media-recovery/settle",
        json={
            "flow_id": flow_id,
            "legacy_record_digest": "e" * 64,
            "resolution": "legacy_owner_unknown_incident",
            "conversation_id": None,
            "record_summary": {},
        },
        headers=headers,
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == (
        "LEGACY_MEDIA_RECOVERY_FLOW_KIND_INVALID"
    )
    with SessionLocal() as db:
        assert db.get(Worker, worker["id"]).inflight_flow_state["flow_id"] == (
            flow_id
        )
        assert db.query(OperationLog).filter(
            OperationLog.event_type.in_(
                {
                    "worker_legacy_media_recovery_settled",
                    "worker_legacy_media_owner_unknown",
                }
            )
        ).count() == 0
