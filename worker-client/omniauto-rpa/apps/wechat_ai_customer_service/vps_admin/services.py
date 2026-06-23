"""Domain services for the VPS admin control plane."""

from __future__ import annotations

import json
import hmac
import os
import re
import secrets
import urllib.error
import urllib.request
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from apps.wechat_ai_customer_service.auth.email_verification import EmailVerificationService, email_settings_from_config, normalize_email
from apps.wechat_ai_customer_service.auth.models import AuthSession, Role
from apps.wechat_ai_customer_service.auth.session import read_local_account_overrides_from_env, write_local_account_overrides_to_env
from apps.wechat_ai_customer_service.llm_config import (
    apply_llm_reasoning_effort,
    llm_urlopen,
    read_secret,
    resolve_deepseek_base_url,
    resolve_deepseek_max_tokens,
    resolve_deepseek_tier_model,
    resolve_deepseek_timeout,
)
from apps.wechat_ai_customer_service.knowledge_paths import (
    DEFAULT_TENANT_ID,
    TENANTS_ROOT,
    active_tenant_id,
    read_tenant_metadata,
    runtime_app_root,
    tenant_knowledge_base_root,
    tenant_metadata_path,
)
from apps.wechat_ai_customer_service.industry_catalog import (
    GLOBAL_INDUSTRY_ID,
    build_policy_bundle,
    default_industry_for_tenant,
    industry_catalog,
    normalize_industry_id,
    normalize_shared_item_industry_id,
    resolve_tenant_industry_id,
    seed_shared_library_items,
)
from apps.wechat_ai_customer_service.sync import BackupService, SharedPatchService
from apps.wechat_ai_customer_service.workflows.generate_review_candidates import (
    call_deepseek_json,
    compact_excerpt,
    stable_digest,
)

from .auth import (
    VpsAdminAuthService,
    ensure_not_reserved_admin,
    ensure_tenant_exists,
    hash_password,
    make_id,
    public_user,
    revoke_user_sessions,
    require_customer_or_guest,
)
from .local_data import build_shared_knowledge_snapshot, build_tenant_data_summary
from apps.wechat_ai_customer_service.exports.readable_export import build_customer_readable_workbook
from .store import VpsAdminStore, append_audit, now_iso


DEFAULT_RECORDER_MODULES = [
    {
        "module_key": "raw_message_log_v1",
        "module_name": "通用原始消息导出V1",
        "module_type": "chat_extract_export",
        "status": "active",
        "version": "1.0.0",
        "config": {
            "description": "Generic baseline export that writes raw messages to a readable workbook.",
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
            "supported_content_types": ["text", "quote"],
        },
        "builtin": True,
    },
]


