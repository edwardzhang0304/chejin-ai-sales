"""Synthetic formal chain: an admitted reply drains, unrelated work stays closed.

HTTP backend/Hook and SQLite are real test implementations; model, GUI startup
readiness, download and final OS installation are substitutes. No Windows claim.
"""
import threading
import time
import socket
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import SentAck
from test_hook_formal_runtime import identity_client, lab, clean
import test_hook_formal_runtime as chain
from chejin_worker_client import storage
from chejin_worker_client.api import ApiError, WorkerApiClient
from chejin_worker_client.models import Binding
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.hook_worker_services import HookWorkerServices
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.client_update import UpdateStateStore
from chejin_worker_client.update_coordinator import UpdateCoordinator
from chejin_worker_client import update_coordinator as uc
from test_update_coordinator import FakeApi, _release


class ChainObserved(Exception):
    pass


def check_original_claim_scope(worker_id, batch_id):
    """Tamper only isolated transactions; retain the real first-send receipt."""
    from app.errors import AppError
    from app.models.c3 import MessageBatch, ReplyAction
    from app.models.task import Task
    from app.models.worker import Worker
    from app.services.task_service import claim_task, SYSTEM_TASK_LEASE_ACTOR
    defects = {
        'source': 'C2_REPLY_TASK_FLOW_OWNERSHIP_REQUIRED',
        'conversation': 'C2_REPLY_TASK_FLOW_OWNERSHIP_REQUIRED',
        'flow': 'C2_REPLY_TASK_FLOW_OWNERSHIP_REQUIRED',
        'batch': 'C2_REPLY_TASK_FLOW_OWNERSHIP_REQUIRED',
        'generation': 'C2_REPLY_TASK_FLOW_OWNERSHIP_REQUIRED',
        'predecessor': 'REPLY_PREDECESSOR_NOT_SENT',
        'faulted': 'WORKER_NOT_ACCEPTING_TASKS',
        'active': 'WORKER_NOT_ACCEPTING_TASKS',
        'offline': 'WORKER_OFFLINE',
        'component': 'RPA_COMPONENT_UNAVAILABLE',
    }
    for defect, expected in defects.items():
        with SessionLocal() as db:
            worker = db.get(Worker, worker_id)
            assert worker.run_status == 'paused'
            assert worker.inflight_flow_state['status'] == 'draining'
            action = db.scalar(select(ReplyAction).where(
                ReplyAction.batch_id == batch_id, ReplyAction.segment_index == 2))
            task = db.scalar(select(Task).where(Task.reply_action_id == action.id))
            origin = db.get(Task, worker.inflight_flow_state['flow_id'])
            origin_action = db.get(ReplyAction, origin.reply_action_id)
            source, conversation = 'hook_conversation_flow', action.conversation_id
            if defect == 'source': source = 'other_source'
            elif defect == 'conversation': conversation = 'other-customer'
            elif defect == 'flow': worker.inflight_flow_state = {**worker.inflight_flow_state, 'flow_id': 'missing-flow'}
            elif defect == 'batch':
                other = MessageBatch(conversation_id=action.conversation_id, active=False)
                db.add(other)
                db.flush()
                origin_action.batch_id = other.id
            elif defect == 'generation': origin_action.generation_no += 1
            elif defect == 'predecessor':
                db.delete(db.scalar(select(SentAck).where(SentAck.reply_action_id == origin_action.id)))
            elif defect == 'faulted': worker.run_status = 'faulted'
            elif defect == 'active': worker.inflight_flow_state = {**worker.inflight_flow_state, 'status': 'active'}
            elif defect == 'offline': worker.last_heartbeat_at = None
            elif defect == 'component': worker.rpa_component_status = 'failed'
            db.flush()
            try:
                claim_task(db, task.id, worker_id, None, None, SYSTEM_TASK_LEASE_ACTOR,
                    require_worker_ready=True, claim_source=source, conversation_id=conversation)
            except AppError as rejected:
                assert rejected.code == expected, defect
            else:
                pytest.fail('Unexpected continuation permission: ' + defect)
            db.rollback()


