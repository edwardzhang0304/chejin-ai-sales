"""D1 bounded correspondence proof; observation text and stored facts stay intact.

The caller still owns authorization, action/voice receipts and frame admission.
This module proves only the finite old-suffix/new-prefix correspondence.
"""
from collections import Counter
import hashlib
import math
import re
import time

from .business_viewport_continuity import compare_business_viewport_continuity, boundary_tokens_for_observations
from .historical_text_correction import checkpoint_comparison
from .message_viewport_projection import normalized_business_message_sequence, ordered_message_viewport_observations
from .text_correspondence import (checkpoint_digest, historical_text_candidate,
    validate_entity_context, normalized_projection_text, historical_confidence_scores,
    validate_historical_match_policy)
from .confirmed_sent_history import extend_checkpoint, validate_receipts

VERSION = 1
POLICY = "historical_context_v1"
ACCEPTED_RELATIONS = {"business_sequence_equal", "unique_tail_append",
    "unique_viewport_slide_with_tail_append", "unique_history_suffix_without_new_messages"}


def requires_text_correspondence(checkpoint, observations, decision, old_entries=None):
    """Candidate signatures omit punctuation; they cannot certify old text.

    Compare the original bodies under the same presentation rule as ingest.
    Hashes and saved projections keep their released meaning.
    """
    entries = comparison_entries(checkpoint) if old_entries is None else old_entries
    rows = ordered_message_viewport_observations(observations)
    pairs = list(decision.get('matched_pairs') or [])
    if not pairs:
        for candidate in decision.get('overlap_candidates') or []:
            pairs.extend({'old_index': candidate['old_start'] + i,
                          'new_index': candidate.get('new_start', 0) + i}
                         for i in range(candidate['overlap_size']))
    for pair in pairs:
        i, j = pair['old_index'], pair['new_index']
        if i >= len(entries) or j >= len(rows):
            continue  # Unconfirmed local sends retain their separate gate.
        old = entries[i]
        if old.get('message_type') not in {'text', 'voice', 'system'}:
            continue
        canonical = (old.get('effective_text') or {}).get('text')
        if isinstance(canonical, str) and normalized_projection_text(canonical) != normalized_projection_text(rows[j].get('content_clean')):
            return True
    return False


def reconcile_checkpoint_continuity(checkpoint, observations, decision, *, old_projection,
        old_boundary_tokens, pre_frame_id, post_frame_id, diagnostics=None, deadline=None,
        old_identities=None):
    """One admission gate for both exact candidates and historical OCR drift."""
    entries = comparison_entries(checkpoint)
    indexes = _baseline_indexes(entries, old_projection, old_identities)
    baseline = [entries[i] for i in indexes] if indexes is not None else None
    needs_proof = requires_text_correspondence(checkpoint, observations, decision, baseline)
    if decision.get('relation') in ACCEPTED_RELATIONS and not needs_proof:
        return decision
    verified = validated_projection_continuity(checkpoint, observations, old_projection=old_projection,
        old_boundary_tokens=old_boundary_tokens, pre_frame_id=pre_frame_id, post_frame_id=post_frame_id,
        diagnostics=diagnostics, deadline=deadline, old_identities=old_identities)
    if verified:
        return verified
    if needs_proof:
        return {**decision, 'relation': 'business_sequence_not_continuous',
                'reason': 'historical_text_correspondence_unverified',
                'matched_pairs': [], 'new_suffix_indexes': []}
    return decision


def _projection_keys(sequence):
    return [tuple(row.get(k) for k in ('sender_role', 'message_type', 'media_state',
            'normalized_content_signature')) for row in sequence]


