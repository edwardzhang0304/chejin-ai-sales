"""Shared password hashing helpers for local and VPS auth."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt.encode("utf-8"), PASSWORD_ITERATIONS)
    encoded = base64.b64encode(digest).decode("ascii")
    return f"{PASSWORD_ALGORITHM}${PASSWORD_ITERATIONS}${salt}${encoded}"


def verify_password(password: str, encoded: str) -> bool:
    parts = str(encoded or "").split("$")
    if len(parts) != 4 or parts[0] != PASSWORD_ALGORITHM:
        return False
    try:
        iterations = int(parts[1])
        salt = parts[2]
        expected = base64.b64decode(parts[3].encode("ascii"))
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt.encode("utf-8"), iterations)
    return hmac.compare_digest(actual, expected)


def validate_password_strength(password: str) -> None:
    text = str(password or "")
    if len(text) < 8:
        raise ValueError("new password must contain at least 8 characters")
    if text.strip() != text:
        raise ValueError("new password cannot start or end with spaces")
    if not any(char.isalpha() for char in text) or not any(char.isdigit() for char in text):
        raise ValueError("new password must contain both letters and numbers")
