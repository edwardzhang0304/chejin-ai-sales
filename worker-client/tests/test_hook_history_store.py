from dataclasses import replace
import json

import pytest

from test_hook_history import history_lab
from test_hook_gateway import lab
from chejin_worker_client import storage
from chejin_worker_client.hook_history import HookHistoryCapture, HookHistoryQuery
from chejin_worker_client.hook_history_store import HookHistoryPageStore
from chejin_worker_client.update_data_access import DataAccess, lock_path
from chejin_worker_client.update_data_snapshot import protected_update_snapshot, assert_protected_update_snapshot, SnapshotMismatch

NOW='2026-10-09T20:00:00+08:00'


def capture(history_lab):
    _,gateway,process,_,delivery,catalog,_=history_lab
    query=HookHistoryQuery(catalog.database('MSG0.db'),'page',delivery,upper_local_id='4',page_size=2)
    return gateway.history_capture(process,catalog,query)


def test_lossless_page_and_cursor_commit_survive_restart_and_repeat(history_lab):
    source=capture(history_lab)
    store=HookHistoryPageStore(max_storage_bytes=65536)
    result=store.append(source,captured_at=NOW)
    assert result['cursor_before']=='0' and result['cursor_after']=='2' and not result['business_ready']
    restarted=HookHistoryPageStore(max_storage_bytes=65536)
    assert restarted.append(source,captured_at='2026-10-09T20:01:00+08:00')==result
    saved=restarted.read(result['event_uuid'])
    assert saved['raw_body']==source.raw_body
    assert saved['captured_at']==NOW
    projection=json.loads(saved['projection_json'])
    assert len(projection['observations'])==2
    assert projection['observations'][0]['native_msg_id']=='18446744073709551614'
    assert projection['observations'][0]['content']=='同秒第一条🙂'
    with storage.db_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM hook_history_pages').fetchone()[0]==1


def test_later_page_records_its_own_scope_and_media_stage(history_lab):
    source=capture(history_lab);store=HookHistoryPageStore(max_storage_bytes=65536)
    first=store.append(source,captured_at=NOW)
    _,gateway,process,_,_,_,_=history_lab
    tail=gateway.history_capture(process,source.catalog,replace(source.query,after_local_id=first['cursor_after']))
    saved=store.append(tail,captured_at=NOW)
    assert saved['history_seq']==2 and saved['cursor_after']=='4'
    observation=json.loads(store.read(saved['event_uuid'])['projection_json'])['observations'][0]
    assert observation['message_type']=='voice' and observation['content'] is None
    assert observation['database_generation']==source.query.database.generation
    terminal=gateway.history_capture(process,source.catalog,replace(source.query,after_local_id='4'))
    end=store.append(terminal,captured_at=NOW)
    assert end['cursor_after']=='4' and not end['business_ready']


def test_capacity_failure_rolls_back_page_and_cursor(history_lab):
    source=capture(history_lab)
    with pytest.raises(RuntimeError,match='CAPACITY'):
        HookHistoryPageStore(max_storage_bytes=1).append(source,captured_at=NOW)
    with storage.db_connection() as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_history_pages'").fetchone():
            assert conn.execute('SELECT COUNT(*) FROM hook_history_pages').fetchone()[0]==0
    result=HookHistoryPageStore(max_storage_bytes=65536).append(source,captured_at=NOW)
    assert result['history_seq']==1 and result['cursor_after']=='2'


@pytest.mark.parametrize('field,value',[('cursor_after','4'),('raw_body',b'{}'),
    ('projection_json','{}'),('context_json','{}'),('upper_local_id','99')])
def test_stored_tampering_cannot_restore_a_cursor(history_lab,field,value):
    store=HookHistoryPageStore(max_storage_bytes=65536);result=store.append(capture(history_lab),captured_at=NOW)
    with storage.db_connection() as conn:
        conn.execute(f'UPDATE hook_history_pages SET {field}=?',(value,));conn.commit()
    with pytest.raises(RuntimeError,match='EVIDENCE_CHANGED'):store.read(result['event_uuid'])


def test_update_snapshot_protects_raw_projection_cursor_and_sequence(history_lab):
    store=HookHistoryPageStore(max_storage_bytes=65536);source=capture(history_lab)
    result=store.append(source,captured_at=NOW)
    expected=protected_update_snapshot(data_dir=storage.APP_DIR,digest_key='private-update-token')
    exported=json.dumps(expected)
    assert '同秒第一条' not in exported and 'wxid_customer_A' not in exported
    assert expected['hook_history_v1']['row_count']==1
    assert_protected_update_snapshot(expected,data_dir=storage.APP_DIR,digest_key='private-update-token')
    with storage.db_connection() as conn:
        conn.execute("UPDATE sqlite_sequence SET seq=100 WHERE name='hook_history_pages'");conn.commit()
    with pytest.raises(SnapshotMismatch):
        assert_protected_update_snapshot(expected,data_dir=storage.APP_DIR,digest_key='private-update-token')
    assert store.read(result['event_uuid'])['cursor_after']=='2'


def test_update_write_gate_blocks_page_without_cursor_commit(history_lab):
    source=capture(history_lab)
    with DataAccess(lock_path(storage.APP_DIR),True):
        with pytest.raises(RuntimeError):HookHistoryPageStore(max_storage_bytes=65536).append(source,captured_at=NOW)
    result=HookHistoryPageStore(max_storage_bytes=65536).append(source,captured_at=NOW)
    assert result['history_seq']==1


def test_changed_provider_response_is_new_evidence_not_overwritten(history_lab):
    source=capture(history_lab);store=HookHistoryPageStore(max_storage_bytes=65536)
    first=store.append(source,captured_at=NOW)
    payload=source.payload;payload['data'][0]['StrContent']='更正后的本地内容'
    changed=HookHistoryCapture(source.catalog,source.query,json.dumps(payload,ensure_ascii=False).encode('utf-8'))
    second=store.append(changed,captured_at=NOW)
    assert second['event_uuid']!=first['event_uuid']
    assert store.read(first['event_uuid'])['raw_body']==source.raw_body
    with storage.db_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM hook_history_pages').fetchone()[0]==2
