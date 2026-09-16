"""Pure, lossless packing of Brain-authored semantic units.

This module does not write or shorten a reply. An oversized semantic unit must
go back to Brain; cutting at an arbitrary character can separate a condition
from a promise, an amount from its unit, or a negation from its predicate.
"""


def pack_reply_sequence(text: str, candidates: object, *, max_chars: int, max_segments: int) -> list[str]:
    if max_chars < 1 or not 1 <= max_segments <= 3:
        raise ValueError("REPLY_SEQUENCE_POLICY_INVALID")
    if not text or text != " ".join(text.split()):
        raise ValueError("REPLY_SEQUENCE_TEXT_INVALID")
    # Count every character actually sent, including spaces and punctuation.
    # The existing canonical send contract converts line breaks to spaces.
    if len(text) <= max_chars:
        return [text]
    if not isinstance(candidates, list) or not all(isinstance(item, str) for item in candidates):
        raise ValueError("REPLY_SEQUENCE_REWRITE_REQUIRED")
    units = [" ".join(item.split()) for item in candidates if item.strip()]
    if not units or " ".join(units) != text or any(len(unit) > max_chars for unit in units):
        raise ValueError("REPLY_SEQUENCE_REWRITE_REQUIRED")
    packed: list[str] = []
    for unit in units:
        if packed and len(packed[-1]) + 1 + len(unit) <= max_chars:
            packed[-1] += " " + unit
        else:
            packed.append(unit)
    if len(packed) > max_segments:
        raise ValueError("REPLY_SEQUENCE_REWRITE_REQUIRED")
    return packed


def reply_sequence_instruction(*, max_chars: int, max_segments: int) -> str:
    return (
        f"本次逐条发送：完整回复不超过{max_chars}字符时只发送一条；超过时最多{max_segments}条，"
        f"每条最多{max_chars}字符，标点、空格和换行均计入。reply_segments必须是能独立发送的完整语义单元，"
        "金额和单位、否定、条件和承诺不能拆开。保留全部必要信息，超限时自行精简重写，"
        "不得截断尾句或依赖后段纠正前段的承诺。所有文字仍须通过原有事实及安全审查。"
    )


def confirmed_customer_interruption(*, error_code: str, action_phase: str, evidence: object) -> dict | None:
    """A cancelled draft is not a failed physical send. Consume existing proof.

    A change before input needs no draft cleanup. This permits only
    cancellation and a new authorized read, never message
    ingestion or sending from the snapshot. Ambiguous changes retain the old
    failure handling. Worker and server use the same rule.
    """
    def obj(value):
        return value if isinstance(value, dict) else {}

    if error_code != "C3_CONTEXT_CHANGED_BEFORE_SEND" or action_phase != "not_attempted":
        return None
    guard = obj(obj(evidence).get("guard"))
    visual = obj(guard.get("visual"))
    clear = obj(visual.get("draft_clear"))
    check = obj(visual.get("context_check"))
    snapshot = obj(check.get("snapshot"))
    before_input = obj(evidence).get("state") == "send_context_changed_before_input"
    if before_input:
        snapshot = obj(obj(evidence).get("send_baseline"))
        check = obj(obj(evidence).get("context_validation"))
        journal = obj(obj(evidence).get("action_journal"))
        safe_input_state = (
            journal.get("ok") is True and journal.get("action_phase") == "not_attempted"
            and obj(snapshot.get("input_region")).get("has_visible_text") is False
            and bool(snapshot.get("screenshot_path"))
            and guard.get("screenshot_path") == snapshot.get("screenshot_path")
        )
    else:
        safe_input_state = (
            visual.get("physical_send_triggered") is False
            and clear.get("ok") is True and clear.get("cleared") is True
            and clear.get("reason") == "confirmed_program_draft_cleared"
            and obj(clear.get("focus_check")).get("ok") is True
        )
    decision = obj(check.get("worker_continuity_decision"))
    current = obj(snapshot.get("send_context_guard"))
    frame = obj(snapshot.get("frame_observation"))
    if not (
        guard.get("ok") is True
        and safe_input_state
        and snapshot.get("ok") is True and current.get("ok") is True
        and current.get("tail_complete") is True
        and decision.get("relation") in {"unique_tail_append", "unique_viewport_slide_with_tail_append"}
        and check.get("continuity_relation") == decision.get("relation")
        and check.get("error_code") == error_code
        and isinstance(frame.get("frame_id"), str) and frame["frame_id"].strip()
        and (before_input or frame.get("frame_id") == obj(check.get("frame_observation")).get("frame_id"))
        and current.get("sequence_sha256")
        and current.get("sequence_sha256") == check.get("current_sequence_sha256")
        and check.get("expected_sequence_sha256")
        and check.get("expected_sequence_sha256") != check.get("current_sequence_sha256")
    ):
        return None
    sequence = current.get("sequence")
    rows = snapshot.get("message_sequence")
    suffix = decision.get("new_suffix_indexes")
    if not (isinstance(sequence, list) and isinstance(rows, list)
            and len(rows) == len(sequence) == decision.get("new_count")
            and isinstance(suffix, list) and suffix and all(type(i) is int for i in suffix)
            and suffix == list(range(suffix[0], len(sequence))) and suffix[0] >= 0):
        return None
    ids = []
    for index in suffix:
        item, row = obj(sequence[index]), obj(rows[index])
        if (item.get("sender_role") != "customer" or row.get("sender_role") != "customer"
                or item.get("message_type") not in {"text", "voice", "image"}
                or not isinstance(row.get("observation_id"), str) or not row["observation_id"].strip()):
            return None
        ids.append(row["observation_id"])
    if len(set(ids)) != len(ids):
        return None
    return {"frame_id": frame["frame_id"], "observation_ids": ids}
