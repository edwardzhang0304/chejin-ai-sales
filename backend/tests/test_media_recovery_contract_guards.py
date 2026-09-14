"""Old media wire fixtures; real authorization and strict HTTP validation."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select, func
from app.core.database import SessionLocal
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.contracts.read_recovery import compatible_read_contract
from app.contracts.shared_rules import shared_adapter
from app.schemas.wechat import WechatMessageIngestRequest
from app.services.wechat_service import _validate_v3_request_contract
from test_contract_equivalent_recovery import prepared_read, use_contract
from test_lead_followup_eligibility import isolated_db, http_api, headers
from test_wechat_c2_api import _fact_settlement_payload, _v3_failed_voice_message


@pytest.mark.parametrize('damage', ['revision', 'sha', 'evidence_sha', 'message_sha', 'source_digest', 'rules'])
@pytest.mark.parametrize('already_settled', [False, True])
def test_frozen_media_rejects_changed_contract_or_source_identity(http_api, tmp_path, damage, already_settled):
    worker, row, original = prepared_read(closed=True, message_count=1, eligible=True)
    with SessionLocal() as db:
        bound = db.get(WechatSessionBinding, row['binding_id'])
        target = {'id': bound.id, 'conversation_id': bound.conversation_id,
                  'rpa_session_key': bound.rpa_session_key, 'unread_generation': bound.unread_generation}
        remark = bound.remark_code
    source = 'guard-original-voice'
    payload = _fact_settlement_payload(target, remark, transaction_id='guard-original-media',
        source_keys=[source], settlement_mode='fact_only', action_kind='voice',
        original_read_run_id=original['read_run_id'], messages=[_v3_failed_voice_message(
            source, role='customer', screen_order=1, reason='VOICE_TRANSCRIBE_PARTIAL')])
    frozen = json.loads((Path(__file__).resolve().parents[2]/'contracts/recovery/c2_contract_v3_0.9.80.json').read_text())
    digest = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    use_contract(payload, {'contract_revision': frozen['contract_revision'], 'contract_sha256': digest})
    assert compatible_read_contract(frozen['contract_revision'], digest) == frozen
    _validate_v3_request_contract(WechatMessageIngestRequest.model_validate(payload), contract=frozen)
    endpoint = '/api/workers/' + worker['id']
    params = {'recovery_transaction_id': 'guard-original-media', 'action_kind': 'voice',
        'source_message_key_digest': hashlib.sha256(source.encode()).hexdigest(),
        'original_authorization_revision': payload['authorization_revision'],
        'original_read_run_id': payload['read_run_id']}
    auth = http_api.get(endpoint + '/wechat/conversations/' + row['conversation_id'] + '/read-authorization',
                        headers=headers(worker), params=params)
    assert auth.status_code == 200, auth.text
    assert auth.json()['data']['allowed'] is False
    assert auth.json()['data']['authorization_scope'] == 'fact_settlement'
    ingest_headers = {**headers(worker), 'X-C2-Settlement-Token': auth.json()['data']['settlement_token']}
    if already_settled:
        accepted = http_api.post(endpoint + '/wechat/messages/ingest', headers=ingest_headers, json=payload)
        assert accepted.status_code == 200, accepted.text
    if damage == 'revision':
        use_contract(payload, {'contract_revision': '999.0.0', 'contract_sha256': digest})
    elif damage == 'sha':
        use_contract(payload, {'contract_revision': frozen['contract_revision'], 'contract_sha256': '0' * 64})
    elif damage == 'evidence_sha':
        payload['evidence']['contract_sha256'] = '0' * 64
    elif damage == 'message_sha':
        payload['messages'][0]['raw_payload']['contract_sha256'] = '0' * 64
    elif damage == 'source_digest':
        payload['messages'][0]['source_message_key'] = 'different-original-source'
    else:
        changed = copy.deepcopy(frozen)
        changed['message_identity_contract']['duplicate_identity_invariants'].remove('sender_role')
        changed_sha = hashlib.sha256(json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        assert shared_adapter('contract_rules').contract_rules_sha256(changed) != shared_adapter('contract_rules').contract_rules_sha256(frozen)
        assert compatible_read_contract(changed['contract_revision'], changed_sha) is None
        use_contract(payload, {'contract_revision': changed['contract_revision'], 'contract_sha256': changed_sha})
    response = http_api.post(endpoint + '/wechat/messages/ingest',
        headers=ingest_headers, json=payload)
    (tmp_path/'observed.json').write_text(json.dumps({'damage': damage, 'status': response.status_code,
        'response': response.json()}, ensure_ascii=False, indent=2))
    # A key conflicting with its own raw identity is rejected by the typed
    # request parser (400), before the contract/token service checks (409).
    assert response.status_code == (400 if damage == 'source_digest' else 409), response.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MessageEvent.id))) == int(already_settled)
        assert db.get(Worker, worker['id']).run_status == 'faulted'
