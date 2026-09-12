"""Independent continuation of the submitted one-shot failure test.

Only one transport request is lost; subsequent HTTP is normal. Never finish a
task, clear a flow or operate WeChat on behalf of the production Worker.
"""
import json
from pathlib import Path
import shutil

import pytest
from sqlalchemy import select
from test_lead_followup_eligibility import isolated_db, http_api, fixture_rows
import test_worker_failure_consistency as submitted
from app.core.database import SessionLocal
from app.models.task import Task
from app.models.worker import Worker
from app.enums import ContactType
from app.services.lead_service import _contact_model
from app.services import contact_utils


def snapshot(worker_id):
    with SessionLocal() as db:
        worker = db.get(Worker, worker_id)
        return {"status": worker.run_status, "flow": worker.inflight_flow_state,
                "tasks": [{"id": task.id, "status": task.status, "code": task.error_code}
                          for task in db.scalars(select(Task).where(Task.worker_id == worker_id))]}


@pytest.mark.parametrize("interruption", ["fail:before", "fail:after"])
def test_one_lost_failure_request_eventually_settles_without_ui(http_api, tmp_path, monkeypatch, interruption):
    worker, rows = fixture_rows()
    with SessionLocal() as db:
        for index, row in enumerate(rows):
            db.add(_contact_model(row["lead_id"], ContactType.phone,
                                 contact_utils.normalize_phone(f"1380000777{index}"), True))
            db.add(Task(lead_id=row["lead_id"], worker_id=worker["id"], task_type="add_friend", status="pending"))
        db.commit()
    base = http_api.get("/healthz").url.removesuffix("/healthz") + "/api"
    request = {"base_url": base, "worker_id": worker["id"], "token": worker["worker_token"],
               "code": "WECHAT_UI_LAYOUT_UNRESOLVED", "interruption": interruption}
    live = submitted.run_worker(tmp_path, r'''
import time
runner.tick_once()
before = load_runtime_control()
deadline = time.monotonic() + 9
ticks = 0
while time.monotonic() < deadline:
    runner.tick_once()
    ticks += 1
    time.sleep(.4)
print(json.dumps({'before':before,'runtime':load_runtime_control(),'http':events,
    'injected':injected,'calls':bridge.calls,'ticks':ticks,'status':binding.run_status,
    'recovery':runner._check_fault_recovery()},default=str))
''', request)
    live["backend"] = snapshot(worker["id"])
    (tmp_path / "live-evidence.json").write_text(json.dumps(live, ensure_ascii=False, indent=2))
    for name in ("worker.py", "worker.stdout", "worker.stderr", "input.json"):
        shutil.copy2(tmp_path / name, tmp_path / ("live-" + name))
    # Restart the real TaskRunner over exactly the same SQLite, without
    # replacing its remembered faulted binding with a synthetic running one.
    needle = "binding=Binding(request['worker_id'],request['token'],'followup-test',run_status='running')\nsave_binding(binding)"
    assert submitted.COMMON.count(needle) == 1
    monkeypatch.setattr(submitted, "COMMON", submitted.COMMON.replace(needle, "binding=load_binding()\nassert binding.run_status=='faulted'"))
    request["interruption"] = ""
    restarted = submitted.run_worker(tmp_path, r'''
import time
runner.start(binding)
deadline = time.monotonic() + 12
while load_runtime_control().get('inflight_flow_id') and time.monotonic() < deadline:
    time.sleep(.2)
result={'runtime':load_runtime_control(),'http':events,'calls':bridge.calls,
        'status':binding.run_status,'recovery':runner.fault_recovery_state()}
runner.stop_for_update(timeout_seconds=5)
print(json.dumps(result,default=str))
''', request)
    restarted["backend"] = snapshot(worker["id"])
    (tmp_path / "restart-evidence.json").write_text(json.dumps(restarted, ensure_ascii=False, indent=2))
    assert live["calls"] == 1 and restarted["calls"] == 0
    assert live["status"] == restarted["status"] == "faulted"
    assert not restarted["backend"]["flow"].get("flow_id"), restarted
    assert not restarted["runtime"].get("inflight_flow_id"), restarted
    assert sum(t["status"] == "failed" for t in restarted["backend"]["tasks"]) == 1
    assert sum(t["status"] == "pending" for t in restarted["backend"]["tasks"]) == 1


