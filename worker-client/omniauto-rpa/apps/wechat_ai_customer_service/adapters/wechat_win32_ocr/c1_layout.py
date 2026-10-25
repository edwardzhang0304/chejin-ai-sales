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
