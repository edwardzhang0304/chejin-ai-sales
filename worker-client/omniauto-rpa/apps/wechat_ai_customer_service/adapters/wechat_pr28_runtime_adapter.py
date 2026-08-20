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
    "UPSTREAM_OMNIAUTO_COMMIT",
    "WeChatPr28RuntimeAdapter",
    "adapt_wechat_pr28_connector",
    "physical_rpa_identity_kwargs",
]
