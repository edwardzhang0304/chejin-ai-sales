"""Bounded, read-only preview of the shared update admission predicate.

All context/cache changes run on Qt's owner thread. A single daemon computes
at a time; no queue or business action is created. Click admission stays fresh
and atomic in UpdateCoordinator/TaskRunner, independent of this UI cache.
"""
from __future__ import annotations

from copy import deepcopy
from threading import Thread
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot


class UpdateControlsPreview(QObject):
    changed = Signal()
    completed = Signal(int, object)

    def __init__(self, check: Callable, parent: QObject | None = None):
        super().__init__(parent)
        self._check = check
        self._context: tuple | None = None
        self._epoch = 0
        self._busy = False
        self._stopped = False
        self.admission: dict[str, Any] | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.refresh)
        self.completed.connect(self._complete, Qt.ConnectionType.QueuedConnection)

    def set_context(self, active: bool, state: dict, *, clock_busy: bool = False) -> None:
        active = active and not self._stopped
        context = (active, deepcopy(state), clock_busy)
        if context == self._context:
            return
        self._context = context
        self._epoch += 1
        self.admission = None
        if active and not clock_busy:
            self._timer.start()
            self.refresh()
        else:
            self._timer.stop()

    def stop(self) -> None:
        self._stopped = True
        self.set_context(False, {})

    @Slot()
    def refresh(self) -> None:
        if (self._busy or self._stopped or self._context is None
                or not self._context[0] or self._context[2]):
            return
        self._busy = True
        epoch = self._epoch
        state = deepcopy(self._context[1])
        def compute() -> None:
            try:
                result = self._check(state)
            except Exception:
                result = {'allowed': False, 'reason_code': 'UPDATE_SAFETY_STATE_UNAVAILABLE',
                          'reason_text': '暂时无法读取本地安全状态，请检查本机存储后重试'}
            try:
                self.completed.emit(epoch, result)
            except RuntimeError:
                pass  # Window was destroyed. No business state needs cleanup.
        try:
            Thread(target=compute, name='update-admission-preview', daemon=True).start()
        except RuntimeError:
            self._complete(epoch, {'allowed': False, 'reason_code': 'UPDATE_SAFETY_STATE_UNAVAILABLE',
                                   'reason_text': '暂时无法读取本地安全状态，请稍后重试'})

    @Slot(int, object)
    def _complete(self, epoch: int, result: object) -> None:
        self._busy = False
        if epoch != self._epoch or self._stopped:
            return
        if not isinstance(result, dict) or not isinstance(result.get('allowed'), bool):
            result = {'allowed': False, 'reason_code': 'UPDATE_SAFETY_STATE_UNAVAILABLE',
                      'reason_text': '暂时无法读取本地安全状态，请稍后重试'}
        if result != self.admission:
            self.admission = dict(result)
            self.changed.emit()
