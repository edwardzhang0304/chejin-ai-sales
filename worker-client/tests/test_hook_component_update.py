"""Offline component transaction with synthetic DLLs; never loads native code."""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import pytest
from chejin_worker_client import storage,hook_component_update as update
from chejin_worker_client.hook_config import HookDeployment,DEPLOYMENT_KEY,VOICE_SETTING_KEY,INSTALLATION_KEY,TRANSPORT_KEY
from chejin_worker_client.models import Binding


def pe(tag):
    data=bytearray(256);data[:2]=b'MZ';struct.pack_into('<I',data,60,128)
    data[128:134]=b'PE\0\0\x4c\x01';return bytes(data)+tag


@pytest.fixture
def installation(tmp_path,monkeypatch):
    monkeypatch.setattr(storage,'APP_DIR',tmp_path/'data')
    monkeypatch.setattr(storage,'DB_FILE',storage.APP_DIR/'worker_client.sqlite3')
    target=tmp_path/'wechat'/'version.dll';target.parent.mkdir();target.write_bytes(pe(b'original'))
    candidate=tmp_path/'version (2).dll';candidate.write_bytes(pe(b'candidate'))
    monkeypatch.setattr(update,'OLD_HASH',hashlib.sha256(target.read_bytes()).hexdigest())
    monkeypatch.setattr(update,'NEW_HASH',hashlib.sha256(candidate.read_bytes()).hexdigest())
    monkeypatch.setattr(update,'assert_wechat_stopped',lambda:None)
    monkeypatch.setattr(HookDeployment,'dll_path',property(lambda self:str(target)))
    deployment=HookDeployment('original-revision',r'C:\Work WeChat\WeChat.exe',update.OLD_HASH,49883,49884)
    settings={INSTALLATION_KEY:{'schema_version':1,'api_base_url':'https://synthetic.example/api'},
              TRANSPORT_KEY:{'mode':'hook'},DEPLOYMENT_KEY:asdict(deployment),
              VOICE_SETTING_KEY:{'deployment_revision':deployment.revision,'disabled':True,'operator_confirmed':True},
              'other_setting':{'keep':'unchanged'}}
    storage.save_binding(Binding('synthetic-worker','synthetic-token','synthetic-instance','paused'))
    with storage.db_connection() as db:
        db.executemany('INSERT INTO client_settings(key,value,updated_at) VALUES(?,?,?)',[(k,json.dumps(v),'original-time') for k,v in settings.items()])
        db.execute('CREATE TABLE hook_active_calls (process_generation TEXT, action_id TEXT, kind TEXT, state TEXT, attempted_at TEXT, completed_at TEXT)')
        db.execute("INSERT INTO hook_active_calls VALUES('old-generation','original-action','Voice2Text','uncertain','old-time',NULL)")
        db.commit()
    pending=storage.APP_DIR/'transactions'/'hook'/'sends'/'original.json';pending.parent.mkdir(parents=True);pending.write_bytes(b'original unresolved send proof')
    activation=target.parent/'license.dat';activation.write_bytes(b'synthetic existing activation')
    return candidate,target,pending,activation


def snapshot():
    with storage.db_connection() as db:
        return {t:[tuple(r) for r in db.execute('SELECT * FROM '+t)] for t in ('binding','client_settings','hook_active_calls')}


def apply(candidate,**kwargs):return update.update_component(candidate,automatic_text_disabled=True,**kwargs)


def test_update_retry_and_rollback_preserve_identity_ports_activation_and_pending_proof(installation):
    candidate,target,pending,activation=installation;before=snapshot()
    result=apply(candidate)
    assert result['status']=='installed_pending_validation' and not result['automatic_start']
    assert result['launch']['arguments']==['StartPort=49883','ConnectType=http','RecvType=0']
    assert target.read_bytes()==candidate.read_bytes()
    after=snapshot()
    assert after['binding']==before['binding'] and after['hook_active_calls']==before['hook_active_calls']
    assert [r for r in after['client_settings'] if r[0] not in (DEPLOYMENT_KEY,VOICE_SETTING_KEY)]==[r for r in before['client_settings'] if r[0] not in (DEPLOYMENT_KEY,VOICE_SETTING_KEY)]
    assert apply(candidate)==result and snapshot()==after
    assert update.update_component(rollback=True,automatic_text_disabled=True)['status']=='rolled_back'
    assert snapshot()==before and update._hash(target)==update.OLD_HASH
    assert pending.read_bytes()==b'original unresolved send proof'
    assert activation.read_bytes()==b'synthetic existing activation'


