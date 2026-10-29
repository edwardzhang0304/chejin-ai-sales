import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.database import Base, SessionLocal, engine
from app.main import app
from app.enums import TaskBlockCode, TaskStatus, TaskType
from app.models.feiyu import FeiyuLeadReceipt
from app.models.lead import AssignmentRoundRobinState, Lead, LeadAssignment, LeadContact, LeadDuplicateEvent, LeadNote
from app.models.task import Task
from app.services import lead_service


client = TestClient(app)
TEST_SECRET = "test-feiyu-forward-secret"
FEIYU_LEADS_URL = "/api/integrations/feiyu/leads"


@pytest.fixture(autouse=True)
def configure_feiyu_secret(monkeypatch):
    from app.api.routes import feiyu

    monkeypatch.setattr(
        feiyu,
        "get_settings",
        lambda: SimpleNamespace(
            feiyu_forward_secret=TEST_SECRET,
            feiyu_account_id="default",
            feiyu_account_ids="default,account-a,account-b",
        ),
    )


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _headers(secret: str = TEST_SECRET, account_id: str | None = None) -> dict[str, str]:
    headers = {"X-Feiyu-Forward-Secret": secret}
    if account_id:
        headers["X-Feiyu-Account-ID"] = account_id
    return headers


def _create_sales(*names: str) -> list[str]:
    sales_ids = []
    for index, name in enumerate(names):
        response = client.post(
            "/api/sales",
            json={
                "sales_name": name,
                "phone": f"1390000000{index + 1}",
                "enabled": True,
                "sort_order": (index + 1) * 10,
            },
        )
        assert response.status_code == 200
        sales_ids.append(response.json()["data"]["id"])
    return sales_ids


def _lead_count() -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Lead)) or 0


def _manual_create(name: str, phone: str):
    return client.post(
        "/api/leads",
        json={"customer_name": name, "phones": [phone]},
    )


def _round_robin_cursor() -> str | None:
    with SessionLocal() as db:
        state = db.get(AssignmentRoundRobinState, 1)
        return state.current_sales_id if state else None


def test_valid_secret_creates_native_lead_with_optional_fields_and_assignment():
    sales_ids = _create_sales("张伟")

    response = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={
            "customer_name": "王先生",
            "phones": ["13896676678"],
            "wechats": ["wang_wechat"],
            "emails": ["wang@example.com"],
            "custom_fields": {"feiyu": {"id": "clue-123", "adv_id": "ad-456"}},
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["created"] is True
    assert data["sales_id"] == sales_ids[0]
    assert data["assignment"]["status"] == "succeeded"
    assert data["assignment"]["failure_reason"] is None

    with SessionLocal() as db:
        lead = db.get(Lead, data["id"])
        contacts = list(db.scalars(select(LeadContact).where(LeadContact.lead_id == data["id"])))
        assert lead is not None
        assert lead.source_type == "feiyu"
        assert lead.source_name_snapshot == "飞鱼线索"
        assert lead.custom_fields["feiyu"] == {"id": "clue-123", "adv_id": "ad-456"}
        assert isinstance(lead.custom_fields.get("remark_code"), str)
        assert {contact.contact_type for contact in contacts} == {"phone", "wechat", "email"}
        assert lead.sales_id == sales_ids[0]


def test_missing_wrong_or_unconfigured_secret_returns_401_without_creating_lead(monkeypatch):
    missing = client.post(FEIYU_LEADS_URL, json={"customer_name": "王先生", "phones": ["13896676678"]})
    wrong = client.post(
        FEIYU_LEADS_URL,
        headers=_headers("wrong-secret"),
        json={"customer_name": "王先生", "phones": ["13896676678"]},
    )

    from app.api.routes import feiyu

    monkeypatch.setattr(feiyu, "get_settings", lambda: SimpleNamespace(feiyu_forward_secret=None))
    unconfigured = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "王先生", "phones": ["13896676678"]},
    )

    assert [missing.status_code, wrong.status_code, unconfigured.status_code] == [401, 401, 401]
    assert all(response.json()["code"] == "FEIYU_FORWARD_UNAUTHORIZED" for response in [missing, wrong, unconfigured])
    assert _lead_count() == 0


