from __future__ import annotations

from typing import Any


class BuiltinVisionPlugin:
    name = "builtin_customer_image_understanding"
    capability = "vision"

    def __init__(self, *, ports: Any = None, config: dict[str, Any] | None = None) -> None:
        self._ports = ports
        self._config = dict(config or {})

    def _service(self) -> Any:
        from .service import create_vision_service

        return create_vision_service(ports=self._ports, config=self._config)

    def available(self) -> bool:
        return self._service().available()

    def should_run(self, context: dict[str, Any]) -> dict[str, Any]:
        return self._service().should_run(context)

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        return self._service().inspect_current_conversation(context)

    def observe_current_surface(self, context: dict[str, Any]) -> dict[str, Any]:
        """Return structural message envelopes without understanding pixels."""

        if self._config.get("_chejin_c2_strict_adapter"):
            raise RuntimeError("CHEJIN_C2_LEGACY_VISION_ENTRY_DISABLED")
        return self._service().observe_current_surface(context)

    def capture_self_context(self, context: dict[str, Any]) -> dict[str, Any]:
        """Return a text-only self-image context result; never a reply plan."""

        if self._config.get("_chejin_c2_strict_adapter"):
            raise RuntimeError("CHEJIN_C2_LEGACY_VISION_ENTRY_DISABLED")
        return self._service().inspect_self_context(context)

    def invoke(self, operation: str, context: dict[str, Any]) -> Any:
        """Dispatch implementation-owned supplemental operations lazily."""

        if self._config.get("_chejin_c2_strict_adapter"):
            raise RuntimeError("CHEJIN_C2_LEGACY_VISION_ENTRY_DISABLED")
        from .operations import invoke_vision_operation

        return invoke_vision_operation(operation, context)


def create_default_vision_plugin() -> BuiltinVisionPlugin:
    return BuiltinVisionPlugin()
