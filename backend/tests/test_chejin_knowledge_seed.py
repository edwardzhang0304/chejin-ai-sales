from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest
from sqlalchemy import func, select


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OMNIAUTO_ROOT = PROJECT_ROOT / "worker-client" / "omniauto-rpa"
for path in (
    OMNIAUTO_ROOT,
    OMNIAUTO_ROOT / "apps" / "wechat_ai_customer_service",
    OMNIAUTO_ROOT / "apps" / "wechat_ai_customer_service" / "workflows",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from apps.wechat_ai_customer_service.workflows import knowledge_runtime  # noqa: E402
from apps.wechat_ai_customer_service.workflows.evidence_resolver import EvidenceResolver  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.scripts import import_chejin_knowledge as seed_command  # noqa: E402
from app.models.vehicle import KnowledgeCategory, KnowledgeItem  # noqa: E402
from app.services.chejin_knowledge_seed import (  # noqa: E402
    CATEGORY_DEFINITIONS,
    SEED_ID,
    SEED_ITEMS,
    TENANT_ID,
    KnowledgeSeedConflictError,
    import_chejin_knowledge,
)


SEED_ITEM_IDS = {item.item_id for item in SEED_ITEMS}
DATA_ROOT = OMNIAUTO_ROOT / "apps" / "wechat_ai_customer_service" / "data"


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


class _BackendKnowledgeStore:
    def list_knowledge_items(
        self,
        tenant_id,
        *,
        layer=None,
        category_id=None,
        product_id=None,
        include_archived=False,
        **_kwargs,
    ):
        with SessionLocal() as db:
            query = select(KnowledgeItem).where(KnowledgeItem.tenant_id == tenant_id)
            if layer:
                query = query.where(KnowledgeItem.layer == layer)
            if category_id:
                query = query.where(KnowledgeItem.category_id == category_id)
            if product_id is not None:
                query = query.where(KnowledgeItem.product_id == product_id)
            if not include_archived:
                query = query.where(KnowledgeItem.status == "active")
            return [dict(item.payload) for item in db.scalars(query).all()]

    def get_knowledge_item(self, tenant_id, *, layer, category_id, item_id):
        with SessionLocal() as db:
            row = db.get(
                KnowledgeItem,
                {
                    "tenant_id": tenant_id,
                    "layer": layer,
                    "category_id": category_id,
                    "product_id": "",
                    "item_id": item_id,
                },
            )
            return dict(row.payload) if row else None


def _run(operation="import", *, dry_run=False):
    with SessionLocal.begin() as db:
        return import_chejin_knowledge(db, operation=operation, dry_run=dry_run)


def _resolver(monkeypatch, *, tenant_id=TENANT_ID):
    monkeypatch.setattr(knowledge_runtime, "postgres_store", lambda _tenant_id: _BackendKnowledgeStore())
    return EvidenceResolver(knowledge_runtime.KnowledgeRuntime(tenant_id=tenant_id))


def _matched_ids(result):
    return {item["item_id"] for item in result["evidence_items"]}


def test_static_category_contract_has_no_business_items():
    shared_registry = json.loads((DATA_ROOT / "shared_knowledge" / "registry.json").read_text(encoding="utf-8"))
    tenant_registry = json.loads(
        (DATA_ROOT / "tenants" / "chejin" / "knowledge_bases" / "registry.json").read_text(encoding="utf-8")
    )

    assert {item["id"] for item in shared_registry["categories"]} == {"global_guidelines", "reply_style"}
    assert {item["id"] for item in tenant_registry["categories"]} == {"policies"}
    assert tenant_registry["tenant_id"] == "chejin"
    assert not list((DATA_ROOT / "shared_knowledge").rglob("items/*.json"))
    assert not list((DATA_ROOT / "tenants" / "chejin" / "knowledge_bases").rglob("items/*.json"))
    static_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            *(DATA_ROOT / "shared_knowledge").rglob("*.json"),
            *(DATA_ROOT / "tenants" / "chejin" / "knowledge_bases").rglob("*.json"),
        ]
    )
    assert all(item_id not in static_text for item_id in SEED_ITEM_IDS)


