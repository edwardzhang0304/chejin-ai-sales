# AI智能客服售前跟进系统 技术方案

版本：v0.6

日期：2026-06-22

当前阶段：运营后台 + Windows Worker 客户端 + V16 系列 C2 状态机定向读取修复；当前 Windows 实机验证包：V16.18

一句话结论：当前技术方案统一收口到本文档；add_friend、C2 会话绑定/微信监听、Worker/服务端接口、状态机、错误码和验收口径都在正文中维护，不再另设当前有效专项技术文档。

---

# 第一部分：变更列表

| 日期 | 版本 | 原来是什么 | 变更成什么 |
|---|---|---|---|
| 2026-06-04 | v0.1 | 技术蓝图分散在多份历史方案中 | 整理为正式工程技术方案，覆盖线索、销售、任务、Worker、AI、召回等完整蓝图 |
| 2026-06-11 | v0.2 | Mac Worker / 人工传值原型 | 改为商家侧 Windows Worker 单应用，OmniAuto 作为内置 RPA Sidecar |
| 2026-06-20 | v0.3 | add_friend 仍在独立集成说明中 | 收口为 Worker 内置 OmniAuto add_friend 能力，V15.3 作为当前客户端验收基线 |
| 2026-06-22 | v0.3.1 | C2 会话绑定/监听独立成专项文档 | C2 接口、状态、错误码和验收口径并入主技术方案模块5 |
| 2026-06-22 | v0.4 | 技术方案沿用 v2.x 编号，且当前有效文档里仍有专项方案入口 | 版本与 PRD v0.4 对齐，结构改为“变更列表 + 正文技术方案”，专项技术文档退出当前有效入口 |
| 2026-06-22 | v0.4内部修订 | Local WeChat UI Lock 只有原则要求，缺少可开发的工程设计 | 补充 Worker 本地锁、服务端任务租约、续租、释放、超时恢复、错误码、优先级和各动作接入方式；同步 V15 add_friend 正式入口 |
| 2026-06-23 | v0.4内部修订 | OmniAuto 容易被理解为仅用于 Worker RPA | 明确 OmniAuto 分为服务端 AI Engine 复用和 Worker 端 RPA Sidecar 复用；服务端 AI 大脑包含上下文、知识库/RAG、Evidence Pack、AI编排、Guard、ReplyAction |
| 2026-06-23 | v0.4内部修订 | C3 只有概要链路，缺少可开发的接口、状态、幂等和错误码 | 补充 C3 `message_batch / reply_action / chat_reply / sent_ack / handoff_event` 技术细化、状态机、接口、错误码、防重复发送和 OmniAuto AI Engine 接入边界 |
| 2026-06-23 | v0.4内部修订 | Worker、C2、C3 存在同义字段和过渡字段，容易造成前后端各自兜底 | 收口正式字段口径：废弃 `runtime_status/current_task_id/client_bind_status/status_flow/executor_status/lead_status/reason_code/ingest_status`；会话业务状态归入 `Conversation`，微信绑定表只负责绑定 |
| 2026-06-23 | v0.4内部修订 | C2 只写了扫描兜底概念，缺少触发时机、优先级、中断和扫描边界 | 曾补充 C2 主监听扫描兜底规则；该口径已在 v0.6 废弃 |
| 2026-06-23 | v0.4内部修订 | C2 主动扫描和消息去重只有原则描述，缺少扫描循环、读目标选择、dedupe_key生成和事务入库规则 | 补充主动扫描工程规则、服务端 read-targets 选择规则、消息去重键生成、幂等入库事务和异常处理 |
| 2026-06-23 | v0.4内部修订 | C2 写了 OmniAuto `sessions/messages`，但没有直白说明它属于 Worker 端 RPA Sidecar 能力 | 明确主动扫描、消息读取均由 Worker 调用 OmniAuto RPA Sidecar 执行，不走加好友 action，不运行本地 AI 客服闭环 |
| 2026-06-23 | v0.5 | C2 曾尝试用滚动兜底非第一屏消息，但当前 OmniAuto `sessions` 只支持当前可见区扫描，且 C3 已推进到开发完成阶段 | 该方案已在 v0.6 废弃，不进入当前开发和验收 |
| 2026-06-24 | v0.6 | C2 曾试图用滚动兜底非第一屏消息，但微信会话列表动态排序，无法保证不漏扫，也不符合销售先看第一屏的习惯 | V16 系列主链路调整为“第一屏主动扫描 + 第一屏命中优先读取 + 状态机驱动定向读取 + 召回前 precheck + 去重入库”；旧滚动兜底方案不保留为当前方案 |
| 2026-06-30 | v0.6内部修订 | “状态机定向读取”只写了目标和字段，未明确非第一屏会话的 RPA 定位方式，容易被误实现为仅靠第一屏 `rpa_session_key` 查找 | 明确定向读取分两类：第一屏可见目标可用 `rpa_session_key/display_name` 快速读取；非第一屏目标必须通过微信搜索框搜索 `remark_code`，二次确认短码后才允许读取；`rpa_session_key` 降级为第一屏辅助证据 |
| 2026-07-01 | v0.6内部修订 | 技术方案中写死 V16.2，无法反映 V16 系列持续修复和 V16.18 Windows 实机回归状态 | 改为 V16 系列 C2 状态机定向读取修复；当前 Windows 实机验证包为 V16.18 |

---

# 第二部分：正文技术方案

## 当前项目口径

| 事项 | 当前口径 |
|---|---|
| 抖音线索获取 | 本期先人工导入 |
| 抖音开发者开放平台 | 已审核通过 |
| 抖音 API 自动接入 | 下一期再做 |
| 企业私信 Webhook/OAuth | 下一期再做 |
| 小风车/巨量引擎自动同步 | 下一期再做 |
| 本期线索重点 | 人工录入/导入、字段映射、去重校验、导入结果反馈、线索分配和跟进 |

## 总体架构口径

说明：本版根据最新业务沟通重构会话状态机：取消 AI 固定轮次限制，改为“AI持续接待 / 转人工 / 等待用户回复 / 召回 / 等待销售回复超时提醒”的长期跟进模型；同时将微信备注短码定义为系统托管开关。

## 1. 核心变化

| 事项 | 旧口径 | 新口径 |
|---|---|---|
| AI回复轮次 | 有固定轮次限制，达到上限后进入观望。 | 取消轮次上限。只要未拒绝、未关闭、未转人工且风控允许，客户继续聊，AI继续回复。 |
| 等待用户回复 | 主要用于观望客户召回。 | AI回复、人工回复、召回回复后，都进入等待用户回复状态。 |
| 自动召回 | 只覆盖观望客户，每客户最多一次。 | 覆盖所有等待用户回复的会话；超过N天未回复可再次召回，直到用户回复、拒绝或关闭。 |
| 转人工后销售未回复 | 原方案不做二次提醒。 | 进入等待销售回复状态；超过N天销售未回复，飞书通知销售。 |
| Worker职责 | 容易被理解为扫所有会话。 | Worker只采集系统绑定会话的微信事实；服务端负责状态机和超时判断。 |
| 备注短码 | 只作为绑定识别手段。 | 备注短码成为系统托管开关：移除短码=销售接手并关闭自动跟进；线下好友补上短码=进入系统跟进。 |

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

## 技术栈与工程实现约束

本期按正式工程产品实施，不按演示 Demo 实施。技术栈选择遵循：优先复用 OmniAuto 现有 Python 能力、减少跨语言链路、保证任务可恢复、错误可定位、部署可复制、二期可演进。

| 层级 | 选型 | 说明 |
|---|---|---|
| 服务端语言/框架 | Python 3.11+ / FastAPI | 与 OmniAuto、AI 编排、RPA 生态一致；接口清晰，便于快速工程化。 |
| ORM/迁移 | SQLAlchemy 2.x / Alembic | 数据模型、状态机、任务表必须可迁移、可回滚、可审计。 |
| 主数据库 | PostgreSQL 15+ | 作为线索、任务、会话、状态机、错误码、审计日志的唯一事实源。 |
| 任务调度 | PostgreSQL 任务表 + lease 锁 + APScheduler/服务端扫描进程 | 第一阶段不引入复杂 MQ；任务状态持久化，服务重启后可恢复，避免重复发送。 |
| 控制面前端 | React + TypeScript + Vite + Ant Design | 适合后台管理、表格、表单、配置、日志和任务看板。 |
| Worker 桌面端 | Windows Worker 单应用 / Python 3.11+ / PySide6 / OmniAuto RPA Sidecar / wxauto4备用 / PyInstaller | 对商家交付一个 Worker 应用；工程内部拆为 Worker 主进程 + OmniAuto RPA Sidecar 子进程。Worker 负责任务领取、调度、展示和上报；Sidecar 负责微信窗口探测、OCR、输入、点击、截图取证和微信异常识别。wxauto4 只作为技术备用，不作为默认路径。 |
| Worker 本地存储 | SQLite + 本地文件目录 | 保存本地配置、运行日志、图片临时文件、未确认发送记录；服务端仍是最终事实源。 |
| 通信方式 | HTTPS REST + Worker 主动轮询/心跳 | 商家电脑不暴露公网端口；Worker 主动拉任务、上报事实和心跳。 |
| AI 文本 | OmniAuto AI Engine + DeepSeek API | OmniAuto 负责上下文、RAG、Guard、编排；DeepSeek 负责文本生成。 |
| 图片理解 | 千问视觉模型 + ImageIntent | 识别图片内容、截图信息、车型线索和置信度；低置信转人工。 |
| 知识检索 | OmniAuto RAG + 关键词/规则混合检索 | 先基于现有能力工程化；Dify/FastGPT 仅预留 Adapter，不作为第一期核心运行时。 |
| 车源索引 | 大风车 API + 本地 vehicle_index | 外部接口同步原始车源，AI 只读取白名单字段。 |
| 文件存储 | Worker 本地图片目录 + 服务端必要文件存储 | 不做长期图片库；服务端仅保存必要文件、识别结果和证据。 |
| 日志与错误码 | JSON 结构化日志 + error_code 字典 + trace_id | 控制面、Worker、服务端日志使用同一错误码体系，便于运维排障。 |
| 部署 | Docker Compose 部署服务端；Worker 以 Windows 安装包/可执行文件交付 | 第一阶段不使用 Kubernetes；保证单机可复制部署和备份恢复。 |
| 测试 | pytest + 接口集成测试 + Worker 端到端录屏/日志验收 | 核心验收看状态机、幂等、防重复发送、错误码、微信串行操作。 |

明确不采用：

- 不把 Dify 作为第一期核心对话运行时，只预留后续 Adapter。
- 不使用 Kubernetes、服务网格、复杂微服务拆分。
- 不在第一期做多商户 SaaS、复杂权限、计费和高可用集群。
- 不读取或破解微信数据库，不使用非公开微信协议。
- 不做 Mac Worker 正式版本；此前 Mac 相关页面或人工传值流程仅作为原型/调试参考，不作为正式业务主链路。
- 不把 OmniAuto 整体不加边界地揉进车金业务主程序；OmniAuto 按两类能力复用：服务端复用 AI Engine、RAG、Evidence Pack、Guard、回复编排能力，Worker 端复用本地 RPA Sidecar 操作微信。

## 状态字段统一口径

本节为后端、Worker 客户端、运营后台和测试的字段准入标准。后续实现不得新增同义字段、兜底字段或临时状态字段；确需新增字段必须先更新本文档。

### 正式字段

| 领域 | 正式字段 | 含义 |
|---|---|---|
| Worker | `online_status` | Worker 是否在线，由服务端根据心跳计算。 |
| Worker | `run_status` | Worker 是否接单，固定为 `running / paused`。 |
| Worker | `running_status` | Worker 是否正在执行本地任务，固定为 `idle / running`。 |
| Worker | `rpa_component_status` | OmniAuto RPA Sidecar 是否可用，固定为 `ready / unavailable`。 |
| Worker | `wechat_status` | 微信桌面客户端状态，固定为 `logged_in / not_found / logged_out / unknown`。 |
| Worker | `client_binding_state` | Worker 客户端绑定状态，固定为 `unbound / bound / reset_required`。 |
| Worker | `current_task` | 当前执行中的任务 ID；字段名保持 `current_task`，语义就是 task id。 |
| Worker | `current_step` | 当前执行步骤，用于运营后台展示和排障。 |
| Task | `status` | 任务生命周期，固定为 `blocked / pending / running / completed / failed / cancelled`。 |
| Task | `result_code` | 任务成功后的业务结果，例如 `invite_sent / chat_reply_sent / follow_up_sent`。 |
| Task | `error_code` | 失败原因。 |
| Task | `block_code` | 阻塞原因。 |
| Task | `events` | 任务事件流，运营后台只读取 `events`。 |
| Conversation | `status` | 客户/会话业务状态。 |
| Conversation | `ai_enabled` | 该会话是否允许 AI 自主回复。 |
| Conversation | `reply_count` | AI 已成功发送次数。 |
| Conversation | `handoff_reason_code` | 转人工原因码。 |
| Conversation | `handoff_at` | 进入等待销售回复的时间。 |
| WechatSessionBinding | `bind_status` | 微信会话与线索的绑定状态。 |
| WechatSessionBinding | `listen_status` | 微信会话监听/读取状态。 |
| WechatSessionBinding | `allow_listening` | 是否允许 Worker 读取该会话消息。 |
| WechatSessionBinding | `error_code` | 绑定、监听或读取失败原因。 |
| MessageEvent | `dedupe_key` | 消息去重键。 |
| MessageBatch | `status` | C3 消息批次状态。 |
| ReplyAction | `status` | C3 回复动作状态。 |
| SentAck | `send_result` | Worker 发送结果，固定为 `sent / failed / unknown`。 |
| HandoffEvent | `status` | 转人工事件状态。 |
| HandoffEvent | `handoff_reason_code` | 转人工原因码。 |

### 废弃字段

以下字段不得作为新接口、新表结构或前端类型继续使用。

| 废弃字段 | 替代字段 | 说明 |
|---|---|---|
| `runtime_status` | `running_status` | Worker 运行态只保留一个名称。 |
| `current_task_id` | `current_task` | `current_task` 的语义就是当前任务 ID。 |
| `client_bind_status` | `client_binding_state` | 客户端绑定状态只保留一个字段。 |
| `status_flow` | `events` | 任务事件流统一使用 `events`。 |
| `executor_status` | `execution.worker` | 执行方状态由 `execution.worker` 计算展示。 |
| `lead_status` | `business_object.lead.status` | 任务详情里的线索状态从业务对象读取。 |
| `reason_code` | `error_code` 或 `handoff_reason_code` | C2 绑定/监听失败统一用 `error_code`；转人工原因用 `handoff_reason_code`。 |
| `message_event.ingest_status` | 接口响应 `results[].ingest_result` | 消息表只保存已入库事实；重复、忽略等属于本次接口处理结果。 |

## 3. 会话主状态

| 状态 | 含义 | 当前等待谁 | 允许动作 |
|---|---|---|---|
| `new` | 线索刚进入系统。 | 系统分配 | 分配销售、绑定Worker。 |
| `assigned` | 已分配销售；Worker可能已绑定，也可能未绑定。 | 系统生成任务 | 若销售已绑定Worker，创建可执行加好友任务；若未绑定Worker，创建阻塞任务。 |
| `add_friend_blocked` | 加好友任务已创建但不可执行。 | 销售/运营绑定Worker | 仅允许绑定Worker后解除阻塞，不允许Worker领取。 |
| `add_friend_pending` | 待加好友。 | Worker执行 | 手机号搜索、发送添加通讯录邀请、写初始备注。 |
| `add_friend_sent` | 已发送添加通讯录邀请，不代表客户已同意。 | Worker扫描会话列表识别新会话 | 等待后续会话绑定。 |
| `friend_added` | Worker从会话列表识别到新会话，或发现已是好友。 | Worker绑定会话 | 绑定微信会话，进入AI接待。 |
| `ai_active` | AI正常接待。 | 客户/AI | 客户来消息后AI可持续回复，不设轮次上限。 |
| `waiting_user_reply` | 我方已经回复，等待客户回。 | 客户 | 服务端到期生成召回任务；Worker监听客户是否回复。 |
| `recall_precheck` | 召回前读取确认中。 | Worker读取微信事实 / 服务端复核 | 不发送召回；确认无新客户消息后才允许创建 `follow_up`。 |
| `recalled_waiting_user` | AI已发过召回，继续等待客户回。 | 客户 | 到下一轮召回周期后可再次召回。 |
| `waiting_sales_reply` | AI已转人工，等待销售回复客户。 | 销售 | 超过N天销售未回，服务端飞书通知销售。 |
| `sales_replied_waiting_user` | 销售已回复，等待客户回。 | 客户 | 到期可由AI发送召回内容。 |
| `rejected` | 客户明确拒绝或黑名单。 | 无 | 不加好友、不回复、不召回。 |
| `closed` | 系统停止自动跟进。常见原因包括销售移除备注短码、销售线下接手、人工确认结束。 | 无 | 不自动回复、不召回、不飞书提醒、不主动关注。 |

## 4. 状态流转规则

### 4.0 销售分配与Worker绑定规则

线索轮询分配到销售后，销售归属成立，不因该销售暂未绑定Worker而回滚为分配失败。

固定规则：

```text
销售分配成功 = lead.status=assigned, lead.sales_id=目标销售
自动化执行可用 = 存在可用worker_id
```

若销售已绑定Worker：

```text
创建add_friend任务
task.status=pending
task.worker_id=已绑定Worker
conversation.status=add_friend_pending
```

若销售未绑定Worker：

```text
创建add_friend任务
task.status=blocked
task.block_code=SALES_WORKER_NOT_BOUND
task.block_reason=销售未绑定Worker，无法自动加好友
conversation.status=add_friend_blocked
```

Worker只领取 `pending` 任务，不领取 `blocked` 任务。

销售后续绑定Worker时，服务端必须自动恢复该销售名下被 `SALES_WORKER_NOT_BOUND` 阻塞的 `add_friend` 任务：

```text
task.status=blocked -> pending
task.worker_id=新绑定Worker
task.block_code=null
task.block_reason=null
conversation.status=add_friend_pending
```

