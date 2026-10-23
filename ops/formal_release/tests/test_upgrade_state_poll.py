"""Exercise the real polling closure without launching or replacing any EXE."""
import ast
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / 'worker-client/scripts/run-windows-client-upgrade-test.py'


def production_poll(tmp_path, reader):
    tree = ast.parse(SCRIPT.read_text())
    finished = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'finished')
    waiter = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'wait_for')
    state = tmp_path / 'update-state.json'
    state.write_text('{}')
    report = {}
    namespace = {'state_path': state, 'report': report, 'read_json': reader,
                 'terminal_update_ui': Mock(return_value=True), 'debug_port': 1, 'old': Mock(), 'finish_deadline': 300,
                 'time': Mock(monotonic=Mock(return_value=0))}
    exec(compile(ast.Module(body=[finished, waiter], type_ignores=[]), str(SCRIPT), 'exec'), namespace)
    return namespace, report, tree


def success():
    return {'state': 'succeeded', 'result_reconciled': True, 'status_restore_pending': False}


def test_transient_access_retries_then_requires_reconciled_success(tmp_path):
    reader = Mock(side_effect=[PermissionError(13, 'busy'), {'state': 'downloading'}, success()])
    env, report, _ = production_poll(tmp_path, reader)
    assert env['finished']() is False
    assert env['finished']() is False
    assert env['finished']() == success()
    assert report['state_read_contention_count'] == 1
    assert reader.call_count == 3


def test_persistent_denial_reaches_original_deadline_never_passes(tmp_path):
    env, report, tree = production_poll(tmp_path, Mock(side_effect=PermissionError(13, 'busy')))
    clock = Mock()
    clock.monotonic.side_effect = [0, 0, 299.9, 300]
    env['time'] = clock
    with pytest.raises(AssertionError, match='Timed out: original GUI full upgrade'):
        env['wait_for'](env['finished'], 'original GUI full upgrade', timeout=300)
    assert report['state_read_contention_count'] == 2
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == 'wait_for' and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id == 'finished')
    assert next(k.value.value for k in call.keywords if k.arg == 'timeout') == 300


@pytest.mark.parametrize('state', ['failed', 'rolled_back', 'rollback_failed'])
def test_product_failure_is_not_hidden_after_access_contention(tmp_path, state):
    env, report, _ = production_poll(tmp_path, Mock(side_effect=[PermissionError(13, 'busy'), {'state': state, 'result_code': 'REAL_FAILURE'}]))
    assert env['finished']() is False
    with pytest.raises(AssertionError, match='Original GUI update failed: REAL_FAILURE'):
        env['finished']()
    assert report['failure_update_state']['state'] == state


@pytest.mark.parametrize('error', [OSError(5, 'I/O failure'), json.JSONDecodeError('invalid', '{', 0)])
def test_non_access_errors_still_fail(tmp_path, error):
    env, report, _ = production_poll(tmp_path, Mock(side_effect=error))
    with pytest.raises(type(error)):
        env['finished']()
    assert 'state_read_contention_count' not in report


def test_reconciliation_and_restore_guards_unchanged(tmp_path):
    env, _, _ = production_poll(tmp_path, Mock(side_effect=[{'state': 'succeeded', 'result_reconciled': False},
        {'state': 'succeeded', 'result_reconciled': True, 'status_restore_pending': True}]))
    assert env['finished']() is False
    assert env['finished']() is False


def test_active_ui_never_opens_live_state_file(tmp_path):
    reader = Mock(return_value=success())
    env, report, _ = production_poll(tmp_path, reader)
    env['terminal_update_ui'].return_value = False
    for _ in range(20):
        assert env['finished']() is False
    reader.assert_not_called()
    env['terminal_update_ui'].return_value = True
    assert env['finished']() == success()
    reader.assert_called_once()


def test_late_state_read_cannot_grant_success_after_deadline(tmp_path):
    env, _, _ = production_poll(tmp_path, Mock(return_value=success()))
    env['time'].monotonic.return_value = 301
    assert env['finished']() is False
