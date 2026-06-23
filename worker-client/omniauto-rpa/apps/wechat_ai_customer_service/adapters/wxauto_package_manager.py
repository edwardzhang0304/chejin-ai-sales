"""Package maintenance helpers for the wxauto sidecar environment."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from packaging.version import InvalidVersion, Version
except Exception:  # pragma: no cover - packaging is normally available with pip.
    InvalidVersion = ValueError  # type: ignore[assignment]
    Version = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[3]
SIDECAR_PYTHON = ROOT / "runtime/tool_envs/wxauto4-py312/Scripts/python.exe"
STATUS_PATH = ROOT / "runtime/apps/wechat_ai_customer_service/admin/wxauto_update_status.json"
PACKAGE_NAME = "wxauto4"
DEFAULT_INCLUDE_PRERELEASE = True


def wxauto4_reserve_enabled() -> bool:
    """Return whether the wxauto4 reserve path is explicitly enabled."""
    return env_flag("WECHAT_ENABLE_WXAUTO4", default=False)


def wxauto_auto_update_enabled() -> bool:
    """Return whether wxauto4 package auto-update is allowed when reserve is on."""
    return env_flag("WECHAT_WXAUTO_AUTO_UPDATE", default=True)


def wxauto4_module_update_enabled() -> bool:
    """Only update wxauto4 when the reserve transport is explicitly enabled."""
    return wxauto4_reserve_enabled() and wxauto_auto_update_enabled()


def wxauto4_module_update_disabled_result(
    *,
    package_name: str = PACKAGE_NAME,
    reason: str | None = None,
) -> dict[str, Any]:
    reserve_enabled = wxauto4_reserve_enabled()
    auto_update_enabled = wxauto_auto_update_enabled()
    if reason is None:
        reason = "disabled_by_WECHAT_WXAUTO_AUTO_UPDATE" if reserve_enabled else "wxauto4_reserve_disabled"
    return {
        "ok": True,
        "enabled": False,
        "updated": False,
        "package": package_name,
        "reason": reason,
        "reserve_enabled": reserve_enabled,
        "auto_update_enabled": auto_update_enabled,
        "technical_reserve": True,
        "checked_at": now_iso(),
    }


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class WxautoPackageManager:
    """Check and upgrade wxauto4 in the Python 3.12 sidecar environment.

    The main OmniAuto runtime is Python 3.13, while wxauto4 is isolated in a
    sidecar interpreter. All package checks and upgrades must run against that
    interpreter, otherwise the admin app may upgrade the wrong environment.
    """

    def __init__(
        self,
        *,
        sidecar_python: Path = SIDECAR_PYTHON,
        status_path: Path = STATUS_PATH,
        package_name: str = PACKAGE_NAME,
        timeout_seconds: int = 180,
        include_prerelease: bool | None = None,
    ) -> None:
        self.sidecar_python = sidecar_python
        self.status_path = status_path
        self.package_name = package_name
        self.timeout_seconds = max(30, int(timeout_seconds))
        self.include_prerelease = (
            env_flag("WECHAT_WXAUTO_INCLUDE_PRERELEASE", default=DEFAULT_INCLUDE_PRERELEASE)
            if include_prerelease is None
            else bool(include_prerelease)
        )

    def auto_update_on_customer_service_start(self) -> dict[str, Any]:
        return self.auto_update_on_wechat_module_start()

    def auto_update_on_wechat_module_start(self) -> dict[str, Any]:
        if not wxauto4_module_update_enabled():
            result = wxauto4_module_update_disabled_result(package_name=self.package_name)
            self.write_status(result)
            return result
        result = self.check_and_update()
        self.write_status(result)
        return result

    def check_and_update(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "ok": True,
            "enabled": True,
            "updated": False,
            "package": self.package_name,
            "sidecar_python": str(self.sidecar_python),
            "include_prerelease": self.include_prerelease,
            "checked_at": now_iso(),
        }
        if not self.sidecar_python.exists():
            return {**base, "ok": False, "reason": "sidecar_python_missing"}

        installed = self.installed_version()
        latest = self.latest_version()
        base.update(
            {
                "installed_version": installed.get("version", ""),
                "latest_version": latest.get("version", ""),
                "installed_status": installed,
                "latest_status": latest,
            }
        )
        if not installed.get("ok"):
            return {**base, "ok": False, "reason": "installed_version_unavailable"}
        if not latest.get("ok"):
            return {**base, "ok": False, "reason": "latest_version_unavailable"}

        installed_version = str(installed.get("version") or "")
        latest_version = str(latest.get("version") or "")
        if not latest_version or compare_versions(latest_version, installed_version) <= 0:
            return {**base, "reason": "already_latest"}

        upgrade = self.upgrade()
        after = self.installed_version()
        updated = bool(upgrade.get("ok") and str(after.get("version") or "") == latest_version)
        return {
            **base,
            "ok": bool(upgrade.get("ok")),
            "updated": updated,
            "reason": "updated" if updated else "upgrade_failed",
            "upgrade": upgrade,
            "installed_after": after,
        }

    def installed_version(self) -> dict[str, Any]:
        code = (
            "import importlib.metadata as m, json\n"
            f"pkg={self.package_name!r}\n"
            "try:\n"
            "    print(json.dumps({'ok': True, 'version': m.version(pkg)}))\n"
            "except Exception as exc:\n"
            "    print(json.dumps({'ok': False, 'error': repr(exc)}))\n"
        )
        proc = self.run([str(self.sidecar_python), "-c", code], timeout=30)
        payload = parse_json_object(proc.get("stdout", ""))
        if isinstance(payload, dict):
            return {**payload, "returncode": proc.get("returncode")}
        return {"ok": False, "error": "invalid_installed_version_response", "process": proc}

    def latest_version(self) -> dict[str, Any]:
        proc = self.run(
            [
                str(self.sidecar_python),
                "-m",
                "pip",
                "index",
                "versions",
                *(["--pre"] if self.include_prerelease else []),
                self.package_name,
            ],
            timeout=60,
        )
        text = "\n".join([str(proc.get("stdout") or ""), str(proc.get("stderr") or "")])
        version = parse_latest_version(text, self.package_name)
        if version:
            return {
                "ok": True,
                "version": version,
                "returncode": proc.get("returncode"),
                "source": "pip_index_versions",
                "include_prerelease": self.include_prerelease,
            }
        return {
            "ok": False,
            "error": "latest_version_not_found",
            "returncode": proc.get("returncode"),
            "stdout_tail": str(proc.get("stdout") or "")[-1200:],
            "stderr_tail": str(proc.get("stderr") or "")[-1200:],
        }

    def upgrade(self) -> dict[str, Any]:
        proc = self.run(
            [
                str(self.sidecar_python),
                "-m",
                "pip",
                "install",
                "--upgrade",
                *(["--pre"] if self.include_prerelease else []),
                self.package_name,
            ],
            timeout=self.timeout_seconds,
        )
        return {
            "ok": int(proc.get("returncode") or 0) == 0,
            "returncode": proc.get("returncode"),
            "stdout_tail": str(proc.get("stdout") or "")[-4000:],
            "stderr_tail": str(proc.get("stderr") or "")[-4000:],
        }

    def run(self, cmd: list[str], *, timeout: int) -> dict[str, Any]:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            return {"ok": proc.returncode == 0, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
        except Exception as exc:
            return {"ok": False, "returncode": None, "stdout": "", "stderr": "", "error": repr(exc)}

    def write_status(self, payload: dict[str, Any]) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.status_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.status_path)


def parse_latest_version(text: str, package_name: str = PACKAGE_NAME) -> str:
    clean = str(text or "")
    pattern = re.compile(rf"^{re.escape(package_name)}\s+\(([^)]+)\)", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(clean)
    if match:
        return match.group(1).strip()
    latest_match = re.search(r"^\s*LATEST:\s*([^\s]+)", clean, flags=re.IGNORECASE | re.MULTILINE)
    if latest_match:
        return latest_match.group(1).strip()
    versions_match = re.search(r"Available versions:\s*([^\r\n]+)", clean, flags=re.IGNORECASE)
    if versions_match:
        versions = [item.strip() for item in versions_match.group(1).split(",") if item.strip()]
        return versions[0] if versions else ""
    return ""


def version_key(value: str) -> tuple[int, ...]:
    parts = [int(item) for item in re.findall(r"\d+", str(value or ""))]
    return tuple(parts or [0])


def compare_versions(left: str, right: str) -> int:
    """Return 1/0/-1 for version comparison, including beta/rc releases."""
    if Version is not None:
        try:
            left_version = Version(str(left or "0"))
            right_version = Version(str(right or "0"))
            return (left_version > right_version) - (left_version < right_version)
        except InvalidVersion:
            pass
    left_key = version_key(left)
    right_key = version_key(right)
    return (left_key > right_key) - (left_key < right_key)


def env_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def parse_json_object(text: str) -> dict[str, Any] | None:
    clean = str(text or "").strip()
    if not clean:
        return None
    try:
        payload = json.loads(clean)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        pass
    start = clean.rfind("{")
    while start >= 0:
        try:
            payload = json.loads(clean[start:])
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            start = clean.rfind("{", 0, start)
    return None


def main() -> int:
    result = WxautoPackageManager().auto_update_on_customer_service_start()
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
