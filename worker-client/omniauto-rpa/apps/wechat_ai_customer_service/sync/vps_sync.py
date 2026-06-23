"""VPS-LOCAL coordination service."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from apps.wechat_ai_customer_service.auth.vps_client import VpsAuthClient, VpsClientError, discover_vps_base_url
from apps.wechat_ai_customer_service.cloud_gate import cloud_gate_status, cloud_required_enabled
from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, runtime_app_root, shared_runtime_cache_root, shared_runtime_snapshot_path

from .backup_service import BackupService
from .shared_patch_service import SHARED_CATEGORY_DEFINITIONS, SharedPatchService
from .shared_candidate_scanner import (
    SHARED_SCAN_TERMINAL_STATUSES,
    build_shared_content_from_suggestion,
    build_universal_shared_suggestions,
    collect_universal_formal_entries,
    formal_source_keys_for_suggestion,
)
from apps.wechat_ai_customer_service.workflows.generate_review_candidates import stable_digest


WINDOWS_FILE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.25)


class VpsLocalSyncService:
    def __init__(
        self,
        *,
        vps_base_url: str | None = None,
        backup_service: BackupService | None = None,
        shared_patch_service: SharedPatchService | None = None,
    ) -> None:
        base_url = (vps_base_url if vps_base_url is not None else discover_vps_base_url()).strip().rstrip("/")
        self.vps = VpsAuthClient(base_url=base_url, timeout_seconds=float(os.getenv("WECHAT_VPS_TIMEOUT_SECONDS") or "8"))
        self.backups = backup_service or BackupService()
        self.shared_patches = shared_patch_service or SharedPatchService()

    def status(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        tenant = active_tenant_id(tenant_id)
        node_cache = self.read_node_cache()
        return {
            "ok": True,
            "tenant_id": tenant,
            "vps_configured": self.vps.configured,
            "vps_base_url": self.vps.base_url,
            "mode": "online_configured" if self.vps.configured else "offline_unconfigured",
            "runtime_root": str(runtime_app_root()),
            "node": node_cache,
            "shared_cloud_cache": self.shared_cloud_cache_status(),
            "cloud_gate": cloud_gate_status(),
            "supported_commands": ["backup_all", "backup_tenant", "pull_shared_patch", "check_update", "restore_backup", "push_update"],
            "supported_sync": ["formal_shared_candidates", "cloud_shared_knowledge", "recorder_module_bindings", "commands", "updates"],
        }

    def register_node(self, *, token: str = "", tenant_id: str | None = None, display_name: str = "") -> dict[str, Any]:
        tenant = active_tenant_id(tenant_id)
        if not self.vps.configured:
            return {"ok": True, "mode": "offline_unconfigured", "tenant_id": tenant, "node": None}
        node_id = str(os.getenv("WECHAT_LOCAL_NODE_ID") or stable_local_node_id()).strip()
        payload = {
            "node_id": node_id,
            "display_name": display_name or f"{socket.gethostname()} Local 客户端",
            "tenant_ids": [tenant],
            "version": os.getenv("WECHAT_LOCAL_APP_VERSION") or "local-dev",
            "capabilities": ["backup_tenant", "backup_all", "pull_shared_patch", "check_update", "restore_backup", "push_update"],
            "metadata": {
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "runtime_root": str(runtime_app_root()),
            },
        }
        enrollment_token = str(os.getenv("WECHAT_VPS_NODE_ENROLLMENT_TOKEN") or "").strip()
        headers = {"X-Enrollment-Token": enrollment_token} if enrollment_token else None
        try:
            response = self.vps.post_json("/v1/local/nodes/register", payload, token=token, headers=headers)
        except VpsClientError as exc:
            return {"ok": False, "tenant_id": tenant, "node_id": node_id, "error": str(exc)}
        node = response.get("node") if isinstance(response.get("node"), dict) else response
        self.write_node_cache(node if isinstance(node, dict) else {"node_id": node_id})
        return {"ok": True, "tenant_id": tenant, "node": node}

    def read_node_cache(self) -> dict[str, Any]:
        path = local_node_cache_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def write_node_cache(self, node: dict[str, Any]) -> None:
        path = local_node_cache_path()
        write_json_payload(path, node)

    def poll_commands(
        self,
        *,
        token: str = "",
        tenant_id: str | None = None,
        node_id: str | None = None,
        node_token: str | None = None,
    ) -> dict[str, Any]:
        tenant = active_tenant_id(tenant_id)
        if not self.vps.configured:
            return {"ok": True, "tenant_id": tenant, "mode": "offline_unconfigured", "commands": [], "results": []}
        cached_node = self.read_node_cache()
        node = str(node_id or cached_node.get("node_id") or os.getenv("WECHAT_LOCAL_NODE_ID") or "").strip()
        node_secret = str(node_token or cached_node.get("node_token") or os.getenv("WECHAT_LOCAL_NODE_TOKEN") or "").strip()
        query = urlencode({"tenant_id": tenant, "node_id": node})
        headers = {"X-Node-Token": node_secret} if node_secret else None
        try:
            payload = self.vps.get_json(f"/v1/local/commands?{query}", token=token, headers=headers)
        except VpsClientError as exc:
            return {"ok": False, "tenant_id": tenant, "error": str(exc), "commands": [], "results": []}
        commands = payload.get("commands", []) if isinstance(payload.get("commands"), list) else []
        results = []
        for command in commands:
            if not isinstance(command, dict):
                continue
            result = self.handle_command(command, tenant_id=tenant)
            report = self.report_command_result(result, token=token, node_token=node_secret)
            if report.get("ok") is False:
                result["report_error"] = report.get("error")
            results.append(result)
        return {"ok": True, "tenant_id": tenant, "commands": commands, "results": results}

    def handle_command(self, command: dict[str, Any], *, tenant_id: str | None = None) -> dict[str, Any]:
        command_type = str(command.get("type") or "")
        tenant = active_tenant_id(command.get("tenant_id") or tenant_id)
        if command_type == "backup_all":
            return {"command_id": command.get("command_id"), "accepted": True, "result": self.backups.build_backup(scope="all", tenant_id=tenant)}
        if command_type == "backup_tenant":
            return {"command_id": command.get("command_id"), "accepted": True, "result": self.backups.build_backup(scope="tenant", tenant_id=tenant)}
        if command_type == "pull_shared_patch":
            payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
            patch_payload = payload.get("patch") if isinstance(payload.get("patch"), dict) else {}
            try:
                refreshed = self.fetch_shared_knowledge_snapshot(
                    tenant_id=tenant,
                    force=payload.get("force", True) is not False,
                    since_version=str(payload.get("version") or ""),
                )
            except Exception as exc:
                return {"command_id": command.get("command_id"), "accepted": False, "error": str(exc)}
            return {
                "command_id": command.get("command_id"),
                "accepted": refreshed.get("ok") is not False,
                "result": {
                    "ok": refreshed.get("ok") is not False,
                    "mode": "cloud_shared_snapshot_refresh",
                    "patch_id": str(payload.get("patch_id") or patch_payload.get("patch_id") or ""),
                    "snapshot": refreshed,
                },
            }
        if command_type == "check_update":
            return {"command_id": command.get("command_id"), "accepted": True, "result": {"ok": True, "deferred": command_type}}
        if command_type in {"restore_backup", "push_update"}:
            return {
                "command_id": command.get("command_id"),
                "accepted": True,
                "result": {"ok": True, "deferred": command_type, "payload": command.get("payload") if isinstance(command.get("payload"), dict) else {}},
            }
        return {"command_id": command.get("command_id"), "accepted": False, "error": f"unsupported command type: {command_type}"}

    def report_command_result(self, result: dict[str, Any], *, token: str = "", node_token: str = "") -> dict[str, Any]:
        command_id = str(result.get("command_id") or "")
        if not command_id or not self.vps.configured:
            return {"ok": True, "skipped": True}
        headers = {"X-Node-Token": node_token} if node_token else None
        try:
            payload = self.vps.post_json(f"/v1/local/commands/{command_id}/result", result, token=token, headers=headers)
        except VpsClientError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "response": payload}

    def check_update(self, *, token: str = "") -> dict[str, Any]:
        if not self.vps.configured:
            return {"ok": True, "mode": "offline_unconfigured", "update": None}
        try:
            payload = self.vps.get_json("/v1/updates/latest", token=token)
        except VpsClientError as exc:
            return {"ok": False, "error": str(exc), "update": None}
        return {"ok": True, "update": payload.get("update") if isinstance(payload.get("update"), dict) else payload}

    def fetch_recorder_module_bindings(
        self,
        *,
        token: str = "",
        tenant_id: str | None = None,
        user_id: str = "",
    ) -> dict[str, Any]:
        tenant = active_tenant_id(tenant_id)
        if not self.vps.configured:
            return {
                "ok": True,
                "mode": "offline_unconfigured",
                "tenant_id": tenant,
                "user_id": str(user_id or ""),
                "updated": False,
            }
        query: dict[str, str] = {"tenant_id": tenant}
        normalized_user_id = str(user_id or "").strip()
        if normalized_user_id:
            query["user_id"] = normalized_user_id
        node_cache = self.read_node_cache()
        node_id = str(node_cache.get("node_id") or os.getenv("WECHAT_LOCAL_NODE_ID") or "").strip()
        node_secret = str(node_cache.get("node_token") or os.getenv("WECHAT_LOCAL_NODE_TOKEN") or "").strip()
        if node_id:
            query["node_id"] = node_id
        headers = {"X-Node-Token": node_secret} if node_secret else None
        try:
            payload = self.vps.get_json(f"/v1/local/recorder-module-bindings?{urlencode(query)}", token=token, headers=headers)
        except VpsClientError as exc:
            return {"ok": False, "tenant_id": tenant, "user_id": normalized_user_id, "error": str(exc)}
        snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else payload
        if not isinstance(snapshot, dict):
            return {"ok": False, "tenant_id": tenant, "user_id": normalized_user_id, "error": "recorder module snapshot must be an object"}
        try:
            from apps.wechat_ai_customer_service.admin_backend.services.recorder_module_registry import RecorderModuleRegistryService

            sync_result = RecorderModuleRegistryService().sync_vps_snapshot(
                snapshot,
                tenant_id=tenant,
                user_id=normalized_user_id,
            )
        except Exception as exc:  # pragma: no cover - defensive to avoid sync loop crash
            return {"ok": False, "tenant_id": tenant, "user_id": normalized_user_id, "error": f"apply_recorder_snapshot_failed: {exc}"}
        return {
            "ok": True,
            "mode": "online_configured",
            "tenant_id": tenant,
            "user_id": normalized_user_id,
            "snapshot_version": str(snapshot.get("snapshot_version") or ""),
            "resolved_user_ids": snapshot.get("resolved_user_ids", []),
            "pulled_module_count": len(snapshot.get("modules", []) if isinstance(snapshot.get("modules"), list) else []),
            "pulled_binding_count": len(snapshot.get("bindings", []) if isinstance(snapshot.get("bindings"), list) else []),
            "apply": sync_result,
            "updated": True,
        }

    def fetch_shared_knowledge_snapshot(
        self,
        *,
        token: str = "",
        tenant_id: str | None = None,
        since_version: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        tenant = active_tenant_id(tenant_id)
        cached = self.read_shared_cloud_snapshot_cache()
        cached_version = str(cached.get("version") or "")
        if not self.vps.configured:
            cache_valid = shared_cloud_cache_valid(cached)
            return {
                "ok": True,
                "mode": "offline_unconfigured",
                "tenant_id": tenant,
                "cached": bool(cached),
                "cache_valid": cache_valid,
                "updated": False,
                "snapshot_version": cached_version,
                "item_count": len(cached.get("items", [])) if isinstance(cached.get("items"), list) else 0,
                "category_count": len(cached.get("categories", [])) if isinstance(cached.get("categories"), list) else 0,
                **shared_cloud_cache_policy_summary(cached),
                "cache_root": str(shared_runtime_cache_root()),
                "snapshot_path": str(shared_runtime_snapshot_path()),
            }
        query: dict[str, str] = {"tenant_id": tenant}
        requested_since = str(since_version or cached_version or "").strip()
        if requested_since and not force:
            query["since_version"] = requested_since
        node_cache = self.read_node_cache()
        node_id = str(node_cache.get("node_id") or os.getenv("WECHAT_LOCAL_NODE_ID") or "").strip()
        node_secret = str(node_cache.get("node_token") or os.getenv("WECHAT_LOCAL_NODE_TOKEN") or "").strip()
        if node_id:
            query["node_id"] = node_id
        headers = {"X-Node-Token": node_secret} if node_secret else None
        try:
            payload = self.vps.get_json(f"/v1/shared/knowledge?{urlencode(query)}", token=token, headers=headers)
        except VpsClientError as exc:
            if cloud_required_enabled():
                expire_shared_cloud_snapshot_cache(cached, reason=str(exc))
            return {
                "ok": False,
                "tenant_id": tenant,
                "error": str(exc),
                "cached": bool(cached),
                "cache_valid": shared_cloud_cache_valid(cached),
                "snapshot_version": cached_version,
                **shared_cloud_cache_policy_summary(cached),
                "cache_root": str(shared_runtime_cache_root()),
                "snapshot_path": str(shared_runtime_snapshot_path()),
            }
        snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else payload
        if payload.get("not_modified") or (isinstance(snapshot, dict) and snapshot.get("not_modified")):
            renewed = renew_shared_cloud_snapshot_cache(cached, snapshot if isinstance(snapshot, dict) else payload)
            return {
                "ok": True,
                "mode": "online_configured",
                "tenant_id": tenant,
                "cached": bool(renewed),
                "cache_valid": shared_cloud_cache_valid(renewed),
                "updated": False,
                "not_modified": True,
                "snapshot_version": str(renewed.get("version") or cached_version or payload.get("version") or ""),
                "item_count": len(renewed.get("items", [])) if isinstance(renewed.get("items"), list) else 0,
                "category_count": len(renewed.get("categories", [])) if isinstance(renewed.get("categories"), list) else 0,
                **shared_cloud_cache_policy_summary(renewed),
                "cache_root": str(shared_runtime_cache_root()),
                "snapshot_path": str(shared_runtime_snapshot_path()),
            }
        if not isinstance(snapshot, dict):
            if cloud_required_enabled():
                expire_shared_cloud_snapshot_cache(cached, reason="snapshot_not_object")
            return {"ok": False, "tenant_id": tenant, "error": "shared knowledge snapshot response must be an object"}
        cache_result = write_shared_cloud_snapshot_cache(snapshot)
        return {
            "ok": True,
            "mode": "online_configured",
            "tenant_id": tenant,
            "cached": True,
            "cache_valid": True,
            "updated": True,
            "not_modified": False,
            **cache_result,
        }

    def upload_formal_knowledge_candidates(
        self,
        *,
        token: str = "",
        tenant_id: str | None = None,
        use_llm: bool = True,
        limit: int = 30,
        only_unscanned: bool = True,
    ) -> dict[str, Any]:
        tenant = active_tenant_id(tenant_id)
        if not self.vps.configured:
            return {
                "ok": True,
                "mode": "offline_unconfigured",
                "tenant_id": tenant,
                "uploaded": [],
                "skipped": [],
                "checked_count": 0,
            }
        try:
            scan_limit = max(1, min(int(limit), 120))
        except (TypeError, ValueError):
            scan_limit = 30
        cache = self.read_shared_formal_scan_cache()
        entries = collect_universal_formal_entries({}, limit=scan_limit, tenant_id=tenant)
        unchecked = []
        for entry in entries:
            source_key = str(entry.get("source_key") or "")
            cached = cache.get(source_key) if source_key else None
            status = str(cached.get("status") or "") if isinstance(cached, dict) else ""
            if only_unscanned and status in SHARED_SCAN_TERMINAL_STATUSES:
                continue
            unchecked.append(entry)
        suggestions = build_universal_shared_suggestions(unchecked, use_llm=use_llm)
        uploaded: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        suggested_keys: set[str] = set()
        for suggestion in suggestions:
            formal_keys = formal_source_keys_for_suggestion(suggestion)
            suggested_keys.update(formal_keys)
            source_key = str(suggestion.get("source_key") or stable_digest(str(suggestion), 18))
            content = build_shared_content_from_suggestion(suggestion)
            proposal_id = "proposal_shared_local_" + stable_digest(f"{tenant}:{source_key}:{suggestion.get('title')}", 18)
            payload = {
                "proposal_id": proposal_id,
                "tenant_id": tenant,
                "title": str(suggestion.get("title") or proposal_id),
                "summary": str(suggestion.get("summary") or "客户正式知识经 AI 判断后提交为候选共享公共知识，等待服务端 admin 审核。"),
                "source": str(suggestion.get("provider") or "formal_knowledge_universal_local_upload"),
                "source_meta": {
                    "source_key": source_key,
                    "source_items": suggestion.get("source_items", []),
                    "universal_reason": suggestion.get("universal_reason", ""),
                    "llm_used": bool(suggestion.get("llm_used")),
                },
                "operations": [
                    {
                        "op": "upsert_json",
                        "path": f"{content['category_id']}/items/{content['id']}.json",
                        "content": content,
                    }
                ],
            }
            try:
                response = self.vps.post_json("/v1/shared/proposals", payload, token=token)
            except VpsClientError as exc:
                return {
                    "ok": False,
                    "tenant_id": tenant,
                    "error": str(exc),
                    "uploaded": uploaded,
                    "skipped": skipped,
                    "checked_count": len(unchecked),
                }
            proposal = response.get("proposal") if isinstance(response, dict) else response
            proposal_status = str(proposal.get("status") or "") if isinstance(proposal, dict) else ""
            skip_reason = str(proposal.get("skip_reason") or "") if isinstance(proposal, dict) else ""
            cache_status = skip_reason or ("uploaded" if proposal_status != "skipped" else "duplicate")
            for key in formal_keys:
                cache[key] = {"status": cache_status, "proposal_id": proposal_id, "updated_at": now_iso_local(), "llm_used": bool(suggestion.get("llm_used"))}
            record = {"suggestion": suggestion, "proposal": proposal}
            if skip_reason or proposal_status == "skipped":
                skipped.append(record)
            else:
                uploaded.append(record)
        for entry in unchecked:
            source_key = str(entry.get("source_key") or "")
            if source_key and source_key not in suggested_keys:
                cache[source_key] = {"status": "not_recommended", "updated_at": now_iso_local(), "llm_used": bool(use_llm)}
        self.write_shared_formal_scan_cache(cache)
        return {
            "ok": True,
            "tenant_id": tenant,
            "uploaded": uploaded,
            "skipped": skipped,
            "checked_count": len(unchecked),
            "collected_count": len(entries),
            "cache_count": len(cache),
            "use_llm": bool(use_llm),
        }

    def read_shared_formal_scan_cache(self) -> dict[str, dict[str, Any]]:
        path = shared_formal_scan_cache_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): value for key, value in payload.items() if isinstance(value, dict)}

    def write_shared_formal_scan_cache(self, cache: dict[str, dict[str, Any]]) -> None:
        path = shared_formal_scan_cache_path()
        write_json_payload(path, cache)

    def read_shared_cloud_snapshot_cache(self) -> dict[str, Any]:
        payload = read_json_payload(shared_runtime_snapshot_path(), default={})
        return payload if isinstance(payload, dict) else {}

    def shared_cloud_cache_status(self) -> dict[str, Any]:
        cached = self.read_shared_cloud_snapshot_cache()
        return {
            "exists": bool(cached),
            "valid": shared_cloud_cache_valid(cached),
            "version": str(cached.get("version") or ""),
            "source": str(cached.get("source") or ""),
            **shared_cloud_cache_policy_summary(cached),
            "item_count": len(cached.get("items", [])) if isinstance(cached.get("items"), list) else 0,
            "category_count": len(cached.get("categories", [])) if isinstance(cached.get("categories"), list) else 0,
            "cache_root": str(shared_runtime_cache_root()),
            "snapshot_path": str(shared_runtime_snapshot_path()),
        }


def shared_formal_scan_cache_path():
    return runtime_app_root() / "sync" / "shared_formal_scan_cache.json"


def local_node_cache_path():
    return runtime_app_root() / "sync" / "local_node.json"


def write_shared_cloud_snapshot_cache(snapshot: dict[str, Any]) -> dict[str, Any]:
    root = shared_runtime_cache_root().resolve()
    ensure_shared_cache_root_safe(root)
    temp_root = root.with_name(root.name + ".tmp")
    ensure_shared_cache_root_safe(temp_root)
    if temp_root.exists():
        remove_tree_with_retry(temp_root)
    temp_root.mkdir(parents=True, exist_ok=True)
    normalized = normalize_shared_cloud_snapshot(snapshot)
    write_json_payload(temp_root / "snapshot.json", normalized)
    materialize_shared_cloud_knowledge_tree(temp_root, normalized)
    if not root.exists():
        replace_path_with_retry(temp_root, root)
    else:
        try:
            remove_tree_with_retry(root)
            replace_path_with_retry(temp_root, root)
        except OSError as exc:
            if not is_windows_lock_error(exc):
                raise
            # Windows can transiently deny deleting snapshot.json while another
            # request reads it. Fallback to in-place refresh to avoid gating lockout.
            apply_shared_cache_overlay(root=root, temp_root=temp_root, snapshot=normalized)
            remove_tree_with_retry(temp_root)
    return {
        "snapshot_version": str(normalized.get("version") or ""),
        "item_count": len(normalized.get("items", [])),
        "category_count": len(normalized.get("categories", [])),
        **shared_cloud_cache_policy_summary(normalized),
        "cache_root": str(root),
        "snapshot_path": str(root / "snapshot.json"),
    }


def write_shared_cloud_snapshot_lease_cache(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Renew/expire lease metadata without rebuilding the full shared cache tree."""
    root = shared_runtime_cache_root().resolve()
    ensure_shared_cache_root_safe(root)
    normalized = normalize_shared_cloud_snapshot(snapshot)
    if not root.exists():
        return write_shared_cloud_snapshot_cache(normalized)
    write_json_payload(root / "snapshot.json", normalized)
    return {
        "snapshot_version": str(normalized.get("version") or ""),
        "item_count": len(normalized.get("items", [])),
        "category_count": len(normalized.get("categories", [])),
        **shared_cloud_cache_policy_summary(normalized),
        "cache_root": str(root),
        "snapshot_path": str(root / "snapshot.json"),
    }


