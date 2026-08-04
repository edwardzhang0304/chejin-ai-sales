# C2-C3 OmniAuto / Worker / 后端接口合同

版本：v0.1

日期：2026-07-21

状态：接口命名和职责边界冻结。2026-07-31 增补 C1-C3 统一事务恢复与事实结算口径；客户端和后端完成三层整改、真实 Vision/Brain 联调和 Windows 故障注入验收前，不得宣称功能完成。

> 恢复流程以
> `C1-C3_事务恢复与事实结算统一架构_v0.1_2026-07-31.md`
> 为最高约束。旧实现中的图片专用恢复和“收到 `target_terminated` 后本地丢弃事实”
> 不再属于正式合同。
> 当前机器合同 `3.10.0` 是 `86b87f2` 候选实现快照；本轮删除PID硬门禁、删除五个
> 专用失败原因并补三个观察失败映射时，客户端与后端必须连同 schema、共享样例和
> 合同测试一次性升级到下一 revision，不能只改 JSON 枚举，也不得借合同升级重开
> 其他状态机设计。
>
> 图片流程的状态矩阵、Windows 位图、剪贴板、Vision Provider、结果 schema、
> 服务端产品权威、跨轮 Brain 上下文和 UAT 门禁，统一以
> `AI智能客服售前跟进系统_技术方案手册_v0.8.md` 第 8 章为最高架构约束。
> `C2_图片流程封版口径与一次性整改清单_v0.1_2026-07-31.md` 只保留为历史
> 审计材料。
> 客户端实际代码以
> `AI智能客服售前跟进系统_技术方案手册_v0.8.md` 第 8.5 至 8.8 节为最高实施
> 约束：继续复用以 OmniAuto `855c218` 为共同基础、从 `2318bd8` 选择性接入的
> 图片一致性能力，不新增第二套剪贴板接口；撤销
> `claim_copy_ownership/微信窗口PID` 硬门禁，保留现有 sequence、位图、
> 指纹辅助、Vision、终态和清理合同。来源元数据必须同时记录基础提交、选择性
> 来源提交和最终目录 tree SHA，不得把选择性引入伪装成整个目录升级。

## 0. 三层统一与职责边界（最高约束）

整条链路只能有一套正式合同：

1. **OmniAuto 负责“看见、操作、理解和生成”**
   - 负责微信 `sessions / open-chat / messages / voice-transcribe / send`、OCR、图片气泡边界、同行头像角色证据和语音父子绑定。
   - 负责 `customer_image_understanding`、`visual_bridge_input`和 `customer_service_brain/brain_plan`。
   - 不负责车金授权、`conversation_id`、状态机、批次、数据库和最终去重。
2. **Worker 负责“准入、串行编排和合同翻译”**
   - 只根据后端 `read-targets + authorization_revision` 处理会话，维持单会话 UI 锁与语音/图片/Brain/发送串行流程。
   - 负责统一消息槽位、`screen_order`、`source_message_key/dedupe_key`、本地处理清单、Outbox 和车金 V3 映射。
   - 不得自己猜业务状态、生成授权或回复文案，不得维护第二套 Vision/Brain 结果。
3. **车金后端负责“授权、业务状态和最终落库”**
   - 负责短码绑定、private 准入、`read-targets`、`authorization_revision`、`message_batch`、会话状态机、主动开场、召回、handoff、`reply_action`、持久化和数据库最终去重。
   - 负责真实调用 OmniAuto Brain Adapter 并映射 Brain 结果。
   - 不得重新推测微信左右侧、发送方、语音父子关系、图片位置或消息顺序。

禁止三层各做一套：同一字段必须有唯一所有者，其他层只能校验、透传或映射。合同缺字段时必须先更新本文和机器合同，再同时修改三层；不得私自加临时字段、动作或 HTTP 接口。三层合同测试必须共用 `contracts/examples/` 中的同组 JSON 样例。缺少真实模型凭据时必须停止凭据联调，不得用 mock 冒充功能完成。

## 1. 一句话规则

```text
OmniAuto 负责“看见、理解、生成了什么”；
Worker 负责“当前获准处理哪个会话，以及如何把 OmniAuto 结果翻译成车金请求”；
后端负责“授权、持久化、状态机和最终去重”。
```

同一个含义只能有一个正式名称。已有 OmniAuto 名称必须优先复用；车金如需数据库流程状态，只能通过适配表映射，不能要求 OmniAuto 改名，也不能把两个名称混在同一层使用。

## 2. 唯一命名表

| 能力 | 正式名称 | 所属层 | 禁止再造的同义名称 |
|---|---|---|---|
| 首屏会话观察 | `sessions` | OmniAuto RPA | `session-scan`、`first-screen-scan-action` |
| 打开并确认会话 | `open-chat` | OmniAuto RPA | `locate-chat` 仅可作为 Worker 方法名，不是 Sidecar action |
| 当前屏消息观察 | `messages` | OmniAuto RPA | `read-messages-v3`、`capture-chat` |
| 语音转写 | `voice-transcribe` | OmniAuto RPA | `voice_ocr`、`speech-to-text` |
| 微信发送 | `send` | OmniAuto RPA | `send-reply` 仅可作为 Worker 方法名，不是 Sidecar action |
| 图片文字化理解结果对象 | `customer_image_understanding` | OmniAuto Vision | `image_recognition`、`image_intent_result` |
| 图片给 Brain 的桥接输入 | `visual_bridge_input` | OmniAuto Vision/Brain bridge | `vision_context`、`image_prompt_context` |
| 客服大脑 | `customer_service_brain` | OmniAuto Brain | `brain_reply_engine`、`ai_reply_result` |
| Brain 计划 | `brain_plan` | OmniAuto Brain | `ai_decision_payload` |
| Brain 动作 | `brain_plan.recommended_action` | OmniAuto Brain | 不得直接用车金 `batch_status` 表达 |
| 后端消息批次 | `message_batch` / `batch_id` | 车金后端 | 不得改叫 `brain_task` |
| 后端发送事实 | `reply_action` | 车金后端 | 不得改叫 `brain_reply` |

### 2.1 名称来源和当前可用性

| 名称 | 源码依据 | 开发分支状态 |
|---|---|---|
| `sessions/open-chat/messages/voice-transcribe/send` | OmniAuto `wechat_win32_ocr_sidecar.py` 的现有 action | 是 |
| `customer_service_brain/brain_plan/reply_segments` | OmniAuto `customer_service_brain.py`、`customer_service_brain_contract.py`、`layer_contracts.py` | 后端已通过唯一 OmniAuto Brain Adapter 调用；真实 Provider/RAG/Guard 凭据联调待完成 |
| `customer_image_understanding/visual_bridge_input` | 正式 OmniAuto `customer_image_turn_router.py` 和 `BuiltinVisionPlugin` | 已通过 Worker 适配层进程内接入；真实模型凭据和 Windows 验收待完成 |
| `message_batch/batch_id/reply_action/handoff_event` | 车金后端持久化和调度对象 | 已接通；Worker 按 `batch_id` 在原会话和同一 UI 锁内等待、刷新、发送并回执 |

本文复用 OmniAuto 已有能力名。不得为图片另造 Sidecar action 或后端图片接口。

## 3. 三层职责和字段所有权

