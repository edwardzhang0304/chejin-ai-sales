"""Nonblocking, bounded producer API; one owned independent sampler per Worker."""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from functools import wraps

from .performance_contract import POLICY, correlations

_ACTIVE = None


def emit(kind, **values):
    active = _ACTIVE
    if active is not None:
        try:
            active.offer(kind, values)
        except Exception:
            active.dropped += 1


@contextmanager
def measurement(name, context=None, *, waiting=None):
    # Diagnostic invocation id is separate from server-issued task/Flow ids.
    if _ACTIVE is None:
        yield
        return
    invocation = uuid.uuid4().hex
    started = time.monotonic()
    fields = correlations(context or {})
    emit(
        "stage",
        name=name,
        status="running",
        measurement_id=invocation,
        waiting=waiting,
        **fields
    )
    status = "failed"
    try:
        yield
        status = "completed"
    finally:
        emit(
            "stage",
            name=name,
            status=status,
            measurement_id=invocation,
            duration_ms=round((time.monotonic() - started) * 1000),
            **fields
        )


def cleanup_measurement(operation):
    @wraps(operation)
    def measured(*args, **kwargs):
        with measurement("cleanup." + operation.__name__):
            result = operation(*args, **kwargs)
        if isinstance(result, dict):
            for key in ("retired_rows", "released_bytes", "retired_captures"):
                if type(result.get(key)) is int:
                    emit("counter", name="cleanup." + key, count=result[key])
        elif hasattr(result, "deleted_files"):
            emit("counter", name="cleanup.files", count=result.deleted_files)
            emit("counter", name="cleanup.bytes", count=result.released_bytes)
        elif type(result) is int:
            emit("counter", name="cleanup.directories", count=result)
        return result

    return measured


def install(root, *, policy=POLICY):
    global _ACTIVE
    if _ACTIVE is not None:
        return _ACTIVE
    active = None
    try:
        active = Monitor(root, policy=policy)
        active.start()
        _ACTIVE = active
        return active
    except Exception as exc:
        if active is not None:
            try:
                active.close(clean=False)
            except Exception:
                pass
        from .performance_sampler import fallback

        folder = Path(root) / "artifacts" / "performance"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        fallback(folder, "not_started", type(exc).__name__, 1)
        return None


def register_process(role, pid, created=None):
    if _ACTIVE is None:
        return
    try:
        import psutil

        stamp = psutil.Process(pid).create_time() if created is None else created
        emit("process", role=role, pid=pid, created=stamp)
    except (OSError, psutil.Error):
        emit("counter", name="process_identity_unavailable")


def attach_renderer(page):
    if _ACTIVE is None:
        return
    try:
        page.renderProcessPidChanged.connect(
            lambda pid: register_process("ui", int(pid)) if pid else None
        )
        pid = page.renderProcessPid()
        if pid:
            register_process("ui", int(pid))
    except Exception:
        emit("counter", name="renderer_identity_unavailable")


def attach_ui(app):
    if _ACTIVE is None:
        return None
    from PySide6.QtCore import QTimer

    timer = QTimer(app)
    timer.setInterval(1000)
    timer.timeout.connect(lambda: emit("ui"))
    timer.start()
    emit("ui")
    return timer


