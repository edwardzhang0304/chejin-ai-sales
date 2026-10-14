from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import pytest
from chejin_worker_client.hook_clock import HookBackendClock
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.hook_c1_flow import SubmitJournal

STAMP = datetime(2026,10,11,4,32,40,tzinfo=timezone.utc)

def receipt(stamp=STAMP,start=10,end=10.05):
    return SimpleNamespace(server_time_utc=stamp.isoformat(),request_started_monotonic=start,response_received_monotonic=end)

def test_heartbeat_clock_ignores_windows_wall_clock_and_records_original(tmp_path,monkeypatch):
    tick=[10.05];clock=HookBackendClock(monotonic=lambda:tick[0]);clock.observe(receipt())
    monkeypatch.setattr('time.time',lambda:STAMP.timestamp()+16)
    tick[0]=10.55
    assert clock.now()==STAMP+timedelta(seconds=.5,microseconds=-1)
    journal=SubmitJournal(tmp_path/'attempt.json',clock=clock);journal.begin('13800000003','原申请','CJTEST01')
    import json
    value=json.loads(journal.path.read_text())
    assert value['time_utc']==(STAMP+timedelta(seconds=.5,microseconds=-1)).timestamp()
    assert value['local_time_utc']==STAMP.timestamp()+16
    assert value['clock_source']=='authenticated_backend_heartbeat'
    assert clock.upper_now()==STAMP+timedelta(seconds=.55,microseconds=1)

@pytest.mark.parametrize('change,code',[
    ('missing','CLOCK_UNCONFIRMED'),('slow','CLOCK_RTT_UNCONFIRMED'),
    ('backwards','CLOCK_UNCONFIRMED'),('stale','CLOCK_STALE'),('infinite','CLOCK_UNCONFIRMED')])
def test_untrustworthy_calibration_stops(change,code):
    clock=HookBackendClock(monotonic=lambda:10.05);r=receipt()
    if change=='missing':r.server_time_utc=None
    if change=='slow':r.request_started_monotonic=8
    if change=='backwards':r.request_started_monotonic=11
    if change=='stale':r.request_started_monotonic=-200;r.response_received_monotonic=-199.9
    if change=='infinite':r.response_received_monotonic=float('nan')
    with pytest.raises(ValueError,match=code):clock.observe(r)

def test_refresh_intersects_bounds_and_never_moves_proof_backwards():
    tick=[10.05];clock=HookBackendClock(monotonic=lambda:tick[0]);clock.observe(receipt())
    tick[0]=20.05;before=clock.now()
    clock.observe(receipt(STAMP+timedelta(seconds=10.01),20,20.05))
    assert clock.now()>=before
    tick[0]=200
    with pytest.raises(ValueError,match='CLOCK_STALE'):clock.now()

def test_backend_clock_jump_rejected():
    tick=[10.05];clock=HookBackendClock(monotonic=lambda:tick[0]);clock.observe(receipt())
    tick[0]=20.05
    with pytest.raises(ValueError,match='CLOCK_CHANGED'):
        clock.observe(receipt(STAMP+timedelta(seconds=30),20,20.05))


@pytest.mark.parametrize('mode',['stale','slow'])
def test_network_wait_keeps_anchor_and_recovers_only_with_fresh_receipt(mode):
    tick=[10.05];clock=HookBackendClock(monotonic=lambda:tick[0]);clock.observe(receipt())
    original=clock.anchor
    tick[0]=140.05
    blocked=receipt() if mode=='stale' else receipt(STAMP+timedelta(seconds=130),138,140.05)
    with pytest.raises(ValueError,match='CLOCK_STALE|CLOCK_RTT_UNCONFIRMED'):clock.observe(blocked)
    assert clock.anchor==original
    with pytest.raises(ValueError,match='CLOCK_STALE'):clock.evidence()
    clock.observe(receipt(STAMP+timedelta(seconds=130),140,140.05))
    assert clock.now()==STAMP+timedelta(seconds=130,microseconds=-1)
    assert clock.evidence()['round_trip_seconds']<=.051


def test_expired_clock_does_not_accept_jump_on_reconnection():
    tick=[10.05];clock=HookBackendClock(monotonic=lambda:tick[0]);clock.observe(receipt())
    tick[0]=140.05
    with pytest.raises(ValueError,match='CLOCK_CHANGED'):
        clock.observe(receipt(STAMP+timedelta(seconds=160),140,140.05))


@pytest.mark.parametrize('now',[float('nan'),float('inf'),9])
def test_invalid_monotonic_remains_fatal(now):
    clock=HookBackendClock(monotonic=lambda:now)
    with pytest.raises(ValueError,match='CLOCK_UNCONFIRMED'):clock.observe(receipt())


