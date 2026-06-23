"""Authentication and authorization helpers for the VPS admin control plane."""

from __future__ import annotations

import hmac
import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Header, HTTPException, Request

from apps.wechat_ai_customer_service.auth.email_verification import EmailVerificationService, email_settings_from_config, mask_email, normalize_email
from apps.wechat_ai_customer_service.auth.models import AuthSession, AuthUser, Role, role_from_value, session_from_payload
from apps.wechat_ai_customer_service.auth.passwords import hash_password, validate_password_strength, verify_password
from apps.wechat_ai_customer_service.auth.session import bearer_token
from apps.wechat_ai_customer_service.knowledge_paths import DEFAULT_TENANT_ID, active_tenant_id

from .store import VpsAdminStore, append_audit, now_iso


@dataclass(frozen=True)
class VpsAdminSettings:
    admin_username: str
    admin_password: str
    admin_email: str
    admin_user_id: str
    session_hours: int
    node_enrollment_token: str


def load_settings() -> VpsAdminSettings:
    return VpsAdminSettings(
        admin_username=(os.getenv("WECHAT_VPS_ADMIN_USERNAME") or "admin").strip() or "admin",
        admin_password=os.getenv("WECHAT_VPS_ADMIN_PASSWORD") or "1234.abcd",
        admin_email=normalize_email(os.getenv("WECHAT_VPS_ADMIN_EMAIL") or os.getenv("WECHAT_ADMIN_EMAIL") or "admin@example.local"),
        admin_user_id=(os.getenv("WECHAT_VPS_ADMIN_USER_ID") or "platform-admin").strip() or "platform-admin",
        session_hours=max(1, int(os.getenv("WECHAT_VPS_SESSION_HOURS") or "12")),
        node_enrollment_token=os.getenv("WECHAT_VPS_NODE_ENROLLMENT_TOKEN", "").strip(),
    )


