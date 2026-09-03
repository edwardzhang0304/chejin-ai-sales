from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_API_BASE_URL = "https://jiangsuchejin.com/api"


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
    c2_low_priority_lock_timeout_seconds: float
    c2_message_min_ocr_confidence: float
    c2_message_failure_cooldown_seconds: float
    c2_voice_transcribe_max_duration_seconds: int
    c2_stop_guard_before_voice_seconds: float
    c3_brain_no_progress_watchdog_seconds: float
    c3_brain_poll_interval_seconds: float
    artifact_success_retention_days: int
    artifact_critical_retention_days: int
    artifact_max_bytes: int
    artifact_cleanup_interval_seconds: float
    outbox_terminal_retention_days: int
    outbox_max_terminal_rows: int
    task_lease_renew_interval_seconds: float
    observability_enabled: bool
    observability_upload_batch_size: int
    observability_upload_timeout_seconds: float
    task_safe_wake_enabled: bool
    c2_locate_frame_reuse_enabled: bool
    c3_pre_send_roi_reuse_enabled: bool
    c3_send_frame_local_reuse_enabled: bool

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
            c2_low_priority_lock_timeout_seconds=float(os.environ.get("CHEJIN_C2_LOW_PRIORITY_LOCK_TIMEOUT_SECONDS", "0.2")),
            c2_message_min_ocr_confidence=float(os.environ.get("CHEJIN_C2_MESSAGE_MIN_OCR_CONFIDENCE", "0.45")),
            c2_message_failure_cooldown_seconds=float(os.environ.get("CHEJIN_C2_MESSAGE_FAILURE_COOLDOWN_SECONDS", "45")),
            c2_voice_transcribe_max_duration_seconds=int(os.environ.get("CHEJIN_C2_VOICE_TRANSCRIBE_MAX_DURATION_SECONDS", "240")),
            c2_stop_guard_before_voice_seconds=float(os.environ.get("CHEJIN_C2_STOP_GUARD_BEFORE_VOICE_SECONDS", "1.5")),
            c3_brain_no_progress_watchdog_seconds=float(
                os.environ.get(
                    "CHEJIN_C3_BRAIN_NO_PROGRESS_WATCHDOG_SECONDS",
                    os.environ.get("CHEJIN_C3_BRAIN_WAIT_TIMEOUT_SECONDS", "360"),
                )
            ),
            c3_brain_poll_interval_seconds=float(os.environ.get("CHEJIN_C3_BRAIN_POLL_INTERVAL_SECONDS", "0.8")),
            artifact_success_retention_days=int(
                os.environ.get("CHEJIN_ARTIFACT_SUCCESS_RETENTION_DAYS", "7")
            ),
            artifact_critical_retention_days=int(
                os.environ.get("CHEJIN_ARTIFACT_CRITICAL_RETENTION_DAYS", "30")
            ),
            artifact_max_bytes=int(
                os.environ.get("CHEJIN_ARTIFACT_MAX_BYTES", str(2 * 1024 * 1024 * 1024))
            ),
            artifact_cleanup_interval_seconds=float(
                os.environ.get("CHEJIN_ARTIFACT_CLEANUP_INTERVAL_SECONDS", "86400")
            ),
            outbox_terminal_retention_days=int(
                os.environ.get("CHEJIN_OUTBOX_TERMINAL_RETENTION_DAYS", "30")
            ),
            outbox_max_terminal_rows=int(
                os.environ.get("CHEJIN_OUTBOX_MAX_TERMINAL_ROWS", "5000")
            ),
            task_lease_renew_interval_seconds=float(
                os.environ.get("CHEJIN_TASK_LEASE_RENEW_INTERVAL_SECONDS", "15")
            ),
            observability_enabled=env_bool("CHEJIN_OBSERVABILITY_ENABLED", True),
            observability_upload_batch_size=max(
                1,
                min(
                    200,
                    int(os.environ.get("CHEJIN_OBSERVABILITY_UPLOAD_BATCH_SIZE", "100")),
                ),
            ),
            observability_upload_timeout_seconds=max(
                0.2,
                float(os.environ.get("CHEJIN_OBSERVABILITY_UPLOAD_TIMEOUT_SECONDS", "3")),
            ),
            task_safe_wake_enabled=env_bool(
                "CHEJIN_TASK_SAFE_WAKE_ENABLED", True
            ),
            c2_locate_frame_reuse_enabled=env_bool(
                "CHEJIN_C2_LOCATE_FRAME_REUSE_ENABLED", True
            ),
            c3_pre_send_roi_reuse_enabled=env_bool(
                "CHEJIN_C3_PRE_SEND_ROI_REUSE_ENABLED", True
            ),
            c3_send_frame_local_reuse_enabled=env_bool(
                "CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED", True
            ),
        )


CONFIG = ClientConfig.from_env()
