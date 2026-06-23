"""Focused checks for authority-gated AI experience pool behavior."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
WORKFLOWS_ROOT = APP_ROOT / "workflows"
if str(WORKFLOWS_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKFLOWS_ROOT))
ADAPTERS_ROOT = APP_ROOT / "adapters"
if str(ADAPTERS_ROOT) not in sys.path:
    sys.path.insert(0, str(ADAPTERS_ROOT))

os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")

from apps.wechat_ai_customer_service.admin_backend.services.rag_experience_governance import (  # noqa: E402
    attach_governance,
    governance_allows_retrieval,
    resolve_rag_experience_governance,
)
from apps.wechat_ai_customer_service.workflows.evidence_authority import (  # noqa: E402
    can_authorize_reply_content,
)
from apps.wechat_ai_customer_service.workflows.rag_experience_store import (  # noqa: E402
    experience_is_reference_candidate,
    experience_is_retrievable,
    with_quality,
)
from apps.wechat_ai_customer_service.workflows.rag_layer import (  # noqa: E402
    RagService,
    build_index_entry,
    runtime_rag_entry_allowed,
)
from apps.wechat_ai_customer_service.workflows.customer_service_brain import compact_ai_experience_pool_for_prompt  # noqa: E402
from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import compact_knowledge_pack, compact_rag_evidence  # noqa: E402
from apps.wechat_ai_customer_service.workflows.style_memory_store import sanitize_style_reply  # noqa: E402


def main() -> int:
    checks = [
        check_ai_experience_pool_cannot_authorize_reply_content,
        check_governed_experience_never_becomes_retrievable,
        check_rag_index_rebuild_excludes_ai_experience_pool_and_raw_uploads,
        check_runtime_search_filters_legacy_ai_experience_pool_entries,
        check_reply_evidence_excludes_ai_experience_pool_hits,
        check_ai_experience_pool_reference_hits_are_auxiliary_only,
        check_ai_experience_pool_reference_search_hits_without_content_authority,
        check_ai_experience_pool_runtime_gate_drops_noise_and_risky_commitments,
        check_ai_experience_pool_reference_sanitizes_numbers_without_dropping_scenario,
        check_ai_experience_pool_prompt_exposes_reference_without_authority,
        check_ai_experience_pool_trace_explains_reference_and_drop_reasons,
        check_style_examples_are_fact_sanitized,
        check_legacy_rag_experience_wording_is_gone,
    ]
    results: list[dict[str, Any]] = []
    for check in checks:
        try:
            results.append({"name": check.__name__, "ok": bool(check())})
        except Exception as exc:
            results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
    failures = [item for item in results if not item.get("ok")]
    payload = {"ok": not failures, "count": len(results), "failures": failures, "results": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


def check_ai_experience_pool_cannot_authorize_reply_content() -> bool:
    ai_pool_item = {"source_type": "cleaned_real_chat_pack", "category": "chats", "status": "active"}
    formal_item = {"source_type": "manual", "category": "policies", "status": "active"}
    return (
        can_authorize_reply_content(ai_pool_item, category_id="chats", source_type="cleaned_real_chat_pack") is False
        and can_authorize_reply_content(formal_item, category_id="policies", source_type="manual") is True
    )


def governed_experience() -> dict[str, Any]:
    return {
        "experience_id": "rag_exp_authority_probe",
        "tenant_id": "authority_probe",
        "status": "active",
        "source": "manual_admin",
        "source_type": "manual",
        "category": "policies",
        "summary": "客户问置换流程，客服收集车型、年份、公里数、城市和照片。",
        "question": "置换怎么走？",
        "reply_text": "置换可以先做大概区间，请发车型、上牌年份、公里数、城市和照片。",
        "experience_review": {"status": "auto_kept"},
        "reviewed_by_user": False,
        "quality": {"band": "high", "retrieval_allowed": True},
        "rag_hit": {"score": 0.93, "source_type": "manual", "category": "policies"},
    }


def check_governed_experience_never_becomes_retrievable() -> bool:
    item = governed_experience()
    governance = resolve_rag_experience_governance(item)
    governed = attach_governance(item)
    return (
        governance.get("effective_state") == "kept_experience"
        and governance.get("retrieval_allowed") is False
        and governance_allows_retrieval(governed) is False
        and experience_is_retrievable(with_quality(governed)) is False
    )


def legacy_ai_pool_chunk() -> dict[str, Any]:
    return {
        "chunk_id": "chunk_ai_pool_legacy",
        "source_id": "rag_exp_legacy",
        "tenant_id": "authority_probe",
        "layer": "rag_experience",
        "source_type": "rag_experience",
        "category": "rag_experience",
        "product_id": "",
        "source_path": "ai_pool.json",
        "chunk_index": 0,
        "text": "置换流程：先收车型、年份、公里数、城市和照片。",
        "char_count": 24,
        "status": "active",
    }


def raw_upload_chunk() -> dict[str, Any]:
    return {
        "chunk_id": "chunk_raw_upload",
        "source_id": "source_raw_upload",
        "tenant_id": "authority_probe",
        "layer": "tenant",
        "source_type": "upload",
        "category": "chats",
        "product_id": "",
        "source_path": "raw_upload.json",
        "chunk_index": 0,
        "text": "历史聊天样本：客户问预算，客服推荐某台车。",
        "char_count": 24,
        "status": "active",
    }


def useful_experience_chunk() -> dict[str, Any]:
    return {
        "chunk_id": "chunk_ai_pool_reference",
        "source_id": "rag_exp_reference",
        "tenant_id": "authority_probe",
        "layer": "rag_experience",
        "source_type": "real_chat_style",
        "category": "chats",
        "product_id": "",
        "source_path": "experiences.json",
        "chunk_index": 1,
        "text": "客户想给家人挑一个不太贵、好上手的商品，客服先确认预算和主要用途，再给两个方向做选择。",
        "char_count": 42,
        "status": "active",
        "score": 0.91,
    }


def noisy_experience_chunk() -> dict[str, Any]:
    return {
        "chunk_id": "chunk_ai_pool_noise",
        "source_id": "rag_exp_noise",
        "tenant_id": "authority_probe",
        "layer": "rag_experience",
        "source_type": "raw_wechat_private",
        "category": "chats",
        "text": "请求添加你为朋友，以上是打招呼的内容",
        "status": "active",
        "score": 0.85,
    }


def risky_experience_chunk() -> dict[str, Any]:
    return {
        "chunk_id": "chunk_ai_pool_risky",
        "source_id": "rag_exp_risky",
        "tenant_id": "authority_probe",
        "layer": "rag_experience",
        "source_type": "real_chat_style",
        "category": "chats",
        "text": "客户问价格时，客服可以说保证最低价，今天一定能办成。",
        "status": "active",
        "score": 0.88,
    }


def budget_experience_chunk() -> dict[str, Any]:
    return {
        "chunk_id": "chunk_ai_pool_budget",
        "source_id": "rag_exp_budget",
        "tenant_id": "authority_probe",
        "layer": "rag_experience",
        "source_type": "real_chat_style",
        "category": "chats",
        "text": "客户预算9万以内，给老婆买好停车的商品，客服先按预算、使用人和倒车影像偏好筛两个方向。",
        "status": "active",
        "score": 0.9,
    }


def formal_chunk() -> dict[str, Any]:
    return {
        "chunk_id": "chunk_formal_policy",
        "source_id": "source_formal_policy",
        "tenant_id": "authority_probe",
        "layer": "tenant",
        "source_type": "manual",
        "category": "policies",
        "product_id": "",
        "source_path": "formal_policy.json",
        "chunk_index": 0,
        "text": "置换流程：请客户提供车型、上牌年份、公里数、所在城市和车辆照片。",
        "char_count": 35,
        "status": "active",
    }


def check_rag_index_rebuild_excludes_ai_experience_pool_and_raw_uploads() -> bool:
    with tempfile.TemporaryDirectory() as root:
        root_path = Path(root)
        chunks_root = root_path / "chunks"
        chunks_root.mkdir(parents=True, exist_ok=True)
        payload = {"chunks": [legacy_ai_pool_chunk(), raw_upload_chunk(), formal_chunk()]}
        (chunks_root / "source_authority_probe.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        service = RagService(
            tenant_id="authority_probe",
            sources_root=root_path / "sources",
            chunks_root=chunks_root,
            index_root=root_path / "index",
            cache_root=root_path / "cache",
        )
        result = service.rebuild_index()
        index = service.load_index()
        entries = index.get("entries", []) or []
        return (
            result.get("entry_count") == 1
            and len(entries) == 1
            and entries[0].get("source_id") == "source_formal_policy"
            and runtime_rag_entry_allowed(entries[0]) is True
        )


def check_runtime_search_filters_legacy_ai_experience_pool_entries() -> bool:
    with tempfile.TemporaryDirectory() as root:
        root_path = Path(root)
        service = RagService(
            tenant_id="authority_probe",
            sources_root=root_path / "sources",
            chunks_root=root_path / "chunks",
            index_root=root_path / "index",
            cache_root=root_path / "cache",
        )
        service.ensure_dirs()
        high_risk_terms: list[str] = []
        entries = [
            build_index_entry(legacy_ai_pool_chunk(), high_risk_terms=high_risk_terms),
            build_index_entry(formal_chunk(), high_risk_terms=high_risk_terms),
        ]
        (root_path / "index" / "index.json").write_text(
            json.dumps({"schema_version": 1, "tenant_id": "authority_probe", "entries": entries}, ensure_ascii=False),
            encoding="utf-8",
        )
        hits = service.search("置换流程怎么走", limit=5).get("hits", [])
        return len(hits) == 1 and hits[0].get("source_id") == "source_formal_policy"


def check_reply_evidence_excludes_ai_experience_pool_hits() -> bool:
    result = compact_rag_evidence(
        {
            "enabled": True,
            "ok": True,
            "hits": [legacy_ai_pool_chunk(), raw_upload_chunk(), formal_chunk()],
        },
        max_hits=5,
        max_text_chars=200,
    )
    hits = result.get("hits", []) or []
    return (
        len(hits) == 1
        and hits[0].get("source_id") == "source_formal_policy"
        and result.get("excluded_hit_count") == 2
    )


def check_ai_experience_pool_reference_hits_are_auxiliary_only() -> bool:
    result = compact_rag_evidence(
        {
            "enabled": True,
            "ok": True,
            "hits": [useful_experience_chunk(), formal_chunk()],
        },
        max_hits=5,
        max_text_chars=200,
    )
    reference_hits = result.get("ai_experience_hits", []) or []
    content_hits = result.get("hits", []) or []
    return (
        len(content_hits) == 1
        and content_hits[0].get("source_id") == "source_formal_policy"
        and len(reference_hits) == 1
        and reference_hits[0].get("runtime_usage") == "reference_experience"
        and reference_hits[0].get("can_authorize_reply_content") is False
        and result.get("reference_hit_count") == 1
        and result.get("excluded_hit_count") == 1
    )


def check_ai_experience_pool_reference_search_hits_without_content_authority() -> bool:
    with tempfile.TemporaryDirectory() as root:
        root_path = Path(root)
        experience_root = root_path / "rag_experience"
        experience_root.mkdir(parents=True, exist_ok=True)
        record = {
            "experience_id": "rag_exp_reference_search",
            "tenant_id": "authority_probe",
            "status": "active",
            "source": "real_chat_style",
            "summary": "客户给家人挑不太贵、好上手的商品，客服先确认预算和主要用途，再给两个方向做选择。",
            "question": "给家人挑不太贵、好上手的商品怎么回复？",
            "reply_text": "可以先按预算和主要用途筛两个方向，再让客户二选一。",
            "experience_review": {"status": "auto_kept"},
            "quality": {
                "band": "high",
                "signals": {
                    "quality_allows_retrieval": True,
                    "review_allows_retrieval": True,
                },
            },
            "rag_hit": {"score": 0.66, "source_type": "manual", "category": "policies"},
        }
        (experience_root / "experiences.json").write_text(json.dumps([record], ensure_ascii=False), encoding="utf-8")
        service = RagService(
            tenant_id="authority_probe",
            sources_root=root_path / "sources",
            chunks_root=root_path / "chunks",
            index_root=root_path / "index",
            cache_root=root_path / "cache",
        )
        reference_result = service.search_experience_references("家人 不太贵 好上手 两个方向", limit=3)
        normal_hits = service.search("家人 不太贵 好上手 两个方向", limit=3).get("hits", [])
        evidence = service.evidence("家人 不太贵 好上手 两个方向", limit=3)
        compact = compact_rag_evidence(evidence, max_hits=3, max_text_chars=200)
        reference_hits = compact.get("ai_experience_hits", []) or []
        return (
            experience_is_reference_candidate(record) is True
            and reference_result.get("rag_can_authorize") is False
            and len(reference_result.get("hits", []) or []) == 1
            and normal_hits == []
            and len(reference_hits) == 1
            and compact.get("hits") == []
            and reference_hits[0].get("can_authorize_reply_content") is False
        )


def check_ai_experience_pool_runtime_gate_drops_noise_and_risky_commitments() -> bool:
    result = compact_rag_evidence(
        {
            "enabled": True,
            "ok": True,
            "hits": [noisy_experience_chunk(), risky_experience_chunk(), useful_experience_chunk()],
        },
        max_hits=5,
        max_text_chars=200,
    )
    reference_hits = result.get("ai_experience_hits", []) or []
    ids = [item.get("chunk_id") for item in reference_hits]
    return ids == ["chunk_ai_pool_reference"] and result.get("excluded_hit_count") == 3


def check_ai_experience_pool_reference_sanitizes_numbers_without_dropping_scenario() -> bool:
    result = compact_rag_evidence(
        {
            "enabled": True,
            "ok": True,
            "hits": [budget_experience_chunk()],
        },
        max_hits=5,
        max_text_chars=200,
    )
    reference_hits = result.get("ai_experience_hits", []) or []
    text = str(reference_hits[0].get("text") if reference_hits else "")
    return (
        len(reference_hits) == 1
        and reference_hits[0].get("runtime_usage") == "reference_experience"
        and "9万" not in text
        and "{金额}" in text
        and "老婆" in text
        and "倒车影像" in text
    )


def check_ai_experience_pool_prompt_exposes_reference_without_authority() -> bool:
    compact = compact_knowledge_pack(
        "帮我推荐一个不太贵、好上手的商品",
        {
            "intent_tags": ["catalog"],
            "evidence": {"products": [], "faq": [], "policies": {}, "product_scoped": [], "style_examples": []},
            "rag_evidence": {"enabled": True, "ok": True, "hits": [useful_experience_chunk()]},
            "safety": {},
        },
        max_rag_hits=3,
        max_rag_text_chars=200,
        max_catalog_candidates=2,
    )
    ai_pool = compact.get("ai_experience_pool", {})
    prompt_payload = compact_ai_experience_pool_for_prompt(ai_pool)
    prompt_hits = prompt_payload.get("hits", []) or []
    return (
        compact.get("rag_evidence", {}).get("hits") == []
        and ai_pool.get("can_authorize_reply_content") is False
        and ai_pool.get("reference_hit_count") == 1
        and len(prompt_hits) == 1
        and prompt_hits[0].get("runtime_usage") == "reference_experience"
        and prompt_payload.get("can_authorize_reply_content") is False
    )


def check_ai_experience_pool_trace_explains_reference_and_drop_reasons() -> bool:
    compact = compact_knowledge_pack(
        "客户想给老婆挑一台不太贵、好上手、有倒车影像的代步车",
        {
            "intent_tags": ["catalog"],
            "evidence": {"products": [], "faq": [], "policies": {}, "product_scoped": [], "style_examples": []},
            "rag_evidence": {
                "enabled": True,
                "ok": True,
                "hits": [noisy_experience_chunk(), risky_experience_chunk(), useful_experience_chunk()],
            },
            "safety": {},
        },
        max_rag_hits=3,
        max_rag_text_chars=200,
        max_catalog_candidates=2,
    )
    ai_pool = compact.get("ai_experience_pool", {})
    trace = ai_pool.get("trace", {}) if isinstance(ai_pool.get("trace"), dict) else {}
    prompt_payload = compact_ai_experience_pool_for_prompt(ai_pool)
    reference_ids = trace.get("reference_ids", []) or []
    reasons = trace.get("exclusion_reasons", {}) or {}
    trace_items = trace.get("items", []) or []
    return (
        ai_pool.get("can_authorize_reply_content") is False
        and ai_pool.get("reference_hit_count") == 1
        and ai_pool.get("excluded_hit_count") == 3
        and reference_ids == ["chunk_ai_pool_reference"]
        and reasons.get("runtime_noise_or_metadata") == 1
        and reasons.get("risky_fact_or_commitment_like_text") == 1
        and reasons.get("complete_non_authoritative_experience") == 1
        and len(trace_items) == 3
        and all(item.get("can_authorize_reply_content") is False for item in trace_items)
        and prompt_payload.get("reference_ids") == ["chunk_ai_pool_reference"]
        and isinstance(prompt_payload.get("exclusion_reasons"), dict)
    )


def check_style_examples_are_fact_sanitized() -> bool:
    text = sanitize_style_reply("客户电话13812345678，报价17.8万，5万公里，保证无事故。")
    return (
        "13812345678" not in text
        and "17.8万" not in text
        and "5万公里" not in text
        and "{手机号}" in text
        and "{价格}" in text
        and "{公里数}" in text
        and "以检测报告为准" in text
    )


def check_legacy_rag_experience_wording_is_gone() -> bool:
    old_prefix = "R" + "AG"
    patterns = (
        old_prefix + "经验",
        old_prefix + " 经验",
        old_prefix + "经验池",
        old_prefix + " 经验池",
        old_prefix + "生成",
        old_prefix + "参考",
        old_prefix + " 参考",
        old_prefix + "检索",
    )
    allowed_parts = ("/docs/history/", "\\docs\\history\\", "/runtime/", "\\runtime\\", "/logs/", "\\logs\\")
    roots = [
        APP_ROOT / "admin_backend",
        APP_ROOT / "workflows",
        APP_ROOT / "adapters",
        APP_ROOT / "scripts",
        APP_ROOT / "tests",
        APP_ROOT / "vps_admin",
        APP_ROOT / "docs",
        APP_ROOT / "README.md",
        APP_ROOT / "data" / "shared_knowledge",
    ]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        if not root.exists():
            continue
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".py", ".js", ".html", ".css", ".md", ".json"}
        )
    offenders = []
    for path in files:
        if path.resolve() == Path(__file__).resolve():
            continue
        path_text = str(path)
        if any(part in path_text for part in allowed_parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in patterns:
            if pattern in text:
                offenders.append(f"{path_text}: {pattern}")
    if offenders:
        raise AssertionError("; ".join(offenders[:20]))
    return True


if __name__ == "__main__":
    raise SystemExit(main())
