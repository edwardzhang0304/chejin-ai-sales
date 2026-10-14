from datetime import datetime,timedelta,timezone
import json
import pytest
from test_hook_outbox import setup,project,message,NOW
from chejin_worker_client import storage
from chejin_worker_client.hook_outbox import HookDeliveryOutbox
from chejin_worker_client.hook_retention import retire_inbox
from chejin_worker_client.update_data_snapshot import protected_update_snapshot,assert_protected_update_snapshot,SnapshotMismatch


def age_raw():
    old=(datetime.now(timezone.utc)-timedelta(days=40)).isoformat()
    # Preserve the linked original observed_at and projection digest while
    # simulating a genuinely old received package in this synthetic fixture.
    from chejin_worker_client.shared_rules import hook_contract_rules as rules
    with storage.db_connection() as conn:
        conn.execute('UPDATE hook_raw_inbox SET received_at=?',(old,))
        for row in conn.execute('SELECT * FROM hook_observation_projection').fetchall():
            value=rules.loads(row['projection_json']);value['observed_at']=old;body=rules.canonical(value)
            import hashlib
            conn.execute('UPDATE hook_observation_projection SET projection_json=?,projection_digest=? WHERE receive_seq=?',(body,hashlib.sha256(body.encode()).hexdigest(),row['receive_seq']))
        conn.commit()


def test_only_acknowledged_text_retires_and_receipt_and_sequence_survive(setup):
    root,scope,receive=setup;projection=project(receive);age_raw()
    outbox=HookDeliveryOutbox(max_storage_bytes=1048576)
    event=outbox.enqueue_projection(projection['receive_seq'],scope,now=NOW)
    # Pending exact same record is protected.
    assert retire_inbox(root,retention_days=30)['retired_rows']==0
    claim=outbox.claim_next(scope,now=NOW,lease_seconds=30)
    from chejin_worker_client.shared_rules import hook_contract_rules as rules
    receipt=rules.receipt(claim.envelope)
    assert outbox.mark_stored(claim,receipt,now=NOW)
    with storage.db_connection() as conn:conn.execute('PRAGMA foreign_keys=ON')
    result=retire_inbox(root,retention_days=30)
    assert result['retired_rows']==1 and result['released_bytes']>0 and not result['pending_records_deleted']
    with storage.db_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM hook_raw_inbox').fetchone()[0]==0
        archived=json.loads(conn.execute('SELECT record_json FROM hook_retired_deliveries').fetchone()[0])
        assert archived['event_uuid']==claim.event_uuid and json.loads(archived['receipt_json'])==receipt
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='hook_raw_inbox'").fetchone()[0]==1
    snap=protected_update_snapshot(data_dir=root,digest_key='synthetic-key')
    with storage.db_connection() as conn:conn.execute('DELETE FROM hook_retired_deliveries');conn.commit()
    with pytest.raises(SnapshotMismatch):assert_protected_update_snapshot(snap,data_dir=root,digest_key='synthetic-key')


def test_unknown_audio_and_undelivered_other_peer_are_never_discarded(setup):
    root,scope,receive=setup
    project(receive,[message(receive,msgtype='34',msg='<msg><voicemsg length="100"/></msg>')]);age_raw()
    assert retire_inbox(root,retention_days=30)['retired_rows']==0
    with storage.db_connection() as conn:assert conn.execute('SELECT COUNT(*) FROM hook_raw_inbox').fetchone()[0]==1


def test_protected_first_page_never_starves_later_confirmed_rows(setup):
    root,scope,receive=setup
    for i in range(100):project(receive,[message(receive,msgsvrid=str(1000+i),fromid='wxid_unbound')])
    projection=project(receive);age_raw()
    outbox=HookDeliveryOutbox(max_storage_bytes=1048576)
    outbox.enqueue_projection(projection['receive_seq'],scope,now=NOW)
    claim=outbox.claim_next(scope,now=NOW,lease_seconds=30)
    from chejin_worker_client.shared_rules import hook_contract_rules as rules
    assert outbox.mark_stored(claim,rules.receipt(claim.envelope),now=NOW)
    assert retire_inbox(root,retention_days=30,limit=100)['retired_rows']==0
    assert retire_inbox(root,retention_days=30,limit=100)['retired_rows']==1
    # Restart uses the durable maintenance cursor and wraps to reconsider old
    # protected records, without erasing them or depending on process memory.
    assert retire_inbox(root,retention_days=30,limit=100)['retired_rows']==0
    with storage.db_connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hook_raw_inbox').fetchone()[0]==100
        assert db.execute('SELECT receive_seq FROM hook_retired_observations').fetchone()[0]==101
