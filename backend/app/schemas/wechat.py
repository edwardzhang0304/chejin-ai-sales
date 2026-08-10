from datetime import datetime
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts.message_limits import (
    C2_MESSAGE_BATCH_MAX_ITEMS,
    C2_MESSAGE_CONTENT_MAX_CHARS,
    C2_MESSAGE_INGEST_MAX_BYTES,
    C2_MESSAGE_RAW_PAYLOAD_MAX_BYTES,
)


class WechatSessionScanItem(BaseModel):
    rpa_session_key: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    remark_code_candidates: list[str] = Field(default_factory=list, max_length=10)
    row_fingerprint: str | None = Field(default=None, max_length=255)
    unread_hint: bool = False
    last_message_preview: str | None = Field(default=None, max_length=1000)
    ocr_confidence: float | None = None


class WechatSessionScanResultRequest(BaseModel):
    scan_id: str = Field(min_length=1, max_length=128)
    sidecar_run_id: str = Field(min_length=1, max_length=128)
    wechat_account_hint: str | None = Field(default=None, max_length=128)
    started_at: datetime
    finished_at: datetime
    sessions: list[WechatSessionScanItem] = Field(default_factory=list, max_length=200)
    evidence: dict | None = None
    scan_failed: bool = False
    error_code: str | None = Field(default=None, max_length=64)


class WechatFriendActivationConfirmRequest(BaseModel):
    authorization_revision: str = Field(min_length=1, max_length=128)
    remark_code: str = Field(min_length=1, max_length=64)
    conversation_type: str = Field(min_length=1, max_length=32)
    chat_surface_ready: bool
    title_evidence: dict


class WechatBindingRestoreRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


class WechatMessagePosition(BaseModel):
    screen_order: int = Field(ge=1)
    visual_top: int | None = Field(default=None, ge=0)
    visual_bottom: int | None = Field(default=None, ge=0)
    frame_source: str = Field(min_length=1, max_length=32)
    order_source: str = Field(
        pattern="^(visual_top|observation_index_fallback)$"
    )

    @model_validator(mode="after")
    def validate_visual_order_evidence(self):
        if self.order_source == "visual_top":
            if self.visual_top is None or self.visual_bottom is None:
                raise ValueError("visual_top 顺序必须携带气泡上下边界")
            if self.visual_bottom <= self.visual_top:
                raise ValueError("气泡上下边界不合法")
        return self


