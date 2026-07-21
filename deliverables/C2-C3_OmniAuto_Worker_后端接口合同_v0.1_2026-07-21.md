# C2-C3 OmniAuto / Worker / 后端接口合同

版本：v0.1

日期：2026-07-21

状态：接口命名和职责边界冻结；文字/语音按 V16.104 现状执行；图片与 C2-C3 单会话串行链路按本文开发，未完成前不得宣称已接入。

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

| 名称 | 源码依据 | V16.104 是否已有 |
|---|---|---|
| `sessions/open-chat/messages/voice-transcribe/send` | OmniAuto `wechat_win32_ocr_sidecar.py` 的现有 action | 是 |
| `customer_service_brain/brain_plan/reply_segments` | OmniAuto `customer_service_brain.py`、`customer_service_brain_contract.py`、`layer_contracts.py` | Brain 源码已有；车金真实后端 Adapter 未接通 |
| `customer_image_understanding/visual_bridge_input` | 新版 OmniAuto `customer_image_turn_router.py` 和 Vision 插件 | 否；当前只在待同步的新版 OmniAuto 中，合并并重新打包后才可调用 |
| `message_batch/batch_id/reply_action/handoff_event` | 车金后端持久化和调度对象 | 部分已有；Worker 按 `batch_id` 原会话等待尚未实现 |

因此，本文复用的是 OmniAuto 已有能力名，不代表 V16.104 已包含所有实现。尤其不得为了提前接图片而在 V16.104 上临时创造另一个 Sidecar action 或后端接口。

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
| `customer_image_understanding`、`visual_bridge_input` | OmniAuto Vision | 只包含文字化结果和非图片内容审计，不生成客户回复 |
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
- `voice_bubble` 只是待处理观察，不能直接入库。
- `voice_transcript` 必须带 `parent_voice_anchor_key`，最终只形成一条 `message_type=voice`，不得再形成一条 text。
- `image_bubble` 当前只观察、不入库；图片合同启用后按第 9 节处理。

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
- `partial` 只是本轮 flow 事实，不创建每条语音的后端 pending 任务。

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
  "authorization_revision": "..."
}
```

准入条件固定为：

```text
有效短码
AND conversation_type=private
AND 当前 read-target 存在
AND authorization_revision 未变化
```

`read-targets=[]` 时允许 `sessions` 被动扫描并上报事实，但不得 `open-chat/messages/voice-transcribe/send`。

### 6.3 `POST /api/workers/{worker_id}/wechat/messages/ingest`

V3 请求顶层字段：

```json
{
  "contract_version": 3,
  "contract_revision": "3.1.0",
  "contract_sha256": "64位sha256",
  "observation_schema_version": 3,
  "read_run_id": "read-...",
  "conversation_id": "...",
  "remark_code": "CJ123456",
  "rpa_session_key": "...",
  "authorization_revision": "...",
  "messages": [],
  "evidence": {
    "sidecar_run_id": "message-...",
    "observations": []
  }
}
```

硬规则：

- 顶层不再发送 `sidecar_run_id`；它只放在 `evidence.sidecar_run_id`，避免同一字段双份。
- `contract_*` 由 Worker 生成。Sidecar 即使兼容性输出同名字段，也只可作诊断，不是权威来源。
- V3 下 `contract_revision/contract_sha256/observation_schema_version/remark_code/authorization_revision` 和每条 canonical message 必填字段必须在 Pydantic schema 与 service 校验中一致，不再一层 optional、一层 required。
- 后端不得根据 `raw_payload` 重算 `sender_role/message_type/screen_order`；只做合同校验和拒绝。

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
    "frame_source": "final_read"
  },
  "raw_payload": {}
}
```

响应继续返回逐条 `ingested/duplicated/ignored`。后端唯一约束仍是最终防线，Worker 本地小缓存只是减少重复请求，不能取消后端去重。

## 7. C2-C3 单会话串行扩展合同

V16.104 尚未实现本节。实现后仍复用 `messages/ingest`，不另造“上报并问 Brain”接口。