| 数据 | 唯一所有者 | 其他层的处理规则 |
|---|---|---|
| `row_kind`、`bubble_rect`、`voice_state`、OCR 原文、会话标题证据 | OmniAuto | Worker 只能校验和映射，后端不得重新猜界面结构 |
| `sender_role`、`sender_role_source` | OmniAuto C2 统一角色识别 | 图片、文字、语音必须共用同行头像规则；Vision 不得另判左右归属 |
| `observation_schema_version` | OmniAuto | 当前为 `3` |
| `contract_version/revision/sha256` | Worker 读取车金 `contracts/c2_contract_v3.json` 生成 | OmniAuto 不依赖车金合同，也不负责生成车金合同指纹 |
| `source_message_key`、`dedupe_key`、`message_position` | Worker | 必须由最终当前屏观察统一组装；坐标和授权版本不得进入消息身份 |
| `conversation_id`、`authorization_revision`、`read_reason` | 后端 | Worker 只能使用 `read-targets` 下发值，不得生成或修补 |
| `message_batch`、会话状态、唯一约束、最终入库结果 | 后端 | Worker 和 OmniAuto 不直接修改业务状态 |
| `customer_image_understanding`、`visual_bridge_input` | OmniAuto Vision | 只包含通过共享 schema 的文字化结果和非图片内容审计，不生成客户回复；车金严格入口不使用客户端本地产品库确认正式 product_id |
| `server_validated_product_id`、历史图片紧凑上下文 | 车金后端 | 使用服务端权威车源/RAG确认；Worker 和 OmniAuto 只能提供视觉线索 |
| `brain_plan`、`reply_text` | OmniAuto Brain | Guard 可以阻断或要求 Brain 重写，但不能自行写客户可见话术 |

## 4. 三种返回外壳不得混用

### 4.1 OmniAuto Sidecar 返回

```json
{
  "ok": true,
  "state": "messages_ocr",
  "sidecar_run_id": "message-...",
  "error_code": null,
  "error": null
}
```

- `ok` 表示本次 RPA action 是否成功完成。
- `state` 是 OmniAuto 运行状态。
- `error_code/error` 是 RPA 失败信息。
- 不得返回后端的 `code/message/data/trace_id` 外壳。

### 4.2 车金 HTTP API 返回

```json
{
  "code": "OK",
  "message": "success",
  "data": {},
  "trace_id": "..."
}
```

- 业务数据只放 `data`。
- HTTP 失败仍使用同一外壳，`code` 为稳定错误码。
- 不得用 `ok/state` 代替后端 API 外壳。

### 4.3 OmniAuto Brain/Vision 能力返回

```json
{
  "enabled": true,
  "applied": true,
  "adoptable": true,
  "reason": "..."
}
```

- `enabled` 表示能力是否启用。
- `applied` 表示本轮是否真实执行。
- `adoptable` 表示结果是否允许主流程采用。
- `reason` 是能力层原因，不是 HTTP 错误码或数据库状态。

### 4.4 不可逆 RPA 动作证据

语音转写点击、图片复制和消息发送等可能改变微信状态的动作，Sidecar 必须在原有返回外壳中增加：

```json
{
  "ok": false,
  "state": "send_result_unknown",
  "action_phase": "trigger_attempted",
  "error_code": "SEND_RESULT_UNKNOWN",
  "evidence": {}
}
```

`action_phase` 只允许：

```text
not_attempted / trigger_attempted / confirmed
```

规则：

- `ok` 只表示 Sidecar action 是否完整结束，不能证明物理动作是否发生。
- `action_phase=not_attempted` 表示没有触发不可逆动作，Worker 才能按明确失败或安全重试处理。
- `action_phase=trigger_attempted` 表示动作可能已经发生。发送不得映射为普通 `failed`；语音/图片不得自动重复点击、复制或调用模型。
- `action_phase=confirmed` 必须附带对应动作的确认凭证。
- OmniAuto 不生成车金业务状态；Worker 必须通过唯一的动作结果判定器映射，后端只校验和持久化。
- Worker 必须先创建持久化 `ActionJournal(not_attempted)`。Sidecar 在发送好友
  申请、语音转写、图片复制或发送消息的物理点击之前，将同一条记录原子推进为
  `trigger_attempted`；落盘失败时禁止点击。取得可靠结果证据后再推进为
  `confirmed`。
- `ActionJournal` 只有在 Worker 已将对应终态写入本地 ledger/可靠回执后才能
  删除。进程重启和 Sidecar 异常收尾必须先恢复日志，再允许新的物理动作。
- 语音和图片 ActionJournal 必须在物理动作前保存完整的文字化
  `replayable_observation`，至少包含消息身份、角色证据、稳定 anchor、观察时间和
  顺序；图片原始位图仍禁止持久化。只保存坐标的语音日志不满足跨重启恢复合同。

## 5. OmniAuto RPA Sidecar 合同

Sidecar 使用现有 daemon/CLI action 名称。Worker 可以有 Python 包装方法，但传给 Sidecar 的 `action` 不得改名。

### 5.1 `sessions`

请求示例：

```json
{
  "action": "sessions",
  "sidecar_run_id": "session-...",
  "artifact_dir": "..."
}
```

`sidecar_run_id` 对 OmniAuto 是兼容式可选字段；Worker 必须保证上报 `scan-result` 前已有稳定运行 ID，可以沿用 Sidecar 返回值，也可以在缺失时本地补齐。不得要求上游 OmniAuto 因车金后端必填而改变既有 `sessions` 调用默认值。

成功响应最小字段：

```json
{
  "ok": true,
  "state": "sessions_ocr",
  "sidecar_run_id": "session-...",
  "sessions": [
    {
      "name": "客户名-CJ123456",
      "session_key": "...",
      "row_fingerprint": {},
      "content": "最后一条预览",
      "unread_signal": true,
      "conversation_type": "private",
      "ocr_confidence": 0.93
    }
  ]
}
```

说明：`sessions` 是第一屏事实观察，不代表后端允许点击。Worker 必须先上报 `scan-result`，再以 `read-targets` 授权结果决定是否打开会话。

### 5.2 `open-chat`

请求最小字段：

```json
{
  "action": "open-chat",
  "sidecar_run_id": "locate-...",
  "target": "客户名-CJ123456",
  "session_key": "...",
  "remark_code": "CJ123456",
  "target_mode": "visible"
}
```

`target_mode` 只允许：

- `visible`：使用本轮首屏候选打开。
- `search_by_remark_code`：首屏未命中时按短码搜索。

成功条件不是“点击发生”，而是顶部标题证据同时确认：

```text
当前标题包含目标短码
AND conversation_type=private
AND 不是 group/unknown
```

### 5.3 `messages`

车金当前屏读取固定参数：

```json
{
  "action": "messages",
  "sidecar_run_id": "message-...",
  "target": "客户名-CJ123456",
  "session_key": "...",
  "conversation_type": "private",
  "history_load_times": 0,
  "max_scroll_steps": 0,
  "max_snapshots": 1
}
```

成功响应的权威数据：

```json
{
  "ok": true,
  "state": "messages_ocr",
  "observation_schema_version": 3,
  "observations": [
    {
      "schema_version": 3,
      "observation_id": "...",
      "row_kind": "text_bubble",
      "sender_role": "customer",
      "sender_role_source": "same_row_avatar",
      "message_type": "text",
      "voice_state": "not_voice",
      "content_clean": "你好",
      "bubble_rect": {},
      "source_message": {}
    }
  ]
}
```

规则：

