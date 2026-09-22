"""Synthetic separator geometry, not a Windows desktop acceptance test."""
import pytest
from PIL import Image, ImageDraw

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_layout as layout


@pytest.mark.parametrize("scale", [1, 1.5, 2])
@pytest.mark.parametrize("thickness", [1, 3, 6])
@pytest.mark.parametrize("bubble", [(157, 242, 159), (238, 238, 240)])
def test_thin_separator_survives_message_covering_most_columns(scale, thickness, bubble):
    width, height = round(484 * scale), round(250 * scale)
    top, line_height = round(80 * scale), round(thickness * scale)
    image = Image.new("RGB", (width, height), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, top, width - 1, top + line_height - 1), fill=(240, 240, 240))
    draw.rectangle((round(width * .08), top + line_height,
                    round(width * .92), top + line_height + round(70 * scale)), fill=bubble)
    assert layout._separator_content_start(
        image, left=0, right=width, edge_y=top - 1,
        measured_row_height=round(24 * scale),
    ) == top + line_height


@pytest.mark.parametrize("impostor", ["missing", "sampled_text", "partial_line", "wide_bubble", "thick_block"])
def test_non_separator_pixels_do_not_authorize_a_boundary(impostor):
    width = 500
    image = Image.new("RGB", (width, 250), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    if impostor == "sampled_text":
        # These disconnected strokes hit every coarse sampling column.
        for ratio in (.05, .18, .34, .50, .66, .82, .95):
            x = int(width * ratio)
            draw.rectangle((x - 3, 80, x + 3, 81), fill=(120, 120, 120))
    elif impostor == "partial_line":
        draw.rectangle((0, 80, width - 1, 80), fill=(240, 240, 240))
        draw.rectangle((200, 79, 240, 82), fill=(250, 250, 250))
    elif impostor == "wide_bubble":
        draw.rectangle((15, 80, 485, 83), fill=(157, 242, 159))
    elif impostor == "thick_block":
        draw.rectangle((0, 80, 499, 94), fill=(240, 240, 240))
    assert layout._separator_content_start(
        image, left=0, right=width, edge_y=79, measured_row_height=24,
    ) is None


def test_missing_header_with_real_input_border_still_fails_calibration():
    image = Image.new("RGB", (784, 844), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 59, 843), fill=(225, 225, 225))
    draw.rectangle((60, 0, 299, 843), fill=(235, 235, 235))
    draw.line((300, 700, 783, 700), fill=(240, 240, 240))
    anchors = [dict(text="搜索", left=75, top=46, right=128, bottom=68, confidence=.99)]
    result = layout.build_startup_layout_calibration(
        hwnd=1, process_id=1, image=image, ocr_items=anchors,
        window_rect=[12, 12, 812, 864], client_rect={"width":784, "height":844},
        client_screen_origin=[20, 12], dpi_scale=1, capture_mode="client_area",
    )
    assert not result["executable"]
    assert "chat_header_boundary_missing" in result["conflicts"]


def test_unstable_exit_does_not_slide_boundary_down_into_content():
    image = Image.new("RGB", (500, 250), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.line((0, 80, 499, 80), fill=(240, 240, 240))
    for y in range(81, 93):
        shade = 246 if y % 2 else 255
        draw.line((0, y, 499, y), fill=(shade, shade, shade))
    assert layout._separator_content_start(
        image, left=0, right=500, edge_y=79, measured_row_height=24,
    ) is None
