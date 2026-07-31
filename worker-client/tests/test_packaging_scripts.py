from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_policy import BuildPolicyError, validate_build_policy
from omniauto_tree import (
    load_source_provenance,
    tree_manifest,
    verify_same_tree,
)


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
        self.assertIn("omniauto_upstream_base_commit", text)
        self.assertIn("omniauto_selective_integrations", text)
        self.assertIn("omniauto_chejin_integration_commit", text)
        self.assertIn("omniauto_source_sidecar_sha256", text)
        self.assertIn("packaged_sidecar_sha256", text)
        self.assertIn("generated_observation_schema_sha256", text)
        self.assertIn("packaged_generated_observation_schema_sha256", text)
        self.assertIn("c2_contract_sha256", text)
        self.assertIn("git_commit", text)
        self.assertIn("tests_status", text)
        self.assertNotIn("omniauto-add-friend-rpa-pr-candidate-20260618.zip", text)
        self.assertNotIn("Expand-Archive", text)
        self.assertIn("omniauto-rpa\\apps\\wechat_ai_customer_service\\adapters\\wechat_win32_ocr_sidecar.py", text)
        self.assertIn("python.exe run_checks.py", text)
        self.assertIn("DevelopmentBuild", text)
        self.assertIn("formal_release", text)
        self.assertIn("omniauto_source_tree_sha256", text)
        self.assertIn("packaged_omniauto_tree_sha256", text)
        self.assertIn("import uiautomation", text)
        self.assertIn("pyi-archive_viewer.exe -l -r", text)
        self.assertIn("最终 exe 未包含 Windows UIA 诊断所需的 uiautomation", text)
        self.assertIn('"--omniauto-sidecar", "--help"', text)
        self.assertIn("最终 exe 无法启动内置 OmniAuto sidecar", text)
        self.assertNotIn('$OmniAutoUpstreamCommit = "855c218', text)

    def test_source_package_script_excludes_local_env_and_runtime_state(self):
        text = (ROOT / "scripts" / "build-source-package.py").read_text(encoding="utf-8")

        self.assertIn("*.local.env", text)
        self.assertIn('".env"', text)
        self.assertNotIn("endswith(\".example.env\")", text)
        self.assertIn('"runtime"', text)
        self.assertIn('"cache"', text)
        self.assertIn('"forbidden_entries"', text)
        self.assertIn("ALLOWED_DATA_PREFIXES", text)
        self.assertIn('"omniauto_tree_sha256"', text)
        self.assertIn('"omniauto_upstream_base_commit"', text)
        self.assertIn('"omniauto_selective_integrations"', text)
        self.assertIn('"omniauto_chejin_integration_commit"', text)
        self.assertIn('"generated_observation_schema_sha256"', text)
        self.assertIn('"canonical_contract_sha256"', text)
        self.assertIn('"contract_file_check": "passed"', text)
        self.assertIn("C2_CONTRACT_FILE_MISMATCH", text)
        self.assertIn("_zip_member_sha256", text)
        self.assertIn("--development-build", text)
        self.assertIn("preflight_status", text)

    def test_official_build_policy_rejects_dirty_and_skipped_checks(self):
        with self.assertRaises(BuildPolicyError):
            validate_build_policy(
                git_dirty=True,
                skip_tests=False,
                skip_preflight=False,
                development_build=False,
            )
        with self.assertRaises(BuildPolicyError):
            validate_build_policy(
                git_dirty=False,
                skip_tests=True,
                skip_preflight=False,
                development_build=False,
            )
        with self.assertRaises(BuildPolicyError):
            validate_build_policy(
                git_dirty=False,
                skip_tests=False,
                skip_preflight=True,
                development_build=False,
            )
        policy = validate_build_policy(
            git_dirty=True,
            skip_tests=True,
            skip_preflight=True,
            development_build=True,
        )
        self.assertFalse(policy.formal_release)

    def test_omniauto_tree_verification_covers_every_packaged_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            packaged = root / "packaged"
            for base in (source, packaged):
                (base / "apps").mkdir(parents=True)
                (base / ".chejin-source.json").write_text(
                    (
                        '{"schema_version":2,'
                        '"upstream_base_commit":'
                        '"855c21881641cdb2f9fe69d3f2e1caa05e37d04d",'
                        '"selective_integrations":[{'
                        '"source_commit":'
                        '"2318bd8c5aa8d8ff2272a8decc285ef2ae9e01e7",'
                        '"scope":["visual_fingerprint"]}],'
                        '"chejin_integration_commit":'
                        '"ff9e0de00013ac51a2f2a05e3774748c43c846fb"}'
                    ),
                    encoding="utf-8",
                )
                (base / "apps" / "one.py").write_text("one", encoding="utf-8")

            verified = verify_same_tree(source, packaged)
            self.assertEqual(
                verified["source"]["tree_sha256"],
                verified["packaged"]["tree_sha256"],
            )
            (packaged / "apps" / "two.py").write_text("two", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "OMNIAUTO_TREE_MISMATCH"):
                verify_same_tree(source, packaged)

    def test_omniauto_provenance_requires_base_selective_and_chejin_commits(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_path = ROOT / "omniauto-rpa" / ".chejin-source.json"
            (root / ".chejin-source.json").write_bytes(
                source_path.read_bytes()
            )
            provenance = load_source_provenance(root)

        self.assertEqual(
            provenance["upstream_base_commit"],
            "855c21881641cdb2f9fe69d3f2e1caa05e37d04d",
        )
        self.assertEqual(
            provenance["selective_integrations"][0]["source_commit"],
            "2318bd8c5aa8d8ff2272a8decc285ef2ae9e01e7",
        )
        self.assertEqual(
            provenance["chejin_integration_commit"],
            "ff9e0de00013ac51a2f2a05e3774748c43c846fb",
        )

    def test_pyinstaller_spec_packages_contract_and_filters_omniauto_runtime_data(self):
        text = (ROOT / "packaging" / "chejin-worker-client.spec").read_text(encoding="utf-8")

        self.assertIn('CONTRACT_PATH = ROOT.parent / "contracts" / "c2_contract_v3.json"', text)
        self.assertIn('(str(CONTRACT_PATH), "contracts")', text)
        self.assertIn("EXCLUDED_OMNIAUTO_PARTS", text)
        self.assertIn("ALLOWED_OMNIAUTO_DATA_PREFIXES", text)
        self.assertIn('"uiautomation"', text)
        self.assertNotIn('(str(OMNIAUTO_RPA_SOURCE), "omniauto-rpa")', text)

    def test_windows_requirements_pin_uiautomation_for_diagnostics(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn(
            'uiautomation==2.0.29; platform_system == "Windows"',
            text,
        )

    def test_run_checks_includes_omniauto_safety_suites(self):
        text = (ROOT / "run_checks.py").read_text(encoding="utf-8")

        self.assertIn("generate-c2-observation-schema.py", text)
        self.assertIn('"--check"', text)
        required_scripts = (
            "run_wechat_win32_ocr_compat_checks.py",
            "run_wechat_win32_ocr_env_config_checks.py",
            "run_wechat_win32_ocr_interaction_evidence_checks.py",
            "run_wechat_win32_ocr_humanized_input_checks.py",
            "run_wechat_win32_ocr_window_action_planning_checks.py",
        )
        for script_name in required_scripts:
            self.assertIn(script_name, text)


if __name__ == "__main__":
    unittest.main()
