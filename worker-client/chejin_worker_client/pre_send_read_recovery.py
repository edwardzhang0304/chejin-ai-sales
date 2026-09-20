"""One durable extra read per reply action, using the existing C2 SQLite row.

No background loop or desktop operation lives here. The current TaskRunner
owns execution and reuses the existing receipt/Flow recovery barriers.
"""
from __future__ import annotations

from copy import deepcopy
import json
import time
from uuid import uuid4

from . import storage
from .emergency_stop import emergency_stop_requested

PREFIX = "pre_send_read_failure:"
BOOT_ID = uuid4().hex


def _decode(raw):
    value = json.loads(raw)
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("PRE_SEND_READ_RECOVERY_STATE_INVALID")
    return value


def _change(action_id, updater):
    if not action_id:
        raise ValueError("PRE_SEND_READ_RECOVERY_IDENTITY_MISSING")
    with storage.db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM c2_runtime_state WHERE key=?", (PREFIX + action_id,)).fetchone()
        prior = _decode(row["value"]) if row else None
        value, result = updater(prior)
        conn.execute("INSERT INTO c2_runtime_state(key,value,updated_at) VALUES(?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                     (PREFIX + action_id, json.dumps(value, ensure_ascii=False), storage.utc_now_iso()))
        conn.commit()
    return result


def reserve(context, failure, *, target=""):
    """Commit before the caller may perform its one extra fresh read.

    Corrupt stored data raises, rather than being treated as an unused budget.
    Existing rows retain their first failure across stages and restarts.
    """
    def update(prior):
        if prior is not None:
            if prior["context"] != context:
                raise ValueError("PRE_SEND_READ_RECOVERY_IDENTITY_CHANGED")
            value = {**prior, "target": prior.get("target") or target}
            return value, (False, value)
        value = {"version": 1, "context": deepcopy(context), "first_failure": deepcopy(failure),
                 "budget_state": "consumed", "started": False, "boot_id": BOOT_ID,
                 "status": "reserved", "target": target, "created_at": storage.utc_now_iso()}
        return value, (True, value)
    return _change(context["reply_action_id"], update)


def complete_attempt(action_id, *, failure=None):
    def update(prior):
        if prior is None or prior["status"] not in {"reserved", "checked", "claim_pending"}:
            raise ValueError("PRE_SEND_READ_RECOVERY_BUDGET_MISSING")
        value = {**prior, "status": "checked", "started": True,
                 "failure": deepcopy(failure), "checked_at": storage.utc_now_iso()}
        if failure is not None:
            value["send_in_progress"] = False
        return value, value
    return _change(action_id, update)


def _input_progress(record):
    if record.get("send_in_progress"):
        return "may_have_started"
    failure = record.get("failure") or record["first_failure"]
    return failure.get("input_progress") or (
        "may_have_started" if failure["stage"] == "before_trigger" else "not_started")


def _retain_input_requirement(record):
    """Only possibly owned input needs an extra check; S0 is not a draft."""
    if record.get("input_safety"):
        return record
    proof = record.get("proof") or {}
    failure = record.get("failure") or record["first_failure"]
    progress = proof.get("input_progress") or _input_progress(record)
    state = proof.get("input_state") or failure["input_state"]
    from apps.wechat_ai_customer_service.adapters.pre_send_read_failure import replacement_input_ready
    replaceable = (not record.get("send_in_progress")
                   and replacement_input_ready(failure, context=record["context"], target=record.get("target")))
    if progress == "may_have_started" and state not in {"empty", "cleared"} and not replaceable:
        return {**record, "input_safety": {"status": "pending", "generation": uuid4().hex,
                                          "target": record.get("target") or ""}}
    return record


def terminal_proof(record, *, phase_proof, input_state, interruption_reason=None):
    from apps.wechat_ai_customer_service.adapters.pre_send_read_failure import validate_proof
    failure = record.get("failure")
    interrupted = bool(interruption_reason) or not failure
    proof = {"version": 1, **record["context"], "first_failure": record["first_failure"],
             "recheck": {"budget_state": record["budget_state"], "started": bool(record.get("started")),
                         "failure": failure, **({"interruption_reason": interruption_reason or "process_interrupted"} if interrupted else {})},
             "outcome": "interrupted" if interrupted else "exhausted",
             "terminal_phase_proof": phase_proof, "input_state": input_state,
             "input_progress": _input_progress(record)}
    return validate_proof(proof)


def save_settlement(record, *, proof, request):
    def update(prior):
        if prior is not None and prior["context"] != record["context"]:
            raise ValueError("PRE_SEND_READ_RECOVERY_IDENTITY_CHANGED")
        value = {**(prior or record), "status": "settlement_pending", "proof": deepcopy(proof),
                 "request": deepcopy(request)}
        value = _retain_input_requirement(value)
        return value, value
    return _change(record["context"]["reply_action_id"], update)


def mark_settled(action_id):
    from .shared_rules import send_interruption
    ack = storage.load_reply_send_ack_outbox(action_id)
    payload = (ack or {}).get("ack_payload") or {}
    sent_confirmed = bool(ack and ack.get("status") == "confirmed"
                          and payload.get("send_result") == "sent")
    def update(prior):
        if prior is None:
            raise ValueError("PRE_SEND_READ_RECOVERY_BUDGET_MISSING")
        value = {**prior, "status": "settled", "settled_at": storage.utc_now_iso()}
        if sent_confirmed:
            value["input_safety"] = {"status": "cleared", "reason": "original_send_confirmed"}
        elif (ack and ack.get("status") == "confirmed"
              and payload.get("reply_text_hash") == prior["context"]["reply_text_hash"]
              and send_interruption.confirmed_customer_interruption(
                  send_result=payload.get("send_result"), action_phase=payload.get("action_phase"),
                  error_code=payload.get("error_code"), evidence=payload.get("evidence"),
                  target=prior.get("target") or "")):
            # A completed recheck can end in a newly observed customer message.
            # Keep the same ownership/cleanup rule as its confirmed receipt;
            # never inherit the earlier in-progress marker as a new draft gate.
            value["send_in_progress"] = False
            value["input_safety"] = {"status": "replacement_ready", "reason": "confirmed_customer_interruption"}
        else:
            value = _retain_input_requirement(value)
        return value, value
    return _change(action_id, update)


def mark_settled_if_present(action_id):
    with storage.db_connection() as conn:
        row = conn.execute("SELECT 1 FROM c2_runtime_state WHERE key=?", (PREFIX + action_id,)).fetchone()
    if row:
        mark_settled(action_id)


def record_claim_attempt(action_id, *, lease_fencing_token):
    """Preserve original lease identity before a potentially lost response.

    Only actions already enrolled by a real read failure use this path.
    Normal replies do not acquire new recovery records.
    """
    with storage.db_connection() as conn:
        exists = conn.execute("SELECT 1 FROM c2_runtime_state WHERE key=?", (PREFIX + action_id,)).fetchone()
    if not exists:
        return False
    def update(prior):
        if prior is None or prior["status"] == "settled":
            raise ValueError("PRE_SEND_READ_RECOVERY_BUDGET_MISSING")
        value = {**prior, "claim_attempt": {"lease_fencing_token": lease_fencing_token},
                 "status": "claim_pending"}
        return value, True
    return _change(action_id, update)


def interrupt(action_id, reason):
    def update(prior):
        if prior is None:
            raise ValueError("PRE_SEND_READ_RECOVERY_BUDGET_MISSING")
        value = {**prior, "status": "settlement_pending", "interruption_reason": reason}
        return value, value
    return _change(action_id, update)


def pending_records(*, include_current=False):
    with storage.db_connection() as conn:
        rows = conn.execute("SELECT value FROM c2_runtime_state WHERE key LIKE ? ORDER BY updated_at,key", (PREFIX + "%",)).fetchall()
    result = []
    for row in rows:
        value = _decode(row["value"])
        if value["status"] == "settled":
            continue
        if include_current or value["status"] == "settlement_pending" or value.get("boot_id") != BOOT_ID:
            result.append(value)
    return result


def settlement_pending():
    return bool(pending_records(include_current=True))


def input_pending_records():
    with storage.db_connection() as conn:
        rows = conn.execute("SELECT value FROM c2_runtime_state WHERE key LIKE ? ORDER BY updated_at,key",
                            (PREFIX + "%",)).fetchall()
    return [value for row in rows if (value := _decode(row["value"])).get("input_safety", {}).get("status") == "pending"]


def _begin_input_observation(record, revision):
    request_id = uuid4().hex
    def update(prior):
        if (prior is None or prior["status"] != "settled"
                or prior.get("input_safety", {}).get("status") != "pending"
                or prior["input_safety"]["generation"] != record["input_safety"]["generation"]):
            raise ValueError("INPUT_SAFETY_OBSERVATION_STALE")
        value = {**prior, "input_safety": {**prior["input_safety"],
                 "request_id": request_id, "fault_revision": revision}}
        return value, request_id
    return _change(record["context"]["reply_action_id"], update)


def _finish_input_observation(record, revision, request_id, observation):
    from apps.wechat_ai_customer_service.adapters.pre_send_read_failure import empty_input_observation
    if not empty_input_observation(observation, target=record["input_safety"]["target"], request_id=request_id):
        return False
    def update(prior):
        gate = (prior or {}).get("input_safety") or {}
        if prior is None:
            raise ValueError("INPUT_SAFETY_OBSERVATION_STALE")
        if (prior["status"] != "settled" or gate.get("status") != "pending"
                or gate.get("generation") != record["input_safety"]["generation"]
                or gate.get("request_id") != request_id or gate.get("fault_revision") != revision):
            return prior, False
        value = {**prior, "input_safety": {**gate, "status": "cleared", "observation": deepcopy(observation),
                                          "cleared_at": storage.utc_now_iso()}}
        return value, True
    return _change(record["context"]["reply_action_id"], update)


def observe_pending_input(runner):
    """Existing task thread only; no pending draft means no extra capture.

    Receipt/Flow settlement comes first. Looking at B never clears A's gate.
    Successful observation only unlocks explicit Start, never starts work.
    """
    from .ui_lock import acquire_ui_lock
    binding = runner.binding
    if (not binding or binding.run_status != "faulted" or runner.stop_event.is_set()
            or emergency_stop_requested()):
        return
    now = time.monotonic()
    if now - getattr(runner, "_last_input_safety_observation_at", 0.0) < max(1.0, runner.heartbeat_interval_seconds):
        return
    with runner._restart_recovery_lock, runner._new_work_admission_lock, runner.reply_send_ack_lock:
        if (runner.current_task is not None or runner.current_ui_lock is not None
                or settlement_pending() or not runner.update_install_safety_snapshot()["settlement_complete"]):
            return
        pending = input_pending_records()
        if not pending:
            return
        record = pending[0]
        if not record["input_safety"].get("target"):
            return
        runner._last_input_safety_observation_at = now
        with runner._run_status_intent_lock:
            if runner.binding is not binding or binding.run_status != "faulted":
                return
            revision = runner._run_status_revision
        lease = None
        try:
            lease = acquire_ui_lock(operation_type="message_ingest",
                owner=f"{binding.worker_id}:{binding.client_instance_id}:input_safety",
                current_step="input_safety_observation", timeout_seconds=0.1)
            lease.start_auto_renew()
            runner.current_ui_lock = lease
            request_id = _begin_input_observation(record, revision)
            def cancelled():
                return (lease.cancel_requested() or runner.stop_event.is_set()
                        or emergency_stop_requested() or runner._run_status_revision != revision)
            result = runner.bridge.get_messages(
                display_name=record["input_safety"]["target"], rpa_session_key="",
                remark_code=record["input_safety"]["target"], target_mode="current",
                max_scroll_steps=0, max_snapshots=1, restore_to_latest=False,
                input_safety_observation=True, input_safety_request_id=request_id,
                cancel_check=cancelled)
            with runner._run_status_intent_lock:
                if (not cancelled() and runner.binding is binding and binding.run_status == "faulted"
                        and result.get("ui_action_performed") is False):
                    _finish_input_observation(record, revision, request_id, result.get("input_safety_observation"))
        except Exception as exc:
            # Missing frame, layout, target, lock or disk: keep the same gate.
            # This read-only observation cannot create a new technical fault.
            storage.append_log("WARN", "input_safety_observation_unavailable",
                               "输入框安全状态尚未确认，仍保持停止接单。",
                               metadata={"exception_type": type(exc).__name__})
        finally:
            if lease is not None:
                try:
                    lease.release()
                except Exception as exc:
                    storage.append_log("WARN", "input_safety_lock_release_pending",
                        "只读核验锁尚未释放，继续保留原有安全屏障。",
                        metadata={"exception_type": type(exc).__name__})
                finally:
                    runner.current_ui_lock = None


def _stop_after_interruption(runner, action_id, reason):
    try:
        interrupt(action_id, reason)
    finally:
        # Even if saving the recovery marker itself fails, the existing
        # fail-safe local stop/persistence barrier must run.
        runner.set_run_status("faulted")


def _record_send_start_if_present(action_id):
    with storage.db_connection() as conn:
        exists = conn.execute("SELECT 1 FROM c2_runtime_state WHERE key=?", (PREFIX + action_id,)).fetchone()
    if not exists:
        return
    def update(prior):
        if prior is None or prior["status"] == "settled":
            raise ValueError("PRE_SEND_READ_RECOVERY_BUDGET_MISSING")
        # Persist BEFORE entering Sidecar. A crash cannot prove that typing
        # never started merely because Enter was never attempted.
        value = {**prior, "send_in_progress": True}
        return value, None
    _change(action_id, update)


def send_with_recheck(runner, binding, *, target, claim, send):
    """Run the original send, granting one repeat only for proven read failure."""
    from apps.wechat_ai_customer_service.adapters.pre_send_read_failure import read_failure_valid, replacement_input_ready
    if claim.raw.get("settlement_only") is True or claim.raw.get("send_allowed") is False:
        raise ValueError("REPLY_SETTLEMENT_PERMIT_CANNOT_SEND")
    try:
        _record_send_start_if_present(claim.reply_action_id)
    except Exception:
        runner.set_run_status("faulted")
        raise
    result = send()
    failure = result.get("pre_send_read_failure_fact")
    if not read_failure_valid(failure):
        return result
    context = {"reply_action_id": claim.reply_action_id, "task_id": claim.task_id,
               "conversation_id": target.conversation_id,
               "flow_id": str(storage.load_runtime_control().get("inflight_flow_id") or ""),
               "authorization_revision": target.authorization_revision,
               "reply_text_hash": claim.reply_text_hash}
    reason = None
    try:
        allowed, record = reserve(context, failure, target=target.remark_code)
    except Exception as exc:
        allowed = False
        reason = "budget_persistence_failed:" + type(exc).__name__
        record = {"version": 1, "context": context, "first_failure": failure,
                  "budget_state": "persist_failed", "started": False, "boot_id": BOOT_ID,
                  "target": target.remark_code}
    safe_input = (failure["stage"] == "before_input" or failure["input_state"] in {"empty", "cleared"}
                  or replacement_input_ready(failure, context=context, target=target.remark_code))
    if allowed and safe_input:
        # Rerun the same original action through its fresh S0 target/input
        # checks. No second claim-send, stale frame or cached click coordinates.
        try:
            _record_send_start_if_present(claim.reply_action_id)
            result = send()
        except Exception:
            _stop_after_interruption(runner, claim.reply_action_id, "extra_read_or_send_interrupted")
            raise
        next_failure = result.get("pre_send_read_failure_fact")
        try:
            record = complete_attempt(claim.reply_action_id, failure=next_failure if read_failure_valid(next_failure) else None)
        except Exception:
            runner.set_run_status("faulted")
            # The extra call returned: retain its real outcome even when this
            # bookkeeping write failed. Never turn a sent result into failed.
            record = {**record, "started": True,
                      "failure": next_failure if read_failure_valid(next_failure) else None}
            reason = "recheck_result_persistence_failed"
        if not read_failure_valid(next_failure):
            return result
        failure = next_failure
    elif not safe_input:
        reason = "original_input_safety_unconfirmed"
    elif reason is None:
        # An earlier stage already spent this action's extra read. Keep the
        # first evidence; this observed failure never grants another attempt.
        record = complete_attempt(claim.reply_action_id, failure=failure)
    runner.set_run_status("faulted")
    proof = terminal_proof(record, phase_proof=failure["phase_proof"],
                           input_state=failure["input_state"], interruption_reason=reason)
    save_settlement(record, proof=proof, request={"receipt_kind": "sent_ack"})
    return {**result, "pre_send_read_failure": proof}


def prepare_receipt_recovery(runner, binding):
    """Called under the existing reply-ack lock before its normal replay.

    A foreign boot's reserved budget restores fault-stop first. A missing or
    contradictory action journal never becomes a fabricated unsent proof.
    """
    from .action_journal import read_action_journal, ACTION_PHASES
    include_current = binding.run_status == "faulted" and runner.current_ui_lock is None and runner.current_task is None
    for record in pending_records(include_current=include_current):
        identity = record["context"]
        action_id = identity["reply_action_id"]
        ack = storage.load_reply_send_ack_outbox(action_id)
        if ack and ack.get("status") == "confirmed":
            mark_settled(action_id)
            continue
        if not runner.set_run_status("faulted"):
            return False
        if not ack:
            if record["first_failure"]["stage"] != "pre_send_refresh":
                return False
            claim_attempt = record.get("claim_attempt")
            if isinstance(claim_attempt, dict):
                # Response loss is not a screenshot failure. Query only the
                # original permit; no send body is obtained or replayed.
                try:
                    permit = runner.api.original_send_permit(binding,
                        reply_action_id=action_id, task_id=identity["task_id"],
                        flow_id=identity["flow_id"],
                        lease_fencing_token=claim_attempt["lease_fencing_token"])
                except Exception as exc:
                    if getattr(exc, "code", None) != "REPLY_ACTION_ORIGINAL_PERMIT_MISSING":
                        return False
                else:
                    if (permit["conversation_id"] != identity["conversation_id"]
                            or permit["reply_text_hash"] != identity["reply_text_hash"]):
                        return False
                    storage.save_reply_send_intent(reply_action_id=action_id,
                        task_id=identity["task_id"],send_token=permit["send_token"],
                        reply_text_hash=permit["reply_text_hash"])
                    original_ack = permit.get("ack")
                    if original_ack is not None:
                        # Existing authoritative result wins over a late local
                        # failure. Replay that result through the normal ACK.
                        storage.finalize_reply_send_ack(reply_action_id=action_id,
                            ack_payload=runner._reply_send_ack_payload(
                                send_result=original_ack["send_result"], action_phase=original_ack["action_phase"],
                                reply_text_hash=identity["reply_text_hash"], error_code=original_ack.get("error_code")))
                        continue
                    ack = storage.load_reply_send_ack_outbox(action_id)
            if ack:
                # The journal below decides sent / unknown / proven unsent.
                # A missing journal must never default to not_attempted.
                pass
            else:
                if not record.get("proof"):
                    proof = terminal_proof(record,
                        phase_proof={"source": "read_only_before_claim", "ok": True, "action_phase": "not_attempted"},
                        input_state="unverified", interruption_reason="process_interrupted")
                    record = save_settlement(record, proof=proof,
                        request={"receipt_kind": "task_fail", "lease_fencing_token": (
                            claim_attempt["lease_fencing_token"] if isinstance(claim_attempt, dict)
                            else runner.api._task_lease_token(identity["task_id"]))})
                try:
                    runner.api.settle_pre_send_read_failure(binding, record)
                    mark_settled(action_id)
                except Exception:
                    return False
                continue
        if ack.get("status") != "intent":
            continue
        proof = record.get("proof")
        if not proof:
            journal = read_action_journal(runner.bridge.send_transaction_journal_path(action_id))
            items = journal.get("items")
            phases = [journal.get("action_phase"), *(
                [item.get("action_phase") for item in items.values() if isinstance(item, dict)]
                if isinstance(items, dict) else [])]
            proven = (journal.get("transaction_id") == action_id
                      and journal.get("conversation_id") == identity["conversation_id"]
                      and isinstance(items, dict) and bool(items)
                      and all(phase in ACTION_PHASES for phase in phases))
            phase = max(phases, key=ACTION_PHASES.index) if proven else "trigger_attempted"
            if phase != "not_attempted":
                result = "sent" if phase == "confirmed" else "unknown"
                storage.finalize_reply_send_ack(reply_action_id=action_id,
                    ack_payload=runner._reply_send_ack_payload(
                        send_result=result, action_phase="confirmed" if result == "sent" else "trigger_attempted",
                        reply_text_hash=identity["reply_text_hash"],
                        error_code=None if result == "sent" else "SEND_INTERRUPTED_BEFORE_RESULT_PERSISTED"))
                continue
            proof = terminal_proof(record, phase_proof={"source": "action_journal", "ok": True,
                "action_phase": "not_attempted"}, input_state="unverified", interruption_reason="process_interrupted")
            save_settlement(record, proof=proof, request={"receipt_kind": "sent_ack"})
        storage.finalize_reply_send_ack(reply_action_id=action_id,
            ack_payload=runner._reply_send_ack_payload(
                send_result="failed", action_phase="not_attempted", reply_text_hash=identity["reply_text_hash"],
                error_code=proof["first_failure"]["error_code"], evidence={"pre_send_read_failure": proof},
                remark="恢复原发送前读取失败回执，不重新操作微信。"))
    return True


def refresh_with_recheck(runner, binding, *, target, action, task_id, first_result, read):
    """The pre-claim C2 refresh shares the same action budget as S0/S1."""
    from apps.wechat_ai_customer_service.adapters.pre_send_read_failure import read_failure_valid
    phase = {"source": "read_only_before_claim", "ok": True, "action_phase": "not_attempted"}
    def fact(result):
        raw = result.get("read_call_failure")
        if not isinstance(raw, dict):
            return None
        value = {**raw, "stage": "pre_send_refresh", "error_code": result.get("error_code") or "MESSAGE_READ_FAILED",
                 "physical_send_triggered": False, "action_phase": "not_attempted",
                 "phase_proof": phase, "input_state": "unverified", "input_progress": "not_started"}
        return value if read_failure_valid(value) else None
    failure = fact(first_result)
    if failure is None:
        return first_result
    context = {"reply_action_id": action["id"], "task_id": task_id,
               "conversation_id": target.conversation_id, "reply_text_hash": action["reply_text_hash"],
               "flow_id": str(storage.load_runtime_control().get("inflight_flow_id") or ""),
               "authorization_revision": target.authorization_revision}
    reason = None
    try:
        allowed, record = reserve(context, failure, target=target.remark_code)
    except Exception as exc:
        allowed = False
        reason = "budget_persistence_failed:" + type(exc).__name__
        record = {"version": 1, "context": context, "first_failure": failure, "budget_state": "persist_failed",
                  "started": False, "boot_id": BOOT_ID, "target": target.remark_code}
    result = first_result
    if allowed:
        try:
            result = read()
        except Exception:
            _stop_after_interruption(runner, action["id"], "extra_read_interrupted")
            raise
        second = fact(result)
        record = complete_attempt(action["id"], failure=second)
        if second is None:
            return result
    elif reason is None:
        record = complete_attempt(action["id"], failure=failure)
    runner.set_run_status("faulted")
    proof = terminal_proof(record, phase_proof=phase, input_state="unverified", interruption_reason=reason)
    saved = save_settlement(record, proof=proof,
        request={"receipt_kind": "task_fail", "lease_fencing_token": runner.api._task_lease_token(task_id)})
    return {**result, "pre_send_read_failure_record": saved}
