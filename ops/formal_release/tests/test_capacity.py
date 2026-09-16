"""Bounded cleanup and failure-before-build tests; no business regression or real Docker."""
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capacity as c
import receiver
import deliver
from verify import SUFFIXES


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path.resolve() / 'stage'; root.mkdir()
    public = tmp_path.resolve() / 'public'; public.mkdir()
    cfg = {'staging_root':str(root), 'manual_download_root':str(public), 'container':'api',
           'capacity':{'protected_versions':['2.0.1']}}
    monkeypatch.setattr(c.shutil, 'disk_usage', lambda _:SimpleNamespace(free=10*c.GIB))
    commands=[]
    def run(args):
        commands.append(args)
        return '2.0.2' if args[:2]==['docker','exec'] else ''
    monkeypatch.setattr(c,'run',run)
    return root,public,cfg,commands


def stage(env, version, complete=True):
    root,public,_,_=env
    body=b'old signed archive'; sha=hashlib.sha256(body).hexdigest()
    stem=f'chejin-worker-v{version}-windows-x64'
    meta={'version':version,'current_version':'1.0.0','commit':'a'*40,'sha256':sha,'run_id':'1',
          'files':{stem+s:{'size':len(body),'sha256':sha} for s in SUFFIXES}}
    folder=root/receiver.stage_id(meta); folder.mkdir()
    (folder/'metadata.json').write_text(json.dumps(meta))
    src=folder/(stem+'.zip'); src.write_bytes(body)
    dst=public/'releases'/version; dst.mkdir(parents=True)
    (dst/src.name).write_bytes(body)
    if complete:
        (folder/'verified.json').write_text(json.dumps({'package':'passed',**{k:meta[k] for k in ('version','commit','sha256')}}))
    return src,dst/src.name


def test_removes_only_redundant_old_staging_zip_and_keeps_audit(env):
    source,public=stage(env,'1.0.0')
    result=c.prepare(env[2],'2.0.3','2.0.4')
    assert result['capacity_prepared'] and result['removed_duplicates']==1
    assert not source.exists() and public.exists()
    assert (source.parent/'metadata.json').exists() and (source.parent/'verified.json').exists()
    audit=json.loads((env[0]/'capacity-audit.jsonl').read_text())
    assert audit['status']=='passed' and audit['removed'][0]['public_copy_retained']
    assert all('prune' not in cmd for cmd in env[3])


@pytest.mark.parametrize('version',['2.0.1','2.0.2','2.0.3','2.0.4'])
def test_rollback_active_current_and_target_are_protected(env,version):
    source,_=stage(env,version)
    assert c.prepare(env[2],'2.0.3','2.0.4')['removed_duplicates']==0
    assert source.exists()


def test_incomplete_transfers_are_preserved(env):
    source,_=stage(env,'1.0.0',False)
    part=source.with_suffix('.zip.part0'); part.write_bytes(b'retryable')
    c.prepare(env[2],'2.0.3','2.0.4')
    assert source.exists() and part.read_bytes()==b'retryable'


def test_mismatched_public_copy_blocks_without_deleting_source(env):
    source,public=stage(env,'1.0.0'); public.write_bytes(b'bad')
    with pytest.raises(ValueError,match='PUBLIC_COPY_MISMATCH'):c.prepare(env[2],'2.0.3','2.0.4')
    assert source.exists()
    assert json.loads((env[0]/'capacity-last.json').read_text())['status']=='failed'


def test_symlink_public_directory_is_rejected(env):
    source,public=stage(env,'1.0.0')
    original=public.parent; moved=original.with_name('elsewhere'); original.rename(moved)
    original.symlink_to(moved,target_is_directory=True)
    with pytest.raises(ValueError,match='UNSAFE_CAPACITY_DIRECTORY'):c.prepare(env[2],'2.0.3','2.0.4')
    assert source.exists()


def test_low_capacity_prunes_only_build_cache_then_blocks(env,monkeypatch):
    monkeypatch.setattr(c.shutil,'disk_usage',lambda _:SimpleNamespace(free=4*c.GIB))
    with pytest.raises(ValueError,match='FREE_SPACE_INSUFFICIENT'):c.prepare(env[2],'2.0.3','2.0.4')
    mutations=[x for x in env[3] if 'prune' in x]
    assert mutations==[['docker','builder','prune','--all','--keep-storage','256MB','--force']]
    assert json.loads((env[0]/'capacity-audit.jsonl').read_text())['status']=='failed'


def test_staging_quota_blocks_build_even_if_filesystem_has_room(env):
    env[2]['staging_limit_bytes']=1
    with pytest.raises(ValueError,match='STAGING_QUOTA_INSUFFICIENT'):c.prepare(env[2],'2.0.3','2.0.4')


def test_docker_inventory_order_is_irrelevant(monkeypatch):
    with patch.object(c,'run',side_effect=['a\nb','x\ny','u\nv','b\na','y\nx','v\nu']):
        assert c.inventory()==c.inventory()


def test_transfer_budget_and_resume(env,monkeypatch):
    with pytest.raises(ValueError,match='ARTIFACT_EXCEEDS_BUDGET'):c.check_transfer(env[2],c.GIB)
    monkeypatch.setattr(c.shutil,'disk_usage',lambda _:SimpleNamespace(free=c.FLOOR+300*c.MIB))
    with pytest.raises(ValueError,match='TRANSFER_HEADROOM'):c.check_transfer(env[2],200*c.MIB)
    c.check_transfer(env[2],200*c.MIB,remaining=10*c.MIB)


def test_missing_rollback_registration_fails_closed(env):
    env[2].pop('capacity')
    with pytest.raises(ValueError,match='PROTECTED_BASELINES_REQUIRED'):c.prepare(env[2],'2.0.3','2.0.4')


@pytest.mark.parametrize('remote_result',[{'receiver_version':2},{'receiver_version':3,'capacity_prepared':False}])
def test_formal_build_cannot_use_old_or_failed_capacity_gate(env,monkeypatch,remote_result):
    remote=Mock(return_value=remote_result)
    monkeypatch.setattr(deliver,'Remote',Mock(return_value=remote))
    monkeypatch.setenv('DELIVERY_MODE','build_only');monkeypatch.setenv('TARGET_VERSION','2.0.4')
    output=env[0]/'result.json'
    monkeypatch.setattr(sys,'argv',['deliver','preflight','--role','stage','--current-version','2.0.3','--result',str(output)])
    with pytest.raises(SystemExit):deliver.main()
    assert json.loads(output.read_text())['error_code']=='CAPACITY_PREBUILD_GATE_REQUIRED'
    assert remote.call_args.args[0]['prepare_capacity'] is True


def test_receiver_preflight_requires_stage_role_for_cleanup(env,monkeypatch):
    monkeypatch.setattr(receiver,'client_api',lambda *a:(None,Mock()))
    env[2]['public_keys']='unused'
    with pytest.raises(ValueError,match='ROLE_DENIED'):
        receiver.handle({'operation':'preflight','prepare_capacity':True},io.BytesIO(), 'promote',env[2])
