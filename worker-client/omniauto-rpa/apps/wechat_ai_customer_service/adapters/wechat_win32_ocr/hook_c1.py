"""Hook WeChat 3.9 C1 startup and current-frame search recognition.

This preparation does not execute the legacy plus-menu flow or invite a user.
Only the original startup normalizer owns movement/resizing. Search/lookup,
form and cleanup actions require later frame-specific evidence.
"""
from __future__ import annotations

from typing import Any

from . import window_layout

STARTUP_PROFILE = "hook-3.9-c1"


def main_search_target(image: Any, ocr_items: list[dict], *,
                       full_client_width: int | None = None) -> dict:
    """Locate the Search label inside pixel-derived sidebar/header bounds."""
    anchors = [item for item in ocr_items
               if "".join(str(item.get("text") or "").split()) == "搜索"]
    resolved = window_layout.build_add_friend_entry_layout_regions(
        image, search_anchor_items=anchors, full_client_width=full_client_width,
    )
    if not resolved.get("ok"):
        return {"ok": False, "reason": "hook_c1_main_search_unresolved",
                "layout": resolved}
    header = resolved['regions']['sidebar_header_bounds']
    candidates = []
    for item in anchors:
        bounds = window_layout.normalize_rect(
            [item.get('left'), item.get('top'), item.get('right'), item.get('bottom')]
        )
        x, y = (bounds[0]+bounds[2])//2, (bounds[1]+bounds[3])//2
        if (float(item.get('confidence') or 0) >= .70
                and bounds[2] > bounds[0] and bounds[3] > bounds[1]
                and bounds[0] >= header[0] and bounds[2] <= header[2]
                and bounds[1] >= header[1] and bounds[3] <= header[3]):
            candidates.append({'point': [x, y], 'bounds': bounds})
    if len(candidates) != 1:
        return {'ok': False, 'reason': 'hook_c1_main_search_ambiguous',
                'layout': resolved}
    return {'ok': True, 'reason': 'hook_c1_main_search_located',
            **candidates[0], 'layout': resolved,
            'click_executed': False, 'source': 'current_frame_search_label_and_pixel_boundaries'}


def main_window_shadow_bounds_match(main: dict, shadow: dict, *, dpi_scale: float) -> bool:
    """An owned shadow may surround only the current main frame, within 32 DIP."""
    import math
    if (type(dpi_scale) not in {int, float} or not math.isfinite(dpi_scale)
            or not .5 <= dpi_scale <= 4):
        return False
    try:
        margins = (main['left']-shadow['left'], main['top']-shadow['top'],
                   shadow['right']-main['right'], shadow['bottom']-main['bottom'])
        return (main['width'] > 0 and main['height'] > 0
                and all(type(value) in {int, float} and math.isfinite(value)
                        and 0 <= value <= 32*dpi_scale for value in margins))
    except (KeyError, TypeError):
        return False


def classify_visible_other_windows(ops: Any, hwnd: int, process_id: int) -> dict:
    gui = ops.win32gui
    blockers = []
    ignored_shadows = []
    main_geometry = ops.get_window_geometry(hwnd)
    def collect(other, _):
        if (other != hwnd and gui.IsWindowVisible(other)
                and ops.win32process.GetWindowThreadProcessId(other)[1] == process_id):
            geometry = ops.get_window_geometry(other)
            if geometry.get('width', 0) < 160 or geometry.get('height', 0) < 80:
                return
            class_name = gui.GetClassName(other)
            owner_is_main = gui.GetWindow(other, 4) == hwnd  # GW_OWNER
            metadata = {'class': class_name, 'geometry': geometry,
                        'owner_is_main': owner_is_main,
                        'foreground': gui.GetForegroundWindow() == other}
            # WeChat 3.9 exposes its visual frame shadow as a visible top-level
            # window. Exclude only a non-foreground shadow directly owned by
            # this main HWND and closely enclosing its actual current rectangle.
            if (class_name == 'popupshadow' and owner_is_main
                    and not metadata['foreground']
                    and main_window_shadow_bounds_match(
                        main_geometry, geometry, dpi_scale=ops.window_dpi_scale(hwnd))):
                ignored_shadows.append(metadata)
            else:
                blockers.append(metadata)
    gui.EnumWindows(collect, None)
    return {'ignored_main_window_shadows': ignored_shadows, 'blocking_window_details': blockers}


