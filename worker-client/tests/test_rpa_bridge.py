from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CHEJIN_WORKER_HOME", tempfile.mkdtemp(prefix="chejin-worker-test-"))
os.environ["CHEJIN_RPA_MODE"] = "mock"

from chejin_worker_client.models import Task
from chejin_worker_client.rpa_bridge import OMNIAUTO_ADD_FRIEND_ACTION, RpaBridge, default_sidecar_script


class RpaBridgeTest(unittest.TestCase):
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
        self.assertEqual(result["state"], "c2_action_cancelled")
        self.assertEqual(result["error_code"], "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS")
        self.assertLess(time.monotonic() - started, 3)

    def test_default_sidecar_script_points_to_omniauto_sidecar(self):
        path = default_sidecar_script()

        self.assertTrue(path.exists())
        self.assertEqual(path.name, "wechat_win32_ocr_sidecar.py")
        self.assertIn("wechat_ai_customer_service", str(path))

    def test_mock_bridge_emits_add_friend_steps_and_result(self):
        bridge = RpaBridge()
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

        def fake_call_omniauto(args, timeout=30):
            captured["args"] = args
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
            [step.current_step for step in steps[:3]],
            ["rpa_sidecar_starting", "wechat_preflight_starting", "operator_guard_starting"],
        )
        self.assertEqual(captured["args"][0], OMNIAUTO_ADD_FRIEND_ACTION)
        self.assertIn("--phone", captured["args"])
        self.assertIn("13800000000", captured["args"])
        self.assertIn("--verify-message", captured["args"])
        self.assertIn("您好，我是车金张伟", captured["args"])
        self.assertIn("--remark-name", captured["args"])
        self.assertIn("CJ-张伟-CJ8K2P-0000", captured["args"])
        self.assertIn("--remark-code", captured["args"])
        self.assertIn("CJ8K2P", captured["args"])
        self.assertNotIn("--remark", captured["args"])
        self.assertNotIn("--sales-name", captured["args"])

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

        def fake_call_omniauto(args, timeout=30):
            captured["args"] = args
            captured["timeout"] = timeout
            return {"ok": True, "adapter": "win32_ocr", "state": "send_win32_rpa", "send_result": {"ok": True}}

        with patch.object(bridge, "_call_omniauto", side_effect=fake_call_omniauto):
            result = bridge.send_reply(target="CJTEST01许聪", rpa_session_key="wx:rpa:v1:a", text="服务端批准文本", task_id="task-chat")

        self.assertTrue(result["ok"])
        self.assertEqual(captured["args"][0], "send")
        self.assertIn("--target", captured["args"])
        self.assertIn("CJTEST01许聪", captured["args"])
        self.assertIn("--session-key", captured["args"])
        self.assertIn("wx:rpa:v1:a", captured["args"])
        self.assertIn("--text", captured["args"])
        self.assertIn("服务端批准文本", captured["args"])
        self.assertEqual(captured["timeout"], 180)

    def test_real_bridge_get_messages_can_search_by_remark_code(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        captured = {"args": [], "timeout": None}

        def fake_call_omniauto(args, timeout=30):
            captured["args"] = args
            captured["timeout"] = timeout
            return {"ok": True, "adapter": "win32_ocr", "state": "messages_ocr", "messages": []}

        with patch.object(bridge, "_call_omniauto", side_effect=fake_call_omniauto):
            result = bridge.get_messages(
                display_name="CJTEST01 许聪",
                rpa_session_key="",
                remark_code="CJTEST01",
                target_mode="search_by_remark_code",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["args"][0], "messages")
        self.assertIn("--target", captured["args"])
        self.assertIn("CJTEST01 许聪", captured["args"])
        self.assertIn("--target-mode", captured["args"])
        self.assertIn("search_by_remark_code", captured["args"])
        self.assertIn("--remark-code", captured["args"])
        self.assertIn("CJTEST01", captured["args"])
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

        def fake_call_omniauto(args, timeout=30):
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
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["args"][0], "open-chat")
        self.assertIn("--target", captured["args"])
        self.assertIn("CJTEST01 许聪", captured["args"])
        self.assertIn("--target-mode", captured["args"])
        self.assertIn("search_by_remark_code", captured["args"])
        self.assertIn("--remark-code", captured["args"])
        self.assertIn("CJTEST01", captured["args"])
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

    def test_real_bridge_voice_transcribe_current_chat_validates_target_without_switching(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        captured = {"args": [], "timeout": None}

        def fake_call_omniauto(args, timeout=30, cancel_check=None):
            captured["args"] = args
            captured["timeout"] = timeout
            return {"ok": True, "adapter": "win32_ocr", "state": "voice_transcribe_no_new_text", "transcribed_messages": []}

        with patch.object(bridge, "_call_omniauto", side_effect=fake_call_omniauto):
            result = bridge.voice_transcribe(
                display_name="CJTEST01 许聪",
                rpa_session_key="",
                remark_code="CJTEST01",
                target_mode="current",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["args"][0], "voice-transcribe")
        self.assertIn("--target", captured["args"])
        self.assertEqual(captured["args"][captured["args"].index("--target") + 1], "CJTEST01 许聪")
        self.assertIn("--target-mode", captured["args"])
        self.assertEqual(captured["args"][captured["args"].index("--target-mode") + 1], "current")
        self.assertIn("--remark-code", captured["args"])
        self.assertIn("--max-duration-seconds", captured["args"])
        self.assertIn("--sidecar-run-id", captured["args"])
        self.assertNotIn("--session-key", captured["args"])
        self.assertGreaterEqual(int(captured["timeout"]), 150)
        self.assertIn("sidecar_run_id", result)
        self.assertIn(str(result["sidecar_run_id"]), str(result["artifact_dir"]))

    def test_real_bridge_emits_preflight_steps_before_sidecar_call(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        steps = []

        with patch.object(
            bridge,
            "_call_omniauto",
            return_value={"ok": True, "result_code": "invite_sent", "message": "已发送添加通讯录邀请"},
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
        self.assertEqual(steps[2].current_step, "operator_guard_starting")
        self.assertIn("启动 OmniAuto", steps[0].title)
        self.assertIn("17368746889", steps[1].remark)
        self.assertIn("键鼠守护", steps[2].title)

    def test_diagnostic_event_artifact_becomes_step_evidence_path(self):
        bridge = RpaBridge(sidecar_script=Path(__file__))
        steps = []

        bridge._emit_steps(
            {
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

    def test_mock_bridge_wechat_diagnostics_is_noop(self):
        bridge = RpaBridge()

        result = bridge.diagnose_wechat()

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "mock")


if __name__ == "__main__":
    unittest.main()
