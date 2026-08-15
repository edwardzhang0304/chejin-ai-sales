from __future__ import annotations

from chejin_worker_client.runtime_process_timeline import RuntimeProcessTimeline
from chejin_worker_client.task_runner import TaskRunner


def _customer_event(event: str, **extra: object) -> dict[str, object]:
    return {
        "event": event,
        "conversation_id": "conversation-1",
        "transaction_id": "read-1",
        "remark_code": "CJP6M3R7",
        **extra,
    }


def test_scan_without_hit_stays_a_short_standalone_flow() -> None:
    timeline = RuntimeProcessTimeline()

    timeline.apply({"event": "scan_started"})
    timeline.apply({"event": "scan_completed", "visible_hit_count": 0})

    assert [step["title"] for step in timeline.scan_model()] == [
        "扫描微信会话第一屏",
        "未发现待处理客户",
    ]
    assert timeline.scan_model()[-1]["finalText"] == "等待下一轮检查"
    assert timeline.customer_model() == []


def test_cancelled_scan_has_a_terminal_step_and_no_running_card() -> None:
    timeline = RuntimeProcessTimeline()

    timeline.apply({"event": "scan_started"})
    timeline.apply({"event": "scan_cancelled"})

    assert [step["title"] for step in timeline.scan_model()] == [
        "扫描微信会话第一屏",
        "首屏扫描已停止",
    ]
    assert all(
        step["state"] != "current" for step in timeline.scan_model()
    )
    assert timeline.scan_model()[-1]["finalText"] == "已暂停接单"


def test_one_customer_transaction_accumulates_without_restarting_for_ai_reply() -> None:
    timeline = RuntimeProcessTimeline()
    timeline.apply(
        _customer_event(
            "customer_started",
            source="状态机定向读取",
        )
    )
    for step in (
        "target_chat_locating",
        "message_read",
        "voice_transcribe_current_chat",
        "image_understanding_current_chat",
        "c3_brain_waiting",
    ):
        timeline.apply(_customer_event("step", step=step))
    timeline.apply(_customer_event("reply_ready"))
    timeline.apply(
        _customer_event(
            "step",
            step="target_chat_locating",
            operation_phase="pre_send_refresh",
        )
    )
    timeline.apply(_customer_event("send_started"))
    timeline.apply(
        _customer_event(
            "customer_completed",
            terminal_state="reply_sent",
        )
    )

    assert [step["title"] for step in timeline.customer_model()] == [
        "发现待处理客户",
        "定位并确认客户会话",
        "读取客户最新消息",
        "识别语音消息",
        "理解图片消息",
        "消息已回传服务端",
        "服务端正在判断处理方式",
        "生成并审核 AI 回复",
        "发送前复核",
        "发送微信消息",
        "确认并回传结果",
    ]
    assert timeline.customer_model()[-1]["finalText"] == "回复已发送"


def test_media_nodes_only_appear_when_the_corresponding_step_happens() -> None:
    timeline = RuntimeProcessTimeline()
    timeline.apply(_customer_event("customer_started"))
    timeline.apply(_customer_event("step", step="target_chat_locating"))
    timeline.apply(_customer_event("step", step="message_read"))
    timeline.apply(
        _customer_event("customer_completed", terminal_state="no_change")
    )

    titles = [step["title"] for step in timeline.customer_model()]
    assert "识别语音消息" not in titles
    assert "理解图片消息" not in titles
    assert titles[-1] == "本次检查完成"


def test_repeated_runtime_updates_do_not_duplicate_a_phase() -> None:
    timeline = RuntimeProcessTimeline()
    timeline.apply(_customer_event("customer_started"))
    timeline.apply(_customer_event("step", step="voice_prepare_current_chat"))
    timeline.apply(_customer_event("step", step="voice_transcribe_current_chat"))

    assert [
        step["title"] for step in timeline.customer_model()
    ].count("识别语音消息") == 1


def test_next_customer_replaces_the_previous_terminal_transaction() -> None:
    timeline = RuntimeProcessTimeline()
    timeline.apply(_customer_event("customer_started"))
    timeline.apply(
        _customer_event("customer_completed", terminal_state="completed")
    )
    timeline.apply(
        {
            "event": "customer_started",
            "conversation_id": "conversation-2",
            "transaction_id": "read-2",
            "remark_code": "CJT9V5X1",
            "source": "微信会话第一屏命中",
        }
    )

    assert len(timeline.customer_model()) == 1
    assert "CJT9V5X1" in timeline.customer_model()[0]["description"]


def test_task_runner_publishes_step_changes_as_ui_only_events() -> None:
    events: list[dict[str, object]] = []
    runner = TaskRunner(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        on_profile=lambda _value: None,
        on_status=lambda _value: None,
        on_step=lambda _value: None,
        on_task=lambda _value: None,
        on_result=lambda _value: None,
        on_error=lambda _value: None,
        on_runtime_process=events.append,
    )
    runner._runtime_process_context = {
        "conversation_id": "conversation-1",
        "transaction_id": "read-1",
        "operation_phase": "authorized_read",
    }

    runner.current_step = "target_chat_locating"
    runner.current_step = "target_chat_locating"
    runner.current_step = "voice_transcribe_current_chat"

    assert [event["step"] for event in events] == [
        "target_chat_locating",
        "voice_transcribe_current_chat",
    ]
