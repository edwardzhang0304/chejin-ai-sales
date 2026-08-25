"""Worker access to the Sidecar-owned pure viewport projection."""

from __future__ import annotations

import sys
from pathlib import Path


_OMNIAUTO_ROOT = Path(__file__).resolve().parents[1] / "omniauto-rpa"
if str(_OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(_OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters.message_viewport_projection import (  # noqa: E402
    normalized_message_viewport_sequence,
)

__all__ = ["normalized_message_viewport_sequence"]
