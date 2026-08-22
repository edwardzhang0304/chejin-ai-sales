"""v0.9.26 startup calibration, business-frame facts and coordinate mapping."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from statistics import median
import time
import uuid
from typing import Any, Callable, Mapping


CAPTURE_MODE_WINDOW_VISIBLE_SCREEN = "wechat_window_visible_screen"
CAPTURE_MODE_VISIBLE_SCREEN = "visible_screen"
CAPTURE_MODE_CLIENT_AREA = "client_area"
CAPTURE_MODE_PRINT_WINDOW = "print_window"
PHYSICAL_CLICK_CAPTURE_MODES = frozenset(
    {
        CAPTURE_MODE_WINDOW_VISIBLE_SCREEN,
        CAPTURE_MODE_VISIBLE_SCREEN,
        CAPTURE_MODE_CLIENT_AREA,
    }
)

ERROR_WINDOW_NORMALIZATION_FAILED = "WECHAT_UI_WINDOW_NORMALIZATION_FAILED"
ERROR_LAYOUT_UNRESOLVED = "WECHAT_UI_LAYOUT_UNRESOLVED"
ERROR_LAYOUT_STALE = "WECHAT_UI_LAYOUT_STALE"
ERROR_COORDINATE_MAPPING_INVALID = "WECHAT_UI_COORDINATE_MAPPING_INVALID"
ERROR_STARTUP_CALIBRATION_FAILED = "WECHAT_UI_STARTUP_CALIBRATION_FAILED"
STARTUP_CALIBRATION_SCHEMA_VERSION = "0.9.26"

REQUIRED_LAYOUT_REGION_NAMES = (
    "left_nav_bounds",
    "sidebar_bounds",
    "sidebar_header_bounds",
    "session_list_bounds",
    "chat_header_bounds",
    "message_viewport_bounds",
    "toolbar_bounds",
    "input_bounds",
)
POPUP_LAYOUT_REGION_NAMES = ("surface_bounds",)
ADD_FRIEND_ENTRY_LAYOUT_REGION_NAMES = (
    "left_nav_bounds",
    "sidebar_bounds",
    "sidebar_header_bounds",
)


class LayoutSnapshotError(RuntimeError):
    """Raised when a frame cannot safely be used for a physical action."""

    def __init__(self, reason: str, *, code: str = ERROR_LAYOUT_UNRESOLVED, details: Mapping[str, Any] | None = None):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.details = dict(details or {})


def normalize_rect(value: Any) -> list[int]:
    if isinstance(value, Mapping):
        if all(key in value for key in ("left", "top", "right", "bottom")):
            raw = [value.get("left"), value.get("top"), value.get("right"), value.get("bottom")]
        elif all(key in value for key in ("x", "y", "width", "height")):
            left = int(value.get("x") or 0)
            top = int(value.get("y") or 0)
            raw = [left, top, left + int(value.get("width") or 0), top + int(value.get("height") or 0)]
        else:
            raw = []
    else:
        raw = list(value or []) if isinstance(value, (list, tuple)) else []
    if len(raw) < 4:
        return [0, 0, 0, 0]
    left, top, right, bottom = [int(float(item or 0)) for item in raw[:4]]
    return [min(left, right), min(top, bottom), max(left, right), max(top, bottom)]


def rect_size(rect: Any) -> tuple[int, int]:
    normalized = normalize_rect(rect)
    return max(0, normalized[2] - normalized[0]), max(0, normalized[3] - normalized[1])


def point_in_bounds(point: Any, bounds: Any) -> bool:
    values = list(point or []) if isinstance(point, (list, tuple)) else []
    if len(values) < 2:
        return False
    left, top, right, bottom = normalize_rect(bounds)
    return left <= int(values[0]) <= right and top <= int(values[1]) <= bottom


def required_region(snapshot: Mapping[str, Any] | None, name: str) -> list[int]:
    value = snapshot if isinstance(snapshot, Mapping) else {}
    bounds = normalize_rect(value.get(str(name or "")))
    if not bool(value.get("valid")) or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        raise LayoutSnapshotError(
            f"layout_region_missing:{name}",
            code=ERROR_LAYOUT_UNRESOLVED,
            details={"region": str(name or "")},
        )
    return bounds


def input_text_detection_bounds(snapshot: Mapping[str, Any] | None) -> list[int]:
    """Return the text-only draft probe inside the broader input surface.

    ``input_bounds`` remains the calibrated click surface.  Current WeChat
    places its emoji/file/voice toolbar along the bottom of that surface, so a
    draft detector must not inspect pixels all the way to the toolbar edge.
    Insets scale from the calibrated panel instead of using screen/DPI pixels.
    """

    input_bounds = required_region(snapshot, "input_bounds")
    message_bounds = required_region(snapshot, "message_viewport_bounds")
    toolbar_bounds = required_region(snapshot, "toolbar_bounds")
    left, top, right, bottom = input_bounds
    input_width = max(1, right - left)
    input_height = max(1, bottom - top)
    panel_height = max(
        input_height,
        int(toolbar_bounds[3]) - int(message_bounds[3]),
    )
    horizontal_guard = max(4, min(18, int(round(input_width * 0.025))))
    top_guard = max(2, min(8, int(round(panel_height * 0.015))))
    toolbar_guard = max(8, min(24, int(round(panel_height * 0.08))))
    text_bounds = [
        left + horizontal_guard,
        top + top_guard,
        right - horizontal_guard,
        bottom - toolbar_guard,
    ]
    if (
        text_bounds[2] - text_bounds[0] < 120
        or text_bounds[3] - text_bounds[1] < 32
    ):
        raise LayoutSnapshotError(
            "input_text_detection_region_too_small",
            code=ERROR_LAYOUT_UNRESOLVED,
            details={
                "input_bounds": input_bounds,
                "text_bounds": text_bounds,
            },
        )
    return text_bounds


def clamp_point(point: Any, bounds: Any) -> list[int]:
    values = list(point or []) if isinstance(point, (list, tuple)) else []
    if len(values) < 2:
        raise LayoutSnapshotError("target_point_missing", code=ERROR_COORDINATE_MAPPING_INVALID)
    left, top, right, bottom = normalize_rect(bounds)
    if right <= left or bottom <= top:
        raise LayoutSnapshotError("target_bounds_invalid", code=ERROR_COORDINATE_MAPPING_INVALID)
    return [
        max(left, min(right, int(values[0]))),
        max(top, min(bottom, int(values[1]))),
    ]


def _normalize_origin(value: Any) -> list[int] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    return [int(float(value[0] or 0)), int(float(value[1] or 0))]


def _normalize_regions(regions: Mapping[str, Any] | None) -> dict[str, list[int]]:
    return {
        str(name): normalize_rect(value)
        for name, value in dict(regions or {}).items()
    }


def validate_layout_regions(
    regions: Mapping[str, Any] | None,
    *,
    image_size: tuple[int, int] | list[int],
    required_region_names: tuple[str, ...] = REQUIRED_LAYOUT_REGION_NAMES,
) -> dict[str, Any]:
    width = int(image_size[0] or 0) if len(image_size) >= 1 else 0
    height = int(image_size[1] or 0) if len(image_size) >= 2 else 0
    normalized = _normalize_regions(regions)
    required_names = tuple(str(name) for name in required_region_names)
    missing = [name for name in required_names if name not in normalized]
    invalid: list[str] = []
    for name, bounds in normalized.items():
        left, top, right, bottom = bounds
        if right <= left or bottom <= top:
            invalid.append(name)
            continue
        if left < 0 or top < 0 or right > width or bottom > height:
            invalid.append(name)
    conflicts: list[str] = []

    def contains(parent: str, child: str) -> bool:
        parent_rect = normalized.get(parent)
        child_rect = normalized.get(child)
        if not parent_rect or not child_rect:
            return False
        return (
            parent_rect[0] <= child_rect[0]
            and parent_rect[1] <= child_rect[1]
            and parent_rect[2] >= child_rect[2]
            and parent_rect[3] >= child_rect[3]
        )

    if set(REQUIRED_LAYOUT_REGION_NAMES).issubset(required_names):
        for parent, child in (
            ("sidebar_bounds", "sidebar_header_bounds"),
            ("sidebar_bounds", "session_list_bounds"),
        ):
            if parent in normalized and child in normalized and not contains(parent, child):
                conflicts.append(f"{child}_outside_{parent}")
        if "sidebar_header_bounds" in normalized and "session_list_bounds" in normalized:
            if normalized["sidebar_header_bounds"][3] > normalized["session_list_bounds"][1]:
                conflicts.append("sidebar_header_session_list_overlap")
        if "chat_header_bounds" in normalized and "message_viewport_bounds" in normalized:
            if normalized["chat_header_bounds"][3] > normalized["message_viewport_bounds"][1]:
                conflicts.append("chat_header_message_viewport_overlap")
        if "message_viewport_bounds" in normalized and "input_bounds" in normalized:
            if normalized["message_viewport_bounds"][3] > normalized["input_bounds"][1]:
                conflicts.append("message_viewport_input_overlap")
    return {
        "ok": bool(width > 0 and height > 0 and not missing and not invalid and not conflicts),
        "regions": normalized,
        "missing": missing,
        "invalid": invalid,
        "conflicts": conflicts,
        "image_size": [width, height],
        "required_region_names": list(required_names),
    }


def _pixel_luma(pixel: Any) -> float:
    try:
        red, green, blue = [float(value) for value in pixel[:3]]
    except Exception:
        return 0.0
    return (red * 0.299) + (green * 0.587) + (blue * 0.114)


def _vertical_edge_candidates(image: Any) -> list[tuple[int, float]]:
    """Find stable vertical separators from the captured pixels.

    This is intentionally independent from WeChat's old 980px geometry. A
    separator is accepted only when its edge signal is present at several
    heights, which keeps text glyphs and the search icon from becoming layout
    boundaries.
    """

    if image is None or not hasattr(image, "size") or not hasattr(image, "getpixel"):
        return []
    width, height = [int(value or 0) for value in image.size[:2]]
    if width < 320 or height < 240:
        return []
    # A real WeChat column separator spans the header, message list and input
    # areas.  Repeated chat bubbles can share one x-coordinate across several
    # message rows, so sampling only the middle of the viewport can mistake a
    # stack of aligned bubbles for the sidebar boundary.  Include the stable
    # top/bottom chrome and require broad vertical coverage.
    sample_rows = sorted(
        {
            max(1, min(height - 2, int(height * ratio)))
            for ratio in (0.04, 0.10, 0.18, 0.34, 0.52, 0.70, 0.84, 0.94)
        }
    )
    def strip_luma(x_start: int, x_end: int, y: int) -> float:
        values = [
            _pixel_luma(image.getpixel((sample_x, y)))
            for sample_x in range(x_start, x_end)
        ]
        return sum(values) / max(1, len(values))

    scores: list[tuple[int, float]] = []
    for x in range(48, max(49, width - 48)):
        row_scores = []
        for y in sample_rows:
            try:
                # Native WeChat separators are a soft transition spread over
                # several pixels, especially after Windows DPI composition.
                # Compare the surfaces on both sides instead of two individual
                # pixels so anti-aliasing cannot erase the structural edge.
                left = strip_luma(x - 4, x - 1, y)
                right = strip_luma(x + 2, x + 5, y)
                row_scores.append(abs(left - right))
            except Exception:
                row_scores.append(0.0)
        stable_rows = sum(1 for score in row_scores if score >= 5.0)
        stable_signal = float(median(row_scores)) if row_scores else 0.0
        score = stable_signal + (stable_rows * 4.0)
        if stable_rows >= 6 and stable_signal >= 5.0 and score >= 28.0:
            scores.append((x, score))
    clusters: list[list[tuple[int, float]]] = []
    for item in sorted(scores):
        if not clusters or item[0] - clusters[-1][-1][0] > 8:
            clusters.append([item])
        else:
            clusters[-1].append(item)
    result = []
    for cluster in clusters:
        x = int(round(float(median([item[0] for item in cluster]))))
        score = max(item[1] for item in cluster)
        result.append((x, round(score, 3)))
    return sorted(result, key=lambda item: item[1], reverse=True)


def _horizontal_edge_candidates(image: Any, *, left: int, right: int) -> list[tuple[int, float]]:
    if image is None or not hasattr(image, "size") or not hasattr(image, "getpixel"):
        return []
    width, height = [int(value or 0) for value in image.size[:2]]
    left = max(0, min(width - 1, int(left)))
    right = max(left + 1, min(width, int(right)))
    sample_columns = sorted(
        {
            max(left + 1, min(right - 2, int(left + (right - left) * ratio)))
            for ratio in (0.18, 0.42, 0.68, 0.86)
        }
    )
    scores: list[tuple[int, float]] = []
    for y in range(32, max(33, height - 32)):
        row_scores = []
        for x in sample_columns:
            try:
                row_scores.append(
                    abs(
                        _pixel_luma(image.getpixel((x, y - 1)))
                        - _pixel_luma(image.getpixel((x, y + 1)))
                    )
                )
            except Exception:
                row_scores.append(0.0)
        stable_columns = sum(1 for score in row_scores if score >= 14.0)
        score = (sum(row_scores) / max(1, len(row_scores))) + (stable_columns * 7.0)
        if stable_columns >= 2 and score >= 24.0:
            scores.append((y, score))
    clusters: list[list[tuple[int, float]]] = []
    for item in sorted(scores):
        if not clusters or item[0] - clusters[-1][-1][0] > 8:
            clusters.append([item])
        else:
            clusters[-1].append(item)
    result = []
    for cluster in clusters:
        y, score = max(cluster, key=lambda item: item[1])
        result.append((y, round(score, 3)))
    return sorted(result, key=lambda item: item[1], reverse=True)


def _full_width_horizontal_separator_candidates(
    image: Any,
    *,
    left: int,
    right: int,
) -> list[tuple[int, float]]:
    """Find soft separators that span nearly the whole supplied region.

    WeChat's input-panel border is intentionally low contrast. Message bubbles
    and selected-session rows can have much stronger edges, but they cover only
    part of the panel. Requiring broad horizontal coverage preserves the old
    input/send semantics while replacing only their region input.
    """

    if image is None or not hasattr(image, "size") or not hasattr(image, "getpixel"):
        return []
    width, height = [int(value or 0) for value in image.size[:2]]
    left = max(0, min(width - 1, int(left)))
    right = max(left + 1, min(width, int(right)))
    sample_columns = sorted(
        {
            max(left + 1, min(right - 2, int(left + (right - left) * ratio)))
            for ratio in (0.05, 0.18, 0.34, 0.50, 0.66, 0.82, 0.95)
        }
    )
    scores: list[tuple[int, float]] = []
    required_columns = max(3, len(sample_columns) - 1)
    for y in range(3, max(4, height - 3)):
        deltas: list[float] = []
        for x in sample_columns:
            try:
                deltas.append(
                    abs(
                        _pixel_luma(image.getpixel((x, y - 1)))
                        - _pixel_luma(image.getpixel((x, y + 1)))
                    )
                )
            except Exception:
                deltas.append(0.0)
        covered = sum(1 for value in deltas if value >= 4.0)
        if covered < required_columns:
            continue
        stable_signal = float(median(deltas)) if deltas else 0.0
        if stable_signal < 4.0:
            continue
        scores.append((y, stable_signal + (covered * 5.0)))
    clusters: list[list[tuple[int, float]]] = []
    for item in sorted(scores):
        if not clusters or item[0] - clusters[-1][-1][0] > 4:
            clusters.append([item])
        else:
            clusters[-1].append(item)
    return sorted(
        [max(cluster, key=lambda item: item[1]) for cluster in clusters],
        key=lambda item: item[0],
    )


def _qualified_edge_confidence(score: float, *, threshold: float) -> float:
    """Normalize an already-qualified structural edge without requiring black borders.

    WeChat separators are intentionally low contrast.  Once an edge has passed
    the multi-row/multi-column stability test, confidence is based on how far it
    clears that test rather than on an impossible 0-255 black/white contrast.
    """

    margin = max(0.0, float(score) - float(threshold))
    return min(0.97, 0.78 + (margin / 180.0))


def _topmost_sidebar_operation_row_anchors(
    anchors: list[dict[str, Any]],
    *,
    nav_boundary_x: int,
    sidebar_boundary_x: int,
) -> list[dict[str, Any]]:
    """Keep only search anchors on the topmost measured sidebar row.

    Session previews can legitimately contain the word ``搜索``. They are
    below the sidebar operation row and must not compete with its search box.
    Anchors on the same measured row remain ambiguous and fail closed.
    """

    sidebar_anchors: list[dict[str, Any]] = []
    for anchor in anchors:
        bounds = normalize_rect(anchor.get("bounds"))
        center_x = int((bounds[0] + bounds[2]) / 2)
        if (
            bounds[2] > bounds[0]
            and bounds[3] > bounds[1]
            and nav_boundary_x < center_x < sidebar_boundary_x
        ):
            sidebar_anchors.append({**anchor, "bounds": bounds})
    if not sidebar_anchors:
        return []
    topmost = min(
        sidebar_anchors,
        key=lambda anchor: (
            int((anchor["bounds"][1] + anchor["bounds"][3]) / 2),
            int(anchor["bounds"][1]),
        ),
    )
    top_bounds = list(topmost["bounds"])
    top_center_y = int((top_bounds[1] + top_bounds[3]) / 2)
    top_height = max(1, top_bounds[3] - top_bounds[1])
    operation_row: list[dict[str, Any]] = []
    for anchor in sidebar_anchors:
        bounds = list(anchor["bounds"])
        center_y = int((bounds[1] + bounds[3]) / 2)
        height = max(1, bounds[3] - bounds[1])
        vertically_overlaps = min(top_bounds[3], bounds[3]) > max(
            top_bounds[1], bounds[1]
        )
        same_measured_row = abs(center_y - top_center_y) <= max(
            2,
            int(max(top_height, height) * 0.75),
        )
        if vertically_overlaps or same_measured_row:
            operation_row.append(anchor)
    return operation_row


def _prefer_boundary_pairs_containing_search_anchors(
    pairs: list[tuple[float, tuple[int, float], tuple[int, float]]],
    search_anchors: list[dict[str, Any]],
) -> list[tuple[float, tuple[int, float], tuple[int, float]]]:
    """Reject avatar/text edges that would place the search box outside the sidebar."""

    for search_anchor in sorted(
        search_anchors,
        key=lambda anchor: (
            int((anchor["bounds"][1] + anchor["bounds"][3]) / 2),
            int(anchor["bounds"][0]),
        ),
    ):
        anchor_bounds = list(search_anchor["bounds"])
        anchor_height = max(1, anchor_bounds[3] - anchor_bounds[1])
        separator_clearance = max(6, int(anchor_height * 0.5))
        compatible_pairs = [
            item
            for item in pairs
            if int(item[1][0]) <= anchor_bounds[0] - separator_clearance
            and int(item[2][0]) >= anchor_bounds[2] + separator_clearance
        ]
        if compatible_pairs:
            return compatible_pairs
    return pairs


def build_add_friend_entry_layout_regions(
    image: Any,
    *,
    search_anchor_items: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve only the current-frame regions needed to open Add Friend.

    The add-friend entry lives in the sidebar header. Requiring the chat
    viewport and input area here couples an unrelated action to message-panel
    content and makes a wide/empty chat fail before plus recognition starts.
    """

    if image is None or not hasattr(image, "size"):
        return {
            "ok": False,
            "regions": {},
            "anchors": [],
            "confidence": 0.0,
            "conflicts": ["image_missing"],
            "vertical_candidates": [],
        }
    width, height = [int(value or 0) for value in image.size[:2]]
    verticals = _vertical_edge_candidates(image)
    # Search semantics belong to the proven 0.9.20 recognizer. This module
    # only selects the topmost operation row inside pixel-derived boundaries.
    raw_search_items = search_anchor_items or []
    search_anchors: list[dict[str, Any]] = []
    for item in raw_search_items or []:
        if not isinstance(item, Mapping):
            continue
        text = "".join(str(item.get("text") or "").split())
        bounds = normalize_rect(
            [item.get("left"), item.get("top"), item.get("right"), item.get("bottom")]
        )
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            continue
        search_anchors.append(
            {
                "name": "search_text",
                "text": text,
                "bounds": bounds,
                "confidence": float(item.get("confidence") or 0.0),
            }
        )
    pairs: list[tuple[float, tuple[int, float], tuple[int, float]]] = []
    for nav in verticals:
        if not (int(width * 0.025) <= nav[0] <= int(width * 0.20)):
            continue
        for main in verticals:
            sidebar_width = main[0] - nav[0]
            if not (int(width * 0.16) <= sidebar_width <= int(width * 0.48)):
                continue
            if not (int(width * 0.24) <= main[0] <= int(width * 0.62)):
                continue
            pairs.append((float(nav[1]) + float(main[1]), nav, main))
    if not pairs:
        return {
            "ok": False,
            "regions": {},
            "anchors": search_anchors,
            "confidence": 0.0,
            "conflicts": ["sidebar_boundary_pair_unresolved"],
            "vertical_candidates": [{"x": x, "score": score} for x, score in verticals[:12]],
        }
    # Repeated, aligned session avatars can produce stronger vertical signals
    # than the real nav separator. Use the already-proven top search OCR bounds
    # to reject any candidate pair whose left edge cuts through (or sits almost
    # against) the search glyph and whose right edge cuts through the same row.
    # The selected coordinates still come from current-frame pixels only.
    pairs = _prefer_boundary_pairs_containing_search_anchors(
        pairs,
        search_anchors,
    )
    ranked_pairs = sorted(pairs, key=lambda item: item[0], reverse=True)
    _score, nav_boundary, main_boundary = ranked_pairs[0]
    position_tolerance = max(18, int(width * 0.018))
    same_nav_pairs = [
        item
        for item in pairs
        if abs(int(item[1][0]) - int(nav_boundary[0])) <= position_tolerance
    ]
    if same_nav_pairs:
        _score, nav_boundary, main_boundary = min(
            same_nav_pairs,
            key=lambda item: int(item[2][0]),
        )
    materially_different = [
        item
        for item in ranked_pairs[1:]
        if abs(item[0] - _score) <= max(6.0, abs(_score) * 0.08)
        and abs(item[1][0] - nav_boundary[0]) > position_tolerance
    ]
    if materially_different:
        return {
            "ok": False,
            "regions": {},
            "anchors": search_anchors,
            "confidence": 0.0,
            "conflicts": ["sidebar_boundary_pair_ambiguous"],
            "vertical_candidates": [{"x": x, "score": score} for x, score in verticals[:12]],
        }

    sidebar_search_anchors = _topmost_sidebar_operation_row_anchors(
        search_anchors,
        nav_boundary_x=int(nav_boundary[0]),
        sidebar_boundary_x=int(main_boundary[0]),
    )
    if len(sidebar_search_anchors) != 1:
        return {
            "ok": False,
            "regions": {},
            "anchors": sidebar_search_anchors,
            "confidence": 0.0,
            "conflicts": [
                "search_anchor_missing"
                if not sidebar_search_anchors
                else "search_anchor_ambiguous"
            ],
            "vertical_candidates": [
                {"x": x, "score": score} for x, score in verticals[:12]
            ],
        }

    search_anchor = sidebar_search_anchors[0]
    search_bounds = list(search_anchor["bounds"])

    search_height = max(1, search_bounds[3] - search_bounds[1])
    # There is deliberately no dependency on a horizontal header separator:
    # when a non-first conversation is selected, native WeChat can render no
    # clear line above the first session. The OCR search glyph defines the
    # current-frame operation row; expand it by its own measured height.
    operation_band_padding = max(8, int(search_height * 1.5))
    operation_band_top = max(0, search_bounds[1] - operation_band_padding)
    operation_band_bottom = min(height, search_bounds[3] + operation_band_padding)
    regions = {
        "left_nav_bounds": [0, 0, nav_boundary[0], height],
        "sidebar_bounds": [nav_boundary[0], 0, main_boundary[0], height],
        "sidebar_header_bounds": [
            nav_boundary[0],
            operation_band_top,
            main_boundary[0],
            operation_band_bottom,
        ],
    }
    validation = validate_layout_regions(
        regions,
        image_size=(width, height),
        required_region_names=ADD_FRIEND_ENTRY_LAYOUT_REGION_NAMES,
    )
    confidence = min(
        _qualified_edge_confidence(nav_boundary[1], threshold=28.0),
        _qualified_edge_confidence(main_boundary[1], threshold=28.0),
        max(0.75, min(0.99, float(search_anchor.get("confidence") or 0.0))),
    )
    conflicts = (
        list(validation.get("missing") or [])
        + list(validation.get("invalid") or [])
        + list(validation.get("conflicts") or [])
    )
    return {
        "ok": bool(validation.get("ok") and not conflicts),
        "regions": regions,
        "anchors": [
            search_anchor,
            {"name": "nav_separator", "x": nav_boundary[0], "score": nav_boundary[1]},
            {"name": "sidebar_separator", "x": main_boundary[0], "score": main_boundary[1]},
            {
                "name": "search_operation_band",
                "bounds": list(regions["sidebar_header_bounds"]),
                "source": "search_anchor_measured_height",
            },
        ],
        "confidence": round(confidence, 3),
        "conflicts": conflicts,
        "vertical_candidates": [{"x": x, "score": score} for x, score in verticals[:12]],
    }


