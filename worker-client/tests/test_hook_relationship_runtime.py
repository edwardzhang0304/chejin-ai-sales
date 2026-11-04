from types import SimpleNamespace
import pytest
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_gateway import HookLocalError


@pytest.mark.parametrize('code,transient',[
    ('HOOK_LOCAL_TRANSPORT_FAILED',True),('HOOK_LOCAL_RESPONSE_INVALID',True),
    ('HOOK_HISTORY_ACCOUNT_MISMATCH',False),('HOOK_PROCESS_GENERATION_CHANGED',False),
    ('HOOK_PROCESS_ACTIVE_CALL_QUARANTINED',False)])
def test_relationship_query_failure_never_becomes_deletion_or_swallows_identity(code,transient,monkeypatch):
    from chejin_worker_client import storage
    logs=[];monkeypatch.setattr(storage,'append_log',lambda *a,**kw:logs.append((a,kw)))
    def fail(*a):raise HookLocalError(code)
    runtime=object.__new__(HookFormalRuntime)
    runtime.gateway=SimpleNamespace(friend_list=fail);runtime.process=object();runtime.scope=object()
    # There is deliberately no client: failures cannot issue state mutations.
    if transient:
        runtime.review_relationships([{'binding_id':'unused'}]);assert len(logs)==1
        assert logs[0][1]['metadata']=={'reason_code':code}
    else:
        with pytest.raises(HookLocalError,match=code):runtime.review_relationships([{'binding_id':'unused'}])
        assert not logs


def test_retired_invitation_has_terminal_description_not_waiting_or_bound_success():
    from chejin_worker_client.hook_progress import HookProgressTimeline
    timeline=HookProgressTimeline()
    for state,note in [('waiting','recipient'),('done','invite_replaced')]:
        timeline.apply({'event':'hook_progress','context':'old-invite','phase':'bind','state':state,'note':note})
    assert len(timeline.steps)==1
    assert timeline.steps[0]['state']=='done'
    assert timeline.steps[0]['title']=='本轮好友绑定已结束'
    assert '新的邀请任务' in timeline.steps[0]['description']


def relationship_runtime(tmp_path, reads, responses):
    from chejin_worker_client.shared_rules import hook_contract_rules as rules
    import json
    runtime=object.__new__(HookFormalRuntime)
    runtime.private_root=tmp_path/'transactions'/'hook'
    runtime.account={'self_wxid':'wxid_self','account_epoch':'a'*36,'process_generation':'g1'}
    runtime.process=object();runtime.scope=object();runtime.credentials=SimpleNamespace(worker_id='w1')
    calls=[]
    def read(*args):
        calls.append('read');value=reads.pop(0)
        if isinstance(value,Exception):raise value
        return SimpleNamespace(payload=value)
    def call(credentials, method, path, body):
        calls.append(('post',body))
        return responses.pop(0)
    runtime.gateway=SimpleNamespace(friend_list=read);runtime.client=SimpleNamespace(call=call)
    binding={'binding_id':'binding1','binding_revision':1,'peer_wxid':'wxid_peer','remark_code':'CJTEST01'}
    return runtime,binding,calls


def test_confirmation_reads_again_after_challenge_and_diagnostic_zip_contains_both(tmp_path,monkeypatch):
    import json,zipfile
    from chejin_worker_client.diagnostic_bundle import export_bundle
    from test_failure_evidence_boundaries import home
    from chejin_worker_client.shared_rules import hook_contract_rules as rules
    first={'code':0,'data':[{'wxid':'wxid_peer','markname':''},
                          {'wxid':'wxid_private_other','markname':'DO_NOT_EXPORT_OTHER_CONTACT'}]}
    second={'code':0,'data':[{'wxid':'wxid_peer','markname':'CJTEST01'}]}
    responses=[{'recheck_required':True,'confirmation_token':'f'*32,'review_required':False},
               {'recheck_required':False,'review_required':False,'state':'matched'}]
    runtime,binding,calls=relationship_runtime(tmp_path,[first,second],responses)
    runtime.review_relationships([binding])
    assert [x if isinstance(x,str) else x[0] for x in calls]==['read','post','read','post']
    assert calls[1][1]['friend_list']==first and calls[3][1]['friend_list']==second
    assert calls[3][1]['confirmation_token']=='f'*32
    path=next(runtime.private_root.rglob('latest.json'))
    value=json.loads(path.read_text());assert len(value['attempts'])==2
    assert [x['comparison']['state'] for x in value['attempts']]==['remark_changed','matched']
    assert 'DO_NOT_EXPORT_OTHER_CONTACT' not in path.read_text()
    assert 'confirmation_token' not in path.read_text()
    # Normal polling must not erase the anomaly's two observations.
    before=path.read_bytes()
    runtime.gateway=SimpleNamespace(friend_list=lambda *a:SimpleNamespace(payload=second))
    runtime.client=SimpleNamespace(call=lambda *a:{'recheck_required':False,'review_required':False,'state':'matched'})
    runtime.review_relationships([binding]);assert path.read_bytes()==before
    monkeypatch.setenv('CHEJIN_UPDATE_STAGING_ROOT',str(tmp_path/'updates'))
    output=export_bundle(tmp_path,tmp_path/'export.zip')
    with zipfile.ZipFile(output) as z:
        member='receipts/'+path.relative_to(tmp_path/'transactions').as_posix()
        assert member in z.namelist()
        exported=json.loads(z.read(member));assert len(exported['attempts'])==2
        assert exported['attempts'][0]['comparison']['peer_objects'][0]['fields'][1]['value']==''
        manifest=json.loads(z.read('export.json'))
        assert any(member in str(x) and x['status']=='collected' for x in manifest['sources'])


