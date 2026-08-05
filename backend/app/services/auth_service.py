import hashlib
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.errors import AppError
from app.models.audit import OperationLog
from app.models.auth import AdminAccount, AdminLoginThrottle, AdminSession
from app.models.base import utcnow


PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19_456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
DUMMY_PASSWORD_HASH = PASSWORD_HASHER.hash("chejin-auth-dummy-password")


def normalize_username(username: str) -> str:
    normalized = unicodedata.normalize("NFKC", username).strip().casefold()
    if not normalized or len(normalized) > 128 or any(ord(char) < 32 for char in normalized):
        raise AppError("AUTH_INVALID_CREDENTIALS", "账号或密码错误", 401)
    return normalized


def _normalize_login_username(username: str) -> tuple[str, bool]:
    normalized = unicodedata.normalize("NFKC", username).strip().casefold()
    valid = bool(normalized) and len(normalized) <= 128 and not any(ord(char) < 32 for char in normalized)
    if valid:
        return normalized, True
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:16]
    return f"<invalid:{digest}>", False


def validate_new_password(password: str) -> None:
    if len(password) < 12 or len(password) > 256:
        raise AppError("ADMIN_PASSWORD_POLICY_INVALID", "密码长度必须为 12 到 256 个字符", 400)


def hash_password(password: str) -> str:
    validate_new_password(password)
    return PASSWORD_HASHER.hash(password)


def _verify_password(password_hash: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _throttle_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _throttle_rows(db: Session, username_normalized: str, ip_address: str) -> list[AdminLoginThrottle]:
    keys = [("username", _throttle_key(username_normalized)), ("ip", _throttle_key(ip_address))]
    rows: list[AdminLoginThrottle] = []
    for scope, key_hash in keys:
        row = db.scalar(
            select(AdminLoginThrottle)
            .where(AdminLoginThrottle.scope == scope, AdminLoginThrottle.key_hash == key_hash)
            .with_for_update()
        )
        if row is None:
            candidate = AdminLoginThrottle(scope=scope, key_hash=key_hash)
            try:
                # Concurrent first failures can race on the unique key. Keep
                # the outer login transaction usable and then lock the winner.
                with db.begin_nested():
                    db.add(candidate)
                    db.flush()
                row = candidate
            except IntegrityError:
                row = db.scalar(
                    select(AdminLoginThrottle)
                    .where(AdminLoginThrottle.scope == scope, AdminLoginThrottle.key_hash == key_hash)
                    .with_for_update()
                )
                if row is None:
                    raise
        rows.append(row)
    return rows


def assert_login_not_throttled(db: Session, username_normalized: str, ip_address: str) -> None:
    now = utcnow()
    rows = _throttle_rows(db, username_normalized, ip_address)
    retry_after = 0
    for row in rows:
        if row.blocked_until and _aware(row.blocked_until) > now:
            retry_after = max(retry_after, int((_aware(row.blocked_until) - now).total_seconds()) + 1)
    if retry_after:
        raise AppError(
            "AUTH_RATE_LIMITED",
            "登录尝试过于频繁，请稍后再试",
            429,
            {"retry_after_seconds": retry_after},
        )


def record_login_failure(db: Session, username_normalized: str, ip_address: str) -> None:
    settings = get_settings()
    now = utcnow()
    for row in _throttle_rows(db, username_normalized, ip_address):
        if _aware(row.window_started_at) + timedelta(seconds=settings.admin_login_rate_window_seconds) <= now:
            row.failure_count = 0
            row.window_started_at = now
            row.blocked_until = None
        row.failure_count += 1
        threshold = (
            settings.admin_login_username_max_failures
            if row.scope == "username"
            else settings.admin_login_ip_max_failures
        )
        if row.failure_count >= threshold:
            multiplier = 2 ** min(row.failure_count - threshold, 8)
            block_seconds = min(
                settings.admin_login_block_base_seconds * multiplier,
                settings.admin_login_block_max_seconds,
            )
            row.blocked_until = now + timedelta(seconds=block_seconds)
        row.updated_at = now


def clear_username_throttle(db: Session, username_normalized: str) -> None:
    db.execute(
        delete(AdminLoginThrottle).where(
            AdminLoginThrottle.scope == "username",
            AdminLoginThrottle.key_hash == _throttle_key(username_normalized),
        )
    )


def write_auth_audit(
    db: Session,
    *,
    event_type: str,
    username_normalized: str,
    ip_address: str | None,
    user_agent: str | None,
    request_id: str,
    result: str,
    account: AdminAccount | None = None,
    reason: str | None = None,
) -> OperationLog:
    metadata = {"username_normalized": username_normalized, "result": result}
    if reason:
        metadata["reason"] = reason
    log = OperationLog(
        event_type=event_type,
        module="auth",
        target_type="admin_account",
        target_id=account.id if account else None,
        operator_id=account.id if account else None,
        operator_name_snapshot=account.display_name if account else username_normalized,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:512] or None,
        request_id=request_id,
        before_data=None,
        after_data=None,
        extra_metadata=metadata,
    )
    db.add(log)
    return log


