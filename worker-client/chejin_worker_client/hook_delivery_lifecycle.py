"""Proof of complete replacement delivery, shared by update and retention.

An old authorization was not accepted. It can only close as covered by a newer
authorization after the identical raw facts were both stored and dispatched.
Original payloads and receipts remain evidence; no send permission is implied.
"""
from .shared_rules import hook_contract_rules as rules


def envelope_for(row):
    envelope = rules.validate_envelope(rules.loads(row['payload_json']))
    if (envelope['event_uuid'] != row['event_uuid']
            or rules.digest(envelope) != row['payload_digest']):
        raise ValueError('HOOK_OUTBOX_PAYLOAD_CHANGED')
    return envelope


def covers(original, replacement):
    keys = set(original) - {'event_uuid', 'binding_revision', 'messages'}
    return (all(original[k] == replacement[k] for k in keys)
        and replacement['binding_revision'] > original['binding_revision']
        and {rules.canonical(m) for m in original['messages']}
            <= {rules.canonical(m) for m in replacement['messages']})


def validate_dispatch(envelope, dispatched):
    ids = dispatched.get('message_event_ids') if isinstance(dispatched, dict) else None
    if (not isinstance(dispatched, dict)
            or dispatched.get('event_uuid') != envelope['event_uuid']
            or dispatched.get('raw_source_event_uuid') != envelope['source_event_uuid']
            or dispatched.get('dispatch_status') != 'facts_stored'
            or dispatched.get('can_send') is not False
            or not isinstance(ids, list)
            or len(ids) != len(rules.unique_message_facts(envelope))
            or any(not rules.identifier(i) for i in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('HOOK_DELIVERY_DISPATCH_MISMATCH')


def terminal_delivery(row):
    if row['status'] == 'stored':
        return True  # Existing receipt semantics are unchanged.
    if row['status'] != 'covered_by_new_authorization':
        return False
    original = envelope_for(row)
    proof = rules.loads(row['receipt_json'])
    if (proof.get('kind') != 'hook-delivery-coverage-v1'
            or proof.get('original_event_uuid') != original['event_uuid']
            or proof.get('original_payload_digest') != rules.digest(original)):
        raise ValueError('HOOK_DELIVERY_COVERAGE_MISMATCH')
    replacement = rules.validate_envelope(proof['replacement_envelope'])
    rules.validate_receipt(replacement, proof['replacement_receipt'])
    validate_dispatch(replacement, proof['replacement_dispatch'])
    if not covers(original, replacement):
        raise ValueError('HOOK_DELIVERY_COVERAGE_MISMATCH')
    return True
