"""Email verification-code delivery used by local and VPS login flows."""

from __future__ import annotations

import json
import os
import secrets
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.knowledge_paths import runtime_app_root


@dataclass(frozen=True)
class EmailVerificationSettings:
    otp_required: bool
    debug: bool
    code_length: int
    ttl_minutes: int
    max_attempts: int
    resend_seconds: int
    trusted_device_days: int
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_use_ssl: bool
    smtp_use_tls: bool
    sender_name: str
    mail_from: str
    outbox_path: Path


def load_email_settings() -> EmailVerificationSettings:
    return EmailVerificationSettings(
        otp_required=parse_bool(os.getenv("WECHAT_EMAIL_OTP_REQUIRED"), default=False),
        debug=parse_bool(os.getenv("WECHAT_EMAIL_OTP_DEBUG"), default=False),
        code_length=max(4, int(os.getenv("WECHAT_EMAIL_OTP_CODE_LENGTH") or os.getenv("VERIFY_CODE_LENGTH") or "4")),
        ttl_minutes=max(1, int(os.getenv("WECHAT_EMAIL_OTP_TTL_MINUTES") or os.getenv("VERIFY_CODE_EXPIRE_MINUTES") or "15")),
        max_attempts=max(1, int(os.getenv("WECHAT_EMAIL_OTP_MAX_ATTEMPTS") or "5")),
        resend_seconds=max(1, int(os.getenv("WECHAT_EMAIL_OTP_RESEND_SECONDS") or "60")),
        trusted_device_days=max(1, int(os.getenv("WECHAT_TRUSTED_DEVICE_DAYS") or os.getenv("TRUSTED_DEVICE_DAYS") or "30")),
        smtp_host=(os.getenv("WECHAT_EMAIL_SMTP_HOST") or os.getenv("MAIL_SERVER") or "").strip(),
        smtp_port=int(os.getenv("WECHAT_EMAIL_SMTP_PORT") or os.getenv("MAIL_PORT") or "465"),
        smtp_username=(os.getenv("WECHAT_EMAIL_SMTP_USERNAME") or os.getenv("MAIL_USERNAME") or "").strip(),
        smtp_password=os.getenv("WECHAT_EMAIL_SMTP_PASSWORD") or os.getenv("MAIL_PASSWORD") or "",
        smtp_use_ssl=parse_bool(os.getenv("WECHAT_EMAIL_SMTP_USE_SSL") or os.getenv("MAIL_USE_SSL"), default=True),
        smtp_use_tls=parse_bool(os.getenv("WECHAT_EMAIL_SMTP_USE_TLS"), default=False),
        sender_name=(os.getenv("WECHAT_EMAIL_SENDER_NAME") or os.getenv("MAIL_SENDER_NAME") or "OmniAuto").strip(),
        mail_from=(os.getenv("WECHAT_EMAIL_FROM") or os.getenv("WECHAT_EMAIL_SMTP_USERNAME") or os.getenv("MAIL_USERNAME") or "no-reply@omniauto.local").strip(),
        outbox_path=Path(os.getenv("WECHAT_EMAIL_OUTBOX_PATH") or runtime_app_root() / "auth" / "email_outbox.jsonl"),
    )


def email_settings_from_config(config: dict[str, Any] | None) -> EmailVerificationSettings:
    base = load_email_settings()
    config = config if isinstance(config, dict) else {}
    smtp_host = str(config.get("server") or config.get("smtp_host") or base.smtp_host or "").strip()
    smtp_username = str(config.get("username") or config.get("smtp_username") or base.smtp_username or "").strip()
    smtp_password = str(config.get("password") or config.get("smtp_password") or base.smtp_password or "")
    # Treat an incomplete SMTP form as not configured so local/dev outbox remains usable.
    if not (smtp_host and smtp_username and smtp_password):
        smtp_host = ""
    return EmailVerificationSettings(
        otp_required=parse_bool(str(config.get("otp_required", "")), default=base.otp_required),
        debug=parse_bool(str(config.get("debug", "")), default=base.debug),
        code_length=max(4, int(config.get("code_length") or base.code_length)),
        ttl_minutes=max(1, int(config.get("ttl_minutes") or base.ttl_minutes)),
        max_attempts=max(1, int(config.get("max_attempts") or base.max_attempts)),
        resend_seconds=max(1, int(config.get("resend_seconds") or base.resend_seconds)),
        trusted_device_days=max(1, int(config.get("trusted_device_days") or base.trusted_device_days)),
        smtp_host=smtp_host,
        smtp_port=int(config.get("port") or config.get("smtp_port") or base.smtp_port),
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        smtp_use_ssl=parse_bool(str(config.get("use_ssl", "")), default=base.smtp_use_ssl),
        smtp_use_tls=parse_bool(str(config.get("use_tls", "")), default=base.smtp_use_tls),
        sender_name=str(config.get("sender_name") or base.sender_name or "OmniAuto"),
        mail_from=str(config.get("from_email") or config.get("mail_from") or smtp_username or base.mail_from),
        outbox_path=Path(str(config.get("outbox_path") or base.outbox_path)),
    )