def _baseline_indexes(entries, projection, identities):
    """Map a frozen viewport to history by identity, never by screen position."""
    if identities is None:
        return None  # Existing full-history callers retain their index space.
    def invalid():
        raise ValueError('TEXT_CORRESPONDENCE_BASELINE_INVALID')
    if len(identities) != len(projection):
        invalid()
    by_stable, by_source = {}, {}
    for i, entry in enumerate(entries):
        for lookup, key in ((by_stable, 'stable_id'), (by_source, 'source_message_key')):
            value = entry.get(key)
            if not value or value in lookup:
                invalid()
            lookup[value] = i
    indexes = []
    for identity in identities:
        stable = identity.get('worker_stable_id') or identity.get('stable_id')
        source = identity.get('source_message_key')
        matched = [lookup.get(value) for lookup, value in ((by_stable, stable), (by_source, source)) if value]
        if not matched or any(i is None or i != matched[0] for i in matched):
            invalid()
        indexes.append(matched[0])
    if indexes != sorted(set(indexes)) or _projection_keys(projection) != _projection_keys(
            [entries[i].get('business_projection') or {} for i in indexes]):
        invalid()
    return indexes


def comparison_entries(checkpoint):
    """Use the same committed sequence order as Worker, without mutating facts."""
    entries = [checkpoint_comparison(item) for item in checkpoint.get("recent_messages", [])]
    numbers = [re.fullmatch(r"worker-message-(\d+)", str(item.get("stable_id") or "")) for item in entries]
    if entries and all(numbers):
        entries = [item for _, item in sorted(zip((int(m.group(1)) for m in numbers), entries), key=lambda p: p[0])]
    return entries


def checkpoint_for_proof_version(checkpoint, version):
    """Project only the additive HC fields away for a frozen v1 replay.

    The same current authority and effective text remain; v1 is never rescored
    under v2. New v2 work still requires the negotiated bound policy.
    """
    if version == 1 and 'historical_match_policy' in checkpoint:
        sent_ids = {r['worker_stable_id'] for r in checkpoint.get('confirmed_sent_receipts', [])}
        result = {k: v for k, v in checkpoint.items() if k not in {'historical_match_policy', 'confirmed_sent_receipts'}}
        result['recent_messages'] = [{k: v for k, v in entry.items() if k != 'historical_identity_features'}
                                     for entry in checkpoint.get('recent_messages', []) if entry.get('stable_id') not in sent_ids]
        result['checkpoint_digest'] = checkpoint_digest(result)
        return result
    return checkpoint


def checkpoint_for_proof(checkpoint, proof):
    """Bind the same complete old sequence, including confirmed send receipts."""
    checkpoint = checkpoint_for_proof_version(checkpoint, proof['version'])
    receipts = proof.get('confirmed_sent_receipts')
    if receipts and not checkpoint.get('confirmed_sent_receipts'):
        checkpoint = extend_checkpoint(checkpoint, receipts)
    return checkpoint


def validate_proof_shape(proof):
    """Strict portable wire shape; authority and all scores are recomputed later."""
    if isinstance(proof, dict) and type(proof.get('version')) is int and proof['version'] == 2:
        return _validate_confidence_proof_shape(proof)
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


def _legacy_anchor_support(changed, size, anchors, old, old_tokens, new_tokens):
    """Frozen D1 support, also used for completed voice compatibility in HC."""
    if len({old[p['old_index']]['normalized_content_signature'] for p in anchors}) < 2:
        return False
    if 0 < changed < size - 1 and not (
            any(a['new_index'] < changed for a in anchors) and any(a['new_index'] > changed for a in anchors)):
        return False
    old_counts = Counter(t for tokens in old_tokens.values() for t in tokens)
    new_counts = Counter(t for tokens in new_tokens.values() for t in tokens)
    return any(old_counts[t] == new_counts[t] == 1 for anchor in anchors
        for t in old_tokens.get(anchor['old_index'], set()) & set(new_tokens.get(anchor['new_index'], [])))


