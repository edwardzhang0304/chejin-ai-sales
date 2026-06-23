"""Fail-closed cloud gate for WeChat customer-service local runtime."""

from __future__ import annotations

import json
import os
import hashlib
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode
from datetime import datetime, timezone
from typing import Any

from apps.wechat_ai_customer_service.auth.vps_client import discover_vps_base_url
from apps.wechat_ai_customer_service.knowledge_paths import parse_cloud_time, runtime_app_root, shared_runtime_cache_valid, shared_runtime_snapshot_path


_PROBE_CACHE: dict[str, Any] = {"base_url": "", "ok": False, "error": "", "expires_at": 0.0}
_NODE_PROBE_CACHE: dict[str, Any] = {"key": "", "ok": False, "error": "", "expires_at": 0.0}


def cloud_required_enabled() -> bool:
    return str(os.getenv("WECHAT_CLOUD_REQUIRED", "1")).strip().lower() in {"1", "true", "yes", "on"}


def cloud_online_freshness_seconds() -> int:
    try:
        value = int(os.getenv("WECHAT_CLOUD_ONLINE_MAX_AGE_SECONDS", "90"))
    except ValueError:
        value = 90
    return max(10, min(1800, value))


def cloud_probe_timeout_seconds() -> float:
    try:
        value = float(os.getenv("WECHAT_CLOUD_PROBE_TIMEOUT_SECONDS", "1.5"))
    except ValueError:
        value = 1.5
    return max(0.25, min(8.0, value))


def cloud_probe_cache_seconds() -> float:
    try:
        value = float(os.getenv("WECHAT_CLOUD_PROBE_CACHE_SECONDS", "5"))
    except ValueError:
        value = 5.0
    return max(0.0, min(60.0, value))


def cloud_node_probe_cache_seconds() -> float:
    try:
        value = float(os.getenv("WECHAT_CLOUD_NODE_PROBE_CACHE_SECONDS", "5"))
    except ValueError:
        value = 5.0
    return max(0.0, min(60.0, value))


def cloud_node_probe_timeout_seconds() -> float:
    try:
        value = float(os.getenv("WECHAT_CLOUD_NODE_PROBE_TIMEOUT_SECONDS", "2"))
    except ValueError:
        value = 2.0
    return max(0.25, min(8.0, value))


def cloud_node_verification_required() -> bool:
    return str(os.getenv("WECHAT_CLOUD_REQUIRE_NODE_VERIFIED", "1")).strip().lower() in {"1", "true", "yes", "on"}


def _read_local_node_cache() -> dict[str, Any]:
    path = runtime_app_root() / "sync" / "local_node.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _node_probe_cache_key(*, base_url: str, tenant_id: str, node_id: str, node_token: str) -> str:
    token_digest = hashlib.sha256(node_token.encode("utf-8")).hexdigest() if node_token else ""
    return f"{base_url}|{tenant_id}|{node_id}|{token_digest}"


def cloud_probe_node_access(base_url: str, *, tenant_id: str) -> tuple[bool, str, str]:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return False, "cloud_base_url_missing", ""
    node_cache = _read_local_node_cache()
    node_id = str(node_cache.get("node_id") or "").strip()
    node_token = str(node_cache.get("node_token") or "").strip()
    if not node_id or not node_token:
        return False, "node_credentials_missing", node_id

    cache_key = _node_probe_cache_key(base_url=base, tenant_id=tenant_id, node_id=node_id, node_token=node_token)
    now = time.monotonic()
    ttl = cloud_node_probe_cache_seconds()
    if _NODE_PROBE_CACHE.get("key") == cache_key and now < float(_NODE_PROBE_CACHE.get("expires_at") or 0.0):
        return bool(_NODE_PROBE_CACHE.get("ok")), str(_NODE_PROBE_CACHE.get("error") or ""), node_id

    query = urlencode({"tenant_id": tenant_id or "default", "node_id": node_id})
    request = urllib.request.Request(
        base + "/v1/local/commands?" + query,
        headers={"Accept": "application/json", "X-Node-Token": node_token},
        method="GET",
    )
    ok = False
    error = ""
    try:
        with urllib.request.urlopen(request, timeout=cloud_node_probe_timeout_seconds()) as response:
            status = int(getattr(response, "status", 200))
            ok = 200 <= status < 300
            if not ok:
                error = f"http_status_{status}"
    except urllib.error.HTTPError as exc:
        ok = False
        error = f"http_error_{int(getattr(exc, 'code', 0) or 0)}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        ok = False
        error = str(exc) or "network_error"
    _NODE_PROBE_CACHE.update(
        {
            "key": cache_key,
            "ok": ok,
            "error": error,
            "expires_at": now + ttl,
        }
    )
    return ok, error, node_id


def cloud_probe_health(base_url: str) -> tuple[bool, str]:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return False, "cloud_base_url_missing"
    now = time.monotonic()
    ttl = cloud_probe_cache_seconds()
    if (
        _PROBE_CACHE.get("base_url") == base
        and now < float(_PROBE_CACHE.get("expires_at") or 0.0)
    ):
        return bool(_PROBE_CACHE.get("ok")), str(_PROBE_CACHE.get("error") or "")
    request = urllib.request.Request(
        base + "/v1/health",
        headers={"Accept": "application/json"},
        method="GET",
    )
    ok = False
    error = ""
    try:
        with urllib.request.urlopen(request, timeout=cloud_probe_timeout_seconds()) as response:
            status = int(getattr(response, "status", 200))
            ok = 200 <= status < 300
            if not ok:
                error = f"http_status_{status}"
    except urllib.error.HTTPError as exc:
        ok = False
        error = f"http_error_{int(getattr(exc, 'code', 0) or 0)}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        ok = False
        error = str(exc) or "network_error"
    _PROBE_CACHE.update(
        {
            "base_url": base,
            "ok": ok,
            "error": error,
            "expires_at": now + ttl,
        }
    )
    return ok, error


