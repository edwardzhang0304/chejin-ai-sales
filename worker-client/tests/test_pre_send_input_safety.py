"""Fresh real OCR + SQLite checks. Desktop/OS only is controlled, not Windows UAT."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from threading import Event, RLock
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "worker-client/omniauto-rpa/apps/wechat_ai_customer_service/tests"))
import dynamic_composer_desktop as fixture
from apps.wechat_ai_customer_service.adapters.pre_send_read_failure import empty_input_observation
from chejin_worker_client import storage, pre_send_read_recovery as recovery
from chejin_worker_client.models import Binding
from chejin_worker_client.rpa_bridge import RpaBridge
from test_pre_send_read_recovery_budget import failure


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "APP_DIR", tmp_path / "worker")
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "worker/client.sqlite3")
    calibration, frames = fixture.derived_frames(reply="z", movement=0, reduction=0)
    desktop = fixture.Desktop(monkeypatch, tmp_path / "desktop", calibration, frames, reply="z")
    sidecar = fixture.sidecar
    monkeypatch.setattr(sidecar, "_WIN32_IMPORT_ERROR", "")
    window = {"hwnd": calibration["hwnd"], "pid": calibration["process_id"],
              "title": "微信", "class_name": "Qt51514QWindowIcon", "visible": True}
    probe = {"windows": [window], "visible_windows": [window], "main_windows": [window],
             "visible_main_windows": [window], "visible_main_count": 1, "main_count": 1}
    monkeypatch.setattr(sidecar, "ensure_visible_wechat_window", lambda **kw: deepcopy(probe))
    forbidden = []
    def no_action(*a, **kw):
        forbidden.append("unexpected UI/history/clipboard/media operation")
        raise AssertionError(forbidden[-1])
    for name in ("activate_window", "human_client_click", "key_press", "hotkey", "clipboard_read", "clipboard_copy",
                 "sendinput_unicode_unit", "messages_payload", "open_chat_by_target"):
        if hasattr(sidecar, name): monkeypatch.setattr(sidecar, name, no_action)
    calls = []
    bridge = RpaBridge(); bridge.mode = "real"
    def transport(args, **kwargs):
        calls.append(args)
        return sidecar.run_sidecar_cli(args)
    monkeypatch.setattr(bridge, "_call_omniauto", transport)
    return desktop, bridge, calls, forbidden


def observe(bridge, target="CJMKZUTH", **kw):
    return bridge.get_messages(display_name=target, rpa_session_key="", remark_code=target,
        target_mode="current", max_snapshots=1, max_scroll_steps=0, restore_to_latest=False,
        input_safety_observation=True, input_safety_request_id="request-a", **kw)


@pytest.mark.parametrize("scene", ["empty", "one_character", "other_chat", "capture_failed", "layout_failed"])
def test_real_cli_observes_only_current_input(environment, monkeypatch, tmp_path, scene):
    desktop, bridge, calls, forbidden = environment
    if scene == "one_character": desktop.draft = "z"
    if scene == "capture_failed":
        def failed(*a, **kw): raise OSError("capture failed")
        monkeypatch.setattr(fixture.sidecar, "capture_wechat", failed)
    if scene == "layout_failed":
        # Fault injection: an absent measured layout cannot become blank input.
        monkeypatch.setattr(fixture.sidecar, "layout_snapshot_for_image", lambda *a, **kw: {})
    target = "CJBZZZZZ" if scene == "other_chat" else "CJMKZUTH"
    result = observe(bridge, target)
    observation = result.get("input_safety_observation", {})
    (tmp_path / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    assert empty_input_observation(observation, target=target, request_id="request-a") is (scene == "empty"), result
    if scene == "one_character": assert observation["status"] == "not_empty", result
    if scene == "other_chat": assert observation["status"] == "target_not_observed", result
    assert len(calls) == 1
    assert len(desktop.captures) == (0 if scene == "capture_failed" else 1)
    assert not forbidden and not desktop.enter_count and not desktop.keys and not desktop.clicked
    assert desktop.clipboard == "original clipboard"


@pytest.mark.parametrize("conflict", [dict(history_mode="anchor_until_found"), dict(chat_fact_roi_ocr=True),
    dict(anchor_ids=["old"]), dict(same_frame_full_ocr_evidence={"frame_id": "cached"}), dict(text_recheck_capture=True)])
def test_conflicting_bridge_arguments_fail_before_any_sidecar(environment, conflict):
    desktop, bridge, calls, forbidden = environment
    with pytest.raises(ValueError, match="ARGUMENT_CONFLICT"):
        observe(bridge, **conflict)
    assert calls == desktop.captures == forbidden == []


def seed_pending(*, stage="before_trigger", input_state="unverified"):
    identity = {"reply_action_id": "action-a", "task_id": "task-a", "conversation_id": "conversation-a",
                "flow_id": "flow-a", "authorization_revision": "rev-a", "reply_text_hash": "a" * 64}
    fact = {**failure(stage), "input_state": input_state}
    _, record = recovery.reserve(identity, fact, target="CJMKZUTH")
    proof = recovery.terminal_proof(record, phase_proof=fact["phase_proof"], input_state=input_state,
                                    interruption_reason="test interruption after original failure")
    recovery.save_settlement(record, proof=proof, request={"receipt_kind": "sent_ack"})
    recovery.mark_settled("action-a")


def worker(bridge):
    # Focused recovery coordinator test: settlement preconditions are explicit;
    # real Worker/HTTP receipt settlement is tested in the adjacent test file.
    return SimpleNamespace(binding=Binding("worker-a", "token", "client-a", run_status="faulted"),
        # Keep both calls in the same poll interval even on slow real OCR.
        stop_event=Event(), heartbeat_interval_seconds=60, current_task=None, current_ui_lock=None,
        _restart_recovery_lock=RLock(), _new_work_admission_lock=RLock(), reply_send_ack_lock=RLock(),
        _run_status_intent_lock=RLock(), _run_status_revision=7, bridge=bridge,
        update_install_safety_snapshot=lambda: {"settlement_complete": True})


@pytest.mark.parametrize("scene", ["normal", "new_fault", "disk_failure", "restart", "before_input", "known_cleared"])
def test_same_sqlite_gate_and_readonly_worker_coordinator(environment, monkeypatch, scene):
    desktop, bridge, calls, forbidden = environment
    seed_pending(stage="before_input" if scene == "before_input" else "before_trigger",
                 input_state="cleared" if scene == "known_cleared" else "unverified")
    runner = worker(bridge)
    if scene == "restart": monkeypatch.setattr(recovery, "BOOT_ID", "second-process")
    original = bridge._call_omniauto
    if scene == "new_fault":
        def late(*a, **kw):
            value = original(*a, **kw); runner._run_status_revision += 1; return value
        monkeypatch.setattr(bridge, "_call_omniauto", late)
    if scene == "disk_failure":
        finish = recovery._finish_input_observation
        def failed(*a, **kw):
            from contextlib import contextmanager
            connection = storage.db_connection
            @contextmanager
            def faulted_write():
                with connection() as conn:
                    yield SimpleNamespace(execute=conn.execute, commit=lambda: (_ for _ in ()).throw(OSError("disk full")))
            with monkeypatch.context() as patch:
                patch.setattr(storage, "db_connection", faulted_write)
                return finish(*a, **kw)
        monkeypatch.setattr(recovery, "_finish_input_observation", failed)
    recovery.observe_pending_input(runner)
    assert bool(recovery.input_pending_records()) is (scene in {"new_fault", "disk_failure"})
    assert len(calls) == (0 if scene in {"before_input", "known_cleared"} else 1)
    assert runner.binding.run_status == "faulted"
    assert runner.current_ui_lock is None
    assert not forbidden and not desktop.enter_count
    # Polls are limited; a resolved gate cannot be recreated at another chat.
    recovery.observe_pending_input(runner)
    assert len(calls) <= 1


def test_stale_observation_cannot_clear_a_new_request(environment):
    _, bridge, _, _ = environment
    seed_pending()
    record = recovery.input_pending_records()[0]
    first = recovery._begin_input_observation(record, 7)
    result = bridge.get_messages(display_name="CJMKZUTH", rpa_session_key="", remark_code="CJMKZUTH",
        target_mode="current", max_snapshots=1, max_scroll_steps=0, restore_to_latest=False,
        input_safety_observation=True, input_safety_request_id=first)
    recovery._begin_input_observation(record, 8)
    assert recovery._finish_input_observation(record, 7, first, result["input_safety_observation"]) is False
    assert recovery.input_pending_records()
