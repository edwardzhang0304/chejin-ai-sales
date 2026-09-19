"""Real incident pixels, real OCR; private chats are not embedded in the repository."""
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as s
from test_avatar_text_input import verified_frame_io, run_local_recheck


@pytest.fixture(scope="module", params=[False, True], ids=["full", "roi"])
def segmentation_frames(request, tmp_path_factory):
    root = os.environ.get("CHEJIN_SEGMENTATION_INCIDENT_ROOT")
    if not root:
        pytest.skip("Private CJNX4V8R original PNG/report required")
    directory = Path(root)/"artifacts/wechat_c2/messages"
    results = {}
    for name, prefix, expected_count in [("before", "20260919_155110", 2),
                                          ("after", "20260919_155412", 4)]:
        report_path = next(directory.glob(prefix+"*/wechat_messages_frame_review.json"))
        report = json.loads(report_path.read_text())
        detail = next(e["result"] for e in report["events"] if "message_ocr_items" in e.get("result", {}))
        target = next(e["result"]["confirmed_target"] for e in report["events"] if e.get("result", {}).get("confirmed_target"))
        layout = detail["layout_snapshot"]
        original = report_path.parent/Path(report["summary"]["screenshot_path"].replace("\\", "/")).name
        png = tmp_path_factory.mktemp("segmentation-"+name)/"original.png"
        png.write_bytes(original.read_bytes())
        if name == "after":
            assert hashlib.sha256(png.read_bytes()).hexdigest() == "8abefa1a900fec10f5e224ec4d7bf349b24f48e8ce20bc5bc28bc048a9cc8ba0"
        raw = Image.open(png).convert("RGB")
        # capture_wechat normally registers this original-frame layout. The
        # desktop substitute must install the exact saved geometry as well.
        s._LAYOUT_SNAPSHOT_STORE.put(layout)
        s._LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID[id(raw)] = layout["layout_snapshot_id"]
        with verified_frame_io(raw, layout, png):
            result = s.messages_payload(1, {}, target=target, confirm_target=target,
                history_load_times=0, artifact_dir=str(png.parent),
                chat_fact_roi_ocr=request.param, retain_text_recheck_frame=True)
        (png.parent/"parsed.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        assert result["ok"], result
        assert not result.get("observation_validation_errors"), result
        assert len(result["observations"]) == expected_count, result["observations"]
        results[name] = result, raw, layout, png
    return results


def test_real_frame_keeps_one_complete_reply_and_the_new_question(segmentation_frames):
    before = segmentation_frames["before"][0]["observations"]
    current = segmentation_frames["after"][0]["observations"]
    assert [o["sender_role"] for o in before] == ["self", "customer"]
    assert [o["sender_role"] for o in current] == ["self", "customer", "self", "customer"]
    def text_digest(row):
        return hashlib.sha256(row["content_clean"].replace("\n", "").replace(" ", "").encode()).hexdigest()
    # Frozen hashes of the complete original reply and the new question.
    assert text_digest(current[-2]) == "9b8190ba01b2e882a36bde6a6142b0bb8c1a3a807d2f506a74a4c3dc8c7d848f"
    assert text_digest(current[-1]) == "7d2b3241f2b75875e6e38a92d5d06d8553a5abe6d1a511c8191770221ab64839"
    assert all("UNI" not in o["content_clean"] for o in current)


@pytest.mark.parametrize("stage", ["validate", "ocr"])
def test_real_complete_bubble_recheck_keeps_existing_admission_rules(segmentation_frames, tmp_path, stage):
    payload, raw, layout, png = segmentation_frames["after"]
    frame = json.loads(Path(payload["text_recheck_frame_path"]).read_text())
    result, captures, calls = run_local_recheck((payload, raw, layout, png, frame), tmp_path, stage, selected_index=-2)
    assert captures == 0
    assert calls == (1 if stage == "ocr" else 0)
    if stage == "validate":
        assert result["ok"], result
    else:
        # This unnecessary forced crop was already ambiguous in native OCR:
        # the scaled line splits after "您。". Keep the original fail-closed
        # rule; normal continuation now succeeds without this recovery step.
        assert not result["ok"]
        assert result["reason"] == "text_recheck_regroup_not_unique"
        assert not result["ui_action_performed"] and not result["new_capture_performed"]
        masked, _ = s.avatar_text_input.prepare(raw, layout)
        for region in result["regions"]:
            assert raw.crop(tuple(region["crop_rect"])).tobytes() == masked.crop(tuple(region["crop_rect"])).tobytes()
        from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.text_bubble_recheck import recognize_regions
        native = recognize_regions(raw, result["regions"], s.run_ocr)
        assert [r["ocr_items"] for r in native] == [r["ocr_items"] for r in result["regions"]]


@pytest.mark.parametrize("size", [(800, 852), (1000, 1065), (1200, 1278), (2100, 2200), (800, 30)])
@pytest.mark.parametrize("classification", [False, True])
def test_separate_sources_retain_native_geometry_filters_and_single_pass(size, classification):
    raw = Image.new("RGB", size, "white")
    ImageDraw.Draw(raw).text((5, 5), "HELLO WORLD 123", fill="black", font_size=20)
    # Warm the cached real runtime, then compare against its native pipeline.
    s.run_ocr(raw)
    engine = s._OCR_ENGINE
    with patch.object(engine, "use_cls", classification):
        expected = s.run_ocr(raw)
        assert expected
        with patch.object(engine, "text_det", wraps=engine.text_det) as det, patch.object(engine, "text_cls", wraps=engine.text_cls) as cls, patch.object(engine, "text_rec", wraps=engine.text_rec) as rec:
            actual = s.run_ocr(raw, recognition_image=raw.copy())
        assert actual == expected
        assert det.call_count == rec.call_count == 1
        assert cls.call_count == int(classification)


def test_separate_input_never_falls_back_to_unmasked_recognition():
    raw = Image.new("RGB", (800, 600), "white")
    ImageDraw.Draw(raw).text((100, 100), "SECRET AVATAR", fill="black", font_size=26)
    assert s.run_ocr(raw)
    assert s.run_ocr(raw, recognition_image=Image.new("RGB", raw.size, "white")) == []
    with pytest.raises(ValueError, match="size_mismatch"):
        s.run_ocr(raw, recognition_image=raw.resize((400, 300)))


def test_default_engine_call_remains_compatible_with_existing_callers():
    class Engine:
        calls = []
        def __call__(self, image):
            self.calls.append(image)
            return [], None
    engine = Engine(); image = object()
    result, cached = s.win32_ocr_engine.run_ocr_with_cache(image, engine_factory=Engine, engine=engine)
    assert result == [] and cached is engine and engine.calls == [image]