这不是重新创建任务，而是恢复已有阻塞任务，避免重复加好友任务。

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
销售人工回复成功 -> sales_replied_waiting_user
```

如果客户回复：

```text
未转人工会话 -> ai_active
已转人工会话 -> waiting_sales_reply 或保持人工负责，由销售继续处理
```

如果客户明确拒绝：

```text
任意状态 -> rejected
```

### 4.3 自动召回

召回不再只属于观望客户，而属于“等待用户回复”类状态。

触发条件：

```text
status in (waiting_user_reply, recalled_waiting_user, sales_replied_waiting_user)
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
服务端生成 recall_precheck read-target
Worker定向读取该会话最新消息
若读到客户新消息，则取消本轮召回并进入AI回复或转人工链路
若确认无客户新消息，服务端才创建follow_up任务
Worker领取任务并发送AI召回内容
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
客户高意向/高风险/模型失败/图片低置信/车源证据不足 -> waiting_sales_reply
```

转人工后：

- AI不再自由回复客户新消息。
- Worker继续监听该绑定会话，识别销售手机端同步到桌面端的人工回复。
- 销售回复后，状态进入 `sales_replied_waiting_user`。

销售超时未回复：

```text
status=waiting_sales_reply
且 now - conversation.handoff_at >= N天
且 sales_first_reply_at为空
-> 服务端触发飞书通知销售
```

飞书口径：

- 只做通知。
- 不做按钮。
- 不做二次发送功能。
- 不单独增加角色权限。
- 失败时记录错误日志，由项目方人工查看处理。

### 4.5 备注短码作为系统托管开关

第一期不额外给销售做“关闭客户”的后台工具。销售在微信里工作的事实优先，备注短码作为最轻量的控制入口。

规则：

```text
微信备注仍包含系统短码 -> 系统继续托管该会话
微信备注不再包含系统短码 -> 视为销售已接手/不需要系统继续自动跟进 -> status=closed
线下好友备注新增系统短码 -> Worker识别并绑定 -> 进入系统跟进
```

进入 `closed` 后：

- AI 不再自动回复。
- 不再生成召回任务。
- 不再触发销售超时飞书提醒。
- Worker 不再主动关注该会话。
- 保留关闭原因：`close_reason=remark_code_removed`。

注意：备注短码消失不代表客户拒绝，所以不进入 `rejected`。它只代表系统自动化退出，由销售自行跟进。

## 5. 备注短码工具与线下线索接入

### 5.1 工具目标

解决销售线下获得客户、自己添加微信好友后，系统无法启动跟进的问题。

工具只做一件事：

```text
生成系统可识别的备注短码和推荐备注名
```

### 5.2 使用流程

```text
销售线下获得新客户 -> 销售自己加微信好友 -> 在备注短码工具生成新短码 -> 销售把微信好友备注改成包含短码 -> Worker第一屏主动扫描或短码定向搜索识别短码 -> 服务端创建/绑定线索和会话 -> 进入ai_active或waiting_user_reply
```

### 5.3 短码工具输入与输出

| 项目 | 说明 |
|---|---|
| 输入 | 销售、手机号或手机号后4位、来源、可选客户备注、可选车型/需求。 |
| 输出 | `remark_code`、推荐微信备注名、线索记录、绑定状态。 |
| 推荐备注格式 | `CJ-销售简称-短码-手机号后4位`，具体命名规则可配置。 |
| 唯一性 | `remark_code` 全局唯一，不能重复分配给多个有效客户；同一客户恢复跟进时优先找回原短码，不生成新短码。 |
| 有效性 | 新短码生成后状态为待绑定；Worker识别到微信备注包含短码后完成绑定。 |

### 5.4 短码工具模式

| 模式 | 使用场景 | 处理规则 |
|---|---|---|
| 生成新短码 | 销售线下获得的新客户，系统里没有有效线索或会话。 | 创建新Lead/RemarkCode，等待Worker识别绑定。 |
| 找回原短码 | 销售误删备注短码，或短码被移除后又希望AI恢复跟进。 | 按销售、手机号后4位、客户备注等查询原Lead/Conversation，复制原推荐备注，不创建新客户。 |
| 作废短码 | 绑定错客户、短码泄露、重复绑定等异常。 | 管理员或项目负责人处理；作废后原短码不可再绑定新客户，必要时人工合并或迁移历史。 |

恢复原则：

```text
同一个客户恢复系统跟进，优先使用原remark_code。
不要重新生成新短码，避免历史对话、AI/人工记录、召回次数和接管记录断裂。
```

原短码恢复后，服务端可继续读取原 `conversation_id` 下的历史记录，包括：

- AI回复记录。
- 销售人工回复记录。
- 召回记录。
- 转人工记录。
- 风控与Guard记录。

恢复后的状态由服务端根据上下文判断：

| 恢复条件 | 恢复后建议状态 |
|---|---|
| 客户刚发了新消息 | `ai_active` 或按风控转人工。 |
| 我方最后发过消息且客户未回复 | `waiting_user_reply`。 |
| 原会话处于转人工链路 | `waiting_sales_reply`，除非人工明确恢复AI。 |
| 客户已 `rejected` | 不自动恢复，需人工确认。 |

### 5.5 Worker识别规则

- Worker只扫描系统要求关注的候选会话，不扫描全量微信。
- 识别到备注包含有效短码后，上报 `remark_code_detected`。
- 服务端根据短码绑定 Lead / Conversation / Sales / Worker。
- 短码绑定成功后，后续消息进入正常 AI 跟进链路。
- 已绑定会话后续发现短码被移除，上报 `remark_code_removed`，服务端进入 `closed`，`close_reason=remark_code_removed`。
- `closed` 后如果原短码再次出现在该销售微信好友备注中，Worker上报 `remark_code_detected`，服务端按原Lead/Conversation恢复系统跟进。

## 6. Worker与服务端定时扫描分工

| 扫描类型 | 谁做 | 扫什么 | 结果 |
|---|---|---|---|
| 微信事实监听 | Worker | 系统绑定会话的新消息、图片、销售人工回复、微信异常。 | 上报 message_event / image_event / sales_reply_event / wechat_error。 |
| 微信第一屏主动扫描 | Worker | 当前第一屏会话、未读/红点/预览变化、短码候选。 | 符合销售先看最新消息的习惯，优先发现眼前新消息。 |
| 状态机定向读取 | Worker + 服务端 | 已绑定且处于 waiting_user_reply、recent_ai_sent、waiting_sales_reply、recall_precheck 等状态的会话。 | 空闲时按服务端目标定向读取，补齐非第一屏已知客户。 |
| 召回到期扫描 | 服务端 | 数据库中的等待用户回复类状态。 | 先生成 `recall_precheck` 读取目标；确认无新客户消息后才创建 `follow_up` 任务。 |
| 销售超时扫描 | 服务端 | `waiting_sales_reply` 且销售未回复的会话。 | 发送飞书通知销售。 |
| Worker健康扫描 | 服务端 | Worker heartbeat、last_sync_at、当前任务。 | 标记离线、卡住、异常。 |
| 发送结果恢复 | Worker + 服务端 | `sending`、`unknown_send_result`、超时任务。 | 防止重复发送，必要时人工确认。 |

结论：

```text
Worker定时扫微信事实。
服务端定时扫数据库状态。
Worker不判断业务规则。
服务端不直接操作微信。
```

### 6.1 微信UI操作串行约束

所有模拟人操作微信桌面端的动作必须全局串行，统一通过 `Local WeChat UI Lock` 控制。不得并行操作微信窗口。

必须串行的动作包括：

- 手机号搜索、点击加好友、填写好友申请语、写备注。
- 打开/切换微信会话、滚动聊天记录、读取需要切换窗口才能确认的信息。
- 点开图片、另存图片。
- 输入AI回复、发送AI回复、发送召回文案。
- 通过备注短码绑定会话、确认短码移除、确认发送结果。

允许并行的动作仅限非微信UI逻辑：

- 等待AI生成。
- RAG检索、车源索引查询、大风车同步。
- 图片上传和视觉识别。
- 服务端状态机判断、定时扫描数据库。
- 飞书通知。
- 日志写入、任务排队、错误记录。

执行规则：

```text
任何任务只要需要操作微信桌面端，
必须先获取 Local WeChat UI Lock，
执行完成或失败后释放锁，
下一个微信UI任务才能继续。
```

任务优先级可以配置，但不改变串行原则：

```text
chat_reply > add_friend > follow_up
```

## 7. 核心数据字段

| 字段 | 含义 | 来源 |
|---|---|---|
| `last_inbound_at` | 最近客户消息时间。 | Worker上报客户消息。 |
| `last_outbound_at` | 最近我方发出消息时间。 | Worker上报AI/召回/人工发送事实。 |
| `last_ai_reply_at` | 最近AI回复时间。 | Worker发送AI成功后上报。 |
| `last_recall_at` | 最近召回时间。 | Worker发送召回成功后上报。 |
| `last_sales_reply_at` | 最近销售人工回复时间。 | Worker识别销售人工消息后上报。 |
| `sales_first_reply_at` | 转人工后销售首次回复时间。 | Worker识别销售人工消息后上报。 |
| `handoff_at` | Conversation 进入等待销售回复时间。 | 服务端转人工时写入。 |
| `recall_count` | 已召回次数。 | 服务端根据sent_ack更新。 |
| `ai_enabled` | Conversation 是否允许AI自由接待。 | 服务端状态机维护。 |
| `owner` | 当前会话责任方：ai / sales。 | 服务端状态机维护。 |
| `remark_code` | 系统识别微信会话的短码。 | 服务端生成，Worker从备注中识别。 |
| `close_reason` | 关闭自动跟进原因。 | 备注短码移除、人工关闭、其他。 |

## 8. 任务类型

| 任务 | 触发方 | 执行方 | 说明 |
|---|---|---|---|
| `add_friend` | 服务端 | Worker | 手机号搜索、发送添加通讯录邀请、写初始备注；销售未绑定Worker时任务先进入 `blocked`，绑定Worker后恢复为 `pending`。 |
| `chat_reply` | 服务端 | Worker | 执行AI回复动作。 |
| `follow_up` | 服务端 | Worker | 发送AI召回内容。 |
| `save_image` | Worker | Worker | 点开并另存客户图片。 |
| `handoff_notify` | 服务端 | 服务端 | 飞书通知销售；不需要Worker操作微信。 |
| `generate_remark_code` | 服务端/控制面 | 服务端 | 为线下好友生成系统短码和推荐备注名。 |
| `recover_remark_code` | 服务端/控制面 | 服务端 | 查询并复制原短码，用于误删备注或恢复系统跟进。 |
| `bind_by_remark_code` | Worker上报 | 服务端 | Worker识别备注短码后绑定线索和会话。 |

### 8.1 统一任务中心状态口径

`Task.status` 只表示任务执行生命周期，不承载具体业务结果。`invite_sent`、`chat_reply_sent`、`follow_up_sent` 这类含义必须写入 `Task.result_code`，再由服务端状态机映射为 `Conversation.status`。

固定状态集合：

```text
Task.status = blocked / pending / running / completed / failed / cancelled
```

固定字段职责：

| 字段 | 职责 | 示例 |
|---|---|---|
| `task.status` | 任务执行生命周期 | blocked、pending、running、completed、failed、cancelled |
| `task.result_code` | 任务完成后的业务结果 | invite_sent、already_friend、chat_reply_sent、follow_up_sent、skipped_by_rule |
| `task.error_code` | 任务失败原因 | WECHAT_WINDOW_NOT_FOUND、PHONE_NOT_FOUND、WORKER_INTERRUPTED |
| `task.block_code` | 任务阻塞原因 | SALES_WORKER_NOT_BOUND、DAILY_LIMIT_REACHED |
| `conversation.status` | 客户/会话业务生命周期 | add_friend_sent、waiting_user_reply、recalled_waiting_user |

核心映射规则：

| 任务结果 | 会话状态更新 |
|---|---|
| `task_type=add_friend` 且 `task.status=completed` 且 `result_code=invite_sent` | `conversation.status=add_friend_sent` |
| `task_type=add_friend` 且 `task.status=completed` 且 `result_code=already_friend` | `conversation.status=friend_added`，并立即尝试绑定会话 |
| `task_type=chat_reply` 且 `task.status=completed` 且 `result_code=chat_reply_sent` | `conversation.status=waiting_user_reply` |
| `task_type=follow_up` 且 `task.status=completed` 且 `result_code=follow_up_sent` | `conversation.status=recalled_waiting_user` |
| `task_type=add_friend` 且 `task.status=blocked` 且 `block_code=SALES_WORKER_NOT_BOUND` | `conversation.status=add_friend_blocked` |
| `task.status=failed` | 不直接等于业务终态，由服务端按 `error_code` 判断可重试、转人工、暂停或拒绝 |

工程约束：

- `invite_sent` 不允许作为 `task.status`。
- `already_friend` 不允许作为失败状态；它表示任务已完成，结果是不需要发送好友申请。
- `Conversation.status` 不允许用于 Worker 领取任务；Worker 只能按 `Task.status=pending` 领取任务。
- `Task.status=completed/failed/cancelled` 为任务终态；终态任务不得被 Worker 再次领取。
- `blocked` 恢复为 `pending` 时必须复用原任务，不得新建重复任务。

## 9. 幂等与防重复

| 对象 | 约束 |
|---|---|
| `add_friend_task` | 同一有效线索同一时间只能存在一个未完成 `add_friend` 任务；`blocked` 恢复为 `pending` 时不得新建重复任务。 |
| `message_event` | `unique(worker_id, conversation_id, dedupe_key)` |
| `message_batch` | 同一 `conversation_id` 同一时间最多一个 active batch。 |
| `reply_action` | Worker只能执行当前有效 `reply_action_id`。 |
| `sent_ack` | `unique(reply_action_id)`，同一动作只能确认一次。 |
| `follow_up` | 召回发送任务按 `conversation_id + recall_round + rule_id` 去重，且只能在 `recall_precheck` 确认无新客户消息后创建。 |
| `handoff_notify` | 同一销售超时提醒周期只发送一次飞书通知。 |
| `remark_code` | 全局唯一；同一短码只能绑定一个有效会话。 |
| `recover_remark_code` | 恢复跟进必须优先使用原短码；不得为同一客户直接生成第二个有效短码。 |

恢复规则：

- Worker重启后必须先向服务端确认状态。
- `sending` 超时进入 `unknown_send_result`，不自动补发。
- 客户新消息到来时，未发送的旧AI回复必须作废并重新生成。

## 10. 错误码与运维可解释性原则

所有报错、失败、跳过、暂停、异常恢复都必须有错误码和错误码解释说明，便于后续运维、排障和验收复盘。

错误记录至少包含：

| 字段 | 说明 |
|---|---|
| `error_code` | 机器可识别错误码，稳定不随文案变化。 |
| `error_message` | 面向运维/实施人员的简短说明。 |
| `error_detail` | 原始异常、第三方返回、窗口标题、截图路径等补充信息。 |
| `severity` | S1/S2/S3/S4 或 info/warn/error/blocker。 |
| `module` | 出错模块，如 Worker、WeChat、AI、Vision、大风车、飞书、RAG、Guard。 |
| `suggested_action` | 建议处理方式，如重试、人工确认、检查登录、检查API Key、联系接口方。 |
| `trace_id` | 关联任务、会话、消息、reply_action、follow_up、handoff_event的追踪ID。 |

错误码命名建议：

```text
WECHAT_LOGIN_EXPIRED
WECHAT_UI_ELEMENT_NOT_FOUND
WECHAT_RATE_LIMIT
WORKER_UI_LOCK_TIMEOUT
AI_MODEL_TIMEOUT
VISION_LOW_CONFIDENCE
DFC_AUTH_FAILED
FEISHU_NOTIFY_FAILED
RAG_NO_EVIDENCE
REMARK_CODE_CONFLICT
REMARK_CODE_REMOVED
SEND_RESULT_UNKNOWN
```

要求：

- 控制面、Worker执行台、日志审计、验收问题单中展示同一错误码。
- 错误码必须有说明文档，不允许只展示“未知错误”。
- 第三方接口原始错误可以保留，但必须映射为系统内部错误码。
- 能恢复的错误给出建议动作；不能自动恢复的错误必须提示人工处理。

## 11. 模块影响

| 模块 | 调整 |
|---|---|
| AI对话 | 取消轮次上限；AI持续接待直到转人工、拒绝、关闭或风控禁止。 |
| 自动召回 | 从观望召回升级为等待用户回复召回；支持多轮，次数和间隔配置化。 |
| 人工接管 | 转人工后进入等待销售回复；销售超时未回由服务端飞书通知。 |
| Worker | 只监听系统绑定会话；采集事实，不做业务状态判断。 |
| Worker/RPA串行化 | 所有微信桌面端模拟人操作必须通过Local WeChat UI Lock串行执行；非UI任务才允许并行。 |
| 服务端 | 维护完整状态机；负责召回到期和销售超时扫描。 |
| 飞书 | 只做通知和错误日志，不做按钮和复杂权限。 |
| 备注短码工具 | 支持线下好友进入系统；支持销售通过移除短码让系统停止自动跟进。 |
| 错误码体系 | 所有异常必须有错误码、解释、建议动作和trace_id，方便运维排障。 |

## 12. 新验收重点

| 编号 | 用例 | 通过标准 |
|---|---|---|
| S-01 | AI连续多轮接待 | 客户持续提问时，AI可持续回复，不因固定轮次停止。 |
| S-02 | AI回复后等待用户 | AI发送成功后状态进入 `waiting_user_reply`。 |
| S-03 | 人工回复后等待用户 | 销售人工回复被Worker识别后，状态进入 `sales_replied_waiting_user`。 |
| S-04 | 等待用户超时召回 | 超过N天客户未回复，服务端先生成 `recall_precheck`；Worker读取确认无新客户消息后，才允许创建并发送 `follow_up`。 |
| S-05 | 多轮召回 | 召回后客户仍未回复，到下一周期可再次召回。 |
| S-06 | 用户回复终止召回 | 客户回复后，召回条件失效。 |
| S-07 | 用户拒绝停止自动动作 | 客户拒绝后，不回复、不召回、不加好友。 |
| S-08 | 转人工等待销售 | AI转人工后进入 `waiting_sales_reply`，AI不再自由回复。 |
| S-09 | 销售超时飞书通知 | 销售超过N天未回复，服务端发送飞书通知并记录结果。 |
| S-10 | Worker与服务端扫描分工 | Worker上报微信事实；服务端根据数据库状态生成召回和销售提醒。 |
| S-11 | 移除备注短码关闭自动跟进 | Worker发现已绑定会话备注短码消失后，服务端进入 `closed`，AI/召回/飞书提醒停止。 |
| S-12 | 线下好友加短码启动跟进 | 销售用短码工具生成备注并改微信备注后，Worker识别短码并绑定会话，系统开始跟进。 |
| S-13 | 找回原短码恢复跟进 | 销售误删短码后，通过短码工具找回原短码并加回微信备注，系统恢复原会话和历史上下文。 |
| S-14 | 微信UI操作串行 | 同时存在多个微信UI任务时，Worker必须按Local WeChat UI Lock串行执行。 |
| S-15 | 错误码可解释 | 任意失败、暂停、跳过、异常都必须展示稳定错误码、错误说明和建议处理动作。 |

## 第二部分：v2.3 全量模块详细设计补充

说明：以下内容来自历史详细设计全量版。凡与第一部分状态机、任务分工、备注短码、销售/Worker绑定和当前项目口径冲突的地方，以第一部分和当前项目口径为准；未冲突部分作为当前完整技术方案的细化要求。

## 2. 模块1：云端业务控制面

### 2.1 模块目标

- 作为业务状态中心、任务调度中心、配置中心和审计中心。
- 统一管理线索、销售、Worker、任务、会话、风控、召回、飞书通知和日志。
- 第一期做轻量后台，由项目方自己控制，不做复杂权限和复杂 CRM。

### 2.2 不做事项

- 不做多商户计费、复杂权限、完整 CRM、复杂 BI、多渠道客服聚合、销售业绩管理、合同订单系统、高可用集群。
- 本方案不覆盖二期 SaaS 化、多商户、复杂权限和计费体系设计；相关内容仅作为未来扩展边界，不作为本次开发、测试和验收范围。
- 不直接操作微信 UI，不保存 Worker 本地临时状态为主状态。

### 2.3 核心子模块与对象

| 子模块/对象 | 说明 |
|---|---|
| Lead | 手机号线索，包含 phone、phone_hash、source、sales_id、worker_id、status、remark_code、last_contact_at、reject_flag、recall_count 等。 |
| Sales | 销售人员，包含 sales_id、sales_name、wechat_account、worker_id、feishu_user_id、enabled、daily_add_friend_limit 等。 |
| Worker | 商家侧 Windows 电脑执行器，包含 worker_id、device_name、platform=windows、online_status、run_status、running_status、rpa_component_status、wechat_status、client_binding_state、current_task、current_step 等。 |
| Task | 统一任务表，task_type 包含 add_friend、chat_reply、follow_up；任务主状态只表达执行生命周期，统一为 blocked、pending、running、completed、failed、cancelled；业务执行结果写入 result_code，例如 invite_sent、already_friend、chat_reply_sent、follow_up_sent；running 内部用 current_step 展示执行步骤；blocked 必须有 block_code 和 block_reason。 |
| WorkerHeartbeat | Worker 客户端心跳流水，记录 worker_id、client_instance_id、run_status、running_status、rpa_component_status、wechat_status、current_task、current_step、local_lock_summary、reported_at。 |
| TaskEvidence | 任务执行证据，记录 task_id、worker_id、evidence_type、url/content/metadata，用于保存 RPA 错误日志、截图 URL、日志片段等。 |
| Conversation | 微信会话业务状态，包含 conversation_id、lead_id、sales_id、worker_id、status、ai_enabled、reply_count、handoff_reason_code、handoff_at、last_inbound_at、last_outbound_at 等。 |
| WechatSessionBinding | 微信会话绑定记录，只负责 conversation_id、lead_id、sales_id、worker_id、remark_code、rpa_session_key、display_name、bind_status、listen_status、allow_listening、error_code 等绑定和监听字段；不承载 AI 业务状态。 |
| MessageBatch | C3 消息批次，按 conversation 聚合一段时间内客户连续消息，作为 AI 大脑的一次输入。 |
| ReplyAction | C3 服务端批准的一次回复动作，包含 reply_action_id、batch_id、reply_text、status、expire_at、guard_result 等；Worker 只能发送 current 且 claim 成功的 ReplyAction。 |
| SentAck | Worker 发送回执，记录 reply_action_id、task_id、send_result、sent_at、evidence 和错误信息；`reply_action_id` 唯一，防重复发送。 |
| RiskPolicy | 风控策略配置。 |
| HandoffEvent | 人工接管事件，记录 handoff_reason_code、通知结果和错误信息。 |

### 2.4 核心流程

```text
线索入库 -> 轮询分配销售 -> 创建add_friend任务 -> 若销售未绑定Worker则blocked -> 后续绑定Worker后恢复pending -> Worker执行 -> task.status=completed且result_code=invite_sent/already_friend等 -> 更新线索/会话业务状态
```

```text
Worker上报客户消息 -> 控制面检查会话和风控 -> AI/RAG/Guard -> 返回send_reply/handoff/no_action/pause/retry_later -> Worker执行 -> 审计
```

```text
等待用户回复类会话到达召回周期 -> 服务端生成recall_precheck读取目标 -> Worker定向读取确认无新客户消息 -> 创建follow_up任务 -> Worker发送固定文案 -> 记录结果
```

### 2.5 已确认决策与待确认

| 事项 | 口径 |
|---|---|
| 登录权限 | 第一期轻量后台，项目方自控，不做复杂权限。 |
| 销售Worker关系 | 一对一绑定，允许换绑，换绑需留痕；销售未绑定Worker时线索分配仍成功，但add_friend任务进入blocked。 |
| 分配策略 | 待定，预留手动分配和轮询分配。 |
| 飞书通知 | 只做飞书，定向销售个人；不做短信。 |
| 召回规则 | 第一期一种规则，周期可配置，默认待定。 |
| 验收重点 | 状态准确、任务可追踪、配置可调整、失败原因可见。 |

## 3. 模块2：Worker任务类型与本地执行台

### 3.1 模块目标与形态

- Worker 部署在商家侧 Windows 电脑，负责看微信、点微信、读消息、存图片、发回复。
- Worker 第一阶段只做 Windows 正式工程版本；Mac Worker 不进入本期正式交付范围。
- 对商家交付形态为一个安装包、一个桌面入口、一个 Worker 客户端；OmniAuto 不作为第二个应用暴露给商家。
- 工程内部采用 `Worker 主进程 + OmniAuto RPA Sidecar 子进程`。Worker 主进程负责 UI、任务领取、任务调度、状态展示、日志上传和错误处理；OmniAuto RPA Sidecar 负责实际操作本机微信桌面客户端。
- Worker 执行台必须呈现为本地可视化窗口，交互效果参照附件视频：微信桌面客户端旁边展示任务步骤、截图证据、AI 结果、运行状态和控制按钮。
- Worker 不保存业务主状态，不直接调用大模型，不持有模型、飞书、大风车密钥。
- Worker 不需要开机自启，通过执行台启动按钮操作。
- Worker RPA 能力优先复用 OmniAuto 仓库的微信 Win32/OCR sidecar、RPA 全局锁、输入/点击节流、截图证据和验收门禁；本项目新增 Worker 任务桥接层、RPA Sidecar 调用协议和 `add_friend` 执行器。`add_friend` 字段契约、结果码和验收口径统一写入本文档模块4，不再另设独立集成方案作为当前有效入口。
- OmniAuto 接入按 checkpoint 分段推进：C1 `add_friend` 已收口到 V14 安装包，V15 新分支适配 OmniAuto 最新更新；C2 做会话绑定和微信监听；C3 做 AI 回复发送；C4 做图片与召回。C2 接口、状态、错误码和验收口径见本文档“模块5：会话绑定与监听”。
- 会话绑定/微信监听不是 `add_friend` 的一部分，也不等同于 `chat_reply` 任务；它是 Worker 运行时扫描和消息事实入库能力。`chat_reply` 只表示服务端已生成并批准回复后，由 Worker 执行发送的任务。
- 人工传值只允许作为调试/兜底能力，不作为正式业务流程；正式主链路必须通过 Worker 调用 OmniAuto RPA Sidecar 自动执行并回传结构化结果。

Worker 与 OmniAuto 的内部调用边界：

```text
服务端AI链路：
车金服务端
  -> OmniAuto AI Engine Adapter
      -> customer_service_brain / RAG / evidence / guard / reply synthesis
      -> DeepSeek API

