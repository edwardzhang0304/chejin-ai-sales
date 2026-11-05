from types import SimpleNamespace
import pytest
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_gateway import HookLocalError


@pytest.mark.parametrize('code,transient',[
    ('HOOK_LOCAL_TRANSPORT_FAILED',True),('HOOK_LOCAL_RESPONSE_INVALID',True),
    ('HOOK_HISTORY_ACCOUNT_MISMATCH',False),('HOOK_PROCESS_GENERATION_CHANGED',False),
    ('HOOK_PROCESS_ACTIVE_CALL_QUARANTINED',False)])
def test_relationship_query_failure_never_becomes_deletion_or_swallows_identity(code,transient,monkeypatch,tmp_path):
    runtime,binding,calls=relationship_runtime(tmp_path,[HookLocalError(code)],[])
    if transient:runtime.review_relationships([binding])
    else:
        with pytest.raises(HookLocalError,match=code):runtime.review_relationships([binding])
    import json
    saved=json.loads(next(runtime.private_root.rglob('latest.json')).read_text())
    assert saved['attempts'][0]['reason_code']==code
    assert calls==['read']


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


@pytest.mark.parametrize('position',[0,1],ids=['first','confirmation'])
@pytest.mark.parametrize('code',[
    'HOOK_LOCAL_TRANSPORT_FAILED','HOOK_LOCAL_RESPONSE_INVALID','HOOK_HTTP_RESPONSE_REJECTED',
    'HOOK_RESPONSE_TOO_LARGE','HOOK_HISTORY_ACCOUNT_MISMATCH','HOOK_PROCESS_GENERATION_CHANGED'])
def test_read_failure_identity_and_reason_reach_actual_zip(tmp_path,monkeypatch,code,position):
    import json,zipfile
    from chejin_worker_client.diagnostic_bundle import export_bundle
    first={'code':0,'data':[{'wxid':'wxid_peer','markname':'changed'}]}
    runtime,binding,calls=relationship_runtime(tmp_path,
        ([first] if position else [])+[HookLocalError(code)],
        [{'recheck_required':True,'initial_check_pending':True,'confirmation_token':'f'*32}])
    if code in {'HOOK_HISTORY_ACCOUNT_MISMATCH','HOOK_PROCESS_GENERATION_CHANGED'}:
        with pytest.raises(HookLocalError,match=code):runtime.review_relationships([binding])
    else:runtime.review_relationships([binding])
    monkeypatch.setenv('CHEJIN_UPDATE_STAGING_ROOT',str(tmp_path/'updates'))
    path=next(runtime.private_root.rglob('latest.json'))
    with zipfile.ZipFile(export_bundle(tmp_path,tmp_path/'export.zip')) as z:
        saved=json.loads(z.read('receipts/'+path.relative_to(tmp_path/'transactions').as_posix()))
    attempt=saved['attempts'][-1]
    assert attempt['reason_code']==code and attempt['stage']=='read' and len(attempt['attempt_id'])==32
    assert len(saved['attempts'])==position+1
    assert saved['binding_id']==binding['binding_id'] and saved['account']==runtime.account
    assert saved['binding_revision']==binding['binding_revision']
    assert len([x for x in calls if isinstance(x,tuple)])==position


def test_http_failure_preserves_post_stage_in_zip(tmp_path,monkeypatch):
    import json,zipfile
    from chejin_worker_client.api import ApiError
    from chejin_worker_client.diagnostic_bundle import export_bundle
    runtime,binding,_=relationship_runtime(tmp_path,[{'code':0,'data':[]}],[])
    def fail(*args):raise ApiError('HOOK_BINDING_REVISION_STALE','stale',409)
    runtime.client.call=fail
    runtime.review_relationships([binding])
    path=next(runtime.private_root.rglob('latest.json'))
    monkeypatch.setenv('CHEJIN_UPDATE_STAGING_ROOT',str(tmp_path/'updates'))
    with zipfile.ZipFile(export_bundle(tmp_path,tmp_path/'export.zip')) as z:
        saved=json.loads(z.read('receipts/'+path.relative_to(tmp_path/'transactions').as_posix()))
    assert saved['attempts'][0]['stage']=='post'
    assert saved['attempts'][0]['reason_code']=='HOOK_BINDING_REVISION_STALE'


from test_failure_evidence_boundaries import home


def test_optional_evidence_write_failure_uses_existing_fallback_and_preserves_identity_error(home,monkeypatch):
    import json,zipfile,sqlite3
    from chejin_worker_client import hook_relationship_evidence as evidence, storage
    from chejin_worker_client.diagnostic_bundle import export_bundle
    def fail(*a,**kw):raise OSError('synthetic readonly evidence directory')
    monkeypatch.setattr(evidence,'_atomic_write_json',fail)
    # A broken log DB must not replace the original identity exception.
    def log_fail(*a,**kw):raise sqlite3.OperationalError('synthetic broken telemetry DB')
    monkeypatch.setattr(storage,'append_log',log_fail)
    runtime,binding,_=relationship_runtime(home,[HookLocalError('HOOK_HISTORY_ACCOUNT_MISMATCH')],[])
    with pytest.raises(HookLocalError,match='HOOK_HISTORY_ACCOUNT_MISMATCH'):
        runtime.review_relationships([binding])
    monkeypatch.setenv('CHEJIN_UPDATE_STAGING_ROOT',str(home/'updates'))
    with zipfile.ZipFile(export_bundle(home,home/'export.zip')) as z:
        data=z.read('logs/runtime/evidence-recorder-failures.jsonl').decode()
        record=json.loads(data.splitlines()[-1])
        assert record['original_error_code']=='HOOK_HISTORY_ACCOUNT_MISMATCH'
        assert record['context']['binding_id']==binding['binding_id']
        assert record['context']['attempts'][0]['stage']=='read'
        assert record['context']['missing_target'].endswith('/latest.json')
        assert record['exception_type']=='OSError'