class VpsAdminAuthService:
    def __init__(self, store: VpsAdminStore, settings: VpsAdminSettings | None = None) -> None:
        self.store = store
        self.settings = settings or load_settings()

    def email_service(self, state: dict[str, Any] | None = None) -> EmailVerificationService:
        payload = state or self.store.read()
        return EmailVerificationService(email_settings_from_config(payload.get("smtp_config")))

    @property
    def admin_user(self) -> AuthUser:
        display_name = "Platform Admin"
        state = self.store.read()
        credentials = self.admin_credentials(state)
        if credentials.get("display_name"):
            display_name = str(credentials.get("display_name"))
        return AuthUser(
            user_id=self.settings.admin_user_id,
            role=Role.ADMIN,
            tenant_ids=("*",),
            display_name=display_name,
            username=self.settings.admin_username,
            resource_scopes=("*",),
        )

    def login(self, username: str, password: str, *, tenant_id: str | None = None) -> AuthSession:
        user, requested_tenant, actor_id, email = self.authenticate(username=username, password=password, tenant_id=tenant_id)
        state = self.store.read()
        init_status = self.initialization_status(state, user=user, actor_id=actor_id, email=email)
        if init_status["required"]:
            raise PermissionError("account initialization required")
        if self.email_service(state).settings.otp_required or user.role == Role.ADMIN:
            raise PermissionError("email verification required")
        return self.create_session(user, requested_tenant, actor_id=actor_id)

    def authenticate(self, username: str, password: str, *, tenant_id: str | None = None) -> tuple[AuthUser, str, str, str]:
        normalized_username = str(username or "").strip()
        state = self.store.read()
        if normalized_username == self.settings.admin_username:
            if not self.verify_admin_password(state, str(password or "")):
                raise PermissionError("invalid credentials")
            return (
                self.admin_user,
                active_tenant_id(tenant_id or DEFAULT_TENANT_ID),
                self.settings.admin_user_id,
                self.admin_email(state),
            )

        user_record = find_user_by_username(state, normalized_username)
        if not user_record or str(user_record.get("status") or "active") != "active":
            raise PermissionError("invalid credentials")
        if not verify_password(str(password or ""), str(user_record.get("password_hash") or "")):
            raise PermissionError("invalid credentials")
        user = auth_user_from_record(user_record)
        requested_tenant = active_tenant_id(tenant_id or (user.tenant_ids[0] if user.tenant_ids else DEFAULT_TENANT_ID))
        if not user.has_tenant(requested_tenant):
            raise PermissionError("tenant not allowed")
        return user, requested_tenant, user.user_id, normalize_email(str(user_record.get("email") or ""))

    def start_login(
        self,
        username: str,
        password: str,
        *,
        tenant_id: str | None = None,
        device_id: str = "",
        device_name: str = "",
        ip_address: str = "",
    ) -> dict[str, Any]:
        user, requested_tenant, actor_id, email = self.authenticate(username=username, password=password, tenant_id=tenant_id)
        state = self.store.read()
        email_service = self.email_service(state)
        init_status = self.initialization_status(state, user=user, actor_id=actor_id, email=email)
        if init_status["required"]:
            return self.create_initialization_challenge(
                state=state,
                user=user,
                active_tenant_id=requested_tenant,
                actor_id=actor_id,
                device_id=device_id,
                device_name=device_name,
                ip_address=ip_address,
                init_status=init_status,
            )
        if not email_service.settings.otp_required and user.role != Role.ADMIN:
            session = self.create_session(user, requested_tenant, actor_id=actor_id)
            return {"requires_verification": False, "session": session.to_dict()}

        fingerprint = device_fingerprint(actor_id, device_id)
        if fingerprint and trusted_device_valid(state, user_id=actor_id, fingerprint=fingerprint):
            self.touch_trusted_device(fingerprint=fingerprint)
            session = self.create_session(user, requested_tenant, actor_id=actor_id)
            return {"requires_verification": False, "trusted_device": True, "session": session.to_dict()}

        challenge_id = "otp_" + secrets.token_urlsafe(24)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=email_service.settings.ttl_minutes)).isoformat(timespec="seconds")
        base_challenge = {
            "challenge_id": challenge_id,
            "username": user.username,
            "user_id": user.user_id,
            "user": user.to_dict(),
            "active_tenant_id": requested_tenant,
            "actor_id": actor_id,
            "device_fingerprint": fingerprint,
            "device_name": device_name,
            "ip_address": ip_address,
            "attempts_remaining": email_service.settings.max_attempts,
            "expires_at": expires_at,
            "created_at": now_iso(),
        }

        if not email:
            if user.role == Role.ADMIN:
                raise PermissionError("admin email is not configured")
            challenge = {**base_challenge, "purpose": "bind_email_login", "email_binding_pending": True}

            def mutate_pending(state: dict[str, Any]) -> None:
                prune_expired_challenges(state)
                state["auth_challenges"][challenge_id] = challenge
                append_audit(
                    state,
                    actor_id=actor_id,
                    action="start_email_binding_login",
                    target_type="auth_challenge",
                    target_id=challenge_id,
                    detail={"username": user.username},
                )

            self.store.update(mutate_pending)
            return {
                "requires_email_binding": True,
                "challenge_id": challenge_id,
                "expires_at": expires_at,
                "message": "账号尚未绑定邮箱，请填写邮箱并完成验证码验证。",
            }

        code = email_service.make_code()
        challenge = {
            **base_challenge,
            "purpose": "login",
            "email": email,
            "code_hash": hash_password(code),
            "last_sent_at": now_iso(),
        }

        # Reserve the challenge slot first so concurrent duplicate submissions
        # cannot both pass resend-window checks and send two emails.
        def reserve_challenge(next_state: dict[str, Any]) -> None:
            prune_expired_challenges(next_state)
            enforce_resend_window(
                next_state,
                user_id=actor_id,
                purposes={"login", "bind_email_login"},
                resend_seconds=email_service.settings.resend_seconds,
            )
            remove_prior_challenges(next_state, user_id=actor_id, purposes={"login", "bind_email_login"})
            next_state["auth_challenges"][challenge_id] = challenge

        self.store.update(reserve_challenge)

        try:
            delivery = email_service.deliver_code(email=email, code=code, username=user.username or user.user_id, purpose="login")
        except Exception:
            # Roll back reserved challenge when delivery fails so user can retry.
            def rollback_challenge(next_state: dict[str, Any]) -> None:
                challenges = next_state.setdefault("auth_challenges", {})
                existing = challenges.get(challenge_id)
                if isinstance(existing, dict) and str(existing.get("actor_id") or "") == actor_id:
                    challenges.pop(challenge_id, None)

            self.store.update(rollback_challenge)
            raise

        challenge = {
            **base_challenge,
            "purpose": "login",
            "email": email,
            "code_hash": hash_password(code),
            "last_sent_at": now_iso(),
        }

        def mutate(state: dict[str, Any]) -> None:
            current = state.setdefault("auth_challenges", {}).get(challenge_id)
            if isinstance(current, dict):
                current["last_sent_at"] = challenge.get("last_sent_at")
            else:
                state["auth_challenges"][challenge_id] = challenge
            append_audit(
                state,
                actor_id=actor_id,
                action="start_email_login",
                target_type="auth_challenge",
                target_id=challenge_id,
                detail={"username": user.username, "email": mask_email(email)},
            )

        self.store.update(mutate)
        return verification_response(
            challenge_id=challenge_id,
            email=email,
            expires_at=expires_at,
            delivery=delivery,
            trusted_device_days=email_service.settings.trusted_device_days,
        )

    def start_login_email_binding(self, *, challenge_id: str, email: str) -> dict[str, Any]:
        email = normalize_email(email)
        if not email:
            raise PermissionError("valid email required")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            prune_expired_challenges(state)
            challenge = state.get("auth_challenges", {}).get(challenge_id)
            if not isinstance(challenge, dict) or challenge.get("purpose") != "bind_email_login":
                raise PermissionError("email binding challenge expired or not found")
            ensure_email_not_used(state, email, except_user_id=str(challenge.get("user_id") or ""))
            email_service = self.email_service(state)
            enforce_resend_window(state, user_id=str(challenge.get("user_id") or ""), purposes={"bind_email_login"}, resend_seconds=email_service.settings.resend_seconds)
            code = email_service.make_code()
            delivery = email_service.deliver_code(
                email=email,
                code=code,
                username=str(challenge.get("username") or ""),
                purpose="bind_email_login",
            )
            challenge.update(
                {
                    "email": email,
                    "email_binding_pending": False,
                    "code_hash": hash_password(code),
                    "last_sent_at": now_iso(),
                    "attempts_remaining": email_service.settings.max_attempts,
                }
            )
            append_audit(
                state,
                actor_id=str(challenge.get("actor_id") or ""),
                action="send_email_binding_login",
                target_type="auth_challenge",
                target_id=challenge_id,
                detail={"email": mask_email(email)},
            )
            return {
                **verification_response(
                    challenge_id=challenge_id,
                    email=email,
                    expires_at=str(challenge.get("expires_at") or ""),
                    delivery=delivery,
                    trusted_device_days=email_service.settings.trusted_device_days,
                )
            }

        return self.store.update(mutate)

    def verify_login(self, challenge_id: str, code: str, *, trust_device: bool = False) -> AuthSession:
        challenge = self.consume_code_challenge(challenge_id=challenge_id, code=code, allowed_purposes={"login", "bind_email_login"})
        user_payload = challenge.get("user") if isinstance(challenge.get("user"), dict) else {}
        user = auth_user_from_payload_allow_admin(user_payload)
        if str(challenge.get("purpose") or "") == "bind_email_login":
            self.set_user_email(user_id=user.user_id, email=str(challenge.get("email") or ""), actor_id=user.user_id)
        fingerprint = str(challenge.get("device_fingerprint") or "")
        if trust_device and fingerprint:
            self.add_trusted_device(
                user_id=user.user_id,
                fingerprint=fingerprint,
                device_name=str(challenge.get("device_name") or ""),
                ip_address=str(challenge.get("ip_address") or ""),
            )
        return self.create_session(
            user,
            active_tenant_id(challenge.get("active_tenant_id") or DEFAULT_TENANT_ID),
            actor_id=str(challenge.get("actor_id") or user.user_id),
        )

    def start_password_change(self, session: AuthSession, *, current_password: str, new_password: str) -> dict[str, Any]:
        try:
            validate_password_strength(new_password)
        except ValueError as exc:
            raise PermissionError(str(exc)) from exc
        state = self.store.read()
        if not self.password_matches_session_user(state, session, current_password):
            raise PermissionError("invalid current password")
        email = self.email_for_session_user(state, session)
        if not email:
            raise PermissionError("account email is not configured")
        email_service = self.email_service(state)
        enforce_resend_window(state, user_id=session.user.user_id, purposes={"change_password"}, resend_seconds=email_service.settings.resend_seconds)
        code = email_service.make_code()
        delivery = email_service.deliver_code(email=email, code=code, username=session.user.username or session.user.user_id, purpose="change_password")
        challenge_id = "otp_" + secrets.token_urlsafe(24)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=email_service.settings.ttl_minutes)).isoformat(timespec="seconds")
        challenge = {
            "challenge_id": challenge_id,
            "purpose": "change_password",
            "user_id": session.user.user_id,
            "role": session.user.role.value,
            "email": email,
            "new_password_hash": hash_password(new_password),
            "code_hash": hash_password(code),
            "attempts_remaining": email_service.settings.max_attempts,
            "expires_at": expires_at,
            "created_at": now_iso(),
        }

        def mutate(next_state: dict[str, Any]) -> None:
            prune_expired_challenges(next_state)
            remove_prior_challenges(next_state, user_id=session.user.user_id, purposes={"change_password"})
            next_state["auth_challenges"][challenge_id] = challenge
            append_audit(
                next_state,
                actor_id=session.user.user_id,
                action="start_password_change",
                target_type="auth_challenge",
                target_id=challenge_id,
                detail={"email": mask_email(email)},
            )

        self.store.update(mutate)
        return verification_response(
            challenge_id=challenge_id,
            email=email,
            expires_at=expires_at,
            delivery=delivery,
            trusted_device_days=email_service.settings.trusted_device_days,
        )

    def verify_password_change(self, session: AuthSession, *, challenge_id: str, code: str) -> dict[str, Any]:
        challenge = self.consume_code_challenge(challenge_id=challenge_id, code=code, allowed_purposes={"change_password"})
        if str(challenge.get("user_id") or "") != session.user.user_id:
            raise PermissionError("verification challenge does not belong to current account")
        new_hash = str(challenge.get("new_password_hash") or "")
        if not new_hash:
            raise PermissionError("password change challenge is invalid")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            user_id = session.user.user_id
            if session.user.role == Role.ADMIN:
                credentials = dict(self.admin_credentials(state))
                credentials.update(
                    {
                        "user_id": self.settings.admin_user_id,
                        "username": self.settings.admin_username,
                        "email": self.admin_email(state),
                        "password_hash": new_hash,
                        "updated_at": now_iso(),
                    }
                )
                state.setdefault("admin_credentials", {})[self.settings.admin_user_id] = credentials
                target_type = "admin"
            else:
                record = state.get("users", {}).get(user_id)
                if not isinstance(record, dict):
                    raise PermissionError("account not found")
                record["password_hash"] = new_hash
                record["updated_at"] = now_iso()
                target_type = "user"
            revoked = revoke_user_sessions(state, user_id=user_id, keep_token=session.token)
            append_audit(
                state,
                actor_id=user_id,
                action="change_password",
                target_type=target_type,
                target_id=user_id,
                detail={"revoked_sessions": revoked, "verified_by_email": True},
            )
            return {"user_id": user_id, "revoked_sessions": revoked}

        result = self.store.update(mutate)
        return {"changed": True, **result}

    def change_password(self, session: AuthSession, *, current_password: str, new_password: str) -> dict[str, Any]:
        # Kept for compatibility in dev mode; UI and production flows use start/verify.
        if self.email_service().settings.otp_required:
            raise PermissionError("email verification required for password changes")
        try:
            validate_password_strength(new_password)
        except ValueError as exc:
            raise PermissionError(str(exc)) from exc
        state = self.store.read()
        if not self.password_matches_session_user(state, session, current_password):
            raise PermissionError("invalid current password")
        challenge = {"new_password_hash": hash_password(new_password), "user_id": session.user.user_id}
        return self.apply_password_hash(session, str(challenge["new_password_hash"]))

    def admin_credentials(self, state: dict[str, Any]) -> dict[str, Any]:
        record = state.get("admin_credentials", {}).get(self.settings.admin_user_id)
        return record if isinstance(record, dict) else {}

    def admin_email(self, state: dict[str, Any]) -> str:
        return normalize_email(str(self.admin_credentials(state).get("email") or self.settings.admin_email))

    def verify_admin_password(self, state: dict[str, Any], password: str) -> bool:
        credentials = self.admin_credentials(state)
        password_hash = str(credentials.get("password_hash") or "")
        if password_hash:
            return verify_password(password, password_hash)
        return hmac.compare_digest(str(password or ""), self.settings.admin_password)

    def initialization_status(self, state: dict[str, Any], *, user: AuthUser, actor_id: str, email: str) -> dict[str, Any]:
        if user.role == Role.ADMIN:
            credentials = self.admin_credentials(state)
            explicit_email = normalize_email(str(credentials.get("email") or ""))
            smtp_config = state.get("smtp_config") if isinstance(state.get("smtp_config"), dict) else {}
            missing = []
            if not credentials.get("password_hash"):
                missing.append("change_password")
            if not explicit_email:
                missing.append("bind_email")
            if not smtp_config.get("initialized_at"):
                missing.append("smtp_config")
            if not credentials.get("initialized_at"):
                missing.append("finish_initialization")
            return {
                "required": bool(missing),
                "role": user.role.value,
                "username": user.username,
                "missing": sorted(set(missing), key=missing.index),
                "email": explicit_email,
            }

        record = state.get("users", {}).get(actor_id)
        stored_email = normalize_email(str(record.get("email") or "")) if isinstance(record, dict) else email
        missing = []
        if not isinstance(record, dict) or not record.get("password_hash"):
            missing.append("change_password")
        if not stored_email:
            missing.append("bind_email")
        if not isinstance(record, dict) or not record.get("initialized_at"):
            missing.append("finish_initialization")
        return {
            "required": bool(missing),
            "role": user.role.value,
            "username": user.username,
            "missing": sorted(set(missing), key=missing.index),
            "email": stored_email,
        }

    def create_initialization_challenge(
        self,
        *,
        state: dict[str, Any],
        user: AuthUser,
        active_tenant_id: str,
        actor_id: str,
        device_id: str,
        device_name: str,
        ip_address: str,
        init_status: dict[str, Any],
    ) -> dict[str, Any]:
        challenge_id = "init_" + secrets.token_urlsafe(24)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(timespec="seconds")
        challenge = {
            "challenge_id": challenge_id,
            "purpose": "initialize_account_pending",
            "username": user.username,
            "user_id": user.user_id,
            "user": user.to_dict(),
            "role": user.role.value,
            "active_tenant_id": active_tenant_id,
            "actor_id": actor_id,
            "device_fingerprint": device_fingerprint(actor_id, device_id),
            "device_name": device_name,
            "ip_address": ip_address,
            "expires_at": expires_at,
            "created_at": now_iso(),
        }

        def mutate(next_state: dict[str, Any]) -> None:
            prune_expired_challenges(next_state)
            remove_prior_challenges(next_state, user_id=actor_id, purposes={"initialize_account_pending", "initialize_account"})
            next_state["auth_challenges"][challenge_id] = challenge
            append_audit(
                next_state,
                actor_id=actor_id,
                action="start_account_initialization",
                target_type="auth_challenge",
                target_id=challenge_id,
                detail={"username": user.username, "role": user.role.value},
            )

        self.store.update(mutate)
        return {
            "requires_initialization": True,
            "challenge_id": challenge_id,
            "expires_at": expires_at,
            "role": user.role.value,
            "username": user.username,
            "missing": init_status.get("missing", []),
            "message": "首次登录前需要完成账号初始化。",
        }

    def start_account_initialization(
        self,
        *,
        challenge_id: str,
        email: str,
        new_password: str,
        smtp_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        email = normalize_email(email)
        if not email:
            raise PermissionError("valid email required")
        try:
            validate_password_strength(new_password)
        except ValueError as exc:
            raise PermissionError(str(exc)) from exc

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            prune_expired_challenges(state)
            challenge = state.get("auth_challenges", {}).get(str(challenge_id or "").strip())
            if not isinstance(challenge, dict) or str(challenge.get("purpose") or "") not in {"initialize_account_pending", "initialize_account"}:
                raise PermissionError("initialization challenge expired or not found")
            user_id = str(challenge.get("user_id") or "")
            role = role_from_value(challenge.get("role"))
            ensure_email_not_used(state, email, except_user_id=user_id)
            enforce_resend_window(
                state,
                user_id=user_id,
                purposes={"initialize_account"},
                resend_seconds=self.email_service(state).settings.resend_seconds,
            )
            pending_smtp = normalize_smtp_init_config(smtp_config, current=state.get("smtp_config") if isinstance(state.get("smtp_config"), dict) else {})
            email_service = EmailVerificationService(email_settings_from_config(pending_smtp)) if role == Role.ADMIN else self.email_service(state)
            code = email_service.make_code()
            delivery = email_service.deliver_code(
                email=email,
                code=code,
                username=str(challenge.get("username") or user_id),
                purpose="initialize_account",
            )
            challenge.update(
                {
                    "purpose": "initialize_account",
                    "email": email,
                    "new_password_hash": hash_password(new_password),
                    "pending_smtp_config": pending_smtp if role == Role.ADMIN else {},
                    "code_hash": hash_password(code),
                    "attempts_remaining": email_service.settings.max_attempts,
                    "last_sent_at": now_iso(),
                }
            )
            append_audit(
                state,
                actor_id=user_id,
                action="send_initialization_code",
                target_type="auth_challenge",
                target_id=str(challenge.get("challenge_id") or challenge_id),
                detail={"email": mask_email(email), "role": role.value},
            )
            return verification_response(
                challenge_id=str(challenge.get("challenge_id") or challenge_id),
                email=email,
                expires_at=str(challenge.get("expires_at") or ""),
                delivery=delivery,
                trusted_device_days=email_service.settings.trusted_device_days,
            )

        return self.store.update(mutate)

    def verify_account_initialization(self, *, challenge_id: str, code: str) -> dict[str, Any]:
        challenge = self.consume_code_challenge(challenge_id=challenge_id, code=code, allowed_purposes={"initialize_account"})
        email = normalize_email(str(challenge.get("email") or ""))
        new_hash = str(challenge.get("new_password_hash") or "")
        if not email or not new_hash:
            raise PermissionError("initialization challenge is invalid")
        user_id = str(challenge.get("user_id") or "")
        role = role_from_value(challenge.get("role"))

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            ensure_email_not_used(state, email, except_user_id=user_id)
            if role == Role.ADMIN:
                credentials = dict(self.admin_credentials(state))
                credentials.update(
                    {
                        "user_id": self.settings.admin_user_id,
                        "username": self.settings.admin_username,
                        "email": email,
                        "password_hash": new_hash,
                        "initialized_at": now_iso(),
                        "updated_at": now_iso(),
                    }
                )
                state.setdefault("admin_credentials", {})[self.settings.admin_user_id] = credentials
                pending_smtp = challenge.get("pending_smtp_config") if isinstance(challenge.get("pending_smtp_config"), dict) else {}
                if pending_smtp:
                    state["smtp_config"] = {**pending_smtp, "initialized_at": now_iso(), "updated_at": now_iso()}
                target_type = "admin"
            else:
                record = state.get("users", {}).get(user_id)
                if not isinstance(record, dict):
                    raise PermissionError("account not found")
                record["email"] = email
                record["password_hash"] = new_hash
                record["initialized_at"] = now_iso()
                record["updated_at"] = now_iso()
                target_type = "user"
            revoked = revoke_user_sessions(state, user_id=user_id)
            append_audit(
                state,
                actor_id=user_id,
                action="complete_account_initialization",
                target_type=target_type,
                target_id=user_id,
                detail={"email": mask_email(email), "revoked_sessions": revoked},
            )
            return {
                "initialized": True,
                "role": role.value,
                "username": str(challenge.get("username") or ""),
                "masked_email": mask_email(email),
                "revoked_sessions": revoked,
            }

        return self.store.update(mutate)

    def security_profile(self, session: AuthSession) -> dict[str, Any]:
        state = self.store.read()
        email = self.email_for_session_user(state, session)
        settings = self.email_service(state).settings
        trusted_devices = [
            public_trusted_device(record)
            for record in state.get("trusted_devices", {}).values()
            if isinstance(record, dict) and str(record.get("user_id") or "") == session.user.user_id
        ]
        trusted_devices = [item for item in trusted_devices if item]
        return {
            "user_id": session.user.user_id,
            "username": session.user.username,
            "role": session.user.role.value,
            "email": email,
            "masked_email": mask_email(email),
            "otp_required": settings.otp_required,
            "trusted_device_days": settings.trusted_device_days,
            "trusted_devices": sorted(trusted_devices, key=lambda item: str(item.get("last_seen_at") or ""), reverse=True),
        }

    def start_email_binding(self, session: AuthSession, *, email: str) -> dict[str, Any]:
        email = normalize_email(email)
        if not email:
            raise PermissionError("valid email required")
        state = self.store.read()
        ensure_email_not_used(state, email, except_user_id=session.user.user_id)
        email_service = self.email_service(state)
        enforce_resend_window(state, user_id=session.user.user_id, purposes={"bind_email"}, resend_seconds=email_service.settings.resend_seconds)
        code = email_service.make_code()
        delivery = email_service.deliver_code(email=email, code=code, username=session.user.username or session.user.user_id, purpose="bind_email")
        challenge_id = "otp_" + secrets.token_urlsafe(24)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=email_service.settings.ttl_minutes)).isoformat(timespec="seconds")
        challenge = {
            "challenge_id": challenge_id,
            "purpose": "bind_email",
            "user_id": session.user.user_id,
            "role": session.user.role.value,
            "email": email,
            "code_hash": hash_password(code),
            "attempts_remaining": email_service.settings.max_attempts,
            "expires_at": expires_at,
            "created_at": now_iso(),
        }

        def mutate(next_state: dict[str, Any]) -> None:
            prune_expired_challenges(next_state)
            remove_prior_challenges(next_state, user_id=session.user.user_id, purposes={"bind_email"})
            ensure_email_not_used(next_state, email, except_user_id=session.user.user_id)
            next_state["auth_challenges"][challenge_id] = challenge
            append_audit(
                next_state,
                actor_id=session.user.user_id,
                action="start_email_binding",
                target_type="auth_challenge",
                target_id=challenge_id,
                detail={"email": mask_email(email)},
            )

        self.store.update(mutate)
        return verification_response(
            challenge_id=challenge_id,
            email=email,
            expires_at=expires_at,
            delivery=delivery,
            trusted_device_days=email_service.settings.trusted_device_days,
        )

    def verify_email_binding(self, session: AuthSession, *, challenge_id: str, code: str) -> dict[str, Any]:
        challenge = self.consume_code_challenge(challenge_id=challenge_id, code=code, allowed_purposes={"bind_email"})
        if str(challenge.get("user_id") or "") != session.user.user_id:
            raise PermissionError("verification challenge does not belong to current account")
        email = normalize_email(str(challenge.get("email") or ""))
        if not email:
            raise PermissionError("email binding challenge is invalid")
        self.set_user_email(user_id=session.user.user_id, email=email, actor_id=session.user.user_id)
        return {"changed": True, "email": email, "masked_email": mask_email(email)}

    def consume_code_challenge(self, *, challenge_id: str, code: str, allowed_purposes: set[str]) -> dict[str, Any]:
        challenge_id = str(challenge_id or "").strip()
        code = str(code or "").strip()
        if not challenge_id or not code:
            raise PermissionError("verification code required")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            prune_expired_challenges(state)
            challenges = state.setdefault("auth_challenges", {})
            challenge = challenges.get(challenge_id)
            if not isinstance(challenge, dict) or str(challenge.get("purpose") or "") not in allowed_purposes:
                raise PermissionError("verification code expired or not found")
            if int(challenge.get("attempts_remaining") or 0) <= 0:
                challenges.pop(challenge_id, None)
                raise PermissionError("verification attempts exceeded")
            if not verify_password(code, str(challenge.get("code_hash") or "")):
                challenge["attempts_remaining"] = int(challenge.get("attempts_remaining") or 0) - 1
                raise PermissionError("invalid verification code")
            challenges.pop(challenge_id, None)
            append_audit(
                state,
                actor_id=str(challenge.get("actor_id") or challenge.get("user_id") or ""),
                action=f"verify_{challenge.get('purpose')}",
                target_type="auth_challenge",
                target_id=challenge_id,
                detail={"email": mask_email(str(challenge.get("email") or ""))},
            )
            return dict(challenge)

        return self.store.update(mutate)

    def set_user_email(self, *, user_id: str, email: str, actor_id: str) -> None:
        email = normalize_email(email)
        if not email:
            raise PermissionError("valid email required")

        def mutate(state: dict[str, Any]) -> None:
            ensure_email_not_used(state, email, except_user_id=user_id)
            if user_id == self.settings.admin_user_id:
                credentials = dict(self.admin_credentials(state))
                credentials.update(
                    {
                        "user_id": self.settings.admin_user_id,
                        "username": self.settings.admin_username,
                        "email": email,
                        "updated_at": now_iso(),
                    }
                )
                state.setdefault("admin_credentials", {})[self.settings.admin_user_id] = credentials
                target_type = "admin"
            else:
                record = state.get("users", {}).get(user_id)
                if not isinstance(record, dict):
                    raise PermissionError("account not found")
                record["email"] = email
                record["updated_at"] = now_iso()
                target_type = "user"
            append_audit(
                state,
                actor_id=actor_id,
                action="bind_email",
                target_type=target_type,
                target_id=user_id,
                detail={"email": mask_email(email)},
            )

        self.store.update(mutate)

    def password_matches_session_user(self, state: dict[str, Any], session: AuthSession, password: str) -> bool:
        if session.user.role == Role.ADMIN:
            return self.verify_admin_password(state, password)
        record = state.get("users", {}).get(session.user.user_id)
        if not isinstance(record, dict):
            return False
        return verify_password(str(password or ""), str(record.get("password_hash") or ""))

    def email_for_session_user(self, state: dict[str, Any], session: AuthSession) -> str:
        if session.user.role == Role.ADMIN:
            return self.admin_email(state)
        record = state.get("users", {}).get(session.user.user_id)
        if not isinstance(record, dict):
            return ""
        return normalize_email(str(record.get("email") or ""))

    def apply_password_hash(self, session: AuthSession, new_hash: str) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            user_id = session.user.user_id
            if session.user.role == Role.ADMIN:
                credentials = dict(self.admin_credentials(state))
                credentials.update(
                    {
                        "user_id": self.settings.admin_user_id,
                        "username": self.settings.admin_username,
                        "email": self.admin_email(state),
                        "password_hash": new_hash,
                        "updated_at": now_iso(),
                    }
                )
                state.setdefault("admin_credentials", {})[self.settings.admin_user_id] = credentials
                target_type = "admin"
            else:
                record = state.get("users", {}).get(user_id)
                if not isinstance(record, dict):
                    raise PermissionError("account not found")
                record["password_hash"] = new_hash
                record["updated_at"] = now_iso()
                target_type = "user"
            revoked = revoke_user_sessions(state, user_id=user_id, keep_token=session.token)
            append_audit(
                state,
                actor_id=user_id,
                action="change_password",
                target_type=target_type,
                target_id=user_id,
                detail={"revoked_sessions": revoked, "verified_by_email": False},
            )
            return {"changed": True, "user_id": user_id, "revoked_sessions": revoked}

        return self.store.update(mutate)

    def add_trusted_device(self, *, user_id: str, fingerprint: str, device_name: str = "", ip_address: str = "") -> None:
        settings = self.email_service().settings
        now = datetime.now(timezone.utc)
        record = {
            "fingerprint": fingerprint,
            "user_id": user_id,
            "device_name": str(device_name or "当前设备")[:120],
            "ip_address": str(ip_address or ""),
            "trusted_until": (now + timedelta(days=settings.trusted_device_days)).isoformat(timespec="seconds"),
            "created_at": now_iso(),
            "last_seen_at": now_iso(),
        }

        def mutate(state: dict[str, Any]) -> None:
            state.setdefault("trusted_devices", {})[fingerprint] = record
            append_audit(
                state,
                actor_id=user_id,
                action="trust_device",
                target_type="trusted_device",
                target_id=fingerprint,
                detail={"device_name": record["device_name"], "days": settings.trusted_device_days},
            )

        self.store.update(mutate)

    def touch_trusted_device(self, *, fingerprint: str) -> None:
        def mutate(state: dict[str, Any]) -> None:
            record = state.setdefault("trusted_devices", {}).get(fingerprint)
            if isinstance(record, dict):
                record["last_seen_at"] = now_iso()

        self.store.update(mutate)

    def create_session(self, user: AuthUser, active_tenant: str, *, actor_id: str) -> AuthSession:
        issued_at = datetime.now(timezone.utc)
        token = "vps_" + secrets.token_urlsafe(32)
        session = AuthSession(
            session_id=token,
            token=token,
            user=user,
            active_tenant_id=active_tenant,
            issued_at=issued_at.isoformat(timespec="seconds"),
            expires_at=(issued_at + timedelta(hours=self.settings.session_hours)).isoformat(timespec="seconds"),
            source="vps",
        )

        def mutate(state: dict[str, Any]) -> None:
            state["sessions"][token] = session.to_dict()
            append_audit(state, actor_id=actor_id, action="login", target_type="session", target_id=token)

        self.store.update(mutate)
        return session

    def resolve_token(self, token: str) -> AuthSession | None:
        if not token:
            return None
        state = self.store.read()
        payload = state.get("sessions", {}).get(token)
        if not isinstance(payload, dict):
            return None
        try:
            session = session_from_payload(payload)
        except Exception:
            return None
        if session.expired():
            return None
        return session

    def require_session(self, authorization: str = "") -> AuthSession:
        session = self.resolve_token(bearer_token(authorization))
        if session is None:
            raise HTTPException(status_code=401, detail="authentication required")
        return session

    def require_admin(self, authorization: str = "") -> AuthSession:
        session = self.require_session(authorization)
        if session.user.role != Role.ADMIN:
            raise HTTPException(status_code=403, detail="admin permission required")
        return session

    def require_node_enrollment(self, token: str = "") -> None:
        expected = self.settings.node_enrollment_token
        if not expected:
            return
        if not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="node enrollment token required")


