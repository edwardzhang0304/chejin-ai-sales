"""Synthetic delivery coverage, update/retention and crash boundaries."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import uuid
import pytest
from test_hook_outbox import setup, project, message, NOW
from chejin_worker_client import storage
from chejin_worker_client.hook_outbox import HookDeliveryOutbox
from chejin_worker_client.hook_delivery_lifecycle import terminal_delivery
from chejin_worker_client.hook_retention import retire_inbox
from chejin_worker_client.shared_rules import hook_contract_rules as rules


def dispatch(claim):
    e=claim.envelope
    return {'event_uuid':e['event_uuid'],'raw_source_event_uuid':e['source_event_uuid'],
        'dispatch_status':'facts_stored','can_send':False,
        'message_event_ids':[str(uuid.uuid4()) for _ in e['messages']]}


def prepare(setup, *, different_instance=False, subset=False):
    root,scope,receive=setup
    p=project(receive,[message(receive),message(receive,msgsvrid='1125348102583403829')])
    box=HookDeliveryOutbox(max_storage_bytes=1048576)
    box.enqueue_projection(p['receive_seq'],scope,now=NOW)
    old=box.claim_next(scope,now=NOW,lease_seconds=30)
    current=replace(scope,binding_revision=3,
        client_instance_id='other' if different_instance else scope.client_instance_id)
    box.enqueue_projection(p['receive_seq'],current,now=NOW,
        allowed_native_ids={old.envelope['messages'][0]['native_msg_id']} if subset else None)
    new=box.claim_next(current,now=NOW,lease_seconds=30)
    return root,box,old,new,current


def rows():
    with storage.db_connection() as db:
        return [dict(r) for r in db.execute('SELECT * FROM hook_delivery_outbox ORDER BY binding_revision')]


def blockers():
    return storage.hook_update_business_blockers('process-1')['hook_pending_delivery']


def retire(root):
    return retire_inbox(root,retention_days=1,now=datetime.now(timezone.utc)+timedelta(days=60))


def test_stored_without_dispatch_holds_then_restart_closes_and_archives_original(setup):
    root,box,old,new,current=prepare(setup)
    old_row=rows()[0]
    receipt=rules.receipt(new.envelope)
    box.mark_stored(new,receipt,now=NOW)
    assert blockers()==1
    assert retire(root)['retired_rows']==0
    box=HookDeliveryOutbox(max_storage_bytes=1048576)
    [(resumed,saved)]=box.pending_replacements(current)
    assert saved==receipt and resumed.event_uuid==new.event_uuid
    ack=dispatch(resumed)
    box.mark_stored(resumed,saved,now=NOW,dispatched=ack)
    first=rows()[0]
    assert first['status']=='covered_by_new_authorization'
    assert first['payload_json']==old_row['payload_json']
    assert json.loads(first['receipt_json'])['replacement_dispatch']==ack
    assert blockers()==0 and box.pending_replacements(current)==[]
    assert box.mark_stored(resumed,saved,now=NOW,dispatched=ack)
    assert rows()[0]==first
    assert retire(root)['retired_rows']==1
    with storage.db_connection() as db:
        archived=[json.loads(r[0]) for r in db.execute('SELECT record_json FROM hook_retired_deliveries')]
        assert first in archived and all(terminal_delivery(r) for r in archived)


@pytest.mark.parametrize('variant',['different_instance','subset'])
def test_other_identity_or_incomplete_coverage_cannot_close_old(setup,variant):
    root,box,old,new,current=prepare(setup,**{variant:True})
    box.mark_stored(new,rules.receipt(new.envelope),now=NOW,dispatched=dispatch(new))
    assert rows()[0]['status']=='attempting' and blockers()==1
    assert retire(root)['retired_rows']==0


@pytest.mark.parametrize('field,value',[
    ('event_uuid',str(uuid.uuid4())),('raw_source_event_uuid',str(uuid.uuid4())),
    ('dispatch_status','pending'),('can_send',True),('message_event_ids',[]),
])
def test_wrong_dispatch_receipt_rolls_back_both_deliveries(setup,field,value):
    root,box,old,new,current=prepare(setup)
    before=rows()
    ack=dispatch(new);ack[field]=value
    with pytest.raises(ValueError,match='DISPATCH_MISMATCH'):
        box.mark_stored(new,rules.receipt(new.envelope),now=NOW,dispatched=ack)
    assert rows()==before and blockers()==2
    assert retire(root)['retired_rows']==0


def test_capacity_failure_rolls_back_new_ack_and_old_closure(setup):
    root,box,old,new,current=prepare(setup)
    before=rows()
    with storage.db_connection() as db:
        used=db.execute('SELECT SUM(length(CAST(payload_json AS BLOB))) FROM hook_delivery_outbox').fetchone()[0]
    box.max_storage_bytes=used+len(rules.canonical(rules.receipt(new.envelope)).encode())+10
    with pytest.raises(RuntimeError,match='CAPACITY'):
        box.mark_stored(new,rules.receipt(new.envelope),now=NOW,dispatched=dispatch(new))
    assert rows()==before and blockers()==2


@pytest.mark.parametrize('mutation',['account','process','messages','receipt','dispatch'])
def test_corrupt_coverage_cannot_bypass_update_or_retention_and_retirement_rolls_back(setup,mutation):
    root,box,old,new,current=prepare(setup)
    box.mark_stored(new,rules.receipt(new.envelope),now=NOW,dispatched=dispatch(new))
    proof=json.loads(rows()[0]['receipt_json'])
    if mutation in {'account','process','messages'}:
        e=proof['replacement_envelope']
        if mutation=='account':e['account_epoch']=str(uuid.uuid4())
        elif mutation=='process':e['process_generation']='other-process'
        else:e['messages']=e['messages'][:1]
        proof['replacement_receipt']=rules.receipt(e)
    elif mutation=='receipt':proof['replacement_receipt']['payload_digest']='0'*64
    else:proof['replacement_dispatch']['message_event_ids']=[]
    with storage.db_connection() as db:
        db.execute('UPDATE hook_delivery_outbox SET receipt_json=? WHERE event_uuid=?',(rules.canonical(proof),old.event_uuid));db.commit()
    with pytest.raises(ValueError):blockers()
    before=rows()
    with pytest.raises(ValueError):retire(root)
    assert rows()==before
    with storage.db_connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hook_raw_inbox').fetchone()[0]==1
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='hook_retired_observations'").fetchone()
