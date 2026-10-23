"""One UI close path for update and normal exit. No process termination."""
from __future__ import annotations

import time

WRITER_STOP_SECONDS = 25.0
OLD_EXIT_SECONDS = 30.0  # Remaining time is reserved for Qt/process teardown.


def request_update_close(window) -> None:
    window._closing_for_update = True
    window.close()  # Qt routes this through the same closeEvent as normal exit.


def prepare_window_close(window) -> bool:
    if getattr(window, "_shutdown_complete", False):
        return True
    if getattr(window, "_shutdown_in_progress", False):
        return False
    updating = getattr(window, "_closing_for_update", False)
    coordinator = window.update_coordinator
    deadline = time.monotonic()
    window._shutdown_in_progress = True

    def stage(name):
        if updating:
            try:
                coordinator.record_shutdown_stage(name, remaining_seconds=deadline - time.monotonic())
            except Exception:
                pass  # Evidence failure cannot grant or deny the close itself.

    try:
        deadline = coordinator.shutdown_deadline() if updating else time.monotonic() + 80
        if getattr(window, "_update_cancellation_pending", False):
            if not coordinator.ensure_shutdown_cancelled():
                raise RuntimeError("UPDATE_SHUTDOWN_CANCEL_UNCONFIRMED")
            window._update_cancellation_pending = False
        coordinator.stop()
        stage("writers")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("UPDATE_WRITERS_NOT_STOPPED")
        if updating or window.runner.hook_services is not None:
            window.runner.stop_for_update(remaining, on_stage=stage)
        else:
            window.runner.stop()
        stage("storage_maintenance")
        service = getattr(window, "storage_maintenance", None)
        if service is not None and not service.stop(max(0, deadline - time.monotonic())):
            raise RuntimeError("UPDATE_WRITERS_NOT_STOPPED")
        if time.monotonic() >= deadline:
            raise RuntimeError("UPDATE_WRITERS_NOT_STOPPED")
        if updating:
            coordinator.report_normal_exit_result(stopped=True)
        window._shutdown_complete = True
        stage("qt_close_requested")  # Not a claim that the OS process has exited.
        return True
    except Exception as exc:
        if updating:
            try:
                coordinator.record_shutdown_stage("failed", exception_type=type(exc).__name__)
            except Exception:
                pass
            window._update_cancellation_pending = True
            try:
                coordinator.report_normal_exit_result(stopped=False)
                window._update_cancellation_pending = False
            except Exception:
                # Disk failure must not escape Qt closeEvent with its default
                # accepted event. The process and original data stay owned here.
                pass
            finally:
                # A subsequent close gets a normal-exit budget, but may only
                # proceed after cancellation of this updater is authenticated.
                window._closing_for_update = False
        try:
            window.error_signal.emit("本机记录还在保存，退出未完成；原数据已保留。")
        except Exception:
            pass
        return False
    finally:
        window._shutdown_in_progress = False
