"""Real Sidecar CLI with only Windows/capture/OCR boundaries supplied by fixtures."""
from pathlib import Path
import json
import os
import sys
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_startup_calibration_evidence import StartupCalibrationEvidenceTest, sidecar, search_ocr


fixture = StartupCalibrationEvidenceTest()
try:
    fixture.setUp()
    # Build the normal persisted map through the real CLI before the runtime probe.
    startup = fixture.run_startup()
    assert startup["ok"], startup
    mode = os.environ.get("PROBE_TEST_MODE", "blank")
    if mode in {"blank", "ocr_error"}:
        fixture.image = Image.new("RGB", (800, 812), "white")

    def ocr(_image):
        if mode == "ocr_error":
            raise RuntimeError("test OCR engine failed")
        return [] if mode == "blank" else search_ocr()

    with patch.object(sidecar, "run_ocr", ocr):
        try:
            result = sidecar.run_sidecar_cli(sys.argv[1:])
        except Exception as exc:
            result = sidecar.exception_payload_for_sidecar(exc, state="win32_ocr_failed")
    print(json.dumps(sidecar.sanitize_sidecar_contract_output(result), ensure_ascii=True))
    raise SystemExit(0 if result.get("ok") else 1)
finally:
    fixture.doCleanups()
