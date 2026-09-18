"""Synthetic status-icon pixels, not Windows or customer-media acceptance."""
from PIL import Image, ImageDraw
import pytest
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.send_status_indicator import inspect_gutter
from apps.wechat_ai_customer_service.adapters.text_correspondence import find_new_matching_self_message


@pytest.mark.parametrize("scale", [1, 1.5, 2])
@pytest.mark.parametrize("kind", ["clear", "red_failure", "possible_sending"])
def test_status_gutter_counterevidence(scale, kind):
    image = Image.new("RGB", (round(600*scale), round(300*scale)), (250,250,250))
    draw = ImageDraw.Draw(image)
    def bounds(values): return tuple(round(v*scale) for v in values)
    bubble = bounds((250,100,550,180))
    draw.rounded_rectangle(bubble, radius=round(5*scale), fill=(149,236,105))
    marker = bounds((225,133,239,147))
    if kind == "red_failure":
        draw.ellipse(marker, fill=(230,55,55))
        draw.line(bounds((232,136,232,141)), fill="white", width=max(1,round(scale)))
        draw.point(bounds((232,144)), fill="white")
    elif kind == "possible_sending":
        draw.arc(marker, start=0, end=285, fill=(140,140,140), width=max(1,round(2*scale)))
    result = inspect_gutter(image, bubble, dpi_scale=scale)
    assert result["state"] == ("clear" if kind == "clear" else "blocked")
    if kind != "clear": assert result["reason"] == kind
    before = [{"observation_id":"a", "row_kind":"text_bubble", "sender_role":"customer", "content_normalized":"你好"}]
    current = before + [{"observation_id":"b", "row_kind":"text_bubble", "sender_role":"self",
                         "content_normalized":"您好，我来帮您看看。", "send_status_evidence":result}]
    assert bool(find_new_matching_self_message(before,current,"您好，我来帮您看看。")) == (kind == "clear")


def test_unrelated_red_pixels_and_invalid_geometry():
    image=Image.new("RGB",(600,300),(250,250,250))
    ImageDraw.Draw(image).ellipse((10,10,26,26),fill=(230,55,55))
    assert inspect_gutter(image,[250,100,550,180])["state"] == "clear"
    assert inspect_gutter(image,None)["state"] == "unavailable"
