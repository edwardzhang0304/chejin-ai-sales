"""v0.9.64 raw-frame regression. Synthetic/DPI cases are not Windows UAT."""
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch
from contextlib import ExitStack

import pytest
from PIL import Image, ImageDraw, ImageEnhance

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "omniauto-rpa"))
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as s
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import frame_avatars as a

FIXTURES = Path(__file__).parent / "fixtures" / "avatars_20260904"
TEXT = "您好，10万左右的二手车可以帮您留意。您更偏轿车还是SUV？平时主要市区代步还是常跑长途？我按您的需求从在售车源里筛两台合适的发给您参考。"
HASHES = {
    "send_baseline_1788518283890.png": "27c7ab9a37feb9ea8f28d34351136dc07067ef3853a0bdc0447d9cb2ab4bcc1f",
    "send_post_guard_and_result_confirm_1_1788518325726.png": "6a6578d6d718ee72a526f8501ce2a212759f608c627660a3f0eb21e0c823d042",
    "send_result_confirm_2_1788518344534.png": "86de77c6023fe9b11514ca37a3b3cf9576ce70b3e7bdc64f9271a7c81bcb9199",
    "send_result_confirm_3_1788518359097.png": "86de77c6023fe9b11514ca37a3b3cf9576ce70b3e7bdc64f9271a7c81bcb9199",
    "send_result_confirm_4_1788518374902.png": "9d98a1b8ffeb5408b05615aa36ea631519ea6d2d798b431d7f18516eb48fb0ed",
}


@pytest.fixture(scope="module")
def incident_frames():
    """Four original post frames, three distinct pixel/file contents; real OCR."""
    layout = s.win32_ocr_layout
    geometry = dict(left=15, top=15, right=953, bottom=1015, width=938, height=1000)
    client = dict(left=0, top=0, right=920, bottom=991)
    original = Image.open(FIXTURES / next(iter(HASHES))).convert("RGB")
    calibration = layout.build_startup_layout_calibration(
        hwnd=1, process_id=1, image=original,
        ocr_items=s.run_ocr(ImageEnhance.Contrast(original).enhance(1.35)),
        window_rect=geometry, client_rect=client, client_screen_origin=[24, 15],
        dpi_scale=1, capture_mode=layout.CAPTURE_MODE_CLIENT_AREA,
    )
    assert calibration["executable"]
    results = []
    for filename, digest in HASHES.items():
        path = FIXTURES / filename
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        image = Image.open(path).convert("RGB")
        snapshot = layout.build_layout_snapshot(
            hwnd=1, frame_id=filename, capture_mode=layout.CAPTURE_MODE_CLIENT_AREA,
            image_size=image.size, capture_screen_origin=[24, 15], window_rect=geometry,
            client_rect=client, client_screen_origin=[24, 15], dpi_scale=1,
            regions={k: calibration[k] for k in layout.REQUIRED_LAYOUT_REGION_NAMES},
            anchors=calibration["anchors"], confidence=calibration["confidence"],
            executable=True, screenshot_path=str(path),
        )
        s._LAYOUT_SNAPSHOT_STORE.put(snapshot)
        s._LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID[id(image)] = snapshot["layout_snapshot_id"]
        with patch.object(s, "get_window_geometry", return_value=geometry), patch.object(s, "window_dpi_scale", return_value=1):
            result = s.build_send_fact_snapshot_from_frame(
                1, target="CJ8R8A35", text=TEXT, exact=True, artifact_dir=None,
                label=filename, screenshot=image, screenshot_path=str(path),
                recover_expected_self_text=bool(results),
                receipt_baseline_message_sequence=results[0][2]["message_sequence"] if results else None,
                receipt_text=TEXT,
            )
        results.append((image, snapshot, result))
    return results


