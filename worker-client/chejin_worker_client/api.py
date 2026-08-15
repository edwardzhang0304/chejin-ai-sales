from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlencode

import requests

from .config import CONFIG
from .models import Binding, ReplySendClaim, Task, WechatReadTarget, WorkerProfile


class ApiError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        data: Any = None,
        trace_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.data = data
        self.trace_id = str(trace_id or "").strip() or None
        self.retryable = (
            bool(data.get("retryable"))
            if isinstance(data, dict) and isinstance(data.get("retryable"), bool)
            else None
        )
        self.recovery_action = (
            str(data.get("recovery_action") or "").strip()
            if isinstance(data, dict)
            else ""
        ) or None


class WorkerApiClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or CONFIG.api_base_url).rstrip("/")
        self.session = requests.Session()
        self.timeout = CONFIG.api_timeout_seconds
        self.task_lease_fencing_tokens: dict[str, int] = {}
        self.inflight_flow_id: str | None = None

    def _remember_task_lease(self, task: Task | None) -> Task | None:
        if task and task.lease_fencing_token > 0:
            self.task_lease_fencing_tokens[task.id] = task.lease_fencing_token
        return task

    def _task_lease_headers(self, task_id: str) -> dict[str, str]:
        token = int(self.task_lease_fencing_tokens.get(task_id) or 0)
        return {"X-Task-Lease-Fencing-Token": str(token)} if token > 0 else {}

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

    def start_inflight_flow(
        self, binding: Binding, *, flow_id: str, flow_kind: str
    ) -> dict[str, Any]:
        payload = self._request(
            "POST",
            f"/workers/{binding.worker_id}/inflight-flow/start",
            binding=binding,
            json={"flow_id": flow_id, "flow_kind": flow_kind},
        )
        self.inflight_flow_id = flow_id
        return dict(payload or {})

    def finish_inflight_flow(
        self,
        binding: Binding,
        *,
        flow_id: str,
        terminal_kind: str,
        conversation_id: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        payload = self._request(
            "POST",
            f"/workers/{binding.worker_id}/inflight-flow/finish",
            binding=binding,
            json={
                "flow_id": flow_id,
                "terminal_kind": terminal_kind,
                "conversation_id": conversation_id,
                "error_code": error_code,
            },
            extra_headers={"X-Inflight-Flow-Id": flow_id},
        )
        if self.inflight_flow_id == flow_id:
            self.inflight_flow_id = None
        return dict(payload or {})

    def pull_task(self, binding: Binding) -> tuple[str, Task | None, str | None]:
        payload = self._request("GET", f"/workers/{binding.worker_id}/tasks/pull", binding=binding)
        task = Task.from_api(payload["task"]) if payload.get("task") else None
        self._remember_task_lease(task)
        return str(payload.get("mode") or "idle"), task, payload.get("reason")

    def claim_task(
        self,
        binding: Binding,
        task: Task,
        *,
        claim_source: str | None = None,
        conversation_id: str | None = None,
    ) -> Task:
        payload = self._request(
            "POST",
            f"/tasks/{task.id}/claim",
            binding=binding,
            json={
                "worker_id": binding.worker_id,
                "current_step": "claimed",
                "remark": "Worker 客户端已领取任务",
                "claim_source": claim_source,
                "conversation_id": conversation_id,
            },
        )
        return self._remember_task_lease(Task.from_api(payload))  # type: ignore[return-value]

    def renew_task_lease(
        self,
        binding: Binding,
        task_id: str,
        *,
        current_step: str | None,
    ) -> Task:
        token = int(self.task_lease_fencing_tokens.get(task_id) or 0)
        if token <= 0:
            raise ApiError("TASK_LEASE_FENCING_MISSING", "缺少任务租约 fencing token", 409)
        payload = self._request(
            "POST",
            f"/tasks/{task_id}/lease/renew",
            binding=binding,
            json={
                "lease_fencing_token": token,
                "current_step": current_step,
            },
        )
        return self._remember_task_lease(Task.from_api(payload))  # type: ignore[return-value]

    def report_step(self, binding: Binding, task_id: str, current_step: str, remark: str) -> Task:
        payload = self._request(
            "POST",
            f"/tasks/{task_id}/step",
            binding=binding,
            json={"current_step": current_step, "remark": remark},
            extra_headers=self._task_lease_headers(task_id),
        )
        return Task.from_api(payload)

    def claim_send(self, binding: Binding, task: Task) -> ReplySendClaim:
        if not task.reply_action_id:
            raise ApiError("REPLY_ACTION_NOT_FOUND", "chat_reply 任务缺少 reply_action_id", 409)
        payload = self._request(
            "POST",
            f"/reply-actions/{task.reply_action_id}/claim-send",
            binding=binding,
            json={"task_id": task.id, "worker_id": binding.worker_id},
            extra_headers=self._task_lease_headers(task.id),
        )
        return ReplySendClaim.from_api(payload)

    def sent_ack(
        self,
        binding: Binding,
        claim: ReplySendClaim,
        *,
        send_result: str,
        action_phase: str,
        reply_text_hash: str | None,
        sidecar_run_id: str | None = None,
        evidence: dict[str, Any] | None = None,
        error_code: str | None = None,
        remark: str | None = None,
        sent_at: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/reply-actions/{claim.reply_action_id}/sent-ack",
            binding=binding,
            json={
                "send_token": claim.send_token,
                "task_id": claim.task_id,
                "worker_id": binding.worker_id,
                "client_instance_id": binding.client_instance_id,
                "send_result": send_result,
                "action_phase": action_phase,
                "sent_at": sent_at,
                "reply_text_hash": reply_text_hash,
                "sidecar_run_id": sidecar_run_id,
                "evidence": evidence or {},
                "error_code": error_code,
                "remark": remark,
            },
        )

    def complete_invite_sent(self, binding: Binding, task_id: str) -> Task:
        payload = self._request(
            "POST",
            f"/tasks/{task_id}/invite-sent",
            binding=binding,
            json={"remark": "已发送添加通讯录邀请"},
            extra_headers=self._task_lease_headers(task_id),
        )
        self.task_lease_fencing_tokens.pop(task_id, None)
        return Task.from_api(payload)

    def complete_already_friend(self, binding: Binding, task_id: str) -> Task:
        payload = self._request(
            "POST",
            f"/tasks/{task_id}/already-friend",
            binding=binding,
            json={"remark": "客户已是好友"},
            extra_headers=self._task_lease_headers(task_id),
        )
        self.task_lease_fencing_tokens.pop(task_id, None)
        return Task.from_api(payload)

    def fail_task(self, binding: Binding, task_id: str, error_code: str, failure_step: str | None, message: str) -> Task:
        payload = self._request(
            "POST",
            f"/tasks/{task_id}/fail",
            binding=binding,
            json={"error_code": error_code, "failure_step": failure_step, "failure_remark": message},
            extra_headers=self._task_lease_headers(task_id),
        )
        self.task_lease_fencing_tokens.pop(task_id, None)
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

    def get_wechat_read_authorization(
        self,
        binding: Binding,
        conversation_id: str,
        *,
        continuation_batch_id: str | None = None,
        continuation_token: str | None = None,
        recovery_transaction_id: str | None = None,
        action_kind: str | None = None,
        source_message_key_digest: str | None = None,
        original_authorization_revision: str | None = None,
    ) -> dict[str, Any]:
        query_values: dict[str, str] = {}
        if continuation_batch_id and continuation_token:
            query_values["continuation_batch_id"] = continuation_batch_id
        recovery_values = {
            "recovery_transaction_id": recovery_transaction_id,
            "action_kind": action_kind,
            "source_message_key_digest": source_message_key_digest,
            "original_authorization_revision": (
                original_authorization_revision
            ),
        }
        query_values.update(
            {
                key: str(value)
                for key, value in recovery_values.items()
                if str(value or "").strip()
            }
        )
        query = f"?{urlencode(query_values)}" if query_values else ""
        payload = self._request(
            "GET",
            (
                f"/workers/{binding.worker_id}/wechat/conversations/"
                f"{conversation_id}/read-authorization{query}"
            ),
            binding=binding,
            extra_headers=(
                {"X-C2-Continuation-Token": continuation_token}
                if continuation_token
                else None
            ),
        )
        return dict(payload) if isinstance(payload, dict) else {}

    def confirm_wechat_friend_activation(
        self,
        binding: Binding,
        target: WechatReadTarget,
        *,
        conversation_type: str,
        chat_surface_ready: bool,
        title_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/workers/{binding.worker_id}/wechat/conversations/{target.conversation_id}/activation-confirm",
            binding=binding,
            json={
                "authorization_revision": target.authorization_revision,
                "remark_code": target.remark_code,
                "conversation_type": conversation_type,
                "chat_surface_ready": chat_surface_ready,
                "title_evidence": title_evidence,
            },
        )

    def post_wechat_messages_ingest(
        self,
        binding: Binding,
        payload: dict[str, Any],
        *,
        settlement_token: str | None = None,
        process_run_id: str | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if settlement_token:
            headers["X-C2-Settlement-Token"] = settlement_token
        if process_run_id:
            headers["X-Process-Run-Id"] = process_run_id
        return self._request(
            "POST",
            f"/workers/{binding.worker_id}/wechat/messages/ingest",
            binding=binding,
            json=payload,
            extra_headers=headers or None,
        )

    def get_wechat_message_batch(self, binding: Binding, batch_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/workers/{binding.worker_id}/wechat/message-batches/{batch_id}",
            binding=binding,
        )

    def post_observability_stage_events(
        self,
        binding: Binding,
        events: list[dict[str, Any]],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Upload side-channel telemetry without changing normal API timeout state."""

        headers = {
            "Content-Type": "application/json",
            "X-Worker-Token": binding.worker_token,
            "X-Client-Instance-Id": binding.client_instance_id,
        }
        # A private Session avoids sharing connection-pool state with the
        # business API client from the telemetry daemon thread.
        with requests.Session() as isolated_session:
            response = isolated_session.post(
                (
                    f"{self.base_url}/workers/{binding.worker_id}"
                    "/observability/stage-events"
                ),
                headers=headers,
                json={"events": events},
                timeout=self.timeout if timeout is None else timeout,
            )
        try:
            envelope = response.json()
        except ValueError as exc:
            raise ApiError(
                "HTTP_ERROR",
                response.text or "观测接口响应不是 JSON",
                response.status_code,
            ) from exc
        if response.status_code >= 400 or envelope.get("code") != "OK":
            raise ApiError(
                str(envelope.get("code") or "API_ERROR"),
                str(envelope.get("message") or "观测上报失败"),
                response.status_code,
                envelope.get("data"),
                envelope.get("trace_id"),
            )
        return dict(envelope.get("data") or {})

    def _request(
        self,
        method: str,
        path: str,
        *,
        binding: Binding | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        headers = {"Content-Type": "application/json"}
        if binding:
            headers["X-Worker-Token"] = binding.worker_token
            headers["X-Client-Instance-Id"] = binding.client_instance_id
            if self.inflight_flow_id:
                headers["X-Inflight-Flow-Id"] = self.inflight_flow_id
        headers.update(extra_headers or {})
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            json=json,
            timeout=self.timeout if timeout is None else timeout,
        )
        try:
            envelope = response.json()
        except ValueError as exc:
            raise ApiError("HTTP_ERROR", response.text or "服务端响应不是 JSON", response.status_code) from exc
        if response.status_code >= 400 or envelope.get("code") != "OK":
            raise ApiError(
                str(envelope.get("code") or "API_ERROR"),
                str(envelope.get("message") or "接口调用失败"),
                response.status_code,
                envelope.get("data"),
                envelope.get("trace_id"),
            )
        return envelope.get("data")
