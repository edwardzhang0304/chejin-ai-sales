"""AI smart recorder admin APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from apps.wechat_ai_customer_service.adapters.wechat_connector import WeChatConnectorError
from ..services.recorder_runtime import RecorderRuntime
from ..services.recorder_service import RecorderService


router = APIRouter(prefix="/api/recorder", tags=["recorder"])


@router.get("/summary")
def summary() -> dict[str, Any]:
    return {"ok": True, "item": RecorderService().summary()}


@router.get("/settings")
def settings() -> dict[str, Any]:
    return {"ok": True, "item": RecorderService().settings()}


@router.put("/settings")
def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    service = RecorderService()
    updated = service.save_settings(payload or {})
    response: dict[str, Any] = {"ok": True, "item": updated}
    if updated.get("enabled", True) is False:
        stop_result = RecorderRuntime().stop()
        response["message"] = "AI智能记录员已关闭，并停止了正在运行的监听任务。"
        response["runtime"] = stop_result.get("item")
    return response


@router.post("/discover")
def discover_sessions() -> dict[str, Any]:
    try:
        return RecorderService().discover_sessions()
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
            "detail": "recorder_discover_failed",
            "message": message,
            "source": {"ok": False, "error": "recorder_discover_failed", "detail": repr(exc)},
        }


@router.get("/conversations")
def conversations(
    conversation_type: str = Query("", pattern="^(|private|group|file_transfer|system|unknown)$"),
    status: str = Query("all", pattern="^(all|active|paused|ignored)$"),
) -> dict[str, Any]:
    return {"ok": True, "items": RecorderService().list_conversations(conversation_type=conversation_type, status=status)}


@router.post("/conversations")
def ensure_conversation(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "item": RecorderService().ensure_conversation(payload or {})}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/conversations/{conversation_id}")
def update_conversation(conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "item": RecorderService().update_conversation(conversation_id, payload or {})}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"conversation not found: {conversation_id}") from exc


@router.post("/capture")
def capture_selected(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    return RecorderService().capture_selected_once(send_notifications=bool(payload.get("send_notifications", False)))


@router.get("/runtime/status")
def runtime_status() -> dict[str, Any]:
    return {"ok": True, "item": RecorderRuntime().status()}


@router.post("/runtime/start")
def runtime_start() -> dict[str, Any]:
    return RecorderRuntime().start()


@router.post("/runtime/stop")
def runtime_stop(tenant_id: str | None = Query(default=None)) -> dict[str, Any]:
    return RecorderRuntime(tenant_id=tenant_id).stop()
