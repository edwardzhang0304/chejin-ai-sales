"""Cancelled segmented continuation: real HTTP, PG, Worker threads, same SQLite.

Only model/WeChat I/O and transport failures are controlled. The Worker owns
receipt replay and Flow finish; marking invalid uses the ordinary admin route.
"""
import json
import os
import subprocess
import sys

import pytest
from fastapi import Request
from sqlalchemy import select

from app.core.auth import require_admin_auth
from app.core.database import SessionLocal
from app.models.c3 import Conversation, HandoffEvent, ReplyAction, SentAck
from app.models.worker import Worker
from app.services import c3_service
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
from test_reply_sequence_http import SequenceModel
import test_c3_api as fixtures
import test_reply_sequence_worker as scenario


@pytest.fixture
def admin(monkeypatch):
    def auth(request: Request):
        request.state.auth_actor = {
            'operator_id': '00000000-0000-0000-0000-000000000001',
            'operator_name': 'Cancellation test', 'actor_type': 'admin_account', 'session_id': 'test',
        }
    monkeypatch.setitem(fixtures.app.dependency_overrides, require_admin_auth, auth)


def cancellation_worker_script():
    script = scenario.WORKER
    script = script.replace('exchanges=[]', "exchanges=[]\ntransport_blocks=0\nwechat_reads=[]")
    script = script.replace(' global injected_status_losses', ''' global injected_status_losses,transport_blocks
 if mode=='resume' and os.environ.get('CANCEL_BLOCK')=='ack' and request.url.endswith('/sent-ack'):
  transport_blocks+=1
  raise requests.ConnectionError('original ack still cannot reach server')
 if mode=='resume' and os.environ.get('CANCEL_BLOCK')=='status_network' and '/message-batches/' in request.url:
  transport_blocks+=1
  raise requests.ConnectionError('temporary batch status outage')''')
    script = script.replace(' response=original(request,**kwargs)', ''' response=original(request,**kwargs)
 if mode=='resume' and os.environ.get('CANCEL_BLOCK')=='cancel_proof' and '/message-batches/' in request.url and response.status_code==200:
  body=response.json()
  body['data']['authorization'].pop('recovery_decision',None)
  response._content=json.dumps(body).encode()
  transport_blocks+=1''')
    script = script.replace(' def get_messages(self,**kwargs):', ' def get_messages(self,**kwargs):\n  wechat_reads.append(mode)')
    script = script.replace("  started=runner.set_run_status('running')\n  assert started, {'errors':errors,'exchanges':exchanges}\n", '')
    script = script.replace('''  for _ in range(200):
   if not load_runtime_control()['inflight_flow_id']: break
   time.sleep(.1)''', '''  admission_samples=[]
  deadline=time.monotonic()+10
  settlement_complete=False
  while True:
   control=load_runtime_control()
   sample={'flow':control.get('inflight_flow_id'),'pending_finish':dict(runner._pending_flow_finish or {}),
    'allowed':runner._can_start_new_flow(),'run_status':load_binding().run_status,'pause_requested':control.get('pause_requested')}
   admission_samples.append(sample)
   observer=globals().get('observe_settlement_sample')
   if observer: observer(sample)
   paused=bool(os.environ.get('CANCEL_PAUSED'))
   settlement_complete=not sample['flow'] and not sample['pending_finish'] and (
    sample['run_status']=='paused' and sample['pause_requested'] and not sample['allowed'] if paused
    else sample['run_status']=='running' and sample['allowed'])
   if settlement_complete or time.monotonic()>=deadline:break
   time.sleep(.05)''')
    script = script.replace('  runner.start(binding)', "  original_flow=load_runtime_control()['inflight_flow_id']\n  runner.start(binding)")
    script = script.replace('  runner.stop_event.set()', '  new_work_allowed=runner._can_start_new_flow()\n  runner.stop_event.set()')
    script = script.replace(" out={'result':result", " if mode!='resume' and os.environ.get('CANCEL_PAUSED'):\n  assert runner.set_run_status('paused')\n out={'result':result")
    script = script.replace(' print(json.dumps(out,ensure_ascii=False,default=str))', ''' from chejin_worker_client.storage import load_c2_state
 out.update(transport_blocks=transport_blocks,wechat_reads=wechat_reads,run_status=load_binding().run_status,
  new_work_allowed=new_work_allowed if mode=='resume' else False,
  settlement_complete=settlement_complete if mode=='resume' else False,
  admission_samples=admission_samples if mode=='resume' else [],
  sequence_marker=load_c2_state('reply_sequence_flow:'+original_flow) if mode=='resume' else {})
 print(json.dumps(out,ensure_ascii=False,default=str))''')
    return script


