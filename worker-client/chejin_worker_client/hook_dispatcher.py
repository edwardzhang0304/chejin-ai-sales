"""Continuous native scheduler hosted by the original Worker lifecycle.

Model polling never waits for one customer. Physical actions retain the original
Flow, Task lease and UI lease; restart recovery can deliver receipts only.
"""
from contextlib import nullcontext
from dataclasses import replace
import hashlib
from pathlib import Path
import threading
import time
from datetime import datetime,timezone
from . import storage
from .api import ApiError
from .c1_receipts import clear_confirmed_add_friend_receipt
from .models import Task
from .hook_progress import emit_progress,register_progress,complete_send_progress,has_progress_context,retained_progress_groups
from .hook_formal_c1 import _save
from .hook_formal_runtime import HookFormalRuntime, publish_current_task,read_batch_view
from .hook_sender import RetainedNativeSendPermit,send_observation_can_settle
from .hook_voice import HookVoiceTranscriber, capture_voice_audio
from . import hook_voice_receipts
from .shared_rules import hook_contract_rules as rules
from .hook_timing import timed_call
from .performance_evidence import emit, measurement
from .hook_gateway import HookLocalError
from .hook_history_wait import is_history_wait
from .hook_history_cache import HistoryReceiptCache, TTL_SECONDS


