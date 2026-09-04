from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SAFE_TEXT_SUFFIXES = {".json", ".jsonl", ".log", ".txt"}
FORBIDDEN_SUFFIXES = {
    ".db", ".db3", ".sqlite", ".sqlite3", ".env", ".png", ".jpg",
    ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff",
}
SECRET_KEYS = re.compile(
    r"(^|_)(worker_token|send_token|access_token|refresh_token|cookie|password|"
    r"secret|one_time_token|api_key|model_key|vision_key|feishu_app_secret|authorization)($|_)",
    re.IGNORECASE,
)
PRIVATE_TEXT_KEYS = re.compile(
    r"(^|_)(content|text|reply_text|transcript|ocr_text|raw_text|title_text|preview|"
    r"display_name|customer_name|sales_name|phone|mobile|email)($|_)",
    re.IGNORECASE,
)
PATH_KEYS = re.compile(
    r"(^|_)(screenshot|image|artifact|evidence|review|clipboard)(_path)?$",
    re.IGNORECASE,
)
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ID_KEYS = {
    "task_id", "conversation_id", "batch_id", "reply_action_id", "trace_id",
    "sidecar_run_id", "process_run_id", "stage_run_id", "read_run_id",
}


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone is required")
    return parsed.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def redacted_summary(value: Any) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "redacted": True,
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "length": len(encoded),
    }


def redact(value: Any, *, key: str = "") -> Any:
    if SECRET_KEYS.search(key):
        return "[REDACTED_SECRET]"
    if PRIVATE_TEXT_KEYS.search(key):
        return redacted_summary(value)
    if PATH_KEYS.search(key) and isinstance(value, str):
        return {"redacted_path": True, "name": Path(value).name}
    if isinstance(value, dict):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, key=key) for item in value]
    if isinstance(value, str):
        return EMAIL_RE.sub(
            "[REDACTED_EMAIL]", PHONE_RE.sub("[REDACTED_PHONE]", value)
        )
    return value


