from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from chejin_worker_client import storage
from chejin_worker_client.hook_config import HookDeployment, load_hook_deployment
from chejin_worker_client.hook_inbox import HookReceiveScope, NativeInbox
from chejin_worker_client.hook_receiver import create_receiver
from chejin_worker_client.update_data_access import DataAccess, lock_path
from chejin_worker_client.update_data_snapshot import protected_update_snapshot, assert_protected_update_snapshot, SnapshotMismatch


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "APP_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "worker_client.sqlite3")
    return tmp_path


@pytest.fixture
def scope():
    return HookReceiveScope("wxid_test_sales", "epoch-1", "process-1", 30001)


def sample(scope, *, native_id="6594133108973694888", **fields):
    return json.dumps({"ServerPort": str(scope.hook_api_port), "selfwxid": scope.self_wxid,
        "sendorrecv": "2", "msgnumber": "1", "msglist": [{"index": "1", "msgtype": "1",
        "msgsvrid": native_id, "msg": "预算12.5万元\n暂时不要报价🙂", **fields}]},
        ensure_ascii=False).encode("utf-8")


def deployment():
    return HookDeployment("deployment-1", r"C:\Test WeChat\WeChat.exe", "a" * 64, 30001, 8000)


def test_one_protected_configuration_derives_both_endpoints(local_db):
    with pytest.raises(ValueError, match="HOOK_DEPLOYMENT_MISSING"):
        load_hook_deployment()
    with storage.db_connection() as conn:
        conn.execute("INSERT INTO client_settings VALUES (?,?,?)",
                     ("hook_deployment_v1", json.dumps(asdict(deployment())), "test"))
        conn.commit()
    loaded = load_hook_deployment()
    assert loaded.api_base_url == "http://127.0.0.1:30001"
    assert loaded.callback_url == "http://127.0.0.1:8000/receiveChatBotMsg"
    assert loaded.dll_path == r"C:\Test WeChat\version.dll"
    assert loaded.launch_arguments == ("StartPort=30001", "ConnectType=http", "RecvType=0")


@pytest.mark.parametrize("change", [dict(hook_api_port=8000), dict(receiver_port=True),
    dict(hook_api_port=65536), dict(wechat_exe=r"relative\WeChat.exe"),
    dict(wechat_exe=r"\\remote\share\WeChat.exe"), dict(dll_sha256="bad")])
def test_invalid_deployment_fails_closed(change):
    with pytest.raises(ValueError):
        HookDeployment(**{**asdict(deployment()), **change})


def test_key_is_not_a_deployment_setting(local_db):
    with storage.db_connection() as conn:
        conn.execute("INSERT INTO client_settings VALUES (?,?,?)", ("hook_deployment_v1",
            json.dumps({**asdict(deployment()), "key": "test-secret"}), "test"))
        conn.commit()
    with pytest.raises(ValueError, match="FIELDS_INVALID"):
        load_hook_deployment()


def test_native_ids_media_updates_and_no_id_observations_are_not_swallowed(local_db, scope):
    inbox = NativeInbox(max_body_bytes=4096, max_inbox_bytes=32768)
    raw = sample(scope)
    other = sample(scope, native_id="1125348102583403829")
    audio = sample(scope, voice_base64="dGVzdA==")
    no_id = sample(scope, native_id="")
    results = [inbox.persist(value, scope) for value in [raw, other, audio, no_id, no_id]]
    assert [x["receive_seq"] for x in results] == [1, 2, 3, 4, 5]
    with storage.db_connection() as conn:
        rows = conn.execute("SELECT raw_body FROM hook_raw_inbox ORDER BY receive_seq").fetchall()
    assert [bytes(x["raw_body"]) for x in rows] == [raw, other, audio, no_id, no_id]
    assert json.loads(rows[0]["raw_body"])["msglist"][0]["msgsvrid"] == "6594133108973694888"
    # A new inbox instance after restart continues the durable receive order.
    assert NativeInbox(max_body_bytes=4096, max_inbox_bytes=32768).persist(raw, scope)["receive_seq"] == 6


