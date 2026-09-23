"""An unsent reply failure is a dependency of the existing Flow finish receipt."""
from .storage import load_c2_state, load_runtime_control, save_c2_state

ERROR = "C2_TEXT_BUBBLE_GROUPING_UNCONFIRMED"
FIELD = "reply_read_failure"


def save(runner, binding, *, task_id, conversation_id, failure_step):
    runtime = load_runtime_control()
    flow_id = str(runtime.get("inflight_flow_id") or "")
    flow_kind = runtime.get("inflight_flow_kind")
    if (not flow_id or flow_kind not in {"c2_read", "task", "chat_reply"}
            or not task_id or not conversation_id
            or failure_step not in {"pre_send_refresh", "reply_sequence_read"}):
        raise ValueError("REPLY_READ_FAILURE_SCOPE_INVALID")
    record = {
        "task_id": task_id, "conversation_id": conversation_id,
        "flow_id": flow_id, "flow_kind": flow_kind,
        "worker_id": binding.worker_id, "client_instance_id": binding.client_instance_id,
        "lease_fencing_token": runner.api._task_lease_token(task_id),
        "error_code": ERROR, "failure_step": failure_step,
    }
    key = runner._inflight_finish_receipt_key(flow_id)
    receipt = load_c2_state(key) or {}
    if receipt.get(FIELD) and receipt[FIELD] != record:
        raise ValueError("REPLY_READ_FAILURE_RECEIPT_MISMATCH")
    terminal = "technical_failed" if flow_kind == "c2_read" else "task_terminal"
    request = {k: record[k] for k in ("flow_id", "worker_id", "client_instance_id", "conversation_id", "error_code")}
    request["terminal_kind"] = terminal
    # Persist before the first HTTP request and before the outer read finalizer.
    # The existing Flow retry owner also covers a crash in either location.
    save_c2_state(key, {**receipt, FIELD: record, "terminal_kind": terminal,
        "conversation_id": conversation_id, "error_code": ERROR,
        "finish_request": request, "finish_stage": "dependencies"})
    return flow_id


def deliver(runner, binding, flow_id):
    key = runner._inflight_finish_receipt_key(flow_id)
    receipt = load_c2_state(key) or {}
    record = receipt.get(FIELD)
    if not record:
        return
    runtime = load_runtime_control()
    if (record.get("flow_id") != flow_id or runtime.get("inflight_flow_id") != flow_id
            or record.get("flow_kind") != runtime.get("inflight_flow_kind")
            or record.get("worker_id") != binding.worker_id
            or record.get("client_instance_id") != binding.client_instance_id
            or record.get("conversation_id") != receipt.get("conversation_id")
            or record.get("error_code") != ERROR):
        raise ValueError("REPLY_READ_FAILURE_RECEIPT_MISMATCH")
    if receipt.get(FIELD + "_confirmed") is True:
        return
    task = runner.api.settle_reply_read_failure(binding, record)
    if (task.id != record["task_id"] or task.status not in {"failed", "cancelled"}
            or task.raw.get(FIELD) != record):
        raise ValueError("REPLY_READ_FAILURE_SETTLEMENT_UNCONFIRMED")
    save_c2_state(key, {**receipt, FIELD + "_confirmed": True})


def assert_confirmed(receipt):
    if receipt.get(FIELD) and receipt.get(FIELD + "_confirmed") is not True:
        raise RuntimeError("RUNTIME_INFLIGHT_REPLY_READ_FAILURE_PENDING")
