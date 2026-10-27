"""Real delivery closure -> subsequent unresolved callback -> bounded actual ZIP."""
import json,zipfile
from datetime import datetime
import pytest
from test_hook_customer_refusal_closure import case,clean,identity_client,lab,callback
from chejin_worker_client.hook_inbox import NativeInbox
from chejin_worker_client import storage
from chejin_worker_client.api import ApiError
from app.models.wechat import WechatSessionBinding
from app.core.database import SessionLocal
from app.services.wechat_service import _set_binding_state

@pytest.mark.parametrize('proof',['valid','corrupt','missing','unknown'])
def test_terminal_coverage_cannot_displace_unfinished_delivery(case,monkeypatch,tmp_path,proof):
    c=case;callback(c)
    def state(enabled):
        with SessionLocal() as db:
            b=db.get(WechatSessionBinding,c.binding['binding_id'])
            _set_binding_state(b,status='bound',listen_status='listening' if enabled else 'paused',
                allow_listening=enabled,preserve_lead=True,remark_code=b.remark_code)
            db.commit()
    state(False)
    try:c.rt.drain(c.binding)
    except ApiError as exc:assert exc.code=='HOOK_BINDING_PAUSED'
    else:raise AssertionError('Expected actual old authorization refusal')
    state(True)
    inventory=c.rt.client.call(c.rt.credentials,'POST',f'/workers/{c.w["id"]}/wechat/native-work',c.rt.account)
    current=next(b for b in inventory['bindings'] if b['binding_id']==c.binding['binding_id'])
    c.rt.drain(current)
    assert storage.hook_update_business_blockers(c.rt.scope.process_generation)['hook_pending_delivery']==0
    raw=json.dumps({'ServerPort':str(c.rt.scope.hook_api_port),'selfwxid':c.rt.scope.self_wxid,
        'sendorrecv':'2','msgnumber':'1','msglist':[{'msgsvrid':'9000000000000000020','msgtype':'1',
        'fromtype':'1','fromid':c.binding['peer_wxid'],'toid':c.rt.scope.self_wxid,
        'msg':'synthetic later unresolved callback','time':datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]}).encode()
    later=NativeInbox(max_body_bytes=65536,max_inbox_bytes=1048576).persist(raw,c.rt.scope)
    projected=c.rt.projection.project_next(c.rt.scope)
    assert projected['receive_seq']==later['receive_seq']
    c.rt.outbox.enqueue_projection(later['receive_seq'],c.rt.delivery(current),now=datetime.now().astimezone().isoformat())
    with storage.db_connection() as db:
        original=dict(db.execute('SELECT * FROM hook_delivery_outbox ORDER BY rowid LIMIT 1').fetchone())
        if proof=='corrupt':
            value=json.loads(original['receipt_json']);value['replacement_dispatch']['message_event_ids']=[]
            db.execute('UPDATE hook_delivery_outbox SET receipt_json=? WHERE event_uuid=?',(json.dumps(value),original['event_uuid']))
        elif proof=='missing':db.execute('UPDATE hook_delivery_outbox SET receipt_json=NULL WHERE event_uuid=?',(original['event_uuid'],))
        elif proof=='unknown':db.execute("UPDATE hook_delivery_outbox SET status='future_unknown' WHERE event_uuid=?",(original['event_uuid'],))
        db.commit()
        before=[dict(r) for r in db.execute('SELECT * FROM hook_delivery_outbox ORDER BY rowid')]
    assert before[-1]['status']=='pending'
    import chejin_worker_client.diagnostic_bundle as diagnostic
    monkeypatch.setattr(diagnostic,'MAX_ROWS',2) # Real exporter with a small bounded budget, not a 1000-row claim.
    target=tmp_path/'priority.zip';diagnostic.export_bundle(tmp_path,target)
    with zipfile.ZipFile(target) as z:
        name=next(n for n in z.namelist() if n.endswith('state/hook_delivery_outbox.json'))
        exported=json.loads(z.read(name))['rows']
        manifest=json.loads(z.read('export.json'))
        raw_rows=json.loads(z.read('state/hook_raw_inbox.json'))['rows']
        projections=json.loads(z.read('state/hook_observation_projection.json'))['rows']
        assert all(any(p['receive_seq']==r['receive_seq'] for p in projections) for r in raw_rows)
        if proof=='valid':
            assert any(r['receive_seq']==later['receive_seq'] for r in raw_rows)
        if proof in {'corrupt','missing'}:
            assert 'delivery_coverage_proof_invalid' in json.dumps(manifest) or 'delivery_proof_unavailable_or_size_limit' in json.dumps(manifest)
        assert 'critical_rows_limit' in json.dumps(manifest) or 'row_or_time_limit' in json.dumps(manifest)
    with storage.db_connection() as db:assert before==[dict(r) for r in db.execute('SELECT * FROM hook_delivery_outbox ORDER BY rowid')]
    print({'source_states':[r['status'] for r in before],'exported_states':[r['status'] for r in exported],
        'later_receive_seq':later['receive_seq']})
    if proof=='valid':
        assert any(r['receive_seq']==later['receive_seq'] for r in exported), 'Completed coverage displaced actual unfinished outbox from ZIP'
    else:
        assert exported[0]['event_uuid']==original['event_uuid'], 'Unverified proof must not be demoted to completed history'

    if proof=='valid':
        monkeypatch.setattr(diagnostic,'MAX_ROWS',1000)
        complete=tmp_path/'complete.zip';diagnostic.export_bundle(tmp_path,complete)
        with zipfile.ZipFile(complete) as z:
            deliveries=json.loads(z.read('state/hook_delivery_outbox.json'))
            assert not deliveries['truncated'] and len(deliveries['rows'])==3
            covered=next(r for r in deliveries['rows'] if r['status']=='covered_by_new_authorization')
            assert covered['receipt_json']['kind']=='hook-delivery-coverage-v1'
            assert covered['receipt_json']['replacement_dispatch']['dispatch_status']=='facts_stored'
            assert all(isinstance(m['content'],dict) and m['content']['omitted']=='message_or_audio_body'
                       for m in covered['receipt_json']['replacement_envelope']['messages'])
            assert len(json.loads(z.read('state/hook_raw_inbox.json'))['rows'])==2
            assert len(json.loads(z.read('state/hook_observation_projection.json'))['rows'])==2