def get_auth_service(request: Request) -> VpsAdminAuthService:
    return request.app.state.vps_admin_auth


def get_store(request: Request) -> VpsAdminStore:
    return request.app.state.vps_admin_store


def current_admin(request: Request, authorization: str = Header(default="")) -> AuthSession:
    return get_auth_service(request).require_admin(authorization)


def current_session(request: Request, authorization: str = Header(default="")) -> AuthSession:
    return get_auth_service(request).require_session(authorization)


def auth_user_from_record(record: dict[str, Any]) -> AuthUser:
    role = role_from_value(record.get("role"))
    if role == Role.ADMIN:
        raise ValueError("stored admin users are not supported")
    tenants = tuple(str(item) for item in record.get("tenant_ids", []) if str(item))
    return AuthUser(
        user_id=str(record.get("user_id") or ""),
        role=role,
        tenant_ids=tenants or (DEFAULT_TENANT_ID,),
        display_name=str(record.get("display_name") or record.get("username") or ""),
        username=str(record.get("username") or ""),
        resource_scopes=tuple(str(item) for item in record.get("resource_scopes", ["*"]) if str(item)),
    )


def auth_user_from_payload_allow_admin(payload: dict[str, Any]) -> AuthUser:
    role = role_from_value(payload.get("role"))
    tenant_values = payload.get("tenant_ids") if isinstance(payload.get("tenant_ids"), list) else [DEFAULT_TENANT_ID]
    return AuthUser(
        user_id=str(payload.get("user_id") or ""),
        role=role,
        tenant_ids=tuple(str(item) for item in tenant_values if str(item)) or (DEFAULT_TENANT_ID,),
        display_name=str(payload.get("display_name") or payload.get("username") or ""),
        username=str(payload.get("username") or ""),
        resource_scopes=tuple(str(item) for item in payload.get("resource_scopes", ["*"]) if str(item)),
    )


