"""Scheduling/Flow boundaries with no active Windows operations."""
from types import SimpleNamespace
from dataclasses import replace
import threading,uuid
from chejin_worker_client import storage
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.hook_config import DEPLOYMENT_KEY
from chejin_worker_client.hook_factory import create_hook_services
from test_hook_gateway import lab
from test_hook_connection import setup
from test_hook_backend import peer as backend_peer
import pytest,json


def test_default_ocr_selection_and_explicit_bad_setting_do_not_fallback(lab):
    assert create_hook_services(SimpleNamespace(base_url='http://127.0.0.1:12345/api')) is None
    with storage.db_connection() as conn:
        conn.execute('INSERT INTO client_settings VALUES (?,?,?)',('wechat_transport_v1',json.dumps({'mode':'something_else'}),'2026-10-12T00:00:00+00:00'));conn.commit()
    with pytest.raises(ValueError,match='HOOK_TRANSPORT_SELECTION_INVALID'):
        create_hook_services(SimpleNamespace(base_url='http://127.0.0.1:12345/api'))


@pytest.mark.parametrize("pending_friend", [False, True])
def test_one_customer_model_wait_does_not_block_other_customer(lab,backend_peer,tmp_path,pending_friend):
    manager,credentials,state,response=setup(lab,backend_peer)
    prepared=manager.prepare(credentials,state)
    scope=prepared.registration.scope
    account={k:getattr(scope,k) for k in ('self_wxid','account_epoch','process_generation')}
    bindings=[{'binding_id':str(uuid.uuid4()),'binding_revision':1,'peer_wxid':f'wxid_customer_{i}',
        'conversation_id':str(uuid.uuid4()),'history_since_create_time':'1','voice_jobs':[],
        'batch_ids':[str(uuid.uuid4())]} for i in range(2)]
    calls=[];sends=[]
    class Client:
        def call(self,credentials,method,path,body=None,**kwargs):
            calls.append(path)
            if path.endswith('native-work'):
                return {'schema_version':'hook-native-work-v1','observation_query_version':1,**account,'run_status':'running','inflight_flow':{},
                    'bindings':bindings,'invitations':[{'source_task_id':'pending-friend'}] if pending_friend else [],'retained_sends':[],'inflight_task':None}
            if bindings[0]['batch_ids'][0] in path:return {'processing':True}
            return {'reply_action':{'status':'queued'},'task':{'id':str(uuid.uuid4())}}
    class Runtime:
        def __init__(self,*args,**kwargs):self.credentials=credentials;self.account=account;self.scope=scope;self.active_flow_id=None;self.active_task_id=None;self.clock=SimpleNamespace(observe=lambda x:None)
        def discover(self,task_id):
            from chejin_worker_client.api import ApiError
            calls.append(task_id)
            raise ApiError('HOOK_FRIEND_NOT_FOUND','synthetic waiting',409)
        def barrier(self,b):return (0,0)
        def sync(self,b):return {'history_ready':True}
        def reply_once(self,b,data,*,history):sends.append(b['binding_id']);return {'status':'sent'}
    runner=SimpleNamespace(binding=SimpleNamespace(run_status='running'),stop_event=threading.Event(),api=SimpleNamespace(),
        _can_start_new_flow=lambda:True,_worker_transaction_barrier_ready=lambda *a,**kw:True)
    services=SimpleNamespace(connection=manager,_credentials=credentials,startup_window_ready_for=lambda _:True)
    dispatcher=HookBusinessDispatcher(Client(),tmp_path/'continuous',lambda *a:None,lambda *a:None,
        automatic_transcription_disabled=lambda:True,runtime_factory=Runtime)
    try:
        dispatcher.tick(runner,services)
        assert ('pending-friend' in calls)==pending_friend
        assert sends==[bindings[1]['binding_id']]
        assert any(bindings[0]['batch_ids'][0] in path for path in calls)
    finally:manager.close()


