from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
LEGACY_SIDECAR_NAME = "chejin_wechat_add_friend_sidecar.py"
OMNIAUTO_SIDECAR_NAME = "wechat_win32_ocr_sidecar.py"


class LegacySidecarIsolationTest(unittest.TestCase):
    def test_worker_bridge_does_not_reference_legacy_sidecar(self):
        text = (ROOT / "chejin_worker_client" / "rpa_bridge.py").read_text(encoding="utf-8")

        self.assertNotIn(LEGACY_SIDECAR_NAME, text)
        self.assertIn(OMNIAUTO_SIDECAR_NAME, text)
        self.assertIn('OMNIAUTO_ADD_FRIEND_ACTION = "add-friend-entry-click-plan-windows"', text)

    def test_packaging_scripts_do_not_package_legacy_sidecar(self):
        paths = [
            ROOT / "packaging" / "chejin-worker-client.spec",
            ROOT / "scripts" / "build-windows.ps1",
            ROOT / "scripts" / "validate-package.ps1",
        ]

        for path in paths:
            text = path.read_text(encoding="utf-8-sig")
            self.assertNotIn(LEGACY_SIDECAR_NAME, text, f"{path.name} must not package the legacy sidecar")
            self.assertIn(OMNIAUTO_SIDECAR_NAME, text)


if __name__ == "__main__":
    unittest.main()
