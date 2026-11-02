"""Bounded append-only performance segments; never touches business receipts."""

import json
import os
from pathlib import Path
import time

from .performance_contract import POLICY


def encoded(value):
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode()


class Store:
    def __init__(self, root, session, policy=POLICY):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.session = session
        self.policy = policy
        self.index_path = self.root / "index.json"
        self.index = {
            "schema": 1,
            "segments": [],
            "sessions": {},
            "events": [],
            "gaps": [],
            "retired": 0,
        }
        self.error_count = 0
        self.segment = None
        self.serial = 0
        self.unindexed_bytes = 0
        self.last_clock = None
        if self.index_path.exists():
            try:
                with self.index_path.open("rb") as stream:
                    raw = stream.read(256 * 1024 + 1)
                if len(raw) > 256 * 1024:
                    raise ValueError("index_size")
                self.index = json.loads(raw)
                if (
                    not isinstance(self.index, dict)
                    or not all(
                        k in self.index
                        for k in ("segments", "sessions", "events", "gaps", "retired")
                    )
                    or not isinstance(self.index["sessions"], dict)
                    or not all(
                        isinstance(v, dict) for v in self.index["sessions"].values()
                    )
                    or not all(
                        isinstance(self.index[k], list)
                        and all(isinstance(v, dict) for v in self.index[k])
                        for k in ("segments", "events", "gaps")
                    )
                ):
                    raise ValueError("index_schema")
                # Reconcile only our bounded directory, once per sampler start.
                for meta in self.index["segments"]:
                    path = self.root / meta["name"]
                    if Path(meta["name"]).name != meta["name"] or path.is_symlink():
                        raise ValueError("index_boundary")
                    try:
                        meta["bytes"] = path.stat().st_size
                    except OSError:
                        meta["missing"] = True
                        self.gap("segment_missing")
            except (ValueError, OSError, TypeError, KeyError):
                # Do not overwrite unindexed evidence. Fail closed for capture,
                # while the business process remains unaffected.
                raise ValueError("performance_index_unreadable")
        indexed = {m["name"] for m in self.index["segments"]}
        # One startup-only reconciliation, bounded even if the folder is corrupt.
        for number, path in enumerate(self.root.iterdir()):
            if number >= 8192:
                raise ValueError("directory_entry_limit")
            if path.name in indexed or path.name in {
                "index.json",
                "index.tmp",
                "sampler.lock",
                "capture-gap.json",
                "capture-gap.tmp",
            }:
                continue
            if path.is_file() and not path.is_symlink():
                self.unindexed_bytes += path.stat().st_size
                self.gap("unindexed_file_retained")
        for sid, previous in self.index["sessions"].items():
            if previous.get("status") == "running":
                previous["status"] = "end_unconfirmed"
                for event in self.index["events"]:
                    if event["session"] == sid and event.get("end") is None:
                        event["post_window"] = "interrupted_at_session_end"

    def gap(self, reason, count=1):
        gaps = self.index["gaps"]
        now = time.time()
        if gaps and gaps[-1]["reason"] == reason:
            gaps[-1]["count"] += count
            gaps[-1]["last"] = now
        else:
            gaps.append({"reason": reason, "count": count, "first": now, "last": now})
        if len(gaps) > 64:
            old = gaps.pop(0)
            self.index["retired_gap_details"] = (
                self.index.get("retired_gap_details", 0) + old["count"]
            )

    def save_index(self):
        data = encoded(self.index)
        if len(data) > min(256 * 1024, self.policy.budget_bytes // 8):
            raise ValueError("performance_index_capacity")
        temp = self.root / "index.tmp"
        temp.write_bytes(data)
        os.replace(temp, self.index_path)

    def _expired(self, meta, now, mono=None):
        if (
            meta.get("session") == self.session
            and mono is not None
            and "last_mono" in meta
        ):
            return mono - meta["last_mono"] >= self.policy.retention_seconds
        # An earlier launch cannot be aged reliably across a detected wall-clock
        # correction. Keep capacity-based ordinary retirement, not guessed age.
        if self.index.get("retention_clock_uncertain"):
            return False
        return now - meta["last"] >= self.policy.retention_seconds

    def _protected(self, meta, now, mono=None):
        for event in self.index["events"]:
            if event["session"] != meta["session"]:
                continue
            if event.get("end") is not None and self._expired(event, now, mono):
                continue
            # Within a launch, use monotonic boundaries even after clock sync.
            suffix = (
                "_mono"
                if all(
                    k + "_mono" in meta and k + "_mono" in event
                    for k in ("first", "last")
                )
                else ""
            )
            if (
                meta["last" + suffix]
                >= event["first" + suffix] - self.policy.window_seconds
                and meta["first" + suffix]
                <= event["last" + suffix] + self.policy.window_seconds
            ):
                return True
        return False

    def append(self, value, now=None):
        now = time.time() if now is None else now
        data = encoded(value)
        p = self.policy
        if len(data) > p.segment_bytes:
            self.gap("record_size_limit")
            self.save_index()
            return False
        metas = self.index["segments"]
        reserve = min(512 * 1024, p.budget_bytes // 4)
        used = self.unindexed_bytes + sum(m["bytes"] for m in metas)
        mono = value.get("monotonic")
        clock_shift = False
        if self.last_clock and isinstance(mono, (float, int)):
            clock_shift = (
                abs((now - self.last_clock[0]) - (mono - self.last_clock[1])) > 120
            )
            if clock_shift:
                self.gap("wall_clock_jump")
                self.index["retention_clock_uncertain"] = True
        if isinstance(mono, (float, int)):
            self.last_clock = (now, mono)
        for m in list(metas):
            if used + len(data) <= p.budget_bytes - reserve and not self._expired(
                m, now, mono
            ):
                continue
            if m is self.segment or self._protected(m, now, mono):
                continue
            path = self.root / m["name"]
            path.unlink(missing_ok=True)
            used -= m["bytes"]
            metas.remove(m)
            self.index["retired"] += 1
            self.index["retired_last_range"] = [m["first"], m["last"]]
        if used + len(data) > p.budget_bytes - reserve:
            self.gap("protected_or_active_capacity_exhausted")
            self.save_index()
            return False
        if (
            self.segment is None
            or self.segment["bytes"] + len(data) > p.segment_bytes
            or (
                mono - self.segment.get("first_mono", mono)
                if isinstance(mono, (float, int))
                else now - self.segment["first"]
            )
            >= p.window_seconds
        ):
            self.serial += 1
            self.segment = {
                "name": f"{self.session}-{self.serial:06d}.jsonl",
                "session": self.session,
                "first": now,
                "last": now,
                "bytes": 0,
                "rows": 0,
            }
            if isinstance(mono, (float, int)):
                self.segment.update(first_mono=mono, last_mono=mono)
            metas.append(self.segment)
        with (self.root / self.segment["name"]).open("ab") as output:
            output.write(data)
        self.segment["bytes"] += len(data)
        self.segment["rows"] += 1
        self.segment["last"] = now
        if isinstance(mono, (float, int)):
            self.segment["last_mono"] = mono
        self.save_index()
        return True

    def event(self, reasons, now, mono):
        events = self.index["events"]
        active = next(
            (
                e
                for e in reversed(events)
                if e["session"] == self.session and e.get("end") is None
            ),
            None,
        )
        if reasons:
            if active is None:
                active = {
                    "id": f"{self.session}-{len(events)+1}",
                    "session": self.session,
                    "first": now,
                    "last": now,
                    "first_mono": mono,
                    "last_mono": mono,
                    "reasons": [],
                    "end": None,
                    "stack_requests": 0,
                    "stacks_received": 0,
                    "stack_status": "not_requested",
                }
                events.append(active)
            active["last"] = now
            active["last_mono"] = mono
            active["reasons"] = sorted(set(active["reasons"]) | set(reasons))[:32]
        elif active and mono - active["last_mono"] >= self.policy.window_seconds:
            active["end"] = now
            active["post_window"] = "complete"
            active = None
        if (
            active
            and active["stack_requests"] < self.policy.stack_count
            and mono - active.get("last_stack_mono", -1e12)
            >= self.policy.stack_interval_seconds
        ):
            active["stack_status"] = "requested_response_unconfirmed"
            active["stack_requests"] += 1
            active["last_stack_mono"] = mono
            return active["id"]
        return None

    def stack_received(self, event_id):
        for event in self.index["events"]:
            if event["id"] == event_id:
                event["stacks_received"] = event.get("stacks_received", 0) + 1
                event["stack_status"] = "received"
                return

    def end(self, clean):
        session = self.index["sessions"][self.session]
        session["end"] = time.time()
        session["status"] = "normal_exit" if clean else "end_unconfirmed"
        for event in self.index["events"]:
            if event["session"] == self.session and event.get("end") is None:
                event["post_window"] = "interrupted_at_session_end"
            if event.get("stack_requests", 0) > event.get("stacks_received", 0):
                event["stack_status"] = "partial_or_unavailable_worker_did_not_respond"
        self.save_index()
