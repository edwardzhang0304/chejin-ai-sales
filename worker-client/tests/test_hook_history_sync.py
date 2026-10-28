"""Fixed SQL over synthetic SQLite through real local HTTP; no WeChat access."""
import pytest
from test_hook_history import history_lab
from test_hook_gateway import lab
from chejin_worker_client.hook_history_sync import HookHistorySynchronizer
from chejin_worker_client.shared_rules import hook_history_rules


def test_sync_rereads_range_without_skipping_late_rows(history_lab):
    server,gateway,process,scope,delivery,catalog,db=history_lab
    db.execute('DELETE FROM MSG WHERE Type=34')
    saved=[];sync=HookHistorySynchronizer(gateway,saved.append,lambda:(0,0))
    _,value=sync.capture(process,scope,delivery,'1791543600')
    result=hook_history_rules.validate_coverage(value,delivery.peer_wxid,1791543600)
    assert result['ready'] and len(result['text_rows'])==2 and len(saved)==8
    # Existing localId 3 belonged to another peer during the prior capture;
    # delayed landing changes its peer and reveals an earlier native sequence.
    db.execute("UPDATE MSG SET StrTalker='wxid_customer_A',CreateTime=1791543601,Sequence=1791543601000,MsgSequence=9 WHERE localId=3")
    _,again=sync.capture(process,scope,delivery,'1791543600')
    result=hook_history_rules.validate_coverage(again,delivery.peer_wxid,1791543600)
    assert result['ready'] and len(result['text_rows'])==3
    assert result['text_rows'][0]['MsgSvrID']=='9001'


def test_two_pass_history_coverage_crosses_numeric_and_full_page_boundaries(history_lab):
    _, gateway, process, scope, delivery, _, db = history_lab
    # The tiny fixture defaults to 4 KiB; use the production response ceiling
    # so a complete 100-row page exercises pagination rather than size rejection.
    gateway.max_response_bytes = 2 * 1024 * 1024
    db.execute('DELETE FROM MSG')
    ids = list(range(98, 205))
    db.executemany('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)', [
        (i, str(10000 + i), 1, 0, 1791543697, i, i, i, delivery.peer_wxid, 'synthetic')
        for i in ids
    ])
    saved = []
    sync = HookHistorySynchronizer(gateway, saved.append, lambda: (0, 0))
    _, coverage = sync.capture(process, scope, delivery, '1791543600')
    checked = hook_history_rules.validate_coverage(coverage, delivery.peer_wxid, 1791543600)
    assert checked['ready'] and len(checked['text_rows']) == len(ids)
    for scan in coverage['scans']:
        pages = scan['databases'][0]['pages']
        assert [len(page) for page in pages] == [100, 7]
        assert [int(row['localId']) for page in pages for row in page] == ids
    assert len([capture for capture in saved if capture.query.kind == 'page']) == 4


def test_persistence_failure_does_not_advance_to_next_page(history_lab):
    server,gateway,process,scope,delivery,catalog,db=history_lab
    def unavailable(_):raise OSError('test storage unavailable')
    before=len([r for r in server.requests if r[0]=='/QueryDB'])
    with pytest.raises(OSError):HookHistorySynchronizer(gateway,unavailable,lambda:(0,0)).capture(process,scope,delivery,'0')
    assert len([r for r in server.requests if r[0]=='/QueryDB'])==before+1


def test_callback_change_during_scan_blocks_checkpoint(history_lab):
    server,gateway,process,scope,delivery,catalog,db=history_lab
    values=iter([(1,0),(2,0)])
    with pytest.raises(ValueError,match='BARRIER_CHANGED'):
        HookHistorySynchronizer(gateway,lambda _:None,lambda:next(values)).capture(process,scope,delivery,'0')


def test_range_limit_blocks_coverage(history_lab):
    server,gateway,process,scope,delivery,catalog,db=history_lab
    with pytest.raises(ValueError,match='RANGE_TOO_LARGE'):
        HookHistorySynchronizer(gateway,lambda _:None,lambda:(0,0),max_rows=1).capture(process,scope,delivery,'0')


def test_pre_invitation_callback_exclusion_reads_metadata_only(history_lab):
    server,gateway,process,scope,delivery,catalog,db=history_lab
    db.execute("UPDATE MSG SET CreateTime=1791543500 WHERE localId=1")
    saved=[]
    sync=HookHistorySynchronizer(gateway,saved.append,lambda:(0,0),media_enabled=True)
    _,value=sync.capture(process,scope,delivery,'1791543600',callback_ids={'18446744073709551614'})
    excluded=value['excluded_pre_invite_metadata']
    assert len(excluded)==1 and excluded[0]['native_msg_id']=='18446744073709551614'
    boundary=[c for c in saved if c.query.kind=='boundary_meta']
    assert len(boundary)==1
    assert set(boundary[0].payload['data'][0])=={'native_msg_id','create_time'}
    assert 'StrContent' not in boundary[0].query.payload()['sql']
    checked=hook_history_rules.validate_coverage(value,delivery.peer_wxid,1791543600)
    assert '18446744073709551614' in checked['excluded_native_ids']


def test_missing_new_callback_does_not_get_excluded_as_old(history_lab):
    _,gateway,process,scope,delivery,_,_=history_lab
    sync=HookHistorySynchronizer(gateway,lambda _:None,lambda:(0,0),media_enabled=True)
    with pytest.raises(ValueError,match='CALLBACK_NOT_LANDED'):
        sync.capture(process,scope,delivery,'1791543600',callback_ids={'999999'})


def test_scan_records_local_bounds_separately_from_backend_clock(history_lab):
    from datetime import datetime,timezone,timedelta
    _,gateway,process,scope,delivery,_,db=history_lab
    db.execute('DELETE FROM MSG WHERE Type=34')
    sync=HookHistorySynchronizer(gateway,lambda _:None,lambda:(0,0),
        now=lambda:datetime.now(timezone.utc)-timedelta(hours=1))
    _,coverage=sync.capture(process,scope,delivery,'1791543600')
    for scan in coverage['scans']:
        local=datetime.fromisoformat(scan['local_clock']['window']['completed_at'])
        backend=datetime.fromisoformat(scan['observed_at'])
        assert 3599<(local-backend).total_seconds()<3601
    assert hook_history_rules.validate_coverage(coverage,delivery.peer_wxid,1791543600)['ready']
    # A clock jump between scans is rejected even if each individual duration is valid.
    clock=coverage['scans'][1]['local_clock']['window']
    for name in ('started_at','completed_at'):
        clock[name]=(datetime.fromisoformat(clock[name])+timedelta(seconds=10)).isoformat()
    with pytest.raises(ValueError,match='CLOCK_INVALID'):
        hook_history_rules.validate_coverage(coverage,delivery.peer_wxid,1791543600)
