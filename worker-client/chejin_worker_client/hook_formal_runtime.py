"""Opt-in native business coordinator using the original task/Flow/lease APIs.

The normal OCR runtime remains unchanged until Windows UAT validates this
coordinator. It never manufactures C2 frames, clicks an old invitation again,
or submits a second send after a retained physical-attempt journal.
"""
from contextlib import contextmanager,nullcontext
from datetime import datetime
import json
import hashlib
import re
from pathlib import Path
import time
import uuid
from .api import ApiError
from .c1_receipts import clear_confirmed_add_friend_receipt
from . import storage,ui_lock
from .models import Binding,Task,utc_now_iso
from .hook_backend import HookIdentityClient,HookHeartbeatIdentity,HookHeartbeatState
from .hook_formal_c1 import HookFormalC1,HookC1ResultReceipts,_save
from .hook_progress import emit_progress,register_progress,complete_send_progress
from .hook_timing import timed_call, record_raw_delivery
from .hook_history_sync import HookHistorySynchronizer
from .hook_outbox import HookDeliveryScope,HookDeliveryOutbox
from .hook_projection import NativeProjectionStore
from .hook_sender import NativeSendPermit,HookNativeSender,RetainedNativeSendPermit,send_observation_can_settle
from .shared_rules import hook_contract_rules as rules


def publish_current_task(runner, task):
    """Publish actual lifecycle facts through the original UI callback only.

    Backend heartbeat/profile updates do not update the GUI's task snapshot.
    Do not leave a completed C1 card active while a native reply runs.
    """
    if runner is None: return
    runner.current_task = task
    key = None if task is None else (task.id, task.task_type, task.status,
        task.current_step, task.result_code, task.error_code)
    if getattr(runner, '_hook_display_task_key', object()) != key:
        callback = getattr(runner, 'on_task', None)
        if callback is not None: callback(task)
        runner._hook_display_task_key = key


def batch_view_path(runner,worker_id,batch_id):
    # Local pause wins before backend status synchronization. A GET result
    # observation must not implicitly launch a model through a stale server.
    observe=bool(runner is not None and (getattr(getattr(runner,'binding',None),'run_status','running')!='running'
        or not getattr(runner,'can_pull_tasks',lambda:True)()
        or storage.load_runtime_control().get('update_no_new_work') is True))
    return f'/workers/{worker_id}/wechat/message-batches/{batch_id}'+('?observe_only=true' if observe else '')


def read_batch_view(client, credentials, runner, batch_id, *, observation_query_version, observe_only=False):
    """Query batches through one pause-aware, receipt-validated entry point."""
    admission_lock = getattr(runner, '_new_work_admission_lock', None)
    with admission_lock if admission_lock is not None else nullcontext():
        path = (f'/workers/{credentials.worker_id}/wechat/message-batches/{batch_id}?observe_only=true'
                if observe_only else batch_view_path(runner, credentials.worker_id, batch_id))
        observe_only = path.endswith('?observe_only=true')
        if observe_only and observation_query_version != 1:
            raise ValueError('HOOK_MODEL_OBSERVATION_UNSUPPORTED')
        data = client.call(credentials, 'GET', path)
        if observe_only and data.get('observation_only') is not True:
            raise ValueError('HOOK_MODEL_OBSERVATION_UNCONFIRMED')
        return data


@contextmanager
def history_read_cycle(runtime):
    """A checkpoint belongs only to this customer's current dispatch turn."""
    if getattr(runtime, '_history_cycle_token', None) is not None:
        raise ValueError('HOOK_HISTORY_CYCLE_NESTED')
    runtime._history_checkpoint = None
    runtime._history_cycle_token = object()
    try:
        yield
    finally:
        runtime._history_checkpoint = None
        runtime._history_cycle_token = None


class HookRuntimeStopping(Exception):
    """Normal lifecycle cancellation, distinct from persisted-data faults."""


