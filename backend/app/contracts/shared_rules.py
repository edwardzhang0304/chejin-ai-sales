"""Load pure rules from the OmniAuto source already shipped with the backend."""

from functools import lru_cache
import hashlib
import importlib
import sys
from types import ModuleType
from pathlib import Path

from app.core.config import get_settings


@lru_cache(maxsize=8)
def _load_module(path: str):
    source = Path(path)
    # Give sibling pure modules a real package so their relative imports keep
    # working. Scope by the configured directory; custom roots must not reuse
    # modules from a different source checkout already imported in the process.
    package_name = "chejin_shared_" + hashlib.sha256(str(source.parent).encode()).hexdigest()[:16]
    package = ModuleType(package_name)
    package.__path__ = [str(source.parent)]
    package.__package__ = package_name
    sys.modules.setdefault(package_name, package)
    return importlib.import_module(package_name + "." + source.stem)


def shared_adapter(name: str):
    root = Path(get_settings().c3_omniauto_root).expanduser()
    # Preserve the existing source-checkout fallback, never override a custom root.
    if root == Path("/app/omniauto-rpa") and not root.exists():
        root = Path(__file__).resolve().parents[3] / "worker-client" / "omniauto-rpa"
    return _load_module(str((root / "apps/wechat_ai_customer_service/adapters" / f"{name}.py").resolve()))
