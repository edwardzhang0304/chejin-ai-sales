"""Read-only PE-01 adapter for the existing desktop diagnostic Collector."""

import json
from pathlib import Path


def collect(collector, archive):
    root = collector.root / "artifacts" / "performance"
    index_path = root / "index.json"
    if not root.exists():
        collector.record(
            "performance",
            "missing",
            reason="no_performance_capture_old_version_or_not_started",
        )
        return
    if not collector.source_allowed(index_path, collector.root):
        collector.record("performance", "omitted", reason="source_boundary")
        return
    try:
        with index_path.open("rb") as stream:
            raw = stream.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("index_limit")
        index = json.loads(raw)
        segments = index["segments"]
        if (
            not isinstance(segments, list)
            or len(segments) > 5000
            or not all(isinstance(m, dict) for m in segments)
            or not isinstance(index.get("sessions"), dict)
            or not all(isinstance(s, dict) for s in index["sessions"].values())
            or not isinstance(index.get("events"), list)
            or not all(isinstance(e, dict) for e in index["events"])
            or not isinstance(index.get("gaps"), list)
            or not all(isinstance(g, dict) for g in index["gaps"])
            or not all(isinstance(m.get("last"), (int, float)) for m in segments)
            or not all(isinstance(e.get("session"), str) for e in index["events"])
        ):
            raise ValueError("index_schema")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        collector.record(
            "performance",
            "read_failed",
            reason="index_missing_or_corrupt",
            error_type=type(exc).__name__,
        )
        # Preserve available segments with bounded normal collector semantics;
        # do not claim completeness when index/ownership is lost.
        collector.tree(root, "performance/unindexed", archive)
        return
    collector.file(index_path, "performance/index.json", archive)
    if (root / "capture-gap.json").exists():
        collector.file(
            root / "capture-gap.json", "performance/capture-gap.json", archive
        )
        collector.record(
            "performance", "read_failed", reason="sampler_reported_capture_gap"
        )
    sessions = index.get("sessions", {})
    anomalous = {e.get("session") for e in index.get("events", [])}
    # Prior abnormal launches survive rapid restarts. Then newest tail/startup.
    segments = sorted(
        segments,
        key=lambda m: (m.get("session") in anomalous, m.get("last", 0)),
        reverse=True,
    )
    for meta in segments:
        name = meta.get("name", "")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.endswith(".jsonl")
        ):
            collector.record("performance", "omitted", reason="invalid_segment_name")
            continue
        if collector.limited():
            collector.record(
                "performance/" + name,
                "omitted",
                reason="export_time_or_file_budget",
                first=meta.get("first"),
                last=meta.get("last"),
                rows=meta.get("rows"),
            )
            continue
        collector.visited += 1
        collector.file(root / name, "performance/" + name, archive)
        if "performance/" + name not in collector.members:
            collector.record(
                "performance/" + name,
                "omitted",
                reason="segment_not_exported",
                first=meta.get("first"),
                last=meta.get("last"),
                rows=meta.get("rows"),
            )
    if index.get("retired"):
        collector.record(
            "performance",
            "retired",
            reason="retention_or_capacity_policy",
            segments=index["retired"],
            last_range=index.get("retired_last_range"),
        )
    for gap in index.get("gaps", []):
        collector.record("performance", "omitted", reason="capture_gap", details=gap)
    for sid, session in sessions.items():
        collector.record(
            "performance/session/" + sid,
            "coverage",
            status_at_last_write=session.get("status"),
            note="running_marker_does_not_prove_process_is_live; missing_tail_is_unknown",
        )
