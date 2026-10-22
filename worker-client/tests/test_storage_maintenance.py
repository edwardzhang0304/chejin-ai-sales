"""Disposable retention with real files/state stores; business sentinels are synthetic."""
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from chejin_worker_client import storage_maintenance as m
from chejin_worker_client.client_update import UpdateStateStore

NOW = 2_000_000_000


def old_file(path, content=b'copy', days=40):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (NOW-days*m.DAY, NOW-days*m.DAY))
    return path


@pytest.fixture
def area(tmp_path):
    root = tmp_path/'home'
    root.mkdir()
    store = UpdateStateStore(tmp_path/'updates')
    store.save({'state': 'idle'})
    return root, store


def export(root, n, **kwargs):
    return old_file(root/'artifacts'/'exports'/f'车金故障证据-20260101-120000-{n:06}.zip', **kwargs)


def settled_request(root, store, days=40):
    request = store.root/'requests'/f'update-{uuid4()}'
    old_file(request/'control'/'update-result.json', b'{}', days=days)
    old_file(request/'failed-program'/'program.exe', b'program', days=days)
    m.mark_update_settled(store.root, request.name, root)
    marker = request/m.MARKER
    value = json.loads(marker.read_text()); value['settled_at'] = NOW-days*m.DAY
    marker.write_text(json.dumps(value))
    for p in [*request.rglob('*'), request]:
        os.utime(p, (NOW-days*m.DAY, NOW-days*m.DAY))
    return request


def test_cleanup_never_changes_business_or_manual_directories(area):
    root, store = area
    db = root/'worker_client.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE evidence (kind, state, value)')
        conn.executemany('INSERT INTO evidence VALUES (?,?,?)', [
            ('binding','bound','synthetic-token'), ('upload','pending','synthetic-payload'),
            ('send','unknown','synthetic-send'), ('settings','paused','manual')])
    files = [db]
    for name in ['transactions/hook/send.json','transactions/hook/pending.json',
                 'incidents/pending.json','artifacts/tasks/active/evidence.png',
                 'artifacts/incidents/INC-old.zip','config.json']:
        files.append(old_file(root/name))
    previous = old_file(root.parent/'CheJinWorkerClient.previous'/'program.exe')
    files.append(previous)
    before = {p:p.read_bytes() for p in files}
    for n in range(7): export(root, n)
    temporary = old_file(root/'artifacts'/'exports'/'.writing.tmp')
    custom = old_file(root/'artifacts'/'exports'/'my-evidence.zip')
    request = settled_request(root, store)
    report = m.Sweep(root, store, now=NOW).run()
    assert report['removed_files'] == 5  # four ZIPs plus one settled request tree
    assert not request.exists()
    assert len(list((root/'artifacts'/'exports').glob('车金*.zip'))) == 3
    assert temporary.exists() and custom.exists()
    assert {p:p.read_bytes() for p in files} == before
    assert json.loads((root/'storage-maintenance.json').read_text())['errors'] == 0


@pytest.mark.parametrize('state', [
    {'state':s} for s in ['checking','downloading','waiting_for_safe_boundary','installing',
                          'restarting','verifying','rolling_back','rollback_failed','failed',
                          'succeeded','rolled_back']
] + [{'state':'succeeded','result_reconciled':True,'data_integrity_failed':True},
     {'state':'failed','install_started':False,'status_restore_pending':True}])
def test_no_cleanup_during_update_or_unresolved_result(area, state):
    root, store = area
    copies = [export(root, n) for n in range(5)]
    store.save(state)
    assert m.Sweep(root, store, now=NOW).run()['skipped'] == 'update_in_progress_or_unreconciled'
    assert all(p.exists() for p in copies)
    assert not (root/'storage-maintenance.json').exists()


def test_request_marker_current_request_and_grace_are_required(area, monkeypatch):
    root, store = area
    current, unmarked, recent = [settled_request(root,store,days=d) for d in [40,40,2]]
    (unmarked/m.MARKER).unlink()
    old_file(unmarked/'download'/'partial.zip')
    store.save({'state':'succeeded','result_reconciled':True,'update_request_id':current.name})
    monkeypatch.setattr(m,'UPDATE_BYTES',1)
    m.Sweep(root,store,now=NOW).run()
    assert all(p.exists() for p in [current,unmarked,recent])


