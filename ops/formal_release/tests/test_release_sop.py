"""Release SOP failures: parameter pollution, stale evidence, partial success and route isolation."""
import copy
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
import pytest
import yaml
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT/'ops/formal_release'))
import release_plan as rp
import acceptance_cases as ac
import candidate


def plan():
    return {'schema_version': 1, 'version': '2.4.9', 'source_commit': 'a'*40,
            'old_client': {'version': '2.4.8', 'source_commit': 'b'*40, 'run_id': '12', 'artifact_id': '34',
                           'zip_sha256': 'c'*64, 'exe_sha256': 'd'*64, 'updater_sha256': 'e'*64},
            'route': 'manual', 'recovery': 'none', 'scope_reason': 'Reviewed ordinary installation',
            'source_evidence_run_id': '56', 'tool_evidence_run_id': '78', 'case_evidence_runs': []}


def proof(p):
    return {'version': p['version'], 'build_commit': p['source_commit'],
            'files': {f"chejin-worker-v{p['version']}-windows-x64.zip": 'f'*64}}


def report(p, state):
    return {'status':'passed','current_version':p['old_client']['version'],'target_version':p['version'],
            'target_commit':p['source_commit'],'target_zip_sha256':'f'*64,'old_exe_sha256':'d'*64,
            'old_updater_sha256':'e'*64,'initial_run_status':state,'mode':'preserve_data_manual_install',
            'real_settings_button_clicked':False,'original_updater_used':False, **{k:True for k in
            ('original_worker_exited','protected_data_preserved','target_ui_confirmed','paused_intent_and_idle_gate_preserved',
             'normal_close_used','target_program_manifest_verified','original_data_directory_reused')}}


@pytest.mark.parametrize('old,new', [('0.9.77','0.9.78'),('1.9.9','2.0.0'),('12.3.4','12.3.5')])
def test_releases_change_data_not_code(old,new):
    p=plan();p['old_client']['version']=old;p['version']=new
    assert rp.validate(p) is p
    assert rp.cases(p)==['manual_paused','manual_faulted']


@pytest.mark.parametrize('key,value',[('version','2.4.9;echo bad'),('source_commit','HEAD'),('route','skip'),
    ('recovery','skip_all'),('source_evidence_run_id','$(bad)'),('case_evidence_runs',['12','12']),
    ('case_evidence_runs',['-1']),('scope_reason','')])
def test_invalid_parameters_fail_before_execution(key,value):
    p=plan();p[key]=value
    with pytest.raises(ValueError):rp.validate(p)


def test_secrets_and_arbitrary_commands_are_not_plan_fields():
    for key in ('api_key','ssh_command','skip_tests'):
        p=plan();p[key]='untrusted'
        with pytest.raises(ValueError):rp.validate(p)


def test_retired_button_start_is_data_policy_and_not_a_required_case():
    p=plan();p['old_client']['version']='0.9.75'
    assert rp.cases(p)==['manual_paused','manual_faulted']
    p['route']='button'
    with pytest.raises(ValueError,match='BUTTON_START_RETIRED'):rp.validate(p)


def test_pending_recovery_is_explicit_and_cannot_be_button_evidence():
    p=plan();p['recovery']='pending_read'
    assert rp.cases(p)[-1]=='pending_read'
    p['route']='button'
    with pytest.raises(ValueError,match='BUTTON_PENDING'):rp.validate(p)


def test_case_resume_preserves_passed_case_when_later_case_fails():
    p=plan();pr=proof(p);saved=[];calls=[];ids={'manual_paused':'one','manual_faulted':'two'}
    def execute(name):
        calls.append(name)
        if name=='manual_faulted':raise RuntimeError('controlled tool failure')
        return report(p,'paused')
    with pytest.raises(RuntimeError):ac.run_cases(rp.cases(p),ids,[],p,pr,execute,lambda x:saved.append(copy.deepcopy(x)))
    assert saved[-1]['manual_paused']['status']=='passed'
    assert saved[-1]['manual_faulted']['status']=='failed'
    bundle={'cases':saved[-1],'producer':{'run_id':'123'}};calls.clear()
    def retry(name):calls.append(name);return report(p,'faulted')
    results=ac.run_cases(rp.cases(p),ids,[bundle],p,pr,retry,lambda x:None)
    assert calls==['manual_faulted']
    assert results['manual_paused']['reused_from']=={'run_id':'123'}


def test_stale_or_failed_case_is_not_reused_but_tampering_blocks():
    p=plan();r=report(p,'paused');entry={'status':'passed','identity':'same','report':r,'report_sha256':ac.sha_json(r)}
    b={'cases':{'manual_paused':entry},'producer':{'run_id':'123'}}
    assert ac.reusable([b],'manual_paused','changed',p,proof(p)) is None
    entry['status']='failed'
    assert ac.reusable([b],'manual_paused','same',p,proof(p)) is None
    entry['status']='passed';r['protected_data_preserved']=False
    with pytest.raises(ValueError,match='TAMPERED'):ac.reusable([b],'manual_paused','same',p,proof(p))
    entry['report_sha256']=ac.sha_json(r)
    with pytest.raises(ValueError,match='PROTECTION'):ac.reusable([b],'manual_paused','same',p,proof(p))