def find_user_by_username(state: dict[str, Any], username: str) -> dict[str, Any] | None:
    for record in state.get("users", {}).values():
        if isinstance(record, dict) and str(record.get("username") or "") == username:
            return record
    return None


def public_user(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in {"password_hash"}}


def ensure_not_reserved_admin(username: str, role: Role, settings: VpsAdminSettings) -> None:
    if role == Role.ADMIN:
        raise HTTPException(status_code=400, detail="the universal admin is env-managed and cannot be stored")
    if str(username or "").strip() == settings.admin_username:
        raise HTTPException(status_code=400, detail="reserved admin username")


def require_customer_or_guest(role_value: str) -> Role:
    role = role_from_value(role_value)
    if role not in {Role.CUSTOMER, Role.GUEST}:
        raise HTTPException(status_code=400, detail="only customer and guest accounts can be managed here")
    return role


def make_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(10).replace('-', '').replace('_', '')[:12]}"


def status_ok(record: dict[str, Any]) -> bool:
    return str(record.get("status") or "active") == "active"


def visible_tenants_for_user(session: AuthSession) -> list[str]:
    if session.user.role == Role.ADMIN:
        return ["*"]
    return list(session.user.tenant_ids)


def make_public_session(session: AuthSession) -> dict[str, Any]:
    payload = session.to_dict()
    payload["admin_hidden"] = session.user.role == Role.ADMIN
    return payload