Worker RPA链路：
车金服务端
  -> Worker 主进程
      -> OmniAuto RPA Sidecar 子进程
          -> 本机销售微信桌面客户端
```

边界说明：

| 边界 | 负责 | 不负责 |
|---|---|---|
| 车金服务端 | 任务创建、状态机、AI编排、知识库/RAG、风控、召回、错误码、审计、控制面展示 | 不直接操作微信，不调用 Worker 本地 RPA Sidecar |
| OmniAuto AI Engine Adapter | 在服务端复用 OmniAuto 的 customer_service_brain、RAG、Evidence Pack、Guard、回复生成/润色等能力 | 不监听微信、不发送微信、不保存车金业务主状态 |
| Worker 主进程 | 心跳、任务领取、任务调度、UI展示、本地串行锁、超时控制、日志/截图上传 | 不写微信点击细节，不决定业务状态 |
| OmniAuto RPA Sidecar | 微信窗口识别、OCR、点击、输入、发送校验、备注、图片另存、截图取证 | 不保存业务主状态，不做线索分配，不判断是否转人工 |
| 微信桌面客户端 | 被 RPA 操作的外部软件 | 不作为系统可信状态源 |

### 3.1.1 Worker 客户端服务端能力

截至 2026-06-11，服务端已补齐 Worker 客户端运行时基础能力。服务端只面对 Worker 主进程，不感知 OmniAuto 内部实现；任务状态仍由服务端统一维护。

已实现能力：

- Worker 客户端绑定：`worker_id + worker_token + client_instance_id`。
- 同一 Worker 只允许绑定一个 `client_instance_id`。
- 后台重置绑定后清空旧客户端绑定、轮换 token；旧 token 或旧 client 后续心跳、任务领取和任务上报会被拒绝。
- 首次绑定后默认 `worker.run_status=paused`，不自动领取任务。
- Worker 状态拆分：
  - `online_status`：由心跳判断。
  - `run_status`：`running / paused`，表示是否接单。
  - `running_status`：`idle / running`，表示 Worker 本地是否正在执行任务；不得使用 `busy / executing` 作为正式值。
  - `rpa_component_status`：`ready / unavailable`。
  - `wechat_status`：`logged_in / not_found / logged_out / unknown`，记录微信桌面客户端是否可控。
  - `client_binding_state`：`unbound / bound / reset_required`，记录 Worker 客户端绑定状态。
  - `current_task`：当前执行中的任务 ID；不得另设 `current_task_id`。
- 心跳写入 `worker_heartbeats`。
- 支持 Worker 开始接单 / 暂停接单。
- 支持 Worker 拉取任务：已有 running 任务时优先返回 running，否则返回可领取 pending。
- Worker 领取任务时校验 online、`run_status=running`、`rpa_component_status=ready`，且同一 Worker 同时只能有一个 running 任务。
- Worker 上报步骤、完成、失败时校验 `X-Worker-Token` 和 `X-Client-Instance-Id`。
- 任务证据上传：错误日志、截图 URL、日志片段等写入 `task_evidences`。
- running 任务不因 Worker 离线自动失败；任务详情中 Worker 离线超过 10 分钟应提示运营介入。

已实现接口：

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/workers/{id}/client-bind` | Worker 客户端绑定 |
| POST | `/api/workers/{id}/reset-client-bind` | 后台重置客户端绑定 |
| POST | `/api/workers/{id}/run-status` | Worker 开始接单 / 暂停接单 |
| GET | `/api/workers/{id}/tasks/pull` | Worker 拉取当前可处理任务 |
| POST | `/api/workers/{id}/heartbeat` | Worker 心跳 |
| POST | `/api/tasks/{id}/claim` | Worker 领取任务 |
| POST | `/api/tasks/{id}/step` | Worker 上报步骤 |
| POST | `/api/tasks/{id}/invite-sent` | Worker 上报已发送邀请 |
| POST | `/api/tasks/{id}/already-friend` | Worker 上报已是好友 |
| POST | `/api/tasks/{id}/fail` | Worker 上报失败 |
| POST | `/api/tasks/{id}/evidences` | Worker 上传执行证据 |

认证规则：

- Worker 客户端调用任务上报接口时，必须携带 `X-Worker-Token` 和 `X-Client-Instance-Id`。
- 带 Worker Header 的请求走 Worker 客户端绑定校验。
- 不带 Worker Header 的任务上报接口保留给运营后台调试 / 兜底能力，不作为正式 Worker 主链路。

数据库迁移：

```text
backend/alembic/versions/20260611_0004_worker_client_runtime.py
```

新增或变更字段 / 表：

- `workers.run_status`
- `workers.rpa_component_status`
- `workers.wechat_status`
- `workers.client_instance_id`
- `workers.bound_at`
- `worker_heartbeats`
- `task_evidences`

自测证据：

| 检查项 | 结果 |
|---|---|
| 编译检查 | 通过 |
| Worker 客户端接口测试 | 通过，覆盖绑定、重置失效、拉取、领取、步骤、完成、证据、领取条件 |
| 相关回归测试 | 通过，`test_worker_client_api.py`、`test_worker_sales_api.py`、`test_tasks_api.py` 共 15 passed |
| 后端全量测试 | 通过，25 passed |
| SQLite 迁移验证 | 通过，空库升级到 `20260611_0004` |
| Docker Postgres 迁移 | 通过，`alembic current` 为 `20260611_0004 (head)` |
| `/readyz` | 通过 |

待确认：

- 当前证据上传保存 URL、文本和 metadata，不负责对象存储直传；如果要上传二进制截图文件，需要补对象存储或本地文件存储方案。

| Worker能力/动作类型 | 职责 | 不负责 |
|---|---|---|
| add_friend | 手机号搜索、发送添加通讯录邀请、写初始绑定备注、记录邀请发送结果；未绑定Worker时由服务端保持blocked，Worker不可领取 | 不聊天、不调用AI、不判断意向、不决定是否分配销售，不判断客户是否同意 |
| session_scan/message_ingest（运行时能力，非Task） | 扫描微信会话、识别客户短码、绑定会话、读取已绑定会话消息、按 `dedupe_key` 上报服务端 | 不进入任务中心、不生成AI回复、不发送消息、不判断意向 |
| chat_reply | 领取服务端已批准的 `reply_action`，调用 OmniAuto 输入并发送回复，回传 `sent_ack` 和证据 | 不监听未绑定会话、不批量加好友、不做线索分配、不改转人工备注 |
| follow_up | 领取召回任务、发送固定召回文案、上报结果 | 不判断召回资格、不AI自由生成文案 |
| Local WeChat UI Lock | 所有微信桌面端 UI 操作串行化 | 不决定业务状态 |

### 3.2 UI锁、优先级与恢复

#### 3.2.1 定位

`Local WeChat UI Lock` 是 Worker 本地基础设施，不属于 C2 业务模块。C1 `add_friend`、C2 `session_scan/message_ingest`、C3 `chat_reply`、C4 `follow_up` 只要需要点击、输入、切换、读取微信 UI，都必须先使用这把锁。

锁分两层：

| 层级 | 名称 | 放在哪里 | 解决什么问题 | 适用范围 |
|---|---|---|---|---|
| 本地互斥锁 | `Local WeChat UI Lock` | Worker 本地进程内 + 本地运行态文件 | 保证同一台电脑上的微信桌面端同一时间只被一个动作操作 | 所有需要操作微信 UI 的动作，包括 C2 运行时扫描 |
| 服务端任务租约 | `task.lease_expires_at` | 服务端任务中心 | 保证一个服务端任务同一时间只被一个 Worker 持有 | `add_friend / chat_reply / follow_up` 任务 |

设计原则：

- 服务端不直接仲裁本机微信 UI 锁；服务端只能看到 Worker 心跳和当前锁摘要。
- 本地锁不可依赖网络可用性，否则断网时可能释放锁导致本机并发操作微信。
- 服务端任务租约和本地 UI 锁必须同时存在，但职责不同。
- C2 不进入任务中心，因此 C2 没有 `task.lease_expires_at`，只使用本地 UI 锁和 C2 自身的 `scan_id / read_run_id / dedupe_key` 幂等控制。

#### 3.2.2 本地锁数据结构

Worker 本地维护内存锁，并同步写入本地运行态文件，例如：

```text
runtime/worker/ui_lock.json
```

字段：

| 字段 | 说明 |
|---|---|
| `lock_id` | 每次获取锁生成的唯一 ID。 |
| `fencing_token` | 单调递增令牌；释放和续租必须匹配，防止旧持有者误释放新锁。 |
| `client_instance_id` | 当前 Worker 客户端实例 ID。 |
| `holder_type` | `task / runtime / recovery`。 |
| `operation_type` | `add_friend / session_scan / message_ingest / chat_reply / follow_up / save_image / diagnostic`。 |
| `task_id` | 服务端任务 ID；C2 运行时能力为空。 |
| `conversation_id` | 相关会话 ID，可为空。 |
| `lead_id` | 相关线索 ID，可为空。 |
| `rpa_session_key` | 微信会话定位键；C2 读取消息时必须记录。 |
| `current_step` | 当前步骤，例如 `opening_wechat_window / typing / clicking_send / reading_messages`。 |
| `acquired_at` | 获取锁时间。 |
| `lease_expires_at` | 本地锁租约过期时间。 |
| `renew_interval_seconds` | 续租间隔。 |
| `max_hold_seconds` | 单次动作最大持锁时间。 |
| `process_id` | Worker 进程 ID。 |
| `last_renewed_at` | 最近续租时间。 |

默认配置：

| 配置 | 默认值 | 说明 |
|---|---:|---|
| `ui_lock_ttl_seconds` | 60 | 本地锁默认租约。 |
| `ui_lock_renew_interval_seconds` | 10 | 持锁期间续租频率。 |
| `ui_step_timeout_seconds` | 30 | 单个 UI 步骤默认超时。 |
| `ui_lock_acquire_timeout_seconds` | 20 | 等待获取锁的默认超时。 |
| `ui_lock_max_hold_seconds` | 180 | 单次 UI 动作最大持锁时间；超过必须失败并恢复。 |

以上默认值可配置，不作为性能承诺。

#### 3.2.3 本地锁接口

Worker 内部提供统一锁接口：

```text
acquire_ui_lock(request) -> lock_handle
renew_ui_lock(lock_handle)
release_ui_lock(lock_handle)
force_recover_stale_lock(reason)
```

`acquire_ui_lock` 请求字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `operation_type` | 是 | 当前 UI 动作类型。 |
| `priority` | 是 | 本地调度优先级。 |
| `holder_type` | 是 | `task / runtime / recovery`。 |
| `task_id` | 否 | 任务动作必填；C2 运行时为空。 |
| `conversation_id` | 否 | 会话动作建议填写。 |
| `rpa_session_key` | 否 | C2 消息读取建议填写。 |
| `ttl_seconds` | 否 | 未传使用默认值。 |
| `acquire_timeout_seconds` | 否 | 未传使用默认值。 |
| `idempotency_key` | 是 | 防止同一动作重复进入锁队列。 |

释放规则：

- 正常完成必须调用 `release_ui_lock`。
- 失败、取消、超时、风控命中也必须进入 `finally` 释放。
- 释放时必须校验 `lock_id + fencing_token + client_instance_id`。
- 不匹配时禁止释放，并返回 `UI_LOCK_OWNER_MISMATCH`。

续租规则：

- 持锁动作预计超过 `renew_interval_seconds` 时必须续租。
- 每次续租刷新 `lease_expires_at`，但不得超过 `acquired_at + max_hold_seconds`。
- 续租失败时必须停止后续 UI 操作，截图取证，返回 `UI_LOCK_RENEW_FAILED`。

#### 3.2.4 超时恢复

Worker 启动、心跳或每次获取锁前都必须执行本地锁恢复检查：

```text
如果 ui_lock.json 存在
  且 process_id 不存在
  或 lease_expires_at < now
  或 client_instance_id != 当前实例
则标记为 stale lock
记录 UI_LOCK_STALE_RECOVERED
清理本地锁
向服务端 heartbeat 上报恢复摘要
```

恢复策略：

| 场景 | 处理 |
|---|---|
| Worker 崩溃重启，本地锁过期 | 清理本地锁；服务端任务按 `task.lease_expires_at` 判断是否恢复、重试或人工处理。 |
| Worker 仍在运行但 UI 步骤超时 | 截图、记录窗口标题、释放锁；当前动作失败并上报错误码。 |
| 服务端任务租约过期但本地锁仍持有 | Worker 停止后续 UI 操作，释放本地锁，上报 `TASK_LEASE_EXPIRED`。 |
| 本地锁过期但动作仍尝试操作微信 | 禁止继续点击/输入/发送，返回 `UI_LOCK_LEASE_EXPIRED`。 |
| 发送动作结果未知 | 不自动补发；进入 `unknown_send_result` 或待人工确认。 |

#### 3.2.5 服务端任务租约

`add_friend / chat_reply / follow_up` 是服务端任务，必须先领取任务租约，再获取本地 UI 锁。

任务租约字段：

| 字段 | 说明 |
|---|---|
| `task.lease_owner_worker_id` | 当前持有任务的 Worker。 |
| `task.lease_owner_client_instance_id` | 当前客户端实例。 |
| `task.lease_expires_at` | 服务端任务租约过期时间。 |
| `task.current_step` | 当前执行步骤。 |
| `task.last_heartbeat_at` | 最近任务心跳时间。 |

任务租约规则：

- Worker claim 任务后，服务端写入 `lease_expires_at`。
- 任务运行中 Worker 按周期续租任务租约。
- 任务租约过期后，服务端不得立刻假设任务未执行；必须结合 Worker 心跳、任务步骤、证据和动作类型判断恢复方式。
- 对发送类动作，租约过期不得自动重放，避免重复发送。
- 对 `add_friend`，如果还没有进入微信点击“确定”步骤，可按错误码重试；如果已进入发送后结果未知，必须人工确认或等待后续 C2 绑定事实。

#### 3.2.6 本地多任务优先级

同一台电脑的微信 UI 操作不可并行，也不做中途抢占。高优先级动作只能等待当前锁释放或超时恢复。

本地 UI 队列优先级：

| 优先级 | 动作 | 说明 |
|---:|---|---|
| 100 | `recovery / emergency_stop` | 释放异常锁、停止风险动作。 |
| 95 | `pre_send_refresh` | 发送前短读目标会话，确认 reply_action 未被新客户消息 supersede。 |
| 90 | `session_scan_visible` | 第一屏主动扫描，优先发现当前微信第一屏新消息、短码和未读变化。 |
| 85 | `message_ingest_visible_hit` | 第一屏命中的会话优先读取消息。 |
| 80 | `chat_reply` | 服务端已批准回复；等待 AI 生成期间不占锁，发送前必须先完成 pre_send_refresh。 |
| 75 | `add_friend` | 加好友任务；优先级高于普通定向读取和召回。 |
| 70 | `message_ingest_state_target` | 状态机驱动定向读取，读取非第一屏的已知重点客户。 |
| 65 | `recall_precheck_read` | 召回前确认读取；确认没有新客户消息后才允许 follow_up。 |
| 60 | `follow_up` | 召回发送；必须经过 recall_precheck。 |
| 40 | `diagnostic` | 人工排查、诊断截图；不作为主监听兜底。 |
| 20 | `save_image` | 图片另存等低优先级动作。 |

队列规则：

- `chat_reply / add_friend / follow_up` 的服务端任务优先级仍成立，但 Worker 发送前必须执行 `pre_send_refresh`，召回发送前必须执行 `recall_precheck_read`。
- C2 不是任务；C2 只有在需要操作微信 UI 时进入本地 UI 队列。
- 第一屏主动扫描和第一屏命中读取优先于普通状态机定向读取，符合销售先处理眼前最新消息的习惯。
- 状态机定向读取必须去重掉本轮已经处理过的 `conversation_id + remark_code` 业务身份；`rpa_session_key / display_name / row_fingerprint` 只作为第一屏快速定位和排查证据。
- 不生成滚动兜底类扫描动作；非第一屏已知客户通过状态机定向读取补齐。
- 同一 `conversation_id` 的发送动作必须按服务端 `reply_action` 顺序执行。

#### 3.2.7 各动作接入方式

| 动作 | 是否进入任务中心 | 是否使用服务端任务租约 | 是否使用本地 UI 锁 | 接入方式 |
|---|---|---|---|---|
| `add_friend` | 是 | 是 | 是 | claim `add_friend` 任务 -> 获取本地锁 -> 调用 OmniAuto `add-friend-entry-click-plan-windows` -> 上报结果 -> 释放锁 -> 完成任务。 |
| `session_scan` | 否 | 否 | 视实现而定 | 若扫描会话列表需要切换/读取微信 UI，则获取本地锁；上报 `session_scan_result`；响应 `next_action=none`。 |
| `message_ingest` | 否 | 否 | 视实现而定 | 若读取消息需要打开/切换会话，则获取本地锁；读取已绑定会话消息；按 `dedupe_key` 上报；响应 `next_action=none`。 |
| `chat_reply` | 是 | 是 | 是 | claim `chat_reply` 任务和 `reply_action` -> 校验未过期、未人工接管、上下文未变化 -> 获取本地锁 -> 发送 -> 上报 `sent_ack` -> 释放锁。 |
| `follow_up` | 是 | 是 | 是 | 仅在 `recall_precheck_read` 确认无新客户消息后，claim `follow_up` 任务 -> 校验召回仍有效 -> 获取本地锁 -> 发送固定文案 -> 上报结果 -> 释放锁。 |