def build_correspondence(checkpoint, observations, *, pre_frame_id, post_frame_id,
                         new_boundary_tokens, enabled=True, diagnostics=None, deadline=None):
    """Return None for an unsupported/insufficient candidate; reject bad context."""
    if not enabled or 'text_correspondence_context' not in checkpoint:
        return None
    entities = validate_entity_context(checkpoint['text_correspondence_context'])
    if checkpoint.get('checkpoint_digest') != checkpoint_digest(checkpoint):
        raise ValueError('TEXT_CORRESPONDENCE_CHECKPOINT_INVALID')
    if not pre_frame_id or not post_frame_id:
        raise ValueError('TEXT_CORRESPONDENCE_FRAME_MISSING')
    if 'historical_match_policy' in checkpoint:
        return _build_confidence_correspondence(checkpoint, observations,
            pre_frame_id=pre_frame_id, post_frame_id=post_frame_id,
            new_boundary_tokens=new_boundary_tokens, entities=entities,
            diagnostics=diagnostics, deadline=deadline)
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
            if len(fuzzy) != 1:
                continue
            changed = fuzzy[0]['new_index']
            # Retain the existing unique strong-token criterion, outside the
            # changed text. A fuzzy row can never become its own anchor.
            if not _legacy_anchor_support(changed, size, anchors, old, old_tokens, new_tokens):
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


def comparison_projection(checkpoint, observations, *, pre_frame_id, post_frame_id, frozen_proof=None):
    """A temporary comparison view, never replacement OCR or a new identity.

    Every tolerant frame is independently compared with authoritative history;
    accepting an earlier OCR error never turns it into an anchor for the next.
    """
    rows = ordered_message_viewport_observations(observations)
    projected = normalized_business_message_sequence(rows, message_viewport_bounds=None)
    result = build_correspondence(checkpoint, rows, pre_frame_id=pre_frame_id,
        post_frame_id=post_frame_id,
        new_boundary_tokens=boundary_tokens_for_observations(rows, committed_only=False))
    if frozen_proof:
        # Replay checks the COMPLETE recomputed proof, including both body
        # hashes, authority digest, frame IDs, scores and identity mappings.
        # It does not accept a proof merely because observation IDs match.
        frozen_checkpoint = checkpoint_for_proof_version(checkpoint, frozen_proof['version'])
        continuity = verify_correspondence(frozen_proof, frozen_checkpoint, rows,
            pre_frame_id=pre_frame_id, post_frame_id=post_frame_id,
            new_boundary_tokens=boundary_tokens_for_observations(rows, committed_only=False))
        result = {'proof': frozen_proof, 'continuity': continuity}
    if result is None:
        entries = comparison_entries(checkpoint)
        decision = compare_business_viewport_continuity(
            [e.get('business_projection') or {} for e in entries], projected,
            old_boundary_tokens={i: set(e.get('strong_boundary_tokens') or []) for i, e in enumerate(entries)},
            new_boundary_tokens=boundary_tokens_for_observations(rows, committed_only=False), allow_history_suffix=True)
        if requires_text_correspondence(checkpoint, rows, decision):
            raise ValueError('TEXT_CORRESPONDENCE_PROOF_INVALID')
        return projected, None
    entries = comparison_entries(checkpoint_for_proof_version(checkpoint, result["proof"]["version"]))
    for pair in result["proof"]["pairs"]:
        if pair["matched_by"] in {"context_ocr", "confidence"}:
            projected[pair["new_index"]] = {**projected[pair["new_index"]],
                "normalized_content_signature": entries[pair["old_index"]]["business_projection"]["normalized_content_signature"]}
    for pair in result['continuity'].get('voice_correspondence', []):
        projected[pair['new_index']]['normalized_content_signature'] = entries[pair['old_index']]['business_projection']['normalized_content_signature']
    return projected, result["proof"]


