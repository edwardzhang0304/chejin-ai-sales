"""Only new retained-install evidence checks; not a repeat of button tests."""
import importlib.util
import json
from pathlib import Path

import pytest
from chejin_worker_client import storage

spec = importlib.util.spec_from_file_location('hook_preserve_install',
    Path(__file__).parents[1] / 'scripts/run-windows-hook-preserve-install.py')
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


@pytest.fixture
def baseline(tmp_path, monkeypatch):
    from chejin_worker_client.models import Binding
    from datetime import datetime, timezone
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path / 'worker_client.sqlite3')
    storage.save_binding(Binding(install.gate.WORKER_ID, install.gate.TOKEN,
        install.gate.INSTANCE_ID, run_status='faulted'))
    storage.request_runtime_pause()
    install.gate.hook_upgrade_fixture.seed(tmp_path, 'https://127.0.0.1:32191/api',
        install.gate.WORKER_ID, install.gate.INSTANCE_ID, datetime.now(timezone.utc).isoformat())
    install.gate.hook_upgrade_fixture.seed_retained_install(tmp_path,
        install.gate.WORKER_ID, install.gate.INSTANCE_ID)
    (tmp_path / 'incidents').mkdir()
    (tmp_path / 'incidents/existing-evidence.json').write_text('{}')
    return tmp_path, install.gate.preserved_values(tmp_path)


def test_retained_unknown_fixture_is_complete_and_preserved(baseline):
    data, before = baseline
    paths = list((data / 'transactions/hook/sends').rglob('outcome.json'))
    assert len(paths) == 1
    outcome = json.loads(paths[0].read_text())
    assert outcome['call_completed'] is False and outcome['automatic_resend_allowed'] is False
    install.assert_retained(before, install.gate.preserved_values(data))


@pytest.mark.parametrize('damage', ['flow', 'pause', 'binding', 'journal', 'config'])
def test_original_recovery_fact_loss_is_rejected(baseline, damage):
    data, before = baseline
    if damage == 'flow':
        storage.finish_runtime_flow(storage.load_runtime_control()['inflight_flow_id'])
    elif damage == 'pause':
        storage.clear_runtime_pause()
    elif damage == 'journal':
        next((data / 'transactions/hook/sends').rglob('outcome.json')).unlink()
    else:
        with storage.connect() as db:
            db.execute("UPDATE binding SET run_status='running'" if damage == 'binding'
                       else "DELETE FROM client_settings WHERE key='hook_deployment_v1'")
            db.commit()
    with pytest.raises(AssertionError):
        install.assert_retained(before, install.gate.preserved_values(data))


def test_install_plan_names_actual_acceptance_scope():
    old = {'schema_version': 1, 'route': 'button', 'recovery': 'none',
           'source_commit': 'sealed-source', 'old_client': {'exe_sha256': 'original'},
           'scope_reason': 'unbound button'}
    new = install.install_plan(old)
    assert new['route'] == 'manual-preserve-data' and new['binding'] == 'bound'
    assert new['recovery'] == 'hook-retained-records-install-only'
    assert new['physical_recovery'] == 'pending_original_windows'
    assert new['old_client'] == old['old_client'] and new['source_commit'] == old['source_commit']
    assert old['route'] == 'button'


@pytest.mark.parametrize('damage', [None, 'worker_target', 'worker_args', 'worker_dir', 'diagnostic_args', 'diagnostic_dir', 'diagnostic_target', 'wechat_args'])
def test_shortcut_validation_checks_all_launch_fields(damage):
    from pathlib import PureWindowsPath
    root = PureWindowsPath(r'C:\New Package')
    deployment = {'wechat_exe': r'C:\Work WeChat\WeChat.exe', 'hook_api_port': 49883}
    data = {'powershell_exe': r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
            'safe_directory': 'C:\\Temp\\'}
    worker = {'name': 'CheJin Worker', 'target': data['powershell_exe'], 'directory': 'C:\\Temp',
              'arguments': '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\\New Package\\start-hook.ps1" -ShowErrorDialog'}
    diagnostic = {'name': '车金故障诊断', 'target': str(root / 'CheJinWorkerClient.exe'),
                  'directory': 'C:\\Temp', 'arguments': '--export-diagnostics'}
    wechat = {'name': 'Work WeChat', 'target': deployment['wechat_exe'],
              'directory': r'C:\Work WeChat', 'arguments': 'StartPort=49883 ConnectType=http RecvType=0'}
    data['links'] = [worker, diagnostic, wechat]
    if damage:
        name, field = damage.split('_')
        {'worker': worker, 'diagnostic': diagnostic, 'wechat': wechat}[name][{'target': 'target', 'dir': 'directory', 'args': 'arguments'}[field]] = 'old or missing'
        with pytest.raises(AssertionError):
            install.check_shortcuts(data, root, deployment)
    else:
        install.check_shortcuts(data, root, deployment)