- `observations` 是 Worker 组装 V3 消息的唯一权威输入；旧 `messages[]` 只作兼容和诊断。
- 观察顺序为当前画面自上而下；Worker 在语音全部处理完成后的最终帧建立统一 `screen_order`。
- 普通 `voice_bubble` 只是待处理观察，不能直接入库。只有真实执行转写后明确失败、且保留稳定语音锚点、同行头像角色和最终画面位置的语音，才允许以 `item_state=failed + content=null + voice_processing_reason` 形成失败语音事实。
- `voice_transcript` 必须带 `parent_voice_anchor_key`，最终只形成一条 `message_type=voice`，不得再形成一条 text。
- `image_bubble` 发现态只观察、不入库。初次同行头像角色不可信时尚未形成业务图片身份，必须形成帧级 `MESSAGE_IDENTITY_UNCONFIRMED`，零点击、零 Vision、零 terminal ledger；不得用 `ignored` 静默结案。只有角色可信且稳定身份唯一的 `NEW_IMAGE` 才进入第 9 节，并在同一 Flow 形成 `completed/failed`。`discovered/ignored` 不入库。

### 5.4 `voice-transcribe`

请求沿用 OmniAuto 名称：

```json
{
  "action": "voice-transcribe",
  "sidecar_run_id": "voice-...",
  "target": "客户名-CJ123456",
  "session_key": "...",
  "conversation_type": "private",
  "max_duration_seconds": 240
}
```

允许的 flow 结果：

- `voice_transcribe_completed`
- `voice_transcribe_partial`
- `voice_transcribe_no_visible_voice`
- 明确失败状态及 `error_code`

约束：

- `max_duration_seconds` 是无进展 watchdog，不是正常流程总时限。
- 正常有进展时继续处理当前屏全部语音；硬安全上限只防程序永久失控。
- 每次物理点击后旧坐标立即失效。
- 最后一条语音完成后的新截图可作为最终消息帧；没有新的 UI 变化时不得再重复截一张相同画面。
- `partial` 只是本轮 flow 事实，不创建每条语音的后端 pending 任务。成功语音照常形成 completed voice；失败语音形成 failed voice，并按最终画面角色和顺序阻断 Brain 或确认销售人工介入。
- Sidecar 必须逐条返回稳定语音 anchor、`action_phase`、item 结果和错误证据。Worker 按 `source_message_key` 做集合并集；不得用后一次调用结果替换前面已经 completed 或 failed 的集合。
- 已经 `trigger_attempted` 但最终无法确认转写结果时，该语音形成 `item_state=failed + error_code=VOICE_TRANSCRIBE_RESULT_UNKNOWN`，本轮不自动再次右键。

### 5.5 `send`

Worker 只有拿到后端已批准的 `reply_action` 并完成 `claim-send` 后，才允许调用：

```json
{
  "action": "send",
  "target": "客户名-CJ123456",
  "session_key": "...",
  "conversation_type": "private",
  "text": "后端批准的原文"
}
```

OmniAuto 不得自行改写 `text`，Worker 不得使用本地模板替代 Brain 结果。

发送结果映射是强合同：

| Sidecar证据 | Worker `sent-ack.send_result` | 处理 |
|---|---|---|
| `action_phase=confirmed` 且新增右侧气泡身份确认 | `sent` | 保存气泡凭证，幂等回执。 |
| `action_phase=not_attempted` 且明确未触发发送 | `failed` | 可以安全结束本次 action，不得谎报已发送。 |
| `action_phase=trigger_attempted` 但无法确认新增气泡 | `unknown` | 正式终态；禁止自动补发，不建立人工确认发送结果流程，会话转销售正常接管。 |

Sidecar 返回空白截图、OCR 异常、确认截图失败或进程在触发后中断，都不能把 `trigger_attempted` 降级成普通 `failed`。

## 6. Worker 与后端 C2 HTTP 合同

所有 Worker API 必须携带：

```http
X-Worker-Token: ...
X-Client-Instance-Id: ...
X-Request-Id: ...     # 可选
```

### 6.1 `POST /api/workers/{worker_id}/wechat/sessions/scan-result`

请求沿用现有字段：

```json
{
  "scan_id": "scan-...",
  "sidecar_run_id": "session-...",
  "started_at": "2026-07-21T10:00:00+08:00",
  "finished_at": "2026-07-21T10:00:03+08:00",
  "sessions": [
    {
      "rpa_session_key": "...",
      "display_name": "客户名-CJ123456",
      "remark_code_candidates": ["CJ123456"],
      "row_fingerprint": "...",
      "unread_hint": true,
      "last_message_preview": "你好",
      "ocr_confidence": 0.93
    }
  ],
  "evidence": {},
  "scan_failed": false,
  "error_code": null
}
```

后端返回 `bindings[]`；只有 `can_ingest_messages=true` 仍不能直接点击，Worker 还必须用当前轮 `read-targets` 做最终授权交集。

### 6.2 `GET /api/workers/{worker_id}/wechat/sessions/read-targets?limit=20`

每个正常目标必须包含：

```json
{
  "conversation_id": "...",
  "lead_id": "...",
  "sales_id": "...",
  "remark_code": "CJ123456",
  "rpa_session_key": "...",
  "display_name": "客户名-CJ123456",
  "last_ingested_at": "...",
  "read_reason": "waiting_user_reply",
  "authorization_revision": "...",
  "identity_transition": {
    "version": 1,
    "source_version": "v16.104",
    "legacy_messages": [
      {
        "dedupe_key": "...",
        "source_message_key": "...",
        "message_type": "text",
        "sender_role": "customer"
      }
    ]
  }
}
```

`identity_transition` 对所有 `read-targets` 固定返回。`version=1 + legacy_messages=[]` 明确表示后端已经支持迁移且该会话没有旧身份消息，不能省略该字段。Worker 对已匹配的历史消息必须继续使用原 `dedupe_key`，对新消息只生成正式 Worker sequence 身份；只有处理过带版本的后端过渡对象后才能写入 `legacy_transition_completed=true`。本地提前生成了 identity state 不能跳过迁移；过渡完成后旧算法不得参与日常身份生成。

若历史与当前画面无法形成唯一对应，Worker 必须阻断消息入库、Vision 和 Brain，不得猜测。Worker 同时通过现有 `messages/ingest` 提交空消息安全门禁：

```json
{
  "messages": [],
  "evidence": {
    "authorization_read_reason": "waiting_user_reply",
    "flow_gate_errors": ["MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"],
    "flow_gate_details": [
      {
        "error_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
        "position_source": "identity_error_visual_top",
        "min_screen_order": 3,
        "max_screen_order": 4
      }
    ],
    "flow_gate_identity_key": "由 conversation_id、错误码和规范化 identity_errors 生成的稳定 SHA-256",
    "identity_errors": []
  }
}
```

`flow_gate_identity_key` 由 Worker 唯一生成，禁止使用 `scan_id`、`read_run_id`、绝对坐标、授权版本或时间桶。Worker 用该键复用同一条本地 Outbox 记录；后端只校验和使用该键创建幂等人工接管。同一个歧义跨轮重试只能产生一条待重传 Outbox、一个批次和一个 `handoff_event`。

每个 `flow_gate_errors[]` 必须有且只有一组同码
`flow_gate_details[]`。能够定位的门禁必须携带最终画面的
`min_screen_order/max_screen_order`；无法定位时必须明确写
`position_source=position_unavailable`，不得猜位置。只有门禁的
`max_screen_order` 明确小于后续人工销售消息的 `screen_order`，后端才允许
认为该门禁已经被人工回复覆盖。失败销售语音本身是已持久化的人工销售事实，
因此 `subject_sender_role=self` 时允许其在相同 `screen_order` 关闭自己的语音失败
门禁；如果其下方还有新客户消息，门禁继续保留并禁止 Brain。

