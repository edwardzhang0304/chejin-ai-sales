"""Read-session performance boundaries with real local HTTP and durable SQLite."""
from dataclasses import replace
import pytest
from test_hook_gateway import lab
from test_hook_history import history_lab
from chejin_worker_client import storage, ui_lock, hook_active_calls
from chejin_worker_client.hook_gateway import HookLocalError
from chejin_worker_client.hook_history import HookHistoryQuery
from chejin_worker_client.hook_history_sync import HookHistorySynchronizer


def calls():
    with storage.db_connection() as c:
        return [dict(r) for r in c.execute('SELECT * FROM hook_active_calls')]


def test_full_capture_inspects_components_twice_and_journals_once(history_lab):
    server,gateway,process,scope,delivery,_,db=history_lab
    inspected=[];live=[]
    gateway.inspector=lambda _:inspected.append(True) or server.current_process
    gateway.generation_inspector=lambda p:live.append(p.generation)
    before=len(calls());requests=len(server.requests)
    sync=HookHistorySynchronizer(gateway,lambda _:None,lambda:(0,0),media_enabled=True)
    sync.capture(process,scope,delivery,'0')
    assert len(inspected)==2 and len(live)>=24
    assert len(server.requests)-requests==52
    added=calls()[before:]
    assert len(added)==1 and added[0]['kind']=='history_capture' and added[0]['state']=='completed'
    assert not ui_lock.lock_summary()['locked']
    assert sync.completed_barrier==(0,0)


def test_timeout_poison_cannot_be_caught_to_continue_and_quarantines(history_lab,monkeypatch):
    _,gateway,process,scope,_,_,_=history_lab
    def unknown(*a,**k):raise HookLocalError('HOOK_LOCAL_TRANSPORT_FAILED')
    monkeypatch.setattr(gateway,'_response_http',unknown)
    with pytest.raises(HookLocalError,match='SESSION_INVALID'):
        with gateway.history_session(process,scope) as reader:
            with pytest.raises(HookLocalError,match='TRANSPORT'):reader.history_catalog(process,scope)
            with pytest.raises(HookLocalError,match='SESSION_INVALID'):reader.history_catalog(process,scope)
    assert calls()[-1]['state']=='uncertain'
    with pytest.raises(HookLocalError,match='QUARANTINED'):gateway.login_status(process)
    assert not ui_lock.lock_summary()['locked']


def test_failed_reserve_never_contacts_dll(history_lab,monkeypatch):
    server,gateway,process,scope,_,_,_=history_lab
    before=len(server.requests)
    def fail(*a):raise OSError('disk full')
    monkeypatch.setattr(hook_active_calls,'reserve',fail)
    with pytest.raises(OSError):
        with gateway.history_session(process,scope):pytest.fail('entered without durable intent')
    assert len(server.requests)==before and not ui_lock.lock_summary()['locked']


def test_failed_finish_retains_calling_and_blocks_next_action(history_lab,monkeypatch):
    _,gateway,process,scope,_,_,_=history_lab
    def fail(*a):raise OSError('disk full')
    with monkeypatch.context() as m:
        m.setattr(hook_active_calls,'finish',fail)
        with pytest.raises(OSError):
            with gateway.history_session(process,scope) as reader:reader.history_catalog(process,scope)
    assert calls()[-1]['state']=='calling'
    with pytest.raises(HookLocalError,match='QUARANTINED'):gateway.login_status(process)


def test_saved_capture_failure_does_not_quarantine_completed_http(history_lab):
    _,gateway,process,scope,delivery,_,_=history_lab
    def fail(_):raise OSError('disk full')
    with pytest.raises(OSError):HookHistorySynchronizer(gateway,fail,lambda:(0,0)).capture(process,scope,delivery,'0')
    assert calls()[-1]['state']=='completed'
    assert gateway.login_status(process).logged_in


def test_reader_closed_wrong_scope_nested_and_write_route_rejected(history_lab):
    server,gateway,process,scope,_,_,_=history_lab
    with gateway.history_session(process,scope) as reader:
        with pytest.raises(HookLocalError,match='SESSION_INVALID'):
            with gateway.history_session(process,scope):pass
        with pytest.raises(HookLocalError,match='SESSION_INVALID'):
            reader.history_catalog(process,replace(scope,account_epoch='other'))
        reader.history_catalog(process,scope)
    with pytest.raises(HookLocalError,match='SESSION_INVALID'):reader.history_catalog(process,scope)
    before=len(server.requests)
    with pytest.raises(HookLocalError,match='ROUTE_NOT_ALLOWED'):
        with gateway.history_session(process,scope):gateway._response('SendTextMsg',{})
    assert len(server.requests)==before


