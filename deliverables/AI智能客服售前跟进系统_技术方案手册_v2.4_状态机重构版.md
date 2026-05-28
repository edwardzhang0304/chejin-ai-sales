# AI智能客服售前跟进系统 技术方案手册（状态机重构版）

版本：v2.4

日期：2026-05-26

适用范围：第一期正式工程版本。

说明：本版根据最新业务沟通重构会话状态机：取消 AI 固定轮次限制，改为“AI持续接待 / 转人工 / 等待用户回复 / 召回 / 等待销售回复超时提醒”的长期跟进模型。

## 1. 核心变化

| 事项 | 旧口径 | 新口径 |
|---|---|---|
| AI回复轮次 | 有固定轮次限制，达到上限后进入观望。 | 取消轮次上限。只要未拒绝、未关闭、未转人工且风控允许，客户继续聊，AI继续回复。 |
| 等待用户回复 | 主要用于观望客户召回。 | AI回复、人工回复、召回回复后，都进入等待用户回复状态。 |
| 自动召回 | 只覆盖观望客户，每客户最多一次。 | 覆盖所有等待用户回复的会话；超过N天未回复可再次召回，直到用户回复、拒绝或关闭。 |
| 转人工后销售未回复 | 原方案不做二次提醒。 | 进入等待销售回复状态；超过N天销售未回复，飞书通知销售。 |
| Worker职责 | 容易被理解为扫所有会话。 | Worker只采集系统绑定会话的微信事实；服务端负责状态机和超时判断。 |

## 2. 总体分工

```text
Worker = 微信事实采集器 + 微信动作执行器
服务端 = 状态机 + 规则判断器 + 任务调度器
```

Worker 上报事实：

- 客户新文字消息。
- 客户新图片消息。
- 销售手机端同步到桌面端的人工回复。
- AI/召回发送成功或失败。
- 图片另存成功或失败。
- 微信登录、窗口、风控提示、发送异常。

服务端负责判断：

- 客户消息是否由 AI 回复。
- 是否需要转人工。
- 是否进入等待用户回复。
- 是否达到召回时间。
- 是否生成 follow_up 任务。
- 是否等待销售回复超时并发送飞书通知。
- 是否停止自动动作。

## 3. 会话主状态

| 状态 | 含义 | 当前等待谁 | 允许动作 |
|---|---|---|---|
| `new` | 线索刚进入系统。 | 系统分配 | 分配销售、绑定Worker。 |
| `assigned` | 已分配销售和Worker。 | 系统生成任务 | 创建加好友任务。 |
| `add_friend_pending` | 待加好友。 | Worker执行 | 手机号搜索、发送申请、写初始备注。 |
| `add_friend_sent` | 好友申请已发出。 | 客户通过/人工确认 | 等待成为好友或失败处理。 |
| `friend_added` | 已成为好友。 | Worker绑定会话 | 绑定微信会话，进入AI接待。 |
| `ai_active` | AI正常接待。 | 客户/AI | 客户来消息后AI可持续回复，不设轮次上限。 |
| `waiting_user_reply` | 我方已经回复，等待客户回。 | 客户 | 服务端到期生成召回任务；Worker监听客户是否回复。 |
| `recalled_waiting_user` | AI已发过召回，继续等待客户回。 | 客户 | 到下一轮召回周期后可再次召回。 |
| `waiting_human_reply` | AI已转人工，等待销售回复客户。 | 销售 | 超过N天销售未回，服务端飞书通知销售。 |
| `human_replied_waiting_user` | 销售已回复，等待客户回。 | 客户 | 到期可由AI发送召回内容。 |
| `rejected` | 客户明确拒绝或黑名单。 | 无 | 不加好友、不回复、不召回。 |
| `closed` | 人工关闭或流程结束。 | 无 | 不自动处理。 |

## 4. 状态流转规则

### 4.1 AI持续接待

```text
客户发消息 -> Worker上报message_event -> 服务端判断可AI回复 -> AI生成回复 -> Worker发送 -> sent_ack -> 状态=waiting_user_reply
```

