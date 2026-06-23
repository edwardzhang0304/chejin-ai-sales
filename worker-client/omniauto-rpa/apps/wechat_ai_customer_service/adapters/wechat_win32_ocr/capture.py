"""Capture planning helpers for the Windows WeChat Win32/OCR adapter."""

from __future__ import annotations

from typing import Any, Callable


Rect = tuple[int, int, int, int]


def normalize_rect(rect: Any) -> Rect:
    left, top, right, bottom = rect
    return (int(left), int(top), int(right), int(bottom))


def capture_rect_candidates(rect: Any, *, dpi_scale: float = 1.0) -> list[Rect]:
    base = normalize_rect(rect)
    candidates = [base]
    scale = float(dpi_scale or 1.0)
    if scale > 1.05:
        candidates.append(
            (
                int(round(float(base[0]) / scale)),
                int(round(float(base[1]) / scale)),
                int(round(float(base[2]) / scale)),
                int(round(float(base[3]) / scale)),
            )
        )
        candidates.append(
            (
                int(round(float(base[0]) * scale)),
                int(round(float(base[1]) * scale)),
                int(round(float(base[2]) * scale)),
                int(round(float(base[3]) * scale)),
            )
        )
    return candidates


def collect_capture_candidates(
    rect: Any,
    *,
    dpi_scale: float,
    grabber: Callable[[Rect], Any | None],
) -> list[Any]:
    captures: list[Any] = []
    for candidate_rect in capture_rect_candidates(rect, dpi_scale=dpi_scale):
        image = grabber(candidate_rect)
        if image is not None:
            captures.append(image)
    return captures


def capture_window_by_rect(
    hwnd: int,
    *,
    rect_provider: Callable[[int], Any],
    dpi_scale_provider: Callable[[int], float],
    grabber: Callable[[Rect], Any | None],
) -> list[Any]:
    rect = rect_provider(hwnd)
    return collect_capture_candidates(
        rect,
        dpi_scale=dpi_scale_provider(hwnd),
        grabber=grabber,
    )


def capture_window_image(
    hwnd: int,
    *,
    win32gui_module: Any,
    win32ui_module: Any,
    user32: Any,
    image_factory: Any,
) -> Any | None:
    if win32ui_module is None or win32gui_module is None:
        return None
    left, top, right, bottom = win32gui_module.GetWindowRect(hwnd)
    width = max(0, int(right - left))
    height = max(0, int(bottom - top))
    if width <= 2 or height <= 2:
        return None
    hwnd_dc = None
    src_dc = None
    mem_dc = None
    bitmap = None
    try:
        hwnd_dc = win32gui_module.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None
        src_dc = win32ui_module.CreateDCFromHandle(hwnd_dc)
        mem_dc = src_dc.CreateCompatibleDC()
        bitmap = win32ui_module.CreateBitmap()
        bitmap.CreateCompatibleBitmap(src_dc, width, height)
        mem_dc.SelectObject(bitmap)

        rendered = int(user32.PrintWindow(hwnd, mem_dc.GetSafeHdc(), 0x2))
        if rendered != 1:
            rendered = int(user32.PrintWindow(hwnd, mem_dc.GetSafeHdc(), 0))
        if rendered != 1:
            return None

        bmpinfo = bitmap.GetInfo()
        bmpstr = bitmap.GetBitmapBits(True)
        image = image_factory.frombuffer(
            "RGB",
            (int(bmpinfo["bmWidth"]), int(bmpinfo["bmHeight"])),
            bmpstr,
            "raw",
            "BGRX",
            0,
            1,
        )
        return image
    except Exception:
        return None
    finally:
        if bitmap is not None:
            try:
                win32gui_module.DeleteObject(bitmap.GetHandle())
            except Exception:
                pass
        if mem_dc is not None:
            try:
                mem_dc.DeleteDC()
            except Exception:
                pass
        if src_dc is not None:
            try:
                src_dc.DeleteDC()
            except Exception:
                pass
        if hwnd_dc is not None:
            try:
                win32gui_module.ReleaseDC(hwnd, hwnd_dc)
            except Exception:
                pass


def try_image_grab(
    rect: Rect,
    *,
    image_grabber: Callable[..., Any],
) -> Any | None:
    left, top, right, bottom = rect
    if int(right - left) <= 2 or int(bottom - top) <= 2:
        return None
    try:
        return image_grabber(bbox=rect)
    except Exception:
        return None


def select_best_capture_candidate(
    candidates: list[Any],
    *,
    score: Callable[[Any], float],
) -> Any | None:
    if not candidates:
        return None
    return max(candidates, key=score)
