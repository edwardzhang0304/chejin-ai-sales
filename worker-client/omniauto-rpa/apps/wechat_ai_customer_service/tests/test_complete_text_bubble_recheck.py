"""Synthetic pixel geometry and real OCR negative controls (not Windows UAT)."""
import os
from pathlib import Path
import sys

import pytest
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.text_bubble_recheck import (
    locate_complete_bubbles, recognize_regions,
)


def scene(*, color=(157,242,159), top=30):
    image = Image.new("RGB", (500,240), (250,250,250))
    ImageDraw.Draw(image).rectangle([40,top,400,180], fill=color)
    row = {"observation_id": "a", "row_kind": "text_bubble", "message_type": "text",
           "sender_role": "self", "bubble_rect": [50,max(35,top+10),280,160]}
    return image, row


@pytest.mark.parametrize("color", [(157,242,159), (237,237,237), (100,110,120)])
def test_full_background_component_extends_past_missing_line_end(color):
    image, row = scene(color=color)
    result = locate_complete_bubbles(image, [row], ["a"], [0,0,500,220])
    assert result[0]["bubble_rect"] == [40,30,401,181]
    assert result[0]["crop_rect"] == [32,22,409,189]
    assert result[0]["crop_rect"][2] > row["bubble_rect"][2]+100


@pytest.mark.parametrize("case", ["clipped", "background", "two_observations", "media", "unknown_role", "ambiguous_id"])
def test_uncertain_pixels_are_never_guessed(case):
    image, row = scene(top=0 if case == "clipped" else 30, color=(250,250,250) if case == "background" else (157,242,159))
    rows = [row]
    if case == "two_observations": rows.append({**row, "observation_id": "b"})
    if case == "media": row["message_type"] = "image"
    if case == "unknown_role": row["sender_role"] = "unknown"
    if case == "ambiguous_id": rows.append(dict(row))
    with pytest.raises(ValueError): locate_complete_bubbles(image, rows, ["a"], [0,0,500,220])


@pytest.mark.parametrize("text,forbidden", [("预算9万", "预算8万"), ("不可以", "可以")])
def test_real_ocr_negative_keeps_changed_number_and_negation(text, forbidden):
    font_path = os.environ.get("TEXT_RECHECK_TEST_FONT")
    if not font_path:
        pytest.skip("Chinese font path and existing RapidOCR required for real OCR negative control")
    from rapidocr_onnxruntime import RapidOCR
    from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.ocr_engine import OcrEngineRunner
    from apps.wechat_ai_customer_service.adapters.message_viewport_projection import stable_business_content_signature
    image = Image.new("RGB", (500,160), (250,250,250))
    painter = ImageDraw.Draw(image)
    painter.rectangle([40,30,400,115], fill=(157,242,159))
    painter.text((55,45), text, font=ImageFont.truetype(font_path, 30), fill=(20,20,20))
    row = {"observation_id": "negative", "row_kind": "text_bubble", "message_type": "text",
           "sender_role": "customer", "bubble_rect": [55,45,210,90]}
    regions = locate_complete_bubbles(image, [row], ["negative"], [0,0,500,150])
    result = recognize_regions(image, regions, OcrEngineRunner(RapidOCR).run)
    actual = "".join(item["text"] for item in result[0]["ocr_items"])
    assert actual == text
    assert stable_business_content_signature({**row, "content_clean": actual}) != stable_business_content_signature({**row, "content_clean": forbidden})
