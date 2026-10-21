"""Real SQLite/files/export/Qt event loop; synthetic private contents only."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile

import pytest

from chejin_worker_client.diagnostic_bundle import Collector, export_bundle
from test_failure_evidence_boundaries import home


def write(root, relative, value):
    p = root / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value), encoding="utf-8")
    return p


def seed(root):
    root.mkdir(exist_ok=True)
    db = sqlite3.connect(root / "worker_client.sqlite3")
    db.execute("CREATE TABLE binding(worker_token TEXT)")
    db.execute("INSERT INTO binding VALUES ('synthetic-worker-credential')")
    db.execute("CREATE TABLE local_logs(created_at TEXT,event TEXT,task_id TEXT,error_code TEXT,metadata TEXT)")
    db.execute("INSERT INTO local_logs VALUES ('2026-10-20T07:59:00+00:00','send_failed','task-a','SEND_UNKNOWN',?)",
               (json.dumps({"reply_action_id": "reply-a", "native_msg_id": "42"}),))
    db.execute("CREATE TABLE hook_active_calls(action_id TEXT,kind TEXT,state TEXT)")
    db.execute("INSERT INTO hook_active_calls VALUES ('reply-a','SendTextMsg','uncertain')")
    db.execute("CREATE TABLE hook_voice_attempts(native_msg_id TEXT,state TEXT,error_code TEXT)")
    db.execute("INSERT INTO hook_voice_attempts VALUES ('42','uncertain','HOOK_LOCAL_TRANSPORT_FAILED')")
    db.execute("CREATE TABLE hook_observation_projection(receive_seq INTEGER,projection_json TEXT)")
    db.execute("INSERT INTO hook_observation_projection VALUES (1,?)", (json.dumps({
        "native_msg_id": "42", "reason_code": "HOOK_MESSAGE_TYPE_UNSUPPORTED",
        "content": "你已添加了测试人，现在可以开始聊天了。"}),))
    db.execute("CREATE TABLE hook_raw_inbox(receive_seq INTEGER,raw_body BLOB,received_at TEXT)")
    db.execute("INSERT INTO hook_raw_inbox VALUES (1,?, '2026-10-20T07:59:04+00:00')",
               (json.dumps({"msglist": [{"msgtype": 10000, "msgsvrid": "42",
                                        "msg": "你已添加了测试人，现在可以开始聊天了。"}]}).encode(),))
    db.commit()
    db.close()
    write(root, "transactions/hook/sends/w/a/reply-a/outcome.json",
          {"state": "uncertain", "reply_action_id": "reply-a", "token": "synthetic-worker-credential"})
    write(root, "transactions/hook/invites/w/task-a/outcome.json",
          {"error_code": "INVITE_FORM_WINDOW_NOT_FOUND", "steps": ["contact_result"], "submit_attempted": False})
    write(root, "artifacts/tasks/task-a/ocr/03-card.json",
          [{"text": "添加到通讯录", "confidence": .99}])
    from PIL import Image
    p = root / "artifacts/tasks/task-a/screenshots/03-card.png"
    p.parent.mkdir(parents=True)
    Image.new("RGB", (60, 40), "green").save(p)
    write(root, "transactions/hook/roi/old-task/03-card.json", [{"text": "旧截图识别"}])
    write(root, "artifacts/tasks/old-ocr-task/20261019/result.json", {"status": "failed"})
    write(root, "artifacts/runtime/startup-crash.jsonl", {"error_code": "STARTUP_FAILED"})
    old = root / "incidents/INC-old.zip"
    old.parent.mkdir()
    with zipfile.ZipFile(old, "w") as z:
        z.writestr("manifest.json", json.dumps({"task_id": "task-a", "error_code": "SEND_UNKNOWN"}))
    return root


def read_zip(path):
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


@pytest.mark.parametrize("scenario", ["text", "voice", "invite", "unknown", "offline", "update", "startup"])
def test_actual_export_carries_chain_and_legacy_evidence_without_mutating_source(tmp_path, monkeypatch, scenario):
    root = seed(tmp_path / "home")
    updates = tmp_path / "updates"
    monkeypatch.setenv("CHEJIN_UPDATE_STAGING_ROOT", str(updates))
    write(updates, "requests/u/control/worker-startup.jsonl", {"phase": "restart", "error_code": "UPDATE_FAILED"})
    write(updates, "requests/u/control/updater.jsonl", {"phase": "rollback"})
    write(updates, "update-state.json", {"phase": "failed", "token": "synthetic-worker-credential"})
    write(root, f"artifacts/tasks/task-a/diagnostics/{scenario}.json",
          {"scenario": scenario, "task_id": "task-a", "reply_action_id": "reply-a", "native_msg_id": "42"})
    before = {p.relative_to(root): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in root.rglob("*") if p.is_file()}
    values = []
    output = export_bundle(root, tmp_path / "evidence.zip", values.append)
    entries = read_zip(output)
    assert "tasks/task-a/screenshots/03-card.png" in entries
    assert "tasks/task-a/ocr/03-card.json" in entries
    assert "tasks/legacy-hook/old-task/03-card.json" in entries
    assert "incidents/legacy/INC-old/manifest.json" in entries
    assert f"tasks/task-a/diagnostics/{scenario}.json" in entries
    assert "receipts/hook/sends/w/a/reply-a/outcome.json" in entries
    assert "logs/update/requests/u/control/worker-startup.jsonl" in entries
    assert "logs/update/requests/u/control/updater.jsonl" in entries
    assert "logs/runtime/startup-crash.jsonl" in entries
    assert not any(".sqlite" in name for name in entries)
    assert not any(b"synthetic-worker-credential" in v for v in entries.values())
    calls = json.loads(entries["state/hook_active_calls.json"])["rows"]
    assert calls == [{"action_id": "reply-a", "kind": "SendTextMsg", "state": "uncertain"}]
    assert json.loads(entries["state/hook_voice_attempts.json"])["rows"][0]["native_msg_id"] == "42"
    callback = json.loads(entries["state/hook_raw_inbox.json"])["rows"][0]
    assert callback["callback_messages"][0]["friend_added_notice"]
    assert callback["callback_messages"][0]["msgsvrid"] == "42"
    assert "raw_body" not in callback
    assert values[-1]["percent"] == 100
    assert before == {p.relative_to(root): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in root.rglob("*") if p.is_file()}


def test_bad_database_and_source_read_failure_still_export_other_evidence(tmp_path, monkeypatch):
    root = seed(tmp_path / "home")
    (root / "worker_client.sqlite3").write_bytes(b"broken database")
    original = Path.open
    def denied(path, *args, **kwargs):
        if path.name == "03-card.png":
            raise PermissionError("synthetic denial")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", denied)
    entries = read_zip(export_bundle(root, tmp_path / "result.zip"))
    status = json.loads(entries["收集清单.json"])
    assert status["partial"]
    assert any(e["source"] == "worker_client.sqlite3" and e["status"] == "read_failed" for e in status["sources"])
    assert any(e["source"].endswith("03-card.png") and e["status"] == "read_failed" for e in status["sources"])
    assert "tasks/task-a/ocr/03-card.json" in entries


def test_size_budget_and_recursive_exports_and_symlinks(tmp_path):
    root = seed(tmp_path / "home")
    write(root, "artifacts/exports/previous.zip.json", {"do_not_export": True})
    outside = write(tmp_path, "outside.json", {"private": True})
    (root / "artifacts/tasks/link.json").symlink_to(outside)
    entries = read_zip(export_bundle(root, root / "artifacts/exports/new.zip", max_bytes=500))
    manifest = json.loads(entries["收集清单.json"])
    assert manifest["partial"]
    assert any(e.get("reason") == "total_size_limit" for e in manifest["sources"])
    assert not any("previous.zip" in n or n.endswith("link.json") for n in entries)


def test_export_does_not_block_wal_writer_or_initialize_schema(tmp_path):
    root = seed(tmp_path / "home")
    db = sqlite3.connect(root / "worker_client.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO hook_active_calls VALUES ('not-committed','SendTextMsg','calling')")
    entries = read_zip(export_bundle(root, tmp_path / "result.zip"))
    assert len(json.loads(entries["state/hook_active_calls.json"])["rows"]) == 1
    db.commit()
    assert db.execute("SELECT count(*) FROM hook_active_calls").fetchone()[0] == 2
    db.close()


def test_rescue_module_import_does_not_load_runtime_even_with_corrupt_database(tmp_path):
    (tmp_path / "worker_client.sqlite3").write_bytes(b"invalid")
    code = """
