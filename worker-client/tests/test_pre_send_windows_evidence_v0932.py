"""v0.9.32 regressions backed by captured Windows WeChat evidence.

The archive is deliberately external to the repository because it contains a
real UAT SQLite database and screenshots.  These tests do not mock a business
success result.  They read the Windows-produced OCR observations from SQLite,
feed those observations into the production normalization/fingerprint
functions, and use the real screenshots at the image boundary.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
import zipfile

from PIL import Image, ImageChops, ImageDraw
try:
    import pytest
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(
        "pytest is required only when external Windows evidence is supplied"
    ) from exc


OMNIAUTO_ROOT = Path(__file__).resolve().parents[1] / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters import (  # noqa: E402
    wechat_win32_ocr_sidecar as sidecar,
)
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import (  # noqa: E402
    window_layout,
)
EVIDENCE_ENV = "CHEJIN_V0932_WINDOWS_EVIDENCE_ZIP"
LAYOUT_SCREENSHOTS_ENV = "CHEJIN_V0932_WINDOWS_LAYOUT_SCREENSHOTS_JSON"
EXPECTED_TRANSCRIPT = "10万块钱的二手车有什么推荐的？"
WINDOWS_LAYOUT = {
    "ok": True,
    "layout_snapshot_id": "windows-uat-920x991",
    "chat_header_bounds": [374, 0, 920, 114],
    "message_viewport_bounds": [374, 114, 920, 835],
    "input_bounds": [384, 835, 820, 940],
    "toolbar_bounds": [374, 940, 920, 991],
}


@pytest.fixture(scope="module")
def random_size_windows_screenshots() -> list[Path]:
    raw = os.environ.get(LAYOUT_SCREENSHOTS_ENV, "").strip()
    if not raw:
        pytest.skip(
            f"set {LAYOUT_SCREENSHOTS_ENV} to a JSON array of real Windows screenshots"
        )
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        pytest.fail(f"{LAYOUT_SCREENSHOTS_ENV} is invalid JSON: {exc}")
    assert isinstance(values, list)
    paths = [Path(str(value)).expanduser() for value in values]
    assert len(paths) >= 5, "the random-size evidence matrix requires at least five screenshots"
    assert all(path.is_file() for path in paths)
    return paths


def _walk_json(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


@pytest.fixture(scope="module")
def windows_evidence(tmp_path_factory: pytest.TempPathFactory) -> dict:
    archive_path = Path(os.environ.get(EVIDENCE_ENV, "")).expanduser()
    if not archive_path.is_file():
        pytest.skip(f"set {EVIDENCE_ENV} to the real Windows evidence ZIP")
    root = tmp_path_factory.mktemp("v0932-windows-evidence")
    with zipfile.ZipFile(archive_path) as archive:
        # The collector ran on Windows and wrote backslash-separated member
        # names. Normalize them explicitly so this evidence test behaves the
        # same on macOS/Linux without changing the source archive.
        for member in archive.infolist():
            relative_parts = [
                part
                for part in member.filename.replace("\\", "/").split("/")
                if part not in {"", ".", ".."}
            ]
            target = root.joinpath(*relative_parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
    database = next(root.rglob("worker_client.sqlite3"), None)
    assert database is not None, "Windows evidence is missing worker_client.sqlite3"

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            """
            SELECT payload_json
            FROM c2_ingest_outbox
            WHERE json_extract(payload_json, '$.messages[0].message_type') = 'voice'
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
        assert row is not None, "Windows evidence contains no voice Outbox"
        outbox = json.loads(row[0])
        message = outbox["messages"][0]
        assert message["content"] == EXPECTED_TRANSCRIPT
        transcription = message["raw_payload"]["voice_transcription_meta"]
        selected_id = transcription["selected_pre_observation_id"]
        metadata_rows = connection.execute(
            """
            SELECT metadata
            FROM local_logs
            WHERE instr(metadata, ?) > 0
            ORDER BY created_at DESC
            """,
            (selected_id,),
        ).fetchall()

    snapshots: list[dict] = []
    selected_observation: dict | None = None
    for (raw_metadata,) in metadata_rows:
        try:
            metadata = json.loads(raw_metadata)
        except (TypeError, json.JSONDecodeError):
            continue
        for item in _walk_json(metadata):
            if not isinstance(item, dict):
                continue
            if (
                item.get("observation_id") == selected_id
                and item.get("row_kind") == "voice_bubble"
            ):
                selected_observation = copy.deepcopy(item)
            observations = item.get("observations")
            if (
                isinstance(observations, list)
                and any(
                    isinstance(observation, dict)
                    and observation.get("observation_id") == selected_id
                    for observation in observations
                )
            ):
                snapshots.append(copy.deepcopy(item))
    assert selected_observation is not None
    assert snapshots
    snapshot = max(snapshots, key=lambda item: len(item.get("observations") or []))

    candidates = sorted(
        list(root.rglob("voice_action_prepare_*.png"))
        + list(root.rglob("voice_action_execute_before_*.png"))
    )
    unique: dict[str, Path] = {}
    for path in candidates:
        unique.setdefault(hashlib.sha256(path.read_bytes()).hexdigest(), path)
    assert len(unique) >= 2, "Windows evidence lacks the caret on/off pair"
    caret_pair: tuple[Path, Path] | None = None
    for first_path in unique.values():
        for second_path in unique.values():
            if first_path == second_path:
                continue
            first = Image.open(first_path).convert("RGB")
            second = Image.open(second_path).convert("RGB")
            if first.size != (920, 991) or second.size != first.size:
                continue
            difference = ImageChops.difference(first, second)
            bounds = difference.getbbox()
            if bounds and bounds[1] >= WINDOWS_LAYOUT["input_bounds"][1]:
                caret_pair = (first_path, second_path)
                break
        if caret_pair is not None:
            break
    assert caret_pair is not None, "real screenshot caret delta was not found"

    return {
        "root": root,
        "snapshot": snapshot,
        "selected_observation": selected_observation,
        "transcription": transcription,
        "caret_pair": caret_pair,
    }


