"""Coordinator -> independent updater -> real SQLite -> production startup verifier.

Only local script/GUI-health actors replace frozen EXEs; never counts as Windows UAT.
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from chejin_worker_client import __version__, storage
from chejin_worker_client.models import Binding, ClientRelease
from chejin_worker_client.release_package_contract import canonical_release_manifest
from chejin_worker_client.update_data_snapshot import load_data_baseline


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX source actors; use the formal Windows EXE process gate on Windows')
@pytest.mark.parametrize('damage',[None,'binding','stale-before-exit','init-insert','init-delete','init-compatible-column','shutdown-timeout'])
def test_live_old_final_writes_are_captured_after_exit_and_tamper_rejected(tmp_path, monkeypatch, damage):
    source=Path(__file__).resolve().parents[1]
    current=tmp_path/'program'/'current'; staged=tmp_path/'staging'/'client'
    update=tmp_path/'update'; request=update/'requests'/'update-process-evidence';control=request/'control'
    data=tmp_path/'data'
    for p in [current,staged,control,data]:p.mkdir(parents=True)
    monkeypatch.setattr(storage,'APP_DIR',data);monkeypatch.setattr(storage,'DB_FILE',data/'worker_client.sqlite3')
    storage.save_binding(Binding('synthetic-worker','sentinel-private-key','synthetic-instance',run_status='paused'))
    storage.save_accept_schedule(enabled=False, start='09:00', end='17:00')
    storage.set_update_new_work_gate(True,update_request_id='update-process-evidence')
    actor=(source/'tests/fixtures/update_handoff/worker_actor.py').read_text()
    updater="""import sys,os
sys.path.insert(0,os.environ['HANDOFF_TEST_SOURCE'])
if os.environ.get('HANDOFF_TEST_DAMAGE') == 'stale-before-exit':
    # Ablation: use a REAL SQLite snapshot taken while the old writer still runs.
    # Only collection timing is deliberately broken; no comparison/health success is mocked.
    import json,time
    from pathlib import Path
    from chejin_worker_client import update_data_snapshot as snapshots
    token=sys.argv[sys.argv.index('--token')+1]
    stale=snapshots.protected_update_snapshot(digest_key=token)
    Path(sys.argv[sys.argv.index('--plan')+1]).with_name('ablation-early-read.json').write_text(json.dumps({'captured_at':time.time()}))
    snapshots.protected_update_snapshot=lambda **kwargs: stale