import sys
from chejin_worker_client.diagnostic_ui import DiagnosticDialog
assert 'chejin_worker_client.config' not in sys.modules
assert 'chejin_worker_client.storage' not in sys.modules
assert 'chejin_worker_client.task_runner' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True, env={
        **os.environ, "CHEJIN_WORKER_HOME": str(tmp_path), "QT_QPA_PLATFORM": "offscreen"})


def test_real_qt_dialog_remains_responsive_and_prevents_duplicate_export(tmp_path):
    seed(tmp_path / "home")
    # Separate process: other legacy tests replace Qt in sys.modules.
    code = r"""
import sys,time,json,zipfile
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from chejin_worker_client.diagnostic_ui import DiagnosticDialog
from chejin_worker_client.diagnostic_bundle import Collector
app=QApplication([])
dialog=DiagnosticDialog(Path(sys.argv[1]))
dialog.open_folder=lambda:None
original=Collector.file
def slow(self,*a,**kw):
    time.sleep(.02)  # Slow storage boundary; real collection and ZIP remain.
    return original(self,*a,**kw)
Collector.file=slow
ticks=[]
timer=QTimer();timer.timeout.connect(lambda:ticks.append(time.monotonic()));timer.start(5)
dialog.show()
dialog.button.click()
assert not dialog.button.isEnabled()
assert not dialog.job.start()
deadline=time.monotonic()+15
while dialog.result_path is None and time.monotonic()<deadline:
    app.processEvents();time.sleep(.001)
assert dialog.result_path is not None
assert len(ticks)>10, ticks
assert dialog.button.isEnabled()
with zipfile.ZipFile(dialog.result_path) as z:
    assert 'tasks/task-a/screenshots/03-card.png' in z.namelist()
    assert '收集清单.json' in z.namelist()
assert len(list((Path(sys.argv[1])/'artifacts/exports').glob('*.zip')))==1
print(json.dumps({'ui_timer_ticks':len(ticks),'zip_verified':True}))
"""
    subprocess.run([sys.executable, "-c", code, str(tmp_path / "home")], check=True,
                   env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, timeout=25)