class HookFormalRuntime:
    def __init__(self,client,credentials,gateway,prepared,private_root:Path,*,fault_check=lambda:False,stop_check=lambda:False,wait=time.sleep):
        self.client,self.credentials,self.gateway,self.prepared,self.private_root=client,credentials,gateway,prepared,private_root
        self.fault_check,self.wait=fault_check,wait
        self.stop_check=stop_check
        self.clock=prepared.clock
        self.c1=HookFormalC1(client,credentials,private_root/'invites',clock=self.clock)
        self.c1_results=HookC1ResultReceipts(self)
        self.identity_client=HookIdentityClient(client.base_url,client.timeout_seconds,client.max_response_bytes)
        self.projection=NativeProjectionStore(max_projection_bytes=32*1024*1024,max_entries=200)
        self.outbox=HookDeliveryOutbox(max_storage_bytes=32*1024*1024)
        self.history_retry_events=[]
        self._history_checkpoint=None
        self._history_pending_contexts={}
        self._history_cycle_token=None
        self.active_flow_id=None
        from .config import CONFIG
        from .hook_retention import HistoryArtifactBudget
        self.history_budget=HistoryArtifactBudget(private_root,maximum_bytes=CONFIG.artifact_max_bytes,retention_days=CONFIG.artifact_critical_retention_days)
        self.history_budget.begin_capture(allow_retire=False)
        self.active_task_id=None
        self.process=prepared.process;self.scope=prepared.registration.scope
        self.account={k:getattr(self.scope,k) for k in ('self_wxid','account_epoch','process_generation')}

    def progress(self, context, phase, state='current', note=None):
        self._progress_context=str(context)
        emit_progress(getattr(self,'lifecycle_runner',None),context,phase,state,note)

    def register_binding_progress(self,binding,context,*,group=None,kind='customer',aliases=(),historical=False):
        register_progress(getattr(self,'lifecycle_runner',None),context,group=group,
            label=binding.get('remark_code',''),kind=kind,aliases=aliases,historical=historical)

    def register_batch_progress(self,binding,bid,data):
        aliases=[f"{binding['binding_id']}:{mid}" for mid in data.get('progress_native_ids',[])]
        aliases.extend(str(mid) for mid in data.get('progress_native_ids',[]))
        task=(data.get('task') or {}).get('id')
        if task:aliases.append(task)
        if data.get('trigger_type')=='friend_welcome':aliases.append(f"{binding['binding_id']}:initial")
        self.register_binding_progress(binding,bid,group=bid,kind='batch',aliases=aliases)
        action=data.get('reply_action') or {}
        if task and action.get('segment_count',1)>1:
            register_progress(getattr(self,'lifecycle_runner',None),task,group=bid,kind='batch',
                label=binding.get('remark_code',''),segment=action.get('segment_index'))

    def batch_view(self,bid):
        return read_batch_view(
            self.client, self.credentials, getattr(self, 'lifecycle_runner', None), bid,
            observation_query_version=getattr(self, '_observation_query_version', 0),
        )

    def _check_lifecycle(self):
        if self.fault_check():
            raise ValueError('HOOK_LOCAL_PERSISTENCE_FAULT')
        if getattr(self, 'stop_check', lambda: False)():
            raise HookRuntimeStopping()

    def heartbeat(self,task_id=None,*,new_work=False,passive=False):
        task_id=task_id or self.active_task_id
        self._check_lifecycle()
        if (new_work or not passive) and not self.history_budget.ready:
            raise ValueError('HOOK_HISTORY_STORAGE_CAPACITY_EXCEEDED')
        login=self.gateway.login_status(self.process)
        if not login.logged_in or login.self_wxid!=self.scope.self_wxid:raise ValueError('HOOK_ACCOUNT_CHANGED')
        receipt=self.identity_client.heartbeat(self.credentials,
            HookHeartbeatIdentity(login.self_wxid,self.process.generation,self.gateway.deployment.revision,
                self.process.dll_sha256,account_epoch=self.scope.account_epoch),
            HookHeartbeatState('running' if task_id else 'idle','ready','logged_in',task_id,None,
                json.dumps({**ui_lock.lock_summary(),'capabilities':{'reply_sequence_version':1}})))
        flow=receipt.worker_profile().inflight_flow_state
        runner=getattr(self,'lifecycle_runner',None)
        if runner:runner._backend_inflight_flow_state=dict(flow)
        draining=(receipt.run_status=='paused' and self.active_flow_id
            and flow.get('flow_id')==self.active_flow_id and flow.get('status') in {'active','draining'})
        if (receipt.blocked or receipt.rebind_required or receipt.account_epoch!=self.scope.account_epoch
                or (new_work and receipt.run_status!='running')
                or (not passive and receipt.run_status!='running' and not draining)):
            raise ValueError('HOOK_RUNTIME_NOT_RUNNING')
        self.clock.observe(receipt)
        return receipt

    def start_flow(self,task,conversation_id=None):
        runner=getattr(self,'lifecycle_runner',None)
        lock=getattr(runner,'_new_work_admission_lock',None)
        intent_lock=getattr(runner,'_run_status_intent_lock',None)
        with (lock if lock is not None else nullcontext()), (intent_lock if intent_lock is not None else nullcontext()):
            if runner and not runner._can_start_new_flow():raise ValueError('HOOK_RUNTIME_NOT_RUNNING')
            self.heartbeat(new_work=True)
            state={'flow_id':task['id'],'flow_kind':'chat_reply' if conversation_id else 'task'}
            if conversation_id:state['conversation_id']=conversation_id
            saved=self.private_root/'flows'/f"{task['id']}.json"
            if saved.exists():
                if rules.loads(saved.read_bytes())!=state:raise ValueError('HOOK_FLOW_INTENT_CONFLICT')
            else:_save(saved,state)
            storage.begin_runtime_flow(task['id'],state['flow_kind'])
            try:
                acknowledged=self.client.call(self.credentials,'POST',f'/workers/{self.credentials.worker_id}/inflight-flow/start',state)
            except ApiError as exc:
                if exc.status_code==409 and exc.code=='WORKER_NEW_FLOW_NOT_ALLOWED':
                    _save(self.private_root/'flow-denials'/f"{task['id']}.json",{'flow_id':task['id'],'error_code':exc.code,
                        'server_rejected':True,'physical_action_attempted':False})
                    storage.finish_runtime_flow(task['id'])
                    raise ValueError('HOOK_RUNTIME_NOT_RUNNING') from None
                raise
            if acknowledged.get('flow_id')!=task['id'] or acknowledged.get('status') not in {'active','draining'}:
                raise ValueError('HOOK_FLOW_START_UNCONFIRMED')
            if runner:runner._backend_inflight_flow_state=dict(acknowledged)
            self.active_flow_id=task['id']
            if runner:runner.api.inflight_flow_id=task['id']
            return task['id']
    def claim(self,task,flow_id,conversation_id=None):
        body={'worker_id':self.credentials.worker_id}
        if conversation_id:body.update(claim_source='hook_conversation_flow',conversation_id=conversation_id)
        claimed=self.client.call(self.credentials,'POST',f"/tasks/{task['id']}/claim",body,task_id=task['id'],flow_id=flow_id)
        self.active_task_id=claimed['id']
        publish_current_task(getattr(self,'lifecycle_runner',None),Task.from_api(claimed))
        return claimed

    def finish(self,task_id,flow_id,token,conversation_id=None):
        # Zero means this terminal task was never claimed, not a valid lease.
        # Omit that header only for finish; the server must prove terminal and
        # original Flow ownership. Claim/begin/renew still require positive tokens.
        token = None if type(token) is int and token == 0 else token
        body={'flow_id':flow_id,'terminal_kind':'task_terminal'}
        if conversation_id:body['conversation_id']=conversation_id
        result=self.client.call(self.credentials,'POST',f'/workers/{self.credentials.worker_id}/inflight-flow/finish',body,
            task_id=task_id,flow_id=flow_id,token=token)
        saved=self.private_root/'finished'/f'{flow_id}.json'
        if not saved.exists():_save(saved,result)
        clear_confirmed_add_friend_receipt(flow_id,self.credentials.worker_id,self.credentials.client_instance_id)
        if storage.load_runtime_control().get('inflight_flow_id')==flow_id:storage.finish_runtime_flow(flow_id)
        self.active_flow_id=None
        self.active_task_id=None
        runner=getattr(self,'lifecycle_runner',None)
        if runner:
            runner.api.inflight_flow_id=None
            publish_current_task(runner,None)
        if conversation_id:
            pending=bool(getattr(self,'_send_result_pending',False))
            if pending:self.progress(task_id,'send_confirm','waiting','send_wait')
            else:self.progress(task_id,'finished','done',getattr(self,'_finish_note',None))
        else:self.progress(task_id,'invite_finished','done','invite_done')
        return result

    @contextmanager
    def task_guard(self,task,flow_id):
        # Reuse original expiry/transient-loss behavior; only HTTP transport differs.
        from .task_runner import TaskLeaseGuard
        runtime=self
        model=Task.from_api(task)
        class LeaseAPI:
            def renew_task_lease(self,binding,task_id,current_step=None):
                data=runtime.client.call(runtime.credentials,'POST',f'/tasks/{task_id}/lease/renew',
                    {'lease_fencing_token':model.lease_fencing_token,'current_step':current_step},
                    task_id=task_id,flow_id=flow_id)
                return Task.from_api(data)
        guard=TaskLeaseGuard(api=LeaseAPI(),binding=Binding(self.credentials.worker_id,self.credentials.worker_token,
            self.credentials.client_instance_id,'running'),task=model,current_step=lambda:'hook_formal',now_utc=self.clock.upper_now)
        guard.start()
        def check():
            runner=getattr(self,'lifecycle_runner',None)
            if runner and runner.binding.run_status=='faulted':raise ValueError('HOOK_ORIGINAL_FLOW_CONTINUATION_BLOCKED')
            if runner and hasattr(runner,'_can_continue_inflight_flow') and not runner._can_continue_inflight_flow():
                raise ValueError('HOOK_ORIGINAL_FLOW_CONTINUATION_BLOCKED')
            if guard.cancel_requested():raise ValueError(guard.error_code or 'HOOK_TASK_LEASE_LOST')
            self._check_lifecycle()
        try:yield check
        finally:guard.stop()

    def invite(self, task, ui_factory):
        return timed_call('c1.add_friend_execute', lambda: self._invite(task, ui_factory),
                          context=self.timing_context(task))

    def _invite(self,task,ui_factory):
        flow=self.start_flow(task);claimed=self.claim(task,flow);token=claimed['lease_fencing_token']
        self.progress(task['id'],'invite_prepare')
        preparation=self.c1.prepare(task['id'],token,self.account)
        register_progress(getattr(self,'lifecycle_runner',None),task['id'],kind='invite',label=preparation.get('remark_code',''))
        self.progress(task['id'],'invite_prepare','done')
        with self.task_guard(claimed,flow) as check:
            self.heartbeat(task['id'])
            with self.gateway._locked(self.process,'hook_formal_c1') as lease:
                def action_guard():
                    check();lease.raise_if_lost();self.gateway._history_identity(self.process,self.scope)
                def create_ui():
                    ui=ui_factory(preparation,action_guard)
                    try:ui.on_progress=lambda phase,state,note=None:self.progress(task['id'],phase,state,note)
                    except Exception:pass  # Display instrumentation is never an action gate.
                    return ui
                outcome=self.c1.execute_prepared(task['id'],token,preparation,create_ui)
            self.c1_results.stop_for_cleanup(outcome)
            if outcome.get('formal_submission_proof_saved'):
                self.progress(task['id'],'invite_receipt')
                result=self.c1.settle(task['id'],token)
                self.progress(task['id'],'invite_receipt','done')
            elif outcome.get('submit_attempted') is False:
                result=self.c1_results.settle_saved(claimed)
                if outcome.get('status')!='already_friend':
                    self.progress(task['id'],'invite_receipt','error','failed')
            else:
                raise ValueError('HOOK_INVITATION_OUTCOME_UNKNOWN_REQUIRES_SETTLEMENT')
        self.finish(task['id'],flow,token)
        return {'task_id':task['id'],'outcome':outcome,'settlement':result,'preparation':preparation}

    def discover(self,task_id):
        self.progress(task_id,'bind','waiting','recipient')
        friends=self.gateway.friend_list(self.process,self.scope)
        path=self.private_root/'friend-reads'/f'{rules.digest([self.process.generation,friends.payload])}.json'
        if not path.exists():_save(path,friends.payload)
        elif rules.loads(path.read_bytes())!=friends.payload:raise ValueError('HOOK_FRIEND_LOCAL_EVIDENCE_CHANGED')
        result=self.client.bind_friend(self.credentials,task_id,self.account,friends.payload)
        self.progress(task_id,'bind','done')
        return result

    def review_relationships(self, bindings):
        from .hook_gateway import HookLocalError
        try:
            friends=self.gateway.friend_list(self.process,self.scope).payload
        except HookLocalError as exc:
            if exc.code not in {'HOOK_LOCAL_TRANSPORT_FAILED','HOOK_LOCAL_RESPONSE_INVALID',
                                'HOOK_HTTP_RESPONSE_REJECTED','HOOK_RESPONSE_TOO_LARGE'}:
                raise
            storage.append_log('INFO','hook_relationship_review_unconfirmed',
                '联系人关系暂未核实，不据此判断删除或恢复托管。',metadata={'reason_code':exc.code},include_exception_text=False)
            return
        for binding in bindings:
            try:
                self.client.call(self.credentials,'POST',f'/workers/{self.credentials.worker_id}/wechat/native-relationships/observe',
                    {**self.account,'binding_id':binding['binding_id'],'binding_revision':binding['binding_revision'],
                     'friend_list':friends,'raw_evidence_digest':rules.digest(friends)})
            except ApiError as exc:
                if exc.status_code!=409 or exc.code!='HOOK_BINDING_NOT_AUTHORIZED':raise
                # The operator may retire this exact inventory entry concurrently.
                continue

    def delivery(self,binding):
        return HookDeliveryScope(self.credentials.worker_id,self.credentials.client_instance_id,self.scope.self_wxid,
            self.scope.account_epoch,binding['binding_id'],binding['binding_revision'],binding['peer_wxid'])

    def barrier(self,binding=None):
        if binding is not None:return self._binding_barrier(binding)
        self._check_lifecycle()
        with storage.db_connection() as conn:
            names={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'hook_raw_inbox' not in names:return (0,0)
            scope=(self.scope.self_wxid,self.scope.account_epoch,self.scope.process_generation)
            rows=conn.execute('SELECT receive_seq FROM hook_raw_inbox WHERE self_wxid=? AND account_epoch=? AND process_generation=?',scope).fetchall()
            if not rows:return (0,0)
            if 'hook_observation_projection' not in names:return (rows[-1][0],len(rows))
            value=conn.execute('''SELECT COALESCE(MAX(i.receive_seq),0),SUM(CASE WHEN p.receive_seq IS NULL THEN 1 ELSE 0 END)
                FROM hook_raw_inbox i LEFT JOIN hook_observation_projection p ON p.receive_seq=i.receive_seq
                WHERE i.self_wxid=? AND i.account_epoch=? AND i.process_generation=?''',scope).fetchone()
            return (value[0],value[1] or 0)

    def _binding_barrier(self,binding):
        self._check_lifecycle()
        peer=binding['peer_wxid']
        with storage.db_connection() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_raw_inbox'").fetchone() is None:return (0,0)
            projected=conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_observation_projection'").fetchone() is not None
            rows=conn.execute("SELECT i.receive_seq,i.raw_body,i.raw_digest,"+('p.projection_json,p.projection_digest' if projected else 'NULL AS projection_json,NULL AS projection_digest')+
                " FROM hook_raw_inbox i "+('LEFT JOIN hook_observation_projection p ON p.receive_seq=i.receive_seq ' if projected else '')+
                "WHERE i.self_wxid=? AND i.account_epoch=? AND i.process_generation=? ORDER BY i.receive_seq",
                (self.scope.self_wxid,self.scope.account_epoch,self.scope.process_generation)).fetchall()
        known={job['native_msg_id'] for job in binding.get('voice_jobs',[])}
        facts=[]
        for row in rows:
            if hashlib.sha256(row['raw_body']).hexdigest()!=row['raw_digest']:
                raise ValueError('HOOK_RAW_EVIDENCE_CHANGED')
            if row['projection_json']:
                if hashlib.sha256(row['projection_json'].encode()).hexdigest()!=row['projection_digest']:
                    raise ValueError('HOOK_PROJECTION_EVIDENCE_CHANGED')
                value=rules.loads(row['projection_json']);items=value.get('observations',[])
                known.update(o['native_msg_id'] for o in items if o.get('peer_wxid')==peer and o.get('native_msg_id'))
                facts.append((row['receive_seq'],items,False,value.get('status')!='projected'))
            else:
                value=rules.loads(row['raw_body']);items=[]
                for message in value.get('msglist',[]):
                    matches=(message.get('fromid')==peer or message.get('toid')==peer) and str(message.get('fromtype','1'))!='2'
                    mid=str(message.get('msgsvrid') or '')
                    if matches and mid:known.add(mid)
                    items.append({'peer_wxid':peer if matches else None,'native_msg_id':mid,
                        'media_stage':'audio_observed' if message.get('voice_data') else None})
                facts.append((row['receive_seq'],items,True,False))
        relevant=[(seq,pending) for seq,items,pending,bad in facts if bad or any(
            o.get('peer_wxid')==peer or (o.get('media_stage')=='audio_observed' and o.get('native_msg_id') in known) for o in items)]
        return (max((seq for seq,_ in relevant),default=0),sum(pending for _,pending in relevant))

    def drain(self,binding,*,allowed_native_ids=None):
        # Only delivery retries are safe; physical actions have their own journals.
        delivery=self.delivery(binding)
        while self.projection.project_next(self.scope) is not None:pass
        with storage.db_connection() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_observation_projection'").fetchone() is None:return
            rows=conn.execute('''SELECT p.receive_seq,p.projection_json FROM hook_observation_projection p
                JOIN hook_raw_inbox i ON i.receive_seq=p.receive_seq
                WHERE i.self_wxid=? AND i.account_epoch=? AND i.process_generation=? ORDER BY p.receive_seq''',
                (self.scope.self_wxid,self.scope.account_epoch,self.scope.process_generation)).fetchall()
        for seq,raw in rows:
            projection=rules.loads(raw)
            if projection['status']!='projected':raise ValueError('HOOK_CALLBACK_PROJECTION_QUARANTINED')
        self.outbox.enqueue_projections([seq for seq, _ in rows],delivery,
                                        now=utc_now_iso(),allowed_native_ids=allowed_native_ids)
        self.drain_pending(binding)

    def drain_pending(self,binding):
        # Retirement can finish existing deliveries; it cannot enqueue new input.
        delivery=self.delivery(binding)
        while True:
            claim=self.outbox.claim_next(delivery,now=utc_now_iso(),lease_seconds=30)
            if claim is None:break
            receipt=self.identity_client.store_observations(self.credentials,claim)
            # Dispatch before local delivery acknowledgement; idempotent retry retains facts.
            dispatched = self.client.call(self.credentials,'POST',f'/workers/{self.credentials.worker_id}/wechat/native-events/{claim.event_uuid}/dispatch',self.account)
            record_raw_delivery(dispatched)
            if not self.outbox.mark_stored(claim,receipt,now=utc_now_iso()):raise ValueError('HOOK_OUTBOX_ACK_NOT_SAVED')

    def verify_legacy_invitation_gap(self,binding,prepared_at,original_attempted_at):
        # An old wall-clock journal stays immutable. Never guess a corrected
        # click time from a clock offset measured hours later. Its backend
        # preparation and pause bound the real click; inspect that entire gap.
        self.heartbeat()
        since=str(int(datetime.fromisoformat(prepared_at).timestamp()))
        cutoff=int(datetime.fromisoformat(original_attempted_at).timestamp())+1
        def save(capture):
            _save(self.private_root/'legacy-boundary-captures'/f'{uuid.uuid4()}.json',{
                'raw_utf8':capture.raw_body.decode('utf-8'),'query':capture.query.payload(),'generation':self.process.generation})
        self.drain(binding)
        scan=HookHistorySynchronizer(self.gateway,save,self.barrier,now=self.clock.now)
        capture,coverage=scan.capture(self.process,self.scope,self.delivery(binding),since)
        rows=[row for item in coverage['scans'] for database in item['databases']
              for page in database['pages'] for row in page]
        count=sum(int(row['CreateTime'])<=cutoff for row in rows)
        evidence={'capture_id':capture,'prepared_at_utc':prepared_at,'original_attempted_at_utc':original_attempted_at,
            'boundary_gap_message_count':count,'original_timestamp_rewritten':False,
            'history_semantically_equivalent':count==0,'coverage_digest':rules.digest(coverage)}
        _save(self.private_root/'legacy-boundary-checks'/f'{capture}.json',evidence)
        if count:raise ValueError('HOOK_LEGACY_INVITE_BOUNDARY_AMBIGUOUS')
        return evidence

    def _history_progress_context(self,binding):
        # Presentation identity follows a distinct incoming parent, not a raw
        # receive sequence. Audio supplements, duplicate callbacks and our own
        # send echo must not create another customer workflow.
        from .shared_rules import hook_history_rules
        incoming={};times={}
        self._incoming_occurred_at=None
        with storage.db_connection() as conn:
            exists=conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_raw_inbox'").fetchone()
            rows=conn.execute('SELECT receive_seq,raw_body,raw_digest,received_at FROM hook_raw_inbox WHERE self_wxid=? AND account_epoch=? AND process_generation=? ORDER BY receive_seq',
                (self.scope.self_wxid,self.scope.account_epoch,self.scope.process_generation)).fetchall() if exists else []
        for row in rows:
            if hashlib.sha256(row['raw_body']).hexdigest()!=row['raw_digest']:
                raise ValueError('HOOK_RAW_EVIDENCE_CHANGED')
            event=rules.loads(row['raw_body'])
            if str(event.get('sendorrecv'))!='2':continue
            for message in event.get('msglist',[]):
                mid=str(message.get('msgsvrid') or '')
                if (message.get('fromid')!=binding['peer_wxid'] or message.get('toid')!=self.scope.self_wxid
                        or str(message.get('fromtype'))!='1' or not re.fullmatch(r'[1-9][0-9]{0,19}',mid)
                        or str(message.get('msgtype')) not in {'1','3','34','49'}
                        or any(isinstance(message.get(field),str) and message[field].endswith('@chatroom') for field in ('fromid','toid','fromgid'))):continue
                if hook_history_rules.is_friend_acceptance_notice({'Type':str(message.get('msgtype')),'IsSender':'0','StrContent':message.get('msg')}):continue
                incoming.setdefault(mid,row['receive_seq'])
                times.setdefault(mid,row['received_at'])
        if not incoming:return f"{binding['binding_id']}:initial",False
        parent=max(incoming,key=incoming.get)
        self._incoming_occurred_at=times.get(parent)
        return f"{binding['binding_id']}:{parent}",True

    def _checkpoint_key(self, binding):
        runner = getattr(self, 'lifecycle_runner', None)
        return (getattr(self, '_history_cycle_token', None),
                getattr(runner, '_run_status_revision', None),
                getattr(getattr(runner, 'binding', None), 'run_status', None),
                self.process.generation, self.scope, self.delivery(binding),
                binding['conversation_id'], binding['history_since_create_time'])

    def _remember_history(self, binding, result, barrier, captured_at):
        # Only a fully persisted, backend-accepted capture can be reused. Its
        # age starts at the actual read, not after the backend/upload latency.
        if (getattr(self, '_history_cycle_token', None) is not None
                and result.get('history_ready') is True and result.get('history_state_digest')
                and barrier[1] == 0 and self.barrier(binding) == barrier):
            self._history_checkpoint = (self._checkpoint_key(binding), barrier,
                                        captured_at, json.dumps(result))

    def _history_before_claim(self, binding):
        checkpoint = self._history_checkpoint
        self._history_checkpoint = None  # one immediate consumer; never a send proof
        if checkpoint is not None:
            key, barrier, captured_at, encoded = checkpoint
            age = time.monotonic() - captured_at
            if (key == self._checkpoint_key(binding) and 0 <= age <= 2
                    and self.barrier(binding) == barrier):
                return json.loads(encoded)
        try:
            return self.sync(binding, show_progress=False)
        finally:
            self._history_checkpoint = None

    def sync(self, binding, *, show_progress=True,reconciliation=None):
        self._history_checkpoint = None
        self._history_profile = {}
        started = time.perf_counter()
        def capture():
            try:
                return self._sync(binding,show_progress=show_progress,reconciliation=reconciliation)
            except ValueError as error:
                if str(error)!='HOOK_HISTORY_STORAGE_CAPACITY_EXCEEDED' or reconciliation is None:
                    raise
                return self._sync(binding,show_progress=show_progress,reconciliation=reconciliation,use_reconcile_reserve=True)
        try:
            return timed_call('hook.history_read',capture,result_context=self.timing_context)
        finally:
            try:
                storage.append_log('INFO','hook_history_profile','消息核对耗时',
                    metadata={**self._history_profile,
                              'total_seconds':round(time.perf_counter()-started,6)},
                    include_exception_text=False)
            except Exception:
                pass  # Measurement cannot cancel or replay a business action.

    def timing_context(self, data):
        runner = getattr(self, 'lifecycle_runner', None)
        return {'process_run_id': data.get('process_run_id'),
                'conversation_id': data.get('conversation_id'),
                'api': getattr(runner, 'api', None), 'binding': getattr(runner, 'binding', None)}

    def _sync(self,binding,*,show_progress=True,reconciliation=None,use_reconcile_reserve=False):
        # Only an already loaded, journal-verified retained permit can use this
        # bounded recovery pool. It has no task claim, model or physical send.
        evidence_root=self.private_root
        budget=self.history_budget
        if reconciliation is not None:
            if (not isinstance(reconciliation,RetainedNativeSendPermit)
                    or reconciliation.worker_id!=self.credentials.worker_id
                    or reconciliation.self_wxid!=self.scope.self_wxid
                    or reconciliation.account_epoch!=self.scope.account_epoch):
                raise ValueError('HOOK_SEND_RETAINED_IDENTITY_INVALID')
            if use_reconcile_reserve:
                from .config import CONFIG
                from .hook_retention import reconciliation_budget
                evidence_root=self.private_root/'history-reconcile'
                budget=reconciliation_budget(self.private_root,retention_days=CONFIG.artifact_critical_retention_days)
        progress_context,has_incoming=self._history_progress_context(binding) if show_progress else (None,False)
        self._history_context=progress_context
        if show_progress:
            received=getattr(self,'_incoming_occurred_at',None)
            started=getattr(self,'_progress_started_at',None)
            fresh=True
            if received and started:
                try:fresh=datetime.fromisoformat(received.replace('Z','+00:00'))>=started
                except (ValueError,TypeError):fresh=False
            self.register_binding_progress(binding,progress_context,kind='customer' if fresh else 'reconcile')
            # This is a saved-record fact, not a new receipt timestamp. Publish
            # the time carried by the original native envelope when available.
            if has_incoming and fresh:
                emit_progress(getattr(self,'lifecycle_runner',None),progress_context,'messages_saved','done',
                    occurred_at=getattr(self,'_incoming_occurred_at',None) or '')
            self._progress_context=str(progress_context)  # Correlate faults even when the display stays waiting.
            if self._history_pending_contexts.get(binding['binding_id']) != progress_context:
                self.progress(progress_context,'history')
                self._history_pending_contexts[binding['binding_id']]=progress_context
        # A newly arrived callback invalidates this capture, not the task. Drain
        # it and reread the complete authorized range, with a fixed retry bound.
        # None of these retries can call the physical sender or advance a cursor.
        for attempt in range(3):
            self.heartbeat(passive=True)
            while self.projection.project_next(self.scope) is not None:pass
            with storage.db_connection() as conn:
                rows=conn.execute('SELECT p.projection_json FROM hook_observation_projection p JOIN hook_raw_inbox i ON i.receive_seq=p.receive_seq WHERE i.self_wxid=? AND i.account_epoch=? AND i.process_generation=?',
                    (self.scope.self_wxid,self.scope.account_epoch,self.scope.process_generation)).fetchall() if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_observation_projection'").fetchone() else []
            callback_ids={o['native_msg_id'] for row in rows for o in rules.loads(row[0]).get('observations',[])
                if o.get('peer_wxid')==binding['peer_wxid'] and o.get('disposition')=='message_candidate'}
            budget.begin_capture(allow_retire=reconciliation is None and not self.active_flow_id)
            capture_files=set()
            def save(capture):
                value={'raw_utf8':capture.raw_body.decode('utf-8'),'query':capture.query.payload(),'generation':self.process.generation}
                if reconciliation is not None:value['retained_reply_action_id']=reconciliation.reply_action_id
                path=evidence_root/'history-captures'/f'{rules.digest(value)}.json'
                if path.exists():
                    if rules.loads(path.read_bytes())!=value:raise ValueError('HOOK_HISTORY_LOCAL_EVIDENCE_CHANGED')
                else:budget.save(path,value)
                capture_files.add(path.relative_to(evidence_root).as_posix())
            synchronizer=HookHistorySynchronizer(self.gateway,save,lambda:self.barrier(binding),now=self.clock.now,media_enabled=True)
            try:
                capture,coverage=synchronizer.capture(self.process,self.scope,self.delivery(binding),binding['history_since_create_time'],callback_ids=callback_ids)
            except ValueError as exc:
                if str(exc) not in {'HOOK_HISTORY_CALLBACK_BARRIER_CHANGED','HOOK_HISTORY_LOCAL_PENDING'}:
                    raise
                event={'reason_code':str(exc),'attempt':attempt+1,'retry_allowed':attempt<2,
                    'observed_at':self.clock.now().isoformat(),'physical_send_retried':False}
                _save(evidence_root/'history-retries'/f'{uuid.uuid4()}.json',event)
                self.history_retry_events.append(event)
                if attempt==2:raise
                self.wait(.5)
                continue
            captured_at=time.monotonic()
            self._history_profile = dict(synchronizer.profile)
            post_read_started=time.perf_counter()
            allowed={r['MsgSvrID'] for d in coverage['scans'][1]['databases'] for page in d['pages'] for r in page}
            drain_started=time.perf_counter()
            if reconciliation is None:self.drain(binding,allowed_native_ids=allowed)
            self._history_profile['delivery_drain_seconds']=round(time.perf_counter()-drain_started,6)
            persist_started=time.perf_counter()
            budget.save(evidence_root/'history-coverage'/f'{capture}.json',coverage)
            coverage_persist_seconds=time.perf_counter()-persist_started
            api_started=time.perf_counter()
            result=self.client.history_coverage(self.credentials,self.account,binding,capture,coverage,
                **({'reconciliation':reconciliation} if reconciliation is not None else {}))
            self._history_profile['coverage_api_seconds']=round(time.perf_counter()-api_started,6)
            persist_started=time.perf_counter()
            if show_progress:
                # Complete native coverage with an unresolved voice means the
                # read has finished; the next actual stage is transcription.
                read_complete=result.get('history_ready') or result.get('reason_code')=='HOOK_VOICE_PENDING'
                if read_complete:self._history_pending_contexts.pop(binding['binding_id'],None)
                self.progress(progress_context,'history','done' if read_complete else 'waiting',None if read_complete else 'history_wait')
            budget.save(evidence_root/'history-receipts'/f'{capture}.json',result,settlement=True)
            # Receipt/index publication follows backend admission. Pending or
            # old receipts without a digest never authorize local retirement.
            artifacts=sorted(capture_files|{f'history-coverage/{capture}.json'})
            budget.save(evidence_root/'history-artifact-index'/f'{capture}.json',{
                'schema_version':'hook-history-artifacts-v1','capture_id':capture,
                'coverage_digest':rules.digest(coverage),
                'retained_reply_action_id':reconciliation.reply_action_id if reconciliation else None,
                'files':{name:hashlib.sha256((evidence_root/name).read_bytes()).hexdigest() for name in artifacts}},settlement=True)
            self._history_profile['history_persist_seconds']=round(coverage_persist_seconds+time.perf_counter()-persist_started,6)
            self._history_profile['delivery_receipt_seconds']=round(time.perf_counter()-post_read_started,6)
            if reconciliation is None:
                self._remember_history(binding,result,synchronizer.completed_barrier,captured_at)
            return result

    def settle_retained_send(self,binding,previous_runtime_root,metadata):
        # Settlement deliberately has no claim/begin/execute path. Keep the
        # original private physical journals and exact completion time immutable.
        from dataclasses import replace
        original=metadata.get('original_process_generation',self.scope.process_generation)
        if not rules.bounded(original,128):raise ValueError('HOOK_SEND_RETAINED_IDENTITY_INVALID')
        original_scope=replace(self.scope,process_generation=original)
        permit,outcome=RetainedNativeSendPermit.load(previous_runtime_root/'sends',
            self.credentials,original_scope,binding,metadata)
        synced=self.sync(binding,show_progress=False,reconciliation=permit)
        from .shared_rules import hook_history_rules
        if not hook_history_rules.retained_send_history_ready(synced):
            raise ValueError(synced['reason_code'] or 'HOOK_HISTORY_COVERAGE_REQUIRED')
        if synced.get('reason_code') == hook_history_rules.OUTGOING_ID_PENDING:
            observed=self.client.observe_send(self.credentials,permit,outcome)
            if not send_observation_can_settle(observed,outcome):
                raise ValueError('HOOK_SEND_CONFIRMATION_PENDING')
        settled=self.client.native_send_settle(self.credentials,permit,outcome)
        saved=self.private_root/'send-receipts'/f'{permit.reply_action_id}.json'
        if saved.exists():
            if rules.loads(saved.read_bytes())!=settled:raise ValueError('HOOK_SEND_SETTLEMENT_CONFLICT')
        else:_save(saved,settled)
        return settled



    def reply_once(self,binding,data,*,history=None):
        """One ready physical segment; it never waits for a customer's model.

        Caller polls all customers independently. Registered Flow ownership
        persists between segments and only the original batch may continue.
        """
        action,task=data.get('reply_action'),data.get('task')
        if not action or action.get('status')!='queued' or not task:return None
        self.register_batch_progress(binding,data['batch_id'],data)
        self._send_result_pending=False;self._finish_note='sent'
        self.progress(task['id'],'send_conditions')
        # Claim compares the already admitted history with the model context.
        # This is not physical-send permission: execute always performs one new
        # complete read after its delay, then begin validates eligibility again.
        # Standalone coordinators have no dispatch receipt and must first
        # establish one. The live dispatcher passes its already admitted read.
        synced=history if history is not None else self._history_before_claim(binding)
        if not synced['history_ready']:
            self.progress(task['id'],'send_conditions','waiting','history_wait');return None
        latest=self.batch_view(data['batch_id'])
        latest_action,latest_task=latest.get('reply_action'),latest.get('task')
        if not latest_action or latest_action.get('id')!=action['id'] or latest_action.get('status')!='queued' or not latest_task:
            return None
        task=latest_task

        flow=self.active_flow_id or self.start_flow(task,binding['conversation_id'])
        claimed=self.claim(task,flow,binding['conversation_id'])
        with self.task_guard(claimed,flow) as check:
            try:
                self.progress(task['id'],'send_conditions')
                native_permit=self.client.native_send_claim(self.credentials,self.account,
                    task['id'],action['id'],claimed['lease_fencing_token'],synced['history_state_digest'],flow_id=flow)
            except ApiError as exc:
                if exc.code not in {'HOOK_SEND_CONTEXT_STALE','HOOK_HISTORY_COVERAGE_REQUIRED','REPLY_ACTION_EXPIRED','REPLY_ACTION_NOT_SENDABLE'}:raise
                self.client.call(self.credentials,'POST',f"/tasks/{task['id']}/fail",
                    {'error_code':'C3_CONTEXT_CHANGED_BEFORE_SEND','failure_step':'hook_pre_send'},
                    task_id=task['id'],flow_id=flow,token=claimed['lease_fencing_token'])
                self._finish_note='not_sent'
                self.finish(task['id'],flow,claimed['lease_fencing_token'],binding['conversation_id'])
                return {'status':'not_triggered','automatic_resend_allowed':False}
            permit=NativeSendPermit.capture(native_permit,clock=self.clock)
            self.progress(task['id'],'send_conditions','done')
            permit_file=self.private_root/'sends'/permit.worker_id/permit.account_epoch/permit.reply_action_id/'permit.json'
            if permit_file.exists():
                if rules.loads(permit_file.read_bytes())!=native_permit:raise ValueError('HOOK_SEND_PERMIT_JOURNAL_CONFLICT')
            else:_save(permit_file,native_permit)
            runtime=self
            class Refresh:
                def barrier(self):check();return runtime.barrier(binding)
                def __call__(self,p):
                    value=runtime.sync(binding,show_progress=False)
                    if not value['history_ready'] or value['history_state_digest']!=p.history_state_digest:
                        raise ValueError('HOOK_SEND_LATEST_CONTEXT_STALE')
                    return self.barrier()
            sender=HookNativeSender(self.gateway,self.client,self.credentials,self.private_root/'sends',wait=self.wait,clock=self.clock,
                progress=lambda phase,state,note=None:self.progress(task['id'],phase,state,note),
                timing_context=self.timing_context(latest))
            try:
                outcome=sender.execute(permit,self.process,self.scope,Refresh())
            except (ValueError,ApiError) as exc:
                if isinstance(exc,ApiError):
                    # A definite customer restriction before trigger reuses the
                    # original no-trigger receipt. Network/identity/other 409s
                    # remain errors, and an existing trigger always forbids this.
                    if exc.status_code!=409 or exc.code not in {'HOOK_FRIEND_VERIFICATION_REQUIRED','HOOK_CONTACT_REMARK_CHANGED','HOOK_AUTHORIZATION_RETIRED'}:raise
                    code='HOOK_SEND_LATEST_CONTEXT_STALE'
                else:code=str(exc)
                allowed={'HOOK_SEND_LATEST_CONTEXT_STALE','HOOK_RUNTIME_NOT_RUNNING','HOOK_SEND_SYNC_PENDING',
                    'HOOK_SEND_PERMIT_EXPIRED_OR_CHANGED','HOOK_ORIGINAL_FLOW_CONTINUATION_BLOCKED'}
                root=self.private_root/'sends'/permit.worker_id/permit.account_epoch/permit.reply_action_id
                if code not in allowed or (root/'trigger.json').exists():raise
                intent=root/'intent.json'
                proof={'reply_action_id':permit.reply_action_id,'task_id':permit.task_id,'phase':'not_triggered',
                    'reason_code':code,'intent_sha256':hashlib.sha256(intent.read_bytes()).hexdigest() if intent.exists() else None,
                    'observed_at':self.clock.now().isoformat()}
                saved=root/'not-triggered.json'
                if saved.exists():
                    proof=rules.loads(saved.read_bytes())
                else:_save(saved,proof)
                result=self.client.call(self.credentials,'POST',f'/workers/{self.credentials.worker_id}/wechat/native-replies/{permit.reply_action_id}/not-triggered',
                    {**permit.account(),'task_id':permit.task_id,'send_token':permit.send_token,'proof':proof,'proof_digest':rules.digest(proof)},
                    task_id=permit.task_id,token=permit.lease_fencing_token,flow_id=permit.flow_id)
                receipt_path=self.private_root/'send-receipts'/f'{permit.reply_action_id}.json'
                if receipt_path.exists():
                    if rules.loads(receipt_path.read_bytes())!=result:raise ValueError('HOOK_SEND_SETTLEMENT_CONFLICT')
                else:_save(receipt_path,result)
                self._finish_note='not_sent'
                self.finish(permit.task_id,flow,permit.lease_fencing_token,binding['conversation_id'])
                return {'status':'not_triggered','automatic_resend_allowed':False}

            self.progress(task['id'],'send_confirm')
            retained,outcome=RetainedNativeSendPermit.load(self.private_root/'sends',self.credentials,self.scope,binding,
                {'task_id':permit.task_id,'reply_action_id':permit.reply_action_id,
                 'lease_fencing_token':permit.lease_fencing_token,'flow_id':permit.flow_id})
            # Observe immediately. A late echo stays confirmation_pending and
            # uses the original retained-send recovery; it never triggers again.
            self.sync(binding,show_progress=False,reconciliation=retained)
            observed=self.client.observe_send(self.credentials,permit,outcome)
            if not send_observation_can_settle(observed,outcome):
                return {'task_id':task['id'],'reply_action_id':action['id'],'flow_id':flow,'status':'confirmation_pending',
                    'automatic_resend_allowed':False}
            settled=timed_call('hook.send_reconcile',
                lambda: self.client.native_send_settle(self.credentials,permit,outcome),
                context=self.timing_context(latest))
            _save(self.private_root/'send-receipts'/f"{action['id']}.json",settled)
            complete_send_progress(self,getattr(self,'lifecycle_runner',None),task['id'],settled)
            runner=getattr(self,'lifecycle_runner',None)
            if runner and settled.get('task'):publish_current_task(runner,Task.from_api(settled['task']))
            latest=self.batch_view(data['batch_id'])
            if not settled['local_send_observed'] or (latest.get('reply_sequence') or {}).get('terminal'):
                self.finish(task['id'],flow,claimed['lease_fencing_token'],binding['conversation_id'])
            return {'task_id':task['id'],'reply_action_id':action['id'],'flow_id':flow,
                'status':'sent' if settled['local_send_observed'] else 'unknown_send_result','automatic_resend_allowed':False}
