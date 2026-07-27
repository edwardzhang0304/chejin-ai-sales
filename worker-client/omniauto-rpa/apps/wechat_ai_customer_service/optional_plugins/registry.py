from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from .contract import OptionalCapabilityPlugin
from .loader import load_factory


PluginFactory = Callable[[], OptionalCapabilityPlugin]
DEFAULT_PLUGIN_FACTORIES = {
    "voice": "apps.wechat_ai_customer_service.optional_plugins.voice.plugin:create_default_voice_plugin",
    "vision": "apps.wechat_ai_customer_service.optional_plugins.vision.plugin:create_default_vision_plugin",
    "vehicle_image_retrieval": "apps.wechat_ai_customer_service.optional_plugins.vehicle_image_retrieval.plugin:create_default_vehicle_image_retrieval_plugin",
}


@dataclass
class _Registration:
    plugin: OptionalCapabilityPlugin | None = None
    factory: PluginFactory | None = None
    factory_path: str = ""
    loaded: bool = False
    error: str = ""


_LOCK = threading.RLock()
_REGISTRATIONS: dict[str, _Registration] = {}


def _clean_capability(capability: str) -> str:
    clean = str(capability or "").strip().lower()
    if not clean:
        raise ValueError("optional capability name is required")
    return clean


def register_optional_capability(
    capability: str,
    *,
    plugin: OptionalCapabilityPlugin | None = None,
    factory: PluginFactory | None = None,
    factory_path: str = "",
    replace: bool = True,
) -> None:
    clean = _clean_capability(capability)
    if plugin is None and factory is None and not str(factory_path or "").strip():
        raise ValueError("plugin, factory, or factory_path is required")
    with _LOCK:
        if not replace and clean in _REGISTRATIONS:
            raise ValueError(f"optional capability already registered: {clean}")
        _REGISTRATIONS[clean] = _Registration(
            plugin=plugin,
            factory=factory,
            factory_path=str(factory_path or "").strip(),
            loaded=plugin is not None,
        )


def unregister_optional_capability(capability: str) -> None:
    clean = _clean_capability(capability)
    with _LOCK:
        _REGISTRATIONS.pop(clean, None)


def _default_registration(capability: str) -> _Registration | None:
    factory_path = DEFAULT_PLUGIN_FACTORIES.get(capability)
    if not factory_path:
        return None
    return _Registration(factory_path=factory_path)


def resolve_optional_capability(capability: str) -> OptionalCapabilityPlugin | None:
    clean = _clean_capability(capability)
    with _LOCK:
        registration = _REGISTRATIONS.get(clean)
        if registration is None:
            registration = _default_registration(clean)
            if registration is None:
                return None
            _REGISTRATIONS[clean] = registration
        if registration.loaded:
            return registration.plugin
        try:
            factory = registration.factory
            if factory is None:
                factory = load_factory(registration.factory_path)
            plugin = factory()
            if plugin is None:
                raise TypeError("optional capability factory returned None")
            if str(getattr(plugin, "capability", "")).strip().lower() != clean:
                raise ValueError(f"optional capability mismatch: expected {clean!r}")
            registration.plugin = plugin
            registration.loaded = True
            registration.error = ""
            return plugin
        except Exception as exc:
            registration.error = repr(exc)
            registration.loaded = True
            registration.plugin = None
            return None


def get_optional_capability_status(capability: str) -> dict[str, Any]:
    clean = _clean_capability(capability)
    with _LOCK:
        registration = _REGISTRATIONS.get(clean)
        if registration is None:
            return {
                "capability": clean,
                "registered": False,
                "loaded": False,
                "available": False,
                "error": "",
            }
        plugin = registration.plugin
        available = False
        if plugin is not None:
            try:
                available = bool(plugin.available())
            except Exception:
                available = False
        return {
            "capability": clean,
            "registered": True,
            "loaded": registration.loaded,
            "available": available,
            "plugin_name": str(getattr(plugin, "name", "") or ""),
            "error": registration.error,
        }


def reset_optional_capabilities_for_tests() -> None:
    with _LOCK:
        _REGISTRATIONS.clear()
