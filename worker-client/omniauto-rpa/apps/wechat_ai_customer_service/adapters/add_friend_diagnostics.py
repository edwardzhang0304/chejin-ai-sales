"""Diagnostic event model and report generation for add_friend RPA."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


STEP_EVENT_FIELDS = (
    "step_id",
    "title",
    "status",
    "state_before",
    "state_after",
    "ocr_items",
    "targets",
    "selected_target",
    "artifacts",
    "timing_ms",
    "result",
)


def make_step_event(
    *,
    step_id: str,
    title: str,
    status: str = "unknown",
    state_before: str = "",
    state_after: str = "",
    ocr_items: list[dict[str, Any]] | None = None,
    targets: list[dict[str, Any]] | None = None,
    selected_target: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    timing_ms: int | float | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "step_id": str(step_id or ""),
        "title": str(title or ""),
        "status": normalize_step_status(status),
        "state_before": str(state_before or ""),
        "state_after": str(state_after or ""),
        "ocr_items": normalize_event_items(ocr_items or []),
        "targets": normalize_event_items(targets or []),
        "selected_target": selected_target if isinstance(selected_target, dict) else {},
        "artifacts": artifacts if isinstance(artifacts, dict) else {},
        "timing_ms": int(round(float(timing_ms or 0))),
        "result": result if isinstance(result, dict) else {},
    }


def normalize_step_status(status: str) -> str:
    clean = str(status or "").strip().lower()
    if clean in {"pending", "running", "completed", "failed", "skipped", "warning"}:
        return clean
    if clean in {"ok", "success", "passed", "pass"}:
        return "completed"
    if clean in {"fail", "error"}:
        return "failed"
    return "unknown"


class StepEventRecorder:
    """Small append-only recorder for flow-native add_friend diagnostics."""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    def add(
        self,
        *,
        step_id: str,
        title: str,
        status: str = "unknown",
        state_before: str = "",
        state_after: str = "",
        ocr_items: list[dict[str, Any]] | None = None,
        targets: list[dict[str, Any]] | None = None,
        selected_target: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
        timing_ms: int | float | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = make_step_event(
            step_id=step_id,
            title=title,
            status=status,
            state_before=state_before,
            state_after=state_after,
            ocr_items=ocr_items,
            targets=targets,
            selected_target=selected_target,
            artifacts=artifacts,
            timing_ms=timing_ms,
            result=result,
        )
        self._events.append(event)
        return event

    def extend(self, events: list[dict[str, Any]] | None) -> None:
        for event in events or []:
            if isinstance(event, dict):
                self._events.append(normalize_step_event(event))

    def to_list(self) -> list[dict[str, Any]]:
        return [normalize_step_event(event) for event in self._events]


def normalize_event_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized.append(dict(item))
    return normalized


def step_status_from_result(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return "unknown"
    if result.get("skipped"):
        return "skipped"
    task_status = str(result.get("task_status") or "").strip().lower()
    if task_status == "failed" or result.get("error_code"):
        return "failed"
    if task_status == "completed" or result.get("ok") is True or result.get("clicked") is True:
        return "completed"
    if result.get("ok") is False:
        return "failed"
    return "unknown"


def selected_target_from_targets(targets: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
    if isinstance(result.get("selected_target"), dict):
        return dict(result["selected_target"])
    if isinstance(result.get("target"), dict):
        return dict(result["target"])
    for target in targets:
        if isinstance(target, dict):
            return dict(target)
    return {}


def step_event_from_review_row(row: dict[str, Any], index: int) -> dict[str, Any]:
    detection = row.get("detection") if isinstance(row.get("detection"), dict) else {}
    targets = row.get("targets") if isinstance(row.get("targets"), list) else []
    raw = row.get("raw") or ""
    annotated = row.get("annotated") or ""
    artifacts = {
        "raw": str(raw) if raw else "",
        "annotated": str(annotated) if annotated else "",
    }
    state_before = str(detection.get("state_before") or detection.get("previous_state") or "")
    state_after = str(detection.get("state_after") or detection.get("state") or "")
    return make_step_event(
        step_id=f"step_{index:02d}",
        title=str(row.get("title") or f"Step {index:02d}"),
        status=step_status_from_result(detection),
        state_before=state_before,
        state_after=state_after,
        ocr_items=detection.get("ocr_items") if isinstance(detection.get("ocr_items"), list) else [],
        targets=targets,
        selected_target=selected_target_from_targets(targets, detection),
        artifacts=artifacts,
        timing_ms=detection.get("timing_ms") if isinstance(detection.get("timing_ms"), (int, float)) else 0,
        result=detection,
    )


def step_events_from_review_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [step_event_from_review_row(row, index) for index, row in enumerate(rows, start=1) if isinstance(row, dict)]


def write_step_event_report(
    *,
    output_dir: Path,
    json_name: str,
    html_name: str,
    title: str,
    description: str,
    summary: dict[str, Any],
    events: list[dict[str, Any]],
) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_events = [normalize_step_event(event) for event in events]
    report = {
        "schema": "add_friend.step_events.v1",
        "summary": dict(summary),
        "events": normalized_events,
    }
    json_path = output_dir / json_name
    html_path = output_dir / html_name
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(render_step_event_html(title=title, description=description, report=report), encoding="utf-8")
    return str(html_path)


def normalize_step_event(event: dict[str, Any]) -> dict[str, Any]:
    clean = {field: event.get(field) for field in STEP_EVENT_FIELDS}
    return make_step_event(
        step_id=str(clean.get("step_id") or ""),
        title=str(clean.get("title") or ""),
        status=str(clean.get("status") or "unknown"),
        state_before=str(clean.get("state_before") or ""),
        state_after=str(clean.get("state_after") or ""),
        ocr_items=clean.get("ocr_items") if isinstance(clean.get("ocr_items"), list) else [],
        targets=clean.get("targets") if isinstance(clean.get("targets"), list) else [],
        selected_target=clean.get("selected_target") if isinstance(clean.get("selected_target"), dict) else {},
        artifacts=clean.get("artifacts") if isinstance(clean.get("artifacts"), dict) else {},
        timing_ms=clean.get("timing_ms") if isinstance(clean.get("timing_ms"), (int, float)) else 0,
        result=clean.get("result") if isinstance(clean.get("result"), dict) else {},
    )


def render_step_event_html(*, title: str, description: str, report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    events = report.get("events") if isinstance(report.get("events"), list) else []
    event_html = []
    for event in events:
        if not isinstance(event, dict):
            continue
        artifacts = event.get("artifacts") if isinstance(event.get("artifacts"), dict) else {}
        annotated = local_artifact_name(artifacts.get("annotated"))
        raw = local_artifact_name(artifacts.get("raw"))
        fallback_warning = event_has_fallback_warning(event)
        section_class = ' class="fallback-warning"' if fallback_warning else ""
        warning_html = (
            '<p class="warning"><b>注意：</b>本步骤触发固定兜底定位，请优先检查 OCR/视觉定位为什么没有命中。</p>'
            if fallback_warning
            else ""
        )
        image_html = f'<img src="{annotated}" alt="{html.escape(str(event.get("title") or ""))} annotated">' if annotated else "<p>无标注图</p>"
        raw_link = f'<a href="{raw}">原始截图</a>' if raw else "无原始截图"
        event_html.append(
            "\n".join(
                [
                    f"<section{section_class}>",
                    f"<h2>{html.escape(str(event.get('step_id') or ''))} · {html.escape(str(event.get('title') or ''))}</h2>",
                    warning_html,
                    f"<p><b>状态：</b>{html.escape(str(event.get('status') or 'unknown'))}</p>",
                    f"<p><b>状态变化：</b>{html.escape(str(event.get('state_before') or ''))} → {html.escape(str(event.get('state_after') or ''))}</p>",
                    f"<p><b>耗时：</b>{html.escape(str(event.get('timing_ms') or 0))} ms</p>",
                    f"<p>{raw_link}</p>",
                    image_html,
                    "<details open><summary>结果</summary>",
                    f"<pre>{html.escape(json.dumps(event.get('result') or {}, ensure_ascii=False, indent=2))}</pre>",
                    "</details>",
                    "<details><summary>目标与选择</summary>",
                    f"<pre>{html.escape(json.dumps({'targets': event.get('targets') or [], 'selected_target': event.get('selected_target') or {}}, ensure_ascii=False, indent=2))}</pre>",
                    "</details>",
                    "<details><summary>OCR Items</summary>",
                    f"<pre>{html.escape(json.dumps(event.get('ocr_items') or [], ensure_ascii=False, indent=2))}</pre>",
                    "</details>",
                    "</section>",
                ]
            )
        )
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh-CN">',
            "<head>",
            '<meta charset="utf-8">',
            f"<title>{html.escape(title)}</title>",
            "<style>",
            "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;background:#f6f7f9;color:#111827}",
            "h1{font-size:24px;margin:0 0 12px}",
            "section{background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:16px;margin:16px 0}",
            "section.fallback-warning{border-color:#dc2626;background:#fff7f7}",
            ".warning{color:#b91c1c;background:#fee2e2;border:1px solid #fecaca;border-radius:6px;padding:8px 10px}",
            "img{display:block;max-width:100%;height:auto;border:1px solid #d8dee8;background:#fff}",
            "pre{white-space:pre-wrap;background:#f3f4f6;border-radius:6px;padding:10px}",
            "a{color:#2563eb}",
            "</style>",
            "</head>",
            "<body>",
            f"<h1>{html.escape(title)}</h1>",
            f"<p>{html.escape(description)}</p>",
            "<section><h2>Summary</h2>",
            f"<pre>{html.escape(json.dumps(summary, ensure_ascii=False, indent=2))}</pre>",
            "</section>",
            *event_html,
            "</body></html>",
        ]
    )


def event_has_fallback_warning(event: dict[str, Any]) -> bool:
    selected = event.get("selected_target") if isinstance(event.get("selected_target"), dict) else {}
    if selected.get("fallback_used"):
        return True
    targets = event.get("targets") if isinstance(event.get("targets"), list) else []
    for target in targets:
        if isinstance(target, dict) and target.get("fallback_used"):
            return True
    return False


def local_artifact_name(path_value: Any) -> str:
    if not path_value:
        return ""
    return html.escape(Path(str(path_value)).name)
