"""Customer-service workbench APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from apps.wechat_ai_customer_service.adapters.wechat_connector import WeChatConnector, WeChatConnectorError
from ..services.customer_service_settings import CustomerServiceSettings
from ..services.customer_service_runtime import CustomerServiceRuntime
from ..services.session_monitor import SessionMonitor


router = APIRouter(prefix="/api/customer-service", tags=["customer-service"])


@router.get("/settings")
def get_settings(tenant_id: str | None = Query(default=None)) -> dict[str, Any]:
    return {"ok": True, "item": CustomerServiceSettings(tenant_id=tenant_id).summary()}


@router.put("/settings")
def save_settings(payload: dict[str, Any], tenant_id: str | None = Query(default=None)) -> dict[str, Any]:
    service = CustomerServiceSettings(tenant_id=tenant_id)
    settings = service.save(payload or {})
    return {"ok": True, "item": service.summary() | {"settings": settings}}


@router.get("/runtime/status")
def runtime_status(tenant_id: str | None = Query(default=None)) -> dict[str, Any]:
    return {"ok": True, "item": CustomerServiceRuntime(tenant_id=tenant_id).status()}


@router.post("/runtime/start")
def start_runtime(request: Request, tenant_id: str | None = Query(default=None)) -> dict[str, Any]:
    token = str(request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
    return CustomerServiceRuntime(tenant_id=tenant_id).start(token=token)


@router.post("/runtime/stop")
def stop_runtime(tenant_id: str | None = Query(default=None)) -> dict[str, Any]:
    return CustomerServiceRuntime(tenant_id=tenant_id).stop()


@router.get("/sessions")
def list_sessions(tenant_id: str | None = Query(default=None)) -> dict[str, Any]:
    settings_service = CustomerServiceSettings(tenant_id=tenant_id)
    settings = settings_service.get()
    configured = settings_service.list_session_targets()
    monitor = SessionMonitor(tenant_id=tenant_id)
    monitor_sessions = monitor.all_sessions()
    items = merge_customer_service_sessions(
        configured_sessions=configured,
        monitor_sessions=monitor_sessions,
        respond_all_unread_sessions=bool(settings.get("respond_all_unread_sessions", False)),
        session_targets_managed=bool(settings.get("session_targets_managed", False)),
    )
    return {
        "ok": True,
        "items": items,
        "sessions": items,
        "respond_all_unread_sessions": bool(settings.get("respond_all_unread_sessions", False)),
        "session_targets_managed": bool(settings.get("session_targets_managed", False)),
    }


@router.post("/sessions/discover")
def discover_sessions(tenant_id: str | None = Query(default=None)) -> dict[str, Any]:
    settings_service = CustomerServiceSettings(tenant_id=tenant_id)
    try:
        source = WeChatConnector().list_sessions(fresh=True)
    except WeChatConnectorError as exc:
        message = str(exc) or "未能连接微信主窗口。"
        return {
            "ok": False,
            "items": [],
            "detail": "wechat_connector_unavailable",
            "message": message,
            "source": {"ok": False, "error": "wechat_connector_unavailable", "detail": message},
        }
    except Exception as exc:  # pragma: no cover - defensive guard for runtime failures
        message = f"识别会话失败：{exc}"
        return {
            "ok": False,
            "items": [],
            "detail": "customer_service_discover_failed",
            "message": message,
            "source": {"ok": False, "error": "customer_service_discover_failed", "detail": repr(exc)},
        }
    sessions = [item for item in source.get("sessions", []) or [] if isinstance(item, dict)]
    merged = settings_service.merge_discovered_sessions(sessions)
    settings = merged.get("settings") if isinstance(merged.get("settings"), dict) else settings_service.get()
    monitor = SessionMonitor(tenant_id=tenant_id)
    items = merge_customer_service_sessions(
        configured_sessions=merged.get("items", []),
        monitor_sessions=monitor.all_sessions(),
        respond_all_unread_sessions=bool(settings.get("respond_all_unread_sessions", False)),
        session_targets_managed=bool(settings.get("session_targets_managed", False)),
    )
    return {
        "ok": bool(source.get("ok", True)),
        "items": items,
        "sessions": items,
        "added_count": int(merged.get("added_count") or 0),
        "discovered_count": int(merged.get("discovered_count") or 0),
        "archived_count": int(merged.get("archived_count") or 0),
        "warnings": merged.get("warnings") if isinstance(merged.get("warnings"), list) else [],
        "respond_all_unread_sessions": bool(settings.get("respond_all_unread_sessions", False)),
        "session_targets_managed": bool(settings.get("session_targets_managed", False)),
        "source": source,
    }


@router.patch("/sessions/{session_name}")
def update_session(session_name: str, payload: dict[str, Any], tenant_id: str | None = Query(default=None)) -> dict[str, Any]:
    settings_service = CustomerServiceSettings(tenant_id=tenant_id)
    result = settings_service.update_session_target(session_name, payload or {})
    settings = result.get("settings") if isinstance(result.get("settings"), dict) else settings_service.get()
    monitor = SessionMonitor(tenant_id=tenant_id)
    items = merge_customer_service_sessions(
        configured_sessions=result.get("items", []),
        monitor_sessions=monitor.all_sessions(),
        respond_all_unread_sessions=bool(settings.get("respond_all_unread_sessions", False)),
        session_targets_managed=bool(settings.get("session_targets_managed", False)),
    )
    return {
        "ok": True,
        "item": result.get("item"),
        "items": items,
        "sessions": items,
        "warnings": result.get("warnings") if isinstance(result.get("warnings"), list) else [],
        "settings": settings,
        "respond_all_unread_sessions": bool(settings.get("respond_all_unread_sessions", False)),
        "session_targets_managed": bool(settings.get("session_targets_managed", False)),
    }


def merge_customer_service_sessions(
    *,
    configured_sessions: list[dict[str, Any]],
    monitor_sessions: list[dict[str, Any]],
    respond_all_unread_sessions: bool,
    session_targets_managed: bool,
) -> list[dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    for raw in configured_sessions or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("target_name") or "").strip()
        if not name:
            continue
        by_name[name] = {
            "name": name,
            "display_name": str(raw.get("display_name") or name),
            "enabled": bool(raw.get("enabled", False)),
            "exact": bool(raw.get("exact", True)),
            "archived": bool(raw.get("archived", False)),
            "conversation_type": str(raw.get("conversation_type") or "unknown"),
            "source": str(raw.get("source") or "manual"),
        }
    for raw in monitor_sessions or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        if name not in by_name and session_targets_managed:
            continue
        item = by_name.get(name) or {
            "name": name,
            "display_name": name,
            "enabled": bool(respond_all_unread_sessions),
            "exact": True,
            "archived": False,
            "conversation_type": "unknown",
            "source": "monitor",
        }
        item["last_message_time"] = str(raw.get("last_message_time") or "")
        item["unread_detected"] = bool(raw.get("unread_detected", False))
        item["priority_score"] = int(raw.get("priority_score", 0) or 0)
        item["first_seen_at"] = str(raw.get("first_seen_at") or "")
        item["last_seen_at"] = str(raw.get("last_seen_at") or "")
        by_name[name] = item
    items = list(by_name.values())
    items.sort(
        key=lambda item: (
            0 if item.get("enabled") else 1,
            0 if item.get("unread_detected") else 1,
            -int(item.get("priority_score", 0) or 0),
            str(item.get("display_name") or item.get("name") or ""),
        )
    )
    return items
