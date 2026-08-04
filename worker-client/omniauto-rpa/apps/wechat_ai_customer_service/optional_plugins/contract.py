from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


CapabilityContext = dict[str, Any]
CapabilityResult = dict[str, Any]


@runtime_checkable
class OptionalCapabilityPlugin(Protocol):
    """Minimal protocol shared by optional input-enhancement plugins."""

    name: str
    capability: str

    def available(self) -> bool: ...

    def should_run(self, context: CapabilityContext) -> CapabilityResult: ...

    def run(self, context: CapabilityContext) -> CapabilityResult: ...