class TenantService:
    def __init__(self, store: VpsAdminStore) -> None:
        self.store = store

    def list_tenants(self) -> list[dict[str, Any]]:
        def mutate(state: dict[str, Any]) -> list[dict[str, Any]]:
            seed_local_customer_accounts(state)
            ensure_tenant_industry_bindings(state)
            tenants = state.get("tenants", {})
            return sorted(tenants.values(), key=lambda item: str(item.get("tenant_id") or ""))

        return self.store.update(mutate)

    def create_tenant(self, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        tenant_id = active_tenant_id(payload.get("tenant_id") or payload.get("id") or make_id("tenant"))
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        industry_id = normalize_industry_id(
            payload.get("industry_id") or payload.get("industry") or metadata.get("industry_id") or metadata.get("industry")
        )
        record = {
            "tenant_id": tenant_id,
            "display_name": str(payload.get("display_name") or tenant_id),
            "status": str(payload.get("status") or "active"),
            "sync_enabled": bool(payload.get("sync_enabled", False)),
            "industry_id": industry_id,
            "metadata": {**metadata, "industry_id": industry_id},
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if tenant_id in state["tenants"]:
                raise HTTPException(status_code=409, detail=f"tenant already exists: {tenant_id}")
            state["tenants"][tenant_id] = record
            append_audit(state, actor_id=actor.user.user_id, action="create_tenant", target_type="tenant", target_id=tenant_id)
            return record

        return self.store.update(mutate)

    def update_tenant(self, tenant_id: str, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        tenant = active_tenant_id(tenant_id)

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if tenant not in state["tenants"]:
                raise HTTPException(status_code=404, detail=f"tenant not found: {tenant}")
            record = state["tenants"][tenant]
            for key in ("display_name", "status", "sync_enabled"):
                if key in payload:
                    record[key] = payload[key]
            if "industry_id" in payload or "industry" in payload:
                record["industry_id"] = normalize_industry_id(payload.get("industry_id") or payload.get("industry"))
            if isinstance(payload.get("metadata"), dict):
                record["metadata"] = payload["metadata"]
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            industry = normalize_industry_id(
                record.get("industry_id")
                or metadata.get("industry_id")
                or metadata.get("industry")
                or default_industry_for_tenant(tenant)
            )
            record["industry_id"] = industry
            record["metadata"] = {**metadata, "industry_id": industry}
            record["updated_at"] = now_iso()
            append_audit(state, actor_id=actor.user.user_id, action="update_tenant", target_type="tenant", target_id=tenant)
            return record

        return self.store.update(mutate)


class RecorderModuleAssignmentService:
    def __init__(self, store: VpsAdminStore) -> None:
        self.store = store

    def list_modules(self, *, include_inactive: bool = True) -> list[dict[str, Any]]:
        def mutate(state: dict[str, Any]) -> list[dict[str, Any]]:
            changed = False
            modules = state.get("recorder_modules", {})
            for builtin in DEFAULT_RECORDER_MODULES:
                module_key = str(builtin.get("module_key") or "")
                if module_key not in modules:
                    modules[module_key] = {
                        **builtin,
                        "created_at": now_iso(),
                        "updated_at": now_iso(),
                    }
                    changed = True
                else:
                    existing = dict(modules.get(module_key) or {})
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
                        for bool_key in ("llm_dynamic_budget_enabled", "force_multi_sku_split_enabled", "context_followup_enabled"):
                            if bool_key not in config:
                                config[bool_key] = bool(builtin_config.get(bool_key, True))
                                config_changed = True
                        for numeric_key, fallback in (
                            ("llm_dynamic_budget_ratio", 0.35),
                            ("llm_dynamic_budget_max", 64),
                            ("llm_dynamic_repair_ratio", 0.25),
                            ("llm_dynamic_repair_max", 32),
                            ("force_multi_sku_min_skus", 2),
                            ("force_multi_order_signal_threshold", 2),
                        ):
                            try:
                                parsed = float(config.get(numeric_key)) if "ratio" in numeric_key else int(config.get(numeric_key))
                            except (TypeError, ValueError):
                                parsed = 0
                            if parsed <= 0:
                                config[numeric_key] = builtin_config.get(numeric_key, fallback)
                                config_changed = True
                    if config_changed:
                        existing["config"] = config
                        existing["updated_at"] = now_iso()
                        modules[module_key] = existing
                        changed = True
            if changed:
                state["recorder_modules"] = modules
            items = sorted(modules.values(), key=lambda item: str(item.get("module_key") or ""))
            if not include_inactive:
                items = [item for item in items if str(item.get("status") or "active") == "active"]
            return items

        return self.store.update(mutate)

    def upsert_module(self, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        module_key = str(payload.get("module_key") or payload.get("key") or "").strip()
        if not module_key:
            raise HTTPException(status_code=400, detail="module_key required")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            modules = state.get("recorder_modules", {})
            existing = modules.get(module_key, {})
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
            modules[module_key] = record
            state["recorder_modules"] = modules
            append_audit(state, actor_id=actor.user.user_id, action="upsert_recorder_module", target_type="recorder_module", target_id=module_key)
            return record

        return self.store.update(mutate)

    def list_bindings(
        self,
        *,
        scope_type: str = "",
        scope_id: str = "",
        tenant_id: str = "",
        user_id: str = "",
    ) -> list[dict[str, Any]]:
        state = self.store.read()
        bindings = [
            item
            for item in (state.get("recorder_module_bindings") or {}).values()
            if str(item.get("scope_type") or "") in {"user", "tenant", "global"}
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

    def local_sync_snapshot(self, *, tenant_id: str, user_id: str = "") -> dict[str, Any]:
        tenant = active_tenant_id(tenant_id)
        state = self.store.read()
        modules = self.list_modules(include_inactive=True)
        bindings = self.list_bindings()

        resolved_user_ids: set[str] = set()
        normalized_user = str(user_id or "").strip()
        if normalized_user:
            resolved_user_ids.add(normalized_user)
        tenant_customer = customer_user_by_tenant(state, tenant)
        if isinstance(tenant_customer, dict):
            customer_user_id = str(tenant_customer.get("user_id") or "").strip()
            if customer_user_id:
                resolved_user_ids.add(customer_user_id)

        filtered: list[dict[str, Any]] = []
        for item in bindings:
            scope_type = str(item.get("scope_type") or "").strip().lower()
            if scope_type == "global":
                filtered.append(item)
                continue
            if scope_type == "tenant":
                scope_id = str(item.get("scope_id") or "").strip()
                binding_tenant = str(item.get("tenant_id") or "").strip()
                if scope_id == tenant or (binding_tenant and binding_tenant == tenant):
                    filtered.append(item)
                continue
            if scope_type == "user":
                scope_id = str(item.get("scope_id") or "").strip()
                binding_user_id = str(item.get("user_id") or "").strip()
                binding_tenant = str(item.get("tenant_id") or "").strip()
                tenant_matches = not binding_tenant or binding_tenant == tenant
                user_matches = scope_id in resolved_user_ids or binding_user_id in resolved_user_ids
                if tenant_matches and user_matches:
                    filtered.append(item)
                continue

        if not any(str(item.get("scope_type") or "") == "tenant" and str(item.get("scope_id") or "") == tenant for item in filtered):
            inferred_module = infer_default_module_for_tenant(state, tenant_id=tenant, modules=modules)
            if inferred_module:
                filtered.append(
                    {
                        "binding_id": f"auto_tenant_{tenant}",
                        "scope_type": "tenant",
                        "scope_id": tenant,
                        "tenant_id": tenant,
                        "user_id": "",
                        "module_key": inferred_module,
                        "enabled": True,
                        "created_at": now_iso(),
                        "updated_at": now_iso(),
                        "synthetic": True,
                    }
                )

        fingerprint_payload = {
            "tenant_id": tenant,
            "resolved_user_ids": sorted(resolved_user_ids),
            "module_keys": [str(item.get("module_key") or "") for item in modules],
            "binding_ids": [str(item.get("binding_id") or "") for item in filtered],
        }
        snapshot_version = "recorder_sync_" + stable_digest(json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True), 20)
        return {
            "tenant_id": tenant,
            "request_user_id": normalized_user,
            "resolved_user_ids": sorted(resolved_user_ids),
            "snapshot_version": snapshot_version,
            "modules": modules,
            "bindings": filtered,
            "generated_at": now_iso(),
        }

    def upsert_binding(self, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        scope_type = str(payload.get("scope_type") or "").strip().lower()
        if scope_type not in {"user", "tenant", "global"}:
            raise HTTPException(status_code=400, detail="scope_type must be one of user|tenant|global")
        scope_id = str(payload.get("scope_id") or "").strip()
        if scope_type == "global":
            scope_id = "*"
        if not scope_id:
            raise HTTPException(status_code=400, detail="scope_id required")
        module_key = str(payload.get("module_key") or "").strip()
        if not module_key:
            raise HTTPException(status_code=400, detail="module_key required")
        module = next((item for item in self.list_modules(include_inactive=True) if str(item.get("module_key") or "") == module_key), None)
        if not module:
            raise HTTPException(status_code=404, detail=f"module not found: {module_key}")
        if str(module.get("status") or "active") != "active":
            raise HTTPException(status_code=400, detail=f"module is not active: {module_key}")

        binding_id = str(payload.get("binding_id") or make_id("recorder_binding"))
        owner_user_id = str(payload.get("user_id") or scope_id if scope_type == "user" else "").strip()
        tenant_scope_id = str(payload.get("tenant_id") or "").strip()
        if scope_type == "tenant":
            tenant_scope_id = tenant_scope_id or scope_id
        if scope_type == "global":
            tenant_scope_id = ""

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            bindings = state.get("recorder_module_bindings", {})
            existing = bindings.get(binding_id, {})
            record = {
                "binding_id": binding_id,
                "scope_type": scope_type,
                "scope_id": scope_id,
                "tenant_id": tenant_scope_id,
                "user_id": owner_user_id,
                "module_key": module_key,
                "enabled": payload.get("enabled", existing.get("enabled", True)) is not False,
                "created_at": str(existing.get("created_at") or now_iso()),
                "updated_at": now_iso(),
            }
            bindings[binding_id] = record
            state["recorder_module_bindings"] = bindings
            append_audit(state, actor_id=actor.user.user_id, action="upsert_recorder_module_binding", target_type="recorder_module_binding", target_id=binding_id)
            return record

        return self.store.update(mutate)

    def delete_binding(self, binding_id: str, *, actor: AuthSession) -> bool:
        def mutate(state: dict[str, Any]) -> bool:
            bindings = state.get("recorder_module_bindings", {})
            if binding_id not in bindings:
                return False
            bindings.pop(binding_id, None)
            state["recorder_module_bindings"] = bindings
            append_audit(state, actor_id=actor.user.user_id, action="delete_recorder_module_binding", target_type="recorder_module_binding", target_id=binding_id)
            return True

        return self.store.update(mutate)


class OverviewService:
    def __init__(self, store: VpsAdminStore) -> None:
        self.store = store

    def overview(self) -> dict[str, Any]:
        state = self.store.read()
        pending_commands = [
            item for item in state.get("commands", {}).values() if item.get("status") in {"queued", "sent"}
        ]
        latest_shared_snapshot = latest_by_created_at(state.get("shared_snapshots", {}).values())
        latest_data_package = latest_by_created_at(state.get("customer_data_packages", {}).values())
        return {
            "counts": {
                "tenants": len(state.get("tenants", {})),
                "users": len(state.get("users", {})),
                "nodes": len(state.get("local_nodes", {})),
                "pending_commands": len(pending_commands),
                "customer_data_packages": len(state.get("customer_data_packages", {})),
                "shared_snapshots": len(state.get("shared_snapshots", {})),
                "shared_library_items": len(state.get("shared_library", {})),
                "shared_pending_proposals": len(
                    [
                        item
                        for item in state.get("shared_proposals", {}).values()
                        if item.get("status") == "pending_review"
                    ]
                ),
                "backup_requests": len(state.get("backup_requests", {})),
                "restore_requests": len(state.get("restore_requests", {})),
            },
            "latest_shared_snapshot": latest_shared_snapshot,
            "latest_data_package": latest_data_package,
            "recommendations": [
                {
                    "title": "云端正式共享库已和客户专业知识分层",
                    "detail": "共享公共知识以云端 shared_library 为唯一正式来源；客户正式知识、商品专属知识和 RAG 数据仍位于 tenant 目录。",
                    "status": "ok",
                },
                {
                    "title": "真实还原仍建议先执行演练",
                    "detail": "当前控制台的一键还原入口默认创建 dry-run 命令，避免误覆盖客户本地数据。",
                    "status": "warning",
                },
            ],
        }


class SecurityConfigService:
    def __init__(self, store: VpsAdminStore) -> None:
        self.store = store

    def smtp_config(self) -> dict[str, Any]:
        state = self.store.read()
        config = state.get("smtp_config") if isinstance(state.get("smtp_config"), dict) else {}
        settings = email_settings_from_config(config)
        return {
            "server": str(config.get("server") or settings.smtp_host or ""),
            "port": int(config.get("port") or settings.smtp_port),
            "use_ssl": bool(config.get("use_ssl", settings.smtp_use_ssl)),
            "use_tls": bool(config.get("use_tls", settings.smtp_use_tls)),
            "username": str(config.get("username") or settings.smtp_username or ""),
            "password_set": bool(config.get("password") or settings.smtp_password),
            "from_email": str(config.get("from_email") or settings.mail_from or ""),
            "sender_name": str(config.get("sender_name") or settings.sender_name or "OmniAuto"),
            "otp_required": bool(config.get("otp_required", settings.otp_required)),
            "code_length": int(config.get("code_length") or settings.code_length),
            "ttl_minutes": int(config.get("ttl_minutes") or settings.ttl_minutes),
            "resend_seconds": int(config.get("resend_seconds") or settings.resend_seconds),
            "trusted_device_days": int(config.get("trusted_device_days") or settings.trusted_device_days),
            "smtp_configured": bool(settings.smtp_host),
        }

    def update_smtp_config(self, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            current = state.get("smtp_config") if isinstance(state.get("smtp_config"), dict) else {}
            password = str(payload.get("password") or "")
            if not password and not payload.get("clear_password"):
                password = str(current.get("password") or "")
            record = {
                "server": str(payload.get("server") or "").strip(),
                "port": int(payload.get("port") or 465),
                "use_ssl": to_bool(payload.get("use_ssl"), default=True),
                "use_tls": to_bool(payload.get("use_tls"), default=False),
                "username": str(payload.get("username") or "").strip(),
                "password": "" if payload.get("clear_password") else password,
                "from_email": normalize_email(str(payload.get("from_email") or payload.get("username") or "")),
                "sender_name": str(payload.get("sender_name") or "OmniAuto").strip() or "OmniAuto",
                "otp_required": to_bool(payload.get("otp_required"), default=True),
                "code_length": clamp_int(payload.get("code_length"), default=4, minimum=4, maximum=8),
                "ttl_minutes": clamp_int(payload.get("ttl_minutes"), default=15, minimum=1, maximum=60),
                "resend_seconds": clamp_int(payload.get("resend_seconds"), default=60, minimum=10, maximum=600),
                "trusted_device_days": clamp_int(payload.get("trusted_device_days"), default=30, minimum=1, maximum=120),
                "initialized_at": str(current.get("initialized_at") or now_iso()),
                "updated_by": actor.user.user_id,
                "updated_at": now_iso(),
            }
            state["smtp_config"] = record
            append_audit(
                state,
                actor_id=actor.user.user_id,
                action="update_smtp_config",
                target_type="security",
                target_id="smtp_config",
                detail={"server": record["server"], "otp_required": record["otp_required"]},
            )
            return self.smtp_config_from_state(state)

        return self.store.update(mutate)

    def test_smtp(self, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        to_email = normalize_email(str(payload.get("to_email") or payload.get("email") or ""))
        if not to_email:
            raise HTTPException(status_code=400, detail="test email required")
        state = self.store.read()
        try:
            delivery = EmailVerificationService(email_settings_from_config(state.get("smtp_config"))).send_test_email(to_email=to_email)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"test email failed: {exc}") from exc

        def mutate(next_state: dict[str, Any]) -> None:
            append_audit(
                next_state,
                actor_id=actor.user.user_id,
                action="test_smtp_config",
                target_type="security",
                target_id="smtp_config",
                detail={"to_email": to_email, "method": delivery.get("method")},
            )

        self.store.update(mutate)
        return {"sent": True, "delivery": {key: value for key, value in delivery.items() if key != "debug_code"}}

    def smtp_config_from_state(self, state: dict[str, Any]) -> dict[str, Any]:
        config = state.get("smtp_config") if isinstance(state.get("smtp_config"), dict) else {}
        settings = email_settings_from_config(config)
        return {
            "server": str(config.get("server") or settings.smtp_host or ""),
            "port": int(config.get("port") or settings.smtp_port),
            "use_ssl": bool(config.get("use_ssl", settings.smtp_use_ssl)),
            "use_tls": bool(config.get("use_tls", settings.smtp_use_tls)),
            "username": str(config.get("username") or settings.smtp_username or ""),
            "password_set": bool(config.get("password") or settings.smtp_password),
            "from_email": str(config.get("from_email") or settings.mail_from or ""),
            "sender_name": str(config.get("sender_name") or settings.sender_name or "OmniAuto"),
            "otp_required": bool(config.get("otp_required", settings.otp_required)),
            "code_length": int(config.get("code_length") or settings.code_length),
            "ttl_minutes": int(config.get("ttl_minutes") or settings.ttl_minutes),
            "resend_seconds": int(config.get("resend_seconds") or settings.resend_seconds),
            "trusted_device_days": int(config.get("trusted_device_days") or settings.trusted_device_days),
            "smtp_configured": bool(settings.smtp_host),
        }


class UserService:
    def __init__(self, store: VpsAdminStore, auth: VpsAdminAuthService) -> None:
        self.store = store
        self.auth = auth

    def list_users(self) -> list[dict[str, Any]]:
        def mutate(state: dict[str, Any]) -> list[dict[str, Any]]:
            seed_local_customer_accounts(state)
            users = state.get("users", {})
            return sorted((self.public_user_with_access(item, state=state) for item in users.values()), key=lambda item: str(item.get("username") or ""))

        return self.store.update(mutate)

    def create_user(self, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        username = str(payload.get("username") or "").strip()
        if not username:
            raise HTTPException(status_code=400, detail="username required")
        role = require_customer_or_guest(str(payload.get("role") or "customer"))
        ensure_not_reserved_admin(username, role, self.auth.settings)
        password = str(payload.get("password") or "").strip()
        if not password:
            raise HTTPException(status_code=400, detail="password required")
        raw_tenant_ids = payload.get("tenant_ids", [])
        if not isinstance(raw_tenant_ids, list):
            raw_tenant_ids = [raw_tenant_ids]
        tenant_ids = [active_tenant_id(item) for item in raw_tenant_ids if str(item).strip()]
        if role == Role.CUSTOMER and not tenant_ids:
            tenant_ids = [active_tenant_id(username)]
        guest_customer_username = ""
        if role == Role.GUEST and not tenant_ids:
            guest_customer_username = str(payload.get("authorized_customer") or payload.get("customer_username") or "").strip()
            if not guest_customer_username:
                raise HTTPException(status_code=400, detail="guest account requires an authorized customer")
        raw_industry = str(payload.get("industry_id") or payload.get("industry") or "").strip()
        requested_industry = normalize_industry_id(raw_industry) if raw_industry else ""

        user_id = str(payload.get("user_id") or make_id("user"))
        record = {
            "user_id": user_id,
            "username": username,
            "display_name": str(payload.get("display_name") or username),
            "email": normalize_email(str(payload.get("email") or "")),
            "role": role.value,
            "tenant_ids": tenant_ids,
            "industry_id": requested_industry or "",
            "resource_scopes": payload.get("resource_scopes") if isinstance(payload.get("resource_scopes"), list) else ["*"],
            "status": str(payload.get("status") or "active"),
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        record["password_hash"] = hash_password(password)

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            resolved_tenant_ids = list(tenant_ids)
            if role == Role.GUEST and guest_customer_username:
                customer = customer_user_by_username(state, guest_customer_username)
                if not customer:
                    raise HTTPException(status_code=404, detail=f"authorized customer not found: {guest_customer_username}")
                resolved_tenant_ids = [
                    active_tenant_id(item)
                    for item in customer.get("tenant_ids", [])
                    if str(item).strip()
                ]
                if not resolved_tenant_ids:
                    raise HTTPException(status_code=400, detail=f"authorized customer has no data scope: {guest_customer_username}")
                record["tenant_ids"] = resolved_tenant_ids
            if role == Role.GUEST and not guest_customer_username:
                missing_customer = [
                    tenant_id
                    for tenant_id in resolved_tenant_ids
                    if not customer_user_by_tenant(state, tenant_id)
                ]
                if missing_customer:
                    raise HTTPException(status_code=400, detail="guest account must be assigned to an existing customer")
            for tenant_id in resolved_tenant_ids:
                if tenant_id not in state["tenants"]:
                    if role == Role.CUSTOMER and tenant_id == active_tenant_id(username):
                        state["tenants"][tenant_id] = customer_tenant_record(
                            username=username,
                            tenant_id=tenant_id,
                            industry_id=requested_industry or default_industry_for_tenant(tenant_id),
                        )
                    else:
                        ensure_tenant_exists(state, tenant_id)
                if role == Role.CUSTOMER and requested_industry:
                    tenant_record = state["tenants"].get(tenant_id)
                    if isinstance(tenant_record, dict):
                        tenant_metadata = tenant_record.get("metadata") if isinstance(tenant_record.get("metadata"), dict) else {}
                        tenant_record["industry_id"] = requested_industry
                        tenant_record["metadata"] = {**tenant_metadata, "industry_id": requested_industry}
                        tenant_record["updated_at"] = now_iso()
            if user_id in state["users"]:
                raise HTTPException(status_code=409, detail=f"user already exists: {user_id}")
            if any(str(item.get("username") or "") == username for item in state["users"].values()):
                raise HTTPException(status_code=409, detail=f"username already exists: {username}")
            state["users"][user_id] = record
            append_audit(state, actor_id=actor.user.user_id, action="create_user", target_type="user", target_id=user_id)
            return self.public_user_with_access(record, state=state)

        return self.store.update(mutate)

    def update_user(self, user_id: str, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if user_id not in state["users"]:
                raise HTTPException(status_code=404, detail=f"user not found: {user_id}")
            record = state["users"][user_id]
            role = require_customer_or_guest(str(payload.get("role") or record.get("role") or "guest"))
            ensure_not_reserved_admin(str(payload.get("username") or record.get("username") or ""), role, self.auth.settings)
            if "username" in payload:
                username = str(payload["username"]).strip()
                if not username:
                    raise HTTPException(status_code=400, detail="username required")
                if any(uid != user_id and str(item.get("username") or "") == username for uid, item in state["users"].items()):
                    raise HTTPException(status_code=409, detail=f"username already exists: {username}")
                record["username"] = username
            if "role" in payload:
                record["role"] = role.value
            if "tenant_ids" in payload:
                tenant_ids = [active_tenant_id(item) for item in payload.get("tenant_ids", []) if str(item).strip()]
                if not tenant_ids:
                    raise HTTPException(status_code=400, detail="tenant_ids required")
                for tenant_id in tenant_ids:
                    ensure_tenant_exists(state, tenant_id)
                record["tenant_ids"] = tenant_ids
            requested_industry = ""
            if "industry_id" in payload or "industry" in payload:
                requested_industry = normalize_industry_id(payload.get("industry_id") or payload.get("industry"))
                record["industry_id"] = requested_industry
                if role == Role.CUSTOMER:
                    target_tenants = [active_tenant_id(item) for item in record.get("tenant_ids", []) if str(item).strip()]
                    for tenant_id in target_tenants:
                        tenant_record = state.get("tenants", {}).get(tenant_id)
                        if not isinstance(tenant_record, dict):
                            continue
                        tenant_metadata = tenant_record.get("metadata") if isinstance(tenant_record.get("metadata"), dict) else {}
                        tenant_record["industry_id"] = requested_industry
                        tenant_record["metadata"] = {**tenant_metadata, "industry_id": requested_industry}
                        tenant_record["updated_at"] = now_iso()
            for key in ("display_name", "resource_scopes", "status"):
                if key in payload:
                    record[key] = payload[key]
            if "email" in payload:
                record["email"] = normalize_email(str(payload.get("email") or ""))
            if payload.get("password"):
                record["password_hash"] = hash_password(str(payload["password"]))
            record["updated_at"] = now_iso()
            append_audit(state, actor_id=actor.user.user_id, action="update_user", target_type="user", target_id=user_id)
            return self.public_user_with_access(record, state=state)

        return self.store.update(mutate)

    def delete_user(self, user_id: str, *, actor: AuthSession) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if user_id not in state["users"]:
                raise HTTPException(status_code=404, detail=f"user not found: {user_id}")
            record = state["users"].pop(user_id)
            removed_local_mirror = remove_local_customer_account_override(record)
            revoked_sessions = revoke_user_sessions(state, user_id=user_id)
            trusted_devices = state.get("trusted_devices", {})
            if isinstance(trusted_devices, dict):
                for fingerprint, device in list(trusted_devices.items()):
                    if isinstance(device, dict) and str(device.get("user_id") or "") == user_id:
                        trusted_devices.pop(fingerprint, None)
            append_audit(
                state,
                actor_id=actor.user.user_id,
                action="delete_user",
                target_type="user",
                target_id=user_id,
                detail={
                    "removed_local_mirror": removed_local_mirror,
                    "revoked_sessions": revoked_sessions,
                },
            )
            payload = self.public_user_with_access(record, state=state)
            payload["removed_local_mirror"] = removed_local_mirror
            payload["revoked_sessions"] = revoked_sessions
            return payload

        return self.store.update(mutate)

    def public_user_with_access(self, record: dict[str, Any], *, state: dict[str, Any]) -> dict[str, Any]:
        result = public_user(record)
        tenant_ids = [active_tenant_id(item) for item in result.get("tenant_ids", []) if str(item).strip()]
        customer_names: list[str] = []
        tenant_industries: dict[str, str] = {}
        for tenant_id in tenant_ids:
            customer = customer_username_for_tenant(state, tenant_id)
            customer_names.append(customer or tenant_id)
            tenant_record = state.get("tenants", {}).get(tenant_id)
            metadata = tenant_record.get("metadata") if isinstance(tenant_record, dict) and isinstance(tenant_record.get("metadata"), dict) else {}
            tenant_industries[tenant_id] = normalize_industry_id(
                (tenant_record.get("industry_id") if isinstance(tenant_record, dict) else "")
                or metadata.get("industry_id")
                or metadata.get("industry")
                or default_industry_for_tenant(tenant_id)
            )
        result["authorized_customers"] = customer_names
        result["tenant_industries"] = tenant_industries
        if result.get("role") == Role.CUSTOMER.value:
            result["customer_name"] = result.get("username")
        return result


class CustomerDataService:
    def __init__(self, store: VpsAdminStore, auth: VpsAdminAuthService) -> None:
        self.store = store
        self.auth = auth

    def list_packages(self) -> list[dict[str, Any]]:
        packages = self.store.read().get("customer_data_packages", {})
        return sorted(packages.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)

    def get_package(self, package_id: str) -> dict[str, Any]:
        record = self.store.read().get("customer_data_packages", {}).get(package_id)
        if not isinstance(record, dict):
            raise HTTPException(status_code=404, detail=f"customer data package not found: {package_id}")
        return record

    def package_for_customer(self, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        username = str(payload.get("account_username") or payload.get("username") or "").strip()
        tenant_id = str(payload.get("tenant_id") or "").strip()
        if not username and not tenant_id:
            raise HTTPException(status_code=400, detail="account_username or tenant_id required")

        state = self.store.read()
        seed_local_customer_accounts(state)
        user = None
        if username:
            for candidate in state.get("users", {}).values():
                if str(candidate.get("username") or "") == username:
                    user = candidate
                    break
            if not isinstance(user, dict):
                raise HTTPException(status_code=404, detail=f"customer account not found: {username}")
            tenant_ids = [active_tenant_id(item) for item in user.get("tenant_ids", []) if str(item).strip()]
            if tenant_id:
                tenant_id = active_tenant_id(tenant_id)
                if tenant_id not in tenant_ids:
                    raise HTTPException(status_code=403, detail="account is not authorized for this tenant")
            elif tenant_ids:
                tenant_id = tenant_ids[0]
        tenant = active_tenant_id(tenant_id or DEFAULT_TENANT_ID)
        if tenant not in state.get("tenants", {}):
            raise HTTPException(status_code=404, detail=f"tenant not found: {tenant}")

        backup = BackupService(output_root=runtime_app_root() / "vps_admin" / "customer_packages").build_backup(
            scope="tenant",
            tenant_id=tenant,
            include_derived=True,
            include_runtime=True,
        )
        summary = build_tenant_data_summary(tenant)
        package_id = f"data_pkg_{username or tenant}_{backup['backup_id']}"
        package = {
            "package_id": package_id,
            "account_username": username,
            "tenant_id": tenant,
            "scope": "tenant",
            "backup_id": backup["backup_id"],
            "package_path": backup["package_path"],
            "bytes": backup["bytes"],
            "summary": summary,
            "manifest": backup["manifest"],
            "created_by": actor.user.user_id,
            "created_at": now_iso(),
        }

        def mutate(next_state: dict[str, Any]) -> dict[str, Any]:
            seed_local_customer_accounts(next_state)
            ensure_tenant_exists(next_state, tenant)
            next_state["customer_data_packages"][package_id] = package
            append_audit(
                next_state,
                actor_id=actor.user.user_id,
                action="package_customer_data",
                target_type="customer_data_package",
                target_id=package_id,
                detail={"account_username": username, "tenant_id": tenant, "backup_id": backup["backup_id"]},
            )
            return package

        return self.store.update(mutate)

    def register_package(self, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        package_id = str(payload.get("package_id") or make_id("data_pkg"))
        tenant_id = active_tenant_id(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        record = {
            "package_id": package_id,
            "account_username": str(payload.get("account_username") or ""),
            "tenant_id": tenant_id,
            "scope": str(payload.get("scope") or "tenant"),
            "backup_id": str(payload.get("backup_id") or ""),
            "package_path": str(payload.get("package_path") or ""),
            "bytes": int(payload.get("bytes") or 0),
            "summary": payload.get("summary") if isinstance(payload.get("summary"), dict) else {},
            "manifest": payload.get("manifest") if isinstance(payload.get("manifest"), dict) else {},
            "created_by": actor.user.user_id,
            "created_at": now_iso(),
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            ensure_tenant_exists(state, tenant_id)
            state["customer_data_packages"][package_id] = record
            append_audit(state, actor_id=actor.user.user_id, action="register_customer_data_package", target_type="customer_data_package", target_id=package_id)
            return record

        return self.store.update(mutate)

    def package_path(self, package_id: str) -> Path:
        record = self.get_package(package_id)
        package_path = Path(str(record.get("package_path") or ""))
        if not package_path.exists() or not package_path.is_file():
            raise HTTPException(status_code=404, detail="package file not found on server")
        return package_path

    def readable_export_path(self, package_id: str) -> Path:
        record = self.get_package(package_id)
        package_path = self.package_path(package_id)
        return build_customer_readable_workbook(record, package_path, output_root=runtime_app_root() / "vps_admin" / "readable_exports")

    def delete_package(self, package_id: str, *, actor: AuthSession) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if package_id not in state["customer_data_packages"]:
                raise HTTPException(status_code=404, detail=f"customer data package not found: {package_id}")
            record = state["customer_data_packages"].pop(package_id)
            append_audit(
                state,
                actor_id=actor.user.user_id,
                action="delete_customer_data_package",
                target_type="customer_data_package",
                target_id=package_id,
            )
            return record

        record = self.store.update(mutate)
        deleted_file = delete_managed_file(Path(str(record.get("package_path") or "")))
        return {"package": record, "deleted_file": deleted_file}

    def bootstrap_test_customer(self, *, actor: AuthSession, tenant_id: str = DEFAULT_TENANT_ID) -> dict[str, Any]:
        tenant = active_tenant_id(tenant_id)
        username = "test01"
        password = "1234.abcd"
        tenant_record = {
            "tenant_id": tenant,
            "display_name": "测试客户 test01（当前客户端数据）",
            "status": "active",
            "sync_enabled": True,
            "metadata": {"source": "local_current_client", "account_username": username},
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        user_record = {
            "user_id": "customer_test01",
            "username": username,
            "display_name": "测试客户 test01",
            "role": Role.CUSTOMER.value,
            "tenant_ids": [tenant],
            "resource_scopes": ["*"],
            "status": "active",
            "email": "test01@example.local",
            "password_hash": hash_password(password),
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        backup = BackupService(output_root=runtime_app_root() / "vps_admin" / "customer_packages").build_backup(
            scope="tenant",
            tenant_id=tenant,
            include_derived=True,
            include_runtime=True,
        )
        summary = build_tenant_data_summary(tenant)
        package_id = f"data_pkg_{username}_{backup['backup_id']}"
        package = {
            "package_id": package_id,
            "account_username": username,
            "tenant_id": tenant,
            "scope": "tenant",
            "backup_id": backup["backup_id"],
            "package_path": backup["package_path"],
            "bytes": backup["bytes"],
            "summary": summary,
            "manifest": backup["manifest"],
            "created_by": actor.user.user_id,
            "created_at": now_iso(),
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            existing_tenant = state["tenants"].get(tenant)
            if existing_tenant:
                existing_tenant.update({key: value for key, value in tenant_record.items() if key not in {"created_at"}})
                tenant_out = existing_tenant
            else:
                state["tenants"][tenant] = tenant_record
                tenant_out = tenant_record
            state["users"][user_record["user_id"]] = user_record
            state["customer_data_packages"][package_id] = package
            append_audit(
                state,
                actor_id=actor.user.user_id,
                action="bootstrap_test01_customer",
                target_type="user",
                target_id=user_record["user_id"],
                detail={"tenant_id": tenant, "backup_id": backup["backup_id"]},
            )
            return {"tenant": tenant_out, "user": public_user(user_record), "package": package}

        return self.store.update(mutate)


class NodeService:
    def __init__(self, store: VpsAdminStore) -> None:
        self.store = store

    def register(self, payload: dict[str, Any], *, actor_id: str = "local-node") -> dict[str, Any]:
        node_id = str(payload.get("node_id") or make_id("node"))
        tenant_ids = [active_tenant_id(item) for item in payload.get("tenant_ids", []) if str(item).strip()]
        if not tenant_ids:
            tenant_ids = [active_tenant_id(payload.get("tenant_id"))]

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            existing = state.get("local_nodes", {}).get(node_id)
            existing_record = existing if isinstance(existing, dict) else {}
            merged_tenant_ids = list(
                dict.fromkeys(
                    [
                        active_tenant_id(item)
                        for item in [*existing_record.get("tenant_ids", []), *tenant_ids]
                        if str(item).strip()
                    ]
                )
            )
            token = str(existing_record.get("node_token") or "") or "node_" + secrets.token_urlsafe(24)
            now = now_iso()
            record = {
                "node_id": node_id,
                "display_name": str(payload.get("display_name") or existing_record.get("display_name") or node_id),
                "tenant_ids": merged_tenant_ids,
                "status": "online",
                "version": str(payload.get("version") or existing_record.get("version") or ""),
                "capabilities": payload.get("capabilities") if isinstance(payload.get("capabilities"), list) else existing_record.get("capabilities", []),
                "node_token": token,
                "registered_at": str(existing_record.get("registered_at") or now),
                "last_seen_at": now,
                "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else existing_record.get("metadata", {}),
            }
            for tenant_id in merged_tenant_ids:
                ensure_tenant_exists(state, tenant_id)
            state["local_nodes"][node_id] = record
            append_audit(state, actor_id=actor_id, action="register_node", target_type="local_node", target_id=node_id)
            return public_node(record, include_token=True)

        return self.store.update(mutate)

    def heartbeat(self, node_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if node_id not in state["local_nodes"]:
                raise HTTPException(status_code=404, detail=f"node not found: {node_id}")
            record = state["local_nodes"][node_id]
            record["status"] = str(payload.get("status") or "online")
            record["last_seen_at"] = now_iso()
            if payload.get("version") is not None:
                record["version"] = str(payload.get("version") or "")
            if isinstance(payload.get("metrics"), dict):
                record["metrics"] = payload["metrics"]
            return public_node(record)

        return self.store.update(mutate)

    def list_nodes(self) -> list[dict[str, Any]]:
        nodes = self.store.read().get("local_nodes", {})
        return sorted((public_node(item) for item in nodes.values()), key=lambda item: str(item.get("node_id") or ""))


class CommandService:
    def __init__(self, store: VpsAdminStore) -> None:
        self.store = store

    def create_command(self, payload: dict[str, Any], *, actor: AuthSession, command_type: str | None = None) -> dict[str, Any]:
        command_id = str(payload.get("command_id") or make_id("cmd"))
        tenant_id = active_tenant_id(payload.get("tenant_id"))
        record = {
            "command_id": command_id,
            "type": str(command_type or payload.get("type") or ""),
            "tenant_id": tenant_id,
            "node_id": str(payload.get("node_id") or ""),
            "payload": payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
            "status": "queued",
            "attempts": 0,
            "created_by": actor.user.user_id,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        if not record["type"]:
            raise HTTPException(status_code=400, detail="command type required")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            ensure_tenant_exists(state, tenant_id)
            if command_id in state["commands"]:
                raise HTTPException(status_code=409, detail=f"command already exists: {command_id}")
            if record["node_id"] and record["node_id"] not in state["local_nodes"]:
                raise HTTPException(status_code=404, detail=f"node not found: {record['node_id']}")
            state["commands"][command_id] = record
            append_audit(state, actor_id=actor.user.user_id, action="create_command", target_type="command", target_id=command_id)
            return record

        return self.store.update(mutate)

    def poll(self, *, tenant_id: str, node_id: str = "") -> list[dict[str, Any]]:
        tenant = active_tenant_id(tenant_id)

        def mutate(state: dict[str, Any]) -> list[dict[str, Any]]:
            if node_id and node_id in state["local_nodes"]:
                node = state["local_nodes"][node_id]
                if tenant not in node.get("tenant_ids", []):
                    raise HTTPException(status_code=403, detail="node is not authorized for this tenant")
                node["status"] = "online"
                node["last_seen_at"] = now_iso()
            commands: list[dict[str, Any]] = []
            for record in state["commands"].values():
                if record.get("tenant_id") != tenant:
                    continue
                if record.get("status") not in {"queued", "sent"}:
                    continue
                if record.get("node_id") and record.get("node_id") != node_id:
                    continue
                record["status"] = "sent"
                record["attempts"] = int(record.get("attempts") or 0) + 1
                record["sent_at"] = now_iso()
                record["updated_at"] = now_iso()
                commands.append(command_payload(record))
            return commands

        return self.store.update(mutate)

    def submit_result(self, command_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if command_id not in state["commands"]:
                raise HTTPException(status_code=404, detail=f"command not found: {command_id}")
            record = state["commands"][command_id]
            accepted = payload.get("accepted")
            ok = payload.get("ok")
            if ok is None:
                ok = accepted is not False and not payload.get("error")
            record["status"] = "succeeded" if ok else "failed"
            record["result"] = payload.get("result") if isinstance(payload.get("result"), dict) else payload
            record["error"] = str(payload.get("error") or "")
            record["completed_at"] = now_iso()
            record["updated_at"] = now_iso()
            result_record = {
                "command_id": command_id,
                "status": record["status"],
                "tenant_id": record.get("tenant_id"),
                "node_id": record.get("node_id"),
                "payload": payload,
                "created_at": now_iso(),
            }
            state.setdefault("command_results", []).append(result_record)
            append_audit(state, actor_id="local-node", action="command_result", target_type="command", target_id=command_id)
            return record

        return self.store.update(mutate)

    def list_commands(self) -> list[dict[str, Any]]:
        commands = self.store.read().get("commands", {})
        return sorted(commands.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)


class SharedKnowledgeService:
    def __init__(self, store: VpsAdminStore) -> None:
        self.store = store

    def industry_catalog(self) -> list[dict[str, Any]]:
        return industry_catalog()

    def list_library_items(self, *, include_inactive: bool = False, industry_id: str = "") -> list[dict[str, Any]]:
        def mutate(state: dict[str, Any]) -> list[dict[str, Any]]:
            seed_shared_library_if_empty(state)
            items = list(state.get("shared_library", {}).values())
            if not include_inactive:
                items = [item for item in items if str(item.get("status") or "active") == "active"]
            industry_filter = normalize_shared_item_industry_id(industry_id) if str(industry_id or "").strip() else GLOBAL_INDUSTRY_ID
            items = [
                item
                for item in items
                if normalize_shared_item_industry_id(item.get("industry_id") or GLOBAL_INDUSTRY_ID) == industry_filter
            ]
            normalized = [public_shared_library_record(item) for item in items]
            return sorted(normalized, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)

        return self.store.update(mutate)

    def official_snapshot(self, *, tenant_id: str = "", since_version: str = "") -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            seed_local_customer_accounts(state)
            seed_shared_library_if_empty(state)
            ensure_tenant_industry_bindings(state)
            return build_official_shared_knowledge_snapshot(state, tenant_id=tenant_id, since_version=since_version)

        return self.store.update(mutate)

    def get_library_item(self, item_id: str) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            seed_shared_library_if_empty(state)
            record = state.get("shared_library", {}).get(item_id)
            if not isinstance(record, dict):
                raise HTTPException(status_code=404, detail=f"shared library item not found: {item_id}")
            return public_shared_library_record(record)

        return self.store.update(mutate)

    def create_library_item(self, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        record = build_shared_library_record(payload, actor_id=actor.user.user_id)

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            seed_shared_library_if_empty(state)
            if record["item_id"] in state["shared_library"]:
                raise HTTPException(status_code=409, detail=f"shared library item already exists: {record['item_id']}")
            state["shared_library"][record["item_id"]] = record
            append_audit(
                state,
                actor_id=actor.user.user_id,
                action="create_shared_library_item",
                target_type="shared_library_item",
                target_id=record["item_id"],
            )
            return public_shared_library_record(record)

        return self.store.update(mutate)

    def update_library_item(self, item_id: str, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            seed_shared_library_if_empty(state)
            if item_id not in state["shared_library"]:
                raise HTTPException(status_code=404, detail=f"shared library item not found: {item_id}")
            record = state["shared_library"][item_id]
            for key in ("category_id", "title", "content", "status", "source", "tenant_id"):
                if key in payload:
                    record[key] = str(payload.get(key) or "")
            if "industry_id" in payload or "industry" in payload:
                record["industry_id"] = normalize_shared_item_industry_id(payload.get("industry_id") or payload.get("industry"))
            if isinstance(payload.get("data"), dict):
                record["data"] = payload["data"]
            if "keywords" in payload:
                record["keywords"] = normalize_text_list(payload.get("keywords"))
            if "applies_to" in payload:
                record["applies_to"] = str(payload.get("applies_to") or "")
            if "notes" in payload:
                record["notes"] = str(payload.get("notes") or "")
            record["updated_by"] = actor.user.user_id
            record["updated_at"] = now_iso()
            append_audit(
                state,
                actor_id=actor.user.user_id,
                action="update_shared_library_item",
                target_type="shared_library_item",
                target_id=item_id,
            )
            return public_shared_library_record(record)

        return self.store.update(mutate)

    def delete_library_item(self, item_id: str, *, actor: AuthSession) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            seed_shared_library_if_empty(state)
            if item_id not in state["shared_library"]:
                raise HTTPException(status_code=404, detail=f"shared library item not found: {item_id}")
            record = state["shared_library"].pop(item_id)
            append_audit(
                state,
                actor_id=actor.user.user_id,
                action="delete_shared_library_item",
                target_type="shared_library_item",
                target_id=item_id,
            )
            return record

        return self.store.update(mutate)

    def submit_proposal(self, payload: dict[str, Any], *, actor_id: str = "local-node") -> dict[str, Any]:
        proposal_id = str(payload.get("proposal_id") or make_id("proposal"))
        tenant_id = active_tenant_id(payload.get("tenant_id"))
        operations = payload.get("operations")
        if not isinstance(operations, list) or not operations:
            raise HTTPException(status_code=400, detail="proposal operations required")
        source_meta = payload.get("source_meta") if isinstance(payload.get("source_meta"), dict) else {}
        record = {
            "proposal_id": proposal_id,
            "tenant_id": tenant_id,
            "title": str(payload.get("title") or proposal_id),
            "summary": str(payload.get("summary") or ""),
            "operations": operations,
            "source": str(payload.get("source") or "local"),
            "source_meta": source_meta,
            "status": "pending_review",
            "created_by": actor_id,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        if isinstance(payload.get("review_assist"), dict):
            record["review_assist"] = normalize_shared_review_assist(payload.get("review_assist"), provider="submitted_review_assist")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            ensure_tenant_exists(state, tenant_id)
            seed_shared_library_if_empty(state)
            duplicate = find_duplicate_shared_proposal(record, state)
            if duplicate:
                return {**duplicate, "skip_reason": "already_pending_or_reviewed"}
            if shared_proposal_matches_library(record, state.get("shared_library", {}).values()):
                existing = {
                    **record,
                    "status": "skipped",
                    "skip_reason": "already_in_shared_library",
                    "updated_at": now_iso(),
                }
                return existing
            if "review_assist" not in record:
                record["review_assist"] = build_shared_proposal_review_assist(
                    record,
                    state.get("shared_library", {}).values(),
                    use_llm=True,
                )
            state["shared_proposals"][proposal_id] = record
            append_audit(state, actor_id=actor_id, action="submit_shared_proposal", target_type="shared_proposal", target_id=proposal_id)
            return record

        return self.store.update(mutate)

    def list_proposals(self) -> list[dict[str, Any]]:
        proposals = self.store.read().get("shared_proposals", {})
        return sorted(proposals.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)

    def generate_universal_proposals(self, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        use_llm = payload.get("use_llm", True) is not False
        limit = clamp_int(payload.get("limit"), default=30, minimum=1, maximum=200)
        requested_tenant = active_tenant_id(payload.get("tenant_id")) if str(payload.get("tenant_id") or "").strip() else ""
        only_unscanned = payload.get("only_unscanned", True) is not False
        rescan = bool(payload.get("rescan") or payload.get("force"))

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            seed_local_customer_accounts(state)
            seed_shared_library_if_empty(state)
            scan_state = state.setdefault("shared_scan_state", {})
            collected_entries = collect_universal_formal_entries(state, limit=limit, tenant_id=requested_tenant)
            entries = filter_unscanned_formal_entries(collected_entries, scan_state, only_unscanned=only_unscanned, rescan=rescan)
            suggestions = build_universal_shared_suggestions(entries, use_llm=use_llm)
            created: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            suggested_formal_keys: set[str] = set()
            for suggestion in suggestions:
                source_key = str(suggestion.get("source_key") or "")
                formal_keys = formal_source_keys_for_suggestion(suggestion)
                suggested_formal_keys.update(formal_keys)
                duplicate = find_duplicate_shared_suggestion(suggestion, state)
                if duplicate:
                    reason = str(duplicate.get("reason") or "duplicate")
                    skipped.append({"source_key": source_key, "reason": reason})
                    mark_shared_scan_state(
                        scan_state,
                        formal_keys,
                        status=reason,
                        detail={"proposal_id": duplicate.get("proposal_id"), "library_item_id": duplicate.get("library_item_id")},
                        use_llm=use_llm,
                    )
                    continue
                proposal_id = "proposal_shared_" + stable_digest(f"{source_key}:{suggestion.get('title')}", 18)
                content = build_shared_content_from_suggestion(suggestion)
                proposal = {
                    "proposal_id": proposal_id,
                    "tenant_id": str(suggestion.get("tenant_id") or DEFAULT_TENANT_ID),
                    "title": str(suggestion.get("title") or proposal_id),
                    "summary": str(suggestion.get("summary") or "AI 从客户正式知识库提炼出的通用共享知识候选。"),
                    "operations": [
                        {
                            "op": "upsert_json",
                            "path": f"{content['category_id']}/items/{content['id']}.json",
                            "content": content,
                        }
                    ],
                    "source": str(suggestion.get("provider") or "formal_knowledge_universal_extraction"),
                    "source_meta": {
                        "source_key": source_key,
                        "source_items": suggestion.get("source_items", []),
                        "universal_reason": suggestion.get("universal_reason", ""),
                        "llm_used": bool(suggestion.get("llm_used")),
                    },
                    "status": "pending_review",
                    "created_by": actor.user.user_id,
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                }
                proposal["review_assist"] = build_shared_proposal_review_assist(
                    proposal,
                    state.get("shared_library", {}).values(),
                    use_llm=use_llm,
                )
                state["shared_proposals"][proposal_id] = proposal
                mark_shared_scan_state(
                    scan_state,
                    formal_keys,
                    status="proposed",
                    detail={"proposal_id": proposal_id, "source_key": source_key},
                    use_llm=use_llm,
                )
                created.append(proposal)
            not_recommended_keys = [
                str(entry.get("source_key") or "")
                for entry in entries
                if str(entry.get("source_key") or "") and str(entry.get("source_key") or "") not in suggested_formal_keys
            ]
            mark_shared_scan_state(
                scan_state,
                not_recommended_keys,
                status="not_recommended",
                detail={"reason": "LLM/规则判断不适合进入共享公共知识候选"},
                use_llm=use_llm,
            )
            append_audit(
                state,
                actor_id=actor.user.user_id,
                action="generate_shared_universal_proposals",
                target_type="shared_proposal",
                target_id="bulk",
                detail={
                    "created_count": len(created),
                    "skipped_count": len(skipped),
                    "entry_count": len(entries),
                    "collected_count": len(collected_entries),
                    "only_unscanned": only_unscanned,
                    "use_llm": use_llm,
                },
            )
            return {
                "created": created,
                "skipped": skipped,
                "entry_count": len(entries),
                "collected_count": len(collected_entries),
                "scan": {
                    "only_unscanned": only_unscanned,
                    "rescan": rescan,
                    "checked_count": len(entries),
                    "newly_marked_not_recommended": len(not_recommended_keys),
                    "scan_state_count": len(scan_state),
                },
                "provider": suggestions[0].get("provider") if suggestions else "none",
            }

        return self.store.update(mutate)

    def refresh_proposal_review_assist(self, proposal_id: str, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        use_llm = payload.get("use_llm", True) is not False

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if proposal_id not in state["shared_proposals"]:
                raise HTTPException(status_code=404, detail=f"proposal not found: {proposal_id}")
            seed_shared_library_if_empty(state)
            proposal = state["shared_proposals"][proposal_id]
            proposal["review_assist"] = build_shared_proposal_review_assist(
                proposal,
                state.get("shared_library", {}).values(),
                use_llm=use_llm,
            )
            proposal["updated_at"] = now_iso()
            append_audit(
                state,
                actor_id=actor.user.user_id,
                action="refresh_shared_proposal_review_assist",
                target_type="shared_proposal",
                target_id=proposal_id,
                detail={"use_llm": use_llm},
            )
            return {"proposal": proposal, "review_assist": proposal.get("review_assist")}

        return self.store.update(mutate)

    def review_proposal(self, proposal_id: str, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        action = str(payload.get("action") or "").strip().lower()
        if action not in {"accept", "reject", "void"}:
            raise HTTPException(status_code=400, detail="review action must be accept, reject, or void")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if proposal_id not in state["shared_proposals"]:
                raise HTTPException(status_code=404, detail=f"proposal not found: {proposal_id}")
            proposal = state["shared_proposals"][proposal_id]
            seed_shared_library_if_empty(state)
            if not isinstance(proposal.get("review_assist"), dict):
                proposal["review_assist"] = build_shared_proposal_review_assist(
                    proposal,
                    state.get("shared_library", {}).values(),
                    use_llm=payload.get("use_llm", True) is not False,
                )
            proposal["reviewed_by"] = actor.user.user_id
            proposal["reviewed_at"] = now_iso()
            proposal["review_note"] = str(payload.get("note") or "")
            proposal["updated_at"] = now_iso()
            formal_keys = shared_proposal_formal_source_keys(proposal)
            if action == "accept":
                patch_id = str(payload.get("patch_id") or make_id("patch"))
                patch = {
                    "schema_version": 1,
                    "patch_id": patch_id,
                    "version": str(payload.get("version") or f"shared-{now_iso()}"),
                    "source_proposal_id": proposal_id,
                    "tenant_id": proposal.get("tenant_id"),
                    "operations": proposal.get("operations", []),
                    "status": "published",
                    "created_by": actor.user.user_id,
                    "created_at": now_iso(),
                }
                validate_shared_patch_payload(patch)
                library_items = upsert_shared_library_from_operations(
                    state,
                    proposal.get("operations", []),
                    actor_id=actor.user.user_id,
                    source=f"proposal:{proposal_id}",
                    tenant_id=str(proposal.get("tenant_id") or ""),
                )
                if not library_items:
                    raise HTTPException(status_code=400, detail="proposal did not produce shared library items")
                patch["library_item_ids"] = [item.get("item_id") for item in library_items]
                patch = sign_shared_patch_if_configured(patch)
                state["shared_patches"][patch_id] = patch
                proposal["status"] = "accepted"
                proposal["patch_id"] = patch_id
                mark_shared_scan_state(
                    state.setdefault("shared_scan_state", {}),
                    formal_keys,
                    status="accepted",
                    detail={"proposal_id": proposal_id, "patch_id": patch_id},
                    use_llm=bool((proposal.get("source_meta") or {}).get("llm_used")),
                )
                append_audit(state, actor_id=actor.user.user_id, action="accept_shared_proposal", target_type="shared_proposal", target_id=proposal_id)
                return {"proposal": proposal, "patch": patch, "library_items": library_items}
            proposal["status"] = "rejected" if action == "reject" else "void"
            mark_shared_scan_state(
                state.setdefault("shared_scan_state", {}),
                formal_keys,
                status=proposal["status"],
                detail={"proposal_id": proposal_id},
                use_llm=bool((proposal.get("source_meta") or {}).get("llm_used")),
            )
            append_audit(state, actor_id=actor.user.user_id, action=f"{action}_shared_proposal", target_type="shared_proposal", target_id=proposal_id)
            return {"proposal": proposal, "patch": None, "library_items": []}

        return self.store.update(mutate)

    def list_patches(self, *, limit: int | None = None, include_delivery: bool = True) -> list[dict[str, Any]]:
        state = self.store.read()
        patches = sorted(state.get("shared_patches", {}).values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)
        if limit is not None:
            patches = patches[: max(1, int(limit))]
        return [public_shared_patch_record(item, state, include_delivery=include_delivery) for item in patches if isinstance(item, dict)]

    def push_patch(self, patch_id: str, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        target_tenant = str(payload.get("tenant_id") or "").strip()
        target_node = str(payload.get("node_id") or "").strip()

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            patch = state.get("shared_patches", {}).get(patch_id)
            if not isinstance(patch, dict):
                raise HTTPException(status_code=404, detail=f"shared patch not found: {patch_id}")
            validate_shared_patch_payload(patch, require_signature=bool(os.getenv("WECHAT_SHARED_PATCH_SECRET", "").strip()))
            nodes = select_target_nodes(state, tenant_id=target_tenant, node_id=target_node)
            if not nodes:
                raise HTTPException(status_code=404, detail="no matching local nodes to push patch")
            commands = []
            for node in nodes:
                tenant_ids = [active_tenant_id(item) for item in node.get("tenant_ids", []) if str(item).strip()]
                command_tenant = active_tenant_id(target_tenant or (tenant_ids[0] if tenant_ids else DEFAULT_TENANT_ID))
                command = command_record(
                    command_type="pull_shared_patch",
                    tenant_id=command_tenant,
                    node_id=str(node.get("node_id") or ""),
                    payload={
                        "patch_id": patch_id,
                        "version": patch.get("version"),
                        "patch": patch,
                        "apply": True,
                    },
                    actor_id=actor.user.user_id,
                )
                state["commands"][command["command_id"]] = command
                commands.append(command)
            patch["last_pushed_at"] = now_iso()
            patch["last_push_command_ids"] = [command.get("command_id") for command in commands]
            patch["last_push_target"] = {"tenant_id": target_tenant, "node_id": target_node}
            patch["updated_at"] = now_iso()
            append_audit(
                state,
                actor_id=actor.user.user_id,
                action="push_shared_patch",
                target_type="shared_patch",
                target_id=patch_id,
                detail={"command_count": len(commands), "tenant_id": target_tenant, "node_id": target_node},
            )
            return {"patch": public_shared_patch_record(patch, state), "commands": commands, "delivery": shared_patch_delivery_status(patch, state)}

        return self.store.update(mutate)

    def overview(self) -> dict[str, Any]:
        state = self.store.read()
        snapshots = sorted(state.get("shared_snapshots", {}).values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)
        official = self.official_snapshot()
        return {
            "local": official,
            "official": official,
            "industry_catalog": industry_catalog(),
            "local_legacy": build_shared_knowledge_snapshot(),
            "snapshots": snapshots,
            "latest_snapshot": snapshots[0] if snapshots else None,
        }

    def sync_local_snapshot(self, *, actor: AuthSession) -> dict[str, Any]:
        snapshot = self.official_snapshot()
        snapshot_id = str("shared_snapshot_" + make_id("sync"))
        record = {
            "snapshot_id": snapshot_id,
            "source": "cloud_official_shared_library",
            "summary": {
                "category_count": len(snapshot.get("categories", [])),
                "item_count": len(snapshot.get("items", [])),
                "version": snapshot.get("version", ""),
            },
            "snapshot": snapshot,
            "created_by": actor.user.user_id,
            "created_at": now_iso(),
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            state["shared_snapshots"][snapshot_id] = record
            append_audit(state, actor_id=actor.user.user_id, action="sync_shared_knowledge", target_type="shared_snapshot", target_id=snapshot_id)
            return record

        return self.store.update(mutate)

    def draft_from_natural_language(self, description: str, *, actor: AuthSession, use_llm: bool = True) -> dict[str, Any]:
        """Use LLM to convert natural language into a structured shared-library draft."""
        if not description.strip():
            raise ValueError("描述不能为空")
        if not use_llm:
            return {"ok": True, "draft": None, "llm_used": False, "message": "LLM 已禁用，请手动填写表单。"}

        existing_items = self.list_library_items(include_inactive=True)
        existing_titles = [str(item.get("title") or item.get("item_id") or "") for item in existing_items[:100]]

        system_prompt = (
            "你是共享知识库整理专家。把管理员的自然语言描述转换成标准化的共享知识条目。"
            "只输出 JSON 对象，不要任何解释。"
            "共享知识是跨租户通用的客服规则、回复风格或风险管控原则，不能包含具体客户、具体商品、具体价格等专属信息。"
        )

        user_prompt = {
            "description": description,
            "existing_titles": existing_titles,
            "instructions": [
                "把描述整理成共享知识库的标准字段。",
                "category_id 必须是 global_guidelines（通用规范）、reply_style（回复风格）或 risk_control（风险管控）之一。",
                "如果信息不完整，把缺失的字段名放入 missing_fields，并给出 followup_question 引导管理员补充。",
                "如果描述包含具体客户/商品/价格等专属信息，给出 warnings 提示需要脱敏。",
                "只有当 title、content/guideline_text、category_id 都齐全时，status 才是 ready。",
            ],
            "response_format": {
                "title": "知识标题",
                "category_id": "global_guidelines|reply_style|risk_control",
                "industry_id": "global 或具体行业ID",
                "keywords": ["关键词1", "关键词2"],
                "applies_to": "适用场景描述",
                "content": "知识正文内容",
                "notes": "管理员备注",
                "status": "ready|draft",
                "missing_fields": [],
                "followup_question": "",
                "warnings": [],
                "reasoning": "整理思路",
            },
        }

        result = call_deepseek_json_with_prompt(
            system_prompt=system_prompt,
            user_prompt=json.dumps(user_prompt, ensure_ascii=False),
            temperature=0.15,
            max_tokens=1800,
        )

        draft = result if isinstance(result, dict) else {}
        if not draft:
            return {"ok": False, "draft": None, "llm_used": True, "message": "LLM 未能生成有效草稿。"}

        # Normalize draft into shared-library record shape
        normalized: dict[str, Any] = {
            "title": str(draft.get("title") or ""),
            "category_id": str(draft.get("category_id") or "global_guidelines"),
            "industry_id": str(draft.get("industry_id") or "global"),
            "keywords": draft.get("keywords") if isinstance(draft.get("keywords"), list) else [],
            "applies_to": str(draft.get("applies_to") or ""),
            "content": str(draft.get("content") or draft.get("guideline_text") or ""),
            "notes": str(draft.get("notes") or ""),
            "data": {
                "title": str(draft.get("title") or ""),
                "category_id": str(draft.get("category_id") or "global_guidelines"),
                "industry_id": str(draft.get("industry_id") or "global"),
                "keywords": draft.get("keywords") if isinstance(draft.get("keywords"), list) else [],
                "applies_to": str(draft.get("applies_to") or ""),
                "guideline_text": str(draft.get("content") or draft.get("guideline_text") or ""),
                "notes": str(draft.get("notes") or ""),
            },
            "status": str(draft.get("status") or "draft"),
            "missing_fields": draft.get("missing_fields") if isinstance(draft.get("missing_fields"), list) else [],
            "followup_question": str(draft.get("followup_question") or ""),
            "warnings": draft.get("warnings") if isinstance(draft.get("warnings"), list) else [],
            "reasoning": str(draft.get("reasoning") or ""),
        }

        is_ready = bool(normalized["status"] == "ready" and normalized["title"] and normalized["content"])
        return {
            "ok": True,
            "draft": normalized,
            "llm_used": True,
            "ready": is_ready,
            "message": "草稿已生成。" + ("请核对后确认入库。" if is_ready else "请先补充缺失信息。"),
        }


class BackupRestoreService:
    def __init__(self, store: VpsAdminStore, commands: CommandService) -> None:
        self.store = store
        self.commands = commands

    def list_backup_requests(self) -> list[dict[str, Any]]:
        state = self.store.read()
        return sorted(state.get("backup_requests", {}).values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)

    def delete_backup_request(self, request_id: str, *, actor: AuthSession) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if request_id not in state["backup_requests"]:
                raise HTTPException(status_code=404, detail=f"backup request not found: {request_id}")
            record = state["backup_requests"].pop(request_id)
            backup_id = str(record.get("backup_id") or "")
            removed_packages: list[dict[str, Any]] = []
            if backup_id:
                for package_id, package in list(state.get("customer_data_packages", {}).items()):
                    if str(package.get("backup_id") or "") == backup_id:
                        removed_packages.append(state["customer_data_packages"].pop(package_id))
            append_audit(
                state,
                actor_id=actor.user.user_id,
                action="delete_backup_request",
                target_type="backup_request",
                target_id=request_id,
                detail={"backup_id": backup_id, "removed_package_count": len(removed_packages)},
            )
            return {"request": record, "packages": removed_packages}

        result = self.store.update(mutate)
        deleted_files = []
        for item in [result["request"], *result["packages"]]:
            if delete_managed_file(Path(str(item.get("package_path") or ""))):
                deleted_files.append(str(item.get("package_path")))
        return {**result, "deleted_files": deleted_files}

    def list_restore_requests(self) -> list[dict[str, Any]]:
        state = self.store.read()
        return sorted(state.get("restore_requests", {}).values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)

    def request_backup(self, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        scope = str(payload.get("scope") or "tenant").strip().lower()
        if scope not in {"tenant", "all"}:
            raise HTTPException(status_code=400, detail="backup scope must be tenant or all")
        command_type = "backup_all" if scope == "all" else "backup_tenant"
        command = self.commands.create_command(payload, actor=actor, command_type=command_type)
        request_id = make_id("backup")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            record = {
                "request_id": request_id,
                "scope": scope,
                "tenant_id": command.get("tenant_id"),
                "node_id": command.get("node_id"),
                "command_id": command.get("command_id"),
                "status": "queued",
                "created_by": actor.user.user_id,
                "created_at": now_iso(),
            }
            state["backup_requests"][request_id] = record
            append_audit(state, actor_id=actor.user.user_id, action="request_backup", target_type="backup_request", target_id=request_id)
            return {"request": record, "command": command}

        return self.store.update(mutate)

    def build_local_backup_now(self, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        scope = str(payload.get("scope") or "all").strip().lower()
        if scope not in {"tenant", "shared", "all"}:
            raise HTTPException(status_code=400, detail="backup scope must be tenant, shared, or all")
        tenant_id = active_tenant_id(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        backup = BackupService(output_root=runtime_app_root() / "vps_admin" / "backups").build_backup(
            scope=scope,
            tenant_id=tenant_id,
            include_derived=True,
            include_runtime=True,
        )
        request_id = make_id("backup")
        package_id = f"data_pkg_{backup['backup_id']}"
        summary = build_tenant_data_summary(tenant_id) if scope in {"tenant", "all"} else build_shared_knowledge_snapshot()

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if scope != "shared":
                ensure_tenant_exists(state, tenant_id)
            record = {
                "request_id": request_id,
                "scope": scope,
                "tenant_id": tenant_id,
                "node_id": "",
                "command_id": "",
                "status": "succeeded",
                "mode": "local_immediate",
                "backup_id": backup["backup_id"],
                "package_path": backup["package_path"],
                "bytes": backup["bytes"],
                "created_by": actor.user.user_id,
                "created_at": now_iso(),
            }
            state["backup_requests"][request_id] = record
            state["customer_data_packages"][package_id] = {
                "package_id": package_id,
                "account_username": str(payload.get("account_username") or ""),
                "tenant_id": tenant_id,
                "scope": scope,
                "backup_id": backup["backup_id"],
                "package_path": backup["package_path"],
                "bytes": backup["bytes"],
                "summary": summary,
                "manifest": backup["manifest"],
                "created_by": actor.user.user_id,
                "created_at": now_iso(),
            }
            append_audit(state, actor_id=actor.user.user_id, action="local_backup_now", target_type="backup_request", target_id=request_id)
            return {"request": record, "backup": backup, "package": state["customer_data_packages"][package_id]}

        return self.store.update(mutate)

    def request_restore(self, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        if not payload.get("backup_id") and not payload.get("backup_url"):
            raise HTTPException(status_code=400, detail="backup_id or backup_url required")
        command = self.commands.create_command({**payload, "payload": payload}, actor=actor, command_type="restore_backup")
        request_id = make_id("restore")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            record = {
                "request_id": request_id,
                "tenant_id": command.get("tenant_id"),
                "node_id": command.get("node_id"),
                "backup_id": str(payload.get("backup_id") or ""),
                "backup_url": str(payload.get("backup_url") or ""),
                "dry_run": bool(payload.get("dry_run", True)),
                "command_id": command.get("command_id"),
                "status": "queued",
                "created_by": actor.user.user_id,
                "created_at": now_iso(),
            }
            state["restore_requests"][request_id] = record
            append_audit(state, actor_id=actor.user.user_id, action="request_restore", target_type="restore_request", target_id=request_id)
            return {"request": record, "command": command}

        return self.store.update(mutate)

    def request_restore_latest(self, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        scope = str(payload.get("scope") or "all").strip().lower()
        tenant_id = active_tenant_id(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        state = self.store.read()
        packages = [
            item
            for item in state.get("customer_data_packages", {}).values()
            if item.get("scope") == scope and (scope == "shared" or item.get("tenant_id") == tenant_id)
        ]
        packages = sorted(packages, key=lambda item: str(item.get("created_at") or ""), reverse=True)
        if not packages:
            raise HTTPException(status_code=404, detail="no matching backup package found")
        latest = packages[0]
        return self.request_restore(
            {
                "tenant_id": tenant_id,
                "node_id": str(payload.get("node_id") or ""),
                "backup_id": latest.get("backup_id"),
                "backup_url": latest.get("package_path"),
                "dry_run": payload.get("dry_run", True) is not False,
            },
            actor=actor,
        )


class ReleaseService:
    def __init__(self, store: VpsAdminStore) -> None:
        self.store = store

    def create_release(self, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        release_id = str(payload.get("release_id") or make_id("release"))
        channel = str(payload.get("channel") or "stable")
        record = {
            "release_id": release_id,
            "channel": channel,
            "version": str(payload.get("version") or ""),
            "title": str(payload.get("title") or ""),
            "notes": str(payload.get("notes") or ""),
            "artifact_url": str(payload.get("artifact_url") or ""),
            "sha256": str(payload.get("sha256") or ""),
            "signature": str(payload.get("signature") or ""),
            "status": str(payload.get("status") or "published"),
            "created_by": actor.user.user_id,
            "created_at": now_iso(),
        }
        if not record["version"]:
            raise HTTPException(status_code=400, detail="release version required")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            state["releases"][release_id] = record
            append_audit(state, actor_id=actor.user.user_id, action="create_release", target_type="release", target_id=release_id)
            return record

        return self.store.update(mutate)

    def list_releases(self) -> list[dict[str, Any]]:
        releases = self.store.read().get("releases", {})
        return sorted(releases.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)

    def latest(self, *, channel: str = "stable") -> dict[str, Any] | None:
        for release in self.list_releases():
            if release.get("channel") == channel and release.get("status") == "published":
                return release
        return None

    def push_release(self, release_id: str, payload: dict[str, Any], *, actor: AuthSession) -> dict[str, Any]:
        target_tenant = str(payload.get("tenant_id") or "").strip()
        target_node = str(payload.get("node_id") or "").strip()
        mode = str(payload.get("mode") or "check_update").strip()
        if mode not in {"check_update", "push_update"}:
            raise HTTPException(status_code=400, detail="release push mode must be check_update or push_update")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            release = state.get("releases", {}).get(release_id)
            if not isinstance(release, dict):
                raise HTTPException(status_code=404, detail=f"release not found: {release_id}")
            nodes = select_target_nodes(state, tenant_id=target_tenant, node_id=target_node)
            if not nodes:
                raise HTTPException(status_code=404, detail="no matching local nodes to push release")
            commands = []
            for node in nodes:
                tenant_ids = [active_tenant_id(item) for item in node.get("tenant_ids", []) if str(item).strip()]
                command_tenant = active_tenant_id(target_tenant or (tenant_ids[0] if tenant_ids else DEFAULT_TENANT_ID))
                command = command_record(
                    command_type=mode,
                    tenant_id=command_tenant,
                    node_id=str(node.get("node_id") or ""),
                    payload={"release_id": release_id, "release": release},
                    actor_id=actor.user.user_id,
                )
                state["commands"][command["command_id"]] = command
                commands.append(command)
            append_audit(
                state,
                actor_id=actor.user.user_id,
                action="push_release",
                target_type="release",
                target_id=release_id,
                detail={"command_count": len(commands), "mode": mode, "tenant_id": target_tenant, "node_id": target_node},
            )
            return {"release": release, "commands": commands}

        return self.store.update(mutate)


def public_node(record: dict[str, Any], *, include_token: bool = False) -> dict[str, Any]:
    if include_token:
        return dict(record)
    return {key: value for key, value in record.items() if key != "node_token"}


def command_record(
    *,
    command_type: str,
    tenant_id: str,
    node_id: str,
    payload: dict[str, Any],
    actor_id: str,
) -> dict[str, Any]:
    return {
        "command_id": make_id("cmd"),
        "type": command_type,
        "tenant_id": active_tenant_id(tenant_id),
        "node_id": node_id,
        "payload": payload if isinstance(payload, dict) else {},
        "status": "queued",
        "attempts": 0,
        "created_by": actor_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


def public_shared_patch_record(patch: dict[str, Any], state: dict[str, Any], *, include_delivery: bool = True) -> dict[str, Any]:
    record = dict(patch)
    signature_required = bool(os.getenv("WECHAT_SHARED_PATCH_SECRET", "").strip())
    if record.get("signature"):
        signature_status = "signed"
        signature_label = "已签名"
    elif signature_required:
        signature_status = "unsigned_required"
        signature_label = "未签名，不能推送"
    else:
        signature_status = "unsigned_allowed"
        signature_label = "本地模式未签名"
    record["signature_required"] = signature_required
    record["signature_status"] = signature_status
    record["signature_label"] = signature_label
    if include_delivery:
        record["delivery"] = shared_patch_delivery_status(record, state)
    return record


def shared_patch_delivery_status(patch: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    patch_id = str(patch.get("patch_id") or "")
    related_commands = [
        item
        for item in state.get("commands", {}).values()
        if isinstance(item, dict)
        and str(item.get("type") or "") == "pull_shared_patch"
        and str((item.get("payload") if isinstance(item.get("payload"), dict) else {}).get("patch_id") or "") == patch_id
    ]
    related_commands = sorted(related_commands, key=lambda item: str(item.get("created_at") or ""), reverse=True)
    targets = [shared_patch_command_target(item, state) for item in related_commands]
    counts = {
        "total": len(targets),
        "queued": sum(1 for item in targets if item.get("delivery_status") == "queued"),
        "sent": sum(1 for item in targets if item.get("delivery_status") == "sent"),
        "applied": sum(1 for item in targets if item.get("delivery_status") == "applied"),
        "failed": sum(1 for item in targets if item.get("delivery_status") == "failed"),
    }
    if counts["total"] == 0:
        overall_status = "not_pushed"
    elif counts["failed"]:
        overall_status = "failed"
    elif counts["applied"] == counts["total"]:
        overall_status = "applied"
    elif counts["sent"] or counts["queued"]:
        overall_status = "pending"
    else:
        overall_status = "pending"
    return {
        "patch_id": patch_id,
        "overall_status": overall_status,
        "counts": counts,
        "targets": targets,
        "last_pushed_at": patch.get("last_pushed_at") or "",
        "last_push_command_ids": patch.get("last_push_command_ids") if isinstance(patch.get("last_push_command_ids"), list) else [],
    }


def shared_patch_command_target(command: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    node_id = str(command.get("node_id") or "")
    tenant_id = active_tenant_id(command.get("tenant_id"))
    node = state.get("local_nodes", {}).get(node_id) if node_id else None
    if not isinstance(node, dict):
        node = {}
    tenant = state.get("tenants", {}).get(tenant_id)
    if not isinstance(tenant, dict):
        tenant = {}
    status = str(command.get("status") or "queued")
    delivery_status = {
        "queued": "queued",
        "sent": "sent",
        "succeeded": "applied",
        "failed": "failed",
    }.get(status, status or "queued")
    result = command.get("result") if isinstance(command.get("result"), dict) else {}
    return {
        "command_id": command.get("command_id"),
        "tenant_id": tenant_id,
        "tenant_name": tenant.get("display_name") or tenant_id,
        "node_id": node_id,
        "node_name": node.get("display_name") or node_id or "未指定客户端",
        "node_status": node.get("status") or "unknown",
        "node_last_seen_at": node.get("last_seen_at") or "",
        "command_status": status,
        "delivery_status": delivery_status,
        "attempts": int(command.get("attempts") or 0),
        "created_at": command.get("created_at") or "",
        "sent_at": command.get("sent_at") or "",
        "completed_at": command.get("completed_at") or "",
        "error": command.get("error") or "",
        "result": result,
    }


def select_target_nodes(state: dict[str, Any], *, tenant_id: str = "", node_id: str = "") -> list[dict[str, Any]]:
    nodes = [item for item in state.get("local_nodes", {}).values() if isinstance(item, dict)]
    if node_id:
        nodes = [item for item in nodes if str(item.get("node_id") or "") == node_id]
    if tenant_id:
        tenant = active_tenant_id(tenant_id)
        nodes = [
            item
            for item in nodes
            if tenant in [active_tenant_id(value) for value in item.get("tenant_ids", []) if str(value).strip()]
        ]
    return nodes


def customer_tenant_record(*, username: str, tenant_id: str, industry_id: str | None = None) -> dict[str, Any]:
    normalized_industry = normalize_industry_id(industry_id or default_industry_for_tenant(tenant_id))
    return {
        "tenant_id": active_tenant_id(tenant_id),
        "display_name": username,
        "status": "active",
        "sync_enabled": False,
        "industry_id": normalized_industry,
        "metadata": {"source": "customer_account", "account_username": username, "industry_id": normalized_industry},
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


def remove_local_customer_account_override(record: dict[str, Any]) -> bool:
    if not isinstance(record, dict):
        return False
    source = str(record.get("source") or "")
    role = str(record.get("role") or "")
    username = str(record.get("username") or "").strip()
    user_id = str(record.get("user_id") or "").strip()
    if source != "local_client_account" and role != Role.CUSTOMER.value:
        return False
    accounts = read_local_account_overrides_from_env()
    if not accounts:
        return False
    removable_keys: set[str] = set()
    for key, account in accounts.items():
        if not isinstance(account, dict):
            continue
        key_text = str(key or "").strip()
        account_user = str(account.get("username") or "").strip()
        account_id = str(account.get("user_id") or "").strip()
        if key_text in {username, user_id} or account_user in {username, user_id} or account_id in {username, user_id}:
            removable_keys.add(key_text)
    if not removable_keys:
        return False
    changed = False
    for key in removable_keys:
        if key in accounts:
            accounts.pop(key, None)
            changed = True
    if changed:
        write_local_account_overrides_to_env(accounts)
    return changed


def seed_local_customer_accounts(state: dict[str, Any]) -> None:
    """Mirror Local Console customer accounts into the VPS admin view."""
    accounts = read_local_account_overrides_from_env()
    for username, account in accounts.items():
        if not isinstance(account, dict) or str(account.get("role") or "") != Role.CUSTOMER.value:
            continue
        tenant_ids = [active_tenant_id(item) for item in account.get("tenant_ids", []) if str(item).strip()]
        if not tenant_ids:
            tenant_ids = [active_tenant_id(account.get("active_tenant_id") or username)]
        for tenant_id in tenant_ids:
            if tenant_id not in state.get("tenants", {}):
                state["tenants"][tenant_id] = local_customer_tenant_record(account, tenant_id)
        user_id = str(account.get("user_id") or username)
        existing = state.get("users", {}).get(user_id)
        if existing and str(existing.get("role") or "") != Role.CUSTOMER.value:
            continue
        record = {
            "user_id": user_id,
            "username": str(account.get("username") or username),
            "display_name": str(account.get("display_name") or account.get("username") or username),
            "email": normalize_email(str(account.get("email") or "")),
            "role": Role.CUSTOMER.value,
            "tenant_ids": tenant_ids,
            "resource_scopes": account.get("resource_scopes") if isinstance(account.get("resource_scopes"), list) else ["*"],
            "status": str(account.get("status") or "active"),
            "source": "local_client_account",
            "created_at": str(account.get("created_at") or account.get("initialized_at") or now_iso()),
            "updated_at": str(account.get("updated_at") or now_iso()),
        }
        if account.get("password_hash"):
            record["password_hash"] = str(account.get("password_hash") or "")
        if account.get("initialized_at"):
            record["initialized_at"] = str(account.get("initialized_at") or "")
        preserved: dict[str, Any] = {}
        if isinstance(existing, dict):
            if existing.get("password_hash") and not record.get("password_hash"):
                preserved["password_hash"] = existing.get("password_hash")
            if existing.get("initialized_at") and not record.get("initialized_at"):
                preserved["initialized_at"] = existing.get("initialized_at")
        state["users"][user_id] = {**record, **preserved}
    if TENANTS_ROOT.exists():
        for path in sorted(item for item in TENANTS_ROOT.iterdir() if item.is_dir()):
            tenant_id = active_tenant_id(path.name)
            if tenant_id not in state.get("tenants", {}):
                state["tenants"][tenant_id] = local_customer_tenant_record({}, tenant_id)
    ensure_tenant_industry_bindings(state)


def local_customer_tenant_record(account: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    metadata = read_tenant_metadata(tenant_id)
    username = str(account.get("username") or metadata.get("account_username") or tenant_id)
    display = str(account.get("display_name") or metadata.get("display_name") or metadata.get("name") or username)
    industry = resolve_tenant_industry_id(tenant_id, tenant_metadata=metadata)
    return {
        "tenant_id": active_tenant_id(tenant_id),
        "display_name": display,
        "status": "active",
        "sync_enabled": True,
        "industry_id": industry,
        "metadata": {"source": "local_client_account", "account_username": username, "industry_id": industry, **(metadata if isinstance(metadata, dict) else {})},
        "created_at": str(account.get("created_at") or account.get("initialized_at") or now_iso()),
        "updated_at": str(account.get("updated_at") or now_iso()),
    }


def ensure_tenant_industry_bindings(state: dict[str, Any]) -> None:
    tenants = state.get("tenants", {})
    if not isinstance(tenants, dict):
        return
    for tenant_id, record in tenants.items():
        if not isinstance(record, dict):
            continue
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        resolved = resolve_tenant_industry_id(str(tenant_id), tenant_record=record, tenant_metadata=metadata)
        record["industry_id"] = resolved
        record["metadata"] = {**metadata, "industry_id": resolved}


def infer_default_module_for_tenant(state: dict[str, Any], *, tenant_id: str, modules: list[dict[str, Any]]) -> str:
    active_module_keys = {
        str(item.get("module_key") or "")
        for item in modules
        if isinstance(item, dict) and str(item.get("status") or "active") == "active"
    }
    if not active_module_keys:
        return ""
    tenant_record = state.get("tenants", {}).get(tenant_id)
    tenant_metadata = tenant_record.get("metadata") if isinstance(tenant_record, dict) and isinstance(tenant_record.get("metadata"), dict) else {}
    industry_id = resolve_tenant_industry_id(tenant_id, tenant_record=tenant_record if isinstance(tenant_record, dict) else None, tenant_metadata=tenant_metadata)
    preferred = "order_sheet_lab_v1" if industry_id == "lab_instruments" else "raw_message_log_v1"
    if preferred in active_module_keys:
        return preferred
    return sorted(active_module_keys)[0]


def customer_username_for_tenant(state: dict[str, Any], tenant_id: str) -> str:
    tenant = active_tenant_id(tenant_id)
    for user in state.get("users", {}).values():
        if not isinstance(user, dict) or user.get("role") != Role.CUSTOMER.value:
            continue
        if tenant in [active_tenant_id(item) for item in user.get("tenant_ids", []) if str(item).strip()]:
            return str(user.get("username") or "")
    tenant_record = state.get("tenants", {}).get(tenant)
    if isinstance(tenant_record, dict):
        metadata = tenant_record.get("metadata") if isinstance(tenant_record.get("metadata"), dict) else {}
        return str(metadata.get("account_username") or tenant_record.get("display_name") or tenant)
    return tenant


def customer_user_by_username(state: dict[str, Any], username: str) -> dict[str, Any] | None:
    for user in state.get("users", {}).values():
        if not isinstance(user, dict):
            continue
        if user.get("role") != Role.CUSTOMER.value:
            continue
        if str(user.get("username") or "") == username:
            return user
    return None


def customer_user_by_tenant(state: dict[str, Any], tenant_id: str) -> dict[str, Any] | None:
    tenant = active_tenant_id(tenant_id)
    for user in state.get("users", {}).values():
        if not isinstance(user, dict):
            continue
        if user.get("role") != Role.CUSTOMER.value:
            continue
        tenant_ids = [active_tenant_id(item) for item in user.get("tenant_ids", []) if str(item).strip()]
        if tenant in tenant_ids:
            return user
    return None


UNIVERSAL_FORMAL_CATEGORIES = {"policies", "chats", "custom"}
PRODUCT_SPECIFIC_HINTS = {
    "product_id",
    "product_category",
    "product_ids",
    "sku",
    "vin",
    "stock_no",
    "inventory",
    "car_id",
    "车辆编号",
    "车架号",
    "库存",
    "在售",
    "已售",
}
TENANT_PRIVATE_FIELD_HINTS = {
    "customer_id",
    "customer_name",
    "customer_phone",
    "customer_wechat",
    "lead_id",
    "lead_name",
    "contact_name",
    "contact_phone",
    "phone",
    "mobile",
    "wechat",
    "address",
    "order_id",
    "contract_id",
    "conversation_id",
    "group_id",
    "session_id",
    "门店",
    "客户姓名",
    "手机号",
    "微信号",
    "联系人",
    "地址",
    "订单号",
}
PRODUCT_SPECIFIC_TEXT_HINTS = {
    "新能源",
    "电池检测",
    "首付",
    "月供",
    "车贷",
    "贷款包过",
    "金融方案",
    "按揭",
    "置换",
    "定金",
    "凯美瑞",
    "雅阁",
    "朗逸",
    "轩逸",
    "宝马",
    "奔驰",
    "奥迪",
    "丰田",
    "本田",
    "大众",
    "日产",
    "这台车",
    "该车",
    "车况",
    "公里",
    "万公里",
    "售价",
    "报价",
    "库存",
    "VIN",
    "二手车",
    "车辆",
    "车型",
    "车源",
    "看车",
    "试驾",
    "过户",
    "上牌",
    "检测报告",
    "事故",
    "水泡",
    "火烧",
}
TENANT_PRIVATE_TEXT_HINTS = {
    "杭州",
    "南京",
    "余杭",
    "江苏",
    "浙江",
    "仓库",
    "测试仓",
    "对公账户",
    "银行账户",
    "开户行",
    "税号",
    "客服电话",
    "售后电话",
    "营业执照",
    "开户许可证",
    "开票资料",
    "开票时效",
    "发货时效",
    "包邮",
    "满减",
    "促销",
    "活动规则",
    "供应商入库",
    "江苏车金",
    "销冠",
    "本公司",
    "我司",
    "本司",
    "我们公司",
    "本店",
    "门店",
    "线下",
    "直播间",
    "文件传输助手",
    "测试群",
    "偷数据测试",
    "客户姓名",
    "客户信息",
    "客户资料",
    "手机号",
    "联系电话",
    "微信号",
    "联系地址",
    "订单号",
    "合同号",
    "客户预算",
    "购车意向",
    "内部备注",
    "内部规则",
    "专属政策",
    "专属优惠",
}
STRICT_UNIVERSAL_TOPIC_HINTS = {
    "转人工",
    "转接人工",
    "人工接管",
    "人工客服",
    "人工确认",
    "人工审核",
    "人工处理",
    "请示",
    "不能直接承诺",
    "不能直接确认",
    "不能直接同意",
    "不能直接拍板",
    "不得承诺",
    "没有足够依据",
    "超出权限",
    "超出范围",
    "敏感",
    "隐私",
    "投诉",
    "退款",
    "赔偿",
    "法务",
    "违法",
    "违规",
    "风险",
    "人工客服",
    "转人工",
    "人工服务",
    "转接人工",
    "请稍等",
    "稍等片刻",
    "礼貌",
    "感谢",
    "抱歉",
    "无法确认",
    "不能确认",
    "不确定",
    "需要核实",
    "人工确认",
    "不要承诺",
    "不能承诺",
    "敏感信息",
    "隐私",
    "风险提示",
    "以人工确认为准",
}
PRIVATE_DATA_PATTERNS = (
    re.compile(r"1[3-9]\d{9}"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b\d{15,18}[0-9Xx]?\b"),
    re.compile(r"\d+(?:\.\d+)?\s*(?:元|万元|万|块|￥)"),
)

SHARED_SCAN_TERMINAL_STATUSES = {
    "proposed",
    "uploaded",
    "not_recommended",
    "already_pending_or_reviewed",
    "already_in_shared_library",
    "duplicate",
    "accepted",
    "rejected",
    "void",
}


def collect_universal_formal_entries(
    state: dict[str, Any],
    *,
    limit: int,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Collect tenant formal knowledge that may be safe to promote as shared public knowledge."""
    tenants = [active_tenant_id(tenant_id)] if tenant_id else universal_scan_tenant_ids(state)
    entries: list[dict[str, Any]] = []
    for tenant in tenants:
        kb_root = tenant_knowledge_base_root(tenant)
        if not kb_root.exists():
            continue
        for category_dir in sorted(path for path in kb_root.iterdir() if path.is_dir()):
            category_id = category_dir.name
            if category_id not in UNIVERSAL_FORMAL_CATEGORIES:
                continue
            items_dir = category_dir / "items"
            if not items_dir.exists():
                continue
            for path in sorted(items_dir.glob("*.json")):
                payload = read_json_file(path, default={})
                if not isinstance(payload, dict):
                    continue
                entry = formal_item_entry(tenant, category_id, path, payload)
                if not is_universal_formal_entry(entry):
                    continue
                entries.append(entry)
                if len(entries) >= limit:
                    return entries
    return entries


def universal_scan_tenant_ids(state: dict[str, Any]) -> list[str]:
    tenants: set[str] = set()
    for user in state.get("users", {}).values():
        if not isinstance(user, dict) or user.get("role") != Role.CUSTOMER.value:
            continue
        tenants.update(active_tenant_id(item) for item in user.get("tenant_ids", []) if str(item).strip())
    tenants.update(active_tenant_id(item) for item in state.get("tenants", {}).keys() if str(item).strip())
    return sorted(tenants)


def formal_item_entry(tenant_id: str, category_id: str, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    title = str(data.get("title") or data.get("name") or payload.get("title") or payload.get("id") or path.stem)
    body = formal_item_body(data)
    keywords = normalize_text_list(data.get("keywords") or data.get("intent_tags") or data.get("tone_tags"))
    source_key = f"{tenant_id}:{category_id}:{payload.get('id') or path.stem}:{stable_digest(title + body, 16)}"
    return {
        "tenant_id": active_tenant_id(tenant_id),
        "category_id": category_id,
        "item_id": str(payload.get("id") or path.stem),
        "path": str(path),
        "status": str(payload.get("status") or data.get("status") or "active"),
        "title": title,
        "body": body,
        "keywords": keywords,
        "data": data,
        "source_key": source_key,
    }


def formal_item_body(data: dict[str, Any]) -> str:
    parts = []
    for key in ("answer", "service_reply", "guideline_text", "content", "body", "customer_message"):
        value = data.get(key)
        if value:
            parts.append(str(value))
    return "\n".join(parts)


def is_universal_formal_entry(entry: dict[str, Any]) -> bool:
    if str(entry.get("status") or "active") != "active":
        return False
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    for key in PRODUCT_SPECIFIC_HINTS:
        if data.get(key):
            return False
    if has_private_structured_fields(data):
        return False
    scope = str(data.get("applicability_scope") or data.get("scope") or "").strip().lower()
    if scope in {"product", "product_specific", "item", "sku", "category_specific", "specific_product", "product_category"}:
        return False
    text = f"{entry.get('title')}\n{entry.get('body')}\n{' '.join(entry.get('keywords') or [])}"
    if any(hint in text for hint in PRODUCT_SPECIFIC_TEXT_HINTS):
        return False
    if looks_tenant_private_or_industry_specific(text):
        return False
    if not str(entry.get("body") or "").strip():
        return False
    return True


def build_universal_shared_suggestions(entries: list[dict[str, Any]], *, use_llm: bool) -> list[dict[str, Any]]:
    if not entries:
        return []
    if use_llm:
        llm_suggestions = llm_universal_shared_suggestions(entries)
        if llm_suggestions is not None:
            return llm_suggestions
    return heuristic_universal_shared_suggestions(entries)


def llm_universal_shared_suggestions(entries: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    prompt = {
        "task": "从每个客户的正式知识库中，严格筛选极少数真正适合进入候选共享公共知识库的内容。不要直接入正式库。",
        "rules": [
            "共享公共知识必须能安全给所有账号、所有行业使用；只保留跨行业通用客服原则、礼貌话术、人工转接、安全边界和隐私/风险提醒。",
            "绝大多数客户正式知识都不应该进入共享公共知识库；不确定时不要输出。",
            "禁止把某个客户自己的规则、行业经验、门店流程、商品政策、聊天客户资料改写成看似通用的公共规则。",
            "凡是包含具体客户、公司、门店、城市、群聊、联系人、手机号、微信号、订单、商品、车型、价格、库存、报价、售后承诺、物流承诺、账期、优惠、内部备注的内容，一律不要输出。",
            "二手车、车辆、检测报告、试驾、过户、门店看车等行业规则只能留在该 customer 自己的正式知识库，不能进入共享公共知识库。",
            "必须基于输入，不要编造新政策。",
            "输出 suggestions 数组；如果没有足够通用的内容，返回空数组。",
            "每条包含 title, category_id, guideline_text, keywords, applies_to, universal_reason, source_keys, universal_score。",
            "category_id 只能是 global_guidelines, reply_style, risk_control。",
        ],
        "negative_examples": [
            "某门店可看车、某城市物流、某商品库存价格、某车型检测承诺、某客户联系方式、某客户优惠政策。",
            "把“南京门店可看车”改写成“门店可看车需确认”也不允许，因为原始依据属于单一客户/单一行业。",
            "把某 customer 的售后、报价、账期、合同规则泛化给所有 customer 使用也不允许。",
        ],
        "entries": [
            {
                "source_key": item["source_key"],
                "tenant_id": item["tenant_id"],
                "category_id": item["category_id"],
                "item_id": item["item_id"],
                "title": item["title"],
                "keywords": item["keywords"],
                "body": compact_excerpt(item["body"], 700),
            }
            for item in entries[:80]
        ],
    }
    result = call_deepseek_json(prompt)
    if not result:
        return None
    raw_suggestions = result.get("suggestions") if isinstance(result.get("suggestions"), list) else []
    suggestions = []
    by_key = {item["source_key"]: item for item in entries}
    for raw in raw_suggestions:
        if not isinstance(raw, dict):
            continue
        source_keys = [str(key) for key in raw.get("source_keys", []) if str(key).strip()]
        source_items = [by_key[key] for key in source_keys if key in by_key]
        if not source_items:
            source_key = str(raw.get("source_key") or "").strip()
            if source_key in by_key:
                source_items = [by_key[source_key]]
        if not source_items:
            continue
        suggestion = normalize_universal_suggestion(raw, source_items, provider="formal_knowledge_universal_llm", llm_used=True)
        if suggestion and is_strictly_shareable_suggestion(suggestion, source_items):
            suggestions.append(suggestion)
    return suggestions


def heuristic_universal_shared_suggestions(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    suggestions = []
    for entry in entries:
        if not entry_has_strict_universal_topic(entry):
            continue
        suggestion = normalize_universal_suggestion(
            {
                "title": entry["title"],
                "category_id": shared_category_for_formal_entry(entry),
                "guideline_text": entry["body"],
                "keywords": entry["keywords"],
                "applies_to": "所有客户都可能遇到的通用客服场景",
                "universal_reason": "该条正式知识没有绑定具体商品、库存或价格，可由管理员审核是否沉淀为共享公共知识。",
            },
            [entry],
            provider="formal_knowledge_universal_heuristic",
            llm_used=False,
        )
        if suggestion and is_strictly_shareable_suggestion(suggestion, [entry]):
            suggestions.append(suggestion)
    return suggestions


def normalize_universal_suggestion(
    raw: dict[str, Any],
    source_items: list[dict[str, Any]],
    *,
    provider: str,
    llm_used: bool,
) -> dict[str, Any] | None:
    title = str(raw.get("title") or source_items[0].get("title") or "").strip()
    guideline = str(raw.get("guideline_text") or raw.get("content") or source_items[0].get("body") or "").strip()
    if not title or not guideline:
        return None
    source_keys = [str(item.get("source_key") or "") for item in source_items if str(item.get("source_key") or "").strip()]
    merged_key = stable_digest("|".join(source_keys) + title + guideline, 18)
    tenant_id = str(source_items[0].get("tenant_id") or DEFAULT_TENANT_ID)
    category_id = sanitize_shared_category(raw.get("category_id") or shared_category_for_formal_entry(source_items[0]))
    return {
        "id": f"shared_{merged_key}",
        "tenant_id": tenant_id,
        "category_id": category_id,
        "title": title,
        "guideline_text": compact_excerpt(guideline, 1200),
        "keywords": normalize_text_list(raw.get("keywords")) or merged_keywords(source_items),
        "applies_to": str(raw.get("applies_to") or "所有客户都可能遇到的通用客服场景"),
        "universal_reason": str(raw.get("universal_reason") or "来自客户正式知识库，未绑定具体商品，适合管理员审核为共享公共知识。"),
        "universal_score": raw.get("universal_score"),
        "source_key": merged_key,
        "source_items": [
            {
                "tenant_id": item.get("tenant_id"),
                "category_id": item.get("category_id"),
                "item_id": item.get("item_id"),
                "source_key": item.get("source_key"),
            }
            for item in source_items
        ],
        "provider": provider,
        "llm_used": llm_used,
    }


def looks_product_specific_suggestion(suggestion: dict[str, Any]) -> bool:
    text = f"{suggestion.get('title')}\n{suggestion.get('guideline_text')}\n{' '.join(suggestion.get('keywords') or [])}"
    return any(hint in text for hint in PRODUCT_SPECIFIC_TEXT_HINTS)


def is_strictly_shareable_suggestion(suggestion: dict[str, Any], source_items: list[dict[str, Any]]) -> bool:
    text = f"{suggestion.get('title')}\n{suggestion.get('guideline_text')}\n{suggestion.get('applies_to')}\n{suggestion.get('universal_reason')}\n{' '.join(suggestion.get('keywords') or [])}"
    if looks_product_specific_suggestion(suggestion):
        return False
    if looks_tenant_private_or_industry_specific(text):
        return False
    try:
        score = int(float(suggestion.get("universal_score")))
    except (TypeError, ValueError):
        score = 0
    if suggestion.get("llm_used") and score < 85:
        return False
    if not entry_has_strict_universal_topic({"title": suggestion.get("title"), "body": suggestion.get("guideline_text"), "keywords": suggestion.get("keywords") or []}):
        return False
    return all(is_universal_formal_entry(item) for item in source_items)


def has_private_structured_fields(data: dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False
    for key, value in data.items():
        key_text = str(key or "").strip().lower()
        if any(hint.lower() in key_text for hint in TENANT_PRIVATE_FIELD_HINTS):
            if value not in (None, "", [], {}):
                return True
        if isinstance(value, dict) and has_private_structured_fields(value):
            return True
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and has_private_structured_fields(item):
                    return True
    return False


def looks_tenant_private_or_industry_specific(text: str) -> bool:
    value = str(text or "")
    if any(hint in value for hint in TENANT_PRIVATE_TEXT_HINTS):
        return True
    if any(pattern.search(value) for pattern in PRIVATE_DATA_PATTERNS):
        return True
    return False


def entry_has_strict_universal_topic(entry: dict[str, Any]) -> bool:
    text = f"{entry.get('title')}\n{entry.get('body')}\n{' '.join(entry.get('keywords') or [])}"
    return any(hint in text for hint in STRICT_UNIVERSAL_TOPIC_HINTS)


def merged_keywords(source_items: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for item in source_items:
        values.extend(item.get("keywords") or [])
    deduped: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in deduped:
            deduped.append(text)
    return deduped[:12]


def shared_category_for_formal_entry(entry: dict[str, Any]) -> str:
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    text = f"{entry.get('title')}\n{entry.get('body')}\n{' '.join(entry.get('keywords') or [])}".lower()
    policy_type = str(data.get("policy_type") or "").lower()
    if "risk" in text or "handoff" in text or "转人工" in text or "人工" in text or policy_type in {"manual_required", "risk_control"}:
        return "risk_control"
    if str(entry.get("category_id") or "") == "chats":
        return "reply_style"
    return "global_guidelines"


def sanitize_shared_category(value: Any) -> str:
    category = str(value or "").strip()
    allowed = {"global_guidelines", "reply_style", "risk_control"}
    return category if category in allowed else "global_guidelines"


def build_shared_content_from_suggestion(suggestion: dict[str, Any]) -> dict[str, Any]:
    category_id = sanitize_shared_category(suggestion.get("category_id"))
    item_id = str(suggestion.get("id") or f"shared_{stable_digest(str(suggestion), 16)}")
    risk_control = category_id == "risk_control"
    data = {
        "title": str(suggestion.get("title") or item_id),
        "guideline_text": str(suggestion.get("guideline_text") or ""),
        "keywords": normalize_text_list(suggestion.get("keywords")),
        "applies_to": str(suggestion.get("applies_to") or ""),
    }
    if risk_control:
        data.update(
            {
                "allow_auto_reply": False,
                "requires_handoff": True,
                "handoff_reason": str(suggestion.get("handoff_reason") or "shared_risk_control"),
            }
        )
    return {
        "schema_version": 1,
        "id": item_id,
        "item_id": item_id,
        "category_id": category_id,
        "title": str(suggestion.get("title") or item_id),
        "status": "active",
        "keywords": normalize_text_list(suggestion.get("keywords")),
        "applies_to": str(suggestion.get("applies_to") or ""),
        "content": str(suggestion.get("guideline_text") or ""),
        "guideline_text": str(suggestion.get("guideline_text") or ""),
        "notes": f"候选理由：{suggestion.get('universal_reason') or ''}".strip(),
        "source": {
            "type": "formal_knowledge_universal_extraction",
            "provider": suggestion.get("provider"),
            "tenant_id": suggestion.get("tenant_id"),
            "source_items": suggestion.get("source_items") or [],
            "llm_used": bool(suggestion.get("llm_used")),
        },
        "data": data,
        "runtime": {
            "allow_auto_reply": not risk_control,
            "requires_handoff": risk_control,
            "risk_level": "high" if risk_control else "normal",
        },
    }


def filter_unscanned_formal_entries(
    entries: list[dict[str, Any]],
    scan_state: dict[str, Any],
    *,
    only_unscanned: bool,
    rescan: bool,
) -> list[dict[str, Any]]:
    if rescan or not only_unscanned:
        return entries
    filtered: list[dict[str, Any]] = []
    for entry in entries:
        source_key = str(entry.get("source_key") or "")
        record = scan_state.get(source_key) if source_key else None
        status = str(record.get("status") or "") if isinstance(record, dict) else ""
        if status in SHARED_SCAN_TERMINAL_STATUSES:
            continue
        filtered.append(entry)
    return filtered


def mark_shared_scan_state(
    scan_state: dict[str, Any],
    source_keys: list[str] | set[str],
    *,
    status: str,
    detail: dict[str, Any] | None = None,
    use_llm: bool = False,
) -> None:
    now = now_iso()
    for key in source_keys:
        source_key = str(key or "").strip()
        if not source_key:
            continue
        scan_state[source_key] = {
            "source_key": source_key,
            "status": status,
            "detail": detail or {},
            "llm_used": bool(use_llm),
            "checked_at": now,
            "updated_at": now,
        }


def formal_source_keys_for_suggestion(suggestion: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for item in suggestion.get("source_items") or []:
        if isinstance(item, dict):
            key = str(item.get("source_key") or "").strip()
            if key and key not in keys:
                keys.append(key)
    return keys


def shared_proposal_formal_source_keys(proposal: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    meta = proposal.get("source_meta") if isinstance(proposal.get("source_meta"), dict) else {}
    for item in meta.get("source_items") or []:
        if isinstance(item, dict):
            key = str(item.get("source_key") or "").strip()
            if key and key not in keys:
                keys.append(key)
    for operation in proposal.get("operations") or []:
        if not isinstance(operation, dict):
            continue
        content = operation.get("content") if isinstance(operation.get("content"), dict) else {}
        source = content.get("source") if isinstance(content.get("source"), dict) else {}
        for item in source.get("source_items") or []:
            if isinstance(item, dict):
                key = str(item.get("source_key") or "").strip()
                if key and key not in keys:
                    keys.append(key)
    return keys


def shared_proposal_source_keys(proposal: dict[str, Any]) -> set[str]:
    keys = set(shared_proposal_formal_source_keys(proposal))
    meta = proposal.get("source_meta") if isinstance(proposal.get("source_meta"), dict) else {}
    source_key = str(meta.get("source_key") or "").strip()
    if source_key:
        keys.add(source_key)
    return keys


def shared_library_source_keys(record: dict[str, Any]) -> set[str]:
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    keys = set()
    for item in source.get("source_items") or []:
        if isinstance(item, dict):
            key = str(item.get("source_key") or "").strip()
            if key:
                keys.add(key)
    return keys


def find_duplicate_shared_proposal(proposal: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    source_keys = shared_proposal_source_keys(proposal)
    title_fp = shared_text_fingerprint(proposal.get("title"))
    content_fp = shared_text_fingerprint(proposal_readable_content(proposal))
    for existing in state.get("shared_proposals", {}).values():
        if not isinstance(existing, dict):
            continue
        if str(existing.get("proposal_id") or "") == str(proposal.get("proposal_id") or ""):
            continue
        if source_keys and source_keys.intersection(shared_proposal_source_keys(existing)):
            return existing
        if title_fp and content_fp and title_fp == shared_text_fingerprint(existing.get("title")) and content_fp == shared_text_fingerprint(proposal_readable_content(existing)):
            return existing
    return None


def shared_proposal_matches_library(proposal: dict[str, Any], records: Any) -> dict[str, Any] | None:
    source_keys = shared_proposal_source_keys(proposal)
    title_fp = shared_text_fingerprint(proposal.get("title"))
    content_fp = shared_text_fingerprint(proposal_readable_content(proposal))
    for record in records:
        if not isinstance(record, dict):
            continue
        if source_keys and source_keys.intersection(shared_library_source_keys(record)):
            return record
        if title_fp and title_fp == shared_text_fingerprint(record.get("title")):
            existing_content_fp = shared_text_fingerprint(record.get("content") or readable_shared_content(record.get("data") if isinstance(record.get("data"), dict) else {}))
            if content_fp and content_fp == existing_content_fp:
                return record
    return None


def find_duplicate_shared_suggestion(suggestion: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    proposal = {
        "title": suggestion.get("title"),
        "source_meta": {
            "source_key": suggestion.get("source_key"),
            "source_items": suggestion.get("source_items") or [],
        },
        "operations": [
            {
                "content": build_shared_content_from_suggestion(suggestion),
            }
        ],
    }
    duplicate_proposal = find_duplicate_shared_proposal(proposal, state)
    if duplicate_proposal:
        return {
            "reason": "already_pending_or_reviewed",
            "proposal_id": duplicate_proposal.get("proposal_id"),
        }
    duplicate_library = shared_proposal_matches_library(proposal, state.get("shared_library", {}).values())
    if duplicate_library:
        return {
            "reason": "already_in_shared_library",
            "library_item_id": duplicate_library.get("item_id"),
        }
    return None


def proposal_readable_content(proposal: dict[str, Any]) -> str:
    parts: list[str] = []
    if proposal.get("summary"):
        parts.append(str(proposal.get("summary")))
    for operation in proposal.get("operations") or []:
        if not isinstance(operation, dict):
            continue
        content = operation.get("content") if isinstance(operation.get("content"), dict) else {}
        if content:
            parts.append(readable_shared_content(content) or readable_shared_content(content.get("data") if isinstance(content.get("data"), dict) else {}) or json.dumps(content, ensure_ascii=False, sort_keys=True))
    return "\n".join(part for part in parts if part)


def shared_text_fingerprint(value: Any) -> str:
    text = "".join(str(value or "").lower().split())
    return stable_digest(text, 18) if text else ""


def build_shared_proposal_review_assist(
    proposal: dict[str, Any],
    library_records: Any,
    *,
    use_llm: bool,
) -> dict[str, Any]:
    matches = shared_review_existing_matches(proposal, library_records)
    if use_llm:
        llm = llm_shared_proposal_review_assist(proposal, matches)
        if llm:
            return llm
    return heuristic_shared_proposal_review_assist(proposal, matches)


def llm_shared_proposal_review_assist(proposal: dict[str, Any], matches: list[dict[str, Any]]) -> dict[str, Any]:
    prompt = {
        "task": "帮助 admin 审核一条候选共享公共知识。请判断它是否适合给所有客户使用。",
        "decision_standard": [
            "共享公共知识必须跨行业、跨客户普遍适用，例如客服礼貌表达、人工转接、风险提示、通用售后沟通方式。",
            "不得包含某个客户、行业、商品、车型、库存、价格、门店、物流承诺、内部流程、特定优惠。",
            "不得把单个 customer 的规则泛化改写成公共知识；如果依据来自门店、行业、车型、客户资料或内部备注，应建议拒绝。",
            "必须和已有共享公共知识做重复/高度重合比对；若高度重合，应优先建议拒绝或合并，而不是直接采纳。",
            "只根据输入内容判断；不确定时建议先修改或拒绝。",
        ],
        "proposal": {
            "proposal_id": proposal.get("proposal_id"),
            "title": proposal.get("title"),
            "summary": proposal.get("summary"),
            "content": compact_excerpt(proposal_readable_content(proposal), 1200),
            "source": proposal.get("source"),
            "source_meta": proposal.get("source_meta") if isinstance(proposal.get("source_meta"), dict) else {},
        },
        "existing_shared_matches": matches[:6],
        "output_schema": {
            "recommendation": "accept|reject|revise",
            "universal_score": "0-100",
            "summary": "普通人能看懂的一句话解释",
            "reasons": ["为什么适合或不适合共享"],
            "risks": ["采纳前需要注意的风险"],
            "duplicate_level": "none|possible|high",
            "existing_matches": [{"item_id": "已有条目ID", "title": "标题", "reason": "重合原因"}],
            "admin_checklist": ["admin 审核时需要确认的点"],
        },
    }
    result = call_deepseek_json(prompt)
    if not result:
        return {}
    return normalize_shared_review_assist(result, provider="shared_review_llm", llm_used=True, fallback_matches=matches)


def heuristic_shared_proposal_review_assist(proposal: dict[str, Any], matches: list[dict[str, Any]]) -> dict[str, Any]:
    text = f"{proposal.get('title')}\n{proposal_readable_content(proposal)}"
    product_specific = any(hint in text for hint in PRODUCT_SPECIFIC_TEXT_HINTS)
    tenant_private = looks_tenant_private_or_industry_specific(text)
    lacks_universal_topic = not entry_has_strict_universal_topic({"title": proposal.get("title"), "body": proposal_readable_content(proposal), "keywords": []})
    duplicate_level = "high" if any(item.get("level") == "high" for item in matches) else ("possible" if matches else "none")
    if product_specific or tenant_private:
        recommendation = "reject"
        score = 25
        summary = "这条候选疑似绑定了具体客户、行业、商品、价格、库存或车辆信息，不适合作为所有客户共用的公共知识。"
    elif lacks_universal_topic:
        recommendation = "revise"
        score = 50
        summary = "这条候选没有明显的跨行业公共客服原则，建议先不要进入共享公共知识库。"
    elif duplicate_level == "high":
        recommendation = "reject"
        score = 45
        summary = "这条候选和已有共享公共知识高度重合，建议不要重复收录。"
    elif duplicate_level == "possible":
        recommendation = "revise"
        score = 65
        summary = "这条候选看起来有通用价值，但需要先确认是否和已有共享知识重复。"
    else:
        recommendation = "accept"
        score = 78
        summary = "这条候选没有明显商品或客户绑定信息，可由 admin 继续审核是否收录。"
    reasons = []
    if product_specific or tenant_private:
        reasons.append("文本中出现了可能指向特定客户、行业、门店、商品、价格、库存、车型或车况的表达。")
    if lacks_universal_topic:
        reasons.append("系统没有识别到足够明确的跨行业公共客服原则。")
    if matches:
        reasons.append("系统已找到可能重复的共享知识条目，需要先比对。")
    if not reasons:
        reasons.append("候选内容来自正式知识库，当前没有发现明显商品专属或客户专属信息。")
    return normalize_shared_review_assist(
        {
            "recommendation": recommendation,
            "universal_score": score,
            "summary": summary,
            "reasons": reasons,
            "risks": ["采纳前请确认这条规则不会把某个客户的承诺误推给所有客户。"],
            "duplicate_level": duplicate_level,
            "existing_matches": matches,
            "admin_checklist": ["确认不含具体客户、商品、价格、库存、门店和行业限定。", "确认和已有共享知识不重复。"],
        },
        provider="shared_review_heuristic",
        llm_used=False,
        fallback_matches=matches,
    )


def normalize_shared_review_assist(
    raw: Any,
    *,
    provider: str,
    llm_used: bool | None = None,
    fallback_matches: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    recommendation = str(data.get("recommendation") or "").strip().lower()
    if recommendation not in {"accept", "reject", "revise"}:
        recommendation = "revise"
    try:
        score = int(float(data.get("universal_score")))
    except (TypeError, ValueError):
        score = 0
    duplicate_level = str(data.get("duplicate_level") or "").strip().lower()
    if duplicate_level not in {"none", "possible", "high"}:
        duplicate_level = "possible" if fallback_matches else "none"
    matches = data.get("existing_matches") if isinstance(data.get("existing_matches"), list) else fallback_matches or []
    clean_matches = []
    for item in matches[:8]:
        if not isinstance(item, dict):
            continue
        clean_matches.append(
            {
                "item_id": str(item.get("item_id") or ""),
                "title": str(item.get("title") or ""),
                "reason": str(item.get("reason") or item.get("match_reason") or ""),
                "level": str(item.get("level") or item.get("duplicate_level") or duplicate_level or "possible"),
            }
        )
    used_llm = bool(llm_used if llm_used is not None else data.get("llm_used"))
    label_prefix = "AI建议" if used_llm else "系统预判"
    label_action = {"accept": "采纳", "reject": "不采纳", "revise": "先修改"}[recommendation]
    return {
        "provider": provider,
        "llm_used": used_llm,
        "recommendation": recommendation,
        "recommendation_label": f"{label_prefix}：{label_action}",
        "universal_score": max(0, min(100, score)),
        "duplicate_level": duplicate_level,
        "summary": str(data.get("summary") or "请结合候选内容和已有共享知识进行人工判断。"),
        "reasons": normalize_text_list(data.get("reasons"))[:8],
        "risks": normalize_text_list(data.get("risks") or data.get("risk_notes"))[:8],
        "existing_matches": clean_matches,
        "admin_checklist": normalize_text_list(data.get("admin_checklist") or data.get("what_to_check"))[:8],
        "checked_at": now_iso(),
    }


def shared_review_existing_matches(proposal: dict[str, Any], library_records: Any) -> list[dict[str, Any]]:
    source_keys = shared_proposal_source_keys(proposal)
    title_fp = shared_text_fingerprint(proposal.get("title"))
    content_fp = shared_text_fingerprint(proposal_readable_content(proposal))
    matches: list[dict[str, Any]] = []
    for record in library_records:
        if not isinstance(record, dict):
            continue
        level = ""
        reason = ""
        if source_keys and source_keys.intersection(shared_library_source_keys(record)):
            level = "high"
            reason = "来自同一个正式知识来源"
        elif title_fp and title_fp == shared_text_fingerprint(record.get("title")):
            level = "possible"
            reason = "标题高度接近"
        existing_content = record.get("content") or readable_shared_content(record.get("data") if isinstance(record.get("data"), dict) else {})
        if content_fp and content_fp == shared_text_fingerprint(existing_content):
            level = "high"
            reason = "正文高度一致"
        if level:
            matches.append(
                {
                    "item_id": str(record.get("item_id") or ""),
                    "title": str(record.get("title") or record.get("item_id") or ""),
                    "reason": reason,
                    "level": level,
                    "content": compact_excerpt(str(existing_content or ""), 240),
                }
            )
    return matches[:8]


def call_deepseek_json_with_prompt(
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.1,
    max_tokens: int = 1800,
) -> dict[str, Any]:
    api_key = read_secret("DEEPSEEK_API_KEY")
    if not api_key:
        return {}
    base_url = resolve_deepseek_base_url(read_secret_fn=read_secret)
    model = resolve_deepseek_tier_model(tier="flash", read_secret_fn=read_secret)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": str(system_prompt or "")},
            {"role": "user", "content": str(user_prompt or "")},
        ],
        "temperature": float(temperature),
        "max_tokens": resolve_deepseek_max_tokens(max_tokens, read_secret_fn=read_secret),
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
        with llm_urlopen(request, timeout=resolve_deepseek_timeout(45, read_secret_fn=read_secret)) as response:
            raw = response.read().decode("utf-8")
        data = json.loads(raw or "{}")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return {}
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return parse_json_object_text(str(content or "")) or {}


def parse_json_object_text(text: str) -> dict[str, Any] | None:
    body = str(text or "").strip()
    if not body:
        return None
    try:
        payload = json.loads(body)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", body, re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def read_json_file(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def build_shared_library_record(
    payload: dict[str, Any],
    *,
    actor_id: str,
    item_id: str | None = None,
    source: str | None = None,
    tenant_id: str | None = None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    readable_data = readable_shared_data(data)
    resolved_item_id = str(item_id or payload.get("item_id") or payload.get("id") or make_id("shared_item")).strip()
    if not resolved_item_id:
        resolved_item_id = make_id("shared_item")
    category_id = str(payload.get("category_id") or data.get("category_id") or readable_data.get("category_id") or "global_guidelines")
    title = str(payload.get("title") or readable_data.get("title") or readable_data.get("name") or data.get("title") or data.get("name") or resolved_item_id)
    content = str(
        payload.get("content")
        or readable_shared_content(readable_data)
        or readable_shared_content(data)
        or json.dumps(data, ensure_ascii=False, sort_keys=True)
    )
    keywords = payload.get("keywords") if "keywords" in payload else readable_data.get("keywords")
    applies_to = payload.get("applies_to") if "applies_to" in payload else readable_data.get("applies_to")
    notes = payload.get("notes") if "notes" in payload else readable_data.get("notes")
    industry_id = normalize_shared_item_industry_id(
        payload.get("industry_id")
        or data.get("industry_id")
        or readable_data.get("industry_id")
        or (existing.get("industry_id") if isinstance(existing, dict) else "")
        or GLOBAL_INDUSTRY_ID
    )
    now = now_iso()
    record = {
        "item_id": resolved_item_id,
        "industry_id": industry_id,
        "category_id": category_id,
        "title": title,
        "content": content,
        "keywords": normalize_text_list(keywords),
        "applies_to": str(applies_to or ""),
        "notes": str(notes or ""),
        "status": str(payload.get("status") or data.get("status") or "active"),
        "source": str(source or payload.get("source") or data.get("source") or ""),
        "tenant_id": active_tenant_id(tenant_id or payload.get("tenant_id") or data.get("tenant_id") or DEFAULT_TENANT_ID),
        "data": data if isinstance(data, dict) else {},
        "created_by": existing.get("created_by") if isinstance(existing, dict) else actor_id,
        "created_at": existing.get("created_at") if isinstance(existing, dict) else now,
        "updated_by": actor_id,
        "updated_at": now,
    }
    return record


def public_shared_library_record(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    readable_data = readable_shared_data(data)
    if looks_like_json_object(str(result.get("content") or "")):
        result["content"] = readable_shared_content(readable_data) or readable_shared_content(data) or str(result.get("content") or "")
    if not result.get("title"):
        result["title"] = readable_data.get("title") or readable_data.get("name") or result.get("item_id")
    result["keywords"] = normalize_text_list(result.get("keywords") or readable_data.get("keywords"))
    result["applies_to"] = str(result.get("applies_to") or readable_data.get("applies_to") or "")
    result["notes"] = str(result.get("notes") or readable_data.get("notes") or "")
    result["industry_id"] = normalize_shared_item_industry_id(
        result.get("industry_id")
        or readable_data.get("industry_id")
        or data.get("industry_id")
        or GLOBAL_INDUSTRY_ID
    )
    return result


def build_official_shared_knowledge_snapshot(
    state: dict[str, Any],
    *,
    tenant_id: str = "",
    since_version: str = "",
) -> dict[str, Any]:
    tenant = active_tenant_id(tenant_id or DEFAULT_TENANT_ID)
    seed_shared_library_if_empty(state)
    ensure_tenant_industry_bindings(state)
    tenant_record = state.get("tenants", {}).get(tenant) if isinstance(state.get("tenants"), dict) else {}
    tenant_metadata = read_tenant_metadata(tenant)
    tenant_industry_id = resolve_tenant_industry_id(
        tenant,
        tenant_record=tenant_record if isinstance(tenant_record, dict) else {},
        tenant_metadata=tenant_metadata,
    )
    allowed_industries = {GLOBAL_INDUSTRY_ID, tenant_industry_id}
    generated_at = now_iso()
    items = [
        public_shared_library_record(item)
        for item in state.get("shared_library", {}).values()
        if isinstance(item, dict)
        and str(item.get("status") or "active") == "active"
        and normalize_shared_item_industry_id(item.get("industry_id") or GLOBAL_INDUSTRY_ID) in allowed_industries
    ]
    items = sorted(items, key=lambda item: (str(item.get("category_id") or ""), str(item.get("title") or item.get("item_id") or "")))
    policy_bundle = build_policy_bundle(tenant_industry_id)
    version = official_shared_snapshot_version(items, tenant_industry_id=tenant_industry_id, policy_bundle=policy_bundle)
    base = {
        "schema_version": 1,
        "source": "cloud_official_shared_library",
        "version": version,
        "tenant_id": tenant,
        "tenant_industry_id": tenant_industry_id,
        "industry_catalog": industry_catalog(),
        "generated_at": generated_at,
        **shared_snapshot_cache_policy(version=version, tenant_id=tenant, issued_at=generated_at),
        "deleted_item_ids": [],
        "policy_bundle": policy_bundle,
    }
    if since_version and str(since_version) == version:
        return {**base, "not_modified": True, "categories": [], "items": []}
    return {
        **base,
        "not_modified": False,
        "categories": official_shared_snapshot_categories(items),
        "items": items,
    }


def official_shared_snapshot_version(
    items: list[dict[str, Any]],
    *,
    tenant_industry_id: str = "",
    policy_bundle: dict[str, Any] | None = None,
) -> str:
    fingerprints = [
        {
            "item_id": item.get("item_id"),
            "industry_id": item.get("industry_id"),
            "category_id": item.get("category_id"),
            "title": item.get("title"),
            "content": item.get("content"),
            "keywords": item.get("keywords"),
            "applies_to": item.get("applies_to"),
            "status": item.get("status"),
            "updated_at": item.get("updated_at"),
        }
        for item in items
    ]
    version_payload = {
        "tenant_industry_id": tenant_industry_id,
        "items": fingerprints,
        "policy_bundle_digest": stable_digest(json.dumps(policy_bundle or {}, ensure_ascii=False, sort_keys=True), 16),
    }
    return "shared_" + stable_digest(json.dumps(version_payload, ensure_ascii=False, sort_keys=True), 20)


def shared_snapshot_cache_policy(*, version: str, tenant_id: str, issued_at: str) -> dict[str, Any]:
    ttl_seconds = clamp_int(os.getenv("WECHAT_SHARED_SNAPSHOT_TTL_SECONDS"), default=1800, minimum=60, maximum=86400)
    refresh_after_seconds = clamp_int(os.getenv("WECHAT_SHARED_SNAPSHOT_REFRESH_AFTER_SECONDS"), default=min(300, max(60, ttl_seconds // 3)), minimum=30, maximum=ttl_seconds)
    issued = parse_iso_datetime(issued_at) or datetime.now(timezone.utc)
    refresh_after_at = (issued + timedelta(seconds=refresh_after_seconds)).isoformat(timespec="seconds")
    expires_at = (issued + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
    lease_id = "shared_lease_" + stable_digest(f"{tenant_id}:{version}:{issued_at}", 20)
    cache_policy = {
        "mode": "cloud_authoritative_lease",
        "ttl_seconds": ttl_seconds,
        "refresh_after_seconds": refresh_after_seconds,
        "issued_at": issued.isoformat(timespec="seconds"),
        "refresh_after_at": refresh_after_at,
        "expires_at": expires_at,
        "lease_id": lease_id,
        "requires_cloud_refresh": True,
    }
    return {
        "ttl_seconds": ttl_seconds,
        "refresh_after_seconds": refresh_after_seconds,
        "issued_at": cache_policy["issued_at"],
        "refresh_after_at": refresh_after_at,
        "expires_at": expires_at,
        "lease_id": lease_id,
        "cache_policy": cache_policy,
    }


def clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def parse_iso_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def official_shared_snapshot_categories(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, int]] = {}
    for item in items:
        category_id = str(item.get("category_id") or "global_guidelines")
        bucket = grouped.setdefault(category_id, {"item_count": 0, "global_count": 0, "industry_count": 0})
        bucket["item_count"] += 1
        if normalize_shared_item_industry_id(item.get("industry_id") or GLOBAL_INDUSTRY_ID) == GLOBAL_INDUSTRY_ID:
            bucket["global_count"] += 1
        else:
            bucket["industry_count"] += 1
    return [
        {
            "category_id": category_id,
            "name": category_id,
            "kind": "mixed" if payload["global_count"] and payload["industry_count"] else ("industry" if payload["industry_count"] else "global"),
            "enabled": True,
            "item_count": payload["item_count"],
            "global_item_count": payload["global_count"],
            "industry_item_count": payload["industry_count"],
        }
        for category_id, payload in sorted(grouped.items())
    ]


def readable_shared_data(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    nested = data.get("data") if isinstance(data.get("data"), dict) else None
    if nested:
        return nested
    return data


def readable_shared_content(data: dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return ""
    parts = []
    for key in ("guideline_text", "content", "answer", "body", "service_reply", "reply_text"):
        value = data.get(key)
        if value:
            parts.append(str(value))
    if data.get("keywords") and len(parts) <= 1:
        parts.append(f"关键词：{'、'.join(normalize_text_list(data.get('keywords')))}")
    if data.get("applies_to"):
        parts.append(f"适用场景：{data.get('applies_to')}")
    return "\n".join(parts)


def normalize_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).replace("，", ",").replace("、", ",").split(",") if item.strip()]


def looks_like_json_object(value: str) -> bool:
    text = value.strip()
    return text.startswith("{") and text.endswith("}")


def seed_shared_library_if_empty(state: dict[str, Any]) -> None:
    library = state.setdefault("shared_library", {})
    if not isinstance(library, dict):
        state["shared_library"] = {}
        library = state["shared_library"]
    for payload in seed_shared_library_items():
        if not isinstance(payload, dict):
            continue
        item_id = str(payload.get("item_id") or payload.get("id") or "").strip()
        if not item_id or item_id in library:
            continue
        record = build_shared_library_record(
            payload,
            actor_id="system_seed",
            item_id=item_id,
            source=str(payload.get("source") or "system_seed_industry_library"),
            tenant_id=DEFAULT_TENANT_ID,
            existing=None,
        )
        library[item_id] = record


def upsert_shared_library_from_operations(
    state: dict[str, Any],
    operations: list[Any],
    *,
    actor_id: str,
    source: str,
    tenant_id: str,
) -> list[dict[str, Any]]:
    seed_shared_library_if_empty(state)
    updated: list[dict[str, Any]] = []
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        op = str(operation.get("op") or operation.get("action") or "").strip().lower()
        if op not in {"upsert", "upsert_json", "create", "update"}:
            continue
        content = operation.get("content") if isinstance(operation.get("content"), dict) else {}
        if not content:
            continue
        op_path = str(operation.get("path") or "")
        item_id = str(content.get("item_id") or content.get("id") or (Path(op_path).stem if op_path else "")).strip()
        existing = state["shared_library"].get(item_id) if item_id else None
        record = build_shared_library_record(
            content,
            actor_id=actor_id,
            item_id=item_id or None,
            source=source,
            tenant_id=tenant_id,
            existing=existing if isinstance(existing, dict) else None,
        )
        state["shared_library"][record["item_id"]] = record
        updated.append(record)
    return updated


def validate_shared_patch_payload(patch: dict[str, Any], *, require_signature: bool = False) -> dict[str, Any]:
    root = runtime_app_root() / "vps_admin" / "shared_patch_validation"
    service = SharedPatchService(root=root, signing_secret="" if not require_signature else os.getenv("WECHAT_SHARED_PATCH_SECRET", ""))
    try:
        return service.preview(patch)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def sign_shared_patch_if_configured(patch: dict[str, Any]) -> dict[str, Any]:
    secret = os.getenv("WECHAT_SHARED_PATCH_SECRET", "").strip()
    if not secret:
        return dict(patch)
    signed = {key: value for key, value in patch.items() if key != "signature"}
    payload = json.dumps(signed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signed["signature"] = hmac.new(secret.encode("utf-8"), payload, sha256).hexdigest()
    return signed


def delete_managed_file(path: Path) -> bool:
    if not str(path) or not path.exists() or not path.is_file():
        return False
    try:
        root = runtime_app_root().resolve()
        resolved = path.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return False
    path.unlink()
    return True


def command_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "command_id": record.get("command_id"),
        "type": record.get("type"),
        "tenant_id": record.get("tenant_id"),
        "node_id": record.get("node_id"),
        "payload": record.get("payload") if isinstance(record.get("payload"), dict) else {},
        "created_at": record.get("created_at"),
    }


def latest_by_created_at(items: Any) -> dict[str, Any] | None:
    values = [item for item in items if isinstance(item, dict)]
    if not values:
        return None
    return sorted(values, key=lambda item: str(item.get("created_at") or ""), reverse=True)[0]


def to_bool(value: Any, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)
