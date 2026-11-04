"""Real Qt event-loop tests; synthetic storage, no Windows/WeChat claims."""
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
from chejin_worker_client.update_controls_preview import UpdateControlsPreview


@pytest.fixture
def app():
    return QCoreApplication.instance() or QCoreApplication([])


def spin(seconds=.06):
    loop=QEventLoop();QTimer.singleShot(int(seconds*1000),loop.quit);loop.exec()


def until(predicate, timeout=3):
    end=time.monotonic()+timeout
    while not predicate() and time.monotonic()<end:spin(.01)
    assert predicate()


def test_single_flight_responsive_and_transient_busy_recovers(app):
    release=threading.Event();calls=[];state={'allowed':False,'reason_text':'正在读取'}
    def check(_):
        calls.append(threading.get_ident());release.wait(2);return dict(state)
    p=UpdateControlsPreview(check);p._timer.setInterval(10)
    p.set_context(True, {'state':'idle'})
    pulses=[];timer=QTimer();timer.setInterval(5);timer.timeout.connect(lambda:pulses.append(1));timer.start()
    for _ in range(10):p.refresh();spin(.01)
    assert len(calls)==1 and len(pulses)>5 and p.admission is None
    release.set();until(lambda:p.admission is not None)
    assert not p.admission['allowed']
    state['allowed']=True
    until(lambda:p.admission['allowed'])  # timer alone, no heartbeat
    assert all(t!=threading.get_ident() for t in calls)
    timer.stop();p.stop()


@pytest.mark.parametrize('transition',['leave','clock','update','stop'])
def test_late_allowed_cannot_overwrite_new_context(app,transition):
    release=threading.Event()
    def check(_):release.wait(2);return {'allowed':True}
    p=UpdateControlsPreview(check);p.set_context(True,{'state':'idle'})
    if transition=='leave':p.set_context(False,{'state':'idle'})
    elif transition=='clock':p.set_context(True,{'state':'idle'},clock_busy=True)
    elif transition=='update':p.set_context(True,{'state':'downloading'})
    else:p.stop()
    release.set();until(lambda:not p._busy)
    assert p.admission is None
    if transition!='update':assert not p._timer.isActive()
    p.stop()


def test_failed_preview_stays_disabled_and_render_never_checks(app):
    def fail(_):raise OSError('synthetic unreadable storage')
    p=UpdateControlsPreview(fail);p.set_context(True,{'state':'idle'})
    until(lambda:p.admission is not None)
    assert not p.admission['allowed'] and p.admission['reason_text']
    p.stop()


def test_actual_storage_scan_runs_outside_qt_and_model_uses_cache(app,tmp_path,monkeypatch):
    import json
    from chejin_worker_client import storage,task_runner as runner_module
    from chejin_worker_client.update_coordinator import UpdateCoordinator
    from chejin_worker_client.client_update import UpdateStateStore
    from test_update_safety_boundary import _runner
    from test_update_coordinator import FakeApi,_release
    monkeypatch.setattr(storage,'APP_DIR',tmp_path);monkeypatch.setattr(storage,'DB_FILE',tmp_path/'worker.sqlite3')
    runner=_runner();runner.hook_services=SimpleNamespace(connection=SimpleNamespace(update_safety_snapshot=lambda:{'checking':False,'process_generation':None}))
    monkeypatch.setattr(runner_module,'lock_summary',lambda:{'locked':False})
    c=UpdateCoordinator(FakeApi(_release(available=False)),runner,binding_provider=lambda:runner.binding,on_state=lambda _:None,request_normal_exit=lambda:None,state_store=UpdateStateStore(tmp_path/'update'),formal_package=True)
    with storage.db_connection() as db:
        payload=json.dumps({'synthetic_history':'x'*32768})
        db.executemany("INSERT INTO c2_ingest_outbox(outbox_id,conversation_id,authorization_revision,read_run_id,payload_json,status,created_at,updated_at,operation) VALUES (?,?,?,?,?,'confirmed','2026-11-04','2026-11-04','ingest')",[(str(i),'synthetic','r',str(i),payload) for i in range(1000)])
        db.commit()
    original=runner_module.update_install_business_blockers;threads=[]
    def delayed_storage():
        threads.append(threading.get_ident());time.sleep(.25);return original()
    monkeypatch.setattr(runner_module,'update_install_business_blockers',delayed_storage)
    p=UpdateControlsPreview(c.check_admission);p.set_context(True,c.state())
    pulses=[];timer=QTimer();timer.setInterval(10);timer.timeout.connect(lambda:pulses.append(time.monotonic()));timer.start()
    for _ in range(20):
        p.refresh()
        assert not c.controls_state(c.state(),p.admission)['check_allowed']
        spin(.005)
    until(lambda:p.admission is not None)
    assert p.admission['allowed'],p.admission
    assert len(threads)==1 and threads[0]!=threading.get_ident()
    assert len(pulses)>=15 and max(b-a for a,b in zip(pulses,pulses[1:]))<.20
    for _ in range(20):assert c.controls_state(c.state(),p.admission)['check_allowed']
    assert len(threads)==1
    # An allowed old preview can never override an ongoing update.
    assert not c.controls_state({'state':'downloading'},p.admission)['check_allowed']
    timer.stop();p.stop()


