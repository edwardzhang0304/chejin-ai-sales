"""Continue an existing reply group only from the normal Worker task loop.

Crash recovery itself remains receipt-only. It leaves this original Flow open
until the normal loop has a healthy, running client and settled durable facts.
"""
from .models import WechatReadTarget
from .storage import load_c2_state, load_runtime_control, save_c2_state
from .ui_lock import acquire_ui_lock, lock_summary


def reuse_continuation_read(runner, target, observation, *, flow_id, lease):
    """Compare the just-ingested frame with its newly frozen checkpoint.

    This is a one-call-stack optimization, never a persisted screenshot cache.
    Unprovable/stale frames fall back to the ordinary pre-send refresh. Sidecar
    still captures and validates the live conversation before and after typing.
    """
    if not observation or not flow_id or lease is None:
        return None
    lock = lock_summary()
    if (runner.current_ui_lock is not lease
            or load_runtime_control().get("inflight_flow_id") != flow_id
            or lease.lease_lost or lock.get("locked") is not True
            or lock.get("lock_id") != lease.lock_id
            or lock.get("fencing_token") != lease.fencing_token
            or observation.get("ok") is not True
            or observation.get("new_customer_message_count") != 0):
        return None
    frame = observation.get("_reply_sequence_frame")
    if not isinstance(frame, dict) or not frame or frame.get("ui_frame_invalidated") is True:
        return None
    _, comparison = runner._compare_pre_send_fact_checkpoint_frame(
        target=target, sidecar_payload=frame, read_run_id=flow_id, comparison_only=True,
    )
    if comparison.get("comparison_result") != "checkpoint_equal":
        return None
    # Counts below are relative to the NEW frozen checkpoint. The preceding
    # AI bubble was just ingested, and is now part of that historical baseline.
    return {
        **{key: value for key, value in observation.items() if key != "_reply_sequence_frame"},
        "new_self_message_count": 0,
        "pre_send_fact_checkpoint_comparison": comparison,
        "pre_send_fact_checkpoint_result": "checkpoint_equal",
        "pre_send_refresh_source": "reply_sequence_read",
    }


def interrupt_on_new_customer(runner, binding, target, payload):
    """Revoke unsent old segments before any slow voice/image action begins."""
    marker = load_c2_state(f"reply_sequence_flow:{load_runtime_control().get('inflight_flow_id')}")
    continuation = target.raw.get("batch_continuation") or {}
    if not marker or int(marker.get("segment_count") or 0) <= 1 or continuation.get("batch_id") != marker.get("batch_id"):
        return
    from .task_runner import pre_send_new_suffix_validation
    alignment = payload.get("sequence_alignment_evidence") or {}
    observations = payload.get("observations") or []
    if not pre_send_new_suffix_validation(observations, alignment).get("ok"):
        return
    suffix = set(alignment.get("new_suffix_observation_ids") or [])
    new_ids = [o["observation_id"] for o in observations if o.get("observation_id") in suffix
               and o.get("sender_role") == "customer" and not o.get("backend_confirmed")]
    if new_ids:
        remember_customer_interruption(str(load_runtime_control().get("inflight_flow_id") or ""))
        runner.api.interrupt_reply_sequence(binding, marker["batch_id"],
            frame_id=str(alignment.get("post_frame_id") or payload.get("frame_id") or ""), observation_ids=new_ids)


def flow_sequence_status(runner, binding, flow_id):
    """None means no sequence; a transient lookup failure means still unknown."""
    state = load_c2_state(f"reply_sequence_flow:{flow_id}")
    if not state.get("batch_id"):
        return None
    try:
        return runner.api.get_wechat_message_batch(binding, state["batch_id"])
    except Exception as exc:
        if not runner._legacy_media_error_is_retryable(exc):
            raise
        # Local uncertainty, not a fabricated backend terminal/authorization.
        # Both normal and restart finish checks must keep this Flow reserved.
        return {"status_unavailable": True}


def sequence_needs_continuation(status, flow_id):
    if not status:
        return False
    group = status.get("reply_sequence") or {}
    if not group.get("terminal"):
        return True
    state = load_c2_state(f"reply_sequence_flow:{flow_id}")
    if not state.get("read_after_interruption"):
        return False
    authorization = status.get("authorization") or {}
    # Explicit business cancellation ends only the intention to read again.
    # Keep the marker and all original receipts until the ordinary no-UI
    # settlement/finish barriers succeed. Other denials may be temporary.
    cancelled = (
        group.get("terminal") is True
        and bool(state.get("batch_id"))
        and state["batch_id"] == status.get("batch_id") == group.get("batch_id")
        and bool(state.get("conversation_id"))
        and state["conversation_id"] == status.get("conversation_id") == authorization.get("conversation_id")
        and authorization.get("allowed") is False
        and authorization.get("error_code") == "LEAD_INVALID"
        and authorization.get("recovery_decision") == "cancel"
    )
    return not cancelled


def remember_customer_interruption(flow_id):
    key = f"reply_sequence_flow:{flow_id}"
    save_c2_state(key, {**load_c2_state(key), "read_after_interruption": True})


