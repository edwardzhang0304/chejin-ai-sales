import base64
import hashlib
import hmac
import re
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings
from app.errors import AppError


PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class NormalizedContact:
    raw: str
    normalized: str
    contact_hash: str
    masked: str
    encrypted: str


def _hash_value(value: str) -> str:
    secret = get_settings().phone_hash_secret.encode("utf-8")
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _contact_cipher() -> Fernet:
    secret = get_settings().contact_encryption_secret.encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def _encrypt_contact(value: str) -> str:
    return _contact_cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_for_p0(encrypted: str) -> str:
    try:
        return _contact_cipher().decrypt(encrypted.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise AppError("CONTACT_DECRYPT_FAILED", "联系方式解密失败", 500)
    except ValueError as exc:
        raise AppError("CONTACT_DECRYPT_FAILED", "联系方式解密失败", 500) from exc


def normalize_phone(phone: str) -> NormalizedContact:
    normalized = re.sub(r"\D", "", phone.strip())
    if not normalized:
        raise AppError("LEAD_PHONE_REQUIRED", "请输入手机号", 400)
    if not PHONE_RE.fullmatch(normalized):
        raise AppError("LEAD_PHONE_INVALID", "手机号格式不正确", 400)
    return NormalizedContact(
        raw=phone.strip(),
        normalized=normalized,
        contact_hash=_hash_value(normalized),
        masked=f"{normalized[:3]}****{normalized[-4:]}",
        encrypted=_encrypt_contact(normalized),
    )


def normalize_wechat(value: str) -> NormalizedContact:
    normalized = value.strip()
    if not normalized:
        raise AppError("VALIDATION_ERROR", "微信号不能为空", 400)
    if len(normalized) < 2 or len(normalized) > 64:
        raise AppError("VALIDATION_ERROR", "微信号长度需为 2-64 字", 400)
    masked = normalized if len(normalized) <= 4 else f"{normalized[:2]}****{normalized[-2:]}"
    return NormalizedContact(value.strip(), normalized, _hash_value(normalized.lower()), masked, _encrypt_contact(normalized))


def normalize_email(value: str) -> NormalizedContact:
    normalized = value.strip().lower()
    if not normalized:
        raise AppError("VALIDATION_ERROR", "邮箱不能为空", 400)
    if not EMAIL_RE.fullmatch(normalized):
        raise AppError("VALIDATION_ERROR", "邮箱格式不正确", 400)
    name, domain = normalized.split("@", 1)
    masked = f"{name[:2]}***@{domain}" if len(name) > 2 else f"{name[:1]}***@{domain}"
    return NormalizedContact(value.strip(), normalized, _hash_value(normalized), masked, _encrypt_contact(normalized))


def ensure_no_duplicates(values: list[str], code: str = "LEAD_CONTACT_DUPLICATED_IN_REQUEST") -> None:
    seen: set[str] = set()
    for value in values:
        key = value.strip().lower()
        if key in seen:
            raise AppError(code, "该联系方式已填写，请勿重复添加", 400)
        seen.add(key)
