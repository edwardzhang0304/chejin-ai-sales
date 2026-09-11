"""Load the same pure OCR projection used by Worker/Sidecar; no second rule set."""
from functools import lru_cache
import importlib.util
from pathlib import Path
from typing import Any

from app.core.config import get_settings


@lru_cache(maxsize=4)
def _projection_module(path: str):
    spec = importlib.util.spec_from_file_location("chejin_shared_message_projection", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Shared message viewport projection is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalized_projection_text(value: Any) -> str:
    root = Path(get_settings().c3_omniauto_root).expanduser()
    # Docker packages /app/omniauto-rpa; source checkouts package it here.
    # An explicitly configured different path must not silently fall back.
    if root == Path("/app/omniauto-rpa") and not root.exists():
        root = Path(__file__).resolve().parents[3] / "worker-client" / "omniauto-rpa"
    path = root / "apps/wechat_ai_customer_service/adapters/message_viewport_projection.py"
    return _projection_module(str(path.resolve())).normalized_projection_text(value)
