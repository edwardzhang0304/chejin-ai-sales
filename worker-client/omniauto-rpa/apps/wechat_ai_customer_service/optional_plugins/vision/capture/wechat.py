from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from typing import Any

from PIL import Image, ImageStat

DEFAULT_BOTTOM_EXCLUDE_PX = 95
DEFAULT_MAX_VISIBLE_IMAGE_CANDIDATES = 64
MIN_MEDIA_COMPONENT_FILL_RATIO = 0.28
TEXT_OVERLAP_REJECTION_RATIO = 0.42
MEDIA_ROLE_EDGE_CONTINUITY_RATIO = 0.45
MEDIA_EDGE_BACKGROUND_DISTANCE = 6.0
MEDIA_CROP_EDGE_ACTIVE_RATIO = 0.15
IMAGE_PREVIEW_TOKENS = ("[图片]", "[照片]", "[Image]", "图片", "照片", "发送了一张图片")
SAVE_MENU_TOKENS = (
    "另存为",
    "保存图片",
    "保存到",
    "保存",
    "saveas",
    "save as",
    "saveimage",
    "save image",
)
COPY_IMAGE_MENU_TOKENS = (
    "复制图片",
    "复制",
    "copyimage",
    "copy image",
    "copy",
)
CHAT_TIME_RE = re.compile(
    r"^(?:(?:昨天|前天|星期[一二三四五六日天])\s*)?(?:[01]?\d|2[0-3]):[0-5]\d$"
)


def session_split_x(width: int) -> int:
    """Return the WeChat session/chat split without importing the host sidecar."""

    return max(300, min(370, int(width * 0.52)))


