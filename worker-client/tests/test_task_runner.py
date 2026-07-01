from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("CHEJIN_WORKER_HOME", tempfile.mkdtemp(prefix="chejin-worker-test-"))
os.environ.setdefault("CHEJIN_RPA_MODE", "mock")

from chejin_worker_client.api import ApiError
from chejin_worker_client.models import Binding, RpaResult, RpaStep, Task, WechatReadTarget, WorkerProfile
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.ui_lock import LOCK_FILE


class FakeApi:
    def __init__(self, task: Task | None, result_mode: str = "success", claim_response: Task | None = None) -> None:
        self.task = task
        self.claim_response = claim_response
        self.result_mode = result_mode
        self.events: list[str] = []
        self.evidence_payloads: list[dict] = []
        self.run_status_updates: list[str] = []
        self.claim_send_error: Exception | None = None
        self.scan_payloads: list[dict] = []
        self.message_payloads: list[dict] = []
        self.read_targets: list[WechatReadTarget] = []
        self.message_ingest_result = "ingested"

    def heartbeat(self, binding: Binding, **kwargs):
        self.events.append(f"heartbeat:{kwargs['rpa_component_status']}:{kwargs['wechat_status']}")
        return WorkerProfile(id=binding.worker_id, worker_name="测试 Worker", run_status=binding.run_status)

    def pull_task(self, binding: Binding):
        self.events.append("pull")
        return ("pending", self.task, None) if self.task else ("idle", None, "NO_PENDING_TASK")

    def claim_task(self, binding: Binding, task: Task):
        self.events.append(f"claim:{task.id}")
        return self.claim_response or task

    def report_step(self, binding: Binding, task_id: str, current_step: str, remark: str):
        self.events.append(f"step:{current_step}")
        return self.task

    def claim_send(self, binding: Binding, task: Task):
        self.events.append(f"claim_send:{task.reply_action_id}")
        if self.claim_send_error:
            raise self.claim_send_error
        from chejin_worker_client.models import ReplySendClaim

        return ReplySendClaim(
            reply_action_id=task.reply_action_id or "reply-action-1",
            task_id=task.id,
            send_token="send-token-1",
            reply_text="您好，可以继续沟通这台车。",
            reply_text_hash="3f0e8fbd416f953607afc8435940dcc2c1f7b8a2f8fa855d50ef9255d2e95e7e",
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            expire_at=None,
            raw={"remark_code": "CJTEST01", "display_name": "CJTEST01 许聪"},
        )

    def sent_ack(self, binding: Binding, claim, **kwargs):
        self.events.append(f"sent_ack:{kwargs['send_result']}:{kwargs.get('error_code')}")
        return {"task": self.task, "ack": kwargs}

    def complete_invite_sent(self, binding: Binding, task_id: str):
        self.events.append(f"complete_invite_sent:{task_id}")
        return self.task

    def complete_already_friend(self, binding: Binding, task_id: str):
        self.events.append(f"complete_already_friend:{task_id}")
        return self.task

    def fail_task(self, binding: Binding, task_id: str, error_code: str, failure_step: str | None, message: str):
        self.events.append(f"fail:{error_code}:{failure_step}")
        return self.task

    def upload_evidence(self, binding: Binding, task_id: str, content: str, **kwargs):
        self.evidence_payloads.append({"task_id": task_id, "content": content, **kwargs})
        self.events.append(f"evidence:{kwargs.get('error_code')}")

    def set_run_status(self, binding: Binding, run_status: str):
        self.run_status_updates.append(run_status)
        self.events.append(f"run_status:{run_status}")
        return WorkerProfile(id=binding.worker_id, worker_name="测试 Worker", run_status=run_status)

    def post_wechat_session_scan_result(self, binding: Binding, payload: dict):
        self.scan_payloads.append(payload)
        self.events.append(f"scan:{len(payload.get('sessions') or [])}:{payload.get('error_code')}")
        session = (payload.get("sessions") or [{}])[0] if payload.get("sessions") else {}
        return {
            "bound_count": 1,
            "bindings": [
                {
                    "conversation_id": "conv-1",
                    "lead_id": "lead-1",
                    "sales_id": "sales-1",
                    "remark_code": "CJTEST01",
                    "rpa_session_key": session.get("rpa_session_key") or "wx:rpa:v1:a",
                    "display_name": session.get("display_name") or "CJTEST01 许聪",
                    "row_fingerprint": session.get("row_fingerprint") or {"title_text": "CJTEST01 许聪"},
                    "ocr_confidence": session.get("ocr_confidence") or 0.98,
                    "can_ingest_messages": True,
                }
            ],
        }

    def get_wechat_read_targets(self, binding: Binding, *, limit: int = 20):
        self.events.append(f"read_targets:{limit}")
        return self.read_targets[:limit]

    def post_wechat_messages_ingest(self, binding: Binding, payload: dict):
        self.message_payloads.append(payload)
        self.events.append(f"ingest:{len(payload.get('messages') or [])}")
        messages = payload.get("messages") or []
        return {
            "ingested_count": len(messages) if self.message_ingest_result == "ingested" else 0,
            "results": [
                {"dedupe_key": item.get("dedupe_key"), "ingest_result": self.message_ingest_result}
                for item in messages
                if isinstance(item, dict)
            ],
        }