这里的“能够定位”只接受最终画面的强证据来源：
`slot_ledger_visual_top`、`identity_error_visual_top`、
`failed_image_visual_top` 或 `failed_voice_visual_top`。
`observation_index_fallback` 只能用于普通消息
排序，不能用于关闭人工接管、清除安全门禁或推动 Brain。

`C2_IMAGE_PROCESSING_DEFERRED` 已从目标合同废弃。新 Worker 不得提交该错误码，
后端新合同不得用它实现“文字先入库、图片以后补”的跨轮恢复。

图片动作前页面变化时必须先重建完整 `final_read`：

```text
重建后图片已出屏 -> 本轮不建立该图片槽位，不门禁、不上滚
重建后图片仍可见且身份唯一 -> 继续复制和 Vision
重建后图片仍可见但身份不唯一 -> item_state=failed
```

Vision 配置必须在 Worker 开始“新的 C2 UI 流程”前完成预检。配置缺失属于客户端
`vision_not_ready`，不是单张图片状态，也不得复用后端请求级
`capability_paused`。全局事务恢复必须排在该预检之前：已有 `sent_ack`、消息
Outbox 和 `settle_without_ui` 事实结算不需要 Vision，不能被一起阻断。

图片归属的唯一业务结论来自 C2 同行头像规则。复制前重新定位得到的
`visual_side` 只能记录为物理一致性证据，不得否决或覆盖已经确认的
`customer/self`。图片阶段必须集中返回本轮真实动作数、终态集合和最终刷新
结果；主流程不得再用
`completed + failed > cached` 等计数公式重新猜测阶段结果。

复制前重新截图后，必须再次使用同一套 C2 同行头像规则计算
`refreshed_sender_role`。只有
`initial_sender_role == refreshed_sender_role` 且两者均为
`customer/self` 时才允许右键；刷新后为另一角色或 `unknown` 时返回
`C2_IMAGE_SLOT_RECONFIRM_FAILED + action_phase=not_attempted`。这项比较是
两次 C2 正式结论的一致性校验，不是使用 `visual_side` 重新判断角色。

图片槽位已完成上述确认后，如右键菜单未准备好、未识别到“复制”或菜单项无法安全点击，正式错误必须为 `C2_IMAGE_MENU_OPERATION_FAILED`，不得伪装成槽位复核失败。复制后的 sequence、位图或指纹一致性失败继续使用 `C2_IMAGE_CLIPBOARD_TRANSACTION_FAILED`。

图片观察入口必须区分：

```text
检测器正常完成且 image_count=0 -> 当前画面确实没有图片，正常继续
检测器或物理锚点生成异常 -> C2_IMAGE_OBSERVATION_FAILED，阻断本批 Brain
```

图片观察异常不得返回空数组伪装成“没有图片”，也不得让同屏文字单独触发
Brain。

目标实现不再保留“未收口图片身份”作为跨轮业务状态。图片阶段只返回本 Flow
动作证据和 `completed/failed` 终态；初次角色不可信属于帧级身份门禁，不是图片
终态。`visual_side` 不得参与角色定案、
跨轮身份或相同图片 occurrence 分组。

准入条件固定为：

```text
有效短码
AND conversation_type=private
AND 当前 read-target 存在
AND authorization_revision 未变化
```

`read-targets=[]` 时允许 `sessions` 被动扫描并上报事实，但不得 `open-chat/messages/voice-transcribe/send`。

### 6.2.1 `GET /api/workers/{worker_id}/wechat/conversations/{conversation_id}/read-authorization`

长动作每秒取消检查和“已触发语音/图片动作但事实尚未结算”的原会话恢复，统一
使用本轻量接口，不得反复下载完整 `read-targets/identity_transition`。普通长动作
响应包含：

```json
{
  "allowed": true,
  "conversation_id": "...",
  "authorization_revision": "...",
  "read_reason": "waiting_user_reply",
  "run_status": "listening"
}
```

Worker 必须同时匹配 `conversation_id + authorization_revision + read_reason`。
该接口不负责普通目标发现，不能替代开始动作前的完整 `read-targets` 准入。
唯一例外是恢复已经持久化的语音/图片事务：后端必须返回统一三态结果，避免
`read-targets` 的数量限制或状态变化让整台 Worker 永久停住。

恢复请求必须携带或引用：

```text
?recovery_transaction_id=...
&action_kind=voice|image
&source_message_key_digest=...
&original_authorization_revision=...
```

```json
{
  "allowed": false,
  "recovery_decision": "settle_without_ui",
  "settlement_mode": "fact_only",
  "settlement_token": "***",
  "conversation_id": "...",
  "trace_id": "..."
}
```

恢复决定只有：

| `recovery_decision` | Worker 行为 |
|---|---|
| `resume_current_target` | 只恢复响应中 `target` 指向的原会话；仍须重新确认有效短码、private 和当前授权。 |
| `settle_without_ui` | 不打开微信，使用现有 `messages/ingest` 结算原事实或技术终态；后端逐条确认前不得清理本地记录。 |
| `retry_later` | 保留原 Ledger 和动作日志，阻断新 UI 动作，稍后重试。 |

`settlement_mode` 只允许：

| `settlement_mode` | 后端行为 |
|---|---|
| `fact_only` | 原身份仍可证明；幂等保存消息事实，固定 `state_transition_applied=false`、`message_batch=null`。 |
| `technical_terminal` | 原身份无法安全映射；持久化恢复终结审计和精确 source key 结果，不写入错误会话。 |

缺失或未知决定必须按 `retry_later` 失败关闭。语音/图片动作日志中所有条目均明确
为 `not_attempted` 时，Worker 在全局门禁前本地清理，不需要请求后端。
`unbound / binding_failed / needs_review / degraded / paused` 不得返回永久终结。
后端仍能证明原 Worker、原绑定、原 conversation 和原短码身份时，返回
`settle_without_ui + fact_only`，但不改变当前绑定状态；身份暂不可证时才
`retry_later`。会话关闭、拒绝、可靠确认短码移除或绑定禁用时，已经产生的事实
仍返回 `settle_without_ui`，不能要求 Worker 本地改成 `not_required`。

`settlement_token` 使用请求头 `X-C2-Settlement-Token`，建议有效期 5 分钟，并
绑定 Worker、conversation、`recovery_transaction_id` 和 source key 摘要。同一
事务在有效期内允许幂等重试，不能因第一次响应丢失而失效。服务端必须以
`unique(worker_id, recovery_transaction_id)` 持久化结算结果；技术终态使用通用
recovery settlement 记录，不得创建 handoff 冒充恢复成功。

`source_message_key_digest` 固定为
`sha256(UTF-8(sorted(unique(source_message_key)).join("\n"))).hexdigest()`。
缺少 `dedupe_key` 时只允许调用 Worker 现有唯一消息身份组装器从
`replayable_observation` 补齐，不得在恢复模块新增哈希算法。

当消息入库已经创建 Brain 批次、会话进入 `ai_active` 后，全局读取授权
必须保持 `allowed=false`。后端只给当前 `batch_id` 签发一张批次续行票：

```http
GET /api/workers/{worker_id}/wechat/conversations/{conversation_id}/read-authorization?continuation_batch_id={batch_id}
X-C2-Continuation-Token: {token}
```

```json
{
  "allowed": true,
  "authorization_scope": "batch_continuation",
  "batch_id": "...",
  "continuation_token": "...",
  "conversation_id": "...",
  "authorization_revision": "...",
  "read_reason": "waiting_sales_reply"
}
```

续行票只允许同一 Worker、同一会话、同一批次、同一授权版本和原始
`read_reason` 继续完成最终刷新、等待 Brain 和发送。停止监听、授权版本
变化、批次被替换、回复任务终结或票据不匹配时必须立即失效。token 只放
请求头和结构化 Outbox，不放 URL、普通日志或截图。

