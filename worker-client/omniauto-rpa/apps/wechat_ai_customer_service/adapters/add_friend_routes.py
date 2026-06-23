"""Route manifest for add_friend RPA capabilities."""

from __future__ import annotations

from typing import Any

from apps.wechat_ai_customer_service.adapters.add_friend_contract import (
    ADD_FRIEND_ENTRY_CLICK_WINDOWS_ROUTE,
)


ADD_FRIEND_WINDOWS_ROUTE = ADD_FRIEND_ENTRY_CLICK_WINDOWS_ROUTE
ADD_FRIEND_CANONICAL_ROUTE = ADD_FRIEND_WINDOWS_ROUTE
ADD_FRIEND_MAIN_ROUTE = ADD_FRIEND_WINDOWS_ROUTE
ADD_FRIEND_WINDOWS_MAIN_ROUTE = ADD_FRIEND_WINDOWS_ROUTE

ADD_FRIEND_FORMAL_ROUTES = (ADD_FRIEND_WINDOWS_ROUTE,)
ADD_FRIEND_ROUTES = (ADD_FRIEND_WINDOWS_ROUTE,)

ADD_FRIEND_ROUTE_MANIFEST: dict[str, dict[str, Any]] = {
    ADD_FRIEND_WINDOWS_ROUTE: {
        "kind": "windows",
        "description": "Worker-facing Windows add_friend entry-click CLI.",
        "accepts_query": True,
        "accepts_formal_fields": True,
        "passive_probe": False,
        "diagnostic": False,
        "legacy": False,
        "official_main": True,
        "platform": "windows",
        "alias_of": "",
    },
}


def normalize_action_name(action: Any) -> str:
    return str(action or "").strip().lower()


def is_add_friend_route(action: Any) -> bool:
    return normalize_action_name(action) in ADD_FRIEND_ROUTE_MANIFEST


def add_friend_route_metadata(action: Any) -> dict[str, Any]:
    metadata = ADD_FRIEND_ROUTE_MANIFEST.get(normalize_action_name(action))
    return dict(metadata) if isinstance(metadata, dict) else {}


def add_friend_route_kind(action: Any) -> str:
    return str(add_friend_route_metadata(action).get("kind") or "")


def is_add_friend_main_route(action: Any) -> bool:
    return bool(add_friend_route_metadata(action).get("official_main"))


def is_add_friend_diagnostic_route(action: Any) -> bool:
    return bool(add_friend_route_metadata(action).get("diagnostic"))


def is_add_friend_legacy_route(action: Any) -> bool:
    return bool(add_friend_route_metadata(action).get("legacy"))


def add_friend_route_accepts_query(action: Any) -> bool:
    return bool(add_friend_route_metadata(action).get("accepts_query"))


def add_friend_route_accepts_formal_fields(action: Any) -> bool:
    return bool(add_friend_route_metadata(action).get("accepts_formal_fields"))


def add_friend_route_uses_passive_probe(action: Any) -> bool:
    return bool(add_friend_route_metadata(action).get("passive_probe"))
