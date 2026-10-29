"""Pure rules for local physical-call evidence; never substitute it for lease time."""
from datetime import datetime, timezone, timedelta
import math


# Diagnostic policy, independent of physical-send authorization tolerances.
CLOCK_WARNING_SECONDS = 5
CLOCK_DIAGNOSTIC_MAX_RTT = 1
BEIJING_UTC_OFFSET_SECONDS = 28800
BEIJING_TIMEZONE = timezone(timedelta(seconds=BEIJING_UTC_OFFSET_SECONDS), "Asia/Shanghai")
CLOCK_REFERENCE_MAX_AGE = 300
# One allowlist for backend observations, Worker validation and Windows repair.
# The VPC endpoint is only used when explicitly configured on the backend.
CLOCK_PUBLIC_NTP_HOSTS = ("ntp.aliyun.com", "time.windows.com", "time.cloudflare.com")
CLOCK_REFERENCE_SOURCES = {
    "ntp.aliyun.com": "aliyun_ntp",
    "ntp.cloud.aliyuncs.com": "aliyun_ntp",
    "time.windows.com": "windows_ntp",
    "time.cloudflare.com": "cloudflare_ntp",
}


def clock_reference_hosts(primary):
    if primary not in CLOCK_REFERENCE_SOURCES:
        raise ValueError('unsupported_time_reference')
    return tuple(dict.fromkeys((primary, *CLOCK_PUBLIC_NTP_HOSTS)))


def windows_time_peers():
    # 0x8 = client; 0xa = client + UseAsFallbackOnly. W32Time owns periodic
    # synchronization and fallback, not a second application retry loop.
    return ' '.join(f'{host},{"0x8" if index == 0 else "0xa"}'
                    for index, host in enumerate(CLOCK_PUBLIC_NTP_HOSTS))

def utc(value):
    if not isinstance(value, str): raise ValueError('HOOK_SEND_LOCAL_CLOCK_INVALID')
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError('HOOK_SEND_LOCAL_CLOCK_INVALID')
    return stamp.astimezone(timezone.utc)


def call_window(value, backend_started_at, backend_completed_at):
    """Keep the two clocks separate, but require compatible elapsed durations.

    New calls also compare the wall duration with monotonic elapsed time. Legacy
    completed-call rows have no monotonic field; they remain explicitly labelled.
    No window is inferred from a fresh clock reading during recovery.
    """
    if not isinstance(value, dict) or set(value) != {'source', 'started_at', 'completed_at', 'elapsed_seconds'}:
        raise ValueError('HOOK_SEND_LOCAL_CLOCK_INVALID')
    start, end = utc(value['started_at']), utc(value['completed_at'])
    duration = (end-start).total_seconds()
    backend_duration = (utc(backend_completed_at)-utc(backend_started_at)).total_seconds()
    if duration < 0 or backend_duration < 0 or duration > backend_duration + .1:
        raise ValueError('HOOK_SEND_LOCAL_CLOCK_INVALID')
    elapsed = value['elapsed_seconds']
    if value['source'] == 'physical_call_v1':
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0 or abs(duration-elapsed) > .1:
            raise ValueError('HOOK_SEND_LOCAL_CLOCK_INVALID')
    elif value['source'] != 'retained_completed_call_v1' or elapsed is not None:
        raise ValueError('HOOK_SEND_LOCAL_CLOCK_INVALID')
    # Preserve the existing one-second native timestamp precision allowance.
    return int(start.timestamp()), int(end.timestamp()) + 1
