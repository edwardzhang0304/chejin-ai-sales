"""Retain the architect's two host-baseline counterexamples unchanged.

Optional candidate/replay paths let the same assertions exercise the frozen r5
negative control, r6, and the released control. Private original pixels/layout
are not shipped in the repository. No baseline or expected point is rewritten.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_ROOT = Path(os.environ.get("CHEJIN_HOST_LAYOUT_CANDIDATE_ROOT", str(ROOT)))
REL = "worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/wechat_win32_ocr/window_layout.py"
BASE = "809b343755bcffdc59a9c8845cdc568466089671"
REPLAY = Path(os.environ.get(
    "CHEJIN_HOST_LAYOUT_REPLAY",
    "/private/tmp/chejin-gaolei-incidents7-20260916/replay_before_input.json",
))
if not REPLAY.is_file():
    pytest.skip("requires the original private incident calibration", allow_module_level=True)


def load_module(name, source):
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(source, name, "exec"), module.__dict__)
    return module


released = load_module("released_host_layout", subprocess.check_output(
    ["git", "-C", str(ROOT), "show", f"{BASE}:{REL}"], text=True))
candidate = load_module("submitted_host_layout", (CANDIDATE_ROOT / REL).read_text(encoding="utf-8"))
subject = released if os.environ.get("REVIEW_RELEASED_CONTROL") == "1" else candidate


def test_startup_schema_stays_at_host_baseline():
    assert subject.STARTUP_CALIBRATION_SCHEMA_VERSION == released.STARTUP_CALIBRATION_SCHEMA_VERSION


def test_add_friend_fallback_coordinates_stay_at_host_baseline():
    replay = json.loads(REPLAY.read_text(encoding="utf-8"))
    layout = replay["layout"]
    assert layout["executable"], "The incident must supply an executable calibration"
    expected = released.map_reference_region_point(layout, "plus_entry")["image_point"]
    actual = subject.map_reference_region_point(layout, "plus_entry")["image_point"]
    print(json.dumps({"baseline_point": expected, "subject_point": actual,
                      "baseline_schema": released.STARTUP_CALIBRATION_SCHEMA_VERSION,
                      "subject_schema": subject.STARTUP_CALIBRATION_SCHEMA_VERSION}))
    assert actual == expected