def test_new_and_old_screenshots_retire_only_after_confirmed_invitation(tmp_path):
    from datetime import datetime, timezone, timedelta
    from chejin_worker_client.hook_retention import retire_c1_roi
    root = tmp_path / "transactions/hook"
    for task in ("00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"):
        write(root, f"roi/{task}/frame.json", {"frame": True})
        write(tmp_path, f"artifacts/tasks/{task}/ocr/frame.json", {"frame": True})
        write(root, f"finished/{task}.json", {"complete": True})
    write(root, "invites/worker/00000000-0000-4000-8000-000000000001/backend-submission-receipt.json",
          {"task": {"result_code": "invite_sent"}})
    write(root, "invites/worker/00000000-0000-4000-8000-000000000002/outcome.json", {"status": "submission_unknown"})
    past = (datetime.now(timezone.utc) - timedelta(days=40)).timestamp()
    for folder in (root/"roi", tmp_path/"artifacts"):
        for p in folder.rglob("*"):
            os.utime(p, (past, past))
    receipts = {p.relative_to(root): p.read_bytes() for p in (root/"invites").rglob("*.json")}
    assert retire_c1_roi(root, retention_days=30, artifact_root=tmp_path/"artifacts") == 2
    assert (root/"roi/00000000-0000-4000-8000-000000000002/frame.json").is_file()
    assert (tmp_path/"artifacts/tasks/00000000-0000-4000-8000-000000000002/ocr/frame.json").is_file()
    assert receipts == {p.relative_to(root): p.read_bytes() for p in (root/"invites").rglob("*.json")}


def test_legacy_incident_index_is_read_without_moving_original(tmp_path, monkeypatch):
    from chejin_worker_client import incident_evidence, storage
    monkeypatch.setattr(storage, "APP_DIR", tmp_path)
    old = tmp_path / "incidents/INC-legacy.zip"
    old.parent.mkdir()
    with zipfile.ZipFile(old, "w") as z:
        z.writestr("manifest.json", '{"incident_id":"INC-legacy"}')
    write(tmp_path, "incidents/latest.json", {"path": str(old), "incident_id": "INC-legacy",
                                            "created_at": "2026-10-19T01:00:00+00:00"})
    before = old.read_bytes()
    assert incident_evidence.latest_incident()["evidence_path"] == str(old)
    assert incident_evidence.incident_by_id("INC-legacy")["evidence_path"] == str(old)
    assert incident_evidence.incident_directory() == tmp_path / "artifacts/incidents"
    export_bundle(tmp_path, tmp_path / "result.zip")
    assert old.read_bytes() == before


