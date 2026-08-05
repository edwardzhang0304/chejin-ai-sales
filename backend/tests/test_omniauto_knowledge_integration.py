from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


OMNIAUTO_ROOT = Path(__file__).resolve().parents[2] / "worker-client" / "omniauto-rpa"
for path in (
    OMNIAUTO_ROOT,
    OMNIAUTO_ROOT / "apps" / "wechat_ai_customer_service",
    OMNIAUTO_ROOT / "apps" / "wechat_ai_customer_service" / "workflows",
    OMNIAUTO_ROOT / "apps" / "wechat_ai_customer_service" / "adapters",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from apps.wechat_ai_customer_service.workflows import customer_service_brain  # noqa: E402
from apps.wechat_ai_customer_service import product_master  # noqa: E402
from apps.wechat_ai_customer_service.workflows import knowledge_runtime  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.vehicle import KnowledgeItem  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402


client = TestClient(app)
ADMIN_HEADERS = {
    "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
    "X-Operator-Name": "Vehicle Brain Acceptance",
    "X-Operator-Role": "admin",
}
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c020000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082"
)


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


class _BackendKnowledgeStore:
    """Read the exact Product Master rows written by the backend test database."""

    def list_knowledge_items(
        self,
        tenant_id,
        *,
        layer=None,
        category_id=None,
        include_archived=False,
        **_kwargs,
    ):
        with SessionLocal() as db:
            query = select(KnowledgeItem).where(KnowledgeItem.tenant_id == tenant_id)
            if layer:
                query = query.where(KnowledgeItem.layer == layer)
            if category_id:
                query = query.where(KnowledgeItem.category_id == category_id)
            if not include_archived:
                query = query.where(KnowledgeItem.status == "active")
            return [dict(item.payload) for item in db.scalars(query).all()]

    def get_knowledge_item(self, tenant_id, *, layer, category_id, item_id):
        items = self.list_knowledge_items(
            tenant_id,
            layer=layer,
            category_id=category_id,
            include_archived=True,
        )
        return next((item for item in items if str(item.get("id") or "") == item_id), None)


def test_knowledge_runtime_failure_handoffs_without_calling_model(monkeypatch):
    monkeypatch.setattr(
        customer_service_brain,
        "build_reply_evidence_pack",
        lambda **_kwargs: {"knowledge_error": "database unavailable"},
    )
    monkeypatch.setattr(
        customer_service_brain,
        "run_brain_llm",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("model must not run")),
    )

    result = customer_service_brain.maybe_run_customer_service_brain(
        config={
            "customer_service_brain": {
                "enabled": True,
                "mode": "brain_first",
                "fallback_to_legacy_on_error": False,
            }
        },
        target_name="customer",
        target_state={},
        batch=[{"message_type": "text", "content": "现在有哪些车？"}],
        combined="现在有哪些车？",
        decision={},
        reply_text="",
        intent_assist={},
        rag_reply={},
        llm_reply={},
        product_knowledge={},
        data_capture={},
        raw_capture={},
        customer_profile={},
    )

    assert result["rule_name"] == "customer_service_brain_handoff"
    assert result["reason"] == "KNOWLEDGE_RUNTIME_UNAVAILABLE"
    assert result["reply_text"] == ""
    assert result["needs_handoff"] is True


class _EmptyPostgresStore:
    def list_knowledge_items(self, *_args, **_kwargs):
        return []