### 6.2.2 `POST /api/workers/{worker_id}/wechat/conversations/{conversation_id}/activation-confirm`

仅用于 `read_reason=friend_acceptance_visible_hit`。Worker 打开会话并获得“有效短码 + private + 输入区可用”的标题证据后，必须先调用本接口；后端确认 `friend_request_sent -> friend_active` 并返回新的 `authorization_revision` 后，Worker 才能读取消息、转写语音或处理图片。

新好友定位时不得提前复用或解析消息截图；激活确认成功后再执行一次 `messages`。普通已激活会话可以复用 `open-chat` 的会话确认帧作为初始消息帧，避免机械重复截图。

### 6.3 `POST /api/workers/{worker_id}/wechat/messages/ingest`

V3 请求顶层字段：

```json
{
  "contract_version": 3,
  "contract_revision": "3.4.8",
  "contract_sha256": "1805645ca709d6cd38c54bd637a808e7536608e635ca1cdba07ba3c311c36de2",
  "observation_schema_version": 3,
  "authorization_scope": "active_read",
  "read_run_id": "read-...",
  "conversation_id": "...",
  "remark_code": "CJ123456",
  "rpa_session_key": "...",
  "authorization_revision": "...",
  "messages": [],
  "evidence": {
    "sidecar_run_id": "message-...",
    "authorization_read_reason": "waiting_user_reply",
    "continuation_batch_id": null,
    "continuation_token": null,
    "finished_at": "2026-07-24T10:00:00+08:00",
    "flow_gate_errors": [],
    "flow_gate_details": [],
    "slot_ledger_states": [],
    "observations": []
  }
}
```

硬规则：

- `authorization_scope` 只允许 `active_read / fact_settlement`。
  `active_read` 使用当前 `authorization_revision`；`fact_settlement` 必须在请求头
  携带恢复授权返回的短时 `settlement_token`，后端固定
  `state_transition_applied=false`、`message_batch=null`。
- 顶层不再发送 `sidecar_run_id`；它只放在 `evidence.sidecar_run_id`，避免同一字段双份。
- `contract_*` 由 Worker 生成。Sidecar 即使兼容性输出同名字段，也只可作诊断，不是权威来源。
- V3 下 `contract_revision/contract_sha256/observation_schema_version/remark_code/authorization_revision` 和每条 canonical message 必填字段必须在 Pydantic schema 与 service 校验中一致，不再一层 optional、一层 required。
- 后端不得根据 `raw_payload` 重算 `sender_role/message_type/screen_order`；只做合同校验和拒绝。
- 后端必须校验 `screen_order` 为正整数且批内唯一，并按
  `message_position.screen_order ASC` 处理事实和状态机；JSON 数组顺序不是
  业务顺序。画面中未入库的图片占位等可以造成序号不连续，因此不要求
  `screen_order` 连号。
- `evidence.authorization_read_reason` 固定记录 Worker 取得本轮授权时的
  `read_reason`。Outbox 延迟重传时，后端仍可补录合法消息事实；如果当前
  授权原因已经变化，则必须返回 `state_transition_applied=false`，不得
  倒退状态、创建 Brain 批次或创建旧门禁 handoff。
- `ai_active` 下只有同时携带后端签发的
  `continuation_batch_id + continuation_token` 才能推动当前批次。无票、
  错票或旧批次票的 Outbox 只能幂等补录消息事实，不能创建或替换 Brain
  批次。
- `evidence.finished_at` 是最终权威画面的观察时间。后端持久化
  `observed_at + observation_order`，Brain 历史不得用网络到达时间重排
  延迟事实。
- `evidence.slot_ledger_states` 是 Worker 从最终权威画面生成的完整槽位账本。
  它必须同时保留本轮新消息和仍在画面中的旧消息，供后端证明消息的上下
  顺序；每个槽位必须携带 `order_source`，且 `source_message_key` 和
  `screen_order` 在同一画面内都必须唯一。
  旧消息可以不再出现在 `messages[]`，但不得从该账本中删除。后端只能
  校验和使用这份顺序证据，不能自行用坐标、扫描时间或正文重算消息身份。
  `screen_order` 只证明顺序，永远不能进入 `source_message_key` 或
  `dedupe_key`。
- `fact_settlement` 是恢复特例：`authoritative_frame_source` 固定为
  `action_journal_recovery`，`slot_ledger_states` 只列本次结算的精确 source keys，
  不伪装成完整当前屏；后端不得据此做上下文完整性、销售顺序或 Brain 判断。
- `order_source=visual_top` 表示最终画面中所有相关气泡都有真实上下边界，
  可用于证明销售消息位于触发消息之后。`observation_index_fallback` 只
  允许恢复普通消息排列，不能解除 handoff、清除安全门禁或推动任何依赖
  “上面/下面”的业务状态。
- 三层统一从 `c2_contract_v3.json.message_limits` 读取限制：正文最多
  `20000` 字符、单条 `raw_payload` 最多 `262144` 字节、每批最多
  `200` 条、请求最多 `2097152` 字节、拆批目标 `1572864` 字节。
- Worker 必须先把原始完整 payload 写入本地 Outbox，再做白名单压缩、
  大小校验或拆批；任何准备失败都保留原 Outbox 并阻断新 UI 动作。
  后端只接收准备完成且满足限制的请求。

每条 `messages[]`：

```json
{
  "source_message_key": "...",
  "dedupe_key": "...",
  "sender_role_hint": "customer",
  "message_type": "text",
  "content": "你好",
  "occurred_at": null,
  "ocr_confidence": 0.93,
  "item_state": "completed",
  "flow_state": "completed",
  "message_position": {
    "screen_order": 1,
    "visual_top": 310,
    "visual_bottom": 372,
    "frame_source": "final_read",
    "order_source": "visual_top"
  },
  "raw_payload": {}
}
```

响应继续返回逐条 `ingested/duplicated/ignored`。每条结果必须原样返回 Worker 生成的 `source_message_key`，Worker 只能用该精确 key 确认本地已处理清单。后端唯一约束仍是最终防线，Worker 本地缓存只是减少重复 RPA/Vision 和重复请求。

失败响应必须在 `data` 中提供正式恢复动作：

```json
{
  "code": "AUTHORIZATION_REVISION_STALE",
  "message": "读取授权已变化",
  "data": {
    "recovery_action": "refresh_and_rebuild"
  },
  "trace_id": "..."
}
```

`recovery_action` 只允许：

| 值 | Worker行为 |
|---|---|
| `retry` | 按有上限的指数退避间隔原样重传同一 Outbox JSON；不按固定次数放弃，不重新操作微信或调用 Vision。 |
| `refresh_and_rebuild` | 重新取得授权，只重建授权/续行外壳；保留原消息身份、内容、观察时间和证据。 |
| `rebuild_failed_facts` | 后端必须返回具体 `source_message_key`；Worker 只把该条无效语音或图片重建为合法 failed 事实，其他成功消息保持不变。 |
| `split_and_retry` | 请求过大时按原 `read_run_id` 和 `screen_order` 拆批；全部分片落盘后再发送，最后一片确认前不得启动 Brain。 |
| `capability_paused` | 仅用于合同版本、SHA 或 schema 暂不兼容；冻结原 Outbox 和 ledger，停止新 UI 动作并自动探测恢复。 |
| `settle_without_ui` | 当前 UI 授权不再有效，但原事实可结算；Worker 取得 settlement token，只重建授权外壳并原样提交事实，不打开微信、不重做媒体动作。 |
| `conversation_terminated` | 后端先持久化技术终态并停止该会话自动处理，再确认 Worker 终结 Outbox。 |

