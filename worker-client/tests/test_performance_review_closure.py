"""PE-01 review counterexamples plus real producer/sampler/desktop ZIP chains.

Accelerated policy/time boundaries are explicitly not 48-hour or Windows UAT.
"""

import hashlib
import json
import subprocess
import sys
import time
import uuid
import zipfile
from dataclasses import replace
from types import SimpleNamespace as NS

import psutil
import pytest
from PySide6.QtWidgets import QApplication
from chejin_worker_client import performance_evidence as pe, storage
from chejin_worker_client.performance_contract import POLICY, Anomalies, export_order
from chejin_worker_client.performance_store import Store, encoded
from chejin_worker_client.diagnostic_bundle import Collector, export_bundle
from chejin_worker_client.performance_export import collect
from chejin_worker_client.hook_formal_runtime import (
    HookFormalRuntime,
    history_read_cycle,
)
from chejin_worker_client.hook_projection import NativeProjectionStore
from chejin_worker_client.hook_outbox import HookDeliveryOutbox
from chejin_worker_client.shared_rules import hook_contract_rules as rules
from test_hook_outbox import setup, project, NOW
from test_performance_evidence import (
    FAST,
    monitors,
    start,
    samples,
    wait_until,
    exported,
)


def desktop(root, monkeypatch, **limits):
    from chejin_worker_client import diagnostic_ui

    app = QApplication.instance() or QApplication([])
    original = diagnostic_ui.export_bundle
    with monkeypatch.context() as patch:
        patch.setattr(
            diagnostic_ui, "export_bundle", lambda *args, **kwargs: original(*args, **kwargs, **limits)
        )
        job = diagnostic_ui.ExportJob(root)
        result, errors = [], []
        job.completed.connect(result.append)
        job.failed.connect(errors.append)
        assert job.start()
        deadline = time.monotonic() + 15
        while not result and not errors and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert result and not errors
        return exported(result[0])


