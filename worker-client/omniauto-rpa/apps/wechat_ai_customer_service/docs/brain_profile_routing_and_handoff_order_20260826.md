# Brain 短消息路由与转人工发送顺序修复方案

本文遵循：

- `customer_visible_reply_ownership_baseline.md`
- `customer_service_external_contract_and_optional_plugin_baseline.md`

## 1. 问题范围

本次修复覆盖两个相互关联、但独立验证的问题：

1. 短业务咨询被错误送入 `low_authority_fast` 社交短消息 profile，导致正常的商品咨询被 Brain 保守地判定为转人工。
2. `reply_then_handoff` 在回复尚未发送时就建立了权威人工 handoff。Worker 暂停、排队或重启时，客户可见的边界回复可能没有发送，但会话已经被人工闸门锁定。

## 2. 根因

### 2.1 短消息路由误判

`low_authority_fast_profile_decision` 原先主要使用消息长度和少量关键词判断。消息“我想找个便宜点的电车，有合适的吗”虽然表达了明确的购车推荐意图，但没有命中原有的“推荐/预算/哪款”等组合条件，最终被标记为 `short_low_authority_turn`。

该 profile 的提示词只允许处理问候、寒暄、感谢、催促和普通闲聊，并要求商品事实问题转人工；因此模型随后返回了 `no_product_authority` 和 `needs_handoff=true`。

完整 Brain profile 对“缺少相关业务证据”的处理是软提示，允许在不编造事实的前提下询问预算、用途或偏好。因此，缺少商品数据是错误 profile 下的放大因素，不是这类消息必须转人工的硬规则。

### 2.2 转人工顺序漏洞

原流程在生成 `reply_then_handoff` 动作时立即创建开放 handoff，并把会话状态改为 `waiting_sales_reply`。发送任务仍是 `pending`，但后续读取和发送流程已经受到人工闸门影响。

## 3. 修复设计

### 3.1 业务意图闸门

在短消息 profile 最终放行前，增加“商品语义 + 业务需求词”判断。业务需求词包括找、买、便宜、合适、适合、需要、用途和预算等。命中后返回既有的 `business_decision_needs_context` 原因，进入完整 Brain；不修改现有函数名、配置键或外部返回字段。

社交闲聊例如“今天路上堵车了”仍可使用短 profile，因为只有车辆字面语境而没有购买或推荐意图。

### 3.2 可见回复先行

`reply_then_handoff` 创建的 handoff 先使用内部状态 `pending_visible_reply`：

```text
生成 Brain 回复
  -> 创建待发送动作和 pending_visible_reply 记录
  -> 不建立权威人工会话闸门
  -> Worker 领取并发送回复
  -> 收到 sent_ack=sent
  -> 将 handoff 激活为 created，切换 waiting_sales_reply，并发送人工通知
```

如果 Worker 暂停或任务仍在队列中，会话保持 `ai_active`，回复动作可以安全恢复；如果发送失败，则沿用现有发送失败转人工流程。已有 handoff、任务、回复动作和 API 字段继续保留，只增加一个内部状态值。

## 4. 兼容性与安全边界

- 保留 `low_authority_fast_profile_decision`、`reply_then_handoff`、`handoff_event`、`sent_ack` 等既有接口和字段。
- 不改变 Brain 对客户可见回复的所有权；回复仍只能来自 BrainPlan。
- 不让缺少商品数据导致编造价格、库存、车况或承诺；完整 Brain 只能询问澄清或给出有边界的回复。
- `pending_visible_reply` 不参与权威人工 handoff 查询，也不触发人工通知。
- 发送确认后才进入既有人工接管状态；发送失败、未知结果和重复回执继续使用既有终态处理。

## 5. 验证方案

### 分类回归

- “我想找个便宜点的电车，有合适的吗”不得进入 `low_authority_fast`。
- “秦PLUS多少钱”继续走完整权威路径。
- “有车吗”继续走完整权威路径。
- “我不太懂车，直接帮我挑最稳的”继续走完整权威路径。
- “今天路上堵车了”仍可使用短 profile。

### 发送顺序回归

- 生成 `reply_then_handoff` 后，handoff 状态为 `pending_visible_reply`，会话仍为 `ai_active`，且没有 `sent_ack`。
- Worker 可以在该状态领取并 claim-send。
- 收到 `sent_ack=sent` 后，handoff 变为 `created`，会话变为 `waiting_sales_reply`。
- 没有 `sent_ack` 时，不得把会话标记为正式人工接管。

## 6. 回滚

如果上线后发现 profile 路由异常，可回滚本次提交；未改变数据库字段和外部 API，旧数据无需迁移。已生成的 `pending_visible_reply` 记录由发送任务终态处理，不需要人工改写状态文件。
