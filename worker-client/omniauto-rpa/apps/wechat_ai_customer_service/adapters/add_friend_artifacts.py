"""Artifact layout contract for add_friend RPA runs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.adapters.add_friend_routes import (
    ADD_FRIEND_MAIN_ROUTE,
    ADD_FRIEND_WINDOWS_ROUTE,
)


ADD_FRIEND_RUNTIME_DIR = "runtime"
ADD_FRIEND_LATEST_DIR = "latest"
ADD_FRIEND_WINDOWS_ARTIFACT_SCOPE = "add_friend_entry_click_plan_windows"
ADD_FRIEND_ARTIFACT_SCOPES: dict[str, str] = {
    ADD_FRIEND_MAIN_ROUTE: ADD_FRIEND_WINDOWS_ARTIFACT_SCOPE,
    ADD_FRIEND_WINDOWS_ROUTE: ADD_FRIEND_WINDOWS_ARTIFACT_SCOPE,
}

ADD_FRIEND_ENTRY_CLICK_PLAN_JSON = "add_friend_entry_click_plan.json"
ADD_FRIEND_ENTRY_CLICK_REVIEW_JSON = "add_friend_entry_click_review.json"
ADD_FRIEND_ENTRY_CLICK_REVIEW_HTML = "add_friend_entry_click_review.html"
ADD_FRIEND_ENTRY_CLICK_STDOUT_JSON = "add_friend_entry_click_plan_stdout.json"
ADD_FRIEND_ENTRY_CLICK_STDERR_LOG = "add_friend_entry_click_plan_stderr.log"


def add_friend_artifact_scope(route: str) -> str:
    return ADD_FRIEND_ARTIFACT_SCOPES.get(str(route or "").strip().lower(), "add_friend")


def add_friend_artifact_root(project_root: str | Path) -> Path:
    return Path(project_root) / ADD_FRIEND_RUNTIME_DIR


def add_friend_route_artifact_root(project_root: str | Path, route: str) -> Path:
    return add_friend_artifact_root(project_root) / add_friend_artifact_scope(route)


def add_friend_timestamp_id(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d_%H%M%S")


def add_friend_timestamp_run_dir(
    project_root: str | Path,
    route: str,
    *,
    timestamp: str | None = None,
) -> Path:
    return add_friend_route_artifact_root(project_root, route) / (timestamp or add_friend_timestamp_id())


def add_friend_latest_dir(project_root: str | Path, route: str) -> Path:
    return add_friend_route_artifact_root(project_root, route) / ADD_FRIEND_LATEST_DIR


def add_friend_entry_click_artifact_paths(artifact_dir: str | Path) -> dict[str, str]:
    root = Path(artifact_dir)
    return {
        "plan_json": str(root / ADD_FRIEND_ENTRY_CLICK_PLAN_JSON),
        "review_json": str(root / ADD_FRIEND_ENTRY_CLICK_REVIEW_JSON),
        "review_html": str(root / ADD_FRIEND_ENTRY_CLICK_REVIEW_HTML),
        "stdout_json": str(root / ADD_FRIEND_ENTRY_CLICK_STDOUT_JSON),
        "stderr_log": str(root / ADD_FRIEND_ENTRY_CLICK_STDERR_LOG),
    }


def add_friend_artifact_manifest(project_root: str | Path, route: str) -> dict[str, Any]:
    return {
        "route": str(route or "").strip().lower(),
        "scope": add_friend_artifact_scope(route),
        "route_root": str(add_friend_route_artifact_root(project_root, route)),
        "latest_dir": str(add_friend_latest_dir(project_root, route)),
        "runtime_dir": ADD_FRIEND_RUNTIME_DIR,
    }