def test_unconfirmed_local_flow_cannot_be_erased(lab,tmp_path):
    flow=str(uuid.uuid4());storage.begin_runtime_flow(flow,'task')
    d=HookBusinessDispatcher(None,tmp_path,lambda *a:None,lambda *a:None,automatic_transcription_disabled=lambda:True)
    d.runtime=SimpleNamespace(active_flow_id=None,active_task_id=None)
    runner=SimpleNamespace(api=SimpleNamespace(),current_task=None)
    with pytest.raises(ValueError,match='HOOK_RETAINED_FLOW_UNCONFIRMED'):
        d._adopt_flow(runner,{'inflight_flow':{},'retained_flow_terminal':False})
    assert storage.load_runtime_control()['inflight_flow_id']==flow


@pytest.mark.parametrize('outcome',['cancelled','exception'])
def test_reply_attempt_invalidates_dispatch_receipt_without_callback(lab,backend_peer,tmp_path,outcome):
    manager,credentials,state,response=setup(lab,backend_peer)
    prepared=manager.prepare(credentials,state);scope=prepared.registration.scope
    account={k:getattr(scope,k) for k in ('self_wxid','account_epoch','process_generation')}
    binding={'binding_id':str(uuid.uuid4()),'binding_revision':1,'peer_wxid':'wxid_customer_A',
        'conversation_id':str(uuid.uuid4()),'history_since_create_time':'1','voice_jobs':[],
        'batch_ids':[str(uuid.uuid4()),str(uuid.uuid4())]}
    reads=[];attempts=[]
    class Client:
        def call(self,credentials,method,path,body=None,**kwargs):
            if path.endswith('native-work'):
                return {'schema_version':'hook-native-work-v1','observation_query_version':1,**account,
                    'run_status':'running','inflight_flow':{},'bindings':[binding],
                    'invitations':[],'retained_sends':[],'inflight_task':None}
            if path.endswith('tasks/pull'):return {'task':None}
            return {'reply_action':{'status':'queued'},'task':{'id':str(uuid.uuid4())}}
    class Runtime:
        def __init__(self,*args,**kwargs):
            self.credentials=credentials;self.account=account;self.scope=scope
            self.active_flow_id=None;self.active_task_id=None;self.clock=SimpleNamespace(observe=lambda x:None)
        def barrier(self,b):return (0,0)
        def sync(self,b):
            reads.append(b['binding_id'])
            return {'history_ready':True,'history_state_digest':str(len(reads))}
        def reply_once(self,b,data,*,history):
            attempts.append(history['history_state_digest'])
            if outcome=='exception':raise ValueError('synthetic_send_failure')
            return None
    runner=SimpleNamespace(binding=SimpleNamespace(run_status='running'),stop_event=threading.Event(),
        api=SimpleNamespace(),_can_start_new_flow=lambda:True,can_pull_tasks=lambda:True,
        _worker_transaction_barrier_ready=lambda *a,**kw:True)
    services=SimpleNamespace(connection=manager,_credentials=credentials,startup_window_ready_for=lambda _:True)
    dispatcher=HookBusinessDispatcher(Client(),tmp_path/'continuous',lambda *a:None,lambda *a:None,
        automatic_transcription_disabled=lambda:True,runtime_factory=Runtime)
    try:
        for _ in range(2):
            if outcome=='exception':
                with pytest.raises(ValueError,match='synthetic_send_failure'):dispatcher.tick(runner,services)
            else:dispatcher.tick(runner,services)
            assert not dispatcher._sync_cache
        assert attempts==['1','2']
        assert len(reads)==2
    finally:manager.close()


