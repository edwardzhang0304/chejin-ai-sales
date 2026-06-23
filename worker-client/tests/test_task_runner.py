from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("CHEJIN_WORKER_HOME", tempfile.mkdtemp(prefix="chejin-worker-test-"))
os.environ.setdefault("CHEJIN_RPA_MODE", "mock")

from chejin_worker_client.models import Binding, RpaResult, RpaStep, Task, WorkerProfile
from chejin_worker_client.task_runner import TaskRunner


class FakeApi:
    def __init__(self, task: Task | None, result_mode: str = "success", claim_response: Task | None = None) -> None:
        self.task = task
        self.claim_response = claim_response
        self.result_mode = result_mode
        self.events: list[str] = []
        self.evidence_payloads: list[dict] = []
        self.run_status_updates: list[str] = []

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


class FakeBridge:
    def __init__(self, result: RpaResult) -> None:
        self.result = result
        self.tasks: list[Task] = []

    def probe(self):
        return "ready", "logged_in"

    def run_add_friend(self, task: Task, emit_step):
        self.tasks.append(task)
        emit_step(RpaStep(current_step="checking_rpa", title="检查自动化组件", remark="自动化组件可用"))
        emit_step(RpaStep(current_step="invite_sent", title="发送添加通讯录邀请", remark="已点击发送"))
        return self.result


class TaskRunnerTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
