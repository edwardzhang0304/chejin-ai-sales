"""Real local files/process admission; Windows budgets are explicitly simulated."""
from argparse import Namespace
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import uuid

import pytest

from chejin_worker_client import rpa_bridge, action_journal, storage
from chejin_worker_client.c2_contract import c2_contract_v3
from apps.wechat_ai_customer_service.adapters import send_request_file as files, send_launch_journal as launch, send_setup_contract as rules
from apps.wechat_ai_customer_service.adapters.send_request_admission import admit

TEXT='你好，这是完整回复。'

@pytest.fixture
def original(tmp_path, monkeypatch):
    root=tmp_path/'用户 中文 😀'
    for module in (rpa_bridge,action_journal):
        monkeypatch.setattr(module,'CONFIG',replace(module.CONFIG,app_dir=root))
    monkeypatch.setattr(storage,'APP_DIR',root);monkeypatch.setattr(storage,'DB_FILE',root/'worker_client.sqlite3')
    action=uuid.uuid4().hex;task=uuid.uuid4().hex
    context={'task_id':task,'reply_action_id':action,'conversation_id':uuid.uuid4().hex,
             'flow_id':task,'authorization_revision':'binding:1','reply_text_hash':hashlib.sha256(TEXT.encode()).hexdigest()}
    path=action_journal.action_journal_path('send',action)
    action_journal.initialize_action_journal(path,action_kind='send',transaction_id=action,conversation_id=context['conversation_id'],
        canonical_action_id=action,reserved_worker_stable_id='reserved-send',items=[{'journal_item_id':action}],
        prepare_evidence={'pre_send_setup_context':context})
    bridge=rpa_bridge.RpaBridge();bridge.mode='real'
    return bridge,path,context


def material(original,guard=None):
    bridge,path,ctx=original
    attempt=launch.begin(path,task_id=ctx['task_id'],action_id=ctx['reply_action_id'])
    raw=rules.package_bytes(request_id=attempt['request_id'],task_id=ctx['task_id'],reply_action_id=ctx['reply_action_id'],
        target='CJTEST01',text=TEXT,expected_context_guard=guard or {'原文':['客户中文😀']})
    ref=files.write_package(raw,request_id=attempt['request_id'],app_dir=rpa_bridge.CONFIG.app_dir,
                            fallback_root=lambda:(_ for _ in ()).throw(AssertionError('unexpected fallback')))
    launch.update(path,attempt['launch_attempt_id'],allowed={'preparing'},process_state='prepared',request=ref)
    launch.update(path,attempt['launch_attempt_id'],allowed={'prepared'},process_state='creating')
    args=Namespace(action='send',target='CJTEST01',text=TEXT,action_journal=str(path),expected_context_guard='',
        expected_context_guard_file=ref['path'],expected_context_guard_sha256=ref['sha256'],send_task_id=ctx['task_id'],send_action_id=ctx['reply_action_id'])
    return args,ref,raw,attempt

@pytest.mark.parametrize('units',[239,240,241,259,260,261,340])
def test_complete_path_budget_and_fallback(tmp_path,units):
    request=uuid.uuid4().hex
    base=tmp_path.resolve();budget=units-len(str(base))-len('/ipc/'+request+'.json')-1
    # Split across components so a native POSIX component limit does not stand
    # in for the Windows full-path boundary being tested.
    segments=[]
    while budget>100:segments.append('a'*99);budget-=100
    segments.append('a'*budget)
    app=base.joinpath(*segments)
    assert files.utf16_units(app/'ipc'/(request+'.json'))==units
    value=files.write_package(b'{}',request_id=request,app_dir=app,fallback_root=lambda:tmp_path/'fallback')
    assert value['root_choice']==(0 if units<=240 else 1)
    assert value['path_utf16_units']<=240
    assert Path(value['path']).read_bytes()==b'{}'


def test_real_bytes_once_and_unique_request(original,monkeypatch):
    args,ref,raw,_=material(original,{'history':[{'text':'客户 '+str(i)+' 😀'} for i in range(200)]})
    native=Path.read_bytes;reads=[]
    def read(p):
        if p==Path(ref['path']):reads.append(str(p))
        return native(p)
    monkeypatch.setattr(Path,'read_bytes',read)
    guard,rejected=admit(args)
    assert rejected is None and len(guard['history'])==200 and reads==[ref['path']]
    assert ref['sha256']==hashlib.sha256(raw).hexdigest()
    with pytest.raises(FileExistsError):
        files.write_package(b'overwrite',request_id=ref['request_id'],app_dir=rpa_bridge.CONFIG.app_dir)
    assert native(Path(ref['path']))==raw


