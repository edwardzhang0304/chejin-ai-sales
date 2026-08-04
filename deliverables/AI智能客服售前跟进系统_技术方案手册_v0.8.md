# AI智能客服售前跟进系统 技术方案

版本：v0.8

日期：2026-07-21

最后更新：2026-08-04

当前阶段：运营后台 + Windows Worker 客户端。C1 已形成稳定基线；C2 的文字、语音、图片、V3 授权、private 单聊准入、群聊阻断、统一顺序、跨轮去重、多目标串行和停止监听已在 `v16.130.0 / 8ee53e1` 完成正式 Windows 实机验收。C3 自动回复与 C4 自动召回均已实现并测试，当前无已知主链问题。OmniAuto 双仓统一已经完成：上游固定提交为 `69dc871`，车金当前代码为 `v16.132.0 / 37139bfd`，受影响范围 Windows 回归由用户确认通过。`2318bd8` 仅作为历史选择性来源保留。

一句话结论：当前技术方案统一收口到本文档；C0—C4 已有主链基线，不重开图片、语音、回复或召回流程设计。下一阶段唯一新主线是：复用 OmniAuto 的本地 Product Master、KnowledgeRuntime、RAG 与 Guard，将运营录入的真实车辆和审核通过的正式知识持久化到车金现有 PostgreSQL；不接大风车 API，不部署第二套后台，不将测试车辆或未审核知识带入生产。

## 文档治理规则

1. 本手册是业务流程、架构边界和技术决策的唯一事实源。后续架构变更只能先修改
   本手册，不得新建平行“最终方案”“封版方案”或用聊天记录代替正式口径。
2. `C2-C3_OmniAuto_Worker_后端接口合同` 是本手册的派生接口合同，只定义字段、
   枚举、所有者和三层映射；与本手册冲突时以本手册为准。
3. PUML 是本手册的派生图示，只展示流程，不新增业务规则；图文冲突时先修本手册，
   再同步图。
4. 测试报告、审计报告、版本交付说明和历史整改清单只记录当时证据，不作为现行
   开发依据。
5. 每次正式变更顺序固定为：更新本手册 -> 更新接口合同/机器合同 -> 同步PUML ->
   修改代码 -> 自动化 -> 形成干净且可追溯的 Git 提交 -> 构建不可变候选包 ->
   Windows UAT -> 通过后合并。不得由代码审计意见直接创造新业务流程，也不得用
   dirty 工作区构建的包形成正式 UAT 结论。

> **2026-07-31 事务恢复唯一口径**
>
> UI 准入、事实结算和业务推进是三道独立门禁。授权撤销只停止新的微信动作，不能删除已经产生的事实；语音和图片共用 `media_fact` 恢复协议；历史事实可通过既有 `messages/ingest` 以 `fact_settlement` 范围补录，但固定不推进状态机、不启动 Brain。完整定义见
> `C1-C3_事务恢复与事实结算统一架构_v0.1_2026-07-31.md`。

> **2026-07-31 图片流程唯一口径**
>
> 图片角色歧义、Windows 原始位图、Vision 超时和结果 schema、系统剪贴板、
> 服务端权威产品库、跨轮图片上下文、`customer/self` 失败门禁及 UAT 门禁统一
> 收录在本文第 8 章。原图片封版清单已归档为历史审计材料，不再属于现行交付
> 文档；图片流程一律以本文第 8 章为准。
>
> **2026-08-04 当前实现状态：**图片模块已通过 OmniAuto 上游固定提交
> `69dc871` 完成双仓统一，不重写剪贴板事务。
> `claim_copy_ownership/微信窗口PID` 错误硬门禁已经删除，错误映射、来源记录、
> 自动化和 C2 Windows UAT 已在 `v16.130.0 / 8ee53e1` 收口；双仓统一后的
> `v16.132.0 / 37139bfd` 受影响范围 Windows 回归已通过。

截至当前已验收基线 `8ee53e1`，下列统一整改项已经进入实现，本轮不得重新设计或
再次拆分；双仓统一时必须把它们作为回归保留项：

1. 图片专用恢复协调器改为语音/图片共用 `media_fact` 协调器。
2. 语音 ActionJournal 补齐可脱离 UI 结算的 `replayable_observation`。
3. 后端实现 `resume_current_target / settle_without_ui / retry_later`，废弃
   `target_terminated -> 本地not_required`。
4. `messages/ingest` 增加 `active_read / fact_settlement` 授权范围，恢复事实固定
   不推进状态机、不启动 Brain。
5. `unbound / binding_failed / needs_review / degraded / paused` 不得当作永久终止；
   单次短码 OCR 缺失不能覆盖已有 bound 绑定。
6. 全局恢复门禁必须先于 Vision 等能力预检；暂停时纯网络恢复仍继续。
7. 图片输入分离原始位图和 Provider 大小限制，清除系统剪贴板，并覆盖完整合法
   Vision 重试时长。
8. 初次图片角色歧义改为帧级身份门禁；图片检测不得静默截断。
9. 后端图片失败门禁覆盖 `customer/self`，并补齐跨轮图片上下文和服务端权威产品
   ID 校验。
10. 两个图片结果对象改由完整机器 schema 生成三层校验器，禁止继续手抄限制。

> **2026-07-23 召回任务唯一口径**
>
> 召回不再拥有独立的 `follow_up` 任务类型。召回先执行 `recall_precheck`，确认无客户新消息后创建 `trigger_type=recall` 的消息批次；Brain/Guard 通过后统一生成 `task_type=chat_reply`，并由当前 C2 单会话流程发送。运行时代码、接口、状态机和本文后续出现的“召回回复任务”均以这一口径为准。
>
> **2026-08-04 验收状态：**C4 上述状态机、召回前读取、Brain/Guard、`chat_reply`、次数/静默时段和防重复已实现并测试。后续只在 OmniAuto、Brain、知识库或车辆库变更影响该链路时做回归，不得将 C4 重新列为待开发模块。

---

# 第一部分：变更列表

| 日期 | 版本 | 原来是什么 | 变更成什么 |
|---|---|---|---|
| 2026-08-04 | v0.8内部修订 | 车源仍依赖大风车 API，OmniAuto 本地车辆/知识能力和 C4 完成状态未收口 | 取消大风车 API；复用 OmniAuto Product Master、KnowledgeRuntime、RAG/Guard；使用车金现有 PostgreSQL 和持久化图片存储；明确 C4 已完成并测试，后续只做受影响回归 |
| 2026-08-03 | v0.8内部修订 | 文档仍停留在 PID 整改和 Windows UAT 之前，且未定义 OmniAuto 双仓收口顺序 | 固定 `v16.130.0 / 8ee53e1` 为已验收回滚基线，补充 OmniAuto 上游 PR → 车金固定统一提交 → 必要回归 → 车金 PR 的唯一顺序 |
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
| 2026-07-01 | v0.7 | C2 已能处理文字读取和短码定向读取，但真实客户可能发送微信语音；若不识别语音，状态机会把“客户已回复”误判为“客户沉默” | 增补 C2 语音消息识别方案：Worker 先通过 OmniAuto `messages` 探测当前会话消息类型；只有发现未转写语音时才调用 `voice-transcribe` 点击微信自带语音转文字，再二次读取 `messages`；语音消息统一入 `message_events`，失败不得触发 AI 自动回复 |
| 2026-07-20 | v0.8 | v0.7 仍停留在早期语音方案，未覆盖 V3 授权、稳定消息身份、角色门禁、private 单聊准入、群聊终止态、侧栏 OCR 同行聚合和长语音流程 | 以 V16.98 为 C2 当前冻结基线：首屏扫描与授权读取分离；只准入有效短码 private 单聊；群聊/unknown 不搜索、不读取、不转写、不入库；消息上报强制 V3 和 `authorization_revision`；语音转写使用同一 flow、同一 UI 锁和进展型 watchdog；图片识别留待下一版确认 |
| 2026-07-21 | v0.8内部修订 | 自动召回仍写成 Worker 发送固定文案；销售人工回复与“等待销售回复”容易被混成同一状态 | 召回统一复用服务端 Brain，使用 `trigger_type=recall + recall_cycle_id`；Brain/Guard 产出批准文案后 Worker 才发送。销售已经发出人工回复时进入 `sales_replied_waiting_user`，停止当前AI回复但保留后续召回资格。 |
| 2026-07-21 | v0.8内部修订 | OmniAuto RPA、Vision、Brain 与车金 Worker/后端存在同义接口和字段所有权不清风险 | 新增 `C2-C3_OmniAuto_Worker_后端接口合同_v0.1_2026-07-21.md`：固定 OmniAuto action/Brain/Vision 名称、三层字段所有权、C2-C3 单会话串行扩展和唯一映射；本手册只保留摘要，详细联调以接口合同为准。 |
| 2026-07-24 | v0.8内部修订 | 手册仍混有“等待 Brain 释放 UI 锁、chat_reply 到达后另行抢锁、180 秒固定持锁上限”的旧并发方案，销售回复解除人工接管也缺少可靠顺序口径 | 统一为 C2-C3 单会话串行事务：打开会话后保持当前会话和 UI 锁，完成文字/语音/图片、入库、Brain、回复前复查和发送终态后才释放；正常链路的 chat_reply 由当前 C2 Flow 领取，恢复线程只处理崩溃恢复；长动作使用租约续期和进展型 watchdog；销售回复以稳定消息身份和最终画面相对顺序为主证据，occurred_at 仅作辅助。 |
| 2026-07-25 | v0.8内部修订 | add_friend 把点击后的诊断截图/OCR误写成成功前置条件，任务租约也未区分明确失效与暂时网络异常；C2 Outbox、发送回执的全局阻断口径不够集中 | add_friend 以最终“确定”按钮物理点击成功作为 `invite_sent` 完成点，不等待微信成功状态；点击后诊断只能发现明确失败，诊断异常不得降级成功点击。租约瞬时续租异常在本地到期前重试，明确失效或真实到期才停止。任何未获后端确认的消息 Outbox 或 `sent_ack` 均阻断全部新会话扫描和 UI 动作。 |
| 2026-07-25 | v0.8内部修订 | Outbox `quarantine` 和发送回执 `abandoned` 被错误设计为需要人工修消息或人工确认，无法形成无人值守闭环 | 删除技术消息的人工处置终态：消息级识别失败以 `item_state=failed` 正常入库并阻断 Brain；临时故障按退避周期自动重试；请求级合同不兼容进入 `capability_paused` 并自动探测恢复；发送无法确认由后端持久化 `unknown_send_result`，禁止补发但自动结束原回复动作。 |
| 2026-07-30 | v0.8内部修订 | 图片 `deferred` 临时门禁没有定义跨轮所有者、被顶出屏后的结束方式和已入库文字重新触发 Brain 的协议，导致流程与 PUML“新槽位必须终态”冲突 | 删除会话内图片 `pending/deferred`：Vision 配置改为 C2 启动前全局预检；只处理 final_read 当前屏图片；出屏图片不建立槽位。该版曾允许 NEW_IMAGE 结束为 ignored，已被 2026-07-31 图片封版口径收紧为：初始身份不可信走帧级门禁，已建立稳定身份的图片只允许 completed/failed。 |
| 2026-07-31 | v0.8内部修订 | 图片专用恢复把 `unbound/binding_failed` 当永久终止，语音未进入相同门禁，授权失效还可能在后端未保存事实时清理本地记录 | 拆分 UI 准入、事实结算、业务推进；语音/图片统一为 `media_fact` 恢复；恢复先于 Vision 等能力预检；新增 `resume_current_target / settle_without_ui / retry_later` 三态及 `fact_settlement` 入库范围；单次短码 OCR 缺失不得确认永久移除。 |
| 2026-07-31 | v0.8内部修订 | 图片主链已接通，但真实 Windows 图片大小、初始角色歧义、两次 Vision 请求预算、剪贴板清理、历史图片上下文和客户端本地产品库边界未冻结 | 新增图片流程封版口径；初始角色不可信改为帧级身份门禁；原始位图与 Provider 大小上限分离；图片结果使用共享 schema；车金正式产品只由服务端确认；failed 图片门禁覆盖 customer/self；补齐跨轮图片上下文和 UAT 门禁。 |
| 2026-07-31 | v0.8内部修订 | 审计过程中把后加的剪贴板拥有者/PID证明误当成OmniAuto正式能力，继续追加规则会形成第二套图片事务；同时把选择性引入误写成整个目录来自2318bd8会造成来源失真 | 以855c218共同基础和2318bd8选择性图片能力组成现有图片执行器；只撤销86b87f2新增的claim_copy_ownership硬门禁，保留现有slot复核、sequence、位图、指纹辅助、Vision、结果终态和清理链；来源记录同时保留基础提交、选择性来源提交和完整tree SHA；自动化通过后从干净提交构建不可变候选包并直接进入Windows UAT。 |

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
- 客户或我方新图片消息（客户端内存中调用 OmniAuto Vision，不上传原图、不保存图片文件；已形成 Windows 实机基线，后续仅做受影响回归）。
- 销售手机端同步到桌面端的人工回复。
- AI/召回发送成功或失败。
- 图片处理成功或失败；只上报允许持久化的文字结果、错误码和事务审计，不上报原图。
- 微信登录、窗口、风控提示、发送异常。

服务端负责判断：

- 客户消息是否由 AI 回复。
- 是否需要转人工。
- 是否进入等待用户回复。
- 是否达到召回时间。
- 是否生成 chat_reply 任务。
- 是否等待销售回复超时并发送飞书通知。
- 是否停止自动动作。

## 技术栈与工程实现约束

本期按正式工程产品实施，不按演示 Demo 实施。技术栈选择遵循：优先复用 OmniAuto 现有 Python 能力、减少跨语言链路、保证任务可恢复、错误可定位、部署可复制、二期可演进。

| 层级 | 选型 | 说明 |
|---|---|---|
| 服务端语言/框架 | Python 3.11+ / FastAPI | 与 OmniAuto、AI 编排、RPA 生态一致；接口清晰，便于快速工程化。 |
| ORM/迁移 | SQLAlchemy 2.x / Alembic | 数据模型、状态机、任务表必须可迁移、可回滚、可审计。 |
| 主数据库 | PostgreSQL 15+ | 继续使用车金现有 PostgreSQL；`public` 保留线索/任务/会话，OmniAuto 车辆、正式知识、RAG 索引和审计使用独立 `wechat_ai_customer_service` schema。不新增数据库服务器。 |
| 任务调度 | PostgreSQL 任务表 + lease 锁 + APScheduler/服务端扫描进程 | 第一阶段不引入复杂 MQ；任务状态持久化，服务重启后可恢复，避免重复发送。 |
| 控制面前端 | React + TypeScript + Vite + Ant Design | 适合后台管理、表格、表单、配置、日志和任务看板。 |
| Worker 桌面端 | Windows Worker 单应用 / Python 3.11+ / PySide6 / OmniAuto RPA Sidecar / wxauto4备用 / PyInstaller | 对商家交付一个 Worker 应用；工程内部拆为 Worker 主进程 + OmniAuto RPA Sidecar 子进程。Worker 负责任务领取、调度、展示和上报；Sidecar 负责微信窗口探测、OCR、输入、点击、截图取证和微信异常识别。wxauto4 只作为技术备用，不作为默认路径。 |
| Worker 本地存储 | SQLite + 本地文件目录 | 保存本地配置、运行日志、ActionJournal、Ledger、Outbox 和未确认发送记录；不保存图片原始字节或图片临时文件，服务端仍是最终事实源。 |
| 通信方式 | HTTPS REST + Worker 主动轮询/心跳 | 商家电脑不暴露公网端口；Worker 主动拉任务、上报事实和心跳。 |
| AI 文本 | OmniAuto AI Engine + 服务端可配置模型 Provider | OmniAuto 负责上下文、RAG、Guard、编排；模型密钥和主备路由服务端配置，不下发 Worker。 |
| 图片理解 | OmniAuto Vision + 服务端批准的视觉 Provider | 识别图片内容、截图信息、车型线索和置信度；原图不上传车金后端，低置信转人工。 |
| 知识检索 | OmniAuto RAG + 关键词/规则混合检索 | 先基于现有能力工程化；Dify/FastGPT 仅预留 Adapter，不作为第一期核心运行时。 |
| 车辆主数据 | OmniAuto Product Master | 本地手工 V2 车辆是唯一车辆事实源；运营通过车辆页面或 Excel 录入真实数据。不同步、不读写大风车 API。 |
| 文件存储 | 服务端持久化文件卷（首期） | 车辆库图片允许运营上传并持久化；微信客户图片仍只在 Worker 内存处理，不上传、不落盘。两类图片不得混用。 |
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
| Task | `result_code` | 任务成功后的业务结果，例如 `invite_sent / chat_reply_sent`。召回通过批次的 `trigger_type=recall` 区分。 |
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
| `friend_added` | C2首屏发现后，经后端许可点击并确认有效短码、private和输入区可用。 | Worker读取首次会话事实 | 有客户消息则进入AI接待；有销售人工消息则进入 `sales_replied_waiting_user`；双方均无消息才创建主动开场候选。 |
| `ai_active` | AI正常接待。 | 客户/AI | 客户来消息后AI可持续回复，不设轮次上限。 |
| `waiting_user_reply` | 我方已经回复，等待客户回。 | 客户 | 服务端到期生成召回任务；Worker监听客户是否回复。 |
| `recall_precheck` | 召回前读取确认中。 | Worker读取微信事实 / 服务端复核 | 不发送召回；确认无新客户消息后才允许创建 `chat_reply`。 |
| `recalled_waiting_user` | AI已发过召回，继续等待客户回。 | 客户 | 到下一轮召回周期后可再次召回。 |
| `waiting_sales_reply` | AI已转人工，等待销售回复客户。 | 销售 | 超过N天销售未回，服务端飞书通知销售。 |
| `sales_replied_waiting_user` | 销售已回复，等待客户回。 | 客户 / 召回到期 | 客户回复后重新进入 `ai_active`；长期未回复可进入 `recall_precheck`，由 Brain 生成召回。 |
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

### 4.1 首次好友激活与主动开场

```text
C1只确认好友申请已发送
-> C2首屏扫描发现短码并上报
-> 后端签发visible-only点击许可
-> Worker打开并确认有效短码 + private + 输入区可用
-> 后端确认friend_added
```

首次读取分支：

- 存在客户消息：取消主动开场，按普通 C2/C3 客户消息流程处理。
- 存在未关联既有 AI `reply_action` 的 `self` 消息：视为销售人工回复，消息入库，取消主动开场，状态进入 `sales_replied_waiting_user`；当前不调用 Brain，但后续长期未回复仍可召回。
- 没有客户消息和销售消息：创建一次性 `trigger_type=friend_welcome` 批次；同一会话只允许成功创建/发送一次。
- `self` 消息若能与既有 AI `reply_action/sent_ack` 对应，不得误判为销售人工回复。

### 4.2 AI持续接待

```text
客户发消息 -> Worker上报message_event -> 服务端判断可AI回复 -> AI生成回复 -> Worker发送 -> sent_ack -> 状态=waiting_user_reply
```

规则：

- AI不再按句数停止。
- AI是否继续聊由风控、客户意图、模型置信度、会话状态决定。
- 命中高风险、高意向、模型失败、图片低置信或证据不足时，不继续自动回复，转人工。

### 4.3 等待用户回复

任何我方回复成功后，都进入等待用户回复：

```text
AI回复成功 -> waiting_user_reply
AI召回成功 -> recalled_waiting_user
销售人工回复成功 -> sales_replied_waiting_user
```

如果客户回复：

```text
未转人工会话 -> ai_active
waiting_sales_reply 且销售尚未回复 -> 保持人工负责，由销售继续处理
sales_replied_waiting_user -> ai_active
AI召回已经成功的会话 -> ai_active；高意向/高风险时仍转 waiting_sales_reply
```

`recall_origin_status` 只用于证明本次召回从哪个等待状态进入和避免重复召回，不改变客户新消息的归属：只要原状态为 `sales_replied_waiting_user`，无论召回发送前后，客户新消息都重新进入 `ai_active`。召回文案发送成功后状态统一进入 `recalled_waiting_user`。

如果客户明确拒绝：

```text
任意状态 -> rejected
```

