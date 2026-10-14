from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen

import pytest

from chejin_worker_client import storage
from chejin_worker_client.hook_inbox import HookReceiveScope, NativeInbox
from chejin_worker_client.hook_projection import NativeProjectionStore
from chejin_worker_client.hook_receiver import create_receiver
from chejin_worker_client.update_data_access import DataAccess, lock_path
from chejin_worker_client.update_data_snapshot import protected_update_snapshot, assert_protected_update_snapshot, SnapshotMismatch

SCOPE = HookReceiveScope("wxid_test_sales", "epoch-1", "process-1", 30001)


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "APP_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "worker_client.sqlite3")
    return tmp_path


def sample(mid="6594133108973694888", **envelope):
    return json.dumps({"ServerPort": "30001", "selfwxid": SCOPE.self_wxid,
        "sendorrecv": "2", "msgnumber": "1", "msglist": [{"msgtype": "1", "msgsvrid": mid,
        "fromtype": "1", "fromid": "wxid_test_A", "toid": SCOPE.self_wxid,
        "msg": "预算12.5万元\n暂时不要报价🙂", "time": "2026-10-06 14:39:44"}], **envelope}, ensure_ascii=False).encode()


def inbox():
    return NativeInbox(max_body_bytes=4096, max_inbox_bytes=32768)


def projector(**fields):
    return NativeProjectionStore(**{"max_projection_bytes": 32768, "max_entries": 10, **fields})


def test_http_to_durable_raw_to_projected_observation_and_restart(local_db):
    failures = []
    server = create_receiver(port=0, inbox=inbox(), scope=SCOPE, request_timeout_seconds=1,
        on_persistence_failure=lambda: failures.append("fault"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for mid in ("6594133108973694888", "1125348102583403829"):
            with urlopen(Request(f"http://127.0.0.1:{server.server_address[1]}/receiveChatBotMsg",
                    data=sample(mid), headers={"Content-Type": "application/json"}), timeout=2) as response:
                assert response.status == 200
        first, second = projector().project_next(SCOPE), projector().project_next(SCOPE)
        assert first["receive_seq"] == 1 and second["receive_seq"] == 2
        assert first["observations"][0]["native_msg_id"] != second["observations"][0]["native_msg_id"]
        assert first["observations"][0]["content"] == second["observations"][0]["content"]
        assert first["observed_at"] and first["raw_evidence_digest"]
        assert projector().project_next(SCOPE) is None
        with storage.db_connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM hook_observation_projection").fetchone()[0] == 2
        assert failures == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_package_quarantine_is_durable_without_inventing_a_partial_batch(local_db):
    inbox().persist(sample(msgnumber="2"), SCOPE)
    projected = projector().project_next(SCOPE)
    assert projected["status"] == "quarantined" and projected["observations"] == []
    assert projected["reason_code"] == "HOOK_CALLBACK_MESSAGE_COUNT_INVALID"
    assert projector().project_next(SCOPE) is None


def test_scope_is_explicit_and_old_events_are_not_relabelled(local_db):
    inbox().persist(sample(), SCOPE)
    other = HookReceiveScope(SCOPE.self_wxid, "epoch-2", "process-2", 30001)
    assert projector().project_next(other) is None
    observation = projector().project_next(SCOPE)["observations"][0]
    assert observation["account_epoch"] == "epoch-1" and observation["process_generation"] == "process-1"


def test_tampered_raw_and_capacity_failure_do_not_advance_projection(local_db):
    inbox().persist(sample(), SCOPE)
    with pytest.raises(RuntimeError, match="CAPACITY_EXCEEDED"):
        projector(max_projection_bytes=1).project_next(SCOPE)
    with storage.db_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM hook_raw_inbox").fetchone()[0] == 1
        conn.execute("UPDATE hook_raw_inbox SET raw_body=?", (b'changed',))
        conn.commit()
    with pytest.raises(RuntimeError, match="RAW_EVIDENCE_CHANGED"):
        projector().project_next(SCOPE)


def test_update_gate_and_snapshot_cover_projected_facts_without_exporting_body(local_db):
    inbox().persist(sample(), SCOPE)
    with DataAccess(lock_path(local_db), True):
        with pytest.raises(RuntimeError, match="UPDATE_DATA_DIRECTORY_BUSY"):
            projector().project_next(SCOPE)
    projector().project_next(SCOPE)
    baseline = protected_update_snapshot(data_dir=local_db, digest_key="test")
    assert baseline["hook_projection_v1"]["row_count"] == 1
    assert "预算" not in json.dumps(baseline, ensure_ascii=False)
    assert "wxid_test_A" not in json.dumps(baseline)
    assert_protected_update_snapshot(baseline, data_dir=local_db, digest_key="test")
    with storage.db_connection() as conn:
        conn.execute("UPDATE hook_observation_projection SET projection_json='changed'")
        conn.commit()
    with pytest.raises(SnapshotMismatch):
        assert_protected_update_snapshot(baseline, data_dir=local_db, digest_key="test")


def test_two_projectors_cannot_commit_the_same_package_twice(local_db):
    for mid in ("6594133108973694888", "1125348102583403829"):
        inbox().persist(sample(mid), SCOPE)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: projector().project_next(SCOPE), range(2)))
    assert sorted(r["receive_seq"] for r in results) == [1, 2]
    assert projector().project_next(SCOPE) is None
