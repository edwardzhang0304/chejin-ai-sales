"""Exercise the shipped 0.9.75 GUI and updater against the exact signed ZIP.

Only the isolated Windows runner is used. Both EXEs are untouched; the backend
is the candidate application with synthetic SQLite data and loopback TLS.
Qt's documented debugging interface drives actual DOM clicks, never the bridge
or coordinator directly. No production credentials, customer data or messaging.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
OLD_EXE_SHA = "3336b868af6647eb207880a603f6ada8166f436e7853191151b28174f168e1bb"
OLD_UPDATER_SHA = "7002af931bee724d24ba1023bc3c900bfd06a3e9991fb267547025a5cccd04d8"
WORKER_ID = "formal-upgrade-isolated-worker"
INSTANCE_ID = "formal-upgrade-isolated-instance"
TOKEN = "synthetic-loopback-worker-token"
RUNTIME_CONTROL_KEY = "runtime_control_v1"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(check, label, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.25)
    raise AssertionError("Timed out: " + label)


class QtPage:
    """Use Qt 6.6's page CDP endpoint; it cannot manage browser contexts.

    JavaScript only reads rendered DOM geometry/text. Mouse input enters the
    actual UI; no bridge, coordinator, update plan or application state is set.
    """

    def __init__(self, port):
        from websockets.sync.client import connect
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=10) as response:
            pages = [p for p in json.load(response) if p.get("type") == "page"]
        assert len(pages) == 1, "Expected one actual Worker page"
        self.socket = connect(pages[0]["webSocketDebuggerUrl"], proxy=None, open_timeout=10,
                              close_timeout=1, max_size=16 * 1024 * 1024)
        self.sequence = 0

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.socket.close()

    def call(self, method, **params):
        self.sequence += 1
        self.socket.send(json.dumps({"id": self.sequence, "method": method, "params": params}))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            message = json.loads(self.socket.recv(timeout=max(0.1, deadline - time.monotonic())))
            if message.get("id") != self.sequence:
                continue
            assert "error" not in message, f"CDP {method}: {message.get('error')}"
            return message.get("result", {})
        raise TimeoutError("CDP response: " + method)

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        assert "exceptionDetails" not in result, "DOM inspection failed"
        return result.get("result", {}).get("value")

    def click_button(self, name):
        expression = """(() => {
          const matches = [...document.querySelectorAll('button')].filter(el => {
            const style = getComputedStyle(el), rect = el.getBoundingClientRect();
            return (el.getAttribute('aria-label') || el.innerText).trim() === NAME
              && !el.disabled && style.visibility === 'visible' && style.display !== 'none'
              && Number(style.opacity) > 0 && rect.width > 0 && rect.height > 0;
          });
          if (matches.length !== 1) return null;
          const el = matches[0], rect = el.getBoundingClientRect();
          const x = rect.x + rect.width / 2, y = rect.y + rect.height / 2;
          const hit = document.elementFromPoint(x, y);
          return hit && (hit === el || el.contains(hit)) ? {x, y} : null;
        })()""".replace("NAME", json.dumps(name))
        point = wait_for(lambda: self.evaluate(expression), "visible clickable button " + name, 60)
        self.call("Input.dispatchMouseEvent", type="mouseMoved", **point)
        self.call("Input.dispatchMouseEvent", type="mousePressed", button="left", buttons=1, clickCount=1, **point)
        self.call("Input.dispatchMouseEvent", type="mouseReleased", button="left", buttons=0, clickCount=1, **point)

    def wait_text(self, value):
        expression = """[...document.querySelectorAll('body *')].some(el =>
          el.children.length === 0 && el.textContent.trim() === TEXT && el.getClientRects().length
          && getComputedStyle(el).visibility === 'visible')""".replace("TEXT", json.dumps(value))
        wait_for(lambda: self.evaluate(expression), "visible text " + value, 30)

    def screenshot(self, path):
        result = self.call("Page.captureScreenshot", format="png")
        Path(path).write_bytes(base64.b64decode(result["data"]))


def serve_driver_fixture():
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineWidgets import QWebEngineView
    app = QApplication([])
    view = QWebEngineView()
    view.setHtml('''<button style="visibility:hidden" aria-label="Driver fixture">hidden</button>
      <button aria-label="Driver fixture" onclick="this.textContent=event.isTrusted?'trusted click':'untrusted click'">ready</button>''')
    view.show()
    app.exec()


def driver_smoke(folder):
    """Prove the CI driver before building; this is not an upgrade result."""
    folder.mkdir(parents=True, exist_ok=False)
    port = free_port()
    env = {**os.environ, "QTWEBENGINE_REMOTE_DEBUGGING": f"127.0.0.1:{port}"}
    with (folder / "qt.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve-driver-fixture"],
                                   env=env, cwd=folder, stdout=log, stderr=log)
        try:
            def ready():
                assert process.poll() is None, "Qt driver fixture exited"
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as response:
                        return any(p.get("type") == "page" for p in json.load(response))
                except OSError:
                    return False
            wait_for(ready, "Qt driver fixture", 90)
            with QtPage(port) as page:
                page.click_button("Driver fixture")
                page.wait_text("trusted click")
                page.screenshot(folder / "clicked.png")
            write_json(folder / "result.json", {"qt_driver": "passed", "trusted_mouse_input": True,
                                               "windows_upgrade_evidence": False})
            print("Qt page driver: trusted click and screenshot passed")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def tls_files(folder):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "isolated-upgrade-loopback")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = folder / "loopback.crt", folder / "loopback.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert_path, key_path


def serve(spec):
    # Imports occur only after the child receives its isolated environment.
    sys.path.insert(0, str(ROOT / "backend"))
    from app.main import app
    from app.core.database import Base, engine, SessionLocal
    from app.models.worker import Worker
    from app.models.base import utcnow
    from app.services.worker_token_service import hash_worker_token, encrypt_worker_token
    from app.services.client_release_service import register_signed_client_release, store_client_release_artifact
    import uvicorn
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(Worker(id=WORKER_ID, worker_name="Isolated upgrade gate", platform="windows",
                      enabled=True, run_status=spec["run_status"], running_status="idle",
                      worker_token_hash=hash_worker_token(TOKEN), worker_token_encrypted=encrypt_worker_token(TOKEN),
                      client_binding_state="bound", client_instance_id=INSTANCE_ID, bound_at=utcnow()))
        release = register_signed_client_release(db, read_json(spec["release"]), public_keys_path=Path(spec["public_keys"]))
        store_client_release_artifact(release, Path(spec["archive"]))
        db.commit()

    @app.middleware("http")
    async def record_gate_requests(request, call_next):
        response = await call_next(request)
        # Never record a lease URL, authorization header or business body.
        if request.url.path.endswith("/client-releases/latest") or "/client-releases/artifacts/" in request.url.path:
            with Path(spec["requests"]).open("a", encoding="utf-8") as out:
                out.write(json.dumps({"kind": "latest" if request.url.path.endswith("/latest") else "download",
                                      "current_version": request.query_params.get("current_version"),
                                      "status": response.status_code}) + "\n")
        return response
    uvicorn.run(app, host="127.0.0.1", port=spec["port"], ssl_certfile=spec["cert"], ssl_keyfile=spec["key"], access_log=False, log_level="warning")


def seed_old_data(source, data, status, env):
    code = """
