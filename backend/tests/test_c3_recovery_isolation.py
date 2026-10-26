"""Scheduler boundary probes; service/database coverage lives in chain tests."""
import logging
import threading

import pytest
from app.services import c3_recovery as recovery
from app.services import human_reminder_service

STAGES = (
    ('handoff_notifications', 'recover_pending_handoff_notifications_once'),
    ('human_reminders', 'recover_reminders_once'),
    ('message_batches', 'recover_due_message_batches_once'),
    ('reply_sends', 'recover_stale_reply_sends_once'),
)


class PollClock:
    """Only controls loop time; never substitutes a recovery service."""
    def __init__(self, rounds=1, after_round=None):
        self.round = 0
        self.rounds = rounds
        self.waits = []
        self.after_round = after_round

    def is_set(self):
        return self.round >= self.rounds

    def wait(self, seconds):
        self.waits.append(seconds)
        self.round += 1
        if self.after_round:
            self.after_round(self.round)
        return self.is_set()


def run_rounds(rounds=1, after_round=None):
    loop = recovery.C3BatchRecoveryLoop(poll_seconds=7)
    clock = PollClock(rounds, after_round)
    loop._stop_event = clock
    loop._run()
    assert clock.waits == [7] * rounds
    return clock


@pytest.mark.parametrize('broken', [None, *range(4)])
def test_all_stages_continue_same_round_and_fault_can_clear(monkeypatch, caplog, broken):
    caplog.set_level(logging.INFO, logger=recovery.__name__)
    clock = PollClock(3)
    called = []
    for index, (stage, name) in enumerate(STAGES):
        def call(index=index, stage=stage):
            called.append((clock.round, stage))
            if index == broken and clock.round < 2:
                raise RuntimeError('token=secret phone=13800000000 private message')
            return {'examined': 1, 'attempted': 1}
        monkeypatch.setattr(recovery, name, call)
        if index == 1:
            monkeypatch.setattr(human_reminder_service, name, call)
    loop = recovery.C3BatchRecoveryLoop(poll_seconds=7)
    loop._stop_event = clock
    loop._run()
    assert called == [(r, stage) for r in range(3) for stage, _ in STAGES]
    assert clock.waits == [7] * 3
    assert 'secret' not in caplog.text and '13800000000' not in caplog.text
    if broken is not None:
        assert caplog.text.count('error_type=RuntimeError') == 2
        assert 'unavailable_before_item_selection' in caplog.text
        assert 'test_c3_recovery_isolation.py:call:' in caplog.text
    assert caplog.text.count('C3 recovery completed') == (12 if broken is None else 10)


@pytest.mark.parametrize('bad', [None, [], {'attempted': 'secret'}, {'phone': 13800000000}, {'attempted': True}])
def test_invalid_return_is_not_success_and_does_not_block_next_stage(monkeypatch, caplog, bad):
    caplog.set_level(logging.INFO, logger=recovery.__name__)
    called = []
    for index, (stage, name) in enumerate(STAGES):
        def call(index=index):
            called.append(index)
            return bad if index == 0 else {'examined': 0}
        monkeypatch.setattr(recovery, name, call)
    run_rounds()
    assert called == [0, 1, 2, 3]
    assert 'invalid result stage=handoff_notifications' in caplog.text
    assert 'completed stage=handoff_notifications' not in caplog.text
    assert 'secret' not in caplog.text and '13800000000' not in caplog.text


def test_log_handler_failure_does_not_stop_other_stages(monkeypatch):
    called = []
    def broken_log(*args, **kwargs):
        raise OSError('sink unavailable')
    monkeypatch.setattr(recovery.logger, 'log', broken_log)
    for index, (_, name) in enumerate(STAGES):
        def call(index=index):
            called.append(index)
            if index == 0:
                raise RuntimeError('service failure too')
            return {'examined': 0}
        monkeypatch.setattr(recovery, name, call)
    run_rounds(2)
    assert called == [0, 1, 2, 3] * 2


def test_base_exception_is_not_swallowed(monkeypatch):
    def interrupt():
        raise KeyboardInterrupt()
    monkeypatch.setattr(recovery, STAGES[0][1], interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_rounds()


def test_disabled_repeat_start_and_stop_during_active_pass(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    called = []
    def first():
        called.append(0)
        entered.set()
        assert release.wait(3)
        return {'examined': 0}
    monkeypatch.setattr(recovery, STAGES[0][1], first)
    for index, (_, name) in enumerate(STAGES[1:], 1):
        def later(index=index):
            called.append(index)
            return {'examined': 0}
        monkeypatch.setattr(recovery, name, later)
    disabled = recovery.C3BatchRecoveryLoop(poll_seconds=0)
    disabled.start()
    assert disabled._thread is None
    loop = recovery.C3BatchRecoveryLoop(poll_seconds=20)
    loop.start()
    assert entered.wait(3)
    thread = loop._thread
    loop.start()
    assert loop._thread is thread
    # Existing stop semantics finish this pass then interrupt the polling wait.
    loop._stop_event.set()
    release.set()
    loop.stop()
    assert not thread.is_alive() and called == [0, 1, 2, 3]
    loop.start()
    loop.stop()
    assert called == [0, 1, 2, 3]  # Existing stopped instance remains stopped.


def test_failure_pass_records_continuation_at_default_warning_threshold(monkeypatch,caplog):
    caplog.set_level(logging.WARNING,logger=recovery.__name__)
    def fail():
        raise RuntimeError('private payload must not leak')
    monkeypatch.setattr(recovery,STAGES[0][1],fail)
    for _,name in STAGES[1:]:
        monkeypatch.setattr(recovery,name,lambda:{'examined':0})
    run_rounds()
    assert "completed=['human_reminders', 'message_batches', 'reply_sends']" in caplog.text
    assert "failed=['handoff_notifications']" in caplog.text
    assert 'private payload' not in caplog.text


def test_database_wide_failure_keeps_poll_interval_and_no_success_summary(monkeypatch,caplog):
    caplog.set_level(logging.WARNING,logger=recovery.__name__)
    calls=[]
    for stage,name in STAGES:
        def fail(stage=stage):
            calls.append(stage)
            raise OSError('database unavailable')
        monkeypatch.setattr(recovery,name,fail)
    run_rounds(2)
    assert calls==[s for s,_ in STAGES]*2
    assert caplog.text.count('completed=[]')==2
    assert caplog.text.count('error_type=OSError')==8
