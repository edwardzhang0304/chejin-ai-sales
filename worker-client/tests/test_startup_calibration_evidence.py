from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

from PIL import Image, ImageEnhance

CLIENT_ROOT = Path(__file__).resolve().parents[1]
for root in (CLIENT_ROOT, CLIENT_ROOT / "omniauto-rpa"):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
from apps.wechat_ai_customer_service.tests.run_wechat_startup_calibration_v0923_checks import (
    search_ocr,
    shell_image,
)
from chejin_worker_client import incident_evidence, vision_credentials
from chejin_worker_client.rpa_bridge import RpaBridge


class StartupCalibrationEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.active_path = self.root / "wechat_startup_layout_calibration_v0.9.35.json"
        self.image = shell_image()
        self.capture = Mock(side_effect=lambda _rect: self.image.copy())
        self.ocr = Mock(side_effect=lambda *_args, **kwargs: (search_ocr(), kwargs.get("engine")))
        physical = {
            "_WIN32_IMPORT_ERROR": "",
            "STARTUP_CALIBRATION_PATH": self.active_path,
            "_OCR_ENGINE": None,
            "ensure_dpi_awareness_status": lambda: {"per_monitor_aware": True},
            "ensure_visible_wechat_window": lambda **_kwargs: {
                "main_windows": [{"hwnd": 101, "pid": 202}],
                "visible_main_windows": [{"hwnd": 101, "pid": 202}],
                "visible_windows": [{"hwnd": 101, "pid": 202}],
            },
            "select_primary_visible_main_window": lambda _probe: {"hwnd": 101, "pid": 202},
            "activate_window": lambda *_args, **_kwargs: None,
            "normalize_wechat_window": lambda *_args, **_kwargs: {"ok": True, "enabled": True, "applied": True},
            "get_window_client_geometry": lambda _hwnd: {
                "width": self.image.width, "height": self.image.height,
                "screen_left": 20, "screen_top": 50,
            },
            "get_window_geometry": lambda _hwnd: {
                "left": 12, "top": 12, "right": 828, "bottom": 864,
                "width": 816, "height": 852,
            },
            "window_dpi_scale": lambda _hwnd: 1.0,
            "screen_work_area": lambda _hwnd: {"left": 0, "top": 0, "width": 1920, "height": 1080},
            "try_image_grab": self.capture,
            "win32gui": SimpleNamespace(
                IsWindow=lambda hwnd: hwnd == 101,
                GetForegroundWindow=lambda: 101,
                GetAncestor=lambda hwnd, _flag: hwnd,
                GetClassName=lambda _hwnd: "TestWeChat",
            ),
            "win32process": SimpleNamespace(GetWindowThreadProcessId=lambda _hwnd: (1, 202)),
            "_LAYOUT_SNAPSHOT_STORE": sidecar.win32_ocr_layout.LayoutSnapshotStore(),
            "_LATEST_LAYOUT_SNAPSHOT_BY_HWND": {},
            "_LAYOUT_SNAPSHOT_ID_BY_IMAGE_ID": {},
        }
        for name, value in physical.items():
            self.enterContext(patch.object(sidecar, name, value))
        self.enterContext(patch.object(sidecar.win32_ocr_engine, "run_ocr_with_cache", self.ocr))
        self.enterContext(patch.object(
            sidecar.ctypes, "windll", SimpleNamespace(user32=SimpleNamespace(
                IsIconic=lambda _hwnd: 0, IsWindowVisible=lambda _hwnd: True,
            )), create=True,
        ))

    def run_startup(self, *args: str) -> dict:
        return sidecar.run_sidecar_cli(["normalize-window", *args])

    def assert_saved_pair(self, payload: dict) -> tuple[Path, Path]:
        calibration = payload["startup_layout_calibration"]
        raw = Path(calibration["screenshot_path"])
        result = Path(calibration["evidence_path"])
        self.assertEqual(raw.parent, result.parent)
        self.assertEqual(json.loads(result.read_text()), calibration)
        self.assertEqual(json.loads(self.active_path.read_text()), calibration)
        with Image.open(raw) as saved:
            self.assertEqual(saved.mode, self.image.mode)
            self.assertEqual(saved.size, self.image.size)
            self.assertEqual(saved.tobytes(), self.image.tobytes())
        return raw, result

    def test_public_startup_without_artifact_flag_saves_exact_input_and_links_incident(self):
        payload = self.run_startup()
        self.assertTrue(payload["ok"], payload)
        raw, result = self.assert_saved_pair(payload)
        self.assertTrue(raw.is_relative_to(self.root / "artifacts" / "startup_layout_calibration"))
        self.capture.assert_called_once_with((20, 50, 820, 862))
        self.ocr.assert_called_once()
        self.assertTrue(payload["no_clicks_performed"])
        self.assertEqual(payload["screenshot_call_count"], 1)
        self.assertEqual(payload["ocr_call_count"], 1)
        calibration = payload["startup_layout_calibration"]
        self.assertTrue(calibration["vertical_candidates"])
        self.assertTrue(any(abs(c["x"] - 304) <= 4 for c in calibration["vertical_candidates"]))
        enhanced = ImageEnhance.Contrast(self.image.convert("RGB")).enhance(1.35)
        self.assertNotEqual(self.image.tobytes(), enhanced.tobytes())
        self.assertEqual(self.ocr.call_args.args[0].tobytes(), enhanced.tobytes())

        # Continue the real persisted-map -> current-frame -> click-plan path.
        snapshot = sidecar._register_layout_snapshot(
            101, self.image, capture_mode="client_area", screenshot_path="",
            capture_screen_origin=[20, 50],
        )
        target = sidecar.win32_ocr_add_friend_windows.add_friend_plus_entry_target(
            {}, self.image.size, screenshot=self.image, layout_snapshot=snapshot,
        )
        evidence = target["metadata"]["startup_calibration_evidence"]
        self.assertEqual(evidence["screenshot_path"], str(raw))
        self.assertEqual(evidence["result_path"], str(result))
        self.assertEqual(target["metadata"]["calibration_id"], calibration["calibration_id"])
        # Worker filters the Sidecar payload before the incident collector sees
        # it. Exercise that middle step: paths in the plan alone are insufficient.
        bridge = RpaBridge()
        metadata = bridge._evidence_metadata(
            {"before": {"planned_targets": [target]}, "error_code": "PLUS_ENTRY_POPUP_NOT_DETECTED"},
            self.root / "artifacts" / "tasks" / "later-task",
        )
        # Replace only unrelated database/credential reads; use the actual
        # evidence selection, redaction and ZIP writer on the saved files.
        storage_boundary = SimpleNamespace(
            APP_DIR=self.root,
            load_binding=lambda: None,
            read_logs=lambda **_kwargs: [],
            list_c2_outbox_waiting=lambda **_kwargs: [],
            list_reply_send_ack_outbox=lambda **_kwargs: [],
        )
        with (
            patch.object(incident_evidence, "_storage", return_value=storage_boundary),
            patch.object(vision_credentials, "resolve_vision_api_key", return_value=""),
        ):
            files = incident_evidence._evidence_files(incident_evidence._path_candidates(metadata))
            package = incident_evidence._create_incident_package({
                "incident_id": "startup-evidence-test",
                "event": "task_failed",
                "metadata": metadata,
            })
        self.assertIn(raw.resolve(), files)
        self.assertIn(result.resolve(), files)
        with zipfile.ZipFile(package) as archive:
            raw_name = next(name for name in archive.namelist() if name.endswith(raw.name))
            result_name = next(name for name in archive.namelist() if name.endswith("-calibration.json"))
            self.assertEqual(archive.read(raw_name), raw.read_bytes())
            self.assertEqual(json.loads(archive.read(result_name))["calibration_id"], calibration["calibration_id"])

    def test_explicit_artifact_directory_is_honored(self):
        selected = self.root / "selected evidence"
        raw, _ = self.assert_saved_pair(self.run_startup("--artifact-dir", str(selected)))
        self.assertTrue(raw.is_relative_to(selected))

    def test_later_startup_does_not_overwrite_previous_frame_or_result(self):
        first = self.run_startup()
        raw, result = self.assert_saved_pair(first)
        first_bytes = (raw.read_bytes(), result.read_bytes())
        self.image.putpixel((799, 811), (1, 2, 3))
        second = self.run_startup()
        second_raw, second_result = self.assert_saved_pair(second)
        self.assertNotEqual(raw, second_raw)
        self.assertNotEqual(result, second_result)
        self.assertEqual(first_bytes, (raw.read_bytes(), result.read_bytes()))

    def test_unresolved_boundaries_still_keep_raw_image_and_failure_result(self):
        self.image = Image.new("RGB", self.image.size, "white")
        payload = self.run_startup()
        self.assertFalse(payload["ok"])
        self.assert_saved_pair(payload)
        self.assertFalse(payload["startup_layout_calibration"]["executable"])
        self.assertTrue(payload["startup_layout_calibration"]["conflicts"])

    def test_unwritable_destination_does_not_claim_a_saved_calibration(self):
        blocked = self.root / "blocked"
        blocked.write_text("a file, not an evidence directory")
        with self.assertRaises(OSError):
            self.run_startup("--artifact-dir", str(blocked))
        self.ocr.assert_not_called()
        self.assertFalse(self.active_path.exists())

    def test_result_write_failure_preserves_existing_active_calibration(self):
        previous = self.run_startup()
        previous_bytes = self.active_path.read_bytes()
        original = Path.write_text

        def disk_write(path, *args, **kwargs):
            if path.name == "calibration.json.tmp":
                raise OSError("simulated evidence disk full")
            return original(path, *args, **kwargs)

        with patch.object(Path, "write_text", disk_write), self.assertRaises(OSError):
            self.run_startup()
        self.assertEqual(self.active_path.read_bytes(), previous_bytes)
        self.assertTrue(Path(previous["startup_layout_calibration"]["evidence_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
