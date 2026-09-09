"""Reuse original conflict regression; add pending finish through real entry."""
import json, time
from unittest.mock import patch
import pytest
import test_task_runner as fixtures
import chejin_worker_client.task_runner as worker
from chejin_worker_client.storage import load_runtime_control, load_c2_state, load_binding, save_binding, read_logs

@pytest.mark.parametrize('pending_finish',[False,True])
def test_terminal_conflict_keeps_original_fault_handling(tmp_path,pending_finish):
    case=fixtures.TaskRunnerTest();case.setUp();seen=[]
    original_start=worker.TaskRunner.start
    flow='read-restart-unrepairable-voice-conflict'
    def start(runner,binding):
        seen.append(runner)
        # The original unit fixture only sets in-memory binding. Persist this
        # case's own initial binding, avoiding unrelated prior case file state.
        save_binding(binding)
        if pending_finish:
            with pytest.raises(RuntimeError,match='PENDING'):
                runner._finish_inflight_flow(binding,flow,terminal_kind='read_confirmed',conversation_id='conv-restart-unrepairable-voice-conflict')
            assert runner._flow_finish_stage=='dependencies'
        result=original_start(runner,binding)
        # Original test waits only three heartbeats. Observe past the real
        # finish retry delay so delayed execution is not mistaken for failure.
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            time.sleep(.02)
        return result
    try:
        with patch.object(worker.TaskRunner,'start',new=start):
            # All original assertions, including fault persistence and heartbeats, remain.
            case.test_restart_unrepairable_terminal_conflict_faults_but_heartbeats()
    finally:
        if seen:
            runner=seen[0]
            evidence={'pending_finish':pending_finish,'run_status':runner.binding.run_status,
              'saved_status':load_binding().run_status,'runtime':load_runtime_control(),
              'fault':load_c2_state(runner._restart_recovery_fault_key(flow)),
              'receipt':load_c2_state(runner._inflight_finish_receipt_key(flow)),
              'heartbeats':len(runner.api.heartbeat_payloads),'api_events':runner.api.events,
              'finish_events':runner.api.inflight_flow_events,'logs':read_logs(limit=50)}
            (tmp_path/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2,default=str))
        case.tearDown()


@pytest.mark.parametrize('finish_response_lost', [False, True])
def test_persisted_conflict_uses_original_terminal_settlement_after_network_recovers(finish_response_lost):
    # Establish the original conflict (including its original assertions), then
    # restart the runner against that same SQLite and persistent backend stub.
    # This local boundary test never changes ledger facts or clears a Flow itself.
    case=fixtures.TaskRunnerTest();case.setUp();seen=[]
    original_start=worker.TaskRunner.start
    def observe(runner,binding):
        seen.append(runner);save_binding(binding)
        return original_start(runner,binding)
    resumed=None
    try:
        with patch.object(worker.TaskRunner,'start',new=observe):
            case.test_restart_unrepairable_terminal_conflict_faults_but_heartbeats()
        previous=seen[0];api=previous.api
        api.run_status_error=None;api.finish_inflight_error=None;api.heartbeat_run_status='faulted'
        original_finish=api.finish_inflight_flow;lost=[]
        def finish(binding,**kwargs):
            result=original_finish(binding,**kwargs)
            if finish_response_lost and not lost:
                lost.append(True);raise ConnectionError('accepted technical terminal response lost')
            return result
        api.finish_inflight_flow=finish
        resumed,_=case.make_runner(api,previous.bridge)
        resumed.poll_interval_seconds=.02
        from dataclasses import replace
        with patch.object(worker,'CONFIG',replace(worker.CONFIG,c2_enabled=False)):
            resumed.start(load_binding())
            deadline=time.monotonic()+3
            while load_runtime_control()['inflight_flow_id'] and time.monotonic()<deadline:time.sleep(.02)
        assert not load_runtime_control()['inflight_flow_id'] and not api.inflight_flow_state
        assert not resumed._pending_flow_finish and resumed._restart_recovery_flow_id is None
        assert load_runtime_control()['pause_requested'] and load_binding().run_status=='faulted'
        assert resumed.binding.run_status=='faulted' and not resumed._can_start_new_flow()
        assert not api.message_payloads and 'pull' not in api.events
        assert not previous.bridge.locate_chats and not previous.bridge.sent_replies
        assert bool(lost)==finish_response_lost
    finally:
        if resumed:resumed.stop_for_update(timeout_seconds=5)
        case.tearDown()
