"""Real Worker/OCR/HTTP/PG/SQLite continuation; desktop I/O and model controlled."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from test_dynamic_composer_http import ROOT, http_api, _drive_composer_http
import dynamic_composer_desktop as fixture

OLD_REPLY = "好的，我帮您看看"
NEW_REPLY = "好的，按十五万预算重新筛选电车。"
FRAME_FACTORY = fixture.derived_frames


class PersistentDesktop(fixture.Desktop):
    """Customer message remains on screen after draft cleanup and restart."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs, reply=OLD_REPLY)
        self.customer_arrived = False
        self.enter_texts = []
        _, next_frames = FRAME_FACTORY(reply=NEW_REPLY, movement=0, reduction=0, final_customer=True)
        self.after_new = self.frames["typing"].copy()
        self.after_new.paste(self.frames["before"].crop((301, 700, 778, 844)), (301, 700))
        self.new_typing = self.after_new.copy()
        self.new_typing.paste(next_frames["typing"].crop((301, 700, 778, 844)), (301, 700))
        self.new_sent = self.after_new.copy()
        self.new_sent.paste(next_frames["sent"].crop((310, 510, 768, 554)), (310, 640))

    def unicode_unit(self, unit):
        self.customer_arrived = True
        super().unicode_unit(unit)

    def key(self, key):
        if key == 13:
            self.enter_texts.append(self.draft)
        super().key(key)

    def capture(self, hwnd, *, artifact_dir=None, label="frame", **kwargs):
        if self.enter_count:
            image, state = self.new_sent.copy(), "new_reply_sent"
        elif self.draft:
            image = (self.new_typing if self.reply == NEW_REPLY else self.frames["typing"]).copy()
            state = "typing"
        else:
            image = (self.after_new if self.customer_arrived else self.frames["before"]).copy()
            state = "chat"
        path = self.directory / f"{len(self.captures):03d}-{label}.png"
        image.save(path)
        layout = fixture.register(image, self.calibration, path)
        self.captures.append({"label": label, "path": str(path), "state": state, "layout": layout})
        return image, str(path)


def resume_in_new_process(base, directory):
    """Run actual startup recovery and the normal state-target reader in a new process."""
    from chejin_worker_client import storage, omniauto_vision
    from chejin_worker_client.api import WorkerApiClient
    from chejin_worker_client.rpa_bridge import RpaBridge
    from chejin_worker_client.task_runner import TaskRunner
    from chejin_worker_client.ui_lock import lock_summary
    patch = pytest.MonkeyPatch()
    calibration, frames = FRAME_FACTORY(reply=OLD_REPLY, movement=0, reduction=0, final_customer=True, new_kind="customer")
    desktop = PersistentDesktop(patch, directory / "desktop", calibration, frames)
    desktop.customer_arrived = True
    desktop.reply = NEW_REPLY
    bridge = RpaBridge(); bridge.mode = "real"
    calls = []

    def process_io(args, **kwargs):
        calls.append(args[0])
        def option(name, default=""):
            return args[args.index(name) + 1] if name in args else default
        if args[0] == "send":
            assert lock_summary()["locked"]
            payload = fixture.sidecar.send_payload(calibration["hwnd"], {}, target=option("--target"),
                text=option("--text"), exact=True, skip_send_rate_guard=True,
                artifact_dir=str(directory / "desktop"),
                expected_context_guard=json.loads(option("--expected-context-guard", "{}")),
                action_journal_path=option("--action-journal"))
        else:
            assert args[0] in {"messages", "open-chat"}, args
            payload = fixture.sidecar.messages_payload(calibration["hwnd"], {"ok": True}, target="CJMKZUTH",
                history_load_times=0, max_scroll_steps=0, max_snapshots=1, confirm_target="CJMKZUTH",
                confirm_exact=True, chat_fact_roi_ocr=True)
            assert payload["ok"], payload
            if args[0] == "open-chat":
                payload = {"ok": True, "guard": payload["target_confirmation"],
                           "initial_messages_snapshot": payload, "state": "chat_target_confirmed"}
        return json.loads(json.dumps(fixture.sidecar.sanitize_sidecar_contract_output(payload)))

    patch.setattr(bridge, "_call_omniauto", process_io)
    patch.setattr(bridge, "probe", lambda: ("ready", "logged_in"))
    patch.setattr(bridge, "prepare_startup_layout_for_new_transaction", lambda **kw: {"ok": True, "layout_snapshot": calibration})
    patch.setattr(omniauto_vision, "vision_configuration_status", lambda: {"ready": True})
    api = WorkerApiClient(base); binding = storage.load_binding()
    errors, runtime_events = [], []
    runner = TaskRunner(api, bridge, on_profile=lambda _: None, on_status=lambda _: None,
                        on_step=lambda _: None, on_task=lambda _: None, on_result=lambda _: None,
                        on_error=errors.append, on_runtime_process=runtime_events.append)
    runner.start(binding)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if (not storage.has_pending_reply_send_ack_outbox()
                and not storage.load_runtime_control().get("inflight_flow_id")
                and not runner._restart_backend_probe_pending):
            break
        time.sleep(.1)
    assert not storage.has_pending_reply_send_ack_outbox()
    assert not storage.load_runtime_control().get("inflight_flow_id")
    assert calls == [], "Startup must settle the old receipt without desktop input"
    assert runner._can_start_new_flow(binding), {
        "binding": binding.run_status, "control": storage.load_runtime_control(),
        "probe": runner._restart_backend_probe_pending, "pending_finish": runner._pending_flow_finish,
        "errors": errors,
    }
    targets = runner._fetch_read_targets(binding)
    assert len(targets) == 1, targets
    deadline = time.monotonic() + 10
    while runner._high_priority_active() and time.monotonic() < deadline:
        time.sleep(.1)
    runner._read_state_target_queue(binding, targets=targets)
    record = {"enter_texts": desktop.enter_texts, "runtime_events": runtime_events,
              "pending_ack": storage.has_pending_reply_send_ack_outbox(),
              "flow": storage.load_runtime_control().get("inflight_flow_id"), "errors": errors, "calls": calls}
    runner.stop_for_update(timeout_seconds=5)
    (directory / "restart.json").write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    assert desktop.enter_texts == [NEW_REPLY], record
    assert not record["pending_ack"] and not record["flow"], record


