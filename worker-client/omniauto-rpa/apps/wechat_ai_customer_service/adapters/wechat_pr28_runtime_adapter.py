"""Runtime containment for the WeChat PR #28 integration.

This adapter preserves the connector API while applying host-side physical
identity and process-environment policy before entering the RPA layer. It must
not contain reply orchestration or optional Vision logic.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from typing import Any


# Fixed protected-file baseline.  The absolute Vision boundary check compares
# these values with the candidate and must fail visibly until an upstream
# reviewer approves and advances the protected baseline.
PR28_HEAD = "3afed619afc8c1e0e71231459acafa3c2aabe608"
PR28_BLOBS = {
    "apps/wechat_ai_customer_service/adapters/wechat_connector.py": "18473e424ca5f41d93e3dfee63df50d5bec31ed2",
    "apps/wechat_ai_customer_service/adapters/wechat_win32_ocr/text_normalization.py": "1582b42286d0c3529907e551adbd0271bc00a8a0",
    "apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py": "4ffcdbd83ea290c9a07782e868dfdaa5706d2806",
    "apps/wechat_ai_customer_service/tests/run_wechat_win32_ocr_compat_checks.py": "1d5af9c0a35cf71a75d4e2ba4cb523802359eedc",
    "apps/wechat_ai_customer_service/tests/run_wechat_win32_ocr_sender_role_screenshot_replay.py": "a79ec7717cccc7c6df27aa75733e5d773cce4f0a",
    "apps/wechat_ai_customer_service/tests/run_wechat_win32_ocr_window_action_planning_checks.py": "bef28c37cfba0991ddc2a09e4b2108291eb3cc3f",
    "apps/wechat_ai_customer_service/wechat_message_envelope.py": "b2af6878294693490b7e56b5f04dbb5f87dc0ace",
}
UPSTREAM_OMNIAUTO_COMMIT = "855c21881641cdb2f9fe69d3f2e1caa05e37d04d"


def physical_rpa_identity_kwargs(values: dict[str, Any]) -> dict[str, Any]:
    """Keep the opaque row key authoritative at the physical RPA boundary."""

    projected = dict(values or {})
    if str(projected.get("session_key") or "").strip():
        projected["conversation_type"] = ""
    return projected


def _install_sidecar_environment_containment(connector: Any) -> None:
    original = getattr(connector, "call_compat_sidecar", None)
    if not callable(original):
        return
    if bool(getattr(original, "_omniauto_pr28_environment_containment", False)):
        return

    @functools.wraps(original)
    def contained_call(
        args: list[str],
        *,
        allow_failure: bool = False,
        primary_payload: dict[str, Any] | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        overrides = dict(env_overrides or {})
        # Window/layout defaults are intentionally not set here.  The Sidecar
        # owns the single production policy and this adapter only passes
        # explicit operator overrides through.
        return original(
            args,
            allow_failure=allow_failure,
            primary_payload=primary_payload,
            env_overrides=overrides or None,
        )

    contained_call._omniauto_pr28_environment_containment = True
    try:
        connector.call_compat_sidecar = contained_call
    except (AttributeError, TypeError):
        return


@dataclass
class WeChatPr28RuntimeAdapter:
    """Transparent internal proxy around the upstream connector."""

    delegate: Any

    def __post_init__(self) -> None:
        _install_sidecar_environment_containment(self.delegate)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def get_messages(self, target: str, exact: bool = True, **kwargs: Any) -> dict[str, Any]:
        return self.delegate.get_messages(target, exact=exact, **physical_rpa_identity_kwargs(kwargs))

    def transcribe_voice_messages(self, target: str, exact: bool = True, **kwargs: Any) -> dict[str, Any]:
        return self.delegate.transcribe_voice_messages(
            target,
            exact=exact,
            **physical_rpa_identity_kwargs(kwargs),
        )

    def send_text(self, target: str, text: str, exact: bool = True, **kwargs: Any) -> dict[str, Any]:
        return self.delegate.send_text(target, text, exact=exact, **physical_rpa_identity_kwargs(kwargs))

    def send_text_and_verify(
        self,
        target: str,
        text: str,
        exact: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.delegate.send_text_and_verify(
            target,
            text,
            exact=exact,
            **physical_rpa_identity_kwargs(kwargs),
        )


def adapt_wechat_pr28_connector(connector: Any) -> Any:
    if isinstance(connector, WeChatPr28RuntimeAdapter):
        return connector
    return WeChatPr28RuntimeAdapter(connector)


__all__ = [
    "PR28_BLOBS",
    "PR28_HEAD",
    "UPSTREAM_OMNIAUTO_COMMIT",
    "WeChatPr28RuntimeAdapter",
    "adapt_wechat_pr28_connector",
    "physical_rpa_identity_kwargs",
]
