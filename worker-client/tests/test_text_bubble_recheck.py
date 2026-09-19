"""Real incident pixels/OCR/Sidecar/Worker/SQLite; HTTP and Windows are fixtures.

Private images are never embedded in the repository. A configured missing
fixture fails the test. Synthetic adversarial cases are explicitly separate.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from test_send_result_evidence import home
from chejin_worker_client import rpa_bridge, storage
from chejin_worker_client.models import Binding, RpaResult, WechatReadTarget
from chejin_worker_client.text_recheck import differing_text_observation_ids


@pytest.fixture
def incident(home, monkeypatch, request):
    filename = os.environ.get("CHEJIN_TEXT_RECHECK_FIXTURE")
    if not filename:
        pytest.skip("Private original incident required: CHEJIN_TEXT_RECHECK_FIXTURE")
    data = json.loads(Path(filename).read_text())
    assert Path(data["image_path"]).is_file()
    options = getattr(request, "param", {})
    if "roi_reuse_enabled" in options:
        from chejin_worker_client import task_runner
        enabled = options["roi_reuse_enabled"]
        monkeypatch.setenv("CHEJIN_C3_PRE_SEND_ROI_REUSE_ENABLED", "1" if enabled else "0")
        for module in (rpa_bridge, task_runner):
            monkeypatch.setattr(module, "CONFIG", replace(module.CONFIG, c3_pre_send_roi_reuse_enabled=enabled))
        data["roi_reuse_enabled"] = enabled
    # The child doing OCR gets only pixels and Windows metadata, never history.
    ui = {key: data[key] for key in ("image_path", "calibration", "geometry", "client_geometry")}
    ui_path = home / "windows-fixture.json"
    ui_path.write_text(json.dumps(ui), encoding="utf-8")
    calls = home / "calls.jsonl"
    monkeypatch.setenv("TEXT_RECHECK_FIXTURE", str(ui_path))
    monkeypatch.setenv("TEXT_RECHECK_CALLS", str(calls))
    driver = Path(__file__).parent / "fixtures/text_recheck_process.py"
    bridge = rpa_bridge.RpaBridge(driver)
    bridge.mode = "omniauto"
    monkeypatch.setattr(bridge, "_sidecar_command", lambda args: [sys.executable, str(driver), *args])
    initial = bridge.get_messages(display_name="CJA7Y368", rpa_session_key="", remark_code="CJA7Y368",
                                  target_mode="current", text_recheck_capture=not options.get("normal_initial", False))
    assert initial["ok"], initial
    assert len(initial["observations"]) == 6
    assert "一般" not in initial["observations"][4]["content_clean"].replace("\n", "")
    assert initial["top_message_fragment"]
    data.update(bridge=bridge, initial=initial, calls=calls)
    return data


def runner_for_incident(data):
    from test_task_runner import FakeApi, FakeBridge, TaskRunnerTest
    api = FakeApi(None)
    bridge = FakeBridge(RpaResult(ok=True, result_code="unused"))
    bridge.locate_payloads = [{"ok": True, "initial_messages_snapshot": copy.deepcopy(data["initial"])}]
    bridge.get_messages = data["bridge"].get_messages
    bridge.recheck_text_bubbles = data["bridge"].recheck_text_bubbles
    runner, seen = TaskRunnerTest().make_runner(api, bridge)
    binding = Binding("worker-test", "test-token", "instance-test", run_status="running")
    runner.binding = binding
    storage.save_binding(binding)
    # Exact persisted checkpoint rows; map only the two documented field names.
    tail = data["checkpoint_context"]["checkpoint"]["committed_tail"]
    recent = [{**copy.deepcopy(row), "stable_id": row["worker_stable_id"],
               "normalized_content_hash": row["business_projection"]["normalized_content_signature"]} for row in tail]
    cid = data["checkpoint_context"]["checkpoint"]["conversation_id"]
    possible = data["possible_sends"]["sends"][0]
    storage.save_c2_state(f"possible_ai_sends:{cid}", data["possible_sends"])
    target = WechatReadTarget(conversation_id=cid, display_name="CJA7Y368", remark_code="CJA7Y368",
        rpa_session_key="fixture-session", authorization_revision="fixture-authorized-revision", unread_generation=1,
        read_reason="recent_ai_sent",
        raw={"identity_checkpoint": {"version": 3, "recent_messages": recent, "next_sequence_floor": 8},
             "ai_reply_boundary": {"reply_action_id": possible["reply_action_id"],
                "reply_text_hash": possible["reply_text_hash"], "worker_stable_id": possible["reserved_worker_stable_id"],
                "sent_at": data["confirmed_sent_at"]}})
    api.read_targets = [target]
    return runner, binding, target, api, seen


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("incident", [
    {"roi_reuse_enabled": True, "normal_initial": True},
    {"roi_reuse_enabled": False, "normal_initial": True},
], indirect=True, ids=["roi-on", "roi-off"])
def test_real_original_reaches_ingest_or_original_fault_and_automatic_cleanup(incident, home, monkeypatch, enabled):
    runner, binding, target, api, seen = runner_for_incident(incident)
    if not enabled:
        # Only disable the new branch. No altered OCR, corrected text or assertion.
        monkeypatch.delattr(runner.bridge, "recheck_text_bubbles")
    before_hash = hashlib.sha256(Path(incident["image_path"]).read_bytes()).hexdigest()
    result = runner._read_one_wechat_target(binding, target, enforce_read_targets=True, wait_for_brain=False)
    calls = [json.loads(line) for line in incident["calls"].read_text().splitlines()]
    assert not any(row["action"] == "forbidden_ui_action" for row in calls)
    assert not storage.load_runtime_control()["inflight_flow_id"]
    assert runner.current_ui_lock is None
    assert not any(row["event"] == "inflight_flow_finish_failed" for row in storage.read_logs(limit=200))
    assert before_hash == hashlib.sha256(Path(incident["image_path"]).read_bytes()).hexdigest()
    assert all("--text-recheck-capture" not in row["argv"] for row in calls[:1])
    if incident["roi_reuse_enabled"] is False:
        assert "pre_send_frame_reuse" not in incident["initial"]
        assert not any("--chat-fact-roi-ocr" in row["argv"] or "--same-frame-full-ocr-evidence" in row["argv"] for row in calls)
    if enabled:
        assert result["ok"], result
        assert binding.run_status == "running"
        assert sum(row["action"] == "capture" for row in calls) == 2  # original + one new frame
        assert len(api.message_payloads) == 1
        evidence = next(row["metadata"]["text_recheck_evidence"] for row in storage.read_logs(limit=200)
                        if row["event"] == "c2_text_recheck_completed")
        assert evidence["adopted"] and evidence["consumed"]
        assert evidence["local_result"]["regions"][0]["scale"] == 2
        assert len(evidence["local_result"]["regions"]) == 1
        assert any("有现车推荐的吗" in m.get("content", "") for m in api.message_payloads[0]["messages"])
        assert result["new_customer_message_count"] == 1
        assert evidence["final_continuity"]["relation"] == "unique_viewport_slide_with_tail_append"
        assert len(evidence["final_continuity"]["matched_pairs"]) == 5
        assert evidence["final_continuity"]["new_suffix_indexes"] == [5]
    else:
        assert result["error_code"] == "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
        assert binding.run_status == "faulted"
        assert sum(row["action"] == "capture" for row in calls) == 1
        assert api.message_payloads[0]["messages"] == []
    (home / "acceptance.json").write_text(json.dumps({"enabled": enabled, "roi_reuse_enabled": incident["roi_reuse_enabled"], "result": result,
        "flow_events": api.inflight_flow_events, "calls": calls}, ensure_ascii=False, default=str), encoding="utf-8")


def test_recheck_quota_atomic_restart_corruption_and_other_entry(home):
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: storage.claim_read_recheck("one", kind="text"), range(8)))
    assert sum(outcomes) == 1
    assert storage.claim_read_recheck("one", kind="media_context") is False
    # A fresh connection/restarted caller cannot renew the same original read.
    with storage.db_connection() as connection:
        connection.execute("UPDATE c2_runtime_state SET value='broken json' WHERE key='read_recheck:one'")
        connection.commit()
    assert storage.claim_read_recheck("one", kind="pre_send") is False
    assert storage.claim_read_recheck("", kind="text") is False


@pytest.mark.parametrize("fault", ["capture", "ocr_timeout", "pause", "new_fault", "update", "rebind", "authorization", "target", "lock"])
def test_boundary_failure_never_adopts_or_reopens_and_cleans_flow(incident, home, monkeypatch, fault):
    runner, binding, target, api, _ = runner_for_incident(incident)
    original_capture = runner.bridge.get_messages
    original_recheck = runner.bridge.recheck_text_bubbles
    injected = []

    def capture(**kwargs):
        if fault == "capture":
            injected.append(fault)
            return {"ok": False, "error_code": "RPA_TIMEOUT", "reason": "injected capture failure"}
        return original_capture(**kwargs)

    def recheck(**kwargs):
        if kwargs["stage"] == "ocr":
            if fault == "ocr_timeout":
                injected.append(fault)
                return {"ok": False, "error_code": "RPA_TIMEOUT", "reason": "injected OCR timeout"}
            result = original_recheck(**kwargs)
            if fault == "pause": runner.set_run_status("paused")
            elif fault == "new_fault": runner.set_run_status("faulted")
            elif fault == "update": storage.set_update_new_work_gate(True, update_request_id="fixture-update")
            elif fault == "rebind": runner.binding = Binding("changed", "test-token", "changed", run_status="running")
            elif fault == "authorization": target.authorization_revision = "revoked"
            elif fault == "target": target.remark_code = "CJOTHER1"
            elif fault == "lock": runner.current_ui_lock._lease_lost.set()
            injected.append(fault)
            return result
        return original_recheck(**kwargs)

    monkeypatch.setattr(runner.bridge, "get_messages", capture)
    monkeypatch.setattr(runner.bridge, "recheck_text_bubbles", recheck)
    result = runner._read_one_wechat_target(binding, target, enforce_read_targets=False, wait_for_brain=False)
    assert result["ok"] is False
    assert injected == [fault]
    if fault == "new_fault":
        # This HTTP fixture deliberately has no fault-recovery capability.
        # Preserve the pending original transaction rather than fabricate an ack.
        assert api.message_payloads == []
        assert storage.has_pending_c2_outbox()
        assert storage.load_runtime_control()["inflight_flow_id"]
    else:
        assert api.message_payloads[0]["messages"] == []
        assert not storage.load_runtime_control()["inflight_flow_id"]
    evidence = next(row["metadata"]["text_recheck_evidence"] for row in storage.read_logs(limit=200)
                    if row["event"] == "c2_text_recheck_completed")
    assert evidence["consumed"] and not evidence.get("adopted")
    assert not evidence.get("exception_type"), evidence
    assert runner.current_ui_lock is None
    flow_id = next(x.split(":", 2)[2] for x in api.inflight_flow_events if x.startswith("start:c2_read:"))
    assert storage.claim_read_recheck(flow_id, kind="retry") is False
    if fault == "new_fault": assert runner.binding.run_status == "faulted"
    if fault in {"pause", "new_fault", "update"}:
        assert runner.binding.run_status != "running"
    if fault == "pause":
        # Export through the production entry after automatic Flow cleanup.
        # No test copies screenshots/reports into the archive on its behalf.
        from chejin_worker_client import incident_evidence
        import zipfile
        exported = incident_evidence.export_diagnostic_bundle(home / "recheck-failure.zip")
        with zipfile.ZipFile(exported) as archive:
            names = archive.namelist()
            assert any(name.endswith("text_bubble_recheck.json") for name in names), names
            assert any(name.endswith("bubble_0.png") for name in names), names
            index = json.loads(archive.read("evidence-index/export.json"))["files"]
            # Identical screenshots are content-deduplicated, with BOTH
            # source paths retained by the production export index.
            screenshots = [item for item in index if item["source_path"].endswith("messages.png")]
            assert len({item["source_path"] for item in screenshots}) >= 2, screenshots
            assert all(item["archive_path"] in names for item in screenshots)
            logs = json.loads(archive.read("logs/latest_logs.json"))
            recorded = next(row["metadata"]["text_recheck_evidence"] for row in logs
                            if row["event"] == "c2_text_recheck_completed")
            assert recorded["original_frame"]["frame_id"] != recorded["fresh_frame"]["frame_id"]
            assert recorded["local_result"]["regions"] == evidence["local_result"]["regions"]


def test_aligned_real_frame_has_zero_additional_capture_or_local_ocr(incident):
    # Fixture setup performs real OCR. The measured normal Worker read below
    # consumes its resulting frame, with the unchanged incident checkpoint.
    corrected = incident["bridge"].recheck_text_bubbles(stage="ocr", payload=incident["initial"],
        observation_ids=[incident["initial"]["observations"][4]["observation_id"]], remark_code="CJA7Y368")
    assert corrected["ok"], corrected
    baseline = incident["calls"].read_text()
    incident["initial"] = corrected
    runner, binding, target, api, _ = runner_for_incident(incident)
    result = runner._read_one_wechat_target(binding, target, enforce_read_targets=False, wait_for_brain=False)
    assert result["ok"], result
    assert incident["calls"].read_text() == baseline
    assert not any(row["event"] == "c2_text_recheck_completed" for row in storage.read_logs(limit=200))


def test_pre_send_real_recheck_keeps_new_customer_question_as_new(incident):
    from test_pre_send_checkpoint import _fact, _checkpoint
    from chejin_worker_client.pre_send_checkpoint import canonical_sha256
    from chejin_worker_client.task_runner import C2_PRE_SEND_REFRESH_PHASE
    runner, binding, target, api, _ = runner_for_incident(incident)
    context = copy.deepcopy(incident["checkpoint_context"])
    reply = incident["possible_sends"]["sends"][0]
    # Backend checkpoint boundary is a fixture, derived from the real confirmed
    # sent body. This is not claimed as a captured production backend response.
    last = _checkpoint(_fact(reply["reserved_worker_stable_id"], sender_role="self", message_type="text", content=reply["reply_text"]))["committed_tail"][0]
    last["business_projection"]["screen_order"] = 6
    context["checkpoint"]["committed_tail"].append(last)
    context["binding"]["checkpoint_digest"] = canonical_sha256(context["checkpoint"])
    target.raw["pre_send_fact_checkpoint_context"] = context
    # The checkpoint declares committed history; prepare its matching local
    # ledger too. This is fixture setup, not a real HTTP history-ingest test.
    for fact in context["checkpoint"]["committed_tail"]:
        storage.save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key=fact["source_message_key"],
            origin_read_run_id="fixture-confirmed-history-read",
            dedupe_key=None,
            message_type=fact["message_type"],
            terminal_state=fact["item_state"],
            ingest_state="confirmed",
        )
    result = runner._read_one_wechat_target(binding, target, enforce_read_targets=True,
        wait_for_brain=False, current_step="pre_send_refresh", current_only=True,
        operation_phase=C2_PRE_SEND_REFRESH_PHASE)
    assert result["ok"], result
    assert result["new_customer_message_count"] == 1
    assert not runner.bridge.sent_replies
    evidence = next(row["metadata"]["text_recheck_evidence"] for row in storage.read_logs(limit=200)
                    if row["event"] == "c2_text_recheck_completed")
    assert evidence["adopted"]
    assert evidence["final_decision"]["comparison_result"] == "checkpoint_unique_viewport_slide_with_suffix"
    assert len(evidence["final_decision"]["new_suffix_observation_ids"]) == 1
    assert not storage.load_runtime_control()["inflight_flow_id"]
    assert runner.current_ui_lock is None


@pytest.mark.parametrize("already_spent", [False, True])
def test_final_read_real_ocr_uses_same_quota_as_initial_and_pre_send(incident, already_spent):
    from chejin_worker_client.task_runner import FlowOutcomeAccumulator
    from chejin_worker_client.ui_lock import acquire_ui_lock
    corrected = incident["bridge"].recheck_text_bubbles(stage="ocr", payload=incident["initial"],
        observation_ids=[incident["initial"]["observations"][4]["observation_id"]], remark_code="CJA7Y368")
    assert corrected["ok"], corrected
    runner, binding, target, api, _ = runner_for_incident(incident)
    read_id = "final-read-shared-quota"
    # The saved pre-send checkpoint does not contain the HTTP-only origin
    # field required by the final-media planner. Model that backend boundary
    # explicitly; retain every original message/projection/commit record.
    for row in target.raw["identity_checkpoint"]["recent_messages"]:
        row["origin_read_run_id"] = "fixture-historical-read"
    baseline, errors = runner._align_initial_identity_frame(target=target, sidecar_payload=corrected, read_run_id=read_id)
    assert not errors, errors
    baseline["authoritative_frame_source"] = "initial_read"
    baseline["observations"] = runner._assign_sequence_new_suffix_identities(target=target,
        observations=baseline["observations"], evidence=baseline["sequence_alignment_evidence"], read_run_id=read_id)
    baseline_plan = runner._build_final_slot_incremental_plan(target=target, sidecar_payload=baseline, read_run_id=read_id)
    assert not baseline_plan["identity_errors"], baseline_plan["identity_errors"]
    if already_spent:
        assert storage.claim_read_recheck(read_id, kind="pre_send_context")
    calls_before = len(incident["calls"].read_text().splitlines())
    # This tests the final-read subroutine with a real caller-owned lease.
    # Its caller releases that lease; full-flow automatic release is asserted
    # separately by the public-entry positive/negative tests above.
    lease = acquire_ui_lock(operation_type="c2_read", owner=read_id)
    lease.start_auto_renew()
    try:
        result = runner._converge_current_screen_after_images(binding=binding, target=target,
            target_label=target.remark_code, sidecar_payload=baseline, lease=lease,
            action_cancel_requested=lease.cancel_requested, enforce_read_targets=True,
            flow_outcomes=FlowOutcomeAccumulator(origin_read_run_id=read_id))
    finally:
        lease.release()
    calls = [json.loads(line) for line in incident["calls"].read_text().splitlines()[calls_before:]]
    assert not any(row["action"] == "forbidden_ui_action" for row in calls)
    assert sum(row["action"] == "capture" for row in calls) == (1 if already_spent else 2)
    assert result["ok"] is not already_spent, result
    assert storage.claim_read_recheck(read_id, kind="initial_retry") is False
    if not already_spent:
        assert result["payload"]["authoritative_frame_source"] == "final_read"
        assert result["payload"]["authoritative_frame_reason"] == "local_text_recheck"
        assert result["payload"]["business_continuity_evidence"]["relation"] == "business_sequence_equal"
        assert len(result["payload"]["observations"]) == 6


def test_real_local_ocr_still_different_keeps_original_failure(incident):
    from chejin_worker_client.task_runner import reply_text_hash
    # Explicit negative backend fixture: the confirmed historical sentence
    # differs by a negation. The actual original pixels/OCR remain untouched.
    reply = incident["possible_sends"]["sends"][0]
    assert "一般" in reply["reply_text"]
    reply["reply_text"] = reply["reply_text"].replace("一般", "不一般", 1)
    reply["reply_text_hash"] = reply_text_hash(reply["reply_text"])
    runner, binding, target, api, _ = runner_for_incident(incident)
    result = runner._read_one_wechat_target(binding, target, enforce_read_targets=True, wait_for_brain=False)
    evidence = next(row["metadata"]["text_recheck_evidence"] for row in storage.read_logs(limit=200)
                    if row["event"] == "c2_text_recheck_completed")
    assert evidence["consumed"] and not evidence.get("adopted"), evidence
    actual = "".join(row["text"] for row in evidence["local_result"]["regions"][0]["ocr_items"])
    assert "一般" in actual and "不一般" not in actual
    assert result["error_code"] == "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS", result
    assert api.message_payloads[0]["messages"] == []
    assert not storage.load_runtime_control()["inflight_flow_id"]
    assert runner.current_ui_lock is None
    assert binding.run_status == "faulted"


def test_new_question_arriving_between_frames_is_not_lost(incident, home):
    from PIL import Image, ImageDraw
    # Explicitly synthetic earlier screen: remove only the last bubble/avatar
    # from a COPY. The new capture is the untouched incident original.
    earlier = Image.open(incident["image_path"]).convert("RGB")
    background = earlier.getpixel((500,690))
    ImageDraw.Draw(earlier).rectangle([301,625,782,698], fill=background)
    path = home / "synthetic-before-arrival.png"
    earlier.save(path)
    ui_path = home / "windows-fixture.json"
    ui = json.loads(ui_path.read_text())
    ui_path.write_text(json.dumps({**ui, "image_path": str(path)}))
    before = incident["bridge"].get_messages(display_name="CJA7Y368", rpa_session_key="", remark_code="CJA7Y368",
        target_mode="current", text_recheck_capture=True)
    assert before["ok"] and len(before["observations"]) == 5, before
    ui_path.write_text(json.dumps(ui))
    incident["initial"] = before
    runner, binding, target, api, _ = runner_for_incident(incident)
    result = runner._read_one_wechat_target(binding, target, enforce_read_targets=True, wait_for_brain=False)
    assert result["ok"], result
    assert result["new_customer_message_count"] == 1
    assert any("有现车推荐的吗" in m.get("content", "") for m in api.message_payloads[0]["messages"])
    assert not runner.bridge.sent_replies
    assert not storage.load_runtime_control()["inflight_flow_id"]


@pytest.mark.parametrize("defect", ["pixels", "geometry", "expired", "ocr_file"])
def test_real_saved_frame_binding_rejects_tampering_without_capture(incident, defect):
    payload = copy.deepcopy(incident["initial"])
    before_calls = incident["calls"].read_text().splitlines()
    stage = "validate"
    if defect == "pixels": payload["frame_observation"]["screenshot_sha256"] = "0"*64
    elif defect == "geometry": payload["frame_observation"]["geometry"]["width"] += 1
    elif defect == "expired": payload["frame_observation"]["captured_monotonic"] -= 181
    else:
        stage = "ocr"
        Path(payload["text_recheck_frame_path"]).write_text("{}")
    result = incident["bridge"].recheck_text_bubbles(stage=stage, payload=payload,
        observation_ids=[payload["observations"][4]["observation_id"]], remark_code="CJA7Y368")
    assert result["ok"] is False
    new_calls = [json.loads(line) for line in incident["calls"].read_text().splitlines()[len(before_calls):]]
    assert [row["action"] for row in new_calls] == ["start"]


def synthetic_payload(texts):
    from chejin_worker_client.message_viewport_projection import normalized_business_message_sequence
    rows = [{"observation_id": str(i), "message_type": "text", "row_kind": "text_bubble", "sender_role": "customer",
             "content_clean": text, "bubble_rect": [10,20+i*40,200,50+i*40]} for i,text in enumerate(texts)]
    payload = {"ok": True, "observations": rows, "frame_observation": {"frame_id": "synthetic"}}
    return payload, normalized_business_message_sequence(rows, message_viewport_bounds=None)


@pytest.mark.parametrize("before,after,expected", [
    (["开头", "下一句", "预算8万"], ["开头", "下一句", "预算9万"], ["2"]),
    (["开头", "下一句", "可以"], ["开头", "下一句", "不可以"], ["2"]),
    (["开头", "下一句", "没有变"], ["开头", "下一句", "没有变", "新问题"], []),
    (["旧画面"], ["全部新消息"], []),
    (["甲", "乙", "丙", "甲", "乙", "丙"], ["甲", "乙", "不同", "甲", "乙", "不同"], []),
])
def test_synthetic_selector_never_changes_a_character_or_accepts_identity(before, after, expected):
    _, old = synthetic_payload(before)
    payload, current = synthetic_payload(after)
    original = copy.deepcopy(payload)
    assert differing_text_observation_ids(old, payload) == expected
    assert payload == original
    if expected:
        from chejin_worker_client.message_viewport_projection import compare_business_viewport_continuity
        assert compare_business_viewport_continuity(old, current)["relation"] not in {
            "business_sequence_equal", "unique_tail_append", "unique_viewport_slide_with_tail_append"}