def test_duplicate_phone_commits_native_duplicate_event_without_changing_original_owner():
    _create_sales("张伟", "李娜")
    original = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "王先生", "phones": ["13896676678"], "custom_fields": {"feiyu": {"id": "dup-original"}}},
    )
    assert original.status_code == 200
    original_data = original.json()["data"]
    original_sales_id = original_data["sales_id"]
    original_count = _lead_count()

    duplicate = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "王先生二次提交", "phones": ["13896676678"], "custom_fields": {"feiyu": {"id": "dup-new"}}},
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "LEAD_PHONE_DUPLICATED"
    assert duplicate.json()["data"]["created"] is False
    assert duplicate.json()["data"]["duplicate_lead"]["sales_id"] == original_sales_id
    assert _lead_count() == original_count
    with SessionLocal() as db:
        lead = db.get(Lead, original_data["id"])
        duplicate_events = db.scalar(select(func.count()).select_from(LeadDuplicateEvent))
        assert lead is not None
        assert lead.sales_id == original_sales_id
        assert lead.duplicate_count == 1
        assert duplicate_events == 1


def test_new_leads_keep_native_round_robin_distribution():
    sales_ids = _create_sales("张伟", "李娜")
    assigned_ids = []
    for index, phone in enumerate(["13896676678", "13896676679"]):
        response = client.post(
            FEIYU_LEADS_URL,
            headers=_headers(),
            json={"customer_name": f"客户{index + 1}", "phones": [phone], "custom_fields": {"feiyu": {"id": f"round-robin-{index}"}}},
        )
        assert response.status_code == 200
        assigned_ids.append(response.json()["data"]["sales_id"])

    assert assigned_ids == sales_ids


def test_native_schema_defaults_optional_fields_and_rejects_missing_phone_or_invalid_name():
    accepted = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "王先生", "phones": ["13896676678"], "custom_fields": {"feiyu": {"id": "optional-fields"}}},
    )
    missing_phone = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "缺少手机号"},
    )
    invalid_name = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "   ", "phones": ["13896676679"]},
    )

    assert accepted.status_code == 200
    assert missing_phone.status_code == 400
    assert invalid_name.status_code == 400
    assert _lead_count() == 1
    with SessionLocal() as db:
        lead = db.get(Lead, accepted.json()["data"]["id"])
        contacts = list(db.scalars(select(LeadContact).where(LeadContact.lead_id == lead.id)))
        assert lead is not None
        assert lead.custom_fields["feiyu"] == {"id": "optional-fields"}
        assert {contact.contact_type for contact in contacts} == {"phone"}


def test_feiyu_contract_rejects_unknown_fields():
    response = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={
            "customer_name": "王先生",
            "phones": ["13896676678"],
            "internal_actor_id": "forged",
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert _lead_count() == 0


def test_feiyu_rejects_external_internal_remark_code_before_writes():
    for index in range(2):
        response = client.post(
            FEIYU_LEADS_URL,
            headers=_headers(),
            json={
                "customer_name": f"外部线索{index}",
                "phones": [f"1389667699{index}"],
                "custom_fields": {"remark_code": "SAME-CODE"},
            },
        )
        assert response.status_code == 400
        assert response.json()["code"] == "FEIYU_RESERVED_FIELD"
    assert _lead_count() == 0


def test_feiyu_rejects_account_id_outside_server_allowlist():
    response = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(account_id="unconfigured-account"),
        json={"customer_name": "未授权账号", "phones": ["13896676998"]},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FEIYU_ACCOUNT_ID_UNAUTHORIZED"
    assert _lead_count() == 0


@pytest.mark.parametrize(
    ("allowed_ids", "request_account_id", "expected_status"),
    [
        ("acct-a", None, 403),
        ("acct-a", "default", 403),
        ("acct-a", "acct-a", 200),
        ("default,acct-a", None, 200),
        ("default,acct-a", "default", 200),
    ],
)
def test_feiyu_default_account_must_be_in_allowlist(
    monkeypatch, allowed_ids, request_account_id, expected_status
):
    from app.api.routes import feiyu

    monkeypatch.setattr(
        feiyu,
        "get_settings",
        lambda: SimpleNamespace(
            feiyu_forward_secret=TEST_SECRET,
            feiyu_account_id="default",
            feiyu_account_ids=allowed_ids,
        ),
    )
    response = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(account_id=request_account_id),
        json={"customer_name": "允许列表回归", "phones": ["13896676997"], "custom_fields": {"feiyu": {"id": "allowlist-case"}}},
    )

    assert response.status_code == expected_status
    assert _lead_count() == (1 if expected_status == 200 else 0)
    if expected_status == 403:
        assert response.json()["code"] == "FEIYU_ACCOUNT_ID_UNAUTHORIZED"


