from __future__ import annotations

import ast
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
    commit_action_journal_item_identity,
    initialize_action_journal,
    read_action_journal,
    record_action_sequence_alignment,
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
            origin_read_run_id="read-voice-transaction",
            items=[
                {
                    "journal_item_id": "message-1",
                    "action_local_id": "message-1",
                    "physical_anchor_keys": ["voice-anchor-1"],
                }
            ],
            pre_frame_id="voice-frame-1",
            canonical_action_id="voice-transaction",
            reserved_worker_stable_id="worker-message-1",
            prepare_evidence={
                "pre_frame_id": "voice-frame-1",
                "selected_pre_observation_id": "voice-observation-1",
                "selected_action_token": "voice-token-1",
                "selected_target_fingerprint": "voice-fingerprint-1",
            },
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
            "journal_item_id='message-1',"
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
            "journal_item_id='message-1',"
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
            "origin_read_run_id='read-worker-result',"
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
        self.assertEqual(
            pending[0]["outcome"]["origin_read_run_id"],
            "read-worker-result",
        )
        clear_c2_action_journal(flow_id)

    def test_action_phase_is_monotonic(self) -> None:
        from chejin_worker_client.action_journal import (
            update_action_journal_item,
        )

        update_action_journal_item(
            self.path,
            journal_item_id="message-1",
            action_phase="confirmed",
            business_state="completed",
            business_result_confirmed=True,
        )
        update_action_journal_item(
            self.path,
            journal_item_id="message-1",
            action_phase="not_attempted",
            business_state="failed",
            business_result_confirmed=False,
            error_code="STALE_FAILURE",
            terminal_payload={"state": "failed"},
        )

        self.assertEqual(action_journal_phase(self.path), "confirmed")
        item = read_action_journal(self.path)["items"]["message-1"]
        self.assertEqual(item["business_state"], "completed")
        self.assertTrue(item["business_result_confirmed"])
        self.assertIsNone(item["error_code"])
        self.assertIsNone(item["terminal_payload"])

    def test_pre_action_sequence_and_reserved_id_survive_alignment_write(
        self,
    ) -> None:
        path = Path(self.tmp.name) / "sequence-action.json"
        pre_sequence = [
            {
                "identity_state": "selected_action",
                "pre_observation_id": "voice-before",
                "pre_sequence_index": 0,
                "sender_role": "customer",
                "message_type": "voice",
                "canonical_action_id": "voice-action-1",
                "reserved_worker_stable_id": "worker-message-8",
            }
        ]
        initialize_action_journal(
            path,
            action_kind="voice",
            transaction_id="voice-action-1",
            conversation_id="conversation-1",
            origin_read_run_id="read-1",
            canonical_action_id="voice-action-1",
            reserved_worker_stable_id="worker-message-8",
            prepare_evidence={
                "pre_frame_id": "frame-before",
                "selected_pre_observation_id": "voice-before",
                "selected_action_token": "voice-token-8",
                "selected_target_fingerprint": "voice-fingerprint-8",
            },
            pre_frame_id="frame-before",
            pre_action_identity_sequence=pre_sequence,
            items=[
                {
                    "journal_item_id": "voice-action-1",
                    "action_local_id": "voice-action-1",
                    "physical_anchor_keys": ["frame-local-anchor"],
                }
            ],
        )
        evidence = {
            "alignment_status": "unique",
            "pre_frame_id": "frame-before",
            "post_frame_id": "frame-after",
            "candidate_alignment_count": 1,
            "matched_pairs": [],
            "old_tail_fully_consumed": True,
            "new_suffix_observation_ids": [],
        }

        record_action_sequence_alignment(path, evidence)
        saved = read_action_journal(path)

        self.assertEqual(saved["schema_version"], 5)
        self.assertEqual(set(saved["items"]), {"voice-action-1"})
        self.assertEqual(
            saved["items"]["voice-action-1"]["journal_item_id"],
            "voice-action-1",
        )
        self.assertIsNone(
            saved["items"]["voice-action-1"]["source_message_key"]
        )
        self.assertEqual(saved["pre_action_identity_sequence"], pre_sequence)
        self.assertEqual(
            saved["reserved_worker_stable_id"], "worker-message-8"
        )
        self.assertEqual(
            saved["sequence_alignment_evidence"], evidence
        )

    def test_identity_commit_never_rekeys_the_action_local_item(self) -> None:
        committed = commit_action_journal_item_identity(
            self.path,
            journal_item_id="message-1",
            source_message_key="source:formal-message-1",
        )

        self.assertEqual(set(committed["items"]), {"message-1"})
        item = committed["items"]["message-1"]
        self.assertEqual(item["journal_item_id"], "message-1")
        self.assertEqual(item["action_local_id"], "message-1")
        self.assertEqual(
            item["source_message_key"],
            "source:formal-message-1",
        )

    def test_precommit_journal_rejects_source_only_identity_and_strips_nested_key(
        self,
    ) -> None:
        source_only = Path(self.tmp.name) / "source-only.json"
        with self.assertRaisesRegex(
            ValueError,
            "ACTION_JOURNAL_ITEMS_MISSING",
        ):
            initialize_action_journal(
                source_only,
                action_kind="image",
                transaction_id="image-source-only",
                conversation_id="conversation-1",
                origin_read_run_id="read-source-only",
                items=[{"source_message_key": "image-action-local"}],
            )

        replay = Path(self.tmp.name) / "replay-source.json"
        initialize_action_journal(
            replay,
            action_kind="image",
            transaction_id="image-replay",
            conversation_id="conversation-1",
            origin_read_run_id="read-image-replay",
            items=[
                {
                    "journal_item_id": "image-action-local",
                    "action_local_id": "image-action-local",
                    "replayable_observation": {
                        "observation_id": "image-observation",
                        "source_message_key": "image-action-local",
                        "source_message": {
                            "source_message_key": "image-action-local"
                        },
                    },
                }
            ],
        )
        saved = read_action_journal(replay)["items"]["image-action-local"]
        self.assertIsNone(saved["source_message_key"])
        self.assertNotIn(
            "source_message_key",
            saved["replayable_observation"],
        )
        self.assertNotIn(
            "source_message_key",
            saved["replayable_observation"]["source_message"],
        )

    def test_production_journal_initializers_have_no_formal_source_key_literal(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1] / "chejin_worker_client"
        violations = []
        for path in (root / "rpa_bridge.py", root / "task_runner.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for call in (
                node for node in ast.walk(tree) if isinstance(node, ast.Call)
            ):
                name = (
                    call.func.id
                    if isinstance(call.func, ast.Name)
                    else call.func.attr
                    if isinstance(call.func, ast.Attribute)
                    else ""
                )
                if name not in {
                    "initialize_action_journal",
                    "_start_irreversible_action_journal",
                }:
                    continue
                items = next(
                    (
                        keyword.value
                        for keyword in call.keywords
                        if keyword.arg == "items"
                    ),
                    None,
                )
                if items is None:
                    continue
                for mapping in (
                    node for node in ast.walk(items) if isinstance(node, ast.Dict)
                ):
                    keys = {
                        key.value
                        for key in mapping.keys
                        if isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                    }
                    if "source_message_key" in keys:
                        violations.append((path.name, call.lineno))
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