### 4.4 自动召回

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
服务端生成 recall_precheck read-target，并保存 recall_origin_status
Worker定向读取该会话最新消息
若读到客户新消息，则取消本轮召回并进入AI回复/转人工判断；原本为 sales_replied_waiting_user 时同样重新进入 ai_active，不继续锁给销售
若确认无客户新消息，服务端创建 trigger_type=recall 的召回批次
同一个 Brain 结合历史会话生成召回候选，Guard 审核通过后创建chat_reply任务
Worker领取任务并发送服务端批准的召回内容
Worker上报sent_ack
服务端更新last_recall_at、recall_count、last_outbound_at
状态=recalled_waiting_user
```

召回次数口径：

- 业务上支持持续召回，直到用户回复、拒绝或关闭。
- 工程上必须保留配置项：召回间隔、每日召回上限、单客户最大召回次数、静默时段。
- 如果项目方配置为“不限次数”，系统也必须受每日上限和静默时段约束。

### 4.5 转人工与销售超时提醒

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

### 4.6 备注短码作为系统托管开关

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
| 召回到期扫描 | 服务端 | 数据库中的等待用户回复类状态。 | 先生成 `recall_precheck` 读取目标；确认无新客户消息后才创建 `chat_reply` 任务。 |
| 销售超时扫描 | 服务端 | `waiting_sales_reply` 且销售未回复的会话。 | 发送飞书通知销售。 |
| Worker健康扫描 | 服务端 | Worker heartbeat、last_sync_at、当前任务。 | 标记离线、卡住、异常。 |
| 发送结果恢复 | Worker + 服务端 | `sending`、`unknown_send_result`、超时任务。 | 自动查询/重传原回执；结果无法确认时由后端持久化 `unknown_send_result`，禁止重复发送。 |

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
- 右键当前图片、点击复制并读取本次剪贴板代次。
- 输入AI回复、发送AI回复、发送召回文案。
- 通过备注短码绑定会话、确认短码移除、确认发送结果。

可以异步执行的动作仅限非微信UI逻辑：

- 等待AI生成。
- RAG 检索、服务端 Product Master 查询和正式知识检索。
- 进程内图片编码和视觉识别；不得因此释放当前会话 UI 锁去处理其他会话。
- 服务端状态机判断、定时扫描数据库。
- 飞书通知。
- 日志写入、任务排队、错误记录。

这里的“异步”只表示计算或网络调用可以在服务端/子进程中执行，不表示当前会话释放 UI 锁，也不表示 Worker 可以转去操作另一个微信会话。单会话 Flow 调用 Vision 或等待 Brain 时，仍保留当前会话事务和本地 UI 锁；只有与微信 UI 完全无关的后台服务工作可以并行。

执行规则：

```text
任何任务只要需要操作微信桌面端，
必须先获取 Local WeChat UI Lock，
执行完成或失败后释放锁，
下一个微信UI任务才能继续。
```

任务优先级可以配置，但不改变串行原则：

```text
chat_reply > add_friend
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
| `chat_reply` | 服务端 | Worker | 发送 Brain/Guard 已批准的客户回复、好友开场或召回内容；具体来源由关联批次的 `trigger_type` 区分。 |
| `handoff_notify` | 服务端 | 服务端 | 飞书通知销售；不需要Worker操作微信。 |
| `generate_remark_code` | 服务端/控制面 | 服务端 | 为线下好友生成系统短码和推荐备注名。 |
| `recover_remark_code` | 服务端/控制面 | 服务端 | 查询并复制原短码，用于误删备注或恢复系统跟进。 |
| `bind_by_remark_code` | Worker上报 | 服务端 | Worker识别备注短码后绑定线索和会话。 |

### 8.1 统一任务中心状态口径

`Task.status` 只表示任务执行生命周期，不承载具体业务结果。`invite_sent`、`chat_reply_sent` 这类含义必须写入 `Task.result_code`，再由服务端结合批次 `trigger_type` 映射为 `Conversation.status`。

固定状态集合：

```text
Task.status = blocked / pending / running / completed / failed / cancelled
```

固定字段职责：

| 字段 | 职责 | 示例 |
|---|---|---|
| `task.status` | 任务执行生命周期 | blocked、pending、running、completed、failed、cancelled |
| `task.result_code` | 任务完成后的业务结果 | invite_sent、already_friend、chat_reply_sent、skipped_by_rule |
| `task.error_code` | 任务失败原因 | WECHAT_WINDOW_NOT_FOUND、PHONE_NOT_FOUND、WORKER_INTERRUPTED |
| `task.block_code` | 任务阻塞原因 | SALES_WORKER_NOT_BOUND、DAILY_LIMIT_REACHED |
| `conversation.status` | 客户/会话业务生命周期 | add_friend_sent、waiting_user_reply、recalled_waiting_user |

核心映射规则：

| 任务结果 | 会话状态更新 |
|---|---|
| `task_type=add_friend` 且 `task.status=completed` 且 `result_code=invite_sent` | `conversation.status=add_friend_sent` |
| `task_type=add_friend` 且 `task.status=completed` 且 `result_code=already_friend` | `conversation.status=friend_added`，并立即尝试绑定会话 |
| `task_type=chat_reply`、`trigger_type=customer_message/friend_welcome` 且发送完成 | `conversation.status=waiting_user_reply` |
| `task_type=chat_reply`、`trigger_type=recall` 且发送完成 | `conversation.status=recalled_waiting_user` |
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
| `trigger_type=recall` 的 `chat_reply` | 按 `conversation_id + recall_round + rule_id` 去重，且只能在 `recall_precheck` 确认无新客户消息后创建。 |
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
| `module` | 出错模块，如 Worker、WeChat、AI、Vision、ProductMaster、KnowledgeRuntime、飞书、RAG、Guard。 |
| `suggested_action` | 建议处理方式，如重试、人工确认、检查登录、检查API Key、联系接口方。 |
| `trace_id` | 关联任务、会话、消息、reply_action、chat_reply、handoff_event的追踪ID。 |

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
| S-04 | 等待用户超时召回 | 超过N天客户未回复，服务端先生成 `recall_precheck`；Worker读取确认无新客户消息后，才允许创建并发送 `chat_reply`。 |
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
| Task | 统一任务表，task_type 只包含 add_friend、chat_reply；召回通过关联批次 `trigger_type=recall` 区分。任务主状态只表达执行生命周期，统一为 blocked、pending、running、completed、failed、cancelled；业务执行结果写入 result_code，例如 invite_sent、already_friend、chat_reply_sent；running 内部用 current_step 展示执行步骤；blocked 必须有 block_code 和 block_reason。 |
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
等待用户回复类会话到达召回周期 -> 服务端生成recall_precheck读取目标 -> Worker定向读取确认无新客户消息 -> 创建trigger_type=recall批次 -> Brain/Guard生成并批准召回内容 -> 创建chat_reply任务 -> Worker发送 -> 记录结果
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

- Worker 部署在商家侧 Windows 电脑，负责看微信、点微信、读消息、在内存中读取图片、发回复；不保存客户原图。
- Worker 第一阶段只做 Windows 正式工程版本；Mac Worker 不进入本期正式交付范围。
- 对商家交付形态为一个安装包、一个桌面入口、一个 Worker 客户端；OmniAuto 不作为第二个应用暴露给商家。
- 工程内部采用 `Worker 主进程 + OmniAuto RPA Sidecar 子进程`。Worker 主进程负责 UI、任务领取、任务调度、状态展示、日志上传和错误处理；OmniAuto RPA Sidecar 负责实际操作本机微信桌面客户端。
- Worker 执行台必须呈现为本地可视化窗口，交互效果参照附件视频：微信桌面客户端旁边展示任务步骤、截图证据、AI 结果、运行状态和控制按钮。
- Worker 不保存业务主状态，不直接调用文本大模型，不持有服务端 Brain、飞书或数据库密钥。Vision Provider 凭据只能按当前已验收的 Worker 安全配置和脱敏日志规则使用。
- Worker 不需要开机自启，通过执行台启动按钮操作。
- Worker RPA 能力优先复用 OmniAuto 仓库的微信 Win32/OCR sidecar、RPA 全局锁、输入/点击节流、截图证据和验收门禁；本项目新增 Worker 任务桥接层、RPA Sidecar 调用协议和 `add_friend` 执行器。`add_friend` 字段契约、结果码和验收口径统一写入本文档模块4，不再另设独立集成方案作为当前有效入口。
- OmniAuto 原 checkpoint 已进入维护基线：C1 `add_friend`、C2 会话绑定/文字/语音/图片事实链、C3 AI 回复发送和 C4 召回均已实现；后续只做双仓源码统一和受影响回归。车辆库/知识库接入是服务端新主线，不改 Worker 微信事实采集和发送合同。
- 会话绑定/微信监听不是 `add_friend` 的一部分，也不等同于 `chat_reply` 任务；它是 Worker 运行时扫描和消息事实入库能力。`chat_reply` 只表示服务端已生成并批准回复后，由 Worker 执行发送的任务。
- 人工传值只允许作为调试/兜底能力，不作为正式业务流程；正式主链路必须通过 Worker 调用 OmniAuto RPA Sidecar 自动执行并回传结构化结果。

Worker 与 OmniAuto 的内部调用边界：

```text
服务端AI链路：
车金服务端
  -> OmniAuto AI Engine Adapter
      -> customer_service_brain / RAG / evidence / guard / reply synthesis
      -> 服务端配置的文本模型 Provider

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
| OmniAuto RPA / Vision | 微信窗口识别、OCR、点击、输入、发送校验、备注、截图取证；进程内完成图片剪贴板事务和图片文字化理解 | 不保存业务主状态，不做线索分配，不判断是否转人工，不决定图片发送方角色 |
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
| session_scan/message_ingest（运行时能力，非Task） | 扫描微信会话、识别客户短码和 private、绑定会话、读取已绑定会话的文字/语音/图片、按稳定身份上报服务端，并在有 batch 时维持当前单会话 Flow | 不进入任务中心，不自行生成AI回复或判断业务意向 |
| chat_reply | 领取服务端已批准的 `reply_action`，按 `trigger_type` 发送客户回复、好友开场或召回文案，回传 `sent_ack` 和证据 | 不判断回复/召回资格，不在 Worker 本地生成或改写文案，不绕过 pre_send_refresh |
| Local WeChat UI Lock | 所有微信桌面端 UI 操作串行化 | 不决定业务状态 |

### 3.2 UI锁、优先级与恢复

#### 3.2.1 定位

`Local WeChat UI Lock` 是 Worker 本地基础设施，不属于 C2 业务模块。C1 `add_friend`、C2 `session_scan/message_ingest`、C3/C4 统一 `chat_reply` 只要需要点击、输入、切换、读取微信 UI，都必须先使用这把锁。

锁分两层：

| 层级 | 名称 | 放在哪里 | 解决什么问题 | 适用范围 |
|---|---|---|---|---|
| 本地互斥锁 | `Local WeChat UI Lock` | Worker 本地进程内 + 本地运行态文件 | 保证同一台电脑上的微信桌面端同一时间只被一个动作操作 | 所有需要操作微信 UI 的动作，包括 C2 运行时扫描 |
| 服务端任务租约 | `task.lease_expires_at` | 服务端任务中心 | 保证一个服务端任务同一时间只被一个 Worker 持有 | `add_friend / chat_reply` 任务 |

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
| `operation_type` | `add_friend / session_scan / message_ingest / chat_reply / diagnostic`。图片理解是 `message_ingest` 单会话 Flow 的内部阶段，不是独立锁操作类型。 |
| `task_id` | 服务端任务 ID；C2 运行时能力为空。 |
| `conversation_id` | 相关会话 ID，可为空。 |
| `lead_id` | 相关线索 ID，可为空。 |
| `rpa_session_key` | 微信会话定位键；C2 读取消息时必须记录。 |
| `current_step` | 当前步骤，例如 `opening_wechat_window / typing / clicking_send / reading_messages`。 |
| `acquired_at` | 获取锁时间。 |
| `lease_expires_at` | 本地锁租约过期时间。 |
| `renew_interval_seconds` | 续租间隔。 |
| `process_id` | Worker 进程 ID。 |
| `last_renewed_at` | 最近续租时间。 |

默认配置：

| 配置 | 默认值 | 说明 |
|---|---:|---|
| `ui_lock_ttl_seconds` | 90 | 本地锁默认租约。 |
| `ui_lock_renew_interval_seconds` | 15 | 持锁期间续租频率。 |
| `ui_step_timeout_seconds` | 120 | 单个 UI 步骤默认超时。 |
| `ui_lock_acquire_timeout_seconds` | 20 | 等待获取锁的默认超时。 |

以上默认值可配置，不作为性能承诺。单会话 C2-C3 Flow 不设置固定总持锁时长；文字读取通常是短动作，多条语音、Vision、Brain 和发送确认属于允许持续推进的长动作。系统依靠租约续期、阶段进展、授权复核、停止信号和硬安全 watchdog 判断异常，不能用固定 180 秒打断正常业务流程。

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
- 每次续租刷新 `lease_expires_at`；只要当前单会话 Flow 持续取得可证明进展、授权仍有效且没有停止信号，可以继续续租。
- 无进展 watchdog 只判断同一阶段是否卡死；正常语音转写、Vision 或 Brain 状态发生变化时必须续命。
- 硬安全 watchdog 只用于防止进程永久失控，触发后保留已完成事实和发送不确定性证据，不得把已完成结果回滚成未知。
- 所有可能长时间占用微信 UI 的 OmniAuto 调用都必须接收取消回调，并在每个安全步骤边界检查本地 UI 锁、服务端任务租约和停止信号；至少包括 C1 `add_friend`、语音转写、图片剪贴板事务和消息发送。
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
| Worker 崩溃重启，本地锁过期 | 清理本地锁；服务端任务按 `task.lease_expires_at` 自动恢复、重试或持久化明确终态。 |
| Worker 仍在运行但 UI 步骤超时 | 截图、记录窗口标题、释放锁；当前动作失败并上报错误码。 |
| 服务端任务租约过期但本地锁仍持有 | Worker 停止后续 UI 操作，释放本地锁，上报 `TASK_LEASE_EXPIRED`。 |
| 本地锁过期但动作仍尝试操作微信 | 禁止继续点击/输入/发送，返回 `UI_LOCK_LEASE_EXPIRED`。 |
| 发送动作结果未知 | 不自动补发；Worker 上报 `unknown`，后端持久化 `unknown_send_result` 后自动结束原回复动作。 |

#### 3.2.5 服务端任务租约

`add_friend / chat_reply` 是服务端任务，但两者进入本地 UI 锁的方式不同：

- `add_friend`：先领取任务租约，再获取本地 UI 锁，执行完成后释放。
- 正常 `chat_reply`：由已经持有当前会话 UI 锁的 C2 Flow 等待 Brain 终态，在回复前复查通过后领取当前会话的 `chat_reply` 任务和发送许可，不重复释放/获取本地锁。
- 崩溃恢复 `chat_reply`：仅当本机没有仍在执行的原 C2 Flow 时，恢复线程才获取本地 UI 锁，重建原会话上下文并执行完整回复前复查；不得直接跳到输入发送。

任务租约字段：

| 字段 | 说明 |
|---|---|
| `task.lease_owner_worker_id` | 当前持有任务的 Worker。 |
| `task.lease_owner_client_instance_id` | 当前客户端实例。 |
| `task.lease_expires_at` | 服务端任务租约过期时间。 |
| `task.lease_last_renewed_at` | 最近一次服务端任务续租时间。 |
| `task.lease_fencing_token` | 每次重新领取时递增；旧进程持有的旧编号不得继续修改任务。 |
| `task.current_step` | 当前执行步骤。 |

任务租约规则：

- Worker claim 任务后，服务端写入所有者、客户端实例、到期时间并递增
  `lease_fencing_token`；默认租约为 90 秒。
- 服务端领取时必须先对目标任务执行 `SELECT ... FOR UPDATE`，再检查
  `status=pending` 并签发租约。禁止使用“普通查询后再更新”的非原子领取。
- 任务运行中 Worker 默认每 15 秒续租。更新步骤、完成、失败、领取发送
  许可都必须校验 `worker_id + client_instance_id + fencing_token`。
- 服务端明确返回租约过期、`fencing_token` 不匹配、所有者/客户端实例变化或
  授权硬失败时，旧进程必须立即停止后续 UI 操作。
- 单次网络超时、连接中断或服务端 5xx 只表示“本次续租结果未知”，不能直接
  判定租约已经丢失。Worker 应在本地记录的 `lease_expires_at` 到期前继续重试；
  续租成功后以服务端返回的新到期时间更新本地记录。
- 本地时间到达 `lease_expires_at` 仍未续租成功时，才按真实租约到期停止动作，
  释放本地 UI 锁，并将任务收口为 `TASK_LEASE_EXPIRED`。任何情况下都不得越过
  已知租约到期时间继续点击、输入或发送。
- 对发送类动作，租约过期不得自动重放，避免重复发送。
- 对 `add_friend`，本轮同样不自动重放；由后续人工或明确恢复流程根据步骤
  和证据重新创建任务，不能复用过期租约继续点击。
- `add_friend` 必须与发送、语音、图片共用唯一 `ActionJournal` 阶段合同。
  Worker 在执行前持久化 `not_attempted`，OmniAuto 在点击“发送好友申请”
  前原子写入 `trigger_attempted`；点击函数明确返回物理点击成功后，必须立即
  写入 `confirmed`，并形成 `task.status=completed + result_code=invite_sent`。
  `confirmed` 在此表示最终“确定”按钮已被程序成功点击，不表示等待微信出现
  额外成功页面或成功文案。
- add_friend 点击后的截图/OCR只用于诊断明确的风控或失败提示。截图失败、OCR
  失败、界面仍停留在原表单或没有成功提示，都不得把已确认的点击降级为
  `unknown/failed`；若可靠识别出操作频繁、账号受限等明确失败，则保留
  `action_phase=confirmed` 的物理事实，同时把唯一业务终态更新为 `failed`
  并写入对应 `error_code`。
- add_friend 只有 `not_attempted` 可以安全自动重试。停在 `trigger_attempted`
  表示进程无法证明点击是否完成，必须按未知结果收口并禁止再次点击；已到
  `confirmed` 时恢复为既有 `invite_sent` 或明确失败结果，不重复发送申请。
- add_friend 最终结果必须由一个集中映射器生成。`ok/result_code/task_status/
  error_code` 不允许由 OmniAuto、Worker bridge 和任务线程分别重新推导；禁止
  出现 `ok=true` 同时 `task_status=failed` 的矛盾响应。
- 本地任务线程在拉取服务端任务前必须持有调度锁并再次确认 C2 没有取得
  UI 锁。微信正忙时不得先把任务领成 `running` 再因抢锁超时判失败。
- C2 原会话流程领取 `chat_reply` 后，Worker 必须在整个租约有效期通过
  心跳上报 `running_status=running + current_task=task_id`；任务终态在
  统一 `finally` 清理，不得中途显示空闲。

#### 3.2.6 本地多任务优先级

同一台电脑的微信 UI 操作不可并行，也不做中途抢占。高优先级动作只能等待当前锁释放或超时恢复。

本地 UI 队列优先级：

| 优先级 | 动作 | 说明 |
|---:|---|---|
| 100 | `recovery / emergency_stop` | 释放异常锁、停止风险动作。 |
| 95 | `pre_send_refresh` | 发送前短读目标会话，确认 reply_action 未被新客户消息 supersede。 |
| 90 | `session_scan_visible` | 第一屏主动扫描，优先发现当前微信第一屏新消息、短码和未读变化。 |
| 85 | `message_ingest_visible_hit` | 第一屏命中的会话优先读取消息。 |
| 80 | `chat_reply` | 当前 C2 单会话 Flow 内的发送阶段；等待 Brain 时保持原会话和 UI 锁，发送前必须完成 pre_send_refresh。独立队列项只用于崩溃恢复。 |
| 75 | `add_friend` | 加好友任务；优先级高于普通定向读取和召回。 |
| 70 | `message_ingest_state_target` | 状态机驱动定向读取，读取非第一屏的已知重点客户。 |
| 65 | `recall_precheck_read` | 召回前确认读取；确认没有新客户消息后才允许 chat_reply。 |
| 40 | `diagnostic` | 人工排查、诊断截图；不作为主监听兜底。 |

队列规则：

- `chat_reply / add_friend` 的服务端任务优先级仍成立，但 Worker 发送前必须执行 `pre_send_refresh`，召回批次生成发送任务前必须执行 `recall_precheck_read`。
- C2 不是任务；C2 只有在需要操作微信 UI 时进入本地 UI 队列。
- 当前 C2 Flow 已经打开并确认某个会话后，不接受其他会话的 `add_friend / chat_reply / message_ingest` 中途抢占；后续任务只能等待该会话到达业务终态并释放锁。
- 当前会话产生的消息事实尚未被后端确认时，完整 V3 请求必须进入 Outbox；只允许重传该 Outbox，不得扫描、打开或处理下一个会话。后端返回 `ingested/duplicated` 并确认本地 ledger 后，才解除门禁。
- AI 回复已经发生物理发送，但 `sent_ack` 尚未被后端确认时，必须先把回执可靠写入本地 Outbox；在确认前整个 C2 停止扫描，也不得执行其他会话的 `add_friend/chat_reply`。为避免形成永久 stale lock，回执可靠落盘后可以在 `finally` 释放本地 UI 锁，但调度门禁继续保持，恢复线程只能查询或重传原 `sent_ack`，不能补发消息。
- `sent_ack` 的 `sent / failed / unknown` 都是后端可确认的正式终态；其中 `unknown` 表示发送可能发生且禁止补发。后端确认任一终态后自动解除门禁，不建立人工发送确认流程。
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
| `chat_reply` | 是 | 是 | 是 | 正常链路：当前 C2 Flow 持锁等待 Brain -> pre_send_refresh -> claim 当前会话 `chat_reply` 和 `reply_action` -> claim-send -> 输入前/点击前复核消息序列 -> 发送 -> 上报 `sent_ack` -> 当前会话终态后释放锁。恢复链路：获取锁并重建原会话上下文后执行同一套复核，不允许另一套直接发送路径。召回批次还必须先由 `recall_precheck_read` 放行。 |

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
| `TASK_LEASE_RENEW_FAILED` | 服务端明确拒绝续租，或瞬时异常持续到本地记录的租约真实到期。 | 停止后续 UI 操作，释放本地锁；单次网络超时、连接中断或 5xx 只重试并记录，不得立即使用该终态错误码。 |
| `UI_STEP_TIMEOUT` | 单个 UI 步骤超时。 | 截图、记录窗口标题和当前步骤，上报失败。 |
| `UI_OPERATION_CANCELLED` | 动作被暂停、人工停止或风控中断。 | 释放锁，按业务状态处理。 |

#### 3.2.9 验收口径

- 同一台 Worker 上同时存在 `add_friend`、`session_scan`、`chat_reply` 时，微信 UI 操作必须串行，不允许并行点击或输入。
- 正常 C2-C3 单会话 Flow 等待 Brain 期间继续持有当前会话 UI 锁，不处理其他微信会话；只有纯服务端计算并行，Worker 不释放会话所有权。
- 消息 Outbox 未确认或发送 `sent_ack` 未确认时，整个 C2 调度必须保持阻断；自动恢复只能重传既有事实/回执，不能进入新会话，也不能重复执行微信动作。
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
| `trigger_type=recall` 的 chat_reply | `unique(lead_id, rule_id, recall_round)`，且只能在 `recall_precheck` 放行后创建。 |
| handoff_event | `unique(conversation_id, handoff_reason_group, active_period)`；转人工原因字段统一为 `handoff_reason_code`。 |

#### 3.3.1 异常事务统一模型

所有会改变微信界面的不可逆动作，包括发送好友申请、语音转写点击、图片复制和消息发送，都必须同时记录“物理动作阶段”和“业务结果”。两者不得混成一个 `ok/failed`。

物理动作阶段只有一套正式枚举：

| `action_phase` | 含义 | 允许的后续处理 |
|---|---|---|
| `not_attempted` | 尚未触发可能改变微信状态的物理动作。 | 可以安全取消或按规则重试。 |
| `trigger_attempted` | 已经触发点击、右键、复制或发送，结果可能已经发生。 | 不得按“肯定没发生”处理；必须继续取证或进入未知结果。 |
| `confirmed` | 已取得足以证明目标动作/结果的执行器返回、UI、剪贴板或服务端证据。 | 可以提交对应 completed/sent 终态；add_friend 的物理点击确认与点击后业务失败仍须分层记录。 |

统一不变量：

1. `failed` 只表示有证据证明目标动作没有发生，或者动作发生后目标结果明确失败。
2. 物理触发已经发生但无法确认结果时，发送必须返回 `unknown`；语音和图片必须形成带 `*_RESULT_UNKNOWN` 错误码的 failed 事实并阻断 Brain，不得自动重复昂贵动作。
3. `unknown` 是正式终态，不能转换成“没有发送”，不得自动补发，也不建立人工确认发送结果的流程；原自动发送动作终结，会话转为销售正常接管。
4. 不可逆动作前先持久化 `not_attempted` 意图；紧邻物理点击之前必须原子落盘
   `trigger_attempted`，落盘成功后才允许点击；取得可靠证据后再落盘
   `confirmed`。宁可把“落盘后、点击前”崩溃保守地视为可能发生，也不能在
   点击已经发生后仍留下 `not_attempted`。
5. Worker 本地记录是执行证据和恢复缓存；`reply_action`、`message_event`、`message_batch` 和人工接管等业务真相由后端持久化。
6. 动作日志只能在对应 ledger/发送回执可靠落盘后删除。Sidecar 异常、停止、
   授权撤销或统一收尾均不得直接清除尚未恢复的动作日志。
7. 语音和图片统一使用 `media_fact` 恢复协议，不依赖普通 `read-targets`
   数量。Worker 使用现有轻量授权接口查询原会话，后端只允许返回
   `resume_current_target / settle_without_ui / retry_later`。UI 授权失效不等于
   事实可以丢弃；任何未知决定按 `retry_later` 失败关闭。
8. `settle_without_ui` 不再操作微信。后端能证明原 Worker、原绑定、原
   conversation 和短码身份一致时，使用现有 `messages/ingest` 的
   `authorization_scope=fact_settlement` 补录事实，并固定
   `state_transition_applied=false`、`message_batch=null`；身份无法安全确认时，
   后端持久化 `technical_terminal`。两种情况都必须逐条确认
   `source_message_key` 后，Worker 才能清理本地记录。
9. 语音或图片动作日志的全部条目均明确为 `not_attempted` 时，证明昂贵动作未
   发生，Worker 可以在全局门禁前本地清理；只要存在 `trigger_attempted` 或
   `confirmed`，就必须进入统一恢复协议，客户端不得自行猜测终态。
10. 恢复顺序固定为 `sent_ack Outbox -> messages Outbox -> media ActionJournal/
    Ledger -> 任务中心恢复 -> 本轮能力预检 -> 新 UI 动作`。Vision 配置缺失不得
    阻断无需 Vision 的历史回执和事实结算。
11. `unbound / binding_failed / needs_review / degraded / paused` 都不是永久
    终止证明。后端仍能证明原事务身份时直接
    `settle_without_ui + fact_only`；身份暂不可证时才 `retry_later`。会话关闭、
    拒绝、可靠确认短码移除也只能停止新 UI，已产生事实仍须结算。
12. 单次首屏 OCR 未识别到短码不能把已有 bound 会话判定为短码已移除。低置信、
    截断或单次缺失进入 `degraded`，只有可审计的关闭操作或专门标题复核证据才能
    确认移除。
13. add_friend 是本合同的明确特例：最终“确定”按钮点击函数返回成功，就是
   `action_phase=confirmed` 的证据；不再等待第二个 UI 成功状态。点击后截图/OCR
   仅用于发现明确风控/失败，不能作为进入 `confirmed` 的前置条件。

工程实现只允许三个集中判定入口：

```text
classify_action_result(action_phase, evidence)
merge_item_outcomes(previous, current)
classify_outbox_recovery(http_result)
```

函数名称可遵循现有代码风格调整，但职责必须唯一。OmniAuto 只返回动作和观察证据；Worker 只能通过统一判定器形成车金结果；后端只校验和持久化，不得再次通过坐标、正文或错误文本猜测动作阶段。

禁止在主编排函数中继续按单个事故增加分散的错误码 `if/elif`。新增错误必须先归入上述正式状态，再由统一状态转换表决定重试、阻断 Brain、转人工或收尾。

### 3.4 执行台展示与验收

- 展示当前 Worker 状态、任务类型、任务 ID、客户/线索短码、步骤时间线、微信/服务端连接、图片缩略图、AI 候选回复、Guard 结果、风控原因、飞书通知结果、错误日志。
- 提供启动、暂停、继续、停止、手动接管/禁用 AI、重试、跳过按钮。
- 验收要求：看得见、停得住、查得到原因、不会重复发送；`add_friend`、`chat_reply` 两类任务与 C2 运行时能力共用同一把 UI 锁。

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
- 加好友结束点是申请添加朋友页最终“确定”按钮的物理点击函数明确返回成功；微信没有可依赖的后续成功状态，不等待成功页面或成功文案。点击成功且没有可靠识别到明确风控/失败提示时，上报 `task.status=completed` 且 `result_code=invite_sent`。点击后的截图/OCR只是诊断步骤，其自身失败不得改变该成功结果。

| 状态/异常 | 处理 |
|---|---|
| 执行状态 | 主状态：blocked、pending、running、completed、failed、cancelled；running 内部步骤：searching_contact、contact_found、opening_add_contact、filling_remark、sending_invite。最终“确定”按钮点击函数返回成功后立即退出 running；不设置 `waiting_ui_response`。邀请已发送表达为 `task.status=completed` 且 `result_code=invite_sent`。 |
| 可重试失败 | wechat_not_login、wechat_window_not_found、ui_element_not_found、network_error、worker_interrupted、unknown_error。 |
| 完成结果 | `already_friend` 表示已是好友，任务应记为 `task.status=completed` 且 `result_code=already_friend`，随后立即尝试绑定会话。 |
| 不建议自动重试 | phone_invalid、phone_not_found、customer_privacy_blocked、wechat_rate_limit、operation_too_frequent、account_restricted、blacklist_hit、daily_limit_reached。 |
| 微信风险提示 | 操作频繁、环境异常、添加受限等出现后暂停 add_friend 并上报，不自动连续重试。 |
| 幂等 | 同一 task_id 多次上报成功只记录一次；同手机号不生成多个未完成加好友任务。 |

## 6. 模块5：会话绑定与监听

- 目标：知道微信里这条消息是谁发的、属于哪条线索、当前 AI 能不能回。
- 第一期不读取微信数据库、不破解协议、不使用非公开微信接口、不依赖客户昵称唯一性。
- 绑定优先通过初始备注/短码；已是好友立即尝试绑定；绑定失败不自动回复。
- 当前 V16.107 开发基线统一处理客户/我方文字、语音和图片事实；Worker 先用 OmniAuto `messages` 读取/探测消息类型，只有发现未转写语音时才调用 `voice-transcribe`，转写正文必须绑定原语音并按 `message_type=voice` 入库，不能再作为独立 `text` 入库。
- 图片必须先进入最终画面的统一消息槽位并完成新老判定；只对 `NEW_IMAGE` 执行一次内存剪贴板事务和真实 OmniAuto Vision。不得落本地图片文件、上传车金后端、调用旧图片入口或上报 `pending/discovered` 占位。
- 重复消息由 Worker 稳定来源身份和 `dedupe_key` 初筛，服务端以 `unique(conversation_id, dedupe_key)` 做最终防线；页面坐标、扫描轮次和绝对时间不得作为消息主身份。
- C2 唯一准入条件为：当前会话标题含有效短码、标题同步确认 `conversation_type=private`、服务端 `read-targets` 仍提供当前 `authorization_revision`。群聊和 `unknown` 不进入消息读取。
- 本模块是 OmniAuto 接入 C2 checkpoint。Worker 调用 OmniAuto `sessions / messages / voice-transcribe` 能力读取微信事实，服务端负责短码绑定、会话状态、消息去重和是否允许后续 AI 回复。
- 本模块不生成 AI 回复、不发送 AI 回复；AI 回复发送属于 C3 checkpoint，必须在会话绑定和消息入库稳定后实施。
- C2 接口、状态、错误码和验收标准以本模块为准。

### 6.0 OmniAuto结合方式

#### 6.0.0 接口名称与适配边界

C2/C3 详细联调字段以 `C2-C3_OmniAuto_Worker_后端接口合同_v0.1_2026-07-21.md` 为唯一专项依据。本手册定义业务流程，专项合同定义可直接开发和测试的请求、响应、枚举、字段所有者及 OmniAuto 到车金后端的唯一映射。

正式命名固定如下：

```text
OmniAuto RPA action：sessions / open-chat / messages / voice-transcribe / send
OmniAuto Vision：customer_image_understanding / visual_bridge_input
OmniAuto Brain：customer_service_brain / brain_plan / brain_plan.recommended_action / reply_segments
车金后端流程对象：message_batch / batch_id / reply_action / handoff_event
```

三层边界：

- OmniAuto 输出 UI 观察、Vision 文字化结果和 Brain 计划，不依赖车金 `contracts/c2_contract_v3.json`。
- Worker 校验 `observation_schema_version`，生成车金 V3 合同指纹、稳定消息身份、统一顺序和后端请求。
- 后端下发 `conversation_id/authorization_revision`，执行状态机、合同校验、最终去重和持久化，不重做 OCR 左右侧、消息类型或语音父子归属判断。
- OmniAuto Brain 的 `brain_plan.recommended_action` 是业务语义；车金 `batch_status` 是持久化流程状态。二者只能按接口合同映射，不能互相替代。
- V16.104 的 `messages/ingest` 仍只完成 C2 入库；后续单会话串行实现是在原响应中增加可选 `message_batch`，Worker 按 `batch_id` 保持原会话和 UI 锁等待 Brain 终态，不新建另一套“上报并问 Brain”接口。

```text
Worker运行时扫描
  -> OmniAuto sessions：扫描微信第一屏，按视觉行聚合OCR并提取唯一短码候选
  -> 服务端绑定/授权：用remark_code匹配唯一lead/conversation/sales/worker，返回read-target和authorization_revision
  -> Worker定位目标：首屏唯一命中走visible，未命中才按remark_code搜索
  -> 顶部标题复核：有效短码且conversation_type=private才准入
  -> OmniAuto messages：读取当前屏文字/语音观察；必要时在同一flow内完成语音转写并最终复读
  -> Worker构建V3消息：稳定来源身份、角色证据、item_state/flow_state和原始证据
  -> 服务端入库：复核授权版本并按unique(conversation_id,dedupe_key)去重
