"""Formal Hook actions retain original task leases and form values."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from app.schemas.hook_identity import NativeWechatIdentity

class HookAccountAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    self_wxid: NativeWechatIdentity
    account_epoch: str = Field(min_length=36, max_length=36)
    process_generation: str = Field(min_length=1, max_length=128)

class HookInviteSubmission(HookAccountAction):
    # Optional for original durable proofs created before dashboard instrumentation.
    submitted_at: str | None = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}T.*\+00:00$')
    attempted_at: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}T.*\+00:00$')
    preparation_id: str = Field(min_length=36, max_length=36)
    form_verified: Literal[True]
    greeting_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    remark_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    greeting_copy_calls: Literal[1]
    greeting_paste_calls: Literal[1]
    remark_copy_calls: Literal[1]
    remark_paste_calls: Literal[1]
    confirm_click_calls: Literal[1]
    form_closed: Literal[True]
    submit_journal_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="before")
    @classmethod
    def exact_effect_types(cls, value):
        if not isinstance(value, dict):
            return value
        if value.get('form_verified') is not True or value.get('form_closed') is not True:
            raise ValueError("Form proof requires exact true booleans")
        for name in ('greeting_copy_calls', 'greeting_paste_calls', 'remark_copy_calls', 'remark_paste_calls', 'confirm_click_calls'):
            if type(value.get(name)) is not int:
                raise ValueError("Action counts must be exact integers")
        return value

class HookFriendBinding(HookAccountAction):
    source_task_id: str = Field(min_length=36, max_length=36)
    friend_list: dict | list
    raw_evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class HookHistoryReconciliation(HookAccountAction):
    reply_action_id: str = Field(min_length=36, max_length=36)
    task_id: str = Field(min_length=36, max_length=36)
    flow_id: str = Field(min_length=36, max_length=36)
    lease_fencing_token: int = Field(ge=1)
    send_token: str = Field(min_length=1, max_length=128)
    journal_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class HookHistoryCoverage(HookAccountAction):
    binding_id: str = Field(min_length=36, max_length=36)
    binding_revision: int = Field(ge=1)
    capture_id: str = Field(min_length=36, max_length=36)
    coverage: dict
    coverage_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    reconciliation: HookHistoryReconciliation | None = None


class HookNativeSendClaim(HookAccountAction):
    task_id: str = Field(min_length=36,max_length=36)
    history_state_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

class HookNativeSendBegin(HookNativeSendClaim):
    send_token: str = Field(min_length=1,max_length=128)
    journal_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    attempted_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T.*\+00:00$")

class HookNativeSendSettlement(HookAccountAction):
    task_id: str = Field(min_length=36,max_length=36)
    send_token: str = Field(min_length=1,max_length=128)
    journal_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    completed_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T.*\+00:00$")
    call_completed: bool


class HookVoiceResult(HookAccountAction):
    binding_id: str = Field(min_length=36,max_length=36)
    binding_revision: int = Field(ge=1)
    peer_wxid: NativeWechatIdentity
    native_msg_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    attempt_process_generation: str | None = Field(default=None,min_length=1,max_length=128)
    state: Literal['transcribed','failed','uncertain']
    audio_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    audio_bytes: int = Field(ge=1,le=8*1024*1024)
    attempted_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T.*\+00:00$")
    completed_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T.*\+00:00$")
    response_sha256: str | None = Field(default=None,pattern=r"^[a-f0-9]{64}$")
    text: str | None = Field(default=None,max_length=20000)
    error_code: str | None = Field(default=None,pattern=r"^HOOK_[A-Z0-9_]+$",max_length=64)
    attempt_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode='after')
    def result_fields(self):
        if self.state=='transcribed':
            if not self.text or not self.text.strip() or not self.response_sha256 or self.error_code is not None:
                raise ValueError('Completed voice requires exact transcript evidence')
        elif self.text is not None or not self.error_code:
            raise ValueError('Unknown/failed voice cannot supply guessed text')
        from datetime import datetime
        if datetime.fromisoformat(self.completed_at)<datetime.fromisoformat(self.attempted_at):
            raise ValueError('Voice result time precedes its attempt')
        return self


class HookWorkRequest(HookAccountAction):
    retained_flow_id: str | None = None

    @field_validator('retained_flow_id')
    @classmethod
    def validate_retained_flow(cls, value):
        from app.contracts.shared_rules import shared_adapter
        if value is not None and not shared_adapter('hook_contract_rules').identifier(value):
            raise ValueError('HOOK_FLOW_ID_INVALID')
        return value


class HookNativeNoTrigger(HookAccountAction):
    task_id: str = Field(min_length=36,max_length=36)
    send_token: str = Field(min_length=1,max_length=128)
    proof: dict
    proof_digest: str = Field(pattern=r'^[a-f0-9]{64}$')


class HookRecoveryRequest(HookAccountAction):
    """Terminal reconciliation only; never a new work/send permission."""
    kind: Literal['flow_start', 'send_claim', 'send_unpermitted']
    task_id: str = Field(min_length=36, max_length=36)
    flow_id: str = Field(min_length=36, max_length=36)
    flow_kind: Literal['task', 'chat_reply']
    conversation_id: str | None = Field(default=None, min_length=36, max_length=36)
    reply_action_id: str | None = Field(default=None, min_length=36, max_length=36)
    original_process_generation: str | None = Field(default=None, min_length=1, max_length=128)
    lease_fencing_token: int | None = Field(default=None, ge=1)
    evidence_digest: str = Field(pattern=r'^[a-f0-9]{64}$')


class HookRelationshipObservation(HookAccountAction):
    binding_id: str = Field(min_length=36,max_length=36)
    binding_revision: int = Field(ge=1)
    friend_list: dict | list
    raw_evidence_digest: str = Field(pattern=r'^[a-f0-9]{64}$')


class HookReinviteRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    binding_id: str = Field(min_length=36,max_length=36)
    binding_revision: int = Field(ge=1)
    confirm_end_previous: Literal[True]
    reason: str = Field(min_length=1, max_length=500)

    @field_validator('confirm_end_previous', mode='before')
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation is required')
        return value


class HookBindingTransferRequest(HookReinviteRequest):
    source_task_id: str = Field(min_length=36, max_length=36)
