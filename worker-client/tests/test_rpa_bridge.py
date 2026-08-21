from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

os.environ.setdefault("CHEJIN_WORKER_HOME", tempfile.mkdtemp(prefix="chejin-worker-test-"))
os.environ["CHEJIN_RPA_MODE"] = "mock"

from chejin_worker_client.action_journal import (
    action_journal_path,
    initialize_action_journal,
    update_action_journal_item,
)
from chejin_worker_client.config import CONFIG
from chejin_worker_client.models import Task
from chejin_worker_client.rpa_bridge import OMNIAUTO_ADD_FRIEND_ACTION, RpaBridge, default_sidecar_script


class RpaBridgeTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(
            CONFIG.app_dir / "transactions" / "actions" / "add_friend",
            ignore_errors=True,
        )

    @staticmethod
    def _confirm_add_friend_journal(
        args,
        *,
        task_id: str,
        ok: bool = True,
        result_code: str = "invite_sent",
        error_code: str = "",
    ):
        journal_path = Path(args[args.index("--action-journal") + 1])
        update_action_journal_item(
            journal_path,
            journal_item_id=task_id,
            action_phase="confirmed",
            business_state=result_code or error_code,
            business_result_confirmed=True,
            error_code=error_code or None,
            terminal_payload={
                "ok": ok,
                "result_code": result_code,
                "error_code": error_code,
                "current_step": "invite_confirm_clicked",
            },
        )

    def test_frozen_runtime_dispatches_sidecar_through_packaged_executable(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        with patch.object(sys, "frozen", True, create=True), patch.object(
            sys,
            "executable",
            "C:\\Program Files\\CheJin\\车金Worker客户端.exe",
        ):
            command = bridge._sidecar_command(["status"])

        self.assertEqual(
            command,
            [
                "C:\\Program Files\\CheJin\\车金Worker客户端.exe",
                "--omniauto-sidecar",
                "status",
            ],
        )

    def test_source_runtime_dispatches_sidecar_through_python(self):
        bridge = RpaBridge(sidecar_script=Path("sidecar.py"))
        with patch.object(sys, "frozen", False, create=True):
            command = bridge._sidecar_command(["status"])

        self.assertEqual(command, [sys.executable, "sidecar.py", "status"])

    def test_probe_reuses_successful_startup_calibration_observation(self):
        bridge = RpaBridge(sidecar_script=Path("sidecar.py"))
        bridge.mode = "real"
        payload = {
            "ok": True,
            "geometry": {
                "left": 100,
                "top": 80,
                "right": 900,
                "bottom": 700,
                "width": 800,
                "height": 620,
            },
        }

        calls: list[list[str]] = []

        def fake_call(args, **_kwargs):
            calls.append(list(args))
            return payload if args[0] == "normalize-window" else {"ok": True}

        with patch.object(sys, "platform", "win32"), patch.object(
            bridge, "_call_omniauto", side_effect=fake_call
        ):
            status = bridge.probe()

        self.assertEqual(status, ("ready", "logged_in"))
        self.assertEqual(bridge.last_probe_payload["geometry"], payload["geometry"])
        self.assertTrue(bridge.last_probe_payload["startup_window_normalization"]["ok"])
        self.assertEqual(calls, [["normalize-window"]])

    def test_probe_normalizes_only_once_then_uses_passive_status(self):
        bridge = RpaBridge(sidecar_script=Path("sidecar.py"))
        bridge.mode = "real"
        calls: list[list[str]] = []

        def fake_call(args, **_kwargs):
            calls.append(list(args))
            return {"ok": True}

        with patch.object(sys, "platform", "win32"), patch.object(
            bridge, "_call_omniauto", side_effect=fake_call
        ):
            self.assertEqual(bridge.probe(), ("ready", "logged_in"))
            self.assertEqual(bridge.probe(), ("ready", "logged_in"))

        self.assertEqual(calls, [["normalize-window"], ["status"]])

    def test_probe_retries_startup_normalization_until_wechat_appears(self):
        bridge = RpaBridge(sidecar_script=Path("sidecar.py"))
        bridge.mode = "real"
        calls: list[list[str]] = []
        normalize_attempts = 0

        def fake_call(args, **_kwargs):
            nonlocal normalize_attempts
            calls.append(list(args))
            if args[0] == "normalize-window":
                normalize_attempts += 1
                if normalize_attempts == 1:
                    return {"ok": False, "error_code": "WECHAT_WINDOW_NOT_FOUND"}
                return {"ok": True}
            if normalize_attempts == 1:
                return {"ok": False, "error_code": "WECHAT_WINDOW_NOT_FOUND"}
            return {"ok": True}

        with patch.object(sys, "platform", "win32"), patch.object(
            bridge, "_call_omniauto", side_effect=fake_call
        ):
            self.assertEqual(bridge.probe(), ("ready", "not_found"))
            self.assertEqual(bridge.probe(), ("ready", "logged_in"))
            self.assertEqual(bridge.probe(), ("ready", "logged_in"))

        self.assertEqual(
            calls,
            [
                ["normalize-window"],
                ["status"],
                ["normalize-window"],
                ["status"],
            ],
        )

    def test_probe_latches_non_not_found_normalization_failure_without_moving_again(self):
        bridge = RpaBridge(sidecar_script=Path("sidecar.py"))
        bridge.mode = "real"
        calls: list[list[str]] = []

        def fake_call(args, **_kwargs):
            calls.append(list(args))
            if args[0] == "normalize-window":
                return {
                    "ok": False,
                    "error_code": "WECHAT_UI_LAYOUT_STALE",
                    "state": "post_move_verification_failed",
                }
            return {"ok": True, "state": "wechat_ready"}

        with patch.object(sys, "platform", "win32"), patch.object(
            bridge, "_call_omniauto", side_effect=fake_call
        ):
            self.assertEqual(bridge.probe(), ("unavailable", "unknown"))
            self.assertEqual(bridge.probe(), ("unavailable", "unknown"))

        self.assertEqual(
            calls,
            [["normalize-window"], ["status"], ["status"]],
        )
        self.assertEqual(
            bridge.last_probe_payload["startup_window_normalization_state"],
            "failed_locked",
        )
        self.assertEqual(
            bridge.last_probe_payload["startup_window_normalization"]["error_code"],
            "WECHAT_UI_LAYOUT_STALE",
        )

    def test_new_transaction_reuses_current_calibration_without_capture_or_normalize(self):
        bridge = RpaBridge(sidecar_script=Path("sidecar.py"))
        bridge.mode = "real"
        calls: list[list[str]] = []

        def fake_call(args, **_kwargs):
            calls.append(list(args))
            return {
                "ok": True,
                "state": "startup_layout_calibration_current",
                "screenshot_call_count": 0,
                "ocr_call_count": 0,
            }

        with patch.object(sys, "platform", "win32"), patch.object(
            bridge, "_call_omniauto", side_effect=fake_call
        ):
            result = bridge.prepare_startup_layout_for_new_transaction()

        self.assertTrue(result["ok"])
        self.assertEqual(calls, [["calibration-status"]])

    def test_idle_changed_window_is_restored_once_before_new_transaction(self):
        bridge = RpaBridge(sidecar_script=Path("sidecar.py"))
        bridge.mode = "real"
        calls: list[list[str]] = []

        def fake_call(args, **_kwargs):
            calls.append(list(args))
            if args[0] == "calibration-status":
                return {
                    "ok": False,
                    "error_code": "WECHAT_UI_STARTUP_CALIBRATION_FAILED",
                    "state": "startup_layout_calibration_stale",
                }
            return {"ok": True, "state": "startup_layout_calibrated"}

        with patch.object(sys, "platform", "win32"), patch.object(
            bridge, "_call_omniauto", side_effect=fake_call
        ):
            result = bridge.prepare_startup_layout_for_new_transaction()

        self.assertTrue(result["ok"])
        self.assertTrue(result["restored_before_new_transaction"])
        self.assertEqual(calls, [["calibration-status"], ["normalize-window"]])

    def test_inflight_calibration_check_never_moves_or_recaptures_window(self):
        bridge = RpaBridge(sidecar_script=Path("sidecar.py"))
        bridge.mode = "real"
        calls: list[list[str]] = []

        def fake_call(args, **_kwargs):
            calls.append(list(args))
            return {
                "ok": False,
                "error_code": "WECHAT_UI_STARTUP_CALIBRATION_FAILED",
                "state": "startup_layout_calibration_stale",
                "screenshot_call_count": 0,
                "ocr_call_count": 0,
            }

        with patch.object(sys, "platform", "win32"), patch.object(
            bridge, "_call_omniauto", side_effect=fake_call
        ):
            result = bridge.verify_startup_layout_for_inflight_transaction()

        self.assertFalse(result["ok"])
        self.assertEqual(calls, [["calibration-status"]])
        self.assertEqual(result["screenshot_call_count"], 0)
        self.assertEqual(result["ocr_call_count"], 0)

    def test_all_ui_flows_do_not_request_window_policy_after_startup(self):
        bridge = RpaBridge(sidecar_script=Path("sidecar.py"))
        bridge.mode = "real"
        calls: list[list[str]] = []

        def fake_call(args, **_kwargs):
            calls.append(list(args))
            return {"ok": True}

        with patch.object(bridge, "_call_omniauto", side_effect=fake_call):
            bridge.locate_chat(
                display_name="CJT9V5X1",
                rpa_session_key="",
                remark_code="CJT9V5X1",
                target_mode="current",
            )
            bridge.get_messages(
                display_name="CJT9V5X1",
                rpa_session_key="",
                remark_code="CJT9V5X1",
                target_mode="current",
            )
            bridge.prepare_voice_action(
                display_name="CJT9V5X1",
                rpa_session_key="",
                remark_code="CJT9V5X1",
                target_mode="current",
            )
            bridge.send_reply(
                target="CJT9V5X1",
                rpa_session_key="session-1",
                text="测试回复",
                task_id="task-1",
            )

        self.assertEqual(
            [args[0] for args in calls],
            ["open-chat", "messages", "voice-transcribe", "send"],
        )
        self.assertTrue(all("--window-policy" not in args for args in calls))
        self.assertTrue(all("normalize-window" not in args for args in calls))

    def test_call_omniauto_protects_artifact_directory_until_process_finishes(self):
        with tempfile.TemporaryDirectory(prefix="chejin-active-artifact-") as tmp:
            artifact_dir = Path(tmp) / "artifacts" / "wechat_c2" / "messages" / "flow-1"
            artifact_dir.mkdir(parents=True)
            bridge = RpaBridge(sidecar_script=Path(__file__))

            def fake_process(args, timeout=30, cancel_check=None):
                del args, timeout, cancel_check
                self.assertEqual(bridge.active_artifact_dirs(), {artifact_dir.resolve()})
                return {"ok": True}

            with patch.object(
                bridge,
                "_call_omniauto_process",
                side_effect=fake_process,
            ):
                result = bridge._call_omniauto(
                    ["status", "--artifact-dir", str(artifact_dir)]
                )

            self.assertTrue(result["ok"])
            self.assertEqual(bridge.active_artifact_dirs(), set())

    def test_artifact_marker_failure_does_not_replace_successful_business_result(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        with patch.object(
            bridge,
            "_call_omniauto_process",
            return_value={"ok": True, "result_code": "invite_sent"},
        ), patch(
            "chejin_worker_client.rpa_bridge.record_artifact_outcome",
            side_effect=OSError("disk full"),
        ):
            result = bridge._call_omniauto(["status"])

        self.assertTrue(result["ok"])
        self.assertEqual(result["result_code"], "invite_sent")

    def test_failure_metadata_keeps_nested_ocr_and_layout_reasons(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        metadata = bridge._evidence_metadata(
            {
                "ok": False,
                "state": "wechat_main_surface_not_ready",
                "error_code": "PLUS_ENTRY_NOT_FOUND",
                "current_step": "preflight_main_surface_ready",
                "evidence": {
                    "pre_click_readiness": {
                        "reason": "shared_header_boundary_missing",
                        "ocr_count": 17,
                        "surface_readiness": {
                            "layout_confidence": 0.41,
                            "layout_conflicts": ["sidebar_header_unresolved"],
                            "no_clicks_performed": True,
                        },
                    }
                },
            },
            Path("artifacts/task-1"),
        )

        diagnostics = {
            item["path"]: item["value"]
            for item in metadata["rpa_failure_diagnostics"]
        }
        self.assertEqual(metadata["state"], "wechat_main_surface_not_ready")
        self.assertEqual(
            diagnostics["evidence.pre_click_readiness.reason"],
            "shared_header_boundary_missing",
        )
        self.assertEqual(
            diagnostics["evidence.pre_click_readiness.ocr_count"],
            17,
        )
        self.assertEqual(
            diagnostics[
                "evidence.pre_click_readiness.surface_readiness.layout_conflicts"
            ],
            ["sidebar_header_unresolved"],
        )

    def test_call_omniauto_terminates_running_sidecar_when_cancelled(self):
        with tempfile.TemporaryDirectory(prefix="chejin-cancel-sidecar-") as tmp:
            script = Path(tmp) / "slow_sidecar.py"
            script.write_text("import time\ntime.sleep(30)\nprint('{}')\n", encoding="utf-8")
            bridge = RpaBridge(sidecar_script=script)
            started = time.monotonic()

            result = bridge._call_omniauto(
                [],
                timeout=35,
                cancel_check=lambda: time.monotonic() - started >= 0.1,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "action_cancelled")
        self.assertEqual(result["error_code"], "WORKER_INTERRUPTED")
        self.assertLess(time.monotonic() - started, 3)

    def test_call_omniauto_preserves_specific_cancellation_reason(self):
        with tempfile.TemporaryDirectory(prefix="chejin-cancel-reason-") as tmp:
            script = Path(tmp) / "slow_sidecar.py"
            script.write_text("import time\ntime.sleep(30)\nprint('{}')\n", encoding="utf-8")
            bridge = RpaBridge(sidecar_script=script)

            result = bridge._call_omniauto(
                [],
                timeout=35,
                cancel_check=lambda: "TASK_LEASE_RENEW_FAILED",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "action_cancelled")
        self.assertEqual(result["error_code"], "TASK_LEASE_RENEW_FAILED")

    def test_default_sidecar_script_points_to_omniauto_sidecar(self):
        path = default_sidecar_script()

        self.assertTrue(path.exists())
        self.assertEqual(path.name, "wechat_win32_ocr_sidecar.py")
        self.assertIn("wechat_ai_customer_service", str(path))

    def test_voice_prepare_is_read_only_and_execute_carries_exact_token(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        calls: list[list[str]] = []

        def fake_call(args, **_kwargs):
            calls.append(list(args))
            return {"ok": True}

        with patch.object(bridge, "_call_omniauto", side_effect=fake_call):
            bridge.prepare_voice_action(
                display_name="CJK7M4Q2",
                rpa_session_key="",
                remark_code="CJK7M4Q2",
                target_mode="current",
                expected_confirmed_self_text="已确认的 AI 回复",
            )
            bridge.execute_voice_action(
                display_name="CJK7M4Q2",
                rpa_session_key="",
                canonical_voice_action_id="voice-action-1",
                reserved_worker_stable_id="worker-message-9",
                pre_frame_id="voice-frame-1",
                selected_pre_observation_id="voice-observation-1",
                selected_action_token="single-use-token",
                selected_target_fingerprint="target-fingerprint",
                remark_code="CJK7M4Q2",
                target_mode="current",
                expected_confirmed_self_text="已确认的 AI 回复",
                action_journal=Path("voice-action.json"),
            )

        prepare_args, execute_args = calls
        self.assertEqual(
            prepare_args[prepare_args.index("--voice-action-stage") + 1],
            "prepare",
        )
        self.assertNotIn("--selected-action-token", prepare_args)
        self.assertEqual(
            prepare_args[
                prepare_args.index("--expected-confirmed-self-text") + 1
            ],
            "已确认的 AI 回复",
        )
        self.assertEqual(
            execute_args[execute_args.index("--voice-action-stage") + 1],
            "execute",
        )
        self.assertEqual(
            execute_args[
                execute_args.index("--expected-confirmed-self-text") + 1
            ],
            "已确认的 AI 回复",
        )
        self.assertEqual(
            execute_args[execute_args.index("--selected-action-token") + 1],
            "single-use-token",
        )
        self.assertEqual(
            execute_args[
                execute_args.index("--selected-target-fingerprint") + 1
            ],
            "target-fingerprint",
        )

    def test_mock_bridge_emits_add_friend_steps_and_result(self):
        bridge = RpaBridge()
        bridge.mode = "mock"
        steps = []
        result = bridge.run_add_friend(
            Task(id="task-1", task_type="add_friend", status="running", phone="13800000000", remark="CJ-TEST"),
            steps.append,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.result_code, "invite_sent")
        self.assertEqual(steps[0].current_step, "checking_rpa")
        self.assertEqual(steps[-1].current_step, "invite_sent")

    def test_task_rejects_masked_phone_as_search_phone(self):
        task = Task.from_api(
            {
                "id": "task-1",
                "task_type": "add_friend",
                "status": "pending",
                "primary_phone_masked": "138****0000",
            }
        )

        self.assertIsNone(task.search_phone)
        self.assertFalse(task.has_searchable_contact)

    def test_real_bridge_calls_omniauto_entry_click_plan_with_formal_fields(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        captured = {"args": []}

        def fake_call_omniauto(args, timeout=30, cancel_check=None):
            captured["args"] = args
            captured["cancel_check"] = cancel_check
            self._confirm_add_friend_journal(args, task_id="task-1")
            return {
                "ok": True,
                "result_code": "invite_sent",
                "message": "已发送添加通讯录邀请",
                "review_path": "/tmp/review.html",
            }

        with patch.object(bridge, "_call_omniauto", side_effect=fake_call_omniauto):
            steps = []
            result = bridge.run_add_friend(
                Task(
                    id="task-1",
                    task_type="add_friend",
                    status="running",
                    phone="13800000000",
                    verify_message="您好，我是车金张伟",
                    remark_name="CJ-张伟-CJ8K2P-0000",
                    remark_code="CJ8K2P",
                ),
                steps.append,
            )

        self.assertTrue(result.ok)
        self.assertEqual(
            [step.current_step for step in steps[:2]],
            ["rpa_sidecar_starting", "wechat_preflight_starting"],
        )
        self.assertEqual(captured["args"][0], OMNIAUTO_ADD_FRIEND_ACTION)
        self.assertNotIn("--window-policy", captured["args"])
        self.assertNotIn("normalize-window", captured["args"])
        self.assertIn("--phone", captured["args"])
        self.assertIn("13800000000", captured["args"])
        self.assertIn("--verify-message", captured["args"])
        self.assertIn("您好，我是车金张伟", captured["args"])
        self.assertIn("--remark-name", captured["args"])
        self.assertIn("CJ-张伟-CJ8K2P-0000", captured["args"])
        self.assertIn("--remark-code", captured["args"])
        self.assertIn("CJ8K2P", captured["args"])
        self.assertIn("--action-journal", captured["args"])
        self.assertNotIn("--remark", captured["args"])
        self.assertNotIn("--sales-name", captured["args"])

    def test_real_add_friend_passes_cancel_check_to_sidecar_process(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        cancel_check = lambda: True

        with patch.object(
            bridge,
            "_call_omniauto",
            return_value={
                "ok": False,
                "error_code": "WORKER_INTERRUPTED",
                "failure_step": "rpa_execution",
            },
        ) as call_omniauto:
            bridge.run_add_friend(
                Task(
                    id="task-cancel",
                    task_type="add_friend",
                    status="running",
                    phone="13800000000",
                    verify_message="您好，我是车金张伟",
                    remark_name="CJ-张伟-CJ8K2P-0000",
                    remark_code="CJ8K2P",
                ),
                lambda step: None,
                cancel_check=cancel_check,
            )

        self.assertIs(call_omniauto.call_args.kwargs["cancel_check"], cancel_check)

    def test_real_bridge_rejects_missing_formal_payload_before_sidecar_call(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"

        with patch.object(bridge, "_call_omniauto") as call_omniauto:
            result = bridge.run_add_friend(
                Task(
                    id="task-1",
                    task_type="add_friend",
                    status="running",
                    phone="13800000000",
                    verify_message="您好，我是车金张伟",
                    remark_name="CJ-张伟-CJ8K2P-0000",
                ),
                lambda step: None,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TASK_PAYLOAD_INVALID")
        self.assertEqual(result.failure_step, "payload_validation")
        self.assertIn("remark_code is required", result.message)
        call_omniauto.assert_not_called()

    def test_real_bridge_send_reply_calls_omniauto_send_action(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        captured = {"args": [], "timeout": None}

        def fake_call_omniauto(args, timeout=30, cancel_check=None):
            captured["args"] = args
            captured["timeout"] = timeout
            captured["cancel_check"] = cancel_check
            return {
                "ok": True,
                "adapter": "win32_ocr",
                "state": "send_win32_rpa",
                "send_result": {"ok": True, "confirmed": True, "result": "sent"},
            }

        with patch.object(bridge, "_call_omniauto", side_effect=fake_call_omniauto):
            result = bridge.send_reply(
                target="CJTEST01许聪",
                rpa_session_key="wx:rpa:v1:a",
                text="服务端批准文本",
                task_id="task-chat",
                expected_context_guard={
                    "schema_version": 1,
                    "sequence": [],
                    "message_count": 0,
                    "bottom": None,
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["args"][0], "send")
        self.assertIn("--current-only", captured["args"])
        self.assertIn("--target", captured["args"])
        self.assertIn("CJTEST01许聪", captured["args"])
        self.assertIn("--session-key", captured["args"])
        self.assertIn("wx:rpa:v1:a", captured["args"])
        self.assertIn("--text", captured["args"])
        self.assertIn("服务端批准文本", captured["args"])
        self.assertIn("--expected-context-guard", captured["args"])
        self.assertEqual(captured["timeout"], 180)

    def test_real_bridge_get_messages_can_search_by_remark_code(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        captured = {"args": [], "timeout": None}

        def fake_call_omniauto(args, timeout=30, **_kwargs):
            captured["args"] = args
            captured["timeout"] = timeout
            return {"ok": True, "adapter": "win32_ocr", "state": "messages_ocr", "messages": []}

        with patch.object(bridge, "_call_omniauto", side_effect=fake_call_omniauto):
            result = bridge.get_messages(
                display_name="CJTEST01 许聪",
                rpa_session_key="",
                remark_code="CJTEST01",
                target_mode="search_by_remark_code",
                expected_confirmed_self_text="已确认的 AI 回复",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["args"][0], "messages")
        self.assertIn("--target", captured["args"])
        self.assertIn("CJTEST01 许聪", captured["args"])
        self.assertIn("--target-mode", captured["args"])
        self.assertIn("search_by_remark_code", captured["args"])
        self.assertIn("--remark-code", captured["args"])
        self.assertIn("CJTEST01", captured["args"])
        self.assertIn("--expected-confirmed-self-text", captured["args"])
        self.assertIn("已确认的 AI 回复", captured["args"])
        self.assertIn("--sidecar-run-id", captured["args"])
        self.assertNotIn("--session-key", captured["args"])
        self.assertIn("--max-duration-seconds", captured["args"])
        max_duration_index = captured["args"].index("--max-duration-seconds") + 1
        self.assertGreaterEqual(int(captured["args"][max_duration_index]), 75)
        self.assertGreaterEqual(int(captured["timeout"]), 150)
        self.assertIn("sidecar_run_id", result)
        self.assertIn(str(result["sidecar_run_id"]), str(result["artifact_dir"]))

    def test_real_bridge_list_sessions_returns_artifact_evidence(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        captured = {"args": []}

        def fake_call_omniauto(args, timeout=30, **_kwargs):
            captured["args"] = args
            artifact_dir = Path(args[args.index("--artifact-dir") + 1])
            artifact_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = artifact_dir / "sessions.png"
            screenshot_path.write_bytes(b"png")
            return {"ok": True, "adapter": "win32_ocr", "state": "sessions_ocr", "sessions": []}

        with patch.object(bridge, "_call_omniauto", side_effect=fake_call_omniauto):
            result = bridge.list_sessions()

        self.assertTrue(result["ok"])
        self.assertEqual(captured["args"][0], "sessions")
        self.assertIn("--sidecar-run-id", captured["args"])
        self.assertIn("--scan-id", captured["args"])
        self.assertEqual(
            result["sidecar_run_id"],
            captured["args"][captured["args"].index("--sidecar-run-id") + 1],
        )
        self.assertEqual(
            result["scan_id"],
            captured["args"][captured["args"].index("--scan-id") + 1],
        )
        self.assertIn("--artifact-dir", captured["args"])
        self.assertIn("artifact_dir", result)
        self.assertTrue(str(result["screenshot_path"]).endswith("sessions.png"))

    def test_real_bridge_locate_chat_can_search_by_remark_code(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        captured = {"args": [], "timeout": None}

        def fake_call_omniauto(args, timeout=30):
            captured["args"] = args
            captured["timeout"] = timeout
            return {"ok": True, "adapter": "win32_ocr", "state": "chat_target_confirmed"}

        with patch.object(bridge, "_call_omniauto", side_effect=fake_call_omniauto):
            result = bridge.locate_chat(
                display_name="CJTEST01 许聪",
                rpa_session_key="",
                remark_code="CJTEST01",
                target_mode="search_by_remark_code",
                expected_confirmed_self_text="已确认的 AI 回复",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["args"][0], "open-chat")
        self.assertIn("--target", captured["args"])
        self.assertIn("CJTEST01 许聪", captured["args"])
        self.assertIn("--target-mode", captured["args"])
        self.assertIn("search_by_remark_code", captured["args"])
        self.assertIn("--remark-code", captured["args"])
        self.assertIn("CJTEST01", captured["args"])
        self.assertIn("--expected-confirmed-self-text", captured["args"])
        self.assertIn("已确认的 AI 回复", captured["args"])
        self.assertIn("--sidecar-run-id", captured["args"])
        self.assertNotIn("--session-key", captured["args"])
        self.assertIn("sidecar_run_id", result)
        self.assertIn(str(result["sidecar_run_id"]), str(result["artifact_dir"]))

    def test_real_bridge_locate_chat_visible_passes_ascii_json_candidate(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        captured = {"args": [], "timeout": None}

        def fake_call_omniauto(args, timeout=30):
            captured["args"] = args
            captured["timeout"] = timeout
            return {"ok": True, "adapter": "win32_ocr", "state": "chat_target_confirmed"}

        candidate = {
            "name": "CJR8S5K3虾丸子大",
            "session_key": "wx:rpa:v1:8182b6ce08421443a07c",
            "center_y": 143.5,
            "row_fingerprint": "3e77b7c1848effea458e29b1",
            "preview": "[语音] 2\"",
        }
        with patch.object(bridge, "_call_omniauto", side_effect=fake_call_omniauto):
            result = bridge.locate_chat(
                display_name="CJR8S5K3虾丸子大",
                rpa_session_key="wx:rpa:v1:old",
                remark_code="CJR8S5K3",
                target_mode="visible",
                visible_session_candidate=candidate,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["args"][0], "open-chat")
        self.assertIn("--visible-session-candidate", captured["args"])
        raw = captured["args"][captured["args"].index("--visible-session-candidate") + 1]
        self.assertEqual(raw.encode("ascii", errors="ignore").decode("ascii"), raw)
        parsed = json.loads(raw)
        self.assertEqual(parsed["name"], candidate["name"])
        self.assertEqual(parsed["session_key"], candidate["session_key"])
        self.assertEqual(parsed["center_y"], candidate["center_y"])

    def test_real_bridge_emits_preflight_steps_before_sidecar_call(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        steps = []

        def fake_call_omniauto(args, timeout=30, cancel_check=None):
            del timeout, cancel_check
            self._confirm_add_friend_journal(
                args,
                task_id="task-preflight",
            )
            return {
                "ok": True,
                "result_code": "invite_sent",
                "message": "已发送添加通讯录邀请",
            }

        with patch.object(
            bridge,
            "_call_omniauto",
            side_effect=fake_call_omniauto,
        ) as call_omniauto:
            result = bridge.run_add_friend(
                Task(
                    id="task-preflight",
                    task_type="add_friend",
                    status="running",
                    phone="17368746889",
                    verify_message="您好，我是车金张伟",
                    remark_name="CJ-张伟-CJ8K2P-6889",
                    remark_code="CJ8K2P",
                ),
                steps.append,
            )

        self.assertTrue(result.ok)
        call_omniauto.assert_called_once()
        self.assertEqual(steps[0].current_step, "rpa_sidecar_starting")
        self.assertEqual(steps[1].current_step, "wechat_preflight_starting")
        self.assertIn("启动 OmniAuto", steps[0].title)
        self.assertIn("17368746889", steps[1].remark)

    def test_real_bridge_accepts_confirmed_journal_written_by_sidecar_process(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        task = Task(
            id="task-real-journal-subprocess",
            task_type="add_friend",
            status="running",
            phone="17368746889",
            verify_message="您好，我是车金张伟",
            remark_name="CJ-张伟-CJ8K2P-6889",
            remark_code="CJ8K2P",
        )
        sidecar_script = """
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("action")
parser.add_argument("--window-policy")
parser.add_argument("--phone")
parser.add_argument("--wechat")
parser.add_argument("--verify-message")
parser.add_argument("--remark-name")
parser.add_argument("--remark-code")
parser.add_argument("--artifact-dir")
parser.add_argument("--action-journal", required=True)
args = parser.parse_args()

path = Path(args.action_journal)
payload = json.loads(path.read_text(encoding="utf-8"))
source_key = next(iter(payload["items"]))
item = payload["items"][source_key]
item.update(
    {
        "action_phase": "confirmed",
        "business_state": "invite_sent",
        "business_result_confirmed": True,
        "terminal_payload": {
            "ok": True,
            "task_status": "completed",
            "result_code": "invite_sent",
            "error_code": "",
            "current_step": "task_completed",
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
)
payload["action_phase"] = "confirmed"
payload["updated_at"] = datetime.now(timezone.utc).isoformat()
temporary = path.with_suffix(path.suffix + ".sidecar-tmp")
temporary.write_text(json.dumps(payload), encoding="utf-8")
os.replace(temporary, path)
print(json.dumps({"ok": True, "task_status": "completed", "result_code": "invite_sent"}))
"""
        with tempfile.TemporaryDirectory(prefix="chejin-journal-sidecar-") as tmp:
            script_path = Path(tmp) / "journal_sidecar.py"
            script_path.write_text(sidecar_script, encoding="utf-8")
            bridge.sidecar_script = script_path
            result = bridge.run_add_friend(task, lambda step: None)

        self.assertTrue(result.ok)
        self.assertEqual(result.result_code, "invite_sent")
        self.assertEqual(result.evidence_metadata["action_phase"], "confirmed")
        self.assertTrue(result.evidence_metadata["recovered_from_action_journal"])

    def test_add_friend_triggered_journal_blocks_second_physical_attempt(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        task = Task(
            id="task-triggered",
            task_type="add_friend",
            status="running",
            phone="13800000000",
            verify_message="您好，我是车金张伟",
            remark_name="CJ-张伟-CJ8K2P-0000",
            remark_code="CJ8K2P",
        )
        journal_path = action_journal_path("add_friend", task.id)
        initialize_action_journal(
            journal_path,
            action_kind="add_friend",
            transaction_id=task.id,
            conversation_id=f"task:{task.id}",
            items=[{"journal_item_id": task.id, "action_local_id": task.id}],
        )
        update_action_journal_item(
            journal_path,
            journal_item_id=task.id,
            action_phase="trigger_attempted",
            business_state="invite_confirm_click_starting",
        )

        with patch.object(bridge, "_call_omniauto") as call_omniauto:
            result = bridge.run_add_friend(task, lambda step: None)

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "ADD_FRIEND_RESULT_UNKNOWN")
        self.assertTrue(result.evidence_metadata["manual_confirmation_required"])
        call_omniauto.assert_not_called()

    def test_add_friend_confirmed_journal_recovers_without_second_click(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        task = Task(
            id="task-confirmed",
            task_type="add_friend",
            status="running",
            phone="13800000000",
            verify_message="您好，我是车金张伟",
            remark_name="CJ-张伟-CJ8K2P-0000",
            remark_code="CJ8K2P",
        )
        journal_path = action_journal_path("add_friend", task.id)
        initialize_action_journal(
            journal_path,
            action_kind="add_friend",
            transaction_id=task.id,
            conversation_id=f"task:{task.id}",
            items=[{"journal_item_id": task.id, "action_local_id": task.id}],
        )
        update_action_journal_item(
            journal_path,
            journal_item_id=task.id,
            action_phase="confirmed",
            business_state="invite_sent",
            business_result_confirmed=True,
            terminal_payload={
                "ok": True,
                "result_code": "invite_sent",
                "current_step": "invite_confirm_clicked",
            },
        )

        with patch.object(bridge, "_call_omniauto") as call_omniauto:
            result = bridge.run_add_friend(task, lambda step: None)

        self.assertTrue(result.ok)
        self.assertEqual(result.result_code, "invite_sent")
        self.assertTrue(
            result.evidence_metadata["recovered_from_action_journal"]
        )
        call_omniauto.assert_not_called()

    def test_add_friend_confirmed_click_survives_post_click_screenshot_failure(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        task = Task(
            id="task-screenshot-failed",
            task_type="add_friend",
            status="running",
            phone="13800000000",
            verify_message="您好，我是车金张伟",
            remark_name="CJ-张伟-CJ8K2P-0000",
            remark_code="CJ8K2P",
        )

        def fake_call(args, **_kwargs):
            self._confirm_add_friend_journal(
                args,
                task_id=task.id,
            )
            return {
                "ok": False,
                "error_code": "SCREENSHOT_FAILED_AFTER_CONFIRM",
                "message": "post-click screenshot failed",
            }

        with patch.object(bridge, "_call_omniauto", side_effect=fake_call):
            result = bridge.run_add_friend(task, lambda step: None)

        self.assertTrue(result.ok)
        self.assertEqual(result.result_code, "invite_sent")
        self.assertTrue(result.evidence_metadata["recovered_from_action_journal"])

    def test_add_friend_confirmed_click_survives_post_click_ocr_failure(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        task = Task(
            id="task-ocr-failed",
            task_type="add_friend",
            status="running",
            phone="13800000000",
            verify_message="您好，我是车金张伟",
            remark_name="CJ-张伟-CJ8K2P-0000",
            remark_code="CJ8K2P",
        )

        def fake_call(args, **_kwargs):
            self._confirm_add_friend_journal(
                args,
                task_id=task.id,
            )
            return {
                "ok": False,
                "error_code": "OCR_FAILED_AFTER_CONFIRM",
                "message": "post-click OCR failed",
            }

        with patch.object(bridge, "_call_omniauto", side_effect=fake_call):
            result = bridge.run_add_friend(task, lambda step: None)

        self.assertTrue(result.ok)
        self.assertEqual(result.result_code, "invite_sent")
        self.assertTrue(result.evidence_metadata["recovered_from_action_journal"])

    def test_add_friend_explicit_restriction_overrides_confirmed_success(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        task = Task(
            id="task-account-restricted",
            task_type="add_friend",
            status="running",
            phone="13800000000",
            verify_message="您好，我是车金张伟",
            remark_name="CJ-张伟-CJ8K2P-0000",
            remark_code="CJ8K2P",
        )

        def fake_call(args, **_kwargs):
            self._confirm_add_friend_journal(
                args,
                task_id=task.id,
                ok=False,
                result_code="",
                error_code="ACCOUNT_RESTRICTED",
            )
            return {
                "ok": True,
                "task_status": "failed",
                "error_code": "ACCOUNT_RESTRICTED",
            }

        with patch.object(bridge, "_call_omniauto", side_effect=fake_call):
            result = bridge.run_add_friend(task, lambda step: None)

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "ACCOUNT_RESTRICTED")
        self.assertTrue(result.evidence_metadata["recovered_from_action_journal"])

    def test_add_friend_bridge_never_accepts_ok_true_with_failed_status(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        task = Task(
            id="task-contradictory-payload",
            task_type="add_friend",
            status="running",
            phone="13800000000",
            verify_message="您好，我是车金张伟",
            remark_name="CJ-张伟-CJ8K2P-0000",
            remark_code="CJ8K2P",
        )

        with patch.object(
            bridge,
            "_call_omniauto",
            return_value={
                "ok": True,
                "task_status": "failed",
                "result_code": "invite_sent",
                "error_code": "ACCOUNT_RESTRICTED",
                "current_step": "invite_confirm_clicked",
            },
        ):
            result = bridge.run_add_friend(task, lambda step: None)

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "ACCOUNT_RESTRICTED")

    def test_diagnostic_event_artifact_becomes_step_evidence_path(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        steps = []

        bridge._emit_steps(
            {
                "sidecar_run_id": "sidecar-run-001",
                "diagnostic_events": [
                    {
                        "step_id": "invite_form",
                        "title": "申请表单截图",
                        "status": "completed",
                        "artifacts": {
                            "raw": "C:/runtime/raw.png",
                            "annotated": "C:/runtime/annotated.png",
                        },
                    }
                ]
            },
            steps.append,
        )

        self.assertEqual(steps[0].current_step, "invite_form")
        self.assertEqual(steps[0].evidence_path, "C:/runtime/annotated.png")
        self.assertEqual(steps[0].sidecar_run_id, "sidecar-run-001")

    def test_mock_bridge_wechat_diagnostics_is_noop(self):
        bridge = RpaBridge()
        bridge.mode = "mock"

        result = bridge.diagnose_wechat()

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "mock")


if __name__ == "__main__":
    unittest.main()
