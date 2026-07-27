from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any

from PIL import Image, ImageStat

DEFAULT_BOTTOM_EXCLUDE_PX = 95
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


def _covered_by_text(bounds: tuple[int, int, int, int], messages: list[dict[str, Any]]) -> bool:
    left, top, right, bottom = bounds
    area = max(1, (right - left) * (bottom - top))
    for message in messages:
        if not isinstance(message, dict):
            continue
        rect = message.get("bubble_rect") if isinstance(message.get("bubble_rect"), dict) else {}
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
        if overlap / area >= 0.42:
            return True
    return False


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


def detect_visual_image_bubbles(
    screenshot: Image.Image,
    *,
    messages: list[dict[str, Any]] | None = None,
    max_images: int = 1,
    side_filter: str = "customer",
    time_markers: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    image = screenshot.convert("RGB")
    width, height = image.size
    left, top, right, bottom = _chat_bounds(width, height)
    if right <= left or bottom <= top:
        return []
    crop = image.crop((left, top, right, bottom))
    scale = min(1.0, 220.0 / max(1, crop.width), 300.0 / max(1, crop.height))
    small = crop.resize((max(32, int(crop.width * scale)), max(32, int(crop.height * scale))), Image.Resampling.BILINEAR)
    block = 5
    grid_w = max(1, small.width // block)
    grid_h = max(1, small.height // block)
    active = [[False for _ in range(grid_w)] for _ in range(grid_h)]
    for gy in range(grid_h):
        for gx in range(grid_w):
            box = (
                gx * block,
                gy * block,
                min(small.width, (gx + 1) * block),
                min(small.height, (gy + 1) * block),
            )
            stat = ImageStat.Stat(small.crop(box))
            mean = stat.mean or [0.0, 0.0, 0.0]
            spread = sum(float(value) for value in (stat.stddev or [0.0, 0.0, 0.0])) / 3.0
            delta = max(mean) - min(mean)
            brightness = sum(float(value) for value in mean) / 3.0
            # A screenshot/document image can be almost monochrome (for
            # example a dark terminal screenshot), so it has neither colour
            # delta nor enough local texture for the original detector. Keep
            # sufficiently large dark surfaces as candidates; the later
            # connected-component size and text-overlap checks still reject
            # avatars, glyphs and ordinary UI chrome.
            dark_low_texture_surface = brightness <= 58.0 and spread <= 20.0
            active[gy][gx] = spread >= 16.0 or delta >= 24.0 or dark_low_texture_surface
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
            bw = bounds[2] - bounds[0]
            bh = bounds[3] - bounds[1]
            area = bw * bh
            if bw < 90 or bh < 90 or area < 14000:
                continue
            if _covered_by_text(bounds, messages or []):
                continue
            structural_side = _structural_media_side(image, bounds)
            if structural_side is None:
                continue
            side, structural_score, structure_evidence = structural_side
            if clean_side_filter != "all" and side != clean_side_filter:
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
                    "structure_evidence": structure_evidence,
                    "auxiliary_visual_evidence": ["colour_texture_connected_component"],
                    "anchor": {"x": int((bounds[0] + bounds[2]) / 2), "y": int((bounds[1] + bounds[3]) / 2)},
                    "wechat_message_time": nearest_chat_time_marker(bounds, time_markers),
                }
            )
    candidates.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    return candidates[: max(1, min(int(max_images or 1), 8))]


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
) -> dict[str, Any] | None:
    width, height = image_size
    candidates: list[dict[str, Any]] = []
    normalized_tokens = [normalize_menu_text(token) for token in tokens]
    for item in ocr_items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        compact = normalize_menu_text(text)
        if not compact:
            continue
        priority = int(priority_fn(compact) or 0)
        if priority <= 0 and not any(token and token in compact for token in normalized_tokens):
            continue
        left = max(0, int(float(item.get("left") or 0)) - 28)
        top = max(0, int(float(item.get("top") or 0)) - 12)
        right = min(width, int(float(item.get("right") or 0)) + 60)
        bottom = min(height, int(float(item.get("bottom") or 0)) + 14)
        if right <= left or bottom <= top:
            continue
        candidates.append(
            {
                "text": text,
                "bounds": [left, top, right, bottom],
                "x": int((left + right) / 2),
                "y": int((top + bottom) / 2),
                "confidence": float(item.get("confidence") or 0.0),
                "priority": int(priority or 1),
            }
        )
    if not candidates:
        return None
    return max(candidates, key=lambda item: (int(item.get("priority") or 0), float(item.get("confidence") or 0.0), int(item.get("y") or 0)))


def find_save_menu_item(ocr_items: list[dict[str, Any]], image_size: tuple[int, int]) -> dict[str, Any] | None:
    del ocr_items, image_size
    return None


def find_copy_menu_item(ocr_items: list[dict[str, Any]], image_size: tuple[int, int]) -> dict[str, Any] | None:
    return _find_context_menu_item(
        ocr_items,
        image_size,
        tokens=COPY_IMAGE_MENU_TOKENS,
        priority_fn=copy_menu_priority,
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


def capture_context_menu_image(
    *,
    sidecar_ops: Any,
    hwnd: int,
    artifact_dir: str,
    label: str,
) -> tuple[Any, str, str]:
    # Geometry/menu OCR is transient.  Ignore the historical artifact-dir
    # argument so no caller can turn this helper into a screenshot archive.
    del artifact_dir
    visible_capture = getattr(sidecar_ops, "capture_wechat_window_visible_screen", None)
    if callable(visible_capture):
        try:
            image, _path = visible_capture(hwnd, artifact_dir=None, label=label)
            return image, "", "visible_window"
        except Exception:
            pass
    image, _path = sidecar_ops.capture_wechat(hwnd, artifact_dir=None, label=label)
    return image, "", "window_capture"


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
        max_images=8,
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
    sidecar_ops.humanized_action_sleep(360, 720)
    try:
        menu_screenshot, _menu_path, _menu_capture_method = capture_context_menu_image(
            sidecar_ops=sidecar_ops,
            hwnd=hwnd,
            artifact_dir="",
            label="image_clipboard_copy_context_menu",
        )
        menu_items = sidecar_ops.run_ocr(menu_screenshot)
        menu_size = getattr(menu_screenshot, "size", image_size)
        copy_target = find_copy_menu_item(menu_items, menu_size)
    except Exception:
        copy_target = None
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
            "reason": "image_context_menu_copy_item_missing",
            "target": target_name,
            "session_key": session_key,
            "assets": [],
            "messages": [],
            "transaction": {
                "status": "failed",
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
            "reason": "image_context_menu_copy_click_failed",
            "target": target_name,
            "session_key": session_key,
            "assets": [],
            "messages": [],
            "transaction": {
                "status": "failed",
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