def test_endpoint_calls_native_create_lead(monkeypatch):
    calls = []
    native_create = lead_service.create_lead

    def observe_create(db, payload, actor, **kwargs):
        calls.append((payload.customer_name, str(actor.operator_id), actor.operator_name))
        return native_create(db, payload, actor, **kwargs)

    monkeypatch.setattr(lead_service, "create_lead", observe_create)
    response = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "王先生", "phones": ["13896676678"], "custom_fields": {"feiyu": {"id": "native-create"}}},
    )

    assert response.status_code == 200
    assert calls == [("王先生", "00000000-0000-0000-0000-00000000f001", "Feiyu integration")]


def test_manual_then_feiyu_duplicate_keeps_original_lead_and_assignment():
    sales_ids = _create_sales("张伟", "李娜")
    manual = _manual_create("手工线索", "13896676678")
    assert manual.status_code == 200
    original = manual.json()["data"]
    cursor_before_duplicate = _round_robin_cursor()

    duplicate = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "飞鱼重复线索", "phones": ["13896676678"], "custom_fields": {"feiyu": {"id": "manual-then-feiyu"}}},
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "LEAD_PHONE_DUPLICATED"
    assert duplicate.json()["data"]["duplicate_lead"]["id"] == original["id"]
    assert duplicate.json()["data"]["duplicate_lead"]["sales_id"] == original["sales_id"] == sales_ids[0]
    assert _lead_count() == 1
    assert _round_robin_cursor() == cursor_before_duplicate
    with SessionLocal() as db:
        lead = db.get(Lead, original["id"])
        assert lead is not None
        assert lead.source_type == "manual"
        assert lead.duplicate_count == 1
        assert db.scalar(select(func.count()).select_from(LeadDuplicateEvent)) == 1


def test_feiyu_then_manual_duplicate_keeps_original_lead_and_assignment():
    sales_ids = _create_sales("张伟", "李娜")
    feiyu = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "飞鱼线索", "phones": ["13896676678"], "custom_fields": {"feiyu": {"id": "feiyu-then-manual"}}},
    )
    assert feiyu.status_code == 200
    original = feiyu.json()["data"]
    cursor_before_duplicate = _round_robin_cursor()

    duplicate = _manual_create("手工重复线索", "13896676678")

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "LEAD_PHONE_DUPLICATED"
    assert duplicate.json()["data"]["duplicate_lead"]["id"] == original["id"]
    assert duplicate.json()["data"]["duplicate_lead"]["sales_id"] == original["sales_id"] == sales_ids[0]
    assert _lead_count() == 1
    assert _round_robin_cursor() == cursor_before_duplicate
    with SessionLocal() as db:
        lead = db.get(Lead, original["id"])
        assert lead is not None
        assert lead.source_type == "feiyu"
        assert lead.duplicate_count == 1
        assert db.scalar(select(func.count()).select_from(LeadDuplicateEvent)) == 1