from chejin_worker_client.models import Binding
from chejin_worker_client.storage import save_binding,save_accept_schedule,request_runtime_pause,connect
from datetime import datetime,timezone
import sys
save_binding(Binding(sys.argv[1],sys.argv[2],sys.argv[3],run_status=sys.argv[4]))
save_accept_schedule(enabled=True,start='09:10',end='18:20')
request_runtime_pause()
now=datetime.now(timezone.utc).isoformat()
with connect() as db:
 db.execute("INSERT INTO c2_runtime_state(key,value,updated_at) VALUES(?,?,?)",('upgrade_gate_history','{\"completed\":true}',now))
 db.execute("INSERT INTO c2_message_ledger(conversation_id,source_message_key,origin_read_run_id,dedupe_key,message_type,terminal_state,ingest_state,result_json,first_seen_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",('isolated-history','isolated-message','isolated-read','isolated-dedupe','text','completed','confirmed','{}',now,now))
"""
    subprocess.run([sys.executable, "-c", code, WORKER_ID, TOKEN, INSTANCE_ID, status],
                   env={**env, "PYTHONPATH": str(source)}, cwd=source, check=True)
    (data / "incidents").mkdir(exist_ok=True)
    (data / "incidents" / "existing-evidence.json").write_text('{"synthetic":true}', encoding="utf-8")


def preserved_values(data):
    with sqlite3.connect(data / "worker_client.sqlite3") as db:
        db.execute("BEGIN")
        return {
            "binding": db.execute("select worker_id,worker_token,client_instance_id,run_status,bound_at from binding").fetchall(),
            "settings": db.execute("select * from client_settings order by key").fetchall(),
            "history": db.execute("select * from c2_runtime_state where key='upgrade_gate_history'").fetchall(),
            "ledger": db.execute("select * from c2_message_ledger where conversation_id='isolated-history'").fetchall(),
            "evidence": digest(data / "incidents" / "existing-evidence.json"),
        }


def assert_preserved(before, after):
    """Check business rows exactly, and the completed runtime gate separately.

    The immutable updater baseline still protects ALL settings during handoff.
    After reconciliation, opening the temporary intake gate legitimately changes
    runtime_control_v1.updated_at; its value must return to the seeded pause state.
    """
    def split(snapshot):
        protected = {**snapshot, "settings": [row for row in snapshot["settings"]
                                              if row[0] != RUNTIME_CONTROL_KEY]}
        runtime = [row for row in snapshot["settings"] if row[0] == RUNTIME_CONTROL_KEY]
        assert len(runtime) == 1, "Expected one persisted runtime control row"
        return protected, runtime[0]

    before_business, before_control = split(before)
    after_business, after_control = split(after)
    changed = [key for key in before_business if before_business[key] != after_business[key]]
    assert not changed, "Existing business data changed: " + ", ".join(changed)
    expected = json.loads(before_control[1])
    assert expected == {
        "pause_requested": True, "pause_requested_at": expected.get("pause_requested_at"),
        "inflight_flow_id": None, "inflight_flow_kind": None, "inflight_started_at": None,
        "update_no_new_work": False, "update_request_id": None,
    } and expected["pause_requested_at"], "Initial pause state is not idle"
    assert json.loads(after_control[1]) == expected, "Pause/flow/update gate was not preserved"
    assert datetime.fromisoformat(after_control[2]) >= datetime.fromisoformat(before_control[2]), "Runtime timestamp regressed"


def run_case(args, status):
    import psutil
    case = args.work_root / status
    case.mkdir(parents=True, exist_ok=False)
    data = case / "data"
    data.mkdir()
    current = case / "install" / "CheJinWorkerClient"
    shutil.copytree(args.old_package_root, current)
    assert digest(current / "CheJinWorkerClient.exe") == OLD_EXE_SHA
    assert digest(current / "CheJinUpdater.exe") == OLD_UPDATER_SHA
    cert, key = tls_files(case)
    port, debug_port = free_port(), free_port()
    base = f"https://127.0.0.1:{port}"
    spec = {"port": port, "cert": str(cert), "key": str(key), "archive": str(args.archive),
            "release": str(args.release), "run_status": status, "requests": str(case / "requests.jsonl"),
            "public_keys": str(args.old_package_root / "_internal" / "release-signing-public-keys.json")}
    # Use the old package's trust. Never substitute target public keys.
    if not Path(spec["public_keys"]).is_file():
        spec["public_keys"] = str(args.old_package_root / "release-signing-public-keys.json")
    assert Path(spec["public_keys"]).is_file(), "Shipped trust file not found"
    write_json(case / "spec.json", spec)
    env = {k: v for k, v in os.environ.items() if not any(word in k.upper() for word in ("TOKEN", "SECRET", "API_KEY", "SIGNING_PRIVATE"))}
    env.update({"CHEJIN_WORKER_HOME": str(data), "CHEJIN_UPDATE_STAGING_ROOT": str(case / "updates"),
                "CHEJIN_API_BASE_URL": base + "/api", "CHEJIN_RPA_MODE": "mock",
                "CHEJIN_HEARTBEAT_INTERVAL": "1", "CHEJIN_API_TIMEOUT": "5",
                "SSL_CERT_FILE": str(cert), "REQUESTS_CA_BUNDLE": str(cert),
                "QTWEBENGINE_REMOTE_DEBUGGING": f"127.0.0.1:{debug_port}",
                "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                "ENVIRONMENT": "test", "DATABASE_URL": "sqlite:///" + (case / "backend.sqlite3").as_posix(),
                "AUTO_CREATE_TABLES": "true", "CLIENT_RELEASE_ARTIFACT_ROOT": str(case / "backend-artifacts"),
                "CLIENT_RELEASE_PUBLIC_BASE_URL": base + "/api", "C3_AI_ADAPTER_MODE": "mock",
                "C3_BATCH_RECOVERY_POLL_SECONDS": "0", "FEISHU_APP_ID": "", "FEISHU_APP_SECRET": ""})
    env.pop("CHEJIN_WORKER_UI_MODE", None)
    env.pop("CHEJIN_RELEASE_SIGNING_PUBLIC_KEY_BASE64", None)
    seed_old_data(args.old_source_root, data, status, env)
    before = preserved_values(data)
    checkpoints = {"synthetic_test_data_only": True, "seeded": before}
    processes = []
    report = {"current_version": "0.9.75", "target_version": "0.9.76", "initial_run_status": status,
              "old_exe_sha256": OLD_EXE_SHA, "old_updater_sha256": OLD_UPDATER_SHA,
              "target_zip_sha256": digest(args.archive), "status": "failed"}
    log = (case / "process.log").open("w", encoding="utf-8")
    try:
        backend = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve", str(case / "spec.json")], env=env, cwd=case, stdout=log, stderr=log)
        processes.append(backend)
        import ssl
        context = ssl.create_default_context(cafile=str(cert))

        def backend_ready():
            if backend.poll() is not None:
                raise AssertionError("Isolated backend exited")
            try:
                with urllib.request.urlopen(base + "/healthz", context=context, timeout=2) as response:
                    return response.status == 200
            except OSError:
                return False
        wait_for(backend_ready, "isolated backend")
        old = subprocess.Popen([str(current / "CheJinWorkerClient.exe")], env=env, cwd=current, stdout=log, stderr=log)
        processes.append(old)

        def debug_ready():
            if old.poll() is not None:
                raise AssertionError("Original Worker exited before update")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json", timeout=2) as response:
                    return any(p.get("type") == "page" for p in json.load(response))
            except OSError:
                return False
        wait_for(debug_ready, "original Worker UI")
        with QtPage(debug_port) as page:
            page.click_button("打开设置")
            page.wait_text("V0.9.75")
            page.screenshot(case / "before.png")
            checkpoints["before_button"] = preserved_values(data)
            assert_preserved(before, checkpoints["before_button"])
            if args.manual_install:
                # A normal WM_CLOSE invokes the original Qt close/shutdown path.
                # Only this isolated test process is targeted, never terminated for installation.
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32
                user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                user32.PostMessageW.restype = wintypes.BOOL
                user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
                user32.IsWindowVisible.argtypes = [wintypes.HWND]
                windows = []
                callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                def capture(hwnd, _):
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value == old.pid and user32.IsWindowVisible(hwnd):
                        windows.append(hwnd)
                    return True
                callback = callback_type(capture)
                user32.EnumWindows(callback, 0)
                assert windows, "Original client visible window not found"
                for hwnd in windows:
                    assert user32.PostMessageW(hwnd, 0x0010, 0, 0), "Normal close request failed"
                assert old.wait(timeout=90) == 0, "Original client did not close normally"
                checkpoints["after_normal_exit"] = preserved_values(data)
                assert_preserved(before, checkpoints["after_normal_exit"])
                sys.path.insert(0, str(ROOT / "worker-client"))
                from chejin_worker_client.models import ClientRelease
                from chejin_worker_client.release_package_contract import verify_release_signature, load_trusted_release_keys, verify_staged_package
                descriptor = read_json(args.release)
                release = ClientRelease.from_api({**descriptor, "latest_version": descriptor["version"], "update_available": True})
                verify_release_signature(release, trusted_keys=load_trusted_release_keys(Path(spec["public_keys"])))
                manifest = verify_staged_package(release, args.target_package_root)
                # Install to a new program directory, retaining the same explicit old data directory.
                installed = case / "new-install" / "CheJinWorkerClient"
                shutil.copytree(args.target_package_root, installed)
                verify_staged_package(release, installed)
                old = subprocess.Popen([str(installed / "CheJinWorkerClient.exe")], env=env, cwd=installed, stdout=log, stderr=log)
                processes.append(old)
                wait_for(debug_ready, "new manually installed Worker UI")
                with QtPage(debug_port) as new_page:
                    new_page.click_button("打开设置")
                    new_page.wait_text("V0.9.76")
                    new_page.screenshot(case / "after.png")
                checkpoints["after_manual_install"] = preserved_values(data)
                assert_preserved(before, checkpoints["after_manual_install"])
                assert old.poll() is None
                report.update(status="passed", mode="preserve_data_manual_install", original_worker_exited=True,
                              normal_close_used=True, protected_data_preserved=True, target_ui_confirmed=True,
                              target_commit=manifest["git_commit"], target_program_manifest_verified=True,
                              paused_intent_and_idle_gate_preserved=True, real_settings_button_clicked=False,
                              original_updater_used=False, original_data_directory_reused=True)
                return report
            page.click_button("检查更新")
            report["real_settings_button_clicked"] = True
            # The original coordinator owns plan creation and normal shutdown.
            # Do not close the browser, inject a plan, or terminate the old EXE.
            state_path = case / "updates" / "update-state.json"

            def finished():
                if not state_path.exists():
                    return False
                state = read_json(state_path)
                if state.get("state") in {"failed", "rolled_back", "rollback_failed"}:
                    report["failure_update_state"] = {
                        key: state.get(key) for key in (
                            "state", "result_code", "result_message", "message", "last_error",
                            "install_started", "result_reconciled", "plan_path",
                            "updater_pid", "updater_executable_path", "target_version",
                        ) if key in state
                    }
                    raise AssertionError("Original GUI update failed: " + str(state.get("result_code")) + ": " + str(state.get("result_message") or state.get("message") or state.get("last_error") or ""))
                return state if state.get("state") == "succeeded" and state.get("result_reconciled") is True and not state.get("status_restore_pending") else False
            state = wait_for(finished, "original GUI full upgrade", timeout=300)
            assert old.poll() is not None, "Original Worker did not exit"
            plan_path = Path(state["plan_path"])
            plan = read_json(plan_path)
            assert plan["schema_version"] == 2 and plan["current_version"] == "0.9.75"
            assert plan["old_pid"] == old.pid
            assert digest(plan_path.parent / "CheJinUpdater.exe") == OLD_UPDATER_SHA
            assert plan["safe_boundary"]["safe"] is True
            marker = read_json(plan["healthy_marker_path"])
            assert marker["healthy"] is True and marker["version"] == "0.9.76"
            assert marker["runtime_health"]["binding_state"] == "bound"
            for name in ("task_runner", "c2_listener", "thread_monitor"):
                health = marker["runtime_health"]["threads"][name]
                assert health["entered_loop"] and health["alive"]
            assert Path(plan["data_baseline_path"]).is_file()
            baseline = read_json(plan["data_baseline_path"])
            assert baseline["payload"]["captured_after_old_exit"] is True
            checkpoints["after_reconciliation"] = preserved_values(data)
            assert_preserved(before, checkpoints["after_reconciliation"])
            target_manifest = read_json(current / "update-package-manifest.json")
            assert target_manifest["git_commit"] == read_json(args.release)["git_commit"]
            assert target_manifest["version"] == "0.9.76"
            with QtPage(debug_port) as new_page:
                new_page.click_button("打开设置")
                new_page.wait_text("V0.9.76")
                new_page.screenshot(case / "after.png")
            requests = [json.loads(line) for line in Path(spec["requests"]).read_text().splitlines()]
            assert any(r["kind"] == "latest" and r["current_version"] == "0.9.75" and r["status"] == 200 for r in requests)
            assert any(r["kind"] == "download" and r["status"] == 200 for r in requests)
            report.update(status="passed", original_worker_exited=True, original_updater_used=True,
                          protected_data_preserved=True, target_ui_confirmed=True, actual_backend_download=True,
                          target_commit=target_manifest["git_commit"], runtime_threads_alive=True,
                          immutable_handoff_baseline=True, paused_intent_and_idle_gate_preserved=True)
    except Exception as exc:
        report["failure"] = str(exc)[:500]
        # Only this isolated run's synthetic state is inspected. Retain bounded
        # diagnostics before the runner is destroyed; do not weaken acceptance.
        diagnostic_files = []
        for pattern in ("**/update-result.json", "**/updater-ready.json", "**/worker-startup.jsonl"):
            for path in case.glob(pattern):
                if path.is_file() and path.stat().st_size < 65536:
                    diagnostic_files.append({"path": str(path.relative_to(case)), "text": path.read_text(encoding="utf-8-sig", errors="replace")[-12000:]})
        report["failure_control_diagnostics"] = diagnostic_files
        raise
    finally:
        write_json(case / "data-checkpoints.json", checkpoints)
        write_json(case / "result.json", report)
        # Cleanup applies only to isolated test processes, after a result exists.
        for process in psutil.process_iter(["pid", "exe"]):
            try:
                if process.info["exe"] and Path(process.info["exe"]).resolve().is_relative_to(case):
                    process.terminate()
            except (psutil.Error, OSError):
                pass
        for process in processes:
            if process.poll() is None:
                process.terminate()
        log.close()
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", type=Path)
    parser.add_argument("--driver-smoke", type=Path)
    parser.add_argument("--serve-driver-fixture", action="store_true")
    parser.add_argument("--old-package-root", type=Path)
    parser.add_argument("--old-source-root", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--manual-install", action="store_true")
    parser.add_argument("--target-package-root", type=Path)
    args = parser.parse_args()
    if args.serve_driver_fixture:
        serve_driver_fixture()
        return
    if args.driver_smoke:
        driver_smoke(args.driver_smoke.resolve())
        return
    if args.serve:
        serve(read_json(args.serve))
        return
    if os.name != "nt":
        raise SystemExit("Requires original Windows EXEs on a Windows runner")
    for name in ("old_package_root", "old_source_root", "archive", "release", "work_root"):
        setattr(args, name, getattr(args, name).resolve())
    if args.manual_install:
        assert args.target_package_root is not None
        args.target_package_root = args.target_package_root.resolve()
    results = [run_case(args, status) for status in ("paused", "faulted")]
    write_json(args.work_root / "upgrade-result.json", {"status": "passed", "cases": results})
    print("Preserve-data manual install passed" if args.manual_install else "Original 0.9.75 GUI -> signed 0.9.76: paused and faulted cases passed")


if __name__ == "__main__":
    main()