@pytest.mark.parametrize('now',[float('nan'),float('inf'),9])
def test_invalid_monotonic_after_anchor_is_not_a_network_wait(now):
    tick=[10.05];clock=HookBackendClock(monotonic=lambda:tick[0]);clock.observe(receipt())
    tick[0]=now
    with pytest.raises(ValueError,match='CLOCK_UNCONFIRMED'):clock.now()


def test_small_disjoint_intervals_use_same_tolerance_and_safe_enclosing_bounds():
    tick=[10.05];clock=HookBackendClock(monotonic=lambda:tick[0]);clock.observe(receipt())
    tick[0]=20.05
    # 20 ms disagreement was accepted by the former first check, then rejected
    # by its strict intersection. Keep both possibilities inside the bounds.
    sample=receipt(STAMP+timedelta(seconds=10.07),20,20.05)
    clock.observe(sample)
    assert clock.now() <= STAMP+timedelta(seconds=10.05)
    assert clock.upper_now() >= STAMP+timedelta(seconds=10.12)
    assert clock.evidence()['round_trip_seconds'] < .121
    assert clock.failure_detail is None


def test_old_connection_snapshot_cannot_undo_new_business_heartbeat():
    tick=[10.05];clock=HookBackendClock(monotonic=lambda:tick[0]);old=receipt();clock.observe(old)
    tick[0]=20.05;clock.observe(receipt(STAMP+timedelta(seconds=10.01),20,20.05))
    before=clock.anchor
    for _ in range(50): clock.observe(old)
    assert clock.anchor==before
    for _ in range(50): clock.observe(receipt(STAMP+timedelta(seconds=10.01),20,20.05))
    assert clock.anchor==before


def test_timestamp_rounding_is_conservative_and_not_a_time_jump():
    tick=[10.05];clock=HookBackendClock(monotonic=lambda:tick[0]);clock.observe(receipt(start=10.05))
    tick[0]=20.05;clock.observe(receipt(STAMP+timedelta(seconds=10,microseconds=1),start=20.05,end=20.05))
    assert clock.now() <= STAMP+timedelta(seconds=10,microseconds=1) <= clock.upper_now()


def test_lower_bound_never_reverses_after_tolerated_correction():
    tick=[10.05];clock=HookBackendClock(monotonic=lambda:tick[0]);clock.observe(receipt())
    tick[0]=20.05;before=clock.now()
    clock.observe(receipt(STAMP+timedelta(seconds=9.98),20,20.05))
    assert clock.now()>=before


def test_real_jump_is_rejected_with_safe_diagnostic_and_anchor_preserved():
    tick=[10.05];clock=HookBackendClock(monotonic=lambda:tick[0]);clock.observe(receipt());anchor=clock.anchor
    tick[0]=20.05
    with pytest.raises(ValueError,match='CLOCK_CHANGED'):
        clock.observe(receipt(STAMP+timedelta(seconds=30),20,20.05))
    assert clock.anchor==anchor and clock.failure_detail['receipt_gap_seconds']>19
    assert set(clock.failure_detail)=={'receipt_gap_seconds','receipt_rtt_seconds','prior_uncertainty_seconds','receipt_interval_seconds'}


@pytest.mark.parametrize('delay',[.001,.5,2,30,118])
def test_delayed_receipt_compares_bounds_at_same_monotonic_instant(delay):
    # Reproduce the Windows diagnostics: zero interval gap, 187 ms RTT,
    # 56.275 ms previous uncertainty, and 1.14 s between receipt ends.
    now=[10.056273];clock=HookBackendClock(monotonic=lambda:now[0])
    clock.observe(receipt(STAMP,10,now[0]))
    end=10.056273+1.14
    sample=receipt(STAMP+timedelta(seconds=1.15),end-.187,end)
    now[0]=end+delay
    before=clock.now()  # History/API work already advanced the issued clock.
    clock.observe(sample)
    assert clock.failure_detail is None
    assert clock.now()>=before
    assert clock.now()<=clock.upper_now()
    assert clock.anchor[1]==end


def test_delayed_receipt_still_rejects_actual_large_time_jump():
    now=[10.05];clock=HookBackendClock(monotonic=lambda:now[0]);clock.observe(receipt())
    now[0]=30.05;clock.now()
    with pytest.raises(ValueError,match='CLOCK_CHANGED'):
        clock.observe(receipt(STAMP+timedelta(seconds=30),20,20.05))


def test_current_read_floor_keeps_previous_value_on_tolerated_correction():
    now=[10.05];clock=HookBackendClock(monotonic=lambda:now[0]);clock.observe(receipt())
    now[0]=20.05;before=clock.now()
    # A correction within tolerance retains both possibilities in its safe
    # enclosing interval. The issued lower bound must never move backwards.
    clock.observe(receipt(STAMP+timedelta(seconds=9.92),20.04,20.05))
    assert clock.now()>=before and clock.now()<=clock.upper_now()
