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
    last_message_preview_time: str | None = Field(default=None, max_length=64)
    last_message_observation_id: str | None = Field(
        default=None,
        max_length=255,
    )
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
    min_screen_order: int | None = Field(default=None, ge=0)
    max_screen_order: int | None = Field(default=None, ge=0)
    gate_scope: str | None = Field(default=None, max_length=64)
    boundary_relation: str | None = Field(
        default=None,
        pattern="^(before_or_equal|after|unknown)$",
    )
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


class WechatSequenceAlignmentPair(BaseModel):
    identity_state: str = Field(
        pattern="^(committed|selected_action|frame_local_unselected)$"
    )
    worker_stable_id: str | None = Field(default=None, max_length=128)
    pre_observation_id: str = Field(min_length=1, max_length=255)
    post_observation_id: str = Field(min_length=1, max_length=255)
    pre_index: int = Field(ge=0)
    post_index: int = Field(ge=0)
    match_basis: str = Field(min_length=1, max_length=64)


class WechatSequenceAlignmentEvidence(BaseModel):
    pre_sequence_source: str = Field(
        pattern="^(action_frame|checkpoint|empty_checkpoint)$"
    )
    pre_frame_id: str = Field(min_length=1, max_length=255)
    post_frame_id: str = Field(min_length=1, max_length=255)
    alignment_status: str = Field(
        pattern="^(unique|ambiguous|unresolved|not_required)$"
    )
    candidate_alignment_count: int = Field(ge=0)
    matched_pairs: list[WechatSequenceAlignmentPair] = Field(
        default_factory=list,
        max_length=500,
    )
    old_tail_fully_consumed: bool
    new_suffix_observation_ids: list[str] = Field(
        default_factory=list,
        max_length=500,
    )

    @model_validator(mode="after")
    def validate_safe_suffix(self):
        if self.alignment_status in {"ambiguous", "unresolved"}:
            if self.old_tail_fully_consumed or self.new_suffix_observation_ids:
                raise ValueError("非唯一对齐不得声明新增尾部")
        if self.new_suffix_observation_ids and not self.old_tail_fully_consumed:
            raise ValueError("新增尾部必须建立在旧尾部已完整消费之上")
        return self


class WechatVoiceTrackingEdge(BaseModel):
    from_frame_id: str = Field(min_length=1, max_length=255)
    from_observation_id: str = Field(min_length=1, max_length=255)
    to_frame_id: str = Field(min_length=1, max_length=255)
    to_observation_id: str = Field(min_length=1, max_length=255)
    sender_role: str = Field(pattern="^(customer|self)$")
    message_type: str = Field(pattern="^voice$")
    structural_evidence: dict
    displacement_evidence: dict
    edge_candidate_count: int = Field(ge=1, le=1)

    @model_validator(mode="after")
    def validate_evidence(self):
        if not self.structural_evidence or not self.displacement_evidence:
            raise ValueError("语音跟踪边缺少结构或位移证据")
        return self


class WechatVoiceNeighborPair(BaseModel):
    pre_observation_id: str = Field(min_length=1, max_length=255)
    post_observation_id: str = Field(min_length=1, max_length=255)
    sender_role: str = Field(pattern="^(customer|self)$")
    scroll_delta_y: float


class WechatVoiceConfirmedActionMapping(BaseModel):
    canonical_action_id: str = Field(min_length=1, max_length=255)
    reserved_worker_stable_id: str = Field(min_length=1, max_length=128)
    binding_confirmed: bool
    post_observation_id: str = Field(default="", max_length=255)
    derived_observation_ids: list[str] = Field(
        default_factory=list,
        max_length=50,
    )