@pytest.mark.parametrize('fault',[None,'publish','sync'])
def test_transcript_receipt_replaces_pending_cache_without_new_callback(lab,backend_peer,tmp_path,fault):
    manager,credentials,state,response=setup(lab,backend_peer)
    prepared=manager.prepare(credentials,state);scope=prepared.registration.scope
    account={k:getattr(scope,k) for k in ('self_wxid','account_epoch','process_generation')}
    binding={'binding_id':str(uuid.uuid4()),'binding_revision':1,'peer_wxid':'wxid_customer_A',
        'conversation_id':str(uuid.uuid4()),'history_since_create_time':'1','history_ready':False,
        'voice_jobs':[{'native_msg_id':'123'}],'batch_ids':[str(uuid.uuid4())]}
    reads=[];attempts=[];views=[]
    class Client:
        def call(self,credentials,method,path,body=None,**kwargs):
            if path.endswith('native-work'):
                return {'schema_version':'hook-native-work-v1','observation_query_version':1,**account,
                    'run_status':'running','inflight_flow':{},'bindings':[binding],
                    'invitations':[],'retained_sends':[],'inflight_task':None}
            if path.endswith('tasks/pull'):return {'task':None}
            views.append(path)
            if len(views)==1:return {'processing':True}
            return {'reply_action':{'status':'queued'},'task':{'id':str(uuid.uuid4())}}
        def voice_result(self,*args):
            if fault=='publish':raise ValueError('synthetic_transcript_failure')
            binding['history_ready']=True;binding['voice_jobs']=[]
            return {'accepted':True}
    class Runtime:
        def __init__(self,*args,**kwargs):
            self.credentials=credentials;self.account=account;self.scope=scope
            self.active_flow_id=None;self.active_task_id=None;self.clock=SimpleNamespace(observe=lambda x:None)
        def barrier(self,b):return (0,0)
        def sync(self,b):
            reads.append(b['history_ready'])
            if fault=='sync' and b['history_ready']:raise ValueError('synthetic_transcript_failure')
            return {'history_ready':b['history_ready'],'history_state_digest':'transcribed' if b['history_ready'] else 'pending'}
        def reply_once(self,b,data,*,history):
            attempts.append(history['history_state_digest']);return {'status':'sent'}
    runner=SimpleNamespace(binding=SimpleNamespace(run_status='running'),stop_event=threading.Event(),
        api=SimpleNamespace(),_can_start_new_flow=lambda:True,can_pull_tasks=lambda:True,
        _worker_transaction_barrier_ready=lambda *a,**kw:True)
    services=SimpleNamespace(connection=manager,_credentials=credentials,startup_window_ready_for=lambda _:True)
    dispatcher=HookBusinessDispatcher(Client(),tmp_path/'continuous',lambda *a:None,lambda *a:None,
        automatic_transcription_disabled=lambda:True,runtime_factory=Runtime)
    dispatcher.voice=SimpleNamespace(saved_parent=lambda *args:{'state':'transcribed','text':'synthetic transcript'})
    try:
        if fault:
            with pytest.raises(ValueError,match='synthetic_transcript_failure'):dispatcher.tick(runner,services)
            assert not dispatcher._sync_cache and not attempts
            return
        dispatcher.tick(runner,services)
        dispatcher.tick(runner,services)
        assert reads==[False,True]
        assert attempts==['transcribed']
    finally:manager.close()


def test_history_cache_does_not_adopt_callback_arriving_during_read(tmp_path):
    dispatcher=HookBusinessDispatcher(None,tmp_path,lambda *a:None,lambda *a:None,
        automatic_transcription_disabled=lambda:True)
    binding={'binding_id':'binding','binding_revision':1}
    key=('generation','binding',1);barrier=[(0,0)]
    def sync(binding):
        barrier[0]=(1,0)
        return {'history_ready':True,'history_state_digest':'older'}
    dispatcher.runtime=SimpleNamespace(scope=SimpleNamespace(process_generation='generation'),
        barrier=lambda b:barrier[0],sync=sync)
    dispatcher._sync_cache[key]=((0,0),0,{'history_ready':True})
    assert dispatcher._read_history(binding)['history_state_digest']=='older'
    assert not dispatcher._sync_cache


