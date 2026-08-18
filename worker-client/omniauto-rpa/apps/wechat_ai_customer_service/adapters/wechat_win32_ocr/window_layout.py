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


class LayoutSnapshotError(RuntimeError):
    """Raised when a frame cannot safely be used for a physical action."""

    def __init__(self, reason: str, *, code: str = "LAYOUT_SNAPSHOT_INVALID", details: Mapping[str, Any] | None = None):
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


def clamp_point(point: Any, bounds: Any) -> list[int]:
    values = list(point or []) if isinstance(point, (list, tuple)) else []
    if len(values) < 2:
        raise LayoutSnapshotError("target_point_missing", code="LAYOUT_TARGET_POINT_MISSING")
    left, top, right, bottom = normalize_rect(bounds)
    if right <= left or bottom <= top:
        raise LayoutSnapshotError("target_bounds_invalid", code="LAYOUT_TARGET_BOUNDS_INVALID")
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
) -> dict[str, Any]:
    width = int(image_size[0] or 0) if len(image_size) >= 1 else 0
    height = int(image_size[1] or 0) if len(image_size) >= 2 else 0
    normalized = _normalize_regions(regions)
    missing = [name for name in REQUIRED_LAYOUT_REGION_NAMES if name not in normalized]
    invalid: list[str] = []
    for name, bounds in normalized.items():
        left, top, right, bottom = bounds
        if right <= left or bottom <= top:
            invalid.append(name)
            continue
        if left < 0 or top < 0 or right > width or bottom > height:
            invalid.append(name)
    return {
        "ok": bool(width > 0 and height > 0 and not missing and not invalid),
        "regions": normalized,
        "missing": missing,
        "invalid": invalid,
        "image_size": [width, height],
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
    sample_rows = sorted(
        {
            max(1, min(height - 2, int(height * ratio)))
            for ratio in (0.18, 0.34, 0.52, 0.70, 0.84)
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
        if stable_rows >= 3 and score >= 28.0:
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
        [item for item in verticals if 92 <= item[0] <= width - 160],
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
    main_boundary = max(selected_verticals, key=lambda item: item[1])
    nav_candidates = [item for item in selected_verticals if item[0] < main_boundary[0] - 80]
    nav_boundary = max(nav_candidates, key=lambda item: item[1]) if nav_candidates else (max(64, int(width * 0.07)), 0.0)
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
    sidebar_header_bottom = (
        max(
            [item for item in sidebar_header_bottom_candidates if 72 <= item[0] <= min(height - 100, 240)],
            key=lambda item: item[1],
            default=(max(96, int(height * 0.14)), 0.0),
        )[0]
    )
    chat_header_bottom = (
        max(
            [item for item in chat_header_bottom_candidates if 72 <= item[0] <= min(height - 120, 260)],
            key=lambda item: item[1],
            default=(max(96, int(height * 0.12)), 0.0),
        )[0]
    )
    input_top_candidates = [
        item
        for item in _horizontal_edge_candidates(image, left=main_boundary[0], right=width)
        if int(height * 0.58) <= item[0] <= height - 44
    ]
    input_top = max(
        input_top_candidates,
        key=lambda item: item[1],
        default=(max(chat_header_bottom + 80, int(height * 0.78)), 0.0),
    )[0]
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
        min(1.0, float(main_boundary[1]) / 100.0),
        min(1.0, float(nav_boundary[1]) / 100.0) if nav_boundary[1] else 0.72,
        0.85 if sidebar_header_bottom_candidates else 0.72,
        0.85 if chat_header_bottom_candidates else 0.72,
        0.85 if input_top_candidates else 0.72,
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
        "conflicts": conflicts + list(validation.get("missing") or []) + list(validation.get("invalid") or []),
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
) -> str:
    return _stable_id(
        "geometry",
        int(hwnd or 0),
        normalize_rect(window_rect),
        normalize_rect(client_rect),
        round(float(dpi_scale or 1.0), 4),
        list(image_size or []),
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
) -> dict[str, Any]:
    width = int(image_size[0] or 0)
    height = int(image_size[1] or 0)
    normalized_mode = str(capture_mode or "").strip() or CAPTURE_MODE_PRINT_WINDOW
    normalized_capture_origin = _normalize_origin(capture_screen_origin)
    normalized_client_origin = _normalize_origin(client_screen_origin)
    normalized_window_rect = normalize_rect(window_rect)
    normalized_client_rect = normalize_rect(client_rect)
    region_validation = validate_layout_regions(regions, image_size=(width, height))
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
    return {
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
        **region_validation["regions"],
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
        ),
        "invalidated": False,
    }


def snapshot_matches_current(
    snapshot: Mapping[str, Any],
    *,
    hwnd: int,
    window_rect: Any,
    client_rect: Any,
    dpi_scale: float,
    image_size: Any,
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
            code="LAYOUT_CAPTURE_ORIGIN_UNKNOWN",
        )
    origin = _normalize_origin(snapshot.get("capture_screen_origin"))
    if origin is None:
        raise LayoutSnapshotError("capture_screen_origin_missing", code="LAYOUT_CAPTURE_ORIGIN_UNKNOWN")
    values = list(point or []) if isinstance(point, (list, tuple)) else []
    if len(values) < 2:
        raise LayoutSnapshotError("image_point_missing", code="LAYOUT_TARGET_POINT_MISSING")
    return [origin[0] + int(values[0]), origin[1] + int(values[1])]


def screen_point_to_client(snapshot: Mapping[str, Any], point: Any) -> list[int]:
    origin = _normalize_origin(snapshot.get("client_screen_origin"))
    if origin is None:
        raise LayoutSnapshotError("client_screen_origin_missing", code="LAYOUT_CLIENT_ORIGIN_UNKNOWN")
    values = list(point or []) if isinstance(point, (list, tuple)) else []
    if len(values) < 2:
        raise LayoutSnapshotError("screen_point_missing", code="LAYOUT_TARGET_POINT_MISSING")
    return [int(values[0]) - origin[0], int(values[1]) - origin[1]]


def transform_target_to_screen(
    snapshot: Mapping[str, Any],
    *,
    point: Any,
    bounds: Any,
) -> dict[str, Any]:
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
