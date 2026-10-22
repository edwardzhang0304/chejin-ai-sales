"""Exercise offline CLI failures through the actual sales diagnostic ZIP export."""
from contextlib import ExitStack, contextmanager
import errno
import json
import sqlite3
from types import SimpleNamespace
import zipfile

import pytest
from test_hook_component_update import installation, snapshot, apply
from chejin_worker_client import storage, hook_component_update as update
from chejin_worker_client.diagnostic_bundle import export_bundle


def cli(monkeypatch, candidate, report, rollback=False):
    from chejin_worker_client import main, config, release_transport
    mode=['--hook-component-rollback'] if rollback else ['--hook-component-update',str(candidate)]
    monkeypatch.setattr(main.sys,'argv',['worker',*mode,'--confirm-auto-text-disabled','--write-report',str(report)])
    monkeypatch.setattr(config,'CONFIG',SimpleNamespace(app_dir=storage.APP_DIR))
    monkeypatch.setattr(release_transport,'selected_release_channel',lambda _: 'gray-hook')
    with ExitStack() as resources:
        return main._main(resources)


def exported_report(tmp_path, report):
    bundle=export_bundle(storage.APP_DIR,tmp_path/'failure.zip')
    with zipfile.ZipFile(bundle) as archive:
        data=b''.join(archive.read(name) for name in archive.namelist())
        for secret in (b'synthetic-token',b'synthetic existing activation',b'SECRET_EXCEPTION_BODY'):
            assert secret not in data
        assert not any(name.endswith('.dll') for name in archive.namelist())
        member=next(name for name in archive.namelist() if name.endswith('/component-updates/'+report.name))
        return json.loads(archive.read(member))


@pytest.mark.parametrize('rollback',[False,True])
@pytest.mark.parametrize('failure',['permission','disk_full','database'])
def test_cli_failure_export_retains_cause_phase_and_restoration(installation,monkeypatch,tmp_path,rollback,failure):
    candidate,target,pending,activation=installation
    if rollback:apply(candidate)
    before=snapshot();original=target.read_bytes()
    report=storage.APP_DIR/'artifacts/hook/component-updates/result.json'
    real_atomic=update._atomic;real_connection=storage.db_connection
    if failure=='database':
        class DB:
            def __init__(self,db):self.db=db
            def __getattr__(self,name):return getattr(self.db,name)
            def commit(self):raise sqlite3.OperationalError('SECRET_EXCEPTION_BODY')
        @contextmanager
        def connection():
            with real_connection() as db:yield DB(db)
        monkeypatch.setattr(storage,'db_connection',connection)
    else:
        number=errno.EACCES if failure=='permission' else errno.ENOSPC
        def atomic(path,data):
            if path==target:raise OSError(number,'SECRET_EXCEPTION_BODY')
            return real_atomic(path,data)
        monkeypatch.setattr(update,'_atomic',atomic)
    assert cli(monkeypatch,candidate,report,rollback)==4
    monkeypatch.setattr(storage,'db_connection',real_connection)
    assert snapshot()==before and target.read_bytes()==original
    record=exported_report(tmp_path,report)
    assert record['operation']==('rollback' if rollback else 'update')
    assert record['journal_id']==update.NEW_HASH
    assert record['rollback_attempted'] and record['rollback_errors']=={}
    assert record['config_outcome']=='rolled_back' and record['file_outcome']=='restored'
    assert record['frames'] and record['file_replacement_attempted']
    if failure=='database':
        assert record['phase']=='commit_config'
        assert record['exception_type']=='OperationalError'
        assert record['error_code']=='HOOK_COMPONENT_DATABASE_FAILED'
    else:
        assert record['phase']=='replace_dll' and record['errno']==number
        assert record['error_code']==('HOOK_COMPONENT_PERMISSION_DENIED' if failure=='permission' else 'HOOK_COMPONENT_DISK_FULL')
    assert pending.read_bytes()==b'original unresolved send proof'
    assert activation.read_bytes()==b'synthetic existing activation'


def test_primary_and_restoration_errors_both_survive_export_and_retry(installation,monkeypatch,tmp_path):
    candidate,target,*_=installation;before=snapshot()
    report=storage.APP_DIR/'artifacts/hook/component-updates/result.json'
    real=update._atomic;calls=0
    def atomic(path,data):
        nonlocal calls
        if path==target:
            calls+=1
            if calls==1:
                real(path,data)
                raise OSError(errno.ENOSPC,'SECRET_EXCEPTION_BODY')
            raise PermissionError(errno.EACCES,'SECRET_EXCEPTION_BODY')
        return real(path,data)
    monkeypatch.setattr(update,'_atomic',atomic)
    assert cli(monkeypatch,candidate,report)==4
    record=exported_report(tmp_path,report)
    assert record['errno']==errno.ENOSPC
    assert record['rollback_errors']['file']['errno']==errno.EACCES
    assert record['file_outcome']=='unknown' and record['config_outcome']=='rolled_back'
    assert snapshot()==before and update._hash(target)==update.NEW_HASH
    monkeypatch.setattr(update,'_atomic',real)
    assert cli(monkeypatch,candidate,report)==0
    assert update.update_component(rollback=True,automatic_text_disabled=True)['status']=='rolled_back'
    assert snapshot()==before


def test_report_failure_does_not_claim_success_or_mask_primary_error(installation,monkeypatch,tmp_path):
    candidate,target,*_=installation
    report=storage.APP_DIR/'artifacts/hook/component-updates/result.json'
    real=update._atomic
    def atomic(path,data):
        if path==target:raise OSError(errno.ENOSPC,'SECRET_EXCEPTION_BODY')
        if path==report:raise PermissionError(errno.EACCES,'SECRET_EXCEPTION_BODY')
        return real(path,data)
    monkeypatch.setattr(update,'_atomic',atomic)
    from chejin_worker_client import main
    monkeypatch.setattr(main,'emit_cli_output',lambda _: None)  # frozen windowed executable
    assert cli(monkeypatch,candidate,report)==4
    fallback=storage.APP_DIR/'artifacts/runtime/evidence-recorder-failures.jsonl'
    record=json.loads(fallback.read_text())['context']
    assert record['errno']==errno.ENOSPC and record['report_error']['errno']==errno.EACCES
    assert not report.exists()
    fallback=storage.APP_DIR/'artifacts/runtime/evidence-recorder-failures.jsonl'
    assert json.loads(fallback.read_text())['original_error_code']=='HOOK_COMPONENT_DISK_FULL'
    with zipfile.ZipFile(export_bundle(storage.APP_DIR,tmp_path/'fallback.zip')) as archive:
        member=next(name for name in archive.namelist() if name.endswith('/evidence-recorder-failures.jsonl'))
        exported=json.loads(archive.read(member))['context']
        assert exported==record
        assert exported['phase']=='replace_dll' and exported['rollback_attempted']
        assert exported['file_outcome']=='restored' and exported['config_outcome']=='rolled_back'
        assert exported['journal_id']==update.NEW_HASH and exported['frames']
        assert b'SECRET_EXCEPTION_BODY' not in archive.read(member)
