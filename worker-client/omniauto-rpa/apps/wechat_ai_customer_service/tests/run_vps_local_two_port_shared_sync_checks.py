from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
TEST_ROOT = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts" / "vps_local_two_port_shared_sync"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.knowledge_paths import runtime_knowledge_roots, shared_runtime_cache_root, shared_runtime_snapshot_path  # noqa: E402
from apps.wechat_ai_customer_service.sync.vps_sync import local_node_cache_path  # noqa: E402


WINDOWS_FILE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.25)


def main() -> int:
    cleanup_test_root()
    cache_backup = TEST_ROOT / "previous_shared_cache"
    node_cache_text = backup_runtime_cache(cache_backup)
    vps_process: subprocess.Popen[str] | None = None
    local_process: subprocess.Popen[str] | None = None
    vps_log = (TEST_ROOT / "vps.log").open("w", encoding="utf-8")
    local_log = (TEST_ROOT / "local.log").open("w", encoding="utf-8")
    try:
        seed_vps_state(TEST_ROOT / "vps_state.json")
        vps_port = free_port()
        local_port = free_port()
        env = server_env(vps_port=vps_port)
        vps_process = start_server(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "apps.wechat_ai_customer_service.vps_admin.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(vps_port),
                "--log-level",
                "warning",
            ],
            env=env,
            log=vps_log,
        )
        wait_for_json(f"http://127.0.0.1:{vps_port}/v1/health", vps_process, TEST_ROOT / "vps.log")
        local_process = start_server(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "apps.wechat_ai_customer_service.admin_backend.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(local_port),
                "--log-level",
                "warning",
            ],
            env=env,
            log=local_log,
        )
        wait_for_json(f"http://127.0.0.1:{local_port}/api/health", local_process, TEST_ROOT / "local.log")

        local_base = f"http://127.0.0.1:{local_port}"
        status = request_json("GET", f"{local_base}/api/sync/status")
        assert_true(status.get("vps_configured") is True, "local client should be configured for the VPS port")
        assert_equal(status.get("vps_base_url"), f"http://127.0.0.1:{vps_port}", "local client should use the configured VPS base URL")

        registration = request_json("POST", f"{local_base}/api/sync/register-node", {"display_name": "two-port-local-client"})
        assert_true(
            registration.get("ok") is True and registration.get("node"),
            f"local node should register through the VPS port: {registration}",
        )

        first_sync = request_json("POST", f"{local_base}/api/sync/shared/cloud-snapshot", {"force": True})
        assert_true(first_sync.get("ok") is True, "cloud shared snapshot sync should succeed")
        assert_true(first_sync.get("cache_valid") is True, "synced cloud cache should carry a valid lease")
        assert_true(str(first_sync.get("snapshot_version") or "").startswith("shared_"), "snapshot version should be cloud-derived")
        assert_true(bool(first_sync.get("expires_at")), "sync response should expose lease expiry")
        assert_true((shared_runtime_cache_root() / "global_guidelines" / "items" / "cloud_two_port_guideline.json").exists(), "cloud item should be materialized in the runtime cache")
        assert_true((shared_runtime_cache_root() / "reply_style" / "items" / "cloud_home_appliance_install_style.json").exists(), "home appliance item should be included for default tenant industry")
        assert_true(not (shared_runtime_cache_root() / "risk_control" / "items" / "cloud_usedcar_transfer_boundary.json").exists(), "used-car-only item should not leak into default tenant snapshot")

        persisted = json.loads(shared_runtime_snapshot_path().read_text(encoding="utf-8"))
        assert_equal(persisted.get("source"), "cloud_official_shared_library", "persisted cache should declare cloud source")
        assert_true(persisted.get("cache_policy", {}).get("requires_cloud_refresh") is True, "persisted cache should require cloud refresh")
        assert_equal(persisted.get("tenant_industry_id"), "home_appliance", "default tenant should resolve to home appliance industry")
        assert_true(isinstance(persisted.get("policy_bundle"), dict), "snapshot should include policy bundle")
        assert_true(shared_runtime_cache_root() in runtime_knowledge_roots("default"), "valid cloud cache should participate in runtime knowledge roots")

        second_sync = request_json("POST", f"{local_base}/api/sync/shared/cloud-snapshot", {"force": False})
        assert_true(second_sync.get("ok") is True, "second cloud shared sync should succeed")
        assert_true(second_sync.get("not_modified") is True, "unchanged cloud snapshot should return a lease renewal")
        assert_true(second_sync.get("cache_valid") is True, "renewed cloud lease should remain valid")

        refreshed_status = request_json("GET", f"{local_base}/api/sync/status")
        cache_status = refreshed_status.get("shared_cloud_cache") if isinstance(refreshed_status.get("shared_cloud_cache"), dict) else {}
        assert_true(cache_status.get("valid") is True, "local status should expose a valid cloud shared cache")
        assert_true(bool(cache_status.get("expires_at")), "local status should expose cloud cache expiry")
        assert_equal(cache_status.get("tenant_industry_id"), "home_appliance", "status should expose tenant industry binding")

        chejin_registration = request_json(
            "POST",
            f"{local_base}/api/sync/register-node",
            {"display_name": "two-port-local-client"},
            headers={"X-Tenant-ID": "chejin"},
        )
        assert_true(chejin_registration.get("ok") is True and chejin_registration.get("node"), "same local node should register for chejin")
        node_cache = json.loads(local_node_cache_path().read_text(encoding="utf-8"))
        assert_true({"default", "chejin"}.issubset(set(node_cache.get("tenant_ids", []))), "local node cache should merge tenant scopes")

        usedcar_sync = request_json(
            "POST",
            f"{local_base}/api/sync/shared/cloud-snapshot",
            {"force": True},
            headers={"X-Tenant-ID": "chejin"},
        )
        assert_true(usedcar_sync.get("ok") is True, "used-car tenant cloud snapshot sync should succeed")
        assert_true((shared_runtime_cache_root() / "risk_control" / "items" / "cloud_usedcar_transfer_boundary.json").exists(), "used-car item should be included for used-car tenant snapshot")
        assert_true(not (shared_runtime_cache_root() / "reply_style" / "items" / "cloud_home_appliance_install_style.json").exists(), "home appliance item should not leak into used-car tenant snapshot")
        assert_true(shared_runtime_cache_root() in runtime_knowledge_roots("chejin"), "shared root should participate for matched tenant")
        chejin_status = request_json("GET", f"{local_base}/api/sync/status", headers={"X-Tenant-ID": "chejin"})
        assert_true(chejin_status.get("cloud_gate", {}).get("ok") is True, f"cloud gate should accept merged chejin node: {chejin_status.get('cloud_gate')}")
        assert_true(shared_runtime_cache_root() not in runtime_knowledge_roots("default"), "shared root should not participate when snapshot tenant mismatches active tenant")

        result = {
            "ok": True,
            "vps_port": vps_port,
            "local_port": local_port,
            "snapshot_version": first_sync.get("snapshot_version"),
            "cache_expires_at": cache_status.get("expires_at"),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": repr(exc)}, ensure_ascii=False, indent=2))
        return 1
    finally:
        stop_process(local_process)
        stop_process(vps_process)
        vps_log.close()
        local_log.close()
        restore_runtime_cache(cache_backup, node_cache_text)


def server_env(*, vps_port: int) -> dict[str, str]:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(PROJECT_ROOT) if not existing_pythonpath else str(PROJECT_ROOT) + os.pathsep + existing_pythonpath
    env["WECHAT_VPS_ADMIN_STATE_PATH"] = str(TEST_ROOT / "vps_state.json")
    env["WECHAT_VPS_BASE_URL"] = f"http://127.0.0.1:{vps_port}"
    env["WECHAT_VPS_AUTO_DISCOVER"] = "0"
    env["WECHAT_AUTH_REQUIRED"] = "0"
    env["WECHAT_EMAIL_OTP_REQUIRED"] = "0"
    env["WECHAT_SHARED_SNAPSHOT_TTL_SECONDS"] = "300"
    env["WECHAT_SHARED_SNAPSHOT_REFRESH_AFTER_SECONDS"] = "60"
    env["WECHAT_LOCAL_NODE_ID"] = "two_port_node_01"
    env["WECHAT_VPS_TIMEOUT_SECONDS"] = "4"
    return env


def seed_vps_state(path: Path) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = {
        "schema_version": 1,
        "tenants": {
            "default": {
                "tenant_id": "default",
                "display_name": "Default Tenant",
                "industry_id": "home_appliance",
                "status": "active",
                "created_at": now,
                "updated_at": now,
            },
            "chejin": {
                "tenant_id": "chejin",
                "display_name": "Used Car Tenant",
                "industry_id": "used_car",
                "status": "active",
                "created_at": now,
                "updated_at": now,
            },
        },
        "shared_library": {
            "cloud_two_port_guideline": {
                "item_id": "cloud_two_port_guideline",
                "industry_id": "global",
                "category_id": "global_guidelines",
                "title": "Two Port Cloud Guideline",
                "content": "The local client must refresh official shared knowledge from the cloud lease before using shared public context.",
                "keywords": ["cloud", "lease", "shared"],
                "applies_to": "all customer-service tenants",
                "notes": "two port integration test fixture",
                "status": "active",
                "source": "two_port_test",
                "tenant_id": "default",
                "data": {
                    "schema_version": 1,
                    "id": "cloud_two_port_guideline",
                    "category_id": "global_guidelines",
                    "title": "Two Port Cloud Guideline",
                    "guideline_text": "The local client must refresh official shared knowledge from the cloud lease before using shared public context.",
                    "keywords": ["cloud", "lease", "shared"],
                    "applies_to": "all customer-service tenants",
                },
                "created_by": "two-port-test",
                "created_at": now,
                "updated_by": "two-port-test",
                "updated_at": now,
            },
            "cloud_home_appliance_install_style": {
                "item_id": "cloud_home_appliance_install_style",
                "industry_id": "home_appliance",
                "category_id": "reply_style",
                "title": "Home Appliance Installation Reply Style",
                "content": "Home appliance installation must be confirmed with site conditions and service windows.",
                "keywords": ["install", "home appliance"],
                "applies_to": "home appliance consultations",
                "notes": "industry fixture for two-port sync checks",
                "status": "active",
                "source": "two_port_test",
                "tenant_id": "default",
                "data": {
                    "schema_version": 1,
                    "id": "cloud_home_appliance_install_style",
                    "category_id": "reply_style",
                    "industry_id": "home_appliance",
                    "title": "Home Appliance Installation Reply Style",
                    "guideline_text": "Home appliance installation must be confirmed with site conditions and service windows.",
                    "keywords": ["install", "home appliance"],
                    "applies_to": "home appliance consultations",
                },
                "created_by": "two-port-test",
                "created_at": now,
                "updated_by": "two-port-test",
                "updated_at": now,
            },
            "cloud_usedcar_transfer_boundary": {
                "item_id": "cloud_usedcar_transfer_boundary",
                "industry_id": "used_car",
                "category_id": "risk_control",
                "title": "Used Car Transfer Boundary",
                "content": "Used-car transfer and registration promises require manual confirmation.",
                "keywords": ["used car", "transfer", "registration"],
                "applies_to": "used-car transfer consultation",
                "notes": "industry fixture for two-port sync checks",
                "status": "active",
                "source": "two_port_test",
                "tenant_id": "default",
                "data": {
                    "schema_version": 1,
                    "id": "cloud_usedcar_transfer_boundary",
                    "category_id": "risk_control",
                    "industry_id": "used_car",
                    "title": "Used Car Transfer Boundary",
                    "guideline_text": "Used-car transfer and registration promises require manual confirmation.",
                    "keywords": ["used car", "transfer", "registration"],
                    "applies_to": "used-car transfer consultation",
                    "allow_auto_reply": False,
                    "requires_handoff": True,
                },
                "created_by": "two-port-test",
                "created_at": now,
                "updated_by": "two-port-test",
                "updated_at": now,
            }
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def backup_runtime_cache(cache_backup: Path) -> str | None:
    cache_root = shared_runtime_cache_root()
    if cache_backup.exists():
        remove_tree(cache_backup)
    if cache_root.exists():
        shutil.copytree(cache_root, cache_backup)
        remove_tree(cache_root)
    node_path = local_node_cache_path()
    if node_path.exists():
        return node_path.read_text(encoding="utf-8")
    return None


def restore_runtime_cache(cache_backup: Path, node_cache_text: str | None) -> None:
    cache_root = shared_runtime_cache_root()
    if cache_root.exists():
        remove_tree(cache_root)
    if cache_backup.exists():
        shutil.copytree(cache_backup, cache_root)
        remove_tree(cache_backup)
    node_path = local_node_cache_path()
    if node_cache_text is None:
        if node_path.exists():
            remove_file(node_path)
    else:
        node_path.parent.mkdir(parents=True, exist_ok=True)
        write_text(node_path, node_cache_text)


def start_server(command: list[str], *, env: dict[str, str], log: Any) -> subprocess.Popen[str]:
    creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    return subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creationflags,
    )


def stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=8)


def wait_for_json(url: str, process: subprocess.Popen[str], log_path: Path) -> dict[str, Any]:
    deadline = time.time() + 25
    last_error = ""
    while time.time() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"server exited early with code {process.returncode}; log={safe_log_tail(log_path)}")
        try:
            return request_json("GET", url)
        except Exception as exc:
            last_error = repr(exc)
            time.sleep(0.25)
    raise AssertionError(f"server did not become ready at {url}; last_error={last_error}; log={safe_log_tail(log_path)}")


