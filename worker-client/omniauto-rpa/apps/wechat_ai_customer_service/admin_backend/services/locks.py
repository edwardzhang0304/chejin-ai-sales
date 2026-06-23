"""Runtime lock inspection."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[4]
RUNTIME_APP_ROOT = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service"


def list_runtime_locks() -> list[dict[str, str]]:
    if not RUNTIME_APP_ROOT.exists():
        return []
    return [
        {
            "path": str(path),
            "name": path.name,
            "updated_at": str(path.stat().st_mtime),
        }
        for path in sorted(RUNTIME_APP_ROOT.rglob("*.lock"))
        if path.is_file()
    ]

