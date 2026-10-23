"""Real signatures, manifest/resources and publication consumer; no real Windows/production."""
import base64
import copy
import json
from pathlib import Path
import sys
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT/'ops/formal_release'), str(ROOT/'worker-client'), str(ROOT/'worker-client/omniauto-rpa')]
import component_release as cr
import publish_hook
import verify_hook as vh
from chejin_worker_client import __version__
from chejin_worker_client.models import ClientRelease
from chejin_worker_client.release_package_contract import canonical_release_manifest
from chejin_worker_client.pending_read_recovery import package_recovery_capability
from chejin_worker_client.c2_contract import c2_contract_v3


@pytest.fixture
def package(tmp_path, monkeypatch):
    # Reuse plan/report fixture only as an explicitly synthetic approval. All
    # actual bytes/signature/manifest/old decoder calls below are real.
    from test_component_release import approval_for
    before = cr.validate_source_versions(ROOT); after = {**before, 'worker':'0.10.14'}
    plan = {'schema_version':1, 'scope':'worker','stages':cr.STAGES['worker'],
            'contract_policy':'unchanged_exact','base_commit':'a'*40,'source_commit':'b'*40,
            'diff_sha256':'c'*64,'before':before,'after':after}
    approval, _ = approval_for(plan)
    folder = tmp_path/'package'; folder.mkdir(); approvals=tmp_path/'approvals'; approvals.mkdir()
    key=Ed25519PrivateKey.generate(); keys=tmp_path/'keys.json'
    keys.write_text(json.dumps({'keys':[{'key_id':'test','algorithm':'ed25519','public_key_base64':base64.b64encode(key.public_key().public_bytes_raw()).decode()}]}))
    (folder/'public-keys.json').write_bytes(keys.read_bytes())
    config={'component_approvals':str(approvals), 'container':'fixture-api', 'public_keys':str(keys),
            'client_baselines':{__version__:str(ROOT/'worker-client')},
            'client_baseline_identities':{__version__:dict(approval['old_client'])}}
    files={'CheJinWorkerClient.exe':b'fixture exe','CheJinUpdater.exe':b'fixture updater',
           '_internal/contracts/c2_contract_v3.json':cr.canonical(c2_contract_v3())}
    for rev,_ in __import__('chejin_worker_client.shared_rules',fromlist=['contract_rules']).contract_rules.RELEASED_READ_CONTRACTS:
        name='recovery/c2_contract_v3_'+rev+'.json'
        files['_internal/contracts/'+name]=(ROOT/'contracts'/name).read_bytes()
    def seal(transform=None, manifest_transform=None, descriptor_transform=None):
        content=copy.deepcopy(files)
        if transform: transform(content)
        manifest={'schema_version':1,'transport':'hook','channel':'gray-hook','version':'0.10.14',
                  'platform':'windows-x64','git_commit':'b'*40,'rollback_safe':True,
                  'files':{n:cr.sha(raw) for n,raw in content.items()},'pending_read_recovery':package_recovery_capability()}
        if manifest_transform: manifest_transform(manifest)
        raw=cr.canonical(manifest)
        with zipfile.ZipFile(folder/'artifact.zip','w') as z:
            for n,b in content.items(): z.writestr('CheJinWorkerClient/'+n,b)
            z.writestr('CheJinWorkerClient/update-package-manifest.json',raw)
        desc={'version':'0.10.14','channel':'gray-hook','platform':'windows-x64','status':'published',
              'git_commit':'b'*40,'artifact_sha256':vh.digest(folder/'artifact.zip'),
              'artifact_size_bytes':(folder/'artifact.zip').stat().st_size,
              'artifact_storage_key':'gray-hook/windows-x64/chejin-worker-v0.10.14-windows-x64.zip',
              'package_manifest_sha256':cr.sha(raw),'published_at':'2026-10-23T00:00:00Z',
              'minimum_updater_version':'0.10.1','rollback_safe':True,'signature_key_id':'test','release_notes':''}
        if descriptor_transform: descriptor_transform(desc)
        release=ClientRelease.from_api({**desc,'latest_version':desc['version'],'update_available':True})
        desc['manifest_signature']=base64.b64encode(key.sign(canonical_release_manifest(release))).decode()
        (folder/'release.json').write_text(json.dumps(desc))
        approval['sha256']=desc['artifact_sha256']
        for report in approval['upgrade_reports'].values(): report['target_zip_sha256']=desc['artifact_sha256']
        (approvals/(approval['sha256']+'.json')).write_text(json.dumps(approval))
        return desc
    return folder,approval,config,seal


def test_real_package_verifier_to_publication_uses_actual_retained_contract(package,monkeypatch):
    folder,approval,config,seal=package; seal()
    import maintenance
    monkeypatch.setattr(maintenance,'validate_ingress',lambda:None)
    monkeypatch.setattr(publish_hook,'trusted_path',lambda p:Path(p)) # only root ownership substituted locally
    calls=[]
    def execute(args,**kw):
        calls.append(args)
        if 'inspect' in args:return 'healthy'
        if '-' in args:return '{"ok":true,"publication":"published"}'
        return ''
    monkeypatch.setattr(publish_hook,'run_fixed',execute) # transport/db boundary, never a server
    result=publish_hook.publish(folder,config)
    assert result['version']=='0.10.14' and result['backend_deployed'] is False
    args=next(a for a in calls if '-' in a)
    assert args[-3:-1]==[approval['component_plan']['after']['contract_revision'],approval['component_plan']['after']['contract_sha256']]


@pytest.mark.parametrize('damage', ['exe','contract','wrong-contract','manifest','recovery','secret','unsupported','wrong-old-zip'])
def test_signed_bad_packages_rejected_before_any_server_call(package,monkeypatch,damage):
    folder,approval,config,seal=package
    transform=None; mt=None; dt=None
    if damage=='exe':transform=lambda f:f.pop('CheJinUpdater.exe')
    if damage=='contract':transform=lambda f:f.pop('_internal/contracts/c2_contract_v3.json')
    if damage=='wrong-contract':transform=lambda f:f.update({'_internal/contracts/c2_contract_v3.json':b'{"contract_revision":"0.0.1"}'})
    if damage=='manifest':mt=lambda m:m['files'].update({'CheJinWorkerClient.exe':'0'*64})
    if damage=='recovery':transform=lambda f:f.pop(next(n for n in f if '/recovery/' in n))
    if damage=='secret':transform=lambda f:f.update({'worker_client.sqlite3':b'data'})
    if damage=='unsupported':dt=lambda d:d.update(minimum_updater_version='99.0.0')
    desc=seal(transform,mt,dt)
    if damage=='wrong-old-zip':config['client_baseline_identities'][__version__]['zip_sha256']='0'*64
    import maintenance
    monkeypatch.setattr(maintenance,'validate_ingress',lambda:None)
    monkeypatch.setattr(publish_hook,'trusted_path',lambda p:Path(p))
    monkeypatch.setattr(publish_hook,'run_fixed',lambda *a,**kw:pytest.fail('bad package reached server'))
    with pytest.raises((ValueError,FileNotFoundError,RuntimeError)):
        publish_hook.publish(folder,config)


def test_untrusted_candidate_directory_is_not_operator_evidence(tmp_path):
    with pytest.raises(ValueError,match='UNTRUSTED_OPERATOR_PATH'):
        vh.trusted_path(tmp_path)
