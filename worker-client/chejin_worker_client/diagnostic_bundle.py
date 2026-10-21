"""Bounded, read-only support collection shared by the UI and rescue entry.

No runtime/configuration imports, business APIs, schema initialization or recovery
actions are permitted here. A damaged client database must not prevent file export.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time
import uuid
import zipfile
from datetime import datetime, timezone

from .evidence_paths import artifacts, incident_roots, update_root
from .incident_evidence import _sanitize, _SECRET_KEY_RE, _FORBIDDEN_FILE_RE
from . import __version__

MAX_BYTES = 100 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILES = 3000
MAX_ROWS = 1000
MAX_SECONDS = 45
TABLES = (
    "local_logs", "hook_raw_inbox", "hook_observation_projection",
    "hook_delivery_outbox", "hook_voice_attempts", "hook_active_calls",
    "hook_history_pages", "hook_retired_observations", "hook_retired_deliveries",
    "c2_runtime_state", "c2_message_ledger", "c2_ingest_outbox",
    "c2_action_journal", "reply_send_ack_outbox",
)
TEXT_SUFFIXES = {".json", ".jsonl", ".log", ".txt"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
BODY_KEYS = {"text", "content", "msg", "message_text", "reply_text", "transcript",
             "strcontent", "raw_body", "audio", "audio_hex", "voice_hex", "voice", "base64"}


UPDATE_DIAGNOSTICS = {
    "update-state.json", "update-result.json", "worker-startup.jsonl",
    "worker-shutdown-failed.json", "updater-startup.jsonl",
    "updater-recovery-startup.jsonl", "updater-ready.json", "healthy.json",
    "missing-result-recovery-ready.json", "updater.jsonl", "updater.log",
}
DATABASE_SECONDS = 2


class ExportWriteError(Exception):
    """Destination I/O failure, distinct from an optional source read failure."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def build_identity():
    result = {"version": __version__}
    path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "runtime-build-identity.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        result.update({k: value[k] for k in ("version", "git_commit", "transport")
                       if isinstance(value.get(k), str)})
    except (OSError, ValueError, AttributeError):
        result["build_identity_unavailable"] = True
    return result

