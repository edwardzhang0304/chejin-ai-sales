"""Opt-in, time/volume limited Python allocation evidence; no object contents.

Not a heap-owner graph and not native Qt/DLL allocation attribution. Sampling
limits are cooperative: tracing overhead can exceed a threshold between checks.
"""
import hashlib
from itertools import islice
import json
import os
import sys
from pathlib import Path
import threading
import time
import tracemalloc
import uuid

MAX_SECONDS = 120
INTERVAL_SECONDS = 10
MAX_METADATA_BYTES = 16 * 1024 * 1024
# Diagnostic-only cooperative budget, authorized 2026-11-04. Business deadlines
# and the 30-second default observation window are independent and unchanged.
MAX_CAPTURE_SECONDS = .5
MAX_TRACES = 50_000
MAX_SITES = 5000
MAX_ROWS = 100
MAX_REPORT_BYTES = 1024 * 1024
MAX_FOLDER_BYTES = 8 * MAX_REPORT_BYTES


# These CPython layouts are verified against their stdlib implementation and
# actual snapshots. Unknown runtimes keep the bounded public API path.
_RAW_LAYOUTS = {(3, 12, 10), (3, 13, 2)}
_MISSING = object()


def _deadline(started):
    if time.monotonic() - started > MAX_CAPTURE_SECONDS:
        raise ValueError('profile_capture_time_budget')


def _allocation_rows(snapshot, attempt):
    """Borrow this snapshot only; normalize both APIs to newest-first frames.

    Missing/unsupported internal API falls back BEFORE enumeration. Malformed
    data fails explicitly, rather than restarting/counting allocations twice.
    Neither this adapter nor its caller retains raw records between samples.
    """
    traces = snapshot.traces
    attempt['trace_count'] = len(traces)
    if len(traces) > MAX_TRACES:
        raise ValueError('profile_trace_budget')
    raw = _MISSING
    if sys.implementation.name == 'cpython' and sys.version_info[:3] in _RAW_LAYOUTS:
        raw = getattr(traces, '_traces', _MISSING)
    attempt['enumeration'] = 'public' if raw is _MISSING else 'cpython_raw'
    if raw is not _MISSING and (type(raw) not in (list, tuple) or len(raw) != len(traces)):
        raise ValueError('profile_trace_format')
    if raw is not _MISSING:
        return raw

    def public_rows():
        for row in traces:
            yield (row.domain, row.size,
                   tuple((f.filename, f.lineno) for f in reversed(row.traceback)))
    return public_rows()


def _aggregate(snapshot, attempt, started):
    groups = {}
    # CPython shares immutable frame tuples. This snapshot-local cache shares
    # bounded allocation-size references, not copies of raw traces or snapshots. Both input
    # adapters use this validation/grouping path. Raw rows remain owned by the
    # snapshot throughout iteration, so cached frame identities cannot be reused.
    validated = {}
    # Total size references are capped by MAX_TRACES; only the current chunk
    # (256 borrowed rows) is materialized. Summation runs once per site.
    try:
        rows = iter(_allocation_rows(snapshot, attempt))
        processed = 0
        while chunk := tuple(islice(rows, 256)):
            _deadline(started)
            attempt['processed_traces'] = processed
            for row in chunk:
                if (type(row) is not tuple or len(row) not in (3, 4)
                        or type(row[0]) is not int or row[0] < 0
                        or type(row[1]) is not int or row[1] < 0
                        or type(row[2]) is not tuple or not 1 <= len(row[2]) <= 5
                        or (len(row) == 4 and (type(row[3]) is not int or row[3] < len(row[2])))):
                    raise ValueError('profile_trace_format')
                size, frames = row[1], row[2]
                identity = id(frames)
                cached = validated.get(identity)
                if cached is not None and cached[0] is frames:
                    counts = cached[1]
                else:
                    for frame in frames:
                        if (type(frame) is not tuple or len(frame) != 2
                                or type(frame[0]) is not str or type(frame[1]) is not int
                                or frame[1] < 0):
                            raise ValueError('profile_trace_format')
                    counts = groups.get(frames)
                    if counts is None:
                        if len(groups) >= MAX_SITES:
                            raise ValueError('profile_site_budget')
                        counts = groups[frames] = []
                    if len(validated) < MAX_SITES:
                        validated[identity] = (frames, counts)
                counts.append(size)
            processed += len(chunk)
        attempt['processed_traces'] = attempt['trace_count']
        sites = {}
        for index, (frames, counts) in enumerate(groups.items()):
            if index % 32 == 0:
                _deadline(started)
            if any(filename == __file__ for filename, _ in frames):
                continue
            frames = tuple(reversed(frames))
            key = hashlib.sha256(repr(frames).encode()).hexdigest()
            sites[key] = {'bytes': sum(counts), 'blocks': len(counts),
                'frames': [{'module': f.replace('\\', '/').rsplit('/', 1)[-1][:100],
                            'line': n} for f, n in frames]}
        _deadline(started)
        return sites
    finally:
        attempt['grouped_sites'] = len(groups)
        validated.clear()
        groups.clear()


