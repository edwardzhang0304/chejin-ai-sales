"""Real two-pass SQLite/HTTP reads through scheduler and business wait boundaries."""
from dataclasses import replace
from types import SimpleNamespace
import threading
import uuid

import pytest

from test_hook_gateway import lab
from test_hook_history import history_lab
from chejin_worker_client import storage
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.hook_history_sync import HookHistorySynchronizer
from chejin_worker_client.hook_worker_services import HookWorkerServices
from chejin_worker_client.hook_gateway import HookLocalError
from chejin_worker_client.shared_rules import hook_history_rules


@pytest.mark.parametrize('change_kind', ['rows', 'catalog'])
@pytest.mark.parametrize('pause_before_retry', [False, True])
def test_changed_customer_yields_to_other_then_reads_fresh_before_reply(history_lab, tmp_path, pause_before_retry, change_kind):
    server, gateway, process, scope, delivery, _, db = history_lab
    db.execute('DELETE FROM MSG WHERE Type=34')
    account = {k: getattr(scope, k) for k in ('self_wxid','account_epoch','process_generation')}
    credentials = SimpleNamespace(worker_id='synthetic-worker')
    bindings = [dict(binding_id=str(uuid.uuid4()), binding_revision=1,
                     conversation_id=str(uuid.uuid4()), peer_wxid=f'wxid_customer_{peer}',
                     voice_jobs=[], batch_ids=[str(uuid.uuid4())]) for peer in ('A','B')]
    run_status = ['running']
    changed = []
    verified = {}
    reads = []
    sends = []
    saved = []

    def save(capture):
        saved.append(capture)
        # A normal new message lands between the first and second scans.
        if change_kind == 'rows' and capture.query.kind == 'count' and not changed:
            db.execute('INSERT INTO MSG VALUES(5,9005,1,0,1791543700,1791543700000,1,14,?,?)',
                       (delivery.peer_wxid, 'synthetic new message'))
            changed.append(True)

    def change_catalog(path):
        if change_kind == 'catalog' and path == '/QueryDB' and not changed:
            server.catalog_entries[0]['dbhandle'] = 'reopened-handle'
            changed.append(True)
    server.on_request = change_catalog

    class Client:
        def call(self, credentials, method, path, body=None, **kwargs):
            if path.endswith('native-work'):
                return dict(schema_version='hook-native-work-v1', observation_query_version=1,
                            **account, run_status=run_status[0], inflight_flow={}, bindings=bindings,
                            invitations=[], retained_sends=[], inflight_task=None)
            if path.endswith('tasks/pull'): return {'task': None}
            b = next(b for b in bindings if b['batch_ids'][0] in path)
            if b['binding_id'] in sends:
                return {'processing': True, 'observation_only':run_status[0]!='running'}
            return {'reply_action': {'status':'queued'}, 'task': {'id':str(uuid.uuid4())},
                    'observation_only':run_status[0]!='running'}

    class Runtime:
        def __init__(self, *args, **kwargs):
            self.credentials=credentials; self.account=account; self.scope=scope
            self.active_flow_id=None; self.active_task_id=None
            self.clock=SimpleNamespace(observe=lambda _:None)
        def barrier(self, binding): return (0,0)
        def sync(self, binding):
            reads.append(binding['binding_id'])
            current = replace(delivery, peer_wxid=binding['peer_wxid'])
            sync = HookHistorySynchronizer(gateway, save, lambda:(0,0))
            _, coverage = sync.capture(process,scope,current,'1791543600')
            proof = hook_history_rules.validate_coverage(coverage,current.peer_wxid,1791543600)
            verified[binding['binding_id']] = proof
            return {'history_ready': proof['ready']}
        def reply_once(self,binding,data,*,history):
            assert run_status[0]=='running' and history['history_ready']
            assert verified[binding['binding_id']]['ready']
            sends.append(binding['binding_id'])
            return {'status':'sent'}

    runner=SimpleNamespace(binding=SimpleNamespace(run_status='running'),stop_event=threading.Event(),
                           api=SimpleNamespace(),_can_start_new_flow=lambda:run_status[0]=='running',
                           can_pull_tasks=lambda:run_status[0]=='running',
                           _worker_transaction_barrier_ready=lambda *a,**kw:True)
    prepared=SimpleNamespace(process=process,registration=SimpleNamespace(scope=scope),account={})
    services=SimpleNamespace(connection=SimpleNamespace(prepared=prepared,gateway=gateway),
                             _credentials=credentials,startup_window_ready_for=lambda _:True)
    dispatcher=HookBusinessDispatcher(Client(),tmp_path/'private',lambda *a:None,lambda *a:None,
        automatic_transcription_disabled=lambda:True,runtime_factory=Runtime)
    dispatcher.tick(runner,services)
    assert changed and sends==[bindings[1]['binding_id']]
    assert bindings[0]['binding_id'] not in verified
    assert runner.binding.run_status=='running'
    assert not any(key[1]==bindings[0]['binding_id'] for key in dispatcher._sync_cache)
    with storage.db_connection() as conn:
        waits=conn.execute("SELECT error_code FROM local_logs WHERE event='hook_business_wait'").fetchall()
    expected_code = 'HOOK_HISTORY_CHANGED_DURING_READ' if change_kind == 'rows' else 'HOOK_HISTORY_CATALOG_CHANGED'
    assert any(row[0]==expected_code for row in waits)
    if pause_before_retry:
        run_status[0]='paused'; runner.binding.run_status='paused'
    dispatcher.tick(runner,services)
    assert reads.count(bindings[0]['binding_id'])==2
    ids = {r['MsgSvrID'] for r in verified[bindings[0]['binding_id']]['text_rows']}
    assert ('9005' in ids) is (change_kind == 'rows')
    assert sends==[bindings[1]['binding_id']]+([] if pause_before_retry else [bindings[0]['binding_id']])
    assert runner.binding.run_status==run_status[0]
    assert not any(path=='/SendTextMsg' for path,*_ in server.requests)


