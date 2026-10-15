"""Normal reads must not repeat initialization writes or wait on a writer."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from chejin_worker_client import storage

@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path / 'worker_client.sqlite3')
    monkeypatch.setattr(storage, '_post_update_initialized_database', None)
    monkeypatch.setattr(storage, '_initialized_database_identity', None)
    return tmp_path


def test_read_connection_does_not_request_write_lock(database):
    with storage.db_connection() as writer:
        writer.execute("INSERT INTO client_settings VALUES ('synthetic','retained','test')")
        # WAL permits a separate reader of committed data while this write is open.
        with storage.db_connection() as reader:
            assert reader.execute('SELECT COUNT(*) FROM client_settings').fetchone()[0] == 0
        writer.commit()
    with storage.db_connection() as reader:
        assert reader.execute('SELECT COUNT(*) FROM client_settings').fetchone()[0] == 1


def test_parallel_first_connections_initialize_once(database, monkeypatch):
    original = storage.init_db
    calls = []
    def initialize(conn):
        calls.append(True)
        original(conn)
    monkeypatch.setattr(storage, 'init_db', initialize)
    barrier = threading.Barrier(4)
    def read(_):
        barrier.wait()
        with storage.db_connection() as conn:
            return conn.execute('SELECT COUNT(*) FROM binding').fetchone()[0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(read, range(4))) == [0] * 4
    assert calls == [True]


def test_failed_initialization_is_not_cached(database, monkeypatch):
    original = storage.init_db
    def broken(conn):
        raise sqlite3.OperationalError('synthetic initialization failure')
    monkeypatch.setattr(storage, 'init_db', broken)
    with pytest.raises(sqlite3.OperationalError):
        storage.connect()
    monkeypatch.setattr(storage, 'init_db', original)
    with storage.db_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM binding').fetchone()[0] == 0


def test_replaced_database_is_initialized_again(database):
    with storage.db_connection():
        pass
    # Offline file replacement is a different database despite the same path.
    old = storage.DB_FILE.with_suffix('.old')
    storage.DB_FILE.rename(old)
    with storage.db_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM binding').fetchone()[0] == 0


def test_initialized_read_needs_no_write_but_real_writes_still_fail(database, monkeypatch):
    with storage.db_connection():
        pass
    connect = sqlite3.connect
    def readonly(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.execute('PRAGMA query_only=ON')
        return conn
    monkeypatch.setattr(sqlite3, 'connect', readonly)
    with storage.db_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM binding').fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        storage.append_log('INFO', 'synthetic', 'must not silently accept a failed write')
