"""Single end-to-end owner of customer-service image capabilities."""

from __future__ import annotations

from typing import Any

from .contract import unavailable_result, vision_context
from .lifecycle import release_image_payload
from .ports import VisionHostPorts


class VisionService:
    """Public, directly callable facade for the complete vision capability.

    Heavy image, provider, Windows clipboard, product, and WeChat modules are
    imported only inside the operation that needs them.  Importing this class
    therefore remains valid in core-only and missing-optional-dependency modes.
    """

    def __init__(
        self,
        *,
        ports: VisionHostPorts | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._ports = ports or VisionHostPorts()
        self._config = dict(config or {})

    def available(self) -> bool:
        return True

    def should_run(self, context: dict[str, Any] | None) -> dict[str, Any]:
        from .trigger import customer_image_capture_trigger

        data = vision_context(context)
        return customer_image_capture_trigger(
            payload=data.get("payload") if isinstance(data.get("payload"), dict) else None,
            pending_signal=(data.get("pending_signal") if isinstance(data.get("pending_signal"), dict) else None),
            pending_signal_kind=str(data.get("pending_signal_kind") or ""),
            target_state=(data.get("target_state") if isinstance(data.get("target_state"), dict) else None),
            recent_message_limit=int(data.get("recent_message_limit") or 6),
        )

    def _fine_grained_ports_ready(self) -> bool:
        return all(
            (
                self._ports.conversation_target is not None,
                self._ports.window_frame is not None,
                self._ports.ui_action is not None,
                self._ports.clipboard is not None,
            )
        )

    def _inspect_via_ports(self, data: dict[str, Any]) -> dict[str, Any]:
        from .capture.transaction import acquire_current_image_via_ports
        from .projection.brain import (
            build_customer_image_brain_bridge,
            compact_customer_image_brain_bridge,
        )
        from .projection.message import build_brain_safe_image_proxy_messages
        from .result_schema import image_understanding_completed

        acquisition = acquire_current_image_via_ports(self._ports, data)
        if not acquisition.get("ok"):
            return {
                "enabled": True,
                "applied": False,
                "adoptable": False,
                "reason": str(acquisition.get("reason") or "vision_port_transaction_failed"),
                "acquisition_state": str(
                    acquisition.get("state")
                    or "vision_port_transaction_failed"
                ),
                "clipboard_transaction": dict(acquisition.get("transaction") or {}),
            }
        payload = acquisition.pop("_ephemeral_clipboard_image", None)
        if payload is None:
            return unavailable_result("clipboard_current_content_not_bitmap")
        try:
            provider = self._ports.vision_provider
            if provider is not None:
                try:
                    understanding = provider.understand(
                        {
                            "image": payload,
                            "customer_text": str(data.get("combined") or data.get("customer_text") or ""),
                            "message_id": str(data.get("message_id") or data.get("pending_signal_id") or "memory-current-image"),
                            "config": data.get("config") if isinstance(data.get("config"), dict) else self._config,
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - preserve copy evidence
                    return {
                        "enabled": True,
                        "applied": False,
                        "adoptable": False,
                        "reason": str(exc) or "vision_provider_exception",
                        "error_type": type(exc).__name__,
                        "clipboard_transaction": dict(
                            acquisition.get("transaction") or {}
                        ),
                    }
                understanding = dict(understanding) if isinstance(understanding, dict) else {}
                understanding.setdefault("applied", False)
                understanding.setdefault("adoptable", False)
                understanding.setdefault("reason", "vision_provider_port_ready")
            else:
                from .understanding.service import maybe_run_customer_image_understanding

                understanding = maybe_run_customer_image_understanding(
                    config=(data.get("config") if isinstance(data.get("config"), dict) else self._config),
                    customer_text=str(data.get("combined") or data.get("customer_text") or ""),
                    image_assets=[{
                        "message_id": str(data.get("message_id") or data.get("pending_signal_id") or "memory-current-image"),
                        "message_type": "image",
                    }],
                    source_reason="vision_host_ports_current_transaction",
                    image_payloads=[payload],
                    ephemeral_clipboard=True,
                )
            direction = str(acquisition.get("direction") or "").strip().lower()
            bridge = build_customer_image_brain_bridge(
                understanding,
                {},
                source_reason="vision_host_ports_current_transaction",
            )
            understanding_schema: dict[str, Any] = {}
            if self._config.get("_chejin_c2_strict_adapter"):
                from .result_schema import (
                    image_result_schema,
                    project_to_schema,
                    validate_schema,
                )

                understanding_schema = image_result_schema(
                    self._config,
                    "customer_image_understanding_v1",
                )
                understanding = project_to_schema(
                    understanding,
                    understanding_schema,
                )
                bridge = {
                    "schema_version": 1,
                    **compact_customer_image_brain_bridge(bridge),
                }
                bridge_schema = image_result_schema(
                    self._config,
                    "visual_bridge_input_v1",
                )
                bridge = project_to_schema(bridge, bridge_schema)
                if (
                    not understanding_schema
                    or not bridge_schema
                    or validate_schema(
                        understanding,
                        understanding_schema,
                    )
                    or validate_schema(bridge, bridge_schema)
                ):
                    return {
                        "enabled": True,
                        "applied": False,
                        "adoptable": False,
                        "reason": (
                            "C2_IMAGE_UNDERSTANDING_SCHEMA_INVALID"
                        ),
                        "clipboard_transaction": dict(
                            acquisition.get("transaction") or {}
                        ),
                    }
            understanding_completed = image_understanding_completed(
                understanding,
                understanding_schema,
            )
            occurrence = acquisition.get("occurrence") if isinstance(acquisition.get("occurrence"), dict) else {}
            proxies = (
                build_brain_safe_image_proxy_messages(
                    [occurrence],
                    target_name=str(data.get("target_name") or ""),
                    session_key=str(data.get("session_key") or ""),
                )
                if direction == "customer"
                else []
            )
            result = {
                "enabled": True,
                "applied": understanding_completed,
                "adoptable": bool(
                    direction == "customer"
                    and understanding_completed
                    and understanding.get("adoptable") is True
                ),
                "context_only": direction == "self",
                "reason": str(understanding.get("reason") or "vision_host_ports_ready"),
                "direction": direction,
                "customer_image_understanding": understanding,
                "image_understanding": understanding,
                "visual_bridge_input": bridge,
                "proxy_batch": proxies,
                "clipboard_transaction": dict(acquisition.get("transaction") or {}),
                "target_proof": dict(acquisition.get("target_proof") or {}),
            }
            audit = self._ports.audit
            if audit is not None:
                audit.record(
                    "vision_current_image_completed",
                    {
                        "direction": direction,
                        "applied": bool(result["applied"]),
                        "adoptable": bool(result["adoptable"]),
                        "reason": str(result["reason"]),
                    },
                )
            return result
        finally:
            release_image_payload(payload)

    def inspect_current_conversation(self, request: dict[str, Any] | None) -> dict[str, Any]:
        data = vision_context(request)
        connector = data.get("connector") or self._ports.connector
        strict_adapter = bool(self._config.get("_chejin_c2_strict_adapter"))
        if connector is None and self._fine_grained_ports_ready():
            return self._inspect_via_ports(data)
        if strict_adapter:
            return unavailable_result("chejin_c2_fine_grained_ports_required")
        if connector is None:
            return unavailable_result("vision_wechat_host_unavailable")
        from .runtime import maybe_route_customer_image_turn

        return maybe_route_customer_image_turn(
            connector=connector,
            target=data.get("target"),
            config=(data.get("config") if isinstance(data.get("config"), dict) else self._config),
            payload=data.get("payload") if isinstance(data.get("payload"), dict) else {},
            target_state=(data.get("target_state") if isinstance(data.get("target_state"), dict) else {}),
            batch=[item for item in (data.get("batch") or []) if isinstance(item, dict)],
            combined=str(data.get("combined") or ""),
        )

    def observe_current_surface(self, request: dict[str, Any] | None) -> dict[str, Any]:
        data = vision_context(request)
        connector = data.get("connector") or self._ports.connector
        if connector is None:
            return {
                "ok": False,
                "state": "vision_wechat_host_unavailable",
                "reason": "vision_wechat_host_unavailable",
                "assets": [],
                "messages": [],
            }
        target = data.get("target")
        target_name = str(getattr(target, "name", "") or data.get("target_name") or "")
        if not target_name and isinstance(target, str):
            target_name = target
        from .integrations.wechat_current import observe_current_surface

        return observe_current_surface(
            connector,
            target_name,
            exact=bool(getattr(target, "exact", data.get("exact", True))),
            session_key=str(getattr(target, "session_key", "") or data.get("session_key") or ""),
            conversation_type=str(
                getattr(target, "conversation_type", "")
                or data.get("conversation_type")
                or ""
            ),
            side_filter=str(data.get("side_filter") or "all"),
            max_images=int(data.get("max_images") or 64),
        )

    def inspect_self_context(self, request: dict[str, Any] | None) -> dict[str, Any]:
        from .runtime import maybe_capture_self_image_context

        data = vision_context(request)
        connector = data.get("connector") or self._ports.connector
        if connector is None:
            return unavailable_result("vision_wechat_host_unavailable", context_only=True)
        return maybe_capture_self_image_context(
            connector=connector,
            target=data.get("target"),
            config=(data.get("config") if isinstance(data.get("config"), dict) else self._config),
            messages=[item for item in (data.get("messages") or []) if isinstance(item, dict)],
            target_state=(data.get("target_state") if isinstance(data.get("target_state"), dict) else {}),
            combined=str(data.get("combined") or ""),
        )

    def understand_memory_image(self, request: dict[str, Any] | None) -> dict[str, Any]:
        from .clipboard_payload import ephemeral_image_from_memory
        from .understanding.service import maybe_run_customer_image_understanding

        data = vision_context(request)
        effective_config = (
            data.get("config")
            if isinstance(data.get("config"), dict)
            else self._config
        )
        supplied = data.get("image")
        owned_payload = False
        if isinstance(supplied, (bytes, bytearray, memoryview)):
            payload = ephemeral_image_from_memory(
                supplied,
                mime_type=str(data.get("mime_type") or "image/png"),
                width=int(data.get("width") or 0),
                height=int(data.get("height") or 0),
                source_limits=effective_config,
            )
            owned_payload = True
        else:
            payload = supplied
        if payload is None:
            return unavailable_result("vision_memory_image_missing")
        try:
            return maybe_run_customer_image_understanding(
                config=effective_config,
                customer_text=str(data.get("customer_text") or ""),
                image_assets=[{"message_id": str(data.get("message_id") or "memory-image"), "message_type": "image"}],
                source_reason=str(data.get("source_reason") or "memory_image_api"),
                image_payloads=[payload],
                ephemeral_clipboard=True,
            )
        finally:
            if owned_payload:
                release_image_payload(payload)

    def index_product_images(self, request: dict[str, Any] | None) -> dict[str, Any]:
        from .vehicle_retrieval.integration import index_product_vehicle_images

        data = vision_context(request)
        return index_product_vehicle_images(
            str(data.get("product_id") or ""),
            force=bool(data.get("force", False)),
            store=data.get("store"),
            config=(data.get("config") if isinstance(data.get("config"), dict) else self._config),
        )

    def match_product_image(self, request: dict[str, Any] | None) -> dict[str, Any]:
        from .vehicle_retrieval.integration import match_customer_image_to_product_master

        data = vision_context(request)
        return match_customer_image_to_product_master(
            data.get("understanding") if isinstance(data.get("understanding"), dict) else {},
            data.get("image"),
            data.get("config") if isinstance(data.get("config"), dict) else self._config,
            store=data.get("store"),
        )


def create_vision_service(
    *,
    ports: VisionHostPorts | None = None,
    config: dict[str, Any] | None = None,
) -> VisionService:
    return VisionService(ports=ports, config=config)