class HookBusinessDispatcher:
    def history_storage_ready(self):
        budget=getattr(self.runtime,'history_budget',None)
        return budget is None or budget.ready

    def __init__(self, client, private_root: Path, ui_factory, startup, *,
                 automatic_transcription_disabled, interval_seconds=1, runtime_factory=HookFormalRuntime,
                 startup_layout_check=None, startup_retry_check=None):
        if not 0 < interval_seconds <= 5: raise ValueError('HOOK_DISPATCH_INTERVAL_INVALID')
        self.client,self.private_root,self.ui_factory,self.startup=client,private_root,ui_factory,startup
        self.automatic_transcription_disabled=automatic_transcription_disabled
        self._startup_layout_check=startup_layout_check
        self._startup_retry_check=startup_retry_check
        self.interval_seconds,self.runtime_factory=interval_seconds,runtime_factory
        self.reset_binding_session()

    def reset_binding_session(self):
        """Reset memory only, after the old runner and its writers have stopped.

        Disk journals retain their original worker/account scope for recovery.
        """
        self.runtime=None
        self.voice=HookVoiceTranscriber(max_audio_bytes=1024*1024,max_text_bytes=65536)
        self._started_generations=set()
        self._startup_window_lock=threading.Lock()
        self._next_customer=0
        self._binding_conflicts=set()
        self._reconciled=False
        self._last_wait_codes={}
        self._sync_cache=HistoryReceiptCache()
        self._content_holds={}
        self.diagnostic_context={}
        self._wait_events=0
        self._pending_wait_phases={}
        self._wait_lock=threading.RLock()
        self._business_clock_ready=False
        self._business_clock_error=None
        self._observation_query_version=None
        self._business_cycle_verified=False
        self._housekeeping_due=0
        self._presented_batches=set()
        self._ever_presented_batch=False
        self._batch_stages={}
        self._live_batches_by_binding={}
        self._open_handoffs_by_binding={}
        self._friend_holds_by_binding={}
        self._reply_owners_by_binding={}
        self._initial_read_finished=False
        self._started_at=datetime.now(timezone.utc)

    def startup_layout_ready(self):
        return self._startup_layout_check is None or self._startup_layout_check()

    def prepare_startup_window(self, prepared, gateway):
        scope = prepared.registration.scope
        key = (scope.self_wxid, scope.account_epoch, scope.process_generation)
        with self._startup_window_lock:
            if key not in self._started_generations:
                self.startup(prepared.process, gateway, scope)
                self._started_generations.add(key)

    def check_startup_retry(self, prepared):
        if self._startup_retry_check is not None:
            self._startup_retry_check(prepared.process, prepared.registration.scope)

    def pending_wait_snapshot(self):
        with self._wait_lock:return {k:set(v) for k,v in self._pending_wait_phases.items()}

    def _observe_clock(self,rt,receipt):
        try:rt.clock.observe(receipt)
        except ValueError as exc:
            self._business_clock_ready=False;self._business_clock_error=str(exc);raise
        self._business_clock_ready=True;self._business_clock_error=None

    def _report_wait(self, code, *, context='business', clock_check=None):
        with self._wait_lock:self._report_wait_locked(code,context=context,clock_check=clock_check)

    def _report_wait_locked(self, code, *, context='business', clock_check=None):
        self._wait_events+=1
        phase=None
        if code in {'HOOK_BACKEND_CLOCK_STALE','HOOK_BACKEND_CLOCK_RTT_UNCONFIRMED'}:phase='clock_wait'
        elif code in {'HOOK_BACKEND_NETWORK_FAILED','HOOK_BACKEND_TEMPORARILY_UNAVAILABLE'}:phase='connection_wait'
        if phase is not None:
            self._pending_wait_phases.setdefault(context,set()).add(phase)
            emit_progress(getattr(self,'_progress_runner',None),context,phase,'waiting',phase)
        log_key=(context,phase)
        signature=(code,(clock_check or {}).get('reason'))
        if self._last_wait_codes.get(log_key)!=signature:
            storage.append_log('WARN','hook_business_wait','工作微信消息正在核对，保留原记录。',
                error_code=code,include_exception_text=False,
                metadata={'wait_owner':context,'clock_check':dict(clock_check or {})})
        self._last_wait_codes[log_key]=signature

    def resolve_wait(self,context,*,phase=None):
        with self._wait_lock:self._resolve_wait_locked(context,phase=phase)

    def _resolve_wait_locked(self,context,*,phase=None):
        pending=self._pending_wait_phases.get(context,set())
        resolved=set(pending) if phase is None else pending.intersection({phase})
        remaining=pending-resolved
        if remaining:self._pending_wait_phases[context]=remaining
        else:self._pending_wait_phases.pop(context,None)
        for completed in resolved:
            emit_progress(getattr(self,'_progress_runner',None),context,completed,'done')
            self._last_wait_codes.pop((context,completed),None)

    def _publish_handoff(self,runner,context,record):
        reason=record.get('handoff_reason_code')
        occurred_at=record.get('created_at') or ''
        if context not in self._presented_batches:
            registry=getattr(runner,'_hook_progress_contexts',{})
            prior=registry.get(context,{})
            register_progress(runner,context,label=prior.get('label',''),kind='batch',
                historical=prior.get('historical',True))
        if reason=='HOOK_VOICE_RESULT_UNCONFIRMED':
            emit_progress(runner,context,'voice','error','handoff_voice_unknown',occurred_at=occurred_at)
        elif reason=='HOOK_VOICE_AUDIO_MISSING':
            emit_progress(runner,context,'voice','error','handoff_voice_audio_missing',occurred_at=occurred_at)
        elif reason=='AI_ENGINE_RETRY_EXHAUSTED':
            emit_progress(runner,context,'decision','error','handoff_ai_failure',occurred_at=occurred_at)
        elif context in self._presented_batches:
            emit_progress(runner,context,'decision','done',occurred_at=occurred_at)
        emit_progress(runner,context,'handoff','done','handoff',occurred_at=occurred_at)

    def _mark_batch_presented(self,bid):
        self._ever_presented_batch=True
        self._presented_batches.add(bid)

    def _prune_presentation_state(self,runner,inventory):
        """Release only display bookkeeping absent from work AND retained UI.

        No journal, delivery cursor, hold, or server ownership is changed here.
        Unfinished batches remain protected until the normal consumer closes them.
        """
        visible={b['binding_id'] for b in inventory['bindings']}
        for mapping in (self._content_holds,self._live_batches_by_binding,self._open_handoffs_by_binding,
                        self._friend_holds_by_binding,self._reply_owners_by_binding):
            for key in list(mapping):
                if key not in visible:mapping.pop(key,None)
        retained=retained_progress_groups(runner)
        retained.add(inventory.get('inflight_batch_id'))
        for binding in inventory['bindings']:
            retained.update(binding.get('batch_ids',[]))
        for events in self._open_handoffs_by_binding.values():retained.update(events.values())
        for batches in self._live_batches_by_binding.values():retained.update(batches)
        # Compatibility for already-presented state in this running dispatcher.
        self._ever_presented_batch=self._ever_presented_batch or bool(self._presented_batches)
        self._presented_batches.intersection_update(retained)
        for bid in list(self._batch_stages):
            if bid not in retained:self._batch_stages.pop(bid,None)

    def _publish_open_handoffs(self,runner,binding):
        # This field is an authoritative read-only snapshot, not a new work
        # item. Older servers omitting it cannot prove that a hold was closed.
        records=binding.get('open_handoffs')
        if not isinstance(records,list):return
        key=binding.get('binding_id')
        if not isinstance(key,str):return
        previous=self._open_handoffs_by_binding.get(key,{})
        current={}
        for record in records:
            if not isinstance(record,dict):return
            event_id=record.get('id')
            if not isinstance(event_id,str) or not event_id:return
            context=record.get('batch_id') or 'handoff:'+event_id
            if not isinstance(context,str):return
            current[event_id]=context
            aliases=[f"{binding['binding_id']}:{mid}" for mid in record.get('progress_native_ids',[])]
            aliases.extend(record.get('progress_native_ids',[]))
            try:
                created=datetime.fromisoformat(str(record.get('created_at','')).replace('Z','+00:00'))
                if created.tzinfo is None:created=created.replace(tzinfo=timezone.utc)
                newly_created=created>=self._started_at
            except (ValueError,TypeError):newly_created=False
            live=context in self._presented_batches or newly_created
            register_progress(runner,context,label=binding.get('remark_code',''),kind='batch',
                historical=not live,aliases=aliases if live else ())
            self._publish_handoff(runner,context,record)
        for event_id,context in previous.items():
            if event_id not in current:
                emit_progress(runner,context,'handoff_closed','done','handoff_closed')
        self._open_handoffs_by_binding[key]=current
        # An old handoff event stays in history, but an unclosed restriction is
        # a current customer state. Only this authoritative inventory can clear
        # it; an omitted field or an unreadable history is not proof of closure.
        friend_hold=next((record for record in records
            if record.get('handoff_reason_code')=='HOOK_FRIEND_VERIFICATION_REQUIRED'),None)
        hold_context='customer-hold:'+key
        if friend_hold is not None or key in self._friend_holds_by_binding:
            register_progress(runner,hold_context,label=binding.get('remark_code',''),
                kind='customer',binding_id=key)
            emit_progress(runner,hold_context,'customer_hold',
                'waiting' if friend_hold is not None else 'done',
                'friend_verification_hold' if friend_hold is not None else 'handoff_closed',
                occurred_at=(friend_hold.get('created_at') or '') if friend_hold is not None else None)
            if friend_hold is not None:self._friend_holds_by_binding[key]=friend_hold['id']
            else:self._friend_holds_by_binding.pop(key,None)
        ownership=binding.get('reply_ownership') or {}
        owner=ownership.get('owner')
        signature=(owner,ownership.get('revision'),bool(ownership.get('round_id')))
        context='ownership:'+key
        if owner in {'ai','human'} and (self._reply_owners_by_binding.get(key)!=signature
                or not has_progress_context(runner,context)):
            register_progress(runner,context,label=binding.get('remark_code',''),kind='batch',binding_id=key)
            note=('human_wait' if ownership.get('round_id') else 'human_replied') if owner=='human' else 'ai_owner'
            # This is the current authoritative snapshot, not a newly occurred
            # business event. Rehydration after UI trimming must not invent time.
            emit_progress(runner,context,'reply_owner','done',note,occurred_at='')
            self._reply_owners_by_binding[key]=signature

    def _publish_batch_progress(self,runner,bid,data):
        emit('observation', name='batch_state', batch_id=bid, status=data.get('batch_status'),
             waiting='ai' if data.get('processing') else None)
        action=data.get('reply_action') or {}
        task=data.get('task') or {}
        live=bool(data.get('processing') or action.get('status') in {'queued','sending','unknown_send_result'}
                  or task.get('status') in {'pending','claimed','running','retry_wait'})
        event=data.get('handoff_event') or {}
        confirmed_handoff=bool(not live and data.get('terminal') is True
            and data.get('decision') in {'handoff','handoff_for_approval'}
            and isinstance(event,dict) and event.get('id') and event.get('batch_id')==bid
            and event.get('closed_at') is None)
        if confirmed_handoff:
            prior=self._batch_stages.get(bid)
            if prior in {'collecting','retry_wait'}:emit_progress(runner,bid,'model_retry' if prior=='retry_wait' else 'model_queue','done')
            self._batch_stages[bid]=data.get('batch_status')
            self._publish_handoff(runner,bid,event)
        elif live or bid in self._presented_batches:
            # Completed old replies are still polled for bookkeeping, but
            # are not presented as a new AI operation after restart.
            self._mark_batch_presented(bid)
            state='current' if data.get('processing') else 'error' if data.get('error_code') else 'done'
            note='model_wait' if data.get('processing') else 'failed' if data.get('error_code') else 'no_reply' if data.get('decision') in {'no_reply','skip','ignore'} else None
            status=data.get('batch_status')
            if status in {'cancelled','superseded'}:
                prior=self._batch_stages.get(bid)
                if prior in {'collecting','retry_wait'}:emit_progress(runner,bid,'model_retry' if prior=='retry_wait' else 'model_queue','done')
                if prior!='collecting':emit_progress(runner,bid,'decision','done','not_sent')
                emit_progress(runner,bid,'finished','done','not_sent')
                self._batch_stages[bid]=status
                return live
            paused=getattr(getattr(runner,'binding',None),'run_status','running')!='running'
            if data.get('processing') and status in {'collecting','retry_wait'}:
                phase='model_retry' if status=='retry_wait' else 'model_queue'
                if self._batch_stages.get(bid)=='generating':
                    emit_progress(runner,bid,'decision','error' if status=='retry_wait' else 'waiting','retry' if status=='retry_wait' else 'queued')
                emit_progress(runner,bid,phase,'waiting','paused_work' if paused else 'retry' if status=='retry_wait' else 'queued')
            else:
                prior=self._batch_stages.get(bid)
                if prior in {'collecting','retry_wait'}:
                    emit_progress(runner,bid,'model_retry' if prior=='retry_wait' else 'model_queue','done')
                emit_progress(runner,bid,'decision',state,note)
            self._batch_stages[bid]=status
            if (not live and not data.get('error_code') and data.get('terminal') is True
                    and data.get('decision') in {'no_reply','skip','ignore'}):
                emit_progress(runner,bid,'finished','done','no_reply')
        return live

    def _publish_initial_read_finished(self,runner,inventory,*,history_ready,pending):
        if (self._initial_read_finished or self._ever_presented_batch or self._presented_batches or not history_ready or pending
                or inventory.get('inflight_task') or inventory.get('inflight_flow',{}).get('flow_id')):return
        self._initial_read_finished=True
        emit_progress(runner,'startup_reconciliation','sync_finished','done')

    def _recover_voice_attempts(self,bindings):
        from .hook_process import inspect_windows_generation_exited
        rt=self.runtime
        with storage.db_connection() as conn:
            pending = hook_voice_receipts.reconcile_pending(conn, self.private_root)
            conn.commit()
        retained_by_binding = hook_voice_receipts.retained_job_map(pending, self.private_root, rt.scope)
        binding_key = lambda b: (b['binding_id'],b['binding_revision'],b['peer_wxid'])
        pending_order = {key:i for i,key in enumerate(retained_by_binding)}
        for binding in sorted(bindings,key=lambda b:pending_order.get(binding_key(b),len(pending_order))):
            retained = retained_by_binding.get(binding_key(binding), [])
            seen = set()
            for job in [*retained,*binding.get('voice_jobs',[]),*binding.get('retained_voice_jobs',[])]:
                if job['native_msg_id'] in seen:continue
                seen.add(job['native_msg_id'])
                saved=self.voice.saved_parent(rt.scope,binding['peer_wxid'],job['native_msg_id'])
                if not saved:continue
                result=self.voice.recover_interrupted(rt.scope,binding['peer_wxid'],job['native_msg_id'],
                    generation_exited=inspect_windows_generation_exited)
                if result['state'] not in {'transcribed','failed','uncertain'}:
                    raise ValueError('HOOK_VOICE_RESULT_UNCONFIRMED')
                if hook_voice_receipts.acknowledged(self.private_root, result):
                    continue
                with storage.db_connection() as conn:
                    hook_voice_receipts.defer_pending(conn, self.private_root, result)
                    conn.commit()
                hook_voice_receipts.publish(self.client, rt.credentials, rt.account, binding, result, self.private_root)
                self._invalidate_history_cache((rt.scope.process_generation,binding['binding_id'],binding['binding_revision']))
                return True
        return False

    def _adopt_flow(self,runner,inventory):
        rt=self.runtime;flow=inventory['inflight_flow'];local=storage.load_runtime_control().get('inflight_flow_id')
        if flow.get('flow_id'):
            fid=flow['flow_id']
            intent=self.private_root/'flows'/f'{fid}.json'
            if not intent.exists() or rules.loads(intent.read_bytes()).get('flow_id')!=fid:
                raise ValueError('HOOK_RETAINED_FLOW_INTENT_MISSING')
            if local and local!=fid:raise ValueError('HOOK_RETAINED_FLOW_CONFLICT')
            storage.begin_runtime_flow(fid,flow['flow_kind'])
            rt.active_flow_id=fid
            runner.api.inflight_flow_id=fid
            task=inventory.get('inflight_task')
            if task:
                rt.active_task_id=task['id']
                publish_current_task(runner,Task.from_api(task))
        elif local:
            if inventory.get('retained_flow_terminal') is not True:
                from .hook_recovery import reconcile
                path=self.private_root/'flows'/f'{local}.json'
                if not path.exists():raise ValueError('HOOK_RETAINED_FLOW_INTENT_MISSING')
                intent=rules.loads(path.read_bytes())
                if intent.get('flow_id')!=local:raise ValueError('HOOK_RETAINED_FLOW_CONFLICT')
                result=reconcile(rt,'flow_start',{'task_id':local,'flow_id':local,
                    'flow_kind':intent['flow_kind'],'conversation_id':intent.get('conversation_id')},evidence=intent)
                if result['status']=='registered':
                    # A registration crossed the inventory read. Refetch the
                    # authoritative inventory; this result grants no action.
                    raise ValueError('HOOK_RETAINED_FLOW_UNCONFIRMED')
            clear_confirmed_add_friend_receipt(local,rt.credentials.worker_id,rt.credentials.client_instance_id)
            storage.finish_runtime_flow(local)
            rt.active_flow_id=rt.active_task_id=None
            runner.api.inflight_flow_id=None;publish_current_task(runner,None)
        else:
            rt.active_flow_id=rt.active_task_id=None
            runner.api.inflight_flow_id=None;publish_current_task(runner,None)
        runner._restart_backend_probe_pending=False
        runner._restart_recovery_flow_id=None
        runner._backend_inflight_flow_state=dict(flow)
        self._reconciled=True

    def _recover_send(self,binding,metadata):
        rt=self.runtime
        if hasattr(rt,'register_binding_progress'):
            rt.register_binding_progress(binding,metadata['task_id'],group=metadata.get('batch_id') or metadata['task_id'],kind='batch')
        root=self.private_root/'sends'/rt.credentials.worker_id/rt.scope.account_epoch/metadata['reply_action_id']
        if not (root/'trigger.json').exists():
            try:
                original=rules.loads((root/'permit.json').read_bytes())
            except FileNotFoundError:
                # Missing local authority cannot authorize a send. Ask the
                # server to close only the original, never-begun claim.
                if metadata.get('physical_begin_registered') is not False:
                    raise ValueError('HOOK_RETAINED_SEND_PERMIT_MISSING') from None
                if any((root/name).exists() for name in ('intent.json','outcome.json','not-triggered.json')):
                    raise ValueError('HOOK_RETAINED_SEND_PERMIT_MISSING') from None
                from .hook_recovery import reconcile
                result=reconcile(rt,'send_claim',{'task_id':metadata['task_id'],'flow_id':metadata['flow_id'],
                    'flow_kind':'chat_reply','conversation_id':binding['conversation_id'],
                    'reply_action_id':metadata['reply_action_id'],
                    'original_process_generation':metadata['original_process_generation'],
                    'lease_fencing_token':metadata['lease_fencing_token']},
                    evidence={'permit_missing':True,'trigger_missing':True})
                rt._finish_note='not_sent';rt._send_result_pending=False
                emit_progress(getattr(self,'_progress_runner',None),metadata['task_id'],'send_confirm','done','not_sent')
                return True
            if (original.get('reply_action_id')!=metadata['reply_action_id'] or original.get('task_id')!=metadata['task_id']
                    or original.get('self_wxid')!=rt.scope.self_wxid or original.get('account_epoch')!=rt.scope.account_epoch
                    or original.get('process_generation')!=metadata['original_process_generation']
                    or original.get('peer_wxid')!=binding['peer_wxid'] or original.get('flow_id')!=metadata['flow_id']
                    or original.get('lease_fencing_token')!=metadata['lease_fencing_token']):
                raise ValueError('HOOK_RETAINED_NO_TRIGGER_IDENTITY_MISMATCH')
            from .hook_recovery import settle_untriggered
            from .hook_sender import NativeSendPermit
            permit=NativeSendPermit.capture(original,clock=rt.clock)
            settle_untriggered(rt,permit,root,'HOOK_RUNTIME_NOT_RUNNING')
            rt._finish_note='not_sent';rt._send_result_pending=False
            emit_progress(getattr(self,'_progress_runner',None),metadata['task_id'],'send_confirm','done','not_sent')
            return True
        original=replace(rt.scope,process_generation=metadata['original_process_generation'])
        permit,outcome=RetainedNativeSendPermit.load(self.private_root/'sends',rt.credentials,original,binding,metadata)
        synced=rt.sync(binding,reconciliation=permit)
        from .shared_rules import hook_history_rules
        from .hook_history_wait import require_retained_history
        require_retained_history(synced)
        observation=self.client.observe_send(rt.credentials,permit,outcome)
        if observation.get('settlement'):
            settled=observation['settlement']['result']
        elif not send_observation_can_settle(observation,outcome):
            emit_progress(getattr(self,'_progress_runner',None),metadata['task_id'],'send_confirm','waiting','send_wait')
            self._report_wait('HOOK_SEND_CONFIRMATION_PENDING');return False
        else:settled=self.client.native_send_settle(rt.credentials,permit,outcome)
        path=self.private_root/'send-receipts'/f"{permit.reply_action_id}.json"
        if not path.exists():_save(path,settled)
        elif rules.loads(path.read_bytes())!=settled:raise ValueError('HOOK_SEND_SETTLEMENT_CONFLICT')
        complete_send_progress(rt,getattr(self,'_progress_runner',None),metadata['task_id'],settled)
        return True

    def _recover_invitation(self,task):
        rt=self.runtime;root=rt.c1.namespace(task['id']);token=task.get('lease_fencing_token')
        outcome_path=root/'outcome.json'
        if outcome_path.exists():
            rt.c1_results.stop_for_cleanup(rules.loads(outcome_path.read_bytes()))
        if task['status'] in {'completed','failed','cancelled','canceled'}:
            if outcome_path.exists() and rules.loads(outcome_path.read_bytes()).get('submit_attempted') is False:
                rt.c1_results.settle_saved(task)
            elif (root/'pre-ui.json').exists() and not (root/'ui-started.json').exists():
                rt.c1_results.settle_before_ui(task)
            rt.finish(task['id'],rt.active_flow_id,token);return
        if (root/'submission-proof.json').exists():
            rt.c1.settle(task['id'],token);rt.finish(task['id'],rt.active_flow_id,token);return
        if outcome_path.exists():
            rt.c1_results.settle_saved(task)
            rt.finish(task['id'],rt.active_flow_id,token);return
        # Only a durable pre-UI identity can settle a preparation interruption.
        # Started/unknown UI remains blocked; recovery never invokes the UI.
        rt.c1_results.settle_before_ui(task)
        rt.finish(task['id'],rt.active_flow_id,token)

    def _discover_invitations(self, runner, invitations, bindings, relationships=()):
        from .shared_rules import hook_contact_rules
        from .hook_friend_discovery import CONTACT_REFRESH_DELAYS
        rt = self.runtime
        self._binding_conflicts.intersection_update(i['source_task_id'] for i in invitations)
        if any(not isinstance(i.get('remark_code'), str) for i in invitations):
            raise ValueError('HOOK_FRIEND_DISCOVERY_PROTOCOL_REQUIRED')
        jobs = list(invitations) + [dict(source_task_id='relationship:'+b['binding_id'],
                    remark_code=b['remark_code'], relationship=b) for b in relationships]
        legacy_peers = {b['peer_wxid'] for b in relationships}
        round_data = rt.discovery_round(jobs, {b['peer_wxid'] for b in bindings} - legacy_peers)
        if round_data is None:
            return
        pending, friends = round_data
        current = {i['source_task_id'] for i in jobs}
        peers = {e['peer_wxid'] for e in pending['events']}
        legacy = [i['relationship'] for i in pending['invitations']
                  if i.get('relationship') and i['source_task_id'] in current
                  and i['source_task_id'] not in pending['results']
                  and (pending['all_peers'] or i['relationship']['peer_wxid'] in peers)]
        reviewed = (rt.review_relationships(legacy, initial_friends=friends) or {}) if legacy else {}
        for invite in pending['invitations']:
            task_id = invite['source_task_id']
            if pending['results'].get(task_id, {}).get('state') not in {None, 'binding', 'refresh_wait'}:
                continue
            if task_id not in current:
                rt.record_discovery(pending, task_id, {'state': 'retired'})
                continue
            self.diagnostic_context = {'source_task_id': task_id, 'phase': 'native_friend_binding'}
            if invite.get('relationship'):
                binding = invite['relationship']
                if not pending['all_peers'] and binding['peer_wxid'] not in peers:
                    rt.record_discovery(pending, task_id, {'state':'not_triggered'})
                    continue
                waiting = reviewed.get(binding['binding_id'], True)
                for item in bindings:
                    if item['binding_id'] == binding['binding_id']:
                        item['relationship_confirmation_pending'] = waiting
                rt.record_discovery(pending, task_id, {'state':'unresolved' if waiting else 'verified'})
                continue
            try:
                candidate = hook_contact_rules.resolve_friend_shortcode(friends, invite['remark_code'], rt.scope.self_wxid)
            except ValueError as exc:
                pending.setdefault('comparisons', {})[task_id] = hook_contact_rules.contact_observation(
                    friends, invite['remark_code'], rt.scope.self_wxid, None)
                rt.record_discovery(pending, task_id, {'state':'unresolved','reason_code':str(exc)})
                emit_progress(runner, task_id, 'bind', 'waiting', 'friend_evidence_wait')
                continue
            comparison = hook_contact_rules.contact_observation(
                friends, invite['remark_code'], rt.scope.self_wxid, candidate.candidate_wxid)
            pending.setdefault('comparisons', {})[task_id] = comparison
            if (candidate.matching_objects != 1 or not candidate.candidate_wxid
                    or comparison['state'] != 'matched'
                    or (not pending['all_peers'] and task_id not in pending['authorized_tasks']
                        and candidate.candidate_wxid not in peers)):
                absent = candidate.matching_objects == 0
                retry = absent and pending.get('refresh_attempt',0) < len(CONTACT_REFRESH_DELAYS)
                reason = ('HOOK_FRIEND_SHORTCODE_ABSENT' if absent else
                          'HOOK_FRIEND_IDENTITY_UNCONFIRMED' if comparison['state'] != 'matched' else
                          'HOOK_FRIEND_EVENT_PEER_MISMATCH')
                rt.record_discovery(pending, task_id, {'state': 'refresh_wait' if retry else 'unresolved',
                    'reason_code': reason})
                emit_progress(runner, task_id, 'bind', 'waiting', 'friend_refresh_wait' if retry else 'friend_evidence_wait')
                continue
            rt.record_discovery(pending, task_id, {'state': 'binding'})
            try:
                result = rt.discover(task_id, friends)
                rt.record_discovery(pending, task_id, {'state': 'bound', 'binding_id': result['binding_id']})
                self._binding_conflicts.discard(task_id)
            except ApiError as exc:
                known = {'HOOK_BINDING_PAUSED','HOOK_BINDING_REVISION_STALE','HOOK_INVITE_AUTHORIZATION_RETIRED',
                         'HOOK_BINDING_CONFLICT','HOOK_SHORTCODE_NOT_UNIQUE','HOOK_FRIEND_NOT_FOUND',
                         'HOOK_SHORTCODE_MATCH_REQUIRED','HOOK_FRIEND_CANDIDATE_UNRESOLVED'}
                if exc.code not in known:
                    raise  # Keep capture for an idempotent backend retry, never reread the DLL.
                rt.record_discovery(pending, task_id, {'state': 'unresolved', 'reason_code': exc.code})
                if exc.code == 'HOOK_INVITE_AUTHORIZATION_RETIRED':
                    emit_progress(runner, task_id, 'bind', 'done', 'invite_replaced')
                elif exc.code == 'HOOK_BINDING_CONFLICT':
                    emit_progress(runner, task_id, 'bind', 'waiting', 'binding_conflict')
                    if task_id not in self._binding_conflicts:
                        storage.append_log('WARN','hook_binding_conflict','客户绑定待运营确认，其他客户继续处理。',
                            task_id=task_id,error_code=exc.code,include_exception_text=False,
                            metadata={'source_task_id':task_id,'phase':'native_friend_binding'})
                        self._binding_conflicts.add(task_id)
                else:
                    emit_progress(runner, task_id, 'bind', 'waiting', 'friend_evidence_wait')
        if any(r.get('state') == 'refresh_wait' for r in pending['results'].values()):
            rt.defer_discovery(pending)
        else:
            pending['stage'] = 'complete'
            rt.finish_discovery(pending)

    def tick(self,runner,services):
        before=self._wait_events
        self._business_cycle_verified=False
        result=self._tick(runner,services)
        # A complete successful business pass resolves its own wait. Exceptions,
        # startup gates and an explicit pending condition never claim recovery.
        if self._business_cycle_verified and before==self._wait_events:
            self.resolve_wait('business')
        return result

    def _invalidate_history_cache(self, key):
        if self._sync_cache.pop(key, None) is not None:
            emit('counter', name='history_cache_invalidated', count=1)

    def _read_history(self,binding,*,barrier=None):
        rt=self.runtime
        key=(rt.scope.process_generation,binding['binding_id'],binding['binding_revision'])
        before=rt.barrier(binding) if barrier is None else barrier
        held=self._content_holds.get(binding['binding_id'])
        if held and held[:2] == (key,before) and before[1] == 0:
            return dict(held[2])  # only a hold, never a history/send permit
        self._content_holds.pop(binding['binding_id'],None)
        self._invalidate_history_cache(key)
        synced=rt.sync(binding)
        if (synced.get('content_isolated') is True and not synced.get('history_ready')
                and before[1] == 0 and rt.barrier(binding) == before):
            # Compact bounded state. Durable receipts survive restart; a new
            # callback/revision/generation triggers the original bounded read.
            if len(self._content_holds)>=64:
                self._content_holds.pop(next(iter(self._content_holds)))
            self._content_holds[binding['binding_id']] = (key,before,{
                k:synced[k] for k in ('history_ready','reason_code','content_isolated','conversation_id') if k in synced})
        # Never label an older receipt with a callback that landed during
        # capture or publication. Failed reads leave no reusable cache.
        if before[1]==0 and rt.barrier(binding)==before:
            self._sync_cache[key]=(before,time.monotonic(),synced)
        emit('gauge',name='history_cache.entries',count=len(self._sync_cache))
        emit('gauge',name='history_cache.payload_bytes',count=self._sync_cache.payload_bytes)
        return synced

    def _tick(self,runner,services):
        prepared=services.connection.prepared
        if prepared is None or runner.binding is None or runner.stop_event.is_set():return
        scope=prepared.registration.scope
        key=(scope.self_wxid,scope.account_epoch,scope.process_generation)
        old=None if self.runtime is None else tuple(self.runtime.account.values())
        if key!=old:
            if self.runtime and self.runtime.scope.account_epoch!=scope.account_epoch:
                raise ValueError('HOOK_RUNTIME_ACCOUNT_CHANGED')
            self._sync_cache.clear()
            self.runtime=self.runtime_factory(self.client,services._credentials,services.connection.gateway,
                prepared,self.private_root,stop_check=lambda:runner.stop_event.is_set(),
                wait=lambda seconds: runner.stop_event.wait(seconds))
            self._reconciled=False
        rt=self.runtime
        rt._friend_connection_identity=prepared.registration
        rt._progress_started_at=self._started_at
        rt.lifecycle_runner=runner
        self._progress_runner=runner
        if runner.binding.run_status!='faulted':
            self._observe_clock(rt,prepared.account)
            self.resolve_wait('business',phase='clock_wait')
        retained=storage.load_runtime_control().get('inflight_flow_id')
        inventory=self.client.call(rt.credentials,'POST',f'/workers/{rt.credentials.worker_id}/wechat/native-work',
            {**rt.account,'retained_flow_id':retained})
        if (inventory.get('schema_version')!='hook-native-work-v1'
                or any(inventory.get(k)!=v for k,v in rt.account.items())):
            raise ValueError('HOOK_WORK_INVENTORY_INVALID')
        self._sync_cache.prune({(scope.process_generation,b['binding_id'],b['binding_revision'])
                                for b in inventory['bindings']})
        self.resolve_wait('business',phase='connection_wait')
        # Counts come from the inventory already read by business, no extra query.
        emit('gauge', name='inventory.retained_sends', count=len(inventory['retained_sends']))
        emit('gauge', name='inventory.invitations', count=len(inventory['invitations']))
        emit('gauge', name='inventory.voice_jobs', count=sum(len(b.get('voice_jobs',[])) for b in inventory['bindings']))
        from . import hook_read_retry
        if hook_read_retry.pending():
            recovery_bindings=[m['reconciliation_binding'] for m in inventory['retained_sends'] if m.get('reconciliation_binding')]
            hook_read_retry.cancel_retired_history(rt.scope.process_generation,
                [hook_read_retry.history_subject(rt.delivery(b),b['history_since_create_time'])
                 for b in [*inventory['bindings'],*inventory.get('retired_evidence',[]),*recovery_bindings]])
        self._observation_query_version=inventory.get('observation_query_version',0)
        rt._observation_query_version=self._observation_query_version
        # Presentation is authorized by the validated inventory, even when the
        # original fault gate forbids all customer/business reconciliation.
        for binding in inventory['bindings']:
            self._publish_open_handoffs(runner,binding)
        self._prune_presentation_state(runner,inventory)
        if runner.binding.run_status=='faulted':
            self._observe_clock(rt,prepared.account)
            self.resolve_wait('business',phase='clock_wait')
        self._adopt_flow(runner,inventory)
        def update_continuation(binding=None, batch_id=None):
            # Only the original persisted Flow and its original batch may
            # continue after update intent. Latest inventory is still required
            # and the normal lease/history/permission guards remain in force.
            flow=inventory['inflight_flow']
            fid=flow.get('flow_id')
            return bool(fid and fid==rt.active_flow_id
                and runner.update_flow_continuation_allowed(fid)
                and (binding is None or flow.get('conversation_id')==binding['conversation_id'])
                and (batch_id is None or inventory.get('inflight_batch_id')==batch_id))
        byid={b['binding_id']:b for b in inventory['bindings']}
        for metadata in inventory['retained_sends']:
            binding=metadata.get('reconciliation_binding') or byid.get(metadata['binding_id'])
            if not binding:raise ValueError('HOOK_RETAINED_BINDING_NOT_AUTHORIZED')
            with measurement('recovery.send', metadata):
                if not self._recover_send(binding,metadata):return
        current=inventory.get('inflight_task')
        for metadata in inventory.get('inflight_unpermitted_tasks',[]):
            # Only the server's original Flow/batch may select a local request.
            # Never scan/replay arbitrary recovery directories or current bindings.
            if (metadata['flow_id']!=rt.active_flow_id
                    or metadata['batch_id']!=inventory.get('inflight_batch_id')
                    or metadata['conversation_id']!=inventory['inflight_flow'].get('conversation_id')):
                raise ValueError('HOOK_RECOVERY_IDENTITY_MISMATCH')
            request=self.private_root/'recovery'/'send_unpermitted'/metadata['task_id']/'request.json'
            if not request.exists():continue
            saved=rules.loads(request.read_bytes())
            if any(saved.get(k)!=metadata[k] for k in
                   ('task_id','reply_action_id','flow_id','conversation_id','lease_fencing_token')):
                raise ValueError('HOOK_RECOVERY_IDENTITY_MISMATCH')
            rt.close_unpermitted_reply({'id':metadata['task_id']},{'id':metadata['reply_action_id']},
                metadata['flow_id'],metadata['conversation_id'],metadata['lease_fencing_token'])
            return
        if current and current['task_type']=='add_friend':
            with measurement('recovery.invitation', {'task_id':current.get('id')}):
                self._recover_invitation(current)
            return
        if self._observation_query_version!=1:
            self._report_wait('HOOK_MODEL_OBSERVATION_UNSUPPORTED');return
        if current and current['status'] in {'completed','failed','cancelled','canceled'}:
            bid=inventory.get('inflight_batch_id')
            more=False
            if current['task_type']=='chat_reply' and bid:
                view=read_batch_view(self.client,rt.credentials,runner,bid,
                    observation_query_version=self._observation_query_version,observe_only=True)
                more=(view.get('reply_sequence') or {}).get('terminal') is False
            if not more or inventory['run_status']=='faulted':
                rt.finish(current['id'],rt.active_flow_id,current['lease_fencing_token'],inventory['inflight_flow'].get('conversation_id'))
                runner.api.inflight_flow_id=None;publish_current_task(runner,None);return
        if storage.load_runtime_control().get('update_no_new_work'):
            # Replay only already-enqueued deliveries; do not ingest another
            # customer's history or create a new task while draining.
            for binding in inventory['bindings']:
                rt.drain_pending(binding)
        for binding in inventory.get('retired_evidence',[]):
            rt.drain_pending(binding)
        if self._recover_voice_attempts([*inventory['bindings'],*inventory.get('retired_evidence',[])]):return
        if storage.load_runtime_control().get('update_no_new_work') and not update_continuation():
            return
        if inventory['run_status']=='faulted' or runner.binding.run_status=='faulted':return
        if not services.startup_window_ready_for(prepared):
            self._report_wait('HOOK_STARTUP_WINDOW_PENDING');return
        self._business_cycle_verified=True
        if not storage.load_runtime_control().get('update_no_new_work'):
            self._discover_invitations(runner,inventory['invitations'],inventory['bindings'],
                inventory.get('relationship_bindings',[]) if inventory.get('relationship_query_version')==1 else [])
        if not rt.active_flow_id and time.monotonic()>=self._housekeeping_due:
            from .hook_retention import retire_inbox,retire_c1_roi,retire_history_artifacts
            from .config import CONFIG
            retire_inbox(self.private_root,retention_days=CONFIG.artifact_critical_retention_days)
            retire_c1_roi(self.private_root,retention_days=CONFIG.artifact_success_retention_days,artifact_root=CONFIG.app_dir/"artifacts")
            retire_history_artifacts(self.private_root,retention_days=CONFIG.artifact_critical_retention_days)
            with storage.db_connection() as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='hook_active_calls'").fetchone():
                    from datetime import datetime,timedelta,timezone
                    cutoff=(datetime.now(timezone.utc)-timedelta(days=CONFIG.artifact_critical_retention_days)).isoformat()
                    from .hook_read_retry import retire_completed_evidence
                    retire_completed_evidence(db,cutoff)
                    db.execute("DELETE FROM hook_active_calls WHERE state='completed' AND completed_at<?",(cutoff,));db.commit()
            self._housekeeping_due=time.monotonic()+CONFIG.artifact_cleanup_interval_seconds
        bindings=inventory['bindings']
        if bindings:
            start=self._next_customer%len(bindings)
            bindings=bindings[start:]+bindings[:start]
            self._next_customer+=1
        all_history_ready=bool(bindings)
        pending_work=bool(inventory['invitations'] or inventory['retained_sends'] or rt.active_flow_id)
        for binding in bindings:
            if storage.load_runtime_control().get('update_no_new_work') and not update_continuation(binding):
                continue
            self.diagnostic_context={'conversation_id':binding['conversation_id'],
                'hook_account_scope_sha256':hashlib.sha256((rt.scope.self_wxid+rt.scope.account_epoch).encode()).hexdigest(),
                'hook_process_generation':rt.scope.process_generation,'hook_operation':'native_customer_sync'}
            try:
                key=(rt.scope.process_generation,binding['binding_id'],binding['binding_revision'])
                barrier=rt.barrier(binding)
                cached=self._sync_cache.get(key)
                if cached and cached[0]==barrier and time.monotonic()-cached[1]<TTL_SECONDS and binding.get('history_ready',True):
                    synced=cached[2]
                    emit('counter', name='history_cache_hit')
                else:
                    emit('counter', name='history_cache_miss')
                    synced=self._read_history(binding,barrier=barrier)
                if not self.history_storage_ready():
                    raise ValueError('HOOK_HISTORY_STORAGE_CAPACITY_EXCEEDED')
                if storage.load_runtime_control().get('update_no_new_work') and not update_continuation(binding):
                    return  # The completed history receipt is already durable.
                for job in binding.get('voice_jobs',[]):
                    if storage.load_runtime_control().get('update_no_new_work'):
                        break  # No new transcription, even for this customer.
                    context=f"{binding['binding_id']}:{job['native_msg_id']}"
                    if hasattr(rt,'register_binding_progress'):
                        rt.register_binding_progress(binding,context,aliases=[job['native_msg_id']])
                    saved=self.voice.saved_parent(scope,binding['peer_wxid'],job['native_msg_id'])
                    if saved and saved['state'] in {'transcribed','failed','uncertain'}:
                        result=saved
                    else:
                        # Inventory may precede the history read that detected
                        # a human reply. Consume the newer server projection.
                        owner=(synced.get('reply_ownership') or binding.get('reply_ownership') or {}).get('owner')
                        if saved is None and owner in {'human','legacy'}:continue
                        audio=capture_voice_audio(scope,binding['peer_wxid'],job['native_msg_id'],
                            max_audio_bytes=self.voice.max_audio_bytes,verified_metadata_xml=job.get('voice_metadata_xml'))
                        if audio is None:
                            waiting=self.client.voice_audio_wait(rt.credentials,rt.account,binding,job['native_msg_id'])
                            if waiting['state']=='handed_off':
                                emit_progress(runner,job['native_msg_id'],'voice','error','handoff_voice_audio_missing')
                            elif waiting['state']=='waiting':
                                emit_progress(runner,job['native_msg_id'],'voice','waiting','voice_audio_wait')
                            pending_work=True;continue
                        admission_lock = getattr(runner, '_new_work_admission_lock', None)
                        with admission_lock if admission_lock is not None else nullcontext():
                            if saved is None and (bool(binding.get('open_handoffs')) or inventory['run_status']!='running' or not runner._can_start_new_flow() or not getattr(runner,'can_pull_tasks',lambda:True)()):
                                emit_progress(runner,job['native_msg_id'],'voice','waiting','paused_work')
                                pending_work=True;continue
                            try:
                                rt._progress_context=job['native_msg_id']
                                emit_progress(runner,job['native_msg_id'],'voice')
                                result=timed_call('hook.voice_transcription',
                                    lambda: self.voice.transcribe_once(services.connection.gateway,prepared.process,audio,
                                        automatic_transcription_disabled=self.automatic_transcription_disabled),
                                    context=rt.timing_context(synced))
                            except Exception:
                                saved=self.voice._read(audio)
                                if saved and saved['state']=='uncertain':
                                    self._invalidate_history_cache(key)
                                    self.client.voice_result(rt.credentials,rt.account,binding,saved)
                                raise
                    emit_progress(runner,job['native_msg_id'],'voice','done' if result['state']=='transcribed' else 'error' if result['state']=='failed' else 'waiting',None if result['state']=='transcribed' else 'voice_wait',occurred_at=result.get('completed_at'))
                    # Publishing a transcript changes the admitted history even
                    # when no additional WeChat callback arrives.
                    self._invalidate_history_cache(key)
                    hook_voice_receipts.publish(self.client, rt.credentials, rt.account, binding, result, self.private_root)
                    synced=self._read_history(binding)
                if not synced['history_ready']:
                    all_history_ready=False;pending_work=True;continue
                batches=list(binding['batch_ids'])
                for bid in sorted(self._live_batches_by_binding.get(binding['binding_id'],set())):
                    if bid not in batches:batches.append(bid)
                if synced.get('message_batch'):
                    bid=synced['message_batch']['batch_id']
                    if bid not in batches:batches.append(bid)
                    if synced['message_batch'].get('next_step')=='generate':self._mark_batch_presented(bid)
                # Keep polling all customers while any model is generating.
                for bid in batches:
                    if storage.load_runtime_control().get('update_no_new_work') and not update_continuation(binding,bid):
                        continue
                    data = read_batch_view(
                        self.client, rt.credentials, runner, bid,
                        observation_query_version=self._observation_query_version,
                    )
                    if hasattr(rt,'register_batch_progress'):rt.register_batch_progress(binding,bid,data)
                    live=self._publish_batch_progress(runner,bid,data)
                    tracked=self._live_batches_by_binding.setdefault(binding['binding_id'],set())
                    if live:pending_work=True;tracked.add(bid)
                    else:tracked.discard(bid)
                    if data.get('processing'):continue
                    data={**data,'batch_id':bid}
                    action=data.get('reply_action')
                    if not action or action.get('status')!='queued':continue
                    emit_progress(runner,bid,'reply_ready','done')
                    if binding.get('relationship_confirmation_pending'):
                        # Do not claim a global Flow for a customer awaiting a
                        # second read. Original receipts/history still reconcile.
                        continue
                    flow=inventory['inflight_flow']
                    if rt.active_flow_id:
                        if flow.get('conversation_id')!=binding['conversation_id']:continue
                        if inventory.get('inflight_batch_id')!=bid:continue
                    elif (inventory['run_status']!='running' or not runner._can_start_new_flow() or not getattr(runner,'can_pull_tasks',lambda:True)()
                            or not runner._worker_transaction_barrier_ready(runner.binding,reason="hook_native_dispatch")):
                        emit_progress(runner,(data.get('task') or {}).get('id',bid),'send_conditions','waiting','paused_work' if runner.binding.run_status!='running' else 'send_queue')
                        continue
                    try:
                        result=rt.reply_once(binding,data,history=synced)
                    finally:
                        # Pre/post-send reconciliation can advance the receipt
                        # without an inbox callback (including cancelled sends).
                        # The next segment must obtain a fresh dispatch receipt.
                        self._invalidate_history_cache(key)
                    runner.api.inflight_flow_id=rt.active_flow_id
                    if result:return
                    # Do not reuse this local receipt for another batch either.
                    break
            except ApiError as exc:
                if exc.status_code==409 and exc.code in {'HOOK_CONTACT_REMARK_CONFIRMATION_PENDING','HOOK_AUTHORIZATION_RETIRED','HOOK_CONTACT_REMARK_CHANGED',
                        'HOOK_BINDING_PAUSED','HOOK_BINDING_REVISION_STALE'}:
                    # Inventory can race an explicit operator retirement. Only
                    # this customer's new work stops; original sends stay in
                    # retained_sends and are reconciled on the next pass.
                    self._invalidate_history_cache(key)
                    continue
                raise
            except (ValueError, HookLocalError) as exc:
                from .hook_read_retry import ReadRetryWait
                if isinstance(exc,ReadRetryWait):raise
                if is_history_wait(exc):
                    all_history_ready=False;pending_work=True;self._report_wait(str(exc))
                    context=getattr(rt,'_history_context',None) or f"{binding['binding_id']}:initial"
                    emit_progress(runner,context,'history','waiting','callback_wait' if str(exc)=='HOOK_HISTORY_CALLBACK_NOT_LANDED' else 'history_wait')
                    continue
                raise
        self._publish_initial_read_finished(runner,inventory,history_ready=all_history_ready,pending=pending_work)
        if inventory['run_status']!='running' or rt.active_flow_id or not runner._can_start_new_flow():return
        if not runner.can_pull_tasks() or not runner._worker_transaction_barrier_ready(runner.binding,reason="hook_native_dispatch"):return
        pulled=self.client.call(rt.credentials,'GET',f'/workers/{rt.credentials.worker_id}/tasks/pull')
        task=pulled.get('task')
        if task and task['task_type']=='add_friend' and pulled.get('mode')=='pending':
            publish_current_task(runner,Task.from_api(task))
            emit_progress(runner,task['id'],'invite_received','done')
            try:
                result=rt.invite(task,lambda prep,guard:self.ui_factory(prepared.process,prep,guard))
                # Keep the terminal fact in the original task history, then
                # clear the active card. A completed invite cannot stay active.
                terminal=result['settlement'].get('task') or result['settlement']
                if rt.active_flow_id is None:
                    if terminal.get('id')==task['id']:publish_current_task(runner,Task.from_api(terminal))
                    publish_current_task(runner,None)
            finally:runner.api.inflight_flow_id=rt.active_flow_id

    def loop(self,runner,services,entered):
        entered('hook_business')
        while not runner.stop_event.is_set():
            try:self.tick(runner,services)
            except ApiError as exc:
                if exc.code=='HOOK_BACKEND_NETWORK_FAILED':self._report_wait(exc.code)
                else:raise
            runner.stop_event.wait(self.interval_seconds)
