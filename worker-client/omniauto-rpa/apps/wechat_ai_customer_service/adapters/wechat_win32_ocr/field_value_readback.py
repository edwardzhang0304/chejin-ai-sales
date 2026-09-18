"""Read the actual invite edit controls; never accept an expected string.

Clipboard fallback is intentionally unavailable for formats we cannot restore
losslessly. The caller recaptures the form after this bounded read-only pass.
"""
from __future__ import annotations

import time
from typing import Any


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason,
            "identity_changed": reason in {"field_or_foreground_changed", "field_value_changed",
                "field_focus_changed", "field_focus_unconfirmed", "field_window_changed"}}


def _identity(control: Any, ops: Any) -> tuple:
    rect = ops.uia_rect_to_dict(control.BoundingRectangle)
    return (int(control.ProcessId), tuple(control.GetRuntimeId()),
            tuple(rect[key] for key in ("left", "top", "right", "bottom")))


def _pattern_value(control: Any) -> tuple[str, str] | None:
    for method in ("GetValuePattern", "GetTextPattern"):
        try:
            pattern = getattr(control, method)()
            value = pattern.Value if method == "GetValuePattern" else pattern.DocumentRange.GetText(-1)
            if isinstance(value, str) and len(value) <= 4096:
                return value, "uia_value" if method == "GetValuePattern" else "uia_text"
        except Exception:
            continue
    return None


class _TextClipboard:
    """Atomic snapshots/restores of supported text formats, no data in logs."""
    def __init__(self, clipboard: Any, gui: Any):
        self.clipboard, self.gui = clipboard, gui

    def snapshot(self) -> tuple[int, list[tuple[int, Any]]]:
        cb = self.clipboard
        cb.OpenClipboard()
        try:
            formats, fmt = [], cb.EnumClipboardFormats(0)
            while fmt:
                # CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_LOCALE only. Images,
                # HTML, files or owner-display formats are left untouched.
                if fmt not in {1, 7, 13, 16}:
                    raise ValueError("clipboard_format_not_restorable")
                formats.append((fmt, cb.GetClipboardData(fmt)))
                fmt = cb.EnumClipboardFormats(fmt)
            return int(cb.GetClipboardSequenceNumber()), formats
        finally:
            cb.CloseClipboard()

    def current(self) -> tuple[int, int, str | None]:
        cb = self.clipboard
        cb.OpenClipboard()
        try:
            # Keep ownership/sequence even when reading the copied value fails,
            # so our own unsuccessful copy can still restore the prior data.
            try:
                value = cb.GetClipboardData(13)
            except Exception:
                value = None
            if not isinstance(value, str) or len(value) > 4096:
                value = None
            return int(cb.GetClipboardSequenceNumber()), int(cb.GetClipboardOwner() or 0), value
        finally:
            cb.CloseClipboard()

    def restore(self, expected_sequence: int, formats: list[tuple[int, Any]]) -> str:
        cb = self.clipboard
        # Our own hidden native window gives EmptyClipboard a valid owner.
        owner = self.gui.CreateWindowEx(0, "STATIC", "", 0, 0, 0, 0, 0, 0, 0, 0, None)
        try:
            cb.OpenClipboard(owner)
            try:
                if int(cb.GetClipboardSequenceNumber()) != expected_sequence:
                    return "third_party_change_preserved"
                cb.EmptyClipboard()
                for fmt, data in formats:
                    cb.SetClipboardData(fmt, data)
                return "restored"
            finally:
                cb.CloseClipboard()
        finally:
            self.gui.DestroyWindow(owner)


