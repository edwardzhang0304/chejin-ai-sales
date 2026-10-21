"""Run sealed EXEs, preserving a synthetic bound Hook flow and unknown journal.

Install acceptance only. No DLL/WeChat is simulated and no original send is
settled here. Physical recovery and working-WeChat shortcut launch need the
original Windows machine. This never relaxes the original button-upgrade gate.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.request

spec = importlib.util.spec_from_file_location('original_upgrade_gate',
    Path(__file__).with_name('run-windows-client-upgrade-test.py'))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def install_plan(original):
    return {**original, 'schema_version': 'hook-preserve-install-plan-v1',
        'route': 'manual-preserve-data', 'recovery': 'hook-retained-records-install-only',
        'binding': 'bound', 'physical_recovery': 'pending_original_windows',
        'scope_reason': 'Sealed old and target EXEs; preserve bound Hook flow and unknown journal. No button upgrade or physical Hook recovery claim.'}


def check_shortcuts(receipt, installed, deployment):
    links = {s['name']: s for s in receipt['links']}
    assert set(links) == {'Work WeChat', 'CheJin Worker', '车金故障诊断'}
    def same_path(a, b):
        return PureWindowsPath(a) == PureWindowsPath(b)
    work = links['Work WeChat']
    assert same_path(work['target'], deployment['wechat_exe'])
    assert same_path(work['directory'], str(PureWindowsPath(deployment['wechat_exe']).parent))
    assert work['arguments'].split() == [f"StartPort={deployment['hook_api_port']}", 'ConnectType=http', 'RecvType=0']
    worker = links['CheJin Worker']
    assert same_path(worker['target'], receipt['powershell_exe'])
    assert same_path(worker['directory'], receipt['safe_directory'])
    expected = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + str(installed / 'start-hook.ps1') + '" -ShowErrorDialog'
    assert worker['arguments'].lower() == expected.lower(), 'Worker shortcut arguments mismatch'
    diagnostic = links['车金故障诊断']
    assert same_path(diagnostic['target'], str(installed / 'CheJinWorkerClient.exe'))
    assert same_path(diagnostic['directory'], receipt['safe_directory'])
    assert diagnostic['arguments'] == '--export-diagnostics', 'Diagnostic shortcut arguments mismatch'


def assert_retained(before, after):
    """Keep every original business fact; only runtime row timestamp may advance."""
    assert len([r for r in before['settings'] if r[0] == gate.RUNTIME_CONTROL_KEY]) == 1, 'Missing baseline runtime control'
    assert before['hook']['journal_files'], 'Missing baseline Hook journals'
    for key in before:
        if key != 'settings':
            assert before[key] == after[key], 'Protected data changed: ' + key
    current = {row[0]: row for row in after['settings']}
    for row in before['settings']:
        assert row[0] in current, 'Original setting missing: ' + row[0]
        found = current[row[0]]
        if row[0] == gate.RUNTIME_CONTROL_KEY:
            control = json.loads(row[1])
            assert control['inflight_flow_id'] and control['inflight_flow_kind'] == 'chat_reply'
            assert control['pause_requested'] is True and control['update_no_new_work'] is False
            assert json.loads(found[1]) == control, 'Retained flow or stop intent changed'
            assert found[2] >= row[2], 'Runtime timestamp regressed'
        else:
            assert found == row, 'Original setting changed: ' + row[0]


def close_normally(pid):
    import ctypes
    from ctypes import wintypes
    import psutil
    user32 = ctypes.windll.user32
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    windows = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def capture(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            windows.append(hwnd)
        return True
    callback = callback_type(capture)
    user32.EnumWindows(callback, 0)
    assert windows, 'No visible original Worker window'
    process = psutil.Process(pid)
    descendants = process.children(recursive=True)
    for hwnd in windows:
        assert user32.PostMessageW(hwnd, 0x0010, 0, 0), 'Normal close failed'
    assert process.wait(timeout=90) == 0, 'Worker did not exit normally'
    _, alive = psutil.wait_procs(descendants, timeout=20)
    assert not alive, 'Worker children still running'


def wait_page(process, port):
    def ready():
        assert process.poll() is None, 'Worker exited before its UI was ready'
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/json', timeout=2) as r:
                return any(p.get('type') == 'page' for p in json.load(r))
        except OSError:
            return False
    gate.wait_for(ready, 'packaged Worker UI')


def run(args):
    import psutil
    assert os.name == 'nt' and os.environ.get('GITHUB_ACTIONS') == 'true', 'Disposable Windows CI only'
    area = args.root.resolve()
    plan = install_plan(gate.read_json(area / 'plan.json'))
    gate.write_json(area / 'preserve-plan.json', plan)
    old_root = area / 'old/CheJinWorkerClient'
    target_root = area / 'target/CheJinWorkerClient'
    old_source = area / 'old-source/worker-client'
    archive = area / 'target/artifact.zip'
    descriptor = gate.read_json(area / 'release.json')
    assert gate.digest(archive) == descriptor['artifact_sha256'] == args.sha256
    assert descriptor['git_commit'] == plan['source_commit'] and descriptor['version'] == plan['version']
    assert gate.digest(old_root / 'CheJinWorkerClient.exe') == plan['old_client']['exe_sha256']
    case = area / 'preserve-install'
    case.mkdir(exist_ok=False)
    data = case / 'local-appdata/CheJinWorkerHook'
    data.mkdir(parents=True)
    current = case / 'old-install/CheJinWorkerClient'
    shutil.copytree(old_root, current)
    cert, key = gate.tls_files(case)
    port, debug = gate.free_port(), gate.free_port()
    base = f'https://127.0.0.1:{port}'
    server_spec = {'port': port, 'cert': str(cert), 'key': str(key), 'archive': str(archive),
        'release': str(area / 'release.json'), 'run_status': 'faulted',
        'requests': str(case / 'requests.jsonl'),
        'public_keys': str(old_root / '_internal/release-signing-public-keys.json')}
    gate.write_json(case / 'spec.json', server_spec)
    env = {k: v for k, v in os.environ.items() if not any(w in k.upper() for w in ('TOKEN', 'SECRET', 'API_KEY', 'SIGNING_PRIVATE'))}
    env.update(CHEJIN_WORKER_HOME=str(data), LOCALAPPDATA=str(data.parent), CHEJIN_C2_ENABLED='false',
        CHEJIN_API_BASE_URL=base + '/api', CHEJIN_RELEASE_TRANSPORT='hook',
        CHEJIN_HEARTBEAT_INTERVAL='1', CHEJIN_API_TIMEOUT='5',
        SSL_CERT_FILE=str(cert), REQUESTS_CA_BUNDLE=str(cert),
        QTWEBENGINE_REMOTE_DEBUGGING=f'127.0.0.1:{debug}', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
        ENVIRONMENT='test', DATABASE_URL='sqlite:///' + (case / 'backend.sqlite3').as_posix(),
        AUTO_CREATE_TABLES='true', CLIENT_RELEASE_ARTIFACT_ROOT=str(case / 'backend-artifacts'),
        CLIENT_RELEASE_PUBLIC_BASE_URL=base + '/api', C3_AI_ADAPTER_MODE='mock',
        C3_BATCH_RECOVERY_POLL_SECONDS='0', FEISHU_APP_ID='', FEISHU_APP_SECRET='')
    for name in ('CHEJIN_WORKER_UI_MODE', 'CHEJIN_RELEASE_SIGNING_KEYS_PATH', 'CHEJIN_RELEASE_SIGNING_PUBLIC_KEY_BASE64'):
        env.pop(name, None)
    env = gate.windows_powershell_environment(env)
    gate.seed_old_data(old_source, data, 'faulted', env, hook_bound=True)
    seed_code = """import json,os,sys
