"""Comparison views for already confirmed sends; never infer a send result.

Worker supplies its immutable receipts. Backend must authenticate each receipt
against the original sent action before using the same view to verify a proof.
No observation text is used to create a receipt or durable message identity.
"""
import hashlib
import json
import re

from .message_contract import canonical_reply_text, reply_text_hash
from .message_viewport_projection import stable_business_content_signature
from .text_correspondence import checkpoint_digest


FIELDS = {'reply_action_id', 'reply_text_hash', 'reply_text', 'worker_stable_id', 'confirmed_at'}


def validate_receipts(receipts):
    if not isinstance(receipts, list) or not 1 <= len(receipts) <= 200:
        raise ValueError('TEXT_CORRESPONDENCE_PROOF_INVALID')
    actions, ids = set(), set()
    for receipt in receipts:
        if (not isinstance(receipt, dict) or set(receipt) != FIELDS
                or any(not isinstance(v, str) or not v or len(v) > 20000 for v in receipt.values())
                or not re.fullmatch(r'worker-message-[1-9]\d*', receipt['worker_stable_id'])
                or receipt['reply_action_id'] in actions or receipt['worker_stable_id'] in ids
                or reply_text_hash(receipt['reply_text']) != receipt['reply_text_hash']):
            raise ValueError('TEXT_CORRESPONDENCE_PROOF_INVALID')
        actions.add(receipt['reply_action_id'])
        ids.add(receipt['worker_stable_id'])
    return receipts


def comparison_entry(conversation_id, receipt):
    """Reuse an existing committed ID and its frozen source-key derivation."""
    stable_id = receipt['worker_stable_id']
    raw = json.dumps({'conversation_id': conversation_id, 'identity_kind': 'worker_sequence',
                      'identity': stable_id}, ensure_ascii=False, sort_keys=True, default=str)
    source = 'source:' + hashlib.sha1(raw.encode('utf-8')).hexdigest()[:40]
    text = canonical_reply_text(receipt['reply_text'])
    signature = stable_business_content_signature({'row_kind': 'text_bubble',
        'sender_role': 'self', 'message_type': 'text', 'content_clean': text})
    original_receipt = {k: v for k, v in receipt.items() if k != 'reply_text'}
    original_receipt['reconciliation_state'] = 'confirmed'
    return {'stable_id': stable_id, 'source_message_key': source,
        'sender_role': 'self', 'message_type': 'text', 'normalized_content_hash': signature,
        'effective_text': {'text': text, 'version': 0, 'sha256': hashlib.sha256(text.encode()).hexdigest()},
        'business_projection': {'screen_order': 0, 'sender_role': 'self', 'message_type': 'text',
            'normalized_content_signature': signature, 'media_state': ''},
        'strong_boundary_tokens': [f'fact:self:text:{signature}:'],
        'message_identity_commit_record': {'object_type': 'committed_message',
            'worker_stable_id': stable_id, 'commit_basis': 'confirmed_sent_ack',
            'observation_id': 'confirmed-ai-reply:' + receipt['reply_action_id'],
            'sender_role': 'self', 'message_type': 'text',
            'proof': {'reply_action_id': receipt['reply_action_id']}},
        'message_identity_runtime_evidence': {'_worker_ai_reply_receipt': original_receipt}}


def extend_checkpoint(checkpoint, receipts):
    """Keep the server history intact and append only unrepresented receipts.

    The full receipt list travels with the proof, including offscreen rows, so
    backend verification cannot accidentally use a shorter history. Existing
    proofs without the additive field retain their original interpretation.
    """
    if not receipts or 'historical_match_policy' not in checkpoint:
        return checkpoint
    if checkpoint.get('checkpoint_digest') != checkpoint_digest(checkpoint):
        raise ValueError('TEXT_CORRESPONDENCE_CHECKPOINT_INVALID')
    validate_receipts(receipts)
    known = {e.get('stable_id') for e in checkpoint.get('recent_messages', [])}
    additions = [dict(r) for r in receipts if r['worker_stable_id'] not in known]
    if not additions:
        return checkpoint
    additions.sort(key=lambda r: int(r['worker_stable_id'].rsplit('-', 1)[1]))
    if checkpoint.get('confirmed_sent_receipts'):
        raise ValueError('TEXT_CORRESPONDENCE_CHECKPOINT_INVALID')
    entries = [*checkpoint.get('recent_messages', []),
               *(comparison_entry(checkpoint['conversation_id'], r) for r in additions)]
    result = {**checkpoint, 'recent_messages': entries, 'confirmed_sent_receipts': additions}
    result['checkpoint_digest'] = checkpoint_digest(result)
    return result
