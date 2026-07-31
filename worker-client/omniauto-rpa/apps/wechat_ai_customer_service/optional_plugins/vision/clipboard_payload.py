"""Ephemeral Windows clipboard image payloads for the optional vision plugin.

This module intentionally has no filesystem API.  A payload can be passed to
the visual provider in memory and must be released before any caller builds a
ledger, capture, or Brain payload.
"""

from __future__ import annotations

import ctypes
import io
import struct
import time
from dataclasses import dataclass
from typing import Any, Callable

from PIL import Image, ImageOps

from .limits import resolve_image_source_limits
from .understanding.provider import (
    MAX_IMAGE_EDGE_PX,
    MAX_IMAGE_PAYLOAD_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_RAW_BYTES,
    MAX_IMAGE_SOURCE_BYTES,
)


# The only live input route is WeChat's current right-click Copy result.  These
# formats are read directly from that clipboard generation in memory; file-drop,
# URL, HTML, text, and the OLE/DataObject wrapper are intentionally ignored.
_CF_BITMAP = 2
_CF_DIB = 8
_CF_DIBV5 = 17
_NATIVE_IMAGE_FORMAT_PRIORITY = (
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/bmp",
)
_ALLOWED_ENCODED_IMAGE_FORMATS = {"PNG", "JPEG", "WEBP", "BMP"}


@dataclass
class EphemeralClipboardImage:
    """A non-serializable, in-memory image payload with explicit cleanup."""

    image_bytes: bytearray
    mime_type: str
    width: int
    height: int
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        for index in range(len(self.image_bytes)):
            self.image_bytes[index] = 0
        self.image_bytes.clear()
        self.released = True


def windows_clipboard_sequence_number() -> int | None:
    try:
        user32 = getattr(getattr(ctypes, "windll", None), "user32", None)
        getter = getattr(user32, "GetClipboardSequenceNumber", None)
        if not callable(getter):
            return None
        value = int(getter())
        return value if value > 0 else None
    except Exception:
        return None


def _clipboard_image(value: Any) -> Image.Image | None:
    if not isinstance(value, Image.Image):
        return None
    try:
        image = ImageOps.exif_transpose(value).convert("RGB")
        image.load()
        return image
    except (OSError, ValueError):
        return None


