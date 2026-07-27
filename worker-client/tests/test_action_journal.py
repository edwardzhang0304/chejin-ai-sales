from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

os.environ.setdefault(
    "CHEJIN_WORKER_HOME",
    tempfile.mkdtemp(prefix="chejin-action-journal-test-"),
)

from chejin_worker_client.action_journal import (
    action_journal_phase,
    initialize_action_journal,
    read_action_journal,
)
from chejin_worker_client.storage import (
    clear_c2_action_journal,
    list_c2_action_journal,
)


class ActionJournalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "action.json"
        initialize_action_journal(
            self.path,
            action_kind="voice",
            transaction_id="voice-transaction",
            conversation_id="conversation-1",
            items=[
                {
                    "source_message_key": "message-1",
                    "physical_anchor_keys": ["voice-anchor-1"],
                }
            ],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _strong_kill_after_update(self, source: str) -> int:
        env = dict(os.environ)
        worker_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [worker_root, env.get("PYTHONPATH", "")])
        )
        result = subprocess.run(
            [sys.executable, "-c", source, str(self.path)],
            env=env,
            check=False,
        )
        return int(result.returncode)

    def test_kill_before_trigger_remains_safe_to_retry(self) -> None:
        returncode = self._strong_kill_after_update(
            "import os; os._exit(17)"
        )

        self.assertEqual(returncode, 17)
        self.assertEqual(action_journal_phase(self.path), "not_attempted")

    def test_kill_immediately_after_trigger_preserves_no_repeat_barrier(
        self,
    ) -> None:
        returncode = self._strong_kill_after_update(
            "import os,sys; "
            "from chejin_worker_client.action_journal import "
            "update_action_journal_item; "
            "update_action_journal_item(sys.argv[1],"
            "source_message_key='message-1',"
            "action_phase='trigger_attempted'); "
            "os._exit(18)"
        )

        self.assertEqual(returncode, 18)
        self.assertEqual(
            action_journal_phase(self.path),
            "trigger_attempted",
        )

    def test_kill_after_confirmation_preserves_business_result(self) -> None:
        returncode = self._strong_kill_after_update(
            "import os,sys; "
            "from chejin_worker_client.action_journal import "
            "update_action_journal_item; "
            "update_action_journal_item(sys.argv[1],"
            "source_message_key='message-1',"
            "action_phase='confirmed',"
            "business_state='completed',"
            "business_result_confirmed=True,"
            "terminal_payload={'state':'completed','content':'ok'}); "
            "os._exit(19)"
        )

        self.assertEqual(returncode, 19)
        payload = read_action_journal(self.path)
        item = payload["items"]["message-1"]
        self.assertEqual(item["action_phase"], "confirmed")
        self.assertTrue(item["business_result_confirmed"])
        self.assertEqual(item["terminal_payload"]["content"], "ok")

    def test_kill_after_worker_result_checkpoint_preserves_ledger_input(
        self,
    ) -> None:
        flow_id = "flow-killed-after-worker-result"
        returncode = self._strong_kill_after_update(
            "import os; "
            "from chejin_worker_client.storage import "
            "checkpoint_c2_action_outcomes; "
            "checkpoint_c2_action_outcomes("
            f"flow_id='{flow_id}',"
            "conversation_id='conversation-1',"
            "outcomes=[{"
            "'source_message_key':'message-1',"
            "'result':'completed',"
            "'evidence':{'action_kind':'voice'}"
            "}]); "
            "os._exit(20)"
        )

        self.assertEqual(returncode, 20)
        pending = list_c2_action_journal("conversation-1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["flow_id"], flow_id)
        self.assertEqual(
            pending[0]["outcome"]["source_message_key"],
            "message-1",
        )
        clear_c2_action_journal(flow_id)

    def test_action_phase_is_monotonic(self) -> None:
        from chejin_worker_client.action_journal import (
            update_action_journal_item,
        )

        update_action_journal_item(
            self.path,
            source_message_key="message-1",
            action_phase="confirmed",
            business_state="completed",
            business_result_confirmed=True,
        )
        update_action_journal_item(
            self.path,
            source_message_key="message-1",
            action_phase="not_attempted",
        )

        self.assertEqual(action_journal_phase(self.path), "confirmed")


if __name__ == "__main__":
    unittest.main()
