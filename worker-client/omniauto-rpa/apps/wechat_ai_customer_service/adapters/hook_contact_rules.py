"""Resolve an exact C1 remark shortcode to a read-only contact candidate."""
from __future__ import annotations
from dataclasses import dataclass
import re
from typing import Any
from .hook_contract_rules import wxid


@dataclass(frozen=True)
class ShortcodeCandidate:
    matching_objects: int
    candidate_wxid: str | None
    matched_remark_fields: tuple[str, ...]


def resolve_friend_shortcode(data: Any, shortcode: str, self_wxid: str) -> ShortcodeCandidate:
    if not re.fullmatch(r'[A-Z0-9]{7,8}', shortcode):
        raise ValueError('SHORTCODE_INVALID')
    if not wxid(self_wxid):
        raise ValueError('SELF_WXID_INVALID')
    token = re.compile(r'(?<![A-Za-z0-9])' + re.escape(shortcode) + r'(?![A-Za-z0-9])')
    matches: list[set[str]] = []
    fields: set[str] = set()
    for value in _contact_objects(data):
        remarks = [key for key, part in value.items() if _key(key) in REMARK_KEYS
                   and isinstance(part, str) and token.search(part)]
        if remarks:
            identities = _identities(value)
            usable = identities if _valid_identity(identities) else set()
            matches.append(usable)
            fields.update(_key(key) for key in remarks)
    candidate = next(iter(matches[0])) if len(matches) == 1 and len(matches[0]) == 1 else None
    if candidate == self_wxid:
        candidate = None
    return ShortcodeCandidate(len(matches), candidate, tuple(sorted(fields)))


REMARK_KEYS = {'remark', 'remarks', 'remarkname', 'markname'}
IDENTITY_KEYS = {'wxid', 'username', 'user'}


def _key(key):
    return key.casefold().replace('_', '') if isinstance(key, str) else ''


def _identities(value):
    return {part for key, part in value.items()
            if _key(key) in IDENTITY_KEYS and isinstance(part, str)}


def _valid_identity(identities):
    return len(identities) == 1 and all(wxid(part) for part in identities)


def _contact_objects(data):
    if not isinstance(data, (dict, list)):
        raise ValueError('FRIEND_RESPONSE_SHAPE_INVALID')
    if isinstance(data, dict) and 'code' in data and (type(data['code']) is not int or data['code'] != 0):
        raise ValueError('FRIEND_READ_REJECTED')
    stack = [(data, 0)]
    visited = 0
    while stack:
        value, depth = stack.pop()
        visited += 1
        if depth > 64 or visited > 50000:
            raise ValueError('FRIEND_READ_LIMIT')
        if isinstance(value, dict):
            yield value
            stack.extend((part, depth + 1) for part in value.values() if isinstance(part, (dict, list)))
        elif isinstance(value, list):
            stack.extend((part, depth + 1) for part in value if isinstance(part, (dict, list)))


def inspect_bound_contact(data: Any, shortcode: str, self_wxid: str, peer_wxid: str) -> str:
    """Positive identity/remark observations only; absence never proves deletion.

    Same parser and shortcode semantics as initial binding. Provider counts or
    repeated identical responses do not certify list completeness/freshness.
    """
    candidate = resolve_friend_shortcode(data, shortcode, self_wxid)
    if not wxid(peer_wxid) or peer_wxid == self_wxid:
        raise ValueError('PEER_WXID_INVALID')
    peers = [value for value in _contact_objects(data) if peer_wxid in _identities(value)]
    if len(peers) != 1 or not _valid_identity(_identities(peers[0])):
        return 'unconfirmed'
    if candidate.matching_objects == 1 and candidate.candidate_wxid == peer_wxid:
        return 'matched'
    remarks = [part for key, part in peers[0].items() if _key(key) in REMARK_KEYS]
    if not remarks or not all(isinstance(part, str) for part in remarks):
        return 'unconfirmed'
    # Ambiguous shortcode matches must not be reinterpreted as a changed remark.
    if candidate.matching_objects:
        return 'unconfirmed'
    return 'remark_changed'


def contact_observation(data: Any, shortcode: str, self_wxid: str, peer_wxid: str) -> dict:
    """Bounded target-only comparison evidence, using the decision's own parser.

    No other contact names, remarks or message bodies are retained. Truncated
    fields carry an explicit marker and a digest; a digest is not raw evidence.
    """
    from .hook_contract_rules import digest
    import hashlib
    result = {'expected_shortcode': shortcode, 'peer_wxid': peer_wxid,
              'response_digest': digest(data), 'peer_objects': [], 'omitted_peer_objects': 0}
    try:
        result['state'] = inspect_bound_contact(data, shortcode, self_wxid, peer_wxid)
        candidate = resolve_friend_shortcode(data, shortcode, self_wxid)
        result['shortcode_matching_objects'] = candidate.matching_objects
        result['matched_remark_fields'] = list(candidate.matched_remark_fields)
        peers = [v for v in _contact_objects(data) if peer_wxid in _identities(v)]
        result['peer_object_count'] = len(peers)
        result['omitted_peer_objects'] = max(0, len(peers)-2)
        for peer in peers[:2]:
            fields = []
            selected = [(k, v) for k, v in peer.items() if _key(k) in REMARK_KEYS | IDENTITY_KEYS]
            for key, value in selected[:16]:
                field = {'field': key[:64], 'type': type(value).__name__}
                if isinstance(value, str):
                    raw = value.encode('utf-8')
                    field.update(value=value[:256], truncated=len(value)>256,
                                 utf8_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                fields.append(field)
            result['peer_objects'].append({'fields': fields, 'omitted_fields': max(0,len(selected)-16)})
    except ValueError as exc:
        result.update(state='unconfirmed', reason_code=str(exc))
    return result
