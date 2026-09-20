"""Production input transaction with a controlled desktop, not Windows UAT.

Typing, copyback, trigger and transaction decisions are real production code.
Only OS calls, captured pixels/OCR and foreground identity are controlled here.
The hint survives Delete and disappears only when text is inserted.
"""
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as s


TEXT = "AIreply12345"
BOUNDS = [310, 610, 760, 750]
LAYOUT = {
    "layout_snapshot_id": "input-lifecycle-frame", "hwnd": 1, "valid": True,
    "input_bounds": BOUNDS, "message_viewport_bounds": [300, 80, 800, 600],
    "toolbar_bounds": [300, 750, 800, 800], "executable": True, "clickable": True,
}


@pytest.fixture
def desktop(monkeypatch):
    state = SimpleNamespace(draft="", hint=False, selected=False, clipboard="original",
                            events=[], captures=[], sent=[], ignore_delete=False,
                            focus=True, drop_character=False, ignore_backspace=False)

    def fresh():
        s._LAYOUT_SNAPSHOT_STORE.put(dict(LAYOUT))
        s._LATEST_LAYOUT_SNAPSHOT_BY_HWND[1] = LAYOUT["layout_snapshot_id"]

    def click(hwnd, x, y, **kwargs):
        _, failure = s._current_click_snapshot(hwnd, expected_snapshot_id=kwargs["expected_snapshot_id"])
        if failure:
            raise RuntimeError(failure["reason"])
        assert s.win32_ocr_layout.point_in_bounds((x, y), kwargs['bounds'])
        state.events.append("click")
        state.selected = False

    def key_event(key, scan, flags, extra):
        if flags:
            return
        if key == ord("A"):
            state.events.append("select_all")
            state.selected = True
        elif key == ord("C"):
            state.events.append("copy")
            if state.selected:
                state.clipboard = state.draft
        elif key in {46, 8}:
            state.events.append("delete" if key == 46 else "backspace")
            ignored = state.ignore_delete if key == 46 else state.ignore_backspace
            if state.selected and not ignored:
                state.draft = ""
            state.selected = False
            # A UI hint is never editable; Delete leaves it visible.
        elif key == 39:
            state.selected = False
        elif key == 13:
            state.events.append("enter")
            state.sent.append(state.draft)
            state.draft = ""
            state.selected = False

    def type_unit(unit):
        char = chr(unit)
        state.events.append("type:" + char)
        if state.selected:
            state.draft = ""
            state.selected = False
        if not (state.drop_character and char == "A"):
            state.draft += char

    def capture(hwnd, **kwargs):
        state.captures.append((kwargs.get("label"), state.draft))
        fresh()
        image = Image.new("RGB", (800, 800), "white")
        ImageDraw.Draw(image).text((330, 630), state.draft or ("voice input hint" if state.hint else ""), fill="black")
        image.info["draft"] = state.draft
        return image, None

    def ocr(image, **kwargs):
        return ([{"text": image.info["draft"], "left": 330, "top": 630,
                  "right": 700, "bottom": 650}], "full")

    fresh()
    monkeypatch.setattr(s, "win32con", SimpleNamespace(VK_CONTROL=17, VK_DELETE=46,
        VK_BACK=8, VK_RIGHT=39, VK_RETURN=13, KEYEVENTF_KEYUP=2))
    monkeypatch.setattr(s, "win32api", SimpleNamespace(keybd_event=key_event))
    monkeypatch.setattr(s, "human_client_click", click)
    monkeypatch.setattr(s, "sendinput_unicode_unit", type_unit)
    monkeypatch.setattr(s, "clipboard_read", lambda: state.clipboard)
    monkeypatch.setattr(s, "clipboard_copy", lambda value: setattr(state, "clipboard", value))
    monkeypatch.setattr(s, "capture_wechat", capture)
    monkeypatch.setattr(s, "layout_snapshot_for_image", lambda image: dict(LAYOUT))
    monkeypatch.setattr(s, "run_ocr_for_input_confirmation", ocr)
    monkeypatch.setattr(s, "send_button_ready_evidence", lambda *a, **kw: {"ok": bool(state.draft)})
    monkeypatch.setattr(s, "recover_send_window_guard", lambda *a, **kw: {"ok": state.focus})
    monkeypatch.setattr(s, "basic_send_window_guard", lambda *a, **kw: {"ok": state.focus})
    monkeypatch.setattr(s, "coordinate_rpa_action", lambda *a, **kw: None)
    monkeypatch.setattr(s, "humanized_action_sleep", lambda *a: None)
    monkeypatch.setattr(s, "humanized_sleep_ms", lambda *a: None)
    monkeypatch.setattr(s.time, "sleep", lambda *a: None)
    monkeypatch.setattr(s.random, "triangular", lambda low, high, mode: mode)
    state.observe_layout = fresh
    return state


