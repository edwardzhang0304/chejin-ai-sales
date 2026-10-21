from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import pytest
from chejin_worker_client import storage
from chejin_worker_client import hook_installation as install
from chejin_worker_client.hook_config import HookDeployment
from chejin_worker_client.models import Binding


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path / 'worker_client.sqlite3')
    deployment = HookDeployment('gray-0.10.1', r'C:\Work WeChat\WeChat.exe', hashlib.sha256(b'dll').hexdigest(), 30009, 18019)
    original_file, original_read = Path.is_file, Path.read_bytes
    monkeypatch.setattr(Path, 'is_file', lambda p: True if str(p) in (deployment.wechat_exe, deployment.dll_path) else original_file(p))
    monkeypatch.setattr(Path, 'read_bytes', lambda p: b'dll' if str(p) == deployment.dll_path else original_read(p))
    return {'schema_version': 1, 'api_base_url': 'https://gray.example.com/api', 'deployment': asdict(deployment)}


def snapshot():
    with storage.db_connection() as db:
        return list(db.execute('SELECT key,value,updated_at FROM client_settings ORDER BY key'))


def test_initial_install_is_unbound_and_launch_uses_same_configuration(profile):
    value = install.configure(profile, automatic_text_disabled=True)
    assert value['arguments'] == ['StartPort=30009', 'ConnectType=http', 'RecvType=0']
    assert value['working_directory'] == r'C:\Work WeChat'
    assert value['api_base_url'] == profile['api_base_url']
    assert value['automatic_start'] is False and storage.load_binding() is None
    before = snapshot()
    assert install.configure(profile, automatic_text_disabled=True) == value
    assert snapshot() == before


@pytest.mark.parametrize('change', ['url', 'port', 'dll', 'revision'])
def test_existing_settings_cannot_be_silently_reassigned(profile, change):
    install.configure(profile, automatic_text_disabled=True)
    storage.save_binding(Binding('worker-1', 'private-token', 'pc-1', 'faulted'))
    before = snapshot()
    if change == 'url': profile['api_base_url'] = 'https://other.example.com/api'
    elif change == 'port': profile['deployment']['hook_api_port'] = 30010
    elif change == 'dll': profile['deployment']['dll_sha256'] = 'f' * 64
    else: profile['deployment']['revision'] = 'changed'
    with pytest.raises(ValueError): install.configure(profile, automatic_text_disabled=True)
    assert snapshot() == before
    assert storage.load_binding().run_status == 'faulted'
    assert storage.load_binding().worker_token == 'private-token'


def test_existing_ocr_identity_is_not_migrated_by_fresh_install(profile):
    storage.save_binding(Binding('ocr', 'private-token', 'original-pc', 'paused'))
    with pytest.raises(ValueError, match='EXISTING_BINDING_REQUIRES_MIGRATION'):
        install.configure(profile, automatic_text_disabled=True)
    assert storage.load_binding().worker_id == 'ocr'
    assert not snapshot()


def test_voice_confirmation_and_payload_fields_are_mandatory(profile):
    with pytest.raises(ValueError, match='AUTO_TEXT_CONFIRMATION_REQUIRED'):
        install.configure(profile, automatic_text_disabled=False)
    profile['worker_token'] = 'must-not-be-in-public-profile'
    with pytest.raises(ValueError, match='PROFILE_INVALID'): install.validate_profile(profile)


@pytest.mark.parametrize('url', ['http://public.example.com/api', 'https://user:password@example.com/api',
    'https://example.com/api?token=secret', 'https://example.com/api#fragment',
    'https://example.com/not-api', 'https://example.com:99999/api', ' https://example.com/api'])
def test_backend_never_silently_falls_back_or_accepts_credential_url(profile, url):
    profile['api_base_url'] = url
    with pytest.raises(ValueError, match='BACKEND_INVALID'): install.validate_profile(profile)


