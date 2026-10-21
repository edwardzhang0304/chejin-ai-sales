"""Cutover failure must retain the fence and never restore a database."""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import recovery_cutover as cutover


def inputs(tmp_path):
    backup=tmp_path/'backup.json';backup.write_text(json.dumps({'protected':{'original':'unchanged'}}))
    plan={'previous_contract':'old','previous_contract_sha256':'a'*64,'target_image':'candidate',
          'source_dir':str(tmp_path),'retained_flow_sha256':{'synthetic':'b'*64},
          'evidence':{'backup':{'path':str(backup)}}}
    p=tmp_path/'plan.json';p.write_text(json.dumps(plan))
    a=tmp_path/'approval.json';a.write_text('{}')
    return ['recovery_cutover.py','--plan',str(p),'--approval',str(a)]


def test_check_only_has_no_mutation(tmp_path):
    before={'cutover_safe':True,'ordinary_readiness':{'ready':False},'protected':{'original':'unchanged'}}
    with patch.object(sys,'argv',inputs(tmp_path)),patch.object(cutover,'validate'),patch.object(cutover,'api_check',return_value=before),patch.object(cutover,'maintenance_main') as maintenance,patch.object(cutover.subprocess,'run') as run:
        cutover.main()
        maintenance.assert_not_called();run.assert_not_called()
        assert not (tmp_path/'recovery-cutover').exists()


def test_failure_retains_fence_and_original_facts(tmp_path):
    before={'cutover_safe':True,'ordinary_readiness':{'ready':False},'protected':{'original':'unchanged'}}
    snippet=tmp_path/'snippet';snippet.write_text(cutover.BLOCK)
    site=tmp_path/'site';site.write_text(cutover.INCLUDE)
    with patch.object(sys,'argv',inputs(tmp_path)+['--execute']),patch.object(cutover,'validate'),patch.object(cutover,'api_check',return_value=before),patch.object(cutover,'maintenance_main'),patch.object(cutover,'verify_live_fence',return_value={'status':'passed'}),patch.object(cutover,'SNIPPET',snippet),patch.object(cutover,'SITE',site),patch.object(cutover.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['docker','compose'])) as run:
        with pytest.raises(subprocess.CalledProcessError):cutover.main()
        assert snippet.read_text()==cutover.BLOCK
        assert run.call_count==1 and run.call_args.args[0][-5:]==['up','-d','--no-deps','--no-build','api']
        failure=json.loads((tmp_path/'recovery-cutover/failure.json').read_text())
        assert failure['maintenance']=='retained'
        assert not (tmp_path/'recovery-cutover/result.json').exists()


def test_changed_plan_fails_before_any_production_command(tmp_path):
    with patch.object(cutover.subprocess,'run') as run:
        with pytest.raises(AssertionError,match='APPROVAL_PLAN_MISMATCH'):
            cutover.validate({'version':'changed'},{'plan_sha256':'0'*64},tmp_path)
        run.assert_not_called()