def prepare_window(ops: Any, hwnd: int, process_id: int, *, artifact_dir: str,
                   capture_evidence: bool = True) -> dict:
    """One Hook startup geometry preparation, then visible capture evidence.

The caller holds the original Worker action lease and verifies account/
process before and after this call. Missing OCR leaves geometry evidence only.
"""
    result = {'ok': False, 'startup_profile': STARTUP_PROFILE,
              'status': 'stopped', 'business_ready': False,
              'invitation_sent': False, 'input_actions': 0, 'click_actions': 0}
    gui = ops.win32gui
    if (not gui.IsWindowVisible(hwnd) or gui.IsIconic(hwnd)
            or gui.GetClassName(hwnd) != 'WeChatMainWndForPC'
            or ops.win32process.GetWindowThreadProcessId(hwnd)[1] != process_id):
        result['reason'] = 'hook_c1_window_identity_unavailable'
        return result
    inventory = classify_visible_other_windows(ops, hwnd, process_id)
    result['ignored_main_window_shadows'] = inventory['ignored_main_window_shadows']
    if inventory['blocking_window_details']:
        result.update(reason='hook_c1_close_wechat_popup_first',
                      blocking_windows=len(inventory['blocking_window_details']), **inventory)
        return result
    awareness = ops.ensure_dpi_awareness_status()
    if not awareness.get('per_monitor_aware'):
        result.update(reason='hook_c1_dpi_awareness_unverified', dpi_awareness=awareness)
        return result
    ops.activate_window(hwnd, foreground_only=True)
    if gui.GetForegroundWindow() != hwnd:
        result['reason'] = 'hook_c1_foreground_unconfirmed'
        return result
    normalized = ops.normalize_wechat_window(hwnd, allow_move=True, startup_profile=STARTUP_PROFILE)
    result['window_normalization'] = normalized
    if not normalized.get('ok'):
        result['reason'] = str(normalized.get('reason') or 'hook_c1_normalization_failed')
        return result
    ops.humanized_action_sleep(210, 330)
    if gui.GetForegroundWindow() != hwnd:
        result['reason'] = 'hook_c1_foreground_changed_before_capture'
        return result
    if not capture_evidence:
        # Startup needs verified geometry only. C1 takes its own current ROI
        # when a real task begins; no screenshot/OCR is repeated here.
        if (ops.get_window_geometry(hwnd) != normalized.get('after')
                or abs(ops.window_dpi_scale(hwnd)-normalized.get('after_dpi_scale', 0)) > .01
                or ops.win32process.GetWindowThreadProcessId(hwnd)[1] != process_id):
            result['reason'] = 'hook_c1_geometry_changed_after_normalization'
            return result
        result.update(ok=True,status='window_geometry_verified',
                      reason='hook_c1_window_geometry_verified',
                      screenshot_call_count=0,ocr_call_count=0)
        return result
    image, path = ops.capture_wechat(hwnd, artifact_dir=artifact_dir, label='hook_c1_window')
    client = ops.get_window_client_geometry(hwnd)
    if (tuple(image.size) != (client.get('width'), client.get('height'))
            or ops.get_window_geometry(hwnd) != normalized.get('after')
            or abs(ops.window_dpi_scale(hwnd)-normalized.get('after_dpi_scale', 0)) > .01
            or ops.win32process.GetWindowThreadProcessId(hwnd)[1] != process_id):
        result['reason'] = 'hook_c1_geometry_changed_during_capture'
        return result
    result.update(ok=True, status='window_geometry_verified_ocr_pending',
                  reason='hook_c1_window_geometry_verified', client_geometry=client,
                  dpi_awareness=awareness, screenshot_size=list(image.size), screenshot_path=path,
                  screenshot_call_count=1, ocr_call_count=0)
    if not getattr(ops, '_OCR_IMPORT_ERROR', 'OCR unavailable'):
        items = ops.run_ocr(image)
        located = main_search_target(image, items)
        result.update(ocr_call_count=1, search_target=located)
        if located['ok']:
            result.update(status='window_and_search_recognition_verified',
                          reason='hook_c1_main_search_located')
        else:
            result.update(status='window_geometry_verified_search_unresolved',
                          reason=located['reason'])
    return result


