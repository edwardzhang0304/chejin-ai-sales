"""One scoped calibration across real lifecycle and business authorization gates."""
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
import json

import pytest

from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from test_hook_worker_services import runtime
from test_hook_gateway import lab
from test_hook_backend import peer as backend_peer


def calibrated(runtime, tmp_path, monkeypatch):
    runner, services, binding, _, errors, _, posted, _ = runtime
    runner.binding = binding
    services.attach(binding, runner._hook_persistence_failed)
    waits = []
    services.dispatcher = SimpleNamespace(automatic_transcription_disabled=lambda:True,
        prepare_startup_window=lambda *a:None, _report_wait=lambda *a,**kw:waits.append(a))
    monkeypatch.setattr('chejin_worker_client.hook_worker_services.inspect_windows_window_geometry',
        lambda _:{'ok':True,'geometry':{'left':15,'top':15,'right':1075,'bottom':755,'width':1060,'height':740}})
    runner.tick_once()
    prepared = services.connection.prepared
    client = services.connection.backend
    business = HookFormalRuntime(client, services._credentials, services.connection.gateway,
        prepared, tmp_path/'business')
    assert business.clock is prepared.clock and prepared.clock.anchor is not None
    clock = prepared.clock
    anchor = clock.anchor
    now = [anchor[1]]
    monkeypatch.setattr(clock, 'monotonic', lambda:now[0])
    return runner, services, business, prepared, now, anchor, waits, errors, posted


def sample(prepared, now, elapsed, rtt):
    end = prepared.account.response_received_monotonic + elapsed
    now[0] = end
    return replace(prepared.account,
        server_time_utc=(datetime.fromisoformat(prepared.account.server_time_utc)+timedelta(seconds=elapsed)).isoformat(),
        request_started_monotonic=end-rtt, response_received_monotonic=end)


def test_valid_time_shared_by_connection_business_and_formal_heartbeat(runtime, tmp_path, monkeypatch):
    runner, services, business, prepared, now, anchor, waits, errors, posted = calibrated(runtime,tmp_path,monkeypatch)
    slow = sample(prepared, now, 10, 2)
    monkeypatch.setattr(services.connection, 'prepare', lambda *a:replace(prepared,account=slow))
    business.identity_client = SimpleNamespace(heartbeat=lambda *a:slow)
    runner.tick_once()
    assert services.startup_check_state()['canStart']
    business.clock.observe(slow)  # Dispatcher's observation of the same snapshot.
    assert business.heartbeat(passive=True) is slow
    assert business.clock.anchor == anchor and not waits and not errors
    assert runner.binding.run_status == 'paused' and not posted
    # Refresh through business; connection can use it even if its own snapshot is older.
    fresh = sample(prepared, now, 100, .05)
    business.identity_client = SimpleNamespace(heartbeat=lambda *a:fresh)
    business.heartbeat(passive=True)
    renewed = business.clock.anchor
    now[0] += 30
    runner.tick_once()
    assert business.clock.anchor == renewed and services.startup_check_state()['canStart']
    assert runner.binding.run_status == 'paused'


def test_shared_old_time_still_expires_and_waits_without_auto_resume(runtime,tmp_path,monkeypatch):
    runner, services, business, prepared, now, anchor, waits, errors, posted = calibrated(runtime,tmp_path,monkeypatch)
    slow = sample(prepared, now, 121, 2)
    monkeypatch.setattr(services.connection, 'prepare', lambda *a:replace(prepared,account=slow))
    runner.tick_once()
    assert not services.startup_check_state()['canStart']
    assert waits == [('HOOK_BACKEND_CLOCK_STALE',)]
    assert business.clock.anchor == anchor and not errors and not posted
    fresh = sample(prepared, now, 122, .05)
    monkeypatch.setattr(services.connection, 'prepare', lambda *a:replace(prepared,account=fresh))
    runner.tick_once()
    assert services.startup_check_state()['canStart'] and runner.binding.run_status == 'paused'


@pytest.mark.parametrize('change',['paused','blocked','epoch','rebind'])
def test_valid_shared_time_never_reuses_business_authorization(runtime,tmp_path,monkeypatch,change):
    _, services, business, prepared, now, anchor, *_ = calibrated(runtime,tmp_path,monkeypatch)
    slow = sample(prepared, now, 10, 2)
    if change!='paused':
        profile=json.loads(slow.worker_profile_json);profile['run_status']='running'
        slow=replace(slow,run_status='running',worker_profile_json=json.dumps(profile))
    if change=='blocked':slow=replace(slow,blocked=True)
    if change=='epoch':slow=replace(slow,account_epoch='different-account-epoch')
    if change=='rebind':slow=replace(slow,rebind_required=True)
    business.identity_client = SimpleNamespace(heartbeat=lambda *a:slow)
    with pytest.raises(ValueError,match='HOOK_RUNTIME_NOT_RUNNING'):
        business.heartbeat(new_work=True)
    assert business.clock.anchor == anchor
