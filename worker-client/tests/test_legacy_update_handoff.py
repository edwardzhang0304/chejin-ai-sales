import base64
import hashlib
import json
from types import SimpleNamespace
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from chejin_worker_client import legacy_update_handoff as legacy, post_update_health as health, storage
from chejin_worker_client import update_data_snapshot as snapshots
from chejin_worker_client.client_update import UpdateStateStore
from chejin_worker_client.models import ClientRelease
from chejin_worker_client.release_package_contract import canonical_release_manifest
from chejin_worker_client.update_data_access import clear_update_writer, canonical

def old_snapshot(data):
    tables = {}
    with snapshots._read_transaction(data) as conn:
        for table, fields in snapshots.PROTECTED_TABLE_FIELDS.items():
            rows = snapshots._canonical_rows(conn, table, fields)
            tables[table] = dict(fields=list(fields), row_count=len(rows), sha256=hashlib.sha256(canonical(rows)).hexdigest())
    return dict(snapshot_schema_version=1, tables=tables, files=snapshots.protected_update_snapshot(data_dir=data)['files'])

@pytest.fixture
def candidate(tmp_path, monkeypatch):
    data=tmp_path/'data';program=tmp_path/'program';control=tmp_path/'request'/'control'
    program.mkdir();control.mkdir(parents=True)
    monkeypatch.setattr(storage,'APP_DIR',data);monkeypatch.setattr(storage,'DB_FILE',data/'worker_client.sqlite3')
    monkeypatch.setattr(storage,'_post_update_initialized_database',None)
    monkeypatch.setattr(health,'CONFIG',SimpleNamespace(app_dir=data));monkeypatch.setattr(health,'__version__','0.9.69')
    monkeypatch.setenv('CHEJIN_UPDATE_STAGING_ROOT',str(tmp_path/'updates'))
    with storage.db_connection() as conn:
        conn.execute("INSERT INTO client_settings VALUES ('schedule','retained','original-time')");conn.commit()
    before=old_snapshot(data)
    (program/'CheJinWorkerClient.exe').write_bytes(b'synthetic client');(program/'CheJinUpdater.exe').write_bytes(b'synthetic updater')
    manifest=dict(schema_version=1,version='0.9.69',platform='windows-x64',git_commit='b'*40,rollback_safe=True,
                  files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in program.iterdir()})
    raw=json.dumps(manifest).encode();(program/'update-package-manifest.json').write_bytes(raw)
    monkeypatch.setattr(health.sys,'executable',str(program/'CheJinWorkerClient.exe'))
    key=Ed25519PrivateKey.generate();keys=tmp_path/'keys.json'
    keys.write_text(json.dumps({'keys':[{'key_id':'test','algorithm':'ed25519','public_key_base64':base64.b64encode(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()}]}))
    monkeypatch.setenv('CHEJIN_RELEASE_SIGNING_KEYS_PATH',str(keys))
    release=ClientRelease.from_api(dict(update_available=True,latest_version='0.9.69',channel='gray',platform='windows-x64',
        artifact_size_bytes=100,artifact_sha256='a'*64,git_commit='b'*40,package_manifest_sha256=hashlib.sha256(raw).hexdigest(),
        signature_key_id='test',published_at='2026-09-07T00:00:00Z',minimum_updater_version='0.9.67',rollback_safe=True))
    descriptor={**release.__dict__,'manifest_signature':base64.b64encode(key.sign(canonical_release_manifest(release))).decode()}
    token='synthetic-token';path=control/'update-plan.json'
    plan=dict(schema_version=1,current_version='0.9.67',target_version='0.9.69',update_request_id='legacy-test',
              current_program_dir=str(program),data_dir=str(data),one_time_token_sha256=hashlib.sha256(token.encode()).hexdigest(),
              release=descriptor,protected_data_snapshot=before,healthy_marker_path=str(control/'healthy.json'))
    path.write_text(json.dumps(plan))
    UpdateStateStore().save(dict(state='installing',install_started=True,update_request_id='legacy-test',plan_path=str(path),pre_update_run_status='running'))
    monkeypatch.setattr(legacy,'_validate_legacy_parent',lambda *a:None)
    yield path,token,data,before
    clear_update_writer()

def test_legacy_unchanged_data_passes_and_initialization_runs_once(candidate,monkeypatch):
    path,token,data,before=candidate;original=path.read_bytes();calls=[];initialize=storage.init_db
    def count(conn):calls.append(1);initialize(conn)
    monkeypatch.setattr(storage,'init_db',count)
    result=health.verify_post_update_startup(path,token)
    with storage.db_connection():pass
    assert calls==[1] and result['legacy_source_plan_schema']==1
    assert old_snapshot(data)==before and path.read_bytes()==original

@pytest.mark.parametrize('damage',['late-write','initialization','file','missing-table'])
def test_legacy_difference_never_rebases_or_reports_success(candidate,monkeypatch,damage):
    path,token,data,before=candidate;original=path.read_bytes()
    if damage=='late-write':
        with storage.db_connection() as conn:
            conn.execute("UPDATE client_settings SET updated_at='late-commit'");conn.commit()
    elif damage=='file':
        plan=json.loads(path.read_text());plan['protected_data_snapshot']['files']['diagnostics/existing.log']={'size':1,'sha256':'a'*64};path.write_text(json.dumps(plan));original=path.read_bytes()
    else:
        initialize=storage.init_db
        def broken(conn):
            initialize(conn)
            conn.execute("DROP TABLE client_settings" if damage=='missing-table' else "INSERT INTO client_settings VALUES ('unexpected','write','now')");conn.commit()
        monkeypatch.setattr(storage,'init_db',broken)
    with pytest.raises(RuntimeError,match='UPDATE_PROTECTED_'):health.verify_post_update_startup(path,token)
    assert not (path.parent/'healthy.json').exists() and path.read_bytes()==original
    assert UpdateStateStore().load()['fault_after_request'] is True
    if damage in {'late-write','file'}:assert not (path.parent/'protected-data-baseline.json').exists()

@pytest.mark.parametrize('field,value',[('current_version','0.9.66'),('target_version','0.9.70'),('one_time_token_sha256','0'*64)])
def test_wrong_version_or_token_fails_closed(candidate,field,value):
    path,token,_,_=candidate;plan=json.loads(path.read_text());plan[field]=value;path.write_text(json.dumps(plan))
    with pytest.raises(RuntimeError):health.verify_post_update_startup(path,token)
    assert not (path.parent/'protected-data-baseline.json').exists()

def test_untrusted_updater_rejected(tmp_path):
    path=tmp_path/'update-plan.json';(tmp_path/'CheJinUpdater.exe').write_bytes(b'untrusted')
    with pytest.raises(RuntimeError,match='UPDATE_LEGACY_UPDATER_UNTRUSTED'):legacy._validate_legacy_parent({},path)