@pytest.mark.parametrize('case',['candidate','installed','running','wechat','no_confirmation','voice_proof','wrong_port','backup'])
def test_refusal_never_overwrites_unrecognized_state(installation,monkeypatch,case):
    candidate,target,pending,activation=installation
    if case=='candidate':candidate.write_bytes(b'unapproved')
    if case=='installed':target.write_bytes(b'unknown DLL')
    if case=='running':storage.save_binding(Binding('synthetic-worker','synthetic-token','synthetic-instance','running'))
    if case=='wechat':monkeypatch.setattr(update,'assert_wechat_stopped',lambda:(_ for _ in ()).throw(ValueError('HOOK_COMPONENT_WECHAT_RUNNING')))
    if case in {'wrong_port','backup'}:
        apply(candidate)
        if case=='backup':(storage.APP_DIR/'transactions'/'hook'/'components'/update.NEW_HASH/'original-version.dll').write_bytes(b'corrupt')
        else:
            with storage.db_connection() as db:
                d=json.loads(db.execute('SELECT value FROM client_settings WHERE key=?',(DEPLOYMENT_KEY,)).fetchone()[0]);d['hook_api_port']=49000
                db.execute('UPDATE client_settings SET value=? WHERE key=?',(json.dumps(d),DEPLOYMENT_KEY));db.commit()
    if case=='voice_proof':
        with storage.db_connection() as db:db.execute('DELETE FROM client_settings WHERE key=?',(VOICE_SETTING_KEY,));db.commit()
    before=snapshot();file_before=target.read_bytes()
    with pytest.raises(ValueError):update.update_component(candidate,automatic_text_disabled=case!='no_confirmation')
    assert snapshot()==before and target.read_bytes()==file_before
    assert pending.exists() and activation.exists()


@pytest.mark.parametrize('failure',['commit','after_replace','after_commit'])
def test_interrupted_update_recovers_from_durable_original_state(installation,monkeypatch,failure):
    candidate,target,*_=installation;before=snapshot()
    original_connection=storage.db_connection;original_atomic=update._atomic
    if failure=='after_replace':
        def atomic(path,data):
            original_atomic(path,data)
            if path==target:raise SystemExit('simulated power loss')
        monkeypatch.setattr(update,'_atomic',atomic)
    else:
        class Connection:
            def __init__(self,db):self.db=db
            def __getattr__(self,key):return getattr(self.db,key)
            def commit(self):
                if failure=='after_commit':self.db.commit();raise SystemExit('simulated exit after durable commit')
                raise OSError('simulated commit failure')
        @contextmanager
        def connection():
            with original_connection() as db:yield Connection(db)
        monkeypatch.setattr(storage,'db_connection',connection)
    with pytest.raises((OSError,SystemExit)):apply(candidate)
    monkeypatch.setattr(storage,'db_connection',original_connection);monkeypatch.setattr(update,'_atomic',original_atomic)
    if failure=='commit':assert snapshot()==before and update._hash(target)==update.OLD_HASH
    assert apply(candidate)['status']=='installed_pending_validation'
    assert update._hash(target)==update.NEW_HASH
    assert snapshot()['binding']==before['binding']
    assert snapshot()['hook_active_calls']==before['hook_active_calls']
    update.update_component(rollback=True,automatic_text_disabled=True)
    assert snapshot()==before


def test_process_check_rejects_live_wechat_and_access_failure(monkeypatch):
    monkeypatch.setattr(update.sys,'platform','win32')
    monkeypatch.setattr(update.psutil,'process_iter',lambda fields:[SimpleNamespace(info={'name':'WeChat.exe'})])
    with pytest.raises(ValueError,match='WECHAT_RUNNING'):update.assert_wechat_stopped()
    def denied(fields):raise update.psutil.AccessDenied(1)
    monkeypatch.setattr(update.psutil,'process_iter',denied)
    with pytest.raises(ValueError,match='PROCESS_CHECK_FAILED'):update.assert_wechat_stopped()


