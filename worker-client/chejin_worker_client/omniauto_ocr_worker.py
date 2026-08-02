from __future__ import annotations

import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import re
import sys
import traceback
from typing import Any

from PIL import Image


OMNIAUTO_ROOT = Path(__file__).resolve().parents[1] / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))


def _stable_reason(exc: Exception) -> str:
    token = str(exc or "").split(":", 1)[0].strip()
    if token and re.fullmatch(r"[A-Za-z0-9_]+", token):
        return token.lower()
    return "omniauto_ocr_worker_failed"


def process_request(request: dict[str, Any]) -> dict[str, Any]:
    encoded_image = str(request.get("image_base64") or "")
    if not encoded_image:
        raise ValueError("OMNIAUTO_OCR_WORKER_REQUEST_INVALID")
    image_bytes = base64.b64decode(encoded_image, validate=True)
    image = None
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            from apps.wechat_ai_customer_service.adapters import (
                wechat_win32_ocr_sidecar,
            )

            items = wechat_win32_ocr_sidecar.run_ocr(image)
        if not isinstance(items, list) or any(
            not isinstance(item, dict) for item in items
        ):
            raise TypeError("OMNIAUTO_OCR_RESULT_INVALID")
        return {"ok": True, "items": items}
    finally:
        if image is not None:
            image.close()


def _error_envelope(exc: Exception) -> dict[str, Any]:
    from .incident_evidence import redact_diagnostic

    return {
        "ok": False,
        "error_code": "OMNIAUTO_OCR_WORKER_FAILED",
        "reason": _stable_reason(exc),
        "exception_type": type(exc).__name__,
        "traceback": str(
            redact_diagnostic(
                "".join(
                    traceback.format_exception(
                        type(exc),
                        exc,
                        exc.__traceback__,
                    )
                )
            )
        ),
    }


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("OMNIAUTO_OCR_WORKER_REQUEST_INVALID")
            envelope = process_request(request)
        except Exception as exc:  # noqa: BLE001
            envelope = _error_envelope(exc)
        sys.stdout.write(
            json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