class EmailVerificationService:
    def __init__(self, settings: EmailVerificationSettings | None = None) -> None:
        self.settings = settings or load_email_settings()

    def make_code(self) -> str:
        upper = 10 ** self.settings.code_length
        return f"{secrets.randbelow(upper):0{self.settings.code_length}d}"

    def deliver_code(self, *, email: str, code: str, username: str, purpose: str) -> dict[str, Any]:
        normalized_email = normalize_email(email)
        if not normalized_email:
            raise ValueError("account email is not configured")
        subject = f"OmniAuto - {purpose_label(purpose)} - 验证码 {code}"
        body = (
            f"账号 {username} 正在进行 {purpose_label(purpose)}。\n\n"
            f"验证码：{code}\n"
            f"有效期：{self.settings.ttl_minutes} 分钟。\n\n"
            "如果不是本人操作，请立即联系管理员。"
        )
        if self.settings.smtp_host and (not self.settings.smtp_username or self.settings.smtp_password):
            self._send_smtp(email=normalized_email, subject=subject, body=body)
            delivery = {"method": "smtp", "masked_email": mask_email(normalized_email)}
        else:
            self._append_outbox(email=normalized_email, subject=subject, body=body, code=code, username=username, purpose=purpose)
            delivery = {
                "method": "dev_outbox",
                "masked_email": mask_email(normalized_email),
                "outbox_path": str(self.settings.outbox_path),
            }
        if self.settings.debug:
            delivery["debug_code"] = code
        return delivery

    def _send_smtp(self, *, email: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"{self.settings.sender_name} <{self.settings.mail_from}>"
        message["To"] = email
        message.set_content(body)
        smtp_cls = smtplib.SMTP_SSL if self.settings.smtp_use_ssl else smtplib.SMTP
        try:
            with smtp_cls(self.settings.smtp_host, self.settings.smtp_port, timeout=15) as client:
                if self.settings.smtp_use_tls and not self.settings.smtp_use_ssl:
                    client.starttls()
                if self.settings.smtp_username:
                    client.login(self.settings.smtp_username, self.settings.smtp_password)
                client.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            detail = decode_smtp_error(exc.smtp_error)
            raise ValueError(f"SMTP认证失败：请确认SMTP账号、授权码/客户端专用密码和邮箱SMTP服务已启用。服务器返回：{detail}") from exc
        except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
            raise ValueError(f"SMTP连接失败：请确认服务器、端口、SSL/STARTTLS设置和网络连通性。错误：{exc}") from exc
        except smtplib.SMTPException as exc:
            raise ValueError(f"SMTP发送失败：{exc}") from exc

    def send_test_email(self, *, to_email: str) -> dict[str, Any]:
        code = self.make_code()
        return self.deliver_code(email=to_email, code=code, username="smtp-test", purpose="smtp_test")

    def _append_outbox(self, *, email: str, subject: str, body: str, code: str, username: str, purpose: str) -> None:
        record = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "email": email,
            "masked_email": mask_email(email),
            "subject": subject,
            "body": body,
            "code": code,
            "username": username,
            "purpose": purpose,
        }
        self.settings.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        with self.settings.outbox_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def normalize_email(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text or "@" not in text:
        return ""
    return text


def mask_email(value: str) -> str:
    email = normalize_email(value)
    if not email:
        return ""
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        masked = name[:1] + "*"
    else:
        masked = name[:2] + "***" + name[-1:]
    return f"{masked}@{domain}"


def parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def decode_smtp_error(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "").strip()


def purpose_label(value: str) -> str:
    return {
        "login": "登录验证",
        "bind_email": "绑定邮箱验证",
        "bind_email_login": "绑定邮箱登录验证",
        "initialize_account": "账号初始化验证",
        "change_password": "密码修改验证",
        "smtp_test": "邮件配置测试",
    }.get(value, "安全验证")
