from __future__ import annotations

import unittest
from unittest import mock

from chejin_worker_client import main
from chejin_worker_client.single_instance import (
    SingleInstanceAlreadyRunning,
    acquire_single_instance,
)


class _FakeKernel32:
    def __init__(self, handle: int = 101) -> None:
        self.handle = handle
        self.created_names: list[str] = []
        self.closed_handles: list[int] = []

    def CreateMutexW(self, _security: object, _owned: bool, name: str) -> int:
        self.created_names.append(name)
        return self.handle

    def CloseHandle(self, handle: int) -> bool:
        self.closed_handles.append(handle)
        return True


class SingleInstanceTest(unittest.TestCase):
    def test_cli_output_is_safe_without_console_stream(self):
        from chejin_worker_client.main import emit_cli_output

        with mock.patch("chejin_worker_client.main.sys.stdout", None):
            emit_cli_output("不会写入窗口版控制台")

    def test_windows_first_instance_holds_mutex_until_release(self):
        kernel32 = _FakeKernel32()
        guard = acquire_single_instance(
            platform_name="win32",
            kernel32=kernel32,
            get_last_error=lambda: 0,
            set_last_error=lambda _value: None,
        )

        self.assertEqual(len(kernel32.created_names), 1)
        self.assertEqual(kernel32.closed_handles, [])
        guard.release()
        self.assertEqual(kernel32.closed_handles, [101])

    def test_windows_second_instance_is_rejected_atomically(self):
        kernel32 = _FakeKernel32()

        with self.assertRaises(SingleInstanceAlreadyRunning):
            acquire_single_instance(
                platform_name="win32",
                kernel32=kernel32,
                get_last_error=lambda: 183,
                set_last_error=lambda _value: None,
            )

        self.assertEqual(kernel32.closed_handles, [101])

    def test_non_windows_does_not_create_windows_mutex(self):
        guard = acquire_single_instance(platform_name="darwin")
        self.assertIsNone(guard.handle)
        guard.release()

    def test_main_does_not_start_ui_when_another_instance_exists(self):
        with (
            mock.patch.object(main.sys, "argv", ["chejin-worker-client"]),
            mock.patch.object(
                main,
                "acquire_single_instance",
                side_effect=SingleInstanceAlreadyRunning,
            ),
            mock.patch.object(main, "notify_already_running") as notify,
            mock.patch.object(main, "bootstrap_qt_plugins") as bootstrap,
        ):
            result = main.main()

        self.assertEqual(result, 2)
        notify.assert_called_once_with()
        bootstrap.assert_not_called()

    def test_ocr_worker_dispatch_runs_before_qt_and_instance_guard(self):
        with (
            mock.patch.object(
                main.sys,
                "argv",
                ["chejin-worker-client", "--omniauto-ocr-worker"],
            ),
            mock.patch.object(
                main,
                "run_bundled_omniauto_ocr_worker",
                return_value=0,
            ) as worker,
            mock.patch.object(main, "acquire_single_instance") as acquire,
            mock.patch.object(main, "bootstrap_qt_plugins") as bootstrap,
        ):
            result = main.main()

        self.assertEqual(result, 0)
        worker.assert_called_once_with()
        acquire.assert_not_called()
        bootstrap.assert_not_called()

    def test_ocr_probe_dispatch_runs_before_qt_and_instance_guard(self):
        with (
            mock.patch.object(
                main.sys,
                "argv",
                ["chejin-worker-client", "--omniauto-ocr-probe"],
            ),
            mock.patch.object(
                main,
                "run_bundled_omniauto_ocr_probe",
                return_value=0,
            ) as probe,
            mock.patch.object(main, "acquire_single_instance") as acquire,
            mock.patch.object(main, "bootstrap_qt_plugins") as bootstrap,
        ):
            result = main.main()

        self.assertEqual(result, 0)
        probe.assert_called_once_with()
        acquire.assert_not_called()
        bootstrap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