@pytest.mark.parametrize('bad',['digest','missing','partial','version','request','target','text','task','action','guard','inline','shortargs'])
def test_bad_file_never_reaches_ui(original,monkeypatch,bad):
    args,ref,raw,_=material(original)
    if bad=='missing':Path(ref['path']).unlink()
    elif bad=='partial':Path(ref['path']).write_bytes(b'{')
    elif bad=='digest':Path(ref['path']).write_bytes(raw+b' ')
    elif bad=='inline':args.expected_context_guard='{}'
    elif bad=='shortargs':args.send_task_id=''
    else:
        value=json.loads(raw)
        key={'version':'schema_version','request':'request_id','target':'target','text':'reply_text_sha256','task':'task_id','action':'reply_action_id','guard':'expected_context_guard'}[bad]
        value[key]=999 if bad=='version' else 'wrong'
        changed=json.dumps(value).encode();Path(ref['path']).write_bytes(changed)
        # Trusted short digest remains the original. Even a matching new
        # transport digest must still reject envelope identity below.
        if bad not in ('task','action'):
            digest=hashlib.sha256(changed).hexdigest();args.expected_context_guard_sha256=digest
            journal,_=launch.read(original[1]);journal['send_launch_attempts'][-1]['request']['sha256']=digest
            launch._write(original[1],journal)
    from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
    monkeypatch.setattr(sidecar,'ensure_visible_wechat_window',lambda **kw:pytest.fail('UI reached on invalid send request'))
    result=sidecar.run_action(args)
    assert result['error_code']=='RPA_SEND_REQUEST_INVALID' and result['ui_action_performed'] is False


def test_popen_failure_is_proven_and_guard_is_not_on_command(original,monkeypatch):
    bridge,path,ctx=original;commands=[]
    def fail(cmd,**kwargs):
        commands.append(cmd)
        journal,_=launch.read(path)
        assert journal['send_launch_attempts'][-1]['process_state']=='creating'
        raise OSError(206,'controlled process creation failure')
    monkeypatch.setattr(rpa_bridge.subprocess,'Popen',fail)
    guard={'history':[{'text':'完整资料😀'*100,'index':i} for i in range(200)]}
    result=bridge.send_reply(target='CJTEST01',rpa_session_key='',text=TEXT,task_id=ctx['task_id'],reply_action_id=ctx['reply_action_id'],expected_context_guard=guard)
    assert result['error_code']=='RPA_SEND_PROCESS_NOT_STARTED'
    assert result['pre_send_setup_failure']['process_state']=='create_failed'
    assert len(commands)==1 and '--expected-context-guard' not in commands[0]
    assert files.command_units(commands[0])<32767
    ref=result['send_request_files'][0]
    assert json.loads(Path(ref['path']).read_bytes())['expected_context_guard']==guard
    assert launch.proof(path,contract=c2_contract_v3())==result['pre_send_setup_failure']


@pytest.mark.parametrize('where',['write','fsync','rename'])
def test_preparation_failure_never_creates_process(original,monkeypatch,where):
    bridge,path,ctx=original
    def fail(*args,**kw):raise PermissionError('controlled disk failure')
    if where=='write':monkeypatch.setattr(files,'write_package',fail)
    else:
        # Fault only the input file, not the independent durable action log.
        original_fn=getattr(files.os,where)
        if where=='rename':monkeypatch.setattr(files.os,'rename',fail)
        else:
            request_fds=set();native_open=Path.open
            def open_file(path,*args,**kwargs):
                stream=native_open(path,*args,**kwargs)
                if path.suffix=='.tmp':request_fds.add(stream.fileno())
                return stream
            def scoped(fd):
                if fd in request_fds:
                    request_fds.remove(fd);fail()
                return original_fn(fd)
            monkeypatch.setattr(Path,'open',open_file)
            monkeypatch.setattr(files.os,'fsync',scoped)
    monkeypatch.setattr(rpa_bridge.subprocess,'Popen',lambda *a,**k:pytest.fail('unexpected process creation'))
    result=bridge.send_reply(target='CJTEST01',rpa_session_key='',text=TEXT,task_id=ctx['task_id'],reply_action_id=ctx['reply_action_id'],expected_context_guard={})
    assert result['error_code']=='RPA_SEND_REQUEST_PREPARE_FAILED'
    assert result['pre_send_setup_failure']['process_state']=='not_called'


@pytest.mark.parametrize('phase',['trigger_attempted','confirmed','creating','finished_unknown'])
def test_previous_unknown_cannot_be_overwritten_by_new_nonstart(original,phase):
    bridge,path,ctx=original
    if phase in ('creating','finished_unknown'):
        _,_,_,attempt=material(original)
        if phase=='finished_unknown':launch.update(path,attempt['launch_attempt_id'],allowed={'creating'},process_state='finished',action_phase=None,physical_send_triggered=None)
    else:
        action_journal.update_action_journal_item(path,journal_item_id=ctx['reply_action_id'],action_phase=phase)
    with pytest.raises(ValueError,match='UNRESOLVED'):
        launch.begin(path,task_id=ctx['task_id'],action_id=ctx['reply_action_id'])
    assert launch.proof(path,contract=c2_contract_v3()) is None