def compare_historical_viewports(checkpoint, baseline, current, *, old_boundary_tokens, allow_history_suffix=True,
                                frozen_correspondence=None):
    """One proof consumer for Sidecar and backend interruption verification."""
    old, old_proof = comparison_projection(checkpoint, baseline, pre_frame_id='checkpoint:send-guard',
        post_frame_id='send-guard:baseline', frozen_proof=(frozen_correspondence or {}).get('baseline'))
    new, new_proof = comparison_projection(checkpoint, current, pre_frame_id='checkpoint:send-guard',
        post_frame_id='send-guard:current', frozen_proof=(frozen_correspondence or {}).get('current'))
    if not (old_proof or new_proof):
        return None
    old_tokens = {i: set(v) for i, v in old_boundary_tokens.items()}
    new_tokens = boundary_tokens_for_observations(current, committed_only=False)
    if any(p and p['version'] == 2 for p in (old_proof, new_proof)):
        entries = comparison_entries(checkpoint)
        authority = [e.get('business_projection') or {} for e in entries]
        authority_tokens = {i: set(e.get('strong_boundary_tokens') or []) for i,e in enumerate(entries)}
        for rows, sequence, proof, tokens in ((baseline, old, old_proof, old_tokens), (current, new, new_proof, new_tokens)):
            if proof:
                pairs = proof['pairs']
            else:
                exact = compare_business_viewport_continuity(authority, sequence,
                    old_boundary_tokens=authority_tokens,
                    new_boundary_tokens=boundary_tokens_for_observations(rows, committed_only=False),
                    allow_history_suffix=True)
                if exact['relation'] not in ACCEPTED_RELATIONS:
                    return None
                pairs = exact['matched_pairs']
            for pair in pairs:
                tokens.setdefault(pair['new_index'], set()).add('hc2:'+entries[pair['old_index']]['source_message_key'])
    decision = compare_business_viewport_continuity(old, new, old_boundary_tokens=old_tokens,
        new_boundary_tokens=new_tokens, allow_history_suffix=allow_history_suffix)
    decision['text_correspondence'] = {'baseline': old_proof, 'current': new_proof}
    return old, new, decision


def validated_projection_continuity(checkpoint, observations, *, old_projection,
        old_boundary_tokens, pre_frame_id, post_frame_id, diagnostics=None, deadline=None,
        old_identities=None):
    """Consume one historical decision over the complete confirmed baseline.

    Unconfirmed send candidates are deliberately outside this checkpoint. Any
    such extra baseline rows still need their original deterministic gate.
    """
    tokens = boundary_tokens_for_observations(observations, committed_only=False)
    built = build_correspondence(checkpoint, observations, pre_frame_id=pre_frame_id,
        post_frame_id=post_frame_id, new_boundary_tokens=tokens, diagnostics=diagnostics, deadline=deadline)
    if not built:
        return None
    entries = comparison_entries(checkpoint_for_proof_version(checkpoint, built['proof']['version']))
    authority = [e.get('business_projection') or {} for e in entries]
    indexes = _baseline_indexes(entries, old_projection, old_identities)
    if indexes is None and _projection_keys(old_projection) == _projection_keys(authority):
        return {**built['continuity'], 'text_correspondence': built['proof']}
    if indexes is None and _projection_keys(old_projection[:len(authority)]) != _projection_keys(authority):
        return None
    rows = ordered_message_viewport_observations(observations)
    projected = normalized_business_message_sequence(rows, message_viewport_bounds=None)
    for pair in [*built['proof']['pairs'], *built['continuity'].get('voice_correspondence', [])]:
        projected[pair['new_index']] = {**projected[pair['new_index']],
            'normalized_content_signature': authority[pair['old_index']]['normalized_content_signature']}
    result = compare_business_viewport_continuity(old_projection, projected,
        old_boundary_tokens=old_boundary_tokens, new_boundary_tokens=tokens, allow_history_suffix=True)
    expected = {(p['old_index'],p['new_index']) for p in built['continuity']['matched_pairs']}
    if indexes is not None:
        local_index = {historical: local for local, historical in enumerate(indexes)}
        if any(i not in local_index for i, _ in expected):
            return None
        expected = {(local_index[i], j) for i, j in expected}
    actual = {(p['old_index'],p['new_index']) for p in result.get('matched_pairs', [])}
    if result['relation'] not in ACCEPTED_RELATIONS or not expected.issubset(actual):
        return None
    # The local decision indexes the frozen viewport; the immutable wire proof
    # still indexes complete server history for independent backend validation.
    return {**result, 'text_correspondence': built['proof'],
        'candidate_alignment_count': built['proof'].get('candidate_count', 1)}