class FakeBridge:
    def __init__(self, result: RpaResult, send_payload: dict | None = None, message_sender_role: str = "unknown") -> None:
        self.result = result
        self.message_sender_role = message_sender_role
        self.tasks: list[Task] = []
        self.sent_replies: list[dict] = []
        self.session_scans: list[dict] = []
        self.message_reads: list[dict] = []
        self.send_payload = send_payload or {"ok": True, "adapter": "mock", "state": "send_mock", "sidecar_run_id": "send-run-1", "send_result": {"ok": True}}

    def probe(self):
        return "ready", "logged_in"

    def run_add_friend(self, task: Task, emit_step):
        self.tasks.append(task)
        emit_step(RpaStep(current_step="checking_rpa", title="检查自动化组件", remark="自动化组件可用"))
        emit_step(RpaStep(current_step="invite_sent", title="发送添加通讯录邀请", remark="已点击发送"))
        return self.result

    def send_reply(self, *, target: str, rpa_session_key: str, text: str, task_id: str):
        self.sent_replies.append({"target": target, "rpa_session_key": rpa_session_key, "text": text, "task_id": task_id})
        return self.send_payload

    def list_sessions(self):
        self.session_scans.append({})
        return {
            "ok": True,
            "adapter": "mock",
            "state": "sessions_mock",
            "sidecar_run_id": "session-run-1",
            "sessions": [
                {
                    "name": "CJTEST01 许聪",
                    "session_key": "wx:rpa:v1:a",
                    "row_fingerprint": {"title_text": "CJTEST01 许聪"},
                    "content": "你好",
                    "unread_signal": True,
                    "ocr_confidence": 0.98,
                }
            ],
        }

    def get_messages(self, *, display_name: str, rpa_session_key: str, **kwargs):
        self.message_reads.append({"display_name": display_name, "rpa_session_key": rpa_session_key, **kwargs})
        return {
            "ok": True,
            "adapter": "mock",
            "state": "messages_mock",
            "sidecar_run_id": "message-run-1",
            "messages": [
                {"id": "wx-msg-1", "sender_role": self.message_sender_role, "type": "text", "content": "你好", "ocr_confidence": 0.98}
            ],
        }


