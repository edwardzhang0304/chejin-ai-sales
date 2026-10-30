"""Real reset/bind HTTP and client lifecycle; Windows/DLL and Qt shell are fixtures."""
from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest

from test_hook_account_client import identity_client
from test_hook_account_http import client, worker
from test_hook_gateway import lab
import test_web_ui_binding_behavior as ui_test
from test_hook_worker_services import until
from chejin_worker_client import storage
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.emergency_stop import reset_emergency_stop_for_tests
from chejin_worker_client.hook_connection import HookConnectionManager
from chejin_worker_client.hook_inbox import NativeInbox
from chejin_worker_client.hook_projection import NativeProjectionStore
from chejin_worker_client.hook_worker_services import HookWorkerServices
from chejin_worker_client.models import Binding
from chejin_worker_client.task_runner import TaskRunner


@pytest.mark.parametrize('restart', [False, True])
def test_reset_a_returns_to_bind_then_b_connects_without_losing_original_evidence(lab, identity_client, tmp_path, restart):
    reset_emergency_stop_for_tests()
    local, gateway, _, _, receiver, thread = lab
    receiver.shutdown(); thread.join(3); receiver.server_close()
    w = worker()
    manager = HookConnectionManager(gateway, identity_client,
        NativeInbox(max_body_bytes=4096, max_inbox_bytes=65536), receiver_timeout_seconds=1,
        on_persistence_failure=lambda: pytest.fail('persistence failed'))
    services = HookWorkerServices(manager, NativeProjectionStore(max_projection_bytes=65536, max_entries=20),
        intake_interval_seconds=.02, projection_batch_size=3)
    def prohibited(*args, **kwargs):
        pytest.fail('No send, AI or OCR action is authorized during rebind')
    binding = Binding(w['id'], w['worker_token'], w['instance'], 'paused')
    storage.save_binding(binding)
    # Retained action files must not be touched by credential/UI reset.
    receipt = tmp_path / 'original-send.json'
    receipt.write_text('{"result":"unknown","account":"A"}')
    with ui_test._headless_web_ui_module() as module:
        window = ui_test.WebUiBindingBehaviorTest._window(module, binding)
        window._position_next_to_wechat_once = Mock()
        window._publish = Mock()
        window.api = WorkerApiClient(identity_client.base_url)
        statuses = []
        errors = []
        def status(value):
            statuses.append(value)
            window.on_status(value)
        runner = TaskRunner(window.api, SimpleNamespace(probe=prohibited),
            on_profile=window.on_profile, on_status=status, on_step=prohibited,
            on_task=prohibited, on_result=prohibited, on_error=errors.append, hook_services=services)
        window.runner = runner
        runner.poll_interval_seconds = .03
        runner._maybe_cleanup_artifacts = lambda **kwargs: None
        runner.binding = binding
        services.attach(binding, runner._hook_persistence_failed)
        local.wxid = 'wxid_sales_A'
        try:
            runner.tick_once()
            original_scope = manager.prepared.registration.scope
            # Seed existing durable evidence directly: macOS keeps a served
            # socket in TIME_WAIT; do not weaken production port exclusivity
            # just to reuse the same callback port in this cross-platform test.
            manager.inbox.persist(json.dumps({
                'ServerPort': str(original_scope.hook_api_port), 'selfwxid': original_scope.self_wxid,
                'sendorrecv': '2', 'msgnumber': '1', 'msglist': [{'index': '1', 'msgtype': '1',
                'msgsvrid': '6594133108973694888', 'msg': 'synthetic original message',
                'fromid': 'wxid_customer_A', 'toid': original_scope.self_wxid}]}).encode(), original_scope)
            response = client.post(f"/api/workers/{w['id']}/reset-binding", json={})
            assert response.status_code == 200
            new_token = response.json()['data']['worker_token']
            if restart:
                services.close()
                runner.binding = storage.load_binding()
                window.binding = runner.binding
                services.attach(runner.binding, runner._hook_persistence_failed)
            local.wxid = 'wxid_sales_B'
            runner.tick_once()
            assert statuses.count('invalid') == 1
            assert runner.stop_event.is_set() and manager.prepared is None
            assert storage.load_binding() is None and window._screen() == 'bind'
            window.bind_worker(w['id'], new_token)
            assert window.bind_error == ''
            try:
                until(lambda: manager.prepared is not None)
            except AssertionError:
                pytest.fail(str((statuses, errors, services._last_error, window.bind_error)))
            assert window.bind_error == ''
            assert manager.prepared.account.self_wxid == 'wxid_sales_B'
            assert manager.prepared.account.account_epoch != original_scope.account_epoch
            assert window.binding.run_status == 'paused'
            assert storage.load_binding().worker_token == new_token
            with storage.db_connection() as db:
                rows = db.execute('SELECT self_wxid, account_epoch FROM hook_raw_inbox').fetchall()
                assert len(rows) == 1
                assert rows[0]['self_wxid'] == 'wxid_sales_A'
                assert rows[0]['account_epoch'] == original_scope.account_epoch
            assert receipt.read_text() == '{"result":"unknown","account":"A"}'
        finally:
            runner.stop_for_update(5)
            reset_emergency_stop_for_tests()
