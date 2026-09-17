"""Pure, lossless packing of Brain-authored semantic units.

This module does not write or shorten a reply. An oversized semantic unit must
go back to Brain; cutting at an arbitrary character can separate a condition
from a promise, an amount from its unit, or a negation from its predicate.
"""

from functools import lru_cache


@lru_cache(maxsize=1)
def _interruption_rules():
    if __package__:
        from . import send_interruption
        return send_interruption
    # The backend's existing facade loads adapters by file, including custom
    # source roots. Resolve this dependency beside that exact loaded file.
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reply_sequence_send_interruption", Path(__file__).with_name("send_interruption.py")
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("send_interruption rule unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    """Keep the sequence API; the factual cancellation rule has one owner."""
    rules = _interruption_rules()
    _object = rules._object

    target = _object(_object(evidence).get("guard")).get("confirmed_target")
    proof = rules.customer_interruption_proof(
        send_result="failed", action_phase=action_phase, error_code=error_code,
        evidence=evidence, target=target,
    )
    if proof is None:
        return None
    snapshot, check = proof["snapshot"], proof["check"]
    current = _object(snapshot.get("send_context_guard"))
    frame = _object(snapshot.get("frame_observation"))
    if not (
        current.get("ok") is True and current.get("tail_complete") is True
        and isinstance(frame.get("frame_id"), str) and frame["frame_id"].strip()
        and (proof["before_input"] or (
            frame["frame_id"] == _object(check.get("frame_observation")).get("frame_id")
            and proof["cleanup"].get("reason") == "confirmed_program_draft_cleared"
        ))
    ):
        return None
    rows, sequence = snapshot.get("message_sequence"), current.get("sequence")
    if not isinstance(rows, list) or len(rows) != len(sequence):
        return None
    ids = []
    for index in proof["suffix"]:
        row = _object(rows[index])
        if (row.get("sender_role") != "customer"
                or not isinstance(row.get("observation_id"), str)
                or not row["observation_id"].strip()):
            return None
        ids.append(row["observation_id"])
    if len(set(ids)) != len(ids):
        return None
    return {"frame_id": frame["frame_id"], "observation_ids": ids}