@pytest.mark.parametrize('segments,failure', [(1, None), (2, None), (2, 'receipt'),
    (2, 'finish'), (2, 'read_before'), (2, 'read_after'), (2, 'send_unknown'),
    (2, 'pause_delay'), (2, 'pause_action_lock'), (2, 'pause_after_begin')])
def test_update_finishes_original_reply_through_normal_consumer(identity_client, lab, tmp_path, monkeypatch, segments, failure):
    def drive(runtime, binding, batch_id, **kwargs):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            data = runtime.batch_view(batch_id)
            if (data.get('reply_action') or {}).get('status') == 'queued':
                break
            time.sleep(.05)
        else:
            pytest.fail('No queued production reply')
        history = runtime.sync(binding, show_progress=False)
        first = runtime.reply_once(binding, {**data, 'batch_id': batch_id}, history=history)
        assert first['status'] == 'sent'
        assert bool(runtime.active_flow_id) == (segments == 2)
        with SessionLocal() as db:
            assert db.scalar(select(SentAck).where(SentAck.reply_action_id == first['reply_action_id'])) is not None
        sends = lambda: [r for r in lab[0].requests if r[0] == '/SendTextMsg']
        assert len(sends()) == 1
        from chejin_worker_client.hook_backend import HookHeartbeatIdentity, HookHeartbeatState
        runtime.prepared.account = identity_client.heartbeat(runtime.credentials,
            HookHeartbeatIdentity(runtime.scope.self_wxid, runtime.scope.process_generation,
                runtime.gateway.deployment.revision, runtime.prepared.process.dll_sha256,
                account_epoch=runtime.scope.account_epoch),
            HookHeartbeatState('idle', 'ready', 'logged_in', None, None,
                '{"capabilities":{"reply_sequence_version":1}}'))
        dispatcher = HookBusinessDispatcher(runtime.client, runtime.private_root, None, None,
            automatic_transcription_disabled=lambda: True)
        dispatcher.runtime = runtime
        dispatcher.interval_seconds = .02
        dispatcher._discover_invitations = lambda *a: pytest.fail('New friend discovery during update')
        connection = SimpleNamespace(prepared=runtime.prepared, gateway=runtime.gateway,
            update_safety_snapshot=lambda: {'checking': False, 'process_generation': runtime.scope.process_generation})
        services = HookWorkerServices(connection, runtime.projection, intake_interval_seconds=.02,
            projection_batch_size=1, dispatcher=dispatcher)
        services._credentials = runtime.credentials
        services.startup_window_ready_for = lambda _: True
        errors = []
        faults = []
        fault_observed = threading.Event()
        original_fault = services._fault
        def record_fault(runner, code, **kw):
            faults.append((code, str(kw)))
            result = original_fault(runner, code, **kw)
            fault_observed.set()
            return result
        services._fault = record_fault
        runner = TaskRunner(WorkerApiClient(identity_client.base_url), SimpleNamespace(sidecar_active=lambda: False),
            on_profile=lambda _: None, on_status=lambda _: None, on_step=lambda _: None,
            on_task=lambda _: None, on_result=lambda _: None, on_error=errors.append, hook_services=services)
        runner.binding = Binding(runtime.credentials.worker_id, runtime.credentials.worker_token,
            runtime.credentials.client_instance_id, 'running')
        storage.save_binding(runner.binding)
        runner.api.inflight_flow_id = runtime.active_flow_id
        services._configure_read_retry(runner)
        read_failures = []
        read_episode = None
        if failure in {'read_before', 'read_after'}:
            from chejin_worker_client import hook_transport, hook_read_retry
            original_post = hook_transport.post
            def refused_once(conn, route, payload, limit):
                if route == 'QueryDB' and not read_failures:
                    read_failures.append(route)
                    # Actual local connect refusal supplies proven-not-sent
                    # transport evidence; no fabricated successful receipt.
                    with socket.socket() as refused:
                        refused.bind(('127.0.0.1', 0))
                        return original_post(hook_transport.connection(refused.getsockname()[1], 1), route, payload, limit)
                return original_post(conn, route, payload, limit)
            monkeypatch.setattr(hook_transport, 'post', refused_once)
            if failure == 'read_before':
                with pytest.raises(hook_read_retry.ReadRetryWait):
                    runtime.sync(binding, show_progress=False)
                read_episode = hook_read_retry.pending()
        if failure == 'send_unknown':
            from chejin_worker_client import hook_transport
            original_post = hook_transport.post
            def lose_send_response(conn, route, payload, limit):
                if route == 'SendTextMsg':
                    getresponse = conn.getresponse
                    def response_without_body():
                        # The real loopback peer has received the physical send.
                        # Fail only its body read: production transport must
                        # classify this as unknown, never proven-not-sent.
                        response = getresponse()
                        def lost_body(*args):
                            raise ConnectionResetError('Synthetic send response lost')
                        response.read = lost_body
                        return response
                    conn.getresponse = response_without_body
                return original_post(conn, route, payload, limit)
            monkeypatch.setattr(hook_transport, 'post', lose_send_response)
        revoked = []
        if failure in {'pause_delay', 'pause_action_lock', 'pause_after_begin'}:
            def pause_again():
                assert runner.update_flow_continuation_allowed(runtime.active_flow_id)
                assert runner.set_run_status('paused')
                assert not runner.update_flow_continuation_allowed(runtime.active_flow_id)
                revoked.append(runtime.active_flow_id)
            if failure == 'pause_delay':
                original_wait = runtime.wait
                def delayed_pause(seconds):
                    original_wait(seconds)
                    pause_again()
                runtime.wait = delayed_pause
            elif failure == 'pause_after_begin':
                original_begin = type(runtime.client).native_send_begin
                def paused_begin(client, *args, **kwargs):
                    result = original_begin(client, *args, **kwargs)
                    pause_again()
                    return result
                monkeypatch.setattr(type(runtime.client), 'native_send_begin', paused_begin)
            else:
                # Competing operator intent arrives while the sender is waiting
                # for the physical action lock; check the real post-lock guard.
                from contextlib import contextmanager
                original_lock = runtime.gateway._locked
                @contextmanager
                def paused_lock(process, operation):
                    if operation == 'hook_send_text':
                        pause_again()
                    with original_lock(process, operation) as lease:
                        yield lease
                monkeypatch.setattr(runtime.gateway, '_locked', paused_lock)
        c = UpdateCoordinator(FakeApi(_release()), runner, binding_provider=lambda: runner.binding,
            on_state=lambda _: None, request_normal_exit=lambda: pytest.fail('No OS installation'),
            state_store=UpdateStateStore(tmp_path/'update'), formal_package=True)
        monkeypatch.setattr(uc, 'prepare_release_package', lambda *a, **kw: {'package_manifest': {}})
        installed = threading.Event()
        def record_install_boundary(*args):
            # Match the OS handoff ownership: returning from this substitute
            # must not be mistaken for cancellation and release the gate.
            c._install_started = True
            installed.set()
        c._start_install = record_install_boundary
        requests = []
        refusals = []
        lost = []
        original_call = type(runtime.client).call
        def guarded_call(self, credentials, method, path, *a, **kw):
            requests.append(path)
            assert not path.endswith('/tasks/pull'), path
            try:
                if failure == 'finish' and path.endswith('/inflight-flow/finish') and not lost:
                    lost.append(path)
                    raise ApiError('HOOK_BACKEND_NETWORK_FAILED', 'Synthetic finish request loss', 0)
                result = original_call(self, credentials, method, path, *a, **kw)
                if failure == 'receipt' and path.endswith('/settle') and not lost:
                    lost.append(path)
                    raise ApiError('HOOK_BACKEND_NETWORK_FAILED', 'Synthetic receipt response loss after commit', 0)
                return result
            except Exception as exc:
                refusals.append((path, getattr(exc, 'code', type(exc).__name__)))
                raise
        monkeypatch.setattr(type(runtime.client), 'call', guarded_call)
        assert c.check_for_updates()
        if failure == 'receipt':
            deadline = time.monotonic() + 5
            while runner._backend_confirmed_run_status != 'paused' and time.monotonic() < deadline:
                time.sleep(.02)
            assert runner._backend_confirmed_run_status == 'paused'
            check_original_claim_scope(runtime.credentials.worker_id, batch_id)
        unexpected = []
        def consume():
            try:
                services.business_loop(runner, lambda _: None)
            except BaseException as exc:
                unexpected.append(exc)
        consumer = threading.Thread(target=consume)
        consumer.start()
        try:
            if failure == 'send_unknown':
                assert fault_observed.wait(8), faults
                assert not installed.is_set()
                assert not runner.update_install_safety_snapshot()['safe']
                assert storage.load_runtime_control()['inflight_flow_id']
                assert storage.load_runtime_control()['update_no_new_work']
                assert storage.load_binding().run_status == 'faulted'
                assert len(sends()) == 2  # The uncertain attempt is never repeated.
                with SessionLocal() as db:
                    assert len(list(db.scalars(select(SentAck)))) == 1
                assert list((runtime.private_root/'sends').rglob('*.json'))
                raise ChainObserved()
            if not installed.wait(15):
                pytest.fail(str({'phase': c.state().get('phase'), 'errors': errors, 'faults': faults[:3],
                    'wait': dispatcher.pending_wait_snapshot(),
                    'binding_status': runner.binding.run_status,
                    'backend_flow': runner._backend_inflight_flow_state,
                    'snapshot': runner.update_install_safety_snapshot(),
                    'requests': requests[-10:], 'refusals': refusals[:5]}))
            expected_sends = 1 if revoked else segments
            assert len(sends()) == expected_sends
            if revoked:
                assert len(revoked) == 1
                receipts = list((runtime.private_root/'send-receipts').glob('*.json'))
                assert any('"physical_send_attempted":false' in path.read_text().replace(' ', '')
                           for path in receipts)
            assert not storage.load_runtime_control()['inflight_flow_id']
            assert storage.load_binding().run_status == 'paused'
            assert storage.load_runtime_control()['pause_requested']
            assert storage.load_runtime_control()['update_no_new_work']
            with SessionLocal() as db:
                acks = list(db.scalars(select(SentAck)))
                assert len(acks) == segments
                if revoked:
                    assert sum(a.send_result == 'sent' for a in acks) == 1
                    blocked = [a for a in acks if a.action_phase == 'not_attempted']
                    assert len(blocked) == 1 and blocked[0].send_result == 'failed'
                    assert blocked[0].evidence['native_no_trigger_verified'] is True
            assert bool(lost) == (failure in {'receipt', 'finish'})
            if failure in {'read_before', 'read_after'}:
                assert read_failures == ['QueryDB'] and hook_read_retry.pending() is None
                if read_episode:
                    with storage.db_connection() as db:
                        saved = dict(db.execute('SELECT * FROM hook_read_retry WHERE event_id=?',
                            (read_episode['event_id'],)).fetchone())
                    assert saved['state'] == 'resolved' and saved['failures'] == 1
                    assert saved['intent'] == read_episode['intent']
        finally:
            runner.stop_event.set(); c._stop.set()
            consumer.join(3); c._worker.join(3)
            assert not unexpected, unexpected
            assert not consumer.is_alive() and not c._worker.is_alive()
        raise ChainObserved()
    monkeypatch.setattr(chain, 'drive_reply', drive)
    with pytest.raises(ChainObserved):
        chain.test_coordinator_pulls_formal_invite_binds_history_generates_and_sends(
            identity_client, lab, tmp_path, monkeypatch, segments=segments, peer='wxid_customer_A')
