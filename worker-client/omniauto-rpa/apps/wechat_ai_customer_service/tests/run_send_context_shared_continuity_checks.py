"""Independent OmniAuto checks for the portable final-send comparator."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


OMNIAUTO_ROOT = Path(__file__).resolve().parents[3]
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

if importlib.util.find_spec("chejin_worker_client") is not None:
    raise AssertionError(
        "isolated check unexpectedly exposes chejin_worker_client"
    )

from apps.wechat_ai_customer_service.adapters.business_viewport_continuity import (  # noqa: E402
    boundary_tokens_for_observations,
)
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import (  # noqa: E402
    build_send_context_guard,
    validate_send_context_guard,
)


def voice_observation(observation_id: str) -> dict[str, object]:
    return {
        "schema_version": 3,
        "observation_id": observation_id,
        "row_kind": "voice_transcript",
        "sender_role": "customer",
        "message_type": "voice",
        "voice_state": "transcribed",
        "voice_duration": "4s",
        "content_clean": "10万左右的二手车有什么推荐吗？",
    }


old_observation = voice_observation("isolated-old-voice")
new_observation = voice_observation("isolated-new-frame-voice")
expected = build_send_context_guard(
    [old_observation],
    layout_evidence={
        "ok": True,
        "layout_snapshot_id": "isolated-old-layout",
        "message_viewport_bounds": [300, 100, 1000, 800],
    },
)
old_tokens = boundary_tokens_for_observations(
    [old_observation],
    committed_only=False,
)
expected["worker_continuity_contract"] = {
    "schema_version": 1,
    "comparator": "compare_business_viewport_continuity",
    "old_boundary_tokens": {
        str(index): sorted(tokens)
        for index, tokens in old_tokens.items()
        if tokens
    },
    "old_top_boundary_complete": False,
}
current = build_send_context_guard(
    [new_observation],
    layout_evidence={
        "ok": True,
        "layout_snapshot_id": "isolated-current-layout",
        "message_viewport_bounds": [300, 100, 1000, 800],
    },
)
result = validate_send_context_guard(
    expected,
    current,
    current_observations=[new_observation],
)
if result.get("ok") is not True:
    raise AssertionError(result)
if result.get("continuity_relation") != "business_sequence_equal":
    raise AssertionError(result)
if "chejin_worker_client" in sys.modules:
    raise AssertionError("Sidecar imported Worker during final guard check")

print("2 checks passed")
