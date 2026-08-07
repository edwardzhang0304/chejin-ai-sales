from __future__ import annotations

from pathlib import Path
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_policy import BuildPolicyError, validate_build_policy
from client_delivery_policy import (
    forbidden_tree_entries,
    is_client_forbidden_path,
    load_client_exclude_paths,
)
from build_source import (
    BuildSourceError,
    resolve_contract_path,
    verify_build_source,
)
from omniauto_tree import (
    load_source_provenance,
    tree_manifest,
    verify_same_tree,
)
from tests.contract_artifacts import resolve_contract_artifact


class PackagingScriptsTest(unittest.TestCase):
    @staticmethod
    def _load_source_package_module():
        path = ROOT / "scripts" / "build-source-package.py"
        spec = importlib.util.spec_from_file_location(
            "chejin_build_source_package_test",
            path,
        )
        if spec is None or spec.loader is None:
            raise AssertionError("source package module unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

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
        self.assertIn("omniauto_historical_integrations", text)
        self.assertIn("omniauto_chejin_overlays", text)
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
        self.assertIn("client_delivery_policy.py", text)
        self.assertIn("client_delivery_boundary_check", text)
        self.assertIn("最终 exe 未包含运行依赖", text)
        self.assertIn('"uiautomation"', text)
        self.assertIn('"pyperclip"', text)
        self.assertIn('"pywinauto"', text)
        self.assertIn('"--omniauto-sidecar", "--help"', text)
        self.assertIn("最终 exe 无法启动内置 OmniAuto sidecar", text)
        self.assertIn('"--omniauto-ocr-probe"', text)
        self.assertIn("最终 exe 无法启动图片复核 OCR 独立进程", text)
        self.assertIn("runtime-build-identity.json", text)
        self.assertIn("CHEJIN_BUILD_IDENTITY_PATH", text)
        self.assertNotIn('$OmniAutoUpstreamCommit = "855c218', text)
        self.assertIn("scripts\\build_source.py", text)
        self.assertIn("$LASTEXITCODE -ne 0", text)
        self.assertNotIn("git rev-parse", text)
        self.assertNotIn("git status --porcelain", text)
        self.assertIn("OFFICIAL_BUILD_GIT_SOURCE_REQUIRED", (
            ROOT / "scripts" / "build_source.py"
        ).read_text(encoding="utf-8"))

    def test_windows_ci_publishes_a_verified_portable_zip(self):
        workflow = (
            ROOT.parent / ".github" / "workflows" / "worker-windows-package.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("4998a5853154dde2c224a21a3eef66c7b6d7db99", workflow)
        self.assertIn("git merge-base --is-ancestor", workflow)
        self.assertIn("Compress-Archive -Path $packageDir", workflow)
        self.assertIn("Expand-Archive -LiteralPath $zipPath", workflow)
        self.assertIn('Get-ChildItem -Path $verifyDir -Filter "*.exe" -File -Recurse', workflow)
        self.assertIn("matching the packaged SHA256", workflow)
        self.assertIn("delivery ZIP does not contain the packaged runtime directory", workflow)
        self.assertIn("app_name = [string]$manifest.app_name", workflow)
        self.assertIn("delivery ZIP executable SHA256 mismatch", workflow)
        self.assertIn("chejin-worker-v16.134.0-windows-x64.delivery.json", workflow)
        self.assertIn("client_delivery_boundary_check", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("if-no-files-found: error", workflow)

    def test_source_package_script_excludes_local_env_and_runtime_state(self):
        text = (ROOT / "scripts" / "build-source-package.py").read_text(encoding="utf-8")

        self.assertIn("*.local.env", text)
        self.assertIn('".env"', text)
        self.assertNotIn("endswith(\".example.env\")", text)
        self.assertIn('"runtime"', text)
        self.assertIn('"cache"', text)
        self.assertIn('".full-check-venv"', text)
        self.assertIn('"forbidden_entries"', text)
        self.assertIn("ALLOWED_DATA_PREFIXES", text)
        self.assertIn('"omniauto_tree_sha256"', text)
        self.assertIn('"omniauto_upstream_base_commit"', text)
        self.assertIn('"omniauto_selective_integrations"', text)
        self.assertIn('"omniauto_historical_integrations"', text)
        self.assertIn('"omniauto_chejin_overlays"', text)
        self.assertIn('"omniauto_chejin_integration_commit"', text)
        self.assertIn('"generated_observation_schema_sha256"', text)
        self.assertIn('"canonical_contract_sha256"', text)
        self.assertIn('"contract_file_check": "passed"', text)
        self.assertIn("C2_CONTRACT_FILE_MISMATCH", text)
        self.assertIn("c2_v3_mixed_roundtrip.json", text)
        self.assertIn("_zip_member_sha256", text)
        self.assertIn("--development-build", text)
        self.assertIn("preflight_status", text)
        self.assertIn("verify_build_source", text)
        self.assertIn("runtime-build-identity.json", text)
        self.assertIn('"version": _full_version()', text)
        self.assertNotIn("def _git_output", text)

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
                boundary_manifest = (
                    base
                    / "apps"
                    / "wechat_ai_customer_service"
                    / "deploy"
                    / "client_source_manifest.json"
                )
                boundary_manifest.parent.mkdir(parents=True)
                boundary_manifest.write_text(
                    json.dumps(
                        {
                            "exclude_paths": [
                                "apps/wechat_ai_customer_service/vps_admin/"
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

            verified = verify_same_tree(source, packaged)
            self.assertEqual(
                verified["source"]["tree_sha256"],
                verified["packaged"]["tree_sha256"],
            )
            (packaged / "apps" / "two.py").write_text("two", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "OMNIAUTO_TREE_MISMATCH"):
                verify_same_tree(source, packaged)

    def test_omniauto_provenance_records_merged_upstream_and_history(
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
            "35b0eee13c6423d56a0f15736f96a422e10d8d1c",
        )
        self.assertEqual(provenance["selective_integrations"], [])
        self.assertIn(
            "strict_current_screen_without_history_scroll",
            provenance["chejin_overlays"],
        )
        self.assertEqual(
            provenance["historical_integrations"][0][
                "chejin_integration_commit"
            ],
            "ff9e0de00013ac51a2f2a05e3774748c43c846fb",
        )

    def test_omniauto_v3_provenance_requires_declared_chejin_overlays(self):
        source_path = ROOT / "omniauto-rpa" / ".chejin-source.json"
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        payload.pop("chejin_overlays")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".chejin-source.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError,
                "OMNIAUTO_CHEJIN_OVERLAYS_INVALID",
            ):
                load_source_provenance(root)

    def test_generated_schema_check_runs_from_packaged_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            packaged_root = Path(temp) / "worker-client"
            (packaged_root / "scripts").mkdir(parents=True)
            (packaged_root / "contracts").mkdir()
            generated_relative = Path(
                "omniauto-rpa/apps/wechat_ai_customer_service/adapters/"
                "chejin_c2_observation_schema.generated.json"
            )
            (packaged_root / generated_relative).parent.mkdir(parents=True)
            shutil.copy2(
                ROOT / "scripts" / "generate-c2-observation-schema.py",
                packaged_root / "scripts" / "generate-c2-observation-schema.py",
            )
            shutil.copy2(
                resolve_contract_artifact("c2_contract_v3.json"),
                packaged_root / "contracts" / "c2_contract_v3.json",
            )
            shutil.copy2(
                ROOT / generated_relative,
                packaged_root / generated_relative,
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/generate-c2-observation-schema.py",
                    "--check",
                ],
                cwd=packaged_root,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("C2 observation schema is current", result.stdout)

    def test_contract_path_prefers_packaged_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            project_root = Path(temp)
            client_root = project_root / "worker-client"
            packaged_contract = (
                client_root / "contracts" / "c2_contract_v3.json"
            )
            repository_contract = (
                project_root / "contracts" / "c2_contract_v3.json"
            )
            packaged_contract.parent.mkdir(parents=True)
            repository_contract.parent.mkdir(parents=True)
            packaged_contract.write_text("packaged", encoding="utf-8")
            repository_contract.write_text("repository", encoding="utf-8")

            resolved = resolve_contract_path(client_root)

        self.assertEqual(resolved, packaged_contract.resolve())

    def test_official_build_without_git_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            client_root = Path(temp) / "worker-client"
            contract = client_root / "contracts" / "c2_contract_v3.json"
            contract.parent.mkdir(parents=True)
            contract.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(
                BuildSourceError,
                "OFFICIAL_BUILD_GIT_SOURCE_REQUIRED",
            ):
                verify_build_source(
                    client_root,
                    development_build=False,
                )
            development = verify_build_source(
                client_root,
                development_build=True,
            )

        self.assertFalse(development["git_available"])
        self.assertTrue(development["git_dirty"])
        self.assertEqual(development["git_commit"], "")
        self.assertFalse(development["git_detached"])

    def test_official_build_rejects_unrelated_parent_git_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            outer_root = Path(temp) / "unrelated"
            client_root = outer_root / "ignored-source" / "worker-client"
            contract = client_root / "contracts" / "c2_contract_v3.json"
            controlled = client_root / "scripts" / "build_source.py"
            outer_root.mkdir()
            subprocess.run(
                ["git", "init", "-q"],
                cwd=outer_root,
                check=True,
            )
            (outer_root / ".gitignore").write_text(
                "ignored-source/\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", ".gitignore"],
                cwd=outer_root,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Chejin Test",
                    "-c",
                    "user.email=chejin-test@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "test parent",
                ],
                cwd=outer_root,
                check=True,
            )
            contract.parent.mkdir(parents=True)
            controlled.parent.mkdir(parents=True)
            contract.write_text("{}", encoding="utf-8")
            controlled.write_text("# ignored source", encoding="utf-8")

            with self.assertRaisesRegex(
                BuildSourceError,
                "OFFICIAL_BUILD_GIT_SOURCE_REQUIRED",
            ):
                verify_build_source(
                    client_root,
                    development_build=False,
                )

    def test_official_detached_head_is_recorded_explicitly(self):
        with tempfile.TemporaryDirectory() as temp:
            project_root = Path(temp) / "project"
            client_root = project_root / "worker-client"
            contract = project_root / "contracts" / "c2_contract_v3.json"
            controlled = client_root / "scripts" / "build_source.py"
            contract.parent.mkdir(parents=True)
            controlled.parent.mkdir(parents=True)
            contract.write_text("{}", encoding="utf-8")
            controlled.write_text("# controlled source", encoding="utf-8")
            subprocess.run(
                ["git", "init", "-q"],
                cwd=project_root,
                check=True,
            )
            subprocess.run(
                ["git", "add", "."],
                cwd=project_root,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Chejin Test",
                    "-c",
                    "user.email=chejin-test@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "test source",
                ],
                cwd=project_root,
                check=True,
            )
            subprocess.run(
                ["git", "checkout", "--detach", "-q", "HEAD"],
                cwd=project_root,
                check=True,
            )

            result = verify_build_source(
                client_root,
                development_build=False,
            )

        self.assertTrue(result["git_available"])
        self.assertTrue(result["git_detached"])
        self.assertEqual(result["git_branch"], "DETACHED")

    def test_pyinstaller_spec_packages_contract_and_filters_omniauto_runtime_data(self):
        text = (ROOT / "packaging" / "chejin-worker-client.spec").read_text(encoding="utf-8")
        entry_text = (ROOT / "packaging" / "chejin_worker_client_entry.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('ENTRY_PATH = ROOT / "packaging" / "chejin_worker_client_entry.py"', text)
        self.assertIn("[str(ENTRY_PATH)]", text)
        self.assertNotIn('["chejin_worker_client/main.py"]', text)
        self.assertIn('PIL_HIDDEN_IMPORTS = collect_submodules("PIL")', text)
        self.assertIn("*PIL_HIDDEN_IMPORTS", text)
        self.assertIn('collect_all("rapidocr_onnxruntime")', text)
        self.assertIn("RAPIDOCR_DATAS", text)
        self.assertIn("RAPIDOCR_BINARIES", text)
        self.assertIn("RAPIDOCR_HIDDEN_IMPORTS", text)
        self.assertIn("disable_windowed_traceback=True", text)
        self.assertIn("from chejin_worker_client.main import main", entry_text)
        self.assertNotIn("from .", entry_text)
        self.assertIn("CHEJIN_PACKAGING_DIAGNOSTIC_PATH", entry_text)
        self.assertIn("_write_startup_diagnostic", entry_text)
        self.assertIn('"startup-crash.jsonl"', entry_text)
        self.assertIn("_restore_frozen_worker_stdio", entry_text)
        self.assertIn('"--omniauto-ocr-worker"', entry_text)
        self.assertIn('"--vision-provider-worker"', entry_text)
        self.assertIn("GetStdHandle", entry_text)
        self.assertIn("CONTRACT_PATH = resolve_contract_path(ROOT)", text)
        self.assertIn('(str(CONTRACT_PATH), "contracts")', text)
        self.assertIn("EXCLUDED_OMNIAUTO_PARTS", text)
        self.assertIn("ALLOWED_OMNIAUTO_DATA_PREFIXES", text)
        self.assertIn("load_client_exclude_paths", text)
        self.assertIn("is_client_forbidden_path", text)
        self.assertIn('"uiautomation"', text)
        self.assertNotIn('(str(OMNIAUTO_RPA_SOURCE), "omniauto-rpa")', text)

    def test_build_script_fails_closed_on_tests_and_required_frozen_modules(self):
        text = (ROOT / "scripts" / "build-windows.ps1").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn('$env:PYTHONUTF8 = "1"', text)
        self.assertIn('$env:PYTHONIOENCODING = "utf-8"', text)
        self.assertIn("onnxruntime.__version__ == '1.20.1'", text)
        self.assertIn("RapidOCR()(image)", text)
        self.assertIn("源码环境无法初始化固定版本的图片复核 OCR", text)
        self.assertIn("Worker 完整测试未通过", text)
        self.assertIn("PyInstaller 构建失败", text)
        self.assertIn('"PIL.ImageEnhance"', text)
        self.assertIn('"PIL.ImageGrab"', text)
        self.assertIn('"rapidocr_onnxruntime"', text)
        self.assertIn('"pyperclip"', text)
        self.assertIn('"pywinauto"', text)
        self.assertIn("packaging-runtime-diagnostics.jsonl", text)
        self.assertIn("CHEJIN_PACKAGING_DIAGNOSTIC_PATH", text)
        self.assertIn('"packaging\\start-uat.ps1"', text)
        self.assertIn("Copy-Item -LiteralPath $UatLauncherSourcePath", text)

    def test_uat_launcher_requires_api_runs_preflight_and_saves_report(self):
        text = (ROOT / "packaging" / "start-uat.ps1").read_text(encoding="utf-8")

        self.assertIn("[Parameter(Mandatory = $true)]", text)
        self.assertIn("CHEJIN_API_BASE_URL", text)
        self.assertIn('CHEJIN_RPA_MODE = "real"', text)
        self.assertIn('"--preflight"', text)
        self.assertIn('"--write-report"', text)
        self.assertIn('"uat-preflight-$timestamp.json"', text)
        self.assertIn("if ($preflight.ExitCode -ne 0)", text)
        self.assertIn("Start-Process -FilePath $exePath", text)

    def test_windows_package_ci_builds_and_probes_the_frozen_executable(self):
        workflow = (
            ROOT.parent / ".github" / "workflows" / "worker-windows-package.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("runs-on: windows-latest", workflow)
        self.assertIn("shell: powershell", workflow)
        self.assertIn(".\\scripts\\build-windows.ps1", workflow)
        self.assertIn("validate-package.ps1", workflow)
        self.assertIn('$manifestFiles = @(Get-ChildItem', workflow)
        self.assertIn('$packageDir = [string]$manifest.package_dir', workflow)
        self.assertIn('$exePath = [string]$manifest.exe_path', workflow)
        self.assertNotIn('dist\\车金Worker客户端', workflow)
        self.assertIn('version -ne "16.134.0"', workflow)
        self.assertIn('tests_status -ne "passed"', workflow)
        self.assertIn('@("--omniauto-sidecar", "--help")', workflow)
        self.assertIn('@("--omniauto-ocr-probe")', workflow)
        self.assertIn("chejin-worker-packaged-preflight.json", workflow)
        self.assertIn("chejin-worker-packaged-diagnostics.jsonl", workflow)
        self.assertIn('"--preflight-format", "json", "--write-report"', workflow)
        self.assertIn('Remove-Item Env:CHEJIN_PACKAGING_DIAGNOSTIC_PATH', workflow)
        self.assertIn('"--startup-crash-probe"', workflow)
        self.assertIn("startup-crash.jsonl", workflow)
        self.assertIn("normal packaged startup produced a false crash diagnostic", workflow)
        self.assertIn("normal packaged startup probe timed out", workflow)
        self.assertIn("intentional packaged startup crash probe timed out", workflow)
        self.assertIn("startup crash diagnostic build identity mismatch", workflow)
        self.assertIn("contents: read", workflow)
        self.assertNotIn("contents: write", workflow)

    def test_packaging_entry_imports_main_with_package_context(self):
        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "packaging" / "chejin_worker_client_entry.py"),
                "--help",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            encoding="utf-8",
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: chejin-worker-client", result.stdout)
        self.assertNotIn("attempted relative import", result.stderr)

    def test_client_delivery_policy_rejects_actual_server_private_paths(self):
        omniauto_root = ROOT / "omniauto-rpa"
        excludes = load_client_exclude_paths(omniauto_root)

        self.assertTrue(
            is_client_forbidden_path(
                "apps/wechat_ai_customer_service/vps_admin/app.py",
                excludes,
            )
        )
        self.assertTrue(
            is_client_forbidden_path(
                "apps/wechat_ai_customer_service/deploy/aliyun1/"
                "vps_admin_control_plane.enc.json",
                excludes,
            )
        )
        self.assertFalse(
            is_client_forbidden_path(
                "apps/wechat_ai_customer_service/adapters/"
                "wechat_win32_ocr_sidecar.py",
                excludes,
            )
        )

    def test_source_zip_member_scan_rejects_server_private_paths(self):
        module = self._load_source_package_module()
        private_member = (
            "worker-client/omniauto-rpa/apps/wechat_ai_customer_service/"
            "vps_admin/app.py"
        )
        allowed_member = (
            "worker-client/omniauto-rpa/apps/wechat_ai_customer_service/"
            "adapters/wechat_win32_ocr_sidecar.py"
        )

        forbidden = module._forbidden_entries(
            [private_member, allowed_member]
        )

        self.assertEqual(forbidden, [private_member])

    def test_final_package_tree_scan_detects_server_private_files(self):
        excludes = load_client_exclude_paths(ROOT / "omniauto-rpa")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            private_file = (
                root
                / "apps"
                / "wechat_ai_customer_service"
                / "deploy"
                / "aliyun1"
                / "control_plane.enc.json"
            )
            allowed_file = (
                root
                / "apps"
                / "wechat_ai_customer_service"
                / "adapters"
                / "sidecar.py"
            )
            private_file.parent.mkdir(parents=True)
            allowed_file.parent.mkdir(parents=True)
            private_file.write_text("private", encoding="utf-8")
            allowed_file.write_text("allowed", encoding="utf-8")

            forbidden = forbidden_tree_entries(root, excludes)

        self.assertEqual(
            forbidden,
            [
                "apps/wechat_ai_customer_service/deploy/aliyun1/"
                "control_plane.enc.json"
            ],
        )

    def test_windows_requirements_pin_native_runtime_dependencies(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn(
            'uiautomation==2.0.29; platform_system == "Windows"',
            text,
        )
        self.assertIn(
            'onnxruntime==1.20.1; platform_system == "Windows"',
            text,
        )

    def test_run_checks_includes_omniauto_safety_suites(self):
        text = (ROOT / "run_checks.py").read_text(encoding="utf-8")

        self.assertIn('env["PYTHONUTF8"] = "1"', text)
        self.assertIn('env["PYTHONIOENCODING"] = "utf-8"', text)
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
