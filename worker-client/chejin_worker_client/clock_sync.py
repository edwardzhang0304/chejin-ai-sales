"""Explicit clock repair using the existing admission and shutdown lifecycle."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
import uuid

from . import storage
from .clock_diagnostics import local_timezone
from .clock_sync_ticket import ClockSyncTicket, valid_operation
from .windows_clock_sync import synchronize_windows

MESSAGES = {
    'idle': '', 'stopping': '正在暂停运行并保留原记录',
    'repairing': '正在同步北京时间，请处理 Windows 授权提示',
    'verifying': '正在重新核验电脑时间与连接',
    'verified': '电脑时间已核验，接单仍需通过原有恢复检查',
    'unconfirmed': '同步已发起，但尚未核验成功，请检查网络后重试',
    'managed': '电脑授时由单位管理，请联系管理员同步时间',
    'service_disabled': 'Windows 时间服务被禁用，请联系管理员启用',
    'configure_failed': '阿里授时配置失败，时区可能已调整，请导出故障证据',
    'resync_failed': '对时请求未成功，时区可能已调整，请检查网络和时间服务',
    'windows_failed': 'Windows 对时未完成，可能已部分修改，请导出故障证据',
    'cancelled': '已取消 Windows 授权，未完成对时',
    'elevation_failed': '无法取得对时权限，请联系管理员',
    'unsupported': '此功能仅用于 Windows 客户端',
    'stop_failed': '当前操作尚未安全停止，未修改电脑时间，请稍后再次同步',
    'restart_failed': '连接检查未启动，请保留数据并重新打开 Worker',
    'busy': '已有更新正在进行，请等待完成',
    'unavailable': '尚未绑定工作微信，暂不能同步',
    'failed': '对时未完成，请导出故障证据',
    'recovering': '正在等待上一次对时结束，暂不运行业务',
    'interrupted': '上一次对时中断，已收回旧授权，请重新核验时间',
    'revoked': '本次对时授权已失效，请重新同步',
    'evidence_failed': '无法保存对时记录，已保留停止状态，请导出故障证据',
}


class ClockSync:
    def __init__(self, runner, root: Path, *, repair=synchronize_windows,
                 monotonic=time.monotonic, verify_seconds=30, on_change=lambda: None):
        self.runner, self.root, self.repair = runner, Path(root), repair
        self.monotonic, self.verify_seconds = monotonic, verify_seconds
        self.on_change, self.ticket = on_change, ClockSyncTicket(root)
        self._lock = threading.Lock()
        self._state = {'state': 'idle', 'message': '', 'busy': False}
        self._events, self._sequence, self._operation = [], 0, None

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def blocks_actions(self):
        control = storage.load_runtime_control()
        return self.snapshot()['busy'] or str(control.get('update_request_id') or '').startswith('clock-sync-')

    def runtime_ready(self, binding):
        """Before starting any threads, revoke a crashed operation's authority.

        A pending UAC can launch later, but its revoked ticket cannot change the
        clock. An already active helper holds the lock; startup keeps waiting.
        """
        control = storage.load_runtime_control()
        operation = str(control.get('update_request_id') or '')
        if not operation.startswith('clock-sync-'):
            return True
        self._operation = operation
        try:
            if not valid_operation(operation):
                raise ValueError('CLOCK_OPERATION_INVALID')
            self.ticket.revoke()
            storage.request_runtime_pause()
            if binding is not None and binding.run_status == 'running':
                binding.run_status = 'paused'
                storage.save_binding(binding)
            self._record('interrupted', busy=False, ticket_revoked=True)
            self.runner.set_update_new_work_gate(False, update_request_id=operation)
            return True
        except BlockingIOError:
            self._publish('recovering', True)
        except Exception:
            self._publish('evidence_failed', False)
        return False

    def start(self):
        with self._lock:
            if self._state['busy']:
                return False
            self._state = {'state': 'stopping', 'message': MESSAGES['stopping'], 'busy': True}
        self.on_change()
        threading.Thread(target=self._run, name='clock-sync', daemon=True).start()
        return True

    def _publish(self, state, busy):
        with self._lock:
            self._state = {'state': state, 'message': MESSAGES[state], 'busy': busy}
        self.on_change()

    def _record(self, state, *, busy=True, publish=True, **details):
        path = self.root / 'artifacts/runtime/clock-sync.json'
        if not self._events and path.exists():
            if path.stat().st_size > 256 * 1024:
                raise ValueError('CLOCK_SYNC_EVIDENCE_TOO_LARGE')
            previous = json.loads(path.read_text(encoding='utf-8'))
            self._events = previous.get('events', [])[-31:]
        self._sequence = max([self._sequence] + [int(e.get('sequence', 0))
            for e in self._events if e.get('operation_id') == self._operation]) + 1
        value = {'operation_id': self._operation, 'sequence': self._sequence,
                 'state': state, 'busy': busy, **details,
                 'observed_monotonic': self.monotonic(),
                 'observed_at_utc': datetime.now(timezone.utc).isoformat()}
        path.parent.mkdir(parents=True, exist_ok=True)
        self._events = (self._events + [value])[-32:]
        tmp = path.with_suffix('.tmp')
        with tmp.open('w', encoding='utf-8') as stream:
            json.dump({'schema_version': 1, 'latest': value, 'events': self._events}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
        if publish:
            self._publish(state, busy)

    def _revoke_when_finished(self):
        # Parent failure must not permit a delayed UAC/helper to run after
        # services restart. Busy locking is retried off the Qt thread.
        while True:
            try:
                self.ticket.revoke()
                return
            except BlockingIOError:
                self._publish('recovering', True)
                time.sleep(.2)

    def _run(self):
        reserved = stopped = ticket_open = False
        result = 'failed'
        helper = {}
        try:
            self._operation = self.runner.begin_clock_sync('clock-sync-' + str(uuid.uuid4()))
            if not self._operation:
                result = 'busy' if self.runner.binding else 'unavailable'
                return
            reserved = True
            self._record('stopping', original_run_status=self.runner.binding.run_status,
                         timezone_before=local_timezone())
            # This keeps an existing fault and persists a local pause even when
            # the backend is offline. The admission gate already blocks new work.
            self.runner.set_run_status('paused')
            saved = storage.load_binding()
            if (saved is None or saved.run_status not in {'paused', 'faulted'}
                    or self.runner.binding.run_status not in {'paused', 'faulted'}):
                result = 'stop_failed'
                return
            try:
                self.runner.stop_for_update(25)
                if self.runner.bridge.sidecar_active():
                    raise RuntimeError('CLOCK_SIDECAR_NOT_STOPPED')
            except Exception:
                result = 'stop_failed'
                return
            stopped = True
            self._record('repairing', writers_stopped=True)
            self.ticket.authorize(self._operation)
            ticket_open = True
            try:
                helper = self.repair(self._operation, self.ticket.path)
                result = helper['result']
            except Exception as exc:
                helper = {'result': 'failed', 'exception_type': type(exc).__name__,
                          'helper_exit_confirmed': False}
                result = 'failed'
            finally:
                self._revoke_when_finished()
                ticket_open = False
            completed = self.monotonic()
            self._record('verifying', helper=helper, ticket_revoked=True,
                         timezone_after=local_timezone())
            try:
                self.runner.start(self.runner.binding)
                stopped = False
            except Exception:
                result = 'restart_failed'
                return
            if result == 'requested':
                result = self._verify(completed)
        except Exception:
            # A missing durable record is not permission to continue repair or
            # forget ownership. Restart can revoke the ticket under its lock.
            result = 'evidence_failed'
        finally:
            try:
                if ticket_open:
                    self._revoke_when_finished()
                retain = reserved and (stopped or result in {'stop_failed', 'restart_failed', 'evidence_failed'})
                # Persist the terminal result BEFORE releasing the owned gate.
                self._record(result, busy=False, publish=False, helper=helper, gate_retained=retain)
                if reserved and not retain:
                    self.runner.set_update_new_work_gate(False, update_request_id=self._operation)
                self._publish(result, False)
            except Exception:
                self._publish('evidence_failed', False)

    def _verify(self, completed):
        monitor = self.runner.hook_services.connection.backend.clock_diagnostics
        deadline = completed + self.verify_seconds
        while self.monotonic() < deadline:
            sample = monitor.snapshot()
            if (sample.get('observed_monotonic', 0) > completed
                    and sample.get('comparison_status') == 'aligned'
                    and sample.get('timezone', {}).get('beijing_timezone') is True
                    and sample.get('backend_reference', {}).get('status') == 'aligned'):
                self._record('verified', clock_check=sample,
                             connection_check=self.runner.hook_services.startup_check_state())
                return 'verified'
            time.sleep(.1)
        self._record('unconfirmed', clock_check=monitor.snapshot(),
                     connection_check=self.runner.hook_services.startup_check_state())
        return 'unconfirmed'
