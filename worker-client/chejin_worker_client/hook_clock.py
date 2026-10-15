"""UTC anchored to an authenticated backend heartbeat, advanced monotonically.

last_heartbeat_at is stamped by the backend in UTC before its response. The
lower estimate is safe for future-time guards; expiry uses the RTT upper bound.
This clock never changes the OS clock or converts existing physical journals.
"""
from datetime import datetime, timezone, timedelta
import math
import threading
import time


# A stale heartbeat or wide network RTT cannot authorize a physical action.
# They are temporary connection waits, distinct from malformed or jumping clocks.
# One tolerance applies to both jump detection and interval reconciliation.
# A small disagreement widens conservative bounds; it never picks a midpoint.
CLOCK_CALIBRATION_TOLERANCE_SECONDS = .1
CLOCK_TIMESTAMP_PRECISION_SECONDS = .000001

CLOCK_WAIT_CODES = frozenset({'HOOK_BACKEND_CLOCK_STALE', 'HOOK_BACKEND_CLOCK_RTT_UNCONFIRMED'})


class ClockWaitError(ValueError):
    """Same wait contract, with numeric evidence safe for local diagnostics."""
    def __init__(self, code, **details):
        super().__init__(code)
        self.clock_check = details


def validate_heartbeat_time(receipt, *, monotonic=time.monotonic, max_age_seconds=120, max_rtt_seconds=1):
    try:
        text = receipt.server_time_utc
        if not isinstance(text, str): raise ValueError()
        stamp = datetime.fromisoformat(text.replace('Z', '+00:00'))
        # The original UTC database column serializes without an offset on SQLite.
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
        start, end, now = receipt.request_started_monotonic, receipt.response_received_monotonic, monotonic()
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end, now)): raise ValueError()
        rtt, age = end - start, now - end
        if rtt < 0 or age < 0: raise ValueError()
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise ValueError('HOOK_BACKEND_CLOCK_UNCONFIRMED') from None
    if age > max_age_seconds: raise ClockWaitError('HOOK_BACKEND_CLOCK_STALE',
        reason='stale_receipt', receipt_age_seconds=age, receipt_rtt_seconds=rtt, max_age_seconds=max_age_seconds)
    if rtt > max_rtt_seconds: raise ClockWaitError('HOOK_BACKEND_CLOCK_RTT_UNCONFIRMED',
        reason='slow_response', receipt_rtt_seconds=rtt, receipt_age_seconds=age, max_rtt_seconds=max_rtt_seconds)
    return stamp, end, rtt


class HookBackendClock:
    def __init__(self, *, monotonic=time.monotonic, max_age_seconds=120, max_rtt_seconds=1):
        self.monotonic, self.max_age, self.max_rtt = monotonic, max_age_seconds, max_rtt_seconds
        self.lock = threading.RLock()
        self.anchor = None
        self._last_receipt = None
        self._last_lower = None
        self.failure_detail = None

    def observe(self, receipt):
        stamp, end, rtt = validate_heartbeat_time(receipt, monotonic=self.monotonic,
            max_age_seconds=self.max_age, max_rtt_seconds=self.max_rtt)
        with self.lock:
            precision = timedelta(seconds=CLOCK_TIMESTAMP_PRECISION_SECONDS)
            lower, upper = stamp-precision, stamp+timedelta(seconds=rtt)+precision
            self.failure_detail = None
            if self.anchor is not None:
                prior, received, uncertainty = self.anchor
                # The business runtime can obtain a newer heartbeat than the
                # connection loop's cached snapshot. Replaying that old sample
                # must not roll calibration back or repeatedly tighten it.
                if end < received: return
                if end == received and self._last_receipt == (stamp,end,rtt): return
                predicted = prior + timedelta(seconds=end-received)
                predicted_upper = predicted + timedelta(seconds=uncertainty)
                gap = max((lower-predicted_upper).total_seconds(), (predicted-upper).total_seconds(), 0)
                detail = {'receipt_gap_seconds':gap,'receipt_rtt_seconds':rtt,
                    'prior_uncertainty_seconds':uncertainty,'receipt_interval_seconds':end-received}
                if gap > CLOCK_CALIBRATION_TOLERANCE_SECONDS:
                    self.failure_detail = detail
                    raise ValueError('HOOK_BACKEND_CLOCK_CHANGED')
                if gap:
                    # Both observations remain possible within the declared
                    # tolerance. The enclosing interval preserves safe future
                    # and expiry guards, unlike ignoring the gap or averaging.
                    lower,upper = min(lower,predicted),max(upper,predicted_upper)
                else:
                    lower,upper = max(lower,predicted),min(upper,predicted_upper)
                # These bounds belong to response_received_monotonic, which
                # can precede the last issued now() value after history/API work.
                # Compare the issued floor only in _bounds(), after advancing
                # the new anchor to the same current monotonic instant.
                if (upper-lower).total_seconds() > self.max_rtt+2*CLOCK_TIMESTAMP_PRECISION_SECONDS:
                    self.failure_detail = detail
                    raise ClockWaitError('HOOK_BACKEND_CLOCK_RTT_UNCONFIRMED', reason='wide_uncertainty',
                        **detail, uncertainty_seconds=(upper-lower).total_seconds(), max_rtt_seconds=self.max_rtt)
            self.anchor = lower, end, (upper-lower).total_seconds()
            self._last_receipt = (stamp,end,rtt)

    def _bounds(self):
        with self.lock:
            if self.anchor is None: raise ValueError('HOOK_BACKEND_CLOCK_UNCONFIRMED')
            stamp, received, rtt = self.anchor
            elapsed = self.monotonic() - received
            if not math.isfinite(elapsed) or elapsed < 0: raise ValueError('HOOK_BACKEND_CLOCK_UNCONFIRMED')
            if elapsed > self.max_age: raise ClockWaitError('HOOK_BACKEND_CLOCK_STALE',
                reason='stale_receipt', receipt_age_seconds=elapsed, max_age_seconds=self.max_age)
            lower = stamp + timedelta(seconds=elapsed)
            upper = lower + timedelta(seconds=rtt)
            if self._last_lower is not None:
                lower = max(lower,self._last_lower)
            if upper < lower: raise ValueError('HOOK_BACKEND_CLOCK_CHANGED')
            self._last_lower = lower
            return lower, upper

    def now(self): return self._bounds()[0]
    def upper_now(self): return self._bounds()[1]

    def evidence(self):
        with self.lock:
            self._bounds()
            stamp, received, rtt = self.anchor
            return {'source':'authenticated_backend_heartbeat', 'server_anchor_utc':stamp.isoformat(),
                    'response_received_monotonic':received, 'round_trip_seconds':rtt}
