"""Frame-local numeric transcript ownership; no OCR, capture or UI actions."""
from __future__ import annotations

import re

from .voice_icons import bubble_surfaces, inspect_bubble


def numeric_transcript_regions(rows, image, viewport):
    """Return only numeric rows in a separate, attached voice transcript box.

    A short number alone proves neither a duration nor a transcript. Require
    an avatar-owned header, a duration at its far end and a separate voice
    glyph at the opposite end, then an aligned box below without a new avatar.
    All distances derive from this frame's avatar, text and surface geometry.
    The returned indexes/bounds are local to this call, never message IDs.
    """
    if image is None or not rows:
        return {}
    surfaces = bubble_surfaces(image, viewport)

    def rect(row):
        return tuple(float(row.get(k) or 0) for k in ('left', 'top', 'right', 'bottom'))

    def surface(row):
        a, b, c, d = rect(row)
        matches = [r for r in surfaces if r[0] <= a < c <= r[2] and r[1] <= b < d <= r[3]]
        return matches[0] if len(matches) == 1 else None

    def avatar(row):
        evidence = row.get('avatar_alignment') or {}
        role = evidence.get('role')
        detail = evidence.get(role) or {}
        bounds = detail.get('foreground_bounds') or detail.get('component_bounds')
        return (role, bounds) if not evidence.get('ambiguous') and role in {'customer', 'self'} and bounds else ('', None)

    headers = []
    for row in rows:
        role, bounds = avatar(row)
        if not role or not re.fullmatch(r'\d{1,3}["“”″\']?', re.sub(r'\s+', '', str(row.get('text') or ''))):
            continue
        box = surface(row)
        if box is None:
            continue
        x1, y1, x2, y2 = box
        a, b, c, d = rect(row)
        height = bounds[3] - bounds[1]
        if height <= 0 or not .6*height <= y2-y1 <= 2*height or x2-x1 < 1.5*height:
            continue
        relative_x = ((a+c)/2-x1)/(x2-x1)
        if not (relative_x >= .55 if role == 'customer' else relative_x <= .45):
            continue
        proof = inspect_bubble(image, box, role, rows)
        if proof.get('state') != 'confirmed' or list(rect(row)) not in proof.get('duration_bounds', []):
            continue
        headers.append((row, role, box, height, proof))

    result = {}
    for index, row in enumerate(rows):
        if not re.fullmatch(r'\d{1,3}', re.sub(r'\s+', '', str(row.get('text') or ''))) or avatar(row)[0]:
            continue
        body = surface(row)
        if body is None:
            continue
        owners = []
        for header, role, box, height, proof in headers:
            edge = 0 if role == 'customer' else 2
            if not (0 < body[1]-box[3] <= height*.75 and abs(body[edge]-box[edge]) <= height*.3):
                continue
            if any(avatar(other)[0] and box[3] <= rect(other)[1] < body[3] for other in rows):
                continue
            owners.append({'parent': list(rect(header)), 'bounds': list(body), 'voice_evidence': proof})
        if len(owners) == 1:
            result[index] = owners[0]
    return result