def normalize_shared_cloud_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    items = [dict(item) for item in snapshot.get("items", []) if isinstance(item, dict)]
    categories = [dict(item) for item in snapshot.get("categories", []) if isinstance(item, dict)]
    source_policy = dict(snapshot.get("cache_policy")) if isinstance(snapshot.get("cache_policy"), dict) else {}
    ttl_seconds = clamp_int(snapshot.get("ttl_seconds") or source_policy.get("ttl_seconds"), default=1800, minimum=60, maximum=86400)
    refresh_after_seconds = clamp_int(
        snapshot.get("refresh_after_seconds") or source_policy.get("refresh_after_seconds"),
        default=min(300, max(60, ttl_seconds // 3)),
        minimum=30,
        maximum=ttl_seconds,
    )
    generated_at = str(snapshot.get("generated_at") or snapshot.get("issued_at") or source_policy.get("issued_at") or now_iso_local())
    issued_at = str(snapshot.get("issued_at") or source_policy.get("issued_at") or generated_at)
    refresh_after_at = str(snapshot.get("refresh_after_at") or source_policy.get("refresh_after_at") or "")
    expires_at = str(snapshot.get("expires_at") or source_policy.get("expires_at") or "")
    if not refresh_after_at or not expires_at:
        issued = parse_iso_datetime(issued_at) or datetime.now(timezone.utc)
        if not refresh_after_at:
            refresh_after_at = (issued + datetime_delta_seconds(refresh_after_seconds)).isoformat(timespec="seconds")
        if not expires_at:
            expires_at = (issued + datetime_delta_seconds(ttl_seconds)).isoformat(timespec="seconds")
    lease_id = str(snapshot.get("lease_id") or source_policy.get("lease_id") or stable_digest(f"{snapshot.get('tenant_id') or ''}:{snapshot.get('version') or ''}:{issued_at}", 20))
    cache_policy = dict(source_policy)
    cache_policy.update(
        {
            "mode": str(cache_policy.get("mode") or "cloud_authoritative_lease"),
            "ttl_seconds": ttl_seconds,
            "refresh_after_seconds": refresh_after_seconds,
            "issued_at": issued_at,
            "refresh_after_at": refresh_after_at,
            "expires_at": expires_at,
            "lease_id": lease_id,
            "requires_cloud_refresh": True,
        }
    )
    return {
        "schema_version": int(snapshot.get("schema_version") or 1),
        "source": str(snapshot.get("source") or "cloud_official_shared_library"),
        "version": str(snapshot.get("version") or stable_digest(json.dumps(items, ensure_ascii=False, sort_keys=True), 20)),
        "tenant_id": str(snapshot.get("tenant_id") or ""),
        "tenant_industry_id": str(snapshot.get("tenant_industry_id") or ""),
        "industry_catalog": [dict(item) for item in snapshot.get("industry_catalog", []) if isinstance(item, dict)],
        "generated_at": generated_at,
        "ttl_seconds": ttl_seconds,
        "refresh_after_seconds": refresh_after_seconds,
        "issued_at": issued_at,
        "refresh_after_at": refresh_after_at,
        "expires_at": expires_at,
        "lease_id": lease_id,
        "cache_policy": cache_policy,
        "categories": categories,
        "items": items,
        "policy_bundle": dict(snapshot.get("policy_bundle")) if isinstance(snapshot.get("policy_bundle"), dict) else {},
        "deleted_item_ids": [str(item) for item in snapshot.get("deleted_item_ids", []) if str(item).strip()],
    }


def materialize_shared_cloud_knowledge_tree(root: Path, snapshot: dict[str, Any]) -> None:
    items = [item for item in snapshot.get("items", []) if isinstance(item, dict) and str(item.get("status") or "active") == "active"]
    category_ids: list[str] = []
    for category in snapshot.get("categories", []):
        if not isinstance(category, dict):
            continue
        category_id = safe_path_fragment(category.get("category_id") or category.get("id"))
        if category_id and category_id not in category_ids:
            category_ids.append(category_id)
    for item in items:
        category_id = safe_path_fragment(item.get("category_id") or "global_guidelines")
        if category_id and category_id not in category_ids:
            category_ids.append(category_id)
    registry_categories = []
    for category_id in category_ids:
        definition = SHARED_CATEGORY_DEFINITIONS.get(category_id)
        registry = dict(definition["registry"]) if definition else fallback_shared_category_registry(category_id)
        registry_categories.append(registry)
        category_root = root / category_id
        category_root.mkdir(parents=True, exist_ok=True)
        write_json_payload(category_root / "schema.json", dict(definition["schema"]) if definition else fallback_shared_schema(category_id))
        write_json_payload(category_root / "resolver.json", dict(definition["resolver"]) if definition else fallback_shared_resolver(category_id))
        (category_root / "items").mkdir(parents=True, exist_ok=True)
    registry_payload = {
        "schema_version": 1,
        "scope": "wechat_ai_customer_service_shared_cloud_cache",
        "source": "cloud_official_shared_library",
        "version": str(snapshot.get("version") or ""),
        "updated_at": str(snapshot.get("generated_at") or now_iso_local()),
        "categories": registry_categories,
    }
    write_json_payload(root / "registry.json", registry_payload)
    for item in items:
        category_id = safe_path_fragment(item.get("category_id") or "global_guidelines") or "global_guidelines"
        item_id = safe_path_fragment(item.get("item_id") or item.get("id") or item.get("title")) or ("shared_" + stable_digest(json.dumps(item, ensure_ascii=False, sort_keys=True), 16))
        write_json_payload(root / category_id / "items" / f"{item_id}.json", shared_cache_item_payload(item, category_id=category_id, item_id=item_id))


def shared_cache_item_payload(item: dict[str, Any], *, category_id: str, item_id: str) -> dict[str, Any]:
    source_data = dict(item.get("data")) if isinstance(item.get("data"), dict) else {}
    nested_data = dict(source_data.get("data")) if isinstance(source_data.get("data"), dict) else dict(source_data)
    title = str(item.get("title") or nested_data.get("title") or source_data.get("title") or item_id)
    content = str(item.get("content") or nested_data.get("guideline_text") or nested_data.get("content") or source_data.get("content") or "")
    keywords = normalize_text_list(item.get("keywords") or nested_data.get("keywords") or source_data.get("keywords"))
    applies_to = str(item.get("applies_to") or nested_data.get("applies_to") or source_data.get("applies_to") or "")
    nested_data.update(
        {
            "title": title,
            "guideline_text": str(nested_data.get("guideline_text") or content),
            "keywords": keywords,
            "applies_to": applies_to,
        }
    )
    runtime = dict(source_data.get("runtime")) if isinstance(source_data.get("runtime"), dict) else {}
    if category_id == "risk_control":
        nested_data.setdefault("allow_auto_reply", False)
        nested_data.setdefault("requires_handoff", True)
        runtime.setdefault("allow_auto_reply", False)
        runtime.setdefault("requires_handoff", True)
        runtime.setdefault("risk_level", "high")
    payload = dict(source_data)
    payload.update(
        {
            "schema_version": int(source_data.get("schema_version") or item.get("schema_version") or 1),
            "id": item_id,
            "item_id": item_id,
            "industry_id": str(item.get("industry_id") or source_data.get("industry_id") or "global"),
            "category_id": category_id,
            "status": str(item.get("status") or source_data.get("status") or "active"),
            "title": title,
            "content": content,
            "keywords": keywords,
            "applies_to": applies_to,
            "data": nested_data,
            "runtime": runtime,
            "source": {
                "type": "cloud_official_shared_library",
                "item_id": str(item.get("item_id") or item_id),
                "version": str(item.get("version") or ""),
            },
            "metadata": {
                **(source_data.get("metadata") if isinstance(source_data.get("metadata"), dict) else {}),
                "knowledge_layer": "shared",
                "cloud_item_id": str(item.get("item_id") or item_id),
                "cloud_updated_at": str(item.get("updated_at") or ""),
            },
        }
    )
    return payload


def fallback_shared_category_registry(category_id: str) -> dict[str, Any]:
    return {
        "id": category_id,
        "name": category_id,
        "kind": "global",
        "path": category_id,
        "enabled": True,
        "participates_in_reply": True,
        "participates_in_learning": False,
        "participates_in_diagnostics": True,
        "sort_order": 100,
    }


def fallback_shared_schema(category_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "category_id": category_id,
        "display_name": category_id,
        "item_title_field": "title",
        "fields": [
            {"id": "title", "label": "title", "type": "short_text", "required": True},
            {"id": "keywords", "label": "keywords", "type": "tags", "required": False},
            {"id": "guideline_text", "label": "guideline", "type": "long_text", "required": True},
            {"id": "applies_to", "label": "applies_to", "type": "long_text", "required": False},
        ],
    }


def fallback_shared_resolver(category_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "category_id": category_id,
        "match_fields": ["title", "keywords", "guideline_text", "applies_to", "content"],
        "intent_fields": ["keywords"],
        "reply_fields": ["guideline_text", "content", "applies_to"],
        "minimum_confidence": 0.34,
        "default_action": "shared_cloud_context",
    }


def ensure_shared_cache_root_safe(root: Path) -> None:
    expected_parent = (runtime_app_root() / "cache").resolve()
    resolved = root.resolve()
    if expected_parent not in resolved.parents:
        raise RuntimeError(f"unsafe shared cache root: {resolved}")


def read_json_payload(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json_payload(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temp.write_text(serialized, encoding="utf-8")
    try:
        replace_path_with_retry(temp, path)
    except OSError as exc:
        if not is_windows_lock_error(exc):
            raise
        # Fallback for Windows: when the target file is opened in read mode by
        # another request, rename-replace can fail but direct overwrite usually works.
        write_text_with_retry(path, serialized)
        try:
            temp.unlink()
        except OSError:
            pass


def replace_path_with_retry(source: Path, destination: Path) -> None:
    last_error: OSError | None = None
    for index, delay in enumerate(WINDOWS_FILE_RETRY_DELAYS):
        try:
            source.replace(destination)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if not is_windows_lock_error(exc):
                raise
            if index < len(WINDOWS_FILE_RETRY_DELAYS) - 1:
                time.sleep(delay)
    if last_error is not None:
        raise last_error


def remove_tree_with_retry(path: Path) -> None:
    last_error: OSError | None = None
    for index, delay in enumerate(WINDOWS_FILE_RETRY_DELAYS):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if not is_windows_lock_error(exc):
                raise
            if index < len(WINDOWS_FILE_RETRY_DELAYS) - 1:
                time.sleep(delay)
    if last_error is not None:
        raise last_error


def remove_path_with_retry(path: Path) -> None:
    last_error: OSError | None = None
    for index, delay in enumerate(WINDOWS_FILE_RETRY_DELAYS):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if not is_windows_lock_error(exc):
                raise
            if index < len(WINDOWS_FILE_RETRY_DELAYS) - 1:
                time.sleep(delay)
    if last_error is not None:
        raise last_error


def apply_shared_cache_overlay(*, root: Path, temp_root: Path, snapshot: dict[str, Any]) -> None:
    """Refresh shared cache in place without deleting locked snapshot.json."""
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.name == "snapshot.json":
            continue
        remove_path_with_retry(child)
    for child in temp_root.iterdir():
        if child.name == "snapshot.json":
            continue
        target = root / child.name
        remove_path_with_retry(target)
        replace_path_with_retry(child, target)
    write_json_payload(root / "snapshot.json", snapshot)


def is_windows_lock_error(exc: OSError) -> bool:
    if os.name != "nt":
        return False
    return int(getattr(exc, "winerror", 0) or 0) in {5, 32, 33} or int(getattr(exc, "errno", 0) or 0) in {13, 22}


def write_text_with_retry(path: Path, content: str) -> None:
    last_error: OSError | None = None
    for index, delay in enumerate(WINDOWS_FILE_RETRY_DELAYS):
        try:
            path.write_text(content, encoding="utf-8")
            return
        except OSError as exc:
            last_error = exc
            if not is_windows_lock_error(exc):
                raise
            if index < len(WINDOWS_FILE_RETRY_DELAYS) - 1:
                time.sleep(delay)
    if last_error is not None:
        raise last_error


def renew_shared_cloud_snapshot_cache(cached: dict[str, Any], lease_payload: dict[str, Any]) -> dict[str, Any]:
    if not cached:
        return {}
    merged = dict(cached)
    for key in (
        "generated_at",
        "ttl_seconds",
        "refresh_after_seconds",
        "issued_at",
        "refresh_after_at",
        "expires_at",
        "lease_id",
        "cache_policy",
        "tenant_industry_id",
        "industry_catalog",
        "policy_bundle",
    ):
        if key in lease_payload:
            merged[key] = lease_payload[key]
    if lease_payload.get("version"):
        merged["version"] = str(lease_payload.get("version") or merged.get("version") or "")
    result = write_shared_cloud_snapshot_lease_cache(merged)
    merged.update(
        {
            "snapshot_version": result.get("snapshot_version"),
            "item_count": result.get("item_count"),
            "category_count": result.get("category_count"),
        }
    )
    return read_json_payload(shared_runtime_snapshot_path(), default=merged)


def expire_shared_cloud_snapshot_cache(cached: dict[str, Any], *, reason: str = "") -> dict[str, Any]:
    if not isinstance(cached, dict) or not cached:
        return {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    expired = dict(cached)
    expired["refresh_after_at"] = now
    expired["expires_at"] = now
    expired["issued_at"] = str(expired.get("issued_at") or now)
    policy = dict(expired.get("cache_policy")) if isinstance(expired.get("cache_policy"), dict) else {}
    policy.update({"issued_at": expired["issued_at"], "refresh_after_at": now, "expires_at": now, "requires_cloud_refresh": True})
    if reason:
        policy["last_error"] = reason[:280]
        expired["last_error"] = reason[:280]
    expired["cache_policy"] = policy
    try:
        write_shared_cloud_snapshot_lease_cache(expired)
    except Exception:
        return expired
    return read_json_payload(shared_runtime_snapshot_path(), default=expired)


def shared_cloud_cache_policy_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    policy = snapshot.get("cache_policy") if isinstance(snapshot.get("cache_policy"), dict) else {}
    return {
        "ttl_seconds": safe_int(snapshot.get("ttl_seconds") or policy.get("ttl_seconds"), default=0),
        "refresh_after_seconds": safe_int(snapshot.get("refresh_after_seconds") or policy.get("refresh_after_seconds"), default=0),
        "issued_at": str(snapshot.get("issued_at") or policy.get("issued_at") or ""),
        "refresh_after_at": str(snapshot.get("refresh_after_at") or policy.get("refresh_after_at") or ""),
        "expires_at": str(snapshot.get("expires_at") or policy.get("expires_at") or ""),
        "lease_id": str(snapshot.get("lease_id") or policy.get("lease_id") or ""),
        "cache_policy_mode": str(policy.get("mode") or ""),
        "requires_cloud_refresh": bool(snapshot) and policy.get("requires_cloud_refresh") is not False,
        "tenant_industry_id": str(snapshot.get("tenant_industry_id") or ""),
    }


def shared_cloud_cache_valid(snapshot: dict[str, Any]) -> bool:
    if not isinstance(snapshot, dict) or str(snapshot.get("source") or "") != "cloud_official_shared_library":
        return False
    expires_at = str(snapshot.get("expires_at") or "")
    if not expires_at and isinstance(snapshot.get("cache_policy"), dict):
        expires_at = str(snapshot["cache_policy"].get("expires_at") or "")
    expires = parse_iso_datetime(expires_at)
    return bool(expires and expires > datetime.now(timezone.utc))


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


def datetime_delta_seconds(seconds: int):
    return timedelta(seconds=max(0, int(seconds)))


def safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    number = safe_int(value, default=default)
    return max(minimum, min(maximum, number))


def safe_path_fragment(value: Any) -> str:
    text = str(value or "").strip()
    allowed = []
    for char in text:
        if char.isalnum() or char in {"_", "-"}:
            allowed.append(char)
        elif char.isspace():
            allowed.append("_")
    return "".join(allowed).strip("_")


def normalize_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).replace("；", ",").replace("、", ",").split(",") if item.strip()]


def stable_local_node_id() -> str:
    seed = f"{socket.gethostname()}|{runtime_app_root()}".encode("utf-8", errors="ignore")
    return "local_" + hashlib.sha256(seed).hexdigest()[:16]


def now_iso_local() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")
