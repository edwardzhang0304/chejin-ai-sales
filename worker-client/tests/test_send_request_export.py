"""Request evidence only: real files/ZIP and the public diagnostic export."""
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest
from test_send_request_file import original, material, TEXT
from chejin_worker_client import send_request_evidence, incident_evidence, client_update
from apps.wechat_ai_customer_service.adapters import send_launch_journal as launches, send_request_file as files


def request_input(original, monkeypatch, *, temporary):
    bridge, journal_path, context = original
    if not temporary:
        _, ref, raw, _ = material(original)
        return ref, Path(ref['path']), raw
    class Interrupted(BaseException): pass
    def before_rename(source, target):
        assert Path(source).is_file() and not Path(target).exists()
        assert launches.references(journal_path)
        raise Interrupted('controlled exit after fsync and before rename')
    with monkeypatch.context() as patch:
        patch.setattr(files.os, 'rename', before_rename)
        with pytest.raises(Interrupted):
            bridge.send_reply(target='CJTEST01', rpa_session_key='', text=TEXT,
                task_id=context['task_id'], reply_action_id=context['reply_action_id'],
                expected_context_guard={'history': ['unchanged input']})
    ref = launches.references(journal_path)[0]
    source = Path(ref['temporary_path'])
    raw = source.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ref['sha256']
    return ref, source, raw


@pytest.mark.parametrize('temporary', [False, True])
def test_registered_input_export_preserves_bytes_and_commit_state(original, monkeypatch, temporary):
    ref, source, raw = request_input(original, monkeypatch, temporary=temporary)
    with zipfile.ZipFile(io.BytesIO(), 'w') as archive:
        omissions = []
        entries = send_request_evidence.export_files(archive, secrets=set(), max_bytes=100000, omissions=omissions)
        assert len(entries) == 1 and not omissions
        entry = entries[0]
        assert entry['archive_path'] == 'ipc/'+ref['request_id']+('.tmp' if temporary else '.json')
        assert entry['package_state'] == ('temporary_uncommitted' if temporary else 'committed')
        assert entry['source_path'] == str(source) and entry['sha256'] == ref['sha256']
        assert archive.read(entry['archive_path']) == raw
    assert source.read_bytes() == raw and Path(ref['path']).exists() != temporary
    if temporary:
        with pytest.raises(ValueError, match='SEND_REQUEST_FILENAME_INVALID'):
            files.read_package(source)  # Sidecar's committed-file admission is unchanged.


@pytest.mark.parametrize('bad', ['digest', 'read', 'size', 'secret', 'path', 'missing'])
def test_bad_temporary_input_is_an_explicit_omission(original, monkeypatch, bad):
    ref, source, raw = request_input(original, monkeypatch, temporary=True)
    if bad == 'digest': source.write_bytes(raw+b'changed')
    if bad == 'missing': source.unlink()
    if bad == 'read':
        native = Path.read_bytes
        def unreadable(path):
            if path == source: raise PermissionError('controlled source read failure')
            return native(path)
        monkeypatch.setattr(Path, 'read_bytes', unreadable)
    if bad == 'path':
        journal, _ = launches.read(original[1])
        journal['send_launch_attempts'][-1]['request_files'][0]['temporary_path'] = str(source.with_name('unregistered.tmp'))
        launches._write(original[1], journal)
    with zipfile.ZipFile(io.BytesIO(), 'w') as archive:
        omissions = []
        entries = send_request_evidence.export_files(archive,
            secrets={'unchanged input'} if bad == 'secret' else set(),
            max_bytes=len(raw)-1 if bad == 'size' else 100000, omissions=omissions)
        assert entries == [] and not archive.namelist()
        assert len(omissions) == 1 and omissions[0]['request_id'] == ref['request_id']
        assert omissions[0]['reason']


def test_existing_invalid_final_never_falls_back_to_temporary(original, monkeypatch):
    ref, source, raw = request_input(original, monkeypatch, temporary=True)
    Path(ref['path']).write_bytes(b'changed final')
    with zipfile.ZipFile(io.BytesIO(), 'w') as archive:
        omissions = []
        assert send_request_evidence.export_files(archive, secrets=set(), max_bytes=100000, omissions=omissions) == []
        assert omissions[0]['reason'] == 'SEND_REQUEST_DIGEST_INVALID'
        assert not archive.namelist()
    assert source.read_bytes() == raw


@pytest.mark.parametrize('temporary', [False, True])
def test_public_export_contains_registered_request_and_state(original, monkeypatch, tmp_path, temporary):
    ref, source, raw = request_input(original, monkeypatch, temporary=temporary)
    monkeypatch.setattr(client_update, 'update_root', lambda: tmp_path/'isolated-updater')
    monkeypatch.setattr(incident_evidence, '_known_secret_values', lambda: set())
    exported = incident_evidence.export_diagnostic_bundle(tmp_path/'diagnostics.zip')
    with zipfile.ZipFile(exported) as archive:
        index = json.loads(archive.read('evidence-index/export.json'))
        entries = [entry for entry in index['files'] if entry.get('request_id') == ref['request_id']]
        assert len(entries) == 1
        assert archive.read(entries[0]['archive_path']) == raw
        assert entries[0]['package_state'] == ('temporary_uncommitted' if temporary else 'committed')
    assert source.read_bytes() == raw


@pytest.mark.parametrize('public', [False, True])
def test_archive_write_error_is_not_reported_as_missing_source(original, monkeypatch, tmp_path, public):
    ref, source, raw = request_input(original, monkeypatch, temporary=True)
    monkeypatch.setattr(client_update, 'update_root', lambda: tmp_path/'isolated-updater')
    monkeypatch.setattr(incident_evidence, '_known_secret_values', lambda: set())
    native = zipfile.ZipFile.writestr
    def fail_request(archive, name, *args, **kwargs):
        if str(name).startswith('ipc/'):
            raise OSError('controlled ZIP destination write failure')
        return native(archive, name, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, 'writestr', fail_request)
    omissions = []
    with pytest.raises(OSError, match='ZIP destination'):
        if public:
            incident_evidence.export_diagnostic_bundle(tmp_path/'failed.zip')
        else:
            with zipfile.ZipFile(io.BytesIO(), 'w') as archive:
                send_request_evidence.export_files(archive, secrets=set(), max_bytes=100000, omissions=omissions)
    assert not omissions and not (tmp_path/'failed.zip').exists()
    assert not list(tmp_path.glob('.failed.zip.*.tmp'))
    assert source.read_bytes() == raw