def authenticate_credentials(
    db: Session,
    *,
    username: str,
    password: str,
    ip_address: str,
    user_agent: str | None,
    request_id: str,
) -> tuple[AdminAccount, str, AdminSession]:
    username_normalized, username_valid = _normalize_login_username(username)
    try:
        assert_login_not_throttled(db, username_normalized, ip_address)
    except AppError:
        write_auth_audit(
            db,
            event_type="admin_login_failed",
            username_normalized=username_normalized,
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
            result="failed",
            reason="rate_limited",
        )
        raise

    account = (
        db.scalar(select(AdminAccount).where(AdminAccount.username_normalized == username_normalized))
        if username_valid
        else None
    )
    candidate_hash = account.password_hash if account else DUMMY_PASSWORD_HASH
    password_valid = _verify_password(candidate_hash, password)
    if account is None or not account.enabled or not password_valid:
        record_login_failure(db, username_normalized, ip_address)
        reason = "unknown_account" if account is None else "account_disabled" if not account.enabled else "password_mismatch"
        write_auth_audit(
            db,
            event_type="admin_login_failed",
            username_normalized=username_normalized,
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
            result="failed",
            account=account,
            reason=reason,
        )
        raise AppError("AUTH_INVALID_CREDENTIALS", "账号或密码错误", 401)

    if PASSWORD_HASHER.check_needs_rehash(account.password_hash):
        account.password_hash = PASSWORD_HASHER.hash(password)
    now = utcnow()
    settings = get_settings()
    absolute_expires_at = now + timedelta(seconds=settings.admin_session_absolute_seconds)
    idle_expires_at = min(
        now + timedelta(seconds=settings.admin_session_idle_seconds),
        absolute_expires_at,
    )
    raw_token = "cjs_" + secrets.token_urlsafe(48)
    admin_session = AdminSession(
        account_id=account.id,
        token_hash=hash_session_token(raw_token),
        session_version=account.session_version,
        created_at=now,
        last_seen_at=now,
        idle_expires_at=idle_expires_at,
        absolute_expires_at=absolute_expires_at,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:512] or None,
    )
    account.last_login_at = now
    db.add(admin_session)
    clear_username_throttle(db, username_normalized)
    write_auth_audit(
        db,
        event_type="admin_login_succeeded",
        username_normalized=username_normalized,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
        result="success",
        account=account,
    )
    db.flush()
    return account, raw_token, admin_session


