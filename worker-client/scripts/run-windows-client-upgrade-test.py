"""Exercise the shipped 0.9.69 GUI and updater against the exact signed ZIP.

Only the isolated Windows runner is used. Both EXEs are untouched; the backend
is the candidate application with synthetic SQLite data and loopback TLS.
Qt's documented debugging interface drives actual DOM clicks, never the bridge
or coordinator directly. No production credentials, customer data or messaging.
"""
from __future__ import annotations

import argparse
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
OLD_EXE_SHA = "c8f4801eba75e7d608514d277da6769f7265d82ed4ae26fc8c00a86170132907"
OLD_UPDATER_SHA = "ced722e9b9d1403454e8bd71e7abfb25739fceb7f1ca5c13a778c919fe3ff1e5"
WORKER_ID = "formal-upgrade-isolated-worker"
INSTANCE_ID = "formal-upgrade-isolated-instance"
TOKEN = "synthetic-loopback-worker-token"


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
from chejin_worker_client.storage import save_binding,save_accept_schedule,connect
from datetime import datetime,timezone
import sys
save_binding(Binding(sys.argv[1],sys.argv[2],sys.argv[3],run_status=sys.argv[4]))
save_accept_schedule(enabled=True,start='09:10',end='18:20')
now=datetime.now(timezone.utc).isoformat()
with connect() as db:
 db.execute("INSERT INTO c2_runtime_state(key,value,updated_at) VALUES(?,?,?)",('upgrade_gate_history','{\"completed\":true}',now))
 db.execute("INSERT INTO c2_message_ledger(conversation_id,source_message_key,origin_read_run_id,dedupe_key,message_type,terminal_state,ingest_state,result_json,first_seen_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",('isolated-history','isolated-message','isolated-read','isolated-dedupe','text','completed','confirmed','{}',now,now))
"""
    subprocess.run([sys.executable, "-c", code, WORKER_ID, TOKEN, INSTANCE_ID, status], env={**env, "PYTHONPATH": str(source)}, check=True)
    (data / "incidents").mkdir(exist_ok=True)
    (data / "incidents" / "existing-evidence.json").write_text('{"synthetic":true}', encoding="utf-8")


def preserved_values(data):
    with sqlite3.connect(data / "worker_client.sqlite3") as db:
        return {
            "binding": db.execute("select worker_id,worker_token,client_instance_id,run_status,bound_at from binding").fetchall(),
            "settings": db.execute("select * from client_settings order by key").fetchall(),
            "history": db.execute("select * from c2_runtime_state where key='upgrade_gate_history'").fetchall(),
            "ledger": db.execute("select * from c2_message_ledger where conversation_id='isolated-history'").fetchall(),
            "evidence": digest(data / "incidents" / "existing-evidence.json"),
        }


def run_case(args, status):
    from playwright.sync_api import sync_playwright
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
    processes = []
    report = {"current_version": "0.9.69", "target_version": "0.9.70", "initial_run_status": status,
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
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}", timeout=30000)
            page = browser.contexts[0].pages[0]
            page.get_by_role("button", name="打开设置", exact=True).click(timeout=60000)
            page.get_by_text("V0.9.69", exact=True).wait_for(timeout=30000)
            page.screenshot(path=str(case / "before.png"))
            page.get_by_role("button", name="检查更新", exact=True).click(timeout=30000)
            report["real_settings_button_clicked"] = True
            # The original coordinator owns plan creation and normal shutdown.
            # Do not close the browser, inject a plan, or terminate the old EXE.
            state_path = case / "updates" / "update-state.json"

            def finished():
                if not state_path.exists():
                    return False
                state = read_json(state_path)
                if state.get("state") in {"failed", "rolled_back", "rollback_failed"}:
                    raise AssertionError("Original GUI update failed: " + str(state.get("result_code")))
                return state if state.get("state") == "succeeded" and not state.get("status_restore_pending") else False
            state = wait_for(finished, "original GUI full upgrade", timeout=300)
            assert old.poll() is not None, "Original Worker did not exit"
            plan_path = Path(state["plan_path"])
            plan = read_json(plan_path)
            assert plan["schema_version"] == 2 and plan["current_version"] == "0.9.69"
            assert plan["old_pid"] == old.pid
            assert digest(plan_path.parent / "CheJinUpdater.exe") == OLD_UPDATER_SHA
            assert plan["safe_boundary"]["safe"] is True
            marker = read_json(plan["healthy_marker_path"])
            assert marker["healthy"] is True and marker["version"] == "0.9.70"
            assert marker["runtime_health"]["binding_state"] == "bound"
            for name in ("task_runner", "c2_listener", "thread_monitor"):
                health = marker["runtime_health"]["threads"][name]
                assert health["entered_loop"] and health["alive"]
            assert Path(plan["data_baseline_path"]).is_file()
            assert preserved_values(data) == before, "Existing binding/config/history changed"
            target_manifest = read_json(current / "update-package-manifest.json")
            assert target_manifest["git_commit"] == read_json(args.release)["git_commit"]
            assert target_manifest["version"] == "0.9.70"
            new_browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}", timeout=30000)
            new_page = new_browser.contexts[0].pages[0]
            new_page.get_by_role("button", name="打开设置", exact=True).click(timeout=30000)
            new_page.get_by_text("V0.9.70", exact=True).wait_for(timeout=30000)
            new_page.screenshot(path=str(case / "after.png"))
            requests = [json.loads(line) for line in Path(spec["requests"]).read_text().splitlines()]
            assert any(r["kind"] == "latest" and r["current_version"] == "0.9.69" and r["status"] == 200 for r in requests)
            assert any(r["kind"] == "download" and r["status"] == 200 for r in requests)
            report.update(status="passed", original_worker_exited=True, original_updater_used=True,
                          protected_data_preserved=True, target_ui_confirmed=True, actual_backend_download=True,
                          target_commit=target_manifest["git_commit"], runtime_threads_alive=True)
    except Exception as exc:
        report["failure"] = str(exc)[:500]
        raise
    finally:
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
    parser.add_argument("--old-package-root", type=Path)
    parser.add_argument("--old-source-root", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--work-root", type=Path)
    args = parser.parse_args()
    if args.serve:
        serve(read_json(args.serve))
        return
    if os.name != "nt":
        raise SystemExit("Requires original Windows EXEs on a Windows runner")
    for name in ("old_package_root", "old_source_root", "archive", "release", "work_root"):
        setattr(args, name, getattr(args, name).resolve())
    results = [run_case(args, status) for status in ("paused", "faulted")]
    write_json(args.work_root / "upgrade-result.json", {"status": "passed", "cases": results})
    print("Original 0.9.69 GUI -> signed 0.9.70: paused and faulted cases passed")


if __name__ == "__main__":
    main()