@pytest.mark.parametrize('update_wins', [False, True])
def test_voice_admission_and_update_share_lock(lab, backend_peer, tmp_path, monkeypatch, update_wins):
    """An update arriving between voice discovery and admission wins before DLL I/O."""
    from contextlib import contextmanager
    import chejin_worker_client.hook_dispatcher as module
    manager, credentials, state, _ = setup(lab, backend_peer)
    prepared = manager.prepare(credentials,state);scope=prepared.registration.scope
    account={k:getattr(scope,k) for k in ('self_wxid','account_epoch','process_generation')}
    binding={'binding_id':'synthetic-binding','binding_revision':1,'peer_wxid':'wxid_customer_A',
        'conversation_id':'synthetic-conversation','history_since_create_time':'1','history_ready':True,
        'voice_jobs':[{'native_msg_id':'123'}],'batch_ids':[]}
    gate={'closed':False,'locked':False};calls=[]
    @contextmanager
    def admission():
        gate.update(locked=True,closed=update_wins)
        try:yield
        finally:gate['locked']=False
    class Client:
        def call(self,credentials,method,path,body=None,**kwargs):
            if path.endswith('native-work'):
                return {'schema_version':'hook-native-work-v1','observation_query_version':1,**account,
                    'run_status':'running','inflight_flow':{},'bindings':[binding],
                    'invitations':[],'retained_sends':[],'inflight_task':None}
            return {'task':None}
        def voice_result(self,*args):return {'accepted':True}
    class Runtime:
        def __init__(self,*a,**kw):
            self.credentials=credentials;self.account=account;self.scope=scope
            self.active_flow_id=None;self.active_task_id=None;self.clock=SimpleNamespace(observe=lambda x:None)
        def barrier(self,b):return (0,0)
        def sync(self,b):return {'history_ready':True}
        def timing_context(self,b):return {}
    runner=SimpleNamespace(binding=SimpleNamespace(run_status='running'),stop_event=threading.Event(),
        api=SimpleNamespace(),_new_work_admission_lock=admission(),
        _can_start_new_flow=lambda:not gate['closed'],can_pull_tasks=lambda:True,
        _worker_transaction_barrier_ready=lambda *a,**kw:True)
    services=SimpleNamespace(connection=manager,_credentials=credentials,startup_window_ready_for=lambda _:True)
    dispatcher=HookBusinessDispatcher(Client(),tmp_path/'continuous',lambda *a:None,lambda *a:None,
        automatic_transcription_disabled=lambda:True,runtime_factory=Runtime)
    def transcribe(*a,**kw):
        assert gate['locked'] and not gate['closed']
        calls.append('voice');return {'state':'transcribed','text':'synthetic'}
    dispatcher.voice=SimpleNamespace(saved_parent=lambda *a:None,max_audio_bytes=4096,transcribe_once=transcribe)
    monkeypatch.setattr(module, 'capture_voice_audio',lambda *a,**kw:object())
    try:
        dispatcher.tick(runner,services)
        assert calls == ([] if update_wins else ['voice'])
    finally:manager.close()


