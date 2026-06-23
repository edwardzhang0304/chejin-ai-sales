"""Registry and binding resolution for recorder extract/export modules."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import runtime_app_root


SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
REMOTE_BINDING_SOURCE = "vps_sync"


BUILTIN_MODULES = [
    {
        "module_key": "raw_message_log_v1",
        "module_name": "通用原始消息导出V1",
        "module_type": "chat_extract_export",
        "status": "active",
        "version": "1.0.0",
        "config": {
            "description": "Generic baseline export that writes raw messages to a readable workbook.",
            "supports_content_types": ["text", "quote", "image", "system"],
        },
        "builtin": True,
    },
    {
        "module_key": "order_sheet_lab_v1",
        "module_name": "实验仪器订货表V1",
        "module_type": "chat_extract_export",
        "status": "active",
        "version": "1.0.0",
        "config": {
            "description": "LLM-first semantic extraction with structured guardrails, exports to order-sheet style workbook.",
            "date_output_mode": "YYYY-MM-DD",
            "allow_empty_cost_fields": True,
            "llm_enabled": True,
            "llm_max_rows_per_run": 12,
            "llm_repair_max_rows_per_run": 8,
            "llm_dynamic_budget_enabled": True,
            "llm_dynamic_budget_ratio": 0.35,
            "llm_dynamic_budget_max": 64,
            "llm_dynamic_repair_ratio": 0.25,
            "llm_dynamic_repair_max": 32,
            "llm_skip_strong_rule_rows": True,
            "llm_parallel_workers": 4,
            "llm_segmentation_enabled": True,
            "llm_segmentation_max_segments": 6,
            "llm_supplement_mode": "missing_core_fields_only",
            "extract_mode": "llm_first",
            "missing_quantity_strategy": "strict",
            "include_record_types_in_main_sheet": ["order_item", "gift_item"],
            "force_multi_sku_split_enabled": True,
            "force_multi_sku_min_skus": 2,
            "force_multi_order_signal_threshold": 2,
            "context_followup_enabled": True,
            "brand_aliases": ["津腾", "康为", "源叶", "麦克林", "施睿康", "甄选", "赛宁", "建成", "索莱宝", "毕得医药"],
            "brand_llm_inference_enabled": True,
            "brand_llm_inference_max_calls_per_run": 20,
            "brand_llm_inference_min_confidence": 0.62,
            "supported_content_types": ["text", "quote"],
        },
        "builtin": True,
    },
]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_id(value: str) -> str:
    text = SAFE_ID_RE.sub("_", str(value or "").strip())
    text = re.sub(r"_+", "_", text).strip("._-")
    if not text:
        return "id_unknown"
    return text[:120]


def binding_matches_tenant(binding: dict[str, Any], tenant_id: str) -> bool:
    bound_tenant = str(binding.get("tenant_id") or "").strip()
    if not bound_tenant:
        return True
    return bound_tenant == str(tenant_id or "")


class RecorderModuleRegistryService:
    """Manage module registry and account/global bindings."""

    def __init__(self) -> None:
        self.root = runtime_app_root() / "admin" / "recorder_modules"

    @property
    def registry_path(self) -> Path:
        return self.root / "module_registry.json"

    @property
    def bindings_path(self) -> Path:
        return self.root / "module_bindings.json"

    def list_modules(self, *, include_inactive: bool = True) -> list[dict[str, Any]]:
        registry = self._read_json(self.registry_path, default=[])
        modules = [item for item in registry if isinstance(item, dict)]
        modules = self._ensure_builtin_modules(modules)
        if not include_inactive:
            modules = [item for item in modules if str(item.get("status") or "active") == "active"]
        modules.sort(key=lambda item: str(item.get("module_key") or ""))
        return modules

    def upsert_module(self, payload: dict[str, Any]) -> dict[str, Any]:
        module_key = safe_id(str(payload.get("module_key") or payload.get("key") or ""))
        if not module_key:
            raise ValueError("module_key is required")
        current = self.list_modules(include_inactive=True)
        by_key = {str(item.get("module_key") or ""): item for item in current}
        existing = by_key.get(module_key, {})
        record = {
            "module_key": module_key,
            "module_name": str(payload.get("module_name") or payload.get("name") or existing.get("module_name") or module_key),
            "module_type": str(payload.get("module_type") or existing.get("module_type") or "chat_extract_export"),
            "status": str(payload.get("status") or existing.get("status") or "active"),
            "version": str(payload.get("version") or existing.get("version") or "1.0.0"),
            "config": payload.get("config") if isinstance(payload.get("config"), dict) else dict(existing.get("config") or {}),
            "builtin": bool(existing.get("builtin", False)),
            "created_at": str(existing.get("created_at") or now_iso()),
            "updated_at": now_iso(),
        }
        by_key[module_key] = record
        items = sorted(by_key.values(), key=lambda item: str(item.get("module_key") or ""))
        self._write_json(self.registry_path, items)
        return record

    def list_bindings(
        self,
        *,
        scope_type: str = "",
        scope_id: str = "",
        tenant_id: str = "",
        user_id: str = "",
    ) -> list[dict[str, Any]]:
        bindings = [
            item
            for item in self._read_json(self.bindings_path, default=[])
            if isinstance(item, dict) and str(item.get("scope_type") or "") in {"user", "tenant", "global"}
        ]
        if scope_type:
            bindings = [item for item in bindings if str(item.get("scope_type") or "") == scope_type]
        if scope_id:
            bindings = [item for item in bindings if str(item.get("scope_id") or "") == scope_id]
        if tenant_id:
            bindings = [item for item in bindings if str(item.get("tenant_id") or "") == tenant_id]
        if user_id:
            bindings = [item for item in bindings if str(item.get("user_id") or "") == user_id]
        bindings.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return bindings

    def upsert_binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        scope_type = str(payload.get("scope_type") or "").strip().lower()
        if scope_type not in {"user", "tenant", "global"}:
            raise ValueError("scope_type must be one of user|tenant|global")
        scope_id = str(payload.get("scope_id") or "").strip()
        if scope_type == "global":
            scope_id = "*"
        if not scope_id:
            raise ValueError("scope_id is required")
        module_key = str(payload.get("module_key") or "").strip()
        if not module_key:
            raise ValueError("module_key is required")
        module = self.get_module(module_key)
        if not module:
            raise ValueError(f"module not found: {module_key}")
        if str(module.get("status") or "active") != "active":
            raise ValueError(f"module is not active: {module_key}")
        user_id = str(payload.get("user_id") or scope_id if scope_type == "user" else "").strip()
        binding_id = safe_id(str(payload.get("binding_id") or f"{scope_type}_{scope_id}"))
        bindings = [item for item in self._read_json(self.bindings_path, default=[]) if isinstance(item, dict)]
        index = next((idx for idx, item in enumerate(bindings) if str(item.get("binding_id") or "") == binding_id), -1)
        existing = bindings[index] if index >= 0 else {}
        tenant_id = str(payload.get("tenant_id") or existing.get("tenant_id") or "").strip()
        if scope_type == "tenant" and not tenant_id:
            tenant_id = scope_id
        if scope_type == "global":
            tenant_id = ""
        record = {
            "binding_id": binding_id,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "module_key": module_key,
            "enabled": payload.get("enabled", existing.get("enabled", True)) is not False,
            "created_at": str(existing.get("created_at") or now_iso()),
            "updated_at": now_iso(),
        }
        if index >= 0:
            bindings[index] = record
        else:
            bindings.append(record)
        self._write_json(self.bindings_path, bindings)
        return record

    def delete_binding(self, binding_id: str) -> bool:
        bindings = [item for item in self._read_json(self.bindings_path, default=[]) if isinstance(item, dict)]
        filtered = [item for item in bindings if str(item.get("binding_id") or "") != binding_id]
        if len(filtered) == len(bindings):
            return False
        self._write_json(self.bindings_path, filtered)
        return True

    def sync_vps_snapshot(
        self,
        payload: dict[str, Any],
        *,
        tenant_id: str = "",
        user_id: str = "",
        source: str = REMOTE_BINDING_SOURCE,
    ) -> dict[str, Any]:
        snapshot = payload if isinstance(payload, dict) else {}
        remote_modules = snapshot.get("modules") if isinstance(snapshot.get("modules"), list) else []
        remote_bindings = snapshot.get("bindings") if isinstance(snapshot.get("bindings"), list) else []

        modules_written = self._merge_remote_modules(remote_modules)
        bindings_written = self._merge_remote_bindings(remote_bindings, source=source)
        resolved = self.resolve_module(tenant_id=str(tenant_id or ""), user_id=str(user_id or ""))
        return {
            "ok": True,
            "source": source,
            "modules_synced": modules_written,
            "bindings_synced": bindings_written,
            "resolved_module_key": str(resolved.get("module_key") or ""),
            "resolved_module_name": str(resolved.get("module_name") or ""),
            "tenant_id": str(tenant_id or ""),
            "user_id": str(user_id or ""),
        }

    def get_module(self, module_key: str) -> dict[str, Any] | None:
        for item in self.list_modules(include_inactive=True):
            if str(item.get("module_key") or "") == module_key:
                return item
        return None

    def resolve_module(self, *, tenant_id: str, user_id: str = "", requested_module_key: str = "") -> dict[str, Any]:
        if requested_module_key:
            module = self.get_module(requested_module_key)
            if not module:
                raise ValueError(f"module not found: {requested_module_key}")
            if str(module.get("status") or "active") != "active":
                raise ValueError(f"module is not active: {requested_module_key}")
            return module

        bindings = [item for item in self._read_json(self.bindings_path, default=[]) if isinstance(item, dict)]
        active_bindings = [item for item in bindings if item.get("enabled", True) is not False]

        user_tenant_binding: dict[str, Any] | None = None
        user_fallback_binding: dict[str, Any] | None = None
        if user_id:
            for item in sorted(active_bindings, key=lambda value: str(value.get("updated_at") or ""), reverse=True):
                if str(item.get("scope_type") or "") != "user":
                    continue
                if str(item.get("scope_id") or "") != user_id:
                    continue
                if not binding_matches_tenant(item, tenant_id):
                    continue
                binding_tenant = str(item.get("tenant_id") or "").strip()
                if binding_tenant == str(tenant_id or "") and user_tenant_binding is None:
                    user_tenant_binding = item
                if not binding_tenant and user_fallback_binding is None:
                    user_fallback_binding = item
            if user_tenant_binding:
                module = self.get_module(str(user_tenant_binding.get("module_key") or ""))
                if module and str(module.get("status") or "active") == "active":
                    return module

        tenant_binding = next(
            (
                item
                for item in sorted(active_bindings, key=lambda value: str(value.get("updated_at") or ""), reverse=True)
                if str(item.get("scope_type") or "") == "tenant"
                and str(item.get("scope_id") or "") == str(tenant_id or "")
            ),
            None,
        )
        if tenant_binding:
            module = self.get_module(str(tenant_binding.get("module_key") or ""))
            if module and str(module.get("status") or "active") == "active":
                return module

        if user_fallback_binding:
            module = self.get_module(str(user_fallback_binding.get("module_key") or ""))
            if module and str(module.get("status") or "active") == "active":
                return module

        global_binding = next(
            (
                item
                for item in sorted(active_bindings, key=lambda value: str(value.get("updated_at") or ""), reverse=True)
                if str(item.get("scope_type") or "") == "global"
            ),
            None,
        )
        if global_binding:
            module = self.get_module(str(global_binding.get("module_key") or ""))
            if module and str(module.get("status") or "active") == "active":
                return module

        modules = self.list_modules(include_inactive=False)
        if modules:
            return modules[0]
        raise ValueError("no active recorder modules available")

    def _ensure_builtin_modules(self, modules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_key = {str(item.get("module_key") or ""): item for item in modules}
        changed = False
        for builtin in BUILTIN_MODULES:
            module_key = str(builtin.get("module_key") or "")
            if module_key not in by_key:
                changed = True
                by_key[module_key] = {
                    **builtin,
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                }
            else:
                existing = dict(by_key[module_key])
                config = dict(existing.get("config") or {})
                builtin_config = dict(builtin.get("config") or {})
                config_changed = False
                for key, value in builtin_config.items():
                    if key not in config:
                        config[key] = value
                        config_changed = True
                if module_key == "order_sheet_lab_v1":
                    if str(config.get("date_output_mode") or "").strip() in {"MMDD_text", "MMDD-TEXT", "MMDD"}:
                        config["date_output_mode"] = str(builtin_config.get("date_output_mode") or "YYYY-MM-DD")
                        config_changed = True
                    llm_budget = int(config.get("llm_max_rows_per_run", 0) or 0)
                    if llm_budget <= 0 or llm_budget >= 40:
                        config["llm_max_rows_per_run"] = int(builtin_config.get("llm_max_rows_per_run") or 12)
                        config_changed = True
                    if not str(config.get("llm_supplement_mode") or "").strip():
                        config["llm_supplement_mode"] = str(builtin_config.get("llm_supplement_mode") or "missing_core_fields_only")
                        config_changed = True
                    if not str(config.get("extract_mode") or "").strip():
                        config["extract_mode"] = str(builtin_config.get("extract_mode") or "llm_first")
                        config_changed = True
                    if "llm_segmentation_enabled" not in config:
                        config["llm_segmentation_enabled"] = bool(builtin_config.get("llm_segmentation_enabled", True))
                        config_changed = True
                    if int(config.get("llm_segmentation_max_segments", 0) or 0) <= 0:
                        config["llm_segmentation_max_segments"] = int(builtin_config.get("llm_segmentation_max_segments") or 6)
                        config_changed = True
                    if int(config.get("llm_repair_max_rows_per_run", 0) or 0) <= 0:
                        config["llm_repair_max_rows_per_run"] = int(builtin_config.get("llm_repair_max_rows_per_run") or 8)
                        config_changed = True
                    if str(config.get("missing_quantity_strategy") or "").strip().lower() not in {"strict", "assume_one", "llm_guess"}:
                        config["missing_quantity_strategy"] = str(builtin_config.get("missing_quantity_strategy") or "strict")
                        config_changed = True
                    include_types = config.get("include_record_types_in_main_sheet")
                    if not isinstance(include_types, list) or not include_types:
                        config["include_record_types_in_main_sheet"] = list(builtin_config.get("include_record_types_in_main_sheet") or ["order_item", "gift_item"])
                        config_changed = True
                    for bool_key in ("llm_dynamic_budget_enabled", "force_multi_sku_split_enabled", "context_followup_enabled", "brand_llm_inference_enabled"):
                        if bool_key not in config:
                            config[bool_key] = bool(builtin_config.get(bool_key, True))
                            config_changed = True
                    brand_aliases = config.get("brand_aliases")
                    if not isinstance(brand_aliases, list) or not any(str(item).strip() for item in brand_aliases):
                        config["brand_aliases"] = list(
                            builtin_config.get("brand_aliases")
                            or ["津腾", "康为", "源叶", "麦克林", "施睿康", "甄选", "赛宁", "建成", "索莱宝", "毕得医药"]
                        )
                        config_changed = True
                    for numeric_key, fallback in (
                        ("llm_dynamic_budget_ratio", 0.35),
                        ("llm_dynamic_budget_max", 64),
                        ("llm_dynamic_repair_ratio", 0.25),
                        ("llm_dynamic_repair_max", 32),
                        ("llm_parallel_workers", 4),
                        ("force_multi_sku_min_skus", 2),
                        ("force_multi_order_signal_threshold", 2),
                        ("brand_llm_inference_max_calls_per_run", 20),
                        ("brand_llm_inference_min_confidence", 0.62),
                    ):
                        try:
                            parsed = float(config.get(numeric_key)) if ("ratio" in numeric_key or "confidence" in numeric_key) else int(config.get(numeric_key))
                        except (TypeError, ValueError):
                            parsed = 0
                        if parsed <= 0:
                            config[numeric_key] = builtin_config.get(numeric_key, fallback)
                            config_changed = True
                if config_changed:
                    existing["config"] = config
                    existing["updated_at"] = now_iso()
                    by_key[module_key] = existing
                    changed = True
                if existing.get("builtin") is not True:
                    existing["builtin"] = True
                    existing["updated_at"] = now_iso()
                    by_key[module_key] = existing
                    changed = True
        if changed:
            items = sorted(by_key.values(), key=lambda item: str(item.get("module_key") or ""))
            self._write_json(self.registry_path, items)
            return items
        return sorted(by_key.values(), key=lambda item: str(item.get("module_key") or ""))

    def _merge_remote_modules(self, modules: list[dict[str, Any]]) -> int:
        current = {str(item.get("module_key") or ""): item for item in self.list_modules(include_inactive=True)}
        changed = False
        written = 0
        for item in modules:
            if not isinstance(item, dict):
                continue
            module_key = safe_id(str(item.get("module_key") or ""))
            if not module_key:
                continue
            existing = current.get(module_key, {})
            record = {
                **existing,
                "module_key": module_key,
                "module_name": str(item.get("module_name") or existing.get("module_name") or module_key),
                "module_type": str(item.get("module_type") or existing.get("module_type") or "chat_extract_export"),
                "status": str(item.get("status") or existing.get("status") or "active"),
                "version": str(item.get("version") or existing.get("version") or "1.0.0"),
                "config": item.get("config") if isinstance(item.get("config"), dict) else dict(existing.get("config") or {}),
                "builtin": bool(existing.get("builtin", False)),
                "created_at": str(existing.get("created_at") or now_iso()),
                "updated_at": now_iso(),
                "sync_source": REMOTE_BINDING_SOURCE,
            }
            current[module_key] = record
            changed = True
            written += 1
        if changed:
            self._write_json(self.registry_path, sorted(current.values(), key=lambda value: str(value.get("module_key") or "")))
        return written

    def _merge_remote_bindings(self, bindings: list[dict[str, Any]], *, source: str) -> int:
        current = [item for item in self._read_json(self.bindings_path, default=[]) if isinstance(item, dict)]
        kept = [item for item in current if str(item.get("sync_source") or "").strip() != source]
        remote_items: dict[str, dict[str, Any]] = {}
        written = 0
        for item in bindings:
            if not isinstance(item, dict):
                continue
            scope_type = str(item.get("scope_type") or "").strip().lower()
            if scope_type not in {"user", "tenant", "global"}:
                continue
            scope_id = str(item.get("scope_id") or "").strip()
            if scope_type == "global":
                scope_id = "*"
            if not scope_id:
                continue
            module_key = str(item.get("module_key") or "").strip()
            if not module_key:
                continue
            binding_id = safe_id(str(item.get("binding_id") or f"{scope_type}_{scope_id}"))
            existing = next((candidate for candidate in current if str(candidate.get("binding_id") or "") == binding_id), {})
            tenant_id = str(item.get("tenant_id") or existing.get("tenant_id") or "").strip()
            if scope_type == "tenant" and not tenant_id:
                tenant_id = scope_id
            if scope_type == "global":
                tenant_id = ""
            user_id = str(item.get("user_id") or existing.get("user_id") or (scope_id if scope_type == "user" else "")).strip()
            remote_items[binding_id] = {
                "binding_id": binding_id,
                "scope_type": scope_type,
                "scope_id": scope_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "module_key": module_key,
                "enabled": item.get("enabled", True) is not False,
                "created_at": str(existing.get("created_at") or item.get("created_at") or now_iso()),
                "updated_at": str(item.get("updated_at") or now_iso()),
                "sync_source": source,
            }
            written += 1
        merged = {str(item.get("binding_id") or ""): item for item in kept if str(item.get("binding_id") or "")}
        merged.update(remote_items)
        ordered = sorted(merged.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        self._write_json(self.bindings_path, ordered)
        return written

    def _read_json(self, path: Path, *, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
        return payload

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