def build_structural_layout_regions(
    image: Any,
    *,
    ocr_items: list[Mapping[str, Any]] | None = None,
    search_anchor_items: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build regions from current pixels and anchors, never from a reference size."""

    if image is None or not hasattr(image, "size"):
        return {
            "ok": False,
            "regions": {},
            "anchors": [],
            "confidence": 0.0,
            "conflicts": ["image_missing"],
        }
    width, height = [int(value or 0) for value in image.size[:2]]
    verticals = _vertical_edge_candidates(image)
    explicit_search_items = search_anchor_items if search_anchor_items is not None else []
    search_anchor_candidates: list[dict[str, Any]] = []
    for item in explicit_search_items:
        if not isinstance(item, Mapping):
            continue
        bounds = normalize_rect(
            [item.get("left"), item.get("top"), item.get("right"), item.get("bottom")]
        )
        if bounds[2] > bounds[0] and bounds[3] > bounds[1]:
            search_anchor_candidates.append(
                {
                    "name": "sidebar_search_anchor",
                    "text": str(item.get("text") or ""),
                    "bounds": bounds,
                    "confidence": float(item.get("confidence") or 0.0),
                }
            )
    selected_verticals = sorted(
        [
            item
            for item in verticals
            if max(20, int(width * 0.02)) <= item[0] <= width - max(80, int(width * 0.08))
        ],
        key=lambda item: item[0],
    )
    conflicts: list[str] = []
    if len(selected_verticals) < 1:
        conflicts.append("sidebar_boundary_missing")
        return {
            "ok": False,
            "regions": {},
            "anchors": [],
            "confidence": 0.0,
            "conflicts": conflicts,
        }
    # Select a structurally valid nav/sidebar pair.  The ratios below only
    # reject impossible edges; the actual boundaries always come from pixels.
    pairs: list[tuple[float, tuple[int, float], tuple[int, float]]] = []
    for nav in selected_verticals:
        if not (int(width * 0.025) <= nav[0] <= int(width * 0.20)):
            continue
        for main in selected_verticals:
            sidebar_width = main[0] - nav[0]
            if sidebar_width <= 0:
                continue
            if not (int(width * 0.16) <= sidebar_width <= int(width * 0.48)):
                continue
            if not (int(width * 0.24) <= main[0] <= int(width * 0.62)):
                continue
            pair_score = float(nav[1]) + float(main[1]) + (main[0] / max(1, width))
            pairs.append((pair_score, nav, main))
    if not pairs:
        conflicts.append("left_nav_boundary_missing")
        conflicts.append("sidebar_boundary_pair_unresolved")
        return {
            "ok": False,
            "regions": {},
            "anchors": [],
            "confidence": 0.0,
            "conflicts": conflicts,
        }
    pairs = _prefer_boundary_pairs_containing_search_anchors(
        pairs,
        search_anchor_candidates,
    )
    ranked_pairs = sorted(pairs, key=lambda item: item[0], reverse=True)
    if search_anchor_candidates:
        _pair_score, nav_boundary, main_boundary = ranked_pairs[0]
    else:
        # The left-most qualified pair is the stable outer shell. Session
        # avatars and text columns can form stronger verticals farther right;
        # signal strength alone must never promote them to navigation edges.
        _pair_score, nav_boundary, main_boundary = min(
            pairs,
            key=lambda item: (int(item[1][0]), int(item[2][0])),
        )
    # A layout snapshot is an action authority, not a best-effort guess.  When
    # two materially different separator pairs have nearly the same pixel
    # evidence, selecting the right-most/strongest one can turn a chat-panel
    # edge into the sidebar boundary.  Keep the frame readable for diagnostics
    # but refuse to make it executable until a fresh frame is unambiguous.
    pair_score_tolerance = max(6.0, abs(float(_pair_score)) * 0.08)
    pair_position_tolerance = max(18, int(width * 0.018))
    competing_pairs = [
        item
        for item in ranked_pairs[1:]
        if abs(float(item[0]) - float(_pair_score)) <= pair_score_tolerance
        and (
            abs(int(item[1][0]) - int(nav_boundary[0])) > pair_position_tolerance
            or abs(int(item[2][0]) - int(main_boundary[0])) > pair_position_tolerance
        )
    ]
    # A chat panel can contain another full-height vertical edge to the right
    # of the real sidebar separator.  If the left navigation edge is the same,
    # the first qualified separator after it is the sidebar boundary; this is
    # structural disambiguation, not a reference coordinate.  Distinct nav
    # candidates remain genuinely ambiguous and must fail closed.
    same_nav_pairs = [
        item
        for item in pairs
        if abs(int(item[1][0]) - int(nav_boundary[0])) <= pair_position_tolerance
    ]
    if same_nav_pairs:
        _pair_score, nav_boundary, main_boundary = min(
            same_nav_pairs,
            key=lambda item: int(item[2][0]),
        )
    unresolved_pairs = [
        item
        for item in competing_pairs
        if abs(int(item[1][0]) - int(nav_boundary[0])) > pair_position_tolerance
    ]
    if unresolved_pairs and search_anchor_candidates:
        conflicts.append("sidebar_boundary_pair_ambiguous")
        return {
            "ok": False,
            "regions": {},
            "anchors": [],
            "confidence": 0.0,
            "conflicts": conflicts,
            "vertical_candidates": [
                {"x": x, "score": score} for x, score in verticals[:12]
            ],
        }
    sidebar_header_bottom_candidates = _horizontal_edge_candidates(
        image,
        left=nav_boundary[0],
        right=main_boundary[0],
    )
    chat_header_bottom_candidates = _horizontal_edge_candidates(
        image,
        left=main_boundary[0],
        right=width,
    )
    upper_limit = max(80, int(height * 0.30))
    sidebar_header_candidates = [
        item for item in sidebar_header_bottom_candidates
        if int(height * 0.055) <= item[0] <= min(height - max(80, int(height * 0.10)), upper_limit)
    ]
    chat_header_candidates = [
        item for item in chat_header_bottom_candidates
        if int(height * 0.055) <= item[0] <= min(height - max(96, int(height * 0.12)), upper_limit)
    ]
    search_anchors = _topmost_sidebar_operation_row_anchors(
        search_anchor_candidates,
        nav_boundary_x=int(nav_boundary[0]),
        sidebar_boundary_x=int(main_boundary[0]),
    )
    if len(search_anchors) > 1:
        conflicts.append("sidebar_search_anchor_ambiguous")
        return {
            "ok": False,
            "regions": {},
            "anchors": search_anchors,
            "confidence": 0.0,
            "conflicts": conflicts,
        }
    # The gray-v0.9.20 executable plus detector is also a raw-pixel anchor for
    # the top operation row. This avoids treating a selected conversation row
    # or avatar edge as the header when OCR misses ``Q/O/0搜索``.
    from apps.wechat_ai_customer_service.adapters.add_friend_layout import (
        vision_plus_icon_candidates,
    )

    plus_candidates = vision_plus_icon_candidates(
        image,
        (width, height),
        search_bounds=[nav_boundary[0], 0, main_boundary[0], upper_limit],
    )
    right_zone_left = nav_boundary[0] + int(
        (main_boundary[0] - nav_boundary[0]) * 0.70
    )
    operation_plus_candidates = [
        item
        for item in plus_candidates
        if int((item.get("point") or [0, 0])[0]) >= right_zone_left
    ]
    operation_plus = (
        max(
            operation_plus_candidates,
            key=lambda item: (
                int((item.get("point") or [0, 0])[0]),
                float(item.get("confidence") or 0.0),
            ),
        )
        if operation_plus_candidates
        else None
    )
    if search_anchors:
        search_bounds = search_anchors[0]["bounds"]
        search_height = max(1, search_bounds[3] - search_bounds[1])
        shared_header_bottom = min(
            height,
            search_bounds[3] + max(8, int(search_height * 1.2)),
        )
        sidebar_header_bottom = shared_header_bottom
        chat_header_bottom = shared_header_bottom
        header_confidence = max(
            0.75,
            min(0.99, float(search_anchors[0].get("confidence") or 0.0)),
        )
    elif operation_plus is not None:
        plus_point = [int(value) for value in operation_plus["point"]]
        sidebar_header_bottom = min(height, max(1, plus_point[1] * 2 + 2))
        chat_header_bottom = sidebar_header_bottom
        header_confidence = min(
            0.97, max(0.80, float(operation_plus.get("confidence") or 0.0))
        )
    else:
        # Without an OCR anchor, only a separator observed at the same height
        # in both panels may define the shared WeChat header. A selected row or
        # a chat icon edge from one panel alone is never enough.
        shared_header_pairs = [
            (left_item, right_item)
            for left_item in sidebar_header_candidates
            for right_item in chat_header_candidates
            if abs(int(left_item[0]) - int(right_item[0])) <= 6
        ]
        if shared_header_pairs:
            sidebar_edge, chat_edge = min(
                shared_header_pairs,
                key=lambda pair: max(int(pair[0][0]), int(pair[1][0])),
            )
            sidebar_header_bottom = int(round((sidebar_edge[0] + chat_edge[0]) / 2))
            chat_header_bottom = sidebar_header_bottom
            header_confidence = min(
                _qualified_edge_confidence(sidebar_edge[1], threshold=24.0),
                _qualified_edge_confidence(chat_edge[1], threshold=24.0),
            )
        elif sidebar_header_candidates:
            # A selected non-first conversation may render no matching line
            # in the chat panel. The top-most strong boundary in the already
            # proven sidebar shell is the search operation row; it is raw
            # pixel structure, not a fixed y coordinate.
            sidebar_edge = min(sidebar_header_candidates, key=lambda item: int(item[0]))
            sidebar_header_bottom = int(sidebar_edge[0])
            chat_header_bottom = sidebar_header_bottom
            header_confidence = _qualified_edge_confidence(sidebar_edge[1], threshold=24.0)
        else:
            conflicts.append("shared_header_boundary_missing")
            return {
                "ok": False,
                "regions": {},
                "anchors": [],
                "confidence": 0.0,
                "conflicts": conflicts,
            }
    input_top_candidates = [
        item
        for item in _full_width_horizontal_separator_candidates(
            image,
            left=main_boundary[0],
            right=width,
        )
        if max(chat_header_bottom + 24, int(height * 0.50))
        <= item[0]
        <= height - max(28, int(height * 0.035))
    ]
    if not input_top_candidates:
        conflicts.append("input_boundary_missing")
        return {
            "ok": False,
            "regions": {},
            "anchors": [],
            "confidence": 0.0,
            "conflicts": conflicts,
        }
    input_top = min(input_top_candidates, key=lambda item: item[0])[0]
    if input_top <= chat_header_bottom or input_top >= height:
        conflicts.append("chat_regions_conflict")
    input_panel_width = max(1, width - main_boundary[0])
    input_panel_height = max(1, height - input_top)
    toolbar_tops: list[int] = []
    for item in ocr_items or []:
        text = "".join(str(item.get("text") or "").split())
        if text not in {"发送", "工具"}:
            continue
        bounds = normalize_rect(
            [item.get("left"), item.get("top"), item.get("right"), item.get("bottom")]
        )
        center_x = int((bounds[0] + bounds[2]) / 2)
        center_y = int((bounds[1] + bounds[3]) / 2)
        if main_boundary[0] <= center_x <= width and input_top <= center_y <= height:
            toolbar_tops.append(bounds[1])
    # This is region geometry only.  The old draft detector remains unchanged;
    # it receives the current editable surface instead of the entire panel.
    # When OCR exposes a toolbar control, its measured top is authoritative.
    # Otherwise use the current panel's own proportions, never screen pixels or
    # a 1920 reference coordinate.
    editable_bottom = (
        min(toolbar_tops) - max(1, int(input_panel_height * 0.01))
        if toolbar_tops
        else input_top + int(input_panel_height * 0.56)
    )
    editable_top = input_top + max(1, int(input_panel_height * 0.04))
    editable_left = main_boundary[0] + max(1, int(input_panel_width * 0.01))
    editable_right = width - max(1, int(input_panel_width * 0.16))
    editable_bottom = max(editable_top + 1, min(height, editable_bottom))
    editable_right = max(editable_left + 1, min(width, editable_right))
    regions = {
        "left_nav_bounds": [0, 0, nav_boundary[0], height],
        "sidebar_bounds": [nav_boundary[0], 0, main_boundary[0], height],
        "sidebar_header_bounds": [nav_boundary[0], 0, main_boundary[0], sidebar_header_bottom],
        "session_list_bounds": [nav_boundary[0], sidebar_header_bottom, main_boundary[0], height],
        "chat_header_bounds": [main_boundary[0], 0, width, chat_header_bottom],
        "message_viewport_bounds": [main_boundary[0], chat_header_bottom, width, input_top],
        "toolbar_bounds": [main_boundary[0], editable_bottom, width, height],
        "input_bounds": [editable_left, editable_top, editable_right, editable_bottom],
    }
    validation = validate_layout_regions(regions, image_size=(width, height))
    confidence_parts = [
        _qualified_edge_confidence(main_boundary[1], threshold=28.0),
        _qualified_edge_confidence(nav_boundary[1], threshold=28.0),
        header_confidence,
        _qualified_edge_confidence(max(input_top_candidates, key=lambda item: item[1])[1], threshold=24.0),
    ]
    anchors = [
        {"name": "nav_separator", "x": nav_boundary[0], "score": nav_boundary[1]},
        {"name": "sidebar_separator", "x": main_boundary[0], "score": main_boundary[1]},
        *search_anchors,
        *(
            [{
                "name": "startup_plus_pixel_anchor",
                "point": list(operation_plus.get("point") or []),
                "bounds": list(operation_plus.get("bounds") or []),
                "confidence": float(operation_plus.get("confidence") or 0.0),
                "source": "gray-v0.9.20 vision_plus_icon",
            }]
            if operation_plus is not None
            else []
        ),
        {
            "name": "input_separator",
            "y": input_top,
            "source": "full_width_soft_separator",
        },
        {
            "name": "input_text_region",
            "bounds": list(regions["input_bounds"]),
            "source": "toolbar_ocr" if toolbar_tops else "current_input_panel_proportion",
        },
    ]
    for item in ocr_items or []:
        text = str(item.get("text") or "").strip()
        if text and any(token in text for token in ("搜索", "发送", "确定", "备注")):
            anchors.append(
                {
                    "name": "ocr_anchor",
                    "text": text,
                    "bounds": [
                        int(float(item.get("left") or 0)),
                        int(float(item.get("top") or 0)),
                        int(float(item.get("right") or 0)),
                        int(float(item.get("bottom") or 0)),
                    ],
                    "confidence": float(item.get("confidence") or 0.0),
                }
            )
    return {
        "ok": bool(validation.get("ok") and not conflicts),
        "regions": regions,
        "anchors": anchors,
        "confidence": round(min(confidence_parts), 3),
        "conflicts": (
            conflicts
            + list(validation.get("missing") or [])
            + list(validation.get("invalid") or [])
            + list(validation.get("conflicts") or [])
        ),
        "vertical_candidates": [{"x": x, "score": score} for x, score in verticals[:12]],
    }


# These values are extracted from the gray-v0.9.20 production geometry on its
# real 980x860 reference window.  That frame has a 41px non-client/title crop,
# leaving the 980x819 visible client image used by startup calibration.  Every
# point below is therefore region-local in that client image; none is a screen
# coordinate and none can authorize a click without a calibration.
REFERENCE_REGION_MAP_V0920: dict[str, dict[str, Any]] = {
    "plus_entry": {
        "region": "sidebar_header_bounds",
        "reference_region_size": [298, 60],
        # gray-v0.9.20 only allowed the raw-pixel vision_plus_icon result to
        # execute.  On its reference client surface the selected icon centre
        # is (349, 70) in the outer frame, i.e. (265, 29) inside the calibrated
        # client sidebar header [84, 0, 382, 60].  The old
        # windows_plus_point helper was diagnostic-only and must not become
        # the executable reference map.
        "point": [265, 29],
        "source": "gray-v0.9.20 vision_plus_icon selected reference-region point",
    },
    "sidebar_search": {
        "region": "sidebar_header_bounds",
        "reference_region_size": [298, 60],
        "point": [38, 23],
        "source": "gray-v0.9.20 search_box_point_for_geometry",
    },
    "session_row_x": {
        "region": "session_list_bounds",
        "reference_region_size": [298, 759],
        "point": [182, 0],
        "source": "gray-v0.9.20 session_click_x_for_geometry",
    },
    "input_focus": {
        "region": "input_bounds",
        "reference_region_size": [498, 85],
        "point": [250, 61],
        "source": "gray-v0.9.20 calculate_send_points.input_point",
    },
    "send_button": {
        "region": "toolbar_bounds",
        "reference_region_size": [598, 72],
        "point": [536, 28],
        "source": "gray-v0.9.20 calculate_send_points.send_point",
    },
}


def map_reference_region_point(
    calibration: Mapping[str, Any],
    reference_name: str,
    *,
    dynamic_axis_value: int | None = None,
) -> dict[str, Any]:
    """Map one v0.9.20 region-local reference into the calibrated region."""

    reference = REFERENCE_REGION_MAP_V0920.get(str(reference_name or ""))
    if reference is None:
        raise LayoutSnapshotError(
            "reference_region_point_unknown",
            code=ERROR_COORDINATE_MAPPING_INVALID,
            details={"reference_name": str(reference_name or "")},
        )
    if not bool(calibration.get("executable")):
        raise LayoutSnapshotError(
            "startup_calibration_not_executable",
            code=ERROR_STARTUP_CALIBRATION_FAILED,
        )
    region_name = str(reference["region"])
    bounds = normalize_rect(calibration.get(region_name))
    region_width, region_height = rect_size(bounds)
    ref_width, ref_height = [max(1, int(value)) for value in reference["reference_region_size"]]
    ref_x, ref_y = [int(value) for value in reference["point"]]
    mapped_x = bounds[0] + int(round((ref_x / ref_width) * region_width))
    mapped_y = bounds[1] + int(round((ref_y / ref_height) * region_height))
    if dynamic_axis_value is not None and reference_name == "session_row_x":
        mapped_y = int(dynamic_axis_value)
    point = clamp_point([mapped_x, mapped_y], bounds)
    return {
        "reference_name": str(reference_name),
        "region_name": region_name,
        "region_bounds": bounds,
        "region_point": [point[0] - bounds[0], point[1] - bounds[1]],
        "image_point": point,
        "calibration_id": str(calibration.get("calibration_id") or ""),
        "source": str(reference.get("source") or ""),
    }


def build_startup_layout_calibration(
    *,
    hwnd: int,
    process_id: int,
    image: Any,
    ocr_items: list[Mapping[str, Any]],
    window_rect: Any,
    client_rect: Any,
    client_screen_origin: Any,
    dpi_scale: float,
    capture_mode: str,
    screenshot_path: str = "",
) -> dict[str, Any]:
    """Build the one executable main-shell calibration for this HWND state."""

    image_size = getattr(image, "size", (0, 0))
    width, height = [int(value or 0) for value in image_size[:2]]
    search_items = []
    for item in ocr_items or []:
        compact = "".join(str(item.get("text") or "").split())
        if "搜索" in compact:
            search_items.append(dict(item))
    layout = build_structural_layout_regions(
        image,
        ocr_items=ocr_items,
        search_anchor_items=search_items,
    )
    regions = dict(layout.get("regions") or {})
    validation = validate_layout_regions(regions, image_size=(width, height))
    conflicts = list(layout.get("conflicts") or [])
    conflicts.extend(str(item) for item in validation.get("missing") or [])
    conflicts.extend(str(item) for item in validation.get("invalid") or [])
    conflicts.extend(str(item) for item in validation.get("conflicts") or [])
    if width < 700 or height < 720:
        conflicts.append("client_surface_below_700x720")
    normalized_client_origin = _normalize_origin(client_screen_origin)
    exact_client_capture = (
        str(capture_mode or "") == CAPTURE_MODE_CLIENT_AREA
        and normalized_client_origin is not None
        and width == int((client_rect or {}).get("width") or width)
        and height == int((client_rect or {}).get("height") or height)
    )
    executable = bool(
        int(hwnd or 0) > 0
        and int(process_id or 0) > 0
        and exact_client_capture
        and layout.get("ok")
        and validation.get("ok")
        and not conflicts
        and float(layout.get("confidence") or 0.0) >= 0.70
    )
    calibration_id = _stable_id(
        "calibration",
        int(hwnd or 0),
        int(process_id or 0),
        normalize_rect(window_rect),
        normalize_rect(client_rect),
        normalized_client_origin,
        round(float(dpi_scale or 1.0), 4),
        width,
        height,
    )
    result = {
        "calibration_id": calibration_id,
        "schema_version": STARTUP_CALIBRATION_SCHEMA_VERSION,
        "hwnd": int(hwnd or 0),
        "process_id": int(process_id or 0),
        "window_rect": normalize_rect(window_rect),
        "client_rect": normalize_rect(client_rect),
        "client_screen_origin": normalized_client_origin,
        "dpi_scale": max(0.01, float(dpi_scale or 1.0)),
        "image_width": width,
        "image_height": height,
        "capture_mode": str(capture_mode or ""),
        **{name: normalize_rect(regions.get(name)) for name in REQUIRED_LAYOUT_REGION_NAMES},
        "anchors": [dict(item) for item in layout.get("anchors") or []],
        "confidence": max(0.0, min(1.0, float(layout.get("confidence") or 0.0))),
        "conflicts": list(dict.fromkeys(conflicts)),
        "calibrated_at": time.time(),
        "executable": executable,
        "screenshot_path": str(screenshot_path or ""),
        "reference_map_revision": "gray-v0.9.20-region-local",
        "error_code": "" if executable else ERROR_STARTUP_CALIBRATION_FAILED,
    }
    return result


def write_startup_layout_calibration(path: Path, calibration: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(calibration), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_startup_layout_calibration(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _stable_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha256(
        "|".join(str(part) for part in parts).encode("utf-8", errors="replace")
    ).hexdigest()[:20]
    return f"{prefix}-{digest}-{uuid.uuid4().hex[:8]}"


def new_frame_id(hwnd: int) -> str:
    return _stable_id("frame", int(hwnd or 0), time.monotonic_ns())


def geometry_signature(
    *,
    hwnd: int,
    window_rect: Any,
    client_rect: Any,
    dpi_scale: float,
    image_size: Any,
    capture_mode: str = "",
    capture_screen_origin: Any = None,
    client_screen_origin: Any = None,
) -> str:
    return _stable_id(
        "geometry",
        int(hwnd or 0),
        normalize_rect(window_rect),
        normalize_rect(client_rect),
        round(float(dpi_scale or 1.0), 4),
        list(image_size or []),
        str(capture_mode or ""),
        _normalize_origin(capture_screen_origin),
        _normalize_origin(client_screen_origin),
    )


def build_layout_snapshot(
    *,
    hwnd: int,
    frame_id: str | None,
    capture_mode: str,
    image_size: tuple[int, int] | list[int],
    capture_screen_origin: Any,
    window_rect: Any,
    client_rect: Any,
    client_screen_origin: Any,
    dpi_scale: float,
    regions: Mapping[str, Any] | None,
    anchors: list[Mapping[str, Any]] | None = None,
    confidence: float = 0.0,
    conflicts: list[str] | None = None,
    executable: bool = False,
    screenshot_path: str = "",
    surface_kind: str = "wechat_main",
    required_region_names: tuple[str, ...] = REQUIRED_LAYOUT_REGION_NAMES,
) -> dict[str, Any]:
    width = int(image_size[0] or 0)
    height = int(image_size[1] or 0)
    normalized_mode = str(capture_mode or "").strip() or CAPTURE_MODE_PRINT_WINDOW
    normalized_capture_origin = _normalize_origin(capture_screen_origin)
    normalized_client_origin = _normalize_origin(client_screen_origin)
    normalized_window_rect = normalize_rect(window_rect)
    normalized_client_rect = normalize_rect(client_rect)
    required_names = tuple(str(name) for name in required_region_names)
    region_validation = validate_layout_regions(
        regions,
        image_size=(width, height),
        required_region_names=required_names,
    )
    normalized_conflicts = [str(item) for item in (conflicts or []) if str(item).strip()]
    can_click = normalized_mode in PHYSICAL_CLICK_CAPTURE_MODES and normalized_capture_origin is not None
    snapshot_id = _stable_id(
        "layout",
        int(hwnd or 0),
        frame_id or "",
        normalized_mode,
        width,
        height,
        normalized_capture_origin,
        normalized_window_rect,
        normalized_client_rect,
        normalized_client_origin,
        round(float(dpi_scale or 1.0), 4),
    )
    valid = bool(
        int(hwnd or 0) > 0
        and width > 0
        and height > 0
        and normalized_client_origin is not None
        and region_validation.get("ok")
        and not normalized_conflicts
        and float(confidence or 0.0) >= 0.70
    )
    normalized_regions = dict(region_validation["regions"])
    result = {
        "layout_snapshot_id": snapshot_id,
        "frame_id": str(frame_id or _stable_id("frame", hwnd, time.monotonic_ns())),
        "hwnd": int(hwnd or 0),
        "capture_mode": normalized_mode,
        "image_width": width,
        "image_height": height,
        "capture_screen_origin_x": normalized_capture_origin[0] if normalized_capture_origin else None,
        "capture_screen_origin_y": normalized_capture_origin[1] if normalized_capture_origin else None,
        "capture_screen_origin": normalized_capture_origin,
        "window_rect": normalized_window_rect,
        "client_rect": normalized_client_rect,
        "client_screen_origin": normalized_client_origin,
        "dpi_scale": max(0.01, float(dpi_scale or 1.0)),
        "surface_kind": str(surface_kind or "wechat_main"),
        "required_region_names": list(required_names),
        "action_region_names": list(required_names),
        **{name: normalized_regions.get(name, [0, 0, 0, 0]) for name in REQUIRED_LAYOUT_REGION_NAMES},
        **normalized_regions,
        "anchors": [dict(item) for item in (anchors or []) if isinstance(item, Mapping)],
        "confidence": max(0.0, min(1.0, float(confidence or 0.0))),
        "conflicts": normalized_conflicts,
        "executable": bool(executable and valid and can_click),
        "clickable": bool(valid and can_click),
        "valid": valid,
        "screenshot_path": str(screenshot_path or ""),
        "geometry_signature": geometry_signature(
            hwnd=hwnd,
            window_rect=normalized_window_rect,
            client_rect=normalized_client_rect,
            dpi_scale=dpi_scale,
            image_size=(width, height),
            capture_mode=normalized_mode,
            capture_screen_origin=normalized_capture_origin,
            client_screen_origin=normalized_client_origin,
        ),
        "invalidated": False,
    }
    return result


def snapshot_matches_current(
    snapshot: Mapping[str, Any],
    *,
    hwnd: int,
    window_rect: Any,
    client_rect: Any,
    dpi_scale: float,
    image_size: Any,
    capture_mode: str | None = None,
    capture_screen_origin: Any = None,
    client_screen_origin: Any = None,
) -> bool:
    if not isinstance(snapshot, Mapping) or snapshot.get("invalidated"):
        return False
    expected = snapshot.get("geometry_signature")
    actual = geometry_signature(
        hwnd=hwnd,
        window_rect=window_rect,
        client_rect=client_rect,
        dpi_scale=dpi_scale,
        image_size=image_size,
        capture_mode=str(capture_mode or snapshot.get("capture_mode") or ""),
        capture_screen_origin=(
            snapshot.get("capture_screen_origin")
            if capture_screen_origin is None
            else capture_screen_origin
        ),
        client_screen_origin=(
            snapshot.get("client_screen_origin")
            if client_screen_origin is None
            else client_screen_origin
        ),
    )
    if int(snapshot.get("hwnd") or 0) != int(hwnd or 0):
        return False
    if list(snapshot.get("image_size") or [snapshot.get("image_width"), snapshot.get("image_height")]) != [
        int(image_size[0] or 0),
        int(image_size[1] or 0),
    ]:
        return False
    return bool(expected and expected.split("-")[1] == actual.split("-")[1])


def image_point_to_screen(snapshot: Mapping[str, Any], point: Any) -> list[int]:
    if not bool(snapshot.get("clickable")) or str(snapshot.get("capture_mode") or "") not in PHYSICAL_CLICK_CAPTURE_MODES:
        raise LayoutSnapshotError(
            "capture_origin_not_proven_for_physical_click",
            code=ERROR_COORDINATE_MAPPING_INVALID,
        )
    origin = _normalize_origin(snapshot.get("capture_screen_origin"))
    if origin is None:
        raise LayoutSnapshotError("capture_screen_origin_missing", code=ERROR_COORDINATE_MAPPING_INVALID)
    values = list(point or []) if isinstance(point, (list, tuple)) else []
    if len(values) < 2:
        raise LayoutSnapshotError("image_point_missing", code=ERROR_COORDINATE_MAPPING_INVALID)
    return [origin[0] + int(values[0]), origin[1] + int(values[1])]


def screen_point_to_client(snapshot: Mapping[str, Any], point: Any) -> list[int]:
    origin = _normalize_origin(snapshot.get("client_screen_origin"))
    if origin is None:
        raise LayoutSnapshotError("client_screen_origin_missing", code=ERROR_COORDINATE_MAPPING_INVALID)
    values = list(point or []) if isinstance(point, (list, tuple)) else []
    if len(values) < 2:
        raise LayoutSnapshotError("screen_point_missing", code=ERROR_COORDINATE_MAPPING_INVALID)
    return [int(values[0]) - origin[0], int(values[1]) - origin[1]]


def client_point_to_screen(snapshot: Mapping[str, Any], point: Any) -> list[int]:
    origin = _normalize_origin(snapshot.get("client_screen_origin"))
    if origin is None:
        raise LayoutSnapshotError("client_screen_origin_missing", code=ERROR_COORDINATE_MAPPING_INVALID)
    values = list(point or []) if isinstance(point, (list, tuple)) else []
    if len(values) < 2:
        raise LayoutSnapshotError("client_point_missing", code=ERROR_COORDINATE_MAPPING_INVALID)
    return [origin[0] + int(values[0]), origin[1] + int(values[1])]


def transform_target_to_screen(
    snapshot: Mapping[str, Any],
    *,
    point: Any,
    bounds: Any,
) -> dict[str, Any]:
    if not point_in_bounds(point, bounds):
        raise LayoutSnapshotError(
            "target_point_outside_target_bounds",
            code=ERROR_COORDINATE_MAPPING_INVALID,
        )
    image_point = clamp_point(point, bounds)
    screen_point = image_point_to_screen(snapshot, image_point)
    bounds_rect = normalize_rect(bounds)
    screen_bounds = [
        image_point_to_screen(snapshot, [bounds_rect[0], bounds_rect[1]]),
        image_point_to_screen(snapshot, [bounds_rect[2], bounds_rect[3]]),
    ]
    return {
        "image_point": image_point,
        "screen_point": screen_point,
        "screen_bounds": [
            screen_bounds[0][0],
            screen_bounds[0][1],
            screen_bounds[1][0],
            screen_bounds[1][1],
        ],
        "layout_snapshot_id": str(snapshot.get("layout_snapshot_id") or ""),
        "frame_id": str(snapshot.get("frame_id") or ""),
        "hwnd": int(snapshot.get("hwnd") or 0),
    }


@dataclass
class LayoutSnapshotStore:
    """Small process-local store; snapshots are invalidated, never mutated in place."""

    _items: dict[str, dict[str, Any]]

    def __init__(self) -> None:
        self._items = {}

    def put(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        item = deepcopy(dict(snapshot))
        item["invalidated"] = False
        snapshot_id = str(item.get("layout_snapshot_id") or "")
        if not snapshot_id:
            raise LayoutSnapshotError("layout_snapshot_id_missing")
        self._items[snapshot_id] = item
        return deepcopy(item)

    def get(self, snapshot_id: str) -> dict[str, Any] | None:
        item = self._items.get(str(snapshot_id or ""))
        return deepcopy(item) if item else None

    def finalize_ocr_anchors(
        self,
        snapshot_id: str,
        *,
        anchors: list[Mapping[str, Any]],
    ) -> dict[str, Any] | None:
        """Complete construction once; an executable snapshot is immutable after this."""
        key = str(snapshot_id or "")
        item = self._items.get(key)
        if item is None or item.get("invalidated"):
            return None
        if bool(item.get("ocr_anchors_finalized")):
            return deepcopy(item)
        completed = deepcopy(item)
        existing = [dict(value) for value in completed.get("anchors") or [] if isinstance(value, Mapping)]
        existing.extend(dict(value) for value in anchors if isinstance(value, Mapping))
        completed["anchors"] = existing
        completed["ocr_anchors_finalized"] = True
        self._items[key] = completed
        return deepcopy(completed)

    def invalidate(self, snapshot_id: str, *, reason: str) -> None:
        item = self._items.get(str(snapshot_id or ""))
        if item is not None:
            item["invalidated"] = True
            item["invalidated_reason"] = str(reason or "ui_changed")

    def invalidate_hwnd(self, hwnd: int, *, reason: str) -> None:
        for item in self._items.values():
            if int(item.get("hwnd") or 0) == int(hwnd or 0):
                item["invalidated"] = True
                item["invalidated_reason"] = str(reason or "ui_changed")

    def invalidate_all(self, *, reason: str) -> None:
        for item in self._items.values():
            item["invalidated"] = True
            item["invalidated_reason"] = str(reason or "ui_changed")


def current_geometry_matches(
    snapshot: Mapping[str, Any],
    *,
    geometry_provider: Callable[[int], Mapping[str, Any]],
    client_geometry_provider: Callable[[int], Mapping[str, Any]],
    dpi_provider: Callable[[int], float],
) -> bool:
    hwnd = int(snapshot.get("hwnd") or 0)
    geometry = geometry_provider(hwnd)
    client_geometry = client_geometry_provider(hwnd)
    return snapshot_matches_current(
        snapshot,
        hwnd=hwnd,
        window_rect=geometry,
        client_rect=client_geometry,
        dpi_scale=dpi_provider(hwnd),
        image_size=(snapshot.get("image_width"), snapshot.get("image_height")),
    )
