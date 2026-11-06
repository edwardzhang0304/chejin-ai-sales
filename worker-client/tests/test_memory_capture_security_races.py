"""Real filesystem mutations + Win32 handle/API model; native coverage is separate."""
import json
import os
from pathlib import Path
import sys
import zipfile

import pytest

from test_memory_capture_windows_security import security
from chejin_worker_client import memory_capture as mc, memory_capture_channel as cp
from chejin_worker_client.diagnostic_bundle import export_bundle


def setup(root, monkeypatch):
    folder = root / 'runtime/memory-capture'
    folder.mkdir(parents=True)
    (root / 'runtime/session.json').write_text(json.dumps({'session_id': 'synthetic-session',
        'pid': os.getpid(), 'status': 'running'}))
    monkeypatch.setattr(cp, 'WINDOWS', True)
    proof = root / 'transactions/unknown-send.json'
    proof.parent.mkdir()
    proof.write_bytes(b'original unknown-send receipt')
    return folder, proof


@pytest.mark.parametrize('when', ['before_check', 'after_check', 'set_dacl', 'set_owner',
                                 'verify', 'publish', 'link'])
def test_record_arriving_at_each_boundary_is_preserved(tmp_path, security, monkeypatch, when):
    folder, proof = setup(tmp_path, monkeypatch)
    endpoint = folder / 'endpoint.json'
    original = b'untrusted record must survive'
    inserted = False
    def insert():
        nonlocal inserted
        if not inserted: endpoint.write_bytes(original); inserted = True
    if when == 'before_check': insert()
    empty = cp._empty_bootstrap
    def inspect(path):
        result = empty(path)
        if when == 'after_check' and path == folder: insert()
        return result
    monkeypatch.setattr(cp, '_empty_bootstrap', inspect)
    api = sys.modules['win32security']
    setter, reader = api.SetSecurityInfo, api.GetSecurityInfo
    def set_info(h, kind, flags, owner, group, acl, sacl):
        if h.path == str(folder) and when == ('set_owner' if owner else 'set_dacl'): insert()
        return setter(h, kind, flags, owner, group, acl, sacl)
    def read_info(h, *args):
        if when == 'verify' and h.path == str(folder) and str(folder) in security.writes: insert()
        return reader(h, *args)
    monkeypatch.setattr(api, 'SetSecurityInfo', set_info)
    monkeypatch.setattr(api, 'GetSecurityInfo', read_info)
    publish = cp.Channel.publish
    def publish_with_arrival(channel, value):
        if when == 'publish': insert()
        return publish(channel, value)
    monkeypatch.setattr(cp.Channel, 'publish', publish_with_arrival)
    link = os.link
    def link_with_arrival(src, dst, *args, **kwargs):
        if when == 'link' and Path(dst) == endpoint: insert()
        return link(src, dst, *args, **kwargs)
    monkeypatch.setattr(os, 'link', link_with_arrival)
    worker = mc.install(tmp_path)
    try:
        assert inserted
        assert worker is None
        assert endpoint.read_bytes() == original
        # A partially normalized ACL must not make the next startup overwrite it.
        retry = mc.install(tmp_path)
        try:
            assert retry is None
            assert endpoint.read_bytes() == original
        finally:
            if retry: retry.close()
        assert proof.read_bytes() == b'original unknown-send receipt'
        assert all(h.closed for h in security.handles)
    finally:
        if worker: worker.close()


@pytest.mark.parametrize('when', ['before_check', 'after_check', 'set_dacl', 'verify', 'publish'])
def test_path_replacement_never_changes_outside_object(tmp_path, security, monkeypatch, when):
    root = tmp_path / 'home'
    folder, proof = setup(root, monkeypatch)
    outside = tmp_path / 'outside'; outside.mkdir(); outside.chmod(0o755)
    original = outside / 'record'; original.write_bytes(b'outside untouched')
    swapped = False
    def swap():
        nonlocal swapped
        if not swapped:
            folder.rename(folder.with_name('original-folder'))
            folder.symlink_to(outside, target_is_directory=True)
            swapped = True
    if when == 'before_check': swap()
    empty = cp._empty_bootstrap
    def inspect(path):
        result = empty(path)
        if when == 'after_check' and path == folder: swap()
        return result
    monkeypatch.setattr(cp, '_empty_bootstrap', inspect)
    api = sys.modules['win32security']
    setter, reader = api.SetSecurityInfo, api.GetSecurityInfo
    def set_info(h, *args):
        if when == 'set_dacl' and h.path == str(folder): swap()
        return setter(h, *args)
    def read_info(h, *args):
        if when == 'verify' and h.path == str(folder) and str(folder) in security.writes: swap()
        return reader(h, *args)
    monkeypatch.setattr(api, 'SetSecurityInfo', set_info)
    monkeypatch.setattr(api, 'GetSecurityInfo', read_info)
    publish = cp.Channel.publish
    def publish_with_swap(channel, value):
        if when == 'publish': swap()
        return publish(channel, value)
    monkeypatch.setattr(cp.Channel, 'publish', publish_with_swap)
    worker = mc.install(root)
    try:
        assert swapped and worker is None
        assert outside.stat().st_mode & 0o777 == 0o755
        assert original.read_bytes() == b'outside untouched'
        assert not (outside / 'endpoint.json').exists()
        assert proof.read_bytes() == b'original unknown-send receipt'
        assert all(h.closed for h in security.handles)
    finally:
        if worker: worker.close()