def test_web_page_update_clock_and_exit_wire_context(app):
    from test_web_ui_binding_behavior import _headless_web_ui_module,WebUiBindingBehaviorTest
    release=threading.Event()
    def check(_):release.wait(2);return {'allowed':True}
    with _headless_web_ui_module() as module:
        w=WebUiBindingBehaviorTest()._window(module,None)
        w.clock_sync=SimpleNamespace(snapshot=lambda:{'busy':False})
        w.bridge=SimpleNamespace(emit_state=lambda:None)
        p=UpdateControlsPreview(check);w._update_preview=p
        w.change_screen('settings');assert p._busy and p._timer.isActive()
        w.go_back();assert p.admission is None and not p._timer.isActive()
        w.change_screen('settings')
        w.on_update_state({'state':'downloading'})
        release.set();until(lambda:not p._busy)
        assert p.admission is None  # old idle callback rejected after state change
        w.clock_sync.snapshot=lambda:{'busy':True}
        w._publish();assert not p._timer.isActive()
        w.clock_sync.snapshot=lambda:{'busy':False}
        w._shutdown_in_progress=True
        w._publish();assert not p._timer.isActive()
        p.stop()


@pytest.mark.parametrize('can_close',[False,True])
def test_web_close_invalidates_preview_before_waiting_for_writers(app,monkeypatch,can_close):
    from test_web_ui_binding_behavior import _headless_web_ui_module,WebUiBindingBehaviorTest
    from chejin_worker_client import update_shutdown
    with _headless_web_ui_module() as module:
        w=WebUiBindingBehaviorTest()._window(module,None)
        w.clock_sync=SimpleNamespace(snapshot=lambda:{'busy':False})
        w.bridge=SimpleNamespace(emit_state=lambda:None)
        p=UpdateControlsPreview(lambda _:{'allowed':True});w._update_preview=p
        w.change_screen('settings');until(lambda:p.admission is not None)
        old_epoch=p._epoch
        def prepare(_):
            assert not p._timer.isActive() and p.admission is None
            assert p._epoch>old_epoch
            return can_close
        monkeypatch.setattr(update_shutdown,'prepare_window_close',prepare)
        closed=[];ignored=[]
        monkeypatch.setattr(module.QMainWindow,'closeEvent',lambda *_:closed.append(True),raising=False)
        w.closeEvent(SimpleNamespace(ignore=lambda:ignored.append(True)))
        assert bool(closed)==can_close and bool(ignored)!=can_close
        assert p._stopped==can_close
        if not can_close:assert p._timer.isActive()
        p.stop();until(lambda:not p._busy)
