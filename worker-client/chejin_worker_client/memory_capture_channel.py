"""Same-user bounded file channel. No TCP, business lock, payload or commands."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

import psutil

SCHEMA = 1
CAPTURE_SECONDS = 30
ACK_SECONDS = 5
WAIT_SECONDS = 40
COOLDOWN_SECONDS = 60
POLL_SECONDS = .25
MAX_BYTES = 64 * 1024
MAX_REQUESTS = 16
KEEP_SECONDS = 120
STATES = frozenset({'queued', 'accepted', 'running', 'finished', 'failed', 'timeout',
                    'not_running', 'unsupported', 'busy', 'cooldown'})
_UUID = re.compile(r'^[0-9a-f]{32}$')
_TEMP = re.compile(r'^[0-9a-f]{32}\.json\.[0-9a-f]{32}\.tmp$')


def identity(pid=None):
    p = psutil.Process(pid or os.getpid())
    if p.status() == psutil.STATUS_ZOMBIE:
        raise ValueError('process_exited')
    return {'pid': p.pid, 'created': p.create_time(),
            'user_hash': hashlib.sha256(p.username().encode()).hexdigest()}


def alive(expected):
    try:
        return identity(expected['pid']) == {k: expected[k] for k in ('pid', 'created', 'user_hash')}
    except (KeyError, ValueError, psutil.Error, OSError):
        return False


def safe(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.is_relative_to(root):
        raise ValueError('channel_path_boundary')
    for ancestor in root.parents:
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('channel_link_boundary')
    current = root
    for part in (None, *path.relative_to(root).parts):
        if part is not None:current = current / part
        try:info = current.lstat()
        except FileNotFoundError:continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('channel_link_boundary')
        if os.name != 'nt' and info.st_uid != os.getuid():
            raise ValueError('channel_owner_mismatch')
    return path


def windows_security(folder, *, create=False, initialize=False):
    import win32api, win32con, win32security
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    descriptor = win32security.GetNamedSecurityInfo(str(folder), win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
    if not create and descriptor.GetSecurityDescriptorOwner() != sid:
        raise ValueError('channel_owner_mismatch')
    if initialize:
        acl = win32security.ACL()
        for principal in (sid, system):
            acl.AddAccessAllowedAceEx(win32security.ACL_REVISION,
                win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE,
                win32con.FILE_ALL_ACCESS, principal)
        win32security.SetNamedSecurityInfo(str(folder), win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION, sid, None, acl, None)
    else:
        acl = descriptor.GetSecurityDescriptorDacl()
        if acl is None:
            raise ValueError('channel_permissions_invalid')
        for index in range(acl.GetAceCount()):
            ace = acl.GetAce(index)
            if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE or ace[-1] not in (sid, system):
                raise ValueError('channel_permissions_invalid')


def restrict(folder, *, created=False):
    """Only this user and SYSTEM; no inherited cross-account writes."""
    if os.name == 'nt':
        windows_security(folder, create=created, initialize=True)
    else:
        folder.chmod(0o700)


class Channel:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.folder = self.root / 'runtime' / 'memory-capture'
        self.requests = self.folder / 'requests'

    def initialize(self):
        safe(self.requests, self.root)
        for folder in (self.folder, self.requests):
            try:folder.mkdir(parents=True, exist_ok=False); created = True
            except FileExistsError:created = False
            safe(folder, self.root)
            restrict(folder, created=created)

    def read(self, path, *, limit=MAX_BYTES):
        path = safe(path, self.root)
        before = path.stat()
        if before.st_size > limit:raise ValueError('channel_record_budget')
        with path.open('rb') as handle:
            opened = os.fstat(handle.fileno())
            raw = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
        latest = safe(path, self.root).stat()
        stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
        if len(raw) > limit or not stamp(before) == stamp(opened) == stamp(after) == stamp(latest):
            raise ValueError('channel_record_changed')
        value = json.loads(raw)
        if not isinstance(value, dict):raise ValueError('channel_record_shape')
        return value

    def write(self, path, value):
        path = safe(path, self.root)
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
        if len(raw) > MAX_BYTES:raise ValueError('channel_record_budget')
        temporary = safe(path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp'), self.root)
        try:
            with temporary.open('xb') as handle:
                if os.name != 'nt':os.fchmod(handle.fileno(), 0o600)
                handle.write(raw)
            safe(path, self.root)
            os.replace(temporary, path)
        finally:temporary.unlink(missing_ok=True)

    def lock(self):
        from .update_data_access import DataAccess
        return DataAccess(safe(self.folder / 'channel.lock', self.root), True)

    def paths(self, *, include_temporary=False):
        result = []
        for n, path in enumerate(self.requests.iterdir()):
            if n >= MAX_REQUESTS * 4:raise ValueError('channel_directory_budget')
            if path.suffix == '.json' and _UUID.fullmatch(path.stem):result.append(path)
            elif _TEMP.fullmatch(path.name):
                if include_temporary:result.append(path)
            else:raise ValueError('channel_unexpected_file')
        return result

    def endpoint(self):
        for folder in (self.folder, self.requests):
            safe(folder, self.root)
            if os.name == 'nt':windows_security(folder)
            elif folder.stat().st_mode & 0o077:raise ValueError('channel_permissions_invalid')
        value = self.read(self.folder / 'endpoint.json')
        target = value['target']
        marker = self.read(self.root / 'runtime/session.json')
        if (value.get('schema') != SCHEMA or value.get('operation') != 'memory_capture'
                or target.get('user_hash') != identity()['user_hash'] or not alive(target)
                or marker.get('session_id') != target.get('runtime_session')
                or marker.get('pid') != target.get('pid') or marker.get('status') != 'running'):
            raise ValueError('worker_identity_unavailable')
        return target

    def prune(self, target=None):
        from .memory_request_retention import retire
        now, wall = time.monotonic(), time.time()
        for path in self.paths(include_temporary=True):
            # Caller holds the channel lock; no legitimate request writer can
            # still own an atomic temporary file here.
            if _TEMP.fullmatch(path.name):
                retire(self, path, 'orphan_atomic_write')
                continue
            try:value = self.read(path)
            except (ValueError, OSError) as exc:
                retire(self, path, failure_reason(exc))
                continue
            if value.get('schema') != SCHEMA or value.get('request_id') != path.stem:
                retire(self, path, 'request_invalid_identity')
                continue
            if target is not None and value.get('target') != target:
                retire(self, path, 'request_old_or_wrong_worker')
                continue
            # Only protocol-owned expired diagnostics, never business receipts.
            if (value.get('schema') == SCHEMA and value.get('request_id') == path.stem
                    and type(value.get('expires_mono')) in (int, float)
                    and type(value.get('requested_wall')) in (int, float)
                    and type(value.get('requested_mono')) in (int, float)
                    and math.isfinite(value['expires_mono']) and math.isfinite(value['requested_wall'])
                    and (now - value['expires_mono'] > KEEP_SECONDS
                         or (now < value.get('requested_mono', now)
                             and wall - value['requested_wall'] > KEEP_SECONDS + WAIT_SECONDS))):
                retire(self, path, 'request_retention_expired')

    def active_count(self):
        return sum(self.read(path).get('status') in ('queued', 'accepted', 'running')
                   for path in self.paths())


def request_valid(value, target):
    now = time.monotonic()
    start, expires = value.get('requested_mono'), value.get('expires_mono')
    allowed = {'schema', 'evidence_kind', 'operation', 'request_id', 'requested_mono', 'requested_wall',
               'expires_mono', 'status', 'target', 'requester'}
    return (not set(value) - allowed and value.get('schema') == SCHEMA and value.get('operation') == 'memory_capture'
            and bool(_UUID.fullmatch(str(value.get('request_id', ''))))
            and value.get('target') == target and value.get('status') == 'queued'
            and type(start) in (int, float) and type(expires) in (int, float)
            and math.isfinite(start) and math.isfinite(expires)
            and start <= now <= expires and 0 < expires - start <= WAIT_SECONDS
            and isinstance(value.get('requester'), dict)
            and value['requester'].get('user_hash') == target.get('user_hash')
            and alive(value['requester']))


def failure_reason(exc):
    # Only our fixed diagnostic codes; never exception text containing paths/data.
    known = {'channel_path_boundary', 'channel_link_boundary', 'channel_owner_mismatch',
             'channel_record_budget', 'channel_record_changed', 'channel_record_shape',
             'channel_directory_budget', 'channel_permissions_invalid', 'channel_unexpected_file', 'worker_identity_unavailable',
             'worker_identity_changed', 'request_path_identity_mismatch', 'capture_identity_mismatch',
             'response_identity_changed', 'response_status_invalid'}
    return str(exc) if isinstance(exc, ValueError) and str(exc) in known else type(exc).__name__
