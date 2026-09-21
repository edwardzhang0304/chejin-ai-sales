"""Optional historical OCR correspondence, backed by the shared machine contract."""
from .c2_contract import c2_contract_v3
from .shared_rules import historical_text_alignment


def checkpoint_for_target(target):
    contract = c2_contract_v3().get("text_correspondence_contract") or {}
    checkpoint = (target.raw or {}).get("identity_checkpoint") or {}
    if contract.get("version") not in {1, 2} or "text_correspondence_context" not in checkpoint:
        return None
    if "historical_match_policy" in checkpoint and (
            contract.get("version") != 2
            or checkpoint["historical_match_policy"] != contract.get("historical_match_policy")):
        raise ValueError("HISTORICAL_MATCH_POLICY_INVALID")
    # The next read can precede ingestion of our own successful send. It is
    # already history, backed by a receipt, and uses the same HC decision as
    # server history in reads, pre-send guards, interruptions and media reads.
    from .storage import load_c2_state
    from apps.wechat_ai_customer_service.adapters.confirmed_sent_history import FIELDS, extend_checkpoint
    receipts = (load_c2_state('message_identity:' + target.conversation_id).get('ai_reply_receipts') or [])
    confirmed = [{k: str(r.get(k) or '') for k in FIELDS} for r in receipts
        if isinstance(r, dict) and r.get('reconciliation_state', 'confirmed') == 'confirmed']
    return extend_checkpoint(checkpoint, confirmed)


def projected_frame(checkpoint, observations, *, pre_frame_id, post_frame_id):
    return historical_text_alignment.comparison_projection(checkpoint, observations,
        pre_frame_id=pre_frame_id, post_frame_id=post_frame_id)


def admit_current_context_frame(checkpoint, payload):
    """Judge a current-screen context reread with the same historical rule.

    Chejin's targeted messages route permits one frame and zero scrolls. Do
    not send it through the independent product's exact-text anchor search.
    This is navigation evidence only; final frame/media admission still runs.
    """
    from apps.wechat_ai_customer_service.adapters.business_viewport_continuity import (
        boundary_tokens_for_observations, compare_business_viewport_continuity)
    from apps.wechat_ai_customer_service.adapters.message_viewport_projection import normalized_business_message_sequence
    rows = payload.get('observations') or []
    frame_id = str((payload.get('frame_observation') or {}).get('frame_id') or payload.get('frame_id') or '')
    entries = historical_text_alignment.comparison_entries(checkpoint)
    projection = [e.get('business_projection') or {} for e in entries]
    tokens = {i: set(e.get('strong_boundary_tokens') or []) for i, e in enumerate(entries)}
    decision = {'relation': 'business_sequence_not_continuous', 'reason': 'context_frame_invalid'}
    unchanged = (payload.get('ui_action_performed') is not True
                 and (payload.get('history_load') or {}).get('scroll_steps', 0) == 0)
    if (payload.get('ok') is True and frame_id and isinstance(rows, list) and rows
            and unchanged and not payload.get('observation_validation_errors')):
        decision = compare_business_viewport_continuity(projection,
            normalized_business_message_sequence(rows, message_viewport_bounds=None),
            old_boundary_tokens=tokens,
            new_boundary_tokens=boundary_tokens_for_observations(rows, committed_only=False),
            allow_history_suffix=True)
        if decision['relation'] not in historical_text_alignment.ACCEPTED_RELATIONS:
            tolerant = historical_text_alignment.validated_projection_continuity(checkpoint, rows,
                old_projection=projection, old_boundary_tokens=tokens,
                pre_frame_id='checkpoint:current-context', post_frame_id=frame_id)
            if tolerant:
                decision = tolerant
    accepted = decision['relation'] in historical_text_alignment.ACCEPTED_RELATIONS
    return {**payload, 'history_load': {
        'ok': payload.get('ok') is True, 'mode': 'current_frame_historical_context',
        'mechanism': 'shared_historical_continuity', 'anchor_found': accepted,
        'anchor_type': 'historical_context' if accepted else '',
        'scroll_steps': 0, 'snapshot_count': 1, 'restored_to_latest': False,
        'viewport_unchanged': unchanged, 'stopped_reason': decision.get('reason', ''),
        'historical_decision': decision}}


def reconcile_viewports(checkpoint, before, after, decision, *, old_boundary_tokens):
    """Use HC for old ordinary text around an otherwise unchanged media action.

    This proves only sequence continuity. The caller must still validate its
    actual selected media, physical receipt and immutable action-frame digest.
    Final ingest recomputes its separate single-frame proof from authority.
    """
    accepted = {'business_sequence_equal', 'unique_tail_append', 'unique_viewport_slide_with_tail_append'}
    if not checkpoint or decision.get('relation') in accepted:
        return decision
    compared = historical_text_alignment.compare_historical_viewports(checkpoint, before, after,
        old_boundary_tokens=old_boundary_tokens, allow_history_suffix=False)
    if not compared or compared[2]['relation'] not in accepted:
        return decision
    result = dict(compared[2])
    # A two-frame comparison is diagnostic here, not the single-frame wire
    # proof that the final ingest validator independently recomputes.
    result['historical_viewport_correspondence'] = result.pop('text_correspondence')
    return result