def test_capacity_prunes_oldest_eligible_copies(area, monkeypatch):
    root, store = area
    copies = [export(root,n,days=2+n) for n in range(5)]
    monkeypatch.setattr(m,'EXPORT_BYTES',12)
    monkeypatch.setattr(m,'UPDATE_BYTES',1)
    request = settled_request(root,store,days=8)
    m.Sweep(root,store,now=NOW).run()
    assert [p.exists() for p in copies] == [True,True,True,False,False]
    assert not request.exists()


def test_links_never_followed_and_traversal_rejected(area):
    root, store = area
    outside = old_file(root.parent/'keep.bin')
    request = settled_request(root,store)
    (request/'link').symlink_to(outside)
    linked = root/'artifacts'/'runtime'/'startup-crash.jsonl'
    linked.parent.mkdir(parents=True)
    linked.symlink_to(outside)
    assert not m.safe_path(root/'..'/'keep.bin',root)
    report=m.Sweep(root,store,now=NOW).run()
    assert outside.read_bytes()==b'copy' and linked.is_symlink() and request.exists()
    assert report['errors'] == 1
    linked.unlink();os.link(outside,linked)
    assert not m.safe_path(linked,root)
    m.Sweep(root,store,now=NOW).run()
    assert linked.exists() and outside.exists()


def test_junction_attribute_rejected(area, monkeypatch):
    root, _ = area
    original=Path.lstat
    def lstat(p,*args,**kwargs):
        info=original(p,*args,**kwargs)
        if p==root:return SimpleNamespace(st_mode=info.st_mode,st_file_attributes=0x400,st_nlink=1)
        return info
    monkeypatch.setattr(Path,'lstat',lstat)
    assert not m.safe_path(root/'child',root)


def test_known_runtime_logs_rotate_and_business_names_are_ignored(area, monkeypatch):
    root,store=area
    log=old_file(root/'artifacts'/'runtime'/'ui-action-audit.jsonl',b'0123456789',days=0)
    unknown=old_file(log.parent/'pending-send.jsonl',b'not disposable')
    monkeypatch.setattr(m,'LOG_BYTES',8)
    m.Sweep(root,store,now=NOW).run()
    previous=log.with_suffix('.previous.jsonl')
    assert not log.exists() and previous.read_bytes()==b'0123456789'
    assert unknown.read_bytes()==b'not disposable'
    log.write_bytes(b'new')
    m.Sweep(root,store,now=NOW+31*m.DAY).run()
    assert not previous.exists() and unknown.exists()


def test_early_updater_log_is_bounded(area):
    root,_=area
    path=root/'log.jsonl'
    for _ in range(30):m.append_bounded_log(path,'{"phase":"test"}\n',limit=64)
    assert path.stat().st_size<=64 and path.with_suffix('.previous.jsonl').stat().st_size<=64
    assert all(json.loads(line)=={'phase':'test'} for line in path.read_text().splitlines())