@pytest.mark.parametrize("index", [1, 2, 3, 4])
def test_original_frame_first_confirmation_succeeds(incident_frames, index):
    image, layout, result = incident_frames[index]
    before = incident_frames[0][2]
    replies = [m for m in result["message_sequence"] if m["sender_role"] == "self" and m["bubble_rect"]["top"] > 490]
    assert len(replies) == 1
    assert replies[0]["content"].replace("\n", "") == TEXT
    table = a.avatar_table(image, layout)
    assert len(table["components"]) == 4 and not table["unresolved"]
    assert a.role_details(image, layout, [476, 589, 680, 612])["state"] == "absent"
    with patch.object(s, "capture_send_fact_snapshot", side_effect=AssertionError("first S2 is sufficient")), patch.object(s, "safe_send_trigger", side_effect=AssertionError("confirmation cannot resend")):
        ack = s.confirm_reply_sent(1, target="CJ8R8A35", text=TEXT, exact=True,
            baseline_match_count=0, baseline_message_sequence=before["message_sequence"], initial_snapshot=result)
    assert ack["ok"] and ack["attempt"] == 1
    assert ack["confirmed_message"]["content"].replace("\n", "") == TEXT
    # Same original image table is reused even if OCR is performed again.
    assert a.avatar_table(image, layout) is table


def test_same_image_full_ocr_uses_same_avatar_table(incident_frames):
    image, layout, _ = incident_frames[1]
    table = a.avatar_table(image, layout)
    with patch.object(a, "_detect", side_effect=AssertionError("must reuse original frame table")):
        rows = s.run_ocr(image)
        messages = s.parse_messages_from_ocr(rows, image.size, target="CJ8R8A35", screenshot=image, layout_snapshot=layout)
    assert any(m["content"].replace("\n", "") == TEXT for m in messages)
    assert a.avatar_table(image, layout) is table


def synthetic_frame(*, dpi=1.0, width=1000):
    image = Image.new("RGB", (round(width*dpi), round(900*dpi)), "white")
    layout = {"valid": True, "layout_snapshot_id": "synthetic", "dpi_scale": dpi,
              "message_viewport_bounds": [round(374*dpi), round(100*dpi), round(width*dpi), round(800*dpi)],
              "input_bounds": [round(374*dpi), round(800*dpi), round(width*dpi), round(890*dpi)]}
    return image, layout


def draw_avatar(image, x, y, dpi=1.0):
    """Synthetic independent outline enclosing disconnected coloured islands."""
    draw = ImageDraw.Draw(image)
    box = lambda r: tuple(round(v*dpi) for v in r)
    draw.rounded_rectangle(box((x, y, x+42, y+42)), radius=round(4*dpi), fill="white", outline="#555555", width=max(1, round(dpi)))
    draw.rectangle(box((x+7, y+7, x+16, y+16)), fill="blue")
    draw.rectangle(box((x+26, y+26, x+35, y+35)), fill="red")


def row(text, y, left=470, right=810):
    return {"text": text, "top": y, "bottom": y+22, "left": left, "right": right,
            "center_y": y+11, "center_x": (left+right)/2, "confidence": 1}


@pytest.mark.parametrize("count", [3, 4, 5])
@pytest.mark.parametrize("role", ["customer", "self"])
def test_real_detector_synthetic_multiline_and_short_tail(count, role):
    image, layout = synthetic_frame()
    draw_avatar(image, 400 if role == "customer" else 930, 200)
    rows = [row("完整原文" + str(i), 209+i*24, right=540 if i == count-1 else 810) for i in range(count)]
    messages = s.parse_messages_from_ocr(rows, image.size, target="CJTEST01", screenshot=image, layout_snapshot=layout)
    assert len(messages) == 1 and messages[0]["sender_role"] == role
    assert messages[0]["content"] == "\n".join(r["text"] for r in rows)


def test_same_avatar_artwork_different_messages_is_not_one_component():
    image, layout = synthetic_frame()
    draw_avatar(image, 930, 200)
    draw_avatar(image, 930, 260)
    rows = [row("第一条", 209), row("第二条", 269)]
    messages = s.parse_messages_from_ocr(rows, image.size, target="CJTEST01", screenshot=image, layout_snapshot=layout)
    assert [m["content"] for m in messages] == ["第一条", "第二条"]
    ids = [m["avatar_alignment"]["avatar_component_id"] for m in messages]
    assert len(set(ids)) == 2