def connect_read_only(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, timeout=1.0
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def has_table(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def rows(
    connection: sqlite3.Connection | None,
    table: str,
    columns: str,
    where: str = "",
    parameters: Iterable[Any] = (),
    order_by: str = "",
) -> list[dict[str, Any]]:
    if connection is None or not has_table(connection, table):
        return []
    query = f"SELECT {columns} FROM {table}"
    if where:
        query += f" WHERE {where}"
    if order_by:
        query += f" ORDER BY {order_by}"
    return [dict(row) for row in connection.execute(query, tuple(parameters))]


def decode_json_fields(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key, value in list(result.items()):
        if not isinstance(value, str) or not key.endswith(("_json", "metadata", "value")):
            continue
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            continue
        if key.endswith("_json"):
            result.pop(key, None)
            result[key[:-5]] = decoded
        else:
            result[key] = decoded
    return result


def collect_ids(value: Any, output: dict[str, set[str]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ID_KEYS and item not in (None, ""):
                output.setdefault(key, set()).add(str(item))
            collect_ids(item, output)
    elif isinstance(value, list):
        for item in value:
            collect_ids(item, output)


def sidecar_projection(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    for row in logs:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        found: dict[str, set[str]] = {}
        collect_ids(metadata, found)
        run_ids = found.get("sidecar_run_id", set())
        direct = str(metadata.get("sidecar_run_id") or "")
        if direct:
            run_ids.add(direct)
        for run_id in run_ids:
            call = calls.setdefault(
                run_id,
                {
                    "sidecar_run_id": run_id,
                    "started_at": row.get("created_at"),
                    "ended_at": row.get("created_at"),
                    "records": [],
                },
            )
            call["started_at"] = min(
                str(call["started_at"]), str(row.get("created_at") or "")
            )
            call["ended_at"] = max(
                str(call["ended_at"]), str(row.get("created_at") or "")
            )
            call["records"].append(row)
    return list(calls.values())


def inside_window(path: Path, start: datetime, end: datetime) -> bool:
    changed = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return start <= changed <= end


def safe_text_payload(path: Path) -> Any:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".json":
        try:
            return redact(json.loads(text))
        except json.JSONDecodeError:
            return redact(text)
    if path.suffix.lower() == ".jsonl":
        result = []
        for line in text.splitlines():
            try:
                result.append(redact(json.loads(line)))
            except json.JSONDecodeError:
                result.append(redact(line))
        return result
    return redact(text)


def write_json(archive: zipfile.ZipFile, name: str, value: Any) -> None:
    archive.writestr(
        name,
        json.dumps(redact(value), ensure_ascii=False, indent=2, default=str),
    )


def update_evidence(root: Path, start: datetime, end: datetime) -> list[tuple[str, Any]]:
    """Only bounded diagnostic projections, never plans, tokens or snapshots of business rows."""
    names = ("worker-startup.jsonl", "updater-startup.jsonl", "update-result.json")
    fields = {
        "schema_version", "timestamp_epoch", "phase", "pid", "error_type", "error_code",
        "exception_type", "exit_code", "errno", "winerror", "frames", "state", "result_code",
        "failure_code", "update_request_id", "target_version", "artifact_sha256",
        "startup_diagnostic", "waiting_reason_code", "waiting_safety_snapshot",
        "child_pid", "reason", "exit_code_hex", "marker_error", "elapsed_ms", "health_timeout_seconds",
    }
    entries = []
    paths = [root / "update-state.json"]
    for name in names:
        paths.extend(root.glob(f"requests/*/control/{name}"))
    for path in paths:
        try:
            if (not path.is_file() or path.is_symlink()
                or not path.resolve().is_relative_to(root.resolve())
                or not inside_window(path, start, end)
                or path.stat().st_size > 2 * 1024 * 1024):
                continue
            payload = safe_text_payload(path)
            records = payload if isinstance(payload, list) else [payload]
            projected = [{k: v for k, v in item.items() if k in fields}
                         for item in records if isinstance(item, dict)]
            entries.append((path.relative_to(root).as_posix(), projected))
            if len(entries) >= 100:
                break
        except (OSError, ValueError):
            continue
    return entries


def build_identity(package_dir: Path) -> dict[str, Any]:
    manifest_path = package_dir / "fast-uat-manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if manifest_path.is_file()
        else {}
    )
    contract_path = package_dir / "app" / "contracts" / "c2_contract_v3.json"
    contract = (
        json.loads(contract_path.read_text(encoding="utf-8"))
        if contract_path.is_file()
        else {}
    )
    canonical = json.dumps(
        contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "worker_version": manifest.get("version"),
        "git_commit": manifest.get("git_commit"),
        "git_branch": manifest.get("git_branch"),
        "git_dirty": manifest.get("git_dirty"),
        "contract_version": contract.get("contract_version"),
        "contract_revision": contract.get("contract_revision"),
        "contract_sha256": hashlib.sha256(canonical).hexdigest() if contract else None,
        "omniauto_source": manifest.get("omniauto_source"),
    }


def collect(args: argparse.Namespace) -> Path:
    app_dir = Path(args.app_dir).resolve()
    package_dir = Path(args.package_dir).resolve()
    output = Path(args.output).resolve()
    start, end = parse_time(args.from_iso), parse_time(args.to_iso)
    if end <= start:
        raise ValueError("INVALID_TIME_WINDOW")
    start_iso, end_iso = iso(start), iso(end)
    update_dir = Path(getattr(args, "update_dir", None)
                      or os.environ.get("CHEJIN_UPDATE_STAGING_ROOT")
                      or app_dir.parent / "CheJinWorkerUpdate").resolve()
    update_files = update_evidence(update_dir, start, end)
    time_where = (
        "(created_at >= ? AND created_at <= ?) OR "
        "(updated_at >= ? AND updated_at <= ?)"
    )
    time_args = (start_iso, end_iso, start_iso, end_iso)

    main_db = connect_read_only(app_dir / "worker_client.sqlite3")
    telemetry_db = connect_read_only(app_dir / "worker_telemetry.sqlite3")
    binding = rows(
        main_db, "binding",
        "worker_id, client_instance_id, run_status, bound_at, updated_at",
        order_by="id",
    )
    logs = [
        decode_json_fields(row)
        for row in rows(
            main_db, "local_logs",
            "id, created_at, level, event, task_id, error_code, message, metadata",
            "created_at >= ? AND created_at <= ?", (start_iso, end_iso), "created_at",
        )
    ]
    ledger = [
        decode_json_fields(row)
        for row in rows(
            main_db, "c2_message_ledger", "*",
            "(first_seen_at >= ? AND first_seen_at <= ?) OR "
            "(updated_at >= ? AND updated_at <= ?)",
            time_args, "updated_at",
        )
    ]
    action_journal = [
        decode_json_fields(row)
        for row in rows(main_db, "c2_action_journal", "*", time_where, time_args, "updated_at")
    ]
    c2_outbox = [
        decode_json_fields(row)
        for row in rows(main_db, "c2_ingest_outbox", "*", time_where, time_args, "updated_at")
    ]
    sent_ack = [
        decode_json_fields(row)
        for row in rows(main_db, "reply_send_ack_outbox", "*", time_where, time_args, "updated_at")
    ]
    runtime_state = [
        decode_json_fields(row)
        for row in rows(
            main_db, "c2_runtime_state", "key, value, updated_at",
            "updated_at >= ? AND updated_at <= ?", (start_iso, end_iso), "updated_at",
        )
    ]
    telemetry_rows = [
        decode_json_fields(row)
        for row in rows(
            telemetry_db, "telemetry_stage_events",
            "*",
            "created_at >= ? AND created_at <= ?", (start_iso, end_iso), "created_at",
        )
    ]
    telemetry = [
        row
        for row in telemetry_rows
        if str(row.get("delivery_state") or "pending") == "pending"
    ]
    telemetry_quarantine = [
        row
        for row in telemetry_rows
        if str(row.get("delivery_state") or "") == "quarantined"
    ]
    backend_authority_snapshots = [
        decode_json_fields(row).get("report")
        for row in rows(
            telemetry_db,
            "telemetry_authority_snapshots",
            "process_run_id, report_json, updated_at",
            "updated_at >= ? AND updated_at <= ?",
            (start_iso, end_iso),
            "updated_at",
        )
    ]
    backend_authority_snapshots = [
        item for item in backend_authority_snapshots if isinstance(item, dict)
    ]
    process_links = rows(
        telemetry_db, "telemetry_process_links", "*",
        "created_at >= ? AND created_at <= ?", (start_iso, end_iso), "created_at",
    )
    if main_db is not None:
        main_db.close()
    if telemetry_db is not None:
        telemetry_db.close()

    related: dict[str, set[str]] = {}
    for value in (
        logs, ledger, action_journal, c2_outbox, sent_ack, runtime_state,
        telemetry, telemetry_quarantine, backend_authority_snapshots,
        process_links,
    ):
        collect_ids(value, related)
    related_ids = {key: sorted(values) for key, values in related.items()}
    sidecar_ids = set(related_ids.get("sidecar_run_id", []))

    file_journals = []
    action_root = app_dir / "transactions" / "actions"
    if action_root.is_dir():
        for path in action_root.rglob("*.json"):
            if inside_window(path, start, end):
                file_journals.append(
                    {"name": path.name, "payload": safe_text_payload(path)}
                )

    artifact_files: list[tuple[str, Any]] = []
    for root_name in ("artifacts", "diagnostics"):
        root = app_dir / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if (
                not path.is_file()
                or path.suffix.lower() not in SAFE_TEXT_SUFFIXES
                or path.suffix.lower() in FORBIDDEN_SUFFIXES
                or not inside_window(path, start, end)
                or path.stat().st_size > 5 * 1024 * 1024
            ):
                continue
            is_startup_crash = root_name == "diagnostics" and path.name == "startup-crash.jsonl"
            if sidecar_ids and not is_startup_crash and not any(run_id in str(path) for run_id in sidecar_ids):
                preview = path.read_text(encoding="utf-8", errors="replace")
                if not any(run_id in preview for run_id in sidecar_ids):
                    continue
            artifact_files.append(
                (
                    f"{root_name}/{path.relative_to(root).as_posix()}",
                    safe_text_payload(path),
                )
            )

    incidents: list[tuple[str, Any]] = []
    incident_root = app_dir / "incidents"
    if incident_root.is_dir():
        for path in incident_root.iterdir():
            if not path.is_file() or not inside_window(path, start, end):
                continue
            if path.suffix.lower() == ".zip":
                try:
                    with zipfile.ZipFile(path) as source:
                        for name in source.namelist():
                            suffix = Path(name).suffix.lower()
                            if suffix not in SAFE_TEXT_SUFFIXES or suffix in FORBIDDEN_SUFFIXES:
                                continue
                            raw = source.read(name).decode("utf-8", errors="replace")
                            try:
                                payload = json.loads(raw) if suffix == ".json" else raw
                            except json.JSONDecodeError:
                                payload = raw
                            incidents.append((f"{path.stem}/{name}", redact(payload)))
                except (OSError, zipfile.BadZipFile):
                    continue
            elif path.suffix.lower() in SAFE_TEXT_SUFFIXES:
                incidents.append((path.name, safe_text_payload(path)))

    identity = build_identity(package_dir)
    sidecar_calls = sidecar_projection(logs)
    manifest = {
        "schema_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "time_window": {"from": start_iso, "to": end_iso},
        "source_access": "sqlite_mode_ro_and_text_files_read_only",
        "build": identity,
        "worker": binding[0] if binding else None,
        "related_ids": related_ids,
        "counts": {
            "structured_logs": len(logs),
            "sidecar_calls": len(sidecar_calls),
            "ledger": len(ledger),
            "action_journal": len(action_journal) + len(file_journals),
            "c2_outbox": len(c2_outbox),
            "sent_ack": len(sent_ack),
            "pending_standard_stage_uploads": len(telemetry),
            "quarantined_standard_stage_uploads": len(
                telemetry_quarantine
            ),
            "backend_authority_snapshots": len(
                backend_authority_snapshots
            ),
            "incident_text_entries": len(incidents),
            "update_diagnostic_files": len(update_files),
        },
        "forbidden_content_excluded": [
            "worker_client.sqlite3", ".env", "tokens", "cookies", "model_keys",
            "feishu_credentials", "raw_customer_images", "chat_screenshots",
        ],
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        write_json(archive, "manifest.json", manifest)
        write_json(archive, "build/identity.json", identity)
        write_json(archive, "logs/structured_logs.json", logs)
        write_json(archive, "ids/related_ids.json", related_ids)
        write_json(archive, "sidecar/calls.json", sidecar_calls)
        write_json(
            archive,
            "telemetry/authority.json",
            {
                "schema_version": 1,
                "operational_authority": "backend.process_stage_runs",
                "query_api": "/api/observability/process-runs/{process_run_id}",
                "local_data_role": (
                    "bounded_pending_upload_quarantine_and_"
                    "backend_authority_snapshot_only"
                ),
                "pending_upload_count": len(telemetry),
                "quarantined_upload_count": len(telemetry_quarantine),
                "backend_authority_snapshot_count": len(
                    backend_authority_snapshots
                ),
                "legacy_timing_reports_emitted": False,
            },
        )
        write_json(archive, "telemetry/pending_stage_uploads.json", telemetry)
        write_json(
            archive,
            "telemetry/quarantined_stage_uploads.json",
            telemetry_quarantine,
        )
        write_json(
            archive,
            "telemetry/backend_authority_snapshots.json",
            backend_authority_snapshots,
        )
        write_json(archive, "telemetry/process_links.json", process_links)
        write_json(archive, "state/binding.json", binding)
        write_json(archive, "state/ledger.json", ledger)
        write_json(archive, "state/action_journal.json", action_journal)
        write_json(archive, "state/action_journal_files.json", file_journals)
        write_json(archive, "state/c2_outbox.json", c2_outbox)
        write_json(archive, "state/sent_ack.json", sent_ack)
        write_json(archive, "state/runtime_state.json", runtime_state)
        for name, payload in artifact_files:
            write_json(archive, f"sidecar/artifacts/{name}.redacted.json", payload)
        for name, payload in incidents:
            write_json(archive, f"incidents/{name}.redacted.json", payload)
        for name, payload in update_files:
            write_json(archive, f"update/{name}.redacted.json", payload)
    temporary.replace(output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--package-dir", required=True)
    parser.add_argument("--update-dir")
    parser.add_argument("--from-iso", required=True)
    parser.add_argument("--to-iso", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        path = collect(args)
    except Exception as exc:
        print(
            f"EVIDENCE_EXPORT_FAILED:{type(exc).__name__}:{exc}",
            file=sys.stderr,
        )
        return 1
    print(str(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
