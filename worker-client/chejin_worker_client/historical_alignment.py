"""Optional historical OCR correspondence, backed by the shared machine contract."""
from .c2_contract import c2_contract_v3
from .shared_rules import historical_text_alignment


def checkpoint_for_target(target):
    contract = c2_contract_v3().get("text_correspondence_contract") or {}
    checkpoint = (target.raw or {}).get("identity_checkpoint") or {}
    if contract.get("version") != 1 or "text_correspondence_context" not in checkpoint:
        return None
    return checkpoint


def projected_frame(checkpoint, observations, *, pre_frame_id, post_frame_id):
    return historical_text_alignment.comparison_projection(checkpoint, observations,
        pre_frame_id=pre_frame_id, post_frame_id=post_frame_id)


def refreshed_correspondence(payload, checkpoint):
    """Revalidate frozen observations; never allocate IDs or rewrite a fact."""
    from apps.wechat_ai_customer_service.adapters.business_viewport_continuity import boundary_tokens_for_observations
    evidence = payload.get('evidence') or {}
    alignment = evidence.get('sequence_alignment_evidence') or {}
    previous = alignment.get('text_correspondence')
    if not previous or not checkpoint:
        return None
    built = historical_text_alignment.build_correspondence(checkpoint, evidence.get('observations') or [],
        pre_frame_id=alignment['pre_frame_id'], post_frame_id=alignment['post_frame_id'],
        new_boundary_tokens=boundary_tokens_for_observations(evidence.get('observations') or [], committed_only=False))
    if not built:
        return None
    proof = built['proof']
    original = {p['observation_id']: p for p in previous['pairs']}
    if set(original) != {p['observation_id'] for p in proof['pairs']}:
        return None
    identity_fields = ('observation_id', 'source_message_key', 'canonical_text_sha256', 'observed_text_sha256')
    if any(any(p[key] != original[p['observation_id']][key] for key in identity_fields) for p in proof['pairs']):
        return None
    return proof
