"""Installation mirror only; never invoke root installation or service deployment."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'ops/formal_release'))
import operator_bundle as ob
from test_hook_component_verification import package


def isolated(installed, script, *args, libraries=False):
    env={k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','CHEJIN_COMPONENT_PLAN'}}
    prelude='import sys;sys.path.insert(0,sys.argv[1]);'
    if libraries:
        # Resolve third-party modules without executing .pth/editable hooks.
        import sysconfig
        prelude += 'sys.path.append(' + repr(sysconfig.get_paths()['purelib']) + ');'
    prelude += "import importlib.util;assert importlib.util.find_spec('app') is None;"
    return subprocess.run([sys.executable,'-I','-S','-c', prelude+script,
                           str(installed),*map(str,args)],cwd=installed,env=env,capture_output=True,text=True)


def test_new_install_and_existing_upgrade_share_inventory_and_preserve_files(tmp_path):
    source=ROOT/'ops/formal_release'; installed=tmp_path/'installed'
    ob.install_modules(source,installed)
    assert {p.name for p in installed.iterdir()}==set(ob.MODULES)
    (installed/'public-keys.json').write_text('original trust')
    (installed/'baseline').mkdir();(installed/'baseline/protected').write_text('old baseline')
    (installed/'component_release.py').write_text('old module')
    ob.install_modules(source,installed)
    assert (installed/'public-keys.json').read_text()=='original trust'
    assert (installed/'baseline/protected').read_text()=='old baseline'
    result=isolated(installed,"import operator_bundle;operator_bundle.check_bundle(sys.argv[1]);print('imports complete')")
    assert result.returncode==0,result.stderr
    # The actual shell installer consumes exactly this same inventory/preflight.
    script=(source/'install.sh').read_text()
    assert 'modules=$(python3 "$bundle/operator_bundle.py" list)' in script
    assert 'for file in $modules; do install' in script


def test_incomplete_bundle_stops_before_existing_entry_replacement(tmp_path):
    bundle=tmp_path/'bundle'; target=tmp_path/'installed'; bundle.mkdir();target.mkdir()
    (target/'receiver.py').write_text('keep original')
    with pytest.raises(ValueError,match='BUNDLE_INCOMPLETE'):ob.install_modules(bundle,target)
    assert (target/'receiver.py').read_text()=='keep original'


def test_installed_old_maintenance_and_admin_entrypoints_without_repo(tmp_path):
    installed=tmp_path/'installed'; ob.install_modules(ROOT/'ops/formal_release',installed)
    script="""
import json
from pathlib import Path
import maintenance, build_admin
root=Path(sys.argv[1]); maintenance.SNIPPET=root/'snippet';maintenance.SNIPPET.write_text(maintenance.BLOCK)
maintenance.SITE=root/'site';maintenance.SITE.write_text(maintenance.INCLUDE)
maintenance.validate_ingress=lambda:None
sys.argv=['maintenance','status','--evidence-dir',str(root/'evidence')];maintenance.main()
front=root/'source/frontend';(front/'dist/assets').mkdir(parents=True)
(front/'package-lock.json').write_text('{}');(front/'dist/index.html').write_text('fixture')
(front/'dist/assets/app.js').write_text('const config={VITE_API_BASE_URL:"/api"};')
build_admin.subprocess.run=lambda *a,**k:None
sys.argv=['build_admin','--environment','production','--api-base','/api','--source-root',str(front.parent),'--output',str(root/'admin.json')]
build_admin.main();assert json.loads((root/'admin.json').read_text())['browser_gate']=='pending'
"""
    result=isolated(installed,script)
    assert result.returncode==0,result.stderr
    assert 'MAINTENANCE' not in result.stderr and 'BROWSER_GATE_PENDING' in result.stdout


def test_installed_component_publish_valid_and_invalid_package(package,tmp_path):
    folder,approval,config,seal=package;seal()
    installed=tmp_path/'installed';ob.install_modules(ROOT/'ops/formal_release',installed)
    configpath=tmp_path/'config.json';configpath.write_text(json.dumps(config))
    script="""