def verify_correspondence(proof, checkpoint, observations, *, pre_frame_id, post_frame_id, new_boundary_tokens):
    """Recompute everything from authoritative facts; never trust reported score."""
    validate_proof_shape(proof)
    if not isinstance(proof, dict) or proof.get('checkpoint_digest') != checkpoint.get('checkpoint_digest'):
        raise ValueError('TEXT_CORRESPONDENCE_CHECKPOINT_EXPIRED')
    if (proof['version'] == 2) != ('historical_match_policy' in checkpoint):
        raise ValueError('TEXT_CORRESPONDENCE_PROOF_INVALID')
    rebuilt = build_correspondence(checkpoint, observations, pre_frame_id=pre_frame_id,
        post_frame_id=post_frame_id, new_boundary_tokens=new_boundary_tokens)
    if (rebuilt is None or rebuilt['proof'] != proof) and proof['version'] == 2:
        # Frozen receipts from before transcript unification retain their exact
        # wire interpretation. New reads never call this compatibility branch.
        rebuilt = _build_confidence_correspondence(checkpoint, observations,
            pre_frame_id=pre_frame_id, post_frame_id=post_frame_id,
            new_boundary_tokens=new_boundary_tokens,
            entities=validate_entity_context(checkpoint['text_correspondence_context']),
            diagnostics=None, deadline=None, legacy_voice=True)
    if rebuilt is None or rebuilt['proof'] != proof:
        raise ValueError('TEXT_CORRESPONDENCE_PROOF_INVALID')
    return rebuilt['continuity']


def _validate_confidence_proof_shape(proof):
    def require(ok):
        if not ok:
            raise ValueError('TEXT_CORRESPONDENCE_PROOF_INVALID')
    def identity(value):
        return isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip()
    def integer(value, minimum=0, maximum=10000):
        return type(value) is int and minimum <= value <= maximum
    def sha(value):
        return isinstance(value, str) and bool(re.fullmatch(r'[0-9a-f]{64}', value))
    fields = {'version', 'policy_id', 'policy_digest', 'checkpoint_digest',
        'pre_frame_id', 'post_frame_id', 'pairs', 'candidate_count', 'best_score', 'runner_up_score', 'margin'}
    require(set(proof) in (fields, fields | {'confirmed_sent_receipts'}))
    if 'confirmed_sent_receipts' in proof:
        validate_receipts(proof['confirmed_sent_receipts'])
    require(proof['policy_id'] == 'historical_text_identity_v2'
        and sha(proof['policy_digest']) and sha(proof['checkpoint_digest'])
        and identity(proof['pre_frame_id']) and identity(proof['post_frame_id'])
        and integer(proof['candidate_count'], 1, 500) and integer(proof['best_score']))
    if proof['candidate_count'] == 1:
        require(proof['runner_up_score'] is None and proof['margin'] is None)
    else:
        require(integer(proof['runner_up_score']) and integer(proof['margin'])
            and proof['margin'] == proof['best_score'] - proof['runner_up_score'])
    require(isinstance(proof['pairs'], list) and 1 <= len(proof['pairs']) <= 500)
    fields = {'old_index', 'new_index', 'source_message_key', 'observation_id',
        'canonical_text_sha256', 'observed_text_sha256', 'effective_text_version',
        'matched_by', 'scores', 'anchor_pairs'}
    seen = {key: set() for key in ('old_index', 'new_index', 'source_message_key', 'observation_id')}
    for pair in proof['pairs']:
        require(isinstance(pair, dict) and set(pair) == fields)
        require(integer(pair['old_index'], 0, 499) and integer(pair['new_index'], 0, 499)
            and identity(pair['source_message_key']) and identity(pair['observation_id'])
            and sha(pair['canonical_text_sha256']) and sha(pair['observed_text_sha256'])
            and integer(pair['effective_text_version'], 0, 2**31-1))
        for key, values in seen.items():
            require(pair[key] not in values)
            values.add(pair[key])
        require(pair['matched_by'] in {'exact', 'confidence'})
        scores = pair['scores']
        if pair['matched_by'] == 'exact':
            require(scores is None)
        else:
            require(isinstance(scores, dict) and set(scores) == {
                'text', 'score'})
            require(integer(scores['text']) and scores['score'] == scores['text'] and integer(scores['score']))
        require(isinstance(pair['anchor_pairs'], list) and len(pair['anchor_pairs']) <= 500)
        anchor_ids = set()
        for anchor in pair['anchor_pairs']:
            require(isinstance(anchor, dict) and set(anchor) == {
                'old_index', 'new_index', 'source_message_key', 'observation_id'})
            require(integer(anchor['old_index'], 0, 499) and integer(anchor['new_index'], 0, 499)
                and identity(anchor['source_message_key']) and identity(anchor['observation_id'])
                and anchor['source_message_key'] not in anchor_ids
                and anchor['source_message_key'] != pair['source_message_key'])
            anchor_ids.add(anchor['source_message_key'])
    require(any(p['matched_by'] == 'confidence' for p in proof['pairs']))
    return proof


