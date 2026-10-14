"""Choose a release channel from the explicit build and persisted mode only.

Reading this state never creates a database, binds a device, or activates Hook.
An incompatible or malformed selection fails closed; it cannot fall back to OCR.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from .shared_rules import client_release_rules as rules


def packaged_transport(*, build_root=None):
    """Read the same build identity used by channel admission, without storage I/O."""
    root = Path(build_root) if build_root is not None else Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    identity = root / 'runtime-build-identity.json'
    if not identity.is_file():
        return None
    metadata = json.loads(identity.read_text(encoding='utf-8-sig'))
    if not isinstance(metadata, dict):
        raise ValueError('UPDATE_TRANSPORT_SELECTION_INVALID')
    transport = metadata.get('transport')
    if 'transport' in metadata:
        rules.channel_for_transport(transport)
    return transport


def selected_release_channel(data_dir, *, build_root=None):
    from .release_package_contract import ClientUpdateError
    try:
        transport = packaged_transport(build_root=build_root)
        database = Path(data_dir) / 'worker_client.sqlite3'
        if database.is_file():
            with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
                table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='client_settings'").fetchone()
                row = db.execute("SELECT value FROM client_settings WHERE key='wechat_transport_v1'").fetchone() if table else None
            if row is not None:
                value = json.loads(row[0])
                if value not in ({'mode':'ocr'}, {'mode':'hook'}):
                    raise ValueError()
                if transport is not None and transport != value['mode']:
                    raise ValueError()
                transport = value['mode']
        return rules.channel_for_transport(transport or 'ocr')
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        raise ClientUpdateError('UPDATE_TRANSPORT_SELECTION_INVALID', '更新模式与当前安装或数据不匹配，请联系管理员保留数据安装') from None
