"""Same-user bounded file channel. No TCP, business lock, payload or commands."""
from contextlib import contextmanager, nullcontext
import hashlib
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

import psutil

WINDOWS = os.name == 'nt'
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


_SECURITY_STAGES = frozenset({'open_token', 'read_token_user', 'read_token_owner',
    'close_token', 'close_directory', 'read_principals', 'build_dacl',
    'open_directory', 'read_directory', 'read_security',
    'set_owner', 'set_dacl', 'verify_security', 'create_directory', 'publish_endpoint'})
_SECURITY_BOOLEANS = frozenset({'owner_matches_user', 'owner_matches_default',
    'private_acl', 'user_full_access', 'bootstrap_recovery'})


class ChannelSecurityError(ValueError):
    def __init__(self, reason, facts):
        super().__init__(reason)
        # Only fixed vocabulary and numeric Win32 codes cross the ZIP boundary.
        self.security_facts = {k: v for k, v in facts.items()
            if (k in _SECURITY_BOOLEANS and type(v) is bool)
            or (k == 'phase' and v in ('initialize', 'validate'))
            or (k == 'stage' and v in _SECURITY_STAGES)
            or (k == 'windows_error_code' and type(v) is int and 0 <= v <= 0xffffffff)}


def _security_call(facts, stage, operation, *args):
    try:
        return operation(*args)
    except ChannelSecurityError:
        raise
    except Exception as exc:
        code = getattr(exc, 'winerror', None)
        if type(code) is not int and exc.args:
            code = exc.args[0]
        raise ChannelSecurityError('channel_security_' + stage + '_failed',
            {**facts, 'stage': stage, 'windows_error_code': code}) from None


