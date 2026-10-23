"""Bounded housekeeping of disposable copies, never business/recovery storage.

No SQLite or transactions are opened here. Existing task/Hook retention remains
owned by those modules. Update payloads need the coordinator's settlement marker.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time

DAY = 86400
INTERVAL = 3600
EXPORT_DAYS = 14
EXPORT_BYTES = 512 * 1024 * 1024
UPDATE_DAYS = 30
UPDATE_BYTES = 1024 * 1024 * 1024
LOG_BYTES = 8 * 1024 * 1024
LOW_DISK_BYTES = 2 * 1024 * 1024 * 1024
MARKER = 'cleanup-ready.json'
EXPORT_NAME = re.compile(r'车金故障证据-\d{8}-\d{6}-\d{6}\.zip')
LOG_NAMES = {'ui-action-audit.jsonl', 'startup-crash.jsonl', 'evidence-recorder-failures.jsonl'}
LOG_NAMES |= {str(Path(n).with_suffix('.previous.jsonl')) for n in LOG_NAMES}
REQUEST_NAME = re.compile(r'update-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}')


def safe_path(path: Path, root: Path) -> bool:
    """Reject links, junctions, hard-linked files and traversal, including parents."""
    path, root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    if not path.is_relative_to(root):
        return False
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            return False
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            return False
    return True


def _json(path):
    if path.stat().st_size > 256 * 1024:
        raise ValueError('HOUSEKEEPING_METADATA_TOO_LARGE')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('HOUSEKEEPING_METADATA_INVALID')
    return value


def _write(path, value):
    temporary = path.with_suffix('.tmp')
    if not safe_path(path, path.parent) or not safe_path(temporary, path.parent):
        raise ValueError('HOUSEKEEPING_UNSAFE_PATH')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(value, ensure_ascii=True), encoding='utf-8')
    os.replace(temporary, path)


def settled_state(state):
    if state.get('data_integrity_failed') or state.get('status_restore_pending'):
        return False
    return ((state.get('state') in {'succeeded', 'rolled_back'}
             and state.get('result_reconciled') is True)
            or (state.get('state') == 'failed' and state.get('install_started') is False))


def mark_update_settled(root: Path, request_id: str, data_dir: Path):
    """Only the coordinator calls this after terminal reconciliation/payload cleanup."""
    if not REQUEST_NAME.fullmatch(request_id):
        return
    folder = Path(root) / 'requests' / request_id
    if folder.is_dir() and safe_path(folder, Path(root)):
        _write(folder / MARKER, {'schema_version': 1, 'request_id': request_id,
            'data_dir': str(Path(data_dir).resolve()), 'settled_at': time.time()})


def append_bounded_log(path: Path, text: str, limit=256 * 1024):
    """Stdlib-only early-startup writer; no imports of runtime/business services."""
    path = Path(path)
    if not safe_path(path, path.parent):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = text.encode('utf-8')
    if len(encoded) > limit:
        return
    previous = path.with_suffix('.previous' + path.suffix)
    if not safe_path(previous, path.parent):
        return
    if path.exists() and path.stat().st_size + len(encoded) > limit:
        os.replace(path, previous)
    with path.open('ab') as stream:
        stream.write(encoded)


class Sweep:
    def __init__(self, root, store, *, now=None, seconds=2):
        self.root, self.store = Path(root), store
        self.now = time.time() if now is None else now
        self.deadline = time.monotonic() + seconds
        self.visited = 0
        self.report = {'removed_files': 0, 'removed_bytes': 0, 'errors': 0,
                       'unsettled_update_requests': 0, 'budget_exhausted': False}

    def budget(self):
        self.visited += 1
        if self.visited > 5000 or time.monotonic() > self.deadline:
            self.report['budget_exhausted'] = True
            raise TimeoutError('HOUSEKEEPING_BUDGET')

    def files(self, folder, boundary):
        """Return a whole validated tree, or skip it; never partially classify."""
        self.budget()
        if not safe_path(folder, boundary):
            raise ValueError('HOUSEKEEPING_UNSAFE_PATH')
        entries = []
        for p in folder.iterdir():
            self.budget()
            if not safe_path(p, boundary):
                raise ValueError('HOUSEKEEPING_UNSAFE_PATH')
            if p.is_dir():
                entries.extend(self.files(p, boundary))
            elif p.is_file():
                entries.append(p)
            else:
                raise ValueError('HOUSEKEEPING_UNKNOWN_FILE')
        return entries

    def remove_file(self, path, boundary):
        self.budget()
        if not safe_path(path, boundary):
            raise ValueError('HOUSEKEEPING_UNSAFE_PATH')
        size = path.stat().st_size
        path.unlink()
        self.report['removed_files'] += 1
        self.report['removed_bytes'] += size

    def exports(self):
        folder = self.root / 'artifacts' / 'exports'
        if not folder.is_dir() or not safe_path(folder, self.root):
            return
        candidates = []
        for p in folder.iterdir():
            self.budget()
            if EXPORT_NAME.fullmatch(p.name) and p.is_file() and safe_path(p, self.root):
                info = p.stat()
                candidates.append((info.st_mtime, info.st_size, p))
        candidates.sort(reverse=True)
        total = sum(size for _, size, _ in candidates)
        # Always keep the latest three and all exports younger than one day.
        for modified, size, p in reversed(candidates[3:]):
            if self.now - modified < DAY:
                continue
            if self.now - modified >= EXPORT_DAYS * DAY or total > EXPORT_BYTES:
                self.remove_file(p, self.root)
                total -= size

    def logs(self):
        for folder in (self.root/'artifacts'/'runtime', self.root/'diagnostics'):
            if not folder.is_dir() or not safe_path(folder, self.root):
                continue
            for p in folder.iterdir():
                self.budget()
                if p.name not in LOG_NAMES or not p.is_file() or not safe_path(p, self.root):
                    continue
                info = p.stat()
                if self.now-info.st_mtime > 30*DAY:
                    self.remove_file(p, self.root)
                elif info.st_size > LOG_BYTES and '.previous.' not in p.name:
                    previous = p.with_suffix('.previous'+p.suffix)
                    if safe_path(previous, self.root):
                        os.replace(p, previous)
                elif '.previous.' in p.name and info.st_size > LOG_BYTES:
                    # An oversized pre-policy log is retired after a one-day grace.
                    if self.now-info.st_mtime >= DAY:
                        self.remove_file(p, self.root)

    def updates(self, state):
        root = Path(self.store.root)
        # Custom update roots cannot overlap any business directory.
        if root.resolve().is_relative_to(self.root.resolve()) or self.root.resolve().is_relative_to(root.resolve()):
            return
        requests = root / 'requests'
        if not requests.is_dir() or not safe_path(requests, root):
            return
        candidates = []
        for folder in requests.iterdir():
            self.budget()
            if not REQUEST_NAME.fullmatch(folder.name) or folder.name == state.get('update_request_id'):
                continue
            if not folder.is_dir() or not safe_path(folder, root):
                continue
            marker = folder / MARKER
            if not marker.is_file() or not safe_path(marker, root):
                self.report['unsettled_update_requests'] += 1
                continue
            value = _json(marker)
            if (value.get('schema_version') != 1 or value.get('request_id') != folder.name
                    or value.get('data_dir') != str(self.root.resolve())):
                continue
            settled = value.get('settled_at')
            if type(settled) not in (int, float) or not 0 < settled <= self.now:
                continue
            files = self.files(folder, root)
            # A recently touched directory is not treated as abandoned.
            modified = max([settled, folder.stat().st_mtime, *[p.stat().st_mtime for p in files]])
            candidates.append((modified, sum(p.stat().st_size for p in files), folder))
        total = sum(size for _, size, _ in candidates)
        for modified, size, folder in sorted(candidates):
            if self.now-modified < 7*DAY:
                continue
            if self.now-modified >= UPDATE_DAYS*DAY or total > UPDATE_BYTES:
                # Recheck links immediately before deleting only this settled request.
                self.files(folder, root)
                if not safe_path(folder, root):
                    continue
                shutil.rmtree(folder)
                self.report['removed_bytes'] += size
                self.report['removed_files'] += 1
                total -= size

    def run(self):
        # Same instance lock as begin/save: no sweep races a new update request.
        with self.store._lock:
            state = self.store.load()
            if (state.get('data_integrity_failed') or state.get('status_restore_pending')
                    or (state.get('state') != 'idle' and not settled_state(state))):
                return {**self.report, 'skipped': 'update_in_progress_or_unreconciled'}
            if not safe_path(self.root, self.root):
                return {**self.report, 'skipped': 'unsafe_data_path'}
            for action in (self.exports, self.logs, lambda: self.updates(state)):
                try:
                    action()
                except TimeoutError:
                    break
                except (OSError, ValueError, TypeError):
                    self.report['errors'] += 1
            volumes = [self.root, Path(self.store.root)]
            free = min(shutil.disk_usage(p).free for p in volumes if p.exists())
            self.report.update(free_bytes=free, low_disk=free < LOW_DISK_BYTES,
                               observed_at=self.now)
            try:
                _write(self.root / 'storage-maintenance.json', self.report)
            except (OSError, ValueError):
                # Full/read-only disks must still produce the low-space UI warning.
                self.report['errors'] += 1
                self.report['audit_write_failed'] = True
            return self.report


class StorageMaintenance:
    """One non-UI thread, independent of binding, pause, fault and network."""
    def __init__(self, root, store, warning=lambda message: None):
        self.root, self.store, self.warning = Path(root), store, warning
        self.stopped = threading.Event()
        self.thread = None
        self.warned = False

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._loop, name='CheJinStorageMaintenance', daemon=True)
        try:
            self.thread.start()
        except (RuntimeError, OSError):
            # A housekeeping resource failure cannot prevent opening/closing Worker.
            self.thread = None
            self.warning('本机自动清理暂时无法启动，请联系运维检查；业务记录未改动。')

    def _loop(self):
        while not self.stopped.is_set():
            try:
                report = Sweep(self.root, self.store).run()
                if report.get('low_disk') and not self.warned:
                    self.warned = True
                    self.warning('车金数据或更新缓存所在磁盘剩余空间不足 2GB。已尝试清理可删除缓存；请联系管理员腾出空间，不要删除绑定和业务记录。')
                if report.get('low_disk') is False:
                    self.warned = False
            except Exception:
                # Never prevent startup, change business status or clear recovery state.
                pass
            self.stopped.wait(INTERVAL)

    def stop(self, timeout_seconds=3):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=max(0, timeout_seconds))
        return self.thread is None or not self.thread.is_alive()
