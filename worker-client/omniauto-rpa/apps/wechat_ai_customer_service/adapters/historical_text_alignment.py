"""D1 bounded correspondence proof; observation text and stored facts stay intact.

The caller still owns authorization, action/voice receipts and frame admission.
This module proves only the finite old-suffix/new-prefix correspondence.
"""
from collections import Counter
import math
import re

from .business_viewport_continuity import compare_business_viewport_continuity, boundary_tokens_for_observations
from .historical_text_correction import checkpoint_comparison
from .message_viewport_projection import normalized_business_message_sequence, ordered_message_viewport_observations
from .text_correspondence import checkpoint_digest, historical_text_candidate, validate_entity_context

VERSION = 1
POLICY = "historical_context_v1"
ACCEPTED_RELATIONS = {"business_sequence_equal", "unique_tail_append",
    "unique_viewport_slide_with_tail_append", "unique_history_suffix_without_new_messages"}


def comparison_entries(checkpoint):
    """Use the same committed sequence order as Worker, without mutating facts."""
    entries = [checkpoint_comparison(item) for item in checkpoint.get("recent_messages", [])]
    numbers = [re.fullmatch(r"worker-message-(\d+)", str(item.get("stable_id") or "")) for item in entries]
    if entries and all(numbers):
        entries = [item for _, item in sorted(zip((int(m.group(1)) for m in numbers), entries), key=lambda p: p[0])]
    return entries


def validate_proof_shape(proof):
    """Strict portable wire shape; authority and all scores are recomputed later."""
    def require(ok):
        if not ok:
            raise ValueError("TEXT_CORRESPONDENCE_PROOF_INVALID")
    def identity(value):
        return isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip()
    def index(value):
        return type(value) is int and 0 <= value < 500
    def sha(value):
        return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))
    require(isinstance(proof, dict) and set(proof) == {
        "version", "policy_id", "checkpoint_digest", "pre_frame_id", "post_frame_id", "pairs"})
    require(type(proof["version"]) is int and proof["version"] == VERSION and proof["policy_id"] == POLICY)
    require(sha(proof["checkpoint_digest"]) and identity(proof["pre_frame_id"]) and identity(proof["post_frame_id"]))
    require(isinstance(proof["pairs"], list) and 1 <= len(proof["pairs"]) <= 500)
    pair_fields = {"old_index", "new_index", "source_message_key", "observation_id", "canonical_text_sha256",
                   "observed_text_sha256", "edit_distance", "similarity", "edits", "exact_anchor_pairs",
                   "protected_conflict", "matched_by"}
    seen_old, seen_new, seen_sources, seen_observations = set(), set(), set(), set()
    for pair in proof["pairs"]:
        require(isinstance(pair, dict) and set(pair) == pair_fields)
        require(index(pair["old_index"]) and index(pair["new_index"])
                and identity(pair["source_message_key"]) and identity(pair["observation_id"]))
        for key, seen in (("old_index", seen_old), ("new_index", seen_new),
                          ("source_message_key", seen_sources), ("observation_id", seen_observations)):
            require(pair[key] not in seen)
            seen.add(pair[key])
        require(sha(pair["canonical_text_sha256"]) and sha(pair["observed_text_sha256"]))
        require(type(pair["edit_distance"]) is int and pair["edit_distance"] in {0, 1})
        score = pair["similarity"]
        require(type(score) in {int, float} and math.isfinite(score) and 0 <= score <= 1)
        require(pair["protected_conflict"] is False and pair["matched_by"] in {"exact", "context_ocr"})
        require(isinstance(pair["edits"], list) and len(pair["edits"]) <= 1)
        for edit in pair["edits"]:
            require(isinstance(edit, dict) and set(edit) == {"op", "old_offset", "new_offset", "old_text", "new_text"})
            require(edit["op"] in {"insert", "delete", "replace"}
                    and type(edit["old_offset"]) is int and edit["old_offset"] >= 0
                    and type(edit["new_offset"]) is int and edit["new_offset"] >= 0
                    and isinstance(edit["old_text"], str) and len(edit["old_text"]) <= 1
                    and isinstance(edit["new_text"], str) and len(edit["new_text"]) <= 1)
        require(isinstance(pair["exact_anchor_pairs"], list) and 2 <= len(pair["exact_anchor_pairs"]) <= 500)
        for anchor in pair["exact_anchor_pairs"]:
            require(isinstance(anchor, dict) and set(anchor) == {"old_index", "new_index", "source_message_key", "observation_id"})
            require(index(anchor["old_index"]) and index(anchor["new_index"])
                    and identity(anchor["source_message_key"]) and identity(anchor["observation_id"]))
    return proof


def _same_kind(left, right):
    return all(left.get(key) == right.get(key) for key in ("sender_role", "message_type", "media_state"))


