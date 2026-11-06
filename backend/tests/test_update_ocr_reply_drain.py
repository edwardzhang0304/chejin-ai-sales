"""Reuse the formal OCR reply producer; only WeChat/model I/O is synthetic."""
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
import test_reply_sequence_worker as sequence


def assert_paused_authorization_scope(db, authorize, worker, batch, binding):
    """Negative controls use the real first-segment permit/ACK, rolled back each time."""
    from datetime import timedelta
    from sqlalchemy import select
    from app.models.c3 import ReplyAction, SentAck
    checked = []
    for defect in ('faulted', 'offline', 'component', 'active', 'flow', 'customer',
                   'old_reply', 'post_pause_permit', 'unknown_send', 'no_ack',
                   'no_permit', 'other_worker', 'generation', 'revoked', 'bad_time'):
        savepoint = db.begin_nested()
        try:
            first = db.scalar(select(ReplyAction).where(ReplyAction.batch_id == batch.id,
                ReplyAction.generation_no == batch.generation_no, ReplyAction.segment_index == 1))
            ack = db.scalar(select(SentAck).where(SentAck.reply_action_id == first.id))
            assert ack.send_result == 'sent' and first.sending_claimed_at
            if defect == 'faulted': worker.run_status = 'faulted'
            elif defect == 'offline': worker.last_heartbeat_at = None
            elif defect == 'component': worker.rpa_component_status = 'failed'
            elif defect == 'active': worker.inflight_flow_state = {**worker.inflight_flow_state, 'status': 'active'}
            elif defect == 'flow': worker.inflight_flow_state = {**worker.inflight_flow_state, 'flow_id': 'other-read'}
            elif defect == 'customer': worker.inflight_flow_state = {**worker.inflight_flow_state, 'conversation_id': 'other-customer'}
            elif defect == 'old_reply':
                worker.inflight_flow_state = {**worker.inflight_flow_state,
                    'registered_at': (first.sending_claimed_at + timedelta(seconds=1)).isoformat()}
            elif defect == 'post_pause_permit':
                worker.inflight_flow_state = {**worker.inflight_flow_state,
                    'pause_requested_at': (first.sending_claimed_at - timedelta(seconds=1)).isoformat()}
            elif defect == 'unknown_send': ack.send_result = 'unknown'
            elif defect == 'no_ack': db.delete(ack)
            elif defect == 'no_permit': first.sending_claimed_at = None
            elif defect == 'other_worker': first.claimed_by_worker_id = 'other-worker'
            elif defect == 'generation': batch.generation_no += 1
            elif defect == 'revoked': binding.allow_listening = False
            elif defect == 'bad_time': worker.inflight_flow_state = {**worker.inflight_flow_state, 'registered_at': 'invalid'}
            db.flush()
            assert not authorize(db, worker=worker, batch=batch, binding=binding)['allowed'], defect
            checked.append(defect)
        finally:
            savepoint.rollback()
    assert authorize(db, worker=worker, batch=batch, binding=binding)['allowed']
    return checked


def test_update_after_first_ocr_segment_drains_via_tick(http_api, monkeypatch, async_generation, tmp_path):
    from app.services import c3_service
    authorize = c3_service.message_batch_continuation_authorization
    checked = []
    def observed_authorization(db, *, worker, batch, binding, **kwargs):
        result = authorize(db, worker=worker, batch=batch, binding=binding, **kwargs)
        if worker.run_status == 'paused' and result['allowed'] and not checked:
            checked.extend(assert_paused_authorization_scope(db, authorize, worker, batch, binding))
        return result
    monkeypatch.setattr(c3_service, 'message_batch_continuation_authorization', observed_authorization)
    script = sequence.WORKER
    trigger = "  if mode in {'pause','pause_network'} and len(self.sent_replies)==1:"
    assert script.count(trigger) == 1
    script = script.replace(trigger, """  if mode=='update' and len(self.sent_replies)==1:
   # This fixture performs WeChat I/O in-process, with no OS sidecar.
   bridge.sidecar_active=lambda:False
   assert runner.begin_update_check(lambda:{'update_request_id':'synthetic-ocr-drain'})[1]
   runner.sync_update_stop()
""" + trigger)
    output = " out={'result':result,"
    assert script.count(output) == 1
    script = script.replace(output, """ if mode=='update':
  for _ in range(100):
   if not load_runtime_control()['inflight_flow_id']:break
   runner.tick_once()
   time.sleep(.05)
  assert binding.run_status=='paused'
  assert load_binding().run_status=='paused'
  assert load_runtime_control()['pause_requested']
  assert load_runtime_control()['update_no_new_work']
  assert not runner._can_start_new_flow()
  assert runner.update_install_safety_snapshot()['safe'], runner.update_install_safety_snapshot()
""" + output)
    monkeypatch.setattr(sequence, 'WORKER', script)
    # Retains original assertions: exactly three sends/receipts, original flow
    # finished, no pending ACK/UI lock, real pre-send checks on later segments.
    sequence.test_worker_sends_and_settles_three_segments_without_test_intervention(
        http_api, monkeypatch, async_generation, tmp_path, mode='update')
    assert len(checked) == 15
