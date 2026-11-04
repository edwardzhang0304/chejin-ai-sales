"""Independent PE-01 process. stdin carries bounded, body-free Worker snapshots."""

from collections import deque
import json
import os
import re
from pathlib import Path
import sys
import threading
import time
from datetime import datetime, timezone

import psutil

from .performance_contract import POLICY, Policy, Anomalies
from .performance_store import Store, encoded


class Resources:
    def __init__(self):
        self.previous = {}

    def sample(self, role, pid, created, mono):
        result = {
            "pid": pid,
            "created": created,
            "role": role,
            "identity_valid": False,
            "unavailable": {},
        }
        try:
            process = psutil.Process(pid)
            if process.create_time() != created:
                return {**result, "unavailable": {"process": "pid_reused"}}

            result["identity_valid"] = True

            def read(name, fn):
                try:
                    result[name] = fn()
                except (
                    psutil.Error,
                    OSError,
                    AttributeError,
                    NotImplementedError,
                ) as exc:
                    result[name] = None
                    result["unavailable"][name] = type(exc).__name__

            with process.oneshot():
                read("cpu_seconds", lambda: sum(process.cpu_times()[:2]))
                read("rss_bytes", lambda: process.memory_info().rss)
                read("private_bytes", lambda: process.memory_info().private)
                read("threads", process.num_threads)
                read("handles", lambda: process.num_handles())
                read(
                    "io_bytes",
                    lambda: {
                        "read": process.io_counters().read_bytes,
                        "write": process.io_counters().write_bytes,
                    },
                )
            key = (pid, created)
            previous = self.previous.get(key)
            result["cpu_percent"] = None
            result["io_delta"] = None
            if previous and mono > previous[0]:
                if result.get("cpu_seconds") is not None and previous[1] is not None:
                    result["cpu_percent"] = max(
                        0,
                        100
                        * (result["cpu_seconds"] - previous[1])
                        / (mono - previous[0]),
                    )
                if result.get("io_bytes") and previous[2]:
                    result["io_delta"] = {
                        k: max(0, v - previous[2][k])
                        for k, v in result["io_bytes"].items()
                    }
            else:
                result["unavailable"]["cpu_percent"] = "first_sample"
                result["unavailable"]["io_delta"] = "first_sample"
            self.previous[key] = (
                mono,
                result.get("cpu_seconds"),
                result.get("io_bytes"),
            )
            # Process set bounded by the producer's known-child contract.
            if len(self.previous) > 128:
                self.previous.pop(next(iter(self.previous)))
        except (psutil.Error, OSError) as exc:
            result["unavailable"]["process"] = type(exc).__name__
        return result


