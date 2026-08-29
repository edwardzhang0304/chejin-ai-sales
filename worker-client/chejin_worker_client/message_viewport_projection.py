"""Worker-facing imports for the portable continuity implementation.

The pure algorithm lives beside the OmniAuto projection so an independent
Sidecar can execute the same verifier without importing this Worker package.
Worker remains the sole owner of continuity decisions and durable identity;
re-exporting the pure functions here preserves existing Worker call sites
without maintaining a second implementation.
"""

from __future__ import annotations

import sys
from pathlib import Path


_OMNIAUTO_ROOT = Path(__file__).resolve().parents[1] / "omniauto-rpa"
if str(_OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(_OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters.business_viewport_continuity import (  # noqa: E402
    BUSINESS_VIEWPORT_CONTINUITY_RESULTS,
    SEND_CONTEXT_BUSINESS_DIGEST_SCHEMA_VERSION,
    boundary_tokens_for_observations,
    compare_business_viewport_continuity,
    normalized_business_message_sequence,
    ordered_message_viewport_observations,
    stable_business_content_signature,
)


__all__ = [
    "BUSINESS_VIEWPORT_CONTINUITY_RESULTS",
    "SEND_CONTEXT_BUSINESS_DIGEST_SCHEMA_VERSION",
    "boundary_tokens_for_observations",
    "compare_business_viewport_continuity",
    "normalized_business_message_sequence",
    "ordered_message_viewport_observations",
    "stable_business_content_signature",
]
