"""Load pure rules from the OmniAuto source already shipped with the backend."""

from functools import lru_cache
import importlib.util
from pathlib import Path

from app.core.config import get_settings


@lru_cache(maxsize=8)
def _load_module(path: str):
    spec = importlib.util.spec_from_file_location("chejin_shared_" + Path(path).stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Shared rules are unavailable: {Path(path).name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def shared_adapter(name: str):
    root = Path(get_settings().c3_omniauto_root).expanduser()
    # Preserve the existing source-checkout fallback, never override a custom root.
    if root == Path("/app/omniauto-rpa") and not root.exists():
        root = Path(__file__).resolve().parents[3] / "worker-client" / "omniauto-rpa"
    return _load_module(str((root / "apps/wechat_ai_customer_service/adapters" / f"{name}.py").resolve()))
