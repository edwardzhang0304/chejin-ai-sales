"""Display badges for review candidates."""

from __future__ import annotations

from typing import Any


Badge = dict[str, str]

TEXT = {
    "complete": "\u5df2\u5b8c\u5584",
    "needs_more_info": "\u5f85\u5b8c\u5584",
    "rag_generated": "AI\u7ecf\u9a8c\u6c60\u751f\u6210",
    "wechat_private": "\u5fae\u4fe1\u79c1\u804a",
    "wechat_group": "\u5fae\u4fe1\u7fa4\u804a",
    "wechat_file_transfer": "\u6587\u4ef6\u4f20\u8f93\u52a9\u624b",
    "upload": "\u6587\u4ef6\u4e0a\u4f20",
    "ai_generator": "AI\u751f\u6210\u5668",
    "warnings": "\u9700\u4eba\u5de5\u786e\u8ba4",
    "duplicate": "\u7591\u4f3c\u91cd\u590d",
    "can_promote": "\u53ef\u664b\u5347",
    "ai_reference": "AI\u7ecf\u9a8c\u6c60",
    "rag_experience": "AI\u7ecf\u9a8c\u6c60",
    "recorder": "AI\u667a\u80fd\u8bb0\u5f55\u5458",
    "knowledge_learning": "\u77e5\u8bc6\u5f55\u5165\u4e0e\u5b66\u4e60",
    "wechat_group_channel": "\u5fae\u4fe1\u7fa4\u804a",
    "wechat_private_channel": "\u5fae\u4fe1\u79c1\u804a",
    "file_transfer_channel": "\u6587\u4ef6\u4f20\u8f93\u52a9\u624b",
    "upload_channel": "\u5bfc\u5165\u8d44\u6599",
    "ai_generator_channel": "AI\u751f\u6210\u5668",
    "other_source": "\u5176\u4ed6\u6765\u6e90",
    "unknown_source": "\u672a\u77e5\u6765\u6e90",
    "unmarked": "\u672a\u6807\u6ce8",
    "from_group": "\u6765\u81ea\u5fae\u4fe1\u7fa4\u804a",
    "from_file_transfer": "\u6765\u81ea\u6587\u4ef6\u4f20\u8f93\u52a9\u624b",
    "from_private": "\u6765\u81ea\u5fae\u4fe1\u79c1\u804a",
    "from_upload": "\u6765\u81ea\u5bfc\u5165\u8d44\u6599",
}


def enrich_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(candidate)
    enriched["display_badges"] = candidate_badges(candidate)
    enriched["source_summary"] = candidate_source_summary(candidate)
    enriched["primary_status"] = primary_status(candidate)
    enriched["can_promote"] = can_promote(candidate)
    return enriched


def candidate_badges(candidate: dict[str, Any]) -> list[Badge]:
    review = candidate.get("review") if isinstance(candidate.get("review"), dict) else {}
    intake = candidate.get("intake") if isinstance(candidate.get("intake"), dict) else {}
    proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {}
    source_types = candidate_source_types(candidate)
    source_path = source_path_text(candidate)
    detected_tags = [str(item) for item in candidate.get("detected_tags", []) or []]
    badges: list[Badge] = []

    completeness = str(review.get("completeness_status") or intake.get("status") or "")
    if completeness == "ready":
        badges.append(badge("complete", TEXT["complete"], "ok"))
    elif completeness == "needs_more_info":
        badges.append(badge("needs_more_info", TEXT["needs_more_info"], "warning"))
    elif completeness:
        badges.append(badge("intake_status", completeness, "muted"))

    if "rag_experience" in source_types or review.get("rag_experience_id") or "rag_experience" in detected_tags:
        badges.append(badge("rag_generated", TEXT["rag_generated"], "info"))
    if source_types & {"wechat_private_chat", "raw_wechat_private"} or "wechat_private_chat" in detected_tags:
        badges.append(badge("wechat_private", TEXT["wechat_private"], "info"))
    if source_types & {"wechat_group_chat", "raw_wechat_group"} or "wechat_group_chat" in detected_tags:
        badges.append(badge("wechat_group", TEXT["wechat_group"], "info"))
    if "raw_wechat_file_transfer" in source_types or "wechat_file_transfer" in detected_tags:
        badges.append(badge("wechat_file_transfer", TEXT["wechat_file_transfer"], "info"))
    if source_types & {"raw_upload", "deepseek_upload_learning"} or "raw_inbox" in source_path:
        badges.append(badge("upload", TEXT["upload"], "muted"))
    if source_types & {"ai_generator", "generator"}:
        badges.append(badge("ai_generator", TEXT["ai_generator"], "info"))

    warnings = intake.get("warnings") or proposal.get("warnings") or []
    if warnings:
        badges.append(badge("warnings", TEXT["warnings"], "danger"))
    if review.get("duplicate") or review.get("duplicate_source"):
        badges.append(badge("duplicate", TEXT["duplicate"], "warning"))
    if can_promote(candidate):
        badges.append(badge("can_promote", TEXT["can_promote"], "ok"))
    return dedupe_badges(badges)


