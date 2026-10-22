"""Offline, pinned DLL replacement. Never executes Hook or settles business work.

The CLI owns the existing Worker single-instance/data-access guards. A durable
intent plus the two known file/config states allow retry after power loss.
"""
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import psutil
from . import storage
from .hook_config import DEPLOYMENT_KEY, INSTALLATION_KEY, TRANSPORT_KEY, VOICE_SETTING_KEY, HookDeployment
from .hook_installation import validate_profile, launch_information
from .hook_process import pe_machine
from .models import utc_now_iso

# Provider-supplied binary stays private; public source pins only its identity.
NEW_HASH = 'eaab817334789b23cc788ea1dd14a84485d3776bb98d3114662384d3665110a7'
OLD_HASH = '9d6447c7c614d3218878f8790f901f326133c08aac02b7c58903dacc9ff2c24a'
REVISION = 'hook-component-20261022-' + NEW_HASH[:12]
KEYS = (INSTALLATION_KEY, TRANSPORT_KEY, DEPLOYMENT_KEY, VOICE_SETTING_KEY)


def assert_wechat_stopped():
    if sys.platform != 'win32':
        raise ValueError('HOOK_COMPONENT_WINDOWS_REQUIRED')
    try:
        for process in psutil.process_iter(['name']):
            if (process.info['name'] or '').lower() in {'wechat.exe', 'weixin.exe'}:
                raise ValueError('HOOK_COMPONENT_WECHAT_RUNNING')
    except psutil.Error as exc:
        raise ValueError('HOOK_COMPONENT_PROCESS_CHECK_FAILED') from exc


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):os.unlink(name)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')


def _settings(db):
    return {r['key']: {'value':r['value'], 'updated_at':r['updated_at']} for r in db.execute(
        'SELECT key,value,updated_at FROM client_settings WHERE key IN (?,?,?,?)', KEYS)}


def _validate_settings(rows):
    if set(rows)!=set(KEYS):raise ValueError('HOOK_COMPONENT_SETTINGS_INCOMPLETE')
    values={k:json.loads(v['value']) for k,v in rows.items()}
    if values[TRANSPORT_KEY]!={'mode':'hook'}:raise ValueError('HOOK_INSTALL_BUILD_REQUIRED')
    _, deployment=validate_profile({**values[INSTALLATION_KEY], 'deployment':values[DEPLOYMENT_KEY]})
    proof=values[VOICE_SETTING_KEY]
    if proof!={'deployment_revision':deployment.revision,'disabled':True,'operator_confirmed':True}:
        raise ValueError('HOOK_AUTO_TEXT_CONFIRMATION_REQUIRED')
    return deployment


def _state(target, current, intent):
    # Settings unrelated to deployment must remain byte-for-byte intact.
    if current not in (intent['before'],intent['after']):
        raise ValueError('HOOK_COMPONENT_SETTINGS_CHANGED')
    actual=_hash(target)
    if actual not in (OLD_HASH,NEW_HASH):raise ValueError('HOOK_COMPONENT_INSTALLED_HASH_MISMATCH')
    return actual