def test_old_pending_incident_finishes_in_new_directory(home):
    from chejin_worker_client import incident_evidence
    import shutil
    request = incident_evidence.schedule_incident(
        event="synthetic_old_pending", error_code="SYNTHETIC_FAILED", message="pending", metadata={}, start_worker=False)
    old = home / "incidents"
    shutil.move(str(home / "artifacts/incidents"), old)
    incident_evidence.start_incident_worker()
    result = incident_evidence.wait_for_incident(request["incident_id"], timeout=5)
    assert result is not None
    assert result.parent == home / "artifacts/incidents"
    assert not list((old/"pending").glob("*.json"))
    with zipfile.ZipFile(result) as z:
        assert json.loads(z.read("manifest.json"))["event"] == "synthetic_old_pending"


def test_actual_voice_schema_and_hook_receipts_redact_text_but_keep_ocr(tmp_path):
    from chejin_worker_client.hook_voice import _ensure_schema
    root = tmp_path / 'home'
    root.mkdir()
    db = sqlite3.connect(root / 'worker_client.sqlite3')
    db.row_factory = sqlite3.Row
    _ensure_schema(db)
    db.execute('INSERT INTO hook_voice_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
               ('self', 'epoch', '123', 'peer', 'generation', 'a'*64, 12, 'transcribed',
                '2026-10-20T00:00:00Z', '2026-10-20T00:00:01Z', 'b'*64, 'PRIVATE_TRANSCRIPT', None))
    db.commit(); db.close()
    write(root, 'transactions/hook/voice-receipts/w/123/receipt.json',
          {'native_msg_id': '123', 'payload': {'text': 'PRIVATE_TRANSCRIPT'}, 'response_sha256': 'b'*64})
    write(root, 'transactions/hook/sends/action-a/outcome.json',
          {'action_id': 'action-a', 'history': [{'text': 'PRIVATE_REPLY', 'msgsvrid': '456'}]})
    write(root, 'artifacts/tasks/task-a/ocr/03-card.json', [{'text': '添加到通讯录'}])
    entries = read_zip(export_bundle(root, tmp_path / 'result.zip'))
    assert not any(b'PRIVATE_TRANSCRIPT' in data or b'PRIVATE_REPLY' in data for data in entries.values())
    row = json.loads(entries['state/hook_voice_attempts.json'])['rows'][0]
    assert row['native_msg_id'] == '123' and row['text']['bytes'] == len('PRIVATE_TRANSCRIPT')
    assert row['text']['sha256'] == hashlib.sha256(b'PRIVATE_TRANSCRIPT').hexdigest()
    assert json.loads(entries['tasks/task-a/ocr/03-card.json'])[0]['text'] == '添加到通讯录'
    assert json.loads(entries['receipts/hook/sends/action-a/outcome.json'])['history'][0]['msgsvrid'] == '456'


@pytest.mark.parametrize('linked', ['transactions', 'artifacts', 'database', 'updates_ancestor'])
def test_selected_source_ancestor_link_never_exports_outside_content(tmp_path, monkeypatch, linked):
    root = tmp_path / 'home'; root.mkdir()
    outside = tmp_path / 'outside'; outside.mkdir()
    marker = 'OUTSIDE_PRIVATE_CONTENT'
    if linked == 'database':
        db = sqlite3.connect(outside / 'db.sqlite3')
        db.execute('CREATE TABLE local_logs(error_code TEXT)')
        db.execute('INSERT INTO local_logs VALUES (?)', (marker,)); db.commit(); db.close()
        (root / 'worker_client.sqlite3').symlink_to(outside / 'db.sqlite3')
    elif linked == 'updates_ancestor':
        write(outside, 'updates/requests/a/control/update-result.json', {'error_code': marker})
        link = tmp_path / 'linked'; link.symlink_to(outside, target_is_directory=True)
        monkeypatch.setenv('CHEJIN_UPDATE_STAGING_ROOT', str(link / 'updates'))
    else:
        relative = 'hook/roi/task-a/03.json' if linked == 'transactions' else 'tasks/task-a/ocr/03.json'
        write(outside, relative, {'text': marker})
        (root / linked).symlink_to(outside, target_is_directory=True)
    entries = read_zip(export_bundle(root, tmp_path / 'result.zip'))
    assert not any(marker.encode() in data for data in entries.values())
    manifest = json.loads(entries['收集清单.json'])
    assert any(e.get('reason') == 'source_boundary' or (linked == 'database' and e['status'] == 'read_failed')
               for e in manifest['sources'])


