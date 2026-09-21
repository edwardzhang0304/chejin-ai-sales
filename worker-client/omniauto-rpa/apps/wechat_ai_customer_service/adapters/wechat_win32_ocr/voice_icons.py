"""Frame-local voice proof. Text/colour alone never authorizes a voice action."""
from __future__ import annotations

import base64
from functools import lru_cache
import re
import zlib

import cv2
import numpy as np


# Customer sound-wave glyph ONLY, including its quiet border (30 x 38).
# Reference: user-supplied 2026-09-21 9-second voice screenshot. The green
# 2-second screenshot is an independent, mirrored validation sample.
_REFERENCE = (
    "c%04CNe%!Z2t-lq{+D)#Bfq+1Vr0n+IH0Ax5|UVfSwHYT4aJen)D#E*)!?c1yx^cq"
    "wpWqrK)JKol{t~Qji$bIt2{xTKEIr$M}oPY{006AccKEvdH~D"
)
# Similarity, NOT a calibrated probability. A seconds quote or high OCR
# confidence cannot compensate for an icon below this threshold.
ICON_THRESHOLD = .86
REVIEW_THRESHOLD = .72


def duration_token(text):
    compact = re.sub(r"\s+", "", str(text or ""))
    # Some OCR engines merge the adjacent wave into the duration, e.g. 4"(c.
    # These remain candidates only; 15w/15W are ordinary budget text.
    match = re.fullmatch(r'(\d{1,3})(["“”″\']?)(?:[（(\[][cC]?|[)）\]]{1,2})?', compact)
    if not match or not 1 <= int(match[1]) <= 300:
        return None
    return {"seconds": int(match[1]), "has_quote": bool(match[2])}


def row_bounds(row):
    return tuple(float(row.get(key) or 0) for key in ('left', 'top', 'right', 'bottom'))


def _merged_glyph_candidate(text):
    compact = re.sub(r'\s+', '', str(text or ''))
    return bool(re.fullmatch(r'''[0-9"'“”″()（）\[\]cC:<>|!]{1,14}''', compact)
                and re.search(r'\d', compact))


def contains(outer, inner):
    a, b, c, d = inner
    return outer[0] <= a < c <= outer[2] and outer[1] <= b < d <= outer[3]


