from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CHEJIN_WORKER_HOME", tempfile.mkdtemp(prefix="chejin-worker-test-"))
os.environ["CHEJIN_RPA_MODE"] = "mock"

from chejin_worker_client.models import Task
from chejin_worker_client.rpa_bridge import OMNIAUTO_ADD_FRIEND_ACTION, RpaBridge, default_sidecar_script


class RpaBridgeTest(unittest.TestCase):
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