兼容字段 `retryable` 只能用于旧客户端展示，不能作为新 Worker 的唯一决策依据。后端不得要求 Worker 解析 `message` 文本或根据 HTTP 状态码猜测恢复方式。未知/缺失 `recovery_action` 时，新 Worker 必须保守进入 `capability_paused`，不得丢弃事实或建立人工消息处理队列。

旧客户端可能识别的 `target_terminated` 只保留为版本兼容输入，服务端不得再对新
合同返回。兼容层必须把它转换为 `settle_without_ui`，并且在事实或
`technical_terminal` 已持久化前不得返回 `terminal_confirmed=true`。

`VALIDATION_ERROR` 发生在请求解析阶段，后端尚未保存消息事实，因此只能返回
`capability_paused`，且不得返回 `terminal_confirmed=true`。只有后端已经持久化目标或
会话技术终态时，Worker 才能结束 Outbox 并把 ledger 标记为不再上报。

任务线程与 C2 线程必须共用同一把 `c2_outbox_lock`。创建、首次提交、后台重传和状态迁移均在该单飞区间内执行；同一 Outbox 同一时刻最多允许一个 HTTP 请求。

本地保留期清理必须覆盖全部已由后端确认的非重试终态：
`confirmed / split_completed / fact_settled /
conversation_terminated`。`waiting / retry_waiting / refresh_pending /
rebuild_pending / split_pending / capability_paused` 不得按终态清理。

消息级失败不能使用请求级拒绝代替。有效会话中的语音/图片处理失败必须以
`item_state=failed + error_code` 通过同一 `messages/ingest` 入库；后端返回普通
`ingested`，并阻断该批次 Brain。附加截图、诊断或非核心证据异常只返回 warning，
不得拒绝已经可信的消息核心事实。

Worker 对逐条结果采用单调合并：

```text
completed集合只增不减；
failed集合只增不减；
后续调用不得用空数组或新数组覆盖本轮累计结果；
partial只表示completed与failed同时存在，不是持久化单条状态。
```

## 7. C2-C3 单会话串行扩展合同

本节在开发分支中复用 `messages/ingest` 和按 `batch_id` 查询的串行链路，不另造“上报并问 Brain”接口。

当本轮出现可触发 Brain 的新客户消息时，`messages/ingest` 的 `data` 增加一个可选对象：

```json
{
  "ingested_count": 2,
  "duplicated_count": 1,
  "ignored_count": 0,
  "results": [],
  "message_batch": {
    "batch_id": "...",
    "batch_status": "collecting",
    "continuation": {
      "batch_id": "...",
      "token": "...",
      "authorization_revision": "...",
      "read_reason": "waiting_sales_reply"
    }
  }
}
```

旧 Worker 忽略新增的 `message_batch` 仍可工作。新 Worker 拿到 `batch_id` 后保持当前会话和 UI 锁，不处理其他会话。

新增且只新增一个 Worker 查询接口：

```http
GET /api/workers/{worker_id}/wechat/message-batches/{batch_id}
```

处理中：

```json
{
  "batch_id": "...",
  "batch_status": "generating",
  "decision": null,
  "error_code": null
}
```

可终止等待的结果：

| `batch_status` | `decision` | Worker 动作 |
|---|---|---|
| `reply_action_created` | `send_reply` | 读取返回的 `reply_action_id/task_id`，调用既有 `claim-send`，再调用 OmniAuto `send` |
| `handoff_created` | `handoff` | 不发送，释放 UI 锁 |
| `no_action` | `no_action` | 这是正常业务决定，不发送，释放 UI 锁 |
| `paused` | `pause` | 关闭本会话自动回复，创建人工接管事实，不发送并释放 UI 锁 |
| `failed` | `retry_later` | 这是技术失败，不发送；保留错误与未回复事实，释放 UI 锁 |

禁止行为：

- Brain 技术失败不得伪装成 `no_action`。
- `no_action` 不得伪装成 `failed`。
- `no_action` 必须把会话从 `ai_active` 恢复到进入该批次前的可监听业务
  状态，不能让会话从 `read-targets` 永久消失。
- 召回进入 `recall_precheck` 前必须保存 `recall_origin_status`；召回批次
  `no_action` 时原样恢复该状态，不能一律改成“销售人工回复后等待”。
- `pause` 必须进入 `waiting_sales_reply`、创建 `handoff_event` 并关闭该
  会话自动回复；不得只把批次标记失败后继续停在 `ai_active`。
- `handoff_event.closed_at IS NULL` 是“人工接管仍在进行”的唯一门禁。
  门禁期间客户新消息必须正常入库并返回成功，但会话保持
  `waiting_sales_reply`，不得创建新 Brain 批次。
- 只有新识别到且来源确认为 `self + human` 的销售消息可以关闭早于该消息
  的人工接管事件。关闭时写入 `status=sales_replied`、`closed_at` 和销售
  消息证据；AI 自发消息、延迟到达且早于 handoff 的旧销售消息均不能关闭。
- 如果被关闭的是 `pause` 产生的人工接管事件，销售实际回复同时恢复
  `ai_enabled=true`。之后新的客户消息才重新允许进入 Brain。
- 等待期间不得切换到其他微信会话。
- 不设置“正常 Brain 思考超过 N 秒就自动放弃”的业务总时限；仅允许网络重连、进程存活检查、人工停止和明确技术失败。
- Worker 停止监听时可以取消本地等待，但不得把未知结果当成发送失败或发送成功。
- `chat_reply` 虽进入统一任务中心，但 `POST /tasks/{task_id}/claim` 必须携带 `claim_source=c2_conversation_flow` 和对应 `conversation_id`；后端强制校验。普通任务线程不得领取该任务，当前 C2 会话流领取后才能调用 `claim-send`。
- Worker 重启恢复 `chat_reply` 时必须先查询原 `batch_id` 并取得有效批次
  续行票，不得依赖全局 `read-targets`。任务拉取顺序固定为：已运行任务、
  待恢复 `chat_reply`、普通加好友任务。
- `ai_enabled` 是关闭会话自动回复的硬开关。普通 handoff、销售人工回复、
  进入 `waiting_sales_reply` 或 `sales_replied_waiting_user` 只用会话状态
  阻断；只有 Brain 明确返回 `pause` 时，允许同时把 `ai_enabled` 改为
  `false`；销售实际回复并关闭该 pause handoff 时恢复为 `true`。
- 销售人工回复后进入 `sales_replied_waiting_user`；客户再次回复时回到 `ai_active`。客户长期未回复时仍可由 `customer_service_brain` 生成召回。

发送仍复用既有接口：

```text
POST /api/reply-actions/{reply_action_id}/claim-send
POST /api/reply-actions/{reply_action_id}/sent-ack
```

新合同下 `sent-ack` 请求必须显式携带动作阶段：

```json
{
  "send_token": "...",
  "task_id": "...",
  "worker_id": "...",
  "send_result": "unknown",
  "action_phase": "trigger_attempted",
  "evidence": {
    "physical_send_triggered": true
  },
  "error_code": "SEND_RESULT_UNKNOWN"
}
```

`sent-ack.send_result` 只允许 `sent / failed / unknown`。其中 `failed` 只允许用于 `action_phase=not_attempted` 或具有“发送明确未触发”的证据；`trigger_attempted` 后无法确认必须使用 `unknown`。后端收到 `unknown` 或发现 `sending` 超时，必须持久化 `unknown_send_result`，禁止自动补发。`sent / failed / unknown` 都是可由后端确认的正式终态；截图或辅助证据异常只记 warning，不能拒绝核心回执。网络失败时 Worker 只按退避周期重传同一回执，不设置“超次 abandoned”，后端确认任一正式终态后自动解除全局门禁。

