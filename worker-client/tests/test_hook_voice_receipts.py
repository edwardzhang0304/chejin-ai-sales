"""Original result delivery and bounded, restartable reconciliation."""
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from chejin_worker_client import hook_voice_receipts as receipts
from chejin_worker_client.hook_voice import _ensure_schema
from chejin_worker_client.shared_rules import hook_contract_rules as rules


def database(path=':memory:'):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;_ensure_schema(db)
    return db


def attempt(db,mid='123',state='transcribed',peer='wxid_peer'):
    db.execute('INSERT INTO hook_voice_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ('wxid_owner','epoch',mid,peer,'old-generation','a'*64,20,state,'start','end','b'*64,'original',None))
    db.commit()
    return dict(db.execute('SELECT * FROM hook_voice_attempts WHERE native_msg_id=?',(mid,)).fetchone())


def reconcile(db,root):
    rows=receipts.reconcile_pending(db,root);db.commit();return rows


def write_ack(root,row):
    p=receipts.receipt_path(root,row);p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(rules.canonical({'attempt_digest':rules.digest(row),'receipt':{'voice_receipt_id':'receipt','native_msg_id':row['native_msg_id'],'state':row['state']}}))


@pytest.mark.parametrize('state',['transcribed','failed'])
def test_pending_original_result_only_settles_matching_receipt(tmp_path,state):
    db=database();row=attempt(db,state=state)
    binding={'binding_id':'binding','binding_revision':1,'peer_wxid':'wxid_peer'}
    scope=SimpleNamespace(self_wxid='wxid_owner',account_epoch='epoch')
    calls=[]
    def unavailable(*args):calls.append(args[-1]);raise OSError('offline')
    with pytest.raises(OSError):receipts.publish(SimpleNamespace(voice_result=unavailable),None,None,binding,row,tmp_path)
    assert receipts.pending_count(db,tmp_path)==1
    pending=reconcile(db,tmp_path);assert pending==[row]
    jobs=receipts.retained_job_map(pending,tmp_path,scope)
    assert jobs.get(('binding',2,'wxid_peer'),[])==[]
    assert receipts.retained_job_map(pending,tmp_path,SimpleNamespace(self_wxid='wxid_other',account_epoch='epoch'))=={}
    assert jobs[('binding',1,'wxid_peer')]==[{'native_msg_id':'123'}]
    def confirmed(*args):calls.append(args[-1]);return {'voice_receipt_id':'receipt','native_msg_id':'123','state':state}
    client=SimpleNamespace(voice_result=confirmed)
    receipts.publish(client,None,None,binding,row,tmp_path)
    receipts.publish(client,None,None,binding,row,tmp_path)
    assert receipts.pending_count(db,tmp_path)==1
    assert reconcile(db,tmp_path)==[]
    assert calls==[row,row] and receipts.pending_count(db,tmp_path)==0
    path=receipts.receipt_path(tmp_path,row)
    path.write_text(rules.canonical({'attempt_digest':'f'*64,'receipt':{}}))
    with pytest.raises(ValueError,match='RECEIPT_CONFLICT'):receipts.publish(client,None,None,binding,row,tmp_path)
    proof=rules.loads(db.execute('SELECT receipt_json FROM hook_voice_result_delivery').fetchone()[0].encode())
    assert proof['attempt_digest']==rules.digest(row)
    assert dict(db.execute('SELECT * FROM hook_voice_attempts').fetchone())==row
    db.close()


@pytest.mark.parametrize('total',[1000,5000])
def test_history_reconciliation_bounded_and_normal_dispatch_does_not_reread(tmp_path,monkeypatch,total):
    from chejin_worker_client import storage
    from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
    db=database();scope=SimpleNamespace(self_wxid='wxid_owner',account_epoch='epoch')
    bindings=[{'binding_id':str(i),'binding_revision':1,'peer_wxid':'wxid_peer'+str(i)} for i in range(20)]
    for n in range(total):write_ack(tmp_path,attempt(db,str(n+1),peer=bindings[n%20]['peer_wxid']))
    counts=[];original=receipts._read_receipt
    def read(root,row):counts.append(row['native_msg_id']);return original(root,row)
    monkeypatch.setattr(receipts,'_read_receipt',read)
    @contextmanager
    def connection():yield db
    monkeypatch.setattr(storage,'db_connection',connection)
    dispatcher=HookBusinessDispatcher(None,tmp_path,None,None,automatic_transcription_disabled=lambda:True)
    dispatcher.runtime=SimpleNamespace(scope=scope)
    assert receipts.pending_count(db,tmp_path)==total
    rounds=0
    while receipts.pending_count(db,tmp_path):
        before=len(counts)
        assert dispatcher._recover_voice_attempts(bindings) is False
        assert 0<len(counts)-before<=receipts.RECONCILE_LIMIT
        rounds+=1;assert rounds<=total
    assert len(counts)==total
    for _ in range(5):
        assert dispatcher._recover_voice_attempts(bindings) is False
        assert receipts.pending_count(db,tmp_path)==0
    assert len(counts)==total
    assert db.execute('SELECT COUNT(*) FROM hook_voice_attempts').fetchone()[0]==total
    db.close()


def test_unchecked_and_interrupted_ack_recover_after_restart(tmp_path):
    path=tmp_path/'original.db';proofs=tmp_path/'proofs';db=database(path)
    rows=[attempt(db,str(n+1)) for n in range(40)]
    for row in rows[:-1]:write_ack(proofs,row)
    reconcile(db,proofs)
    remaining=receipts.pending_count(db,proofs);assert 1<remaining<40
    db.close();db=database(path)
    assert receipts.pending_count(db,proofs)==remaining
    while db.execute("SELECT COUNT(*) FROM hook_voice_result_delivery WHERE status='pending'").fetchone()[0]==0:reconcile(db,proofs)
    write_ack(proofs,rows[-1])
    receipts.reconcile_pending(db,proofs);db.rollback();db.close()
    db=database(path);assert receipts.pending_count(db,proofs)>0
    for _ in range(10):reconcile(db,proofs)
    assert receipts.pending_count(db,proofs)==0
    assert receipts.pending_count(db,tmp_path/'other-root')==40
    db.execute("UPDATE hook_voice_attempts SET text='changed' WHERE native_msg_id='1'");db.commit()
    assert receipts.pending_count(db,proofs)==1
    with pytest.raises(ValueError,match='RECEIPT_CONFLICT'):reconcile(db,proofs)
    db.rollback()
    db.execute('DROP TRIGGER hook_voice_delivery_updated');db.commit()
    with pytest.raises(ValueError,match='INDEX_INVALID'):receipts.pending_count(db,proofs)
    db.close()


def test_pending_rotation_and_time_budget(tmp_path,monkeypatch):
    db=database()
    for n in range(80):attempt(db,str(n+1))
    seen=set();ticks=[0.0]
    def clock():ticks[0]+=.06;return ticks[0]
    monkeypatch.setattr(receipts.time,'monotonic',clock)
    for _ in range(10):
        rows=reconcile(db,tmp_path);assert len(rows)==1
        seen.update(r['native_msg_id'] for r in rows)
        assert receipts.pending_count(db,tmp_path)==80
    assert len(seen)>1
    db.close()


def test_delivery_projection_in_existing_diagnostic_zip(tmp_path):
    import json,zipfile
    from chejin_worker_client.diagnostic_bundle import export_bundle
    root=tmp_path/'home';root.mkdir()
    db=database(root/'worker_client.sqlite3');proofs=root/'transactions/hook'
    rows=[attempt(db,str(i+1)) for i in range(2)]
    write_ack(proofs,rows[0]);reconcile(db,proofs);db.close()
    output=export_bundle(root,tmp_path/'diagnostic.zip')
    with zipfile.ZipFile(output) as z:
        data=json.loads(z.read('state/hook_voice_result_delivery.json'))['rows']
        assert {r['status'] for r in data}=={'pending','confirmed'}
        confirmed=next(r for r in data if r['status']=='confirmed')
        assert confirmed['attempt_digest']==rules.digest(rows[0])
        assert data[0]['status']=='pending'
