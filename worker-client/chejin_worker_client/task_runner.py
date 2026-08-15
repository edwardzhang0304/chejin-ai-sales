from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, TypeAlias

from .api import ApiError, WorkerApiClient
from .action_journal import (
    action_journal_item_has_formed_fact,
    action_journal_is_strictly_not_attempted,
    action_journal_path,
    action_journal_phase,
    commit_action_journal_item_identity,
    initialize_action_journal,
    list_action_journals,
    remove_action_journal,
    read_action_journal,
    record_action_sequence_alignment,
    update_action_journal_item,
)
from .artifact_retention import cleanup_artifacts, record_artifact_outcome
from .c2_contract import (
    contract_revision,
    formal_image_failure_code,
    observation_role_is_trusted,
    sidecar_contract_error,
    target_location_recovery_contract,
)
from .c2_outbox_recovery import (
    encoded_payload_size,
    rebuild_identity_collision,
    rebuild_invalid_media_as_failed,
    split_ingest_payload,
)
from .config import CONFIG
from .emergency_stop import emergency_stop_requested, trigger_emergency_stop
from .image_phase import (
    finalize_image_phase_result,
    mark_image_action,
    mark_image_removed_from_final_screen,
    mark_image_ui_frame_invalidated,
    mark_image_terminal,
    merge_image_phase_results,
    new_image_phase_result,
)
from .incident_evidence import mark_incident_recovered, redact_diagnostic
from .message_contract import canonical_reply_text, reply_text_hash
from .models import Binding, ReplySendClaim, RpaResult, RpaStep, Task, WechatReadTarget, WorkerProfile
from .rpa_bridge import RpaBridge
from .storage import (
    append_log,
    checkpoint_c2_action_outcomes,
    clear_c2_state,
    clear_c2_action_journal,
    enqueue_c2_outbox,
    begin_runtime_flow,
    clear_runtime_pause,
    finish_runtime_flow,
    finalize_reply_send_ack,
    discard_reply_send_intent,
    has_pending_c2_outbox,
    has_pending_c2_outbox_for_read_run_id,
    has_c2_outbox_for_read_run_id,
    has_c2_outbox_for_source_keys,
    has_c2_action_journal_for_origin_read_run_id,
    has_c2_ledger_for_origin_read_run_id,
    has_pending_reply_send_ack_outbox,
    has_pending_reply_send_ack_for_task_id,
    list_c2_outbox_waiting,
    list_c2_action_journal,
    list_c2_ledger_entries,
    list_waiting_c2_ledger_conversation_ids,
    list_reply_send_ack_outbox,
    load_c2_outbox_entry,
    load_c2_outbox_origin_read_run_ids,
    load_c2_state,
    load_runtime_control,
    load_c2_ledger_entry,
    load_reply_send_ack_outbox,
    mark_c2_ledger_ingested,
    mark_c2_ledger_rejected,
    mark_c2_outbox_attempt,
    mark_c2_outbox_capability_paused,
    mark_c2_outbox_identity_quarantined,
    mark_reply_send_ack_attempt,
    mark_reply_send_ack_confirmed,
    prepare_c2_outbox_payload,
    prune_terminal_outboxes,
    rebuild_c2_outbox_payload,
    refresh_c2_outbox_payload,
    replace_c2_outbox_with_partitions,
    save_binding,
    save_c2_ledger_terminal,
    save_c2_state,
    save_reply_send_intent,
    request_runtime_pause,
    set_c2_outbox_error,
    set_reply_send_ack_error,
    transition_c2_outbox,
    terminate_waiting_c2_image_ledger,
    update_c2_state_atomic,
)
from .telemetry import (
    StageTimer,
    abandon_buffered_running_stages,
    enqueue_c2_flow_timing_stages,
    enqueue_existing_duration,
    load_process_run,
    remember_process_run,
    schedule_stage_event_upload,
)
from .sequence_alignment import (
    align_committed_message_sequence,
    apply_inherited_worker_ids,
    build_post_action_observation_sequence,
    build_pre_action_identity_sequence,
)
from .transaction_outcomes import (
    FlowOutcomeAccumulator,
    classify_action_result,
    classify_outbox_recovery,
    merge_item_outcomes,
    transition_outbox_state,
)
from .ui_lock import UiLockError, UiLockLease, acquire_ui_lock, force_recover_stale_lock, lock_summary
from .wechat_c2 import (
    apply_image_terminal_result,
    authoritative_order_source,
    build_flow_gate_ingest_payload,
    build_message_ingest_payload,
    build_scan_result_payload,
    image_observation_source_key,
    is_formal_c2_remark_code,
    message_rect,
    order_authoritative_slots,
    project_final_slot_flow_gates,
    replayable_image_observation,
    voice_observation_source_key,
    worker_source_message_key,
)


C2ReadOperationPhase: TypeAlias = Literal[
    "authorized_read",
    "pre_send_refresh",
]
C2_AUTHORIZED_READ_PHASE: C2ReadOperationPhase = "authorized_read"
C2_PRE_SEND_REFRESH_PHASE: C2ReadOperationPhase = "pre_send_refresh"


def should_submit_c2_ingest_payload(
    *,
    read_reason: str,
    messages: list[Any] | None,
    has_flow_gate: bool,
) -> bool:
    """Return whether an opened authorized chat must be acknowledged by C2."""

    return (
        bool(messages)
        or bool(str(read_reason or "").strip())
        or has_flow_gate
    )


ENV_STOP_ERRORS = {
    "RPA_COMPONENT_NOT_READY",
    "WECHAT_WINDOW_NOT_FOUND",
    "ACCOUNT_RESTRICTED",
    "OPERATION_TOO_FREQUENT",
    "NETWORK_ERROR",
    "RPA_SIDECAR_TIMEOUT",
    "RPA_SIDECAR_PROTOCOL_INVALID",
    "RPA_SIDECAR_CRASHED",
    "WORKER_INTERRUPTED",
    "TASK_LEASE_RENEW_FAILED",
    "TASK_LEASE_EXPIRED",
    "TASK_LEASE_FENCING_STALE",
    "TASK_LEASE_OWNER_MISMATCH",
    "UI_LOCK_RENEW_FAILED",
    "OTHER",
}


def _ui_lock_cancel_reason(lease: UiLockLease | None) -> bool | str:
    if lease is None or not lease.cancel_requested():
        return False
    error = getattr(lease, "lease_error", None)
    return str(error.code if error is not None else "UI_LOCK_RENEW_FAILED")

C2_RECENT_VISIBLE_CACHE_TTL_SECONDS = 90.0
LEGACY_FOLLOW_UP_REMOVAL_CONDITION = (
    "Remove after the production database migration proves no pending/running follow_up rows remain."
)
TASK_LEASE_DEFINITIVE_LOSS_CODES = frozenset(
    {
        "TASK_NOT_FOUND",
        "TASK_LEASE_NOT_RUNNING",
        "TASK_LEASE_CLIENT_INSTANCE_REQUIRED",
        "TASK_LEASE_OWNER_MISMATCH",
        "TASK_LEASE_FENCING_STALE",
        "TASK_LEASE_EXPIRED",
    }
)

_C2_REUSED_SNAPSHOT_EVIDENCE_FIELDS = (
    "artifact_dir",
    "review_path",
    "evidence_path",
)


def _freeze_phase_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Snapshot diagnostics so later phase merges cannot rewrite history."""

    return copy.deepcopy(metadata)


def _reuse_initial_snapshot_with_locate_evidence(
    snapshot: dict[str, Any],
    locate_payload: dict[str, Any],
) -> dict[str, Any]:
    """Keep the locate run's evidence context when its message frame is reused."""

    reused = dict(snapshot)
    for field in _C2_REUSED_SNAPSHOT_EVIDENCE_FIELDS:
        if not reused.get(field) and locate_payload.get(field):
            reused[field] = locate_payload[field]
    return reused


def voice_action_journal_anchor_keys(
    observation: dict[str, Any],
) -> list[str]:
    """Carry every OmniAuto-provided stable alias into the pre-click journal."""

    source = (
        observation.get("source_message")
        if isinstance(observation.get("source_message"), dict)
        else {}
    )
    anchor = (
        source.get("voice_anchor")
        if isinstance(source.get("voice_anchor"), dict)
        else {}
    )
    values = [
        *(observation.get("_voice_action_anchor_keys") or []),
        observation.get("parent_voice_anchor_key"),
        observation.get("voice_anchor_key"),
        observation.get("voice_anchor_stable_key"),
        observation.get("voice_anchor_structural_key"),
        source.get("parent_voice_anchor_key"),
        source.get("voice_anchor_key"),
        source.get("voice_anchor_stable_key"),
        source.get("voice_anchor_structural_key"),
        anchor.get("anchor_key"),
        anchor.get("anchor_stable_key"),
        anchor.get("anchor_structural_key"),
    ]
    return sorted(
        {
            str(value).strip()
            for value in values
            if str(value or "").strip()
        }
    )


def _same_frame_voice_rect_matches(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    """Use geometry only to prove aliases in one captured frame."""

    left_rect = message_rect({"bubble_rect": left.get("bubble_rect")})
    right_rect = message_rect({"bubble_rect": right.get("bubble_rect")})
    if not left_rect or not right_rect:
        return False
    if str(left.get("sender_role") or "").strip().lower() != str(
        right.get("sender_role") or ""
    ).strip().lower():
        return False
    return all(
        abs(float(left_rect[key]) - float(right_rect[key])) <= 3.0
        for key in ("left", "top", "right", "bottom")
    )


def collapse_same_frame_voice_aliases(
    observations: list[Any],
) -> list[Any]:
    """Collapse OCR/visual aliases before allocating a business identity."""

    result: list[Any] = []
    voice_indexes: list[int] = []
    for raw in observations:
        if not isinstance(raw, dict):
            result.append(raw)
            continue
        observation = dict(raw)
        if not (
            str(observation.get("row_kind") or "").strip().lower()
            == "voice_bubble"
            and str(observation.get("voice_state") or "").strip().lower()
            == "untranscribed"
        ):
            result.append(observation)
            continue
        aliases = set(voice_action_journal_anchor_keys(observation))
        matching_index = next(
            (
                index
                for index in voice_indexes
                if aliases
                & set(
                    result[index].get("_voice_action_anchor_keys")
                    or voice_action_journal_anchor_keys(result[index])
                )
                or _same_frame_voice_rect_matches(
                    result[index], observation
                )
            ),
            None,
        )
        if matching_index is None:
            observation["_voice_action_anchor_keys"] = sorted(aliases)
            result.append(observation)
            voice_indexes.append(len(result) - 1)
            continue
        existing = dict(result[matching_index])
        existing["_voice_action_anchor_keys"] = sorted(
            {
                *voice_action_journal_anchor_keys(existing),
                *aliases,
            }
        )
        result[matching_index] = existing
    return result


def align_post_action_observations(
    pre_observations: list[Any],
    post_observations: list[Any],
    *,
    confirmed_action_mapping: dict[str, Any] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    """Use the unified sequence aligner after a frame-changing UI action."""

    committed_ids = {
        str(item.get("observation_id") or "").strip(): str(
            item.get("_worker_stable_id") or ""
        ).strip()
        for item in pre_observations
        if isinstance(item, dict)
        and str(item.get("observation_id") or "").strip()
        and str(item.get("_worker_stable_id") or "").strip()
    }
    mapping = (
        dict(confirmed_action_mapping)
        if isinstance(confirmed_action_mapping, dict)
        else {}
    )
    pre_sequence = build_pre_action_identity_sequence(
        pre_observations,
        committed_ids=committed_ids,
        selected_observation_id=str(
            mapping.get("pre_observation_id") or ""
        ),
        canonical_action_id=str(mapping.get("canonical_action_id") or ""),
        reserved_worker_stable_id=str(
            mapping.get("reserved_worker_stable_id") or ""
        ),
    )
    post_sequence = build_post_action_observation_sequence(
        post_observations,
        confirmed_action_mapping=mapping,
    )
    pre_digest = hashlib.sha256(
        json.dumps(pre_sequence, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    post_digest = hashlib.sha256(
        json.dumps(post_sequence, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    evidence = align_committed_message_sequence(
        pre_sequence,
        post_sequence,
        mapping,
        pre_sequence_source="action_frame",
        pre_frame_id=f"action-pre:{pre_digest}",
        post_frame_id=f"action-post:{post_digest}",
    )
    aligned = apply_inherited_worker_ids(post_observations, evidence)
    voice_action_summary_by_stable_id = {
        str(item.get("_worker_stable_id") or "").strip(): dict(
            item.get("_worker_voice_action_summary") or {}
        )
        for item in pre_observations
        if isinstance(item, dict)
        and str(item.get("_worker_stable_id") or "").strip()
        and isinstance(item.get("_worker_voice_action_summary"), dict)
        and item.get("_worker_voice_action_summary")
    }
    if voice_action_summary_by_stable_id:
        carried: list[Any] = []
        for raw in aligned:
            if not isinstance(raw, dict):
                carried.append(raw)
                continue
            observation = dict(raw)
            stable_id = str(
                observation.get("_worker_stable_id") or ""
            ).strip()
            summary = voice_action_summary_by_stable_id.get(stable_id)
            if summary:
                observation["_worker_voice_action_summary"] = dict(
                    summary
                )
            carried.append(observation)
        aligned = carried
    return aligned, evidence


def confirmed_voice_action_mapping(
    *,
    voice_payload: dict[str, Any],
    pre_observations: list[Any],
    post_observations: list[Any],
    canonical_action_id: str,
    reserved_worker_stable_id: str,
    expected_pre_frame_id: str,
    pre_observation_id: str,
    selected_anchor_keys: set[str],
) -> dict[str, Any]:
    """Validate the Sidecar decision without reconstructing its binding."""

    del selected_anchor_keys
    if (
        str(voice_payload.get("canonical_voice_action_id") or "").strip()
        != canonical_action_id
        or str(
            voice_payload.get("reserved_worker_stable_id") or ""
        ).strip()
        != reserved_worker_stable_id
        or str(
            voice_payload.get("transcript_binding_status") or ""
        ).strip()
        not in {"confirmed", "failed"}
        or int(voice_payload.get("binding_candidate_count") or 0) != 1
    ):
        raise ValueError("C2_VOICE_IDENTITY_CONTRACT_INVALID")

    method = str(
        voice_payload.get("transcript_binding_method") or ""
    ).strip()
    tracking_frame_ids = [
        str(value or "").strip()
        for value in (voice_payload.get("tracking_frame_ids") or [])
        if str(value or "").strip()
    ]
    tracking_edges = list(voice_payload.get("tracking_edges") or [])
    neighbor_pairs = list(
        voice_payload.get("matched_neighbor_pairs") or []
    )
    native_id = str(
        voice_payload.get("native_source_message_id") or ""
    ).strip()
    if method == "native_source_id":
        evidence_valid = bool(
            native_id
            and not tracking_frame_ids
            and not tracking_edges
            and not neighbor_pairs
        )
    elif method == "continuous_target_tracking":
        evidence_valid = bool(
            not native_id
            and not neighbor_pairs
            and len(tracking_frame_ids) >= 3
            and len(set(tracking_frame_ids))
            == len(tracking_frame_ids)
            and len(tracking_edges) == len(tracking_frame_ids) - 1
            and tracking_frame_ids[0]
            == str(expected_pre_frame_id or "").strip()
        )
        expected_observation_id = str(pre_observation_id or "").strip()
        previous_to_frame = ""
        tracking_role = ""
        for index, edge in enumerate(tracking_edges):
            if not isinstance(edge, dict):
                evidence_valid = False
                continue
            from_frame = str(edge.get("from_frame_id") or "").strip()
            from_observation = str(
                edge.get("from_observation_id") or ""
            ).strip()
            to_frame = str(edge.get("to_frame_id") or "").strip()
            to_observation = str(
                edge.get("to_observation_id") or ""
            ).strip()
            structural = edge.get("structural_evidence")
            displacement = edge.get("displacement_evidence")
            edge_role = str(edge.get("sender_role") or "").lower()
            if (
                not from_frame
                or not from_observation
                or not to_frame
                or not to_observation
                or str(edge.get("message_type") or "").lower() != "voice"
                or edge_role not in {"customer", "self"}
                or (tracking_role and edge_role != tracking_role)
                or int(edge.get("edge_candidate_count") or 0) != 1
                or not isinstance(structural, dict)
                or not structural
                or not isinstance(displacement, dict)
                or not displacement
                or from_frame != tracking_frame_ids[index]
                or to_frame != tracking_frame_ids[index + 1]
                or (index == 0 and from_observation != expected_observation_id)
                or (index > 0 and from_frame != previous_to_frame)
                or (index > 0 and from_observation != expected_observation_id)
            ):
                evidence_valid = False
            tracking_role = tracking_role or edge_role
            expected_observation_id = to_observation
            previous_to_frame = to_frame
    elif method == "neighbor_scroll_alignment":
        deltas = []
        evidence_valid = bool(
            not native_id
            and not tracking_frame_ids
            and not tracking_edges
            and len(neighbor_pairs) >= 2
        )
        for pair in neighbor_pairs:
            if not isinstance(pair, dict):
                evidence_valid = False
                continue
            if (
                not str(pair.get("pre_observation_id") or "").strip()
                or not str(
                    pair.get("post_observation_id") or ""
                ).strip()
                or str(pair.get("sender_role") or "").strip().lower()
                not in {"customer", "self"}
            ):
                evidence_valid = False
            try:
                deltas.append(float(pair["scroll_delta_y"]))
            except (KeyError, TypeError, ValueError):
                evidence_valid = False
        if deltas and max(deltas) - min(deltas) > 8.0:
            evidence_valid = False
    else:
        evidence_valid = False
    if not evidence_valid:
        raise ValueError("C2_VOICE_IDENTITY_CONTRACT_INVALID")

    mapping = voice_payload.get("confirmed_action_mapping")
    if not isinstance(mapping, dict):
        raise ValueError("C2_VOICE_IDENTITY_CONTRACT_INVALID")
    post_observation_id = str(
        mapping.get("post_observation_id") or ""
    ).strip()
    observation_ids = [
        str(item.get("observation_id") or "").strip()
        for item in post_observations
        if isinstance(item, dict)
    ]
    if (
        str(mapping.get("canonical_action_id") or "").strip()
        != canonical_action_id
        or str(
            mapping.get("reserved_worker_stable_id") or ""
        ).strip()
        != reserved_worker_stable_id
        or mapping.get("binding_confirmed") is not True
        or not post_observation_id
        or observation_ids.count(post_observation_id) != 1
        or (
            method == "continuous_target_tracking"
            and tracking_edges
            and str(tracking_edges[-1].get("to_observation_id") or "").strip()
            != post_observation_id
        )
    ):
        raise ValueError("C2_VOICE_IDENTITY_CONTRACT_INVALID")

    def unique_observation(
        observations: list[Any] | None,
        observation_id: str,
    ) -> dict[str, Any] | None:
        matches = [
            item
            for item in (observations or [])
            if isinstance(item, dict)
            and str(item.get("observation_id") or "").strip()
            == observation_id
        ]
        return matches[0] if len(matches) == 1 else None

    def observation_native_id(observation: dict[str, Any] | None) -> str:
        if not isinstance(observation, dict):
            return ""
        source = (
            observation.get("source_message")
            if isinstance(observation.get("source_message"), dict)
            else {}
        )
        return str(
            observation.get("native_source_message_id")
            or source.get("native_source_message_id")
            or source.get("native_message_id")
            or ""
        ).strip()

    if method == "native_source_id":
        pre_observation = unique_observation(
            pre_observations,
            str(pre_observation_id or "").strip(),
        )
        post_observation = unique_observation(
            post_observations,
            post_observation_id,
        )
        if (
            observation_native_id(pre_observation) != native_id
            or observation_native_id(post_observation) != native_id
            or str((pre_observation or {}).get("message_type") or "")
            .strip().lower()
            != "voice"
            or str((post_observation or {}).get("message_type") or "")
            .strip().lower()
            != "voice"
            or str((pre_observation or {}).get("sender_role") or "")
            .strip().lower()
            != str((post_observation or {}).get("sender_role") or "")
            .strip().lower()
        ):
            raise ValueError("C2_VOICE_IDENTITY_CONTRACT_INVALID")
    elif method == "continuous_target_tracking":
        pre_observation = unique_observation(
            pre_observations,
            str(pre_observation_id or "").strip(),
        )
        post_observation = unique_observation(
            post_observations,
            post_observation_id,
        )
        pre_role = str(
            (pre_observation or {}).get("sender_role") or ""
        ).strip().lower()
        post_role = str(
            (post_observation or {}).get("sender_role") or ""
        ).strip().lower()
        if (
            not pre_observation
            or not post_observation
            or pre_role not in {"customer", "self"}
            or post_role != pre_role
            or tracking_role != pre_role
            or str(pre_observation.get("message_type") or "")
            .strip().lower()
            != "voice"
            or str(post_observation.get("message_type") or "")
            .strip().lower()
            != "voice"
        ):
            raise ValueError("C2_VOICE_IDENTITY_CONTRACT_INVALID")
    elif method == "neighbor_scroll_alignment":
        pre_by_id = {
            str(item.get("observation_id") or "").strip(): item
            for item in (pre_observations or [])
            if isinstance(item, dict)
            and str(item.get("observation_id") or "").strip()
        }
        post_by_id = {
            str(item.get("observation_id") or "").strip(): item
            for item in post_observations
            if isinstance(item, dict)
            and str(item.get("observation_id") or "").strip()
        }
        measured_deltas: list[float] = []
        used_pre_ids: set[str] = set()
        used_post_ids: set[str] = set()
        for pair in neighbor_pairs:
            pre_id = str(pair.get("pre_observation_id") or "").strip()
            post_id = str(pair.get("post_observation_id") or "").strip()
            pre_item = pre_by_id.get(pre_id)
            post_item = post_by_id.get(post_id)
            pre_rect = message_rect(pre_item or {})
            post_rect = message_rect(post_item or {})
            role = str(pair.get("sender_role") or "").strip().lower()
            if (
                not pre_item
                or not post_item
                or not pre_rect
                or not post_rect
                or pre_id in used_pre_ids
                or post_id in used_post_ids
                or str(pre_item.get("sender_role") or "").strip().lower()
                != role
                or str(post_item.get("sender_role") or "").strip().lower()
                != role
            ):
                raise ValueError("C2_VOICE_IDENTITY_CONTRACT_INVALID")
            measured = float(post_rect["center_y"]) - float(
                pre_rect["center_y"]
            )
            if abs(measured - float(pair["scroll_delta_y"])) > 8.0:
                raise ValueError("C2_VOICE_IDENTITY_CONTRACT_INVALID")
            used_pre_ids.add(pre_id)
            used_post_ids.add(post_id)
            measured_deltas.append(measured)
        if (
            len(measured_deltas) < 2
            or max(measured_deltas) - min(measured_deltas) > 8.0
        ):
            raise ValueError("C2_VOICE_IDENTITY_CONTRACT_INVALID")
        expected_delta = sum(measured_deltas) / len(measured_deltas)
        selected_pre = unique_observation(
            pre_observations,
            str(pre_observation_id or "").strip(),
        )
        selected_pre_rect = message_rect(selected_pre or {})
        if not selected_pre or not selected_pre_rect:
            raise ValueError("C2_VOICE_IDENTITY_CONTRACT_INVALID")
        selected_role = str(
            selected_pre.get("sender_role") or ""
        ).strip().lower()
        projected_y = float(selected_pre_rect["center_y"]) + expected_delta
        projected_candidates = []
        for item in post_observations:
            if not isinstance(item, dict):
                continue
            item_rect = message_rect(item)
            if (
                not item_rect
                or str(item.get("message_type") or "").strip().lower()
                != "voice"
                or str(item.get("sender_role") or "").strip().lower()
                != selected_role
                or abs(float(item_rect["center_y"]) - projected_y) > 8.0
            ):
                continue
            projected_candidates.append(
                str(item.get("observation_id") or "").strip()
            )
        if projected_candidates != [post_observation_id]:
            raise ValueError("C2_VOICE_IDENTITY_CONTRACT_INVALID")
    derived_ids = [
        str(value or "").strip()
        for value in (mapping.get("derived_observation_ids") or [])
        if str(value or "").strip()
    ]
    if (
        post_observation_id in derived_ids
        or any(observation_ids.count(value) != 1 for value in derived_ids)
    ):
        raise ValueError("C2_VOICE_IDENTITY_CONTRACT_INVALID")
    return {
        "canonical_action_id": canonical_action_id,
        "reserved_worker_stable_id": reserved_worker_stable_id,
        "pre_observation_id": pre_observation_id,
        "binding_confirmed": True,
        "post_observation_id": post_observation_id,
        "derived_observation_ids": derived_ids,
        "binding_method": method,
    }


class TaskLeaseGuard:
    def __init__(
        self,
        *,
        api: WorkerApiClient,
        binding: Binding,
        task: Task,
        current_step: Callable[[], str | None],
    ) -> None:
        self.api = api
        self.binding = binding
        self.task = task
        self.current_step = current_step
        self.stop_event = threading.Event()
        self.lost_event = threading.Event()
        self.error_code: str | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.task.lease_fencing_token <= 0:
            self.error_code = "TASK_LEASE_FENCING_MISSING"
            self.lost_event.set()
            return
        self.thread = threading.Thread(
            target=self._renew_loop,
            name=f"CheJinTaskLease:{self.task.id}",
            daemon=True,
        )
        self.thread.start()

    def _renew_loop(self) -> None:
        interval = max(1.0, float(CONFIG.task_lease_renew_interval_seconds))
        while not self.stop_event.wait(interval):
            if not self._renew_once():
                return

    def _renew_once(self) -> bool:
        try:
            renewed = self.api.renew_task_lease(
                self.binding,
                self.task.id,
                current_step=self.current_step(),
            )
            self.task.lease_expires_at = renewed.lease_expires_at
            self.task.lease_fencing_token = renewed.lease_fencing_token
            return True
        except ApiError as exc:
            if exc.code in TASK_LEASE_DEFINITIVE_LOSS_CODES:
                self._mark_lost(
                    exc.code,
                    "服务端已明确拒绝当前任务租约，必须停止后续微信操作。",
                )
                return False
            return self._handle_transient_renew_failure(
                error_code=exc.code,
                error_type=type(exc).__name__,
            )
        except Exception as exc:
            return self._handle_transient_renew_failure(
                error_code="TASK_LEASE_RENEW_FAILED",
                error_type=type(exc).__name__,
            )

    def _lease_expired_locally(self) -> bool:
        value = str(self.task.lease_expires_at or "").strip()
        if not value:
            return True
        try:
            expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return True
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at.astimezone(timezone.utc) <= datetime.now(timezone.utc)

    def _handle_transient_renew_failure(
        self,
        *,
        error_code: str,
        error_type: str,
    ) -> bool:
        if self._lease_expired_locally():
            self._mark_lost(
                "TASK_LEASE_EXPIRED",
                "续租暂时失败且本地任务租约已经到期，必须停止后续微信操作。",
            )
            return False
        append_log(
            "WARN",
            "task_lease_renew_retrying",
            "任务租约续租暂时失败；租约仍有效，将继续重试。",
            task_id=self.task.id,
            error_code=error_code,
            metadata={
                "error_type": error_type,
                "lease_expires_at": self.task.lease_expires_at,
            },
        )
        return True

    def _mark_lost(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        self.lost_event.set()
        append_log(
            "ERROR",
            "task_lease_renew_failed",
            message,
            task_id=self.task.id,
            error_code=error_code,
            metadata={"lease_expires_at": self.task.lease_expires_at},
        )

    def cancel_requested(self) -> bool:
        if not self.lost_event.is_set() and self._lease_expired_locally():
            self._mark_lost(
                "TASK_LEASE_EXPIRED",
                "本地任务租约已经到期，必须停止后续微信操作。",
            )
        return self.lost_event.is_set()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)

C2_LOCATE_TERMINAL_ERROR_CODES = {
    "C2_VISIBLE_TARGET_AMBIGUOUS",
    "C2_GROUP_CHAT_NOT_ALLOWED",
    "C2_CONVERSATION_TYPE_UNKNOWN",
}

_C2_IMAGE_DIAGNOSTIC_FIELDS = {
    "phase",
    "capture_step",
    "capture_mode",
    "frame_fingerprint",
    "image_size",
    "ocr_item_count",
    "local_ocr_item_count",
    "ocr_roi",
    "ocr_execution",
    "menu_panel_bounds",
    "menu_window_evidence",
    "menu_structure_evidence",
    "local_ocr_evidence",
    "parsed_message_count",
    "screenshot_path",
    "roi_screenshot_path",
    "image_persisted",
    "image_bytes_persisted",
    "point",
    "bounds",
    "sequence_number",
    "reason",
    "reason_detail",
    "error_type",
    "provider_traceback",
    "provider",
    "base_url",
    "model",
    "request_style",
    "role",
    "role_source",
    "bubble_rect",
    "applied",
    "vision_summary_length",
    "vision_summary_sha256",
}


def _c2_text_fingerprint(value: Any) -> str:
    text = str(value or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def _safe_c2_image_transaction(value: Any) -> dict[str, Any]:
    transaction = value if isinstance(value, dict) else {}
    return {
        key: transaction.get(key)
        for key in (
            "status",
            "right_click_ok",
            "menu_copy_confirmed",
            "clipboard_sequence_changed",
            "clipboard_sequence_before",
            "clipboard_sequence_after",
            "clipboard_content_read",
            "clipboard_image_valid",
            "menu_labels",
            "menu_panel_bounds",
            "image_sha256",
            "image_width",
            "image_height",
            "slot_identity_confirmed",
            "slot_identity_evidence",
            "current_bubble_rect",
            "source",
        )
        if transaction.get(key) is not None
    }


def _safe_c2_image_diagnostic_event(value: Any) -> dict[str, Any]:
    event = value if isinstance(value, dict) else {}
    safe = {
        key: event.get(key)
        for key in ("sequence", "stage", "status", "offset_ms", "duration_ms")
        if event.get(key) is not None
    }
    safe.update(
        {
            key: event.get(key)
            for key in _C2_IMAGE_DIAGNOSTIC_FIELDS
            if event.get(key) is not None
        }
    )
    return safe


def _c2_image_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in (payload.get("messages") or [])
        if isinstance(item, dict) and str(item.get("message_type") or "").lower() == "image"
    ]


def _untranscribed_voice_observations(sidecar_payload: dict[str, Any]) -> list[dict[str, Any]]:
    observations = sidecar_payload.get("observations")
    if not isinstance(observations, list):
        return []
    return [
        item
        for item in observations
        if isinstance(item, dict)
        and item.get("row_kind") == "voice_bubble"
        and item.get("message_type") == "voice"
        and item.get("voice_state") == "untranscribed"
        and observation_role_is_trusted(item)
        and not item.get("contract_errors")
    ]


def _executable_untranscribed_voice_observations(
    target: WechatReadTarget,
    sidecar_payload: dict[str, Any],
    *,
    excluded_anchor_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return only voices that may enter the one production orchestrator.

    A trusted untranscribed row is not automatically actionable.  A stable
    identity already present in the backend checkpoint, or any local terminal
    ledger fact, proves that the physical message was settled previously.
    Frame-local anchors may suppress a voice only inside the current
    orchestrator run; they never become a cross-round identity.
    """

    excluded = {
        str(value).strip()
        for value in (excluded_anchor_keys or set())
        if str(value).strip()
    }
    checkpoint = (
        target.raw.get("identity_checkpoint")
        if isinstance(target.raw, dict)
        and isinstance(target.raw.get("identity_checkpoint"), dict)
        else {}
    )
    confirmed_stable_ids = {
        str(item.get("stable_id") or "").strip()
        for item in (checkpoint.get("recent_messages") or [])
        if isinstance(item, dict)
        and str(item.get("stable_id") or "").strip()
    }
    confirmed_source_keys = {
        str(item.get("source_message_key") or "").strip()
        for item in (checkpoint.get("recent_messages") or [])
        if isinstance(item, dict)
        and str(item.get("source_message_key") or "").strip()
    }
    executable: list[dict[str, Any]] = []
    for observation in _untranscribed_voice_observations(sidecar_payload):
        aliases = set(voice_action_journal_anchor_keys(observation))
        aliases.update(
            str(value).strip()
            for value in (
                observation.get("_voice_action_anchor_keys") or []
            )
            if str(value).strip()
        )
        if aliases & excluded:
            continue
        stable_id = str(
            observation.get("_worker_stable_id") or ""
        ).strip()
        if stable_id and stable_id in confirmed_stable_ids:
            continue
        try:
            source_key = voice_observation_source_key(target, observation)
        except ValueError:
            source_key = ""
        if source_key and source_key in confirmed_source_keys:
            continue
        ledger = (
            load_c2_ledger_entry(target.conversation_id, source_key)
            if source_key
            else None
        )
        if ledger and str(ledger.get("terminal_state") or "") in {
            "completed",
            "failed",
        }:
            continue
        if str(observation.get("item_state") or "") in {
            "completed",
            "failed",
        }:
            continue
        executable.append(observation)
    return executable


def _voice_payload_has_unbound_transcript(sidecar_payload: dict[str, Any]) -> bool:
    transcribed = sidecar_payload.get("transcribed_messages")
    if isinstance(transcribed, list) and any(isinstance(item, dict) for item in transcribed):
        return False
    new_messages = sidecar_payload.get("new_messages")
    return isinstance(new_messages, list) and any(
        isinstance(item, dict)
        and str(item.get("type") or item.get("message_type") or "").lower() in {"voice", "audio"}
        and "voice_duration_prefix_removed" in (item.get("quality_flags") or [])
        and bool(str(item.get("content_clean") or item.get("content") or "").strip())
        for item in new_messages
    )


def _voice_terminal_payload(
    voice_payload: dict[str, Any],
    *,
    anchor_keys: list[str],
    result: str,
    error_code: str | None,
) -> dict[str, Any]:
    aliases = {str(value).strip() for value in anchor_keys if str(value).strip()}
    matched_transcript: dict[str, Any] | None = None
    for item in voice_payload.get("transcribed_messages") or []:
        if not isinstance(item, dict):
            continue
        item_aliases = {
            str(item.get(key) or "").strip()
            for key in (
                "parent_voice_anchor_key",
                "voice_anchor_stable_key",
                "voice_anchor_key",
            )
            if str(item.get(key) or "").strip()
        }
        if aliases & item_aliases:
            matched_transcript = {
                key: item.get(key)
                for key in (
                    "content",
                    "content_clean",
                    "sender_role",
                    "parent_voice_anchor_key",
                    "voice_anchor_stable_key",
                    "voice_anchor_key",
                )
                if item.get(key) is not None
            }
            break
    return {
        "state": result,
        "error_code": error_code,
        "transcribed_message": matched_transcript,
    }


def _unconfirmed_voice_action_outcomes(
    *,
    source_keys: list[str] | set[str],
    roles: dict[str, str],
    error_code: str,
    voice_payload: dict[str, Any] | None = None,
    anchors: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Classify missing per-item voice evidence through the only outcome gate."""

    outcomes: list[dict[str, Any]] = []
    for source_key in sorted(
        {str(value).strip() for value in source_keys if str(value).strip()}
    ):
        anchor_key = str((anchors or {}).get(source_key) or "").strip()
        outcome = classify_action_result(
            "voice",
            {
                "error_code": str(
                    error_code or "VOICE_ITEM_ACTION_OUTCOME_MISSING"
                ),
                "evidence": {
                    "sender_role": str(roles.get(source_key) or ""),
                    "voice_anchor_key": anchor_key,
                    "item_action_outcome_missing": True,
                },
            },
            source_message_key=source_key,
        )
        evidence = dict(outcome.get("evidence") or {})
        evidence["action_kind"] = "voice"
        outcome["evidence"] = evidence
        outcome["terminal_payload"] = _voice_terminal_payload(
            voice_payload or {},
            anchor_keys=[anchor_key] if anchor_key else [],
            result=str(outcome.get("result") or "failed"),
            error_code=str(outcome.get("error_code") or "") or None,
        )
        outcomes.append(outcome)
    return outcomes


def _checkpoint_voice_outcome_in_action_journal(
    journal_path: Path | None,
    outcome: dict[str, Any],
) -> None:
    """Durably save the physical result before any identity alignment gate."""

    if journal_path is None:
        return
    source_key = str(outcome.get("source_message_key") or "").strip()
    result = str(outcome.get("result") or "").strip().lower()
    if not source_key or result not in {"completed", "failed"}:
        return
    update_action_journal_item(
        journal_path,
        source_message_key=source_key,
        action_phase=str(
            outcome.get("action_phase") or "not_attempted"
        ),
        business_state=result,
        business_result_confirmed=(result == "completed"),
        error_code=(
            str(outcome.get("error_code") or "").strip() or None
        ),
        terminal_payload=(
            dict(outcome.get("terminal_payload") or {})
            if isinstance(outcome.get("terminal_payload"), dict)
            else {"state": result}
        ),
    )


class TaskRunner:
    def __init__(
        self,
        api: WorkerApiClient,
        bridge: RpaBridge,
        *,
        on_profile: Callable[[WorkerProfile], None],
        on_status: Callable[[str], None],
        on_step: Callable[[RpaStep], None],
        on_task: Callable[[Task | None], None],
        on_result: Callable[[RpaResult | None], None],
        on_error: Callable[[str], None],
        can_pull_tasks: Callable[[], bool] | None = None,
        on_runtime_process: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.api = api
        self.bridge = bridge
        self.on_profile = on_profile
        self.on_status = on_status
        self.on_step = on_step
        self.on_task = on_task
        self.on_result = on_result
        self.on_error = on_error
        self.on_runtime_process = on_runtime_process or (lambda _event: None)
        self.can_pull_tasks = can_pull_tasks or (lambda: True)
        self.binding: Binding | None = None
        self.current_task: Task | None = None
        self._current_step: str | None = None
        self._runtime_process_context: dict[str, Any] = {}
        self._backend_inflight_flow_state: dict[str, Any] = {}
        self._restart_recovery_flow_id: str | None = None
        self.current_ui_lock: UiLockLease | None = None
        self.current_task_lease: TaskLeaseGuard | None = None
        self.last_rpa_component_status: str | None = None
        self.last_wechat_status: str | None = None
        self.last_rpa_probe_at = 0.0
        self.c2_stats: dict[str, Any] = {
            "last_scan_at": None,
            "last_scan_sessions": 0,
            "last_bound_count": 0,
            "last_message_read_at": None,
            "last_ingested_count": 0,
            "last_visible_hit_count": 0,
            "last_state_target_count": 0,
            "last_error": None,
        }
        self.visible_hit_queue: list[WechatReadTarget] = []
        self.c2_round_processed_conversation_ids: set[str] = set()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.c2_thread: threading.Thread | None = None
        self.thread_monitor: threading.Thread | None = None
        self._thread_health_lock = threading.RLock()
        self._thread_failure_reported: set[str] = set()
        self.c2_manual_scan_requested = threading.Event()
        self.task_lock = threading.Lock()
        self.reply_send_ack_lock = threading.Lock()
        self.c2_outbox_lock = threading.RLock()
        self.heartbeat_interval_seconds = CONFIG.heartbeat_interval_seconds
        self.poll_interval_seconds = CONFIG.poll_interval_seconds
        self.last_c2_scan_at = 0.0
        self.last_c2_read_at = 0.0
        self.last_c2_vision_preflight_at = 0.0
        self.c2_vision_preflight_ready = False
        self.c2_vision_preflight_signature = ""
        self.c2_read_failure_cooldowns: dict[str, float] = {}
        self.c2_read_success_cooldowns: dict[str, float] = {}
        self.c2_read_allowlist_keys: set[str] = set()
        self.c2_active_target_cache: dict[str, Any] = {}
        self.c2_last_visible_sessions: list[dict[str, Any]] = []
        self.c2_last_visible_sessions_monotonic = 0.0
        self.c2_recent_visible_hits_by_remark_code: dict[str, dict[str, Any]] = {}
        self.c2_voice_binding_blocked_authorizations: set[str] = set()
        self.c2_stop_guard_before_voice_seconds = max(0.0, float(CONFIG.c2_stop_guard_before_voice_seconds))
        self.last_artifact_cleanup_at = 0.0
        self._pending_run_status_sync: str | None = None
        self._last_run_status_sync_attempt = 0.0
        self.run_status_sync_error: str | None = None
        abandon_buffered_running_stages()

    @property
    def current_step(self) -> str | None:
        return self._current_step

    @current_step.setter
    def current_step(self, value: str | None) -> None:
        normalized = str(value or "").strip() or None
        if normalized == self._current_step:
            return
        self._current_step = normalized
        if normalized is None:
            return
        if normalized == "first_screen_session_scan":
            self._emit_runtime_process({"event": "scan_started"})
            return
        if not self._runtime_process_context:
            return
        self._emit_runtime_process(
            {
                "event": "step",
                "step": normalized,
                **self._runtime_process_context,
            }
        )

    def _emit_runtime_process(self, event: dict[str, Any]) -> None:
        """Publish presentation-only progress without affecting business work."""

        try:
            self.on_runtime_process(dict(event))
        except Exception as exc:
            append_log(
                "WARN",
                "ui_runtime_process_projection_failed",
                "当前运行过程展示更新失败；业务线程继续运行。",
                error_code="UI_RUNTIME_PROCESS_PROJECTION_FAILED",
                metadata={"exception_type": type(exc).__name__},
            )

    @staticmethod
    def _runtime_terminal_for_result(result: dict[str, Any]) -> str:
        brain_result = (
            result.get("brain_result")
            if isinstance(result.get("brain_result"), dict)
            else {}
        )
        if brain_result.get("sent") is True:
            return "reply_sent"
        conversation_state = str(
            result.get("conversation_terminal_state") or ""
        ).lower()
        if "handoff" in conversation_state or "waiting_sales" in conversation_state:
            return "handoff"
        if result.get("ok") is not True:
            return "failed"
        read_completion = (
            (result.get("result") or {}).get("read_completion")
            if isinstance(result.get("result"), dict)
            else {}
        )
        if isinstance(read_completion, dict) and str(
            read_completion.get("result") or ""
        ) in {"no_change", "empty"}:
            return "no_change"
        if brain_result and brain_result.get("sent") is not True:
            return "no_reply"
        return "completed"

    def start(self, binding: Binding) -> None:
        self.binding = binding
        persisted_flow_id = str(
            load_runtime_control().get("inflight_flow_id") or ""
        ).strip()
        self._restart_recovery_flow_id = persisted_flow_id or None
        self.stop_event.clear()
        with self._thread_health_lock:
            self._thread_failure_reported.clear()
        self._maybe_cleanup_artifacts(force=True)
        if not (self.thread and self.thread.is_alive()):
            self.thread = threading.Thread(
                target=self._run_supervised_loop,
                args=("task_runner", self._loop),
                name="CheJinWorkerTaskRunner",
                daemon=True,
            )
            self.thread.start()
        if CONFIG.c2_enabled and not (self.c2_thread and self.c2_thread.is_alive()):
            self.c2_thread = threading.Thread(
                target=self._run_supervised_loop,
                args=("c2_listener", self._c2_loop),
                name="CheJinWorkerC2Listener",
                daemon=True,
            )
            self.c2_thread.start()
        if not (self.thread_monitor and self.thread_monitor.is_alive()):
            self.thread_monitor = threading.Thread(
                target=self._run_supervised_loop,
                args=("thread_monitor", self._monitor_background_threads),
                name="CheJinWorkerThreadMonitor",
                daemon=True,
            )
            self.thread_monitor.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.c2_manual_scan_requested.set()

    def request_immediate_scan(self) -> None:
        self.c2_manual_scan_requested.set()

    def _run_supervised_loop(
        self,
        thread_kind: str,
        target: Callable[[], None],
    ) -> None:
        try:
            target()
        except BaseException as exc:
            self._handle_background_thread_failure(
                thread_kind,
                error_code="WORKER_BACKGROUND_THREAD_CRASHED",
                message=f"{thread_kind} 后台线程异常退出：{exc}",
                traceback_text="".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ),
            )
            return
        if not self.stop_event.is_set():
            self._handle_background_thread_failure(
                thread_kind,
                error_code="WORKER_BACKGROUND_THREAD_EXITED",
                message=f"{thread_kind} 后台线程意外结束。",
                traceback_text="",
            )

    def _monitor_background_threads(self) -> None:
        while not self.stop_event.wait(0.25):
            if (
                self._pending_run_status_sync == "paused"
                and not (self.thread and self.thread.is_alive())
            ):
                self._sync_pending_run_status()
            expected = {"task_runner": self.thread}
            if CONFIG.c2_enabled:
                expected["c2_listener"] = self.c2_thread
            for thread_kind, thread in expected.items():
                if thread is not None and thread.is_alive():
                    continue
                self._handle_background_thread_failure(
                    thread_kind,
                    error_code="WORKER_BACKGROUND_THREAD_NOT_ALIVE",
                    message=f"线程监控发现 {thread_kind} 已停止运行。",
                    traceback_text="",
                )

    def _handle_background_thread_failure(
        self,
        thread_kind: str,
        *,
        error_code: str,
        message: str,
        traceback_text: str,
    ) -> None:
        emergency_state = trigger_emergency_stop(
            reason=error_code,
            origin=f"thread:{thread_kind}",
        )
        with self._thread_health_lock:
            if thread_kind in self._thread_failure_reported:
                return
            self._thread_failure_reported.add(thread_kind)
        pause_error = ""
        if self.binding:
            try:
                self._apply_local_run_status("paused")
                self._pending_run_status_sync = "paused"
            except Exception as exc:
                self.binding.run_status = "paused"
                pause_error = f"{type(exc).__name__}: {exc}"
        self.c2_stats["last_error"] = error_code
        result = append_log(
            "ERROR",
            "worker_background_thread_failed",
            message,
            error_code=error_code,
            metadata={
                "thread_kind": thread_kind,
                "traceback": traceback_text,
                "automatic_pause": True,
                "pause_persist_error": pause_error,
                "emergency_stop": emergency_state,
            },
        )
        incident_id = str((result or {}).get("incident_id") or "")
        suffix = f"，故障编号 {incident_id}" if incident_id else ""
        self.on_error(f"后台线程异常，客户端已自动暂停{suffix}。")

    def _can_start_new_flow(self, binding: Binding | None = None) -> bool:
        active = binding or self.binding
        control = load_runtime_control()
        return bool(
            active
            and not self.stop_event.is_set()
            and not emergency_stop_requested()
            and active.run_status == "running"
            and control.get("pause_requested") is not True
            and not control.get("inflight_flow_id")
        )

    def _can_continue_inflight_flow(self, flow_id: str | None = None) -> bool:
        if self.stop_event.is_set() or emergency_stop_requested():
            return False
        control = load_runtime_control()
        current_id = str(control.get("inflight_flow_id") or "").strip()
        expected_id = str(flow_id or current_id).strip()
        return bool(
            current_id
            and expected_id == current_id
            and self.api.inflight_flow_id == current_id
            and str(
                self._backend_inflight_flow_state.get("flow_id") or ""
            ) == current_id
            and self._backend_inflight_flow_state.get("status")
            in {"active", "draining"}
        )

    def _start_inflight_flow(
        self,
        binding: Binding,
        *,
        flow_id: str,
        flow_kind: str,
    ) -> bool:
        control = load_runtime_control()
        existing_id = str(control.get("inflight_flow_id") or "").strip()
        if existing_id:
            if existing_id != flow_id:
                return False
            self.api.inflight_flow_id = existing_id
            return True
        if not self._can_start_new_flow(binding):
            return False
        backend_state = self.api.start_inflight_flow(
            binding,
            flow_id=flow_id,
            flow_kind=flow_kind,
        )
        self._backend_inflight_flow_state = dict(backend_state)
        try:
            begin_runtime_flow(flow_id, flow_kind)
        except Exception:
            append_log(
                "ERROR",
                "runtime_inflight_local_persist_failed",
                "后端已登记在途流程，但本地持久化失败；保留后端流程禁止继续。",
                error_code="RUNTIME_INFLIGHT_LOCAL_PERSIST_FAILED",
                metadata={"flow_id": flow_id, "flow_kind": flow_kind},
            )
            raise
        return True

    @staticmethod
    def _inflight_finish_receipt_key(flow_id: str) -> str:
        return f"inflight_finish_receipt:{str(flow_id or '').strip()}"

    @staticmethod
    def _has_physical_action_journal_for_flow(flow_id: str) -> bool:
        clean_id = str(flow_id or "").strip()
        if not clean_id:
            return False
        for _, payload in list_action_journals():
            payload_matches = clean_id in {
                str(payload.get("transaction_id") or "").strip(),
                str(payload.get("origin_read_run_id") or "").strip(),
                str(payload.get("read_run_id") or "").strip(),
            }
            items = payload.get("items")
            item_values = list(
                items.values() if isinstance(items, dict) else (items or [])
            )
            item_matches = False
            for item in item_values:
                if not isinstance(item, dict):
                    continue
                if clean_id in {
                    str(item.get("transaction_id") or "").strip(),
                    str(item.get("origin_read_run_id") or "").strip(),
                    str(item.get("read_run_id") or "").strip(),
                }:
                    item_matches = True
            if not (payload_matches or item_matches):
                continue

            # A quarantined media action intentionally has no durable message
            # identity, so it cannot produce a normal Ledger fact.  The
            # Journal remains as the no-repeat audit record and is checked on
            # the next authorized read, but the quarantine itself is already
            # a finite terminal state rather than pending transaction work.
            # Any non-terminal item keeps the normal hard barrier.
            journal_items = [
                item
                for item in item_values
                if isinstance(item, dict)
            ]
            if journal_items and all(
                str(item.get("action_phase") or "").strip()
                in {"quarantined", "cancelled_before_trigger"}
                for item in journal_items
            ):
                continue
            return True
        return False

    def _finish_inflight_flow(
        self,
        binding: Binding,
        flow_id: str,
        *,
        terminal_kind: str,
        conversation_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        if self.current_ui_lock is not None or bool(lock_summary().get("locked")):
            raise RuntimeError("RUNTIME_INFLIGHT_FINISH_BEFORE_UI_UNLOCK")
        if terminal_kind in {
            "read_confirmed",
            "failed_before_message_action",
            "read_failed_no_fact",
            "task_terminal",
        }:
            if has_pending_c2_outbox_for_read_run_id(flow_id):
                raise RuntimeError("RUNTIME_INFLIGHT_C2_OUTBOX_PENDING")
            if has_c2_action_journal_for_origin_read_run_id(flow_id):
                raise RuntimeError("RUNTIME_INFLIGHT_C2_ACTION_JOURNAL_PENDING")
            if self._has_physical_action_journal_for_flow(flow_id):
                raise RuntimeError("RUNTIME_INFLIGHT_ACTION_JOURNAL_PENDING")
        if has_c2_ledger_for_origin_read_run_id(flow_id, pending_only=True):
            raise RuntimeError("RUNTIME_INFLIGHT_C2_LEDGER_PENDING")
        if terminal_kind == "read_confirmed" and has_pending_reply_send_ack_outbox():
            raise RuntimeError("RUNTIME_INFLIGHT_SENT_ACK_PENDING")
        if terminal_kind == "failed_before_message_action" and has_c2_ledger_for_origin_read_run_id(
            flow_id
        ):
            raise RuntimeError("RUNTIME_INFLIGHT_PRE_ACTION_LEDGER_CONFLICT")
        if terminal_kind == "failed_before_message_action" and has_c2_outbox_for_read_run_id(
            flow_id
        ):
            raise RuntimeError("RUNTIME_INFLIGHT_PRE_ACTION_OUTBOX_CONFLICT")
        if terminal_kind == "failed_before_message_action" and has_pending_reply_send_ack_outbox():
            raise RuntimeError("RUNTIME_INFLIGHT_SENT_ACK_PENDING")
        if terminal_kind == "read_failed_no_fact" and has_c2_ledger_for_origin_read_run_id(
            flow_id
        ):
            raise RuntimeError("RUNTIME_INFLIGHT_NO_FACT_LEDGER_CONFLICT")
        if terminal_kind == "read_failed_no_fact" and has_c2_outbox_for_read_run_id(
            flow_id
        ):
            raise RuntimeError("RUNTIME_INFLIGHT_NO_FACT_OUTBOX_CONFLICT")
        if terminal_kind == "read_failed_no_fact" and has_pending_reply_send_ack_outbox():
            raise RuntimeError("RUNTIME_INFLIGHT_SENT_ACK_PENDING")
        if terminal_kind == "task_terminal" and has_pending_reply_send_ack_for_task_id(
            flow_id
        ):
            raise RuntimeError("RUNTIME_INFLIGHT_SENT_ACK_PENDING")
        self.api.finish_inflight_flow(
            binding,
            flow_id=flow_id,
            terminal_kind=terminal_kind,
            conversation_id=conversation_id,
            error_code=error_code,
        )
        finish_runtime_flow(flow_id)
        clear_c2_state(self._inflight_finish_receipt_key(flow_id))
        self._backend_inflight_flow_state = {}
        if self._restart_recovery_flow_id == flow_id:
            self._restart_recovery_flow_id = None

    def _finish_restart_recovery_flow_if_settled(
        self,
        binding: Binding,
    ) -> None:
        """Close a pre-restart flow only after durable replay needs no UI.

        A restart never resumes locating, clicking, media handling or sending.
        The normal transaction barrier may replay already-persisted Outbox and
        sent-ack facts; the backend remains the authority on task terminality.
        """

        flow_id = str(self._restart_recovery_flow_id or "").strip()
        if (
            not flow_id
            or self.current_task is not None
            or self.current_ui_lock is not None
            or bool(lock_summary().get("locked"))
            or not self._can_continue_inflight_flow(flow_id)
        ):
            return
        flow_kind = str(
            load_runtime_control().get("inflight_flow_kind") or ""
        ).strip()
        receipt = load_c2_state(self._inflight_finish_receipt_key(flow_id))
        terminal_kind = str(receipt.get("terminal_kind") or "").strip()
        if flow_kind in {"task", "chat_reply"}:
            terminal_kind = "task_terminal"
        if terminal_kind not in {
            "task_terminal",
            "read_confirmed",
            "failed_before_message_action",
            "read_failed_no_fact",
        }:
            return
        try:
            self._finish_inflight_flow(
                binding,
                flow_id,
                terminal_kind=terminal_kind,
                conversation_id=(
                    str(receipt.get("conversation_id") or "").strip()
                    or None
                ),
                error_code=(
                    str(receipt.get("error_code") or "").strip() or None
                ),
            )
        except Exception as exc:
            append_log(
                "INFO",
                "restart_inflight_flow_settlement_pending",
                "重启前的原流程仍有业务终态未确认；继续保持暂停且不操作微信。",
                error_code="RUNTIME_INFLIGHT_RECOVERY_PENDING",
                metadata={"flow_id": flow_id, "error": str(exc)},
            )

    def _apply_local_run_status(self, run_status: str) -> None:
        if not self.binding:
            return
        if run_status == "paused":
            request_runtime_pause()
        else:
            clear_runtime_pause()
        self.binding.run_status = run_status  # type: ignore[assignment]
        save_binding(self.binding)

    def _sync_pending_run_status(self, *, force: bool = False) -> None:
        binding = self.binding
        pending = self._pending_run_status_sync
        if not binding or not pending:
            return
        now = time.monotonic()
        if not force and now - self._last_run_status_sync_attempt < 5.0:
            return
        self._last_run_status_sync_attempt = now
        try:
            profile = self.api.set_run_status(binding, pending)
            if profile.run_status != pending:
                raise RuntimeError(
                    f"后端返回状态 {profile.run_status}，与请求状态 {pending} 不一致"
                )
        except Exception as exc:
            self.run_status_sync_error = str(exc)
            append_log(
                "WARN",
                "run_status_sync_retry_failed",
                "本地已停止接收新工作，但暂停状态尚未同步到后端；当前在途流程仍按原凭证安全收口。",
                error_code="RUN_STATUS_SYNC_FAILED",
                metadata={"requested_run_status": pending, "error": str(exc)},
            )
            return
        self._pending_run_status_sync = None
        self.run_status_sync_error = None
        self._apply_local_run_status(profile.run_status)
        self.on_profile(profile)
        append_log(
            "INFO",
            "run_status_sync_recovered",
            "接单状态已与后端重新同步。",
            metadata={"run_status": profile.run_status},
        )

    def set_run_status(self, run_status: str) -> bool:
        if not self.binding:
            return False
        if run_status not in {"running", "paused"}:
            self.on_error("接单状态无效。")
            return False
        if run_status == "running" and emergency_stop_requested():
            self.on_error("客户端已触发紧急停止，请重启后再开始接单。")
            return False
        if run_status == "paused":
            # Pause drains the registered flow. Emergency stop remains the
            # separate fail-safe for not-yet-started physical actions.
            self._apply_local_run_status("paused")
        try:
            profile = self.api.set_run_status(self.binding, run_status)
            if profile.run_status != run_status:
                raise RuntimeError(
                    f"后端返回状态 {profile.run_status}，与请求状态 {run_status} 不一致"
                )
            self._pending_run_status_sync = None
            self.run_status_sync_error = None
            self._apply_local_run_status(profile.run_status)
            self.on_profile(profile)
            self._backend_inflight_flow_state = dict(
                profile.inflight_flow_state or {}
            )
            append_log("INFO", "run_status_changed", "开始接单。" if run_status == "running" else "暂停接单。")
            return True
        except Exception as exc:
            if run_status == "paused":
                self._pending_run_status_sync = "paused"
                self.run_status_sync_error = str(exc)
                self.on_error(
                    "本地已停止接收新工作；暂停状态尚未同步到后端，"
                    "当前客户仍会安全处理完，后端状态将自动重试同步。"
                )
                append_log(
                    "WARN",
                    "run_status_pause_sync_pending",
                    "本地新工作门禁已生效，后端暂停同步失败并进入自动重试。",
                    error_code="RUN_STATUS_SYNC_FAILED",
                    metadata={"error": str(exc)},
                )
            else:
                self._apply_local_run_status("paused")
                self.on_error(f"开始接单失败，仍保持暂停：{exc}")
                append_log(
                    "WARN",
                    "run_status_start_rejected",
                    "后端未确认开始接单，本地继续保持暂停。",
                    error_code="RUN_STATUS_SYNC_FAILED",
                    metadata={"error": str(exc)},
                )
            return False

    def _loop(self) -> None:
        append_log("INFO", "client_started", "Worker 客户端任务循环启动。")
        while not self.stop_event.is_set():
            self.tick_once()
            self.stop_event.wait(self.poll_interval_seconds)

    def tick_once(self) -> None:
        binding = self.binding
        if not binding or emergency_stop_requested():
            return
        self._sync_pending_run_status()
        now = time.monotonic()
        probe_due = (
            self.last_rpa_component_status is None
            or self.last_wechat_status is None
            or now - self.last_rpa_probe_at >= max(1.0, float(self.heartbeat_interval_seconds))
        )
        ui_action_active = self.current_ui_lock is not None or bool(lock_summary().get("locked"))
        if not self.current_task and not ui_action_active:
            self._maybe_cleanup_artifacts()
        previous_wechat_status = self.last_wechat_status
        probe_performed = False
        if probe_due and not ui_action_active:
            rpa_status, wechat_status = self.bridge.probe()
            probe_performed = True
            self.last_rpa_component_status = rpa_status
            self.last_wechat_status = wechat_status
            self.last_rpa_probe_at = time.monotonic()
        else:
            rpa_status = self.last_rpa_component_status or "unavailable"
            wechat_status = self.last_wechat_status or "unknown"
        if probe_performed and wechat_status == "not_found":
            if previous_wechat_status != "not_found":
                append_log(
                    "WARN",
                    "wechat_window_missing",
                    "未检测到可用的微信桌面窗口。",
                    error_code="WECHAT_WINDOW_NOT_FOUND",
                    metadata={
                        "rpa_component_status": rpa_status,
                        "wechat_status": wechat_status,
                    },
                    force_incident=True,
                )
        elif probe_performed and wechat_status == "logged_in":
            mark_incident_recovered("wechat_window_missing")
        local_lock = lock_summary()
        vision_capability = load_c2_state("vision_preflight")
        if vision_capability:
            local_lock = {
                **local_lock,
                "capabilities": {
                    **(
                        local_lock.get("capabilities")
                        if isinstance(local_lock.get("capabilities"), dict)
                        else {}
                    ),
                    "vision": vision_capability,
                },
            }
        try:
            profile = self.api.heartbeat(
                binding,
                running_status="running" if self.current_task else "idle",
                current_task=self.current_task.id if self.current_task else None,
                rpa_component_status=rpa_status,
                wechat_status=wechat_status,
                current_step=self.current_step,
                local_lock_summary=local_lock,
            )
            if self._pending_run_status_sync == "paused":
                if profile.run_status == "paused":
                    self._pending_run_status_sync = None
                    self.run_status_sync_error = None
                else:
                    profile.run_status = "paused"
            elif profile.run_status != binding.run_status:
                # The run-status endpoint is authoritative. In particular, an
                # operator pause must not be overwritten by the next heartbeat
                # carrying a stale local "running" value.
                self._apply_local_run_status(profile.run_status)
            self._backend_inflight_flow_state = dict(
                profile.inflight_flow_state or {}
            )
            persisted_flow_id = str(
                load_runtime_control().get("inflight_flow_id") or ""
            ).strip()
            backend_flow_id = str(
                self._backend_inflight_flow_state.get("flow_id") or ""
            ).strip()
            if persisted_flow_id and persisted_flow_id == backend_flow_id:
                self.api.inflight_flow_id = persisted_flow_id
            self.on_profile(profile)
            self.on_status("online")
            mark_incident_recovered("heartbeat_failed")
        except ApiError as exc:
            if exc.status_code == 401:
                self.on_status("invalid")
                self.on_error("绑定已失效，请重新绑定。")
                append_log("ERROR", "binding_invalid", str(exc), error_code=exc.code)
                return
            self.on_status("offline")
            self.on_error(str(exc))
            append_log("ERROR", "heartbeat_failed", str(exc), error_code=exc.code)
            return
        except Exception as exc:
            self.on_status("offline")
            self.on_error(str(exc))
            append_log("ERROR", "heartbeat_failed", str(exc))
            return

        # All new WeChat work shares one durable transaction barrier. Do not
        # pull add_friend/chat_reply while message facts or sent_ack are pending.
        if (
            not ui_action_active
            and not self._worker_transaction_barrier_ready(
                binding,
                reason="heartbeat_task_pull",
            )
        ):
            return

        self._finish_restart_recovery_flow_if_settled(binding)

        if (
            self._can_start_new_flow(binding)
            and rpa_status == "ready"
            and wechat_status == "logged_in"
            and not self.current_task
            and not ui_action_active
            and self.can_pull_tasks()
        ):
            self._pull_and_execute(binding)

    def _pull_and_execute(self, binding: Binding) -> None:
        with self.task_lock:
            if not self._can_start_new_flow(binding):
                return
            if self.current_ui_lock is not None or bool(lock_summary().get("locked")):
                append_log(
                    "INFO",
                    "task_pull_deferred_by_ui_flow",
                    "微信 UI 正在执行单会话流程，服务端任务继续排队，本轮不领取。",
                )
                return
            if not self._worker_transaction_barrier_ready(
                binding,
                reason="task_pull",
            ):
                return
            try:
                mode, task, reason = self.api.pull_task(binding)
            except Exception as exc:
                append_log("ERROR", "task_pull_failed", str(exc))
                self.on_error(str(exc))
                return
            if not task:
                self.on_task(None)
                if reason and reason != "NO_PENDING_TASK":
                    self.on_error(reason)
                return
            if not self._can_start_new_flow(binding):
                return
            self._execute_task(binding, task, mode)

    def _execute_task(self, binding: Binding, task: Task, mode: str) -> None:
        flow_kind = "chat_reply" if task.task_type == "chat_reply" else "task"
        if not self._start_inflight_flow(
            binding,
            flow_id=task.id,
            flow_kind=flow_kind,
        ):
            return
        self.current_task = task
        self.on_task(task)
        self.on_result(None)
        append_log("INFO", "task_recovered" if mode == "running" else "task_pulled", f"准备执行任务 {task.id}", task_id=task.id)
        try:
            if task.task_type == "chat_reply":
                if mode == "running":
                    self._start_task_lease_guard(binding, task)
                self._execute_c2_reply_recovery(binding, task, mode)
                return
            if not self._can_continue_inflight_flow(task.id):
                return
            running_task = task if mode == "running" else self.api.claim_task(binding, task)
            if not self._can_continue_inflight_flow(task.id):
                return
            if not running_task.search_phone and task.search_phone:
                running_task.phone = task.phone
            if not running_task.wechat and task.wechat:
                running_task.wechat = task.wechat
            if not running_task.verify_message and task.verify_message:
                running_task.verify_message = task.verify_message
            if not running_task.remark_name and task.remark_name:
                running_task.remark_name = task.remark_name
            if not running_task.remark_code and task.remark_code:
                running_task.remark_code = task.remark_code
            if running_task.remark_code_valid is None and task.remark_code_valid is not None:
                running_task.remark_code_valid = task.remark_code_valid
            self.current_task = running_task
            self.on_task(running_task)
            self._start_task_lease_guard(binding, running_task)
            if running_task.task_type == "add_friend":
                self._execute_add_friend_task(binding, running_task)
                return
            if running_task.task_type == "follow_up":
                # Defensive gate for pre-migration database rows only. This is
                # not a normal dispatch path; see LEGACY_FOLLOW_UP_REMOVAL_CONDITION.
                self._handle_failed_result(
                    binding,
                    running_task,
                    RpaResult(
                        ok=False,
                        error_code="LEGACY_FOLLOW_UP_FLOW_DISABLED",
                        failure_step="task_dispatch",
                        message="旧 follow_up 发送入口已禁用；召回必须通过 C3 批次生成 chat_reply。",
                    ),
                )
                return
            result = RpaResult(ok=False, error_code="TASK_TYPE_NOT_SUPPORTED", failure_step="task_dispatch", message=f"不支持的任务类型：{running_task.task_type}")
            self._handle_failed_result(binding, running_task, result)
        except Exception as exc:
            self.on_error(str(exc))
            append_log("ERROR", "task_execute_failed", str(exc), task_id=task.id)
        finally:
            self._stop_task_lease_guard()
            self.current_step = None
            self.current_task = None
            self.on_task(None)
            try:
                self._finish_inflight_flow(
                    binding,
                    task.id,
                    terminal_kind="task_terminal",
                )
            except Exception as exc:
                append_log(
                    "ERROR",
                    "inflight_flow_finish_failed",
                    "业务终态已处理，但在途流程结束登记失败，保留持久化状态等待恢复。",
                    error_code="RUNTIME_INFLIGHT_FINISH_FAILED",
                    metadata={"flow_id": task.id, "error": str(exc)},
                )

    def _start_task_lease_guard(self, binding: Binding, task: Task) -> TaskLeaseGuard:
        self._stop_task_lease_guard()
        guard = TaskLeaseGuard(
            api=self.api,
            binding=binding,
            task=task,
            current_step=lambda: self.current_step,
        )
        self.current_task_lease = guard
        guard.start()
        return guard

    def _stop_task_lease_guard(self) -> None:
        guard = self.current_task_lease
        self.current_task_lease = None
        if guard is not None:
            guard.stop()

    def _maybe_cleanup_artifacts(self, *, force: bool = False) -> None:
        now = time.monotonic()
        interval = max(60.0, float(CONFIG.artifact_cleanup_interval_seconds))
        if not force and now - self.last_artifact_cleanup_at < interval:
            return
        self.last_artifact_cleanup_at = now
        result = None
        active_artifact_dirs: set[Path] = set()
        try:
            active_artifact_dirs = (
                self.bridge.active_artifact_dirs()
                if hasattr(self.bridge, "active_artifact_dirs")
                else set()
            )
            result = cleanup_artifacts(
                app_dir=CONFIG.app_dir,
                success_retention_days=CONFIG.artifact_success_retention_days,
                critical_retention_days=CONFIG.artifact_critical_retention_days,
                max_bytes=CONFIG.artifact_max_bytes,
                protected_paths=active_artifact_dirs,
            )
        except Exception as exc:
            append_log(
                "WARN",
                "artifact_cleanup_failed",
                "截图证据清理失败，本轮不删除任何正在使用的业务数据。",
                error_code=type(exc).__name__,
                metadata={"error_type": type(exc).__name__},
            )
        if result is not None:
            append_log(
                "INFO",
                "artifact_cleanup_completed",
                "截图证据清理完成。",
                metadata={
                    "deleted_directories": result.deleted_directories,
                    "deleted_files": result.deleted_files,
                    "released_bytes": result.released_bytes,
                    "retained_bytes": result.retained_bytes,
                    "success_retention_days": CONFIG.artifact_success_retention_days,
                    "critical_retention_days": CONFIG.artifact_critical_retention_days,
                    "max_bytes": CONFIG.artifact_max_bytes,
                    "protected_directories": len(active_artifact_dirs),
                },
            )
        try:
            outbox_result = prune_terminal_outboxes(
                retention_days=CONFIG.outbox_terminal_retention_days,
                max_terminal_rows=CONFIG.outbox_max_terminal_rows,
            )
            append_log(
                "INFO",
                "outbox_cleanup_completed",
                "本地 Outbox 终态记录清理完成，待重传记录未触碰。",
                metadata={
                    **outbox_result,
                    "retention_days": CONFIG.outbox_terminal_retention_days,
                    "max_terminal_rows": CONFIG.outbox_max_terminal_rows,
                },
            )
        except Exception as exc:
            append_log(
                "WARN",
                "outbox_cleanup_failed",
                "本地 Outbox 清理失败，未影响待重传记录。",
                error_code=type(exc).__name__,
            )

    def _execute_add_friend_task(self, binding: Binding, task: Task) -> None:
        journal_path = action_journal_path("add_friend", task.id)
        stage_timer = (
            StageTimer(
                process_run_id=task.process_run_id,
                conversation_id=None,
                stage_name="c1.add_friend_execute",
                component="worker",
                trace_id=str(uuid.uuid4()),
            )
            if task.process_run_id
            else None
        )
        try:
            result = self._run_add_friend_with_ui_lock(binding, task)
        except Exception as exc:
            if stage_timer is not None:
                stage_timer.finish(
                    status="failed",
                    error_code=type(exc).__name__,
                )
                schedule_stage_event_upload(self.api, binding)
            raise
        if stage_timer is not None:
            stage_timer.finish(
                status="succeeded" if result.ok else "failed",
                error_code=result.error_code,
            )
            schedule_stage_event_upload(self.api, binding)
        if result.ok:
            completed = (
                self.api.complete_already_friend(binding, task.id)
                if result.result_code == "already_friend"
                else self.api.complete_invite_sent(binding, task.id)
            )
            remove_action_journal(journal_path)
            if result.evidence_path or result.evidence_metadata:
                self._upload_evidence_best_effort(
                    binding,
                    task.id,
                    result.message,
                    evidence_path=result.evidence_path,
                    metadata=result.evidence_metadata,
                )
            self.on_result(result)
            append_log("INFO", "task_completed", result.message, task_id=completed.id)
            return
        self._handle_failed_result(binding, task, result)
        if (
            read_action_journal(journal_path)
            and action_journal_phase(journal_path) != "trigger_attempted"
        ):
            remove_action_journal(journal_path)

    def _handle_failed_result(self, binding: Binding, task: Task, result: RpaResult) -> None:
        failed = self.api.fail_task(binding, task.id, result.error_code or "OTHER", result.failure_step, result.message)
        self._upload_evidence_best_effort(
            binding,
            task.id,
            result.message,
            error_code=result.error_code,
            evidence_path=result.evidence_path,
            metadata=result.evidence_metadata,
        )
        log_result = append_log(
            "ERROR",
            "task_failed",
            result.message,
            task_id=failed.id,
            error_code=result.error_code,
            metadata={
                "evidence_path": result.evidence_path,
                "evidence_metadata": result.evidence_metadata or {},
            },
        )
        result.evidence_metadata = {
            **(result.evidence_metadata or {}),
            "incident_id": str((log_result or {}).get("incident_id") or ""),
            "incident_evidence_path": str((log_result or {}).get("evidence_path") or ""),
        }
        self.on_result(result)
        if result.error_code in ENV_STOP_ERRORS:
            self.set_run_status("paused")
            self.on_error("运行环境异常，已暂停接单。")

    def _settle_chat_reply_context_failure_before_unlock(
        self,
        binding: Binding,
        *,
        task_id: str,
        source_error_code: str,
    ) -> bool:
        """Make a pre-send read failure durable before C2 may scan again."""

        normalized_task_id = str(task_id or "").strip()
        normalized_source_error = str(
            source_error_code or "PRE_SEND_REFRESH_FAILED"
        ).strip()
        if not normalized_task_id:
            append_log(
                "ERROR",
                "c3_pre_send_failure_task_missing",
                "发送前复读失败，但批次没有可结算的 chat_reply 任务；已暂停接单，禁止 C2 扫描重新介入。",
                error_code="C3_REPLY_TASK_MISSING",
                metadata={"source_error_code": normalized_source_error},
                force_incident=True,
            )
            self.set_run_status("paused")
            return False
        try:
            self.api.fail_task(
                binding,
                normalized_task_id,
                "C2_REPLY_CONTEXT_RECOVERY_FAILED",
                "pre_send_refresh",
                normalized_source_error,
            )
        except Exception as exc:
            append_log(
                "ERROR",
                "c3_pre_send_failure_settlement_failed",
                "发送前复读失败后未能确认 chat_reply 终态；已暂停接单，禁止释放锁后恢复 C2 扫描。",
                task_id=normalized_task_id,
                error_code="C3_REPLY_FAILURE_SETTLEMENT_UNCONFIRMED",
                metadata={
                    "source_error_code": normalized_source_error,
                    "exception_type": type(exc).__name__,
                },
                force_incident=True,
            )
            self.set_run_status("paused")
            return False
        append_log(
            "INFO",
            "c3_pre_send_failure_settled",
            "发送前复读失败已在原 C2 单会话流程内结算，释放 UI 锁后不会留下待领取回复任务。",
            task_id=normalized_task_id,
            error_code="C2_REPLY_CONTEXT_RECOVERY_FAILED",
            metadata={"source_error_code": normalized_source_error},
        )
        return True

    def _execute_c2_reply_recovery(self, binding: Binding, task: Task, mode: str) -> None:
        if not self._c2_vision_ready_before_scan():
            append_log(
                "WARN",
                "c2_reply_recovery_blocked_by_vision_preflight",
                "Vision 全局配置未就绪，待发送回复保持任务中心状态，本轮不打开微信。",
                task_id=task.id,
                error_code="C2_VISION_NOT_READY",
            )
            return
        c3 = task.raw.get("c3") if isinstance(task.raw.get("c3"), dict) else {}
        batch = c3.get("message_batch") if isinstance(c3.get("message_batch"), dict) else {}
        action = c3.get("reply_action") if isinstance(c3.get("reply_action"), dict) else {}
        conversation_id = str(action.get("conversation_id") or batch.get("conversation_id") or "").strip()
        batch_id = str(batch.get("id") or "").strip()
        if not conversation_id or not batch_id:
            self._handle_failed_result(
                binding,
                task,
                RpaResult(
                    ok=False,
                    error_code="C2_REPLY_CONTEXT_MISSING",
                    failure_step="c2_reply_recovery",
                    message="chat_reply 缺少会话或批次上下文，禁止走旧发送路径。",
                ),
            )
            return
        try:
            batch_status = self.api.get_wechat_message_batch(binding, batch_id)
        except Exception:
            batch_status = {}
        authorization = (
            batch_status.get("authorization")
            if isinstance(batch_status.get("authorization"), dict)
            else {}
        )
        if authorization.get("allowed") is not True:
            self._handle_failed_result(
                binding,
                task,
                RpaResult(
                    ok=False,
                    error_code="C2_REPLY_TARGET_NOT_AUTHORIZED",
                    failure_step="c2_reply_recovery",
                    message="后端没有为该回复批次签发有效续行票，已停止恢复并转人工。",
                ),
            )
            return
        target = WechatReadTarget(
            conversation_id=conversation_id,
            lead_id=authorization.get("lead_id"),
            sales_id=authorization.get("sales_id"),
            rpa_session_key=str(
                authorization.get("rpa_session_key") or ""
            ),
            display_name=str(authorization.get("display_name") or ""),
            remark_code=str(authorization.get("remark_code") or "") or None,
            read_reason=str(authorization.get("read_reason") or "") or None,
            authorization_revision=(
                str(authorization.get("authorization_revision") or "")
                or None
            ),
            process_run_id=task.process_run_id,
            raw={
                "authorization_read_reason": str(
                    authorization.get("read_reason") or ""
                ),
                "batch_continuation": {
                    "batch_id": str(authorization.get("batch_id") or batch_id),
                    "token": str(
                        authorization.get("continuation_token") or ""
                    ),
                },
            },
        )
        if not self._batch_authorization_allows_target(batch_status, target):
            self._handle_failed_result(
                binding,
                task,
                RpaResult(
                    ok=False,
                    error_code="C2_REPLY_TARGET_NOT_AUTHORIZED",
                    failure_step="c2_reply_recovery",
                    message="回复批次续行票与当前 Worker、会话或授权版本不一致，已转人工。",
                ),
            )
            return
        last_authorization_check = 0.0

        def recovery_cancel_requested() -> bool | str:
            nonlocal last_authorization_check
            ui_lock_reason = _ui_lock_cancel_reason(self.current_ui_lock)
            if ui_lock_reason:
                return ui_lock_reason
            if (
                self.stop_event.is_set()
                or not self._can_continue_inflight_flow(task.id)
                or (
                    self.current_task_lease is not None
                    and self.current_task_lease.cancel_requested()
                )
            ):
                return True
            now = time.monotonic()
            if now - last_authorization_check < 1.0:
                return False
            last_authorization_check = now
            return not self._backend_still_allows_read_target_lightweight(
                binding,
                target,
            )

        owner = f"{binding.worker_id}:{binding.client_instance_id}:c2_reply:{task.id}"
        try:
            force_recover_stale_lock(reason="before_c2_reply_recovery")
            lease = acquire_ui_lock(
                operation_type="message_ingest",
                owner=owner,
                current_step="c2_reply_context_recovering",
            )
            lease.start_auto_renew()
            self.current_ui_lock = lease
            self.current_step = "c2_reply_context_recovering"
            refresh = self._read_one_wechat_target(
                binding,
                target,
                current_step="pre_send_refresh",
                operation_phase=C2_PRE_SEND_REFRESH_PHASE,
                allow_during_current_task=True,
                enforce_read_targets=True,
                held_lease=lease,
                current_only=False,
                wait_for_brain=False,
            )
            if not refresh.get("ok"):
                self._settle_chat_reply_context_failure_before_unlock(
                    binding,
                    task_id=task.id,
                    source_error_code=str(
                        refresh.get("error_code")
                        or "恢复回复任务时未能安全重建当前会话"
                    ),
                )
                self.on_result(
                    RpaResult(
                        ok=False,
                        error_code=str(refresh.get("error_code") or "PRE_SEND_REFRESH_FAILED"),
                        failure_step="pre_send_refresh",
                        message="恢复回复任务时未能安全重建当前会话，未发送。",
                        evidence_metadata={"pre_send_refresh": refresh},
                    )
                )
                return
            if int(refresh.get("new_self_message_count") or 0) > 0:
                self.on_result(
                    RpaResult(
                        ok=True,
                        result_code="chat_reply_cancelled_by_sales_message",
                        message="恢复回复任务时发现销售已回复，旧回复已取消。",
                        evidence_metadata={"pre_send_refresh": refresh},
                    )
                )
                return
            refresh_result = refresh.get("result") if isinstance(refresh.get("result"), dict) else {}
            replacement = refresh_result.get("message_batch") if isinstance(refresh_result, dict) else None
            if isinstance(replacement, dict) and replacement.get("batch_id"):
                batch_id = str(replacement["batch_id"])
            elif int(refresh.get("new_customer_message_count") or 0) > 0:
                try:
                    self.api.fail_task(
                        binding,
                        task.id,
                        "C3_REPLACEMENT_BATCH_MISSING",
                        "c2_reply_recovery",
                        "恢复回复任务时发现客户新消息，但后端未返回替代批次。",
                    )
                except Exception as report_exc:
                    append_log(
                        "ERROR",
                        "c3_replacement_batch_failure_report_failed",
                        str(report_exc),
                        task_id=task.id,
                        error_code="C3_REPLACEMENT_BATCH_MISSING",
                    )
                self.on_result(
                    RpaResult(
                        ok=False,
                        error_code="C3_REPLACEMENT_BATCH_MISSING",
                        failure_step="c2_reply_recovery",
                        message="恢复回复任务时发现客户新消息，但后端未返回替代批次，已禁止发送旧回复。",
                        evidence_metadata={"pre_send_refresh": refresh},
                    )
                )
                return
            self.current_task = task
            self.on_task(task)
            result = self._wait_and_send_current_c3_batch(
                binding=binding,
                target=target,
                batch_id=batch_id,
                cancel_check=recovery_cancel_requested,
                recovered_task=task if mode == "running" else None,
            )
            self.on_result(
                RpaResult(
                    ok=bool(result.get("ok")),
                    result_code="chat_reply_sent" if result.get("sent") else None,
                    error_code=str(result.get("error_code") or "") or None,
                    failure_step=None if result.get("ok") else "c2_reply_recovery",
                    message="C2 会话流程已恢复并发送回复。" if result.get("sent") else "C2 会话流程已完成恢复检查。",
                    evidence_metadata={"c2_reply_recovery": result},
                )
            )
        except UiLockError as exc:
            self.on_result(
                RpaResult(
                    ok=False,
                    error_code=exc.code,
                    failure_step="ui_lock_acquire",
                    message=str(exc),
                    evidence_metadata={"ui_lock": exc.data},
                )
            )
        finally:
            self._release_current_ui_lock(task_id=task.id, reason="c2_reply_recovery_finished")

    def _reply_send_ack_payload(
        self,
        *,
        send_result: str,
        action_phase: str,
        reply_text_hash: str | None,
        sidecar_run_id: str | None = None,
        evidence: dict[str, Any] | None = None,
        error_code: str | None = None,
        remark: str | None = None,
        sent_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "send_result": send_result,
            "action_phase": action_phase,
            "reply_text_hash": reply_text_hash,
            "sidecar_run_id": sidecar_run_id,
            "evidence": evidence or {},
            "error_code": error_code,
            "remark": remark,
            "sent_at": sent_at,
        }

    def _attempt_reply_send_ack(
        self,
        binding: Binding,
        record: dict[str, Any],
    ) -> bool:
        reply_action_id = str(record.get("reply_action_id") or "")
        ack_payload = (
            record.get("ack_payload")
            if isinstance(record.get("ack_payload"), dict)
            else {}
        )
        if not reply_action_id or not ack_payload:
            return False
        claim = ReplySendClaim(
            reply_action_id=reply_action_id,
            task_id=str(record.get("task_id") or ""),
            send_token=str(record.get("send_token") or ""),
            reply_text="",
            reply_text_hash=ack_payload.get("reply_text_hash"),
            conversation_id="",
            rpa_session_key="",
            expire_at=None,
        )
        mark_reply_send_ack_attempt(reply_action_id)
        try:
            self.api.sent_ack(
                binding,
                claim,
                **ack_payload,
            )
            mark_reply_send_ack_confirmed(reply_action_id)
            remove_action_journal(
                self.bridge.send_transaction_journal_path(
                    reply_action_id
                )
            )
            append_log(
                "INFO",
                "sent_ack_confirmed",
                "回复发送结果已得到后端确认。",
                task_id=claim.task_id,
                metadata={
                    "reply_action_id": reply_action_id,
                    "send_result": ack_payload.get("send_result"),
                },
            )
            return True
        except Exception as exc:
            error_code = (
                exc.code if isinstance(exc, ApiError) else type(exc).__name__
            )
            recovery_action = classify_outbox_recovery(exc)
            paused = recovery_action == "capability_paused"
            set_reply_send_ack_error(
                reply_action_id,
                error_code,
                status=(
                    "capability_paused"
                    if paused
                    else "waiting"
                ),
            )
            append_log(
                "ERROR",
                (
                    "sent_ack_capability_paused"
                    if paused
                    else "sent_ack_waiting_retry"
                ),
                str(exc),
                task_id=claim.task_id,
                error_code=error_code,
                metadata={
                    "reply_action_id": reply_action_id,
                    "send_result": ack_payload.get("send_result"),
                    "recovery_action": recovery_action,
                },
            )
            self.on_error(f"sent_ack 上报失败：{exc}")
            return False

    def _queue_and_submit_reply_send_ack(
        self,
        binding: Binding,
        claim: ReplySendClaim,
        *,
        send_result: str,
        action_phase: str,
        reply_text_hash: str | None,
        sidecar_run_id: str | None = None,
        evidence: dict[str, Any] | None = None,
        error_code: str | None = None,
        remark: str | None = None,
        sent_at: str | None = None,
    ) -> bool:
        finalize_reply_send_ack(
            reply_action_id=claim.reply_action_id,
            ack_payload=self._reply_send_ack_payload(
                send_result=send_result,
                action_phase=action_phase,
                reply_text_hash=reply_text_hash,
                sidecar_run_id=sidecar_run_id,
                evidence=evidence,
                error_code=error_code,
                remark=remark,
                sent_at=sent_at,
            ),
        )
        if send_result == "unknown":
            append_log(
                "ERROR",
                "send_result_unknown",
                "微信发送动作结果不确定，已在本地持久化禁止自动补发。",
                task_id=claim.task_id,
                error_code=error_code or "SEND_RESULT_UNKNOWN",
                metadata={
                    "reply_action_id": claim.reply_action_id,
                    "sidecar_run_id": sidecar_run_id,
                    "send_result": send_result,
                    "action_phase": action_phase,
                    "evidence": evidence or {},
                    "local_terminal_persisted": True,
                },
                force_incident=True,
            )
        record = load_reply_send_ack_outbox(claim.reply_action_id)
        return bool(record) and self._attempt_reply_send_ack(binding, record)

    def _replay_reply_send_ack_outbox(self, binding: Binding) -> bool:
        with self.reply_send_ack_lock:
            return self._replay_reply_send_ack_outbox_unlocked(binding)

    def _replay_reply_send_ack_outbox_unlocked(
        self,
        binding: Binding,
    ) -> bool:
        for record in list_reply_send_ack_outbox(limit=20):
            reply_action_id = str(record.get("reply_action_id") or "")
            if record.get("status") == "intent":
                journal_phase = self._send_transaction_journal_phase(
                    reply_action_id
                )
                if journal_phase == "not_attempted":
                    discard_reply_send_intent(reply_action_id)
                    continue
                send_result = (
                    "sent" if journal_phase == "confirmed" else "unknown"
                )
                finalize_reply_send_ack(
                    reply_action_id=reply_action_id,
                    ack_payload=self._reply_send_ack_payload(
                        send_result=send_result,
                        action_phase=journal_phase,
                        reply_text_hash=record.get("reply_text_hash"),
                        error_code=(
                            None
                            if send_result == "sent"
                            else "SEND_INTERRUPTED_BEFORE_RESULT_PERSISTED"
                        ),
                        remark=(
                            "根据 Sidecar 物理动作日志恢复发送结果，"
                            "不会重复操作微信。"
                        ),
                    ),
                )
                record = load_reply_send_ack_outbox(reply_action_id) or record
            if not self._attempt_reply_send_ack(binding, record):
                return False
        return not has_pending_reply_send_ack_outbox()

    def _send_transaction_journal_phase(
        self,
        reply_action_id: str,
    ) -> str:
        return action_journal_phase(
            self.bridge.send_transaction_journal_path(
                reply_action_id
            )
        )

    def _c2_sent_ack_barrier_ready(
        self,
        binding: Binding,
        *,
        reason: str,
    ) -> bool:
        ready = self._replay_reply_send_ack_outbox(binding)
        if not ready:
            append_log(
                "INFO",
                "c2_scan_blocked_by_pending_sent_ack",
                "AI 回复已在微信发送，但后端回执尚未确认；禁止扫描该气泡并误判为人工消息。",
                error_code="C3_SENT_ACK_PENDING",
                metadata={"reason": reason},
            )
        return ready

    def _worker_transaction_barrier_ready(
        self,
        binding: Binding,
        *,
        reason: str,
        allowed_image_recovery_conversation_id: str = "",
    ) -> bool:
        """Recover durable work before authorizing any new WeChat UI action."""

        if not self._c2_sent_ack_barrier_ready(
            binding,
            reason=reason,
        ):
            return False
        if not self._replay_c2_outbox(binding):
            append_log(
                "INFO",
                "worker_transaction_barrier_blocked_by_message_outbox",
                "上一批消息事实尚未得到后端确认，禁止领取任务或执行新的微信动作。",
                error_code="C2_OUTBOX_PENDING",
                metadata={"reason": reason},
            )
            return False
        if (
            has_pending_reply_send_ack_outbox()
            or has_pending_c2_outbox()
        ):
            append_log(
                "INFO",
                "worker_transaction_barrier_pending",
                "持久化事务仍在退避或能力暂停中，禁止新的微信动作。",
                error_code="WORKER_TRANSACTION_BARRIER_PENDING",
                metadata={"reason": reason},
            )
            return False
        self._discard_not_attempted_image_action_journals()
        pending_image_conversations = (
            self._pending_image_recovery_conversation_ids()
        )
        allowed_recovery = str(
            allowed_image_recovery_conversation_id or ""
        ).strip()
        if pending_image_conversations and (
            not allowed_recovery
            or allowed_recovery != pending_image_conversations[0]
        ):
            append_log(
                "INFO",
                "worker_transaction_barrier_pending_image_fact",
                "图片事实尚未得到后端确认，只允许恢复最早的原会话。",
                error_code="C2_IMAGE_FACT_PENDING",
                metadata={
                    "reason": reason,
                    "conversation_id": pending_image_conversations[0],
                    "pending_conversation_count": len(
                        pending_image_conversations
                    ),
                },
            )
            return False
        return True

    @staticmethod
    def _discard_not_attempted_image_action_journals() -> int:
        removed_count = 0
        journal_entries = sorted(
            list_action_journals(action_kinds=("image",)),
            key=lambda entry: (
                str(entry[1].get("created_at") or ""),
                str(entry[1].get("conversation_id") or ""),
            ),
        )
        for path, payload in journal_entries:
            if TaskRunner._strict_not_attempted_journal_can_be_removed(
                payload
            ):
                try:
                    remove_action_journal(path)
                except OSError as exc:
                    append_log(
                        "WARN",
                        "c2_image_not_attempted_journal_remove_failed",
                        "未执行图片动作日志暂时无法清理，继续保持全局门禁。",
                        error_code="C2_IMAGE_JOURNAL_REMOVE_FAILED",
                        metadata={
                            "conversation_id": str(
                                payload.get("conversation_id") or ""
                            ),
                            "error_type": type(exc).__name__,
                        },
                    )
                else:
                    append_log(
                        "INFO",
                        "c2_image_not_attempted_journal_removed",
                        "图片动作日志证明尚未右键，已在全局门禁前安全清理。",
                        metadata={
                            "conversation_id": str(
                                payload.get("conversation_id") or ""
                            ),
                            "transaction_id": str(
                                payload.get("transaction_id") or ""
                            ),
                        },
                    )
                    removed_count += 1
        return removed_count

    @staticmethod
    def _strict_not_attempted_journal_can_be_removed(
        payload: dict[str, Any],
    ) -> bool:
        """Allow pure intents to settle only when no durable work remains.

        A legacy producer could leave an empty ``not_attempted`` journal even
        after the same source fact reached a confirmed Ledger/Outbox. That
        residue is safe to remove; waiting/retry facts remain a hard barrier.
        """

        if not action_journal_is_strictly_not_attempted(payload):
            return False
        conversation_id = str(
            payload.get("conversation_id") or ""
        ).strip()
        items = (
            payload.get("items")
            if isinstance(payload.get("items"), dict)
            else {}
        )
        for raw_source_key in items:
            source_key = str(raw_source_key or "").strip()
            if not source_key:
                continue
            ledger = load_c2_ledger_entry(
                conversation_id,
                source_key,
            )
            if ledger:
                if (
                    str(ledger.get("terminal_state") or "")
                    in {"completed", "failed"}
                    and str(ledger.get("ingest_state") or "")
                    == "confirmed"
                ):
                    continue
                return False
            if has_c2_outbox_for_source_keys(
                conversation_id,
                {source_key},
            ):
                return False
        return True

    @staticmethod
    def _pending_image_recovery_conversation_ids() -> list[str]:
        ordered = list_waiting_c2_ledger_conversation_ids(
            message_type="image",
        )
        seen = set(ordered)
        journal_entries = sorted(
            list_action_journals(action_kinds=("image",)),
            key=lambda entry: (
                str(entry[1].get("created_at") or ""),
                str(entry[1].get("conversation_id") or ""),
            ),
        )
        for _path, payload in journal_entries:
            conversation_id = str(
                payload.get("conversation_id") or ""
            ).strip()
            if conversation_id and conversation_id not in seen:
                ordered.append(conversation_id)
                seen.add(conversation_id)
        return ordered

    def _send_evidence(self, payload: dict[str, Any], *, target: str) -> dict[str, Any]:
        send_baseline = (
            payload.get("send_baseline")
            if isinstance(payload.get("send_baseline"), dict)
            else {}
        )
        baseline_guard = (
            send_baseline.get("send_context_guard")
            if isinstance(send_baseline.get("send_context_guard"), dict)
            else {}
        )
        return {
            "adapter": payload.get("adapter"),
            "state": payload.get("state"),
            "target": target,
            "window_probe": payload.get("window_probe"),
            "send_result": payload.get("send_result"),
            "guard": payload.get("guard"),
            "context_validation": payload.get("context_validation"),
            "send_baseline": {
                "screenshot_path": send_baseline.get("screenshot_path"),
                "message_region_sha256": baseline_guard.get(
                    "message_region_sha256"
                ),
                "sequence_sha256": baseline_guard.get("sequence_sha256"),
                "message_count": baseline_guard.get("message_count"),
            },
            "timing": payload.get("timing"),
            "stdout_tail": payload.get("stdout_tail") or payload.get("stdout"),
            "stderr_tail": payload.get("stderr_tail") or payload.get("stderr"),
            "returncode": payload.get("returncode"),
        }

    def _reply_text_hash(self, value: str) -> str:
        return reply_text_hash(value)

    def _canonical_reply_text(self, value: str) -> str:
        return canonical_reply_text(value)

    def _utc_now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _send_identity_frame(
        self,
        sidecar_payload: dict[str, Any],
    ) -> dict[str, Any]:
        frame_token = str(
            sidecar_payload.get("frame_id")
            or sidecar_payload.get("sidecar_run_id")
            or sidecar_payload.get("run_id")
            or ""
        ).strip()
        observations = [
            dict(item)
            for item in (sidecar_payload.get("observations") or [])
            if isinstance(item, dict)
        ]
        if not frame_token or not observations:
            return {}
        return {
            "contract_revision": contract_revision(),
            "frame_id": f"send-pre:{frame_token}",
            "observations": observations,
        }

    def _record_possible_ai_send(
        self,
        *,
        target: WechatReadTarget,
        reply_action_id: str,
        reply_text_hash: str,
        reserved_worker_stable_id: str,
        pre_frame_id: str,
        pre_action_identity_sequence: list[dict[str, Any]],
    ) -> None:
        """Persist the exact pre-send identity frame before any send trigger."""

        action_id = str(reply_action_id or "").strip()
        reserved_id = str(reserved_worker_stable_id or "").strip()
        frame_id = str(pre_frame_id or "").strip()
        if not action_id or not reserved_id or not frame_id:
            raise ValueError("AI_REPLY_IDENTITY_RESERVATION_INCOMPLETE")
        if not isinstance(pre_action_identity_sequence, list):
            raise ValueError("AI_REPLY_PRE_SEQUENCE_MISSING")
        state_key = f"possible_ai_sends:{target.conversation_id}"

        def persist(previous: dict[str, Any]):
            sends = [
                dict(item)
                for item in (previous.get("sends") or [])
                if isinstance(item, dict)
                and str(item.get("reply_action_id") or "").strip()
                != action_id
            ]
            sends.append(
                {
                    "reply_action_id": action_id,
                    "reply_text_hash": str(reply_text_hash or "").strip(),
                    "reserved_worker_stable_id": reserved_id,
                    "pre_frame_id": frame_id,
                    "pre_action_identity_sequence": json.loads(
                        json.dumps(
                            pre_action_identity_sequence,
                            ensure_ascii=False,
                        )
                    ),
                    "armed_at": self._utc_now_iso(),
                    "physical_send_possible": True,
                    "reconciliation_state": "armed_before_trigger",
                }
            )
            try:
                previous_version = int(previous.get("version") or 0)
            except (TypeError, ValueError):
                previous_version = 0
            state = dict(previous)
            state.update(
                {
                    "version": max(2, previous_version),
                    "sends": sends,
                    "updated_at": self._utc_now_iso(),
                }
            )
            return state, None

        update_c2_state_atomic(state_key, persist)

    def _mark_possible_ai_send_alignment(
        self,
        *,
        conversation_id: str,
        reply_action_id: str,
        reconciliation_state: str,
        sequence_alignment_evidence: dict[str, Any] | None = None,
        physical_send_confirmed: bool = False,
    ) -> None:
        state_key = f"possible_ai_sends:{conversation_id}"

        def mark(previous: dict[str, Any]):
            sends: list[dict[str, Any]] = []
            for raw in previous.get("sends") or []:
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                if (
                    str(item.get("reply_action_id") or "").strip()
                    == reply_action_id
                ):
                    item["reconciliation_state"] = reconciliation_state
                    item["physical_send_possible"] = True
                    item["physical_send_confirmed"] = bool(
                        physical_send_confirmed
                    )
                    if isinstance(sequence_alignment_evidence, dict):
                        item["sequence_alignment_evidence"] = json.loads(
                            json.dumps(
                                sequence_alignment_evidence,
                                ensure_ascii=False,
                            )
                        )
                    item["updated_at"] = self._utc_now_iso()
                sends.append(item)
            state = dict(previous)
            state["sends"] = sends
            state["updated_at"] = self._utc_now_iso()
            return state, None

        update_c2_state_atomic(state_key, mark)

    def _clear_possible_ai_send(
        self,
        *,
        conversation_id: str,
        reply_action_id: str,
    ) -> None:
        state_key = f"possible_ai_sends:{conversation_id}"

        def clear(previous: dict[str, Any]):
            state = dict(previous)
            state["sends"] = [
                dict(item)
                for item in (previous.get("sends") or [])
                if isinstance(item, dict)
                and str(item.get("reply_action_id") or "").strip()
                != reply_action_id
            ]
            state["updated_at"] = self._utc_now_iso()
            return state, None

        update_c2_state_atomic(state_key, clear)

    def _attach_possible_ai_send_receipts(
        self,
        *,
        target: WechatReadTarget,
        observations: list[Any],
    ) -> list[Any]:
        """Never rehang an uncertain send using text, position, or snapshots."""

        del target
        return [
            dict(item) if isinstance(item, dict) else item
            for item in observations
        ]

    def _record_confirmed_ai_reply_receipt(
        self,
        *,
        target: WechatReadTarget,
        reply_action_id: str,
        reply_text_hash: str,
        sidecar_result: dict[str, Any],
        confirmed_at: str,
    ) -> bool:
        possible_state = load_c2_state(
            f"possible_ai_sends:{target.conversation_id}"
        )
        possible = next(
            (
                dict(item)
                for item in (possible_state.get("sends") or [])
                if isinstance(item, dict)
                and str(item.get("reply_action_id") or "").strip()
                == reply_action_id
            ),
            None,
        )
        if not possible:
            return False
        reserved_id = str(
            possible.get("reserved_worker_stable_id") or ""
        ).strip()
        pre_frame_id = str(possible.get("pre_frame_id") or "").strip()
        pre_sequence = [
            dict(item)
            for item in (possible.get("pre_action_identity_sequence") or [])
            if isinstance(item, dict)
        ]
        if not reserved_id or not pre_frame_id or not pre_sequence:
            return False
        send_result = (
            sidecar_result.get("send_result")
            if isinstance(sidecar_result.get("send_result"), dict)
            else {}
        )
        confirmation = (
            send_result.get("sent_confirmation")
            if isinstance(send_result.get("sent_confirmation"), dict)
            else {}
        )
        if (
            send_result.get("confirmed") is not True
            or str(send_result.get("result") or "").strip() != "sent"
            or confirmation.get("ok") is not True
        ):
            return False
        snapshot = (
            confirmation.get("snapshot")
            if isinstance(confirmation.get("snapshot"), dict)
            else {}
        )
        confirmed_observation = (
            confirmation.get("confirmed_observation")
            if isinstance(confirmation.get("confirmed_observation"), dict)
            else {}
        )
        observations = [
            dict(item)
            for item in (snapshot.get("observations") or [])
            if isinstance(item, dict)
        ]
        confirmed_observation_id = str(
            confirmed_observation.get("observation_id") or ""
        ).strip()
        confirmed_matches = [
            item
            for item in observations
            if str(item.get("observation_id") or "").strip()
            == confirmed_observation_id
        ]
        if (
            not observations
            or not confirmed_observation_id
            or len(confirmed_matches) != 1
            or str(confirmed_matches[0].get("sender_role") or "")
            .strip()
            .lower()
            != "self"
            or str(confirmed_matches[0].get("message_type") or "text")
            .strip()
            .lower()
            != "text"
        ):
            return False
        mapping = {
            "canonical_action_id": reply_action_id,
            "reserved_worker_stable_id": reserved_id,
            "post_observation_id": confirmed_observation_id,
            "binding_confirmed": True,
            "action_appended": True,
        }
        post_sequence = build_post_action_observation_sequence(
            observations,
            confirmed_action_mapping=mapping,
        )
        run_id = str(
            sidecar_result.get("sidecar_run_id")
            or sidecar_result.get("run_id")
            or ""
        ).strip()
        try:
            confirmation_attempt = int(confirmation.get("attempt") or 1)
        except (TypeError, ValueError):
            confirmation_attempt = 1
        post_frame_id = (
            f"send-post:{run_id or reply_action_id}:"
            f"{confirmation_attempt}"
        )
        try:
            evidence = align_committed_message_sequence(
                pre_sequence,
                post_sequence,
                mapping,
                pre_sequence_source="action_frame",
                pre_frame_id=pre_frame_id,
                post_frame_id=post_frame_id,
            )
        except (TypeError, ValueError) as exc:
            evidence = {
                "pre_sequence_source": "action_frame",
                "pre_frame_id": pre_frame_id,
                "post_frame_id": post_frame_id,
                "alignment_status": "unresolved",
                "candidate_alignment_count": 0,
                "matched_pairs": [],
                "old_tail_fully_consumed": False,
                "new_suffix_observation_ids": [],
                "error_code": "AI_REPLY_SEQUENCE_ALIGNMENT_INVALID",
                "error": repr(exc),
            }
        new_suffix = [
            str(value or "").strip()
            for value in (evidence.get("new_suffix_observation_ids") or [])
            if str(value or "").strip()
        ]
        if (
            evidence.get("alignment_status") != "unique"
            or evidence.get("old_tail_fully_consumed") is not True
            or not new_suffix
            or new_suffix[0] != confirmed_observation_id
        ):
            # The immediate Sidecar confirmation proves that this exact send
            # action produced one unique bottom-most self bubble while the
            # input became empty.  A newly appended bubble can clip the old
            # viewport, and media-only history cannot prove full alignment.
            # Commit only the action's reserved identity here.  Historical
            # rows are deliberately not inherited from text, coordinates, or
            # an old frame; the next authorized read must align them normally.
            post_index = next(
                index
                for index, item in enumerate(post_sequence)
                if str(item.get("post_observation_id") or "").strip()
                == confirmed_observation_id
            )
            visible_prefix_ids = [
                str(item.get("post_observation_id") or "").strip()
                for item in post_sequence[:post_index]
            ]
            pre_observation_ids = [
                str(item.get("pre_observation_id") or "").strip()
                for item in pre_sequence
            ]
            prefix_is_unchanged_pre_suffix = bool(
                all(visible_prefix_ids)
                and len(visible_prefix_ids) <= len(pre_observation_ids)
                and visible_prefix_ids
                == pre_observation_ids[-len(visible_prefix_ids):]
            )
            if not prefix_is_unchanged_pre_suffix:
                self._mark_possible_ai_send_alignment(
                    conversation_id=target.conversation_id,
                    reply_action_id=reply_action_id,
                    reconciliation_state=(
                        "identity_unconfirmed_possible_sent"
                    ),
                    sequence_alignment_evidence=evidence,
                    physical_send_confirmed=True,
                )
                return False
            history_alignment = dict(evidence)
            evidence = {
                "pre_sequence_source": "action_frame",
                "pre_frame_id": pre_frame_id,
                "post_frame_id": post_frame_id,
                "alignment_status": "unique",
                "candidate_alignment_count": 1,
                "matched_pairs": [
                    {
                        "identity_state": "selected_action",
                        "worker_stable_id": reserved_id,
                        "pre_observation_id": reply_action_id,
                        "post_observation_id": confirmed_observation_id,
                        "pre_index": len(pre_sequence),
                        "post_index": post_index,
                        "match_basis": "confirmed_action",
                    }
                ],
                "old_tail_fully_consumed": False,
                "new_suffix_observation_ids": [
                    confirmed_observation_id
                ],
                "confirmed_action_mapping": dict(mapping),
                "action_identity_only": True,
                "history_alignment_evidence": history_alignment,
            }
        journal_path = self.bridge.send_transaction_journal_path(
            reply_action_id
        )
        try:
            record_action_sequence_alignment(journal_path, evidence)
        except (OSError, ValueError):
            self._mark_possible_ai_send_alignment(
                conversation_id=target.conversation_id,
                reply_action_id=reply_action_id,
                reconciliation_state="identity_journal_unconfirmed_possible_sent",
                sequence_alignment_evidence=evidence,
                physical_send_confirmed=True,
            )
            return False
        confirmed_sequence_item = next(
            item
            for item in post_sequence
            if str(item.get("post_observation_id") or "").strip()
            == confirmed_observation_id
        )
        receipt = {
            "reply_action_id": reply_action_id,
            "reply_text_hash": reply_text_hash,
            "worker_stable_id": reserved_id,
            "confirmed_at": confirmed_at,
            "native_source_message_id": str(
                confirmed_sequence_item.get("native_source_message_id") or ""
            ).strip(),
            "canonical_visual_id": str(
                confirmed_sequence_item.get("canonical_visual_id") or ""
            ).strip(),
            "sequence_alignment_evidence": evidence,
        }
        state_key = f"message_identity:{target.conversation_id}"

        def commit(previous: dict[str, Any]):
            state = dict(previous)
            try:
                current_version = int(state.get("version") or 0)
                current_next = int(state.get("next_sequence") or 1)
                reserved_match = re.fullmatch(
                    r"worker-message-(\d+)", reserved_id
                )
                if reserved_match is None:
                    return previous, False
                reserved_number = int(reserved_match.group(1))
            except (TypeError, ValueError):
                return previous, False
            reservations = dict(state.get("sequence_reservations") or {})
            reservation_key = f"send-action:{reply_action_id}"
            if str(reservations.get(reservation_key) or "").strip() != reserved_id:
                return previous, False
            commits = dict(state.get("sequence_commits") or {})
            committed = str(commits.get(reply_action_id) or "").strip()
            if committed and committed != reserved_id:
                return previous, False
            receipts = [
                dict(item)
                for item in (state.get("ai_reply_receipts") or [])
                if isinstance(item, dict)
                and str(item.get("reply_action_id") or "").strip()
                != reply_action_id
            ]
            receipts.append(receipt)
            commits[reply_action_id] = reserved_id
            state.update(
                {
                    "version": max(4, current_version),
                    "next_sequence": max(current_next, reserved_number + 1),
                    "sequence_reservations": reservations,
                    "sequence_commits": commits,
                    "ai_reply_receipts": receipts,
                    "updated_at": self._utc_now_iso(),
                }
            )
            return state, True

        committed = update_c2_state_atomic(state_key, commit)
        if not committed:
            self._mark_possible_ai_send_alignment(
                conversation_id=target.conversation_id,
                reply_action_id=reply_action_id,
                reconciliation_state="identity_commit_conflict_possible_sent",
                sequence_alignment_evidence=evidence,
                physical_send_confirmed=True,
            )
            return False
        try:
            commit_action_journal_item_identity(
                journal_path,
                journal_item_id=reply_action_id,
                source_message_key=worker_source_message_key(
                    target,
                    identity_kind="worker_sequence",
                    identity=reserved_id,
                ),
            )
        except (OSError, ValueError) as exc:
            append_log(
                "WARN",
                "ai_reply_identity_journal_commit_deferred",
                "AI 回复身份已原子提交；动作日志提交延后，不会降级编号或触发重发。",
                error_code="AI_REPLY_IDENTITY_JOURNAL_COMMIT_DEFERRED",
                metadata={
                    "conversation_id": target.conversation_id,
                    "reply_action_id": reply_action_id,
                    "error": repr(exc),
                },
            )
        return True

    def _reserve_worker_sequence(
        self,
        target: WechatReadTarget,
        *,
        reservation_key: str,
    ) -> str:
        checkpoint = (
            target.raw.get("identity_checkpoint")
            if isinstance(target.raw, dict)
            and isinstance(target.raw.get("identity_checkpoint"), dict)
            else {}
        )
        try:
            server_floor = max(
                1, int(checkpoint.get("next_sequence_floor") or 1)
            )
        except (TypeError, ValueError):
            server_floor = 1
        # Defend against a stale or partially projected checkpoint.  The
        # declared floor is authoritative only when it is above every
        # committed Worker sequence included in that same response; otherwise
        # allocating it would reuse an existing durable identity.
        recent_floor = 1
        for item in (checkpoint.get("recent_messages") or []):
            if not isinstance(item, dict):
                continue
            match = re.fullmatch(
                r"worker-message-(\d+)",
                str(item.get("stable_id") or "").strip(),
            )
            if match is not None:
                recent_floor = max(recent_floor, int(match.group(1)) + 1)
        server_floor = max(server_floor, recent_floor)

        def reserve(previous: dict[str, Any]):
            state = dict(previous)
            try:
                state_version = int(state.get("version") or 0)
            except (TypeError, ValueError):
                state_version = 0
            reservations = dict(state.get("sequence_reservations") or {})
            existing = str(reservations.get(reservation_key) or "").strip()
            if existing:
                return state, existing
            try:
                next_sequence = max(
                    server_floor, int(state.get("next_sequence") or 1)
                )
            except (TypeError, ValueError):
                next_sequence = server_floor
            stable_id = f"worker-message-{next_sequence}"
            reservations[reservation_key] = stable_id
            state.update(
                {
                    "version": max(4, state_version),
                    "next_sequence": next_sequence + 1,
                    "sequence_reservations": reservations,
                }
            )
            return state, stable_id

        return update_c2_state_atomic(
            f"message_identity:{target.conversation_id}", reserve
        )

    def _align_initial_identity_frame(
        self,
        *,
        target: WechatReadTarget,
        sidecar_payload: dict[str, Any],
        read_run_id: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Align the first frame with the authoritative backend checkpoint."""

        prepared = dict(sidecar_payload)
        observations = collapse_same_frame_voice_aliases(
            list(prepared.get("observations") or [])
        )
        checkpoint = (
            target.raw.get("identity_checkpoint")
            if isinstance(target.raw, dict)
            and isinstance(target.raw.get("identity_checkpoint"), dict)
            else {}
        )
        if int(checkpoint.get("version") or 0) != 2:
            return prepared, [
                {
                    "error_code": "MESSAGE_IDENTITY_CHECKPOINT_MISSING",
                    "reason": "authoritative_checkpoint_missing",
                }
            ]
        recent = [
            item
            for item in (checkpoint.get("recent_messages") or [])
            if isinstance(item, dict)
        ]
        pre_sequence: list[dict[str, Any]] = []
        for index, item in enumerate(recent):
            stable_id = str(item.get("stable_id") or "").strip()
            if not stable_id:
                continue
            pre_sequence.append(
                {
                    "identity_state": "committed",
                    "worker_stable_id": stable_id,
                    "pre_observation_id": str(
                        item.get("source_message_key") or f"checkpoint-{index}"
                    ),
                    "pre_sequence_index": index,
                    "sender_role": str(item.get("sender_role") or "").strip(),
                    "message_type": str(item.get("message_type") or "").strip(),
                    "normalized_content_hash": str(
                        item.get("normalized_content_hash") or ""
                    ).strip(),
                    "native_source_message_id": str(
                        item.get("native_source_message_id") or ""
                    ).strip(),
                    "canonical_visual_id": str(
                        item.get("canonical_visual_id") or ""
                    ).strip(),
                }
            )
        checkpoint_stable_ids = {
            str(item.get("worker_stable_id") or "").strip()
            for item in pre_sequence
            if str(item.get("worker_stable_id") or "").strip()
        }
        identity_state = load_c2_state(
            f"message_identity:{target.conversation_id}"
        )
        ai_reply_boundary = (
            target.raw.get("ai_reply_boundary")
            if isinstance(target.raw, dict)
            and isinstance(target.raw.get("ai_reply_boundary"), dict)
            else {}
        )
        boundary_action_id = str(
            ai_reply_boundary.get("reply_action_id") or ""
        ).strip()
        boundary_hash = str(
            ai_reply_boundary.get("reply_text_hash") or ""
        ).strip()
        boundary_stable_id = str(
            ai_reply_boundary.get("worker_stable_id") or ""
        ).strip()
        for receipt in identity_state.get("ai_reply_receipts") or []:
            if not isinstance(receipt, dict):
                continue
            stable_id = str(
                receipt.get("worker_stable_id") or ""
            ).strip()
            stable_match = re.fullmatch(r"worker-message-(\d+)", stable_id)
            if (
                stable_match is None
                or stable_id in checkpoint_stable_ids
                or not str(
                    receipt.get("reply_action_id") or ""
                ).strip()
                or not str(
                    receipt.get("reply_text_hash") or ""
                ).strip()
            ):
                continue
            pre_sequence.append(
                {
                    "identity_state": "committed",
                    "worker_stable_id": stable_id,
                    "pre_observation_id": (
                        "confirmed-ai-reply:"
                        f"{receipt['reply_action_id']}"
                    ),
                    "pre_sequence_index": len(pre_sequence),
                    "sender_role": "self",
                    "message_type": "text",
                    "normalized_content_hash": str(
                        receipt.get("reply_text_hash") or ""
                    ).strip(),
                    "native_source_message_id": str(
                        receipt.get("native_source_message_id") or ""
                    ).strip(),
                    "canonical_visual_id": str(
                        receipt.get("canonical_visual_id") or ""
                    ).strip(),
                    "ai_reply_boundary": bool(
                        boundary_action_id
                        and boundary_hash
                        and str(receipt.get("reply_action_id") or "").strip()
                        == boundary_action_id
                        and str(receipt.get("reply_text_hash") or "").strip()
                        == boundary_hash
                    ),
                    "_worker_sequence_number": int(
                        stable_match.group(1)
                    ),
                }
            )
            checkpoint_stable_ids.add(stable_id)
        if (
            boundary_action_id
            and boundary_hash
            and boundary_stable_id
            and boundary_stable_id not in checkpoint_stable_ids
        ):
            stable_match = re.fullmatch(
                r"worker-message-(\d+)", boundary_stable_id
            )
            if stable_match is not None:
                pre_sequence.append(
                    {
                        "identity_state": "committed",
                        "worker_stable_id": boundary_stable_id,
                        "pre_observation_id": (
                            f"sent-ack-boundary:{boundary_action_id}"
                        ),
                        "pre_sequence_index": len(pre_sequence),
                        "sender_role": "self",
                        "message_type": "text",
                        "normalized_content_hash": boundary_hash,
                        "native_source_message_id": "",
                        "canonical_visual_id": "",
                        "ai_reply_boundary": True,
                        "_worker_sequence_number": int(
                            stable_match.group(1)
                        ),
                    }
                )
                checkpoint_stable_ids.add(boundary_stable_id)
        possible_state = load_c2_state(
            f"possible_ai_sends:{target.conversation_id}"
        )
        if boundary_action_id and boundary_hash:
            for possible in possible_state.get("sends") or []:
                if not isinstance(possible, dict):
                    continue
                reserved_id = str(
                    possible.get("reserved_worker_stable_id") or ""
                ).strip()
                stable_match = re.fullmatch(
                    r"worker-message-(\d+)", reserved_id
                )
                if (
                    stable_match is None
                    or reserved_id in checkpoint_stable_ids
                    or str(possible.get("reply_action_id") or "").strip()
                    != boundary_action_id
                    or str(possible.get("reply_text_hash") or "").strip()
                    != boundary_hash
                ):
                    continue
                pre_sequence.append(
                    {
                        "identity_state": "committed",
                        "worker_stable_id": reserved_id,
                        "pre_observation_id": (
                            f"sent-ack-possible:{boundary_action_id}"
                        ),
                        "pre_sequence_index": len(pre_sequence),
                        "sender_role": "self",
                        "message_type": "text",
                        "normalized_content_hash": boundary_hash,
                        "native_source_message_id": "",
                        "canonical_visual_id": "",
                        "ai_reply_boundary": True,
                        "ai_reconciliation_state": str(
                            possible.get("reconciliation_state") or ""
                        ),
                        "_worker_sequence_number": int(
                            stable_match.group(1)
                        ),
                    }
                )
                checkpoint_stable_ids.add(reserved_id)
        if any("_worker_sequence_number" in item for item in pre_sequence):
            def sequence_number(item: dict[str, Any]) -> int:
                explicit = item.get("_worker_sequence_number")
                if isinstance(explicit, int):
                    return explicit
                match = re.fullmatch(
                    r"worker-message-(\d+)",
                    str(item.get("worker_stable_id") or "").strip(),
                )
                return int(match.group(1)) if match else 2**63 - 1

            pre_sequence.sort(key=sequence_number)
            for index, item in enumerate(pre_sequence):
                item.pop("_worker_sequence_number", None)
                item["pre_sequence_index"] = index
        post_sequence = build_post_action_observation_sequence(observations)
        frame_id = str(
            prepared.get("frame_id")
            or prepared.get("sidecar_run_id")
            or prepared.get("run_id")
            or ""
        ).strip()
        if not frame_id:
            return prepared, [
                {
                    "error_code": "MESSAGE_IDENTITY_UNCONFIRMED",
                    "reason": "initial_frame_id_missing",
                }
            ]
        explicit_empty = not pre_sequence and int(
            checkpoint.get("next_sequence_floor") or 0
        ) <= 1
        if not pre_sequence and not explicit_empty:
            return prepared, [
                {
                    "error_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
                    "reason": "checkpoint_history_not_visible",
                }
            ]
        evidence = align_committed_message_sequence(
            pre_sequence,
            post_sequence,
            pre_sequence_source=(
                "empty_checkpoint" if explicit_empty else "checkpoint"
            ),
            pre_frame_id=(
                f"checkpoint:none:{target.conversation_id}"
                if explicit_empty
                else f"checkpoint:{target.authorization_revision}"
            ),
            post_frame_id=f"frame:{frame_id}",
        )
        if evidence.get("alignment_status") not in {"unique", "not_required"}:
            prepared["sequence_alignment_evidence"] = evidence
            return prepared, [
                {
                    "error_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
                    "reason": str(evidence.get("alignment_status") or ""),
                    "sequence_alignment_evidence": evidence,
                }
            ]
        observations = apply_inherited_worker_ids(observations, evidence)
        observations = self._assign_sequence_new_suffix_identities(
            target=target,
            observations=observations,
            evidence=evidence,
            read_run_id=read_run_id,
        )
        prepared["observations"] = observations
        prepared["sequence_alignment_evidence"] = evidence
        return prepared, []

    def _assign_sequence_new_suffix_identities(
        self,
        *,
        target: WechatReadTarget,
        observations: list[Any],
        evidence: dict[str, Any],
        read_run_id: str,
    ) -> list[Any]:
        """Number only the safely exposed tail of one authoritative frame."""

        if (
            evidence.get("alignment_status") not in {"unique", "not_required"}
            or evidence.get("old_tail_fully_consumed") is not True
        ):
            return list(observations)
        new_suffix = {
            str(value).strip()
            for value in (evidence.get("new_suffix_observation_ids") or [])
            if str(value).strip()
        }
        assigned: list[Any] = []
        for raw in observations:
            if not isinstance(raw, dict):
                assigned.append(raw)
                continue
            observation = dict(raw)
            observation_id = str(
                observation.get("observation_id") or ""
            ).strip()
            message_type = str(
                observation.get("message_type") or ""
            ).strip().lower()
            uncommitted_media = (
                message_type == "voice"
                and str(observation.get("voice_state") or "").strip().lower()
                == "untranscribed"
            )
            if (
                observation_id in new_suffix
                and not uncommitted_media
                and observation_role_is_trusted(observation)
            ):
                observation["_worker_stable_id"] = self._reserve_worker_sequence(
                    target,
                    reservation_key=(
                        f"committed:{read_run_id}:{observation_id}"
                    ),
                )
            assigned.append(observation)
        return assigned

    def _retry_identity_from_backend_checkpoint_once(
        self,
        *,
        target: WechatReadTarget,
        read_run_id: str,
        target_label: str,
        cancel_check: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
        """Reread the current chat once and let backend identity win."""

        refreshed = self.bridge.get_messages(
            display_name=target_label,
            rpa_session_key="",
            remark_code=target.remark_code or "",
            target_mode="current",
            max_duration_seconds=20,
            cancel_check=cancel_check,
        )
        if not refreshed.get("ok"):
            return (
                None,
                [],
                str(
                    refreshed.get("error_code")
                    or refreshed.get("state")
                    or "TARGET_NOT_CONFIRMED_FOR_MESSAGES"
                ),
            )
        contract_error = sidecar_contract_error(refreshed)
        if contract_error:
            return None, [], contract_error
        refreshed, errors = self._align_initial_identity_frame(
            target=target,
            sidecar_payload=refreshed,
            read_run_id=read_run_id,
        )
        refreshed["authoritative_frame_source"] = "final_read"
        refreshed["identity_automatic_reread_performed"] = True
        return refreshed, errors, ""

    def _retry_history_gap_from_backend_once(
        self,
        *,
        target: WechatReadTarget,
        read_run_id: str,
        target_label: str,
        sidecar_payload: dict[str, Any],
        incremental_plan: dict[str, Any],
        cancel_check: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Recheck one blocking history gap before projecting a handoff."""

        if (
            not incremental_plan.get("history_gap")
            or sidecar_payload.get(
                "history_gap_automatic_reread_performed"
            )
            or sidecar_payload.get(
                "identity_automatic_reread_performed"
            )
        ):
            return sidecar_payload, incremental_plan
        original = dict(sidecar_payload)
        original["history_gap_automatic_reread_performed"] = True
        refreshed, identity_errors, retry_error_code = (
            self._retry_identity_from_backend_checkpoint_once(
                target=target,
                read_run_id=read_run_id,
                target_label=target_label,
                cancel_check=cancel_check,
            )
        )
        if refreshed is None or identity_errors:
            original["history_gap_automatic_reread_error_code"] = str(
                retry_error_code
                or (
                    identity_errors[0].get("error_code")
                    if identity_errors
                    else "C2_MESSAGE_HISTORY_GAP_RECHECK_FAILED"
                )
            )
            return original, incremental_plan
        for key in (
            "initial_messages",
            "voice_transcription",
            "send_context_guard",
            "ui_frame_invalidated",
        ):
            if key in sidecar_payload and key not in refreshed:
                refreshed[key] = sidecar_payload[key]
        refreshed["ui_frame_invalidated"] = bool(
            sidecar_payload.get("ui_frame_invalidated")
            or refreshed.get("ui_frame_invalidated")
        )
        retry_sidecar_run_id = str(
            refreshed.get("sidecar_run_id") or ""
        )
        refreshed = dict(refreshed)
        refreshed["history_gap_automatic_reread_performed"] = True
        refreshed["history_gap_automatic_reread_sidecar_run_id"] = (
            retry_sidecar_run_id
        )
        deferred_voices = _executable_untranscribed_voice_observations(
            target,
            refreshed,
        )
        if deferred_voices:
            # A history-gap reread is evidence for confirming the existing
            # gate; it is not a second media-processing pass.  A voice first
            # observed here intentionally has no committed Worker identity
            # yet, so building the settlement plan would either invent a
            # cross-round identity or fail before the deferred branch.  Keep
            # the original authoritative frame/gate and let the next
            # authorized read own prepare/execute and identity commitment.
            deferred_observation_ids = sorted(
                {
                    str(item.get("observation_id") or "").strip()
                    for item in deferred_voices
                    if str(item.get("observation_id") or "").strip()
                }
            )
            original["history_gap_automatic_reread_sidecar_run_id"] = (
                retry_sidecar_run_id
            )
            original["history_gap_reread_new_facts_deferred"] = True
            original["history_gap_reread_deferred_observation_ids"] = (
                deferred_observation_ids
            )
            append_log(
                "INFO",
                "c2_history_gap_reread_new_voice_deferred",
                "历史断层自动重读发现未处理语音；保留原结算画面和门禁，语音交由下一次授权读取。",
                metadata={
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "deferred_observation_ids": deferred_observation_ids,
                    "sidecar_run_id": retry_sidecar_run_id,
                },
            )
            return original, incremental_plan
        refreshed = self._merge_waiting_image_facts(
            target=target,
            sidecar_payload=refreshed,
        )
        refreshed_plan = self._build_final_slot_incremental_plan(
            target=target,
            sidecar_payload=refreshed,
            read_run_id=read_run_id,
        )
        original_source_keys = {
            str(item.get("source_message_key") or "").strip()
            for item in incremental_plan.get("slot_ledger_states") or []
            if isinstance(item, dict)
            and str(item.get("source_message_key") or "").strip()
        }
        refreshed_source_keys = {
            str(item.get("source_message_key") or "").strip()
            for item in refreshed_plan.get("slot_ledger_states") or []
            if isinstance(item, dict)
            and str(item.get("source_message_key") or "").strip()
        }
        deferred_source_keys = sorted(
            refreshed_source_keys - original_source_keys
        )
        if original_source_keys and deferred_source_keys:
            # The voice/media phases for this authorized read have already
            # finished. A history-gap confirmation frame must not become a
            # second settlement frame, otherwise newly arrived untranscribed
            # media could be omitted while the remaining text reaches Brain.
            # Keep the original gate and let the next authorized read own all
            # newly observed facts through the normal media pipeline.
            original["history_gap_automatic_reread_sidecar_run_id"] = (
                retry_sidecar_run_id
            )
            original["history_gap_reread_new_facts_deferred"] = True
            original["history_gap_reread_deferred_source_keys"] = (
                deferred_source_keys
            )
            append_log(
                "INFO",
                "c2_history_gap_reread_new_facts_deferred",
                "历史断层自动重读发现新增事实；保留原结算画面和门禁，新增事实交由下一次授权读取。",
                metadata={
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "deferred_source_keys": deferred_source_keys,
                    "sidecar_run_id": retry_sidecar_run_id,
                },
            )
            return original, incremental_plan
        return refreshed, refreshed_plan

    def _attach_confirmed_ai_reply_receipts(
        self,
        *,
        target: WechatReadTarget,
        observations: list[Any],
    ) -> list[Any]:
        identity_state = load_c2_state(
            f"message_identity:{target.conversation_id}"
        )
        active_receipts: list[dict[str, Any]] = []
        for raw in identity_state.get("ai_reply_receipts") or []:
            if not isinstance(raw, dict):
                continue
            if (
                str(raw.get("reply_action_id") or "").strip()
                and str(raw.get("reply_text_hash") or "").strip()
                and str(raw.get("worker_stable_id") or "").strip()
            ):
                active_receipts.append(dict(raw))
        receipts_by_stable_id = {
            str(item["worker_stable_id"]): item for item in active_receipts
        }
        enriched: list[Any] = []
        for raw_observation in observations:
            if not isinstance(raw_observation, dict):
                enriched.append(raw_observation)
                continue
            observation = dict(raw_observation)
            stable_id = str(observation.get("_worker_stable_id") or "").strip()
            receipt = receipts_by_stable_id.get(stable_id)
            if (
                receipt
                and str(observation.get("row_kind") or "") == "text_bubble"
                and str(observation.get("sender_role") or "") == "self"
            ):
                observation["_worker_ai_reply_receipt"] = receipt
            enriched.append(observation)
        return enriched

    def _consume_confirmed_ai_reply_receipts(
        self,
        *,
        payload: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        successful_source_keys = {
            str(item.get("source_message_key") or "").strip()
            for item in (result.get("results") or [])
            if isinstance(item, dict)
            and item.get("ingest_result") in {"ingested", "duplicated"}
            and str(item.get("source_message_key") or "").strip()
        }
        if not successful_source_keys:
            return
        consumed_action_ids: set[str] = set()
        for message in payload.get("messages") or []:
            if not isinstance(message, dict):
                continue
            source_key = str(message.get("source_message_key") or "").strip()
            if source_key not in successful_source_keys:
                continue
            raw_payload = (
                message.get("raw_payload")
                if isinstance(message.get("raw_payload"), dict)
                else {}
            )
            receipt = (
                raw_payload.get("ai_reply_receipt")
                if isinstance(raw_payload.get("ai_reply_receipt"), dict)
                else {}
            )
            action_id = str(receipt.get("reply_action_id") or "").strip()
            if action_id:
                consumed_action_ids.add(action_id)
        if not consumed_action_ids:
            return
        conversation_id = str(payload.get("conversation_id") or "").strip()
        if not conversation_id:
            return
        state_key = f"ai_reply_receipts:{conversation_id}"
        state = load_c2_state(state_key)
        remaining = [
            dict(item)
            for item in (state.get("receipts") or [])
            if isinstance(item, dict)
            and str(item.get("reply_action_id") or "").strip()
            not in consumed_action_ids
        ]
        save_c2_state(
            state_key,
            {
                "version": 1,
                "receipts": remaining,
                "updated_at": self._utc_now_iso(),
            },
        )
        identity_state_key = f"message_identity:{conversation_id}"

        def consume_identity_receipts(previous: dict[str, Any]):
            state = dict(previous)
            state["ai_reply_receipts"] = [
                dict(item)
                for item in (previous.get("ai_reply_receipts") or [])
                if isinstance(item, dict)
                and str(item.get("reply_action_id") or "").strip()
                not in consumed_action_ids
            ]
            state["updated_at"] = self._utc_now_iso()
            return state, None

        update_c2_state_atomic(
            identity_state_key,
            consume_identity_receipts,
        )
        for action_id in consumed_action_ids:
            self._clear_possible_ai_send(
                conversation_id=conversation_id,
                reply_action_id=action_id,
            )
        append_log(
            "INFO",
            "ai_reply_receipt_consumed",
            "AI 回复气泡已得到后端入库确认，本地凭证已安全消费。",
            metadata={
                "conversation_id": conversation_id,
                "reply_action_ids": sorted(consumed_action_ids),
            },
        )

    def _run_add_friend_with_ui_lock(self, binding: Binding, task: Task) -> RpaResult:
        owner = f"{binding.worker_id}:{binding.client_instance_id}:add_friend:{task.id}"
        try:
            force_recover_stale_lock(reason="before_add_friend")
            lease = acquire_ui_lock(operation_type="add_friend", owner=owner, current_step="add_friend_starting")
            lease.start_auto_renew()
            self.current_ui_lock = lease
            self.current_step = "add_friend_starting"
            append_log("INFO", "ui_lock_acquired", "已获取微信 UI 锁，开始执行加好友。", task_id=task.id, metadata={"lock_id": lease.lock_id, "fencing_token": lease.fencing_token, "operation_type": getattr(lease, "operation_type", "add_friend")})
        except UiLockError as exc:
            append_log("ERROR", "ui_lock_acquire_failed", str(exc), task_id=task.id, error_code=exc.code, metadata=exc.data)
            return RpaResult(ok=False, error_code=exc.code, failure_step="ui_lock_acquire", message=str(exc), evidence_metadata={"ui_lock": exc.data})
        try:
            def add_friend_cancel_reason() -> bool | str:
                if not self._can_continue_inflight_flow():
                    return "WORKER_INTERRUPTED"
                if (
                    self.current_task_lease is not None
                    and self.current_task_lease.cancel_requested()
                ):
                    return (
                        self.current_task_lease.error_code
                        or "TASK_LEASE_RENEW_FAILED"
                    )
                ui_lock_reason = _ui_lock_cancel_reason(lease)
                if ui_lock_reason:
                    return ui_lock_reason
                return False

            return self.bridge.run_add_friend(
                task,
                lambda step: self._report_step(binding, task, step),
                cancel_check=add_friend_cancel_reason,
            )
        except UiLockError as exc:
            append_log("ERROR", "ui_lock_runtime_failed", str(exc), task_id=task.id, error_code=exc.code, metadata=exc.data)
            return RpaResult(ok=False, error_code=exc.code, failure_step=self.current_step or "ui_lock_runtime", message=str(exc), evidence_metadata={"ui_lock": exc.data})
        finally:
            self._release_current_ui_lock(task_id=task.id, reason="add_friend_finished")

    def _release_current_ui_lock(self, *, task_id: str | None = None, reason: str) -> None:
        lease = self.current_ui_lock
        self.current_ui_lock = None
        if not lease:
            return
        try:
            lease.release()
            append_log("INFO", "ui_lock_released", f"已释放微信 UI 锁：{reason}", task_id=task_id, metadata={"lock_id": lease.lock_id, "fencing_token": lease.fencing_token, "operation_type": getattr(lease, "operation_type", ""), "release_reason": reason})
        except UiLockError as exc:
            append_log("ERROR", "ui_lock_release_failed", str(exc), task_id=task_id, error_code=exc.code, metadata=exc.data)

    def _upload_evidence_best_effort(
        self,
        binding: Binding,
        task_id: str,
        content: str,
        *,
        error_code: str | None = None,
        evidence_path: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.api.upload_evidence(
                binding,
                task_id,
                content,
                error_code=error_code,
                evidence_path=evidence_path,
                metadata=metadata,
            )
        except Exception as exc:
            append_log("WARN", "evidence_upload_failed", str(exc), task_id=task_id, error_code=error_code)
            self.on_error("执行证据上传失败，主任务结果已保留。")

    def _report_step(self, binding: Binding, task: Task, step: RpaStep) -> None:
        self.current_step = step.current_step
        if self.current_ui_lock:
            self.current_ui_lock.update_step(step.current_step)
            self.current_ui_lock.check_step_timeout()
        self.on_step(step)
        self.api.report_step(binding, task.id, step.current_step, step.remark)
        append_log("INFO", "step_reported", step.remark, task_id=task.id)

    def _c2_loop(self) -> None:
        append_log("INFO", "c2_listener_started", "C2 微信监听循环启动。")
        while not self.stop_event.is_set():
            binding = self.binding
            if not binding or not CONFIG.c2_enabled or not self._c2_dependencies_ready():
                self.stop_event.wait(1.0)
                continue
            if self.current_ui_lock is not None or bool(lock_summary().get("locked")):
                self.stop_event.wait(0.5)
                continue
            runtime_control = load_runtime_control()
            inflight_flow_id = str(
                runtime_control.get("inflight_flow_id") or ""
            ).strip()
            if inflight_flow_id:
                if self._can_continue_inflight_flow(inflight_flow_id):
                    # A persisted flow is recovery-only in this listener.  Its
                    # live owner (if any) continues the customer chain; after
                    # restart only durable Journal/Outbox/sent_ack settlement
                    # is allowed.  Never scan or select another conversation.
                    self._worker_transaction_barrier_ready(
                        binding,
                        reason="c2_inflight_recovery",
                    )
                    self._finish_restart_recovery_flow_if_settled(binding)
                self.stop_event.wait(1.0)
                continue
            if not self._can_start_new_flow(binding):
                self.stop_event.wait(1.0)
                continue
            # A reply already exists in WeChat once its bubble is confirmed.
            # Its durable sent_ack must reach the backend before C2 is allowed
            # to scan that bubble and classify its source.
            if not self._worker_transaction_barrier_ready(
                binding,
                reason="c2_loop",
            ):
                # A settled image fact may still be waiting for its backend
                # acknowledgement after a crash.  This is durable recovery,
                # not a new scan: only the oldest affected conversation may
                # proceed, and _recover_pending_image_transaction performs
                # its own authorization and identity checks before any UI
                # action.  Do not leave the listener waiting forever on the
                # very barrier that this recovery is responsible for clearing.
                if (
                    self._pending_image_recovery_conversation_ids()
                    and self._can_start_new_flow(binding)
                    and self._wechat_ready_for_c2()
                ):
                    self._recover_pending_image_transaction(binding)
                self.stop_event.wait(1.0)
                continue
            if not self._wechat_ready_for_c2():
                self.stop_event.wait(1.0)
                continue
            if not self._c2_vision_ready_before_scan():
                self.stop_event.wait(1.0)
                continue
            now = time.monotonic()
            if self.c2_manual_scan_requested.is_set():
                self.c2_manual_scan_requested.clear()
                self._run_c2_scan_round(binding, reason="manual_immediate_scan")
            if now - self.last_c2_scan_at >= CONFIG.c2_session_scan_interval_seconds:
                self._run_c2_scan_round(binding, reason="visible_sessions")
                self.last_c2_scan_at = now
            if now - self.last_c2_read_at >= CONFIG.c2_message_read_interval_seconds:
                self._read_state_target_queue(binding)
                self.last_c2_read_at = now
            self.stop_event.wait(1.0)

    def _recover_pending_image_transaction(
        self,
        binding: Binding,
    ) -> bool:
        self._discard_not_attempted_image_action_journals()
        pending_conversations = (
            self._pending_image_recovery_conversation_ids()
        )
        if not pending_conversations:
            return True
        conversation_id = pending_conversations[0]
        journal_entries = list_action_journals(
            conversation_id=conversation_id,
            action_kinds=("image",),
        )
        transaction_id = next(
            (
                str(payload.get("transaction_id") or "").strip()
                for _path, payload in journal_entries
                if str(payload.get("transaction_id") or "").strip()
            ),
            "",
        )
        journal_outcomes = self._physical_action_journal_outcomes(
            journal_entries
        )
        missing_ledger_outcomes = [
            outcome
            for outcome in journal_outcomes
            if not load_c2_ledger_entry(
                conversation_id,
                str(outcome.get("source_message_key") or "").strip(),
            )
        ]
        if missing_ledger_outcomes:
            self._persist_c2_flow_outcomes(
                WechatReadTarget(
                    conversation_id=conversation_id,
                    rpa_session_key="",
                    display_name="",
                ),
                missing_ledger_outcomes,
            )
        entries = list_c2_ledger_entries(
            conversation_id,
            message_type="image",
            ingest_state="waiting",
        )
        source_keys = sorted(
            {
                str(entry.get("source_message_key") or "").strip()
                for entry in entries
                if str(entry.get("source_message_key") or "").strip()
            }
        )
        if not source_keys:
            return False
        source_digest = hashlib.sha256(
            "\n".join(source_keys).encode("utf-8")
        ).hexdigest()
        if not transaction_id:
            transaction_id = f"media-fact-{source_digest[:32]}"
        original_revision = next(
            (
                str(
                    (
                        entry.get("result")
                        if isinstance(entry.get("result"), dict)
                        else {}
                    ).get("authorization_revision")
                    or ""
                ).strip()
                for entry in entries
                if str(
                    (
                        entry.get("result")
                        if isinstance(entry.get("result"), dict)
                        else {}
                    ).get("authorization_revision")
                    or ""
                ).strip()
            ),
            f"recovery-{source_digest[:16]}",
        )
        try:
            authorization = self.api.get_wechat_read_authorization(
                binding,
                conversation_id,
                recovery_transaction_id=transaction_id,
                action_kind="image",
                source_message_key_digest=source_digest,
                original_authorization_revision=original_revision,
            )
        except Exception as exc:
            append_log(
                "WARN",
                "c2_image_fact_recovery_read_targets_failed",
                "获取原会话恢复授权失败，继续保持全局门禁并稍后重试。",
                error_code="C2_IMAGE_FACT_RECOVERY_AUTHORIZATION_FAILED",
                metadata={
                    "conversation_id": conversation_id,
                    "error_type": type(exc).__name__,
                },
            )
            return False
        recovery_decision = str(
            authorization.get("recovery_decision") or ""
        ).strip()
        if recovery_decision == "settle_without_ui":
            return bool(
                self._settle_invalid_image_facts_without_ui(
                    binding=binding,
                    conversation_id=conversation_id,
                    authorization=authorization,
                    entries=entries,
                    source_keys=source_keys,
                    transaction_id=transaction_id,
                    source_digest=source_digest,
                    original_revision=original_revision,
                )
            )
        if recovery_decision != "resume_current_target":
            append_log(
                "WARN",
                "c2_image_fact_recovery_waiting_authorization",
                "待上报图片事实的原会话暂不可用，继续保持全局门禁。",
                error_code="C2_IMAGE_FACT_RECOVERY_RETRY_LATER",
                metadata={
                    "conversation_id": conversation_id,
                    "recovery_decision": recovery_decision or "missing",
                },
            )
            return False
        target_payload = authorization.get("target")
        target = (
            WechatReadTarget.from_api(target_payload)
            if isinstance(target_payload, dict)
            else None
        )
        if (
            target is None
            or target.conversation_id != conversation_id
            or authorization.get("allowed") is not True
        ):
            append_log(
                "WARN",
                "c2_image_fact_recovery_authorization_invalid",
                "图片恢复授权缺少一致的原会话定位信息，继续保持全局门禁。",
                error_code="C2_IMAGE_FACT_RECOVERY_AUTHORIZATION_INVALID",
                metadata={"conversation_id": conversation_id},
            )
            return False
        validation_error = self._validate_read_target(target)
        if validation_error:
            append_log(
                "WARN",
                "c2_image_fact_recovery_target_invalid",
                "待上报图片事实的原会话授权不完整，继续保持全局门禁。",
                error_code=validation_error,
                metadata={
                    "conversation_id": conversation_id,
                    "remark_code": target.remark_code,
                },
            )
            return False
        append_log(
            "INFO",
            "c2_image_fact_recovery_started",
            "开始恢复待上报图片事实；本轮只允许打开原会话。",
            metadata={
                "conversation_id": conversation_id,
                "remark_code": target.remark_code,
            },
        )
        try:
            result = self._read_one_wechat_target(
                binding,
                target,
                current_step="image_fact_recovery",
                enforce_read_targets=True,
                recovery_waiting_image_facts=True,
            )
        except Exception as exc:
            append_log(
                "WARN",
                "c2_image_fact_recovery_interrupted",
                "恢复图片事实时后端或本地事务暂时失败，继续保持全局门禁并稍后重试。",
                error_code="C2_IMAGE_FACT_RECOVERY_INTERRUPTED",
                metadata={
                    "conversation_id": conversation_id,
                    "remark_code": target.remark_code,
                    "error_type": type(exc).__name__,
                },
            )
            return False
        recovered = bool(
            result.get("ok")
            and conversation_id
            not in self._pending_image_recovery_conversation_ids()
        )
        append_log(
            "INFO" if recovered else "WARN",
            "c2_image_fact_recovery_finished",
            (
                "待上报图片事实已由现有 Outbox 交给后端。"
                if recovered
                else "待上报图片事实尚未确认，继续保持全局门禁。"
            ),
            error_code=(
                None
                if recovered
                else str(
                    result.get("error_code")
                    or "C2_IMAGE_FACT_RECOVERY_PENDING"
                )
            ),
            metadata={
                "conversation_id": conversation_id,
                "remark_code": target.remark_code,
            },
        )
        return recovered

    def _settle_invalid_image_facts_without_ui(
        self,
        *,
        binding: Binding,
        conversation_id: str,
        authorization: dict[str, Any],
        entries: list[dict[str, Any]],
        source_keys: list[str],
        transaction_id: str,
        source_digest: str,
        original_revision: str,
    ) -> bool:
        """Report deterministic invalid-image facts without reopening WeChat.

        Older clients could leave ``C2_IMAGE_SOURCE_INVALID`` in the waiting
        ledger after a text menu or non-bitmap clipboard result.  Those facts
        are already terminal: reopening the chat cannot improve them and can
        starve every other target.  Recovery therefore rebuilds the original
        failed message observations and waits for per-message backend
        confirmation without repeating any WeChat or Vision action.
        """

        recovery_observations: list[dict[str, Any]] = []
        for entry in entries:
            result = (
                dict(entry.get("result") or {})
                if isinstance(entry.get("result"), dict)
                else {}
            )
            replayable = (
                result.get("replayable_observation")
                if isinstance(result.get("replayable_observation"), dict)
                else {}
            )
            transaction = (
                result.get("transaction")
                if isinstance(result.get("transaction"), dict)
                else {}
            )
            error_code = str(
                result.get("error_code")
                or result.get("reason")
                or replayable.get("error_code")
                or ""
            ).strip()
            if (
                str(entry.get("terminal_state") or "")
                not in {"completed", "failed"}
                or not replayable
            ):
                return False
            source_key = str(
                entry.get("source_message_key") or ""
            ).strip()
            if source_key:
                restored = dict(replayable)
                restored_source = (
                    dict(restored.get("source_message"))
                    if isinstance(restored.get("source_message"), dict)
                    else {}
                )
                restored["source_message"] = {
                    **restored_source,
                    "source_message_key": source_key,
                }
                reason_detail = str(
                    restored.get("reason_detail")
                    or result.get("reason_detail")
                    or transaction.get("status")
                    or error_code
                ).strip()
                if str(entry.get("terminal_state") or "") == "failed":
                    restored["error_code"] = error_code
                    restored["reason_detail"] = reason_detail
                sender_role = str(
                    restored.get("sender_role")
                    or restored_source.get("sender_role")
                    or ""
                ).strip().lower()
                if sender_role in {"customer", "self"}:
                    recovery_observations.append(restored)
                else:
                    append_log(
                        "WARN",
                        "c2_fact_settlement_sender_identity_untrusted",
                        "恢复事实的发送方身份无法证明；保留本地事实并等待后端可安全终结。",
                        error_code="MESSAGE_IDENTITY_UNCONFIRMED",
                        metadata={
                            "conversation_id": conversation_id,
                            "source_message_key": source_key,
                        },
                    )
                    return False
        settlement_mode = str(
            authorization.get("settlement_mode") or ""
        ).strip()
        target_payload = authorization.get("target")
        target = (
            WechatReadTarget.from_api(target_payload)
            if isinstance(target_payload, dict)
            else None
        )
        if settlement_mode == "fact_only" and target is None:
            return False
        if target is None:
            target = WechatReadTarget(
                conversation_id=conversation_id,
                rpa_session_key="",
                display_name="",
                remark_code="RECOVERY",
                read_reason="fact_settlement",
                authorization_revision=original_revision,
            )
        origin_read_run_ids = {
            str(entry.get("origin_read_run_id") or "").strip()
            for entry in entries
            if str(entry.get("origin_read_run_id") or "").strip()
        }
        if len(origin_read_run_ids) != 1:
            append_log(
                "ERROR",
                "c2_fact_settlement_origin_read_run_invalid",
                "恢复事实缺少唯一原读取轮次；保持等待且不重建 Outbox。",
                error_code="C2_FACT_ORIGIN_READ_RUN_ID_INVALID",
                metadata={"conversation_id": conversation_id},
            )
            return False
        origin_read_run_id = next(iter(origin_read_run_ids))
        recovery_pairs: list[dict[str, Any]] = []
        for index, observation in enumerate(recovery_observations):
            stable_id = str(
                observation.get("_worker_stable_id") or ""
            ).strip()
            observation_id = str(
                observation.get("observation_id") or ""
            ).strip()
            if not stable_id or not observation_id:
                append_log(
                    "ERROR",
                    "c2_action_journal_recovery_identity_missing",
                    "ActionJournal 恢复事实缺少已提交的统一序列身份；保持等待且不读取微信。",
                    error_code="MESSAGE_SEQUENCE_IDENTITY_MISSING",
                    metadata={
                        "conversation_id": conversation_id,
                        "source_message_keys": source_keys,
                    },
                )
                return False
            recovery_pairs.append(
                {
                    "identity_state": "committed",
                    "worker_stable_id": stable_id,
                    "pre_observation_id": observation_id,
                    "post_observation_id": observation_id,
                    "pre_index": index,
                    "post_index": index,
                    "match_basis": (
                        "action_journal_committed_identity"
                    ),
                }
            )
        recovery_alignment_evidence = {
            "pre_sequence_source": "action_frame",
            "pre_frame_id": f"action-journal-terminal:{source_digest}",
            "post_frame_id": (
                f"action-journal-recovery:{transaction_id}"
            ),
            "alignment_status": "unique",
            "candidate_alignment_count": 1,
            "matched_pairs": recovery_pairs,
            "old_tail_fully_consumed": True,
            "new_suffix_observation_ids": [],
        }
        payload = build_message_ingest_payload(
            target,
            {
                "observation_schema_version": 3,
                "authoritative_frame_source": "action_journal_recovery",
                "ui_frame_invalidated": False,
                "adapter": "local_failed_image_recovery",
                "state": "failed_image_fact_recovery",
                "sidecar_run_id": "",
                "observations": (
                    recovery_observations
                    if settlement_mode == "fact_only"
                    else []
                ),
                "flow_gate_errors": [],
                "flow_gate_details": [],
                "sequence_alignment_evidence": (
                    recovery_alignment_evidence
                ),
            },
            read_run_id=origin_read_run_id,
        )
        payload_source_keys = sorted(
            str(item.get("source_message_key") or "").strip()
            for item in (payload.get("messages") or [])
            if isinstance(item, dict)
            and str(item.get("source_message_key") or "").strip()
        )
        if (
            settlement_mode == "fact_only"
            and payload_source_keys != source_keys
        ):
            append_log(
                "ERROR",
                "c2_invalid_image_recovery_identity_mismatch",
                "失败图片恢复后的消息身份与本地账本不一致；保持等待且不操作微信。",
                error_code="MESSAGE_SOURCE_IDENTITY_MISMATCH",
                metadata={
                    "conversation_id": conversation_id,
                    "remark_code": target.remark_code,
                    "ledger_source_keys": source_keys,
                    "payload_source_keys": payload_source_keys,
                },
            )
            return False
        payload_evidence = dict(payload.get("evidence") or {})
        payload_evidence.update(
            {
                "recovery_transaction_id": transaction_id,
                "action_kind": "image",
                "source_message_key_digest": source_digest,
                "settlement_mode": settlement_mode,
                "settlement_source_message_keys": source_keys,
                "recovery_requires_per_message_confirmation": True,
                "wechat_reopened": False,
                "clipboard_repeated": False,
                "vision_repeated": False,
            }
        )
        payload["evidence"] = payload_evidence
        payload["authorization_scope"] = "fact_settlement"
        payload["authorization_revision"] = original_revision
        delivery = self._submit_c2_outbox_payload(
            binding=binding,
            payload=payload,
            operation="invalid_image_failure_gate",
        )
        if not delivery.get("ok"):
            append_log(
                "WARN",
                "c2_invalid_image_failure_gate_waiting",
                "无效图片失败事实尚未得到后端确认；仅重传本地事实，不重新打开微信。",
                error_code=str(delivery.get("error_code") or ""),
                metadata={
                    "conversation_id": conversation_id,
                    "remark_code": target.remark_code,
                    "source_message_keys": source_keys,
                },
            )
            return False
        remaining_source_keys = {
            str(entry.get("source_message_key") or "").strip()
            for entry in list_c2_ledger_entries(
                target.conversation_id,
                message_type="image",
                ingest_state="waiting",
            )
        }
        unconfirmed_source_keys = sorted(
            source_key
            for source_key in source_keys
            if source_key in remaining_source_keys
        )
        if unconfirmed_source_keys:
            append_log(
                "WARN",
                "c2_invalid_image_recovery_unconfirmed",
                "后端未逐条确认全部失败图片事实；未确认记录继续等待且不操作微信。",
                error_code="C2_IMAGE_FACT_RECOVERY_PENDING",
                metadata={
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "unconfirmed_source_keys": unconfirmed_source_keys,
                },
            )
            return False
        removed_journal_count = 0
        for path, _payload in list_action_journals(
            conversation_id=conversation_id,
            action_kinds=("image",),
        ):
            remove_action_journal(path)
            removed_journal_count += 1
        append_log(
            "WARN",
            "c2_invalid_image_failure_gate_reported",
            "无效图片事实已由后端逐条确认；本地等待和全局门禁已释放。",
            error_code="C2_IMAGE_SOURCE_INVALID",
            metadata={
                "conversation_id": conversation_id,
                "remark_code": target.remark_code,
                "source_message_keys": source_keys,
                "removed_journal_count": removed_journal_count,
                "wechat_reopened": False,
                "vision_repeated": False,
            },
        )
        return True

    def _c2_dependencies_ready(self) -> bool:
        return all(
            hasattr(obj, name)
            for obj, name in (
                (self.bridge, "list_sessions"),
                (self.bridge, "get_messages"),
                (self.bridge, "prepare_voice_action"),
                (self.bridge, "execute_voice_action"),
                (self.api, "post_wechat_session_scan_result"),
                (self.api, "get_wechat_read_targets"),
                (self.api, "post_wechat_messages_ingest"),
            )
        )

    def _c2_vision_ready_before_scan(self) -> bool:
        now = time.monotonic()
        if now - self.last_c2_vision_preflight_at < 5.0:
            return self.c2_vision_preflight_ready
        self.last_c2_vision_preflight_at = now
        try:
            from .omniauto_vision import vision_configuration_status

            status = vision_configuration_status()
        except Exception as exc:
            status = {
                "ready": False,
                "missing_configuration": ["VISION_PREFLIGHT_EXCEPTION"],
                "error_type": type(exc).__name__,
            }
        ready = bool(status.get("ready"))
        missing = sorted(
            {
                str(value).strip()
                for value in (
                    status.get("missing_configuration") or []
                )
                if str(value).strip()
            }
        )
        signature = json.dumps(
            {
                "ready": ready,
                "missing_configuration": missing,
                "provider": str(status.get("provider") or ""),
                "base_url": str(status.get("base_url") or ""),
                "model": str(status.get("model") or ""),
                "request_style": str(status.get("request_style") or ""),
                "error_type": str(status.get("error_type") or ""),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        changed = signature != self.c2_vision_preflight_signature
        self.c2_vision_preflight_signature = signature
        self.c2_vision_preflight_ready = ready
        self.c2_stats["vision_ready"] = ready
        self.c2_stats["vision_missing_configuration"] = missing
        if ready:
            if self.c2_stats.get("last_error") == "C2_VISION_NOT_READY":
                self.c2_stats["last_error"] = None
        else:
            self.c2_stats["last_error"] = "C2_VISION_NOT_READY"
        save_c2_state(
            "vision_preflight",
            {
                "state": "ready" if ready else "vision_not_ready",
                "error_code": None if ready else "C2_VISION_NOT_READY",
                "missing_configuration": missing,
                "provider": str(status.get("provider") or ""),
                "base_url": str(status.get("base_url") or ""),
                "model": str(status.get("model") or ""),
                "request_style": str(status.get("request_style") or ""),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        if changed:
            append_log(
                "INFO" if ready else "ERROR",
                (
                    "c2_vision_preflight_ready"
                    if ready
                    else "c2_vision_preflight_blocked"
                ),
                (
                    "Vision 全局配置预检通过，C2 可以开始扫描。"
                    if ready
                    else "Vision 全局配置未就绪，C2 不扫描、不打开会话。"
                ),
                error_code=None if ready else "C2_VISION_NOT_READY",
                metadata={
                    "ready": ready,
                    "missing_configuration": missing,
                    "provider": str(status.get("provider") or ""),
                    "base_url": str(status.get("base_url") or ""),
                    "model": str(status.get("model") or ""),
                    "request_style": str(status.get("request_style") or ""),
                    "error_type": str(status.get("error_type") or ""),
                },
            )
        return ready

    def _wechat_ready_for_c2(self) -> bool:
        return self.last_rpa_component_status == "ready" and self.last_wechat_status == "logged_in"

    def _high_priority_active(self) -> bool:
        return self.current_task is not None or self.task_lock.locked()

    def _compact_visible_sessions_for_review(self, sessions: list[Any], *, limit: int = 20) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, session in enumerate(sessions[:limit]):
            if not isinstance(session, dict):
                continue
            rows.append(
                {
                    "index": index,
                    "display_name": session.get("display_name") or session.get("name") or session.get("title"),
                    "rpa_session_key": session.get("rpa_session_key") or session.get("session_key"),
                    "remark_code_candidates": session.get("remark_code_candidates") or [],
                    "last_message_preview": session.get("last_message_preview") or session.get("content") or session.get("preview"),
                    "ocr_confidence": session.get("ocr_confidence"),
                    "row_fingerprint": session.get("row_fingerprint"),
                    "center_y": session.get("center_y"),
                    "bounds": [session.get("left"), session.get("top"), session.get("right"), session.get("bottom")]
                    if any(session.get(key) is not None for key in ("left", "top", "right", "bottom"))
                    else None,
                }
            )
        return rows

    def _compact_ocr_items_for_review(self, items: list[Any], *, limit: int = 120) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            row: dict[str, Any] = {"text": str(item.get("text") or "")}
            for key in ("left", "top", "right", "bottom", "center_x", "center_y", "confidence"):
                if key in item:
                    try:
                        row[key] = round(float(item.get(key) or 0), 3)
                    except Exception:
                        row[key] = item.get(key)
            if item.get("ocr_source"):
                row["ocr_source"] = item.get("ocr_source")
            rows.append(row)
        return rows

    def _visible_sessions_with_click_geometry(self, mapped_sessions: list[Any], raw_sessions: list[Any]) -> list[dict[str, Any]]:
        raw_by_key = {str(item.get("session_key") or ""): item for item in raw_sessions if isinstance(item, dict)}
        enriched: list[dict[str, Any]] = []
        for item in mapped_sessions:
            if not isinstance(item, dict):
                continue
            session = dict(item)
            raw = raw_by_key.get(str(session.get("rpa_session_key") or ""))
            if isinstance(raw, dict):
                for source_key, target_key in (
                    ("center_y", "center_y"),
                    ("left", "left"),
                    ("right", "right"),
                    ("top", "top"),
                    ("bottom", "bottom"),
                    ("center_x", "center_x"),
                    ("confidence", "ocr_confidence"),
                ):
                    if session.get(target_key) is None and raw.get(source_key) is not None:
                        session[target_key] = raw.get(source_key)
                if not isinstance(session.get("row_fingerprint"), dict) and raw.get("row_fingerprint"):
                    session["row_fingerprint"] = raw.get("row_fingerprint")
                if session.get("center_y") is None:
                    row_fingerprint = session.get("row_fingerprint")
                    if isinstance(row_fingerprint, dict):
                        bbox = row_fingerprint.get("title_bbox")
                        if isinstance(bbox, list) and len(bbox) >= 4:
                            try:
                                left, top, right, bottom = [float(value) for value in bbox[:4]]
                                session.setdefault("left", left)
                                session.setdefault("right", right)
                                session.setdefault("top", top)
                                session.setdefault("bottom", bottom)
                                session["center_y"] = (top + bottom) / 2.0
                            except (TypeError, ValueError):
                                pass
                session["raw_session_available"] = True
            enriched.append(session)
        return enriched

    def _remember_recent_visible_hits(self, sessions: list[dict[str, Any]]) -> None:
        now = time.monotonic()
        self._prune_recent_visible_hits(now=now)
        for session in sessions:
            if not isinstance(session, dict):
                continue
            codes = [str(code or "").strip().upper() for code in (session.get("remark_code_candidates") or []) if str(code or "").strip()]
            for code in codes:
                self.c2_recent_visible_hits_by_remark_code[code] = {"session": dict(session), "seen_at": now}

    def _prune_recent_visible_hits(self, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        expired = [
            code
            for code, entry in self.c2_recent_visible_hits_by_remark_code.items()
            if current - float(entry.get("seen_at") or 0) > C2_RECENT_VISIBLE_CACHE_TTL_SECONDS
        ]
        for code in expired:
            self.c2_recent_visible_hits_by_remark_code.pop(code, None)

    def _sidecar_visible_session_candidate(self, session: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(session, dict):
            return {}
        row_fingerprint = session.get("row_fingerprint")
        candidate: dict[str, Any] = {
            "name": session.get("name") or session.get("display_name") or session.get("title"),
            "session_key": session.get("session_key") or session.get("rpa_session_key"),
            "row_fingerprint": row_fingerprint,
            "confidence": session.get("confidence") if session.get("confidence") is not None else session.get("ocr_confidence"),
            "preview": session.get("preview") or session.get("content") or session.get("last_message_preview"),
        }
        for key in ("center_y", "left", "right", "top", "bottom"):
            value = session.get(key)
            if value is None:
                continue
            try:
                candidate[key] = float(value)
            except (TypeError, ValueError):
                pass
        if "center_y" not in candidate and isinstance(row_fingerprint, dict):
            bbox = row_fingerprint.get("title_bbox")
            if isinstance(bbox, list) and len(bbox) >= 4:
                try:
                    left, top, right, bottom = [float(value) for value in bbox[:4]]
                    candidate.setdefault("left", left)
                    candidate.setdefault("right", right)
                    candidate.setdefault("top", top)
                    candidate.setdefault("bottom", bottom)
                    candidate["center_y"] = (top + bottom) / 2.0
                    candidate["click_geometry_source"] = "row_fingerprint.title_bbox"
                except (TypeError, ValueError):
                    pass
        elif "center_y" in candidate:
            candidate["click_geometry_source"] = "session_fields"
        return {key: value for key, value in candidate.items() if value not in (None, "", {})}

    def _write_c2_sessions_review(
        self,
        *,
        reason: str,
        sidecar_payload: dict[str, Any],
        scan_payload: dict[str, Any],
        target: WechatReadTarget | None = None,
        match_metadata: dict[str, Any] | None = None,
    ) -> str | None:
        artifact_dir_value = sidecar_payload.get("artifact_dir")
        if not artifact_dir_value:
            return None
        try:
            artifact_dir = Path(str(artifact_dir_value))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            raw_sessions = sidecar_payload.get("sessions") if isinstance(sidecar_payload.get("sessions"), list) else []
            mapped_sessions = scan_payload.get("sessions") if isinstance(scan_payload.get("sessions"), list) else []
            ocr_items = sidecar_payload.get("ocr_items") if isinstance(sidecar_payload.get("ocr_items"), list) else []
            review = {
                "schema": "chejin.c2.sessions_review.v1",
                "reason": reason,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "sidecar": {
                    "ok": bool(sidecar_payload.get("ok")),
                    "state": sidecar_payload.get("state"),
                    "error_code": sidecar_payload.get("error_code"),
                    "sidecar_run_id": sidecar_payload.get("sidecar_run_id") or scan_payload.get("sidecar_run_id"),
                    "artifact_dir": str(artifact_dir),
                    "screenshot_path": sidecar_payload.get("screenshot_path") or (scan_payload.get("evidence") or {}).get("screenshot"),
                    "ocr_items_count": sidecar_payload.get("ocr_items_count"),
                    "ocr_items_enhanced_count": sidecar_payload.get("ocr_items_enhanced_count"),
                    "ocr_items": self._compact_ocr_items_for_review(ocr_items),
                },
                "target": {
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "display_name": target.display_name,
                    "rpa_session_key": target.rpa_session_key,
                    "read_reason": target.read_reason,
                }
                if target
                else None,
                "scan": {
                    "scan_id": scan_payload.get("scan_id"),
                    "session_count": len(mapped_sessions),
                    "mapped_sessions": self._compact_visible_sessions_for_review(mapped_sessions),
                    "raw_sessions": self._compact_visible_sessions_for_review(raw_sessions),
                },
                "match": match_metadata,
            }
            review_path = artifact_dir / "sessions_review.json"
            review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
            return str(review_path)
        except Exception as exc:
            append_log("WARN", "c2_sessions_review_write_failed", "C2 首屏扫描证据报告写入失败。", error_code="C2_SESSIONS_REVIEW_WRITE_FAILED", metadata={"reason": reason, "error": str(exc)})
            return None

    def _run_c2_scan_round(self, binding: Binding, *, reason: str) -> None:
        if not self._can_start_new_flow(binding):
            return
        if not self._worker_transaction_barrier_ready(
            binding,
            reason=reason,
        ):
            return
        self.c2_round_processed_conversation_ids = set()
        self._scan_wechat_sessions(binding, reason=reason)
        if not self._can_start_new_flow(binding):
            return
        targets = self._fetch_read_targets(binding)
        allowed_keys = {self._target_dedupe_key(target) for target in targets}
        self.c2_read_allowlist_keys = allowed_keys
        self._drain_visible_hit_queue(binding, authorized_targets=targets)
        self._read_state_target_queue(binding, targets=targets)

    def _scan_wechat_sessions(self, binding: Binding, *, reason: str = "scheduled") -> None:
        if not self._can_start_new_flow(binding):
            return
        if self._high_priority_active():
            self.c2_stats["last_error"] = "C2_SCAN_SKIPPED_BY_HIGH_PRIORITY_ACTION"
            append_log("INFO", "c2_session_scan_skipped", "C2 第一屏扫描被高优先级微信动作跳过。", error_code="C2_SCAN_SKIPPED_BY_HIGH_PRIORITY_ACTION", metadata={"reason": reason})
            return
        owner = f"{binding.worker_id}:{binding.client_instance_id}:session_scan:first_screen"
        lease: UiLockLease | None = None
        sidecar_scan_duration_ms: int | None = None
        try:
            with self.task_lock:
                if not self._can_start_new_flow(binding):
                    return
                if not self._worker_transaction_barrier_ready(
                    binding,
                    reason="session_scan_lock",
                ):
                    return
                if self.current_task is not None:
                    self.c2_stats["last_error"] = (
                        "C2_SCAN_SKIPPED_BY_HIGH_PRIORITY_ACTION"
                    )
                    append_log(
                        "INFO",
                        "c2_session_scan_skipped",
                        "领取 UI 锁前发现高优先级任务，首屏扫描继续排队。",
                        error_code="C2_SCAN_SKIPPED_BY_HIGH_PRIORITY_ACTION",
                        metadata={"reason": reason},
                    )
                    return
                force_recover_stale_lock(reason="before_session_scan")
                lease = acquire_ui_lock(
                    operation_type="session_scan",
                    owner=owner,
                    current_step="first_screen_session_scan",
                    timeout_seconds=CONFIG.c2_low_priority_lock_timeout_seconds,
                )
                lease.start_auto_renew()
                self.current_ui_lock = lease
            self.current_step = "first_screen_session_scan"
            if not self._can_start_new_flow(binding):
                return
            if self._high_priority_active():
                self.c2_stats["last_error"] = "C2_SCAN_SKIPPED_BY_HIGH_PRIORITY_ACTION"
                append_log("INFO", "c2_session_scan_skipped", "C2 第一屏扫描拿锁后发现高优先级动作，已跳过。", error_code="C2_SCAN_SKIPPED_BY_HIGH_PRIORITY_ACTION", metadata={"reason": reason})
                return
            sidecar_scan_started_at = time.perf_counter()
            try:
                sidecar_payload = self.bridge.list_sessions(
                    cancel_check=lambda: not self._can_start_new_flow(binding)
                )
            finally:
                sidecar_scan_duration_ms = int(
                    round(
                        (time.perf_counter() - sidecar_scan_started_at) * 1000
                    )
                )
            if not self._can_start_new_flow(binding):
                return
            payload = build_scan_result_payload(sidecar_payload)
            admission_evidence = (
                (payload.get("evidence") or {}).get("c2_conversation_admission")
                if isinstance(payload.get("evidence"), dict)
                else {}
            )
            contract_rejected_count = int(
                (admission_evidence or {}).get("contract_rejected_count") or 0
            )
            if contract_rejected_count:
                append_log(
                    "WARN",
                    "c2_sidecar_identity_contract_rejected",
                    "OmniAuto 会话身份结论字段缺失或互相矛盾，Worker 已拒绝该候选且未重新解析标题。",
                    error_code="C2_SIDECAR_IDENTITY_CONTRACT_INVALID",
                    metadata={
                        "rejected_count": contract_rejected_count,
                        "rejections": (admission_evidence or {}).get(
                            "contract_rejections"
                        )
                        or [],
                    },
                )
            review_path = self._write_c2_sessions_review(reason=reason, sidecar_payload=sidecar_payload, scan_payload=payload)
            raw_sessions = sidecar_payload.get("sessions") if isinstance(sidecar_payload.get("sessions"), list) else []
            self.c2_last_visible_sessions = [
                item
                for item in self._visible_sessions_with_click_geometry(payload.get("sessions") or [], raw_sessions)
                if isinstance(item, dict)
            ]
            self.c2_last_visible_sessions_monotonic = time.monotonic()
            self._remember_recent_visible_hits(self.c2_last_visible_sessions)
            result = self.api.post_wechat_session_scan_result(binding, payload)
            scan_process_run_id = str(
                (result or {}).get("process_run_id")
                if isinstance(result, dict)
                else ""
            ).strip()
            if scan_process_run_id:
                enqueue_existing_duration(
                    process_run_id=scan_process_run_id,
                    conversation_id=None,
                    stage_name="c2.scan",
                    component="worker",
                    execution_duration_ms=sidecar_scan_duration_ms,
                    status=(
                        "failed" if payload.get("scan_failed") else "succeeded"
                    ),
                    error_code=(
                        str(payload.get("error_code") or "C2_SCAN_FAILED")
                        if payload.get("scan_failed")
                        else None
                    ),
                    trace_id=str(payload.get("scan_id") or "") or None,
                    stage_run_id=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"chejin:c2.scan:{payload.get('scan_id')}",
                        )
                    ),
                )
                schedule_stage_event_upload(self.api, binding)
            for binding_result in (
                result.get("bindings")
                if isinstance(result, dict)
                and isinstance(result.get("bindings"), list)
                else []
            ):
                if not isinstance(binding_result, dict):
                    continue
                server_allowed = bool(
                    binding_result.get("can_ingest_messages")
                )
                append_log(
                    "INFO" if server_allowed else "WARN",
                    (
                        "c2_scan_binding_server_authorized"
                        if server_allowed
                        else "c2_scan_binding_server_rejected"
                    ),
                    (
                        "最新唯一扫描已由后端授权；Worker 未自行恢复暂停绑定。"
                        if server_allowed
                        else "最新扫描未获后端授权，Worker 不会点击该会话。"
                    ),
                    error_code=(
                        str(binding_result.get("error_code") or "") or None
                    ),
                    metadata={
                        "conversation_id": binding_result.get(
                            "conversation_id"
                        ),
                        "remark_code": binding_result.get("remark_code"),
                        "bind_status": binding_result.get("bind_status"),
                        "listen_status": binding_result.get(
                            "listen_status"
                        ),
                        "allow_listening": binding_result.get(
                            "allow_listening"
                        ),
                        "recovery_state": binding_result.get(
                            "recovery_state"
                        ),
                        "server_authorized": server_allowed,
                    },
                )
            self._enqueue_visible_hits(payload, result, sidecar_payload=sidecar_payload)
            self.c2_stats.update(
                {
                    "last_scan_at": payload.get("finished_at"),
                    "last_scan_sessions": len(payload.get("sessions") or []),
                    "last_bound_count": result.get("bound_count") if isinstance(result, dict) else 0,
                    "last_visible_hit_count": len(self.visible_hit_queue),
                    "last_error": payload.get("error_code"),
                }
            )
            self._emit_runtime_process(
                {
                    "event": "scan_completed",
                    "visible_hit_count": len(self.visible_hit_queue),
                    "session_count": self.c2_stats["last_scan_sessions"],
                }
            )
            append_log(
                "INFO",
                "c2_session_scan_reported",
                "微信第一屏会话扫描结果已上报。",
                metadata={
                    "session_count": self.c2_stats["last_scan_sessions"],
                    "bound_count": self.c2_stats["last_bound_count"],
                    "visible_hit_count": self.c2_stats["last_visible_hit_count"],
                    "reason": reason,
                    "sidecar_run_id": payload.get("sidecar_run_id"),
                    "artifact_dir": sidecar_payload.get("artifact_dir"),
                    "screenshot_path": (payload.get("evidence") or {}).get("screenshot"),
                    "review_path": review_path,
                    "session_match_debug": self._visible_session_match_debug("", self.c2_last_visible_sessions),
                },
            )
        except UiLockError as exc:
            self.c2_stats["last_error"] = exc.code
            self._emit_runtime_process(
                {"event": "scan_failed", "error_code": exc.code}
            )
            append_log("WARN", "c2_session_scan_lock_skipped", str(exc), error_code=exc.code, metadata=exc.data)
        except Exception as exc:
            self.c2_stats["last_error"] = str(exc)
            self._emit_runtime_process(
                {
                    "event": "scan_failed",
                    "error_code": type(exc).__name__,
                }
            )
            append_log("ERROR", "c2_session_scan_failed", str(exc))
            self.on_error(f"C2 会话扫描失败：{exc}")
        finally:
            if lease:
                self._release_current_ui_lock(reason="session_scan_finished")
            self.current_step = None

    def _enqueue_visible_hits(self, payload: dict[str, Any], result: dict[str, Any] | None, *, sidecar_payload: dict[str, Any] | None = None) -> None:
        if not isinstance(result, dict):
            return
        sessions = payload.get("sessions") if isinstance(payload.get("sessions"), list) else []
        session_by_key = {str(item.get("rpa_session_key") or ""): item for item in sessions if isinstance(item, dict)}
        raw_sessions = (sidecar_payload or {}).get("sessions") if isinstance((sidecar_payload or {}).get("sessions"), list) else []
        raw_session_by_key = {str(item.get("session_key") or ""): item for item in raw_sessions if isinstance(item, dict)}
        queued_keys = {self._target_dedupe_key(item) for item in self.visible_hit_queue}
        for item in result.get("bindings") or []:
            if not isinstance(item, dict) or not item.get("can_ingest_messages"):
                continue
            conversation_id = str(item.get("conversation_id") or "")
            rpa_session_key = str(item.get("rpa_session_key") or "")
            session = session_by_key.get(rpa_session_key) or self._visible_session_for_binding(item, sessions) or {}
            visible_session_key = str(session.get("rpa_session_key") or "").strip()
            target = WechatReadTarget.from_api(
                {
                    "conversation_id": conversation_id,
                    "lead_id": item.get("lead_id"),
                    "sales_id": item.get("sales_id"),
                    "remark_code": item.get("remark_code"),
                    "rpa_session_key": visible_session_key or rpa_session_key,
                    "display_name": item.get("display_name") or session.get("display_name"),
                    "row_fingerprint": item.get("row_fingerprint") or session.get("row_fingerprint"),
                    "ocr_confidence": item.get("ocr_confidence") if item.get("ocr_confidence") is not None else session.get("ocr_confidence"),
                    "read_reason": "visible_hit",
                    "visible_session_candidate": self._sidecar_visible_session_candidate(raw_session_by_key.get(visible_session_key or rpa_session_key) or session),
                    "visible_session_source": "first_screen_session_scan",
                    "local_unread_hint": session.get("unread_hint") is True,
                    "next_read_due_at": item.get("next_read_due_at"),
                }
            )
            dedupe_key = self._target_dedupe_key(target)
            if not dedupe_key or dedupe_key in queued_keys or dedupe_key in self.c2_round_processed_conversation_ids:
                continue
            if self._c2_read_cooldown_remaining(dedupe_key) > 0:
                continue
            if (
                self._c2_read_success_cooldown_remaining(dedupe_key) > 0
                and not (
                    isinstance(target.raw, dict)
                    and target.raw.get("local_unread_hint") is True
                )
            ):
                continue
            if self._validate_read_target(target) == "C2_READ_TARGET_NOT_DUE":
                continue
            self.visible_hit_queue.append(target)
            queued_keys.add(dedupe_key)

    def _visible_session_for_binding(self, binding_item: dict[str, Any], sessions: list[Any]) -> dict[str, Any] | None:
        remark_code = str(binding_item.get("remark_code") or "").strip().upper()
        remark_matches = self._visible_sessions_for_remark_code(remark_code, sessions)
        if len(remark_matches) == 1:
            return remark_matches[0]
        return None

    def _visible_sessions_for_remark_code(self, remark_code: str, sessions: list[Any]) -> list[dict[str, Any]]:
        code = str(remark_code or "").strip().upper()
        if not code:
            return []
        normalized_code = self._code_match_text(code)
        matches: list[dict[str, Any]] = []
        for session in sessions:
            if not isinstance(session, dict):
                continue
            candidates = {str(item or "").strip().upper() for item in (session.get("remark_code_candidates") or [])}
            normalized_candidates = {self._code_match_text(item) for item in candidates if item}
            if code in candidates or normalized_code in normalized_candidates:
                matches.append(session)
        return matches

    @staticmethod
    def _code_match_text(value: Any) -> str:
        return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())

    def _visible_session_identity_text(self, session: dict[str, Any]) -> str:
        parts: list[str] = []
        for key in ("display_name", "name", "title", "last_message_preview", "content", "preview"):
            value = session.get(key)
            if value:
                parts.append(str(value))
        candidates = session.get("remark_code_candidates")
        if isinstance(candidates, list):
            parts.extend(str(item) for item in candidates if item)
        row = session.get("row_fingerprint")
        if isinstance(row, dict):
            parts.extend(str(value) for value in row.values() if value)
        elif row:
            parts.append(str(row))
        return " ".join(parts).upper()

    def _visible_session_match_debug(self, remark_code: str, sessions: list[Any]) -> list[dict[str, Any]]:
        code = str(remark_code or "").strip().upper()
        normalized_code = self._code_match_text(code)
        debug_rows: list[dict[str, Any]] = []
        for index, session in enumerate(sessions[:12]):
            if not isinstance(session, dict):
                continue
            candidates = [str(item or "").strip().upper() for item in (session.get("remark_code_candidates") or []) if str(item or "").strip()]
            normalized_candidates = [self._code_match_text(item) for item in candidates if item]
            identity = self._visible_session_identity_text(session)
            normalized_identity = self._code_match_text(identity)
            debug_rows.append(
                {
                    "index": index,
                    "display_name": session.get("display_name") or session.get("name") or session.get("title"),
                    "rpa_session_key": session.get("rpa_session_key"),
                    "remark_code_candidates": candidates,
                    "last_message_preview": session.get("last_message_preview") or session.get("content") or session.get("preview"),
                    "ocr_confidence": session.get("ocr_confidence"),
                    "row_fingerprint": session.get("row_fingerprint"),
                    "identity_text": identity[:300],
                    "normalized_identity_text": normalized_identity[:300],
                    "match_checks": {
                        "candidate_exact": code in candidates,
                        "candidate_normalized": normalized_code in normalized_candidates,
                        "identity_exact_contains": bool(code and code in identity),
                        "identity_normalized_contains": bool(normalized_code and normalized_code in normalized_identity),
                    },
                }
            )
        return debug_rows

    def _authorized_visible_hit_target(
        self,
        visible_target: WechatReadTarget,
        authorized_target: WechatReadTarget,
    ) -> WechatReadTarget:
        return WechatReadTarget(
            conversation_id=authorized_target.conversation_id,
            lead_id=authorized_target.lead_id,
            sales_id=authorized_target.sales_id,
            rpa_session_key=visible_target.rpa_session_key or authorized_target.rpa_session_key,
            display_name=visible_target.display_name or authorized_target.display_name,
            remark_code=authorized_target.remark_code,
            row_fingerprint=visible_target.row_fingerprint or authorized_target.row_fingerprint,
            ocr_confidence=(
                visible_target.ocr_confidence
                if visible_target.ocr_confidence is not None
                else authorized_target.ocr_confidence
            ),
            read_reason=authorized_target.read_reason,
            authorization_revision=authorized_target.authorization_revision,
            process_run_id=authorized_target.process_run_id,
            unread_generation=authorized_target.unread_generation,
            raw={
                **authorized_target.raw,
                **visible_target.raw,
                "authorization_revision": authorized_target.authorization_revision,
                "authorization_read_reason": authorized_target.read_reason,
                "visible_hit": True,
            },
        )

    def _drain_visible_hit_queue(
        self,
        binding: Binding,
        *,
        authorized_targets: list[WechatReadTarget] | None = None,
    ) -> None:
        if not self._can_start_new_flow(binding):
            return
        authorized_by_key = {
            self._target_dedupe_key(target): target
            for target in (authorized_targets or [])
            if self._target_dedupe_key(target)
        }
        if authorized_targets is not None and not authorized_by_key:
            dropped = len(self.visible_hit_queue)
            self.visible_hit_queue.clear()
            if dropped:
                append_log("INFO", "c2_visible_hit_queue_cleared", "后端 read-targets 为空，已清空本地第一屏命中读取队列。", metadata={"dropped_count": dropped})
            return
        while self.visible_hit_queue:
            if not self._can_start_new_flow(binding):
                return
            visible_target = self.visible_hit_queue.pop(0)
            dedupe_key = self._target_dedupe_key(visible_target)
            authorized_target = authorized_by_key.get(dedupe_key)
            if authorized_targets is not None and authorized_target is None:
                append_log(
                    "INFO",
                    "c2_visible_hit_not_allowed",
                    "第一屏命中目标不在后端 read-targets 许可内，跳过本地读取。",
                    error_code="C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                    metadata={"conversation_id": visible_target.conversation_id, "remark_code": visible_target.remark_code, "read_reason": visible_target.read_reason},
                )
                self.c2_round_processed_conversation_ids.add(dedupe_key)
                continue
            if authorized_target is None or not authorized_target.authorization_revision:
                self.c2_stats["last_error"] = "C2_TARGET_AUTHORIZATION_REVISION_MISSING"
                append_log(
                    "WARN",
                    "c2_visible_hit_authorization_missing",
                    "第一屏命中目标缺少当前 read-targets 授权版本，已禁止读取。",
                    error_code="C2_TARGET_AUTHORIZATION_REVISION_MISSING",
                    metadata={
                        "conversation_id": visible_target.conversation_id,
                        "remark_code": visible_target.remark_code,
                        "read_reason": visible_target.read_reason,
                    },
                )
                self.c2_round_processed_conversation_ids.add(dedupe_key)
                continue
            target = self._authorized_visible_hit_target(visible_target, authorized_target)
            if self._c2_identity_quarantine(target):
                self.c2_round_processed_conversation_ids.add(dedupe_key)
                continue
            if dedupe_key in self.c2_round_processed_conversation_ids:
                continue
            cooldown_remaining = self._c2_read_cooldown_remaining(dedupe_key)
            if cooldown_remaining > 0:
                append_log("INFO", "c2_visible_hit_cooldown", "C2 第一屏命中目标刚失败过，冷却期内跳过本轮重试。", metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code, "read_reason": target.read_reason, "next_read_due_at": target.raw.get("next_read_due_at") if isinstance(target.raw, dict) else None, "cooldown_remaining_seconds": round(cooldown_remaining, 1)})
                continue
            success_cooldown_remaining = self._c2_read_success_cooldown_remaining(
                dedupe_key
            )
            if success_cooldown_remaining > 0 and not (
                target.read_reason == "visible_unread"
                and isinstance(target.raw, dict)
                and target.raw.get("local_unread_hint") is True
            ):
                append_log(
                    "INFO",
                    "c2_visible_hit_success_cooldown",
                    "C2 第一屏命中目标刚完成读取，成功冷却内不重复加入读取。",
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "cooldown_remaining_seconds": round(
                            success_cooldown_remaining, 1
                        ),
                    },
                )
                continue
            validation_error = self._validate_read_target(target)
            if validation_error:
                self.c2_stats["last_error"] = validation_error
                append_log(
                    "WARN",
                    "c2_visible_hit_skipped",
                    "C2 第一屏命中目标校验未通过，已跳过。",
                    error_code=validation_error,
                    metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code, "read_reason": target.read_reason, "next_read_due_at": target.raw.get("next_read_due_at") if isinstance(target.raw, dict) else None},
                )
                continue
            self.c2_round_processed_conversation_ids.add(dedupe_key)
            read_result = self._read_one_wechat_target(binding, target, current_step="visible_hit_message_read", enforce_read_targets=True)
            if read_result.get("ok"):
                self.c2_read_failure_cooldowns.pop(dedupe_key, None)
                self._mark_c2_read_success_cooldown(dedupe_key)
            else:
                self._mark_c2_read_failure_cooldown(dedupe_key, read_result.get("error_code"))

    def _fetch_read_targets(self, binding: Binding) -> list[WechatReadTarget]:
        try:
            return list(self.api.get_wechat_read_targets(binding, limit=CONFIG.c2_read_targets_limit) or [])
        except Exception as exc:
            self.c2_stats["last_error"] = str(exc)
            append_log("ERROR", "c2_read_targets_failed", str(exc))
            return []

    def _read_state_target_queue(self, binding: Binding, *, targets: list[WechatReadTarget] | None = None) -> None:
        if not self._can_start_new_flow(binding):
            return
        targets = self._fetch_read_targets(binding) if targets is None else list(targets)
        self.c2_read_allowlist_keys = {self._target_dedupe_key(target) for target in targets}
        self.c2_stats["last_state_target_count"] = len(targets)
        for target in targets:
            if not self._can_start_new_flow(binding):
                break
            dedupe_key = self._target_dedupe_key(target)
            if self._c2_identity_quarantine(target):
                self.c2_round_processed_conversation_ids.add(dedupe_key)
                continue
            if dedupe_key in self.c2_round_processed_conversation_ids:
                append_log("INFO", "c2_state_target_deduped", "状态机读取目标已在本轮第一屏命中读取中处理，跳过重复读取。", metadata={"conversation_id": target.conversation_id, "rpa_session_key": target.rpa_session_key, "remark_code": target.remark_code, "read_reason": target.read_reason})
                continue
            cooldown_remaining = self._c2_read_cooldown_remaining(dedupe_key)
            if cooldown_remaining > 0:
                append_log("INFO", "c2_state_target_cooldown", "C2 定向读取刚失败过，冷却期内跳过本轮重试。", metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code, "read_reason": target.read_reason, "next_read_due_at": target.raw.get("next_read_due_at") if isinstance(target.raw, dict) else None, "cooldown_remaining_seconds": round(cooldown_remaining, 1)})
                continue
            success_cooldown_remaining = self._c2_read_success_cooldown_remaining(dedupe_key)
            if success_cooldown_remaining > 0:
                append_log("INFO", "c2_state_target_success_cooldown", "C2 读取目标刚完成，短冷却内跳过重复读取。", metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code, "cooldown_remaining_seconds": round(success_cooldown_remaining, 1)})
                continue
            validation_error = self._validate_read_target(target)
            if validation_error:
                self.c2_stats["last_error"] = validation_error
                append_log("WARN", "c2_read_target_skipped", "C2 读取目标校验未通过，已跳过。", error_code=validation_error, metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code, "read_reason": target.read_reason, "next_read_due_at": target.raw.get("next_read_due_at") if isinstance(target.raw, dict) else None})
                continue
            if self._high_priority_active():
                append_log("INFO", "c2_message_read_interrupted", "C2 消息读取被高优先级微信动作中断。", error_code="SCAN_INTERRUPTED_BY_HIGH_PRIORITY_ACTION", metadata={"conversation_id": target.conversation_id})
                self.c2_stats["last_error"] = "SCAN_INTERRUPTED_BY_HIGH_PRIORITY_ACTION"
                break
            read_result = self._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=True)
            if read_result.get("ok"):
                self.c2_round_processed_conversation_ids.add(dedupe_key)
                self.c2_read_failure_cooldowns.pop(dedupe_key, None)
                self._mark_c2_read_success_cooldown(dedupe_key)
            else:
                self._mark_c2_read_failure_cooldown(dedupe_key, read_result.get("error_code"))

    def _c2_identity_quarantine(
        self,
        target: WechatReadTarget,
    ) -> dict[str, Any]:
        state = load_c2_state(
            f"identity_quarantine:{target.conversation_id}"
        )
        if not state or state.get("active") is not True:
            return {}
        append_log(
            "WARN",
            "c2_identity_quarantine_skipped",
            "当前客户存在不可自动恢复的身份碰撞；仅隔离该客户，继续处理其他短码。",
            error_code=str(
                state.get("error_code")
                or "MESSAGE_IDENTITY_COLLISION_NOT_REKEYABLE"
            ),
            metadata={
                "conversation_id": target.conversation_id,
                "remark_code": target.remark_code,
                "outbox_id": state.get("outbox_id"),
            },
        )
        return state

    def _visible_target_from_recent_scan(
        self,
        target: WechatReadTarget,
        *,
        sessions: list[dict[str, Any]] | None = None,
        seen_at: float | None = None,
    ) -> WechatReadTarget | None:
        if target.read_reason == "visible_hit":
            return None
        scan_seen_at = float(self.c2_last_visible_sessions_monotonic if seen_at is None else seen_at)
        visible_sessions = (
            sessions
            if isinstance(sessions, list) and time.monotonic() - scan_seen_at <= C2_RECENT_VISIBLE_CACHE_TTL_SECONDS
            else self.c2_last_visible_sessions
            if isinstance(self.c2_last_visible_sessions, list) and time.monotonic() - scan_seen_at <= C2_RECENT_VISIBLE_CACHE_TTL_SECONDS
            else []
        )
        session = self._visible_session_for_binding(
            {
                "remark_code": target.remark_code,
                "display_name": target.display_name,
                "rpa_session_key": target.rpa_session_key,
            },
            visible_sessions,
        )
        if not session:
            self._prune_recent_visible_hits()
            recent = self.c2_recent_visible_hits_by_remark_code.get(str(target.remark_code or "").strip().upper())
            if isinstance(recent, dict):
                recent_session = recent.get("session")
                recent_seen_at = float(recent.get("seen_at") or 0)
                if isinstance(recent_session, dict) and time.monotonic() - recent_seen_at <= C2_RECENT_VISIBLE_CACHE_TTL_SECONDS:
                    session = recent_session
                    scan_seen_at = recent_seen_at
            if not session:
                return None
        visible_session_key = str(session.get("rpa_session_key") or "").strip()
        if not visible_session_key:
            return None
        append_log(
            "INFO",
            "c2_state_target_promoted_to_visible_hit",
            "状态机读取目标仍在最近第一屏扫描结果中，优先按首屏命中读取，避免重复定向搜索。",
            metadata={
                "conversation_id": target.conversation_id,
                "remark_code": target.remark_code,
                "rpa_session_key": visible_session_key,
                "display_name": session.get("display_name") or target.display_name,
            },
        )
        return WechatReadTarget(
            conversation_id=target.conversation_id,
            lead_id=target.lead_id,
            sales_id=target.sales_id,
            rpa_session_key=visible_session_key,
            display_name=str(session.get("display_name") or target.display_name or ""),
            remark_code=target.remark_code,
            row_fingerprint=session.get("row_fingerprint") or target.row_fingerprint,
            ocr_confidence=session.get("ocr_confidence") if session.get("ocr_confidence") is not None else target.ocr_confidence,
            read_reason=target.read_reason,
            authorization_revision=target.authorization_revision,
            unread_generation=target.unread_generation,
            raw={
                **target.raw,
                "visible_session_candidate": self._sidecar_visible_session_candidate(session),
                "visible_session_source": "recent_visible_scan",
                "authorization_read_reason": target.read_reason,
                "visible_hit": True,
            },
        )

    def _backend_still_allows_read_target(self, binding: Binding, target: WechatReadTarget) -> bool:
        if not target.authorization_revision:
            self.c2_stats["last_error"] = "C2_TARGET_AUTHORIZATION_REVISION_MISSING"
            append_log(
                "WARN",
                "c2_message_read_cancelled_by_missing_authorization",
                "C2 V3 读取目标缺少授权版本，已在操作微信前取消。",
                error_code="C2_TARGET_AUTHORIZATION_REVISION_MISSING",
                metadata={
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "read_reason": target.read_reason,
                },
            )
            return False
        allowed = self._backend_still_allows_read_target_lightweight(
            binding,
            target,
        )
        if not allowed:
            self.c2_stats["last_error"] = "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS"
            append_log(
                "INFO",
                "c2_message_read_cancelled_by_read_targets",
                "后端轻量授权已不再允许该目标，取消本地消息读取并释放 UI 锁。",
                error_code="C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                metadata={
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "read_reason": target.read_reason,
                    "authorization_key": self._target_authorization_key(
                        target
                    ),
                    "target_key": self._target_dedupe_key(target),
                },
            )
        return allowed

    def _backend_still_allows_read_target_lightweight(
        self,
        binding: Binding,
        target: WechatReadTarget,
    ) -> bool:
        """Check one authorization ticket without downloading target/history lists."""

        try:
            continuation = self._target_batch_continuation(target)
            authorization = self.api.get_wechat_read_authorization(
                binding,
                target.conversation_id,
                continuation_batch_id=str(
                    continuation.get("batch_id") or ""
                )
                or None,
                continuation_token=str(continuation.get("token") or "")
                or None,
            )
        except Exception as exc:
            append_log(
                "WARN",
                "c2_lightweight_authorization_failed",
                "长动作轻量授权检查失败，按不允许继续处理。",
                error_code="C2_AUTHORIZATION_CHECK_FAILED",
                metadata={
                    "conversation_id": target.conversation_id,
                    "exception_type": type(exc).__name__,
                },
            )
            return False
        allowed = self._batch_authorization_allows_target(
            {"authorization": authorization},
            target,
        )
        if allowed:
            self._merge_read_authorization_checkpoint(target, authorization)
        else:
            append_log(
                "INFO",
                "c2_read_authorization_backed_off",
                "后端当前未授权读取，未执行微信操作。",
                metadata={
                    "conversation_id": target.conversation_id,
                    "read_reason": target.read_reason,
                    "recovery_decision": authorization.get(
                        "recovery_decision"
                    ),
                    "next_read_due_at": authorization.get(
                        "next_read_due_at"
                    ),
                },
            )
        return allowed

    @staticmethod
    def _merge_read_authorization_checkpoint(
        target: WechatReadTarget,
        authorization: dict[str, Any],
    ) -> None:
        """Merge the latest backend-owned identity floor before a UI read."""

        if not isinstance(target.raw, dict):
            target.raw = {}
        checkpoint = authorization.get("identity_checkpoint")
        if isinstance(checkpoint, dict):
            target.raw["identity_checkpoint"] = json.loads(
                json.dumps(checkpoint, ensure_ascii=False, default=str)
            )
        target.raw["next_read_due_at"] = authorization.get(
            "next_read_due_at"
        )
        target.raw["read_authorization_refreshed_at"] = (
            datetime.now(timezone.utc).isoformat()
        )

    def _target_batch_continuation(
        self,
        target: WechatReadTarget,
    ) -> dict[str, Any]:
        if not isinstance(target.raw, dict):
            return {}
        value = target.raw.get("batch_continuation")
        return dict(value) if isinstance(value, dict) else {}

    def _apply_ingest_batch_continuation_to_target(
        self,
        message_batch: dict[str, Any],
        target: WechatReadTarget,
    ) -> bool:
        """Promote an active-read result to its batch-scoped continuation.

        The backend consumes the original read authorization when ingest is
        settled.  Brain waiting must therefore start with the continuation
        returned by that same ingest response; otherwise the first polling
        guard would incorrectly test the already-consumed active-read ticket.
        """

        continuation = (
            message_batch.get("continuation")
            if isinstance(message_batch.get("continuation"), dict)
            else {}
        )
        batch_id = str(message_batch.get("batch_id") or "").strip()
        continuation_batch_id = str(
            continuation.get("batch_id") or ""
        ).strip()
        token = str(continuation.get("token") or "").strip()
        revision = str(
            continuation.get("authorization_revision") or ""
        ).strip()
        read_reason = str(continuation.get("read_reason") or "").strip()
        expected_reason = str(target.read_reason or "").strip()
        if isinstance(target.raw, dict):
            expected_reason = str(
                target.raw.get("authorization_read_reason")
                or expected_reason
            ).strip()
        if (
            not batch_id
            or continuation_batch_id != batch_id
            or not token
            or revision != str(target.authorization_revision or "").strip()
            or read_reason != expected_reason
        ):
            return False
        if not isinstance(target.raw, dict):
            target.raw = {}
        target.raw["batch_continuation"] = {
            "batch_id": batch_id,
            "token": token,
        }
        target.raw["authorization_read_reason"] = read_reason
        return True

    def _apply_batch_continuation_to_target(
        self,
        status: dict[str, Any],
        target: WechatReadTarget,
    ) -> bool:
        authorization = (
            status.get("authorization")
            if isinstance(status.get("authorization"), dict)
            else {}
        )
        batch_id = str(
            authorization.get("batch_id")
            or status.get("batch_id")
            or ""
        ).strip()
        token = str(authorization.get("continuation_token") or "").strip()
        if (
            authorization.get("authorization_scope") != "batch_continuation"
            or not batch_id
            or not token
        ):
            return False
        if not isinstance(target.raw, dict):
            target.raw = {}
        target.raw["batch_continuation"] = {
            "batch_id": batch_id,
            "token": token,
        }
        frozen_reason = str(authorization.get("read_reason") or "").strip()
        if frozen_reason:
            target.raw["authorization_read_reason"] = frozen_reason
            target.read_reason = frozen_reason
        frozen_revision = str(
            authorization.get("authorization_revision") or ""
        ).strip()
        if frozen_revision:
            target.authorization_revision = frozen_revision
        return True

    def _backend_still_allows_read_target_for_voice(
        self,
        binding: Binding,
        target: WechatReadTarget,
        *,
        read_run_id: str,
    ) -> bool:
        if not self._can_continue_inflight_flow(read_run_id):
            return False
        if not self._backend_still_allows_read_target(binding, target):
            return False
        guard_seconds = max(0.0, float(self.c2_stop_guard_before_voice_seconds))
        if guard_seconds <= 0:
            return True
        self.stop_event.wait(guard_seconds)
        if not self._can_continue_inflight_flow(read_run_id):
            self.c2_stats["last_error"] = "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS"
            append_log(
                "INFO",
                "c2_message_read_cancelled_by_local_stop",
                "客户端停止信号已触发，取消进入语音转写。",
                error_code="C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                metadata={
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "read_reason": target.read_reason,
                },
            )
            return False
        return self._backend_still_allows_read_target(binding, target)

    def _target_dedupe_key(self, target: WechatReadTarget) -> str:
        if target.conversation_id and target.remark_code:
            return f"conversation:{target.conversation_id}:remark_code:{target.remark_code}"
        return f"invalid:{target.conversation_id}:{target.remark_code or ''}:{target.rpa_session_key}:{target.display_name}"

    @staticmethod
    def _conversation_flow_outcome(
        brain_result: dict[str, Any] | None,
        *,
        had_message_batch: bool,
    ) -> tuple[bool, str, str | None]:
        if not had_message_batch:
            return True, "facts_ingested_no_batch", None
        if not isinstance(brain_result, dict):
            return False, "technical_failure", "C3_FLOW_RESULT_MISSING"
        if brain_result.get("ok") is not True:
            return (
                False,
                "technical_failure",
                str(brain_result.get("error_code") or "C3_CONVERSATION_FLOW_FAILED"),
            )
        if brain_result.get("sent") is True:
            return True, "reply_sent", None
        batch = (
            brain_result.get("batch")
            if isinstance(brain_result.get("batch"), dict)
            else {}
        )
        decision = str(batch.get("decision") or "").strip()
        reason = str(brain_result.get("reason") or "").strip()
        return True, decision or reason or "completed_without_send", None

    def _target_authorization_key(self, target: WechatReadTarget) -> str:
        reason = ""
        if isinstance(target.raw, dict):
            reason = str(target.raw.get("authorization_read_reason") or "").strip()
        reason = reason or str(target.read_reason or "").strip()
        if target.authorization_revision:
            return (
                f"authorization_revision:{target.conversation_id}:"
                f"{target.authorization_revision}:read_reason:{reason}"
            )
        if target.conversation_id and target.remark_code and reason and reason != "visible_hit":
            return f"authorization:{target.conversation_id}:remark_code:{target.remark_code}:read_reason:{reason}"
        return self._target_dedupe_key(target)

    def _batch_authorization_allows_target(
        self,
        status: dict[str, Any],
        target: WechatReadTarget,
    ) -> bool:
        authorization = (
            status.get("authorization")
            if isinstance(status.get("authorization"), dict)
            else {}
        )
        reason = ""
        if isinstance(target.raw, dict):
            reason = str(target.raw.get("authorization_read_reason") or "").strip()
        reason = reason or str(target.read_reason or "").strip()
        continuation = self._target_batch_continuation(target)
        if continuation:
            return bool(
                authorization.get("allowed") is True
                and authorization.get("authorization_scope")
                == "batch_continuation"
                and str(authorization.get("batch_id") or "")
                == str(continuation.get("batch_id") or "")
                and str(authorization.get("continuation_token") or "")
                == str(continuation.get("token") or "")
                and str(authorization.get("conversation_id") or "")
                == str(target.conversation_id or "")
                and str(authorization.get("authorization_revision") or "")
                == str(target.authorization_revision or "")
                and str(authorization.get("read_reason") or "") == reason
            )
        return bool(
            authorization.get("allowed") is True
            and str(authorization.get("conversation_id") or "")
            == str(target.conversation_id or "")
            and str(authorization.get("authorization_revision") or "")
            == str(target.authorization_revision or "")
            and str(authorization.get("read_reason") or "") == reason
        )

    def _validate_read_target(self, target: WechatReadTarget) -> str | None:
        if not target.conversation_id:
            return "C2_TARGET_CONVERSATION_ID_MISSING"
        if not target.remark_code:
            return "C2_TARGET_REMARK_CODE_MISSING"
        if not is_formal_c2_remark_code(target.remark_code):
            return "C2_TARGET_REMARK_CODE_INVALID"
        if target.ocr_confidence is not None and target.ocr_confidence < CONFIG.c2_message_min_ocr_confidence:
            return "C2_TARGET_OCR_LOW_CONFIDENCE"
        next_read_due_at = (
            target.raw.get("next_read_due_at")
            if isinstance(target.raw, dict)
            else None
        )
        if next_read_due_at:
            try:
                due_at = datetime.fromisoformat(
                    str(next_read_due_at).replace("Z", "+00:00")
                )
                if due_at.tzinfo is None:
                    due_at = due_at.replace(tzinfo=timezone.utc)
                if due_at > datetime.now(timezone.utc):
                    return "C2_READ_TARGET_NOT_DUE"
            except ValueError:
                return "C2_READ_TARGET_DUE_AT_INVALID"
        return None

    def _c2_read_cooldown_remaining(self, dedupe_key: str) -> float:
        until = float(self.c2_read_failure_cooldowns.get(dedupe_key) or 0)
        remaining = until - time.monotonic()
        if remaining <= 0:
            self.c2_read_failure_cooldowns.pop(dedupe_key, None)
            return 0.0
        return remaining

    def _c2_read_success_cooldown_remaining(self, dedupe_key: str) -> float:
        until = float(self.c2_read_success_cooldowns.get(dedupe_key) or 0)
        remaining = until - time.monotonic()
        if remaining <= 0:
            self.c2_read_success_cooldowns.pop(dedupe_key, None)
            return 0.0
        return remaining

    def _mark_c2_read_failure_cooldown(self, dedupe_key: str, error_code: Any = None) -> None:
        cooldown = max(0.0, float(CONFIG.c2_message_failure_cooldown_seconds))
        if cooldown <= 0:
            return
        self.c2_read_failure_cooldowns[dedupe_key] = time.monotonic() + cooldown
        append_log("INFO", "c2_read_failure_cooldown_started", "C2 定向读取失败，已进入短冷却，避免反复重置微信搜索框。", metadata={"target_key": dedupe_key, "error_code": error_code, "cooldown_seconds": cooldown})

    def _mark_c2_read_success_cooldown(self, dedupe_key: str) -> None:
        cooldown = max(float(CONFIG.c2_message_read_interval_seconds), 20.0)
        self.c2_read_success_cooldowns[dedupe_key] = time.monotonic() + cooldown
        append_log("INFO", "c2_read_success_cooldown_started", "C2 读取完成，已进入短冷却，避免服务端状态更新前重复读取同一目标。", metadata={"target_key": dedupe_key, "cooldown_seconds": cooldown})

    def _mark_ingest_ledger_confirmed(self, payload: dict[str, Any], result: dict[str, Any]) -> None:
        evidence = (
            payload.get("evidence")
            if isinstance(payload.get("evidence"), dict)
            else {}
        )
        requires_per_message_confirmation = bool(
            evidence.get("recovery_requires_per_message_confirmation")
        )
        accepted = {
            str(item.get("source_message_key") or "").strip()
            for item in (result.get("results") or [])
            if isinstance(item, dict)
            and item.get("ingest_result")
            in {"ingested", "duplicated", "technical_terminal"}
        }
        if (
            not requires_per_message_confirmation
            and not result.get("results")
            and int(result.get("ignored_count") or 0) == 0
        ):
            accepted = {
                str(item.get("source_message_key") or "").strip()
                for item in (payload.get("messages") or [])
                if isinstance(item, dict)
            }
        if not requires_per_message_confirmation:
            accepted.update(
                str(value).strip()
                for value in (evidence.get("failed_voice_source_keys") or [])
                if str(value).strip()
            )
        mark_c2_ledger_ingested(
            str(payload.get("conversation_id") or ""),
            sorted(value for value in accepted if value),
        )
        rejected = {
            str(item.get("source_message_key") or "").strip()
            for item in (result.get("results") or [])
            if isinstance(item, dict) and item.get("ingest_result") == "ignored"
        }
        if not requires_per_message_confirmation:
            mark_c2_ledger_rejected(
                str(payload.get("conversation_id") or ""),
                sorted(value for value in rejected if value),
            )

    def _attempt_c2_outbox_delivery(
        self,
        *,
        binding: Binding,
        payload: dict[str, Any],
        outbox_id: str,
        operation: str,
    ) -> dict[str, Any]:
        outbox_entry = load_c2_outbox_entry(outbox_id) or {}
        current_state = str(
            outbox_entry.get("status") or "waiting"
        )
        if current_state == "confirmed":
            return {
                "ok": True,
                "outbox_id": outbox_id,
                "result": {},
                "already_confirmed": True,
            }
        mark_c2_outbox_attempt(outbox_id)
        try:
            settlement_token: str | None = None
            if str(payload.get("authorization_scope") or "") == "fact_settlement":
                evidence = (
                    payload.get("evidence")
                    if isinstance(payload.get("evidence"), dict)
                    else {}
                )
                authorization = self.api.get_wechat_read_authorization(
                    binding,
                    str(payload.get("conversation_id") or ""),
                    recovery_transaction_id=str(
                        evidence.get("recovery_transaction_id") or ""
                    ),
                    action_kind=str(evidence.get("action_kind") or ""),
                    source_message_key_digest=str(
                        evidence.get("source_message_key_digest") or ""
                    ),
                    original_authorization_revision=str(
                        payload.get("authorization_revision") or ""
                    ),
                )
                if (
                    str(authorization.get("recovery_decision") or "")
                    != "settle_without_ui"
                    or str(authorization.get("settlement_mode") or "")
                    != str(evidence.get("settlement_mode") or "")
                ):
                    raise ApiError(
                        "C2_FACT_SETTLEMENT_RETRY_LATER",
                        "后端尚未授权无界面事实结算",
                        409,
                    )
                settlement_token = str(
                    authorization.get("settlement_token") or ""
                ).strip()
                if not settlement_token:
                    raise ApiError(
                        "C2_SETTLEMENT_TOKEN_MISSING",
                        "后端未返回事实结算凭据",
                        409,
                    )
            process_run_id = load_process_run(
                str(payload.get("read_run_id") or "")
            )
            ingest_kwargs: dict[str, Any] = {}
            if settlement_token:
                ingest_kwargs["settlement_token"] = settlement_token
            if process_run_id:
                ingest_kwargs["process_run_id"] = process_run_id
            result = self.api.post_wechat_messages_ingest(
                binding,
                payload,
                **ingest_kwargs,
            )
        except Exception as exc:
            error_code = str(
                exc.code if isinstance(exc, ApiError) else type(exc).__name__
            )
            if error_code == "MESSAGE_IDENTITY_COLLISION_NOT_REKEYABLE":
                conversation_id = str(
                    payload.get("conversation_id") or ""
                )
                mark_c2_outbox_identity_quarantined(
                    outbox_id,
                    error_code,
                )
                save_c2_state(
                    f"identity_quarantine:{conversation_id}",
                    {
                        "active": True,
                        "error_code": error_code,
                        "outbox_id": outbox_id,
                        "conversation_id": conversation_id,
                        "read_run_id": payload.get("read_run_id"),
                        "source_message_keys": sorted(
                            {
                                str(item.get("source_message_key") or "")
                                for item in (payload.get("messages") or [])
                                if isinstance(item, dict)
                                and str(item.get("source_message_key") or "")
                            }
                        ),
                        "quarantined_at": self._utc_now_iso(),
                    },
                )
                append_log(
                    "ERROR",
                    "c2_identity_collision_quarantined",
                    "消息身份碰撞无法安全换号；已保留 Outbox 证据并仅隔离当前客户，不再自动重试。",
                    error_code=error_code,
                    metadata={
                        "outbox_id": outbox_id,
                        "conversation_id": conversation_id,
                        "backend_error_response": (
                            redact_diagnostic(
                                {
                                    "code": exc.code,
                                    "message": str(exc),
                                    "status_code": exc.status_code,
                                    "trace_id": exc.trace_id,
                                    "data": exc.data,
                                }
                            )
                            if isinstance(exc, ApiError)
                            else None
                        ),
                    },
                    force_incident=True,
                )
                return {
                    "ok": False,
                    "outbox_id": outbox_id,
                    "error_code": error_code,
                    "exception": exc,
                    "recovery_action": "identity_quarantined",
                    "conversation_quarantined": True,
                }
            recovery_action = classify_outbox_recovery(exc)
            next_state = transition_outbox_state(
                current_state=str(outbox_entry.get("status") or "waiting"),
                event=recovery_action,
                attempt_count=int(outbox_entry.get("attempt_count") or 0) + 1,
                refresh_attempt_count=(
                    int(outbox_entry.get("refresh_attempt_count") or 0) + 1
                ),
            )
            if next_state == "rebuild_pending":
                transition_c2_outbox(
                    outbox_id,
                    status="rebuild_pending",
                    error=error_code,
                )
                try:
                    rebuilt = rebuild_invalid_media_as_failed(
                        payload,
                        error_code=error_code,
                        source_message_key=str(
                            exc.data.get("source_message_key") or ""
                        )
                        if isinstance(exc, ApiError)
                        and isinstance(exc.data, dict)
                        else "",
                    )
                    rebuild_c2_outbox_payload(outbox_id, rebuilt)
                except Exception as rebuild_exc:
                    transition_c2_outbox(
                        outbox_id,
                        status="capability_paused",
                        error=str(
                            rebuild_exc
                            if isinstance(rebuild_exc, ValueError)
                            else type(rebuild_exc).__name__
                        ),
                    )
                    return {
                        "ok": False,
                        "outbox_id": outbox_id,
                        "error_code": error_code,
                        "exception": exc,
                        "capability_paused": True,
                        "recovery_action": "capability_paused",
                    }
                append_log(
                    "WARN",
                    "c2_outbox_rebuilt_failed_media_fact",
                    "后端拒绝了不完整媒体结果；已保留同一消息身份并重建为明确失败事实，等待重新入库。",
                    error_code=error_code,
                    metadata={
                        "outbox_id": outbox_id,
                        "conversation_id": payload.get("conversation_id"),
                    },
                )
                return {
                    "ok": False,
                    "outbox_id": outbox_id,
                    "error_code": error_code,
                    "exception": exc,
                    "recovery_action": "rebuild_failed_facts",
                    "rebuild_prepared": True,
                }
            if next_state == "split_pending":
                transition_c2_outbox(
                    outbox_id,
                    status="split_pending",
                    error=error_code,
                )
                try:
                    partitions = split_ingest_payload(payload)
                    if len(partitions) < 2:
                        raise ValueError("C2_INGEST_PAYLOAD_NOT_SPLITTABLE")
                    child_ids = replace_c2_outbox_with_partitions(
                        outbox_id,
                        partitions,
                    )
                except Exception as split_exc:
                    transition_c2_outbox(
                        outbox_id,
                        status="capability_paused",
                        error=str(
                            split_exc
                            if isinstance(split_exc, ValueError)
                            else type(split_exc).__name__
                        ),
                    )
                    return {
                        "ok": False,
                        "outbox_id": outbox_id,
                        "error_code": error_code,
                        "exception": exc,
                        "capability_paused": True,
                        "recovery_action": "capability_paused",
                    }
                append_log(
                    "WARN",
                    "c2_outbox_split_prepared",
                    "消息批次超过接口上限；已保持原 read_run 和画面顺序拆批，全部分片完成前不会触发 Brain。",
                    error_code=error_code,
                    metadata={
                        "outbox_id": outbox_id,
                        "conversation_id": payload.get("conversation_id"),
                        "partition_count": len(child_ids),
                    },
                )
                return {
                    "ok": False,
                    "outbox_id": outbox_id,
                    "error_code": error_code,
                    "exception": exc,
                    "recovery_action": "split_and_retry",
                    "split_prepared": True,
                    "partition_outbox_ids": child_ids,
                }
            if next_state in {
                "target_terminated",
                "conversation_terminated",
            }:
                mark_c2_outbox_capability_paused(
                    outbox_id,
                    f"{error_code}:FACT_SETTLEMENT_REQUIRED",
                )
                append_log(
                    "WARN",
                    "c2_outbox_fact_settlement_required",
                    "当前读取授权已终止，但已形成事实仍须走无界面结算；本地记录继续保留。",
                    error_code=error_code,
                    metadata={
                        "outbox_id": outbox_id,
                        "conversation_id": payload.get("conversation_id"),
                        "requested_recovery_action": recovery_action,
                    },
                )
                return {
                    "ok": False,
                    "outbox_id": outbox_id,
                    "error_code": error_code,
                    "exception": exc,
                    "capability_paused": True,
                    "recovery_action": "capability_paused",
                }
            if next_state == "capability_paused":
                mark_c2_outbox_capability_paused(
                    outbox_id,
                    error_code,
                )
                append_log(
                    "ERROR",
                    "c2_outbox_capability_paused",
                    "C2 请求合同或能力暂不兼容；原始事实已冻结并持续阻断新微信动作，等待能力恢复后自动重传。",
                    error_code=error_code,
                    metadata={
                        "operation": operation,
                        "outbox_id": outbox_id,
                        "conversation_id": payload.get("conversation_id"),
                        "read_run_id": payload.get("read_run_id"),
                        "status_code": (
                            exc.status_code if isinstance(exc, ApiError) else None
                        ),
                        "trace_id": (
                            exc.trace_id if isinstance(exc, ApiError) else None
                        ),
                        "backend_error_response": (
                            redact_diagnostic(
                                {
                                    "code": exc.code,
                                    "message": str(exc),
                                    "status_code": exc.status_code,
                                    "trace_id": exc.trace_id,
                                    "data": exc.data,
                                }
                            )
                            if isinstance(exc, ApiError)
                            else None
                        ),
                        "recovery_action": "capability_paused",
                        "requested_recovery_action": recovery_action,
                    },
                )
                return {
                    "ok": False,
                    "outbox_id": outbox_id,
                    "error_code": error_code,
                    "exception": exc,
                    "capability_paused": True,
                    "recovery_action": "capability_paused",
                }
            if next_state == "refresh_pending":
                transition_c2_outbox(
                    outbox_id,
                    status="refresh_pending",
                    error=error_code,
                    increment_refresh=True,
                )
                refreshed = self._refresh_c2_outbox_authorization(
                    binding=binding,
                    payload=payload,
                    outbox_id=outbox_id,
                    identity_collision=(
                        exc.data
                        if recovery_action == "refresh_identity_and_retry"
                        and isinstance(exc, ApiError)
                        and isinstance(exc.data, dict)
                        else None
                    ),
                )
                return {
                    "ok": False,
                    "outbox_id": outbox_id,
                    "error_code": error_code,
                    "exception": exc,
                    "recovery_action": recovery_action,
                    "refresh_prepared": refreshed,
                }
            transition_c2_outbox(
                outbox_id,
                status="retry_waiting",
                error=error_code,
            )
            append_log(
                "WARN",
                "c2_outbox_submit_failed",
                "C2 结构化结果尚未得到后端确认，已保留 Outbox 等待原样重传。",
                error_code=error_code,
                metadata={
                    "operation": operation,
                    "outbox_id": outbox_id,
                    "conversation_id": payload.get("conversation_id"),
                    "read_run_id": payload.get("read_run_id"),
                    "error_type": type(exc).__name__,
                    "recovery_action": recovery_action,
                },
            )
            return {
                "ok": False,
                "outbox_id": outbox_id,
                "error_code": error_code,
                "exception": exc,
                "recovery_action": recovery_action,
            }
        normalized_result = result if isinstance(result, dict) else {}
        self._mark_ingest_ledger_confirmed(payload, normalized_result)
        self._consume_confirmed_ai_reply_receipts(
            payload=payload,
            result=normalized_result,
        )
        confirmed_state = transition_outbox_state(
            current_state=str(outbox_entry.get("status") or "waiting"),
            event="confirmed",
            attempt_count=int(outbox_entry.get("attempt_count") or 0) + 1,
            refresh_attempt_count=int(
                outbox_entry.get("refresh_attempt_count") or 0
            ),
        )
        transition_c2_outbox(
            outbox_id,
            status=confirmed_state,
        )
        recovery_evidence = (
            payload.get("evidence", {}).get("identity_collision_recovery")
            if isinstance(payload.get("evidence"), dict)
            and isinstance(
                payload.get("evidence", {}).get(
                    "identity_collision_recovery"
                ),
                dict,
            )
            else None
        )
        if recovery_evidence:
            append_log(
                "INFO",
                "c2_identity_collision_retransmit_confirmed",
                "身份冲突消息已使用原 Outbox 和新编号重传成功。",
                metadata={
                    "outbox_id": outbox_id,
                    "conversation_id": payload.get("conversation_id"),
                    "old_stable_id": recovery_evidence.get(
                        "old_stable_id"
                    ),
                    "new_stable_id": recovery_evidence.get(
                        "new_stable_id"
                    ),
                    "retransmit_result": "confirmed",
                },
            )
        return {
            "ok": True,
            "outbox_id": outbox_id,
            "result": normalized_result,
        }

    def _refresh_c2_outbox_authorization(
        self,
        *,
        binding: Binding,
        payload: dict[str, Any],
        outbox_id: str,
        identity_collision: dict[str, Any] | None = None,
    ) -> bool:
        evidence = (
            payload.get("evidence")
            if isinstance(payload.get("evidence"), dict)
            else {}
        )
        try:
            authorization = self.api.get_wechat_read_authorization(
                binding,
                str(payload.get("conversation_id") or ""),
                continuation_batch_id=str(
                    evidence.get("continuation_batch_id") or ""
                )
                or None,
                continuation_token=str(evidence.get("continuation_token") or "")
                or None,
            )
        except Exception as exc:
            set_c2_outbox_error(
                outbox_id,
                str(
                    exc.code
                    if isinstance(exc, ApiError)
                    else type(exc).__name__
                ),
            )
            return False
        if not authorization.get("allowed"):
            set_c2_outbox_error(
                outbox_id,
                "C2_OUTBOX_REFRESH_AUTHORIZATION_NOT_ALLOWED",
            )
            return False
        refreshed = json.loads(
            json.dumps(payload, ensure_ascii=False, default=str)
        )
        identity_replacement = None
        if isinstance(identity_collision, dict):
            checkpoint = authorization.get("identity_checkpoint")
            checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
            try:
                sequence_floor = max(
                    int(
                        identity_collision.get("next_sequence_floor")
                        or 1
                    ),
                    int(checkpoint.get("next_sequence_floor") or 1),
                )
                refreshed, replacement = rebuild_identity_collision(
                    refreshed,
                    source_message_key=str(
                        identity_collision.get("source_message_key") or ""
                    ),
                    dedupe_key=str(
                        identity_collision.get("dedupe_key") or ""
                    ),
                    next_sequence_floor=sequence_floor,
                )
                identity_replacement = replacement
                refreshed_evidence = dict(refreshed.get("evidence") or {})
                refreshed_evidence["identity_collision_recovery"] = {
                    "old_stable_id": replacement.get("old_stable_id"),
                    "new_stable_id": replacement.get("new_stable_id"),
                    "old_source_message_key": replacement.get(
                        "old_source_message_key"
                    ),
                    "new_source_message_key": replacement.get(
                        "new_source_message_key"
                    ),
                    "state": "waiting_retransmit",
                }
                refreshed["evidence"] = refreshed_evidence
            except (TypeError, ValueError) as exc:
                set_c2_outbox_error(outbox_id, str(exc))
                return False
        refreshed["authorization_revision"] = str(
            authorization.get("authorization_revision") or ""
        )
        refreshed_evidence = dict(refreshed.get("evidence") or {})
        refreshed_evidence["authorization_read_reason"] = str(
            authorization.get("read_reason") or ""
        )
        refreshed["evidence"] = refreshed_evidence
        refreshed_state = transition_outbox_state(
            current_state="refresh_pending",
            event="refresh_succeeded",
            attempt_count=0,
            refresh_attempt_count=0,
        )
        try:
            refresh_c2_outbox_payload(
                outbox_id,
                refreshed,
                next_status=refreshed_state,
                identity_replacement=identity_replacement,
                identity_state_key=(
                    f"message_identity:{refreshed.get('conversation_id')}"
                    if identity_replacement
                    else None
                ),
            )
        except ValueError as exc:
            set_c2_outbox_error(outbox_id, str(exc))
            return False
        append_log(
            "INFO",
            "c2_outbox_authorization_refreshed",
            (
                "C2 Outbox 已按服务端检查点原子重建冲突身份；消息内容和动作证据保持不变。"
                if identity_replacement
                else "C2 Outbox 只刷新了授权外壳，消息身份、内容和动作证据保持不变。"
            ),
            metadata={
                "outbox_id": outbox_id,
                "conversation_id": refreshed.get("conversation_id"),
                "authorization_revision": refreshed.get(
                    "authorization_revision"
                ),
                "old_stable_id": (
                    identity_replacement.get("old_stable_id")
                    if identity_replacement
                    else None
                ),
                "new_stable_id": (
                    identity_replacement.get("new_stable_id")
                    if identity_replacement
                    else None
                ),
                "retransmit_result": (
                    "waiting"
                    if identity_replacement
                    else None
                ),
            },
        )
        return True

    def _submit_c2_outbox_payload(
        self,
        *,
        binding: Binding,
        payload: dict[str, Any],
        operation: str,
    ) -> dict[str, Any]:
        with self.c2_outbox_lock:
            durable_payload = copy.deepcopy(payload)
            message_source_keys = {
                str(item.get("source_message_key") or "").strip()
                for item in (durable_payload.get("messages") or [])
                if isinstance(item, dict)
                and str(item.get("source_message_key") or "").strip()
            }
            durable_evidence = (
                durable_payload.get("evidence")
                if isinstance(durable_payload.get("evidence"), dict)
                else {}
            )
            for slot in durable_evidence.get("slot_ledger_states") or []:
                if (
                    isinstance(slot, dict)
                    and str(slot.get("source_message_key") or "").strip()
                    in message_source_keys
                    and str(slot.get("delivery_state") or "")
                    != "backend_confirmed"
                ):
                    slot["delivery_state"] = "outbox_waiting"
                    if slot.get("fact_scope") == "current_read_run":
                        slot["ledger_state"] = "OUTBOX_WAITING"
            durable_payload["evidence"] = durable_evidence
            outbox_id = enqueue_c2_outbox(durable_payload)
            outbox_items = self._prepare_persisted_c2_outbox(
                outbox_id=outbox_id,
                payload=durable_payload,
            )
            if outbox_items is None:
                return {
                    "ok": False,
                    "outbox_id": outbox_id,
                    "resolved": False,
                    "recovery_action": "capability_paused",
                    "error_code": "C2_OUTBOX_TRANSPORT_PREPARATION_FAILED",
                }
            final_delivery: dict[str, Any] = {
                "ok": True,
                "outbox_id": outbox_id,
                "result": {},
            }
            for outbox_id, partition in outbox_items:
                final_delivery = self._attempt_c2_outbox_delivery(
                    binding=binding,
                    payload=partition,
                    outbox_id=outbox_id,
                    operation=operation,
                )
                if not final_delivery.get("ok"):
                    return final_delivery
            if len(outbox_items) > 1:
                append_log(
                    "INFO",
                    "c2_outbox_partition_batch_confirmed",
                    "超大消息批次的全部分片已按原画面顺序确认入库。",
                    metadata={
                        "conversation_id": payload.get("conversation_id"),
                        "read_run_id": durable_payload.get("read_run_id"),
                        "partition_count": len(outbox_items),
                        "original_bytes": encoded_payload_size(
                            durable_payload
                        ),
                    },
                )
            return final_delivery

    def _prepare_persisted_c2_outbox(
        self,
        *,
        outbox_id: str,
        payload: dict[str, Any],
    ) -> list[tuple[str, dict[str, Any]]] | None:
        """Prepare transport data only after the complete source payload is durable."""

        existing = load_c2_outbox_entry(outbox_id) or {}
        if str(existing.get("status") or "") in {
            "confirmed",
            "split_completed",
            "target_terminated",
            "conversation_terminated",
        }:
            return []
        try:
            partitions = split_ingest_payload(payload)
            if len(partitions) == 1:
                prepare_c2_outbox_payload(outbox_id, partitions[0])
                return [(outbox_id, partitions[0])]

            transition_c2_outbox(
                outbox_id,
                status="split_pending",
                error="C2_OUTBOX_SPLIT_PREPARING",
            )
            child_ids = replace_c2_outbox_with_partitions(
                outbox_id,
                partitions,
            )
            active_items: list[tuple[str, dict[str, Any]]] = []
            for child_id, partition in zip(
                child_ids,
                partitions,
                strict=True,
            ):
                child = load_c2_outbox_entry(child_id) or {}
                if str(child.get("status") or "") == "confirmed":
                    continue
                active_items.append((child_id, partition))
            return active_items
        except Exception as exc:
            error_code = str(exc) or "C2_OUTBOX_TRANSPORT_PREPARATION_FAILED"
            try:
                mark_c2_outbox_capability_paused(
                    outbox_id,
                    error_code,
                )
            except ValueError:
                pass
            append_log(
                "ERROR",
                "c2_outbox_transport_preparation_failed",
                "C2 原始完整消息已保存在 Outbox；运输压缩或拆分失败，本轮暂停新微信动作且不丢弃消息。",
                error_code=error_code,
                metadata={
                    "outbox_id": outbox_id,
                    "conversation_id": payload.get("conversation_id"),
                    "read_run_id": payload.get("read_run_id"),
                    "original_bytes": encoded_payload_size(payload),
                },
            )
            return None

    def _replay_c2_outbox(self, binding: Binding) -> bool:
        with self.c2_outbox_lock:
            return self._replay_c2_outbox_locked(binding)

    def _replay_c2_outbox_locked(self, binding: Binding) -> bool:
        waiting = list_c2_outbox_waiting(limit=20)
        for item in waiting:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            outbox_id = str(item.get("outbox_id") or "")
            if not payload or not outbox_id:
                continue
            if item.get("status") == "refresh_pending":
                transition_c2_outbox(
                    outbox_id,
                    status="refresh_pending",
                    error=str(item.get("last_error") or ""),
                    increment_refresh=True,
                )
                if not self._refresh_c2_outbox_authorization(
                    binding=binding,
                    payload=payload,
                    outbox_id=outbox_id,
                ):
                    return False
                refreshed_entry = load_c2_outbox_entry(outbox_id) or {}
                payload = (
                    refreshed_entry.get("payload")
                    if isinstance(refreshed_entry.get("payload"), dict)
                    else payload
                )
            outbox_items = self._prepare_persisted_c2_outbox(
                outbox_id=outbox_id,
                payload=payload,
            )
            if outbox_items is None:
                return False
            delivery: dict[str, Any] = {"ok": True, "result": {}}
            for prepared_id, prepared_payload in outbox_items:
                delivery = self._attempt_c2_outbox_delivery(
                    binding=binding,
                    payload=prepared_payload,
                    outbox_id=prepared_id,
                    operation="replay",
                )
                if not delivery.get("ok"):
                    break
            if not delivery.get("ok"):
                if delivery.get("resolved"):
                    continue
                recovery_action = classify_outbox_recovery(
                    delivery.get("recovery_action")
                )
                if recovery_action in {
                    "rebuild_failed_facts",
                    "split_and_retry",
                }:
                    return False
                if recovery_action == "capability_paused":
                    for image_message in _c2_image_messages(payload):
                        append_log(
                            "WARN",
                            "c2_image_ingest_replay_capability_paused",
                            "C2 图片 Outbox 等待能力恢复，原结果不会丢弃或重新调用 Vision。",
                            error_code=str(delivery.get("error_code") or ""),
                            metadata={
                                "outbox_id": outbox_id,
                                "conversation_id": item.get("conversation_id"),
                                "read_run_id": item.get("read_run_id"),
                                "source_message_key": image_message.get("source_message_key"),
                                "dedupe_key": image_message.get("dedupe_key"),
                                "image_persisted": False,
                            },
                        )
                    append_log(
                        "WARN",
                        "c2_outbox_capability_paused",
                        "C2 Outbox 处于能力暂停并继续阻断新微信动作，将按退避周期自动探测恢复。",
                        error_code=str(delivery.get("error_code") or ""),
                        metadata={
                            "outbox_id": outbox_id,
                            "conversation_id": item.get("conversation_id"),
                            "read_run_id": item.get("read_run_id"),
                        },
                    )
                    return False
                if recovery_action in {
                    "refresh_and_rebuild",
                    "refresh_identity_and_retry",
                }:
                    append_log(
                        "INFO",
                        "c2_outbox_refresh_waiting",
                        "C2 Outbox 已准备新的授权外壳，下轮原样重传，不重复操作微信。",
                        error_code=str(delivery.get("error_code") or ""),
                        metadata={
                            "outbox_id": outbox_id,
                            "conversation_id": item.get("conversation_id"),
                            "refresh_prepared": bool(
                                delivery.get("refresh_prepared")
                            ),
                        },
                    )
                    return False
                for image_message in _c2_image_messages(payload):
                    append_log(
                        "WARN",
                        "c2_image_ingest_replay_waiting",
                        "C2 图片 Outbox 重传尚未得到后端确认。",
                        error_code=str(delivery.get("error_code") or ""),
                        metadata={
                            "outbox_id": outbox_id,
                            "conversation_id": item.get("conversation_id"),
                            "read_run_id": item.get("read_run_id"),
                            "source_message_key": image_message.get("source_message_key"),
                            "dedupe_key": image_message.get("dedupe_key"),
                            "image_persisted": False,
                        },
                    )
                append_log(
                    "WARN",
                    "c2_outbox_replay_waiting",
                    "C2 Outbox 尚未得到后端确认，本轮不执行新的微信动作。",
                    error_code=str(delivery.get("error_code") or "C2_OUTBOX_REPLAY_FAILED"),
                    metadata={
                        "outbox_id": outbox_id,
                        "conversation_id": item.get("conversation_id"),
                        "read_run_id": item.get("read_run_id"),
                    },
                )
                return False
            for image_message in _c2_image_messages(payload):
                append_log(
                    "INFO",
                    "c2_image_ingest_replayed",
                    "C2 图片 Outbox 已重传原结构化 JSON，没有重新操作微信或调用 Vision。",
                    metadata={
                        "outbox_id": outbox_id,
                        "conversation_id": item.get("conversation_id"),
                        "read_run_id": item.get("read_run_id"),
                        "source_message_key": image_message.get("source_message_key"),
                        "dedupe_key": image_message.get("dedupe_key"),
                        "image_persisted": False,
                    },
                )
            append_log(
                "INFO",
                "c2_outbox_replayed",
                "C2 Outbox 已重传原结构化结果，没有重新执行微信操作。",
                metadata={"outbox_id": outbox_id, "conversation_id": item.get("conversation_id")},
            )
        return not has_pending_c2_outbox()

    def _filter_confirmed_messages(self, payload: dict[str, Any]) -> dict[str, Any]:
        filtered = dict(payload)
        messages: list[dict[str, Any]] = []
        removed_observation_ids: set[str] = set()
        evidence = (
            dict(payload.get("evidence"))
            if isinstance(payload.get("evidence"), dict)
            else {}
        )
        slot_states = {
            str(item.get("source_message_key") or "").strip(): item
            for item in (evidence.get("slot_ledger_states") or [])
            if isinstance(item, dict)
            and str(item.get("source_message_key") or "").strip()
        }
        for item in payload.get("messages") or []:
            if not isinstance(item, dict):
                continue
            source_key = str(item.get("source_message_key") or "").strip()
            ledger = load_c2_ledger_entry(str(payload.get("conversation_id") or ""), source_key) if source_key else None
            slot = slot_states.get(source_key)
            already_settled = bool(
                isinstance(slot, dict)
                and (
                    str(slot.get("fact_scope") or "") == "unknown"
                    or str(slot.get("delivery_state") or "")
                    in {"outbox_waiting", "backend_confirmed"}
                )
            )
            if already_settled or (
                ledger and ledger.get("ingest_state") == "not_required"
            ):
                raw_payload = (
                    item.get("raw_payload")
                    if isinstance(item.get("raw_payload"), dict)
                    else {}
                )
                observation = (
                    raw_payload.get("observation")
                    if isinstance(raw_payload.get("observation"), dict)
                    else {}
                )
                observation_id = str(
                    observation.get("observation_id") or ""
                ).strip()
                if observation_id:
                    removed_observation_ids.add(observation_id)
                continue
            messages.append(item)
        filtered["messages"] = messages
        observations = evidence.get("observations")
        if removed_observation_ids and isinstance(observations, list):
            evidence["observations"] = [
                observation
                for observation in observations
                if not (
                    isinstance(observation, dict)
                    and str(observation.get("observation_id") or "").strip()
                    in removed_observation_ids
                )
            ]
        filtered["evidence"] = evidence
        return filtered

    def _stage_payload_ledger(self, payload: dict[str, Any]) -> None:
        conversation_id = str(payload.get("conversation_id") or "")
        read_run_id = str(payload.get("read_run_id") or "").strip()
        if not read_run_id:
            raise ValueError("C2_READ_RUN_ID_MISSING")
        evidence = (
            payload.get("evidence")
            if isinstance(payload.get("evidence"), dict)
            else {}
        )
        slots_by_source = {
            str(slot.get("source_message_key") or "").strip(): slot
            for slot in (evidence.get("slot_ledger_states") or [])
            if isinstance(slot, dict)
            and str(slot.get("source_message_key") or "").strip()
        }
        for item in payload.get("messages") or []:
            if not isinstance(item, dict):
                continue
            source_key = str(item.get("source_message_key") or "").strip()
            if not source_key:
                continue
            slot = slots_by_source.get(source_key)
            if not isinstance(slot, dict):
                raise ValueError("C2_SLOT_LEDGER_MESSAGE_MAPPING_MISSING")
            if str(slot.get("fact_scope") or "") == "unknown":
                continue
            slot_origin_read_run_id = str(
                slot.get("origin_read_run_id") or ""
            ).strip()
            if not slot_origin_read_run_id:
                raise ValueError("C2_SLOT_LEDGER_ORIGIN_READ_RUN_ID_MISSING")
            existing = load_c2_ledger_entry(conversation_id, source_key)
            item_origin_read_run_id = str(
                slot_origin_read_run_id
            ).strip()
            item_state = str(item.get("item_state") or "completed").strip().lower()
            if existing and (
                item.get("message_type") in {"voice", "image"}
                or item_state == "failed"
            ):
                save_c2_ledger_terminal(
                    conversation_id=conversation_id,
                    source_message_key=source_key,
                    origin_read_run_id=item_origin_read_run_id,
                    dedupe_key=(
                        str(item.get("dedupe_key") or "")
                        or str(existing.get("dedupe_key") or "")
                        or None
                    ),
                    message_type=str(
                        item.get("message_type")
                        or existing.get("message_type")
                        or "unknown"
                    ),
                    terminal_state=str(
                        existing.get("terminal_state")
                        or (
                            "failed"
                            if item_state == "failed"
                            else "completed"
                        )
                    ),
                    ingest_state=str(
                        existing.get("ingest_state") or "waiting"
                    ),
                    result=(
                        dict(existing.get("result") or {})
                        if isinstance(existing.get("result"), dict)
                        else {}
                    ),
                )
                continue
            save_c2_ledger_terminal(
                conversation_id=conversation_id,
                source_message_key=source_key,
                origin_read_run_id=item_origin_read_run_id,
                dedupe_key=str(item.get("dedupe_key") or "") or None,
                message_type=str(item.get("message_type") or "unknown"),
                terminal_state=(
                    "failed" if item_state == "failed" else "completed"
                ),
                ingest_state="waiting",
                result=(
                    {
                        "state": "failed",
                        "error_code": str(
                            (
                                item.get("raw_payload")
                                if isinstance(item.get("raw_payload"), dict)
                                else {}
                            ).get("error_code")
                            or (
                                item.get("raw_payload")
                                if isinstance(item.get("raw_payload"), dict)
                                else {}
                            ).get("error_code")
                            or "MESSAGE_PROCESSING_FAILED"
                        ),
                    }
                    if item_state == "failed"
                    else {}
                ),
            )

    def _report_voice_failure_gate(
        self,
        *,
        binding: Binding,
        target: WechatReadTarget,
        read_run_id: str,
        error_code: str,
        source_keys: list[str],
        voice_payload: dict[str, Any],
    ) -> bool:
        clean_keys = sorted({str(value).strip() for value in source_keys if str(value).strip()})
        self._mark_voice_sources_failed(
            target=target,
            origin_read_run_id=read_run_id,
            source_keys=clean_keys,
            error_code=error_code,
        )
        payload = build_flow_gate_ingest_payload(
            target,
            read_run_id=read_run_id,
            error_code="C2_VOICE_TRANSCRIBE_FAILED",
            evidence={
                "failed_voice_source_keys": clean_keys,
                "voice_failure": {
                    "error_code": str(error_code or "VOICE_TRANSCRIBE_FAILED"),
                    "state": str(voice_payload.get("state") or ""),
                    "failed_voice_anchor_keys": list(
                        voice_payload.get("failed_voice_anchor_keys") or []
                    ),
                    "sidecar_run_id": str(voice_payload.get("sidecar_run_id") or ""),
                },
            },
        )
        delivery = self._submit_c2_outbox_payload(
            binding=binding,
            payload=payload,
            operation="voice_failure_gate",
        )
        outbox_id = str(delivery["outbox_id"])
        if not delivery.get("ok"):
            append_log(
                "WARN",
                "c2_voice_failure_gate_waiting",
                "失败语音安全门禁尚未得到后端确认，已保留 Outbox，禁止继续 Brain。",
                error_code=str(delivery.get("error_code") or ""),
                metadata={
                    "conversation_id": target.conversation_id,
                    "outbox_id": outbox_id,
                    "source_message_keys": clean_keys,
                },
            )
            return False
        append_log(
            "WARN",
            "c2_voice_failure_gate_reported",
            "失败语音事实已上报后端并创建 Brain 阻断/人工接管门禁。",
            error_code="C2_VOICE_TRANSCRIBE_FAILED",
            metadata={
                "conversation_id": target.conversation_id,
                "outbox_id": outbox_id,
                "source_message_keys": clean_keys,
            },
        )
        return True

    @staticmethod
    def _mark_voice_sources_failed(
        *,
        target: WechatReadTarget,
        origin_read_run_id: str,
        source_keys: list[str],
        error_code: str,
    ) -> None:
        for source_key in sorted(
            {str(value).strip() for value in source_keys if str(value).strip()}
        ):
            existing = load_c2_ledger_entry(
                target.conversation_id,
                source_key,
            )
            effective_origin_read_run_id = str(
                (existing or {}).get("origin_read_run_id")
                or origin_read_run_id
            ).strip()
            existing_result = (
                dict(existing.get("result") or {})
                if isinstance((existing or {}).get("result"), dict)
                else {}
            )
            save_c2_ledger_terminal(
                conversation_id=target.conversation_id,
                source_message_key=source_key,
                origin_read_run_id=effective_origin_read_run_id,
                dedupe_key=None,
                message_type="voice",
                terminal_state="failed",
                ingest_state="waiting",
                result={
                    **existing_result,
                    "state": "failed",
                    "error_code": str(
                        existing_result.get("error_code")
                        or error_code
                        or "VOICE_TRANSCRIBE_FAILED"
                    ),
                },
            )

    @staticmethod
    def _annotate_failed_voice_observations(
        *,
        target: WechatReadTarget,
        sidecar_payload: dict[str, Any],
        failed_source_keys: set[str],
        error_code: str,
    ) -> dict[str, str]:
        failed_roles: dict[str, str] = {}
        observations = sidecar_payload.get("observations")
        if not isinstance(observations, list):
            return failed_roles
        for observation in observations:
            if (
                not isinstance(observation, dict)
                or str(observation.get("row_kind") or "").strip().lower()
                != "voice_bubble"
                or str(observation.get("voice_state") or "").strip().lower()
                != "untranscribed"
            ):
                continue
            try:
                source_key = voice_observation_source_key(target, observation)
            except ValueError:
                continue
            if source_key not in failed_source_keys:
                continue
            if not observation_role_is_trusted(observation):
                continue
            role = str(observation.get("sender_role") or "").strip().lower()
            observation["item_state"] = "failed"
            observation["error_code"] = str(
                error_code or "VOICE_TRANSCRIBE_FAILED"
            )
            observation["reason_detail"] = observation["error_code"]
            source_message = (
                dict(observation.get("source_message"))
                if isinstance(observation.get("source_message"), dict)
                else {}
            )
            source_message["item_state"] = "failed"
            source_message["error_code"] = observation["error_code"]
            source_message["reason_detail"] = observation["reason_detail"]
            source_message["voice_anchor_stable_key"] = str(
                observation.get("voice_anchor_key") or ""
            )
            observation["source_message"] = source_message
            failed_roles[source_key] = role
        return failed_roles

    def _report_identity_failure_gate(
        self,
        *,
        binding: Binding,
        target: WechatReadTarget,
        read_run_id: str,
        error_code: str,
        identity_errors: list[dict[str, Any]],
        authoritative_frame_source: str = "initial_read",
        ui_frame_invalidated: bool = False,
    ) -> bool:
        normalized_errors = [
            {
                "observation_id": str(item.get("observation_id") or ""),
                "row_kind": str(item.get("row_kind") or ""),
                "error_code": str(item.get("error_code") or error_code),
                "signature": str(item.get("signature") or ""),
                "reason": str(item.get("reason") or ""),
                "screen_order": int(item.get("screen_order") or 0),
                "order_source": str(item.get("order_source") or ""),
            }
            for item in identity_errors
            if isinstance(item, dict)
        ]
        stable_gate_key = _c2_text_fingerprint(
            json.dumps(
                {
                    "conversation_id": target.conversation_id,
                    "error_code": error_code,
                    "identity_errors": normalized_errors,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        gate_orders = sorted(
            {
                int(item.get("screen_order") or 0)
                for item in normalized_errors
                if int(item.get("screen_order") or 0) > 0
            }
        )
        has_visual_order_proof = bool(gate_orders) and all(
            item.get("order_source") == "visual_top"
            for item in normalized_errors
            if int(item.get("screen_order") or 0) > 0
        )
        gate_detail: dict[str, Any] = {
            "error_code": error_code,
            "position_source": (
                "identity_error_visual_top"
                if has_visual_order_proof
                else "position_unavailable"
            ),
            "gate_scope": "reply_suffix",
            "boundary_relation": "unknown",
            "min_screen_order": gate_orders[0] if gate_orders else 0,
            "max_screen_order": gate_orders[-1] if gate_orders else 0,
        }
        payload = build_flow_gate_ingest_payload(
            target,
            read_run_id=read_run_id,
            error_code=error_code,
            evidence={
                "flow_gate_identity_key": stable_gate_key,
                "identity_errors": normalized_errors,
                "flow_gate_details": [gate_detail],
                "recovery_attempt_kind": (
                    "stable_reread"
                    if isinstance(target.raw, dict)
                    and isinstance(target.raw.get("recovery_hold"), dict)
                    and target.raw.get("recovery_hold", {}).get("status")
                    == "active"
                    else "checkpoint_merge"
                ),
                "authoritative_frame_source": (
                    authoritative_frame_source
                ),
                "ui_frame_invalidated": ui_frame_invalidated,
            },
        )
        delivery = self._submit_c2_outbox_payload(
            binding=binding,
            payload=payload,
            operation="identity_failure_gate",
        )
        outbox_id = str(delivery["outbox_id"])
        if not delivery.get("ok"):
            append_log(
                "WARN",
                "c2_identity_failure_gate_waiting",
                "消息身份歧义门禁尚未得到后端确认，已保留 Outbox，禁止继续入库和 Brain。",
                error_code=str(delivery.get("error_code") or ""),
                metadata={
                    "conversation_id": target.conversation_id,
                    "outbox_id": outbox_id,
                    "flow_gate_identity_key": stable_gate_key,
                },
            )
            return False
        append_log(
            "WARN",
            "c2_identity_failure_gate_reported",
            "消息身份无法唯一确认，已创建一次可去重的人工接管门禁。",
            error_code=error_code,
            metadata={
                "conversation_id": target.conversation_id,
                "outbox_id": outbox_id,
                "flow_gate_identity_key": stable_gate_key,
            },
        )
        return True

    def _build_final_slot_incremental_plan(
        self,
        *,
        target: WechatReadTarget,
        sidecar_payload: dict[str, Any],
        read_run_id: str,
    ) -> dict[str, Any]:
        clean_read_run_id = str(read_run_id or "").strip()
        if not clean_read_run_id:
            raise ValueError("C2_READ_RUN_ID_MISSING")
        preliminary_payload = build_message_ingest_payload(
            target,
            sidecar_payload,
            read_run_id=clean_read_run_id,
        )
        canonical_by_observation_id: dict[str, dict[str, Any]] = {}
        for message in preliminary_payload.get("messages") or []:
            if not isinstance(message, dict):
                continue
            raw_payload = message.get("raw_payload") if isinstance(message.get("raw_payload"), dict) else {}
            observation = raw_payload.get("observation") if isinstance(raw_payload.get("observation"), dict) else {}
            observation_id = str(observation.get("observation_id") or "").strip()
            if observation_id:
                canonical_by_observation_id[observation_id] = message

        observations = sidecar_payload.get("observations") if isinstance(sidecar_payload.get("observations"), list) else []

        slots = [
            {
                "authority_index": index,
                "rect": message_rect({"bubble_rect": item.get("bubble_rect")}),
                "observation": item,
            }
            for index, item in enumerate(observations)
            if isinstance(item, dict)
        ]
        frame_order_source = authoritative_order_source(slots)
        ordered = [
            (int(slot["authority_index"]), slot["observation"])
            for slot in order_authoritative_slots(slots)
        ]

        states: list[dict[str, Any]] = []
        identity_errors: list[dict[str, Any]] = []
        seen_source_keys: set[str] = set()
        backend_confirmed_origins = {
            str(item.get("source_message_key") or "").strip(): str(
                item.get("origin_read_run_id") or ""
            ).strip()
            for item in (
                (
                    target.raw.get("identity_checkpoint") or {}
                ).get("recent_messages")
                if isinstance(target.raw, dict)
                and isinstance(
                    target.raw.get("identity_checkpoint"), dict
                )
                else []
            )
            if isinstance(item, dict)
            and str(item.get("source_message_key") or "").strip()
        }
        outbox_origins = load_c2_outbox_origin_read_run_ids(
            target.conversation_id
        )
        for screen_order, (index, observation) in enumerate(ordered, start=1):
            row_kind = str(observation.get("row_kind") or "").strip().lower()
            voice_state = str(observation.get("voice_state") or "").strip().lower()
            if row_kind == "voice_bubble" and voice_state != "untranscribed":
                continue
            if row_kind not in {"text_bubble", "voice_bubble", "voice_transcript", "image_bubble", "system_message"}:
                continue
            observation_id = str(observation.get("observation_id") or "").strip()
            trusted_role = observation_role_is_trusted(observation)
            if not trusted_role:
                identity_errors.append(
                    {
                        "observation_id": (
                            observation_id or f"observation-{index}"
                        ),
                        "screen_order": screen_order,
                        "order_source": frame_order_source,
                        "row_kind": row_kind,
                        "error_code": "MESSAGE_IDENTITY_UNCONFIRMED",
                    }
                )
                continue
            canonical = canonical_by_observation_id.get(observation_id)
            try:
                if canonical:
                    source_key = str(
                        canonical.get("source_message_key") or ""
                    ).strip()
                elif row_kind == "image_bubble":
                    source_key = image_observation_source_key(
                        target,
                        observation,
                    )
                elif row_kind in {"voice_bubble", "voice_transcript"}:
                    source_key = voice_observation_source_key(
                        target,
                        observation,
                    )
                else:
                    source_key = ""
            except ValueError:
                source_key = ""
            sender_role = str(
                (canonical or {}).get("sender_role_hint")
                or observation.get("sender_role")
                or ""
            ).strip().lower()
            if (
                not source_key
                or source_key in seen_source_keys
            ):
                error = {
                    "observation_id": observation_id or f"observation-{index}",
                    "screen_order": screen_order,
                    "order_source": frame_order_source,
                    "row_kind": row_kind,
                    "error_code": "MESSAGE_IDENTITY_UNCONFIRMED",
                }
                identity_errors.append(error)
                continue
            seen_source_keys.add(source_key)
            ledger = load_c2_ledger_entry(target.conversation_id, source_key)
            ledger_origin = str(
                (ledger or {}).get("origin_read_run_id") or ""
            ).strip()
            outbox_present = source_key in outbox_origins
            outbox_origin = str(outbox_origins.get(source_key) or "").strip()
            backend_present = source_key in backend_confirmed_origins
            backend_origin = str(
                backend_confirmed_origins.get(source_key) or ""
            ).strip()
            known_origins = {
                value
                for value in (ledger_origin, outbox_origin, backend_origin)
                if value
            }
            origin_conflict = len(known_origins) > 1
            has_existing_fact = bool(ledger or outbox_present or backend_present)
            if origin_conflict or (has_existing_fact and not known_origins):
                origin_read_run_id = "unknown"
                fact_scope = "unknown"
                identity_errors.append(
                    {
                        "observation_id": observation_id or f"observation-{index}",
                        "screen_order": screen_order,
                        "order_source": frame_order_source,
                        "row_kind": row_kind,
                        "error_code": "MESSAGE_IDENTITY_UNCONFIRMED",
                        "reason": "origin_read_run_id_unconfirmed",
                    }
                )
            else:
                origin_read_run_id = (
                    next(iter(known_origins))
                    if known_origins
                    else clean_read_run_id
                )
                fact_scope = (
                    "current_read_run"
                    if origin_read_run_id == clean_read_run_id
                    else "historical"
                )
            if ledger and str(ledger.get("ingest_state") or "") == "confirmed":
                delivery_state = "backend_confirmed"
            elif backend_present:
                delivery_state = "backend_confirmed"
            elif outbox_present:
                delivery_state = "outbox_waiting"
            else:
                delivery_state = "not_enqueued"
            item_state = (
                "failed"
                if str(
                    (ledger or {}).get("terminal_state")
                    or observation.get("item_state")
                    or ""
                ).strip().lower()
                == "failed"
                else "completed"
            )
            legacy_ledger_state: str | None = None
            if fact_scope == "current_read_run":
                legacy_ledger_state = (
                    "OUTBOX_WAITING"
                    if delivery_state == "outbox_waiting"
                    else "NEW_MESSAGE"
                )
            elif fact_scope == "historical":
                legacy_ledger_state = (
                    "OLD_FAILED"
                    if item_state == "failed"
                    else "OLD_COMPLETED"
                )
            state_payload = {
                "observation_id": observation_id or f"observation-{index}",
                "screen_order": screen_order,
                "order_source": frame_order_source,
                "row_kind": row_kind,
                "sender_role": sender_role,
                "source_message_key": source_key,
                "origin_read_run_id": origin_read_run_id,
                "fact_scope": fact_scope,
                "delivery_state": delivery_state,
                "item_state": item_state,
                "history_source": (
                    "local_ledger"
                    if ledger
                    else "local_outbox"
                    if outbox_present
                    else "backend_identity_checkpoint"
                    if backend_present
                    else "current_read_run"
                ),
            }
            if legacy_ledger_state:
                state_payload["ledger_state"] = legacy_ledger_state
            states.append(state_payload)

        seen_current = False
        first_current_screen_order = 0
        history_gap_screen_order = 0
        raw_history_gap = False
        for item in states:
            if item["fact_scope"] == "current_read_run":
                seen_current = True
                if not first_current_screen_order:
                    first_current_screen_order = int(item["screen_order"])
            elif item["fact_scope"] == "historical" and seen_current:
                raw_history_gap = True
                history_gap_screen_order = int(item["screen_order"])
                break
        latest_self_screen_order = max(
            (
                int(item["screen_order"])
                for item in states
                if item.get("sender_role") == "self"
            ),
            default=0,
        )
        pending_customer_orders = [
            int(item["screen_order"])
            for item in states
            if item.get("sender_role") == "customer"
            and int(item["screen_order"]) > latest_self_screen_order
        ]
        history_gap = bool(
            raw_history_gap
            and pending_customer_orders
            and history_gap_screen_order > latest_self_screen_order
        )
        historical_warnings: list[dict[str, Any]] = [
            dict(item)
            for item in (
                sidecar_payload.get("historical_warnings") or []
            )
            if isinstance(item, dict)
        ]
        if raw_history_gap and not history_gap:
            historical_warnings.append(
                {
                    "warning_code": "C2_MESSAGE_HISTORY_GAP_HISTORICAL",
                    "min_screen_order": first_current_screen_order,
                    "max_screen_order": history_gap_screen_order,
                    "latest_self_screen_order": latest_self_screen_order,
                }
            )
        new_image_source_keys = {
            item["source_message_key"]
            for item in states
            if item["row_kind"] == "image_bubble"
            and item["fact_scope"] == "current_read_run"
            and item["delivery_state"] == "not_enqueued"
            and item["history_source"] == "current_read_run"
        }
        flow_gate_details: list[dict[str, Any]] = []
        ai_reply_boundary = (
            target.raw.get("ai_reply_boundary")
            if isinstance(target.raw, dict)
            and isinstance(target.raw.get("ai_reply_boundary"), dict)
            else {}
        )
        boundary_stable_id = str(
            ai_reply_boundary.get("worker_stable_id") or ""
        ).strip()
        boundary_screen_order = 0
        if boundary_stable_id:
            state_order_by_observation_id = {
                str(item.get("observation_id") or ""): int(
                    item.get("screen_order") or 0
                )
                for item in states
                if isinstance(item, dict)
            }
            for observation in observations:
                if not isinstance(observation, dict):
                    continue
                if str(
                    observation.get("_worker_stable_id")
                    or observation.get("worker_stable_id")
                    or ""
                ).strip() == boundary_stable_id:
                    boundary_screen_order = int(
                        state_order_by_observation_id.get(
                            str(observation.get("observation_id") or ""),
                            0,
                        )
                    )
                    break

        def boundary_relation(min_order: int, max_order: int) -> str:
            if boundary_screen_order <= 0:
                return "unknown"
            if max_order > 0 and max_order <= boundary_screen_order:
                return "before_or_equal"
            if min_order > boundary_screen_order:
                return "after"
            return "unknown"

        if history_gap:
            history_gap_detail: dict[str, Any] = {
                "error_code": "C2_MESSAGE_HISTORY_GAP",
                "position_source": (
                    "slot_ledger_visual_top"
                    if frame_order_source == "visual_top"
                    else "position_unavailable"
                ),
                "gate_scope": "reply_suffix",
                "boundary_relation": "unknown",
                "min_screen_order": 0,
                "max_screen_order": 0,
            }
            if frame_order_source == "visual_top":
                history_gap_detail["min_screen_order"] = first_current_screen_order
                history_gap_detail["max_screen_order"] = history_gap_screen_order
                history_gap_detail["boundary_relation"] = boundary_relation(
                    first_current_screen_order,
                    history_gap_screen_order,
                )
            flow_gate_details.append(history_gap_detail)
        if identity_errors:
            error_orders = sorted(
                {
                    int(item.get("screen_order") or 0)
                    for item in identity_errors
                    if int(item.get("screen_order") or 0) > 0
                }
            )
            detail: dict[str, Any] = {
                "error_code": "MESSAGE_IDENTITY_UNCONFIRMED",
                "position_source": (
                    "identity_error_visual_top"
                    if error_orders and frame_order_source == "visual_top"
                    else "position_unavailable"
                ),
                "gate_scope": "reply_suffix",
                "boundary_relation": "unknown",
                "min_screen_order": 0,
                "max_screen_order": 0,
            }
            if error_orders and frame_order_source == "visual_top":
                detail["min_screen_order"] = error_orders[0]
                detail["max_screen_order"] = error_orders[-1]
                detail["boundary_relation"] = boundary_relation(
                    error_orders[0], error_orders[-1]
                )
            flow_gate_details.append(detail)
        recoverable_reason_codes = sorted(
            {
                str(value).strip()
                for value in (
                    target.raw.get(
                        "recoverable_handoff_reason_codes", []
                    )
                    if isinstance(target.raw, dict)
                    else []
                )
                if str(value).strip()
                in {
                    "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
                    "C2_MESSAGE_HISTORY_GAP",
                }
            }
        )
        recovery_resolution = None
        if (
            recoverable_reason_codes
            and not history_gap
            and not identity_errors
            and bool(observations)
        ):
            recovery_resolution = {
                "version": 1,
                "status": "latest_unreplied_turn_complete",
                "reason_codes": recoverable_reason_codes,
                "identity_confirmed": True,
                "history_confirmed": True,
                "automatic_reread_performed": bool(
                    sidecar_payload.get(
                        "identity_automatic_reread_performed"
                    )
                    or sidecar_payload.get(
                        "history_gap_automatic_reread_performed"
                    )
                ),
            }
        return {
            "preliminary_payload": preliminary_payload,
            "slot_ledger_states": states,
            "history_gap": history_gap,
            "identity_errors": identity_errors,
            "new_image_source_keys": new_image_source_keys,
            "flow_gate_details": flow_gate_details,
            "historical_warnings": historical_warnings,
            "recoverable_handoff_resolution": recovery_resolution,
        }

    def _image_slot_access_decision(
        self,
        *,
        binding: Binding,
        target: WechatReadTarget,
        observation: dict[str, Any],
        source_key: str,
        allowed_new_source_keys: set[str] | None,
        enforce_read_targets: bool,
    ) -> str:
        if not observation_role_is_trusted(observation):
            return "role_untrusted"
        if (
            allowed_new_source_keys is not None
            and source_key not in allowed_new_source_keys
        ):
            return "not_new"
        if (
            enforce_read_targets
            and not self._backend_still_allows_read_target(
                binding,
                target,
            )
        ):
            return "authorization_revoked"
        return "process"

    def _execute_one_image_slot_vision(
        self,
        *,
        target: WechatReadTarget,
        payload: dict[str, Any],
        observation: dict[str, Any],
        source_key: str,
        cancel_check: Callable[[], bool | str] | None,
        flow_outcomes: FlowOutcomeAccumulator | None,
    ) -> dict[str, Any]:
        image_action_journal: Path | None = None
        if flow_outcomes is not None:
            payload_observations = list(payload.get("observations") or [])
            committed_ids = {
                str(item.get("observation_id") or "").strip(): str(
                    item.get("_worker_stable_id") or ""
                ).strip()
                for item in payload_observations
                if isinstance(item, dict)
                and str(item.get("observation_id") or "").strip()
                and str(item.get("_worker_stable_id") or "").strip()
            }
            pre_action_sequence = build_pre_action_identity_sequence(
                payload_observations,
                committed_ids=committed_ids,
            )
            pre_frame_id = str(
                payload.get("frame_id")
                or payload.get("sidecar_run_id")
                or payload.get("run_id")
                or ""
            ).strip()
            if not pre_frame_id:
                return {
                    "state": "failed",
                    "reason": "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
                    "action_phase": "not_attempted",
                    "diagnostics": {"events": [], "image_persisted": False},
                }
            image_action_journal = self._start_irreversible_action_journal(
                action_kind="image",
                target=target,
                items=[
                    {
                        "source_message_key": source_key,
                        "physical_anchor_keys": [
                            str(
                                observation.get("observation_id") or ""
                            ).strip()
                        ],
                        "replayable_observation": (
                            replayable_image_observation(
                                observation,
                                source_message_key=source_key,
                            )
                        ),
                    }
                ],
                flow_outcomes=flow_outcomes,
                pre_action_identity_sequence=pre_action_sequence,
                pre_frame_id=pre_frame_id,
            )
        try:
            from .omniauto_vision import process_image_slot

            result = process_image_slot(
                observation=observation,
                remark_code=str(target.remark_code or ""),
                session_key=str(target.rpa_session_key or ""),
                window_context=(
                    dict(payload.get("window_context") or {})
                    if isinstance(payload.get("window_context"), dict)
                    else None
                ),
                trace_id=source_key,
                cancel_check=cancel_check,
                action_journal_path=image_action_journal,
                source_message_key=source_key,
                artifact_dir=str(payload.get("artifact_dir") or "") or None,
            )
        except Exception as exc:
            result = {
                "state": "failed",
                "reason": "vision_adapter_failed",
                "error_type": type(exc).__name__,
                "action_phase": "quarantined",
                "transaction": {
                    "action_phase": "quarantined",
                    "status": "vision_adapter_failed",
                },
                "diagnostics": {
                    "events": [],
                    "image_persisted": False,
                },
            }
        if image_action_journal is not None and str(
            result.get("state") or ""
        ).strip() in {"completed", "failed"}:
            journal_payload = read_action_journal(image_action_journal)
            journal_item = (
                (journal_payload.get("items") or {}).get(source_key)
                if isinstance(journal_payload.get("items"), dict)
                else None
            )
            journal_terminal = bool(
                isinstance(journal_item, dict)
                and (
                    str(journal_item.get("business_state") or "")
                    in {"completed", "failed"}
                    or str(journal_item.get("error_code") or "").strip()
                    or (
                        isinstance(
                            journal_item.get("terminal_payload"), dict
                        )
                        and bool(journal_item.get("terminal_payload"))
                    )
                )
            )
            if not journal_terminal:
                completed = str(result.get("state") or "") == "completed"
                transaction = (
                    result.get("transaction")
                    if isinstance(result.get("transaction"), dict)
                    else {}
                )
                error_code = None if completed else str(
                    result.get("reason") or "IMAGE_RESULT_UNCONFIRMED"
                )
                update_action_journal_item(
                    image_action_journal,
                    source_message_key=source_key,
                    action_phase=str(
                        result.get("action_phase") or "not_attempted"
                    ),
                    business_state=(
                        "completed" if completed else "failed"
                    ),
                    business_result_confirmed=completed,
                    error_code=error_code,
                    terminal_payload={
                        "state": str(result.get("state") or "failed"),
                        "error_code": error_code,
                        "reason_detail": str(
                            transaction.get("status")
                            or result.get("reason_detail")
                            or error_code
                            or ""
                        ),
                        "customer_image_understanding": (
                            dict(
                                result.get(
                                    "customer_image_understanding"
                                )
                                or {}
                            )
                            if isinstance(
                                result.get(
                                    "customer_image_understanding"
                                ),
                                dict,
                            )
                            else None
                        ),
                        "visual_bridge_input": (
                            result.get("visual_bridge_input")
                            if isinstance(
                                result.get("visual_bridge_input"),
                                (dict, list, str),
                            )
                            else None
                        ),
                    },
                )
        return result

    @staticmethod
    def _normalize_one_image_slot_result(
        result: dict[str, Any],
        *,
        source_key: str,
    ) -> dict[str, Any]:
        normalized = dict(result or {})
        transaction = (
            dict(normalized.get("transaction"))
            if isinstance(normalized.get("transaction"), dict)
            else {}
        )
        diagnostics = (
            dict(normalized.get("diagnostics"))
            if isinstance(normalized.get("diagnostics"), dict)
            else {}
        )
        action_phase = str(
            normalized.get("action_phase")
            or transaction.get("action_phase")
            or "not_attempted"
        )
        ui_frame_invalidated = bool(
            normalized.get("ui_action_performed") is True
            or transaction.get("ui_action_performed") is True
            or transaction.get("right_click_ok") is True
            or transaction.get("menu_opened") is True
            or transaction.get("menu_dismissed") is True
            or transaction.get("copy_click_ok") is True
            or transaction.get("clipboard_content_read") is True
            or action_phase
            not in {"not_attempted", "cancelled_before_trigger"}
        )
        if (
            action_phase == "not_attempted"
            and str(normalized.get("state") or "")
            in {"image_not_visible", "not_visible"}
            and not ui_frame_invalidated
        ):
            return {
                "result": normalized,
                "transaction": transaction,
                "diagnostics": diagnostics,
                "action_phase": action_phase,
                "removed_from_final_screen": True,
                "action_was_attempted": False,
                "ui_frame_invalidated": ui_frame_invalidated,
                "terminal_state": "",
            }
        action_outcome = classify_action_result(
            "image",
            {
                **normalized,
                "action_phase": action_phase,
                "error_code": normalized.get("reason"),
                "evidence": transaction,
            },
            source_message_key=source_key,
        )
        normalized["action_outcome"] = action_outcome
        raw_terminal_state = str(
            normalized.get("state") or ""
        ).strip()
        if (
            raw_terminal_state != "cancelled"
            and action_outcome["result"] != "completed"
        ):
            raw_reason = str(
                action_outcome.get("error_code")
                or normalized.get("reason")
                or ""
            )
            reason_detail = str(
                transaction.get("status")
                or normalized.get("reason_detail")
                or raw_reason
            )
            normalized = {
                **normalized,
                "state": "failed",
                "reason": formal_image_failure_code(raw_reason),
                "reason_detail": reason_detail,
                "action_outcome": action_outcome,
            }
        return {
            "result": normalized,
            "transaction": transaction,
            "diagnostics": diagnostics,
            "action_outcome": action_outcome,
            "action_phase": action_phase,
            "removed_from_final_screen": False,
            "action_was_attempted": action_phase != "not_attempted",
            "ui_frame_invalidated": ui_frame_invalidated,
            "raw_terminal_state": raw_terminal_state,
            "terminal_state": str(normalized.get("state") or "failed"),
        }

    @staticmethod
    def _persist_one_image_slot_terminal(
        *,
        target: WechatReadTarget,
        payload: dict[str, Any],
        observation: dict[str, Any],
        source_key: str,
        origin_read_run_id: str,
        result: dict[str, Any],
        stats: dict[str, Any],
    ) -> tuple[dict[str, Any], str, str]:
        terminal_state = str(result.get("state") or "failed")
        if terminal_state not in {"completed", "failed"}:
            terminal_state = "failed"
            result = {
                **result,
                "state": "failed",
                "reason": "vision_terminal_state_invalid",
            }
        terminal_observation = apply_image_terminal_result(
            observation,
            result,
        )
        projected_state = str(
            terminal_observation.get("item_state") or terminal_state
        )
        if projected_state in {"completed", "failed"}:
            terminal_state = projected_state
        terminal_reason = str(
            terminal_observation.get("error_code")
            or result.get("reason")
            or ""
        )
        if terminal_state == "failed":
            artifact_dir_value = str(
                payload.get("artifact_dir") or ""
            ).strip()
            if artifact_dir_value:
                record_artifact_outcome(
                    Path(artifact_dir_value),
                    {
                        "ok": False,
                        "error_code": (
                            terminal_reason or "C2_IMAGE_SLOT_FAILED"
                        ),
                    },
                )
        ledger_result: dict[str, Any] = {
            "state": terminal_state,
            "reason": terminal_reason,
            "reason_detail": str(result.get("reason_detail") or ""),
            "transaction": _safe_c2_image_transaction(
                result.get("transaction")
            ),
            "replayable_observation": replayable_image_observation(
                terminal_observation,
                source_message_key=source_key,
            ),
        }
        if terminal_state == "completed":
            ledger_result.update(
                {
                    "customer_image_understanding": dict(
                        terminal_observation.get(
                            "customer_image_understanding"
                        )
                        or {}
                    ),
                    "visual_bridge_input": dict(
                        terminal_observation.get("visual_bridge_input")
                        or {}
                    ),
                }
            )
        save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key=source_key,
            origin_read_run_id=origin_read_run_id,
            dedupe_key=None,
            message_type="image",
            terminal_state=terminal_state,
            ingest_state=(
                "waiting"
                if terminal_state in {"completed", "failed"}
                else "not_required"
            ),
            result=ledger_result,
        )
        mark_image_terminal(
            stats,
            source_key,
            terminal_state=terminal_state,
        )
        return terminal_observation, terminal_state, terminal_reason

    @staticmethod
    def _merge_waiting_image_facts(
        *,
        target: WechatReadTarget,
        sidecar_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Restore terminal image facts that are not yet confirmed by backend."""

        payload = dict(sidecar_payload)
        observations = [
            dict(item) if isinstance(item, dict) else item
            for item in (payload.get("observations") or [])
        ]
        visible_source_keys: set[str] = set()
        for observation in observations:
            if (
                not isinstance(observation, dict)
                or observation.get("row_kind") != "image_bubble"
            ):
                continue
            source = (
                observation.get("source_message")
                if isinstance(observation.get("source_message"), dict)
                else {}
            )
            source_key = str(
                source.get("source_message_key") or ""
            ).strip()
            if not source_key:
                try:
                    source_key = image_observation_source_key(
                        target,
                        observation,
                    )
                except ValueError:
                    source_key = ""
            if source_key:
                visible_source_keys.add(source_key)

        restored = 0
        restored_observations: list[dict[str, Any]] = []
        for ledger in list_c2_ledger_entries(
            target.conversation_id,
            message_type="image",
            ingest_state="waiting",
        ):
            source_key = str(
                ledger.get("source_message_key") or ""
            ).strip()
            if not source_key or source_key in visible_source_keys:
                continue
            result = (
                ledger.get("result")
                if isinstance(ledger.get("result"), dict)
                else {}
            )
            replayable = (
                result.get("replayable_observation")
                if isinstance(
                    result.get("replayable_observation"),
                    dict,
                )
                else {}
            )
            if (
                replayable.get("row_kind") != "image_bubble"
                or str(replayable.get("item_state") or "")
                not in {"completed", "failed"}
            ):
                continue
            restored_observation = dict(replayable)
            # This fact is durable, but its last visible coordinates are not
            # current-screen evidence. Keeping them would let an old frame
            # reorder the recovered image among newly visible messages.
            for field in ("bubble_rect", "bounds", "anchor"):
                restored_observation.pop(field, None)
            restored_source = (
                dict(restored_observation.get("source_message"))
                if isinstance(
                    restored_observation.get("source_message"),
                    dict,
                )
                else {}
            )
            for field in ("bubble_rect", "bounds", "anchor"):
                restored_source.pop(field, None)
            restored_observation["source_message"] = restored_source
            restored_observations.append(restored_observation)
            visible_source_keys.add(source_key)
            restored += 1

        if restored:
            def stable_sequence(item: Any) -> int | None:
                if not isinstance(item, dict):
                    return None
                match = re.fullmatch(
                    r"worker-message-(\d+)",
                    str(item.get("_worker_stable_id") or "").strip(),
                )
                return int(match.group(1)) if match else None

            combined = [*restored_observations, *observations]
            sequences = [stable_sequence(item) for item in combined]
            if all(sequence is not None for sequence in sequences):
                combined = [
                    item
                    for _, item in sorted(
                        zip(sequences, combined),
                        key=lambda pair: int(pair[0] or 0),
                    )
                ]
            observations = combined
            append_log(
                "INFO",
                "c2_image_fact_restored",
                "已从本地账本恢复尚未确认入库的完整图片事实。",
                metadata={
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "restored_count": restored,
                    "image_persisted": False,
                },
            )
        payload["observations"] = observations
        return payload

    def _restore_visible_failed_voice_facts(
        self,
        *,
        target: WechatReadTarget,
        sidecar_payload: dict[str, Any],
    ) -> tuple[set[str], dict[str, str]]:
        """Project waiting failed voice facts without repeating UI actions."""

        grouped: dict[str, set[str]] = {}
        for observation in sidecar_payload.get("observations") or []:
            if (
                not isinstance(observation, dict)
                or observation.get("row_kind") != "voice_bubble"
                or observation.get("voice_state") != "untranscribed"
            ):
                continue
            try:
                source_key = voice_observation_source_key(
                    target,
                    observation,
                )
            except ValueError:
                continue
            ledger = load_c2_ledger_entry(
                target.conversation_id,
                source_key,
            )
            if (
                not ledger
                or ledger.get("terminal_state") != "failed"
                or ledger.get("ingest_state") == "confirmed"
            ):
                continue
            result = (
                ledger.get("result")
                if isinstance(ledger.get("result"), dict)
                else {}
            )
            error_code = str(
                result.get("error_code")
                or "VOICE_TRANSCRIBE_FAILED"
            )
            grouped.setdefault(error_code, set()).add(source_key)

        roles: dict[str, str] = {}
        source_keys: set[str] = set()
        for error_code, grouped_keys in grouped.items():
            roles.update(
                self._annotate_failed_voice_observations(
                    target=target,
                    sidecar_payload=sidecar_payload,
                    failed_source_keys=grouped_keys,
                    error_code=error_code,
                )
            )
            source_keys.update(grouped_keys)
        return source_keys, roles

    def _process_final_image_slots(
        self,
        *,
        binding: Binding,
        target: WechatReadTarget,
        sidecar_payload: dict[str, Any],
        enforce_read_targets: bool,
        cancel_check: Callable[[], bool | str] | None = None,
        allowed_new_source_keys: set[str] | None = None,
        flow_outcomes: FlowOutcomeAccumulator | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        observations = sidecar_payload.get("observations")
        stats = new_image_phase_result()
        if not isinstance(observations, list):
            return sidecar_payload, stats
        payload = dict(sidecar_payload)
        enriched_observations = [dict(item) if isinstance(item, dict) else item for item in observations]
        payload["observations"] = enriched_observations

        def visual_top(item: dict[str, Any]) -> float:
            rect = item.get("bubble_rect")
            try:
                return float(rect.get("top") if isinstance(rect, dict) else rect[1])
            except (TypeError, ValueError, IndexError):
                return float("inf")

        indexed = [
            (index, item)
            for index, item in enumerate(enriched_observations)
            if isinstance(item, dict) and item.get("row_kind") == "image_bubble"
        ]
        stats["discovered"] = len(indexed)
        image_action_started = False
        for index, observation in sorted(indexed, key=lambda value: (visual_top(value[1]), value[0])):
            source = observation.get("source_message") if isinstance(observation.get("source_message"), dict) else {}
            try:
                source_key = image_observation_source_key(
                    target,
                    observation,
                )
            except ValueError:
                source_key = ""
            common_metadata = {
                "conversation_id": target.conversation_id,
                "remark_code": target.remark_code,
                "authorization_revision": target.authorization_revision,
                "source_message_key": source_key,
                "observation_id": str(observation.get("observation_id") or ""),
                "sender_role": str(observation.get("sender_role") or ""),
                "sender_role_source": str(observation.get("sender_role_source") or ""),
                "bubble_rect": observation.get("bubble_rect"),
                "authoritative_frame_source": str(payload.get("authoritative_frame_source") or ""),
                "sidecar_run_id": str(payload.get("sidecar_run_id") or ""),
                # These refer to the existing full-frame C2 evidence. Image
                # pixels and image crops are never written by this flow.
                "authoritative_screenshot": payload.get("screenshot_path") or payload.get("screenshot"),
                "artifact_dir": payload.get("artifact_dir"),
                "review_path": payload.get("review_path"),
                "image_persisted": False,
            }
            append_log(
                "INFO",
                "c2_image_slot_discovered",
                "C2 在最终权威画面发现图片槽位。",
                metadata=common_metadata,
            )
            if not observation_role_is_trusted(observation):
                append_log(
                    "WARN",
                    "c2_image_role_rejected",
                    "C2 图片初始同行头像角色不可信；本轮不建立图片身份、不落终态、不调用 Vision。",
                    error_code="MESSAGE_IDENTITY_UNCONFIRMED",
                    metadata=common_metadata,
                )
                continue
            if not source_key:
                append_log(
                    "WARN",
                    "c2_image_identity_rejected",
                    "C2 图片缺少 canonical_visual_id 或统一序列身份；本轮不落终态、不调用 Vision。",
                    error_code="MESSAGE_SEQUENCE_IDENTITY_MISSING",
                    metadata=common_metadata,
                )
                continue
            observation["source_message"] = {
                **source,
                "source_message_key": source_key,
            }
            ledger = load_c2_ledger_entry(target.conversation_id, source_key)
            if ledger and ledger.get("terminal_state") in {"completed", "failed"}:
                result = ledger.get("result") if isinstance(ledger.get("result"), dict) else {}
                result = {**result, "state": ledger.get("terminal_state")}
                enriched_observations[index] = apply_image_terminal_result(observation, result)
                mark_image_terminal(
                    stats,
                    source_key,
                    terminal_state=str(ledger.get("terminal_state")),
                    cached=True,
                )
                append_log(
                    "INFO",
                    "c2_image_slot_cached",
                    "C2 图片槽位命中本地终态，不重复右键、复制或调用 Vision。",
                    metadata={
                        **common_metadata,
                        "terminal_state": str(ledger.get("terminal_state") or ""),
                        "ingest_state": str(ledger.get("ingest_state") or ""),
                    },
                )
                continue
            access_decision = self._image_slot_access_decision(
                binding=binding,
                target=target,
                observation=observation,
                source_key=source_key,
                allowed_new_source_keys=allowed_new_source_keys,
                enforce_read_targets=enforce_read_targets,
            )
            if access_decision == "role_untrusted":
                append_log(
                    "WARN",
                    "c2_image_role_rejected",
                    "C2 图片初始同行头像角色不可信；本轮不建立图片身份、不落终态、不调用 Vision。",
                    error_code="MESSAGE_IDENTITY_UNCONFIRMED",
                    metadata=common_metadata,
                )
                continue
            elif access_decision == "not_new":
                append_log(
                    "INFO",
                    "c2_image_slot_not_new",
                    "图片不满足 current_read_run + not_enqueued；历史终态由 ledger 复用，OUTBOX 只重传原 JSON。",
                    metadata=common_metadata,
                )
                continue
            elif access_decision == "authorization_revoked":
                stats["authorization_revoked"] = 1
                append_log(
                    "WARN",
                    "c2_image_authorization_checked",
                    "C2 图片处理前后端授权已撤销。",
                    error_code="C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                    metadata={**common_metadata, "allowed": False},
                )
                break
            else:
                if image_action_started:
                    # Every irreversible image operation invalidates the
                    # frame. Remaining candidates are selected only after a
                    # fresh read and sequence alignment.
                    continue
                append_log(
                    "INFO",
                    "c2_image_role_confirmed",
                    "C2 图片已通过统一同行头像角色规则。",
                    metadata=common_metadata,
                )
                append_log(
                    "INFO",
                    "c2_image_authorization_checked",
                    "C2 图片处理前已重新校验后端授权。",
                    metadata={**common_metadata, "allowed": True},
                )
                append_log(
                    "INFO",
                    "c2_image_slot_started",
                    "C2 图片开始执行内存剪贴板与 OmniAuto Vision 流程。",
                    metadata=common_metadata,
                )
                result = self._execute_one_image_slot_vision(
                    target=target,
                    payload=payload,
                    observation=observation,
                    source_key=source_key,
                    cancel_check=cancel_check,
                    flow_outcomes=flow_outcomes,
                )
            normalized = self._normalize_one_image_slot_result(
                result,
                source_key=source_key,
            )
            result = normalized["result"]
            transaction = normalized["transaction"]
            diagnostics = normalized["diagnostics"]
            result_reason = str(result.get("reason") or "")
            result_action_phase = normalized["action_phase"]
            if normalized["removed_from_final_screen"]:
                image_action_started = True
                stats["removed_from_final_screen"] += 1
                mark_image_removed_from_final_screen(
                    stats,
                    source_key,
                )
                enriched_observations[index] = None
                append_log(
                    "INFO",
                    "c2_image_slot_removed_from_final_screen",
                    "动作前重建画面后图片已不在当前屏，本轮删除旧候选，不右键、不门禁。",
                    metadata={
                        **common_metadata,
                        "reason": result_reason,
                        "action_phase": result_action_phase,
                    },
                )
                continue
            image_action = normalized["action_outcome"]
            raw_terminal_state = normalized["raw_terminal_state"]
            action_was_attempted = normalized["action_was_attempted"]
            if normalized["ui_frame_invalidated"]:
                image_action_started = True
                mark_image_ui_frame_invalidated(stats, source_key)
            if action_was_attempted:
                mark_image_action(stats, source_key)
            if (
                flow_outcomes is not None
                and (
                    action_was_attempted
                    or raw_terminal_state in {"completed", "failed"}
                )
            ):
                image_evidence = dict(image_action.get("evidence") or {})
                image_evidence["action_kind"] = "image"
                image_action["evidence"] = image_evidence
                image_action["terminal_payload"] = {
                    "state": raw_terminal_state,
                    "error_code": (
                        str(result.get("reason") or "")
                        if raw_terminal_state == "failed"
                        else None
                    ),
                    "reason_detail": (
                        str(transaction.get("status") or result_reason)
                        if raw_terminal_state == "failed"
                        else None
                    ),
                    "customer_image_understanding": (
                        dict(result.get("customer_image_understanding") or {})
                        if isinstance(
                            result.get("customer_image_understanding"), dict
                        )
                        else None
                    ),
                    "visual_bridge_input": (
                        dict(result.get("visual_bridge_input") or {})
                        if isinstance(result.get("visual_bridge_input"), dict)
                        else None
                    ),
                    "transaction": _safe_c2_image_transaction(
                        result.get("transaction")
                    ),
                }
                flow_outcomes.record(image_action)
            for diagnostic_event in diagnostics.get("events") or []:
                safe_event = _safe_c2_image_diagnostic_event(diagnostic_event)
                append_log(
                    "WARN" if safe_event.get("status") == "failed" else "INFO",
                    "c2_image_stage",
                    "C2 图片处理阶段证据。",
                    error_code=(str(safe_event.get("reason") or "") or None)
                    if safe_event.get("status") == "failed"
                    else None,
                    metadata={**common_metadata, **safe_event},
                )
            terminal_state = str(result.get("state") or "failed")
            if terminal_state == "cancelled":
                stats["authorization_revoked"] = 1
                append_log(
                    "WARN",
                    "c2_image_slot_cancelled",
                    "C2 图片处理期间授权已撤销；未触发动作可在重新授权后处理，已触发动作证据由统一收尾落账且不会重复执行。",
                    error_code="C2_IMAGE_PROCESSING_CANCELLED",
                    metadata={
                        **common_metadata,
                        "reason": str(result.get("reason") or "vision_cancelled"),
                        "image_persisted": False,
                    },
                )
                break
            if (
                enforce_read_targets
                and (
                    self.stop_event.is_set()
                    or not self._backend_still_allows_read_target_lightweight(
                        binding,
                        target,
                    )
                )
            ):
                stats["authorization_revoked"] = 1
                append_log(
                    "WARN",
                    "c2_image_result_discarded_after_authorization_check",
                    "Vision 返回后授权已撤销；动作结果已进入本地统一收尾，但本轮不会使用旧授权上报后端。",
                    error_code="C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                    metadata={**common_metadata, "image_persisted": False},
                )
                break
            append_log(
                "INFO" if terminal_state == "completed" else "WARN",
                "c2_image_slot_finished",
                "C2 单张图片处理已得到明确终态。",
                error_code=(str(result.get("reason") or "") or None)
                if terminal_state != "completed"
                else None,
                metadata={
                    **common_metadata,
                    "terminal_state": str(result.get("state") or "failed"),
                    "reason": str(result.get("reason") or ""),
                    "error_type": str(result.get("error_type") or ""),
                    "total_duration_ms": diagnostics.get("total_duration_ms"),
                    "transaction": _safe_c2_image_transaction(result.get("transaction")),
                    "image_persisted": False,
                },
            )
            (
                terminal_observation,
                terminal_state,
                terminal_reason,
            ) = self._persist_one_image_slot_terminal(
                target=target,
                payload=payload,
                observation=observation,
                source_key=source_key,
                origin_read_run_id=(
                    flow_outcomes.origin_read_run_id
                    if flow_outcomes is not None
                    else ""
                ),
                result=result,
                stats=stats,
            )
            enriched_observations[index] = terminal_observation
            summary = str(terminal_observation.get("content_clean") or "") if isinstance(terminal_observation, dict) else ""
            append_log(
                "INFO" if terminal_state == "completed" else "WARN",
                "c2_image_slot_terminalized",
                "C2 图片结果已映射回统一消息槽位。",
                error_code=(str(result.get("reason") or "") or None) if terminal_state != "completed" else None,
                metadata={
                    **common_metadata,
                    "terminal_state": terminal_state,
                    "reason": terminal_reason,
                    "vision_summary_length": len(summary),
                    "vision_summary_sha256": _c2_text_fingerprint(summary),
                    "transaction": _safe_c2_image_transaction(
                        result.get("transaction")
                    ),
                    "diagnostics": {
                        "trace_id": str(diagnostics.get("trace_id") or ""),
                        "total_duration_ms": diagnostics.get("total_duration_ms"),
                        "events": [
                            _safe_c2_image_diagnostic_event(item)
                            for item in (diagnostics.get("events") or [])
                            if isinstance(item, dict)
                        ],
                    },
                    "image_persisted": False,
                },
                force_incident=terminal_state == "failed",
            )
        payload["observations"] = [
            item
            for item in enriched_observations
            if isinstance(item, dict)
        ]
        existing_validation_errors = [
            dict(item)
            for item in (
                payload.get("observation_validation_errors") or []
            )
            if isinstance(item, dict)
        ]
        payload["observation_validation_errors"] = existing_validation_errors + [
            {
                "observation_id": str(item.get("observation_id") or ""),
                "row_kind": str(item.get("row_kind") or ""),
                "error_codes": list(item.get("contract_errors") or []),
            }
            for item in payload["observations"]
            if isinstance(item, dict) and item.get("contract_errors")
        ]
        return payload, finalize_image_phase_result(stats)

    def _wait_and_send_current_c3_batch(
        self,
        *,
        binding: Binding,
        target: WechatReadTarget,
        batch_id: str,
        cancel_check: Callable[[], bool | str],
        recovered_task: Task | None = None,
    ) -> dict[str, Any]:
        previous_task = self.current_task
        try:
            return self._wait_and_send_current_c3_batch_impl(
                binding=binding,
                target=target,
                batch_id=batch_id,
                cancel_check=cancel_check,
                recovered_task=recovered_task,
            )
        finally:
            # A normal C2 flow starts without a service task and claims the
            # chat_reply only when Brain is ready. Recovery flows already own
            # their outer task lifecycle.
            if previous_task is None and self.current_task is not None:
                self._stop_task_lease_guard()
                self.current_task = None
                self.on_task(None)

    def _wait_and_send_current_c3_batch_impl(
        self,
        *,
        binding: Binding,
        target: WechatReadTarget,
        batch_id: str,
        cancel_check: Callable[[], bool | str],
        recovered_task: Task | None = None,
    ) -> dict[str, Any]:
        """Wait for Brain and send under the C2 lease already held by the caller."""
        no_progress_limit = max(1.0, CONFIG.c3_brain_no_progress_watchdog_seconds)
        last_progress_at = time.monotonic()
        last_status_fingerprint = ""
        current_batch_id = batch_id
        while True:
            if cancel_check():
                error_code = _ui_lock_cancel_reason(self.current_ui_lock) or "WORKER_INTERRUPTED"
                return {"ok": False, "error_code": error_code, "batch_id": current_batch_id}
            try:
                status = self.api.get_wechat_message_batch(binding, current_batch_id)
            except ApiError as exc:
                if exc.status_code in {401, 403, 404}:
                    return {"ok": False, "error_code": exc.code, "batch_id": current_batch_id}
                if time.monotonic() - last_progress_at >= no_progress_limit:
                    return {
                        "ok": False,
                        "error_code": "C3_BRAIN_NETWORK_NO_PROGRESS_WATCHDOG",
                        "batch_id": current_batch_id,
                    }
                time.sleep(max(0.1, CONFIG.c3_brain_poll_interval_seconds))
                continue
            except Exception:
                if time.monotonic() - last_progress_at >= no_progress_limit:
                    return {
                        "ok": False,
                        "error_code": "C3_BRAIN_NETWORK_NO_PROGRESS_WATCHDOG",
                        "batch_id": current_batch_id,
                    }
                time.sleep(max(0.1, CONFIG.c3_brain_poll_interval_seconds))
                continue
            self._apply_batch_continuation_to_target(status, target)
            if not status.get("processing") and status.get("decision") != "send_reply":
                return {"ok": True, "batch": status, "sent": False}
            if not self._batch_authorization_allows_target(status, target):
                return {
                    "ok": False,
                    "error_code": "C2_TARGET_NOT_ALLOWED_BY_BATCH_AUTHORIZATION",
                    "batch_id": current_batch_id,
                }
            status_fingerprint = json.dumps(
                {
                    "batch_id": status.get("batch_id"),
                    "batch_status": status.get("batch_status"),
                    "decision": status.get("decision"),
                    "updated_at": status.get("updated_at"),
                    "error_code": status.get("error_code"),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if status_fingerprint != last_status_fingerprint:
                last_status_fingerprint = status_fingerprint
                last_progress_at = time.monotonic()
            if status.get("processing"):
                if time.monotonic() - last_progress_at >= no_progress_limit:
                    return {
                        "ok": False,
                        "error_code": "C3_BRAIN_STATE_NO_PROGRESS_WATCHDOG",
                        "batch_id": current_batch_id,
                    }
                self.current_step = "c3_brain_waiting"
                if self.current_ui_lock:
                    self.current_ui_lock.update_step("c3_brain_waiting")
                time.sleep(max(0.1, CONFIG.c3_brain_poll_interval_seconds))
                continue
            task_payload = (
                status.get("task")
                if isinstance(status.get("task"), dict)
                else {}
            )
            reply_task_id = str(task_payload.get("id") or "").strip()
            self._emit_runtime_process(
                {"event": "reply_ready", **self._runtime_process_context}
            )
            # Reuse the only complete C2 fact flow under the lease already held.
            # It may transcribe voice and run Vision, but it may only validate the
            # current chat; pre-send refresh must never search or switch sessions.
            refresh_read = self._read_one_wechat_target(
                binding,
                target,
                current_step="pre_send_refresh",
                operation_phase=C2_PRE_SEND_REFRESH_PHASE,
                allow_during_current_task=True,
                enforce_read_targets=True,
                held_lease=self.current_ui_lock,
                current_only=True,
                wait_for_brain=False,
            )
            if not refresh_read.get("ok"):
                reply_task_settled = (
                    self._settle_chat_reply_context_failure_before_unlock(
                        binding,
                        task_id=reply_task_id,
                        source_error_code=str(
                            refresh_read.get("error_code")
                            or "PRE_SEND_REFRESH_FAILED"
                        ),
                    )
                )
                return {
                    "ok": False,
                    "error_code": str(refresh_read.get("error_code") or "PRE_SEND_REFRESH_FAILED"),
                    "batch": status,
                    "pre_send_refresh": refresh_read,
                    "reply_task_settled": reply_task_settled,
                }
            if int(refresh_read.get("new_self_message_count") or 0) > 0:
                return {
                    "ok": True,
                    "batch": status,
                    "sent": False,
                    "reason": "sales_replied_during_brain_wait",
                }
            refresh_result = refresh_read.get("result") if isinstance(refresh_read.get("result"), dict) else {}
            replacement = refresh_result.get("message_batch") if isinstance(refresh_result, dict) else None
            if isinstance(replacement, dict) and replacement.get("batch_id") and replacement.get("batch_id") != current_batch_id:
                current_batch_id = str(replacement["batch_id"])
                last_status_fingerprint = ""
                last_progress_at = time.monotonic()
                continue
            if int(refresh_read.get("new_customer_message_count") or 0) > 0:
                return {
                    "ok": False,
                    "error_code": "C3_REPLACEMENT_BATCH_MISSING",
                    "batch": status,
                    "pre_send_refresh": refresh_read,
                }
            expected_context_guard = (
                refresh_read.get("send_context_guard")
                if isinstance(refresh_read.get("send_context_guard"), dict)
                else {}
            )
            if (
                int(expected_context_guard.get("schema_version") or 0) != 1
                or not isinstance(expected_context_guard.get("sequence"), list)
            ):
                return {
                    "ok": False,
                    "error_code": "C3_SEND_CONTEXT_GUARD_MISSING",
                    "batch": status,
                    "pre_send_refresh": refresh_read,
                }
            send_identity_frame = (
                refresh_read.get("send_identity_frame")
                if isinstance(refresh_read.get("send_identity_frame"), dict)
                else {}
            )
            pre_send_observations = [
                dict(item)
                for item in (send_identity_frame.get("observations") or [])
                if isinstance(item, dict)
            ]
            pre_send_frame_id = str(
                send_identity_frame.get("frame_id") or ""
            ).strip()
            if (
                send_identity_frame.get("contract_revision")
                != contract_revision()
                or not pre_send_frame_id
                or not pre_send_observations
            ):
                return {
                    "ok": False,
                    "error_code": "C3_SEND_IDENTITY_FRAME_MISSING",
                    "batch": status,
                    "pre_send_refresh": refresh_read,
                }

            if not task_payload:
                return {"ok": False, "error_code": "C3_REPLY_TASK_MISSING", "batch": status}
            pending_task = Task.from_api(task_payload)
            if recovered_task is not None and recovered_task.id == pending_task.id:
                task = recovered_task
            else:
                if not self._can_continue_inflight_flow():
                    return {
                        "ok": False,
                        "error_code": "WORKER_EMERGENCY_STOPPED",
                        "failure_step": "before_claim_task",
                        "batch": status,
                    }
                try:
                    task = self.api.claim_task(
                        binding,
                        pending_task,
                        claim_source="c2_conversation_flow",
                        conversation_id=target.conversation_id,
                    )
                except ApiError as exc:
                    return {
                        "ok": False,
                        "error_code": exc.code,
                        "failure_step": "claim_task",
                        "batch": status,
                    }
            if not self._can_continue_inflight_flow():
                return {
                    "ok": False,
                    "error_code": "WORKER_EMERGENCY_STOPPED",
                    "failure_step": "after_claim_task",
                    "batch": status,
                }
            self.current_task = task
            self.on_task(task)
            if (
                self.current_task_lease is None
                or self.current_task_lease.task.id != task.id
            ):
                self._start_task_lease_guard(binding, task)
            if self.current_task_lease.cancel_requested():
                return {
                    "ok": False,
                    "error_code": (
                        self.current_task_lease.error_code
                        or "TASK_LEASE_RENEW_FAILED"
                    ),
                    "failure_step": "task_lease",
                    "batch": status,
                }
            if (
                not self._can_continue_inflight_flow()
                or not self._backend_still_allows_read_target(binding, target)
            ):
                return {
                    "ok": False,
                    "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                    "failure_step": "before_claim_send",
                    "batch": status,
                }
            try:
                claim = self.api.claim_send(binding, task)
            except ApiError as exc:
                try:
                    self.api.fail_task(binding, task.id, exc.code, "claim_send", str(exc))
                except Exception as report_exc:
                    append_log(
                        "ERROR",
                        "c3_reply_claim_send_failure_report_failed",
                        str(report_exc),
                        task_id=task.id,
                        error_code=exc.code,
                    )
                return {
                    "ok": False,
                    "error_code": exc.code,
                    "failure_step": "claim_send",
                    "batch": status,
                }
            save_reply_send_intent(
                reply_action_id=claim.reply_action_id,
                task_id=claim.task_id,
                send_token=claim.send_token,
                reply_text_hash=claim.reply_text_hash,
            )
            send_journal_path = (
                self.bridge.send_transaction_journal_path(
                    claim.reply_action_id
                )
            )
            journal_phase = self._send_transaction_journal_phase(
                claim.reply_action_id
            )
            if claim.duplicated and journal_phase != "not_attempted":
                self._mark_possible_ai_send_alignment(
                    conversation_id=target.conversation_id,
                    reply_action_id=claim.reply_action_id,
                    reconciliation_state=(
                        "journal_confirmed_identity_pending"
                        if journal_phase == "confirmed"
                        else "triggered_identity_pending"
                    ),
                    physical_send_confirmed=(journal_phase == "confirmed"),
                )
                pending_ack = load_reply_send_ack_outbox(claim.reply_action_id)
                if pending_ack and pending_ack.get("status") == "intent":
                    recovered_send_result = (
                        "sent" if journal_phase == "confirmed" else "unknown"
                    )
                    finalize_reply_send_ack(
                        reply_action_id=claim.reply_action_id,
                        ack_payload=self._reply_send_ack_payload(
                            send_result=recovered_send_result,
                            action_phase=journal_phase,
                            reply_text_hash=claim.reply_text_hash,
                            error_code=(
                                None
                                if recovered_send_result == "sent"
                                else "SEND_CLAIM_RECOVERED_WITHOUT_LOCAL_RESULT"
                            ),
                            remark=(
                                "根据 Sidecar 物理动作日志恢复发送结果，"
                                "不会重复操作微信。"
                            ),
                        ),
                    )
                    pending_ack = load_reply_send_ack_outbox(
                        claim.reply_action_id
                    )
                ack_confirmed = bool(pending_ack) and self._attempt_reply_send_ack(
                    binding,
                    pending_ack,
                )
                recovered_result = (
                    (pending_ack or {}).get("ack_payload")
                    if isinstance((pending_ack or {}).get("ack_payload"), dict)
                    else {}
                )
                return {
                    "ok": ack_confirmed,
                    "error_code": (
                        None
                        if ack_confirmed
                        else "SEND_ACK_REPLAY_PENDING"
                    ),
                    "batch": status,
                    "sent": (
                        ack_confirmed
                        and recovered_result.get("send_result") == "sent"
                    ),
                    "reason": "duplicate_send_claim_suppressed",
                }
            reserved_send_stable_id = self._reserve_worker_sequence(
                target,
                reservation_key=f"send-action:{claim.reply_action_id}",
            )
            committed_pre_ids = {
                str(item.get("observation_id") or "").strip(): str(
                    item.get("_worker_stable_id") or ""
                ).strip()
                for item in pre_send_observations
                if str(item.get("observation_id") or "").strip()
                and str(item.get("_worker_stable_id") or "").strip()
            }
            pre_send_identity_sequence = build_pre_action_identity_sequence(
                pre_send_observations,
                committed_ids=committed_pre_ids,
            )
            if not pre_send_identity_sequence:
                self._queue_and_submit_reply_send_ack(
                    binding,
                    claim,
                    send_result="failed",
                    action_phase="not_attempted",
                    reply_text_hash=claim.reply_text_hash,
                    error_code="C3_SEND_IDENTITY_SEQUENCE_EMPTY",
                    remark="发送前消息序列为空，未操作微信输入框。",
                )
                return {
                    "ok": False,
                    "error_code": "C3_SEND_IDENTITY_SEQUENCE_EMPTY",
                    "batch": status,
                }
            if not send_journal_path.exists():
                initialize_action_journal(
                    send_journal_path,
                    action_kind="send",
                    transaction_id=claim.reply_action_id,
                    conversation_id=target.conversation_id,
                    items=[
                        {
                            "source_message_key": claim.reply_action_id,
                            "physical_anchor_keys": [],
                        }
                    ],
                    pre_action_identity_sequence=(
                        pre_send_identity_sequence
                    ),
                    pre_frame_id=pre_send_frame_id,
                    canonical_action_id=claim.reply_action_id,
                    reserved_worker_stable_id=reserved_send_stable_id,
                )
            else:
                existing_send_journal = read_action_journal(
                    send_journal_path
                )
                if (
                    str(
                        existing_send_journal.get("canonical_action_id")
                        or ""
                    ).strip()
                    != claim.reply_action_id
                    or str(
                        existing_send_journal.get(
                            "reserved_worker_stable_id"
                        )
                        or ""
                    ).strip()
                    != reserved_send_stable_id
                    or existing_send_journal.get(
                        "pre_action_identity_sequence"
                    )
                    != pre_send_identity_sequence
                ):
                    self._queue_and_submit_reply_send_ack(
                        binding,
                        claim,
                        send_result="failed",
                        action_phase="not_attempted",
                        reply_text_hash=claim.reply_text_hash,
                        error_code="C3_SEND_IDENTITY_JOURNAL_CONFLICT",
                        remark="发送身份日志与当前序列不一致，未操作微信输入框。",
                    )
                    return {
                        "ok": False,
                        "error_code": "C3_SEND_IDENTITY_JOURNAL_CONFLICT",
                        "batch": status,
                    }
            final_send_text = self._canonical_reply_text(claim.reply_text)
            if not final_send_text or final_send_text != claim.reply_text:
                actual_hash = self._reply_text_hash(final_send_text)
                self._queue_and_submit_reply_send_ack(
                    binding, claim, send_result="failed", action_phase="not_attempted", reply_text_hash=actual_hash,
                    error_code="SEND_TEXT_NOT_CANONICAL", remark="后端未提供唯一的最终发送文本，未发送。",
                )
                return {"ok": False, "error_code": "SEND_TEXT_NOT_CANONICAL", "batch": status}
            actual_hash = self._reply_text_hash(final_send_text)
            if claim.reply_text_hash and actual_hash != claim.reply_text_hash:
                self._queue_and_submit_reply_send_ack(
                    binding, claim, send_result="failed", action_phase="not_attempted", reply_text_hash=actual_hash,
                    error_code="SEND_TEXT_HASH_MISMATCH", remark="Brain 原文 hash 不一致，未发送。",
                )
                return {"ok": False, "error_code": "SEND_TEXT_HASH_MISMATCH", "batch": status}
            if self.stop_event.is_set() or not self._backend_still_allows_read_target(binding, target):
                self._queue_and_submit_reply_send_ack(
                    binding,
                    claim,
                    send_result="failed",
                    action_phase="not_attempted",
                    reply_text_hash=actual_hash,
                    error_code="C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                    remark="开始输入前授权已停止，未操作微信输入框。",
                )
                return {
                    "ok": False,
                    "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                    "failure_step": "before_send_input",
                    "batch": status,
                }
            last_send_authorization_check = 0.0

            def send_cancel_requested() -> bool | str:
                nonlocal last_send_authorization_check
                ui_lock_reason = _ui_lock_cancel_reason(self.current_ui_lock)
                if ui_lock_reason:
                    return ui_lock_reason
                if (
                    not self._can_continue_inflight_flow()
                    or (
                        self.current_task_lease is not None
                        and self.current_task_lease.cancel_requested()
                    )
                ):
                    return True
                now = time.monotonic()
                if now - last_send_authorization_check < 0.5:
                    return False
                last_send_authorization_check = now
                try:
                    current_status = self.api.get_wechat_message_batch(
                        binding,
                        current_batch_id,
                    )
                except Exception:
                    return True
                return not self._batch_authorization_allows_target(
                    current_status,
                    target,
                )

            self._record_possible_ai_send(
                target=target,
                reply_action_id=claim.reply_action_id,
                reply_text_hash=actual_hash,
                reserved_worker_stable_id=reserved_send_stable_id,
                pre_frame_id=pre_send_frame_id,
                pre_action_identity_sequence=pre_send_identity_sequence,
            )
            self._emit_runtime_process(
                {"event": "send_started", **self._runtime_process_context}
            )
            reply_send_stage = (
                StageTimer(
                    process_run_id=(
                        task.process_run_id
                        or target.process_run_id
                        or ""
                    ),
                    conversation_id=target.conversation_id,
                    stage_name=(
                        "c4.reply_send_confirm"
                        if str(status.get("trigger_type") or "") == "recall"
                        else "c3.reply_send_confirm"
                    ),
                    component="worker",
                    trace_id=str(uuid.uuid4()),
                )
                if (task.process_run_id or target.process_run_id)
                else None
            )
            try:
                sidecar_result = self.bridge.send_reply(
                    target=target.remark_code or target.display_name,
                    rpa_session_key="",
                    text=final_send_text,
                    task_id=task.id,
                    reply_action_id=claim.reply_action_id,
                    current_only=True,
                    expected_context_guard=expected_context_guard,
                    cancel_check=send_cancel_requested,
                )
            except Exception as exc:
                if reply_send_stage is not None:
                    reply_send_stage.finish(
                        status="failed",
                        error_code=type(exc).__name__,
                    )
                    schedule_stage_event_upload(self.api, binding)
                raise
            evidence = self._send_evidence(sidecar_result, target=target.remark_code or target.display_name)
            run_id = str(sidecar_result.get("sidecar_run_id") or sidecar_result.get("run_id") or "") or None
            action_outcome = classify_action_result("send", sidecar_result)
            if reply_send_stage is not None:
                reply_send_stage.finish(
                    status=(
                        "succeeded"
                        if action_outcome["result"] == "sent"
                        else "failed"
                    ),
                    error_code=(
                        None
                        if action_outcome["result"] == "sent"
                        else str(
                            action_outcome.get("error_code")
                            or "RPA_SEND_REPLY_FAILED"
                        )
                    ),
                )
                schedule_stage_event_upload(self.api, binding)
            if action_outcome["result"] == "sent":
                sent_at = self._utc_now_iso()
                receipt_recorded = self._record_confirmed_ai_reply_receipt(
                    target=target,
                    reply_action_id=claim.reply_action_id,
                    reply_text_hash=actual_hash,
                    sidecar_result=sidecar_result,
                    confirmed_at=sent_at,
                )
                if not receipt_recorded:
                    append_log(
                        "WARN",
                        "ai_reply_stable_identity_not_recorded",
                        "AI 回复已确认发送，但新气泡稳定身份未能建立；已保留发送前凭证，下一轮仅允许标记为待对账 AI 消息。",
                        task_id=task.id,
                        error_code="AI_REPLY_STABLE_IDENTITY_UNCONFIRMED",
                        metadata={
                            "conversation_id": target.conversation_id,
                            "reply_action_id": claim.reply_action_id,
                        },
                    )
                else:
                    self._clear_possible_ai_send(
                        conversation_id=target.conversation_id,
                        reply_action_id=claim.reply_action_id,
                    )
                ack_confirmed = self._queue_and_submit_reply_send_ack(
                    binding, claim, send_result="sent", action_phase="confirmed", reply_text_hash=actual_hash,
                    sidecar_run_id=run_id, evidence=evidence, remark="同一 C2 UI 锁内发送 Guard 批准的 Brain 原文。",
                    sent_at=sent_at,
                )
                return {
                    "ok": True,
                    "batch": status,
                    "sent": True,
                    "reply_action_id": claim.reply_action_id,
                    "ack_confirmed": ack_confirmed,
                }
            error_code = str(
                action_outcome.get("error_code") or "RPA_SEND_REPLY_FAILED"
            )
            send_result = str(action_outcome["result"])
            action_phase = str(action_outcome["action_phase"])
            if action_phase == "not_attempted":
                self._clear_possible_ai_send(
                    conversation_id=target.conversation_id,
                    reply_action_id=claim.reply_action_id,
                )
            else:
                # The physical trigger may have run.  Preserve the action as
                # unreconciled; recovery must not re-send or reattach it by
                # body text, viewport position, or an old screenshot.
                self._mark_possible_ai_send_alignment(
                    conversation_id=target.conversation_id,
                    reply_action_id=claim.reply_action_id,
                    reconciliation_state="ai_unreconciled",
                    physical_send_confirmed=False,
                )
            self._queue_and_submit_reply_send_ack(
                binding, claim, send_result=send_result, action_phase=action_phase, reply_text_hash=actual_hash,
                sidecar_run_id=run_id, evidence=evidence, error_code=error_code,
                remark="发送结果未知，禁止自动补发。" if send_result == "unknown" else "发送失败。",
            )
            return {"ok": False, "error_code": error_code, "batch": status, "sent": False}

    def _finish_new_visible_voices_in_current_chat(
        self,
        *,
        binding: Binding,
        target: WechatReadTarget,
        target_label: str,
        sidecar_payload: dict[str, Any],
        lease: UiLockLease,
        action_cancel_requested: Callable[[], bool],
        enforce_read_targets: bool,
        read_run_id: str,
        excluded_voice_anchor_keys: set[str],
        flow_outcomes: FlowOutcomeAccumulator | None = None,
    ) -> dict[str, Any]:
        """The only production voice orchestrator: prepare, persist, execute."""

        if flow_outcomes is None:
            return {
                "ok": False,
                "error_code": "C2_VOICE_FLOW_OUTCOMES_MISSING",
                "payload": sidecar_payload,
            }
        current_payload = dict(sidecar_payload)
        current_payload["ui_frame_invalidated"] = bool(
            current_payload.get("ui_frame_invalidated")
        )
        item_outcomes: list[dict[str, Any]] = []
        failure_code = ""
        terminal_gate: dict[str, Any] | None = None
        cancelled_prepares = 0
        remaining_actions = 32
        attempted_fingerprints: set[str] = set()

        def result() -> dict[str, Any]:
            payload = {
                "ok": True,
                "payload": current_payload,
                "failure_code": failure_code,
                "item_outcomes": list(item_outcomes),
                "failed_source_keys": sorted({
                    str(item.get("source_message_key") or "")
                    for item in item_outcomes
                    if item.get("result") == "failed"
                }),
            }
            if terminal_gate is not None:
                payload["terminal_gate"] = dict(terminal_gate)
            return payload

        def quarantine_voice_identity(
            *,
            action_id: str,
            action_error_code: str,
        ) -> None:
            """Project an unbound physical result as a conversation gate.

            The action id is durable audit evidence for the one physical
            click. It is deliberately not promoted to a message identity,
            and the reserved worker sequence remains burned and uncommitted.
            """

            nonlocal failure_code, terminal_gate
            failure_code = str(
                action_error_code or "C2_VOICE_RESULT_AMBIGUOUS"
            ).strip()
            terminal_gate = {
                "error_code": (
                    "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
                ),
                "identity_errors": [
                    {
                        "observation_id": "",
                        "row_kind": "voice_bubble",
                        "error_code": (
                            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
                        ),
                        "signature": action_id,
                        "reason": (
                            "action_triggered_without_confirmed_post_alignment"
                        ),
                        "screen_order": 0,
                        "order_source": "",
                    }
                ],
                "authoritative_frame_source": "final_read",
                "ui_frame_invalidated": True,
                "action_error_code": failure_code,
            }

        while remaining_actions > 0:
            if action_cancel_requested():
                return {"ok": False, "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS", "payload": current_payload}
            if enforce_read_targets and not self._backend_still_allows_read_target_for_voice(
                binding,
                target,
                read_run_id=read_run_id,
            ):
                return {"ok": False, "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS", "payload": current_payload}
            current_executable_voices = (
                _executable_untranscribed_voice_observations(
                    target,
                    current_payload,
                    excluded_anchor_keys=excluded_voice_anchor_keys,
                )
            )
            if not current_executable_voices:
                return result()

            lease.update_step("voice_prepare_current_chat")
            self.current_step = "voice_prepare_current_chat"
            prepared = self.bridge.prepare_voice_action(
                display_name=target_label,
                rpa_session_key="",
                remark_code=target.remark_code or "",
                target_mode="current",
                max_duration_seconds=20,
                excluded_voice_anchor_keys=sorted(excluded_voice_anchor_keys),
                cancel_check=action_cancel_requested,
            )
            contract_error = sidecar_contract_error(prepared, require_observations=False)
            if contract_error:
                return {"ok": False, "error_code": contract_error, "payload": current_payload}
            prepare_state = str(prepared.get("state") or "")
            if prepare_state == "voice_action_prepare_empty":
                if not isinstance(prepared.get("observations"), list):
                    return {
                        "ok": False,
                        "error_code": "C2_AUTHORITATIVE_FRAME_SOURCE_INVALID",
                        "payload": current_payload,
                    }
                current_observations = list(
                    current_payload.get("observations") or []
                )
                prepared_observations = list(
                    prepared.get("observations") or []
                )
                current_candidate_ids = {
                    str(item.get("observation_id") or "").strip()
                    for item in current_executable_voices
                    if str(item.get("observation_id") or "").strip()
                }
                prepared_voice_ids = {
                    str(item.get("observation_id") or "").strip()
                    for item in _untranscribed_voice_observations(prepared)
                    if str(item.get("observation_id") or "").strip()
                }
                if current_candidate_ids & prepared_voice_ids:
                    return {
                        "ok": False,
                        "error_code": "C2_AUTHORITATIVE_FRAME_SOURCE_INVALID",
                        "payload": current_payload,
                    }
                aligned, alignment_evidence = (
                    align_post_action_observations(
                        current_observations,
                        prepared_observations,
                    )
                )
                if alignment_evidence.get("alignment_status") not in {
                    "unique",
                    "not_required",
                }:
                    return {
                        "ok": False,
                        "error_code": (
                            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
                        ),
                        "payload": current_payload,
                    }
                aligned = self._assign_sequence_new_suffix_identities(
                    target=target,
                    observations=aligned,
                    evidence=alignment_evidence,
                    read_run_id=flow_outcomes.origin_read_run_id,
                )
                completed_voice_summary = current_payload.get(
                    "voice_transcription"
                )
                current_payload = {
                    **prepared,
                    "ok": True,
                    "state": "messages_ocr",
                    "observations": aligned,
                    "sequence_alignment_evidence": alignment_evidence,
                    "authoritative_frame_source": "final_read",
                    "ui_frame_invalidated": bool(
                        current_payload.get("ui_frame_invalidated")
                    ),
                }
                if completed_voice_summary:
                    current_payload["voice_transcription"] = (
                        completed_voice_summary
                    )
                continue
            prepare_fields = {
                key: str(prepared.get(key) or "").strip()
                for key in (
                    "pre_frame_id",
                    "selected_pre_observation_id",
                    "selected_action_token",
                    "selected_target_fingerprint",
                )
            }
            if (
                not prepared.get("ok")
                or prepare_state != "voice_action_prepared"
                or prepared.get("ui_action_performed") is not False
                or any(not value for value in prepare_fields.values())
            ):
                return {
                    "ok": False,
                    "error_code": str(prepared.get("error_code") or "C2_VOICE_PREPARE_CONTRACT_INVALID"),
                    "payload": current_payload,
                }
            fingerprint = prepare_fields["selected_target_fingerprint"]
            if fingerprint in attempted_fingerprints:
                failure_code = failure_code or "VOICE_TRANSCRIBE_NO_PROGRESS"
                return result()
            current_observations = list(
                current_payload.get("observations") or []
            )
            pre_observations = list(prepared.get("observations") or [])
            current_has_committed_identity = any(
                isinstance(item, dict)
                and str(item.get("_worker_stable_id") or "").strip()
                for item in current_observations
            )
            current_ids = [
                str(item.get("observation_id") or "").strip()
                for item in current_observations
                if isinstance(item, dict)
            ]
            prepared_ids = [
                str(item.get("observation_id") or "").strip()
                for item in pre_observations
                if isinstance(item, dict)
            ]
            exact_read_only_frame = bool(
                current_ids
                and current_ids == prepared_ids
                and len(set(current_ids)) == len(current_ids)
                and all(current_ids)
            )
            if exact_read_only_frame:
                committed_by_id = {
                    str(item.get("observation_id") or "").strip(): str(
                        item.get("_worker_stable_id") or ""
                    ).strip()
                    for item in current_observations
                    if isinstance(item, dict)
                    and str(item.get("observation_id") or "").strip()
                    and str(item.get("_worker_stable_id") or "").strip()
                }
                voice_action_summary_by_id = {
                    str(item.get("observation_id") or "").strip(): dict(
                        item.get("_worker_voice_action_summary") or {}
                    )
                    for item in current_observations
                    if isinstance(item, dict)
                    and str(item.get("observation_id") or "").strip()
                    and isinstance(
                        item.get("_worker_voice_action_summary"), dict
                    )
                    and item.get("_worker_voice_action_summary")
                }
                inherited_pre_observations: list[Any] = []
                for raw_observation in pre_observations:
                    if not isinstance(raw_observation, dict):
                        inherited_pre_observations.append(raw_observation)
                        continue
                    observation = dict(raw_observation)
                    observation_id = str(
                        observation.get("observation_id") or ""
                    ).strip()
                    committed_id = committed_by_id.get(observation_id)
                    if committed_id:
                        observation["_worker_stable_id"] = committed_id
                    voice_action_summary = (
                        voice_action_summary_by_id.get(observation_id)
                    )
                    if voice_action_summary:
                        observation["_worker_voice_action_summary"] = dict(
                            voice_action_summary
                        )
                    inherited_pre_observations.append(observation)
                pre_observations = inherited_pre_observations
                prepared = {
                    **prepared,
                    "observations": pre_observations,
                }
            elif current_has_committed_identity:
                pre_observations, prepare_alignment_evidence = (
                    align_post_action_observations(
                        current_observations,
                        pre_observations,
                    )
                )
                if prepare_alignment_evidence.get(
                    "alignment_status"
                ) not in {"unique", "not_required"}:
                    return {
                        "ok": False,
                        "error_code": (
                            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
                        ),
                        "payload": current_payload,
                    }
                pre_observations = (
                    self._assign_sequence_new_suffix_identities(
                        target=target,
                        observations=pre_observations,
                        evidence=prepare_alignment_evidence,
                        read_run_id=(
                            flow_outcomes.origin_read_run_id
                        ),
                    )
                )
                prepared = {
                    **prepared,
                    "observations": pre_observations,
                    "sequence_alignment_evidence": (
                        prepare_alignment_evidence
                    ),
                }
            selected_id = prepare_fields["selected_pre_observation_id"]
            selected_observations = [
                item
                for item in pre_observations
                if isinstance(item, dict)
                and str(item.get("observation_id") or "").strip()
                == selected_id
            ]
            selected_plan = (
                prepared.get("selected_voice_observation")
                if isinstance(
                    prepared.get("selected_voice_observation"), dict
                )
                else {}
            )
            executable_prepared_ids = {
                str(item.get("observation_id") or "").strip()
                for item in _executable_untranscribed_voice_observations(
                    target,
                    {**prepared, "observations": pre_observations},
                    excluded_anchor_keys=excluded_voice_anchor_keys,
                )
                if str(item.get("observation_id") or "").strip()
            }
            selected_observation = (
                selected_observations[0]
                if len(selected_observations) == 1
                else {}
            )
            if (
                len(selected_observations) != 1
                or selected_id not in executable_prepared_ids
                or not observation_role_is_trusted(selected_observation)
                or str(
                    selected_observation.get("message_type") or ""
                ).strip().lower()
                != "voice"
                or str(
                    selected_observation.get("voice_state") or ""
                ).strip().lower()
                != "untranscribed"
                or str(
                    selected_plan.get("observation_id") or ""
                ).strip()
                != selected_id
                or str(
                    selected_plan.get("sender_role") or ""
                ).strip().lower()
                != str(
                    selected_observation.get("sender_role") or ""
                ).strip().lower()
            ):
                return {"ok": False, "error_code": "C2_VOICE_PREPARE_CONTRACT_INVALID", "payload": current_payload}
            physical_anchor_keys = sorted({
                str(value).strip()
                for value in (prepared.get("selected_physical_anchor_keys") or [])
                if str(value).strip()
            })
            if not physical_anchor_keys:
                return {"ok": False, "error_code": "C2_VOICE_PREPARE_CONTRACT_INVALID", "payload": current_payload}

            action_id = f"voice:{target.conversation_id}:{uuid.uuid4()}"
            reserved_id = self._reserve_worker_sequence(
                target,
                reservation_key=f"selected-action:{action_id}",
            )
            committed_ids = {
                str(item.get("observation_id") or ""): str(item.get("_worker_stable_id") or "")
                for item in pre_observations
                if isinstance(item, dict)
                and str(item.get("observation_id") or "")
                and str(item.get("_worker_stable_id") or "")
            }
            pre_sequence = build_pre_action_identity_sequence(
                pre_observations,
                committed_ids=committed_ids,
                selected_observation_id=selected_id,
                canonical_action_id=action_id,
                reserved_worker_stable_id=reserved_id,
            )
            prepare_evidence = {
                **prepare_fields,
                "selected_physical_anchor_keys": physical_anchor_keys,
                "candidate_group_count": int(
                    prepared.get("candidate_group_count") or 0
                ),
            }
            journal = self._start_irreversible_action_journal(
                action_kind="voice",
                target=target,
                items=[{"source_message_key": action_id, "physical_anchor_keys": physical_anchor_keys}],
                flow_outcomes=flow_outcomes,
                transaction_id=action_id,
                pre_action_identity_sequence=pre_sequence,
                pre_frame_id=prepare_fields["pre_frame_id"],
                reserved_worker_stable_id=reserved_id,
                prepare_evidence=prepare_evidence,
            )

            def terminate_created_voice_action(
                error_code: str,
                *,
                definitely_before_trigger: bool = False,
            ) -> None:
                """Ensure every created voice action has a finite phase.

                The Sidecar journal is authoritative about whether the
                irreversible trigger may have run.  A still-not-attempted
                journal can be cancelled.  Any other unresolved phase is
                quarantined without attaching the reserved sequence identity
                to message content or permitting a repeat click.
                """

                payload = read_action_journal(journal)
                items = (
                    payload.get("items")
                    if isinstance(payload.get("items"), dict)
                    else {}
                )
                item = (
                    dict(items.get(action_id) or {})
                    if isinstance(items.get(action_id), dict)
                    else {}
                )
                phase = str(
                    item.get("action_phase")
                    or payload.get("action_phase")
                    or "not_attempted"
                ).strip()
                if definitely_before_trigger and phase == "not_attempted":
                    update_action_journal_item(
                        journal,
                        source_message_key=action_id,
                        action_phase="cancelled_before_trigger",
                        business_state="not_attempted",
                        business_result_confirmed=False,
                        error_code=error_code,
                        terminal_payload={
                            "state": "cancelled_before_trigger",
                            "error_code": error_code,
                        },
                    )
                    return
                if phase in {"cancelled_before_trigger", "quarantined"}:
                    return
                update_action_journal_item(
                    journal,
                    source_message_key=action_id,
                    action_phase="quarantined",
                    error_code=error_code,
                    terminal_payload={
                        "state": "quarantined",
                        "error_code": error_code,
                        "previous_action_phase": phase,
                    },
                )

            if enforce_read_targets and not self._backend_still_allows_read_target_for_voice(
                binding,
                target,
                read_run_id=read_run_id,
            ):
                terminate_created_voice_action(
                    "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                    definitely_before_trigger=True,
                )
                return {"ok": False, "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS", "payload": current_payload}

            lease.update_step("voice_transcribe_current_chat")
            self.current_step = "voice_transcribe_current_chat"
            try:
                executed = self.bridge.execute_voice_action(
                    display_name=target_label,
                    rpa_session_key="",
                    canonical_voice_action_id=action_id,
                    reserved_worker_stable_id=reserved_id,
                    pre_frame_id=prepare_fields["pre_frame_id"],
                    selected_pre_observation_id=selected_id,
                    selected_action_token=prepare_fields["selected_action_token"],
                    selected_target_fingerprint=fingerprint,
                    remark_code=target.remark_code or "",
                    target_mode="current",
                    max_duration_seconds=CONFIG.c2_voice_transcribe_max_duration_seconds,
                    action_journal=journal,
                    cancel_check=action_cancel_requested,
                )
            except Exception:
                terminate_created_voice_action(
                    "C2_VOICE_EXECUTE_INTERRUPTED",
                )
                raise
            execute_contract_error = sidecar_contract_error(executed, require_observations=False)
            if execute_contract_error:
                terminate_created_voice_action(
                    execute_contract_error,
                    definitely_before_trigger=bool(
                        executed.get("ui_action_performed") is False
                        and str(executed.get("action_phase") or "").strip()
                        in {"not_attempted", "cancelled_before_trigger"}
                    ),
                )
                return {"ok": False, "error_code": execute_contract_error, "payload": current_payload}
            execute_state = str(executed.get("state") or "")
            action_phase = str(executed.get("action_phase") or "not_attempted")
            if execute_state == "voice_action_cancelled_before_trigger":
                if (
                    action_phase != "cancelled_before_trigger"
                    or executed.get("ui_action_performed") is not False
                    or str(executed.get("voice_action_stage") or "")
                    != "execute"
                    or any(
                        str(executed.get(key) or "").strip() != value
                        for key, value in {
                            "canonical_voice_action_id": action_id,
                            "reserved_worker_stable_id": reserved_id,
                            **prepare_fields,
                        }.items()
                    )
                ):
                    terminate_created_voice_action(
                        "C2_VOICE_CANCEL_CONTRACT_INVALID"
                    )
                    return {"ok": False, "error_code": "C2_VOICE_CANCEL_CONTRACT_INVALID", "payload": current_payload}
                terminate_created_voice_action(
                    str(executed.get("error_code") or "C2_VOICE_ACTION_CANCELLED"),
                    definitely_before_trigger=True,
                )
                if (
                    str(executed.get("error_code") or "").strip()
                    == "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS"
                    or action_cancel_requested()
                    or (
                        enforce_read_targets
                        and not self._backend_still_allows_read_target_for_voice(
                            binding,
                            target,
                            read_run_id=read_run_id,
                        )
                    )
                ):
                    return {
                        "ok": False,
                        "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                        "payload": current_payload,
                    }
                cancelled_prepares += 1
                if cancelled_prepares >= 4:
                    return {"ok": False, "error_code": "C2_VOICE_PREPARE_TARGET_UNSTABLE", "payload": current_payload}
                continue

            binding_status = str(
                executed.get("transcript_binding_status") or ""
            ).strip().lower()
            binding_method = str(
                executed.get("transcript_binding_method") or ""
            ).strip().lower()
            confirmed_mapping = (
                executed.get("confirmed_action_mapping")
                if isinstance(
                    executed.get("confirmed_action_mapping"), dict
                )
                else {}
            )
            tracking_edges = executed.get("tracking_edges")
            tracking_frame_ids = executed.get("tracking_frame_ids")
            try:
                binding_candidate_count = int(
                    executed.get("binding_candidate_count")
                )
            except (TypeError, ValueError):
                binding_candidate_count = -1
            execute_identity_contract_valid = bool(
                str(executed.get("voice_action_stage") or "").strip()
                == "execute"
                and str(executed.get("pre_frame_id") or "").strip()
                == prepare_fields["pre_frame_id"]
                and str(executed.get("post_frame_id") or "").strip()
                and str(
                    executed.get("post_frame_id") or ""
                ).strip()
                != prepare_fields["pre_frame_id"]
                and str(
                    executed.get("selected_pre_observation_id") or ""
                ).strip()
                == selected_id
                and str(
                    executed.get("selected_action_token") or ""
                ).strip()
                == prepare_fields["selected_action_token"]
                and str(
                    executed.get("selected_target_fingerprint") or ""
                ).strip()
                == fingerprint
                and
                str(
                    executed.get("canonical_voice_action_id") or ""
                ).strip()
                == action_id
                and str(
                    executed.get("reserved_worker_stable_id") or ""
                ).strip()
                == reserved_id
                and binding_status in {"confirmed", "ambiguous", "failed"}
                and binding_method
                and binding_candidate_count >= 0
                and isinstance(tracking_frame_ids, list)
                and isinstance(tracking_edges, list)
                and str(
                    confirmed_mapping.get("canonical_action_id") or ""
                ).strip()
                == action_id
                and str(
                    confirmed_mapping.get("reserved_worker_stable_id")
                    or ""
                ).strip()
                == reserved_id
                and isinstance(executed.get("ui_action_performed"), bool)
            )
            if execute_state == "voice_transcribe_completed":
                execute_identity_contract_valid = bool(
                    execute_identity_contract_valid
                    and binding_status == "confirmed"
                    and binding_candidate_count == 1
                    and confirmed_mapping.get("binding_confirmed") is True
                )
            elif execute_state == "voice_transcribe_ambiguous":
                execute_identity_contract_valid = bool(
                    execute_identity_contract_valid
                    and binding_status == "ambiguous"
                    and confirmed_mapping.get("binding_confirmed") is False
                )
            else:
                execute_identity_contract_valid = bool(
                    execute_identity_contract_valid
                    and binding_status == "failed"
                    and binding_candidate_count == 1
                    and confirmed_mapping.get("binding_confirmed") is True
                    and str(
                        confirmed_mapping.get("post_observation_id") or ""
                    ).strip()
                    and executed.get("ui_action_performed") is True
                )
            if not execute_identity_contract_valid:
                terminate_created_voice_action(
                    "C2_VOICE_IDENTITY_CONTRACT_INVALID",
                    definitely_before_trigger=bool(
                        executed.get("ui_action_performed") is False
                        and action_phase
                        in {"not_attempted", "cancelled_before_trigger"}
                    ),
                )
                return {
                    "ok": False,
                    "error_code": "C2_VOICE_IDENTITY_CONTRACT_INVALID",
                    "payload": current_payload,
                }

            remaining_actions -= 1
            attempted_fingerprints.add(fingerprint)
            durable_source_key = worker_source_message_key(
                target,
                identity_kind="worker_sequence",
                identity=reserved_id,
            )
            sender_role = str((prepared.get("selected_voice_observation") or {}).get("sender_role") or "")
            if execute_state == "voice_transcribe_ambiguous":
                # Sidecar has exhausted its bounded evidence-only reads but
                # cannot bind the result to one physical voice. This is an
                # independent terminal gate, not a failed message slot: keep
                # the quarantined Journal, burn the reservation, and never
                # ask normal slot settlement for a source_message_key.
                ambiguous_error_code = str(
                    executed.get("error_code")
                    or "C2_VOICE_RESULT_AMBIGUOUS"
                )
                terminate_created_voice_action(ambiguous_error_code)
                excluded_voice_anchor_keys.update(physical_anchor_keys)
                current_payload = {
                    **executed,
                    "ok": True,
                    "state": "messages_ocr",
                    "observations": list(
                        executed.get("observations") or []
                    ),
                    "voice_transcription": executed,
                    "authoritative_frame_source": "final_read",
                    "ui_frame_invalidated": True,
                }
                quarantine_voice_identity(
                    action_id=action_id,
                    action_error_code=ambiguous_error_code,
                )
                return result()

            if execute_state != "voice_transcribe_completed":
                post_observations = list(executed.get("observations") or [])
                try:
                    mapping = confirmed_voice_action_mapping(
                        voice_payload=executed,
                        pre_observations=pre_observations,
                        post_observations=post_observations,
                        canonical_action_id=action_id,
                        reserved_worker_stable_id=reserved_id,
                        expected_pre_frame_id=(
                            prepare_fields["pre_frame_id"]
                        ),
                        pre_observation_id=selected_id,
                        selected_anchor_keys=set(physical_anchor_keys),
                    )
                except ValueError:
                    terminate_created_voice_action(
                        "C2_VOICE_IDENTITY_CONTRACT_INVALID"
                    )
                    return {
                        "ok": False,
                        "error_code": "C2_VOICE_IDENTITY_CONTRACT_INVALID",
                        "payload": current_payload,
                    }
                aligned, alignment_evidence = align_post_action_observations(
                    pre_observations,
                    post_observations,
                    confirmed_action_mapping=mapping,
                )
                record_action_sequence_alignment(journal, alignment_evidence)
                if alignment_evidence.get("alignment_status") != "unique":
                    terminate_created_voice_action(
                        "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
                    )
                    current_payload = {
                        **executed,
                        "ok": True,
                        "state": "messages_ocr",
                        "observations": post_observations,
                        "sequence_alignment_evidence": alignment_evidence,
                        "voice_transcription": executed,
                        "authoritative_frame_source": "final_read",
                        "ui_frame_invalidated": True,
                    }
                    quarantine_voice_identity(
                        action_id=action_id,
                        action_error_code=(
                            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
                        ),
                    )
                    return result()
                aligned = self._assign_sequence_new_suffix_identities(
                    target=target,
                    observations=aligned,
                    evidence=alignment_evidence,
                    read_run_id=flow_outcomes.origin_read_run_id,
                )
                confirmed_post_observation_id = str(
                    mapping.get("post_observation_id") or ""
                ).strip()
                confirmed_post_anchor_keys: set[str] = set()
                for observation in aligned:
                    if (
                        isinstance(observation, dict)
                        and str(
                            observation.get("observation_id") or ""
                        ).strip()
                        == confirmed_post_observation_id
                    ):
                        observation["_worker_voice_action_summary"] = dict(
                            executed
                        )
                        # Exclusions are deliberately frame-local.  The
                        # failed bubble may have moved after a new message
                        # arrived, so carrying the pre-click seat/anchor into
                        # the next prepare could suppress the new bubble that
                        # now occupies that seat.  Continuous tracking has
                        # already proved which post-action observation owns
                        # this failure; exclude only that latest observation's
                        # aliases during the remainder of this UI flow.
                        confirmed_post_anchor_keys.update(
                            voice_action_journal_anchor_keys(observation)
                        )
                if not confirmed_post_anchor_keys:
                    terminate_created_voice_action(
                        "C2_VOICE_IDENTITY_CONTRACT_INVALID"
                    )
                    return {
                        "ok": False,
                        "error_code": (
                            "C2_VOICE_IDENTITY_CONTRACT_INVALID"
                        ),
                        "payload": current_payload,
                    }
                commit_action_journal_item_identity(
                    journal,
                    journal_item_id=action_id,
                    source_message_key=durable_source_key,
                )
                outcome = classify_action_result(
                    "voice",
                    executed,
                    source_message_key=durable_source_key,
                )
                evidence = dict(outcome.get("evidence") or {})
                evidence.update(
                    {"action_kind": "voice", "sender_role": sender_role}
                )
                outcome["evidence"] = evidence
                outcome["terminal_payload"] = _voice_terminal_payload(
                    executed,
                    anchor_keys=physical_anchor_keys,
                    result="failed",
                    error_code=str(
                        executed.get("error_code")
                        or "C2_VOICE_TRANSCRIBE_FAILED"
                    ),
                )
                update_action_journal_item(
                    journal,
                    source_message_key=durable_source_key,
                    action_phase=action_phase,
                    business_state="failed",
                    business_result_confirmed=False,
                    error_code=str(
                        executed.get("error_code")
                        or "C2_VOICE_TRANSCRIBE_FAILED"
                    ),
                    terminal_payload=outcome["terminal_payload"],
                )
                flow_outcomes.record(outcome)
                item_outcomes = merge_item_outcomes(
                    item_outcomes,
                    [outcome],
                )
                failure_code = str(
                    executed.get("error_code")
                    or "C2_VOICE_TRANSCRIBE_FAILED"
                )
                excluded_voice_anchor_keys.update(
                    confirmed_post_anchor_keys
                )
                current_payload = {
                    **executed,
                    "ok": True,
                    "state": "messages_ocr",
                    "observations": aligned,
                    "sequence_alignment_evidence": alignment_evidence,
                    "voice_transcription": executed,
                    "authoritative_frame_source": "final_read",
                    "ui_frame_invalidated": True,
                }
                continue

            post_observations = list(executed.get("observations") or [])
            try:
                mapping = confirmed_voice_action_mapping(
                    voice_payload=executed,
                    pre_observations=pre_observations,
                    post_observations=post_observations,
                    canonical_action_id=action_id,
                    reserved_worker_stable_id=reserved_id,
                    expected_pre_frame_id=(
                        prepare_fields["pre_frame_id"]
                    ),
                    pre_observation_id=selected_id,
                    selected_anchor_keys=set(physical_anchor_keys),
                )
            except ValueError:
                terminate_created_voice_action(
                    "C2_VOICE_IDENTITY_CONTRACT_INVALID"
                )
                return {"ok": False, "error_code": "C2_VOICE_IDENTITY_CONTRACT_INVALID", "payload": current_payload}
            aligned, alignment_evidence = align_post_action_observations(
                pre_observations,
                post_observations,
                confirmed_action_mapping=mapping,
            )
            record_action_sequence_alignment(journal, alignment_evidence)
            if alignment_evidence.get("alignment_status") != "unique":
                terminate_created_voice_action(
                    "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
                )
                current_payload = {
                    **executed,
                    "ok": True,
                    "state": "messages_ocr",
                    "observations": post_observations,
                    "sequence_alignment_evidence": alignment_evidence,
                    "voice_transcription": executed,
                    "authoritative_frame_source": "final_read",
                    "ui_frame_invalidated": True,
                }
                quarantine_voice_identity(
                    action_id=action_id,
                    action_error_code=(
                        "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
                    ),
                )
                return result()
            aligned = self._assign_sequence_new_suffix_identities(
                target=target,
                observations=aligned,
                evidence=alignment_evidence,
                read_run_id=flow_outcomes.origin_read_run_id,
            )
            confirmed_post_observation_id = str(
                mapping.get("post_observation_id") or ""
            ).strip()
            for observation in aligned:
                if (
                    isinstance(observation, dict)
                    and str(observation.get("observation_id") or "").strip()
                    == confirmed_post_observation_id
                ):
                    observation["_worker_voice_action_summary"] = dict(
                        executed
                    )
            commit_action_journal_item_identity(
                journal,
                journal_item_id=action_id,
                source_message_key=durable_source_key,
            )
            outcome = classify_action_result("voice", executed, source_message_key=durable_source_key)
            evidence = dict(outcome.get("evidence") or {})
            evidence.update({"action_kind": "voice", "sender_role": sender_role})
            outcome["evidence"] = evidence
            outcome["terminal_payload"] = _voice_terminal_payload(
                executed,
                anchor_keys=physical_anchor_keys,
                result="completed",
                error_code=None,
            )
            update_action_journal_item(
                journal,
                source_message_key=durable_source_key,
                action_phase=action_phase,
                business_state="completed",
                business_result_confirmed=True,
                error_code="",
                terminal_payload=outcome["terminal_payload"],
            )
            flow_outcomes.record(outcome)
            item_outcomes = merge_item_outcomes(item_outcomes, [outcome])
            current_payload = {
                **executed,
                "ok": True,
                "state": "messages_ocr",
                "observations": aligned,
                "sequence_alignment_evidence": alignment_evidence,
                "voice_transcription": executed,
                "authoritative_frame_source": "final_read",
                "ui_frame_invalidated": True,
            }

        return {
            "ok": False,
            "error_code": "C2_VOICE_ACTION_BUDGET_EXHAUSTED",
            "payload": current_payload,
        }

    def _converge_current_screen_after_images(
        self,
        *,
        binding: Binding,
        target: WechatReadTarget,
        target_label: str,
        sidecar_payload: dict[str, Any],
        lease: UiLockLease,
        action_cancel_requested: Callable[[], bool],
        enforce_read_targets: bool,
        flow_outcomes: FlowOutcomeAccumulator,
    ) -> dict[str, Any]:
        """Finish media that arrived while Vision held the current chat.

        This loop never scrolls or switches conversations. Each iteration
        performs one current-screen read, reconciles Worker-owned identities,
        finishes visible voices, and processes only image source keys that are
        still NEW against the local ledger.
        """

        current_payload = dict(sidecar_payload)
        current_payload["ui_frame_invalidated"] = bool(
            current_payload.get("ui_frame_invalidated")
        )
        aggregate_image_stats = new_image_phase_result()
        failed_voice_source_keys: set[str] = set()
        failed_voice_roles: dict[str, str] = {}
        seen_pending_image_sets: set[tuple[str, ...]] = set()

        def add_image_stats(values: dict[str, Any]) -> None:
            merge_image_phase_results(aggregate_image_stats, values)

        while True:
            if action_cancel_requested():
                return {
                    "ok": False,
                    "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                    "payload": current_payload,
                    "image_stats": aggregate_image_stats,
                }
            lease.update_step("image_post_vision_final_read")
            self.current_step = "image_post_vision_final_read"
            refreshed = self.bridge.get_messages(
                display_name=target_label,
                rpa_session_key="",
                remark_code=target.remark_code or "",
                target_mode="current",
                max_duration_seconds=20,
                cancel_check=action_cancel_requested,
            )
            if not refreshed.get("ok"):
                return {
                    "ok": False,
                    "error_code": str(
                        refreshed.get("error_code")
                        or refreshed.get("state")
                        or "TARGET_NOT_CONFIRMED_FOR_MESSAGES"
                    ),
                    "payload": current_payload,
                    "image_stats": aggregate_image_stats,
                }
            contract_error = sidecar_contract_error(refreshed)
            if contract_error:
                return {
                    "ok": False,
                    "error_code": contract_error,
                    "payload": current_payload,
                    "image_stats": aggregate_image_stats,
                }

            observations, sequence_alignment_evidence = (
                align_post_action_observations(
                    list(current_payload.get("observations") or []),
                    list(refreshed.get("observations") or []),
                )
            )
            refreshed["sequence_alignment_evidence"] = (
                sequence_alignment_evidence
            )
            identity_errors = (
                []
                if sequence_alignment_evidence.get("alignment_status")
                == "unique"
                else [
                    {
                        "error_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
                        "reason": sequence_alignment_evidence.get(
                            "alignment_status"
                        ),
                    }
                ]
            )
            if not identity_errors:
                for journal_path, journal_payload in list_action_journals(
                    conversation_id=target.conversation_id,
                    action_kinds=("image",),
                ):
                    if journal_payload.get("sequence_alignment_evidence") is None:
                        record_action_sequence_alignment(
                            journal_path,
                            sequence_alignment_evidence,
                        )
            if identity_errors:
                return {
                    "ok": False,
                    "error_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
                    "payload": current_payload,
                    "image_stats": aggregate_image_stats,
                    "sequence_alignment_evidence": (
                        sequence_alignment_evidence
                    ),
                }
            observations = self._assign_sequence_new_suffix_identities(
                target=target,
                observations=observations,
                evidence=sequence_alignment_evidence,
                read_run_id=flow_outcomes.origin_read_run_id,
            )
            refreshed["observations"] = observations
            refreshed = self._merge_waiting_image_facts(
                target=target,
                sidecar_payload=refreshed,
            )
            refreshed["initial_messages"] = (
                current_payload.get("initial_messages")
                or sidecar_payload.get("initial_messages")
                or sidecar_payload
            )
            refreshed["voice_transcription"] = (
                current_payload.get("voice_transcription")
                or sidecar_payload.get("voice_transcription")
            )
            refreshed["authoritative_frame_source"] = "final_read"
            refreshed["ui_frame_invalidated"] = bool(
                current_payload.get("ui_frame_invalidated")
            )
            current_payload = refreshed

            failed_ledger_groups: dict[str, set[str]] = {}
            for observation in (
                current_payload.get("observations") or []
            ):
                if (
                    not isinstance(observation, dict)
                    or observation.get("row_kind") != "voice_bubble"
                    or observation.get("voice_state")
                    != "untranscribed"
                ):
                    continue
                try:
                    source_key = voice_observation_source_key(
                        target,
                        observation,
                    )
                except ValueError:
                    continue
                ledger = load_c2_ledger_entry(
                    target.conversation_id,
                    source_key,
                )
                if (
                    not ledger
                    or ledger.get("terminal_state") != "failed"
                ):
                    continue
                result = (
                    ledger.get("result")
                    if isinstance(ledger.get("result"), dict)
                    else {}
                )
                error_code = str(
                    result.get("error_code")
                    or "VOICE_TRANSCRIBE_FAILED"
                )
                failed_ledger_groups.setdefault(
                    error_code,
                    set(),
                ).add(source_key)
            for error_code, source_keys in failed_ledger_groups.items():
                annotated_roles = (
                    self._annotate_failed_voice_observations(
                        target=target,
                        sidecar_payload=current_payload,
                        failed_source_keys=source_keys,
                        error_code=error_code,
                    )
                )
                failed_voice_source_keys.update(source_keys)
                failed_voice_roles.update(annotated_roles)

            if _executable_untranscribed_voice_observations(
                target,
                current_payload,
            ):
                voice_result = (
                    self._finish_new_visible_voices_in_current_chat(
                        binding=binding,
                        target=target,
                        target_label=target_label,
                        sidecar_payload=current_payload,
                        lease=lease,
                        action_cancel_requested=action_cancel_requested,
                        enforce_read_targets=enforce_read_targets,
                        read_run_id=flow_outcomes.origin_read_run_id,
                        excluded_voice_anchor_keys=set(),
                        flow_outcomes=flow_outcomes,
                    )
                )
                if not voice_result.get("ok"):
                    return {
                        **voice_result,
                        "image_stats": aggregate_image_stats,
                    }
                current_payload = dict(
                    voice_result.get("payload") or current_payload
                )
                terminal_gate = voice_result.get("terminal_gate")
                if isinstance(terminal_gate, dict):
                    return {
                        "ok": False,
                        "error_code": str(
                            terminal_gate.get("error_code")
                            or "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
                        ),
                        "terminal_gate": dict(terminal_gate),
                        "payload": current_payload,
                        "image_stats": aggregate_image_stats,
                        "failed_voice_source_keys": sorted(
                            failed_voice_source_keys
                        ),
                        "failed_voice_roles": failed_voice_roles,
                    }
                new_failed_keys = {
                    str(value)
                    for value in (
                        voice_result.get("failed_source_keys") or []
                    )
                    if str(value).strip()
                }
                if new_failed_keys:
                    failure_code = str(
                        voice_result.get("failure_code")
                        or "VOICE_TRANSCRIBE_FAILED"
                    )
                    self._mark_voice_sources_failed(
                        target=target,
                        origin_read_run_id=flow_outcomes.origin_read_run_id,
                        source_keys=sorted(new_failed_keys),
                        error_code=failure_code,
                    )
                    annotated_roles = (
                        self._annotate_failed_voice_observations(
                            target=target,
                            sidecar_payload=current_payload,
                            failed_source_keys=new_failed_keys,
                            error_code=failure_code,
                        )
                    )
                    failed_voice_source_keys.update(new_failed_keys)
                    failed_voice_roles.update(annotated_roles)
                    failed_voice_roles.update(
                        {
                            str(key): str(value)
                            for key, value in (
                                voice_result.get("failed_roles") or {}
                            ).items()
                            if str(value) in {"customer", "self"}
                        }
                    )

            incremental_plan = self._build_final_slot_incremental_plan(
                target=target,
                sidecar_payload=current_payload,
                read_run_id=flow_outcomes.origin_read_run_id,
            )
            if incremental_plan["identity_errors"]:
                return {
                    "ok": False,
                    "error_code": (
                        "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
                    ),
                    "payload": current_payload,
                    "image_stats": aggregate_image_stats,
                }
            pending_image_keys = tuple(
                sorted(incremental_plan["new_image_source_keys"])
            )
            # One image action per stable frame. The next candidate is chosen
            # only after the loop obtains and aligns a fresh authoritative
            # frame.
            allowed_image_keys = (
                {pending_image_keys[0]} if pending_image_keys else set()
            )
            image_observations = [
                item
                for item in (current_payload.get("observations") or [])
                if isinstance(item, dict)
                and item.get("row_kind") == "image_bubble"
            ]
            if not image_observations:
                return {
                    "ok": True,
                    "payload": current_payload,
                    "image_stats": aggregate_image_stats,
                    "failed_voice_source_keys": sorted(
                        failed_voice_source_keys
                    ),
                    "failed_voice_roles": failed_voice_roles,
                }

            if pending_image_keys in seen_pending_image_sets:
                append_log(
                    "ERROR",
                    "c2_post_vision_current_screen_no_progress",
                    "图片处理后当前屏仍有未形成终态的图片；禁止继续入库或进入 Brain。",
                    error_code="C2_IMAGE_TERMINALIZATION_INCOMPLETE",
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "pending_image_source_keys": list(
                            pending_image_keys
                        ),
                    },
                )
                return {
                    "ok": False,
                    "error_code": "C2_IMAGE_TERMINALIZATION_INCOMPLETE",
                    "payload": current_payload,
                    "image_stats": aggregate_image_stats,
                    "failed_voice_source_keys": sorted(
                        failed_voice_source_keys
                    ),
                    "failed_voice_roles": failed_voice_roles,
                }
            seen_pending_image_sets.add(pending_image_keys)

            lease.update_step("image_understanding_current_chat")
            self.current_step = "image_understanding_current_chat"
            current_payload, image_stats = (
                self._process_final_image_slots(
                    binding=binding,
                    target=target,
                    sidecar_payload=current_payload,
                    enforce_read_targets=enforce_read_targets,
                    cancel_check=action_cancel_requested,
                    allowed_new_source_keys=allowed_image_keys,
                    flow_outcomes=flow_outcomes,
                )
            )
            if image_stats.get("ui_frame_invalidated"):
                current_payload["ui_frame_invalidated"] = True
            add_image_stats(image_stats)
            if not pending_image_keys:
                return {
                    "ok": True,
                    "payload": current_payload,
                    "image_stats": aggregate_image_stats,
                    "failed_voice_source_keys": sorted(
                        failed_voice_source_keys
                    ),
                    "failed_voice_roles": failed_voice_roles,
                }
            if image_stats.get("authorization_revoked"):
                return {
                    "ok": False,
                    "error_code": (
                        "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS"
                    ),
                    "payload": current_payload,
                    "image_stats": aggregate_image_stats,
                }
            if pending_image_keys and not any(
                image_stats.get(key)
                for key in (
                    "completed",
                    "failed",
                    "removed_from_final_screen",
                )
            ):
                return {
                    "ok": False,
                    "error_code": "C2_IMAGE_TERMINALIZATION_INCOMPLETE",
                    "payload": current_payload,
                    "image_stats": aggregate_image_stats,
                }

    @staticmethod
    def _start_irreversible_action_journal(
        *,
        action_kind: str,
        target: WechatReadTarget,
        items: list[dict[str, Any]],
        flow_outcomes: FlowOutcomeAccumulator,
        transaction_id: str | None = None,
        pre_action_identity_sequence: list[dict[str, Any]] | None = None,
        pre_frame_id: str | None = None,
        reserved_worker_stable_id: str | None = None,
        prepare_evidence: dict[str, Any] | None = None,
    ) -> Path:
        action_id = str(transaction_id or "").strip() or (
            f"{action_kind}:{target.conversation_id}:{uuid.uuid4()}"
        )
        path = action_journal_path(action_kind, action_id)
        initialize_action_journal(
            path,
            action_kind=action_kind,
            transaction_id=action_id,
            conversation_id=target.conversation_id,
            items=items,
            origin_read_run_id=flow_outcomes.origin_read_run_id,
            canonical_action_id=(
                action_id if reserved_worker_stable_id else None
            ),
            reserved_worker_stable_id=reserved_worker_stable_id,
            pre_frame_id=pre_frame_id,
            pre_action_identity_sequence=pre_action_identity_sequence,
            prepare_evidence=prepare_evidence,
        )
        flow_outcomes.register_action_journal(path)
        return path

    def _recover_physical_action_journals(
        self,
        target: WechatReadTarget,
    ) -> list[dict[str, Any]]:
        journal_entries = list_action_journals(
            conversation_id=target.conversation_id,
        )
        unresolved: list[dict[str, Any]] = []
        recovered_entries: list[tuple[Path, dict[str, Any]]] = []
        for path, raw_payload in journal_entries:
            payload = dict(raw_payload)
            action_id = str(payload.get("canonical_action_id") or "").strip()
            reserved_id = str(
                payload.get("reserved_worker_stable_id") or ""
            ).strip()
            committed_id = str(
                payload.get("committed_worker_stable_id") or ""
            ).strip()
            items = (
                payload.get("items")
                if isinstance(payload.get("items"), dict)
                else {}
            )
            action_kind = str(payload.get("action_kind") or "").strip()
            action_item = (
                dict(items.get(action_id) or {})
                if action_id and isinstance(items.get(action_id), dict)
                else {}
            )
            if (
                action_kind == "voice"
                and action_id
                and str(action_item.get("action_phase") or "").strip()
                == "trigger_attempted"
                and not isinstance(
                    payload.get("sequence_alignment_evidence"), dict
                )
            ):
                # A crash/transport loss after the durable no-repeat barrier
                # cannot remain pending forever.  No body or reserved
                # sequence identity may be guessed; close the physical action
                # as quarantined and let the idempotent identity gate isolate
                # only this conversation.
                payload = update_action_journal_item(
                    path,
                    source_message_key=action_id,
                    action_phase="quarantined",
                    error_code="C2_VOICE_RESULT_AMBIGUOUS",
                    terminal_payload={
                        "state": "quarantined",
                        "error_code": "C2_VOICE_RESULT_AMBIGUOUS",
                        "reason": (
                            "recovered_trigger_without_post_alignment"
                        ),
                    },
                )
                items = (
                    payload.get("items")
                    if isinstance(payload.get("items"), dict)
                    else {}
                )
            formed_fact = any(
                isinstance(item, dict)
                and action_journal_item_has_formed_fact(item)
                for item in items.values()
            )
            if action_id and reserved_id and not committed_id and formed_fact:
                evidence = (
                    payload.get("sequence_alignment_evidence")
                    if isinstance(
                        payload.get("sequence_alignment_evidence"), dict
                    )
                    else {}
                )
                selected_pair_confirmed = any(
                    isinstance(pair, dict)
                    and pair.get("identity_state") == "selected_action"
                    and str(pair.get("worker_stable_id") or "").strip()
                    == reserved_id
                    for pair in (evidence.get("matched_pairs") or [])
                )
                if (
                    evidence.get("alignment_status") == "unique"
                    and selected_pair_confirmed
                    and action_id in items
                ):
                    durable_source_key = worker_source_message_key(
                        target,
                        identity_kind="worker_sequence",
                        identity=reserved_id,
                    )
                    payload = commit_action_journal_item_identity(
                        path,
                        journal_item_id=action_id,
                        source_message_key=durable_source_key,
                    )
                else:
                    unresolved.append(
                        {
                            "error_code": (
                                "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
                            ),
                            "reason": "action_triggered_without_confirmed_post_alignment",
                            "row_kind": "voice_bubble",
                            "signature": action_id,
                            "transaction_id": str(
                                payload.get("transaction_id") or ""
                            ),
                            "origin_read_run_id": str(
                                payload.get("origin_read_run_id") or ""
                            ),
                            "sequence_alignment_evidence": evidence,
                        }
                    )
                    continue
            recovered_entries.append((path, payload))
        recovered_outcomes = self._physical_action_journal_outcomes(
            recovered_entries,
        )
        if recovered_outcomes:
            self._persist_c2_flow_outcomes(target, recovered_outcomes)
            append_log(
                "WARN",
                "c2_physical_action_journal_recovered",
                "检测到上次进程退出前已经触发的语音或图片动作，已恢复账本并禁止重复操作。",
                metadata={
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "outcome_count": len(recovered_outcomes),
                },
            )
        for path, payload in journal_entries:
            if self._action_journal_can_be_removed(payload):
                remove_action_journal(path)
        return unresolved

    @staticmethod
    def _action_journal_can_be_removed(payload: dict[str, Any]) -> bool:
        """Delete a physical journal only after every formed fact is confirmed.

        ``not_attempted`` describes whether the irreversible UI trigger ran;
        it does not mean that no business fact exists.  A text/voice menu
        rejection, for example, is a completed failed observation even though
        image copy was deliberately not attempted.
        """

        conversation_id = str(payload.get("conversation_id") or "").strip()
        items = (
            payload.get("items")
            if isinstance(payload.get("items"), dict)
            else {}
        )
        formed_source_keys: set[str] = set()
        for source_key, item in items.items():
            source_key = str(source_key or "").strip()
            if not source_key or not isinstance(item, dict):
                continue
            if action_journal_item_has_formed_fact(item):
                formed_source_keys.add(source_key)
        if formed_source_keys:
            return all(
                str(
                    (
                        load_c2_ledger_entry(conversation_id, source_key)
                        or {}
                    ).get("ingest_state")
                    or ""
                )
                == "confirmed"
                for source_key in formed_source_keys
            )
        return TaskRunner._strict_not_attempted_journal_can_be_removed(
            payload
        )

    @staticmethod
    def _physical_action_journal_outcomes(
        journal_entries: list[tuple[Path, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        recovered_outcomes: list[dict[str, Any]] = []
        for _path, payload in journal_entries:
            action_kind = str(payload.get("action_kind") or "").strip()
            if action_kind not in {"voice", "image"}:
                continue
            if (
                str(payload.get("canonical_action_id") or "").strip()
                and str(
                    payload.get("reserved_worker_stable_id") or ""
                ).strip()
                and not str(
                    payload.get("committed_worker_stable_id") or ""
                ).strip()
            ):
                # The physical result remains durable in the Journal, but it
                # has no confirmed cross-frame identity yet. Persisting it
                # under the action-local key would create a second message.
                continue
            origin_read_run_id = str(
                payload.get("origin_read_run_id") or ""
            ).strip()
            items = (
                payload.get("items")
                if isinstance(payload.get("items"), dict)
                else {}
            )
            for source_key, item in items.items():
                if not isinstance(item, dict):
                    continue
                business_confirmed = (
                    item.get("business_result_confirmed") is True
                )
                business_state = str(
                    item.get("business_state") or ""
                ).strip()
                terminal_payload = (
                    dict(item.get("terminal_payload") or {})
                    if isinstance(item.get("terminal_payload"), dict)
                    else {}
                )
                phase = str(
                    item.get("action_phase") or "not_attempted"
                ).strip()
                if phase == "cancelled_before_trigger":
                    continue
                formed_fact = bool(terminal_payload) or business_state in {
                    "completed",
                    "failed",
                } or business_confirmed or bool(
                    str(item.get("error_code") or "").strip()
                )
                if phase == "not_attempted" and not formed_fact:
                    continue
                if action_kind == "image" and isinstance(
                    item.get("replayable_observation"),
                    dict,
                ):
                    recovered_state = (
                        "completed" if business_confirmed else "failed"
                    )
                    recovered_reason = str(
                        terminal_payload.get("error_code")
                        or item.get("error_code")
                        or (
                            "vision_ready"
                            if business_confirmed
                            else "IMAGE_INTERRUPTED_AFTER_TRIGGER"
                        )
                    )
                    terminal_observation = apply_image_terminal_result(
                        dict(item["replayable_observation"]),
                        {
                            "state": recovered_state,
                            "reason": recovered_reason,
                            "action_phase": phase,
                            "customer_image_understanding": (
                                terminal_payload.get(
                                    "customer_image_understanding"
                                )
                            ),
                            "visual_bridge_input": terminal_payload.get(
                                "visual_bridge_input"
                            ),
                        },
                    )
                    terminal_payload[
                        "replayable_observation"
                    ] = replayable_image_observation(
                        terminal_observation,
                        source_message_key=str(source_key),
                    )
                outcome = classify_action_result(
                    action_kind,
                    {
                        "action_phase": phase,
                        "business_state": business_state,
                        "business_result_confirmed": business_confirmed,
                        "error_code": (
                            str(item.get("error_code") or "").strip()
                            or (
                                None
                                if business_confirmed
                                else f"{action_kind.upper()}_INTERRUPTED_AFTER_TRIGGER"
                            )
                        ),
                        "evidence": {
                            "action_kind": action_kind,
                            "physical_anchor_keys": list(
                                item.get("physical_anchor_keys") or []
                            ),
                            "recovered_from_action_journal": True,
                            "transaction_id": str(
                                payload.get("transaction_id") or ""
                            ),
                        },
                        **(
                            {
                                "customer_image_understanding": (
                                    terminal_payload
                                ).get("customer_image_understanding")
                            }
                            if action_kind == "image"
                            and terminal_payload
                            else {}
                        ),
                    },
                    source_message_key=str(source_key),
                )
                if terminal_payload:
                    outcome["terminal_payload"] = terminal_payload
                outcome["origin_read_run_id"] = origin_read_run_id
                recovered_outcomes = merge_item_outcomes(
                    recovered_outcomes,
                    [outcome],
                )
        return recovered_outcomes

    def _read_one_wechat_target(
        self,
        binding: Binding,
        target: WechatReadTarget,
        **kwargs: Any,
    ) -> dict[str, Any]:
        unresolved_action_journals = (
            self._recover_physical_action_journals(target)
        )
        if unresolved_action_journals:
            read_run_id = str(
                unresolved_action_journals[0].get("origin_read_run_id") or ""
            ).strip() or f"recovery-{uuid.uuid4()}"
            self._report_identity_failure_gate(
                binding=binding,
                target=target,
                read_run_id=read_run_id,
                error_code="MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
                identity_errors=unresolved_action_journals,
            )
            return {
                "ok": False,
                "error_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
                "identity_errors": unresolved_action_journals,
            }
        self._recover_c2_action_journal(target)
        existing_runtime_flow_id = str(
            load_runtime_control().get("inflight_flow_id") or ""
        ).strip()
        owns_inflight_flow = not existing_runtime_flow_id
        # A nested pre-send/current-customer refresh is part of the already
        # registered customer transaction. Reuse that exact durable id so
        # media continuation checks cannot accidentally compare a fresh local
        # UUID with the outer draining flow and abort the current customer.
        read_run_id = existing_runtime_flow_id or f"read-{uuid.uuid4()}"
        process_run_id = str(target.process_run_id or "").strip()
        if process_run_id:
            remember_process_run(
                read_run_id,
                process_run_id,
                conversation_id=target.conversation_id,
            )
        if owns_inflight_flow and not self._start_inflight_flow(
            binding,
            flow_id=read_run_id,
            flow_kind="c2_read",
        ):
            return {
                "ok": False,
                "error_code": "WORKER_NEW_FLOW_NOT_ALLOWED",
            }
        if existing_runtime_flow_id:
            self.api.inflight_flow_id = existing_runtime_flow_id
        flow_id = f"c2-action:{uuid.uuid4()}"
        flow_outcomes = FlowOutcomeAccumulator(
            checkpoint=lambda outcomes: checkpoint_c2_action_outcomes(
                flow_id=flow_id,
                conversation_id=target.conversation_id,
                origin_read_run_id=read_run_id,
                outcomes=outcomes,
            ),
            origin_read_run_id=read_run_id,
        )
        previous_runtime_context = self._runtime_process_context
        operation_phase = str(
            kwargs.get("operation_phase") or C2_AUTHORIZED_READ_PHASE
        )
        runtime_context = {
            "conversation_id": target.conversation_id,
            "remark_code": target.remark_code,
            "transaction_id": read_run_id,
            "operation_phase": operation_phase,
        }
        self._runtime_process_context = runtime_context
        inflight_activity = {"message_read_attempted": False}
        flow_result: dict[str, Any] = {}
        pre_send_stage_timer = (
            StageTimer(
                process_run_id=process_run_id,
                conversation_id=target.conversation_id,
                stage_name="c3.pre_send_refresh",
                component="worker",
                trace_id=str(uuid.uuid4()),
            )
            if process_run_id and operation_phase == C2_PRE_SEND_REFRESH_PHASE
            else None
        )
        if operation_phase == C2_AUTHORIZED_READ_PHASE:
            raw_target = target.raw if isinstance(target.raw, dict) else {}
            source = (
                "微信会话第一屏命中"
                if target.read_reason == "visible_hit"
                or raw_target.get("visible_hit") is True
                else "状态机定向读取"
            )
            self._emit_runtime_process(
                {
                    "event": "customer_started",
                    "source": source,
                    **runtime_context,
                }
            )
        try:
            try:
                result = self._read_one_wechat_target_impl(
                    binding,
                    target,
                    flow_outcomes=flow_outcomes,
                    read_run_id=read_run_id,
                    inflight_activity=inflight_activity,
                    **kwargs,
                )
            except Exception as exc:
                if pre_send_stage_timer is not None:
                    pre_send_stage_timer.finish(
                        status="failed",
                        error_code=type(exc).__name__,
                    )
                    schedule_stage_event_upload(self.api, binding)
                raise
            flow_result = dict(result)
            if pre_send_stage_timer is not None:
                pre_send_stage_timer.finish(
                    status="succeeded" if result.get("ok") else "failed",
                    error_code=str(result.get("error_code") or "") or None,
                )
                schedule_stage_event_upload(self.api, binding)
            if operation_phase == C2_AUTHORIZED_READ_PHASE:
                self._emit_runtime_process(
                    {
                        "event": "customer_completed",
                        "terminal_state": self._runtime_terminal_for_result(
                            result
                        ),
                        "error_code": result.get("error_code"),
                        **runtime_context,
                    }
                )
            return result
        finally:
            current_journal_entries = [
                (path, payload)
                for path in flow_outcomes.action_journal_paths()
                if (payload := read_action_journal(path))
            ]
            flow_outcomes.extend(
                self._physical_action_journal_outcomes(
                    current_journal_entries,
                )
            )
            self._finalize_c2_flow_outcomes(target, flow_outcomes)
            clear_c2_action_journal(flow_id)
            for path, payload in current_journal_entries:
                if self._action_journal_can_be_removed(payload):
                    remove_action_journal(path)
            self._runtime_process_context = previous_runtime_context
            if owns_inflight_flow:
                error_code = str(
                    flow_result.get("error_code") or "C2_READ_STOPPED"
                ).strip()
                durable_read_artifact_exists = bool(
                    has_c2_outbox_for_read_run_id(read_run_id)
                    or has_c2_ledger_for_origin_read_run_id(read_run_id)
                    or has_c2_action_journal_for_origin_read_run_id(read_run_id)
                    or self._has_physical_action_journal_for_flow(read_run_id)
                )
                read_completion = (
                    (flow_result.get("result") or {}).get("read_completion")
                    if isinstance(flow_result.get("result"), dict)
                    else {}
                )
                backend_read_confirmed = bool(
                    isinstance(read_completion, dict)
                    and str(read_completion.get("result") or "")
                    in {"new_facts", "no_change"}
                )
                if backend_read_confirmed or durable_read_artifact_exists:
                    terminal_kind = "read_confirmed"
                elif inflight_activity.get("message_read_attempted") is True:
                    terminal_kind = "read_failed_no_fact"
                else:
                    terminal_kind = "failed_before_message_action"
                receipt = {
                    "terminal_kind": terminal_kind,
                    "conversation_id": target.conversation_id,
                    "error_code": (
                        error_code
                        if terminal_kind
                        in {
                            "failed_before_message_action",
                            "read_failed_no_fact",
                        }
                        else None
                    ),
                }
                save_c2_state(
                    self._inflight_finish_receipt_key(read_run_id), receipt
                )
                try:
                    self._finish_inflight_flow(
                        binding,
                        read_run_id,
                        terminal_kind=terminal_kind,
                        conversation_id=target.conversation_id,
                        error_code=receipt["error_code"],
                    )
                except Exception as exc:
                    append_log(
                        "ERROR",
                        "inflight_flow_finish_failed",
                        "C2 结算完成，但在途流程结束登记失败。",
                        error_code="RUNTIME_INFLIGHT_FINISH_FAILED",
                        metadata={"flow_id": read_run_id, "error": str(exc)},
                    )

    def _recover_c2_action_journal(
        self,
        target: WechatReadTarget,
    ) -> None:
        pending = list_c2_action_journal(target.conversation_id)
        if not pending:
            return
        outcomes = merge_item_outcomes(
            [],
            [
                item.get("outcome")
                for item in pending
                if isinstance(item.get("outcome"), dict)
            ],
        )
        self._persist_c2_flow_outcomes(target, outcomes)
        for flow_id in {
            str(item.get("flow_id") or "")
            for item in pending
            if str(item.get("flow_id") or "")
        }:
            clear_c2_action_journal(flow_id)
        append_log(
            "WARN",
            "c2_action_journal_recovered",
            "检测到上次进程退出前已经发生的微信动作，已先恢复本地账本，禁止重复操作。",
            metadata={
                "conversation_id": target.conversation_id,
                "remark_code": target.remark_code,
                "outcome_count": len(outcomes),
            },
        )

    @staticmethod
    def _finalize_c2_flow_outcomes(
        target: WechatReadTarget,
        flow_outcomes: FlowOutcomeAccumulator,
    ) -> None:
        TaskRunner._persist_c2_flow_outcomes(
            target,
            flow_outcomes.snapshot(),
            origin_read_run_id=flow_outcomes.origin_read_run_id,
        )

    @staticmethod
    def _persist_c2_flow_outcomes(
        target: WechatReadTarget,
        outcomes: list[dict[str, Any]],
        *,
        origin_read_run_id: str | None = None,
    ) -> None:
        for outcome in outcomes:
            source_key = str(outcome.get("source_message_key") or "").strip()
            outcome_origin_read_run_id = str(
                outcome.get("origin_read_run_id")
                or origin_read_run_id
                or ""
            ).strip()
            result = str(outcome.get("result") or "").strip().lower()
            if not source_key or result not in {"completed", "failed"}:
                continue
            if not outcome_origin_read_run_id:
                raise ValueError("C2_FLOW_OUTCOME_ORIGIN_READ_RUN_ID_MISSING")
            evidence = (
                dict(outcome.get("evidence") or {})
                if isinstance(outcome.get("evidence"), dict)
                else {}
            )
            message_type = str(
                evidence.get("action_kind") or ""
            ).strip()
            if message_type not in {"voice", "image"}:
                raise ValueError("C2_FLOW_ACTION_KIND_INVALID")
            existing = load_c2_ledger_entry(
                target.conversation_id,
                source_key,
            )
            if (
                existing
                and str(existing.get("terminal_state") or "") != result
            ):
                raise ValueError(
                    f"C2_LEDGER_TERMINAL_CONFLICT:{source_key}"
                )
            existing_result = (
                dict(existing.get("result") or {})
                if isinstance((existing or {}).get("result"), dict)
                else {}
            )
            save_c2_ledger_terminal(
                conversation_id=target.conversation_id,
                source_message_key=source_key,
                origin_read_run_id=outcome_origin_read_run_id,
                dedupe_key=(
                    str(existing.get("dedupe_key") or "") or None
                    if existing
                    else None
                ),
                message_type=str(
                    (existing or {}).get("message_type") or message_type
                ),
                terminal_state=result,
                ingest_state=str(
                    (existing or {}).get("ingest_state") or "waiting"
                ),
                result={
                    **existing_result,
                    **(
                        dict(outcome.get("terminal_payload") or {})
                        if isinstance(outcome.get("terminal_payload"), dict)
                        else {}
                    ),
                    "state": result,
                    "action_outcome": outcome,
                },
            )

    def _read_one_wechat_target_impl(
        self,
        binding: Binding,
        target: WechatReadTarget,
        *,
        flow_outcomes: FlowOutcomeAccumulator,
        read_run_id: str,
        inflight_activity: dict[str, bool],
        operation_phase: C2ReadOperationPhase = C2_AUTHORIZED_READ_PHASE,
        current_step: str = "message_read",
        allow_during_current_task: bool = False,
        enforce_read_targets: bool = False,
        held_lease: UiLockLease | None = None,
        current_only: bool = False,
        wait_for_brain: bool = True,
        recovery_waiting_image_facts: bool = False,
    ) -> dict[str, Any]:
        owner = f"{binding.worker_id}:{binding.client_instance_id}:message_ingest:{target.conversation_id}"
        lease: UiLockLease | None = held_lease
        owns_lease = held_lease is None
        flow_started_at = time.perf_counter()
        flow_timing: dict[str, Any] = {
            "schema_version": 1,
            "flow": "c2_message_read",
            "conversation_id": target.conversation_id,
            "remark_code": target.remark_code,
            "read_run_id": read_run_id,
            "operation_phase": operation_phase,
            "phases": [],
        }

        def record_phase(name: str, started_at: float, **metadata: Any) -> None:
            phase = {
                "name": name,
                "duration_seconds": round(max(0.0, time.perf_counter() - started_at), 4),
            }
            phase.update(
                _freeze_phase_metadata(
                    {
                        key: value
                        for key, value in metadata.items()
                        if value is not None
                    }
                )
            )
            flow_timing["phases"].append(phase)

        last_authorization_check = 0.0

        def action_cancel_requested() -> bool | str:
            nonlocal last_authorization_check
            ui_lock_reason = _ui_lock_cancel_reason(lease)
            if ui_lock_reason:
                return ui_lock_reason
            if (
                self.stop_event.is_set()
                or not self._can_continue_inflight_flow(read_run_id)
                or (
                    self.current_task_lease is not None
                    and self.current_task_lease.cancel_requested()
                )
            ):
                return True
            if not enforce_read_targets:
                return False
            now = time.monotonic()
            if now - last_authorization_check < 1.0:
                return False
            last_authorization_check = now
            return not self._backend_still_allows_read_target_lightweight(
                binding,
                target,
            )

        def settle_identity_terminal_gate(
            terminal_gate: dict[str, Any],
            *,
            final_payload: dict[str, Any],
            target_confirmation: dict[str, Any],
            initial_observations: list[Any],
        ) -> dict[str, Any]:
            """Finish one conversation gate without normal slot settlement."""

            gate_error_code = str(
                terminal_gate.get("error_code")
                or "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
            )
            identity_errors = [
                dict(item)
                for item in (terminal_gate.get("identity_errors") or [])
                if isinstance(item, dict)
            ]
            gate_reported = self._report_identity_failure_gate(
                binding=binding,
                target=target,
                read_run_id=read_run_id,
                error_code=gate_error_code,
                identity_errors=identity_errors,
                authoritative_frame_source=str(
                    terminal_gate.get("authoritative_frame_source")
                    or "final_read"
                ),
                ui_frame_invalidated=(
                    terminal_gate.get("ui_frame_invalidated") is True
                ),
            )
            self.c2_stats["last_error"] = gate_error_code
            return {
                "ok": False,
                "error_code": gate_error_code,
                "identity_gate_reported": gate_reported,
                "identity_errors": identity_errors,
                "target_confirmation": target_confirmation,
                "initial_observations": initial_observations,
                "final_messages": final_payload,
            }

        try:
            if operation_phase not in {
                C2_AUTHORIZED_READ_PHASE,
                C2_PRE_SEND_REFRESH_PHASE,
            }:
                return {
                    "ok": False,
                    "error_code": "C2_READ_OPERATION_PHASE_INVALID",
                }
            if (current_step == "pre_send_refresh") != (
                operation_phase == C2_PRE_SEND_REFRESH_PHASE
            ):
                return {
                    "ok": False,
                    "error_code": "C2_READ_OPERATION_PHASE_CONFLICT",
                }
            if not str(target.authorization_revision or "").strip():
                self.c2_stats["last_error"] = "C2_TARGET_AUTHORIZATION_REVISION_MISSING"
                append_log(
                    "WARN",
                    "c2_message_read_blocked_by_missing_authorization",
                    "C2 目标缺少后端签发的当前授权票据，未执行任何微信操作。",
                    error_code="C2_TARGET_AUTHORIZATION_REVISION_MISSING",
                    metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code},
                )
                return {"ok": False, "error_code": "C2_TARGET_AUTHORIZATION_REVISION_MISSING"}
            if not allow_during_current_task and self._high_priority_active():
                self.c2_stats["last_error"] = "SCAN_INTERRUPTED_BY_HIGH_PRIORITY_ACTION"
                append_log("INFO", "c2_message_read_interrupted", "C2 消息读取被高优先级微信动作中断。", error_code="SCAN_INTERRUPTED_BY_HIGH_PRIORITY_ACTION", metadata={"conversation_id": target.conversation_id})
                return {"ok": False, "error_code": "SCAN_INTERRUPTED_BY_HIGH_PRIORITY_ACTION"}
            if owns_lease:
                with self.task_lock:
                    if not self._worker_transaction_barrier_ready(
                        binding,
                        reason="message_read_lock",
                        allowed_image_recovery_conversation_id=(
                            target.conversation_id
                            if recovery_waiting_image_facts
                            else ""
                        ),
                    ):
                        return {
                            "ok": False,
                            "error_code": "WORKER_TRANSACTION_BARRIER_PENDING",
                        }
                    if self.current_task is not None:
                        self.c2_stats["last_error"] = (
                            "SCAN_INTERRUPTED_BY_HIGH_PRIORITY_ACTION"
                        )
                        return {
                            "ok": False,
                            "error_code": "SCAN_INTERRUPTED_BY_HIGH_PRIORITY_ACTION",
                        }
                    force_recover_stale_lock(reason="before_message_ingest")
                    lease = acquire_ui_lock(
                        operation_type="message_ingest",
                        owner=owner,
                        current_step=current_step,
                        timeout_seconds=CONFIG.c2_low_priority_lock_timeout_seconds,
                    )
                    lease.start_auto_renew()
                    self.current_ui_lock = lease
            if lease is None:
                return {"ok": False, "error_code": "UI_LOCK_NOT_HELD"}
            self.current_step = current_step
            if enforce_read_targets and not self._backend_still_allows_read_target(binding, target):
                return {"ok": False, "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS"}
            effective_target = target
            target_label = str(effective_target.remark_code or "").strip()
            real_time_visible_metadata: dict[str, Any] = {}
            visible_source = ""
            fallback_target_mode = ""
            target_is_visible_hit = target.read_reason == "visible_hit" or bool(
                isinstance(target.raw, dict) and target.raw.get("visible_hit")
            )
            friend_activation_read = bool(
                target.read_reason == "friend_acceptance_visible_hit"
                and operation_phase == C2_AUTHORIZED_READ_PHASE
            )
            if target_is_visible_hit:
                base_target_mode = "visible"
                visible_source = "visible_hit_queue"
            else:
                previous_visible_sessions = list(self.c2_last_visible_sessions) if isinstance(self.c2_last_visible_sessions, list) else []
                previous_visible_sessions_seen_at = float(self.c2_last_visible_sessions_monotonic or 0)
                recent_visible_target = self._visible_target_from_recent_scan(
                    target,
                    sessions=previous_visible_sessions,
                    seen_at=previous_visible_sessions_seen_at,
                )
                if recent_visible_target:
                    effective_target = recent_visible_target
                    target_label = str(effective_target.remark_code or "").strip()
                    visible_source = "recent_visible_scan_hint"
                    if isinstance(effective_target.raw, dict):
                        effective_target.raw["authorization_read_reason"] = target.read_reason
                else:
                    visible_source = "atomic_visible_scan"
                base_target_mode = "visible"
                fallback_target_mode = "search_by_remark_code" if effective_target.remark_code else ""
                real_time_visible_metadata = {
                    "merged_into_locate": True,
                    "reason": "fresh_visible_scan_and_click_share_one_sidecar_frame",
                    "recent_hint_used": bool(recent_visible_target),
                }
                append_log(
                    "INFO",
                    "c2_state_target_visible_check_merged",
                    "C2 状态目标的实时首屏检查已合并到定位动作，使用同一张新截图匹配并点击。",
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "visible_source": visible_source,
                        "visible_scan": real_time_visible_metadata,
                    },
                )
            if enforce_read_targets and not self._backend_still_allows_read_target(binding, target):
                return {"ok": False, "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS"}
            lease.update_step("target_chat_locating")
            self.current_step = "target_chat_locating"
            target_cache_key = self._target_dedupe_key(target)
            cache = self.c2_active_target_cache if isinstance(self.c2_active_target_cache, dict) else {}
            can_try_current = (
                cache.get("target_key") == target_cache_key
                and float(cache.get("expires_at") or 0) > time.monotonic()
            )
            if base_target_mode == "visible" and effective_target.remark_code and not fallback_target_mode:
                fallback_target_mode = "search_by_remark_code"
            locate_modes = ["current"] if current_only else ["current", base_target_mode] if can_try_current else [base_target_mode]
            if not current_only and fallback_target_mode and fallback_target_mode not in locate_modes:
                locate_modes.append(fallback_target_mode)
            locate_payload: dict[str, Any] = {}
            safe_visible_relocation_used = False
            for locate_mode in locate_modes:
                visible_target = locate_mode == "visible"
                phase_started_at = time.perf_counter()
                locate_payload = self.bridge.locate_chat(
                    display_name=target_label,
                    rpa_session_key=effective_target.rpa_session_key if visible_target else "",
                    remark_code=effective_target.remark_code or "",
                    target_mode=locate_mode,
                    visible_session_candidate=effective_target.raw.get("visible_session_candidate") if visible_target and isinstance(effective_target.raw, dict) else None,
                    capture_initial_messages=not friend_activation_read,
                    max_duration_seconds=20 if locate_mode == "current" else 30 if visible_target else 90,
                    cancel_check=action_cancel_requested,
                )
                record_phase(
                    "target_chat_locate",
                    phase_started_at,
                    target_mode=locate_mode,
                    state=locate_payload.get("state"),
                    sidecar_run_id=locate_payload.get("sidecar_run_id"),
                )
                attempt_ok = bool(locate_payload.get("ok"))
                append_log(
                    "INFO" if attempt_ok else "WARN",
                    "c2_target_chat_locate_attempt",
                    f"C2 目标会话定位尝试：{locate_mode}。",
                    error_code=None
                    if attempt_ok
                    else str(locate_payload.get("error_code") or locate_payload.get("state") or "TARGET_NOT_CONFIRMED"),
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "target_mode": locate_mode,
                        "visible_source": visible_source,
                        "state": locate_payload.get("state"),
                        "sidecar_run_id": locate_payload.get("sidecar_run_id"),
                        "artifact_dir": locate_payload.get("artifact_dir"),
                        "review_path": locate_payload.get("review_path"),
                        "targeting": locate_payload.get("targeting"),
                        "step_events": locate_payload.get("step_events"),
                        "open_chat_timing": locate_payload.get("open_chat_timing"),
                    },
                )
                if locate_payload.get("ok"):
                    break
                locate_error_code = str(
                    locate_payload.get("error_code") or ""
                )
                location_recovery_contract = (
                    target_location_recovery_contract()
                )
                location_recovery_error_code = str(
                    location_recovery_contract.get("error_code") or ""
                )
                if locate_error_code == location_recovery_error_code:
                    stale_evidence = (
                        (locate_payload.get("targeting") or {}).get(
                            "stale_after_click"
                        )
                        if isinstance(locate_payload.get("targeting"), dict)
                        else None
                    )
                    required_evidence_values = dict(
                        location_recovery_contract.get(
                            "required_evidence_values"
                        )
                        or {}
                    )
                    safe_relocation_evidence = bool(
                        isinstance(stale_evidence, dict)
                        and required_evidence_values
                        and all(
                            stale_evidence.get(field) is expected
                            for field, expected in required_evidence_values.items()
                        )
                    )
                    if (
                        locate_mode != "visible"
                        or current_only
                        or safe_visible_relocation_used
                        or not safe_relocation_evidence
                    ):
                        locate_payload["error_code"] = (
                            "C2_VISIBLE_TARGET_STALE_EVIDENCE_INVALID"
                            if not safe_relocation_evidence
                            else location_recovery_error_code
                        )
                        break
                    if enforce_read_targets and not self._backend_still_allows_read_target(
                        binding,
                        target,
                    ):
                        return {
                            "ok": False,
                            "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                            "target_confirmation": locate_payload,
                        }
                    safe_visible_relocation_used = True
                    self.c2_active_target_cache = {}
                    append_log(
                        "WARN",
                        "c2_visible_target_stale_relocating",
                        "会话列表在点击前发生重排；已确认误点无消息或媒体副作用，将在同一授权内重新定位一次。",
                        error_code=locate_error_code,
                        metadata={
                            "conversation_id": target.conversation_id,
                            "remark_code": target.remark_code,
                            "read_run_id": read_run_id,
                            "next_target_mode": "search_by_remark_code",
                        },
                    )
                    continue
                if locate_error_code in C2_LOCATE_TERMINAL_ERROR_CODES:
                    break
            locate_payload["visible_scan"] = real_time_visible_metadata
            append_log(
                "INFO" if locate_payload.get("ok") else "WARN",
                "c2_target_chat_located",
                "C2 目标会话定位完成。" if locate_payload.get("ok") else "C2 目标会话定位失败。",
                error_code=None if locate_payload.get("ok") else str(locate_payload.get("error_code") or locate_payload.get("state") or "TARGET_NOT_CONFIRMED"),
                metadata={
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "state": locate_payload.get("state"),
                    "sidecar_run_id": locate_payload.get("sidecar_run_id"),
                    "artifact_dir": locate_payload.get("artifact_dir"),
                    "review_path": locate_payload.get("review_path"),
                    "target_mode": locate_payload.get("target_mode"),
                    "visible_source": visible_source,
                    "attempted_target_modes": locate_modes,
                    "safe_visible_relocation_used": safe_visible_relocation_used,
                    "targeting": locate_payload.get("targeting"),
                    "step_events": locate_payload.get("step_events"),
                    "open_chat_timing": locate_payload.get("open_chat_timing"),
                    "visible_scan": real_time_visible_metadata,
                },
            )
            if not locate_payload.get("ok"):
                code = str(locate_payload.get("error_code") or locate_payload.get("state") or "TARGET_NOT_CONFIRMED")
                self.c2_stats["last_error"] = code
                return {"ok": False, "error_code": code, "target_confirmation": locate_payload}
            self.c2_active_target_cache = {
                "target_key": target_cache_key,
                "conversation_id": target.conversation_id,
                "remark_code": target.remark_code,
                "expires_at": time.monotonic() + 120.0,
            }
            if enforce_read_targets and not self._backend_still_allows_read_target(binding, target):
                return {"ok": False, "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS", "target_confirmation": locate_payload}
            if target.read_reason == "friend_acceptance_visible_hit":
                locate_guard = (
                    locate_payload.get("guard")
                    if isinstance(locate_payload.get("guard"), dict)
                    else {}
                )
                title_evidence = (
                    locate_payload.get("conversation_type_evidence")
                    if isinstance(locate_payload.get("conversation_type_evidence"), dict)
                    else locate_guard.get("conversation_type_evidence")
                    if isinstance(locate_guard.get("conversation_type_evidence"), dict)
                    else {}
                )
                conversation_type = str(
                    locate_payload.get("conversation_type")
                    or locate_guard.get("conversation_type")
                    or title_evidence.get("conversation_type")
                    or "unknown"
                ).strip().lower()
                if (
                    conversation_type != "private"
                    or title_evidence.get("short_code_confirmed") is not True
                    or title_evidence.get("admission_allowed") is not True
                ):
                    code = "C2_FRIEND_ACTIVATION_EVIDENCE_INVALID"
                    self.c2_stats["last_error"] = code
                    append_log(
                        "WARN",
                        "c2_friend_activation_blocked",
                        "新好友会话缺少短码与 private 单聊证据，未读取消息。",
                        error_code=code,
                        metadata={
                            "conversation_id": target.conversation_id,
                            "remark_code": target.remark_code,
                            "conversation_type": conversation_type,
                            "title_evidence": title_evidence,
                        },
                    )
                    return {"ok": False, "error_code": code, "target_confirmation": locate_payload}
                if friend_activation_read:
                    phase_started_at = time.perf_counter()
                    activation = self.api.confirm_wechat_friend_activation(
                        binding,
                        target,
                        conversation_type=conversation_type,
                        chat_surface_ready=True,
                        title_evidence=title_evidence,
                    )
                    record_phase(
                        "friend_activation_confirm",
                        phase_started_at,
                        activation_confirmed=activation.get(
                            "activation_confirmed"
                        ),
                        friend_state=activation.get("friend_state"),
                    )
                    if activation.get("activation_confirmed") is not True:
                        code = "C2_FRIEND_ACTIVATION_NOT_CONFIRMED"
                        self.c2_stats["last_error"] = code
                        return {
                            "ok": False,
                            "error_code": code,
                            "target_confirmation": locate_payload,
                        }
                    refreshed_revision = str(
                        activation.get("authorization_revision") or ""
                    ).strip()
                    if refreshed_revision:
                        target.authorization_revision = refreshed_revision
                    if isinstance(target.raw, dict):
                        target.raw["friend_activation"] = dict(activation)
                    append_log(
                        "INFO",
                        "c2_friend_activation_confirmed",
                        "新好友已由后端确认激活，现在才允许读取文字、语音和图片。",
                        metadata={
                            "conversation_id": target.conversation_id,
                            "remark_code": target.remark_code,
                            "friend_state": activation.get("friend_state"),
                            "conversation_status": activation.get(
                                "conversation_status"
                            ),
                        },
                    )
                else:
                    append_log(
                        "INFO",
                        "c2_friend_activation_provenance_preserved",
                        "批次续行保留首次读取原因，但发送前复读不会重复执行好友激活状态转换。",
                        metadata={
                            "conversation_id": target.conversation_id,
                            "remark_code": target.remark_code,
                            "authorization_read_reason": target.read_reason,
                            "operation_phase": operation_phase,
                        },
                    )
            lease.update_step(current_step)
            self.current_step = current_step
            phase_started_at = time.perf_counter()
            # Starting OCR is not itself a durable message fact. If this
            # attempt fails before a trusted observation or local artifact is
            # formed, the flow uses the explicit read_failed_no_fact terminal.
            inflight_activity["message_read_attempted"] = True
            reusable_initial_snapshot = (
                locate_payload.get("initial_messages_snapshot")
                if isinstance(locate_payload.get("initial_messages_snapshot"), dict)
                else None
            )
            if reusable_initial_snapshot and reusable_initial_snapshot.get("ok"):
                sidecar_payload = _reuse_initial_snapshot_with_locate_evidence(
                    reusable_initial_snapshot,
                    locate_payload,
                )
            else:
                sidecar_payload = self.bridge.get_messages(
                    display_name=target_label,
                    rpa_session_key="",
                    remark_code=effective_target.remark_code or "",
                    target_mode="current",
                    max_duration_seconds=20,
                    cancel_check=action_cancel_requested,
                )
            record_phase(
                "initial_message_read",
                phase_started_at,
                state=sidecar_payload.get("state"),
                sidecar_run_id=sidecar_payload.get("sidecar_run_id"),
                reused_open_chat_confirmation_frame=bool(reusable_initial_snapshot),
            )
            sidecar_payload["target_confirmation"] = locate_payload
            sidecar_payload["authoritative_frame_source"] = "initial_read"
            sidecar_payload["ui_frame_invalidated"] = False
            if not sidecar_payload.get("ok"):
                code = str(sidecar_payload.get("error_code") or sidecar_payload.get("state") or "MESSAGE_READ_FAILED")
                self.c2_stats["last_error"] = code
                append_log(
                    "WARN",
                    "c2_message_read_sidecar_failed",
                    str(sidecar_payload.get("error") or sidecar_payload.get("reason") or code),
                    error_code=code,
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "sidecar_run_id": sidecar_payload.get("sidecar_run_id"),
                        "artifact_dir": sidecar_payload.get("artifact_dir"),
                        "review_path": sidecar_payload.get("review_path"),
                        "evidence_path": sidecar_payload.get("evidence_path"),
                        "target_mode": sidecar_payload.get("target_mode"),
                        "targeting": sidecar_payload.get("targeting"),
                        "step_events": sidecar_payload.get("step_events"),
                        "open_chat_timing": sidecar_payload.get("open_chat_timing"),
                    },
                    force_incident=True,
                )
                return {"ok": False, "error_code": code}
            initial_contract_error = sidecar_contract_error(sidecar_payload)
            if initial_contract_error:
                self.c2_stats["last_error"] = initial_contract_error
                append_log(
                    "WARN",
                    "c2_sidecar_contract_invalid",
                    "OmniAuto 消息观察不符合当前唯一 C2 合同，已在任何语音操作和入库前阻断。",
                    error_code=initial_contract_error,
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "contract_revision": sidecar_payload.get("contract_revision"),
                        "contract_sha256": sidecar_payload.get("contract_sha256"),
                        "observation_validation_errors": sidecar_payload.get("observation_validation_errors"),
                    },
                    force_incident=True,
                )
                return {
                    "ok": False,
                    "error_code": initial_contract_error,
                    "target_confirmation": locate_payload,
                    "initial_messages": sidecar_payload,
                }
            if enforce_read_targets and not self._backend_still_allows_read_target(binding, target):
                return {"ok": False, "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS", "target_confirmation": locate_payload, "initial_messages": sidecar_payload}
            sidecar_payload, initial_identity_errors = (
                self._align_initial_identity_frame(
                    target=target,
                    sidecar_payload=sidecar_payload,
                    read_run_id=read_run_id,
                )
            )
            if initial_identity_errors:
                code = str(
                    initial_identity_errors[0].get("error_code")
                    or "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
                )
                self.c2_stats["last_error"] = code
                append_log(
                    "WARN",
                    "c2_initial_sequence_alignment_blocked",
                    "当前画面无法与后端身份尾部唯一对齐，已禁止媒体动作、入库和 Brain。",
                    error_code=code,
                    metadata={
                        "conversation_id": target.conversation_id,
                        "identity_errors": initial_identity_errors,
                        "sequence_alignment_evidence": sidecar_payload.get(
                            "sequence_alignment_evidence"
                        ),
                    },
                    force_incident=True,
                )
                self._report_identity_failure_gate(
                    binding=binding,
                    target=target,
                    read_run_id=read_run_id,
                    error_code=code,
                    identity_errors=initial_identity_errors,
                )
                return {
                    "ok": False,
                    "error_code": code,
                    "initial_messages": sidecar_payload,
                }
            voice_binding_guard_key = (
                f"{target_cache_key}:{str(target.authorization_revision).strip()}"
            )
            if voice_binding_guard_key in self.c2_voice_binding_blocked_authorizations:
                code = "C2_VOICE_TRANSCRIPT_BINDING_PENDING"
                self.c2_stats["last_error"] = code
                append_log(
                    "WARN",
                    "c2_messages_ingest_blocked_by_voice_binding",
                    "此前已出现可见语音正文与稳定锚点绑定矛盾，本授权轮次禁止部分上报。",
                    error_code=code,
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "authorization_revision": target.authorization_revision,
                    },
                )
                return {
                    "ok": False,
                    "error_code": code,
                    "target_confirmation": locate_payload,
                    "initial_messages": sidecar_payload,
                }
            initial_read_observations = list(sidecar_payload.get("observations") or [])
            excluded_voice_anchor_keys: set[str] = set()
            voice_item_outcomes: list[dict[str, Any]] = []
            voice_action_failure_code = ""
            (
                restored_failed_voice_source_keys,
                restored_failed_voice_roles,
            ) = self._restore_visible_failed_voice_facts(
                target=target,
                sidecar_payload=sidecar_payload,
            )
            if _executable_untranscribed_voice_observations(
                target,
                sidecar_payload,
            ):
                phase_started_at = time.perf_counter()
                voice_result = self._finish_new_visible_voices_in_current_chat(
                    binding=binding, target=target, target_label=target_label,
                    sidecar_payload=sidecar_payload, lease=lease,
                    action_cancel_requested=action_cancel_requested,
                    enforce_read_targets=enforce_read_targets,
                    read_run_id=read_run_id,
                    excluded_voice_anchor_keys=excluded_voice_anchor_keys,
                    flow_outcomes=flow_outcomes,
                )
                record_phase(
                    "voice_transcribe",
                    phase_started_at,
                    completed=bool(voice_result.get("ok")),
                    failure_code=voice_result.get("failure_code"),
                    item_count=len(voice_result.get("item_outcomes") or []),
                )
                if not voice_result.get("ok"):
                    code = str(voice_result.get("error_code") or "C2_VOICE_ORCHESTRATION_FAILED")
                    self.c2_stats["last_error"] = code
                    return {"ok": False, "error_code": code, "target_confirmation": locate_payload, "initial_messages": sidecar_payload, "final_messages": voice_result.get("payload")}
                sidecar_payload = dict(
                    voice_result.get("payload") or sidecar_payload
                )
                voice_item_outcomes = list(
                    voice_result.get("item_outcomes") or []
                )
                voice_action_failure_code = str(
                    voice_result.get("failure_code") or ""
                )
                terminal_gate = voice_result.get("terminal_gate")
                if isinstance(terminal_gate, dict):
                    return settle_identity_terminal_gate(
                        terminal_gate,
                        final_payload=sidecar_payload,
                        target_confirmation=locate_payload,
                        initial_observations=initial_read_observations,
                    )
            partial_failed_voice_source_keys = sorted({
                *restored_failed_voice_source_keys,
                *{
                    str(item.get("source_message_key") or "")
                    for item in voice_item_outcomes
                    if isinstance(item, dict)
                    and item.get("result") == "failed"
                    and str(item.get("source_message_key") or "")
                },
            })
            partial_failed_voice_roles = {
                **restored_failed_voice_roles,
                **{
                    str(item.get("source_message_key") or ""): str(
                        (item.get("evidence") or {}).get("sender_role")
                        or ""
                    )
                    for item in voice_item_outcomes
                    if isinstance(item, dict)
                    and item.get("result") == "failed"
                    and str(
                        (item.get("evidence") or {}).get("sender_role")
                        or ""
                    ) in {"customer", "self"}
                },
            }
            sidecar_payload = self._merge_waiting_image_facts(target=target, sidecar_payload=sidecar_payload)
            sidecar_payload["observations"] = self._attach_possible_ai_send_receipts(target=target, observations=self._attach_confirmed_ai_reply_receipts(target=target, observations=list(sidecar_payload.get("observations") or [])))
            self._persist_c2_flow_outcomes(target, voice_item_outcomes, origin_read_run_id=read_run_id)
            if partial_failed_voice_source_keys:
                partial_failed_voice_roles.update(
                    self._annotate_failed_voice_observations(
                        target=target,
                        sidecar_payload=sidecar_payload,
                        failed_source_keys=set(
                            partial_failed_voice_source_keys
                        ),
                        error_code=(
                            voice_action_failure_code
                            or "C2_VOICE_TRANSCRIBE_FAILED"
                        ),
                    )
                )
            incremental_plan = self._build_final_slot_incremental_plan(
                target=target,
                sidecar_payload=sidecar_payload,
                read_run_id=read_run_id,
            )
            sidecar_payload, incremental_plan = (
                self._retry_history_gap_from_backend_once(
                    target=target,
                    read_run_id=read_run_id,
                    target_label=target_label,
                    sidecar_payload=sidecar_payload,
                    incremental_plan=incremental_plan,
                    cancel_check=action_cancel_requested,
                )
            )
            gate_projection = project_final_slot_flow_gates(
                incremental_plan,
                failed_voice_source_roles=partial_failed_voice_roles,
            )
            flow_gate_errors = list(
                gate_projection["flow_gate_errors"]
            )
            sidecar_payload.update(gate_projection)
            sidecar_payload["failed_voice_source_keys"] = (
                partial_failed_voice_source_keys
            )
            if flow_gate_errors:
                append_log(
                    "WARN",
                    "c2_incremental_flow_gated",
                    "C2 最终槽位存在安全门禁；允许保存已确认事实，可靠图片仍可处理，但禁止 Brain 自动回复。",
                    error_code=flow_gate_errors[0],
                    metadata={
                        "conversation_id": target.conversation_id,
                        "flow_gate_errors": flow_gate_errors,
                        "identity_errors": incremental_plan["identity_errors"],
                        "slot_ledger_states": incremental_plan["slot_ledger_states"],
                    },
                )
            allowed_new_image_source_keys = set(
                incremental_plan["new_image_source_keys"]
            )
            image_observations = [
                item
                for item in (sidecar_payload.get("observations") or [])
                if isinstance(item, dict) and item.get("row_kind") == "image_bubble"
            ]
            if image_observations:
                lease.update_step("image_understanding_current_chat")
                self.current_step = "image_understanding_current_chat"
                phase_started_at = time.perf_counter()
                sidecar_payload, image_stats = self._process_final_image_slots(
                    binding=binding,
                    target=target,
                    sidecar_payload=sidecar_payload,
                    enforce_read_targets=enforce_read_targets,
                    cancel_check=action_cancel_requested,
                    allowed_new_source_keys=allowed_new_image_source_keys,
                    flow_outcomes=flow_outcomes,
                )
                if image_stats.get("ui_frame_invalidated"):
                    sidecar_payload["ui_frame_invalidated"] = True
                record_phase("image_understanding", phase_started_at, **image_stats)
                append_log(
                    "INFO" if not image_stats.get("failed") else "WARN",
                    "c2_image_slots_finished",
                    "C2 最终画面图片槽位处理完成；单张失败不会阻断文字和语音。",
                    error_code="C2_IMAGE_SLOT_FAILED" if image_stats.get("failed") else None,
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        **image_stats,
                    },
                )
                lease.update_step(current_step)
                self.current_step = current_step
                if image_stats.get("authorization_revoked"):
                    return {
                        "ok": False,
                        "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                        "target_confirmation": locate_payload,
                        "final_messages": sidecar_payload,
                    }
                image_actions_completed = bool(
                    image_stats.get("requires_final_refresh")
                )
                convergence = (
                    self._converge_current_screen_after_images(
                        binding=binding,
                        target=target,
                        target_label=target_label,
                        sidecar_payload=sidecar_payload,
                        lease=lease,
                        action_cancel_requested=action_cancel_requested,
                        enforce_read_targets=enforce_read_targets,
                        flow_outcomes=flow_outcomes,
                    )
                    if image_actions_completed
                    else {
                        "ok": True,
                        "payload": sidecar_payload,
                        "image_stats": {},
                        "failed_voice_source_keys": [],
                        "failed_voice_roles": {},
                    }
                )
                convergence_terminal_gate = convergence.get(
                    "terminal_gate"
                )
                if isinstance(convergence_terminal_gate, dict):
                    return settle_identity_terminal_gate(
                        convergence_terminal_gate,
                        final_payload=dict(
                            convergence.get("payload") or sidecar_payload
                        ),
                        target_confirmation=locate_payload,
                        initial_observations=initial_read_observations,
                    )
                if not convergence.get("ok"):
                    code = str(
                        convergence.get("error_code")
                        or "C2_POST_VISION_CURRENT_SCREEN_FAILED"
                    )
                    self.c2_stats["last_error"] = code
                    append_log(
                        "WARN",
                        "c2_post_vision_current_screen_failed",
                        "Vision 后最终当前屏收敛失败。",
                        error_code=code,
                        metadata={
                            "conversation_id": target.conversation_id,
                            "remark_code": target.remark_code,
                            "sidecar_run_id": (
                                convergence.get("payload") or {}
                            ).get("sidecar_run_id")
                            if isinstance(convergence.get("payload"), dict)
                            else None,
                        },
                        force_incident=True,
                    )
                    return {
                        "ok": False,
                        "error_code": code,
                        "target_confirmation": locate_payload,
                        "final_messages": (
                            convergence.get("payload")
                            or sidecar_payload
                        ),
                    }
                sidecar_payload = dict(
                    convergence.get("payload") or sidecar_payload
                )
                post_image_stats = dict(
                    convergence.get("image_stats") or {}
                )
                merge_image_phase_results(
                    image_stats,
                    post_image_stats,
                )
                partial_failed_voice_source_keys = sorted(
                    {
                        *partial_failed_voice_source_keys,
                        *(
                            str(value)
                            for value in (
                                convergence.get(
                                    "failed_voice_source_keys"
                                )
                                or []
                            )
                            if str(value).strip()
                        ),
                    }
                )
                partial_failed_voice_roles.update(
                    {
                        str(key): str(value)
                        for key, value in (
                            convergence.get("failed_voice_roles") or {}
                        ).items()
                        if str(value) in {"customer", "self"}
                    }
                )
                incremental_plan = (
                    self._build_final_slot_incremental_plan(
                        target=target,
                        sidecar_payload=sidecar_payload,
                        read_run_id=read_run_id,
                    )
                )
                gate_projection = project_final_slot_flow_gates(
                    incremental_plan,
                    failed_voice_source_roles=(
                        partial_failed_voice_roles
                    ),
                )
                flow_gate_errors = list(
                    gate_projection["flow_gate_errors"]
                )
                sidecar_payload.update(gate_projection)
                sidecar_payload["failed_voice_source_keys"] = (
                    partial_failed_voice_source_keys
                )
            phase_started_at = time.perf_counter()
            try:
                payload = build_message_ingest_payload(
                    target,
                    sidecar_payload,
                    read_run_id=read_run_id,
                )
            except ValueError as exc:
                code = str(exc)
                self.c2_stats["last_error"] = code
                append_log(
                    "WARN",
                    "c2_messages_ingest_blocked_by_missing_authorization",
                    "C2 V3 入库请求不符合唯一合同，已在发送前阻断。",
                    error_code=code,
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "read_reason": target.read_reason,
                    },
                )
                return {
                    "ok": False,
                    "error_code": code,
                    "target_confirmation": locate_payload,
                    "initial_messages": sidecar_payload,
                }
            for image_message in _c2_image_messages(payload):
                position = image_message.get("message_position") if isinstance(image_message.get("message_position"), dict) else {}
                summary = str(image_message.get("content") or "")
                append_log(
                    "INFO",
                    "c2_image_slot_assembled",
                    "C2 图片已组装为统一 V3 消息并进入最终画面顺序。",
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "authorization_revision": target.authorization_revision,
                        "read_run_id": payload.get("read_run_id"),
                        "source_message_key": image_message.get("source_message_key"),
                        "dedupe_key": image_message.get("dedupe_key"),
                        "sender_role": image_message.get("sender_role_hint"),
                        "item_state": image_message.get("item_state"),
                        "flow_state": image_message.get("flow_state"),
                        "screen_order": position.get("screen_order"),
                        "order_source": position.get("order_source"),
                        "frame_source": position.get("frame_source"),
                        "vision_summary_length": len(summary),
                        "vision_summary_sha256": _c2_text_fingerprint(summary),
                        "image_persisted": False,
                    },
                )
            record_phase("build_ingest_payload", phase_started_at, message_count=len(payload.get("messages") or []))
            local_validation_errors = (
                (payload.get("evidence") or {}).get("observation_validation_errors")
                if isinstance(payload.get("evidence"), dict)
                else []
            )
            if not isinstance(local_validation_errors, list):
                local_validation_errors = []
            if local_validation_errors:
                code = str(local_validation_errors[0].get("error_code") or "C2_OBSERVATION_CONTRACT_INVALID")
                self.c2_stats["last_error"] = code
                append_log(
                    "WARN",
                    "c2_messages_ingest_blocked_by_contract",
                    "C2 observation 存在合同冲突，整批已在调用后端前阻断。",
                    error_code=code,
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "local_validation_errors": local_validation_errors,
                    },
                )
                return {
                    "ok": False,
                    "error_code": code,
                    "payload": payload,
                    "local_validation_errors": local_validation_errors,
                }
            if isinstance(payload.get("evidence"), dict):
                evidence_timing = {
                    **flow_timing,
                    "elapsed_before_ingest_seconds": round(max(0.0, time.perf_counter() - flow_started_at), 4),
                }
                payload["evidence"]["timing"] = json.loads(json.dumps(evidence_timing, ensure_ascii=False, default=str))
            payload = self._filter_confirmed_messages(payload)
            has_flow_gate = bool(
                isinstance(payload.get("evidence"), dict)
                and payload["evidence"].get("flow_gate_errors")
            )
            if not should_submit_c2_ingest_payload(
                read_reason=target.read_reason,
                messages=payload.get("messages"),
                has_flow_gate=has_flow_gate,
            ):
                record_phase("messages_ingest_skipped", time.perf_counter(), reason="all_messages_already_confirmed")
                return {
                    "ok": True,
                    "result": {"ingested_count": 0, "ignored_count": 0, "results": []},
                    "payload": payload,
                    "new_customer_message_count": 0,
                    "new_self_message_count": 0,
                    "brain_result": None,
                    "fact_ingest_ok": True,
                    "conversation_flow_ok": True,
                    "conversation_terminal_state": "no_new_facts",
                    "send_context_guard": (
                        sidecar_payload.get("send_context_guard")
                        if isinstance(sidecar_payload.get("send_context_guard"), dict)
                        else {}
                    ),
                    "send_identity_frame": self._send_identity_frame(
                        sidecar_payload
                    ),
                }
            self._stage_payload_ledger(payload)
            phase_started_at = time.perf_counter()
            outbox_id = f"c2-outbox:{payload.get('read_run_id')}"
            for image_message in _c2_image_messages(payload):
                append_log(
                    "INFO",
                    "c2_image_outbox_staged",
                    "C2 图片结构化结果已进入 Outbox；重试只重传 JSON，不重复操作微信或 Vision。",
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "read_run_id": payload.get("read_run_id"),
                        "outbox_id": outbox_id,
                        "source_message_key": image_message.get("source_message_key"),
                        "dedupe_key": image_message.get("dedupe_key"),
                        "image_persisted": False,
                    },
                )
            delivery = self._submit_c2_outbox_payload(
                binding=binding,
                payload=payload,
                operation="message_ingest",
            )
            outbox_id = str(delivery["outbox_id"])
            if not delivery.get("ok"):
                exc = delivery.get("exception")
                error_code = str(delivery.get("error_code") or "")
                for image_message in _c2_image_messages(payload):
                    append_log(
                        "WARN",
                        "c2_image_ingest_failed",
                        "C2 图片结构化结果尚未得到后端确认，保留 Outbox 等待原样重传。",
                        error_code=str(error_code),
                        metadata={
                            "conversation_id": target.conversation_id,
                            "remark_code": target.remark_code,
                            "read_run_id": payload.get("read_run_id"),
                            "outbox_id": outbox_id,
                            "source_message_key": image_message.get("source_message_key"),
                            "dedupe_key": image_message.get("dedupe_key"),
                            "error_type": type(exc).__name__ if isinstance(exc, Exception) else "",
                            "image_persisted": False,
                        },
                    )
                if isinstance(exc, Exception):
                    raise exc
                raise RuntimeError(error_code or "C2_OUTBOX_SUBMIT_FAILED")
            result = delivery.get("result") if isinstance(delivery.get("result"), dict) else {}
            ingest_results = result.get("results") if isinstance(result, dict) and isinstance(result.get("results"), list) else []
            for image_message in _c2_image_messages(payload):
                source_key = str(image_message.get("source_message_key") or "")
                dedupe_key = str(image_message.get("dedupe_key") or "")
                matched = next(
                    (
                        item
                        for item in ingest_results
                        if isinstance(item, dict)
                        and (
                            (source_key and str(item.get("source_message_key") or "") == source_key)
                            or (dedupe_key and str(item.get("dedupe_key") or "") == dedupe_key)
                        )
                    ),
                    {},
                )
                ingest_result = str(matched.get("ingest_result") or "")
                result_error = str(matched.get("error_code") or "")
                append_log(
                    "WARN" if ingest_result == "ignored" or result_error else "INFO",
                    "c2_image_ingest_finished",
                    "C2 图片消息后端入库结果已确认。",
                    error_code=result_error or None,
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "read_run_id": payload.get("read_run_id"),
                        "outbox_id": outbox_id,
                        "source_message_key": source_key,
                        "dedupe_key": dedupe_key,
                        "ingest_result": ingest_result or "batch_confirmed",
                        "image_persisted": False,
                    },
                )
            record_phase(
                "messages_ingest_api",
                phase_started_at,
                ingested_count=result.get("ingested_count") if isinstance(result, dict) else None,
                ignored_count=result.get("ignored_count") if isinstance(result, dict) else None,
            )
            customer_keys = {
                item.get("dedupe_key")
                for item in payload.get("messages") or []
                if isinstance(item, dict) and item.get("sender_role_hint") == "customer" and item.get("dedupe_key")
            }
            new_customer_message_count = sum(
                1
                for item in (result.get("results") or [])
                if isinstance(item, dict) and item.get("dedupe_key") in customer_keys and item.get("ingest_result") == "ingested"
            )
            if not result.get("results") and customer_keys and int(result.get("ingested_count") or 0) > 0:
                new_customer_message_count = int(result.get("ingested_count") or 0)
            self_keys = {
                item.get("dedupe_key")
                for item in payload.get("messages") or []
                if isinstance(item, dict) and item.get("sender_role_hint") == "self" and item.get("dedupe_key")
            }
            new_self_message_count = sum(
                1
                for item in (result.get("results") or [])
                if isinstance(item, dict) and item.get("dedupe_key") in self_keys and item.get("ingest_result") == "ingested"
            )
            ignored_results = [
                item
                for item in (result.get("results") or [])
                if isinstance(item, dict) and item.get("ingest_result") == "ignored"
            ]
            ingest_error_code = ""
            if local_validation_errors:
                ingest_error_code = str(local_validation_errors[0].get("error_code") or "C2_OBSERVATION_CONTRACT_INVALID")
            elif ignored_results:
                ingest_error_code = str(ignored_results[0].get("error_code") or "C2_MESSAGE_INGEST_IGNORED")
            elif int(result.get("ignored_count") or 0) > 0:
                ingest_error_code = "C2_MESSAGE_INGEST_IGNORED"
            self.c2_stats.update(
                {
                    "last_message_read_at": (payload.get("evidence") or {}).get("finished_at") if isinstance(payload.get("evidence"), dict) else None,
                    "last_ingested_count": result.get("ingested_count") if isinstance(result, dict) else 0,
                    "last_error": ingest_error_code or None,
                }
            )
            append_log(
                "WARN" if ingest_error_code else "INFO",
                "c2_messages_ingest_partial_failure" if ingest_error_code else "c2_messages_ingested",
                "微信消息存在合同校验失败或被后端忽略。" if ingest_error_code else "微信消息读取结果已上报。",
                error_code=ingest_error_code or None,
                metadata={
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "message_count": len(payload.get("messages") or []),
                    "ingested_count": self.c2_stats["last_ingested_count"],
                    "ignored_count": int(result.get("ignored_count") or 0),
                    "local_validation_errors": local_validation_errors,
                    "ignored_results": ignored_results,
                },
            )
            read_completion = (
                result.get("read_completion")
                if isinstance(result.get("read_completion"), dict)
                else {}
            )
            if read_completion:
                append_log(
                    "INFO",
                    "c2_read_completion_confirmed",
                    "后端已确认本次完整读取结果和下次允许读取时间。",
                    metadata={
                        "conversation_id": target.conversation_id,
                        "read_reason": target.read_reason,
                        "read_result": read_completion.get("result"),
                        "no_change_read_count": read_completion.get(
                            "no_change_read_count"
                        ),
                        "next_read_due_at": read_completion.get(
                            "next_read_due_at"
                        ),
                    },
                )
            brain_result = None
            message_batch = result.get("message_batch") if isinstance(result, dict) else None
            if wait_for_brain and isinstance(message_batch, dict) and message_batch.get("batch_id"):
                if not self._apply_ingest_batch_continuation_to_target(
                    message_batch,
                    target,
                ):
                    error_code = "C3_BATCH_CONTINUATION_INVALID"
                    append_log(
                        "ERROR",
                        "c3_batch_continuation_invalid",
                        "消息已入库但批次续行票缺失或与原读取授权冲突；已暂停接单，禁止释放锁后恢复扫描。",
                        error_code=error_code,
                        metadata={
                            "conversation_id": target.conversation_id,
                            "remark_code": target.remark_code,
                            "batch_id": message_batch.get("batch_id"),
                            "authorization_revision": target.authorization_revision,
                            "authorization_read_reason": (
                                target.raw.get("authorization_read_reason")
                                if isinstance(target.raw, dict)
                                else target.read_reason
                            ),
                        },
                        force_incident=True,
                    )
                    self.set_run_status("paused")
                    brain_result = {
                        "ok": False,
                        "error_code": error_code,
                        "batch_id": str(message_batch.get("batch_id") or ""),
                    }
                else:
                    try:
                        brain_result = self._wait_and_send_current_c3_batch(
                            binding=binding,
                            target=target,
                            batch_id=str(message_batch["batch_id"]),
                            cancel_check=action_cancel_requested,
                        )
                    finally:
                        self._stop_task_lease_guard()
            fact_ingest_ok = not bool(ingest_error_code)
            conversation_flow_ok, conversation_terminal_state, flow_error_code = (
                self._conversation_flow_outcome(
                    brain_result,
                    had_message_batch=bool(
                        isinstance(message_batch, dict)
                        and message_batch.get("batch_id")
                        and wait_for_brain
                    ),
                )
            )
            final_error_code = ingest_error_code or flow_error_code
            if flow_error_code:
                self.c2_stats["last_error"] = flow_error_code
                append_log(
                    "WARN",
                    "c2_conversation_flow_failed",
                    "消息事实已处理，但 Brain、任务领取或回复发送流程未正常结束。",
                    error_code=flow_error_code,
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "conversation_terminal_state": conversation_terminal_state,
                        "brain_result": brain_result,
                    },
                )
            return {
                "ok": bool(fact_ingest_ok and conversation_flow_ok),
                "error_code": final_error_code or None,
                "result": result,
                "payload": payload,
                "new_customer_message_count": new_customer_message_count,
                "new_self_message_count": new_self_message_count,
                "brain_result": brain_result,
                "fact_ingest_ok": fact_ingest_ok,
                "conversation_flow_ok": conversation_flow_ok,
                "conversation_terminal_state": conversation_terminal_state,
                "send_context_guard": (
                    sidecar_payload.get("send_context_guard")
                    if isinstance(sidecar_payload.get("send_context_guard"), dict)
                    else {}
                ),
                "send_identity_frame": self._send_identity_frame(
                    sidecar_payload
                ),
            }
        except UiLockError as exc:
            code = "voice_transcribe_lock_timeout" if exc.code in {"UI_LOCK_BUSY", "UI_LOCK_ACQUIRE_TIMEOUT"} else exc.code
            self.c2_stats["last_error"] = code
            append_log("INFO", "c2_message_read_interrupted", "C2 消息读取未拿到锁，按低优先级中断处理。", error_code=code, metadata={"ui_lock": exc.data, "lock_error_code": exc.code})
            return {"ok": False, "error_code": code}
        except ApiError as exc:
            self.c2_stats["last_error"] = exc.code
            append_log("WARN", "c2_messages_ingest_rejected", str(exc), error_code=exc.code, metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code})
            return {"ok": False, "error_code": exc.code, "api_error": exc.data}
        except Exception as exc:
            self.c2_stats["last_error"] = str(exc)
            append_log("ERROR", "c2_message_read_failed", str(exc), metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code})
            return {"ok": False, "error_code": "MESSAGE_READ_FAILED", "message": str(exc)}
        finally:
            if lease and owns_lease:
                self._release_current_ui_lock(reason="message_ingest_finished")
            flow_timing["total_duration_seconds"] = round(max(0.0, time.perf_counter() - flow_started_at), 4)
            process_run_id = load_process_run(read_run_id)
            if process_run_id and operation_phase != C2_PRE_SEND_REFRESH_PHASE:
                enqueue_c2_flow_timing_stages(
                    process_run_id=process_run_id,
                    conversation_id=target.conversation_id,
                    read_run_id=read_run_id,
                    flow_timing=flow_timing,
                    trace_id=str(
                        sidecar_payload.get("sidecar_run_id")
                        if isinstance(locals().get("sidecar_payload"), dict)
                        else ""
                    )
                    or None,
                )
                schedule_stage_event_upload(self.api, binding)
            try:
                append_log(
                    "INFO",
                    "c2_message_read_timing",
                    "C2 消息读取耗时账本。",
                    metadata=flow_timing,
                )
            except Exception:
                pass
            self.current_step = None