def test_original_package_tool_or_environment_change_invalidates_case():
    p=plan();pr=proof(p);env={'system':'Windows','image_version':'one'}
    with patch.object(ac,'git',return_value=b'tool'),patch.object(ac,'source_inputs',return_value='runtime'):
        expected=ac.case_identity(p,pr,'manual_paused','a'*40,env)
        changed=copy.deepcopy(p);changed['old_client']['exe_sha256']='0'*64
        assert ac.case_identity(changed,pr,'manual_paused','a'*40,env)!=expected
        assert ac.case_identity(p,pr,'manual_paused','a'*40,dict(env,image_version='two'))!=expected
        with patch.object(ac,'git',return_value=b'changed tool'):
            assert ac.case_identity(p,pr,'manual_paused','a'*40,env)!=expected


def test_a_failed_run_can_supply_passed_cases_but_foreign_workflow_cannot():
    run={'id':123,'run_attempt':1,'head_sha':'a'*40,'path':ac.FORMAL,'event':'workflow_dispatch',
         'head_branch':ac.BRANCH,'repository':{'full_name':ac.REPO},'status':'completed','conclusion':'failure'}
    jobs=[{'name':'Accept exact Windows candidate','run_attempt':1,'status':'completed','conclusion':'failure'}]
    artifact={'name':'release-cases-123-1','expired':False}
    bundle={'schema_version':1,'producer':{'commit':'a'*40,'run_id':'123','attempt':'1'},'environment':{'system':'Windows'},'cases':{}}
    assert ac.trusted_bundle(run,jobs,artifact,bundle)==bundle
    for key,value in [('path','.github/workflows/worker-windows-fast-uat.yml'),('head_branch','unreviewed'),('repository',{'full_name':'other/repo'})]:
        with pytest.raises(ValueError):ac.trusted_bundle(dict(run,**{key:value}),jobs,artifact,bundle)


def test_manual_report_cannot_be_used_for_button_case():
    p=plan();p['route']='button'
    with pytest.raises(ValueError):ac.validate_case('button_paused',report(p,'paused'),p,proof(p))


def test_wrong_old_and_new_package_report_rejected():
    p=plan()
    for k in ('target_commit','target_zip_sha256','old_exe_sha256','old_updater_sha256'):
        r=report(p,'paused');r[k]='0'*len(r[k])
        with pytest.raises(ValueError):ac.validate_case('manual_paused',r,p,proof(p))


def test_active_workflow_has_no_fixed_release_version_or_run_ids():
    s=(ROOT/'.github/workflows/worker-windows-package.yml').read_text()
    assert '0.9.77' not in s and '0.9.75' not in s and '34615585004' not in s
    jobs=yaml.safe_load(s)['jobs']
    assert 'build-windows.ps1' not in '\n'.join(x.get('run','') for x in jobs['acceptance']['steps'])
    assert 'accept_candidate' not in jobs['package']['if']
    assert 'accept_candidate' not in jobs['deliver']['if']
    assert any(s.get('if')=='always()' and s.get('with',{}).get('name','').startswith('release-cases-') for s in jobs['acceptance']['steps'])


def test_complete_candidate_blocks_another_build_before_dispatch():
    p=plan()
    def api(path):
        if '/runs?' in path:return {'workflow_runs':[{'id':123}]}
        return {'jobs':[]} if '/jobs?' in path else {'artifacts':[]}
    with patch.dict(os.environ,DELIVERY_MODE='build_only',GITHUB_RUN_ID='456'),patch('source_evidence.api',side_effect=api),patch('candidate.select_candidate',return_value=(789,'a'*40)):
        with pytest.raises(ValueError,match='CANDIDATE_ALREADY_SAVED'):rp.prevent_duplicate_build(p)


def test_zip_accepts_windows_separators_but_rejects_escape_and_duplicate(tmp_path):
    import zipfile
    for index,names in enumerate([
        ['CheJinWorkerClient\\_internal\\','CheJinWorkerClient\\_internal\\fixture.txt'],
        ['CheJinWorkerClient/../../outside'],
        ['CheJinWorkerClient/a.txt','CheJinWorkerClient/A.txt']]):
        archive=tmp_path/f'{index}.zip'
        with zipfile.ZipFile(archive,'w') as z:
            for name in names:z.writestr(name,b'' if name.endswith('\\') else b'fixture')
        if index==0:assert (ac.extract(archive,tmp_path/str(index))/'_internal/fixture.txt').read_bytes()==b'fixture'
        else:
            with pytest.raises(ValueError):ac.extract(archive,tmp_path/str(index))


def test_inline_powershell_extracted_without_github_expressions(tmp_path):
    from workflow_scripts import extract
    paths=extract(ROOT/'.github/workflows/worker-windows-package.yml',tmp_path)
    assert paths and all('${{' not in p.read_text(encoding='utf-8-sig') for p in paths)
    assert any('Compress-Archive' in p.read_text(encoding='utf-8-sig') for p in paths)


def test_protocol_floor_is_policy_not_a_release_constant():
    p=plan();p['old_client']['version']='0.9.68'
    with pytest.raises(ValueError,match='UNSUPPORTED_UPGRADE_START'):rp.validate(p)


def test_legacy_receiver_rejected_before_build(tmp_path):
    import deliver
    with patch.object(sys,'argv',['deliver','preflight','--current-version','2.4.8','--result',str(tmp_path/'r.json')]), patch.object(deliver,'Remote') as remote:
        remote.return_value.return_value={'receiver_version':1}
        with pytest.raises(SystemExit):deliver.main()
        assert json.loads((tmp_path/'r.json').read_text())['error_code']=='RECEIVER_TOOL_UPDATE_REQUIRED'