def test_wrong_account_and_ambiguous_json_are_rejected(local_db, scope):
    inbox = NativeInbox(max_body_bytes=4096, max_inbox_bytes=32768)
    wrong = HookReceiveScope("another-account", "epoch-1", "process-1", 30001)
    for raw in [sample(wrong), b'[]', b'{"selfwxid":"one","selfwxid":"two"}',
                b'{"selfwxid":NaN}', b'\xff']:
        with pytest.raises(ValueError):
            inbox.persist(raw, scope)


def test_update_exclusive_lock_blocks_intake(local_db, scope):
    inbox = NativeInbox(max_body_bytes=4096, max_inbox_bytes=32768)
    storage.load_runtime_control()  # Initialize the existing database first.
    with DataAccess(lock_path(local_db), True):
        with pytest.raises(RuntimeError, match="UPDATE_DATA_DIRECTORY_BUSY"):
            inbox.persist(sample(scope), scope)


def test_capacity_failure_keeps_existing_records(local_db, scope):
    raw = sample(scope)
    inbox = NativeInbox(max_body_bytes=len(raw), max_inbox_bytes=len(raw))
    inbox.persist(raw, scope)
    with pytest.raises(RuntimeError, match="CAPACITY_EXCEEDED"):
        inbox.persist(raw, scope)
    with storage.db_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM hook_raw_inbox").fetchone()[0] == 1


def test_update_snapshot_protects_hook_facts_without_exporting_customer_body(local_db, scope):
    storage.load_runtime_control()
    assert "hook_inbox_v1" not in protected_update_snapshot(data_dir=local_db, digest_key="test")
    inbox = NativeInbox(max_body_bytes=4096, max_inbox_bytes=32768)
    inbox.persist(sample(scope), scope)
    baseline = protected_update_snapshot(data_dir=local_db, digest_key="test")
    assert baseline["hook_inbox_v1"]["row_count"] == 1
    assert "预算" not in json.dumps(baseline, ensure_ascii=False)
    assert "wxid_test_sales" not in json.dumps(baseline)
    assert_protected_update_snapshot(baseline, data_dir=local_db, digest_key="test")
    with storage.db_connection() as conn:
        conn.execute("UPDATE hook_raw_inbox SET raw_body=?", (sqlite3.Binary(b"changed"),))
        conn.commit()
    with pytest.raises(SnapshotMismatch, match="UPDATE_PROTECTED_DATABASE_CHANGED"):
        assert_protected_update_snapshot(baseline, data_dir=local_db, digest_key="test")


def test_http_public_entry_commits_before_ack_and_reports_real_write_failure(local_db, scope):
    failures = []
    raw = sample(scope)
    server = create_receiver(port=0, inbox=NativeInbox(max_body_bytes=len(raw), max_inbox_bytes=len(raw)),
        scope=scope, request_timeout_seconds=1, on_persistence_failure=lambda: failures.append("fault"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    def post(body, path="/receiveChatBotMsg"):
        return urlopen(Request(base + path, data=body, headers={"Content-Type": "application/json"}), timeout=2)
    try:
        with post(raw) as response:
            assert response.status == 200
        # Read through a separate connection: response proves a committed record.
        with sqlite3.connect(storage.DB_FILE) as conn:
            assert conn.execute("SELECT raw_body FROM hook_raw_inbox").fetchone()[0] == raw
        for body, path, status in [(raw, "/wrong", 404), (b'[]', "/receiveChatBotMsg", 400),
            (raw + b' ', "/receiveChatBotMsg", 413), (raw, "/receiveChatBotMsg", 503)]:
            with pytest.raises(HTTPError) as exc:
                post(body, path)
            assert exc.value.code == status
        assert failures == ["fault"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_port_occupied_is_not_silently_reused(scope):
    inbox = NativeInbox(max_body_bytes=4096, max_inbox_bytes=32768)
    server = create_receiver(port=0, inbox=inbox, scope=scope, request_timeout_seconds=1,
                             on_persistence_failure=lambda: None)
    try:
        with pytest.raises(OSError):
            create_receiver(port=server.server_address[1], inbox=inbox, scope=scope,
                            request_timeout_seconds=1, on_persistence_failure=lambda: None)
    finally:
        server.server_close()
