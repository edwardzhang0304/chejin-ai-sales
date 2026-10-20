"""Evidence layout only. This module never loads configuration or business state."""
from pathlib import Path
import os


def default_data_root() -> Path:
    if os.environ.get("CHEJIN_WORKER_HOME"):
        return Path(os.environ["CHEJIN_WORKER_HOME"])
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / "CheJinWorkerHook"


def artifacts(root: Path) -> Path:
    return Path(root) / "artifacts"


def incident_roots(root: Path) -> tuple[Path, Path]:
    return artifacts(root) / "incidents", Path(root) / "incidents"


def task_artifacts(root: Path, task_id: str) -> Path:
    # Identifiers are supplied by the task contract; still reject path traversal.
    if not task_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in task_id):
        raise ValueError("EVIDENCE_TASK_ID_INVALID")
    return artifacts(root) / "tasks" / task_id


def update_root(data_root: Path) -> Path:
    """Same path contract for updater and rescue collection; no CONFIG import."""
    configured = str(os.environ.get("CHEJIN_UPDATE_STAGING_ROOT") or "").strip()
    if configured:
        return Path(configured)
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    return (Path(local_app_data) if local_app_data else Path(data_root).parent) / "CheJinWorkerUpdate"
