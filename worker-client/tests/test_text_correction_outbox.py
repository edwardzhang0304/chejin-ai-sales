"""Queue/receipt/restart tests. Synthetic bytes test durability, not visual OCR."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_c2_identity_gate_receipts import harness
from test_gate_only_outbox import setup_gate, ledger_rows
from chejin_worker_client import storage, text_correction_outbox as correction
from chejin_worker_client.api import ApiError, WorkerApiClient
from chejin_worker_client.models import Binding
from chejin_worker_client.shared_rules import historical_text_correction


def proposal():
    image = b"synthetic queue bytes, not an OCR fixture"
    request = {"operation": correction.OPERATION, "version": 1, "conversation_id": "conv-gate",
        "binding_id": "binding-test", "authorization_revision": "revision-conv-gate",
        "message_event_id": "original-event", "source_message_key": "old-fact",
        "original_read_run_id": "same-flow", "original_observation_id": "old-obs",
        "original_text_sha256": hashlib.sha256('手车'.encode()).hexdigest(),
        "expected_effective_version": 0, "corrected_text": "二手车",
        "proof": {"image_sha256": hashlib.sha256(image).hexdigest(), "digest_recorded_at": "2026-09-18T03:00:00Z"}}
    request['proof_sha256'] = historical_text_correction.correction_digest(request)
    return request, image


def accepted(request):
    return {"outcome": "accepted", "correction_id": "correction-test",
        "message_event_id": request['message_event_id'], "effective_version": 1,
        "effective_text": request['corrected_text'],
        "effective_text_sha256": hashlib.sha256(request['corrected_text'].encode()).hexdigest()}


@pytest.mark.parametrize('damage', [None, 'worker_id', 'client_instance_id', 'conversation_id',
    'binding_id', 'authorization_revision', 'message_event_id', 'proof_sha256',
    'expected_effective_version', 'original_work_settled', 'version', 'extra'])
def test_business_closure_must_match_original_owner_and_proposal(harness, damage):
    runner, api, bridge, binding, _ = setup_gate(harness)
    request, image = proposal()
    outbox = correction.enqueue(request, image, binding)
    resolution = historical_text_correction.closed_business_resolution(request,
        worker_id=binding.worker_id, client_instance_id=binding.client_instance_id)
    if damage in {'version', 'expected_effective_version'}:
        resolution[damage] = True  # bool must not pass integer equality
    elif damage == 'original_work_settled':
        resolution[damage] = False
    elif damage:
        resolution[damage] = 'foreign-or-extra'
    def rejected(*args):
        raise ApiError('HISTORICAL_TEXT_CORRECTION_REJECTED', 'no private content', 422,
                       {'resolution': resolution})
    api.post_wechat_message_text_correction = rejected
    assert correction.replay_one(api, binding, storage.load_c2_outbox_entry(outbox)) is (damage is None)
    saved = storage.load_c2_outbox_entry(outbox)
    if damage is None:
        assert saved['status'] == 'correction_rejected'
        assert not correction.recovery_block_reason(binding)
        assert storage.load_c2_state(correction.RESULT_PREFIX + outbox)['outcome'] == 'rejected'
        with storage.db_connection() as conn:
            conn.execute('UPDATE c2_runtime_state SET value=? WHERE key=?',
                (json.dumps({'outcome': 'rejected', 'resolution': resolution}), correction.RESULT_PREFIX + outbox))
            conn.commit()
        assert correction.recovery_block_reason(binding)  # incomplete saved receipt blocks
    else:
        assert saved['status'] == 'retry_waiting'
        assert correction.recovery_block_reason(binding)
    assert binding.run_status == 'faulted' and not bridge.message_reads and not bridge.sent_replies


def test_resolution_contract_extension_does_not_relax_original_recovery_rules():
    from chejin_worker_client.c2_contract import c2_contract_v3
    from chejin_worker_client.shared_rules import contract_rules
    current = c2_contract_v3()
    previous = {k: v for k, v in current.items() if k not in {'historical_text_correction_recheck_contract', 'historical_text_correction_resolution_contract'}}
    digest = contract_rules.contract_sha256(previous)
    assert contract_rules.read_recovery_contract(current, previous['contract_revision'], digest) == previous
    changed = copy.deepcopy(current)
    changed['historical_text_correction_resolution_contract']['resume'] = 'auto_resume'
    assert contract_rules.read_recovery_contract(changed, previous['contract_revision'], digest) is None


def settlement_intent(request):
    from apps.wechat_ai_customer_service.adapters.historical_correction_pending import PROPOSAL_FIELDS
    return {'lease_fencing_token': 1, 'proof': {'version': 1, 'reply_action_id': 'original-action',
        'task_id': 'original-task', 'conversation_id': request['conversation_id'], 'flow_id': 'current-flow',
        'authorization_revision': request['authorization_revision'], 'reply_text_hash': 'a'*64,
        'read_observation': {'call_status': 'succeeded', 'frame_id': 'current-frame',
            'observation_id': 'current-observation', 'identity_error_code': 'MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS'},
        'proposal': {key: request[key] for key in PROPOSAL_FIELDS},
        'terminal_phase_proof': {'ok': True, 'action_phase': 'not_attempted', 'source': 'read_only_before_claim'},
        'input_progress': 'not_started', 'physical_send_triggered': False}}


def test_correction_upgrade_snapshot_detects_blob_or_operation_change(harness):
    from chejin_worker_client.update_data_snapshot import protected_update_snapshot, assert_protected_update_snapshot
    _, _, _, binding, _ = setup_gate(harness)
    request, image = proposal(); outbox = correction.enqueue(request, image, binding)
    baseline = protected_update_snapshot(data_dir=storage.APP_DIR, digest_key='test-only')
    assert baseline['correction_columns_v1']
    assert_protected_update_snapshot(baseline, data_dir=storage.APP_DIR, digest_key='test-only')
    for field, changed, original in [('correction_image', b'damaged', image), ('operation', 'ingest', correction.OPERATION)]:
        with storage.db_connection() as conn:
            conn.execute(f'UPDATE c2_ingest_outbox SET {field}=? WHERE outbox_id=?', (changed, outbox)); conn.commit()
        with pytest.raises(RuntimeError, match='UPDATE_PROTECTED_DATABASE_CHANGED'):
            assert_protected_update_snapshot(baseline, data_dir=storage.APP_DIR, digest_key='test-only')
        with storage.db_connection() as conn:
            conn.execute(f'UPDATE c2_ingest_outbox SET {field}=? WHERE outbox_id=?', (original, outbox)); conn.commit()


def test_correction_ui_reason_tracks_durable_pending_and_rejected(harness, monkeypatch):
    from test_web_ui_binding_behavior import _headless_web_ui_module, WebUiBindingBehaviorTest
    runner, api, bridge, binding, _ = setup_gate(harness)
    runner._backend_confirmed_run_status = 'faulted'
    runner._restart_backend_probe_pending = False
    runner._restart_recovery_flow_id = ''
    api.task_lease_fencing_tokens = {}
    bridge.sidecar_active = lambda: False
    request, image = proposal()
    outbox = correction.enqueue(request, image, binding)
    states = []
    for status in ('pending', 'rejected'):
        if status == 'rejected':
            correction.settle(outbox, {'outcome': 'rejected', 'reason': 'original_proof_invalid'})
        recovery = runner._check_fault_recovery()
        assert not recovery['ready']
        assert ('正在核对' if status == 'pending' else '未通过') in recovery['reason']
        with _headless_web_ui_module() as module:
            monkeypatch.setattr(module, '_log_rows', lambda: [])
            monkeypatch.setattr(module, 'latest_incident', lambda: None)
            monkeypatch.setattr(module, 'lock_summary', lambda: {})
            window = WebUiBindingBehaviorTest._window(module, binding)
            window.runner.fault_recovery_state = lambda: {**recovery, 'statusText': '已停止接单'}
            state = json.loads(window.bridge.initialState())
            assert state['screen'] == 'client-faulted'
            assert state['model']['faultRecovery']['reason'] == recovery['reason']
            states.append(state)
    output = os.environ.get('CHEJIN_CORRECTION_UI_STATES')
    if output:
        Path(output).write_text(json.dumps(states, ensure_ascii=False))


def test_old_settlement_and_flow_finish_precede_correction(harness, monkeypatch):
    _, api, _, binding, _ = setup_gate(harness)
    request, image = proposal(); intent = settlement_intent(request)
    outbox = correction.enqueue(request, image, binding, settlement_intent=intent)
    item = storage.load_c2_outbox_entry(outbox)
    calls = []
    runtime = {'inflight_flow_id': 'current-flow'}
    monkeypatch.setattr(storage, 'load_runtime_control', lambda: runtime)
    def lost(*args):
        calls.append('old-receipt'); raise ConnectionError('response lost')
    api.settle_historical_text_correction_pending = lost
    api.post_wechat_message_text_correction = lambda b,p: calls.append('correction') or accepted(p)
    assert not correction.replay_one(api, binding, item)
    assert calls == ['old-receipt']
    api.settle_historical_text_correction_pending = lambda *a: calls.append('old-receipt')
    def finish(proof):
        assert proof == intent['proof']; calls.append('finish'); runtime['inflight_flow_id'] = None
    assert correction.replay_one(api, binding, item, finish_flow=finish)
    assert calls == ['old-receipt', 'old-receipt', 'finish', 'correction']
    assert storage.load_c2_state('inflight_finish_receipt:current-flow')['terminal_kind'] == 'technical_failed'


@pytest.mark.parametrize('kind', ['read_confirmed', 'technical_failed'])
def test_correction_retains_successful_read_but_cannot_replace_another_failure(harness, kind):
    _, _, _, binding, _ = setup_gate(harness)
    request, image = proposal(); intent = settlement_intent(request)
    prior = {'terminal_kind':kind,'conversation_id':request['conversation_id'],
             'read_completion':{'result':'new_facts','completed_at':'2026-09-18T01:00:00Z'}}
    key='inflight_finish_receipt:current-flow'; storage.save_c2_state(key,prior)
    if kind=='technical_failed':
        with pytest.raises(ValueError,match='OCR_CORRECTION_FLOW_RECEIPT_CONFLICT'):
            correction.enqueue(request,image,binding,settlement_intent=intent)
        assert storage.load_c2_state(key)==prior
        assert not storage.has_pending_c2_outbox()
    else:
        correction.enqueue(request,image,binding,settlement_intent=intent)
        saved=storage.load_c2_state(key)
        assert saved['read_completion']==prior['read_completion']
        assert saved['terminal_kind']=='technical_failed' and saved['error_code']=='HISTORICAL_TEXT_CORRECTION_PENDING'


def test_correction_does_not_own_or_settle_original_flow(harness):
    runner, api, bridge, binding, gate = setup_gate(harness)
    request, image = proposal()
    before = ledger_rows()
    flow_control = storage.load_runtime_control()
    original = storage.enqueue_c2_outbox(gate)
    outbox = correction.enqueue(request, image, binding)
    calls = []
    api.post_wechat_message_text_correction = lambda b, p: calls.append(p) or accepted(p)
    assert [r['outbox_id'] for r in storage.list_c2_outbox_waiting(read_run_id='same-flow')] == [original]
    assert runner._replay_c2_outbox(binding)
    assert len(api.message_payloads) == len(calls) == 1
    assert storage.load_c2_outbox_entry(outbox)['status'] == 'confirmed'
    assert storage.load_c2_state('ocr_correction_result:' + outbox)['outcome'] == 'accepted'
    assert not storage.load_c2_state('read_settlement:' + outbox)
    assert ledger_rows() == before and storage.load_runtime_control() == flow_control
    assert binding.run_status == 'faulted' and not bridge.message_reads and not bridge.sent_replies
    with storage.db_connection() as conn:
        row = conn.execute('SELECT payload_json,correction_image FROM c2_ingest_outbox WHERE outbox_id=?', (outbox,)).fetchone()
        assert 'image_base64' not in row['payload_json'] and row['correction_image'] == image


def test_correction_alone_is_not_a_read_artifact_and_ordinary_mutations_reject_it(harness):
    _, _, _, binding, _ = setup_gate(harness)
    request, image = proposal()
    request['original_read_run_id'] = 'ended-unowned-flow'
    request['proof_sha256'] = historical_text_correction.correction_digest(request)
    outbox = correction.enqueue(request, image, binding)
    assert storage.has_pending_c2_outbox()
    assert not storage.has_pending_c2_outbox_for_read_run_id('ended-unowned-flow')
    assert not storage.has_c2_outbox_for_read_run_id('ended-unowned-flow')
    assert storage.c2_flow_conversation_ids('ended-unowned-flow') == []
    assert storage.legacy_media_flow_snapshot('ended-unowned-flow')['outbox'] == []
    assert not storage.has_c2_outbox_for_source_keys('conv-gate', ['old-fact'])
    assert storage.load_c2_outbox_origin_read_run_ids('conv-gate') == {}
    with pytest.raises(ValueError):
        storage.transition_c2_outbox(outbox, status='confirmed')
    with pytest.raises(ValueError):
        storage.quarantine_legacy_malformed_c2_outbox(outbox, error='old-error')
    with pytest.raises(ValueError):
        storage.settle_c2_outbox(outbox, {}, binding, server_bound_at=binding.bound_at)
    assert storage.load_c2_outbox_entry(outbox)['status'] == 'waiting'


@pytest.mark.parametrize('status', [400, 401, 403, 404, 413, 422])
def test_permanent_rejection_gets_own_terminal_receipt_without_resuming(harness, status):
    runner, api, bridge, binding, _ = setup_gate(harness)
    request, image = proposal()
    outbox = correction.enqueue(request, image, binding)
    def reject(*args):
        raise ApiError('CORRECTION_REJECTED', 'private response must not persist', status)
    api.post_wechat_message_text_correction = reject
    assert runner._replay_c2_outbox(binding)
    assert storage.load_c2_outbox_entry(outbox)['status'] == 'correction_rejected'
    assert not storage.has_pending_c2_outbox()
    assert binding.run_status == 'faulted' and not bridge.message_reads and not bridge.sent_replies
    assert 'private response' not in json.dumps(storage.load_c2_state('ocr_correction_result:' + outbox))
    assert correction.recovery_block_reason(binding) == '原图复核未通过，旧消息已保留，需要核查故障记录。'


@pytest.mark.parametrize('status', [409, 429, 500, 503])
def test_transient_errors_back_off_the_frozen_proposal(harness, status):
    runner, api, _, binding, _ = setup_gate(harness)
    request, image = proposal()
    outbox = correction.enqueue(request, image, binding)
    def fail(*args):
        raise ApiError('CORRECTION_BUSY', 'transient', status)
    api.post_wechat_message_text_correction = fail
    assert not runner._replay_c2_outbox(binding)
    saved = storage.load_c2_outbox_entry(outbox)
    assert saved['payload']['request'] == request and saved['next_attempt_at']
    assert storage.has_pending_c2_outbox() and not storage.has_pending_c2_outbox_for_read_run_id('same-flow')


def test_restart_replays_persisted_proof_in_a_new_python_process(harness, tmp_path, monkeypatch):
    runner, api, _, binding, _ = setup_gate(harness)
    request, image = proposal()
    outbox = correction.enqueue(request, image, binding)
    def lost_response(*args):
        raise ConnectionError('response lost after a possible commit')
    api.post_wechat_message_text_correction = lost_response
    assert not runner._replay_c2_outbox(binding)
    assert storage.load_c2_outbox_entry(outbox)['status'] == 'retry_waiting'
    script = tmp_path/'replay.py'
    script.write_text('''import json,sys,hashlib
from pathlib import Path
from chejin_worker_client import storage,text_correction_outbox as c
from chejin_worker_client.models import Binding
storage.APP_DIR=Path(sys.argv[1]);storage.DB_FILE=storage.APP_DIR/'worker_client.sqlite3'
binding=Binding(**json.loads(Path(sys.argv[2]).read_text()))
item=storage.load_c2_outbox_entry(sys.argv[3])
class API:
 def post_wechat_message_text_correction(self,b,p):
  Path(sys.argv[4]).write_text(json.dumps(p,ensure_ascii=False))
  return {'outcome':'accepted','correction_id':'new-process-receipt','message_event_id':p['message_event_id'],'effective_version':1,'effective_text_sha256':hashlib.sha256(p['corrected_text'].encode()).hexdigest()}
assert c.replay_one(API(),binding,item)
''')
    from dataclasses import asdict
    binding_path = tmp_path/'binding.json'; binding_path.write_text(json.dumps(asdict(binding)))
    output = tmp_path/'sent.json'
    result = subprocess.run([sys.executable, str(script), str(storage.APP_DIR), str(binding_path), outbox, str(output)],
                            env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    sent = json.loads(output.read_text()); sent.pop('image_base64')
    assert sent == request
    assert storage.load_c2_outbox_entry(outbox)['status'] == 'confirmed'
    assert not storage.has_pending_c2_outbox()


@pytest.mark.parametrize('defect', ['event', 'text', 'version', 'missing_id'])
def test_invalid_acceptance_cannot_release_queue(harness, defect):
    runner, api, _, binding, _ = setup_gate(harness)
    request, image = proposal(); outbox = correction.enqueue(request, image, binding)
    receipt = accepted(request)
    if defect == 'event': receipt['message_event_id'] = 'other'
    if defect == 'text': receipt['effective_text_sha256'] = '0'*64
    if defect == 'version': receipt['effective_version'] = True
    if defect == 'missing_id': receipt.pop('correction_id')
    api.post_wechat_message_text_correction = lambda *args: receipt
    assert not runner._replay_c2_outbox(binding)
    assert storage.has_pending_c2_outbox()
    assert not storage.load_c2_state('ocr_correction_result:' + outbox)


def test_confirmed_label_alone_does_not_release_or_prune_correction(harness):
    _, _, _, binding, _ = setup_gate(harness)
    request, image = proposal(); outbox = correction.enqueue(request, image, binding)
    with storage.db_connection() as conn:
        conn.execute("UPDATE c2_ingest_outbox SET status='confirmed', updated_at='2000-01-01' WHERE outbox_id=?", (outbox,)); conn.commit()
    assert storage.has_pending_c2_outbox()
    storage.prune_terminal_outboxes()
    assert storage.load_c2_outbox_entry(outbox) is not None


def test_api_never_uses_historical_read_as_active_flow():
    api = WorkerApiClient(); calls = []
    class Response:
        status_code = 200
        def json(self): return {'code':'OK','data':{}}
    api.session.request = lambda *args, **kw: calls.append(kw) or Response()
    binding = Binding('worker','token','instance')
    api.post_wechat_message_text_correction(binding, {'original_read_run_id':'ended-old'})
    assert 'X-Inflight-Flow-Id' not in calls[0]['headers']
    api.inflight_flow_id = 'real-current'
    api.post_wechat_message_text_correction(binding, {'original_read_run_id':'ended-old'})
    assert calls[1]['headers']['X-Inflight-Flow-Id'] == 'real-current'


@pytest.mark.parametrize('outcome', ['network','old_server','invalid_resolution','valid_resolution','unexpected_accept'])
def test_rejected_correction_recheck_preserves_original_and_backs_off(harness,monkeypatch,outcome):
    runner,api,bridge,binding,_=setup_gate(harness)
    request,image=proposal()
    outbox=correction.enqueue(request,image,binding)
    correction.settle(outbox,{'outcome':'rejected','reason':'HISTORICAL_TEXT_CORRECTION_REJECTED'})
    original=storage.load_c2_state(correction.RESULT_PREFIX+outbox)
    initial=storage.load_c2_outbox_entry(outbox)
    proof=historical_text_correction.closed_business_resolution(request,
        worker_id=binding.worker_id,client_instance_id=binding.client_instance_id)
    if outcome=='invalid_resolution': proof['client_instance_id']='foreign'
    calls=[]
    def endpoint(b,p,*,resolution_only=False):
        calls.append((p,resolution_only))
        assert resolution_only
        if outcome=='network': raise ConnectionError('controlled disconnect')
        if outcome=='old_server': raise ApiError('NOT_FOUND','missing',404,{})
        if outcome=='unexpected_accept': return accepted(request)
        raise ApiError('HISTORICAL_TEXT_CORRECTION_REJECTED','rejected',422,{'resolution':proof})
    api.post_wechat_message_text_correction=endpoint
    correction.replay_one(api,binding,initial)
    saved=storage.load_c2_outbox_entry(outbox)
    assert saved['payload']==initial['payload'] and saved['status']=='correction_rejected'
    assert saved['last_error']==initial['last_error']
    assert saved['attempt_count']==initial['attempt_count']+1
    assert saved['next_attempt_at']
    assert storage.load_c2_state(correction.RESULT_PREFIX+outbox)==original
    assert not storage.list_c2_outbox_waiting()
    assert bool(correction.recovery_block_reason(binding))==(outcome!='valid_resolution')
    monkeypatch.setattr(storage,'utc_now_iso',lambda:saved['next_attempt_at'])
    due=storage.list_c2_outbox_waiting()
    assert bool(due)==(outcome!='valid_resolution')
    if due:
        correction.replay_one(api,binding,due[0])
        assert storage.load_c2_outbox_entry(outbox)['attempt_count']==saved['attempt_count']+1
    assert not bridge.message_reads and not bridge.sent_replies


def test_recheck_extension_accepts_only_frozen_predecessors():
    from chejin_worker_client.c2_contract import c2_contract_v3
    from chejin_worker_client.shared_rules import contract_rules
    current=c2_contract_v3()
    previous={k:v for k,v in current.items() if k!='historical_text_correction_recheck_contract'}
    digest=contract_rules.contract_sha256(previous)
    assert contract_rules.read_recovery_contract(current,previous['contract_revision'],digest)==previous
    changed=copy.deepcopy(current)
    changed['historical_text_correction_recheck_contract']['mode']='apply_correction'
    assert contract_rules.read_recovery_contract(changed,previous['contract_revision'],digest) is None