def request_json(method: str, url: str, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(f"{method} {url} failed {exc.code}: {detail}") from exc
    return json.loads(text or "{}")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def safe_log_tail(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-2000:]


def cleanup_test_root() -> None:
    resolved = TEST_ROOT.resolve()
    expected_parent = (PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts").resolve()
    if expected_parent not in resolved.parents and resolved != expected_parent:
        raise RuntimeError(f"unsafe test cleanup path: {resolved}")
    if resolved.exists():
        remove_tree(resolved)
    resolved.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temp.write_text(content, encoding="utf-8")
    try:
        replace_path(temp, path)
    finally:
        if temp.exists():
            remove_file(temp)


def is_transient_windows_file_error(exc: OSError) -> bool:
    return (
        os.name == "nt"
        and (
            int(getattr(exc, "errno", 0) or 0) in {13, 22}
            or int(getattr(exc, "winerror", 0) or 0) in {5, 32, 33}
        )
    )


def replace_path(source: Path, destination: Path) -> None:
    last_error: OSError | None = None
    for index, delay in enumerate(WINDOWS_FILE_RETRY_DELAYS):
        try:
            source.replace(destination)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if not is_transient_windows_file_error(exc):
                raise
            if index < len(WINDOWS_FILE_RETRY_DELAYS) - 1:
                time.sleep(delay)
    if last_error is not None:
        raise last_error


def remove_tree(path: Path) -> None:
    last_error: OSError | None = None
    for index, delay in enumerate(WINDOWS_FILE_RETRY_DELAYS):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if not is_transient_windows_file_error(exc):
                raise
            if index < len(WINDOWS_FILE_RETRY_DELAYS) - 1:
                time.sleep(delay)
    if last_error is not None:
        raise last_error


def remove_file(path: Path) -> None:
    last_error: OSError | None = None
    for index, delay in enumerate(WINDOWS_FILE_RETRY_DELAYS):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if not is_transient_windows_file_error(exc):
                raise
            if index < len(WINDOWS_FILE_RETRY_DELAYS) - 1:
                time.sleep(delay)
    if last_error is not None:
        raise last_error


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    raise SystemExit(main())
