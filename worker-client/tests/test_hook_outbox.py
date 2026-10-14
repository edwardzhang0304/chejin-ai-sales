import json
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import uuid

import pytest
from chejin_worker_client import storage
from chejin_worker_client.hook_inbox import NativeInbox, HookReceiveScope
from chejin_worker_client.hook_projection import NativeProjectionStore
from chejin_worker_client.hook_outbox import HookDeliveryOutbox, HookDeliveryScope
from chejin_worker_client.shared_rules import hook_contract_rules as rules
from chejin_worker_client.update_data_snapshot import protected_update_snapshot, assert_protected_update_snapshot, SnapshotMismatch

NOW = '2026-10-09T12:00:00+00:00'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path/'worker_client.sqlite3')
    scope = HookDeliveryScope(str(uuid.uuid4()), 'instance-A', 'wxid_sales_A', str(uuid.uuid4()), str(uuid.uuid4()), 1, 'wxid_customer_A')
    receive = HookReceiveScope(scope.self_wxid, scope.account_epoch, 'process-1', 30001)
    return tmp_path, scope, receive


def project(receive, messages=None):
    messages = messages if messages is not None else [message(receive)]
    raw = json.dumps({'ServerPort':'30001','selfwxid':receive.self_wxid,'sendorrecv':'2',
        'msgnumber':str(len(messages)), 'msglist':messages}, ensure_ascii=False).encode()
    NativeInbox(max_body_bytes=65536,max_inbox_bytes=1048576).persist(raw,receive)
    return NativeProjectionStore(max_projection_bytes=1048576,max_entries=200).project_next(receive)


def message(receive, **fields):
    return {'msgsvrid':'6594133108973694888','msgtype':'1','fromtype':'1','fromid':'wxid_customer_A',
        'toid':receive.self_wxid,'msg':'预算12.5万元\n暂时不要报价🙂','time':'2026-10-09 20:00:00',**fields}


def test_projection_delivery_restart_and_receipt_dont_delete_raw_or_start_business(setup):
    directory, scope, receive = setup
    p = project(receive)
    outbox = HookDeliveryOutbox(max_storage_bytes=1048576)
    event = outbox.enqueue_projection(p['receive_seq'],scope,now=NOW)
    assert event == outbox.enqueue_projection(p['receive_seq'],scope,now=NOW)
    claim = HookDeliveryOutbox(max_storage_bytes=1048576).claim_next(scope,now=NOW,lease_seconds=10)
    assert claim.event_uuid == event and claim.attempt_count == 1
    assert claim.envelope['messages'][0]['content'] == '预算12.5万元\n暂时不要报价🙂'
    assert claim.envelope['receive_seq'] == '1'
    with pytest.raises(ValueError,match='RECEIPT_MISMATCH'):
        outbox.mark_stored(claim, {'status':'stored'},now=NOW)
    assert outbox.mark_stored(claim,rules.receipt(claim.envelope),now=NOW)
    assert outbox.mark_stored(claim,rules.receipt(claim.envelope),now=NOW)
    assert outbox.claim_next(scope,now=NOW,lease_seconds=10) is None
    with storage.db_connection() as db:
        assert db.execute('SELECT count(*) FROM hook_raw_inbox').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM hook_observation_projection').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM hook_delivery_outbox').fetchone()[0] == 1


def test_same_body_different_ids_and_peers_stay_separate_without_uploading_group_or_no_id(setup):
    _, scope, receive = setup
    p = project(receive,[message(receive),message(receive,msgsvrid='1125348102583403829'),
        message(receive,msgsvrid='333',fromid='wxid_customer_B'),
        message(receive,msgsvrid='444',fromtype='2',fromgid='123@chatroom'),
        message(receive,msgsvrid='',fromid=receive.self_wxid,toid=scope.peer_wxid)])
    outbox=HookDeliveryOutbox(max_storage_bytes=1048576)
    outbox.enqueue_projection(p['receive_seq'],scope,now=NOW)
    claim=outbox.claim_next(scope,now=NOW,lease_seconds=10)
    assert [m['native_msg_id'] for m in claim.envelope['messages']] == ['6594133108973694888','1125348102583403829']
    scope_b=replace(scope,binding_id=str(uuid.uuid4()),peer_wxid='wxid_customer_B')
    outbox.enqueue_projection(p['receive_seq'],scope_b,now=NOW)
    assert outbox.claim_next(scope_b,now=NOW,lease_seconds=10).envelope['messages'][0]['native_msg_id'] == '333'
    assert outbox.enqueue_projection(p['receive_seq'],replace(scope,peer_wxid='wxid_unbound'),now=NOW) is None
    with pytest.raises(ValueError,match='ACCOUNT_MISMATCH'):
        outbox.enqueue_projection(p['receive_seq'],replace(scope,account_epoch=str(uuid.uuid4())),now=NOW)