def remember_ingested_replacement(payload, result):
    """Save the server's batch pointer before acknowledging the original Outbox."""
    flow_id = str(payload.get("read_run_id") or "")
    key = f"reply_sequence_flow:{flow_id}"
    state = load_c2_state(key)
    replacement = result.get("message_batch") or {}
    if (state and flow_id == load_runtime_control().get("inflight_flow_id")
            and state.get("conversation_id") == payload.get("conversation_id")
            and state.get("read_after_interruption")
            and result.get("state_transition_applied") is True
            and replacement.get("batch_id")):
        save_c2_state(key, {**state, "batch_id": replacement["batch_id"], "read_after_interruption": False})


def reread_after_interruption(runner, binding, target):
    """The old snapshot is evidence only. New facts require the normal C2 read."""
    flow_id = str(load_runtime_control().get("inflight_flow_id") or "")
    status = flow_sequence_status(runner, binding, flow_id)
    if not status or not (status.get("authorization") or {}).get("allowed"):
        return {"ok": False, "error_code": "C2_TARGET_NOT_ALLOWED_BY_BATCH_AUTHORIZATION"}
    runner._apply_batch_continuation_to_target(status, target)
    target.raw.pop("pre_send_fact_checkpoint_context", None)
    observed = runner._read_one_wechat_target(
        binding, target, current_step="reply_sequence_interruption_read", operation_phase="authorized_read",
        allow_during_current_task=True, enforce_read_targets=True,
        held_lease=runner.current_ui_lock, current_only=True, wait_for_brain=False,
    )
    if observed.get("ok"):
        key = f"reply_sequence_flow:{flow_id}"
        save_c2_state(key, {**load_c2_state(key), "read_after_interruption": False})
    return observed


def _finish_settled_sequence(runner, binding, flow_id):
    prepared = runner._prepare_inflight_finish_from_durable_state(binding, flow_id=flow_id)
    if prepared:
        _, conversation_id, receipt, terminal_kind = prepared
        runner._finish_recovery_request(
            binding, flow_id, terminal_kind=terminal_kind,
            conversation_id=receipt.get("conversation_id") or conversation_id,
            error_code=receipt.get("error_code"),
        )


def resume_reply_sequence(runner, binding):
    """Return whether this loop belongs to a still-open sequence, never a new Flow."""
    control = load_runtime_control()
    flow_id = str(control.get("inflight_flow_id") or "")
    state = load_c2_state(f"reply_sequence_flow:{flow_id}")
    if not state:
        return False
    with runner.task_lock:
        if runner.current_task or runner.current_ui_lock or lock_summary().get("locked"):
            return True
        if not runner._can_continue_inflight_flow(flow_id):
            return True
        if not runner._worker_transaction_barrier_ready(
            binding, reason="reply_sequence_resume",
            allowed_image_recovery_conversation_id=str(state.get("conversation_id") or ""),
        ):
            return True
        status = flow_sequence_status(runner, binding, flow_id)
        if not status:
            return False
        if not sequence_needs_continuation(status, flow_id):
            _finish_settled_sequence(runner, binding, flow_id)
            return True
        if binding.run_status == "faulted":
            _finish_settled_sequence(runner, binding, flow_id)
            return True
        if binding.run_status != "running" or control.get("pause_requested") or control.get("update_no_new_work"):
            return True
        authorization = status.get("authorization") or {}
        if not authorization.get("allowed"):
            return True
        target = WechatReadTarget.from_api({
            **authorization,
            "unread_generation": runner._backend_inflight_flow_state.get("unread_generation", 0),
        })
        runner._apply_batch_continuation_to_target(status, target)
        lease = acquire_ui_lock(operation_type="message_ingest", owner=f"{binding.worker_id}:sequence:{flow_id}",
                                current_step="reply_sequence_resume")
        lease.start_auto_renew()
        runner.current_ui_lock = lease
        try:
            # Restart may find a different conversation on screen. Reuse the
            # ordinary authorized target-locating/read flow before any send.
            observed = runner._read_one_wechat_target(
                binding, target, current_step="reply_sequence_resume", operation_phase="authorized_read",
                allow_during_current_task=True, enforce_read_targets=True,
                held_lease=lease, current_only=False, wait_for_brain=False,
            )
            if observed.get("ok") and state.get("read_after_interruption"):
                save_c2_state(f"reply_sequence_flow:{flow_id}", {**load_c2_state(f"reply_sequence_flow:{flow_id}"), "read_after_interruption": False})
            if not observed.get("ok"):
                runner._settle_chat_reply_context_failure_before_unlock(
                    binding, task_id=str((status.get("task") or {}).get("id") or ""),
                    source_error_code=observed.get("error_code") or "REPLY_SEQUENCE_FRESH_READ_REQUIRED",
                    evidence={"reply_sequence_resume": observed},
                )
            else:
                replacement = (observed.get("result") or {}).get("message_batch") or {}
                runner._wait_and_send_current_c3_batch(
                    binding=binding, target=target, batch_id=str(replacement.get("batch_id") or status["batch_id"]),
                    cancel_check=lambda: runner.stop_event.is_set(),
                )
        finally:
            runner._release_current_ui_lock(reason="reply_sequence_resume_finished")
        _finish_settled_sequence(runner, binding, flow_id)
        return True
