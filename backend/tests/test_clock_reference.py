import struct
import pytest
from app.services.clock_reference import ClockReference, decode_sample, NTP_EPOCH


def packet(now=1793232000):
    value = bytearray(48); value[0] = 0x24; value[1] = 2
    value[24:32] = b'12345678'
    for pos in (32, 40): struct.pack_into('!II', value, pos, int(now+NTP_EPOCH) % 2**32, 0)
    return value


def test_verified_interval_including_2036_rollover():
    for now in (1793232000, 2200000000):
        result = decode_sample(packet(now), b'12345678', now-.01, now+.01, .02)
        assert result['status'] == 'aligned'
        assert result['offset_lower_seconds'] <= 0 <= result['offset_upper_seconds']


@pytest.mark.parametrize('bad', ['nonce','unsynced','kiss','mode','zero','dispersion','length'])
def test_bad_ntp_evidence_is_not_success(bad):
    value = packet()
    if bad == 'nonce': value[24] = 0
    if bad == 'unsynced': value[0] |= 0xc0
    if bad == 'kiss': value[1] = 0
    if bad == 'mode': value[0] = 0x23
    if bad == 'zero': value[40:48] = bytes(8)
    if bad == 'dispersion': struct.pack_into('!I', value, 8, 2*65536)
    if bad == 'length': value.append(0)
    with pytest.raises(ValueError): decode_sample(value, b'12345678', 1793232000-.01, 1793232000+.01, .02)


def test_clock_jump_and_slow_network_rejected():
    for end, elapsed in [(1793232005, .02), (1793232003, 3)]:
        with pytest.raises(ValueError): decode_sample(packet(), b'12345678', 1793232000, end, elapsed)


def test_cache_never_queries_on_heartbeat_and_expires_and_failure_replaces_success():
    tick = [10]; calls = []
    def query(host):
        calls.append(host)
        if len(calls)>1: raise TimeoutError()
        return {'status':'aligned','round_trip_seconds':.01,'offset_lower_seconds':-.01,'offset_upper_seconds':.01}
    reference = ClockReference(query=query, monotonic=lambda:tick[0])
    assert reference.snapshot()['status'] == 'unconfirmed'
    reference.refresh()
    for _ in range(20): assert reference.snapshot()['status'] == 'aligned'
    assert len(calls) == 1
    tick[0] += 301; assert reference.snapshot()['status'] == 'stale'
    reference.refresh(); assert reference.snapshot()['status'] == 'unconfirmed'
    assert reference.snapshot()['authenticated'] is False
    with pytest.raises(ValueError): ClockReference('unapproved.example')


def test_background_probe_does_not_block_snapshot_and_stops():
    import threading
    entered, release = threading.Event(), threading.Event()
    def query(host):
        entered.set(); release.wait(2)
        raise TimeoutError()
    reference=ClockReference(query=query)
    reference.start()
    assert entered.wait(1)
    assert reference.snapshot()['status']=='unconfirmed'
    release.set(); reference.stop()
    assert not reference._thread.is_alive()


def test_authenticated_heartbeat_response_includes_only_cached_reference(monkeypatch):
    from types import SimpleNamespace
    from app.api.routes.workers import heartbeat_worker
    from app.services import worker_service
    from app.api.routes import workers
    reference=ClockReference(query=lambda host: (_ for _ in ()).throw(AssertionError('route performed NTP')))
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(clock_reference=reference)))
    calls=[]
    monkeypatch.setattr(workers,'worker_actor_context',lambda *args,**kwargs:None)
    def heartbeat(db,worker_id,token,payload,**kwargs):
        assert token=='synthetic-token'
        calls.append('authenticated-service')
        return {'id':worker_id}
    monkeypatch.setattr(worker_service,'heartbeat_worker',heartbeat)
    db=SimpleNamespace(commit=lambda:calls.append('commit'),rollback=lambda:calls.append('rollback'))
    result=heartbeat_worker('synthetic-worker',None,request,db,'synthetic-token')
    assert result['data']['clock_reference']['status']=='unconfirmed'
    assert calls==['authenticated-service','commit']


def test_primary_timeout_uses_fallback_then_recovers_primary():
    calls = []; primary_down = [True]
    def query(host):
        calls.append(host)
        if host == 'ntp.aliyun.com' and primary_down[0]: raise TimeoutError('private-network-details')
        return decode_sample(packet(), b'12345678', 1793232000-.01, 1793232000+.01, .02)
    reference = ClockReference(query=query)
    reference.refresh()
    sample = reference.snapshot()
    assert calls == ['ntp.aliyun.com', 'time.windows.com']
    assert sample['status'] == 'aligned' and sample['source'] == 'windows_ntp'
    assert sample['host'] == 'time.windows.com'
    assert sample['attempts'][0]['reason'] == 'timeout'
    assert 'private-network-details' not in str(sample)
    primary_down[0] = False; calls.clear()
    reference.refresh()
    assert calls == ['ntp.aliyun.com']
    assert reference.snapshot()['source'] == 'aliyun_ntp'
    assert len(reference.snapshot()['attempts']) == 1


@pytest.mark.parametrize('offset', [30, -30])
def test_real_skew_is_not_hidden_by_trying_another_source(offset):
    calls = []
    def query(host):
        calls.append(host)
        return decode_sample(packet(1793232000+offset), b'12345678', 1793232000-.01, 1793232000+.01, .02)
    reference = ClockReference(query=query)
    reference.refresh()
    assert reference.snapshot()['status'] == 'skewed'
    assert calls == ['ntp.aliyun.com']


def test_all_sources_fail_is_unknown_not_skew_and_vpc_is_explicit():
    calls = []
    def query(host):
        calls.append(host); raise TimeoutError()
    reference = ClockReference('ntp.cloud.aliyuncs.com', query=query)
    reference.refresh()
    assert calls == ['ntp.cloud.aliyuncs.com', 'ntp.aliyun.com', 'time.windows.com', 'time.cloudflare.com']
    sample = reference.snapshot()
    assert sample['status'] == 'unconfirmed'
    assert len(sample['attempts']) == 4
    assert 'offset_lower_seconds' not in sample


@pytest.mark.parametrize('offset,status', [(0, 'aligned'), (30, 'skewed')])
def test_two_outages_reach_cloudflare_without_hiding_valid_skew(offset, status):
    calls = []
    recovered = [False]
    def query(host):
        calls.append(host)
        if host != 'time.cloudflare.com' and not recovered[0]:
            raise TimeoutError('private transport error')
        return decode_sample(packet(1793232000 + offset), b'12345678',
                             1793232000-.01, 1793232000+.01, .02)
    reference = ClockReference(query=query)
    reference.refresh()
    sample = reference.snapshot()
    assert calls == ['ntp.aliyun.com', 'time.windows.com', 'time.cloudflare.com']
    assert sample['status'] == status and sample['source'] == 'cloudflare_ntp'
    assert sample['host'] == 'time.cloudflare.com' and sample['authenticated'] is False
    assert [x['reason'] for x in sample['attempts'][:2]] == ['timeout', 'timeout']
    assert 'private transport error' not in str(sample)
    recovered[0] = True; calls.clear(); reference.refresh()
    assert calls == ['ntp.aliyun.com'] and reference.snapshot()['status'] == status
