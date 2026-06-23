from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PackagingScriptsTest(unittest.TestCase):
    def test_powershell_param_block_precedes_executable_statements(self):
        scripts = [
            ROOT / "scripts" / "build-windows.ps1",
            ROOT / "scripts" / "collect-wechat-diagnostics.ps1",
            ROOT / "scripts" / "run-preflight.ps1",
            ROOT / "scripts" / "validate-package.ps1",
        ]

        for script in scripts:
            text = script.read_text(encoding="utf-8-sig").lstrip()
            if "param(" in text:
                self.assertTrue(text.startswith("param("), f"{script.name} 的 param 块必须在脚本最前面")

    def test_build_script_writes_manifest_and_checks_sidecar(self):
        text = (ROOT / "scripts" / "build-windows.ps1").read_text(encoding="utf-8-sig")

        self.assertIn("车金Worker客户端.manifest.json", text)
        self.assertIn("Get-FileHash -Algorithm SHA256", text)
        self.assertIn("omniauto-add-friend-rpa-pr-candidate-20260618.zip", text)
        self.assertIn("omniauto-rpa\\apps\\wechat_ai_customer_service\\adapters\\wechat_win32_ocr_sidecar.py", text)
        self.assertIn("python.exe run_checks.py", text)


if __name__ == "__main__":
    unittest.main()