@pytest.mark.parametrize('stage', ['open_token', 'read_token_user', 'read_token_owner',
    'open_directory', 'read_directory', 'read_security', 'set_dacl', 'set_owner', 'verify_security'])
def test_native_error_keeps_stage_code_and_facts_in_export(tmp_path, security, monkeypatch, stage):
    folder, proof = setup(tmp_path, monkeypatch)
    class error(Exception): pass
    api = sys.modules['win32security']
    def deny(*args): raise error(5, 'native-api', 'SECRET SID path customer text')
    if stage == 'open_token': monkeypatch.setattr(api, 'OpenProcessToken', deny)
    elif stage.startswith('read_token'):
        get = api.GetTokenInformation
        def token_info(token, kind):
            if kind == (api.TokenUser if stage == 'read_token_user' else api.TokenOwner): deny()
            return get(token, kind)
        monkeypatch.setattr(api, 'GetTokenInformation', token_info)
    elif stage in ('open_directory', 'read_directory'):
        monkeypatch.setattr(sys.modules['win32file'],
            'CreateFile' if stage == 'open_directory' else 'GetFileInformationByHandle', deny)
    elif stage in ('set_owner', 'set_dacl'):
        set_info = api.SetSecurityInfo
        def set_failure(h, kind, flags, owner, group, acl, sacl):
            if stage == ('set_owner' if owner else 'set_dacl'): deny()
            return set_info(h, kind, flags, owner, group, acl, sacl)
        monkeypatch.setattr(api, 'SetSecurityInfo', set_failure)
    else:
        get_info = api.GetSecurityInfo
        def read_failure(h, *args):
            if stage == 'read_security' or str(folder) in security.writes: deny()
            return get_info(h, *args)
        monkeypatch.setattr(api, 'GetSecurityInfo', read_failure)
    assert mc.install(tmp_path) is None
    assert not (folder / 'endpoint.json').exists()
    result = mc.request_capture(tmp_path)
    archive = export_bundle(tmp_path, tmp_path / 'failed.zip', capture_request=result)
    with zipfile.ZipFile(archive) as z:
        gap = json.loads(z.read('logs/runtime/memory-capture-gap.json'))
        assert gap['reason'] == 'channel_security_' + stage + '_failed'
        facts = gap['security_facts']
        assert facts['stage'] == stage and facts['windows_error_code'] == 5
        if stage in ('set_dacl', 'set_owner', 'verify_security'):
            assert facts['owner_matches_default'] is True
            assert facts['bootstrap_recovery'] is True
        if stage in ('open_token', 'read_token_user', 'read_token_owner', 'open_directory',
                     'read_directory', 'read_security'):
            saved = json.loads(z.read('memory/request.json'))
            assert saved['security_facts']['stage'] == stage
            assert saved['security_facts']['phase'] == 'validate'
        assert not any(b'SECRET' in z.read(n) for n in z.namelist())
    assert proof.read_bytes() == b'original unknown-send receipt'
    assert json.loads((tmp_path / 'runtime/session.json').read_text())['status'] == 'running'
    assert all(h.closed for h in security.handles)


@pytest.mark.parametrize('principal', ['everyone', 'foreign-user'])
def test_even_empty_cross_account_writable_directory_is_not_repaired(tmp_path, security, principal):
    security.acls[str(tmp_path)] = security.ACL([((0, 0), 0xffff, 'user'), ((0, 0), 0xffff, principal)])
    with pytest.raises(cp.ChannelSecurityError, match='channel_permissions_invalid'):
        cp.windows_security(tmp_path, initialize=True)
    assert not security.writes


def test_error_evidence_filters_raw_text_and_invalid_codes():
    err = cp.ChannelSecurityError('channel_owner_mismatch', {'phase': 'initialize',
        'stage': 'SECRET', 'owner_matches_user': 'S-SECRET', 'sid': 'S-SECRET',
        'windows_error_code': 'SECRET', 'private_acl': False})
    assert err.security_facts == {'phase': 'initialize', 'private_acl': False}


@pytest.mark.parametrize('stage', ['set_owner', 'set_dacl'])
def test_successful_api_without_effect_is_rejected(tmp_path, security, monkeypatch, stage):
    folder, _ = setup(tmp_path, monkeypatch)
    api = sys.modules['win32security']
    setter = api.SetSecurityInfo
    def no_effect(h, kind, flags, owner, group, acl, sacl):
        if stage != ('set_owner' if owner else 'set_dacl'):
            return setter(h, kind, flags, owner, group, acl, sacl)
    monkeypatch.setattr(api, 'SetSecurityInfo', no_effect)
    assert mc.install(tmp_path) is None
    assert not (folder / 'endpoint.json').exists()
    gap = json.loads((tmp_path / 'artifacts/runtime/memory-capture-gap.json').read_text())
    assert gap['security_facts']['stage'] == 'verify_security'
    assert gap['reason'] == ('channel_owner_mismatch' if stage == 'set_owner' else 'channel_permissions_invalid')
