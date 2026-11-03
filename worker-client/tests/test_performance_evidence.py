"""PE-01: real producers -> independent process -> restart -> desktop ZIP.

Accelerated isolated tests, not a Windows packaged or 48-hour acceptance claim.
"""

import hashlib
import json
import os
from pathlib import Path
import sys
import time
import zipfile
from dataclasses import replace

import psutil
import pytest

from chejin_worker_client import performance_evidence as pe
from chejin_worker_client.performance_contract import POLICY, Anomalies
from chejin_worker_client.performance_store import Store
from chejin_worker_client.performance_sampler import Resources, acquire
from chejin_worker_client.hook_outbox import HookDeliveryOutbox
from chejin_worker_client.hook_timing import timed_call
from chejin_worker_client.shared_rules import hook_contract_rules as rules
from chejin_worker_client.diagnostic_bundle import export_bundle
from test_hook_outbox import setup, project, NOW

FAST = replace(
    POLICY,
    sample_seconds=0.08,
    snapshot_seconds=0.015,
    ui_delay_seconds=0.20,
    sustained_seconds=0.20,
    loop_delay_seconds=0.20,
    stage_delay_seconds=0.20,
    growth_bytes=2 * 1024 * 1024,
    window_seconds=0.35,
)


def wait_until(predicate, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition timed out")


def samples(root):
    data = []
    for path in (root / "artifacts" / "performance").glob("*.jsonl"):
        for line in path.read_bytes().splitlines():
            data.append(json.loads(line))
    return sorted(data, key=lambda v: (v["session"], v["seq"]))


def exported(path):
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


@pytest.fixture
def monitors():
    active = []
    yield active
    for m in active:
        m.close()


def start(root, monitors, policy=FAST):
    m = pe.install(root, policy=policy)
    assert m is not None
    monitors.append(m)
    wait_until(lambda: len(samples(root)) >= 2)
    return m


def test_real_producers_independent_capture_restart_and_desktop_export(
    setup, monitors, monkeypatch
):
    root, scope, receive = setup
    m = start(root, monitors)
    p = project(receive)
    outbox = HookDeliveryOutbox(max_storage_bytes=1048576)
    event = outbox.enqueue_projection(p["receive_seq"], scope, now=NOW)
    assert outbox.enqueue_projection(p["receive_seq"], scope, now=NOW) == event
    claim = outbox.claim_next(scope, now=NOW, lease_seconds=10)
    assert outbox.mark_stored(claim, rules.receipt(claim.envelope), now=NOW)
    assert outbox.mark_stored(claim, rules.receipt(claim.envelope), now=NOW)
    # Stage start/end within one resource interval must both survive coalescing.
    timed_call(
        "hook.send_reconcile",
        lambda: {"local_send_observed": False},
        context={"conversation_id": "synthetic-conversation"},
    )
    pe.emit("ui")
    pe.emit("loop", name="business")
    pe.emit(
        "counter",
        name="privacy",
        body="PRIVATE_BODY_MUST_NOT_APPEAR",
        token="SECRET_MUST_NOT_APPEAR",
    )
    wait_until(
        lambda: any(
            s["snapshot"].get("counters", {}).get("outbox_receipts_stored") == 1
            for s in samples(root)
        )
    )
    wait_until(lambda: any("ui_response_gap" in s["anomalies"] for s in samples(root)))
    db = root / "worker_client.sqlite3"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    m.close()
    assert not psutil.pid_exists(m.process.pid)
    first_id = m.session
    second = start(root, monitors)
    time.sleep(0.15)
    second.close()
    assert before == hashlib.sha256(db.read_bytes()).hexdigest()
    # Exercise the same async job used by the sole desktop diagnostic button.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from chejin_worker_client.diagnostic_ui import ExportJob

    app = QApplication.instance() or QApplication([])
    job = ExportJob(root)
    result = []
    errors = []
    job.completed.connect(result.append)
    job.failed.connect(errors.append)
    assert job.start()
    deadline = time.monotonic() + 10
    pulses = 0
    while not result and not errors and time.monotonic() < deadline:
        app.processEvents()
        pulses += 1
        time.sleep(0.005)
    assert not errors and result and pulses > 1
    entries = exported(result[0])
    index = json.loads(entries["performance/index.json"])
    assert {first_id, second.session} <= index["sessions"].keys()
    assert all(
        index["sessions"][sid]["status"] == "normal_exit"
        for sid in (first_id, second.session)
    )
    old = [
        json.loads(line)
        for n, raw in entries.items()
        if n.startswith("performance/" + first_id) and n.endswith(".jsonl")
        for line in raw.splitlines()
    ]
    events = [e for s in old for e in s["snapshot"]["events"]]
    assert {e["status"] for e in events if e.get("measurement_id")} >= {
        "running",
        "completed",
    }
    counters = old[-1]["snapshot"]["counters"]
    assert (
        counters["received_packages"]
        == counters["projected_packages"]
        == counters["outbox_created"]
        == counters["outbox_receipts_stored"]
        == 1
    )
    assert any(s["resources"]["worker"]["rss_bytes"] > 0 for s in old)
    assert any(s["resources"]["worker"].get("cpu_percent") is not None for s in old)
    assert index["events"] and index["events"][0]["stack_requests"] <= 3
    joined = b"\n".join(
        raw for name, raw in entries.items() if name.startswith("performance/")
    )
    assert (
        b"PRIVATE_BODY_MUST_NOT_APPEAR" not in joined
        and b"SECRET_MUST_NOT_APPEAR" not in joined
    )
    assert before == hashlib.sha256(db.read_bytes()).hexdigest()


def test_actual_memory_cpu_io_and_ui_block_capture(tmp_path, monitors):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    m = start(tmp_path, monitors, replace(FAST, cpu_percent=10))
    timer = pe.attach_ui(app)
    timer.setInterval(15)
    for _ in range(15):
        app.processEvents()
        time.sleep(0.02)
    allocation = bytearray(24 * 1024 * 1024)
    (tmp_path / "isolated-io.bin").write_bytes(allocation[: 1024 * 1024])
    with pe.measurement("synthetic.busy"):
        pe.emit("loop", name="business")
        end = time.monotonic() + 0.8
        while time.monotonic() < end:
            sum(range(1000))
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)
    del allocation
    timer.stop()
    m.close()
    rows = samples(tmp_path)
    reasons = {r for row in rows for r in row["anomalies"]}
    assert {
        "sustained_cpu",
        "ui_response_gap",
        "loop_progress_gap:business",
        "long_stage",
    } <= reasons
    # Working set may not shrink after Python frees memory. Do not call it a leak.
    assert max(r["resources"]["worker"]["rss_bytes"] for r in rows) > min(
        r["resources"]["worker"]["rss_bytes"] for r in rows
    )
    assert max(r["resources"]["worker"].get("cpu_percent") or 0 for r in rows) > 10
    assert all("io_bytes" in r["resources"]["worker"] for r in rows)