def _image_from_encoded_clipboard_bytes(
    value: Any,
    *,
    max_source_bytes: int = MAX_IMAGE_SOURCE_BYTES,
) -> Image.Image | None:
    """Decode a native clipboard image blob without touching the filesystem."""

    if not isinstance(value, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(value)
    if not raw or len(raw) > int(max_source_bytes):
        return None
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if str(source.format or "").upper() not in _ALLOWED_ENCODED_IMAGE_FORMATS:
                return None
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.load()
            return image
    except (Image.DecompressionBombError, OSError, ValueError, Warning):
        return None


def _dib_as_bmp_bytes(
    value: Any,
    *,
    max_raw_bytes: int = MAX_IMAGE_RAW_BYTES,
) -> bytes | None:
    """Add an in-memory BMP file header to a native CF_DIB/CF_DIBV5 payload."""

    if not isinstance(value, (bytes, bytearray, memoryview)):
        return None
    dib = bytes(value)
    if len(dib) < 12 or len(dib) > int(max_raw_bytes):
        return None
    try:
        header_size = int(struct.unpack_from("<I", dib, 0)[0])
    except struct.error:
        return None
    if header_size not in {12, 40, 52, 56, 64, 108, 124} or header_size > len(dib):
        return None
    try:
        if header_size == 12:
            bit_count = int(struct.unpack_from("<H", dib, 10)[0])
            colors_used = 0
            color_size = 3
            compression = 0
        else:
            bit_count = int(struct.unpack_from("<H", dib, 14)[0])
            compression = int(struct.unpack_from("<I", dib, 16)[0])
            colors_used = int(struct.unpack_from("<I", dib, 32)[0])
            color_size = 4
    except struct.error:
        return None
    palette_entries = colors_used if colors_used > 0 else (1 << bit_count if 0 < bit_count <= 8 else 0)
    bitfield_bytes = 0
    if header_size == 40 and compression in {3, 6}:  # BI_BITFIELDS / BI_ALPHABITFIELDS
        bitfield_bytes = 16 if compression == 6 else 12
    pixel_offset = 14 + header_size + bitfield_bytes + palette_entries * color_size
    if pixel_offset >= 14 + len(dib):
        return None
    try:
        file_header = struct.pack("<2sIHHI", b"BM", 14 + len(dib), 0, 0, pixel_offset)
    except struct.error:
        return None
    return file_header + dib


def _image_from_hbitmap(
    value: Any,
    *,
    max_pixels: int = MAX_IMAGE_PIXELS,
    max_raw_bytes: int = MAX_IMAGE_RAW_BYTES,
) -> Image.Image | None:
    """Read CF_BITMAP into a temporary in-memory DIB; never save the bitmap."""

    try:
        hbitmap = int(value)
    except (TypeError, ValueError):
        return None
    if hbitmap <= 0:
        return None

    class _Bitmap(ctypes.Structure):
        _fields_ = [
            ("bmType", ctypes.c_long),
            ("bmWidth", ctypes.c_long),
            ("bmHeight", ctypes.c_long),
            ("bmWidthBytes", ctypes.c_long),
            ("bmPlanes", ctypes.c_ushort),
            ("bmBitsPixel", ctypes.c_ushort),
            ("bmBits", ctypes.c_void_p),
        ]

    class _BitmapInfoHeader(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", ctypes.c_ushort),
            ("biBitCount", ctypes.c_ushort),
            ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    class _BitmapInfo(ctypes.Structure):
        _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", ctypes.c_uint32 * 1)]

    user32 = getattr(getattr(ctypes, "windll", None), "user32", None)
    gdi32 = getattr(getattr(ctypes, "windll", None), "gdi32", None)
    if user32 is None or gdi32 is None:
        return None
    bitmap = _Bitmap()
    if int(gdi32.GetObjectW(hbitmap, ctypes.sizeof(bitmap), ctypes.byref(bitmap))) <= 0:
        return None
    width, height = int(bitmap.bmWidth), abs(int(bitmap.bmHeight))
    if width <= 0 or height <= 0 or width * height > int(max_pixels):
        return None
    byte_count = width * height * 4
    if byte_count <= 0 or byte_count > int(max_raw_bytes):
        return None
    hdc = user32.GetDC(0)
    if not hdc:
        return None
    raw = (ctypes.c_ubyte * byte_count)()
    info = _BitmapInfo()
    info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height  # top-down, avoids reversing the image.
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0  # BI_RGB
    try:
        rows = int(gdi32.GetDIBits(hdc, hbitmap, 0, height, ctypes.byref(raw), ctypes.byref(info), 0))
    finally:
        user32.ReleaseDC(0, hdc)
    if rows != height:
        return None
    image: Image.Image | None = None
    try:
        image = Image.frombuffer("RGBA", (width, height), raw, "raw", "BGRA", 0, 1)
        return image.convert("RGB")
    except (OSError, ValueError):
        return None
    finally:
        if image is not None:
            image.close()
        for index in range(len(raw)):
            raw[index] = 0


def _decode_native_clipboard_value(
    *,
    format_id: int,
    format_name: str,
    value: Any,
    source_limits: dict[str, Any] | None = None,
) -> Image.Image | None:
    """Accept only native image clipboard formats, never paths or text."""

    limits = resolve_image_source_limits(source_limits)
    name = str(format_name or "").strip().lower()
    if name in _NATIVE_IMAGE_FORMAT_PRIORITY:
        return _image_from_encoded_clipboard_bytes(
            value,
            max_source_bytes=limits["max_encoded_source_bytes"],
        )
    if format_id in {_CF_DIB, _CF_DIBV5}:
        bmp = _dib_as_bmp_bytes(
            value,
            max_raw_bytes=limits["max_decoded_rgba_bytes"],
        )
        return (
            _image_from_encoded_clipboard_bytes(
                bmp,
                max_source_bytes=limits["max_decoded_rgba_bytes"],
            )
            if bmp is not None
            else None
        )
    if format_id == _CF_BITMAP:
        return _image_from_hbitmap(
            value,
            max_pixels=limits["max_decoded_pixels"],
            max_raw_bytes=limits["max_decoded_rgba_bytes"],
        )
    return None


def _native_clipboard_format_name(clipboard: Any, format_id: int) -> str:
    try:
        return str(clipboard.GetClipboardFormatName(format_id) or "")
    except Exception:
        return ""


def _read_current_windows_native_clipboard_image(
    *,
    source_limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read a native bitmap from the current Windows clipboard generation only."""

    try:
        import win32clipboard  # Lazy optional Windows dependency.
    except Exception as exc:
        return {"ok": False, "reason": "clipboard_native_reader_unavailable", "error": repr(exc)}

    opened = False
    try:
        win32clipboard.OpenClipboard()
        opened = True
        found: list[tuple[int, str]] = []
        current = 0
        while True:
            current = int(win32clipboard.EnumClipboardFormats(current) or 0)
            if not current:
                break
            found.append((current, _native_clipboard_format_name(win32clipboard, current)))
        ordered: list[tuple[int, str]] = []
        for expected_name in _NATIVE_IMAGE_FORMAT_PRIORITY:
            ordered.extend((format_id, name) for format_id, name in found if name.strip().lower() == expected_name)
        ordered.extend((format_id, name) for format_id, name in found if format_id in {_CF_DIBV5, _CF_DIB, _CF_BITMAP})
        seen: set[int] = set()
        for format_id, format_name in ordered:
            if format_id in seen:
                continue
            seen.add(format_id)
            try:
                value = win32clipboard.GetClipboardData(format_id)
            except Exception:
                continue
            image = _decode_native_clipboard_value(
                format_id=format_id,
                format_name=format_name,
                value=value,
                source_limits=source_limits,
            )
            if image is not None:
                return {"ok": True, "image": image}
        return {"ok": False, "reason": "clipboard_current_content_not_bitmap"}
    except Exception as exc:
        return {"ok": False, "reason": "clipboard_current_read_failed", "error": repr(exc)}
    finally:
        if opened:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass


def _read_injected_clipboard_image(reader: Callable[[], Any]) -> dict[str, Any]:
    """Compatibility seam for deterministic tests; it accepts only a bitmap object."""

    try:
        value = reader()
    except (Image.DecompressionBombError, OSError, ValueError, Warning) as exc:
        return {"ok": False, "reason": "clipboard_current_read_failed", "error": repr(exc)}
    if isinstance(value, (list, tuple)):
        return {"ok": False, "reason": "clipboard_current_content_not_bitmap"}
    image = _clipboard_image(value)
    if image is None:
        return {"ok": False, "reason": "clipboard_current_content_not_bitmap"}
    return {"ok": True, "image": image}


def _encode_ephemeral_image(
    image: Image.Image,
    *,
    source_limits: dict[str, Any] | None = None,
) -> EphemeralClipboardImage | None:
    limits = resolve_image_source_limits(source_limits)
    max_pixels = limits["max_decoded_pixels"]
    max_edge = limits["max_provider_edge_px"]
    max_payload_bytes = limits["max_provider_payload_bytes"]
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > max_pixels:
        return None
    normalized = image
    owns_normalized = False
    if max(width, height) > max_edge:
        normalized = image.copy()
        owns_normalized = True
        normalized.thumbnail(
            (max_edge, max_edge),
            Image.Resampling.LANCZOS,
        )
        width, height = normalized.size
    try:
        attempts = [
            ("PNG", "image/png", {"optimize": True, "compress_level": 9}),
            *[
                (
                    "JPEG",
                    "image/jpeg",
                    {
                        "quality": quality,
                        "optimize": True,
                        "progressive": True,
                        "subsampling": 2,
                    },
                )
                for quality in (92, 86, 80, 74, 68, 60, 52, 44)
            ],
        ]
        for image_format, mime_type, save_options in attempts:
            buffer = io.BytesIO()
            try:
                normalized.save(
                    buffer,
                    format=image_format,
                    **save_options,
                )
                raw = buffer.getvalue()
            finally:
                buffer.close()
            if raw and len(raw) <= max_payload_bytes:
                return EphemeralClipboardImage(
                    image_bytes=bytearray(raw),
                    mime_type=mime_type,
                    width=int(width),
                    height=int(height),
                )
        return None
    finally:
        if owns_normalized:
            normalized.close()


def ephemeral_image_from_memory(
    value: Any,
    *,
    mime_type: str = "image/png",
    width: int = 0,
    height: int = 0,
    source_limits: dict[str, Any] | None = None,
) -> EphemeralClipboardImage | None:
    """Normalize a host-port bitmap result into the module-owned payload."""

    if isinstance(value, EphemeralClipboardImage):
        return None if value.released else value
    raw = value.get("image") if isinstance(value, dict) and value.get("ok") else value
    if isinstance(raw, EphemeralClipboardImage):
        return None if raw.released else raw
    if isinstance(raw, (bytes, bytearray, memoryview)):
        content = bytes(raw)
        limits = resolve_image_source_limits(source_limits)
        if (
            not content
            or len(content) > limits["max_encoded_source_bytes"]
        ):
            return None
        image = _image_from_encoded_clipboard_bytes(
            content,
            max_source_bytes=limits["max_encoded_source_bytes"],
        )
        if image is None:
            return None
        try:
            return _encode_ephemeral_image(
                image,
                source_limits=limits,
            )
        finally:
            image.close()
    image = _clipboard_image(raw)
    if image is None:
        return None
    try:
        return _encode_ephemeral_image(
            image,
            source_limits=source_limits,
        )
    finally:
        image.close()


def read_current_clipboard_image(
    transaction: dict[str, Any] | None,
    *,
    clipboard_reader: Callable[[], Any] | None = None,
    sequence_provider: Callable[[], int | None] | None = None,
    source_limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read only the clipboard generation produced by the current RPA copy.

    The expected generation comes from the sidecar after it has clicked
    WeChat's Copy menu item.  Any later mutation, stale generation, file-list,
    text, invalid bitmap, or oversized image is rejected rather than guessed.
    """
    tx = transaction if isinstance(transaction, dict) else {}
    expected = tx.get("clipboard_sequence_after")
    try:
        expected_sequence = int(expected)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "clipboard_sequence_missing"}
    if expected_sequence <= 0:
        return {"ok": False, "reason": "clipboard_sequence_missing"}
    current_sequence = sequence_provider or windows_clipboard_sequence_number
    before = current_sequence()
    if before != expected_sequence:
        return {"ok": False, "reason": "clipboard_sequence_not_current"}
    # A supplied reader is retained only as a deterministic compatibility seam
    # for tests.  Live Windows use raw native image formats; the OLE/DataObject
    # wrapper is deliberately not part of the formal recognition route because
    # it can surface WeChat images as a one-item file list.
    read_result = (
        _read_injected_clipboard_image(clipboard_reader)
        if clipboard_reader is not None
        else _read_current_windows_native_clipboard_image(
            source_limits=source_limits,
        )
    )
    if not read_result.get("ok"):
        return read_result
    image = read_result.get("image")
    if not isinstance(image, Image.Image):
        return {"ok": False, "reason": "clipboard_current_content_not_bitmap"}
    try:
        payload = _encode_ephemeral_image(
            image,
            source_limits=source_limits,
        )
    finally:
        try:
            image.close()
        except Exception:
            pass
    after = current_sequence()
    if after != expected_sequence:
        if payload is not None:
            payload.release()
        return {"ok": False, "reason": "clipboard_sequence_changed_during_read"}
    if payload is None:
        return {"ok": False, "reason": "clipboard_current_image_invalid"}
    return {"ok": True, "image": payload}


def clear_current_windows_clipboard_image(
    expected_sequence: int,
) -> dict[str, Any]:
    """Clear only the clipboard generation read by this image transaction."""

    current = windows_clipboard_sequence_number()
    if current != int(expected_sequence):
        return {
            "ok": False,
            "reason": "clipboard_sequence_not_current_for_clear",
        }
    try:
        import win32clipboard
    except Exception as exc:
        return {
            "ok": False,
            "reason": "clipboard_clear_unavailable",
            "error_type": type(exc).__name__,
        }
    deadline = time.monotonic() + 2.0
    last_error_type = ""
    while True:
        opened = False
        try:
            win32clipboard.OpenClipboard()
            opened = True
            if windows_clipboard_sequence_number() != int(
                expected_sequence
            ):
                return {
                    "ok": False,
                    "reason": "clipboard_sequence_changed_before_clear",
                }
            win32clipboard.EmptyClipboard()
            return {"ok": True}
        except Exception as exc:
            last_error_type = type(exc).__name__
        finally:
            if opened:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
        if time.monotonic() >= deadline:
            return {
                "ok": False,
                "reason": "clipboard_clear_failed",
                "error_type": last_error_type,
            }
        if windows_clipboard_sequence_number() != int(expected_sequence):
            return {
                "ok": False,
                "reason": "clipboard_sequence_changed_before_clear",
            }
        time.sleep(0.05)
