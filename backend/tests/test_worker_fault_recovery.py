"""Synthetic records; real HTTP/PostgreSQL and Worker threads/SQLite.

Only desktop/WeChat physical boundaries are replaced. No live customer data or
physical sends. Persistence probes also observe subsequent real empty pulls or
heartbeats, so reporting a transition cannot hide resumed intake after failure.
"""
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.worker import Worker
from app.models.task import Task
from app.models.c3 import ReplyAction
from app.models.base import utcnow
from test_lead_followup_eligibility import http_api, isolated_db


def setup_worker(http):
    response = http.post("/api/workers", json={"worker_name": "Recovery test", "enabled": True})
    assert response.status_code == 200, response.text
    worker = response.json()["data"]
    path = f"/api/workers/{worker['id']}"
    headers = {"X-Worker-Token": worker["worker_token"]}
    assert http.post(path + "/client-bind", json={"worker_token": worker["worker_token"], "client_instance_id": "fault-test"}).status_code == 200
    assert http.post(path + "/heartbeat", headers=headers, json={
        "client_instance_id": "fault-test", "running_status": "idle", "rpa_component_status": "ready", "wechat_status": "logged_in",
    }).status_code == 200
    response = http.post(path + "/run-status", headers=headers, json={"client_instance_id": "fault-test", "run_status": "faulted"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["fault_recovery"]["ready"]
    return worker, path, headers, response.url.split("/api/")[0]


@pytest.mark.parametrize("blocker", ["none", "flow", "task", "lease", "sending", "unknown_send_result", "offline", "wechat", "lock"])
def test_server_rechecks_fault_recovery_under_worker_lock(http_api, blocker):
    worker, path, headers, _ = setup_worker(http_api)
    with SessionLocal() as db:
        w = db.get(Worker, worker["id"])
        if blocker == "flow": w.inflight_flow_state = {"flow_id": "old", "status": "draining"}
        if blocker == "task": db.add(Task(worker_id=w.id, task_type="add_friend", status="running"))
        if blocker == "lease": db.add(Task(worker_id=w.id, task_type="add_friend", status="failed", lease_owner_worker_id=w.id, lease_expires_at=utcnow()+timedelta(minutes=1)))
        if blocker in {"sending", "unknown_send_result"}: db.add(ReplyAction(batch_id="synthetic", conversation_id="synthetic", claimed_by_worker_id=w.id, status=blocker))
        if blocker == "offline": w.last_heartbeat_at = utcnow()-timedelta(minutes=5)
        if blocker == "wechat": w.wechat_status = "not_logged_in"
        if blocker == "lock": w.local_lock_summary = {"locked": True}
        db.commit()
    response = http_api.post(path + "/run-status", headers=headers, json={"client_instance_id": "fault-test", "run_status": "running", "recover_from_fault": True})
    assert response.status_code == (200 if blocker == "none" else 409), response.text
    with SessionLocal() as db:
        assert db.get(Worker, worker["id"]).run_status == ("running" if blocker == "none" else "faulted")
        if blocker in {"sending", "unknown_send_result"}:
            assert db.scalar(select(ReplyAction)).status == blocker


def test_old_client_cannot_downgrade_fault_by_bind_heartbeat_or_plain_start(http_api):
    worker, path, headers, _ = setup_worker(http_api)
    for status in ["running", "paused"]:
        response = http_api.post(path + "/run-status", headers=headers, json={"client_instance_id":"fault-test", "run_status":status})
        assert response.status_code == 409
        response = http_api.post(path + "/heartbeat", headers=headers, json={"client_instance_id":"fault-test", "running_status":"idle", "run_status":status})
        assert response.status_code == 200
        assert response.json()["data"]["run_status"] == "faulted"
    response = http_api.post(path + "/client-bind", json={"worker_token":worker["worker_token"], "client_instance_id":"fault-test"})
    assert response.json()["data"]["run_status"] == "faulted"
    for bad_headers, instance in [({"X-Worker-Token":"wrong"}, "fault-test"), (headers, "wrong")]:
        response = http_api.post(path + "/run-status", headers=bad_headers, json={"client_instance_id":instance,"run_status":"running","recover_from_fault":True})
        assert response.status_code == 401


WORKER_PROCESS = r'''
import json, sys, time
from pathlib import Path
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.storage import load_binding, save_binding, save_c2_ledger_terminal, update_install_business_blockers
request=json.loads(Path(sys.argv[1]).read_text())
mode=request["mode"]
probe_mode=request.get("persistence_probe", "")
status_posts=[]
new_work_requests=[]
heartbeat_samples=[]
save_failures=0
restore_failures=0
pause_failures=0
compensation_failures=0
profile_failures=0
boundaries=[]
from chejin_worker_client.storage import load_runtime_control
import chejin_worker_client.task_runner as runner_module
original_save=runner_module.save_binding
original_clear=runner_module.clear_runtime_pause
def snapshot(stage):
    return {"stage":stage,"memory":runner.binding.run_status,"sqlite":load_binding().run_status,
            "pause":load_runtime_control()["pause_requested"],"can_start":runner._can_start_new_flow(),
            "pending":runner._pending_run_status_sync,"persistence_pending":runner._run_status_persistence_pending}
def save_with_failure(candidate):
    global save_failures, restore_failures
    if probe_mode in {"save_before", "save_after", "restore_retry", "compensation_retry"} and candidate.run_status=="running" and not save_failures:
        save_failures+=1
        if probe_mode=="save_after": original_save(candidate)
        boundaries.append(snapshot("running_save_exception"))
        raise OSError("synthetic local save failure")
    if probe_mode=="restore_retry" and save_failures and candidate.run_status=="faulted" and restore_failures<2:
        restore_failures+=1
        boundaries.append(snapshot("fault_save_exception"))
        raise OSError("synthetic stop save failure")
    return original_save(candidate)
def clear_with_failure():
    global pause_failures
    if probe_mode in {"pause_before", "pause_after"} and not pause_failures:
        pause_failures+=1
        if probe_mode=="pause_after": original_clear()
        boundaries.append(snapshot("pause_clear_exception"))
        raise OSError("synthetic pause save failure")
    return original_clear()
if probe_mode:
    runner_module.save_binding=save_with_failure
    runner_module.clear_runtime_pause=clear_with_failure
class DesktopBoundary:
    def __init__(self): self.probes=0
    def probe(self): self.probes+=1; return ("ready", "logged_in")
    def sidecar_active(self): return False
class Transport(WorkerApiClient):
    def __init__(self, url): super().__init__(url); self.recoveries=0; self.pulls=0
    def _request(self, method, path, **kwargs):
        global compensation_failures
        payload=kwargs.get("json") or {}
        if probe_mode and path.endswith("/run-status"):
            status_posts.append({"status":payload.get("run_status"),"recover_from_fault":payload.get("recover_from_fault",False)})
        if probe_mode and path.split("?")[0].endswith(("/pull", "/read-targets")):
            new_work_requests.append(path.split("?")[0].rsplit("/",1)[-1])
        if probe_mode=="compensation_retry" and save_failures and payload.get("run_status")=="faulted" and not compensation_failures:
            compensation_failures+=1
            boundaries.append(snapshot("compensation_network_exception"))
            raise TimeoutError("synthetic compensation network failure")
        recovery=(kwargs.get("json") or {}).get("recover_from_fault") is True
        if path.endswith("/pull"): self.pulls+=1
        result=super()._request(method,path,**kwargs)
        if probe_mode and path.endswith("/heartbeat") and runner.binding and len(heartbeat_samples)<80:
            heartbeat_samples.append({**snapshot("heartbeat"),"backend":result.get("run_status")})
        if mode=="old_backend" and isinstance(result,dict): result.pop("fault_recovery",None)
        if recovery:
            self.recoveries+=1
            if mode=="lost_ack": raise TimeoutError("test dropped committed response")
            if mode=="new_fault": runner.set_run_status("faulted")
            if mode=="pause": runner.set_run_status("paused")
            if mode=="update": runner.block_new_work_for_update()
        return result
api=Transport(request["url"]+"/api")
bridge=DesktopBoundary()
errors=[]
runner=None
def profile_observer(profile):
    global profile_failures
    if probe_mode:
        if probe_mode=="profile_failure" and profile.run_status=="running" and not profile_failures:
            profile_failures+=1
            boundaries.append(snapshot("profile_callback_exception"))
            raise OSError("synthetic projection failure")
        return
    if profile.run_status=="running": runner.stop()
runner=TaskRunner(api,bridge,on_profile=profile_observer,on_status=lambda _:None,on_step=lambda _:None,on_task=lambda _:None,on_result=lambda _:None,on_error=errors.append)
binding=Binding(**request["binding"],run_status="faulted")
save_binding(binding)
if mode=="ledger":
    save_c2_ledger_terminal(conversation_id="synthetic",source_message_key="message",origin_read_run_id="read",dedupe_key=None,message_type="text",terminal_state="completed",ingest_state="waiting",result={"state":"completed"})
runner.start(load_binding())
try:
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        state=runner.fault_recovery_state()
        if state["ready"] or (mode in {"ledger","old_backend"} and runner._recovery_heartbeat_at): break
        time.sleep(.05)
    before=load_binding().run_status
    health_before=runner.post_update_runtime_health_snapshot()
    state_before=runner.fault_recovery_state()
    first=runner.set_run_status("running")
    second=runner.set_run_status("running")
    deadline=time.monotonic()+8
    while time.monotonic()<deadline and (runner._fault_recovery_processing or runner._fault_recovery_requested is not None): time.sleep(.05)
    # No implicit retry after network failure; another normal heartbeat retains fault.
    if mode in {"lost_ack","new_fault","pause","update"}: time.sleep(.35)
    if probe_mode:
        settled=snapshot("recovery_attempt_finished")
        heartbeats_after_attempt=len(heartbeat_samples)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            if probe_mode=="success" and api.pulls: break
            if probe_mode!="success" and len(heartbeat_samples)>=heartbeats_after_attempt+3 and not runner._pending_run_status_sync and not runner._run_status_persistence_pending: break
            time.sleep(.05)
    result={"state_before":state_before,"health_before":health_before,"before":before,"after":load_binding().run_status,"first":first,"second":second,"recoveries":api.recoveries,"probes":bridge.probes,"state":runner.fault_recovery_state(),"durable":update_install_business_blockers(),"threads":runner.post_update_runtime_health_snapshot()}
    if probe_mode:
        result.update({"mode":probe_mode,"settled":settled,"final":snapshot("final"),"errors":errors,
            "status_posts":status_posts,"new_work_requests":new_work_requests,"pulls":api.pulls,
            "save_failures":save_failures,"restore_failures":restore_failures,"pause_failures":pause_failures,
            "compensation_failures":compensation_failures,"profile_failures":profile_failures,
            "boundaries":boundaries,"heartbeat_samples":heartbeat_samples,
            "heartbeats_after_attempt":len(heartbeat_samples)-heartbeats_after_attempt})
finally:
    runner.stop_for_update(timeout_seconds=10)
print(json.dumps(result))
'''


@pytest.mark.parametrize("probe", ["success", "save_before", "save_after", "pause_before", "pause_after", "restore_retry", "compensation_retry", "profile_failure"])
def test_recovery_persistence_failure_never_reopens_intake(http_api, tmp_path, probe):
    worker, _, _, url = setup_worker(http_api)
    input_file = tmp_path / "input.json"
    input_file.write_text(json.dumps({"mode":"persistence", "persistence_probe":probe, "url":url,
        "binding":{"worker_id":worker["id"], "worker_token":worker["worker_token"], "client_instance_id":"fault-test"}}))
    result = subprocess.run([sys.executable, "-c", WORKER_PROCESS, str(input_file)], capture_output=True,
        text=True, timeout=40, env={**os.environ, "PYTHONPATH":str(Path(__file__).resolve().parents[2]/"worker-client"),
        "CHEJIN_WORKER_HOME":str(tmp_path/"worker"), "CHEJIN_TASK_POLL_INTERVAL":"0.1", "CHEJIN_HEARTBEAT_INTERVAL":"0.1"})
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout.splitlines()[-1])
    with SessionLocal() as db:
        evidence["backend_status"] = db.get(Worker, worker["id"]).run_status
        evidence["tasks"] = len(list(db.scalars(select(Task))))
        evidence["replies"] = len(list(db.scalars(select(ReplyAction))))
    (tmp_path/"evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    assert evidence["save_failures"] == (1 if probe in {"save_before", "save_after", "restore_retry", "compensation_retry"} else 0)
    assert evidence["pause_failures"] == (1 if probe in {"pause_before", "pause_after"} else 0)
    assert evidence["profile_failures"] == (1 if probe == "profile_failure" else 0)
    assert evidence["health_before"]["ready"] and evidence["threads"]["ready"]
    assert evidence["first"] is True and evidence["second"] is False
    assert evidence["recoveries"] == 1
    assert evidence["tasks"] == evidence["replies"] == 0
    assert all(p["status"] == "faulted" or (p["status"] == "running" and p["recover_from_fault"]) for p in evidence["status_posts"])
    final = evidence["final"]
    assert final["pending"] is None and final["persistence_pending"] is False
    if probe == "success":
        assert evidence["pulls"] >= 1, "The normal case must reach the next real HTTP task pull"
        assert final["memory"] == final["sqlite"] == evidence["backend_status"] == "running"
        assert final["pause"] is False and not evidence["errors"]
    else:
        assert evidence["heartbeats_after_attempt"] >= 3
        assert evidence["errors"] == ["恢复接单失败，仍保持停止接单；请稍后重试。"]
        assert final["memory"] == final["sqlite"] == evidence["backend_status"] == "faulted"
        assert final["pause"] is True and final["can_start"] is False
        assert evidence["new_work_requests"] == [] and evidence["pulls"] == 0
        assert evidence["boundaries"] and all(b["can_start"] is False for b in evidence["boundaries"])
        assert evidence["status_posts"][-1] == {"status":"faulted", "recover_from_fault":False}
        if probe == "restore_retry":
            assert evidence["restore_failures"] == 2
            assert evidence["settled"]["persistence_pending"] is True
            assert evidence["settled"]["pending"] == "faulted"
            assert any(s["backend"] == "faulted" and s["persistence_pending"] for s in evidence["heartbeat_samples"])
        if probe == "compensation_retry":
            assert evidence["compensation_failures"] == 1
            assert evidence["settled"]["pending"] == "faulted"


@pytest.mark.parametrize("mode", ["success", "lost_ack", "new_fault", "pause", "update", "ledger", "old_backend"])
def test_persisted_fault_recovery_through_real_worker_threads_http_sqlite(http_api, tmp_path, mode):
    worker, _, _, url = setup_worker(http_api)
    input_file=tmp_path/"input.json"
    input_file.write_text(json.dumps({"mode":mode,"url":url,"binding":{"worker_id":worker["id"],"worker_token":worker["worker_token"],"client_instance_id":"fault-test"}}))
    worker_root=Path(__file__).resolve().parents[2]/"worker-client"
    result=subprocess.run([sys.executable,"-c",WORKER_PROCESS,str(input_file)],capture_output=True,text=True,timeout=40,env={**os.environ,"PYTHONPATH":str(worker_root),"CHEJIN_WORKER_HOME":str(tmp_path/"worker"),"CHEJIN_TASK_POLL_INTERVAL":"0.1","CHEJIN_HEARTBEAT_INTERVAL":"0.1"})
    assert result.returncode==0,result.stderr
    evidence=json.loads(result.stdout.splitlines()[-1])
    (tmp_path/"evidence.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    assert evidence["before"]=="faulted"
    assert evidence["after"]==("running" if mode=="success" else "faulted"),evidence
    assert evidence["recoveries"]==(0 if mode in {"ledger","old_backend"} else 1),evidence
    assert evidence["second"] is False,evidence
    assert evidence["probes"]>=1
    if evidence["first"]:
        assert evidence["health_before"]["ready"] is True
        assert evidence["state_before"]["ready"] is True
    if mode=="ledger": assert evidence["durable"]["waiting_ledger"]==1
    with SessionLocal() as db:
        assert db.get(Worker,worker["id"]).run_status==evidence["after"]
        assert not list(db.scalars(select(Task)))
        assert not list(db.scalars(select(ReplyAction)))


def test_recovery_waits_for_worker_lock_and_reads_committed_flow(http_api):
    from concurrent.futures import ThreadPoolExecutor
    import time
    worker, path, headers, url = setup_worker(http_api)
    # An independent transaction changes the Flow while the recovery request
    # waits. The HTTP transition must read the committed row, not stale auth.
    import requests
    with ThreadPoolExecutor(max_workers=1) as pool:
        with SessionLocal() as db:
            w = db.scalar(select(Worker).where(Worker.id == worker['id']).with_for_update())
            future = pool.submit(requests.post, url + path + '/run-status',
                headers=headers, json={'client_instance_id':'fault-test','run_status':'running','recover_from_fault':True}, timeout=10)
            time.sleep(.2)
            assert not future.done()
            w.inflight_flow_state = {'flow_id':'committed-old-flow','status':'draining'}
            db.commit()
        response = future.result(timeout=5)
        assert response.status_code == 409, response.text
    with SessionLocal() as db:
        w = db.get(Worker, worker['id'])
        assert w.run_status == 'faulted'
        assert w.inflight_flow_state['flow_id'] == 'committed-old-flow'