当本轮出现可触发 Brain 的新客户消息时，`messages/ingest` 的 `data` 增加一个可选对象：

```json
{
  "ingested_count": 2,
  "duplicated_count": 1,
  "ignored_count": 0,
  "results": [],
  "message_batch": {
    "batch_id": "...",
    "batch_status": "collecting"
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
| `failed` | `retry_later` | 这是技术失败，不发送；保留错误与未回复事实，释放 UI 锁 |

禁止行为：

- Brain 技术失败不得伪装成 `no_action`。
- `no_action` 不得伪装成 `failed`。
- 等待期间不得切换到其他微信会话。
- 不设置“正常 Brain 思考超过 N 秒就自动放弃”的业务总时限；仅允许网络重连、进程存活检查、人工停止和明确技术失败。
- Worker 停止监听时可以取消本地等待，但不得把未知结果当成发送失败或发送成功。
- `ai_enabled` 是人工明确关闭全部自动化的硬开关。Brain 转人工、销售人工回复、进入 `waiting_sales_reply` 或 `sales_replied_waiting_user` 都只能用会话状态阻断当下动作，不得自动把 `ai_enabled` 改成 `false`。
- 销售人工回复后进入 `sales_replied_waiting_user`；客户再次回复时回到 `ai_active`。客户长期未回复时仍可由 `customer_service_brain` 生成召回。

发送仍复用既有接口：

```text
POST /api/reply-actions/{reply_action_id}/claim-send
POST /api/reply-actions/{reply_action_id}/sent-ack
```

`sent-ack.send_result` 只允许 `sent / failed / unknown`。

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

`brain_plan.recommended_action` 只沿用 OmniAuto 枚举：

```text
send_reply / handoff / handoff_for_approval / fallback_existing
```

唯一映射：

| OmniAuto 结果 | 车金后端结果 |
|---|---|
| `adoptable=true` 且 `recommended_action=send_reply` 且 Guard 通过 | `decision=send_reply`，创建 `reply_action`；`reply_text` 必须原样来自 Brain |
| `recommended_action=handoff` | `decision=handoff`，创建 `handoff_event` |
| `recommended_action=handoff_for_approval` | `decision=handoff`，保留原动作到 `ai_response_snapshot` |
| `fallback_existing` | Brain First 模式不采用旧本地回复；转 `batch_status=failed, decision=retry_later` |
| `customer_service_brain_no_visible_reply` 或 `no_visible_reply.retryable=true` | `batch_status=failed, decision=retry_later`，不得转 `no_action` |
| Brain 调用前业务门禁明确无需回复 | `batch_status=no_action, decision=no_action`；这不是伪造的 BrainPlan 动作 |

车金 `AIEngineDecision` 是适配器内部类型，不是新的模型输出协议。`raw_payload` 必须保存原始 OmniAuto Brain 结果，便于审计映射是否正确。

## 9. 图片接口预冻结

图片能力复用 OmniAuto 当前命名和边界，不新增图片专用后端接口：

```text
image_bubble
→ C2 同行头像规则确定 customer/self
→ 当前剪贴板一次性图片事务
→ customer_image_understanding
→ visual_bridge_input
→ 与本轮文字、语音按最终当前屏顺序组成 message_batch
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
3. Vision 插件只在进程内消费不可 JSON 序列化的临时图片载荷。
4. 插件释放图片内存并返回文字化 `customer_image_understanding`、`visual_bridge_input`。
5. Worker 只把允许持久化的文字结果映射进现有 `messages/ingest`。

如果新版 OmniAuto 尚未同步进 Worker 打包目录，本能力必须保持关闭，不允许用临时接口代替。

硬约束：