```

会话绑定/微信监听是 Worker 运行时能力，不直接作为 `chat_reply` 任务。`chat_reply` 任务只在服务端已经生成并批准 `reply_action` 后，用于让 Worker 调用 OmniAuto 执行发送。

C2 第一屏主动扫描、状态机定向读取、消息读取和必要时的语音转文字预处理均属于 Worker 端 OmniAuto RPA Sidecar 能力，具体调用 `sessions / messages / voice-transcribe`。它们只负责识别微信窗口、OCR 当前可见会话列表、定位/切换指定会话、确认短码和单聊类型、读取消息、在发现未转写语音时点击微信语音转文字和截图取证；不调用加好友正式入口 `add-friend-entry-click-plan-windows`，不执行旧滚动兜底方案，不运行 OmniAuto 原本的本地 AI 客服闭环，也不调用尚未确认的图片识别入口。

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
-> 确认无新客户消息后才允许 chat_reply
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
- 普通 OCR 与增强 OCR 必须先聚合同一视觉行，再选择该行标题；唯一合法短码标题优先于数字角标、时间和消息预览。
- 普通 OCR 与增强 OCR 识别到相同短码时合并为一个会话；同一视觉行出现两个不同短码时按 `unknown` 阻断。
- 同码同时存在 private 和明确群聊人数后缀证据时，保留 group 证据，不能用 private 结果覆盖安全阻断。
- 只上报会话事实，不直接判断是否回复，不直接创建 `reply_action`。
- `read-targets=[]` 时仍允许执行第一屏事实发现和 `scan-result` 上报，但必须清空本地 `visible_hit_queue`，不得点击会话、读取消息、转写语音或上报消息。
- 发现短码命中后，只有该目标也存在于本轮 `read-targets` 且携带当前 `authorization_revision`，才允许进入消息读取。

#### 6.0.1.2 第一屏命中优先读取

第一屏主动扫描发现的命中会话进入 Worker 本地 `visible_hit_queue`。

读取顺序：

```text
第一屏扫描
-> 上报scan-result
-> 拉取并复核read-targets授权
-> 授权命中进入visible_hit_queue
-> 点击会话并同步确认有效短码 + private
-> OmniAuto messages读取
-> 生成dedupe_key
-> 上报/messages/ingest
-> 服务端去重入库
```

`visible_hit_queue` 处理规则：

| 规则 | 说明 |
|---|---|
| 授权后优先读取 | 第一屏命中优先于普通状态机定向读取，但短码命中本身不是读取授权。 |
| 批量上限 | 每轮最多读取配置化数量，第一期建议 3-5 个，避免长期占用微信。 |
| 去重 | 同一轮按 `conversation_id + remark_code` 身份键去重；`remark_code` 是非第一屏微信搜索定位主锚点，`rpa_session_key / display_name / row_fingerprint` 只用于第一屏快速定位和排查证据。 |
| 失败处理 | 找不到目标、OCR低置信、类型为 group/unknown、微信异常或授权已变化时记录错误码，不乱滚、不乱点。 |

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
| 未读到新客户消息 | 创建 `trigger_type=recall` 的召回批次；同一个 Brain/Guard 生成并批准内容后才创建 `chat_reply` 任务。 |
| 找不到会话 / OCR低置信 / 微信异常 | 不发送召回；记录 `RECALL_PRECHECK_FAILED` 或具体错误码，等待人工或下次检查。 |
| 读到销售人工回复 | 不发送召回；状态进入销售已回复后的等待客户状态。 |

#### 6.0.1.5 C2调度顺序

Worker 本地调度不是简单优先级表，而是闭环调度：

```text
1. 当前动作正在执行时，持续检查取消信号和授权版本；停止监听或授权变化后不再开始新的微信动作。
2. 到达第一屏扫描周期时，优先执行 session_scan_visible。
3. 上报 scan-result 后拉取 read-targets；为空时清空 visible_hit_queue，本轮不读消息。
4. 第一屏命中且获得当前授权的会话进入 visible_hit_queue，并优先读取。
5. 点击目标后用顶部标题同步确认有效短码和 private；group/unknown 立即结束本轮。
6. 服务端消息入库后返回当前 batch_id；Worker 保持当前会话和 UI 锁，只等待这个 batch 的 Brain 终态，不处理其他微信会话。
7. 若 Brain 返回 send_reply，Worker 在原会话内执行 pre_send_refresh；通过后再领取当前 chat_reply 和发送许可，输入前、点击前均复核消息序列。
8. 若 Brain 返回 no_action / handoff / technical_failed，或发送结果已经可靠收口，当前会话事务到达终态并释放 UI 锁。
9. 当前会话结束并释放锁后，才处理未被第一屏读取覆盖的 state_target_queue。
10. 召回到期先执行 recall_precheck_read，确认无新客户消息后创建召回 batch；同样保持该会话和 UI 锁等待 Brain 与发送终态。
11. chat_reply 发送完成后按 trigger_type 进入 waiting_user_reply 或 recalled_waiting_user。
12. 不执行旧滚动兜底方案。
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
> diagnostic
```

说明：

- `pre_send_refresh` 只针对即将发送的会话做短读取，防止旧回复发出。
- `session_scan_visible` 和 `message_ingest_visible_hit` 优先体现销售先处理第一屏最新消息的习惯。
- 普通状态机定向读取用于空闲时补齐已知客户，不抢第一屏即时消息。
- 只有 `trigger_type=recall` 的 `chat_reply` 必须先由 `recall_precheck_read` 放行；客户实时回复和好友开场不经过召回预检。

### 6.0.2 C2主动扫描工程规则

主动扫描是 C2 的主工作机制，指 Worker 不等待服务端下发 `chat_reply` 任务，也不等待微信官方推送，而是在本地按规则主动调用 OmniAuto `sessions / messages` 获取微信事实；仅当 `messages` 探测到未转写语音时，才追加调用 `voice-transcribe`。

主动扫描分为事实扫描、授权读取和条件性语音预处理：

| 动作 | 调用能力 | 作用 | 是否进入统一任务中心 |
|---|---|---|---|
| `session_scan` | OmniAuto `sessions` | 扫描会话列表可见区，识别短码、未读提示、会话行特征。 | 否 |
| `message_ingest` | OmniAuto `messages` | 在 read-target 授权内打开/定位 private 会话，读取当前屏消息并上报服务端。 | 否 |
| `voice_transcribe` | OmniAuto `voice-transcribe` | 仅在首次读取发现当前屏未转写语音时，在同一 flow 内逐条调用微信自带转文字。 | 否 |

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
普通首屏扫描和无语音消息读取应保持短动作
多条语音转写属于长动作例外，以“是否持续取得进展”判断卡死，不以普通扫描总时长强行截断
```

以上是调度建议，不作为性能承诺。V16.95 Windows 实测 3 条语音转写耗时约 93 秒，因此 10-20 秒只适用于普通扫描，不适用于正常推进中的语音 flow。

#### 6.0.2.2 主动扫描执行步骤

每轮第一屏主动扫描按以下顺序执行：

```text
1. Worker确认本机在线、微信可控、C2 enabled；第一屏事实扫描允许在read-targets为空时运行。
2. Worker检查是否处于正在执行的微信 UI 动作；若无锁占用，第一屏扫描可优先执行。
3. 如果需要操作微信UI，先获取Local WeChat UI Lock。
4. 调用OmniAuto sessions扫描当前可见会话列表。
5. 普通/增强OCR按视觉行聚合，生成scan_id、sidecar_run_id、rpa_session_key、row_fingerprint、remark_code_candidates和conversation_type证据。
6. 上报 /wechat/sessions/scan-result。
7. Worker调用 /wechat/sessions/read-targets 获取当前读取授权和authorization_revision。
8. read-targets为空：清空visible_hit_queue，释放UI锁，本轮只保留扫描事实。
9. read-targets非空：仅将“有效短码 + 当前授权”命中加入visible_hit_queue，并去重状态机目标。
10. 点击/定位目标后，用同一次顶部标题确认同步检查remark_code和conversation_type。
11. 只有conversation_type=private继续；group/unknown立即终止，不搜索、不读、不转写、不入库。
12. 首次messages读取；发现未转写语音时在同一flow执行voice-transcribe，再做一次最终messages读取和目标复核。
13. Worker构建contract_version=3消息并携带authorization_revision，上报 /wechat/messages/ingest。
14. 服务端先复核授权版本，再按dedupe_key幂等入库，返回ingest_result。
15. 若服务端没有返回需要等待的message_batch，或batch已进入no_action/handoff/failed等终态，Worker释放Local WeChat UI Lock。
16. 若返回处理中batch_id，Worker保持当前会话和UI锁，只等待该batch；Brain终态为send_reply时在原会话执行pre_send_refresh、领取chat_reply和发送许可、输入前/点击前消息序列复核、发送及sent_ack。
17. 当前会话到达可靠终态后统一释放UI锁，记录各阶段耗时和结构化证据，再处理下一个会话。
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
authorization_revision非空且代表当前监听授权版本
```