def test_windows_reparse_attribute_is_rejected_before_file_read(tmp_path, monkeypatch):
    # Windows lstat exposes this attribute for junctions; exercise that branch on all hosts.
    from types import SimpleNamespace
    root = tmp_path / 'home'; root.mkdir()
    p = write(root, 'transactions/hook/roi/task-a/03.json', {'marker': 'JUNCTION_PRIVATE'})
    original = Path.lstat
    def lstat(path, *a, **kw):
        info = original(path, *a, **kw)
        if path == root / 'transactions':
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, 'lstat', lstat)
    assert not Collector(root, tmp_path/'result.zip').source_allowed(p)


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows junction acceptance')
def test_native_windows_junction_is_excluded(tmp_path):
    root = tmp_path/'home'; root.mkdir()
    outside = tmp_path/'outside'; outside.mkdir()
    write(outside, 'hook/roi/task/03.json', {'marker': 'JUNCTION_PRIVATE'})
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(root/'transactions'), str(outside)], check=True)
    try:
        entries = read_zip(export_bundle(root, tmp_path/'result.zip'))
        assert not any(b'JUNCTION_PRIVATE' in data for data in entries.values())
        assert any(e.get('reason') == 'source_boundary' for e in json.loads(entries['收集清单.json'])['sources'])
    finally:
        os.rmdir(root/'transactions')


@pytest.mark.parametrize('mode', ['localappdata', 'override', 'fallback'])
def test_update_result_uses_same_root_as_runtime(tmp_path, monkeypatch, mode):
    from types import SimpleNamespace
    from chejin_worker_client import client_update
    from chejin_worker_client.evidence_paths import update_root
    root = tmp_path/'custom/home'; root.mkdir(parents=True)
    monkeypatch.setattr(client_update, 'CONFIG', SimpleNamespace(app_dir=root))
    monkeypatch.delenv('CHEJIN_UPDATE_STAGING_ROOT', raising=False)
    monkeypatch.delenv('LOCALAPPDATA', raising=False)
    expected = root.parent/'CheJinWorkerUpdate'
    if mode != 'fallback':
        monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'local'))
        expected = tmp_path/'local/CheJinWorkerUpdate'
    if mode == 'override':
        monkeypatch.setenv('CHEJIN_UPDATE_STAGING_ROOT', str(tmp_path/'custom-updates'))
        expected = tmp_path/'custom-updates'
    assert update_root(root) == client_update.update_root() == expected
    write(expected, 'requests/r/control/update-result.json', {'status': 'failed', 'error_code': 'UPDATE_FAILED'})
    write(expected, 'requests/r/control/worker-startup.jsonl', {'phase': 'startup', 'error_code': 'STARTUP_FAILED'})
    write(expected, 'requests/r/control/update-plan.json', {'private': 'DO_NOT_EXPORT_PLAN'})
    entries = read_zip(export_bundle(root, tmp_path/'result.zip'))
    assert json.loads(entries['logs/update/requests/r/control/update-result.json'])['error_code'] == 'UPDATE_FAILED'
    assert not any(b'DO_NOT_EXPORT_PLAN' in data for data in entries.values())
    assert any(e['source'].endswith('updater-startup.jsonl') and e['status'] == 'absent'
               for e in json.loads(entries['收集清单.json'])['sources'])