def _build_confidence_correspondence(checkpoint, observations, *, pre_frame_id,
        post_frame_id, new_boundary_tokens, entities, diagnostics, deadline, legacy_voice=False):
    """HC v2: all legal suffixes compete using their weakest nonexact row.

    This is an identity projection only. Facts, feature sources and durable IDs
    remain untouched. Exact boundaries retain the existing deterministic gate.
    """
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError('historical correspondence execution deadline exceeded')
    policy = validate_historical_match_policy(checkpoint['historical_match_policy'])
    entries = comparison_entries(checkpoint)
    rows = ordered_message_viewport_observations(observations)
    old = [item.get('business_projection') or {} for item in entries]
    new = normalized_business_message_sequence(rows, message_viewport_bounds=None)
    report = diagnostics if diagnostics is not None else {}
    report.update(policy_digest=policy['policy_digest'], candidates=[], accepted=False)
    # The 200 server facts stay intact. Locally confirmed sends have not yet
    # entered that window, and must not invalidate it merely by being added.
    receipt_count = len(checkpoint.get('confirmed_sent_receipts') or [])
    if (not old or not new or len(old) > 200 + receipt_count or len(new) > 500
            or any(not e.get('source_message_key') or not e.get('stable_id') for e in entries)
            or len({e['source_message_key'] for e in entries}) != len(entries)
            or any(not r.get('observation_id') or r.get('contract_errors') for r in rows)
            or len({r['observation_id'] for r in rows}) != len(rows)):
        report['reason'] = 'invalid_original_identity_or_frame'
        return None
    old_tokens = {i: set(e.get('strong_boundary_tokens') or []) for i, e in enumerate(entries)}
    exact = compare_business_viewport_continuity(old, new, old_boundary_tokens=old_tokens,
        new_boundary_tokens=new_boundary_tokens, allow_history_suffix=True)
    if (exact['relation'] in ACCEPTED_RELATIONS or exact['overlap_candidates']) and not requires_text_correspondence(checkpoint, rows, exact):
        report['reason'] = 'existing_exact_boundary'
        return None
    old_text = [(e.get('effective_text') or {}).get('text') for e in entries]
    new_text = [r.get('content_clean') for r in rows]
    old_normal = [normalized_projection_text(t) if isinstance(t, str) else '' for t in old_text]
    new_normal = [normalized_projection_text(t) if isinstance(t, str) else '' for t in new_text]
    old_counts, new_counts = Counter(old_normal), Counter(new_normal)
    token_counts_old = Counter(t for tokens in old_tokens.values() for t in tokens)
    token_counts_new = Counter(t for tokens in new_boundary_tokens.values() for t in tokens)
    def strong_pair(i, j):
        return any(token_counts_old[t] == token_counts_new[t] == 1
                   for t in old_tokens.get(i, set()) & set(new_boundary_tokens.get(j, [])))
    def native_id(value):
        source = value.get('source_message') or {}
        return str(value.get('native_source_message_id') or source.get('native_source_message_id') or '').strip()
    report['auxiliary_evidence'] = 'diagnostic_only'
    score_cache, metrics_cache, candidates = {}, {}, []
    for start in range(max(0, len(old)-len(new)), len(old)):
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError('historical correspondence execution deadline exceeded')
        size = len(old)-start
        indexes = [(start+j, j) for j in range(size)]
        if any(not _same_kind(old[i], new[j]) for i, j in indexes):
            continue
        if any(native_id(entries[i]) and native_id(rows[j]) and native_id(entries[i]) != native_id(rows[j])
               for i, j in indexes):
            continue
        text_indexes = [(i, j) for i, j in indexes if old[i].get('message_type') == 'text'
            or not legacy_voice and (old[i].get('message_type') == 'system'
                or old[i].get('message_type') == 'voice' and old[i].get('media_state') == 'transcribed')]
        if any(not old_normal[i] or not new_normal[j] for i, j in text_indexes):
            continue
        anchors = [{'old_index': i, 'new_index': j, 'source_message_key': entries[i]['source_message_key'],
            'observation_id': rows[j]['observation_id']} for i, j in text_indexes
            if old[i].get('message_type') == 'text'
            and old_normal[i] == new_normal[j] and old_counts[old_normal[i]] == new_counts[new_normal[j]] == 1]
        media_changes = [(i, j) for i, j in indexes if (i, j) not in text_indexes
            and old[i].get('normalized_content_signature') != new[j].get('normalized_content_signature')]
        # New reads score completed transcripts exactly like old text. Physical
        # media identity/state is still checked by _same_kind and the caller.
        # The single-edit branch only decodes an already saved older proof.
        if media_changes and (not legacy_voice or len(media_changes) != 1 or any(
                old[i].get('message_type') != 'voice' or old[i].get('media_state') != 'transcribed'
                or not historical_text_candidate(old_text[i], new_text[j], protected_entities=entities)['eligible']
                or not _legacy_anchor_support(j, size, anchors, old, old_tokens, new_boundary_tokens)
                for i, j in media_changes)):
            continue
        # A fuzzy spelling cannot authorize itself. Original native/strong
        # evidence or another unique exact ordinary-text row locates this
        # overlap; all legal candidates then compete, even below threshold.
        if any(old_normal[i] != new_normal[j] and not (strong_pair(i, j) or anchors)
               for i, j in text_indexes):
            continue
        pairs, scores = [], []
        for i, j in text_indexes:
            support = [a for a in anchors if a['old_index'] != i]
            same = old_normal[i] == new_normal[j]
            score = None
            if not same:
                key = (i, j)
                if key not in score_cache:
                    metrics_cache[key] = {}
                    score_cache[key] = historical_confidence_scores(old_text[i], new_text[j],
                        policy=policy, deadline=deadline, diagnostics=metrics_cache[key])
                score = score_cache[key]
                scores.append(score['score'])
            effective_version = (entries[i].get('effective_text') or {}).get('version', 0)
            if type(effective_version) is not int or effective_version < 0:
                raise ValueError('TEXT_CORRESPONDENCE_CHECKPOINT_INVALID')
            pairs.append({'old_index': i, 'new_index': j, 'source_message_key': entries[i]['source_message_key'],
                'observation_id': rows[j]['observation_id'],
                'canonical_text_sha256': hashlib.sha256(old_text[i].encode('utf-8')).hexdigest(),
                'observed_text_sha256': hashlib.sha256(new_text[j].encode('utf-8')).hexdigest(),
                'effective_text_version': effective_version, 'matched_by': 'exact' if same else 'confidence',
                'scores': score, 'anchor_pairs': [] if same else support[:1]})
        if not scores:
            report['reason'] = 'unresolved_exact_boundary'
            return None
        projected = [dict(item) for item in new]
        for pair in pairs:
            projected[pair['new_index']]['normalized_content_signature'] = old[pair['old_index']]['normalized_content_signature']
        for i, j in media_changes:
            projected[j]['normalized_content_signature'] = old[i]['normalized_content_signature']
        original = compare_business_viewport_continuity(old, projected,
            old_boundary_tokens=old_tokens, new_boundary_tokens=new_boundary_tokens, allow_history_suffix=True)
        if (original['relation'] not in ACCEPTED_RELATIONS
                or [(p['old_index'], p['new_index']) for p in original['matched_pairs']] != indexes):
            continue
        candidates.append({'old_start': start, 'overlap_size': size, 'score': min(scores), 'pairs': pairs, 'anchors': anchors,
            'voice_correspondence': [{'old_index': i, 'new_index': j, 'source_message_key': entries[i]['source_message_key'],
                                      'observation_id': rows[j]['observation_id']} for i, j in media_changes]})
    candidates.sort(key=lambda c: (-c['score'], c['old_start']))
    report['candidates'] = [{**c, 'text_metrics': [
        {'old_index': p['old_index'], 'new_index': p['new_index'],
         **metrics_cache[(p['old_index'], p['new_index'])]}
        for p in c['pairs'] if p['matched_by'] == 'confidence'],
        'media_validation': 'original_sequence_rules',
        'new_suffix_indexes': list(range(c['overlap_size'], len(new)))} for c in candidates]
    if not candidates:
        report['reason'] = 'no_legal_candidate'
        return None
    best = candidates[0]
    runner_up = candidates[1]['score'] if len(candidates) > 1 else None
    margin = best['score']-runner_up if runner_up is not None else None
    report.update(best_score=best['score'], runner_up_score=runner_up, margin=margin)
    if best['score'] < policy['accept_threshold'] or margin is not None and margin < policy['minimum_margin']:
        report['reason'] = 'insufficient_score_or_margin'
        return None
    proof = {'version': 2, 'policy_id': policy['policy_id'], 'policy_digest': policy['policy_digest'],
        'checkpoint_digest': checkpoint['checkpoint_digest'], 'pre_frame_id': pre_frame_id,
        'post_frame_id': post_frame_id, 'pairs': best['pairs'], 'candidate_count': len(candidates),
        'best_score': best['score'], 'runner_up_score': runner_up, 'margin': margin}
    if checkpoint.get('confirmed_sent_receipts'):
        proof['confirmed_sent_receipts'] = checkpoint['confirmed_sent_receipts']
    validate_proof_shape(proof)
    start, size = best['old_start'], best['overlap_size']
    continuity = {'relation': ('business_sequence_equal' if start == 0 and size == len(new)
        else 'unique_history_suffix_without_new_messages' if size == len(new)
        else 'unique_tail_append' if start == 0 else 'unique_viewport_slide_with_tail_append'),
        'reason': 'historical_confidence_unique_winner', 'old_count': len(old), 'new_count': len(new),
        'matched_pairs': [{'old_index': start+j, 'new_index': j} for j in range(size)],
        'new_suffix_indexes': list(range(size, len(new))), 'candidate_alignment_count': len(candidates),
        'overlap_candidates': [{'old_start': c['old_start'], 'overlap_size': c['overlap_size'],
            'new_start': 0, 'new_suffix_start': c['overlap_size']} for c in candidates],
        'context_expansion_used': False}
    if best['voice_correspondence']:
        continuity['voice_correspondence'] = best['voice_correspondence']
    report.update(accepted=True, reason='historical_confidence_unique_winner')
    return {'proof': proof, 'continuity': continuity}
