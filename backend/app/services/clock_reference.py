"""Read-only Aliyun SNTP cross-check; never sets the host or business clock.

UDP NTP is not authenticated. This is operational evidence, not authorization
for a send. Heartbeats only read a cached snapshot and never wait for UDP/DNS.
"""
from datetime import datetime, timezone
import secrets
import socket
import struct
import threading
import time

NTP_EPOCH = 2208988800
from app.contracts.shared_rules import shared_adapter
_policy = shared_adapter('hook_time_rules')
HOSTS = _policy.ALIYUN_REFERENCE_HOSTS
MAX_AGE = _policy.ALIYUN_REFERENCE_MAX_AGE
MAX_RTT = _policy.CLOCK_DIAGNOSTIC_MAX_RTT
SKEW_WARNING = _policy.CLOCK_WARNING_SECONDS


def decode_sample(packet, nonce, started_wall, ended_wall, elapsed):
    if (len(packet) != 48 or packet[0] >> 6 == 3 or (packet[0] >> 3) & 7 not in (3, 4)
            or packet[0] & 7 != 4 or not 1 <= packet[1] <= 15 or packet[24:32] != nonce):
        raise ValueError('invalid_ntp_response')
    if not 0 <= elapsed <= MAX_RTT or abs(ended_wall-started_wall-elapsed) > .1:
        raise ValueError('unstable_clock_or_network')
    def stamp(offset):
        seconds, fraction = struct.unpack_from('!II', packet, offset)
        if not (seconds or fraction): raise ValueError('missing_ntp_timestamp')
        raw = seconds + fraction / 2**32 - NTP_EPOCH
        # Unfold the NTP era nearest our current wall clock (2036 rollover).
        return raw + round((ended_wall-raw)/2**32)*2**32
    received, transmitted = stamp(32), stamp(40)
    root_delay = struct.unpack_from('!i', packet, 4)[0] / 65536
    dispersion = struct.unpack_from('!I', packet, 8)[0] / 65536
    if not 0 <= transmitted-received <= elapsed + .001 or not 0 <= root_delay <= 1 or dispersion > 1:
        raise ValueError('uncertain_ntp_response')
    uncertainty = root_delay / 2 + dispersion + .001
    lower = transmitted - ended_wall - uncertainty
    upper = received - started_wall + uncertainty
    if lower > upper: raise ValueError('invalid_ntp_interval')
    return {'status': 'skewed' if lower > SKEW_WARNING or upper < -SKEW_WARNING else
            'aligned' if -SKEW_WARNING <= lower <= upper <= SKEW_WARNING else 'uncertain',
            'offset_lower_seconds': lower, 'offset_upper_seconds': upper,
            'round_trip_seconds': elapsed, 'stratum': packet[1]}


def probe(host):
    nonce = struct.pack("!II", int(time.time()+NTP_EPOCH) % 2**32, secrets.randbits(32))
    packet = bytearray(48)
    packet[0] = 0x23  # NTPv4 client; random echoed origin binds this response.
    packet[40:48] = nonce
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.settimeout(2)
        connection.connect((host, 123))
        start = time.monotonic()
        wall = time.time()
        connection.send(packet)
        response = connection.recv(512)
        ended_wall = time.time()
        elapsed = time.monotonic()-start
    return decode_sample(response, nonce, wall, ended_wall, elapsed)


class ClockReference:
    def __init__(self, host='ntp.aliyun.com', *, query=probe, monotonic=time.monotonic):
        if host not in HOSTS: raise ValueError('unsupported_time_reference')
        self.host, self.query, self.monotonic = host, query, monotonic
        self._result = {'status': 'unconfirmed', 'reason': 'not_checked'}
        self._received = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def refresh(self):
        try:
            result = self.query(self.host)
        except (OSError, ValueError, OverflowError):
            result = {'status': 'unconfirmed', 'reason': 'reference_unavailable_or_invalid'}
        with self._lock:
            self._result = {**result, 'checked_at_utc': datetime.now(timezone.utc).isoformat()}
            self._received = self.monotonic()

    def snapshot(self):
        with self._lock:
            result = dict(self._result)
            age = None if self._received is None else self.monotonic()-self._received
        if age is not None and not 0 <= age <= MAX_AGE:
            result['status'] = 'stale'
        return {**result, 'source': 'aliyun_ntp', 'host': self.host,
                'authenticated': False, 'sample_age_seconds': age,
                'business_timezone': 'Asia/Shanghai', 'utc_offset_seconds': 28800}

    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        def run():
            while not self._stop.is_set():
                self.refresh()
                if self._stop.wait(60): break
        self._thread = threading.Thread(target=run, name='clock-reference', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread: self._thread.join(.1)
