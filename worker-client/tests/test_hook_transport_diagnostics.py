"""Original voice failure -> process recovery -> bounded desktop ZIP."""
import json
import socket
import threading
import zipfile
import pytest
from test_hook_gateway import lab
from test_hook_history import history_lab
from chejin_worker_client import hook_transport, storage
from chejin_worker_client.hook_gateway import HookLocalError
from chejin_worker_client.hook_active_calls import unresolved


def test_history_failure_precedes_successful_prefix_of_same_call(history_lab,monkeypatch,tmp_path):
    from chejin_worker_client.hook_history_sync import HookHistorySynchronizer
    from chejin_worker_client import diagnostic_bundle
    _,gateway,process,scope,delivery,*_=history_lab
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
    received=[]
    def serve():
        conn,_=listener.accept()
        with conn:received.append(conn.recv(65536))
    thread=threading.Thread(target=serve);thread.start()
    post=hook_transport.post
    def drop_page(conn,route,payload,limit):
        if route=='QueryDB' and 'ORDER BY MSG.localId ASC' in payload['sql']:
            return post(hook_transport.connection(listener.getsockname()[1],1),route,payload,limit)
        return post(conn,route,payload,limit)
    monkeypatch.setattr(hook_transport,'post',drop_page)
    try:
        with pytest.raises(HookLocalError):
            HookHistorySynchronizer(gateway,lambda _:None,lambda:(0,0)).capture(process,scope,delivery,'0')
        thread.join(2);assert received and unresolved(process.generation)
    finally:listener.close()
    with storage.db_connection() as db:
        failure=dict(db.execute('SELECT * FROM hook_transport_events ORDER BY rowid DESC LIMIT 1').fetchone())
        assert json.loads(failure['evidence'])['delivery']=='unknown'
        assert db.execute('SELECT COUNT(*) FROM hook_transport_events WHERE action_id=?',(failure['action_id'],)).fetchone()[0]>2
    monkeypatch.setattr(diagnostic_bundle,'MAX_ROWS',2)
    path=diagnostic_bundle.export_bundle(storage.APP_DIR,tmp_path/'history-failure.zip')
    with zipfile.ZipFile(path) as archive:
        rows=json.loads(archive.read('state/hook_transport_events.json'))['rows']
        assert rows[0]['event_id']==failure['event_id']


@pytest.mark.parametrize('budget,reads',[(2,3),(1000,1001)])
def test_original_unknown_voice_transport_is_kept_after_recovery_reads(lab,monkeypatch,tmp_path,budget,reads):
    from dataclasses import replace
    from test_hook_voice import persist,capture,transcriber
    server,gateway,old,*_=lab
    persist(lab,metadata=True);persist(lab,audio=True)
    audio=capture(lab);assert audio is not None
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
    received=[]
    def serve():
        c,_=listener.accept()
        with c:received.append(c.recv(65536))
    thread=threading.Thread(target=serve);thread.start()
    post=hook_transport.post;connection=hook_transport.connection
    def drop_voice(conn,route,payload,limit):
        if route=='Voice2Text':return post(connection(listener.getsockname()[1],1),route,payload,limit)
        return post(conn,route,payload,limit)
    monkeypatch.setattr(hook_transport,'post',drop_voice)
    try:
        with pytest.raises(HookLocalError):transcriber().transcribe_once(gateway,old,audio,automatic_transcription_disabled=lambda:True)
        thread.join(2);assert received and unresolved(old.generation)
    finally:listener.close()
    with storage.db_connection() as db:
        original=[dict(r) for r in db.execute('SELECT * FROM hook_transport_events')]
        failure=[r for r in original if r['route']=='Voice2Text'][-1]
        original_voice=dict(db.execute('SELECT * FROM hook_voice_attempts').fetchone())
        assert original_voice['state']=='uncertain'
    # OS exit is a fixture; product creates the actual proof, not this test.
    new=replace(old,started_at=old.started_at+1);server.current_process=new
    gateway.generation_exited=lambda generation:generation==old.generation
    monkeypatch.setattr(hook_transport,'post',post)
    for _ in range(reads):assert gateway.login_status(new).logged_in
    from chejin_worker_client import diagnostic_bundle
    monkeypatch.setattr(diagnostic_bundle,'MAX_ROWS',budget)
    path=diagnostic_bundle.export_bundle(storage.APP_DIR,tmp_path/'after-restart.zip')
    with zipfile.ZipFile(path) as z:
        events=json.loads(z.read('state/hook_transport_events.json'))['rows']
        voices=json.loads(z.read('state/hook_voice_attempts.json'))['rows']
        assert any(r['state']=='uncertain' for r in voices)
    with storage.db_connection() as db:
        assert dict(db.execute('SELECT * FROM hook_voice_attempts').fetchone())==original_voice
        assert dict(db.execute('SELECT * FROM hook_transport_events WHERE event_id=?',(failure['event_id'],)).fetchone())==failure
    assert failure['event_id'] in {r['event_id'] for r in events}, {'original_unknown':failure['event_id'],'exported':events}