@pytest.mark.parametrize("role", ["customer", "self"])
@pytest.mark.parametrize("transcript", ["好的，我明天下午过来看车", "语音转写的内容请看这里"])
def test_voice_and_transcript_share_one_real_component(role, transcript):
    image, layout = synthetic_frame()
    draw_avatar(image, 400 if role == "customer" else 930, 200)
    duration = row('6"', 209, left=470 if role == "customer" else 760, right=530 if role == "customer" else 810)
    rows = [duration, row(transcript, 242), row("谢谢", 266, right=570)]
    # Real detection must see the SAME component at the duration and text row.
    evidence = [a.role_details(image, layout, [r[k] for k in ("left", "top", "right", "bottom")]) for r in rows[:2]]
    assert evidence[0]["role"] == evidence[1]["role"] == role
    assert evidence[0]["avatar_component_id"] == evidence[1]["avatar_component_id"]
    messages = s.parse_messages_from_ocr(rows, image.size, target="CJTEST01", screenshot=image, layout_snapshot=layout)
    assert len(messages) == 1
    assert messages[0]["type"] == "voice" and messages[0]["sender_role"] == role
    assert messages[0]["content"].replace("\n", "") == transcript + "谢谢"
    assert messages[0]["content_raw_ocr"] == '\n'.join(r["text"] for r in rows)


def test_voice_followed_by_a_different_avatar_is_not_its_transcript():
    image, layout = synthetic_frame()
    draw_avatar(image, 400, 200)
    draw_avatar(image, 400, 260)
    rows = [row('6"', 209, right=530), row("另一条独立文字", 269)]
    messages = s.parse_messages_from_ocr(rows, image.size, target="CJTEST01", screenshot=image, layout_snapshot=layout)
    assert [m["type"] for m in messages] == ["voice", "text"]
    assert len({m["avatar_alignment"]["avatar_component_id"] for m in messages}) == 2


@pytest.mark.parametrize("role", ["customer", "self"])
def test_later_voice_transcript_line_can_reobserve_the_parent_component(role):
    image, layout = synthetic_frame()
    draw_avatar(image, 400 if role == "customer" else 930, 200)
    rows = [row('6"', 209, left=470 if role == "customer" else 760,
                right=530 if role == "customer" else 810),
            row("好的", 231), row("明天下午过来看车", 249, right=650)]
    # Tight but legal OCR line overlap exercises the already-started voice
    # transcript branch, not just duration -> first transcript line.
    evidence = [a.role_details(image, layout, [r[k] for k in ("left", "top", "right", "bottom")]) for r in rows]
    assert {e["role"] for e in evidence} == {role}
    assert len({e["avatar_component_id"] for e in evidence}) == 1
    messages = s.parse_messages_from_ocr(rows, image.size, target="CJTEST01", screenshot=image, layout_snapshot=layout)
    assert len(messages) == 1 and messages[0]["type"] == "voice"
    assert messages[0]["sender_role"] == role
    assert messages[0]["content"].replace("\n", "") == "好的明天下午过来看车"
    assert messages[0]["content_raw_ocr"] == '\n'.join(r["text"] for r in rows)


@pytest.mark.parametrize("bottom, reason", [(340, "oversized_avatar_candidate"), (312, "avatar_shape_unresolved")])
def test_oversized_attached_avatar_is_not_absence_and_cannot_merge_text(bottom, reason):
    # Artificial boundary, NOT an incident screenshot: an independent second
    # avatar touches a vertical graphic while staying inside the avatar column.
    image, layout = synthetic_frame()
    draw_avatar(image, 400, 200)
    draw_avatar(image, 400, 250)
    ImageDraw.Draw(image).rectangle((418, 289, 430, bottom), fill="#555555")
    rows = [row("第一条文字", 225), row("第二条独立文字", 249)]
    with pytest.raises(a.AvatarEvidenceError, match="avatar_association_unresolved"):
        s.parse_messages_from_ocr(rows, image.size, target="CJTEST01", screenshot=image, layout_snapshot=layout)
    table = a.avatar_table(image, layout)
    assert any(c["reason"] == reason for c in table["unresolved"])