服务端先按 `last_read_dispatched_at` 从旧到新公平轮询，再以
`read_reason` 优先级和最近可见时间作为同轮排序依据。每次下发目标后必须持久化
派发时间；超过 20 个有效目标时，后续轮次必须覆盖上轮未返回目标，禁止固定截断
导致永久饥饿。长动作的授权复核使用单会话轻量授权接口，不受本轮 20 条发现窗口
影响。

业务优先级：

| 优先级 | read_reason | 说明 |
|---|---|---|
| 1 | `recall_precheck` | 召回到期前确认读取；读取后仍无新消息才允许 chat_reply。 |
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

`conversation_id + remark_code` 是业务身份收口，`authorization_revision` 是本轮读取门票。若历史脏数据或异常绑定导致已绑定会话缺少任一必填值，服务端不得把该会话作为正常 `read-target` 返回，应将绑定/监听状态置为 `degraded` 或 `needs_review`，并记录对应错误码。`rpa_session_key / display_name` 只用于第一屏可见会话的快速定位和排查证据，不作为非第一屏定向读取的必要条件。

#### 6.0.2.4 主动扫描中断与恢复

主动扫描被中断时：

| 场景 | 处理 |
|---|---|
| 当前会话生成 `chat_reply` | 不释放锁、不重新排队；当前 C2 Flow 在原会话完成 `pre_send_refresh`，通过后领取任务和发送许可并发送。 |
| 其他会话已有 `chat_reply` | 不抢占当前会话；等待当前会话达到 no_action/handoff/failed/sent 等可靠终态并释放锁后再处理。 |
| `add_friend` 到达 | 第一屏扫描不抢占正在执行的加好友；若未开始加好友，先完成短切片第一屏扫描和命中读取。 |
| 微信窗口不可控 | 停止本轮扫描，上报 `WECHAT_WINDOW_NOT_READY`。 |
| OmniAuto 超时 | 停止本轮扫描，上报 `RPA_SIDECAR_TIMEOUT`。 |
| Worker 退出/重启 | 不补发、不重复入库；重启后继续按状态机 `read-targets` 读取。 |
| `read-targets=[]` | 允许继续上报第一屏扫描事实；清空本地命中读取队列，不点击会话、不读取、不转写、不入库。 |
| `authorization_revision` 变化 | 当前读取许可失效；停止开始后续动作，旧请求由后端返回 409，不得绕过授权重试入库。 |

主动扫描只能上报事实，不能自己补发消息、不能自己创建 `reply_action`、不能直接改变 `conversation.status`。

### 6.0.3 C2消息去重入库工程规则

Worker 负责生成稳定 `source_message_key` 和候选 `dedupe_key`，服务端负责授权复核和最终去重。后续可以在 Worker 本地按 `conversation_id` 维护最近已上报的 `dedupe_key/content_hash` 小缓存减少重复请求，但该缓存只能是前置优化，不能取消服务端数据库唯一约束。

#### 6.0.3.1 dedupe_key生成规则

优先级如下：

| 优先级 | 来源 | 生成方式 |
|---|---|---|
| 1 | OmniAuto/微信侧稳定来源 ID | 优先使用稳定消息 ID、`canonical_input_id` 或 `canonical_visual_id` 形成 `source_message_key`。 |
| 2 | 语音消息 | 优先使用稳定 voice anchor；缺失时使用“角色 + 归一化正文 + 时长 + 同类序号”等稳定语义身份，不使用页面绝对位置和扫描时间桶。 |
| 3 | 文本消息 | 使用稳定来源身份；缺失时使用“角色 + 归一化正文 + 同类序号”等当前屏内稳定语义身份。 |
| 4 | 系统/文件等兼容消息 | 使用稳定来源 ID 或受约束的结构化内容摘要；无法形成可靠身份时不入库。 |
| 5 | 图片消息 | 优先使用 `canonical_visual_id`；降级为“角色 + 同类出现序号 + 邻近稳定消息锚点”。`image_hash` 只能增强复制后的证据，不能作为复制前唯一身份。 |

文本归一化规则：

```text
去除首尾空白
统一换行和连续空格
语音转写文本必须去除语音时长前缀和 OCR 错识别的时长符号
保留中文、数字、标点本身
不做语义改写
不把不同文本合并成同一内容
```

`occurred_at`、页面纵坐标、当前扫描轮次和 `message_position` 都是观察证据，不是消息身份。微信页面位移、语音文字展开或 Worker 重启后，同一消息的 `source_message_key/dedupe_key` 必须保持稳定。

禁止做法：

```text
不能只用 content 做 dedupe_key
不能只用 occurred_at 做 dedupe_key
不能只用 display_name 做 dedupe_key
不能把绝对页面坐标、screen_order或authorization_revision放入dedupe_key
不能把同一客户连续两句不同内容合并成同一 dedupe_key
不能因为 dedupe_key 生成失败而自动触发 AI 回复
```

#### 6.0.3.2 消息入库事务

服务端收到 `/wechat/messages/ingest` 后必须按事务处理：

```text
1. 强制校验contract_version=3。
2. 校验Worker、conversation_id、remark_code与已绑定会话一致。
3. 校验authorization_revision仍为当前有效监听授权；旧版本请求返回409。
4. 校验每条消息具有source_message_key、dedupe_key、row_kind、sender_role_source、item_state和flow_state。
5. 校验conversation.status和监听状态允许入库。
6. 对每条消息执行数据库唯一键写入。
7. 写入成功：创建message_event，返回ingested。
8. 唯一键冲突：不创建新message_event，返回duplicated。
9. 状态不允许、发送方不明确、目标未确认、群聊/unknown或合同门禁失败：不创建message_event，返回ignored或拒绝整批。
10. 只有ingested且sender_role=customer的消息，才允许触发后续message_batch收集。
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
| 普通聊天文本缺少同行头像证明 | 不入库。只有 `lane_geometry`、没有 `same_row_avatar` 不足以证明 customer/self。 |
| 语音转写文本缺少独立头像 | 只能继承已确认的父语音 `parent_voice` 角色；无法绑定父语音则不入库。 |
| 普通文字/语音发送方无法判断 | 不入库或形成帧级身份门禁，不得触发 AI 回复。 |
| 消息顺序异常 | 不得假定物理处理顺序等于对话顺序。V16.104 已按最终权威画面建立统一 `screen_order`，并完成 Windows 实机回归。 |
| 图片气泡 | 初次观察角色不可信时尚不能建立业务图片身份，形成 `MESSAGE_IDENTITY_UNCONFIRMED` 帧级门禁，零点击且不得持久化 `ignored` Ledger；角色可靠后才建立稳定身份并判定 `NEW_IMAGE`。`OLD/OUTBOX` 不重复复制或调用模型；动作前出屏图片本轮移除；已有稳定身份且仍可见但复核失败时形成 failed 事实。 |

#### 6.0.3.5 逐条结果单调合并

同一画面中每条文字、语音和图片都按 `source_message_key` 独立处理。任何循环、补充读取或后续处理都只能合并结果，不能覆盖整组结果。

| 当前状态 | 新观察/结果 | 合并结果 |
|---|---|---|
| 未处理 | `completed` | 保留 completed 事实。 |
| 未处理 | `failed` | 保留 failed 事实和完整错误证据。 |
| `completed` | 任意较弱结果 | 仍为 `completed`，不得回退。 |
| `failed` | 同一自动 Flow 再次观察 | 仍为 `failed`，不得自动重做昂贵动作。 |
| 任意历史结果 | 新的不同 `source_message_key` | 新增一条独立结果，不得替换历史集合。 |

批次规则：

```text
completed集合只增不减；
failed集合只增不减；
ignored集合只记录明确忽略原因；
每轮结束前所有新槽位必须归入completed / failed / ignored；
不得以最后一次函数返回值替换本轮累计集合。
```

这里的“新槽位”指已经具有可信角色和稳定 `source_message_key` 的业务消息。初次图片观察尚无法确认角色时，不得为了满足终态计数而伪造 `ignored`；它属于本帧身份门禁，等待后续自然画面重新观察。

成功事实正常进入 Outbox 和后端入库。存在 failed `customer/self` 语音或图片时，已成功事实不得丢弃，但必须按最终画面顺序应用同一套上下文门禁；门禁未被可靠的后续销售事实覆盖时阻断 Brain，并按现有业务规则进入 handoff/等待销售流程。失败不能伪装成 `no_action`。`partial` 只是“同一 Flow 同时包含成功和失败”的汇总视图，不是新的单条消息状态，也不创建长期 pending 任务。

#### 6.0.3.6 Outbox统一恢复动作

Outbox 保存已经完成昂贵处理后的完整结构化 V3 JSON，不保存原图。后续失败必须
由一个集中分类器归入以下正式 `recovery_action`：

| `recovery_action` | 适用场景 | 恢复方式 |
|---|---|---|
| `retry` | 网络中断、连接超时、服务端暂时不可用。 | 按有上限的指数退避间隔原样重传同一 JSON 和幂等键；不按固定次数放弃，不得重新读微信、转语音或调用 Vision。 |
| `refresh_and_rebuild` | 授权版本过期或续行票失效，但消息事实本身仍合法。 | 重新取得当前授权，只重建授权/续行外壳；原消息身份、正文、观察时间和证据保持不变，不重复昂贵动作。后端可补录事实，但只有新授权允许时才能推进状态机或 Brain。 |
| `settle_without_ui` | 当前 UI 授权、短码托管或会话状态已变化，但原事实仍可由后端确认归属。 | 取得 settlement token，把同一 JSON 改为 `authorization_scope=fact_settlement`；只结算事实，固定不推进状态机、不启动 Brain。 |
| `rebuild_failed_facts` | 请求中个别语音/图片的失败事实结构不合法，后端能精确指出 source key。 | 只重建指定 failed 事实，不改变其他 completed 事实，不重复媒体动作。 |
| `split_and_retry` | 请求体超过合同上限。 | 按原 read_run 和 source key 清单确定性拆分；最后一片确认前不启动 Brain。 |
| `capability_paused` | 请求级合同、身份或版本整体不兼容，后端无法安全接收整个请求。 | 冻结原 Outbox 和 ledger，阻断新 UI 动作，按退避周期自动探测合同/能力恢复；恢复后转回 `retry`，不建立人工消息处理队列。 |
| `conversation_terminated` | 后端无法安全把内容归入业务会话。 | 后端先持久化 technical terminal 和逐条 source key 结果，再确认 Worker 结束 Outbox；不得静默丢弃。 |

消息级失败不能升级成请求级拒绝。有效会话中的语音或图片失败必须作为
`item_state=failed + error_code` 通过同一个 `messages/ingest` 入库，后端仍返回
`ingested`；附加截图、诊断或非核心证据异常只返回 warning。存在未被后续销售
事实覆盖的 `customer/self` failed 媒体时不把残缺上下文交给 Brain，状态机按正常
业务规则让销售接手会话，但不要求任何人到后台修消息。

兼容字段 `retryable` 不能单独决定 Worker 行为；正式响应必须提供 `recovery_action`。没有该字段时采取最保守策略：不操作微信、不调用 Brain、不发送，冻结原事实并进入 `capability_paused` 自动探测。

Outbox 是 Worker 级全局调度门禁，不只是当前会话的小缓存。只要仍存在一批已经
完成微信读取/语音/Vision但尚未获后端确认的消息事实，Worker 就只能执行该批次
对应的正式恢复动作，不得继续首屏扫描、定向读取、切换会话或执行新微信动作。
网络恢复、授权刷新、事实结算或合同能力恢复后自动续传；不存在按次数放弃、
本地 `not_required` 丢弃或人工修消息的出口。

### 6.0.4 V16状态机定向读取工程方案

V16 是 C2 会话监听的修复 checkpoint。修复目标不是扩展旧滚动兜底方案，而是把 C2 主链路收口为“第一屏主动扫描 + 第一屏命中优先读取 + 短码搜索定向读取 + 召回前 precheck + 去重入库”。

#### 6.0.4.0 定向读取口径修正

定向读取不能理解为“OmniAuto 可以凭 `rpa_session_key` 找到任意历史会话”。当前 `rpa_session_key` 是 Worker/OmniAuto 基于微信当前可见会话行 OCR 信息生成的本地定位键，本质上属于当前窗口、当前列表形态下的行指纹，不是微信官方稳定会话 ID。

因此定向读取分两种路径：

| 路径 | 适用场景 | 定位依据 | 处理要求 |
|---|---|---|---|
| 第一屏可见快速读取 | 目标会话刚被 `sessions` 扫到，仍在当前第一屏可见区。 | `rpa_session_key / display_name / remark_code`。 | 可优先用可见行定位，但读取前仍需确认目标短码。 |
| 短码搜索定向读取 | 服务端 `read-targets` 下发的目标不在第一屏，或第一屏未命中。 | `remark_code`。 | 必须通过微信搜索框搜索短码，找到会话后再次确认标题/备注包含该短码，确认成功才允许读取。 |

`read-targets` 执行前必须先做 `visible-first resolve`，不能只依赖上一次首屏扫描缓存。微信会话列表是动态的，客户新消息可能在 Worker 拉取 `read-targets` 后被顶到第一屏；如果直接搜索短码，会增加不必要的搜索框点击、输入和 UI 风险。

正式执行流程：

```text
1. Worker 拉取 read-targets
2. 获取 Local WeChat UI Lock
3. 在锁内调用 OmniAuto sessions，拿当前实时首屏
4. 用 read-target.remark_code 匹配当前实时首屏
5. 如果唯一命中：
   - 走 visible 路径
   - 点击当前首屏这一行
6. 如果没有命中：
   - 走 search_by_remark_code 路径
7. 如果多条命中：
   - 不读
   - 上报/记录 C2_VISIBLE_TARGET_AMBIGUOUS
8. 定位成功后调用 messages 探测
9. 如发现未转写语音，调用 voice-transcribe
10. 再调用 messages 二次读取
11. ingest
12. 没有 message_batch 或 batch 已终态：释放 Local WeChat UI Lock
13. 返回处理中 batch_id：保持原会话和 UI 锁等待 Brain
14. send_reply：原会话 pre_send_refresh -> claim chat_reply/reply_action -> claim-send -> 输入前/点击前复核 -> 发送 -> sent_ack
15. no_action/handoff/technical_failed 或发送已收口：释放 Local WeChat UI Lock
```

规则：

- `visible-first resolve` 是定向读取的本地定位优化，不是新增授权入口。
- 服务端 `read-targets` 已授权的目标，如果在当前实时首屏唯一命中，可以直接走 visible 路径，不需要再上报 visible_hit 申请许可。
- 首屏唯一命中必须以 `remark_code` 为主锚点；`rpa_session_key / display_name / row_fingerprint` 只能辅助定位和证据排查。
- 首屏没有命中时才允许 `search_by_remark_code`。
- 首屏多条命中同一短码时禁止读取，返回 `C2_VISIBLE_TARGET_AMBIGUOUS`，避免把消息读到错误会话。

禁止做法：

```text
不能把 rpa_session_key 当成跨屏稳定 ID。
不能为了找非第一屏会话执行多屏滚动补偿扫描。
不能在短码未确认时读取当前聊天窗口消息。
不能在搜索结果多义、低置信或标题不含短码时继续读取。
不能跳过当前实时首屏解析，直接对所有 read-targets 执行 search_by_remark_code。
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
| `C2_VISIBLE_TARGET_AMBIGUOUS` | 当前实时首屏中同一 `remark_code` 命中多条会话。 | 不点击、不读取，记录首屏截图和候选列表，等待人工复核或下轮数据修正。 |
| `TARGET_CONFIRM_FAILED` | 点击后标题/备注未确认包含短码。 | 不读取，返回目标不确认。 |
| `TARGET_OCR_LOW_CONFIDENCE` | 搜索结果或标题 OCR 置信度不足。 | 不读取，保留截图证据。 |
| `TARGET_NOT_CONFIRMED_FOR_MESSAGES` | 目标会话未确认，不允许读取消息。 | 不入库，不触发 AI。 |

#### 6.0.4.1 定向读取执行步骤

定向读取要参考 `add_friend` 主链路的工程风格：字段先强校验，校验失败不触达微信 UI；每个 UI 动作都有 step event、截图证据、耗时和错误码；固定坐标兜底必须在报告中明显标红；目标没有二次确认时不得读取消息。

正式 OmniAuto 入口建议保持在 `messages` action 下扩展模式，避免新增一套并行读取协议。`target_mode=auto` 表示先做实时首屏解析，首屏唯一命中则走 visible，未命中才走 `search_by_remark_code`：

```text
action=messages
target_mode=auto | visible | search_by_remark_code
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
| 3 | 实时首屏解析 | 在锁内调用 `sessions`，获取当前这一刻的第一屏会话列表和截图证据，用 `read-target.remark_code` 匹配。 | 唯一命中则进入 visible 路径；未命中才进入搜索路径。 | 多条命中返回 `C2_VISIBLE_TARGET_AMBIGUOUS`；不得点击和读取。 |
| 4A | visible 点击当前行 | 仅实时首屏唯一命中时执行。使用当前首屏候选行安全点击点进入会话，点击前后均记录截图和候选框。 | 微信进入目标会话。 | 返回 `TARGET_CLICK_FAILED`；不得读取当前窗口。 |
| 4B | 搜索路径基线截图 | 仅实时首屏未命中时执行。截取操作前窗口，记录当前标题、窗口位置、DPI、当前选中会话摘要。 | evidence 中有 raw/annotated 截图和窗口元数据。 | 截图失败返回 `SCREENSHOT_CAPTURE_FAILED`；不得继续。 |
| 5 | 定位微信搜索框 | 仅搜索路径执行。复用 OmniAuto locator 思路：优先控件/视觉/OCR 定位微信左上搜索框；固定坐标只能作为最后兜底。 | 搜索框点击点位于微信左侧顶部搜索区域。 | 返回 `SEARCH_BOX_NOT_FOUND`；若使用固定兜底，报告必须标记 `fallback_used=true`。 |
| 6 | 聚焦并清空搜索框 | 仅搜索路径执行。点击搜索框，执行清空动作；允许最多 2 次轻量重试。 | OCR/控件状态确认搜索框为空，或已回到占位符状态。 | 返回 `SEARCH_BOX_CLEAR_FAILED`；不得输入短码。 |
| 7 | 输入短码 | 仅搜索路径执行。按“人工复制短码后粘贴搜索”的习惯输入 `remark_code`，默认使用剪贴板粘贴；粘贴前后必须有短随机停顿；不得高速逐字输入；不得输入其他客户信息。 | 搜索框内容或搜索结果上下文能确认本次查询为该 `remark_code`。 | 返回 `SEARCH_INPUT_VERIFY_FAILED`；清理搜索状态并释放锁。 |
| 8 | 等待搜索结果稳定 | 仅搜索路径执行。等待搜索结果刷新，至少两帧 OCR 结果稳定，或达到配置超时。 | 候选结果列表稳定。 | 返回 `TARGET_SEARCH_TIMEOUT`；不得点击不稳定结果。 |
| 9 | 解析候选结果 | 仅搜索路径执行。只接受“联系人/会话标题/备注”包含 `remark_code` 的候选；单纯消息内容命中不能作为目标。 | 唯一候选包含 `remark_code`。 | 0 个候选返回 `TARGET_SEARCH_NOT_FOUND`；多个候选返回 `TARGET_SEARCH_AMBIGUOUS`。 |
| 10 | 点击唯一候选 | 仅搜索路径执行。使用候选行安全点击点进入会话，点击前后均记录截图和候选框。 | 微信进入候选会话。 | 返回 `TARGET_CLICK_FAILED`；不得读取当前窗口。 |
| 11 | 二次确认目标 | 进入会话后 OCR 标题/备注/当前选中行，必须确认包含 `remark_code`。`display_name` 只能辅助，不能替代短码。 | `target_confirmed=true`，确认来源写入 evidence。 | 返回 `TARGET_CONFIRM_FAILED / TARGET_NOT_CONFIRMED_FOR_MESSAGES`；不得读取消息。 |
| 12 | 读取消息 | 复用 OmniAuto `messages` 解析能力读取当前会话可见消息，输出 `sender_role_hint / message_type / content / occurred_at / raw_payload`。 | 返回消息列表或明确空结果，并带 `target_confirmed=true`。 | 读取失败返回 `MESSAGE_READ_FAILED`；不得伪造空成功。 |
| 13 | 生成结果与证据 | 输出 `read_run_id / conversation_id / remark_code / target_mode / target_confirmed / messages / evidence / step_events`。 | Worker 可上报后端 `/wechat/messages/ingest`。 | 结果缺关键字段视为 `C2_MESSAGE_READ_RESULT_INVALID`。 |
| 14 | 清理或保持安全状态 | 成功读取后可保持目标会话打开，供 `pre_send_refresh` 后继续发送；失败时清理搜索框或回到安全状态。 | 不影响下一次 add_friend / scan / send 操作。 | 清理失败记录 `SEARCH_STATE_CLEANUP_FAILED`，但不得继续执行发送。 |

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
正常 C2-C3 链路必须先完成 pre_send_refresh，再 claim 当前 batch 的 chat_reply / reply_action。
仅崩溃恢复时可能接手已处于 running / claimed 的任务；恢复线程也必须先重建原会话并完成 pre_send_refresh，目标未确认时禁止发送。
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
| `send_queue` | 服务端 `chat_reply` 任务。 | 只发送服务端已批准且仍有效的内容；召回通过批次 `trigger_type=recall` 区分。 |

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
正常链路：C2已持有目标会话和UI锁
-> 等待当前batch的Brain/Guard终态
-> Brain批准send_reply
-> 在原会话执行pre_send_refresh，不重新搜索或切换客户
-> 服务端按dedupe_key入库，并判断旧reply_action是否已被新消息作废
-> 没有上下文变化时，claim当前chat_reply/reply_action和claim-send
-> 把pre_send_refresh消息序列传入Sidecar
-> 输入前和点击前各复核一次消息序列
-> 发送并上报sent_ack

崩溃恢复链路：确认原C2 Flow已不存在
-> 获取UI锁
-> 按conversation_id + remark_code重建并严格确认原会话
-> 执行完整pre_send_refresh
-> 后续复用上述唯一发送流程
```