def test_interleaved_manual_and_feiyu_creates_share_round_robin_and_duplicates_do_not_advance_it():
    sales_ids = _create_sales("张伟", "李娜")
    first_manual = _manual_create("手工一", "13896676671")
    assert first_manual.status_code == 200
    assert first_manual.json()["data"]["sales_id"] == sales_ids[0]
    cursor_after_manual = _round_robin_cursor()
    assert cursor_after_manual == sales_ids[1]

    first_feiyu = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "飞鱼一", "phones": ["13896676672"], "custom_fields": {"feiyu": {"id": "interleave-1"}}},
    )
    assert first_feiyu.status_code == 200
    assert first_feiyu.json()["data"]["sales_id"] == sales_ids[1]
    cursor_after_feiyu = _round_robin_cursor()
    assert cursor_after_feiyu == sales_ids[0]

    repeated = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "飞鱼重复手工一", "phones": ["13896676671"], "custom_fields": {"feiyu": {"id": "interleave-duplicate"}}},
    )
    assert repeated.status_code == 409
    assert _round_robin_cursor() == cursor_after_feiyu

    second_manual = _manual_create("手工二", "13896676673")
    assert second_manual.status_code == 200
    assert second_manual.json()["data"]["sales_id"] == sales_ids[0]
    second_feiyu = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "飞鱼二", "phones": ["13896676674"], "custom_fields": {"feiyu": {"id": "interleave-2"}}},
    )
    assert second_feiyu.status_code == 200
    assert second_feiyu.json()["data"]["sales_id"] == sales_ids[1]

    with SessionLocal() as db:
        assignments = list(
            db.scalars(select(LeadAssignment).order_by(LeadAssignment.created_at.asc()))
        )
        assert len(assignments) == 4
        assert [item.to_sales_id for item in assignments] == [
            sales_ids[0], sales_ids[1], sales_ids[0], sales_ids[1]
        ]
        assert db.scalar(select(func.count()).select_from(LeadDuplicateEvent)) == 1
    assert _lead_count() == 4


def test_feiyu_create_generates_native_add_friend_task():
    sales_ids = _create_sales("张伟")
    response = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "飞鱼线索", "phones": ["13896676678"], "custom_fields": {"feiyu": {"id": "task-create"}}},
    )

    assert response.status_code == 200
    lead_id = response.json()["data"]["id"]
    with SessionLocal() as db:
        lead = db.get(Lead, lead_id)
        task = db.scalar(
            select(Task).where(Task.lead_id == lead_id, Task.task_type == TaskType.add_friend.value)
        )
        assert lead is not None
        assert task is not None
        assert task.sales_id == sales_ids[0]
        assert task.status == TaskStatus.blocked.value
        assert task.block_code == TaskBlockCode.SALES_WORKER_NOT_BOUND.value


def test_same_feiyu_clue_replayed_ten_times_reuses_committed_ack_and_side_effects():
    sales_ids = _create_sales("张伟")
    headers = _headers(account_id="account-a")
    payload = {
        "customer_name": "飞鱼客户",
        "phones": ["13896676881"],
        "remark": "预算：10-15万；意向：SUV",
        "custom_fields": {"feiyu": {"id": "clue-retry-1", "adv_id": "adv-1"}},
    }

    first = client.post(FEIYU_LEADS_URL, headers=headers, json=payload)
    assert first.status_code == 200
    # Treat the first committed response as lost; subsequent receiver retries
    # must get the same acknowledgement without entering native create again.
    for _ in range(9):
        replay = client.post(FEIYU_LEADS_URL, headers=headers, json=payload)
        assert replay.status_code == 200
        assert replay.content == first.content

    lead_id = first.json()["data"]["id"]
    with SessionLocal() as db:
        lead = db.get(Lead, lead_id)
        assert lead is not None
        assert lead.source_type == "feiyu"
        assert lead.source_name_snapshot == "飞鱼线索"
        assert lead.duplicate_count == 0
        assert db.scalar(select(func.count()).select_from(FeiyuLeadReceipt)) == 1
        assert db.scalar(select(func.count()).select_from(LeadDuplicateEvent)) == 0
        assert db.scalar(select(func.count()).select_from(LeadNote).where(LeadNote.lead_id == lead_id)) == 1
        assert db.scalar(select(func.count()).select_from(Task).where(Task.lead_id == lead_id)) == 1
        assert db.scalar(select(func.count()).select_from(LeadAssignment).where(LeadAssignment.lead_id == lead_id)) == 1
        assert lead.sales_id == sales_ids[0]
        assert "10-15万" in db.scalar(select(LeadNote.content).where(LeadNote.lead_id == lead_id))


