from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_API_BASE_URL = "http://127.0.0.1:8000/api"


def default_app_dir() -> Path:
    if os.environ.get("CHEJIN_WORKER_HOME"):
        return Path(os.environ["CHEJIN_WORKER_HOME"])
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "CheJinWorker"
    return Path.home() / ".chejin-worker"


@dataclass(frozen=True)
class ClientConfig:
    api_base_url: str
    app_dir: Path
    heartbeat_interval_seconds: float
    poll_interval_seconds: float
    api_timeout_seconds: float
    rpa_mode: str
    rpa_mock_step_delay_seconds: float
    ui_lock_lease_seconds: float
    ui_lock_renew_interval_seconds: float
    ui_lock_acquire_timeout_seconds: float
    ui_step_timeout_seconds: float
    c2_enabled: bool
    c2_session_scan_interval_seconds: float
    c2_message_read_interval_seconds: float
    c2_read_targets_limit: int

    @classmethod
    def from_env(cls) -> "ClientConfig":
        def env_bool(key: str, default: bool) -> bool:
            raw = os.environ.get(key)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            api_base_url=os.environ.get("CHEJIN_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/"),
            app_dir=default_app_dir(),
            heartbeat_interval_seconds=float(os.environ.get("CHEJIN_HEARTBEAT_INTERVAL", "20")),
            poll_interval_seconds=float(os.environ.get("CHEJIN_TASK_POLL_INTERVAL", "4")),
            api_timeout_seconds=float(os.environ.get("CHEJIN_API_TIMEOUT", "12")),
            rpa_mode=os.environ.get("CHEJIN_RPA_MODE", "real").strip().lower(),
            rpa_mock_step_delay_seconds=float(os.environ.get("CHEJIN_RPA_MOCK_STEP_DELAY_SECONDS", "0.45")),
            ui_lock_lease_seconds=float(os.environ.get("CHEJIN_UI_LOCK_LEASE_SECONDS", "90")),
            ui_lock_renew_interval_seconds=float(os.environ.get("CHEJIN_UI_LOCK_RENEW_INTERVAL_SECONDS", "15")),
            ui_lock_acquire_timeout_seconds=float(os.environ.get("CHEJIN_UI_LOCK_ACQUIRE_TIMEOUT_SECONDS", "20")),
            ui_step_timeout_seconds=float(os.environ.get("CHEJIN_UI_STEP_TIMEOUT_SECONDS", "120")),
            c2_enabled=env_bool("CHEJIN_C2_ENABLED", True),
            c2_session_scan_interval_seconds=float(os.environ.get("CHEJIN_C2_SESSION_SCAN_INTERVAL_SECONDS", "30")),
            c2_message_read_interval_seconds=float(os.environ.get("CHEJIN_C2_MESSAGE_READ_INTERVAL_SECONDS", "10")),
            c2_read_targets_limit=int(os.environ.get("CHEJIN_C2_READ_TARGETS_LIMIT", "20")),
        )


CONFIG = ClientConfig.from_env()
