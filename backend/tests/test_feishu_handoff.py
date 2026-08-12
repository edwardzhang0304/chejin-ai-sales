from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.database import Base, SessionLocal, engine
from app.enums import TaskType
from app.main import app
from app.models.c3 import Conversation, HandoffEvent
from app.models.lead import Lead, LeadContact
from app.models.sales import Sales
from app.services import c3_service, feishu_service
from app.services.feishu_adapter import FeishuAdapter, FeishuAdapterError


client = TestClient(app)
HEADERS = {
    "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
    "X-Operator-Name": "Ops Tester",
    "X-Operator-Role": "admin",
}


class FakeFeishuAdapter:
    def __init__(self) -> None:
        self.lookup_calls: list[str] = []
        self.send_calls: list[tuple[str, str]] = []
        self.lookup_result = "ou_sales_current"
        self.lookup_error: FeishuAdapterError | None = None
        self.send_error: FeishuAdapterError | None = None
        self.on_lookup = None

    def lookup_open_id(self, phone: str) -> str:
        self.lookup_calls.append(phone)
        if self.on_lookup:
            self.on_lookup(phone)
        if self.lookup_error:
            raise self.lookup_error
        return self.lookup_result

    def send_text_message(self, open_id: str, text: str) -> None:
        self.send_calls.append((open_id, text))
        if self.send_error:
            raise self.send_error


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _use_fake_adapter(monkeypatch, fake: FakeFeishuAdapter) -> None:
    monkeypatch.setattr(feishu_service, "get_feishu_adapter", lambda: fake)


def _seed_conversation(*, open_id: str | None = "ou_sales_current") -> tuple[str, str, str]:
    with SessionLocal() as db:
        sales = Sales(
            sales_name="张伟",
            phone="13900000001",
            feishu_user_id=open_id,
            enabled=True,
        )
        lead = Lead(
            customer_name="王先生",
            source_type="manual",
            source_name_snapshot="人工录入",
            created_by="system",
        )
        db.add_all([sales, lead])
        db.flush()
        db.add(
            LeadContact(
                lead_id=lead.id,
                contact_type="phone",
                contact_value_encrypted="encrypted",
                contact_value_normalized="13896676678",
                contact_hash="hash",
                masked_value="138****6678",
                is_primary=True,
            )
        )
        conversation = Conversation(
            lead_id=lead.id,
            sales_id=sales.id,
            status="ai_active",
            ai_enabled=True,
        )
        db.add(conversation)
        db.commit()
        return conversation.conversation_id, sales.id, lead.id


def _create_handoff(conversation_id: str, *, reason: str = "CUSTOMER_HIGH_INTENT") -> str:
    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        assert conversation is not None
        event, created = c3_service._create_or_reuse_open_handoff(
            db,
            conversation=conversation,
            batch_id="batch-one",
            handoff_reason_code=reason,
            reason_detail=reason,
            trigger_message_event_ids=["message-one"],
            risk_flags=["high_intent"],
            evidence_refs=["message:message-one"],
            ai_payload={},
        )
        assert created is True
        conversation.status = "waiting_sales_reply"
        event_id = event.id
        db.commit()
        return event_id


def test_sales_phone_is_required_and_server_managed_ids_are_rejected(monkeypatch):
    fake = FakeFeishuAdapter()
    _use_fake_adapter(monkeypatch, fake)

    missing_phone = client.post(
        "/api/sales",
        json={"sales_name": "张伟", "enabled": True},
        headers=HEADERS,
    )
    assert missing_phone.status_code == 400

    for field in (
        "feishu_user_id",
        "open_id",
        "feishu_open_id",
        "user_id",
        "union_id",
    ):
        response = client.post(
            "/api/sales",
            json={
                "sales_name": "张伟",
                "phone": "13900000001",
                field: "ou_injected",
            },
            headers=HEADERS,
        )
        assert response.status_code == 422
        assert response.json()["code"] == "SALES_FEISHU_ID_SERVER_MANAGED"