def test_empty_database_imports_exactly_eight_items_and_three_categories():
    result = _run()

    assert result == {
        "seed_id": SEED_ID,
        "seed_version": 1,
        "tenant_id": TENANT_ID,
        "operation": "import",
        "dry_run": False,
        "category_count": 3,
        "knowledge_count": 8,
        "created": 8,
        "reused": 0,
        "conflicts": 0,
        "archived": 0,
        "activated": 0,
    }
    with SessionLocal() as db:
        rows = list(db.scalars(select(KnowledgeItem)))
        categories = list(db.scalars(select(KnowledgeCategory)))
    assert {row.item_id for row in rows} == SEED_ITEM_IDS
    assert {(row.layer, row.category_id) for row in categories} == {
        (item["layer"], item["id"]) for item in CATEGORY_DEFINITIONS
    }
    assert all(row.tenant_id == "chejin" and row.product_id == "" and row.status == "active" for row in rows)
    assert all(row.payload["review_state"]["is_new"] is True for row in rows)
    assert all(row.payload["runtime"] == {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"} for row in rows)
    assert all(row.payload["metadata"]["seed_id"] == SEED_ID for row in rows)
    assert all(len(row.payload["metadata"]["content_sha256"]) == 64 for row in rows)


def test_import_is_idempotent_and_dry_run_does_not_write():
    preview = _run(dry_run=True)
    assert preview["created"] == 8
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(KnowledgeItem)) == 0

    assert _run()["created"] == 8
    repeated = _run()
    assert repeated["created"] == 0
    assert repeated["reused"] == 8
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(KnowledgeItem)) == 8