def inspect_main_search_roi(ops: Any, hwnd: int, process_id: int, *, screenshot_path: str) -> dict:
    """Capture one top-left client ROI and recognize it once, without UI input.

    The caller verifies account and process and holds the original action
    lease. Capture bounds select evidence only; click bounds come from OCR
    and pixel edges. This step never normalizes the accepted window again.
    """
    from pathlib import Path
    import math
    result = {'ok': False, 'status': 'stopped', 'business_ready': False,
              'click_actions': 0, 'input_actions': 0, 'invitation_sent': False,
              'screenshot_call_count': 0, 'ocr_call_count': 0,
              'capture_policy': 'one_top_left_client_roi_no_full_window_or_screen'}
    gui = ops.win32gui
    if (not gui.IsWindowVisible(hwnd) or gui.IsIconic(hwnd)
            or gui.GetClassName(hwnd) != 'WeChatMainWndForPC'
            or ops.win32process.GetWindowThreadProcessId(hwnd)[1] != process_id):
        result['reason'] = 'hook_c1_window_identity_unavailable'
        return result
    awareness = ops.ensure_dpi_awareness_status()
    if not awareness.get('per_monitor_aware'):
        result.update(reason='hook_c1_dpi_awareness_unverified', dpi_awareness=awareness)
        return result
    inventory = classify_visible_other_windows(ops, hwnd, process_id)
    result.update(inventory)
    if inventory['blocking_window_details']:
        result['reason'] = 'hook_c1_close_wechat_popup_first'
        return result
    if getattr(ops, '_OCR_IMPORT_ERROR', 'OCR unavailable'):
        result['reason'] = 'hook_c1_ocr_engine_unavailable'
        return result
    geometry = ops.get_window_geometry(hwnd)
    client = ops.get_window_client_geometry(hwnd)
    dpi = ops.window_dpi_scale(hwnd)
    if (abs(geometry.get('width', 0)-1060) > 6
            or abs(geometry.get('height', 0)-740) > 6
            or client.get('width', 0) < 700 or client.get('height', 0) < 600
            or not math.isfinite(dpi) or not .5 <= dpi <= 4):
        result['reason'] = 'hook_c1_accepted_window_geometry_changed'
        return result
    ops.activate_window(hwnd, foreground_only=True)
    if gui.GetForegroundWindow() != hwnd:
        result['reason'] = 'hook_c1_foreground_unconfirmed'
        return result
    # Keep only the header operation area. Its width includes both pixel
    # separators, and its height is bounded to avoid the message viewport.
    roi_width = min(client['width'], math.ceil(client['width']*.62))
    roi_height = min(client['height'], math.ceil(64*dpi))
    origin_x, origin_y = client['screen_left'], client['screen_top']
    bbox = (origin_x, origin_y, origin_x+roi_width, origin_y+roi_height)
    result.update(window_geometry=geometry, client_geometry=client, dpi_scale=dpi,
                  capture_client_bounds=[0, 0, roi_width, roi_height],
                  capture_screen_bounds=list(bbox), dpi_awareness=awareness)
    path = Path(screenshot_path)
    if path.exists():
        result['reason'] = 'hook_c1_search_roi_evidence_already_exists'
        return result
    result['screenshot_call_count'] = 1
    image = ops.try_image_grab(bbox)
    if image is None or tuple(image.size) != (roi_width, roi_height):
        result['reason'] = 'hook_c1_exact_search_roi_capture_unavailable'
        return result
    image.save(path)
    result.update(screenshot_path=str(path), screenshot_size=list(image.size))
    def frame_still_current():
        return (gui.GetForegroundWindow() == hwnd
                and ops.get_window_geometry(hwnd) == geometry
                and ops.get_window_client_geometry(hwnd) == client
                and abs(ops.window_dpi_scale(hwnd)-dpi) <= .01
                and ops.win32process.GetWindowThreadProcessId(hwnd)[1] == process_id)
    if not frame_still_current():
        result['reason'] = 'hook_c1_frame_changed_after_roi_capture'
        return result
    result['ocr_call_count'] = 1
    items = ops.run_ocr(image)
    result['ocr_items'] = ops.compact_ocr_items_for_report(items, limit=20)
    located = main_search_target(image, items, full_client_width=client['width'])
    result['search_target'] = located
    if not frame_still_current():
        result['reason'] = 'hook_c1_frame_changed_during_roi_ocr'
        return result
    if not located.get('ok'):
        result.update(status='search_roi_captured_recognition_unresolved', reason=located['reason'])
        return result
    result.update(ok=True, status='search_roi_recognition_verified',
                  reason='hook_c1_main_search_located',
                  target_screen_point=[origin_x+located['point'][0], origin_y+located['point'][1]])
    return result
