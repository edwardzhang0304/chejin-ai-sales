from __future__ import annotations

import json
import os
import tempfile

os.environ.setdefault("CHEJIN_RPA_MODE", "mock")
os.environ.setdefault("CHEJIN_RPA_MOCK_STEP_DELAY_SECONDS", "0")
os.environ.setdefault("CHEJIN_WORKER_HOME", tempfile.mkdtemp(prefix="chejin-worker-smoke-"))

from chejin_worker_client.models import Binding, Task, WorkerProfile
from chejin_worker_client.rpa_bridge import RpaBridge
from chejin_worker_client.task_runner import TaskRunner


class SmokeApi:
    def __init__(self) -> None:
        self.task = Task(
            id="smoke-task-001",
            task_type="add_friend",
            status="pending",
            customer_name="冒烟客户",
            phone="13800000000",
            sales_name="冒烟销售",
            remark="SMOKE",
            verify_message="您好，我是车金冒烟销售，您刚咨询过二手车",
            remark_name="CJ-冒烟销售-CJSMK1-0000",
            remark_code="CJSMK1",
            remark_code_valid=True,
        )
        self.events: list[str] = []
        self.inflight_flow_id: str | None = None
        self.inflight_flow_state: dict = {}

    def start_inflight_flow(
        self,
        binding: Binding,
        *,
        flow_id: str,
        flow_kind: str,
    ):
        self.inflight_flow_id = flow_id
        self.inflight_flow_state = {
            "status": "active",
            "flow_id": flow_id,
            "flow_kind": flow_kind,
            "registered_at": "2026-08-14T00:00:00+00:00",
            "pause_requested_at": None,
        }
        self.events.append(f"flow_start:{flow_kind}:{flow_id}")
        return dict(self.inflight_flow_state)

    def finish_inflight_flow(
        self,
        binding: Binding,
        *,
        flow_id: str,
        terminal_kind: str,
        conversation_id: str | None = None,
        error_code: str | None = None,
    ):
        self.events.append(f"flow_finish:{terminal_kind}:{flow_id}")
        self.inflight_flow_id = None
        self.inflight_flow_state = {}
        return {"finished": True, "flow_id": flow_id}

    def heartbeat(self, binding: Binding, **kwargs):
        self.events.append(f"heartbeat:{kwargs['rpa_component_status']}:{kwargs['wechat_status']}")
        return WorkerProfile(
            id=binding.worker_id,
            worker_name="冒烟 Worker",
            run_status=binding.run_status,
            inflight_flow_state=dict(self.inflight_flow_state),
        )

    def pull_task(self, binding: Binding):
        self.events.append("pull")
        return "pending", self.task, None

    def claim_task(self, binding: Binding, task: Task):
        self.events.append(f"claim:{task.id}")
        task.status = "running"
        return task

    def report_step(self, binding: Binding, task_id: str, current_step: str, remark: str):
        self.events.append(f"step:{current_step}")
        return self.task

    def complete_invite_sent(self, binding: Binding, task_id: str):
        self.events.append(f"complete_invite_sent:{task_id}")
        self.task.status = "completed"
        self.task.result_code = "invite_sent"
        return self.task

    def complete_already_friend(self, binding: Binding, task_id: str):
        self.events.append(f"complete_already_friend:{task_id}")
        self.task.status = "completed"
        self.task.result_code = "already_friend"
        return self.task

    def fail_task(self, binding: Binding, task_id: str, error_code: str, failure_step: str | None, message: str):
        self.events.append(f"fail:{error_code}:{failure_step}")
        self.task.status = "failed"
        self.task.error_code = error_code
        return self.task

    def upload_evidence(self, binding: Binding, task_id: str, content: str, **kwargs):
        self.events.append(f"evidence:{kwargs.get('error_code')}")

    def set_run_status(self, binding: Binding, run_status: str):
        self.events.append(f"run_status:{run_status}")
        return WorkerProfile(id=binding.worker_id, worker_name="冒烟 Worker", run_status=run_status)


def main() -> int:
    api = SmokeApi()
    seen = {"profiles": [], "statuses": [], "tasks": [], "steps": [], "results": [], "errors": []}
    runner = TaskRunner(
        api,  # type: ignore[arg-type]
        RpaBridge(),
        on_profile=lambda item: seen["profiles"].append(item.worker_name),
        on_status=lambda item: seen["statuses"].append(item),
        on_task=lambda item: seen["tasks"].append(item.id if item else None),
        on_step=lambda item: seen["steps"].append(item.current_step),
        on_result=lambda item: seen["results"].append(item.result_code if item else None),
        on_error=lambda item: seen["errors"].append(item),
    )
    runner.binding = Binding(worker_id="smoke-worker-001", worker_token="smoke-token", client_instance_id="smoke-client", run_status="running")
    runner.tick_once()

    required_events = {
        "heartbeat:ready:logged_in",
        "pull",
        "claim:smoke-task-001",
        "complete_invite_sent:smoke-task-001",
    }
    ok = required_events.issubset(set(api.events)) and "invite_sent" in seen["results"] and not seen["errors"]
    report = {"ok": ok, "api_events": api.events, "seen": seen, "task_status": api.task.status, "result_code": api.task.result_code}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