def chat_header_cutoff_y(height: int) -> int:
    """Return the chat header cutoff without importing the host sidecar."""

    return max(90, min(150, int(height * 0.12)))


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def image_preview_text(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    compact = re.sub(r"\s+", "", text).lower()
    return any(re.sub(r"\s+", "", token).lower() in compact for token in IMAGE_PREVIEW_TOKENS)


def parse_preview_speaker(source_preview: Any, explicit_speaker: Any = "") -> str:
    explicit = str(explicit_speaker or "").strip()
    if explicit:
        return explicit
    text = str(source_preview or "").strip()
    if not text:
        return ""
    for sep in (":", "："):
        if sep not in text:
            continue
        left, right = text.split(sep, 1)
        if image_preview_text(right):
            return left.strip()
    return ""


def extract_chat_time_markers(
    ocr_items: list[dict[str, Any]] | None,
    image_size: tuple[int, int],
) -> list[dict[str, Any]]:
    """Extract centered WeChat time separators without treating them as messages."""
    width, height = image_size
    split_x = session_split_x(width)
    header_cutoff = chat_header_cutoff_y(height)
    markers: list[dict[str, Any]] = []
    for item in ocr_items or []:
        text = re.sub(r"\s+", "", str(item.get("text") or "").strip())
        if not text or not CHAT_TIME_RE.fullmatch(text):
            continue
        center_x = float(item.get("center_x") or 0.0)
        center_y = float(item.get("center_y") or 0.0)
        if center_y < header_cutoff or center_y > height - DEFAULT_BOTTOM_EXCLUDE_PX:
            continue
        # Session-list times live left of the chat split. Chat separators are
        # centered in the conversation pane and have compact OCR boxes.
        if center_x < split_x or center_x > width - 80:
            continue
        markers.append(
            {
                "text": text,
                "top": int(float(item.get("top") or center_y)),
                "bottom": int(float(item.get("bottom") or center_y)),
                "center_y": center_y,
            }
        )
    return sorted(markers, key=lambda item: float(item.get("center_y") or 0.0))


def nearest_chat_time_marker(
    bubble_bounds: list[int] | tuple[int, int, int, int] | None,
    markers: list[dict[str, Any]] | None,
) -> str:
    """Attach the latest visible time separator above a visual bubble."""
    bounds = [int(value) for value in list(bubble_bounds or [])[:4]]
    if len(bounds) != 4:
        return ""
    bubble_top = bounds[1]
    candidates = [
        item
        for item in (markers or [])
        if isinstance(item, dict) and float(item.get("center_y") or 0.0) <= bubble_top
    ]
    if not candidates:
        return ""
    return str(candidates[-1].get("text") or "").strip()


def _chat_bounds(width: int, height: int) -> tuple[int, int, int, int]:
    split = session_split_x(width)
    left = min(width - 1, split + 12)
    top = chat_header_cutoff_y(height)
    right = width - 8
    bottom = height - max(DEFAULT_BOTTOM_EXCLUDE_PX, int(height * 0.10))
    return left, top, right, bottom


def _bounds_continue_through_chat_crop_boundary(
    screenshot: Image.Image,
    bounds: tuple[int, int, int, int],
    chat_bounds: tuple[int, int, int, int],
    *,
    background: list[float],
) -> bool:
    """Return true when visible media pixels continue through a crop edge."""

    left, top, right, bottom = bounds
    chat_left, chat_top, chat_right, chat_bottom = chat_bounds
    strip_width = max(2, min(6, int(round(min(right - left, bottom - top) * 0.02))))
    strips: list[tuple[int, int, int, int]] = []
    if left <= chat_left:
        strips.append((chat_left, top, chat_left + strip_width, bottom))
    if top <= chat_top:
        strips.append((left, chat_top, right, chat_top + strip_width))
    if right >= chat_right:
        strips.append((chat_right - strip_width, top, chat_right, bottom))
    if bottom >= chat_bottom:
        strips.append((left, chat_bottom - strip_width, right, chat_bottom))
    for strip in strips:
        edge = screenshot.crop(strip).convert("RGB")
        try:
            pixel_reader = getattr(edge, "get_flattened_data", edge.getdata)
            pixels = list(pixel_reader())
        finally:
            edge.close()
        if not pixels:
            continue
        active_ratio = sum(
            1
            for pixel in pixels
            if sum(
                abs(float(pixel[index]) - float(background[index]))
                for index in range(3)
            )
            / 3.0
            >= MEDIA_EDGE_BACKGROUND_DISTANCE
        ) / len(pixels)
        if active_ratio >= MEDIA_CROP_EDGE_ACTIVE_RATIO:
            return True
    return False


def _maximum_text_overlap_ratio(
    bounds: tuple[int, int, int, int],
    messages: list[dict[str, Any]],
) -> float:
    left, top, right, bottom = bounds
    area = max(1, (right - left) * (bottom - top))
    maximum = 0.0
    for message in messages:
        if not isinstance(message, dict):
            continue
        message_type = str(
            message.get("type") or message.get("message_type") or ""
        ).strip().lower()
        if message_type != "text":
            continue
        rect = (
            message.get("bubble_rect")
            if isinstance(message.get("bubble_rect"), dict)
            else {}
        )
        if not rect:
            continue
        ml = int(float(rect.get("left") or 0)) - 8
        mt = int(float(rect.get("top") or 0)) - 8
        mr = int(float(rect.get("right") or 0)) + 8
        mb = int(float(rect.get("bottom") or 0)) + 8
        overlap_left = max(left, ml)
        overlap_top = max(top, mt)
        overlap_right = min(right, mr)
        overlap_bottom = min(bottom, mb)
        if overlap_right <= overlap_left or overlap_bottom <= overlap_top:
            continue
        overlap = (overlap_right - overlap_left) * (overlap_bottom - overlap_top)
        maximum = max(maximum, overlap / area)
    return maximum


def _role_facing_edge_surface_continuity(
    screenshot: Image.Image,
    bounds: tuple[int, int, int, int],
    *,
    side: str,
    background: list[float],
) -> float:
    """Measure whether the role-facing edge is a full media edge or a text tail.

    A WeChat text bubble narrows to a small tail on the avatar-facing edge. An
    image thumbnail keeps a continuous rectangular edge. This structural
    evidence lets text-heavy screenshots remain images without treating a
    genuinely long text bubble as media.
    """

    left, top, right, bottom = bounds
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0 or side not in {"customer", "self"}:
        return 0.0
    strip_width = max(2, min(8, int(round(min(width, height) * 0.03))))
    if side == "customer":
        strip = (left, top, min(right, left + strip_width), bottom)
    else:
        strip = (max(left, right - strip_width), top, right, bottom)
    edge_image = screenshot.crop(strip).convert("RGB")
    try:
        pixel_reader = getattr(
            edge_image,
            "get_flattened_data",
            edge_image.getdata,
        )
        pixels = list(pixel_reader())
    finally:
        edge_image.close()
    if not pixels:
        return 0.0
    active = sum(
        1
        for pixel in pixels
        if sum(
            abs(float(pixel[index]) - float(background[index]))
            for index in range(3)
        )
        / 3.0
        >= MEDIA_EDGE_BACKGROUND_DISTANCE
    )
    return active / len(pixels)


def _structural_media_lanes(width: int, height: int) -> dict[str, dict[str, int]]:
    """Return relative WeChat media/avatar lanes for each message side.

    These are deliberately expressed from the current chat pane instead of
    fixed screenshot coordinates.  A media candidate is owned by the message
    row whose avatar column it adjoins; image pixels never decide ownership.
    """
    chat_left, top, chat_right, bottom = _chat_bounds(width, height)
    chat_width = max(1, chat_right - chat_left)
    avatar_width = max(42, min(64, int(chat_width * 0.10)))
    media_gap = max(14, min(30, int(chat_width * 0.04)))
    customer_media_left = chat_left + avatar_width + media_gap
    self_media_right = chat_right - avatar_width - media_gap
    media_column_width = max(150, int(chat_width * 0.62))
    return {
        "customer": {
            "avatar_left": max(chat_left - avatar_width - 6, session_split_x(width) + 2),
            "avatar_right": chat_left + 12,
            "media_left": customer_media_left,
            "media_right": min(chat_right, customer_media_left + media_column_width),
            "top": top,
            "bottom": bottom,
        },
        "self": {
            "avatar_left": chat_right - 12,
            "avatar_right": min(width - 2, chat_right + avatar_width + 6),
            "media_left": max(chat_left, self_media_right - media_column_width),
            "media_right": self_media_right,
            "top": top,
            "bottom": bottom,
        },
    }
def _avatar_row_presence(
    image: Image.Image,
    *,
    lane: dict[str, int],
    bubble_bounds: tuple[int, int, int, int],
) -> bool:
    """Lightweight same-row avatar confirmation, never an identity classifier."""
    left = max(0, int(lane.get("avatar_left") or 0))
    right = min(image.width, int(lane.get("avatar_right") or 0))
    top = max(0, int(bubble_bounds[1]) - 14)
    bottom = min(image.height, int(bubble_bounds[1]) + max(72, int((bubble_bounds[3] - bubble_bounds[1]) * 0.28)))
    if right - left < 12 or bottom - top < 12:
        return False
    stat = ImageStat.Stat(image.crop((left, top, right, bottom)).convert("RGB"))
    mean = stat.mean or [255.0, 255.0, 255.0]
    spread = sum(float(value) for value in (stat.stddev or [0.0, 0.0, 0.0])) / 3.0
    brightness = sum(float(value) for value in mean) / 3.0
    # An avatar normally differs from the blank chat background either by
    # texture/colour or by a dark surface.  This only corroborates an already
    # position-valid row; it must not create a media candidate by itself.
    return spread >= 7.0 or brightness <= 225.0


def _structural_media_side(
    screenshot: Image.Image,
    bounds: tuple[int, int, int, int],
) -> tuple[str, float, list[str]] | None:
    """Resolve sender side from message-lane adjacency before image features.

    The tolerance allows different WeChat DPI/layout variants and permits a
    mostly white image whose detected visual content starts inside its bubble.
    """
    width, height = screenshot.size
    left, top, right, bottom = bounds
    if left >= right or top >= bottom:
        return None
    lanes = _structural_media_lanes(width, height)
    candidates: list[tuple[str, float, list[str]]] = []
    for side, lane in lanes.items():
        if top < lane["top"] or bottom > lane["bottom"]:
            continue
        column_width = max(1, lane["media_right"] - lane["media_left"])
        edge_tolerance = max(76, min(180, int(column_width * 0.42)))
        cross_tolerance = max(48, min(110, int(column_width * 0.24)))
        if side == "customer":
            edge_distance = abs(left - lane["media_left"])
            in_column = left <= lane["media_left"] + edge_tolerance and right <= lane["media_right"] + cross_tolerance
            edge_name = "left_edge_adjacent_to_customer_avatar_column"
        else:
            edge_distance = abs(right - lane["media_right"])
            in_column = right >= lane["media_right"] - edge_tolerance and left >= lane["media_left"] - cross_tolerance
            edge_name = "right_edge_adjacent_to_self_avatar_column"
        if not in_column:
            continue
        avatar_present = _avatar_row_presence(screenshot, lane=lane, bubble_bounds=bounds)
        position_score = max(0.0, 1.0 - (edge_distance / float(edge_tolerance + 1)))
        score = position_score + (0.16 if avatar_present else 0.0)
        evidence = ["structural_media_lane_v1", edge_name, f"edge_distance={edge_distance}"]
        if avatar_present:
            evidence.append("same_row_avatar_column_present")
        candidates.append((side, score, evidence))
    if not candidates:
        return None
    # A component may visually overlap both broad columns.  The side with the
    # closer avatar-adjacent edge wins; there is no center-point side fallback.
    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[0]


def _exclude_avatar_column_from_media_bounds(
    screenshot: Image.Image,
    bounds: tuple[int, int, int, int],
    *,
    side: str,
) -> tuple[tuple[int, int, int, int], bool]:
    """Keep the media rectangle outside the same-row avatar column.

    Connected-component detection can join a textured avatar to an adjacent
    image through a small visual bridge. The preliminary structural side is
    used only to trim that avatar column; the host adapter still owns the
    authoritative sender-role decision through its shared avatar rule.
    """
    left, top, right, bottom = bounds
    lane = _structural_media_lanes(*screenshot.size).get(str(side or "").strip().lower())
    if not lane:
        return bounds, False
    if side == "customer":
        left = max(left, int(lane["media_left"]))
    elif side == "self":
        right = min(right, int(lane["media_right"]))
    normalized = (left, top, right, bottom)
    return normalized, normalized != bounds


def _visual_bounds(value: Any) -> tuple[int, int, int, int] | None:
    if isinstance(value, dict):
        raw = (
            value.get("left"),
            value.get("top"),
            value.get("right"),
            value.get("bottom"),
        )
    elif isinstance(value, (list, tuple)) and len(value) >= 4:
        raw = value[:4]
    else:
        return None
    try:
        left, top, right, bottom = (int(float(item)) for item in raw)
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def explained_non_image_regions(
    messages: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Return regions whose parsed type is reliable enough to veto image.

    Arbitrary OCR text inside a real photograph is deliberately not enough.
    Text requires trusted row-role evidence. Voice requires a bound transcript
    or a size-checked visual/duration voice structure; a bare duration-like OCR
    token inside a real image is not reliable. Physical action success is
    intentionally absent because it settles a transaction, not a message type.
    """

    regions: list[dict[str, Any]] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        message_type = str(
            message.get("type")
            or message.get("message_type")
            or ""
        ).strip().lower()
        if message_type not in {"text", "voice"}:
            continue
        role = str(
            message.get("sender_role") or message.get("sender") or ""
        ).strip().lower()
        if role == "contact":
            role = "customer"
        if role not in {"customer", "self"}:
            continue
        anchor = (
            message.get("voice_anchor")
            if isinstance(message.get("voice_anchor"), dict)
            else {}
        )
        anchor_item = (
            anchor.get("item")
            if isinstance(anchor.get("item"), dict)
            else {}
        )
        bounds = _visual_bounds(
            message.get("bubble_rect")
            or message.get("bounds")
            or anchor_item.get("parser_bubble_rect")
            or anchor_item
        )
        if bounds is None:
            continue

        avatar = (
            message.get("avatar_alignment")
            if isinstance(message.get("avatar_alignment"), dict)
            else {}
        )
        role_source = str(
            message.get("sender_role_source") or ""
        ).strip().lower()
        row_role_trusted = bool(
            role_source in {"same_row_avatar", "parent_voice"}
            or str(avatar.get("role") or "").strip().lower() == role
            or "avatar_row_structure_confirmed"
            in {
                str(value).strip()
                for value in (message.get("sender_role_evidence") or [])
            }
        )
        voice_anchor_key = str(
            message.get("parent_voice_anchor_key")
            or message.get("voice_anchor_stable_key")
            or message.get("voice_anchor_structural_key")
            or message.get("voice_anchor_key")
            or anchor.get("anchor_stable_key")
            or anchor.get("anchor_structural_key")
            or anchor.get("anchor_key")
            or ""
        ).strip()
        content = str(
            message.get("content_clean") or message.get("content") or ""
        ).strip()
        quality_flags = {
            str(value).strip()
            for value in (message.get("quality_flags") or [])
        }
        region_width = bounds[2] - bounds[0]
        region_height = bounds[3] - bounds[1]
        bound_transcript = bool(
            message_type == "voice"
            and str(message.get("parent_voice_anchor_key") or "").strip()
            and content
            and not content.startswith("[语音]")
        )
        visual_voice = bool(
            message_type == "voice"
            and "visual_voice_hint" in quality_flags
            and row_role_trusted
        )
        duration_voice = bool(
            message_type == "voice"
            and "untranscribed_voice_placeholder" in quality_flags
            and row_role_trusted
            and region_width >= 36
            and region_height >= 16
        )
        if message_type == "text" and not row_role_trusted:
            continue
        if message_type == "voice" and not (
            bound_transcript or visual_voice or duration_voice
        ):
            continue
        regions.append(
            {
                "bounds": bounds,
                "message_type": message_type,
                "sender_role": role,
                "voice_anchor_key": voice_anchor_key,
                "evidence": (
                    "bound_voice_transcript"
                    if bound_transcript
                    else (
                        "visual_voice_structure"
                        if visual_voice
                        else (
                            "duration_voice_structure"
                            if duration_voice
                            else "trusted_row_type"
                        )
                    )
                ),
            }
        )
    return regions


def explained_non_image_conflict(
    bounds: tuple[int, int, int, int],
    *,
    side: str,
    regions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return reliable typed evidence contained by a structural candidate."""

    left, top, right, bottom = bounds
    for region in regions:
        region_bounds = region.get("bounds")
        if (
            not isinstance(region_bounds, tuple)
            or len(region_bounds) != 4
            or str(region.get("sender_role") or "") != side
        ):
            continue
        rl, rt, rr, rb = region_bounds
        overlap_width = max(0, min(right, rr) - max(left, rl))
        overlap_height = max(0, min(bottom, rb) - max(top, rt))
        region_area = max(1, (rr - rl) * (rb - rt))
        contained_ratio = (overlap_width * overlap_height) / region_area
        if contained_ratio >= 0.80:
            return {
                **region,
                "contained_ratio": round(contained_ratio, 6),
            }
    return None


def _trim_sparse_vertical_whiskers(
    cells: list[tuple[int, int]],
) -> tuple[list[tuple[int, int]], bool]:
    """Drop thin vertical bridges without assuming a media aspect ratio.

    Pale screenshots can join the chat header or surrounding chrome to the
    real media rectangle through one or two active grid cells.  Those sparse
    rows move the candidate's top away from its same-row avatar.  Keep the
    dense body of the connected component; the threshold is derived from the
    component itself so landscape, portrait and long images use one rule.
    """

    if not cells:
        return cells, False
    row_counts: dict[int, int] = {}
    for _x, y in cells:
        row_counts[y] = row_counts.get(y, 0) + 1
    ordered_rows = sorted(row_counts)
    if len(ordered_rows) < 4:
        return cells, False
    peak_width = max(row_counts.values())
    if peak_width < 4:
        return cells, False
    dense_threshold = max(3, (peak_width * 22 + 99) // 100)
    dense_rows = [
        y for y in ordered_rows if row_counts[y] >= dense_threshold
    ]
    if len(dense_rows) < 2:
        return cells, False
    first_dense = dense_rows[0]
    last_dense = dense_rows[-1]
    original_span = ordered_rows[-1] - ordered_rows[0] + 1
    retained_span = last_dense - first_dense + 1
    # A real media surface must remain the dominant vertical body.  This
    # prevents a sparse or highly fragmented component from being reshaped
    # into a plausible image merely by this cleanup step.
    if retained_span * 100 < original_span * 60:
        return cells, False
    trimmed = [
        (x, y) for x, y in cells if first_dense <= y <= last_dense
    ]
    return trimmed, len(trimmed) != len(cells)


def _media_surface_cell_active(
    stat: ImageStat.Stat,
    background: list[float],
) -> bool:
    mean = stat.mean or [0.0, 0.0, 0.0]
    spread = sum(
        float(value) for value in (stat.stddev or [0.0, 0.0, 0.0])
    ) / 3.0
    delta = max(mean) - min(mean)
    brightness = sum(float(value) for value in mean) / 3.0
    dark_low_texture_surface = brightness <= 58.0 and spread <= 20.0
    background_distance = sum(
        abs(float(value) - background[index])
        for index, value in enumerate(mean[:3])
    ) / 3.0
    pale_rectangular_surface = (
        background_distance >= 6.0 and spread <= 24.0
    )
    return (
        spread >= 16.0
        or delta >= 24.0
        or dark_low_texture_surface
        or pale_rectangular_surface
    )


def _fine_grid_confirms_separate_stacked_surfaces(
    small: Image.Image,
    *,
    coarse_cells: list[tuple[int, int]],
    coarse_block: int,
    background: list[float],
    side: str,
    minimum_media_height: float,
) -> bool:
    """Confirm that one coarse L-component is really two fine components."""

    if not coarse_cells or side not in {"customer", "self"}:
        return False
    fine_block = 2
    min_x = min(x for x, _ in coarse_cells) * coarse_block
    max_x = (max(x for x, _ in coarse_cells) + 1) * coarse_block
    min_y = min(y for _, y in coarse_cells) * coarse_block
    max_y = (max(y for _, y in coarse_cells) + 1) * coarse_block
    padding = coarse_block
    start_gx = max(0, (min_x - padding) // fine_block)
    end_gx = min(
        small.width // fine_block - 1,
        (max_x + padding - 1) // fine_block,
    )
    start_gy = max(0, (min_y - padding) // fine_block)
    end_gy = min(
        small.height // fine_block - 1,
        (max_y + padding - 1) // fine_block,
    )
    active: set[tuple[int, int]] = set()
    for gy in range(start_gy, end_gy + 1):
        for gx in range(start_gx, end_gx + 1):
            box = (
                gx * fine_block,
                gy * fine_block,
                min(small.width, (gx + 1) * fine_block),
                min(small.height, (gy + 1) * fine_block),
            )
            if _media_surface_cell_active(
                ImageStat.Stat(small.crop(box)),
                background,
            ):
                active.add((gx, gy))

    components: list[dict[str, float]] = []
    visited: set[tuple[int, int]] = set()
    candidate_width = max_x - min_x
    candidate_height = max_y - min_y
    for seed in sorted(active, key=lambda item: (item[1], item[0])):
        if seed in visited:
            continue
        stack = [seed]
        visited.add(seed)
        cells: list[tuple[int, int]] = []
        while stack:
            cx, cy = stack.pop()
            cells.append((cx, cy))
            for neighbour in (
                (cx - 1, cy),
                (cx + 1, cy),
                (cx, cy - 1),
                (cx, cy + 1),
            ):
                if neighbour in active and neighbour not in visited:
                    visited.add(neighbour)
                    stack.append(neighbour)
        left = min(x for x, _ in cells) * fine_block
        right = (max(x for x, _ in cells) + 1) * fine_block
        top = min(y for _, y in cells) * fine_block
        bottom = (max(y for _, y in cells) + 1) * fine_block
        width = right - left
        height = bottom - top
        cell_area = max(1, (width // fine_block) * (height // fine_block))
        density = len(cells) / cell_area
        if (
            width >= max(8.0, candidate_width * 0.35)
            and height >= max(6.0, candidate_height * 0.25)
            and density >= 0.72
        ):
            components.append(
                {
                    "left": float(left),
                    "right": float(right),
                    "top": float(top),
                    "bottom": float(bottom),
                    "width": float(width),
                    "height": float(height),
                    "density": float(density),
                }
            )

    components.sort(key=lambda item: (item["top"], item["left"]))
    for index, upper in enumerate(components):
        for lower in components[index + 1 :]:
            gap = lower["top"] - upper["bottom"]
            if gap < 0 or gap > max(4.0, candidate_height * 0.18):
                continue
            # Two separate short chat-row surfaces are not one media object.
            # This covers voice-duration + expanded-transcript rows even when
            # their widths are similar. Two real stacked image thumbnails are
            # intentionally not suppressed: each is tall enough to remain a
            # standalone media candidate on the next observation frame.
            if (
                upper["height"] >= minimum_media_height
                or lower["height"] >= minimum_media_height
            ):
                continue
            edge_delta = (
                abs(upper["left"] - lower["left"])
                if side == "customer"
                else abs(upper["right"] - lower["right"])
            )
            if edge_delta <= fine_block:
                return True
    return False


def image_bubble_visual_fingerprint(
    screenshot: Image.Image,
    bounds: Any,
) -> str:
    """Return the one shared movement-stable fingerprint for an image bubble."""

    rect = _visual_bounds(bounds)
    if rect is None:
        return ""
    left, top, right, bottom = rect
    left = max(0, left)
    top = max(0, top)
    right = min(int(screenshot.size[0]), right)
    bottom = min(int(screenshot.size[1]), bottom)
    if right <= left or bottom <= top:
        return ""
    crop = screenshot.crop((left, top, right, bottom))
    try:
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        gray = crop.convert("L").resize((9, 8), resampling)
        pixels = list(gray.getdata())
        bits = [
            1
            if pixels[row * 9 + column] > pixels[row * 9 + column + 1]
            else 0
            for row in range(8)
            for column in range(8)
        ]
        value = sum(bit << index for index, bit in enumerate(bits))
        return f"dhash64:{value:016x}"
    finally:
        crop.close()


def image_visual_fingerprint_distance(left: Any, right: Any) -> int | None:
    """Return dHash Hamming distance, or None for incompatible evidence."""

    left_text = str(left or "").strip().lower()
    right_text = str(right or "").strip().lower()
    if not left_text.startswith("dhash64:") or not right_text.startswith("dhash64:"):
        return None
    try:
        return (
            int(left_text.split(":", 1)[1], 16)
            ^ int(right_text.split(":", 1)[1], 16)
        ).bit_count()
    except (TypeError, ValueError):
        return None


def stable_image_neighbor_signature(message: dict[str, Any]) -> str:
    payload = {
        "sender_role": str(
            message.get("sender_role")
            or message.get("sender")
            or "unknown"
        ).strip().lower(),
        "message_type": str(
            message.get("type")
            or message.get("message_type")
            or "unknown"
        ).strip().lower(),
        "content": str(
            message.get("content")
            or message.get("content_clean")
            or message.get("content_raw_ocr")
            or ""
        ).strip(),
        "voice_duration": message.get("voice_duration"),
    }
    return "message_semantic_" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]


def attach_image_physical_anchors(
    screenshot: Image.Image,
    image_items: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach C2 role, physical side, neighbours and relative occurrence."""

    def message_identity(item: dict[str, Any]) -> str:
        for key in (
            "message_id",
            "id",
            "legacy_message_id",
            "original_message_id",
            "canonical_input_id",
        ):
            value = str(item.get(key) or "").strip()
            if value:
                return value
        return ""

    stable_by_message_id = {
        message_identity(message): stable_image_neighbor_signature(message)
        for message in messages
        if isinstance(message, dict) and message_identity(message)
    }
    image_role_rows: list[
        tuple[tuple[int, int, int, int], str]
    ] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        message_type = str(
            message.get("type")
            or message.get("message_type")
            or message.get("row_kind")
            or ""
        ).strip().lower()
        if message_type not in {"image", "image_bubble"}:
            continue
        role = str(message.get("sender_role") or "").strip().lower()
        if role not in {"customer", "self"}:
            continue
        rect = _visual_bounds(
            message.get("bubble_rect") or message.get("bounds")
        )
        if rect is not None:
            image_role_rows.append((rect, role))

    def c2_role_for_bounds(
        bounds: tuple[int, int, int, int],
        item: dict[str, Any],
    ) -> str:
        explicit = str(item.get("sender_role") or "").strip().lower()
        if explicit in {"customer", "self"}:
            return explicit
        center_x = (bounds[0] + bounds[2]) / 2.0
        center_y = (bounds[1] + bounds[3]) / 2.0
        candidates: list[tuple[float, str]] = []
        for message_bounds, role in image_role_rows:
            intersection_width = max(
                0,
                min(bounds[2], message_bounds[2])
                - max(bounds[0], message_bounds[0]),
            )
            intersection_height = max(
                0,
                min(bounds[3], message_bounds[3])
                - max(bounds[1], message_bounds[1]),
            )
            intersection = intersection_width * intersection_height
            if intersection > 0:
                candidates.append((float(intersection), role))
                continue
            if (
                message_bounds[0] <= center_x <= message_bounds[2]
                and message_bounds[1] <= center_y <= message_bounds[3]
            ):
                candidates.append((1.0, role))
        if not candidates:
            return "unknown"
        candidates.sort(key=lambda value: value[0], reverse=True)
        return candidates[0][1]
    text_rows: list[tuple[int, int, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        message_type = str(
            message.get("type") or message.get("message_type") or "text"
        ).strip().lower()
        if message_type != "text":
            continue
        rect = _visual_bounds(message.get("bubble_rect"))
        if rect is None:
            continue
        text_rows.append(
            (rect[1], rect[3], stable_image_neighbor_signature(message))
        )
    text_rows.sort(key=lambda item: (item[0], item[1], item[2]))

    ordered = sorted(
        [dict(item) for item in image_items if isinstance(item, dict)],
        key=lambda item: (
            (_visual_bounds(item.get("bounds") or item.get("bubble_rect")) or (0, 0, 0, 0))[1],
            (_visual_bounds(item.get("bounds") or item.get("bubble_rect")) or (0, 0, 0, 0))[0],
        ),
    )
    occurrences: dict[tuple[str, str, str, str], int] = {}
    occurrence_groups: list[tuple[str, str, str, str]] = []
    anchored: list[dict[str, Any]] = []
    for item in ordered:
        bounds = _visual_bounds(item.get("bounds") or item.get("bubble_rect"))
        if bounds is None:
            continue
        role = c2_role_for_bounds(bounds, item)
        visual_side = str(
            item.get("visual_side") or item.get("side") or "unknown"
        ).strip().lower()
        if visual_side not in {"customer", "self"}:
            visual_side = "unknown"
        preceding_id = str(item.get("_vision_preceding_text_id") or "").strip()
        following_id = str(item.get("_vision_following_text_id") or "").strip()
        preceding = stable_by_message_id.get(preceding_id, "")
        following = stable_by_message_id.get(following_id, "")
        if not preceding:
            prior = [row for row in text_rows if row[1] <= bounds[1] + 6]
            preceding = prior[-1][2] if prior else ""
        if not following:
            later = [row for row in text_rows if row[0] >= bounds[3] - 6]
            following = later[0][2] if later else ""
        fingerprint = image_bubble_visual_fingerprint(screenshot, bounds)
        occurrence_group = (role, preceding, following, fingerprint)
        occurrence_index = occurrences.get(occurrence_group, 0)
        occurrences[occurrence_group] = occurrence_index + 1
        item["image_physical_anchor"] = {
            "sender_role": role,
            "visual_side": visual_side,
            "visual_side_consistent": (
                role in {"customer", "self"} and role == visual_side
            ),
            "preceding_stable_message": preceding,
            "following_stable_message": following,
            "bubble_visual_fingerprint": fingerprint,
            "occurrence_index": occurrence_index,
            "occurrence_count": 0,
        }
        item["visual_fingerprint"] = fingerprint
        anchored.append(item)
        occurrence_groups.append(occurrence_group)
    occurrence_totals: dict[tuple[str, str, str, str], int] = {}
    for occurrence_group in occurrence_groups:
        occurrence_totals[occurrence_group] = (
            occurrence_totals.get(occurrence_group, 0) + 1
        )
    for item, occurrence_group in zip(anchored, occurrence_groups):
        item["image_physical_anchor"]["occurrence_count"] = occurrence_totals[
            occurrence_group
        ]
    return anchored


def detect_visual_image_bubbles(
    screenshot: Image.Image,
    *,
    messages: list[dict[str, Any]] | None = None,
    max_images: int = DEFAULT_MAX_VISIBLE_IMAGE_CANDIDATES,
    side_filter: str = "customer",
    time_markers: list[dict[str, Any]] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    image = screenshot.convert("RGB")
    width, height = image.size
    left, top, right, bottom = _chat_bounds(width, height)
    if right <= left or bottom <= top:
        return []
    crop = image.crop((left, top, right, bottom))
    scale = min(1.0, 220.0 / max(1, crop.width), 300.0 / max(1, crop.height))
    small = crop.resize((max(32, int(crop.width * scale)), max(32, int(crop.height * scale))), Image.Resampling.BILINEAR)
    background_stat = ImageStat.Stat(small)
    background = [
        float(value)
        for value in (background_stat.median or [242.0, 242.0, 242.0])[:3]
    ]
    block = 5
    grid_w = max(1, small.width // block)
    grid_h = max(1, small.height // block)
    protected_regions = explained_non_image_regions(messages)
    active = [[False for _ in range(grid_w)] for _ in range(grid_h)]
    for gy in range(grid_h):
        for gx in range(grid_w):
            box = (
                gx * block,
                gy * block,
                min(small.width, (gx + 1) * block),
                min(small.height, (gy + 1) * block),
            )
            active[gy][gx] = _media_surface_cell_active(
                ImageStat.Stat(small.crop(box)),
                background,
            )
    visited: set[tuple[int, int]] = set()
    candidates: list[dict[str, Any]] = []
    clean_side_filter = str(side_filter or "customer").strip().lower()
    if clean_side_filter not in {"customer", "self", "all"}:
        clean_side_filter = "customer"
    for gy in range(grid_h):
        for gx in range(grid_w):
            if not active[gy][gx] or (gx, gy) in visited:
                continue
            stack = [(gx, gy)]
            visited.add((gx, gy))
            cells: list[tuple[int, int]] = []
            while stack:
                cx, cy = stack.pop()
                cells.append((cx, cy))
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                    if nx < 0 or ny < 0 or nx >= grid_w or ny >= grid_h:
                        continue
                    if not active[ny][nx] or (nx, ny) in visited:
                        continue
                    visited.add((nx, ny))
                    stack.append((nx, ny))
            cells, vertical_whiskers_trimmed = (
                _trim_sparse_vertical_whiskers(cells)
            )
            min_x = min(point[0] for point in cells)
            max_x = max(point[0] for point in cells)
            min_y = min(point[1] for point in cells)
            max_y = max(point[1] for point in cells)
            bounds = (
                left + int((min_x * block) / scale),
                top + int((min_y * block) / scale),
                left + int(min(crop.width, ((max_x + 1) * block) / scale)),
                top + int(min(crop.height, ((max_y + 1) * block) / scale)),
            )
            # A component continuing through the cropped chat boundary may have
            # avatar or media pixels outside the current frame. It is not a
            # complete current-screen message and must not become an
            # actionable image target.
            if _bounds_continue_through_chat_crop_boundary(
                image,
                bounds,
                (left, top, right, bottom),
                background=background,
            ):
                continue
            bw = bounds[2] - bounds[0]
            bh = bounds[3] - bounds[1]
            area = bw * bh
            if bw < 90 or bh < 90 or area < 14000:
                continue
            component_cell_area = max(
                1,
                (max_x - min_x + 1) * (max_y - min_y + 1),
            )
            component_fill_ratio = len(cells) / component_cell_area
            # A media surface is a compact rectangular component. Disjoint UI
            # controls joined by a thin edge can span a large bounding box but
            # leave most of it empty; treating that box as an image creates a
            # false observation that blocks the whole authoritative frame.
            if component_fill_ratio < MIN_MEDIA_COMPONENT_FILL_RATIO:
                continue
            structural_side = _structural_media_side(image, bounds)
            if structural_side is None:
                continue
            side, structural_score, structure_evidence = structural_side
            if clean_side_filter != "all" and side != clean_side_filter:
                continue
            if _fine_grid_confirms_separate_stacked_surfaces(
                small,
                coarse_cells=cells,
                coarse_block=block,
                background=background,
                side=side,
                minimum_media_height=90.0 * scale,
            ):
                if diagnostics is not None:
                    diagnostics.append(
                        {
                            "event": "image_candidate_rejected_by_fine_grid",
                            "reason": "separate_stacked_chat_row_surfaces",
                            "bounds": list(bounds),
                            "side": side,
                            "component_fill_ratio": round(
                                component_fill_ratio,
                                6,
                            ),
                            "text_overlap_ratio": round(
                                _maximum_text_overlap_ratio(
                                    bounds,
                                    messages or [],
                                ),
                                6,
                            ),
                            "role_facing_edge_surface_continuity": round(
                                _role_facing_edge_surface_continuity(
                                    image,
                                    bounds,
                                    side=side,
                                    background=background,
                                ),
                                6,
                            ),
                        }
                    )
                continue
            bounds, avatar_column_excluded = _exclude_avatar_column_from_media_bounds(
                image,
                bounds,
                side=side,
            )
            bw = bounds[2] - bounds[0]
            bh = bounds[3] - bounds[1]
            area = bw * bh
            if bw < 90 or bh < 90 or area < 14000:
                continue
            text_overlap_ratio = _maximum_text_overlap_ratio(
                bounds,
                messages or [],
            )
            role_edge_continuity = _role_facing_edge_surface_continuity(
                image,
                bounds,
                side=side,
                background=background,
            )
            typed_conflict = explained_non_image_conflict(
                bounds,
                side=side,
                regions=protected_regions,
            )
            # Reliable type evidence is terminal for this frame.  Structural
            # media geometry is deliberately weaker evidence and must never
            # turn a trusted text or voice row back into an image.  In
            # particular, role-facing edge continuity cannot distinguish a
            # solid WeChat text bubble from an image surface.
            reliable_type_veto = typed_conflict is not None
            if reliable_type_veto:
                if diagnostics is not None:
                    diagnostics.append(
                        {
                            "event": (
                                "structural_image_candidate_rejected_by_"
                                "reliable_message_type"
                            ),
                            "reason": "reliable_non_image_region_overlap",
                            "bounds": list(bounds),
                            "side": side,
                            "message_type": typed_conflict[
                                "message_type"
                            ],
                            "protected_bounds": list(
                                typed_conflict["bounds"]
                            ),
                            "protected_evidence": typed_conflict[
                                "evidence"
                            ],
                            "voice_anchor_key": typed_conflict[
                                "voice_anchor_key"
                            ],
                            "contained_ratio": typed_conflict[
                                "contained_ratio"
                            ],
                            "role_facing_edge_surface_continuity": round(
                                role_edge_continuity,
                                6,
                            ),
                        }
                    )
                continue
            if (
                text_overlap_ratio >= TEXT_OVERLAP_REJECTION_RATIO
                and role_edge_continuity < MEDIA_ROLE_EDGE_CONTINUITY_RATIO
            ):
                continue
            if avatar_column_excluded:
                structure_evidence = [
                    *structure_evidence,
                    "avatar_column_excluded_from_media_bounds",
                ]
            if vertical_whiskers_trimmed:
                structure_evidence = [
                    *structure_evidence,
                    "sparse_vertical_whiskers_trimmed",
                ]
            visual_fingerprint = image_bubble_visual_fingerprint(image, bounds)
            if not visual_fingerprint:
                continue
            score = area + bounds[3] * 12 + structural_score * 10000
            candidates.append(
                {
                    "bounds": [int(value) for value in bounds],
                    "width": int(bw),
                    "height": int(bh),
                    "area": int(area),
                    "side": side,
                    "score": float(score),
                    "detection_method": "structural_media_lane_v1",
                    "component_fill_ratio": round(component_fill_ratio, 6),
                    "text_overlap_ratio": round(text_overlap_ratio, 6),
                    "role_facing_edge_surface_continuity": round(
                        role_edge_continuity,
                        6,
                    ),
                    "structure_evidence": structure_evidence,
                    "auxiliary_visual_evidence": [
                        "colour_texture_or_background_surface_component"
                    ],
                    "visual_fingerprint": visual_fingerprint,
                    "anchor": {"x": int((bounds[0] + bounds[2]) / 2), "y": int((bounds[1] + bounds[3]) / 2)},
                    "wechat_message_time": nearest_chat_time_marker(bounds, time_markers),
                }
            )
    candidates.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    limit = max(
        1,
        min(
            int(max_images or DEFAULT_MAX_VISIBLE_IMAGE_CANDIDATES),
            DEFAULT_MAX_VISIBLE_IMAGE_CANDIDATES,
        ),
    )
    if len(candidates) > limit:
        raise RuntimeError("C2_IMAGE_OBSERVATION_TRUNCATED")
    return candidates


def detect_customer_image_bubbles(
    screenshot: Image.Image,
    *,
    messages: list[dict[str, Any]] | None = None,
    max_images: int = 1,
) -> list[dict[str, Any]]:
    return detect_visual_image_bubbles(
        screenshot,
        messages=messages,
        max_images=max_images,
        side_filter="customer",
    )


def normalize_menu_text(value: Any) -> str:
    return re.sub(r"[\s.。…·\-_/\\\\]+", "", str(value or "")).lower()


def save_menu_priority(compact_text: str) -> int:
    compact = normalize_menu_text(compact_text)
    if "另存为" in compact or "saveas" in compact:
        return 3
    if "保存图片" in compact or "saveimage" in compact:
        return 2
    if "保存到" in compact or compact == "保存" or compact == "save":
        return 1
    return 0


def copy_menu_priority(compact_text: str) -> int:
    compact = normalize_menu_text(compact_text)
    if "复制图片" in compact or "copyimage" in compact:
        return 3
    if compact == "复制" or compact == "copy":
        return 2
    if "复制" in compact or "copy" in compact:
        return 1
    return 0


def _find_context_menu_item(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    tokens: tuple[str, ...],
    priority_fn: Any,
    anchor: tuple[int, int] | None = None,
) -> dict[str, Any] | None:
    width, height = image_size
    candidates: list[dict[str, Any]] = []
    normalized_tokens = [normalize_menu_text(token) for token in tokens]

    def item_geometry(item: dict[str, Any]) -> tuple[int, int, int, int, int, int] | None:
        try:
            left = max(0, int(float(item.get("left") or 0)))
            top = max(0, int(float(item.get("top") or 0)))
            right = min(width, int(float(item.get("right") or 0)))
            bottom = min(height, int(float(item.get("bottom") or 0)))
        except (TypeError, ValueError):
            return None
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom, int((left + right) / 2), int((top + bottom) / 2)

    for item in ocr_items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        compact = normalize_menu_text(text)
        if not compact:
            continue
        priority = int(priority_fn(compact) or 0)
        if priority < 2 and not any(token and compact == token for token in normalized_tokens):
            continue
        geometry = item_geometry(item)
        if geometry is None:
            continue
        item_left, item_top, item_right, item_bottom, center_x, center_y = geometry
        if anchor is not None:
            anchor_x, anchor_y = int(anchor[0]), int(anchor[1])
            if abs(center_x - anchor_x) > 360 or abs(center_y - anchor_y) > 420:
                continue
        click_bounds = [
            max(0, item_left - 20),
            max(0, item_top - 8),
            min(width, item_right + 20),
            min(height, item_bottom + 8),
        ]
        candidates.append(
            {
                "text": text,
                "bounds": click_bounds,
                "x": center_x,
                "y": center_y,
                "confidence": float(item.get("confidence") or 0.0),
                "priority": int(priority or 1),
                "distance_to_anchor": (
                    ((center_x - int(anchor[0])) ** 2 + (center_y - int(anchor[1])) ** 2) ** 0.5
                    if anchor is not None
                    else 0.0
                ),
                "menu_evidence": [{"text": text, "bounds": [item_left, item_top, item_right, item_bottom]}],
            }
        )
    if not candidates:
        return None
    if anchor is not None:
        return min(
            candidates,
            key=lambda item: (
                -int(item.get("priority") or 0),
                float(item.get("distance_to_anchor") or 0.0),
                -float(item.get("confidence") or 0.0),
            ),
        )
    return max(candidates, key=lambda item: (int(item.get("priority") or 0), float(item.get("confidence") or 0.0), int(item.get("y") or 0)))


def find_save_menu_item(ocr_items: list[dict[str, Any]], image_size: tuple[int, int]) -> dict[str, Any] | None:
    del ocr_items, image_size
    return None


def find_copy_menu_item(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    anchor: tuple[int, int] | None = None,
) -> dict[str, Any] | None:
    return _find_context_menu_item(
        ocr_items,
        image_size,
        tokens=COPY_IMAGE_MENU_TOKENS,
        priority_fn=copy_menu_priority,
        anchor=anchor,
    )


def planned_saved_image_path(
    artifact_dir: Path,
    *,
    target_name: str,
    session_key: str = "",
    source_preview: str = "",
    extension: str = ".jpg",
) -> Path:
    seed = json.dumps(
        {
            "target_name": target_name,
            "session_key": session_key,
            "source_preview": source_preview,
            "time": now_iso(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    ext = extension if str(extension).startswith(".") else f".{extension}"
    return artifact_dir / f"wx_image_{datetime.now().strftime('%H%M%S')}_{digest}{ext}"


def planned_visual_crop_path(
    artifact_dir: Path,
    *,
    target_name: str,
    session_key: str = "",
    visual_side: str = "customer",
    bounds: list[int] | tuple[int, int, int, int] | None = None,
    index: int = 0,
) -> Path:
    seed = json.dumps(
        {
            "target_name": target_name,
            "session_key": session_key,
            "visual_side": visual_side,
            "bounds": list(bounds or []),
            "time": now_iso(),
            "index": int(index),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    side = safe_path_part(visual_side or "visual", default="visual")
    return artifact_dir / f"wx_visual_{datetime.now().strftime('%H%M%S')}_{side}_{int(index):02d}_{digest}.png"


def clamp_bounds(bounds: list[int] | tuple[int, int, int, int], image_size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = image_size
    left, top, right, bottom = [int(value) for value in list(bounds)[:4]]
    left = max(0, min(width - 1, left))
    top = max(0, min(height - 1, top))
    right = max(left + 1, min(width, right))
    bottom = max(top + 1, min(height, bottom))
    return left, top, right, bottom


def click_context_menu_item(
    *,
    sidecar_ops: Any,
    hwnd: int,
    menu_target: dict[str, Any],
    action_name: str,
) -> dict[str, Any]:
    screen_click = getattr(sidecar_ops, "human_screen_click", None)
    win32gui = getattr(sidecar_ops, "win32gui", None)
    if callable(screen_click) and win32gui is not None:
        try:
            left, top, _right, _bottom = win32gui.GetWindowRect(hwnd)
            return screen_click(
                int(left) + int(menu_target.get("x") or 0),
                int(top) + int(menu_target.get("y") or 0),
                action_name=action_name,
            )
        except Exception:
            pass
    return sidecar_ops.human_window_image_click_in_bounds(
        hwnd,
        int(menu_target.get("x") or 0),
        int(menu_target.get("y") or 0),
        bounds=[int(value) for value in (menu_target.get("bounds") or [])[:4]],
        action_name=action_name,
    )


def _latest_visual_bubble(bubbles: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Select the latest visible occurrence by conversation position.

    Detection score may prefer a large older photo.  Clipboard acquisition is
    occurrence-oriented, so final selection must follow chat order rather
    than image appearance or area.
    """

    candidates = [item for item in (bubbles or []) if isinstance(item, dict)]
    if not candidates:
        return {}

    def position(item: dict[str, Any]) -> tuple[int, int, float]:
        bounds = item.get("bounds")
        try:
            top = int(float(bounds[1]))
            bottom = int(float(bounds[3]))
        except (TypeError, ValueError, IndexError):
            top, bottom = 0, 0
        return bottom, top, float(item.get("score") or 0.0)

    return dict(max(candidates, key=position))


def clipboard_sequence_number(sidecar_ops: Any) -> int | None:
    """Read the Windows clipboard generation without reading its content.

    The caller uses this to prove that the immediately preceding WeChat copy
    action replaced the clipboard.  A pre-existing image is never accepted.
    """
    provider = getattr(sidecar_ops, "clipboard_sequence_number", None)
    if not callable(provider):
        return None
    try:
        value = provider()
        return int(value) if value is not None else None
    except Exception:
        return None


def execute_wechat_clipboard_image_copy(
    *,
    hwnd: int,
    probe: dict[str, Any],
    target_name: str,
    session_key: str = "",
    exact: bool = True,
    source_preview: str = "",
    speaker_name: str = "",
    pending_signal_id: str = "",
    side_filter: str = "customer",
    sidecar_ops: Any,
) -> dict[str, Any]:
    """Copy one current customer image to the Windows clipboard without saving it.

    Screenshots are only transient geometry input.  This function deliberately
    returns no image, file path, crop, bounds, hash, or asset metadata: a
    caller in the same RPA lock must read the changed clipboard into memory.
    """
    captured_at = now_iso()
    visual_side = str(side_filter or "customer").strip().lower()
    if visual_side not in {"customer", "self"}:
        return {
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "image_clipboard_copy_failed",
            "reason": "image_clipboard_side_filter_invalid",
            "target": target_name,
            "session_key": session_key,
            "assets": [],
            "messages": [],
            "transaction": {"status": "failed", "captured_at": captured_at},
        }
    try:
        screenshot, _ = sidecar_ops.capture_wechat(hwnd, artifact_dir=None, label="image_clipboard_copy_before")
        ocr_items = sidecar_ops.run_ocr(screenshot)
    except Exception as exc:
        return {
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "image_clipboard_copy_failed",
            "reason": "image_clipboard_capture_surface_failed",
            "target": target_name,
            "session_key": session_key,
            "assets": [],
            "messages": [],
            "transaction": {"status": "failed", "captured_at": captured_at, "error": repr(exc)},
        }
    geometry = sidecar_ops.get_window_geometry(hwnd)
    image_size = getattr(screenshot, "size", (int(geometry.get("width") or 0), int(geometry.get("height") or 0)))
    messages = sidecar_ops.parse_messages_from_ocr(ocr_items, image_size, target=target_name)
    blocking_reason = sidecar_ops.blocking_screen_reason(ocr_items)
    if blocking_reason:
        return {
            "ok": False,
            "online": False if blocking_reason == "login_or_qr" else True,
            "adapter": "win32_ocr",
            "state": "image_clipboard_copy_blocked",
            "reason": blocking_reason,
            "target": target_name,
            "session_key": session_key,
            "assets": [],
            "messages": [],
            "transaction": {"status": "blocked", "captured_at": captured_at},
        }
    bubbles = detect_visual_image_bubbles(
        screenshot,
        messages=messages,
        max_images=DEFAULT_MAX_VISIBLE_IMAGE_CANDIDATES,
        side_filter=visual_side,
        time_markers=extract_chat_time_markers(ocr_items, image_size),
    )
    if not bubbles:
        return {
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "image_clipboard_copy_failed",
            "reason": f"{visual_side}_image_target_not_found",
            "target": target_name,
            "session_key": session_key,
            "assets": [],
            "messages": [],
            "transaction": {"status": "failed", "captured_at": captured_at},
        }
    sequence_before = clipboard_sequence_number(sidecar_ops)
    if sequence_before is None:
        return {
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "image_clipboard_copy_failed",
            "reason": "clipboard_sequence_unavailable",
            "target": target_name,
            "session_key": session_key,
            "assets": [],
            "messages": [],
            "transaction": {"status": "failed", "captured_at": captured_at},
        }
    bubble = _latest_visual_bubble(bubbles)
    anchor = dict(bubble.get("anchor") or {})
    bounds = [int(value) for value in (bubble.get("bounds") or [])[:4]]
    right_click = sidecar_ops.human_window_image_right_click_in_bounds(
        hwnd,
        int(anchor.get("x") or 0),
        int(anchor.get("y") or 0),
        bounds=bounds,
        action_name="image_clipboard_copy_context_right_click",
    )
    wait_for_menu = getattr(
        sidecar_ops,
        "wait_for_wechat_context_menu_stable",
        None,
    )
    if not callable(wait_for_menu):
        try:
            sidecar_ops.key_press(sidecar_ops.win32con.VK_ESCAPE)
        except Exception:
            pass
        return {
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "image_clipboard_copy_failed",
            "reason": "C2_IMAGE_MENU_OPERATION_FAILED",
            "target": target_name,
            "session_key": session_key,
            "assets": [],
            "messages": [],
            "transaction": {
                "status": "menu_panel_unconfirmed",
                "captured_at": captured_at,
                "right_click_ok": bool(right_click.get("ok")),
                "menu_copy_confirmed": False,
            },
        }
    wait_for_menu()
    observe_menu = getattr(sidecar_ops, "observe_wechat_context_menu", None)
    menu_observation: dict[str, Any] = {}
    if callable(observe_menu):
        try:
            menu_observation = observe_menu(
                hwnd,
                anchor_screen=(
                    int((right_click or {}).get("screen_x") or 0),
                    int((right_click or {}).get("screen_y") or 0),
                ),
                artifact_dir=None,
                label="image_clipboard_copy_context_menu",
            )
        except Exception:
            menu_observation = {}
    menu_items = [
        item
        for item in (menu_observation.get("local_ocr_items") or [])
        if isinstance(item, dict)
    ]
    menu_size = tuple(menu_observation.get("image_size") or image_size)
    copy_target = find_copy_menu_item(
        menu_items,
        menu_size,
        anchor=(
            int((right_click or {}).get("screen_x") or 0),
            int((right_click or {}).get("screen_y") or 0),
        ),
    )
    if not right_click.get("ok") or not copy_target:
        try:
            sidecar_ops.key_press(sidecar_ops.win32con.VK_ESCAPE)
        except Exception:
            pass
        return {
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "image_clipboard_copy_failed",
            "reason": "C2_IMAGE_MENU_OPERATION_FAILED",
            "target": target_name,
            "session_key": session_key,
            "assets": [],
            "messages": [],
            "transaction": {
                "status": (
                    "menu_evidence_incomplete"
                    if bool(right_click.get("ok"))
                    else "menu_panel_unconfirmed"
                ),
                "captured_at": captured_at,
                "right_click_ok": bool(right_click.get("ok")),
                "menu_copy_confirmed": False,
            },
        }
    menu_click = click_context_menu_item(
        sidecar_ops=sidecar_ops,
        hwnd=hwnd,
        menu_target=copy_target,
        action_name="image_clipboard_copy_menu_item_click",
    )
    if not menu_click.get("ok"):
        try:
            sidecar_ops.key_press(sidecar_ops.win32con.VK_ESCAPE)
        except Exception:
            pass
        return {
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "image_clipboard_copy_failed",
            "reason": "C2_IMAGE_MENU_OPERATION_FAILED",
            "target": target_name,
            "session_key": session_key,
            "assets": [],
            "messages": [],
            "transaction": {
                "status": "menu_copy_item_unsafe",
                "captured_at": captured_at,
                "right_click_ok": True,
                "menu_copy_confirmed": False,
            },
        }
    sequence_after: int | None = None
    for _ in range(6):
        sidecar_ops.humanized_action_sleep(80, 140)
        candidate = clipboard_sequence_number(sidecar_ops)
        if candidate is not None and candidate != sequence_before:
            sequence_after = candidate
            break
    if sequence_after is None:
        return {
            "ok": False,
            "online": True,
            "adapter": "win32_ocr",
            "state": "image_clipboard_copy_failed",
            "reason": "clipboard_sequence_unchanged_after_copy",
            "target": target_name,
            "session_key": session_key,
            "assets": [],
            "messages": [],
            "transaction": {
                "status": "failed",
                "captured_at": captured_at,
                "right_click_ok": True,
                "menu_copy_confirmed": True,
                "clipboard_sequence_changed": False,
            },
        }
    return {
        "ok": True,
        "online": True,
        "adapter": "win32_ocr",
        "state": "image_clipboard_copied",
        "target": target_name,
        "session_key": session_key,
        "assets": [],
        "messages": [],
        "transaction": {
            "status": "copied",
            "captured_at": captured_at,
            "right_click_ok": True,
            "menu_copy_confirmed": True,
            "clipboard_sequence_changed": True,
            "clipboard_sequence_after": sequence_after,
            "pending_signal_id": str(pending_signal_id or ""),
            "source": "clipboard_current_transaction",
            "visual_side": visual_side,
        },
    }