class Monitor:
    def __init__(self, root, *, policy=POLICY):
        import psutil

        self.policy = policy
        self.root = Path(root) / "artifacts" / "performance"
        self.session = uuid.uuid4().hex
        self.pid = os.getpid()
        self.created = psutil.Process().create_time()
        self.pending = deque(maxlen=256)
        self.lock = threading.Lock()
        self.dropped = 0
        self.stop = threading.Event()
        self.clean = False
        self.state = {
            "seq": 0,
            "stages": {},
            "batches": {},
            "loops": {},
            "counters": {},
            "events": [],
            "processes": [],
            "gauges": {},
            "coverage": {"queue_baseline": "unknown_preexisting_backlog"},
        }
        self.last_children = -1e12
        self.children = {}
        self.process = None
        self.thread = None
        self.reader = None
        self.stack_requests = deque(maxlen=3)
        self.last_stack = -1e12

    def offer(self, kind, values):
        # No disk, Hook call, wait or unbounded serialization in producer code.
        if not self.lock.acquire(False):
            self.dropped += 1
            return
        try:
            if len(self.pending) == self.pending.maxlen:
                self.dropped += 1
            # Accept only typed diagnostic values; raw task/message dictionaries
            # never travel through this boundary.
            allowed = {}
            for key in (
                "name",
                "status",
                "waiting",
                "stage_run_id",
                "process_run_id",
                "conversation_id",
                "task_id",
                "flow_id",
                "batch_id",
                "action_id",
                "native_msg_id",
                "pid",
                "created",
                "role",
                "rows",
                "bytes",
                "duration_ms",
                "delta",
                "count",
                "cache_hit",
                "measurement_id",
            ):
                value = values.get(key)
                if value is None or type(value) in (bool, int, float):
                    allowed[key] = value
                elif isinstance(value, str):
                    allowed[key] = value[:128]
            self.pending.append((kind, allowed, time.monotonic()))
        finally:
            self.lock.release()

    def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        from .diagnostic_bundle import build_identity

        self.state["build"] = build_identity()
        args = [str(self.root), str(self.pid), repr(self.created), self.session]
        if self.policy != POLICY:
            args.append(json.dumps(self.policy.payload()))
        command = (
            [sys.executable, "--performance-sampler"]
            if getattr(sys, "frozen", False)
            else [
                sys.executable,
                "-m",
                "chejin_worker_client.main",
                "--performance-sampler",
            ]
        )
        self.process = subprocess.Popen(
            command + args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.reader = threading.Thread(
            target=self._read, daemon=True, name="performance-stack-requests"
        )
        self.reader.start()
        self.thread = threading.Thread(
            target=self._pump, daemon=True, name="performance-snapshot"
        )
        self.thread.start()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(1025)
                if not line:
                    break
                if len(line) > 1024:
                    break
                request = json.loads(line)
                event = request.get("stack_event")
                if isinstance(event, str):
                    self.stack_requests.append(event[:128])
        except (OSError, ValueError):
            pass

    def _drain(self):
        with self.lock:
            pending = list(self.pending)
            self.pending.clear()
        events = []
        for kind, value, at in pending:
            if kind == "gauge":
                self.state["gauges"][value["name"]] = {
                    "value": value.get("count"),
                    "at": at,
                }
            elif kind == "observation" and value.get("name") == "batch_state":
                batch = value.get("batch_id")
                if batch:
                    states = self.state["batches"]
                    states[batch] = {
                        "status": value.get("status"),
                        "at": at,
                        "waiting": value.get("waiting"),
                    }
                    while len(states) > 64:
                        states.pop(next(iter(states)))
                        self.dropped += 1
            elif kind == "ui":
                self.state["ui_monotonic"] = at
            elif kind == "loop":
                self.state["loops"][value["name"]] = {
                    "at": at,
                    "waiting": value.get("waiting"),
                }
            elif kind == "process":
                if (
                    value.get("role") in ("ocr", "ui", "wechat")
                    and type(value.get("pid")) is int
                    and type(value.get("created")) in (int, float)
                ):
                    self.children[(value["pid"], value["created"])] = {
                        k: value[k] for k in ("pid", "created", "role")
                    }
                    while len(self.children) > 32:
                        self.children.pop(next(iter(self.children)))
            elif kind == "stage":
                key = value.get("measurement_id") or value.get("stage_run_id")
                if key:
                    if value.get("status") == "running":
                        if len(self.state["stages"]) >= self.policy.active_stages:
                            self.dropped += 1
                        else:
                            self.state["stages"][key] = {
                                "at": at,
                                "name": value.get("name"),
                                "waiting": value.get("waiting"),
                                **correlations(value),
                            }
                    else:
                        self.state["stages"].pop(key, None)
                counter = (
                    "stage." + str(value.get("name")) + "." + str(value.get("status"))
                )
                if (
                    counter in self.state["counters"]
                    or len(self.state["counters"]) < 128
                ):
                    self.state["counters"][counter] = (
                        self.state["counters"].get(counter, 0) + 1
                    )
                else:
                    self.dropped += 1
                events.append({"kind": kind, "at": at, **value})
            else:
                name = value.get("name")
                if name and (
                    name in self.state["counters"] or len(self.state["counters"]) < 128
                ):
                    self.state["counters"][name] = self.state["counters"].get(
                        name, 0
                    ) + (
                        value["delta"]
                        if value.get("delta") is not None
                        else value["count"] if value.get("count") is not None else 1
                    )
                events.append({"kind": kind, "at": at, **value})
        self.dropped += max(0, len(events) - 128)
        for name, limit in (("gauges", 64), ("loops", 32)):
            values = self.state[name]
            while len(values) > limit:
                values.pop(next(iter(values)))
                self.dropped += 1
        self.state["events"] = events[-128:]
        self.state["producer_dropped"] = self.dropped
        self.state["gauges"]["observed_generating_batches"] = {
            "value": sum(
                b.get("status") == "generating" for b in self.state["batches"].values()
            ),
            "coverage": "last_observed_bounded_set_not_server_backlog",
        }
        # Relative flow balance is useful without a full-table startup scan.
        # It is not an absolute backlog: pre-existing work remains unknown.
        counters = self.state["counters"]
        for name, produced, consumed in (
            ("unprojected_delta", "received_packages", "projected_packages"),
            ("unreceipted_delta", "outbox_created", "outbox_receipts_stored"),
        ):
            self.state["gauges"][name] = {
                "value": counters.get(produced, 0)
                - counters.get(consumed, 0)
                - (
                    counters.get("outbox_covered", 0)
                    if name == "unreceipted_delta"
                    else 0
                ),
                "coverage": "relative_since_capture_start_baseline_unknown",
            }
        self.state["processes"] = list(self.children.values())
        self.state["seq"] += 1
        self.state["snapshot_monotonic"] = time.monotonic()

    def _stacks(self):
        if (
            not self.stack_requests
            or time.monotonic() - self.last_stack < self.policy.stack_interval_seconds
        ):
            return
        event = self.stack_requests.popleft()
        self.last_stack = time.monotonic()
        frames = sys._current_frames()
        stacks = []
        for ident, frame in list(frames.items())[:64]:
            stack = []
            while frame and len(stack) < 32:
                stack.append(
                    {
                        "module": Path(frame.f_code.co_filename).name,
                        "function": frame.f_code.co_name,
                        "line": frame.f_lineno,
                    }
                )
                frame = frame.f_back
            stacks.append({"thread": ident, "frames": stack})
        self.state["stack"] = {"event": event, "at": self.last_stack, "threads": stacks}

    def _prune_processes(self):
        # Only identities registered by the launcher or Qt's renderer PID signal.
        import psutil

        for key in list(self.children):
            try:
                if psutil.Process(key[0]).create_time() == key[1]:
                    continue
            except psutil.NoSuchProcess:
                pass
            except (psutil.Error, OSError):
                continue  # retain identity so sampler records AccessDenied
            self.children.pop(key, None)

    def _send_snapshot(self):
        self._drain()
        self._stacks()
        if time.monotonic() - self.last_children >= self.policy.sample_seconds:
            self._prune_processes()
            self.last_children = time.monotonic()
        raw = (json.dumps(self.state, separators=(",", ":")) + "\n").encode()
        if len(raw) > self.policy.ipc_bytes:
            self.dropped += len(self.state["events"]) + int("stack" in self.state)
            self.state["events"] = []
            self.state.pop("stack", None)
            self.state["producer_dropped"] = self.dropped
            raw = (json.dumps(self.state, separators=(",", ":")) + "\n").encode()
            if len(raw) > self.policy.ipc_bytes:
                raise ValueError("snapshot_size_limit")
        # A blocked pipe blocks only this daemon, never business/UI producers.
        self.process.stdin.write(raw)
        self.process.stdin.flush()
        self.state.pop("stack", None)

    def _pump(self):
        from .performance_sampler import fallback

        try:
            while not self.stop.is_set():
                if self.process.poll() is not None:
                    fallback(self.root, self.session, "sampler_exited", 1)
                    return
                self._send_snapshot()
                self.stop.wait(self.policy.snapshot_seconds)
            self._send_snapshot()
            if self.clean:
                self.process.stdin.write(b'{"end":"normal"}\n')
                self.process.stdin.flush()
            self.process.stdin.close()
        except Exception as exc:
            fallback(self.root, self.session, type(exc).__name__, 1)

    def close(self, *, clean=True):
        global _ACTIVE
        self.clean = clean
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)
        if self.process:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        if _ACTIVE is self:
            _ACTIVE = None
