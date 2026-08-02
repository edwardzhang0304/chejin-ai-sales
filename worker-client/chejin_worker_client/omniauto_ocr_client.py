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
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = int(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        self.process = subprocess.Popen(self.command(), **popen_kwargs)
        return self.process

    def recognize(self, image: Any) -> list[dict[str, Any]]:
        if _cancel_requested(self.cancel_check):
            raise OmniAutoOcrCancelledError("vision_window_ocr_cancelled")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        request = json.dumps(
            {
                "image_base64": base64.b64encode(
                    buffer.getvalue()
                ).decode("ascii")
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
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


def probe_omniauto_ocr_subprocess() -> dict[str, Any]:
    """Exercise the same non-Qt OCR boundary used by Windows image actions."""

    from PIL import Image

    runner = CancellableOmniAutoOcr(None)
    image = Image.new("RGB", (96, 48), "white")
    try:
        items = runner.recognize(image)
        return {"ok": True, "ocr_item_count": len(items)}
    except Exception as exc:  # noqa: BLE001
        message = str(exc or "").split(":", 1)[0].strip().lower()
        return {
            "ok": False,
            "reason": message or "omniauto_ocr_worker_failed",
            "error_type": type(exc).__name__,
        }
    finally:
        image.close()
        runner.close()
