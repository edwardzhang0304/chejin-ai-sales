"""Opt-in, time/volume limited Python allocation evidence; no object contents.

Not a heap-owner graph and not native Qt/DLL allocation attribution. Sampling
limits are cooperative: tracing overhead can exceed a threshold between checks.
"""
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import tracemalloc
import uuid

MAX_SECONDS = 120
INTERVAL_SECONDS = 10
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_CAPTURE_SECONDS = .25
MAX_TRACES = 50_000
MAX_SITES = 5000
MAX_ROWS = 100
MAX_REPORT_BYTES = 1024 * 1024
MAX_FOLDER_BYTES = 8 * MAX_REPORT_BYTES


class Profile:
    def __init__(self, root, seconds):
        if type(seconds) is not int or not 1 <= seconds <= MAX_SECONDS:
            raise ValueError('PYTHON_MEMORY_PROFILE_DURATION_INVALID')
        self.root = Path(root) / 'artifacts' / 'runtime' / 'python-memory'
        self.path = self.root / (uuid.uuid4().hex + '.json')
        self.stop = threading.Event()
        self.thread = None
        self.owned = False
        self.report_allowed = False
        self.baseline = self.previous = None
        self.started = time.monotonic()
        self.data = {'schema': 1, 'evidence_kind': 'python_allocations', 'enabled': True, 'pid': os.getpid(), 'started_wall': time.time(),
            'started_monotonic': self.started, 'duration_limit_seconds': seconds,
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
        finally:
            temporary.unlink(missing_ok=True)

    def begin(self):
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

    def sample(self):
        if not self.owned:
            return
        if not tracemalloc.is_tracing():
            raise ValueError('tracer_stopped_externally')
        if len(self.data['samples']) >= 14:
            raise ValueError('profile_sample_budget')
        metadata = tracemalloc.get_tracemalloc_memory()
        if metadata > MAX_METADATA_BYTES:
            raise ValueError('profile_metadata_budget')
        started = time.monotonic()
        current, peak = tracemalloc.get_traced_memory()
        snapshot = tracemalloc.take_snapshot()
        try:
            if len(snapshot.traces) > MAX_TRACES:
                raise ValueError('profile_trace_budget')
            sites = {}
            for index, trace in enumerate(snapshot.traces):
                if index % 256 == 0 and time.monotonic() - started > MAX_CAPTURE_SECONDS:
                    raise ValueError('profile_capture_time_budget')
                # Exclude our own observer allocations; never retain snapshots
                # or live application objects between measurements.
                if any(frame.filename == __file__ for frame in trace.traceback):
                    continue
                frames = tuple((frame.filename, frame.lineno) for frame in trace.traceback)
                key = hashlib.sha256(repr(frames).encode()).hexdigest()
                if key not in sites:
                    if len(sites) >= MAX_SITES:
                        raise ValueError('profile_site_budget')
                    # Paths may contain user names. Only basename and source
                    # location survive; no source lines or locals are captured.
                    sites[key] = {'bytes': 0, 'blocks': 0,
                        'frames': [{'module': Path(f).name[:100], 'line': n} for f, n in frames]}
                sites[key]['bytes'] += trace.size
                sites[key]['blocks'] += 1
        finally:
            del snapshot
        if self.baseline is None:
            self.baseline = sites
        prior = self.previous or self.baseline
        candidates = []
        for key in sites.keys() | self.baseline.keys() | prior.keys():
            now = sites.get(key, {})
            base = self.baseline.get(key, {})
            old = prior.get(key, {})
            candidates.append({'site': key, 'frames': (now or base or old)['frames'],
                'bytes': now.get('bytes', 0), 'blocks': now.get('blocks', 0),
                'baseline_delta_bytes': now.get('bytes', 0) - base.get('bytes', 0),
                'previous_delta_bytes': now.get('bytes', 0) - old.get('bytes', 0)})
        candidates.sort(key=lambda r: abs(r['baseline_delta_bytes']), reverse=True)
        self.data['samples'].append({'monotonic': started, 'wall_time': time.time(),
            'traced_current_bytes_including_observer': current,
            'traced_peak_bytes_including_observer': peak,
            'tracer_metadata_bytes': metadata,
            'capture_ms': round((time.monotonic() - started) * 1000, 3),
            'sites': candidates[:MAX_ROWS], 'total_sites': len(candidates),
            'omitted_sites': max(0, len(candidates) - MAX_ROWS)})
        self.previous = sites
        self._write()
        self.data['samples'][-1]['total_observer_ms'] = round((time.monotonic() - started) * 1000, 3)

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
        try:
            self.root.parent.mkdir(parents=True, exist_ok=True)
            gap = self.root.parent / 'python-memory-gap.json'
            gap.write_text(json.dumps({'evidence_kind': 'python_allocations', 'enabled': True,
                                       'status': 'capture_failed', 'reason': reason,
                                       'at': time.time(), 'retention': 'latest_failure_only'}))
        except OSError:
            pass

    def start(self):
        self.thread = threading.Thread(target=self._run, name='python-memory-profile', daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=.5)  # Never keep Worker exit waiting on tracing.


def start_profile(root, seconds):
    if not seconds:
        return None
    profile = Profile(root, seconds)
    try:
        return profile.start()
    except Exception as exc:
        profile.gap(type(exc).__name__)
        # Failure to start this optional observer does not affect normal startup.
        return None