#### 3.2.8 错误码

| 错误码 | 含义 | 处理 |
|---|---|---|
| `UI_LOCK_BUSY` | 本地 UI 锁被其他动作持有。 | 等待或按优先级重新排队。 |
| `UI_LOCK_ACQUIRE_TIMEOUT` | 等待获取锁超时。 | 当前动作不执行微信 UI；任务可重试或延后。 |
| `UI_LOCK_LEASE_EXPIRED` | 本地锁租约已过期。 | 立即停止 UI 操作，截图取证，释放/恢复锁。 |
| `UI_LOCK_RENEW_FAILED` | 本地锁续租失败。 | 停止后续 UI 操作，上报失败。 |
| `UI_LOCK_RELEASE_FAILED` | 释放锁失败。 | 记录严重日志，触发恢复检查。 |
| `UI_LOCK_OWNER_MISMATCH` | 释放或续租者不是当前锁持有者。 | 禁止释放，记录错误，避免误释放新锁。 |
| `UI_LOCK_STALE_RECOVERED` | 启动或检查时发现并清理过期锁。 | 上报 heartbeat 摘要，不算业务失败。 |
| `TASK_LEASE_EXPIRED` | 服务端任务租约过期。 | 停止任务动作，按动作类型恢复；发送类不自动重放。 |
| `TASK_LEASE_RENEW_FAILED` | 服务端任务续租失败。 | 停止后续 UI 操作，释放本地锁。 |
| `UI_STEP_TIMEOUT` | 单个 UI 步骤超时。 | 截图、记录窗口标题和当前步骤，上报失败。 |
| `UI_OPERATION_CANCELLED` | 动作被暂停、人工停止或风控中断。 | 释放锁，按业务状态处理。 |

#### 3.2.9 验收口径

- 同一台 Worker 上同时存在 `add_friend`、`session_scan`、`chat_reply`、`follow_up` 时，微信 UI 操作必须串行，不允许并行点击或输入。
- `chat_reply` 等待 AI 生成期间不占用本地 UI 锁。
- C2 `session_scan/message_ingest` 不进入任务中心，但需要操作微信 UI 时必须获取本地锁。
- Worker 崩溃重启后，过期本地锁能自动恢复，不重复发送消息。
- 服务端任务租约过期后，发送类任务不得自动补发。
- 所有锁相关失败必须有错误码、`trace_id`、本地日志和必要截图证据。

### 3.3 不重复发送硬约束

```text
message_id/dedupe_key用于识别同一条客户消息；
reply_action_id用于识别同一次服务端回复动作；
sent_ack用于确认Worker已发送。
已sent、已过期、上下文变化或已人工接管的动作不得补发。
```

| 对象 | 唯一约束/幂等键 |
|---|---|
| message_event | `unique(conversation_id, dedupe_key)`。 |
| message_batch | 同一 `conversation_id` 最多一个 `active_batch`。 |
| reply_action | `reply_action_id` 全局唯一；同一 batch 同一 generation 只能有一个 current action。 |
| sent_ack | `unique(reply_action_id)`，`sent_ack` 只能成功写入一次。 |
| follow_up | `unique(lead_id, rule_id, recall_round)`，且只能在 `recall_precheck` 放行后创建。 |
| handoff_event | `unique(conversation_id, handoff_reason_group, active_period)`；转人工原因字段统一为 `handoff_reason_code`。 |

### 3.4 执行台展示与验收

- 展示当前 Worker 状态、任务类型、任务 ID、客户/线索短码、步骤时间线、微信/服务端连接、图片缩略图、AI 候选回复、Guard 结果、风控原因、飞书通知结果、错误日志。
- 提供启动、暂停、继续、停止、手动接管/禁用 AI、重试、跳过按钮。
- 验收要求：看得见、停得住、查得到原因、不会重复发送、三个任务类型共用同一 UI 锁。

## 4. 模块3：线索与销售分配

- 目标：把抖音小风车手机号线索变成某个销售微信号要执行的 `add_friend` 任务。
- 第一期线索接入方式不锁死 Excel、CSV 或 API，统一抽象为线索接入适配器；后续可接小风车/API。
- 手机号默认脱敏展示；手机号标准化后作为核心去重键。
- 同手机号一旦标记 `rejected`，后续再次导入也不自动处理。
- 销售每日加好友上限需要配置，默认值待定。
- 分配策略待定，先预留手动分配和轮询分配。

| 状态/规则 | 说明 |
|---|---|
| Lead/Conversation状态 | new、assigned、add_friend_blocked、add_friend_pending、add_friend_sent、friend_added、ai_active、waiting_user_reply、recalled_waiting_user、waiting_sales_reply、sales_replied_waiting_user、rejected、closed。 |
| 任务生成条件 | 手机号有效、销售启用、未超上限、不在黑名单、不存在未完成 add_friend 任务；销售未绑定Worker时仍创建任务，但状态为blocked，block_code=SALES_WORKER_NOT_BOUND。 |
| 重复手机号 | 同手机号未关闭线索不重复新建有效线索，追加来源记录。 |
| 销售Worker | 一对一绑定，允许换绑；后续绑定Worker时，服务端自动把该销售名下 SALES_WORKER_NOT_BOUND 的 add_friend 任务从 blocked 恢复为 pending；运行中任务换绑需人工确认。 |

### 4.1 页面与验收

- 线索列表展示手机号后四位、来源、销售、状态、最近联系时间、是否转人工、是否拒绝、召回次数、创建时间。
- 销售列表展示销售姓名、微信号、绑定 Worker、飞书用户、启用状态、今日加好友数、今日 AI 回复数。
- 任务列表展示任务 ID、手机号后四位、销售、Worker、状态、失败原因、重试次数、创建/完成时间。
- 验收：可导入/接入线索、去重、手动分配、绑定 Worker、生成 `add_friend` 任务、失败原因可见、备注短码唯一。

## 5. 模块4：加好友 add_friend

- 目标：通过商家侧电脑微信桌面客户端，按手机号提交好友申请，并建立初始绑定标识。
- 不负责线索分配、AI 聊天、自动召回、意向判断、飞书接管通知和转人工备注。
- 好友申请语最终文案待定，作为配置项；默认可支持销售姓名、门店名、线索来源变量。
- 初始备注用于线索与微信会话初始绑定，命名规则待定；转人工阶段不修改备注。
- 已是好友时立即尝试绑定会话，不重复发送添加邀请。
- 加好友失败后允许人工在控制面点击重试。
- Worker 调用 OmniAuto V15 当前正式主链路只允许使用 `add-friend-entry-click-plan-windows`，旧 `add-friend`、`add-friend-plan`、`add-friend-entry-plan`、`add-friend-entry-click-plan` 不作为正式入口。
- 正式 payload 必填字段为 `phone_or_wechat`、`verify_message`、`remark_name`、`remark_code`；缺任一字段必须返回 `TASK_PAYLOAD_INVALID`，且 `wechat_ui_action_attempted=false`，不得触达微信 UI。
- `remark_name` 必须包含 `remark_code`；`remark_code` 由服务端生成，OmniAuto 只消费不生成。
- 主链路不使用 `sales_name` 自动拼申请语，不使用 `remark` 兜底备注名，避免字段来源混乱。
- 加好友结束点是申请添加朋友页点击“确定”成功后；只要没有明确风控/失败提示，即上报 `task.status=completed` 且 `result_code=invite_sent`。

| 状态/异常 | 处理 |
|---|---|
| 执行状态 | 主状态：blocked、pending、running、completed、failed、cancelled；running 内部步骤：searching_contact、contact_found、opening_add_contact、filling_remark、sending_invite、waiting_ui_response；邀请已发送表达为 `task.status=completed` 且 `result_code=invite_sent`。 |
| 可重试失败 | wechat_not_login、wechat_window_not_found、ui_element_not_found、network_error、worker_interrupted、unknown_error。 |
| 完成结果 | `already_friend` 表示已是好友，任务应记为 `task.status=completed` 且 `result_code=already_friend`，随后立即尝试绑定会话。 |
| 不建议自动重试 | phone_invalid、phone_not_found、customer_privacy_blocked、wechat_rate_limit、operation_too_frequent、account_restricted、blacklist_hit、daily_limit_reached。 |
| 微信风险提示 | 操作频繁、环境异常、添加受限等出现后暂停 add_friend 并上报，不自动连续重试。 |
| 幂等 | 同一 task_id 多次上报成功只记录一次；同手机号不生成多个未完成加好友任务。 |

## 6. 模块5：会话绑定与监听

- 目标：知道微信里这条消息是谁发的、属于哪条线索、当前 AI 能不能回。
- 第一期不读取微信数据库、不破解协议、不使用非公开微信接口、不依赖客户昵称唯一性。
- 绑定优先通过初始备注/短码；已是好友立即尝试绑定；绑定失败不自动回复。
- 监听客户文字、图片、系统提示和我方消息；图片 `message type=3` 作为已知线索，实际实现保留兜底。
- 重复消息通过 `dedupe_key` 去重；同一 `dedupe_key` 只处理一次。
- 本模块是 OmniAuto 接入 C2 checkpoint。Worker 调用 OmniAuto `sessions/messages` 能力读取微信事实，服务端负责短码绑定、会话状态、消息去重和是否允许后续 AI 回复。
- 本模块不生成 AI 回复、不发送 AI 回复；AI 回复发送属于 C3 checkpoint，必须在会话绑定和消息入库稳定后实施。
- C2 接口、状态、错误码和验收标准以本模块为准。

### 6.0 OmniAuto结合方式

```text
Worker运行时扫描
  -> OmniAuto sessions：扫描微信会话列表，提取display_name、短码候选、未读提示、行特征和截图证据
  -> 服务端绑定：用remark_code匹配唯一lead/conversation/sales/worker
  -> OmniAuto messages：读取已绑定会话近期消息
  -> 服务端入库：按dedupe_key去重，更新conversation状态，后续再触发AI编排
```

会话绑定/微信监听是 Worker 运行时能力，不直接作为 `chat_reply` 任务。`chat_reply` 任务只在服务端已经生成并批准 `reply_action` 后，用于让 Worker 调用 OmniAuto 执行发送。

C2 第一屏主动扫描、状态机定向读取和消息读取均属于 Worker 端 OmniAuto RPA Sidecar 能力，具体调用 `sessions/messages`。它们只负责识别微信窗口、OCR 当前可见会话列表、定位/切换指定会话、读取消息和截图取证；不调用加好友正式入口 `add-friend-entry-click-plan-windows`，不执行旧滚动兜底方案，也不运行 OmniAuto 原本的本地 AI 客服闭环。

工程约束：C2 不进入统一任务中心，`session_scan`、`message_ingest`、`wechat_binding` 不允许定义为 `task_type`。C2 只能写入 `wechat_session_bindings`、`message_events` 和会话监听状态；只有进入 C3 且服务端决定需要发送 AI 回复时，才创建 `chat_reply` 任务。

### 6.0.1 C2第一屏主动扫描与状态机定向读取

C2 的微信监听不是微信官方事件推送，也不采用旧滚动兜底方案作为主链路。微信左侧会话列表会随最新消息动态排序，遍历非当前可见区无法保证不漏扫，也容易造成微信 UI 状态扰动。

V16 系列主链路固定为：

```text
第一屏主动扫描
-> 第一屏命中会话优先读取
-> 服务端去重入库并触发AI/转人工判断
-> 空闲后按状态机定向读取剩余重点会话
-> 召回到期先 recall_precheck_read
-> 确认无新客户消息后才允许 follow_up
```

明确删除：

```text
此前滚动兜底方案已废弃，不作为当前设计、接口参数、配置或验收项
```

#### 6.0.1.1 第一屏主动扫描

第一屏主动扫描是 Worker 日常最高频 C2 动作，模拟销售先看微信当前第一屏最新消息的习惯。

第一屏主动扫描只做轻量事实发现：

| 对象 | 说明 | 后续动作 |
|---|---|---|
| 当前第一屏疑似未读/红点会话 | 最新消息通常会被微信顶到第一屏。 | 加入 `visible_hit_queue`。 |
| 当前第一屏预览文本变化会话 | 可能有客户新消息或销售同步消息。 | 加入 `visible_hit_queue`。 |
| 当前第一屏包含短码但未绑定会话 | 线下短码好友或新绑定候选。 | 上报 `session_scan_result` 走绑定。 |
| 当前第一屏已绑定且近期活跃会话 | 需要确认是否有新消息。 | 加入 `visible_hit_queue`。 |

第一屏主动扫描要求：

- 只扫描当前可见会话列表，不遍历非当前可见区。
- 单次持有 `Local WeChat UI Lock` 必须短，默认建议 3-8 秒。
- 只上报会话事实，不直接判断是否回复，不直接创建 `reply_action`。
- 发现命中会话后，优先进入 `message_ingest_visible_hit`。

#### 6.0.1.2 第一屏命中优先读取

第一屏主动扫描发现的命中会话进入 Worker 本地 `visible_hit_queue`。

读取顺序：

```text
第一屏扫描
-> visible_hit_queue
-> OmniAuto messages读取
-> 生成dedupe_key
-> 上报/messages/ingest
-> 服务端去重入库
```

`visible_hit_queue` 处理规则：

| 规则 | 说明 |
|---|---|
| 优先读取 | 第一屏命中优先于普通状态机定向读取。 |
| 批量上限 | 每轮最多读取配置化数量，第一期建议 3-5 个，避免长期占用微信。 |
| 去重 | 同一轮按 `conversation_id + remark_code` 身份键去重；`remark_code` 是非第一屏微信搜索定位主锚点，`rpa_session_key / display_name / row_fingerprint` 只用于第一屏快速定位和排查证据。 |
| 失败处理 | 找不到目标、OCR低置信、微信异常时记录错误码，不乱滚、不乱点。 |

#### 6.0.1.3 状态机驱动定向读取

状态机定向读取用于处理非第一屏的已知客户。服务端根据会话状态和时间字段返回 `read-targets`，Worker 空闲后逐个定向读取。

定向读取不是滚动找人，而是明确找指定客户。`conversation_id + remark_code` 是业务身份收口，`remark_code` 是 Worker/OmniAuto 非第一屏搜索定位主锚点；`rpa_session_key / display_name / row_fingerprint` 只作为第一屏快速定位和排查证据。服务端返回的 `read-targets` 必须包含 `remark_code`。

定位顺序：

```text
1. 第一屏可见时可用 rpa_session_key / display_name 辅助快速定位
2. 非第一屏或定位键失效时必须用 remark_code 搜索定位
3. 进入会话后必须二次确认 remark_code
4. 仍找不到则失败，不进入旧滚动兜底方案
```

状态机可返回的读取原因：

| read_reason | 场景 | 说明 |
|---|---|---|
| `recent_ai_sent` | AI 刚发送过回复 | 客户可能马上回复，短期提高读取优先级。 |
| `waiting_user_reply` | 我方已回复，等待客户回 | 定期读取确认客户是否回复。 |
| `waiting_sales_reply` | 已转人工，等待销售回复 | 监听销售手机端回复是否同步到桌面端。 |
| `recall_precheck` | 召回到期前确认 | 召回前必须读取一次，确认客户没有新消息。 |

定向读取前必须去重掉本轮第一屏已处理对象：

```text
state_target_queue = read_targets - visible_hit_processed
去重键：conversation_id + remark_code
```

#### 6.0.1.4 召回前 precheck

召回不能只按时间直接发送。只要会话达到召回时间，服务端必须先生成 `recall_precheck` 读取目标。

召回判断流程：

```text
waiting_user_reply / recalled_waiting_user 超过N天
-> 服务端生成 recall_precheck read-target
-> Worker 定向读取该会话
-> 服务端处理读取结果
```

读取结果分支：

| 结果 | 后续动作 |
|---|---|
| 读到新的客户消息 | 取消召回；新消息进入 message_batch，按 AI 回复/转人工链路处理。 |
| 未读到新客户消息 | 允许创建 `follow_up` 任务，Worker 发送固定召回文案。 |
| 找不到会话 / OCR低置信 / 微信异常 | 不发送召回；记录 `RECALL_PRECHECK_FAILED` 或具体错误码，等待人工或下次检查。 |
| 读到销售人工回复 | 不发送召回；状态进入销售已回复后的等待客户状态。 |

#### 6.0.1.5 C2调度顺序

Worker 本地调度不是简单优先级表，而是闭环调度：

```text
1. 当前动作正在执行时，不中途强杀，除非 emergency_stop。
2. 到达第一屏扫描周期时，优先执行 session_scan_visible。
3. 第一屏命中会话进入 visible_hit_queue，并优先读取。
4. 服务端消息入库后，AI思考在服务端异步进行，不占用微信 UI 锁。
5. 若服务端生成 chat_reply，Worker 发送前先做 pre_send_refresh。
6. 空闲后拉取 read-targets，去重掉本轮第一屏已处理对象。
7. 处理状态机定向读取 state_target_queue。
8. 召回到期先执行 recall_precheck_read，确认无新客户消息后才进入 follow_up。
9. follow_up 发送完成后进入 recalled_waiting_user。
10. 不执行旧滚动兜底方案。
```

本地 UI 操作优先级：

```text
recovery / emergency_stop
> pre_send_refresh
> session_scan_visible
> message_ingest_visible_hit
> chat_reply
> add_friend
> message_ingest_state_target
> recall_precheck_read
> follow_up
> diagnostic
> save_image
```

说明：

- `pre_send_refresh` 只针对即将发送的会话做短读取，防止旧回复发出。
- `session_scan_visible` 和 `message_ingest_visible_hit` 优先体现销售先处理第一屏最新消息的习惯。
- 普通状态机定向读取用于空闲时补齐已知客户，不抢第一屏即时消息。
- `follow_up` 必须由 `recall_precheck_read` 放行后才能发送。

### 6.0.2 C2主动扫描工程规则

主动扫描是 C2 的主工作机制，指 Worker 不等待服务端下发 `chat_reply` 任务，也不等待微信官方推送，而是在本地按规则主动调用 OmniAuto `sessions/messages` 获取微信事实。

主动扫描分为两个短动作：

| 动作 | 调用能力 | 作用 | 是否进入统一任务中心 |
|---|---|---|---|
| `session_scan` | OmniAuto `sessions` | 扫描会话列表可见区，识别短码、未读提示、会话行特征。 | 否 |
| `message_ingest` | OmniAuto `messages` | 打开/定位已绑定会话，读取近期消息并上报服务端。 | 否 |

#### 6.0.2.1 主动扫描触发

主动扫描在以下场景触发：

| 触发场景 | 默认口径 |
|---|---|
| Worker 在线循环 | Worker 启动后进入本地循环，按 `poll_after_seconds` 和本地配置执行短切片扫描。 |
| 服务端返回 read-targets | Worker 定期调用 `/wechat/sessions/read-targets`，但普通定向读取必须让位于第一屏命中读取。 |
| 当前屏未读/活跃变化 | Worker 可见区扫描发现未读、红点、最近消息预览变化时，上报 `scan-result` 并读取可绑定目标。 |
| 加好友成功或已是好友 | 对该线索对应会话做一次定向扫描/读取。 |
| AI发送完成 | 将该会话加入本地重点观察队列，后续主动扫描优先读取。 |
| 人工点击立即扫描 | 执行一次人工触发扫描，仍遵守 UI 锁和优先级。 |

主动扫描不是无限循环刷屏。第一期建议配置为：

```text
可见区session_scan：5-15秒一轮，配置化
read-targets拉取：5-15秒一轮，配置化
单轮message_ingest最大会话数：配置化，第一期建议3-5个
单轮主动扫描最大持锁时间：配置化，第一期建议10-20秒
```

以上是默认配置，不作为性能承诺；验收以测试环境实际表现为准。

#### 6.0.2.2 主动扫描执行步骤

每轮第一屏主动扫描按以下顺序执行：

```text
1. Worker确认本机在线、微信可控、自动监听开关开启。
2. Worker检查是否处于正在执行的微信 UI 动作；若无锁占用，第一屏扫描可优先执行。
3. 如果需要操作微信UI，先获取Local WeChat UI Lock。
4. 调用OmniAuto sessions扫描当前可见会话列表。
5. 生成scan_id、sidecar_run_id、rpa_session_key、row_fingerprint、remark_code_candidates。
6. 上报 /wechat/sessions/scan-result。
7. 服务端返回绑定结果和can_ingest_messages。
8. Worker将第一屏命中会话加入 `visible_hit_queue` 并优先读取。
9. Worker调用 /wechat/sessions/read-targets 拉取状态机待读取会话，并去重掉本轮第一屏已处理对象。
10. Worker上报 /wechat/messages/ingest。
11. 服务端按dedupe_key幂等入库，返回ingest_result。
12. Worker释放Local WeChat UI Lock，记录本轮结果。
```

