"""Immutable per-frame layout facts and coordinate conversion for WeChat UI."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
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

REQUIRED_LAYOUT_REGION_NAMES = (
    "left_nav_bounds",
    "sidebar_bounds",
    "sidebar_header_bounds",
    "session_list_bounds",
    "chat_header_bounds",
    "message_viewport_bounds",
    "input_bounds",
)
POPUP_LAYOUT_REGION_NAMES = ("surface_bounds",)


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
    scores: list[tuple[int, float]] = []
    for x in range(48, max(49, width - 48)):
        row_scores = []
        for y in sample_rows:
            try:
                left = _pixel_luma(image.getpixel((x - 1, y)))
                right = _pixel_luma(image.getpixel((x + 1, y)))
                row_scores.append(abs(left - right))
            except Exception:
                row_scores.append(0.0)
        stable_rows = sum(1 for score in row_scores if score >= 14.0)
        score = (sum(row_scores) / max(1, len(row_scores))) + (stable_rows * 8.0)
        if stable_rows >= 5 and score >= 28.0:
            scores.append((x, score))
    clusters: list[list[tuple[int, float]]] = []
    for item in sorted(scores):
        if not clusters or item[0] - clusters[-1][-1][0] > 8:
            clusters.append([item])
        else:
            clusters[-1].append(item)
    result = []
    for cluster in clusters:
        x, score = max(cluster, key=lambda item: item[1])
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


def _qualified_edge_confidence(score: float, *, threshold: float) -> float:
    """Normalize an already-qualified structural edge without requiring black borders.

    WeChat separators are intentionally low contrast.  Once an edge has passed
    the multi-row/multi-column stability test, confidence is based on how far it
    clears that test rather than on an impossible 0-255 black/white contrast.
    """

    margin = max(0.0, float(score) - float(threshold))
    return min(0.97, 0.78 + (margin / 180.0))


def build_structural_layout_regions(
    image: Any,
    *,
    ocr_items: list[Mapping[str, Any]] | None = None,
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
    ranked_pairs = sorted(pairs, key=lambda item: item[0], reverse=True)
    _pair_score, nav_boundary, main_boundary = ranked_pairs[0]
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
    if unresolved_pairs:
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
    if not sidebar_header_candidates:
        conflicts.append("sidebar_header_boundary_missing")
    if not chat_header_candidates:
        conflicts.append("chat_header_boundary_missing")
    if conflicts:
        return {
            "ok": False,
            "regions": {},
            "anchors": [],
            "confidence": 0.0,
            "conflicts": conflicts,
        }
    sidebar_header_bottom = max(sidebar_header_candidates, key=lambda item: item[1])[0]
    chat_header_bottom = max(chat_header_candidates, key=lambda item: item[1])[0]
    input_top_candidates = [
        item
        for item in _horizontal_edge_candidates(image, left=main_boundary[0], right=width)
        if int(height * 0.50) <= item[0] <= height - max(28, int(height * 0.035))
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
    input_top = max(input_top_candidates, key=lambda item: item[1])[0]
    if input_top <= chat_header_bottom or input_top >= height:
        conflicts.append("chat_regions_conflict")
    regions = {
        "left_nav_bounds": [0, 0, nav_boundary[0], height],
        "sidebar_bounds": [nav_boundary[0], 0, main_boundary[0], height],
        "sidebar_header_bounds": [nav_boundary[0], 0, main_boundary[0], sidebar_header_bottom],
        "session_list_bounds": [nav_boundary[0], sidebar_header_bottom, main_boundary[0], height],
        "chat_header_bounds": [main_boundary[0], 0, width, chat_header_bottom],
        "message_viewport_bounds": [main_boundary[0], chat_header_bottom, width, input_top],
        "input_bounds": [main_boundary[0], input_top, width, height],
    }
    validation = validate_layout_regions(regions, image_size=(width, height))
    confidence_parts = [
        _qualified_edge_confidence(main_boundary[1], threshold=28.0),
        _qualified_edge_confidence(nav_boundary[1], threshold=28.0),
        _qualified_edge_confidence(max(sidebar_header_candidates, key=lambda item: item[1])[1], threshold=24.0),
        _qualified_edge_confidence(max(chat_header_candidates, key=lambda item: item[1])[1], threshold=24.0),
        _qualified_edge_confidence(max(input_top_candidates, key=lambda item: item[1])[1], threshold=24.0),
    ]
    anchors = [
        {"name": "nav_separator", "x": nav_boundary[0], "score": nav_boundary[1]},
        {"name": "sidebar_separator", "x": main_boundary[0], "score": main_boundary[1]},
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