sys.path.insert(0,sys.argv[1])
from hook_upgrade_fixture import seed_retained_install
from pathlib import Path
state=seed_retained_install(os.environ['CHEJIN_WORKER_HOME'],sys.argv[2],sys.argv[3])
Path(sys.argv[4]).write_text(json.dumps(state))
"""
    subprocess.run([sys.executable, '-c', seed_code, str(gate.ROOT / 'ops/formal_release'),
        gate.WORKER_ID, gate.INSTANCE_ID, str(case / 'retained-flow.json')],
        env={**env, 'PYTHONPATH': str(old_source)}, cwd=old_source, check=True)
    before = gate.preserved_values(data)
    checkpoints = {'seeded': before}
    report = {'status': 'failed', 'mode': 'hook_preserve_install',
        'target_zip_sha256': args.sha256, 'target_source': descriptor['git_commit'],
        'physical_recovery': 'pending_original_windows', 'button_upgrade': 'not_tested',
        'wechat_dll_ports_and_history': 'not_tested', 'worker_shortcut_launch': 'pending_real_wechat',
        'synthetic_data_only': True, 'package_rebuilt': False}
    processes = []
    log = (case / 'process.log').open('w', encoding='utf-8')
    try:
        backend = subprocess.Popen([sys.executable, str(Path(gate.__file__)), '--serve', str(case / 'spec.json')],
            env=env, cwd=case, stdout=log, stderr=log)
        processes.append(backend)
        context = ssl.create_default_context(cafile=str(cert))
        def backend_ready():
            assert backend.poll() is None, 'Isolated backend exited'
            try:
                with urllib.request.urlopen(base + '/healthz', context=context, timeout=2) as response:
                    return response.status == 200
            except OSError:
                return False
        gate.wait_for(backend_ready, 'isolated backend')
        # Generated fixture only; no production database is used by this tool.
        with sqlite3.connect(case / 'backend.sqlite3') as db:
            db.execute('UPDATE workers SET inflight_flow_state=? WHERE id=?',
                ((case / 'retained-flow.json').read_text(), gate.WORKER_ID))
            db.commit()
        old = subprocess.Popen([str(current / 'CheJinWorkerClient.exe')], env=env, cwd=current, stdout=log, stderr=log)
        processes.append(old)
        wait_page(old, debug)
        old_fault = gate.wait_for(lambda: gate.hook_upgrade_fixture.environment_fault(data,
            not_before=psutil.Process(old.pid).create_time()), 'old actual missing-WeChat fault', 40)
        with gate.QtPage(debug) as page:
            page.click_button('打开设置'); page.wait_text('V' + plan['old_client']['version'])
            page.screenshot(case / 'before.png')
        checkpoints['before_exit'] = gate.preserved_values(data)
        assert_retained(before, checkpoints['before_exit'])
        close_normally(old.pid)
        checkpoints['after_normal_exit'] = gate.preserved_values(data)
        assert_retained(before, checkpoints['after_normal_exit'])
        shutil.copytree(data, case / 'backup')
        assert_retained(before, gate.preserved_values(case / 'backup'))
        sys.path.insert(0, str(gate.ROOT / 'worker-client'))
        from chejin_worker_client.models import ClientRelease
        from chejin_worker_client.release_package_contract import verify_release_signature, load_trusted_release_keys, verify_staged_package
        release = ClientRelease.from_api({**descriptor, 'latest_version': descriptor['version'], 'update_available': True})
        verify_release_signature(release, trusted_keys=load_trusted_release_keys(Path(server_spec['public_keys'])))
        verify_staged_package(release, target_root)
        installed = case / 'new-install/CheJinWorkerClient'
        shutil.copytree(target_root, installed)
        manifest = verify_staged_package(release, installed)
        # Run the shipped refresh entry, rather than synthesizing shortcuts.
        subprocess.run(['cmd.exe', '/d', '/c', str(installed / 'Refresh-CheJin-Shortcuts.cmd')],
            env=env, cwd=case, stdout=log, stderr=log, check=True, timeout=90)
        shortcut_script = case / 'read-shortcuts.ps1'
        shortcut_script.write_text("""$ErrorActionPreference='Stop'
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
namespace CheJinAcceptance {
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    internal class ShellLink { }
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count, IntPtr data, uint flags);
        void GetIDList(out IntPtr list);
        void SetIDList(IntPtr list);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int count);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int count);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string text);
    }
    public static class ShortcutReader {
        public static string[] Read(string path) {
            object instance = new ShellLink();
            try {
                ((IPersistFile)instance).Load(path, 0);
                IShellLinkW link = (IShellLinkW)instance;
                StringBuilder target = new StringBuilder(32768);
                StringBuilder directory = new StringBuilder(32768);
                StringBuilder arguments = new StringBuilder(32768);
                link.GetPath(target, target.Capacity, IntPtr.Zero, 0);
                link.GetWorkingDirectory(directory, directory.Capacity);
                link.GetArguments(arguments, arguments.Capacity);
                return new string[] { target.ToString(), directory.ToString(), arguments.ToString() };
            } finally { Marshal.FinalReleaseComObject(instance); }
        }
    }
}
'@
$desktop=[Environment]::GetFolderPath('Desktop')
$rows=@('Work WeChat','CheJin Worker','车金故障诊断') | ForEach-Object {
  $p=Join-Path $desktop ($_+'.lnk'); if(-not(Test-Path -LiteralPath $p)){throw 'Missing shortcut'}
  $before=(Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash
  $s=[CheJinAcceptance.ShortcutReader]::Read($p)
  if((Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash -ne $before){throw 'Shortcut changed during read'}
  [pscustomobject]@{name=$_;target=$s[0];directory=$s[1];arguments=$s[2];sha256=$before.ToLowerInvariant()}
}
[pscustomobject]@{reader='IPersistFile.Load/IShellLinkW';links=@($rows);powershell_exe=(Join-Path $PSHOME 'powershell.exe');safe_directory=[IO.Path]::GetTempPath()} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $args[0] -Encoding UTF8
""", encoding='utf-8-sig')
        shortcut_path = case / 'shortcuts.json'
        subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(shortcut_script), str(shortcut_path)],
            env=env, check=True, timeout=30, stdout=log, stderr=log)
        deployment = json.loads(next(r[1] for r in before['settings'] if r[0] == 'hook_deployment_v1'))
        check_shortcuts(gate.read_json(shortcut_path), installed, deployment)
        checkpoints['after_refresh'] = gate.preserved_values(data)
        assert_retained(before, checkpoints['after_refresh'])
        new = subprocess.Popen([str(installed / 'CheJinWorkerClient.exe')], env=env, cwd=installed, stdout=log, stderr=log)
        processes.append(new)
        wait_page(new, debug)
        gate.wait_for(lambda: gate.hook_upgrade_fixture.environment_fault(data,
            not_before=psutil.Process(new.pid).create_time(), excluded_ids=old_fault['log_ids']),
            'new actual missing-WeChat fault', 40)
        with gate.QtPage(debug) as page:
            page.click_button('打开设置'); page.wait_text('V' + descriptor['version'])
            page.screenshot(case / 'after.png')
        checkpoints['new_runtime'] = gate.preserved_values(data)
        assert_retained(before, checkpoints['new_runtime'])
        close_normally(new.pid)
        checkpoints['final'] = gate.preserved_values(data)
        assert_retained(before, checkpoints['final'])
        verify_staged_package(release, installed)
        report.update(status='passed', normal_old_exit=True, normal_new_exit=True,
            original_data_directory_reused=True, backup_verified=True, original_identity_preserved=True,
            retained_flow_preserved=True, unknown_send_files_preserved=True, stopped_intent_preserved=True,
            actual_target_ui=True, real_shortcuts_refreshed=True, full_wechat_arguments_verified=True,
            target_program_manifest_verified=True, target_commit=manifest['git_commit'])
    except Exception as exc:
        report['failure'] = type(exc).__name__ + ': ' + str(exc)[:600]
        raise
    finally:
        gate.write_json(case / 'result.json', report)
        gate.write_json(case / 'data-checkpoints.json', checkpoints)
        # Only disposable processes belonging to this acceptance run are cleaned.
        for process in reversed(processes):
            if process.poll() is None:
                try:
                    for child in psutil.Process(process.pid).children(recursive=True):
                        child.terminate()
                    process.terminate(); process.wait(timeout=15)
                except (psutil.Error, subprocess.TimeoutExpired):
                    pass
        log.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    run(parser.parse_args())
