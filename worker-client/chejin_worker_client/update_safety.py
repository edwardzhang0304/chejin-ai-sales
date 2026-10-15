"""One business-boundary projection shared by update discovery and installation."""


def business_wait_reason(snapshot: dict) -> tuple[str, str]:
    checks = (
        (("hook_unresolved_calls",), "UPDATE_WAITING_HOOK_CALL", "微信接口仍在执行或结果未确认，请先完成恢复核对"),
        (("current_task", "inflight_flow_id"), "UPDATE_WAITING_ACTIVE_FLOW", "正在等待当前任务安全结束"),
        (("task_lease_active",), "UPDATE_WAITING_TASK_LEASE", "正在等待任务租约释放"),
        (("ui_lock_active",), "UPDATE_WAITING_UI_LOCK", "正在等待微信操作或写入结束"),
        (("sidecar_active",), "UPDATE_WAITING_SIDECAR", "正在等待自动化操作安全结束"),
        (("action_journal_state_unavailable",), "UPDATE_WAITING_DURABLE_SETTLEMENT", "无法确认本地操作记录，请先检查本机存储"),
        (("pending_sqlite_action_journal", "pending_file_action_journal"), "UPDATE_WAITING_DURABLE_SETTLEMENT", "上次操作结果尚未确认，正在等待核对"),
        (("pending_sent_ack",), "UPDATE_WAITING_DURABLE_SETTLEMENT", "发送结果尚未回传确认，正在等待结算"),
        (("waiting_ledger", "pending_c2_outbox", "hook_pending_delivery"), "UPDATE_WAITING_DURABLE_SETTLEMENT", "本地消息尚未回传或结算，正在等待处理"),
    )
    for keys, code, text in checks:
        if any(snapshot.get(key) for key in keys):
            return code, text
    return "", ""