def test_failed_second_read_cannot_post_reused_first_list(tmp_path,monkeypatch):
    from chejin_worker_client import storage
    import json
    monkeypatch.setattr(storage,'append_log',lambda *a,**kw:None)
    first={'code':0,'data':[{'wxid':'wxid_peer','markname':'changed'}]}
    runtime,binding,calls=relationship_runtime(tmp_path,[first,HookLocalError('HOOK_LOCAL_TRANSPORT_FAILED')],
        [{'recheck_required':True,'confirmation_token':'f'*32,'review_required':False}])
    runtime.review_relationships([binding])
    assert len([c for c in calls if isinstance(c,tuple)])==1
    saved=json.loads(next(runtime.private_root.rglob('latest.json')).read_text())
    assert saved['attempts'][-1]['read_failed'] and saved['result']['recheck_required']


def test_target_evidence_has_bounded_raw_values_and_explicit_omissions():
    from chejin_worker_client.shared_rules import hook_contact_rules as rules
    value=rules.contact_observation({'data':[{'wxid':'wxid_peer','markname':'X'*400}]},
                                    'CJTEST01','wxid_self','wxid_peer')
    field=value['peer_objects'][0]['fields'][1]
    assert field['value']=='X'*256 and field['truncated'] and field['utf8_bytes']==400
    assert len(field['sha256'])==64 and value['state']=='remark_changed'


def test_pending_review_at_send_claim_does_not_close_original_flow(tmp_path):
    from contextlib import nullcontext
    from chejin_worker_client.api import ApiError
    runtime=object.__new__(HookFormalRuntime)
    data={'batch_id':'batch','reply_action':{'id':'action','status':'queued'},'task':{'id':'task'}}
    runtime.register_batch_progress=lambda *a:None;runtime.progress=lambda *a:None
    runtime.batch_view=lambda *a:data;runtime.active_flow_id='original-flow'
    runtime.claim=lambda *a:{'lease_fencing_token':1};runtime.task_guard=lambda *a:nullcontext(lambda:None)
    runtime.credentials=object();runtime.account={}
    def pending(*a,**kw):raise ApiError('HOOK_CONTACT_REMARK_CONFIRMATION_PENDING','pending',409)
    runtime.client=SimpleNamespace(native_send_claim=pending)
    runtime.close_unpermitted_reply=lambda *a:pytest.fail('transient review must not cancel original flow')
    with pytest.raises(ApiError,match='pending'):
        runtime.reply_once({'conversation_id':'conversation'},data,history={'history_ready':True,'history_state_digest':'d'})
    assert runtime.active_flow_id=='original-flow'


def test_second_read_identity_failure_is_preserved_and_not_swallowed(tmp_path):
    import json
    first={'code':0,'data':[{'wxid':'wxid_peer','markname':'changed'}]}
    runtime,binding,calls=relationship_runtime(tmp_path,[first,HookLocalError('HOOK_HISTORY_ACCOUNT_MISMATCH')],
        [{'recheck_required':True,'confirmation_token':'f'*32,'review_required':False}])
    with pytest.raises(HookLocalError,match='HOOK_HISTORY_ACCOUNT_MISMATCH'):
        runtime.review_relationships([binding])
    saved=json.loads(next(runtime.private_root.rglob('latest.json')).read_text())
    assert saved['attempts'][-1]['reason_code']=='HOOK_HISTORY_ACCOUNT_MISMATCH'
    assert len([c for c in calls if isinstance(c,tuple)])==1