def test_feiyu_clue_id_is_required_before_native_create_or_duplicate_processing():
    _create_sales("张伟")
    payload = {"customer_name": "缺编号线索", "phones": ["13896676890"]}

    responses = [
        client.post(FEIYU_LEADS_URL, headers=_headers(), json=payload)
        for _ in range(10)
    ]

    assert all(response.status_code == 400 for response in responses)
    assert all(response.json()["code"] == "FEIYU_CLUE_ID_REQUIRED" for response in responses)
    assert _lead_count() == 0
    assert _round_robin_cursor() is None
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(FeiyuLeadReceipt)) == 0
        assert db.scalar(select(func.count()).select_from(LeadDuplicateEvent)) == 0
        assert db.scalar(select(func.count()).select_from(LeadNote)) == 0
        assert db.scalar(select(func.count()).select_from(LeadAssignment)) == 0
        assert db.scalar(select(func.count()).select_from(Task)) == 0


def test_same_feiyu_clue_with_changed_details_returns_manual_review_without_mutation():
    sales_ids = _create_sales("张伟")
    headers = _headers(account_id="account-a")
    original_payload = {
        "customer_name": "飞鱼客户",
        "phones": ["13896676891"],
        "remark": "预算：10万",
        "custom_fields": {
            "feiyu": {
                "id": "clue-details-change",
                "business": [{"field_name": "座位数", "value": "五座"}],
            }
        },
    }
    first = client.post(FEIYU_LEADS_URL, headers=headers, json=original_payload)
    assert first.status_code == 200
    lead_id = first.json()["data"]["id"]
    cursor_after_create = _round_robin_cursor()

    changed_payload = {
        **original_payload,
        "remark": "预算：20万；七座",
        "custom_fields": {
            "feiyu": {
                "id": "clue-details-change",
                "business": [{"field_name": "座位数", "value": "七座"}],
            }
        },
    }
    changed = client.post(FEIYU_LEADS_URL, headers=headers, json=changed_payload)
    replay = client.post(FEIYU_LEADS_URL, headers=headers, json=changed_payload)

    for response in (changed, replay):
        assert response.status_code == 409
        assert response.json()["code"] == "FEIYU_CLUE_UPDATE_REQUIRES_REVIEW"
        assert response.json()["data"]["lead_id"] == lead_id
        assert response.json()["data"]["update_applied"] is False
        assert response.json()["data"]["manual_review_required"] is True
        assert "未应用" in response.json()["message"]
    assert _round_robin_cursor() == cursor_after_create
    with SessionLocal() as db:
        lead = db.get(Lead, lead_id)
        notes = list(db.scalars(select(LeadNote).where(LeadNote.lead_id == lead_id)))
        assert lead is not None
        assert lead.sales_id == sales_ids[0]
        assert lead.duplicate_count == 0
        assert len(notes) == 1
        assert "预算：10万" in notes[0].content
        assert "预算：20万" not in notes[0].content
        assert db.scalar(select(func.count()).select_from(FeiyuLeadReceipt)) == 1
        assert db.scalar(select(func.count()).select_from(LeadDuplicateEvent)) == 0
        assert db.scalar(select(func.count()).select_from(LeadAssignment).where(LeadAssignment.lead_id == lead_id)) == 1
        assert db.scalar(select(func.count()).select_from(Task).where(Task.lead_id == lead_id)) == 1


def test_feiyu_clue_id_alias_replays_the_same_canonical_identity():
    _create_sales("张伟")
    headers = _headers(account_id="account-a")
    first = client.post(
        FEIYU_LEADS_URL,
        headers=headers,
        json={
            "customer_name": "编号别名客户",
            "phones": ["13896676892"],
            "remark": "预算：15万",
            "custom_fields": {"feiyu": {"id": "alias-clue-1", "adv_id": "adv-1"}},
        },
    )
    replay = client.post(
        FEIYU_LEADS_URL,
        headers=headers,
        json={
            "customer_name": "编号别名客户",
            "phones": ["13896676892"],
            "remark": "预算：15万",
            "custom_fields": {"feiyu": {"clue_id": "alias-clue-1", "adv_id": "adv-1"}},
        },
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.content == first.content
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(FeiyuLeadReceipt)) == 1
        assert db.scalar(select(func.count()).select_from(Lead)) == 1
        assert db.scalar(select(func.count()).select_from(LeadNote)) == 1
        assert db.scalar(select(func.count()).select_from(Task)) == 1


