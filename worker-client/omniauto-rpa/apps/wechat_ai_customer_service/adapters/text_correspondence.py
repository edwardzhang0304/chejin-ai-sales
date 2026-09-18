"""Portable text evidence rules; no identity allocation, I/O, or reply decisions.

Eligibility here never proves that two observations are the same message.
The caller must still verify the unique overlap, anchors and original ownership.
Edit offsets refer to normalized Unicode codepoints; hashes use original UTF-8.
"""
from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable

HISTORICAL_POLICY_ID = "historical_context_v1"
MIN_HISTORICAL_TEXT_LENGTH = 10
MAX_HISTORICAL_EDIT_DISTANCE = 1
MIN_HISTORICAL_SIMILARITY = 0.90


def validate_entity_context(context: Any) -> tuple[str, ...]:
    """Missing and malformed differ from a valid empty authoritative list."""
    if (not isinstance(context, dict) or set(context) != {"version", "known_entities"}
            or type(context.get("version")) is not int or context["version"] != 1
            or not isinstance(context.get("known_entities"), list)):
        raise ValueError("TEXT_CORRESPONDENCE_CONTEXT_INVALID")
    pairs = []
    for entity in context["known_entities"]:
        if (not isinstance(entity, dict) or set(entity) != {"kind", "value"}
                or entity["kind"] not in {"person", "vehicle", "location"}
                or not isinstance(entity["value"], str) or not entity["value"]
                or entity["value"] != unicodedata.normalize("NFKC", entity["value"].strip()).casefold().strip()):
            raise ValueError("TEXT_CORRESPONDENCE_CONTEXT_INVALID")
        pairs.append((entity["kind"], entity["value"]))
    if pairs != sorted(set(pairs)):
        raise ValueError("TEXT_CORRESPONDENCE_CONTEXT_INVALID")
    return tuple(value for _, value in pairs)