def test_cross_table_snapshot_remains_consistent_during_real_concurrent_commit(tmp_path, monkeypatch):
    import threading
    root = tmp_path/'home'; root.mkdir()
    path = root/'worker_client.sqlite3'
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE hook_raw_inbox(receive_seq INTEGER,raw_body BLOB)')
    db.execute('CREATE TABLE hook_observation_projection(receive_seq INTEGER,projection_json TEXT)')
    db.execute("INSERT INTO hook_raw_inbox VALUES (0, NULL)")
    db.execute("INSERT INTO hook_observation_projection VALUES (0, '{}')")
    db.commit(); db.close()
    original = Collector.clean
    committed = []
    def clean(self, value, **kw):
        if isinstance(value, dict) and 'raw_body_bytes' in value and not committed:
            def writer():
                c = sqlite3.connect(path, timeout=.2)
                with c:
                    c.execute("INSERT INTO hook_raw_inbox VALUES (1, NULL)")
                    c.execute("INSERT INTO hook_observation_projection VALUES (1, '{}')")
                c.close(); committed.append(True)
            t = threading.Thread(target=writer); t.start(); t.join(1)
            assert committed, 'WAL writer must finish while read snapshot is open'
        return original(self, value, **kw)
    monkeypatch.setattr(Collector, 'clean', clean)
    entries = read_zip(export_bundle(root, tmp_path/'result.zip'))
    a = json.loads(entries['state/hook_raw_inbox.json'])
    b = json.loads(entries['state/hook_observation_projection.json'])
    assert [r['receive_seq'] for r in a['rows']] == [r['receive_seq'] for r in b['rows']] == [0]
    assert a['snapshot_at'] == b['snapshot_at'] and committed
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT count(*) FROM hook_observation_projection').fetchone()[0] == 2