@pytest.mark.parametrize("scenario", ["normal", "restart_loss", "paused"])
def test_customer_interrupt_continues_ai_reply(tmp_path, request, monkeypatch, scenario):
    if os.environ.get("CHEJIN_INTERRUPT_HTTP_CHILD") != "1":
        env = {**os.environ, "CHEJIN_INTERRUPT_HTTP_CHILD": "1", "CHEJIN_COMPOSER_HTTP_CHILD": "1",
               "CHEJIN_WORKER_HOME": str(tmp_path / "worker"), "CHEJIN_COMPOSER_WORKER_SOURCE": str(ROOT),
               "CHEJIN_C2_ENABLED": "false", "CHEJIN_OBSERVABILITY_ENABLED": "false",
               "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": os.pathsep.join(str(ROOT / p) for p in
                   ("backend", "backend/tests", "worker-client", "worker-client/omniauto-rpa"))}
        result = subprocess.run([sys.executable, "-m", "pytest", f"{__file__}::test_customer_interrupt_continues_ai_reply[{scenario}]",
                                 "-xq", "--tb=short", f"--basetemp={tmp_path / 'child'}"],
                                env=env, cwd=ROOT, text=True, capture_output=True, timeout=300)
        (tmp_path / "child.stdout").write_text(result.stdout)
        (tmp_path / "child.stderr").write_text(result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        return

    import test_c3_api as backend
    from chejin_worker_client import task_runner, storage
    from app.services.ai_adapter import MockOmniAutoAIEngineAdapter, AIEngineDecision
    from app.contracts.shared_rules import shared_adapter
    assert backend.engine.url.host == "127.0.0.1" and backend.engine.url.port == 55490
    assert backend.engine.url.database == "composer_test"
    brain_calls, runtime_events, lost = [], [], []
    captured = {}

    def frames(**kwargs):
        return FRAME_FACTORY(**kwargs, reply=OLD_REPLY, movement=0, reduction=0)

    class RecordingDesktop(PersistentDesktop):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured["desktop"] = self

    class RecordingRunner(task_runner.TaskRunner):
        def __init__(self, *args, **kwargs):
            kwargs["on_runtime_process"] = runtime_events.append
            super().__init__(*args, **kwargs)
            if scenario == "restart_loss":
                original = self.api.session.send
                def wire(prepared, **kw):
                    if prepared.url.endswith("/sent-ack") and lost:
                        raise __import__("requests").ConnectionError("controlled outage until restart")
                    response = original(prepared, **kw)
                    if prepared.url.endswith("/sent-ack") and response.status_code == 200:
                        lost.append(True)
                        raise __import__("requests").ConnectionError("controlled response loss after server commit")
                    return response
                monkeypatch.setattr(self.api.session, "send", wire)

    def generate(self, *, conversation_context, message_batch):
        brain_calls.append({"context": conversation_context, "batch": message_batch})
        is_new = "十五万元" in json.dumps(message_batch, ensure_ascii=False)
        if is_new:
            captured["desktop"].reply = NEW_REPLY
        return AIEngineDecision(decision="send_reply", reply_text=NEW_REPLY if is_new else OLD_REPLY,
                                confidence=.9, guard_result="pass", evidence_refs=["controlled_model"],
                                raw_payload={"adapter": "controlled_model"})

    monkeypatch.setattr(fixture, "REPLY", OLD_REPLY)
    monkeypatch.setattr(fixture, "derived_frames", frames)
    monkeypatch.setattr(fixture, "Desktop", RecordingDesktop)
    monkeypatch.setattr(task_runner, "TaskRunner", RecordingRunner)
    monkeypatch.setattr(MockOmniAutoAIEngineAdapter, "generate_reply_decision", generate)
    if os.environ.get("CHEJIN_INTERRUPT_NEGATIVE_CONTROL") == "1":
        # Disable only the new backend interpretation; real generic failed path must reproduce the bug.
        monkeypatch.setattr(shared_adapter("send_interruption"), "confirmed_customer_interruption", lambda **kw: False)
    runner, desktop, first = _drive_composer_http(tmp_path, request, monkeypatch, "failed",
                                                expect_pending_ack=scenario == "restart_loss")
    assert desktop.enter_texts == [] and not desktop.draft
    with backend.SessionLocal() as db:
        old = db.query(backend.ReplyAction).one()
        assert old.status == "superseded" and old.current is False
        assert db.query(backend.Task).filter_by(reply_action_id=old.id).one().status == "cancelled"
        assert db.query(backend.HandoffEvent).count() == 0
        old_id = old.id

    targets = runner._fetch_read_targets(runner.binding)
    if scenario == "restart_loss":
        assert targets == [], "Pending receipt barrier must prevent new work"
        assert lost == [True]
        # Lead creation also seeds an unrelated add-friend task. This desktop
        # fixture is already a friend; cancel that setup-only task through API
        # so the restarted real poller cannot pick unrelated work.
        with backend.SessionLocal() as db:
            unrelated = [t.id for t in db.query(backend.Task).filter_by(task_type="add_friend", status="pending").all()]
        for task_id in unrelated:
            response = backend.client.post(f"/api/tasks/{task_id}/cancel", json={"reason": "already-friend test fixture"}, headers=backend.HEADERS)
            assert response.status_code == 200, response.text
        runner.stop_for_update(timeout_seconds=5)
        script = "from pathlib import Path; from test_customer_interrupt_http import resume_in_new_process; import sys; resume_in_new_process(sys.argv[1], Path(sys.argv[2]))"
        base = runner.api.base_url
        result = subprocess.run([sys.executable, "-c", script, base, str(tmp_path)], env=os.environ.copy(),
                                cwd=ROOT, text=True, capture_output=True, timeout=150)
        (tmp_path / "restart.stdout").write_text(result.stdout)
        (tmp_path / "restart.stderr").write_text(result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        outcome = json.loads((tmp_path / "restart.json").read_text())
    else:
        assert len(targets) == 1, targets
        if scenario == "paused":
            runner.set_run_status("paused")
            runner._read_state_target_queue(runner.binding, targets=targets)
            assert desktop.enter_texts == [] and len(brain_calls) == 1
            runner.set_run_status("running")
        runner._read_state_target_queue(runner.binding, targets=targets)
        outcome = {"enter_texts": desktop.enter_texts, "runtime_events": runtime_events,
                   "pending_ack": storage.has_pending_reply_send_ack_outbox(),
                   "flow": storage.load_runtime_control().get("inflight_flow_id")}
    record = {**outcome, "scenario": scenario, "brain_calls": brain_calls}
    with backend.SessionLocal() as db:
        record["actions"] = [{"status": a.status, "text": a.reply_text} for a in db.query(backend.ReplyAction).all()]
        record["handoffs"] = [h.handoff_reason_code for h in db.query(backend.HandoffEvent).all()]
        record["messages"] = [m.content for m in db.query(backend.MessageEvent).all()]
        record["tasks"] = [t.status for t in db.query(backend.Task).filter(backend.Task.reply_action_id.is_not(None)).order_by(backend.Task.created_at).all()]
        record["conversation"] = db.query(backend.Conversation).one().status
        assert db.query(backend.SentAck).filter_by(reply_action_id=old_id).count() == 1
    (tmp_path / "continuation.json").write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    assert outcome["enter_texts"] == [NEW_REPLY], record
    assert len(brain_calls) == 2 and "十五万元" in json.dumps(brain_calls[-1]["batch"], ensure_ascii=False), record
    assert "市区通勤" in json.dumps(brain_calls[-1]["context"], ensure_ascii=False), record
    assert record["handoffs"] == [], record
    assert record["tasks"] == ["cancelled", "completed"], record
    assert sum("十五万元" in (m or "") for m in record["messages"]) == 1, record
    assert record["conversation"] == "waiting_user_reply", record
    assert not outcome["pending_ack"] and not outcome["flow"], record
    runner.stop_for_update(timeout_seconds=5)
