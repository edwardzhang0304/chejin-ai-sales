"""Unmodified incident PNG -> real OCR/CLI -> Worker probe; Win32 is controlled."""
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest
from PIL import Image

import test_startup_calibration_evidence as startup_evidence
from chejin_worker_client.rpa_bridge import RpaBridge


sidecar = startup_evidence.sidecar
_REAL_OCR = sidecar.win32_ocr_engine.run_ocr_with_cache
_MANIFEST = os.environ.get("CHEJIN_STARTUP_HEADER_REPLAY")
_FRAMES = json.loads(Path(_MANIFEST).read_text())["frames"] if _MANIFEST else []


@pytest.mark.parametrize("frame", _FRAMES, ids=lambda frame: frame["id"])
def test_original_frame_calibrates_through_cli_and_worker(frame, tmp_path):
    path = Path(frame["path"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == frame["sha256"]
    harness = startup_evidence.StartupCalibrationEvidenceTest(
        methodName="test_explicit_artifact_directory_is_honored",
    )
    harness.setUp()
    try:
        harness.image = Image.open(path).convert("RGB")
        width, height = harness.image.size
        with patch.object(sidecar.win32_ocr_engine, "run_ocr_with_cache", _REAL_OCR), patch.object(
            sidecar, "get_window_geometry", return_value={
                "left":12, "top":12, "right":width + 28, "bottom":height + 20,
                "width":width + 16, "height":height + 8,
            }
        ), patch.object(sidecar, "get_window_client_geometry", return_value={
            "width":width, "height":height, "screen_left":20, "screen_top":12,
        }):
            payload = harness.run_startup("--artifact-dir", str(tmp_path))

        bridge = RpaBridge()
        bridge.mode = "real"
        # Only replace transport/OS here; success comes from the real CLI.
        with patch.object(sys, "platform", "win32"), patch.object(
            bridge, "_call_omniauto", return_value=payload,
        ) as calls:
            statuses = bridge.probe()
        report = {"frame":frame, "payload":payload, "worker_status":statuses,
                  "startup_state":bridge._startup_window_normalization_state}
        report_dir = Path(os.environ.get("CHEJIN_STARTUP_HEADER_REPORT", str(tmp_path)))
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / (frame["id"] + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2))

        assert payload["ok"], payload
        calibration = payload["startup_layout_calibration"]
        assert calibration["executable"]
        assert calibration["chat_header_bounds"] == [300, 0, 784, 81]
        assert calibration["message_viewport_bounds"] == [300, 81, 784, 700]
        assert payload["screenshot_call_count"] == payload["ocr_call_count"] == 1
        assert payload["no_clicks_performed"]
        assert statuses == ("ready", "logged_in")
        assert bridge._startup_window_normalization_state == "completed"
        assert calls.call_count == 1
        harness.assert_saved_pair(payload)
    finally:
        harness.doCleanups()
