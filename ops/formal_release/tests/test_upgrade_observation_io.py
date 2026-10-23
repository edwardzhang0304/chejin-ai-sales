"""Observe actual UI terminal text before touching updater-owned files."""
import ast
import io
import json
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[3] / 'worker-client/scripts/run-windows-client-upgrade-test.py'


class TerminalUIObservationTests(unittest.TestCase):
    def setUp(self):
        node = next(n for n in ast.parse(SCRIPT.read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'terminal_update_ui')
        self.view = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=self.view)
        context.__exit__ = Mock(return_value=False)
        self.factory, self.clock = Mock(return_value=context), Mock()
        self.old, self.report = Mock(), {}
        self.old.poll.return_value = None
        self.clock.monotonic.return_value = 10.0
        namespace = dict(QtPage=self.factory, time=self.clock)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SCRIPT), 'exec'), namespace)
        self.observe = namespace['terminal_update_ui']
        self.connection_error = type('ConnectionClosed', (Exception,), {})
        self.handshake_error = type('InvalidMessage', (Exception,), {})
        modules = {'websockets.exceptions': types.SimpleNamespace(
            ConnectionClosed=self.connection_error, InvalidMessage=self.handshake_error)}
        self.enterContext(patch.dict('sys.modules', modules))

    def check(self, text):
        self.view.evaluate.return_value = text
        return self.observe(1234, self.old, self.report, 300)

    def test_active_and_idle_status_do_not_trigger_state_read(self):
        for status in ['正在下载50%', '正在安装并重启客户端', '正在检查更新', '当前已是最新版本', '']:
            self.assertFalse(self.check('客户端版本号\nV0.10.13\n'+status))
        self.view.click_button.assert_not_called()

    def test_terminal_requires_stable_visible_result(self):
        text = '客户端版本号\nV0.10.14\n已更新到V0.10.14'
        self.assertFalse(self.check(text))
        self.clock.monotonic.return_value = 11.9
        self.assertFalse(self.check(text))
        self.clock.monotonic.return_value = 12.0
        self.assertTrue(self.check(text))

    def test_failure_results_trigger_original_durable_failure_checks(self):
        for failure in ['检查更新失败，请重试', '更新失败，已恢复到V0.10.13',
                        '更新失败，请查看本机更新日志', '后台未能及时停止，本次更新已结束']:
            self.report.clear()
            self.clock.monotonic.return_value = 20
            self.assertFalse(self.check('客户端版本号\n'+failure))
            self.clock.monotonic.return_value = 23
            self.assertTrue(self.check('客户端版本号\n'+failure))

    def test_restart_opens_settings_via_actual_button_only_after_old_exit(self):
        self.assertFalse(self.check('工作台'))
        self.view.click_button.assert_not_called()
        self.old.poll.return_value = 0
        self.assertFalse(self.check('工作台'))
        self.view.click_button.assert_called_once_with('打开设置', wait=False)

    def test_restart_connection_loss_resets_terminal_observation(self):
        self.report['terminal_ui_observation'] = {'text': 'old', 'since': 0}
        self.factory.side_effect = self.connection_error()
        self.assertFalse(self.observe(1234, self.old, self.report, 300))
        self.assertNotIn('terminal_ui_observation', self.report)

    def test_invalid_message_during_restart_retries_without_granting_success(self):
        self.report['terminal_ui_observation'] = {'text': 'old', 'since': 0}
        self.factory.side_effect = self.handshake_error('incomplete HTTP response')
        self.assertFalse(self.observe(1234, self.old, self.report, 300))
        self.assertNotIn('terminal_ui_observation', self.report)
        self.view.evaluate.assert_not_called()

    def test_invalid_message_at_deadline_cannot_grant_success(self):
        self.clock.monotonic.return_value = 300
        self.factory.side_effect = self.handshake_error('incomplete HTTP response')
        self.assertFalse(self.observe(1234, self.old, self.report, 300))

    def test_unexpected_ui_assertion_is_not_hidden(self):
        self.factory.side_effect = AssertionError('ambiguous pages')
        with self.assertRaisesRegex(AssertionError, 'ambiguous'):
            self.observe(1234, self.old, self.report, 300)

    def test_nonterminal_projection_resets_stability(self):
        self.assertFalse(self.check('客户端版本号\n已更新到V0.10.14'))
        self.assertFalse(self.check('客户端版本号\n正在安装并重启客户端'))
        self.clock.monotonic.return_value = 40
        self.assertFalse(self.check('客户端版本号\n已更新到V0.10.14'))


class PageStartupBudgetTests(unittest.TestCase):
    def setUp(self):
        node = next(n for n in ast.parse(SCRIPT.read_text()).body
                    if isinstance(n, ast.ClassDef) and n.name == 'QtPage')
        self.clock = Mock(monotonic=Mock(return_value=299.5))
        self.urlopen, self.connect = Mock(), Mock()
        namespace = dict(time=self.clock, json=json,
                         urllib=types.SimpleNamespace(request=types.SimpleNamespace(urlopen=self.urlopen)))
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SCRIPT), 'exec'), namespace)
        self.Page = namespace['QtPage']
        self.enterContext(patch.dict('sys.modules', {
            'websockets.sync.client': types.SimpleNamespace(connect=self.connect)}))

    def response(self, pages):
        self.urlopen.return_value = io.StringIO(json.dumps(pages))

    def test_restart_empty_page_is_retryable_without_opening_socket(self):
        self.response([])
        with self.assertRaises(ConnectionError):
            self.Page(1234, deadline=300)
        self.connect.assert_not_called()

    def test_multiple_pages_remains_hard_failure(self):
        self.response([{'type': 'page'}, {'type': 'page'}])
        with self.assertRaises(AssertionError):
            self.Page(1234, deadline=300)
        self.connect.assert_not_called()

    def test_transport_timeouts_use_remaining_overall_budget(self):
        self.response([{'type': 'page', 'webSocketDebuggerUrl': 'ws://localhost/synthetic'}])
        page = self.Page(1234, deadline=300)
        self.assertEqual(self.urlopen.call_args.kwargs['timeout'], 0.5)
        self.assertEqual(self.connect.call_args.kwargs['open_timeout'], 0.5)
        self.clock.monotonic.return_value = 300
        with self.assertRaises(TimeoutError):
            page.call('Runtime.evaluate')
        page.socket.send.assert_not_called()

    def test_nonwaiting_navigation_does_not_enter_inner_wait_loop(self):
        page = self.Page.__new__(self.Page)
        page.deadline = 300
        page.evaluate = Mock(return_value=None)
        page.call = Mock()
        self.assertFalse(page.click_button('打开设置', wait=False))
        page.evaluate.assert_called_once()
        page.call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