## 8. OmniAuto Brain 合同与车金映射

后端真实 Adapter 必须调用/包装 OmniAuto `customer_service_brain`，不得维护第二套 Prompt 输出合同。

OmniAuto 权威结果：

```json
{
  "enabled": true,
  "mode": "brain_first",
  "applied": true,
  "adoptable": true,
  "rule_name": "customer_service_brain_reply",
  "reason": "brain_guard_passed",
  "needs_handoff": false,
  "reply_text": "...",
  "visible_reply_owner": "brain",
  "visible_reply_source": "brain_plan.reply_segments",
  "brain_plan": {
    "schema_version": 1,
    "answer_mode": "direct_answer",
    "reply_segments": ["..."],
    "risk": {
      "risk_level": "low",
      "risk_tags": [],
      "needs_handoff": false,
      "handoff_reason": ""
    },
    "recommended_action": "send_reply",
    "confidence": 0.86
  }
}
```

`brain_plan.recommended_action` 只沿用 OmniAuto 正式枚举：

```text
send_reply / handoff / handoff_for_approval / no_action / pause /
retry_later / fallback_existing
```

唯一映射：

| OmniAuto 结果 | 车金后端结果 |
|---|---|
| `adoptable=true` 且 `recommended_action=send_reply` 且 Guard 通过 | `decision=send_reply`，创建 `reply_action`；`reply_text` 必须原样来自 Brain |
| `recommended_action=handoff` | `decision=handoff`，创建 `handoff_event` |
| `recommended_action=handoff_for_approval` | `decision=handoff`，保留原动作到 `ai_response_snapshot` |
| `recommended_action=no_action` | `batch_status=no_action`，恢复进入该批次前的可监听状态 |
| `recommended_action=pause` | `batch_status=paused`，进入人工接管并关闭本会话自动回复 |
| `recommended_action=retry_later` | `batch_status=retry_wait`，执行有限重试，耗尽后转人工 |
| `fallback_existing` | Brain First 模式不采用旧本地回复；转 `batch_status=failed, decision=retry_later` |
| `customer_service_brain_no_visible_reply` 或 `no_visible_reply.retryable=true` | `batch_status=failed, decision=retry_later`，不得转 `no_action` |
| Brain 调用前业务门禁明确无需回复 | `batch_status=no_action, decision=no_action`；这不是伪造的 BrainPlan 动作 |

车金 `AIEngineDecision` 是适配器内部类型，不是新的模型输出协议。`raw_payload` 必须保存原始 OmniAuto Brain 结果，便于审计映射是否正确。

## 9. 图片接口冻结

图片能力复用 OmniAuto 当前命名和边界，不新增图片专用后端接口：

```text
image_bubble
→ C2 同行头像规则确定 customer/self
→ Worker建立稳定身份并只选择NEW_IMAGE
→ 当前剪贴板一次性图片事务
→ customer_image_understanding
→ visual_bridge_input
→ 与本轮文字、语音按最终当前屏顺序组成messages/ingest
→ 后端用服务端权威车源校验视觉产品线索
→ message_batch
→ customer_service_brain
```

### 9.1 调用边界

`customer_image_understanding` 是结果对象，不是 Sidecar action。客户端同步新版 OmniAuto 后，Worker 通过本地 OmniAuto Vision 插件调用：

```python
BuiltinVisionPlugin.run(context)
```

底层继续复用 OmniAuto 已有：

```python
maybe_route_customer_image_turn(
    connector=...,
    target=...,
    config=...,
    payload=...,
    target_state=...,
    batch=...,
    combined=...,
)
```

这里不新增 `image-recognize`、`image-save` 或 `/images/upload`：

1. Worker 保持当前会话和 UI 锁。
2. OmniAuto connector 的 `run_customer_clipboard_image_transaction` 在当前会话完成右键复制。
3. Vision 插件只在进程内消费不可 JSON 序列化的临时图片载荷；Windows 原始位图先按源内存上限解码，再缩放和自适应编码到 Provider 上限。
4. 完成目标指纹校验并取得内存副本后，清除本次复制产生的系统剪贴板内容。
5. 插件释放图片内存并返回通过共享 schema 的文字化 `customer_image_understanding`、`visual_bridge_input`。
6. Worker 只把允许持久化的文字结果映射进现有 `messages/ingest`。

如果新版 OmniAuto 尚未同步进 Worker 打包目录，本能力必须保持关闭，不允许用临时接口代替。

硬约束：

- 不恢复 Sidecar `image-save` 或 `image-clipboard-copy` action。
- 不使用 `image_local_path`、截图裁切、历史图片文件或后端图片上传接口。
- 图片只在当前进程内存中短暂存在；Vision 完成后立即释放。
- Windows `CF_DIB/CF_DIBV5/CF_BITMAP` 原始内存上限与最终 Provider 3 MB 上限分开；不得在压缩前用 3 MB 拒绝普通 1080p 位图。
- 图片复制到内存并校验后清除当前剪贴板代次；生产环境关闭剪贴板历史和跨设备同步。
- 可持久化的只有 `customer_image_understanding` 文字结果、`visual_bridge_input` 和不含图片内容的事务审计。
- 图片角色只使用 C2 的 `sender_role/sender_role_source=same_row_avatar`；忽略 Vision 的 `side/visual_side` 归属判断。
- Vision 不生成客户可见回复；唯一回复作者仍是 `customer_service_brain`。
- Provider 地址默认只允许 HTTPS，请求风格使用明确白名单。未知风格不得自动落入其他 Provider 分支。
- 一次非 JSON 格式纠正重试属于同一合法 Vision 调用；父进程安全预算必须覆盖两次请求和进程通信余量。
- 车金严格入口禁止把客户端 OmniAuto `KnowledgeRuntime` 的产品候选作为正式产品事实；正式 product_id 只能由后端权威车源验证。

OmniAuto 文字化结果沿用以下对象名：

```json
{
  "schema_version": 1,
  "enabled": true,
  "applied": true,
  "adoptable": true,
  "reason": "vision_ready",
  "provider": "openai_compatible",
  "request_style": "anthropic_messages_vision",
  "model": "...",
  "source_messages": [{"message_id": "...", "message_type": "image"}],
  "local_visual_profile": {},
  "vision_summary": "...",
  "image_ocr_text": [],
  "classification": {},
  "entities": {},
  "intent_hints": {},
  "bridge": {},
  "catalog_alignment": {},
  "audit": {}
}
```

字段名来自 OmniAuto `normalize_customer_image_understanding_result`，但不能继续由
OmniAuto、Worker 和后端分别手抄限制。`contracts/c2_contract_v3.json` 必须新增
完整的 `customer_image_understanding_v1` 与 `visual_bridge_input_v1` JSON Schema，
并生成或复用三层校验器。Schema 固定必填字段、类型、长度、数组条数、有限数及
`0..1` 置信度范围；字符串 `"false"`、NaN、越界值和仅靠默认空字段不得成为
completed。持久化时剔除 Provider 原始响应/错误正文、图片字节、图片路径、
`asset_id`、缩略图和可还原图片的内容。

正式启用图片入库必须同时满足：