如果当前已经有微信 UI 动作在执行，主动扫描等待当前动作完成；不做中途抢占。

#### 6.0.2.3 服务端 read-targets 选择规则

服务端 `/wechat/sessions/read-targets` 只返回允许读取的已绑定会话。

必须满足：

```text
bind_status=bound
remark_code非空
allow_listening=true
listen_status in listening/degraded
conversation.status not in closed/rejected
worker_id匹配当前Worker
```

排序优先级：

| 优先级 | read_reason | 说明 |
|---|---|---|
| 1 | `recall_precheck` | 召回到期前确认读取；读取后仍无新消息才允许 follow_up。 |
| 2 | `recent_ai_sent` | AI 刚发送过，正在等待客户回复。 |
| 3 | `waiting_user_reply` | 我方已回复，等待客户回。 |
| 4 | `waiting_sales_reply` | 已转人工，监听销售是否回复。 |

服务端不得返回：

```text
bind_status != bound 的会话
remark_code为空的会话
listen_status=disabled/paused/error 且未人工恢复的会话
conversation.status=closed/rejected 的会话
非当前 Worker 负责的会话
```

`conversation_id + remark_code` 是定向读取的身份收口。若历史脏数据或异常绑定导致已绑定会话缺少 `conversation_id` 或 `remark_code`，服务端不得把该会话作为正常 `read-target` 返回，应将绑定/监听状态置为 `degraded` 或 `needs_review`，并记录对应错误码。`rpa_session_key / display_name` 只用于第一屏可见会话的快速定位和排查证据，不作为非第一屏定向读取的必要条件。

#### 6.0.2.4 主动扫描中断与恢复

主动扫描被中断时：

| 场景 | 处理 |
|---|---|
| `chat_reply` 到达 | 当前最小读取步骤结束后释放锁；发送前必须先执行 `pre_send_refresh`。 |
| `add_friend` 到达 | 第一屏扫描不抢占正在执行的加好友；若未开始加好友，先完成短切片第一屏扫描和命中读取。 |
| 微信窗口不可控 | 停止本轮扫描，上报 `WECHAT_WINDOW_NOT_READY`。 |
| OmniAuto 超时 | 停止本轮扫描，上报 `RPA_SIDECAR_TIMEOUT`。 |
| Worker 退出/重启 | 不补发、不重复入库；重启后继续按状态机 `read-targets` 读取。 |

主动扫描只能上报事实，不能自己补发消息、不能自己创建 `reply_action`、不能直接改变 `conversation.status`。

### 6.0.3 C2消息去重入库工程规则

去重入库由服务端负责，Worker 只负责生成并上报候选 `dedupe_key` 和原始证据。服务端必须以数据库唯一约束作为最终防线，不能只靠内存判断。

#### 6.0.3.1 dedupe_key生成规则

优先级如下：

| 优先级 | 来源 | 生成方式 |
|---|---|---|
| 1 | OmniAuto/微信侧可稳定识别的消息 ID | 如果 OmniAuto 能提供稳定消息 ID，使用 `conversation_id + stable_message_id` 生成。 |
| 2 | 图片/文件消息 | 使用 `conversation_id + sender_role_hint + message_type + file_hash/image_hash + occurred_at_bucket` 生成。 |
| 3 | 文本消息 | 使用 `conversation_id + sender_role_hint + message_type + normalized_content_hash + occurred_at_bucket + visual_position_fingerprint` 生成。 |
| 4 | 兜底 | 使用 `conversation_id + rpa_session_key + sender_role_hint + message_type + raw_payload_hash` 生成，并标记低置信。 |

文本归一化规则：

```text
去除首尾空白
统一换行和连续空格
保留中文、数字、标点本身
不做语义改写
不把不同文本合并成同一内容
```

时间桶 `occurred_at_bucket` 用于降低 OCR 时间不稳定造成的重复，但不能单独作为去重依据。第一期建议按分钟级或消息列表行位置辅助，实际由 Worker/OmniAuto 能力决定。

禁止做法：

```text
不能只用 content 做 dedupe_key
不能只用 occurred_at 做 dedupe_key
不能只用 display_name 做 dedupe_key
不能把同一客户连续两句不同内容合并成同一 dedupe_key
不能因为 dedupe_key 生成失败而自动触发 AI 回复
```

#### 6.0.3.2 消息入库事务

服务端收到 `/wechat/messages/ingest` 后必须按事务处理：

```text
1. 校验 Worker、conversation_id 是否匹配已绑定会话，并确认绑定存在非空 remark_code。
2. 校验 conversation.status 是否允许监听。
3. 校验 message.dedupe_key 是否存在。
4. 对每条消息执行数据库唯一键写入。
5. 写入成功：创建 message_event，返回 ingested。
6. 唯一键冲突：不创建新 message_event，返回 duplicated。
7. 状态不允许、发送方不明确、系统低价值消息、目标未确认、搜索不到或搜索结果不唯一：不创建 message_event，返回 ignored。
8. 只有 ingested 且 sender_role=customer 的消息，才允许触发后续 message_batch 收集。
```

推荐唯一约束：

```text
unique(conversation_id, dedupe_key)
```

如果历史实现已使用：

```text
unique(worker_id, conversation_id, dedupe_key)
```

需要保证同一会话换 Worker 后不会重复触发 AI；服务端在查询/收集 `message_batch` 时必须以 `conversation_id + dedupe_key` 做业务去重。

#### 6.0.3.3 去重结果与后续动作

| ingest_result | 是否写入 message_events | 是否触发 message_batch | 说明 |
|---|---|---|---|
| `ingested` | 是 | 仅 `sender_role=customer` 时允许 | 新客户消息进入后续 AI 编排。 |
| `duplicated` | 否 | 否 | 已处理过，不重复触发。 |
| `ignored` | 否 | 否 | 系统消息、自发消息、状态不允许或低价值消息。 |

同一个 `read_run_id` 重复上报时，服务端必须返回同样或等价的处理结果，不得重复入库。

#### 6.0.3.4 低置信和异常处理

| 场景 | 处理 |
|---|---|
| 缺少 `dedupe_key` | 拒绝该条消息，返回 `MESSAGE_DEDUPE_KEY_MISSING`。 |
| `conversation_id` 未绑定当前 Worker、绑定缺少 `remark_code` 或监听状态不允许 | 拒绝整批或该会话消息，返回 `MESSAGE_CONVERSATION_NOT_BOUND`。 |
| 读取目标未确认、搜索不到或搜索结果不唯一 | 返回 `ignored`，错误码为 `TARGET_NOT_CONFIRMED / SEARCH_NOT_FOUND / SEARCH_AMBIGUOUS`；不得触发 AI，不得创建发送任务。 |
| OCR 低置信但有明确文本 | 可入库但 `raw_payload/ocr_confidence` 必须保留，后续 Guard 可转人工。 |
| 发送方无法判断 | 返回 `ignored` 或 `sender_role=unknown`，不得触发 AI 回复。 |
| 消息顺序异常 | 按 `occurred_at + 入库顺序` 排序展示，不能靠顺序异常重复触发 AI。 |
| 图片无 hash | C2 只记录图片事实；C4 视觉处理前不得自动生成图片回复。 |

### 6.0.4 V16状态机定向读取工程方案

V16 是 C2 会话监听的修复 checkpoint。修复目标不是扩展旧滚动兜底方案，而是把 C2 主链路收口为“第一屏主动扫描 + 第一屏命中优先读取 + 短码搜索定向读取 + 召回前 precheck + 去重入库”。

#### 6.0.4.0 定向读取口径修正

定向读取不能理解为“OmniAuto 可以凭 `rpa_session_key` 找到任意历史会话”。当前 `rpa_session_key` 是 Worker/OmniAuto 基于微信当前可见会话行 OCR 信息生成的本地定位键，本质上属于当前窗口、当前列表形态下的行指纹，不是微信官方稳定会话 ID。

因此定向读取分两种路径：

| 路径 | 适用场景 | 定位依据 | 处理要求 |
|---|---|---|---|
| 第一屏可见快速读取 | 目标会话刚被 `sessions` 扫到，仍在当前第一屏可见区。 | `rpa_session_key / display_name / remark_code`。 | 可优先用可见行定位，但读取前仍需确认目标短码。 |
| 短码搜索定向读取 | 服务端 `read-targets` 下发的目标不在第一屏，或第一屏未命中。 | `remark_code`。 | 必须通过微信搜索框搜索短码，找到会话后再次确认标题/备注包含该短码，确认成功才允许读取。 |

禁止做法：

```text
不能把 rpa_session_key 当成跨屏稳定 ID。
不能为了找非第一屏会话执行多屏滚动补偿扫描。
不能在短码未确认时读取当前聊天窗口消息。
不能在搜索结果多义、低置信或标题不含短码时继续读取。
```

OmniAuto 需要提供或扩展的 RPA 能力：

```text
messages target-mode=visible
  - 用于第一屏可见会话快速读取。

messages target-mode=search_by_remark_code
  - 输入 remark_code。
  - 点击微信搜索框。
  - 清空搜索框。
  - 按人工习惯输入 remark_code。
  - 等待搜索结果稳定。
  - OCR 搜索结果。
  - 唯一命中后点击会话。
  - 进入会话后再次确认标题/备注包含 remark_code。
  - 确认成功后读取消息。
  - 清理搜索状态或恢复到安全状态。
```

失败必须显式返回错误码，不得降级为读取当前窗口：

| 错误码 | 含义 | 处理 |
|---|---|---|
| `TARGET_SEARCH_NOT_FOUND` | 搜索短码未找到会话。 | 不读取、不入库，记录读取失败。 |
| `TARGET_SEARCH_AMBIGUOUS` | 搜索结果存在多个疑似会话。 | 不读取，进入人工复核或等待下次扫描。 |
| `TARGET_CONFIRM_FAILED` | 点击后标题/备注未确认包含短码。 | 不读取，返回目标不确认。 |
| `TARGET_OCR_LOW_CONFIDENCE` | 搜索结果或标题 OCR 置信度不足。 | 不读取，保留截图证据。 |
| `TARGET_NOT_CONFIRMED_FOR_MESSAGES` | 目标会话未确认，不允许读取消息。 | 不入库，不触发 AI。 |

#### 6.0.4.1 短码搜索定向读取执行步骤

短码搜索定向读取要参考 `add_friend` 主链路的工程风格：字段先强校验，校验失败不触达微信 UI；每个 UI 动作都有 step event、截图证据、耗时和错误码；固定坐标兜底必须在报告中明显标红；目标没有二次确认时不得读取消息。

正式 OmniAuto 入口建议保持在 `messages` action 下扩展模式，避免新增一套并行读取协议：

```text
action=messages
target_mode=search_by_remark_code
conversation_id=<服务端会话ID>
remark_code=<客户短码>
read_reason=waiting_user_reply | recent_ai_sent | recall_precheck | pre_send_refresh | waiting_sales_reply
last_ingested_at=<服务端最后入库时间，可选>
display_name=<最近一次绑定展示名，可选>
rpa_session_key=<第一屏最近一次定位键，可选>
artifact_dir=<本次证据目录>
```

`remark_code` 是搜索和身份确认主锚点；`conversation_id` 是服务端业务身份；`display_name / rpa_session_key` 只能辅助定位、展示和排查。

| 步骤 | 名称 | 操作要求 | 成功标准 | 失败处理 |
|---|---|---|---|---|
| 0 | Payload 强校验 | 校验 `conversation_id / remark_code / read_reason / artifact_dir`；`remark_code` 必须非空、无空白、长度不超过备注规则上限。 | 字段合法，生成 `read_run_id`。 | 返回 `C2_TARGET_PAYLOAD_INVALID`；`wechat_ui_action_attempted=false`；不得探测窗口、截图或点击。 |
| 1 | 获取本地微信 UI 锁 | Worker 获取 Local WeChat UI Lock，`operation_type=message_ingest`，`read_reason` 写入锁上下文。 | 获得有效锁和 fencing token。 | 返回 `WECHAT_UI_LOCK_BUSY` 或等待下轮调度；不得并发操作微信。 |
| 2 | 微信窗口预检 | 调 OmniAuto 检查微信主窗口、登录态、遮挡、弹窗、风险提示、当前窗口是否可控。 | 微信主窗口可控，无阻塞弹窗。 | 返回 `WECHAT_WINDOW_NOT_READY / WECHAT_RISK_PROMPT_DETECTED / WECHAT_MODAL_BLOCKED`；释放锁。 |
| 3 | 基线截图与 step event | 截取操作前窗口，记录当前标题、窗口位置、DPI、当前选中会话摘要。 | evidence 中有 raw/annotated 截图和窗口元数据。 | 截图失败返回 `SCREENSHOT_CAPTURE_FAILED`；不得继续。 |
| 4 | 定位微信搜索框 | 复用 OmniAuto locator 思路：优先控件/视觉/OCR 定位微信左上搜索框；固定坐标只能作为最后兜底。 | 搜索框点击点位于微信左侧顶部搜索区域。 | 返回 `SEARCH_BOX_NOT_FOUND`；若使用固定兜底，报告必须标记 `fallback_used=true`。 |
| 5 | 聚焦并清空搜索框 | 点击搜索框，执行清空动作；允许最多 2 次轻量重试。 | OCR/控件状态确认搜索框为空，或已回到占位符状态。 | 返回 `SEARCH_BOX_CLEAR_FAILED`；不得输入短码。 |
| 6 | 输入短码 | 按“人工复制短码后粘贴搜索”的习惯输入 `remark_code`，默认使用剪贴板粘贴；粘贴前后必须有短随机停顿；不得高速逐字输入；不得输入其他客户信息。 | 搜索框内容或搜索结果上下文能确认本次查询为该 `remark_code`。 | 返回 `SEARCH_INPUT_VERIFY_FAILED`；清理搜索状态并释放锁。 |
| 7 | 等待搜索结果稳定 | 等待搜索结果刷新，至少两帧 OCR 结果稳定，或达到配置超时。 | 候选结果列表稳定。 | 返回 `TARGET_SEARCH_TIMEOUT`；不得点击不稳定结果。 |
| 8 | 解析候选结果 | 只接受“联系人/会话标题/备注”包含 `remark_code` 的候选；单纯消息内容命中不能作为目标。 | 唯一候选包含 `remark_code`。 | 0 个候选返回 `TARGET_SEARCH_NOT_FOUND`；多个候选返回 `TARGET_SEARCH_AMBIGUOUS`。 |
| 9 | 点击唯一候选 | 使用候选行安全点击点进入会话，点击前后均记录截图和候选框。 | 微信进入候选会话。 | 返回 `TARGET_CLICK_FAILED`；不得读取当前窗口。 |
| 10 | 二次确认目标 | 进入会话后 OCR 标题/备注/当前选中行，必须确认包含 `remark_code`。`display_name` 只能辅助，不能替代短码。 | `target_confirmed=true`，确认来源写入 evidence。 | 返回 `TARGET_CONFIRM_FAILED / TARGET_NOT_CONFIRMED_FOR_MESSAGES`；不得读取消息。 |
| 11 | 读取消息 | 复用 OmniAuto `messages` 解析能力读取当前会话可见消息，输出 `sender_role_hint / message_type / content / occurred_at / raw_payload`。 | 返回消息列表或明确空结果，并带 `target_confirmed=true`。 | 读取失败返回 `MESSAGE_READ_FAILED`；不得伪造空成功。 |
| 12 | 生成结果与证据 | 输出 `read_run_id / conversation_id / remark_code / target_mode / target_confirmed / messages / evidence / step_events`。 | Worker 可上报后端 `/wechat/messages/ingest`。 | 结果缺关键字段视为 `C2_MESSAGE_READ_RESULT_INVALID`。 |
| 13 | 清理或保持安全状态 | 成功读取后可保持目标会话打开，供 `pre_send_refresh` 后继续发送；失败时清理搜索框或回到安全状态。 | 不影响下一次 add_friend / scan / send 操作。 | 清理失败记录 `SEARCH_STATE_CLEANUP_FAILED`，但不得继续执行发送。 |

短码输入策略：

| 策略 | 默认值 | 说明 |
|---|---|---|
| `input_method` | `clipboard_paste` | 默认模拟人工从系统复制短码后粘贴到微信搜索框；短码不是客户聊天内容，使用粘贴比逐字输入更符合人工检索习惯。 |
| `before_paste_delay_ms` | `180-450` 随机 | 点击搜索框并清空后，粘贴前短暂停顿。 |
| `after_paste_delay_ms` | `300-800` 随机 | 粘贴后等待微信搜索自然刷新。 |
| `press_enter_after_input` | `false` | 默认不按回车，优先等待搜索结果自然出现；只有实机确认当前微信版本必须回车触发搜索时，才配置为 true。 |
| `fallback_input_method` | `humanized_typing` | 粘贴失败或剪贴板不可用时，才使用人工节奏逐字输入。 |
| `typing_char_delay_ms` | `80-180` 随机 | 逐字输入 fallback 时使用；不得固定 0ms 高速输入。 |
| `max_input_attempts` | `2` | 输入失败最多重试 2 次；重试前必须重新清空搜索框。 |

剪贴板使用要求：

```text
1. Worker/OmniAuto 只能把 remark_code 写入剪贴板，不得写入手机号、客户姓名、回复内容等敏感文本。
2. 粘贴前记录剪贴板前置状态是否可读；可读时在结束后尽量恢复原剪贴板内容。
3. 若系统不允许读取或恢复剪贴板，必须在 evidence 中记录 clipboard_restore=skipped。
4. 粘贴后必须通过 OCR/控件状态确认搜索词为 remark_code，不能假定粘贴成功。
5. 不得为了“看起来像人”故意输入错误字符再删除。
```

回车策略：

```text
默认不按回车。
原因：微信搜索框通常输入后会自动刷新结果，回车可能直接打开当前高亮结果，增加误点风险。
只有在 Windows 实机验证确认该微信版本必须回车才出结果时，才允许按 Enter。
如果启用 Enter，必须先等待 after_paste_delay_ms，再按 Enter，并再次等待搜索结果稳定。
```

搜索结果判定细则：

```text
1. 精确短码命中优先：结果标题/备注包含完整 remark_code。
2. 不能只靠 display_name 命中，因为销售可能修改展示名。
3. 不能只靠搜索到的聊天内容命中，因为那可能是历史消息，不代表当前会话标题。
4. 同一个 remark_code 出现多个联系人/会话候选时，必须返回 TARGET_SEARCH_AMBIGUOUS。
5. OCR 置信不足时，必须返回 TARGET_OCR_LOW_CONFIDENCE，并保留截图证据。
```

证据报告要求与 add_friend 保持一致：

```text
每一步都有 step_id、title、status、state_before、state_after、timing_ms。
涉及点击的步骤必须保存 raw screenshot 和 annotated screenshot。
selected_target 必须记录坐标、来源、置信度、fallback_used。
fallback_used=true 的步骤必须在 HTML/JSON 报告中突出提示。
最终事件必须是 directed_message_read_after_confirm，不得用模糊的 final_detection。
```

Worker 调度规则：

```text
第一屏主动扫描和第一屏命中读取优先。
只有 visible_hit_queue 处理完后，才处理 state_target_queue 的短码搜索定向读取。
短码搜索定向读取执行期间占用同一把本地微信 UI 锁。
如果 add_friend / chat_reply send 已持锁，定向读取等待，不抢占。
如果 chat_reply 已 claim，发送前 pre_send_refresh 必须走本流程；目标未确认时禁止发送。
```

#### 6.0.4.2 范围边界

| 项目 | V16 系列要做 | V16 系列不做 |
|---|---|---|
| OmniAuto RPA Sidecar | 继续使用 `sessions` 扫当前可见区；`messages` 支持第一屏可见读取和基于 `remark_code` 的微信搜索框定向读取。 | 不新增跨屏滚动兜底模式。 |
| Worker 客户端 | 实现本地调度：第一屏扫描、第一屏命中读取、状态机短码搜索定向读取、召回前 precheck、发送前 pre_send_refresh。 | 不自己实现 OCR/滚动算法，不绕过 OmniAuto；不把 `rpa_session_key` 当成跨屏稳定 ID。 |
| 后端服务 | 基于会话状态、时间字段和召回规则生成 `read-targets`；消息入库后触发 AI/转人工/召回判断。 | 不直接控制微信，不依赖 Worker 全量遍历微信列表。 |
| C3 回归 | C2 定向读取稳定后回归 C3 AI 回复发送。 | 不在 C2 未稳定时直接通过 C3 正式验收。 |

#### 6.0.4.3 Worker本地队列

Worker 本地维护四类 C2/C3 队列：