def transaction(state, *, bad_probe=False, customer_changed=False):
    visible = bool(state.draft or state.hint)
    seed = {"input_region": {"has_visible_text": visible,
            "reason": "ocr_or_text_shape" if visible else "input_region_blank",
            "bounds": [320, 612, 750, 734], "click_bounds": getattr(state, 'start_bounds', BOUNDS)}}
    if bad_probe:
        seed["input_region"]["error"] = "capture unavailable"
    locator = s.locate_visual_send_input(before_input_region_seed=seed)
    return s.execute_send_transaction(1, TEXT, locator=locator,
        geometry={"width": 800, "height": 800}, before_input_region_seed=seed,
        settings={"enabled": True, "method": "sendinput_unicode", "typo_probability": 0,
                  "typo_max": 0, "chunk_min_chars": 5, "chunk_max_chars": 12},
        before_send_trigger_check=lambda **kw: {"ok": not customer_changed,
            "error_code": "C3_CONTEXT_CHANGED_BEFORE_SEND" if customer_changed else None})


@pytest.mark.parametrize("draft,hint", [("", False), ("", True), ("x", False),
    ("old manual draft", False), ("按住鼠标语音输入文字", False)])
def test_replace_only_when_text_is_detected_without_post_delete_empty_check(desktop, draft, hint):
    desktop.draft, desktop.hint = draft, hint
    result = transaction(desktop)
    assert result["ok"], result
    assert desktop.sent == [TEXT]
    assert desktop.events.count("delete") == int(bool(draft or hint))
    assert desktop.events.count("enter") == 1
    # There is no intermediate capture/OCR between Delete and text entry.
    assert desktop.captures == [("send_input_probe_1", TEXT)]
    first_type = desktop.events.index("type:A")
    if draft or hint:
        assert desktop.events[:first_type] == ["click", "select_all", "delete"]
    else:
        assert desktop.events[:first_type] == ["click"]
    assert desktop.events.index("copy") > first_type
    assert desktop.clipboard == "original"


@pytest.mark.parametrize("failure", ["delete_did_not_clear", "typed_wrong", "focus_lost", "bad_probe", "customer_changed"])
def test_existing_guards_prevent_enter_after_input_failure(desktop, failure):
    desktop.draft = "old manual draft"
    desktop.ignore_delete = failure == "delete_did_not_clear"
    desktop.drop_character = failure == "typed_wrong"
    desktop.focus = failure != "focus_lost"
    result = transaction(desktop, bad_probe=failure == "bad_probe", customer_changed=failure == "customer_changed")
    assert not result["ok"], result
    assert desktop.sent == []
    assert "enter" not in desktop.events
    if failure in {"focus_lost", "bad_probe"}:
        assert desktop.events == []
    if failure in {"delete_did_not_clear", "typed_wrong"}:
        assert "focused_input_draft_mismatch" in str(result), result
        assert "copy" in desktop.events
    if failure == "customer_changed":
        assert result["context_check"]["error_code"] == "C3_CONTEXT_CHANGED_BEFORE_SEND", result
        assert result["draft_clear"]["clear_attempted"] is True, result


@pytest.mark.parametrize("hint,remaining", [(False, False), (True, False), (False, True)])
def test_cancel_clears_once_without_probe_and_next_reply_replaces_remaining(desktop, hint, remaining):
    desktop.hint = hint
    desktop.ignore_backspace = remaining
    result = transaction(desktop, customer_changed=True)
    assert result["error_code"] == "C3_CONTEXT_CHANGED_BEFORE_SEND", result
    assert result["draft_clear"]["ok"] is True
    assert result["draft_clear"]["clear_attempted"] is True
    assert result["draft_clear"]["cleared"] is False  # No empty-field claim.
    assert desktop.sent == []
    assert desktop.events.count("backspace") == 1
    assert desktop.captures == [("send_input_probe_1", TEXT)]
    assert desktop.draft == (TEXT if remaining else "")
    # A separate reply enters with a new baseline observation, as send_payload
    # does in production. Do not reuse the keyboard-invalidated old layout.
    desktop.observe_layout()
    followup = transaction(desktop)
    assert followup["ok"], followup
    assert desktop.sent == [TEXT]


def test_long_old_draft_can_shrink_before_final_copyback(desktop):
    desktop.draft = "old draft\n" * 50
    desktop.start_bounds = [310, 200, 760, 750]
    result = transaction(desktop)
    assert result['ok'], result
    assert desktop.sent == [TEXT]
    assert desktop.events.count('delete') == 1
    assert desktop.captures == [('send_input_probe_1', TEXT)]