@pytest.mark.parametrize('change',['account','catalog','process','component'])
def test_mid_capture_changes_never_publish_coverage(history_lab,change):
    server,gateway,process,scope,delivery,_,_=history_lab
    original=gateway.generation_inspector
    gateway.generation_inspector=lambda p:original(p) if change!='component' else None
    changed=False
    def alter(path):
        nonlocal changed
        if path!='/QueryDB' or changed:return
        changed=True
        if change=='account':server.wxid='wxid_other'
        elif change=='catalog':server.catalog_entries=[]
        else:server.current_process=replace(process,started_at=process.started_at+1)
    server.on_request=alter
    with pytest.raises((ValueError,HookLocalError)):
        HookHistorySynchronizer(gateway,lambda _:None,lambda:(0,0)).capture(process,scope,delivery,'0')
    assert changed and not ui_lock.lock_summary()['locked']


def test_crashed_read_session_is_not_cleared_by_age(history_lab):
    _,gateway,process,scope,_,_,_=history_lab
    hook_active_calls.reserve(process.generation,'crashed-read','history_capture')
    with storage.db_connection() as c:
        c.execute("UPDATE hook_active_calls SET attempted_at='2000-01-01T00:00:00Z' WHERE action_id='crashed-read'");c.commit()
    with pytest.raises(HookLocalError,match='QUARANTINED'):
        with gateway.history_session(process,scope):pass


@pytest.mark.parametrize('request_number',[1,26,52])
def test_timeout_at_any_request_keeps_unknown_session(history_lab,monkeypatch,request_number):
    _,gateway,process,scope,delivery,_,_=history_lab
    http=gateway._response_http;counter=0
    def response(*a,**kw):
        nonlocal counter
        counter+=1
        if counter==request_number:raise HookLocalError('HOOK_LOCAL_TRANSPORT_FAILED')
        return http(*a,**kw)
    monkeypatch.setattr(gateway,'_response_http',response)
    with pytest.raises(HookLocalError,match='TRANSPORT'):
        HookHistorySynchronizer(gateway,lambda _:None,lambda:(0,0),media_enabled=True).capture(process,scope,delivery,'0')
    assert counter==request_number and calls()[-1]['state']=='uncertain'
    with pytest.raises(HookLocalError,match='QUARANTINED'):gateway.login_status(process)


def test_session_budget_stops_before_next_http_and_releases_lease(history_lab,monkeypatch):
    server,gateway,process,scope,_,_,_=history_lab
    import chejin_worker_client.hook_gateway as module
    clock=[0.0];monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    before=len(server.requests)
    def advance(_):clock[0]=31
    server.on_request=advance
    with pytest.raises(ValueError,match='READ_BUDGET_EXCEEDED'):
        with gateway.history_session(process,scope) as reader:reader.history_catalog(process,scope)
    assert len(server.requests)==before+1 and calls()[-1]['state']=='completed'
    assert not ui_lock.lock_summary()['locked']


def test_real_process_exit_leaves_durable_read_barrier(history_lab,tmp_path):
    # Simulate abrupt termination at the first DLL request, with the actual
    # gateway journal and lease. The child never contacts WeChat or our server.
    server,gateway,process,scope,_,_,_=history_lab
    import os,sys,subprocess,json
    from dataclasses import asdict
    child=tmp_path/'crash_read.py'
    child.write_text("""
import json,os,sys
from pathlib import Path
from chejin_worker_client import storage
from chejin_worker_client.hook_config import HookDeployment
from chejin_worker_client.hook_gateway import HookGateway
from chejin_worker_client.hook_process import HookProcessSnapshot
from chejin_worker_client.hook_inbox import HookReceiveScope
v=json.loads(sys.argv[1]); storage.DB_FILE=Path(v['db']);storage.APP_DIR=storage.DB_FILE.parent
p=HookProcessSnapshot(**v['process']);d=HookDeployment(**v['deployment']);s=HookReceiveScope(**v['scope'])
g=HookGateway(d,timeout_seconds=1,max_response_bytes=4096,lock_timeout_seconds=.1,inspector=lambda _:p)
g._response_http=lambda *a,**kw:os._exit(71)
with g.history_session(p,s) as read:read.history_catalog(p,s)
""")
    args=dict(db=str(storage.DB_FILE),process=asdict(process),deployment=asdict(gateway.deployment),scope=asdict(scope))
    # JSON turns snapshot tuples into lists; restore constructor comparison.
    source=child.read_text().replace("p=HookProcessSnapshot(**v['process']);", "v['process']['command_arguments']=tuple(v['process']['command_arguments']);v['process']['listener_owners']=tuple(v['process']['listener_owners']);p=HookProcessSnapshot(**v['process']);")
    child.write_text(source)
    result=subprocess.run([sys.executable,str(child),json.dumps(args)],capture_output=True,text=True,timeout=10)
    assert result.returncode==71,result.stderr
    assert calls()[-1]['state']=='calling'
    # Expire only the abandoned action lease, never the durable DLL journal.
    from chejin_worker_client import ui_lock as lock_module
    lease=json.loads(lock_module.LOCK_FILE.read_text())
    lease['lease_expires_at']='2000-01-01T00:00:00+00:00'
    lock_module.LOCK_FILE.write_text(json.dumps(lease))
    with pytest.raises(HookLocalError,match='QUARANTINED'):gateway.login_status(process)