def _content_safe(value, *, ocr=False):
    """Retain correlation IDs and diagnostic codes, omit conversation/audio bodies."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k.lower() == "raw_utf8" and isinstance(v, str):
                # Native capture envelopes contain another JSON document. Decode
                # before scrubbing so IDs survive but message bodies do not.
                try:
                    out[k] = _content_safe(json.loads(v), ocr=False)
                except (ValueError, RecursionError):
                    out[k] = {"omitted": "unparseable_native_response",
                              "bytes": len(v.encode("utf-8")),
                              "sha256": hashlib.sha256(v.encode("utf-8")).hexdigest()}
            elif k.lower() in BODY_KEYS and not (ocr and k.lower() == "text") and isinstance(v, (str, bytes)):
                raw = v.encode("utf-8") if isinstance(v, str) else v
                out[k] = {"omitted": "message_or_audio_body", "bytes": len(raw),
                          "sha256": hashlib.sha256(raw).hexdigest()}
                if isinstance(v, str) and "现在可以开始聊天了" in v:
                    out[k]["friend_added_notice"] = True
            else:
                out[k] = _content_safe(v, ocr=ocr)
        return out
    if isinstance(value, list):
        return [_content_safe(v, ocr=ocr) for v in value]
    if isinstance(value, bytes):
        return {"omitted": "binary", "bytes": len(value)}
    return value


class Collector:
    def __init__(self, root, destination, progress=None, *, max_bytes=MAX_BYTES, max_seconds=MAX_SECONDS):
        self.root = Path(root).resolve()
        self.destination = Path(destination)
        self.progress = progress or (lambda _value: None)
        self.max_bytes = max_bytes
        self.deadline = time.monotonic() + max_seconds
        self.entries = []
        self.secrets = {v for k, v in os.environ.items() if _SECRET_KEY_RE.search(k) and len(v) >= 6}
        self.bytes = 0
        self.visited = 0
        self.members = set()
        self.latest_logs = []
        self.source_deadline = self.deadline
        self.source_end = MAX_FILES

    def record(self, source, status, **details):
        self.entries.append({"source": str(source), "status": status, **details})

    def emit(self, percent, stage):
        self.progress({"percent": percent, "stage": stage})

    def limited(self):
        return time.monotonic() >= min(self.deadline, self.source_deadline) or self.visited >= min(MAX_FILES, self.source_end)

    def clean(self, value, *, ocr=False):
        return _sanitize(_content_safe(value, ocr=ocr), self.secrets)

    def text_bytes(self, data, suffix, *, ocr=False):
        text = data.decode("utf-8", errors="replace")
        if suffix == ".json":
            return _json(self.clean(json.loads(text), ocr=ocr))
        if suffix == ".jsonl":
            return b"\n".join(_json(self.clean(json.loads(line), ocr=ocr)).replace(b"\n", b" ")
                              for line in text.splitlines() if line.strip())
        return _sanitize(text, self.secrets).encode("utf-8")

    def add(self, archive, member, data, source):
        if member in self.members:
            return
        if self.bytes + len(data) > self.max_bytes:
            self.record(source, "omitted", reason="total_size_limit")
            return
        try:
            archive.writestr(member, data)
        except OSError as exc:
            raise ExportWriteError(type(exc).__name__) from exc
        self.bytes += len(data)
        self.members.add(member)
        self.record(source, "collected", archive_path=member, bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    collected_at=datetime.now(timezone.utc).isoformat())

    def source_allowed(self, path, allowed_root=None):
        """Reject linked descendants (including Windows junctions) before reading."""
        boundary = Path(allowed_root or self.root)
        path = Path(path)
        try:
            relative = path.relative_to(boundary)
            # Ancestors of an explicitly configured external update root count too.
            for ancestor in boundary.parents:
                info = ancestor.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    return False
            current = boundary
            for part in (None, *relative.parts):
                if part is not None:
                    current /= part
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    return False
            return path.resolve().is_relative_to(boundary.resolve())
        except (ValueError, OSError):
            return False

    def connection(self, name="worker_client.sqlite3"):
        path = self.root / name
        if not self.source_allowed(path):
            raise OSError("source_boundary")
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.25)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.set_progress_handler(lambda: int(time.monotonic() >= self.deadline), 1000)
            db.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        except BaseException:
            db.close()
            raise
        return db

    def read_secrets(self):
        # Only credential fields are read internally; neither table is exported.
        try:
            db = self.connection()
            try:
                names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "binding" in names:
                    for row in db.execute("SELECT worker_token FROM binding LIMIT 10"):
                        if row[0]:
                            self.secrets.add(str(row[0]))
                if "client_settings" in names:
                    def find(v):
                        if isinstance(v, dict):
                            for k, item in v.items():
                                if _SECRET_KEY_RE.search(k) and isinstance(item, str) and len(item) >= 6:
                                    self.secrets.add(item)
                                find(item)
                        elif isinstance(v, list):
                            for item in v:
                                find(item)
                    for row in db.execute("SELECT key,value FROM client_settings LIMIT 1000"):
                        try:
                            value = json.loads(row["value"])
                            if _SECRET_KEY_RE.search(row["key"]) and isinstance(value, str):
                                self.secrets.add(value)
                            find(value)
                        except (ValueError, TypeError):
                            pass
            finally:
                db.close()
        except (OSError, sqlite3.Error):
            self.record("database_credentials", "read_failed", reason="credential_values_unavailable")

    def database(self, archive, *, name="worker_client.sqlite3", tables=TABLES):
        if not (self.root / name).exists():
            self.record(name, "absent")
            return
        try:
            db = self.connection(name)
        except (OSError, sqlite3.Error) as exc:
            self.record(name, "read_failed", error_type=type(exc).__name__)
            return
        snapshots = []
        snapshot_at = datetime.now(timezone.utc).isoformat()
        snapshot_deadline = min(self.deadline, time.monotonic() + DATABASE_SECONDS)
        try:
            db.set_progress_handler(lambda: int(time.monotonic() >= snapshot_deadline), 1000)
            db.execute("BEGIN")
            db.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            for table in tables:
                if self.limited() or time.monotonic() >= snapshot_deadline:
                    self.record(table, "omitted", reason="collection_limit")
                    continue
                try:
                    columns = [r["name"] for r in db.execute(f'PRAGMA table_info("{table}")')]
                    if not columns:
                        self.record(table, "absent")
                        continue
                    # Never materialize raw callback/history/audio BLOBs.
                    selected = [c for c in columns if c not in {"raw_body", "audio", "audio_hex"}]
                    expressions = []
                    for c in selected:
                        quoted = '"' + c.replace('"', '""') + '"'
                        expressions.append(f"CASE WHEN length({quoted}) > 65536 THEN "
                                           f"'[OMITTED: cell_size_limit]' ELSE {quoted} END AS {quoted}")
                    if "raw_body" in columns:
                        expressions.append('length(raw_body) AS raw_body_bytes')
                        if table == "hook_raw_inbox":
                            expressions.append("CASE WHEN length(raw_body)<=65536 THEN raw_body END AS diagnostic_callback")
                    rows = db.execute(f'SELECT {",".join(expressions)} FROM "{table}" ORDER BY rowid DESC LIMIT ?',
                                      (MAX_ROWS + 1,))
                    payload = []
                    table_bytes = 0
                    truncated = False
                    for row in rows:
                        if len(payload) >= MAX_ROWS or self.limited() or time.monotonic() >= snapshot_deadline:
                            truncated = True
                            break
                        record = dict(row)
                        callback = record.pop("diagnostic_callback", None)
                        if callback is not None:
                            try:
                                raw = json.loads(callback)
                                messages = raw.get("msglist", [])
                                if isinstance(messages, dict):
                                    messages = [messages]
                                record["callback_messages"] = []
                                for message in messages[:100] if isinstance(messages, list) else []:
                                    if not isinstance(message, dict):
                                        continue
                                    entry = {k: message[k] for k in (
                                        "msgtype", "msgsvrid", "createtime", "timestamp", "clientmsgid",
                                        "localid") if isinstance(message.get(k), (str, int, float))}
                                    text = message.get("msg")
                                    if isinstance(text, str):
                                        entry["message_bytes"] = len(text.encode("utf-8"))
                                        entry["message_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
                                        entry["friend_added_notice"] = "现在可以开始聊天了" in text
                                    record["callback_messages"].append(entry)
                            except (ValueError, TypeError, AttributeError):
                                record["callback_summary_unavailable"] = True
                        for k, v in record.items():
                            if v == "[OMITTED: cell_size_limit]":
                                self.record(table, "omitted", reason="cell_size_limit", field=k)
                            if isinstance(v, str) and (k.endswith("_json") or k in {"metadata", "value"}):
                                try:
                                    record[k] = json.loads(v)
                                except ValueError:
                                    record[k] = {"invalid_json": True}
                        cleaned = self.clean(record)
                        size = len(_json(cleaned))
                        if table_bytes + size > MAX_FILE_BYTES:
                            truncated = True
                            self.record(table, "omitted", reason="table_size_limit")
                            break
                        table_bytes += size
                        payload.append(cleaned)
                    rows.close()
                    data = _json({
                        "snapshot_at": snapshot_at, "database": name,
                        "consistent_database_snapshot": True,
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "rows": payload, "limit": MAX_ROWS, "truncated": truncated,
                        "raw_bodies_excluded": True,
                    })
                    snapshots.append((f"state/{table}.json", data, table))
                    if table == "local_logs":
                        self.latest_logs = payload
                        snapshots.append(("logs/latest_logs.json", _json(payload), table))
                    if truncated:
                        self.record(table, "omitted", reason="older_rows_limit")
                except (sqlite3.Error, ValueError) as exc:
                    self.record(table, "read_failed", error_type=type(exc).__name__)
        except sqlite3.Error as exc:
            self.record(name, "read_failed", error_type=type(exc).__name__)
        finally:
            db.close()  # Release the read snapshot before compression or file collection.
        for member, data, source in snapshots:
            self.add(archive, member, data, source)

    def priority_recovery(self, archive):
        """Find unresolved journals independently of age, then follow their index.

        This reads evidence only. Receipt presence is a collection hint, never
        a business settlement decision. Discovery and output remain bounded.
        """
        hook = self.root / "transactions" / "hook"
        deadline = min(self.deadline, time.monotonic() + 8)
        examined = 0

        def records(folder, filename=None):
            nonlocal examined
            stack = [folder]
            while stack:
                directory = stack.pop()
                if not directory.exists():
                    continue
                if not self.source_allowed(directory):
                    self.record(str(directory.relative_to(self.root)), "omitted", reason="source_boundary")
                    continue
                try:
                    with os.scandir(directory) as listing:
                        for entry in listing:
                            examined += 1
                            if examined > 12000 or time.monotonic() >= deadline:
                                self.record("priority_recovery", "omitted", reason="discovery_limit")
                                return
                            path = Path(entry.path)
                            if not self.source_allowed(path):
                                self.record(str(path.relative_to(self.root)), "omitted", reason="source_boundary")
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(path)
                            elif path.suffix == ".json" and (filename is None or path.name == filename):
                                try:
                                    with path.open("rb") as stream:
                                        raw = stream.read(MAX_FILE_BYTES + 1)
                                    if len(raw) > MAX_FILE_BYTES:
                                        self.record(str(path.relative_to(self.root)), "omitted", reason="file_size_limit")
                                        continue
                                    value = json.loads(raw)
                                    if isinstance(value, dict):
                                        yield path, value
                                except (OSError, ValueError, RecursionError) as exc:
                                    self.record(str(path.relative_to(self.root)), "read_failed", error_type=type(exc).__name__)
                except OSError as exc:
                    self.record(str(directory.relative_to(self.root)), "read_failed", error_type=type(exc).__name__)

        def collect(path):
            member = "receipts/" + path.relative_to(self.root / "transactions").as_posix()
            if member in self.members:
                return
            if not path.exists():
                self.record(member, "absent")
                return
            self.visited += 1
            if self.limited():
                self.record(member, "omitted", reason="priority_budget_limit")
                return
            self.file(path, member, archive)

        # A locally retained flow can outlive its ACK. Keep that journal too.
        flows = {p.stem for p, _ in records(hook / "flows")
                 if not self.source_allowed(hook / "finished" / p.name)}
        try:
            db = self.connection()
            try:
                row = db.execute("SELECT value FROM client_settings WHERE key='runtime_control_v1'").fetchone()
                control = json.loads(row[0]) if row else {}
                flow = control.get("inflight_flow_id") if isinstance(control, dict) else None
                if isinstance(flow, str) and len(flow) <= 128 and Path(flow).name == flow:
                    flows.add(flow)
            finally:
                db.close()
        except (OSError, sqlite3.Error, ValueError):
            self.record("priority_recovery/runtime_control", "read_failed", reason="current_flow_unavailable")
        actions = set()
        for path, intent in records(hook / "sends", "intent.json"):
            action = intent.get("reply_action_id")
            if not isinstance(action, str) or path.parent.name != action:
                continue
            receipt = hook / "send-receipts" / (action + ".json")
            # A malformed/unreadable receipt is not evidence that we may omit
            # the original. Keep every unacknowledged action before history.
            receipt_valid = False
            if self.source_allowed(receipt):
                try:
                    with receipt.open("rb") as stream:
                        data = stream.read(MAX_FILE_BYTES + 1)
                    value = json.loads(data) if len(data) <= MAX_FILE_BYTES else None
                    receipt_valid = isinstance(value, dict) and value.get("automatic_resend_allowed") is False
                except (OSError, ValueError, RecursionError):
                    pass
            if receipt_valid and intent.get("task_id") not in flows:
                continue
            actions.add(action)
            for name in ("permit.json", "intent.json", "trigger.json", "outcome.json"):
                collect(path.parent / name)
            collect(receipt)
            task = intent.get("task_id")
            if isinstance(task, str) and task in flows:
                collect(hook / "flows" / (task + ".json"))
        # Pick the latest complete capture per current action in each pool;
        # its references take priority over unrelated newer raw pages.
        for pool in (hook, hook / "history-reconcile"):
            latest = {}
            for path, index in records(pool / "history-artifact-index"):
                action = index.get("retained_reply_action_id")
                if action not in actions or not isinstance(index.get("files"), dict):
                    continue
                try:
                    stamp = path.stat().st_mtime
                except OSError as exc:
                    self.record(str(path.relative_to(self.root)), "read_failed", error_type=type(exc).__name__)
                    continue
                if action not in latest or stamp > latest[action][0]:
                    latest[action] = (stamp, path, index)
            for _, path, index in latest.values():
                collect(path)
                collect(pool / "history-receipts" / path.name)
                for relative in index["files"]:
                    ref = Path(relative)
                    if (ref.is_absolute() or len(ref.parts) != 2 or ".." in ref.parts
                            or ref.parts[0] not in {"history-captures", "history-coverage"}):
                        self.record("priority_recovery", "omitted", reason="invalid_index_reference")
                        continue
                    collect(pool / ref)

    def tree(self, folder, prefix, archive, *, update_only=False, allowed_root=None, ocr=False):
        if not folder.exists():
            self.record(prefix, "absent")
            return
        if not self.source_allowed(folder, allowed_root):
            self.record(prefix, "omitted", reason="source_boundary")
            return
        # One noisy/legacy source cannot consume the entire export budget.
        self.source_end = self.visited + 400
        self.source_deadline = min(self.deadline, time.monotonic() + 4)
        stack = [(folder, "")]
        while stack:
            directory, relative = stack.pop(0)
            if self.limited():
                self.record(prefix, "omitted", reason="time_or_file_limit")
                break
            try:
                if not self.source_allowed(directory, allowed_root):
                    self.record(f"{prefix}/{relative}", "omitted", reason="source_boundary")
                    continue
                if update_only:
                    expected = {"update-state.json"} if directory == folder else (
                        {"update-result.json", "worker-startup.jsonl", "updater-startup.jsonl"}
                        if directory.name == "control" else set())
                    for name in sorted(expected):
                        if not (directory / name).exists():
                            self.record(f"{prefix}/{relative}/{name}", "absent")
                with os.scandir(directory) as listing:
                    recent = []
                    for entry in listing:
                        if len(recent) >= MAX_FILES or self.limited():
                            self.record(f"{prefix}/{relative}", "omitted", reason="directory_listing_limit")
                            break
                        recent.append(entry)
                    # Most recent evidence first, even in a long-lived data home.
                    recent.sort(key=lambda e: e.stat(follow_symlinks=False).st_mtime, reverse=True)
                    for entry in recent:
                        self.visited += 1
                        if self.limited():
                            self.record(prefix, "omitted", reason="time_or_file_limit")
                            return
                        rel = f"{relative}/{entry.name}".lstrip("/")
                        if not self.source_allowed(Path(entry.path), allowed_root):
                            self.record(f"{prefix}/{rel}", "omitted", reason="source_boundary")
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if prefix == "receipts" and directory == self.root/"transactions"/"hook" and entry.name in {
                                "sends", "send-receipts", "history-receipts", "history-captures",
                            }:
                                continue
                            if folder == artifacts(self.root) and entry.name in {
                                "tasks", "runtime", "incidents", "ui_failures", "wechat_c2",
                            }:
                                continue
                            # Never recurse into generated exports, installed programs or update payloads.
                            if entry.name in {"exports", "download", "staging", "backup", "control", "failed-program", "roi"}:
                                if update_only and entry.name == "control":
                                    stack.append((Path(entry.path), rel))
                                continue
                            stack.append((Path(entry.path), rel))
                        elif entry.is_file(follow_symlinks=False):
                            path = Path(entry.path)
                            if path.resolve() == self.destination.resolve() or path.name.endswith(".tmp"):
                                continue
                            if update_only and path.name not in UPDATE_DIAGNOSTICS:
                                continue
                            self.file(path, f"{prefix}/{rel}", archive, allowed_root=allowed_root,
                                      ocr=ocr or (prefix == "tasks" and "ocr" in Path(rel).parts))
            except OSError as exc:
                self.record(f"{prefix}/{relative}", "read_failed", error_type=type(exc).__name__)

    def file(self, path, member, archive, *, allowed_root=None, ocr=False):
        if not self.source_allowed(path, allowed_root):
            self.record(member, "omitted", reason="source_boundary")
            return
        if _FORBIDDEN_FILE_RE.search(path.name) or path.suffix.lower() not in TEXT_SUFFIXES | IMAGE_SUFFIXES | {".zip"}:
            self.record(member, "omitted", reason="unsupported_or_private_file")
            return
        try:
            # Bound actual reads too (file may grow after stat).
            with path.open("rb") as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                self.record(member, "omitted", reason="file_size_limit")
                return
            if path.suffix.lower() in TEXT_SUFFIXES:
                data = self.text_bytes(data, path.suffix.lower(), ocr=ocr)
        except (OSError, ValueError, RuntimeError, RecursionError) as exc:
            self.record(member, "read_failed", error_type=type(exc).__name__)
            return
        if path.suffix.lower() == ".zip":
            try:
                self.incident_zip(data, member, archive)
            except (ValueError, zipfile.BadZipFile, RuntimeError, RecursionError) as exc:
                self.record(member, "read_failed", error_type=type(exc).__name__)
            return
        # Destination failures abort atomically; never present a broken ZIP as partial success.
        self.add(archive, member, data, member)

    def incident_zip(self, data, member, archive):
        with zipfile.ZipFile(io.BytesIO(data)) as source:
            for info in source.infolist():
                self.visited += 1
                if self.limited():
                    self.record(member, "omitted", reason="time_or_file_limit")
                    break
                path = Path(info.filename)
                if info.is_dir():
                    continue
                if (path.is_absolute() or ".." in path.parts or "\\" in info.filename
                        or info.file_size > MAX_FILE_BYTES or _FORBIDDEN_FILE_RE.search(path.name)
                        or path.suffix.lower() not in TEXT_SUFFIXES | IMAGE_SUFFIXES):
                    self.record(f"{member}/{info.filename}", "omitted", reason="unsafe_or_oversize_member")
                    continue
                raw = source.read(info)
                if path.suffix.lower() in TEXT_SUFFIXES:
                    raw = self.text_bytes(raw, path.suffix.lower(), ocr="ocr" in path.parts)
                self.add(archive, f"{member.removesuffix('.zip')}/{info.filename}", raw, f"{member}!/{info.filename}")

    def run(self):
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.destination.with_name(f".{self.destination.name}.{uuid.uuid4().hex}.tmp")
        self.emit(5, "读取诊断配置")
        self.read_secrets()
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                self.emit(15, "读取最新状态")
                self.database(archive)
                self.database(archive, name="worker_telemetry.sqlite3", tables=(
                    "telemetry_stage_events", "telemetry_process_links",
                    "telemetry_stage_attempts", "telemetry_authority_snapshots"))
                self.priority_recovery(archive)
                updates_root = update_root(self.root).absolute()
                # Current recovery proofs first; legacy incident volume cannot starve them.
                sources = [
                    *[(self.root/"transactions"/"hook"/name, "receipts/hook/"+name, False, self.root, False)
                      for name in ("sends", "send-receipts", "history-receipts", "history-captures")],
                    (self.root/"transactions", "receipts", False, self.root, False),
                    (artifacts(self.root)/"runtime", "logs/runtime", False, self.root, False),
                    (updates_root, "logs/update", True, updates_root, False),
                    (self.root/"diagnostics", "logs/legacy-startup", False, self.root, False),
                    (self.root.parent/"CheJinWorker"/"diagnostics", "logs/legacy-packaging-startup", False, self.root.parent, False),
                    (incident_roots(self.root)[0], "incidents/current", False, self.root, False),
                    (artifacts(self.root)/"tasks", "tasks", False, self.root, False),
                    (self.root/"transactions"/"hook"/"roi", "tasks/legacy-hook", False, self.root, True),
                    (artifacts(self.root)/"ui_failures", "tasks/legacy-ui", False, self.root, True),
                    (artifacts(self.root)/"wechat_c2", "tasks/legacy-ocr", False, self.root, True),
                    (artifacts(self.root), "tasks/legacy-artifacts", False, self.root, False),
                    (incident_roots(self.root)[1], "incidents/legacy", False, self.root, False),
                ]
                for index, (folder, prefix, updates, boundary, ocr) in enumerate(sources):
                    self.emit(25 + int(index * 65 / len(sources)), "收集现场和关联凭证")
                    self.tree(folder, prefix, archive, update_only=updates, allowed_root=boundary, ocr=ocr)
                issues = [e for e in self.entries if e["status"] in {"read_failed", "omitted"}]
                manifest = {"schema_version": 1, "exported_at": datetime.now(timezone.utc).isoformat(),
                            "build": build_identity(),
                            "log_last_at": self.latest_logs[0].get("created_at") if self.latest_logs else None,
                            "partial": bool(issues), "limits": {"bytes": self.max_bytes, "files": MAX_FILES,
                            "rows_per_table": MAX_ROWS, "seconds": MAX_SECONDS}, "sources": self.entries,
                            "business_actions_performed": False,
                            "content_policy": "Conversation/audio bodies and credentials excluded; screenshots may contain customer information."}
                if "logs/latest_logs.json" not in self.members:
                    archive.writestr("logs/latest_logs.json", b"[]")
                archive.writestr("收集清单.json", _json(manifest))
                archive.writestr("export.json", _json(manifest))
                archive.writestr("evidence-index/export.json", _json({
                    "files": [e for e in self.entries if e["status"] == "collected"],
                    "omissions": issues,
                }))
                archive.writestr("诊断摘要.txt", (
                    "车金故障证据\n包含故障现场、导出时状态和关联凭证；未执行任何业务动作。\n"
                    + ("部分材料未收集，请先查看收集清单中的缺项。\n" if issues else "已完成本次收集，请查看收集清单。\n")
                    + "state 内的任务、消息及动作编号可用于查询后台记录。\n"
                    + "本包含本机现场，仅用于授权排查，不应提交公开仓库。\n").encode("utf-8"))
            os.replace(temporary, self.destination)
            self.emit(100, "导出完成" if not issues else "导出完成，部分证据缺失")
            return self.destination
        finally:
            temporary.unlink(missing_ok=True)


def export_bundle(root, destination, progress=None, **limits):
    return Collector(root, destination, progress, **limits).run()
