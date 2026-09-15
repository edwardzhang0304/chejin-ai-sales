"""Select a place to re-observe; this module never accepts message identity."""
from __future__ import annotations

from typing import Any

from .message_viewport_projection import (
    normalized_business_message_sequence, ordered_message_viewport_observations,
)


def differing_text_observation_ids(old: list[dict[str, Any]], payload: dict[str, Any]) -> list[str]:
    rows = ordered_message_viewport_observations(payload.get("observations"))
    new = normalized_business_message_sequence(rows, message_viewport_bounds=None)
    if (not old or not new or payload.get("ok") is not True
            or payload.get("observation_validation_errors") or payload.get("flow_gate_errors")
            or payload.get("ui_frame_invalidated") or payload.get("history_gap")
            or not payload.get("frame_observation")):
        return []
    proposals = []
    # Exact neighboring text facts locate a possible overlap. They authorize
    # only a fresh observation. The original strict comparator remains owner
    # of continuity, including actual 8/9 or affirmative/negative differences.
    for offset in range(len(old)):
        count = len(old)-offset
        if count > len(new):
            continue
        mismatch, matches = [], 0
        for index, (before, after) in enumerate(zip(old[offset:], new)):
            if any(before.get(key) != after.get(key) for key in ("sender_role", "message_type", "media_state")):
                break
            if before.get("normalized_content_signature") == after.get("normalized_content_signature"):
                matches += int(before.get("message_type") == "text")
            else:
                if before.get("message_type") != "text":
                    break
                mismatch.append(index)
        else:
            if matches >= 2 and mismatch:
                proposals.append(mismatch)
    if len(proposals) != 1:
        return []
    selected = [rows[index] for index in proposals[0]]
    if any(row.get("row_kind") != "text_bubble" or row.get("contract_errors")
           or row.get("sender_role") not in {"customer", "self"} for row in selected):
        return []
    ids = [str(row.get("observation_id") or "") for row in selected]
    return ids if all(ids) and len(set(ids)) == len(ids) else []
