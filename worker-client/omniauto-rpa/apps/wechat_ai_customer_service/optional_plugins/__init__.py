"""Optional customer-service capability plugins.

The core imports only the neutral contract and registry. Concrete voice and
vision implementations are loaded on first use.
"""

from .contract import OptionalCapabilityPlugin
from .registry import (
    get_optional_capability_status,
    register_optional_capability,
    resolve_optional_capability,
    unregister_optional_capability,
)

__all__ = [
    "OptionalCapabilityPlugin",
    "get_optional_capability_status",
    "register_optional_capability",
    "resolve_optional_capability",
    "unregister_optional_capability",
]