class WechatVoiceActionEvidence(BaseModel):
    model_config = ConfigDict(extra="allow")

    voice_action_stage: str = Field(pattern="^execute$")
    canonical_voice_action_id: str = Field(min_length=1, max_length=255)
    reserved_worker_stable_id: str = Field(min_length=1, max_length=128)
    pre_frame_id: str = Field(min_length=1, max_length=255)
    post_frame_id: str = Field(min_length=1, max_length=255)
    selected_pre_observation_id: str = Field(min_length=1, max_length=255)
    selected_action_token: str = Field(min_length=1, max_length=255)
    selected_target_fingerprint: str = Field(min_length=1, max_length=255)
    transcript_binding_status: str = Field(
        pattern="^(confirmed|failed|ambiguous)$"
    )
    transcript_binding_method: str = Field(
        pattern="^(native_source_id|continuous_target_tracking|neighbor_scroll_alignment|none)$"
    )
    binding_candidate_count: int = Field(ge=0)
    tracking_frame_ids: list[str] = Field(default_factory=list, max_length=20)
    tracking_edges: list[WechatVoiceTrackingEdge] = Field(
        default_factory=list,
        max_length=19,
    )
    matched_neighbor_pairs: list[WechatVoiceNeighborPair] = Field(
        default_factory=list,
        max_length=50,
    )
    native_source_message_id: str | None = Field(default=None, max_length=255)
    confirmed_action_mapping: WechatVoiceConfirmedActionMapping
    ui_action_performed: bool
    action_phase: str = Field(
        pattern="^(confirmed|failed|quarantined)$"
    )

    @model_validator(mode="after")
    def validate_binding_proof(self):
        mapping = self.confirmed_action_mapping
        if self.pre_frame_id == self.post_frame_id:
            raise ValueError("语音动作前后帧必须不同")
        if (
            mapping.canonical_action_id != self.canonical_voice_action_id
            or mapping.reserved_worker_stable_id
            != self.reserved_worker_stable_id
        ):
            raise ValueError("语音动作映射与动作身份不一致")
        if self.ui_action_performed is not True:
            raise ValueError("语音 execute 证据必须声明真实 UI 动作")
        expected_phase = {
            "confirmed": "confirmed",
            "failed": "failed",
            "ambiguous": "quarantined",
        }[self.transcript_binding_status]
        if self.action_phase != expected_phase:
            raise ValueError("语音绑定终态与动作阶段不一致")

        confirmed = self.transcript_binding_status in {
            "confirmed",
            "failed",
        }
        if confirmed:
            if (
                self.binding_candidate_count != 1
                or mapping.binding_confirmed is not True
                or not mapping.post_observation_id
            ):
                raise ValueError("语音已结算事实缺少唯一动作绑定")
        elif (
            mapping.binding_confirmed is not False
            or mapping.post_observation_id
        ):
            raise ValueError("歧义语音不得绑定正式消息身份")

        method = self.transcript_binding_method
        native_id = str(self.native_source_message_id or "").strip()
        if method == "continuous_target_tracking":
            frames = [str(value or "").strip() for value in self.tracking_frame_ids]
            if (
                not confirmed
                or native_id
                or self.matched_neighbor_pairs
                or len(frames) < 3
                or any(not value for value in frames)
                or len(set(frames)) != len(frames)
                or len(self.tracking_edges) != len(frames) - 1
                or frames[0] != self.pre_frame_id
                or frames[-1] != self.post_frame_id
            ):
                raise ValueError("连续语音跟踪证据不完整")
            previous_observation_id = self.selected_pre_observation_id
            for index, edge in enumerate(self.tracking_edges):
                if (
                    edge.from_frame_id != frames[index]
                    or edge.to_frame_id != frames[index + 1]
                    or edge.from_observation_id
                    != previous_observation_id
                ):
                    raise ValueError("连续语音跟踪边未首尾相接")
                previous_observation_id = edge.to_observation_id
            if previous_observation_id != mapping.post_observation_id:
                raise ValueError("连续语音跟踪未连接到最终绑定观察")
        elif method == "native_source_id":
            if (
                not confirmed
                or not native_id
                or self.tracking_frame_ids
                or self.tracking_edges
                or self.matched_neighbor_pairs
            ):
                raise ValueError("语音原生身份绑定证据不合法")
        elif method == "neighbor_scroll_alignment":
            deltas = [pair.scroll_delta_y for pair in self.matched_neighbor_pairs]
            if (
                not confirmed
                or native_id
                or self.tracking_frame_ids
                or self.tracking_edges
                or len(deltas) < 2
                or max(deltas) - min(deltas) > 8.0
            ):
                raise ValueError("语音邻居滚动对齐证据不合法")
        elif (
            self.transcript_binding_status != "ambiguous"
            or native_id
            or self.matched_neighbor_pairs
        ):
            raise ValueError("语音无绑定方法只允许歧义终态")
        return self