def build_correspondence(checkpoint, observations, *, pre_frame_id, post_frame_id,
                         new_boundary_tokens, enabled=True):
    """Return None for an unsupported/insufficient candidate; reject bad context."""
    if not enabled or 'text_correspondence_context' not in checkpoint:
        return None
    entities = validate_entity_context(checkpoint['text_correspondence_context'])
    if checkpoint.get('checkpoint_digest') != checkpoint_digest(checkpoint):
        raise ValueError('TEXT_CORRESPONDENCE_CHECKPOINT_INVALID')
    if not pre_frame_id or not post_frame_id:
        raise ValueError('TEXT_CORRESPONDENCE_FRAME_MISSING')
    entries = comparison_entries(checkpoint)
    if not entries or any(not item.get('source_message_key') or not item.get('stable_id') for item in entries):
        return None
    if len({item['source_message_key'] for item in entries}) != len(entries):
        return None
    old = [item.get('business_projection') or {} for item in entries]
    rows = ordered_message_viewport_observations(observations)
    new = normalized_business_message_sequence(rows, message_viewport_bounds=None)
    if (not new or len({row.get('observation_id') for row in rows}) != len(rows)
            or any(not row.get('observation_id') or row.get('contract_errors') for row in rows)):
        return None
    old_tokens = {i: set(item.get('strong_boundary_tokens') or []) for i, item in enumerate(entries)}
    new_tokens = {i: set(tokens) for i, tokens in new_boundary_tokens.items()}
    old_counts = Counter(token for tokens in old_tokens.values() for token in tokens)
    new_counts = Counter(token for tokens in new_tokens.values() for token in tokens)
    candidates = []
    for start in range(len(old)):
        size = len(old) - start
        if size < 3 or size > len(new):
            continue
        pairs, fuzzy, anchors = [], [], []
        for post_index, before in enumerate(old[start:]):
            old_index = start + post_index
            after, entry, observed = new[post_index], entries[old_index], rows[post_index]
            if not _same_kind(before, after):
                break
            kind = before.get('message_type')
            effective = entry.get('effective_text') or {}
            canonical, current = effective.get('text'), observed.get('content_clean')
            text_capable = kind == 'text' or (kind == 'voice' and before.get('media_state') == 'transcribed')
            if not text_capable or not isinstance(canonical, str) or not isinstance(current, str):
                if before.get('normalized_content_signature') != after.get('normalized_content_signature'):
                    break
                continue
            decision = historical_text_candidate(canonical, current, protected_entities=entities)
            if not decision['eligible']:
                break
            pair = {key: decision[key] for key in ('canonical_text_sha256', 'observed_text_sha256',
                'edit_distance', 'similarity', 'edits', 'protected_conflict', 'matched_by')}
            pair.update(old_index=old_index, new_index=post_index,
                source_message_key=entry['source_message_key'], observation_id=observed['observation_id'])
            pairs.append(pair)
            if decision['matched_by'] == 'context_ocr':
                fuzzy.append(pair)
            elif kind == 'text':
                anchors.append(pair)
        else:
            if len(fuzzy) != 1 or len({old[p['old_index']]['normalized_content_signature'] for p in anchors}) < 2:
                continue
            changed = fuzzy[0]['new_index']
            if 0 < changed < size - 1 and not (
                any(a['new_index'] < changed for a in anchors) and any(a['new_index'] > changed for a in anchors)):
                continue
            # Retain the existing unique strong-token criterion, outside the
            # changed text. A fuzzy row can never become its own anchor.
            strong = any(old_counts[token] == new_counts[token] == 1
                for anchor in anchors
                for token in old_tokens.get(anchor['old_index'], set()) & new_tokens.get(anchor['new_index'], set()))
            if not strong:
                continue
            projected = [dict(item) for item in new]
            projected[changed]['normalized_content_signature'] = old[fuzzy[0]['old_index']]['normalized_content_signature']
            continuity = compare_business_viewport_continuity(old, projected,
                old_boundary_tokens=old_tokens, new_boundary_tokens=new_tokens, allow_history_suffix=True)
            if continuity.get('relation') not in ACCEPTED_RELATIONS:
                continue
            expected = [(start + i, i) for i in range(size)]
            actual = [(p.get('old_index'), p.get('new_index')) for p in continuity.get('matched_pairs', [])]
            if actual != expected:
                continue
            anchor_pairs = [{key: p[key] for key in ('old_index', 'new_index', 'source_message_key', 'observation_id')}
                            for p in anchors]
            for pair in pairs:
                pair['exact_anchor_pairs'] = anchor_pairs
            proof = {'version': VERSION, 'policy_id': POLICY, 'checkpoint_digest': checkpoint['checkpoint_digest'],
                'pre_frame_id': pre_frame_id, 'post_frame_id': post_frame_id, 'pairs': pairs}
            candidates.append({'proof': proof, 'continuity': continuity})
    return candidates[0] if len(candidates) == 1 else None


def comparison_projection(checkpoint, observations, *, pre_frame_id, post_frame_id):
    """A temporary comparison view, never replacement OCR or a new identity.

    Every tolerant frame is independently compared with authoritative history;
    accepting an earlier OCR error never turns it into an anchor for the next.
    """
    rows = ordered_message_viewport_observations(observations)
    projected = normalized_business_message_sequence(rows, message_viewport_bounds=None)
    result = build_correspondence(checkpoint, rows, pre_frame_id=pre_frame_id,
        post_frame_id=post_frame_id,
        new_boundary_tokens=boundary_tokens_for_observations(rows, committed_only=False))
    if result is None:
        return projected, None
    entries = comparison_entries(checkpoint)
    for pair in result["proof"]["pairs"]:
        if pair["matched_by"] == "context_ocr":
            projected[pair["new_index"]] = {**projected[pair["new_index"]],
                "normalized_content_signature": entries[pair["old_index"]]["business_projection"]["normalized_content_signature"]}
    return projected, result["proof"]


def verify_correspondence(proof, checkpoint, observations, *, pre_frame_id, post_frame_id, new_boundary_tokens):
    """Recompute everything from authoritative facts; never trust reported score."""
    validate_proof_shape(proof)
    if not isinstance(proof, dict) or proof.get('checkpoint_digest') != checkpoint.get('checkpoint_digest'):
        raise ValueError('TEXT_CORRESPONDENCE_CHECKPOINT_EXPIRED')
    rebuilt = build_correspondence(checkpoint, observations, pre_frame_id=pre_frame_id,
        post_frame_id=post_frame_id, new_boundary_tokens=new_boundary_tokens)
    if rebuilt is None or rebuilt['proof'] != proof:
        raise ValueError('TEXT_CORRESPONDENCE_PROOF_INVALID')
    return rebuilt['continuity']
