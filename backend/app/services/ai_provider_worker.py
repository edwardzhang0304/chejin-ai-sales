from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import time


BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _safe_label(value: object, *, fallback: str) -> str:
    text = str(value or "").strip()[:96]
    normalized = "".join(
        character
        if character.isalnum() or character in {"_", "-", "."}
        else "_"
        for character in text
    ).strip("_")
    return normalized or fallback


def _emit_worker_progress_unchecked(
    *,
    stage: str,
    event: str,
    result_class: str = "",
) -> None:
    raw_path = str(os.environ.get("CHEJIN_AI_PROGRESS_PATH") or "").strip()
    if not raw_path:
        return
    path = Path(raw_path)
    if not path.is_absolute() or not path.parent.is_dir():
        return
    payload = {
        "schema_version": 1,
        "progress_id": _safe_label(
            os.environ.get("CHEJIN_AI_PROGRESS_ID"),
            fallback="unbound",
        ),
        "stage": _safe_label(stage, fallback="brain_worker"),
        "route": "local",
        "event": _safe_label(event, fallback="unknown"),
        "occurred_at_unix_ms": int(round(time.time() * 1000)),
    }
    if result_class:
        payload["result_class"] = _safe_label(
            result_class,
            fallback="unknown",
        )
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
            handle.flush()
    except OSError:
        return


def _emit_worker_progress(*, stage: str, event: str, result_class: str = "") -> None:
    try:
        _emit_worker_progress_unchecked(
            stage=stage,
            event=event,
            result_class=result_class,
        )
    except Exception:  # noqa: BLE001 - diagnostics must be behavior-neutral
        return


def _read_utf8_request() -> str:
    """Read the parent envelope independently of the Windows code page."""

    stream = getattr(sys.stdin, "buffer", None)
    if stream is not None:
        return stream.read().decode("utf-8")
    return sys.stdin.read()


def _write_utf8_envelope(payload: dict) -> None:
    """Write the child envelope as UTF-8 even under a non-UTF Windows locale."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    stream = getattr(sys.stdout, "buffer", None)
    if stream is not None:
        stream.write(encoded)
        stream.flush()
        return
    sys.stdout.write(encoded.decode("utf-8"))
    sys.stdout.flush()


def main() -> int:
    try:
        payload = json.loads(_read_utf8_request() or "{}")
        config = payload.get("config")
        invocation = payload.get("invocation")
        if not isinstance(config, dict) or not isinstance(invocation, dict):
            raise ValueError("AI_PROVIDER_WORKER_REQUEST_INVALID")

        _emit_worker_progress(stage="runtime_import", event="started")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            from app.services.ai_adapter import RealOmniAutoAIEngineAdapter

            adapter = RealOmniAutoAIEngineAdapter()
            brain = adapter._load_brain()
            _emit_worker_progress(
                stage="runtime_import",
                event="finished",
                result_class="succeeded",
            )
            _emit_worker_progress(stage="brain_workflow", event="started")
            result = brain(config=config, **invocation)
            _emit_worker_progress(
                stage="brain_workflow",
                event="finished",
                result_class="succeeded",
            )
        if not isinstance(result, dict):
            raise TypeError("AI_ENGINE_CONTRACT_INVALID")
        envelope = {"ok": True, "result": result}
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - child errors are normalized for the parent
        _emit_worker_progress(
            stage="brain_worker",
            event="finished",
            result_class=type(exc).__name__,
        )
        envelope = {
            "ok": False,
            "error_code": getattr(exc, "code", None) or "AI_ENGINE_PROVIDER_FAILED",
            "exception_type": type(exc).__name__,
        }
        exit_code = 1
    _write_utf8_envelope(envelope)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