| 队列 | 来源 | 作用 |
|---|---|---|
| `visible_hit_queue` | 第一屏 `sessions` 扫描命中。 | 优先读取当前第一屏最新消息。 |
| `state_target_queue` | 服务端 `/wechat/sessions/read-targets`。 | 空闲时读取已知重点客户；第一屏可见则快速读取，非第一屏必须按 `remark_code` 搜索定位。 |
| `recall_precheck_queue` | 服务端召回到期判断。 | 召回前确认客户是否已有新消息。 |
| `send_queue` | 服务端 `chat_reply / follow_up` 任务。 | 只发送服务端已批准且仍有效的内容。 |

队列去重规则：

```text
visible_hit_queue 优先。
state_target_queue 必须去掉 visible_hit_queue 本轮已处理对象。
recall_precheck_queue 如果对应会话刚在 visible_hit_queue 或 state_target_queue 读到新客户消息，则取消本轮召回前检查。
去重键使用 conversation_id + remark_code。rpa_session_key / display_name 仅用于第一屏可见会话快速定位和排查证据；remark_code 是非第一屏微信搜索定位和身份确认的主锚点。
```

#### 6.0.4.4 发送前 pre_send_refresh

`chat_reply` 发送前必须做短读取，避免把旧上下文生成的回复发出去。

流程：

```text
Worker claim chat_reply/reply_action
-> 获取目标 conversation_id / remark_code
-> pre_send_refresh 定向读取该会话最新消息；第一屏可见则快速读取，非第一屏则搜索 remark_code
-> 服务端按 dedupe_key 入库
-> 服务端判断 reply_action 是否仍有效
```

判断分支：

| 结果 | 处理 |
|---|---|
| 没有新客户消息 | 允许 Worker 发送原 reply_action。 |
| 有新客户消息 | 原 reply_action 置为 `superseded`，不发送；新消息进入 message_batch 重新生成回复。 |
| 读取失败/目标不确认 | 不发送，返回错误码，等待重试或人工处理。 |

#### 6.0.4.5 召回前 recall_precheck

`follow_up` 任务不能仅按时间直接创建或发送。服务端发现召回到期后，先生成 `read_reason=recall_precheck` 的读取目标。

流程：

```text
等待用户回复超过N天
-> 服务端返回 recall_precheck read-target
-> Worker 定向读取该会话
-> 服务端去重入库并重新判断
-> 无新客户消息才创建 follow_up 任务
```

判断分支：

| 结果 | 处理 |
|---|---|
| 读到客户新消息 | 取消召回，进入 C3 AI 回复/转人工判断。 |
| 未读到新客户消息 | 创建 `follow_up` 任务，Worker 发送固定召回文案。 |
| 读到销售人工回复 | 不召回，状态进入 `sales_replied_waiting_user`。 |
| 读取失败/找不到会话 | 不召回，记录 `RECALL_PRECHECK_FAILED` 或具体错误码。 |

#### 6.0.4.6 V16验收标准

V16 系列通过标准：

```text
1. Worker 能优先扫描微信当前第一屏并上报 session_scan_result。
2. 第一屏命中会话能优先读取并上报 message_event。
3. 服务端 read-targets 能按状态机返回非第一屏已知客户。
4. Worker 能去重掉本轮第一屏已处理对象后再执行状态机定向读取。
5. 非第一屏定向读取必须通过微信搜索框搜索 remark_code，并在标题/备注二次确认短码后读取。
6. rpa_session_key 只作为第一屏可见会话辅助定位，不作为跨屏定向读取依据。
7. chat_reply 发送前执行 pre_send_refresh，新客户消息出现时旧 reply_action 不发送。
8. 召回到期先执行 recall_precheck，确认无新客户消息后才创建/发送 follow_up。
9. 不执行旧滚动兜底方案。
10. 重复读取不会重复入库、重复触发 AI、重复发送回复。
11. 找不到目标会话、搜索多义、标题不含短码或 OCR 低置信时不乱读、不乱点、不乱发。
12. V16 定向读取通过后再回归 C3 AI 回复发送链路。
```

### 6.1 C2数据结构

#### 6.1.1 wechat_session_bindings

| 字段 | 说明 |
|---|---|
| `id` | 绑定记录 ID。 |
| `lead_id` | 线索 ID。 |
| `conversation_id` | 会话 ID。 |
| `sales_id` | 销售 ID。 |
| `worker_id` | Worker ID。 |
| `remark_code` | 系统生成的客户短码。 |
| `display_name` | 微信会话列表展示名称。 |
| `rpa_session_key` | Worker/OmniAuto 根据当前可见会话行生成的本地定位键，只用于第一屏辅助定位和排查证据，不是跨屏稳定 ID。 |
| `row_fingerprint` | 会话列表行特征，用于辅助定位和变更检测。 |
| `bind_status` | `bound / binding_failed / needs_review / unbound / disabled`。 |
| `listen_status` | `not_started / listening / paused / degraded / error / disabled`。 |
| `allow_listening` | 是否允许 Worker 读取该会话消息。 |
| `error_code` | 绑定、监听或读取失败原因；C2 不再使用 `reason_code`。 |
| `first_seen_at` | 首次扫描到时间。 |
| `last_seen_at` | 最近扫描到时间。 |
| `last_scan_snapshot` | 最近一次扫描摘要或截图证据引用。 |

#### 6.1.2 message_events

| 字段 | 说明 |
|---|---|
| `id` | 消息事件 ID。 |
| `conversation_id` | 会话 ID。 |
| `worker_id` | Worker ID。 |
| `rpa_session_key` | 本机会话定位键。 |
| `dedupe_key` | 去重键，同一消息只能处理一次。 |
| `sender_role` | `customer / self / sales / sales_candidate / unknown`。`customer` 才允许进入 C3 AI 回复判断；`self / sales / sales_candidate` 视为销售侧消息，不触发 AI，并按人工接管/AI 暂停处理；`unknown` 不触发 AI。 |
| `message_type` | `text / image / system / file / unknown`。 |
| `content` | 文本内容或消息摘要。 |
| `raw_payload` | OmniAuto 原始结构化结果。 |
| `ocr_confidence` | OCR 置信度。 |
| `occurred_at` | 微信侧推断时间。 |
| `ingested_at` | 服务端入库时间。 |

说明：`message_events` 只保存已经入库的消息事实，不保存重复、忽略或失败记录。重复、忽略、失败属于本次接口处理结果，必须放在接口响应的 `results[].ingest_result` 和 `error_code` 中。

### 6.2 C2服务端接口契约

C2 接口只接收 Worker 上报的微信事实，不接收 AI 回复内容，不下发自动发送动作。

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/workers/{worker_id}/wechat/sessions/scan-result` | Worker 上报会话扫描结果。 |
| GET | `/api/workers/{worker_id}/wechat/sessions/read-targets?limit=20` | Worker 拉取需要读取消息的已绑定会话。 |
| POST | `/api/workers/{worker_id}/wechat/messages/ingest` | Worker 上报已绑定会话的消息事件。 |
| GET | `/api/conversations/{conversation_id}/wechat-binding` | 后台查询微信绑定状态。 |
| GET | `/api/conversations/{conversation_id}/messages` | 后台查询消息入库记录。 |

所有 Worker 接口必须携带：

| Header | 必填 | 说明 |
|---|---|---|
| `X-Worker-Token` | 是 | Worker 客户端 token。 |
| `X-Client-Instance-Id` | 是 | 当前客户端实例 ID。 |
| `X-Request-Id` | 否 | 链路追踪 ID；未传则服务端生成。 |

#### 6.2.1 上报会话扫描结果

```http
POST /api/workers/{worker_id}/wechat/sessions/scan-result
```

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `scan_id` | string | 是 | Worker 本次扫描 ID，幂等键之一。 |
| `sidecar_run_id` | string | 是 | OmniAuto Sidecar 本次运行 ID。 |
| `wechat_account_hint` | string | 否 | 微信账号提示，不作为身份依据。 |
| `started_at` | datetime | 是 | 扫描开始时间。 |
| `finished_at` | datetime | 是 | 扫描结束时间。 |
| `sessions` | array | 是 | 扫描到的会话列表。 |
| `evidence` | object | 否 | 截图、日志片段、OCR摘要。 |

`sessions[]` 字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `rpa_session_key` | string | 是 | Worker/OmniAuto 生成的本机会话定位键。 |
| `display_name` | string | 是 | 微信会话列表展示名。 |
| `remark_code_candidates` | array | 是 | 从展示名/备注中识别出的短码候选。 |
| `row_fingerprint` | string | 否 | 会话列表行特征，仅作辅助证据，不作为绑定或定向读取必填条件。 |
| `unread_hint` | boolean | 否 | 是否疑似未读。 |
| `last_message_preview` | string | 否 | 列表预览文本。 |
| `ocr_confidence` | number | 否 | OCR 置信度。 |

响应字段：

| 字段 | 说明 |
|---|---|
| `accepted_count` | 接收的会话数量。 |
| `bound_count` | 成功绑定或确认已绑定数量。 |
| `needs_review_count` | 需要人工检查数量。 |
| `bindings[]` | 每个会话的绑定处理结果。 |

`bindings[]` 字段：

| 字段 | 说明 |
|---|---|
| `rpa_session_key` | 本机会话定位键。 |
| `conversation_id` | 绑定成功时返回。 |
| `lead_id` | 绑定成功时返回。 |
| `bind_status` | `bound / already_bound / unbound / needs_review / binding_failed`。 |
| `error_code` | 未绑定、绑定失败或需要人工检查的原因。 |
| `can_ingest_messages` | 是否允许 Worker 后续读取并上报消息。 |

#### 6.2.2 拉取需读取消息的会话

```http
GET /api/workers/{worker_id}/wechat/sessions/read-targets?limit=20
```

响应字段：

| 字段 | 说明 |
|---|---|
| `targets[]` | 待读取会话列表。 |
| `poll_after_seconds` | 建议下次拉取间隔。 |

`targets[]` 字段：

| 字段 | 说明 |
|---|---|
| `conversation_id` | 会话 ID。 |
| `lead_id` | 线索 ID。 |
| `remark_code` | 系统客户短码，定向读取的身份校验锚点；必填。 |
| `rpa_session_key` | 本机会话定位键；第一屏快速读取可用，非第一屏不得依赖它作为唯一定位依据。 |
| `display_name` | 微信展示名。 |
| `row_fingerprint` | 会话列表行特征，用于辅助定位和变更检测；可选。 |
| `ocr_confidence` | 最近一次绑定/扫描的 OCR 置信度。 |
| `last_ingested_at` | 服务端最后入库消息时间。 |
| `read_reason` | `recall_precheck / recent_ai_sent / waiting_user_reply / waiting_sales_reply`。 |

契约要求：

- 正常 `read-targets.targets[]` 必须包含 `remark_code`。
- 已绑定会话如果缺少 `remark_code`，不得出现在正常 `read-targets` 中，应进入 `needs_review / degraded` 并记录 `C2_TARGET_REMARK_CODE_MISSING`。
- Worker 收到缺少 `remark_code` 的读取目标时，必须跳过读取，不得继续打开微信会话或上报消息。
- Worker 本轮读取去重以 `conversation_id + remark_code` 作为身份键；服务端身份收口必须满足 `conversation_id + remark_code`。
- 微信定位分两段：第一屏可见目标可用 `rpa_session_key / display_name / remark_code` 快速定位；第一屏未命中或非第一屏目标必须通过微信搜索框搜索 `remark_code`，并在进入会话后再次确认标题/备注包含该短码。
- `rpa_session_key`、`display_name`、`row_fingerprint` 仅作辅助定位和排查证据，不能替代 `remark_code` 做非第一屏定向读取。

#### 6.2.3 上报消息事件

```http
POST /api/workers/{worker_id}/wechat/messages/ingest
```

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `read_run_id` | string | 是 | 本次读取运行 ID。 |
| `conversation_id` | string | 是 | 服务端已绑定会话 ID。 |
| `rpa_session_key` | string | 否 | 本机会话定位键；第一屏读取时建议上报，短码搜索读取时可为空或上报搜索后重新识别到的定位键。 |
| `messages` | array | 是 | 本次读取到的消息事实。 |
| `evidence` | object | 否 | 截图、日志、OCR摘要。 |

`messages[]` 字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `dedupe_key` | string | 是 | 消息去重键；同一会话内唯一。 |
| `sender_role_hint` | string | 是 | `customer / self / sales / sales_candidate / unknown`。Worker 上报该字段，后端落库为 `message_events.sender_role`。 |
| `message_type` | string | 是 | `text / image / system / file / unknown`。 |
| `content` | string | 否 | 文本内容或消息摘要。 |
| `image_local_path` | string | 否 | 图片本地路径；C2 只记录，不做视觉识别。 |
| `occurred_at` | datetime | 否 | 微信侧推断时间。 |
| `ocr_confidence` | number | 否 | OCR 置信度。 |
| `raw_payload` | object | 否 | OmniAuto 原始结构化结果。 |

响应字段：

| 字段 | 说明 |
|---|---|
| `ingested_count` | 新入库消息数量。 |
| `duplicated_count` | 被去重跳过数量。 |
| `ignored_count` | 因状态不允许或发送方不明确跳过数量。 |
| `results[]` | 每条消息的处理结果，包含 `dedupe_key`、`ingest_result=ingested/duplicated/ignored`、`message_event_id`、`error_code`。 |
| `next_action` | C2 固定为 `none`；不返回 AI 发送动作。 |

### 6.3 C2状态流转

#### 6.3.1 微信绑定状态 `wechat_session_binding.bind_status`

| 状态 | 含义 | 进入条件 | 后续动作 |
|---|---|---|---|
| `unbound` | 尚未绑定微信会话。 | 线索已分配但未扫描到有效短码。 | 不读取消息，不自动回复。 |
| `binding_candidate` | 扫描到短码候选，等待服务端校验。 | Worker 上报 `remark_code_candidates`。 | 服务端校验 lead/sales/worker。 |
| `bound` | 已绑定唯一会话。 | 短码唯一匹配 lead/conversation/sales/worker。 | 允许进入消息读取。 |
| `needs_review` | 需要人工检查。 | 短码冲突、低置信度、会话特征异常。 | 不自动回复，可后台查看原因。 |
| `binding_failed` | 本次绑定失败。 | 微信不可控、扫描失败、短码无效等。 | 等待下次扫描或人工处理。 |
| `disabled` | 不再由系统监听。 | 客户拒绝、会话关闭、短码被销售移除。 | 停止读取，不自动回复。 |

#### 6.3.2 微信会话监听状态 `wechat_session_binding.listen_status`

| 状态 | 含义 | 进入条件 | 展示口径 |
|---|---|---|---|
| `not_started` | 未开始监听。 | 未绑定会话。 | 未绑定。 |
| `listening` | 正常监听。 | `wechat_session_binding.bind_status=bound` 且 Worker 在线可控。 | 监听中。 |
| `paused` | 暂停监听。 | Worker 暂停、全局开关关闭、静默规则要求。 | 已暂停。 |
| `degraded` | 降级监听。 | OCR 低置信、读取失败但未完全不可用。 | 监听异常。 |
| `error` | 监听失败。 | 连续扫描/读取失败或微信不可控。 | 失败，需处理。 |
| `disabled` | 不再监听。 | 会话关闭、拒绝、短码移除。 | 已停止。 |

#### 6.3.3 消息入库响应结果 `results[].ingest_result`

| 结果 | 含义 |
|---|---|
| `ingested` | 新消息已入库，并生成 `message_event_id`。 |
| `duplicated` | `dedupe_key` 已存在，跳过；不新增 `message_event`。 |
| `ignored` | 系统消息、低价值消息或状态不允许处理；不新增 `message_event`。 |

状态原则：

- `bound` 是 C2 后续读取消息的唯一正常入口。
- `needs_review / binding_failed / unbound` 均不得触发 AI 回复。
- `disabled` 优先级最高；会话关闭、拒绝或短码移除后，后续扫描不得重新开启监听，除非人工重新生成/恢复短码并重新绑定。
- C2 不创建 `reply_action`；即使消息成功入库，响应也只能返回 `next_action=none`。
- 服务端是状态唯一事实源；Worker 只上报扫描和消息事实，不直接改变最终业务状态。

### 6.4 C2错误码

| 错误码 | 触发场景 | 处理 |
|---|---|---|
| `SESSION_SCAN_FAILED` | 会话列表扫描失败。 | 记录证据，监听状态进入 `degraded/error`。 |
| `SESSION_REMARK_CODE_NOT_FOUND` | 会话未识别到短码。 | `bind_status=unbound`。 |
| `SESSION_REMARK_CODE_INVALID` | 短码格式非法或不存在。 | `bind_status=binding_failed`。 |
| `SESSION_REMARK_CODE_DUPLICATED` | 一个短码匹配多条活动线索。 | `bind_status=needs_review`。 |
| `SESSION_BINDING_CONFLICT` | 短码匹配到其他销售或 Worker。 | `bind_status=needs_review`。 |
| `SESSION_BINDING_DISABLED` | 会话已关闭、拒绝或短码已移除。 | `bind_status=disabled`。 |
| `C2_TARGET_REMARK_CODE_MISSING` | 已绑定会话缺少 `remark_code`，不能作为定向读取目标。 | 不返回正常 read-target；进入 `degraded/needs_review` 并提示修复绑定数据。 |
| `C2_TARGET_CONVERSATION_ID_MISSING` | 已绑定会话缺少 `conversation_id`，不能作为定向读取目标。 | 不返回正常 read-target；进入 `degraded/needs_review` 并提示修复绑定数据。 |
| `MESSAGE_READ_FAILED` | 已绑定会话读取消息失败。 | 记录证据，不改变业务状态。 |
| `MESSAGE_CONVERSATION_NOT_BOUND` | 上报消息的会话未绑定。 | 拒绝入库。 |
| `MESSAGE_DEDUPE_KEY_MISSING` | 消息缺少去重键。 | 拒绝入库。 |
| `MESSAGE_INGEST_DUPLICATED` | 去重键已存在。 | 返回 duplicated，不算失败。 |
| `WECHAT_WINDOW_NOT_READY` | 微信窗口不可控。 | Worker 状态异常，暂停读取。 |
| `RPA_SIDECAR_TIMEOUT` | OmniAuto Sidecar 超时。 | 记录证据，不自动重放。 |

### 6.5 C2验收标准

- Worker 能调用 OmniAuto 扫描微信会话列表，并上传结构化扫描结果和证据。
- 微信备注中包含有效客户短码时，服务端能绑定唯一 lead/conversation/sales/worker。
- 未绑定或绑定冲突的会话不触发 AI 回复。
- Worker 能读取已绑定会话的客户文字消息并上报服务端。
- 同一消息在 Worker 重启、断网恢复、重复扫描时不会重复入库和重复触发后续动作。
- 会话绑定失败、消息读取失败、微信窗口不可控、Sidecar 超时均有错误码、trace_id 和可查看证据。
- 后端、Worker/RPA、前端均按本模块接口字段、状态枚举和错误码实现，不允许各自新增同义状态。

### 6.6 销售人工回复检测

```text
Worker发送AI回复前登记reply_action_id、reply_text_hash、send_started_at、send_finished_at。
桌面端同步出我方消息时统一标记为 `self / sales / sales_candidate` 中的一种销售侧消息；后端不再把 `ai_worker` 作为主字段值使用。
否则视为sales_reply，Conversation状态转`sales_replied_waiting_user`，`conversation.ai_enabled=false`。
```

### 6.7 重启恢复

- Worker 启动后读取本地快照，再向服务端确认真实状态，以服务端为准。
- 未完成 `reply_action` 或 Worker 本地发送记录必须检查是否已发送、是否过期、是否仍允许 AI、是否已有销售人工消息、是否有客户新消息覆盖旧上下文。
- 不满足条件时跳过、重新生成或转人工；禁止盲发旧回复。

### 6.8 消息批处理与旧回复作废规则

本节为 C3 预埋概要，正式开发以“模块6：AI对话模块”的 C3 接口、状态机、错误码、幂等和防重复发送设计为准。

本规则由服务端会话调度器执行，不由大模型判断。模型只接收服务端整理后的最新 `message_batch` 和 `evidence_pack`，用于生成候选回复。

| 规则 | 说明 |
|---|---|
| 单会话唯一 active_batch | 同一 `conversation_id` 同一时间只能有一个 `active_batch`。 |
| 会话内合并 | 同一客户短时间多条消息合并为一个 `message_batch`；合并窗口和最大等待时间配置化。 |
| 会话间排队 | 不同客户的 batch 按首条消息到达时间排队；同会话新消息更新当前 batch，不改变初始排队位置。 |
| 生成中收到新消息 | 若 A1 已进入 AI 生成但 `reply_action` 尚未 `sent`，A2 到来后并入当前 batch，旧 `reply_action` 标记 `superseded/cancelled`，并基于 A1+A2 重新生成。 |
| Worker 可执行动作 | Worker 只能执行最新有效 `reply_action_id`；`superseded`、`cancelled`、`expired`、`sent` 状态均不得发送。 |
| sending 状态 | 若旧 `reply_action` 已进入 `sending` 或 Worker 已持有 UI 锁开始输入/发送，不强行取消；等待 `sent_ack` 或 `failed_ack`，A2 进入下一轮 batch。 |
| 已发送后新消息 | 旧 `reply_action` 已 `sent` 后，新消息创建下一轮 batch，不撤回已发送消息。 |
| 模型职责边界 | 模型不判断是否取消旧回复、不判断 batch 合并、不决定发送顺序，只生成候选回复。 |

```text
示例：A1、B1、C1、A2依次到达。
若A1尚未发送，则A_batch=[A1,A2]，B_batch=[B1]，C_batch=[C1]；
发送顺序按首条消息到达时间建议为A、B、C。
```

### 6.9 服务端事务与发送确认

本节为 C3 预埋概要，正式开发以“模块6：AI对话模块”的 C3 接口、状态机、错误码、幂等和防重复发送设计为准。

| 步骤 | 事务/状态要求 |
|---|---|
| 接收消息 | 先写 `message_event` 唯一键；重复 `dedupe_key` 直接返回已处理结果，不再创建 batch。 |
| 合并batch | 在 `conversation_id` 维度加行级锁或等效互斥；更新 `active_batch` 版本号 `batch_version`。 |
| 生成回复 | 生成完成时检查 `batch_version` 未变化、`conversation.ai_enabled=true`、`conversation.status` 未进入 `waiting_sales_reply/sales_replied_waiting_user`；否则标记 `superseded`。 |
| 下发Worker | 只下发 `status=current` 且未过期的 `reply_action`；同时写入 dispatch 记录。 |
| Worker发送前 | 再次向服务端 claim `reply_action`；服务端原子更新 `queued -> sending`，失败则 Worker 不得发送。 |
| Worker发送后 | 写入 `sent_ack`；`sent_ack` 成功后 `reply_action` 变 `sent`。Worker 本地失败但服务端未知时，恢复时必须先查 `sent_ack`。 |
| 恢复扫描 | `sending` 超时进入 `unknown_send_result`，不自动补发；需要 Worker 截图/人工确认或重新生成下一轮回复。 |

### 6.10 验收

- 可绑定微信会话；未绑定会话不自动回复。
- 客户文字和图片可识别并上传。
- Worker 发送的 AI 消息不会误判为销售人工回复。
- 销售手机端人工回复后 AI 停止。
- 同一客户短时间多条消息可合并为一个 `message_batch`。
- 生成中但未发送的旧 `reply_action` 在新消息到来后会被 `superseded/cancelled`，不会被 Worker 发送。
- `reply_action` 从 `queued` 到 `sending` 再到 `sent_ack` 必须有服务端原子状态流转。
- 重启/断网恢复后不重复发送同一 `reply_action_id`。

## 7. 模块6：AI对话模块

- 目标：根据客户消息、上下文、知识库、车源事实和规则生成候选回复或接管建议。
- 第一版模型使用 DeepSeek；模型调用、RAG、车源检索、Guard 均在服务端。
- OmniAuto 不是只作为 RPA 使用；C3 AI文字回复阶段必须复用 OmniAuto AI Engine 的 `customer_service_brain`、RAG、Evidence Pack、Guard、回复生成/润色等能力，但运行位置在服务端。
- OmniAuto 的 RPA Sidecar 只负责后续微信发送动作。两者在工程上必须分层：服务端决定回复内容和状态，Worker/Sidecar 只执行已批准动作。
- Dify/FastGPT Adapter 第一期只预留不实现，不接管主状态。
- OmniAuto 现有 RAG 能力需要先做代码评估；知识库资料由项目方整理。
- 模型失败直接转人工，不使用兜底话术继续自动回复。
- AI 只输出候选回复和动作建议，不拥有最终发送权。
- AI 文字回复属于 OmniAuto 接入 C3 checkpoint；C2 会话绑定/微信监听未验收前，不进入自动发送开发和验收。

### 7.0 服务端AI大脑内部职责拆分

第一期不把 AI 大脑拆成多个微服务，仍部署在同一个后端服务内；但代码和接口必须按职责拆模块，避免把知识库、模型调用、风控和发送动作写成一坨。

| 职责模块 | 运行位置 | 说明 |
|---|---|---|
| 会话上下文构建 | 服务端 | 汇总客户最近消息、销售状态、会话状态、历史 AI/销售回复，形成本轮模型输入。 |
| 知识库管理 | 服务端 | 管理正式知识、销售话术、禁说内容、资料来源、更新时间和负责人。 |
| RAG检索 | 服务端 | 基于 OmniAuto RAG 能力，从知识库、商品库、车源索引中召回相关证据；风险词必须走关键词加权。 |
| Evidence Pack | 服务端 | 把模型可见证据统一打包，过滤底价、采购价、手机号、内部备注等敏感字段。 |
| AI编排器 | 服务端 | 负责调用 OmniAuto Brain、DeepSeek、RAG、Guard 的顺序和重试策略。 |
| 回复生成 | 服务端 | 基于 `customer_service_brain`、`reply_synthesis` 和风格规则生成候选回复。 |
| Guard风控 | 服务端 | 判断候选回复是否可发、是否需要改写、是否必须转人工。 |
| ReplyAction | 服务端 | 把通过审批的回复固化为 `reply_action`，再由任务中心创建 `chat_reply` 任务。 |
| RPA发送 | Worker端 | Worker 调用 OmniAuto RPA Sidecar 打开会话、输入、发送并回传 `sent_ack`。 |

OmniAuto 原本的本地一体化链路不能原样运行在本系统中：

```text
监听微信 -> 读消息 -> RAG/AI生成 -> Guard -> RPA发送
```

本系统必须拆成服务端/客户端两段：

```text
Worker C2 读消息
  -> 服务端入库和去重
  -> 服务端 OmniAuto AI Engine 生成候选回复
  -> 服务端 Guard 审批
  -> 服务端创建 reply_action 和 chat_reply 任务
  -> Worker 调用 OmniAuto RPA Sidecar 发送
