"""Production close adapters without Qt; exercise the shared shutdown path."""
import ast
from pathlib import Path
from types import SimpleNamespace
import pytest
from chejin_worker_client import update_shutdown as shutdown


def slot(file, klass, name):
    source = Path(__file__).resolve().parents[1] / 'chejin_worker_client' / file
    cls = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef) and n.name == klass)
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    fn.decorator_list = []
    ns = {"__name__": "chejin_worker_client.ui_test", "__package__": "chejin_worker_client"}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), str(source), 'exec'), ns)
    return ns[name]


@pytest.mark.parametrize('file,klass', [('web_ui.py','WorkerWebWindow'), ('ui.py','WorkerWindow')])
@pytest.mark.parametrize('failure', [None, 'writer', 'maintenance'])
def test_each_ui_closes_once_only_after_all_writers_stop(file, klass, failure, monkeypatch):
    events=[]
    def stop(timeout, **kw):
        events.append(('writers',timeout))
        if failure == 'writer': raise RuntimeError('UPDATE_WRITERS_NOT_STOPPED')
    now=[100.0]
    monkeypatch.setattr(shutdown.time,'monotonic',lambda:now[0])
    ui=SimpleNamespace(runner=SimpleNamespace(hook_services=object(), stop_for_update=stop),
        storage_maintenance=SimpleNamespace(stop=lambda timeout:events.append(('maintenance',timeout)) or failure!='maintenance'),
        update_coordinator=SimpleNamespace(stop=lambda:None, shutdown_deadline=lambda:125,
            record_shutdown_stage=lambda *a,**kw:None, report_normal_exit_result=lambda **kw:events.append(kw)),
        error_signal=SimpleNamespace(emit=lambda msg:events.append('error')))
    def close():
        if shutdown.prepare_window_close(ui):events.append('closed')
    ui.close=close
    slot(file,klass,'_quit_for_update')(ui)
    assert events[0]==('writers',25)
    if failure:
        assert 'closed' not in events
        assert {'stopped':False} in events and {'stopped':True} not in events
    else:
        assert events==[('writers',25),('maintenance',25),{'stopped':True},'closed']
        assert shutdown.prepare_window_close(ui)
        assert len(events)==4  # A second Qt close cannot restart cleanup.


def test_ui_queue_delay_consumes_same_budget(monkeypatch):
    monkeypatch.setattr(shutdown.time,'monotonic',lambda:126)
    events=[]
    ui=SimpleNamespace(_closing_for_update=True,
        runner=SimpleNamespace(stop_for_update=lambda *a,**kw:pytest.fail('expired budget must not restart')),
        update_coordinator=SimpleNamespace(stop=lambda:None,shutdown_deadline=lambda:125,
            record_shutdown_stage=lambda *a,**kw:None,report_normal_exit_result=lambda **kw:events.append(kw)),
        error_signal=SimpleNamespace(emit=lambda _:None))
    assert not shutdown.prepare_window_close(ui)
    assert events==[{'stopped':False}]


def test_remaining_budget_is_not_reset_between_stages(monkeypatch):
    now=[100.0];budgets=[]
    monkeypatch.setattr(shutdown.time,'monotonic',lambda:now[0])
    def writers(budget,**kw):budgets.append(budget);now[0]+=20
    ui=SimpleNamespace(_closing_for_update=True,
        runner=SimpleNamespace(stop_for_update=writers,hook_services=object()),
        storage_maintenance=SimpleNamespace(stop=lambda budget:budgets.append(budget) or True),
        update_coordinator=SimpleNamespace(stop=lambda:None,shutdown_deadline=lambda:125,
            record_shutdown_stage=lambda *a,**kw:None,report_normal_exit_result=lambda **kw:None),
        error_signal=SimpleNamespace(emit=lambda _:pytest.fail('unexpected failure')))
    assert shutdown.prepare_window_close(ui)
    assert budgets==[25,5]


def test_failed_update_can_later_close_normally_without_reusing_expired_deadline(monkeypatch):
    now=[126.0];monkeypatch.setattr(shutdown.time,'monotonic',lambda:now[0])
    events=[]
    ui=SimpleNamespace(_closing_for_update=True,
        runner=SimpleNamespace(hook_services=object(),stop_for_update=lambda timeout,**kw:events.append(timeout)),
        update_coordinator=SimpleNamespace(stop=lambda:None,shutdown_deadline=lambda:125,
            record_shutdown_stage=lambda *a,**kw:None,report_normal_exit_result=lambda **kw:events.append(kw)),
        error_signal=SimpleNamespace(emit=lambda _:None))
    assert not shutdown.prepare_window_close(ui)
    now[0]+=5
    assert shutdown.prepare_window_close(ui)
    assert events==[{'stopped':False},80]


@pytest.mark.parametrize('failure', ['deadline','report','notice','diagnostic'])
def test_failure_in_shutdown_reporting_cannot_escape_close_protection(failure, monkeypatch):
    monkeypatch.setattr(shutdown.time,'monotonic',lambda:100)
    def fail(*a,**kw):raise OSError('synthetic disk failure')
    ui=SimpleNamespace(_closing_for_update=True,
        runner=SimpleNamespace(hook_services=object(),stop_for_update=fail),
        update_coordinator=SimpleNamespace(stop=lambda:None,
            shutdown_deadline=fail if failure=='deadline' else lambda:125,
            record_shutdown_stage=fail if failure=='diagnostic' else lambda *a,**kw:None,
            report_normal_exit_result=fail if failure=='report' else lambda **kw:None),
        error_signal=SimpleNamespace(emit=fail if failure=='notice' else lambda _:None))
    assert shutdown.prepare_window_close(ui) is False
    assert not ui._shutdown_in_progress
    assert not getattr(ui,'_shutdown_complete',False)


def test_unwritten_cancellation_keeps_later_manual_close_blocked(monkeypatch):
    monkeypatch.setattr(shutdown.time,'monotonic',lambda:100)
    calls=[];cancelled=[False]
    def fail(*a,**kw):raise OSError('synthetic state disk failure')
    ui=SimpleNamespace(_closing_for_update=True,
        runner=SimpleNamespace(hook_services=object(),stop_for_update=fail),
        update_coordinator=SimpleNamespace(stop=lambda:None,shutdown_deadline=lambda:125,
            record_shutdown_stage=lambda *a,**kw:None,report_normal_exit_result=fail,
            ensure_shutdown_cancelled=lambda:cancelled[0]),
        error_signal=SimpleNamespace(emit=lambda _:None))
    assert not shutdown.prepare_window_close(ui)
    ui.runner.stop_for_update=lambda *a,**kw:calls.append('stopped')
    assert not shutdown.prepare_window_close(ui) and not calls
    cancelled[0]=True
    assert shutdown.prepare_window_close(ui) and calls==['stopped']