class _WindowsPins:
    """Pin every path component without following reparses or sharing deletion.

    Held through endpoint publication. Security writes use the opened object,
    never a name. Denying write sharing also rejects existing directory writers;
    it is NOT a substitute for checking the DACL of the directory and its children.
    """
    def __init__(self):
        self.handles = {}
        self.stamps = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        failure = None
        for handle in reversed(list(self.handles.values())):
            try:_security_call({}, 'close_directory', handle.Close)
            except ChannelSecurityError as exc:failure = failure or exc
        if failure is not None and exc_type is None:raise failure

    def pin(self, path, facts, *, modify=False):
        import win32con, win32file
        path = Path(path).absolute()
        if path in self.handles:
            return self.handles[path]
        if path.parent != path:
            self.pin(path.parent, facts)
        access = win32con.FILE_READ_ATTRIBUTES | win32con.READ_CONTROL
        if modify:
            access |= win32con.WRITE_DAC | win32con.WRITE_OWNER
        handle = _security_call(facts, 'open_directory', win32file.CreateFile,
            str(path), access, win32con.FILE_SHARE_READ, None, win32con.OPEN_EXISTING,
            win32con.FILE_FLAG_BACKUP_SEMANTICS | win32con.FILE_FLAG_OPEN_REPARSE_POINT, None)
        self.handles[path] = handle
        info = _security_call(facts, 'read_directory', win32file.GetFileInformationByHandle, handle)
        if info[0] & win32con.FILE_ATTRIBUTE_REPARSE_POINT or not info[0] & win32con.FILE_ATTRIBUTE_DIRECTORY:
            raise ChannelSecurityError('channel_link_boundary', facts)
        self.stamps[path] = _security_call(facts, 'read_directory', path.lstat)
        self.check(facts)
        return handle

    def check(self, facts):
        # The no-delete/no-write handles provide the Windows exclusion. This
        # identity check additionally fails closed on unsupported filesystem behavior.
        for path, before in self.stamps.items():
            after = _security_call(facts, 'read_directory', path.lstat)
            if (stat.S_ISLNK(after.st_mode) or getattr(after, 'st_file_attributes', 0) & 0x400
                    or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
                raise ChannelSecurityError('channel_record_changed', facts)


def _empty_bootstrap(folder):
    """Only an interrupted, empty diagnostic namespace can be reinitialized."""
    entries = list(islice(folder.iterdir(), 2))
    if not entries:
        return True
    if folder.name != 'memory-capture' or len(entries) != 1 or entries[0].name != 'requests':
        return False
    info = entries[0].lstat()
    return (stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and not getattr(info, 'st_file_attributes', 0) & 0x400
            and next(entries[0].iterdir(), None) is None)


def windows_security(folder, *, initialize=False, pins=None):
    """One owner/DACL policy for initialization and read-only request validation."""
    import win32api, win32con, win32security
    facts = {'phase': 'initialize' if initialize else 'validate'}
    if pins is None:
        with _WindowsPins() as owned:
            return windows_security(folder, initialize=initialize, pins=owned)
    handle = pins.pin(folder, facts, modify=initialize)
    token = _security_call(facts, 'open_token', win32security.OpenProcessToken,
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = _security_call(facts, 'read_token_user', win32security.GetTokenInformation,
            token, win32security.TokenUser)[0]
        default_owner = _security_call(facts, 'read_token_owner', win32security.GetTokenInformation,
            token, win32security.TokenOwner)
    finally:
        _security_call(facts, 'close_token', token.Close)
    system = _security_call(facts, 'read_principals', win32security.CreateWellKnownSid,
        win32security.WinLocalSystemSid, None)
    admins = _security_call(facts, 'read_principals', win32security.CreateWellKnownSid,
        win32security.WinBuiltinAdministratorsSid, None)
    def read_security():
        descriptor = win32security.GetSecurityInfo(handle,
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
        owner = descriptor.GetSecurityDescriptorOwner()
        acl = descriptor.GetSecurityDescriptorDacl()
        private_acl, bootstrap_acl, user_access = acl is not None, acl is not None, False
        if acl is not None:
            for index in range(acl.GetAceCount()):
                ace = acl.GetAce(index)
                allowed = ace[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE
                if not allowed or ace[-1] not in (sid, system): private_acl = False
                if not allowed or ace[-1] not in (sid, system, admins): bootstrap_acl = False
                if (allowed and ace[-1] == sid and not ace[0][1] & win32con.INHERIT_ONLY_ACE
                        and ace[1] & win32con.FILE_ALL_ACCESS == win32con.FILE_ALL_ACCESS):
                    user_access = True
        facts.update(owner_matches_user=owner == sid, owner_matches_default=owner == default_owner,
            private_acl=private_acl, user_full_access=user_access)
        return owner, private_acl, bootstrap_acl, user_access
    def inspect(stage):
        return _security_call(facts, stage, read_security)
    owner, private, bootstrap_acl, user_access = inspect('read_security')
    if not initialize:
        if owner != sid: raise ChannelSecurityError('channel_owner_mismatch', facts)
        if not private or not user_access: raise ChannelSecurityError('channel_permissions_invalid', facts)
        return False
    if owner != sid and not (owner == default_owner == admins):
        raise ChannelSecurityError('channel_owner_mismatch', facts)
    recovery = not (private and user_access)
    facts['bootstrap_recovery'] = recovery
    if recovery:
        # Narrower than the previous empty-directory exception: arbitrary other
        # accounts / Everyone are never admitted, even for an empty directory.
        # Administrators and SYSTEM are privileged OS principals, not new users.
        if not bootstrap_acl or not user_access or not _empty_bootstrap(Path(folder)):
            raise ChannelSecurityError('channel_permissions_invalid', facts)
        child = Path(folder) / 'requests'
        if child.exists():
            windows_security(child, initialize=True, pins=pins)
    pins.check(facts)
    def build_acl():
        acl = win32security.ACL()
        for principal in (sid, system):
            # No inheritable ACE: never propagate a repair to old records.
            acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, 0, win32con.FILE_ALL_ACCESS, principal)
        return acl
    acl = _security_call(facts, 'build_dacl', build_acl)
    _security_call(facts, 'set_dacl', win32security.SetSecurityInfo, handle, win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None)
    _security_call(facts, 'set_owner', win32security.SetSecurityInfo, handle, win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION, sid, None, None, None)
    owner, private, _, user_access = inspect('verify_security')
    if owner != sid: raise ChannelSecurityError('channel_owner_mismatch', {**facts, 'stage': 'verify_security'})
    if not private or not user_access:
        raise ChannelSecurityError('channel_permissions_invalid', {**facts, 'stage': 'verify_security'})
    pins.check(facts)
    if recovery and not _empty_bootstrap(Path(folder)):
        raise ChannelSecurityError('channel_bootstrap_changed', facts)
    return recovery


def restrict(folder, *, pins=None):
    """Only this user and SYSTEM; no inherited cross-account writes."""
    if WINDOWS:
        return windows_security(folder, initialize=True, pins=pins)
    folder.chmod(0o700)
    return False


class Channel:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.folder = self.root / 'runtime' / 'memory-capture'
        self.requests = self.folder / 'requests'

    @contextmanager
    def initialization(self):
        # Keep pinned Windows ancestors/objects alive until Coordinator publishes.
        with (_WindowsPins() if WINDOWS else nullcontext(None)) as pins:
            self._bootstrap_recovery = False
            self._pins = pins
            try:
                safe(self.requests, self.root)
                for folder in (self.folder, self.requests):
                    if pins:
                        # Only runtime may need creation outside the channel.
                        pins.pin(self.root, {'phase': 'initialize'})
                        _security_call({'phase': 'initialize'}, 'create_directory',
                            lambda: self.root.joinpath('runtime').mkdir(exist_ok=True))
                        pins.pin(folder.parent, {'phase': 'initialize'})
                    def create_folder():
                        folder.mkdir(parents=True, exist_ok=True)
                    if pins:_security_call({'phase': 'initialize'}, 'create_directory', create_folder)
                    else:create_folder()
                    safe(folder, self.root)
                    self._bootstrap_recovery |= bool(restrict(folder, pins=pins))
                yield
            finally:
                self._pins = None

    def initialize(self):
        with self.initialization():
            pass

    def publish(self, value):
        facts = {'phase': 'initialize'}
        if self._pins: self._pins.check(facts)
        if self._bootstrap_recovery and not _empty_bootstrap(self.folder):
            raise ChannelSecurityError('channel_bootstrap_changed', facts)
        previous_path = self.folder / 'endpoint.json'
        if previous_path.exists():
            # A failed bootstrap may already have normalized the ACL. On retry
            # that alone must not turn an arriving foreign record into our endpoint.
            try:previous = self.read(previous_path)
            except ValueError:
                raise ChannelSecurityError('channel_unexpected_file', facts) from None
            target = previous.get('target')
            if (set(previous) != {'schema', 'operation', 'target'}
                    or previous.get('schema') != SCHEMA or previous.get('operation') != 'memory_capture'
                    or not isinstance(target, dict)
                    or set(target) != {'pid', 'created', 'user_hash', 'runtime_session'}
                    or type(target.get('pid')) is not int or target['pid'] <= 0
                    or type(target.get('created')) not in (int, float) or not math.isfinite(target['created'])
                    or not isinstance(target.get('runtime_session'), str) or not target['runtime_session']
                    or target.get('user_hash') != value['target']['user_hash']):
                raise ChannelSecurityError('channel_unexpected_file', facts)
        if WINDOWS:
            _security_call(facts, 'publish_endpoint', self.write,
                self.folder / 'endpoint.json', value, self._bootstrap_recovery)
        else:
            self.write(self.folder / 'endpoint.json', value, self._bootstrap_recovery)

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

    def write(self, path, value, exclusive=False):
        path = safe(path, self.root)
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
        if len(raw) > MAX_BYTES:raise ValueError('channel_record_budget')
        temporary = safe(path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp'), self.root)
        try:
            with temporary.open('xb') as handle:
                if os.name != 'nt':os.fchmod(handle.fileno(), 0o600)
                handle.write(raw)
            safe(path, self.root)
            if exclusive:
                # Atomic no-replace publication: a record arriving after the
                # last emptiness check must never be truncated or overwritten.
                try:os.link(temporary, path)
                except FileExistsError:
                    raise ChannelSecurityError('channel_bootstrap_changed', {'phase': 'initialize'}) from None
            else:
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
            if WINDOWS:windows_security(folder)
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
             'channel_bootstrap_changed', 'channel_record_budget', 'channel_record_changed', 'channel_record_shape',
             'channel_directory_budget', 'channel_permissions_invalid', 'channel_unexpected_file', 'worker_identity_unavailable',
             'worker_identity_changed', 'request_path_identity_mismatch', 'capture_identity_mismatch',
             'response_identity_changed', 'response_status_invalid'}
    known |= {'channel_security_' + stage + '_failed' for stage in _SECURITY_STAGES}
    return str(exc) if isinstance(exc, ValueError) and str(exc) in known else type(exc).__name__
