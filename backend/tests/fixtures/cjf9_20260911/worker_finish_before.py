# Frozen pre-fix production method for recovery/ablation only.
def _finish_inflight_flow_locked(
    self,
    binding: Binding,
    flow_id: str,
    *,
    terminal_kind: str,
    conversation_id: str | None = None,
    error_code: str | None = None,
) -> None:
    self.api.finish_inflight_flow(
        binding,
        flow_id=flow_id,
        terminal_kind=terminal_kind,
        conversation_id=conversation_id,
        error_code=error_code,
    )
    # HTTP accepted this exact receipt. Local pointer clearing may already
    # have committed before clearing the receipt failed on the last attempt.
    # Never clear a different local Flow; the storage compare guards it.
    if load_runtime_control().get("inflight_flow_id"):
        finish_runtime_flow(flow_id)
    clear_c2_state(self._inflight_finish_receipt_key(flow_id))
    self._backend_inflight_flow_state = {}
    if self._restart_recovery_flow_id == flow_id:
        self._restart_recovery_flow_id = None