class TaskRunnerTest(unittest.TestCase):
    def setUp(self):
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass

    def make_runner(self, api: FakeApi, bridge: FakeBridge):
        seen = {"profiles": [], "statuses": [], "steps": [], "tasks": [], "results": [], "errors": []}
        runner = TaskRunner(
            api,  # type: ignore[arg-type]
            bridge,  # type: ignore[arg-type]
            on_profile=lambda item: seen["profiles"].append(item),
            on_status=lambda item: seen["statuses"].append(item),
            on_step=lambda item: seen["steps"].append(item),
            on_task=lambda item: seen["tasks"].append(item),
            on_result=lambda item: seen["results"].append(item),
            on_error=lambda item: seen["errors"].append(item),
        )
        return runner, seen

    def test_tick_once_claims_reports_steps_and_completes(self):
        task = Task(id="task-1", task_type="add_friend", status="pending", phone="13800000000")
        api = FakeApi(task)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="已发送添加通讯录邀请"))
        runner, seen = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertIn("online", seen["statuses"])
        self.assertEqual([step.current_step for step in seen["steps"]], ["checking_rpa", "invite_sent"])
        self.assertIn("claim:task-1", api.events)
        self.assertIn("step:checking_rpa", api.events)
        self.assertIn("complete_invite_sent:task-1", api.events)
        self.assertIsNone(runner.current_task)

    def test_claim_response_does_not_drop_plain_contact_from_pull_payload(self):
        pulled_task = Task(id="task-plain", task_type="add_friend", status="pending", phone="17368746889")
        masked_claim_task = Task(id="task-plain", task_type="add_friend", status="running", phone="173****6889")
        api = FakeApi(pulled_task, claim_response=masked_claim_task)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="已发送添加通讯录邀请"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertEqual(bridge.tasks[0].search_phone, "17368746889")

    def test_claim_response_does_not_drop_formal_rpa_fields_from_pull_payload(self):
        pulled_task = Task(
            id="task-formal",
            task_type="add_friend",
            status="pending",
            phone="17368746889",
            verify_message="您好，我是车金张伟",
            remark_name="CJ-张伟-CJ8K2P-6889",
            remark_code="CJ8K2P",
            remark_code_valid=True,
        )
        masked_claim_task = Task(id="task-formal", task_type="add_friend", status="running", phone="173****6889")
        api = FakeApi(pulled_task, claim_response=masked_claim_task)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="已发送添加通讯录邀请"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertEqual(bridge.tasks[0].verify_message, "您好，我是车金张伟")
        self.assertEqual(bridge.tasks[0].remark_name, "CJ-张伟-CJ8K2P-6889")
        self.assertEqual(bridge.tasks[0].remark_code, "CJ8K2P")
        self.assertTrue(bridge.tasks[0].remark_code_valid)

    def test_success_uploads_omniauto_evidence_metadata_when_present(self):
        task = Task(id="task-evidence", task_type="add_friend", status="pending", phone="13800000000")
        api = FakeApi(task)
        bridge = FakeBridge(
            RpaResult(
                ok=True,
                result_code="invite_sent",
                message="已发送添加通讯录邀请",
                evidence_path="C:/runtime/latest/review.html",
                evidence_metadata={"review_path": "C:/runtime/latest/review.html", "current_step": "invite_sent"},
            )
        )
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertIn("evidence:None", api.events)
        self.assertEqual(api.evidence_payloads[0]["evidence_path"], "C:/runtime/latest/review.html")
        self.assertEqual(api.evidence_payloads[0]["metadata"]["current_step"], "invite_sent")

    def test_environment_failure_pauses_worker_after_failed_report(self):
        task = Task(id="task-2", task_type="add_friend", status="pending", phone="13800000000")
        api = FakeApi(task)
        bridge = FakeBridge(
            RpaResult(
                ok=False,
                error_code="WECHAT_WINDOW_NOT_FOUND",
                failure_step="wechat_window_found",
                message="微信窗口未找到",
            )
        )
        runner, seen = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        runner.binding = binding

        runner.tick_once()

        self.assertIn("fail:WECHAT_WINDOW_NOT_FOUND:wechat_window_found", api.events)
        self.assertIn("evidence:WECHAT_WINDOW_NOT_FOUND", api.events)
        self.assertIn("paused", api.run_status_updates)
        self.assertEqual(binding.run_status, "paused")
        self.assertTrue(any("运行环境异常" in item for item in seen["errors"]))

    def test_phone_not_found_does_not_pause_worker_after_failed_report(self):
        task = Task(id="task-404", task_type="add_friend", status="pending", phone="13800000000")
        api = FakeApi(task)
        bridge = FakeBridge(
            RpaResult(
                ok=False,
                error_code="PHONE_NOT_FOUND",
                failure_step="phone_search_finished",
                message="搜索不到客户",
            )
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        runner.binding = binding

        runner.tick_once()

        self.assertIn("fail:PHONE_NOT_FOUND:phone_search_finished", api.events)
        self.assertNotIn("paused", api.run_status_updates)
        self.assertEqual(binding.run_status, "running")

    def test_paused_worker_only_sends_heartbeat(self):
        task = Task(id="task-3", task_type="add_friend", status="pending", phone="13800000000")
        api = FakeApi(task)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="已发送添加通讯录邀请"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner.tick_once()

        self.assertIn("heartbeat:ready:logged_in", api.events)
        self.assertNotIn("pull", api.events)

    def test_schedule_paused_worker_only_sends_heartbeat(self):
        task = Task(id="task-schedule", task_type="add_friend", status="pending", phone="13800000000")
        api = FakeApi(task)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="已发送添加通讯录邀请"))
        runner, _ = self.make_runner(api, bridge)
        runner.can_pull_tasks = lambda: False
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertIn("heartbeat:ready:logged_in", api.events)
        self.assertNotIn("pull", api.events)

    def test_chat_reply_claim_send_then_sends_and_acks(self):
        task = Task(id="task-chat", task_type="chat_reply", status="pending", reply_action_id="reply-action-1", customer_name="CJTEST01许聪")
        api = FakeApi(task)
        api.message_ingest_result = "duplicated"
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertIn("claim:task-chat", api.events)
        self.assertIn("claim_send:reply-action-1", api.events)
        self.assertEqual(bridge.sent_replies[0]["text"], "您好，可以继续沟通这台车。")
        self.assertEqual(bridge.sent_replies[0]["rpa_session_key"], "wx:rpa:v1:a")
        self.assertIn("sent_ack:sent:None", api.events)

    def test_chat_reply_pre_send_refresh_supersedes_when_new_customer_message_arrives(self):
        task = Task(id="task-chat-new-message", task_type="chat_reply", status="pending", reply_action_id="reply-action-1", customer_name="CJTEST01许聪")
        api = FakeApi(task)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")
        runner, seen = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertIn("claim_send:reply-action-1", api.events)
        self.assertIn("ingest:1", api.events)
        self.assertEqual(bridge.sent_replies, [])
        self.assertTrue(any(result and result.error_code == "REPLY_ACTION_SUPERSEDED_BY_PRE_SEND_REFRESH" for result in seen["results"]))

    def test_chat_reply_claim_send_failure_does_not_touch_wechat(self):
        task = Task(id="task-chat-fail", task_type="chat_reply", status="pending", reply_action_id="reply-action-1")
        api = FakeApi(task)
        api.claim_send_error = ApiError("REPLY_ACTION_EXPIRED", "回复动作已过期", 409)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertIn("claim_send:reply-action-1", api.events)
        self.assertEqual(bridge.sent_replies, [])
        self.assertIn("fail:REPLY_ACTION_EXPIRED:claim_send", api.events)

    def test_chat_reply_timeout_reports_unknown_ack(self):
        task = Task(id="task-chat-unknown", task_type="chat_reply", status="pending", reply_action_id="reply-action-1")
        api = FakeApi(task)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="invite_sent", message="unused"),
            send_payload={"ok": False, "error_code": "RPA_SIDECAR_TIMEOUT", "current_step": "rpa_sidecar_timeout", "state": "send_maybe_sent"},
        )
        api.message_ingest_result = "duplicated"
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertIn("sent_ack:unknown:RPA_SIDECAR_TIMEOUT", api.events)
        self.assertTrue(any(result and result.error_code == "RPA_SIDECAR_TIMEOUT" for result in _["results"]))

    def test_chat_reply_pre_send_refresh_blocks_stale_reply_when_customer_message_ingested(self):
        task = Task(id="task-chat-stale", task_type="chat_reply", status="pending", reply_action_id="reply-action-1", customer_name="CJTEST01 许聪")
        api = FakeApi(task)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")
        runner, seen = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertIn("claim_send:reply-action-1", api.events)
        self.assertIn("ingest:1", api.events)
        self.assertEqual(bridge.sent_replies, [])
        self.assertTrue(any(result and result.error_code == "REPLY_ACTION_SUPERSEDED_BY_PRE_SEND_REFRESH" for result in seen["results"]))

    def test_c2_visible_scan_reports_first_screen_sessions(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")
        runner.binding = binding

        runner._scan_wechat_sessions(binding, reason="unit")

        self.assertIn("scan:1:None", api.events)
        self.assertEqual(api.scan_payloads[0]["sessions"][0]["remark_code_candidates"], ["CJTEST01"])
        self.assertEqual(bridge.session_scans[0], {})
        self.assertNotIn("scan_mode", api.scan_payloads[0]["evidence"])

    def test_c2_message_read_allows_target_without_row_fingerprint(self):
        api = FakeApi(None)
        api.read_targets = [WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJTEST01 许聪", remark_code="CJTEST01")]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual(bridge.message_reads[0]["display_name"], "CJTEST01 许聪")
        self.assertEqual(bridge.message_reads[0]["target_mode"], "search_by_remark_code")
        self.assertEqual(bridge.message_reads[0]["remark_code"], "CJTEST01")
        self.assertEqual(bridge.message_reads[0]["rpa_session_key"], "")
        self.assertIn("ingest:1", api.events)
        self.assertEqual(api.message_payloads[0]["evidence"]["target_row_fingerprint"], {})

    def test_c2_message_read_skips_target_without_remark_code(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual(bridge.message_reads, [])
        self.assertNotIn("ingest:1", api.events)
        self.assertEqual(runner.c2_stats["last_error"], "C2_TARGET_REMARK_CODE_MISSING")

    def test_c2_message_read_skips_target_without_conversation_id(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual(bridge.message_reads, [])
        self.assertNotIn("ingest:1", api.events)
        self.assertEqual(runner.c2_stats["last_error"], "C2_TARGET_CONVERSATION_ID_MISSING")

    def test_c2_message_read_skips_target_without_locator(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="",
                display_name="",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual(bridge.message_reads, [])
        self.assertNotIn("ingest:1", api.events)
        self.assertEqual(runner.c2_stats["last_error"], "C2_TARGET_LOCATOR_MISSING")

    def test_c2_target_dedupe_key_uses_identity_pair(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)

        self.assertEqual(
            runner._target_dedupe_key(
                WechatReadTarget(
                    conversation_id="conv-1",
                    rpa_session_key="wx:rpa:v1:a",
                    display_name="CJTEST01 许聪",
                    remark_code="CJTEST01",
                )
            ),
            "conversation:conv-1:remark_code:CJTEST01",
        )
        self.assertTrue(
            runner._target_dedupe_key(
                WechatReadTarget(conversation_id="", rpa_session_key="wx:rpa:v1:a", display_name="CJTEST01 许聪", remark_code="CJTEST01")
            ).startswith("invalid:")
        )

    def test_c2_message_read_uses_read_targets_only_and_ingests(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual(bridge.message_reads[0]["display_name"], "CJTEST01 许聪")
        self.assertEqual(bridge.message_reads[0]["rpa_session_key"], "")
        self.assertEqual(bridge.message_reads[0]["remark_code"], "CJTEST01")
        self.assertEqual(bridge.message_reads[0]["target_mode"], "search_by_remark_code")
        self.assertIn("ingest:1", api.events)

    def test_c2_message_read_failure_enters_cooldown_before_retry(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def failing_get_messages(*, display_name: str, rpa_session_key: str, **kwargs):
            bridge.message_reads.append({"display_name": display_name, "rpa_session_key": rpa_session_key, **kwargs})
            return {
                "ok": False,
                "error_code": "TARGET_NOT_CONFIRMED_FOR_MESSAGES",
                "sidecar_run_id": "message-failed-1",
                "artifact_dir": "C:/artifact/message-failed-1",
            }

        bridge.get_messages = failing_get_messages  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)
        runner._read_bound_wechat_messages(binding)

        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(runner.c2_stats["last_error"], "TARGET_NOT_CONFIRMED_FOR_MESSAGES")

    def test_c2_visible_hit_reads_before_state_target_and_dedupes_same_round(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(bridge.message_reads[0]["target_mode"], "visible")
        self.assertEqual(bridge.message_reads[0]["rpa_session_key"], "wx:rpa:v1:a")
        self.assertEqual(bridge.message_reads[0]["remark_code"], "CJTEST01")
        self.assertEqual(api.events.count("ingest:1"), 1)
        self.assertIn("read_targets:20", api.events)

    def test_c2_scan_interrupted_when_high_priority_task_active(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")
        runner.current_task = Task(id="task-chat", task_type="chat_reply", status="running")

        runner._scan_wechat_sessions(binding, reason="unit")

        self.assertEqual(runner.c2_stats["last_error"], "C2_SCAN_SKIPPED_BY_HIGH_PRIORITY_ACTION")
        self.assertEqual(api.scan_payloads, [])
        self.assertEqual(bridge.session_scans, [])

    def test_c2_listener_scans_after_start_when_wechat_ready(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        runner.last_rpa_component_status = "ready"
        runner.last_wechat_status = "logged_in"
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertTrue(api.scan_payloads)
        self.assertNotIn("scan_type", api.scan_payloads[0])


if __name__ == "__main__":
    unittest.main()
