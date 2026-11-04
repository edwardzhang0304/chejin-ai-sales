"""Real gateway/HTTP/lease/ledger/ZIP; OS counters and DLL responses synthetic."""
import json
import time
import zipfile
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_hook_gateway import lab
from chejin_worker_client import hook_memory, storage, ui_lock
from chejin_worker_client.hook_active_calls import unresolved
from chejin_worker_client.hook_gateway import HookLocalError


def rows():
    with storage.db_connection() as db:
        return [dict(row) for row in db.execute('SELECT * FROM hook_transport_events ORDER BY rowid')]


def counters(monkeypatch, process):
    values = {'wechat': 1000, 'caller': 2000}

    def fake_process(pid):
        role = 'wechat' if pid == process.pid else 'caller'
        return SimpleNamespace(create_time=lambda: process.started_at if role == 'wechat' else 1.,
            memory_info=lambda: SimpleNamespace(rss=values[role] // 2, private=values[role]))
    monkeypatch.setattr(hook_memory.psutil, 'Process', fake_process)
    return values


def test_business_call_memory_reaches_desktop_zip_without_diagnostic_http(lab, monkeypatch, tmp_path):
    from chejin_worker_client.diagnostic_bundle import export_bundle
    server, gateway, process, *_ = lab
    values = counters(monkeypatch, process)
    def handled(route):
        open_event = json.loads(rows()[-1]['evidence'])
        assert open_event['phase'] == 'in_flight'
        assert open_event['memory']['before']['wechat']['private_bytes'] == 1000
        assert open_event['memory']['after'] is None
        values['wechat'] += 4096
        values['caller'] -= 128
    server.on_request = handled
    assert gateway.login_status(process).logged_in
    event = rows()[-1]
    evidence = json.loads(event['evidence'])
    memory = evidence['memory']
    assert memory['delta_bytes']['wechat']['private_bytes'] == 4096
    assert memory['delta_bytes']['caller']['private_bytes'] == -128
    assert memory['before']['dll_sha256'] == process.dll_sha256
    assert memory['before']['process_generation'] == process.generation
    assert memory['attribution'] == 'process_net_change_not_dll_allocation'
    assert not memory['peak_measured'] and not memory['delayed_release_measured']
    archive = export_bundle(storage.APP_DIR, tmp_path / 'memory.zip')
    with zipfile.ZipFile(archive) as zipped:
        exported = json.loads(zipped.read('state/hook_transport_events.json'))['rows']
        received = next(row for row in exported if row['event_id'] == event['event_id'])
        assert json.loads(received['evidence'])['memory'] == memory
    assert [r[0] for r in server.requests] == ['/IsLoginStatus']
    from chejin_worker_client.hook_read_retry import complete_transport
    with pytest.raises(RuntimeError, match='NOT_OPEN'):
        complete_transport(event['event_id'], {'phase': 'finished'})
    assert rows()[-1] == event


@pytest.mark.parametrize('route', ['DiySceneStats', 'SwapMemory'])
def test_explicit_interfaces_share_gateway_lease_identity_and_evidence(lab, monkeypatch, route):
    server, gateway, process, *_ = lab
    values = counters(monkeypatch, process)
    server.raw_override = json.dumps({'code': 0, 'data': {'created': 9, 'released': 7},
        'msg': 'private customer body', 'secret_customer_field': 'token'}).encode()
    server.on_request = lambda _: values.update(wechat=500)
    with pytest.raises(HookLocalError, match='NOT_AUTHORIZED'):
        gateway.memory_diagnostic(process, route)
    assert not server.requests
    result = gateway.memory_diagnostic(process, route, operator_requested=True)
    assert result['status'] == 'semantics_unverified'
    assert result['allocation_bytes_known'] is False
    assert result['counters'][0] == {'field': 'data.created', 'value': 9,
                                    'units': 'unverified', 'coverage': 'unverified'}
    assert [r[0:2] for r in server.requests] == [('/' + route, {})]
    assert server.lock_observations[0]['locked']
    assert not unresolved(process.generation)
    record = json.loads(rows()[-1]['evidence'])
    assert record['dll_diagnostic'] == result
    assert record['memory']['delta_bytes']['wechat']['private_bytes'] == -500
    assert 'private customer' not in json.dumps(record)
    assert 'secret_customer_field' not in json.dumps(record)
    if route == 'SwapMemory':
        assert result['effect'] == 'working_set_only_not_leak_repair'


def test_stats_timeout_keeps_original_unknown_call_and_does_not_repeat(lab, monkeypatch):
    server, gateway, process, *_ = lab
    counters(monkeypatch, process)
    gateway.timeout_seconds = .05
    server.on_request = lambda _: time.sleep(.15)
    with pytest.raises(HookLocalError):
        gateway.memory_diagnostic(process, 'DiySceneStats', operator_requested=True)
    assert unresolved(process.generation)
    event = rows()[-1]
    evidence = json.loads(event['evidence'])
    assert evidence['delivery'] == 'unknown'
    assert evidence['dll_diagnostic']['status'] == 'response_unavailable'
    assert 'memory' in evidence
    with pytest.raises(HookLocalError, match='QUARANTINED'):
        gateway.memory_diagnostic(process, 'SwapMemory', operator_requested=True)
    assert len(server.requests) == 1 and rows()[-1] == event


def test_probe_cannot_cross_process_change_or_action_lease(lab):
    server, gateway, process, *_ = lab
    with pytest.raises(HookLocalError, match='NOT_AUTHORIZED'):
        gateway._response_http('SwapMemory', {})
    lease = ui_lock.acquire_ui_lock(operation_type='add_friend', owner='C1', timeout_seconds=.1)
    try:
        with pytest.raises(ui_lock.UiLockError):
            gateway.memory_diagnostic(process, 'DiySceneStats', operator_requested=True)
    finally:
        lease.release()
    server.current_process = replace(process, started_at=process.started_at + 1)
    with pytest.raises(HookLocalError, match='GENERATION_CHANGED'):
        gateway.memory_diagnostic(process, 'DiySceneStats', operator_requested=True)
    assert server.requests == []


def test_counter_access_failure_does_not_change_business_result(lab, monkeypatch):
    server, gateway, process, *_ = lab
    def denied(_):
        raise PermissionError('must not appear in evidence')
    monkeypatch.setattr(hook_memory.psutil, 'Process', denied)
    assert gateway.login_status(process).logged_in
    evidence = json.loads(rows()[-1]['evidence'])
    assert evidence['memory']['delta_bytes']['wechat']['private_bytes'] is None
    assert evidence['memory']['before']['wechat']['unavailable']['process'] == 'PermissionError'
    assert 'must not appear' not in json.dumps(evidence)
    assert len(server.requests) == 1


def test_pid_reuse_and_unavailable_private_commit_are_not_zero(monkeypatch):
    monkeypatch.setattr(hook_memory.psutil, 'Process', lambda pid:
        SimpleNamespace(create_time=lambda: 2., memory_info=lambda: SimpleNamespace(rss=80)))
    before = hook_memory._sample(100, 1.)
    after = hook_memory._sample(100, 2.)
    assert before['unavailable']['process'] == 'pid_reused'
    assert after['private_bytes'] is None and after['rss_bytes'] == 80
    result = hook_memory.comparison({'caller': before, 'wechat': before},
                                   {'caller': after, 'wechat': after})
    assert result['delta_bytes']['wechat'] == {'rss_bytes': None, 'private_bytes': None}


@pytest.mark.parametrize('payload,status', [({'code': 404}, 'provider_rejected'),
    ({'code': 0, 'data': '1'}, 'semantics_unverified'), (['1'], 'unsupported_shape')])
def test_unknown_provider_contract_is_not_reported_as_released(payload, status):
    result = hook_memory.provider_summary('DiySceneStats', json.dumps(payload).encode())
    assert result['status'] == status and result['counters'] == []
    assert result['allocation_bytes_known'] is False


def test_engineering_entry_invokes_actual_gateway_but_trim_requires_explicit_flag(lab, monkeypatch):
    from chejin_worker_client import hook_memory_probe
    server, gateway, process, *_ = lab
    monkeypatch.setattr(hook_memory_probe, 'load_hook_deployment', lambda: gateway.deployment)
    monkeypatch.setattr(hook_memory_probe, 'HookGateway', lambda *a, **k: gateway)
    with pytest.raises(SystemExit):
        hook_memory_probe.main(['trim-working-set'])
    assert not server.requests
    assert hook_memory_probe.main(['stats']) == 0
    assert hook_memory_probe.main(['trim-working-set', '--confirm-working-set-only']) == 0
    assert [r[0] for r in server.requests] == ['/DiySceneStats', '/SwapMemory']


def test_interrupted_call_keeps_pre_call_memory_in_export(lab, monkeypatch, tmp_path):
    from chejin_worker_client import hook_transport, diagnostic_bundle
    server, gateway, process, *_ = lab
    counters(monkeypatch, process)
    def interrupted(*_):
        # Abrupt boundary interruption cannot be completed as a response; model
        # process interruption, not a successful DLL or Windows OOM experiment.
        raise KeyboardInterrupt()
    monkeypatch.setattr(hook_transport, 'post', interrupted)
    with pytest.raises(KeyboardInterrupt):
        gateway.memory_diagnostic(process, 'DiySceneStats', operator_requested=True)
    event = rows()[-1]
    evidence = json.loads(event['evidence'])
    assert evidence['phase'] == 'in_flight' and unresolved(process.generation)
    assert evidence['memory']['before']['wechat']['private_bytes'] == 1000
    assert evidence['memory']['after'] is None and evidence['memory']['delta_bytes'] is None
    path = diagnostic_bundle.export_bundle(storage.APP_DIR, tmp_path / 'interrupted.zip')
    with zipfile.ZipFile(path) as zipped:
        exported = json.loads(zipped.read('state/hook_transport_events.json'))['rows']
        assert any(r['event_id'] == event['event_id'] for r in exported)
    assert rows()[-1] == event and server.requests == []


def test_frozen_dispatch_uses_probe_entry_without_starting_business(lab, monkeypatch):
    from test_packaging_entry import load_entry_module
    from chejin_worker_client import hook_memory_probe
    server, gateway, *_ = lab
    monkeypatch.setattr(hook_memory_probe, 'load_hook_deployment', lambda: gateway.deployment)
    monkeypatch.setattr(hook_memory_probe, 'HookGateway', lambda *a, **k: gateway)
    entry = load_entry_module()
    monkeypatch.setattr(entry.sys, 'argv', ['Worker.exe', '--hook-memory-probe', 'stats'])
    assert entry._load_main()() == 0
    assert [r[0] for r in server.requests] == ['/DiySceneStats']


def test_engineering_probe_rejects_running_worker_before_any_hook_call(lab, monkeypatch):
    from chejin_worker_client import hook_memory_probe
    from chejin_worker_client.single_instance import SingleInstanceAlreadyRunning
    server, *_ = lab
    def running():
        raise SingleInstanceAlreadyRunning()
    monkeypatch.setattr(hook_memory_probe, 'acquire_single_instance', running)
    with pytest.raises(SingleInstanceAlreadyRunning):
        hook_memory_probe.main(['stats'])
    assert server.requests == []
