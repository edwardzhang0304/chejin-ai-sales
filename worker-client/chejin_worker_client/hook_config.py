"""One deployment description for the WeChat shortcut and local callback."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PureWindowsPath

DEPLOYMENT_KEY = "hook_deployment_v1"
INSTALLATION_KEY = "hook_installation_v1"
TRANSPORT_KEY = "wechat_transport_v1"
VOICE_SETTING_KEY = "hook_native_auto_text_confirmation_v1"
CALLBACK_PATH = "/receiveChatBotMsg"


@dataclass(frozen=True)
class HookDeployment:
    revision: str
    wechat_exe: str
    dll_sha256: str
    hook_api_port: int
    receiver_port: int

    def __post_init__(self) -> None:
        if not isinstance(self.revision, str) or not self.revision.strip():
            raise ValueError("HOOK_DEPLOYMENT_REVISION_INVALID")
        if not isinstance(self.wechat_exe, str):
            raise ValueError("HOOK_DEPLOYMENT_PATH_INVALID")
        path = PureWindowsPath(self.wechat_exe)
        if (not path.is_absolute() or path.drive.startswith("\\\\")
                or path.name.lower() != "wechat.exe" or ".." in path.parts):
            raise ValueError("HOOK_DEPLOYMENT_PATH_INVALID")
        if not isinstance(self.dll_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", self.dll_sha256):
            raise ValueError("HOOK_DEPLOYMENT_HASH_INVALID")
        for port in (self.hook_api_port, self.receiver_port):
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("HOOK_DEPLOYMENT_PORT_INVALID")
        if self.hook_api_port == self.receiver_port:
            raise ValueError("HOOK_DEPLOYMENT_PORT_CONFLICT")

    @property
    def api_base_url(self) -> str:
        return f"http://127.0.0.1:{self.hook_api_port}"

    @property
    def callback_url(self) -> str:
        return f"http://127.0.0.1:{self.receiver_port}{CALLBACK_PATH}"

    @property
    def dll_path(self) -> str:
        return str(PureWindowsPath(self.wechat_exe).with_name("version.dll"))

    @property
    def launch_arguments(self) -> tuple[str, ...]:
        return (f"StartPort={self.hook_api_port}", "ConnectType=http", "RecvType=0")


def load_hook_deployment() -> HookDeployment:
    """Read protected settings. Never guess ports or fall back to production."""
    from . import storage

    with storage.db_connection() as conn:
        row = conn.execute("SELECT value FROM client_settings WHERE key=?", (DEPLOYMENT_KEY,)).fetchone()
    if row is None:
        raise ValueError("HOOK_DEPLOYMENT_MISSING")
    try:
        payload = json.loads(row["value"])
        if not isinstance(payload, dict) or set(payload) != set(HookDeployment.__dataclass_fields__):
            raise ValueError("HOOK_DEPLOYMENT_FIELDS_INVALID")
        return HookDeployment(**payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("HOOK_DEPLOYMENT_INVALID") from exc