- 不恢复 Sidecar `image-save` 或 `image-clipboard-copy` action。
- 不使用 `image_local_path`、截图裁切、历史图片文件或后端图片上传接口。
- 图片只在当前进程内存中短暂存在；Vision 完成后立即释放。
- 可持久化的只有 `customer_image_understanding` 文字结果、`visual_bridge_input` 和不含图片内容的事务审计。
- 图片角色只使用 C2 的 `sender_role/sender_role_source=same_row_avatar`；忽略 Vision 的 `side/visual_side` 归属判断。
- Vision 不生成客户可见回复；唯一回复作者仍是 `customer_service_brain`。

OmniAuto 文字化结果沿用当前 schema：

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

上面字段名直接来自 OmniAuto `normalize_customer_image_understanding_result`。持久化时必须做文本白名单投影，剔除 Provider 原始响应、图片字节、图片路径、`asset_id`、缩略图和可还原图片的内容；不得把完整运行时对象原样写入 `raw_payload`。

正式启用图片入库前必须同时完成：

1. 将 `contracts/c2_contract_v3.json` 升级新 revision，使“已完成 Vision 的 image 消息”可入库；当前 `3.1.0` 的 `image_bubble` 仍为不可入库。
2. Worker 在 `raw_payload.customer_image_understanding` 保存上述文字白名单投影；不得改名为 `image_recognition`，也不得保存完整运行时对象。
3. 后端校验图片 canonical message 与原始 `image_bubble` 的稳定来源关系。
4. 自动化测试和 Windows 实机回归通过后再开启能力开关。

## 10. 当前代码与目标合同差异

| 优先级 | 当前差异 | 修改责任人 |
|---|---|---|
| P0 | Worker 仍要求 Sidecar 输出车金 `contract_revision/contract_sha256`；目标应由 Worker 本地生成并校验 OmniAuto `observation_schema_version=3` | 客户端工程师 |
| P0 | Worker 当前把 `sidecar_run_id` 同时放在 ingest 顶层和 evidence；目标只保留 evidence | 客户端工程师 |
| P0 | 后端 V3 必填字段在 Pydantic 中仍声明 optional、在 service 中才强校验 | 后端工程师 |
| P0 | 后端仍保留上一轮 WIP 的 `raw_payload.image_recognition` 识别和 `image_local_path` 兼容分支；它们不是本文正式图片接口 | 后端工程师在图片开发启动时删除或一次性迁移为 `customer_image_understanding`，不得双轨长期共存 |
| P0 | `messages/ingest` 目前不返回 `message_batch`，Worker 也不能按 `batch_id` 保持原会话等待 Brain | 后端 + 客户端工程师 |
| P0 | `RealOmniAutoAIEngineAdapter` 尚未实现，当前 C3 只有 mock；且车金 `decision` 尚未按本合同映射 OmniAuto `brain_plan` | 后端工程师 |
| P0 | 后端在创建 handoff 和识别销售人工消息时会写 `conversation.ai_enabled=false`，与“状态门禁负责临时阻断、硬开关不自动关闭”的流程合同冲突 | 后端工程师 |
| P1 | 图片在 C2 合同 `3.1.0` 中仍不可入库，OmniAuto 图片能力尚未接入 Worker 单会话 flow | OmniAuto/客户端 + 后端工程师 |
| P1 | 当前 C3 仍通过全局任务拉取发送，可能在 Brain 等待期间切换其他会话 | 客户端 + 后端工程师 |

## 11. 联调验收门禁

1. 接口样例必须通过后端 schema、Worker adapter 和 OmniAuto contract test 三方验证。
2. 任一新增字段必须明确唯一所有者、是否必填、缺失行为和版本号。
3. 不得同时出现 `image_recognition` 与 `customer_image_understanding`。
4. 不得同时把 `brain_plan.recommended_action` 和车金 `batch_status` 当成同一状态机。
5. 不得让 OmniAuto 依赖 `contracts/c2_contract_v3.json`。
6. 不得让后端重做 OCR、左右侧或语音父子归属判断。
7. 不得让 Worker 自己生成 `conversation_id/authorization_revision/reply_text`。
8. 每次合同变更先改本文和机器合同，再改客户端/后端代码，最后做 Windows 实机回归。
