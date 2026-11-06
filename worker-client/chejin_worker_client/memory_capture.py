"""One in-Worker coordinator plus a bounded diagnostic requester.

Neither side imports business APIs, modifies binding/status nor calls the DLL.
The Collector remains a read-only consumer of the resulting evidence.
"""
import os
from pathlib import Path
import threading
import time
import uuid

from . import memory_capture_channel as protocol
from .python_memory_profile import Profile, start_profile


class Coordinator:
    def __init__(self, root, performance=None):
        self.channel = protocol.Channel(root)
        self.performance = performance
        self.stop = threading.Event()
        self.thread = None
        self.profile = None
        self.started = -float('inf')
        self.target = None

    def pressure(self):
        return self.performance.capture_pressure() if self.performance else {'reason': 'load_unavailable'}

    def _begin(self, request_id, seconds):
        self.profile = Profile(self.channel.root, seconds, context={
            'request_id': request_id, 'runtime_session': self.target['runtime_session'],
            'created': self.target['created']}, pressure=self.pressure)
        self.started = time.monotonic()
        try:self.profile.start()
        except Exception as exc:self.profile.gap(type(exc).__name__)

    def start(self, engineering_seconds=0):
        with self.channel.initialization():
            marker = self.channel.read(self.channel.root / 'runtime/session.json')
            self.target = {**protocol.identity(), 'runtime_session': marker.get('session_id')}
            if marker.get('pid') != os.getpid() or marker.get('status') != 'running' or not self.target['runtime_session']:
                raise ValueError('runtime_session_unavailable')
            self.channel.publish({
                'schema': protocol.SCHEMA, 'operation': 'memory_capture', 'target': self.target})
        start_profile(self.channel.root, 0)  # unified explicit disabled marker, no tracer
        if engineering_seconds:self._begin(uuid.uuid4().hex, engineering_seconds)
        self.thread = threading.Thread(target=self._run, name='memory-capture-coordinator', daemon=True)
        self.thread.start()
        return self

    def _reply(self, record):
        profile = self.profile
        capture = record.get('capture_id') or profile.identity['capture_id']
        record['capture_id'] = capture
        record['report_file'] = capture + '.json'
        if profile and capture == profile.identity['capture_id'] and profile.thread and profile.thread.is_alive():
            record['status'] = 'running'
            started = profile.started
        else:
            from .memory_capture_outcome import resolve
            state = resolve(self.channel, capture, self.target)
            started = state.pop('started_monotonic')
            record.update(state)
        record['capture_age_seconds'] = max(0, time.monotonic() - started)
        return record

    def tick(self):
        if self.channel.endpoint() != self.target:raise ValueError('worker_identity_changed')
        with self.channel.lock():
            self.channel.prune(self.target)
            for path in self.channel.paths():
                try:
                    record = self.channel.read(path)
                    if record.get('request_id') != path.stem:raise ValueError('request_path_identity_mismatch')
                    if record.get('target') != self.target:continue
                    if record.get('status') == 'queued':
                        if not protocol.request_valid(record, self.target):
                            record.update(status='failed', reason='request_invalid_or_expired')
                        else:
                            active = self.profile and self.profile.thread and self.profile.thread.is_alive()
                            if active:
                                record.update(status='accepted', reason='joined_active_capture', new_capture=False)
                            elif self.profile and time.monotonic() - self.started < protocol.COOLDOWN_SECONDS:
                                record.update(status='cooldown', reason='minimum_interval', new_capture=False)
                            else:
                                record.update(status='accepted', new_capture=True)
                                # Persist acceptance before attempting tracing; a crash is not success.
                                self.channel.write(path, record)
                                self._begin(record['request_id'], protocol.CAPTURE_SECONDS)
                            cooldown = record['status'] == 'cooldown'
                            self._reply(record)
                            if cooldown:record.update(status='cooldown', reason='minimum_interval')
                    elif record.get('status') in ('accepted', 'running'):
                        if not protocol._UUID.fullmatch(str(record.get('capture_id', ''))):
                            record.update(status='failed', reason='capture_not_owned')
                        else:self._reply(record)
                    else:continue
                    record['updated_mono'] = time.monotonic()
                    record['updated_wall'] = time.time()
                    self.channel.write(path, record)
                except Exception as exc:
                    # No malformed request or diagnostic write is a business fault.
                    self._gap(protocol.failure_reason(exc), path.stem)

    def _gap(self, reason, request_id=None, security_facts=None):
        try:
            from .python_memory_profile import _write_record
            _write_record(self.channel.root / 'artifacts/runtime/memory-capture-gap.json', {'schema': 1, 'evidence_kind': 'memory_capture_request',
                'status': 'failed', 'reason': reason, 'request_id': request_id, 'target': self.target,
                'capture_id': self.profile.identity['capture_id'] if self.profile else None,
                'at': time.time(), 'retention': 'latest_failure_only',
                **({'security_facts': security_facts} if security_facts else {})})
        except Exception:pass

    def _run(self):
        while not self.stop.is_set():
            try:self.tick()
            except RuntimeError as exc:
                if str(exc) != 'UPDATE_DATA_DIRECTORY_BUSY':self._gap(type(exc).__name__)
            except Exception as exc:
                self._gap(protocol.failure_reason(exc), security_facts=getattr(exc, 'security_facts', None))
            self.stop.wait(protocol.POLL_SECONDS)

    def close(self):
        self.stop.set()
        try:
            if self.profile:self.profile.close()
            if self.thread:self.thread.join(timeout=.5)
        except Exception as exc:self._gap(type(exc).__name__)


