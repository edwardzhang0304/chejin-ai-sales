from dataclasses import replace
import uuid
import pytest
from chejin_worker_client import storage
from chejin_worker_client.hook_active_calls import reserve,finish,unresolved
from chejin_worker_client.hook_gateway import HookLocalError
from chejin_worker_client.update_data_snapshot import protected_update_snapshot,assert_protected_update_snapshot,SnapshotMismatch
from test_hook_gateway import lab


def test_unknown_send_isolates_all_calls_but_new_process_keeps_old_evidence(lab):
    server,gateway,process,scope,*_=lab
    action=str(uuid.uuid4());reserve(process.generation,action,'send_text');finish(process.generation,action,False)
    assert unresolved(process.generation)
    before=len(server.requests)
    with pytest.raises(HookLocalError,match='ACTIVE_CALL_QUARANTINED'):gateway.login_status(process)
    assert len(server.requests)==before
    new=replace(process,started_at=process.started_at+1)
    assert not unresolved(new.generation) and unresolved(process.generation)
    with pytest.raises(ValueError,match='PREVIOUS_ATTEMPT'):reserve(process.generation,action,'send_text')


def test_update_snapshot_protects_unresolved_call(lab):
    _,_,process,*_=lab
    reserve(process.generation,str(uuid.uuid4()),'send_text')
    saved=protected_update_snapshot(data_dir=storage.APP_DIR,digest_key='synthetic-only')
    assert saved.get('hook_active_calls_v1')
    with storage.db_connection() as conn:conn.execute('DELETE FROM hook_active_calls');conn.commit()
    with pytest.raises(SnapshotMismatch):assert_protected_update_snapshot(saved,data_dir=storage.APP_DIR,digest_key='synthetic-only')


def test_read_timeout_keeps_process_isolated_without_another_native_call(lab):
    import time
    server,gateway,process,*_=lab
    gateway.timeout_seconds=.01
    server.on_request=lambda route:time.sleep(.05)
    with pytest.raises(HookLocalError,match='LOCAL_TRANSPORT_FAILED'):gateway.login_status(process)
    assert unresolved(process.generation)
    count=len(server.requests)
    with pytest.raises(HookLocalError,match='ACTIVE_CALL_QUARANTINED'):gateway.login_status(process)
    assert len(server.requests)==count
