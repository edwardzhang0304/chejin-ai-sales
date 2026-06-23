"""Authentication APIs for the local admin console."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..auth_context import current_auth_context
from apps.wechat_ai_customer_service.auth import AuthService, load_auth_settings
from apps.wechat_ai_customer_service.cloud_gate import cloud_gate_error_payload, cloud_gate_status, cloud_required_enabled
from apps.wechat_ai_customer_service.knowledge_paths import shared_runtime_cache_valid
from apps.wechat_ai_customer_service.sync import VpsLocalSyncService


router = APIRouter(prefix="/api/auth", tags=["auth"])
compat_router = APIRouter(prefix="/v1/auth", tags=["auth-compat"])


@router.post("/login")
def login(payload: dict[str, Any]) -> dict[str, Any]:
    enforce_cloud_gate_before_login(payload)
    service = AuthService()
    username = str(payload.get("username") or "")
    try:
        session = service.login(
            username=username,
            password=str(payload.get("password") or ""),
            tenant_id=str(payload.get("tenant_id") or "") or None,
        )
    except Exception as exc:
        detail = str(exc)
        if detail in {"account initialization required", "email verification required"}:
            try:
                result = service.start_login(
                    username=username,
                    password=str(payload.get("password") or ""),
                    tenant_id=str(payload.get("tenant_id") or "") or None,
                    device_id=str(payload.get("device_id") or ""),
                    device_name=str(payload.get("device_name") or ""),
                )
            except Exception as start_exc:
                raise HTTPException(status_code=401, detail=str(start_exc)) from start_exc
            return {"ok": True, **result}
        if username.strip() == "admin" and ("HTTP Error 401" in detail or "invalid credentials" in detail):
            detail = "admin 登录失败：当前启用了 VPS 统一授权，请使用统一管理员密码。"
        raise HTTPException(status_code=401, detail=detail) from exc
    return {"ok": True, "session": session.to_dict()}


@router.post("/login/start")
def start_login(payload: dict[str, Any]) -> dict[str, Any]:
    enforce_cloud_gate_before_login(payload)
    service = AuthService()
    try:
        result = service.start_login(
            username=str(payload.get("username") or ""),
            password=str(payload.get("password") or ""),
            tenant_id=str(payload.get("tenant_id") or "") or None,
            device_id=str(payload.get("device_id") or ""),
            device_name=str(payload.get("device_name") or ""),
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"ok": True, **result}


@router.post("/login/bind-email/start")
def start_login_email_binding(payload: dict[str, Any]) -> dict[str, Any]:
    enforce_cloud_gate_before_login(payload)
    service = AuthService()
    try:
        return {
            "ok": True,
            **service.start_login_email_binding(
                challenge_id=str(payload.get("challenge_id") or ""),
                email=str(payload.get("email") or ""),
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.post("/login/verify")
def verify_login(payload: dict[str, Any]) -> dict[str, Any]:
    enforce_cloud_gate_before_login(payload)
    service = AuthService()
    try:
        session = service.verify_login(
            challenge_id=str(payload.get("challenge_id") or ""),
            code=str(payload.get("code") or ""),
            trust_device=bool(payload.get("trust_device")),
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"ok": True, "session": session.to_dict()}


@router.post("/initialize/start")
def start_account_initialization(payload: dict[str, Any]) -> dict[str, Any]:
    enforce_cloud_gate_before_login(payload)
    service = AuthService()
    try:
        return {
            "ok": True,
            **service.start_account_initialization(
                challenge_id=str(payload.get("challenge_id") or ""),
                email=str(payload.get("email") or ""),
                new_password=str(payload.get("new_password") or ""),
                smtp_config=payload.get("smtp_config") if isinstance(payload.get("smtp_config"), dict) else None,
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.post("/initialize/verify")
def verify_account_initialization(payload: dict[str, Any]) -> dict[str, Any]:
    enforce_cloud_gate_before_login(payload)
    service = AuthService()
    try:
        return {
            "ok": True,
            **service.verify_account_initialization(
                challenge_id=str(payload.get("challenge_id") or ""),
                code=str(payload.get("code") or ""),
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.post("/logout")
def logout(request: Request) -> dict[str, Any]:
    service = AuthService()
    token = str(request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
    return {"ok": True, "revoked": service.revoke(token) if token else False}


@router.post("/change-password")
def change_password(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    context = current_auth_context(request)
    if not context.authenticated:
        raise HTTPException(status_code=401, detail="登录后才能修改密码")
    try:
        return {
            "ok": True,
            **AuthService().change_password(
                context.session,
                current_password=str(payload.get("current_password") or ""),
                new_password=str(payload.get("new_password") or ""),
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/security")
def security_profile(request: Request) -> dict[str, Any]:
    context = current_auth_context(request)
    if not context.authenticated:
        raise HTTPException(status_code=401, detail="登录后才能查看账号安全")
    try:
        return {"ok": True, "security": AuthService().security_profile(context.session)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/email/start")
def start_email_binding(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    context = current_auth_context(request)
    if not context.authenticated:
        raise HTTPException(status_code=401, detail="登录后才能绑定邮箱")
    try:
        return {"ok": True, **AuthService().start_email_binding(context.session, email=str(payload.get("email") or ""))}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/email/verify")
def verify_email_binding(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    context = current_auth_context(request)
    if not context.authenticated:
        raise HTTPException(status_code=401, detail="登录后才能绑定邮箱")
    try:
        return {
            "ok": True,
            **AuthService().verify_email_binding(
                context.session,
                challenge_id=str(payload.get("challenge_id") or ""),
                code=str(payload.get("code") or ""),
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/change-password/start")
def start_password_change(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    context = current_auth_context(request)
    if not context.authenticated:
        raise HTTPException(status_code=401, detail="登录后才能修改密码")
    try:
        return {
            "ok": True,
            **AuthService().start_password_change(
                context.session,
                current_password=str(payload.get("current_password") or ""),
                new_password=str(payload.get("new_password") or ""),
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/change-password/verify")
def verify_password_change(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    context = current_auth_context(request)
    if not context.authenticated:
        raise HTTPException(status_code=401, detail="登录后才能修改密码")
    try:
        return {
            "ok": True,
            **AuthService().verify_password_change(
                context.session,
                challenge_id=str(payload.get("challenge_id") or ""),
                code=str(payload.get("code") or ""),
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/me")
def me(request: Request) -> dict[str, Any]:
    context = current_auth_context(request)
    return {"ok": True, "auth": public_session(context.to_dict())}


@router.post("/cloud-gate/prepare")
def prepare_cloud_gate(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    report = prepare_cloud_gate_for_login(payload or {}, force_refresh=bool((payload or {}).get("force", True)))
    message = (
        ""
        if report.get("ok")
        else str((cloud_gate_error_payload().get("detail") or {}).get("message") or "当前客户端未通过云端授权校验。")
    )
    return {"ok": bool(report.get("ok")), "required": bool(report.get("required")), "message": message, **report}


def public_session(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    if "token" in result:
        result["token"] = result["token"][:8] + "..." if result.get("token") else ""
    session = result.get("session")
    if isinstance(session, dict) and session.get("token"):
        session["token"] = str(session["token"])[:8] + "..."
    return result


def local_only_auth_service() -> AuthService:
    return AuthService(settings=replace(load_auth_settings(), vps_base_url=""))


def compat_login(payload: dict[str, Any]) -> dict[str, Any]:
    enforce_cloud_gate_before_login(payload)
    service = local_only_auth_service()
    username = str(payload.get("username") or "")
    try:
        session = service.login(
            username=username,
            password=str(payload.get("password") or ""),
            tenant_id=str(payload.get("tenant_id") or "") or None,
        )
    except Exception as exc:
        detail = str(exc)
        if detail in {"account initialization required", "email verification required"}:
            try:
                result = service.start_login(
                    username=username,
                    password=str(payload.get("password") or ""),
                    tenant_id=str(payload.get("tenant_id") or "") or None,
                    device_id=str(payload.get("device_id") or ""),
                    device_name=str(payload.get("device_name") or ""),
                )
            except Exception as start_exc:
                raise HTTPException(status_code=401, detail=str(start_exc)) from start_exc
            return {"ok": True, **result}
        raise HTTPException(status_code=401, detail=detail) from exc
    return {"ok": True, "session": session.to_dict()}


def compat_start_login(payload: dict[str, Any]) -> dict[str, Any]:
    enforce_cloud_gate_before_login(payload)
    try:
        result = local_only_auth_service().start_login(
            username=str(payload.get("username") or ""),
            password=str(payload.get("password") or ""),
            tenant_id=str(payload.get("tenant_id") or "") or None,
            device_id=str(payload.get("device_id") or ""),
            device_name=str(payload.get("device_name") or ""),
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"ok": True, **result}


def compat_start_login_email_binding(payload: dict[str, Any]) -> dict[str, Any]:
    enforce_cloud_gate_before_login(payload)
    try:
        return {
            "ok": True,
            **local_only_auth_service().start_login_email_binding(
                challenge_id=str(payload.get("challenge_id") or ""),
                email=str(payload.get("email") or ""),
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def compat_verify_login(payload: dict[str, Any]) -> dict[str, Any]:
    enforce_cloud_gate_before_login(payload)
    try:
        session = local_only_auth_service().verify_login(
            challenge_id=str(payload.get("challenge_id") or ""),
            code=str(payload.get("code") or ""),
            trust_device=bool(payload.get("trust_device")),
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"ok": True, "session": session.to_dict()}


def compat_start_account_initialization(payload: dict[str, Any]) -> dict[str, Any]:
    enforce_cloud_gate_before_login(payload)
    try:
        return {
            "ok": True,
            **local_only_auth_service().start_account_initialization(
                challenge_id=str(payload.get("challenge_id") or ""),
                email=str(payload.get("email") or ""),
                new_password=str(payload.get("new_password") or ""),
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def compat_verify_account_initialization(payload: dict[str, Any]) -> dict[str, Any]:
    enforce_cloud_gate_before_login(payload)
    try:
        return {
            "ok": True,
            **local_only_auth_service().verify_account_initialization(
                challenge_id=str(payload.get("challenge_id") or ""),
                code=str(payload.get("code") or ""),
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def compat_prepare_cloud_gate(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return prepare_cloud_gate(payload or {})


compat_router.add_api_route("/login", compat_login, methods=["POST"])
compat_router.add_api_route("/login/start", compat_start_login, methods=["POST"])
compat_router.add_api_route("/login/bind-email/start", compat_start_login_email_binding, methods=["POST"])
compat_router.add_api_route("/login/verify", compat_verify_login, methods=["POST"])
compat_router.add_api_route("/cloud-gate/prepare", compat_prepare_cloud_gate, methods=["POST"])
compat_router.add_api_route("/initialize/start", compat_start_account_initialization, methods=["POST"])
compat_router.add_api_route("/initialize/verify", compat_verify_account_initialization, methods=["POST"])


def enforce_cloud_gate_before_login(payload: dict[str, Any] | None = None) -> None:
    if not cloud_required_enabled():
        return
    report = prepare_cloud_gate_for_login(payload or {}, force_refresh=True)
    if report.get("ok"):
        return
    error_payload = cloud_gate_error_payload()
    detail = error_payload.get("detail")
    if isinstance(detail, dict):
        detail = {**detail, "prepare": report}
    raise HTTPException(status_code=423, detail=detail or error_payload.get("message") or "cloud gate locked")


def prepare_cloud_gate_for_login(payload: dict[str, Any], *, force_refresh: bool) -> dict[str, Any]:
    required = cloud_required_enabled()
    tenant_id = str(payload.get("tenant_id") or "").strip() or "default"
    gate_before = cloud_gate_status()
    if not required:
        return {
            "ok": True,
            "required": False,
            "tenant_id": tenant_id,
            "cloud_gate": gate_before,
            "register_node": {"ok": True, "skipped": True, "reason": "cloud_not_required"},
            "shared_snapshot_sync": {"ok": True, "skipped": True, "reason": "cloud_not_required"},
        }
    sync_service = VpsLocalSyncService()
    register_result: dict[str, Any]
    try:
        register_result = sync_service.register_node(
            token="",
            tenant_id=tenant_id,
            display_name="Local Console Cloud Gate Bootstrap",
        )
    except Exception as exc:
        register_result = {"ok": False, "tenant_id": tenant_id, "error": repr(exc)}

    effective_force_refresh = bool(force_refresh)
    # If the gate is already healthy and the current shared-cache lease is valid,
    # avoid forcing a full rewrite on every page refresh.
    if gate_before.get("ok") and shared_runtime_cache_valid(tenant_id=tenant_id):
        effective_force_refresh = False

    snapshot_result: dict[str, Any]
    try:
        snapshot_result = sync_service.fetch_shared_knowledge_snapshot(
            token="",
            tenant_id=tenant_id,
            force=effective_force_refresh,
        )
    except Exception as exc:
        snapshot_result = {"ok": False, "tenant_id": tenant_id, "error": repr(exc)}
    gate_after = cloud_gate_status()
    return {
        "ok": bool(gate_after.get("ok")),
        "required": True,
        "tenant_id": tenant_id,
        "cloud_gate_before": gate_before,
        "cloud_gate": gate_after,
        "register_node": register_result,
        "shared_snapshot_sync": snapshot_result,
        "effective_force_refresh": effective_force_refresh,
    }
