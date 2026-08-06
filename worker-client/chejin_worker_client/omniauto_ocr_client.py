from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from .emergency_stop import emergency_stop_requested
from .subprocess_protocol import (
    UNICODE_PROTOCOL_SENTINEL,
    encode_subprocess_json,
    require_unicode_protocol,
    subprocess_utf8_environment,
)


DEFAULT_OMNIAUTO_OCR_TIMEOUT_SECONDS = 45.0


class OmniAutoOcrCancelledError(RuntimeError):
    pass


def _cancel_requested(callback: Callable[[], bool] | None) -> bool:
    if emergency_stop_requested():
        return True
    if not callable(callback):
        return False
    try:
        return bool(callback())
    except Exception:
        return True


class CancellableOmniAutoOcr:
    """Keep OCR in a non-Qt child while one image transaction is active."""

    def __init__(self, cancel_check: Callable[[], bool] | None) -> None:
        self.cancel_check = cancel_check
        self.process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    @staticmethod
    def command() -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--omniauto-ocr-worker"]
        return [
            sys.executable,
            "-m",
            "chejin_worker_client.omniauto_ocr_worker",
        ]

    @staticmethod
    def timeout_seconds() -> float:
        try:
            value = float(
                os.environ.get("CHEJIN_OMNIAUTO_OCR_TIMEOUT_SECONDS")
                or DEFAULT_OMNIAUTO_OCR_TIMEOUT_SECONDS
            )
        except (TypeError, ValueError):
            value = DEFAULT_OMNIAUTO_OCR_TIMEOUT_SECONDS
        return max(5.0, min(120.0, value))

    def _start(self) -> subprocess.Popen[str]:
        if self.process is not None and self.process.poll() is None:
            return self.process
        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
            "env": subprocess_utf8_environment(),
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = int(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        self.process = subprocess.Popen(self.command(), **popen_kwargs)
        return self.process

    def _exchange(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = encode_subprocess_json(payload)
        with self._lock:
            process = self._start()
            if process.stdin is None or process.stdout is None:
                self.close()
                raise RuntimeError("omniauto_ocr_worker_pipe_unavailable")
            process.stdin.write(request + "\n")
            process.stdin.flush()
            response: dict[str, str] = {}

            def read_response() -> None:
                response["line"] = process.stdout.readline()

            thread = threading.Thread(
                target=read_response,
                name="chejin-omniauto-ocr-pipe",
                daemon=True,
            )
            thread.start()
            deadline = time.monotonic() + self.timeout_seconds()
            while thread.is_alive():
                if _cancel_requested(self.cancel_check):
                    self.close()
                    thread.join(timeout=5.0)
                    raise OmniAutoOcrCancelledError(
                        "vision_window_ocr_cancelled"
                    )
                if time.monotonic() >= deadline:
                    self.close()
                    thread.join(timeout=5.0)
                    raise TimeoutError("vision_window_ocr_timeout")
                thread.join(timeout=0.1)
            try:
                envelope = json.loads(response.get("line") or "{}")
            except json.JSONDecodeError as exc:
                self.close()
                raise RuntimeError("omniauto_ocr_worker_result_invalid") from exc
            return envelope

    def verify_unicode_protocol(self) -> None:
        envelope = self._exchange(
            {
                "protocol_probe_only": True,
                "protocol_unicode_sentinel": UNICODE_PROTOCOL_SENTINEL,
            }
        )
        if envelope.get("ok") is not True:
            raise RuntimeError("omniauto_ocr_worker_protocol_probe_failed")
        require_unicode_protocol(envelope)

    def recognize(self, image: Any) -> list[dict[str, Any]]:
        if _cancel_requested(self.cancel_check):
            raise OmniAutoOcrCancelledError("vision_window_ocr_cancelled")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        envelope = self._exchange(
            {
                "image_base64": base64.b64encode(
                    buffer.getvalue()
                ).decode("ascii"),
                "protocol_unicode_sentinel": UNICODE_PROTOCOL_SENTINEL,
            }
        )
        if envelope.get("ok") is not True:
            error = RuntimeError(
                str(
                    envelope.get("reason")
                    or envelope.get("error_code")
                    or "omniauto_ocr_worker_failed"
                )
            )
            error.diagnostic_traceback = str(  # type: ignore[attr-defined]
                envelope.get("traceback") or ""
            )
            raise error
        require_unicode_protocol(envelope)
        items = envelope.get("items")
        if not isinstance(items, list) or any(
            not isinstance(item, dict) for item in items
        ):
            raise RuntimeError("omniauto_ocr_worker_result_invalid")
        return items

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)
        finally:
            for stream in (process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass


def probe_omniauto_ocr_subprocess() -> dict[str, Any]:
    """Exercise the same non-Qt OCR boundary used by Windows image actions."""

    from PIL import Image

    runner = CancellableOmniAutoOcr(None)
    image = Image.new("RGB", (96, 48), "white")
    try:
        runner.verify_unicode_protocol()
        items = runner.recognize(image)
        return {
            "ok": True,
            "ocr_item_count": len(items),
            "protocol_unicode_verified": True,
        }
    except Exception as exc:  # noqa: BLE001
        message = str(exc or "").split(":", 1)[0].strip().lower()
        result = {
            "ok": False,
            "reason": message or "omniauto_ocr_worker_failed",
            "error_type": type(exc).__name__,
        }
        if os.environ.get("CHEJIN_PACKAGING_DIAGNOSTIC_PATH"):
            diagnostic_traceback = str(
                getattr(exc, "diagnostic_traceback", "") or ""
            ).strip()
            if diagnostic_traceback:
                result["diagnostic_traceback"] = diagnostic_traceback[-4000:]
        return result
    finally:
        image.close()
        runner.close()
