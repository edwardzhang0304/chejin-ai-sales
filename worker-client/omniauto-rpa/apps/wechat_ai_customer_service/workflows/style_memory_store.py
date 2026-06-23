"""Tenant-scoped live chat style memory helpers.

The style memory is deliberately fact-light: it retrieves reusable service
wording patterns, not product facts. Runtime callers still use the ordinary
product, formal knowledge and current-conversation evidence layers for what to say.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:  # pragma: no cover - supports script imports from workflows/.
    from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, tenant_industry_hint, tenant_root
except ImportError:  # pragma: no cover
    active_tenant_id = lambda tenant_id=None: tenant_id or "default"
    tenant_industry_hint = lambda tenant_id=None: ""
    tenant_root = lambda tenant_id=None: Path("apps/wechat_ai_customer_service/data/tenants") / active_tenant_id(tenant_id)

try:  # pragma: no cover
    from rag_answer_layer import extract_service_style_snippet
except ImportError:  # pragma: no cover
    from .rag_answer_layer import extract_service_style_snippet


STYLE_CATEGORY_IDS = {"chats", "reply_style", "global_guidelines"}
STYLE_SOURCE_TYPES = {"cleaned_real_chat_pack", "real_chat_style", "wechat_raw_message", "upload", "real_chat", "chat_template"}
PRODUCT_MASTER_TERMS = (
    "库存",
    "售价",
    "报价",
    "指导价",
    "车架号",
    "vin",
    "inventory",
    "price:",
    "stock:",
    "specs:",
)


def retrieve_style_examples(
    customer_message: str,
    *,
    evidence_pack: dict[str, Any] | None = None,
    limit: int = 3,
    min_similarity: float = 0.08,
    include_rag_style_hits: bool = True,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return reusable style examples for the current tenant and message."""
    tenant = active_tenant_id(tenant_id)
    examples: list[dict[str, Any]] = []
    pack = evidence_pack if isinstance(evidence_pack, dict) else {}
    evidence = pack.get("evidence") if isinstance(pack.get("evidence"), dict) else {}
    for raw in evidence.get("style_examples", []) or []:
        if not isinstance(raw, dict):
            continue
        normalized = normalize_style_example(raw, customer_message=customer_message, tenant_id=tenant, source="evidence_style")
        if normalized:
            examples.append(normalized)

    if include_rag_style_hits:
        rag_evidence = pack.get("rag_evidence") if isinstance(pack.get("rag_evidence"), dict) else {}
        for raw in rag_evidence.get("hits", []) or []:
            if not isinstance(raw, dict):
                continue
            if not rag_hit_looks_like_style(raw):
                continue
            normalized = normalize_style_example(raw, customer_message=customer_message, tenant_id=tenant, source="rag_hit")
            if normalized:
                examples.append(normalized)

    persisted_examples = load_persisted_style_examples(customer_message, tenant_id=tenant, limit=limit * 3)
    examples.extend(persisted_examples)
    if len(persisted_examples) < max(1, int(limit or 1)) and include_rag_style_hits:
        examples.extend(load_governed_rag_style_examples(customer_message, tenant_id=tenant, limit=limit * 3))
    deduped = dedupe_examples(examples)
    threshold = max(0.0, float(min_similarity or 0.0))
    filtered = [item for item in deduped if float(item.get("score") or 0.0) >= threshold]
    filtered.sort(key=lambda item: (float(item.get("score") or 0.0), float(item.get("quality_score") or 0.0)), reverse=True)
    return filtered[: max(1, int(limit or 1))]


