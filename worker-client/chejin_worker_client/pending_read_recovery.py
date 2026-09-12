"""Read-only handoff of a stopped C2 read; never execute or settle business."""
import hashlib
import json
from pathlib import Path

from .update_data_snapshot import _read_transaction

PROTOCOL = 1
LEGACY_SHA256 = 'bcb1af09321339b159cc02581f5938e402f16094465933645c71bd7dc0eadcf1'
MAPPING_ERRORS = {'MESSAGE_OBSERVATION_MAPPING_INCOMPLETE',
                  'MESSAGE_OBSERVATION_MAPPING_INCOMPLETE:FACT_SETTLEMENT_REQUIRED'}


def package_recovery_capability() -> dict:
    from .c2_contract import c2_contract_v3, contract_sha256, _contract_candidates
    current = c2_contract_v3()
    path = next((p.parent / 'recovery/c2_contract_v3_0.9.75.json'
                 for p in _contract_candidates()
                 if (p.parent / 'recovery/c2_contract_v3_0.9.75.json').is_file()), None)
    if path is None:
        raise RuntimeError('RECOVERY_CONTRACT_MISSING')
    legacy = json.loads(path.read_text(encoding='utf-8'))
    canonical = json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    if hashlib.sha256(canonical).hexdigest() != LEGACY_SHA256:
        raise RuntimeError('RECOVERY_CONTRACT_CORRUPTED')
    if {k: v for k, v in legacy.items() if k != 'contract_revision'} != {
            k: v for k, v in current.items() if k != 'contract_revision'}:
        raise RuntimeError('RECOVERY_CONTRACT_SEMANTICS_CHANGED')
    return {'protocol_version': PROTOCOL, 'contracts': [
        {'revision': legacy['contract_revision'], 'sha256': LEGACY_SHA256},
        {'revision': current['contract_revision'], 'sha256': contract_sha256()},
    ]}


def accepts_handoff(capability: dict, handoff: dict) -> bool:
    return bool(isinstance(capability, dict)
                and capability.get('protocol_version') == PROTOCOL
                and handoff.get('protocol_version') == PROTOCOL
                and handoff.get('contracts')
                and all(c in capability.get('contracts', []) for c in handoff['contracts']))


def backend_accepts_handoff(capability: dict, handoff: dict) -> bool:
    """Require the backend's ready proof for this exact stopped read owner."""
    return bool(
        accepts_handoff(capability, handoff)
        and capability.get('ready')
        and all(capability.get(key) == handoff[key] for key in (
            'flow_id', 'conversation_id', 'worker_id', 'client_instance_id',
        ))
    )


def inspect_pending_read(data_dir: Path) -> dict:
    """One SQLite read transaction; called again after all old writers exit.

    Full payload bytes remain in Outbox. The handoff contains identities and
    digests only. An action/unknown send or an uncovered ledger row rejects it.
    """
    def require(condition):
        if not condition:
            raise RuntimeError('UPDATE_PENDING_READ_NOT_TRANSFERABLE')

    with _read_transaction(data_dir) as db:
        def state(key):
            row = db.execute('SELECT value FROM c2_runtime_state WHERE key=?', (key,)).fetchone()
            value = json.loads(row[0]) if row else {}
            require(isinstance(value, dict))
            return value
        binding = db.execute('SELECT * FROM binding WHERE id=1').fetchone()
        require(binding is not None and binding['run_status'] == 'faulted')
        row = db.execute('SELECT value FROM client_settings WHERE key=?', ('runtime_control_v1',)).fetchone()
        control = json.loads(row[0]) if row else {}
        require(isinstance(control, dict))
        flow_id = control.get('inflight_flow_id')
        require(flow_id and control.get('inflight_flow_kind') == 'c2_read')
        receipt = state('inflight_finish_receipt:' + flow_id)
        require(receipt.get('terminal_kind') == 'technical_failed'
                and receipt.get('error_code') in MAPPING_ERRORS
                and not receipt.get('read_completion'))
        conversation_id = receipt.get('conversation_id')
        require(conversation_id)
        require(db.execute('SELECT COUNT(*) FROM c2_action_journal').fetchone()[0] == 0)
        require(db.execute("SELECT COUNT(*) FROM reply_send_ack_outbox WHERE status IN ('intent','waiting','capability_paused')").fetchone()[0] == 0)
        rows = db.execute("SELECT * FROM c2_ingest_outbox WHERE status != 'confirmed'").fetchall()
        require(rows)
        contracts, digests, covered = [], {}, set()
        for row in rows:
            require(row['status'] in {'waiting', 'retry_waiting', 'capability_paused'})
            require(row['read_run_id'] == flow_id and row['conversation_id'] == conversation_id)
            payload = json.loads(row['payload_json'])
            require(payload.get('read_run_id') == flow_id and payload.get('conversation_id') == conversation_id)
            require(payload.get('authorization_scope') != 'fact_settlement')
            pair = {'revision': payload.get('contract_revision'), 'sha256': payload.get('contract_sha256')}
            require(pair['revision'] and pair['sha256'])
            if pair not in contracts:
                contracts.append(pair)
            require(payload.get('messages'))
            for message in payload['messages']:
                require(message.get('source_message_key') and message.get('item_state') in {'completed','failed'})
                covered.add(message['source_message_key'])
            digests[row['outbox_id']] = hashlib.sha256(row['payload_json'].encode()).hexdigest()
        for row in db.execute("SELECT * FROM c2_message_ledger WHERE ingest_state='waiting'"):
            require(row['conversation_id'] == conversation_id and row['origin_read_run_id'] == flow_id
                    and row['source_message_key'] in covered)
        # A filesystem journal denotes a possibly triggered physical action.
        # Missing transactions is normal; unreadable/symlinked content is not.
        transactions = data_dir / 'transactions'
        require(not transactions.is_symlink())
        if transactions.exists():
            require(not any(p.is_symlink() or p.is_file() for p in transactions.rglob('*')))
        return {'protocol_version': PROTOCOL, 'flow_id': flow_id,
                'conversation_id': conversation_id, 'worker_id': binding['worker_id'],
                'client_instance_id': binding['client_instance_id'],
                'contracts': contracts, 'outbox_sha256': digests}
