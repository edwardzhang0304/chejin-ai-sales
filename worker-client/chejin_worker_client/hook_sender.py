"""One physical Hook call per durable ReplyAction attempt, never automatic retry."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import random
import time
from pathlib import Path
from .hook_formal_c1 import _save
from .shared_rules import hook_contract_rules as rules
from .hook_gateway import HookLocalError
from .hook_active_calls import reserve,finish
from .hook_timing import timed_call


@dataclass(frozen=True)
class NativeSendPermit:
    worker_id: str
    client_instance_id: str
    task_id: str
    reply_action_id: str
    self_wxid: str
    peer_wxid: str
    account_epoch: str
    process_generation: str
    history_state_digest: str
    lease_fencing_token: int
    send_token: str = field(repr=False)
    reply_text: str = field(repr=False)
    reply_text_hash: str
    expire_at: str
    flow_id: str | None = None
    clock: object | None = field(default=None,repr=False,compare=False)

    @classmethod
    def capture(cls,value,*,clock=None):
        if not isinstance(value,dict) or value.get('schema_version')!='hook-native-send-v1' or value.get('native_send_ready') is not True:
            raise ValueError('HOOK_SEND_PERMIT_INVALID')
        permit=cls(**{name:value[name] for name in cls.__dataclass_fields__ if name not in {'flow_id','clock'}},flow_id=value.get('flow_id',value['task_id']),clock=clock)
        if (any(not rules.identifier(getattr(permit,n)) for n in ('worker_id','task_id','reply_action_id','account_epoch'))
                or not rules.wxid(permit.self_wxid) or not rules.wxid(permit.peer_wxid) or permit.self_wxid==permit.peer_wxid
                or not rules.identifier(permit.flow_id) or not rules.bounded(permit.client_instance_id,128) or not rules.bounded(permit.process_generation,128)
                or type(permit.lease_fencing_token) is not int or permit.lease_fencing_token<=0
                or not rules.bounded(permit.send_token,128) or not isinstance(permit.reply_text,str) or not 0<len(permit.reply_text)<=20000
                or hashlib.sha256(permit.reply_text.encode()).hexdigest()!=permit.reply_text_hash
                or not rules.aware(permit.expire_at)):
            raise ValueError('HOOK_SEND_PERMIT_INVALID')
        return permit

    def account(self):
        return {name:getattr(self,name) for name in ('self_wxid','account_epoch','process_generation')}

    def validate_scope(self,process,scope):
        if (self.process_generation!=process.generation or self.process_generation!=scope.process_generation
                or self.self_wxid!=scope.self_wxid or self.account_epoch!=scope.account_epoch
                or datetime.fromisoformat(self.expire_at)<=(self.clock.upper_now() if self.clock else datetime.now(timezone.utc))):
            raise HookLocalError('HOOK_SEND_PERMIT_EXPIRED_OR_CHANGED')


@dataclass(frozen=True)
class RetainedNativeSendPermit:
    """Only the identity required to settle a previously journaled send."""
    worker_id: str
    client_instance_id: str
    task_id: str
    reply_action_id: str
    self_wxid: str
    account_epoch: str
    process_generation: str
    lease_fencing_token: int
    flow_id: str
    send_token: str = field(repr=False)
    journal_sha256: str = ''

    def account(self):
        return {name:getattr(self,name) for name in ('self_wxid','account_epoch','process_generation')}

    @classmethod
    def load(cls,private_root,credentials,scope,binding,metadata):
        if (not isinstance(metadata,dict)
                or any(not rules.identifier(metadata.get(k)) for k in ('task_id','reply_action_id','flow_id'))
                or type(metadata.get('lease_fencing_token')) is not int or metadata['lease_fencing_token']<=0):
            raise ValueError('HOOK_SEND_RETAINED_IDENTITY_INVALID')
        root=private_root/credentials.worker_id/scope.account_epoch/metadata['reply_action_id']
        journal=root/'intent.json';trigger=root/'trigger.json';outcome_path=root/'outcome.json'
        try:
            intent=rules.loads(journal.read_bytes());called=rules.loads(trigger.read_bytes());outcome=rules.loads(outcome_path.read_bytes())
            digest=hashlib.sha256(journal.read_bytes()).hexdigest()
            if (intent.get('schema_version')!='hook-send-journal-v1'
                    or intent.get('task_id')!=metadata['task_id'] or intent.get('reply_action_id')!=metadata['reply_action_id']
                    or any(intent.get(k)!=getattr(scope,k) for k in ('self_wxid','account_epoch','process_generation'))
                    or intent.get('peer_wxid')!=binding['peer_wxid']
                    or outcome.get('task_id')!=metadata['task_id'] or outcome.get('reply_action_id')!=metadata['reply_action_id']
                    or type(outcome.get('call_completed')) is not bool or outcome.get('automatic_resend_allowed') is not False
                    or called.get('phase')!='trigger_attempted' or called.get('intent_sha256')!=digest
                    or outcome.get('journal_sha256')!=digest or metadata.get('journal_sha256',digest)!=digest
                    or not rules.bounded(intent.get('send_token'),128)
                    or not rules.aware(intent.get('attempted_at')) or not rules.aware(outcome.get('completed_at'))
                    or datetime.fromisoformat(outcome['completed_at'])<datetime.fromisoformat(intent['attempted_at'])):
                raise ValueError()
        except (OSError,KeyError,TypeError,ValueError):
            raise ValueError('HOOK_SEND_RETAINED_JOURNAL_INVALID') from None
        return cls(credentials.worker_id,credentials.client_instance_id,metadata['task_id'],metadata['reply_action_id'],
            scope.self_wxid,scope.account_epoch,scope.process_generation,metadata['lease_fencing_token'],metadata['flow_id'],
            intent['send_token'],digest),outcome


class HookNativeSender:
    def __init__(self,gateway,client,credentials,private_root:Path,*,wait=time.sleep,delay=lambda:random.uniform(.5,3),clock=None,progress=None,timing_context=None):
        self.progress=progress or (lambda *args:None)
        self.gateway,self.client,self.credentials,self.private_root=gateway,client,credentials,private_root
        self.wait,self.delay=wait,delay
        self.timing_context=dict(timing_context or {})
        self.now=clock.now if clock else (lambda:datetime.now(timezone.utc))

    def _progress(self,phase,state,note=None):
        try:self.progress(phase,state,note)
        except Exception:pass  # A display failure cannot alter physical journals.

    def execute(self,permit,process,scope,refresh):
        if (permit.worker_id!=self.credentials.worker_id or permit.client_instance_id!=self.credentials.client_instance_id):
            raise ValueError('HOOK_SEND_OWNER_MISMATCH')
        root=self.private_root/permit.worker_id/permit.account_epoch/permit.reply_action_id
        journal=root/'intent.json';trigger=root/'trigger.json'
        if journal.exists() or trigger.exists():raise ValueError('HOOK_SEND_PREVIOUS_ATTEMPT_REQUIRES_SETTLEMENT')
        quarantine=self.private_root/permit.worker_id/permit.account_epoch/'send-quarantine.json'
        if quarantine.exists():
            saved=rules.loads(quarantine.read_bytes())
            if saved.get('process_generation',process.generation)==process.generation:
                raise ValueError('HOOK_SEND_ACCOUNT_QUARANTINED')
        seconds=self.delay()
        if type(seconds) not in (int,float) or not .5<=seconds<=3:raise ValueError('HOOK_SEND_DELAY_INVALID')
        # Queue/lock time cannot count toward this per-task delay, including the
        # first action. Waiting never occupies the physical action lock.
        self._progress('send_delay','current')
        timed_call('hook.send_delay', lambda: self.wait(seconds), context=self.timing_context)
        self._progress('send_delay','done')
        self._progress('pre_send','current')
        barrier=refresh(permit)
        permit.validate_scope(process,scope)
        if not isinstance(barrier,tuple) or len(barrier)!=2 or barrier[1]!=0:
            raise ValueError('HOOK_SEND_SYNC_PENDING')
        self._progress('pre_send','done')
        def begin():
            attempted=self.now().isoformat()
            _save(journal,{'schema_version':'hook-send-journal-v1','reply_action_id':permit.reply_action_id,
                'task_id':permit.task_id,**permit.account(),'peer_wxid':permit.peer_wxid,
                'reply_text_hash':permit.reply_text_hash,'send_token':permit.send_token,
                'history_state_digest':permit.history_state_digest,'attempted_at':attempted,'delay_seconds':seconds})
            digest=hashlib.sha256(journal.read_bytes()).hexdigest()
            response=self.client.native_send_begin(self.credentials,permit,
                {**permit.account(),'task_id':permit.task_id,'history_state_digest':permit.history_state_digest,
                 'send_token':permit.send_token,'journal_sha256':digest,'attempted_at':attempted})
            if response.get('may_call_hook') is not True or response.get('duplicated') is not False:
                raise HookLocalError('HOOK_SEND_BEGIN_NOT_CONFIRMED')
        def mark_trigger():
            reserve(process.generation,permit.reply_action_id,'send_text')
            _save(trigger,{'phase':'trigger_attempted','time_utc':self.now().isoformat(),
                'intent_sha256':hashlib.sha256(journal.read_bytes()).hexdigest()})
            self._progress('send_submit','current')
        try:
            response=timed_call('hook.send_call',
                lambda: self.gateway.send_text_once(process,scope,permit,barrier,refresh.barrier,begin,mark_trigger),
                context=self.timing_context)
            outcome={'call_completed':True,'hook_submission_flag':response.payload.get('SendTextMsg')=='1',
                'response_sha256':hashlib.sha256(response.raw_body).hexdigest()}
            _save(root/'response.json',response.payload)
            finish(process.generation,permit.reply_action_id,True)
            self._progress('send_submit','done' if outcome['hook_submission_flag'] else 'waiting',None if outcome['hook_submission_flag'] else 'send_wait')
        except HookLocalError as exc:
            if not trigger.exists():raise
            finish(process.generation,permit.reply_action_id,not exc.outcome_unknown)
            outcome={'call_completed':False,'error_code':exc.code,'quarantined':exc.outcome_unknown}
            self._progress('send_submit','waiting','send_wait')
            if exc.outcome_unknown:
                _save(quarantine,{'reply_action_id':permit.reply_action_id,'reason_code':exc.code,'process_generation':process.generation,'automatic_clear':False})
        outcome.update({'reply_action_id':permit.reply_action_id,'task_id':permit.task_id,
            'journal_sha256':hashlib.sha256(journal.read_bytes()).hexdigest(),
            'completed_at':self.now().isoformat(),'automatic_resend_allowed':False})
        _save(root/'outcome.json',outcome)
        return outcome