def authenticate_session(db: Session, raw_token: str | None) -> tuple[AdminAccount, AdminSession]:
    if not raw_token or len(raw_token) > 512 or not raw_token.startswith("cjs_"):
        raise AppError("ADMIN_UNAUTHORIZED", "登录已失效，请重新登录", 401)
    row = db.execute(
        select(AdminSession, AdminAccount)
        .join(AdminAccount, AdminAccount.id == AdminSession.account_id)
        .where(AdminSession.token_hash == hash_session_token(raw_token))
    ).one_or_none()
    if row is None:
        raise AppError("ADMIN_UNAUTHORIZED", "登录已失效，请重新登录", 401)
    admin_session, account = row
    now = utcnow()
    invalid_reason = None
    if admin_session.revoked_at is not None:
        invalid_reason = admin_session.revoke_reason or "revoked"
    elif not account.enabled:
        invalid_reason = "account_disabled"
    elif admin_session.session_version != account.session_version:
        invalid_reason = "session_version_changed"
    elif _aware(admin_session.idle_expires_at) <= now:
        invalid_reason = "idle_expired"
    elif _aware(admin_session.absolute_expires_at) <= now:
        invalid_reason = "absolute_expired"
    if invalid_reason:
        if admin_session.revoked_at is None:
            admin_session.revoked_at = now
            admin_session.revoke_reason = invalid_reason
            db.flush()
        raise AppError("ADMIN_UNAUTHORIZED", "登录已失效，请重新登录", 401)

    settings = get_settings()
    if _aware(admin_session.last_seen_at) + timedelta(seconds=settings.admin_session_touch_interval_seconds) <= now:
        admin_session.last_seen_at = now
        admin_session.idle_expires_at = min(
            now + timedelta(seconds=settings.admin_session_idle_seconds),
            _aware(admin_session.absolute_expires_at),
        )
        db.flush()
    return account, admin_session


def revoke_session(db: Session, raw_token: str | None, *, reason: str = "logout") -> AdminAccount | None:
    if not raw_token or len(raw_token) > 512:
        return None
    row = db.execute(
        select(AdminSession, AdminAccount)
        .join(AdminAccount, AdminAccount.id == AdminSession.account_id)
        .where(AdminSession.token_hash == hash_session_token(raw_token))
        .with_for_update()
    ).one_or_none()
    if row is None:
        return None
    admin_session, account = row
    if admin_session.revoked_at is None:
        admin_session.revoked_at = utcnow()
        admin_session.revoke_reason = reason
    return account


def revoke_account_sessions(db: Session, account: AdminAccount, *, reason: str) -> None:
    now = utcnow()
    db.execute(
        update(AdminSession)
        .where(AdminSession.account_id == account.id, AdminSession.revoked_at.is_(None))
        .values(revoked_at=now, revoke_reason=reason)
    )


def create_account(db: Session, *, username: str, display_name: str, password: str) -> AdminAccount:
    username_normalized = normalize_username(username)
    clean_display_name = display_name.strip()
    if not clean_display_name or len(clean_display_name) > 64:
        raise AppError("ADMIN_DISPLAY_NAME_INVALID", "显示名称不能为空且不能超过 64 个字符", 400)
    if db.scalar(select(AdminAccount.id).where(AdminAccount.username_normalized == username_normalized)):
        raise AppError("ADMIN_ACCOUNT_DUPLICATED", "账号已存在", 409)
    account = AdminAccount(
        username_normalized=username_normalized,
        display_name=clean_display_name,
        password_hash=hash_password(password),
        enabled=True,
        session_version=1,
    )
    db.add(account)
    db.flush()
    return account


def reset_account_password(db: Session, *, username: str, password: str) -> AdminAccount:
    account = get_account_by_username(db, username, lock=True)
    account.password_hash = hash_password(password)
    account.session_version += 1
    revoke_account_sessions(db, account, reason="password_reset")
    return account


def set_account_enabled(db: Session, *, username: str, enabled: bool) -> AdminAccount:
    account = get_account_by_username(db, username, lock=True)
    if account.enabled == enabled:
        return account
    account.enabled = enabled
    if not enabled:
        account.session_version += 1
        revoke_account_sessions(db, account, reason="account_disabled")
    return account


def get_account_by_username(db: Session, username: str, *, lock: bool = False) -> AdminAccount:
    query = select(AdminAccount).where(AdminAccount.username_normalized == normalize_username(username))
    if lock:
        query = query.with_for_update()
    account = db.scalar(query)
    if account is None:
        raise AppError("ADMIN_ACCOUNT_NOT_FOUND", "后台账号不存在", 404)
    return account