规则：

- AI不再按句数停止。
- AI是否继续聊由风控、客户意图、模型置信度、会话状态决定。
- 命中高风险、高意向、模型失败、图片低置信或证据不足时，不继续自动回复，转人工。

### 4.2 等待用户回复

任何我方回复成功后，都进入等待用户回复：

```text
AI回复成功 -> waiting_user_reply
AI召回成功 -> recalled_waiting_user
销售人工回复成功 -> human_replied_waiting_user
```

如果客户回复：

```text
未转人工会话 -> ai_active
已转人工会话 -> waiting_human_reply 或保持人工负责，由销售继续处理
```

如果客户明确拒绝：

```text
任意状态 -> rejected
```

### 4.3 自动召回

召回不再只属于观望客户，而属于“等待用户回复”类状态。

触发条件：

```text
status in (waiting_user_reply, recalled_waiting_user, human_replied_waiting_user)
且 now - last_outbound_at >= N天
且 last_inbound_at为空 或 last_inbound_at < last_outbound_at
且 未拒绝
且 未关闭
且 不在黑名单
且 未命中静默时段
且 未超过每日召回上限
且 未超过单客户召回策略限制
```

动作：

```text
服务端创建follow_up_task
Worker领取任务
Worker发送AI召回内容
Worker上报sent_ack
服务端更新last_recall_at、recall_count、last_outbound_at
状态=recalled_waiting_user
```

召回次数口径：

- 业务上支持持续召回，直到用户回复、拒绝或关闭。
- 工程上必须保留配置项：召回间隔、每日召回上限、单客户最大召回次数、静默时段。
- 如果项目方配置为“不限次数”，系统也必须受每日上限和静默时段约束。

### 4.4 转人工与销售超时提醒

触发转人工：

```text
客户高意向/高风险/模型失败/图片低置信/车源证据不足 -> waiting_human_reply
```

转人工后：

- AI不再自由回复客户新消息。
- Worker继续监听该绑定会话，识别销售手机端同步到桌面端的人工回复。
- 销售回复后，状态进入 `human_replied_waiting_user`。

销售超时未回复：

```text
status=waiting_human_reply
且 now - handoff_at >= N天
且 sales_first_reply_at为空
-> 服务端触发飞书通知销售
```

飞书口径：

- 只做通知。
- 不做按钮。
- 不做二次发送功能。
- 不单独增加角色权限。
- 失败时记录错误日志，由项目方人工查看处理。

## 5. Worker与服务端定时扫描分工

| 扫描类型 | 谁做 | 扫什么 | 结果 |
|---|---|---|---|
| 微信事实监听 | Worker | 系统绑定会话的新消息、图片、销售人工回复、微信异常。 | 上报 message_event / image_event / human_sales_event / wechat_error。 |
| 微信补偿扫描 | Worker | 绑定会话中近期活跃、等待用户回复、等待销售回复的会话。 | 防止漏消息，补充上报事实。 |
| 召回到期扫描 | 服务端 | 数据库中的等待用户回复类状态。 | 创建 follow_up_task。 |
| 销售超时扫描 | 服务端 | `waiting_human_reply` 且销售未回复的会话。 | 发送飞书通知销售。 |
| Worker健康扫描 | 服务端 | Worker heartbeat、last_sync_at、当前任务。 | 标记离线、卡住、异常。 |
| 发送结果恢复 | Worker + 服务端 | `sending`、`unknown_send_result`、超时任务。 | 防止重复发送，必要时人工确认。 |

结论：

```text
Worker定时扫微信事实。
服务端定时扫数据库状态。
Worker不判断业务规则。
服务端不直接操作微信。
```

## 6. 核心数据字段

