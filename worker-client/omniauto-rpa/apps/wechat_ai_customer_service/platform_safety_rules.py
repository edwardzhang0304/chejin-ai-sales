"""Visible platform safety-rule loading for WeChat customer-service runtime."""

from __future__ import annotations

import copy
import json
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import shared_runtime_snapshot_path


def load_platform_safety_rules(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    del settings
    cloud = load_platform_safety_rules_from_cloud()
    if cloud is not None:
        item = normalize_platform_safety_rules(cloud)
        item["_path"] = "cloud://shared_snapshot/policy_bundle/merged/platform_safety_rules"
        return {
            "ok": True,
            "path": item["_path"],
            "source": "cloud_shared_snapshot",
            "readonly": True,
            "item": item,
        }
    item = empty_rules()
    item["_path"] = "cloud://shared_snapshot/policy_bundle/merged/platform_safety_rules"
    return {
        "ok": False,
        "path": item["_path"],
        "source": "cloud_shared_snapshot",
        "readonly": True,
        "error": "platform_safety_rules_cloud_snapshot_required",
        "item": item,
    }


def save_platform_safety_rules(payload: dict[str, Any], settings: dict[str, Any] | None = None) -> dict[str, Any]:
    del payload, settings
    cloud = load_platform_safety_rules_from_cloud()
    item = normalize_platform_safety_rules(cloud) if cloud is not None else empty_rules()
    item["_path"] = "cloud://shared_snapshot/policy_bundle/merged/platform_safety_rules"
    return {
        "ok": False,
        "path": item["_path"],
        "source": "cloud_shared_snapshot",
        "readonly": True,
        "error": "platform_safety_rules_managed_by_cloud",
        "item": item,
    }


def normalize_platform_safety_rules(payload: dict[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(payload)
    item.setdefault("schema_version", 1)
    item.setdefault("title", "平台底线规则")
    item.setdefault("description", "所有客户通用的自动回复安全边界。行业专属规则不应写在这里。")
    item.setdefault("prompt_rules", [])
    item.setdefault("guard_terms", {})
    item["prompt_rules"] = normalize_prompt_rules(item.get("prompt_rules"))
    item["guard_terms"] = normalize_guard_terms(item.get("guard_terms"))
    return item


def empty_rules() -> dict[str, Any]:
    return normalize_platform_safety_rules({"schema_version": 1, "prompt_rules": [], "guard_terms": {}})


def normalize_prompt_rules(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rules: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            continue
        instruction = str(raw.get("instruction") or raw.get("description") or "").strip()
        if not instruction:
            continue
        rules.append(
            {
                "id": str(raw.get("id") or f"rule_{index + 1}").strip(),
                "title": str(raw.get("title") or raw.get("id") or f"规则 {index + 1}").strip(),
                "description": str(raw.get("description") or "").strip(),
                "instruction": instruction,
                "enabled": raw.get("enabled", True) is not False,
            }
        )
    return rules


def normalize_guard_terms(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key, raw in value.items():
        if isinstance(raw, list):
            normalized[str(key)] = [str(item).strip() for item in raw if str(item).strip()]
        elif isinstance(raw, dict):
            normalized[str(key)] = normalize_guard_terms(raw)
    return normalized


def enabled_prompt_instructions(rules: dict[str, Any]) -> list[str]:
    return [
        str(item.get("instruction") or "").strip()
        for item in rules.get("prompt_rules", []) or []
        if isinstance(item, dict) and item.get("enabled", True) is not False and str(item.get("instruction") or "").strip()
    ]


def guard_term_set(rules: dict[str, Any], name: str) -> set[str]:
    terms = rules.get("guard_terms", {}) if isinstance(rules.get("guard_terms"), dict) else {}
    value = terms.get(name, [])
    if not isinstance(value, list):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def load_platform_safety_rules_from_cloud() -> dict[str, Any] | None:
    path = shared_runtime_snapshot_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    policy_bundle = payload.get("policy_bundle") if isinstance(payload.get("policy_bundle"), dict) else {}
    merged = policy_bundle.get("merged") if isinstance(policy_bundle.get("merged"), dict) else {}
    rules = merged.get("platform_safety_rules") if isinstance(merged.get("platform_safety_rules"), dict) else None
    return copy.deepcopy(rules) if isinstance(rules, dict) else None