def test_proven_viewport_boundary_does_not_block_text_continuation():
    image, layout = synthetic_frame()
    draw_avatar(image, 930, 200)
    ImageDraw.Draw(image).rectangle((995, 100, 999, 799), fill="#dddddd")
    rows = [row("完整第一行", 209), row("短末行", 233, right=570)]
    messages = s.parse_messages_from_ocr(rows, image.size, target="CJTEST01", screenshot=image, layout_snapshot=layout)
    assert len(messages) == 1 and messages[0]["content"] == "完整第一行\n短末行"
    table = a.avatar_table(image, layout)
    assert not table["unresolved"]
    assert any(c["reason"] == "viewport_vertical_boundary" for c in table["excluded"])


@pytest.mark.parametrize("role", ["customer", "self"])
@pytest.mark.parametrize("variant", ["independent", "horizontal_attachment", "before_bottom", "at_bottom"])
def test_pending_avatar_overlapping_row_is_never_absence(role, variant):
    # Architect's synthetic boundary: extending the same attachment by one
    # pixel must not turn unresolved evidence into permission to merge text.
    image, layout = synthetic_frame()
    x = 400 if role == "customer" else 930
    draw_avatar(image, x, 200)
    draw_avatar(image, x, 260)
    draw = ImageDraw.Draw(image)
    if variant == "horizontal_attachment":
        draw.rectangle((x-26, 276, x+11, 288), fill="#555555")
    elif variant in {"before_bottom", "at_bottom"}:
        draw.rectangle((x+18, 288, x+25, 798 if variant == "before_bottom" else 799), fill="#555555")
    rows = [row("第一条第一行", 209), row("第一条第二行", 233), row("第二条独立消息", 269)]
    def parse():
        return s.parse_messages_from_ocr(rows, image.size, target="CJTEST01", screenshot=image, layout_snapshot=layout)
    if variant == "independent":
        assert [m["content"] for m in parse()] == ["第一条第一行\n第一条第二行", "第二条独立消息"]
        return
    table = a.avatar_table(image, layout)
    assert table["unresolved"]
    if variant == "at_bottom":
        assert any(c["reason"] == "candidate_clipped" for c in table["unresolved"])
    with pytest.raises(a.AvatarEvidenceError, match="avatar_association_unresolved"):
        parse()


def test_pending_avatar_outside_text_rows_does_not_block_continuation():
    image, layout = synthetic_frame()
    draw_avatar(image, 930, 200)
    draw_avatar(image, 930, 400)
    ImageDraw.Draw(image).rectangle((948, 428, 955, 799), fill="#555555")
    table = a.avatar_table(image, layout)
    assert any(c["reason"] == "candidate_clipped" for c in table["unresolved"])
    messages = s.parse_messages_from_ocr([row("第一行", 209), row("第二行", 233)],
        image.size, target="CJTEST01", screenshot=image, layout_snapshot=layout)
    assert [m["content"] for m in messages] == ["第一行\n第二行"]


