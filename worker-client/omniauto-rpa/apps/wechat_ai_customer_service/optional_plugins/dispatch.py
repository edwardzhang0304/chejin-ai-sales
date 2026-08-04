"""Neutral invocation seam for optional capabilities.

This module deliberately knows nothing about any concrete optional domain.  A
core caller supplies a capability name, an operation name, and the existing
compatibility context.  Concrete implementations remain lazy and replaceable.
"""

from __future__ import annotations

import copy
from typing import Any

from .registry import resolve_optional_capability


def invoke_optional_capability(
    capability: str,
    operation: str,
    context: dict[str, Any] | None = None,
    *,
    default: Any = None,
) -> Any:
    """Invoke one optional operation without importing its implementation.

    ``should_run`` and ``run`` are the frozen protocol operations.  Additional
    implementation-owned operations are reached through an optional ``invoke``
    method so third-party plugins can remain minimal or opt in selectively.
    Missing plugins/operations return a fresh copy of ``default``.  Exceptions
    from an available implementation are intentionally preserved for the
    existing caller-level failure handling.
    """

    plugin = resolve_optional_capability(capability)
    if plugin is None:
        return copy.deepcopy(default)
    payload = dict(context or {})
    clean_operation = str(operation or "").strip()
    if clean_operation == "should_run":
        return plugin.should_run(payload)
    if clean_operation == "run":
        return plugin.run(payload)
    direct = getattr(plugin, clean_operation, None)
    if callable(direct):
        return direct(payload)
    dispatcher = getattr(plugin, "invoke", None)
    if not callable(dispatcher):
        return copy.deepcopy(default)
    result = dispatcher(clean_operation, payload)
    if result is NotImplemented:
        return copy.deepcopy(default)
    return result
