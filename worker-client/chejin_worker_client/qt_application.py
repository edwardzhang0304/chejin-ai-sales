from __future__ import annotations

from typing import Any, Callable

from PySide6.QtWidgets import QApplication

from .runtime_supervision import report_unhandled_exception


class GuardedQApplication(QApplication):
    """Turn Python exceptions escaping Qt callbacks into durable incidents."""

    def notify(self, receiver, event):  # type: ignore[no-untyped-def]
        return run_guarded_qt_callback(
            lambda: super(GuardedQApplication, self).notify(receiver, event)
        )


def run_guarded_qt_callback(callback: Callable[[], Any]) -> Any:
    try:
        return callback()
    except BaseException as exc:
        report_unhandled_exception(
            "qt_callback",
            type(exc),
            exc,
            exc.__traceback__,
        )
        return False