def test_initial_flow_refusal_does_not_leave_local_flow_or_cancel_task(home):
    from chejin_worker_client.api import ApiError
    from chejin_worker_client import storage
    runtime=object.__new__(HookFormalRuntime)
    runtime.private_root=home/'transactions'/'hook';runtime.credentials=SimpleNamespace(worker_id='w1')
    runtime.heartbeat=lambda **kw:None;runtime.active_flow_id=None
    def rejected(*a):raise ApiError('HOOK_CONTACT_REMARK_CONFIRMATION_PENDING','initial check pending',409)
    runtime.client=SimpleNamespace(call=rejected)
    with pytest.raises(ApiError,match='initial check pending'):
        runtime.start_flow({'id':'original-task'},'conversation')
    assert not storage.load_runtime_control().get('inflight_flow_id')
    assert (runtime.private_root/'flows'/'original-task.json').exists()
    assert not (runtime.private_root/'recovery').exists()
    runtime.client.call=lambda *a:{'flow_id':'original-task','status':'active'}
    assert runtime.start_flow({'id':'original-task'},'conversation')=='original-task'
    assert storage.load_runtime_control()['inflight_flow_id']=='original-task'


def rejected_flow_runtime(home, code='HOOK_CONTACT_REMARK_CONFIRMATION_PENDING'):
    from chejin_worker_client.api import ApiError
    runtime=object.__new__(HookFormalRuntime)
    runtime.private_root=home/'transactions'/'hook';runtime.credentials=SimpleNamespace(worker_id='w1')
    runtime.heartbeat=lambda **kw:None;runtime.active_flow_id=None
    def rejected(*a):raise ApiError(code,'synthetic admission refusal',409)
    runtime.client=SimpleNamespace(call=rejected)
    return runtime


@pytest.mark.parametrize('restart',[False,True])
def test_repeated_initial_flow_refusal_preserves_receipt_and_leaves_no_flow(home,restart):
    from chejin_worker_client.api import ApiError
    from chejin_worker_client import storage
    runtime=rejected_flow_runtime(home);before=None
    for index in range(4):
        if restart and index:runtime=rejected_flow_runtime(home)
        with pytest.raises(ApiError) as error:
            runtime.start_flow({'id':'original-task'},'conversation')
        assert error.value.code=='HOOK_CONTACT_REMARK_CONFIRMATION_PENDING'
        assert not storage.load_runtime_control().get('inflight_flow_id')
        receipt=runtime.private_root/'flow-denials'/'original-task.json'
        snapshot=(receipt.read_bytes(),receipt.stat().st_mtime_ns)
        if before is not None:assert snapshot==before
        before=snapshot
    runtime.client.call=lambda *a:{'flow_id':'original-task','status':'active'}
    assert runtime.start_flow({'id':'original-task'},'conversation')=='original-task'
    assert receipt.read_bytes()==before[0]


@pytest.mark.parametrize('failure',['conflict','corrupt','write','network','existing_flow','unknown_send'])
def test_flow_denial_never_overwrites_or_clears_uncertain_attempt(home,monkeypatch,failure):
    from chejin_worker_client import storage,hook_formal_runtime as module
    from chejin_worker_client.api import ApiError
    runtime=rejected_flow_runtime(home)
    path=runtime.private_root/'flow-denials'/'original-task.json'
    original=None
    if failure in {'conflict','corrupt'}:
        path.parent.mkdir(parents=True)
        original=b'{"server_rejected":false}' if failure=='conflict' else b'{unfinished'
        path.write_bytes(original)
    if failure=='write':
        save=module._save
        def fail(target,value):
            if target.parent.name=='flow-denials':raise OSError('synthetic disk failure')
            return save(target,value)
        monkeypatch.setattr(module,'_save',fail)
    if failure in {'network','existing_flow','unknown_send'}:
        code={'network':'HOOK_BACKEND_NETWORK_FAILED','existing_flow':'WORKER_INFLIGHT_FLOW_CONFLICT',
              'unknown_send':'HOOK_SEND_ALREADY_ATTEMPTED'}[failure]
        def failed(*a):raise ApiError(code,'synthetic uncertainty',503 if failure=='network' else 409)
        runtime.client.call=failed
        if failure!='network':storage.begin_runtime_flow('original-task','chat_reply')
    expected=ApiError if failure in {'network','existing_flow','unknown_send'} else OSError if failure=='write' else ValueError
    with pytest.raises(expected):runtime.start_flow({'id':'original-task'},'conversation')
    assert storage.load_runtime_control()['inflight_flow_id']=='original-task'
    if original is not None:assert path.read_bytes()==original
    if failure in {'network','existing_flow','unknown_send','write'}:assert not path.exists()
    assert not (runtime.private_root/'recovery').exists()
