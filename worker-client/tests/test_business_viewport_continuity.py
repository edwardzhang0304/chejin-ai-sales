from __future__ import annotations

import ast
from pathlib import Path

from chejin_worker_client.message_viewport_projection import (
    compare_business_viewport_continuity,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def fact(
    content: str,
    *,
    message_type: str = "text",
    media_state: str = "",
    order: int = 0,
) -> dict[str, object]:
    return {
        "screen_order": order,
        "sender_role": "customer",
        "message_type": message_type,
        "normalized_content_signature": content,
        "media_state": media_state,
    }


def sequence(*items: dict[str, object]) -> list[dict[str, object]]:
    return [{**item, "screen_order": index} for index, item in enumerate(items)]


def test_complete_prefix_with_one_explanation_is_tail_append() -> None:
    old = sequence(fact("text-1"), fact("voice-a", message_type="voice"))
    new = sequence(
        fact("text-1"),
        fact("voice-a", message_type="voice"),
        fact("text-2"),
    )

    result = compare_business_viewport_continuity(old, new)

    assert result["relation"] == "unique_tail_append"
    assert result["new_suffix_indexes"] == [2]


def test_repeated_weak_images_do_not_select_longest_overlap() -> None:
    image = fact("image", message_type="image", media_state="image")
    old = sequence(image, image)
    new = sequence(image, image, image)

    result = compare_business_viewport_continuity(old, new)

    assert result["relation"] == "continuity_context_expansion_required"
    assert result["reason"] == "multiple_weak_overlap_explanations"
    assert [item["overlap_size"] for item in result["overlap_candidates"]] == [1, 2]


def test_same_duration_voices_do_not_select_longest_overlap() -> None:
    voice = fact(
        "duration-3",
        message_type="voice",
        media_state="untranscribed",
    )

    result = compare_business_viewport_continuity(
        sequence(voice, voice),
        sequence(voice, voice, voice),
    )

    assert result["relation"] == "continuity_context_expansion_required"
    assert result["reason"] == "multiple_weak_overlap_explanations"


def test_unique_strong_boundary_proves_normal_viewport_slide() -> None:
    old = sequence(fact("text-1"), fact("text-2"), fact("voice-a", message_type="voice"))
    new = sequence(fact("text-2"), fact("voice-a", message_type="voice"), fact("text-3"))

    result = compare_business_viewport_continuity(
        old,
        new,
        old_boundary_tokens={1: {"fact:text-2"}},
        new_boundary_tokens={0: {"fact:text-2"}},
    )

    assert result["relation"] == "unique_viewport_slide_with_tail_append"
    assert result["matched_pairs"] == [
        {"old_index": 1, "new_index": 0},
        {"old_index": 2, "new_index": 1},
    ]
    assert result["new_suffix_indexes"] == [2]


def test_equal_text_sequence_without_boundary_is_equal() -> None:
    old = sequence(fact("你好"), fact("我在的"))
    new = sequence(fact("你好"), fact("我在的"))

    result = compare_business_viewport_continuity(old, new)

    assert result["relation"] == "business_sequence_equal"
    assert result["reason"] == "same_text_sequence"
    assert result["matched_pairs"] == [
        {"old_index": 0, "new_index": 0},
        {"old_index": 1, "new_index": 1},
    ]


def test_equal_weak_full_viewport_requires_context() -> None:
    image = fact("image", message_type="image", media_state="image")

    first = compare_business_viewport_continuity(
        sequence(image, image),
        sequence(image, image),
    )
    final = compare_business_viewport_continuity(
        sequence(image, image),
        sequence(image, image),
        context_expansion_used=True,
    )

    assert first["relation"] == "continuity_context_expansion_required"
    assert final["relation"] == "business_sequence_not_continuous"


def test_confirmed_empty_viewport_is_equal_but_unproved_empty_is_not() -> None:
    confirmed = compare_business_viewport_continuity(
        [],
        [],
        old_top_boundary_complete=True,
        new_top_boundary_complete=True,
    )
    unproved = compare_business_viewport_continuity([], [])

    assert confirmed["relation"] == "business_sequence_equal"
    assert unproved["relation"] == "continuity_context_expansion_required"


def test_production_has_one_continuity_decider_and_one_voice_merge_owner() -> None:
    """Keep Sidecar/Worker/backend from growing competing decisions."""

    roots = (
        REPOSITORY_ROOT / "worker-client" / "chejin_worker_client",
        REPOSITORY_ROOT
        / "worker-client"
        / "omniauto-rpa"
        / "apps"
        / "wechat_ai_customer_service",
        REPOSITORY_ROOT / "backend" / "app",
    )
    definitions: dict[str, list[Path]] = {
        "compare_business_viewport_continuity": [],
        "_merge_same_frame_voice_hint": [],
    }
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name in definitions:
                        definitions[node.name].append(path)

    assert definitions["compare_business_viewport_continuity"] == [
        REPOSITORY_ROOT
        / "worker-client"
        / "chejin_worker_client"
        / "message_viewport_projection.py"
    ]
    assert definitions["_merge_same_frame_voice_hint"] == [
        REPOSITORY_ROOT
        / "worker-client"
        / "omniauto-rpa"
        / "apps"
        / "wechat_ai_customer_service"
        / "adapters"
        / "wechat_win32_ocr_sidecar.py"
    ]
