"""0.9.66 object classification regression; all images here are SYNTHETIC.

PR #16 (许聪, a046760de74dcef39a4a04f0f3819e6beda8f65e) supplied the
normal-bubble geometry. Retain its reproduction, not its narrowed column or
new tolerance. Real Windows PNG/OCR and automatic send closure live in
test_frame_avatars.py and backend/tests/test_avatar_send_closure.py.
"""
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw, ImageOps

from test_frame_avatars import a, s, draw_avatar, row, write_avatar_evidence

EXCLUSION = "independent_inward_object_beside_confirmed_avatar"


def bubble_frame(role="customer", dpi=1.0, shift=0, size=0, variant="normal", width=983):
    """Artificial pixels + controlled OCR; never described as Windows capture."""
    coord = lambda v: round(v * dpi)
    image = Image.new("RGB", (coord(width), coord(1056)), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.rectangle(tuple(map(coord, (0, 0, 348, 872))), fill=(238, 238, 238))
    if variant not in {"missing", "opposite", "multiple"}:
        draw_avatar(image, 351, 244 if variant == "other_row" else 324, dpi)
    if variant == "opposite":
        draw_avatar(image, width-55, 324, dpi)
    if variant == "multiple":
        # Two separate 25x25 contours both satisfy existing avatar admission.
        for x in (351, 381):
            draw.rectangle(tuple(map(coord, (x, 324, x+24, 348))), fill="#555555")
    bubble_left = 414 if variant == "multiple" else 407 + shift
    bubble_bottom = 369 + size
    draw.rounded_rectangle(tuple(map(coord, (bubble_left, 324, 479+shift+size, bubble_bottom))),
                           radius=coord(16), fill=(235, 235, 235))
    if variant == "attached":
        draw.rectangle(tuple(map(coord, (385, 340, bubble_left+10, 349))), fill="#555555")
    if variant == "clipped":
        # A concave cross-column object reaching the viewport bottom. A tall
        # solid rectangle does not enter the existing attached-candidate rule.
        draw.rectangle(tuple(map(coord, (420, 355, 429, 880))), fill=(235, 235, 235))
    viewport = list(map(coord, (348, 110, width, 872)))
    input_bounds = list(map(coord, (348, 872, width, 1056)))
    bounds = list(map(coord, (426+shift, 334, 469+shift, 356)))
    if role == "self":
        image = ImageOps.mirror(image)
        def mirror(box):
            return [image.width-box[2], box[1], image.width-box[0], box[3]]
        viewport, input_bounds, bounds = map(mirror, (viewport, input_bounds, bounds))
    layout = {"valid": True, "layout_snapshot_id": "synthetic-object-relation",
              "dpi_scale": dpi, "message_viewport_bounds": viewport, "input_bounds": input_bounds}
    left, top, right, bottom = bounds
    ocr = {"text": "你好", "left": left, "top": top, "right": right, "bottom": bottom,
           "center_x": (left+right)/2, "center_y": (top+bottom)/2, "confidence": 1}
    return image, layout, [ocr]


def parse(image, layout, rows):
    return s.parse_messages_from_ocr(rows, image.size, target="CJTEST01",
                                    screenshot=image, layout_snapshot=layout)


@pytest.mark.parametrize("role", ["customer", "self"])
@pytest.mark.parametrize("dpi", [1.0, 1.25, 1.5])
@pytest.mark.parametrize("shift", [-2, -1, 0, 1])
@pytest.mark.parametrize("size", [-3, 0, 5])
def test_normal_bubble_keeps_full_text_and_avatar(role, dpi, shift, size):
    image, layout, rows = bubble_frame(role, dpi, shift, size)
    table = a.avatar_table(image, layout)
    name = f"synthetic-{role}-dpi{dpi}-shift{shift}-size{size}"
    try:
        messages = parse(image, layout, rows)
    except a.AvatarEvidenceError as error:
        write_avatar_evidence(name, {"source": "synthetic_pixels_controlled_ocr",
            "layout": layout, "ocr": rows, "avatar_table": table, "error": error.evidence}, image)
        raise
    write_avatar_evidence(name, {"source": "synthetic_pixels_controlled_ocr",
        "layout": layout, "ocr": rows, "avatar_table": table, "messages": messages}, image)
    assert len(table["components"]) == 1 and not table["unresolved"], table
    exclusions = [item for item in table["excluded"] if item["reason"] == EXCLUSION]
    # Some larger/scaled rounded rectangles already fail the original concave
    # candidate threshold. They need correct text, not a fabricated exclusion.
    assert len(exclusions) <= 1, table
    for exclusion in exclusions:
        assert exclusion["supporting_avatar_bounds"] == table["components"][0]["bounds"]
        assert exclusion["bounds"] == exclusion["object_bounds"]
    assert len(messages) == 1 and messages[0]["sender_role"] == role
    assert messages[0]["content"] == rows[0]["text"] == "你好"


@pytest.mark.parametrize("role", ["customer", "self"])
@pytest.mark.parametrize("shift", [-2, -1, 0, 1])
def test_reported_geometry_is_explicitly_excluded(role, shift):
    image, layout, rows = bubble_frame(role, shift=shift)
    table = a.avatar_table(image, layout)
    exclusions = [item for item in table["excluded"] if item["reason"] == EXCLUSION]
    assert len(exclusions) == 1 and not table["unresolved"], table
    assert exclusions[0]["bounds"] == exclusions[0]["object_bounds"]
    assert exclusions[0]["supporting_avatar_bounds"] == table["components"][0]["bounds"]
    assert parse(image, layout, rows)[0]["content"] == "你好"


@pytest.mark.parametrize("width", [920, 1000, 1180])
@pytest.mark.parametrize("dpi", [1.0, 1.25, 1.5])
@pytest.mark.parametrize("role", ["customer", "self"])
def test_bubble_supported_layout_sizes(width, dpi, role):
    image, layout, rows = bubble_frame(role, dpi, width=width)
    assert parse(image, layout, rows)[0]["sender_role"] == role
    assert not a.avatar_table(image, layout)["unresolved"]


@pytest.mark.parametrize("role", ["customer", "self"])
@pytest.mark.parametrize("variant", ["attached", "clipped", "missing", "opposite", "other_row", "multiple"])
def test_unproven_object_stays_unresolved(role, variant):
    image, layout, rows = bubble_frame(role, variant=variant)
    table = a.avatar_table(image, layout)
    assert table["unresolved"], table
    assert not any(item["reason"] == EXCLUSION for item in table["excluded"]), table
    with pytest.raises(a.AvatarEvidenceError):
        parse(image, layout, rows)


def test_exclusion_does_not_clear_unrelated_unresolved_object():
    image, layout, rows = bubble_frame()
    draw_avatar(image, 351, 500)
    ImageDraw.Draw(image).rectangle((370, 527, 379, 880), fill="#555555")
    table = a.avatar_table(image, layout)
    assert any(item["reason"] == EXCLUSION for item in table["excluded"])
    assert any(item["reason"] == "candidate_clipped" for item in table["unresolved"])
    assert parse(image, layout, rows)[0]["content"] == "你好"
    with pytest.raises(a.AvatarEvidenceError):
        parse(image, layout, [row("另一个未决对象", 509, left=426, right=469)])


@pytest.mark.parametrize("role", ["customer", "self"])
def test_two_independent_bubbles_remain_two_messages(role):
    image, layout, rows = bubble_frame(role)
    # Copy this synthetic row only; two identical avatar pictures still refer
    # to two independent contours, and neither message is a continuation.
    image.paste(image.crop((0, 320, image.width, 375)), (0, 380))
    second = {**rows[0], "text": "第二条独立消息"}
    for key in ("top", "bottom", "center_y"):
        second[key] += 60
    messages = parse(image, layout, rows + [second])
    assert [m["content"] for m in messages] == ["你好", "第二条独立消息"]
    assert len({m["avatar_alignment"]["avatar_component_id"] for m in messages}) == 2
    table = a.avatar_table(image, layout)
    assert len(table["components"]) == 2 and not table["unresolved"]
    assert len([item for item in table["excluded"] if item["reason"] == EXCLUSION]) == 2


@pytest.mark.parametrize("role", ["customer", "self"])
def test_isolated_mutation_reproduces_error_then_restores(role):
    image, layout, rows = bubble_frame(role)
    assert parse(image, layout, rows)[0]["content"] == "你好"
    # Only disable E's classification in memory. New image avoids frame cache;
    # neither consumer nor OCR/association/grouping/role is patched.
    with patch.object(a, "_exclude_independent_inward_objects", return_value=None):
        mutated = image.copy()
        with pytest.raises(a.AvatarEvidenceError):
            parse(mutated, layout, rows)
        assert a.avatar_table(mutated, layout)["unresolved"]
    assert parse(image.copy(), layout, rows)[0]["content"] == "你好"