# These cases leave immediately after the interrupted first attempt. In
# restart cases, settlement is owned by TaskRunner.start's production threads.
@pytest.mark.parametrize("mode,interruption,expired", [
    ("restart", "fail:before", False), ("restart", "fail:after", False),
    ("restart", "fail:before", True),
    ("restart", "confirm-save:before", False),
    ("restart", "confirm-save:after", False),
    ("restart", "persisted-crash", False),
])
def test_original_failure_receipt_survives_loss(http_api, tmp_path, monkeypatch, mode, interruption, expired):
    from datetime import timedelta
    from app.models.base import utcnow
    from app.models.task import TaskEvent
    from app.models.audit import OperationLog
    worker, rows = fixture_rows()
    with SessionLocal() as db:
        for index, row in enumerate(rows):
            db.add(_contact_model(row["lead_id"], ContactType.phone,
                                 contact_utils.normalize_phone(f"1380000666{index}"), True))
            db.add(Task(lead_id=row["lead_id"], worker_id=worker["id"], task_type="add_friend", status="pending"))
        db.commit()
    base = http_api.get("/healthz").url.removesuffix("/healthz") + "/api"
    request = {"base_url": base, "worker_id": worker["id"], "token": worker["worker_token"],
               "code": "WECHAT_UI_LAYOUT_UNRESOLVED", "interruption": interruption}
    # Capture original payload and fencing headers, including the lost request.
    common = submitted.COMMON.replace("    interruption=request.get('interruption', '')", """
    if prepared.url.endswith('/fail'):
        attempts.append({'body': json.loads(prepared.body), 'flow': prepared.headers.get('X-Inflight-Flow-Id'),
                         'fence': prepared.headers.get('X-Task-Lease-Fencing-Token')})
    interruption=request.get('interruption', '')""").replace("events=[]", "events=[]\nattempts=[]")
    assert common != submitted.COMMON
    monkeypatch.setattr(submitted, "COMMON", common)
    first = submitted.run_worker(tmp_path, r"""
import chejin_worker_client.task_runner as tr
from chejin_worker_client.storage import load_c2_state
if request['interruption'].startswith('confirm-save:'):
    original_save = tr.save_c2_state
    def save(key, value, **kwargs):
        hit = isinstance(value, dict) and value.get('task_failure_confirmed') is True and not injected
        if hit:
            injected.append({'at': request['interruption']})
            if request['interruption'].endswith(':after'):
                original_save(key, value, **kwargs)
                emit_first()
                import os
                os._exit(0)
            raise OSError('controlled local confirmation persistence failure')
        return original_save(key, value, **kwargs)
    tr.save_c2_state = save
def emit_first():
    flow = load_runtime_control().get('inflight_flow_id')
    print(json.dumps({'runtime': load_runtime_control(), 'status': binding.run_status,
        'calls': bridge.calls, 'http': events, 'attempts': attempts, 'injected': injected,
        'receipt': load_c2_state(runner._inflight_finish_receipt_key(flow))}, default=str), flush=True)
if request['interruption'] == 'persisted-crash':
    import os
    original_transport = api.session.send
    def die_before_failure(prepared, **kwargs):
        if prepared.url.endswith('/fail'):
            injected.append({'at':'persisted-crash'})
            attempts.append({'body':json.loads(prepared.body), 'flow':prepared.headers.get('X-Inflight-Flow-Id'),
                             'fence':prepared.headers.get('X-Task-Lease-Fencing-Token')})
            emit_first()
            os._exit(0)
        return original_transport(prepared, **kwargs)
    api.session.send = die_before_failure
runner.tick_once()
emit_first()
""", request)
    first['backend'] = snapshot(worker['id'])
    (tmp_path/'first-evidence.json').write_text(json.dumps(first, indent=2))
    assert first['calls'] == 1 and first['status'] == 'faulted'
    assert len(first['injected']) == 1
    assert first['runtime']['inflight_flow_id']
    receipt = first['receipt']['task_failure']
    assert receipt['error_code'] == 'WECHAT_UI_LAYOUT_UNRESOLVED'
    assert receipt['failure_step'] == 'window_layout_calibration'
    assert receipt['failure_remark'] == 'controlled desktop boundary; no physical click'
    for name in ('worker.py', 'worker.stdout', 'worker.stderr'):
        shutil.copy2(tmp_path/name, tmp_path/('first-'+name))
    if expired:
        with SessionLocal() as db:
            task = db.get(Task, receipt['task_id'])
            assert task.status == 'running'
            task.lease_expires_at = utcnow() - timedelta(minutes=1)
            db.commit()
    request['interruption'] = ''
    needle = "binding=Binding(request['worker_id'],request['token'],'followup-test',run_status='running')\nsave_binding(binding)"
    assert common.count(needle) == 1
    monkeypatch.setattr(submitted, 'COMMON', common.replace(needle, "binding=load_binding()\nassert binding.run_status=='faulted'"))
    program = r"""
import time
runner.heartbeat_interval_seconds = 1
runner.start(binding)
deadline = time.monotonic() + 15
while (load_runtime_control().get('inflight_flow_id') or not runner.fault_recovery_state()['ready']) and time.monotonic() < deadline:
    time.sleep(.1)
result = {'runtime': load_runtime_control(), 'status': binding.run_status,
      'calls': bridge.calls, 'http': events, 'attempts': attempts,
      'recovery': runner.fault_recovery_state()}
runner.stop_for_update(timeout_seconds=5)
print(json.dumps(result, default=str))
"""
    recovered = submitted.run_worker(tmp_path, program, request)
    recovered['backend'] = snapshot(worker['id'])
    with SessionLocal() as db:
        failed = db.get(Task, receipt['task_id'])
        assert (failed.status, failed.error_code, failed.failure_step, failed.failure_remark) == (
            'failed', receipt['error_code'], receipt['failure_step'], receipt['failure_remark'])
        assert failed.lease_owner_worker_id is None and failed.lease_expires_at is None
        assert failed.lease_fencing_token == receipt['lease_fencing_token']
        task_events = list(db.scalars(select(TaskEvent).where(TaskEvent.task_id == failed.id, TaskEvent.event_type == 'failed')))
        logs = list(db.scalars(select(OperationLog).where(OperationLog.target_id == failed.id, OperationLog.event_type == 'task_failure_receipt_confirmed')))
        assert len(task_events) == len(logs) == 1
    (tmp_path/'recovered-evidence.json').write_text(json.dumps(recovered, indent=2))
    assert recovered['calls'] == 0 and recovered['status'] == recovered['backend']['status'] == 'faulted', recovered
    assert not recovered['runtime']['inflight_flow_id'] and not recovered['backend']['flow'].get('flow_id'), recovered
    assert sorted(t['status'] for t in recovered['backend']['tasks']) == ['failed', 'pending'], recovered
    assert not any(e['url'].endswith('/claim') for e in recovered['http'])
    for attempt in first['attempts'] + recovered['attempts']:
        assert attempt == first['attempts'][0]  # Identical receipt, including original fence/Flow.
    if mode == 'restart':
        assert recovered['recovery']['ready'], recovered
    if expired:
        # Click the existing start-intake handler only after automatic recovery
        # reports ready. The next customer's desktop boundary is controlled.
        resumed = submitted.run_worker(tmp_path, r'''
import time
runner.heartbeat_interval_seconds = 1
runner.start(binding)
deadline = time.monotonic()+15
while not runner.fault_recovery_state()['ready'] and time.monotonic()<deadline:
    time.sleep(.1)
assert runner.fault_recovery_state()['ready'], runner.fault_recovery_state()
accepted = runner.set_run_status('running')
assert accepted
while (bridge.calls != 1 or load_runtime_control().get('inflight_flow_id')) and time.monotonic()<deadline:
    time.sleep(.1)
result = {'accepted': accepted, 'calls': bridge.calls, 'http': events,
          'status': binding.run_status, 'runtime': load_runtime_control()}
runner.stop_for_update(timeout_seconds=5)
print(json.dumps(result, default=str))
''', request)
        resumed['backend'] = snapshot(worker['id'])
        (tmp_path/'explicit-resume-evidence.json').write_text(json.dumps(resumed, indent=2))
        assert resumed['calls'] == 1, resumed
        claims = [e for e in resumed['http'] if e['url'].endswith('/claim')]
        assert len(claims) == 1 and claims[0]['status'] == 200, resumed
        assert receipt['task_id'] not in claims[0]['url'], resumed
        assert all(t['status'] == 'failed' for t in resumed['backend']['tasks']), resumed
        assert not resumed['runtime']['inflight_flow_id'], resumed