def test_cli_is_explicit_and_reports_machine_readable_counts(capsys):
    exit_code = seed_command.main(["--seed", SEED_ID, "--dry-run"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["dry_run"] is True
    assert output["created"] == 8
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(KnowledgeItem)) == 0
    assert SEED_ID not in (PROJECT_ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    assert all(
        SEED_ID not in path.read_text(encoding="utf-8")
        for path in (PROJECT_ROOT / "backend" / "alembic" / "versions").glob("*.py")
    )


def test_cli_import_activate_and_rollback_lifecycle(capsys):
    assert seed_command.main(["--seed", SEED_ID]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["created"] == 8

    assert seed_command.main(["--seed", SEED_ID]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["created"] == 0
    assert repeated["reused"] == 8

    assert seed_command.main(["--seed", SEED_ID, "--activate"]) == 0
    activated = json.loads(capsys.readouterr().out)
    assert activated["activated"] == 8

    assert seed_command.main(["--seed", SEED_ID, "--rollback"]) == 0
    rolled_back = json.loads(capsys.readouterr().out)
    assert rolled_back["archived"] == 8


def test_same_item_id_with_different_identity_or_sha_fails_atomically():
    definition = SEED_ITEMS[0]
    with SessionLocal.begin() as db:
        db.add(
            KnowledgeItem(
                tenant_id=TENANT_ID,
                layer=definition.layer,
                category_id=definition.category_id,
                product_id="",
                item_id=definition.item_id,
                status="active",
                search_text="collision",
                payload={"id": definition.item_id, "metadata": {"content_sha256": "different", "seed_version": 1}},
            )
        )

    with pytest.raises(KnowledgeSeedConflictError, match="chejin_evidence_first"):
        _run()
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(KnowledgeItem)) == 1
        assert db.scalar(select(func.count()).select_from(KnowledgeCategory)) == 0


def test_cli_conflict_fails_with_structured_count(capsys):
    definition = SEED_ITEMS[0]
    with SessionLocal.begin() as db:
        db.add(
            KnowledgeItem(
                tenant_id=TENANT_ID,
                layer=definition.layer,
                category_id=definition.category_id,
                product_id="",
                item_id=definition.item_id,
                status="active",
                search_text="collision",
                payload={"id": definition.item_id, "metadata": {"content_sha256": "different"}},
            )
        )

    assert seed_command.main(["--seed", SEED_ID]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["conflicts"] == 1
    assert output["conflict_item_ids"] == [definition.item_id]


def test_unreviewed_items_are_invisible_then_activation_is_chejin_only(monkeypatch):
    _run()
    resolver = _resolver(monkeypatch)
    assert resolver.resolve("8 万预算代步")["evidence_items"] == []

    activated = _run("activate")
    assert activated["activated"] == 8
    chejin_result = _resolver(monkeypatch, tenant_id="chejin").resolve("8 万预算代步")
    default_result = _resolver(monkeypatch, tenant_id="default").resolve("8 万预算代步")
    assert "chejin_lead_need_collection" in _matched_ids(chejin_result)
    assert default_result["evidence_items"] == []


@pytest.mark.parametrize(
    ("message", "expected_id", "forbidden_fragments"),
    [
        ("8 万预算代步", "chejin_lead_need_collection", ("推荐具体车型",)),
        ("之前那台还有吗", "chejin_returning_lead_recheck", ("直接回答‘有’",)),
        ("旧车想置换", "chejin_trade_in_collection", ("承诺估价", "固定处理时效")),
        ("我是抖音来的", "chejin_douyin_lead_intake", ("不得承诺库存", "成交结果")),
    ],
)
def test_activated_seed_reaches_formal_evidence(message, expected_id, forbidden_fragments, monkeypatch):
    _run()
    _run("activate")

    result = _resolver(monkeypatch).resolve(message)
    matched = next(item for item in result["evidence_items"] if item["item_id"] == expected_id)
    assert matched["allow_auto_reply"] is True
    assert matched["requires_handoff"] is False
    assert matched["risk_level"] == "normal"
    combined_excerpts = " ".join(item["reply_excerpt"] for item in result["evidence_items"])
    assert all(fragment in combined_excerpts for fragment in forbidden_fragments)
    assert not any(item["category_id"] == "products" for item in result["evidence_items"])


@pytest.mark.parametrize("message", ["家电怎么安装", "办公家具发票", "物流怎么收费", "测试公司资料"])
def test_unrelated_old_industry_knowledge_never_appears(message, monkeypatch):
    _run()
    _run("activate")

    result = _resolver(monkeypatch).resolve(message)
    assert _matched_ids(result) <= {"chejin_evidence_first", "chejin_entity_normalization"}
    assert not ({"wfcase", "wfrq"} & set(" ".join(_matched_ids(result)).lower().split()))


def test_soft_test_drive_rejection_does_not_gain_handoff_knowledge(monkeypatch):
    _run()
    _run("activate")

    result = _resolver(monkeypatch).resolve("暂时不想试驾")
    assert all(item["requires_handoff"] is False for item in result["evidence_items"])
    assert not any(item["category_id"] == "policies" for item in result["evidence_items"])


def test_existing_hard_gate_safety_is_unchanged_by_seed_activation(monkeypatch):
    resolver = _resolver(monkeypatch)
    messages = ("合同怎么签", "付款后怎么赔偿", "现在就交定金")
    before = {message: resolver.resolve(message)["safety"] for message in messages}
    _run()
    _run("activate")
    after = {message: _resolver(monkeypatch).resolve(message)["safety"] for message in messages}

    assert after == before
    assert all(value["must_handoff"] is True for value in after.values())


def test_rollback_archives_only_this_seed_and_removes_it_from_runtime(monkeypatch):
    _run()
    _run("activate")
    with SessionLocal.begin() as db:
        db.add(
            KnowledgeItem(
                tenant_id=TENANT_ID,
                layer="tenant",
                category_id="policies",
                product_id="",
                item_id="existing_unrelated_policy",
                status="active",
                search_text="既有知识",
                payload={
                    "id": "existing_unrelated_policy",
                    "category_id": "policies",
                    "status": "active",
                    "data": {"title": "既有知识", "keywords": ["既有知识"], "answer": "保持可用"},
                    "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
                    "review_state": {"is_new": False},
                },
            )
        )

    result = _run("rollback")
    assert result["archived"] == 8
    with SessionLocal() as db:
        seed_rows = list(db.scalars(select(KnowledgeItem).where(KnowledgeItem.item_id.in_(SEED_ITEM_IDS))))
        unrelated = db.scalar(select(KnowledgeItem).where(KnowledgeItem.item_id == "existing_unrelated_policy"))
    assert all(row.status == "archived" and row.payload["status"] == "archived" for row in seed_rows)
    assert unrelated is not None and unrelated.status == "active"
    runtime = _resolver(monkeypatch).runtime
    assert not (SEED_ITEM_IDS & {item["id"] for item in runtime.list_items("policies")})
    assert not (SEED_ITEM_IDS & {item["id"] for item in runtime.list_items("global_guidelines")})