def test_sales_create_normalizes_phone_and_only_returns_binding_status(monkeypatch):
    fake = FakeFeishuAdapter()
    _use_fake_adapter(monkeypatch, fake)

    response = client.post(
        "/api/sales",
        json={"sales_name": "张伟", "phone": "139-0000-0001", "enabled": True},
        headers=HEADERS,
    )
    assert response.status_code == 200
    sales_id = response.json()["data"]["id"]

    with SessionLocal() as db:
        sales = db.get(Sales, sales_id)
        assert sales.phone == "13900000001"
        assert sales.feishu_user_id == "ou_sales_current"
    detail = client.get(f"/api/sales/{sales_id}", headers=HEADERS).json()["data"]
    assert detail["phone"] == "139****0001"
    assert detail["feishu_binding_status"] == "matched"
    assert "feishu_user_id" not in detail
    listed = client.get("/api/sales", headers=HEADERS).json()["data"]["items"]
    assert all("feishu_user_id" not in item for item in listed)
    assert all("open_id" not in item for item in listed)
    assert fake.lookup_calls == ["13900000001"]


def test_sales_update_without_phone_preserves_phone_and_open_id(monkeypatch):
    fake = FakeFeishuAdapter()
    _use_fake_adapter(monkeypatch, fake)
    created = client.post(
        "/api/sales",
        json={"sales_name": "张伟", "phone": "13900000001", "enabled": True},
        headers=HEADERS,
    )
    sales_id = created.json()["data"]["id"]
    fake.lookup_calls.clear()

    updated = client.put(
        f"/api/sales/{sales_id}",
        json={"sales_name": "张伟（华东）", "enabled": False},
        headers=HEADERS,
    )

    assert updated.status_code == 200
    assert fake.lookup_calls == []
    with SessionLocal() as db:
        sales = db.get(Sales, sales_id)
        assert sales.sales_name == "张伟（华东）"
        assert sales.phone == "13900000001"
        assert sales.feishu_user_id == "ou_sales_current"


def test_sales_update_new_phone_queries_open_id_once_and_rejects_masked_phone(monkeypatch):
    fake = FakeFeishuAdapter()
    _use_fake_adapter(monkeypatch, fake)
    created = client.post(
        "/api/sales",
        json={"sales_name": "张伟", "phone": "13900000001", "enabled": True},
        headers=HEADERS,
    )
    sales_id = created.json()["data"]["id"]
    fake.lookup_calls.clear()

    masked = client.put(
        f"/api/sales/{sales_id}",
        json={"phone": "139****0001"},
        headers=HEADERS,
    )
    assert masked.status_code == 422
    assert fake.lookup_calls == []

    changed = client.put(
        f"/api/sales/{sales_id}",
        json={"phone": "13800000002"},
        headers=HEADERS,
    )
    assert changed.status_code == 200
    assert fake.lookup_calls == ["13800000002"]
    with SessionLocal() as db:
        sales = db.get(Sales, sales_id)
        assert sales.phone == "13800000002"
        assert sales.feishu_user_id == "ou_sales_current"


def test_sales_update_rejects_every_server_managed_feishu_id(monkeypatch):
    fake = FakeFeishuAdapter()
    _use_fake_adapter(monkeypatch, fake)
    created = client.post(
        "/api/sales",
        json={"sales_name": "张伟", "phone": "13900000001", "enabled": True},
        headers=HEADERS,
    )
    sales_id = created.json()["data"]["id"]

    for field in (
        "feishu_user_id",
        "open_id",
        "feishu_open_id",
        "user_id",
        "union_id",
    ):
        response = client.put(
            f"/api/sales/{sales_id}",
            json={field: "ou_injected"},
            headers=HEADERS,
        )
        assert response.status_code == 422
        assert response.json()["code"] == "SALES_FEISHU_ID_SERVER_MANAGED"


def test_phone_change_commits_empty_id_before_lookup_and_discards_stale_response(monkeypatch):
    fake = FakeFeishuAdapter()
    _use_fake_adapter(monkeypatch, fake)
    created = client.post(
        "/api/sales",
        json={"sales_name": "张伟", "phone": "13900000001", "enabled": True},
        headers=HEADERS,
    )
    sales_id = created.json()["data"]["id"]
    fake.lookup_result = "ou_old_response"

    def change_phone_while_provider_is_running(_phone: str) -> None:
        with SessionLocal() as db:
            sales = db.get(Sales, sales_id)
            assert sales.feishu_user_id is None
            sales.phone = "13900000003"
            db.commit()

    fake.on_lookup = change_phone_while_provider_is_running
    updated = client.put(
        f"/api/sales/{sales_id}",
        json={"sales_name": "张伟", "phone": "13900000002", "enabled": True},
        headers=HEADERS,
    )
    assert updated.status_code == 200

    with SessionLocal() as db:
        sales = db.get(Sales, sales_id)
        assert sales.phone == "13900000003"
        assert sales.feishu_user_id is None