class WechatMessageItem(BaseModel):
    dedupe_key: str = Field(min_length=1, max_length=255)
    source_message_key: str = Field(min_length=1, max_length=255)
    sender_role_hint: str = Field(min_length=1, max_length=32)
    message_type: str = Field(min_length=1, max_length=32)
    content: str | None = Field(default=None, max_length=C2_MESSAGE_CONTENT_MAX_CHARS)
    occurred_at: datetime | None = None
    ocr_confidence: float | None = None
    item_state: str = Field(min_length=1, max_length=32)
    flow_state: str = Field(min_length=1, max_length=32)
    message_position: WechatMessagePosition
    raw_payload: dict

    @model_validator(mode="after")
    def validate_raw_payload_size(self):
        encoded = json.dumps(
            self.raw_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        if len(encoded) > C2_MESSAGE_RAW_PAYLOAD_MAX_BYTES:
            raise ValueError("单条消息 raw_payload 超过大小限制")
        return self


class WechatFlowGateDetail(BaseModel):
    error_code: str = Field(min_length=1, max_length=64)
    position_source: str = Field(min_length=1, max_length=64)
    min_screen_order: int | None = Field(default=None, ge=1)
    max_screen_order: int | None = Field(default=None, ge=1)
    subject_sender_role: str | None = Field(
        default=None,
        pattern="^(customer|self)$",
    )


class WechatSlotLedgerState(BaseModel):
    observation_id: str = Field(min_length=1, max_length=255)
    screen_order: int = Field(ge=1)
    order_source: str = Field(
        pattern="^(visual_top|observation_index_fallback)$"
    )
    row_kind: str = Field(min_length=1, max_length=64)
    source_message_key: str = Field(min_length=1, max_length=255)
    origin_read_run_id: str = Field(min_length=1, max_length=128)
    fact_scope: str = Field(
        pattern="^(current_read_run|historical|unknown)$"
    )
    delivery_state: str = Field(
        pattern="^(not_enqueued|outbox_waiting|backend_confirmed)$"
    )
    item_state: str = Field(pattern="^(completed|failed)$")
    ledger_state: str | None = Field(
        default=None,
        pattern="^(NEW_MESSAGE|OUTBOX_WAITING|OLD_FAILED|OLD_COMPLETED)$"
    )


class WechatIngestPartition(BaseModel):
    group_id: str = Field(min_length=1, max_length=128)
    index: int = Field(ge=1, le=C2_MESSAGE_BATCH_MAX_ITEMS)
    count: int = Field(ge=2, le=C2_MESSAGE_BATCH_MAX_ITEMS)
    expected_source_message_keys: list[str] = Field(
        min_length=1,
        max_length=C2_MESSAGE_BATCH_MAX_ITEMS,
    )

    @model_validator(mode="after")
    def validate_partition(self):
        if self.index > self.count:
            raise ValueError("消息分片序号不能超过总数")
        if len(self.expected_source_message_keys) != len(
            set(self.expected_source_message_keys)
        ):
            raise ValueError("消息分片完整清单不能重复")
        return self


class WechatMessageEvidence(BaseModel):
    model_config = ConfigDict(extra="allow")

    contract_revision: str = Field(min_length=1, max_length=32)
    contract_sha256: str = Field(min_length=64, max_length=64)
    observation_schema_version: int = Field(ge=1)
    authoritative_frame_source: str = Field(min_length=1, max_length=32)
    observations: list[dict] = Field(max_length=500)
    authorization_read_reason: str = Field(min_length=1, max_length=64)
    continuation_batch_id: str | None = Field(default=None, max_length=36)
    continuation_token: str | None = Field(default=None, max_length=64)
    finished_at: datetime
    flow_gate_errors: list[str] = Field(default_factory=list, max_length=50)
    flow_gate_details: list[WechatFlowGateDetail] = Field(
        default_factory=list,
        max_length=50,
    )
    slot_ledger_states: list[WechatSlotLedgerState] = Field(max_length=500)
    ingest_partition: WechatIngestPartition | None = None

    @model_validator(mode="after")
    def validate_continuation_pair(self):
        if bool(self.continuation_batch_id) != bool(self.continuation_token):
            raise ValueError("批次续行标识和 token 必须同时提供")
        source_keys = [
            item.source_message_key for item in self.slot_ledger_states
        ]
        screen_orders = [
            item.screen_order for item in self.slot_ledger_states
        ]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("最终画面槽位 source_message_key 不能重复")
        if len(screen_orders) != len(set(screen_orders)):
            raise ValueError("最终画面槽位 screen_order 不能重复")
        return self


class WechatMessageIngestRequest(BaseModel):
    contract_version: int = Field(ge=1, le=3)
    contract_revision: str = Field(min_length=1, max_length=32)
    contract_sha256: str = Field(min_length=64, max_length=64)
    observation_schema_version: int = Field(ge=1)
    authorization_scope: str = Field(
        default="active_read",
        pattern="^(active_read|fact_settlement)$",
    )
    read_run_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=36)
    remark_code: str = Field(min_length=1, max_length=64)
    rpa_session_key: str | None = Field(default=None, max_length=255)
    authorization_revision: str = Field(min_length=1, max_length=128)
    messages: list[WechatMessageItem] = Field(
        default_factory=list,
        max_length=C2_MESSAGE_BATCH_MAX_ITEMS,
    )
    evidence: WechatMessageEvidence

    @model_validator(mode="after")
    def validate_settlement_envelope(self):
        evidence = self.evidence.model_dump(mode="json")
        for slot in self.evidence.slot_ledger_states:
            if (
                slot.fact_scope == "current_read_run"
                and slot.origin_read_run_id != self.read_run_id
            ):
                raise ValueError("本轮事实的 origin_read_run_id 必须等于 read_run_id")
            if (
                slot.fact_scope == "historical"
                and slot.origin_read_run_id == self.read_run_id
            ):
                raise ValueError("历史事实不能归属于当前 read_run_id")
        message_source_keys = {
            item.source_message_key for item in self.messages
        }
        slot_source_keys = {
            item.source_message_key
            for item in self.evidence.slot_ledger_states
        }
        if message_source_keys and not message_source_keys.issubset(
            slot_source_keys
        ):
            raise ValueError("每条入库消息必须具有槽位轮次归属")
        slots_by_source = {
            item.source_message_key: item
            for item in self.evidence.slot_ledger_states
        }
        for message in self.messages:
            slot = slots_by_source.get(message.source_message_key)
            if slot is None:
                continue
            if slot.item_state != message.item_state:
                raise ValueError("消息终态必须与槽位 item_state 一致")
            if slot.screen_order != message.message_position.screen_order:
                raise ValueError("消息顺序必须与槽位 screen_order 一致")
        settlement_fields = {
            "recovery_transaction_id": str(
                evidence.get("recovery_transaction_id") or ""
            ).strip(),
            "action_kind": str(evidence.get("action_kind") or "").strip(),
            "source_message_key_digest": str(
                evidence.get("source_message_key_digest") or ""
            ).strip(),
            "settlement_mode": str(
                evidence.get("settlement_mode") or ""
            ).strip(),
        }
        if self.authorization_scope == "active_read":
            if any(settlement_fields.values()):
                raise ValueError("普通读取不能携带事实结算字段")
            return self
        if not all(settlement_fields.values()):
            raise ValueError("事实结算缺少恢复事务字段")
        if settlement_fields["action_kind"] not in {"voice", "image"}:
            raise ValueError("事实结算 action_kind 不合法")
        if settlement_fields["settlement_mode"] not in {
            "fact_only",
            "technical_terminal",
        }:
            raise ValueError("事实结算 settlement_mode 不合法")
        digest = settlement_fields["source_message_key_digest"]
        if len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest.lower()
        ):
            raise ValueError("事实结算 source_message_key_digest 不合法")
        if self.evidence.authoritative_frame_source != "action_journal_recovery":
            raise ValueError("事实结算必须使用 action_journal_recovery 证据")
        return self

    @model_validator(mode="after")
    def validate_request_size(self):
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > C2_MESSAGE_INGEST_MAX_BYTES:
            raise ValueError("消息入库请求超过大小限制")
        return self
