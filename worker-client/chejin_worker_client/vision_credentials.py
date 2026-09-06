from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock

import base64
import json
import os
from pathlib import Path
import sys
from typing import Any


VISION_API_KEY_ENV = "CUSTOMER_IMAGE_UNDERSTANDING_API_KEY"
OFFICIAL_VISION_PROVIDER = "anthropic_compatible"
OFFICIAL_VISION_BASE_URL = "https://aiself.vip/v1"
OFFICIAL_VISION_MODEL = "doubao-seed-2-0-lite-260428"
OFFICIAL_VISION_REQUEST_STYLE = "anthropic_messages_vision"
_VISION_PROBE_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAN0lEQVR4nO3RwQ0A"
    "MAjDwJT9d05HMB9+vgGCZF7bXJrT9XhgwR8gEyETIRMhEyETIRMhEyEThXzH8QM9O"
    "MM6fAAAAABJRU5ErkJggg=="
)


def _runtime_build_identity() -> dict[str, Any]:
    roots: list[Path] = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(Path(frozen_root))
    configured = str(os.environ.get("CHEJIN_BUILD_IDENTITY_PATH") or "").strip()
    if configured and not getattr(sys, "frozen", False):
        roots.append(Path(configured).parent)
    roots.extend(
        [
            Path(__file__).resolve().parents[1],
            Path(sys.executable).resolve().parent,
        ]
    )
    for root in roots:
        try:
            payload = json.loads(
                (root / "runtime-build-identity.json").read_text(
                    encoding="utf-8-sig"
                )
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def is_official_vision_runtime() -> bool:
    """Return True for a distributed executable or an official build probe."""

    if getattr(sys, "frozen", False):
        return True
    build_kind = str(os.environ.get("CHEJIN_BUILD_KIND") or "").strip().lower()
    if build_kind:
        return build_kind in {"official", "debug_uat_locked"}
    identity = _runtime_build_identity()
    return bool(identity.get("formal_release")) or str(
        identity.get("build_kind") or ""
    ).strip().lower() in {"official", "debug_uat_locked"}


# Runtime memory only. A generation rejects late responses after another fetch,
# rebind, or shutdown; ContextVar snapshots keep an admitted media flow stable.
_lock = RLock()
_generation = 0
_runtime_key = ""
_failure_reason = "VISION_CREDENTIAL_NOT_CONFIGURED"
_flow_key: ContextVar[str | None] = ContextVar("vision_flow_key", default=None)


def begin_credential_refresh() -> int:
    global _generation, _runtime_key, _failure_reason
    with _lock:
        _generation += 1
        _runtime_key = ""
        _failure_reason = "VISION_CREDENTIAL_FETCH_PENDING"
        return _generation


def complete_credential_refresh(generation: int, key: str = "", *, failure_reason: str = "") -> bool:
    global _runtime_key, _failure_reason
    with _lock:
        if generation != _generation:
            return False
        _runtime_key = key
        _failure_reason = failure_reason or ("" if key else "VISION_CREDENTIAL_NOT_CONFIGURED")
        return True


def clear_vision_credential() -> None:
    complete_credential_refresh(begin_credential_refresh())


def resolve_vision_api_key() -> str:
    snapshot = _flow_key.get()
    if snapshot is not None:
        return snapshot
    with _lock:
        return _runtime_key


@contextmanager
def vision_credential_snapshot():
    token = _flow_key.set(resolve_vision_api_key())
    try:
        yield
    finally:
        _flow_key.reset(token)


def install_resolved_vision_api_key() -> bool:
    """Compatibility capability check; never put the key in the parent environment."""
    return bool(resolve_vision_api_key())


def vision_provider_environment(base: dict[str, str]) -> dict[str, str]:
    environment = dict(base)
    environment.pop(VISION_API_KEY_ENV, None)
    key = resolve_vision_api_key()
    if key:
        environment[VISION_API_KEY_ENV] = key
    return environment


def resolve_vision_runtime_settings() -> dict[str, str]:
    if is_official_vision_runtime():
        return {
            "provider": OFFICIAL_VISION_PROVIDER,
            "base_url": OFFICIAL_VISION_BASE_URL,
            "model": OFFICIAL_VISION_MODEL,
            "request_style": OFFICIAL_VISION_REQUEST_STYLE,
        }
    return {
        "provider": str(
            os.environ.get("CUSTOMER_IMAGE_UNDERSTANDING_PROVIDER")
            or OFFICIAL_VISION_PROVIDER
        ).strip(),
        "base_url": str(
            os.environ.get("CUSTOMER_IMAGE_UNDERSTANDING_BASE_URL")
            or OFFICIAL_VISION_BASE_URL
        ).strip(),
        "model": str(
            os.environ.get("CUSTOMER_IMAGE_UNDERSTANDING_MODEL")
            or OFFICIAL_VISION_MODEL
        ).strip(),
        "request_style": str(
            os.environ.get("CUSTOMER_IMAGE_UNDERSTANDING_REQUEST_STYLE")
            or OFFICIAL_VISION_REQUEST_STYLE
        ).strip(),
    }


def vision_credential_status() -> dict[str, Any]:
    """Return a secret-free status safe for preflight reports and manifests."""

    official = is_official_vision_runtime()
    settings = resolve_vision_runtime_settings()
    return {
        "configured": bool(resolve_vision_api_key()),
        "credential_source": "worker_backend",
        "failure_reason": "" if resolve_vision_api_key() else _failure_reason,
        "configuration_locked": official,
        **settings,
    }


def _run_vision_provider_probe_request(
    api_key: str,
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    omniauto_root = Path(__file__).resolve().parents[1] / "omniauto-rpa"
    if str(omniauto_root) not in sys.path:
        sys.path.insert(0, str(omniauto_root))
    from apps.wechat_ai_customer_service.optional_plugins.vision.understanding.provider import (
        run_customer_image_understanding_provider,
    )

    return run_customer_image_understanding_provider(
        api_key=api_key,
        base_url=OFFICIAL_VISION_BASE_URL,
        model=OFFICIAL_VISION_MODEL,
        request_style=OFFICIAL_VISION_REQUEST_STYLE,
        prompt='Return exactly one JSON object: {"probe":"ok"}.',
        image_paths=[],
        timeout_seconds=max(3, min(60, int(timeout_seconds))),
        max_tokens=64,
        temperature=0.0,
        image_payloads=[
            {
                "image_bytes": base64.b64decode(_VISION_PROBE_PNG_BASE64),
                "mime_type": "image/png",
            }
        ],
    )


def probe_official_vision_provider(
    *,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Perform a synthetic, customer-data-free live capability probe.

    The result is deliberately restricted to non-secret operational facts.
    Provider response bodies and exception text are never returned.
    """

    api_key = resolve_vision_api_key()
    if not is_official_vision_runtime() or not api_key:
        return {
            "ok": False,
            "status": 0,
            "failure_reason": "vision_credential_unavailable",
            "model": OFFICIAL_VISION_MODEL,
            "request_style": OFFICIAL_VISION_REQUEST_STYLE,
        }
    try:
        result = _run_vision_provider_probe_request(
            api_key,
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        result = {}
    ok = result.get("ok") is True and int(result.get("status") or 0) == 200
    return {
        "ok": ok,
        "status": int(result.get("status") or 0),
        "failure_reason": "" if ok else "vision_provider_probe_failed",
        "model": OFFICIAL_VISION_MODEL,
        "request_style": OFFICIAL_VISION_REQUEST_STYLE,
    }