@pytest.mark.parametrize('loss,paused,block', [
    ('typing_request_loss', False, ''), ('typing_response_loss', False, ''),
    ('typing_request_loss', True, ''), ('typing_response_loss', True, ''),
    ('typing_request_loss', False, 'ack'),
    ('typing_response_loss', False, 'cancel_proof'),
    ('typing_response_loss', False, 'status_network'),
])
def test_invalid_customer_settles_without_ui_or_unpausing(
    http_api, isolated_db, monkeypatch, async_generation, admin, tmp_path, loss, paused, block,
):
    monkeypatch.setattr(fixtures, 'client', http_api)
    monkeypatch.setattr(c3_service, 'get_ai_engine_adapter', SequenceModel)
    worker, binding = fixtures._setup_bound_conversation()
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding['conversation_id'])
        conversation.status = 'waiting_user_reply'
        lead_id = conversation.lead_id
        db.get(Worker, worker['id']).local_lock_summary = {'capabilities': {'reply_sequence_version': 1}}
        db.commit()
    script = tmp_path / 'worker.py'
    script.write_text(cancellation_worker_script())
    env = {**os.environ, 'CHEJIN_WORKER_HOME': str(tmp_path / 'worker'), 'CHEJIN_RPA_MODE': 'mock'}
    if paused:
        env['CANCEL_PAUSED'] = '1'
    base = http_api.get('/healthz').url.removesuffix('/healthz')

    def run(mode, label=None):
        process = subprocess.run(
            [sys.executable, str(script), base, json.dumps(worker), binding['conversation_id'], mode],
            env=env, text=True, capture_output=True, timeout=45,
        )
        (tmp_path / ((label or mode) + '.stdout')).write_text(process.stdout)
        (tmp_path / ((label or mode) + '.stderr')).write_text(process.stderr)
        assert process.returncode == 0, process.stderr
        return json.loads(process.stdout.strip().splitlines()[-1])

    first = run(loss)
    original_flow = first['runtime']['inflight_flow_id']
    assert original_flow and first['pending_ack'] and first['sent'] == []
    invalidated = http_api.post(f'/api/leads/{lead_id}/mark-invalid', json={'invalid_reason': 'test_data'})
    assert invalidated.status_code == 200, invalidated.text
    env['CANCEL_BLOCK'] = block
    resumed = run('resume')
    with SessionLocal() as db:
        active = db.get(Worker, worker['id'])
        evidence = {
            'original_flow_id': original_flow, 'loss': loss, 'paused': paused, 'block': block,
            'resumed': resumed, 'backend_flow': active.inflight_flow_state,
            'backend_run_status': active.run_status,
            'acks': [{'result': a.send_result, 'phase': a.action_phase} for a in db.scalars(select(SentAck))],
            'actions': [a.status for a in db.scalars(select(ReplyAction))],
            'handoffs': [h.handoff_reason_code for h in db.scalars(select(HandoffEvent))],
            'generation': async_generation['counts'],
        }
    (tmp_path / 'cancellation-evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
    assert resumed['sent'] == [] and resumed['wechat_reads'] == []
    assert not resumed['locked'] and not resumed['pending_c2_outbox']
    assert not evidence['handoffs']
    assert async_generation['counts']['generated'] == 1
    finished = [e for e in resumed['exchanges'] if e['path'].endswith('/inflight-flow/finish') and e['status'] == 200]
    if block:
        assert not resumed['settlement_complete']
        assert resumed['transport_blocks'] > 0
        assert resumed['runtime']['inflight_flow_id'] == original_flow
        assert evidence['backend_flow']['flow_id'] == original_flow
        assert resumed['sequence_marker']['read_after_interruption']
        assert not resumed['new_work_allowed'] and not finished
        assert resumed['pending_ack'] == (block == 'ack')
        # Restore only transport/response visibility and restart the same
        # SQLite. No test-side ack, finish, local flag repair or Start command.
        env['CANCEL_BLOCK'] = ''
        recovered = run('resume', 'connection-restored')
        with SessionLocal() as db:
            active = db.get(Worker, worker['id'])
            recovery = {'worker': recovered, 'backend_flow': active.inflight_flow_state,
                        'backend_run_status': active.run_status}
        (tmp_path / 'recovery-evidence.json').write_text(json.dumps(recovery, ensure_ascii=False, indent=2, default=str))
        assert recovered['sent'] == [] and recovered['wechat_reads'] == []
        assert not recovered['runtime']['inflight_flow_id'] and not recovery['backend_flow']
        assert not recovered['pending_ack'] and not recovered['pending_c2_outbox']
        assert recovered['run_status'] == recovery['backend_run_status'] == 'running'
        assert recovered['new_work_allowed'] and not recovered['sequence_marker']
        assert recovered['settlement_complete']
    else:
        assert resumed['settlement_complete'], resumed['admission_samples'][-3:]
        assert not resumed['pending_ack'] and not resumed['runtime']['inflight_flow_id']
        assert not evidence['backend_flow'] and not resumed['sequence_marker']
        assert evidence['acks'] == [{'result': 'failed', 'phase': 'not_attempted'}]
        assert resumed['run_status'] == evidence['backend_run_status'] == ('paused' if paused else 'running')
        assert resumed['new_work_allowed'] is (not paused)
        assert finished


@pytest.mark.parametrize('paused', [False, True])
def test_waits_for_remaining_production_cleanup_after_sqlite_flow_cleared(
    http_api, isolated_db, monkeypatch, async_generation, admin, tmp_path, paused,
):
    # Hold only the scheduling window after the real SQLite clear returns.
    # Release when observed, not after an assumed fixed cleanup duration.
    observer = '''
import threading
import chejin_worker_client.task_runner as observed_runner_module
cleanup_window=threading.Event()
release_cleanup=threading.Event()
native_finish_runtime=observed_runner_module.finish_runtime_flow
def observed_finish_runtime(*args,**kwargs):
 value=native_finish_runtime(*args,**kwargs)
 if mode=='resume':
  cleanup_window.set()
  assert release_cleanup.wait(5), 'test observer did not see the actual cleanup window'
 return value
observed_runner_module.finish_runtime_flow=observed_finish_runtime
def observe_settlement_sample(sample):
 if cleanup_window.is_set() and not sample['flow'] and sample['pending_finish']:
  assert not sample['allowed'], 'must not admit work during unfinished cleanup'
  release_cleanup.set()
'''
    monkeypatch.setattr(scenario, 'WORKER', scenario.WORKER.replace('exchanges=[]', observer + '\nexchanges=[]'))
    test_invalid_customer_settles_without_ui_or_unpausing(
        http_api, isolated_db, monkeypatch, async_generation, admin, tmp_path, 'typing_request_loss', paused, '',
    )
    result = json.loads((tmp_path / 'cancellation-evidence.json').read_text())['resumed']
    samples = result['admission_samples']
    assert any(not s['flow'] and s['pending_finish'] and not s['allowed'] for s in samples)
    assert not samples[-1]['flow'] and not samples[-1]['pending_finish']
    assert result['new_work_allowed'] is (not paused)


@pytest.mark.parametrize('loss', ['typing_request_loss', 'typing_response_loss'])
def test_old_continuation_predicate_reproduces_cancelled_flow_block(
    http_api, isolated_db, monkeypatch, async_generation, admin, tmp_path, loss,
):
    old_predicate = '''
from chejin_worker_client import reply_sequence_runtime as sequence_runtime
def old_needs_continuation(status,flow_id):
 return bool(status and (not (status.get('reply_sequence') or {}).get('terminal')
  or sequence_runtime.load_c2_state('reply_sequence_flow:'+flow_id).get('read_after_interruption')))
sequence_runtime.sequence_needs_continuation=old_needs_continuation
'''
    monkeypatch.setattr(scenario, 'WORKER', scenario.WORKER.replace('exchanges=[]', old_predicate + '\nexchanges=[]'))
    with pytest.raises(AssertionError):
        test_invalid_customer_settles_without_ui_or_unpausing(
            http_api, isolated_db, monkeypatch, async_generation, admin, tmp_path, loss, False, '',
        )
    evidence = json.loads((tmp_path / 'cancellation-evidence.json').read_text())
    resumed = evidence['resumed']
    assert resumed['sent'] == [] and not resumed['pending_ack'] and not resumed['pending_c2_outbox']
    assert evidence['backend_flow']['flow_id'] == evidence['original_flow_id'] == resumed['runtime']['inflight_flow_id']
    assert resumed['sequence_marker']['read_after_interruption'] and not resumed['new_work_allowed']
    assert evidence['acks'] == [{'result': 'failed', 'phase': 'not_attempted'}]


@pytest.mark.parametrize('change', ['other_batch', 'other_group', 'other_conversation', 'other_authorization', 'other_reason', 'allowed', 'not_terminal'])
def test_cancellation_cannot_release_unrelated_or_unfinished_sequence(monkeypatch, change):
    from chejin_worker_client import reply_sequence_runtime as runtime
    marker = {'batch_id': 'batch', 'conversation_id': 'conversation', 'read_after_interruption': True}
    monkeypatch.setattr(runtime, 'load_c2_state', lambda key: marker.copy())
    status = {'batch_id': 'batch', 'conversation_id': 'conversation',
              'reply_sequence': {'batch_id': 'batch', 'terminal': True},
              'authorization': {'conversation_id': 'conversation', 'allowed': False, 'error_code': 'LEAD_INVALID', 'recovery_decision': 'cancel'}}
    assert not runtime.sequence_needs_continuation(status, 'flow')
    if change == 'other_batch': status['batch_id'] = 'other'
    elif change == 'other_group': status['reply_sequence']['batch_id'] = 'other'
    elif change == 'other_conversation': status['conversation_id'] = 'other'
    elif change == 'other_authorization': status['authorization']['conversation_id'] = 'other'
    elif change == 'other_reason': status['authorization']['error_code'] = 'SERVICE_UNAVAILABLE'
    elif change == 'allowed': status['authorization']['allowed'] = True
    elif change == 'not_terminal': status['reply_sequence']['terminal'] = False
    assert runtime.sequence_needs_continuation(status, 'flow')