def test_voice_arriving_between_scans_requires_fresh_read_and_audio(history_lab):
    _,gateway,process,scope,delivery,_,db=history_lab
    db.execute('DELETE FROM MSG WHERE Type=34')
    saved=[]
    def save(capture):
        saved.append(capture)
        if capture.query.kind=='count' and not any(c.query.kind=='count' for c in saved[:-1]):
            db.execute('INSERT INTO MSG VALUES(5,9005,34,0,1791543700,1791543700000,1,14,?,NULL)',(delivery.peer_wxid,))
    sync=HookHistorySynchronizer(gateway,save,lambda:(0,0),media_enabled=True)
    with pytest.raises(ValueError,match='^HOOK_HISTORY_CHANGED_DURING_READ$'):
        sync.capture(process,scope,delivery,'1791543600')
    _,coverage=sync.capture(process,scope,delivery,'1791543600')
    proof=hook_history_rules.validate_coverage(coverage,delivery.peer_wxid,1791543600)
    # Coverage certifies the metadata read, not the availability of audio or
    # text. The separate audio admission gate must still withhold transcription.
    assert proof['ready'] and [r['MsgSvrID'] for r in proof['media_rows']]==['9005']
    assert '9005' not in {r['MsgSvrID'] for r in proof['text_rows']}
    from chejin_worker_client.hook_voice import capture_voice_audio
    assert capture_voice_audio(scope,delivery.peer_wxid,'9005',max_audio_bytes=1024*1024) is None


@pytest.mark.parametrize('error_type', [ValueError, HookLocalError])
@pytest.mark.parametrize('code,waits', [
    ('HOOK_HISTORY_CHANGED_DURING_READ',True),
    ('HOOK_HISTORY_CALLBACK_BARRIER_CHANGED',True),
    ('HOOK_HISTORY_CATALOG_CHANGED',True),
    ('HOOK_HISTORY_ROW_SCOPE_INVALID',False),
    ('HOOK_HISTORY_MESSAGE_ID_UNRESOLVED',False),
    ('HOOK_PROCESS_GENERATION_CHANGED',False),
    ('HOOK_HISTORY_STORAGE_CAPACITY_EXCEEDED',False),
])
def test_business_recovery_boundary_waits_only_for_read_holds(code,waits,error_type):
    runner=SimpleNamespace(binding=SimpleNamespace(run_status='paused'),stop_event=threading.Event())
    reports=[]; faults=[]
    def tick(*args):
        runner.stop_event.set()
        raise error_type(code)
    services=HookWorkerServices.__new__(HookWorkerServices)
    services.dispatcher=SimpleNamespace(tick=tick,interval_seconds=.01,
                                       _report_wait=lambda value,**kwargs:reports.append(value))
    services._fault=lambda runner,value,**kwargs:faults.append(value)
    services.business_loop(runner,lambda _:None)
    assert reports==([code] if waits else [])
    assert faults==([] if waits else [code])
    assert runner.binding.run_status=='paused'


def test_unknown_action_outcome_cannot_be_reclassified_as_history_wait():
    from chejin_worker_client.hook_history_wait import is_history_wait
    assert not is_history_wait(HookLocalError("HOOK_HISTORY_CATALOG_CHANGED", outcome_unknown=True))
    assert not is_history_wait(RuntimeError("HOOK_HISTORY_CATALOG_CHANGED"))
