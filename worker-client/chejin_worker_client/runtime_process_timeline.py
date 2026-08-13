from __future__ import annotations

from datetime import datetime
from typing import Any


_STEP_PHASES: dict[str, tuple[str, str]] = {
    "target_chat_locating": ("locate", "定位并确认客户会话"),
    "target_chat_reconfirming": ("locate", "定位并确认客户会话"),
    "visible_hit_message_read": ("read", "读取客户最新消息"),
    "state_target_message_read": ("read", "读取客户最新消息"),
    "message_read": ("read", "读取客户最新消息"),
    "voice_prepare_current_chat": ("voice", "识别语音消息"),
    "voice_transcribe_current_chat": ("voice", "识别语音消息"),
    "image_understanding_current_chat": ("image", "理解图片消息"),
    "image_post_vision_final_read": ("image", "理解图片消息"),
    "c3_brain_waiting": ("decision", "服务端正在判断处理方式"),
    "pre_send_refresh": ("pre_send", "发送前复核"),
}


def _event_time(value: Any) -> str:
    text = str(value or "").strip()
    if text:
        return text
    return datetime.now().strftime("%H:%M:%S")


class RuntimeProcessTimeline:
    """UI-only projection of one continuously displayed customer transaction."""

    def __init__(self, *, max_steps: int = 24) -> None:
        self.max_steps = max(8, int(max_steps))
        self.scan_steps: list[dict[str, Any]] = []
        self.customer_steps: list[dict[str, Any]] = []
        self.customer_conversation_id = ""
        self.customer_transaction_id = ""
        self.customer_active = False
        self.customer_terminal_state = ""
        self.last_process_kind = ""

    @staticmethod
    def _finish_current(steps: list[dict[str, Any]]) -> None:
        for step in reversed(steps):
            if step.get("state") == "current":
                step["state"] = "done"
                return

    def _append_or_focus(
        self,
        steps: list[dict[str, Any]],
        *,
        phase: str,
        title: str,
        description: str = "",
        time_text: str = "",
    ) -> None:
        existing = next(
            (item for item in steps if item.get("_phase") == phase),
            None,
        )
        self._finish_current(steps)
        if existing is not None:
            existing["state"] = "current"
            if description:
                existing["description"] = description
            return
        step: dict[str, Any] = {
            "_phase": phase,
            "state": "current",
            "title": title,
        }
        if description:
            step["description"] = description
        if time_text:
            step["time"] = time_text
        steps.append(step)
        if len(steps) > self.max_steps:
            del steps[: len(steps) - self.max_steps]

    @staticmethod
    def _public_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in step.items() if not key.startswith("_")}
            for step in steps
        ]

    def apply(self, event: dict[str, Any]) -> None:
        event_name = str(event.get("event") or "").strip()
        event_time = _event_time(event.get("time"))
        if event_name == "scan_started":
            self.last_process_kind = "scan"
            self.scan_steps = [
                {
                    "_phase": "scan",
                    "state": "current",
                    "title": "扫描微信会话第一屏",
                    "description": "Worker 正在检查当前可见会话。",
                }
            ]
            return
        if event_name == "scan_completed":
            self._finish_current(self.scan_steps)
            hit_count = max(0, int(event.get("visible_hit_count") or 0))
            if hit_count:
                self.scan_steps.append(
                    {
                        "_phase": "scan_result",
                        "state": "done",
                        "title": "发现待处理客户",
                        "description": f"第一屏命中 {hit_count} 个已授权目标。",
                    }
                )
            else:
                self.scan_steps.append(
                    {
                        "_phase": "scan_result",
                        "state": "done",
                        "title": "未发现待处理客户",
                        "finalText": "等待下一轮检查",
                    }
                )
            return
        if event_name == "scan_failed":
            self._finish_current(self.scan_steps)
            self.scan_steps.append(
                {
                    "_phase": "scan_error",
                    "state": "error",
                    "title": "首屏扫描失败",
                    "description": str(event.get("error_code") or "扫描未完成。"),
                    "finalText": "本次检查失败",
                }
            )
            return

        conversation_id = str(event.get("conversation_id") or "").strip()
        transaction_id = str(event.get("transaction_id") or "").strip()
        if event_name == "customer_started":
            self.last_process_kind = "customer"
            should_reset = (
                not self.customer_steps
                or self.customer_terminal_state
                or (
                    conversation_id
                    and self.customer_conversation_id
                    and conversation_id != self.customer_conversation_id
                )
            )
            if should_reset:
                self.customer_steps = []
            self.customer_conversation_id = conversation_id
            self.customer_transaction_id = transaction_id
            self.customer_terminal_state = ""
            self.customer_active = True
            source = str(event.get("source") or "状态机定向读取")
            remark_code = str(event.get("remark_code") or "").strip()
            description = f"来源：{source}"
            if remark_code:
                description += f" · {remark_code}"
            self._append_or_focus(
                self.customer_steps,
                phase="discovered",
                title="发现待处理客户",
                description=description,
                time_text=event_time,
            )
            return
        if not self.customer_active:
            return
        if conversation_id and self.customer_conversation_id and (
            conversation_id != self.customer_conversation_id
        ):
            return

        if event_name == "step":
            step_name = str(event.get("step") or "").strip()
            operation_phase = str(event.get("operation_phase") or "").strip()
            if operation_phase == "pre_send_refresh":
                phase, title = ("pre_send", "发送前复核")
            else:
                mapped = _STEP_PHASES.get(step_name)
                if mapped is None:
                    return
                phase, title = mapped
            if phase == "decision":
                self._append_or_focus(
                    self.customer_steps,
                    phase="uploaded",
                    title="消息已回传服务端",
                )
                self._finish_current(self.customer_steps)
            self._append_or_focus(
                self.customer_steps,
                phase=phase,
                title=title,
                time_text=event_time,
            )
            return
        if event_name == "reply_ready":
            self._append_or_focus(
                self.customer_steps,
                phase="reply",
                title="生成并审核 AI 回复",
                time_text=event_time,
            )
            return
        if event_name == "send_started":
            self._append_or_focus(
                self.customer_steps,
                phase="send",
                title="发送微信消息",
                time_text=event_time,
            )
            return
        if event_name == "customer_completed":
            self._finish_current(self.customer_steps)
            terminal_state = str(event.get("terminal_state") or "completed")
            error_code = str(event.get("error_code") or "").strip()
            terminal_map = {
                "reply_sent": ("done", "确认并回传结果", "回复已发送"),
                "no_change": ("done", "本次检查完成", "没有新消息"),
                "no_reply": ("done", "服务端判断无需回复", "本次处理完成"),
                "handoff": ("done", "已转人工", "等待销售处理"),
                "completed": ("done", "本次处理完成", "消息已回传"),
                "failed": ("error", "本次处理失败", "处理已停止"),
            }
            state, title, final_text = terminal_map.get(
                terminal_state,
                terminal_map["completed"],
            )
            terminal: dict[str, Any] = {
                "_phase": "terminal",
                "state": state,
                "title": title,
                "finalText": final_text,
            }
            if error_code:
                terminal["description"] = error_code
            self.customer_steps.append(terminal)
            self.customer_active = False
            self.customer_terminal_state = terminal_state

    def scan_model(self) -> list[dict[str, Any]]:
        return self._public_steps(self.scan_steps)

    def customer_model(self) -> list[dict[str, Any]]:
        return self._public_steps(self.customer_steps)
