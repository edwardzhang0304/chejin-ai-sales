"""Opt-in D2 observer around unchanged production-entry integration tests.

Usage: pytest -p memory_business_probe --memory-business-mode=off|on
--memory-business-output=/private/path. No model/network outside local fixtures.
The HTTP backend shares the test process: tracemalloc costs include it. The
scheduler pulse below is not a Windows or Qt UI responsiveness measurement.
"""
import functools
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import tracemalloc
import uuid

import psutil
import pytest


def pytest_addoption(parser):
    parser.addoption('--memory-business-mode', choices=('off', 'on'), required=True)
    parser.addoption('--memory-business-output', required=True)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    from chejin_worker_client import memory_capture as mc, performance_evidence as pe
    from chejin_worker_client.performance_contract import POLICY
    from chejin_worker_client.diagnostic_bundle import export_bundle
    from chejin_worker_client.hook_formal_client import HookFormalClient
    from chejin_worker_client.hook_gateway import HookGateway
    from chejin_worker_client.hook_inbox import NativeInbox
    from chejin_worker_client.hook_projection import NativeProjectionStore
    from chejin_worker_client.hook_outbox import HookDeliveryOutbox
    from chejin_worker_client.hook_formal_runtime import HookFormalRuntime

    mode = item.config.getoption('--memory-business-mode')
    folder = Path(item.config.getoption('--memory-business-output')) / mode
    folder.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(item.nodeid.encode()).hexdigest()[:16]
    root = folder / key
    (root / 'runtime').mkdir(parents=True, exist_ok=True)
    (root / 'runtime/session.json').write_text(json.dumps({
        'session_id': uuid.uuid4().hex, 'pid': os.getpid(), 'status': 'running'}))
    monitor = pe.install(root, policy=POLICY)
    assert monitor is not None
    stop = threading.Event()
    pulses, rss, spans, results = [], [], [], []
    process = psutil.Process()
    def pulse():
        while not stop.wait(.05):
            now = time.monotonic()
            if len(pulses) < 2048:
                pulses.append(now)
                rss.append(process.memory_info().rss)
            # Controlled observer input; genuine UI coverage is D1's Qt actor.
            pe.emit('ui', at=now)
    pulse_thread = threading.Thread(target=pulse, daemon=True)
    pulse_thread.start()
    deadline = time.monotonic() + POLICY.sample_seconds * 2 + 5
    while monitor.capture_pressure() is not None and time.monotonic() < deadline:
        time.sleep(.05)
    assert monitor.capture_pressure() is None, monitor.capture_pressure()
    coordinator = mc.install(root, monitor)
    assert coordinator
    request_thread = None
    activation_wait = 0.
    def activate():
        nonlocal request_thread, activation_wait
        if mode != 'on' or request_thread is not None:
            return
        activation_start = time.monotonic()
        # Test driver waits for the real load gate after fixture construction;
        # production thresholds/requests and business timeouts stay unchanged.
        deadline = time.monotonic() + 5
        while monitor.capture_pressure() is not None and time.monotonic() < deadline:
            time.sleep(.05)
        assert monitor.capture_pressure() is None, monitor.capture_pressure()
        request_thread = threading.Thread(target=lambda: results.append(mc.request_capture(root)), daemon=True)
        request_thread.start()
        deadline = time.monotonic() + 5
        while not tracemalloc.is_tracing() and not results and time.monotonic() < deadline:
            time.sleep(.01)
        activation_wait = time.monotonic() - activation_start
        assert tracemalloc.is_tracing(), results
    patch = pytest.MonkeyPatch()
    def observe(owner, name):
        original = getattr(owner, name)
        @functools.wraps(original)
        def measured(*args, **kwargs):
            activate()
            start = time.monotonic()
            tracing = tracemalloc.is_tracing()
            try:
                return original(*args, **kwargs)
            finally:
                if len(spans) < 4096:
                    spans.append({'operation': owner.__name__ + '.' + name,
                                  'seconds': time.monotonic() - start, 'tracing_at_start': tracing})
                pe.emit('loop', name='business_probe')
        patch.setattr(owner, name, measured)
    for owner, names in (
        (HookFormalClient, ('call',)), (HookGateway, ('_response',)),
        (NativeInbox, ('persist',)), (NativeProjectionStore, ('project_next',)),
        (HookDeliveryOutbox, ('mark_stored',)),
        (HookFormalRuntime, ('sync', 'reply_once', 'finish'))):
        for name in names:
            observe(owner, name)
    cpu = process.cpu_times()
    start = time.monotonic()
    outcome = yield
    elapsed = time.monotonic() - start
    cpu_after = process.cpu_times()
    patch.undo()
    if request_thread:
        request_thread.join(42)
    stop.set()
    pulse_thread.join(1)
    coordinator.close()
    if coordinator.profile and coordinator.profile.thread:
        coordinator.profile.thread.join(3)
    monitor.close()
    assert not tracemalloc.is_tracing()
    assert not request_thread or not request_thread.is_alive()
    archive = export_bundle(root, root / 'evidence.zip', capture_request=results[0] if results else None)
    report = {
        'test': item.nodeid, 'mode': mode, 'passed': outcome.excinfo is None,
        'business_seconds': elapsed, 'spans': spans,
        'test_driver_activation_wait_seconds_included_in_business_total': activation_wait,
        'activation_point': 'first observed production entry after test fixture setup',
        'cpu_seconds': cpu_after.user + cpu_after.system - cpu.user - cpu.system,
        'rss_min': min(rss, default=0), 'rss_max': max(rss, default=0),
        'scheduler_max_gap_seconds': max((b-a for a,b in zip(pulses, pulses[1:])), default=0),
        'pulse_is_not_ui_measurement': True, 'request': results[0] if results else None,
        'sampling_policy': POLICY.payload(),
        'archive': str(archive), 'backend_shares_traced_test_process': True,
        'substitutes': ['physical Hook HTTP/WeChat SQLite', 'Windows OCR replay', 'controlled model adapter'],
        'business_assertions_modified': False}
    (folder / (key + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2))
