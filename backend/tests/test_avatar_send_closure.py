"""Real Worker read/send entrypoints automatically settle the original frames.

Windows/Bridge process I/O and unused Vision configuration are controlled.
Earlier voice commits and the approved reply are explicit preconditions.
OCR, grouping, C2 refresh/checkpoint, SQLite, HTTP, receipts, unlock and Flow
settlement are production code, not test callbacks. This is not Windows UAT.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
from contextlib import ExitStack
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "worker-client" / "tests"))
from test_frame_avatars import incident_frames, replay_send_transport, TEXT, FIXTURES, HASHES, s
import test_c3_api as backend


def test_original_send_bridge_sqlite_http_flow_closure(tmp_path, request):
    if os.environ.get("CHEJIN_AVATAR_CLOSURE_CHILD") != "1":
        env = {**os.environ, "CHEJIN_AVATAR_CLOSURE_CHILD": "1",
            "CHEJIN_WORKER_HOME": str(tmp_path / "worker"),
            "DATABASE_URL": f"sqlite:///{tmp_path / 'backend.sqlite3'}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(str(ROOT / p) for p in (
                "backend", "backend/tests", "worker-client", "worker-client/omniauto-rpa"))}
        completed = subprocess.run([sys.executable, "-m", "pytest", __file__, "-q", "--tb=short",
            "--basetemp=" + str(tmp_path / "pytest")],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=360)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return
    frames = request.getfixturevalue("incident_frames")
    from chejin_worker_client import storage, omniauto_vision
    from chejin_worker_client.api import WorkerApiClient
    from chejin_worker_client.models import Binding, Task
    from chejin_worker_client.rpa_bridge import RpaBridge
    from chejin_worker_client.task_runner import TaskRunner
    from chejin_worker_client.ui_lock import lock_summary
    from app.models.worker import Worker

    assert any(Path(storage.DB_FILE).resolve().is_relative_to(p.resolve()) for p in (Path(tempfile.gettempdir()), Path("/private/tmp")))
    backend.setup_function()
    worker = backend._create_worker()
    backend._create_sales(worker["id"])
    backend._create_lead(remark_code="CJ8R8A35")
    session = backend._scan(worker, remark_code="CJ8R8A35")
    conversation_id = session["conversation_id"]
    with backend.SessionLocal() as db:
        conversation = db.get(backend.Conversation, conversation_id)
        conversation.friend_state = "friend_active"
        conversation.status = "waiting_user_reply"
        db.commit()

    api = WorkerApiClient("http://testserver/api")
    requests = []
    class HttpTransport:
        def request(self, method, url, **kwargs):
            kwargs.pop("timeout", None)
            if url.endswith("/sent-ack"):
                assert lock_summary()["locked"]
                assert api.task_lease_fencing_tokens  # leased BEFORE authoritative settlement
                record = storage.list_reply_send_ack_outbox()[0]
                assert record["status"] == "waiting"  # durable BEFORE HTTP
                assert record["ack_payload"]["send_result"] == "sent"
            if url.endswith("/inflight-flow/finish"):
                assert not lock_summary()["locked"]
                assert not storage.has_pending_reply_send_ack_outbox()
                assert any(path.endswith("/sent-ack") and status == 200 for _, path, status in requests)
            response = backend.client.request(method, url, **kwargs)
            requests.append((method, url, response.status_code))
            return response
    api.session = HttpTransport()
    binding = Binding(worker_id=worker["id"], worker_token=worker["worker_token"], client_instance_id="client-c3", run_status="running")
    api.set_run_status(binding, "running")
    storage.save_binding(binding)
    bridge = RpaBridge(sidecar_script=ROOT / "worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py")
    bridge.mode = "real"
    errors, results, actions = [], [], []
    runner = TaskRunner(api, bridge, on_profile=lambda _: None, on_status=lambda _: None,
        on_step=lambda _: None, on_task=lambda _: None, on_result=results.append, on_error=errors.append)
    runner.binding = binding
    before_image, layout, before = frames[0]
    geometry = before["validation"]["geometry"]

    def process_io(args, **_kwargs):
        action = args[0]
        actions.append(action)
        if action == "send":
            assert lock_summary()["locked"]
            return json.loads(json.dumps(s.sanitize_sidecar_contract_output(
                replay_send_transport(args, frames)), ensure_ascii=False))
        assert action in {"messages", "open-chat"}, args
        # The actual raw-frame C2 producer: no preassembled observations,
        # no hand-filled comparisons, and no mock OCR/avatars.
        with (
            patch.object(s, "capture_wechat", return_value=(before_image, str(FIXTURES / next(iter(HASHES))))),
            patch.object(s, "get_window_geometry", return_value=geometry),
            patch.object(s, "window_dpi_scale", return_value=1),
        ):
            payload = s.messages_payload(1, {"ok": True}, target="CJ8R8A35", history_load_times=0,
                max_scroll_steps=0, max_snapshots=1, confirm_target="CJ8R8A35", confirm_exact=True,
                chat_fact_roi_ocr="--chat-fact-roi-ocr" in args)
        assert payload["ok"], payload
        if action == "open-chat":
            # Only locating the already-open Windows chat is controlled; the
            # target proof is produced by real OCR/target validation above.
            payload = {"ok": True, "guard": payload["target_confirmation"],
                "initial_messages_snapshot": payload, "state": "chat_target_confirmed"}
        return json.loads(json.dumps(s.sanitize_sidecar_contract_output(payload), ensure_ascii=False))

    with ExitStack() as stack:
        stack.enter_context(patch.object(bridge, "_call_omniauto", side_effect=process_io))
        stack.enter_context(patch.object(bridge, "prepare_startup_layout_for_new_transaction", return_value={"ok": True, "layout_snapshot": layout}))
        stack.enter_context(patch.object(omniauto_vision, "vision_configuration_status", return_value={"ready": True}))
        assert any(t.conversation_id == conversation_id for t in api.get_wechat_read_targets(binding))
        # Precondition: the voice in S0 was transcribed/committed BEFORE this
        # send task. Use the raw OCR facts and a historical action fixture;
        # do not pretend to re-enact that earlier voice action. Ingest and
        # checkpoint freezing still go through production code and HTTP.
        import test_wechat_c2_api as c2
        from chejin_worker_client.message_identity_commit import MessageCommitBasis
        from chejin_worker_client.message_viewport_projection import stable_business_content_signature
        seed = process_io(["messages"])
        committed = []
        for index, observation in enumerate(seed["observations"], 1):
            oid = observation["observation_id"]
            runtime = {}
            basis = MessageCommitBasis.NEW_SUFFIX
            proof = {"alignment_status": "not_required", "old_tail_fully_consumed": True,
                "new_suffix_observation_id": oid}
            if observation["message_type"] == "voice":
                summary = c2._voice_action_evidence(stable_id=f"worker-message-{index}",
                    post_observation_id=oid, result_screen_order=index - 1,
                    content_signature=stable_business_content_signature(observation))
                runtime["_worker_voice_action_summary"] = summary
                proof = summary["confirmed_action_mapping"]
                basis = MessageCommitBasis.CONFIRMED_VOICE_ACTION
            committed.append(c2._committed_test_observation(observation, worker_sequence=index,
                commit_basis=basis, proof=proof, runtime_evidence=runtime))
        seed_binding = {**session, "id": session["binding_id"]} if "binding_id" in session else session
        seeded = c2._production_worker_payload_for_test(binding=seed_binding,
            remark_code="CJ8R8A35", read_run_id="historical-before-avatar-send",
            observations=committed, read_reason="waiting_user_reply")
        response = api.session.request("POST", f"http://testserver/api/workers/{worker['id']}/wechat/messages/ingest",
            json=seeded, headers=backend._worker_headers(worker))
        assert response.status_code == 200, response.text
        assert not lock_summary()["locked"]
        assert not storage.load_runtime_control()["inflight_flow_id"]
        with backend.SessionLocal() as db:
            action = db.query(backend.ReplyAction).filter_by(conversation_id=conversation_id, current=True).one()
            # Approved reply text is a precondition; Brain generation is not
            # the subject of this send regression.
            action.reply_text = TEXT
            action.reply_text_hash = backend.reply_text_hash(TEXT)
            action_id, batch_id = action.id, action.batch_id
            task_id = db.query(backend.Task).filter_by(reply_action_id=action_id).one().id
            assert db.get(backend.Task, task_id).status == "pending"
            assert db.query(backend.SentAck).count() == 0
            db.commit()
        status = api.get_wechat_message_batch(binding, batch_id)
        task = Task.from_api(status["task"])
        # No calls to claim/send-ack/unlock/finish here: the production task
        # wrapper MUST drive every transition automatically.
        runner._execute_task(binding, task, "pending")

    assert not errors, errors
    assert any(result and result.result_code == "chat_reply_sent" for result in results), results
    assert actions.count("send") == 1, actions
    assert not lock_summary()["locked"]
    assert runner.current_task is None and runner.current_ui_lock is None
    assert not storage.load_runtime_control()["inflight_flow_id"]
    assert not storage.has_pending_reply_send_ack_outbox()
    assert not bridge.send_transaction_journal_path(action_id).exists()
    with backend.SessionLocal() as db:
        assert db.get(backend.Task, task_id).status == "completed"
        assert db.get(backend.ReplyAction, action_id).status == "sent"
        assert db.query(backend.SentAck).filter_by(reply_action_id=action_id).one().send_result == "sent"
        assert not (db.get(Worker, worker["id"]).inflight_flow_state or {}).get("flow_id")
        assert db.query(backend.HandoffEvent).count() == 0
    assert sum(url.endswith("/sent-ack") for _, url, _ in requests) == 1
    assert sum(url.endswith("/inflight-flow/start") for _, url, _ in requests) == 1
    assert sum(url.endswith("/inflight-flow/finish") for _, url, _ in requests) == 1
    assert all(status == 200 for _, _, status in requests), requests

    # The original production wrapper has completed every transition. Neither
    # this test nor the update gate may clear leases or settle receipts for it.
    assert runner.current_task_lease is None
    assert api.task_lease_fencing_tokens == {}
    assert runner.set_run_status("paused") is True
    runner.set_update_new_work_gate(True, update_request_id="after-real-send")
    for _ in range(2):
        safety = runner.update_install_safety_snapshot()
        assert safety["safe"] is True, safety
        assert safety["cached_task_lease_count"] == 0
        assert safety["task_lease_guard_active"] is False
        assert safety["waiting_reason_code"] == ""
