from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

os.environ.setdefault("CHEJIN_WORKER_HOME", tempfile.mkdtemp(prefix="chejin-worker-test-"))
os.environ.setdefault("CHEJIN_RPA_MODE", "mock")

from chejin_worker_client.api import ApiError
from chejin_worker_client.models import Binding, RpaResult, RpaStep, Task, WechatReadTarget, WorkerProfile
from chejin_worker_client.task_runner import C2_RECENT_VISIBLE_CACHE_TTL_SECONDS, TaskRunner
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
        targets = self.read_targets[:limit]
        for target in targets:
            if target.conversation_id and target.remark_code and not target.authorization_revision:
                target.authorization_revision = f"revision-{target.conversation_id}"
        return targets

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
        self.get_messages_payloads: list[dict] = []
        self.locate_chats: list[dict] = []
        self.locate_payloads: list[dict] = []
        self.voice_transcribes: list[dict] = []
        self.c2_operation_order: list[str] = []
        self.probe_calls = 0
        self.send_payload = send_payload or {"ok": True, "adapter": "mock", "state": "send_mock", "sidecar_run_id": "send-run-1", "send_result": {"ok": True}}
        self.voice_payload: dict = {
            "ok": False,
            "adapter": "mock",
            "state": "voice_transcribe_no_visible_voice",
            "sidecar_run_id": "voice-run-1",
            "transcribed_messages": [],
            "attempt_count": 0,
            "quality_flags": ["mock_no_visible_voice"],
        }

    def probe(self):
        self.probe_calls += 1
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
        self.c2_operation_order.append("sessions")
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
        self.c2_operation_order.append("messages")
        self.message_reads.append({"display_name": display_name, "rpa_session_key": rpa_session_key, **kwargs})
        if self.get_messages_payloads:
            payload = dict(self.get_messages_payloads.pop(0))
            payload.setdefault("ok", True)
            payload.setdefault("adapter", "mock")
            payload.setdefault("state", "messages_mock")
            payload.setdefault("sidecar_run_id", f"message-run-{len(self.message_reads)}")
            return payload
        return {
            "ok": True,
            "adapter": "mock",
            "state": "messages_mock",
            "sidecar_run_id": "message-run-1",
            "messages": [
                {"id": "wx-msg-1", "sender_role": self.message_sender_role, "type": "text", "content": "你好", "ocr_confidence": 0.98}
            ],
        }

    def locate_chat(self, *, display_name: str, rpa_session_key: str, **kwargs):
        self.c2_operation_order.append("locate_chat")
        self.locate_chats.append({"display_name": display_name, "rpa_session_key": rpa_session_key, **kwargs})
        if self.locate_payloads:
            payload = dict(self.locate_payloads.pop(0))
            payload.setdefault("adapter", "mock")
            payload.setdefault("sidecar_run_id", f"locate-run-{len(self.locate_chats)}")
            payload.setdefault("target_mode", kwargs.get("target_mode") or "visible")
            payload.setdefault("remark_code", kwargs.get("remark_code") or "")
            return payload
        return {
            "ok": True,
            "adapter": "mock",
            "state": "chat_target_confirmed",
            "sidecar_run_id": "locate-run-1",
            "target_mode": kwargs.get("target_mode") or "visible",
            "remark_code": kwargs.get("remark_code") or "",
            "targeting": {"ok": True, "mode": kwargs.get("target_mode") or "visible"},
        }

    def voice_transcribe(self, *, display_name: str, rpa_session_key: str, **kwargs):
        self.c2_operation_order.append("voice_transcribe")
        self.voice_transcribes.append({"display_name": display_name, "rpa_session_key": rpa_session_key, **kwargs})
        return dict(self.voice_payload)


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
        runner.c2_stop_guard_before_voice_seconds = 0
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

    def test_heartbeat_reuses_fresh_rpa_probe(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner.tick_once()
        runner.tick_once()

        self.assertEqual(bridge.probe_calls, 1)
        self.assertEqual(sum(1 for item in api.events if item == "heartbeat:ready:logged_in"), 2)

    def test_heartbeat_does_not_start_status_ocr_during_ui_action(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")
        runner.last_rpa_component_status = "ready"
        runner.last_wechat_status = "logged_in"
        runner.current_ui_lock = object()  # type: ignore[assignment]

        runner.tick_once()

        self.assertEqual(bridge.probe_calls, 0)
        self.assertIn("heartbeat:ready:logged_in", api.events)

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

        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:a")
        self.assertEqual(bridge.locate_chats[0]["remark_code"], "CJTEST01")
        self.assertEqual(bridge.message_reads[0]["display_name"], "CJTEST01 许聪")
        self.assertEqual(bridge.message_reads[0]["target_mode"], "current")
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

    def test_c2_message_read_skips_target_with_invalid_remark_code(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="张三",
                remark_code="NOT-A-C2-CODE",
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual(bridge.locate_chats, [])
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(runner.c2_stats["last_error"], "C2_TARGET_REMARK_CODE_INVALID")

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

        self.assertEqual(bridge.locate_chats[0]["display_name"], "CJTEST01 许聪")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:a")
        self.assertEqual(bridge.locate_chats[0]["remark_code"], "CJTEST01")
        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.message_reads[0]["display_name"], "CJTEST01 许聪")
        self.assertEqual(bridge.message_reads[0]["rpa_session_key"], "")
        self.assertEqual(bridge.message_reads[0]["remark_code"], "CJTEST01")
        self.assertEqual(bridge.message_reads[0]["target_mode"], "current")
        self.assertIn("ingest:1", api.events)

    def test_c2_message_read_skips_voice_transcribe_when_messages_have_no_voice(self):
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

        self.assertEqual(bridge.c2_operation_order, ["locate_chat", "messages"])
        self.assertEqual(bridge.voice_transcribes, [])
        self.assertIsNone(api.message_payloads[0]["evidence"]["voice_transcription"])

    def test_c2_message_read_uses_visual_voice_hint_when_ocr_misses_duration(self):
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
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [{"id": "text-1", "type": "text", "sender_role": "customer", "content": "下午退吧"}],
                "visible_untranscribed_voice": {
                    "detected": True,
                    "source": "visual_self_voice_bubble_context_menu_anchor",
                    "sender_role": "self",
                    "anchor_stable_key": "voice-stable:self-2s",
                },
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual(len(bridge.voice_transcribes), 1)
        self.assertIn("voice_transcribe", bridge.c2_operation_order)

    def test_c2_message_read_rejects_visual_voice_hint_without_avatar_role(self):
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
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [{"id": "text-1", "type": "text", "sender_role": "self", "content": "普通绿色文字"}],
                "visible_untranscribed_voice": {
                    "detected": True,
                    "source": "visual_self_voice_bubble_context_menu_anchor",
                    "sender_role": "unknown",
                },
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual(bridge.voice_transcribes, [])

    def test_c2_read_cancelled_after_message_read_before_ingest_when_target_stopped(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-text-after-stop",
                        "type": "text",
                        "sender_role": "customer",
                        "content": "好的",
                    }
                ],
            }
        ]
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
            authorization_revision="revision-conv-1",
        )
        calls = {"count": 0}

        def get_targets(binding: Binding, *, limit: int = 20):
            api.events.append(f"read_targets:{limit}")
            calls["count"] += 1
            return [target] if calls["count"] <= 3 else []

        api.get_wechat_read_targets = get_targets  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        runner.c2_stop_guard_before_voice_seconds = 0
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        result = runner._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS")
        self.assertEqual(bridge.c2_operation_order, ["locate_chat", "messages"])
        self.assertEqual(api.message_payloads, [])

    def test_c2_read_cancelled_by_stable_guard_before_voice_when_target_stops(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                    }
                ],
            }
        ]
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
            authorization_revision="revision-conv-1",
        )
        calls = {"count": 0}

        def get_targets(binding: Binding, *, limit: int = 20):
            api.events.append(f"read_targets:{limit}")
            calls["count"] += 1
            return [target] if calls["count"] <= 5 else []

        api.get_wechat_read_targets = get_targets  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        runner.c2_stop_guard_before_voice_seconds = 0.001
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        result = runner._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS")
        self.assertEqual(bridge.c2_operation_order, ["locate_chat", "messages"])
        self.assertEqual(bridge.voice_transcribes, [])
        self.assertEqual(api.message_payloads, [])

    def test_c2_voice_transcription_is_ingested_as_voice_message(self):
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
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                    }
                ],
            },
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-text",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": "你好",
                    }
                ],
            },
        ]
        bridge.voice_payload = {
            "ok": True,
            "adapter": "mock",
            "state": "voice_transcribe_completed",
            "sidecar_run_id": "voice-run-1",
            "artifact_dir": "C:/voice-run-1",
            "attempt_count": 1,
            "quality_flags": [],
            "transcribed_messages": [{"content": "你好", "sender_role": "customer"}],
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual(
            bridge.c2_operation_order,
            ["locate_chat", "messages", "voice_transcribe", "messages"],
        )
        self.assertEqual(bridge.voice_transcribes[0]["target_mode"], "current")
        self.assertEqual(bridge.voice_transcribes[0]["max_duration_seconds"], 240)
        self.assertEqual(bridge.message_reads[0]["target_mode"], "current")
        self.assertEqual(bridge.message_reads[1]["target_mode"], "current")
        self.assertIn("ingest:1", api.events)
        self.assertEqual(api.message_payloads[0]["messages"][0]["message_type"], "voice")
        self.assertEqual(api.message_payloads[0]["messages"][0]["sender_role_hint"], "customer")
        self.assertEqual(api.message_payloads[0]["messages"][0]["raw_payload"]["voice_transcription"], "你好")
        self.assertEqual(api.message_payloads[0]["messages"][0]["raw_payload"]["voice_transcription_meta"]["state"], "voice_transcribe_completed")
        timing = api.message_payloads[0]["evidence"]["timing"]
        self.assertEqual(timing["schema_version"], 1)
        self.assertEqual(
            [phase["name"] for phase in timing["phases"]],
            [
                "target_chat_locate",
                "initial_message_read",
                "voice_transcribe",
                "target_chat_reconfirm_and_final_read",
                "build_ingest_payload",
            ],
        )

    def test_c2_partial_voice_transcription_ingests_confirmed_message(self):
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
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 3"',
                        "quality_flags": ["untranscribed_voice_placeholder"],
                    }
                ],
            },
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-text",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": "果然掉在更衣柜里了。",
                    },
                    {
                        "id": "wx-msg-sales-placeholder",
                        "type": "voice",
                        "sender_role": "self",
                        "content": '[语音] 6" (c',
                        "quality_flags": ["untranscribed_voice_placeholder"],
                    },
                ],
            },
        ]
        bridge.voice_payload = {
            "ok": True,
            "adapter": "mock",
            "state": "voice_transcribe_partial",
            "sidecar_run_id": "voice-run-partial",
            "attempt_count": 1,
            "quality_flags": ["untranscribed_voice_remaining"],
            "transcribed_messages": [{"content": "果然掉在更衣柜里了。", "sender_role": "customer"}],
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual(
            bridge.c2_operation_order,
            ["locate_chat", "messages", "voice_transcribe", "messages"],
        )
        self.assertIn("ingest:1", api.events)
        self.assertEqual(len(api.message_payloads[0]["messages"]), 1)
        self.assertEqual(api.message_payloads[0]["messages"][0]["content"], "果然掉在更衣柜里了。")
        self.assertEqual(api.message_payloads[0]["messages"][0]["raw_payload"]["voice_transcription_meta"]["state"], "voice_transcribe_partial")

    def test_c2_text_noise_does_not_trigger_voice_transcribe(self):
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
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-sales-voice-noise-1",
                        "type": "text",
                        "sender_role": "sales_candidate",
                        "content": '2" (c',
                        "content_raw_ocr": '2" (c',
                        "quality_flags": ["ocr_low_confidence"],
                    }
                ],
            },
            {"ok": True, "messages": []},
        ]
        bridge.voice_payload = {
            "ok": True,
            "adapter": "mock",
            "state": "voice_transcribe_no_new_text",
            "sidecar_run_id": "voice-run-no-text",
            "attempt_count": 1,
            "quality_flags": ["no_new_transcribed_text"],
            "transcribed_messages": [],
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual(bridge.c2_operation_order, ["locate_chat", "messages"])
        self.assertEqual(bridge.voice_transcribes, [])
        self.assertEqual(api.message_payloads[0]["messages"], [])

    def test_c2_unbound_visible_transcript_blocks_later_partial_ingest(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
        )
        api.read_targets = [target]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        initial_voice = {
            "ok": True,
            "messages": [
                {
                    "id": "voice-23-before",
                    "type": "voice",
                    "sender_role": "customer",
                    "voice_duration": 23,
                    "content": '[语音] 23"',
                    "quality_flags": ["untranscribed_voice_placeholder"],
                }
            ],
        }
        bridge.get_messages_payloads = [initial_voice]
        bridge.voice_payload = {
            "ok": True,
            "adapter": "mock",
            "state": "voice_transcribe_no_new_text",
            "sidecar_run_id": "voice-run-unbound",
            "attempt_count": 1,
            "quality_flags": ["no_new_transcribed_text", "voice_transcribe_anchor_failed"],
            "transcribed_messages": [],
            "new_messages": [
                {
                    "id": "voice-23-expanded",
                    "type": "voice",
                    "sender_role": "customer",
                    "content": "然后，你看那个数字人直播这块儿。",
                    "quality_flags": ["voice_duration_prefix_removed"],
                }
            ],
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual(runner.c2_stats["last_error"], "VOICE_TRANSCRIPT_BINDING_INCONSISTENT")
        self.assertEqual(api.message_payloads, [])
        self.assertEqual(bridge.c2_operation_order, ["locate_chat", "messages", "voice_transcribe"])
        self.assertEqual(len(runner.c2_voice_binding_blocked_authorizations), 1)

        runner.c2_read_failure_cooldowns.clear()
        bridge.c2_operation_order.clear()
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {"id": "text-after-failure", "type": "text", "sender_role": "self", "content": "ok"}
                ],
            }
        ]
        runner._read_bound_wechat_messages(binding)

        self.assertEqual(runner.c2_stats["last_error"], "C2_VOICE_TRANSCRIPT_BINDING_PENDING")
        self.assertEqual(api.message_payloads, [])
        self.assertEqual(bridge.c2_operation_order, ["locate_chat", "messages"])

    def test_c2_voice_click_failed_stops_before_current_chat_reconfirm(self):
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
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                    }
                ],
            }
        ]
        bridge.voice_payload = {
            "ok": False,
            "adapter": "mock",
            "state": "voice_transcribe_click_failed",
            "error_code": "VOICE_TRANSCRIBE_CLICK_FAILED",
            "sidecar_run_id": "voice-run-click-failed",
            "attempt_count": 1,
            "quality_flags": ["voice_transcribe_click_failed"],
            "transcribed_messages": [],
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual(bridge.c2_operation_order, ["locate_chat", "messages", "voice_transcribe"])
        self.assertEqual(len(bridge.locate_chats), 1)
        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(api.message_payloads, [])
        self.assertEqual(runner.c2_stats["last_error"], "VOICE_TRANSCRIBE_CLICK_FAILED")

    def test_c2_voice_sidecar_timeout_stops_before_current_chat_reconfirm(self):
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
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                    }
                ],
            }
        ]
        bridge.voice_payload = {
            "ok": False,
            "adapter": "mock",
            "error_code": "RPA_SIDECAR_TIMEOUT",
            "current_step": "rpa_sidecar_timeout",
            "sidecar_run_id": "voice-run-timeout",
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual(bridge.c2_operation_order, ["locate_chat", "messages", "voice_transcribe"])
        self.assertEqual(len(bridge.locate_chats), 1)
        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(api.message_payloads, [])
        self.assertEqual(runner.c2_stats["last_error"], "RPA_SIDECAR_TIMEOUT")

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

    def test_c2_message_read_success_enters_short_cooldown_before_retry(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJVOICE01 虾丸子大人",
                remark_code="CJVOICE01",
                row_fingerprint={"title_text": "CJVOICE01 虾丸子大人"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)
        runner._read_bound_wechat_messages(binding)

        self.assertEqual(len(bridge.message_reads), 1)
        self.assertTrue(runner.c2_read_success_cooldowns)

    def test_c2_repeated_read_reuses_current_chat_before_searching_again(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
        )
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        first = runner._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=False)
        second = runner._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=False)

        self.assertTrue(first.get("ok"))
        self.assertTrue(second.get("ok"))
        self.assertEqual([item["target_mode"] for item in bridge.locate_chats[:2]], ["visible", "current"])

    def test_c2_recent_visible_scan_survives_read_target_permission_delay(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="",
                display_name="CJR8S5K3 虾丸子大人",
                remark_code="CJR8S5K3",
                row_fingerprint={"title_text": "CJR8S5K3 虾丸子大人"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")
        runner, _ = self.make_runner(api, bridge)
        runner.c2_last_visible_sessions = [
            {
                "display_name": "CJR8S5K3 虾丸子大.",
                "rpa_session_key": "wx:rpa:v1:recent",
                "remark_code_candidates": ["CJR8S5K3"],
                "last_message_preview": '[语音] 2"',
                "ocr_confidence": 0.94,
            }
        ]
        runner.c2_last_visible_sessions_monotonic = time.monotonic() - 60.0
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        result = runner._read_one_wechat_target(binding, api.read_targets[0], current_step="state_target_message_read", enforce_read_targets=False)

        self.assertTrue(result.get("ok"))
        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:recent")
        self.assertNotIn("search_by_remark_code", [item.get("target_mode") for item in bridge.locate_chats])

    def test_c2_recent_visible_hit_survives_one_ocr_miss(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJR8S5K3 虾丸子大人",
                remark_code="CJR8S5K3",
                row_fingerprint={"title_text": "CJR8S5K3 虾丸子大人"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")

        def list_missed_session():
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-miss",
                "sessions": [
                    {"name": "腾讯新闻", "session_key": "wx:rpa:v1:news", "content": "新闻", "ocr_confidence": 0.98}
                ],
            }

        bridge.list_sessions = list_missed_session  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        runner.c2_recent_visible_hits_by_remark_code["CJR8S5K3"] = {
            "seen_at": time.monotonic() - 20.0,
            "session": {
                "display_name": "CJR8S5K3 虾丸子大.",
                "rpa_session_key": "wx:rpa:v1:recent-hit",
                "remark_code_candidates": ["CJR8S5K3"],
                "row_fingerprint": {"title_text": "CJR8S5K3 虾丸子大.", "title_bbox": [155, 118, 373, 141], "row_y_bucket": 16},
                "ocr_confidence": 0.95,
            },
        }
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        result = runner._read_one_wechat_target(binding, api.read_targets[0], current_step="state_target_message_read", enforce_read_targets=False)

        self.assertTrue(result.get("ok"))
        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:recent-hit")
        self.assertEqual(bridge.locate_chats[0]["visible_session_candidate"]["center_y"], 129.5)
        self.assertNotIn("search_by_remark_code", [item.get("target_mode") for item in bridge.locate_chats])

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
        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:a")
        self.assertEqual(bridge.message_reads[0]["target_mode"], "current")
        self.assertEqual(bridge.message_reads[0]["rpa_session_key"], "")
        self.assertEqual(bridge.message_reads[0]["remark_code"], "CJTEST01")
        self.assertEqual(api.events.count("ingest:1"), 1)
        self.assertIn("read_targets:20", api.events)

    def test_c2_visible_hit_uses_current_scan_session_key_when_backend_binding_key_is_stale(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-voice",
                rpa_session_key="wx:rpa:v1:stale-binding",
                display_name="CJVOICE01 许聪",
                remark_code="CJVOICE01",
                row_fingerprint={"title_text": "CJVOICE01 许聪"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]

        def list_voice_session():
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-voice",
                "sessions": [
                    {
                        "name": "CJVOICE01 许聪",
                        "session_key": "wx:rpa:v1:current-visible",
                        "row_fingerprint": {"title_text": "CJVOICE01 许聪", "title_bbox": [154, 115, 306, 143]},
                        "content": '[语音] 2"',
                        "unread_signal": True,
                        "ocr_confidence": 0.98,
                    }
                ],
            }

        def post_scan_with_stale_key(binding: Binding, payload: dict):
            api.scan_payloads.append(payload)
            api.events.append(f"scan:{len(payload.get('sessions') or [])}:{payload.get('error_code')}")
            return {
                "bound_count": 1,
                "bindings": [
                    {
                        "conversation_id": "conv-voice",
                        "lead_id": "lead-voice",
                        "sales_id": "sales-1",
                        "remark_code": "CJVOICE01",
                        "rpa_session_key": "wx:rpa:v1:stale-binding",
                        "display_name": "CJVOICE01 许聪",
                        "row_fingerprint": {"title_text": "CJVOICE01 许聪"},
                        "ocr_confidence": 0.98,
                        "can_ingest_messages": True,
                    }
                ],
            }

        bridge.list_sessions = list_voice_session  # type: ignore[method-assign]
        api.post_wechat_session_scan_result = post_scan_with_stale_key  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:current-visible")
        self.assertEqual(bridge.locate_chats[0]["visible_session_candidate"]["center_y"], 129.0)
        self.assertEqual(bridge.locate_chats[0]["visible_session_candidate"]["click_geometry_source"], "row_fingerprint.title_bbox")
        self.assertEqual(bridge.message_reads[0]["target_mode"], "current")
        self.assertEqual(bridge.message_reads[0]["rpa_session_key"], "")

    def test_c2_visible_candidate_derives_click_geometry_from_title_bbox(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)

        candidate = runner._sidecar_visible_session_candidate(
            {
                "name": "CJR8S5K3虾丸子大..",
                "session_key": "wx:rpa:v1:visible",
                "row_fingerprint": {"title_text": "CJR8S5K3虾丸子大..", "title_bbox": [154, 115, 372, 143], "row_y_bucket": 16},
                "content": "好多人",
                "ocr_confidence": 0.955,
            }
        )

        self.assertEqual(candidate["center_y"], 129.0)
        self.assertEqual(candidate["top"], 115.0)
        self.assertEqual(candidate["bottom"], 143.0)
        self.assertEqual(candidate["click_geometry_source"], "row_fingerprint.title_bbox")

    def test_c2_visible_session_geometry_replaces_mapped_hash_with_raw_fingerprint(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)

        sessions = runner._visible_sessions_with_click_geometry(
            [
                {
                    "display_name": "CJR8S5K3虾丸子大",
                    "rpa_session_key": "wx:rpa:v1:visible",
                    "remark_code_candidates": ["CJR8S5K3"],
                    "row_fingerprint": "mapped-hash-only",
                }
            ],
            [
                {
                    "name": "CJR8S5K3虾丸子大",
                    "session_key": "wx:rpa:v1:visible",
                    "row_fingerprint": {"title_text": "CJR8S5K3虾丸子大", "title_bbox": [155, 118, 373, 141], "row_y_bucket": 16},
                }
            ],
        )

        self.assertEqual(sessions[0]["center_y"], 129.5)
        self.assertIsInstance(sessions[0]["row_fingerprint"], dict)

    def test_c2_state_target_merges_realtime_visible_scan_into_locate(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-voice",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJVOICE01 虾丸子大人",
                remark_code="CJVOICE01",
                row_fingerprint={"title_text": "CJVOICE01 虾丸子大人"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_voice_session():
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-visible-now",
                "sessions": [
                    {
                        "name": "CJVOICE01 虾丸子大人",
                        "session_key": "wx:rpa:v1:visible-now",
                        "row_fingerprint": {"title_text": "CJVOICE01 虾丸子大人"},
                        "content": '[语音] 2"',
                        "unread_signal": True,
                        "ocr_confidence": 0.98,
                    }
                ],
            }

        bridge.list_sessions = list_voice_session  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertNotIn("sessions", bridge.c2_operation_order)
        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:backend")
        self.assertEqual(bridge.locate_chats[0]["remark_code"], "CJVOICE01")

    def test_c2_state_target_passes_short_code_to_atomic_visible_locate(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-voice",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJR8S5K3 虾丸子大人",
                remark_code="CJR8S5K3",
                row_fingerprint={"title_text": "CJR8S5K3 虾丸子大人"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_visible_session_with_spaced_code():
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-visible-spaced",
                "sessions": [
                    {
                        "name": "CJR8 S5K3 虾丸子大人",
                        "session_key": "wx:rpa:v1:visible-spaced",
                        "row_fingerprint": {"title_text": "CJR8 S5K3 虾丸子大人"},
                        "content": '[语音] 2"',
                        "unread_signal": True,
                        "ocr_confidence": 0.98,
                    }
                ],
            }

        bridge.list_sessions = list_visible_session_with_spaced_code  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertNotIn("sessions", bridge.c2_operation_order)
        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:backend")
        self.assertEqual(bridge.locate_chats[0]["remark_code"], "CJR8S5K3")

    def test_c2_state_target_falls_back_to_search_when_realtime_visible_misses(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-voice",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJVOICE01 虾丸子大人",
                remark_code="CJVOICE01",
                row_fingerprint={"title_text": "CJVOICE01 虾丸子大人"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_other_session():
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-other",
                "sessions": [
                    {
                        "name": "CJOTHER01 许聪",
                        "session_key": "wx:rpa:v1:other",
                        "content": "你好",
                        "ocr_confidence": 0.98,
                    }
                ],
            }

        bridge.list_sessions = list_other_session  # type: ignore[method-assign]
        bridge.locate_payloads = [
            {"ok": False, "state": "target_not_confirmed", "error_code": "TARGET_NOT_CONFIRMED"},
            {"ok": True, "state": "chat_target_confirmed"},
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual([item["target_mode"] for item in bridge.locate_chats], ["visible", "search_by_remark_code"])
        self.assertEqual(bridge.locate_chats[1]["rpa_session_key"], "")

    def test_c2_realtime_visible_miss_reports_session_match_debug(self):
        api = FakeApi(None)
        artifact_dir = tempfile.mkdtemp(prefix="chejin-c2-sessions-review-")
        target = WechatReadTarget(
            conversation_id="conv-voice",
            rpa_session_key="wx:rpa:v1:backend",
            display_name="CJR8S5K3 虾丸子大人",
            remark_code="CJR8S5K3",
            row_fingerprint={"title_text": "CJR8S5K3 虾丸子大人"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
        )
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_other_session():
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-miss-debug",
                "artifact_dir": artifact_dir,
                "screenshot_path": os.path.join(artifact_dir, "sessions.png"),
                "sessions": [
                    {
                        "name": "三国望神州-秘银",
                        "session_key": "wx:rpa:v1:other",
                        "content": "周末来了节奏没准快一些",
                        "ocr_confidence": 0.98,
                    }
                ],
            }

        bridge.list_sessions = list_other_session  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)

        visible_target, metadata = runner._resolve_current_visible_target(target)

        self.assertIsNone(visible_target)
        self.assertEqual(metadata["match_count"], 0)
        self.assertEqual(metadata["session_match_debug"][0]["display_name"], "三国望神州-秘银")
        self.assertFalse(metadata["session_match_debug"][0]["match_checks"]["identity_normalized_contains"])
        self.assertTrue(metadata["review_path"])
        self.assertTrue(os.path.exists(metadata["review_path"]))
        with open(metadata["review_path"], encoding="utf-8") as handle:
            review = json.load(handle)
        self.assertEqual(review["reason"], "read_target_realtime_visible_check")
        self.assertEqual(review["target"]["remark_code"], "CJR8S5K3")
        self.assertEqual(review["match"]["match_count"], 0)
        self.assertEqual(review["scan"]["mapped_sessions"][0]["display_name"], "三国望神州-秘银")

    def test_c2_state_target_uses_recent_visible_scan_before_search(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-voice",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJVOICE01 虾丸子大人",
                remark_code="CJVOICE01",
                row_fingerprint={"title_text": "CJVOICE01 虾丸子大人"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_missed_session():
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-miss",
                "sessions": [
                    {
                        "name": "腾讯新闻",
                        "session_key": "wx:rpa:v1:news",
                        "content": "新闻",
                        "ocr_confidence": 0.98,
                    }
                ],
            }

        bridge.list_sessions = list_missed_session  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        runner.c2_last_visible_sessions = [
            {
                "display_name": "CJVOICE01 虾丸子大人",
                "rpa_session_key": "wx:rpa:v1:recent-visible",
                "remark_code_candidates": ["CJVOICE01"],
                "last_message_preview": '[语音] 2"',
                "row_fingerprint": {"title_text": "CJVOICE01 虾丸子大人", "title_bbox": [154, 198, 306, 222]},
                "ocr_confidence": 0.98,
            }
        ]
        runner.c2_last_visible_sessions_monotonic = time.monotonic()
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:recent-visible")
        self.assertEqual(bridge.locate_chats[0]["visible_session_candidate"]["center_y"], 210.0)
        self.assertNotEqual(bridge.locate_chats[0]["target_mode"], "search_by_remark_code")

    def test_c2_state_target_does_not_use_expired_recent_visible_scan(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-voice",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJVOICE01 虾丸子大人",
                remark_code="CJVOICE01",
                row_fingerprint={"title_text": "CJVOICE01 虾丸子大人"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_missed_session():
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-miss",
                "sessions": [
                    {"name": "腾讯新闻", "session_key": "wx:rpa:v1:news", "content": "新闻", "ocr_confidence": 0.98}
                ],
            }

        bridge.list_sessions = list_missed_session  # type: ignore[method-assign]
        bridge.locate_payloads = [
            {"ok": False, "state": "target_not_confirmed", "error_code": "TARGET_NOT_CONFIRMED"},
            {"ok": True, "state": "chat_target_confirmed"},
        ]
        runner, _ = self.make_runner(api, bridge)
        runner.c2_last_visible_sessions = [
            {
                "display_name": "CJVOICE01 虾丸子大人",
                "rpa_session_key": "wx:rpa:v1:recent-visible",
                "remark_code_candidates": ["CJVOICE01"],
                "last_message_preview": '[语音] 2"',
                "row_fingerprint": {"title_text": "CJVOICE01 虾丸子大人"},
                "ocr_confidence": 0.98,
            }
        ]
        runner.c2_last_visible_sessions_monotonic = time.monotonic() - C2_RECENT_VISIBLE_CACHE_TTL_SECONDS - 1.0
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual([item["target_mode"] for item in bridge.locate_chats], ["visible", "search_by_remark_code"])
        self.assertEqual(bridge.locate_chats[1]["rpa_session_key"], "")

    def test_c2_state_target_rejects_ambiguous_realtime_visible_matches(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-voice",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJVOICE01 虾丸子大人",
                remark_code="CJVOICE01",
                row_fingerprint={"title_text": "CJVOICE01 虾丸子大人"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_duplicate_sessions():
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-ambiguous",
                "sessions": [
                    {"name": "CJVOICE01 虾丸子大人", "session_key": "wx:rpa:v1:a", "content": '[语音] 2"', "ocr_confidence": 0.98},
                    {"name": "群聊", "session_key": "wx:rpa:v1:b", "content": "包含:CJVOICE01 虾丸子大人", "ocr_confidence": 0.98},
                ],
            }

        bridge.list_sessions = list_duplicate_sessions  # type: ignore[method-assign]
        bridge.locate_payloads = [
            {"ok": False, "state": "target_not_confirmed", "error_code": "C2_VISIBLE_TARGET_AMBIGUOUS"},
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._read_bound_wechat_messages(binding)

        self.assertEqual(len(bridge.locate_chats), 1)
        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(runner.c2_stats["last_error"], "C2_VISIBLE_TARGET_AMBIGUOUS")

    def test_c2_state_target_does_not_search_after_conversation_admission_rejection(self):
        for error_code in ("C2_GROUP_CHAT_NOT_ALLOWED", "C2_CONVERSATION_TYPE_UNKNOWN"):
            with self.subTest(error_code=error_code):
                api = FakeApi(None)
                api.read_targets = [
                    WechatReadTarget(
                        conversation_id="conv-group-guard",
                        rpa_session_key="wx:rpa:v1:group-guard",
                        display_name="CJTEST01",
                        remark_code="CJTEST01",
                        row_fingerprint={"title_text": "CJTEST01"},
                        ocr_confidence=0.98,
                        read_reason="waiting_user_reply",
                    )
                ]
                bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
                bridge.locate_payloads = [
                    {"ok": False, "state": "target_not_confirmed", "error_code": error_code},
                ]
                runner, _ = self.make_runner(api, bridge)
                binding = Binding(
                    worker_id="worker-1",
                    worker_token="token",
                    client_instance_id="client-1",
                    run_status="paused",
                )

                runner._read_bound_wechat_messages(binding)

                self.assertEqual([item["target_mode"] for item in bridge.locate_chats], ["visible"])
                self.assertEqual(bridge.message_reads, [])
                self.assertEqual(runner.c2_stats["last_error"], error_code)

    def test_c2_visible_hit_attempt_skips_state_target_search_in_same_round_even_when_visible_read_fails(self):
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

        def failing_visible_get_messages(*, display_name: str, rpa_session_key: str, **kwargs):
            bridge.c2_operation_order.append("messages")
            bridge.message_reads.append({"display_name": display_name, "rpa_session_key": rpa_session_key, **kwargs})
            return {
                "ok": False,
                "error_code": "TARGET_NOT_CONFIRMED_FOR_MESSAGES",
                "sidecar_run_id": "message-visible-failed",
            }

        bridge.get_messages = failing_visible_get_messages  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:a")
        self.assertEqual(bridge.message_reads[0]["target_mode"], "current")
        self.assertEqual(bridge.message_reads[0]["rpa_session_key"], "")
        self.assertIn("read_targets:20", api.events)
        self.assertNotIn("search_by_remark_code", [item.get("target_mode") for item in bridge.locate_chats])

    def test_c2_visible_hit_locate_falls_back_to_remark_search_when_not_confirmed(self):
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

        def locate_chat(*, display_name: str, rpa_session_key: str, **kwargs):
            bridge.c2_operation_order.append("locate_chat")
            bridge.locate_chats.append({"display_name": display_name, "rpa_session_key": rpa_session_key, **kwargs})
            mode = kwargs.get("target_mode") or "visible"
            if mode == "visible":
                return {
                    "ok": False,
                    "adapter": "mock",
                    "state": "target_not_confirmed",
                    "sidecar_run_id": "locate-visible-failed",
                    "target_mode": "visible",
                }
            return {
                "ok": True,
                "adapter": "mock",
                "state": "chat_target_confirmed",
                "sidecar_run_id": "locate-search-ok",
                "target_mode": mode,
            }

        bridge.locate_chat = locate_chat  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual([item["target_mode"] for item in bridge.locate_chats[:2]], ["visible", "search_by_remark_code"])
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:a")
        self.assertEqual(bridge.locate_chats[1]["rpa_session_key"], "")
        self.assertEqual(bridge.message_reads[0]["target_mode"], "current")
        self.assertIn("ingest:1", api.events)

    def test_c2_visible_hit_queue_is_cleared_when_backend_read_targets_empty(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(bridge.voice_transcribes, [])
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(runner.visible_hit_queue, [])
        self.assertIn("read_targets:20", api.events)

    def test_c2_visible_hit_inherits_current_read_target_authorization(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        visible_target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:visible",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="visible_hit",
            raw={"visible_session_source": "first_screen_session_scan"},
        )
        authorized_target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:backend",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-current",
            raw={"authorization_revision": "revision-current"},
        )
        captured: list[WechatReadTarget] = []

        def capture_read(binding: Binding, target: WechatReadTarget, **kwargs):
            captured.append(target)
            return {"ok": True}

        runner._read_one_wechat_target = capture_read  # type: ignore[method-assign]
        runner.visible_hit_queue = [visible_target]

        runner._drain_visible_hit_queue(
            binding=Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1"),
            authorized_targets=[authorized_target],
        )

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].authorization_revision, "revision-current")
        self.assertEqual(captured[0].rpa_session_key, "wx:rpa:v1:visible")
        self.assertEqual(captured[0].raw["authorization_read_reason"], "waiting_user_reply")

    def test_c2_visible_hit_v3_ingest_carries_current_authorization_revision(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                read_reason="waiting_user_reply",
                authorization_revision="revision-current",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "observation_schema_version": 3,
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "text-1",
                        "row_kind": "text_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "content_clean": "明天下午三点联系。",
                        "source_message": {"id": "text-1", "type": "text", "content": "明天下午三点联系。"},
                    }
                ],
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(api.message_payloads[0]["contract_version"], 3)
        self.assertEqual(api.message_payloads[0]["authorization_revision"], "revision-current")

    def test_c2_visible_hit_without_current_authorization_is_dropped_before_ui_action(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        visible_target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:visible",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            read_reason="visible_hit",
        )
        authorized_without_revision = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:backend",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            read_reason="waiting_user_reply",
        )
        runner.visible_hit_queue = [visible_target]

        runner._drain_visible_hit_queue(
            binding=Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1"),
            authorized_targets=[authorized_without_revision],
        )

        self.assertEqual(bridge.c2_operation_order, [])
        self.assertEqual(runner.visible_hit_queue, [])
        self.assertEqual(runner.c2_stats["last_error"], "C2_TARGET_AUTHORIZATION_REVISION_MISSING")

    def test_c2_read_authorization_requires_exact_revision(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                read_reason="waiting_user_reply",
                authorization_revision="revision-new",
            )
        ]
        runner, _ = self.make_runner(api, FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused")))
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1")
        stale_target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-old",
        )
        current_target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-new",
        )

        self.assertFalse(runner._backend_still_allows_read_target(binding, stale_target))
        self.assertTrue(runner._backend_still_allows_read_target(binding, current_target))

    def test_c2_visible_read_rechecks_read_targets_after_voice_before_messages(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                    }
                ],
            }
        ]
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
            authorization_revision="revision-conv-1",
        )
        calls = {"count": 0}

        def get_targets(binding: Binding, *, limit: int = 20):
            api.events.append(f"read_targets:{limit}")
            calls["count"] += 1
            return [target] if calls["count"] <= 6 else []

        api.get_wechat_read_targets = get_targets  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(len(bridge.voice_transcribes), 1)
        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(api.message_payloads, [])
        self.assertEqual(runner.c2_stats["last_error"], "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS")
        self.assertFalse(LOCK_FILE.exists())

    def test_c2_visible_read_cancelled_after_voice_before_reconfirm_when_target_stopped(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                    }
                ],
            }
        ]
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
            authorization_revision="revision-conv-1",
        )
        calls = {"count": 0}

        def get_targets(binding: Binding, *, limit: int = 20):
            api.events.append(f"read_targets:{limit}")
            calls["count"] += 1
            return [target] if calls["count"] <= 6 else []

        api.get_wechat_read_targets = get_targets  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(len(bridge.voice_transcribes), 1)
        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual([item["target_mode"] for item in bridge.locate_chats], ["visible"])
        self.assertEqual(api.message_payloads, [])
        self.assertEqual(runner.c2_stats["last_error"], "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS")
        self.assertFalse(LOCK_FILE.exists())

    def test_c2_read_authorization_requires_same_read_reason_for_state_target(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
        )
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
                read_reason="recall_precheck",
            )
        ]
        runner, _ = self.make_runner(api, FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused")))
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        self.assertFalse(runner._backend_still_allows_read_target(binding, target))

    def test_c2_read_cancelled_before_locating_when_read_targets_empty_after_lock(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        result = runner._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=True)

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_code"), "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS")
        self.assertEqual(bridge.c2_operation_order, [])
        self.assertEqual(bridge.locate_chats, [])
        self.assertEqual(bridge.message_reads, [])
        self.assertFalse(LOCK_FILE.exists())

    def test_c2_read_cancelled_after_visible_check_before_locating_when_target_stopped(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
            authorization_revision="revision-conv-1",
        )
        calls = {"count": 0}

        def get_targets(binding: Binding, *, limit: int = 20):
            api.events.append(f"read_targets:{limit}")
            calls["count"] += 1
            return [target] if calls["count"] == 1 else []

        api.get_wechat_read_targets = get_targets  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        result = runner._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=True)

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_code"), "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS")
        self.assertEqual(bridge.c2_operation_order, [])
        self.assertEqual(bridge.locate_chats, [])
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(api.events.count("read_targets:20"), 2)
        self.assertFalse(LOCK_FILE.exists())

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
