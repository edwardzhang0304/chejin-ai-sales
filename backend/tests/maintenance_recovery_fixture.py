"""Actual isolated Nginx: every Worker has the same grant/recovery boundary."""
from pathlib import Path
from urllib.parse import urlsplit
import json, os, subprocess, sys, time, uuid
import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ops/formal_release'))
from maintenance import BLOCK


def exercise_cutover_boundary(tmp_path):
    """Synthetic DB only: prove stop/identity guards before starting recovery."""
    import hashlib
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.core.database import SessionLocal
    from app.models.worker import Worker
    from app.models.task import Task
    from app.models.c3 import ReplyAction
    from manual_readiness import inspect_stopped_workers, inspect_cutover
    with SessionLocal() as db:
        workers=list(db.scalars(select(Worker)))
        expected={w.id:hashlib.sha256(json.dumps(w.inflight_flow_state,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                  for w in workers if w.inflight_flow_state}
        with pytest.raises(AssertionError,match='WORKER_RECONNECTED'):
            inspect_stopped_workers(db,expected)
        # Only this new isolated fixture database is altered to model stopped clients.
        for w in workers:w.last_heartbeat_at=datetime(2020,1,1,tzinfo=timezone.utc)
        db.commit()
        with pytest.raises(AssertionError,match='UNREVIEWED_OR_CHANGED_RECOVERY_FLOW'):
            inspect_stopped_workers(db,{})
        owner=next(w for w in workers if w.inflight_flow_state)
        owner.current_task=owner.inflight_flow_state['flow_id'];owner.running_status='running'
        db.commit()
        with pytest.raises(AssertionError,match='WORKER_NOT_STOPPED'):
            inspect_stopped_workers(db,expected)
        readiness,workers=inspect_stopped_workers(db,expected,allow_terminal_projection=True)
        assert not readiness['ready'] and readiness['worker_blockers']==len(expected)
        protected=inspect_cutover(db,workers)
        assert protected['workers']['count']==2
        task=db.get(Task,owner.inflight_flow_state['flow_id'])
        task.lease_owner_worker_id=owner.id;db.flush()
        with pytest.raises(AssertionError,match='UNSETTLED_EXECUTION'):
            inspect_stopped_workers(db,expected,allow_terminal_projection=True)
        db.rollback()
        owner.current_task=str(uuid.uuid4());db.flush()
        with pytest.raises(AssertionError,match='WORKER_NOT_STOPPED'):
            inspect_stopped_workers(db,expected,allow_terminal_projection=True)
        db.rollback()
        task.status='running';db.flush()
        with pytest.raises(AssertionError,match='UNSETTLED_EXECUTION'):
            inspect_stopped_workers(db,expected,allow_terminal_projection=True)
        db.rollback()
        with pytest.raises(AssertionError,match='HEARTBEAT_AFTER_EXIT_CONFIRMATION'):
            inspect_stopped_workers(db,expected,allow_terminal_projection=True,
                                    exited_at=datetime(2019,1,1,tzinfo=timezone.utc))
        owner.local_lock_summary={'locked':True};db.flush()
        with pytest.raises(AssertionError,match='WORKER_SEND_OR_LOCK_PENDING'):
            inspect_stopped_workers(db,expected,allow_terminal_projection=True)
        db.rollback()
        action=db.get(ReplyAction,task.reply_action_id)
        action.ai_payload={**action.ai_payload,'native_send_identity':{
            **action.ai_payload['native_send_identity'],'client_instance_id':'wrong-synthetic-instance'}}
        db.flush()
        with pytest.raises(AssertionError,match='ORIGINAL_SEND_IDENTITY_MISMATCH'):
            inspect_stopped_workers(db,expected,allow_terminal_projection=True)
        db.rollback()
        action.status='sending';db.flush()
        with pytest.raises(AssertionError,match='UNSETTLED_EXECUTION'):
            inspect_stopped_workers(db,expected,allow_terminal_projection=True)
        db.rollback()
        (tmp_path/'cutover-admission.json').write_text(json.dumps({'recent_heartbeat_rejected':True,
            'changed_flow_rejected':True,'remaining_lease_rejected':True,'ordinary_gate_still_blocked':True,
            'terminal_hook_recovery_category_accepted':True,'stale_projection_preserved':True,
            'wrong_current_task_rejected':True,'running_task_rejected':True,
            'heartbeat_after_exit_rejected':True,'local_lock_rejected':True,
            'wrong_native_identity_rejected':True,'unsettled_send_rejected':True,
            'protected_tables':sorted(protected),'worker_count':len(workers)}))


def require_disposable_postgres(engine):
    """Validate the newly created Docker DB before existing fixtures reset it."""
    from sqlalchemy import inspect
    receipt=json.loads(Path(os.environ['CHEJIN_MAINTENANCE_DB_RECEIPT']).read_text())
    state=json.loads(subprocess.check_output(['docker','inspect',receipt['container']],text=True))[0]
    assert state['Id']==receipt['container_id']
    assert state['Config']['Labels'].get('chejin.purpose')=='isolated-maintenance-acceptance'
    assert state['HostConfig']['PortBindings']['5432/tcp']==[{'HostIp':'127.0.0.1','HostPort':str(engine.url.port)}]
    assert engine.url.host=='127.0.0.1' and engine.url.username=='postgres'
    assert engine.url.database==receipt['database'] and engine.url.database.startswith('chejin_maintenance_test_')
    assert not inspect(engine).get_table_names(), 'PostgreSQL fixture must start empty'
    from app.core.database import Base
    from sqlalchemy.schema import CreateSchema
    with engine.begin() as connection:
        for schema in {table.schema for table in Base.metadata.tables.values() if table.schema}:
            assert not inspect(engine).get_table_names(schema=schema)
            connection.execute(CreateSchema(schema, if_not_exists=True))

@pytest.fixture
def maintenance_proxy(request, identity_client, tmp_path):
    if request.node.callspec.params.get('mode') != 'maintenance_recovery':
        yield None
        return
    upstream = identity_client.base_url
    upstream_port = urlsplit(upstream).port
    config = tmp_path / 'nginx'
    config.mkdir()
    name = 'chejin-recovery-test-' + uuid.uuid4().hex[:12]
    path = config / 'default.conf'
    original = f'''server {{
 listen 8080;
 access_log /dev/stdout;
 location / {{ proxy_pass http://host.docker.internal:{upstream_port}; }}
}}
'''
    path.write_text(original)
    subprocess.run(['docker','run','-d','--rm','--name',name,'-p','127.0.0.1::8080',
                    '-v',str(config)+':/etc/nginx/conf.d:ro','nginx:stable-alpine'],check=True,capture_output=True)
    port = int(subprocess.check_output(['docker','port',name,'8080'],text=True).strip().rsplit(':',1)[1])
    base = f'http://127.0.0.1:{port}'
    try:
        for _ in range(50):
            try:
                response=requests.get(base+'/readyz', timeout=1)
                if response.status_code==200:break
            except requests.RequestException:pass
            time.sleep(.1)
        else:raise AssertionError('ISOLATED_NGINX_UPSTREAM_UNAVAILABLE')
        object.__setattr__(identity_client, 'base_url', base+'/api')
        class Proxy:
            url=base+'/api'
            def enable(self, current, account, metadata):
                worker_id=current['id']
                from test_hook_account_http import worker, heartbeat, context, client
                other=worker('isolated-other',phone='13900000002')
                registered=heartbeat(other,context('wxid_sales_B')).json()['data']['hook_context']
                other_account={k:registered[k] for k in ('self_wxid','account_epoch','process_generation')}
                headers={'X-Worker-Token':other['worker_token'],'X-Client-Instance-Id':other['instance']}
                endpoint=f"/workers/{other['id']}/wechat/native-work"
                assert requests.post(self.url+endpoint,json=other_account,headers=headers,timeout=2).status_code==200
                path.write_text(original.replace(' listen 8080;', ' listen 8080;\n'+BLOCK))
                subprocess.run(['docker','exec',name,'nginx','-t'],check=True,capture_output=True)
                subprocess.run(['docker','exec',name,'nginx','-s','reload'],check=True,capture_output=True)
                for _ in range(50):
                    r=requests.post(self.url+f'/workers/{worker_id}/wechat/native-replies/synthetic/claim',json={},timeout=2)
                    if r.status_code==503:break
                    time.sleep(.1)
                else:raise AssertionError('MAINTENANCE_NOT_ACTIVE')
                for suffix in ('claim','begin'):
                    r=requests.post(self.url+f'/workers/{worker_id}/wechat/native-replies/synthetic/{suffix}',json={},timeout=2)
                    assert r.status_code==503 and r.json()['code']=='RELEASE_MAINTENANCE'
                # An unrelated authenticated Worker keeps its own read/recovery
                # access. No device ID receives a maintenance exemption.
                available=requests.post(self.url+endpoint,json=other_account,headers=headers,timeout=2)
                assert available.status_code==200,available.text
                for actor,actor_headers in ((worker_id,{'X-Worker-Token':current['worker_token'],'X-Client-Instance-Id':current['instance']}),(other['id'],headers)):
                    for route in (f'/workers/{actor}/run-status',f'/workers/{actor}/inflight-flow/start',
                                  f'/workers/{actor}/wechat/native-replies/synthetic/claim',
                                  f'/workers/{actor}/wechat/native-replies/synthetic/begin'):
                        denied=requests.post(self.url+route,json={},headers=actor_headers,timeout=2)
                        assert denied.status_code==503 and denied.json()['code']=='RELEASE_MAINTENANCE'
                auth={'X-Worker-Token':current['worker_token'],'X-Client-Instance-Id':current['instance']}
                r=requests.post(self.url+f'/workers/{worker_id}/wechat/native-work',json=account,
                    headers={**auth,'X-Worker-Token':other['worker_token']},timeout=2)
                assert r.status_code==401
                # Real backend identity checks must still reject mismatched original flows.
                r=requests.post(self.url+f'/workers/{worker_id}/inflight-flow/finish',
                    json={'flow_id':str(uuid.uuid4()),'terminal_kind':'task_terminal'},
                    headers={**auth,'X-Inflight-Flow-Id':metadata['flow_id'],
                             'X-Task-Lease-Fencing-Token':str(metadata['lease_fencing_token'])},timeout=2)
                assert r.status_code in {404,409},r.text
                (tmp_path/'maintenance-boundary.json').write_text(json.dumps({
                    'actual_nginx':True,'claim_and_begin_denied':True,
                    'all_devices_same_grant_fence':True,'second_valid_device_read_access':True,
                    'wrong_token_rejected':True,'wrong_flow_rejected':True}))
                exercise_cutover_boundary(tmp_path)
        yield Proxy()
    finally:
        object.__setattr__(identity_client, 'base_url', upstream)
        log=subprocess.run(['docker','logs',name],capture_output=True,text=True)
        (tmp_path/'maintenance-nginx.log').write_text(log.stdout+log.stderr)
        subprocess.run(['docker','rm','-f',name],check=False,capture_output=True)
        import re
        assert not re.search(r'" 5(?!03)[0-9]{2} ',log.stdout+log.stderr), 'Unexpected HTTP 5xx during maintenance recovery'
