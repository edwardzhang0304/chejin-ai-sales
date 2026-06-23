"""Small JSON client for VPS authorization and coordination APIs."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from .models import AuthSession, session_from_payload


class VpsClientError(RuntimeError):
    pass


_DISCOVERY_CACHE: dict[str, Any] = {"key": None, "value": "", "expires_at": 0.0}


def discover_vps_base_url() -> str:
    configured = (os.getenv("WECHAT_VPS_BASE_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    if not _parse_bool_env(os.getenv("WECHAT_VPS_AUTO_DISCOVER"), default=True):
        return ""
    candidates = [
        item.strip().rstrip("/")
        for item in (os.getenv("WECHAT_VPS_DISCOVERY_URLS") or "http://127.0.0.1:8766,http://localhost:8766").split(",")
        if item.strip()
    ]
    timeout = _safe_float(os.getenv("WECHAT_VPS_DISCOVERY_TIMEOUT_SECONDS"), default=0.35)
    cache_seconds = max(0.0, _safe_float(os.getenv("WECHAT_VPS_DISCOVERY_CACHE_SECONDS"), default=10.0))
    cache_key = (tuple(candidates), timeout, cache_seconds)
    now = time.monotonic()
    if _DISCOVERY_CACHE.get("key") == cache_key and now < float(_DISCOVERY_CACHE.get("expires_at") or 0):
        return str(_DISCOVERY_CACHE.get("value") or "")
    discovered = ""
    for base_url in candidates:
        if _vps_health_ok(base_url, timeout=timeout):
            discovered = base_url
            break
    _DISCOVERY_CACHE.update({"key": cache_key, "value": discovered, "expires_at": now + cache_seconds})
    return discovered


def _vps_health_ok(base_url: str, *, timeout: float) -> bool:
    if not base_url:
        return False
    request = urllib.request.Request(base_url.rstrip("/") + "/v1/health", headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= int(getattr(response, "status", 200)) < 500
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return False


def _parse_bool_env(value: str | None, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _safe_float(value: str | None, *, default: float) -> float:
    try:
        return float(value) if value not in {None, ""} else default
    except (TypeError, ValueError):
        return default


class VpsAuthClient:
    def __init__(self, *, base_url: str = "", timeout_seconds: float = 8) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def login(self, *, username: str, password: str, tenant_id: str | None = None) -> AuthSession:
        if not self.configured:
            raise VpsClientError("WECHAT_VPS_BASE_URL is not configured")
        payload = self.post_json(
            "/v1/auth/login",
            {
                "username": username,
                "password": password,
                "tenant_id": tenant_id or "",
            },
        )
        session_payload = payload.get("session") if isinstance(payload.get("session"), dict) else payload
        return session_from_payload({**session_payload, "source": "vps"})

    def start_login(
        self,
        *,
        username: str,
        password: str,
        tenant_id: str | None = None,
        device_id: str = "",
        device_name: str = "",
    ) -> dict[str, Any]:
        if not self.configured:
            raise VpsClientError("WECHAT_VPS_BASE_URL is not configured")
        return self.post_json(
            "/v1/auth/login/start",
            {
                "username": username,
                "password": password,
                "tenant_id": tenant_id or "",
                "device_id": device_id,
                "device_name": device_name,
            },
        )

    def start_login_email_binding(self, *, challenge_id: str, email: str) -> dict[str, Any]:
        if not self.configured:
            raise VpsClientError("WECHAT_VPS_BASE_URL is not configured")
        return self.post_json(
            "/v1/auth/login/bind-email/start",
            {
                "challenge_id": challenge_id,
                "email": email,
            },
        )

    def verify_login(self, *, challenge_id: str, code: str, trust_device: bool = False) -> AuthSession:
        if not self.configured:
            raise VpsClientError("WECHAT_VPS_BASE_URL is not configured")
        payload = self.post_json(
            "/v1/auth/login/verify",
            {
                "challenge_id": challenge_id,
                "code": code,
                "trust_device": trust_device,
            },
        )
        session_payload = payload.get("session") if isinstance(payload.get("session"), dict) else payload
        return session_from_payload({**session_payload, "source": "vps"})

    def start_account_initialization(
        self,
        *,
        challenge_id: str,
        email: str,
        new_password: str,
        smtp_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.configured:
            raise VpsClientError("WECHAT_VPS_BASE_URL is not configured")
        return self.post_json(
            "/v1/auth/initialize/start",
            {
                "challenge_id": challenge_id,
                "email": email,
                "new_password": new_password,
                "smtp_config": smtp_config or {},
            },
        )

    def verify_account_initialization(self, *, challenge_id: str, code: str) -> dict[str, Any]:
        if not self.configured:
            raise VpsClientError("WECHAT_VPS_BASE_URL is not configured")
        return self.post_json(
            "/v1/auth/initialize/verify",
            {
                "challenge_id": challenge_id,
                "code": code,
            },
        )

    def change_password(self, *, token: str, current_password: str, new_password: str) -> dict[str, Any]:
        if not self.configured:
            raise VpsClientError("WECHAT_VPS_BASE_URL is not configured")
        return self.post_json(
            "/v1/auth/change-password",
            {
                "current_password": current_password,
                "new_password": new_password,
            },
            token=token,
        )

    def security_profile(self, *, token: str) -> dict[str, Any]:
        if not self.configured:
            raise VpsClientError("WECHAT_VPS_BASE_URL is not configured")
        return self.get_json("/v1/auth/security", token=token)

    def start_email_binding(self, *, token: str, email: str) -> dict[str, Any]:
        if not self.configured:
            raise VpsClientError("WECHAT_VPS_BASE_URL is not configured")
        return self.post_json("/v1/auth/email/start", {"email": email}, token=token)

    def verify_email_binding(self, *, token: str, challenge_id: str, code: str) -> dict[str, Any]:
        if not self.configured:
            raise VpsClientError("WECHAT_VPS_BASE_URL is not configured")
        return self.post_json("/v1/auth/email/verify", {"challenge_id": challenge_id, "code": code}, token=token)

    def start_password_change(self, *, token: str, current_password: str, new_password: str) -> dict[str, Any]:
        if not self.configured:
            raise VpsClientError("WECHAT_VPS_BASE_URL is not configured")
        return self.post_json(
            "/v1/auth/change-password/start",
            {"current_password": current_password, "new_password": new_password},
            token=token,
        )

    def verify_password_change(self, *, token: str, challenge_id: str, code: str) -> dict[str, Any]:
        if not self.configured:
            raise VpsClientError("WECHAT_VPS_BASE_URL is not configured")
        return self.post_json(
            "/v1/auth/change-password/verify",
            {"challenge_id": challenge_id, "code": code},
            token=token,
        )

    def post_json(self, path: str, payload: dict[str, Any], *, token: str = "", headers: dict[str, str] | None = None) -> dict[str, Any]:
        return self.request_json("POST", path, payload=payload, token=token, extra_headers=headers)

    def get_json(self, path: str, *, token: str = "", headers: dict[str, str] | None = None) -> dict[str, Any]:
        return self.request_json("GET", path, payload=None, token=token, extra_headers=headers)

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None,
        token: str = "",
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.configured:
            raise VpsClientError("WECHAT_VPS_BASE_URL is not configured")
        url = self.base_url + "/" + path.lstrip("/")
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if extra_headers:
            headers.update({str(key): str(value) for key, value in extra_headers.items()})
        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw_error = exc.read().decode("utf-8", errors="replace")
            detail = raw_error.strip()
            try:
                parsed = json.loads(raw_error or "{}")
                if isinstance(parsed, dict):
                    detail = str(parsed.get("detail") or parsed.get("message") or parsed.get("error") or detail)
            except json.JSONDecodeError:
                pass
            raise VpsClientError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise VpsClientError(str(exc)) from exc
        try:
            payload_out = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise VpsClientError("VPS returned invalid JSON") from exc
        if isinstance(payload_out, dict) and payload_out.get("ok") is False:
            raise VpsClientError(str(payload_out.get("message") or payload_out.get("error") or "VPS request failed"))
        if not isinstance(payload_out, dict):
            raise VpsClientError("VPS returned non-object JSON")
        return payload_out