def test_partial_previous_configuration_fails_without_overwrite(profile):
    with storage.db_connection() as db:
        db.execute('INSERT INTO client_settings VALUES (?,?,?)', (install.TRANSPORT_KEY, '{"mode":"hook"}', 'old'))
        db.commit()
    before = snapshot()
    with pytest.raises(ValueError, match='EXISTING_CONFIG_MISMATCH'):
        install.configure(profile, automatic_text_disabled=True)
    assert snapshot() == before


def test_shortcut_and_start_share_validated_arguments_and_keep_existing_wechat():
    script = (Path(__file__).parents[1] / 'packaging/start-hook.ps1').read_text(encoding='utf-8-sig')
    assert "Save-UnicodeShortcut -Path (Join-Path $desktop 'Work WeChat.lnk') -Target $launch.exe -WorkingDirectory $launch.working_directory -Arguments ($launch.arguments -join ' ')" in script
    assert '[CheJin.ShortcutWriter]::Save($Path, $Target, $WorkingDirectory, $Arguments)' in script
    assert 'link.SetArguments(arguments);' in script
    assert '-ArgumentList $launch.arguments' in script
    assert 'Get-Process -Name WeChat,Weixin' in script
    assert 'Stop-Process' not in script and 'HookLab' not in script and 'uat/enrol' not in script


def test_packaged_hook_uses_separate_home_and_saved_backend_only(profile, tmp_path, monkeypatch):
    from chejin_worker_client import config, release_transport
    install.configure(profile, automatic_text_disabled=True)
    monkeypatch.setattr(release_transport, 'packaged_transport', lambda **kw: 'hook')
    monkeypatch.delenv('CHEJIN_WORKER_HOME', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setenv('CHEJIN_API_BASE_URL', 'https://wrong-production.example.com/api')
    assert config.default_app_dir() == tmp_path / 'local' / 'CheJinWorkerHook'
    assert config.configured_api_base_url(tmp_path) == profile['api_base_url']
    assert config.configured_api_base_url(tmp_path / 'fresh') == ''
    assert not (tmp_path / 'fresh').exists()


def test_ocr_default_home_and_backend_override_unchanged(tmp_path, monkeypatch):
    from chejin_worker_client import config, release_transport
    monkeypatch.setattr(release_transport, 'packaged_transport', lambda **kw: 'ocr')
    monkeypatch.delenv('CHEJIN_WORKER_HOME', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setenv('CHEJIN_API_BASE_URL', 'https://ocr.example.com/api/')
    assert config.default_app_dir() == tmp_path / 'CheJinWorker'
    assert config.configured_api_base_url(tmp_path) == 'https://ocr.example.com/api'


@pytest.mark.parametrize('transport,name', [('hook', '.chejin-worker-hook'), ('ocr', '.chejin-worker')])
def test_missing_localappdata_still_keeps_transport_homes_separate(monkeypatch, transport, name):
    from chejin_worker_client import config, release_transport
    monkeypatch.setattr(release_transport, 'packaged_transport', lambda **kw: transport)
    monkeypatch.delenv('CHEJIN_WORKER_HOME', raising=False)
    monkeypatch.delenv('LOCALAPPDATA', raising=False)
    assert config.default_app_dir() == Path.home() / name


def test_current_product_versions_are_unified():
    import ast
    import tomllib
    from chejin_worker_client import __version__
    root = Path(__file__).resolve().parents[2]
    frontend = json.loads((root / 'frontend/package.json').read_text())
    lock = json.loads((root / 'frontend/package-lock.json').read_text())
    backend = tomllib.loads((root / 'backend/pyproject.toml').read_text())
    contract = json.loads((root / 'contracts/c2_contract_v3.json').read_text())
    hook = json.loads((root / 'contracts/hook_contract_v1.json').read_text())
    entry = ast.parse((root / 'worker-client/packaging/chejin_worker_client_entry.py').read_text())
    entry_version = next(ast.literal_eval(n.value) for n in entry.body if isinstance(n, ast.Assign)
                         and any(isinstance(t, ast.Name) and t.id == 'ENTRY_VERSION' for t in n.targets))
    assert {__version__, frontend['version'], lock['version'], lock['packages']['']['version'],
            backend['project']['version'], contract['contract_revision'], hook['product_version'],
            entry_version} == {__version__}
