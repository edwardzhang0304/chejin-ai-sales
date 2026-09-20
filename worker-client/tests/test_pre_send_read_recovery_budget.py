"""Real SQLite failure/restart boundaries of the shared per-action budget."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from chejin_worker_client import storage, pre_send_read_recovery as recovery


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "APP_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "worker.sqlite3")
    return {"reply_action_id": "action-a", "task_id": "task-a", "conversation_id": "customer-a",
            "flow_id": "flow-a", "authorization_revision": "original-auth", "reply_text_hash": "a" * 64}


def failure(stage="before_input", identity="first"):
    return {"stage": stage, "operation": "capture", "call_status": "failed", "attempt_id": identity,
            "error_code": "SEND_BASELINE_UNAVAILABLE", "failure_reason": "capture raised",
            "physical_send_triggered": False, "action_phase": "not_attempted",
            "phase_proof": {"source": "action_journal", "ok": True, "action_phase": "not_attempted"},
            "input_state": "unverified", "frame_id": None, "no_frame_reason": "capture raised"}


def test_one_budget_across_threads_stages_and_restart(state, monkeypatch):
    # Initialize the real database before the two callers compete on its row.
    storage.load_c2_state("init")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: recovery.reserve(state, failure()), range(2)))
    assert sum(allowed for allowed, _ in results) == 1
    allowed, record = recovery.reserve(state, failure("before_trigger", "later-stage"))
    assert not allowed and record["first_failure"]["stage"] == "before_input"
    monkeypatch.setattr(recovery, "BOOT_ID", "next-process")
    assert len(recovery.pending_records()) == 1
    assert recovery.reserve(state, failure())[0] is False


def test_budget_persistence_failure_never_authorizes_retry(state, monkeypatch):
    original = storage.db_connection
    from contextlib import contextmanager
    @contextmanager
    def failed():
        with original() as conn:
            class CommitFailure:
                def execute(self, *args): return conn.execute(*args)
                def commit(self): raise OSError("disk write failure")
            yield CommitFailure()
    monkeypatch.setattr(storage, "db_connection", failed)
    with pytest.raises(OSError, match="disk write failure"):
        recovery.reserve(state, failure())


@pytest.mark.parametrize("raw", ["broken-json", "{}", "[]"])
def test_corrupt_budget_does_not_grant_another_attempt(state, raw):
    with storage.db_connection() as conn:
        conn.execute("INSERT INTO c2_runtime_state(key,value,updated_at) VALUES(?,?,?)",
                     (recovery.PREFIX + state["reply_action_id"], raw, storage.utc_now_iso()))
        conn.commit()
    with pytest.raises((ValueError, json.JSONDecodeError)):
        recovery.reserve(state, failure())


def test_two_no_image_failures_keep_actual_attempts(state):
    allowed, first = recovery.reserve(state, failure())
    assert allowed
    record = recovery.complete_attempt(state["reply_action_id"], failure=failure(identity="second"))
    proof = recovery.terminal_proof(record, phase_proof=failure()["phase_proof"], input_state="unverified")
    assert proof["outcome"] == "exhausted"
    assert proof["first_failure"]["frame_id"] is proof["recheck"]["failure"]["frame_id"] is None
    recovery.save_settlement(record, proof=proof, request={"receipt_kind": "sent_ack"})
    assert recovery.pending_records()[0]["proof"] == proof
    recovery.mark_settled(state["reply_action_id"])
    assert recovery.pending_records() == []
    assert recovery.reserve(state, failure())[0] is False


@pytest.mark.parametrize('case', ['confirmed', 'unconfirmed', 'foreign_text', 'unknown', 'wrong_target', 'clear_error'])
def test_recheck_customer_receipt_keeps_only_unresolved_input_gate(state, case):
    from test_send_interruption import receipt
    payload=receipt();payload.pop('target')
    payload['reply_text_hash']=state['reply_text_hash']
    cleanup=payload['evidence']['guard']['visual']['draft_clear']
    cleanup.update(cleared=False,clear_attempted=True,method='select_all_backspace',
                   reason='confirmed_program_draft_clear_requested')
    recovery.reserve(state,failure('before_trigger'),target='OTHER' if case=='wrong_target' else 'CJTEST01')
    recovery._record_send_start_if_present(state['reply_action_id'])
    recovery.complete_attempt(state['reply_action_id'])
    if case=='foreign_text':payload['reply_text_hash']='b'*64
    if case=='unknown':payload.update(send_result='unknown',action_phase='trigger_attempted')
    if case=='clear_error':cleanup['ok']=False
    storage.save_reply_send_intent(reply_action_id=state['reply_action_id'],task_id=state['task_id'],
        send_token='original',reply_text_hash=state['reply_text_hash'])
    storage.finalize_reply_send_ack(reply_action_id=state['reply_action_id'],ack_payload=payload)
    if case!='unconfirmed':storage.mark_reply_send_ack_confirmed(state['reply_action_id'])
    recovery.mark_settled(state['reply_action_id'])
    assert bool(recovery.input_pending_records()) is (case!='confirmed')
    persisted=storage.load_c2_state(recovery.PREFIX+state['reply_action_id'])
    if case=='confirmed':
        assert persisted['input_safety']['status']=='replacement_ready'
        assert persisted['send_in_progress'] is False