@pytest.mark.parametrize('already_active', [False, True])
def test_complete_cutover_control_flow_with_explicit_host_adapters(tmp_path, monkeypatch, already_active):
    """Actual validation, maintenance CLI and file switch; Docker/HTTP/OS are adapters.

    This proves orchestration, not a real production container transition or
    Windows/WeChat recovery. The separate PostgreSQL/Nginx test covers recovery.
    """
    import hashlib
    import maintenance
    from contextlib import nullcontext
    root=tmp_path
    sites=root/'sites';sites.mkdir()
    site=sites/'main.conf';site.write_text('server {\n    location = /api {\n    }\n}\n')
    snippet=root/'maintenance.conf'
    if already_active:
        snippet.write_text(cutover.BLOCK)
        site.write_text(site.read_text().replace('    location = /api {', cutover.INCLUDE+'    location = /api {'))
    for module in (maintenance,cutover):
        monkeypatch.setattr(module,'SITE',site);monkeypatch.setattr(module,'SNIPPET',snippet)
    envfile=root/'production.env';envfile.write_text('SYNTHETIC=value\n')
    admin=root/'web';admin.mkdir();oldadmin=admin/'old';oldadmin.mkdir()
    link=admin/'jiangsuchejin';link.symlink_to(oldadmin)
    monkeypatch.setattr(cutover,'ADMIN_LINK',link);monkeypatch.setattr(cutover,'PRODUCTION_ENV',envfile)
    frontend=root/'candidate';frontend.mkdir();(frontend/'index.html').write_text('<p>synthetic</p>')
    source=root/'source';(source/'backend').mkdir(parents=True)
    compose=source/'backend/docker-compose.yml';compose.write_text('synthetic boundary input\n')
    (root/'artifact.zip').write_bytes(b'synthetic sealed artifact')
    dump=root/'database.dump';dump.write_bytes(b'synthetic restored backup')
    protected={'workers':{'count':2,'sha256':'c'*64},'message_events':{'count':3,'sha256':'d'*64}}
    plan={'version':'new','source_commit':'f'*40,'source_dir':str(source),
        'api_container':'chejin-leads-api','previous_image':'previous','previous_image_id':'old-id',
        'target_image':'candidate','target_image_id':'new-id','previous_contract':'old',
        'previous_contract_sha256':'a'*64,'contract_sha256':'b'*64,
        'retained_flow_sha256':{'synthetic':'e'*64},'production_env_sha256':cutover.sha(envfile),
        'previous_admin':str(oldadmin),'frontend_candidate':str(frontend),
        'frontend_files':{'index.html':cutover.sha(frontend/'index.html')},'frontend_api_base':'/api',
        'compose_sha256':cutover.sha(compose),'package_sha256':cutover.sha(root/'artifact.zip'),
        'stopped_writer_confirmation':{'all_workers_normally_exited':True,'original_data_preserved':True,
           'source_reference':'synthetic human receipt','confirmed_exited_at':'2020-01-02T00:00:00+00:00'},'evidence':{}}
    receipts={'windows_preservation':{'target_zip_sha256':plan['package_sha256']},
        'postgres_recovery':{},'compatibility':{'previous_image':'previous','target_image':'candidate',
            'schema_migration_change':False,'startup_code_change':False},
        'backup':{'restore_verified':True,'dump_path':str(dump),'dump_sha256':cutover.sha(dump),'protected':protected}}
    for key,value in receipts.items():
        path=root/(key+'.json');path.write_text(json.dumps({'status':'passed',**value}))
        plan['evidence'][key]={'path':str(path),'sha256':cutover.sha(path)}
    approval={'review_status':'approved','review_reference':'synthetic review',
        'plan_sha256':hashlib.sha256(json.dumps(plan,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        'operator_files':{f:cutover.sha(Path(cutover.__file__).with_name(f)) for f in ('maintenance.py','manual_readiness.py','recovery_cutover.py')}}
    p=root/'plan.json';p.write_text(json.dumps(plan));a=root/'approval.json';a.write_text(json.dumps(approval))
    state={'switched':False,'calls':[],'api_checks':0}
    def inspection():
        return [{'Image':'new-id' if state['switched'] else 'old-id',
            'Config':{'Image':'candidate' if state['switched'] else 'previous','Env':['SYNTHETIC=value']},
            'Mounts':[{'Type':'volume','Destination':'/data','Name':'data-volume'}],
            'HostConfig':{'PortBindings':{'8000/tcp':[{'HostIp':'127.0.0.1','HostPort':'8000'}]}}}]
    config={'services':{'api':{'environment':{'SYNTHETIC':'value'},'image':'candidate',
        'volumes':[{'type':'volume','target':'/data','source':'data'}],
        'ports':[{'host_ip':'127.0.0.1','published':'8000'}]}},'volumes':{'data':{'name':'data-volume'}}}
    def host_run(argv,**kwargs):
        state['calls'].append(argv)
        if argv[:2]==['docker','inspect']:out=json.dumps(inspection())
        elif argv[:3]==['docker','image','inspect']:out='new-id\n'
        elif argv[:2]==['docker','compose'] and 'config' in argv:out=json.dumps(config)
        elif argv[:2]==['docker','compose'] and 'up' in argv:
            assert snippet.read_text()==cutover.BLOCK
            assert state['api_checks']==2 and not state['switched']
            state['switched']=True;out=''
        elif argv[:2]==['docker','exec']:
            request=json.loads(argv[-1]);state['api_checks']+=1
            assert request['contract_revision']==('new' if state['switched'] else 'old')
            assert request['confirmed_exited_at']==plan['stopped_writer_confirmation']['confirmed_exited_at']
            assert request['approved_read_flows']==plan['retained_flow_sha256']
            out=json.dumps({'cutover_safe':True,'ordinary_readiness':{'ready':False},'protected':protected})
        elif argv[:2]==['curl','-q']:
            assert not state['switched'] and snippet.read_text()==cutover.BLOCK
            assert '--resolve' in argv and '--insecure' not in argv and '-k' not in argv
            out=json.dumps({'code':'RELEASE_MAINTENANCE'})+'\n503'
        elif argv==['ps','-eo','pid,args']:out=''
        elif argv in (['nginx','-t'],['systemctl','reload','nginx']):out=''
        else:raise AssertionError('Unexpected host operation '+repr(argv))
        return subprocess.CompletedProcess(argv,0,stdout=out,stderr='')
    monkeypatch.setattr(subprocess,'run',host_run)
    monkeypatch.setattr(subprocess,'check_output',lambda argv,**kw: json.dumps(inspection()).encode())
    class Ready: status=200
    monkeypatch.setattr(cutover.urllib.request,'urlopen',lambda *a,**kw:nullcontext(Ready()))
    monkeypatch.setattr(sys,'argv',['recovery_cutover.py','--plan',str(p),'--approval',str(a),'--execute'])
    cutover.main()
    result=json.loads((root/'recovery-cutover/result.json').read_text())
    assert result['status']=='awaiting_original_windows_recovery_and_browser'
    assert result['maintenance']=='active' and result['protected_data_unchanged']
    assert result['automatic_update_published'] is False and result['workers_resumed'] is False
    assert link.resolve()==admin/'jiangsuchejin-vnew-ffffffff'
    assert (link/'index.html').read_bytes()==(frontend/'index.html').read_bytes()
    assert snippet.read_text()==cutover.BLOCK and cutover.INCLUDE in site.read_text()
    assert state['api_checks']==3
    entry=json.loads((root/'recovery-cutover/maintenance-entry.json').read_text())
    assert entry['operation']=='enable' and entry['reused_existing_fence'] is already_active
    assert state['calls'].count(['systemctl','reload','nginx'])==1
    assert sum('up' in argv for argv in state['calls'])==1
    assert not any('disable' in argv or 'pg_restore' in argv for argv in state['calls'])


def test_invalid_existing_maintenance_stops_before_docker_switch(tmp_path):
    before={'cutover_safe':True,'ordinary_readiness':{'ready':False},'protected':{'original':'unchanged'}}
    snippet=tmp_path/'snippet';snippet.write_text(cutover.BLOCK)
    site=tmp_path/'site';site.write_text('missing maintenance include')
    def status():
        assert sys.argv[1]=='enable'
        raise AssertionError('MAINTENANCE_NOT_ENABLED')
    with patch.object(sys,'argv',inputs(tmp_path)+['--execute']),patch.object(cutover,'validate'),patch.object(cutover,'api_check',return_value=before),patch.object(cutover,'maintenance_main',side_effect=status),patch.object(cutover,'SNIPPET',snippet),patch.object(cutover,'SITE',site),patch.object(cutover.subprocess,'run') as run:
        with pytest.raises(AssertionError,match='MAINTENANCE_NOT_ENABLED'):cutover.main()
        run.assert_not_called()
        assert snippet.read_text()==cutover.BLOCK


@pytest.mark.parametrize('response', ['{"code":"RELEASE_MAINTENANCE"}\n200','{"code":"OTHER"}\n503','Unauthorized\n401'])
def test_live_fence_probe_rejects_disk_only_or_wrong_response(response):
    with patch.object(cutover,'command',return_value=subprocess.CompletedProcess([],0,stdout=response)):
        with pytest.raises((AssertionError,ValueError)):cutover.verify_live_fence()


def test_live_probe_checks_each_grant_route_on_loopback_with_tls_and_no_credentials():
    with patch.object(cutover,'command',return_value=subprocess.CompletedProcess([],0,stdout='{"code":"RELEASE_MAINTENANCE"}\n503')) as command:
        receipt=cutover.verify_live_fence()
    assert len(receipt['probes'])==6 and command.call_count==6
    for call in command.call_args_list:
        argv=call.args[0]
        assert argv[:2]==['curl','-q'] and argv[argv.index('--resolve')+1]=='jiangsuchejin.com:443:127.0.0.1'
        assert argv[argv.index('--noproxy')+1]=='*'
        assert not any('Authorization' in arg or 'Cookie' in arg for arg in argv)
        assert '-k' not in argv and '--insecure' not in argv


def test_already_active_fence_with_undrained_old_worker_never_switches(tmp_path):
    import maintenance
    snippet=tmp_path/'snippet';snippet.write_text(cutover.BLOCK)
    site=tmp_path/'site';site.write_text(cutover.INCLUDE)
    before={'cutover_safe':True,'ordinary_readiness':{'ready':False},'protected':{'original':'unchanged'}}
    with patch.object(sys,'argv',inputs(tmp_path)+['--execute']),patch.object(cutover,'validate'),patch.object(cutover,'api_check',return_value=before),patch.object(cutover,'SNIPPET',snippet),patch.object(cutover,'SITE',site),patch.object(maintenance,'SNIPPET',snippet),patch.object(maintenance,'SITE',site),patch.object(maintenance,'validate_ingress'),patch.object(maintenance,'old_workers',return_value={42}),patch.object(maintenance.time,'monotonic',side_effect=[0,61]),patch.object(maintenance.subprocess,'run') as run,patch.object(cutover,'verify_live_fence',return_value={'status':'passed'}) as live:
        with pytest.raises(RuntimeError,match='freeze restored'):cutover.main()
    assert not any(c.args[0][0]=='docker' for c in run.call_args_list)
    live.assert_not_called()
    assert snippet.read_text()==cutover.BLOCK
    failure=json.loads((tmp_path/'recovery-cutover/failure.json').read_text())
    assert failure['phase']=='before_cutover' and failure['docker_switch_started'] is False
    assert failure['maintenance']=='retained'
