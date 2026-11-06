"""Synthetic evidence only: no production records or Windows claims."""
from types import SimpleNamespace
import pytest
from chejin_worker_client import storage
from chejin_worker_client.hook_active_calls import reserve, finish, verify_previous_generations
from test_update_admission import environment
from test_hook_gateway import lab
from test_hook_backend import peer as backend_peer
from test_hook_connection import setup
from chejin_worker_client.hook_gateway import HookLocalError


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path/'worker_client.sqlite3')


def rows():
    with storage.db_connection() as db:
        return [dict(r) for r in db.execute('SELECT * FROM hook_active_calls ORDER BY action_id')]


@pytest.mark.parametrize('kind,expected', [
    ('GetFriendAndChatRoomList',0), ('IsLoginStatus',0), ('QueryDB',0),
    ('GetDBHandleAndPath',0), ('history_capture',0),
    ('SendTextMsg',1), ('send_text',1), ('Voice2Text',1), ('ConfigureMsgReciveFullURL',1),
])
def test_death_proof_only_unblocks_old_reads_without_modifying_evidence(data,kind,expected):
    reserve('old','read',kind);finish('old','read',False)
    before=rows()
    assert storage.hook_update_business_blockers(None)['hook_unresolved_calls']==1
    verify_previous_generations('new',lambda generation:generation=='old')
    assert storage.hook_update_business_blockers(None)['hook_unresolved_calls']==expected
    # An explicitly current process is never exempted by old recovery proof.
    assert storage.hook_update_business_blockers('old')['hook_unresolved_calls']==1
    assert rows()==before


@pytest.mark.parametrize('invalid', ['no_exit','wrong_kind','same_generation','empty_digest'])
def test_missing_or_invalid_proof_cannot_release_read(data,invalid):
    reserve('old','read','GetFriendAndChatRoomList');finish('old','read',False)
    if invalid=='no_exit':
        with pytest.raises(ValueError):verify_previous_generations('new',lambda _:False)
    else:
        verify_previous_generations('new',lambda _:True)
        field,value={'wrong_kind':('death_proof','pid_changed'),
                     'same_generation':('new_generation','old'),
                     'empty_digest':('evidence_sha256','')}[invalid]
        with storage.db_connection() as db:
            db.execute(f'UPDATE hook_process_recovery SET {field}=?',(value,));db.commit()
    assert storage.hook_update_business_blockers(None)['hook_unresolved_calls']==1


def test_failed_login_does_not_lose_existing_death_proof(lab,backend_peer,environment):
    server,gateway,process,*_=lab
    reserve('old','friend-read','GetFriendAndChatRoomList');finish('old','friend-read',False)
    before=rows()
    gateway.generation_exited=lambda old:old=='old'
    server.login_status='invalid-synthetic-status'
    manager,credentials,heartbeat,_=setup(lab,backend_peer)
    try:
        with pytest.raises(HookLocalError,match='HOOK_LOGIN_RESPONSE_INVALID'):
            manager.prepare(credentials,heartbeat)
        assert manager.update_safety_snapshot()=={'checking':False,'process_generation':None}
        runner,control,durable,create=environment
        runner.binding.run_status='faulted'
        runner.hook_services=SimpleNamespace(connection=manager)
        assert create().check_admission()['allowed']
        assert runner.binding.run_status=='faulted' and not control['update_no_new_work']
        # Send acknowledgements and pending writes still block despite proof.
        durable['pending_sent_ack']=1
        assert create().check_admission()['allowed']
        assert runner.update_install_safety_snapshot()['business_reason_code'] == 'UPDATE_WAITING_DURABLE_SETTLEMENT'
        assert [r for r in rows() if r['process_generation']=='old']==before
    finally:manager.close()


def test_rejection_visible_and_old_update_receipt_preserved(environment,monkeypatch):
    runner,control,durable,create=environment
    c=create();published=[];c.on_state=published.append
    c.store.save({'state':'succeeded','target_version':'0.10.28','result_code':'UPDATE_SUCCEEDED'})
    before=c.store.load()
    control['update_no_new_work']=True
    assert not c.check_for_updates()
    assert published[-1]['check_allowed'] is False
    assert '已有更新' in published[-1]['status_text']
    assert c.store.load()==before and not c.api.calls
    assert '已有更新' in c.controls_state(published[-1])['status_text']
    view=c.controls_state(before, c.check_admission(before))
    assert not view['check_allowed'] and '已有更新' in view['status_text']
    control['update_no_new_work']=False
    view=c.controls_state(before, c.check_admission(before))
    assert view['check_allowed'] and view['status_text']==''
    assert c.store.load()==before


def test_atomic_admission_race_publishes_reason(environment):
    runner,control,_,create=environment
    c=create();published=[];c.on_state=published.append
    original=runner.begin_update_check
    def race(begin):
        control['update_no_new_work']=True
        return original(begin)
    runner.begin_update_check=race
    assert not c.check_for_updates()
    assert published[-1]['check_reason_code']=='UPDATE_ALREADY_IN_PROGRESS'
    assert not c.api.calls and c.store.load()['state']=='idle'


def test_old_read_proof_does_not_exempt_new_calls_voice_or_delivery(data):
    reserve('old','read','GetFriendAndChatRoomList');finish('old','read',False)
    verify_previous_generations('new',lambda _:True)
    reserve('new','new-read','GetFriendAndChatRoomList')
    with storage.db_connection() as db:
        db.execute('CREATE TABLE hook_voice_attempts(process_generation TEXT,state TEXT)')
        db.execute("INSERT INTO hook_voice_attempts VALUES ('old','uncertain')")
        db.execute('CREATE TABLE hook_delivery_outbox(status TEXT)')
        db.execute("INSERT INTO hook_delivery_outbox VALUES ('retry_wait')");db.commit()
    assert storage.hook_update_business_blockers(None)=={'hook_unresolved_calls':2,'hook_pending_delivery':1,'hook_pending_voice_results':0}


def test_real_web_model_projects_shared_admission(environment):
    from contextlib import ExitStack
    from unittest.mock import patch
    from test_web_ui_binding_behavior import _headless_web_ui_module, WebUiBindingBehaviorTest
    runner,control,durable,create=environment
    c=create()
    with _headless_web_ui_module() as module, ExitStack() as stack:
        stack.enter_context(patch.object(module,'_log_rows',return_value=[]))
        stack.enter_context(patch.object(module,'latest_incident',return_value=None))
        stack.enter_context(patch.object(module,'lock_summary',return_value={}))
        window=WebUiBindingBehaviorTest()._window(module,runner.binding)
        window.active_page='settings';window.update_coordinator=c;window.update_state=c.state()
        control['update_no_new_work']=True
        window._update_preview=SimpleNamespace(admission=c.check_admission(window.update_state))
        model=window._model()
        assert not model['update']['check_allowed']
        assert '已有更新' in model['update']['status_text']
        control['update_no_new_work']=False
        window._update_preview.admission=c.check_admission(window.update_state)
        # Rendering the completed cache must not query the shared producer again.
        c.check_admission=lambda *_: (_ for _ in ()).throw(AssertionError('UI scanned storage'))
        assert window._model()['update']['check_allowed']
        assert c.api.calls==[]
