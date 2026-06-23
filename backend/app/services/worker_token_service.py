import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings
from app.errors import AppError


def generate_worker_token() -> str:
    return f"wkt_{secrets.token_urlsafe(32)}"


def hash_worker_token(token: str) -> str:
    secret = get_settings().contact_encryption_secret.encode("utf-8")
    return hmac.new(secret, token.encode("utf-8"), hashlib.sha256).hexdigest()


def _cipher() -> Fernet:
    secret = get_settings().contact_encryption_secret.encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt_worker_token(token: str) -> str:
    return _cipher().encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_worker_token(encrypted: str) -> str:
    try:
        return _cipher().decrypt(encrypted.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise AppError("WORKER_TOKEN_DECRYPT_FAILED", "Worker Token 解密失败", 500) from exc