def _viewport_digest(observations: list[dict], image: Image.Image) -> dict:
    return sidecar.build_message_viewport_change_evidence(
        observations,
        screenshot=image,
        layout_evidence=WINDOWS_LAYOUT,
    )


def test_real_windows_caret_pair_changes_pixels_only_in_input(
    windows_evidence: dict,
) -> None:
    first_path, second_path = windows_evidence["caret_pair"]
    first = Image.open(first_path).convert("RGB")
    second = Image.open(second_path).convert("RGB")
    difference = ImageChops.difference(first, second)
    bounds = difference.getbbox()

    assert bounds is not None
    input_left, input_top, input_right, input_bottom = WINDOWS_LAYOUT["input_bounds"]
    assert input_left <= bounds[0] < bounds[2] <= input_right
    assert input_top <= bounds[1] < bounds[3] <= input_bottom
    # The captured incident is the actual two-pixel-wide blinking caret.
    assert bounds[2] - bounds[0] <= 2
    assert bounds[3] - bounds[1] <= 24


def test_real_windows_caret_does_not_change_viewport_or_voice_target(
    windows_evidence: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_path, second_path = windows_evidence["caret_pair"]
    first = Image.open(first_path).convert("RGB")
    second = Image.open(second_path).convert("RGB")
    observations = windows_evidence["snapshot"]["observations"]
    selected = copy.deepcopy(windows_evidence["selected_observation"])
    source_message = selected.get("source_message") or {}
    selected["action_target"] = {
        "anchor_stable_key": source_message.get("voice_anchor_stable_key") or "",
        "anchor_structural_key": selected.get("voice_anchor_key") or "",
        "avatar_alignment": source_message.get("avatar_alignment") or {},
    }
    selected["screen_order"] = 2

    first_digest = _viewport_digest(observations, first)
    second_digest = _viewport_digest(observations, second)
    monkeypatch.setattr(
        sidecar,
        "basic_chat_layout_evidence",
        lambda _image: WINDOWS_LAYOUT,
    )
    first_target = sidecar._voice_observation_fingerprint(first, selected)
    second_target = sidecar._voice_observation_fingerprint(second, selected)

    assert first_digest["raw_rgb_hash_used"] is False
    assert first_digest["message_viewport_change_digest"] == second_digest[
        "message_viewport_change_digest"
    ]
    assert first_target == second_target


@pytest.mark.parametrize(
    ("name", "bounds"),
    [
        ("input_caret", (399, 849, 401, 872)),
        ("input_selection", (430, 860, 610, 884)),
        ("toolbar_hover", (500, 945, 548, 982)),
        ("sidebar_badge", (120, 180, 150, 210)),
        ("scrollbar", (362, 104, 372, 225)),
        ("voice_playback_animation", (477, 402, 525, 428)),
        ("message_hover_border", (460, 390, 580, 442)),
        ("gif_animation_frame", (650, 500, 730, 580)),
    ],
)
def test_real_windows_dynamic_pixel_classes_do_not_enter_digest(
    windows_evidence: dict,
    name: str,
    bounds: tuple[int, int, int, int],
) -> None:
    del name
    base_path = windows_evidence["caret_pair"][0]
    base = Image.open(base_path).convert("RGB")
    changed = base.copy()
    ImageDraw.Draw(changed).rectangle(bounds, fill=(7, 129, 243))
    observations = windows_evidence["snapshot"]["observations"]

    baseline = _viewport_digest(observations, base)
    current = _viewport_digest(observations, changed)

    assert ImageChops.difference(base, changed).getbbox() is not None
    assert baseline["message_viewport_change_digest"] == current[
        "message_viewport_change_digest"
    ]
    assert baseline["raw_rgb_hash_used"] is False


def test_transient_voice_and_image_states_are_not_message_facts(
    windows_evidence: dict,
) -> None:
    image = Image.open(windows_evidence["caret_pair"][0]).convert("RGB")
    observations = copy.deepcopy(windows_evidence["snapshot"]["observations"])
    transient = copy.deepcopy(observations)
    for observation in transient:
        if observation.get("row_kind") == "voice_bubble":
            observation["voice_state"] = "playing_73_percent"
            observation["playback_progress"] = 0.73
            observation["selected"] = True
    baseline = _viewport_digest(observations, image)
    changed_voice_state = _viewport_digest(transient, image)

    image_row = {
        "observation_id": "image-real-slot",
        "row_kind": "image_bubble",
        "sender_role": "customer",
        "message_type": "image",
        "bubble_rect": [480, 470, 640, 590],
        "raw_pixel_sha256": "a" * 64,
        "item_state": "gif_frame_1",
    }
    next_frame = {**image_row, "raw_pixel_sha256": "b" * 64, "item_state": "gif_frame_19"}
    first_image_state = _viewport_digest(observations + [image_row], image)
    next_image_state = _viewport_digest(observations + [next_frame], image)

    assert baseline["message_viewport_change_digest"] == changed_voice_state[
        "message_viewport_change_digest"
    ]
    assert first_image_state["message_viewport_change_digest"] == next_image_state[
        "message_viewport_change_digest"
    ]


@pytest.mark.parametrize(
    "new_fact",
    [
        {
            "observation_id": "new-text",
            "row_kind": "text_bubble",
            "sender_role": "customer",
            "message_type": "text",
            "content_clean": "再补充一条文字",
            "bubble_rect": [480, 470, 690, 515],
        },
        {
            "observation_id": "new-same-duration-voice",
            "row_kind": "voice_bubble",
            "sender_role": "customer",
            "message_type": "voice",
            "voice_duration": 4,
            "voice_duration_text": '4"',
            "bubble_rect": [480, 470, 530, 495],
        },
        {
            "observation_id": "new-similar-image",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "message_type": "image",
            "bubble_rect": [480, 470, 640, 590],
        },
    ],
)
def test_real_windows_sequence_detects_each_new_fact(
    windows_evidence: dict,
    new_fact: dict,
) -> None:
    image = Image.open(windows_evidence["caret_pair"][0]).convert("RGB")
    observations = windows_evidence["snapshot"]["observations"]
    before = _viewport_digest(observations, image)
    after = _viewport_digest(observations + [new_fact], image)

    assert after["message_count"] == before["message_count"] + 1
    assert after["message_viewport_change_digest"] != before[
        "message_viewport_change_digest"
    ]


def test_layout_invalid_never_falls_back_to_full_window_pixels(
    windows_evidence: dict,
) -> None:
    image = Image.open(windows_evidence["caret_pair"][0]).convert("RGB")
    result = sidecar.build_message_viewport_change_evidence(
        windows_evidence["snapshot"]["observations"],
        screenshot=image,
        layout_evidence={"ok": False, "reason": "message_viewport_missing"},
    )

    assert result["message_viewport_change_digest"] == ""
    assert result["sequence"] == []
    assert result.get("raw_rgb_hash_used") is not True


def test_five_random_size_windows_screenshots_build_structural_regions(
    random_size_windows_screenshots: list[Path],
) -> None:
    """Exercise the production pixel layout builder on real Windows frames.

    OCR is deliberately not replaced with a fabricated success object here.
    The structural builder obtains all outer-shell boundaries from the actual
    screenshot pixels; OCR anchors are optional for this path.
    """

    supported_count = 0
    explicitly_unsupported_count = 0
    for index, path in enumerate(random_size_windows_screenshots):
        image = Image.open(path).convert("RGB")
        layout = window_layout.build_structural_layout_regions(image)
        validation = window_layout.validate_layout_regions(
            layout.get("regions"),
            image_size=image.size,
        )

        assert layout["ok"] is True, path.name
        assert validation["ok"] is True, path.name
        assert not layout.get("conflicts"), path.name
        regions = layout["regions"]
        assert regions["chat_header_bounds"][2] == image.width
        assert regions["message_viewport_bounds"][2] == image.width
        assert regions["message_viewport_bounds"][3] <= regions["input_bounds"][1]
        assert regions["input_bounds"][3] <= regions["toolbar_bounds"][3]

        calibration = window_layout.build_startup_layout_calibration(
            hwnd=1000 + index,
            process_id=2000 + index,
            image=image,
            ocr_items=[],
            window_rect=[0, 0, image.width, image.height],
            client_rect={
                "x": 0,
                "y": 0,
                "width": image.width,
                "height": image.height,
            },
            client_screen_origin=[0, 0],
            dpi_scale=1.0,
            capture_mode=window_layout.CAPTURE_MODE_CLIENT_AREA,
        )
        if image.width >= 700 and image.height >= 720:
            supported_count += 1
            assert calibration["executable"] is True, path.name
        else:
            explicitly_unsupported_count += 1
            assert calibration["executable"] is False, path.name
            assert calibration["error_code"] == (
                window_layout.ERROR_STARTUP_CALIBRATION_FAILED
            )
            assert "client_surface_below_700x720" in calibration["conflicts"]

    assert supported_count >= 4
    # The supplied matrix intentionally includes one extremely short window.
    # Its regions remain diagnosable, but startup calibration must reject it.
    assert explicitly_unsupported_count >= 1


def test_real_window_shell_layout_ignores_local_dynamic_paint(
    random_size_windows_screenshots: list[Path],
) -> None:
    """Local caret/hover/badge/playback paint cannot move shell regions."""

    for path in random_size_windows_screenshots:
        image = Image.open(path).convert("RGB")
        baseline = window_layout.build_structural_layout_regions(image)
        assert baseline["ok"] is True, path.name
        regions = baseline["regions"]
        changed = image.copy()
        draw = ImageDraw.Draw(changed)

        input_left, input_top, input_right, input_bottom = regions["input_bounds"]
        toolbar_left, toolbar_top, _, toolbar_bottom = regions["toolbar_bounds"]
        sidebar_left, sidebar_top, sidebar_right, sidebar_bottom = regions[
            "sidebar_bounds"
        ]
        view_left, view_top, view_right, view_bottom = regions[
            "message_viewport_bounds"
        ]
        # Representative real-UI dynamic paint, scaled from detected regions:
        # input caret, toolbar hover, sidebar badge, playback/GIF paint and a
        # short scrollbar segment. None is allowed to become shell geometry.
        caret_x = input_left + max(8, (input_right - input_left) // 4)
        draw.rectangle(
            (caret_x, input_top + 8, caret_x + 1, min(input_bottom - 5, input_top + 31)),
            fill=(15, 111, 229),
        )
        draw.rectangle(
            (
                toolbar_left + 20,
                toolbar_top + 8,
                toolbar_left + 58,
                min(toolbar_bottom - 4, toolbar_top + 40),
            ),
            outline=(15, 111, 229),
            width=2,
        )
        badge_top = min(
            sidebar_bottom - 28,
            sidebar_top + max(155, (sidebar_bottom - sidebar_top) // 5),
        )
        draw.ellipse(
            (
                min(sidebar_right - 24, sidebar_left + 50),
                badge_top,
                min(sidebar_right - 8, sidebar_left + 68),
                badge_top + 18,
            ),
            fill=(255, 52, 52),
        )
        dynamic_left = view_left + max(20, (view_right - view_left) // 3)
        dynamic_top = view_top + max(20, (view_bottom - view_top) // 3)
        draw.rectangle(
            (
                dynamic_left,
                dynamic_top,
                min(view_right - 20, dynamic_left + 46),
                min(view_bottom - 20, dynamic_top + 30),
            ),
            fill=(7, 129, 243),
        )
        draw.rectangle(
            (
                max(view_left + 2, view_right - 9),
                view_top + 30,
                max(view_left + 4, view_right - 7),
                min(view_bottom - 30, view_top + 95),
            ),
            fill=(175, 175, 175),
        )

        current = window_layout.build_structural_layout_regions(changed)
        assert current["ok"] is True, path.name
        assert current["regions"] == regions, path.name
