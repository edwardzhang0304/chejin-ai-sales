from fastapi.testclient import TestClient

from app.core.database import Base, SessionLocal, engine
from app.main import app
from app.models.lead import LeadContact
from app.services import lead_service


client = TestClient(app)
HEADERS = {
    "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
    "X-Operator-Name": "Ops Tester",
    "X-Operator-Role": "admin",
}


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_create_lead_assigns_round_robin_sales():
    client.post(
        "/api/sales",
        json={"sales_name": "张伟", "phone": "13900000001", "enabled": True, "sort_order": 10},
        headers=HEADERS,
    )

    response = client.post(
        "/api/leads",
        json={"customer_name": "王先生", "phones": ["13896676678"], "remark": "预算 10 万"},
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "assigned"
    assert data["sales_name"] == "张伟"


def test_create_lead_without_sales_records_assignment_failure_then_retry_succeeds():
    created = client.post(
        "/api/leads",
        json={"customer_name": "王先生", "phones": ["13896676678"], "remark": "暂无销售"},
        headers=HEADERS,
    )
    assert created.status_code == 200
    lead_id = created.json()["data"]["id"]
    detail = client.get(f"/api/leads/{lead_id}").json()["data"]
    assert detail["status"] == "unassigned"
    assert detail["assign_status"] == "assign_failed"
    assert detail["assign_failure_reason"] == "无可用销售"

    client.post(
        "/api/sales",
        json={"sales_name": "张伟", "phone": "13900000001", "enabled": True, "sort_order": 10},
        headers=HEADERS,
    )
    retried = client.post("/api/leads/retry-auto-assign", json={"lead_ids": [lead_id]}, headers=HEADERS)

    assert retried.status_code == 200
    data = retried.json()["data"]
    assert data["succeeded"] == 1
    assert data["items"][0]["sales_name"] == "张伟"


def test_duplicate_phone_does_not_create_new_lead_and_appends_event():
    client.post(
        "/api/leads",
        json={"customer_name": "王先生", "phones": ["13896676678"], "remark": "第一次"},
        headers=HEADERS,
    )
    response = client.post(
        "/api/leads",
        json={"customer_name": "王先生2", "phones": ["13896676678"], "remark": "第二次"},
        headers=HEADERS,
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["code"] == "LEAD_PHONE_DUPLICATED"
    assert payload["data"]["created"] is False
    assert payload["data"]["duplicate_count"] == 1
    assert isinstance(payload["data"]["duplicate_lead"]["created_at"], str)
    assert payload["trace_id"]


def test_contact_value_is_encrypted_not_base64_plaintext():
    created = client.post(
        "/api/leads",
        json={"customer_name": "王先生", "phones": ["13896676678"]},
        headers=HEADERS,
    ).json()["data"]

    with SessionLocal() as db:
        contact = db.query(LeadContact).filter(LeadContact.lead_id == created["id"]).one()
        encrypted = contact.contact_value_encrypted

    assert "13896676678" not in encrypted
    assert not encrypted.startswith("Y2hhbmdlLW1lLWluLXByb2R1Y3Rpb246")


def test_reveal_phone_writes_audit_log():
    created = client.post(
        "/api/leads",
        json={"customer_name": "王先生", "phones": ["13896676678"]},
        headers=HEADERS,
    ).json()["data"]
    lead_id = created["id"]
    detail = client.get(f"/api/leads/{lead_id}").json()["data"]
    contact_id = detail["contacts"][0]["id"]

    reveal = client.post(
        f"/api/leads/{lead_id}/contacts/{contact_id}/reveal",
        json={"reason": "电话确认到店时间"},
        headers=HEADERS,
    )
    logs = client.get("/api/operation-logs?event_type=phone_revealed").json()["data"]

    assert reveal.status_code == 200
    assert reveal.json()["data"]["value"] == "13896676678"
    assert logs["total"] == 1
    assert "13896676678" not in str(logs["items"][0]["metadata"])


def test_mark_invalid_and_restore_lead():
    created = client.post(
        "/api/leads",
        json={"customer_name": "王先生", "phones": ["13896676678"]},
        headers=HEADERS,
    ).json()["data"]
    lead_id = created["id"]

    invalid = client.post(
        f"/api/leads/{lead_id}/mark-invalid",
        json={"invalid_reason": "duplicate_or_mistaken", "invalid_remark": "重复/误录"},
        headers=HEADERS,
    )
    restored = client.post(f"/api/leads/{lead_id}/restore", headers=HEADERS)

    assert invalid.status_code == 200
    assert invalid.json()["data"]["status"] == "invalid"
    assert restored.status_code == 200
    assert restored.json()["data"]["status"] == "unassigned"


def test_export_selected_leads_writes_audit_target_id():
    created = client.post(
        "/api/leads",
        json={"customer_name": "王先生", "phones": ["13896676678"]},
        headers=HEADERS,
    ).json()["data"]

    response = client.post(
        "/api/leads/export",
        json={"lead_ids": [created["id"]], "fields": ["customer_name", "primary_phone_masked"]},
        headers=HEADERS,
    )
    logs = client.get("/api/operation-logs?event_type=leads_exported").json()["data"]

    assert response.status_code == 200
    assert "138****6678" in response.text
    assert "13896676678" not in response.text
    assert logs["total"] == 1
    assert logs["items"][0]["target_type"] == "export_task"
    assert logs["items"][0]["target_id"]
    assert logs["items"][0]["metadata"]["masked"] is True


def test_validation_error_includes_trace_id():
    response = client.post(
        "/api/leads",
        json={"customer_name": "", "phones": []},
        headers={**HEADERS, "X-Request-Id": "trace-test-001"},
    )

    payload = response.json()
    assert response.status_code == 400
    assert payload["code"] == "VALIDATION_ERROR"
    assert payload["trace_id"] == "trace-test-001"


def test_browser_operator_headers_are_ignored_in_favor_of_server_identity():
    response = client.post(
        "/api/leads",
        json={"customer_name": "王先生", "phones": ["13896676678"]},
        headers={
            **HEADERS,
            "X-Operator-Id": "not-a-uuid",
            "X-Operator-Name": "Forged Browser Admin",
            "X-Operator-Role": "admin",
        },
    )

    assert response.status_code == 200
    logs = client.get("/api/operation-logs?event_type=lead_created").json()["data"]
    assert logs["total"] == 1
    assert logs["items"][0]["operator_name"] != "Forged Browser Admin"


def test_unhandled_exception_returns_stable_error_code(monkeypatch):
    def raise_runtime_error(_db):
        raise RuntimeError("boom")

    monkeypatch.setattr(lead_service, "lead_stats", raise_runtime_error)
    local_client = TestClient(app, raise_server_exceptions=False)
    response = local_client.get("/api/leads/stats")

    payload = response.json()
    assert response.status_code == 500
    assert payload["code"] == "INTERNAL_SERVER_ERROR"
    assert payload["trace_id"]
