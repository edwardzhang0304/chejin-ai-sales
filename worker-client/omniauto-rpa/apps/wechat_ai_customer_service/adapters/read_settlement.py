"""Pure identity checks for settlement of an immutable C2 Outbox partition."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any


PROTOCOL_VERSION = 1
TERMINAL_ACTIONS = frozenset({'conversation_terminated', 'target_terminated'})


def is_failure_report(payload: dict) -> bool:
    """A failure report contains no facts or physical-action result to settle."""
    evidence = payload.get('evidence') or {}
    errors = evidence.get('flow_gate_errors')
    return (payload.get('messages') == []
        and evidence.get('observations') == [] and evidence.get('slot_ledger_states') == []
        and isinstance(errors, list) and bool(errors)
        and all(isinstance(code, str) and bool(code.strip()) for code in errors)
        and not evidence.get('failed_voice_source_keys')
        and not evidence.get('ingest_partition')
        and payload.get('authorization_scope') != 'fact_settlement')


def payload_sha256(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def partition_identity(payload: dict[str, Any]) -> dict[str, Any]:
    messages = payload.get('messages') or []
    keys = [item.get('source_message_key') for item in messages]
    partition = (payload.get('evidence') or {}).get('ingest_partition') or {}
    expected = partition.get('expected_source_message_keys') or keys
    index, count = partition.get('index', 1), partition.get('count', 1)
    if (not keys or not isinstance(expected, list)
            or any(not isinstance(key, str) or not key for key in [*keys, *expected])
            or len(keys) != len(set(keys)) or len(expected) != len(set(expected))
            or not set(keys).issubset(expected)
            or type(index) is not int or type(count) is not int or not 1 <= index <= count
            or (partition and partition.get('group_id') != payload.get('read_run_id'))):
        raise ValueError('C2_RECOVERY_PARTITION_INVALID')
    return {
        'flow_id': payload['read_run_id'], 'conversation_id': payload['conversation_id'],
        'authorization_revision': payload['authorization_revision'],
        'contract_revision': payload['contract_revision'], 'contract_sha256': payload['contract_sha256'],
        'payload_sha256': payload_sha256(payload), 'partition_index': index, 'partition_count': count,
        'source_message_keys': sorted(keys), 'expected_source_message_keys': sorted(expected),
    }


def utc_identity(value: str | datetime) -> str:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError('C2_RECOVERY_BINDING_TIME_INVALID')
    return parsed.astimezone(timezone.utc).isoformat()


def validate_settlement(payload: dict, result: dict, *, worker_id: str,
                        client_instance_id: str, bound_at: str) -> dict:
    identity = partition_identity(payload)
    proof = result.get('recovery_settlement')
    if (result.get('recovery_action') not in TERMINAL_ACTIONS or not isinstance(proof, dict)
            or type(proof.get('protocol_version')) is not int or proof.get('protocol_version') != PROTOCOL_VERSION
            or proof.get('disposition') != 'business_cancelled'
            or any(not isinstance(proof.get(key), str) or not proof[key].strip()
                   for key in ('proof_id', 'reason_code', 'worker_id', 'client_instance_id'))
            or proof.get('worker_id') != worker_id or proof.get('client_instance_id') != client_instance_id
            or utc_identity(proof.get('bound_at')) != utc_identity(bound_at)
            or not proof.get('settled_at')
            or any(proof.get(key) != value for key, value in identity.items()
                   if key not in {'source_message_keys', 'expected_source_message_keys'})):
        raise ValueError('C2_RECOVERY_PROOF_MISMATCH')
    cancelled = proof.get('source_message_keys')
    accepted = result.get('accepted_source_message_keys')
    if (not isinstance(cancelled, list) or not isinstance(accepted, list)
            or len(cancelled) != len(set(cancelled)) or len(accepted) != len(set(accepted))
            or set(cancelled) & set(accepted)
            or set(cancelled) | set(accepted) != set(identity['source_message_keys'])):
        raise ValueError('C2_RECOVERY_PROOF_COVERAGE_MISMATCH')
    confirmations = result.get('results') or []
    confirmed = {row.get('source_message_key') for row in confirmations
                 if row.get('ingest_result') in {'ingested', 'duplicated'}
                 and row.get('message_event_id')}
    if confirmed != set(accepted):
        raise ValueError('C2_RECOVERY_ACCEPTED_PROOF_MISMATCH')
    utc_identity(proof['settled_at'])
    return proof
