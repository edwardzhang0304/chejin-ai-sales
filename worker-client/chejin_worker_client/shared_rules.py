"""Access pure rules in the existing bundled OmniAuto adapter boundary."""

import sys
from pathlib import Path

_OMNIAUTO_ROOT = Path(__file__).resolve().parents[1] / "omniauto-rpa"
if str(_OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(_OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters import (  # noqa: E402
    contract_rules,
    message_contract,
)

__all__ = ["contract_rules", "message_contract"]
