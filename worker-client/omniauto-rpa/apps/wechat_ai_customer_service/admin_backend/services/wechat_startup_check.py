"""Shared WeChat startup capability checks for long-running modules."""

from __future__ import annotations

from typing import Any

from apps.wechat_ai_customer_service.adapters.wechat_connector import WeChatConnector


def run_wechat_startup_self_check(*, require_send: bool, module_name: str) -> dict[str, Any]:
    capability = WeChatConnector().capabilities(interactive=True)
    decision = evaluate_wechat_capability(
        capability,
        require_send=require_send,
        module_name=module_name,
    )
    return {**decision, "wechat_capability": capability}


def evaluate_wechat_capability(
    capability: dict[str, Any],
    *,
    require_send: bool,
    module_name: str,
) -> dict[str, Any]:
    online = bool(capability.get("online"))
    receive = capability.get("receive") if isinstance(capability.get("receive"), dict) else {}
    send = capability.get("send") if isinstance(capability.get("send"), dict) else {}
    receive_ok = bool(receive.get("ok") or (online and capability.get("adapter") == "wxauto4"))
    send_ok = bool(send.get("ok") or (online and capability.get("adapter") == "wxauto4"))
    scheme = str(capability.get("scheme") or capability.get("adapter") or "")
    display = wechat_scheme_display_name(scheme)

    if not online:
        failure = rpa_failure_detail_payload(capability)
        state = str(failure.get("state") or "")
        reason = str(failure.get("reason") or "")
        if state == "main_window_in_tray" or reason == "wechat_window_in_tray":
            return {
                "ok": False,
                "detail": "wechat_window_in_tray",
                "scheme": scheme or "wechat_window_in_tray",
                "message": f"{module_name}启动前自检未通过：已检测到微信正在运行，但主窗口收在托盘里。请手动点开微信主窗口，确认聊天页面正常显示后再启动。",
            }
        if state == "main_window_geometry_invalid" and reason == "window_offscreen_or_minimized":
            return {
                "ok": False,
                "detail": "wechat_window_minimized",
                "scheme": scheme or "wechat_window_minimized",
                "message": f"{module_name}启动前自检未通过：已检测到微信进程，但微信主窗口处于最小化或托盘状态，当前无法截图读取。请从任务栏点开微信后重试。",
            }
        if state == "blank_render_detected" or reason == "blank_render":
            return {
                "ok": False,
                "detail": "wechat_blank_render",
                "scheme": scheme or "wechat_blank_render",
                "message": f"{module_name}启动前自检未通过：微信窗口疑似白屏或渲染卡住。请关闭当前微信窗口并从任务栏重新打开后再启动。",
            }
        return {
            "ok": False,
            "detail": "wechat_not_ready",
            "scheme": scheme or "wechat_not_ready",
            "message": f"{module_name}启动前自检未通过：未检测到已登录的微信主窗口。请先打开微信并完成登录，再重新启动。",
        }
    if not receive_ok:
        return {
            "ok": False,
            "detail": "wechat_receive_unavailable",
            "scheme": scheme,
            "message": f"{module_name}启动前自检未通过：已检测到微信，但当前窗口无法稳定读取聊天记录。请回到微信主窗口后重试。",
        }
    if require_send and not send_ok:
        return {
            "ok": False,
            "detail": "wechat_send_unavailable",
            "scheme": scheme,
            "message": f"{module_name}启动前自检未通过：当前只能读取微信，暂不满足安全发送条件。请把微信主窗口放大并停留在正常会话页后重试。",
        }

    action = "读取并发送" if require_send else "采集聊天记录"
    return {
        "ok": True,
        "detail": "wechat_capability_ready",
        "scheme": scheme,
        "message": f"{module_name}启动前自检通过：当前使用{display}，可以{action}。",
    }


def rpa_failure_detail_payload(capability: dict[str, Any]) -> dict[str, Any]:
    """Prefer the underlying Win32/OCR failure over generic reserve wrapping."""
    if not isinstance(capability, dict):
        return {}
    primary = capability.get("primary_status") if isinstance(capability.get("primary_status"), dict) else {}
    if primary:
        state = str(primary.get("state") or "")
        reason = str(primary.get("reason") or "")
        if state in {"main_window_in_tray", "main_window_geometry_invalid", "blank_render_detected", "login_window_detected"} or reason:
            return primary
    return capability


def wechat_scheme_display_name(scheme: str) -> str:
    return {
        "wxauto4": "wxauto4 控件级方案",
        "win32_ocr_uia": "Win32/OCR + UIA 控件方案",
        "win32_ocr_guarded_click": "Win32/OCR 安全兜底方案（已启用限频/熔断）",
        "win32_ocr_receive_only": "Win32/OCR 只读记录方案",
        "win32_ocr_blocked": "Win32/OCR 阻塞检测方案",
        "wechat_not_online": "微信未登录状态",
    }.get(str(scheme or ""), str(scheme or "未知方案"))
