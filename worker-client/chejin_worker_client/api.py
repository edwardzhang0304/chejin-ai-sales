from __future__ import annotations

import os
from typing import Any

import requests

from .config import CONFIG
from .models import Binding, Task, WechatReadTarget, WorkerProfile


class ApiError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.data = data


class WorkerApiClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or CONFIG.api_base_url).rstrip("/")
        self.session = requests.Session()
        self.timeout = CONFIG.api_timeout_seconds

    def bind(self, worker_id: str, worker_token: str, client_instance_id: str) -> WorkerProfile:
        payload = self._request(
            "POST",
            f"/workers/{worker_id}/client-bind",
            json={"worker_token": worker_token, "client_instance_id": client_instance_id},
        )
        return WorkerProfile.from_api(payload)

    def heartbeat(
        self,
        binding: Binding,
        *,
        running_status: str,
        current_task: str | None,
        rpa_component_status: str,
        wechat_status: str,
        current_step: str | None = None,
        local_lock_summary: dict[str, Any] | None = None,
    ) -> WorkerProfile:
        payload = self._request(
            "POST",
            f"/workers/{binding.worker_id}/heartbeat",
            binding=binding,
            json={
                "client_instance_id": binding.client_instance_id,
                "client_binding_state": "bound",
                "run_status": binding.run_status,
                "running_status": running_status,
                "current_task": current_task,
                "rpa_component_status": rpa_component_status,
                "wechat_status": wechat_status,
                "current_step": current_step,
                "local_lock_summary": local_lock_summary or {},
            },
        )
        return WorkerProfile.from_api(payload)

    def set_run_status(self, binding: Binding, run_status: str) -> WorkerProfile:
        payload = self._request(
            "POST",
            f"/workers/{binding.worker_id}/run-status",
            binding=binding,
            json={"client_instance_id": binding.client_instance_id, "run_status": run_status},
        )
        return WorkerProfile.from_api(payload)

    def pull_task(self, binding: Binding) -> tuple[str, Task | None, str | None]:
        payload = self._request("GET", f"/workers/{binding.worker_id}/tasks/pull", binding=binding)
        task = Task.from_api(payload["task"]) if payload.get("task") else None
        return str(payload.get("mode") or "idle"), task, payload.get("reason")

    def claim_task(self, binding: Binding, task: Task) -> Task:
        payload = self._request(
            "POST",
            f"/tasks/{task.id}/claim",
            binding=binding,
            json={"worker_id": binding.worker_id, "current_step": "claimed", "remark": "Worker 客户端已领取任务"},
        )
        return Task.from_api(payload)

    def report_step(self, binding: Binding, task_id: str, current_step: str, remark: str) -> Task:
        payload = self._request(
            "POST",
            f"/tasks/{task_id}/step",
            binding=binding,
            json={"current_step": current_step, "remark": remark},
        )
        return Task.from_api(payload)

    def complete_invite_sent(self, binding: Binding, task_id: str) -> Task:
        payload = self._request("POST", f"/tasks/{task_id}/invite-sent", binding=binding, json={"remark": "已发送添加通讯录邀请"})
        return Task.from_api(payload)

    def complete_already_friend(self, binding: Binding, task_id: str) -> Task:
        payload = self._request("POST", f"/tasks/{task_id}/already-friend", binding=binding, json={"remark": "客户已是好友"})
        return Task.from_api(payload)

    def fail_task(self, binding: Binding, task_id: str, error_code: str, failure_step: str | None, message: str) -> Task:
        payload = self._request(
            "POST",
            f"/tasks/{task_id}/fail",
            binding=binding,
            json={"error_code": error_code, "failure_step": failure_step, "failure_remark": message},
        )
        return Task.from_api(payload)

    def upload_evidence(
        self,
        binding: Binding,
        task_id: str,
        content: str,
        *,
        error_code: str | None = None,
        evidence_path: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        evidence_metadata = dict(metadata or {})
        if evidence_path:
            evidence_metadata.setdefault("evidence_path", evidence_path)
        self._request(
            "POST",
            f"/tasks/{task_id}/evidences",
            binding=binding,
            json={
                "evidence_type": "log",
                "content": content,
                "error_code": error_code,
                "remark": "Worker 客户端本机执行证据",
                "metadata": evidence_metadata,
            },
        )

    def post_wechat_session_scan_result(self, binding: Binding, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/workers/{binding.worker_id}/wechat/sessions/scan-result", binding=binding, json=payload)

    def get_wechat_read_targets(self, binding: Binding, *, limit: int = 20) -> list[WechatReadTarget]:
        payload = self._request("GET", f"/workers/{binding.worker_id}/wechat/sessions/read-targets?limit={int(limit)}", binding=binding)
        targets = payload.get("targets") if isinstance(payload, dict) else []
        return [WechatReadTarget.from_api(item) for item in targets if isinstance(item, dict)]

    def post_wechat_messages_ingest(self, binding: Binding, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/workers/{binding.worker_id}/wechat/messages/ingest", binding=binding, json=payload)

    def _request(self, method: str, path: str, *, binding: Binding | None = None, json: dict[str, Any] | None = None) -> Any:
        headers = {"Content-Type": "application/json"}
        if binding:
            headers["X-Worker-Token"] = binding.worker_token
            headers["X-Client-Instance-Id"] = binding.client_instance_id
        response = self.session.request(method, f"{self.base_url}{path}", headers=headers, json=json, timeout=self.timeout)
        try:
            envelope = response.json()
        except ValueError as exc:
            raise ApiError("HTTP_ERROR", response.text or "服务端响应不是 JSON", response.status_code) from exc
        if response.status_code >= 400 or envelope.get("code") != "OK":
            raise ApiError(str(envelope.get("code") or "API_ERROR"), str(envelope.get("message") or "接口调用失败"), response.status_code, envelope.get("data"))
        return envelope.get("data")
