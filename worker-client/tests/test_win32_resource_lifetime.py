"""Real DB/inspection owners; only Win32 calls are substituted (not Windows UAT)."""
import ctypes
import sys
from types import SimpleNamespace
import pytest
from chejin_worker_client import storage, update_data_access as access
from chejin_worker_client.hook_process import _wechat_process_ids
from test_hook_process_enumeration import Kernel, Function


def test_process_snapshots_release_handles_and_reuse_one_abi():
    types = set()
    for i in range(300):
        kernel = Kernel([(i + 1, 'WeChat.exe')])
        assert _wechat_process_ids(kernel) == (i + 1,)
        assert kernel.closed == [7]
        types.add(kernel.Process32FirstW.argtypes[1])
    # Old implementation retained one new type per call in ctypes' global cache.
    assert len(types) == 1


@pytest.fixture
def windows_lock_api(monkeypatch):
    state = SimpleNamespace(lock_types=set(), locked=0, unlocked=0, fail_lock=False)
    def lock(*args):
        state.lock_types.add(type(args[-1]._obj))
        if state.fail_lock: return 0
        state.locked += 1
        return 1
    def unlock(*args):
        state.unlocked += 1
        return 1
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **k: SimpleNamespace(
        LockFileEx=Function(lock), UnlockFileEx=Function(unlock)), raising=False)
    monkeypatch.setattr(ctypes, 'WinError', lambda *a: OSError('synthetic lock error'), raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 5, raising=False)
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(get_osfhandle=lambda fd: fd))
    monkeypatch.setattr(access, 'os', SimpleNamespace(name='nt'))
    return state


def test_database_connections_reuse_abi_but_close_each_lock(tmp_path, monkeypatch, windows_lock_api):
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path / 'worker_client.sqlite3')
    for i in range(200):
        with storage.db_connection() as db:
            guard = db.data_access
            db.execute('CREATE TABLE IF NOT EXISTS synthetic_work (id INTEGER PRIMARY KEY)')
            db.execute('INSERT INTO synthetic_work VALUES (?)', (i,))
            db.commit()
        assert guard.closed and guard.file.closed
    with storage.db_connection() as db:
        assert db.execute('SELECT COUNT(*) FROM synthetic_work').fetchone()[0] == 200
    assert windows_lock_api.locked == windows_lock_api.unlocked == 201
    assert len(windows_lock_api.lock_types) == 1


def test_lock_state_is_independent_for_concurrent_owners(tmp_path, windows_lock_api):
    with access.DataAccess(tmp_path/'a.lock', False) as first:
        with access.DataAccess(tmp_path/'b.lock', False) as second:
            assert first.windows_lock.overlapped is not second.windows_lock.overlapped
            assert first.file.fileno() != second.file.fileno()
    assert first.file.closed and second.file.closed
    assert windows_lock_api.locked == windows_lock_api.unlocked == 2


def test_database_failure_closes_guard_and_does_not_commit(tmp_path, monkeypatch, windows_lock_api):
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path / 'worker_client.sqlite3')
    with storage.db_connection() as db:
        db.execute('CREATE TABLE synthetic_work (id INTEGER PRIMARY KEY)'); db.commit()
    with pytest.raises(ValueError, match='cancel'):
        with storage.db_connection() as db:
            guard = db.data_access
            db.execute('INSERT INTO synthetic_work VALUES (1)')
            raise ValueError('cancel')
    assert guard.closed and guard.file.closed
    with storage.db_connection() as db:
        assert db.execute('SELECT COUNT(*) FROM synthetic_work').fetchone()[0] == 0
    assert windows_lock_api.locked == windows_lock_api.unlocked == 3


def test_denied_lock_never_grants_database_access(tmp_path, windows_lock_api):
    windows_lock_api.fail_lock = True
    with pytest.raises(RuntimeError, match='UPDATE_DATA_DIRECTORY_BUSY'):
        access.acquire_data_access(tmp_path/'data')
    assert windows_lock_api.locked == windows_lock_api.unlocked == 0