def bubble_surfaces(image, viewport):
    """Connected surfaces inside this frame's authoritative message viewport."""
    if image is None:
        return []
    left, top, right, bottom = map(int, viewport)
    left, top = max(0, left), max(0, top)
    right, bottom = min(image.width, right), min(image.height, bottom)
    pixels = np.asarray(image.convert('RGB'))[top:bottom, left:right]
    if not pixels.size:
        return []
    # Read the viewport edge, not the dominant interior: a tightly cropped
    # voice may contain more bubble pixels than conversation background.
    sample = np.concatenate((pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1])).astype('int16')
    colours, counts = np.unique(sample // 8, axis=0, return_counts=True)
    background = np.median(sample[np.all(sample // 8 == colours[counts.argmax()], axis=1)], axis=0)
    mask = (np.abs(pixels.astype('int16') - background).mean(axis=2) >= 3).astype('uint8')
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return [(left+x, top+y, left+x+w, top+y+h)
            for x, y, w, h in map(cv2.boundingRect, contours) if h >= 16 and w >= h]


@lru_cache(maxsize=256)
def _template(height, role):
    raw = np.frombuffer(zlib.decompress(base64.b85decode(_REFERENCE)), dtype='uint8').reshape(38, 30)
    if role == 'self':
        raw = raw[:, ::-1]
    return cv2.resize(raw.astype('float32'), (round(height*30/38), height), interpolation=cv2.INTER_AREA)


def _match(pixels, role, threshold):
    mask = (pixels.max(axis=2) < threshold).astype('float32')
    best = {"icon_score": 0.0, "icon_bounds": None}
    # The full quiet border must fit inside the bubble. This excludes avatars
    # and neighbouring messages; height follows actual pixels, not screen DPI.
    for height in range(max(12, round(mask.shape[0]*.30)), round(mask.shape[0]*.80)+1):
        template = _template(height, role)
        if template.shape[0] > mask.shape[0] or template.shape[1] > mask.shape[1]:
            continue
        _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(mask, template, cv2.TM_CCOEFF_NORMED))
        if score > best['icon_score']:
            best = {"icon_score": float(score), "icon_bounds": [x, y, x+template.shape[1], y+height]}
    return best


def inspect_bubble(image, bounds, role, rows=()):
    """One rule shared by parser, numeric transcripts and action fallback.

    One bounded visual recheck changes binarization only, on the SAME bubble.
    It neither captures UI nor runs OCR, and cannot lower the acceptance bar.
    """
    if image is None or role not in {'customer', 'self'}:
        return {"state": "not_voice", "icon_score": 0.0}
    left, top, right, bottom = map(int, bounds)
    if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
        return {"state": "not_voice", "icon_score": 0.0}
    pixels = np.asarray(image.crop((left, top, right, bottom)).convert('RGB'))
    if not pixels.size or right-left < bottom-top:
        return {"state": "not_voice", "icon_score": 0.0}
    proof = _match(pixels, role, 120)
    proof['review_count'] = 0
    if REVIEW_THRESHOLD <= proof['icon_score'] < ICON_THRESHOLD:
        proof = max([proof, _match(pixels, role, 85), _match(pixels, role, 155)], key=lambda item: item['icon_score'])
        proof['review_count'] = 1
    proof.update(bubble_bounds=list(bounds), role=role, algorithm='voice_icon_v1', icon_threshold=ICON_THRESHOLD)
    icon = proof['icon_bounds']
    if icon is None:
        return {**proof, 'state': 'not_voice'}
    icon = [icon[0]+left, icon[1]+top, icon[2]+left, icon[3]+top]
    proof['icon_bounds'] = icon
    in_bubble = [(row, duration_token(row.get('text'))) for row in rows if contains(bounds, row_bounds(row))]
    duration_rows = []
    conflicting_text = False
    for row, token in in_bubble:
        a, b, c, d = row_bounds(row)
        if contains(icon, (a, b, c, d)):
            continue  # OCR sometimes reads the sound-wave itself as punctuation.
        pad = max(2, (icon[3]-icon[1])*.15)
        beside = (a >= icon[2]-pad or (a >= icon[0]-pad and c > icon[2])) if role == 'customer' else (
            c <= icon[0]+pad or (c <= icon[2]+pad and a < icon[0]))
        aligned = abs((b+d-icon[1]-icon[3])/2) <= (icon[3]-icon[1])*.5
        merged_icon = a < icon[2] and c > icon[0] and _merged_glyph_candidate(row.get('text'))
        if (token or merged_icon) and beside and aligned:
            duration_rows.append((row, token))
        elif str(row.get('text') or '').strip():
            conflicting_text = True
    proof['duration_bounds'] = [list(row_bounds(row)) for row, _ in duration_rows]
    proof['seconds'] = duration_rows[0][1]['seconds'] if len(duration_rows) == 1 and duration_rows[0][1] else None
    proof['has_quote'] = any(token and token['has_quote'] for _, token in duration_rows)
    proof['score'] = round(.8*proof['icon_score'] + .15*bool(duration_rows) + .05*proof['has_quote'], 4)
    proof['state'] = (
        'confirmed' if proof['icon_score'] >= ICON_THRESHOLD and not conflicting_text and len(duration_rows) <= 1
        else 'uncertain' if proof['icon_score'] >= REVIEW_THRESHOLD and not conflicting_text
        else 'not_voice'
    )
    return proof


def annotate_duration_rows(rows, image, viewport):
    """Return copied rows; proof never leaks across frames or overwrites OCR."""
    surfaces = bubble_surfaces(image, viewport)
    result = []
    proofs = {}
    for row in rows:
        clean = {key: value for key, value in row.items()
                 if key not in {'_voice_visual_evidence', '_voice_duration_region', '_voice_transcript_region', '_voice_icon_region'}}
        if duration_token(row.get('text')) or _merged_glyph_candidate(row.get('text')):
            boxes = [box for box in surfaces if contains(box, row_bounds(row))]
            alignment = row.get('avatar_alignment') or {}
            role = alignment.get('role') if not alignment.get('ambiguous') else None
            if len(boxes) == 1 and role in {'customer', 'self'}:
                key = (boxes[0], role)
                if key not in proofs:
                    proofs[key] = inspect_bubble(image, boxes[0], role, rows)
                clean['_voice_visual_evidence'] = proofs[key]
        result.append(clean)
    for row in result:
        if any(proof.get('state') == 'confirmed' and proof.get('icon_bounds')
               and contains(proof['icon_bounds'], row_bounds(row)) for proof in proofs.values()):
            row['_voice_icon_region'] = True
    return result