def test_lookup_failure_does_not_roll_back_sales_profile(monkeypatch):
    fake = FakeFeishuAdapter()
    fake.lookup_error = FeishuAdapterError(
        "FEISHU_OPEN_ID_NOT_FOUND",
        "open_id_not_found",
    )
    _use_fake_adapter(monkeypatch, fake)

    response = client.post(
        "/api/sales",
        json={"sales_name": "张伟", "phone": "13900000001", "enabled": True},
        headers=HEADERS,
    )
    assert response.status_code == 200
    with SessionLocal() as db:
        sales = db.get(Sales, response.json()["data"]["id"])
        assert sales.phone == "13900000001"
        assert sales.feishu_user_id is None


def test_handoff_and_waiting_status_commit_before_single_notification(monkeypatch):
    fake = FakeFeishuAdapter()
    _use_fake_adapter(monkeypatch, fake)
    conversation_id, _, _ = _seed_conversation()

    event_id = _create_handoff(conversation_id)

    assert len(fake.send_calls) == 1
    sent_open_id, message = fake.send_calls[0]
    assert sent_open_id == "ou_sales_current"
    assert "王先生" in message
    assert "138****6678" in message
    assert "请前往微信处理" in message
    assert "13896676678" not in message
    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        event = db.get(HandoffEvent, event_id)
        assert conversation.status == "waiting_sales_reply"
        assert event.notify_status == "succeeded"
        assert event.notify_attempted_at is not None
        assert event.notify_completed_at is not None


@pytest.mark.parametrize(
    ("context_change", "expected_code"),
    [
        ("missing_sales_id", "HANDOFF_SALES_ID_MISSING"),
        ("missing_sales", "HANDOFF_SALES_NOT_FOUND"),
        ("missing_open_id", "FEISHU_OPEN_ID_MISSING"),
    ],
)
def test_notification_recipient_fails_closed_without_conversation_sales_route(
    monkeypatch,
    context_change,
    expected_code,
):
    fake = FakeFeishuAdapter()
    _use_fake_adapter(monkeypatch, fake)
    conversation_id, sales_id, _ = _seed_conversation()
    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        sales = db.get(Sales, sales_id)
        if context_change == "missing_sales_id":
            conversation.sales_id = None
        elif context_change == "missing_sales":
            sales.deleted_at = c3_service.utcnow()
        else:
            sales.feishu_user_id = None
        event = HandoffEvent(
            conversation_id=conversation_id,
            status="created",
            handoff_reason_code="HANDOFF_REQUIRED",
            notify_status="pending",
        )
        db.add(event)
        db.commit()
        event_id = event.id

    result = feishu_service.dispatch_handoff_notification(event_id, adapter=fake)

    assert result == expected_code
    assert fake.send_calls == []
    with SessionLocal() as db:
        event = db.get(HandoffEvent, event_id)
        assert event.notify_status == "failed"
        assert event.notify_error_code == expected_code


def test_transaction_rollback_never_calls_feishu(monkeypatch):
    fake = FakeFeishuAdapter()
    _use_fake_adapter(monkeypatch, fake)
    conversation_id, _, _ = _seed_conversation()

    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        c3_service._create_or_reuse_open_handoff(
            db,
            conversation=conversation,
            batch_id="batch-rollback",
            handoff_reason_code="HANDOFF_REQUIRED",
            reason_detail="HANDOFF_REQUIRED",
            trigger_message_event_ids=[],
            risk_flags=[],
            evidence_refs=[],
            ai_payload={},
        )
        conversation.status = "waiting_sales_reply"
        db.rollback()

    assert fake.send_calls == []
    with SessionLocal() as db:
        assert db.scalar(select(HandoffEvent)) is None
        assert db.get(Conversation, conversation_id).status == "ai_active"


