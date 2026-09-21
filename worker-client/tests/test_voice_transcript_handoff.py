"""Real OCR -> parser/envelope -> production transcript execution.

Original icon pixels, rendered transcript text; desktop/menu/capture are
controlled. No proof or transcript metadata is inserted by the test harness.
This does not exercise Windows or backend business settlement.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont
import test_wechat_win32_ocr_sidecar_voice as fixtures

sidecar = fixtures.sidecar
ASSETS = Path(__file__).parent / "fixtures/voice_icons"


@pytest.fixture(params=[("customer", "15", 1), ("customer", "预算十五万", 1.25),
                        ("self", "15", 1), ("self", "预算十五万", 1)])
def parsed_transcript(request, tmp_path):
    role, body, scale = request.param
    before = Image.new("RGB", (965, 852), (250, 250, 250))
    with Image.open(ASSETS / ("customer_9s.png" if role == "customer" else "self_2s.png")) as crop:
        before.paste(crop.convert("RGB"), (448, 285) if role == "customer" else (680, 285))
    fixtures.WechatWin32OcrVoiceSelectionTest.draw_avatar(
        ImageDraw.Draw(before), (398, 315, 444, 361) if role == "customer" else (900, 315, 946, 361))
    after = before.copy()
    draw = ImageDraw.Draw(after)
    font = ImageFont.truetype("/System/Library/Fonts/STHeiti Light.ttc", 20)
    width = max(65, round(draw.textlength(body, font=font)) + 26)
    left, right = (458, 458 + width) if role == "customer" else (870 - width, 870)
    draw.rounded_rectangle((left, 388, right, 438), radius=8, fill=(238, 238, 240))
    draw.text((left + 12, 401), body, font=font, fill=(25, 25, 25))
    size = (round(before.width * scale), round(before.height * scale))
    before, after = before.resize(size), after.resize(size)
    fixture = fixtures.WechatWin32OcrVoiceSelectionTest()
    fixture.setUp()
    try:
        for frame in (before, after):
            fixture._semantic_layout_for_image(frame)["dpi_scale"] = scale
        before_items = sidecar._run_chat_text_ocr(before, "voice_handoff_before")
        before_messages = sidecar.parse_current_chat_frame_messages(
            before_items, size, target="CJVOICE1", screenshot=before)
        candidates = [o for o in sidecar.build_unified_voice_observations_v3(
            before, before_items, size, parsed_messages=before_messages) if o.get("action_target")]
        assert len(candidates) == 1, candidates
        items = sidecar._run_chat_text_ocr(after, "voice_handoff_after")
        messages = sidecar.parse_current_chat_frame_messages(items, size, target="CJVOICE1", screenshot=after)
        before.save(tmp_path / "before.png")
        after.save(tmp_path / "after.png")
        (tmp_path / "parser.json").write_text(json.dumps({
            "before_ocr": before_items, "before_messages": before_messages,
            "candidate": candidates[0], "after_ocr": items, "messages": messages,
        }, ensure_ascii=False, indent=2))
        assert len(messages) == 1, messages
        assert (messages[0]["type"], messages[0]["sender_role"], messages[0]["content"]) == ("voice", role, body)
        yield fixture, candidates[0], messages[0], after, body
    finally:
        fixture.doCleanups()


def test_real_parser_transcript_completes_formal_execution(parsed_transcript, tmp_path):
    fixture, candidate, message, image, body = parsed_transcript
    original = deepcopy(message)
    result, click, _, phase = fixture._execute_prepared_voice(
        candidate=candidate, bound_message=message,
        preserve_parsed_message=True, execution_image=image)
    (tmp_path / "execution.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    assert result.get("ok") is True, result
    assert result["state"] == "voice_transcribe_completed"
    assert result["transcript_binding_status"] == "confirmed"
    assert result["binding_candidate_count"] == 1
    assert phase == "confirmed"
    assert click.call_count == 1
    assert result["messages"][0]["content"] == body
    assert message == original  # The harness must not fill any parser fields.
    markers = [r for r in message["ocr_items"] if sidecar.voice_duration_item_like(r)]
    assert len(markers) == 1
    proof = markers[0]["_voice_visual_evidence"]
    assert sidecar.message_voice_duration_number(message) == (str(proof["seconds"]) if proof["seconds"] is not None else "")
    # The same proof must also survive into the existing diagnostic report.
    sidecar.write_messages_frame_review(tmp_path, {"messages": [message]})
    report = json.loads((tmp_path / "wechat_messages_frame_review.json").read_text())
    assert json.dumps(proof, sort_keys=True) in json.dumps(report, sort_keys=True)


def test_old_frame_proof_cannot_authorize_new_frame(parsed_transcript):
    _, _, message, image, _ = parsed_transcript
    # Actual transported rows, then a fresh frame with no voice pixels.
    clean = Image.new("RGB", image.size, (250, 250, 250))
    rows = sidecar.voice_icons.annotate_duration_rows(message["ocr_items"], clean, [0, 0, *clean.size])
    assert not any(sidecar.voice_duration_item_like(row) for row in rows)
    assert all("_voice_visual_evidence" not in row and "_voice_transcript_region" not in row for row in rows)