@pytest.mark.parametrize('mode',['paused','faulted','startup_pending','retired_during_read','retired_invite'])
def test_relationship_review_respects_gates_and_retirement_is_customer_scoped(lab,backend_peer,tmp_path,mode):
    from chejin_worker_client.api import ApiError
    manager,credentials,state,response=setup(lab,backend_peer)
    prepared=manager.prepare(credentials,state);scope=prepared.registration.scope
    account={k:getattr(scope,k) for k in ('self_wxid','account_epoch','process_generation')}
    bindings=[{'binding_id':str(uuid.uuid4()),'binding_revision':1,'peer_wxid':f'wxid_customer_{i}',
        'conversation_id':str(uuid.uuid4()),'history_since_create_time':'1','voice_jobs':[],
        'batch_ids':[]} for i in range(2)]
    reviews=[];reads=[]
    status='faulted' if mode=='faulted' else 'paused'
    class Client:
        def call(self,credentials,method,path,body=None,**kwargs):
            assert path.endswith('native-work')
            return {'schema_version':'hook-native-work-v1','observation_query_version':1,**account,
                'run_status':status,'inflight_flow':{},'bindings':bindings,'invitations':[{'source_task_id':'old-round'}] if mode=='retired_invite' else [],
                'retained_sends':[],'inflight_task':None,'relationship_query_version':1,'relationship_bindings':bindings}
    class Runtime:
        def __init__(self,*args,**kwargs):
            self.credentials=credentials;self.account=account;self.scope=scope
            self.active_flow_id=None;self.active_task_id=None;self.clock=SimpleNamespace(observe=lambda x:None)
        def review_relationships(self,items):reviews.append([b['binding_id'] for b in items])
        def discover(self,task_id):raise ApiError('HOOK_INVITE_AUTHORIZATION_RETIRED','old invite retired',409)
        def barrier(self,b):return (0,0)
        def sync(self,b):
            reads.append(b['binding_id'])
            if mode=='retired_during_read' and b is bindings[0]:
                raise ApiError('HOOK_AUTHORIZATION_RETIRED','old authorization ended',409)
            return {'history_ready':True}
    runner=SimpleNamespace(binding=SimpleNamespace(run_status=status),stop_event=threading.Event(),
        api=SimpleNamespace(),_can_start_new_flow=lambda:False,can_pull_tasks=lambda:False,
        _worker_transaction_barrier_ready=lambda *a,**kw:True)
    services=SimpleNamespace(connection=manager,_credentials=credentials,startup_window_ready_for=lambda _:mode!='startup_pending')
    dispatcher=HookBusinessDispatcher(Client(),tmp_path/'continuous',lambda *a:None,lambda *a:None,
        automatic_transcription_disabled=lambda:True,runtime_factory=Runtime)
    try:
        dispatcher.tick(runner,services);dispatcher.tick(runner,services)
        if mode in {'faulted','startup_pending'}:assert not reviews and not reads
        else:
            assert reviews==[[b['binding_id'] for b in bindings]]
            assert bindings[1]['binding_id'] in reads
        assert runner.binding.run_status==status
    finally:manager.close()


@pytest.mark.parametrize('current_owner',['ai','human'])
def test_history_owner_supersedes_stale_voice_inventory(lab,backend_peer,tmp_path,monkeypatch,current_owner):
    manager,credentials,state,response=setup(lab,backend_peer)
    prepared=manager.prepare(credentials,state);scope=prepared.registration.scope
    account={k:getattr(scope,k) for k in ('self_wxid','account_epoch','process_generation')}
    binding={'binding_id':'owner-binding','binding_revision':1,'peer_wxid':'wxid_customer_A',
        'conversation_id':'owner-conversation','history_since_create_time':'1','batch_ids':[],
        'reply_ownership':{'owner':'ai','revision':0},'open_handoffs':[],
        'voice_jobs':[{'native_msg_id':'8000000000000000001'}]}
    captured=[]
    monkeypatch.setattr('chejin_worker_client.hook_dispatcher.capture_voice_audio',lambda *a,**kw:captured.append(True))
    class Client:
        def call(self,credentials,method,path,body=None,**kwargs):
            if path.endswith('native-work'):
                return {'schema_version':'hook-native-work-v1','observation_query_version':1,**account,
                    'run_status':'running','inflight_flow':{},'bindings':[binding],
                    'invitations':[],'retained_sends':[],'inflight_task':None}
            if path.endswith('tasks/pull'):return {'task':None}
            pytest.fail(path)
    class Runtime:
        def __init__(self,*a,**kw):
            self.credentials=credentials;self.account=account;self.scope=scope
            self.active_flow_id=self.active_task_id=None;self.clock=SimpleNamespace(observe=lambda _:None)
        def barrier(self,b):return (0,0)
        def sync(self,b):return {'history_ready':True,'reply_ownership':{'owner':current_owner,'revision':1}}
    runner=SimpleNamespace(binding=SimpleNamespace(run_status='running'),stop_event=threading.Event(),api=SimpleNamespace(),
        _can_start_new_flow=lambda:True,can_pull_tasks=lambda:True,_worker_transaction_barrier_ready=lambda *a,**kw:True)
    services=SimpleNamespace(connection=manager,_credentials=credentials,startup_window_ready_for=lambda _:True)
    dispatcher=HookBusinessDispatcher(Client(),tmp_path/'owner',lambda *a:None,lambda *a:None,
        automatic_transcription_disabled=lambda:True,runtime_factory=Runtime)
    try:
        dispatcher.tick(runner,services)
        assert len(captured)==int(current_owner=='ai')
    finally:manager.close()