def cloud_gate_status() -> dict[str, Any]:
    base_url = discover_vps_base_url().strip()
    snapshot_path = shared_runtime_snapshot_path()
    snapshot: dict[str, Any] = {}
    if snapshot_path.exists():
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                snapshot = payload
        except (OSError, json.JSONDecodeError):
            snapshot = {}

    source_ok = str(snapshot.get("source") or "") == "cloud_official_shared_library"
    cache_valid = shared_runtime_cache_valid()
    policy_bundle = snapshot.get("policy_bundle") if isinstance(snapshot.get("policy_bundle"), dict) else {}
    merged = policy_bundle.get("merged") if isinstance(policy_bundle.get("merged"), dict) else {}
    policy_ready = isinstance(merged.get("platform_safety_rules"), dict) and isinstance(merged.get("platform_understanding_rules"), dict)
    issued_at = parse_cloud_time(str(snapshot.get("issued_at") or ""))
    max_age = _effective_online_freshness_seconds(snapshot)
    now = datetime.now(timezone.utc)
    lease_fresh = bool(issued_at and (now - issued_at).total_seconds() <= max_age)
    strict_online = str(os.getenv("WECHAT_CLOUD_STRICT_ONLINE", "1")).strip().lower() in {"1", "true", "yes", "on"}
    probe_ok, probe_error = cloud_probe_health(base_url) if strict_online else (True, "")
    online_ready = (lease_fresh and probe_ok) if strict_online else True
    node_required = cloud_node_verification_required()
    probe_tenant = str(snapshot.get("tenant_id") or "default")
    node_ok, node_error, node_id = cloud_probe_node_access(base_url, tenant_id=probe_tenant) if node_required else (True, "", "")
    vps_configured = bool(base_url)

    ok = bool(vps_configured and source_ok and cache_valid and policy_ready and online_ready and node_ok)
    reason = ""
    if not vps_configured:
        reason = "cloud_base_url_missing"
    elif strict_online and not probe_ok:
        reason = "cloud_server_unreachable"
    elif node_required and not node_ok:
        reason = "cloud_node_not_verified"
    elif not source_ok:
        reason = "cloud_snapshot_missing_or_invalid_source"
    elif not cache_valid:
        reason = "cloud_snapshot_lease_expired"
    elif not policy_ready:
        reason = "cloud_policy_bundle_missing"
    elif not online_ready:
        reason = "cloud_snapshot_not_fresh"

    return {
        "ok": ok,
        "required": cloud_required_enabled(),
        "reason": reason,
        "vps_base_url": base_url,
        "snapshot_path": str(snapshot_path),
        "source_ok": source_ok,
        "cache_valid": cache_valid,
        "policy_ready": policy_ready,
        "strict_online": strict_online,
        "online_ready": online_ready,
        "node_required": node_required,
        "node_ok": node_ok,
        "node_error": node_error,
        "node_id": node_id,
        "lease_fresh": lease_fresh,
        "online_freshness_seconds": max_age,
        "probe_ok": probe_ok,
        "probe_error": probe_error,
        "tenant_industry_id": str(snapshot.get("tenant_industry_id") or ""),
        "issued_at": str(snapshot.get("issued_at") or ""),
        "expires_at": str(snapshot.get("expires_at") or ""),
    }


def _effective_online_freshness_seconds(snapshot: dict[str, Any]) -> int:
    """Keep strict-online freshness aligned with cloud snapshot refresh cadence.

    If snapshot refresh/TTL is configured slower than the default online freshness
    window, use the larger window so clients are not falsely locked while the
    cloud lease is still valid and the live VPS/node probes are healthy.
    """
    base = cloud_online_freshness_seconds()
    policy = snapshot.get("cache_policy") if isinstance(snapshot.get("cache_policy"), dict) else {}
    refresh_after = _safe_positive_int(snapshot.get("refresh_after_seconds"))
    if refresh_after <= 0:
        refresh_after = _safe_positive_int(policy.get("refresh_after_seconds"))
    ttl_seconds = _safe_positive_int(snapshot.get("ttl_seconds"))
    if ttl_seconds <= 0:
        ttl_seconds = _safe_positive_int(policy.get("ttl_seconds"))
    if refresh_after > 0:
        base = max(base, refresh_after + 30)
    if ttl_seconds > 0:
        base = max(base, ttl_seconds)
    return max(30, min(3600, base))


def _safe_positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def cloud_gate_error_payload() -> dict[str, Any]:
    status = cloud_gate_status()
    message = "当前客户端未通过云端授权校验。请连接服务端并完成共享行业知识库刷新后再使用。"
    return {
        "ok": False,
        "detail": {
            "code": "cloud_authoritative_access_required",
            "message": message,
            "cloud_gate": status,
        },
        "message": message,
        "cloud_gate": status,
    }
