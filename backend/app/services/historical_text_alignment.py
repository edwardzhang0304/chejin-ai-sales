"""Verify optional D1 proof against current authoritative history, never write it."""
from app.contracts.shared_rules import shared_adapter
from app.errors import AppError
from sqlalchemy import select
from app.models.wechat import MessageEvent


def verified_pairs(db, payload, *, worker, rebuild_missing=False):
    alignment = payload.evidence.sequence_alignment_evidence
    proof = alignment.text_correspondence if alignment else None
    if proof is None:
        if rebuild_missing and alignment and worker:
            from app.services.read_recovery_service import closed_read_verified_pairs
            accepted = closed_read_verified_pairs(db, worker, payload)
            if accepted is not None:
                return accepted
            # Only the original stopped/closed read owner can reach this path.
            # Rebuild from authority plus the frozen frame, then run the same
            # full proof/mapping verifier. Never rewrite the saved request.
            from app.services.wechat_service import _identity_checkpoint
            checkpoint = _identity_checkpoint(db, conversation_id=payload.conversation_id)
            rules = shared_adapter('historical_text_alignment')
            try:
                built = rules.build_correspondence(checkpoint, payload.evidence.observations,
                    pre_frame_id=alignment.pre_frame_id, post_frame_id=alignment.post_frame_id,
                    new_boundary_tokens=shared_adapter('business_viewport_continuity').boundary_tokens_for_observations(
                        payload.evidence.observations, committed_only=False))
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise AppError('TEXT_CORRESPONDENCE_PROOF_INVALID', '历史文字对应凭证未通过权威校验', 409) from exc
            if built and built['proof']['version'] == 2:
                comparison = payload.model_copy(deep=True)
                comparison.evidence.sequence_alignment_evidence.text_correspondence = built['proof']
                return verified_pairs(db, comparison, worker=worker)
        return set()
    capability = ((worker.local_lock_summary or {}).get("capabilities") or {}).get("text_correspondence_version") if worker else None
    if type(capability) is not int or capability not in {1, 2} or capability < proof.get('version', 0):
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
            accepted = {(pair["source_message_key"], pair["observation_id"]) for pair in proof["pairs"]}
            if proof['version'] == 2:
                voices = {o['observation_id'] for o in saved.get('observations', [])
                          if o.get('row_kind') == 'voice_transcript' and o.get('voice_state') == 'transcribed'}
                accepted.update((s['source_message_key'], s['observation_id']) for s in saved.get('slot_ledger_states', [])
                    if s.get('observation_id') in voices and s.get('source_message_key') and s.get('fact_scope') == 'historical')
            return accepted
    from app.services.wechat_service import _identity_checkpoint
    checkpoint = _identity_checkpoint(db, conversation_id=payload.conversation_id)
    rules = shared_adapter("historical_text_alignment")
    checkpoint = rules.checkpoint_for_proof_version(checkpoint, proof['version'])
    continuity_rules = shared_adapter("business_viewport_continuity")
    try:
        from app.services.wechat_service import _verified_ai_reply_action_for_self_message
        sent_rules = shared_adapter('confirmed_sent_history')
        receipt_entries = {}
        for receipt in proof.get('confirmed_sent_receipts', []):
            entry = sent_rules.comparison_entry(payload.conversation_id, receipt)
            canonical_receipt = {**entry['message_identity_runtime_evidence']['_worker_ai_reply_receipt'],
                                 'source_message_key': entry['source_message_key']}
            action = _verified_ai_reply_action_for_self_message(db, conversation_id=payload.conversation_id,
                content=receipt['reply_text'], source_message_key=entry['source_message_key'],
                raw_payload={'observation': {'row_kind': 'text_bubble', 'message_type': 'text'},
                             'ai_reply_receipt': canonical_receipt})
            if action is None or action.status != 'sent' or action.claimed_by_worker_id != worker.id:
                raise ValueError('TEXT_CORRESPONDENCE_PROOF_INVALID')
            receipt_entries[entry['source_message_key']] = canonical_receipt
        checkpoint = rules.checkpoint_for_proof(checkpoint, proof)
        continuity = rules.verify_correspondence(
            proof, checkpoint, payload.evidence.observations,
            pre_frame_id=alignment.pre_frame_id, post_frame_id=alignment.post_frame_id,
            new_boundary_tokens=continuity_rules.boundary_tokens_for_observations(
                payload.evidence.observations, committed_only=False),
        )
        allowed_sources = {'checkpoint', 'action_frame'} if proof['version'] == 2 else {'checkpoint'}
        if (alignment.pre_sequence_source not in allowed_sources or alignment.alignment_status != "unique"
                or not alignment.old_tail_fully_consumed
                or alignment.candidate_alignment_count != continuity.get('candidate_alignment_count', 1)):
            raise ValueError("TEXT_CORRESPONDENCE_PROOF_INVALID")
        slots = {slot.observation_id: slot for slot in payload.evidence.slot_ledger_states}
        pairs = {pair.post_observation_id: pair for pair in alignment.matched_pairs}
        entries = rules.comparison_entries(checkpoint)
        def matches_original_slot(mapped, slot, entry):
            if mapped.identity_state == 'committed':
                return mapped.worker_stable_id == entry['stable_id']
            # Original pre-send comparison deliberately makes NO physical
            # media identity claim. Its settled slot points to a server fact;
            # the shared proof independently checks that complete old prefix.
            return (proof['version'] == 2 and alignment.pre_sequence_source == 'checkpoint'
                and mapped.identity_state == 'frame_local_unselected' and not mapped.worker_stable_id
                and slot.delivery_state == 'backend_confirmed'
                and slot.source_message_key == entry['source_message_key'])

        if proof['version'] == 2:
            rows = shared_adapter('message_viewport_projection').ordered_message_viewport_observations(payload.evidence.observations)
            expected_mapping = [(p['old_index'], p['new_index']) for p in continuity['matched_pairs']]
            mapped_prefix = alignment.matched_pairs[:len(expected_mapping)]
            if ([p.post_index for p in mapped_prefix] != [j for _, j in expected_mapping]
                    or alignment.pre_sequence_source == 'checkpoint'
                    and [(p.pre_index, p.post_index) for p in mapped_prefix] != expected_mapping):
                raise ValueError('TEXT_CORRESPONDENCE_PROOF_INVALID')
            # Frozen older proofs may omit local receipts from their comparison
            # baseline. Keep that wire interpretation for replay only. New
            # proofs include confirmed sends in the same recomputed HC mapping.
            suffix_start = len(expected_mapping)
            messages = {(m.raw_payload or {}).get('observation', {}).get('observation_id'): m for m in payload.messages}
            historical_sources = {entry['source_message_key'] for entry in entries}
            historical_ids = {entry['stable_id'] for entry in entries}
            for offset, mapped in enumerate(alignment.matched_pairs[len(expected_mapping):]):
                item = messages.get(mapped.post_observation_id)
                slot = slots.get(mapped.post_observation_id)
                if (alignment.pre_sequence_source == 'action_frame'
                        and item and item.message_type == 'text' and item.sender_role_hint == 'customer'
                        and slot and slot.fact_scope == 'current_read_run'
                        and slot.origin_read_run_id == payload.read_run_id
                        and slot.delivery_state == 'outbox_waiting'
                        and slot.source_message_key == item.source_message_key
                        and item.source_message_key not in historical_sources
                        and mapped.identity_state == 'committed'
                        and mapped.worker_stable_id not in historical_ids
                        and mapped.worker_stable_id == (item.raw_payload.get('dedupe_basis') or {}).get('worker_stable_id')
                        and mapped.post_index == suffix_start+offset):
                    # New text observed before the media action remains in
                    # this request's ingest set. It is not a historical HC
                    # pair and cannot be omitted as already delivered.
                    continue
                if (alignment.pre_sequence_source == 'action_frame'
                        and (mapped.identity_state, mapped.match_basis) in {
                            ('selected_action','confirmed_action'), ('committed','prior_confirmed_action')}
                        and item and item.message_type in {'voice','image'} and slot
                        and slot.source_message_key == item.source_message_key
                        and slot.fact_scope == 'current_read_run'
                        and mapped.post_index == suffix_start+offset):
                    # Only classify this as an original media-action mapping.
                    # The unchanged V3 action/parent/result validators below
                    # must still verify its receipt and completed/failed state.
                    continue
                if (alignment.pre_sequence_source == 'checkpoint' and mapped.pre_index != len(entries)+offset or mapped.post_index != suffix_start+offset
                        or not item or item.sender_role_hint not in {'self', 'sales'}
                        or (item.raw_payload.get('ai_reply_receipt') or {}).get('worker_stable_id') != mapped.worker_stable_id):
                    raise ValueError('TEXT_CORRESPONDENCE_PROOF_INVALID')
                action = _verified_ai_reply_action_for_self_message(db, conversation_id=payload.conversation_id,
                    content=item.content, source_message_key=item.source_message_key, raw_payload=item.raw_payload)
                if action is None or action.status != 'sent':
                    raise ValueError('TEXT_CORRESPONDENCE_PROOF_INVALID')
            if alignment.new_suffix_observation_ids != [r['observation_id'] for r in rows[len(alignment.matched_pairs):]]:
                raise ValueError('TEXT_CORRESPONDENCE_PROOF_INVALID')
            for mapped in mapped_prefix:
                entry = entries[expected_mapping[mapped.post_index][0]]
                slot = slots.get(mapped.post_observation_id)
                if (not slot or slot.source_message_key != entry['source_message_key']
                        or not matches_original_slot(mapped, slot, entry)):
                    raise ValueError('TEXT_CORRESPONDENCE_PROOF_INVALID')
        verified = [*proof['pairs'], *continuity.get('voice_correspondence', [])]
        for pair in verified:
            slot, mapped = slots.get(pair["observation_id"]), pairs.get(pair["observation_id"])
            expected = entries[pair["old_index"]]
            receipt = receipt_entries.get(pair['source_message_key'])
            delivered_receipt = False
            if receipt:
                item = next((m for m in payload.messages if m.source_message_key == pair['source_message_key']), None)
                actual = (item.raw_payload or {}).get('ai_reply_receipt', {}) if item else {}
                delivered_receipt = bool(item and item.sender_role_hint in {'self', 'sales'}
                    and item.message_type == 'text'
                    and (item.raw_payload.get('observation') or {}).get('observation_id') == pair['observation_id']
                    and all(actual.get(k) == v for k, v in receipt.items())
                    and slot and slot.delivery_state == 'outbox_waiting')
            if (not slot or not mapped or slot.source_message_key != pair["source_message_key"]
                    or mapped.post_index != pair["new_index"]
                    or not matches_original_slot(mapped, slot, expected)
                    or not (delivered_receipt if receipt else
                            slot.fact_scope == "historical" or slot.delivery_state == "backend_confirmed")
                    or pair["observation_id"] in alignment.new_suffix_observation_ids):
                raise ValueError("TEXT_CORRESPONDENCE_PROOF_INVALID")
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        code = "TEXT_CORRESPONDENCE_CHECKPOINT_EXPIRED" if str(exc) == "TEXT_CORRESPONDENCE_CHECKPOINT_EXPIRED" else "TEXT_CORRESPONDENCE_PROOF_INVALID"
        raise AppError(code, "历史文字对应凭证未通过权威校验", 409) from exc
    return {(pair["source_message_key"], pair["observation_id"]) for pair in verified}
