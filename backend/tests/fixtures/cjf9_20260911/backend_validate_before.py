# Frozen pre-fix production method for recovery/ablation only.
def _validate_non_delivered_frame_observations(
    db: Session,
    payload: WechatMessageIngestRequest,
) -> None:
    """Prove every non-delivered settled frame row is already persisted.

    ``messages`` is an incremental delivery set.  The authoritative frame may
    also contain historical rows needed for the pre-send checkpoint, but a
    Worker declaration alone must never be enough to suppress a new fact.
    """

    mapped_observation_ids = {
        str(
            (item.raw_payload or {}).get("observation", {}).get(
                "observation_id"
            )
            or ""
        ).strip()
        for item in payload.messages
        if isinstance(item.raw_payload, dict)
        and isinstance(item.raw_payload.get("observation"), dict)
    }
    observations_by_id = {
        str(observation.get("observation_id") or "").strip(): observation
        for observation in payload.evidence.observations
        if isinstance(observation, dict)
        and str(observation.get("observation_id") or "").strip()
    }
    partition = payload.evidence.ingest_partition
    is_final_partition = bool(
        partition is not None and partition.index == partition.count
    )
    settled_slots = []
    for slot in payload.evidence.slot_ledger_states:
        observation_id = str(slot.observation_id or "").strip()
        observation = observations_by_id.get(observation_id)
        if observation_id in mapped_observation_ids or not isinstance(
            observation, dict
        ):
            continue
        _validated_id, rule = _validate_v3_observation(observation)
        if not bool(rule.get("ingestible")):
            continue
        if slot.fact_scope == "historical" or (
            is_final_partition and slot.fact_scope == "current_read_run"
        ):
            settled_slots.append(slot)
    if not settled_slots:
        return

    source_keys = {
        str(slot.source_message_key or "").strip()
        for slot in settled_slots
        if str(slot.source_message_key or "").strip()
    }
    existing_by_source_key = {
        str(event.source_message_key or "").strip(): event
        for event in db.scalars(
            select(MessageEvent).where(
                MessageEvent.conversation_id == payload.conversation_id,
                MessageEvent.source_message_key.in_(source_keys),
            )
        ).all()
        if str(event.source_message_key or "").strip()
    }
    invalid: list[str] = []
    for slot in settled_slots:
        observation_id = str(slot.observation_id or "").strip()
        observation = observations_by_id.get(observation_id)
        event = existing_by_source_key.get(
            str(slot.source_message_key or "").strip()
        )
        observed_type = str(
            (observation or {}).get("message_type") or ""
        ).strip().lower()
        content_mismatch = bool(
            event is not None
            and observed_type in {"text", "voice", "system"}
            and _normalized_contract_text(event.content)
            != _normalized_contract_text(
                (observation or {}).get("content_clean")
            )
        )
        if (
            not isinstance(observation, dict)
            or event is None
            or str(event.sender_role or "").strip().lower()
            != str(observation.get("sender_role") or "").strip().lower()
            or str(event.message_type or "").strip().lower()
            != str(observation.get("message_type") or "").strip().lower()
            or content_mismatch
        ):
            invalid.append(observation_id)
    if invalid:
        raise AppError(
            "MESSAGE_OBSERVATION_MAPPING_INCOMPLETE",
            (
                "V3 完整画面的非本分片 observation 无法绑定到已入库事实: "
                f"invalid={sorted(invalid)}"
            ),
            409,
        )
