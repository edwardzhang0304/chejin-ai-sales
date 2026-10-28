"""Immutable original journals only authorize receipt settlement, never sending."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import hashlib,json
import pytest
from chejin_worker_client.hook_sender import RetainedNativeSendPermit
from chejin_worker_client.hook_backend import HookWorkerCredentials
from chejin_worker_client.hook_inbox import HookReceiveScope

@pytest.fixture
def retained(tmp_path):
    credentials=HookWorkerCredentials('11111111-1111-4111-8111-111111111111','private-test-token','test-instance')
    scope=HookReceiveScope('wxid_sales_A','22222222-2222-4222-8222-222222222222','generation-1',30001)
    metadata={'task_id':'33333333-3333-4333-8333-333333333333','reply_action_id':'44444444-4444-4444-8444-444444444444',
              'flow_id':'33333333-3333-4333-8333-333333333333','lease_fencing_token':1}
    binding={'peer_wxid':'wxid_customer_A'}
    folder=tmp_path/credentials.worker_id/scope.account_epoch/metadata['reply_action_id'];folder.mkdir(parents=True)
    intent={'schema_version':'hook-send-journal-v1','task_id':metadata['task_id'],'reply_action_id':metadata['reply_action_id'],
        'self_wxid':scope.self_wxid,'account_epoch':scope.account_epoch,'process_generation':scope.process_generation,
        'peer_wxid':binding['peer_wxid'],'send_token':'original-private-send-token','attempted_at':'2026-10-11T11:47:12+00:00'}
    (folder/'intent.json').write_text(json.dumps(intent));digest=hashlib.sha256((folder/'intent.json').read_bytes()).hexdigest()
    metadata['journal_sha256']=digest
    (folder/'trigger.json').write_text(json.dumps({'phase':'trigger_attempted','intent_sha256':digest}))
    (folder/'outcome.json').write_text(json.dumps({'task_id':metadata['task_id'],'reply_action_id':metadata['reply_action_id'],
        'call_completed':True,'automatic_resend_allowed':False,'journal_sha256':digest,'completed_at':'2026-10-11T11:47:13+00:00'}))
    return tmp_path,credentials,scope,binding,metadata,folder

def test_loader_keeps_original_completion_and_has_no_send_capability(retained):
    root,c,s,b,m,folder=retained
    before={p.name:p.read_bytes() for p in folder.iterdir()}
    permit,outcome=RetainedNativeSendPermit.load(root,c,s,b,m)
    assert outcome['completed_at']=='2026-10-11T11:47:13+00:00'
    assert permit.lease_fencing_token==1 and permit.flow_id==m['flow_id']
    assert 'original-private-send-token' not in repr(permit)
    assert not hasattr(permit,'reply_text') and not hasattr(permit,'validate_scope')
    assert {p.name:p.read_bytes() for p in folder.iterdir()}==before

@pytest.mark.parametrize('case',['missing_trigger','journal_hash','clock_reverse','generation','peer','outcome_type','metadata_path','task'])
def test_damaged_or_relabelled_original_send_cannot_be_settled(retained,case):
    root,c,s,b,m,folder=retained
    if case=='missing_trigger':(folder/'trigger.json').unlink()
    if case=='journal_hash':m['journal_sha256']='f'*64
    if case=='clock_reverse':
        p=folder/'outcome.json';d=json.loads(p.read_text());d['completed_at']='2026-10-11T11:47:11+00:00';p.write_text(json.dumps(d))
    if case=='generation':s=replace(s,process_generation='other-generation')
    if case=='peer':b={'peer_wxid':'wxid_customer_B'}
    if case=='outcome_type':
        p=folder/'outcome.json';d=json.loads(p.read_text());d['call_completed']=1;p.write_text(json.dumps(d))
    if case=='metadata_path':m['reply_action_id']='../other'
    if case=='task':m['task_id']='55555555-5555-4555-8555-555555555555'
    with pytest.raises(ValueError,match='HOOK_SEND_RETAINED_'):RetainedNativeSendPermit.load(root,c,s,b,m)

@pytest.mark.parametrize('case',['match','generation','kind','uncertain','long_duration','reverse','missing'])
def test_old_completed_call_supplies_original_local_clock_without_rewriting(retained,monkeypatch,tmp_path,case):
    from chejin_worker_client import storage
    from chejin_worker_client.hook_active_calls import ensure
    root,c,s,b,m,folder=retained
    monkeypatch.setattr(storage,'APP_DIR',tmp_path/'isolated-home')
    monkeypatch.setattr(storage,'DB_FILE',tmp_path/'isolated-home'/'worker_client.sqlite3')
    if case!='missing':
        with storage.db_connection() as conn:
            ensure(conn)
            conn.execute('INSERT INTO hook_active_calls VALUES(?,?,?,?,?,?)',(
                'other' if case=='generation' else s.process_generation,m['reply_action_id'],
                'QueryDB' if case=='kind' else 'send_text','uncertain' if case=='uncertain' else 'completed',
                '2026-10-11T11:47:14+00:00',
                '2026-10-11T11:47:20+00:00' if case=='long_duration' else
                '2026-10-11T11:47:13+00:00' if case=='reverse' else '2026-10-11T11:47:15+00:00'))
            conn.commit()
    before={p.name:p.read_bytes() for p in folder.iterdir()}
    _,outcome=RetainedNativeSendPermit.load(root,c,s,b,m)
    assert ('local_call_window' in outcome)==(case=='match')
    if case=='match':
        assert outcome['local_call_window']['started_at']=='2026-10-11T11:47:14+00:00'
        assert outcome['completed_at']=='2026-10-11T11:47:13+00:00'
    assert {p.name:p.read_bytes() for p in folder.iterdir()}==before
