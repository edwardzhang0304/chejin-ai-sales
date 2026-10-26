"""Query failure through production HTTP settlement and restart, no real WeChat."""
import json
from pathlib import Path

import pytest
from app.core.database import SessionLocal
from app.models.task import Task
from app.models.worker import Worker
from test_hook_fault_recovery_http import (clean, local_storage, runtime_for, identity_client, lab, client)
from chejin_worker_client import storage
from chejin_worker_client.api import ApiError
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from apps.wechat_ai_customer_service.tests.test_hook_c1_flow import ReplayUI


def query_ui(prep, guard, *, cleanup=True, fail=True):
    class UI(ReplayUI):
        def guard(self, *args):
            guard()
            super().guard(*args)
        def capture(self, label):
            frame = super().capture(label)
            if label == 'lookup':
                for row in frame.rows:
                    row['text'] = row['text'].replace('13800000003', ('Q' if fail else '') + prep['primary_phone'])
            return frame
    ui = UI()
    ui.cleanup = cleanup
    ui.kind = 'not_found'
    return ui


@pytest.mark.parametrize('cleanup', [True, False])
def test_query_failure_settles_original_and_only_unsafe_cleanup_stops_all(identity_client, lab, tmp_path, cleanup):
    w, task, rt = runtime_for(identity_client, lab, tmp_path)
    seen = []
    def factory(prep, guard):
        ui = query_ui(prep, guard, cleanup=cleanup)
        seen.append(ui)
        return ui
    result = rt.invite(task, factory)
    out = result['outcome']
    assert out['error_code'] == 'SEARCH_QUERY_UNCONFIRMED'
    assert not out['submit_attempted']
    assert 'network_lookup' not in seen[0].actions
    assert out['cleanup'] == ('search_empty_verified' if cleanup else 'failed_preserve_business_outcome')
    root = rt.c1.namespace(task['id'])
    before = (root/'outcome.json').read_bytes()
    assert not (root/'submit-attempt.json').exists()
    assert not storage.load_runtime_control()['inflight_flow_id']
    with SessionLocal() as db:
        assert db.get(Task, task['id']).status == 'failed'
        assert db.get(Task, task['id']).error_code == ('SEARCH_QUERY_UNCONFIRMED' if cleanup else 'WECHAT_UI_LAYOUT_UNRESOLVED')
        worker = db.get(Worker, w['id'])
        assert worker.run_status == ('running' if cleanup else 'faulted')
        assert not worker.inflight_flow_state
    if cleanup:
        created = client.post('/api/leads', json={'customer_name':'Synthetic next query', 'phones':['13800000004']})
        assert created.status_code == 200, created.text
        pulled = client.get(f"/api/workers/{w['id']}/tasks/pull", headers={
            'X-Worker-Token': w['worker_token'], 'X-Client-Instance-Id': w['instance']})
        next_task = pulled.json()['data']['task']
        assert next_task and next_task['id'] != task['id']
        rt.invite(next_task, lambda prep, guard: query_ui(prep, guard, fail=False))
        with SessionLocal() as db:
            assert db.get(Task, next_task['id']).error_code == 'PHONE_NOT_FOUND'
            assert db.get(Worker, w['id']).run_status == 'running'
            assert not db.get(Worker, w['id']).inflight_flow_state
    assert (root/'outcome.json').read_bytes() == before
    assert len(seen) == 1


@pytest.mark.parametrize('lost_after_commit', [False, True])
def test_query_failure_lost_receipt_restarts_without_ui_or_automatic_resume(identity_client, lab, tmp_path, monkeypatch, lost_after_commit):
    w, task, rt = runtime_for(identity_client, lab, tmp_path)
    call = HookFormalClient.call
    def disconnected(self, credentials, method, path, body=None, **kwargs):
        if path.endswith('/fail'):
            if lost_after_commit: call(self, credentials, method, path, body, **kwargs)
            raise ApiError('HOOK_BACKEND_NETWORK_FAILED', 'controlled lost receipt', 0)
        return call(self, credentials, method, path, body, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(HookFormalClient, 'call', disconnected)
        with pytest.raises(ApiError): rt.invite(task, query_ui)
    assert storage.load_runtime_control()['inflight_flow_id'] == task['id']
    root = rt.c1.namespace(task['id'])
    before = {p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
    rt.c1_results.set_run_status('paused')
    fresh = HookFormalRuntime(rt.client, rt.credentials, rt.gateway, rt.prepared, rt.private_root)
    fresh.active_flow_id = fresh.active_task_id = task['id']
    inventory = fresh.client.call(fresh.credentials, 'POST', f"/workers/{w['id']}/wechat/native-work",
        {**fresh.account, 'retained_flow_id':task['id']})
    d = HookBusinessDispatcher(rt.client, rt.private_root, None, None, automatic_transcription_disabled=lambda:True)
    d.runtime = fresh
    monkeypatch.setattr(fresh.c1, 'execute_prepared', lambda *a, **kw:pytest.fail('recovery reopened UI'))
    d._recover_invitation(inventory['inflight_task'])
    with SessionLocal() as db:
        assert db.get(Task, task['id']).error_code == 'SEARCH_QUERY_UNCONFIRMED'
        assert db.get(Worker, w['id']).run_status == 'paused'
        assert not db.get(Worker, w['id']).inflight_flow_state
    assert not storage.load_runtime_control()['inflight_flow_id']
    assert all((root/name).read_bytes() == value for name,value in before.items())
    assert not (root/'submit-attempt.json').exists()