def test_budget_permission_error_and_low_disk_do_not_mutate_business(area,monkeypatch):
    root,store=area
    [export(root,n) for n in range(5)]
    assert m.Sweep(root,store,now=NOW,seconds=-1).run()['budget_exhausted']
    original=Path.unlink
    def deny(p,*args,**kwargs):
        if p.suffix=='.zip':raise PermissionError('synthetic locked file')
        return original(p,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',deny)
    monkeypatch.setattr(m.shutil,'disk_usage',lambda _:SimpleNamespace(free=1))
    report=m.Sweep(root,store,now=NOW).run()
    assert report['errors']==1 and report['low_disk']
    assert len(list((root/'artifacts'/'exports').glob('*.zip')))==5


def test_same_store_lock_serializes_update_start(area,monkeypatch):
    root,store=area
    entered=threading.Event();release=threading.Event();begun=threading.Event()
    monkeypatch.setattr(m.Sweep,'exports',lambda _: (entered.set(),release.wait(2)))
    cleanup=threading.Thread(target=lambda:m.Sweep(root,store,now=NOW).run());cleanup.start()
    assert entered.wait(2)
    def begin():
        store.begin(pre_update_run_status='paused',client_instance_id='synthetic');begun.set()
    updater=threading.Thread(target=begin);updater.start()
    assert not begun.wait(.05)
    release.set();cleanup.join(3);updater.join(3)
    assert begun.is_set() and store.load()['state']=='checking'


def test_service_starts_once_warns_without_business_and_stops(area,monkeypatch):
    root,store=area
    warning=threading.Event();calls=[]
    monkeypatch.setattr(m.shutil,'disk_usage',lambda _:SimpleNamespace(free=1))
    monkeypatch.setattr(m,'INTERVAL',.01)
    service=m.StorageMaintenance(root,store,lambda text:(calls.append(text),warning.set()))
    service.start();thread=service.thread;service.start()
    assert service.thread is thread and warning.wait(2)
    time.sleep(.04);service.stop()
    assert len(calls)==1 and not thread.is_alive()
    assert store.load()['state']=='idle'


def test_coordinator_marks_only_proven_settlement(area,monkeypatch):
    from chejin_worker_client.update_coordinator import UpdateCoordinator
    import chejin_worker_client.update_coordinator as c
    root,store=area
    monkeypatch.setattr(c,'CONFIG',SimpleNamespace(app_dir=root))
    coordinator=object.__new__(UpdateCoordinator);coordinator.store=store;coordinator._publish=lambda _:None
    for state,eligible in [({'state':'failed','install_started':False},True),
                           ({'state':'succeeded','result_reconciled':True},True),
                           ({'state':'rolled_back','result_reconciled':True},True),
                           ({'state':'failed'},False),
                           ({'state':'rollback_failed'},False),
                           ({'state':'succeeded','result_reconciled':True,'data_integrity_failed':True},False)]:
        request=store.root/'requests'/f'update-{uuid4()}';request.mkdir(parents=True)
        coordinator._save({**state,'update_request_id':request.name})
        assert (request/m.MARKER).exists()==eligible


def test_real_export_includes_cleanup_report_and_not_credentials(area,tmp_path,monkeypatch):
    import zipfile
    from chejin_worker_client.diagnostic_bundle import export_bundle
    root,store=area
    monkeypatch.setenv('CHEJIN_UPDATE_STAGING_ROOT',str(store.root))
    m.Sweep(root,store,now=NOW).run()
    output=export_bundle(root,tmp_path/'diagnostic.zip')
    with zipfile.ZipFile(output) as archive:
        report=json.loads(archive.read('logs/storage-maintenance.json'))
        assert report['removed_files']==0 and 'observed_at' in report


def test_corrupt_marker_and_foreign_owner_never_deleted(area):
    root,store=area
    request=settled_request(root,store)
    marker=request/m.MARKER
    marker.write_text('{broken')
    assert m.Sweep(root,store,now=NOW).run()['errors']==1 and request.exists()
    marker.write_text(json.dumps({'schema_version':1,'request_id':request.name,
                                 'data_dir':str(root.parent/'another-user'),'settled_at':NOW-40*m.DAY}))
    m.Sweep(root,store,now=NOW).run()
    assert request.exists()


def test_update_root_overlapping_business_is_not_deleted(area):
    root,_=area
    store=UpdateStateStore(root/'custom-cache')
    store.save({'state':'idle'})
    request=settled_request(root,store)
    m.Sweep(root,store,now=NOW).run()
    assert request.exists()


def test_full_disk_still_returns_warning_when_report_cannot_be_written(area,monkeypatch):
    root,store=area
    monkeypatch.setattr(m.shutil,'disk_usage',lambda _:SimpleNamespace(free=0))
    def fail(*args):raise OSError(28,'synthetic full disk')
    monkeypatch.setattr(m,'_write',fail)
    report=m.Sweep(root,store,now=NOW).run()
    assert report['low_disk'] and report['audit_write_failed']


def test_actual_updater_startup_writer_rotates_and_omits_unknown_fields(area,monkeypatch):
    from chejin_worker_client.chejin_updater import _startup_diagnostic
    root,_=area
    path=root/'control'/'updater-startup.jsonl'
    monkeypatch.setenv('CHEJIN_UPDATER_DIAGNOSTIC_PATH',str(path))
    old_file(path,b'x'*(256*1024),days=0)
    _startup_diagnostic('synthetic-phase',password='must-not-appear',error_code='synthetic')
    payload=json.loads(path.read_text())
    assert payload['phase']=='synthetic-phase' and 'password' not in payload
    assert path.with_suffix('.previous.jsonl').stat().st_size==256*1024


def test_maintenance_thread_resource_failure_does_not_break_start_or_close(area,monkeypatch):
    root,store=area
    messages=[]
    def fail(_):raise RuntimeError('synthetic thread resource exhaustion')
    monkeypatch.setattr(threading.Thread,'start',fail)
    service=m.StorageMaintenance(root,store,messages.append)
    service.start();service.stop()
    assert service.thread is None and len(messages)==1 and store.load()['state']=='idle'
