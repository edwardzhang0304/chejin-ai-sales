from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        config = payload.get("config")
        invocation = payload.get("invocation")
        if not isinstance(config, dict) or not isinstance(invocation, dict):
            raise ValueError("AI_PROVIDER_WORKER_REQUEST_INVALID")

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            from app.services.ai_adapter import RealOmniAutoAIEngineAdapter

            adapter = RealOmniAutoAIEngineAdapter()
            brain = adapter._load_brain()
            result = brain(config=config, **invocation)
        if not isinstance(result, dict):
            raise TypeError("AI_ENGINE_CONTRACT_INVALID")
        envelope = {"ok": True, "result": result}
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - child errors are normalized for the parent
        envelope = {
            "ok": False,
            "error_code": getattr(exc, "code", None) or "AI_ENGINE_PROVIDER_FAILED",
            "exception_type": type(exc).__name__,
        }
        exit_code = 1
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