def refreshed_correspondence(payload, checkpoint):
    """Revalidate frozen observations; never allocate IDs or rewrite a fact."""
    from apps.wechat_ai_customer_service.adapters.business_viewport_continuity import boundary_tokens_for_observations
    evidence = payload.get('evidence') or {}
    alignment = evidence.get('sequence_alignment_evidence') or {}
    previous = alignment.get('text_correspondence')
    if not previous or not checkpoint:
        return None
    checkpoint = historical_text_alignment.checkpoint_for_proof(checkpoint, previous)
    if previous.get('version') == 2 and previous.get('policy_digest') != (
            checkpoint.get('historical_match_policy') or {}).get('policy_digest'):
        return None
    # Only the authority envelope may refresh. Recompute and compare every
    # other saved proof field (body hashes, IDs, frame IDs, versions, scores),
    # retaining the decoder for the original receipt rather than creating a
    # new-read proof with a different transcript mapping.
    proof = {**previous, 'checkpoint_digest': checkpoint['checkpoint_digest']}
    try:
        historical_text_alignment.verify_correspondence(proof, checkpoint, evidence.get('observations') or [],
            pre_frame_id=alignment['pre_frame_id'], post_frame_id=alignment['post_frame_id'],
            new_boundary_tokens=boundary_tokens_for_observations(evidence.get('observations') or [], committed_only=False))
    except ValueError:
        return None
    return proof


def bind_final_frame_correspondence(target, payload, *, deadline=None):
    """Recompute ordinary old-text identity after an original media action.

    Keep the action's full mapping, observations and receipt. The proof covers
    only confirmed history and cannot claim that a new media action completed.
    """
    from apps.wechat_ai_customer_service.adapters.business_viewport_continuity import boundary_tokens_for_observations
    checkpoint = checkpoint_for_target(target)
    alignment = payload.get('sequence_alignment_evidence') or {}
    if (not checkpoint or 'historical_match_policy' not in checkpoint
            or alignment.get('pre_sequence_source') != 'action_frame'):
        return
    rows = payload.get('authoritative_evidence_observations', payload.get('observations')) or []
    report = {}
    built = historical_text_alignment.build_correspondence(checkpoint, rows,
        pre_frame_id=alignment['pre_frame_id'], post_frame_id=alignment['post_frame_id'],
        new_boundary_tokens=boundary_tokens_for_observations(rows, committed_only=False),
        diagnostics=report, deadline=deadline)
    payload['historical_match_diagnostics'] = report
    if built and built['proof']['version'] == 2:
        payload['sequence_alignment_evidence'] = {**alignment, 'text_correspondence': built['proof'],
            'candidate_alignment_count': built['proof']['candidate_count']}


def save_match_review(target, payload, *, read_run_id, result):
    """Keep full compared bodies in the existing local artifact lifecycle only."""
    diagnostics = payload.get('historical_match_diagnostics')
    artifact_dir = payload.get('artifact_dir')
    if not diagnostics or not artifact_dir:
        return None
    from pathlib import Path
    import json
    from .storage import load_c2_state
    from .failure_evidence import record_capture_failure
    try:
        server_checkpoint = (target.raw or {}).get('identity_checkpoint') or {}
        proof = (payload.get('sequence_alignment_evidence') or {}).get('text_correspondence')
        # A successful ingest can already have consumed the local receipts.
        # Preserve the comparison view bound to this frame, not a later view.
        checkpoint = (historical_text_alignment.checkpoint_for_proof(server_checkpoint, proof)
                      if proof else checkpoint_for_target(target) or server_checkpoint)
        review = {
            'schema': 'chejin.historical_match_review.v2',
            'conversation_id': target.conversation_id, 'read_run_id': read_run_id,
            'checkpoint_digest': checkpoint.get('checkpoint_digest'),
            'server_checkpoint_digest': server_checkpoint.get('checkpoint_digest'),
            'confirmed_sent_receipts': checkpoint.get('confirmed_sent_receipts') or [],
            'original_messages': checkpoint.get('recent_messages') or [],
            'observations': payload.get('authoritative_evidence_observations', payload.get('observations')) or [],
            'frame_source': {k: payload.get(k) for k in ('sidecar_run_id', 'screenshot_path', 'review_path')},
            'diagnostics': diagnostics,
            'recheck_budget': load_c2_state('read_recheck:' + str(read_run_id)),
            'routing': {k: result.get(k) for k in ('ok', 'error_code', 'worker_faulted', 'handoff_created', 'flow_terminal_kind')},
        }
        path = Path(str(artifact_dir)) / 'historical_match_review.json'
        path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding='utf-8')
        return str(path)
    except Exception as exc:
        record_capture_failure('historical_match.review', exc)
        return None
