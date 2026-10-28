"""Access pure rules in the existing bundled OmniAuto adapter boundary."""

import importlib
import sys
from pathlib import Path

_OMNIAUTO_ROOT = Path(__file__).resolve().parents[1] / "omniauto-rpa"
if str(_OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(_OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters import (  # noqa: E402
    contract_rules,
    message_contract,
    read_settlement,
    send_interruption,
)

__all__ = ["contract_rules", "historical_text_correction", "historical_text_alignment", "message_contract", "read_settlement", "send_interruption", "text_correspondence", "hook_contract_rules", "hook_contact_rules", "hook_history_rules", "hook_event_rules", "hook_observability_rules", "client_release_rules", "hook_window_layout", "hook_time_rules"]


def __getattr__(name):
    # The updater only needs the existing contract rules. Loading new OCR
    # comparison helpers there would couple recovery to the whole RPA stack.
    if name not in {"historical_text_correction", "historical_text_alignment", "text_correspondence", "hook_contract_rules", "hook_contact_rules", "hook_history_rules", "hook_event_rules", "hook_observability_rules", "client_release_rules", "hook_window_layout", "hook_time_rules"}:
        raise AttributeError(name)
    module = importlib.import_module(f"apps.wechat_ai_customer_service.adapters.{name}")
    globals()[name] = module
    return module