```

### 7.1 C3核心对象

C3 的核心不是“Worker 自动回复”，而是服务端把一次回复拆成可审计、可取消、可恢复的对象链。

```text
message_event
  -> message_batch
  -> OmniAuto AI Engine / RAG / Guard
  -> reply_action
  -> chat_reply task
  -> sent_ack
  -> conversation.status
```

| 对象 | 谁创建 | 作用 | 关键约束 |
|---|---|---|---|
| `conversation` | 服务端 | 承载客户/会话业务状态、AI开关、回复次数、转人工信息 | `status / ai_enabled / reply_count / handoff_reason_code / handoff_at` 只属于 Conversation，不写入微信绑定表。 |
| `message_event` | C2 消息入库接口 | 保存客户/销售/AI消息事实 | 已由 C2 用 `dedupe_key` 去重；C3 不重复入库。 |
| `message_batch` | 服务端会话调度器 | 把同一会话短时间内的客户消息合并成一次 AI 输入 | 同一 `conversation_id` 同一时间最多一个 active batch。 |
| `reply_action` | 服务端 AI 编排器 | 表示服务端批准的一次候选回复或转人工决策 | Worker 只能发送 `status=queued` 且 claim 成功的 action。 |
| `chat_reply task` | 服务端任务中心 | 让 Worker 执行微信发送动作 | `task_type=chat_reply`，必须绑定唯一 `reply_action_id`。 |
| `sent_ack` | Worker | 证明某个 `reply_action` 已发送或发送失败 | `reply_action_id` 唯一；重复上报只返回既有结果。 |
| `handoff_event` | 服务端 | 记录转人工原因、触发消息、通知结果 | 转人工后 `conversation.ai_enabled=false`，不创建 `chat_reply` 发送任务。 |

#### 7.1.1 Conversation正式字段

`Conversation` 是业务会话状态的唯一承载对象。`WechatSessionBinding` 只保存微信会话绑定和监听信息，不保存以下业务状态字段。

| 字段 | 说明 |
|---|---|
| `conversation_id` | 会话 ID。 |
| `lead_id` | 线索 ID。 |
| `sales_id` | 销售 ID。 |
| `worker_id` | 当前绑定 Worker ID。 |
| `status` | 客户/会话业务状态，例如 `ai_active / waiting_user_reply / waiting_sales_reply / sales_replied_waiting_user / rejected / closed`。 |
| `ai_enabled` | 是否允许 AI 自主回复。 |
| `reply_count` | AI 成功发送回复次数。 |
| `handoff_reason_code` | 最近一次转人工原因码。 |
| `handoff_at` | 最近一次进入等待销售回复的时间。 |
| `last_inbound_at` | 最近客户消息时间。 |
| `last_outbound_at` | 最近我方消息时间，包括 AI、召回和销售人工消息。 |
| `last_ai_reply_at` | 最近 AI 回复时间。 |
| `last_sales_reply_at` | 最近销售人工回复时间。 |
| `sales_first_reply_at` | 本轮转人工后销售首次回复时间。 |
| `close_reason` | 关闭自动跟进原因。 |

迁移要求：

- 新增 C3 能力时，`conversation.status / ai_enabled / reply_count / handoff_reason_code / handoff_at` 必须从业务会话对象读取和写入。
- 不允许继续把上述字段写入 `WechatSessionBinding`。
- 旧数据如已临时写入微信绑定表，迁移时应搬迁到 `Conversation`，并从绑定表删除或停止使用。

### 7.2 C3状态机

#### 7.2.1 message_batch.status

| 状态 | 含义 | 允许流转 |
|---|---|---|
| `collecting` | 正在收集同一会话短时间内连续客户消息 | `generating / cancelled` |
| `generating` | 已冻结本批消息，正在构建上下文、RAG 和模型回复 | `reply_action_created / handoff_created / no_action / superseded / failed` |
| `reply_action_created` | 已生成可发送回复，并创建 `reply_action` | 终态 |
| `handoff_created` | 已判断需要转人工，并创建 `handoff_event` | 终态 |
| `no_action` | 判断无需回复，例如客户无效闲聊、静默、策略跳过 | 终态 |
| `superseded` | 生成期间来了新消息，本批被新 batch 取代 | 终态 |
| `cancelled` | 会话关闭、拒绝、人工接管或短码移除导致取消 | 终态 |
| `failed` | 上下文、RAG、模型或 Guard 异常 | 终态，默认转人工或记录错误。 |

#### 7.2.2 reply_action.status

| 状态 | 含义 | 允许流转 |
|---|---|---|
| `draft` | 服务端内部生成中，未允许 Worker 看到 | `guarding / cancelled` |
| `guarding` | Guard 审核中 | `queued / handoff / blocked / failed` |
| `queued` | 已通过 Guard，等待创建/领取 `chat_reply` 任务 | `sending / superseded / expired / cancelled` |
| `sending` | Worker 已 claim，准备或正在操作微信发送 | `sent / failed / unknown_send_result` |
| `sent` | Worker 已上报 `sent_ack` 成功 | 终态 |
| `failed` | Worker 明确发送失败 | 终态，不自动重发同一 action。 |
| `unknown_send_result` | Worker 断网、崩溃或超时，无法确认是否已发 | 终态，禁止自动补发。 |
| `superseded` | 发送前客户来了新消息，被新回复取代 | 终态 |
| `expired` | 超过 `expire_at` 未发送 | 终态 |
| `cancelled` | 会话状态变化或人工接管导致取消 | 终态 |
| `handoff` | Guard 或 AI 判断需要转人工 | 终态，不创建发送任务。 |
| `blocked` | Guard 阻断且不允许改写发送 | 终态，默认转人工。 |

#### 7.2.3 chat_reply task.status

`chat_reply` 继续使用统一任务中心状态，不新增特殊任务状态。

| 状态 | C3含义 |
|---|---|
| `pending` | 已创建发送任务，等待 Worker 领取。 |
| `running` | Worker 已领取任务，正在发送。 |
| `completed` | Worker 上报成功，`result_code=chat_reply_sent`。 |
| `failed` | Worker 明确发送失败，写入 `error_code`。 |
| `cancelled` | 对应 `reply_action` 已取消、过期或被取代。 |

禁止把 `chat_reply_sent` 写成 `task.status`；它只能写入 `task.result_code`。

#### 7.2.4 handoff_event.status

| 状态 | 含义 |
|---|---|
| `created` | 服务端已判定转人工。 |
| `notify_pending` | 等待通知销售；如果当前阶段未启用飞书，也可停留在 `created` 并在后台展示。 |
| `notified` | 已通知销售。 |
| `notify_failed` | 通知失败，AI 仍保持停止。 |
| `sales_replied` | 后续检测到销售已回复。 |
| `closed` | 销售关闭托管、客户拒绝或会话结束。 |

### 7.3 C3接口定义

接口路径可按后端现有路由风格调整，但语义、入参、出参和幂等规则必须保持一致。

#### 7.3.1 C2消息入库后触发批处理

```text
POST /api/internal/conversations/{conversation_id}/message-batches/collect
```

触发方式：C2 `message_event` 入库成功后由服务端内部调用或后台扫描器调用，Worker 不直接调用。

请求核心字段：

| 字段 | 说明 |
|---|---|
| `conversation_id` | 会话ID。 |
| `trigger_message_event_id` | 触发本次收集的消息ID。 |
| `trace_id` | 全链路追踪ID。 |

响应核心字段：

| 字段 | 说明 |
|---|---|
| `batch_id` | 当前 active batch。 |
| `batch_status` | `collecting/generating/...`。 |
| `next_step` | `wait_more/generate/no_action/handoff`。 |

#### 7.3.2 服务端生成回复或转人工

```text
POST /api/internal/message-batches/{batch_id}/generate
```

只能由服务端任务/扫描器调用，不暴露给 Worker。

处理步骤：

```text
锁定 conversation
-> 校验 conversation.ai_enabled 和 conversation.status
-> 冻结 message_batch
-> 构建 conversation_context
-> RAG / 车源 / Evidence Pack
-> 调用 OmniAuto AI Engine Adapter
-> Guard 审核
-> 生成 reply_action 或 handoff_event
```

响应核心字段：

| 字段 | 说明 |
|---|---|
| `decision` | `send_reply / handoff / no_action / retry_later / pause`。 |
| `reply_action_id` | `decision=send_reply` 时返回。 |
| `handoff_event_id` | `decision=handoff` 时返回。 |
| `error_code` | 失败时返回。 |

#### 7.3.3 Worker领取chat_reply任务

沿用统一任务中心领取接口，但 C3 必须校验 `reply_action`。

```text
POST /api/tasks/{task_id}/claim
```

领取成功条件：

| 条件 | 说明 |
|---|---|
| task 是 `chat_reply` | `task_type=chat_reply`。 |
| Worker 与销售绑定正确 | 防止跨销售发送。 |
| 任务未过期 | `task.lease_expires_at` 有效。 |
| reply_action 可发送 | `reply_action.status=queued` 且 `expire_at` 未过期。 |
| 会话允许发送 | 未转人工、未关闭、未拒绝、短码仍有效。 |

#### 7.3.4 Worker发送前claim reply_action

```text
POST /api/reply-actions/{reply_action_id}/claim-send
```

这是防重复发送的核心接口。Worker 拿到 `chat_reply` 任务后，真正打开微信发送前必须再调用一次。

原子流转：

```text
queued -> sending
```

失败时 Worker 不得操作微信。

响应：

| 字段 | 说明 |
|---|---|
| `send_token` | 本次发送令牌，写入 Worker 本地快照和 `sent_ack`。 |
| `reply_text` | 服务端批准后的最终可见回复文本。 |
| `conversation_id` | 会话ID。 |
| `rpa_session_key` | Worker 定位微信会话使用。 |
| `expire_at` | 过期时间。 |
| `reply_text_hash` | 发送前后校验用。 |

#### 7.3.5 Worker发送回执 sent_ack

```text
POST /api/reply-actions/{reply_action_id}/sent-ack
```

请求核心字段：

| 字段 | 说明 |
|---|---|
| `send_token` | `claim-send` 返回的令牌。 |
| `task_id` | 对应 `chat_reply` 任务。 |
| `worker_id` | Worker ID。 |
| `client_instance_id` | 客户端实例ID。 |
| `send_result` | `sent / failed / unknown`。 |
| `sent_at` | 成功发送时间。 |
| `reply_text_hash` | Worker 实际发送文本 hash。 |
| `sidecar_run_id` | OmniAuto RPA Sidecar 执行ID。 |
| `evidence` | 截图、日志、窗口标题、错误详情。 |
| `error_code` | 失败或未知时必填。 |

服务端处理：

| send_result | 服务端动作 |
|---|---|
| `sent` | `reply_action.status=sent`，`task.status=completed`，`result_code=chat_reply_sent`，`conversation.status=waiting_user_reply`。 |
| `failed` | `reply_action.status=failed`，`task.status=failed`，写入错误码，不自动重发同一 action。 |
| `unknown` | `reply_action.status=unknown_send_result`，`task.status=failed`，禁止自动补发。 |

### 7.4 C3幂等与防重复发送

| 对象 | 幂等键/唯一约束 | 规则 |
|---|---|---|
| `message_event` | `unique(worker_id, rpa_session_key, dedupe_key)` 或 `unique(conversation_id, dedupe_key)` | 同一微信消息只入库一次。 |
| `message_batch` | `unique(conversation_id, active=true)` | 同一会话同一时间只有一个 active batch。 |
| `reply_action` | `unique(batch_id, generation_no)`，且同一 batch 只有一个 `current=true` | 旧 action 被取代时必须置为 `superseded/cancelled`。 |
| `chat_reply task` | `unique(reply_action_id)` | 同一个回复动作只能创建一个发送任务。 |
| `claim-send` | 原子比较更新 `queued -> sending` | 只有一个 Worker 能拿到发送权。 |
| `sent_ack` | `unique(reply_action_id)` | 同一个 action 只能确认一次；重复 ack 返回已有结果。 |
| `handoff_event` | `unique(conversation_id, handoff_reason_group, active_period)` | 同一接管周期不重复创建接管事件；转人工原因字段统一为 `handoff_reason_code`。 |

防重复发送硬规则：

1. Worker 只发送 `claim-send` 成功返回的 `reply_text`。
2. `reply_action.status` 不是 `queued` 时，Worker 不得发送。
3. `reply_action.expire_at` 过期后不得发送。
4. Worker 重启后，如果本地存在未完成发送记录，必须先查服务端 `reply_action` 和 `sent_ack`，不能直接补发。
5. `sending` 超时后进入 `unknown_send_result`，默认人工确认，不自动重放。
6. 客户新消息到来时，未发送的旧 `reply_action` 必须 `superseded/cancelled`，重新基于最新 batch 生成。
7. 销售人工回复、客户拒绝、短码移除、会话关闭后，所有未发送 `reply_action` 和 `chat_reply task` 必须取消。

### 7.5 OmniAuto AI Engine接入边界

OmniAuto AI Engine 在服务端通过 Adapter 接入，不允许运行 OmniAuto 原本的本地监听/发送一体化循环。

| 边界项 | 规定 |
|---|---|
| 运行位置 | 服务端后端进程内或服务端内部模块；第一期不拆独立微服务。 |
| 调用方式 | 后端通过 `OmniAutoAIEngineAdapter` 调用 `customer_service_brain / RAG / evidence / guard / reply_synthesis` 等能力。 |
| 输入 | `conversation_context`、`message_batch`、`evidence_pack`、`risk_policy`、`vehicle_candidates`、`allowed_fields`。 |
| 输出 | 严格结构化 JSON：`decision`、`reply_text`、`confidence`、`handoff_reason_code`、`risk_flags`、`evidence_refs`、`rewrite_required`。 |
| 禁止事项 | 不监听微信、不读取微信UI、不发送微信、不写业务主状态、不直接创建任务、不直接发飞书。 |
| 失败处理 | 超时、异常、输出不合法、证据不足时默认 `handoff`，不使用本地兜底话术继续自动回复。 |
| 审计 | 保存 prompt 版本、模型名、RAG命中、Evidence Pack 摘要、候选回复、Guard结果、最终 decision 和 trace_id。 |

Adapter 输出合同：

```json
{
  "decision": "send_reply",
  "reply_text": "可以，我先帮您看下需求。您主要看几万预算、轿车还是SUV？",
  "confidence": 0.86,
  "handoff_reason_code": null,
  "risk_flags": [],
  "evidence_refs": ["kb_001", "vehicle_1024"],
  "guard_result": "pass",
  "rewrite_required": false
}
```

`decision` 枚举：

| decision | 处理 |
|---|---|
| `send_reply` | Guard 通过后创建 `reply_action` 和 `chat_reply task`。 |
| `handoff` | 创建 `handoff_event`，`conversation.ai_enabled=false`，不创建发送任务。 |
| `no_action` | 本轮不回复，记录原因。 |
| `pause` | 暂停会话自动回复，等待人工处理。 |
| `retry_later` | 模型/依赖短暂异常，按配置短延迟重试；超过次数转人工。 |

### 7.6 C3错误码

| error_code | 场景 | 处理 |
|---|---|---|
| `CONVERSATION_NOT_ELIGIBLE` | 会话未绑定、已关闭、已拒绝、已转人工或短码无效 | 不生成回复。 |
| `MESSAGE_BATCH_SUPERSEDED` | batch 生成期间被新消息取代 | 旧 batch/action 作废，使用新 batch。 |
| `AI_CONTEXT_BUILD_FAILED` | 上下文构建失败 | 转人工，记录 trace_id。 |
| `AI_ENGINE_UNAVAILABLE` | OmniAuto AI Engine Adapter 不可用 | 转人工。 |
| `AI_ENGINE_TIMEOUT` | AI Engine 或 DeepSeek 超时 | 可短重试；超过次数转人工。 |
| `AI_ENGINE_CONTRACT_INVALID` | AI 输出不是合法结构化 JSON 或缺必要字段 | 转人工。 |
| `RAG_NO_EVIDENCE` | 无足够知识/车源证据支撑回复 | 转人工或 no_action，不编造。 |
| `GUARD_REWRITE_FAILED` | Guard 要求改写但改写失败 | 转人工。 |
| `GUARD_BLOCKED` | Guard 阻断发送 | 转人工。 |
| `REPLY_ACTION_EXPIRED` | 回复动作已过期 | Worker 不发送，任务取消。 |
| `REPLY_ACTION_SUPERSEDED` | 回复动作已被新消息取代 | Worker 不发送，任务取消。 |
| `REPLY_ACTION_CLAIM_CONFLICT` | 多 Worker 或重复请求抢同一 action | 只有一个成功，其余拒绝。 |
| `CHAT_REPLY_TASK_DUPLICATED` | 同一 `reply_action_id` 重复创建任务 | 返回已有任务。 |
| `SEND_ACK_DUPLICATED` | 同一 action 重复回执 | 返回已有 ack，不重复更新状态。 |
| `SEND_TEXT_HASH_MISMATCH` | Worker 回传文本 hash 与服务端批准文本不一致 | 标记失败，人工核查。 |
| `SEND_RESULT_UNKNOWN` | Worker 断网/崩溃/超时，无法确认是否发出 | 禁止自动补发，人工确认。 |
| `HANDOFF_REQUIRED` | 服务端判定必须转人工 | 创建 handoff_event，不是系统异常。 |
| `HANDOFF_NOTIFY_FAILED` | 转人工通知失败 | AI 仍停止，后台/日志展示错误。 |

### 7.7 C3验收标准

- 客户消息入库后，服务端能创建或更新 `message_batch`。
- 同一客户短时间连续消息能合并成一个 batch。
- A1 生成中 A2 到来时，旧 `reply_action` 被 `superseded/cancelled`，不会发送旧回复。
- Guard 通过时，服务端创建唯一 `reply_action` 和唯一 `chat_reply task`。
- Worker 发送前必须 `claim-send`；claim 失败不得操作微信。
- 同一 `reply_action_id` 不会被发送两次。
- Worker 成功发送后，`sent_ack` 能把状态更新为 `reply_action.sent`、`task.completed`、`conversation.waiting_user_reply`。
- Worker 失败或结果未知时，不自动补发同一内容。
- 模型失败、RAG 证据不足、Guard 阻断时，系统转人工或 no_action，不编造回复。
- 销售人工回复、客户拒绝、短码移除、会话关闭后，AI 不再创建发送任务。
- OmniAuto AI Engine 只在服务端生成候选回复；OmniAuto RPA Sidecar 只在 Worker 端发送已批准文本。

| 主题 | 设计 |
|---|---|
| RAG方式 | RAG + 语义检索 + 关键词加权检索。语义检索理解意思，关键词检索抓住泡水、火烧、事故、贷款、定金、底价等关键风险词。 |
| Evidence Pack | 包含 conversation_context、customer_message、retrieved_knowledge、matched_cars、image_intent、risk_flags、allowed_fields。 |
| AI可见车源字段 | 品牌、车系、车型、年份、里程、城市、颜色、燃料、配置摘要、对外可说价格、车辆图片。 |
| AI不可见字段 | 采购价、销售底价、经理价、车主姓名、手机号、身份证、银行卡、内部备注。 |
| 动作输出 | send_reply、handoff、no_action、pause、retry_later，均带 reply_action_id 和 expire_at。 |
| 调度边界 | `message_batch` 合并、旧 `reply_action` 作废、发送顺序和幂等判断由服务端会话调度器负责，不交给模型判断。 |
| 固定轮次限制 | 不设置固定 20 条自动停止规则；AI 是否继续由会话状态、风控、客户拒绝、人工接管、关闭托管和召回规则决定。 |

### 7.8 Guard检查

- 检查是否承诺无事故、无泡水、无火烧，是否承诺底价、最低价、贷款包过、定金可退，是否涉及合同、赔偿、投诉、法务，是否暴露系统规则或敏感字段。
- Guard 结果为 `pass`、`rewrite`、`handoff`、`block`；不通过时不发送原文。

| Guard层 | 说明 |
|---|---|
| 字段隔离 | 服务端构造 evidence pack 时先按白名单过滤，敏感字段不进入模型上下文。 |
| 规则检查 | 发送前使用规则词表检查底价、包过、绝对承诺、投诉法务等明确风险。 |
| 模型复核 | 对候选回复做二次安全判断，输出 `pass/rewrite/handoff/block` 及原因。 |
| 人工接管 | 规则或模型任一层判断 `handoff/block` 时，默认停止 AI 并触发接管。 |
| 审计记录 | 保存召回知识片段、候选回复、Guard 结论、改写原因和最终动作。 |

### 7.9 RAG与知识库验收口径

| 项目 | 验收口径 |
|---|---|
| OmniAuto评估 | 开发前完成现有RAG代码评估，输出可复用、需重构、需新增清单。 |
| 知识库标准 | 知识条目需有标题、适用场景、正文、禁说内容、更新时间、负责人；过期内容不得进入正式索引。 |
| 检索方式 | 采用语义检索+关键词加权；事故、泡水、火烧、底价、贷款、合同等风险词必须被关键词层召回。 |
| 低置信处理 | 知识不足、检索冲突、车源证据不足、模型不确定时转人工，不编造。 |
| 优化目标 | RAG命中率、误召回率、转人工率作为灰度期优化指标，不作为未经样本集验证的硬承诺。 |

## 8. 模块7：图片理解与图文回复

- 图片采集在 Worker 本地完成，图片理解在服务端完成。
- 第一版视觉模型使用千问视觉；低置信度全部转人工。
- 客户多张图片第一期逐张处理。
- 图片本地保存路径固定可配置；保存周期配置化，默认一年。
- 云端只保存必要文件和识别结果，不做长期图片库。
- 视觉模型只负责看懂图片，不直接生成最终客服话术。

```text
客户图片 -> Worker识别type=3 -> 点开另存 -> 上传服务端 -> 千问视觉 -> ImageIntent -> 车源索引 -> evidence pack -> OmniAuto候选回复 -> Guard -> send_reply或handoff
```

| ImageIntent字段 | 说明 |
|---|---|
| image_type | car_photo、car_listing_screenshot、price_screenshot、finance_screenshot、inspection_report、chat_screenshot、unrelated、unknown。 |
| detected_vehicle | 品牌、车系、车型、年份、颜色等。 |
| detected_price | 识别到的图片价格与置信度。 |
| customer_intent | find_similar_car、ask_price、ask_condition、ask_finance、compare_car、unknown。 |
| risk_flags | price、condition、finance、contract等。 |

- 图片保存失败、上传失败、视觉失败、低置信度均不强行图文回复，按规则转人工。
- 图片也必须遵守 `message_dedupe_key`、`image_dedupe_key`、`reply_action_id`、`sent_ack`，避免重复识别和重复回复。

## 9. 模块8：大风车与车源索引

该模块整体待确认。当前阶段应向大风车提供 API 需求清单，对照其开放接口确认是否满足，不足部分再列沟通清单。第一期不应写死接口细节。

| API需求 | 说明 |
|---|---|
| 店铺信息查询 | 根据 shopCode 查询店铺信息，确认门店权限和基础信息。 |
| 车辆ID列表 | 根据 shopCode 和 operationPhase 查询车辆ID，需 operationPhase 枚举说明和可售状态定义。 |
| 车辆详情 | 按 carId 查询品牌、车系、车型、年份、里程、颜色、配置、状态、对外展示价格等。 |
| 车辆图片 | 按 carId 查询图片URL、图片名称、图片类型、排序、大图/缩略图。 |
| 增量同步 | 确认是否支持按更新时间查询变更车辆、分页、车辆状态变更同步或 Webhook。 |
| 鉴权与限制 | 确认 appKey、appSecret、appId、shopCode、operator、IP白名单、频率限制、错误码、测试环境。 |

- 大风车作为权威车源系统，不作为图片搜车接口。
- 服务端同步后分为 `raw_vehicle` 原始数据层和 `vehicle_index` AI 可见索引层。
- AI 只读白名单字段；采购价、底价、经理价、车主隐私、内部备注默认隔离。
- 同步失败保留上次成功索引，不阻塞普通聊天；鉴权失败需告警。

### 9.1 Gate 0接口确认

| 确认项 | 未满足时处理 |
|---|---|
| 鉴权参数与IP白名单 | 不能联调大风车；改用本地车源导入/样本索引完成AI链路验收。 |
| operationPhase枚举与可售状态 | 不能自动判断在售范围；需人工配置可售状态白名单。 |
| 对外价格字段 | 不能让AI回答具体价格；价格问题默认转人工或只回复需销售确认。 |
| 车辆详情与图片字段 | 字段不足则降低图片找车准确性；按可用字段建索引并标注缺失。 |
| 增量同步能力 | 无增量接口时使用定时全量/车辆ID轮询；同步频率和成本需另行确认。 |
| 频率限制与错误码 | 无明确限制时按保守频率调用；错误原因不可识别时进入告警和人工确认。 |

## 10. 模块9：风控策略中心

- 风控属于云端业务控制面的核心子模块，但作为独立业务模块详细设计。
- 风控策略由服务端控制面配置和判定，Worker 执行服务端返回的动作，并展示命中原因。
- 第一期不承诺规避微信平台风控，不做复杂反检测和机器学习风控模型。

| 风控项 | 口径 |
|---|---|
| 自动回复总开关 | 可按全局、销售、Worker、会话控制；关闭后不自动回复和召回。 |
| 人工接管模式 | waiting_sales_reply 或 sales_replied_waiting_user 时 AI 必须停止。 |
| 静默时段 | 客户主动发消息也完全不自动回复；召回必须延期或跳过。 |
| 每日上限 | AI 回复、加好友、召回上限均配置化，默认待定。 |
| 黑名单 | 第一期支持，用于拒绝、投诉、无效、不再跟进客户。 |
| 白名单 | 预留或仅支持测试手机号，不能绕过高风险接管。 |
| 关键词拦截 | 投诉、报警、律师、退款、赔偿、诈骗、别联系等。 |
| 人工接管关键词 | 底价、事故、泡水、贷款、定金、合同、地址、现在定等，销售和项目方确认，最终项目方确认。 |
| 随机发送延迟 | 配置化，默认待定；仅体验优化，不承诺规避微信风控。 |
| 风险提示检测 | 操作频繁、环境异常、添加受限等出现后暂停任务并上报。 |
| 单会话突发限频 | 配置化，默认待定。 |
| 风险暂停恢复 | 支持人工解除或到期自动解除，默认人工确认更稳。 |

### 10.1 执行顺序

```text
总开关 -> 会话状态 -> 黑名单 -> 白名单 -> 静默时段 -> 每日上限 -> 单会话限频 -> 关键词拦截 -> 人工接管关键词 -> 模型/图片/车源异常 -> Guard发送前检查
```

## 11. 模块10：人工接管与飞书通知

- 目标：AI 停手，销售接上。
- 接管状态在云端控制面；飞书通知由服务端触发；Worker 停止该会话自动回复并展示状态。
- 第一期使用飞书机器人定向通知销售个人，不做短信通知。
- 接管后客户继续发消息不再次提醒销售；销售长时间不接管不做二次自动提醒。
- 第一期不做飞书重发按钮、不做“我已接管”按钮、不单独增加飞书通知角色和权限。

| 触发来源 | 说明 |
|---|---|
| 风控/关键词 | 高风险、高意向、投诉、金融、合同、底价等。 |
| 模型失败 | DeepSeek 超时或失败、RAG 失败、图片视觉失败、低置信度、车源失败无法安全回复。 |
| 销售主动回复 | 检测到销售手机端人工消息。 |
| 手动操作 | 控制面或 Worker 执行台点击停止 AI/手动接管。 |

- 进入 `waiting_sales_reply` 时必须 `conversation.ai_enabled=false`。
- 飞书通知包含客户标识、线索短码、手机号后四位、销售、触发原因、最近消息、建议动作、时间。
- 飞书通知失败时 AI 仍停止，控制面和 Worker 执行台展示错误日志，由项目方人工查看处理。
- 同一 `handoff_event_id` 只触发一次飞书通知，避免重复提醒销售。

### 11.1 飞书通知轻量实现

| 机制 | 要求 |
|---|---|
| 触发 | 服务端进入 `waiting_sales_reply` 后调用飞书机器人发送一次通知。 |
| 记录 | 在 `HandoffEvent.status` 中记录 `notified/notify_failed`，并保存请求时间、返回码、错误摘要。 |
| 失败处理 | 不做自动重发和手动重发；失败时保留错误日志，项目方自行查看并人工处理。 |
| 幂等 | 同一 `handoff_event_id` 只允许触发一次通知。 |
| 降级 | 飞书失败不恢复 AI 自动回复；会话继续保持接管状态。 |

## 12. 模块11：自动召回 follow_up

- 目标：对已添加微信、处于等待用户回复类状态、长期未互动且未拒绝的客户做低频再触达。
- 云端先判断召回到期资格，但不得直接创建/发送 `follow_up`；必须先生成 `recall_precheck` 读取目标。
- Worker 先定向读取该会话，服务端确认没有新客户消息后，才允许创建 `follow_up` 任务。
- Worker 只发送服务端已创建的固定召回文案并上报结果；第一期不让模型自由生成召回话术。
- 第一期只做一种召回规则；周期默认待定，可配置为 7 天或 14 天。
- 每个客户最多召回 1 次；每日召回上限待定；扫描频率每天一次。
- `watching` 不作为第一期必需主状态；观望客户可用 `waiting_user_reply / recalled_waiting_user` 加规则字段表达。

| 规则 | 说明 |
|---|---|
| 适用客户 | 已加好友、会话已绑定、处于 `waiting_user_reply / recalled_waiting_user / sales_replied_waiting_user` 等等待用户类状态、未拒绝、未关闭、未黑名单、最近N天无客户/销售消息。 |
| 召回前确认 | 到期后先进入 `recall_precheck`，Worker 定向读取该会话；读到新客户消息则取消召回。 |
| 排除条件 | rejected、waiting_sales_reply、closed、黑名单、近期客户/销售已联系、达到召回上限、风控暂停、静默时段。 |
| 发送 | 只有 `recall_precheck` 确认无新客户消息后，follow_up 才能获取 Local WeChat UI Lock 并发送固定文案。 |
| 防重复 | 同一客户同一规则周期只发送一次，同一 follow_up 任务只发送一次，重启后已 sent 不再发送。 |

## 13. 模块12：测试、验收与部署

- 核心验收原则：系统能正常回复，或在不能安全回复时触发人工接管。
- 性能目标不作为未经压测的硬承诺，最终以测试环境、账号状态、网络质量、模型服务和真实样本实测为准。
- 测试环境包含云端控制面、数据库、AI 服务、RAG/知识库、车源索引、风控配置、飞书机器人、商家侧 Windows 电脑、微信桌面端、Worker 执行台、销售手机微信和飞书。

| 测试阶段 | 内容 |
|---|---|
| P1基础链路 | 线索接入、销售分配、Worker绑定、add_friend、客户短码写入和邀请结果回传。 |
| P2会话绑定/微信监听 | OmniAuto sessions/messages、短码绑定、消息入库、dedupe_key去重、未绑定会话不自动回复。 |
| P3文字回复 | 客户文字、RAG、DeepSeek、Guard、`reply_action`、Worker调用OmniAuto发送、审计。 |
| P4图片回复 | 图片另存、上传、千问视觉、ImageIntent、车源索引、图文回复或转人工。 |
| P5风控接管 | 总开关、静默、上限、黑名单、关键词、模型失败、飞书通知、销售回复后AI停止。 |
| P6自动召回 | 等待用户回复类状态、N天未联系、召回前 `recall_precheck`、固定文案、上限、跳过原因、防重复。 |
| P7异常恢复 | Worker断网/重启、服务端不可用、AI超时、重复消息、reply_action恢复、不重复发送。 |

### 13.1 S1阻塞缺陷

- 无法加好友、无法监听消息、无法发送回复、AI无法停止、重复发送同一回复、转人工后仍自动回复、敏感字段泄露。

### 13.2 缺陷分级与外部依赖验收

| 级别 | 定义 | 处理 |
|---|---|---|
| S1阻塞 | 主链路不可用、重复发送、AI停不住、敏感字段泄露、错误接管后继续回复。 | 必须修复后验收。 |
| S2严重 | 部分场景失败但有人工降级，例如图片低置信过多、大风车同步失败但保留旧索引。 | 需给出修复计划或降级方案。 |
| S3一般 | 体验问题、配置默认值调整、页面展示不完整但不影响主链路。 | 可进入试运行问题清单。 |
| 外部依赖 | 微信版本变化、账号受限、模型服务故障、大风车未开放字段、飞书配置不可用。 | 按降级方案和责任边界处理，不直接归为内部开发缺陷。 |

### 13.3 必测用例补充

- A、B、C 同时来消息，A 追加第二条：验证同会话合并、跨会话排队、旧 `reply_action` 作废。
- AI 生成中断网、Worker 重启、服务端重启：验证不会重复发送。
- Worker 进入 `sending` 后异常退出：状态进入 `unknown_send_result`，不自动补发。
- 销售手机端回复后桌面端同步：验证 AI 停止且不再召回。
- 微信出现操作频繁/添加受限提示：验证 Worker 暂停、截图、告警、加好友不继续冲。
- 飞书发送失败：验证 AI 仍停止，`HandoffEvent` 记录失败状态和错误日志，控制面/Worker 执行台可见。
- 大风车鉴权失败/字段缺失/无可售车：验证按 Gate 0 降级，不编造车源。

## 14. 支撑模块

| 支撑模块 | 第一期口径 |
|---|---|
| 日志审计与数据留痕 | 记录任务、消息、RAG召回、候选回复、Guard、风控、飞书通知、Worker错误、人工操作，敏感字段脱敏。 |
| 配置中心与运维监控 | 集中管理模型、风控、召回、销售/Worker绑定、图片保留周期、车源同步等配置，展示Worker在线与同步状态。 |
| 数据安全与权限边界 | 第一期轻量权限；模型Key、大风车密钥、飞书配置不下发Worker；AI只读白名单字段。 |
| Worker兼容性管理 | 记录Windows、微信、Worker版本；每次微信升级前跑核心回归；支持暂停Worker和人工降级。 |
| 异常恢复任务 | 定时扫描 `stale running`、`unknown_send_result`、`vehicle sync failed`，生成待办；飞书失败仅记录错误日志。 |

## 15. 总待确认清单

- Gate 0 阻塞项：大风车 API 接口、字段、operationPhase 枚举、可售状态、对外价格字段、鉴权/IP 白名单，待确认。
- Gate 0 阻塞项：销售手机端人工回复同步到桌面端后的可读结构，需真实微信环境实测。
- Gate 0 阻塞项：OmniAuto 现有 RAG 能力需先做代码评估。
- 开发可并行项：线索接入方式 Excel、CSV、手动录入或 API，待确认，先按适配器实现。
- 开发可并行项：线索分配策略手动、轮询或其他规则，待确认，先保留配置位。
- 配置待确认项：好友申请语最终文案。
- 配置待确认项：初始备注命名规则。
- 配置待确认项：每日加好友、AI 回复、召回上限默认值。
- 配置待确认项：随机发送延迟范围和单会话限频默认规则。
- 配置待确认项：召回周期默认值、召回固定文案。
- 联调待确认项：飞书机器人定向个人通知的具体实现方式和错误返回格式。