import json
from pathlib import Path
import publish_hook,maintenance
assert Path(publish_hook.__file__).parent==Path(sys.argv[1])
maintenance.validate_ingress=lambda:None
publish_hook.trusted_path=lambda p:Path(p)
def transport(args,**kw):
    if 'inspect' in args:return 'healthy'
    if '-' in args:return '{"ok":true,"publication":"published"}'
    return ''
publish_hook.run_fixed=transport
result=publish_hook.publish(Path(sys.argv[2]),json.loads(Path(sys.argv[3]).read_text()));assert result['publication']=='published'
"""
    result=isolated(installed,script,folder,configpath,libraries=True)
    assert result.returncode==0,result.stderr
    seal(lambda f:f.pop('CheJinUpdater.exe'))
    result=isolated(installed,script,folder,configpath,libraries=True)
    assert result.returncode!=0 and 'ClientUpdateError' in result.stderr


def test_new_and_existing_config_migration_keeps_trust_and_validates_baselines():
    original={'staging_root':'/var/lib/chejin-formal-staging', 'client_baselines':{'0.10.13':'/opt/baseline/0.10.13'},
              'public_keys':'/opt/original-keys','capacity':{'protected_versions':['0.10.13']},'custom':'preserve'}
    fresh=ob.merged_config(original,{})
    assert fresh['client_baseline_identities']=={} # no manufactured Hook acceptance
    identity={'version':'0.10.13','zip_sha256':'a'*64,'exe_sha256':'b'*64,'updater_sha256':'c'*64}
    migrated=ob.merged_config(fresh,{'0.10.13':identity})
    assert all(migrated[k]==v for k,v in original.items())
    assert ob.merged_config(migrated,{'0.10.13':identity})==migrated
    for bad in ({**identity,'zip_sha256':'d'*64},{**identity,'exe_sha256':'bad'}):
        with pytest.raises(ValueError):ob.merged_config(migrated,{'0.10.13':bad})
    with pytest.raises(ValueError,match='NOT_INSTALLED'):ob.merged_config(fresh,{'9.0.0':identity})
    with pytest.raises(ValueError,match='SEPARATE'):
        ob.merged_config({**fresh,'component_approvals':original['staging_root']},{})



def test_existing_upgrade_entry_migrates_config_and_keeps_backup(tmp_path,monkeypatch):
    import verify_hook
    monkeypatch.setattr(verify_hook,'trusted_path',lambda p:Path(p)) # simulate ownership only in temp paths
    bundle=ROOT/'ops/formal_release'; installed=tmp_path/'installed';installed.mkdir()
    (installed/'public-keys.json').write_text('original keys')
    configpath=tmp_path/'config.json'; approvals=tmp_path/'approvals'
    original={'staging_root':str(tmp_path/'staging'),'component_approvals':str(approvals),
              'client_baselines':{'0.10.13':'/opt/original-baseline'},'public_keys':str(installed/'public-keys.json'),
              'capacity':{'protected_versions':['0.10.13']},'preserved':{'unknown':'field'}}
    configpath.write_text(json.dumps(original));original_bytes=configpath.read_bytes()
    identity={'0.10.13':{'version':'0.10.13','zip_sha256':'a'*64,'exe_sha256':'b'*64,'updater_sha256':'c'*64}}
    path=tmp_path/'identities.json';path.write_text(json.dumps(identity))
    ob.upgrade_tools(bundle,installed,configpath,path)
    updated=json.loads(configpath.read_text())
    assert updated==ob.merged_config(original,identity)
    assert configpath.with_suffix('.json.pre-component').read_bytes()==original_bytes
    assert approvals.is_dir() and (installed/'public-keys.json').read_text()=='original keys'
    ob.upgrade_tools(bundle,installed,configpath,path)
    assert configpath.with_suffix('.json.pre-component').read_bytes()==original_bytes
    identity['0.10.13']['zip_sha256']='d'*64;path.write_text(json.dumps(identity))
    with pytest.raises(ValueError,match='IMMUTABLE'):ob.upgrade_tools(bundle,installed,configpath,path)
    assert json.loads(configpath.read_text())==updated