| 字段 | 含义 | 来源 |
|---|---|---|
| `last_inbound_at` | 最近客户消息时间。 | Worker上报客户消息。 |
| `last_outbound_at` | 最近我方发出消息时间。 | Worker上报AI/召回/人工发送事实。 |
| `last_ai_reply_at` | 最近AI回复时间。 | Worker发送AI成功后上报。 |
| `last_recall_at` | 最近召回时间。 | Worker发送召回成功后上报。 |
| `last_sales_reply_at` | 最近销售人工回复时间。 | Worker识别销售人工消息后上报。 |
| `sales_first_reply_at` | 转人工后销售首次回复时间。 | Worker识别销售人工消息后上报。 |
| `handoff_at` | 进入等待销售回复时间。 | 服务端转人工时写入。 |
| `recall_count` | 已召回次数。 | 服务端根据sent_ack更新。 |
| `ai_enabled` | 是否允许AI自由接待。 | 服务端状态机维护。 |
| `owner` | 当前会话责任方：ai / human。 | 服务端状态机维护。 |

## 7. 任务类型

| 任务 | 触发方 | 执行方 | 说明 |
|---|---|---|---|
| `add_friend` | 服务端 | Worker | 手机号搜索、发送好友申请、写初始备注。 |
| `chat_reply` | 服务端 | Worker | 执行AI回复动作。 |
| `follow_up` | 服务端 | Worker | 发送AI召回内容。 |
| `save_image` | Worker | Worker | 点开并另存客户图片。 |
| `handoff_notify` | 服务端 | 服务端 | 飞书通知销售；不需要Worker操作微信。 |

## 8. 幂等与防重复

| 对象 | 约束 |
|---|---|
| `message_event` | `unique(worker_id, conversation_id, dedupe_key)` |
| `message_batch` | 同一 `conversation_id` 同一时间最多一个 active batch。 |
| `reply_action` | Worker只能执行当前有效 `reply_action_id`。 |
| `send_receipt` | `unique(reply_action_id)`，同一动作只能确认一次。 |
| `follow_up_task` | 召回任务按 `conversation_id + recall_round + rule_id` 去重。 |
| `handoff_notify` | 同一销售超时提醒周期只发送一次飞书通知。 |

恢复规则：

- Worker重启后必须先向服务端确认状态。
- `sending` 超时进入 `unknown_send_result`，不自动补发。
- 客户新消息到来时，未发送的旧AI回复必须作废并重新生成。

## 9. 模块影响

| 模块 | 调整 |
|---|---|
| AI对话 | 取消轮次上限；AI持续接待直到转人工、拒绝、关闭或风控禁止。 |
| 自动召回 | 从观望召回升级为等待用户回复召回；支持多轮，次数和间隔配置化。 |
| 人工接管 | 转人工后进入等待销售回复；销售超时未回由服务端飞书通知。 |
| Worker | 只监听系统绑定会话；采集事实，不做业务状态判断。 |
| 服务端 | 维护完整状态机；负责召回到期和销售超时扫描。 |
| 飞书 | 只做通知和错误日志，不做按钮和复杂权限。 |

## 10. 新验收重点

| 编号 | 用例 | 通过标准 |
|---|---|---|
| S-01 | AI连续多轮接待 | 客户持续提问时，AI可持续回复，不因固定轮次停止。 |
| S-02 | AI回复后等待用户 | AI发送成功后状态进入 `waiting_user_reply`。 |
| S-03 | 人工回复后等待用户 | 销售人工回复被Worker识别后，状态进入 `human_replied_waiting_user`。 |
| S-04 | 等待用户超时召回 | 超过N天客户未回复，服务端生成 `follow_up_task`，Worker发送召回。 |
| S-05 | 多轮召回 | 召回后客户仍未回复，到下一周期可再次召回。 |
| S-06 | 用户回复终止召回 | 客户回复后，召回条件失效。 |
| S-07 | 用户拒绝停止自动动作 | 客户拒绝后，不回复、不召回、不加好友。 |
| S-08 | 转人工等待销售 | AI转人工后进入 `waiting_human_reply`，AI不再自由回复。 |
| S-09 | 销售超时飞书通知 | 销售超过N天未回复，服务端发送飞书通知并记录结果。 |
| S-10 | Worker与服务端扫描分工 | Worker上报微信事实；服务端根据数据库状态生成召回和销售提醒。 |
