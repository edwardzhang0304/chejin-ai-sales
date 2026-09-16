"""Frame-local numeric transcript ownership; no OCR, capture or UI actions."""
from __future__ import annotations

import re


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
    import cv2
    import numpy as np

    left, top, right, bottom = map(int, viewport)
    pixels = np.asarray(image.convert("RGB"))[top:bottom, left:right]
    if not pixels.size:
        return {}
    sample = pixels[::3, ::3].reshape(-1, 3).astype('int16')
    colours, counts = np.unique(sample // 8, axis=0, return_counts=True)
    background = np.median(sample[np.all(sample // 8 == colours[counts.argmax()], axis=1)], axis=0)
    mask = (np.abs(pixels.astype('int16') - background).mean(axis=2) >= 3).astype('uint8')
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    surfaces = [(left+x, top+y, left+x+w, top+y+h) for x, y, w, h in map(cv2.boundingRect, contours)]

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
        # Look only in the opposite half, excluding all OCR rows. A plain
        # numeric text bubble must not become a voice merely because of width.
        half = (x1+x2)//2
        gx1, gx2 = (x1, half) if role == 'customer' else (half, x2)
        glyph = (pixels[y1-top:y2-top, gx1-left:gx2-left].max(axis=2) < 100)
        for other in rows:
            oa, ob, oc, od = rect(other)
            pad = max(1, (od-ob)*.15)
            gl, gt = max(gx1, int(oa-pad)), max(y1, int(ob-pad))
            gr, gb = min(gx2, int(oc+pad)+1), min(y2, int(od+pad)+1)
            if gr > gl and gb > gt:
                glyph[gt-y1:gb-y1, gl-gx1:gr-gx1] = False
        ys, xs = np.nonzero(glyph)
        if not len(xs) or xs.max()-xs.min()+1 < height*.2 or ys.max()-ys.min()+1 < height*.35:
            continue
        headers.append((row, role, box, height))

    result = {}
    for index, row in enumerate(rows):
        if not re.fullmatch(r'\d{1,3}', re.sub(r'\s+', '', str(row.get('text') or ''))) or avatar(row)[0]:
            continue
        body = surface(row)
        if body is None:
            continue
        owners = []
        for header, role, box, height in headers:
            edge = 0 if role == 'customer' else 2
            if not (0 < body[1]-box[3] <= height*.75 and abs(body[edge]-box[edge]) <= height*.3):
                continue
            if any(avatar(other)[0] and box[3] <= rect(other)[1] < body[3] for other in rows):
                continue
            owners.append({'parent': list(rect(header)), 'bounds': list(body)})
        if len(owners) == 1:
            result[index] = owners[0]
    return result
