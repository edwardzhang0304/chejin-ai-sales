"""All-worker maintenance cutover. No build, DB reset, update publication or resume.

Run as the production operator only after review of this tool, the bound plan
and missing acceptance evidence. Normal release readiness is NOT overridden.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request

from maintenance import BLOCK, INCLUDE, SNIPPET, SITE, validate_ingress, main as maintenance_main

PRODUCTION_ENV = Path('/etc/chejin/production.env')
ADMIN_LINK = Path('/var/www/jiangsuchejin')


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def command(argv, **kwargs):
    return subprocess.run(argv,check=True,capture_output=True,**kwargs)


def api_check(plan, revision, digest):
    request={'purpose':'maintenance_cutover','contract_revision':revision,'contract_sha256':digest,
             'approved_read_flows':plan['retained_flow_sha256'],
             'confirmed_exited_at':plan['stopped_writer_confirmation']['confirmed_exited_at']}
    code=Path(__file__).with_name('manual_readiness.py').read_text()
    value=command(['docker','exec',plan['api_container'],'python','-c',code,json.dumps(request)],text=True)
    return json.loads(value.stdout)


def validate(plan, approval, stage):
    canonical=json.dumps(plan,sort_keys=True,separators=(',',':')).encode()
    assert approval['plan_sha256']==hashlib.sha256(canonical).hexdigest(), 'APPROVAL_PLAN_MISMATCH'
    assert approval['review_status']=='approved' and approval['review_reference'], 'REVIEW_REQUIRED'
    assert plan['stopped_writer_confirmation']['all_workers_normally_exited'] is True
    assert plan['stopped_writer_confirmation']['original_data_preserved'] is True
    assert plan['stopped_writer_confirmation']['source_reference'], 'WRITER_CONFIRMATION_REQUIRED'
    for filename,expected in approval['operator_files'].items():
        assert '/' not in filename and sha(Path(__file__).with_name(filename))==expected, 'OPERATOR_CODE_CHANGED'
    assert {'maintenance.py','manual_readiness.py','recovery_cutover.py'} <= set(approval['operator_files'])
    assert sha(stage/'artifact.zip')==plan['package_sha256'], 'SEALED_PACKAGE_CHANGED'
    for key in ('windows_preservation','postgres_recovery','compatibility','backup'):
        evidence=plan['evidence'][key]
        path=Path(evidence['path'])
        assert sha(path)==evidence['sha256'], 'EVIDENCE_CHANGED:'+key
        data=json.loads(path.read_text())
        assert data['status']=='passed', 'EVIDENCE_NOT_PASSED:'+key
        if key=='windows_preservation':assert data['target_zip_sha256']==plan['package_sha256']
        if key=='compatibility':
            assert data['previous_image']==plan['previous_image'] and data['target_image']==plan['target_image']
            assert data['schema_migration_change'] is False and data['startup_code_change'] is False
        if key=='backup':
            assert data['restore_verified'] is True and sha(data['dump_path'])==data['dump_sha256']
    assert sha(PRODUCTION_ENV)==plan['production_env_sha256'], 'ENV_CHANGED'
    current=json.loads(command(['docker','inspect',plan['api_container']],text=True).stdout)[0]
    assert current['Config']['Image']==plan['previous_image'] and current['Image']==plan['previous_image_id'], 'RUNNING_IMAGE_CHANGED'
    assert command(['docker','image','inspect',plan['target_image'],'--format','{{.Id}}'],text=True).stdout.strip()==plan['target_image_id'], 'TARGET_IMAGE_CHANGED'
    assert str(ADMIN_LINK.resolve())==plan['previous_admin'], 'ADMIN_CHANGED'
    frontend=Path(plan['frontend_candidate'])
    files={str(p.relative_to(frontend)):sha(p) for p in frontend.rglob('*') if p.is_file()}
    assert files==plan['frontend_files'], 'FRONTEND_CHANGED'
    assert plan['frontend_api_base']=='/api', 'PRODUCTION_FRONTEND_REQUIRED'
    assert sha(Path(plan['source_dir'])/'backend/docker-compose.yml')==plan['compose_sha256'], 'COMPOSE_CHANGED'
    env={**os.environ,'BACKEND_IMAGE':plan['target_image'],'BACKEND_ENV_FILE':'/etc/chejin/production.env'}
    config=json.loads(command(['docker','compose','--project-name','backend','--env-file','/etc/chejin/production.env',
        '-f',str(Path(plan['source_dir'])/'backend/docker-compose.yml'),'config','--format','json'],env=env,text=True).stdout)
    proposed=config['services']['api']
    running_env=dict(v.split('=',1) for v in current['Config']['Env'] if '=' in v)
    assert all(str(v)==running_env.get(k) for k,v in proposed['environment'].items()), 'API_ENVIRONMENT_CHANGED'
    running_mounts={m['Destination']:m['Name'] for m in current['Mounts'] if m['Type']=='volume'}
    proposed_mounts={m['target']:config['volumes'][m['source']]['name'] for m in proposed['volumes'] if m['type']=='volume'}
    assert running_mounts==proposed_mounts, 'DATA_VOLUMES_CHANGED'
    assert proposed['image']==plan['target_image'], 'COMPOSE_IMAGE_CHANGED'
    assert proposed['ports'][0]['host_ip']=='127.0.0.1' and str(proposed['ports'][0]['published'])=='8000', 'API_BIND_CHANGED'
    validate_ingress()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--approval',type=Path,required=True)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    plan=json.loads(args.plan.read_text());approval=json.loads(args.approval.read_text())
    stage=args.plan.parent
    validate(plan,approval,stage)
    before=api_check(plan,plan['previous_contract'],plan['previous_contract_sha256'])
    assert before['cutover_safe'] is True
    if not args.execute:
        print(json.dumps({'check':'passed','mutation':False,'ordinary_readiness':before['ordinary_readiness']}));return
    evidence=stage/'recovery-cutover';evidence.mkdir(exist_ok=False,mode=0o700)
    def record(name,value):(evidence/name).write_text(json.dumps(value,indent=2))
    import sys
    # A resumed release must keep the existing fence, never disable/re-enable it.
    already_active = SNIPPET.exists() and SNIPPET.read_text() == BLOCK
    operation = 'status' if already_active else 'enable'
    sys.argv=['maintenance.py',operation,'--evidence-dir',str(evidence)]
    maintenance_main()
    record('maintenance-entry.json', {'operation': operation, 'reused_existing_fence': already_active})
    assert SNIPPET.read_text()==BLOCK and INCLUDE in SITE.read_text()
    locked=api_check(plan,plan['previous_contract'],plan['previous_contract_sha256'])
    assert before['protected']==locked['protected'], 'STATE_CHANGED_BEFORE_CUTOVER'
    backup=json.loads(Path(plan['evidence']['backup']['path']).read_text())
    assert backup['protected']==locked['protected'], 'BACKUP_STATE_CHANGED'
    record('before.json',locked)
    env={**os.environ,'BACKEND_IMAGE':plan['target_image'],'BACKEND_ENV_FILE':'/etc/chejin/production.env'}
    argv=['docker','compose','--project-name','backend','--env-file','/etc/chejin/production.env',
          '-f',str(Path(plan['source_dir'])/'backend/docker-compose.yml'),'up','-d','--no-deps','--no-build','api']
    try:
        with (evidence/'compose.log').open('w') as log:
            subprocess.run(argv,env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=120)
        until=time.monotonic()+90
        while True:
            try:
                with urllib.request.urlopen('http://127.0.0.1:8000/readyz',timeout=2) as response:
                    assert response.status==200
                break
            except Exception:
                if time.monotonic()>until:raise RuntimeError('NEW_API_NOT_READY')
                time.sleep(1)
        after=api_check(plan,plan['version'],plan['contract_sha256'])
        record('after.json',after)
        assert before['protected']==after['protected'], 'PROTECTED_STATE_CHANGED_DURING_CUTOVER'
        running=json.loads(command(['docker','inspect',plan['api_container']],text=True).stdout)[0]
        assert running['Image']==plan['target_image_id'] and running['Config']['Image']==plan['target_image'], 'SWITCHED_IMAGE_MISMATCH'
        assert sha(PRODUCTION_ENV)==plan['production_env_sha256'], 'ENV_CHANGED'
        destination=ADMIN_LINK.parent/('jiangsuchejin-v'+plan['version']+'-'+plan['source_commit'][:8])
        assert not destination.exists(), 'ADMIN_DESTINATION_EXISTS'
        shutil.copytree(plan['frontend_candidate'],destination)
        link=ADMIN_LINK.with_name(ADMIN_LINK.name+'.next')
        assert not link.exists() and not link.is_symlink()
        link.symlink_to(destination);os.replace(link,ADMIN_LINK)
        result={'status':'awaiting_original_windows_recovery_and_browser','version':plan['version'],
                'package_sha256':plan['package_sha256'],'protected_data_unchanged':True,
                'ordinary_readiness':after['ordinary_readiness'],'maintenance':'active',
                'automatic_update_published':False,'workers_resumed':False,'database_recreated':False}
        record('result.json',result);print(json.dumps(result))
    except Exception as exc:
        # Never restore a database or downgrade blindly after new recovery facts.
        record('failure.json',{'type':type(exc).__name__,'maintenance':'retained',
                              'next':'inspect current facts; application rollback requires compatibility review; never overwrite DB'})
        raise


if __name__=='__main__':main()