def test_packaging_entries_use_guarded_cli_without_business_commands():
    root=Path(__file__).parents[1]
    script=(root/'packaging/update-hook-component.ps1').read_text(encoding='utf-8-sig')
    assert '--hook-component-update' in script and '--hook-component-rollback' in script
    assert 'Stop-Process' not in script and 'Remove-Item' not in script
    build=(root/'scripts/build-windows.ps1').read_text(encoding='utf-8-sig')
    for file in ('update-hook-component.ps1','Update-CheJin-Hook.cmd','Rollback-CheJin-Hook.cmd'):
        assert file in build
    assert (root/'packaging/update-hook-component.ps1').read_bytes().startswith(b'\xef\xbb\xbf')


def test_cli_keeps_single_instance_gate_before_any_component_write(installation,monkeypatch):
    from contextlib import ExitStack
    from chejin_worker_client import main
    candidate,target,*_=installation;before=target.read_bytes()
    monkeypatch.setattr(main.sys,'argv',['worker','--hook-component-update',str(candidate)])
    monkeypatch.setattr(main,'acquire_single_instance',lambda:(_ for _ in ()).throw(main.SingleInstanceAlreadyRunning()))
    monkeypatch.setattr(main,'notify_already_running',lambda:None)
    with ExitStack() as resources:assert main._main(resources)==2
    assert target.read_bytes()==before


def test_component_cli_cannot_join_an_authenticated_updater_child(installation,monkeypatch):
    from contextlib import ExitStack
    from chejin_worker_client import main
    candidate,target,*_=installation
    monkeypatch.setattr(main.sys,'argv',['worker','--hook-component-update',str(candidate),'--post-update-plan','plan.json'])
    with ExitStack() as resources,pytest.raises(SystemExit):main._main(resources)
    assert update._hash(target)==update.OLD_HASH


def test_rollback_interruption_can_be_retried_without_losing_original_proof(installation,monkeypatch):
    candidate,target,*_=installation;before=snapshot();apply(candidate)
    original=update._atomic
    def fail(path,data):
        original(path,data)
        if path==target:raise SystemExit('simulated interrupted rollback')
    monkeypatch.setattr(update,'_atomic',fail)
    with pytest.raises(SystemExit):update.update_component(rollback=True,automatic_text_disabled=True)
    monkeypatch.setattr(update,'_atomic',original)
    assert update.update_component(rollback=True,automatic_text_disabled=True)['status']=='rolled_back'
    assert snapshot()==before and update._hash(target)==update.OLD_HASH


def test_updated_deployment_still_requires_real_loaded_component_identity(installation):
    from chejin_worker_client.hook_config import load_hook_deployment
    from chejin_worker_client.hook_process import HookProcessSnapshot,HookProcessError
    from dataclasses import replace
    candidate,target,*_=installation;apply(candidate);deployment=load_hook_deployment()
    process=HookProcessSnapshot(123,1000.0,deployment.wechat_exe,'3.9.10.16',0x14c,0x14c,
        update.NEW_HASH,True,tuple(deployment.launch_arguments),(123,),1,False)
    process.validate(deployment)
    for changed in (replace(process,dll_sha256=update.OLD_HASH),replace(process,dll_loaded=False),
                    replace(process,command_arguments=()),replace(process,listener_owners=(456,))):
        with pytest.raises(HookProcessError):changed.validate(deployment)
    assert storage.load_binding().run_status=='paused'


def test_full_cli_owns_worker_and_data_guards_and_writes_diagnostic(installation,monkeypatch,tmp_path):
    from contextlib import ExitStack
    from chejin_worker_client import main,config,release_transport,update_data_access
    candidate,target,*_=installation;before=snapshot();events=[];report=tmp_path/'result.json'
    monkeypatch.setattr(main.sys,'argv',['worker','--hook-component-update',str(candidate),'--confirm-auto-text-disabled','--write-report',str(report)])
    monkeypatch.setattr(main,'acquire_single_instance',lambda:events.append('worker_lock') or SimpleNamespace(release=lambda:events.append('worker_unlock')))
    monkeypatch.setattr(update_data_access,'acquire_data_access',lambda home:events.append('data_lock') or SimpleNamespace(close=lambda:events.append('data_unlock')))
    monkeypatch.setattr(release_transport,'selected_release_channel',lambda home:'gray-hook')
    monkeypatch.setattr(config,'CONFIG',SimpleNamespace(app_dir=storage.APP_DIR))
    with ExitStack() as resources:assert main._main(resources)==0
    assert events==['worker_lock','data_lock','data_unlock','worker_unlock']
    assert json.loads(report.read_text())['status']=='installed_pending_validation'
    assert snapshot()['binding']==before['binding'] and snapshot()['hook_active_calls']==before['hook_active_calls']
