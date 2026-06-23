"""Execution context helpers for add_friend RPA flows."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from apps.wechat_ai_customer_service.adapters.add_friend_artifacts import (
    ADD_FRIEND_ENTRY_CLICK_PLAN_JSON,
    add_friend_route_artifact_root,
)
from apps.wechat_ai_customer_service.adapters.add_friend_diagnostics import StepEventRecorder
from apps.wechat_ai_customer_service.adapters.add_friend_flow_events import add_friend_entry_click_events_from_payload
from apps.wechat_ai_customer_service.adapters.add_friend_routes import ADD_FRIEND_MAIN_ROUTE


class AddFriendFlowContext:
    """Shared per-run context for add_friend flow orchestration."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        route: str = ADD_FRIEND_MAIN_ROUTE,
        artifact_dir: str | Path | None = None,
        plan_filename: str = ADD_FRIEND_ENTRY_CLICK_PLAN_JSON,
    ) -> None:
        self.route = str(route or ADD_FRIEND_MAIN_ROUTE)
        self.output_dir = Path(artifact_dir) if artifact_dir else add_friend_route_artifact_root(project_root, self.route)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.plan_path = self.output_dir / plan_filename
        self.recorder = StepEventRecorder()
        self.timings: list[dict[str, Any]] = []
        self.started_at = time.perf_counter()

    def add_event(self, **kwargs: Any) -> dict[str, Any]:
        return self.recorder.add(**kwargs)

    def add_events(self, events: list[dict[str, Any]] | None) -> None:
        self.recorder.extend(events)

    def add_timing(
        self,
        name: str,
        *,
        seconds: int | float | None = None,
        started_at: int | float | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        elapsed = seconds
        if elapsed is None and started_at is not None:
            elapsed = time.perf_counter() - float(started_at)
        item = {"name": str(name or ""), "seconds": round(float(elapsed or 0), 3), **extra}
        self.timings.append(item)
        return item

    def add_flow_total_timing(self, name: str = "flow_total") -> dict[str, Any]:
        return self.add_timing(name, started_at=self.started_at)

    def build_events(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return add_friend_entry_click_events_from_payload(
            payload,
            existing_events=self.recorder.to_list(),
        )

    def finalize_payload(
        self,
        payload: dict[str, Any],
        *,
        report_writer: Callable[[Path, dict[str, Any]], str],
        write_plan: bool = True,
    ) -> dict[str, Any]:
        payload.setdefault("timings", list(self.timings))
        payload["native_diagnostic_events"] = self.recorder.to_list()
        payload["diagnostic_events"] = self.build_events(payload)
        payload["review_path"] = report_writer(self.output_dir, payload)
        if write_plan:
            self.plan_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
