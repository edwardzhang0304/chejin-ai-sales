"""Real Worker/SQLite/UI resume; only desktop and backend boundaries are doubles."""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from test_c2_identity_gate_receipts import harness
from test_task_runner import FakeApi, FakeBridge, identity_checkpoint
from chejin_worker_client.models import Binding, RpaResult, WechatReadTarget
from chejin_worker_client.storage import load_binding, load_runtime_control, save_binding
from chejin_worker_client.runtime_process_timeline import RuntimeProcessTimeline

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "omniauto-rpa/apps/wechat_ai_customer_service/tests"))
from test_input_boundary_gap import calibration, frame, install_desktop, register, sidecar


def ocr_item(text, left, top, right, bottom):
    return dict(text=text, left=left, top=top, right=right, bottom=bottom,
                center_x=(left+right)/2, center_y=(top+bottom)/2, confidence=0.99)


def click_start(runner):
    # The actual WebChannel Start button handler, through the real Worker API.
    from test_web_ui_binding_behavior import _headless_web_ui_module
    with _headless_web_ui_module() as module:
        window = module.WorkerWebWindow.__new__(module.WorkerWebWindow)
        window.binding = runner.binding
        window.runner = runner
        window._publish = lambda: None  # Qt rendering boundary only
        bridge = module.WorkerWebBridge(window)
        bridge.startAccepting()


def test_real_sidecar_scan_failure_pause_ui_start_and_scan_resume(harness, monkeypatch, tmp_path):
    c = calibration()
    install_desktop(monkeypatch, tmp_path, c)
    desktop = {"dpi": 1.25, "captures": 0}
    monkeypatch.setattr(sidecar, "window_dpi_scale", lambda _: desktop["dpi"])
    def capture(hwnd, **kwargs):
        desktop["captures"] += 1
        image = frame(boundary=False)  # composer is absent throughout; sidebar is independent
        register(image, c)
        return image, "controlled-desktop.png"
    monkeypatch.setattr(sidecar, "capture_wechat", capture)
    items = [ocr_item("搜索", 80, 42, 115, 60), ocr_item("CJTEST01", 113, 104, 185, 121)]
    monkeypatch.setattr(sidecar, "run_ocr", lambda image: list(items))
    class Bridge(FakeBridge):
        def list_sessions(self, **kwargs):
            return sidecar.sessions_payload(c["hwnd"], {"ok": True})
    class Api(FakeApi):
        def post_wechat_session_scan_result(self, binding, payload):
            result = super().post_wechat_session_scan_result(binding, payload)
            return {"bound_count": 0, "bindings": []} if payload["scan_failed"] else result
    api = Api(None)
    bridge = Bridge(RpaResult(ok=True, result_code="unused"))
    runner, _ = harness.make_runner(api, bridge)
    runner.binding = Binding("worker-test", "test-token", "instance-test", run_status="running")
    save_binding(runner.binding)
    timeline = RuntimeProcessTimeline()
    events = []
    def publish(event):
        events.append(event)
        timeline.apply(event)
    runner.on_runtime_process = publish
    for _ in range(3):
        runner._scan_wechat_sessions(runner.binding)
    assert len(api.scan_payloads) == 3
    assert all(p["scan_failed"] for p in api.scan_payloads)
    assert not any(e["event"] == "scan_completed" for e in events)
    assert timeline.scan_model()[-1]["state"] == "error"
    assert "3次无法确认" in timeline.scan_model()[-1]["description"]
    assert runner.binding.run_status == "paused"
    assert load_runtime_control()["pause_requested"]
    assert runner.layout_recovery_state()["blocked"]
    from test_web_ui_binding_behavior import _headless_web_ui_module, WebUiBindingBehaviorTest
    with _headless_web_ui_module() as module:
        window = WebUiBindingBehaviorTest._window(module, runner.binding)
        window.runner = runner
        window.connection_status = "online"
        state = window._state()
        assert state["screen"] == "paused-empty"
        assert state["model"]["layoutRecovery"]["blocked"]
        evidence_path = os.environ.get("CHEJIN_LAYOUT_UI_STATE")
        if evidence_path:
            # This test uses only synthetic identities and a synthetic token.
            Path(evidence_path).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    runner._scan_wechat_sessions(runner.binding)
    assert desktop["captures"] == 3  # no fourth passive retry
    restarted, _ = harness.make_runner(api, bridge)
    restarted.binding = load_binding()
    assert restarted.layout_recovery_state()["blocked"]
    desktop["dpi"] = 1.0
    click_start(runner)
    assert runner.binding.run_status == "running"
    runner._scan_wechat_sessions(runner.binding)
    assert api.scan_payloads[-1]["scan_failed"] is False
    assert api.scan_payloads[-1]["sessions"][0]["remark_code_candidates"] == ["CJTEST01"]
    assert runner.visible_hit_queue[0].remark_code == "CJTEST01"
    assert not runner.layout_recovery_state()["blocked"]
    assert not load_runtime_control()["pause_requested"]
    assert not bridge.sent_replies


