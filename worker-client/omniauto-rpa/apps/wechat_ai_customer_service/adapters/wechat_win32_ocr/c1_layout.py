"""Geometry-only ROI definitions. Cached locations never authorize input."""

MAIN_REGIONS = ('header', 'lookup', 'card', 'chat_header')


def main_regions(width, height):
    # Preserve the supported 3.9 layout's existing capture regions. Calculate
    # once at startup, not once per task; OCR still verifies current contents.
    return {
        'header': (0, 0, int(width*.62), min(height, 90)),
        'lookup': (0, 0, int(width*.38), int(height*.43)),
        'card': (int(width*.34), 0, int(width*.80), int(height*.55)),
        'chat_header': (int(width*.36), 0, int(width*.80), min(height, 90)),
    }


def form_regions(width, height):
    return {'fields': (0, 0, width, int(height*.36)),
            'review': (0, 0, width, height),
            'footer': (0, int(height*.80), width, height)}


def valid_rect(rect, width, height):
    return (isinstance(rect, (list, tuple)) and len(rect)==4
            and all(type(value) is int for value in rect)
            and 0 <= rect[0] < rect[2] <= width
            and 0 <= rect[1] < rect[3] <= height)


def main_key(hwnd, pid, geometry, dpi):
    return {'hwnd': hwnd, 'pid': pid, 'origin': (geometry['screen_left'], geometry['screen_top']),
            'geometry': dict(geometry), 'dpi': dpi}


def main_key_changes(expected, observed):
    """Metadata differences only; no title, account or message contents."""
    expected, observed = expected or {}, observed or {}
    changes = [name for name in ('hwnd', 'pid', 'dpi', 'origin')
               if expected.get(name) != observed.get(name)]
    before, after = expected.get('geometry') or {}, observed.get('geometry') or {}
    changes.extend('geometry.' + name for name in sorted(set(before) | set(after))
                   if before.get(name) != after.get(name))
    return changes


# Physical pixels, measured against the immutable startup map (no cumulative
# drift). The smallest supported search field is 25 px high; its center remains
# >9 px inside the field at this bound. Current content checks still apply.
MAIN_LAYOUT_TOLERANCE_PX = 3
_GEOMETRY_FIELDS = {'left', 'top', 'right', 'bottom', 'width', 'height',
                    'screen_left', 'screen_top', 'screen_right', 'screen_bottom'}


def layout_comparison(expected, observed):
    """One cache reuse decision plus exportable, content-free evidence."""
    changes = main_key_changes(expected, observed)
    result = {'tolerance_px': MAIN_LAYOUT_TOLERANCE_PX, 'changed_fields': changes,
              'expected_key': expected, 'observed_key': observed, 'deltas': {},
              'accepted': False, 'reason': 'layout_identity_or_shape_changed'}
    if not expected or not observed or set(expected) != set(observed):
        return result
    if any(expected.get(k) != observed.get(k) for k in ('hwnd', 'pid', 'dpi')):
        return result
    before, after = expected.get('geometry'), observed.get('geometry')
    if not isinstance(before, dict) or not isinstance(after, dict) or set(before) != set(after):
        return result
    required = {'width', 'height', 'screen_left', 'screen_top'}
    if not required <= set(before):
        return result
    for key in (expected, observed):
        g = key['geometry']
        if (not all(type(g.get(k)) is int for k in g if k in _GEOMETRY_FIELDS)
                or g['width'] <= 0 or g['height'] <= 0
                or not isinstance(key.get('origin'), (tuple, list))
                or tuple(key['origin']) != (g['screen_left'], g['screen_top'])):
            return result
        for axis, extent in [('left', 'width'), ('top', 'height')]:
            far = 'right' if axis == 'left' else 'bottom'
            for prefix in ('', 'screen_'):
                if prefix+axis in g and prefix+far in g and g[prefix+far]-g[prefix+axis] != g[extent]:
                    return result
    result['deltas'] = {k: after[k]-before[k] for k in before if k in _GEOMETRY_FIELDS}
    if any(before[k] != after[k] for k in before if k not in _GEOMETRY_FIELDS):
        return result
    result['accepted'] = all(abs(d) <= MAIN_LAYOUT_TOLERANCE_PX for d in result['deltas'].values())
    result['reason'] = ('unchanged' if not changes else 'minor_geometry_deviation') if result['accepted'] else 'geometry_tolerance_exceeded'
    return result