def test_command_counts_every_argument_including_quoting():
    command=['C:\\Program Files\\Worker.exe','--text','😀中文 space','x'*32720]
    assert files.command_units(command)==len((subprocess.list2cmdline(command)+'\0').encode('utf-16-le'))//2
    with pytest.raises(ValueError,match='COMMAND_BUDGET'):
        files.validate_command(command)


@pytest.mark.parametrize('argv',[
 ['send','--expected-context-guard-file',''],
 ['send','--expected-context-guard-file='],
 ['send','--expected-context-guard-file','x','--expected-context-guard',''],
])
def test_cli_presence_rejects_even_empty_file_flags(monkeypatch,argv):
    from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
    monkeypatch.setattr(sidecar,'ensure_visible_wechat_window',lambda **kw:pytest.fail('UI reached'))
    result=sidecar.run_sidecar_cli(argv)
    assert result['error_code']=='RPA_SEND_REQUEST_INVALID'
    assert result['ui_action_performed'] is False


def test_later_oserror_is_not_creation_failure(original,monkeypatch):
    bridge,path,ctx=original
    class Started:
        def __init__(self,cmd,**kw):self.args=cmd;self.returncode=None;self.pid=123
        def communicate(self,timeout=None):raise OSError('read pipe failed after creation')
    monkeypatch.setattr(rpa_bridge.subprocess,'Popen',Started)
    with pytest.raises(OSError,match='after creation'):
        bridge.send_reply(target='CJTEST01',rpa_session_key='',text=TEXT,task_id=ctx['task_id'],reply_action_id=ctx['reply_action_id'],expected_context_guard={})
    assert launch.read(path)[0]['send_launch_attempts'][-1]['process_state']=='creating'
    assert launch.proof(path,contract=c2_contract_v3()) is None


def test_original_bounded_read_retry_still_uses_fresh_request(original,monkeypatch):
    bridge,path,ctx=original;seen=[]
    class ReadFailed:
        def __init__(self,cmd,**kw):
            self.args=cmd;self.returncode=0
            seen.append(cmd)
        def communicate(self,timeout=None):
            return json.dumps({'ok':False,'action_phase':'not_attempted','physical_send_triggered':False,
                'error_code':'SEND_BASELINE_UNAVAILABLE','send_result':{'result':'failed','ok':False}}),''
    monkeypatch.setattr(rpa_bridge.subprocess,'Popen',ReadFailed)
    first=bridge.send_reply(target='CJTEST01',rpa_session_key='',text=TEXT,task_id=ctx['task_id'],reply_action_id=ctx['reply_action_id'],expected_context_guard={})
    assert launch.read(path)[0]['send_launch_attempts'][0]['process_state']=='finished'
    def fail(*args,**kw):raise OSError('second attempt cannot start')
    monkeypatch.setattr(rpa_bridge.subprocess,'Popen',fail)
    second=bridge.send_reply(target='CJTEST01',rpa_session_key='',text=TEXT,task_id=ctx['task_id'],reply_action_id=ctx['reply_action_id'],expected_context_guard={})
    assert second['pre_send_setup_failure']['process_state']=='create_failed'
    refs=second['send_request_files'];assert len(refs)==2 and refs[0]['request_id']!=refs[1]['request_id']
    assert all(Path(ref['path']).is_file() for ref in refs)


