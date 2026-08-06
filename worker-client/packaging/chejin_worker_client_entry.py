from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import traceback


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chejin_worker_client.main import main


def _write_packaging_diagnostic(exc: BaseException) -> None:
    path_value = os.environ.get("CHEJIN_PACKAGING_DIAGNOSTIC_PATH")
    if not path_value:
        return
    payload = {
        "pid": os.getpid(),
        "argv": list(sys.argv),
        "exception_type": type(exc).__name__,
        "traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
    }
    try:
        with Path(path_value).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        _write_packaging_diagnostic(exc)
        raise