判断分支：

| 结果 | 处理 |
|---|---|
| 没有新客户消息，且输入前/点击前消息序列均未变化 | 允许 Worker 发送原 reply_action。 |
| 有新客户消息 | 原 reply_action 置为 `superseded`，不发送；新消息进入 message_batch 重新生成回复。 |
| 读取失败/目标不确认 | 不发送，返回错误码，等待重试或人工处理。 |
| 输入前或点击前消息序列变化 | 返回 `C3_CONTEXT_CHANGED_BEFORE_SEND`；只允许清理能证明属于本次程序的草稿，不点击发送，新事实进入下一轮读取和 batch。 |

#### 6.0.4.5 召回前 recall_precheck

`chat_reply` 任务不能仅按时间直接创建或发送。服务端发现召回到期后，先生成 `read_reason=recall_precheck` 的读取目标。

流程：

```text
等待用户回复超过N天
-> 服务端返回 recall_precheck read-target
-> Worker 定向读取该会话
-> 服务端去重入库并重新判断
-> 无新客户消息才创建 chat_reply 任务
```

判断分支：

| 结果 | 处理 |
|---|---|
| 读到客户新消息 | 取消召回，进入 C3 AI 回复/转人工判断。 |
| 未读到新客户消息 | 创建 `trigger_type=recall` 的召回批次；同一个 Brain/Guard 生成并批准内容后才创建 `chat_reply` 任务。 |
| 读到销售人工回复 | 不召回，状态进入 `sales_replied_waiting_user`。 |
| 读取失败/找不到会话 | 不召回，记录 `RECALL_PRECHECK_FAILED` 或具体错误码。 |

#### 6.0.4.6 语音消息识别接入

语音消息属于 C2 微信事实采集能力，不属于 C3 AI 生成能力。Worker 必须先把客户语音转成可入库的 `message_event`，服务端才能判断是否进入 C3。

OmniAuto 当前语音能力不是导出微信语音文件后调用外部 ASR，也不是读取微信数据库。它的实现方式是：

```text
打开/确认目标会话
-> OCR 当前聊天窗口
-> 识别可见语音气泡或“转文字”按钮
-> 模拟人工 hover / 点击微信自带语音转文字
-> 等待微信生成转写文本
-> 再次 OCR 当前聊天窗口
-> 对比点击前后的消息列表
-> 把新增文本作为 transcribed_messages 返回
```

OmniAuto 已提供的动作：

| 能力 | 入口 | 说明 |
|---|---|---|
| 语音转文字 | `voice-transcribe` | 点击微信自带语音转文字按钮，返回 `transcribed_messages / new_messages / attempts / screenshot_path` 等证据。 |
| 消息读取 | `messages` | 读取当前会话可见观察；语音转写文字可以被 OCR 观察到，但必须绑定父语音，不能作为独立普通文本入库。 |

Worker C2 读取某个会话时，执行顺序必须调整为：

```text
获取本地微信 UI 锁
-> 定位目标会话：第一屏 visible 或 remark_code 搜索
-> 调用 OmniAuto messages 做首次读取/消息类型探测
-> 如果没有未转写语音：直接转换并上报 message_event
-> 如果发现未转写语音：调用 OmniAuto voice-transcribe，在同一flow内处理当前屏全部可处理语音
-> 每次点击或页面变化后重新截图，旧坐标立即失效；已完成语音加入本轮processed集合，不能重复右键
-> voice-transcribe 后调用一次最终messages，新截图同时完成目标复核和消息读取
-> 以最终有效画面建立文字、语音、图片统一slots和screen_order
-> 为每个slot生成稳定source_message_key，并查询本地ledger/Outbox判定NEW、OLD、OUTBOX_WAITING或身份冲突
-> 初次图片同行头像角色不可信时形成帧级身份门禁，不建立图片消息、不写ignored Ledger
-> 只对NEW_IMAGE执行一次图片右键复制和进程内真实OmniAuto Vision；旧图片和Outbox图片不得重复复制或重复计费
-> 图片成功回填customer_image_understanding/visual_bridge_input；右键前仍可见但身份无法唯一确认、复制失败、Vision失败或结果非法均回填failed事实；被顶出最终当前屏的图片本轮不建立槽位；禁止会话内图片deferred/pending
-> 按最终screen_order收集本轮新文字、已绑定父语音和图片终态，转换为message_event
-> 上报 /api/workers/{worker_id}/wechat/messages/ingest
-> 无需等待batch或batch已终态：释放本地微信 UI 锁
-> 需要等待当前batch：保持原会话和UI锁，继续Brain、pre_send_refresh和发送收口
```

语音转写不单独创建任务，不进入任务中心。它是 `message_ingest` 前的条件性本地预处理步骤，只有首次 `messages` 读取/探测发现未转写语音时才执行，和 `messages` 读取共享同一把 `Local WeChat UI Lock`。不得出现一边执行 `add_friend/chat_reply/send`，另一边点击语音转文字的并行操作。

定位目标会话只能做一次：第一屏 visible 命中或 `search_by_remark_code` 定向搜索成功后，后续 `messages` 和 `voice-transcribe` 都必须在同一个已确认会话内执行。`voice-transcribe` 不得再次搜索客户，`messages` 二次读取也不得再次搜索客户；如果中途发现当前会话标题、短码或会话指纹不匹配，必须失败退出并记录证据，不能继续点击或读取。

Worker 上报语音消息时：

| 字段 | 规则 |
|---|---|
| `message_type` | 使用 `voice`。如果 OmniAuto 只能输出转写后的文本，Worker 仍应根据 `voice_transcription.transcribed_messages` 或 `quality_flags=voice_duration_prefix_removed` 标记为 `voice`。 |
| `content` | 保存微信转写后的文本。 |
| `sender_role_hint` | V3 合同只使用 `customer / self / system / unknown`；销售本人、历史 `sales / sales_candidate` 在合同边界统一归一为 `self`。语音角色继承原语音气泡/父语音证据。 |
| `raw_payload` | 必须保存 OmniAuto 原始消息、稳定 voice anchor、父语音绑定、`voice_transcription_meta`、截图引用和质量标记。 |
| `ocr_confidence` | 使用转写文本 OCR 置信度或消息解析置信度；没有则为空。 |

语音转写状态处理：

| OmniAuto 状态 | Worker处理 | 服务端处理 |
|---|---|---|
| `voice_transcribe_completed` | 继续调用 `messages`，把转写文本入库。 | 按正常 `message_event` 处理；客户语音可触发 C3。 |
| `voice_transcribe_partial` | 同一 flow 内存在成功和失败；只保留已绑定父语音且 `item_state=completed` 的结果，失败项不伪造。 | 成功事实可入库；该状态只是本轮结果摘要，不创建持久化语音任务。 |
| `voice_transcribe_no_new_text` | 已尝试但没有确认出对应文字，阻断本轮最终消息上报，保留证据。 | 不把时长或疑似文本入库，不触发 C3。 |
| `voice_transcribe_no_visible_voice` | 继续调用 `messages`；说明当前可见区没有待转写语音。 | 不作为错误。 |
| `voice_transcribe_target_not_found` | 目标会话或目标语音未能确认，按安全失败阻断；不能与 `no_visible_voice` 混为一谈。 | 不入库，不触发 AI。 |
| `target_not_confirmed_for_voice_transcribe` | 停止读取该目标，不调用 `messages`。 | 记录读取失败，不触发 AI。 |
| `voice_transcribe_click_failed` | 停止读取该目标或按配置降级为只读已有文本；默认不触发 AI。 | 记录 `VOICE_TRANSCRIBE_CLICK_FAILED`。 |
| `voice_transcribe_lock_timeout` | 本轮跳过，等待下轮。 | 记录 `VOICE_TRANSCRIBE_LOCK_TIMEOUT`。 |
| `voice_transcription_exception` | 本轮失败。 | 记录 `VOICE_TRANSCRIBE_FAILED`，不得创建 `reply_action`。 |

语音转写失败时，不能把“语音时长 5 秒”当成客户内容，也不能因为没有文本就认为客户沉默。V16.98 的 `item_state/flow_state` 是单次 flow 的结果合同，不是新增数据库状态机，也不要求拆出新的语音任务。默认规则：

```text
目标客户已确认，且客户语音可见但转写失败
-> 不进入 C3 自动回复
-> conversation.status 进入 waiting_sales_reply
-> ai_enabled 保持原值，由 waiting_sales_reply 状态门禁阻断普通实时回复
-> 记录 error_code
-> 后台可展示“语音转写失败，需人工查看”
```

如果目标客户未确认，例如 `target_not_confirmed_for_voice_transcribe`，不得改变会话主状态，只记录读取失败证据，避免把别人的会话误判为当前客户。

服务端判断规则：

- `sender_role=customer` 且 `message_type=voice` 且 `content` 非空：等同客户新消息，可触发 C3。
- `sender_role=self` 且 `message_type=voice`：等同销售侧人工回复，不触发 AI，并按销售接管/AI 暂停处理。
- `sender_role=unknown` 或 `content` 为空：不触发 AI，记录 `VOICE_MESSAGE_UNCONFIRMED` 或 `VOICE_TRANSCRIBE_EMPTY`。

实机验证要求：

```text
1. 客户发一条语音，Worker 能点击微信语音转文字并入库为 message_type=voice。
2. 客户连续发文字+语音，服务端能按同一 conversation_id 合并上下文。
3. 销售手机端发语音，桌面同步后不得触发 AI 自动回复。
4. 语音转写失败时不触发 AI、不召回、不重复点击。
5. Worker 重启后同一条语音转写文本不重复入库、不重复触发 C3。
6. add_friend / chat_reply / pre_send_refresh 与 voice-transcribe 共享本地 UI 锁，不允许并行操作微信。
```

语音 flow 的时间保护采用两层安全机制：

| 机制 | V16.98 默认口径 | 目的 |
|---|---|---|
| 无进展 watchdog | 240 秒无任何新截图、成功转写或处理进展才停止 | 识别 OCR/微信 UI/sidecar 真正卡死，不打断正常慢流程。 |
| 硬安全上限 | 900 秒 | 仅防止进程永久占锁，是最终保险丝，不是正常业务时限或性能承诺。 |

只要持续取得进展，语音 flow 应继续处理当前屏全部可处理语音；不能套用普通首屏扫描的 10-20 秒建议。触发安全保护时必须显式返回已完成项、失败项、停止原因和证据，不得把已成功语音降级成普通 `text`。

语音处理期间若最终画面出现新的可见未转写语音，不释放当前会话和 UI 锁，也不机械留到下一轮。Worker 应按新的稳定语音身份继续调用 `voice-transcribe`；前一条失败语音只加入本轮排除集合，不得连坐后来出现的其他语音。待处理集合缩小或变化即视为有进展。只有同一稳定待处理集合连续不变、sidecar 明确失败、授权撤销或会话无法确认时才结束，并将明确失败项同批上报门禁。

#### 6.0.4.7 V16.98 当前冻结口径

本节是 C2 当前实现和验收的最高优先级口径；与前文历史 V16/V17 描述冲突时，以本节为准。

**准入门：**

```text
有效客户短码
AND 顶部标题确认conversation_type=private
AND conversation_id/remark_code匹配当前read-target
AND authorization_revision仍有效
= 才允许读取、转写和入库
```

`conversation_type` 只用于准入，不写入会话主身份，不参与 `source_message_key/dedupe_key`。判断复用点击会话后已有的顶部标题 OCR：标题末尾明确人数后缀如 `(6)`、`（6）` 判为 group；证据不足、不同短码冲突或类型冲突判为 unknown；只有明确 private 才放行。

**终止态：**

| 错误码/状态 | 行为 |
|---|---|
| `C2_GROUP_CHAT_NOT_ALLOWED` | 本轮立即结束，不再搜索、不读文字/语音/图片、不转写、不入库。 |
| `C2_CONVERSATION_TYPE_UNKNOWN` | 本轮立即结束，等待证据改善或人工处理，不降级为 private。 |
| `C2_VISIBLE_TARGET_AMBIGUOUS` | 当前首屏同码候选不唯一，不点击、不读取、不继续搜索。 |

普通首屏确实没有目标且没有得到以上终止态时，才允许 `search_by_remark_code` 兜底。搜索后仍必须再次确认有效短码和 private。

**扫描与读取分离：**

- `first_screen_session_scan` 是事实发现，可以在 `read-targets=[]` 时继续运行并上报 `scan-result`。
- `read-targets` 是读取许可；为空时必须清空本地 visible hit 队列。
- 暂停监听或重新授权会产生新的 `authorization_revision`；旧 Worker ingest 必须返回 409。
- 停止后的验收重点是不得再定位目标、读取、转写或入库；常驻首屏事实扫描本身不等于尾随读取。

**V3 消息合同：**

```text
contract_version=3
authorization_revision
source_message_key
row_kind
sender_role_source
item_state
flow_state
```

- 正式发送方角色为 `customer / self / system / unknown`；`sales / sales_candidate` 仅作旧输入兼容，进入合同边界前归一为 `self`。
- 普通聊天消息必须由同一行头像结构证明角色；只有左右 lane 推测不足以入库。
- 语音转写行必须绑定父语音，继承父语音角色和稳定 anchor，最终只形成一条 `voice` 消息。
- `unknown`、未确认目标、未完成语音、通话/非聊天 UI 和低置信伪消息不得进入 C3。
- 当前只处理当前屏：`history_load_times=0`、`max_scroll_steps=0`、`max_snapshots=1`；不会为寻找屏幕外历史语音主动上滚。
- 同一业务阶段尽量复用同一截图完成 OCR；发生点击、转写展开或其他页面变化后必须重新截图。

**性能与可观测性：**

- 保留目标定位、首次读取、voice flow、最终读取和 ingest 的分阶段耗时。
- 整帧 OCR 漏标题时，只对同一截图标题栏做 ROI 补充 OCR，不为补标题重新截图。
- 后台 status OCR 按心跳周期运行，UI Lock 占用期间不得并发抢微信窗口。
- V16.95 Windows 证据显示：首屏目标定位 8.13 秒、首次读取 8.05 秒、3 条语音转写 93.01 秒、最终读取 8.54 秒；这些是现场结果，不是 SLA。

**尚未纳入 V16.98：**

- 图片复制、临时图像获取、Vision 调用、图片结构化结果和图片入库合同。
- 文字/语音混合消息的统一 `screen_order` 合同；该能力不属于 V16.98，后续由 V16.100 开始开发，并已随 V16.104 完成 Windows 实机回归。
- C3 自动回复发送和 C4 自动召回实机联动。

#### 6.0.4.8 V16.98验收标准

V16.98 通过标准：