def _read_control(control: Any, *, identity: tuple, guard: Any, auto: Any,
                  clipboard: _TextClipboard, ops: Any, pid: int) -> dict[str, Any]:
    def same_field(*, focused: bool = False) -> bool:
        if not guard() or _identity(control, ops) != identity:
            return False
        return not focused or _identity(auto.GetFocusedControl(), ops) == identity

    if not same_field():
        return _unavailable("field_or_foreground_changed")
    value = _pattern_value(control)
    if value is not None:
        # Read twice to reject a field changed while obtaining its value.
        repeated = _pattern_value(control)
        if not same_field() or repeated != value:
            return _unavailable("field_value_changed")
        return {"available": True, "value": value[0], "method": value[1],
                "field_identity": list(identity), "source_verified": True}

    copied_sequence = 0
    result = _unavailable("clipboard_copy_unconfirmed")
    try:
        before, saved = clipboard.snapshot()
        if before <= 0 or not same_field():
            return _unavailable("clipboard_or_field_unavailable")
        control.SetFocus()
        if not same_field(focused=True):
            return _unavailable("field_focus_unconfirmed")
        ops.hotkey(ops.win32con.VK_CONTROL, ord("A"))
        if not same_field(focused=True):
            return _unavailable("field_focus_changed")
        ops.hotkey(ops.win32con.VK_CONTROL, ord("C"))
        deadline = time.monotonic() + .35
        while time.monotonic() < deadline:
            sequence, owner, text = clipboard.current()
            if sequence != before and sequence > 0:
                if not owner or ops.win32process.GetWindowThreadProcessId(owner)[1] != pid:
                    return _unavailable("clipboard_owner_changed")
                copied_sequence = sequence
                if not same_field(focused=True):
                    result = _unavailable("field_focus_changed")
                    break
                if text is None:
                    result = _unavailable("clipboard_text_unavailable")
                    break
                result = {"available": True, "value": text, "method": "clipboard_copy",
                          "field_identity": list(identity), "source_verified": True,
                          "clipboard_sequence_before": before,
                          "clipboard_sequence_after": sequence}
                break
            if not same_field(focused=True):
                result = _unavailable("field_focus_changed")
                break
            time.sleep(.01)
    except Exception as exc:
        result = _unavailable(type(exc).__name__)
    finally:
        if copied_sequence:
            try:
                result["clipboard_restore"] = clipboard.restore(copied_sequence, saved)
            except Exception as exc:
                result = _unavailable("clipboard_restore_" + type(exc).__name__)
    return result


def read_invite_fields(hwnd: int, targets: dict[str, dict], *, ops: Any) -> dict[str, dict]:
    """One pass under the existing UI lease; no input/delete/paste/send keys."""
    if not targets:
        return {}
    try:
        import uiautomation as auto
        import win32clipboard

        snapshot_id = str(next(iter(targets.values())).get("layout_snapshot_id") or "")
        if any(str(target.get("layout_snapshot_id") or "") != snapshot_id for target in targets.values()):
            raise ValueError("field_snapshot_mismatch")
        snapshot, error = ops._current_click_snapshot(hwnd, expected_snapshot_id=snapshot_id)
        if error or not snapshot:
            raise ValueError("field_snapshot_unavailable")
        rect = tuple(snapshot["window_rect"])
        origin = snapshot["capture_screen_origin"]
        pid = int(ops.win32process.GetWindowThreadProcessId(hwnd)[1])

        def guard() -> bool:
            return bool(ops.foreground_window_matches_target(hwnd).get("ok")
                        and tuple(ops.win32gui.GetWindowRect(hwnd)) == rect
                        and ops.win32process.GetWindowThreadProcessId(hwnd)[1] == pid)

        if not guard():
            raise ValueError("field_window_changed")
        controls = ops.collect_uia_controls(auto.ControlFromHandle(hwnd), max_depth=8, max_count=900)
        selected, results = {}, {}
        for name, target in targets.items():
            bounds = list(target.get("bounds") or [])
            if len(bounds) != 4:
                results[name] = _unavailable("field_bounds_missing")
                continue
            screen = [bounds[0] + origin[0], bounds[1] + origin[1],
                      bounds[2] + origin[0], bounds[3] + origin[1]]
            candidates = []
            for control in controls:
                if "edit" not in str(ops.safe_uia_attr(control, "ControlTypeName")).lower():
                    continue
                identity = _identity(control, ops)
                if (identity[0] == pid and identity[1]
                        and ops._screen_rect_inside_bounds(ops.uia_rect_to_dict(control.BoundingRectangle), screen)):
                    candidates.append((control, identity))
            if len(candidates) == 1:
                selected[name] = candidates[0]
            else:
                results[name] = _unavailable("field_control_not_unique")
        clipboard = _TextClipboard(win32clipboard, ops.win32gui)
        for name, (control, identity) in selected.items():
            results[name] = _read_control(control, identity=identity, guard=guard, auto=auto,
                                         clipboard=clipboard, ops=ops, pid=pid)
            results[name].update(hwnd=hwnd, window_rect=list(rect), process_id=pid,
                                 layout_source_id=snapshot_id,
                                 field_bounds=list(targets[name].get("bounds") or []))
            if results[name].get("method") not in {"uia_value", "uia_text"}:
                results[name]["requires_fresh_layout"] = True
        return results
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {
            "field_snapshot_mismatch", "field_snapshot_unavailable", "field_window_changed",
        } else type(exc).__name__
        return {name: _unavailable(reason) for name in targets}