def install(root, performance=None, engineering_seconds=0):
    coordinator = Coordinator(root, performance)
    try:return coordinator.start(engineering_seconds)
    except Exception as exc:
        coordinator._gap(protocol.failure_reason(exc), security_facts=getattr(exc, 'security_facts', None))
        coordinator.close()
        return None


def request_capture(root, progress=None):
    """Called on ExportJob's background thread. Failures still permit export."""
    start = time.monotonic()
    record = {'schema': protocol.SCHEMA, 'evidence_kind': 'memory_capture_request',
        'operation': 'memory_capture', 'request_id': uuid.uuid4().hex,
        'requested_mono': start, 'requested_wall': time.time(),
        'expires_mono': start + protocol.ACK_SECONDS, 'status': 'queued'}
    channel = protocol.Channel(root)
    try:
        if not channel.folder.exists():
            marker_path = channel.root / 'runtime/session.json'
            marker = channel.read(marker_path) if marker_path.exists() else {}
            record.update(status='unsupported' if marker.get('status') == 'running' else 'not_running',
                          reason='capture_endpoint_absent')
            return record
        record['target'] = channel.endpoint()
        record['requester'] = protocol.identity()
        path = channel.requests / (record['request_id'] + '.json')
        while True:
            try:
                with channel.lock():
                    channel.prune(record['target'])
                    if channel.active_count() >= protocol.MAX_REQUESTS or len(channel.paths()) >= protocol.MAX_REQUESTS * 4 - 1:
                        record.update(status='busy', reason='request_budget')
                        return record
                    channel.write(path, record)
                break
            except RuntimeError as exc:
                if str(exc) != 'UPDATE_DATA_DIRECTORY_BUSY':raise
                if time.monotonic() - start >= protocol.ACK_SECONDS:
                    record.update(status='busy', reason='channel_busy')
                    return record
                time.sleep(protocol.POLL_SECONDS)
        while time.monotonic() - start < protocol.WAIT_SECONDS:
            if channel.endpoint() != record['target']:
                raise ValueError('worker_identity_changed')
            try:
                with channel.lock():response = channel.read(path)
            except RuntimeError as exc:
                if str(exc) != 'UPDATE_DATA_DIRECTORY_BUSY':raise
                time.sleep(protocol.POLL_SECONDS)
                continue
            if response.get('request_id') != record['request_id'] or response.get('target') != record['target']:
                raise ValueError('response_identity_changed')
            if response.get('status') not in protocol.STATES:raise ValueError('response_status_invalid')
            record = response
            if record['status'] not in ('queued', 'accepted', 'running'):return record
            if record['status'] == 'queued' and time.monotonic() - start >= protocol.ACK_SECONDS:
                record.update(status='timeout', reason='acceptance_timeout')
                return record
            if progress:
                progress({'percent': 5, 'stage': '正在采集运行状态，随后导出故障证据……'})
            time.sleep(protocol.POLL_SECONDS)
        record.update(status='timeout', reason='capture_wait_timeout')
    except FileNotFoundError:
        record.update(status='not_running', reason='capture_endpoint_missing')
    except Exception as exc:
        record.update(status='failed', reason=protocol.failure_reason(exc))
        if isinstance(exc, protocol.ChannelSecurityError):
            record['security_facts'] = exc.security_facts
    finally:
        record['request_wait_seconds'] = max(0, time.monotonic() - start)
        record['observed_wall'] = time.time()
    return record
