"""Startup/heartbeat clock evidence and notices, separate from send permission.

No Windows setting is changed. The existing authenticated monotonic business
clock remains authoritative; these observations never clear its safety gates.
"""
from datetime import datetime, timezone
from pathlib import Path
import copy
import json
import math
import os
import sys
import threading
import time
import uuid

from .shared_rules import hook_time_rules as policy

SKEW_WARNING_SECONDS = policy.CLOCK_WARNING_SECONDS
MAX_RTT_SECONDS = policy.CLOCK_DIAGNOSTIC_MAX_RTT
MAX_SAMPLE_AGE_SECONDS = 120


def local_timezone():
    local = datetime.now().astimezone()
    offset = int(local.utcoffset().total_seconds())
    key = None
    if sys.platform == 'win32':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                    r'SYSTEM\CurrentControlSet\Control\TimeZoneInformation') as registry:
                key = winreg.QueryValueEx(registry, 'TimeZoneKeyName')[0].rstrip('\x00')
        except OSError:
            pass
    beijing = offset == policy.BEIJING_UTC_OFFSET_SECONDS
    if sys.platform == 'win32' and beijing:
        beijing = None if key is None else key == 'China Standard Time'
    return {'utc_offset_seconds': offset, 'windows_zone': key, 'beijing_timezone': beijing}


def finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def reference_summary(value):
    """Allowlisted protocol projection; never copy arbitrary upstream text."""
    unknown = {'status': 'unconfirmed'}
    if not isinstance(value, dict): return unknown
    if (value.get('source') != 'aliyun_ntp' or value.get('host') not in
            policy.ALIYUN_REFERENCE_HOSTS or value.get('business_timezone') != 'Asia/Shanghai'
            or value.get('utc_offset_seconds') != 28800 or value.get('authenticated') is not False):
        return unknown
    status = value.get('status')
    age = value.get('sample_age_seconds')
    if status not in {'aligned', 'skewed', 'uncertain', 'unconfirmed', 'stale'}: return unknown
    result = {'status': status, 'source': 'aliyun_ntp', 'host': value['host'], 'authenticated': False}
    if finite(age): result['sample_age_seconds'] = age
    if status in {'aligned', 'skewed', 'uncertain'}:
        low, high, rtt = (value.get(key) for key in
            ('offset_lower_seconds', 'offset_upper_seconds', 'round_trip_seconds'))
        if not (finite(age) and 0 <= age <= policy.ALIYUN_REFERENCE_MAX_AGE and all(finite(v) for v in (low, high, rtt))
                and low <= high and 0 <= rtt <= MAX_RTT_SECONDS):
            return unknown
        # Derive classification rather than trusting a contradictory label.
        result['status'] = ('skewed' if low > SKEW_WARNING_SECONDS or high < -SKEW_WARNING_SECONDS else
                            'aligned' if -SKEW_WARNING_SECONDS <= low <= high <= SKEW_WARNING_SECONDS else 'uncertain')
        result.update(offset_lower_seconds=low, offset_upper_seconds=high, round_trip_seconds=rtt)
    return result


def compare_clock(receipt, zone):
    result = {'comparison_status': 'unconfirmed', 'timezone': zone,
              'backend_reference': reference_summary(getattr(receipt, 'clock_reference', None))}
    try:
        start, end = receipt.request_started_monotonic, receipt.response_received_monotonic
        wall_start, wall_end = receipt.request_started_wall, receipt.response_received_wall
        if not all(finite(v) for v in (start, end, wall_start, wall_end)): raise ValueError()
        rtt = end-start
        result['round_trip_seconds'] = rtt
        if rtt < 0: raise ValueError()
        if abs(wall_end-wall_start-rtt) > .1:
            result['reason'] = 'computer_clock_changed_during_request'
            return result
        stamp = datetime.fromisoformat(receipt.server_time_utc.replace('Z', '+00:00'))
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
        result.update(server_time_utc=stamp.isoformat(),
            server_time_beijing=stamp.astimezone(policy.BEIJING_TIMEZONE).isoformat(),
            computer_time_utc=datetime.fromtimestamp(wall_end, timezone.utc).isoformat())
        if rtt > MAX_RTT_SECONDS:
            result['reason'] = 'network_round_trip_too_wide'
            return result
        # Positive means the computer is ahead. The backend stamp precedes its
        # response; retain the whole RTT interval instead of guessing midpoint.
        low, high = wall_end-stamp.timestamp()-rtt-.000001, wall_end-stamp.timestamp()+.000001
        result.update(offset_lower_seconds=low, offset_upper_seconds=high,
            comparison_status='skewed' if low > SKEW_WARNING_SECONDS or high < -SKEW_WARNING_SECONDS else
            'aligned' if -SKEW_WARNING_SECONDS <= low <= high <= SKEW_WARNING_SECONDS else 'uncertain')
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        result['reason'] = 'heartbeat_time_unconfirmed'
    return result