def test_detector_transient_vs_sustained_and_intentional_wait():
    a = Anomalies(POLICY)
    assert not a.check(0, {"worker": {"rss_bytes": 1, "cpu_percent": 99}}, {})
    assert not a.check(119, {"worker": {"rss_bytes": 1, "cpu_percent": 99}}, {})
    assert a.check(120, {"worker": {"rss_bytes": 1, "cpu_percent": 99}}, {}) == [
        "sustained_cpu"
    ]
    assert not a.check(121, {"worker": {"rss_bytes": 1, "cpu_percent": 0}}, {})
    assert not a.check(
        1000, {}, {"loops": {"business": {"at": 0, "waiting": "network"}}}
    )
    a.check(122, {"worker": {"rss_bytes": 1 + POLICY.growth_bytes}}, {})
    assert "memory_growth" in a.check(
        242, {"worker": {"rss_bytes": 1 + POLICY.growth_bytes}}, {}
    )
    assert "memory_growth" not in a.check(243, {"worker": {"rss_bytes": 1}}, {})


def test_process_identity_access_denied_and_pid_reuse(monkeypatch):
    resource = Resources()
    pid = os.getpid()
    created = psutil.Process().create_time()
    assert (
        resource.sample("worker", pid, created + 1, time.monotonic())["unavailable"][
            "process"
        ]
        == "pid_reused"
    )

    def denied(_):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(psutil, "Process", denied)
    value = resource.sample("worker", pid, created, time.monotonic())
    assert (
        value["unavailable"]["process"] == "AccessDenied" and "rss_bytes" not in value
    )