1. `contracts/c2_contract_v3.json` 必须允许 `completed` 图片事实和结构完整的 `failed` 图片事实入库，同时拦截 `discovered/ignored/pending` 占位。
2. Worker 在 `raw_payload.customer_image_understanding` 保存上述文字白名单投影；不得改名为 `image_recognition`，也不得保存完整运行时对象。
3. 后端校验图片 canonical message 与原始 `image_bubble` 的稳定来源关系。
4. `failed customer/self` 图片不得伪装文字；后端使用与 failed 语音相同的角色和最终画面顺序门禁，未被可靠后续销售事实覆盖时禁止 Brain；同批已确认文字和语音仍正常入库。
5. 后端给 Brain 的历史图片保留紧凑 `item_state/error_code`、摘要、OCR、分类、实体、中性查询和服务端确认产品 ID，支持下一轮“这辆/刚才那台”。
6. 图片检测不得静默限制为 8 张。若内部容量不足，必须返回 `observation_truncated=true + C2_IMAGE_OBSERVATION_FAILED` 并阻断 Brain。
7. 自动化测试和 Windows 实机回归通过后再开启能力开关。

## 10. 当前实施审计状态

| 优先级 | 状态 | 说明 |
|---|---|---|
| P0 | 已收口 | 合同指纹由 Worker 生成；`sidecar_run_id` 只存在 evidence；后端 V3 schema/service 强制同一机器合同。 |
| P0 | 已收口 | 图片只使用 `customer_image_understanding/visual_bridge_input`，Outbox 禁止原图、路径、base64 和 Provider 原始响应。 |
| P0 | 已收口 | `messages/ingest` 返回 `message_batch`；Worker 按 `batch_id` 在原会话和同一 UI 锁下等待、刷新和发送。 |
| P0 | 已收口 | 后端真实 OmniAuto Brain Adapter 与状态映射已实现；客户后发重建批次，销售后发取消回复。 |
| P0 | 已收口 | 后端逐条返回 `source_message_key`，Worker 精确确认本地 ledger；未确认时 Outbox 只重传 JSON，不重做 RPA/Vision。 |
| P0 | 已收口 | `chat_reply` 只能由持有当前会话 UI 锁的 C2 flow 领取；普通任务线程被 Worker 与后端双重阻断。 |
| P0 | 已在当前候选收口；本轮回归保留 | 语音在右键前查询持久化稳定身份，最终槽位先判定 NEW/OLD/OUTBOX；初次图片角色不可信使用帧级身份门禁，不持久化为 ignored。 |
| P0 | 已收口 | 新好友先调用 activation-confirm，再读取消息、转写语音和处理图片。 |
| P1 | 已在当前候选收口；本轮回归保留 | 普通会话继续复用 open-chat 确认帧；Vision 只门禁新的 C2 UI 流程，事务恢复与事实结算先于能力预检。 |
| P0 | 已收口 | Sidecar、Worker 与后端已统一落实 `action_phase`；发送触发后无法确认时只进入 `unknown`，不会按普通失败清除证据。 |
| P0 | 已收口 | Worker 已使用唯一 `merge_item_outcomes` 单调累计语音/图片逐条结果；后续调用只能合并，不能覆盖既有成功或失败结果。 |
| P0 | 已在当前候选收口；本轮回归保留 | 删除 `C2_IMAGE_PROCESSING_DEFERRED` 和图片跨轮未收口项；出屏图片本轮不存在，仍可见但不能唯一确认则 failed；当前屏 NEW_IMAGE 必须在同一 Flow 内终态。 |
| P0 | 已收口 | 图片角色只采用 C2 同行头像结论；复制前几何侧仅记录物理一致性证据，不再作为第二套准入规则。 |
| P0 | 已收口 | 后端失败外壳与 Worker Outbox 统一使用 `retry / refresh_and_rebuild / capability_paused`；旧 `quarantine/abandoned` 仅作为启动迁移输入，不再是运行时终态。 |
| P0 | 已在当前候选收口；本轮回归保留 | 图片专用恢复必须改为 voice/image 共用 `media_fact` 协调器；语音日志补齐 replayable observation，并进入同一 Worker 级全局门禁。 |
| P0 | 已在当前候选收口；本轮回归保留 | 废弃新合同中的 `target_terminated -> 本地not_required`；实现 `resume_current_target / settle_without_ui / retry_later` 与 `fact_settlement`。 |
| P0 | 已在当前候选收口；本轮回归保留 | `unbound/binding_failed/needs_review/degraded/paused` 不得作为永久终止；原事务身份可信时 fact_only 结算，暂不可证时 retry_later；单次短码 OCR 缺失不得覆盖已有 bound 绑定。 |
| P0 | 已在当前候选收口；本轮回归保留 | 技术恢复终态不得创建 handoff 冒充结算；改用通用 recovery settlement 持久化，并支持绑定已不存在时仍安全终结原事务。 |
| P0 | 已在当前候选收口；本轮回归保留 | 分离 Windows 原始位图和 Provider 载荷上限，支持普通 1080p DIB/HBITMAP 自适应压缩，并在取入内存后清除系统剪贴板。 |
| P0 | 已在当前候选收口；本轮回归保留 | Vision 父进程预算覆盖两次合法请求；Provider 使用 HTTPS/请求风格白名单；模型结果由共享完整 schema 校验。 |
| P0 | 已在当前候选收口；本轮回归保留 | 图片观察不得静默截断 8 张；failed 图片门禁覆盖 customer/self；历史图片上下文保留结构化结果。 |
| P0 | 已在当前候选收口；本轮回归保留 | 车金严格 Vision 入口不得使用客户端本地 KnowledgeRuntime 确认正式产品 ID；后端使用服务端权威车源验证。 |
| P1 | 待实机 | 真实 Vision Provider 凭据、Windows 微信图文语音混合回归与进程重启 Outbox 回归尚未完成。 |

## 11. 联调验收门禁

1. 接口样例必须通过后端 schema、Worker adapter 和 OmniAuto contract test 三方验证。
2. 任一新增字段必须明确唯一所有者、是否必填、缺失行为和版本号。
3. 不得同时出现 `image_recognition` 与 `customer_image_understanding`。
4. 不得同时把 `brain_plan.recommended_action` 和车金 `batch_status` 当成同一状态机。
5. 不得让 OmniAuto 依赖 `contracts/c2_contract_v3.json`。
6. 不得让后端重做 OCR、左右侧或语音父子归属判断。
7. 不得让 Worker 自己生成 `conversation_id/authorization_revision/reply_text`。
8. 每次合同变更先改本文和机器合同，再改客户端/后端代码，最后做 Windows 实机回归。
9. `action_phase`、逐条结果合并和 Outbox 恢复动作各自只能有一个集中判定器；主编排函数不得复制判断逻辑。
10. 不得为单个事故增加只匹配某段错误文字、某个截图名称或某个测试数据的业务分支。
11. 业务编排只消费统一结果对象，不直接解析 Sidecar 原始异常、HTTP 文案或 Provider 原始返回。
12. 状态转换测试必须表驱动覆盖全部枚举组合，并至少验证：完成事实不丢失、失败集合不缩小、未知发送不重发、隔离 Outbox 不循环重建。
13. 同一业务规则只能在一层拥有：OmniAuto 提供观察证据，Worker 映射执行结果，后端持久化业务真相。
14. 图片专项自动化、来源追溯、不可变候选包和 Windows UAT 必须逐项通过
`AI智能客服售前跟进系统_技术方案手册_v0.8.md` 第 8.5 至 8.8 节；任何 P0
未通过不得形成正式 UAT 结论。
15. 允许在现有大函数周围提取上述集中判定器和阶段结果对象；本期不要求全面重写，但禁止继续增长重复分支。