def normalize_style_example(
    raw: dict[str, Any],
    *,
    customer_message: str,
    tenant_id: str,
    source: str,
) -> dict[str, Any] | None:
    source_id = str(raw.get("id") or raw.get("item_id") or raw.get("chunk_id") or raw.get("source_id") or "").strip()
    raw_customer = str(raw.get("customer_message") or raw.get("question") or raw.get("query") or "")
    raw_reply = str(
        raw.get("service_reply")
        or raw.get("reply")
        or raw.get("guideline_text")
        or raw.get("answer")
        or raw.get("text")
        or ""
    )
    service_reply = sanitize_style_reply(clean_style_reply(raw_reply))
    if not service_reply or len(service_reply) < 6:
        return None
    if looks_like_product_master_data(service_reply):
        return None
    similarity = reply_similarity(customer_message, " ".join([raw_customer, service_reply]))
    score = max(similarity, source_base_score(raw)) + evidence_score(raw)
    quality_score = float(raw.get("quality_score") or raw.get("confidence") or raw.get("score") or 0.0)
    return {
        "id": source_id or stable_style_id(raw_customer, service_reply),
        "tenant_id": tenant_id,
        "industry_id": str(raw.get("industry_id") or tenant_industry_hint(tenant_id) or ""),
        "source": source,
        "source_type": str(raw.get("source_type") or source),
        "source_id": str(raw.get("source_id") or source_id or ""),
        "customer_message": compact_text(raw_customer, 180),
        "service_reply": compact_text(service_reply, 240),
        "runtime_usage": {
            "can_affect_style": True,
            "can_authorize_reply_content": False,
        },
        "intent_tags": [str(item) for item in raw.get("intent_tags", []) or [] if str(item)],
        "tone_tags": [str(item) for item in raw.get("tone_tags", []) or [] if str(item)],
        "risk_tags": style_risk_tags(service_reply),
        "similarity": round(similarity, 4),
        "quality_score": round(quality_score, 4),
        "score": round(min(score, 1.0), 4),
    }


def load_persisted_style_examples(customer_message: str, *, tenant_id: str, limit: int) -> list[dict[str, Any]]:
    """Load optional future tenant style-memory files.

    The first implementation works without this file because current evidence
    packs already include formal chat items and allowed retrieval hits. This loader makes the
    module reusable for future one-click imports that write a dedicated style
    memory index.
    """
    root = tenant_root(tenant_id) / "style_memory"
    paths = [root / "examples.jsonl", root / "examples.json"]
    loaded: list[dict[str, Any]] = []
    scan_cap = max(100, min(max(1, int(limit or 1)) * 120, 1200))
    for path in paths:
        if not path.exists():
            continue
        try:
            if path.suffix.lower() == ".jsonl":
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            else:
                payload = json.loads(path.read_text(encoding="utf-8"))
                rows = payload.get("items", payload) if isinstance(payload, dict) else payload
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rows, list):
            continue
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("status") or "active") not in {"active", "approved", "enabled"}:
                continue
            normalized = normalize_style_example(raw, customer_message=customer_message, tenant_id=tenant_id, source="style_memory")
            if normalized:
                loaded.append(normalized)
            if len(loaded) >= scan_cap:
                break
        if loaded:
            loaded.sort(key=lambda item: (float(item.get("score") or 0.0), float(item.get("quality_score") or 0.0)), reverse=True)
            return loaded[:scan_cap]
    return loaded


def load_governed_rag_style_examples(customer_message: str, *, tenant_id: str, limit: int) -> list[dict[str, Any]]:
    """Load style-only AI experience pool items as a fallback style memory source."""

    try:
        from apps.wechat_ai_customer_service.admin_backend.services.rag_experience_governance import attach_governance
        from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore, with_quality
    except Exception:
        return []
    loaded: list[dict[str, Any]] = []
    scan_cap = max(100, min(max(1, int(limit or 1)) * 120, 1200))
    try:
        records = RagExperienceStore(tenant_id=tenant_id).list_for_counts()
    except Exception:
        return []
    for raw in records[:scan_cap]:
        item = attach_governance(with_quality(raw))
        governance = item.get("governance") if isinstance(item.get("governance"), dict) else {}
        if str(governance.get("effective_state") or "") != "style_only":
            continue
        if not bool(governance.get("style_allowed")):
            continue
        if bool(governance.get("retrieval_allowed")):
            continue
        example = normalize_style_example(
            {
                "id": f"rag_style_only_{item.get('experience_id') or ''}",
                "source_id": item.get("experience_id"),
                "source_type": item.get("source_type") or item.get("source") or "rag_style_only",
                "customer_message": item.get("question") or item.get("summary") or "",
                "service_reply": item.get("reply_text") or item.get("evidence_excerpt") or "",
                "score": (item.get("quality") or {}).get("score") or 0.5,
                "quality_score": (item.get("quality") or {}).get("score") or 0.5,
            },
            customer_message=customer_message,
            tenant_id=tenant_id,
            source="rag_style_only",
        )
        if example:
            loaded.append(example)
        if len(loaded) >= scan_cap:
            break
    loaded.sort(key=lambda item: (float(item.get("score") or 0.0), float(item.get("quality_score") or 0.0)), reverse=True)
    return loaded[:scan_cap]