@pytest.mark.parametrize('mode', ['success', 'failure', 'embedded', 'escape'])
def test_real_qt_close_export_lifecycle(tmp_path, mode):
    seed(tmp_path/'home')
    code = r'''
import sys,time
from pathlib import Path
from PySide6.QtWidgets import QApplication,QWidget
from PySide6.QtCore import QTimer
import chejin_worker_client.diagnostic_ui as ui
app=QApplication([])
mode=sys.argv[2]
parent=QWidget() if mode=='embedded' else None
if parent: parent.show()
dialog=ui.DiagnosticDialog(Path(sys.argv[1]),parent)
dialog.open_folder=lambda:None
real_export=ui.export_bundle
started=[]
def slow(*a,**kw):
    started.append(True)
    time.sleep(.15)
    if mode=='failure': raise OSError('synthetic')
    return real_export(*a,**kw)
ui.export_bundle=slow
watchdog=[]
def timeout(): watchdog.append(True);app.quit()
QTimer.singleShot(5000,timeout)
dialog.show();dialog.start()
QTimer.singleShot(20,dialog.reject if mode=='escape' else dialog.close)
if mode=='embedded':
    def check_and_reopen():
        assert not dialog.job.running and not dialog.isVisible()
        assert dialog.result_path and parent.isVisible()
        dialog.show()
        assert not dialog.close_requested
        dialog.start()
        assert dialog.job.running
    def finish_embedded():
        if len(started)==2 and not dialog.job.running and dialog.button.isEnabled():
            assert parent.isVisible() and dialog.isVisible()
            app.quit()
    poll=QTimer();poll.timeout.connect(finish_embedded);poll.start(20)
    QTimer.singleShot(800,check_and_reopen)
app.exec()
assert not watchdog and started
assert not dialog.job.running
if mode=='failure': assert dialog.result_path is None
else: assert dialog.result_path is not None
'''
    subprocess.run([sys.executable, '-c', code, str(tmp_path/'home'), mode], check=True,
                   env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'}, timeout=12)


def test_actual_web_bridge_hands_off_to_shared_dialog_once(home, monkeypatch):
    from types import ModuleType, SimpleNamespace, MethodType
    from test_web_ui_binding_behavior import _headless_web_ui_module
    constructed = []
    class Dialog:
        def __init__(self, root, parent):
            constructed.append((root, parent)); self.job = SimpleNamespace(running=False)
            self.shown = 0; self.starts = 0
        def show(self): self.shown += 1
        def raise_(self): pass
        def start(self): self.starts += 1; self.job.running = True
    module = ModuleType('chejin_worker_client.diagnostic_ui'); module.DiagnosticDialog = Dialog
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with _headless_web_ui_module() as web:
        window = SimpleNamespace()
        window._export_incident = MethodType(web.WorkerWebWindow._export_incident, window)
        window.export_latest_incident = MethodType(web.WorkerWebWindow.export_latest_incident, window)
        bridge = web.WorkerWebBridge(window)
        assert bridge.exportLatestIncident() == ''
        assert bridge.exportLatestIncident() == ''
        assert len(constructed) == 1 and constructed[0] == (web.CONFIG.app_dir, window)
        assert window._diagnostic_dialog.shown == 2 and window._diagnostic_dialog.starts == 1


def test_legacy_incident_flood_cannot_starve_current_recovery_or_raw_history(tmp_path):
    root=seed(tmp_path/'home')
    for i in range(3100):
        write(root, f'incidents/old-{i}.json', {'error_code':'OLD_ERROR'})
    capture={'query':{'kind':'page'},'raw_utf8':json.dumps({'data':[
        {'localId':'119','MsgSvrID':'0','Type':'1','IsSender':'1',
         'StrContent':'synthetic-private-body','token':'synthetic-worker-credential'}]})}
    path=write(root,'transactions/hook/history-captures/latest.json',capture)
    before=path.read_bytes()
    entries=read_zip(export_bundle(root,tmp_path/'bundle.zip'))
    assert 'receipts/hook/sends/w/a/reply-a/outcome.json' in entries
    parsed=json.loads(entries['receipts/hook/history-captures/latest.json'])
    row=parsed['raw_utf8']['data'][0]
    assert (row['localId'],row['MsgSvrID'],row['Type'],row['IsSender'])==('119','0','1','1')
    assert row['StrContent']['omitted']=='message_or_audio_body'
    assert b'synthetic-private-body' not in b''.join(entries.values())
    assert b'synthetic-worker-credential' not in b''.join(entries.values())
    manifest=json.loads(entries['收集清单.json'])
    assert manifest['partial']
    assert any(e['source']=='incidents/legacy' and e['status']=='omitted' for e in manifest['sources'])
    assert path.read_bytes()==before


def test_original_unsettled_journal_and_linked_capture_precede_newer_completed_records(tmp_path):
    root=seed(tmp_path/'home');hook=root/'transactions/hook'
    original='00000000-0000-4000-8000-000000000001'
    for i in range(451):
        action=original if i==0 else f'finished-{i}'
        for name in ('intent','permit','trigger','outcome'):
            p=write(hook,f'sends/w/epoch/{action}/{name}.json',{'reply_action_id':action,'task_id':action})
            os.utime(p,(1000+i,1000+i))
        if i:
            write(hook,f'send-receipts/{action}.json',{'local_send_observed':True,'automatic_resend_allowed':False})
    index={'retained_reply_action_id':original,'files':{'history-captures/old-current.json':'x','history-coverage/original.json':'y'}}
    write(hook,'history-artifact-index/original.json',index)
    raw=write(hook,'history-captures/old-current.json',{'raw_utf8':json.dumps({'data':[{'MsgSvrID':'0','localId':'119','StrContent':'private-body'}]})})
    os.utime(raw,(900,900))
    write(hook,'history-coverage/original.json',{'coverage':'synthetic'})
    write(hook,'history-receipts/original.json',{'reason_code':'HOOK_HISTORY_OUTGOING_ID_PENDING'})
    for i in range(450):
        p=write(hook,f'history-captures/newer-{i}.json',{})
        os.utime(p,(2000+i,2000+i))
    before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (hook/'sends/w/epoch'/original).iterdir()}
    entries=read_zip(export_bundle(root,tmp_path/'evidence.zip'))
    for name in ('intent','permit','trigger','outcome'):
        assert f'receipts/hook/sends/w/epoch/{original}/{name}.json' in entries
    for name in ('history-artifact-index/original.json','history-captures/old-current.json',
                 'history-coverage/original.json','history-receipts/original.json'):
        assert 'receipts/hook/'+name in entries
    assert b'private-body' not in b''.join(entries.values())
    assert before=={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in before}


def test_priority_recovery_index_cannot_escape_source_boundary(tmp_path):
    root=seed(tmp_path/'home');hook=root/'transactions/hook'
    write(hook,'sends/w/e/original/intent.json',{'reply_action_id':'original','task_id':'task'})
    write(hook,'history-artifact-index/current.json',{'retained_reply_action_id':'original','files':{
        '../../private.json':'x','history-captures/link.json':'x'}})
    outside=write(tmp_path,'private.json',{'secret':'must-not-appear'})
    (hook/'history-captures').mkdir()
    (hook/'history-captures/link.json').symlink_to(outside)
    entries=read_zip(export_bundle(root,tmp_path/'export.zip'))
    assert b'must-not-appear' not in b''.join(entries.values())
    omissions=json.loads(entries['evidence-index/export.json'])['omissions']
    assert any(e['reason']=='invalid_index_reference' for e in omissions)
    assert any(e['reason']=='source_boundary' for e in omissions)
