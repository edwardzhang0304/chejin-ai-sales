from __future__ import annotations

import unittest

from chejin_worker_client.ui_state_mapping import runtime_process_screen, runtime_step_title


class WebUiStateMappingTests(unittest.TestCase):
    def test_background_runtime_steps_select_the_expected_screen(self) -> None:
        self.assertEqual(runtime_process_screen("first_screen_session_scan"), "scan-running")
        self.assertEqual(runtime_process_screen("state_target_message_read"), "target-read-running")
        self.assertEqual(runtime_process_screen("visible_hit_message_read"), "target-read-running")
        self.assertIsNone(runtime_process_screen("pre_send_refresh"))

    def test_runtime_step_codes_are_presented_as_chinese_business_copy(self) -> None:
        self.assertEqual(runtime_step_title("first_screen_session_scan", "fallback"), "正在扫描微信会话第一屏")
        self.assertEqual(runtime_step_title("c3_brain_waiting", "fallback"), "等待服务端生成回复")
        self.assertEqual(runtime_step_title("unknown_step", "正在执行任务"), "正在执行任务")


if __name__ == "__main__":
    unittest.main()
