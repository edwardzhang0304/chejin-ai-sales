from __future__ import annotations

import importlib
from typing import Any, Callable


def load_factory(factory_path: str) -> Callable[[], Any]:
    """Load ``package.module:factory`` without importing concrete plugins early."""

    module_name, separator, attribute = str(factory_path or "").partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"invalid optional capability factory path: {factory_path!r}")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute)
    if not callable(factory):
        raise TypeError(f"optional capability factory is not callable: {factory_path!r}")
    return factory