def test_same_feiyu_identity_with_changed_contact_is_blocked_without_business_writes():
    _create_sales("张伟")
    headers = _headers(account_id="account-a")
    first = client.post(
        FEIYU_LEADS_URL,
        headers=headers,
        json={
            "customer_name": "原始客户",
            "phones": ["13896676882"],
            "custom_fields": {"feiyu": {"id": "clue-conflict-1"}},
        },
    )
    assert first.status_code == 200
    original_id = first.json()["data"]["id"]

    conflict = client.post(
        FEIYU_LEADS_URL,
        headers=headers,
        json={
            "customer_name": "被替换客户",
            "phones": ["13896676883"],
            "remark": "不得追加",
            "custom_fields": {"feiyu": {"id": "clue-conflict-1"}},
        },
    )

    assert conflict.status_code == 409
    assert conflict.json()["code"] == "FEIYU_CLUE_IDENTITY_CONFLICT"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Lead)) == 1
        assert db.scalar(select(func.count()).select_from(FeiyuLeadReceipt)) == 1
        assert db.scalar(select(func.count()).select_from(LeadDuplicateEvent)) == 0
        assert db.scalar(select(func.count()).select_from(LeadNote).where(LeadNote.lead_id == original_id)) == 0
        assert db.scalar(select(func.count()).select_from(Task).where(Task.lead_id == original_id)) == 1


def test_new_feiyu_clue_for_existing_customer_records_one_repeat_and_note_then_replays():
    _create_sales("张伟")
    headers = _headers(account_id="account-a")
    original = client.post(
        FEIYU_LEADS_URL,
        headers=headers,
        json={
            "customer_name": "首次飞鱼客户",
            "phones": ["13896676884"],
            "remark": "首次预算：8万",
            "custom_fields": {"feiyu": {"id": "clue-first"}},
        },
    )
    assert original.status_code == 200
    lead_id = original.json()["data"]["id"]
    repeat_payload = {
        "customer_name": "再次留资客户",
        "phones": ["13896676884"],
        "remark": "本次意向：轿车",
        "custom_fields": {"feiyu": {"id": "clue-second", "adv_id": "adv-current"}},
    }
    repeat = client.post(FEIYU_LEADS_URL, headers=headers, json=repeat_payload)
    assert repeat.status_code == 409
    assert repeat.json()["code"] == "LEAD_PHONE_DUPLICATED"
    replay = client.post(FEIYU_LEADS_URL, headers=headers, json=repeat_payload)
    assert replay.status_code == 409
    assert replay.content == repeat.content

    with SessionLocal() as db:
        lead = db.get(Lead, lead_id)
        events = list(db.scalars(select(LeadDuplicateEvent).where(LeadDuplicateEvent.lead_id == lead_id)))
        notes = list(db.scalars(select(LeadNote).where(LeadNote.lead_id == lead_id).order_by(LeadNote.created_at)))
        assert lead is not None
        assert lead.source_type == "feiyu"
        assert lead.source_name_snapshot == "飞鱼线索"
        assert lead.duplicate_count == 1
        assert len(events) == 1
        assert events[0].submitted_payload["custom_fields"]["feiyu"]["id"] == "clue-second"
        assert len(notes) == 2
        assert "首次预算：8万" in notes[0].content
        assert "本次意向：轿车" in notes[1].content
        assert db.scalar(select(func.count()).select_from(FeiyuLeadReceipt)) == 2
        assert db.scalar(select(func.count()).select_from(Task).where(Task.lead_id == lead_id)) == 1


def test_same_clue_id_is_scoped_by_configured_account():
    _create_sales("张伟")
    payload = {
        "customer_name": "同编号线索",
        "phones": ["13896676885"],
        "custom_fields": {"feiyu": {"id": "shared-clue-id"}},
    }
    first = client.post(FEIYU_LEADS_URL, headers=_headers(account_id="account-a"), json=payload)
    second = client.post(FEIYU_LEADS_URL, headers=_headers(account_id="account-b"), json=payload)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["code"] == "LEAD_PHONE_DUPLICATED"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(FeiyuLeadReceipt)) == 2
        assert db.scalar(select(func.count()).select_from(LeadDuplicateEvent)) == 1