```text
1. Worker 能优先扫描微信当前第一屏并上报 session_scan_result。
2. 第一屏命中会话能优先读取并上报 message_event。
3. 第一屏扫描结果不能直接授权读取；Worker 必须用当前 read-targets 和 authorization_revision 复核。
4. Worker 执行 read-target 前必须在 UI 锁内重新调用 `sessions` 做当前实时首屏解析；首屏唯一命中则走 visible 路径，不再搜索短码。
5. 当前实时首屏没有命中时，才允许通过微信搜索框搜索 remark_code，并在标题/备注二次确认短码后读取。
6. 当前实时首屏同一短码多条命中时，不读取并返回 `C2_VISIBLE_TARGET_AMBIGUOUS`。
7. read-targets为空时清空本地命中队列，不读取、不转写、不入库，但允许继续首屏事实扫描。
8. rpa_session_key 只作为第一屏可见会话辅助定位，不作为跨屏定向读取依据。
9. chat_reply 发送前执行 pre_send_refresh，新客户消息出现时旧 reply_action 不发送。
10. 召回到期先执行 recall_precheck，确认无新客户消息后才创建/发送 chat_reply。
11. 不执行旧滚动兜底方案。
12. 重复读取不会重复入库、重复触发 AI、重复发送回复。
13. 找不到目标会话、搜索多义、标题不含短码或 OCR 低置信时不乱读、不乱点、不乱发。
14. 只有有效短码且顶部标题明确为private的会话准入；group/unknown是终止态，不再搜索、读取、转写或入库。
15. 语音消息先由 `messages` 探测；只有发现未转写语音时才执行 `voice-transcribe`，成功正文绑定父语音并以 `message_type=voice` 入库，不产生重复 text。
16. 语音物理操作可自下而上，但每次页面变化后重新截图；本轮已完成语音不得重复右键。
17. 普通聊天角色必须有同行头像证据，销售侧统一为self；unknown不触发AI。
18. contract_version=3和authorization_revision强制校验；停止/重授权后的旧请求返回409。
19. 语音转写失败不得触发AI自动回复，不得把语音时长当作客户文本。
20. 图片识别和统一消息顺序不作为V16.98通过项，必须由后续专项版本另行验收。
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

说明：V16.98 的 `conversation_type=private/group/unknown` 是 Worker 每次点击后根据顶部标题生成的准入证据，放在扫描/定位原始证据中即可；当前不要求为它新增独立数据库状态机字段，也不允许它改变 `conversation_id` 或消息去重身份。

#### 6.1.2 message_events

| 字段 | 说明 |
|---|---|
| `id` | 消息事件 ID。 |
| `conversation_id` | 会话 ID。 |
| `worker_id` | Worker ID。 |
| `rpa_session_key` | 本机会话定位键。 |
| `dedupe_key` | 去重键，同一消息只能处理一次。 |
| `sender_role` | V3 正式值为 `customer / self / system / unknown`。`customer` 才允许进入 C3 AI 回复判断；`self` 表示销售/本机侧消息，不触发 AI；`system/unknown` 不触发 AI。旧 `sales / sales_candidate` 必须在合同边界前归一为 `self`。 |
| `message_type` | V3 合同枚举为 `text / image / voice / system / file / unknown`。`voice` 表示转写正文与父语音绑定后形成的单条事实；`image` 只允许明确 `completed` 或结构完整的 `failed` 终态。 |
| `content` | 文本内容或消息摘要。 |
| `raw_payload` | OmniAuto 原始结构化结果。 |
| `ocr_confidence` | OCR 置信度。 |
| `occurred_at` | 微信侧推断时间。 |
| `ingested_at` | 服务端入库时间。 |

V3 结构化证据还必须保存在 `raw_payload`：`source_message_key / row_kind / sender_role_source / item_state / flow_state / canonical_visual_id / canonical_input_id`；语音额外保存稳定 anchor 和 `voice_transcription_meta`；图片只保存白名单投影后的 `customer_image_understanding / visual_bridge_input`。这些字段用于证明“哪条气泡、谁发的、是否完成”，不是新增业务状态机。

说明：`message_events` 保存已准入的消息事实，包括结构完整的失败图片事实；不保存 `duplicated / ignored / discovered / pending`。初次图片角色不可信时尚未形成可入库消息，只上报稳定帧级身份门禁。接口处理结果继续放在 `results[].ingest_result` 和 `error_code` 中。

### 6.2 C2服务端接口契约

C2 消息接口只接收 Worker 上报的微信事实，不接收 Worker 生成的 AI 回复内容，也不直接下发发送动作。消息入库后，后端状态机可以基于同一批事实启动 Brain、创建 `reply_action` 和任务中心 `chat_reply`；当前持锁的 C2 单会话流程再按批次合同领取并执行该任务。

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

V16.98 不在 `sessions[]` 中新增 `conversation_type` 字段：群聊/unknown 的会话行通过清空 `remark_code_candidates` 阻止自动绑定；扫描级 `evidence.c2_conversation_admission` 只保存 private/group/unknown 数量和规则摘要。点击目标后的详细 `raw_title / conversation_type / conversation_type_reason` 保存在 Worker/Sidecar 定位证据中，用于本轮终止判断，不发散新的后端绑定字段。

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
| `authorization_revision` | 当前监听授权版本；必填。停止监听或重新授权后会变化，旧版本请求不得入库。 |

契约要求：

- 正常 `read-targets.targets[]` 必须包含 `conversation_id + remark_code + authorization_revision`。
- 已绑定会话如果缺少 `remark_code`，不得出现在正常 `read-targets` 中，应进入 `needs_review / degraded` 并记录 `C2_TARGET_REMARK_CODE_MISSING`。
- Worker 收到缺少 `remark_code` 的读取目标时，必须跳过读取，不得继续打开微信会话或上报消息。
- Worker 本轮读取去重以 `conversation_id + remark_code` 作为身份键；服务端身份收口必须满足 `conversation_id + remark_code`。
- `authorization_revision` 只代表本轮读取许可，不参与会话身份和消息 `dedupe_key`。
- 微信定位分两段：第一屏可见目标可用 `rpa_session_key / display_name / remark_code` 快速定位；第一屏未命中或非第一屏目标必须通过微信搜索框搜索 `remark_code`，并在进入会话后再次确认标题/备注包含该短码。
- `rpa_session_key`、`display_name`、`row_fingerprint` 仅作辅助定位和排查证据，不能替代 `remark_code` 做非第一屏定向读取。

#### 6.2.3 上报消息事件

```http
POST /api/workers/{worker_id}/wechat/messages/ingest
```

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `contract_version` | integer | 是 | 当前固定为 `3`，并同时校验 `contract_revision / contract_sha256 / observation_schema_version`。 |
| `read_run_id` | string | 是 | 本次读取运行 ID。 |
| `conversation_id` | string | 是 | 服务端已绑定会话 ID。 |
| `remark_code` | string | 是 | 本轮已确认的客户短码。 |
| `authorization_revision` | string | 是 | 必须与服务端当前 read-target 授权一致。 |
| `rpa_session_key` | string | 否 | 本机会话定位键；第一屏读取时建议上报，短码搜索读取时可为空或上报搜索后重新识别到的定位键。 |
| `messages` | array | 是 | 本次读取到的消息事实。 |
| `evidence` | object | 否 | 截图、日志、OCR摘要。 |

`sidecar_run_id` 放入 `evidence.sidecar_run_id` 和每条原始证据中，不新增为后端请求顶层必填字段，避免 Worker 与后端产生一组实际未消费的冗余接口字段。

`messages[]` 字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `source_message_key` | string | 是 | 消息稳定来源身份；页面移动、重启和授权版本变化后保持稳定。 |
| `dedupe_key` | string | 是 | 消息去重键；同一会话内唯一，不能包含页面坐标或授权版本。 |
| `sender_role_hint` | string | 是 | `customer / self / system / unknown`；unknown 不入库、不触发 AI。 |
| `message_type` | string | 是 | `text / image / voice / system / file / unknown`；图片只允许明确成功或结构完整的失败事实。 |
| `content` | string | 否 | 文本内容或消息摘要。 |
| `occurred_at` | datetime | 否 | 微信侧推断时间。 |
| `ocr_confidence` | number | 否 | OCR 置信度。 |
| `item_state` | string | 是 | 普通消息必须为 `completed`；图片允许 `completed / failed`，`discovered / ignored / pending` 不入库。 |
| `flow_state` | string | 是 | `completed / partial / failed / cancelled`；表示本轮 flow 结果，不创建独立任务。 |
| `raw_payload` | object | 否 | OmniAuto 原始结构化结果。 |

语音正文放在顶层 `content`，`raw_payload.voice_transcription` 仅保留同一正文兼容值；结构化转写证据统一放入 `raw_payload.voice_transcription_meta`，不在 `messages[]` 顶层继续发散语音专属字段。推荐结构：

| 字段 | 说明 |
|---|---|
| `state` | OmniAuto 语音转写状态，例如 `voice_transcribe_completed`，位于 `voice_transcription_meta`。 |
| `voice_anchor_stable_key` | 父语音稳定身份，用于证明正文归属。 |
| `attempt_count` | 本轮点击/识别尝试次数。 |
| `transcribed_messages_count` | 成功识别出的转写消息数量。 |
| `before_screenshot_path` / `after_screenshot_path` | 语音转写前后截图证据。 |
| `quality_flags` | 例如 `voice_duration_prefix_removed`，用于证明已去除语音时长噪声。 |

响应字段：

| 字段 | 说明 |
|---|---|
| `ingested_count` | 新入库消息数量。 |
| `duplicated_count` | 被去重跳过数量。 |
| `ignored_count` | 因状态不允许或发送方不明确跳过数量。 |
| `results[]` | 每条消息的处理结果，包含 `dedupe_key`、`ingest_result=ingested/duplicated/ignored`、`message_event_id`、`error_code`。 |
| `next_action` | 兼容字段，固定为 `none`；发送动作不从消息入库响应直接下发。 |
| `message_batch` | 可选。后端因本批事实启动 C3 时返回 `batch_id / batch_status`，Worker 只按该批次继续查询、领取任务和执行发送。 |

兼容说明：原 C2 字段保持兼容，C2-C3 单会话串行链路只增加可选 `message_batch={batch_id,batch_status}`。具体等待和终态查询合同见 `C2-C3_OmniAuto_Worker_后端接口合同_v0.1_2026-07-21.md`，不得另建同义消息上报或回复接口。

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
- `needs_review / binding_failed / unbound / degraded / paused` 只是当前不可读或
  暂不可判定，不是媒体恢复的永久终止证明；存在已触发语音/图片动作或待确认
  Outbox 时，原事务身份可信则直接 `fact_only` 结算，身份暂不可证才
  `retry_later`，两者都不能丢弃本地记录。
- `disabled` 优先级最高；会话关闭、拒绝或短码移除后，后续扫描不得重新开启监听，除非人工重新生成/恢复短码并重新绑定。
- `disabled` 只终止新的 UI 和业务自动化；禁用前已经产生的消息事实仍须通过
  `fact_settlement` 或 `technical_terminal` 得到后端逐条确认。
- 已有 bound 会话单次首屏未识别到短码时进入 `degraded`。只有后台明确关闭，
  或后端签发只允许读取标题的 `binding_recheck`，并由两张独立稳定标题 ROI
  同时确认 private 且原短码不存在后，才能上报
  `remark_code_removed_confirmed` 并进入 `disabled`；该检查禁止读取消息和媒体。
- C2 Worker 只提交消息事实，不创建 `reply_action` 或回复文案；后端状态机可以根据该批事实启动 Brain、创建 `reply_action` 和任务中心任务。
- `messages/ingest` 不直接下发发送动作；当前 C2 单会话流程通过 `message_batch` 续行票等待 Brain，并领取同一批次的 `chat_reply`。
- Worker 为 `paused` 时停止所有微信 UI 操作，包括首屏扫描、定向读取、语音、图片、加好友和发送；心跳与已落入本地 Outbox 的事实重传可以继续。
- Vision 配置属于“新 C2 UI 流程”的启动前置条件：API Key、模型或地址缺失时
  C2 为 `vision_not_ready`，不得开始新的扫描或打开会话；但已落盘的
  `sent_ack`、消息 Outbox 和无需 UI/Vision 的事实结算必须先于能力预检继续执行。
- 服务端是状态唯一事实源；Worker 只上报扫描和消息事实，不直接改变最终业务状态。

### 6.4 C2错误码

| 错误码 | 触发场景 | 处理 |
|---|---|---|
| `SESSION_SCAN_FAILED` | 会话列表扫描失败。 | 记录证据，监听状态进入 `degraded/error`。 |
| `SESSION_REMARK_CODE_NOT_FOUND` | 新会话未识别到短码，或已有绑定本次未观察到短码。 | 新会话不准入；已有 bound 会话单次缺失只进入 `degraded` 并保留原绑定，不能据此确认短码移除。 |
| `SESSION_REMARK_CODE_INVALID` | 短码格式非法或不存在。 | `bind_status=binding_failed`。 |
| `SESSION_REMARK_CODE_DUPLICATED` | 一个短码匹配多条活动线索。 | `bind_status=needs_review`。 |
| `SESSION_BINDING_CONFLICT` | 短码匹配到其他销售或 Worker。 | `bind_status=needs_review`。 |
| `SESSION_BINDING_DISABLED` | 会话已关闭、拒绝或短码已移除。 | `bind_status=disabled`。 |
| `C2_TARGET_REMARK_CODE_MISSING` | 已绑定会话缺少 `remark_code`，不能作为定向读取目标。 | 不返回正常 read-target；进入 `degraded/needs_review` 并提示修复绑定数据。 |
| `C2_TARGET_CONVERSATION_ID_MISSING` | 已绑定会话缺少 `conversation_id`，不能作为定向读取目标。 | 不返回正常 read-target；进入 `degraded/needs_review` 并提示修复绑定数据。 |
| `C2_VISIBLE_TARGET_AMBIGUOUS` | 执行 read-target 前实时首屏解析发现同一 `remark_code` 命中多条会话。 | 不点击、不读取、不入库，记录首屏截图和候选列表。 |
| `C2_GROUP_CHAT_NOT_ALLOWED` | 顶部标题明确为群聊。 | 本轮终止，不搜索、不读取、不转写、不入库。 |
| `C2_CONVERSATION_TYPE_UNKNOWN` | 顶部标题证据不足或冲突，无法确认 private。 | 本轮终止，不降级准入。 |
| `C2_TARGET_AUTHORIZATION_REVISION_MISSING` | read-target 或 ingest 缺少授权版本。 | 拒绝读取或入库。 |
| `MESSAGE_AUTHORIZATION_REVISION_EXPIRED` | Worker 使用停止/重授权前的旧版本上报。 | 返回 409，不写库；Worker停止后续动作并刷新read-targets。 |
| `MESSAGE_READ_FAILED` | 已绑定会话读取消息失败。 | 记录证据，不改变业务状态。 |
| `MESSAGE_CONVERSATION_NOT_BOUND` | 上报消息的会话未绑定。 | 拒绝入库。 |
| `MESSAGE_DEDUPE_KEY_MISSING` | 消息缺少去重键。 | 拒绝入库。 |
| `MESSAGE_INGEST_DUPLICATED` | 去重键已存在。 | 返回 duplicated，不算失败。 |
| `C2_VISION_NOT_READY` | 新 C2 UI 流程启动前发现真实 Vision Provider 的 API Key、模型或地址缺失。 | 不开始新扫描、不打开会话；已有回执、Outbox 和 `settle_without_ui` 仍先恢复。 |
| `C2_IMAGE_SLOT_RECONFIRM_FAILED` | 图片仍在最终当前屏，但动作前无法用稳定身份唯一确认原槽位。 | 不右键、不调用 Vision；以 failed 图片事实入库并阻断 Brain，不跨轮重试。 |
| `C2_IMAGE_MENU_OPERATION_FAILED` | 图片槽位已确认且右键已执行，但菜单未准备好、未识别到“复制”或菜单项无法安全点击。 | 关闭菜单，不读取剪贴板、不调用 Vision；以 failed 图片事实入库并阻断 Brain。 |
| `VOICE_TRANSCRIBE_FAILED` | 语音转文字整体失败，无法确认有效转写文本。 | 记录证据，不触发 C3，不生成 `reply_action`。 |
| `VOICE_TRANSCRIBE_CLICK_FAILED` | OmniAuto 找到疑似语音转文字入口，但点击或转写动作失败。 | 记录截图和 OCR 证据，不自动重试发送，不触发 AI。 |
| `VOICE_TRANSCRIBE_LOCK_TIMEOUT` | 语音转写等待 Local WeChat UI Lock 超时。 | 本轮跳过，保持会话原状态，后续扫描可再尝试。 |
| `VOICE_TRANSCRIBE_EMPTY` | 转写动作完成但未产生新的可用文字。 | 不把语音时长当正文，不触发 AI。 |
| `VOICE_MESSAGE_UNCONFIRMED` | OCR 只识别到语音形态或时长，未确认转写文本。 | 记录为待人工查看或下轮扫描重试，不触发 AI。 |
| `TARGET_NOT_CONFIRMED_FOR_VOICE_TRANSCRIBE` | 定向读取时未能通过短码/会话标题确认目标客户。 | 禁止点击和读取，避免读错人。 |
| `WECHAT_WINDOW_NOT_READY` | 微信窗口不可控。 | Worker 状态异常，暂停读取。 |
| `RPA_SIDECAR_TIMEOUT` | OmniAuto Sidecar 超时。 | 记录证据，不自动重放。 |

### 6.5 C2验收标准

- Worker 能调用 OmniAuto 扫描微信会话列表，并上传结构化扫描结果和证据。
- 微信备注中包含有效客户短码时，服务端能绑定唯一 lead/conversation/sales/worker。
- 只有有效短码且顶部标题明确为 private 的会话可读取；group/unknown 不搜索、不读取、不转写、不入库。
- read-targets 为空时第一屏扫描可以继续，但本地命中队列被清空，消息新增为 0。
- 读取和入库必须携带当前 authorization_revision；停止或重授权后的旧请求返回 409。
- 未绑定或绑定冲突的会话不触发 AI 回复。
- Worker 能读取已绑定会话的客户文字消息并上报服务端。
- Worker 先执行 OmniAuto `messages` 读取/探测；发现未转写语音时再执行 `voice-transcribe`，客户语音成功转写后按 `message_type=voice` 入库，并保留 `raw_payload.voice_transcription` 证据。
- 新 C2 UI 流程进入扫描循环前必须通过真实 Vision 配置预检；恢复门禁先于能力
  预检。具有可信角色和稳定身份的当前屏 NEW_IMAGE 必须在同一 Flow 内结束为
  completed/failed，不允许图片 pending/deferred；初次角色不可信走帧级身份门禁，
  不写 ignored Ledger。
- 图片动作前重建最终画面后已经出屏的图片本轮不处理；仍在屏幕但无法唯一确认时形成 failed 事实，不允许反复右键或跨轮 Vision。
- 语音只识别到时长、转写失败、目标客户未确认、UI 锁超时或微信窗口不可控时，不得触发 C3 自动回复，不得把语音时长当作客户正文。
- 同一消息在 Worker 重启、断网恢复、重复扫描时不会重复入库和重复触发后续动作。
- 会话绑定失败、消息读取失败、微信窗口不可控、Sidecar 超时均有错误码、trace_id 和可查看证据。
- 后端、Worker/RPA、前端均按本模块接口字段、状态枚举和错误码实现，不允许各自新增同义状态。

### 6.6 销售人工回复检测

```text
Worker发送AI回复前登记reply_action_id、reply_text_hash、send_started_at、send_finished_at。
桌面端同步出我方消息时在 V3 合同中统一标记为 `self`；旧 `sales / sales_candidate / ai_worker` 只作兼容输入，不作为新事件正式值。
能与本机已确认发送回执的稳定消息身份严格对应时，标记sender_source=ai，不作为人工销售回复。
不能对应AI回执的self消息只能先认定为“人工销售候选事实”；是否解除现有handoff，必须证明该消息发生在本轮handoff之后。
解除成功后Conversation状态转sales_replied_waiting_user，取消当前AI回复动作；ai_enabled只作为明确关闭全部自动化的硬开关，不因一条销售回复自动关闭。普通实时AI回复由会话状态门禁阻断，召回到期后仍可进入recall_precheck。
```

`occurred_at` 是可选的微信侧推断时间，当前 OmniAuto OCR 不能保证每条消息都有可靠时间，因此不能把 `occurred_at` 作为解除人工接管的必填条件，也不能把本次扫描时间伪装成消息发送时间。

人工接管顺序证明按以下优先级执行：

1. **最终画面相对顺序**：当前 `self` 稳定消息身份位于触发本轮 handoff 的客户消息身份下方，可以证明是后续销售回复。同一最终画面可以比较时，该证据优先于时间字段；销售位于触发消息上方时不得解除，即使时间看起来较新。
2. **可靠微信消息时间**：只有最终画面无法比较时，来源明确且置信度满足合同要求的微信消息时间才可辅助证明；普通 `occurred_at`、扫描完成时间、入库时间不能代替。
3. **无法证明**：销售消息事实照常入库，但保持 handoff 和 `waiting_sales_reply`，不得因为“本轮刚扫描到”就解除。

必须覆盖以下回归：

- 历史销售消息位于 handoff 触发消息上方，即使本轮首次入库也不能解除 handoff。
- 新销售消息位于 handoff 触发消息下方，即使 `occurred_at=null` 也应解除 handoff。
- 触发消息已不可见且没有可靠消息时间时保持 handoff，等待后续可证明画面或人工处理。
- 同一最终画面为“客户 -> 销售 -> 客户”时，销售解除旧 handoff，销售下方的新客户消息进入新的 Brain batch。
- 历史消息延迟补录时，`last_inbound_at / last_outbound_at /
  last_sales_reply_at` 只能更新为更晚时间，不能覆盖成更早值。

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
| sending 状态 | 在物理点击发送前，即使已经输入程序草稿，只要输入前/点击前复核发现 A2 或消息序列变化，就清理可证明属于本次程序的草稿、停止发送并将旧 action 置为 superseded；物理点击已经发生后不得自动重发或强行回滚，等待 `sent_ack / failed_ack / unknown_send_result` 收口，A2 进入下一轮 batch。 |
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
| Worker发送后 | `claim-send` 成功后服务端进入 `sending`，其含义是“发送权已发出，物理发送可能发生”，在可靠终态前禁止自动补发。物理点击发送前 Worker 持久化 `possible_ai_send + action_phase=not_attempted`；触发发送时立即推进为 `trigger_attempted`。微信确认新增右侧气泡后推进为 `confirmed`，先将稳定气泡凭证和 `sent_ack` 写入本地可靠 Outbox，再请求后端确认。触发后无法确认必须回执 `unknown`，不得回执普通 `failed`。后端未确认 `sent_ack` 时，整个 C2 停止扫描和其他会话动作；可靠落盘后可在 `finally` 释放 UI 锁，但只能查询/重传原回执，不能补发消息。下一轮匹配气泡标记为 `ai_unreconciled`，不得按人工销售处理或关闭 handoff。 |
| 恢复扫描 | `sending` 超时后 Worker 先执行仅限原会话的自动对账；仍无法确认时上报 `unknown`，后端持久化 `unknown_send_result` 并结束原回复动作，不自动补发。 |

### 6.10 验收

- 可绑定微信会话；未绑定会话不自动回复。
- 客户文字、语音和图片理解结果可按 V3 合同入库；图片原始字节只在 Worker 当前进程内存中短暂存在，不上传车金后端、不落本地文件。
- Worker 发送的 AI 消息不会误判为销售人工回复。
- 销售手机端人工回复后 AI 停止。
- 同一客户短时间多条消息可合并为一个 `message_batch`。
- 生成中但未发送的旧 `reply_action` 在新消息到来后会被 `superseded/cancelled`，不会被 Worker 发送。
- `reply_action` 从 `queued` 到 `sending` 再到 `sent_ack` 必须有服务端原子状态流转。
- 重启/断网恢复后不重复发送同一 `reply_action_id`。

## 7. 模块6：AI对话模块

- 目标：根据客户消息、上下文、正式知识库、Product Master 车辆事实和规则生成候选回复或接管建议。
- 文本模型 Provider 由服务端配置；模型调用、RAG、Product Master 检索、Guard 均在服务端。
- OmniAuto 不是只作为 RPA 使用；C3 AI文字回复阶段必须复用 OmniAuto AI Engine 的 `customer_service_brain`、RAG、Evidence Pack、Guard、回复生成/润色等能力，但运行位置在服务端。
- OmniAuto 的 RPA Sidecar 只负责后续微信发送动作。两者在工程上必须分层：服务端决定回复内容和状态，Worker/Sidecar 只执行已批准动作。
- Dify/FastGPT Adapter 第一期只预留不实现，不接管主状态。
- OmniAuto 现有 `KnowledgeRuntime`/RAG 和 PostgreSQL 存储能力直接复用；许聪本机数据、仓库测试车辆和示例知识不属于生产交付。真实车辆由车金运营录入，正式知识由产品/业务确认后导入。
- 模型失败直接转人工，不使用兜底话术继续自动回复。
- AI 只输出候选回复和动作建议，不拥有最终发送权。
- AI文字回复属于OmniAuto接入C3 checkpoint。C3 自动回复此前已实机测试且当前
  无已知问题；双仓统一不得另行扩展自动发送流程。正式链路固定为
  `事实 -> Brain -> reply_action -> 发送 -> sent_ack`，任一 C2 准入或媒体门禁
  失败时不得进入后续发送。

### 7.0 服务端AI大脑内部职责拆分

第一期不把 AI 大脑拆成多个微服务，仍部署在同一个后端服务内；但代码和接口必须按职责拆模块，避免把知识库、模型调用、风控和发送动作写成一坨。

| 职责模块 | 运行位置 | 说明 |
|---|---|---|
| 会话上下文构建 | 服务端 | 汇总客户最近消息、销售状态、会话状态、历史 AI/销售回复，形成本轮模型输入。 |
| 知识库管理 | 服务端 | 复用 OmniAuto KnowledgeRuntime，管理正式知识、车辆专属知识、资料来源、更新时间和负责人。首期由管理员审核导入，不建独立知识规则页。 |
| RAG检索 | 服务端 | 基于 OmniAuto RAG 能力，从正式知识和车辆专属知识中召回相关证据；RAG/历史话术只辅助理解和表达，不得授权价格、库存或政策事实。 |
| 车辆主数据 | 服务端 | 复用 OmniAuto ProductMasterStore 和 customer-safe projection；价格、库存、车型、里程和在售状态只能来自 Product Master。 |
| Evidence Pack | 服务端 | 把模型可见证据统一打包，过滤底价、采购价、手机号、内部备注等敏感字段。 |
| AI编排器 | 服务端 | 负责调用 OmniAuto Brain、配置的文本模型、Product Master、RAG、Guard 的顺序和重试策略。 |
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
| `handoff_event` | 服务端 | 记录转人工原因、触发消息、通知结果 | 转人工后进入 `waiting_sales_reply`，由状态门禁阻断普通实时回复，不自动关闭 `ai_enabled` 硬开关，不创建当前批次的发送任务。 |

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
5. `sending` 超时后自动对账；仍无法确认时由后端持久化 `unknown_send_result` 并结束原回复动作，不自动重放。
6. 客户新消息到来时，未发送的旧 `reply_action` 必须 `superseded/cancelled`，重新基于最新 batch 生成。
7. 销售人工回复、客户拒绝、短码移除、会话关闭后，所有未发送 `reply_action` 和 `chat_reply task` 必须取消。
8. `sent_ack=failed` 只允许用于 `action_phase=not_attempted` 或有明确证据证明发送没有触发；`action_phase=trigger_attempted` 且未确认时只能回执 `unknown`。

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

车金 Adapter 内部投影如下；它不是第二套模型输出合同，原始语义必须来自 OmniAuto `customer_service_brain.brain_plan`，唯一字段映射见 C2-C3 接口合同：

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
| `handoff` | 创建 `handoff_event`，状态进入 `waiting_sales_reply`，不自动关闭 `ai_enabled` 硬开关，不创建发送任务。 |
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
| `AI_ENGINE_TIMEOUT` | AI Engine 或当前文本模型 Provider 超时 | 可短重试；超过次数转人工。 |
| `AI_ENGINE_CONTRACT_INVALID` | AI 输出不是合法结构化 JSON 或缺必要字段 | 转人工。 |
| `RAG_NO_EVIDENCE` | 无足够知识/车源证据支撑回复 | 转人工或 no_action，不编造。 |
| `GUARD_REWRITE_FAILED` | Guard 要求改写但改写失败 | 转人工。 |
| `GUARD_BLOCKED` | Guard 阻断发送 | 转人工。 |
| `REPLY_ACTION_EXPIRED` | 回复动作已过期 | Worker 不发送，任务取消。 |
| `REPLY_ACTION_SUPERSEDED` | 回复动作已被新消息取代 | Worker 不发送，任务取消。 |
| `REPLY_ACTION_CLAIM_CONFLICT` | 多 Worker 或重复请求抢同一 action | 只有一个成功，其余拒绝。 |
| `CHAT_REPLY_TASK_DUPLICATED` | 同一 `reply_action_id` 重复创建任务 | 返回已有任务。 |
| `SEND_ACK_DUPLICATED` | 同一 action 重复回执 | 返回已有 ack，不重复更新状态。 |
| `SEND_TEXT_HASH_MISMATCH` | Worker 回传文本 hash 与服务端批准文本不一致 | 发送未触发时持久化 `failed`；发送可能已触发时持久化 `unknown`，均禁止补发并记录 warning。 |
| `SEND_RESULT_UNKNOWN` | Worker 断网/崩溃/超时，无法确认是否发出 | 后端确认 `unknown_send_result` 正式终态，禁止自动补发。 |
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
- Brain 技术失败必须进入 `failed/retry_later`，不得伪装成 `no_action`；RAG 证据不足或 Guard 阻断时可转人工或形成合法 `no_action`，不得编造回复。
- 等待销售回复、客户拒绝、短码移除或会话关闭时，不创建普通实时 AI 发送任务；销售已回复只进入 `sales_replied_waiting_user`，客户再次回复时交回 AI，客户长期未回复时仍可进入 Brain 召回判断。
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
| OmniAuto接入 | 复用最新固定提交中的 ProductMasterStore、KnowledgeRuntime、RAG、customer-safe projection 和 Guard；不另建平行车辆库/知识库。 |
| 知识库标准 | 知识条目需有标题、适用场景、正文、禁说内容、更新时间、负责人；过期内容不得进入正式索引。 |
| 检索方式 | 采用语义检索+关键词加权；事故、泡水、火烧、底价、贷款、合同等风险词必须被关键词层召回。 |
| 低置信处理 | 知识不足、检索冲突、车辆证据不足、模型不确定或数据库不可用时，明确说无法确认或转人工，不编造。 |
| 优化目标 | RAG命中率、误召回率、转人工率作为灰度期优化指标，不作为未经样本集验证的硬承诺。 |

## 8. 模块7：图片理解与图文回复

本模块已完成开发和 Windows 实机验收，并进入当前可回滚基线。后续源码统一或相关代码变更只做受影响回归，不重开图片流程设计；任何新 Provider 或微信版本仍须按本章门禁验证后再发布。

本章是图片流程的唯一现行技术口径。图片状态矩阵、内存/剪贴板、真实 Provider、
跨轮上下文、产品权威边界和 UAT 门禁均以本章为准；已归档的历史审计材料不得
覆盖本章，也不得作为开发或验收入口。

### 8.1 唯一职责边界

- 图片与文字、语音属于同一个 C2 单会话 Flow，不另建图片扫描任务、图片上传接口或平行准入链路。
- 图片复用有效短码、`conversation_type=private`、`read-targets` 和 `authorization_revision` 门禁。
- OmniAuto 负责发现 `image_bubble`、返回 `bubble_rect`、执行当前剪贴板图片事务和 Vision 文字化理解。
- 图片、文字、语音只使用一套 C2 `sender_role` 规则：同行左头像为 `customer`，同行右头像为 `self`；两侧同时成立或都不成立为 `unknown`。Vision 的 `side / visual_side` 只能作诊断证据，不得参与角色定案。
- Worker 负责最终画面统一槽位、`screen_order`、跨轮 `source_message_key/dedupe_key`、本地 ledger、Outbox 和 V3 映射。
- 后端负责授权、消息事实持久化、数据库最终去重、服务端权威车源匹配、跨轮图片上下文、`message_batch`、状态机、Brain/Guard、handoff 和 `chat_reply`。
- Vision 只理解图片，不生成客户可见回复；唯一回复作者是服务端 `customer_service_brain`。
- 会话内不存在图片 `pending/deferred`。全局能力未就绪在进入 C2 前阻断；具有可信角色和稳定身份的当前屏 `NEW_IMAGE` 必须在同一 Flow 内结束为 `completed/failed`。`ignored` 只允许在建立业务图片身份前表示已经明确证明不是聊天图片消息。
- 车金正式产品 ID 和车辆事实只由服务端 Product Master 确认；RAG 和 Vision 只能形成检索线索，不能独立认定价格、库存或车型。服务端允许通过 OmniAuto `KnowledgeRuntime` 读取已过 customer-safe projection 的 Product Master 证据；Worker/客户端本地知识不得成为正式事实源。

### 8.2 单会话图片处理流程

```text
语音处理完成后的最终有效画面
-> 同时建立文字、语音、图片slots并按画面自上而下生成screen_order
-> 为全部slot生成稳定source_message_key
-> 查询Worker本地ledger和Outbox，判定NEW / OLD_COMPLETED / OLD_FAILED / OUTBOX_WAITING / IDENTITY_CONFLICT
-> 初次图片角色不可信时形成帧级MESSAGE_IDENTITY_UNCONFIRMED，不建立图片消息、不写ignored Ledger
-> OLD图片不复制、不调用Vision；OUTBOX图片只重传原JSON
-> 只把NEW_IMAGE加入图片增强队列
-> 再次确认图片bubble_rect、同行头像角色、当前短码、private和authorization_revision
-> 刷新后的同行头像角色必须与初始C2角色一致；另一角色或unknown时零点击并failed
-> 页面已经变化时先重建完整final_read；图片已出屏则从本轮候选删除，仍可见但无法唯一匹配则failed
-> 右键图片，OCR确认菜单，点击复制
-> 从Windows剪贴板把位图读入当前进程内存
-> 按原始位图内存上限解码，再缩放和自适应编码到Provider载荷上限
-> 校验image_hash/视觉指纹后清除本次Windows剪贴板内容
-> Worker进程内调用OmniAuto BuiltinVisionPlugin和真实模型
-> 使用共享JSON Schema校验结果，得到completed / failed明确终态
-> 释放该图片内存
-> 把文字化结果回填原图片slot，不在批次末尾另行追加
-> 按最终screen_order与本轮新文字、语音共同调用现有messages/ingest
```

图片不能先于统一槽位和新老判定调用 Vision。否则旧图片会被重复右键、重复计费，或在后续发现历史断层时白做一次高成本操作。

### 8.3 图像生命周期与接口

- 不恢复旧 Sidecar `image-save / image-clipboard-copy` 动作，不存在独立 `save_image` 任务。
- 不使用 `image_local_path`、截图裁切文件、历史图片文件、Base64 入库或 `/images/upload`。
- 原图只在 Worker 当前进程内存中短暂存在，单张 Vision 完成后立即释放；不得写入 ledger、Outbox、日志、截图证据或后端。
- Windows 原始位图的安全内存上限与 Provider 最终 3 MB 载荷上限必须分开；不得在缩放压缩前用 3 MB 拒绝常见 1080p DIB/HBITMAP。
- 图片读入内存并完成目标指纹校验后，必须清除本次复制产生的系统剪贴板内容；生产测试机关闭剪贴板历史和跨设备同步。
- 允许持久化的只有文本白名单投影 `customer_image_understanding`、`visual_bridge_input` 和不含原图内容的事务审计。
- 图片继续复用 `POST /api/workers/{worker_id}/wechat/messages/ingest`；不得新增 `image_recognition` 同义字段或图片专用后端接口。
- 真实 Provider 配置必须在新的 C2 UI 流程启动前完成预检。API Key、模型或地址
  缺失时 Worker 进入 `vision_not_ready`，不得首屏扫描、定向读取或打开会话；
  配置恢复并重新预检通过后才能启动新 C2。已有 `sent_ack`、消息 Outbox 和
  `settle_without_ui` 必须先恢复，不受该能力门禁影响。不得用 mock 冒充真实能力完成。
- Provider 地址必须为 HTTPS（显式本地开发模式除外），请求风格使用白名单。一次
  非 JSON 格式纠正重试属于合法流程，父进程安全预算必须覆盖两次请求，不能用
  `单次timeout + 5秒` 提前杀死第二次请求。

### 8.4 成功、失败与重试口径

| 场景 | 处理 |
|---|---|
| 新图片识别成功 | `message_type=image + item_state=completed`，保存文字白名单结果并按原槽位顺序入库。 |
| 单张图片技术失败 | `message_type=image + item_state=failed + content=null + error_code/reason` 入库；不得伪装为文字，不自动重复 Vision。 |
| 图片在动作前已被顶出最终当前屏 | 重建 final_read 后本轮不建立该图片槽位，不上滚、不追踪、不产生失败事实或 Brain 门禁；后续自然可见时重新观察。 |
| 图片仍在最终当前屏但无法唯一确认原稳定身份 | `message_type=image + item_state=failed + content=null + error_code=C2_IMAGE_SLOT_RECONFIRM_FAILED` 入库；不右键、不调用 Vision、不跨轮重试。 |
| 初次图片观察无法确认同行头像角色 | 尚未建立业务图片身份；返回帧级 `MESSAGE_IDENTITY_UNCONFIRMED`，零点击、零 Vision、零 terminal ledger，并阻断本批 Brain。 |
| customer/self 图片失败 | 同批已确认文字和语音继续入库；按与 failed 语音相同的角色和最终画面顺序门禁处理，未被可靠后续销售事实覆盖时不得进入 Brain。 |
| Vision 配置缺失 | 新 C2 UI 流程启动前 `vision_not_ready`；不得开始扫描或打开会话，但不阻断已有回执、Outbox 和无 UI 事实结算。该状态不是图片消息状态，也不使用后端 `capability_paused`。 |
| 同屏语音失败 | 失败语音阻断 Brain，但不阻止身份可靠的新图片执行复制和 Vision；历史断层或消息身份冲突才允许阻止图片物理操作。 |
| 网络或后端未确认 | 完整 JSON 进入 Outbox；下轮只重传，不重复图片 RPA 或 Vision。 |
| 后端返回 duplicated | 不新增数据库记录，但用服务端原样返回的 `source_message_key` 确认本地 ledger，避免下轮重复处理。 |
| 图片身份不确定 | 不复制、不调用 Vision、不触发 Brain；保存门禁错误和证据。 |
| 图片检测成功且数量为 0 | 按当前画面没有图片正常继续。 |
| 图片检测器或物理锚点生成异常 | 返回 `C2_IMAGE_OBSERVATION_FAILED` 结构化合同错误；同屏文字可保留在本地事实，但本批不得入库后触发 Brain，禁止伪装成零图片。 |
| 图片候选超过内部处理容量 | 不得静默只返回前 8 张；全部观察，或返回 `C2_IMAGE_OBSERVATION_FAILED/observation_truncated=true` 并阻断 Brain。 |
| Vision 返回字段类型、范围或结构非法 | `failed`；字符串 `"false"`、NaN、越界置信度或仅靠默认空字段均不得成为 completed。 |

图片稳定身份以 `canonical_visual_id` 优先，必要时使用角色、同类出现序号和邻近稳定消息锚点降级。`image_hash` 只有复制后才能得到，只能增强证据，不能作为复制前唯一去重条件；画面坐标、扫描编号、读取编号和扫描时间均不得成为消息身份。

图片的 `customer/self` 只由 C2 同行头像规则确定。图片事务重新截图时得到的几何
左右侧只能用于记录 `visual_side_consistent` 物理证据，不得覆盖或否决 C2 角色。
但重新截图后仍必须再次执行同一套 C2 同行头像规则，并将结果与初始 C2
角色比较；两次正式角色不一致或刷新结果为 `unknown` 时不得右键。
图片阶段必须通过一个统一结果对象表达：

```text
本轮真实执行的新图片动作
已经形成终态的图片
当前屏仍未收口的新图片
是否必须执行图片后最终刷新
是否必须阻断 Brain
```

后续流程不得再通过 `completed/failed/cached` 计数关系自行推导这些结论。

后端给 Brain 的历史图片必须保留紧凑的 `item_state/error_code`、摘要、图片 OCR、
分类、实体、中性查询和服务端确认产品 ID；不能只保留
`message_type=image + content`。当前轮与历史轮使用同一个投影函数，保证下一轮
“这辆多少钱/刚才那台”仍有图片上下文。

### 8.5 当前基线与双仓统一结果

正式 Windows 验收回滚基线固定为：

```text
车金分支：codex/c2-omniauto-2318bd8-integration
车金提交：8ee53e1c8d1ee94ba40a1e008436aa7bb106c095
客户端版本：16.130.0
已验收安装包SHA256：4c62183370e1915a463e5771a52377a05753ac41a61a43cc6e48fc9832e44179
机器合同revision：3.12.4
OmniAuto共同基础：meta-xucong/omniauto@855c218
图片一致性能力选择性来源：meta-xucong/omniauto@2318bd8
首次集成提交：ff9e0de
当前内嵌OmniAuto目录SHA256：e1fea61c6d4c0c5516f499e9767178317cd4fabf78f01a60abf6aa80d47e2dce
```

这里的 `2318bd8` 是回滚包中图片气泡和复制后一致性能力的历史选择性来源，不表示
当前 `worker-client/omniauto-rpa` 仍停留在该提交。当前双仓统一结果为：

```text
OmniAuto 通用上游固定提交：69dc871e8649773a921e8e50123116687a710bb5
车金 main 固定提交：37139bfd5663d1f06fd4ddc151624e6bdda1a8e4
客户端版本：16.132.0
活动 selective_integrations：[]
历史来源：855c218 + 2318bd8 + ff9e0de（只读追溯）
```

当前主链完整复用：

```text
图片气泡观察
-> 动作前重新定位
-> 右键并OCR确认局部“复制”
-> 点击复制
-> 校验剪贴板新代次并读取内存位图
-> 图片指纹辅助检查
-> 内存Vision
-> 返回completed/failed
```

`claim_copy_ownership/微信窗口PID` 硬门禁已经撤销。后续双仓统一不得恢复该门禁，
也不得借机改写 OmniAuto 的图片检测、重新定位、复制、指纹辅助、内存图片、
Vision 或结果返回主链。

下列整改已完成，并作为双仓统一的必保合同：

1. 在 `capture/transaction.py` 删除 `claim_copy_ownership` 调用和
   `test_port_bitmap_proof` 兜底，恢复为：

   ```text
   sequence变化
   -> 读取当前bitmap
   -> bitmap可解码
   -> 二次读取sequence仍等于本次candidate_sequence
   -> 接受同一份内存payload
   ```

2. 从 OmniAuto `ClipboardPort` 删除 `claim_copy_ownership` 方法。
3. 从 Worker `_Clipboard` 适配器删除 `claim_copy_ownership`。
4. 从 `clipboard_payload.py` 删除
   `windows_clipboard_image_ownership_evidence`。
5. 从机器合同删除只服务于上述误加门禁的失败原因：
   `clipboard_copy_ownership_unconfirmed`、
   `clipboard_owner_api_unavailable`、
   `clipboard_owner_window_missing`、
   `clipboard_owner_not_wechat_image`、
   `clipboard_owner_check_failed`；升级合同 revision 并重新生成 OmniAuto
   observation schema。
6. 把实际会产生但尚未映射的
   `vision_host_ports_incomplete`、
   `vision_window_context_capture_missing`、
   `vision_window_capture_failed`
   精确归入 `C2_IMAGE_OBSERVATION_FAILED`，并增加错误原因映射完整性测试。
   完整性测试必须证明运行时可产生的图片失败原因全部显式映射，不得用
   `default_failure_error_code` 掩盖漏配。

`v16.130.0` 的历史发布来源元数据按下列方式保留；当前 schema v3 已把活动
`upstream_base_commit` 更新为 `69dc871`，活动 `selective_integrations` 置空，
旧三段来源迁入只读 `historical_integrations`。来源 schema、打包脚本和测试已同次
升级，后续不得伪造目录来源：

```text
upstream_base_commit = 855c218...
selective_integrations[].source_commit = 2318bd8...
selective_integrations[].scope = 图片气泡/视觉指纹/复制一致性重试
chejin_integration_commit = ff9e0de...
```

打包 manifest 已记录且后续继续必须记录 Worker Git commit、branch、`git_dirty=false`、
OmniAuto 基础提交、选择性来源提交、完整目录 tree SHA256、包内 tree SHA256、
合同 revision/SHA、生成 observation schema SHA 和自动化/preflight 结果。
tree SHA 必须由最终提交后的真实目录动态计算，不得把历史 tree SHA 手工写成当前值。

#### 8.5.1 双仓最终收口结果

1. OmniAuto 通用改动已通过上游 PR 合并到固定提交 `69dc871`。
2. 车金内嵌 `worker-client/omniauto-rpa` 已更新到该固定提交，车金业务适配仍留在
   Worker/后端边界，没有进入通用上游。
3. `.chejin-source.json` 已使用 schema v3：活动
   `upstream_base_commit=69dc871`、`selective_integrations=[]`，旧
   `855c218 + 2318bd8 + ff9e0de` 保留在 `historical_integrations`。
4. 车金 PR 已合并到 `main@37139bfd`，客户端版本为 `16.132.0`。
5. 自动化和受影响范围 Windows 回归已通过。正式回滚仍使用
   `8ee53e1 / v16.130.0`，不得在旧安装包上继续打补丁。

### 8.6 证据层级与不得回退项

图片业务身份只由 Worker/C2 的最终画面统一 slot、同行头像 `sender_role`、
`canonical_visual_id` 或“角色 + 同类出现序号 + 邻近稳定消息锚点”以及
`source_message_key` 决定。只有 `NEW_IMAGE` 才进入 OmniAuto；
`OLD/OUTBOX` 不得重复执行图片动作。

OmniAuto 当前事务只负责证明：

```text
动作前原slot仍在当前屏
-> 记录sequence_before
-> 右键并点击局部“复制”
-> sequence_after != sequence_before
-> 当前图片位图可解码
-> 读取前后sequence_after稳定
```

保留现有图片指纹作为复制一致性辅助检查。指纹不生成消息身份、不判断角色；
指纹匹配不能单独授权点击、Vision 或覆盖 Worker 身份。首次不匹配沿用现有同 Flow
最多一次重新确认/复制；再次不匹配后明确 failed，重启和下一轮不得再执行。

必须保留：

- 当前基线对空 Vision 结果、业务终态和 ActionJournal 一致性的修复。
- 当前基线对 finally 剪贴板清理失败和外部新代次保护的修复。
- OmniAuto `2318bd8` 的图片检测、重新定位、局部菜单、指纹辅助和同 Flow
  最多一次重新确认。
- 原始位图与 Provider 载荷大小分离、内存压缩、真实 Vision 和共享 schema。
- 既有短码 + private准入、统一 sender_role、文字、语音、最终顺序、当前屏不主动
  上滚、单会话UI锁、Outbox、授权、停止、Brain回复和召回流程。

不得趁本次修改重构其他模块、增加新接口、恢复旧图片入口或调整 C1/C2/C3
业务状态。

### 8.7 自动化、Windows UAT 与后续回归

`v16.130.0 / 8ee53e1` 已通过以下自动化门禁；双仓统一后的
`v16.132.0 / 37139bfd` 已重新执行并通过受影响范围回归。后续车辆信息版本仍必须
一次性通过：

1. 正式 ClipboardPort 和测试 Fake 均不再包含/依赖
   `claim_copy_ownership`。
2. sequence不变时不读取旧图片、不调用Vision。
3. sequence变化、bitmap有效且读取期间稳定时进入现有指纹/Vision链。
4. 读取期间sequence再次变化时失败并释放候选图片。
5. 指纹首次不匹配最多同Flow重试一次，重启不重复。
6. 空Vision摘要不得成为completed。
7. finally清理失败不得被吞掉；外部新代次不得被清除。
8. 所有实际图片失败原因都有机器合同精确映射。
9. `python3 run_checks.py`、后端C2/C3回归、合同生成、Python编译和
   `git diff --check` 全部通过。
10. 来源元数据能同时表达 `855c218` 基础和 `2318bd8` 选择性集成，打包脚本及
    manifest 测试覆盖新增字段。

当前 C2 Windows 实机验收结果见
`C2_Windows实机验收报告_2026-08-03.md`，结论为通过，P0/P1 均为 0。C3 自动回复
沿用此前实机通过且当前无已知问题的证据，不混写成此次 C2 报告的新测试。

后续构建必须先形成唯一 Git 提交并确认工作区干净，再由该提交构建
Windows UAT 包；不得先用 dirty 包测试、UAT 后再修改来源文件重打同版本包。
候选包、manifest 和测试结果必须绑定同一个 Git commit 和 ZIP SHA256。
车辆信息或 OmniAuto 后续变更如果改变运行代码，则自动化后至少做受影响范围的 Windows 冒烟；若
图片事务、消息身份、授权、发送或恢复合同发生实质变化，才重新执行完整 UAT。
完整实机回归门禁至少包括：

- 客户单图、我方单图、连续两张相同图片。
- 白底长截图、车辆照片、含文字车辆图和普通大图。
- 文字 + 语音 + 图片混合及最终入库顺序。
- Vision期间新增消息，原图片完成后继续读取最终当前屏。
- 旧图片、Outbox图片、动作前被顶出屏图片均不重复处理。
- Vision成功、超时、鉴权失败、非JSON纠正和schema非法。
- 停止授权、崩溃恢复和ingest响应丢失不重复执行图片动作。
- Windows 剪贴板 owner 为空、隐藏窗口或非微信主窗口PID时，只要会话、slot、
  sequence、可解码位图、稳定读取和指纹链成立，正常图片不得被额外PID规则拒绝。
- 图片读取期间其他程序或用户复制新图片时，错图必须被指纹/sequence门禁拒绝；
  用户后来产生的新剪贴板代次不得被清除。

UAT 责任固定为：架构师维护方案和判定门禁，客户端工程师按本节实现，后端工程师
同步机器合同并回归，用户与测试人员在真实 Windows 微信执行用例并提供证据，
架构师依据证据给出通过或退回结论。已确定使用豆包 Vision，不再把“是否使用
第三方 Vision”或“谁提供 Windows 环境”列为待管理决策。

### 8.8 后续变更控制

以后只有以下确定问题可以阻断图片 UAT：

- 操作错误会话，或把旧图片当新图片重复处理。
- 图片角色、消息身份或最终顺序错误。
- 同一图片动作跨轮重复执行。
- 原图进入文件、日志、后端或安装包。
- 图片终态丢失或错误触发Brain。
- 修改导致文字、语音、回复、召回或停止流程回归。
- OmniAuto 通用能力再次只存在车金内嵌副本、来源记录与真实代码不一致，或引用
  浮动分支而不是固定提交。

其他性能参数、诊断字段、感知指纹阈值和没有真实复现的算法推测进入P1/P2，
不再阻断打包。

## 9. 模块8：OmniAuto 本地车辆库与知识库

本项目不再接入大风车 API。车辆和知识能力复用 OmniAuto 最新主线中的
Product Master、KnowledgeRuntime、RAG 与 Guard，但车金后台仍是唯一运营入口，
不得再部署一套 OmniAuto 管理后台。

### 9.1 唯一实施口径

- Product Master 的本地手工 V2 数据是车辆事实源；包名或类名中若仍含历史命名，
  只代表代码沿革，不代表仍需大风车接口、签名、拉取或回写。
- 真实车辆由运营通过车金后台录入或 Excel 导入。许聪本地样本和自动化测试数据
  仅用于开发验证，不得迁入生产。
- 正式知识通过 KnowledgeRuntime/RAG 使用，内容必须经过业务审核。第一期不新增
  独立“知识规则”页面，先由管理员按受控流程导入。
- 不复制实现另一套 Product Master、RAG 或 Guard；车金只增加适配层、鉴权、审计
  和运营页面。

### 9.2 数据与存储边界

| 数据类型 | 唯一权威来源 | AI 使用边界 |
|---|---|---|
| 车辆主数据 | Product Master | 只读对客白名单字段；库存、状态、对外价格等事实不得由 RAG 覆盖。 |
| 单车知识 | Product Master 车辆条目 | 只能补充该车辆的已审核说明，不能改写车辆事实。 |
| 正式业务知识 | KnowledgeRuntime | 只使用已审核、已启用内容；命中不足时不编造。 |
| RAG 经验与表达 | RAG/Guard | 用于检索和表达约束，不作为车辆库存、价格或政策事实源。 |

- 继续使用车金现有 PostgreSQL，同一数据库实例内新建
  `wechat_ai_customer_service` schema；现有业务表继续留在 `public`。
- PostgreSQL 是生产唯一真相源。JSON 只允许用于本地开发、导入导出和一次性迁移，
  不得长期双写。切换前必须完成全量迁移、条数校验和可恢复备份。
- 车辆图片使用持久化文件卷或对象存储，并在数据库保存引用；客户微信原图仍按现有
  隐私边界只在客户端内存处理，不落盘、不上传后端。

### 9.3 后台与接口边界

第一期后台只增加一个“车辆管理”入口：车辆列表与搜索、上下架、添加/编辑、图片
维护，以及 Excel 模板下载、预览校验和确认导入。暂不新增知识规则、AI 工作台、
转人工等独立页面。

浏览器只调用车金后端 API。车金后端负责登录鉴权、角色权限、输入校验、审计日志，
再调用 OmniAuto 能力；前端不得直接读写 Product Master、KnowledgeRuntime 或数据库。

### 9.4 Brain 查询流程

客户询问“某车有没有”时，Brain 先从 Product Master 查询在售车辆，再按需读取正式
知识并交给 Guard 校验后生成答复：

- 唯一明确命中：可以回答白名单内的真实车辆信息。
- 多车或条件不完整：先追问必要条件，不擅自选车。
- 无匹配：明确说暂未查到，并可转人工；不得根据模型常识编造库存。
- 数据库不可用或结果不可信：停止自动回答车辆事实，记录证据并转人工，不把
  “查询失败”说成“没有车”。

### 9.5 安全、权限与审计

- 运营可维护车辆；普通销售默认只读；敏感字段默认不进入 AI 可见投影。
- 数据库凭据、模型密钥和存储凭据只保存在服务端，不下发 Worker 或浏览器。
- 新增、编辑、上下架、批量导入、图片变更均记录操作人、时间、对象、结果和失败原因。
- 采购价、底价、车主隐私、内部备注等字段默认禁止对客回答；需要开放时必须由
  管理层明确字段和角色权限。

### 9.6 发布门槛与回滚

- 以固定提交接入 OmniAuto 最新主线的完整相关模块，不允许只拷贝部分文件。
- Product Master 和 KnowledgeRuntime 自动化必须全绿；当前样本与测试预期不一致的
  用例必须先修正，不能跳过或改成无效断言。
- 生产包不含测试车辆、测试知识、临时数据库和开发密钥。
- 数据切换前完成全量迁移、校验和备份；用 3—5 辆真实车辆验证新增、修改、上下架、
  Excel 导入、图片与 Brain 问答闭环。
- 出现问题时关闭车辆问答与管理入口，恢复切换前数据库备份；C0—C4 既有聊天主链
  继续运行，不跟随新车辆能力回滚。

## 10. 模块9：风控策略中心

- 风控属于云端业务控制面的核心子模块，但作为独立业务模块详细设计。
- 风控策略由服务端控制面配置和判定，Worker 执行服务端返回的动作，并展示命中原因。
- 第一期不承诺规避微信平台风控，不做复杂反检测和机器学习风控模型。

| 风控项 | 口径 |
|---|---|
| 自动回复总开关 | 可按全局、销售、Worker、会话控制；关闭后不自动回复和召回。 |
| 人工接管模式 | `waiting_sales_reply / sales_replied_waiting_user` 时停止普通实时AI回复；其中 `sales_replied_waiting_user` 到期后仍允许按召回规则进入 `recall_precheck`。 |
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
| 销售主动回复 | 检测到销售手机端人工消息后直接进入 `sales_replied_waiting_user`，不再创建“等待销售回复”的 handoff。 |
| 手动操作 | 控制面或 Worker 执行台点击停止 AI/手动接管。 |

- 进入 `waiting_sales_reply` 时由会话状态阻断普通实时 AI 回复；只有人工明确关闭全部自动化时才设置 `conversation.ai_enabled=false`。
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

## 12. 模块11：自动召回（`trigger_type=recall` 的 `chat_reply`）

- 目标：对已添加微信、处于等待用户回复类状态、长期未互动且未拒绝的客户做低频再触达。
- 云端先判断召回到期资格，但不得直接创建/发送 `chat_reply`；必须先生成 `recall_precheck` 读取目标。
- Worker 先定向读取该会话，服务端确认没有新客户消息后，才允许创建 `chat_reply` 任务。
- 服务端复用与普通回复相同的 Brain/Guard，以 `trigger_type=recall` 和 `recall_cycle_id` 生成、审核召回内容；不新增召回专用模型。
- Worker 只发送服务端批准的召回文案并上报结果，不在本地生成或改写内容。
- 第一期只做一种召回规则；当前默认连续 72 小时无新消息后进入召回前确认。
- 当前默认单客户最多 3 个召回周期、每日最多 1 次，静默时段为 21:00—次日 09:00；参数由服务端配置，修改默认值必须同步产品口径并回归。
- `watching` 不作为第一期必需主状态；观望客户可用 `waiting_user_reply / recalled_waiting_user` 加规则字段表达。

| 规则 | 说明 |
|---|---|
| 适用客户 | 已加好友、会话已绑定、处于 `waiting_user_reply / recalled_waiting_user / sales_replied_waiting_user`、自动化硬开关未关闭、未拒绝、未关闭、未黑名单、最近N天无客户/销售消息。 |
| 召回前确认 | 到期后先进入 `recall_precheck`，Worker 定向读取该会话；读到新客户消息则取消召回。 |
| 排除条件 | rejected、waiting_sales_reply、closed、黑名单、近期客户/销售已联系、达到召回上限、风控暂停、静默时段。 |
| 生成与发送 | 只有 `recall_precheck` 确认无新客户消息后，才创建 `trigger_type=recall` 批次；Brain/Guard 返回 `send_reply` 后创建 chat_reply，Worker 保持当前会话和 UI 锁发送批准文案。 |
| 防重复 | 同一客户同一规则周期只发送一次，同一 chat_reply 任务只发送一次，重启后已 sent 不再发送。 |

## 13. 模块12：测试、验收与部署

- 核心验收原则：系统能正常回复，或在不能安全回复时触发人工接管。
- 性能目标不作为未经压测的硬承诺，最终以测试环境、账号状态、网络质量、模型服务和真实样本实测为准。
- 测试环境包含云端控制面、数据库、AI 服务、RAG/知识库、Product Master、风控配置、飞书机器人、商家侧 Windows 电脑、微信桌面端、Worker 执行台、销售手机微信和飞书。

| 测试阶段 | 内容 |
|---|---|
| P1基础链路 | 线索接入、销售分配、Worker绑定、add_friend、客户短码写入和邀请结果回传。 |
| P2会话绑定/微信监听 | OmniAuto sessions / messages / 条件性 voice-transcribe、短码 + private 准入、统一消息顺序、语音转写入库、稳定身份、Outbox、数据库最终去重。 |
| P3文字回复 | 客户文字、RAG、真实 OmniAuto Brain、Guard、任务中心 `chat_reply/reply_action`、单会话持锁、pre_send_refresh、输入前/点击前复核、发送审计。 |
| P4图片回复 | 统一消息槽位和新老判定、常见 Windows 原始位图、自适应内存编码与剪贴板清理、真实 OmniAuto Vision、共享结果 schema、V3 成功/失败事实、customer/self 失败门禁、跨轮图片指代、服务端权威车源匹配及 Brain/Guard 使用；必须完成图片封版清单规定的 Windows 真实模型混合消息回归。 |
| P5风控接管 | 总开关、静默、上限、黑名单、关键词、模型失败、飞书通知、销售回复后AI停止。 |
| P6自动召回 | 等待用户回复类状态、N天未联系、召回前 `recall_precheck`、`trigger_type=recall` 的 Brain/Guard 文案生成、上限、跳过原因、防重复。 |
| P7异常恢复 | Worker断网/重启、服务端不可用、模型超时、Outbox 重传、重复消息、reply_action 恢复、发送结果未知和不重复发送。 |

### 13.1 S1阻塞缺陷

- 无法加好友、无法监听消息、无法发送回复、AI无法停止、重复发送同一回复、转人工后仍自动回复、敏感字段泄露。

### 13.2 缺陷分级与外部依赖验收

| 级别 | 定义 | 处理 |
|---|---|---|
| S1阻塞 | 主链路不可用、重复发送、AI停不住、敏感字段泄露、错误接管后继续回复。 | 必须修复后验收。 |
| S2严重 | 部分场景失败但有人工降级，例如图片低置信过多、Product Master 查询失败但车辆问答已安全转人工。 | 需给出修复计划或降级方案。 |
| S3一般 | 体验问题、配置默认值调整、页面展示不完整但不影响主链路。 | 可进入试运行问题清单。 |
| 外部依赖 | 微信版本变化、账号受限、模型服务故障、数据库或图片存储故障、飞书配置不可用。 | 按降级方案和责任边界处理，不直接归为内部开发缺陷。 |

### 13.3 必测用例补充

- A、B、C 同时来消息，A 追加第二条：验证同会话合并、跨会话排队、旧 `reply_action` 作废。
- AI 生成中断网、Worker 重启、服务端重启：验证不会重复发送。
- Worker 进入 `sending` 后异常退出：状态进入 `unknown_send_result`，不自动补发。
- 销售手机端回复后桌面端同步：验证 AI 停止且不再召回。
- 微信出现操作频繁/添加受限提示：验证 Worker 暂停、截图、告警、加好友不继续冲。
- 飞书发送失败：验证 AI 仍停止，`HandoffEvent` 记录失败状态和错误日志，控制面/Worker 执行台可见。
- Product Master 数据库不可用、无匹配或关键字段缺失：验证车辆事实问答安全转人工，不把查询失败说成无车，也不编造车源。

### 13.4 无人值守故障证据

- TaskRunner、C2 Listener 使用统一线程监督入口；线程异常或意外结束时必须先触发进程级紧急停止，再持久化暂停状态和故障证据。进程级紧急停止必须进入任务领取、UI 操作、Sidecar 取消回调和 Vision 取消门禁，触发后本进程不得重新开始接单，只能重启恢复。
- 主线程、其他 Python 线程和 Qt 回调统一安装全局异常捕获；全局异常同样先触发进程级紧急停止。启动时写入运行标记，正常退出时写入结束标记；下次启动发现上次未正常结束时，自动生成恢复故障证据。
- 每条 `ERROR`、真实图片/窗口/Vision 失败、发送结果 `unknown` 和线程崩溃均显式调度唯一 `incident_id`。业务线程只持久化小型取证请求，不同步复制截图或压缩 ZIP；独立后台线程生成本地脱敏故障包。
- 故障指纹必须包含 `thread_kind / origin / reason` 等稳定来源，TaskRunner 与 C2 Listener 崩溃不得合并。完全相同的故障只在 10 分钟窗口内合并；超出窗口或恢复后再次出现必须创建新编号。合并期间每次发生时间和独立脱敏堆栈均追加到同一故障包的 `occurrences/`，不能只保留首次堆栈。追加时必须在临时副本中完成并校验 ZIP，再原子替换正式包；中断时必须保留原 ZIP 和待追加记录。
- 取证目录最多保留 30 天、50 个、2GB，任一上限触发即按时间清理旧包。磁盘空间不足时降级保留小型脱敏 JSON 和堆栈，不得拖死业务线程。
- 发送结果 `unknown` 必须先把本地 Outbox 写成禁止补发终态，再调度异步取证；故障包必须包含该次回复动作的最终 Outbox 和动作日志。
- Vision 子进程意外异常必须返回脱敏堆栈；超时或被强制终止则记录父进程的超时/终止证据，不得只留下异常类型。
- 完整故障包包含客户端版本/提交、前后日志、错误码、关联 ID、允许保留的截图与 review、动作日志、Outbox 状态及完整脱敏堆栈。取证请求入队后，后台线程默认等待 3 秒收尾窗口再封包，用于收集暂停同步、任务释放和 Outbox 收尾日志；该等待不得阻塞业务线程。
- 故障 ZIP 仅保存在 Worker 本机证据目录；UAT 阶段后端只记录本地 `evidence_path`，不自动上传客户截图。
- 故障 ZIP 使用文件白名单和字段脱敏，不得包含原始 SQLite、`.env`、`worker_token`、豆包/Vision API Key、回复 Brain Key 或其他认证秘密；旧 `export_debug_snapshot` 不作为外部证据导出入口。
- 客户端日志页展示 `event / error_code / incident_id / sidecar_run_id / evidence_path`，并提供“导出最近一次故障证据”和“打开证据目录”。
- Windows UAT 必须无人值守模拟：微信窗口丢失、目标图片定位失败、剪贴板一致性失败、豆包超时、后端断网、发送结果不确定、TaskRunner/C2 线程异常退出；每类故障必须产生可直接交付的 ZIP。

## 14. 支撑模块

| 支撑模块 | 第一期口径 |
|---|---|
| 日志审计与数据留痕 | 记录任务、消息、RAG召回、候选回复、Guard、风控、飞书通知、Worker错误、人工操作，敏感字段脱敏。 |
| 配置中心与运维监控 | 集中管理模型、风控、召回、销售/Worker绑定、车辆/知识存储等配置，展示Worker在线和服务健康状态。 |
| 数据安全与权限边界 | 第一期轻量权限；模型Key、数据库/存储凭据、飞书配置不下发Worker；AI只读白名单字段。 |
| Worker兼容性管理 | 记录Windows、微信、Worker版本；每次微信升级前跑核心回归；支持暂停Worker和人工降级。 |
| 异常恢复任务 | 定时扫描 `stale running`、未确认 Outbox 和车辆/知识迁移异常并按安全规则处理；`unknown_send_result` 是已确认终态，只用于防重复与后续气泡自动对账，不生成消息人工待办。 |

## 15. 剩余上线前确认清单

- 发布阻塞项：车辆/知识数据完成 PostgreSQL 全量迁移、校验和备份，生产数据中不含测试样本。
- 发布阻塞项：车辆管理页由产品经理给出最小页面和验收口径，再由前后端实现；不新增第二套后台。
- Gate 0 阻塞项：销售手机端人工回复同步到桌面端后的可读结构，需真实微信环境实测。
- 开发可并行项：线索接入方式 Excel、CSV、手动录入或 API，待确认，先按适配器实现。
- 开发可并行项：线索分配策略手动、轮询或其他规则，待确认，先保留配置位。
- 配置待确认项：好友申请语最终文案。
- 配置待确认项：初始备注命名规则。
- 配置待确认项：每日加好友、AI 回复、召回上限默认值。
- 配置待确认项：随机发送延迟范围和单会话限频默认规则。
- 配置已定项：C4 默认 72 小时、最多 3 个周期、每日 1 次、21:00—09:00 静默；召回文案由 Brain/Guard 生成，不使用固定文案。
- 联调待确认项：飞书机器人定向个人通知的具体实现方式和错误返回格式。