def ensure_tenant_exists(state: dict[str, Any], tenant_id: str) -> None:
    if tenant_id == "*":
        return
    if tenant_id not in state.get("tenants", {}):
        raise HTTPException(status_code=404, detail=f"tenant not found: {tenant_id}")


def prune_expired_challenges(state: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    challenges = state.setdefault("auth_challenges", {})
    for challenge_id, challenge in list(challenges.items()):
        if not isinstance(challenge, dict):
            challenges.pop(challenge_id, None)
            continue
        expires_at = str(challenge.get("expires_at") or "")
        try:
            expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            challenges.pop(challenge_id, None)
            continue
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            challenges.pop(challenge_id, None)


def revoke_user_sessions(state: dict[str, Any], *, user_id: str, keep_token: str = "") -> int:
    sessions = state.setdefault("sessions", {})
    revoked = 0
    for token, payload in list(sessions.items()):
        if token == keep_token:
            continue
        if not isinstance(payload, dict):
            continue
        user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else payload
        if str(user_payload.get("user_id") or "") == user_id:
            sessions.pop(token, None)
            revoked += 1
    return revoked


def enforce_resend_window(state: dict[str, Any], *, user_id: str, purposes: set[str], resend_seconds: int) -> None:
    now = datetime.now(timezone.utc)
    for challenge in state.get("auth_challenges", {}).values():
        if not isinstance(challenge, dict):
            continue
        if str(challenge.get("purpose") or "") not in purposes:
            continue
        challenge_user = str(challenge.get("user_id") or challenge.get("actor_id") or "")
        if challenge_user != user_id:
            continue
        sent_at = parse_datetime(str(challenge.get("last_sent_at") or ""))
        if not sent_at:
            continue
        elapsed = (now - sent_at).total_seconds()
        if elapsed < resend_seconds:
            remaining = max(1, int(resend_seconds - elapsed))
            raise PermissionError(f"please wait {remaining} seconds before resending code")


def remove_prior_challenges(state: dict[str, Any], *, user_id: str, purposes: set[str]) -> None:
    challenges = state.setdefault("auth_challenges", {})
    for challenge_id, challenge in list(challenges.items()):
        if not isinstance(challenge, dict):
            continue
        if str(challenge.get("purpose") or "") not in purposes:
            continue
        challenge_user = str(challenge.get("user_id") or challenge.get("actor_id") or "")
        if challenge_user == user_id:
            challenges.pop(challenge_id, None)


def normalize_smtp_init_config(payload: dict[str, Any] | None, *, current: dict[str, Any]) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    current = current if isinstance(current, dict) else {}
    password = str(payload.get("password") or "")
    if not password:
        password = str(current.get("password") or "")
    return {
        "server": str(payload.get("server") or payload.get("smtp_host") or current.get("server") or "").strip(),
        "port": int(payload.get("port") or payload.get("smtp_port") or current.get("port") or 465),
        "use_ssl": bool_value(payload.get("use_ssl"), default=bool(current.get("use_ssl", True))),
        "use_tls": bool_value(payload.get("use_tls"), default=bool(current.get("use_tls", False))),
        "username": str(payload.get("username") or payload.get("smtp_username") or current.get("username") or "").strip(),
        "password": password,
        "from_email": normalize_email(str(payload.get("from_email") or payload.get("mail_from") or payload.get("username") or current.get("from_email") or "")),
        "sender_name": str(payload.get("sender_name") or current.get("sender_name") or "OmniAuto").strip() or "OmniAuto",
        "otp_required": bool_value(payload.get("otp_required"), default=True),
        "code_length": clamp_int(payload.get("code_length"), default=int(current.get("code_length") or 4), minimum=4, maximum=8),
        "ttl_minutes": clamp_int(payload.get("ttl_minutes"), default=int(current.get("ttl_minutes") or 15), minimum=1, maximum=60),
        "resend_seconds": clamp_int(payload.get("resend_seconds"), default=int(current.get("resend_seconds") or 60), minimum=10, maximum=600),
        "trusted_device_days": clamp_int(payload.get("trusted_device_days"), default=int(current.get("trusted_device_days") or 30), minimum=1, maximum=120),
    }


def bool_value(value: Any, *, default: bool = False) -> bool:
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


def device_fingerprint(user_id: str, device_id: str) -> str:
    stable_device_id = str(device_id or "").strip()
    if not stable_device_id:
        return ""
    return hashlib.sha256(f"{user_id}:{stable_device_id}".encode("utf-8")).hexdigest()


def trusted_device_valid(state: dict[str, Any], *, user_id: str, fingerprint: str) -> bool:
    record = state.get("trusted_devices", {}).get(fingerprint)
    if not isinstance(record, dict):
        return False
    if str(record.get("user_id") or "") != user_id:
        return False
    expires = parse_datetime(str(record.get("trusted_until") or ""))
    return bool(expires and expires > datetime.now(timezone.utc))


def public_trusted_device(record: dict[str, Any]) -> dict[str, Any]:
    if not trusted_device_record_active(record):
        return {}
    return {
        "fingerprint": str(record.get("fingerprint") or ""),
        "device_name": str(record.get("device_name") or "当前设备"),
        "trusted_until": str(record.get("trusted_until") or ""),
        "last_seen_at": str(record.get("last_seen_at") or ""),
    }


def trusted_device_record_active(record: dict[str, Any]) -> bool:
    expires = parse_datetime(str(record.get("trusted_until") or ""))
    return bool(expires and expires > datetime.now(timezone.utc))


def verification_response(
    *,
    challenge_id: str,
    email: str,
    expires_at: str,
    delivery: dict[str, Any],
    trusted_device_days: int,
) -> dict[str, Any]:
    public_delivery = {key: value for key, value in delivery.items() if key != "debug_code"}
    response: dict[str, Any] = {
        "requires_verification": True,
        "challenge_id": challenge_id,
        "masked_email": mask_email(email),
        "expires_at": expires_at,
        "trusted_device_days": trusted_device_days,
        "delivery": public_delivery,
    }
    if delivery.get("debug_code"):
        response["debug_code"] = delivery["debug_code"]
    return response


def ensure_email_not_used(state: dict[str, Any], email: str, *, except_user_id: str = "") -> None:
    normalized = normalize_email(email)
    if not normalized:
        raise PermissionError("valid email required")


def parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