def candidate_source_summary(candidate: dict[str, Any]) -> dict[str, str]:
    source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
    review = candidate.get("review") if isinstance(candidate.get("review"), dict) else {}
    source_types = candidate_source_types(candidate)
    path = source_path_text(candidate)
    file_name = path.replace("\\", "/").split("/")[-1] if path else ""
    detail_id = str(source.get("conversation_id") or source.get("raw_batch_id") or "")

    if "rag_experience" in source_types or review.get("rag_experience_id"):
        origin = original_source_label(source_types, file_name, detail_id)
        experience_id = str(review.get("rag_experience_id") or source.get("rag_experience_id") or source.get("experience_id") or "")
        detail = " · ".join(part for part in (origin, experience_id) if part)
        return source_summary(TEXT["ai_reference"], TEXT["rag_experience"], detail)
    if source_types & {"raw_wechat_group", "wechat_group_chat"}:
        return source_summary(TEXT["recorder"], TEXT["wechat_group_channel"], detail_id)
    if "raw_wechat_file_transfer" in source_types:
        return source_summary(TEXT["recorder"], TEXT["file_transfer_channel"], detail_id)
    if source_types & {"raw_wechat_private", "wechat_private_chat"}:
        return source_summary(TEXT["recorder"], TEXT["wechat_private_channel"], detail_id)
    if source_types & {"raw_upload", "deepseek_upload_learning"} or "raw_inbox" in path or file_name:
        return source_summary(TEXT["knowledge_learning"], TEXT["upload_channel"], file_name)
    if source_types & {"ai_generator", "generator"}:
        return source_summary(TEXT["knowledge_learning"], TEXT["ai_generator_channel"], str(source.get("session_id") or ""))
    if source_types:
        return source_summary(TEXT["other_source"], next(iter(source_types)), file_name or detail_id)
    return source_summary(TEXT["unknown_source"], TEXT["unmarked"], file_name)


def candidate_source_types(candidate: dict[str, Any]) -> set[str]:
    source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
    types = {
        str(source.get("type") or ""),
        str(source.get("original_type") or source.get("original_source_type") or ""),
    }
    original_source = source.get("original_source")
    if isinstance(original_source, dict):
        types.add(str(original_source.get("type") or ""))
    return {item for item in types if item}


def source_path_text(candidate: dict[str, Any]) -> str:
    source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
    original_source = source.get("original_source") if isinstance(source.get("original_source"), dict) else {}
    return str(source.get("path") or source.get("source_path") or original_source.get("path") or "")


def original_source_label(source_types: set[str], file_name: str, detail_id: str) -> str:
    if source_types & {"raw_wechat_group", "wechat_group_chat"}:
        return with_detail(TEXT["from_group"], detail_id)
    if "raw_wechat_file_transfer" in source_types:
        return with_detail(TEXT["from_file_transfer"], detail_id)
    if source_types & {"raw_wechat_private", "wechat_private_chat"}:
        return with_detail(TEXT["from_private"], detail_id)
    if source_types & {"raw_upload", "deepseek_upload_learning"}:
        return with_detail(TEXT["from_upload"], file_name)
    return ""


def with_detail(label: str, detail: str) -> str:
    return f"{label} · {detail}" if detail else label


def source_summary(module: str, channel: str, detail: str = "") -> dict[str, str]:
    return {"module": module, "channel": channel, "detail": detail}


def primary_status(candidate: dict[str, Any]) -> str:
    intake = candidate.get("intake") if isinstance(candidate.get("intake"), dict) else {}
    review = candidate.get("review") if isinstance(candidate.get("review"), dict) else {}
    if str(intake.get("status") or review.get("completeness_status") or "") == "needs_more_info":
        return "needs_more_info"
    if str(intake.get("status") or review.get("completeness_status") or "") == "ready":
        return "ready"
    return str(review.get("status") or "pending")


def can_promote(candidate: dict[str, Any]) -> bool:
    intake = candidate.get("intake") if isinstance(candidate.get("intake"), dict) else {}
    review = candidate.get("review") if isinstance(candidate.get("review"), dict) else {}
    status = str(intake.get("status") or review.get("completeness_status") or "")
    if status == "needs_more_info":
        return False
    if review.get("applied") or str(review.get("status") or "pending") != "pending":
        return False
    return bool((candidate.get("proposal") or {}).get("formal_patch"))


def badge(key: str, label: str, tone: str) -> Badge:
    return {"key": key, "label": label, "tone": tone}


def dedupe_badges(items: list[Badge]) -> list[Badge]:
    seen: set[str] = set()
    result: list[Badge] = []
    for item in items:
        key = str(item.get("key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
