"""Real durable inbox -> dispatcher -> contact gate, synthetic DLL/backend only."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from chejin_worker_client import storage
from chejin_worker_client.api import ApiError
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_inbox import HookReceiveScope, NativeInbox


@pytest.fixture
def env(tmp_path, monkeypatch):
    from chejin_worker_client import hook_friend_discovery
    clock=[1000.0]
    monkeypatch.setattr(hook_friend_discovery,'monotonic',lambda:clock[0])
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path/'worker_client.sqlite3')
    scope = HookReceiveScope('wxid_sales_test', 'epoch-test', 'process-test', 30001)
    contacts = {'code': 0, 'data': []}
    calls, bound = [], []
    client = SimpleNamespace()
    def bind(credentials, task, account, friends):
        bound.append(task)
        return {'binding_id': 'binding-'+task}
    client.bind_friend = bind
    def read(process, current):
        assert current == scope
        calls.append('contacts')
        return SimpleNamespace(payload=json.loads(json.dumps(contacts)))
    def runtime():
        rt = HookFormalRuntime.__new__(HookFormalRuntime)
        rt.scope, rt.process, rt.private_root = scope, SimpleNamespace(generation=scope.process_generation), tmp_path/'transactions/hook'
        rt.gateway, rt.client, rt.credentials = SimpleNamespace(friend_list=read), client, None
        rt.account = {k: getattr(scope,k) for k in ('self_wxid','account_epoch','process_generation')}
        return rt
    d = HookBusinessDispatcher(client,tmp_path,None,None,automatic_transcription_disabled=True)
    d.runtime = runtime()
    def receive(mid='101', peer='wxid_peer_test', content='我通过了你的朋友验证请求，现在我们可以开始聊天了', kind='1', other=None, incoming=True):
        current = other or scope
        body = {'ServerPort':str(current.hook_api_port),'selfwxid':current.self_wxid,'sendorrecv':'2',
                'msgnumber':'1','msglist':[{'msgsvrid':mid,'msgtype':kind,'fromtype':'1',
                'fromid':peer if incoming else current.self_wxid,'toid':current.self_wxid if incoming else peer,
                'msg':content,'time':'2026-11-05 11:35:00'}]}
        NativeInbox(max_body_bytes=4096,max_inbox_bytes=1024*1024).persist(json.dumps(body).encode(),current)
    def tick(invites=None, bindings=None):
        d._discover_invitations(None, invites if invites is not None else [invite()], bindings or [])
    def advance(seconds=6):clock[0]+=seconds
    return SimpleNamespace(**locals())


def invite(task='task-1', code='CJTEST1'):
    return {'source_task_id':task,'remark_code':code,'discovery_after_authorization':False}


def contact(peer='wxid_peer_test', code='CJTEST1'):
    return {'wxid':peer,'markname':code}


def test_waiting_invite_does_not_poll_and_callback_binds_once(env):
    env.tick([])  # startup has no pending invitations
    env.tick()
    assert env.calls == []
    env.contacts['data'] = [contact()]
    env.receive()
    env.tick()
    for _ in range(30):env.tick()
    env.receive()  # duplicate physical callback, same native message
    env.tick()
    assert env.calls == ['contacts'] and env.bound == ['task-1']


def test_offline_acceptance_startup_sweep_shared_across_invites(env):
    env.contacts['data'] = [contact(), contact('wxid_peer_two','CJTEST2')]
    invites = [invite(),invite('task-2','CJTEST2'),invite('task-3','CJTEST3')]
    env.tick(invites)
    for _ in range(30):env.tick(invites)
    assert env.calls == ['contacts'] and env.bound == ['task-1','task-2']
    for _ in range(2):env.advance();env.tick(invites)
    state = next(json.loads(p.read_text()) for p in (env.tmp_path/'transactions/hook/friend-discovery').glob('*.json')
                 if json.loads(p.read_text())['stage']=='complete')
    assert state['stage']=='complete' and state['results']['task-3']['state']=='unresolved'
    assert len(env.calls)==3 and env.bound==['task-1','task-2']


def test_startup_backlog_is_one_read_not_one_per_page(env):
    for i in range(100):env.receive(str(1000+i))
    env.tick()
    for _ in range(10):env.tick()
    assert len(env.calls)==1


def test_missed_acceptance_normal_message_recovers_without_reinviting(env):
    env.tick()  # startup not yet accepted
    for _ in range(2):env.advance();env.tick()  # finite startup reconciliation exhausted
    env.contacts['data']=[contact()]
    env.receive(content='你好，想咨询一辆车')
    env.tick()
    assert len(env.calls)==4 and env.bound==['task-1']


@pytest.mark.parametrize('change', ['group','outgoing','missing_id','other_account','other_epoch','bound_peer'])
def test_non_candidate_callbacks_cannot_trigger_contact_query(env,change):
    env.tick([])
    kwargs = {}
    if change=='group':kwargs['peer']='group@chatroom'
    if change=='outgoing':kwargs['incoming']=False
    if change=='missing_id':kwargs['mid']='0'
    if change=='other_account':kwargs['other']=replace(env.scope,self_wxid='wxid_other_sales')
    if change=='other_epoch':kwargs['other']=replace(env.scope,account_epoch='epoch-other')
    env.receive(**kwargs)
    env.tick(bindings=[{'peer_wxid':'wxid_peer_test'}] if change=='bound_peer' else [])
    assert env.calls==[] and env.bound==[]


@pytest.mark.parametrize('rows', [[contact('wxid_other')], [contact(),contact()], [contact(code='CJWRONG')]])
def test_text_notice_alone_cannot_bypass_peer_or_exact_shortcode(env,rows):
    env.tick([])
    env.contacts['data']=rows
    env.receive()
    env.tick()
    assert len(env.calls)==1 and env.bound==[]


def test_multiple_events_share_one_capture_and_reply_notices_are_not_authority(env):
    env.tick([])
    env.contacts['data']=[contact(), contact('wxid_peer_two','CJTEST2')]
    env.receive()
    env.receive('102','wxid_peer_two')
    env.tick([invite(), invite('task-2','CJTEST2')])
    assert len(env.calls)==1 and env.bound==['task-1','task-2']


def test_backend_retry_reuses_snapshot_across_runtime_restart(env):
    env.contacts['data']=[contact()]
    original=env.client.bind_friend
    env.client.bind_friend=lambda *a: (_ for _ in ()).throw(ApiError('HOOK_BACKEND_NETWORK_FAILED','offline',0))
    with pytest.raises(ApiError):env.tick()
    env.d.runtime=env.runtime()
    env.client.bind_friend=original
    env.tick()
    assert len(env.calls)==1 and env.bound==['task-1']


def test_contact_read_failure_closes_round_and_does_not_spin(env):
    def fail(*a):env.calls.append('failure');raise ValueError('synthetic read failure')
    env.d.runtime.gateway.friend_list=fail
    with pytest.raises(ValueError):env.tick()
    for _ in range(10):env.tick()
    assert env.calls==['failure']
    report=json.loads(next((env.tmp_path/'transactions/hook/friend-discovery').glob('*.json')).read_text())
    assert report['stage']=='read_failed'


def test_reconnect_once_reconciles_pending_invites(env):
    env.tick()
    for _ in range(2):env.advance();env.tick()
    env.contacts['data']=[contact()]
    env.d.runtime._friend_connection_identity=object()
    env.tick()
    for _ in range(10):env.tick()
    assert len(env.calls)==4 and env.bound==['task-1']


def test_operator_transfer_grant_checked_once_without_new_callback(env):
    env.tick([])
    env.contacts['data']=[contact()]
    item={**invite(),'discovery_after_authorization':True}
    env.tick([item])
    env.tick([item])
    assert len(env.calls)==1 and env.bound==['task-1']


def test_unsent_read_keeps_original_retry_round_until_recovered(env):
    from chejin_worker_client.hook_read_retry import ReadRetryWait
    original=env.d.runtime.gateway.friend_list
    def wait(*a):raise ReadRetryWait('HOOK_READ_RETRY_WAIT')
    env.d.runtime.gateway.friend_list=wait
    with pytest.raises(ReadRetryWait):env.tick()
    with storage.db_connection() as db:
        before=json.loads(db.execute('SELECT state_json FROM hook_friend_discovery').fetchone()[0])['pending']
    assert before['stage']=='prepared'
    env.contacts['data']=[contact()]
    env.d.runtime.gateway.friend_list=original
    env.tick()
    report=json.loads(next((env.tmp_path/'transactions/hook/friend-discovery').glob('*.json')).read_text())
    assert report['round_id']==before['round_id'] and report['stage']=='complete'
    assert env.bound==['task-1'] and len(env.calls)==1


def test_callback_arriving_during_capture_is_not_lost(env):
    env.tick([])
    env.contacts['data']=[contact()]
    original=env.client.bind_friend
    def bind(*a):
        env.receive('102','wxid_peer_two')
        return original(*a)
    env.client.bind_friend=bind
    env.receive()
    env.tick([invite(),invite('task-2','CJTEST2')])
    env.contacts['data'].append(contact('wxid_peer_two','CJTEST2'))
    env.client.bind_friend=original
    env.advance()
    env.tick([invite('task-2','CJTEST2')],bindings=[{'peer_wxid':'wxid_peer_test'}])
    env.tick([invite('task-2','CJTEST2')],bindings=[{'peer_wxid':'wxid_peer_test'}])
    assert env.bound==['task-1','task-2'] and len(env.calls)==3


def test_interrupted_query_is_recorded_without_automatic_reissue(env):
    from chejin_worker_client.hook_friend_discovery import FriendDiscovery
    store=FriendDiscovery(env.scope)
    pending=store.prepare([invite()],set())
    pending['stage']='reading';store.update(pending)
    env.d.runtime._friend_discovery=store
    env.tick()
    assert not env.calls
    report=json.loads(next((env.tmp_path/'transactions/hook/friend-discovery').glob('*.json')).read_text())
    assert report['stage']=='interrupted'
    env.tick()
    assert not env.calls


def test_legacy_unresolved_checks_share_startup_capture_then_wait_for_event(env):
    legacy=[{'binding_id':'legacy1','peer_wxid':'wxid_peer_test','remark_code':'CJTEST1'},
            {'binding_id':'legacy2','peer_wxid':'wxid_peer_two','remark_code':'CJTEST2'}]
    reviews=[]
    def review(items,*,initial_friends):
        reviews.append((items,initial_friends))
        return {b['binding_id']:True for b in items}
    env.d.runtime.review_relationships=review
    for _ in range(20):env.d._discover_invitations(None,[],legacy,legacy)
    assert len(env.calls)==1 and len(reviews)==1 and len(reviews[0][0])==2
    env.receive()
    env.d._discover_invitations(None,[],legacy,legacy)
    assert len(env.calls)==2 and len(reviews[-1][0])==1


def test_export_contains_round_and_cursor_without_unrelated_contacts(env,monkeypatch):
    import zipfile
    from chejin_worker_client.diagnostic_bundle import export_bundle
    env.contacts['data']=[contact(),contact('wxid_unrelated','PRIVATE_OTHER_CONTACT')]
    env.tick()
    monkeypatch.setenv('CHEJIN_UPDATE_STAGING_ROOT',str(env.tmp_path/'updates'))
    with zipfile.ZipFile(export_bundle(env.tmp_path,env.tmp_path/'out.zip')) as archive:
        names=archive.namelist()
        rounds=[n for n in names if n.startswith('receipts/hook/friend-discovery/')]
        assert len(rounds)==1
        report=json.loads(archive.read(rounds[0]))
        assert report['results']['task-1']['state']=='bound'
        assert report['comparisons']['task-1']['state']=='matched'
        assert any('hook_friend_discovery' in n for n in names)
        assert not any('friend-reads/' in n for n in names)
        assert not any(b'PRIVATE_OTHER_CONTACT' in archive.read(n) for n in names)


def test_new_wechat_process_archives_old_round_and_uses_new_capture(env):
    env.contacts['data']=[contact()]
    original=env.client.bind_friend
    env.client.bind_friend=lambda *a: (_ for _ in ()).throw(ApiError('HOOK_BACKEND_NETWORK_FAILED','offline',0))
    with pytest.raises(ApiError):env.tick()
    env.client.bind_friend=original
    env.d.runtime=env.runtime()
    env.d.runtime.scope=replace(env.scope,process_generation='process-new')
    env.d.runtime.process=SimpleNamespace(generation='process-new')
    def read(*a):
        env.calls.append('new-process')
        return SimpleNamespace(payload={'code':0,'data':[]})
    env.d.runtime.gateway.friend_list=read
    env.tick()  # archive, never bind from old captured contacts
    env.tick()  # one new startup capture
    assert env.calls==['contacts','new-process'] and not env.bound
    reports=[json.loads(p.read_text()) for p in (env.tmp_path/'transactions/hook/friend-discovery').glob('*.json')]
    assert {r['stage'] for r in reports}=={'process_changed','waiting_contact_refresh'}


def test_retired_invitation_closes_round_without_repeating_query(env):
    env.contacts['data']=[contact()]
    env.client.bind_friend=lambda *a: (_ for _ in ()).throw(ApiError('HOOK_BACKEND_NETWORK_FAILED','offline',0))
    with pytest.raises(ApiError):env.tick()
    env.tick([])
    assert len(env.calls)==1 and not env.bound
    report=json.loads(next((env.tmp_path/'transactions/hook/friend-discovery').glob('*.json')).read_text())
    assert report['stage']=='retired'


@pytest.mark.parametrize('code', ['UI_LOCK_BUSY','UI_LOCK_ACQUIRE_TIMEOUT'])
def test_lock_contention_keeps_event_until_query_can_start(env, code):
    from chejin_worker_client.ui_lock import UiLockError
    env.tick([])
    env.receive()
    original=env.d.runtime.gateway.friend_list
    env.d.runtime.gateway.friend_list=lambda *a: (_ for _ in ()).throw(UiLockError(code,'busy'))
    with pytest.raises(UiLockError):env.tick()
    assert not env.calls and not env.bound
    with storage.db_connection() as db:
        pending=json.loads(db.execute('SELECT state_json FROM hook_friend_discovery').fetchone()[0])['pending']
    assert pending['stage']=='prepared' and pending['events']
    env.contacts['data']=[contact()]
    env.d.runtime.gateway.friend_list=original
    env.tick()
    for _ in range(10):env.tick()
    assert env.calls==['contacts'] and env.bound==['task-1']


def test_lease_lost_after_query_never_implicitly_reissues_query(env):
    from chejin_worker_client.ui_lock import UiLockError
    def lost(*a):
        env.calls.append('contacts')
        raise UiLockError('UI_LOCK_LEASE_EXPIRED','lost')
    env.d.runtime.gateway.friend_list=lost
    with pytest.raises(UiLockError):env.tick()
    for _ in range(10):env.tick()
    assert env.calls==['contacts'] and not env.bound


@pytest.mark.parametrize('rows,count', [([],0),([contact(),contact('wxid_other')],2),
    ([contact(),contact(code='CJOTHER')],1)])
def test_unresolved_comparison_export_is_target_only(env,monkeypatch,rows,count):
    import zipfile
    from chejin_worker_client.diagnostic_bundle import export_bundle
    env.contacts['data']=rows+[contact('wxid_unrelated','PRIVATE_OTHER_CONTACT')]
    env.tick()
    assert not env.bound
    monkeypatch.setenv('CHEJIN_UPDATE_STAGING_ROOT',str(env.tmp_path/'updates'))
    with zipfile.ZipFile(export_bundle(env.tmp_path,env.tmp_path/'out.zip')) as archive:
        report=json.loads(archive.read(next(n for n in archive.namelist() if n.startswith('receipts/hook/friend-discovery/'))))
        comparison=report['comparisons']['task-1']
        assert comparison['state']=='unconfirmed'
        assert comparison['shortcode_matching_objects']==count
        assert len(comparison['shortcode_objects'])==count
        if count==1:assert comparison['peer_object_count']==2
        assert not any(b'PRIVATE_OTHER_CONTACT' in archive.read(n) for n in archive.namelist())


def test_contact_refresh_lag_then_binds_without_second_callback(env):
    env.tick([])
    env.receive()
    env.tick()
    for _ in range(20):env.tick()
    assert len(env.calls)==1 and not env.bound
    env.advance(2)
    env.tick()
    assert len(env.calls)==2 and not env.bound
    env.contacts['data']=[contact()]
    env.advance(5)
    env.tick()
    assert len(env.calls)==3 and env.bound==['task-1']
    for _ in range(20):env.advance();env.tick()
    assert len(env.calls)==3


def test_refresh_budget_survives_worker_restart_and_exhausts(env):
    env.tick([])
    env.receive()
    env.tick()
    for _ in range(2):
        env.d.runtime=env.runtime()
        env.advance()
        env.tick()
    for _ in range(20):env.advance();env.tick()
    assert len(env.calls)==3 and not env.bound
    reports=[json.loads(p.read_text()) for p in (env.tmp_path/'transactions/hook/friend-discovery').glob('*.json')]
    assert len(reports)==3
    final=next(r for r in reports if r['stage']=='complete')
    assert final['refresh_attempt']==2
    assert final['results']['task-1']['state']=='unresolved'
    assert final['comparisons']['task-1']['shortcode_matching_objects']==0


def test_retired_during_refresh_does_not_query_again(env):
    env.tick()
    env.advance()
    env.tick([])
    assert len(env.calls)==1
    with storage.db_connection() as db:
        state=json.loads(db.execute('SELECT state_json FROM hook_friend_discovery').fetchone()[0])
    assert state['pending'] is None and state['last_round']['stage']=='retired'


def test_invalid_contact_shape_ends_with_specific_evidence_without_refresh(env):
    env.d.runtime.gateway.friend_list=lambda *a:SimpleNamespace(payload='invalid provider shape')
    env.tick()
    env.tick()
    report=json.loads(next((env.tmp_path/'transactions/hook/friend-discovery').glob('*.json')).read_text())
    assert report['stage']=='complete' and not env.bound
    assert report['results']['task-1']['reason_code']=='FRIEND_RESPONSE_SHAPE_INVALID'
    assert report['comparisons']['task-1']['reason_code']=='FRIEND_RESPONSE_SHAPE_INVALID'


def test_restart_resumes_old_budget_and_reconciles_new_offline_invite(env):
    env.tick([])
    env.receive()
    env.tick()
    env.advance(2);env.tick()  # old task has one delayed read left
    env.contacts['data']=[contact('wxid_peer_two','CJTEST2')]
    env.d.runtime=env.runtime()
    invitations=[invite(),invite('task-2','CJTEST2')]
    env.advance(5);env.tick(invitations)  # original task finishes its budget
    assert not env.bound and len(env.calls)==3
    env.tick(invitations)  # new offline task gets its own startup capture
    assert env.bound==['task-2'] and len(env.calls)==4
    for _ in range(20):env.advance();env.tick(invitations)
    assert len(env.calls)==4  # original task's budget was not reset
    reports=[json.loads(p.read_text()) for p in (env.tmp_path/'transactions/hook/friend-discovery').glob('*.json')]
    extra=next(r for r in reports if r['reason']=='resume_startup')
    assert [i['source_task_id'] for i in extra['invitations']]==['task-2']


def test_uncovered_startup_does_not_consume_new_transfer_grant(env):
    env.tick()
    env.d.runtime=env.runtime()
    items=[invite(),invite('task-2','CJTEST2')]
    env.tick(items)  # record uncovered task while old round waits
    for _ in range(2):env.advance();env.tick(items)
    env.contacts['data']=[contact('wxid_peer_two','CJTEST2'),contact('wxid_peer_three','CJTEST3')]
    transfer={**invite('task-3','CJTEST3'),'discovery_after_authorization':True}
    env.tick(items+[transfer])  # only the queued offline task
    assert env.bound==['task-2']
    env.tick([invite(),transfer],bindings=[{'peer_wxid':'wxid_peer_two'}])
    assert env.bound==['task-2','task-3']
