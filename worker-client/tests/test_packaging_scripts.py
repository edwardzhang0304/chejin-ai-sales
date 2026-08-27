from __future__ import annotations

from pathlib import Path
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


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
    def _load_fast_uat_package_module():
        path = ROOT / "scripts" / "build-fast-uat-package.py"
        spec = importlib.util.spec_from_file_location(
            "chejin_build_fast_uat_package_test",
            path,
        )
        if spec is None or spec.loader is None:
            raise AssertionError("fast UAT package module unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

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
            ROOT / "scripts" / "build-fast-uat-runtime.ps1",
            ROOT / "scripts" / "collect-wechat-diagnostics.ps1",
            ROOT / "scripts" / "run-preflight.ps1",
            ROOT / "scripts" / "validate-package.ps1",
            ROOT / "packaging" / "start-fast-uat.ps1",
        ]

        for script in scripts:
            text = script.read_text(encoding="utf-8-sig").lstrip()
            if "param(" in text:
                self.assertTrue(text.startswith("param("), f"{script.name} 的 param 块必须在脚本最前面")

    def test_retired_desktop_input_feature_has_no_active_source_residue(self):
        source_roots = (
            ROOT / "chejin_worker_client",
            ROOT / "packaging",
            ROOT / "scripts",
            ROOT / "omniauto-rpa" / "apps" / "wechat_ai_customer_service",
            ROOT.parent / "packages" / "worker-ui-baseline" / "src",
            ROOT.parent / ".github",
        )
        source_suffixes = {".py", ".ps1", ".yml", ".yaml", ".json", ".ts", ".tsx", ".js", ".css"}
        skipped_parts = {"__pycache__", "node_modules", "dist", "web_assets"}
        retired_patterns = (
            "ui_" + "operator_" + "guard",
            "rpa_" + "operator_" + "guard",
            "rpa-" + "operator-" + "guard",
            "operator" + "Guard",
            "guard" + "_fault",
            "guard" + "_health",
            "floating_" + "indicator",
            "block_" + "manual_input",
            "OPERATOR_" + "GUARD",
            "悬浮" + "球",
            "守护" + "故障",
        )
        shortcut_pattern = re.compile(r"(?<![A-Za-z0-9])" + "F" + "8" + r"(?![A-Za-z0-9])", re.IGNORECASE)
        residue: list[str] = []

        for source_root in source_roots:
            for path in source_root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in source_suffixes:
                    continue
                if skipped_parts.intersection(path.parts):
                    continue
                text = path.read_text(encoding="utf-8-sig", errors="ignore")
                lowered = text.lower()
                matches = [pattern for pattern in retired_patterns if pattern.lower() in lowered]
                if matches or shortcut_pattern.search(text):
                    residue.append(f"{path}: {', '.join(matches) or 'retired shortcut'}")

        self.assertEqual([], residue, "\n".join(residue))

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
        self.assertIn('"psutil"', text)
        self.assertIn('"tkinter"', text)
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
        self.assertIn("chejin-worker-v0.9.42-windows-x64.delivery.json", workflow)
        self.assertIn("CHEJIN_VISION_CLIENT_API_KEY", workflow)
        self.assertIn("vision_credential_embedded", workflow)
        self.assertIn("vision_configuration_locked", workflow)
        self.assertIn("vision_live_probe_check", workflow)
        self.assertIn("diagnostic or manifest output leaked the Vision credential", workflow)
        self.assertIn("delivery manifest leaked the Vision credential", workflow)
        self.assertIn('Join-Path $verifiedPackageRoot "start-uat.ps1"', workflow)
        self.assertIn("powershell.exe -NoProfile -NonInteractive", workflow)
        self.assertIn("validate-uat-launcher.ps1", workflow)
        self.assertIn('uat_launcher_utf8_bom_check = "passed"', workflow)
        self.assertIn('uat_launcher_powershell_5_1_parse_check = "passed"', workflow)
        self.assertIn("client_delivery_boundary_check", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("if-no-files-found: error", workflow)

    def test_formal_exe_workflow_is_manual_and_requires_completed_uat(self):
        workflow = (
            ROOT.parent / ".github" / "workflows" / "worker-windows-package.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("\n  push:", workflow)
        self.assertIn("release_approved:", workflow)
        self.assertIn("release_reason:", workflow)
        self.assertIn("Formal EXE build is blocked until Fast UAT C0-C4 is approved", workflow)

    def test_fast_uat_workflow_reuses_runtime_and_probes_extracted_zip(self):
        workflow = (
            ROOT.parent / ".github" / "workflows" / "worker-windows-fast-uat.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("actions/cache@v4", workflow)
        self.assertIn("build-fast-uat-runtime.ps1", workflow)
        self.assertIn("python run_checks.py", workflow)
        self.assertIn("build-fast-uat-package.py", workflow)
        self.assertIn("debug_uat", workflow)
        self.assertIn("git_dirty", workflow)
        self.assertIn("not_for_customer_release", workflow)
        self.assertIn("Expand-Archive -LiteralPath $zip.FullName", workflow)
        self.assertIn("start-fast-uat.ps1", workflow)
        self.assertIn("-PreflightOnly -SkipBackend -SkipWechat", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)

    def test_fast_uat_runtime_cache_is_gitignored(self):
        result = subprocess.run(
            [
                "git",
                "check-ignore",
                "-q",
                "--no-index",
                "worker-client/.fast-uat-runtime/runtime/python.exe",
            ],
            cwd=ROOT.parent,
            check=False,
        )

        self.assertEqual(result.returncode, 0)

    def test_fast_uat_launcher_uses_locked_runtime_and_shared_worker_data(self):
        launcher = (ROOT / "packaging" / "start-fast-uat.ps1").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn('$env:CHEJIN_BUILD_KIND = "debug_uat_locked"', launcher)
        self.assertIn("CHEJIN_VISION_CREDENTIAL_PATH", launcher)
        self.assertIn("CHEJIN_OMNIAUTO_RPA_SOURCE", launcher)
        self.assertIn('Join-Path $localAppData "CheJinWorker\\diagnostics"', launcher)
        self.assertNotIn("CHEJIN_WORKER_HOME", launcher)
        self.assertIn('"-m", "chejin_worker_client.main"', launcher)

    def test_fast_uat_zip_is_portable_traceable_and_explicitly_non_formal(self):
        module = self._load_fast_uat_package_module()
        commit = "a" * 40
        provenance = {
            "schema_version": 2,
            "upstream_base_commit": "b" * 40,
            "chejin_integration_commit": "c" * 40,
        }

        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"CHEJIN_VISION_CLIENT_API_KEY": "fast-uat-secret-never-public"},
            clear=False,
        ):
            root = Path(temp)
            runtime = root / "runtime-base"
            output = root / "release"
            runtime.mkdir()
            (runtime / "python.exe").write_bytes(b"portable-python")
            (runtime / "pythonw.exe").write_bytes(b"portable-pythonw")
            (runtime / "fast-uat-runtime-base.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "runtime_kind": "chejin_worker_fast_uat_base",
                        "python_version": "3.12.10",
                        "platform": "windows-x64",
                        "reusable": True,
                    }
                ),
                encoding="utf-8",
            )

            def copy_worker(destination):
                target = destination / "chejin_worker_client" / "__init__.py"
                target.parent.mkdir(parents=True)
                target.write_text('__version__ = "test"\n', encoding="utf-8")

            def copy_omniauto(destination):
                target = destination / "omniauto-rpa" / "apps" / "sidecar.py"
                target.parent.mkdir(parents=True)
                target.write_text("# packaged OmniAuto\n", encoding="utf-8")

            with patch.object(module, "_copy_worker_app", side_effect=copy_worker), patch.object(
                module, "_copy_omniauto", side_effect=copy_omniauto
            ), patch.object(
                module,
                "verify_build_source",
                return_value={"git_commit": commit, "git_dirty": False},
            ), patch.object(
                module, "load_source_provenance", return_value=provenance
            ), patch.object(
                module,
                "tree_manifest",
                return_value={"tree_sha256": "d" * 64, "file_count": 1},
            ):
                result = module.build(
                    runtime_root=runtime,
                    output_dir=output,
                    git_commit=commit,
                    git_branch="codex/worker-fast-uat-test",
                )

            zip_path = Path(str(result["zip_path"]))
            self.assertTrue(zip_path.is_file())
            with zipfile.ZipFile(zip_path) as archive:
                members = set(archive.namelist())
                manifest = json.loads(
                    archive.read("CheJinWorkerDebug/fast-uat-manifest.json")
                )
                credential = json.loads(
                    archive.read("CheJinWorkerDebug/app/vision-runtime.json")
                )
                public_manifest_bytes = archive.read(
                    "CheJinWorkerDebug/fast-uat-manifest.json"
                )

            self.assertIn("CheJinWorkerDebug/runtime/python.exe", members)
            self.assertIn("CheJinWorkerDebug/runtime/pythonw.exe", members)
            self.assertIn("CheJinWorkerDebug/start-fast-uat.ps1", members)
            self.assertTrue(manifest["debug_uat"])
            self.assertFalse(manifest["formal_release"])
            self.assertTrue(manifest["not_for_customer_release"])
            self.assertEqual(manifest["git_commit"], commit)
            self.assertIs(manifest["git_dirty"], False)
            self.assertEqual(manifest["omniauto_source"], provenance)
            self.assertEqual(
                manifest["runtime_base"]["runtime_kind"],
                "chejin_worker_fast_uat_base",
            )
            self.assertEqual(
                credential["vision_api_key"], "fast-uat-secret-never-public"
            )
            self.assertNotIn(b"fast-uat-secret-never-public", public_manifest_bytes)

    def test_fast_uat_zip_rejects_dirty_git_source(self):
        module = self._load_fast_uat_package_module()
        commit = "a" * 40

        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"CHEJIN_VISION_CLIENT_API_KEY": "fast-uat-secret-never-public"},
            clear=False,
        ):
            root = Path(temp)
            runtime = root / "runtime-base"
            runtime.mkdir()
            (runtime / "python.exe").write_bytes(b"portable-python")

            with patch.object(
                module,
                "verify_build_source",
                return_value={"git_commit": commit, "git_dirty": True},
            ), self.assertRaisesRegex(SystemExit, "FAST_UAT_GIT_DIRTY"):
                module.build(
                    runtime_root=runtime,
                    output_dir=root / "release",
                    git_commit=commit,
                    git_branch="codex/worker-fast-uat-test",
                )

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
            "a563e6688c47a8922510794101967823fe1389d7",
        )
        self.assertEqual(
            provenance["selective_integrations"],
            [
                {
                    "source_commit": (
                        "307241810963c2e649ba04483a898687d06ba9f4"
                    ),
                    "scope": [
                        "exact_wechat_context_menu_classification",
                        "same_popup_menu_panel_evidence_contract",
                        "clipboard_non_bitmap_failure_settlement",
                        "formal_image_menu_failure_reason_contract",
                        "copy_click_precommit_safety_order",
                        (
                            "reliable_message_type_before_structural_"
                            "image_arbitration"
                        ),
                        (
                            "monotonic_reliable_text_voice_type_"
                            "arbitration"
                        ),
                        "voice_prepare_execute_single_target_contract",
                        "continuous_voice_tracking_edges_contract",
                        "unified_voice_observation_sequence_arbitration",
                        (
                            "voice_action_journal_monotonic_terminal_"
                            "settlement"
                        ),
                        "ambiguous_voice_finite_terminal_settlement",
                        "frame_visual_id_not_durable_identity",
                        "source_message_transport_allowlist",
                        "typed_committed_media_identity_contract",
                        "media_action_four_terminal_contract",
                        "c2_contract_0_9_36_generated_schema",
                        "c2_contract_0_9_37_generated_schema",
                        "c2_contract_0_9_38_generated_schema",
                        (
                            "incremental_delivery_complete_frame_"
                            "evidence_contract"
                        ),
                        (
                            "final_partition_complete_observation_"
                            "commit_contract"
                        ),
                        "shared_message_viewport_projection_contract",
                        "brain_provider_progress_markers_contract",
                        (
                            "brain_provider_primary_fallback_repair_"
                            "reviewer_stage_contract"
                        ),
                        "brain_provider_progress_secret_free_contract",
                        (
                            "voice_frame_action_binding_observation_"
                            "projection_contract"
                        ),
                        "bounded_send_foreground_focus_recovery_contract",
                        (
                            "brain_soft_evidence_clarification_and_"
                            "reply_then_handoff_contract"
                        ),
                        "post_confirm_add_friend_dialog_cleanup_contract",
                        (
                            "post_confirm_add_friend_surviving_hwnd_"
                            "cleanup_verification"
                        ),
                        "already_friend_add_friend_dialog_cleanup_contract",
                        "sidebar_title_preview_physical_line_separation",
                        (
                            "safe_visible_target_stale_after_click_"
                            "relocation_contract"
                        ),
                        "tall_image_bubble_same_row_avatar_role_recovery",
                        "popup_snapshot_bound_media_action_contract",
                        "wechat_legacy_fixed_coordinate_model_removed",
                        "strict_typed_vision_port_only_contract",
                        "pr28_protected_baseline_complete_reporting",
                        "typed_empty_ocr_region_failure_contract",
                        "stable_invite_form_atomic_snapshot_reuse_contract",
                        (
                            "copied_eight_char_short_code_presence_"
                            "verification_contract"
                        ),
                        "unchanged_surface_evidence_frame_reuse_contract",
                        "add_friend_frame_seed_facade_forwarding_contract",
                        "verified_send_input_bounds_forwarding_contract",
                        "invalid_session_layout_explicit_failure_contract",
                        "v0_9_31_startup_layout_calibration_contract",
                        "gray_v0_9_20_region_local_coordinate_map_contract",
                        "current_monitor_dpi_window_profile_contract",
                        "startup_window_position_and_size_once_contract",
                        "exact_visible_client_capture_calibration_contract",
                        "startup_region_mapped_plus_reference_contract",
                        (
                            "business_frame_and_calibration_identity_"
                            "separation_contract"
                        ),
                        "c1_c2_c3_c4_region_map_coordinate_owner_contract",
                        "business_actions_never_normalize_window_contract",
                        (
                            "runtime_window_change_manual_restart_"
                            "contract"
                        ),
                        "c1_eight_business_change_frames_contract",
                        (
                            "gray_v0_9_20_entry_activation_without_global_"
                            "success_gate_contract"
                        ),
                        "popup_menu_foreground_not_main_hwnd_equality_gated_contract",
                        "frozen_compat_sidecar_single_command_contract",
                        "four_edge_dpi_window_margin_contract",
                        "visible_calibrated_window_ignores_hidden_weixin_shell_contract",
                        (
                            "send_input_click_surface_and_text_detection_"
                            "roi_separation_contract"
                        ),
                        "voice_popup_hwnd_unknown_state_contract",
                        (
                            "voice_click_verification_shared_bounded_"
                            "evidence_wait_contract"
                        ),
                        (
                            "image_popup_cleanup_until_clipboard_"
                            "confirmation_contract"
                        ),
                        "media_stage_failure_telemetry_correction_contract",
                        "windows_search_focus_fixture_layout_binding_contract",
                        (
                            "add_friend_plus_vertical_search_anchor_"
                            "constraint_contract"
                        ),
                        (
                            "restart_inflight_media_action_journal_"
                            "recovery_contract"
                        ),
                        (
                            "restart_full_durable_media_"
                            "reconciliation_contract"
                        ),
                        (
                            "worker_sequence_ordered_mixed_media_"
                            "recovery_contract"
                        ),
                        (
                            "transient_recovery_authorization_"
                            "retry_contract"
                        ),
                        "legacy_media_upgrade_finite_terminal_contract",
                        (
                            "legacy_media_permanent_error_manual_review_"
                            "contract"
                        ),
                        "legacy_media_owner_unknown_audit_contract",
                        (
                            "normalized_message_viewport_dynamic_noise_"
                            "exclusion_contract"
                        ),
                        "frame_action_binding_not_durable_identity_contract",
                        "sidecar_worker_identity_fields_forbidden_contract",
                        "selected_current_media_reservation_only_contract",
                        "voice_then_image_single_action_per_frame_contract",
                        "authoritative_final_frame_screen_order_contract",
                        "pre_send_single_full_reidentification_contract",
                        "layout_invalid_faulted_worker_evidence_contract",
                        (
                            "recursive_sidecar_public_output_identity_"
                            "sanitization_contract"
                        ),
                        (
                            "four_sidecar_production_entry_identity_"
                            "boundary_regression_contract"
                        ),
                        (
                            "windows_full_gate_production_fixture_"
                            "alignment_contract"
                        ),
                        "telemetry_sqlite_connection_close_contract",
                        (
                            "windows_test_artifact_filename_and_incident_"
                            "worker_cleanup_contract"
                        ),
                        "optional_windows_evidence_pytest_skip_contract",
                        "same_frame_voice_physical_row_merge_contract",
                        (
                            "low_authority_fast_short_vehicle_purchase_"
                            "intent_contract"
                        ),
                        "brain_shared_total_time_budget_contract",
                        "c2_contract_0_9_42_generated_schema",
                        "c2_locate_frame_same_sidecar_reuse_contract",
                        "c3_same_frame_roi_full_ocr_fallback_contract",
                        "sidebar_candidate_full_window_safety_contract",
                    ],
                }
            ],
        )
        self.assertIn(
            "strict_current_screen_without_history_scroll",
            provenance["chejin_overlays"],
        )
        self.assertIn(
            "c2_omniauto_authoritative_session_admission",
            provenance["chejin_overlays"],
        )
        self.assertIn(
            "c3_active_chat_send_context_guard",
            provenance["chejin_overlays"],
        )
        self.assertIn(
            "add_friend_preclick_layout_failure_non_pausing_contract",
            provenance["chejin_overlays"],
        )
        self.assertIn(
            "307241810963c2e649ba04483a898687d06ba9f4",
            provenance["integration_note"],
        )
        self.assertIn("0.9.42", provenance["integration_note"])
        self.assertIn(
            "四类 Sidecar 公开输出递归删除 Worker 专属身份字段",
            provenance["integration_note"],
        )
        self.assertIn(
            "worker_owned_message_identity_sequence_and_commit_gate_contract",
            provenance["chejin_overlays"],
        )
        self.assertIn(
            "selected_current_media_reservation_only_contract",
            provenance["chejin_overlays"],
        )
        self.assertIn(
            "pre_send_guard_exact_observation_frame_binding_contract",
            provenance["chejin_overlays"],
        )
        self.assertIn(
            "pre_send_passive_reread_layout_recheck_contract",
            provenance["chejin_overlays"],
        )
        self.assertIn(
            "incremental_messages_complete_frame_evidence_contract",
            provenance["chejin_overlays"],
        )
        self.assertIn(
            "final_partition_complete_frame_commit_contract",
            provenance["chejin_overlays"],
        )
        self.assertIn(
            "unknown_fact_explicit_identity_gate_contract",
            provenance["chejin_overlays"],
        )
        self.assertIn("不重复点击", provenance["integration_note"])
        self.assertIn("旧剪贴板不能形成图片事实", provenance["integration_note"])
        self.assertIn("无界面的语音/图片 ActionJournal 恢复", provenance["integration_note"])
        self.assertIn("结束旧 Flow 后继续拉取其他任务", provenance["integration_note"])
        self.assertIn(
            "immutable_visible_scan_frame_reuse_contract",
            provenance["chejin_overlays"],
        )
        self.assertIn(
            "post_brain_pre_send_fresh_frame_local_reuse_contract",
            provenance["chejin_overlays"],
        )
        self.assertIn(
            "send_s0_s1_s2_distinct_frame_local_reuse_contract",
            provenance["chejin_overlays"],
        )
        self.assertIn("startup_layout_calibration", provenance["integration_note"])
        self.assertIn("C1-C4 业务帧", provenance["integration_note"])
        self.assertIn("OmniAuto 独占区域映射和坐标决策", provenance["integration_note"])
        self.assertIn("删除统一激活成功布尔门禁", provenance["integration_note"])
        self.assertIn("C1 保留八类真实变化画面", provenance["integration_note"])
        self.assertIn("sent_ack 语义不变", provenance["integration_note"])
        self.assertIn("manual_review_required", provenance["integration_note"])
        self.assertIn("LEGACY_MEDIA_OWNER_UNKNOWN", provenance["integration_note"])
        self.assertIn("OmniAuto 独立候选固定于", provenance["integration_note"])
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
        retired_mode = '"--rpa-' + 'operator-' + 'guard"'
        self.assertNotIn(retired_mode, entry_text)
        self.assertIn('"--omniauto-vision-wechat-worker"', entry_text)
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
        self.assertIn('"psutil"', text)
        self.assertIn('"tkinter"', text)
        self.assertIn("packaging-runtime-diagnostics.jsonl", text)
        self.assertIn("CHEJIN_PACKAGING_DIAGNOSTIC_PATH", text)
        self.assertIn('"packaging\\start-uat.ps1"', text)
        self.assertIn("Copy-Item -LiteralPath $UatLauncherSourcePath", text)
        self.assertIn("validate-uat-launcher.ps1", text)
        self.assertIn("powershell.exe -NoProfile -NonInteractive", text)
        self.assertIn("Windows PowerShell 5.1 BOM/语法门禁", text)
        self.assertIn("CHEJIN_VISION_CLIENT_API_KEY", text)
        self.assertIn("GITHUB_ACTIONS", text)
        self.assertIn("CHEJIN_VISION_CREDENTIAL_PATH", text)
        self.assertIn("vision-runtime.json", text)
        self.assertIn("最终 exe 内置 Vision 能力预检未通过", text)
        self.assertIn("最终 exe 内置 Vision 真实能力探针未通过", text)
        self.assertIn("vision_live_probe_check", text)

    def test_uat_launcher_requires_api_runs_preflight_and_saves_report(self):
        raw = (ROOT / "packaging" / "start-uat.ps1").read_bytes()
        self.assertEqual(raw[:3], bytes.fromhex("EF BB BF"))
        text = raw.decode("utf-8-sig")

        self.assertIn("[Parameter(Mandatory = $true)]", text)
        self.assertIn("CHEJIN_API_BASE_URL", text)
        self.assertIn('CHEJIN_RPA_MODE = "real"', text)
        self.assertIn('"--preflight"', text)
        self.assertIn('"--write-report"', text)
        self.assertIn('"uat-preflight-$timestamp.json"', text)
        self.assertIn("if ($preflight.ExitCode -ne 0)", text)
        self.assertIn("Start-Process -FilePath $exePath", text)

    def test_powershell_5_1_validator_checks_bom_and_real_parser(self):
        text = (ROOT / "scripts" / "validate-uat-launcher.ps1").read_text(
            encoding="ascii"
        )

        self.assertIn("PSVersionTable.PSVersion.Major -ne 5", text)
        self.assertIn("PSVersionTable.PSVersion.Minor -ne 1", text)
        self.assertIn('PSVersionTable.PSEdition -ne "Desktop"', text)
        self.assertIn("[System.IO.File]::ReadAllBytes", text)
        self.assertIn("$bytes[0] -ne 0xEF", text)
        self.assertIn("$bytes[1] -ne 0xBB", text)
        self.assertIn("$bytes[2] -ne 0xBF", text)
        self.assertIn("Language.Parser]::ParseFile", text)
        self.assertIn("parse_error_count", text)
        self.assertIn("POWERSHELL_5_1_PARSE_GATE", text)

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
        self.assertIn('version -ne "0.9.42"', workflow)
        self.assertIn('tests_status -ne "passed"', workflow)
        self.assertIn('@("--omniauto-sidecar", "--help")', workflow)
        self.assertIn('@("--omniauto-ocr-probe")', workflow)
        self.assertIn("chejin-worker-packaged-preflight.json", workflow)
        self.assertIn("chejin-worker-packaged-diagnostics.jsonl", workflow)
        self.assertIn('"--preflight-format", "json", "--write-report"', workflow)
        self.assertIn("packaged Vision live capability probe did not pass", workflow)
        self.assertIn('Remove-Item Env:CHEJIN_PACKAGING_DIAGNOSTIC_PATH', workflow)
        self.assertIn('"--startup-crash-probe"', workflow)
        self.assertIn("startup-crash.jsonl", workflow)
        self.assertIn("normal packaged startup produced a false crash diagnostic", workflow)
        self.assertIn("normal packaged startup probe timed out", workflow)
        self.assertIn("intentional packaged startup crash probe timed out", workflow)
        self.assertIn("startup crash diagnostic build identity mismatch", workflow)
        self.assertIn("contents: read", workflow)
        self.assertNotIn("contents: write", workflow)

    def test_package_includes_one_command_uat_evidence_collector(self):
        collector = ROOT / "packaging" / "collect-uat-evidence.ps1"
        helper = ROOT / "packaging" / "collect_uat_evidence.py"
        raw = collector.read_bytes()
        text = raw.decode("utf-8-sig")
        helper_text = helper.read_text(encoding="utf-8")
        build = (ROOT / "scripts" / "build-windows.ps1").read_text(
            encoding="utf-8-sig"
        )
        workflow = (
            ROOT.parent / ".github" / "workflows" / "worker-windows-package.yml"
        ).read_text(encoding="utf-8")

        self.assertEqual(raw[:3], b"\xef\xbb\xbf")
        self.assertIn("[datetimeoffset]$From", text)
        self.assertIn("[datetimeoffset]$To", text)
        self.assertIn("collect_uat_evidence.py", text)
        self.assertIn("mode=ro", helper_text)
        self.assertIn("PRAGMA query_only=ON", helper_text)
        self.assertIn('"worker_client.sqlite3"', helper_text)
        self.assertIn('"chat_screenshots"', helper_text)
        self.assertIn("collect-uat-evidence.ps1", build)
        self.assertIn("collect_uat_evidence.py", build)
        self.assertIn("collect-uat-evidence.ps1", workflow)
        self.assertIn("collect_uat_evidence.py", workflow)

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
        self.assertTrue(
            is_client_forbidden_path(
                "apps/wechat_ai_customer_service/scripts/"
                "run_customer_service_listener.py",
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
            "run_wechat_startup_calibration_v0923_checks.py",
        )
        for script_name in required_scripts:
            self.assertIn(script_name, text)


if __name__ == "__main__":
    unittest.main()
