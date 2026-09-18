"""Verify optional D1 proof against current authoritative history, never write it."""
from app.contracts.shared_rules import shared_adapter
from app.errors import AppError
from sqlalchemy import select
from app.models.wechat import MessageEvent


def verified_pairs(db, payload, *, worker):
    alignment = payload.evidence.sequence_alignment_evidence
    proof = alignment.text_correspondence if alignment else None
    if proof is None:
        return set()
    capability = ((worker.local_lock_summary or {}).get("capabilities") or {}).get("text_correspondence_version") if worker else None
    if type(capability) is not int or capability != 1:
        raise AppError("TEXT_CORRESPONDENCE_UNSUPPORTED", "当前客户端未声明历史文字对应能力", 409)
    # An accepted request may have advanced the checkpoint before its response
    # was lost. Only the exact already-stored authoritative frame may reuse its
    # accepted mapping; a changed/new frame must be verified against current facts.
    evidence = payload.evidence.model_dump(mode="json")
    for event in db.scalars(select(MessageEvent).where(MessageEvent.worker_id == worker.id,
            MessageEvent.conversation_id == payload.conversation_id, MessageEvent.read_run_id == payload.read_run_id)):
        saved = event.evidence or {}
        if (saved.get("sequence_alignment_evidence") == evidence.get("sequence_alignment_evidence")
                and saved.get("observations") == evidence.get("observations")
                and saved.get("slot_ledger_states") == evidence.get("slot_ledger_states")):
            return {(pair["source_message_key"], pair["observation_id"]) for pair in proof["pairs"]}
    from app.services.wechat_service import _identity_checkpoint
    checkpoint = _identity_checkpoint(db, conversation_id=payload.conversation_id)
    rules = shared_adapter("historical_text_alignment")
    continuity_rules = shared_adapter("business_viewport_continuity")
    try:
        continuity = rules.verify_correspondence(
            proof, checkpoint, payload.evidence.observations,
            pre_frame_id=alignment.pre_frame_id, post_frame_id=alignment.post_frame_id,
            new_boundary_tokens=continuity_rules.boundary_tokens_for_observations(
                payload.evidence.observations, committed_only=False),
        )
        if (alignment.pre_sequence_source != "checkpoint" or alignment.alignment_status != "unique"
                or not alignment.old_tail_fully_consumed
                or alignment.candidate_alignment_count != 1):
            raise ValueError("TEXT_CORRESPONDENCE_PROOF_INVALID")
        slots = {slot.observation_id: slot for slot in payload.evidence.slot_ledger_states}
        pairs = {pair.post_observation_id: pair for pair in alignment.matched_pairs}
        entries = rules.comparison_entries(checkpoint)
        for pair in proof["pairs"]:
            slot, mapped = slots.get(pair["observation_id"]), pairs.get(pair["observation_id"])
            expected = entries[pair["old_index"]]
            if (not slot or not mapped or slot.source_message_key != pair["source_message_key"]
                    or mapped.post_index != pair["new_index"]
                    or mapped.worker_stable_id != expected["stable_id"]
                    or mapped.identity_state != "committed"
                    or not (slot.fact_scope == "historical" or slot.delivery_state == "backend_confirmed")
                    or pair["observation_id"] in alignment.new_suffix_observation_ids):
                raise ValueError("TEXT_CORRESPONDENCE_PROOF_INVALID")
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        code = "TEXT_CORRESPONDENCE_CHECKPOINT_EXPIRED" if str(exc) == "TEXT_CORRESPONDENCE_CHECKPOINT_EXPIRED" else "TEXT_CORRESPONDENCE_PROOF_INVALID"
        raise AppError(code, "历史文字对应凭证未通过权威校验", 409) from exc
    return {(pair["source_message_key"], pair["observation_id"]) for pair in proof["pairs"]}