def test_postgres_product_master_does_not_resurrect_json_items(monkeypatch, tmp_path):
    store = product_master.ProductMasterStore(root=tmp_path)
    store.ensure_structure()
    (store.items_dir / "retired-test-item.json").write_text(
        '{"id":"retired-test-item","status":"active","data":{"name":"must not load"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(product_master, "postgres_store", lambda _tenant_id: _EmptyPostgresStore())

    assert store.list_items() == []


def test_postgres_formal_knowledge_empty_category_does_not_fallback_to_json(monkeypatch):
    runtime = knowledge_runtime.KnowledgeRuntime()
    monkeypatch.setattr(knowledge_runtime, "postgres_store", lambda _tenant_id: _EmptyPostgresStore())

    assert runtime.list_items("policies") == []


def test_postgres_outage_is_explicit_instead_of_falling_back_to_json(monkeypatch):
    missing = SimpleNamespace(use_postgres=True, postgres_configured=False)
    monkeypatch.setattr(product_master, "load_storage_config", lambda: missing)
    monkeypatch.setattr(knowledge_runtime, "load_storage_config", lambda: missing)

    with pytest.raises(RuntimeError, match="Product Master PostgreSQL DSN"):
        product_master.postgres_store("chejin")
    with pytest.raises(RuntimeError, match="KnowledgeRuntime PostgreSQL DSN"):
        knowledge_runtime.postgres_store("chejin")


def test_listed_vehicle_reaches_brain_reply_and_internal_fields_never_leave_product_master(monkeypatch):
    created = client.post(
        "/api/vehicles",
        json={
            "display_name": "2024款星河通勤车",
            "brand": "星河",
            "series": "通勤系列",
            "public_price": 10.88,
            "first_registration": "2024-03",
            "mileage_km": 8600,
            "customer_description": "适合城市通勤，车身灵活。",
            "vin": "LSECRET1234567890",
            "plate_number": "苏A·SECRET",
            "purchase_price": 7.66,
            "internal_notes": "内部底价 8.20 万，禁止对客。",
        },
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 200, created.text
    vehicle = created.json()["data"]
    code = vehicle["vehicle_code"]
    uploaded = client.post(
        f"/api/vehicles/{code}/images",
        files={"files": ("car.png", PNG_1X1, "image/png")},
        headers=ADMIN_HEADERS,
    )
    assert uploaded.status_code == 200, uploaded.text
    assert client.post(f"/api/vehicles/{code}/list", headers=ADMIN_HEADERS).status_code == 200

    monkeypatch.setenv("WECHAT_KNOWLEDGE_TENANT", "chejin")
    store = _BackendKnowledgeStore()
    monkeypatch.setattr(product_master, "postgres_store", lambda _tenant_id: store)
    monkeypatch.setattr(knowledge_runtime, "postgres_store", lambda _tenant_id: store)

    plan = {
        "can_answer": True,
        "understanding": {
            "user_intent": "询问指定车辆",
            "normalized_entities": [{"raw": "星河通勤车", "normalized": "2024款星河通勤车", "entity_type": "product"}],
        },
        "answer_mode": "quote_product_fact",
        "reply_strategy": {"style": "concise_human"},
        "evidence_used": {"product_ids": [code]},
        "facts_claimed": [
            {"fact_type": "price", "value": "10.88万", "source_level": "product_master", "source_id": code},
        ],
        "reply_segments": ["2024款星河通勤车在售，公开售价10.88万。", "它适合城市通勤，表显里程8600公里。"],
        "risk": {"risk_level": "low", "risk_tags": [], "needs_handoff": False},
        "recommended_action": "send_reply",
        "confidence": 0.92,
        "reason": "Product Master 命中已上架车辆。",
    }
    config = {
        "customer_service_brain": {
            "enabled": True,
            "mode": "brain_first",
            "provider": "manual_json",
            "brain_plan": plan,
            "min_confidence": 0.2,
            "require_evidence": True,
            "include_evidence_pack_in_audit": True,
            "include_brain_input_in_audit": True,
            "fallback_to_legacy_on_error": False,
        },
        "llm_reply_synthesis": {
            "enabled": True,
            "provider": "manual_json",
            "min_confidence": 0.2,
            "require_evidence": True,
        },
        "raw_message_store": {"enabled": False},
        "final_visible_llm_polish": {"enabled": False},
    }
    call = {
        "config": config,
        "target_name": "客户",
        "target_state": {"conversation_context": {}},
        "batch": [{"id": "message-vehicle-001", "sender": "客户", "message_type": "text", "content": "你们有没有星河通勤车？"}],
        "combined": "你们有没有星河通勤车？",
        "decision": {},
        "reply_text": "",
        "intent_assist": {},
        "rag_reply": {},
        "llm_reply": {},
        "product_knowledge": {},
        "data_capture": {},
        "raw_capture": {"conversation": {"conversation_id": "vehicle-brain-001", "chat_type": "private"}},
        "customer_profile": None,
    }

    answer = customer_service_brain.maybe_run_customer_service_brain(**call)
    assert answer["adoptable"] is True, answer
    assert answer["rule_name"] == "customer_service_brain_reply"
    assert "2024款星河通勤车" in answer["reply_text"]
    assert "10.88万" in answer["reply_text"]
    serialized_answer = str(answer)
    for secret in ("LSECRET1234567890", "苏A·SECRET", "7.66", "内部底价"):
        assert secret not in serialized_answer

    assert client.post(f"/api/vehicles/{code}/unlist", headers=ADMIN_HEADERS).status_code == 200
    after_unlist = customer_service_brain.maybe_run_customer_service_brain(**call)
    assert after_unlist.get("adoptable") is not True or not str(after_unlist.get("reply_text") or "").strip()
    assert "10.88万" not in str(after_unlist.get("reply_text") or "")
