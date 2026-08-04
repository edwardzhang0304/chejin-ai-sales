from __future__ import annotations

import unittest
from pathlib import Path

from chejin_worker_client.models import task_type_title


ROOT = Path(__file__).resolve().parents[1]


class TaskDisplayTests(unittest.TestCase):
    def test_task_titles_are_driven_by_task_type(self) -> None:
        self.assertEqual(task_type_title("add_friend"), "添加通讯录邀请")
        self.assertEqual(task_type_title("chat_reply"), "AI 自动回复")
        self.assertEqual(task_type_title("future_task"), "Worker 任务")

    def test_both_client_views_use_the_shared_task_title_rule(self) -> None:
        desktop = (ROOT / "chejin_worker_client" / "ui.py").read_text(encoding="utf-8")
        web = (ROOT / "chejin_worker_client" / "web_ui.py").read_text(encoding="utf-8")

        self.assertIn("task_type_title(display_task.task_type)", desktop)
        self.assertIn('"title": task_type_title(task.task_type)', web)
        self.assertNotIn("Worker 已领取 add_friend 任务", desktop + web)
        self.assertNotIn("Worker 正在执行 add_friend 任务", desktop + web)


if __name__ == "__main__":
    unittest.main()
