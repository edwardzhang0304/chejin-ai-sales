"""Adapt persisted launch facts to the original sent-ACK/Flow recovery loop."""
from __future__ import annotations

from . import storage
from .c2_contract import c2_contract_v3
from .update_filesystem import update_filesystem_path
from apps.wechat_ai_customer_service.adapters import send_launch_journal as launches, send_setup_contract


def context_for_claim(claim, target):
    return {"reply_action_id": claim.reply_action_id, "task_id": claim.task_id,
            "conversation_id": target.conversation_id,
            "flow_id": str(storage.load_runtime_control().get("inflight_flow_id") or ""),
            "authorization_revision": target.authorization_revision, "reply_text_hash": claim.reply_text_hash}


def stop_before_ack(runner, record):
    evidence = (record.get("ack_payload") or {}).get("evidence") or {}
    if "pre_send_setup_failure" not in evidence:
        return True
    send_setup_contract.validate_proof(evidence["pre_send_setup_failure"], contract=c2_contract_v3())
    # set_run_status persists the stop first and returns true only after the
    # backend confirms that same revision. A failed request stays pending.
    if (runner.binding.run_status == "faulted" and runner._backend_confirmed_run_status == "faulted"
            and runner._pending_run_status_sync is None):
        return True
    return runner.set_run_status("faulted")


def prepare_receipt_recovery(runner, binding):
    for record in storage.list_reply_send_ack_outbox(limit=20):
        evidence = (record.get("ack_payload") or {}).get("evidence") or {}
        context = evidence.get("pre_send_setup_context")
        if record.get("status") != "intent" or not isinstance(context, dict):
            continue
        path = update_filesystem_path(runner.bridge.send_transaction_journal_path(record["reply_action_id"]))
        try:
            journal, _ = launches.read(path)
        except (OSError, ValueError, TypeError):
            # No launch record is not evidence of a startup failure. Let the
            # existing physical/read-journal recovery keep its original rules.
            continue
        attempts = journal.get("send_launch_attempts") or []
        if not attempts:
            continue
        from apps.wechat_ai_customer_service.adapters.pre_send_read_failure import read_failure_valid
        if (attempts[-1].get("process_state") == "finished"
                and read_failure_valid(attempts[-1].get("read_failure_fact"))):
            # A completed original OCR failure belongs to its existing
            # receipt/recheck owner. Missing/timeout results do not: even a
            # not_attempted physical journal alone cannot prove non-start.
            continue
        # Never settle while the existing owner can still be acting.
        if runner.current_ui_lock is not None or runner.bridge.sidecar_active():
            return False
        if not runner.set_run_status("faulted"):
            return False
        try:
            original = (journal.get("prepare_evidence") or {}).get("pre_send_setup_context")
            if original != context:
                return False
            attempt = attempts[-1]
            if attempt.get("process_state") in {"preparing", "prepared"}:
                # The creating record is a mandatory durable barrier before
                # Popen. These earlier states prove it was never called.
                launches.fail(path, attempt["launch_attempt_id"],
                    error_code="RPA_SEND_REQUEST_PREPARE_FAILED", process_state="not_called",
                    reason="preparation_interrupted_before_process_creation")
            proof = launches.proof(path, contract=c2_contract_v3())
            references = launches.references(path)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            storage.append_log("ERROR", "send_setup_recovery_evidence_unavailable", str(exc),
                               task_id=record["task_id"], error_code=type(exc).__name__)
            return False
        if proof:
            payload = runner._reply_send_ack_payload(
                send_result="failed", action_phase="not_attempted", reply_text_hash=record["reply_text_hash"],
                evidence={"pre_send_setup_failure": proof, "send_request_files": references},
                error_code=proof["error_code"], remark="原发送准备或启动失败；未操作微信，补交原失败结果。")
        else:
            # A creating/started record with no explicit failure is ambiguous.
            # Even a not_attempted physical journal alone cannot prove that
            # an unobserved child did not run. Keep the existing unknown guard.
            phase = runner._send_transaction_journal_phase(record["reply_action_id"])
            result = "sent" if phase == "confirmed" else "unknown"
            payload = runner._reply_send_ack_payload(
                send_result=result, action_phase="confirmed" if result == "sent" else "trigger_attempted",
                reply_text_hash=record["reply_text_hash"], evidence={"send_request_files": references},
                error_code=None if result == "sent" else "SEND_INTERRUPTED_BEFORE_RESULT_PERSISTED",
                remark="沿用原物理动作记录结算；不执行旧资料包或自动补发。")
        storage.finalize_reply_send_ack(reply_action_id=record["reply_action_id"], ack_payload=payload)
    return True