def _write_record(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=True))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_current(root, state):
    _write_record(Path(root) / 'artifacts/runtime/python-memory-status.json', state)


def _write_gap(root, state):
    _write_record(Path(root) / 'artifacts/runtime/python-memory-gap.json', state)


def _identity(capture_id, enabled):
    return {'schema': 1, 'evidence_kind': 'python_allocations',
            'capture_id': capture_id, 'enabled': enabled, 'pid': os.getpid(),
            'started_wall': time.time(), 'started_monotonic': time.monotonic()}


class Profile:
    def __init__(self, root, seconds, *, context=None, pressure=None):
        if type(seconds) is not int or not 1 <= seconds <= MAX_SECONDS:
            raise ValueError('PYTHON_MEMORY_PROFILE_DURATION_INVALID')
        self.pressure = pressure
        self.app_root = Path(root)
        self.root = Path(root) / 'artifacts' / 'runtime' / 'python-memory'
        self.path = self.root / (uuid.uuid4().hex + '.json')
        self.stop = threading.Event()
        self.thread = None
        self.owned = False
        self.report_allowed = False
        self.baseline = self.previous = None
        self.identity = _identity(self.path.stem, True)
        self.identity.update({k: v for k, v in (context or {}).items() if k in ('request_id', 'runtime_session', 'created')})
        self.started = self.identity['started_monotonic']
        self.last_status = None
        self.data = {**self.identity, 'record_role': 'report', 'duration_limit_seconds': seconds,
            'interval_seconds': INTERVAL_SECONDS, 'trace_frames': 5,
            'status': 'starting', 'samples': [], 'omitted_samples': 0,
            'limits': {'metadata_bytes': MAX_METADATA_BYTES, 'traces': MAX_TRACES,
                       'sites': MAX_SITES, 'rows_per_sample': MAX_ROWS,
                       'capture_seconds': MAX_CAPTURE_SECONDS,
                       'report_bytes': MAX_REPORT_BYTES, 'folder_bytes': MAX_FOLDER_BYTES},
            'limits_are_cooperative': True,
            'blind_spots': ['allocations_before_start', 'untracked_native_allocations',
                           'other_processes', 'object_owners_and_reference_graphs'],
            'object_contents_collected': False}

    def status(self, status, reason=None):
        if self.last_status == (status, reason):
            return
        _write_current(self.app_root, {**self.identity, 'record_role': 'current_status',
            'status': status, 'reason': reason, 'updated_wall': time.time(),
            'report_file': self.path.name})
        self.last_status = (status, reason)

    def _write(self):
        raw = json.dumps(self.data, ensure_ascii=True).encode()
        while len(raw) > MAX_REPORT_BYTES and len(self.data['samples']) > 1:
            self.data['samples'].pop(1 if len(self.data['samples']) > 2 else 0)
            self.data['omitted_samples'] += 1
            raw = json.dumps(self.data, ensure_ascii=True).encode()
        if len(raw) > MAX_REPORT_BYTES:
            raise ValueError('profile_report_budget')
        temporary = self.path.with_suffix('.tmp')
        try:
            temporary.write_bytes(raw)
            os.replace(temporary, self.path)
            self.status(self.data['status'], self.data.get('capture_failed', self.data.get('reason', self.data.get('end_reason'))))
        finally:
            temporary.unlink(missing_ok=True)

    def begin(self):
        self.check_pressure()
        self.status('starting')
        self.root.mkdir(parents=True, exist_ok=True)
        size = 0
        for count, path in enumerate(self.root.iterdir()):
            if count >= 128 or path.is_symlink():
                raise ValueError('profile_directory_boundary')
            if path.is_file():
                size += path.stat().st_size
        if size + 2 * MAX_REPORT_BYTES > MAX_FOLDER_BYTES:
            raise ValueError('profile_folder_budget')
        self.report_allowed = True
        if tracemalloc.is_tracing():
            self.data.update(status='not_started', reason='another_tracer_active')
            self._write()
            return False
        self._write()  # An interrupted start leaves an explicit unfinished file.
        tracemalloc.start(5)
        self.owned = True
        self.data['status'] = 'running'
        self.sample()
        return True

    def check_pressure(self):
        if self.pressure:
            pressure = self.pressure()
            if pressure:
                self.data['pressure'] = pressure
                raise ValueError('profile_business_pressure')

    def sample(self):
        if not self.owned:
            return
        if not tracemalloc.is_tracing():
            raise ValueError('tracer_stopped_externally')
        if len(self.data['samples']) >= 14:
            raise ValueError('profile_sample_budget')
        self.check_pressure()
        metadata = tracemalloc.get_tracemalloc_memory()
        if metadata > MAX_METADATA_BYTES:
            raise ValueError('profile_metadata_budget')
        started = time.monotonic()
        cpu_started = time.thread_time()
        attempt = self.data['last_attempt'] = {
            'started_monotonic': started, 'tracer_metadata_bytes': metadata,
            'trace_count': None, 'processed_traces': 0, 'site_count': 0}
        current, peak = tracemalloc.get_traced_memory()
        snapshot = None
        sample = None
        try:
            snapshot = tracemalloc.take_snapshot()
            attempt['snapshot_ms'] = round((time.monotonic() - started) * 1000, 3)
            aggregate_started = time.monotonic()
            try:
                sites = _aggregate(snapshot, attempt, started)
            finally:
                attempt['aggregate_ms'] = round((time.monotonic() - aggregate_started) * 1000, 3)
            attempt['site_count'] = len(sites)
            del snapshot
            snapshot = None
            if self.baseline is None:
                self.baseline = sites
            prior = self.previous or self.baseline
            candidates = []
            for index, key in enumerate(sites.keys() | self.baseline.keys() | prior.keys()):
                if index % 32 == 0:
                    _deadline(started)
                now = sites.get(key, {})
                base = self.baseline.get(key, {})
                old = prior.get(key, {})
                candidates.append({'site': key, 'frames': (now or base or old)['frames'],
                    'bytes': now.get('bytes', 0), 'blocks': now.get('blocks', 0),
                    'baseline_delta_bytes': now.get('bytes', 0) - base.get('bytes', 0),
                    'previous_delta_bytes': now.get('bytes', 0) - old.get('bytes', 0)})
            candidates.sort(key=lambda r: abs(r['baseline_delta_bytes']), reverse=True)
            sample = {'monotonic': started, 'wall_time': time.time(),
                'traced_current_bytes_including_observer': current,
                'traced_peak_bytes_including_observer': peak,
                'tracer_metadata_bytes': metadata,
                'capture_ms': round((time.monotonic() - started) * 1000, 3),
                'sites': candidates[:MAX_ROWS], 'total_sites': len(candidates),
                'omitted_sites': max(0, len(candidates) - MAX_ROWS)}
            self.data['samples'].append(sample)
            self.previous = sites
            candidates.clear()
            _deadline(started)
            write_started = time.monotonic()
            try:
                self._write()
            finally:
                attempt['report_write_ms'] = round((time.monotonic() - write_started) * 1000, 3)
        finally:
            del snapshot
            attempt['elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)
            attempt['thread_cpu_ms'] = round((time.thread_time() - cpu_started) * 1000, 3)
            if sample is not None:
                sample['total_observer_ms'] = attempt['elapsed_ms']
                sample['report_write_ms'] = attempt.get('report_write_ms')
        # Includes snapshot, normalization, diff/sort, release and report I/O.
        # Cooperative only: an individual blocking call can overrun the limit.
        _deadline(started)

    def finish(self, reason='duration_complete'):
        if self.owned:
            tracemalloc.stop()
            self.owned = False
        self.baseline = self.previous = None
        if not self.report_allowed:
            self.gap(reason)
            return
        if self.data['status'] != 'not_started':
            self.data.update(status='capture_failed' if 'capture_failed' in self.data else 'finished', end_reason=reason,
                             ended_monotonic=time.monotonic(), ended_wall=time.time())
            self._write()

    def _run(self):
        reason = 'duration_complete'
        try:
            if not self.begin():
                return
            deadline = self.started + self.data['duration_limit_seconds']
            next_sample = time.monotonic() + INTERVAL_SECONDS
            while not self.stop.wait(min(1., max(0, deadline - time.monotonic()))):
                self.check_pressure()
                if tracemalloc.get_tracemalloc_memory() > MAX_METADATA_BYTES:
                    raise ValueError('profile_metadata_budget')
                now = time.monotonic()
                if now >= next_sample or now >= deadline:
                    self.sample()
                    next_sample = now + INTERVAL_SECONDS
                if now >= deadline:
                    break
            if self.stop.is_set():
                reason = 'worker_exit'
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(('profile_', 'tracer_')) else type(exc).__name__
            self.data['capture_failed'] = reason
        finally:
            try:
                self.finish(reason)
            except Exception as exc:
                self.gap(type(exc).__name__)

    def gap(self, reason):
        state = {**self.identity, 'record_role': 'gap', 'status': 'capture_failed',
                 'reason': reason, 'first_failure': self.data.get('capture_failed', reason),
                 'last_attempt': self.data.get('last_attempt'),
                 'pressure': self.data.get('pressure'), 'at': time.time(), 'retention': 'latest_failure_only'}
        try:
            _write_gap(self.app_root, state)
        except Exception:
            pass
        try:
            self.status('capture_failed', reason)
        except Exception:
            pass

    def start(self):
        self.status('starting')
        self.thread = threading.Thread(target=self._run, name='python-memory-profile', daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=.5)  # Never keep Worker exit waiting on tracing.


def start_profile(root, seconds):
    if not seconds:
        # Each normal launch explicitly supersedes old capture state. It does
        # not start a tracer, touch old reports or infer that memory is healthy.
        identity = _identity(uuid.uuid4().hex, False)
        try:
            _write_current(root, {**identity, 'record_role': 'current_status',
                                 'status': 'disabled', 'reason': 'not_requested'})
        except OSError as exc:
            try:
                _write_gap(root, {**identity, 'record_role': 'gap', 'status': 'capture_failed',
                    'reason': 'disabled_status_write_failed', 'error_type': type(exc).__name__,
                    'retention': 'latest_failure_only'})
            except OSError:
                pass  # Complete storage loss cannot manufacture durable evidence.
        return None
    profile = Profile(root, seconds)
    try:
        return profile.start()
    except Exception as exc:
        profile.gap(type(exc).__name__)
        # Failure to start this optional observer does not affect normal startup.
        return None