def test_unknown_peer_audio_stays_local(setup):
    _,scope,receive=setup
    audio=b'\x02#!SILK_V3\x00\x01'
    p=project(receive,[message(receive,msgtype='34',fromid='',toid='',msg='',voice_data=audio.hex(),voice_len=str(len(audio)))])
    assert HookDeliveryOutbox(max_storage_bytes=1048576).enqueue_projection(p['receive_seq'],scope,now=NOW) is None


def test_one_lease_retry_after_loss_and_old_attempt_cannot_overwrite_new_attempt(setup):
    _,scope,receive=setup
    p=project(receive); outbox=HookDeliveryOutbox(max_storage_bytes=1048576)
    outbox.enqueue_projection(p['receive_seq'],scope,now=NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims=list(pool.map(lambda _:outbox.claim_next(scope,now=NOW,lease_seconds=10), range(2)))
    first=next(c for c in claims if c)
    assert sum(c is not None for c in claims)==1
    # Account/client rebind must never relabel an old envelope.
    assert outbox.claim_next(replace(scope,client_instance_id='new-instance'),now=NOW,lease_seconds=10) is None
    second=outbox.claim_next(scope,now='2026-10-09T12:00:11+00:00',lease_seconds=10)
    assert second.payload_json==first.payload_json and second.attempt_count==2
    assert not outbox.mark_stored(first,rules.receipt(first.envelope),now=NOW)
    assert not outbox.defer(first,now=NOW,next_attempt_at=NOW,error_code='NETWORK_FAILED')
    assert outbox.mark_stored(second,rules.receipt(second.envelope),now=NOW)


def test_retry_due_and_blocked_record_preserve_evidence(setup):
    _,scope,receive=setup
    p=project(receive); outbox=HookDeliveryOutbox(max_storage_bytes=1048576)
    outbox.enqueue_projection(p['receive_seq'],scope,now=NOW)
    claim=outbox.claim_next(scope,now=NOW,lease_seconds=10)
    assert outbox.defer(claim,now=NOW,next_attempt_at='2026-10-09T12:00:20+00:00',error_code='NETWORK_FAILED')
    assert outbox.claim_next(scope,now=NOW,lease_seconds=10) is None
    second=outbox.claim_next(scope,now='2026-10-09T12:00:20+00:00',lease_seconds=10)
    assert outbox.defer(second,now=NOW,next_attempt_at=NOW,error_code='ACCOUNT_CHANGED',blocked=True)
    assert outbox.claim_next(scope,now='2026-10-09T12:00:30+00:00',lease_seconds=10) is None


def test_capacity_and_update_snapshot_protect_payload_and_ack_state(setup):
    directory,scope,receive=setup
    p=project(receive)
    with pytest.raises(RuntimeError,match='CAPACITY_EXCEEDED'):
        HookDeliveryOutbox(max_storage_bytes=10).enqueue_projection(p['receive_seq'],scope,now=NOW)
    outbox=HookDeliveryOutbox(max_storage_bytes=1048576)
    outbox.enqueue_projection(p['receive_seq'],scope,now=NOW)
    before=protected_update_snapshot(data_dir=directory,digest_key='test-key')
    assert '预算' not in json.dumps(before,ensure_ascii=False)
    with storage.db_connection() as db:
        db.execute("UPDATE hook_delivery_outbox SET status='stored'");db.commit()
    with pytest.raises(SnapshotMismatch):
        assert_protected_update_snapshot(before,data_dir=directory,digest_key='test-key')


@pytest.mark.parametrize('key,value', [('direction',[]),('message_type',{}),('source',[]),
    ('native_msg_id',6594133108973694888),('conversation_type','group'),('to_wxid','wxid_other_sales')])
def test_contract_rejects_invalid_types_roles_and_numeric_message_id(setup,key,value):
    _,scope,receive=setup
    p=project(receive);outbox=HookDeliveryOutbox(max_storage_bytes=1048576)
    outbox.enqueue_projection(p['receive_seq'],scope,now=NOW)
    payload=outbox.claim_next(scope,now=NOW,lease_seconds=10).envelope
    payload['messages'][0][key]=value
    with pytest.raises(ValueError):rules.validate_envelope(payload)


def test_shared_contract_artifact_matches_rules_and_validates_real_projection(setup):
    from pathlib import Path
    import hashlib
    from jsonschema import Draft7Validator,FormatChecker
    _,scope,receive=setup
    p=project(receive);outbox=HookDeliveryOutbox(max_storage_bytes=1048576)
    outbox.enqueue_projection(p['receive_seq'],scope,now=NOW)
    envelope=outbox.claim_next(scope,now=NOW,lease_seconds=10).envelope
    contract=json.loads((Path(__file__).parents[2]/'contracts/hook_contract_v1.json').read_text())
    assert contract['rules_source_sha256']==hashlib.sha256(Path(rules.__file__).read_bytes()).hexdigest()
    assert contract['limits']=={'body_max_bytes':rules.MAX_BYTES,'message_max_items':rules.MAX_ITEMS,'content_max_chars':rules.MAX_CONTENT_CHARS}
    Draft7Validator(contract['schemas']['native_delivery_envelope'],format_checker=FormatChecker()).validate(envelope)


@pytest.mark.parametrize('count', [1, 128, 130])
def test_bulk_enqueue_preserves_ids_and_receipts_across_chunks(setup, count):
    _, scope, receive = setup
    seqs = [project(receive, [message(receive, msgsvrid=str(10000+i))])['receive_seq'] for i in range(count)]
    outbox = HookDeliveryOutbox(max_storage_bytes=1048576)
    events = outbox.enqueue_projections(seqs, scope, now=NOW)
    assert len(set(events)) == count
    assert outbox.enqueue_projections(seqs, scope, now=NOW) == events
    for event in events:
        claim = outbox.claim_next(scope, now=NOW, lease_seconds=10)
        assert claim.event_uuid == event
        assert outbox.mark_stored(claim, rules.receipt(claim.envelope), now=NOW)
    assert outbox.claim_next(scope, now=NOW, lease_seconds=10) is None


def test_bulk_does_not_skip_late_admission_or_different_customer(setup):
    _, scope, receive = setup
    p = project(receive, [message(receive, msgsvrid='111'), message(receive, msgsvrid='222', fromid='wxid_customer_B')])
    outbox = HookDeliveryOutbox(max_storage_bytes=1048576)
    assert outbox.enqueue_projections([p['receive_seq']], scope, now=NOW, allowed_native_ids=set()) == [None]
    event = outbox.enqueue_projections([p['receive_seq']], scope, now=NOW, allowed_native_ids={'111'})[0]
    other = replace(scope, binding_id=str(uuid.uuid4()), peer_wxid='wxid_customer_B')
    other_event = outbox.enqueue_projections([p['receive_seq']], other, now=NOW, allowed_native_ids={'222'})[0]
    assert event and other_event and event != other_event
    assert [m['native_msg_id'] for m in outbox.claim_next(scope, now=NOW, lease_seconds=10).envelope['messages']] == ['111']
    assert [m['native_msg_id'] for m in outbox.claim_next(other, now=NOW, lease_seconds=10).envelope['messages']] == ['222']


def test_bulk_corruption_rolls_back_current_chunk_retry_preserves_previous(setup):
    _, scope, receive = setup
    seqs = [project(receive, [message(receive, msgsvrid=str(10000+i))])['receive_seq'] for i in range(130)]
    with storage.db_connection() as db:
        digest = db.execute('SELECT projection_digest FROM hook_observation_projection WHERE receive_seq=?', (seqs[-1],)).fetchone()[0]
        db.execute('UPDATE hook_observation_projection SET projection_digest=? WHERE receive_seq=?', ('corrupt', seqs[-1])); db.commit()
    outbox = HookDeliveryOutbox(max_storage_bytes=1048576)
    with pytest.raises(RuntimeError, match='PROJECTION_CHANGED'):
        outbox.enqueue_projections(seqs, scope, now=NOW)
    with storage.db_connection() as db:
        assert db.execute('SELECT count(*) FROM hook_delivery_outbox').fetchone()[0] == 128
        db.execute('UPDATE hook_observation_projection SET projection_digest=? WHERE receive_seq=?', (digest, seqs[-1])); db.commit()
    assert len(set(outbox.enqueue_projections(seqs, scope, now=NOW))) == 130
    with storage.db_connection() as db:
        assert db.execute('SELECT count(*) FROM hook_delivery_outbox').fetchone()[0] == 130


def test_bulk_irrelevant_row_cannot_rollback_earlier_envelope(setup):
    _, scope, receive = setup
    first = project(receive)['receive_seq']
    second = project(receive, [message(receive, msgsvrid='111', fromid='wxid_other')])['receive_seq']
    outbox = HookDeliveryOutbox(max_storage_bytes=1048576)
    ids = outbox.enqueue_projections([first, second], scope, now=NOW)
    assert ids[0] and ids[1] is None
    assert outbox.claim_next(scope, now=NOW, lease_seconds=10).event_uuid == ids[0]