def test_duplicate_handoff_reuses_event_and_cannot_reacquire_send_right(monkeypatch):
    fake = FakeFeishuAdapter()
    _use_fake_adapter(monkeypatch, fake)
    conversation_id, _, _ = _seed_conversation()
    first_id = _create_handoff(conversation_id)

    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        event, created = c3_service._create_or_reuse_open_handoff(
            db,
            conversation=conversation,
            batch_id="batch-two",
            handoff_reason_code="C2_IMAGE_UNDERSTANDING_FAILED",
            reason_detail="ignored duplicate reason",
            trigger_message_event_ids=["message-two"],
            risk_flags=["media_failed"],
            evidence_refs=[],
            ai_payload={},
        )
        db.commit()
        assert created is False
        assert event.id == first_id

    assert len(fake.send_calls) == 1
    assert feishu_service.dispatch_handoff_notification(first_id, adapter=fake) == "not_claimed"
    assert len(fake.send_calls) == 1


def test_database_unique_constraint_rejects_two_open_handoffs():
    conversation_id, _, _ = _seed_conversation()
    with SessionLocal() as db:
        db.add(
            HandoffEvent(
                conversation_id=conversation_id,
                status="created",
                handoff_reason_code="ONE",
            )
        )
        db.commit()
    with SessionLocal() as db:
        db.add(
            HandoffEvent(
                conversation_id=conversation_id,
                status="created",
                handoff_reason_code="TWO",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_postgres_concurrent_handoffs_reuse_one_event_and_one_send_right(monkeypatch):
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL handoff row-lock concurrency test")

    fake = FakeFeishuAdapter()
    _use_fake_adapter(monkeypatch, fake)
    conversation_id, _, _ = _seed_conversation()
    start = threading.Barrier(2)
    outcomes: list[tuple[str, bool]] = []
    failures: list[Exception] = []
    result_lock = threading.Lock()

    def create_handoff(batch_id: str) -> None:
        try:
            with SessionLocal() as db:
                conversation = db.get(Conversation, conversation_id)
                start.wait(timeout=5)
                event, created = c3_service._create_or_reuse_open_handoff(
                    db,
                    conversation=conversation,
                    batch_id=batch_id,
                    handoff_reason_code="CUSTOMER_HIGH_INTENT",
                    reason_detail="customer_high_intent",
                    trigger_message_event_ids=[f"message-{batch_id}"],
                    risk_flags=["high_intent"],
                    evidence_refs=[],
                    ai_payload={},
                )
                conversation.status = "waiting_sales_reply"
                event_id = event.id
                db.commit()
            with result_lock:
                outcomes.append((event_id, created))
        except Exception as exc:  # pragma: no cover - asserted below
            with result_lock:
                failures.append(exc)

    threads = [
        threading.Thread(target=create_handoff, args=("batch-one",), daemon=True),
        threading.Thread(target=create_handoff, args=("batch-two",), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert all(not thread.is_alive() for thread in threads)
    assert len(outcomes) == 2
    assert len({event_id for event_id, _created in outcomes}) == 1
    assert sorted(created for _event_id, created in outcomes) == [False, True]
    assert len(fake.send_calls) == 1
    with SessionLocal() as db:
        events = list(
            db.scalars(
                select(HandoffEvent).where(
                    HandoffEvent.conversation_id == conversation_id,
                    HandoffEvent.closed_at.is_(None),
                )
            ).all()
        )
        assert len(events) == 1
        assert events[0].notify_status == "succeeded"


def test_explicit_failure_and_timeout_never_restore_ai_or_retry(monkeypatch):
    fake = FakeFeishuAdapter()
    fake.send_error = FeishuAdapterError(
        "FEISHU_MESSAGE_SEND_FAILED",
        "provider_code=230001",
    )
    _use_fake_adapter(monkeypatch, fake)
    conversation_id, _, _ = _seed_conversation()
    event_id = _create_handoff(conversation_id)

    with SessionLocal() as db:
        event = db.get(HandoffEvent, event_id)
        conversation = db.get(Conversation, conversation_id)
        assert event.notify_status == "failed"
        assert event.notify_error_code == "FEISHU_MESSAGE_SEND_FAILED"
        assert conversation.status == "waiting_sales_reply"
        assert conversation.ai_enabled is True
    assert feishu_service.dispatch_handoff_notification(event_id, adapter=fake) == "not_claimed"
    assert len(fake.send_calls) == 1

    timeout_fake = FakeFeishuAdapter()
    timeout_fake.send_error = FeishuAdapterError(
        "FEISHU_MESSAGE_SEND_FAILED",
        "provider_transport_error=TimeoutError",
        result_unknown=True,
    )
    _use_fake_adapter(monkeypatch, timeout_fake)
    second_conversation_id, _, _ = _seed_conversation(open_id="ou_second_sales")
    second_event_id = _create_handoff(second_conversation_id)
    with SessionLocal() as db:
        second_event = db.get(HandoffEvent, second_event_id)
        assert second_event.notify_error_code == "FEISHU_NOTIFY_RESULT_UNKNOWN"
        assert second_event.notify_status == "failed"


def test_restart_sends_unattempted_pending_but_never_resends_sending(monkeypatch):
    fake = FakeFeishuAdapter()
    _use_fake_adapter(monkeypatch, fake)
    first_conversation_id, _, _ = _seed_conversation()
    second_conversation_id, _, _ = _seed_conversation(open_id="ou_second_sales")
    with SessionLocal() as db:
        pending = HandoffEvent(
            conversation_id=first_conversation_id,
            status="created",
            handoff_reason_code="HANDOFF_REQUIRED",
            notify_status="pending",
        )
        sending = HandoffEvent(
            conversation_id=second_conversation_id,
            status="created",
            handoff_reason_code="HANDOFF_REQUIRED",
            notify_status="sending",
        )
        db.add_all([pending, sending])
        db.commit()
        pending_id = pending.id
        sending_id = sending.id

    result = feishu_service.recover_handoff_notifications(adapter=fake)
    assert result == {"unknown_settled": 1, "pending_attempted": 1}
    assert len(fake.send_calls) == 1
    with SessionLocal() as db:
        assert db.get(HandoffEvent, pending_id).notify_status == "succeeded"
        sending = db.get(HandoffEvent, sending_id)
        assert sending.notify_status == "failed"
        assert sending.notify_error_code == "FEISHU_NOTIFY_RESULT_UNKNOWN"


def test_state_reprojection_does_not_create_event_or_notify(monkeypatch):
    fake = FakeFeishuAdapter()
    _use_fake_adapter(monkeypatch, fake)
    conversation_id, _, _ = _seed_conversation()
    with SessionLocal() as db:
        event = HandoffEvent(
            conversation_id=conversation_id,
            status="created",
            handoff_reason_code="HANDOFF_REQUIRED",
            notify_status="succeeded",
        )
        db.add(event)
        db.commit()

    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        conversation.status = "ai_active"
        events = c3_service.enforce_open_handoff_gate(db, conversation, for_update=True)
        db.commit()
        assert len(events) == 1
        assert conversation.status == "waiting_sales_reply"

    with SessionLocal() as db:
        assert db.query(HandoffEvent).count() == 1
    assert fake.send_calls == []


def test_adapter_uses_fixed_paths_caches_token_and_requires_business_success():
    calls: list[tuple[str, dict, dict[str, str]]] = []
    responses = [
        (200, {"code": 0, "tenant_access_token": "token-value", "expire": 7200}),
        (
            200,
            {
                "code": 0,
                "data": {
                    "user_list": [
                        {"mobile": "13900000001", "user_id": "ou_sales_current"}
                    ]
                },
            },
        ),
        (200, {"code": 0}),
    ]

    def requester(method, url, headers, body, timeout):
        calls.append((url, json.loads(body), headers))
        status, payload = responses.pop(0)
        return status, json.dumps(payload).encode()

    adapter = FeishuAdapter(
        app_id="cli_test",
        app_secret="secret-value",
        requester=requester,
    )
    assert adapter.lookup_open_id("13900000001") == "ou_sales_current"
    adapter.send_text_message("ou_sales_current", "测试通知")

    assert len(calls) == 3
    assert calls[0][0].endswith("/open-apis/auth/v3/tenant_access_token/internal")
    assert calls[1][0].endswith(
        "/open-apis/contact/v3/users/batch_get_id?user_id_type=open_id"
    )
    assert calls[1][1] == {"mobiles": ["13900000001"]}
    assert calls[2][0].endswith(
        "/open-apis/im/v1/messages?receive_id_type=open_id"
    )
    assert calls[2][1]["receive_id"] == "ou_sales_current"
    assert calls[2][1]["msg_type"] == "text"
    assert calls[2][2]["Authorization"] == "Bearer token-value"


@pytest.mark.parametrize(
    ("lookup_payload", "expected_code"),
    [
        ({"code": 0, "data": {"user_list": []}}, "FEISHU_OPEN_ID_NOT_FOUND"),
        (
            {
                "code": 0,
                "data": {
                    "user_list": [
                        {"mobile": "13900000001", "user_id": "ou_first"},
                        {"mobile": "13900000001", "user_id": "ou_second"},
                    ]
                },
            },
            "FEISHU_OPEN_ID_CONFLICT",
        ),
        (
            {"code": 99991663, "msg": "user is outside app scope"},
            "FEISHU_USER_OUT_OF_SCOPE",
        ),
    ],
)
def test_adapter_fails_closed_for_directory_errors_without_retry(
    lookup_payload,
    expected_code,
):
    calls = []
    responses = [
        (200, {"code": 0, "tenant_access_token": "token-value", "expire": 7200}),
        (200, lookup_payload),
    ]

    def requester(method, url, headers, body, timeout):
        calls.append(url)
        status, payload = responses.pop(0)
        return status, json.dumps(payload).encode()

    adapter = FeishuAdapter(
        app_id="cli_test",
        app_secret="secret-value",
        requester=requester,
    )
    with pytest.raises(FeishuAdapterError) as exc:
        adapter.lookup_open_id("13900000001")

    assert exc.value.code == expected_code
    assert len(calls) == 2


def test_send_settlement_crash_is_unknown_after_restart_and_never_resends(monkeypatch):
    fake = FakeFeishuAdapter()
    _use_fake_adapter(monkeypatch, fake)
    conversation_id, _, _ = _seed_conversation()
    with SessionLocal() as db:
        event = HandoffEvent(
            conversation_id=conversation_id,
            status="created",
            handoff_reason_code="HANDOFF_REQUIRED",
            notify_status="pending",
        )
        db.add(event)
        db.commit()
        event_id = event.id

    original_settle = feishu_service._settle_notification

    def crash_after_provider_send(*args, **kwargs):
        raise RuntimeError("simulated_process_crash")

    monkeypatch.setattr(feishu_service, "_settle_notification", crash_after_provider_send)
    with pytest.raises(RuntimeError, match="simulated_process_crash"):
        feishu_service.dispatch_handoff_notification(event_id, adapter=fake)
    assert len(fake.send_calls) == 1
    with SessionLocal() as db:
        assert db.get(HandoffEvent, event_id).notify_status == "sending"

    monkeypatch.setattr(feishu_service, "_settle_notification", original_settle)
    result = feishu_service.recover_handoff_notifications(adapter=fake)
    assert result == {"unknown_settled": 1, "pending_attempted": 0}
    assert len(fake.send_calls) == 1
    with SessionLocal() as db:
        event = db.get(HandoffEvent, event_id)
        assert event.notify_status == "failed"
        assert event.notify_error_code == "FEISHU_NOTIFY_RESULT_UNKNOWN"


def test_notification_errors_are_redacted_and_bounded():
    unsafe = (
        "Bearer live-token app_secret=secret-value "
        "tenant_access_token=tenant-value 13900000001 ou_full_open_id "
        + "x" * 600
    )
    safe = feishu_service._safe_error_summary(unsafe)

    assert len(safe) <= 512
    assert "live-token" not in safe
    assert "secret-value" not in safe
    assert "tenant-value" not in safe
    assert "13900000001" not in safe
    assert "ou_full_open_id" not in safe


def test_backfill_clears_legacy_ids_and_only_restores_current_app_open_ids():
    first_conversation_id, first_sales_id, _ = _seed_conversation(open_id="legacy-user-id")
    second_conversation_id, second_sales_id, _ = _seed_conversation(open_id="legacy-union-id")
    assert first_conversation_id != second_conversation_id
    fake = FakeFeishuAdapter()
    fake.lookup_result = "ou_current_app"

    result = feishu_service.backfill_sales_open_ids(adapter=fake)

    assert result == {"matched": 2, "failed": 0, "stale": 0}
    with SessionLocal() as db:
        assert db.get(Sales, first_sales_id).feishu_user_id == "ou_current_app"
        assert db.get(Sales, second_sales_id).feishu_user_id == "ou_current_app"
    assert fake.lookup_calls == ["13900000001", "13900000001"]


def test_no_handoff_notify_task_type_was_added():
    assert {item.value for item in TaskType} == {"add_friend", "chat_reply"}
