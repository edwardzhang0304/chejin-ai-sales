"""Dynamic composer regression; real frames are opt-in, never fabricated OCR."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_layout


def install_desktop(monkeypatch, tmp_path, calibration):
    """Only the saved desktop/OS boundary is substituted; registration is real."""
    path = tmp_path / "startup.json"
    window_layout.write_startup_layout_calibration(path, calibration)
    monkeypatch.setattr(sidecar, "STARTUP_CALIBRATION_PATH", path)
    rect = calibration["window_rect"]
    geometry = dict(zip(("left", "top", "right", "bottom"), rect))
    geometry.update(width=rect[2]-rect[0], height=rect[3]-rect[1])
    origin = calibration["client_screen_origin"]
    width, height = calibration["image_width"], calibration["image_height"]
    client = {"left": 0, "top": 0, "right": width, "bottom": height,
              "width": width, "height": height,
              "screen_left": origin[0], "screen_top": origin[1]}
    monkeypatch.setattr(sidecar, "get_window_geometry", lambda _: dict(geometry))
    monkeypatch.setattr(sidecar, "get_window_client_geometry", lambda _: dict(client))
    monkeypatch.setattr(sidecar, "window_dpi_scale", lambda _: calibration["dpi_scale"])
    monkeypatch.setattr(sidecar, "screen_work_area", lambda _: {})
    monkeypatch.setattr(sidecar, "win32process", SimpleNamespace(
        GetWindowThreadProcessId=lambda _: (1, calibration["process_id"])))
    return geometry


def register(image, calibration, path=""):
    return sidecar._register_layout_snapshot(
        calibration["hwnd"], image, capture_mode="client_area",
        screenshot_path=str(path), capture_screen_origin=calibration["client_screen_origin"],
    )


def incident_inputs():
    root = Path(os.environ.get("CHEJIN_COMPOSER_INCIDENT", ""))
    if not (root / "replay_before_input.json").is_file():
        pytest.skip("requires private incidents7 original PNGs and recorded startup layout")
    saved = json.loads((root / "replay_before_input.json").read_text())
    calibration = copy.deepcopy(saved["layout"])
    calibration["process_id"] = 2188
    paths = {label: Path(json.loads((root / f"replay_{label}.json").read_text())["image"])
             for label in ("before_input", "after_input", "after_cleanup")}
    return calibration, paths


@pytest.mark.parametrize("roi", [True, False])
def test_original_three_frames_real_registration_and_ocr(monkeypatch, tmp_path, roi):
    calibration, paths = incident_inputs()
    install_desktop(monkeypatch, tmp_path, calibration)
    records = []
    for label, path in paths.items():
        image = Image.open(path).convert("RGB")
        layout = register(image, calibration, path)
        assert layout["valid"], layout
        items, plan = sidecar.run_ocr_for_chat_fact_frame(
            image, purpose="composer_original", source="original_png_replay", enabled=roi)
        messages = sidecar.parse_current_chat_frame_messages(
            items, image.size, target="CJMKZUTH", screenshot=image)
        content = [(m["sender_role"], m["content"]) for m in messages]
        assert len(messages) == 4
        assert content[-1][0] == "customer" and "还是买电车好" in content[-1][1]
        records.append({"label": label, "layout": layout, "ocr": items,
                        "plan": plan, "messages": content})
    assert records[0]["messages"] == records[1]["messages"] == records[2]["messages"]
    assert records[1]["layout"]["message_viewport_bounds"][3] < records[0]["layout"]["message_viewport_bounds"][3]
    assert records[0]["layout"]["message_viewport_bounds"] == records[2]["layout"]["message_viewport_bounds"]
    (tmp_path / "original_frames.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))


def test_original_draft_fails_with_old_registration_regions(monkeypatch, tmp_path):
    calibration, paths = incident_inputs()
    install_desktop(monkeypatch, tmp_path, calibration)
    # Explicit mutation control: disable just the new measurement, preserving
    # the old startup regions. Same unmodified PNG and real OCR must fail.
    monkeypatch.setattr(window_layout, "measure_business_input_regions", lambda image, c: {
        "ok": True, "regions": {n: c[n] for n in window_layout.REQUIRED_LAYOUT_REGION_NAMES},
        "anchors": [], "confidence": c["confidence"], "conflicts": [], "vertical_candidates": [],
    })
    image = Image.open(paths["after_input"]).convert("RGB")
    register(image, calibration, paths["after_input"])
    items, _ = sidecar.run_ocr_for_chat_fact_frame(
        image, purpose="old_boundary_control", source="original_png_replay", enabled=True)
    with pytest.raises(sidecar.frame_avatars.AvatarEvidenceError, match="avatar_association_unresolved"):
        sidecar.parse_current_chat_frame_messages(items, image.size, target="CJMKZUTH", screenshot=image)


@pytest.mark.parametrize("mode", ["missing", "ambiguous"])
def test_unmeasured_boundary_cannot_inherit_startup_success(monkeypatch, tmp_path, mode):
    calibration, paths = incident_inputs()
    install_desktop(monkeypatch, tmp_path, calibration)
    image = Image.open(paths["before_input"]).convert("RGB")
    draw = ImageDraw.Draw(image)
    if mode == "missing":
        draw.rectangle((301, 680, 778, 799), fill=(250, 250, 250))
    else:
        draw.line((301, 600, 778, 600), fill=(200, 200, 200), width=2)
    layout = register(image, calibration)
    assert not layout["valid"]
    assert f"input_boundary_{mode}" in layout["layout_builder"]["conflicts"]


@pytest.mark.parametrize("movement", [185, 160])
@pytest.mark.parametrize("reuse", [True, False])
def test_derived_history_and_partial_top_real_send_chain(monkeypatch, tmp_path, movement, reuse):
    from dynamic_composer_desktop import Desktop, derived_frames
    monkeypatch.setenv("CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED", str(int(reuse)))
    calibration,frames=derived_frames(movement=movement)
    desktop=Desktop(monkeypatch,tmp_path,calibration,frames)
    result=desktop.run()
    assert result.get("ok"), result
    assert desktop.enter_count == 1
    assert result["send_result"]["result"] == "sent"
    visual = result["send_result"]["visual"]
    assert visual["context_check"]["reason"] == "history_suffix_unchanged"
    decision=visual["context_check"]["worker_continuity_decision"]
    assert decision["new_suffix_indexes"] == [] and len(decision["matched_pairs"]) == 3
    assert visual["context_check"]["composer_crop_evidence"]["observed_upward_pixels"] == movement
    assert result["action_phase"] == "confirmed"
    assert desktop.clipboard == "original clipboard"


@pytest.mark.parametrize("movement", [185,160])
def test_history_suffix_disabled_reproduces_false_block(monkeypatch,tmp_path,movement):
    from dynamic_composer_desktop import Desktop, derived_frames
    original=sidecar._shared_compare_business_viewport_continuity
    def disabled(*args,**kwargs):
        kwargs["allow_history_suffix"]=False
        return original(*args,**kwargs)
    monkeypatch.setattr(sidecar,"_shared_compare_business_viewport_continuity",disabled)
    calibration,frames=derived_frames(movement=movement)
    desktop=Desktop(monkeypatch,tmp_path,calibration,frames)
    result=desktop.run()
    assert not result["ok"] and desktop.enter_count == 0
    assert result["error_code"] == "C3_CONTEXT_CHANGED_BEFORE_SEND"
    assert result["guard"]["visual"]["draft_clear"]["cleared"] is True
    assert not desktop.draft


@pytest.mark.parametrize("kind", ["customer","same","sales","image"])
def test_new_fact_during_input_never_triggers_send(monkeypatch,tmp_path,kind):
    from dynamic_composer_desktop import Desktop, derived_frames
    calibration,frames=derived_frames(new_kind=kind)
    desktop=Desktop(monkeypatch,tmp_path,calibration,frames)
    result=desktop.run()
    assert not result["ok"] and desktop.enter_count == 0
    assert result["action_phase"] == "not_attempted"
    assert result["guard"]["visual"]["draft_clear"]["cleared"] is True


def test_cleanup_failure_keeps_block_and_owned_draft(monkeypatch,tmp_path):
    from dynamic_composer_desktop import Desktop, derived_frames
    calibration,frames=derived_frames(new_kind="customer")
    desktop=Desktop(monkeypatch,tmp_path,calibration,frames,cleanup_fails=True)
    result=desktop.run()
    assert result["error_code"] == "SEND_DRAFT_CLEANUP_UNCONFIRMED"
    assert desktop.enter_count == 0 and desktop.draft


def test_original_three_frames_through_actual_send_checks(monkeypatch,tmp_path):
    from dynamic_composer_desktop import Desktop
    calibration,paths=incident_inputs()
    frames={key:Image.open(paths[source]).convert("RGB") for key,source in
            (("before","before_input"),("typing","after_input"),("sent","after_cleanup"))}
    desktop=Desktop(monkeypatch,tmp_path,calibration,frames,unknown=True)
    result=desktop.run()
    # No real sent PNG exists. S0/S1 pass, but disappearance of the draft alone
    # MUST NOT fabricate a successful S2 receipt from the unchanged old chat.
    assert desktop.enter_count == 1
    assert result["send_result"]["result"] == "unknown"
    assert result["action_phase"] == "trigger_attempted"


@pytest.mark.parametrize("mutation", ["dpi","window","timepoint","calibration","boundary"])
def test_composer_scope_rejects_stale_or_changed_geometry(monkeypatch,tmp_path,mutation):
    from dynamic_composer_desktop import Desktop, derived_frames
    from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import composer_viewport
    calibration,frames=derived_frames()
    desktop=Desktop(monkeypatch,tmp_path,calibration,frames)
    a,path=desktop.capture(calibration["hwnd"])
    la=sidecar.layout_snapshot_for_image(a)
    fa=sidecar.immutable_frame_pixel_evidence(a,hwnd=calibration["hwnd"],geometry=desktop.geometry,screenshot_path=path)
    desktop.draft="typed"
    b,path=desktop.capture(calibration["hwnd"])
    lb=sidecar.layout_snapshot_for_image(b)
    fb=sidecar.immutable_frame_pixel_evidence(b,hwnd=calibration["hwnd"],geometry=desktop.geometry,screenshot_path=path)
    assert composer_viewport.composer_frame_scope(la,lb,fa,fb)["ok"]
    if mutation=="dpi": lb["dpi_scale"]=1.25
    elif mutation=="window": lb["window_rect"][0]+=10
    elif mutation=="timepoint": fb["frame_id"]=fa["frame_id"]
    elif mutation=="calibration": lb["calibration_id"]="different"
    else: lb["anchors"]=[]
    assert not composer_viewport.composer_frame_scope(la,lb,fa,fb)["ok"]


@pytest.mark.parametrize("case", ["single","native_single","duplicates","weak","changed_tail","missing_tail","new_voice"])
def test_suffix_uses_original_identity_contract(case):
    from dynamic_composer_desktop import worker_imports
    incident_inputs()
    root=Path(os.environ["CHEJIN_COMPOSER_INCIDENT"])
    # Recorded real OCR output is an input to this pure contract test only.
    messages=json.loads((root/"replay_before_input.json").read_text())["messages"]
    old=sidecar.build_message_observations_v3(messages)
    layout={"ok":True,"layout_snapshot_id":"unit","message_viewport_bounds":[300,81,784,700]}
    if case=="duplicates": old=[copy.deepcopy(old[-1]) for _ in range(3)]
    if case=="native_single": old[-1]["native_source_message_id"]="fixture-native-provider-id"
    new=copy.deepcopy(old[-1:] if case in {"single","native_single"} else old[-2:])
    if case=="changed_tail": new[-1]["content_clean"]+="新的内容"
    if case=="missing_tail": new=copy.deepcopy(old[:2])
    if case=="new_voice":
        new.append({"row_kind":"voice_bubble","sender_role":"customer","voice_state":"untranscribed",
                    "message_type":"voice","voice_duration":"5s","bubble_rect":[320,600,400,630]})
    for i,item in enumerate(old):
        item["_worker_stable_id"]=f"historical-unit-{i}";item["_worker_identity_scope"]="committed"
    binder,_=worker_imports()
    expected=binder(sidecar.build_send_context_guard(old,layout_evidence=layout),old,
                    checkpoint={},checkpoint_comparison={},empty_welcome_baseline=False)
    if case=="weak":expected["worker_continuity_contract"]["old_boundary_tokens"]={}
    current=sidecar.build_send_context_guard(new,layout_evidence=layout)
    result=sidecar.validate_send_context_guard(expected,current,current_observations=new,allow_history_suffix=True)
    assert result["ok"] is (case=="native_single"),result
    if case=="single":assert result["reason"]=="single_visible_tail_identity_unproven"
    # The old default is still closed, including single rows with native IDs.
    assert not sidecar.validate_send_context_guard(expected,current,current_observations=new)["ok"]


@pytest.mark.parametrize("scale", [1.25,1.5,2.0])
def test_derived_dpi_registration_uses_current_pixels(monkeypatch,tmp_path,scale):
    calibration,paths=incident_inputs()
    for key in window_layout.REQUIRED_LAYOUT_REGION_NAMES:
        calibration[key]=[round(v*scale) for v in calibration[key]]
    for key in ("image_width","image_height"):
        calibration[key]=round(calibration[key]*scale)
    calibration["window_rect"]=[round(v*scale) for v in calibration["window_rect"]]
    calibration["client_screen_origin"]=[round(v*scale) for v in calibration["client_screen_origin"]]
    calibration["dpi_scale"]=scale
    install_desktop(monkeypatch,tmp_path,calibration)
    bounds=[]
    for label in ("before_input","after_input","after_cleanup"):
        image=Image.open(paths[label]).convert("RGB").resize((calibration["image_width"],calibration["image_height"]))
        layout=register(image,calibration)
        assert layout["valid"],layout
        bounds.append(layout["message_viewport_bounds"][3])
    assert bounds[1] < bounds[0] == bounds[2]


@pytest.mark.parametrize("length",[8,154,1800])
def test_final_focus_checks_full_draft_not_visible_fragment(monkeypatch,tmp_path,length):
    from dynamic_composer_desktop import Desktop,derived_frames,REPLY
    calibration,frames=derived_frames()
    desktop=Desktop(monkeypatch,tmp_path,calibration,frames)
    text=(REPLY*20)[:length]
    # Controlled OS clipboard is the whole edit value even when the PNG shows
    # only a fragment. No production confirmation result is substituted.
    desktop.draft=text
    desktop.capture(calibration["hwnd"])
    result=sidecar.confirm_exact_program_draft_focus(calibration["hwnd"],input_point=(500,750),expected_text=text)
    assert result["ok"] and result["observed_length"]==length
    assert desktop.clipboard=="original clipboard"
    desktop.draft=text[-4:]
    truncated=sidecar.confirm_exact_program_draft_focus(calibration["hwnd"],input_point=(500,750),expected_text=text)
    assert not truncated["ok"] and truncated["selection_released"]


def test_manual_scroll_displacement_does_not_authorize_suffix(monkeypatch,tmp_path):
    from dynamic_composer_desktop import Desktop,derived_frames
    calibration,frames=derived_frames(movement=220,reduction=140)
    desktop=Desktop(monkeypatch,tmp_path,calibration,frames)
    result=desktop.run()
    assert not result["ok"] and desktop.enter_count==0
    assert result["guard"]["visual"]["context_check"]["composer_crop_evidence"]["reason"]=="composer_movement_not_explained_by_shrink"


def test_journal_write_failure_prevents_enter(monkeypatch,tmp_path):
    from dynamic_composer_desktop import Desktop,derived_frames
    calibration,frames=derived_frames()
    desktop=Desktop(monkeypatch,tmp_path,calibration,frames)
    def failed_write(*args,**kwargs): raise OSError("controlled disk write failure")
    monkeypatch.setattr(sidecar,"write_action_phase_journal",failed_write)
    result=desktop.run()
    assert not result["ok"] and desktop.enter_count==0
    assert result["error_code"]=="SEND_ACTION_JOURNAL_WRITE_FAILED"


@pytest.mark.parametrize("reply",["好的，我帮您看看", "您好，市区通勤可以先考虑充电条件。\n方便的话说说每天的通勤距离，我再帮您比较。"])
def test_short_and_multiline_full_send_chain(monkeypatch,tmp_path,reply):
    from dynamic_composer_desktop import Desktop,derived_frames
    # Worker canonicalizes hard newlines before the frozen send contract.
    # Long text still wraps visually; do not relax that existing contract here.
    reply=sidecar.sendinput_safe_text(reply)
    calibration,frames=derived_frames(reply=reply,movement=0,reduction=0)
    desktop=Desktop(monkeypatch,tmp_path,calibration,frames,reply=reply)
    result=desktop.run()
    assert result["ok"] and result["send_result"]["result"]=="sent",result
    assert desktop.enter_count==1
    assert result["send_result"]["visual"]["context_check"]["continuity_relation"]=="business_sequence_equal"


def test_partial_top_pixels_must_come_from_old_frame(monkeypatch,tmp_path):
    from dynamic_composer_desktop import Desktop,derived_frames
    calibration,frames=derived_frames(movement=160)
    # Complete tail is unchanged; change only the clipped bubble's visible
    # pixels. Excluding partial OCR must not hide an unproven top occurrence.
    ImageDraw.Draw(frames["typing"]).rectangle((450,84,457,87),fill=(170,180,190))
    desktop=Desktop(monkeypatch,tmp_path,calibration,frames)
    result=desktop.run()
    assert not result["ok"] and desktop.enter_count==0
    context=result["guard"]["visual"]["context_check"]
    assert context["composer_crop_evidence"]["reason"]=="composer_top_fragment_pixels_changed"
