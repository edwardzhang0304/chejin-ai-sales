"""Resolve an exact C1 remark shortcode to a read-only contact candidate."""
from __future__ import annotations
from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class ShortcodeCandidate:
    matching_objects: int
    candidate_wxid: str | None
    matched_remark_fields: tuple[str, ...]


def resolve_friend_shortcode(data: Any, shortcode: str, self_wxid: str) -> ShortcodeCandidate:
    if not re.fullmatch(r'[A-Z0-9]{7,8}', shortcode):
        raise ValueError('SHORTCODE_INVALID')
    if not re.fullmatch(r'wxid_[A-Za-z0-9_]+', self_wxid):
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
    return len(identities) == 1 and all(re.fullmatch(r'wxid_[A-Za-z0-9_]+', part) for part in identities)


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
    if not re.fullmatch(r'wxid_[A-Za-z0-9_]+', peer_wxid) or peer_wxid == self_wxid:
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
