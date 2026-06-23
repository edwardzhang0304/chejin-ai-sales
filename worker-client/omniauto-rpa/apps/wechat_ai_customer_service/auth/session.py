"""Local session service with VPS-ready authorization hooks."""

from __future__ import annotations

import json
import os
import secrets
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.auth.email_verification import EmailVerificationService, mask_email, normalize_email
from apps.wechat_ai_customer_service.auth.passwords import hash_password, validate_password_strength, verify_password
from apps.wechat_ai_customer_service.knowledge_paths import DEFAULT_TENANT_ID, active_tenant_id, runtime_app_root

from .models import AuthContext, AuthSession, AuthUser, Role, role_from_value, session_from_payload
from .vps_client import VpsAuthClient, VpsClientError, discover_vps_base_url


@dataclass(frozen=True)
class AuthSettings:
    required: bool
    vps_base_url: str
    timeout_seconds: float
    local_session_hours: int
    session_path: Path
    account_state_path: Path
    challenge_path: Path
    trusted_device_path: Path


def load_auth_settings() -> AuthSettings:
    auth_required_env = os.getenv("WECHAT_AUTH_REQUIRED")
    auth_required_default = cloud_auth_required_default()
    return AuthSettings(
        required=parse_bool(auth_required_env, default=auth_required_default),
        vps_base_url=discover_vps_base_url(),
        timeout_seconds=float(os.getenv("WECHAT_VPS_TIMEOUT_SECONDS") or "8"),
        local_session_hours=max(1, int(os.getenv("WECHAT_LOCAL_SESSION_HOURS") or "12")),
        session_path=Path(os.getenv("WECHAT_LOCAL_SESSION_PATH") or runtime_app_root() / "auth" / "sessions.json"),
        account_state_path=Path(os.getenv("WECHAT_LOCAL_ACCOUNTS_STATE_PATH") or runtime_app_root() / "auth" / "local_accounts.json"),
        challenge_path=Path(os.getenv("WECHAT_LOCAL_AUTH_CHALLENGE_PATH") or runtime_app_root() / "auth" / "local_auth_challenges.json"),
        trusted_device_path=Path(os.getenv("WECHAT_LOCAL_TRUSTED_DEVICE_PATH") or runtime_app_root() / "auth" / "local_trusted_devices.json"),
    )


