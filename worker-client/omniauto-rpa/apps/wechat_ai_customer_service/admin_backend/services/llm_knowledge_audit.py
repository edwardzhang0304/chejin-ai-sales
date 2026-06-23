"""LLM-powered knowledge base and AI experience pool item quality audit.

This module uses a DeepSeek model to detect semantic duplicates,
logic errors, and scenario-inappropriate content across both formal
knowledge bases and the AI experience pool item layer.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from apps.wechat_ai_customer_service.llm_config import (
    apply_llm_reasoning_effort,
    llm_urlopen,
    read_secret,
    resolve_deepseek_base_url,
    resolve_deepseek_max_tokens,
    resolve_deepseek_tier_model,
    resolve_deepseek_timeout,
)
from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore

from .knowledge_base_store import KnowledgeBaseStore
from .knowledge_registry import KnowledgeRegistry
from .knowledge_schema_manager import KnowledgeSchemaManager


MAX_ITEMS_PER_BATCH = 30
LLM_MAX_TOKENS = 4096


def audit_knowledge_and_rag(tenant_id: str, max_items_per_batch: int = MAX_ITEMS_PER_BATCH) -> list[dict[str, Any]]:
    """Run LLM audit over all active knowledge items and AI experience pool items.

    Returns a list of diagnostic issues with action_type, involved_targets,
    and llm_reasoning fields.  If API key is missing or the call fails,
    returns an empty list (or a single warning issue for total failure).
    """
    if not has_llm_config():
        return []

    entries = _collect_entries()
    if not entries:
        return []

    # Batch entries to stay within prompt token budget
    batches = _batch_entries(entries, max_items_per_batch)
    all_findings: list[dict[str, Any]] = []
    for batch in batches:
        findings = _call_llm_audit_batch(batch)
        all_findings.extend(findings)

    if not all_findings:
        return []

    # Deduplicate by involved_targets set
    seen_target_sets: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for finding in all_findings:
        key = _target_set_key(finding.get("involved_targets", []))
        if key and key in seen_target_sets:
            continue
        if key:
            seen_target_sets.add(key)
        deduped.append(finding)

    return [_finding_to_issue(f) for f in deduped]


def auto_dedup_rag_experiences(tenant_id: str, max_items_per_batch: int = MAX_ITEMS_PER_BATCH) -> dict[str, Any]:
    """Automatically detect and discard duplicate AI experience pool items.

    This is meant to run without manual confirmation. Duplicate RAG
    experiences are discarded silently (the one with lower reply_count
    or more recent creation time is discarded).
    """
    if not has_llm_config():
        return {"ok": True, "message": "LLM not configured, skipped auto dedup", "discarded": []}

    store = RagExperienceStore()
    experiences = store.list(status="active", limit=500)
    if not experiences:
        return {"ok": True, "message": "No active AI experience pool items", "discarded": []}

    entries = [_serialize_rag_experience(e) for e in experiences]
    batches = _batch_entries(entries, max_items_per_batch)

    all_findings: list[dict[str, Any]] = []
    for batch in batches:
        findings = _call_llm_audit_batch(batch, rag_only_mode=True)
        all_findings.extend(findings)

    # Keep only merge/delete findings that involve RAG targets
    duplicates: list[dict[str, Any]] = []
    seen_target_sets: set[str] = set()
    for finding in all_findings:
        targets = finding.get("involved_targets", [])
        if not targets or len(targets) < 2:
            continue
        if not all(t.startswith("rag_exp_") for t in targets):
            continue
        if finding.get("action") not in ("merge", "delete"):
            continue
        key = _target_set_key(targets)
        if key in seen_target_sets:
            continue
        seen_target_sets.add(key)
        duplicates.append(finding)

    discarded: list[str] = []
    for finding in duplicates:
        targets = finding.get("involved_targets", [])
        # Determine which experience to keep (higher reply_count, or older)
        to_discard = _choose_discard_target(targets, experiences)
        if to_discard:
            try:
                store.discard(to_discard, reason="auto_dedup: LLM detected semantic duplicate")
                discarded.append(to_discard)
            except KeyError:
                pass

    return {
        "ok": True,
        "message": f"Auto-dedup discarded {len(discarded)} AI experience pool item(s)",
        "discarded": discarded,
        "duplicate_groups": [f.get("involved_targets", []) for f in duplicates],
    }


def has_llm_config() -> bool:
    return bool(read_secret("DEEPSEEK_API_KEY"))


def _collect_entries() -> list[dict[str, Any]]:
    """Gather all active knowledge items and AI experience pool items into a flat list."""
    entries: list[dict[str, Any]] = []
    entries.extend(_collect_knowledge_entries())
    entries.extend(_collect_rag_entries())
    return entries


def _collect_knowledge_entries() -> list[dict[str, Any]]:
    try:
        registry = KnowledgeRegistry()
        schema_manager = KnowledgeSchemaManager(registry)
        store = KnowledgeBaseStore(registry, schema_manager)
    except Exception:
        return []
    entries: list[dict[str, Any]] = []
    for category in registry.list_categories(enabled_only=True):
        category_id = str(category.get("id") or "")
        if not category_id:
            continue
        try:
            items = store.list_items(category_id, include_archived=False)
        except Exception:
            continue
        for item in items:
            if item.get("status") == "archived":
                continue
            serialized = _serialize_knowledge_item(category_id, item)
            if serialized:
                entries.append(serialized)
    return entries


def _collect_rag_entries() -> list[dict[str, Any]]:
    try:
        store = RagExperienceStore()
        experiences = store.list(status="active", limit=500)
    except Exception:
        return []
    entries: list[dict[str, Any]] = []
    for exp in experiences:
        serialized = _serialize_rag_experience(exp)
        if serialized:
            entries.append(serialized)
    return entries


def _serialize_knowledge_item(category_id: str, item: dict[str, Any]) -> dict[str, Any] | None:
    item_id = str(item.get("id") or "")
    if not item_id:
        return None
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    content_parts = []
    for key, value in data.items():
        if value not in (None, "", [], {}):
            content_parts.append(f"{key}: {value}")
    content_text = "\n".join(content_parts)
    if not content_text.strip():
        return None
    return {
        "target": f"{category_id}/{item_id}",
        "source_type": "knowledge",
        "category": category_id,
        "content_text": content_text[:800],
    }


def _serialize_rag_experience(exp: dict[str, Any]) -> dict[str, Any] | None:
    exp_id = str(exp.get("experience_id") or "")
    if not exp_id:
        return None
    parts = [
        str(exp.get("summary") or ""),
        str(exp.get("question") or ""),
        str(exp.get("reply_text") or ""),
        str(exp.get("evidence_excerpt") or ""),
    ]
    content_text = "\n".join(p for p in parts if p.strip())
    if not content_text.strip():
        return None
    return {
        "target": exp_id,
        "source_type": "rag",
        "category": str(exp.get("source") or "rag_experience"),
        "content_text": content_text[:800],
    }


def _batch_entries(entries: list[dict[str, Any]], max_size: int) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    for i in range(0, len(entries), max_size):
        batches.append(entries[i : i + max_size])
    return batches


def _call_llm_audit_batch(batch: list[dict[str, Any]], rag_only_mode: bool = False) -> list[dict[str, Any]]:
    api_key = read_secret("DEEPSEEK_API_KEY")
    if not api_key:
        return []
    base_url = resolve_deepseek_base_url(read_secret_fn=read_secret)
    model = resolve_deepseek_tier_model(tier="flash", read_secret_fn=read_secret)

    scope_hint = "AI经验池" if rag_only_mode else "知识库条目和AI经验池"
    system_content = (
        "你是微信AI客服知识库的质量审计专家。你的任务是从提供的条目中找出问题。"
        "你必须只输出合法的JSON对象，不要输出markdown代码块或其他格式。"
    )

    user_prompt = {
        "task": f"请审计以下{scope_hint}，找出语义重复、逻辑错误和场景不适用的条目。",
        "instructions": [
            "1. 语义重复：不同条目表达同一业务含义，建议合并（merge）。",
            "2. 逻辑错误：价格自相矛盾、政策前后冲突、话术与商品属性明显不符，建议删除（delete）或修改（review）。",
            "3. 场景不适用：内容与当前业务场景明显不匹配（如二手车业务出现'7天无理由退货'），建议删除（delete）或修改（review）。",
            "4. AI经验池质量问题：AI经验池与正式知识严重冲突或本身质量极低，建议删除（delete）或 review。",
            "只对确实有问题且你有信心的条目输出；不要编造问题。",
            "每条finding必须包含 type、severity、involved_targets、reason、action。",
        ],
        "response_format": {
            "findings": [
                {
                    "type": "semantic_duplicate|logic_error|inappropriate|rag_quality_low",
                    "severity": "warning|error",
                    "involved_targets": ["条目标识符，如 products/item_a 或 rag_exp_xxx"],
                    "reason": "详细说明为什么认为有问题",
                    "action": "merge|delete|review",
                }
            ]
        },
        "entries": [
            {
                "target": e["target"],
                "source_type": e["source_type"],
                "category": e["category"],
                "content": e["content_text"],
            }
            for e in batch
        ],
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
        ],
        "temperature": 0.1,
        "max_tokens": resolve_deepseek_max_tokens(LLM_MAX_TOKENS, read_secret_fn=read_secret),
        "response_format": {"type": "json_object"},
    }
    apply_llm_reasoning_effort(payload, tier="flash", read_secret_fn=read_secret)

    request = urllib.request.Request(
        url=base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )

    try:
        with llm_urlopen(request, timeout=resolve_deepseek_timeout(120, read_secret_fn=read_secret)) as response:
            raw = response.read().decode("utf-8")
        data = json.loads(raw or "{}")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return []

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    parsed = _parse_json_object(str(content or ""))
    if not isinstance(parsed, dict):
        return []

    findings = parsed.get("findings")
    if not isinstance(findings, list):
        return []

    validated: list[dict[str, Any]] = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        finding_type = str(f.get("type") or "").strip()
        if finding_type not in {"semantic_duplicate", "logic_error", "inappropriate", "rag_quality_low"}:
            continue
        action = str(f.get("action") or "").strip()
        if action not in {"merge", "delete", "review"}:
            action = "review"
        targets = [str(t) for t in f.get("involved_targets", []) if str(t).strip()]
        if not targets:
            continue
        validated.append({
            "type": finding_type,
            "severity": "error" if finding_type in {"logic_error", "inappropriate"} else "warning",
            "involved_targets": targets,
            "reason": str(f.get("reason") or "").strip(),
            "action": action,
        })
    return validated


def _finding_to_issue(finding: dict[str, Any]) -> dict[str, Any]:
    finding_type = finding["type"]
    action = finding["action"]
    targets = finding["involved_targets"]
    reason = finding["reason"]

    code_map = {
        "semantic_duplicate": "llm_duplicate_semantic",
        "logic_error": "llm_logic_error",
        "inappropriate": "llm_inappropriate",
        "rag_quality_low": "llm_rag_quality_low",
    }
    title_map = {
        "semantic_duplicate": "LLM：语义重复",
        "logic_error": "LLM：逻辑错误",
        "inappropriate": "LLM：场景不适用",
        "rag_quality_low": "LLM：AI经验池质量低",
    }

    primary_target = targets[0]
    target_labels = ", ".join(targets)

    suggestions: list[dict[str, str]] = []
    if action == "merge":
        suggestions.append({
            "title": "建议合并",
            "detail": "多条知识表达同一含义，建议保留信息最完整的一条，将其余归档。",
            "level": "warning",
        })
    elif action == "delete":
        suggestions.append({
            "title": "建议删除",
            "detail": "该条目存在明显错误或不适用，建议归档或废弃。",
            "level": "danger",
        })
    else:
        suggestions.append({
            "title": "建议人工复核",
            "detail": "LLM认为该条目可能存在问题，请人工确认后决定处理方式。",
            "level": "warning",
        })

    return {
        "code": code_map.get(finding_type, "llm_audit_finding"),
        "severity": finding["severity"],
        "title": title_map.get(finding_type, "LLM审计发现"),
        "detail": reason,
        "target": primary_target,
        "target_label": target_labels,
        "repairable": False,
        "action_type": action,
        "involved_targets": targets,
        "llm_reasoning": reason,
        "suggestions": suggestions,
    }


def _target_set_key(targets: list[str]) -> str | None:
    if not targets:
        return None
    return "|".join(sorted(set(str(t) for t in targets)))


def _parse_json_object(value: str) -> dict[str, Any] | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def audit_single_rag_experience(record: dict[str, Any]) -> dict[str, Any] | None:
    """Lightweight LLM quality gate for a single AI experience pool item at creation time.

    Returns {"action": "keep"|"delete", "reason": "..."} or None on failure.
    The caller should treat None as "keep" to avoid blocking the hot path.
    """
    api_key = read_secret("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    base_url = resolve_deepseek_base_url(read_secret_fn=read_secret)
    model = resolve_deepseek_tier_model(tier="flash", read_secret_fn=read_secret)

    content_parts = [
        str(record.get("summary") or ""),
        str(record.get("question") or ""),
        str(record.get("reply_text") or ""),
        str(record.get("evidence_excerpt") or ""),
    ]
    content_text = "\n".join(p for p in content_parts if p.strip())
    if not content_text.strip():
        return {"action": "delete", "reason": "内容为空，无业务价值"}

    source_type = record.get("source_type") or ""
    reply_text = str(record.get("reply_text") or "")

    # Detect structural patterns
    has_customer_dialog = "customer_message" in reply_text and "service_reply" in reply_text
    is_product_catalog_copy = (
        "name:" in reply_text and "sku:" in reply_text and "price:" in reply_text and not has_customer_dialog
    )
    is_system_log_only = (
        not has_customer_dialog
        and not is_product_catalog_copy
        and (reply_text.startswith("[") or "self:" in reply_text or "system:" in reply_text)
        and len(reply_text) < 200
    )

    system_content = (
        "你是微信AI客服AI经验池的质量守门员。你的任务是判断一条刚生成的AI经验池是否应该被保留。"
        "你必须只输出合法的JSON对象，不要输出markdown代码块或其他格式。"
    )

    instructions = [
        "判断标准（按优先级）：",
        "1. 【真实客服对话】如果 reply_text 中包含 customer_message 和 service_reply 的问答结构，"
        "且内容是真实的客户咨询（如车型推荐、省油需求、购车预算、售后问题等），建议 keep。"
        "这类对话即使表达不够 polished，只要信息有价值就应该保留。",
        "2. 【纯商品目录复制】如果内容只是从商品库/知识库复制的结构化数据（包含 name/sku/price/category 等字段），"
        "且没有客户互动问答，建议 delete。这类内容和正式知识库完全重复，不需要作为AI经验池保留。",
        "3. 【纯系统提示/日志】如果内容只是系统时间戳、self: 自动回复记录、打招呼、转账通知等内部日志，"
        "没有客户实际提问和客服有意义的回复，建议 delete。",
        "4. 【纯噪音】如果内容是纯噪音、测试消息、无意义重复、只有表情，建议 delete。",
        "5. 【自相矛盾】如果内容自相矛盾、与常识明显冲突、信息严重截断到无法理解，建议 delete。",
        "6. 【边缘情况】对表达不够完善但有信息价值的（如车型推荐但缺少细节），建议 keep（不要过度删除）。",
        "输出格式：{\"action\": \"keep|delete\", \"reason\": \"简短说明\"}",
    ]

    user_prompt = {
        "task": "请审计这条AI经验池，判断它是否应该被保留。",
        "content_analysis": {
            "has_customer_dialog": has_customer_dialog,
            "is_product_catalog_copy": is_product_catalog_copy,
            "is_system_log_only": is_system_log_only,
        },
        "instructions": instructions,
        "experience": {
            "source": record.get("source"),
            "source_type": source_type,
            "summary": str(record.get("summary") or "")[:400],
            "question": str(record.get("question") or "")[:300],
            "reply_text": str(record.get("reply_text") or "")[:600],
            "evidence_excerpt": str(record.get("evidence_excerpt") or "")[:400],
        },
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
        ],
        "temperature": 0.1,
        "max_tokens": resolve_deepseek_max_tokens(800, read_secret_fn=read_secret),
        "response_format": {"type": "json_object"},
    }
    apply_llm_reasoning_effort(payload, tier="flash", read_secret_fn=read_secret)

    request = urllib.request.Request(
        url=base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )

    try:
        with llm_urlopen(request, timeout=resolve_deepseek_timeout(30, read_secret_fn=read_secret)) as response:
            raw = response.read().decode("utf-8")
        data = json.loads(raw or "{}")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    parsed = _parse_json_object(str(content or ""))
    if not isinstance(parsed, dict):
        return None

    action = str(parsed.get("action") or "").strip().lower()
    if action not in {"keep", "delete"}:
        return None
    return {
        "action": action,
        "reason": str(parsed.get("reason") or "").strip(),
    }


def _choose_discard_target(targets: list[str], experiences: list[dict[str, Any]]) -> str | None:
    """Given duplicate targets, choose which AI experience pool item to discard.

    Prefer keeping the one with higher reply_count (more used) or older
    creation time (more stable). Discard the other(s).
    """
    exp_map = {str(e.get("experience_id") or ""): e for e in experiences}
    candidates = [t for t in targets if t.startswith("rag_exp_") and t in exp_map]
    if not candidates:
        return None
    if len(candidates) == 1:
        return None  # Only one RAG target, don't discard

    def sort_key(exp_id: str) -> tuple[int, str]:
        e = exp_map.get(exp_id, {})
        usage = e.get("usage", {}) or {}
        reply_count = 0
        try:
            reply_count = int(usage.get("reply_count", 0) or 0)
        except (TypeError, ValueError):
            pass
        created_at = str(e.get("created_at") or "")
        return (-reply_count, created_at)

    candidates_sorted = sorted(candidates, key=sort_key)
    # Keep the first (highest reply_count / oldest), discard the rest
    to_discard = candidates_sorted[1:]
    return to_discard[0] if to_discard else None
