from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.core.config import get_settings


TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
OPEN_ID_PATH = "/open-apis/contact/v3/users/batch_get_id?user_id_type=open_id"
MESSAGE_PATH = "/open-apis/im/v1/messages?receive_id_type=open_id"


@dataclass(frozen=True)
class FeishuAdapterError(Exception):
    code: str
    summary: str
    result_unknown: bool = False

    def __str__(self) -> str:
        return self.summary


Requester = Callable[[str, str, dict[str, str], bytes, float], tuple[int, bytes]]


def _urllib_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
) -> tuple[int, bytes]:
    request = Request(url=url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS base URL
            return int(response.status), response.read()
    except HTTPError as exc:
        return int(exc.code), exc.read()


def _json_object(raw: bytes, *, error_code: str, result_unknown: bool = False) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeishuAdapterError(
            error_code,
            "provider_response_invalid_json",
            result_unknown=result_unknown,
        ) from exc
    if not isinstance(value, dict):
        raise FeishuAdapterError(
            error_code,
            "provider_response_not_object",
            result_unknown=result_unknown,
        )
    return value


def _provider_code(payload: dict[str, Any]) -> int | str:
    value = payload.get("code")
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value or "missing")[:32]


class FeishuAdapter:
    def __init__(
        self,
        *,
        app_id: str | None = None,
        app_secret: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        requester: Requester | None = None,
    ) -> None:
        settings = get_settings()
        self._app_id = str(app_id if app_id is not None else settings.feishu_app_id or "").strip()
        self._app_secret = str(
            app_secret if app_secret is not None else settings.feishu_app_secret or ""
        ).strip()
        self._base_url = str(base_url or settings.feishu_base_url).rstrip("/")
        self._timeout_seconds = float(timeout_seconds or settings.feishu_http_timeout_seconds)
        self._requester = requester or _urllib_request
        self._token_lock = threading.Lock()
        self._tenant_access_token = ""
        self._token_expires_at = 0.0

    def configuration_status(self) -> dict[str, Any]:
        ready = bool(self._app_id and self._app_secret)
        return {
            "ready": ready,
            "provider": "feishu",
            "app_configured": ready,
            "error_code": None if ready else "FEISHU_APP_CONFIG_MISSING",
        }

    def _require_configuration(self) -> None:
        if not self._app_id or not self._app_secret:
            raise FeishuAdapterError(
                "FEISHU_APP_CONFIG_MISSING",
                "feishu_app_credentials_missing",
            )

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        token: str | None,
        error_code: str,
        result_unknown_on_transport: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            status, raw = self._requester(
                "POST",
                f"{self._base_url}{path}",
                headers,
                body,
                self._timeout_seconds,
            )
        except (TimeoutError, socket.timeout, URLError, OSError) as exc:
            raise FeishuAdapterError(
                error_code,
                f"provider_transport_error={type(exc).__name__}",
                result_unknown=result_unknown_on_transport,
            ) from exc
        result_unknown = result_unknown_on_transport and int(status) >= 500
        if int(status) < 200 or int(status) >= 300:
            raise FeishuAdapterError(
                error_code,
                f"provider_http_status={int(status)}",
                result_unknown=result_unknown,
            )
        return int(status), _json_object(
            raw,
            error_code=error_code,
            result_unknown=result_unknown_on_transport,
        )

    def _get_tenant_access_token(self) -> str:
        self._require_configuration()
        now = time.monotonic()
        if self._tenant_access_token and now < self._token_expires_at:
            return self._tenant_access_token
        with self._token_lock:
            now = time.monotonic()
            if self._tenant_access_token and now < self._token_expires_at:
                return self._tenant_access_token
            _, payload = self._post_json(
                TOKEN_PATH,
                {"app_id": self._app_id, "app_secret": self._app_secret},
                token=None,
                error_code="FEISHU_TOKEN_FETCH_FAILED",
            )
            if _provider_code(payload) != 0:
                raise FeishuAdapterError(
                    "FEISHU_TOKEN_FETCH_FAILED",
                    f"provider_code={_provider_code(payload)}",
                )
            token = str(payload.get("tenant_access_token") or "").strip()
            try:
                expires_in = int(payload.get("expire") or 0)
            except (TypeError, ValueError):
                expires_in = 0
            if not token or expires_in <= 0:
                raise FeishuAdapterError(
                    "FEISHU_TOKEN_FETCH_FAILED",
                    "provider_token_or_expiry_missing",
                )
            self._tenant_access_token = token
            self._token_expires_at = time.monotonic() + max(1, expires_in - 60)
            return token

    def lookup_open_id(self, normalized_phone: str) -> str:
        if len(normalized_phone) != 11 or not normalized_phone.isdigit():
            raise FeishuAdapterError("FEISHU_PHONE_INVALID", "sales_phone_invalid")
        token = self._get_tenant_access_token()
        _, payload = self._post_json(
            OPEN_ID_PATH,
            {"mobiles": [normalized_phone]},
            token=token,
            error_code="FEISHU_OPEN_ID_LOOKUP_FAILED",
        )
        if _provider_code(payload) != 0:
            provider_message = str(payload.get("msg") or "").lower()
            permission_markers = ("permission", "scope", "权限", "可用范围")
            code = (
                "FEISHU_USER_OUT_OF_SCOPE"
                if any(marker in provider_message for marker in permission_markers)
                else "FEISHU_OPEN_ID_LOOKUP_FAILED"
            )
            raise FeishuAdapterError(code, f"provider_code={_provider_code(payload)}")

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        raw_users = data.get("user_list") if isinstance(data, dict) else []
        users = raw_users if isinstance(raw_users, list) else []
        matches: list[str] = []
        for user in users:
            if not isinstance(user, dict):
                continue
            mobile = "".join(ch for ch in str(user.get("mobile") or "") if ch.isdigit())
            if mobile and mobile != normalized_phone:
                continue
            open_id = str(user.get("open_id") or user.get("user_id") or "").strip()
            if open_id:
                matches.append(open_id)
        unique_matches = list(dict.fromkeys(matches))
        if not unique_matches:
            raise FeishuAdapterError("FEISHU_OPEN_ID_NOT_FOUND", "open_id_not_found")
        if len(unique_matches) != 1:
            raise FeishuAdapterError("FEISHU_OPEN_ID_CONFLICT", "open_id_not_unique")
        open_id = unique_matches[0]
        if not open_id.startswith("ou_"):
            raise FeishuAdapterError("FEISHU_OPEN_ID_CONFLICT", "returned_id_is_not_open_id")
        return open_id

    def send_text_message(self, open_id: str, text: str) -> None:
        if not open_id.startswith("ou_"):
            raise FeishuAdapterError("FEISHU_OPEN_ID_MISSING", "recipient_open_id_invalid")
        token = self._get_tenant_access_token()
        _, payload = self._post_json(
            MESSAGE_PATH,
            {
                "receive_id": open_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False, separators=(",", ":")),
            },
            token=token,
            error_code="FEISHU_MESSAGE_SEND_FAILED",
            result_unknown_on_transport=True,
        )
        if _provider_code(payload) != 0:
            raise FeishuAdapterError(
                "FEISHU_MESSAGE_SEND_FAILED",
                f"provider_code={_provider_code(payload)}",
            )


_adapter_lock = threading.Lock()
_adapter: FeishuAdapter | None = None


def get_feishu_adapter() -> FeishuAdapter:
    global _adapter
    if _adapter is not None:
        return _adapter
    with _adapter_lock:
        if _adapter is None:
            _adapter = FeishuAdapter()
        return _adapter
def check_feishu_readiness() -> dict[str, Any]:
    return get_feishu_adapter().configuration_status()