def test_feiyu_lead_without_sales_can_be_natively_assigned_later():
    created = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "先入池线索", "phones": ["13896676678"], "custom_fields": {"feiyu": {"id": "unassigned-retry"}}},
    )
    assert created.status_code == 200
    data = created.json()["data"]
    assert data["status"] == "unassigned"
    assert data["assign_status"] == "assign_failed"
    assert data["assignment"]["status"] == "failed"
    lead_id = data["id"]

    sales_ids = _create_sales("张伟")
    retried = client.post("/api/leads/retry-auto-assign", json={"lead_ids": [lead_id]})

    assert retried.status_code == 200
    assert retried.json()["data"]["succeeded"] == 1
    assert retried.json()["data"]["items"][0]["sales_id"] == sales_ids[0]
    with SessionLocal() as db:
        lead = db.get(Lead, lead_id)
        assert lead is not None
        assert lead.status == "assigned"
        assert lead.assign_status == "assigned"


def test_feiyu_ack_is_compact_when_full_native_result_has_many_cross_channel_suggestions(monkeypatch):
    _create_sales("张伟")
    shared_wechat = "same-wechat-contact"
    shared_email = "same-contact@example.test"
    for index in range(10):
        response = client.post(
            "/api/leads",
            json={
                "customer_name": ("客户" * 22) + f"{index:02d}",
                "phones": [f"138966700{index:02d}"],
                "wechats": [shared_wechat],
                "emails": [shared_email],
            },
        )
        assert response.status_code == 200

    original_create = lead_service.create_lead
    full_results = []

    def capture_native_result(db, payload, actor, **kwargs):
        result = original_create(db, payload, actor, **kwargs)
        full_results.append(result)
        return result

    monkeypatch.setattr(lead_service, "create_lead", capture_native_result)
    response = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={
            "customer_name": "最终飞鱼线索",
            "phones": ["13896679999"],
            "wechats": [shared_wechat],
            "emails": [shared_email],
            "custom_fields": {"feiyu": {"id": "compact-ack"}},
        },
    )

    assert response.status_code == 200
    assert len(full_results) == 1
    assert len(full_results[0]["potential_duplicates"]["wechat"]) == 10
    assert len(full_results[0]["potential_duplicates"]["email"]) == 10
    assert len(json.dumps(full_results[0], ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 4096
    assert len(response.content) < 4096
    body = response.json()
    assert body["code"] == "OK"
    assert body["data"]["created"] is True
    assert body["data"]["status"] == "assigned"
    assert body["data"]["assign_status"] == "assigned"
    assert body["data"]["assignment"]["status"] == "succeeded"
    assert "potential_duplicates" not in body["data"]


def test_create_lead_acquires_shared_creation_lock_before_phone_dedup(monkeypatch):
    order = []
    original_find = lead_service._find_existing_active_by_phone_hashes

    def record_find(*args, **kwargs):
        order.append("dedup")
        return original_find(*args, **kwargs)

    monkeypatch.setattr(lead_service, "_lock_lead_creation", lambda _db: order.append("lock"))
    monkeypatch.setattr(lead_service, "_find_existing_active_by_phone_hashes", record_find)

    response = _manual_create("顺序核验", "13896676678")

    assert response.status_code == 200
    assert order[:2] == ["lock", "dedup"]


def test_postgresql_create_lock_uses_stable_transaction_advisory_key():
    calls = []
    db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        execute=lambda statement, params: calls.append((str(statement), params)),
    )

    lead_service._lock_lead_creation(db)

    assert calls == [
        (
            "SELECT pg_advisory_xact_lock(:namespace, :lock_key)",
            {"namespace": 1128811086, "lock_key": 1},
        )
    ]


def test_non_postgresql_create_lock_is_a_noop():
    calls = []
    db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
        execute=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    lead_service._lock_lead_creation(db)

    assert calls == []


def _postgresql_actor():
    from uuid import UUID

    from app.core.request_context import ActorContext

    return ActorContext(
        operator_id=UUID("00000000-0000-0000-0000-000000000001"),
        operator_name="PostgreSQL concurrency test",
        role="authenticated",
        ip_address=None,
        user_agent=None,
        request_id="postgresql-concurrency-test",
    )


def _postgresql_payload(name: str, *phones: str):
    from app.schemas.lead import LeadCreate

    return LeadCreate(customer_name=name, phones=list(phones))


def _postgresql_concurrent_create(payload, state):
    from sqlalchemy import text

    from app.errors import DuplicateLeadError

    with SessionLocal() as db:
        state["pid"] = db.scalar(text("SELECT pg_backend_pid()"))
        state["started"].set()
        try:
            data = lead_service.create_lead(db, payload, _postgresql_actor())
            db.commit()
            return {"kind": "created", "data": data}
        except DuplicateLeadError as exc:
            # The Feiyu and native lead routes commit duplicate audit events.
            db.commit()
            return {"kind": "duplicate", "data": exc.data}
        except Exception:
            db.rollback()
            raise


def _postgresql_waiting_on_creation_lock(pid: int, timeout_seconds: float = 5.0) -> bool:
    from time import monotonic, sleep

    from sqlalchemy import text

    deadline = monotonic() + timeout_seconds
    statement = text(
        "SELECT wait_event_type, query FROM pg_stat_activity WHERE pid = :pid"
    )
    with SessionLocal() as db:
        while monotonic() < deadline:
            row = db.execute(statement, {"pid": pid}).one_or_none()
            if (
                row
                and row.wait_event_type == "Lock"
                and "pg_advisory_xact_lock" in row.query.lower()
            ):
                return True
            sleep(0.025)
    return False


def _postgresql_finish_contender(owner, payload, *, commit_owner: bool):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    state = {"started": Event()}
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_postgresql_concurrent_create, payload, state)
    try:
        assert state["started"].wait(5), "contending connection did not start"
        waited = _postgresql_waiting_on_creation_lock(state["pid"])
        if commit_owner:
            owner.commit()
        else:
            owner.rollback()
        outcome = future.result(timeout=10)
        assert waited, "contending connection never waited on pg_advisory_xact_lock"
        return outcome
    finally:
        if owner.in_transaction():
            owner.rollback()
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="requires independent PostgreSQL connections; SQLite is not concurrency evidence",
)
def test_postgresql_create_lock_reads_committed_state_after_wait():

    with engine.connect() as connection:
        isolation = connection.exec_driver_sql("SHOW transaction_isolation").scalar_one()
    assert isolation.lower() == "read committed"

    payload = _postgresql_payload("同号并发先到", "13896679991")
    owner = SessionLocal()
    try:
        original = lead_service.create_lead(owner, payload, _postgresql_actor())
        outcome = _postgresql_finish_contender(owner, payload, commit_owner=True)
    finally:
        owner.close()

    assert outcome["kind"] == "duplicate"
    assert outcome["data"]["duplicate_lead"]["id"] == original["id"]
    assert _lead_count() == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(LeadDuplicateEvent)) == 1


@pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="requires independent PostgreSQL connections; SQLite is not concurrency evidence",
)
def test_postgresql_create_lock_serializes_overlapping_multi_phone_payloads():
    owner = SessionLocal()
    try:
        original = lead_service.create_lead(
            owner,
            _postgresql_payload("多手机号先到", "13896679992", "13896679993"),
            _postgresql_actor(),
        )
        outcome = _postgresql_finish_contender(
            owner,
            _postgresql_payload("共享手机号后来", "13896679993", "13896679994"),
            commit_owner=True,
        )
    finally:
        owner.close()

    assert outcome["kind"] == "duplicate"
    assert outcome["data"]["duplicate_lead"]["id"] == original["id"]
    assert _lead_count() == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(LeadDuplicateEvent)) == 1


@pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="requires independent PostgreSQL connections; SQLite is not concurrency evidence",
)
def test_postgresql_create_lock_is_released_when_owner_transaction_rolls_back():
    payload = _postgresql_payload("回滚后重建", "13896679995")
    owner = SessionLocal()
    try:
        original = lead_service.create_lead(owner, payload, _postgresql_actor())
        outcome = _postgresql_finish_contender(owner, payload, commit_owner=False)
    finally:
        owner.close()

    assert outcome["kind"] == "created"
    assert outcome["data"]["id"] != original["id"]
    assert _lead_count() == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(LeadDuplicateEvent)) == 0
