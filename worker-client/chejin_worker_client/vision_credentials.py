from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any


VISION_API_KEY_ENV = "CUSTOMER_IMAGE_UNDERSTANDING_API_KEY"
VISION_CREDENTIAL_FILENAME = "vision-runtime.json"
OFFICIAL_VISION_PROVIDER = "anthropic_compatible"
OFFICIAL_VISION_BASE_URL = "https://aiself.vip/v1"
OFFICIAL_VISION_MODEL = "doubao-seed-2-0-lite-260428"
OFFICIAL_VISION_REQUEST_STYLE = "anthropic_messages_vision"


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
        return build_kind == "official"
    identity = _runtime_build_identity()
    return bool(identity.get("formal_release")) or str(
        identity.get("build_kind") or ""
    ).strip().lower() == "official"


def _official_credential_path() -> Path | None:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / VISION_CREDENTIAL_FILENAME
    configured = str(
        os.environ.get("CHEJIN_VISION_CREDENTIAL_PATH") or ""
    ).strip()
    return Path(configured) if configured else None


def _read_official_credential() -> str:
    path = _official_credential_path()
    if path is None:
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != 1:
        return ""
    return str(payload.get("vision_api_key") or "").strip()


def resolve_vision_api_key() -> str:
    """Resolve the dedicated key without logging or returning its source path.

    Distributed builds ignore ordinary environment overrides. Source development
    builds may use the dedicated development environment variable.
    """

    if is_official_vision_runtime():
        return _read_official_credential()
    return str(os.environ.get(VISION_API_KEY_ENV) or "").strip()


def install_resolved_vision_api_key() -> bool:
    """Install the resolved key only for the isolated provider child process."""

    api_key = resolve_vision_api_key()
    if not api_key:
        os.environ.pop(VISION_API_KEY_ENV, None)
        return False
    os.environ[VISION_API_KEY_ENV] = api_key
    return True


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
        "credential_source": "embedded" if official else "development_environment",
        "configuration_locked": official,
        **settings,
    }
