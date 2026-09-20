"""Real Worker/OCR/HTTP/PG/SQLite and BackgroundTasks; desktop/model controlled."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from test_dynamic_composer_http import ROOT, http_api, _drive_composer_http, controlled_send_process_started
import dynamic_composer_desktop as fixture

OLD_REPLY = "好的，我帮您看看"
NEW_REPLY = "好的，按十五万预算重新筛选电车。"
FRAME_FACTORY = fixture.derived_frames


def observe_async_generation(monkeypatch, *, remove_callback=False):
    """Exercise the production scheduling branch, substituting only the model."""
    from starlette.background import BackgroundTasks
    from app.api.routes import wechat as routes
    from app.core.config import get_settings
    from app.services import c3_service
    from app.services.ai_adapter import MockOmniAutoAIEngineAdapter

    monkeypatch.setattr(get_settings(), "c3_ai_adapter_mode", "real")
    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", lambda: MockOmniAutoAIEngineAdapter())
    events = {"scheduled": [], "executed": [], "suppressed": []}
    native_generate = routes._generate_message_batch
    native_add = BackgroundTasks.add_task

    def generate(batch_id, attempt):
        events["executed"].append({"batch_id": batch_id, "attempt": attempt})
        return native_generate(batch_id, attempt)

    def add(background, callback, *args, **kwargs):
        if callback is generate:
            event = {"batch_id": args[0], "attempt": args[1]}
            if remove_callback and events["scheduled"]:
                events["suppressed"].append(event)
                return None
            events["scheduled"].append(event)
        return native_add(background, callback, *args, **kwargs)

    monkeypatch.setattr(routes, "_generate_message_batch", generate)
    monkeypatch.setattr(BackgroundTasks, "add_task", add)
    return events


def assert_business_continuation(record):
    """The negative control must fail these same business assertions."""
    assert record["enter_texts"] == [NEW_REPLY], "new reply was not sent exactly once"
    calls = record["brain_calls"]
    assert len(calls) == 2 and "十五万元" in json.dumps(calls[-1]["batch"], ensure_ascii=False)
    assert "市区通勤" in json.dumps(calls[-1]["context"], ensure_ascii=False)
    assert record["handoffs"] == []
    assert record["tasks"] == ["cancelled", "completed"]
    assert sum("十五万元" in (m or "") for m in record["messages"]) == 1
    assert record["conversation"] == "waiting_user_reply"
    assert not record["pending_ack"] and not record["flow"]


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
        # Preserve the customer's appended bubble. It occupies the bottom of
        # the viewport; scroll history up before appending the new self bubble.
        # Pasting directly at y=640 would erase that customer message.
        self.new_sent.paste(self.after_new.crop((301, 161, 778, 700)), (301, 81))
        from PIL import ImageDraw
        ImageDraw.Draw(self.new_sent).rectangle((301, 620, 777, 699), fill=(250,250,250))
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
            image = (self.new_typing if self.draft == NEW_REPLY else self.frames["typing"]).copy()
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
            controlled_send_process_started(args)
            payload = fixture.sidecar.send_payload(calibration["hwnd"], {}, target=option("--target"),
                text=option("--text"), exact=True, skip_send_rate_guard=True,
                artifact_dir=str(directory / "desktop"),
                expected_context_guard=fixture.send_context_from_args(args),
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


@pytest.mark.parametrize("scenario", ["normal", "restart_loss", "paused", "no_auto_callback",
                                     "hint", "remaining_draft", "hint_no_auto_callback", "read_retry", "read_retry_no_callback"])
def test_customer_interrupt_continues_ai_reply(tmp_path, request, monkeypatch, scenario):
    if os.environ.get("CHEJIN_INTERRUPT_HTTP_CHILD") != "1":
        env = {**os.environ, "CHEJIN_INTERRUPT_HTTP_CHILD": "1", "CHEJIN_COMPOSER_HTTP_CHILD": "1",
               "CHEJIN_WORKER_HOME": str(tmp_path / "worker"), "CHEJIN_COMPOSER_WORKER_SOURCE": str(ROOT),
               "CHEJIN_C2_ENABLED": "false", "CHEJIN_OBSERVABILITY_ENABLED": "false",
               "CHEJIN_C3_BRAIN_NO_PROGRESS_WATCHDOG_SECONDS": "3",
               "CHEJIN_C3_BRAIN_POLL_INTERVAL_SECONDS": "0.1",
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
    monkeypatch.setattr(storage, "APP_DIR", tmp_path / "worker")
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "worker/worker_client.sqlite3")
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
            self.cancel_clear_count = 0
            if scenario in {"hint", "hint_no_auto_callback"}:
                # Explicitly derived pixels: hint remains on an empty control,
                # disappears when real text is typed, returns after sending.
                from PIL import ImageDraw, ImageFont
                font = ImageFont.truetype(os.environ.get("CHEJIN_COMPOSER_TEST_FONT", "/System/Library/Fonts/STHeiti Light.ttc"), 14)
                for image in (self.frames["before"], self.after_new, self.new_sent):
                    ImageDraw.Draw(image).text((320, 712), "按住鼠标 语音输入文字", font=font, fill=(159,159,166))

        def key(self, key):
            cancel_clear = key == 8 and self.selected and self.draft == OLD_REPLY
            if cancel_clear:
                self.cancel_clear_count += 1
            if cancel_clear and scenario == "remaining_draft":
                # Clear operation is issued, but the OS leaves the old draft.
                # The next reply must erase it before typing, not append to it.
                self.keys.append(key)
                self.selected = False
                return
            super().key(key)

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
    read_failures = []
    if scenario in {"read_retry", "read_retry_no_callback"}:
        from apps.wechat_ai_customer_service.adapters.pre_send_read_failure import ReadCallFailed
        native_build = fixture.sidecar.build_send_fact_snapshot_from_frame
        def read_once(*args, **kwargs):
            if kwargs.get("label") == "send_pre_trigger_context_reused" and not read_failures:
                read_failures.append(kwargs["label"])
                raise ReadCallFailed(operation="read", reason="controlled first pre-trigger read failure")
            return native_build(*args, **kwargs)
        monkeypatch.setattr(fixture.sidecar, "build_send_fact_snapshot_from_frame", read_once)
    callback_disabled = scenario in {"no_auto_callback", "hint_no_auto_callback", "read_retry_no_callback"}
    async_events = observe_async_generation(monkeypatch, remove_callback=callback_disabled)
    if os.environ.get("CHEJIN_INTERRUPT_NEGATIVE_CONTROL") == "1":
        # Disable only the new backend interpretation; real generic failed path must reproduce the bug.
        monkeypatch.setattr(shared_adapter("send_interruption"), "confirmed_customer_interruption", lambda **kw: False)
    runner, desktop, first = _drive_composer_http(tmp_path, request, monkeypatch, "failed",
                                                expect_pending_ack=scenario == "restart_loss",
                                                expected_send_calls=2 if scenario.startswith("read_retry") else 1)
    assert desktop.enter_texts == []
    assert desktop.draft == (OLD_REPLY if scenario == "remaining_draft" else "")
    assert desktop.cancel_clear_count == 1
    if scenario.startswith("read_retry"):
        from chejin_worker_client.pre_send_read_recovery import input_pending_records
        assert len(read_failures) == 1
        assert not input_pending_records(), "Confirmed customer interruption must not leave stale input barrier"
    assert all(c['label'] != 'send_program_draft_cleanup' for c in desktop.captures)
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
    record = {**outcome, "scenario": scenario, "brain_calls": brain_calls, "async_events": async_events}
    with backend.SessionLocal() as db:
        record["actions"] = [{"status": a.status, "text": a.reply_text} for a in db.query(backend.ReplyAction).all()]
        record["handoffs"] = [h.handoff_reason_code for h in db.query(backend.HandoffEvent).all()]
        record["messages"] = [m.content for m in db.query(backend.MessageEvent).all()]
        record["tasks"] = [t.status for t in db.query(backend.Task).filter(backend.Task.reply_action_id.is_not(None)).order_by(backend.Task.created_at).all()]
        record["conversation"] = db.query(backend.Conversation).one().status
        assert db.query(backend.SentAck).filter_by(reply_action_id=old_id).count() == 1
    (tmp_path / "continuation.json").write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    runner.stop_for_update(timeout_seconds=5)
    if callback_disabled:
        assert len(async_events["scheduled"]) == len(async_events["executed"]) == 1, record
        assert len(async_events["suppressed"]) == 1 and len(brain_calls) == 1, record
        assert outcome["enter_texts"] == [], record
        assert sum("十五万元" in (m or "") for m in record["messages"]) == 1, record
        with pytest.raises(AssertionError, match="new reply was not sent exactly once"):
            assert_business_continuation(record)
    else:
        assert len(async_events["scheduled"]) == len(async_events["executed"]) == 2, record
        assert async_events["scheduled"] == async_events["executed"], record
        assert len({event["batch_id"] for event in async_events["executed"]}) == 2, record
        assert async_events["suppressed"] == [], record
        assert_business_continuation(record)
