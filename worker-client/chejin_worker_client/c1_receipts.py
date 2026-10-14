"""Original C1 result receipts shared by OCR and Hook; no UI actions.

Owners provide their recovery lock, durable stop entry and settlement API.
Existing receipt keys, identity checks and confirmation rules are preserved.
"""
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .models import Binding, Task, RpaResult
from . import storage
from .transaction_outcomes import c1_stop_status
from .action_journal import action_journal_path, read_action_journal, action_journal_phase, remove_action_journal

def confirmed_add_friend_result(task, receipt, kind):
    terminal_matches = (
        task.status == "completed" and task.result_code == receipt["result_code"]
        if kind == "success" else
        (task.status == "failed" and task.error_code == receipt["error_code"]
         or task.status == "cancelled" and task.raw.get("cancel_reason") == "LEAD_INVALID")
    )
    return (task.id == receipt["flow_id"] and task.raw.get(f"{kind}_receipt") == receipt and terminal_matches)


def clear_confirmed_add_friend_receipt(flow_id, worker_id, client_instance_id):
    key=f'inflight_finish_receipt:{flow_id}'
    saved=storage.load_c2_state(key) or {}
    for kind in ('failure','success'):
        receipt=saved.get(f'task_{kind}') or {}
        if saved.get(f'task_{kind}_confirmed') is True:
            if (receipt.get('flow_id')!=flow_id or receipt.get('worker_id')!=worker_id
                    or receipt.get('client_instance_id')!=client_instance_id):
                raise ValueError('HOOK_C1_RECEIPT_IDENTITY_MISMATCH')
            storage.clear_c2_state(key)
            return


class AddFriendResultReceipts:
    @staticmethod
    def _inflight_finish_receipt_key(flow_id):
        return f"inflight_finish_receipt:{str(flow_id or '').strip()}"

    def _save_add_friend_result(
        self, binding: Binding, task: Task, result: RpaResult, *, kind: str,
    ) -> None:
        """Persist the original result and finish dependency before any HTTP."""
        if kind not in {"failure", "success"}:
            raise ValueError("Unknown task result kind")
        mismatch = f"TASK_{kind.upper()}_RECEIPT_MISMATCH"
        if kind == "success" and (not result.ok or result.result_code not in {"invite_sent", "already_friend"}):
            raise RuntimeError(mismatch)
        with self._restart_recovery_lock:
            if storage.load_runtime_control().get("inflight_flow_id") != task.id:
                raise RuntimeError(mismatch)
            key = self._inflight_finish_receipt_key(task.id)
            saved = self._receipt_load(key) or {}
            field = f"task_{kind}"
            details = (
                {"result_code": result.result_code,
                 "remark": "客户已是好友" if result.result_code == "already_friend" else "已发送添加通讯录邀请"}
                if kind == "success" else
                {"error_code": result.error_code or "OTHER",
                 "failure_step": result.failure_step, "failure_remark": result.message}
            )
            receipt = {
                "task_id": task.id, "task_type": "add_friend", "flow_id": task.id,
                "worker_id": binding.worker_id, "client_instance_id": binding.client_instance_id,
                "lease_fencing_token": task.lease_fencing_token, **details,
            }
            other = "task_failure" if kind == "success" else "task_success"
            if saved.get(other) or (saved.get(field) and saved[field] != receipt):
                raise RuntimeError(mismatch)
            self._receipt_save(key, {
                **saved, field: receipt,
                f"{field}_confirmed": saved.get(f"{field}_confirmed") is True,
                "terminal_kind": "task_terminal", "finish_stage": "dependencies",
                "finish_request": {
                    "flow_id": task.id, "worker_id": binding.worker_id,
                    "client_instance_id": binding.client_instance_id,
                    "terminal_kind": "task_terminal", "conversation_id": None, "error_code": None,
                },
            })


    def _deliver_add_friend_result(self, binding: Binding, flow_id: str) -> Task | None:
        """Replay only a saved result; confirm before consuming its journal."""
        with self._restart_recovery_lock:
            key = self._inflight_finish_receipt_key(flow_id)
            saved = self._receipt_load(key) or {}
            kinds = [kind for kind in ("failure", "success") if saved.get(f"task_{kind}")]
            if not kinds:
                return None
            if len(kinds) != 1:
                raise RuntimeError("TASK_SUCCESS_RECEIPT_MISMATCH")
            kind = kinds[0]
            field = f"task_{kind}"
            receipt = saved[field]
            local_flow_id = storage.load_runtime_control().get("inflight_flow_id")
            if (receipt.get("task_id") != flow_id or receipt.get("flow_id") != flow_id
                    or receipt.get("task_type") != "add_friend"
                    or receipt.get("worker_id") != binding.worker_id
                    or receipt.get("client_instance_id") != binding.client_instance_id
                    or (local_flow_id != flow_id and (
                        local_flow_id or saved.get(f"{field}_confirmed") is not True))):
                raise RuntimeError(f"TASK_{kind.upper()}_RECEIPT_MISMATCH")
            # A crash can occur after saving the failure but before saving its
            # stop state. Restore the stop decision before any settlement.
            error_code = receipt.get("error_code") if kind == "failure" else None
            stop_status = c1_stop_status(error_code)
            if stop_status:
                if binding.run_status == "faulted":
                    stop_status = "faulted"
                if (binding.run_status != stop_status
                        or not storage.load_runtime_control().get("pause_requested")
                        or self._run_status_persistence_pending):
                    self.set_run_status(stop_status)
            if saved.get(f"{field}_confirmed") is True:
                return None
            task = (self.api.settle_task_success(binding, receipt) if kind == "success"
                    else self.api.settle_task_failure(binding, receipt))
            if not confirmed_add_friend_result(task, receipt, kind):
                raise RuntimeError(f"TASK_{kind.upper()}_RECEIPT_UNCONFIRMED")
            path = action_journal_path("add_friend", flow_id)
            if read_action_journal(path) and action_journal_phase(path) != "trigger_attempted":
                remove_action_journal(path)
            self._receipt_save(key, {**saved, f"{field}_confirmed": True})
            storage.append_log("INFO", f"task_{kind}_receipt_confirmed", "原任务结果已获后端确认，继续结束原流程。", task_id=flow_id)
            return task
