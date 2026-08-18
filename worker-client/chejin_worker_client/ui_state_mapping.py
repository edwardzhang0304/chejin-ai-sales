from __future__ import annotations


SCAN_RUNTIME_STEPS = {"first_screen_session_scan"}
TARGET_READ_RUNTIME_STEPS = {
    "visible_hit_message_read",
    "state_target_message_read",
    "message_read",
    "target_chat_locating",
    "target_chat_reconfirming",
    "voice_transcribe_current_chat",
    "image_understanding_current_chat",
    "image_post_vision_final_read",
}
RUNTIME_STEP_TITLES = {
    "first_screen_session_scan": "正在扫描微信会话第一屏",
    "visible_hit_message_read": "正在读取第一屏命中的目标会话",
    "state_target_message_read": "正在定位并读取目标会话",
    "message_read": "正在读取客户最新消息",
    "target_chat_locating": "正在定位目标会话",
    "target_chat_reconfirming": "正在确认目标会话",
    "voice_transcribe_current_chat": "正在识别客户语音消息",
    "image_understanding_current_chat": "正在理解客户图片消息",
    "image_post_vision_final_read": "正在复核图片消息上下文",
    "c2_reply_context_recovering": "正在恢复客户会话上下文",
    "c3_brain_waiting": "等待服务端生成回复",
    "pre_send_refresh": "执行发送前复核",
    "add_friend_starting": "正在准备微信加好友",
}


def runtime_process_screen(current_step: str | None) -> str | None:
    step = str(current_step or "").strip()
    if step in SCAN_RUNTIME_STEPS:
        return "scan-running"
    if step in TARGET_READ_RUNTIME_STEPS:
        return "target-read-running"
    return None


def runtime_step_title(current_step: str | None, fallback: str) -> str:
    step = str(current_step or "").strip()
    return RUNTIME_STEP_TITLES.get(step, fallback)