def checkpoint_digest(checkpoint: dict) -> str:
    """Bind correspondence to the entire authoritative identity/context view."""
    view = {key: value for key, value in checkpoint.items() if key != "checkpoint_digest"}
    return hashlib.sha256(json.dumps(view, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()

_OCR_PUNCTUATION_TRANSLATION = str.maketrans({
    "。": ".", "｡": ".", "、": ",", "､": ",", "“": '"', "”": '"', "„": '"', "‟": '"',
    "「": '"', "」": '"', "『": '"', "』": '"', "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "—": "-", "–": "-", "―": "-", "−": "-", "‐": "-", "‑": "-", "…": "...", "‥": "..",
    "【": "[", "】": "]", "〔": "[", "〕": "]",
})


def normalized_projection_text(value: Any) -> str:
    """Existing OCR presentation normalization, unchanged before hashing."""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = normalized.translate(_OCR_PUNCTUATION_TRANSLATION)
    normalized = "".join(character for character in normalized
        if not character.isspace() and unicodedata.category(character) != "Cf"
        and ord(character) not in {0xFE0E, 0xFE0F})
    return re.sub(r"\.{2,}", "...", normalized).casefold()


def business_comparison_text(value: Any) -> str:
    return "".join(c for c in normalized_projection_text(value)
                   if not unicodedata.category(c).startswith("P"))


def field_value_text(value: str) -> str:
    """D3 actual field values: preserve internal spaces and all punctuation."""
    return unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n").strip()


def single_edit(old: str, new: str) -> dict[str, Any] | None:
    """Return the sole edit or None for exact / more than one edit.

    Linear in text length; never a permissive approximate-match scorer.
    """
    if old == new or abs(len(old) - len(new)) > MAX_HISTORICAL_EDIT_DISTANCE:
        return None
    index = 0
    while index < min(len(old), len(new)) and old[index] == new[index]:
        index += 1
    if len(old) == len(new):
        if old[index + 1:] != new[index + 1:]:
            return None
        operation, old_text, new_text = "replace", old[index:index + 1], new[index:index + 1]
    elif len(old) > len(new):
        if old[index + 1:] != new[index:]:
            return None
        operation, old_text, new_text = "delete", old[index:index + 1], ""
    else:
        if old[index:] != new[index + 1:]:
            return None
        operation, old_text, new_text = "insert", "", new[index:index + 1]
    return {"op": operation, "old_offset": index, "new_offset": index,
            "old_text": old_text, "new_text": new_text}


_PROTECTED_WORDS = (
    "不", "没", "无", "未", "别", "勿", "非", "要", "想", "愿意", "可以", "能", "行", "是", "有",
    "保证", "肯定", "一定", "必须", "只", "至少", "最多", "以内", "以上", "以下", "左右", "前", "后",
)
_NUMERIC_IDENTIFIER = re.compile(r"[+\-]?[a-z0-9]+(?:[.:/\-_][a-z0-9]+)*", re.IGNORECASE)
_CHINESE_QUANTITY = re.compile(
    r"[零〇一二两三四五六七八九十百千万亿点]+(?:公里|千米|万元|元|年|座|米|升|吨|个月|月|天|小时|分钟|秒|万|亿)"
)


def protected_spans(value: str, entities: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Find protected content BEFORE punctuation removal (8.8 differs from 88)."""
    text = normalized_projection_text(value)
    candidates = []
    for kind, pattern in (("identifier", _NUMERIC_IDENTIFIER), ("quantity", _CHINESE_QUANTITY)):
        candidates.extend((m.start(), m.end(), kind, m.group()) for m in pattern.finditer(text))
    for kind, words in (("polarity", _PROTECTED_WORDS), ("entity", entities)):
        for word in sorted({normalized_projection_text(w) for w in words if w}, key=lambda s: (-len(s), s)):
            if not word:
                continue
            start = text.find(word)
            while start >= 0:
                candidates.append((start, start + len(word), kind, word))
                start = text.find(word, start + 1)
    # Longest first within the same category, retaining intersecting categories.
    chosen = []
    for candidate in sorted(candidates, key=lambda s: (-(s[1] - s[0]), s[0], s[2])):
        start, end, kind, token = candidate
        if any(k == kind and left <= start and end <= right for left, right, k, _ in chosen):
            continue
        chosen.append(candidate)
    offsets = [0]
    for character in text:
        offsets.append(offsets[-1] + (not unicodedata.category(character).startswith("P")))
    return [{"kind": kind, "text": token, "start": offsets[start], "end": offsets[end]}
            for start, end, kind, token in sorted(chosen)]


def _touches_edit(spans: list[dict], edit: dict, side: str) -> bool:
    start = edit[f"{side}_offset"]
    end = start + len(edit[f"{side}_text"])
    return any((span['start'] < start < span['end']) if start == end
               else (start < span['end'] and end > span['start']) for span in spans)


def historical_text_candidate(old: str, new: str, *, protected_entities: Iterable[str] = ()) -> dict[str, Any]:
    """Assess one old-text candidate, without authorizing message identity.

    Protected entities are the finite authoritative checkpoint list. Unknown
    names are an explicitly accepted residual risk, not an extra veto.
    """
    old_text, new_text = business_comparison_text(old), business_comparison_text(new)
    entities = tuple(protected_entities)
    left, right = protected_spans(old, entities), protected_spans(new, entities)
    conflict = Counter((s['kind'], s['text']) for s in left) != Counter((s['kind'], s['text']) for s in right)
    result = {"eligible": False, "policy_id": HISTORICAL_POLICY_ID, "matched_by": "none",
              "canonical_text_sha256": hashlib.sha256(old.encode('utf-8')).hexdigest(),
              "observed_text_sha256": hashlib.sha256(new.encode('utf-8')).hexdigest(),
              "edit_distance": None, "similarity": None, "edits": [], "protected_conflict": conflict}
    if conflict:
        return {**result, "reason": "protected_content_changed"}
    if old_text == new_text:
        return {**result, "eligible": True, "matched_by": "exact", "edit_distance": 0,
                "similarity": 1.0, "reason": "normalized_text_equal"}
    length = max(len(old_text), len(new_text))
    if length < MIN_HISTORICAL_TEXT_LENGTH:
        return {**result, "reason": "text_too_short"}
    edit = single_edit(old_text, new_text)
    if edit is None:
        return {**result, "reason": "more_than_one_edit"}
    result.update(edit_distance=1, similarity=1 - 1 / length, edits=[edit])
    if _touches_edit(left, edit, "old") or _touches_edit(right, edit, "new"):
        return {**result, "protected_conflict": True, "reason": "edit_touches_protected_content"}
    if result['similarity'] < MIN_HISTORICAL_SIMILARITY:
        return {**result, "reason": "similarity_below_policy"}
    return {**result, "eligible": True, "matched_by": "context_ocr", "reason": "single_unprotected_edit"}


def normalized_send_confirmation_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


_SEND_OCR_PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "。": ".",
        "｡": ".",
        "、": ",",
        "､": ",",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "—": "-",
        "–": "-",
        "―": "-",
        "−": "-",
        "‐": "-",
        "‑": "-",
        "…": "...",
        "‥": "..",
        "【": "[",
        "】": "]",
        "〔": "[",
        "〕": "]",
    }
)
SEND_OCR_MIN_EXPECTED_COVERAGE = 0.80
SEND_OCR_MIN_OBSERVED_COVERAGE = 0.80
SEND_OCR_MIN_SIMILARITY = 0.80
SEND_OCR_MIN_MATCHING_CHARACTERS = 4
SEND_OCR_MAX_REQUIRED_CONTIGUOUS_MATCH = 8


def _normalized_send_ocr_correspondence_text(value: Any) -> str:
    """Canonicalize OCR presentation differences without rewriting content."""

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = normalized.translate(_SEND_OCR_PUNCTUATION_TRANSLATION)
    normalized = "".join(
        character
        for character in normalized
        if not character.isspace()
        and unicodedata.category(character) != "Cf"
        and ord(character) not in {0xFE0E, 0xFE0F}
    )
    normalized = re.sub(r"\.{2,}", "...", normalized)
    return normalized.casefold()


def _send_ocr_has_readable_text(value: Any) -> bool:
    normalized = _normalized_send_ocr_correspondence_text(value)
    return bool(re.search(r"[0-9a-z\u3400-\u9fff]", normalized))


def _send_ocr_text_correspondence(
    expected_text: Any,
    observed_text: Any,
) -> dict[str, Any]:
    """Correlate OCR text with the just-triggered AI reply.

    Message type and send ownership are deliberately separate decisions. A
    readable self-side OCR result is text even when this correspondence check
    fails. Send ownership additionally requires high ordered overlap in both
    directions so a short shared phrase inside unrelated text cannot confirm
    a send.
    """

    raw_expected = normalized_send_confirmation_text(expected_text)
    raw_observed = normalized_send_confirmation_text(observed_text)
    expected = _normalized_send_ocr_correspondence_text(expected_text)
    observed = _normalized_send_ocr_correspondence_text(observed_text)
    matcher = SequenceMatcher(None, expected, observed, autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size > 0]
    matching_characters = sum(block.size for block in blocks)
    longest_matching_block = max((block.size for block in blocks), default=0)
    expected_coverage = (
        matching_characters / len(expected) if expected else 0.0
    )
    observed_coverage = (
        matching_characters / len(observed) if observed else 0.0
    )
    similarity = matcher.ratio() if expected and observed else 0.0
    normalized_exact = bool(expected and observed and expected == observed)
    min_comparable_length = min(len(expected), len(observed))
    required_contiguous_match = min(
        SEND_OCR_MAX_REQUIRED_CONTIGUOUS_MATCH,
        max(SEND_OCR_MIN_MATCHING_CHARACTERS, int(min_comparable_length * 0.20)),
    )
    high_overlap = bool(
        expected
        and observed
        and matching_characters >= SEND_OCR_MIN_MATCHING_CHARACTERS
        and longest_matching_block >= required_contiguous_match
        and expected_coverage >= SEND_OCR_MIN_EXPECTED_COVERAGE
        and observed_coverage >= SEND_OCR_MIN_OBSERVED_COVERAGE
        and similarity >= SEND_OCR_MIN_SIMILARITY
    )
    accepted = bool(normalized_exact or high_overlap)
    if normalized_exact and raw_expected == raw_observed:
        reason = "exact_program_text"
    elif normalized_exact:
        reason = "unicode_normalized_exact_program_text"
    elif high_overlap:
        reason = "high_overlap_program_text"
    elif not expected or not observed:
        reason = "ocr_text_empty"
    else:
        reason = "ocr_text_low_overlap"
    result: dict[str, Any] = {
        "accepted": accepted,
        "exact": normalized_exact,
        "raw_exact": bool(raw_expected and raw_expected == raw_observed),
        "reason": reason,
        "expected_length": len(expected),
        "observed_length": len(observed),
        "matching_characters": matching_characters,
        "longest_matching_block": longest_matching_block,
        "required_contiguous_match": required_contiguous_match,
        "expected_coverage": round(expected_coverage, 6),
        "observed_coverage": round(observed_coverage, 6),
        "similarity": round(similarity, 6),
    }
    return result



_SEND_CHAT_KINDS = {'text_bubble', 'voice_bubble', 'voice_transcript', 'image_bubble'}


def find_new_matching_self_message(baseline_sequence, current_sequence, text):
    """Unique suffix/prefix baseline plus exactly one newly added self bubble.

    A customer's response after that bubble does not undo the physical send.
    This rule grants no permission to send and allocates no message identity.
    """
    def chats(sequence):
        return [item for item in sequence if isinstance(item,dict) and item.get('row_kind') in _SEND_CHAT_KINDS]
    def signature(item):
        return (item.get('row_kind'),item.get('sender_role'),
                _normalized_send_ocr_correspondence_text(item.get('content_normalized')))
    before,after=chats(baseline_sequence),chats(current_sequence)
    old,new=list(map(signature,before)),list(map(signature,after))
    overlaps=[count for count in range(1,min(len(old),len(new))+1) if old[-count:]==new[:count]] if before else [0]
    if len(overlaps)!=1:
        return None
    offset=overlaps[0]
    suffix=after[offset:]
    # Unknown role or a second outgoing bubble could be a simultaneous manual
    # action. Neither is silently attributed to this program send.
    own=[(offset+i,row) for i,row in enumerate(suffix) if row.get('sender_role') in {'self','sales'}]
    if len(own)!=1 or any(row.get('sender_role') not in {'self','sales','customer'} for row in suffix):
        return None
    index,candidate=own[0]
    status = candidate.get('send_status_evidence')
    if status is not None and (not isinstance(status, dict) or status.get('state') != 'clear'):
        return None
    correspondence=_send_ocr_text_correspondence(text,candidate.get('content_normalized'))
    if candidate.get('row_kind')!='text_bubble' or not correspondence['accepted']:
        return None
    structural=str(candidate.get('recovered_from_structural_observation_id') or '')
    if structural and structural in {str(row.get('observation_id') or '') for row in before}:
        return None
    following=[row for row in after[index+1:] if row.get('sender_role')=='customer']
    return {**candidate,'send_text_correspondence':correspondence,
            'following_customer_observation_ids':[str(row.get('observation_id') or '') for row in following]}


def confirmed_post_send_customer_suffix(evidence, *, target, text):
    """Recompute an observation-only reread signal from a confirmed receipt."""
    def obj(value):return value if isinstance(value,dict) else {}
    send=obj(obj(evidence).get('send_result'))
    baseline=obj(send.get('send_baseline'))
    confirmation=obj(send.get('sent_confirmation'))
    snapshot=obj(confirmation.get('snapshot'))
    if (send.get('confirmed') is not True or send.get('result')!='sent'
            or confirmation.get('ok') is not True or baseline.get('ok') is not True
            or snapshot.get('ok') is not True
            or obj(snapshot.get('input_region')).get('has_visible_text') is not False
            or not target or obj(baseline.get('validation')).get('confirmed_target')!=target
            or obj(snapshot.get('validation')).get('confirmed_target')!=target):
        return None
    pre=str(obj(baseline.get('frame_observation')).get('frame_id') or '')
    post=str(obj(snapshot.get('frame_observation')).get('frame_id') or '')
    if not pre or not post or pre==post:
        return None
    before,current=baseline.get('message_sequence'),snapshot.get('message_sequence')
    if not isinstance(before,list) or not isinstance(current,list):
        return None
    found=find_new_matching_self_message(before,current,text)
    if not found or found.get('observation_id')!=obj(confirmation.get('confirmed_observation')).get('observation_id'):
        return None
    ids=found['following_customer_observation_ids']
    if not ids or not all(ids) or len(set(ids))!=len(ids):
        return None
    observations=snapshot.get('observations')
    if not isinstance(observations,list):
        return None
    for oid in ids:
        matches=[row for row in observations if isinstance(row,dict) and row.get('observation_id')==oid]
        if len(matches)!=1 or matches[0].get('sender_role')!='customer' or matches[0].get('row_kind') not in _SEND_CHAT_KINDS:
            return None
    return {'version':1,'frame_id':post,'observation_ids':ids,'confirmed_observation_id':found['observation_id']}
