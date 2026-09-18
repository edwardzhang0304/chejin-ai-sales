"""Read a narrow status gutter from the existing immutable send-result frame.

This is counterevidence only. A clear gutter never proves delivery by itself;
the original target, draft, new-bubble and body checks remain mandatory.
"""
import math


def _components(points):
    remaining = set(points)
    while remaining:
        seed = remaining.pop()
        component, pending = {seed}, [seed]
        while pending:
            x, y = pending.pop()
            for dx, dy in ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)):
                neighbor = (x + dx, y + dy)
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    pending.append(neighbor)
        yield component


def inspect_gutter(image, bubble_rect, *, dpi_scale=1.0):
    try:
        if isinstance(bubble_rect, dict):
            bubble_rect = [bubble_rect[key] for key in ("left", "top", "right", "bottom")]
        left, top, right, bottom = map(float, bubble_rect)
        scale = float(dpi_scale)
        if (not all(math.isfinite(v) for v in (left, top, right, bottom, scale))
                or not 0.5 <= scale <= 4 or not 0 <= left < right <= image.width
                or not 0 <= top < bottom <= image.height):
            raise ValueError("invalid_geometry")
        center = (top + bottom) / 2
        roi = [max(0, round(left - 44 * scale)), max(0, round(center - 20 * scale)),
               max(0, round(left - 3 * scale)), min(image.height, round(center + 20 * scale))]
        if roi[2] <= roi[0] or roi[3] <= roi[1]:
            raise ValueError("empty_gutter")
        crop = image.crop(roi).convert("RGB")
    except (TypeError, ValueError, KeyError, AttributeError):
        return {"state": "unavailable", "reason": "send_status_gutter_geometry_missing"}
    masks = {"red_failure": set(), "possible_sending": set()}
    for y in range(crop.height):
        for x in range(crop.width):
            red, green, blue = crop.getpixel((x, y))
            if red >= 145 and red - green >= 60 and red - blue >= 50:
                masks["red_failure"].add((x, y))
            elif max(red, green, blue) - min(red, green, blue) <= 25 and 75 <= red <= 200:
                masks["possible_sending"].add((x, y))
    for kind, mask in masks.items():
        for points in _components(mask):
            xs, ys = zip(*points)
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            width, height = x1 - x0 + 1, y1 - y0 + 1
            if (not 5 * scale <= min(width, height) <= 24 * scale
                    or max(width, height) > 26 * scale or not .6 <= width / height <= 1.7):
                continue
            density = len(points) / (width * height)
            if kind == "red_failure":
                matched = density >= .35
            else:
                # A pending spinner is a hollow arc, not arbitrary gray text.
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                center_empty = not any(abs(x - cx) < width * .18 and abs(y - cy) < height * .18 for x, y in points)
                quadrants = {(x >= cx, y >= cy) for x, y in points}
                matched = .12 <= density <= .6 and center_empty and len(quadrants) >= 3
            if matched:
                return {"state": "blocked", "reason": kind, "roi": roi,
                        "indicator_bounds": [roi[0] + x0, roi[1] + y0, roi[0] + x1 + 1, roi[1] + y1 + 1]}
    return {"state": "clear", "roi": roi}
