"""Bounded, non-authoritative send observations. Never used to permit/replay a send.

Only metadata is copied from existing calls. A same-body row is a candidate, not
proof of delivery or attribution to the original attempt. No network operations.
"""
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
from threading import RLock
import uuid

from .evidence_paths import task_artifacts
from .hook_history import schema_has_send_status

MAX_EVENTS = 64
MAX_ROWS = 32
MAX_BYTES = 256 * 1024
_LOCK = RLock()
_LOG = logging.getLogger(__name__)


def diagnostic_path(root, task_id, action_id):
    # Both identifiers use the existing safe path contract.
    task_artifacts(root, action_id)
    return task_artifacts(root, task_id) / 'diagnostics' / ('send-' + action_id + '.json')


def _load(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('diagnostic_size_limit')
    return json.loads(raw)


def _append(root, task_id, action_id, event, observed_at):
    """Atomic bounded snapshots; repeated polls coalesce without losing changes."""
    path = diagnostic_path(root, task_id, action_id)
    with _LOCK:
        value = _load(path) if path.exists() else {
            'schema_version': 'hook-send-diagnostics-v1', 'task_id': task_id,
            'reply_action_id': action_id, 'events': [], 'omitted_events': 0,
            'authority': 'diagnostic_only', 'automatic_resend_allowed': False,
            'limitations': [
                'Hook submission is not recipient delivery confirmation.',
                'Status is an uninterpreted WeChat field (first 64 characters).',
                'Same-text rows cannot prove which operation sent them.',
                'Not observed in a range does not prove deletion or non-delivery.',
                'Provider internal errors and network delivery acknowledgements are not exposed here.',
            ],
        }
        if (value.get('schema_version') != 'hook-send-diagnostics-v1'
                or value.get('task_id') != task_id or value.get('reply_action_id') != action_id):
            raise ValueError('diagnostic_identity_mismatch')
        events = value['events']
        if events and events[-1]['observation'] == event:
            events[-1]['last_observed_at'] = observed_at
            events[-1]['repeat_count'] += 1
        else:
            events.append({'first_observed_at': observed_at, 'last_observed_at': observed_at,
                           'repeat_count': 1, 'observation': event})
        while len(events) > MAX_EVENTS:
            # Preserve the initial event and newest changes; gaps are explicit.
            events.pop(1)
            value['omitted_events'] += 1
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        while len(raw) > MAX_BYTES and len(events) > 2:
            events.pop(1)
            value['omitted_events'] += 1
            raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(raw) > MAX_BYTES:
            raise ValueError('diagnostic_size_limit')
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('xb') as stream:
                os.chmod(temp, 0o600)
                stream.write(raw)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def _best_effort(operation, *, task_id, action_id):
    try:
        operation()
    except Exception as error:
        # Error text may contain a payload or path; log only the class.
        _LOG.warning('HOOK_SEND_DIAGNOSTICS_WRITE_FAILED error_type=%s', type(error).__name__)
        try:
            from .failure_evidence import record_capture_failure
            record_capture_failure('hook_send_diagnostics', error,
                error_code='HOOK_SEND_DIAGNOSTICS_WRITE_FAILED',
                safe_context={'task_id': task_id, 'reply_action_id': action_id})
        except Exception:
            pass  # Fully unavailable storage cannot be repaired by replaying a send.


def record_submission(root, permit, process, outcome, journal_root, elapsed_seconds):
    def write():
        intent = _load(journal_root / 'intent.json')
        response_path = journal_root / 'response.json'
        response = _load(response_path) if response_path.exists() else {}
        flag = response.get('SendTextMsg')
        event = {
            'kind': 'submission', 'attempted_at': intent['attempted_at'],
            'completed_at': outcome['completed_at'], 'send_operation_ms': round(elapsed_seconds * 1000),
            'wechat_version': process.file_version, 'component_sha256': process.dll_sha256,
            'process_generation': process.generation, 'account_epoch': permit.account_epoch,
            'peer_sha256': hashlib.sha256(permit.peer_wxid.encode()).hexdigest(),
            'reply_text_hash': intent['reply_text_hash'],
            'hook_return': flag if isinstance(flag, (str, int, bool)) and len(str(flag)) <= 64 else None,
            'hook_return_available': 'SendTextMsg' in response,
            'outcome': {k: outcome[k] for k in ('call_completed', 'hook_submission_flag',
                'response_sha256', 'journal_sha256', 'error_code', 'quarantined',
                'local_call_window', 'local_clock_error') if k in outcome},
        }
        _append(root, permit.task_id, permit.reply_action_id, event, outcome['completed_at'])
    _best_effort(write, task_id=permit.task_id, action_id=permit.reply_action_id)


class SendHistoryDiagnostics:
    """One existing two-pass read, including partial/failed reads; no extra queries."""
    def __init__(self, root, permit, journal_root, process, now=None):
        self.root, self.permit, self.journal_root, self.process = root, permit, journal_root, process
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.databases = []
        self.current = None
        self.candidate_count = 0
        self.intent = None
        self.gaps = []
        try:
            self.intent = _load(journal_root / 'intent.json')
        except Exception:
            self.gaps.append('original_intent_unavailable')

    def capture(self, capture):
        try:
            self._capture(capture)
        except Exception:
            if 'capture_metadata_failed' not in self.gaps:
                self.gaps.append('capture_metadata_failed')

    def _capture(self, capture):
        q, payload = capture.query, capture.payload
        if q.kind == 'schema':
            if len(self.databases) >= 32:
                self.current = None
                if 'database_snapshot_limit' not in self.gaps:
                    self.gaps.append('database_snapshot_limit')
                return
            self.current = {'database': q.database.name, 'database_generation': q.database.generation,
                'status_capability': 'available' if schema_has_send_status(payload) else 'column_not_available',
                'rows': [], 'omitted_rows': 0, 'since_create_time': None, 'upper_local_id': None}
            self.databases.append(self.current)
        elif self.current is not None and q.kind == 'bound':
            self.current.update(since_create_time=q.since_create_time,
                upper_local_id=payload['data'][0]['upper_local_id'], row_count=payload['data'][0]['row_count'])
        elif self.current is not None and q.kind == 'page' and self.intent:
            for row in payload.get('data', []):
                body = row.get('StrContent')
                if (row.get('IsSender') != '1' or row.get('Type') != '1' or not isinstance(body, str)
                        or hashlib.sha256(body.encode()).hexdigest() != self.intent['reply_text_hash']):
                    continue
                status = row.get('diagnostic_status')
                self.current['rows'].append({
                    'local_id': row['localId'], 'server_id': row['MsgSvrID'], 'create_time': row['CreateTime'],
                    'source_page_sha256': hashlib.sha256(capture.raw_body).hexdigest(),
                    'status': status if isinstance(status, str) and len(status) <= 64 else None,
                    'status_observation': ('column_not_available' if not q.include_send_status else
                        'missing' if 'diagnostic_status' not in row else 'null' if status is None else
                        'value' if isinstance(status, str) and len(status) <= 64 else 'malformed'),
                })
                self.candidate_count += 1
                if len(self.current['rows']) > MAX_ROWS:
                    self.current['rows'].pop(0)
                    self.current['omitted_rows'] += 1
                    self.candidate_count -= 1
                if self.candidate_count > MAX_ROWS * 2:
                    oldest = next(d for d in self.databases if d['rows'])
                    oldest['rows'].pop(0)
                    oldest['omitted_rows'] += 1
                    self.candidate_count -= 1

    def finish(self, error=None):
        def write():
            code = getattr(error, 'code', None) or str(error or '')
            # Never persist arbitrary exception messages (may include credentials).
            code = code if code.startswith('HOOK_') and code.replace('_', '').isalnum() and len(code) < 100 else None
            event = {'kind': 'history', 'read_completed': error is None, 'error_code': code,
                     'error_type': type(error).__name__ if error else None,
                     'process_generation': self.process.generation, 'gaps': self.gaps,
                     'databases': self.databases,
                     'empty_rows_mean': 'no_same_text_candidate_observed_in_this_range'}
            _append(self.root, self.permit.task_id, self.permit.reply_action_id, event, self.now().isoformat())
        _best_effort(write, task_id=self.permit.task_id, action_id=self.permit.reply_action_id)