def test_bounded_producer_never_waits_or_changes_business(tmp_path):
    m = pe.Monitor(tmp_path)
    m.lock.acquire()
    try:
        start_time = time.monotonic()
        m.offer("ui", {})
        assert time.monotonic() - start_time < 0.1 and m.dropped == 1
    finally:
        m.lock.release()
    for _ in range(1000):
        m.offer("counter", {"name": "bounded"})
    assert len(m.pending) == 256 and m.dropped == 745
    m._drain()
    assert m.state["counters"]["bounded"] == 256


def test_sampler_crash_does_not_change_producers(tmp_path, monitors):
    m = start(tmp_path, monitors)
    m.process.kill()
    m.process.wait()
    pe.emit("counter", name="still_safe")
    wait_until(lambda: (m.root / "capture-gap.json").exists())
    assert (
        json.loads((m.root / "capture-gap.json").read_text())["reason"]
        == "sampler_exited"
    )
    m.close(clean=False)
    new = start(tmp_path, monitors)
    new.close()
    index = json.loads((m.root / "index.json").read_text())
    assert index["sessions"][m.session]["status"] == "end_unconfirmed"


@pytest.mark.parametrize('exit_point',['normal','before_poll','after_poll'])
def test_sampler_owned_pipes_close_on_normal_exit_and_exit_write_race(
    tmp_path, monitors, monkeypatch, exit_point
):
    import threading
    m=start(tmp_path,monitors)
    if exit_point=='normal':
        m.close()
    else:
        original=m.process.poll
        injected=[]
        def poll():
            result=original()
            if threading.current_thread() is m.thread and result is None and not injected:
                injected.append(True)
                m.process.kill()
                m.process.wait(timeout=3)
                # Force both branches with a real child: exit observed before
                # poll returns, or immediately after the last alive observation.
                return None if exit_point=='after_poll' else m.process.returncode
            return result
        monkeypatch.setattr(m.process,'poll',poll)
        wait_until(lambda:(m.root/'capture-gap.json').exists())
        assert injected
        gap=json.loads((m.root/'capture-gap.json').read_text())
        assert gap['reason']==('BrokenPipeError' if exit_point=='after_poll' else 'sampler_exited')
        pe.emit('counter',name='still_safe')
        assert any(item[1].get('name')=='still_safe' for item in m.pending)
        m.close(clean=False)
    wait_until(lambda:not m.thread.is_alive() and not m.reader.is_alive())
    assert m.process.stdin.closed and m.process.stdout.closed
    # Same durable directory can start a fresh collector; failed capture is
    # retained as incomplete, never promoted to a successful old session.
    new=start(tmp_path,monitors)
    new.close()
    index=json.loads((m.root/'index.json').read_text())
    if exit_point!='normal':
        assert index['sessions'][m.session]['status']=='end_unconfirmed'


def test_duplicate_sampler_lock_and_release(tmp_path):
    lock = acquire(tmp_path)
    try:
        with pytest.raises(OSError):
            acquire(tmp_path)
    finally:
        lock.close()
    again = acquire(tmp_path)
    again.close()


def test_retention_rotation_frozen_capacity_and_clock_jump(tmp_path):
    # Unit boundary simulation, not actual 72h duration.
    p = replace(
        POLICY,
        segment_bytes=4096,
        budget_bytes=32768,
        window_seconds=10,
        retention_seconds=72 * 3600,
    )
    s = Store(tmp_path, "boundary", p)
    s.index["sessions"]["boundary"] = {"status": "running"}
    for i in range(20):
        s.append({"monotonic": i, "padding": "x" * 3500}, now=1000 + i)
    assert all(m["bytes"] <= 4096 for m in s.index["segments"])
    assert s.index["retired"] > 0
    s.event(["memory_growth"], 1020, 20)
    protected = {m["name"] for m in s.index["segments"] if s._protected(m, 1020)}
    old = {n: (tmp_path / n).read_bytes() for n in protected}
    for i in range(21, 200):
        s.event(["memory_growth"], 1000 + i, i)
        s.append({"monotonic": i, "padding": "x" * 3500}, 1000 + i)
    assert all((tmp_path / n).read_bytes().startswith(v) for n, v in old.items())
    assert any(
        g["reason"] == "protected_or_active_capacity_exhausted" for g in s.index["gaps"]
    )
    assert sum(p.stat().st_size for p in tmp_path.iterdir()) <= 32768
    s.append({"monotonic": 101}, now=1000000)
    assert any(g["reason"] == "wall_clock_jump" for g in s.index["gaps"])


