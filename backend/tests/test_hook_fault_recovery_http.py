"""Regression coverage derived from architect4 counterexamples: production HTTP/SQLite and dispatcher boundaries.

No physical WeChat, external AI, production database, or production credentials.
UI failure and native-message facts are controlled fixtures, not Windows proof.
"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import threading
import pytest

from conftest import authenticated_admin_dependency
from test_hook_business_http import claimed, post
from test_hook_account_http import client
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from test_hook_native_send_http import permitted
from app.core.database import SessionLocal
from app.models.task import Task
from app.models.c3 import ReplyAction
from sqlalchemy import select
from test_hook_account_http import context
import test_hook_native_send_http
from app.services import c3_service
from app.services.ai_adapter import AIEngineDecision
from app.models.worker import Worker
from app.models.wechat import WechatSessionBinding
from chejin_worker_client import storage
from chejin_worker_client.api import ApiError
from chejin_worker_client.hook_backend import HookWorkerCredentials
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_formal_c1 import _save
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.shared_rules import hook_contract_rules as rules

@pytest.fixture(autouse=True)
def clean():
    from app.core.database import Base, engine
    url=engine.url
    sqlite_test=url.drivername=='sqlite' and '/private/tmp/chejin-hook-formal-20261011/test-' in str(url)
    postgres_test=(url.drivername=='postgresql+psycopg' and url.host=='127.0.0.1'
        and url.port==18831 and url.database=='chejin_hook_gray_recovery_test')
    assert sqlite_test or postgres_test, 'Only the isolated recovery test database is allowed'
    Base.metadata.drop_all(engine);Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def local_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path/'worker_client.sqlite3')


def runtime_for(identity_client, lab, tmp_path):
    server, gateway, process, scope, *_ = lab
    w, task, account = claimed(process_generation=process.generation, pending=True)
    scope = replace(scope, account_epoch=account['account_epoch'])
    prepared = SimpleNamespace(process=process, registration=SimpleNamespace(scope=scope))
    runtime = HookFormalRuntime(HookFormalClient(identity_client.base_url, 5),
        HookWorkerCredentials(w['id'], w['worker_token'], w['instance']),
        gateway, prepared, tmp_path / 'original-runtime')
    return w, task, runtime


def unreadable_ui(prep, guard):
    class UI:
        def guard(self): guard()
        def capture(self, name): raise RuntimeError('OCR_ANCHOR_MISSING_OR_AMBIGUOUS')
    return UI()


def test_c1_technical_error_must_stop_worker_before_next_customer(identity_client, lab, tmp_path):
    w, task, runtime = runtime_for(identity_client, lab, tmp_path)
    result = runtime.invite(task, unreadable_ui)
    assert result['outcome']['error_code'] == 'OCR_ANCHOR_MISSING_OR_AMBIGUOUS'
    assert not result['outcome']['submit_attempted']
    next_lead = client.post('/api/leads', json={
        'customer_name': 'independent counterexample B', 'phones': ['13800000004']})
    assert next_lead.status_code == 200
    with SessionLocal() as db:
        state = db.get(Worker, w['id']).run_status
        assert db.get(Task, task['id']).status == 'failed'
    pulled = client.get(f"/api/workers/{w['id']}/tasks/pull", headers={
        'X-Worker-Token': w['worker_token'], 'X-Client-Instance-Id': w['instance']})
    data = pulled.json()['data']
    print('C1_FAILURE', {'worker_status': state, 'next_task_available': bool(data.get('task'))})
    assert state == 'faulted', 'Technical OCR failure finished as normal failure; Worker still running'


def test_c1_failed_receipt_must_recover_same_sqlite_without_ui(identity_client, lab, tmp_path, monkeypatch):
    w, task, runtime = runtime_for(identity_client, lab, tmp_path)
    original = HookFormalClient.call
    def disconnect(self, credentials, method, path, body=None, **kwargs):
        if path.endswith('/fail'):
            raise ApiError('HOOK_BACKEND_NETWORK_FAILED', 'controlled one-request outage', 0)
        return original(self, credentials, method, path, body, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(HookFormalClient, 'call', disconnect)
        with pytest.raises(ApiError, match='controlled one-request outage'):
            runtime.invite(task, unreadable_ui)
    namespace = runtime.c1.namespace(task['id'])
    assert (namespace / 'outcome.json').exists()
    assert not (namespace / 'submit-attempt.json').exists()
    assert storage.load_runtime_control()['inflight_flow_id'] == task['id']
    # Fresh coordinator, same saved task/SQLite; recovery must not call UI.
    fresh = HookFormalRuntime(runtime.client, runtime.credentials, runtime.gateway,
        runtime.prepared, runtime.private_root)
    fresh.active_flow_id = task['id']
    fresh.active_task_id = task['id']
    inventory = fresh.client.call(fresh.credentials, 'POST',
        f"/workers/{w['id']}/wechat/native-work", {**fresh.account, 'retained_flow_id': task['id']})
    assert inventory['inflight_task']['status'] == 'running'
    d = HookBusinessDispatcher(runtime.client, runtime.private_root, None, None,
        automatic_transcription_disabled=lambda: True)
    d.runtime = fresh
    d._recover_invitation(inventory['inflight_task'])
    with SessionLocal() as db:
        assert db.get(Task, task['id']).status == 'failed'
        assert not db.get(Worker, w['id']).inflight_flow_state


def scheduler_for(w, account, private_root, formal_client, run_status):
    """Real inventory HTTP + production dispatcher + production finish.

    Physical/runtime reads unavailable by design: terminal settlement needs none.
    Local original-flow intent is a controlled starting-state fixture.
    """
    credentials = HookWorkerCredentials(w['id'], w['worker_token'], w['instance'])
    inv = formal_client.call(credentials, 'POST', f"/workers/{w['id']}/wechat/native-work", account)
    flow = inv['inflight_flow']
    _save(private_root / 'flows' / (flow['flow_id'] + '.json'), flow)
    from chejin_worker_client.hook_inbox import HookReceiveScope
    scope = HookReceiveScope(**account, hook_api_port=30001)
    rt = SimpleNamespace(account=account, scope=scope, credentials=credentials,
        client=formal_client, private_root=private_root, active_flow_id=None,
        active_task_id=None, clock=SimpleNamespace(observe=lambda _: None), progress=lambda *args: None)
    rt.batch_view = lambda bid: formal_client.call(credentials, 'GET',
        f"/workers/{w['id']}/wechat/message-batches/{bid}?observe_only=true")
    rt.finish = lambda *args: HookFormalRuntime.finish(rt, *args)
    runner = SimpleNamespace(binding=SimpleNamespace(run_status=run_status),
        stop_event=threading.Event(), api=SimpleNamespace(), current_task=None)
    prepared = SimpleNamespace(registration=SimpleNamespace(scope=scope), account=None)
    services = SimpleNamespace(connection=SimpleNamespace(prepared=prepared), _credentials=credentials,
        startup_window_ready_for=lambda _: False)
    d = HookBusinessDispatcher(formal_client, private_root, None, None,
        automatic_transcription_disabled=lambda: True)
    d.runtime = rt
    return d, runner, services


@pytest.mark.parametrize('run_status', ['paused', 'faulted'])
def test_terminal_chat_flow_must_finish_while_stopped(identity_client, lab, tmp_path, monkeypatch, run_status):
    w, account, binding, headers, permit, _ = permitted(monkeypatch)
    proof = {'reply_action_id': permit['reply_action_id'], 'task_id': permit['task_id'],
        'phase': 'not_triggered', 'reason_code': 'HOOK_RUNTIME_NOT_RUNNING',
        'intent_sha256': None, 'observed_at': __import__('datetime').datetime.now(
            __import__('datetime').timezone.utc).isoformat()}
    response = post(w, f"/workers/{w['id']}/wechat/native-replies/{permit['reply_action_id']}/not-triggered",
        {**account, 'task_id': permit['task_id'], 'send_token': permit['send_token'],
         'proof': proof, 'proof_digest': rules.digest(proof)}, headers)
    assert response.status_code == 200, response.text
    stopped = post(w, f"/workers/{w['id']}/run-status", {'client_instance_id': w['instance'], 'run_status': run_status})
    assert stopped.status_code == 200, stopped.text
    formal_client = HookFormalClient(identity_client.base_url, 5)
    d, runner, services = scheduler_for(w, account, tmp_path / 'dispatcher', formal_client, run_status)
    for _ in range(2): d.tick(runner, services)
    with SessionLocal() as db:
        flow = db.get(Worker, w['id']).inflight_flow_state
    print('TERMINAL_FLOW', {'run_status': run_status, 'retained_flow': bool(flow.get('flow_id'))})
    if run_status == 'faulted':
        resume = post(w, f"/workers/{w['id']}/run-status", {
            'client_instance_id': w['instance'], 'run_status': 'running', 'recover_from_fault': True})
        print('RESUME', {'status': resume.status_code, 'code': resume.json()['code']})
    assert not flow.get('flow_id'), 'Fault guard prevents terminal original Flow settlement'
    assert not storage.load_runtime_control()['inflight_flow_id']


def test_revoked_customer_retains_old_send_settlement_authority(identity_client, lab, tmp_path, monkeypatch):
    w, account, binding, headers, permit, _ = permitted(monkeypatch)
    with SessionLocal() as db:
        lead_id = db.get(WechatSessionBinding, binding['binding_id']).lead_id
    invalid = client.post(f'/api/leads/{lead_id}/mark-invalid', json={'invalid_reason': 'test_data'})
    assert invalid.status_code == 200, invalid.text
    formal_client = HookFormalClient(identity_client.base_url, 5)
    d, runner, services = scheduler_for(w, account, tmp_path / 'revoked-dispatcher', formal_client, 'running')
    seen = []
    d._recover_send = lambda b, m: seen.append((b['binding_id'], m['reply_action_id'])) or False
    d.tick(runner, services)
    assert seen == [(binding['binding_id'], permit['reply_action_id'])], 'Old settlement was incorrectly gated by new business authorization'


@pytest.mark.parametrize('disconnect_at', ['before_receipt', 'after_receipt'])
def test_c1_network_recovery_uses_saved_outcome_after_lease_expiry(identity_client, lab, tmp_path, monkeypatch, disconnect_at):
    from datetime import datetime, timedelta, timezone
    w, task, runtime = runtime_for(identity_client, lab, tmp_path)
    original = HookFormalClient.call
    ui_calls = []
    def ui(prep, guard):
        ui_calls.append(1)
        return unreadable_ui(prep, guard)
    def disconnect(self, credentials, method, path, body=None, **kwargs):
        if path.endswith('/fail'):
            if disconnect_at == 'after_receipt': original(self, credentials, method, path, body, **kwargs)
            raise ApiError('HOOK_BACKEND_NETWORK_FAILED', 'lost receipt', 0)
        return original(self, credentials, method, path, body, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(HookFormalClient, 'call', disconnect)
        with pytest.raises(ApiError): runtime.invite(task, ui)
    with SessionLocal() as db:
        t = db.get(Task, task['id']);t.lease_expires_at = datetime.now(timezone.utc)-timedelta(seconds=90)
        assert db.get(Worker, w['id']).run_status == 'faulted'
        db.commit()
    fresh = HookFormalRuntime(runtime.client, runtime.credentials, runtime.gateway, runtime.prepared, runtime.private_root)
    fresh.active_flow_id = task['id']
    d = HookBusinessDispatcher(runtime.client, runtime.private_root, None, None, automatic_transcription_disabled=lambda: True)
    d.runtime = fresh
    inv = fresh.client.call(fresh.credentials, 'POST', f"/workers/{w['id']}/wechat/native-work", fresh.account)
    d._recover_invitation(inv['inflight_task'])
    assert ui_calls == [1]
    with SessionLocal() as db:
        assert db.get(Task, task['id']).status == 'failed'
        assert not db.get(Worker, w['id']).inflight_flow_state
        assert db.get(Worker, w['id']).run_status == 'faulted'
    assert storage.load_c2_state(f"inflight_finish_receipt:{task['id']}") == {}


@pytest.mark.parametrize('status,cleanup,expected_run', [
    ('not_found', False, 'running'), ('already_friend', False, 'running'),
    ('already_friend', True, 'faulted'), ('not_found', True, 'faulted'),
])
def test_c1_business_result_kept_and_cleanup_failure_stops_new_work(identity_client, lab, tmp_path, monkeypatch, status, cleanup, expected_run):
    from chejin_worker_client import hook_formal_c1
    w, task, runtime = runtime_for(identity_client, lab, tmp_path)
    outcome = {'status': status, 'submit_attempted': False}
    if cleanup: outcome.update(cleanup='failed_preserve_business_outcome', cleanup_error_code='OCR_ANCHOR_MISSING_OR_AMBIGUOUS')
    monkeypatch.setattr(hook_formal_c1, 'run_flow', lambda *a, **kw: outcome)
    result = runtime.invite(task, lambda *args: SimpleNamespace())
    assert result['outcome'] == outcome
    with SessionLocal() as db:
        t = db.get(Task, task['id'])
        assert t.status == ('completed' if status == 'already_friend' else 'failed')
        assert db.get(Worker, w['id']).run_status == expected_run
        assert not db.get(Worker, w['id']).inflight_flow_state


def retained_scenario(identity_client, tmp_path, monkeypatch, *, invalidate=True, trigger=True, outgoing=True):
    from datetime import datetime, timezone
    from app.services import hook_send_service, hook_history_service
    w, account, b, h, permit, value = permitted(monkeypatch)
    since = int(b['history_since_create_time'])
    now = lambda n: datetime.fromtimestamp(since+n, timezone.utc).isoformat()
    monkeypatch.setattr(hook_send_service, 'utcnow', lambda: datetime.fromtimestamp(since+600, timezone.utc))
    monkeypatch.setattr(hook_history_service, 'utcnow', lambda: datetime.fromtimestamp(since+600, timezone.utc))
    private = tmp_path/'retained'
    root = private/'sends'/w['id']/account['account_epoch']/permit['reply_action_id']
    _save(root/'permit.json', permit)
    if trigger:
        intent = {**account, 'schema_version':'hook-send-journal-v1','task_id':permit['task_id'],
            'reply_action_id':permit['reply_action_id'],'peer_wxid':b['peer_wxid'],
            'send_token':permit['send_token'],'attempted_at':now(40)}
        _save(root/'intent.json', intent)
        digest = hashlib.sha256((root/'intent.json').read_bytes()).hexdigest()
        begin = post(w, f"/workers/{w['id']}/wechat/native-replies/{permit['reply_action_id']}/begin",
            {**account,'task_id':permit['task_id'],'history_state_digest':b['history_state_digest'],
             'send_token':permit['send_token'],'journal_sha256':digest,'attempted_at':now(40)},h)
        assert begin.status_code == 200, begin.text
        _save(root/'trigger.json', {'phase':'trigger_attempted','intent_sha256':digest})
        _save(root/'outcome.json', {'task_id':permit['task_id'],'reply_action_id':permit['reply_action_id'],
            'call_completed':outgoing,'automatic_resend_allowed':False,'journal_sha256':digest,'completed_at':now(41)})
    if outgoing:
        for scan in value['scans']:
            d = scan['databases'][0];rows = d['pages'][0]
            rows.append({**rows[0],'localId':'7803','CreateTime':str(since+40),'Sequence':str((since+40)*1000),
                'MsgSequence':'780013','MsgSvrID':'8000000000000000003','IsSender':'1','StrContent':permit['reply_text']})
            d['row_count']=d['count_after']='3';d['upper_local_id']='7803';scan['observed_at']=now(50)
    if invalidate:
        with SessionLocal() as db:lead_id=db.get(WechatSessionBinding,b['binding_id']).lead_id
        r=client.post(f'/api/leads/{lead_id}/mark-invalid',json={'invalid_reason':'test_data'})
        assert r.status_code==200,r.text
        # Explicit revocation of listening must not revoke original receipt authority.
        with SessionLocal() as db:
            binding=db.get(WechatSessionBinding,b['binding_id']);binding.authorization_revision+=1
            binding.allow_listening=False;db.commit()
    formal=HookFormalClient(identity_client.base_url,5)
    d,runner,services=scheduler_for(w,account,private,formal,'running')
    d.runtime.clock.now=lambda:datetime.now(timezone.utc)
    d.runtime.sync=lambda binding,*,reconciliation:formal.history_coverage(d.runtime.credentials,account,binding,
        str(__import__('uuid').uuid4()),value,reconciliation=reconciliation)
    return w,account,b,h,permit,value,d,runner,services


@pytest.mark.parametrize('trigger,outgoing', [(False,False),(True,False),(True,True)])
def test_invalid_customer_original_send_settles_and_flow_finishes_without_new_work(identity_client, tmp_path, monkeypatch, trigger, outgoing):
    from sqlalchemy import select,func
    from app.models.c3 import ReplyAction,MessageBatch,SentAck
    from app.services import c3_service
    w,account,b,h,permit,value,d,runner,services=retained_scenario(identity_client,tmp_path,monkeypatch,trigger=trigger,outgoing=outgoing)
    monkeypatch.setattr(c3_service,'generate_for_batch',lambda *a,**kw:pytest.fail('Settlement launched AI'))
    with SessionLocal() as db:
        before=db.scalar(select(func.count()).select_from(MessageBatch))
        digest=db.get(WechatSessionBinding,b['binding_id']).native_context['history_state_digest']
    for _ in range(3):d.tick(runner,services)
    with SessionLocal() as db:
        assert not db.get(Worker,w['id']).inflight_flow_state
        assert db.get(Task,permit['task_id']).status in {'completed','failed','cancelled'}
        assert db.scalar(select(func.count()).select_from(SentAck))==1
        assert db.scalar(select(func.count()).select_from(MessageBatch))==before
        binding=db.get(WechatSessionBinding,b['binding_id'])
        assert not binding.allow_listening
        assert binding.native_context['history_state_digest']==digest  # no business checkpoint from recovery-only history
        action=db.get(ReplyAction,permit['reply_action_id'])
        saved=action.ai_payload.get('native_send_settlement') or action.ai_payload.get('native_no_trigger_settlement')
        assert saved and saved['result']['automatic_resend_allowed'] is False
        if trigger:assert saved['result']['local_send_observed']==outgoing
    inv=d.client.call(d.runtime.credentials,'POST',f"/workers/{w['id']}/wechat/native-work",account)
    assert not inv['bindings'] and not inv['retained_sends']
    # No new history/permit authority is granted to this invalid customer.
    r=d.client
    with pytest.raises(ApiError):r.history_coverage(d.runtime.credentials,account,b,str(__import__('uuid').uuid4()),value)
    with pytest.raises(ApiError):r.native_send_claim(d.runtime.credentials,account,permit['task_id'],permit['reply_action_id'],permit['lease_fencing_token'],b['history_state_digest'])


@pytest.mark.parametrize('change',['binding','fence','flow','token','journal','account','original_process'])
def test_reconciliation_history_rejects_changed_original_identity(identity_client,tmp_path,monkeypatch,change):
    from dataclasses import replace
    from chejin_worker_client.hook_sender import RetainedNativeSendPermit
    from app.models.hook_native import HookHistoryReceipt
    from sqlalchemy import select,func
    import uuid
    w,account,b,h,permit,value,d,runner,services=retained_scenario(identity_client,tmp_path,monkeypatch)
    inv=d.client.call(d.runtime.credentials,'POST',f"/workers/{w['id']}/wechat/native-work",account)
    meta=inv['retained_sends'][0];binding=meta['reconciliation_binding']
    retained,outcome=RetainedNativeSendPermit.load(d.private_root/'sends',d.runtime.credentials,d.runtime.scope,binding,meta)
    fields={'fence':('lease_fencing_token',999),'flow':('flow_id',str(uuid.uuid4())),
        'token':('send_token','wrong'),'journal':('journal_sha256','f'*64),
        'account':('account_epoch',str(uuid.uuid4())),'original_process':('process_generation','other-process')}
    if change=='binding':binding={**binding,'binding_id':str(uuid.uuid4())}
    else:k,v=fields[change];retained=replace(retained,**{k:v})
    with SessionLocal() as db:before=db.scalar(select(func.count()).select_from(HookHistoryReceipt))
    with pytest.raises(ApiError):d.client.history_coverage(d.runtime.credentials,account,binding,str(uuid.uuid4()),value,reconciliation=retained)
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(HookHistoryReceipt))==before


def test_c1_lost_flow_finish_ack_is_reconciled_without_reopening_ui(identity_client,lab,tmp_path,monkeypatch):
    w,task,rt=runtime_for(identity_client,lab,tmp_path)
    original=HookFormalClient.call
    def lost(self,credentials,method,path,body=None,**kw):
        result=original(self,credentials,method,path,body,**kw)
        if path.endswith('/inflight-flow/finish'):raise ApiError('HOOK_BACKEND_NETWORK_FAILED','lost finish ack',0)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(HookFormalClient,'call',lost)
        with pytest.raises(ApiError):rt.invite(task,unreadable_ui)
    assert storage.load_runtime_control()['inflight_flow_id']==task['id']
    receipt=storage.load_c2_state(f"inflight_finish_receipt:{task['id']}")
    assert receipt['task_failure_confirmed']
    inv=rt.client.call(rt.credentials,'POST',f"/workers/{w['id']}/wechat/native-work",
        {**rt.account,'retained_flow_id':task['id']})
    assert inv['retained_flow_terminal'] and not inv['inflight_task']
    d=HookBusinessDispatcher(rt.client,rt.private_root,None,None,automatic_transcription_disabled=lambda:True);d.runtime=rt
    runner=SimpleNamespace(api=SimpleNamespace())
    d._adopt_flow(runner,inv)
    assert not storage.load_runtime_control()['inflight_flow_id']
    assert storage.load_c2_state(f"inflight_finish_receipt:{task['id']}")=={}


@pytest.mark.parametrize('stop_status', ['paused', 'faulted', 'local_faulted', 'faulted_sending'])
def test_segment_tail_does_not_deadlock_fault_recovery(identity_client, tmp_path, monkeypatch, stop_status):
    class Model:
        def generate_reply_decision(self, **kwargs):
            parts = ['这台车的资料需要按实际检测结果核实，'*3+'我们会说明已知的情况。',
                     '另一台车也需要核实使用情况，'*4+'不能保证没有未发现的问题。']
            return AIEngineDecision(decision='send_reply', reply_text=' '.join(parts), guard_result='pass',
                raw_payload={'omniauto_brain_result': {'brain_plan': {'reply_segments': parts}}})
    monkeypatch.setattr(c3_service, 'get_ai_engine_adapter', Model)
    original_bound = test_hook_native_send_http.bound
    def capable_bound():
        w, task, account, binding = original_bound()
        response = post(w, f"/workers/{w['id']}/heartbeat", {
            'client_instance_id': w['instance'], 'running_status': 'idle', 'run_status': 'running',
            'rpa_component_status': 'ready', 'wechat_status': 'logged_in',
            'hook_context': {**context(), **account},
            'local_lock_summary': {'capabilities': {'reply_sequence_version': 1}},
        })
        assert response.status_code == 200, response.text
        return w, task, account, binding
    monkeypatch.setattr(test_hook_native_send_http, 'bound', capable_bound)
    w, account, binding, headers, permit, value, d, runner, services = retained_scenario(
        identity_client, tmp_path, monkeypatch, invalidate=False, trigger=True, outgoing=True)
    # The production retained-send branch settles the first segment from its
    # original evidence. No synthetic DB change makes a task appear completed.
    d.tick(runner, services)
    with SessionLocal() as db:
        actions = list(db.scalars(select(ReplyAction).order_by(ReplyAction.segment_index)))
        assert [a.status for a in actions] == ['sent', 'queued']
    backend_status='running' if stop_status=='local_faulted' else 'faulted' if stop_status=='faulted_sending' else stop_status
    response = post(w, f"/workers/{w['id']}/run-status", {
        'client_instance_id': w['instance'], 'run_status': backend_status})
    assert response.status_code == 200, response.text
    runner.binding.run_status = 'faulted' if stop_status in {'local_faulted','faulted_sending'} else stop_status
    if stop_status=='faulted_sending':
        # Controlled backend guard fixture: an unresolved physical send must
        # block the original finish entrance, even though the first task ended.
        with SessionLocal() as db:
            tail=db.scalar(select(ReplyAction).where(ReplyAction.status=='queued'))
            tail.status='sending';db.commit()
        with pytest.raises(ApiError) as blocked:d.tick(runner,services)
        assert blocked.value.code=='WORKER_INFLIGHT_FLOW_NOT_SETTLED'
    else:
        for _ in range(3): d.tick(runner, services)
    with SessionLocal() as db:
        flow = db.get(Worker, w['id']).inflight_flow_state
        statuses = list(db.scalars(select(ReplyAction.status).order_by(ReplyAction.segment_index)))
    print('SEGMENT_RECOVERY', {'stop_status': stop_status, 'flow_retained': bool(flow.get('flow_id')), 'actions': statuses})
    if stop_status in {'paused','local_faulted'}:
        assert flow.get('flow_id') and statuses == ['sent', 'queued'], 'Pause/local-only fault must not cancel remaining segments'
    elif stop_status=='faulted_sending':
        assert flow.get('flow_id') and statuses==['sent','sending']
    else:
        resume = post(w, f"/workers/{w['id']}/run-status", {
            'client_instance_id': w['instance'], 'run_status': 'running', 'recover_from_fault': True})
        print('FAULT_RESUME', {'status': resume.status_code, 'code': resume.json()['code']})
        assert not flow.get('flow_id'), 'Faulted segmented reply waits for its forbidden next segment'
        assert statuses == ['sent', 'cancelled']


def test_prepare_request_outage_recovers_without_first_ui_action(identity_client, lab, tmp_path, monkeypatch):
    w, task, rt = runtime_for(identity_client, lab, tmp_path)
    real_call = HookFormalClient.call
    ui_calls = []
    def ui(preparation, guard):
        ui_calls.append(1)
        return unreadable_ui(preparation, guard)
    def fail_prepare(self, credentials, method, path, body=None, **kwargs):
        if path.endswith('/hook-invite/prepare'):
            raise ApiError('HOOK_BACKEND_NETWORK_FAILED', 'controlled prepare-request outage', 0)
        return real_call(self, credentials, method, path, body, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(HookFormalClient, 'call', fail_prepare)
        with pytest.raises(ApiError): rt.invite(task, ui)
    assert ui_calls == []
    assert not (rt.c1.namespace(task['id'])/'preparation.json').exists()
    assert not (rt.c1.namespace(task['id'])/'submit-attempt.json').exists()
    fresh = HookFormalRuntime(rt.client, rt.credentials, rt.gateway, rt.prepared, rt.private_root)
    fresh.active_flow_id = task['id']
    inv = fresh.client.call(fresh.credentials, 'POST', f"/workers/{w['id']}/wechat/native-work", fresh.account)
    assert inv['inflight_task']['status'] == 'running'
    d = HookBusinessDispatcher(rt.client, rt.private_root, None, None, automatic_transcription_disabled=lambda: True)
    d.runtime = fresh
    d._recover_invitation(inv['inflight_task'])
    with SessionLocal() as db:
        assert db.get(Task, task['id']).status in {'failed', 'cancelled'}
        assert not db.get(Worker, w['id']).inflight_flow_state
    assert ui_calls == []


@pytest.mark.parametrize('point', ['before_prepare', 'prepare_ack_lost', 'after_prepare'])
@pytest.mark.parametrize('lost_settlement_ack', [False, True])
def test_c1_pre_ui_interruption_replays_only_original_receipt(identity_client, lab, tmp_path, monkeypatch,
                                                            point, lost_settlement_ack):
    w, task, rt = runtime_for(identity_client, lab, tmp_path)
    real_call=HookFormalClient.call
    def disconnect(self, credentials, method, path, body=None, **kwargs):
        if path.endswith('/hook-invite/prepare'):
            if point=='prepare_ack_lost': real_call(self,credentials,method,path,body,**kwargs)
            if point!='after_prepare': raise ApiError('HOOK_BACKEND_NETWORK_FAILED','prepare outage',0)
        return real_call(self,credentials,method,path,body,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(HookFormalClient,'call',disconnect)
        if point=='after_prepare':
            original_heartbeat=rt.heartbeat
            def heartbeat(task_id=None, **kwargs):
                if task_id: raise ApiError('HOOK_BACKEND_NETWORK_FAILED','heartbeat outage',0)
                return original_heartbeat(task_id,**kwargs)
            patch.setattr(rt,'heartbeat',heartbeat)
        with pytest.raises(ApiError):
            rt.invite(task,lambda *args:pytest.fail('UI must not start'))
    # Preserve the original expired fencing token; settlement must not claim again.
    from datetime import timedelta
    from app.models.base import utcnow
    with SessionLocal() as db:
        saved=db.get(Task,task['id']);saved.lease_expires_at=utcnow()-timedelta(seconds=30);db.commit()
    fresh=HookFormalRuntime(rt.client,rt.credentials,rt.gateway,rt.prepared,rt.private_root)
    fresh.active_flow_id=task['id']
    d=HookBusinessDispatcher(rt.client,rt.private_root,None,None,automatic_transcription_disabled=lambda:True)
    d.runtime=fresh
    def inventory():
        return fresh.client.call(fresh.credentials,'POST',f"/workers/{w['id']}/wechat/native-work",fresh.account)['inflight_task']
    if lost_settlement_ack:
        def lose_ack(self,credentials,method,path,body=None,**kwargs):
            result=real_call(self,credentials,method,path,body,**kwargs)
            if path.endswith('/fail'):raise ApiError('HOOK_BACKEND_NETWORK_FAILED','settlement ack lost',0)
            return result
        with monkeypatch.context() as patch:
            patch.setattr(HookFormalClient,'call',lose_ack)
            with pytest.raises(ApiError):d._recover_invitation(inventory())
    d._recover_invitation(inventory())
    with SessionLocal() as db:
        saved=db.get(Task,task['id']);worker=db.get(Worker,w['id'])
        assert saved.status=='failed' and saved.error_code=='WORKER_INTERRUPTED'
        assert worker.run_status=='paused' and not worker.inflight_flow_state
    assert not storage.load_runtime_control().get('inflight_flow_id')
    assert not (rt.c1.namespace(task['id'])/'ui-started.json').exists()


@pytest.mark.parametrize('alter', ['missing', 'task_id', 'flow_id', 'lease_fencing_token', 'worker_id',
                                   'client_instance_id', 'self_wxid', 'account_epoch', 'phase',
                                   'ui-started.json', 'submit-attempt.json', 'submission-proof.json'])
def test_c1_pre_ui_recovery_rejects_missing_identity_and_started_actions(identity_client, lab, tmp_path,
                                                                       monkeypatch, alter):
    w,task,rt=runtime_for(identity_client,lab,tmp_path)
    real_call=HookFormalClient.call
    def disconnect(self,credentials,method,path,body=None,**kwargs):
        if path.endswith('/hook-invite/prepare'):raise ApiError('HOOK_BACKEND_NETWORK_FAILED','outage',0)
        return real_call(self,credentials,method,path,body,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(HookFormalClient,'call',disconnect)
        with pytest.raises(ApiError):rt.invite(task,lambda *args:pytest.fail('No UI'))
    root=rt.c1.namespace(task['id']);intent=root/'pre-ui.json'
    if alter=='missing':intent.unlink()
    elif alter.endswith('.json'):(root/alter).write_text('{}')
    else:
        value=json.loads(intent.read_text());value[alter]='mismatch';intent.write_text(json.dumps(value))
    current=rt.client.call(rt.credentials,'POST',f"/workers/{w['id']}/wechat/native-work",rt.account)['inflight_task']
    with pytest.raises(ValueError,match='IDENTITY_MISMATCH|REQUIRES_ORIGINAL_EVIDENCE'):
        rt.c1_results.settle_before_ui(current)
    with SessionLocal() as db:
        assert db.get(Task,task['id']).status=='running'
        assert db.get(Worker,w['id']).inflight_flow_state


def test_ui_factory_failure_is_not_proof_of_untouched_ui(identity_client,lab,tmp_path):
    w,task,rt=runtime_for(identity_client,lab,tmp_path)
    calls=[]
    def fail_ui(*args):
        calls.append(1);raise RuntimeError('constructor interrupted')
    with pytest.raises(RuntimeError,match='constructor interrupted'):rt.invite(task,fail_ui)
    assert (rt.c1.namespace(task['id'])/'ui-started.json').exists()
    current=rt.client.call(rt.credentials,'POST',f"/workers/{w['id']}/wechat/native-work",rt.account)['inflight_task']
    d=HookBusinessDispatcher(rt.client,rt.private_root,None,None,automatic_transcription_disabled=lambda:True)
    d.runtime=rt
    with pytest.raises(ValueError,match='REQUIRES_ORIGINAL_EVIDENCE'):d._recover_invitation(current)
    assert calls==[1]