def clock_notice(value):
    notices = []
    if value.get('timezone', {}).get('beijing_timezone') is False:
        notices.append('请将电脑时区设为北京时间（UTC+8，北京）')
    if value.get('comparison_status') == 'skewed':
        notices.append('电脑时间与后台明显不一致，请同步电脑时间')
    elif value.get('reason') == 'computer_clock_changed_during_request':
        notices.append('电脑时间发生变化，等待重新对时')
    if value.get('backend_reference', {}).get('status') == 'skewed':
        notices.append('后台时间与阿里授时不一致，请联系管理员')
    return '；'.join(notices)


class ClockDiagnostics:
    def __init__(self, root: Path, *, monotonic=time.monotonic, zone_reader=local_timezone):
        self.path = Path(root)/'artifacts'/'runtime'/'clock-check.json'
        self.monotonic, self.zone_reader = monotonic, zone_reader
        self.session = str(uuid.uuid4())
        self._lock, self._write_lock = threading.Lock(), threading.Lock()
        self._latest = {}
        self._history = []
        self._last_saved = float('-inf')
        self._signature = None
        self._received = self.monotonic()
        self._publish({'comparison_status': 'pending', 'reason': 'startup_waiting_for_heartbeat',
                       'timezone': zone_reader(), 'backend_reference': {'status': 'unconfirmed'}}, force=True)
        self._startup = copy.deepcopy(self._latest)

    def observe(self, receipt):
        self._publish(compare_clock(receipt, self.zone_reader()))

    def failed(self):
        self._publish({'comparison_status': 'unconfirmed', 'reason': 'heartbeat_failed',
            'timezone': self.zone_reader(), 'backend_reference': {'status': 'unconfirmed'}})

    def _publish(self, value, *, force=False):
        # Serialize evidence writers without putting file I/O under the GUI lock.
        with self._write_lock:
            self._publish_serialized(value, force=force)

    def _publish_serialized(self, value, *, force=False):
        now = self.monotonic()
        value = {**value, 'observed_at_utc': datetime.now(timezone.utc).isoformat(),
                 'observed_monotonic': now, 'business_timezone': 'Asia/Shanghai'}
        signature = (value.get('comparison_status'), value.get('reason'),
                     value['timezone'].get('beijing_timezone'), value['backend_reference'].get('status'))
        with self._lock:
            self._latest, self._received = value, now
            save = force or signature != self._signature or now-self._last_saved >= 60
            if not save: return
            self._history = (self._history+[value])[-32:]
            self._last_saved, self._signature = now, signature
            output = {'schema_version': 1, 'session_id': self.session,
                      'startup': getattr(self, '_startup', value), 'latest': value,
                      'recent_checks': self._history, 'changes_system_clock': False,
                      'permission_source': 'existing_authenticated_backend_monotonic_clock'}
        # Only startup/heartbeat threads write; GUI snapshot never performs I/O.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(output, ensure_ascii=False, allow_nan=False), encoding='utf-8')
            os.replace(temporary, self.path)
        except (OSError, ValueError) as exc:
            from .failure_evidence import record_capture_failure
            record_capture_failure('clock_diagnostics', exc)

    def snapshot(self):
        with self._lock:
            value = copy.deepcopy(self._latest)
            age = self.monotonic()-self._received
        value['sample_age_seconds'] = age
        if not 0 <= age <= MAX_SAMPLE_AGE_SECONDS:
            value['comparison_status'] = 'stale'
            value['backend_reference'] = {'status': 'unconfirmed'}
        value['notice'] = clock_notice(value)
        return value
