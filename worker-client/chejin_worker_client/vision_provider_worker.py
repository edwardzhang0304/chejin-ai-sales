from __future__ import annotations

import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys


OMNIAUTO_ROOT = Path(__file__).resolve().parents[1] / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))


def main() -> int:
    payload = None
    release_payload = None
    try:
        request = json.loads(sys.stdin.read() or "{}")
        config = request.get("config")
        encoded_image = str(request.get("image_base64") or "")
        if not isinstance(config, dict) or not encoded_image:
            raise ValueError("VISION_PROVIDER_WORKER_REQUEST_INVALID")
        image_bytes = base64.b64decode(encoded_image, validate=True)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            from apps.wechat_ai_customer_service.optional_plugins.vision.clipboard_payload import (
                ephemeral_image_from_memory,
            )
            from apps.wechat_ai_customer_service.optional_plugins.vision.lifecycle import (
                release_image_payload,
            )
            from apps.wechat_ai_customer_service.optional_plugins.vision.understanding.service import (
                maybe_run_customer_image_understanding,
            )

            payload = ephemeral_image_from_memory(
                image_bytes,
                mime_type=str(request.get("mime_type") or "image/png"),
                width=int(request.get("width") or 0),
                height=int(request.get("height") or 0),
            )
            if payload is None:
                raise ValueError("VISION_PROVIDER_IMAGE_INVALID")
            release_payload = release_image_payload
            result = maybe_run_customer_image_understanding(
                config=config,
                customer_text=str(request.get("customer_text") or ""),
                image_assets=[
                    {
                        "message_id": str(
                            request.get("message_id") or "memory-current-image"
                        ),
                        "message_type": "image",
                    }
                ],
                source_reason="vision_host_ports_current_transaction",
                image_payloads=[payload],
                ephemeral_clipboard=True,
            )
        if not isinstance(result, dict):
            raise TypeError("VISION_PROVIDER_RESULT_INVALID")
        envelope = {"ok": True, "result": result}
        exit_code = 0
    except Exception as exc:  # noqa: BLE001
        envelope = {
            "ok": False,
            "error_code": "VISION_PROVIDER_WORKER_FAILED",
            "exception_type": type(exc).__name__,
        }
        exit_code = 1
    finally:
        if payload is not None and callable(release_payload):
            try:
                release_payload(payload)
            except Exception:
                pass
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
