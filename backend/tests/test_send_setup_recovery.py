"""Protocol checks using real HTTP/PostgreSQL and actual launch-failure journals.

These are receipt validation tests; the separate subprocess suite owns the
full Worker stop/ACK/Flow/start/automatic-generation chain.
"""
from dataclasses import replace
from copy import deepcopy
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import ReplyAction,SentAck,HandoffEvent
from app.models.task import Task
from app.models.wechat import WechatSessionBinding
from app.models.worker import Worker
from app.contracts import c2
from app.contracts.shared_rules import shared_adapter
from test_lead_followup_eligibility import http_api,isolated_db
from test_pre_send_checkpoint_order import async_generation
from test_pre_send_read_recovery import setup_receipt
from chejin_worker_client import rpa_bridge,action_journal


def prepare(http_api,monkeypatch,tmp_path,mode='create_failed',capable=True,stopped=True):
    worker,ids,path,headers,body=setup_receipt(http_api,monkeypatch,permit=True,stopped=stopped)
    r=http_api.post(f"/api/workers/{worker['id']}/heartbeat",headers=headers,json={
        'client_instance_id':'client-c3','run_status':'faulted' if stopped else 'running',
        'running_status':'idle','rpa_component_status':'ready','wechat_status':'logged_in',
        'local_lock_summary':{'capabilities':{'pre_send_setup_recovery_version':1} if capable else {}}})
    assert r.status_code==200,r.text
    root=tmp_path/'worker'
    for module in (rpa_bridge,action_journal):monkeypatch.setattr(module,'CONFIG',replace(module.CONFIG,app_dir=root))
    journal=action_journal.action_journal_path('send',ids['reply_action_id'])
    action_journal.initialize_action_journal(journal,action_kind='send',transaction_id=ids['reply_action_id'],conversation_id=ids['conversation_id'],
        canonical_action_id=ids['reply_action_id'],reserved_worker_stable_id='synthetic-reserved',items=[{'journal_item_id':ids['reply_action_id']}],
        prepare_evidence={'pre_send_setup_context':ids})
    bridge=rpa_bridge.RpaBridge();bridge.mode='real'
    with SessionLocal() as db:text=db.get(ReplyAction,ids['reply_action_id']).reply_text
    if mode=='not_called':
        from apps.wechat_ai_customer_service.adapters import send_request_file
        monkeypatch.setattr(send_request_file,'write_package',lambda *a,**k:(_ for _ in ()).throw(OSError('controlled disk full')))
    elif mode=='create_failed':
        # Actual POSIX process creation failure, not a Windows claim.
        monkeypatch.setattr(bridge,'_sidecar_command',lambda args:[str(tmp_path/'missing-executable'),*args])
    else:
        native=rpa_bridge.subprocess.Popen
        def corrupt(command,**kw):
            f=Path(command[command.index('--expected-context-guard-file')+1]);f.write_bytes(b'{broken')
            return native(command,**kw)
        monkeypatch.setattr(rpa_bridge.subprocess,'Popen',corrupt)
    result=bridge.send_reply(target='CJTEST01',rpa_session_key='',text=text,task_id=ids['task_id'],reply_action_id=ids['reply_action_id'],expected_context_guard={'complete_history':[]})
    assert result['pre_send_setup_failure']['process_state']==mode,result
    body.update(error_code=result['error_code'],evidence={'pre_send_setup_failure':result['pre_send_setup_failure']})
    return worker,ids,path,headers,body,result


@pytest.mark.parametrize('mode',['not_called','create_failed','rejected_before_ui'])
def test_setup_failures_settle_original_action_without_handoff(http_api,monkeypatch,tmp_path,async_generation,mode):
    w,ids,path,headers,body,result=prepare(http_api,monkeypatch,tmp_path,mode)
    one=http_api.post(path,headers=headers,json=body);assert one.status_code==200,one.text
    two=http_api.post(path,headers=headers,json=body);assert two.status_code==200 and two.json()['data']['duplicated'],two.text
    with SessionLocal() as db:
        assert db.get(Task,ids['task_id']).status=='failed'
        assert db.get(ReplyAction,ids['reply_action_id']).status=='failed'
        assert db.scalar(select(SentAck)).send_result=='failed'
        assert db.query(SentAck).count()==1 and db.query(HandoffEvent).count()==0
        b=db.scalar(select(WechatSessionBinding))
        assert b.last_scan_snapshot['pre_send_read_pending']['status']=='pending'
    (tmp_path/'receipt.json').write_text(json.dumps({'input':body,'result':one.json(),'repeat':two.json()},ensure_ascii=False,indent=2))


