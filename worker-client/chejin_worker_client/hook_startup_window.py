"""Bounded startup preparation; waiting never grants business authority."""
import time


class StartupWindowWait(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class StartupWindowError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


PREPARE_FAILURES = {
    'hook_c1_foreground_unconfirmed': 'HOOK_STARTUP_FOREGROUND_REQUIRED',
    'hook_c1_foreground_changed_before_capture': 'HOOK_STARTUP_FOREGROUND_REQUIRED',
    'hook_c1_close_wechat_popup_first': 'HOOK_C1_POPUP_PENDING',
    'hook_c1_window_identity_unavailable': 'HOOK_WINDOW_UNAVAILABLE',
    'hook_c1_dpi_awareness_unverified': 'HOOK_STARTUP_DPI_UNVERIFIED',
}


class StartupWindowAttempts:
    """One active activation, then at most two passive-foreground retries.

    A heartbeat may observe readiness, but cannot repeatedly bring WeChat to
    the front. A fresh process/account starts a new bounded preparation.
    """
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.key = None
        self.count = 0
        self.retry_after = 0.0
        self.activation_used = False

    def select(self, process, scope):
        key = (process.pid, getattr(process, 'generation', None),
               getattr(scope, 'self_wxid', None), getattr(scope, 'account_epoch', None))
        if key != self.key:
            self.key, self.count, self.retry_after = key, 0, 0.0
            self.activation_used = False

    def check(self, foreground_ready):
        if self.count >= 3:
            raise StartupWindowWait('HOOK_STARTUP_RETRY_EXHAUSTED')
        if self.count and self.clock() < self.retry_after:
            raise StartupWindowWait('HOOK_STARTUP_RETRY_WAIT')
        if self.activation_used and not foreground_ready():
            raise StartupWindowWait('HOOK_STARTUP_FOREGROUND_REQUIRED')

    def begin(self):
        self.count += 1
        activate = not self.activation_used
        self.activation_used = True
        return activate

    def completed(self):
        # Later layout invalidation is a new check, not a lifetime failure.
        # It still cannot activate the same process/account again.
        self.count, self.retry_after = 0, 0.0

    def failed(self):
        self.retry_after = self.clock() + 10.0