def test_input_file_export_and_retention_follow_original_outbox(original,tmp_path,monkeypatch):
    from datetime import datetime,timedelta,timezone
    from types import SimpleNamespace
    import zipfile
    from chejin_worker_client import send_request_evidence as lifecycle
    bridge,path,ctx=original
    args,ref,raw,attempt=material(original)
    launch.fail(path,attempt['launch_attempt_id'],process_state='create_failed',error_code='RPA_SEND_PROCESS_NOT_STARTED',reason='native test failure')
    result=launch.failure_result(path,contract=c2_contract_v3())
    storage.save_reply_send_intent(reply_action_id=ctx['reply_action_id'],task_id=ctx['task_id'],send_token='synthetic-token',reply_text_hash=ctx['reply_text_hash'])
    payload={'send_result':'failed','action_phase':'not_attempted','reply_text_hash':ctx['reply_text_hash'],
             'evidence':{'pre_send_setup_failure':result['pre_send_setup_failure'],'send_request_files':[ref]}}
    storage.finalize_reply_send_ack(reply_action_id=ctx['reply_action_id'],ack_payload=payload)
    runner=SimpleNamespace(bridge=bridge,binding=SimpleNamespace(run_status='running'),_pending_run_status_sync=None)
    with zipfile.ZipFile(tmp_path/'evidence.zip','w') as z:
        omissions=[];index=lifecycle.export_files(z,secrets={'synthetic-token'},max_bytes=10000000,omissions=omissions)
        assert omissions==[] and len(index)==1
        assert z.read('ipc/'+ref['request_id']+'.json')==raw
        assert index[0]['sha256']==ref['sha256']
        assert not any(str(tmp_path) in name for name in z.namelist())
    now=datetime.now(timezone.utc)+timedelta(days=40)
    assert lifecycle.cleanup(runner,now=now)==0 # ACK not confirmed
    storage.mark_reply_send_ack_confirmed(ctx['reply_action_id'])
    runner.binding.run_status='faulted';assert lifecycle.cleanup(runner,now=now)==0
    runner.binding.run_status='running'
    monkeypatch.setattr(bridge,'sidecar_active',lambda:True);assert lifecycle.cleanup(runner,now=now)==0
    monkeypatch.setattr(bridge,'sidecar_active',lambda:False)
    assert lifecycle.retains_files(storage.load_reply_send_ack_outbox(ctx['reply_action_id']))
    assert lifecycle.cleanup(runner,now=now)==1 and not Path(ref['path']).exists()
    assert not lifecycle.retains_files(storage.load_reply_send_ack_outbox(ctx['reply_action_id']))
    assert storage.load_reply_send_ack_outbox(ctx['reply_action_id'])['ack_payload']==payload


def test_missing_component_is_prepare_failure_without_creation(original,monkeypatch):
    bridge,path,ctx=original
    bridge.sidecar_script=path.parent/'missing-sidecar.py'
    monkeypatch.setattr(rpa_bridge.subprocess,'Popen',lambda *a,**kw:pytest.fail('must not create process'))
    value=bridge.send_reply(target='CJTEST01',rpa_session_key='',text=TEXT,task_id=ctx['task_id'],
        reply_action_id=ctx['reply_action_id'],expected_context_guard={'original':'complete'})
    assert value['pre_send_setup_failure']['process_state']=='not_called'
    assert launch.proof(path,contract=c2_contract_v3())==value['pre_send_setup_failure']


def test_original_incident_reconstruction_through_formal_file(original,monkeypatch):
    """Recorded OCR/raw PNG -> formal parser reconstruction, not native Windows."""
    import os
    configured=os.environ.get('CHEJIN_SP_GUARD_FIXTURE')
    if not configured:pytest.skip('requires the private original incident guard reconstruction')
    source=Path(configured)
    guard=json.loads(source.read_text())
    old=['--expected-context-guard',json.dumps(guard,ensure_ascii=True,sort_keys=True,separators=(',',':'))]
    assert files.command_units(old)-1==42262
    with pytest.raises(ValueError):files.validate_command(old)
    bridge,path,ctx=original;received=[];commands=[]
    class Receiver:
        def __init__(self,cmd,**kw):
            self.args=cmd;self.pid=999999;self.returncode=0;commands.append(cmd)
            def value(flag):return cmd[cmd.index(flag)+1]
            args=Namespace(action='send',expected_context_guard='',action_journal=value('--action-journal'),
                target=value('--target'),text=value('--text'),expected_context_guard_file=value('--expected-context-guard-file'),
                expected_context_guard_sha256=value('--expected-context-guard-sha256'),send_task_id=value('--send-task-id'),send_action_id=value('--send-action-id'))
            same,rejected=admit(args);assert rejected is None
            received.append(same)
            # No physical UI is requested in this transport-only test.
        def communicate(self,timeout=None):
            return json.dumps({'ok':False,'error_code':'CONTROLLED_TRANSPORT_ONLY','action_phase':'not_attempted','physical_send_triggered':False}),''
    monkeypatch.setattr(rpa_bridge.subprocess,'Popen',Receiver)
    bridge.send_reply(target='CJTEST01',rpa_session_key='',text=TEXT,task_id=ctx['task_id'],reply_action_id=ctx['reply_action_id'],expected_context_guard=guard)
    assert received==[guard] and len(commands)==1 and '--expected-context-guard' not in commands[0]
    assert files.command_units(commands[0])<32767
    output=path.parent/'original-guard-transport-evidence.json'
    output.write_text(json.dumps({'source':str(source),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'old_argument_utf16_units':42262,'new_complete_command_utf16_units_including_null':files.command_units(commands[0]),
        'guard_equal':True,'native_windows':False},indent=2))
