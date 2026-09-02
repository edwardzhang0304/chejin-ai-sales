"""Earliest safe startup evidence for the frozen updater.

This hook runs after the PyInstaller bootloader has extracted the application
but before application imports.  It deliberately records no arguments, paths,
tokens, release metadata, or environment values.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path


def _write_bootstrap_marker() -> None:
    raw_path = str(os.environ.get("CHEJIN_UPDATER_DIAGNOSTIC_PATH") or "").strip()
    if not raw_path:
        return
    try:
        path = Path(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "phase": "runtime_hook_loaded",
                        "pid": os.getpid(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
    except Exception:
        # Diagnostics must never decide whether an update proceeds.
        return


_write_bootstrap_marker()