from chejin_worker_client.chejin_updater import main
raise SystemExit(main())
"""
    for root in [current,staged]:
        for name,body in [('CheJinWorkerClient.exe',actor),('CheJinUpdater.exe',updater)]:
            p=root/name;p.write_text('#!'+sys.executable+'\n'+body);p.chmod(0o755)
    manifest={'schema_version':1,'version':__version__,'platform':'windows-x64','git_commit':'b'*40,'rollback_safe':True,
              'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in staged.iterdir()}}
    manifest_path=staged/'update-package-manifest.json';manifest_path.write_text(json.dumps(manifest))
    archive=request/'client.zip'
    with zipfile.ZipFile(archive,'w') as z:
        for p in staged.iterdir():z.write(p,arcname='client/'+p.name)
    private=Ed25519PrivateKey.generate();keys=control/'test-public-keys.json'
    keys.write_text(json.dumps({'schema_version':1,'keys':[{'key_id':'test','algorithm':'ed25519','public_key_base64':base64.b64encode(private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()}]}))
    release=ClientRelease(True,__version__,'gray','windows-x64','https://example.test/client.zip',archive.stat().st_size,
        hashlib.sha256(archive.read_bytes()).hexdigest(),None,'test','b'*40,hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        '2026-09-07T00:00:00+00:00','synthetic', '0.9.59',True)
    release=ClientRelease(**{**release.__dict__,'manifest_signature':base64.b64encode(private.sign(canonical_release_manifest(release))).decode()})
    boundary={'safe':True,'new_work_blocked':True,'backend_stopped_confirmed_or_unbound':True,'confirmed_run_status':'paused',
        'current_task':None,'inflight_flow_id':None,'task_lease_active':False,'ui_lock_active':False,'sidecar_active':False,
        'waiting_ledger':0,'pending_c2_outbox':0,'pending_sqlite_action_journal':0,'pending_file_action_journal':0,
        'pending_sent_ack':0,'action_journal_state_unavailable':0}
    config={'release':release.__dict__,'state':{'state':'waiting_for_safe_boundary','update_request_id':'update-process-evidence',
        'package_root':str(staged),'archive_path':str(archive),'pre_update_run_status':'paused'},'control':str(control),
        'events':str(tmp_path/'events.jsonl'),'update_root':str(update),'current':str(current),'request_root':str(request),'boundary':boundary}
    config_path=tmp_path/'fixture-config.json';config_path.write_text(json.dumps(config))
    env={**os.environ,'CHEJIN_WORKER_HOME':str(data),'CHEJIN_RELEASE_SIGNING_KEYS_PATH':str(keys),
        'HANDOFF_TEST_SOURCE':str(source),'HANDOFF_TEST_CONFIG':str(config_path),'HANDOFF_TEST_DAMAGE':damage or ''}
    old=subprocess.Popen([str(current/'CheJinWorkerClient.exe')],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    stdout,stderr=old.communicate(timeout=30)
    assert old.returncode==0,stderr
    deadline=time.monotonic()+20
    result_path=control/'update-result.json'
    while not result_path.exists() and time.monotonic()<deadline:time.sleep(.1)
    assert result_path.exists()
    result=json.loads(result_path.read_text())
    plan=json.loads((control/'update-plan.json').read_text())
    assert plan['schema_version']==2 and 'protected_data_snapshot' not in plan
    if damage == 'shutdown-timeout':
        assert result['state']=='failed'
        assert not (control/'protected-data-baseline.json').exists()
        assert not (control/'healthy.json').exists()
        ui_result=json.loads((control/'shutdown-ui-result.json').read_text())
        assert ui_result['state']['result_reconciled'] and not ui_result['state']['in_progress']
        assert ui_result['slow_writer_stopped'] and ui_result['launch_count']==1
        assert ui_result['binding_status']=='faulted' and not ui_result['gate_blocked']
        return
    baseline=json.loads((control/'protected-data-baseline.json').read_text())
    assert baseline['payload']['captured_after_old_exit'] is True
    events=[json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    phases=[json.loads(line) for line in (control/'updater-startup.jsonl').read_text().splitlines()]
    times={item['phase']:item['timestamp_epoch'] for item in phases}
    assert events[-1]['phase']=='final_exit_commit'
    assert events[-1]['time'] <= times['old_writers_exited'] <= times['data_exclusive_acquired'] <= times['data_baseline_captured']
    if damage and damage != 'init-compatible-column':
        assert result['state']=='rolled_back' and result['data_integrity_failed'] is True
        failure=json.loads((control/'worker-startup.jsonl').read_text().splitlines()[-1])
        assert failure['error_code']=='UPDATE_PROTECTED_DATABASE_CHANGED'
        if damage == 'binding':
            assert failure['differences'][0]['fields']=={'worker_token':1}
        elif damage == 'stale-before-exit':
            early=json.loads((control/'ablation-early-read.json').read_text())
            assert early['captured_at'] < events[-1]['time']
            assert failure['differences'][0]['table']=='client_settings'
            assert failure['differences'][0]['fields']['value']==1
        elif damage == 'init-insert':
            assert failure['differences'][0]['table']=='client_settings' and failure['differences'][0]['added']==1
        elif damage == 'init-delete':
            assert failure['differences'][0]['table']=='binding' and failure['differences'][0]['missing']==1
        assert not (control/'healthy.json').exists()
    else:
        assert result['state']=='succeeded',result
        verified=json.loads((control/'verified-data.json').read_text())
        assert verified['schedule']=={'enabled':True,'start':'11:00','end':'19:00'}
    assert 'sentinel-private-key' not in (control/'protected-data-baseline.json').read_text()
    assert 'synthetic-tamper' not in result_path.read_text()