@pytest.mark.parametrize('bad',['capability','running','physical','terminal_action','process_state','request_hash','outer_error','mutex'])
def test_invalid_setup_proofs_never_fall_back_to_ordinary_handoff(http_api,monkeypatch,tmp_path,async_generation,bad):
    w,ids,path,headers,body,result=prepare(http_api,monkeypatch,tmp_path,capable=bad!='capability',stopped=bad!='running')
    proof=body['evidence']['pre_send_setup_failure']
    if bad=='physical':proof['physical_send_triggered']=True
    elif bad=='terminal_action':proof['terminal_phase_proof']['canonical_action_id']='another-action'
    elif bad=='process_state':proof['process_state']='not_called'
    elif bad=='request_hash':proof['request_sha256']=None
    elif bad=='outer_error':body['error_code']='READ_SOMETHING_FAILED'
    elif bad=='mutex':body['evidence']['pre_send_read_failure']={}
    response=http_api.post(path,headers=headers,json=body)
    assert response.status_code==409,response.text
    with SessionLocal() as db:
        assert db.get(ReplyAction,ids['reply_action_id']).status=='sending'
        assert db.query(SentAck).count()==db.query(HandoffEvent).count()==0


@pytest.mark.parametrize('upgrade_at', ['before_receipt', 'after_response_lost'])
def test_equivalent_upgrade_settles_frozen_receipt_once(http_api, monkeypatch, tmp_path, async_generation, upgrade_at):
    worker, ids, path, headers, body, result = prepare(http_api, monkeypatch, tmp_path)
    frozen_body = deepcopy(body)
    current = deepcopy(c2.c2_contract_v3())
    upgraded = {**current, 'contract_revision': '0.9.91'}  # Isolated release-label test only.
    common = shared_adapter('contract_rules')
    assert common.equivalent_contract(upgraded, current['contract_revision'], common.contract_sha256(current)) == current
    if upgrade_at == 'after_response_lost':
        # Server commits, caller discards the first response and retries exact bytes.
        first = http_api.post(path, headers=headers, json=body)
        assert first.status_code == 200, first.text
    monkeypatch.setattr(c2, 'c2_contract_v3', lambda: upgraded)
    response = http_api.post(path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    finish = http_api.post(f"/api/workers/{worker['id']}/inflight-flow/finish", headers=headers,
        json={'flow_id': ids['flow_id'], 'terminal_kind': 'task_terminal', 'conversation_id': ids['conversation_id']})
    assert finish.status_code == 200, finish.text
    repeated = http_api.post(path, headers=headers, json=body)
    assert repeated.status_code == 200 and repeated.json()['data']['duplicated'], repeated.text
    assert body == frozen_body
    with SessionLocal() as db:
        assert db.get(ReplyAction, ids['reply_action_id']).status == 'failed'
        assert db.get(Task, ids['task_id']).status == 'failed'
        assert db.query(SentAck).count() == 1 and db.query(HandoffEvent).count() == 0
        assert not db.get(Worker, worker['id']).inflight_flow_state
        assert db.get(Worker, worker['id']).run_status == 'faulted'
        assert db.scalar(select(WechatSessionBinding)).last_scan_snapshot['pre_send_read_pending']['status'] == 'pending'
    (tmp_path/'equivalent-upgrade.json').write_text(json.dumps({'original': current, 'upgraded': upgraded,
        'frozen_body': frozen_body, 'response': response.json(), 'repeat': repeated.json()}, ensure_ascii=False, indent=2))


@pytest.mark.parametrize('bad', ['no_sp', 'changed_rule', 'forged_revision', 'forged_sha', 'sent', 'unknown', 'identity'])
def test_setup_upgrade_rejects_incompatible_or_terminal_original(http_api, monkeypatch, tmp_path, async_generation, bad):
    worker, ids, path, headers, body, result = prepare(http_api, monkeypatch, tmp_path)
    current = deepcopy(c2.c2_contract_v3())
    original = deepcopy(current)
    common = shared_adapter('contract_rules')
    if bad == 'no_sp':
        original = json.loads((Path(__file__).resolve().parents[2]/'contracts/recovery/c2_contract_v3_0.9.90.json').read_text())
    elif bad == 'changed_rule':
        original['pre_send_setup_recovery_contract']['path_utf16_budget'] -= 1
    with SessionLocal() as db:
        w = db.get(Worker, worker['id'])
        state = dict(w.inflight_flow_state)
        state.update(contract_revision=original['contract_revision'], contract_sha256=common.contract_sha256(original))
        if bad == 'forged_revision': state['contract_revision'] = '0.9.89'
        if bad == 'forged_sha': state['contract_sha256'] = '0'*64
        w.inflight_flow_state = state
        if bad in ('sent', 'unknown'):
            db.get(ReplyAction, ids['reply_action_id']).status = 'sent' if bad == 'sent' else 'unknown_send_result'
        db.commit()
    if bad == 'identity': body['evidence']['pre_send_setup_failure']['conversation_id'] = 'another-conversation'
    monkeypatch.setattr(c2, 'c2_contract_v3', lambda: {**current, 'contract_revision': '0.9.91'})
    response = http_api.post(path, headers=headers, json=body)
    assert response.status_code == 409, response.text
    with SessionLocal() as db:
        assert db.query(SentAck).count() == db.query(HandoffEvent).count() == 0
        assert db.get(Task, ids['task_id']).status == 'running'