def acquire(root):
    """OS lock survives neither process exit nor a lost Python reference."""
    path = root / "sampler.lock"
    handle = path.open("a+b")
    handle.seek(0)
    handle.write(b"0")
    handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise
    return handle


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0])
    root.mkdir(parents=True, exist_ok=True)
    parent_pid, parent_created, session = int(argv[1]), float(argv[2]), argv[3]
    if parent_pid <= 0 or not re.fullmatch(r"[a-f0-9]{32}", session):
        return 2
    # Tests invoke the same process with a bounded accelerated policy, never via
    # production CLI/profile settings. The default is always the central policy.
    p = POLICY if len(argv) < 5 else Policy(**json.loads(argv[4]))

    def parent_alive():
        try:
            return psutil.Process(parent_pid).create_time() == parent_created
        except psutil.Error:
            return False

    lock = None
    for _ in range(30):
        try:
            lock = acquire(root)
            break
        except OSError:
            if not parent_alive():
                return 2
            time.sleep(0.1)
    if lock is None:
        fallback(root, session, "sampler_lock_unavailable", 1)
        return 3
    inbox = {
        "latest": {},
        "closed": False,
        "clean": False,
        "bad": 0,
        "merged": 0,
        "dropped": 0,
    }
    pending_events = deque(maxlen=512)
    pending_stacks = deque(maxlen=3)
    inbox_lock = threading.Lock()

    def receive():
        while True:
            line = sys.stdin.buffer.readline(p.ipc_bytes + 1)
            if not line:
                inbox["closed"] = True
                return
            if len(line) > p.ipc_bytes or not line.endswith(b"\n"):
                inbox["bad"] += 1
                while line and not line.endswith(b"\n"):
                    line = sys.stdin.buffer.readline(p.ipc_bytes + 1)
                continue
            try:
                data = json.loads(line)
                if not isinstance(data, dict):
                    raise ValueError("snapshot_type")
                if data.get("end") == "normal":
                    inbox["clean"] = True
                    inbox["closed"] = True
                    return
                with inbox_lock:
                    events = data.pop("events", [])
                    if not isinstance(events, list):
                        raise ValueError("events_type")
                    for event in events:
                        if len(pending_events) == pending_events.maxlen:
                            inbox["dropped"] += 1
                        pending_events.append(event)
                    stack = data.pop("stack", None)
                    if stack is not None:
                        if len(pending_stacks) == pending_stacks.maxlen:
                            inbox["dropped"] += 1
                        pending_stacks.append(stack)
                    inbox["merged"] += 1
                    inbox["latest"] = data
            except (ValueError, AttributeError):
                inbox["bad"] += 1

    threading.Thread(target=receive, name="performance-ipc", daemon=True).start()
    try:
        store = Store(root, session, p)
        store.index["sessions"][session] = {
            "pid": parent_pid,
            "created": parent_created,
            "start": time.time(),
            "status": "running",
            "policy": p.payload(),
        }
        store.save_index()
        resource = Resources()
        detector = Anomalies(p)
        self_created = psutil.Process().create_time()
        seq = 0
        next_sample = time.monotonic()
        last_snapshot_seq = None
        known = set()
        psutil.cpu_percent(None)
        started_mono = time.monotonic()
        previous_losses = {}
        while True:
            ending = not parent_alive() or inbox["closed"]
            mono = time.monotonic()
            if mono < next_sample and not ending:
                time.sleep(min(0.2, next_sample - mono))
                continue
            next_sample = mono + p.sample_seconds
            started = mono
            seq += 1
            now = time.time()
            snapshot = None
            with inbox_lock:
                snapshot = dict(inbox["latest"])
                snapshot["events"] = list(pending_events)
                pending_events.clear()
                sample_stacks = list(pending_stacks)
                pending_stacks.clear()
                merged, ipc_dropped = inbox["merged"], inbox["dropped"]
                inbox["merged"] = 0
            sample = {
                "schema": 1,
                "session": session,
                "seq": seq,
                "utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "monotonic": mono,
                "build": snapshot.get("build"),
                "resources": {},
                "snapshot": snapshot,
                "stacks": sample_stacks,
                "snapshots_merged": merged,
                "ipc_events_dropped": ipc_dropped,
                "cpu_basis": "one_logical_cpu_100_percent",
            }
            identities = [
                ("worker", parent_pid, parent_created),
                ("sampler", os.getpid(), self_created),
            ]
            for child in snapshot.get("processes", [])[:32]:
                identities.append((child["role"], child["pid"], child["created"]))
            present = set()
            for role, pid, created in identities:
                present.add((role, pid, created))
                sample["resources"][
                    role if role in ("worker", "sampler", "wechat") else f"{role}:{pid}"
                ] = resource.sample(role, pid, created, mono)
            sample["process_started"] = list(present - known)
            sample["process_removed"] = list(known - present)
            known = present
            try:
                sample["system"] = {
                    "available_memory": psutil.virtual_memory().available,
                    "cpu_percent": psutil.cpu_percent(None) if seq > 1 else None,
                    "disk_free": psutil.disk_usage(str(root)).free,
                }
            except (OSError, psutil.Error) as exc:
                sample["system"] = {"unavailable": type(exc).__name__}
            snapshot_seq = snapshot.get("seq")
            sample["snapshot_status"] = (
                "missing"
                if snapshot_seq is None
                else ("stale" if snapshot_seq == last_snapshot_seq else "updated")
            )
            last_snapshot_seq = snapshot_seq
            reasons = detector.check(mono, sample["resources"], snapshot)
            if mono - snapshot.get("snapshot_monotonic", started_mono) > max(
                p.ui_delay_seconds, p.sample_seconds * 2
            ):
                reasons.append("worker_snapshot_gap")
            if "ui_monotonic" not in snapshot:
                sample["ui_status"] = "unavailable_no_ui_pulse"
            for stack in sample_stacks:
                store.stack_received(stack.get("event"))
            request = store.event(reasons, now, mono)
            if request:
                print(json.dumps({"stack_event": request}), flush=True)
            # Bounded health feedback for optional in-process diagnostics.
            print(json.dumps({"capture_load": {"at": mono,
                "cpu_percent": sample["resources"].get("worker", {}).get("cpu_percent")}}), flush=True)
            sample["anomalies"] = reasons
            sample["anomaly_details"] = detector.details
            sample["capture_seconds"] = time.monotonic() - started
            sample["ipc_invalid"] = inbox["bad"]
            for reason, count in (
                ("producer_events_dropped", snapshot.get("producer_dropped", 0)),
                ("ipc_events_dropped", ipc_dropped),
                ("ipc_invalid", inbox["bad"]),
            ):
                previous = previous_losses.get(reason, 0)
                if count > previous:
                    store.gap(reason, count - previous)
                previous_losses[reason] = count
            try:
                store.append(sample, now)
            except (OSError, ValueError) as exc:
                store.error_count += 1
                # Independent small fallback. If even this fails, the missing
                # samples/index status remain unknown, never reported healthy.
                fallback(root, session, type(exc).__name__, store.error_count)
            if ending:
                break
        store.end(inbox["clean"])
    except Exception as exc:
        fallback(root, session, type(exc).__name__, 1)
        return 1
    finally:
        lock.close()
    return 0


def fallback(root, session, reason, count):
    try:
        temp = root / "capture-gap.tmp"
        temp.write_bytes(
            encoded(
                {
                    "session": session,
                    "reason": reason,
                    "count": count,
                    "at": time.time(),
                    "coverage": "unknown_since_last_sample",
                }
            )
        )
        os.replace(temp, root / "capture-gap.json")
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
