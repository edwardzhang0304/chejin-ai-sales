"""Prepare original-pixel evidence and join the existing receipt/Flow recovery."""
import hashlib
import json
from pathlib import Path

from . import storage, text_correction_outbox
from .config import CONFIG
from .shared_rules import historical_text_correction


def _original_local_fact(entry, conversation_id):
    with storage.db_connection() as conn:
        rows = conn.execute("""SELECT payload_json FROM c2_ingest_outbox
            WHERE operation='ingest' AND conversation_id=? AND read_run_id=? AND status='confirmed'""",
            (conversation_id, entry['origin_read_run_id'])).fetchall()
    matches = []
    for row in rows:
        payload = json.loads(row['payload_json'])
        for message in payload.get('messages', []):
            if message.get('source_message_key') == entry['source_message_key']:
                matches.append((payload, message))
    if len(matches) != 1:
        raise ValueError('OCR_CORRECTION_ORIGINAL_LOCAL_FACT_MISSING')
    payload, message = matches[0]
    if hashlib.sha256(message.get('content', '').encode()).hexdigest() != entry['original_text_sha256']:
        raise ValueError('OCR_CORRECTION_ORIGINAL_LOCAL_FACT_CHANGED')
    return {**entry, 'effective_version': (entry.get('effective_text') or {}).get('version', 0),
            'raw_payload': message['raw_payload'], 'evidence': payload['evidence']}


def prepare(runner, *, binding, target, read_run_id, payload, defer_task_settlement):
    """Called only after the ordinary/D1/one-recheck path failed, before input."""
    if (payload.get('ok') is not True or payload.get('history_gap') or payload.get('top_message_fragment')
            or not target.raw.get('binding_id') or not hasattr(runner.bridge, 'recheck_original_message')):
        return False
    checkpoint = target.raw.get('identity_checkpoint') or {}
    selected = historical_text_correction.original_omission_candidate(checkpoint, payload.get('observations') or [])
    if not selected:
        return False
    entry, observation_id = selected
    try:
        original = _original_local_fact(entry, target.conversation_id)
        path = Path(original['evidence']['screenshot'])
        if not path.resolve().is_relative_to((CONFIG.app_dir / 'artifacts').resolve()):
            raise ValueError('OCR_CORRECTION_ORIGINAL_PATH_OUTSIDE_ARTIFACTS')
        image = path.read_bytes()
        request = runner.bridge.recheck_original_message(original=original,
            authorization={'conversation_id': target.conversation_id, 'binding_id': target.raw['binding_id'],
                           'authorization_revision': target.authorization_revision}, image_path=str(path),
            cancel_check=lambda: runner.stop_event.is_set())
        text_correction_outbox._validate_request(request, image)
        candidate = {'request': request, 'image': image, 'read_run_id': read_run_id,
            'owner': text_correction_outbox._owner(binding), 'target': target,
            'read_observation': {'call_status': 'succeeded', 'frame_id': str(payload.get('frame_id')
                or (payload.get('frame_observation') or {}).get('frame_id') or payload.get('sidecar_run_id') or ''),
                'observation_id': observation_id, 'identity_error_code': 'MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS'}}
        if defer_task_settlement:
            runner._historical_correction_candidate = candidate
        else:
            text_correction_outbox.enqueue(request, image, binding)
            runner.set_run_status('faulted')
        payload['historical_text_correction_candidate'] = {'message_event_id': request['message_event_id'],
            'proof_sha256': request['proof_sha256'], 'read_run_id': read_run_id}
        return True
    except (OSError, KeyError, TypeError, ValueError) as exc:
        storage.append_log('WARN', 'historical_text_correction_unavailable', '原图纠错证据不足，保留原消息并按原故障流程收尾。',
            metadata={'conversation_id': target.conversation_id, 'error_type': type(exc).__name__,
                      'reason': str(exc) if isinstance(exc, ValueError) else 'original_evidence_unavailable'})
        return False


def settle_task(runner, binding, *, task_id):
    """None means no proposal; false keeps the original task safely blocked."""
    candidate = getattr(runner, '_historical_correction_candidate', None)
    if not candidate:
        return None
    from apps.wechat_ai_customer_service.adapters.historical_correction_pending import PROPOSAL_FIELDS, validate_proof
    try:
        runtime = storage.load_runtime_control()
        if (candidate['owner'] != text_correction_outbox._owner(binding)
                or candidate['read_run_id'] != runtime.get('inflight_flow_id')):
            raise ValueError('OCR_CORRECTION_FLOW_CHANGED')
        target = candidate['target']
        bound = (target.raw.get('pre_send_fact_checkpoint_context') or {}).get('binding') or {}
        sequence = storage.load_c2_state('reply_sequence_flow:' + candidate['read_run_id']) or {}
        batch_id = bound.get('batch_id') or (sequence.get('batch_id')
            if sequence.get('conversation_id') == target.conversation_id else None)
        if not batch_id:
            raise ValueError('OCR_CORRECTION_ORIGINAL_BATCH_MISSING')
        status = runner.api.get_wechat_message_batch(binding, batch_id)
        action, task = status['reply_action'], status['task']
        if (task['id'] != task_id or (bound.get('reply_action_id') and action['id'] != bound['reply_action_id'])
                or action['status'] != 'queued' or action.get('send_token') or action.get('sending_claimed_at')
                or storage.load_reply_send_ack_outbox(action['id'])):
            raise ValueError('OCR_CORRECTION_ACTION_NOT_PROVEN_UNSENT')
        request = candidate['request']
        proof = validate_proof({'version': 1, 'reply_action_id': action['id'], 'task_id': task_id,
            'conversation_id': target.conversation_id, 'flow_id': candidate['read_run_id'],
            'authorization_revision': target.authorization_revision, 'reply_text_hash': action['reply_text_hash'],
            'read_observation': candidate['read_observation'],
            'proposal': {key: request[key] for key in PROPOSAL_FIELDS},
            'terminal_phase_proof': {'ok': True, 'action_phase': 'not_attempted', 'source': 'read_only_before_claim'},
            'input_progress': 'not_started', 'physical_send_triggered': False})
        intent = {'proof': proof, 'lease_fencing_token': runner.api._task_lease_token(task_id)}
        identity = text_correction_outbox.enqueue(request, candidate['image'], binding, settlement_intent=intent)
        runner.set_run_status('faulted')
        item = storage.load_c2_outbox_entry(identity)
        text_correction_outbox.settle_original(runner.api, binding, item)
        runner._historical_correction_candidate = None
        return True
    except Exception as exc:
        runner.set_run_status('faulted')
        storage.append_log('ERROR', 'historical_text_correction_settlement_pending', '历史文字纠错尚未结算，继续停止接单。',
            task_id=task_id, metadata={'error_type': type(exc).__name__}, force_incident=True)
        return False
