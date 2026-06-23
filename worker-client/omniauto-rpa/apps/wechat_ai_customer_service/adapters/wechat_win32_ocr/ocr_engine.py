"""OCR engine adapter helpers for the Windows WeChat Win32/OCR sidecar."""

from __future__ import annotations

from typing import Any, Callable

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.render_diagnostics import (
    likely_foreign_overlay_capture,
)
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.text_normalization import (
    normalize_ocr_text,
)


OCR_MIN_CONFIDENCE = 0.45


def normalize_ocr_rows(
    result: Any,
    *,
    min_confidence: float = OCR_MIN_CONFIDENCE,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in result or []:
        try:
            box, text, confidence = row
        except ValueError:
            continue
        clean = normalize_ocr_text(text)
        if not clean:
            continue
        try:
            conf = float(confidence)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_confidence:
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        items.append(
            {
                "text": clean,
                "confidence": conf,
                "box": box,
                "left": min(xs),
                "right": max(xs),
                "top": min(ys),
                "bottom": max(ys),
                "center_x": sum(xs) / len(xs),
                "center_y": sum(ys) / len(ys),
            }
        )
    items.sort(key=lambda item: (float(item["top"]), float(item["left"])))
    if likely_foreign_overlay_capture(items):
        return []
    return items


class OcrEngineRunner:
    def __init__(self, engine_factory: Callable[[], Any] | None, *, import_error: str = "") -> None:
        self._engine_factory = engine_factory
        self._import_error = str(import_error or "")
        self._engine: Any | None = None

    def run(self, image: Any) -> list[dict[str, Any]]:
        if self._engine_factory is None:
            raise RuntimeError(f"rapidocr_onnxruntime_unavailable: {self._import_error}")
        if self._engine is None:
            self._engine = self._engine_factory()
        result, _ = self._engine(image)
        return normalize_ocr_rows(result)


def run_ocr_with_cache(
    image: Any,
    *,
    engine_factory: Callable[[], Any] | None,
    engine: Any | None,
    import_error: str = "",
    min_confidence: float = OCR_MIN_CONFIDENCE,
) -> tuple[list[dict[str, Any]], Any | None]:
    if engine_factory is None:
        raise RuntimeError(f"rapidocr_onnxruntime_unavailable: {import_error}")
    cached_engine = engine
    if cached_engine is None:
        cached_engine = engine_factory()
    result, _ = cached_engine(image)
    return normalize_ocr_rows(result, min_confidence=min_confidence), cached_engine


def create_ocr_runner(engine_factory: Callable[[], Any] | None, *, import_error: str = "") -> OcrEngineRunner:
    return OcrEngineRunner(engine_factory, import_error=import_error)