@pytest.mark.parametrize("network_down", [False, True])
def test_read_layout_failure_is_bounded_and_finishes_flow(harness, network_down):
    api = FakeApi(None)
    bridge = FakeBridge(RpaResult(ok=True, result_code="unused"))
    failure = {"ok": False, "error_code": "TARGET_NOT_CONFIRMED", "guard": {
        "conversation_type_evidence": {"error_code": "WECHAT_UI_LAYOUT_UNRESOLVED"}}}
    bridge.locate_payloads = [failure.copy() for _ in range(20)]
    runner, _ = harness.make_runner(api, bridge)
    runner.binding = Binding("worker-test", "test-token", "instance-test", run_status="running")
    save_binding(runner.binding)
    target = WechatReadTarget(conversation_id="conv-layout", display_name="CJTEST01", remark_code="CJTEST01",
        rpa_session_key="test", authorization_revision="rev-layout", unread_generation=1,
        raw={"identity_checkpoint": identity_checkpoint()})
    if network_down:
        api.run_status_error = ConnectionError("controlled network failure")
    for _ in range(3):
        result = runner._read_one_wechat_target(runner.binding, target, enforce_read_targets=False, wait_for_brain=False)
        assert not result["ok"]
        # Success in a different scope cannot clear this customer's failures.
        runner._scan_wechat_sessions(runner.binding)
    assert runner.binding.run_status == "paused"
    assert not load_runtime_control()["inflight_flow_id"]
    assert not bridge.message_reads and not bridge.sent_replies
    assert runner.layout_recovery_state()["blocked"]
    if network_down:
        assert runner._pending_run_status_sync == "paused"


def test_target_mismatch_without_layout_evidence_does_not_trigger_layout_pause(harness):
    from chejin_worker_client.layout_recovery import record_result, recovery_view
    state = {}
    for _ in range(10):
        state = record_result(state, scope="read", payload={"ok": False, "error_code": "TARGET_NOT_CONFIRMED"})
    assert not recovery_view(state)["blocked"]


def test_fault_recovery_also_resets_layout_limit_only_after_existing_gates_pass(harness):
    from chejin_worker_client.storage import save_c2_state
    class Api(FakeApi):
        def set_run_status(self, binding, run_status, **kwargs):
            if run_status == "running":
                assert kwargs.get("recover_from_fault") is True
            return super().set_run_status(binding, run_status)
    bridge = FakeBridge(RpaResult(ok=True, result_code="unused"))
    runner, _ = harness.make_runner(Api(None), bridge)
    runner.api.task_lease_fencing_tokens = {}
    bridge.sidecar_active = lambda: False
    runner.binding = Binding("worker-test", "test-token", "instance-test", run_status="faulted")
    save_binding(runner.binding)
    save_c2_state("layout_recovery", {"counts": {"scan": 3}})
    runner._backend_confirmed_run_status = "faulted"
    runner._backend_fault_recovery = {"protocol_version": 1, "ready": True}
    runner._recovery_heartbeat_at = time.monotonic()
    runner.last_rpa_component_status = "ready"
    runner.last_wechat_status = "logged_in"
    # Controlled thread boundary. The real recovery checker inspects real live
    # threads; this test does not claim the full background task loop ran.
    stop = threading.Event()
    threads = []
    try:
        for name, attr in [("task_runner", "thread"), ("thread_monitor", "thread_monitor"), ("c2_listener", "c2_thread")]:
            entered = threading.Event()
            def loop(kind=name, ready=entered):
                runner._mark_background_loop_entered(kind)
                ready.set()
                stop.wait(5)
            thread = threading.Thread(target=loop)
            setattr(runner, attr, thread)
            threads.append(thread)
            thread.start()
            assert entered.wait(1)
        checked = runner._check_fault_recovery()
        assert checked["ready"], checked
        runner._publish_fault_recovery()
        assert runner.fault_recovery_state()["ready"], runner.fault_recovery_state()
        click_start(runner)
        assert runner.layout_recovery_state()["blocked"]  # request alone is not recovery
        runner._process_fault_recovery()
        assert runner.binding.run_status == "running"
        assert not runner.layout_recovery_state()["blocked"]
    finally:
        stop.set()
        for thread in threads:
            thread.join(1)
