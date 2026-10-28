"""Wire the verified 3.9 controller to server-owned task values and durable proof.

The caller owns the original UI lease and active task/account guards. Recovery
only delivers saved HTTP receipts; an attempted invitation is never clicked twice.
"""
import hashlib
from datetime import datetime, timezone
import json
import os
from threading import RLock
from pathlib import Path
from . import storage
from .models import Binding, Task, RpaResult
from .c1_receipts import AddFriendResultReceipts, confirmed_add_friend_result
from .transaction_outcomes import c1_stop_status
from .shared_rules import hook_contract_rules as rules
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.hook_c1_flow import SubmitJournal, run_flow


def _save(path, value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(path.parent,0o700)
    with path.open('x',encoding='utf-8') as stream:
        os.chmod(path,0o600)
        stream.write(rules.canonical(value));stream.flush();os.fsync(stream.fileno())


def invitation_proof(preparation, outcome, ui, journal):
    if (outcome.get('status')!='submitted_pending_recipient' or outcome.get('submit_attempted') is not True
            or outcome.get('submission_observation',{}).get('form_closed') is not True
            or outcome.get('field_verification',{}).get('mode') != 'remark_shortcode_only'
            or outcome.get('field_verification',{}).get('verified') is not True
            or not all(step in outcome.get('steps',[]) for step in ('verify_remark_shortcode','confirm_once'))):
        raise ValueError('HOOK_INVITE_SUBMISSION_UNCONFIRMED')
    events=getattr(ui,'clipboard_field_events',[])
    if len(events)!=2:
        raise ValueError('HOOK_FORMAL_CLIPBOARD_PROOF_MISSING')
    for event in events:
        if (type(event.get('copy_calls')) is not int or event['copy_calls']!=1
                or type(event.get('paste_calls')) is not int or event['paste_calls']!=1
                or event.get('clipboard_match') is not True
                or event.get('completion_guard_passed') is not True):
            raise ValueError('HOOK_FORMAL_CLIPBOARD_PROOF_MISSING')
    expected={name:hashlib.sha256(preparation[field].encode()).hexdigest()
        for name,field in [('greeting_sha256','verify_message'),('remark_sha256','remark_name')]}
    saved=json.loads(journal.path.read_text(encoding='utf-8'))
    if (saved.get('submit_attempted') is not True
            or any(saved.get(name)!=value for name,value in expected.items())):
        raise ValueError('HOOK_FORMAL_JOURNAL_MISMATCH')
    return {key:preparation[key] for key in ('self_wxid','account_epoch','process_generation','preparation_id')} | {
        **expected,'attempted_at':datetime.fromtimestamp(saved['time_utc'],timezone.utc).isoformat(),
        **({'local_attempted_at':datetime.fromtimestamp(saved['local_time_utc'],timezone.utc).isoformat()}
           if saved.get('clock_source')=='authenticated_backend_heartbeat' and 'local_time_utc' in saved else {}),
        'form_verified':True,'form_closed':True,
        'greeting_copy_calls':1,'greeting_paste_calls':1,'remark_copy_calls':1,'remark_paste_calls':1,
        'confirm_click_calls':1,'submit_journal_sha256':hashlib.sha256(journal.path.read_bytes()).hexdigest()}


class HookFormalC1:
    def __init__(self, client, credentials, private_root: Path, *, clock=None):
        self.client,self.credentials,self.private_root=client,credentials,private_root
        self.clock=clock

    def namespace(self, task_id):
        if not rules.identifier(task_id):raise ValueError('HOOK_TASK_ID_INVALID')
        return self.private_root/self.credentials.worker_id/task_id

    def prepare(self, task_id, token, account):
        path=self.namespace(task_id)/'preparation.json'
        if path.exists():raise ValueError('HOOK_FORMAL_RUN_ALREADY_PREPARED')
        # Durable proof precedes the first preparation request. Missing legacy
        # files are not proof that an interrupted UI never ran.
        _save(self.namespace(task_id)/'pre-ui.json', {
            'task_id':task_id, 'flow_id':task_id, 'lease_fencing_token':token,
            'worker_id':self.credentials.worker_id, 'client_instance_id':self.credentials.client_instance_id,
            **account, 'phase':'before_ui',
        })
        prep=self.client.prepare(self.credentials,task_id,token,account)
        if (prep.get('task_id')!=task_id or prep.get('lease_fencing_token')!=token
                or any(prep.get(key)!=value for key,value in account.items())
                or not rules.identifier(prep.get('preparation_id'))
                or not isinstance(prep.get('verify_message'),str)
                or prep.get('remark_name')!=prep.get('remark_code')
                or not prep.get('primary_phone')):
            raise ValueError('HOOK_FORMAL_PREPARATION_INVALID')
        _save(path,prep)
        return prep

    def execute_prepared(self, task_id, token, preparation, create_ui):
        root=self.namespace(task_id)
        if preparation.get('task_id')!=task_id or preparation.get('lease_fencing_token')!=token:
            raise ValueError('HOOK_FORMAL_PREPARATION_IDENTITY_MISMATCH')
        if json.loads((root/'preparation.json').read_text(encoding='utf-8'))!=preparation:
            raise ValueError('HOOK_FORMAL_PREPARATION_CHANGED')
        if any((root/name).exists() for name in ('outcome.json','submit-attempt.json','ui-started.json')):
            raise ValueError('HOOK_FORMAL_PREVIOUS_ATTEMPT_REQUIRES_SETTLEMENT')
        # Persist before constructing the UI too: a constructor failure must
        # not be mistaken for a definitely untouched WeChat session.
        _save(root/'ui-started.json', {'task_id':task_id, 'lease_fencing_token':token})
        ui=create_ui()
        journal=SubmitJournal(root/'submit-attempt.json', clock=self.clock)
        outcome=run_flow(ui,phone=preparation['primary_phone'],verify_message=preparation['verify_message'],
            remark_name=preparation['remark_name'],remark_code=preparation['remark_code'],journal=journal)
        _save(root/'outcome.json',outcome)
        if outcome.get('status')!='submitted_pending_recipient':return outcome
        proof=invitation_proof(preparation,outcome,ui,journal)
        # Reporting precision is optional; a stale clock must not lose the
        # original successful UI proof or introduce a second invitation.
        try:
            proof['submitted_at'] = (self.clock.now() if self.clock else datetime.now(timezone.utc)).isoformat()
        except Exception:
            pass  # Legacy bounded attempt-to-receipt interval remains available.
        _save(root/'submission-proof.json',proof)
        return {**outcome,'formal_submission_proof_saved':True}

    def settle(self, task_id, token):
        root=self.namespace(task_id)
        proof=json.loads((root/'submission-proof.json').read_text(encoding='utf-8'))
        prep=json.loads((root/'preparation.json').read_text(encoding='utf-8'))
        if prep.get('task_id')!=task_id or prep.get('lease_fencing_token')!=token:
            raise ValueError('HOOK_FORMAL_SETTLEMENT_IDENTITY_MISMATCH')
        saved=root/'backend-submission-receipt.json'
        if saved.exists():return json.loads(saved.read_text(encoding='utf-8'))
        response=self.client.submitted(self.credentials,task_id,token,proof)
        task=response.get('task',{})
        if task.get('id')!=task_id or task.get('status')!='completed' or task.get('result_code')!='invite_sent':
            raise ValueError('HOOK_FORMAL_SETTLEMENT_UNCONFIRMED')
        _save(saved,response)
        return response


class HookC1ResultReceipts(AddFriendResultReceipts):
    """Hook evidence adapter to the original C1 result/stop/settlement contract."""
    def __init__(self, runtime):
        self.runtime = runtime
        self._restart_recovery_lock = RLock()
        self._run_status_persistence_pending = True  # Confirm the server stop before replay.
        self.api = self

    _receipt_load = staticmethod(storage.load_c2_state)
    _receipt_save = staticmethod(storage.save_c2_state)

    def binding(self):
        runner = getattr(self.runtime, 'lifecycle_runner', None)
        if runner is not None:
            return runner.binding
        c = self.runtime.credentials
        saved = storage.load_binding()
        if saved and (saved.worker_id != c.worker_id or saved.client_instance_id != c.client_instance_id):
            raise ValueError('HOOK_C1_RECEIPT_IDENTITY_MISMATCH')
        return saved or Binding(c.worker_id, c.worker_token, c.client_instance_id, 'running')

    def set_run_status(self, status):
        runner = getattr(self.runtime, 'lifecycle_runner', None)
        if runner is not None:
            if not runner.set_run_status(status):
                raise ValueError('HOOK_C1_STOP_UNCONFIRMED')
            return
        # Headless formal runtime uses the same durable state and authenticated API.
        binding = self.binding()
        binding.run_status = 'faulted' if binding.run_status == 'faulted' else status
        storage.save_binding(binding)
        storage.request_runtime_pause()
        data = self.runtime.client.call(self.runtime.credentials, 'POST',
            f'/workers/{binding.worker_id}/run-status',
            {'client_instance_id': binding.client_instance_id, 'run_status': binding.run_status})
        if data.get('run_status') != binding.run_status:
            raise ValueError('HOOK_C1_STOP_UNCONFIRMED')

    def settle_task_failure(self, binding, receipt):
        return self._settle(receipt, 'fail', {k: receipt[k] for k in ('error_code', 'failure_step', 'failure_remark')})

    def settle_task_success(self, binding, receipt):
        return self._settle(receipt, receipt['result_code'].replace('_', '-'), {'remark': receipt['remark']})

    def _settle(self, receipt, endpoint, fields):
        value = self.runtime.client.call(self.runtime.credentials, 'POST',
            f"/tasks/{receipt['task_id']}/{endpoint}", {**fields, 'settlement_only': True},
            task_id=receipt['task_id'], flow_id=receipt['flow_id'], token=receipt['lease_fencing_token'])
        return Task.from_api(value)

    def stop_for_cleanup(self, outcome):
        if outcome.get('cleanup') == 'failed_preserve_business_outcome':
            self.set_run_status('faulted')

    def settle_saved(self, task):
        rt = self.runtime
        root = rt.c1.namespace(task['id'])
        prep = rules.loads((root/'preparation.json').read_bytes())
        outcome = rules.loads((root/'outcome.json').read_bytes())
        if (prep.get('task_id') != task['id'] or prep.get('lease_fencing_token') != task['lease_fencing_token']
                or any(prep.get(k) != rt.account[k] for k in ('self_wxid', 'account_epoch'))
                or outcome.get('submit_attempted') is not False or (root/'submit-attempt.json').exists()):
            raise ValueError('HOOK_C1_RECEIPT_IDENTITY_MISMATCH')
        success = outcome.get('status') == 'already_friend'
        raw_code = outcome.get('error_code')
        # Only this pre-lookup content failure, with verified cleanup and no
        # guard failure, is task-local. Original receipt/finish gates still run.
        query_failure = (raw_code == 'SEARCH_QUERY_UNCONFIRMED'
            and outcome.get('status') == 'stopped'
            and outcome.get('steps', [])[-1:] == ['query_verified_and_network_lookup']
            and outcome.get('cleanup') == 'search_empty_verified'
            and not outcome.get('guard_failures'))
        code = ('PHONE_NOT_FOUND' if outcome.get('status') == 'not_found' else
                raw_code if query_failure or c1_stop_status(raw_code) else 'WECHAT_UI_LAYOUT_UNRESOLVED')
        result = RpaResult(ok=success, result_code='already_friend' if success else None,
            error_code=None if success else code, failure_step='hook_c1',
            message='客户已是好友' if success else
                '搜索号码未核对通过，本次未提交好友申请；搜索内容已清空，请核对后重新安排。' if query_failure else
                '加好友未提交；原始结果已保留')
        self.stop_for_cleanup(outcome)
        return self._settle_result(task, result, kind='success' if success else 'failure')

    def settle_before_ui(self, task):
        rt=self.runtime
        root=rt.c1.namespace(task['id'])
        intent=root/'pre-ui.json'
        if (not intent.exists() or any((root/name).exists() for name in
                ('ui-started.json','submit-attempt.json','submission-proof.json','outcome.json'))):
            raise ValueError('HOOK_INVITATION_RECOVERY_REQUIRES_ORIGINAL_EVIDENCE')
        saved=rules.loads(intent.read_bytes())
        expected={'task_id':task['id'],'flow_id':rt.active_flow_id,
            'lease_fencing_token':task['lease_fencing_token'],
            'worker_id':rt.credentials.worker_id,'client_instance_id':rt.credentials.client_instance_id,
            **{k:rt.account[k] for k in ('self_wxid','account_epoch')},'phase':'before_ui'}
        if any(saved.get(k)!=v for k,v in expected.items()):
            raise ValueError('HOOK_C1_RECEIPT_IDENTITY_MISMATCH')
        return self._settle_result(task, RpaResult(ok=False,error_code='WORKER_INTERRUPTED',
            failure_step='hook_c1_prepare',message='加好友准备已中断；确认尚未操作微信，按原任务结算'),kind='failure')

    def _settle_result(self, task, result, *, kind):
        binding = self.binding()
        self._save_add_friend_result(binding, Task.from_api(task), result, kind=kind)
        settled = self._deliver_add_friend_result(binding, task['id'])
        if settled is None:
            receipt=self._receipt_load(self._inflight_finish_receipt_key(task['id']))[f'task_{kind}']
            settled=Task.from_api(task)
            if not confirmed_add_friend_result(settled,receipt,kind):
                raise ValueError('HOOK_C1_TERMINAL_TASK_CONFIRMATION_REQUIRED')
        return settled.raw
