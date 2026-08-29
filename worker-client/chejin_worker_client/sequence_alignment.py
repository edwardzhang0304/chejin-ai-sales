from __future__ import annotations

"""Build Worker context for one approved media action.

Cross-frame business continuity has exactly one implementation:
``message_viewport_projection.compare_business_viewport_continuity``.
This module must not align two frames or inherit durable identities.
"""

import hashlib
from typing import Any

from .message_contract import canonical_message_identity_text


IDENTITY_STATES = {
    "committed",
    "selected_action",
    "frame_local_unselected",
}


def require_selected_only_media_reservation(
    observations: list[Any],
    *,
    selected_observation_id: str = "",
) -> None:
    """Reject a reserved identity on every unselected media candidate."""

    selected_id = _token(selected_observation_id)
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        observation_id = _token(observation.get("observation_id"))
        if observation_id == selected_id:
            continue
        if _message_type(observation) not in {"voice", "image"}:
            continue
        stable_id = _token(observation.get("_worker_stable_id"))
        identity_scope = _token(
            observation.get("_worker_identity_scope")
        )
        if identity_scope == "current_read_provisional" or (
            stable_id and identity_scope != "committed"
        ):
            raise ValueError("MESSAGE_IDENTITY_CONTRACT_INVALID")


def normalized_content_hash(value: Any) -> str:
    normalized = canonical_message_identity_text(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _token(value: Any) -> str:
    return str(value or "").strip()


def _message_type(item: dict[str, Any]) -> str:
    explicit = _token(item.get("message_type")).lower()
    if explicit in {"text", "voice", "image", "system"}:
        return explicit
    row_kind = _token(item.get("row_kind")).lower()
    return {
        "text_bubble": "text",
        "voice_bubble": "voice",
        "voice_transcript": "voice",
        "image_bubble": "image",
        "system_message": "system",
    }.get(row_kind, "")


def _source_value(item: dict[str, Any], key: str) -> str:
    direct = _token(item.get(key))
    if direct:
        return direct
    source = item.get("source_message")
    if isinstance(source, dict):
        nested = _token(source.get(key))
        if nested:
            return nested
    return ""


def _native_source_message_id(item: dict[str, Any]) -> str:
    explicit = _source_value(item, "native_source_message_id")
    if explicit:
        return explicit
    source = (
        item.get("source_message")
        if isinstance(item.get("source_message"), dict)
        else {}
    )
    adapter = _token(
        item.get("source_adapter") or source.get("source_adapter")
    ).lower()
    raw_id = _token(
        source.get("id")
        or source.get("message_id")
        or item.get("source_message_id")
    )
    if (
        raw_id
        and adapter not in {
            "win32_ocr",
            "wechat_win32_ocr",
            "rpa_ocr",
            "ocr_rpa",
        }
        and not raw_id.lower().startswith(
            (
                "win32_ocr:",
                "wechat_win32_ocr:",
                "ocr:",
                "screen_ocr:",
                "uia_ocr:",
            )
        )
    ):
        return raw_id
    return ""


def is_business_observation(item: Any) -> bool:
    return isinstance(item, dict) and _message_type(item) in {
        "text",
        "voice",
        "image",
        "system",
    }


def observation_sequence_item(
    observation: dict[str, Any],
    *,
    sequence_index: int,
) -> dict[str, Any]:
    message_type = _message_type(observation)
    content = observation.get("content_clean")
    if content is None:
        content = observation.get("content")
    action_summary = next(
        (
            observation.get(key)
            for key in (
                "_worker_voice_action_summary",
                "_worker_image_action_summary",
            )
            if isinstance(observation.get(key), dict)
        ),
        {},
    )
    prior_mapping = (
        action_summary.get("confirmed_action_mapping")
        if isinstance(action_summary.get("confirmed_action_mapping"), dict)
        else {}
    )
    return {
        "post_observation_id": _token(observation.get("observation_id")),
        "post_sequence_index": int(sequence_index),
        "sender_role": _token(observation.get("sender_role")).lower(),
        "message_type": message_type,
        "normalized_content_hash": normalized_content_hash(content),
        "native_source_message_id": _native_source_message_id(
            observation
        ),
        # Frame visual ids are diagnostic/click-local evidence only.  Keep
        # them in the sequence report so an incident can be reconstructed,
        # but never use them to establish or reject a cross-round identity.
        "frame_visual_id": _source_value(
            observation, "frame_visual_id"
        ),
        "voice_state": _token(observation.get("voice_state")).lower(),
        "worker_stable_id": (
            _token(observation.get("_worker_stable_id"))
            if _token(observation.get("_worker_identity_scope"))
            == "committed"
            else ""
        ),
        "prior_confirmed_action_id": _token(
            prior_mapping.get("canonical_action_id")
        ),
        "prior_confirmed_action_post_observation_id": _token(
            prior_mapping.get("post_observation_id")
        ),
        "prior_confirmed_action_binding": (
            prior_mapping.get("binding_confirmed") is True
        ),
        "prior_confirmed_action_image_sha256": _token(
            action_summary.get("image_sha256")
        ).lower(),
    }


def build_pre_action_identity_sequence(
    observations: list[Any],
    *,
    committed_ids: dict[str, str] | None = None,
    selected_observation_id: str = "",
    canonical_action_id: str = "",
    reserved_worker_stable_id: str = "",
) -> list[dict[str, Any]]:
    committed = {
        _token(key): _token(value)
        for key, value in (committed_ids or {}).items()
        if _token(key) and _token(value)
    }
    selected_id = _token(selected_observation_id)
    action_id = _token(canonical_action_id)
    reserved_id = _token(reserved_worker_stable_id)
    if selected_id and (not action_id or not reserved_id):
        raise ValueError("C2_SELECTED_ACTION_IDENTITY_MISSING")

    # v0.9.35: only the one media row selected for the current physical
    # action may carry a provisional Worker identity.
    require_selected_only_media_reservation(
        observations,
        selected_observation_id=selected_id,
    )
    selected_observation = next(
        (
            observation
            for observation in observations
            if isinstance(observation, dict)
            and _token(observation.get("observation_id")) == selected_id
        ),
        None,
    )
    if isinstance(selected_observation, dict):
        selected_stable_id = _token(
            selected_observation.get("_worker_stable_id")
        )
        selected_scope = _token(
            selected_observation.get("_worker_identity_scope")
        )
        if (
            selected_scope == "current_read_provisional"
            and selected_stable_id
            and selected_stable_id != reserved_id
        ):
            raise ValueError("MESSAGE_IDENTITY_CONTRACT_INVALID")

    sequence: list[dict[str, Any]] = []
    for observation in observations:
        if not is_business_observation(observation):
            continue
        observation_id = _token(observation.get("observation_id"))
        base = observation_sequence_item(
            observation,
            sequence_index=len(sequence),
        )
        item = {
            "pre_observation_id": observation_id,
            "pre_sequence_index": base.pop("post_sequence_index"),
            **{
                key: value
                for key, value in base.items()
                if key != "post_observation_id"
            },
        }
        if observation_id == selected_id:
            item.update(
                {
                    "identity_state": "selected_action",
                    "canonical_action_id": action_id,
                    "reserved_worker_stable_id": reserved_id,
                }
            )
        elif observation_id in committed:
            item.update(
                {
                    "identity_state": "committed",
                    "worker_stable_id": committed[observation_id],
                }
            )
        else:
            item["identity_state"] = "frame_local_unselected"
            # An unselected media row is only a current-frame candidate.  Do
            # not even serialize empty durable identity/action fields: their
            # presence has repeatedly been mistaken for a reservation or a
            # second identity proof by downstream code and tests.
            if item.get("message_type") in {"voice", "image"}:
                for field in (
                    "worker_stable_id",
                    "prior_confirmed_action_id",
                    "prior_confirmed_action_post_observation_id",
                    "prior_confirmed_action_binding",
                    "prior_confirmed_action_image_sha256",
                ):
                    item.pop(field, None)
        sequence.append(item)
    return sequence
