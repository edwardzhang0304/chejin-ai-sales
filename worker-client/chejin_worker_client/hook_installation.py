"""Customer-independent Hook setup. Does not pair, enrol, start work or call WeChat."""
from dataclasses import asdict
import hashlib
import ipaddress
import json
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from .hook_config import (HookDeployment, DEPLOYMENT_KEY, INSTALLATION_KEY, TRANSPORT_KEY,
                          VOICE_SETTING_KEY, load_hook_deployment)


def validate_profile(payload):
    if not isinstance(payload, dict) or set(payload) != {'schema_version', 'api_base_url', 'deployment'}:
        raise ValueError('HOOK_INSTALL_PROFILE_INVALID')
    if type(payload['schema_version']) is not int or payload['schema_version'] != 1:
        raise ValueError('HOOK_INSTALL_PROFILE_INVALID')
    url = payload['api_base_url']
    if not isinstance(url, str) or any(c.isspace() for c in url):
        raise ValueError('HOOK_INSTALL_BACKEND_INVALID')
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme in {'https', 'http'} and parsed.hostname and parsed.path == '/api'
                 and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
        if parsed.scheme == 'http':
            valid = valid and ipaddress.ip_address(parsed.hostname).is_private
        if parsed.port is not None:
            valid = valid and 1 <= parsed.port <= 65535
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('HOOK_INSTALL_BACKEND_INVALID')
    raw = payload['deployment']
    if not isinstance(raw, dict) or set(raw) != set(HookDeployment.__dataclass_fields__):
        raise ValueError('HOOK_INSTALL_PROFILE_INVALID')
    return url, HookDeployment(**raw)


def configure(profile, *, automatic_text_disabled):
    from . import storage
    from .models import utc_now_iso
    if automatic_text_disabled is not True:
        raise ValueError('HOOK_AUTO_TEXT_CONFIRMATION_REQUIRED')
    url, deployment = validate_profile(profile)
    exe, dll = Path(deployment.wechat_exe), Path(deployment.dll_path)
    if not exe.is_file() or not dll.is_file():
        raise ValueError('HOOK_INSTALL_COMPONENT_MISSING')
    if hashlib.sha256(dll.read_bytes()).hexdigest() != deployment.dll_sha256.lower():
        raise ValueError('HOOK_INSTALL_COMPONENT_HASH_MISMATCH')
    values = {
        INSTALLATION_KEY: {'schema_version': 1, 'api_base_url': url},
        DEPLOYMENT_KEY: asdict(deployment),
        TRANSPORT_KEY: {'mode': 'hook'},
        VOICE_SETTING_KEY: {'deployment_revision': deployment.revision, 'disabled': True, 'operator_confirmed': True},
    }
    with storage.db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        prior = {r['key']: json.loads(r['value']) for r in db.execute(
            'SELECT key,value FROM client_settings WHERE key IN (?,?,?,?)', tuple(values))}
        # No identity migration or partial replacement of an existing deployment.
        if prior and prior != values:
            raise ValueError('HOOK_INSTALL_EXISTING_CONFIG_MISMATCH')
        if not prior and db.execute('SELECT 1 FROM binding LIMIT 1').fetchone():
            raise ValueError('HOOK_INSTALL_EXISTING_BINDING_REQUIRES_MIGRATION')
        if not prior:
            db.executemany('INSERT INTO client_settings(key,value,updated_at) VALUES (?,?,?)',
                [(k, json.dumps(v, sort_keys=True), utc_now_iso()) for k, v in values.items()])
        db.commit()
    return launch_information()


def launch_information():
    from . import storage
    with storage.db_connection() as db:
        rows = {r['key']: json.loads(r['value']) for r in db.execute(
            'SELECT key,value FROM client_settings WHERE key IN (?,?)', (INSTALLATION_KEY, TRANSPORT_KEY))}
    if rows.get(TRANSPORT_KEY) != {'mode': 'hook'} or INSTALLATION_KEY not in rows:
        raise ValueError('HOOK_INSTALL_REQUIRED')
    installation = rows[INSTALLATION_KEY]
    if not isinstance(installation, dict) or set(installation) != {'schema_version', 'api_base_url'}:
        raise ValueError('HOOK_INSTALL_PROFILE_INVALID')
    deployment = load_hook_deployment()
    url, _ = validate_profile({**installation, 'deployment': asdict(deployment)})
    return {'api_base_url': url, 'exe': deployment.wechat_exe,
            'working_directory': str(PureWindowsPath(deployment.wechat_exe).parent),
            'arguments': list(deployment.launch_arguments), 'automatic_start': False}


def run_cli(args):
    """Caller owns the original single-instance and update-data guards."""
    from .config import CONFIG
    from .release_transport import selected_release_channel
    if selected_release_channel(CONFIG.app_dir) != 'gray-hook':
        raise ValueError('HOOK_INSTALL_BUILD_REQUIRED')
    if args.write_report is None:
        raise ValueError('HOOK_INSTALL_REPORT_REQUIRED')
    result = (configure(json.loads(args.hook_configure.read_text(encoding='utf-8-sig')),
                        automatic_text_disabled=args.confirm_auto_text_disabled)
              if args.hook_configure else launch_information())
    args.write_report.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return 0
