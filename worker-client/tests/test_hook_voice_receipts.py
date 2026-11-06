"""Receipt identity and original authorization; no native calls."""
import sqlite3
from types import SimpleNamespace
import pytest
from chejin_worker_client import hook_voice_receipts as receipts
from chejin_worker_client.hook_voice import _ensure_schema
from chejin_worker_client.shared_rules import hook_contract_rules as rules

@pytest.mark.parametrize('state',['transcribed','failed'])
def test_pending_original_result_only_settles_matching_receipt(tmp_path,state):
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row;_ensure_schema(db)
    db.execute('INSERT INTO hook_voice_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ('wxid_owner','epoch','123','wxid_peer','old-generation','a'*64,20,state,'start','end','b'*64,'original',None))
    attempt=dict(db.execute('SELECT * FROM hook_voice_attempts').fetchone())
    binding={'binding_id':'binding','binding_revision':1,'peer_wxid':'wxid_peer'}
    scope=SimpleNamespace(self_wxid='wxid_owner',account_epoch='epoch')
    calls=[]
    def unavailable(*args): calls.append(args[-1]);raise OSError('offline')
    with pytest.raises(OSError):
        receipts.publish(SimpleNamespace(voice_result=unavailable),None,None,binding,attempt,tmp_path)
    assert receipts.pending_results(db,tmp_path)==[attempt]
    assert receipts.retained_jobs(db,tmp_path,scope,{**binding,'binding_revision':2})==[]
    assert receipts.retained_jobs(db,tmp_path,SimpleNamespace(self_wxid='wxid_other',account_epoch='epoch'),binding)==[]
    assert receipts.retained_jobs(db,tmp_path,scope,binding)==[{'native_msg_id':'123'}]
    def confirmed(*args): calls.append(args[-1]);return {'voice_receipt_id':'receipt','native_msg_id':'123','state':state}
    client=SimpleNamespace(voice_result=confirmed)
    receipts.publish(client,None,None,binding,attempt,tmp_path)
    receipts.publish(client,None,None,binding,attempt,tmp_path)
    assert calls==[attempt,attempt] and receipts.pending_results(db,tmp_path)==[]
    path=receipts.receipt_path(tmp_path,attempt)
    path.write_text(rules.canonical({'attempt_digest':'f'*64,'receipt':{}}))
    with pytest.raises(ValueError,match='RECEIPT_CONFLICT'):receipts.pending_results(db,tmp_path)
    assert dict(db.execute('SELECT * FROM hook_voice_attempts').fetchone())==attempt
    db.close()
