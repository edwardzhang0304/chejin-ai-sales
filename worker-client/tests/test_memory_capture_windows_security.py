"""Windows API boundary controls; native Windows validation is separate."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import pytest

from chejin_worker_client import memory_capture as mc, memory_capture_channel as cp
from chejin_worker_client.diagnostic_bundle import export_bundle


@pytest.fixture
def security(monkeypatch):
    class ACL:
        def __init__(self, entries=()): self.entries = list(entries)
        def GetAceCount(self): return len(self.entries)
        def GetAce(self, index): return self.entries[index]
        def AddAccessAllowedAceEx(self, revision, flags, mask, sid):
            self.entries.append(((0, flags), mask, sid))
    state = SimpleNamespace(owners={}, acls={}, writes=[], no_effect=False, handles=[], calls=[])
    class Handle:
        def __init__(self, path):
            self.path = path
            self.fd = os.open(path, os.O_RDONLY)
            self.closed = False
        def Close(self):
            if not self.closed: os.close(self.fd); self.closed = True
    def open_directory(path, access, share, sa, disposition, flags, template):
        assert share == 1 and flags == 0x02200000
        assert disposition == 3
        state.calls.append(('open', path, access))
        h = Handle(path); state.handles.append(h); return h
    def descriptor(handle, *_args):
        path = handle.path
        return SimpleNamespace(GetSecurityDescriptorOwner=lambda: state.owners.get(path, 'admins'),
            GetSecurityDescriptorDacl=lambda: state.acls.get(path, ACL([
                ((0, 0), 0x1f01ff, 'user'), ((0, 0), 0x1f01ff, 'system'), ((0, 0), 0x1f01ff, 'admins')])))
    def set_info(handle, kind, flags, owner, group, acl, sacl):
        assert isinstance(handle, Handle), 'permission mutation must be handle-based'
        path = handle.path
        state.writes.append(path)
        state.calls.append(('set_owner' if owner else 'set_dacl', path))
        if not state.no_effect:
            if owner is not None: state.owners[path] = owner
            if acl is not None:
                assert all(a[0][1] == 0 for a in acl.entries), 'must not propagate to old child records'
                state.acls[path] = acl
                os.fchmod(handle.fd, 0o700)
    api = SimpleNamespace(ACL=ACL, OpenProcessToken=lambda *_: SimpleNamespace(Close=lambda: None),
        GetTokenInformation=lambda _, kind: ('user', 0) if kind == 1 else 'admins',
        CreateWellKnownSid=lambda kind, _: 'system' if kind == 22 else 'admins', GetSecurityInfo=descriptor,
        SetSecurityInfo=set_info, TokenUser=1, TokenOwner=4, WinLocalSystemSid=22,
        WinBuiltinAdministratorsSid=26,
        SE_FILE_OBJECT=1, OWNER_SECURITY_INFORMATION=1, DACL_SECURITY_INFORMATION=4,
        PROTECTED_DACL_SECURITY_INFORMATION=0x80000000, ACL_REVISION=2, ACCESS_ALLOWED_ACE_TYPE=0)
    monkeypatch.setitem(sys.modules, 'win32api', SimpleNamespace(GetCurrentProcess=lambda: 1))
    monkeypatch.setitem(sys.modules, 'win32security', api)
    monkeypatch.setitem(sys.modules, 'ntsecuritycon', SimpleNamespace(
        FILE_READ_ATTRIBUTES=0x80, FILE_ALL_ACCESS=0x1f01ff))
    monkeypatch.setitem(sys.modules, 'win32file', SimpleNamespace(CreateFile=open_directory,
        FILE_FLAG_OPEN_REPARSE_POINT=0x00200000,
        GetFileInformationByHandle=lambda h: (0x400 if Path(h.path).is_symlink() else 0x10,)))
    monkeypatch.setitem(sys.modules, 'win32con', SimpleNamespace(TOKEN_QUERY=8,
        OBJECT_INHERIT_ACE=1, CONTAINER_INHERIT_ACE=2, INHERIT_ONLY_ACE=8,
        READ_CONTROL=0x20000, WRITE_DAC=0x40000, WRITE_OWNER=0x80000,
        FILE_SHARE_READ=1, OPEN_EXISTING=3, FILE_FLAG_BACKUP_SEMANTICS=0x02000000,
        FILE_ATTRIBUTE_REPARSE_POINT=0x400,
        FILE_ATTRIBUTE_DIRECTORY=0x10))
    state.ACL = ACL
    return state


@pytest.mark.parametrize('existing', ['empty', 'empty_requests', 'private_records'])
def test_default_owner_startup_recovery_preserves_records(tmp_path, security, existing):
    folder = tmp_path / 'memory-capture'; folder.mkdir()
    if existing == 'empty_requests': (folder / 'requests').mkdir()
    if existing == 'private_records':
        (folder / 'endpoint.json').write_bytes(b'original diagnostic record')
        security.acls[str(folder)] = security.ACL([((0, 0), 0x1f01ff, 'user'), ((0, 0), 0x1f01ff, 'system')])
    original = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    cp.windows_security(folder, initialize=True)
    cp.windows_security(folder)
    assert security.owners[str(folder)] == 'user'
    assert original == {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    assert security.writes.count(str(folder)) == 2
    assert all(h.closed for h in security.handles)


@pytest.mark.parametrize('case', ['foreign_owner', 'public_records', 'foreign_acl', 'requester'])
def test_no_ownership_takeover_or_untrusted_records(tmp_path, security, case):
    folder = tmp_path / 'memory-capture'; folder.mkdir()
    if case == 'foreign_owner': security.owners[str(folder)] = 'foreign-user'
    if case in ('public_records', 'foreign_acl'):
        (folder / 'endpoint.json').write_bytes(b'untrusted original')
    if case == 'foreign_acl':
        security.acls[str(folder)] = security.ACL([((0, 0), 0x1f01ff, 'foreign-user')])
    with pytest.raises(cp.ChannelSecurityError, match=('channel_permissions_invalid' if case in ('public_records', 'foreign_acl') else 'channel_owner_mismatch')) as err:
        cp.windows_security(folder, initialize=case != 'requester')
    assert not security.writes
    assert {'phase', 'owner_matches_user', 'owner_matches_default',
            'private_acl', 'user_full_access'} <= set(err.value.security_facts)


@pytest.mark.parametrize('entries', [[], [((0, 0), 1, 'user')], [((0, 8), 0x1f01ff, 'user')],
                                   [((0, 0), 0x1f01ff, 'user'), ((0, 0), 1, 'everyone')]])
def test_requester_requires_effective_private_user_access(tmp_path, security, entries):
    security.owners[str(tmp_path)] = 'user'
    security.acls[str(tmp_path)] = security.ACL(entries)
    with pytest.raises(cp.ChannelSecurityError, match='channel_permissions_invalid'):
        cp.windows_security(tmp_path)
    assert not security.writes


def test_failed_owner_update_cannot_publish_endpoint(tmp_path, security, monkeypatch):
    security.no_effect = True
    monkeypatch.setattr(cp, 'WINDOWS', True)
    assert mc.install(tmp_path) is None
    assert not (tmp_path / 'runtime/memory-capture/endpoint.json').exists()
    gap = json.loads((tmp_path / 'artifacts/runtime/memory-capture-gap.json').read_text())
    assert gap['reason'] == 'channel_owner_mismatch'
    assert gap['security_facts']['phase'] == 'initialize'
    assert gap['security_facts']['stage'] == 'verify_security'


def test_recovered_channel_to_actual_profile_and_export(tmp_path, security, monkeypatch):
    import os
    root = tmp_path
    folder = root / 'runtime/memory-capture'
    (folder / 'requests').mkdir(parents=True)
    (root / 'runtime/session.json').write_text(json.dumps({'session_id': 'synthetic-session',
        'pid': os.getpid(), 'status': 'running'}))
    proof = root / 'transactions/original.json'; proof.parent.mkdir()
    proof.write_bytes(b'original unknown-send evidence')
    monkeypatch.setattr(cp, 'WINDOWS', True)
    original_endpoint = cp.Channel.endpoint
    def endpoint(channel):
        for path in (channel.folder, channel.requests): cp.windows_security(path)
        return original_endpoint(channel)
    monkeypatch.setattr(cp.Channel, 'endpoint', endpoint)
    monkeypatch.setattr(cp, 'CAPTURE_SECONDS', 1)
    monkeypatch.setattr(cp, 'POLL_SECONDS', .02)
    worker = mc.install(root, SimpleNamespace(capture_pressure=lambda: None))
    assert worker is not None
    try:
        result = mc.request_capture(root)
        assert result['status'] == 'finished', result
        archive = export_bundle(root, root / 'capture.zip', capture_request=result)
        with zipfile.ZipFile(archive) as z:
            report = json.loads(z.read('logs/runtime/python-memory/' + result['report_file']))
            assert report['pid'] == os.getpid() and report['request_id'] == result['request_id']
            assert report['status'] == 'finished' and report['samples']
    finally: worker.close()
    assert proof.read_bytes() == b'original unknown-send evidence'


def test_missing_profile_is_missing_not_a_path_violation(tmp_path):
    request = {'status': 'failed', 'reason': 'channel_owner_mismatch'}
    archive = export_bundle(tmp_path, tmp_path / 'missing.zip', capture_request=request)
    with zipfile.ZipFile(archive) as z:
        entries = json.loads(z.read('export.json'))['sources']
    for name in ('python-memory-status.json', 'python-memory-gap.json'):
        assert any(e['source'] == 'logs/runtime/' + name and e['status'] == 'missing'
                   and e.get('reason') == 'file_not_found' for e in entries)


def test_missing_profile_behind_link_is_still_forbidden(tmp_path):
    outside = tmp_path / 'outside'; outside.mkdir()
    root = tmp_path / 'home'; (root / 'artifacts').mkdir(parents=True)
    (root / 'artifacts/runtime').symlink_to(outside, target_is_directory=True)
    archive = export_bundle(root, tmp_path / 'linked.zip', capture_request={'status': 'failed'})
    with zipfile.ZipFile(archive) as z:
        entries = json.loads(z.read('export.json'))['sources']
    assert any(e['source'] == 'logs/runtime/python-memory-status.json' and e.get('reason') == 'source_boundary'
               for e in entries)


def test_request_failure_exports_fixed_security_facts_only(tmp_path, security, monkeypatch):
    folder = tmp_path / 'runtime/memory-capture'; folder.mkdir(parents=True)
    security.owners[str(folder)] = 'foreign-user'
    monkeypatch.setattr(cp.Channel, 'endpoint', lambda channel: cp.windows_security(channel.folder))
    result = mc.request_capture(tmp_path)
    assert result['status'] == 'failed' and result['reason'] == 'channel_owner_mismatch'
    assert result['security_facts']['owner_matches_user'] is False
    archive = export_bundle(tmp_path, tmp_path / 'failed.zip', capture_request=result)
    with zipfile.ZipFile(archive) as z:
        saved = json.loads(z.read('memory/request.json'))
        assert saved['security_facts'] == result['security_facts']
        assert b'foreign-user' not in z.read('memory/request.json')
    assert not security.writes


@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows owner/DACL APIs')
def test_native_windows_created_channel_survives_restart(tmp_path):
    channel = cp.Channel(tmp_path)
    try:
        channel.initialize()
    except cp.ChannelSecurityError as exc:
        import win32api, win32con, win32security
        token=win32security.OpenProcessToken(win32api.GetCurrentProcess(),win32con.TOKEN_QUERY)
        try:
            user=win32security.GetTokenInformation(token,win32security.TokenUser)[0]
        finally:token.Close()
        principals={str(user):'current_user'}
        for kind,label in ((win32security.WinLocalSystemSid,'system'),(win32security.WinBuiltinAdministratorsSid,'admins')):
            principals[str(win32security.CreateWellKnownSid(kind,None))]=label
        acl=win32security.GetFileSecurity(str(channel.folder),win32security.DACL_SECURITY_INFORMATION).GetSecurityDescriptorDacl()
        entries=[{'kind':a[0][0],'flags':a[0][1],'mask':a[1],'principal':principals.get(str(a[-1]),'other')} for a in (acl.GetAce(i) for i in range(acl.GetAceCount()))]
        pytest.fail(json.dumps({'security_facts':exc.security_facts,'acl_categories':entries}))
    channel.write(channel.requests / ('a'*32+'.json'), {'synthetic': 'preserved'})
    cp.Channel(tmp_path).initialize()
    assert channel.read(channel.requests / ('a'*32+'.json')) == {'synthetic': 'preserved'}
    for folder in (channel.folder, channel.requests): cp.windows_security(folder)


@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows elevated default-owner token')
def test_native_windows_interrupted_empty_default_owner_bootstrap(tmp_path):
    import win32api, win32con, win32security
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        owner = win32security.GetTokenInformation(token, win32security.TokenOwner)
    finally: token.Close()
    if user == owner: pytest.skip('default owner equals user; elevated-token case not present')
    channel = cp.Channel(tmp_path); channel.requests.mkdir(parents=True)
    cp.Channel(tmp_path).initialize()
    for folder in (channel.folder, channel.requests): cp.windows_security(folder)


@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows sharing and junction behavior')
def test_native_windows_pins_hold_namespace_through_publication(tmp_path):
    import subprocess
    folder = tmp_path / 'runtime/memory-capture'
    folder.mkdir(parents=True)
    outside = tmp_path / 'outside'; outside.mkdir()
    channel = cp.Channel(tmp_path)
    with channel.initialization():
        # Directory/ancestor rename must be denied while initialization holds pins.
        for target in (folder, folder.parent):
            with pytest.raises(OSError): target.rename(target.with_name(target.name + '-moved'))
        value = {'schema': cp.SCHEMA, 'operation': 'memory_capture', 'target': {'synthetic': True}}
        channel.publish(value)
    assert channel.read(folder / 'endpoint.json') == value
    # Existing reparse points must be rejected before any security mutation.
    linkroot = tmp_path / 'junction-home'; (linkroot / 'runtime').mkdir(parents=True)
    junction = linkroot / 'runtime/memory-capture'
    subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(junction), str(outside)], check=True, capture_output=True)
    try:
        with pytest.raises(ValueError, match='channel_link_boundary'):
            cp.Channel(linkroot).initialize()
        assert list(outside.iterdir()) == []
    finally:
        junction.rmdir()
