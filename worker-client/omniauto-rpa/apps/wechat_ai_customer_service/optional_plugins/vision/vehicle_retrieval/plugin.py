"""Lazy optional-plugin facade for vehicle image indexing and fingerprints."""

from __future__ import annotations

from typing import Any


class VehicleImageRetrievalPlugin:
    name = "builtin_vehicle_image_retrieval"
    capability = "vehicle_image_retrieval"

    def available(self) -> bool:
        try:
            from .fingerprint import perceptual_dhash  # noqa: F401
        except Exception:
            return False
        return True

    def should_run(self, context: dict[str, Any]) -> dict[str, Any]:
        operation = str(context.get("operation") or "").strip()
        return {"run": operation in {"fingerprint", "describe_product_image"}, "operation": operation}

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        operation = str(context.get("operation") or "").strip()
        image_bytes = context.get("_image_bytes")
        if not isinstance(image_bytes, (bytes, bytearray, memoryview)):
            return {"ok": False, "reason": "vehicle_image_bytes_missing"}
        raw = bytes(image_bytes)
        try:
            from .fingerprint import perceptual_dhash

            fingerprint = perceptual_dhash(raw)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": "vehicle_image_fingerprint_failed", "error_type": type(exc).__name__}
        if operation == "fingerprint":
            return {"ok": True, "perceptual_hash": fingerprint}
        if operation != "describe_product_image":
            return {"ok": False, "reason": "vehicle_image_retrieval_operation_unsupported"}
        from .descriptor import describe_product_image

        result = describe_product_image(
            image_bytes=raw,
            mime_type=str(context.get("mime_type") or "image/jpeg"),
            picture=context.get("picture") if isinstance(context.get("picture"), dict) else {},
            settings=context.get("settings") if isinstance(context.get("settings"), dict) else {},
        )
        if result.get("ok"):
            result["perceptual_hash"] = fingerprint
        return result


def create_default_vehicle_image_retrieval_plugin() -> VehicleImageRetrievalPlugin:
    return VehicleImageRetrievalPlugin()
