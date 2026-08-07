from __future__ import annotations

import importlib.util
import json
import os
import platform
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests

from .config import CONFIG
from .rpa_bridge import RpaBridge, default_sidecar_script
from .storage import APP_DIR, load_binding


@dataclass
class PreflightCheck:
    name: str
    ok: bool
    severity: str
    message: str
    detail: dict[str, Any] | None = None


def backend_readyz_url(api_base_url: str) -> str:
    parsed = urlparse(api_base_url.rstrip("/"))
    path = parsed.path
    if path.endswith("/api"):
        path = path[: -len("/api")]
    return urlunparse(parsed._replace(path=f"{path}/readyz", params="", query="", fragment=""))


def module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def run_preflight(*, check_backend: bool = True, check_wechat: bool = True) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    checks.append(
        PreflightCheck(
            name="platform",
            ok=sys.platform == "win32",
            severity="error" if CONFIG.rpa_mode == "real" else "warning",
            message="Windows 环境可运行真实微信 RPA。" if sys.platform == "win32" else f"当前系统是 {platform.system()}，真实微信 RPA 只能在 Windows 验收。",
            detail={"platform": sys.platform, "rpa_mode": CONFIG.rpa_mode},
        )
    )

    checks.extend(
        [
            dependency_check("requests"),
            dependency_check("PySide6"),
            dependency_check("pyperclip"),
            dependency_check("pywinauto", required=CONFIG.rpa_mode == "real" and sys.platform == "win32"),
        ]
    )
    checks.append(sidecar_check(default_sidecar_script()))
    if CONFIG.rpa_mode == "real" and sys.platform == "win32":
        checks.append(omniauto_vision_ocr_check())
    checks.append(vision_credential_check())
    checks.append(app_dir_check(APP_DIR))
    checks.append(binding_check())

    if check_backend:
        checks.append(backend_check(CONFIG.api_base_url))
    if check_wechat:
        checks.append(wechat_check())
    return checks


def dependency_check(module_name: str, *, required: bool = True) -> PreflightCheck:
    ok = module_available(module_name)
    return PreflightCheck(
        name=f"dependency:{module_name}",
        ok=ok,
        severity="error" if required else "warning",
        message=f"{module_name} 已安装。" if ok else f"{module_name} 未安装。",
    )


def sidecar_check(path: Path) -> PreflightCheck:
    ok = path.exists()
    return PreflightCheck(
        name="sidecar",
        ok=ok,
        severity="error",
        message=f"sidecar 文件存在：{path}" if ok else f"sidecar 文件不存在：{path}",
        detail={"path": str(path)},
    )


def omniauto_vision_ocr_check() -> PreflightCheck:
    from .omniauto_ocr_client import probe_omniauto_ocr_subprocess

    result = probe_omniauto_ocr_subprocess()
    ok = result.get("ok") is True
    return PreflightCheck(
        name="vision_ocr_subprocess",
        ok=ok,
        severity="error",
        message=(
            "图片复核 OCR 独立进程可用。"
            if ok
            else "图片复核 OCR 独立进程不可用。"
        ),
        detail=result,
    )


def vision_credential_check() -> PreflightCheck:
    from .vision_credentials import (
        is_official_vision_runtime,
        vision_credential_status,
    )

    status = vision_credential_status()
    configured = status.get("configured") is True
    official = is_official_vision_runtime()
    safe_detail = {
        key: value
        for key, value in status.items()
        if key != "configured"
    }
    return PreflightCheck(
        name="vision_credential",
        ok=configured,
        severity="error" if official else "warning",
        message=(
            "内置 Vision 凭据已配置。"
            if configured and official
            else "Vision 开发凭据已配置。"
            if configured
            else "内置 Vision 凭据未配置。"
            if official
            else "Vision 开发凭据未配置。"
        ),
        detail=safe_detail,
    )


def app_dir_check(path: Path) -> PreflightCheck:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".preflight-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return PreflightCheck("app_dir", True, "error", f"本机数据目录可写：{path}", {"path": str(path)})
    except Exception as exc:
        return PreflightCheck("app_dir", False, "error", f"本机数据目录不可写：{exc}", {"path": str(path)})


def binding_check() -> PreflightCheck:
    try:
        binding = load_binding()
    except Exception as exc:
        return PreflightCheck("binding", False, "warning", f"读取本机绑定信息失败：{exc}")
    if not binding:
        return PreflightCheck("binding", False, "warning", "本机尚未绑定 Worker，首次启动需输入 Worker ID 和 Worker Token。")
    return PreflightCheck(
        "binding",
        True,
        "error",
        "本机已有 Worker 绑定信息。",
        {"worker_id": binding.worker_id, "client_instance_id": binding.client_instance_id, "run_status": binding.run_status},
    )


def backend_check(api_base_url: str) -> PreflightCheck:
    url = backend_readyz_url(api_base_url)
    try:
        response = requests.get(url, timeout=5)
        ok = response.status_code == 200
        return PreflightCheck(
            "backend",
            ok,
            "error",
            f"后端 readyz 可用：{url}" if ok else f"后端 readyz 非 200：{response.status_code}",
            {"url": url, "status_code": response.status_code, "body": response.text[:300]},
        )
    except Exception as exc:
        return PreflightCheck("backend", False, "error", f"后端 readyz 不可达：{exc}", {"url": url})


def wechat_check() -> PreflightCheck:
    if CONFIG.rpa_mode == "mock":
        return PreflightCheck("wechat", True, "warning", "当前是 mock RPA 模式，不探测真实微信。")
    status, wechat_status = RpaBridge().probe()
    ok = status == "ready" and wechat_status == "logged_in"
    return PreflightCheck(
        "wechat",
        ok,
        "error",
        "已检测到可用微信桌面客户端。" if ok else f"微信探测未通过：rpa={status}, wechat={wechat_status}",
        {"rpa_component_status": status, "wechat_status": wechat_status},
    )


def has_blocking_failures(checks: list[PreflightCheck]) -> bool:
    return any(not item.ok and item.severity == "error" for item in checks)


def checks_to_dict(checks: list[PreflightCheck]) -> dict[str, Any]:
    return {"ok": not has_blocking_failures(checks), "checks": [asdict(item) for item in checks]}


def format_text(checks: list[PreflightCheck]) -> str:
    lines = ["车金 Worker 客户端预检", ""]
    for item in checks:
        marker = "OK" if item.ok else ("ERROR" if item.severity == "error" else "WARN")
        lines.append(f"[{marker}] {item.name}: {item.message}")
    lines.append("")
    lines.append("结论：" + ("通过" if not has_blocking_failures(checks) else "不通过"))
    return "\n".join(lines)


def format_json(checks: list[PreflightCheck]) -> str:
    return json.dumps(checks_to_dict(checks), ensure_ascii=False, indent=2)


def write_report(checks: list[PreflightCheck], path: Path | None = None) -> Path:
    target = path or APP_DIR / "preflight-report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(format_json(checks), encoding="utf-8")
    return target
