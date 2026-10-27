from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.database import Base, SessionLocal, engine
from app.main import app
from app.models.lead import Lead, LeadContact, LeadDuplicateEvent
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
        lambda: SimpleNamespace(feiyu_forward_secret=TEST_SECRET),
    )


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _headers(secret: str = TEST_SECRET) -> dict[str, str]:
    return {"X-Feiyu-Forward-Secret": secret}


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
        json={"customer_name": "王先生", "phones": ["13896676678"]},
    )
    assert original.status_code == 200
    original_data = original.json()["data"]
    original_sales_id = original_data["sales_id"]
    original_count = _lead_count()

    duplicate = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "王先生二次提交", "phones": ["13896676678"]},
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
            json={"customer_name": f"客户{index + 1}", "phones": [phone]},
        )
        assert response.status_code == 200
        assigned_ids.append(response.json()["data"]["sales_id"])

    assert assigned_ids == sales_ids


def test_native_schema_defaults_optional_fields_and_rejects_missing_phone_or_invalid_name():
    accepted = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "王先生", "phones": ["13896676678"]},
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
        assert lead.custom_fields == {}
        assert {contact.contact_type for contact in contacts} == {"phone"}


def test_endpoint_calls_native_create_lead(monkeypatch):
    calls = []
    native_create = lead_service.create_lead

    def observe_create(db, payload, actor):
        calls.append((payload.customer_name, str(actor.operator_id), actor.operator_name))
        return native_create(db, payload, actor)

    monkeypatch.setattr(lead_service, "create_lead", observe_create)
    response = client.post(
        FEIYU_LEADS_URL,
        headers=_headers(),
        json={"customer_name": "王先生", "phones": ["13896676678"]},
    )

    assert response.status_code == 200
    assert calls == [("王先生", "00000000-0000-0000-0000-00000000f001", "Feiyu integration")]