def rag_hit_looks_like_style(hit: dict[str, Any]) -> bool:
    category = str(hit.get("category") or "")
    source_type = str(hit.get("source_type") or "")
    text = str(hit.get("text") or "")
    if category in STYLE_CATEGORY_IDS or source_type in STYLE_SOURCE_TYPES:
        return True
    return "客服：" in text or "service_reply" in text or "话术" in text


def clean_style_reply(text: str) -> str:
    clean = extract_service_style_snippet(str(text or ""))
    clean = re.sub(r"^\[[^\]]{0,30}(?:客服|AI|OmniAuto)[^\]]*\]\s*", "", clean, flags=re.I)
    clean = clean.replace("转人工客服", "核实确认").replace("转人工", "核实确认")
    clean = clean.replace("人工客服", "负责同事").replace("真人客服", "负责同事")
    clean = re.sub(r"\s+", " ", clean).strip(" ：:，,。；;")
    if clean and not clean.endswith(("。", "？", "！", "…")):
        clean += "。"
    return clean


def sanitize_style_reply(text: str) -> str:
    """Remove time-sensitive facts so real chat examples only teach wording."""

    clean = str(text or "")
    clean = re.sub(r"1[3-9]\d{9}", "{手机号}", clean)
    clean = re.sub(r"\b[A-HJ-NPR-Z0-9]{17}\b", "{车架号}", clean, flags=re.I)
    clean = re.sub(r"\d+(?:\.\d+)?\s*(?:万公里|w公里|W公里|公里)", "{公里数}", clean)
    clean = re.sub(r"\d+(?:\.\d+)?\s*(?:万|w|W|元|块)", "{价格}", clean)
    clean = re.sub(r"(?:19|20)\d{2}\s*款", "{年份}款", clean)
    clean = re.sub(r"(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*上牌", "{上牌时间}", clean)
    clean = re.sub(r"(?:19|20)\d{2}\s*年", "{年份}", clean)
    clean = clean.replace("包过", "需要核实").replace("保证无事故", "以检测报告为准")
    return re.sub(r"\s+", " ", clean).strip()


def looks_like_product_master_data(text: str) -> bool:
    clean = normalize_text(text)
    term_hits = sum(1 for term in PRODUCT_MASTER_TERMS if term.lower() in clean)
    digit_count = len(re.findall(r"\d", clean))
    money_like = len(re.findall(r"\d+(?:\.\d+)?\s*(?:万|元|¥)", clean, flags=re.I))
    return term_hits >= 2 and (digit_count >= 6 or money_like >= 2)


def source_base_score(raw: dict[str, Any]) -> float:
    category = str(raw.get("category") or "")
    source_type = str(raw.get("source_type") or "")
    if source_type == "cleaned_real_chat_pack":
        return 0.42
    if category in STYLE_CATEGORY_IDS:
        return 0.36
    if source_type in STYLE_SOURCE_TYPES:
        return 0.30
    return 0.18


def evidence_score(raw: dict[str, Any]) -> float:
    try:
        raw_score = float(raw.get("score") or raw.get("confidence") or 0.0)
    except (TypeError, ValueError):
        raw_score = 0.0
    return min(max(raw_score, 0.0), 1.0) * 0.18


def style_risk_tags(text: str) -> list[str]:
    clean = normalize_text(text)
    tags: list[str] = []
    if any(term in clean for term in ("转人工", "人工客服", "真人客服")):
        tags.append("explicit_handoff_phrase")
    if any(term in clean for term in ("我是ai", "我是机器人", "智能客服", "自动回复")):
        tags.append("ai_identity_phrase")
    if looks_like_product_master_data(text):
        tags.append("product_master_like")
    return tags


def dedupe_examples(examples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in examples:
        key = str(item.get("id") or "") or normalize_text(str(item.get("service_reply") or ""))[:80]
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def reply_similarity(left: str, right: str) -> float:
    left_tokens = char_bigrams(normalize_text(left))
    right_tokens = char_bigrams(normalize_text(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))


def char_bigrams(text: str) -> set[str]:
    if len(text) <= 1:
        return {text} if text else set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


def normalize_text(text: str) -> str:
    return "".join(str(text or "").split()).lower()


def compact_text(text: str, limit: int) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= limit:
        return clean
    return clean[: max(1, limit - 1)].rstrip("，,。；; ") + "…"


def stable_style_id(customer_message: str, service_reply: str) -> str:
    import hashlib

    digest = hashlib.sha256(f"{customer_message}|{service_reply}".encode("utf-8")).hexdigest()
    return "style_" + digest[:16]
