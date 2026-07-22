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
        self.assertIn('$OmniAutoSourcePath = Join-Path $Root "omniauto-rpa"', text)
        self.assertIn("omniauto_upstream_commit", text)
        self.assertIn("omniauto_source_sidecar_sha256", text)
        self.assertIn("packaged_sidecar_sha256", text)
        self.assertNotIn("omniauto-add-friend-rpa-pr-candidate-20260618.zip", text)
        self.assertNotIn("Expand-Archive", text)
        self.assertIn("omniauto-rpa\\apps\\wechat_ai_customer_service\\adapters\\wechat_win32_ocr_sidecar.py", text)
        self.assertIn("python.exe run_checks.py", text)

    def test_source_package_script_excludes_local_env_and_runtime_state(self):
        text = (ROOT / "scripts" / "build-source-package.py").read_text(encoding="utf-8")

        self.assertIn("*.local.env", text)
        self.assertIn('".env"', text)
        self.assertNotIn("endswith(\".example.env\")", text)
        self.assertIn('"runtime"', text)
        self.assertIn('"cache"', text)
        self.assertIn('"forbidden_entries"', text)

    def test_run_checks_includes_omniauto_safety_suites(self):
        text = (ROOT / "run_checks.py").read_text(encoding="utf-8")

        required_scripts = (
            "run_wechat_win32_ocr_interaction_evidence_checks.py",
            "run_wechat_win32_ocr_humanized_input_checks.py",
            "run_wechat_win32_ocr_window_action_planning_checks.py",
        )
        for script_name in required_scripts:
            self.assertIn(script_name, text)


if __name__ == "__main__":
    unittest.main()