def update_component(candidate=None, *, rollback=False, automatic_text_disabled=False):
    """Called only by the guarded offline CLI; no process kill or account reset."""
    if automatic_text_disabled is not True:raise ValueError('HOOK_AUTO_TEXT_CONFIRMATION_REQUIRED')
    assert_wechat_stopped()
    binding=storage.load_binding()
    if binding and binding.run_status not in {'paused','faulted','idle'}:
        raise ValueError('HOOK_COMPONENT_PAUSE_REQUIRED')
    source=Path(candidate) if candidate is not None else None
    if not rollback:
        if source is None or source.is_symlink() or not source.is_file() or _hash(source)!=NEW_HASH:
            raise ValueError('HOOK_COMPONENT_CANDIDATE_HASH_MISMATCH')
        if pe_machine(source)!=0x14c:raise ValueError('HOOK_COMPONENT_VERSION_MISMATCH')
    root=storage.APP_DIR/'transactions'/'hook'/'components'/NEW_HASH
    journal=root/'intent.json';backup=root/'original-version.dll'
    with storage.db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        current=_settings(db);deployment=_validate_settings(current)
        target=Path(deployment.dll_path)
        if target.is_symlink() or not target.is_file():raise ValueError('HOOK_INSTALL_COMPONENT_MISSING')
        if journal.exists():
            intent=json.loads(journal.read_text(encoding='utf-8'))
            if (set(intent)!={'schema_version','target','before','after'} or intent['schema_version']!=1
                    or intent['target']!=str(target)):
                raise ValueError('HOOK_COMPONENT_JOURNAL_INVALID')
            before=_validate_settings(intent['before']);after=_validate_settings(intent['after'])
            if (before.dll_sha256!=OLD_HASH or after!=replace(before,dll_sha256=NEW_HASH,revision=REVISION)
                    or any(intent['before'][k]!=intent['after'][k] for k in (INSTALLATION_KEY,TRANSPORT_KEY))):
                raise ValueError('HOOK_COMPONENT_JOURNAL_INVALID')
        else:
            if rollback:raise ValueError('HOOK_COMPONENT_BACKUP_MISSING')
            if deployment.dll_sha256!=OLD_HASH or _hash(target)!=OLD_HASH:
                raise ValueError('HOOK_COMPONENT_INSTALLED_HASH_MISMATCH')
            if root.exists() and any(root.iterdir()):
                # A durable backup may precede the intent on interruption.
                if set(root.iterdir())!={backup} or _hash(backup)!=OLD_HASH:
                    raise ValueError('HOOK_COMPONENT_JOURNAL_INVALID')
            _atomic(backup,target.read_bytes())
            after_deployment=replace(deployment,dll_sha256=NEW_HASH,revision=REVISION)
            after=dict(current);timestamp=utc_now_iso()
            after[DEPLOYMENT_KEY]={'value':_json(asdict(after_deployment)).decode(),'updated_at':timestamp}
            after[VOICE_SETTING_KEY]={'value':_json({'deployment_revision':REVISION,'disabled':True,'operator_confirmed':True}).decode(),'updated_at':timestamp}
            intent={'schema_version':1,'target':str(target),'before':current,'after':after}
            _atomic(journal,_json(intent))
        if not backup.is_file() or backup.is_symlink() or _hash(backup)!=OLD_HASH:
            raise ValueError('HOOK_COMPONENT_BACKUP_INVALID')
        actual=_state(target,current,intent)
        desired=intent['before'] if rollback else intent['after']
        desired_hash=OLD_HASH if rollback else NEW_HASH
        # Stopped WeChat permits component replacement, not deletion of any
        # unresolved call, send, media, identity or business recovery evidence.
        assert_wechat_stopped()
        original=target.read_bytes()
        replaced=False
        try:
            if actual!=desired_hash:
                payload=backup.read_bytes() if rollback else source.read_bytes()
                if hashlib.sha256(payload).hexdigest()!=desired_hash:
                    raise ValueError('HOOK_COMPONENT_CANDIDATE_HASH_MISMATCH')
                _atomic(target,payload);replaced=True
            if _hash(target)!=desired_hash:raise ValueError('HOOK_COMPONENT_INSTALLED_HASH_MISMATCH')
            assert_wechat_stopped()
            for key in (DEPLOYMENT_KEY,VOICE_SETTING_KEY):
                row=desired[key]
                db.execute('UPDATE client_settings SET value=?,updated_at=? WHERE key=?',
                           (row['value'],row['updated_at'],key))
            db.commit()
        except Exception:
            db.rollback()
            if replaced:
                assert_wechat_stopped()
                _atomic(target,original)
            raise
    return {'status':'rolled_back' if rollback else 'installed_pending_validation',
            'dll_sha256':desired_hash,'original_receipts_preserved':True,'automatic_start':False,
            'launch':launch_information()}


def run_cli(args):
    from .config import CONFIG
    from .release_transport import selected_release_channel
    if selected_release_channel(CONFIG.app_dir)!='gray-hook':raise ValueError('HOOK_INSTALL_BUILD_REQUIRED')
    if args.write_report is None:raise ValueError('HOOK_INSTALL_REPORT_REQUIRED')
    result=update_component(args.hook_component_update,rollback=args.hook_component_rollback,
                            automatic_text_disabled=args.confirm_auto_text_disabled)
    _atomic(args.write_report,_json(result))
    return 0