def test_bad_index_full_disk_and_legacy_export(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    before = exported(export_bundle(root, tmp_path / "old.zip"))
    assert any(
        e.get("reason") == "no_performance_capture_old_version_or_not_started"
        for e in json.loads(before["收集清单.json"])["sources"]
    )
    perf = root / "artifacts" / "performance"
    perf.mkdir(parents=True)
    (perf / "index.json").write_text("{")
    with pytest.raises(ValueError, match="index_unreadable"):
        Store(perf, "new")
    assert (perf / "index.json").read_text() == "{"
    bad = exported(export_bundle(root, tmp_path / "bad.zip"))
    assert any(
        e.get("reason") == "index_missing_or_corrupt"
        for e in json.loads(bad["收集清单.json"])["sources"]
    )
    (perf / "index.json").unlink()
    s = Store(perf, "disk")
    original = Path.open

    def fail(path, *args, **kwargs):
        if path.suffix == ".jsonl":
            raise OSError(28, "disk full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(OSError):
        s.append({"seq": 1})


def test_real_parent_suspended_and_killed_sampler_still_records_then_exits(tmp_path):
    """POSIX isolated Worker harness, not a Windows freeze claim."""
    if os.name == "nt":
        pytest.skip("POSIX suspend fixture")
    import signal
    import subprocess

    code = """
import time,sys
from pathlib import Path
from dataclasses import replace
from chejin_worker_client.performance_evidence import install,emit
from chejin_worker_client.performance_contract import POLICY
m=install(Path(sys.argv[1]),policy=replace(POLICY,sample_seconds=.05,snapshot_seconds=.02,ui_delay_seconds=.15))
emit('ui')
Path(sys.argv[1],'ready.json').write_text(__import__('json').dumps({'sampler':m.process.pid,'session':m.session}))
time.sleep(30)
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_until(lambda: (tmp_path / "ready.json").exists())
        ready = json.loads((tmp_path / "ready.json").read_text())
        wait_until(
            lambda: any(s["snapshot"].get("ui_monotonic") for s in samples(tmp_path))
        )
        os.kill(parent.pid, signal.SIGSTOP)
        count = len(samples(tmp_path))
        time.sleep(0.4)
        rows = samples(tmp_path)
        assert len(rows) > count
        assert any("worker_snapshot_gap" in s["anomalies"] for s in rows)
        parent.kill()
        parent.wait(timeout=3)
        index_path = tmp_path / "artifacts" / "performance" / "index.json"
        wait_until(
            lambda: json.loads(index_path.read_text())["sessions"][ready["session"]][
                "status"
            ]
            == "end_unconfirmed"
        )
        wait_until(lambda: not psutil.pid_exists(ready["sampler"]))
        index = json.loads(index_path.read_text())
        assert (
            index["events"][-1]["stack_status"]
            == "partial_or_unavailable_worker_did_not_respond"
        )
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()


def test_continuous_real_samples_over_1000_survive_export(tmp_path, monitors):
    p = replace(
        FAST,
        sample_seconds=0.003,
        snapshot_seconds=0.003,
        window_seconds=600,
        ui_delay_seconds=30,
        loop_delay_seconds=30,
    )
    m = start(tmp_path, monitors, p)
    # Real independent resource samples and writes; only cadence accelerated.
    wait_until(
        lambda: sum(
            v["rows"]
            for v in json.loads((m.root / "index.json").read_text())["segments"]
        )
        > 1005,
        seconds=15,
    )
    m.close()
    entries = exported(export_bundle(tmp_path, tmp_path / "many.zip"))
    count = sum(
        len(raw.splitlines())
        for name, raw in entries.items()
        if name.startswith("performance/") and name.endswith(".jsonl")
    )
    assert count > 1000
    assert (
        len(
            [
                name
                for name in entries
                if name.startswith("performance/") and name.endswith(".jsonl")
            ]
        )
        > 1
    )


def test_72h_retention_and_real_default_64mib_budget(tmp_path):
    # Accelerated clock boundary, actual production byte/segment bounds.
    s = Store(tmp_path, "retention")
    s.index["sessions"]["retention"] = {"status": "running"}
    s.append({"monotonic": 0}, now=1000)
    s.append(
        {"monotonic": POLICY.retention_seconds + 1},
        now=1000 + POLICY.retention_seconds + 1,
    )
    s.append(
        {"monotonic": POLICY.retention_seconds + 2},
        now=1000 + POLICY.retention_seconds + 2,
    )
    assert s.index["retired"] >= 1
    s.event(
        ["memory_growth"],
        1000 + POLICY.retention_seconds + 3,
        POLICY.retention_seconds + 3,
    )
    for i in range(75):
        s.append(
            {
                "monotonic": POLICY.retention_seconds + 4 + i,
                "padding": "X" * (POLICY.segment_bytes - 200),
            },
            now=1000 + POLICY.retention_seconds + 4 + i,
        )
    assert all(
        p.stat().st_size <= POLICY.segment_bytes for p in tmp_path.glob("*.jsonl")
    )
    assert sum(p.stat().st_size for p in tmp_path.iterdir()) <= POLICY.budget_bytes
    assert any(
        g["reason"] == "protected_or_active_capacity_exhausted" for g in s.index["gaps"]
    )


def test_corrupt_lines_missing_segments_and_export_budget_explain_gaps(
    tmp_path, monitors
):
    m = start(tmp_path, monitors)
    time.sleep(0.1)
    m.close()
    original = samples(tmp_path)
    segment = next(m.root.glob("*.jsonl"))
    # Damage an actual generated segment, rather than manufacturing evidence.
    with segment.open("ab") as stream:
        stream.write(b'{bad middle}\n{"unfinished":')
    entries = exported(export_bundle(tmp_path, tmp_path / "damaged.zip"))
    manifest = json.loads(entries["收集清单.json"])
    assert (
        sum(e.get("reason") == "invalid_jsonl_line" for e in manifest["sources"]) >= 2
    )
    exported_samples = sum(
        len(raw.splitlines())
        for n, raw in entries.items()
        if n.startswith("performance/") and n.endswith(".jsonl")
    )
    assert exported_samples == len(original)
    tiny = exported(export_bundle(tmp_path, tmp_path / "tiny.zip", max_bytes=128))
    assert any(
        e.get("reason") == "segment_not_exported"
        for e in json.loads(tiny["收集清单.json"])["sources"]
    )
    segment.unlink()
    missing = exported(export_bundle(tmp_path, tmp_path / "missing.zip"))
    assert any(
        e.get("reason") == "segment_not_exported"
        for e in json.loads(missing["收集清单.json"])["sources"]
    )


def test_export_target_failure_keeps_receipts_and_original_zip(tmp_path, monitors):
    m = start(tmp_path, monitors)
    m.close()
    target = tmp_path / "original.zip"
    target.write_bytes(b"ORIGINAL")
    before = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in m.root.iterdir()
        if p.is_file()
    }
    with pytest.raises(FileExistsError):
        export_bundle(tmp_path, target)
    assert target.read_bytes() == b"ORIGINAL"
    assert before == {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in m.root.iterdir()
        if p.is_file()
    }


@pytest.mark.parametrize("crash", [False, True])
def test_worker_main_owns_sampler_lifecycle(tmp_path, monkeypatch, crash):
    """Real main install/finally and child entry; UI/business boundaries isolated."""
    from types import SimpleNamespace
    from chejin_worker_client import main, config, release_transport, hook_installation
    from chejin_worker_client import update_data_access, runtime_supervision, ui

    monkeypatch.setattr(sys, "argv", ["worker"])
    monkeypatch.setenv("CHEJIN_WORKER_UI_MODE", "pyside")
    monkeypatch.setattr(
        config,
        "CONFIG",
        SimpleNamespace(app_dir=tmp_path, api_base_url="https://synthetic.invalid"),
    )
    monkeypatch.setattr(
        main, "acquire_single_instance", lambda: SimpleNamespace(release=lambda: None)
    )
    monkeypatch.setattr(update_data_access, "acquire_data_access", lambda root: None)
    monkeypatch.setattr(update_data_access, "clear_update_writer", lambda: None)
    monkeypatch.setattr(release_transport, "packaged_transport", lambda: "hook")
    monkeypatch.setattr(
        hook_installation,
        "launch_information",
        lambda: {"api_base_url": "https://synthetic.invalid"},
    )
    monkeypatch.setattr(
        runtime_supervision, "install_runtime_supervision", lambda: None
    )
    monkeypatch.setattr(
        runtime_supervision, "mark_runtime_clean_exit", lambda code: None
    )
    actual_install = pe.install
    monkeypatch.setattr(pe, "install", lambda root: actual_install(root, policy=FAST))
    owned = []

    def isolated_ui():
        owned.append(pe._ACTIVE)
        with pe.measurement("synthetic.main_lifecycle", {"task_id": "synthetic-task"}):
            pe.emit("ui")
            wait_until(lambda: len(samples(tmp_path)) >= 2)
        if crash:
            raise RuntimeError("synthetic UI failure")
        return 0

    monkeypatch.setattr(ui, "run_app", isolated_ui)
    if crash:
        with pytest.raises(RuntimeError, match="synthetic UI failure"):
            main.main()
    else:
        assert main.main() == 0
    assert pe._ACTIVE is None and owned[0].process.poll() is not None
    index = json.loads((owned[0].root / "index.json").read_text())
    assert index["sessions"][owned[0].session]["status"] == (
        "end_unconfirmed" if crash else "normal_exit"
    )


def test_wall_clock_correction_cannot_age_out_frozen_window(tmp_path):
    store = Store(tmp_path, "clock")
    store.index["sessions"]["clock"] = {"status": "running"}
    store.append({"monotonic": 0}, now=1000)
    store.event(["long_stage"], 1001, 1)
    store.append({"monotonic": 1}, now=1001)
    store.event([], 1602, 602)
    original = store.index["segments"][0]["name"]
    store.append({"monotonic": 603}, now=1000000)
    store.append({"monotonic": 604}, now=1000001)
    assert (tmp_path / original).exists()
    assert store.index["retention_clock_uncertain"] is True


def test_gauge_loop_cardinality_and_permission_denial(tmp_path, monkeypatch):
    m = pe.Monitor(tmp_path)
    for batch in range(5):
        for i in range(100):
            m.offer("gauge", {"name": f"g{batch}-{i}", "count": 1})
            m.offer("loop", {"name": f"l{batch}-{i}"})
        m._drain()
    assert len(m.state["gauges"]) <= 67 and len(m.state["loops"]) <= 32
    assert m.state["producer_dropped"] > 0
    original = Path.mkdir

    def deny(path, *args, **kwargs):
        if path.name == "performance":
            raise PermissionError("synthetic")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny)
    assert pe.install(tmp_path) is None
    assert pe._ACTIVE is None


def test_dropped_producer_events_are_explained_in_desktop_zip(tmp_path, monitors):
    m = start(tmp_path, monitors)
    m.lock.acquire()
    try:
        for _ in range(20):
            pe.emit("counter", name="drop_fixture")
    finally:
        m.lock.release()
    wait_until(
        lambda: any(
            s["snapshot"].get("producer_dropped", 0) >= 20 for s in samples(tmp_path)
        )
    )
    m.close()
    entries = exported(export_bundle(tmp_path, tmp_path / "dropped.zip"))
    manifest = json.loads(entries["收集清单.json"])
    assert manifest["partial"] is True
    assert any(
        e.get("details", {}).get("reason") == "producer_events_dropped"
        for e in manifest["sources"]
    )
