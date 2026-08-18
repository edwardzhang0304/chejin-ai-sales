"""Idempotent import of the reviewed Chejin formal-knowledge seed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.vehicle import KnowledgeCategory, KnowledgeItem, KnowledgeTenant


TENANT_ID = "chejin"
SEED_ID = "chejin_kb_seed_v1"
SEED_VERSION = 1
PRODUCT_ID = ""
RUNTIME_POLICY = {
    "allow_auto_reply": True,
    "requires_handoff": False,
    "risk_level": "normal",
}


@dataclass(frozen=True)
class SeedDefinition:
    item_id: str
    layer: Literal["shared", "tenant"]
    category_id: str
    title: str
    keywords: tuple[str, ...]
    content: str
    always_include: bool = False
    policy_type: str = ""


SEED_ITEMS = (
    SeedDefinition(
        item_id="chejin_evidence_first",
        layer="shared",
        category_id="global_guidelines",
        title="车辆事实证据优先",
        keywords=("车型", "价格", "库存", "配置", "车况", "优惠", "证据"),
        always_include=True,
        content="车型、价格、库存、配置、车况和优惠等事实，只能来自当前有效 Product Master 或当前会话已确认事实。没有证据时应说明需要确认或继续追问，不得根据常识补齐。",
    ),
    SeedDefinition(
        item_id="chejin_entity_normalization",
        layer="shared",
        category_id="global_guidelines",
        title="车型实体先归一",
        keywords=("车型简称", "错别字", "同音词", "品牌别名", "车型归一"),
        always_include=True,
        content="客户提及车型简称、错别字、同音词或品牌别名时先进行归一；无法唯一确认时，只追问一个必要问题，不得猜测车型。",
    ),
    SeedDefinition(
        item_id="chejin_wechat_natural_style",
        layer="shared",
        category_id="reply_style",
        title="微信自然回复风格",
        keywords=("微信", "称呼", "开场", "连续对话", "回复风格"),
        content="回复应简短自然，先接住客户当前问题；称呼只用于开场或新话题，连续对话不重复称呼；不得从群名、门店名、文件传输助手或测试名称推断客户称呼。",
    ),
    SeedDefinition(
        item_id="chejin_small_talk_pivot",
        layer="shared",
        category_id="reply_style",
        title="闲聊自然转入购车需求",
        keywords=("闲聊", "聊天", "购车需求", "代步"),
        content="客户闲聊时先自然回应，再结合上下文询问一个购车相关问题；没有车辆证据时不得借机推荐具体车型。",
    ),
    SeedDefinition(
        item_id="chejin_lead_need_collection",
        layer="tenant",
        category_id="policies",
        title="购车需求收集",
        keywords=("预算", "代步", "用途", "车型偏好", "车身类型", "城市", "贷款", "全款", "置换"),
        policy_type="lead_need_collection",
        content="低风险购车咨询可收集预算范围、主要用途、车型或车身类型偏好、所在城市、贷款或全款、是否置换。每轮只询问一到两个最关键问题，不要一次连续追问全部信息；没有车辆证据时不得推荐具体车型。",
    ),
    SeedDefinition(
        item_id="chejin_douyin_lead_intake",
        layer="tenant",
        category_id="policies",
        title="抖音线索接待",
        keywords=("抖音", "直播", "新加微信", "预算", "用途", "意向车型", "城市"),
        policy_type="douyin_lead_intake",
        content="抖音、直播或新加微信线索，AI 可以先确认预算、用途、意向车型和所在城市；不得承诺库存、价格、到店安排或成交结果。",
    ),
    SeedDefinition(
        item_id="chejin_trade_in_collection",
        layer="tenant",
        category_id="policies",
        title="置换资料收集",
        keywords=("旧车", "置换", "上牌年份", "公里数", "车况", "贷款", "照片", "估价"),
        policy_type="trade_in_collection",
        content="置换咨询可收集旧车品牌车型、上牌年份、公里数、车况、所在城市、是否贷款和必要照片；不得承诺估价、最终收车价、上门验车或固定处理时效。",
    ),
    SeedDefinition(
        item_id="chejin_returning_lead_recheck",
        layer="tenant",
        category_id="policies",
        title="老客户重新核验车源",
        keywords=("之前那台", "还有吗", "老客户", "新车源", "库存", "重新查询"),
        policy_type="returning_lead_recheck",
        content="老客户询问之前车辆或新车源时，必须重新查询当前 Product Master；没有有效库存证据时只能说明需要确认，并可询问预算或车型是否变化，不能直接回答‘有’。",
    ),
)


CATEGORY_DEFINITIONS = (
    {
        "layer": "shared",
        "id": "global_guidelines",
        "name": "共享通用客服原则",
        "kind": "global",
        "path": "global_guidelines",
        "sort_order": 10,
    },
    {
        "layer": "shared",
        "id": "reply_style",
        "name": "共享回复口吻",
        "kind": "global",
        "path": "reply_style",
        "sort_order": 75,
    },
    {
        "layer": "tenant",
        "id": "policies",
        "name": "车金业务政策",
        "kind": "classified",
        "path": "policies",
        "sort_order": 30,
    },
)


class KnowledgeSeedConflictError(RuntimeError):
    def __init__(self, item_ids: list[str]) -> None:
        self.item_ids = sorted(set(item_ids))
        super().__init__(f"CHEJIN_KNOWLEDGE_SEED_CONFLICT: {', '.join(self.item_ids)}")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def definition_payload(definition: SeedDefinition) -> dict[str, Any]:
    data: dict[str, Any] = {
        "title": definition.title,
        "keywords": list(definition.keywords),
    }
    if definition.category_id in {"global_guidelines", "reply_style"}:
        data.update(
            {
                "guideline_text": definition.content,
                "applies_to": "车金微信售前客服",
            }
        )
        if definition.always_include:
            data["always_include"] = True
    else:
        data.update(
            {
                "policy_type": definition.policy_type,
                "answer": definition.content,
                "applicability_scope": "global",
            }
        )
    return {
        "id": definition.item_id,
        "category_id": definition.category_id,
        "schema_version": 1,
        "status": "active",
        "source": {
            "type": "chejin_formal_seed",
            "seed_id": SEED_ID,
            "entry_id": definition.item_id,
        },
        "data": data,
        "runtime": dict(RUNTIME_POLICY),
    }


def definition_sha256(definition: SeedDefinition) -> str:
    identity = {
        "seed_id": SEED_ID,
        "seed_version": SEED_VERSION,
        "tenant_id": TENANT_ID,
        "layer": definition.layer,
        "category_id": definition.category_id,
        "product_id": PRODUCT_ID,
        "payload": definition_payload(definition),
    }
    return sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def build_persisted_payload(
    definition: SeedDefinition,
    *,
    imported_at: datetime,
    is_new: bool,
) -> dict[str, Any]:
    payload = definition_payload(definition)
    payload["review_state"] = {
        "is_new": is_new,
        "acknowledged_at": None if is_new else imported_at.isoformat(),
        "acknowledged_by": None if is_new else "chejin_kb_seed_activate",
    }
    payload["metadata"] = {
        "seed_id": SEED_ID,
        "seed_version": SEED_VERSION,
        "content_sha256": definition_sha256(definition),
        "source_entry": definition.item_id,
        "imported_at": imported_at.isoformat(),
    }
    return payload


def seed_search_text(definition: SeedDefinition) -> str:
    return " ".join((definition.item_id, definition.title, *definition.keywords, definition.content))


def import_chejin_knowledge(
    db: Session,
    *,
    operation: Literal["import", "activate", "rollback"] = "import",
    dry_run: bool = False,
    allow_version_upgrade: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    if operation not in {"import", "activate", "rollback"}:
        raise ValueError(f"unsupported seed operation: {operation}")
    imported_at = now or datetime.now(timezone.utc)
    existing_rows = list(
        db.scalars(
            select(KnowledgeItem).where(
                KnowledgeItem.tenant_id == TENANT_ID,
                KnowledgeItem.item_id.in_([item.item_id for item in SEED_ITEMS]),
            )
        )
    )
    by_id = {row.item_id: row for row in existing_rows}
    conflicts: list[str] = []
    missing: list[str] = []
    upgrade_ids: set[str] = set()
    for definition in SEED_ITEMS:
        existing = by_id.get(definition.item_id)
        if existing is None:
            if operation == "activate":
                missing.append(definition.item_id)
            continue
        metadata = existing.payload.get("metadata") if isinstance(existing.payload, dict) else {}
        source = existing.payload.get("source") if isinstance(existing.payload, dict) else {}
        existing_sha = str((metadata or {}).get("content_sha256") or "")
        same_seed = (
            str((metadata or {}).get("seed_id") or "") == SEED_ID
            and str((source or {}).get("seed_id") or "") == SEED_ID
        )
        same_identity = (
            existing.layer == definition.layer
            and existing.category_id == definition.category_id
            and existing.product_id == PRODUCT_ID
        )
        if same_seed and same_identity and existing_sha == definition_sha256(definition):
            continue
        previous_version = (metadata or {}).get("seed_version")
        upgrade_allowed = (
            allow_version_upgrade
            and same_seed
            and same_identity
            and isinstance(previous_version, int)
            and not isinstance(previous_version, bool)
            and previous_version < SEED_VERSION
        )
        if not upgrade_allowed:
            conflicts.append(definition.item_id)
        else:
            upgrade_ids.add(definition.item_id)
    if missing:
        raise RuntimeError(f"CHEJIN_KNOWLEDGE_SEED_NOT_IMPORTED: {', '.join(sorted(missing))}")
    if conflicts:
        raise KnowledgeSeedConflictError(conflicts)

    result = {
        "seed_id": SEED_ID,
        "seed_version": SEED_VERSION,
        "tenant_id": TENANT_ID,
        "operation": operation,
        "dry_run": dry_run,
        "category_count": len(CATEGORY_DEFINITIONS),
        "knowledge_count": len(SEED_ITEMS),
        "created": 0,
        "reused": 0,
        "conflicts": 0,
        "archived": 0,
        "activated": 0,
    }
    if operation == "import":
        result["created"] = sum(1 for item in SEED_ITEMS if item.item_id not in by_id or item.item_id in upgrade_ids)
        result["reused"] = len(SEED_ITEMS) - result["created"]
    elif operation == "activate":
        result["activated"] = sum(
            1
            for row in existing_rows
            if row.status != "active" or bool((row.payload.get("review_state") or {}).get("is_new"))
        )
        result["reused"] = len(SEED_ITEMS) - result["activated"]
    else:
        result["archived"] = sum(1 for row in existing_rows if row.status != "archived")
        result["reused"] = len(existing_rows) - result["archived"]
    if dry_run:
        return result

    _ensure_catalog(db)
    if operation == "import":
        for definition in SEED_ITEMS:
            existing = by_id.get(definition.item_id)
            if existing is not None and definition.item_id not in upgrade_ids:
                continue
            payload = build_persisted_payload(definition, imported_at=imported_at, is_new=True)
            if existing is None:
                db.add(
                    KnowledgeItem(
                        tenant_id=TENANT_ID,
                        layer=definition.layer,
                        category_id=definition.category_id,
                        product_id=PRODUCT_ID,
                        item_id=definition.item_id,
                        status="active",
                        search_text=seed_search_text(definition),
                        payload=payload,
                    )
                )
            else:
                existing.status = "active"
                existing.search_text = seed_search_text(definition)
                existing.payload = payload
    elif operation == "activate":
        for row in existing_rows:
            payload = dict(row.payload)
            review_state = dict(payload.get("review_state") or {})
            review_state.update(
                {
                    "is_new": False,
                    "acknowledged_at": imported_at.isoformat(),
                    "acknowledged_by": "chejin_kb_seed_activate",
                }
            )
            payload["review_state"] = review_state
            payload["status"] = "active"
            row.payload = payload
            row.status = "active"
    else:
        for row in existing_rows:
            payload = dict(row.payload)
            metadata = dict(payload.get("metadata") or {})
            metadata.update(
                {
                    "archived_at": imported_at.isoformat(),
                    "archived_by": "chejin_kb_seed_rollback",
                }
            )
            payload["metadata"] = metadata
            payload["status"] = "archived"
            row.payload = payload
            row.status = "archived"
    db.flush()
    return result


def _ensure_catalog(db: Session) -> None:
    if not db.get(KnowledgeTenant, TENANT_ID):
        db.add(
            KnowledgeTenant(
                tenant_id=TENANT_ID,
                display_name="车金",
                payload={"source": "chejin_backend"},
            )
        )
    for definition in CATEGORY_DEFINITIONS:
        key = {
            "tenant_id": TENANT_ID,
            "layer": definition["layer"],
            "category_id": definition["id"],
        }
        existing = db.get(KnowledgeCategory, key)
        payload = {
            "id": definition["id"],
            "name": definition["name"],
            "kind": definition["kind"],
            "path": definition["path"],
            "enabled": True,
            "participates_in_reply": True,
            "participates_in_learning": False,
            "participates_in_diagnostics": True,
            "sort_order": definition["sort_order"],
            "scope": definition["layer"],
            "source": SEED_ID,
        }
        if existing:
            existing.enabled = True
            existing.sort_order = definition["sort_order"]
            existing.payload = payload
        else:
            db.add(
                KnowledgeCategory(
                    **key,
                    enabled=True,
                    sort_order=definition["sort_order"],
                    payload=payload,
                )
            )