@pytest.mark.parametrize("role", ["customer", "self"])
def test_image_observer_preserves_clipped_avatar_failure(role):
    image, layout = synthetic_frame()
    x = 400 if role == "customer" else 930
    draw_avatar(image, x, 260)
    draw = ImageDraw.Draw(image)
    draw.rectangle((x+18, 288, x+25, 799), fill="#555555")
    left, right = (480, 680) if role == "customer" else (680, 900)
    for y in range(260, 440, 8):
        for column in range(left, right, 8):
            tone = 35 if ((column-left+y-260)//8) % 2 else 220
            draw.rectangle((column, y, min(column+7, right-1), y+7), fill=(tone,150,80))
    with pytest.raises(RuntimeError, match="C2_IMAGE_OBSERVATION_FAILED:same_row_avatar_role"):
        s.merge_structural_image_messages(image, [], [], target="CJTEST01", layout_snapshot=layout)
    errors = []
    s.merge_structural_image_messages(image, [], [], target="CJTEST01", layout_snapshot=layout,
        observation_validation_errors=errors)
    assert errors and errors[0]["stage"] == "same_row_avatar_role"


@pytest.mark.parametrize("dpi", [1, 1.25, 1.5])
@pytest.mark.parametrize("width", [920, 1000, 1180])
def test_synthetic_valid_layout_sizes_dpi_and_white_avatar_interiors(dpi, width):
    image, layout = synthetic_frame(dpi=dpi, width=width)
    draw_avatar(image, 400, 200, dpi)
    draw_avatar(image, width-70, 260, dpi)
    table = a.avatar_table(image, layout)
    assert table["state"] == "complete" and not table["unresolved"]
    assert [c["role"] for c in table["components"]] == ["customer", "self"]
    # Identical pixels in a new frame must acquire separate frame references.
    assert a.avatar_table(image.copy(), {**layout, "layout_snapshot_id": "next"})["frame_reference"] != table["frame_reference"]


def test_no_avatar_does_not_create_customer_messages():
    image, layout = synthetic_frame()
    assert s.parse_messages_from_ocr([row("第一行", 200), row("第二行", 224)], image.size, target="CJTEST01", screenshot=image, layout_snapshot=layout) == []


def test_white_avatar_surface_with_disconnected_colour_islands():
    image, layout = synthetic_frame()
    image.paste((247, 247, 247), (0, 0, *image.size))
    draw = ImageDraw.Draw(image)
    draw.rectangle((400, 200, 442, 242), fill="white")
    draw.rectangle((408, 208, 420, 219), fill="blue")
    draw.rectangle((426, 224, 435, 234), fill="red")
    table = a.avatar_table(image, layout)
    assert len(table["components"]) == 1 and not table["unresolved"]
    assert table["components"][0]["bounds"] == [400, 200, 443, 243]


@pytest.mark.parametrize("kind,extra", [
    ("voice", {"text": '3"'}), ("image", {"image_candidate": True}),
    ("system", {"is_system_message": True}), ("file", {"is_file_card": True}),
    ("quote", {"is_quote_card": True}),
])
def test_real_avatar_does_not_absorb_media_or_system_rows(kind, extra):
    image, layout = synthetic_frame()
    draw_avatar(image, 400, 200)
    rows = [row("普通文字", 209), {**row("其他消息", 233), "row_kind": kind, "message_type": kind, **extra}]
    messages = s.parse_messages_from_ocr(rows, image.size, target="CJTEST01", screenshot=image, layout_snapshot=layout)
    assert messages and messages[0]["content"] == "普通文字"


def test_real_avatars_keep_opposite_roles_and_group_chat_rules():
    image, layout = synthetic_frame()
    draw_avatar(image, 400, 200)
    draw_avatar(image, 930, 260)
    rows = [row("客户原文", 209), row("销售原文", 269)]
    messages = s.parse_messages_from_ocr(rows, image.size, target="CJTEST01", screenshot=image, layout_snapshot=layout)
    assert [m["sender_role"] for m in messages] == ["customer", "self"]
    with patch.object(s, "message_line_continues_anchored_text_bubble", side_effect=AssertionError("no private rule in group chat")):
        s.parse_messages_from_ocr(rows, image.size, target="车金测试群", screenshot=image, layout_snapshot=layout)


def test_single_table_is_shared_by_real_text_voice_and_image_entrypoints():
    image, layout = synthetic_frame()
    draw_avatar(image, 400, 200)
    draw_avatar(image, 400, 400)
    ImageDraw.Draw(image).rectangle((470, 400, 700, 600), fill="#222222")
    s._LAYOUT_SNAPSHOT_STORE.put(layout)
    s._LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID[id(image)] = layout["layout_snapshot_id"]
    with patch.object(a, "_detect", wraps=a._detect) as detect:
        text = s.parse_messages_from_ocr([row("原文", 209)], image.size, target="CJTEST01", screenshot=image, layout_snapshot=layout)
        voice = s.normalize_voice_evidence_target(image, {"source": "visual_customer_voice", "click_bounds": [470, 200, 590, 242]}, image.size)
        images = s.merge_structural_image_messages(image, [], [], target="CJTEST01", layout_snapshot=layout)
    assert text[0]["sender_role"] == voice["avatar_alignment"]["role"] == "customer"
    assert images and images[0]["sender_role"] == "customer"
    assert detect.call_count == 1


def test_original_ocr_mutation_fails_and_restoration_passes(incident_frames):
    image, layout, _ = incident_frames[1]
    rows = s.run_ocr(image)
    def parse():
        return s.parse_messages_from_ocr(rows, image.size, target="CJ8R8A35", screenshot=image, layout_snapshot=layout)
    assert any(m["content"].replace("\n", "") == TEXT for m in parse())
    with patch.object(s, "message_line_continues_anchored_text_bubble", return_value=False):
        assert not any(m["content"].replace("\n", "") == TEXT for m in parse())
    assert any(m["content"].replace("\n", "") == TEXT for m in parse())


def test_invalid_frame_even_with_empty_ocr_is_not_empty_success():
    image, layout = synthetic_frame()
    with pytest.raises(a.AvatarEvidenceError):
        s.parse_messages_from_ocr([], image.size, target="CJTEST01", screenshot=image, layout_snapshot={**layout, "valid": False})
    with patch.object(a, "_detect", side_effect=RuntimeError("injected")):
        with pytest.raises(a.AvatarEvidenceError):
            s.parse_messages_from_ocr([], image.size, target="CJTEST01", screenshot=image, layout_snapshot=layout)


def test_invalid_baseline_error_does_not_reference_unassigned_validation():
    with patch.object(s, "recover_send_window_guard", return_value={"ok": True}), patch.object(s, "capture_send_fact_snapshot", side_effect=a.AvatarEvidenceError({"reason": "injected"})):
        result = s.send_payload(1, {}, target="CJTEST01", text="原文", exact=True)
    assert not result["ok"] and result["action_phase"] == "not_attempted"
    assert result["error_code"] == "SEND_BASELINE_UNAVAILABLE"


def test_invalid_avatar_crosses_cli_boundary_as_error_not_empty_read():
    error = a.AvatarEvidenceError({"state": "invalid", "reason": "layout_invalidated"})
    payload = s.sanitize_sidecar_contract_output(s.exception_payload_for_sidecar(error))
    assert payload["ok"] is False and payload["error_code"] == "C2_AVATAR_EVIDENCE_INVALID"
    assert payload["avatar_evidence"]["reason"] == "layout_invalidated"
    assert "messages" not in payload


def test_invalid_and_ambiguous_evidence_is_not_normal_absence():
    image, layout = synthetic_frame()
    assert a.role_details(image, {**layout, "valid": False}, [470, 200, 800, 222])["state"] == "invalid"
    with patch.object(a, "_detect", side_effect=ValueError("injected detector failure")):
        with pytest.raises(a.AvatarEvidenceError):
            s.message_row_avatar_role_details(image, [470, 200, 800, 222], image.size, layout_snapshot=layout)
    image, layout = synthetic_frame()
    draw_avatar(image, 930, 82)  # top edge is cut by the viewport
    assert a.role_details(image, layout, [470, 107, 810, 129])["state"] == "ambiguous"


def test_read_only_retry_limit_and_delayed_success(incident_frames):
    before = incident_frames[0][2]
    after = incident_frames[1][2]
    kwargs = dict(target="CJ8R8A35", text=TEXT, exact=True, baseline_match_count=0,
                  baseline_message_sequence=before["message_sequence"], initial_snapshot=before)
    with patch.object(s, "capture_send_fact_snapshot", return_value=after) as capture, patch.object(s.time, "sleep"), patch.object(s, "safe_send_trigger", side_effect=AssertionError("no resend")):
        ack = s.confirm_reply_sent(1, **kwargs)
        assert ack["ok"] and ack["attempt"] == 2 and capture.call_count == 1
    with patch.object(s, "capture_send_fact_snapshot", return_value=before) as capture, patch.object(s.time, "sleep"), patch.object(s, "safe_send_trigger", side_effect=AssertionError("no resend")):
        ack = s.confirm_reply_sent(1, **kwargs)
        assert not ack["ok"] and ack["error_code"] == "SEND_RESULT_UNKNOWN"
        assert len(ack["attempts"]) == 6 and capture.call_count == 5


def replay_send_transport(args, incident_frames):
    """Controlled Windows I/O only; real S0/S1/S2 OCR, grouping and send gate.

    This replaces process/desktop I/O, not the Bridge argument serialization,
    Sidecar transaction, avatar detector, OCR, comparison, or send confirmation.
    """
    def arg(name):
        return args[args.index(name)+1]
    before_image, layout, before = incident_frames[0]
    after_image, _, _ = incident_frames[1]
    path = FIXTURES / "send_input_probe_1_1788518311798.png"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == "c76620f08acafae0ec8d98a645ec04a68ea055cf4913f8a9327b685df3754f0d"
    typed_image = Image.open(path).convert("RGB")
    typed_layout = {**layout, "layout_snapshot_id": "original-input-frame", "frame_id": path.name}
    s._LAYOUT_SNAPSHOT_STORE.put(typed_layout)
    s._LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID[id(typed_image)] = typed_layout["layout_snapshot_id"]
    events = []
    geometry = before["validation"]["geometry"]
    input_bounds = layout["input_bounds"]
    def controlled_type(*_args, **_kwargs):
        events.append("type")
        return {"ok": True, "input_result": {"ok": True, "typed_chars": len(TEXT)},
                "_post_input_screenshot": typed_image, "_post_input_screenshot_path": str(path)}
    def visual_transaction(hwnd, text, **kwargs):
        return s.execute_send_transaction(hwnd, text, locator={"ok": True,
            "path": "controlled_windows_input", "input_point": ((input_bounds[0]+input_bounds[2])//2, (input_bounds[1]+input_bounds[3])//2),
            "input_click": {"bounds": input_bounds}}, **kwargs)
    with ExitStack() as stack:
        stack.enter_context(patch.object(s, "capture_wechat", side_effect=[(before_image, str(FIXTURES / next(iter(HASHES)))), (after_image, str(FIXTURES / list(HASHES)[1]))]))
        stack.enter_context(patch.object(s, "get_window_geometry", return_value=geometry))
        stack.enter_context(patch.object(s, "window_dpi_scale", return_value=1))
        stack.enter_context(patch.object(s, "recover_send_window_guard", return_value={"ok": True}))
        stack.enter_context(patch.object(s, "send_with_visual_input", side_effect=visual_transaction))
        stack.enter_context(patch.object(s, "paste_text_with_confirmation", side_effect=controlled_type))
        stack.enter_context(patch.object(s, "confirm_exact_program_draft_focus", return_value={"ok": True}))
        stack.enter_context(patch.object(s, "key_press", side_effect=lambda *_args: events.append("enter")))
        stack.enter_context(patch.object(s, "humanized_action_sleep"))
        result = s.send_payload(1, {"ok": True}, target=arg("--target"), text=arg("--text"), exact=True,
            skip_send_rate_guard=True, expected_context_guard=json.loads(arg("--expected-context-guard")),
            action_journal_path=arg("--action-journal"))
    assert events == ["type", "enter"], (events, result)
    assert result["send_result"]["sent_confirmation"]["attempt"] == 1, result
    result["sidecar_run_id"] = "raw-avatar-replay-0964"
    return json.loads(json.dumps(result, ensure_ascii=False))