class WechatMessageEvidence(BaseModel):
    model_config = ConfigDict(extra="allow")

    contract_revision: str = Field(min_length=1, max_length=32)
    contract_sha256: str = Field(min_length=64, max_length=64)
    observation_schema_version: int = Field(ge=1)
    authoritative_frame_source: str = Field(
        min_length=1,
        max_length=32,
        pattern="^(initial_read|final_read|action_journal_recovery)$",
    )
    ui_frame_invalidated: bool = False
    observations: list[dict] = Field(max_length=500)
    authorization_read_reason: str = Field(min_length=1, max_length=64)
    recovery_attempt_kind: str | None = Field(
        default=None,
        pattern="^(checkpoint_merge|stable_reread)$",
    )
    continuation_batch_id: str | None = Field(default=None, max_length=36)
    continuation_token: str | None = Field(default=None, max_length=64)
    finished_at: datetime
    flow_gate_errors: list[str] = Field(default_factory=list, max_length=50)
    flow_gate_details: list[WechatFlowGateDetail] = Field(
        default_factory=list,
        max_length=50,
    )
    slot_ledger_states: list[WechatSlotLedgerState] = Field(max_length=500)
    sequence_alignment_evidence: WechatSequenceAlignmentEvidence | None = None
    ingest_partition: WechatIngestPartition | None = None
    voice_transcription: WechatVoiceActionEvidence | None = None

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
        if (
            self.ui_frame_invalidated
            and self.authoritative_frame_source == "initial_read"
        ):
            raise ValueError(
                "媒体 UI 动作发生后不得继续使用 initial_read"
            )
        recoverable_identity_codes = {
            "MESSAGE_IDENTITY_UNCONFIRMED",
            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
            "C2_MESSAGE_HISTORY_GAP",
        }
        for detail in self.flow_gate_details:
            if detail.error_code not in recoverable_identity_codes:
                continue
            if (
                not detail.gate_scope
                or detail.min_screen_order is None
                or detail.max_screen_order is None
                or not detail.boundary_relation
            ):
                raise ValueError("身份/历史门禁缺少 AI 回复边界范围证据")
        if (
            self.ui_frame_invalidated
            and self.authoritative_frame_source
            == "action_journal_recovery"
        ):
            raise ValueError(
                "ActionJournal 无 UI 恢复不得声明当前画面失效"
            )
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
    unread_generation: int = Field(ge=0)
    messages: list[WechatMessageItem] = Field(
        default_factory=list,
        max_length=C2_MESSAGE_BATCH_MAX_ITEMS,
    )
    evidence: WechatMessageEvidence

    @model_validator(mode="after")
    def validate_settlement_envelope(self):
        evidence = self.evidence.model_dump(mode="json")
        if (
            (self.messages or self.evidence.slot_ledger_states)
            and self.evidence.sequence_alignment_evidence is None
        ):
            raise ValueError("消息事实必须携带统一序列对齐证据")
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
        if (
            self.authorization_scope == "active_read"
            and self.evidence.authoritative_frame_source == "initial_read"
        ):
            current_source_keys = {
                slot.source_message_key
                for slot in self.evidence.slot_ledger_states
                if slot.fact_scope == "current_read_run"
            }
            current_action_phases: set[str] = set()
            for observation in self.evidence.observations:
                if not isinstance(observation, dict):
                    continue
                source = observation.get("source_message")
                source = source if isinstance(source, dict) else {}
                source_key = str(
                    source.get("source_message_key") or ""
                ).strip()
                if source_key not in current_source_keys:
                    continue
                current_action_phases.add(
                    str(observation.get("action_phase") or "")
                    .strip()
                    .lower()
                )
            voice_transcription = evidence.get("voice_transcription")
            voice_transcription = (
                voice_transcription
                if isinstance(voice_transcription, dict)
                else {}
            )
            # The top-level voice transaction describes UI work performed in
            # this active read.  It remains authoritative even when a final
            # refresh has scrolled the operated voice out of the visible slot
            # list.  Do not condition this evidence on a surviving voice row.
            voice_action_phase = str(
                voice_transcription.get("action_phase") or ""
            ).strip().lower()
            if voice_action_phase:
                current_action_phases.add(voice_action_phase)
            if (
                voice_transcription.get("ui_action_performed") is True
            ) or any(
                phase
                not in {
                    "",
                    "not_attempted",
                    "cancelled_before_trigger",
                }
                for phase in current_action_phases
            ):
                raise ValueError(
                    "媒体 UI 动作发生后不得继续使用 initial_read"
                )
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
