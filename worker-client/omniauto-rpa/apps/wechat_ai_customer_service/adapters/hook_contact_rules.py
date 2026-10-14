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
    if not isinstance(data, (dict, list)):
        raise ValueError('FRIEND_RESPONSE_SHAPE_INVALID')
    if isinstance(data, dict) and 'code' in data and (type(data['code']) is not int or data['code'] != 0):
        raise ValueError('FRIEND_READ_REJECTED')
    token = re.compile(r'(?<![A-Za-z0-9])' + re.escape(shortcode) + r'(?![A-Za-z0-9])')
    remark_keys = {'remark', 'remarks', 'remarkname', 'markname'}
    identity_keys = {'wxid', 'username', 'user'}
    stack = [(data, 0)]
    matches: list[set[str]] = []
    fields: set[str] = set()
    visited = 0
    while stack:
        value, depth = stack.pop()
        visited += 1
        if depth > 64 or visited > 50000:
            raise ValueError('FRIEND_READ_LIMIT')
        if isinstance(value, dict):
            remarks = [key for key, part in value.items() if isinstance(key, str)
                and key.casefold().replace('_', '') in remark_keys
                and isinstance(part, str) and token.search(part)]
            if remarks:
                identities = {part for key, part in value.items() if isinstance(key, str)
                    and key.casefold().replace('_', '') in identity_keys and isinstance(part, str)}
                # Contradictory identities, groups and missing IDs are ambiguous.
                usable = identities if len(identities) == 1 and all(
                    re.fullmatch(r'wxid_[A-Za-z0-9_]+', part) for part in identities) else set()
                matches.append(usable)
                fields.update(key.casefold().replace('_', '') for key in remarks)
            stack.extend((part, depth + 1) for part in value.values() if isinstance(part, (dict, list)))
        elif isinstance(value, list):
            stack.extend((part, depth + 1) for part in value if isinstance(part, (dict, list)))
    candidate = next(iter(matches[0])) if len(matches) == 1 and len(matches[0]) == 1 else None
    if candidate == self_wxid:
        candidate = None
    return ShortcodeCandidate(len(matches), candidate, tuple(sorted(fields)))