def test_actual_anomaly_then_normal_segments_restart_desktop_budget(
    tmp_path, monitors, monkeypatch
):
    app = QApplication.instance() or QApplication([])
    policy = replace(FAST, growth_bytes=1024**3, cpu_percent=10000)
    m = start(tmp_path, monitors, policy)
    timer = pe.attach_ui(app)
    timer.setInterval(15)
    # Stop processing UI events: independent sampler must protect this window.
    time.sleep(0.65)
    wait_until(
        lambda: any("ui_response_gap" in r["anomalies"] for r in samples(tmp_path))
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    timer.stop()
    m.close()
    # Multiple launches must not promote recent normal detail over old event.
    for _ in range(2):
        new = start(tmp_path, monitors, policy)
        time.sleep(0.45)
        new.close()
    folder = tmp_path / "artifacts/performance"
    index = json.loads((folder / "index.json").read_text())
    ordered = export_order(index)
    old_event = index["events"][0]
    from chejin_worker_client.performance_contract import window_overlaps

    protected = [x for x in ordered if window_overlaps(x, old_event, policy)]
    assert protected and ordered[0]["name"] == protected[0]["name"]
    # Budget only permits a subset; the first actual anomalous segment survives.
    full = desktop(tmp_path, monkeypatch)
    first_member = "performance/" + protected[0]["name"]
    budget = 500
    for name, raw in full.items():
        budget += len(raw)
        if name == first_member:
            break
    else:
        pytest.fail("protected segment absent from unconstrained export")
    pack = desktop(tmp_path, monkeypatch, max_bytes=budget)
    assert "performance/" + protected[0]["name"] in pack
    manifest = json.loads(pack["收集清单.json"])
    assert any(e.get("reason") == "segment_not_exported" for e in manifest["sources"])
    # Time budget exhaustion explicitly reports each range instead of silently
    # falling back to recent ordinary segments.
    collector = Collector(tmp_path, tmp_path / "timed.zip")
    monkeypatch.setattr(collector, "limited", lambda: True)
    with zipfile.ZipFile(collector.destination, "w") as z:
        collect(collector, z)
    assert any(
        e.get("reason") == "export_time_or_file_budget" and "first" in e
        for e in collector.entries
    )


def test_index_lifecycle_after_many_events_restarts_and_clock_change(tmp_path):
    root = tmp_path / "artifacts/performance"
    store = Store(root, "launch-0")
    store.index["sessions"][store.session] = {"status": "running", "start": 1000000}
    for i in range(2000):
        if i and i % 50 == 0:
            store.end(True)
            store = Store(root, f"launch-{i}")
            store.index["sessions"][store.session] = {
                "status": "running",
                "start": 1000000 + i * 1202,
            }
        mono = i * 1202
        now = 1000000 + mono
        store.event(["sustained_cpu"], now, mono)
        assert store.append({"monotonic": mono, "seq": i * 2}, now)
        store.event([], now + 601, mono + 601)
        assert store.append({"monotonic": mono + 601, "seq": i * 2 + 1}, now + 601)
    assert store.index["retired_events"] > 1000
    assert store.index["retired_sessions"] > 10
    assert len(encoded(store.index)) < 256 * 1024
    # Keep one unresolved window across a forward clock correction and restart.
    store.event(["memory_growth"], now + 602, mono + 602)
    store.append({"monotonic": mono + 602}, now + 602)
    frozen = store.segment["name"]
    before = hashlib.sha256((root / frozen).read_bytes()).hexdigest()
    store.append({"monotonic": mono + 1300}, now + 864000)
    store.end(False)
    resumed = Store(root, "resumed")
    resumed.index["sessions"]["resumed"] = {"status": "running"}
    assert resumed.append({"monotonic": mono + 1400}, now + 864100)
    assert hashlib.sha256((root / frozen).read_bytes()).hexdigest() == before
    resumed.end(True)
    pack = exported(export_bundle(tmp_path, tmp_path / "index-lifecycle.zip"))
    assert "performance/" + frozen in pack
    assert any(
        e.get("status") == "retired" and e.get("reason") == "index_lifecycle_policy"
        for e in json.loads(pack["收集清单.json"])["sources"]
    )


def test_child_detector_identity_unknown_short_peak_and_wechat_control():
    p = replace(POLICY, sustained_seconds=2, growth_bytes=10)
    a = Anomalies(p)

    def resource(created=1, **kw):
        return {
            "identity_valid": True,
            "pid": 42,
            "created": created,
            "rss_bytes": 1,
            "cpu_percent": 0,
            **kw,
        }

    a.check(0, {"ui:42": resource()}, {})
    assert not a.check(0.5, {"ui:42": resource(rss_bytes=20, cpu_percent=99)}, {})
    assert not a.check(1, {"ui:42": resource()}, {})
    a.check(2, {"ui:42": resource(rss_bytes=20, cpu_percent=99)}, {})
    assert len(a.check(4, {"ui:42": resource(rss_bytes=20, cpu_percent=99)}, {})) == 2
    assert a.details[0]["role"] == "ui" and a.details[0]["created"] == 1
    assert not a.check(5, {"ui:42": resource(created=2)}, {})
    assert not a.check(
        8, {"ui:42": resource(created=2, unavailable={"process": "AccessDenied"})}, {}
    )
    assert not a.baselines
    assert not a.check(12, {"wechat": resource(rss_bytes=999, cpu_percent=999)}, {})
    assert not a.check(20, {"wechat": resource(rss_bytes=999, cpu_percent=999)}, {})


@pytest.mark.parametrize("role", ["ui", "ocr"])
def test_real_registered_child_load_freezes_event_and_survives_restart(
    tmp_path, monitors, monkeypatch, role
):
    policy = replace(FAST, cpu_percent=10, sustained_seconds=0.3, ui_delay_seconds=100)
    m = start(tmp_path, monitors, policy)
    child = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import sys,time;print('ready',flush=True);sys.stdin.readline();x=bytearray(24*1024*1024);t=time.monotonic()+1.3\nwhile time.monotonic()<t:sum(range(10000))\nprint('done',flush=True);sys.stdin.readline()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        created = psutil.Process(child.pid).create_time()
        pe.register_process(role, child.pid, created)
        wait_until(
            lambda: any(
                f"{role}:{child.pid}" in r["resources"] for r in samples(tmp_path)
            )
        )
        child.stdin.write("go\n")
        child.stdin.flush()
        wait_until(
            lambda: any(
                any(
                    d["role"] == role and d["pid"] == child.pid
                    for d in r.get("anomaly_details", [])
                )
                for r in samples(tmp_path)
            )
        )
        assert child.stdout.readline().strip() == "done"
        child.stdin.write("exit\n")
        child.stdin.flush()
        child.wait(timeout=3)
        time.sleep(0.5)
        m.close()
        new = start(tmp_path, monitors, policy)
        new.close()
        pack = desktop(tmp_path, monkeypatch)
        index = json.loads(pack["performance/index.json"])
        assert any(
            any(f":{role}:{child.pid}@{created}" in reason for reason in e["reasons"])
            for e in index["events"]
        )
        assert any(
            b"process_removed" in raw
            for n, raw in pack.items()
            if n.startswith("performance/") and n.endswith(".jsonl")
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)


def test_formal_local_barrier_drain_success_and_failure_evidence(
    setup, monitors, monkeypatch
):
    root, scope, receive = setup
    monitor = start(root, monitors)
    project(receive)
    rt = HookFormalRuntime.__new__(HookFormalRuntime)
    rt.scope = receive
    rt.fault_check = lambda: False
    rt.credentials = NS(
        worker_id=scope.worker_id, client_instance_id=scope.client_instance_id
    )
    rt.projection = NativeProjectionStore(max_projection_bytes=1048576, max_entries=100)
    rt.outbox = HookDeliveryOutbox(max_storage_bytes=1048576)

    # Network boundary only: actual local drain and durable receipt stay real.
    def receipt(binding):
        claim = rt.outbox.claim_next(scope, now=NOW, lease_seconds=30)
        if claim:
            assert rt.outbox.mark_stored(claim, rules.receipt(claim.envelope), now=NOW)

    rt.drain_pending = receipt
    binding = {
        "binding_id": scope.binding_id,
        "binding_revision": 1,
        "peer_wxid": scope.peer_wxid,
        "conversation_id": str(uuid.uuid4()),
    }
    assert rt.barrier(binding) == (1, 0)
    rt.drain(binding)
    rt.barrier()  # global scan is also recorded separately
    db = root / "worker_client.sqlite3"
    original = db.read_bytes()
    # Repeat the same cached-view barrier; no new query solely for diagnostics.
    assert rt.barrier(binding) == (1, 0)
    assert db.read_bytes() == original
    rt.process = NS(generation=receive.process_generation)
    binding["history_since_create_time"] = "1"

    def unexpected_sync(*args, **kwargs):
        pytest.fail("cache hit unexpectedly read Hook history")

    rt.sync = unexpected_sync
    with history_read_cycle(rt):
        result = {"history_ready": True, "history_state_digest": "synthetic-digest"}
        rt._remember_history(binding, result, (1, 0), time.monotonic())
        assert rt._history_before_claim(binding) == result
    assert db.read_bytes() == original
    with storage.db_connection() as conn:
        conn.execute(
            "UPDATE hook_observation_projection SET projection_digest='corrupt'"
        )
        conn.commit()
    corrupt = db.read_bytes()
    with pytest.raises(ValueError, match="PROJECTION_EVIDENCE_CHANGED"):
        rt.barrier(binding)
    assert db.read_bytes() == corrupt
    time.sleep(0.25)
    monitor.close()
    pack = desktop(root, monkeypatch)
    events = [
        e
        for n, raw in pack.items()
        if n.startswith("performance/") and n.endswith(".jsonl")
        for line in raw.splitlines()
        for e in json.loads(line)["snapshot"]["events"]
    ]
    scans = [
        e
        for e in events
        if e.get("name") in {"local.binding_barrier", "local.drain_scan"}
        and e.get("status") != "running"
    ]
    assert scans and all(
        e["conversation_id"] == binding["conversation_id"] for e in scans
    )
    assert all(
        e["rows"] == 1 and e["bytes"] > 0 and e["duration_ms"] >= 0 for e in scans
    )
    assert any(e["status"] == "failed" and e["validated_rows"] == 0 for e in scans)
    assert any(
        e["name"] == "local.drain_scan"
        and e["status"] == "completed"
        and e["validated_rows"] == 1
        for e in scans
    )
    assert db.read_bytes() == corrupt


def test_frozen_index_capacity_is_explicit_and_capture_recovers(tmp_path):
    policy = replace(POLICY, budget_bytes=64 * 1024)
    store = Store(tmp_path / "artifacts/performance", "current", policy)
    store.index["sessions"]["current"] = {"status": "running"}
    # Pure capacity boundary: unresolved metadata cannot be silently compacted.
    for i in range(40):
        store.index["events"].append(
            {
                "id": str(i),
                "session": "old",
                "first": 0,
                "last": 0,
                "end": None,
                "reasons": ["memory_growth"],
                "padding": "x" * 80,
            }
        )
    store.event(["sustained_cpu"], 100, 100)
    assert store.index["event_capture_degraded"] is True
    frozen = json.dumps(store.index["events"])
    assert store.append({"monotonic": 100}, 100) is False
    assert json.dumps(store.index["events"]) == frozen
    assert any(
        g["reason"] == "protected_index_capacity_exhausted" for g in store.index["gaps"]
    )
    # Closed events with no retained segment may retire by the normal policy.
    for e in store.index["events"]:
        e["end"] = 1
    store.event([], POLICY.retention_seconds + 2, POLICY.retention_seconds + 2)
    assert not store.index.get("event_capture_degraded")
    assert store.append(
        {"monotonic": POLICY.retention_seconds + 2}, POLICY.retention_seconds + 2
    )
    assert store.index["retired_events"] == 40


def test_export_bad_policy_reports_gap_and_preserves_available_segments(tmp_path):
    root = tmp_path / "artifacts/performance"
    store = Store(root, "test")
    store.index["sessions"]["test"] = {"status": "running"}
    store.append({"monotonic": 1}, 1)
    store.index["policy"]["window_seconds"] = "invalid"
    store.index["events"] = [{"session": "test", "first": 1, "last": 1, "end": None}]
    store.save_index()
    pack = exported(export_bundle(tmp_path, tmp_path / "invalid-policy.zip"))
    manifest = json.loads(pack["收集清单.json"])
    assert any(
        e.get("reason") == "index_missing_or_corrupt" for e in manifest["sources"]
    )
    assert any(
        name.startswith("performance/unindexed/") and name.endswith(".jsonl")
        for name in pack
    )
