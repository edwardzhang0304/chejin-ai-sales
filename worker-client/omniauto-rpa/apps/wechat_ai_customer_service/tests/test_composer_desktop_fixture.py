"""The controlled clipboard and captured draft must describe the same text."""
import json

import pytest
from PIL import ImageChops

import dynamic_composer_desktop as desktop


@pytest.mark.parametrize("reply", ["好的，我帮您看看", "好的，按十五万预算重新筛选电车。"])
def test_short_draft_pixels_match_text_after_reply_override(monkeypatch, tmp_path, reply):
    _, before = desktop.derived_frames(reply=reply, movement=0, reduction=0)
    monkeypatch.setattr(desktop, "REPLY", reply)
    _, after = desktop.derived_frames(reply=reply, movement=0, reduction=0)
    assert ImageChops.difference(before["typing"], after["typing"]).getbbox() is None
    # Real OCR on the drawn input, independent of the controlled clipboard.
    crop = after["typing"].crop((313, 701, 766, 789))
    crop.save(tmp_path / "draft.png")
    items = desktop.sidecar.run_ocr(crop)
    observed = "".join(item["text"] for item in items)
    normalize = lambda text: "".join(char for char in text if char.isalnum())
    assert normalize(observed) == normalize(reply), observed
    (tmp_path / "draft.json").write_text(json.dumps({"typed": reply, "ocr": observed}, ensure_ascii=False))