class AuthService:
    def __init__(self, settings: AuthSettings | None = None) -> None:
        self.settings = settings or load_auth_settings()
        self.vps = VpsAuthClient(base_url=self.settings.vps_base_url, timeout_seconds=self.settings.timeout_seconds)
        self.email = EmailVerificationService()

    def login(self, username: str, password: str, *, tenant_id: str | None = None) -> AuthSession:
        if self.email.settings.otp_required or str(username or "").strip() == "admin":
            raise PermissionError("email verification required")
        if self.settings.vps_base_url:
            try:
                session = self.vps.login(username=username, password=password, tenant_id=tenant_id)
                self.save_session(session)
                return session
            except VpsClientError as exc:
                if self.settings.required and not self.can_fallback_to_local_initialization(username=username, password=password, error=exc):
                    raise
        session = self.local_login(username=username, password=password, tenant_id=tenant_id)
        self.save_session(session)
        return session

    def start_login(
        self,
        username: str,
        password: str,
        *,
        tenant_id: str | None = None,
        device_id: str = "",
        device_name: str = "",
    ) -> dict[str, Any]:
        admin_login = is_admin_username(username)
        if self.settings.vps_base_url:
            try:
                result = self.vps.start_login(
                    username=username,
                    password=password,
                    tenant_id=tenant_id,
                    device_id=device_id,
                    device_name=device_name,
                )
                if admin_login and result.get("requires_initialization"):
                    raise PermissionError("客户端 admin 使用服务端统一账号登录；请先在服务端后台完成 admin 首次设置。")
                session_payload = result.get("session") if isinstance(result.get("session"), dict) else None
                if session_payload:
                    session = session_from_payload({**session_payload, "source": "vps"})
                    self.save_session(session)
                    result["session"] = session.to_dict()
                return result
            except VpsClientError as exc:
                if admin_login:
                    message = str(exc)
                    if "HTTP 401" in message or "invalid credentials" in message.lower():
                        raise PermissionError("admin 登录失败：请使用服务端 admin 当前密码。") from exc
                    raise PermissionError(f"客户端 admin 使用服务端统一账号登录，当前无法连接服务端：{exc}") from exc
                if self.settings.required and not self.can_fallback_to_local_initialization(username=username, password=password, error=exc):
                    raise
        if admin_login:
            raise PermissionError("客户端 admin 使用服务端统一账号登录；当前未发现服务端，请启动服务端或配置 WECHAT_VPS_BASE_URL。")
        return self.local_start_login(
            username=username,
            password=password,
            tenant_id=tenant_id,
            device_id=device_id,
            device_name=device_name,
        )

    def can_fallback_to_local_initialization(self, *, username: str, password: str, error: Exception | None = None) -> bool:
        if is_admin_username(username):
            return False
        if error is not None:
            message = str(error).lower()
            if not any(marker in message for marker in {"404", "not found", "account initialization required", "authentication required"}):
                return False
        try:
            account = local_account(username)
        except PermissionError:
            return False
        return local_password_matches(account, password) and local_account_needs_initialization(account)

    def start_login_email_binding(self, *, challenge_id: str, email: str) -> dict[str, Any]:
        challenge_id = str(challenge_id or "").strip()
        if challenge_id.startswith("otp_"):
            if not self.settings.vps_base_url:
                raise PermissionError("VPS verification challenge requires a reachable WECHAT_VPS_BASE_URL")
            try:
                return self.vps.start_login_email_binding(challenge_id=challenge_id, email=email)
            except VpsClientError as exc:
                raise PermissionError(f"VPS verification challenge unavailable: {exc}") from exc
        return self.local_start_login_email_binding(challenge_id=challenge_id, email=email)

    def verify_login(self, *, challenge_id: str, code: str, trust_device: bool = False) -> AuthSession:
        challenge_id = str(challenge_id or "").strip()
        if challenge_id.startswith("otp_"):
            if not self.settings.vps_base_url:
                raise PermissionError("VPS verification challenge requires a reachable WECHAT_VPS_BASE_URL")
            try:
                session = self.vps.verify_login(challenge_id=challenge_id, code=code, trust_device=trust_device)
                self.save_session(session)
                return session
            except VpsClientError as exc:
                message = str(exc)
                lowered = message.lower()
                if "verification code required" in lowered:
                    raise PermissionError("verification code required") from exc
                if "verification code expired or not found" in lowered:
                    raise PermissionError("verification code expired or not found") from exc
                raise PermissionError(f"VPS verification challenge unavailable: {exc}") from exc
        session = self.local_verify_login(challenge_id=challenge_id, code=code, trust_device=trust_device)
        self.save_session(session)
        return session

    def start_account_initialization(
        self,
        *,
        challenge_id: str,
        email: str,
        new_password: str,
        smtp_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        challenge_id = str(challenge_id or "").strip()
        if challenge_id.startswith("init_"):
            if not self.settings.vps_base_url:
                raise PermissionError("VPS initialization challenge requires a reachable WECHAT_VPS_BASE_URL")
            try:
                return self.vps.start_account_initialization(
                    challenge_id=challenge_id,
                    email=email,
                    new_password=new_password,
                    smtp_config=smtp_config,
                )
            except VpsClientError as exc:
                raise PermissionError(f"VPS initialization challenge unavailable: {exc}") from exc
        return self.local_start_account_initialization(challenge_id=challenge_id, email=email, new_password=new_password)

    def verify_account_initialization(self, *, challenge_id: str, code: str) -> dict[str, Any]:
        challenge_id = str(challenge_id or "").strip()
        if challenge_id.startswith("init_"):
            if not self.settings.vps_base_url:
                raise PermissionError("VPS initialization challenge requires a reachable WECHAT_VPS_BASE_URL")
            try:
                return self.vps.verify_account_initialization(challenge_id=challenge_id, code=code)
            except VpsClientError as exc:
                raise PermissionError(f"VPS initialization challenge unavailable: {exc}") from exc
        return self.local_verify_account_initialization(challenge_id=challenge_id, code=code)

    def local_login(self, username: str, password: str, *, tenant_id: str | None = None) -> AuthSession:
        account = local_account(username)
        if not local_password_matches(account, password):
            raise PermissionError("invalid local credentials")
        if local_account_needs_initialization(account):
            raise PermissionError("account initialization required")
        return self.local_session_from_account(account, username=username, tenant_id=tenant_id)

    def local_session_from_account(self, account: dict[str, Any], *, username: str, tenant_id: str | None = None) -> AuthSession:
        role = role_from_value(account.get("role"))
        tenant_ids = tuple(str(item) for item in account.get("tenant_ids", []) if str(item))
        requested_tenant = active_tenant_id(tenant_id) if tenant_id else ""
        account_tenant = active_tenant_id(account.get("active_tenant_id") or (tenant_ids[0] if tenant_ids else DEFAULT_TENANT_ID))
        if role == Role.ADMIN or "*" in tenant_ids:
            tenant = requested_tenant or account_tenant or DEFAULT_TENANT_ID
        elif requested_tenant and requested_tenant not in tenant_ids:
            tenant = account_tenant
        else:
            tenant = requested_tenant or account_tenant
        now_value = datetime.now(timezone.utc)
        session_id = "sess_" + secrets.token_urlsafe(24)
        user = AuthUser(
            user_id=str(account.get("user_id") or username),
            role=role,
            tenant_ids=tenant_ids or (tenant,),
            display_name=str(account.get("display_name") or username),
            username=username,
            resource_scopes=tuple(str(item) for item in account.get("resource_scopes", ["*"]) if str(item)),
        )
        return AuthSession(
            session_id=session_id,
            token=session_id,
            user=user,
            active_tenant_id=tenant,
            issued_at=now_value.isoformat(),
            expires_at=(now_value + timedelta(hours=self.settings.local_session_hours)).isoformat(),
            source="local",
        )

    def local_create_initialization_challenge(
        self,
        *,
        account: dict[str, Any],
        username: str,
        tenant_id: str | None = None,
        device_id: str = "",
        device_name: str = "",
    ) -> dict[str, Any]:
        challenge_id = "local_init_" + secrets.token_urlsafe(24)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(timespec="seconds")
        user_id = str(account.get("user_id") or username)
        challenges = self.read_local_challenges()
        remove_prior_local_challenges(challenges, user_id=user_id, purposes={"initialize_account_pending", "initialize_account"})
        challenges[challenge_id] = {
            "challenge_id": challenge_id,
            "purpose": "initialize_account_pending",
            "username": username,
            "user_id": user_id,
            "role": str(account.get("role") or "customer"),
            "tenant_id": active_tenant_id(tenant_id or account.get("active_tenant_id") or DEFAULT_TENANT_ID),
            "device_fingerprint": local_device_fingerprint(user_id, device_id),
            "device_name": device_name,
            "expires_at": expires_at,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.write_local_challenges(challenges)
        missing = []
        if not account.get("password_hash"):
            missing.append("change_password")
        if not normalize_email(str(account.get("email") or "")):
            missing.append("bind_email")
        if not account.get("initialized_at"):
            missing.append("finish_initialization")
        return {
            "requires_initialization": True,
            "challenge_id": challenge_id,
            "expires_at": expires_at,
            "role": str(account.get("role") or "customer"),
            "username": username,
            "missing": sorted(set(missing), key=missing.index),
            "message": "首次登录前需要完成账号初始化。",
        }

    def local_start_account_initialization(self, *, challenge_id: str, email: str, new_password: str) -> dict[str, Any]:
        email = normalize_email(email)
        if not email:
            raise PermissionError("valid email required")
        try:
            validate_password_strength(new_password)
        except ValueError as exc:
            raise PermissionError(str(exc)) from exc
        challenges = self.read_local_challenges()
        challenge_id = str(challenge_id or "").strip()
        challenge = challenges.get(challenge_id)
        if not isinstance(challenge, dict) or str(challenge.get("purpose") or "") not in {"initialize_account_pending", "initialize_account"}:
            raise PermissionError("initialization challenge expired or not found")
        username = str(challenge.get("username") or "")
        if is_admin_username(username):
            raise PermissionError("客户端 admin 账号由服务端统一管理，不能在本地客户端初始化。")
        self.ensure_local_email_available(email, except_username=username)
        code = self.email.make_code()
        delivery = self.email.deliver_code(email=email, code=code, username=username, purpose="initialize_account")
        challenge.update(
            {
                "purpose": "initialize_account",
                "email": email,
                "new_password_hash": hash_password(new_password),
                "code_hash": hash_password(code),
                "attempts_remaining": self.email.settings.max_attempts,
                "last_sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        challenges[challenge_id] = challenge
        self.write_local_challenges(challenges)
        response: dict[str, Any] = {
            "requires_verification": True,
            "challenge_id": challenge_id,
            "masked_email": mask_email(email),
            "expires_at": str(challenge.get("expires_at") or ""),
            "trusted_device_days": self.email.settings.trusted_device_days,
            "delivery": {key: value for key, value in delivery.items() if key != "debug_code"},
        }
        if delivery.get("debug_code"):
            response["debug_code"] = delivery["debug_code"]
        return response

    def local_verify_account_initialization(self, *, challenge_id: str, code: str) -> dict[str, Any]:
        challenge = self.consume_local_code_challenge(challenge_id=challenge_id, code=code, allowed_purposes={"initialize_account"})
        username = str(challenge.get("username") or "")
        if is_admin_username(username):
            raise PermissionError("客户端 admin 账号由服务端统一管理，不能在本地客户端初始化。")
        email = normalize_email(str(challenge.get("email") or ""))
        new_hash = str(challenge.get("new_password_hash") or "")
        if not username or not email or not new_hash:
            raise PermissionError("initialization challenge is invalid")
        overrides = self.read_local_account_overrides()
        existing = overrides.get(username, {})
        overrides[username] = {
            **existing,
            "username": username,
            "email": email,
            "password_hash": new_hash,
            "initialized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.write_local_account_overrides(overrides)
        user_id = str(challenge.get("user_id") or username)
        return {
            "initialized": True,
            "username": username,
            "role": str(challenge.get("role") or "customer"),
            "masked_email": mask_email(email),
            "revoked_sessions": self.revoke_user_sessions(user_id),
        }

    def local_start_login(
        self,
        username: str,
        password: str,
        *,
        tenant_id: str | None = None,
        device_id: str = "",
        device_name: str = "",
    ) -> dict[str, Any]:
        account = local_account(username)
        if not local_password_matches(account, password):
            raise PermissionError("invalid local credentials")
        if is_admin_username(username):
            raise PermissionError("客户端 admin 使用服务端统一账号登录；本地客户端只允许 customer 和 guest 走首次登录。")
        if local_account_needs_initialization(account):
            return self.local_create_initialization_challenge(
                account=account,
                username=username,
                tenant_id=tenant_id,
                device_id=device_id,
                device_name=device_name,
            )
        role = role_from_value(account.get("role"))
        if not self.email.settings.otp_required and role != Role.ADMIN:
            session = self.local_session_from_account(account, username=username, tenant_id=tenant_id)
            self.save_session(session)
            return {"requires_verification": False, "session": session.to_dict()}
        fingerprint = local_device_fingerprint(str(account.get("user_id") or username), device_id)
        if fingerprint and self.local_trusted_device_valid(user_id=str(account.get("user_id") or username), fingerprint=fingerprint):
            session = self.local_session_from_account(account, username=username, tenant_id=tenant_id)
            self.touch_local_trusted_device(fingerprint=fingerprint)
            self.save_session(session)
            return {"requires_verification": False, "trusted_device": True, "session": session.to_dict()}
        email = normalize_email(str(account.get("email") or ""))
        if not email:
            challenge_id = "local_otp_" + secrets.token_urlsafe(24)
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=self.email.settings.ttl_minutes)).isoformat(timespec="seconds")
            challenges = self.read_local_challenges()
            challenges[challenge_id] = {
                "challenge_id": challenge_id,
                "purpose": "bind_email_login",
                "username": username,
                "user_id": str(account.get("user_id") or username),
                "tenant_id": active_tenant_id(tenant_id or account.get("active_tenant_id") or DEFAULT_TENANT_ID),
                "device_fingerprint": fingerprint,
                "device_name": device_name,
                "attempts_remaining": self.email.settings.max_attempts,
                "expires_at": expires_at,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self.write_local_challenges(challenges)
            return {
                "requires_email_binding": True,
                "challenge_id": challenge_id,
                "expires_at": expires_at,
                "message": "账号尚未绑定邮箱，请填写邮箱并完成验证码验证。",
            }
        code = self.email.make_code()
        delivery = self.email.deliver_code(email=email, code=code, username=username, purpose="login")
        challenge_id = "local_otp_" + secrets.token_urlsafe(24)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=self.email.settings.ttl_minutes)).isoformat(timespec="seconds")
        challenges = self.read_local_challenges()
        challenges[challenge_id] = {
            "challenge_id": challenge_id,
            "purpose": "login",
            "username": username,
            "user_id": str(account.get("user_id") or username),
            "tenant_id": active_tenant_id(tenant_id or account.get("active_tenant_id") or DEFAULT_TENANT_ID),
            "email": email,
            "device_fingerprint": fingerprint,
            "device_name": device_name,
            "code_hash": hash_password(code),
            "attempts_remaining": self.email.settings.max_attempts,
            "expires_at": expires_at,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.write_local_challenges(challenges)
        response: dict[str, Any] = {
            "requires_verification": True,
            "challenge_id": challenge_id,
            "masked_email": mask_email(email),
            "expires_at": expires_at,
            "trusted_device_days": self.email.settings.trusted_device_days,
            "delivery": {key: value for key, value in delivery.items() if key != "debug_code"},
        }
        if delivery.get("debug_code"):
            response["debug_code"] = delivery["debug_code"]
        return response

    def local_start_login_email_binding(self, *, challenge_id: str, email: str) -> dict[str, Any]:
        email = normalize_email(email)
        if not email:
            raise PermissionError("valid email required")
        challenges = self.read_local_challenges()
        challenge_id = str(challenge_id or "").strip()
        challenge = challenges.get(challenge_id)
        if not isinstance(challenge, dict) or challenge.get("purpose") != "bind_email_login":
            raise PermissionError("email binding challenge expired or not found")
        self.ensure_local_email_available(email, except_username=str(challenge.get("username") or ""))
        code = self.email.make_code()
        delivery = self.email.deliver_code(email=email, code=code, username=str(challenge.get("username") or ""), purpose="bind_email_login")
        challenge["email"] = email
        challenge["code_hash"] = hash_password(code)
        challenge["attempts_remaining"] = self.email.settings.max_attempts
        challenge["last_sent_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        challenges[challenge_id] = challenge
        self.write_local_challenges(challenges)
        response: dict[str, Any] = {
            "requires_verification": True,
            "challenge_id": challenge_id,
            "masked_email": mask_email(email),
            "expires_at": str(challenge.get("expires_at") or ""),
            "trusted_device_days": self.email.settings.trusted_device_days,
            "delivery": {key: value for key, value in delivery.items() if key != "debug_code"},
        }
        if delivery.get("debug_code"):
            response["debug_code"] = delivery["debug_code"]
        return response

    def security_profile(self, session: AuthSession) -> dict[str, Any]:
        if session.source == "vps" and self.settings.vps_base_url:
            return self.vps.security_profile(token=session.token).get("security", {})
        username = session.user.username or session.user.user_id
        account = local_account(username)
        email = normalize_email(str(account.get("email") or ""))
        return {
            "user_id": session.user.user_id,
            "username": username,
            "role": session.user.role.value,
            "email": email,
            "masked_email": mask_email(email),
            "otp_required": self.email.settings.otp_required,
            "trusted_device_days": self.email.settings.trusted_device_days,
            "trusted_devices": [
                public_local_trusted_device(record)
                for record in self.read_local_trusted_devices().values()
                if isinstance(record, dict) and str(record.get("user_id") or "") == session.user.user_id and local_trusted_device_active(record)
            ],
        }

    def start_email_binding(self, session: AuthSession, *, email: str) -> dict[str, Any]:
        if session.source == "vps" and self.settings.vps_base_url:
            return self.vps.start_email_binding(token=session.token, email=email)
        email = normalize_email(email)
        if not email:
            raise PermissionError("valid email required")
        username = session.user.username or session.user.user_id
        self.ensure_local_email_available(email, except_username=username)
        code = self.email.make_code()
        delivery = self.email.deliver_code(email=email, code=code, username=username, purpose="bind_email")
        challenge_id = "local_otp_" + secrets.token_urlsafe(24)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=self.email.settings.ttl_minutes)).isoformat(timespec="seconds")
        challenges = self.read_local_challenges()
        challenges[challenge_id] = {
            "challenge_id": challenge_id,
            "purpose": "bind_email",
            "username": username,
            "user_id": session.user.user_id,
            "email": email,
            "code_hash": hash_password(code),
            "attempts_remaining": self.email.settings.max_attempts,
            "expires_at": expires_at,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.write_local_challenges(challenges)
        response: dict[str, Any] = {
            "requires_verification": True,
            "challenge_id": challenge_id,
            "masked_email": mask_email(email),
            "expires_at": expires_at,
            "delivery": {key: value for key, value in delivery.items() if key != "debug_code"},
        }
        if delivery.get("debug_code"):
            response["debug_code"] = delivery["debug_code"]
        return response

    def verify_email_binding(self, session: AuthSession, *, challenge_id: str, code: str) -> dict[str, Any]:
        if session.source == "vps" and self.settings.vps_base_url and str(challenge_id or "").startswith("otp_"):
            return self.vps.verify_email_binding(token=session.token, challenge_id=challenge_id, code=code)
        challenge = self.consume_local_code_challenge(challenge_id=challenge_id, code=code, allowed_purposes={"bind_email"})
        if str(challenge.get("user_id") or "") != session.user.user_id:
            raise PermissionError("verification challenge does not belong to current account")
        email = normalize_email(str(challenge.get("email") or ""))
        self.apply_local_email(username=session.user.username or session.user.user_id, email=email)
        return {"changed": True, "email": email, "masked_email": mask_email(email)}

    def start_password_change(self, session: AuthSession, *, current_password: str, new_password: str) -> dict[str, Any]:
        if session.source == "vps" and self.settings.vps_base_url:
            return self.vps.start_password_change(token=session.token, current_password=current_password, new_password=new_password)
        try:
            validate_password_strength(new_password)
        except ValueError as exc:
            raise PermissionError(str(exc)) from exc
        username = session.user.username or session.user.user_id
        account = local_account(username)
        if not local_password_matches(account, current_password):
            raise PermissionError("invalid current password")
        email = normalize_email(str(account.get("email") or ""))
        if not email:
            raise PermissionError("account email is not configured")
        code = self.email.make_code()
        delivery = self.email.deliver_code(email=email, code=code, username=username, purpose="change_password")
        challenge_id = "local_otp_" + secrets.token_urlsafe(24)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=self.email.settings.ttl_minutes)).isoformat(timespec="seconds")
        challenges = self.read_local_challenges()
        challenges[challenge_id] = {
            "challenge_id": challenge_id,
            "purpose": "change_password",
            "username": username,
            "user_id": session.user.user_id,
            "email": email,
            "new_password_hash": hash_password(new_password),
            "code_hash": hash_password(code),
            "attempts_remaining": self.email.settings.max_attempts,
            "expires_at": expires_at,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.write_local_challenges(challenges)
        response: dict[str, Any] = {
            "requires_verification": True,
            "challenge_id": challenge_id,
            "masked_email": mask_email(email),
            "expires_at": expires_at,
            "delivery": {key: value for key, value in delivery.items() if key != "debug_code"},
        }
        if delivery.get("debug_code"):
            response["debug_code"] = delivery["debug_code"]
        return response

    def verify_password_change(self, session: AuthSession, *, challenge_id: str, code: str) -> dict[str, Any]:
        if session.source == "vps" and self.settings.vps_base_url and str(challenge_id or "").startswith("otp_"):
            return self.vps.verify_password_change(token=session.token, challenge_id=challenge_id, code=code)
        challenge = self.consume_local_code_challenge(challenge_id=challenge_id, code=code, allowed_purposes={"change_password"})
        if str(challenge.get("user_id") or "") != session.user.user_id:
            raise PermissionError("verification challenge does not belong to current account")
        new_hash = str(challenge.get("new_password_hash") or "")
        if not new_hash:
            raise PermissionError("password change challenge is invalid")
        return self.apply_local_password_hash(session, new_hash)

    def local_verify_login(self, *, challenge_id: str, code: str, trust_device: bool = False) -> AuthSession:
        challenge_id = str(challenge_id or "").strip()
        code = str(code or "").strip()
        challenge = self.consume_local_code_challenge(challenge_id=challenge_id, code=code, allowed_purposes={"login", "bind_email_login"})
        username = str(challenge.get("username") or "")
        if str(challenge.get("purpose") or "") == "bind_email_login":
            self.apply_local_email(username=username, email=str(challenge.get("email") or ""))
        account = local_account(username)
        if trust_device and challenge.get("device_fingerprint"):
            self.add_local_trusted_device(
                user_id=str(account.get("user_id") or username),
                fingerprint=str(challenge.get("device_fingerprint") or ""),
                device_name=str(challenge.get("device_name") or ""),
            )
        return self.local_session_from_account(account, username=username, tenant_id=str(challenge.get("tenant_id") or DEFAULT_TENANT_ID))

    def change_password(self, session: AuthSession, *, current_password: str, new_password: str) -> dict[str, Any]:
        if session.source == "vps" and self.settings.vps_base_url:
            return self.vps.change_password(token=session.token, current_password=current_password, new_password=new_password)
        if self.email.settings.otp_required:
            raise PermissionError("email verification required for password changes")
        try:
            validate_password_strength(new_password)
        except ValueError as exc:
            raise PermissionError(str(exc)) from exc
        account = local_account(session.user.username or session.user.user_id)
        if not local_password_matches(account, current_password):
            raise PermissionError("invalid current password")
        return self.apply_local_password_hash(session, hash_password(new_password))

    def implicit_admin_context(self, *, tenant_id: str | None = None) -> AuthContext:
        tenant = active_tenant_id(tenant_id)
        user = AuthUser(user_id="local-admin", role=Role.ADMIN, tenant_ids=("*",), display_name="Local Admin", username="admin")
        session = AuthSession(session_id="implicit-local-admin", token="", user=user, active_tenant_id=tenant, source="implicit")
        return AuthContext(session=session, tenant_id=tenant, strict=False, authenticated=False)

    def resolve_context(
        self,
        *,
        authorization: str = "",
        tenant_id: str | None = None,
        dev_role: str = "",
        dev_user_id: str = "",
    ) -> AuthContext | None:
        requested_tenant = active_tenant_id(tenant_id)
        token = bearer_token(authorization)
        if token:
            session = self.get_session(token)
            if session and not session.expired():
                if session.user.role == Role.ADMIN and session.source != "vps":
                    return None
                tenant = active_tenant_id(tenant_id or session.active_tenant_id)
                return AuthContext(session=session, tenant_id=tenant, strict=self.settings.required, authenticated=True)
        if not self.settings.required:
            if dev_role:
                role = role_from_value(dev_role)
                tenant_ids = ("*",) if role == Role.ADMIN else (requested_tenant,)
                user = AuthUser(
                    user_id=dev_user_id or f"dev-{role.value}",
                    role=role,
                    tenant_ids=tenant_ids,
                    display_name=dev_user_id or role.value,
                    username=dev_user_id or role.value,
                )
                session = AuthSession(session_id="dev-header", user=user, active_tenant_id=requested_tenant, source="dev-header")
                return AuthContext(session=session, tenant_id=requested_tenant, strict=False, authenticated=False)
            return self.implicit_admin_context(tenant_id=requested_tenant)
        return None

    def save_session(self, session: AuthSession) -> None:
        records = self.read_sessions()
        records = [item for item in records if item.get("session_id") != session.session_id and item.get("token") != session.token]
        records.append(session.to_dict())
        self.settings.session_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.settings.session_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(records[-200:], ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.settings.session_path)

    def get_session(self, token: str) -> AuthSession | None:
        for item in self.read_sessions():
            if token not in {str(item.get("session_id") or ""), str(item.get("token") or "")}:
                continue
            try:
                return session_from_payload(item)
            except Exception:
                return None
        return None

    def revoke(self, token: str) -> bool:
        records = self.read_sessions()
        remaining = [item for item in records if token not in {str(item.get("session_id") or ""), str(item.get("token") or "")}]
        if len(remaining) == len(records):
            return False
        self.settings.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.session_path.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")
        return True

    def revoke_user_sessions(self, user_id: str, *, keep_token: str = "") -> int:
        records = self.read_sessions()
        kept = []
        revoked = 0
        for item in records:
            user_payload = item.get("user") if isinstance(item.get("user"), dict) else item
            token = str(item.get("token") or item.get("session_id") or "")
            if str(user_payload.get("user_id") or "") == user_id and token != keep_token:
                revoked += 1
                continue
            kept.append(item)
        self.settings.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.session_path.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        return revoked

    def read_sessions(self) -> list[dict[str, Any]]:
        if not self.settings.session_path.exists():
            return []
        try:
            payload = json.loads(self.settings.session_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        return payload if isinstance(payload, list) else []

    def read_local_account_overrides(self) -> dict[str, Any]:
        if not self.settings.account_state_path.exists():
            return {}
        try:
            payload = json.loads(self.settings.account_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        accounts = payload.get("accounts") if isinstance(payload, dict) else payload
        if isinstance(accounts, dict):
            return accounts
        if isinstance(accounts, list):
            return {str(item.get("username") or item.get("user_id") or ""): item for item in accounts if isinstance(item, dict)}
        return {}

    def write_local_account_overrides(self, accounts: dict[str, Any]) -> None:
        self.settings.account_state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.settings.account_state_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps({"accounts": accounts}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.settings.account_state_path)

    def read_local_challenges(self) -> dict[str, Any]:
        if not self.settings.challenge_path.exists():
            return {}
        try:
            payload = json.loads(self.settings.challenge_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {key: value for key, value in payload.items() if isinstance(value, dict) and not challenge_expired(value)}

    def write_local_challenges(self, challenges: dict[str, Any]) -> None:
        self.settings.challenge_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.settings.challenge_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(challenges, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.settings.challenge_path)

    def consume_local_code_challenge(self, *, challenge_id: str, code: str, allowed_purposes: set[str]) -> dict[str, Any]:
        challenge_id = str(challenge_id or "").strip()
        code = str(code or "").strip()
        challenges = self.read_local_challenges()
        challenge = challenges.get(challenge_id)
        if not isinstance(challenge, dict) or str(challenge.get("purpose") or "") not in allowed_purposes:
            raise PermissionError("verification code expired or not found")
        if challenge_expired(challenge):
            challenges.pop(challenge_id, None)
            self.write_local_challenges(challenges)
            raise PermissionError("verification code expired or not found")
        if int(challenge.get("attempts_remaining") or 0) <= 0:
            challenges.pop(challenge_id, None)
            self.write_local_challenges(challenges)
            raise PermissionError("verification attempts exceeded")
        if not verify_password(code, str(challenge.get("code_hash") or "")):
            challenge["attempts_remaining"] = int(challenge.get("attempts_remaining") or 0) - 1
            self.write_local_challenges(challenges)
            raise PermissionError("invalid verification code")
        challenges.pop(challenge_id, None)
        self.write_local_challenges(challenges)
        return dict(challenge)

    def ensure_local_email_available(self, email: str, *, except_username: str = "") -> None:
        normalized = normalize_email(email)
        if not normalized:
            raise PermissionError("valid email required")

    def apply_local_email(self, *, username: str, email: str) -> None:
        email = normalize_email(email)
        if not email:
            raise PermissionError("valid email required")
        overrides = self.read_local_account_overrides()
        existing = overrides.get(username, {})
        overrides[username] = {
            **existing,
            "username": username,
            "email": email,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.write_local_account_overrides(overrides)

    def apply_local_password_hash(self, session: AuthSession, new_password_hash: str) -> dict[str, Any]:
        overrides = self.read_local_account_overrides()
        username = session.user.username or session.user.user_id
        existing = overrides.get(username, {})
        overrides[username] = {
            **existing,
            "username": username,
            "password_hash": new_password_hash,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.write_local_account_overrides(overrides)
        return {
            "changed": True,
            "user_id": session.user.user_id,
            "revoked_sessions": self.revoke_user_sessions(session.user.user_id, keep_token=session.token),
        }

    def read_local_trusted_devices(self) -> dict[str, Any]:
        if not self.settings.trusted_device_path.exists():
            return {}
        try:
            payload = json.loads(self.settings.trusted_device_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def write_local_trusted_devices(self, records: dict[str, Any]) -> None:
        self.settings.trusted_device_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.settings.trusted_device_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.settings.trusted_device_path)

    def local_trusted_device_valid(self, *, user_id: str, fingerprint: str) -> bool:
        record = self.read_local_trusted_devices().get(fingerprint)
        return bool(
            isinstance(record, dict)
            and str(record.get("user_id") or "") == user_id
            and local_trusted_device_active(record)
        )

    def add_local_trusted_device(self, *, user_id: str, fingerprint: str, device_name: str = "") -> None:
        records = self.read_local_trusted_devices()
        now = datetime.now(timezone.utc)
        records[fingerprint] = {
            "fingerprint": fingerprint,
            "user_id": user_id,
            "device_name": str(device_name or "当前设备")[:120],
            "trusted_until": (now + timedelta(days=self.email.settings.trusted_device_days)).isoformat(timespec="seconds"),
            "created_at": now.isoformat(timespec="seconds"),
            "last_seen_at": now.isoformat(timespec="seconds"),
        }
        self.write_local_trusted_devices(records)

    def touch_local_trusted_device(self, *, fingerprint: str) -> None:
        records = self.read_local_trusted_devices()
        record = records.get(fingerprint)
        if isinstance(record, dict):
            record["last_seen_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.write_local_trusted_devices(records)


def local_account(username: str) -> dict[str, Any]:
    username = str(username or "").strip() or "admin"
    overrides = read_local_account_overrides_from_env()
    accounts_path = os.getenv("WECHAT_LOCAL_ACCOUNTS_JSON", "").strip()
    if accounts_path:
        payload = json.loads(Path(accounts_path).read_text(encoding="utf-8"))
        for account in payload.get("accounts", []) if isinstance(payload, dict) else []:
            if str(account.get("username") or account.get("user_id") or "") == username:
                return {**account, **overrides.get(username, {})}
    defaults = {
        "admin": {
            "user_id": "admin",
            "role": "admin",
            "password": os.getenv("WECHAT_LOCAL_ADMIN_PASSWORD") or os.getenv("WECHAT_VPS_ADMIN_PASSWORD") or "1234.abcd",
            "email": normalize_email(os.getenv("WECHAT_LOCAL_ADMIN_EMAIL") or os.getenv("WECHAT_VPS_ADMIN_EMAIL") or os.getenv("WECHAT_ADMIN_EMAIL") or "admin@example.local"),
            "tenant_ids": ["*"],
            "active_tenant_id": DEFAULT_TENANT_ID,
        },
        "customer": {
            "user_id": "customer",
            "role": "customer",
            "password": os.getenv("WECHAT_LOCAL_CUSTOMER_PASSWORD") or "customer-local-dev",
            "email": normalize_email(os.getenv("WECHAT_LOCAL_CUSTOMER_EMAIL") or "customer@example.local"),
            "tenant_ids": [DEFAULT_TENANT_ID],
            "active_tenant_id": DEFAULT_TENANT_ID,
        },
        "test01": {
            "user_id": "customer_test01",
            "role": "customer",
            "password": os.getenv("WECHAT_LOCAL_TEST01_PASSWORD") or "1234.abcd",
            "email": normalize_email(os.getenv("WECHAT_LOCAL_TEST01_EMAIL") or "test01@example.local"),
            "tenant_ids": [DEFAULT_TENANT_ID],
            "active_tenant_id": DEFAULT_TENANT_ID,
        },
        "guest": {
            "user_id": "guest",
            "role": "guest",
            "password": os.getenv("WECHAT_LOCAL_GUEST_PASSWORD") or "guest-local-dev",
            "email": normalize_email(os.getenv("WECHAT_LOCAL_GUEST_EMAIL") or "guest@example.local"),
            "tenant_ids": [DEFAULT_TENANT_ID],
            "active_tenant_id": DEFAULT_TENANT_ID,
        },
    }
    if username in overrides:
        base = defaults.get(username, {})
        return {**base, **overrides[username]}
    if username not in defaults:
        raise PermissionError("unknown local account")
    return defaults[username]


def local_password_matches(account: dict[str, Any], password: str) -> bool:
    if account.get("password_hash"):
        return verify_password(str(password or ""), str(account.get("password_hash") or ""))
    expected = account.get("password") or ""
    return not expected or str(password or "") == expected


def local_account_needs_initialization(account: dict[str, Any]) -> bool:
    return not (
        account.get("initialized_at")
        and account.get("password_hash")
        and normalize_email(str(account.get("email") or ""))
    )


def read_local_account_overrides_from_env() -> dict[str, Any]:
    path = Path(os.getenv("WECHAT_LOCAL_ACCOUNTS_STATE_PATH") or runtime_app_root() / "auth" / "local_accounts.json")
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    accounts = payload.get("accounts") if isinstance(payload, dict) else payload
    if isinstance(accounts, dict):
        return accounts
    if isinstance(accounts, list):
        return {str(item.get("username") or item.get("user_id") or ""): item for item in accounts if isinstance(item, dict)}
    return {}


def write_local_account_overrides_to_env(accounts: dict[str, Any]) -> None:
    path = Path(os.getenv("WECHAT_LOCAL_ACCOUNTS_STATE_PATH") or runtime_app_root() / "auth" / "local_accounts.json")
    payload = accounts if isinstance(accounts, dict) else {}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps({"accounts": payload}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def challenge_expired(challenge: dict[str, Any]) -> bool:
    expires_at = str(challenge.get("expires_at") or "")
    try:
        value = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= datetime.now(timezone.utc)


def remove_prior_local_challenges(challenges: dict[str, Any], *, user_id: str, purposes: set[str]) -> None:
    for challenge_id, challenge in list(challenges.items()):
        if not isinstance(challenge, dict):
            continue
        if str(challenge.get("purpose") or "") in purposes and str(challenge.get("user_id") or "") == user_id:
            challenges.pop(challenge_id, None)


def local_device_fingerprint(user_id: str, device_id: str) -> str:
    stable_device_id = str(device_id or "").strip()
    if not stable_device_id:
        return ""
    return hashlib.sha256(f"{user_id}:{stable_device_id}".encode("utf-8")).hexdigest()


def public_local_trusted_device(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "fingerprint": str(record.get("fingerprint") or ""),
        "device_name": str(record.get("device_name") or "当前设备"),
        "trusted_until": str(record.get("trusted_until") or ""),
        "last_seen_at": str(record.get("last_seen_at") or ""),
    }


def local_trusted_device_active(record: dict[str, Any]) -> bool:
    expires_at = str(record.get("trusted_until") or "")
    try:
        value = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value > datetime.now(timezone.utc)


def bearer_token(value: str) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("bearer "):
        return text[7:].strip()
    return text


def parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def cloud_auth_required_default() -> bool:
    return parse_bool(os.getenv("WECHAT_CLOUD_REQUIRED"), default=True)


def is_admin_username(username: str | None) -> bool:
    return str(username or "").strip().lower() == "admin"
