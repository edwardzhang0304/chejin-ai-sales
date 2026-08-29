# AI智能客服售前跟进系统 技术方案

版本：v0.9.45

日期：2026-07-21

最后更新：2026-08-29

适用范围：运营后台、车金后端、Windows Worker、OmniAuto Sidecar、C0—C4、车辆 Product Master、人工接管与飞书通知。

术语强约束：本文“客户媒体确定失败”只表示操作对象、动作类型和结果回执已经唯一证明，但微信或批准的媒体 Provider 明确返回内容处理失败。动作无结果、多结果、错对象、结果无法绑定、跨层合同冲突或程序无法解释自身动作，统一属于客户端技术故障，禁止创建 HandoffEvent 或发送飞书。

当前唯一架构口径：服务端负责授权、状态机、事实持久化、Brain/Guard、任务和通知；OmniAuto Sidecar 只负责微信 UI 观察、本帧物理目标定位、鼠标/键盘动作和动作证据，不得生成或继承业务消息身份；Worker 是消息身份、顺序、连续性和 `worker_stable_id` 的唯一决策者；后端只保存、校验不可变身份、去重和结算，不得根据截图、坐标或正文重新猜身份。C2 使用固定八位短码和 private 单聊门禁；文字、语音、图片按同一最终画面顺序入库，一条物理媒体只形成一个业务对象，所有已触发媒体动作必须有限终态。C2 消息身份只允许沿“帧内观察 -> 本帧动作绑定/待处理媒体动作 -> Worker 正式提交或隔离记录”单向推进；只有 Worker 已提交的正式消息可以生成 source key、查询 Ledger/Outbox、上报后端或进入 Brain。Brain 只在最新待回复尾部完整且证据足够时生成回复；当前客户媒体确定失败、高意向及必须人工批准的业务硬风险进入人工接管。人工接管以未关闭 `HandoffEvent` 为权威事实并投影 `waiting_sales_reply`，服务端按同一事件通过车金统一飞书应用立即且至多通知一次所属销售。发送前出现新文字、语音、图片或组合消息时，必须立即禁止旧回复外发。只要相关消息区域发生变化，OmniAuto 只返回最新观察和“本帧动作目标已变化/消失/冲突”的证据，Worker 必须取消旧动作计划并对最新画面执行一次完整、确定性重新仲裁；成功则下发新的本帧动作计划，失败则返回能说明“对象消失、多候选、序列无法对齐、角色无法确认、正文无法读取、布局无效或再次变化”的具体错误，零点击结束当前仲裁；不存在“暂时看不清”、`recoverable_uncertain`、发送前 `recoverable_hold`、多轮时间重试或笼统 `C2_REPLY_CONTEXT_RECOVERY_FAILED` 出口。消息视口变化摘要必须基于排除光标、工具栏、侧栏、GIF/动画帧、滚动条、悬停/播放效果和红点的规范化有序消息观察，禁止直接哈希原始 RGB 像素。本帧动作绑定只证明“当前唯一操作目标仍满足 Worker 本次动作计划”，不是跨轮业务身份；不得要求新媒体在第一次点击前已经具有 `native_source_message_id`、本次 `confirmed_action_mapping` 或正式 `worker_stable_id`。若连标题区、消息视口和输入区这些微信基本布局都无法建立，该现象固定按代码缺陷收口：禁止旧回复、记录 `C2_PRE_SEND_LAYOUT_INVALID` 及完整证据、将当前任务/Flow 结算为技术失败并释放 UI 锁，Worker 进入故障状态且停止新接单；不创建 HandoffEvent，不自动移窗/重标定/重试，不设人工解锁或清数据流程。微信列表在截图与点击之间重排时，点击后的短码校验仍是硬门禁，但明确点到其他会话时允许丢弃旧坐标并在同一授权内完整重新定位一次。添加朋友流程无论“邀请已发送”还是“已经是好友”，都只对已经证明的添加朋友 HWND 执行一次右上角关窗并验证结果。启动时基于真实微信客户区截图建立一次区域坐标地图，之后只替换 `0.9.20` 中依赖主窗口固定几何的区域边界和点位计算；会话行、消息、菜单、弹窗和表单仍由 `0.9.20` 原必要业务帧与原判断流程决定。同一个单会话事务允许在首次读取和发送前复读中形成任意多次逻辑入库；Worker 本地每组不同正式消息事实必须生成独立、确定性的 `outbox_batch_key` 并形成独立 Outbox，相同事实重试复用原本地 ID，不得因为沿用外层 `read_run_id` 而复用已确认 Outbox、丢弃新消息或继续发送旧回复。`outbox_batch_key` 绝不进入 HTTP 请求、后端 Schema 或业务状态机。

当前版本以 `gray-v0.9.43`（`b6ad192`）为唯一开发基线。历史版本的提交、合同 SHA、发布状态和变更原因只允许记录在《版本更新记录》，不在本文继续保留可能被误读为现行规则的旧实现说明。本文及配套全流程图是 `0.9.45` 唯一有效技术口径。

`0.9.45` 收口两件彼此关联但职责不同的事：一是删除跨帧业务事实比较中的几何误判；二是把媒体正式身份的形成时点固定到“实际动作结果已由 Worker 验证且跨帧连续性唯一成立之后”。跨帧业务投影只允许包含 `screen_order + sender_role + message_type + normalized_content_signature + media_state`；OCR 框、气泡尺寸、相对/绝对坐标、64 分桶、截图像素、frame/observation ID 不得参与 checkpoint、`pre_send_refresh` 或 S0/S1/S2 的业务相等判断。几何信息必须保留，但仅供当前不可变帧内解析物理消息行、确定本次操作对象、坐标换算、安全点击和诊断；产生新截图后，旧几何立即失去操作权。严禁使用五字段投影、旧坐标、邻居或气泡截图指纹映射跨帧媒体身份、继承 `worker_stable_id`、二次归并媒体或决定 Ledger/Outbox/ingest。

媒体处理固定为“先操作当前对象，动作结果确认后再发正式身份证”：Worker 在最新完整画面中按固定顺序只批准一条当前媒体动作，语音优先于图片，同类型选择当前画面 `screen_order` 最大的一条；Sidecar 重新取得最新帧，只在该帧内定位并操作该物理消息行。动作前的 observation ID、坐标、像素和视觉指纹只用于本次定位与审计，不是业务身份证。语音以本次唯一新增转写正文和动作回执为结果；图片以本次实际复制的图片字节 SHA、菜单/点击/剪贴板回执为结果。Worker 验证结果后，先把“预留号 + 本次实际动作结果”作为 `confirmed_result_pending_continuity` 持久化到 ActionJournal，此时不生成 source key、Ledger、Outbox 或正式消息；完整复读经唯一连续性比较通过后，才经唯一提交门形成正式消息。不得把回执按旧 `screen_order` 挂到复读后的当前同行对象。每次媒体动作后必须完整复读；新旧视口通过“旧序列尾部 = 新序列头部”的唯一连续重叠段处理正常滚屏，剩余媒体继续逐条处理，最终入库顺序按经唯一对齐后的权威业务序列，Brain 在全部已观察事实结算后最多调用一次。两条相同三秒语音或相似图片不是异常：先处理当前最下方一条，再根据动作回执和重叠对齐处理新到消息与剩余媒体；动作前不需要证明它在上一帧是哪一个业务对象。

媒体动作的代码不变量失败不得转人工。已经点击却没有形成结果、一次动作出现多个可能结果、实际点到非目标对象、图片结果无法绑定，或 Sidecar/Worker 合同冲突，都属于客户端技术缺陷：禁止重复点击、禁止生成正式消息、禁止 Brain、禁止创建 HandoffEvent 和飞书通知；持久化截图、OCR、ActionJournal、回执和具体错误，把当前 task/Flow 结算为 `technical_failed`，释放 UI 锁，并将 Worker 置为 `faulted + can_pull_tasks=false`。只有已经证明操作对象唯一且动作回执完整、但微信或外部媒体服务明确返回内容处理失败时，才沿用现有 customer 媒体失败的业务 Handoff 规则；self 媒体失败仍只告警。不得用 Handoff 掩盖程序无法证明自己点了什么或结果属于谁。

**实现状态：** 本文已按架构复审修正“正常视口滑动衔接”口径；全流程图已同步。`0.9.44` 生产业务代码已经通过架构复审并形成不可变标签，但正式 Windows 完整门禁发现 8 条旧测试夹具尚未迁移，因此未生成 ZIP。`0.9.45` 不改变生产业务逻辑，只迁移这些测试合同、消除 Pillow 弃用告警并统一 Worker/后端机器合同与生成 Schema；规范化合同为 `0.9.45 / 8813425572dad678b86354856dad798c43a9c47192d17319dfb8e84c8877e99e`。冻结前 `edc5066fac32a371634f8a220710b71b3e3bf4c709561dc8350444a7ed992c27` 仍作废，不是合规候选。`0.9.45` 独立 OmniAuto 来源已形成真实提交 `53caedad5baece001659aafcb5d7f86d98933e27`；车金提交、推送、标签、ZIP、配套后端部署和 Windows 实机 UAT 尚未完成，不得把自动化写成实机验收。

## 0.9.45 跨帧比较、媒体动作结果绑定与三层职责冻结

### 0.9.45.1 三类判断必须分离

| 判断 | 唯一用途 | 允许证据 | 禁止用途 |
|---|---|---|---|
| 业务事实投影 | 判断两帧的客户消息事实是否新增、缺失、替换或换序 | 顺序、角色、类型、规范化内容、媒体状态 | 不得生成或继承消息身份，不得提供点击坐标 |
| 长期消息身份 | 决定 `worker_stable_id`、Ledger、Outbox 与 ingest 身份 | Worker 正式身份状态、ActionJournal、confirmed receipt、已提交 checkpoint | 不得仅凭五字段相等或坐标相近继承身份 |
| 当前帧几何 | 在本次不可变截图中解析消息行、确定目标边界并安全点击 | 当前 frame ID、消息行 bounds、布局快照、客户区到屏幕坐标映射 | 不得跨帧复用，不得进入业务事实摘要或长期身份 |

### 0.9.45.2 OmniAuto、Worker、后端分工

| 组件 | 必须负责 | 像素/坐标权限 | 明确禁止 |
|---|---|---|---|
| OmniAuto Sidecar | 截图、OCR、当前帧消息行解析、同帧 OCR/图形观察归并、目标边界、安全点击和动作证据 | 仅用于当前帧布局、排序、目标内部点击及排障 | 根据跨帧位置或五字段相等生成/继承业务身份；向 Worker 返回 `same_business_message` 结论 |
| Worker | 唯一负责跨帧业务连续性、消息顺序、动作准入、长期身份与 `worker_stable_id` 提交 | 可消费当前帧 bounds 执行本次计划；不得把它写入业务相等摘要 | 用坐标、64 分桶或业务投影逐行复制旧 ID；在 Sidecar 之后再做第二套媒体归并 |
| 后端 | 冻结 Brain 实际使用的 checkpoint，校验合同、幂等保存、去重和结算 | 可保存原始几何为只读诊断证据 | 根据截图、像素、坐标、正文或媒体相似度重新猜身份；用几何决定是否允许发送 |

共享投影是纯函数而不是第四个决策者。Sidecar 与 Worker 必须调用同一实现，将当前帧 observations 投影为固定五字段序列；该函数不得执行媒体归并、跨帧动作映射、ID 继承、Handoff 或状态迁移。

同帧语音观察的唯一执行顺序固定为：`Sidecar 在同一不可变帧内完成唯一一次 OCR/visual 物理行归并 -> Sidecar 输出已归并 observations -> 五字段共享投影消费已归并 observations -> Worker 只做合同重复/冲突校验和后续身份决策`。共享投影不得调用或内嵌 `_merge_same_frame_voice_hint` 等等价归并；Worker 收到同一稳定锚点重复、同一物理行角色/时长/状态冲突或 Sidecar 未收敛的重复观察时，必须在任何点击、编号、Ledger、Outbox、ingest 和 Brain 前拒绝合同，不能自行挑一条或再次合并。删除共享层归并前必须通过生产 Sidecar 入口证明 OCR 与 visual hint 已先收敛为一个 observation；不得用测试替身预先合并输入。

#### 0.9.45.2.1 必须删除的重复判断与必须保留的几何

必须删除的是“跨帧认业务消息”的决定权，而不是截图、像素或坐标本身：

1. 删除 Sidecar 使用旧 observation ID、64 分桶、旧位置指纹、旧邻居或气泡截图判断最新帧对象“仍是原消息”的生产分支。
2. 删除图片插件使用跨帧坐标重合、邻居或气泡裁剪指纹重新认图片身份的生产分支。
3. 删除发送门使用图片气泡/ROI 指纹裁定 checkpoint 中旧图片与当前图片是同一物理消息的生产分支。
4. 删除 Worker、共享投影或后端在 Sidecar 同帧归并之外再次合并媒体、挑选“最像旧对象”的候选，或根据正文/时长/图片相似度生成正式身份的生产分支。
5. 删除任何把媒体动作无结果、多结果、错对象或结果无法绑定转换为 HandoffEvent 的映射。

以下能力必须保留：当前截图的 OCR 框和像素用于划分物理消息行；当前帧 bounds 用于计算目标内部点击点并验证点击没有越界；菜单边界用于确认动作类型；原始截图、OCR、坐标和视觉材料用于诊断。它们的共同限制是“只对产生它们的这一帧有效”，不得跨帧继承或拥有长期身份决定权。

代码门禁必须证明同一事务只有一套决策：Sidecar 只做同帧物理归并和动作；Worker 只做动作准入、业务顺序及正式身份提交；后端只做验真、幂等保存和结算；共享投影只做五字段转换。测试中不得复制生产判断形成第二套测试实现，也不得预先构造“已经合并/已经成功”的输入绕过生产入口。

### 0.9.45.3 C0—C4 比较口径

| 阶段 | 保持严格的事实 | 几何处理 | 本轮是否修改 |
|---|---|---|---|
| C0 线索、分配、任务领取 | 线索/销售/Worker、去重键、租约和版本 | 不适用 | 否 |
| C1 授权、微信窗口、添加结果 | 手机号、短码、任务归属、可见标定 HWND、申请结果 | 启动坐标地图和当前区域只负责点击；不做跨帧像素相等判断 | 否 |
| C2 会话定位 | private、标题、客户短码、授权版本 | 列表重排后旧坐标失效并重新定位 | 否 |
| C2 消息业务变化 | 数量、顺序、角色、类型、规范化内容、媒体状态 | OCR 框或气泡轻微抖动不得单独判为新消息 | 是，仅替换变化摘要 |
| C2 媒体身份与动作 | Worker 每次只批准最新完整画面中的一条当前媒体动作；语音优先、图片随后；动作结果确认后才提交正式身份 | 当前帧 bounds 是本次点击必要条件；坐标、像素、邻居和气泡截图指纹不得跨帧认身份 | 是，删除跨帧认对象旁路，统一为“实际动作结果后绑定” |
| C2 入库 | `worker_stable_id`、Ledger、Outbox、载荷不可变性 | 坐标不生成 source key | 否 |
| C3 Brain 与 claim | batch、claim、消息版本和过期状态 | 不适用 | 否 |
| C3 checkpoint、`pre_send_refresh` | 完整业务五字段序列严格相等/唯一尾部追加/唯一视口滑动衔接/需一次上下文扩展/不连续五类结果 | 坐标、像素和分桶不得一票否决 | 是 |
| C3 S0/S1/S2 | 三个独立真实时点；标题、会话、业务序列、草稿、输入内容和发送回执严格 | 只允许同帧定位；跨帧轻微几何变化不判新消息 | 是 |
| C4 召回资格、读取和发送 | 冷却、会话状态、Handoff、批次与回执严格 | 读取复用 C2，发送复用 C3，不另造比较器 | 自动继承，不新增流程 |

验收必须同时证明：业务事实不变而气泡跨 64 分桶边界、偏移或缩放若干像素时允许继续；新增、缺失、替换、角色/类型/正文/媒体状态或顺序变化时仍禁止旧回复；当前点击 bounds 无效时仍零点击。两条相同时长语音、两张相似图片必须分别只动作一次、分别绑定自己的实际动作结果；同一条媒体的 OCR/视觉重复观察只能由 Sidecar 在同一帧归并一次。动作已经触发但结果缺失、多结果、错对象或回执矛盾时，必须以 `technical_failed + Worker faulted` 结束，零正式消息、零 Brain、零 Handoff、零飞书、零重复 UI；已经唯一证明对象与回执但内容处理明确失败时，才允许进入现有 customer 媒体失败 Handoff。ActionJournal 恢复不得重新操作微信；C4 必须复用同一 C2/C3 链路，不得另建判断器。禁止用 FakeBridge 直接返回成功绕过生产 Sidecar/Worker 边界证明“主流程未受影响”。

### 0.9.45.4 跨帧视口连续性唯一规则

“视口顶部消息滚出”是微信正常行为，不得一律视为截断或客户端故障。C2 媒体动作后复读、C3 `pre_send_refresh`/S0/S1/S2 与 C4 复用读取只能调用同一个 Worker 连续性比较器，固定输出以下五种关系：

| 关系 | 可执行证明 | 处理 |
|---|---|---|
| `business_sequence_equal` | 新旧完整五字段序列相等，且满足以下任一强条件：两帧都证明可见区包含同一完整顶部边界；或所有可见项都能由已提交 Worker 身份/正式动作回执唯一连续映射。若满屏且全是重复弱事实，不能仅凭五字段相等直接放行 | 保持旧事实，不重复入库/动作 |
| `unique_tail_append` | 旧序列是新序列的唯一完整前缀，且有非空尾部新消息 | 保留旧事实，只处理新尾部 |
| `unique_viewport_slide_with_tail_append` | 存在唯一、非空、连续的重叠段：旧序列尾部精确等于新序列头部；旧序列前缀仅因正常满屏滚动离开可见区，新序列尾部是新到消息 | 接受正常滚屏，保留重叠段身份，只处理新尾部 |
| `continuity_context_expansion_required` | 重复文字/同时长语音/相似图片导致零个或多个合法重叠段，但当前仍未执行第二次上下文扩展 | 同一 UI 锁和 Flow 内最多执行一次受限历史扩展读取；只允许滚动、截图、OCR 和对齐，零媒体点击、零发送、零 Brain |
| `business_sequence_not_continuous` | 扩展后仍无重叠或多解，或证明发生替换、中间插入、换序、unknown、证据矛盾 | 禁止继续旧回复/媒体动作；保留已形成动作回执，当前 task/Flow=`technical_failed`、Worker=`faulted`，零 Handoff/飞书 |

比较器只回答业务序列关系，不得覆盖发送事务阶段。`pre_send_refresh`、S0、S1 发生在物理发送触发前，可以据此取消/作废旧回复或进入技术故障；S2 发生在 Enter/发送点击可能已经触发之后，绝不能倒推为“零发送”或再次发送。S2 若无法确认本次发送结果，仍必须沿用既有 `SEND_RESULT_UNKNOWN`/sent_ack 终态并禁止自动补发；新到客户事实由后续正式读取处理。

唯一重叠段的计算方向固定为“旧序列的连续尾部”对“新序列的连续头部”；不允许任意子串、跳行匹配或仅因重叠长度更大就忽略其他合法解释。`screen_order` 表达的是每一帧内部从上到下的相对顺序，每帧都会重新编号，不能要求旧尾部与新头部的绝对数值相等。比较某个候选重叠段时，必须把两边候选段各自从 0 重新编号，要求其相对顺序连续，并逐项严格比较 `sender_role + message_type + normalized_content_signature + media_state`；这仍是同一份五字段投影，不得另造第二套消息摘要。比较证据只使用该业务投影、已提交的 Worker 身份/动作回执和受限扩展取得的稳定业务边界；坐标、像素、64 分桶、OCR 框和气泡截图不得用于挑选某个对齐解释。

“强边界”只允许以下三类证据：当前观察携带的真实原生消息 ID 与已提交消息一致；一条已提交文字/system/AI sent_ack 的 `角色 + 类型 + 规范化正文` 在 checkpoint 与扩展画面中各自只出现一次且顺序上下文一致；或一条已提交语音的正式动作回执、规范化转写和可获得时长共同对应扩展画面中唯一一个已转写语音事实。仅有 `worker_stable_id` 的历史记录不能让当前 OCR 行自动继承身份；图片动作回执或图片字节 SHA 若当前画面无法重新观察，就不能单独充当跨帧强边界；坐标、行号、时长单值和相似内容也都不是强边界。

一次“受限上下文扩展”是一个完整且不可嵌套的只读事务：在同一 UI 锁/Flow 内，从当前底部视口向上读取，直到重新看见 checkpoint 中最近一个符合上一段定义的强边界，最大读取范围不得超过本 checkpoint 覆盖的历史长度；随后必须回到消息底部并重新取得一张最新权威帧，才允许继续媒体编排。整个事务可包含为找到该边界所必需的多次滚动/截图/OCR，但只消耗一次扩展机会，且零媒体点击、零输入、零发送、零 Brain。找不到强边界、无法回到底部或回到底部后业务序列再次发生无法唯一解释的变化，统一进入 `business_sequence_not_continuous`；禁止无限滚动或重新开启第二次扩展。

显式传入的 `observations=[]` 必须按“本次观察确实为零条业务消息”处理，绝不能回退旧画面。只有同一帧同时具备合法 private/短码、有效布局、`send_context_guard.ok=true`、`message_count=0` 和 `tail_complete=true` 时，它才是可用于 `friend_welcome_empty` 等场景的权威空序列；缺少上述空视口证明时返回具体“观察证据缺失”技术错误。只有参数未提供（`None`）时，才可按调用入口的既有合同选择已持久化 payload。禁止使用 `observations or old_observations` 把空复读偷换为旧画面。

媒体动作回执必须绑定到 Sidecar 实际动作帧和实际结果：语音绑定唯一新增转写结果，图片绑定唯一新剪贴板代次与实际图片字节 SHA。连续性通过前，回执只能处于 ActionJournal `confirmed_result_pending_continuity`，不得形成 source key、Ledger、Outbox 或正式消息。该值只是同一 Flow 内“结果已落盘、等待连续性结算”的中间阶段，不是第五个媒体终态；它必须在本 Flow 内收敛到 `committed_completed / committed_failed / identity_unresolved` 之一，进程崩溃后也只能无 UI 恢复该结算，禁止重新点击。后续复读只用于识别正常滚屏、新到消息和剩余未处理媒体，不得再把已形成回执按旧 `screen_order`、旧 observation ID 或旧坐标挂给复读后的当前行。

统一正向验收场景必须覆盖：`[文字1,图片1,图片2] -> 处理图片2 -> 新视口[图片1,图片2,文字2,语音B] -> 两张弱图片产生多个重叠解释 -> 执行一次受限上下文扩展并重新看见文字1强边界 -> 回到底部取得最新帧 -> 唯一确认正常滑动与新尾部 -> 先处理语音B -> 再处理图片1 -> 最终按权威序列入库 -> Brain 最多一次`；还必须覆盖“合法空会话证据 + observations=[]”继续走 `friend_welcome_empty`。统一反向场景必须覆盖“observations=[] 但缺少合法空视口证明”、缺乏已提交强边界的重复弱图片 `[图片A,图片B] -> [图片B,图片C]` 多解、扩展后仍多解、替换和换序；断言零错绑回执、零重复媒体 UI、零伪正式消息、零 Brain、零 Handoff/飞书。测试必须从正式 TaskRunner/Sidecar 入口走完 ActionJournal、Ledger、Outbox 和后端路由，不得只调内部比较函数或用 FakeBridge 预组装成功终态。

## 文档治理规则

1. 项目级文档只允许四份：PRD、本文技术方案、版本更新记录和一张全流程图。
   不再建立独立接口合同、事务恢复架构、专项测试/验收报告、交接文档、一致性检查、
   场景矩阵、文档目录或子流程图。已有有效内容全部并入这四份，历史状态进入版本更新记录。
2. PRD 只定义业务目标、用户行为、范围和验收；本文是架构边界、数据/接口合同、状态机、
   恢复、安全和工程门禁的唯一事实源；全流程图只可图示 PRD 与本文，不能新增规则；
   版本更新记录只记录版本、提交、验证、风险和回滚证据，不能反向创造业务或架构口径。
3. `contracts/c2_contract_v3.json`、OpenAPI/schema、数据库迁移和代码内类型属于可执行生成物，
   不是第五份项目文档；它们必须由本文字段生成或校验，不得拥有本文未定义的同义状态和兜底决策。
4. 测试结果、Windows UAT、审计结论、客户端版本、提交、包 SHA 和回滚基线统一写入
   版本更新记录，不再单独生成测试报告或交接报告。原始日志、截图、ZIP 属证据文件，不是决策文档。
5. 每次正式变更顺序固定为：更新 PRD（有产品变化时）-> 更新本文 -> 同步全流程图 ->
   更新机器合同/代码 -> 定向自动化 -> 干净且可追溯的 Git 提交 -> 不可变候选包 ->
   按影响范围执行 Windows UAT -> 把结果写入版本更新记录。不得用聊天记录代替文档，
   不得由代码审计意见直接创造新流程，也不得用 dirty 工作区构建的包形成正式 UAT 结论。
6. 对外 HTTP 接口的唯一正式名称固定为“接口编号 + HTTP 方法 + 从 `/api` 开始的
   完整路径”。前端或 Worker 因 `base_url` 已包含 `/api` 而使用的相对路径、Python/
   TypeScript 函数名、中文简称都不是第二个接口名；URL 路径使用 kebab-case，JSON
   字段和领域对象字段使用 snake_case，两者不得被误认为两个接口。新增、改名或废弃
   接口必须先修改本文的权威目录和接口编号，不允许在代码、聊天记录或派生合同中另起
   同义名称。
7. 灰度版本使用唯一 `0.9.x` 序列：`0.9.0` 至 `0.9.44` 已冻结，当前目标候选为 `0.9.45`；
   后续任何内容不同且进入测试的候选必须继续升版。PRD（仅有产品变化时）、技术方案、全流程图、版本记录、客户端、
   后端、OmniAuto 合同 `contract_revision`、生成 Schema、manifest 和安装包必须写入同一个
   精确版本，禁止各自升版、复用旧号覆盖新内容或把占位符 `0.9.X` 写入运行产物。
   `contract_version=3`、`observation_schema_version=3` 和文中 V3 仅是协议结构代号，不属于
   灰度发布版本。`0.9.41` 已形成不可覆盖的代码、合同和 ZIP 回退基线；`0.9.42`、`0.9.43`、`0.9.44` 已形成不可覆盖标签，其中 `0.9.44` 因完整 Windows 门禁旧夹具失败未生成 ZIP。当前目标为 `0.9.45`。客户端、后端、OmniAuto 生成 Schema、manifest 与打包入口最终必须统一使用 `contract_revision=0.9.45`；本地实现已真实计算规范化合同 SHA `8813425572dad678b86354856dad798c43a9c47192d17319dfb8e84c8877e99e`。旧 `edc5066fac32a371634f8a220710b71b3e3bf4c709561dc8350444a7ed992c27` 未包含本节最终连续性规则，已作废。架构复审已通过，OmniAuto 来源提交已固定；车金候选提交、GitHub Windows 门禁、Windows 实机 UAT 和正式包仍待完成。
   版本车道固定为：`0.9.x` 仅用于正式上线前灰度验证，`1.0.x` 用于正式上线及其稳定性修复，
   `1.1.x` 用于下一期优化。三个 `x` 都只表示版本系列，任何提交、合同、Schema、manifest、
   安装包和运行日志必须写入 `0.9.15`、`1.0.0`、`1.1.0` 等精确版本，不得写入字面占位符。
   `1.0.x` 不混入下一期功能；`1.1.x` 不得反向覆盖已经发布的 `1.0.x` 产物。
8. 灰度稳定分支固定为 `codex/gray-release-0.9.x`，只接收当前灰度版本的缺陷修复、合同同步、
   测试和发布治理，不在该分支增加下一期功能。每个不可变灰度候选使用精确标签
   `gray-v0.9.0、gray-v0.9.1、gray-v0.9.2、gray-v0.9.3、gray-v0.9.4、gray-v0.9.5、gray-v0.9.6……`。`main` 只接收已完成灰度验收的确切标签提交，不允许把
   dirty 工作区、未固定的本地提交或多个并行开发分支直接合入 `main`。


## 当前项目口径

| 事项 | 当前口径 |
|---|---|
| 抖音线索获取 | 本期先人工导入 |
| 抖音开发者开放平台 | 已审核通过 |
| 抖音 API 自动接入 | `1.1.x` 优化版本 |
| 企业私信 Webhook/OAuth | `1.1.x` 优化版本 |
| 小风车/巨量引擎自动同步 | `1.1.x` 优化版本 |
| 本期线索重点 | 人工录入/导入、字段映射、去重校验、导入结果反馈、线索分配和跟进 |

## 总体架构与职责边界

```text
运营后台 -> 车金后端 -> PostgreSQL
Windows Worker -> OmniAuto Sidecar -> 微信桌面端
车金后端 -> OmniAuto AI Engine / Product Master / RAG / Guard
车金后端 -> 车金统一飞书自建应用 -> 会话所属销售
```

- 服务端是业务决策和数据权威：负责销售分配、读取授权、会话状态机、事实入库、Brain/Guard、`reply_action`、`chat_reply`、`HandoffEvent`、飞书通知和幂等。
- OmniAuto 是微信观察和本帧 UI 动作证据权威：负责 OCR、会话类型、正式短码、消息结构、本帧候选及局部几何、点击和动作回执；它可以拒绝不安全点击，但不得输出“与历史是同一条业务消息”、`worker_stable_id`、`source_message_key` 或任何跨轮身份结论。
- Worker 是消息身份与动作准入的唯一决策者：校验后端授权和 Sidecar 原始证据，完成消息顺序/连续性仲裁，生成动作 ID 与不可复用预留号，决定是否允许本帧动作，并且只有在有效动作回执或正式历史检查点成立后提交 `worker_stable_id`；同时维护 ActionJournal/Ledger/Outbox 并上报事实。
- 后端只接受 Worker 已提交的稳定身份，校验不可变字段、幂等保存、去重和结算；不得根据截图、OCR 正文、坐标、时长或媒体相似度重新生成、修复或覆盖消息身份。
- 前端只做运营控制面；不持有 Worker Token、模型密钥、飞书 `app_secret` 或 `tenant_access_token`。
- 飞书通知是服务端基于 `HandoffEvent` 的内部副作用，不进入统一 `Task.task_type`，不由 Worker 参与。
- 当前不使用 Kubernetes、复杂微服务、RBAC、多商户 SaaS 或大风车 API。

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
| 图片理解 | Worker 内置 OmniAuto Vision + 服务端批准的视觉 Provider | Vision 在 Windows 客户端执行并直接调用批准的外部 Provider；正式包内置 CI 注入的客户端专用低权限 Key，用户不手工配置；原图不上传车金后端、不落盘，低置信转人工。 |
| 知识检索 | OmniAuto RAG + 关键词/规则混合检索 | 先基于现有能力工程化；Dify/FastGPT 仅预留 Adapter，不作为第一期核心运行时。 |
| 车辆主数据 | OmniAuto Product Master | 本地手工 V2 车辆是唯一车辆事实源；运营通过车辆页面或 Excel 录入真实数据。不同步、不读写大风车 API。 |
| 文件存储 | 服务端持久化文件卷（首期） | 车辆库图片允许运营上传并持久化；微信客户图片仍只在 Worker 内存处理，不上传、不落盘。两类图片不得混用。 |
| 日志与错误码 | JSON 结构化日志 + error_code 字典 + trace_id | 控制面、Worker、服务端日志使用同一错误码体系，便于运维排障。 |
| 部署 | Docker Compose 部署服务端；Worker 以 Windows 安装包/可执行文件交付 | 第一阶段不使用 Kubernetes；保证单机可复制部署和备份恢复。 |
| 测试 | pytest + 接口集成测试 + Worker 端到端录屏/日志验收 | 核心验收看状态机、幂等、防重复发送、错误码、微信串行操作。 |

明确不采用：

- 不把 Dify 作为第一期核心对话运行时，只预留后续 Adapter。
- 不使用 Kubernetes、服务网格、复杂微服务拆分。
- 不在第一期做多商户 SaaS、RBAC/角色分级、计费和高可用集群；指定账号登录和服务端会话鉴权属于本期安全门禁，不得以“不做复杂权限”为由省略。
- 不读取或破解微信数据库，不使用非公开微信协议。
- 不做 Mac Worker 正式版本；Mac 页面和人工传值只允许用于调试，不属于正式业务主链路。
- 不把 OmniAuto 整体不加边界地揉进车金业务主程序；OmniAuto 按两类能力复用：服务端复用 AI Engine、RAG、Evidence Pack、Guard、回复编排能力，Worker 端复用本地 RPA Sidecar 操作微信。

## 运营后台登录与会话鉴权

本节是 PRD v0.9.5“指定账号登录、登录成功即全部权限”的唯一技术实现口径。
鉴权只回答“是不是已登录的后台账号”，第一期不再回答“这个账号属于哪个权限角色”。

### 登录职责与边界

| 身份 | 正式凭证 | 允许访问 | 明确禁止 |
|---|---|---|---|
| 运营后台账号 | 账号密码换取的服务端会话 Cookie | 线索、车辆、销售、Worker 管理、任务中心、操作日志等全部后台功能 | 使用 Worker 身份接口或读取服务端密钥 |
| Worker 客户端 | `Worker Token + client_instance_id` | 心跳、绑定、任务领取/回报、C2/C3 Worker 接口 | 访问运营后台管理接口 |
| 服务内部调用 | 独立内部凭证或进程内调用 | 明确登记的内部接口 | 使用后台 Cookie 或 Worker Token 代替内部身份 |

- 浏览器、Worker 和服务内部身份必须使用不同验证器，任何一种凭证都不能跨边界替代
 另一种凭证。
- `/healthz`、`/readyz`、登录接口和静态资源不要求后台会话；所有运营后台业务
  API 必须在服务端校验有效会话，不能只靠前端隐藏页面或按钮。
- 后台账号与业务对象“销售”不是同一个概念。第一期不把销售记录自动变成登录账号，
  也不根据销售身份生成只读权限。

### 数据模型

`admin_accounts` 至少包含：

| 字段 | 说明 |
|---|---|
| `id` | 服务端生成的账号 ID。 |
| `username_normalized` | 规范化后的唯一登录名；比较时不依赖前端大小写处理。 |
| `display_name` | 操作日志和后台左下角展示名称。 |
| `password_hash` | Argon2id 密码哈希；禁止保存、打印或返回明文密码。 |
| `enabled` | 是否允许登录；停用后已有会话也必须失效。 |
| `session_version` | 停用或重置密码时递增，用于统一作废旧会话。 |
| `last_login_at / created_at / updated_at` | 登录和维护审计时间。 |

`admin_sessions` 至少包含：

| 字段 | 说明 |
|---|---|
| `id / account_id` | 会话及所属账号。 |
| `token_hash` | 随机会话 Token 的不可逆摘要；数据库不保存可直接登录的原 Token。 |
| `session_version` | 创建会话时的账号版本，必须与当前账号一致。 |
| `created_at / last_seen_at` | 创建和最近使用时间。 |
| `idle_expires_at / absolute_expires_at` | 默认空闲 12 小时、最长 7 天；允许部署配置缩短，不允许无限会话。 |
| `revoked_at / revoke_reason` | 退出、停用、重置密码或安全处置时的撤销记录。 |
| `ip_address / user_agent` | 安全审计快照，不作为唯一身份依据。 |

账号由服务端管理命令创建、停用和重置密码；第一期不开发注册、账号管理、自助找回
密码或修改权限页面。初始密码不得写入 Git、镜像、`.env.example` 或群文件。

### 接口合同

| 方法 | 路径 | 请求/响应与行为 |
|---|---|---|
| `POST` | `/api/auth/login` | 请求 `username + password`；成功后创建新会话、轮换 Token、设置 Cookie，并返回 `operator_id + operator_name`。失败统一返回“账号或密码错误”，不暴露账号是否存在或已停用。 |
| `GET` | `/api/auth/session` | 校验会话后返回 `operator_id + operator_name`；不返回 `role/viewer/permissions`。无效、过期、撤销或账号停用统一返回 401。 |
| `POST` | `/api/auth/logout` | 幂等撤销当前会话并清除 Cookie；无论会话是否已失效都可安全调用。 |

正式 Cookie 名称固定为 `chejin_admin_session`，使用密码学安全随机数且至少 256 bit；
设置 `HttpOnly`、生产环境 `Secure`、`SameSite=Strict` 和 `Path=/api`。生产部署优先
同源反向代理 `/api`；开发环境如需跨端口，CORS 必须使用明确来源白名单并开启
credentials，禁止 `allow_origins=["*"] + credentials`。

浏览器请求统一使用 `credentials: "include"`。前端不得读取、保存或转发会话 Token，
不得继续使用 `localStorage`、`VITE_ADMIN_TOKEN` 或 `Authorization: Bearer ...` 作为
运营后台登录。所有非 GET/HEAD 的后台请求同时校验 `Origin`；不符合部署允许来源的
请求直接拒绝，作为 Cookie `SameSite` 之外的 CSRF 防线。

### 全权限与代码边界

- 登录成功账号统一拥有全部后台功能权限；前后端删除 `admin/operator/viewer/只读`
  权限集合、车辆只读分支和联系方式查看角色门禁。
- `AuthSession` 对外合同只保留账号 ID 和显示名称。后端审计上下文可保留
  `actor_type=admin_account/system/worker` 区分调用者类型，但不能据此对后台账号做
  RBAC。
- 不得批量删除微信业务字段 `sender_role`、`sender_role_source`、
  `subject_sender_role`；这些字段证明消息由客户还是我方发送，与后台登录权限无关。
- 敏感车辆字段是否允许进入 AI、手机号明文查看必须填写原因并写审计等规则继续有效；
  “全部后台权限”不等于允许模型读取内部字段，也不等于取消操作审计。

### 会话安全与审计

- 登录成功后创建新会话，不接受客户端指定 session id；登录、提权边界变化时防止
  session fixation。
- 账号停用、密码重置、主动退出必须立即撤销相关会话；会话过期后不得静默降级为
  开发身份或旧 Bearer Token。
- 登录接口按规范化账号和来源 IP 双维度限速；连续失败使用渐进等待，不能无限尝试。
- 登录成功、登录失败、退出、账号停用和密码重置必须写操作日志。失败日志允许
  `operator_id=null`，记录规范化账号提示、IP、User-Agent、Request ID、结果和内部
  原因分类；绝不记录密码、Cookie、会话原 Token 或密码哈希。
- 正式环境必须强制启用会话鉴权；删除 `ADMIN_API_TOKEN`、
  `OPERATOR_API_CREDENTIALS` 正式路径，禁止通过 `AUTH_ENFORCEMENT=false` 绕过。

### 迁移、验收与回滚

实施顺序固定为：数据库迁移和账号管理命令 -> 登录/会话/退出接口 -> 运营后台登录页
和 Cookie 接入 -> 删除旧 Bearer 与角色门禁 -> 全量鉴权回归。迁移前先建立一个可用
初始账号，但初始密码只通过安全渠道交付。

至少覆盖以下自动化和联调用例：正确/错误密码、未知/停用账号、限速、刷新保持登录、
空闲和绝对过期、退出、停用和重置密码使旧会话失效、未登录遍历全部后台路由、所有
账号均可完成写操作、Cookie 安全属性、Origin 拒绝、日志脱敏、后台会话与 Worker
Token 双向越权拒绝。

回滚只能回滚应用代码，不能删除已产生的账号、会话和登录审计事实。若新登录链路
故障，可回滚到上一应用版本并在受控维护窗口修复；不得重新开放共享 Admin Token
作为生产旁路。

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

## 系统详细设计

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
线索入库 -> 轮询分配销售 -> 创建add_friend任务 -> 若销售未绑定Worker则blocked -> 后续绑定Worker后恢复pending -> Worker执行 -> task.status=completed且result_code=invite_sent/already_friend -> 只结算C1任务 -> C2首屏发现有效短码后才绑定/创建Conversation并进入首次激活读取
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
- 展示层以 `conversation_id + read_run_id / reply task id` 关联同一次客户事务，消费 Worker 已有
  `current_step`、任务事实和本地展示历史，将首屏发现、定位读取、媒体、上报、Brain、发送与回执
  投影为一条只追加已发生节点的动态链路。该投影只读，不参与调度、授权、事实结算或重试决策；
  展示异常必须被隔离，不能杀停 Worker。
- 首屏未命中客户时保留独立扫描过程；一旦命中并建立客户事务，后续阶段必须在同一展示链路追加，
  不得因进入 `target_chat_locating / c3_brain_waiting / chat_reply` 重置为另一套 screen。
- 当前过程容器使用原生可访问滚动语义，保留滚轮、触控板、方向键、PageUp/PageDown、Home/End 和
  可拖动滚动能力；视觉上仅覆盖为窄圆角中性灰滑块。隐藏原生轨道时必须保留等价拖动和键盘能力。
- Worker 不保存业务主状态，不直接调用文本大模型，不持有服务端 Brain、飞书或数据库密钥。Worker 正式包持有的唯一模型凭据是客户端直连图片理解所需的 Vision 客户端专用 Key：由 CI Secret 注入安装包，固定 Provider/接口/模型白名单，限制额度与调用频率，可监控、吊销和轮换；不得写入 Git、独立 `.env`、启动脚本、manifest、日志或故障证据。正式包不依赖用户手工设置 `CUSTOMER_IMAGE_UNDERSTANDING_API_KEY`；该环境变量只允许开发包显式覆盖。
- Worker 不需要开机自启，通过执行台启动按钮操作。
- Worker RPA 能力优先复用 OmniAuto 仓库的微信 Win32/OCR sidecar、RPA 全局锁、输入/点击节流、截图证据和验收门禁；本项目新增 Worker 任务桥接层、RPA Sidecar 调用协议和 `add_friend` 执行器。`add_friend` 字段契约、结果码和验收口径统一写入本文档模块4，不再另设独立集成方案作为当前有效入口。
- C1 `add_friend`、C2 会话绑定与文字/语音/图片事实、C3 AI 回复和 C4 召回使用本文定义的单一主链；车辆库/知识库只在服务端接入，不改变 Worker 的微信事实采集和发送合同。
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

服务端只面对 Worker 主进程，不感知 OmniAuto 内部实现；任务状态由服务端统一维护。

正式能力：

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

#### 3.1.1.1 暂停接单与紧急停止的唯一语义

`暂停接单` 是“停止接收新工作，当前业务流程安全收口后停止”，
不是强制中断按钮。`紧急停止` 才是立即禁止新 UI 动作的故障处置。
两者不得共用一个取消布尔值或错误码。

点击“暂停接单”后的状态转换固定为：

```text
用户点击暂停接单
-> 本地立即持久化 pause_requested=true
-> 服务端立即投影 worker.run_status=paused，禁止新 task pull、新首屏扫描、新 read-target 和新 UI 锁
-> 当前无 inflight_flow_id：立即进入完全 paused
-> 当前有 inflight_flow_id：仅该流程可继续使用或续租已有资源，并可按原流程重取必需的 UI 锁、授权票、任务 lease 和 batch continuation
-> 该流程完成事实入库/Journal/Outbox/sent_ack/失败结算，到达业务终态
-> 释放 UI 锁和 inflight_flow_id
-> 完全 paused
```

`inflight_flow_id` 必须在进入某个具体客户的业务流程、首次获取该客户 UI 锁之前持久化，值为当前
`task_id` 或 `read_run_id`，不得用会话短码、界面 `current_step` 或线程 ID 代替。
暂停后只允许继续这一个 ID；任何其他会话、恢复队列、扫描或任务必须等待
重新“开始接单”。客户端界面在收口期显示“正在暂停，当前客户处理完后停止”，
不得提前显示“已暂停”。

无客户目标的全局首屏扫描不登记 `inflight_flow_id`，暂停时允许直接终止，也不得改变
任何 Conversation 状态。C2 主循环的唯一分支为：无在途流程且
`_can_start_new_flow=true` 时允许首屏扫描和获取 read-target；存在在途流程时只允许
原流程恢复或结算，禁止新扫描；`paused` 且无在途流程时两者都不得执行。禁止在正常
空闲接单时用 `_can_continue_inflight_flow` 作为进入首屏扫描的前置条件。

服务端在 `Worker.inflight_flow_state` 单一 JSON 字段保存权威在途登记，结构固定为：

```json
{
  "status": "active|draining",
  "flow_id": "task_id or read_run_id",
  "flow_kind": "task|c2_read|chat_reply",
  "registered_at": "ISO-8601 UTC",
  "pause_requested_at": "ISO-8601 UTC or null"
}
```

Worker 必须在具体客户业务流程首次获取 UI 锁之前调用 `API-WORKER-06` 登记，只有
`run_status=running` 且当前没有另一个 active flow 时才能成功。点击暂停时，
`API-WORKER-03` 必须在同一数据库事务内将 `run_status` 改为 `paused`，并把已有
active flow 改为 `draining`、写入 `pause_requested_at`；不得接受暂停后新登记。
当前流程达到业务终态并释放本地 UI 锁后，调用 `API-WORKER-07` 清除同一 flow。
“业务终态”必须同时满足：本轮 Task/ReplyAction 已终态，本轮 Journal 已结算，
本轮 Outbox 与 sent_ack 已得到后端确认或进入有同一 flow 凭证可继续重传的明确恢复态。
任一条件不满足时不得清除本地或后端 `inflight_flow_id`。后端对 `c2_read` 的 finish
同样必须校验本轮事实结算证明，不得只校验 `task/chat_reply`。

`API-WORKER-07` 请求体固定为：

```json
{
  "flow_id": "task_id or read_run_id",
  "terminal_kind": "task_terminal|read_confirmed|failed_before_message_action|read_failed_no_fact",
  "conversation_id": "c2_read 时必填，否则为 null",
  "error_code": "failed_before_message_action/read_failed_no_fact 时必填，否则为 null"
}
```

- `task_terminal`：只适用于 `task/chat_reply`，后端任务必须已是终态。
- `read_confirmed`：只适用于 `c2_read`，后端 Binding 的 `last_read_run_id` 必须等于
  `flow_id`，且 `last_read_completed_at` 非空、`last_read_result` 为 `new_facts/no_change`。
- `failed_before_message_action`：只允许在尚未读取消息、尚未触发语音/图片/发送、
  本地无本 flow 的 Journal/Outbox 时使用；后端必须记录 conversation、error_code 和审计时间。
  一旦发生消息或媒体动作，只能完成 `read_confirmed`，不得用失败类型提前清除 flow。
- `read_failed_no_fact`：已经调用当前会话消息读取，但 Sidecar、OCR 或合同校验在形成任何
  可信消息、媒体动作、Ledger、Journal、Outbox、sent_ack 前失败时使用；后端确认同一
  `read_run_id` 没有消息事实并记录审计。它不能用于绕过已经形成的事实或动作。

所有 terminal kind 在本地调用 finish 前，都必须检查同一 flow 的待处理 Outbox、Ledger、
ActionJournal 和 sent_ack；`task_terminal` 也不例外。发送前嵌套复读与外层任务复用同一
flow 时，必须先结算嵌套产生的 C2 事实，再结束任务 flow。

暂停后所有续行请求必须携带 `X-Inflight-Flow-Id`。服务端仅在该值与
`Worker.inflight_flow_state.flow_id` 严格相等、状态为 `draining`，且原授权、lease、
会话和幂等键仍有效时，允许该流程续租或重取完成收口所必需的资源；不得据此领取
新任务、切换会话或扩大授权。客户端单独传任意 flow ID 不得绕过暂停。
心跳和 UI 可以展示该字段的只读摘要，但不得成为第二权威来源。

本地持久化位置固定为 Worker SQLite `client_settings` 中唯一键
`runtime_control_v1`，值为 JSON：

```json
{
  "pause_requested": true,
  "pause_requested_at": "ISO-8601 UTC",
  "inflight_flow_id": "task_id or read_run_id",
  "inflight_flow_kind": "task|c2_read|chat_reply",
  "inflight_started_at": "ISO-8601 UTC"
}
```

禁止在 Binding、内存布尔值、UI 属性或另一个文件中再保存同义状态。
`pause_requested / inflight_flow_id` 的建立、更新和清除必须在同一个 SQLite
`BEGIN IMMEDIATE` 事务内完成读取、校验和 UPSERT，禁止先读后另开连接覆盖；
流程终态已持久化后才能清空 `inflight_flow_id`。

强制约束：

- `_can_start_new_flow` 与 `_can_continue_inflight_flow` 必须是两个独立判定。
  禁止再用 `binding.run_status == running` 同时表示“可接新工作”和“已开始流程可收口”。
- 暂停期间，当前流程仍必须执行授权复核和安全门禁；暂停不会绕过短码、
  `private`、消息顺序、claim-send 或发送结果结算。
- 当前 `c2_read` 已进入语音或图片阶段后，授权复核使用
  `_can_continue_inflight_flow(read_run_id)`；不得再用 `_can_start_new_flow` 取消当前媒体处理。
- `pause_requested` 本身不是消息事实、身份门禁或业务风险，不得生成
  `MESSAGE_IDENTITY_UNCONFIRMED`、`C2_MESSAGE_HISTORY_GAP`、`HandoffEvent`、
  `waiting_sales_reply`、Brain 请求或飞书通知。
- 暂停之前已经由业务事实决定的 handoff 可继续幂等结算，但 `HandoffEvent`
  必须记录原始 `read_run_id / reason_code / created_at`，不得将后来的暂停写成原因。
- 紧急停止触发后，未开始的 UI 动作立即禁止；已发生的物理点击只允许完成
  Journal/Outbox 结算，不得重点、补发或把中断伪装成身份 handoff。
- `API-WORKER-03/06/07` 对同一 Worker 的在途状态读写必须锁定 Worker 数据行
  （PostgreSQL `SELECT ... FOR UPDATE` 或等价原子条件更新）。两个并发 start 最多一个成功；
  pause 与 start 并发时不得出现两个 flow、丢失 pause 或把新 flow 伪装成 draining。

客户端重启时如本地仍有 `pause_requested=true`，必须先同步 `run_status=paused`
并结算已存在的 Journal/Outbox；在用户明确点击“开始接单”之前不得重建 UI 读取流程。

暂停接单必须通过以下定向验收：

1. 空闲时点击暂停：服务端进入 `paused`，之后零 task pull、零 C2 扫描、零 UI 点击。
2. `target_chat_locating` 中点击暂停：仅原 `read_run_id` 继续到安全终态，其他短码不得开始。
3. Brain 已返回 `send_reply` 但尚未发送时点击暂停：原 flow 仍按
   `pre_send_refresh -> claim-send -> send -> sent_ack` 完成；UI 必须明示“当前客户仍会完成”。
4. Journal 已进入 `trigger_attempted` 时点击暂停：只收口原动作，不得重发；终态后才释放 UI 锁。
5. `recent_ai_sent` 读取中点击暂停：暂停前后 HandoffEvent 数量必须一致，
   除非该 `read_run_id` 已有与暂停无关的业务硬门禁证据。
6. 暂停收口期间重启：重启后仍为 `paused`，只重传已持久化终态，零新 UI 动作。
7. 紧急停止与暂停分别回放：前者取消未开始动作，后者让原 flow 收口；
   两者都不得产生身份 handoff。
8. 正常 running、无 inflight flow 启动 C2 主循环：必须实际完成一次首屏扫描；
   不得因 `_can_continue_inflight_flow=false` 永久空转。
9. 当前 read_run 已定位并准备处理语音/图片时点击暂停：原媒体流程继续完成并落账，
   后续客户不得开始。
10. C2 ingest 或 sent_ack 暂时失败后点击暂停：保留同一 inflight flow 完成重传；
    Outbox/Journal 未结算前 `API-WORKER-07` 必须拒绝。
11. 真实 PostgreSQL 两个并发 start，以及 start 与 pause 并发：只能保留一个权威 flow，
    不得绕过 paused。

正式接口（下列编号、方法和完整路径是唯一正式名称）：

| 接口编号 | 方法 | 路径 | 用途 |
|---|---|---|---|
| `API-WORKER-01` | POST | `/api/workers/{worker_id}/client-bind` | Worker 客户端绑定 |
| `API-WORKER-02` | POST | `/api/workers/{worker_id}/reset-client-bind` | 后台重置客户端绑定 |
| `API-WORKER-03` | POST | `/api/workers/{worker_id}/run-status` | Worker 开始接单 / 暂停接单 |
| `API-WORKER-04` | GET | `/api/workers/{worker_id}/tasks/pull` | Worker 拉取当前可处理任务 |
| `API-WORKER-05` | POST | `/api/workers/{worker_id}/heartbeat` | Worker 心跳 |
| `API-WORKER-06` | POST | `/api/workers/{worker_id}/inflight-flow/start` | 在具体客户业务流程首次 UI 锁前登记唯一在途流程；仅 running 可调用 |
| `API-WORKER-07` | POST | `/api/workers/{worker_id}/inflight-flow/finish` | 业务终态及 UI 锁释放后清除同一在途流程 |
| `API-TASK-01` | POST | `/api/tasks/{task_id}/claim` | Worker 领取任务 |
| `API-TASK-02` | POST | `/api/tasks/{task_id}/lease/renew` | Worker 续租运行中的任务 |
| `API-TASK-03` | POST | `/api/tasks/{task_id}/step` | Worker 上报步骤 |
| `API-TASK-04` | POST | `/api/tasks/{task_id}/invite-sent` | Worker 上报已发送邀请 |
| `API-TASK-05` | POST | `/api/tasks/{task_id}/already-friend` | Worker 上报已是好友 |
| `API-TASK-06` | POST | `/api/tasks/{task_id}/fail` | Worker 上报失败 |
| `API-TASK-07` | POST | `/api/tasks/{task_id}/evidences` | Worker 上传执行证据 |

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

以上默认值可配置，不作为性能承诺。单会话 C2-C3 Flow 不设置固定总逻辑事务时长；文字读取通常是短动作，多条语音、Vision、Brain 和发送确认属于允许持续推进的长动作。本版全程不锁人工键鼠。系统依靠租约续期、阶段进展、授权复核、停止信号和硬安全 watchdog 判断异常，不能用固定 180 秒打断正常业务流程。

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

#### 3.2.4 当前版本不使用悬浮球和人工键鼠锁

##### 3.2.4.1 当前发布边界

当前版本完全不提供防误触悬浮球和人工键盘鼠标锁定能力。Worker、OmniAuto 和
Sidecar 必须满足：

1. 从当前源码、安装包和运行入口彻底删除 `OperatorGuardService` 及其启动、附着、
   切换、重建、监控和退出代码。
2. 不创建悬浮球窗口，不显示灰/绿/蓝/黄/红状态灯。
3. 不安装键盘或鼠标 Hook，不屏蔽任何人工输入。
4. 不注册或监听 F8 守护快捷键；暂停、继续、停止只使用 Worker 客户端已有按钮和
   运行状态接口。
5. 不读取、写入或门禁 `operator_guard` 控制、状态、心跳和 PID 文件。
6. 不因为 `OPERATOR_GUARD_*` 状态暂停任务、拒绝扫描、终止读取或阻断回复。
7. 删除专用守护适配器、脚本、兼容包装、配置项、环境变量、错误码、状态字段、日志
   事件、单元测试、打包探针、PowerShell 检查和 CI 条件；不得保留 disabled 开关、
   no-op/mock 空实现或“以后可能复用”的死代码。
8. Windows 安装包不得包含守护脚本或相关资源，启动探针不得再检查悬浮球、Hook、
   心跳和守护进程。

悬浮球、防误触 Hook、快捷键接管、身份校验、心跳和故障恢复整体退出当前发布范围，
仅作为 `1.1.x` 条件评估项。后续重新引入前，必须证明该能力可以独立关闭、不会改变主流程
业务状态、不会持有 UI 锁、不会让任务永久 executing，并另行完成 Windows 安全验收。

##### 3.2.4.2 当前仍保留的安全能力

取消人工键鼠锁不等于取消业务安全校验。当前版本继续保留：

- Worker 本地逻辑 UI 锁：只防止 Worker 自己的扫描、读取、加好友和发送动作并行操作
  同一个微信客户端；该锁不会拦截用户鼠标键盘。
- 服务端任务租约、fencing token、取消回调、超时、ActionJournal 和 Outbox。
- 每次不可逆动作前的窗口、目标会话/短码、`authorization_revision`、最终消息顺序和
  `reply_action` 有效性复核。
- `not_attempted` 可安全重排；`trigger_attempted` 按结果未知且禁止自动重放；
  `confirmed` 保留已经完成的物理事实。
- 用户点击 Worker 的暂停或停止后，禁止开始新的微信动作；当前动作在安全步骤边界收到
  取消并按 ActionJournal 收口。恢复后必须取得新 UI 锁并重新确认现场，不续跑旧坐标。

##### 3.2.4.3 人工使用约束和干扰处理

本版明确接受“自动化运行时由操作人员自行不操作微信”的现实约束：

- 点击“开始接单”前，操作人员应打开并登录正确微信；Worker 执行期间不要移动鼠标、
  输入键盘、切换微信会话或遮挡/最小化微信窗口。
- 系统不阻止用户操作电脑，也不承诺用户干扰时继续完成当前动作。
- 检测到窗口、会话、短码、授权或消息顺序与预期不一致时，必须安全取消当前动作、
  保留日志和截图并重新排队或提示重试；不得猜测目标后继续点击或发送。
- 发送等不可逆动作已进入 `trigger_attempted` 后受到人工干扰时，结果按未知收口并禁止
  自动补发，防止重复回复。
- 人工误操作属于可定位的运行干扰，不得造成线程无限等待、逻辑 UI 锁永久占用或任务
  永久 running；这些情况仍属于 P0。

##### 3.2.4.4 当前版本验收要求

- Worker 从启动、开始接单到停止，全流程不得出现悬浮球窗口。
- 全流程人工键盘鼠标始终可用，不得安装或启用输入拦截 Hook。
- 不按 F8、按 F8 或操作其他普通按键，均不得触发 Worker 守护暂停/停止状态。
- 加好友、主动扫描、定向读取、文字/语音/图片、Vision、Brain、自动回复和召回均不得
  依赖守护进程、守护文件或 `OPERATOR_GUARD_*` 门禁。
- 连续运行时进程列表不得出现独立 `rpa_operator_guard` 守护进程；故障证据和日志不得
  再产生悬浮球心跳、身份、重建或故障灯错误。
- 人工切换窗口或会话的干扰用例必须证明动作安全取消、不发错客户、不重复发送，并且
  逻辑 UI 锁和任务可以正常收口。

#### 3.2.5 超时恢复

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

#### 3.2.6 服务端任务租约

`add_friend / chat_reply` 是服务端任务，但两者进入本地 UI 锁的方式不同：

- `add_friend`：先领取任务租约，再获取本地 UI 锁，执行完成后释放。
- 正常 `chat_reply`：由已经持有当前会话逻辑 UI 锁的 C2 Flow 等待 Brain 终态；本版没有物理键鼠守护。回复前重新确认窗口、会话、授权和消息顺序，通过后领取当前会话的 `chat_reply` 任务和发送许可，不另开第二套会话事务。
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

#### 3.2.7 本地多任务优先级

同一台电脑的微信 UI 操作不可并行，也不做中途抢占。高优先级动作只能等待当前锁释放或超时恢复。

本地 UI 队列优先级：

| 优先级 | 动作 | 说明 |
|---:|---|---|
| 100 | `recovery / emergency_stop` | 释放异常锁、停止风险动作。 |
| 95 | `pre_send_refresh` | 发送前短读目标会话，确认 reply_action 未被新客户消息 supersede。 |
| 90 | `session_scan_visible` | 第一屏主动扫描，优先发现当前微信第一屏新消息、短码和未读变化。 |
| 85 | `message_ingest_visible_hit` | 第一屏命中的会话优先读取消息。 |
| 80 | `chat_reply` | 当前 C2 单会话 Flow 内的发送阶段；等待 Brain 时保持原会话的逻辑事务所有权，本版不锁人工键鼠；发送前完成窗口、会话、授权、消息顺序和 pre_send_refresh 复核。独立队列项只用于崩溃恢复。 |
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

#### 3.2.8 各动作接入方式

| 动作 | 是否进入任务中心 | 是否使用服务端任务租约 | 是否使用本地 UI 锁 | 接入方式 |
|---|---|---|---|---|
| `add_friend` | 是 | 是 | 是 | claim `add_friend` 任务 -> 获取本地锁 -> 调用 OmniAuto `add-friend-entry-click-plan-windows` -> 上报结果 -> 释放锁 -> 完成任务。 |
| `session_scan` | 否 | 否 | 视实现而定 | 若扫描会话列表需要切换/读取微信 UI，则获取本地锁；上报 `session_scan_result`；响应 `next_action=none`。 |
| `message_ingest` | 否 | 否 | 视实现而定 | 若读取消息需要打开/切换会话，则获取本地锁；读取已绑定会话消息；按 `dedupe_key` 上报；响应 `next_action=none`。 |
| `chat_reply` | 是 | 是 | 是 | 正常链路：当前 C2 Flow 保持逻辑事务所有权等待 Brain，本版不锁人工键鼠 -> Brain 返回后重认窗口/会话/授权/消息顺序 -> pre_send_refresh -> claim 当前会话 `chat_reply` 和 `reply_action` -> claim-send -> 输入前/点击前再复核 -> 发送 -> 上报 `sent_ack` -> 当前会话终态后释放逻辑锁。恢复链路必须取得新锁并重建上下文，不允许另一套直接发送路径。召回批次还必须先由 `recall_precheck_read` 放行。 |

#### 3.2.9 错误码

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

#### 3.2.10 验收口径

- 同一台 Worker 上同时存在 `add_friend`、`session_scan`、`chat_reply` 时，微信 UI 操作必须串行，不允许并行点击或输入。
- 正常 C2-C3 单会话 Flow 等待 Brain/Vision Provider 期间继续保有当前会话逻辑事务所有权，不处理其他微信会话；本版全程不锁人工键鼠。网络返回后完整复核现场，不能沿用旧坐标。
- 消息 Outbox 未确认或发送 `sent_ack` 未确认时，整个 C2 调度必须保持阻断；自动恢复只能重传既有事实/回执，不能进入新会话，也不能重复执行微信动作。
- C2 `session_scan/message_ingest` 不进入任务中心，但需要操作微信 UI 时必须获取本地锁。
- Worker 崩溃重启后，过期本地锁能自动恢复，不重复发送消息。
- 服务端任务租约过期后，发送类任务不得自动补发。
- 所有锁相关失败必须有错误码、`trace_id`、本地日志和必要截图证据。
- Worker、Sidecar 和内嵌 OmniAuto 源码、测试及安装包内不得再包含悬浮球、人工输入
  Hook、F8 守护、守护进程、守护文件和 `OPERATOR_GUARD_*` 门禁或兼容空壳。
- 从 Worker 启动、开始接单、加好友、扫描、读取、语音、图片、Vision、Brain、发送、
  召回到停止，全流程键盘鼠标始终由用户正常使用，系统不得拦截。
- 人工切换窗口、切换会话或出现新消息时，系统必须在动作前复核失败并取消/重排，不能
  把回复发到错误会话；干扰后不得永久占用逻辑 UI 锁或任务租约。
- Worker 页面暂停/停止仍必须进入现有取消回调；恢复后使用新逻辑 UI 锁并重新确认
  窗口、会话、授权和消息顺序，不能续跑旧坐标、旧 Sidecar 或旧发送步骤。
- `not_attempted / trigger_attempted / confirmed` 仍分别按可重排、未知禁止重放、保留
  已完成事实处理；删除输入守护不得削弱防重复发送和 ActionJournal。

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
| `not_attempted` | 尚未触发本事务的目标不可逆业务动作；仅打开并关闭可判定的右键菜单不算目标动作已触发。 | 只说明目标动作可安全取消或按规则重试，不表示已经形成的 completed/failed 业务事实可以删除。 |
| `trigger_attempted` | 已经触发目标业务动作的物理点击、复制或发送，结果可能已经发生。仅打开并关闭可判定的右键菜单、且未点击目标菜单项，不等于目标业务动作已触发。 | 不得按“肯定没发生”处理；必须继续取证或进入未知结果。 |
| `confirmed` | 已取得足以证明目标动作/结果的执行器返回、UI、剪贴板或服务端证据。 | 可以提交对应 completed/sent 终态；add_friend 的物理点击确认与点击后业务失败仍须分层记录。 |

统一不变量：

1. `failed` 只表示有证据证明目标动作没有发生，或者动作发生后目标结果明确失败。
2. 物理触发已经发生但无法确认结果时，发送必须返回 `unknown`；语音和图片必须形成带
   `*_RESULT_UNKNOWN` 错误码的 failed 事实，不得自动重复昂贵动作。当前 customer 媒体
   失败按 L1 转人工，self 媒体失败按 L0 告警，身份或暂态技术异常按 L2 恢复。
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
9. `action_phase` 只判断动作重放安全，不判断事实是否需要结算。只有语音或图片
   ActionJournal 是纯动作意图，并且不存在 terminal payload、completed/failed Ledger
   和对应 Outbox 时，全部 `not_attempted` 才允许在全局门禁前本地清理。只要已经形成
   completed/failed 事实，无论 action_phase 为何都必须进入统一恢复协议；只要存在
   `trigger_attempted` 或 `confirmed` 也必须进入统一恢复协议，客户端不得自行猜测终态。
   生产端一旦得到 completed/failed 业务结果，必须不受 `action_phase` 分支影响地
   先原子写入 terminal payload，再由可重入投递器幂等推进到 Ledger/Outbox；任一步
   崩溃都从上一个已落盘状态续传。禁止消费者假设 terminal 必然存在，也禁止依靠恢复端
   或测试代码补造生产端没有写出的终态。
10. 恢复顺序固定为 `sent_ack Outbox -> messages Outbox -> media ActionJournal/
    Ledger -> 任务中心恢复 -> 本轮能力预检 -> 新 UI 动作`。Vision 配置缺失不得
    阻断无需 Vision 的历史回执和事实结算。
11. `unbound / binding_failed / needs_review / degraded / paused` 都不是永久
    终止证明。后端仍能证明原事务身份时直接
    `settle_without_ui + fact_only`；身份暂不可证时才 `retry_later`。会话关闭、
    拒绝、可靠确认短码移除也只能停止新 UI，已产生事实仍须结算。
    该规则只处理已符合当前合同的记录；不得把升级前缺少当前必填字段的
    历史记录直接当成当前合同错误并无期限暂停。
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
| Lead/Task业务阶段 | new、assigned、add_friend_blocked、add_friend_pending、add_friend_sent；其中 `add_friend_sent` 是 `completed + invite_sent` 的展示投影，不是 Conversation.status。 |
| Conversation状态 | friend_request_sent、friend_active（仅 already_friend 首读前过渡）、friend_activation_reading、ai_active、waiting_user_reply、recall_precheck、recalled_waiting_user、waiting_sales_reply、sales_replied_waiting_user、rejected、closed。`friend_added` 退出正式枚举。 |
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
- 邀请表单的问候语与备注是同一稳定页面内的原子填写步骤：复用填写前的同一表单业务帧与页面局部定位，不在两项之间重复截图；两项完成后统一截图、OCR 复核，再以该输入后业务帧点击确认。程序复制粘贴的八位字母数字短码只要求在备注字段内出现唯一高置信八位候选，允许 OCR 将 `V/W` 等单字符混淆；低置信或多候选仍零点击失败关闭。
- 主链路不使用 `sales_name` 自动拼申请语，不使用 `remark` 兜底备注名，避免字段来源混乱。
- 加好友业务成功点仍是申请添加朋友页最终“确定”按钮的物理点击函数明确返回成功；微信没有可依赖的后续成功状态，不等待成功页面或成功文案。点击成功且没有可靠识别到明确风控/失败提示时，上报 `task.status=completed` 且 `result_code=invite_sent`。
- `invite_sent` 的确定按钮点击成功后，或搜索结果资料页已经确认 `already_friend` 后，等待约 1—2 秒再执行统一 UI 收尾。只有顶部标题 OCR 精确确认“添加朋友”，或当前 HWND 已由本次添加朋友流程证明且仍存活可截图时，才点击该窗口右上角 X 一次并验证窗口销毁或隐藏。不得根据联系人资料正文、文字数量或“已通过好友”等正文猜测页面；未知窗口零点击。收尾失败不得覆盖已经确认的 `invite_sent/already_friend` 完成事实，不得重新点击确定按钮或重复关窗。

| 状态/异常 | 处理 |
|---|---|
| 执行状态 | 主状态：blocked、pending、running、completed、failed、cancelled；running 内部步骤：searching_contact、contact_found、opening_add_contact、filling_remark、sending_invite。最终“确定”按钮点击函数返回成功后立即退出 running；不设置 `waiting_ui_response`。邀请已发送表达为 `task.status=completed` 且 `result_code=invite_sent`。 |
| 可重试失败 | wechat_not_login、wechat_window_not_found、ui_element_not_found、network_error、worker_interrupted、unknown_error。 |
| 完成结果 | `already_friend` 表示 C1 已确认原本就是好友，任务记为 `task.status=completed` 且 `result_code=already_friend`。它不授权直接读取；必须等待 C2 首屏发现并绑定有效短码，再通过 `friend_acceptance_visible_hit -> activation-confirm -> friend_activation_reading` 执行首读。 |
| 不建议自动重试 | phone_invalid、phone_not_found、customer_privacy_blocked、wechat_rate_limit、operation_too_frequent、account_restricted、blacklist_hit、daily_limit_reached。 |
| 微信风险提示 | 操作频繁、环境异常、添加受限等出现后暂停 add_friend 并上报，不自动连续重试。 |
| 幂等 | 同一 task_id 多次上报成功只记录一次；同手机号不生成多个未完成加好友任务。 |

## 6. 模块5：会话绑定与监听

- 目标：知道微信里这条消息是谁发的、属于哪条线索、当前 AI 能不能回。
- 第一期不读取微信数据库、不破解协议、不使用非公开微信接口、不依赖客户昵称唯一性。
- 绑定优先通过初始备注/短码；已是好友立即尝试绑定；绑定失败不自动回复。
- C2 统一处理客户/我方文字、语音和图片事实；Worker 先用 OmniAuto `messages` 读取/探测消息类型，只有发现未转写语音时才调用 `voice-transcribe`，转写正文必须绑定原语音并按 `message_type=voice` 入库，不能再作为独立 `text` 入库。
- 图片必须先进入最终画面的统一消息槽位并完成事实归属与传输状态分层；只对 `fact_scope=current_read_run + delivery_state=not_enqueued` 的图片执行一次内存剪贴板事务和真实 OmniAuto Vision。不得落本地图片文件、上传车金后端、调用旧图片入口或上报 `pending/discovered` 占位。
- 重复消息由 Worker 稳定来源身份和 `dedupe_key` 初筛，服务端以 `unique(conversation_id, dedupe_key)` 做最终防线；页面坐标、扫描轮次和绝对时间不得作为消息主身份。
- C2 唯一准入条件为：当前会话标题含有效短码、标题同步确认 `conversation_type=private`、服务端 `read-targets` 仍提供当前 `authorization_revision`。群聊和 `unknown` 不进入消息读取。
- 本模块是 OmniAuto 与 C2 的唯一接入边界。Worker 调用 OmniAuto `sessions / messages / voice-transcribe` 能力读取微信事实，服务端负责短码绑定、会话状态、消息去重和是否允许后续 AI 回复。
- 本模块不生成或发送 AI 回复；AI 回复属于 C3，必须在会话绑定和消息入库稳定后执行。
- C2 接口、状态、错误码和验收标准以本模块为准。

### 6.0 OmniAuto结合方式

#### 6.0.0 接口名称与适配边界

C2/C3 详细联调字段、请求响应、枚举、字段所有者及 OmniAuto 到车金后端的映射全部定义在本手册第 6 章；不再维护派生接口合同。接口编号、HTTP 方法、完整路径、字段和业务流程只能在本手册中修改。

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
- `messages/ingest` 只完成 C2 入库，并可在原响应中返回可选 `message_batch`；Worker 按 `batch_id` 保持原会话的逻辑事务所有权等待 Brain 终态。本版不锁人工键鼠，也不新建另一套“上报并问 Brain”接口。

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

#### 6.0.0.1 媒体编排进入条件与权威画面合同

媒体编排不是每次会话读取都必须执行的固定步骤。首次 `messages` 返回后，Worker 必须先根据该次
OmniAuto 权威观察判断是否存在“当前可执行、尚未转写且未由历史终态结算”的语音：

- 不存在时，不得调用语音 `prepare/execute`，不得用 `prepare empty` 充当消息读取结果，也不得为纯文字、
  纯图片或仅含已转写/历史语音的会话增加一次语音 OCR。原 `initial_read` payload、完整 observations、
  消息顺序和读取轮次必须原样继续进入统一对齐；没有图片 UI 动作时直接进入 ingest。
- 存在时，才进入唯一语音编排器逐条 `prepare -> execute`。`prepare` 是并发变化下的第二次目标确认，
  不是首次消息探测器。若 `prepare` 因页面变化返回空，且未执行任何 UI 动作，Worker 不得用空响应覆盖
  原读取结果；必须复核当前帧是否仍与 `initial_read` 等价。若等价帧仍存在原候选，属于 Sidecar 合同异常，
  保留原结果作诊断但禁止 ingest/Brain；若画面确已变化，则取得最新稳定完整帧并标记为 `final_read` 后
  重新仲裁。无法取得时进入身份/画面恢复门禁，禁止携带残缺上下文 ingest 或启动 Brain。
- 每次语音或图片 UI 动作后，动作前画面立即失效；下一媒体动作、ingest 或 Brain 前必须得到同一短码、
  private 会话的最新稳定完整画面并完成统一序列对齐。不得只返回被操作媒体的局部结果。

`evidence.authoritative_frame_source` 的所有者为 Worker，但 Worker 只能依据 OmniAuto 返回的真实帧和
`ui_action_performed` 证据从以下枚举中选择；不得由任一模块增加同义值：

| 值 | 唯一含义 | 使用条件 |
|---|---|---|
| `initial_read` | 本轮首次完整 `messages` 权威画面 | 首次读取后没有发生任何会使聊天画面失效的语音/图片 UI 动作；允许直接复用原 payload，禁止机械补读。 |
| `final_read` | `initial_read` 失效后的最新稳定完整画面 | 本轮发生过语音/图片 UI 动作，或并发页面变化使初始画面不能原样复用时使用；必须在 ingest/Brain 前完成标题、private、短码、完整 observations 和统一顺序复核。语音成功后的画面也统一使用此值，禁止 `voice_execute_final`。 |
| `action_journal_recovery` | 不依赖当前微信画面、仅由已落盘动作事实恢复的结算证据 | 只允许事务恢复/事实补录；若恢复流程重新读取或操作了当前微信画面，必须按实际情况使用 `initial_read` 或 `final_read`。 |

OmniAuto 生产帧、观察和是否执行 UI 动作；Worker 选择并校验上述枚举、保持原 payload 或构建最终 payload；
后端只校验枚举及其与动作证据的逻辑一致性，不重做 OCR 或自行推断来源。Sidecar、Worker、后端 schema、
机器合同和测试必须使用相同枚举。未知值、字段缺失、发生媒体 UI 动作却仍上报 `initial_read`，或未执行
UI 动作却用空 `prepare` 覆盖原读取结果，统一按 `C2_AUTHORITATIVE_FRAME_SOURCE_INVALID` 失败关闭。

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

禁止使用滚动全量扫描作为兜底；它不是设计、接口参数、配置项或验收路径。

#### 6.0.1.1 第一屏主动扫描

第一屏主动扫描是 Worker 日常最高频 C2 动作，模拟销售先看微信当前第一屏最新消息的习惯。

第一屏主动扫描只做轻量事实发现：

| 对象 | 说明 | 后续动作 |
|---|---|---|
| 当前第一屏疑似未读/红点会话 | 最新消息通常会被微信顶到第一屏。 | 上报 `unread_hint` 并加入 `visible_hit_queue`；后端满足本节门禁时返回 `read_reason=visible_unread` 的 `read-target`。 |
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
- `unread_hint` 只是申请读取许可的观察事实，不是 Worker 的自授权，也不是“必须回复”的业务结论。
- 后端必须在处理本次 `scan-result` 后立即重算 `read_reason`；若已有效绑定、当前 Worker 归属和监听授权都有效，且 `conversation.status=ai_active` 、`unread_hint=true`，必须返回 `visible_unread` 读取目标，不得再要求会话预先具有“等待客户回复/召回”状态。
- 无有效短码、短码多义、绑定冲突、非当前 Worker、监听暂停/禁用、会话已关闭/拒绝或授权版本无效时，不得生成 `visible_unread` 许可。

#### 6.0.1.2 第一屏命中优先读取

第一屏主动扫描发现的命中会话进入 Worker 本地 `visible_hit_queue`。

读取顺序：

```text
第一屏扫描
-> 上报scan-result
-> 后端对满足门禁的首屏未读签发read_reason=visible_unread
-> 拉取并复核read-targets + authorization_revision
-> 当前首屏唯一命中时进入visible_hit_queue并走快速定位
-> 当前首屏未命中时进入state_target_queue并按remark_code搜索
-> 打开会话并同步确认有效短码 + private
-> OmniAuto messages读取
-> 生成dedupe_key
-> 上报/messages/ingest
-> 服务端去重入库
```

`visible_hit_queue` 处理规则：

| 规则 | 说明 |
|---|---|
| 授权后优先读取 | 第一屏命中优先于普通状态机定向读取，但短码命中本身不是读取授权。 |
| 授权与定位分离 | `visible_unread` 由首屏事实触发，但后端签发后就是当前读取授权；`visible_hit/local_unread_hint` 只能选择快速定位路径，不得否决授权。首屏未命中或红点已不可见时，Worker 不得静默丢弃目标，应交给状态目标队列按正式短码搜索。 |
| 首次未读闭环 | 会话没有“等待客户回复/召回”等既有状态时，只要当前首屏扫描事实满足 `visible_unread` 门禁，后端也必须签发一次可重试的读取许可，不能让“未读事实”和“等待 read-target”互相卡住。 |
| 批量上限 | 每轮最多读取配置化数量，第一期建议 3-5 个，避免长期占用微信。 |
| 去重 | 同一轮按 `conversation_id + remark_code` 身份键去重；`remark_code` 是非第一屏微信搜索定位主锚点，`rpa_session_key / display_name / row_fingerprint` 只用于第一屏快速定位和排查证据。 |
| 失败处理 | 找不到目标、OCR低置信、类型为 group/unknown、微信异常或授权已变化时记录错误码，不乱滚、不乱点。 |

#### 6.0.1.3 状态机驱动定向读取

状态机定向读取用于处理非第一屏的已知客户。服务端根据会话状态、时间字段或仍有效的
`visible_unread` 事实返回 `read-targets`，Worker 空闲后逐个定向读取。`visible_unread`
在当前首屏未命中时与其他已授权状态目标走同一套合同校验、冷却和短码搜索流程。

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
| `friend_acceptance_visible_hit` | `invite_sent` 后好友通过，或 C1 返回 `already_friend`后的首次受控读取 | 仅在有效短码会话最近可见、已绑定且未完成首读时使用；不要求 `unread_hint=true`。 |
| `visible_unread` | 首屏扫描发现已绑定 `ai_active` 会话当前未读 | 为无其他业务状态的首次未读签发临时读取许可；不改写会话主状态。 |
| `recent_ai_sent` | AI 刚发送过回复 | 客户可能马上回复，短期提高读取优先级。 |
| `waiting_user_reply` | 我方已回复，等待客户回 | 定期读取确认客户是否回复。 |
| `waiting_sales_reply` | 已转人工，等待销售回复 | 监听销售手机端回复是否同步到桌面端。 |
| `recall_precheck` | 召回到期前确认 | 召回前必须读取一次，确认客户没有新消息。 |

`friend_acceptance_visible_hit` 的后端判定顺序固定为：

```text
1. friend_state/status=friend_request_sent，且该绑定在有效时间内被首屏看到
   -> 返回friend_acceptance_visible_hit
2. friend_state/status=friend_active，且来源为already_friend首读过渡，该绑定在有效时间内被首屏看到
   -> 返回friend_acceptance_visible_hit
3. conversation.status=friend_activation_reading
   -> 继续返回friend_acceptance_visible_hit，直到首读完整入库/结算
```

上述判定优先于 `visible_unread`。`unread_hint` 只用于已进入 `ai_active` 后、没有其他业务读取原因的当前首屏未读；不得用它取代好友首次激活读取。

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
10. 召回到期先执行 recall_precheck_read，确认无新客户消息后创建召回 batch；同样保持该会话的逻辑事务所有权等待 Brain 与发送终态，本版不使用物理键鼠守护。
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

以上是调度建议，不作为性能承诺。10—20 秒只适用于普通扫描，不适用于仍在正常推进的语音 flow。

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
12. 首次messages读取；只有权威观察发现当前可执行且未结算的未转写语音时，才在同一flow执行唯一语音编排器。无未转写语音时零调用 `voice-transcribe` 并原样保留 `initial_read`；发生任何媒体 UI 动作后才必须取得最新稳定完整 `final_read` 并复核目标。
13. Worker构建contract_version=3消息并携带authorization_revision，上报 /wechat/messages/ingest。
14. 服务端先复核授权版本，再按dedupe_key幂等入库，返回ingest_result。
15. 若服务端没有返回需要等待的message_batch，或batch已进入no_action/handoff/failed等终态，Worker释放Local WeChat UI Lock。
16. 若返回处理中batch_id，Worker保持当前会话和UI锁，只等待该batch；Brain终态为send_reply时在原会话执行pre_send_refresh、领取chat_reply和发送许可、输入前/点击前消息序列复核、发送及sent_ack。
17. 当前会话到达可靠终态后统一释放UI锁，记录各阶段耗时和结构化证据，再处理下一个会话。
```

如果当前已经有微信 UI 动作在执行，主动扫描等待当前动作完成；不做中途抢占。

#### 6.0.2.3 服务端 read-targets 选择规则

服务端 `/wechat/sessions/read-targets` 只返回允许读取的已绑定会话。

`read-targets` 必须同时支持两类授权来源：

1. 长期业务状态驱动，例如等待客户回复、等待销售回复和召回前复核。
2. 当前首屏事实驱动，即已绑定的 `ai_active` 会话在最新成功扫描中为 `unread_hint=true`。此时使用临时 `read_reason=visible_unread`，不改写 `conversation.status`。

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

`visible_unread` 还必须同时满足：

```text
最新成功scan-result对该绑定上报unread_hint=true
本轮只有一个有效remark_code且不存在同码多会话冲突
conversation.status=ai_active
会话不属于closed/rejected，且没有更高安全优先级的recall_precheck或好友通过后首读状态
```

其生命周期固定为：

```text
首屏未读观察上报
-> 后端根据稳定语义证据建立 unread_generation=N
-> 后端生成读取票，并在票上冻结N（无待处理代次则为0）
-> Worker按授权交集读取；授权复核只验证票仍有效，不把票上N替换成最新值
-> Worker在入库时回传读取票冻结的N
-> 后端确认本次active_read完整入库/结算：仅消费N
-> 后续扫描仍是同一红点/预览证据：仍属于N，不清空冷却、不提前派发
-> 能证明新未读事实时建立N+1；或到next_read_due_at后允许低频复核
```

定位失败、窗口不可控、类型无法确认、读取失败或入库未被后端确认时，不得消费当前 `unread_generation`；保留证据并按现有冷却/重试规则处理。扫描中的 `unread_hint` 是物理画面电平，`unread_generation` 是后端持久化的业务事件代次，两者不得继续共用一个布尔状态。

长期状态不能等于“每轮都要打开微信”。后端必须区分“这个会话仍需监听”和“现在已经
到下一次读取时间”。一次完整 `messages/ingest` 即使 `messages=[]` 或全部返回
`duplicated`，只要授权匹配且读取流程完整，就必须结算本轮读取完成并更新：

```text
last_read_completed_at = 本轮完成时间
last_read_result = new_facts / no_change / failed
no_change_read_count = 连续无变化次数
next_read_due_at = 下次允许定向读取时间
```

读取失败不增加“已读无变化”次数，仍按失败退避安全重试。读取成功且没有新事实时，
服务端退避固定为：第 1 次 2 分钟、第 2 次 5 分钟、第 3 次及以后 10 分钟。读取成功且
存在新事实时，也必须写入默认不少于 2 分钟的 `next_read_due_at`，不能清成 `null` 让
30 秒扫描立即重新派发。只有下列带有“完成时间之后的新证据”的事件可以提前唤醒：

```text
后端建立了高于已消费代次的新 unread_generation
首次好友激活待首读
conversation.status 或授权版本发生有效变化
召回到期进入 recall_precheck
上一批读取/Brain明确签发同会话continuation_token
```

`unread_generation` 只能由后端对同一绑定单调分配。新代次必须至少有一项可证明的新事实：规范化消息预览/预览时间发生稳定变化；OmniAuto 获得微信提供的稳定消息观察 ID；后端收到新的消息或业务事件；或成功扫描曾明确观察为 `unread_hint=false`，之后再观察为 `true`。单纯“红点仍亮”、新 `scan_id`、OCR 置信度波动、会话行坐标/顺序变化或红点边框变化不得创建新代次。`row_fingerprint` 包含位置证据，禁止将其整体作为未读事件版本。

当无法仅凭侧栏证据区分“客户连续发了完全相同的消息”与“原红点未消失”时，不得猜测新代次来绕过冷却；到 `next_read_due_at` 后仍必须允许低频完整复核，因此不会永久漏读。读取N执行期间如已建立N+1，N的完成结算只能消费N，N+1必须保持待处理。

被动首屏扫描仍是只读操作，禁止为了消除红点而点击当前会话。完整读取已获后端结算且仍持有本会话 UI 锁时，Worker 可在再次确认 `private + 目标短码一致 + 唯一会话行` 后，最多尝试一次“点击当前行消红点”。该动作只是最佳努力的 UI 收尾：失败、红点未消失或无法唯一确认时直接跳过，不得改写已消费代次、不得重读、搜索、转写、Vision、Brain 或创建新任务。

`waiting_sales_reply` 仍需低频轮询，因为销售从同一微信账号发出的消息不一定产生未读
红点；它同样遵守 2/5/10 分钟退避，不能每 30 秒切换会话。`waiting_user_reply` 优先由
首屏未读事实立即唤醒，无未读变化时也按退避低频复核。Worker 的本地成功冷却只是
防止同一进程瞬时重复点击，不能代替后端的 `next_read_due_at` 调度门禁。首屏
`visible_hit_queue`、定向 `read-targets` 和恢复队列必须调用同一个准入函数，同时检查
本地成功冷却、本地失败冷却和后端 `next_read_due_at`；禁止首屏只检查失败冷却。完成
一次读取后，这三条入口看到的冷却结果必须完全一致。

服务端先按 `last_read_dispatched_at` 从旧到新公平轮询，再以
`read_reason` 优先级和最近可见时间作为同轮排序依据。每次下发目标后必须持久化
派发时间；长期状态目标只有 `next_read_due_at <= 当前时间` 才能进入候选，单纯更新
`last_read_dispatched_at` 不能视为读取完成。超过 20 个有效目标时，后续轮次必须覆盖
上轮未返回目标，禁止固定截断导致永久饥饿。长动作的授权复核使用单会话轻量授权
接口，不受本轮 20 条发现窗口影响。

业务优先级：

| 优先级 | read_reason | 说明 |
|---|---|---|
| 1 | `recall_precheck` / `friend_acceptance_visible_hit` | 安全前置复核，或好友通过后的首次受控读取；原业务状态优先于普通未读事实。 |
| 2 | `visible_unread` | 最新首屏扫描证明已绑定 `ai_active` 会话当前未读；用于闭环第一条客户消息和无其他待处理状态的新消息。 |
| 3 | `recent_ai_sent` | AI 刚发送过，正在等待客户回复。 |
| 4 | `waiting_user_reply` | 我方已回复，等待客户回。 |
| 5 | `waiting_sales_reply` | 已转人工，监听销售是否回复。 |

服务端不得返回：

```text
bind_status != bound 的会话
remark_code为空的会话
listen_status=disabled/paused/error 且尚未满足各自恢复条件的会话
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
| `read-targets=[]` | 允许继续上报第一屏扫描事实；清空本地命中读取队列，不点击会话、不读取、不转写、不入库。但后端必须已正确计算 `visible_unread`；对满足门禁的首屏未读仍返回空列表属于后端调度错误，不得由 Worker 绕过授权补救。 |
| `authorization_revision` 变化 | 当前读取许可失效；停止开始后续动作，旧请求由后端返回 409，不得绕过授权重试入库。 |

主动扫描只能上报事实，不能自己补发消息、不能自己创建 `reply_action`、不能直接改变 `conversation.status`。

### 6.0.3 C2消息去重入库工程规则

Worker 负责生成稳定 `source_message_key` 和候选 `dedupe_key`，服务端负责授权复核和最终去重。后续可以在 Worker 本地按 `conversation_id` 维护最近已上报的 `dedupe_key/content_hash` 小缓存减少重复请求，但该缓存只能是前置优化，不能取消服务端数据库唯一约束。

#### 6.0.2.5 本地身份丢失后的服务端恢复

Windows 本地 `next_sequence` 不是消息身份的唯一事实源。`API-C2-02` 和
`API-C2-03` 必须为每个会话返回 `identity_checkpoint`：

| 字段 | 说明 |
|---|---|
| `version` | 身份检查点版本。 |
| `next_sequence_floor` | 后端从历史 `worker-message-*` 计算出的下一编号下限；Worker 本地只能取更大值，禁止回到 1。 |
| `recent_messages[]` | 最近消息身份；必须包含 Worker 序号消息，不得再排除。 |
| `recent_messages[].stable_id` | 历史 Worker 稳定编号。 |
| `recent_messages[].source_message_key / dedupe_key` | 已入库的正式身份键。 |
| `recent_messages[].sender_role / message_type` | 历史发送方和类型。 |
| `recent_messages[].normalized_content_hash` | 规范化正文摘要，不返回不必要的完整敏感正文。 |
| `recent_messages[].alignment_signature` | 用于把当前可见消息序列与历史连续片段对齐的结构签名。 |

`normalized_content_hash` 的正文规范化由 Worker 与后端共用同一机器合同。微信气泡的中文、
日文、韩文或标点旁出现 OCR 视觉换行时，换行不属于客户正文，必须在计算身份摘要前删除；
普通水平空白和英文单词间换行仍折叠为一个空格。该规则不得执行 NFKC 或改写全角标点，
所有未包含视觉换行的 `0.9.8` 历史正文必须保持原哈希，升级不得要求清理 Ledger、Outbox
或后端身份检查点。

Worker 每次读取目标时固定执行：

```text
读取本地身份状态
-> 合并服务端identity_checkpoint
-> next_sequence取本地值和服务端下限中的较大值
-> 用最近消息身份恢复当前画面中的历史消息编号
-> 只给无法匹配的新消息分配新编号
-> 在同一个SQLite事务中保存新编号和新next_sequence
-> 再构建Outbox并上报
```

本地数据库缺失、版本过旧或被清理都只能触发“从服务端恢复”，不得默认从 1 开始。
同一会话的编号分配必须受单写者锁和 SQLite 事务保护；并发线程不能各自读取同一个
旧 `next_sequence`。历史画面无法唯一对齐时返回
`MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS`，不猜编号、不启动 Brain。

如果后端仍发现相同 `dedupe_key` 对应的角色、类型、规范化正文或媒体稳定锚点不一致，
返回 `409 + MESSAGE_IDENTITY_COLLISION + recovery_action=refresh_identity_and_retry`。
Worker 必须保留已经读取完成的原 Outbox，刷新检查点，为冲突项分配高于服务端下限的
新编号后只重建身份外壳并重传；不得重新打开微信、重新转写语音或重新调用 Vision。

#### 6.0.3.0 动作前后统一消息序列对齐

本节是 C2 文字、语音、图片和 AI 发送回执身份编排的唯一权威模型。MECE 在这里的含义是：
每个运行时对象在任一时刻只能属于下表一种类型，所有缺失、空白、未知或矛盾组合都有唯一失败出口；
禁止再用多个局部状态字段分别推断“它是不是正式消息”。

| 对象类型 | 唯一含义 | 允许持久化内容 | 是否可生成正式 source key / 查询 Ledger、Outbox / 入库 / 进入 Brain |
|---|---|---|---|
| 帧内观察 `frame_observation` | OmniAuto 在一张有效画面中看到的文字、语音、图片或系统行；只有 observation、顺序、角色、类型和帧内定位证据 | 当前帧证据；不进入正式身份目录 | 否 |
| 待处理媒体动作 `pending_media_action` | Worker 已唯一选中一条新语音或图片、分配不可复用预留号并原子写入 ActionJournal，但尚未完成身份提交 | action ID、reserved ID、动作前五字段完整序列、本轮临时动作槽位、当前帧 bounds、动作阶段 | 否 |
| 正式消息 `committed_message` | 已由允许的提交依据唯一证明并进入 Worker 正式身份目录的消息 | `worker_stable_id + commit_basis + message_type + sender_role` 及必要证据 | 是 |
| 隔离记录 `quarantine_record` | UI 动作可能发生，但无法唯一证明操作对象或动作结果归属；保留审计证据，不代表业务消息 | 原 action、reserved ID、已保存帧、错误码和 `technical_failed` 结算证据；不得记录或触发 handoff | 否 |

`frame_observation` 没有正式 Worker 身份。`pending_media_action` 的
`reserved_worker_stable_id` 只是不可复用的预留号，不是消息身份。
`quarantine_record` 即使包含预留号也不得进入正式身份目录。
只有 `committed_message` 才能被任何正式消费者使用。

正式消息的 `commit_basis` 只允许：

| 消息来源 | 允许的提交依据 |
|---|---|
| 已存在历史消息 | 后端或本地已确认 checkpoint 与当前完整序列唯一对齐；复用原 `worker_stable_id` |
| 新文字/系统消息 | 唯一对齐后位于完整 `new_suffix`，角色与类型可信；直接分配并提交新 `worker_stable_id` |
| 新语音 | 本次唯一 prepare/execute 动作的 confirmed 映射，或身份已唯一的 failed 动作映射；正文成功与否不改变“操作对象必须唯一” |
| 新图片 | 本次唯一图片动作的已落盘 confirmed receipt，严格证明 action、reserved ID、菜单/点击/剪贴板代次和实际复制图片字节 SHA-256；气泡截图指纹和 Vision 成功本身都不能提交身份 |
| AI 已发送文字 | 已确认 `sent_ack` 与当前 self 文字气泡的唯一发送回执映射 |
| 微信真实原生消息 ID | 原生 ID 与会话、角色、类型合同一致；OCR/坐标生成 ID 不属于此项 |

除此以外没有兼容提交依据。正文相同、时长相同、坐标接近、同类序号、单侧文字、
anchor、`frame_visual_id`、`canonical_visual_id/canonical_input_id`、Vision 成功或“当前只有一个候选”
均不能单独或组合把观察/预留号升级为正式消息。

文字、语音和图片共用同一个“当前会话消息序列编排器”，但业务连续性和物理点击必须分开。
全窗口截图可一次性裁剪标题、消息视口和输入区 ROI；业务连续性只比较五字段业务投影，整屏像素、
气泡像素、量化坐标、旧 observation ID、邻居和视觉指纹都不能充当跨帧消息身份证。Sidecar 只在
最新单帧内解析物理消息行并使用该帧 bounds 操作；Worker 只批准当前会话、当前最新完整画面中的
一条媒体动作，并在实际动作结果回传后提交长期身份。侧栏排序、其他客户红点、工具栏、光标以及
当前目标外像素变化必须忽略。

当前会话新增文字、语音或图片造成气泡上移或滚动时，旧帧所有点击坐标立即失效，但不因此宣布
业务消息消失或被替换。Worker 必须使用最新完整画面继续固定编排：语音优先、图片随后；同类型按
最新 `screen_order` 从大到小一次只处理一条。Sidecar 不把动作前对象跨帧投影成旧对象，而是在执行时
重新截图并只在该帧内定位当前获批类型的一个物理行。相同正文、相同时长或相似图片既不能继承旧身份，
也不能仅因外观相同被判歧义；实际转写正文或实际复制图片字节及完整动作回执才是提交正式身份的依据。

媒体动作的终态必须恰好是以下四种之一：

| 终态 | 前提 | 后续处理 |
|---|---|---|
| `cancelled_before_trigger` | 明确未发生任何媒体 UI 触发 | 预留号烧毁，不形成消息事实；从最新画面重新 prepare |
| `committed_completed` | 操作对象唯一且内容处理成功 | 提交正式身份，形成 completed 事实 |
| `committed_failed` | 操作对象、动作类型和回执唯一，已得到实际媒体结果证据，但微信或批准 Provider 明确内容处理失败 | 提交正式身份，形成 failed 事实；customer 按 L1 handoff，self 只告警。点击/菜单/剪贴板/合同不变量失败不属于本终态 |
| `identity_unresolved` | 已触发或可能已触发，但程序未能唯一证明操作结果，或发现错对象/多结果/回执矛盾 | 不形成 completed/failed 消息，不重复 UI 动作；立即进入技术故障收口 |

不存在长期 `pending/deferred/not_attempted` 第五终态。当前版本新产生的 `identity_unresolved` 是
客户端代码不变量失败，不是客户业务状态，也不进入“多读几次后转人工”的恢复流程。唯一收口为：
保存本次截图、OCR、ActionJournal、动作回执和具体错误；烧毁预留号；零正式消息、零 Ledger/Outbox、
零 ingest、零 Brain、零 HandoffEvent、零飞书；将当前 task/Flow 结算为 `technical_failed`，释放 UI 锁，
并将 Worker 置为 `faulted + can_pull_tasks=false`。只有动作对象和回执已唯一确认、但微信/Provider 明确
返回转写、复制、Vision 等内容处理失败时，才是 `committed_failed`，继续使用 customer 转人工、self 告警的现有业务规则。

#### 跨版本历史媒体记录的有限恢复

上述四终态是当前合同内的运行时状态机。当本地 SQLite/ActionJournal 来自旧灰度版本，
且缺少当前必填的 `worker_stable_id`、提交回执或序列证据时，必须先进入独立
`legacy_media_recovery` 分类，不得直接进入当前合同提交门。该分类只运行一次并持久化结果，
禁止每个心跳重新猜测。

| 旧记录可证明状态 | 唯一处理 | 恢复后的全局状态 |
|---|---|---|
| 明确未触发媒体 UI，且无 terminal/Ledger/Outbox | 写入 `legacy_cancelled_before_trigger`，烧毁旧预留号并归档；不生成消息 | 清除旧 Flow，继续拉单 |
| 已形成媒体事实，且旧 checkpoint/receipt 能唯一证明原正式身份和顺序 | 使用已证明的原身份执行一次幂等迁移；不得因字段缺失新编或猜测序号 | 后端逐条确认后归档旧记录、清除旧 Flow |
| 能唯一确定 conversation，但无法证明消息序号或归属 | 零消息入库、零 Brain、零 UI；以 `worker_id + conversation_id + legacy_record_digest` 创建一次幂等 `LEGACY_MEDIA_IDENTITY_UNRESOLVED` handoff/技术终态 | 后端确认后归档旧记录，当前客户转人工，清除旧 Flow，其他短码继续 |
| 连 conversation 也无法唯一确定 | 零消息、零 UI；生成一次幂等 Worker 级 `LEGACY_MEDIA_OWNER_UNKNOWN` 事故并归档该媒体记录，不得伪造客户 handoff | 清除旧媒体 Flow，恢复其他可确认客户的工作；后台持续显示待人工审核事故 |

后端临时不可用时，上述已持久化的决定按退避重试，不修改为普通暂停、不重复操作微信；
后端确认终态前不拉取新工作，确认后自动释放旧 Flow。只有“消息可能已发送但结果无法确认”
继续使用原发送硬门禁；历史语音/图片记录不得因新序号缺失永久锁死整个 Worker。

运行时只允许一个正式身份提交门（函数名可调整，语义固定为
`commit_message_identity`）。文字、语音、图片、AI 回执、普通读取、媒体续行、Outbox 恢复和
ActionJournal 恢复均必须调用该门；Ledger 查询、正式 source key 构建、Outbox 写入、V3 message
构建和 Brain 准入只能接收其返回的 `committed_message`。禁止消费者自行读取
`identity_state / identity_phase / _worker_identity_scope` 后重复判断或放宽。

现有字段按以下方式降级为各自单一维度，不能互相代用：

| 现有字段 | 唯一用途 |
|---|---|
| `identity_state=committed/selected_action/frame_local_unselected` | 只表示动作前序列中的帧内对齐角色，不表示持久化生命周期 |
| `identity_phase=historical_restored/sequence_reserved/business_committed/identity_quarantined` | 只表示语音 ActionJournal 的动作内阶段，不是跨媒体正式身份枚举；正式准入必须转换为上述对象类型后再消费 |
| `_worker_identity_scope` | `0.9.20` 过渡期内部字段；缺失、空白、未知和非 `committed` 一律不是正式身份，不得由消费者直接判定 |
| `fact_scope / item_state / delivery_state` | 只在正式消息提交后分别表示读取轮次、内容结果和传输进度；不得反向证明身份正式 |

当前合同新产生的记录必须显式覆盖下列非法输入：字段缺失、空字符串、未知枚举、合法字段之间矛盾、正式 ID 无提交依据、
提交依据指向不同 observation/action/reserved ID、动作已触发但没有终态、隔离记录携带正式消息字段。
这些输入统一在任何 Ledger/source key/Outbox/ingest/Brain 消费前失败关闭；不得采用“只拒绝已知坏值、
其他默认放行”的黑名单写法。升级前历史记录不得进入该分支，必须先由
`legacy_media_recovery` 完成版本识别和有限终结。

实现和验收必须使用同一张消费者矩阵，不得继续按现场案例逐个补丁：

| 输入对象 | source key | Ledger 查询/写入 | Outbox/V3 message | Brain | 唯一处理 |
|---|---:|---:|---:|---:|---|
| `frame_observation` | 禁止 | 禁止 | 禁止 | 禁止 | 对齐、分类或建立媒体动作 |
| `pending_media_action` | 禁止 | 禁止 | 禁止 | 禁止 | 执行一次动作并取得四种终态之一 |
| `committed_message` | 允许 | 允许 | 允许 | 仍需通过业务门禁 | 按 commit basis、角色、顺序和传输状态处理 |
| `quarantine_record` | 禁止 | 禁止 | 禁止 | 禁止 | 仅保存故障证据；当前版本新记录按 `technical_failed + Worker faulted` 终结，禁止转人工掩盖代码缺陷 |
| 缺失/空白/未知/矛盾 | 禁止 | 禁止 | 禁止 | 禁止 | 结构化合同错误，失败关闭 |

自动化必须对文字、语音、图片、AI 回执以及普通读取、媒体后续读、continuation、
ActionJournal 恢复、Outbox 恢复逐一执行上述矩阵。至少覆盖：

1. 所有允许 commit basis 的正向提交，以及每个字段缺失、空白、未知、互相矛盾的反向拒绝；
2. 新语音/图片即使拥有 reserved ID、属于 `new_suffix`、内容处理成功，也不能在 action receipt 前访问任何正式消费者；
3. 语音和图片使用同一消费者白名单，不允许某一媒体只检查“ID 非空”；
4. 四种媒体终态恰好覆盖全部 action；已触发但无终态必须进入 `identity_unresolved`，不能伪造 failed 消息；同时必须断言 task/Flow=`technical_failed`、Worker=`faulted`、零 Handoff、零飞书、零重复 UI；
5. 崩溃发生在预留前、预留后未触发、触发后未回执、回执后未写 Outbox、Outbox 后未获后端确认的五个边界；
6. 静态门禁保证 Ledger/source key/Outbox/V3 builder/Brain 入口不能直接读取旧身份状态字段放行，必须依赖唯一正式提交门的类型化返回值；
7. 全流程正向回归必须包含纯文字、单语音、单图片、文字+语音+图片、媒体动作期间新增同文文字、连续同类型媒体、页面滚动和崩溃恢复，不能只跑安全反例。
8. 发布前必须直接复制上一个已发布灰度版本的原始 SQLite 和 ActionJournal，不允许测试代码重新构造成当前格式。至少覆盖：无序号但明确未触发、无序号且已形成事实、客户可归属但身份不可证、客户不可归属、结算前断网、结算后崩溃重启。每个分支都必须证明零重复 UI、零伪造消息、幂等终结和最终恢复 `/tasks/pull`。

文字、语音和图片必须共用一套“动作前已确认序列 -> 动作后最新观察序列”对齐器。
禁止语音、图片、文字分别维护三套身份恢复算法。

每次可能改变聊天画面的 UI 动作前，Worker 必须在本地事务中保存
`pre_action_identity_sequence`；动作后只使用最新有效帧的 `post_action_observation_sequence`。
两个序列都按微信时间顺序从上到下排列，先排除时间标签、菜单文字和其他非业务噪声。
语音转写后展开的正文行不是新消息；必须先通过本次 confirmed action 映射折叠回原语音
`selected_action`，再执行“业务时间线中的新消息只能追加在尾部、可见视口允许正常头部滑出”的对齐规则。菜单、转写衍生行或其他 UI 衍生物
不得进入 `new_suffix`。

`pre_action_identity_sequence[]` 必须包含动作前帧内的全部业务观察，不能只保存已经编号的消息。
否则，本帧已存在但尚未处理的语音/图片，可能在动作后被误当成新增消息。每项至少包含：

| 字段 | 说明 |
|---|---|
| `identity_state` | `committed / selected_action / frame_local_unselected`；仅表示本动作帧序列角色，不是正式身份生命周期 |
| `worker_stable_id` | 仅 `committed` 必填；其他状态不得伪造正式身份 |
| `canonical_voice_action_id / reserved_worker_stable_id` | 仅本次 `selected_action` 可填；不是已提交身份 |
| `sender_role / message_type` | 角色与业务类型 |
| `normalized_content_hash` | 文本/转写正文摘要；只作兼容证据，不是身份 |
| `native_source_message_id` | 只有微信真实提供稳定原生 ID 时才作强锚点 |
| `frame_visual_id` | 可包含坐标，只用于本帧点击确认和排障；不是跨轮身份 |
| `pre_observation_id / pre_sequence_index` | 动作前帧内观察与顺序位置；不进入 source key |

以下正式对齐算法只用于跨读取轮次恢复已提交历史身份、动作后根据正式回执提交预留号，以及
判断 `new_suffix`；不得直接作为新媒体第一次物理动作的点击前准入条件。第一次动作准入只使用
6.0.4.3.1 的 `frame_action_binding`，真实 Win32 OCR 没有原生消息 ID 时仍是正式支持路径。

正式对齐算法固定为：

```text
1. 只用 native_source_message_id / 本次已 confirmed 的 action 映射建立强锚点；`frame_visual_id` 及历史 `canonical_visual_id/canonical_input_id` 不得参与跨轮对齐或矛盾判定。
2. 对剩余项枚举保持时间顺序的单调、一对一对齐。
3. 兼容候选必须sender_role相同、message_type兼容；文字再要求normalized_content_hash相同。
4. 没有真实 `native_source_message_id`、也没有本次 confirmed action 映射的历史语音/图片，不得只凭角色、类型、顺序或单侧文字继承 `worker_stable_id`。
5. 本轮刚分配但尚未结算的语音/图片属于 `pending_media_action`，只能携带不可复用预留号；不得用该号查询跨轮 Ledger/Outbox，也不得因命中历史终态跳过本次动作。
6. 已确认的语音/图片 action receipt 只能证明“本次实际动作结果”可以绑定本 action 的预留号，不得使用坐标、行号、正文、时长或气泡截图跨帧回挂。图片的正式结果证据必须是实际复制图片字节 SHA-256 与菜单/点击/剪贴板代次回执；气泡/ROI 图像指纹只能诊断。
7. 已确认 `recent_ai_sent` 的 AI 气泡可作为“最新未回复尾部”边界；它只证明边界之后的新消息，不得顺带为边界上方缺乏证据的历史弱媒体恢复身份。
8. 弱身份媒体只有在同一最新完整稳定画面中，被前后两个已唯一对齐的已提交历史上下文夹住，且中间连续业务观察无缺失时，才可继承原编号。历史文字/system 上下文可以通过其已提交 `worker_stable_id`、角色、类型、规范化正文和全序列单调唯一对齐成为两侧边界，不要求微信额外提供原生消息 ID；但单个文字、单侧边界或仅正文相同均不充分。
9. 旧媒体本帧消失、同角色同类型新媒体占据原尾部位置时，必须 `ambiguous/unresolved`；禁止继承旧 ID、生成旧 source key、按历史 Ledger 跳过或进入 Brain。
10. 微信消息只能在已有序列尾部追加。新业务消息不得插入两条已确认历史消息之间。
11. 同一个有效对齐片段内，两个已匹配历史项之间不允许遗漏未匹配的业务观察。
12. 只有对齐已消费 pre_action 序列的最后一条业务观察（包括 `frame_local_unselected`），`old_tail_fully_consumed=true`，才能把 post 序列中其后未匹配的连续尾部判定为 `new_suffix`。
13. post序列在对齐片段之前的未匹配项只能是新暴露的历史区间，必须用checkpoint恢复或进入unknown，不得分配新编号。
14. 两个或以上对齐结果同时满足上述规则时，alignment_status=ambiguous；禁止“取第一个”或“取坐标最近”。
   零个合法对齐时 `alignment_status=unresolved`，同样不得产生 `new_suffix`。
15. 对齐唯一时，只有已由正式提交门确认的历史 `committed_message` 继承旧 `worker_stable_id`；`selected_action` 只能经本次 confirmed action 映射提交预留号；`frame_local_unselected` 只用于证明序列连续性，动作后必须重新仲裁，不继承身份。
16. `new_suffix` 中的新文字/系统消息可以直接提交新序号；未转写语音和新图片首先全部保持 `frame_local_unselected`，不得批量分配 action ID、预留号或 ActionJournal。Worker 只能从当前最新帧唯一选中一条可执行媒体；选中后才为该条建立唯一 `pending_media_action`、分配不可复用预留号并原子写入 ActionJournal。其余媒体仍是未选观察；本次动作终结且获得新帧后，才能重新仲裁下一条。
17. 图片预留号只能作为 ActionJournal 的当次动作键。只有同一份已落盘的 confirmed receipt 同时严格证明 `canonical_action_id、reserved_worker_stable_id、binding_confirmed=true`、菜单/点击/剪贴板代次以及实际复制图片字节 SHA-256，才能经唯一提交门升级为正式消息。`pre/post_observation_id`、行号、坐标和气泡截图只作审计，不是提交依据。缺少或矛盾时必须上报幂等 `C2_IMAGE_IDENTITY_CONTRACT_INVALID` 技术故障；不得查询历史 Ledger/Outbox、不得生成正式 source key、不得写入 Ledger/Outbox、不得生成 completed/failed 图片消息，不得进入 Brain、Handoff 或飞书通知。
18. 语音与图片的正式消费者必须使用同一个白名单准入：缺失、空白、未知、临时、隔离或提交依据不完整全部拒绝。不得出现“图片校验完整回执、语音只校验存在 worker_stable_id”的媒体不对称旁路。
19. 禁止循环证明：新媒体第一次动作前不得要求本次动作产生的 `confirmed_action_mapping`，也不得因 `native_source_message_id` 为空而拒绝合法 `frame_action_binding`；动作前只预留编号，动作后才允许通过正式回执提交长期身份。
20. 媒体 UI 动作优先级固定为“语音优先、图片随后”：每次只执行一条已选语音，动作后用新帧重新仲裁；当前帧仍有可执行未转写语音时不得开始新图片动作。语音收敛后每次只选中一张图片；图片动作后新帧如出现可执行新语音，必须先回到语音阶段，再处理其余图片。
21. 媒体的 UI 动作顺序不是最终消息入库顺序。全部媒体动作终结后，Worker 必须以最后一张权威完整画面重建文字/语音/图片统一序列，最终 ingest 按该画面 `screen_order` 排序；不得因“语音先执行、图片后执行”改写客户真实发送顺序。
22. 图片复制后微信气泡通常不变，因此 Worker 必须在 ActionJournal 中保存“本 read flow 的临时动作槽位”：`read_run_id + action_plan_revision + selected_sequence_ordinal + pre_action_business_projection_digest`。它只用于防止同一 read flow 重复点击，不是消息身份，不得写入 `worker_stable_id/source_message_key`，也不得跨 flow 使用。
23. 图片动作后取得新完整画面时，Worker 必须调用 0.9.45.4 的唯一连续性比较器。旧序列与新序列完全相等、旧序列是新序列唯一完整前缀，或“旧序列尾部 = 新序列头部”形成唯一正常视口滑动衔接时，都可以继续；只处理对齐后的新尾部和剩余未处理媒体。已形成的动作回执绑定实际动作结果，不得按复读后的相同行号重挂。
24. 若当前画面因重复文字、同时长语音或相似图片产生零个/多个重叠解释，Worker 只允许一次受限上下文扩展读取，不重复任何媒体 UI 动作。扩展后能唯一对齐则继续；仍无重叠、多解，或证明发生替换、中间插入、换序、unknown/证据矛盾时，才停止后续图片 UI，保留已终结回执，以具体技术错误结算当前 task/Flow 并将 Worker 置为 `faulted`；零重复点击、零伪正式消息、零 Handoff、零飞书。正常头部滚出不属于本技术故障。
```

初次打开会话也使用同一算法：将本地/后端 checkpoint 中最近已提交尾部作为 pre 序列，将当前最新帧作为 post 序列。
确认该会话尚无任何历史 checkpoint 时，记录
`pre_sequence_source=empty_checkpoint + alignment_status=not_required + old_tail_fully_consumed=true`，
当前完整业务序列才可作为初始 `new_suffix`；
已存在 checkpoint 但可见上下文不足以形成唯一对齐时，必须进入歧义门禁，不得把整屏当成新消息。

“内容相同”只能说两条观察可能兼容，不能说它们是同一条消息。
对于连续重复内容，必须使用整段已确认序列和“新消息只能追加在尾部”的不变量解除歧义。

标准示例：

```text
pre_action_identity_sequence:
  文字1=worker-message-10
  语音1=worker-message-11
  图片1=worker-message-12
  文字2=worker-message-13（正文“好的”）

图片动作后post_action_observation_sequence:
  文字1
  语音1
  图片1
  文字2（正文“好的”）
  文字3（正文“好的”）

唯一合法结果:
  前四条按原顺序继承worker-message-10..13
  old_tail_fully_consumed=true
  文字3是未被历史序列消费的new_suffix
  文字3分配worker-message-14
```

如果最新帧只看到一条“好的”，没有足够上下文证明它是旧文字2还是新文字3，
必须返回 `MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS`，不得用正文相等或单条出现就复用 `worker-message-13`。

对齐结果必须落盘为 `sequence_alignment_evidence`：

| 字段 | 规则 |
|---|---|
| `pre_sequence_source` | `action_frame / checkpoint / empty_checkpoint`；初次无历史的特例必须显式为 `empty_checkpoint` |
| `pre_frame_id / post_frame_id` | 必填且不同 |
| `alignment_status` | `unique / ambiguous / unresolved / not_required` |
| `matched_pairs[]` | 每项含 `identity_state/worker_stable_id（可空）/pre_observation_id/post_observation_id/pre_index/post_index/match_basis` |
| `old_tail_fully_consumed` | 布尔值；表示 pre 序列最后一条业务观察已被消费；只有 `true` 时允许存在 `new_suffix_observation_ids[]` |
| `new_suffix_observation_ids[]` | post 序列尾部连续且未被 matched_pairs 消费的新观察 |
| `candidate_alignment_count` | `unique` 时必须为 `1`；`ambiguous` 时必须大于 `1`；`unresolved/not_required` 时为 `0` |

`pre_sequence_source=checkpoint` 时 `pre_frame_id` 使用 `checkpoint:<revision>`；
`empty_checkpoint` 时使用 `checkpoint:none:<conversation_id>`，禁止伪造截图 ID。
`ambiguous/unresolved` 时 `new_suffix_observation_ids[]` 必须为空。

Worker 是序列对齐和编号分配的唯一决定者；OmniAuto 只提供 frame ID、observation ID、
顺序、角色、类型、内容摘要和原生稳定源证据。后端只校验、持久和返回 checkpoint，
不得重新排序或根据正文重算身份。

#### 6.0.3.1 dedupe_key生成规则

优先级如下：

| 优先级 | 来源 | 生成方式 |
|---|---|---|
| 1 | 微信原生稳定来源 ID | 只有真实 `native_source_message_id` 可直接作跨轮强证据；OCR 生成的视觉编号不属于原生 ID。 |
| 2 | OCR 文字和系统消息 | 正式 `source_message_key` 只允许使用经唯一历史序列恢复，或对完整 `new_suffix` 直接提交的 `worker_stable_id`。 |
| 3 | OCR 语音和图片 | 历史媒体只能恢复后端/本地已确认正式 ID；本轮新媒体只有 confirmed action 经唯一提交门提交后才能生成正式 `source_message_key`。仅属于 `new_suffix` 不足以提交媒体身份。 |
| 4 | 语音补充禁止项 | voice anchor、时长、正文、坐标、“从底部第几条”及它们的组合都不得生成、恢复或重挂长期身份。 |
| 5 | 系统/文件等兼容消息 | 使用稳定来源 ID 或受约束的结构化内容摘要；无法形成可靠身份时不入库。 |
| 6 | 帧内视觉证据 | `frame_visual_id` 允许包含坐标，仅供当帧定位、点击和证据排查；不得生成 `dedupe_key/source_message_key`。旧 `canonical_visual_id/canonical_input_id` 不得进入正式身份输入。 |

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

语音的 structural/stable anchor、气泡坐标和“从底部第几条”只属于当前 Sidecar
动作事务的定位证据。Worker 必须先在同一初始帧合并 aliases，得到动作局部
`canonical_voice_action_id`。已知历史语音只能由已持久化的正式 Worker 身份和后端
`identity_checkpoint.recent_messages` 恢复；新语音在点击前只能原子预留一个永不复用的
`reserved_worker_stable_id`，并写入 ActionJournal，不得加入已确认身份 catalog、不得生成
`source_message_key`、不得查询后便以“历史已结算”删除当前观察。只有同一 Sidecar 动作
事务返回了本合同规定的唯一绑定证据，Worker 才能把预留序号提交为正式
`worker_stable_id`。后端已确认的历史语音、图片不得再次转写、调用 Vision、进入新 Outbox
或参与 Brain；本地已形成 terminal 但尚未创建 Outbox 的崩溃边界事实，仍按原
`origin_read_run_id` 恢复投递。

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
8. 唯一键命中：重新读取已有事件，比较sender_role、message_type、规范化content和媒体稳定锚点；全部一致才返回duplicated。
9. 唯一键相同但任一身份不变量不同：返回MESSAGE_IDENTITY_COLLISION和refresh_identity_and_retry，禁止静默duplicated，禁止推进状态机。
10. 数据库IntegrityError补偿分支也必须重新查询并执行同一身份比较，不能直接吞成duplicated。
11. 状态不允许、发送方不明确、目标未确认、群聊/unknown或合同门禁失败：不创建message_event，返回ignored或拒绝整批。
12. 只有ingested且sender_role=customer的消息，才允许触发后续message_batch收集。
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
| `duplicated` | 否 | 否 | 已有消息与本次角色、类型、正文和稳定锚点全部一致，确认为同一条。 |
| `ignored` | 否 | 否 | 系统消息、自发消息、状态不允许或低价值消息。 |

`MESSAGE_IDENTITY_COLLISION` 不是 `duplicated`，也不是可以忽略的普通错误。后端必须
写入可诊断事件，至少记录会话、冲突键、已有/新消息的角色、类型和脱敏正文摘要；
Worker 必须保留 Outbox 并执行身份刷新重传。在冲突解决前不得把该消息标记为本地
已入库，也不得把残缺上下文交给 Brain。

服务端继续按每条消息的 `source_message_key/dedupe_key` 幂等；同一消息重复上报时必须返回
同样或等价的处理结果，不得重复入库。`read_run_id` 只表示读取事实所属轮次；一个读取事务
可能先后形成多个内容不同的入库请求。Worker 不得再把 `read_run_id` 直接当作本地消息
Outbox 的唯一键，本地 `outbox_batch_key` 也不得发送给服务端。

##### 6.0.3.3.1 读取轮次归属与传输状态分层

原先把 `NEW_MESSAGE / OUTBOX_WAITING / OLD_FAILED / OLD_COMPLETED` 作为同一个
互斥枚举的模型废弃。它混合了“事实属于哪次读取”和“事实是否上报”，
不得继续作为历史连续性、Vision 准入或 Brain 准入的决策输入。

只有已经通过唯一正式身份提交门的 `committed_message` 才能成为业务槽位，并同时具有三个独立维度。
帧内观察、待处理媒体动作和隔离记录不得伪造这些字段，也不得因为拥有其中任一字段而被反向认定为正式消息：

| 维度 | 字段 | 允许值 | 只回答什么 |
|---|---|---|---|
| 事实归属 | `fact_scope` + `origin_read_run_id` | `current_read_run / historical / unknown` + 原始读取轮次 ID | 这条事实是本轮还是历史。 |
| 传输进度 | `delivery_state` | `not_enqueued / outbox_waiting / backend_confirmed` | 完整 JSON 是否已被后端确认。 |
| 单条处理结果 | `item_state` | `completed / failed` | 文字、语音或图片事实本身是否处理成功。 |

`read_run_id` 生成和继承规则固定为：

1. Worker 取得单会话有效授权后，在首次 `messages/initial_read` 和任何语音、图片
   动作前生成一个 `read_run_id`。
2. 本次的 initial read、语音展开、图片处理、final read、二次连续性判断、Ledger、
   ActionJournal 和 Outbox 全部沿用该 ID；中途不得重新生成。
3. 本轮首次出现且未命中历史 Ledger、Outbox 或后端身份检查点的稳定消息，固定为
   `fact_scope=current_read_run`、`origin_read_run_id=顶层 read_run_id`。
4. 命中已有 Ledger、Outbox 或后端检查点时必须继承 `origin_read_run_id`；与顶层 ID
   相同为 `current_read_run`，不同为 `historical`，来源冲突或不可证明为 `unknown`。
5. 媒体身份正式提交并生成 `source_message_key` 后，重建槽位才允许按该 key 合并；只允许 `item_state` 和
   `delivery_state` 单调前进，不得改变 `origin_read_run_id` 或 `fact_scope`。
6. Outbox 重传、授权外壳重建、拆批和事务恢复必须保留原始读取轮次；只有真正开始
   一次新的权威读取才允许生成新 ID。
7. `flow_id` 继续只表示动作流程或 UI 锁上下文，不得成为第二套事实轮次身份。

历史连续性算法固定为：

```text
按 final_read.screen_order 从上到下遍历稳定业务槽位
-> historical* 后接 current_read_run* 为正常增量边界
-> 一旦看到 current_read_run，其下方再出现 historical 才是 C2_MESSAGE_HISTORY_GAP
-> 任何 unknown 走现有身份不确定门禁，不得伪装成历史断层
-> delivery_state 和 item_state 全程不参与新旧连续性判断
```

因此，“本轮新文字 -> 本轮图片 `outbox_waiting` -> 本轮新语音”三者均为
`current_read_run`，不得报历史断层。图片进入右键复制和 Vision 的必要条件为
`fact_scope=current_read_run + delivery_state=not_enqueued`；历史图片和已进入 Outbox
的图片不得重复处理。

当前机器合同必须完整包含媒体和读取轮次字段，同时表达读取轮次与传输状态、语音动作身份与业务身份分离、
选中目标两阶段握手和逐相邻帧同对象证明。`identity_checkpoint.recent_messages[]` 回传
`origin_read_run_id`；`ledger_state` 只允许作为诊断投影，业务门禁不得读取。

##### 6.0.3.3.2 同一会话事务内任意多次入库的本地 Outbox 唯一键

本节只修复 Worker 本地 Outbox 主键冲突，不拆分现有外层单会话 Flow，不改变
`read_run_id`、HTTP 消息合同、UI 锁、暂停接单、授权、媒体编排、Brain、
`reply_action`、`pre_send_refresh` 或发送状态机。同一 Flow 内可以发生零次、一次或任意
多次逻辑入库；次数不写死为两次。每次只要正式消息集合不同，就必须形成独立本地 Outbox。

四类身份的职责固定如下：

| 身份 | 唯一职责 | 禁止用途 |
|---|---|---|
| `flow_id` | 当前单会话业务事务、UI 锁和暂停后安全结算范围 | 不得作为消息或 Outbox 载荷身份。 |
| `read_run_id` | 消息事实所属读取轮次、后端读取结算和 Ledger/Journal 恢复关联 | 可以被多条本地 Outbox 关联，但不能单独成为 Outbox 主键。 |
| `source_message_key` | 一条正式消息的长期身份 | 不得表示整次请求或 MessageBatch。 |
| `outbox_batch_key` | Worker 本地一组不可变待投递事实的确定性键，只编码在 `outbox_id` 中 | 不得进入 HTTP 请求、后端 Schema、消息身份、Brain 批次或会话状态。 |

`outbox_batch_key` 必须由 `storage.py` 的唯一函数在首次持久化前计算；TaskRunner、恢复器和
拆包器不得自行拼接。计算规则为：

```text
message_keys =
  若为拆包子项：evidence.ingest_partition.expected_source_message_keys
  否则：messages[].source_message_key
然后按字符串升序去重。

payload_kind =
  message_keys非空                         -> "messages"
  authorization_scope == "fact_settlement" -> "fact_settlement"
  flow_gate_errors非空                     -> "flow_gate"
  其他受控空读                             -> "control_read"

seed = {
  "namespace": "chejin:c2-local-outbox:v1",
  "conversation_id": conversation_id,
  "read_run_id": read_run_id,
  "payload_kind": payload_kind,
  "source_message_keys": message_keys,
  "flow_gate_identity_key": evidence.flow_gate_identity_key或
      按字符串升序去重的flow_gate_errors以"\n"连接，
  "recovery_transaction_id": evidence.recovery_transaction_id或空字符串,
  "source_message_key_digest": evidence.source_message_key_digest或空字符串,
  "control_key": 对control_read使用
      authorization_read_reason + ":" + continuation_batch_id + ":" + recall_cycle_id，
      其他类型为空字符串
}
canonical = UTF-8 JSON(seed, sort_keys=true, separators=(",", ":"), ensure_ascii=false)
outbox_batch_key = SHA256(canonical).hexdigest()
```

本地 Outbox ID 固定为：

```text
普通/父批：c2-outbox:{read_run_id}:batch-{outbox_batch_key}
拆包子项：c2-outbox:{read_run_id}:batch-{outbox_batch_key}:part-{part_index}
```

规则固定如下：

1. 相同不可变事实在 HTTP 重试、进程重启、授权版本刷新或 Outbox 重传后必须得到相同本地 ID。
   种子禁止包含合同 revision、时间戳、`trace_id`、HTTP 请求 ID、重试次数、
   `authorization_revision`、操作阶段、冷却时间、坐标、OCR 文本、诊断字段和瞬时错误。
2. 同一个 `read_run_id` 内，只要正式 `source_message_key` 集合不同，就得到不同
   `outbox_batch_key`；A、A+B、A+B+C 是三条独立 Outbox，后续可继续产生第 N 条。
3. 同一 source key 集合的顺序变化不得改变 ID。同一 source key 的角色、类型、正文、
   `dedupe_key`、`item_state` 或稳定媒体结果发生变化时，必须命中同一 ID并由
   `enqueue_c2_outbox` 比较不可变事实摘要后返回 `C2_OUTBOX_LOGICAL_FACT_COLLISION`；
   不得覆盖旧载荷，也不得生成新 ID 绕过后端身份碰撞。
4. 不可变事实摘要只包含每条消息的 `source_message_key、dedupe_key、sender_role_hint、
   message_type、content、item_state、flow_state` 和稳定媒体结果字段；明确排除授权外壳、
   timing、截图路径、OCR bbox、trace 和诊断。相同 ID 且摘要一致时返回已有 Outbox，
   `enqueue_c2_outbox` 不覆盖其 JSON；授权刷新只能继续走现有
   `refresh_c2_outbox_payload` 显式入口。
5. 已进入 `confirmed、split_completed、identity_quarantined、capability_paused、
   target_terminated、conversation_terminated` 的 Outbox 永远不可覆盖、改写或删除后复用。
6. 拆包前后 `evidence.ingest_partition.group_id` 继续沿用现有后端合同的 `read_run_id`；
   所有子项根据完整 `expected_source_message_keys` 取得相同 `outbox_batch_key`，只用
   `part_index` 区分本地子行。不得改后端分片合同。
7. `POST /wechat/messages/ingest` 请求和响应不新增 `ingest_batch_id/outbox_batch_key`；后端继续
   按 `source_message_key/dedupe_key` 去重，并在同一事务内完成新消息入库、旧回复作废和
   MessageBatch 创建或复用。本地键不得成为第二套后端业务身份。
8. 同一 Flow 结束前继续使用现有 `has_pending_c2_outbox_for_read_run_id` 检查该
   `read_run_id` 关联的全部 Outbox；任一尚未结算时均不得结束 Flow、开始下一个客户或发送旧回复。
9. 升级任何新合同前必须暂停旧 Worker，并确认消息 Outbox、媒体 Journal、Ledger
   和 sent_ack 均无未结算事实；如仍有 pending，只能先使用原版本完成结算，不得删除或改写。
   不增加新旧本地键双轨兼容。

发送前新增消息的唯一处理顺序为：

```text
原消息批次已生成待发送回复
-> 同一单会话Flow执行pre_send_refresh
-> 发现并提交新的客户文字/语音/图片事实
-> 按新source_message_key集合生成新的本地outbox_batch_key
-> 先可靠写入新的独立Outbox，再按原合同调用messages/ingest
-> 后端同一事务入库新消息、将旧reply_action置为superseded、创建或复用新MessageBatch
-> 事务提交后返回逐消息结果和新MessageBatch
-> 只基于最新完整尾部重新调用Brain一次
-> 新回复重新经过pre_send_refresh和发送门禁
```

若新批次入库失败或结果不确定，必须保留新 Outbox 并阻止旧回复发送；不得把旧批次成功
冒充为本次新事实成功。若发送前复读没有新正式消息，则不得新建消息 Outbox，继续原发送流程。

本变更的发布门禁必须调用生产构建器、真实本地 SQLite Outbox、后端正式路由和真实服务层；
允许替换微信截图/OCR/鼠标，但禁止 Fake API 返回预造成功、禁止 mock
`_read_one_wechat_target`、`enqueue_c2_outbox`、Outbox 投递器或后端 `ingest_messages` 后宣称
端到端通过。至少覆盖：

1. 初次客户文字 A 入库并生成回复；发送前新增客户语音 B，语音只点击/转写一次，形成第二个
   `outbox_batch_key` 和第二条 Outbox；后端确实新增 B，旧回复作废，只基于 A+B 生成一次新回复。
2. 同一 Flow/read_run 连续形成 A、A+B、A+B+C 三组事实，必须有三条不同本地 Outbox；
   再增加事实时可继续形成第 N 条，不得覆盖前面任一条。
3. 把 B 分别替换为新文字、新图片、新 self 消息，均验证独立 Outbox、不发送旧回复；
   self 路径不得调用 Brain。
4. 相同事实在写 Outbox 前、写后未请求、请求后未响应、后端已提交但本地未确认四个崩溃点
   重启，均复用同一 ID、零重复消息、零重复媒体动作。
5. 同一 `read_run_id` 的不同消息集合必须产生不同 ID；相同集合但顺序不同必须产生同一 ID；
   时间戳、trace、授权 revision、重试次数变化不得改变 ID。
6. 相同 source key 集合但正文/角色/类型变化必须在本地报
   `C2_OUTBOX_LOGICAL_FACT_COLLISION`，旧 JSON 不变且零后端调用。
7. 已确认旧 Outbox 与新 Outbox 同时存在且载荷互不覆盖；Flow 结束门禁会枚举全部行，任一未确认
   都不能结束、切换客户或发送旧回复。
8. 拆包场景全部 part 共用基础 `outbox_batch_key`，后端 `group_id` 仍等于 `read_run_id`；
   重复任一 part 不重复入库。
9. `flow_gate`、`control_read` 和 `fact_settlement` 分别验证稳定本地 ID、无 UI 重试及合同反例；空事实不得伪装
   `fact_settlement`。
10. 授权 revision 刷新前后 Outbox ID 不变，只允许显式授权外壳更新，消息事实 JSON 不变。
11. 暂停接单发生在任意第 N 批入库边界时，只安全结算当前 Flow，不开始下一个客户；恢复后只
   重传原 Outbox，不重新读微信。
12. C4 召回、普通首次读取和没有新事实的发送前复读保持原行为；没有新事实时零新 Outbox、
    零额外 Brain、原回复仍经过既有发送门禁。

#### 6.0.3.4 低置信和异常处理

| 场景 | 处理 |
|---|---|
| 缺少 `dedupe_key` | 拒绝该条消息，返回 `MESSAGE_DEDUPE_KEY_MISSING`。 |
| `conversation_id` 未绑定当前 Worker、绑定缺少 `remark_code` 或监听状态不允许 | 拒绝整批或该会话消息，返回 `MESSAGE_CONVERSATION_NOT_BOUND`。 |
| 读取目标未确认、搜索不到或搜索结果不唯一 | 本轮零消息读取、零媒体操作、零入库、零回复，记录 `TARGET_NOT_CONFIRMED / SEARCH_NOT_FOUND / SEARCH_AMBIGUOUS`。如果 visible 点击后明确打开了不含目标短码的其他会话，允许按安全误点恢复规则完整重新定位一次；其他目标准入失败等待后续扫描证据改善。不得直接创建客户 handoff，也不能影响其他短码。 |
| 普通聊天文本缺少同行头像证明 | 不入库。只有 `lane_geometry`、没有 `same_row_avatar` 不足以证明 customer/self。 |
| 语音转写文本缺少独立头像 | 只能继承已确认的父语音 `parent_voice` 角色；无法绑定父语音则不入库。 |
| 普通文字/语音发送方无法判断 | 不猜角色、不入库；按 L2 `recoverable_hold` 先纯数据合并，仍不明确时最多执行两次不同 `read_run_id` 的被动稳定重读。只在仍影响最新待回复尾部时停止该会话 AI，旧区间歧义不连坐最新完整消息。 |
| 消息顺序异常 | 不得假定物理处理顺序等于对话顺序；必须按最终权威画面建立统一 `screen_order`。 |
| 图片气泡 | 初次观察角色不可信时仍是帧内观察，形成可自动恢复的 `MESSAGE_IDENTITY_UNCONFIRMED` 帧级 hold，零点击且不得持久化 `ignored` Ledger；角色可靠且被唯一识别为新图片时只建立 `pending_media_action`，不能提前生成正式身份、`fact_scope/delivery_state` 或查询 Ledger。历史正式图片和已入 Outbox 图片不重复执行；动作前出屏且零触发的图片取消本轮候选；操作对象已唯一但内容处理失败时才形成 committed failed 事实；操作对象无法确认时进入隔离而不是伪造 failed 消息。customer committed failed 按 L1 转人工，self committed failed 只记 warning。 |

#### 6.0.3.5 逐条结果单调合并

同一画面中每条已经提交的正式文字、语音和图片都按 `source_message_key` 独立处理；
帧内观察、待处理动作和隔离记录没有正式 source key。任何循环、补充读取或后续处理都只能合并结果，不能覆盖整组结果。

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
每轮结束前所有正式消息必须归入completed / failed / ignored；所有已创建媒体动作必须归入四种动作终态之一；
不得以最后一次函数返回值替换本轮累计集合。
```

这里的“正式消息”指已经通过唯一身份提交门并具有可信角色和稳定 `source_message_key` 的业务消息。
初次图片观察、待处理动作或隔离记录不得为了满足终态计数而伪造 `ignored/failed` 消息；它们分别按观察、动作或隔离恢复规则收口。

成功事实正常进入 Outbox 和后端入库。failed 语音或图片同样必须形成逐条终态，并按
角色和时点处理：`self` 失败媒体不阻断客户回复；发生在 reply-safe boundary 之前且已
结算的旧失败不重新触发接管；当前 `customer` 单条语音/图片失败在事实结算后直接创建
handoff 并进入 `waiting_sales_reply`，不生成请求重发或改发文字的自动回复。失败不能伪装成
`no_action`。`partial` 只是“同一 Flow 同时包含成功和失败”的汇总视图，不是新的单条
消息状态，也不创建长期 pending 任务。

#### 6.0.3.6 AI回复优先的门禁分级与自动恢复

本系统的优化目标是：在不猜客户、不读错会话、不编造事实和不重复发送的前提下，
能够由 AI 安全回复的场景尽量由 AI 回复。技术错误、销售跟进通知和“必须停止 AI”是
三个不同概念，禁止再统一映射成永久 `waiting_sales_reply`。

后端必须先确定 `reply_safe_boundary`：最后一条已被服务端确认身份和顺序的消息；再确定
`reply_safe_suffix`：该边界之后、截至最新客户消息，角色、顺序、内容和媒体终态均完整的
连续尾部。Brain 只需最新待回复尾部完整，不要求整个历史窗口百分之百完美。任何门禁都
必须携带 `gate_scope=item/reply_suffix/conversation/worker`、受影响的 source key 或最终
画面顺序范围；没有影响范围的门禁不得自动升级为永久会话 handoff。

| 等级 | 典型状态 | 固定处理 |
|---|---|---|
| L0：不阻断 AI | reply-safe boundary 之前的旧缺口；已被服务端确认的旧消息；`self` 侧媒体失败；普通寒暄/澄清问题没有 RAG 证据 | 记录 warning，继续使用完整 `reply_safe_suffix` 进入 Brain。 |
| L1：单条媒体人工接管 | 当前 customer 媒体的操作对象、动作类型和结果回执都已唯一证明，但微信或批准的媒体 Provider 明确返回内容处理失败，例如已确认语音动作后的 `C2_VOICE_TRANSCRIBE_FAILED/EMPTY`，或已复制出唯一图片字节后的 `C2_IMAGE_UNDERSTANDING_FAILED` | 失败事实照常入库且不重复媒体动作；随后直接创建 handoff、通知销售并进入 `waiting_sales_reply`。不得调用 Brain 回答同批文字，也不得生成“请重发/改发文字”等澄清回复。 |
| L2：可自动恢复暂停 | 仅限尚未触发媒体 UI 动作、且存在可靠数据恢复路径的历史事实问题：`MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS`、`C2_MESSAGE_HISTORY_GAP`、`MESSAGE_IDENTITY_UNCONFIRMED`；非 6.0.4.4 新消息重新识别的回复上下文、授权、续行批次、任务租约或 AI Provider 暂时失败 | 首次只创建 `recoverable_hold` 且次数记 0，不得创建长期 handoff。先用后端检查点和数据库事实纯数据合并，不操作微信且不计次数；仍未恢复时，最多做两次不同 `read_run_id` 的被动稳定重读。中途确认清楚立即关闭 hold 并继续当前最新批次；两次后或 120 秒后仍不明确，只有本行列出的业务事实不确定才可幂等 handoff。`pre_send_refresh` 只允许一次确定性重新识别或具体错误终态。 |
| LF：客户端技术故障 | `C2_IMAGE_IDENTITY_CONTRACT_INVALID`、`C2_VOICE_IDENTITY_CONTRACT_INVALID`、`C2_VOICE_RESULT_AMBIGUOUS`，以及已触发或可能已触发动作后的无结果、多结果、错对象、回执矛盾或无法绑定 | 不重读、不重复点击、不伪造 failed 消息、不进入 Brain、不创建 HandoffEvent、不通知飞书。保存完整证据，task/Flow=`technical_failed`，释放 UI 锁，Worker=`faulted + can_pull_tasks=false`。 |
| L3：硬停止或人工接管 | 高意向；明确 `hard_opt_out`、会话关闭/拒绝/黑名单、人工关闭 AI、销售已实际接管、目标客户或 private 会话无法确认、发送结果可能已触发但未知、违法/支付/合同/审批/赔付等必须由权威人员决定且无法形成安全边界回复 | 禁止自由回答。高意向直接 handoff 并通知销售；除拒收、关闭、目标不明和发送结果未知等必须静默的场景外，其他业务硬风险优先使用 `reply_then_handoff`：先发送 Guard 通过的边界说明，再创建人工接管；禁止编造价格、库存、审批或承诺。 |

两个身份门禁的固定恢复规则：

```text
MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS
-> 每轮都合并后端identity_checkpoint，后端已确认身份优先于本地缓存
-> 有可靠边界时，只给边界后的新消息分配更高序号
-> 无可靠边界时创建recoverable_hold（次数0），先纯数据合并，再最多两次不同read_run_id的被动稳定重读
-> 不影响最新尾部或恢复成功：自动清除
-> 两次/120秒后仍影响最新待回复消息：仅该会话handoff

C2_MESSAGE_HISTORY_GAP
-> NEW/OLD判断同时查询本地Ledger和后端recent_messages，禁止只看本地
-> 后端已有的消息归为OLD；旧区间回填只作为backfill，不触发回复也不转人工
-> 缺口位于reply_safe_boundary之前：继续回复最新完整尾部
-> 缺口切入最新待回复尾部且重建失败：仅该会话handoff
```

仅由 L2 技术门禁创建的历史 handoff，在后续一次权威读取证明最新尾部完整后，服务端必须
以 `auto_recovered_clean_read` 自动关闭；不得要求销售先发一条消息才能恢复 AI。L3 业务
接管、人工 `pause`、明确拒收和硬开关不得自动关闭。

本轮机器字段固定为：后端 `read-targets` 下发
`recoverable_handoff_reason_codes`；Worker 在同一个 V3 `messages/ingest.evidence` 中回传
`recoverable_handoff_resolution`，包含 `status=latest_unreplied_turn_complete`、精确
`reason_codes`、`identity_confirmed=true`、`history_confirmed=true` 和是否执行自动重读。
后端先关闭匹配的 L2 旧事件，再让同批最新 customer 消息进入 Brain；有其他 handoff、
当前 flow gate、无权威非空画面、授权已变化或 `ai_enabled=false` 时不得自动恢复。

C3 同样采用分级恢复：`C2_REPLY_CONTEXT_MISSING`、
`C2_REPLY_TARGET_NOT_AUTHORIZED`、`C2_REPLY_CONTEXT_RECOVERY_FAILED`、
`C3_REPLACEMENT_BATCH_MISSING`、`TASK_LEASE_EXPIRED`、`AI_ENGINE_UNAVAILABLE/TIMEOUT`、
`AI_ENGINE_CONTRACT_INVALID` 和 `GUARD_REWRITE_FAILED` 首次不得直接长期 handoff；应分别
从数据库重建上下文、刷新授权/批次/租约或按同一 batch 有界重试。技术恢复耗尽后仍有
未回复客户消息，才进入人工接管。

#### 6.0.3.7 Outbox统一恢复动作

Outbox 保存已经完成昂贵处理后的完整结构化 V3 JSON，不保存原图。后续失败必须
由一个集中分类器归入以下正式 `recovery_action`：

| `recovery_action` | 适用场景 | 恢复方式 |
|---|---|---|
| `retry` | 网络中断、连接超时、服务端暂时不可用。 | 按有上限的指数退避间隔原样重传同一 JSON 和幂等键；不按固定次数放弃，不得重新读微信、转语音或调用 Vision。 |
| `refresh_and_rebuild` | 授权版本过期或续行票失效，但消息事实本身仍合法。 | 重新取得当前授权，只重建授权/续行外壳；原消息身份、正文、观察时间和证据保持不变，不重复昂贵动作。后端可补录事实，但只有新授权允许时才能推进状态机或 Brain。 |
| `refresh_identity_and_retry` | 相同去重键对应的角色、类型、正文或媒体锚点不同，服务端确认发生身份碰撞。 | 保留原 Outbox，刷新服务端身份检查点，只为冲突项分配高于服务端下限的新编号并重建身份键后重传；不重新读取微信，不重复语音/Vision，不把冲突项记为 duplicated。 |
| `settle_without_ui` | 当前 UI 授权、短码托管或会话状态已变化，但原事实仍可由后端确认归属。 | 取得 settlement token，把包含原完整 messages 的同一 V3 JSON 改为 `authorization_scope=fact_settlement`；只结算事实，固定不推进状态机、不创建 handoff、不启动 Brain。禁止使用 `messages=[]` 的空门禁替代原失败消息。 |
| `rebuild_failed_facts` | 请求中个别语音/图片的失败事实结构不合法，后端能精确指出 source key。 | 只重建指定 failed 事实，不改变其他 completed 事实，不重复媒体动作。 |
| `split_and_retry` | 请求体超过合同上限。 | 按原 read_run 和 source key 清单确定性拆分；最后一片确认前不启动 Brain。 |
| `capability_paused` | 请求级合同、身份或版本整体不兼容，后端无法安全接收整个请求。 | 冻结原 Outbox 和 ledger，阻断新 UI 动作，按退避周期自动探测合同/能力恢复；恢复后转回 `retry`，不建立人工消息处理队列。 |
| `identity_quarantined` | 后端明确返回 `MESSAGE_IDENTITY_COLLISION_NOT_REKEYABLE`，无法安全换号。 | 保留原 Outbox、响应和身份证据；停止自动重试，仅隔离当前会话并继续其他短码，等待显式修复。 |
| `conversation_terminated` | 后端无法安全把内容归入业务会话。 | 后端先持久化 technical terminal 和逐条 source key 结果，再确认 Worker 结束 Outbox；不得静默丢弃。 |

消息级失败不能升级成请求级拒绝。有效会话中的语音或图片失败必须作为
`item_state=failed + error_code` 通过同一个 `messages/ingest` 入库，后端仍返回
`ingested`；附加截图、诊断或非核心证据异常只返回 warning。`self` failed 媒体只作
上下文 warning；当前 customer failed 媒体按 L1 单条媒体人工接管处理：事实完成逐条
结算后直接创建 handoff、通知销售并进入 `waiting_sales_reply`，不调用 Brain 回答同批
文字，也不发送“请改发文字/重发图片”等自动澄清回复。

兼容字段 `retryable` 不能单独决定 Worker 行为；正式响应必须提供 `recovery_action`。没有该字段时采取最保守策略：不操作微信、不调用 Brain、不发送，冻结原事实并进入 `capability_paused` 自动探测。

Outbox 是 Worker 级全局调度门禁，不只是当前会话的小缓存。只要仍存在一批已经
完成微信读取/语音/Vision但尚未获后端确认的消息事实，Worker 就只能执行该批次
对应的正式恢复动作，不得继续首屏扫描、定向读取、切换会话或执行新微信动作。
网络恢复、授权刷新、事实结算或合同能力恢复后自动续传；不存在按次数放弃、
本地 `not_required` 丢弃或人工修消息的出口。

恢复结果必须与授权范围分层：当前 `active_read` 授权仍有效时，重传原完整 V3
消息并按 sender_role 和最终画面顺序正常应用状态机；只有 `fact_settlement` 授权时，
保存相同消息事实但固定 `state_transition_applied=false`。两种路径都必须返回逐
`source_message_key` 结果，Worker 不得用一个空 `flow_gate` 成功响应替代消息确认，
也不得在逐条确认前把 Ledger 改为 confirmed。

### 6.0.4 状态机定向读取工程方案

C2 主链固定为“第一屏主动扫描 + 第一屏命中优先读取 + 短码搜索定向读取 + 召回前 precheck + 去重入库”。

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
13. 返回处理中 batch_id：保持原会话的逻辑事务所有权等待 Brain；本版没有物理键鼠锁
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
| `C2_VISIBLE_TARGET_STALE_AFTER_CLICK` | visible 截图与点击之间列表重排，点击后标题明确不含目标短码。 | 不读取消息区、不操作媒体、不入库、不发送；丢弃全部旧坐标，复核授权后最多完整重新定位一次。 |
| `TARGET_SEARCH_AMBIGUOUS` | 搜索结果存在多个疑似会话。 | 不读取，进入人工复核或等待下次扫描。 |
| `C2_VISIBLE_TARGET_AMBIGUOUS` | 当前实时首屏中同一 `remark_code` 命中多条会话。 | 不点击、不读取，记录首屏截图和候选列表，等待人工复核或下轮数据修正。 |
| `TARGET_CONFIRM_FAILED` | 点击后标题/备注未确认包含短码。 | 不读取，返回目标不确认。 |
| `TARGET_OCR_LOW_CONFIDENCE` | 搜索结果或标题 OCR 置信度不足。 | 不读取，保留截图证据。 |
| `TARGET_NOT_CONFIRMED_FOR_MESSAGES` | 目标会话未确认，不允许读取消息。 | 不入库，不触发 AI。 |

#### 6.0.4.1 定向读取执行步骤

定向读取要参考 `add_friend` 主链路的工程风格：字段先强校验，校验失败不触达微信 UI；每个 UI 动作都有 step event、截图证据、耗时和错误码；禁止固定坐标兜底；目标没有二次确认时不得读取消息。

正式 OmniAuto 入口建议保持在 `messages` action 下扩展模式，避免新增一套并行读取协议。`target_mode=auto` 表示先做实时首屏解析，首屏唯一命中则走 visible，未命中才走 `search_by_remark_code`：

```text
action=messages
target_mode=auto | visible | search_by_remark_code
conversation_id=<服务端会话ID>
remark_code=<客户短码>
read_reason=friend_acceptance_visible_hit | visible_unread | waiting_user_reply | recent_ai_sent | recall_precheck | waiting_sales_reply
last_ingested_at=<服务端最后入库时间，可选>
display_name=<最近一次绑定展示名，可选>
rpa_session_key=<第一屏最近一次定位键，可选>
artifact_dir=<本次证据目录>
```

上面仍使用既有后端/Sidecar 字段名 `read_reason`；Worker 收到后只按
`authorization_read_reason` 语义保存。`operation_phase=authorized_read | pre_send_refresh`
是 Worker 调用读取流程时的内部执行上下文，不得伪装成 Sidecar 或后端新增字段。

`remark_code` 是搜索和身份确认主锚点；`conversation_id` 是服务端业务身份；`display_name / rpa_session_key` 只能辅助定位、展示和排查。

| 步骤 | 名称 | 操作要求 | 成功标准 | 失败处理 |
|---|---|---|---|---|
| 0 | Worker 调用上下文强校验 | 校验 `conversation_id / remark_code / authorization_read_reason / operation_phase / artifact_dir`；`remark_code` 必须非空、无空白、长度不超过备注规则上限；阶段缺失、非法或与 `current_step` 矛盾时失败关闭。 | 字段合法，生成 `read_run_id`；尚未操作微信。 | 返回 `C2_TARGET_PAYLOAD_INVALID / C2_READ_OPERATION_PHASE_INVALID / C2_READ_OPERATION_PHASE_CONFLICT`；`wechat_ui_action_attempted=false`；不得探测窗口、截图或点击。 |
| 1 | 获取本地微信 UI 锁 | Worker 获取 Local WeChat UI Lock，`operation_type=message_ingest`；授权来源保留在目标/证据，执行阶段保留在本轮 Flow timing 与日志中，锁只负责互斥和当前步骤。 | 获得有效锁和 fencing token。 | 返回 `WECHAT_UI_LOCK_BUSY` 或等待下轮调度；不得并发操作微信。 |
| 2 | 微信窗口预检 | 调 OmniAuto 检查微信主窗口、登录态、遮挡、弹窗、风险提示、当前窗口是否可控。 | 微信主窗口可控，无阻塞弹窗。 | 返回 `WECHAT_WINDOW_NOT_READY / WECHAT_RISK_PROMPT_DETECTED / WECHAT_MODAL_BLOCKED`；释放锁。 |
| 3 | 实时首屏解析 | 在锁内调用 `sessions`，获取当前这一刻的第一屏会话列表和截图证据，用 `read-target.remark_code` 匹配。 | 唯一命中则进入 visible 路径；未命中才进入搜索路径。 | 多条命中返回 `C2_VISIBLE_TARGET_AMBIGUOUS`；不得点击和读取。 |
| 4A | visible 点击当前行 | 仅实时首屏唯一命中时执行。使用当前首屏候选行安全点击点进入会话，点击前后均记录截图和候选框。 | 微信进入目标会话。 | 返回 `TARGET_CLICK_FAILED`；不得读取当前窗口。 |
| 4B | 搜索路径基线截图 | 仅实时首屏未命中时执行。截取操作前窗口，记录当前标题、窗口位置、DPI、当前选中会话摘要。 | evidence 中有 raw/annotated 截图和窗口元数据。 | 截图失败返回 `SCREENSHOT_CAPTURE_FAILED`；不得继续。 |
| 5 | 定位微信搜索框 | 仅搜索路径执行。只允许使用当前可执行布局快照中的侧栏头部区域和本帧唯一控件/视觉/OCR 证据定位微信搜索框，禁止固定坐标或比例兜底。 | 搜索框点击点位于当前快照的侧栏头部及唯一已确认目标 bounds 内。 | 返回 `SEARCH_BOX_NOT_FOUND / WECHAT_UI_LAYOUT_UNRESOLVED`；零点击结束。 |
| 6 | 聚焦并清空搜索框 | 仅搜索路径执行。点击搜索框，执行清空动作；允许最多 2 次轻量重试。 | OCR/控件状态确认搜索框为空，或已回到占位符状态。 | 返回 `SEARCH_BOX_CLEAR_FAILED`；不得输入短码。 |
| 7 | 输入短码 | 仅搜索路径执行。按“人工复制短码后粘贴搜索”的习惯输入 `remark_code`，默认使用剪贴板粘贴；粘贴前后必须有短随机停顿；不得高速逐字输入；不得输入其他客户信息。 | 搜索框内容或搜索结果上下文能确认本次查询为该 `remark_code`。 | 返回 `SEARCH_INPUT_VERIFY_FAILED`；清理搜索状态并释放锁。 |
| 8 | 等待搜索结果稳定 | 仅搜索路径执行。等待搜索结果刷新，至少两帧 OCR 结果稳定，或达到配置超时。 | 候选结果列表稳定。 | 返回 `TARGET_SEARCH_TIMEOUT`；不得点击不稳定结果。 |
| 9 | 解析候选结果 | 仅搜索路径执行。只接受“联系人/会话标题/备注”包含 `remark_code` 的候选；单纯消息内容命中不能作为目标。 | 唯一候选包含 `remark_code`。 | 0 个候选返回 `TARGET_SEARCH_NOT_FOUND`；多个候选返回 `TARGET_SEARCH_AMBIGUOUS`。 |
| 10 | 点击唯一候选 | 仅搜索路径执行。使用候选行安全点击点进入会话，点击前后均记录截图和候选框。 | 微信进入候选会话。 | 返回 `TARGET_CLICK_FAILED`；不得读取当前窗口。 |
| 11 | 二次确认目标 | 进入会话后 OCR 标题/备注/当前选中行，必须确认包含 `remark_code`。`display_name` 只能辅助，不能替代短码。 | `target_confirmed=true`，确认来源写入 evidence。 | 标题明确不含目标短码且本轮由 visible 坐标点击进入时，进入步骤 11A；其他情况返回 `TARGET_CONFIRM_FAILED / TARGET_NOT_CONFIRMED_FOR_MESSAGES`，不得读取消息。 |
| 11A | 安全误点短码重新定位 | Sidecar 返回 `C2_VISIBLE_TARGET_STALE_AFTER_CLICK`，并证明已点击但未读取消息区、未操作媒体、未输入或发送。Worker 保持同一 UI 锁和 `read_run_id`，复核 `authorization_revision`，丢弃旧坐标/候选/截图，重新截取安全基线后直接进入 `search_by_remark_code`，不再重复 visible 坐标点击。 | 本轮只允许一次恢复定位，搜索结果必须是目标短码的唯一候选；点击后仍必须重复步骤 11 的 private + 精确短码确认。 | 授权失效、搜索无唯一候选、恢复点击仍不是目标、无法恢复搜索基线或任何副作用已越过标题校验边界时，立即结束当前客户；不创建 handoff，不影响其他短码。 |
| 12 | 读取消息 | 复用 OmniAuto `messages` 解析能力读取当前会话可见消息，输出 `sender_role_hint / message_type / content / occurred_at / raw_payload`。 | 返回消息列表或明确空结果，并带 `target_confirmed=true`。 | 读取失败返回 `MESSAGE_READ_FAILED`；不得伪造空成功。 |
| 13 | 生成结果与证据 | 输出 `read_run_id / conversation_id / remark_code / target_mode / target_confirmed / messages / evidence / step_events`。 | Worker 可上报后端 `/wechat/messages/ingest`。 | 结果缺关键字段视为 `C2_MESSAGE_READ_RESULT_INVALID`。 |
| 14 | 清理或保持安全状态 | 成功读取后可保持目标会话打开，供 `pre_send_refresh` 后继续发送；失败时清理搜索框或回到安全状态。 | 不影响下一次 add_friend / scan / send 操作。 | 清理失败记录 `SEARCH_STATE_CLEANUP_FAILED`，但不得继续执行发送。 |

安全误点恢复必须有以下定向验收，不得只测函数返回值：

1. 目标在首帧任意行唯一命中，点击前列表因其他会话变化而重排，旧坐标打开任意非目标会话；标题复核后必须零消息区 OCR、零媒体动作、零 ingest、零 Brain 和零发送。
2. 上述误点后授权仍有效；Worker 必须废弃旧坐标，在同一 UI 锁和 `read_run_id` 内重新截取安全基线并直接按目标短码精确搜索一次，最终只读取一次目标会话，不得对误点会话建立任何消息事实。
3. 误点后授权被撤销时不得重新定位；第二次仍点错时必须有限结束，不得循环点击。
4. 点击后标题已包含目标短码，但它是群聊或类型不明时，必须继续返回 `C2_GROUP_CHAT_NOT_ALLOWED / C2_CONVERSATION_TYPE_UNKNOWN`，不得借恢复分支降级为 private。
5. 当前客户恢复失败只结束该客户，必须继续队列中其他短码；恢复成功不写入失败冷却，也不创建 handoff。

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

#### 6.0.4.3.1 本帧动作绑定与长期消息身份的唯一权限边界

本节同时适用于 `authorized_read`、`pre_send_refresh`、语音和图片，不允许各入口、各媒体类型
重新定义一套“是不是同一条消息”的算法。必须先区分两个互不替代的对象：

| 对象 | 生命周期 | 唯一用途 | 所有者 | 明确禁止 |
|---|---|---|---|---|
| `frame_action_binding` | 同一 UI 事务中，从 prepare 最新帧到本次物理触发前；动作结束、取消或画面相关变化后立即失效 | 证明“Worker 选中的本帧唯一目标仍可安全操作” | Sidecar 产生原始观察与 token，Worker 校验并决定是否准入 | 生成/继承 `worker_stable_id`、生成 source key、查询历史 Ledger/Outbox、声称跨轮是同一条业务消息 |
| `committed_message_identity` | Worker 正式提交后跨读取轮次长期存在 | 消息去重、顺序、Ledger、Outbox、后端 ingest 和 Brain | Worker 唯一决策；后端只校验和保存 | Sidecar 或后端根据外观、坐标、正文、时长重新判断、修复或覆盖 |

`frame_action_binding` 不是“临时身份证”，而是一张只对本次 UI 动作有效的操作票。其输入只允许来自
同一已确认 private 会话的最新帧：`selected_action_token + pre_frame_id +
selected_pre_observation_id + selected_target_fingerprint + candidate_group_count +
当前有序观察摘要`。其中目标指纹只能描述本帧局部目标、角色/类型、必要邻接结构和局部几何，
不得写入 `source_message`，不得参与跨轮 source key。

职责和调用顺序固定为：

```text
Sidecar prepare：观察最新画面，返回候选、局部目标证据和一次性 token，零 UI 动作
-> Worker：结合完整序列、历史 checkpoint、授权和本地事务状态选定唯一候选
-> Worker：创建 action_id、不可复用 reserved_worker_stable_id 和 ActionJournal
-> Sidecar execute：只核验原 token 未消费、仍是同一会话、当前唯一局部目标仍满足本帧动作绑定
-> 无相关变化且目标唯一：执行一次物理动作并返回动作回执
-> 相关变化/目标消失/多候选：零点击返回原始观察和具体原因，不输出身份结论
-> Worker：按当前 operation_phase 决定重新仲裁或具体失败终态
-> 有效 confirmed/failed 动作回执：Worker 经唯一身份提交门提交预留号
-> ambiguous/可能已触发：只走 ActionJournal 隔离恢复，严禁重复动作
-> 后端：只按 Worker 已提交的 source_message_key 保存、去重和结算
```

首次动作准入不得形成循环依赖：

1. 新语音/图片第一次物理动作前没有本次 `confirmed_action_mapping` 是正常状态，不得因此拒绝动作。
2. Win32 OCR 没有真实 `native_source_message_id` 是正式支持路径，不得因此拒绝正常首次动作。
3. `reserved_worker_stable_id` 只是不可复用预留号，不是正式消息身份，不得用于历史去重。
4. Worker 只能在 Sidecar 本帧目标证据唯一、授权有效、当前消息序列不存在可见冲突时允许动作；
   不能用“角色 + 类型 + 时长 + 位置”生成长期身份，也不能要求正式长期身份反过来作为首次点击前提。
5. Sidecar 可以返回 `cancelled_before_trigger`、目标消失、多候选或画面相关变化，但不得返回
   `same_business_message=true/false` 等业务身份结论；Worker 不得把 Sidecar 的局部目标相似结果
   直接转换成跨轮身份。
6. Sidecar 内部可继续构建为 OCR 兼容使用的旧 `messages[]/message_envelope`
   投影，但它们只是内部过程数据。普通 CLI、常驻 daemon、冻结 Windows 客户端
   和旧 `main()` 四类公开输出在返回 Worker 前，必须对完整 JSON 递归删除
   `same_business_message` 、`worker_stable_id`、`source_message_key` 和
   `commit_basis`；不得删除 Worker 本次动作合法预留的
   `reserved_worker_stable_id`，也不得删除原始观察、动作回执或错误证据。
7. Worker 是 Sidecar 输出合同的第一道权威门禁：必须在语音/图片动作、
   Ledger、Outbox、后端 ingest 和 Brain 之前递归检查整个 Sidecar 返回；任一
   层级仍残留上述四个越权字段，统一返回
   `C2_SIDECAR_IDENTITY_CONTRACT_INVALID` 且零业务事实。后端不从 Sidecar 响应
   重建身份，只接收 Worker 唯一身份提交门已正式提交的消息。

媒体动作准入与结果矩阵固定为：

| 当前情况 | Sidecar 输出 | Worker 处理 | 是否允许后续动作 |
|---|---|---|---|
| 最新完整画面存在未处理语音 | 同帧归并后的有序 observations 和当前帧 bounds | 选择 `screen_order` 最大的一条语音，创建 action ID、预留号和 Journal | 允许执行一次该语音动作 |
| 没有语音但存在未处理图片 | 有序 observations 和当前帧 bounds | 选择 `screen_order` 最大的一张图片，创建 action ID、预留号和 Journal | 允许执行一次该图片动作 |
| 动作前新增消息、滚动或坐标改变 | 返回最新完整帧；旧 bounds 失效 | 不比较旧 observation/坐标/像素；按最新帧重新选择当前一条 | 旧坐标禁止使用，新动作仍可继续 |
| 当前帧目标区域无效、角色/类型冲突或菜单证明不是目标类型，且明确零触发 | `cancelled_before_trigger` + 具体证据 | 烧毁预留号，完整复读后重新按固定顺序选择 | 原动作不重试，可建立新动作 |
| 动作结果唯一：语音出现唯一新增转写；图片得到唯一新剪贴板字节和完整回执 | action/reserved/pre/post/result receipt | Worker 验证后绑定预留 ID，提交 `committed_completed/failed` | 该动作终结，完整复读后继续剩余媒体 |
| 已触发但没有结果、出现多个可能结果、点错对象或回执矛盾 | `identity_unresolved` + 完整故障证据 | 零正式消息、零 Brain、零 Handoff；task/Flow=`technical_failed`，释放 UI 锁，Worker=`faulted` | 禁止重复点击，停止新接单 |
| 布局无法建立 | `C2_PRE_SEND_LAYOUT_INVALID` 或对应普通读取布局错误；零点击 | 同样按客户端技术故障终态结算 | 禁止动作 |

特别禁止把跨轮长期身份算法直接用作首次点击门禁。动作前无需证明当前媒体在旧帧中是哪一个对象，
也不得要求新媒体已具有真实原生 ID、confirmed action 或正式 `worker_stable_id`。Sidecar execute 只能
在它自己刚取得的最新帧中定位本次获批类型的当前目标并返回实际结果；Worker 是唯一有权把该结果与
预留 ID 绑定并调用 `commit_message_identity` 的组件。两条相同三秒语音按“最下方一条 -> 转写结果 ->
完整复读 -> 剩余一条”处理，不建立 A/B 的跨帧猜测。

#### 6.0.4.4 发送前新消息统一仲裁（`pre_send_refresh`）

`chat_reply` 发送前必须重新读取当前会话，避免把旧上下文生成的回复发出去。本节是
文字、语音、图片及任意组合在“Brain 已产生旧回复后又到达”场景的唯一处理流程，禁止再按
媒体类型建立互相冲突的局部分支。

`pre_send_refresh` 是 Worker 当前执行阶段，不是后端 `read_reason`。正常链路和崩溃恢复
都必须保留原始 `authorization_read_reason`，继续校验同一 Worker、同一会话、正式短码、
`private`、授权版本和批次；但不得重复执行首次好友激活、召回状态转换或其他只属于
`authorized_read` 的业务动作。

发送前比较的对象是“事实是否新增”，不是“历史消息身份是否重新成立”。后端必须在本次 MessageBatch 的 Brain 输入冻结时，
从该输入实际使用的有序正式 MessageEvent 构造唯一不可变 `pre_send_fact_checkpoint`，写入现有
`MessageBatch.ai_request_snapshot.pre_send_fact_checkpoint`；不得新增数据库同义列。生成 reply_action 后，后端在现有 batch/reply
查询响应中只读返回同一 checkpoint，并用 `conversation_id + batch_id + reply_action_id` 绑定。Worker 在第一次取得该响应时原样校验并
持久化到本地 SQLite，后续重试复用原副本；重启恢复只能重新读取后端同一 batch 的已冻结 checkpoint 并核对摘要，禁止用当前截图、
当前数据库最新尾部或新的 `identity_checkpoint` 重建旧 checkpoint。缺失、摘要不一致或 batch/reply 绑定不一致必须在任何微信操作前以
`C2_PRE_SEND_FACT_CHECKPOINT_INVALID` 失败关闭，禁止发送、禁止猜测。该对象只用于发送前连续性比较，不新增第二套业务消息身份，
不得作为 source key、Ledger、Outbox 或 ingest 的提交依据；Sidecar 永远不接收或返回该对象。`messages/ingest` 请求和其他现有请求
不得新增 `pre_send_fact_checkpoint` 顶层字段。checkpoint 至少包含：

| 字段 | 固定语义 |
|---|---|
| `conversation_id / batch_id` | checkpoint 内容身份；创建后不可修改。 |
| `reply_action_id` | 只属于后端响应和 Worker 本地绑定外壳，不进入 checkpoint 内容摘要；证明当前待发送回复确实引用该 checkpoint。 |
| `checkpoint_revision` | 本次不可变事实快照版本；同一回复重试必须复用，不得读取当前画面后改写。 |
| `baseline_kind` | 普通消息固定为 `message_tail`；从未包含客户业务消息的首次欢迎语固定为 `friend_welcome_empty`。禁止用“空且不完整”冒充欢迎语基线。 |
| `authoritative_frame_source` | 普通消息只允许来自合法完整 `initial_read` 或 `final_read`；欢迎语空基线固定为 `control_empty`。纯文字没有媒体 UI 动作时，`initial_read` 是合法权威画面。 |
| `committed_tail[]` | 按客户真实时间顺序保存最近完整尾部的 `worker_stable_id + sender_role + message_type + item_state + stable_fact_signature + continuity_basis + continuity_signature`；已提交媒体还必须冻结 `commit_basis + action_receipt_digest + reply_fact_evidence`，ID 只来自已提交事实。 |
| `tail_complete=true` | 该尾部在生成 Brain 输入时已完成顺序、角色和媒体终态结算；否则不得生成可发送回复。 |

事实比较前和进入 claim-send 前的两处 Worker 检查都必须先验证同一份生产
`build_send_context_guard()` 快照：`ok=true`、`layout_snapshot_id` 非空、消息视口边界合法、
`message_count` 与有序 `sequence` 数量一致、序号连续、业务摘要等于该序列的规范化
SHA-256、`bottom` 与序列末项自洽，且不得使用原始整屏像素哈希或量化坐标生成业务摘要。Worker 还必须使用与
Sidecar 相同的共享纯业务投影规则，从本次响应的 `observations` 重新生成
有序 `sequence`，逐项核对数量、角色、类型、正文摘要和媒体状态；两者必须完全一致，
不得只分别验证 guard 和 observations。消息视口边界与每行 bounds 另存为当前帧几何，
只证明本帧布局、排序和点击区域有效，不参与跨帧业务相等、checkpoint 连续性或长期身份。
共享投影不生成、继承或判断 `worker_stable_id`，也不得归并语音/图片或映射前后帧动作对象。
唯一一次被动重读返回后必须重新执行全部布局及同帧绑定校验，不能把重读布局错误降级成序列错误。任一项不成立均是
`C2_PRE_SEND_LAYOUT_INVALID`：不消耗被动重读预算，零 Handoff、零 claim-send、零输入、零媒体动作和零发送，
原 reply task/Flow 按技术故障结算、释放 UI 锁并将 Worker 置为 `faulted`。

`stable_fact_signature` 只用于 checkpoint 与当前观察的事实连续性比较：文字/system 使用规范化正文摘要；已转写语音使用角色、类型、完成状态、规范化转写和可获得时的规范化时长。图片不得使用微信气泡截图、ROI 截图、dHash/pHash、坐标或媒体状态相同宣布跨帧等价；这些材料最多是原动作本帧证据或诊断。只有当前图片已由 Worker 的正式身份链和 confirmed receipt 唯一确认时，才可作为已提交历史事实继续比较；唯一新图片必须进入当前媒体动作链，通过实际复制图片字节 SHA 与动作回执取得自己的正式身份。若代码无法判断该图片是否已处理、或无法把实际动作结果唯一绑定到本次 action，固定返回具体技术错误，旧回复作废、零发送、零重复图片动作、零 Handoff、零飞书，并按 `technical_failed + Worker faulted` 结算。坐标、时长单值、`frame_visual_id`、OCR observation ID、Sidecar 临时 anchor 或正文相同本身均不能充当业务身份。当前观察不得预先携带或生成历史 `worker_stable_id`。

语音的“物理身份连续”与“发送所依赖的业务事实等价”必须分开；图片执行上一段更严格的安全规则：

1. 真实原生消息 ID，或前后静态事实与稳定媒体局部证据形成的唯一双侧连续性，可记录 `physical_identity_confirmed=true`。
2. 已提交末尾语音无原生 ID 且无后侧邻居时，任何实现都不能根据完全相同的 Win32 画面证明物理身份，不得伪造这种结论。只有同时满足以下全部条件，才允许使用 `continuity_basis=terminal_committed_fact_equivalence`：该 checkpoint 项位于冻结尾部；原事实是 `confirmed_voice_action/prior_confirmed_voice_action` 正式提交且 action receipt 摘要完整；当前帧是未截断的最新完整尾部；其前全部 checkpoint 事实按顺序唯一匹配；当前对象的角色、类型、终态与 `reply_fact_evidence` 精确一致；不存在 unknown、缺行、前部插入或多个匹配解释。该结果只证明“旧 AI 回复所依赖的业务事实没有可观测变化”，必须返回 `physical_identity_confirmed=false + match_basis=terminal_committed_fact_equivalence`。图片禁止进入该分支。
3. 上述事实等价结果只能用于本 reply_action 的 `pre_send_refresh` 发送门；不得给当前 observation 回填旧 ID，不得产生 source key、Ledger、Outbox 或 ingest，不得进入普通 `authorized_read`、媒体提交或崩溃恢复。
4. 任一条件缺失时先进入本节唯一比较器：若属于重复弱事实造成的零个/多个重叠解释，只允许一次受限上下文扩展；扩展后仍不能唯一证明连续，才返回具体 `checkpoint_not_continuous` 原因。不使用“暂时看不清”、时间等待或自动猜测。

后端冻结普通 checkpoint 时，只能把最新合法完整 `initial_read/final_read` 的 observations 通过该帧
`slot_ledger_states.source_message_key` 或正式 `worker_stable_id` 唯一投影到本次 Brain 实际使用的 MessageEvent。
无法唯一投影时 checkpoint 必须不完整；严禁静默使用数据库整段历史补齐。`friend_welcome_empty` 则必须明确保存
空 `committed_tail + tail_complete=true + control_empty`；发送前当前帧仍无业务消息才允许发送，出现任意消息即
按唯一新后缀作废欢迎语并进入正常消息链。

比较结果必须 MECE 且只能进入以下一项：

| 比较结果 | 必要条件 | 处理 |
|---|---|---|
| `checkpoint_equal` | private、短码、授权、批次均有效；当前帧已确认是最新完整尾部；全部 checkpoint 项按顺序唯一匹配，数量相同且无未知业务行；并且完整顶部边界或既有正式身份/回执链能排除“满屏同类事实发生头部替换”；图片必须具有正式身份链唯一证据和 confirmed receipt；语音可以具有物理身份强证据，或仅在上述全部条件成立时以 `terminal_committed_fact_equivalence` 匹配 | 不重新提交或改写任何历史身份，直接得到 `unchanged_sendable`。语音事实等价匹配必须同时保留 `physical_identity_confirmed=false`。若无法排除满屏替换，必须进入一次受限上下文扩展，不得按相等放行。 |
| `checkpoint_unique_prefix_with_suffix` | 全部 checkpoint 项按顺序形成当前完整尾部的唯一前缀，且其后存在非空、连续、无缺口的新 observation 后缀 | 立即关闭旧发送门并将旧 reply_action 置为 superseded；只把新后缀送回既有文字/语音/图片统一处理链。历史项不重复入库。 |
| `checkpoint_unique_viewport_slide_with_suffix` | checkpoint 的唯一连续尾部精确对齐当前视口头部，checkpoint 前缀仅因正常滚屏离开可见区，当前视口后缀是新到消息 | 视为正常视口滑动；立即 supersede 旧 reply_action，保留唯一重叠段，只把新尾部送回统一处理链。 |
| `checkpoint_continuity_context_expansion_required` | 当前视口因重复业务事实无法得到唯一重叠段，且本次尚未执行扩展读取 | 同一 Flow/UI 锁内最多执行一次受限上下文扩展；零媒体动作、零发送、零 Brain，然后从头执行本表。 |
| `checkpoint_not_continuous` | 扩展后仍无重叠/多解，或证明发生缺行、替换、中间插入、换序、未知业务行或签名冲突；正常头部滚出不属于本项 | 禁止旧回复；返回 `C2_PRE_SEND_MESSAGE_SEQUENCE_ALIGNMENT_FAILED`，保留具体 reason 和帧证据，不进入时间等待、Handoff 或第二次扩展。 |

若 checkpoint 为 `文字1 -> 文字2 -> 已转写语音A`，当前为 `文字1 -> 文字2 -> 语音A -> 新语音B`，则新后缀 B 使旧回复立即作废。若当前仍只是 `文字1 -> 文字2 -> 末尾语音`，Win32 无法区分“原 A 仍在”与“物理上新 B 以完全相同事实占据原位置”时，不宣布它们物理身份相同；只有语音满足 `terminal_committed_fact_equivalence` 的全部条件时才可允许原回复继续，且该分支不会点击媒体、不会让当前对象继承 A 的编号。图片无法由正式身份链和 confirmed receipt 唯一确认时，不做事实等价放行，也不得转人工掩盖；返回具体技术错误并进入 `technical_failed`。

普通 `authorized_read`、媒体动作后正式提交、跨轮 checkpoint 恢复和历史弱媒体身份仍执行 6.0.4.3 的原强证据/
双侧规则。禁止通过修改 `_compatible()` 全局放行末尾媒体、把“位于最后一条”作为身份依据，或新增
`tail_media_identity_exception`。Sidecar 只返回当前有序观察、完整尾部/截断证据和布局事实；Worker 独占上述比较
与发送门决策；后端只保存既有正式事实和结算 superseded，不根据截图重新判断身份。

统一流程：

```text
C2保持当前单会话Flow和UI锁等待Brain/Guard
-> Brain批准旧reply_action
-> 对当前会话取一张新物理帧执行pre_send_refresh
-> 先将当前最新完整尾部与本reply_action的pre_send_fact_checkpoint比较
-> 完全一致：不重判历史身份；有强物理证据时记录physical_identity_confirmed=true，无原生ID的已提交末尾媒体只有在terminal_committed_fact_equivalence全部条件成立时可以physical_identity_confirmed=false进入unchanged_sendable
-> checkpoint为当前序列唯一完整前缀且有新后缀：立即关闭旧回复发送门，只处理新后缀
-> checkpoint的唯一连续尾部=当前视口头部且有新尾部：视为正常滚屏，立即关闭旧回复发送门，只处理新尾部
-> 零个/多个重叠解释且未扩展：同一Flow/UI锁内只执行一次受限上下文扩展，零媒体点击/零发送/零Brain
-> 扩展后仍无重叠/多解，或证明替换/中间插入/换序/未知/矛盾：禁止发送，返回具体序列错误；当前task/Flow=technical_failed并释放UI锁，Worker=faulted；不转人工、不继续等待
-> 如完整/增强OCR后仍不能判定具体原因，直接返回对应的正文/序列/角色/布局错误，不进入等待
-> 在关系尚未收敛期间不claim-send、不输入、不点击发送，保持原Flow/UI锁/任务租约；一旦进入技术失败必须按上一条释放
-> 按完整消息序列从上到下收敛新文字、语音、图片、self和system消息
-> 文字直接走统一身份提交门；语音优先、图片随后，同类型每次只操作最新画面screen_order最大的一条可执行对象
-> 每次媒体UI动作后重新读取完整消息视口并重做统一序列对齐
-> 把动作期间新到达的文字/语音/图片加入同一最新顺序，不丢弃、不重排已确认旧身份
-> 当前已观察尾部全部形成正式消息/确定失败事实时生成final_read；命中reidentification_failed/worker_environment_failed/hard_stop则直接按该终态结算，不伪造final_read
-> 新正式事实先落本地Outbox，再按原messages/ingest合同上报
-> 后端在同一事务内作废旧reply_action，以最新未回复连续尾部创建或复用唯一当前batch
-> 只有当前有效batch可进入Brain；过时Brain结果一律不得发送
-> 新reply_action再完整执行pre_send_refresh
```

“收敛”不要求等待固定的无消息时间窗。一张最终权威帧已覆盖当前完整消息视口、序列对齐成功且已观察
新对象均有终态，即可结算本次复读。之后到达的新消息仍会被下一次 `pre_send_refresh` 或 S1 发现，不能用无限等待追求
不存在的“绝对静止”。

一次重识别成功不是业务终态；它只是把新目标送回同一收敛器。收敛器的最终判断必须且只能进入下表一行：

| 统一结果 | 证据条件 | 旧 reply_action | 后续处理 |
|---|---|---|---|
| `unchanged_sendable` | private+短码+授权有效，`pre_send_fact_checkpoint` 与当前最新完整尾部唯一完全一致，无新 customer/self，也无未知、截断或多解释；已提交历史身份不重判 | 保持 current | 允许进入 claim-send；S0/S1/S2 仍各自取新帧。 |
| `new_customer_facts_committed` | 一条或多条新客户文字/语音/图片均已形成按画面顺序排列的正式事实 | 后端入库事务原子置 `superseded` | 根据最新完整尾部创建/复用新 batch 并调用 Brain；禁止发送旧回复。 |
| `new_self_fact_committed` | 出现能证明是销售人工发送、且不属于本系统 AI sent_ack 的新 self 文字/语音/图片事实 | 取消/作废 | 入库 self 事实，按销售已接管处理，不调用 Brain；只要角色和新旧已确认，不需为了理解 self 媒体内容再做转写/Vision。 |
| `new_system_fact_committed` | 已唯一确认新 system 行的身份、顺序和类型 | 立即禁止发送 | 先入库并重做一次仲裁；能证明拒收/关闭/硬状态则进 `hard_stop`，否则不单独启动 Brain，重读确认无其他新 customer 事实后才可恢复原发送判断。 |
| `message_fact_unresolved` | 已排除正常唯一尾部追加和唯一视口滑动；一次受限上下文扩展后，序列已能证明连续，但其中仍存在文字无法读取、角色无法确认、system 无法分类等真实消息事实不确定 | 取消/作废 | 零点击、零 Brain，保留具体 source error 和证据；沿用既有具体消息事实 Handoff 规则。不得把“正常头部滚出”、连续性多解或媒体动作代码不变量失败塞入本项。 |
| `message_sequence_technical_failed` | 一次受限上下文扩展后仍无重叠/多解，或已证明历史替换、中间插入、换序、unknown/证据矛盾 | 取消/作废 | 零输入、零发送、零 Brain、零 Handoff、零飞书；保留证据，task/Flow=`technical_failed`，释放 UI 锁，Worker=`faulted + can_pull_tasks=false`。 |
| `media_action_technical_failed` | 当前媒体动作没有结果、出现多个可能结果、点错对象、回执矛盾，或 Sidecar/Worker 对同一动作产生冲突 | 取消/作废 | 零重复点击、零正式消息、零 Brain、零 Handoff、零飞书；保存证据，task/Flow=`technical_failed`，释放 UI 锁，Worker=`faulted + can_pull_tasks=false`。 |
| `worker_environment_failed` | 最新帧无法建立合法微信基本布局 | 取消/作废 | 按代码缺陷收口：上报 `C2_PRE_SEND_LAYOUT_INVALID` 和完整证据，将当前任务/Flow 结算为技术失败，释放 UI 锁，Worker 进入故障状态并停止新接单。不 handoff、不自动移窗/重标定/重试。 |
| `customer_media_committed_failed` | 已唯一确认客户语音/图片对象，且转写、复制、Vision 或正式内容处理已得到确定失败终态 | 取消/作废 | 失败事实先入库，再按现有 L1 规则幂等 handoff 一次；不调用 Brain。 |
| `hard_stop` | 授权明确撤销、客户/短码/private 无法确认、销售已接管、客户拒绝联系、会话关闭或发送可能已发生但结果未知 | 禁止发送并按原硬门禁结算 | 保持原 L3 处理；本节不放宽任何安全硬门禁。 |

类型化收敛规则：

| 新对象 | 首次识别确定时 | 相关消息区变化时 | 一次重识别失败时 |
|---|---|---|---|
| customer 文字 | 按完整 `new_suffix` 和统一序列提交；相同正文的两条消息仍依赖序列位置与邻居分开 | 对最新帧仅完整 OCR/序列对齐一次 | 返回文字无法读取、序列无法对齐或角色无法确认的具体错误；不伪造正文。 |
| 语音 | Worker 从最新完整画面选择 `screen_order` 最大的一条未处理语音，落 ActionJournal；Sidecar 在执行帧内定位并只点击一次；唯一新增转写和动作回执经 Worker 绑定后提交 | 旧坐标直接失效；从最新帧重新按固定规则选择当前一条，不跨帧认 A/B | 已触发却无结果、多结果、错对象或回执矛盾是技术失败；对象与回执唯一、微信明确转写失败才形成 `committed_failed`。 |
| 图片 | Worker 从最新完整画面选择 `screen_order` 最大的一张未处理图片，落 ActionJournal；实际复制图片字节 SHA 和完整动作回执经 Worker 绑定后提交 | 旧坐标直接失效；从最新帧重新选择，不用旧坐标、邻居或气泡截图认身份 | 已触发却无结果、多结果、错对象或回执矛盾是技术失败；对象与回执唯一、复制/Vision 明确失败才形成 `committed_failed`。 |
| self 文字/语音/图片 | 确认为新销售人工 self 且排除 AI sent_ack 后直接作废旧回复 | 使用同一次最新帧重识别 | 角色仍无法确认时返回具体角色错误；已确认 self 则无需理解媒体内容。 |
| system 行 | 先入库，按已定义的拒收/关闭/普通系统状态分类 | 使用同一次最新帧重识别 | 身份或语义仍无法确认时返回具体 system 错误；不伪装为 customer 正文。 |
| 混合或连续新消息 | UI 动作固定语音优先、图片随后且每次只操作一条；每次动作后重读并吸收新到达对象。全部动作终结后，最终入库才按最新权威完整画面的 `screen_order` 排序 | 一次重识别必须同时重建整个最新序列，禁止只重试某一种媒体；图片阶段发现新语音时，当前图片终态后先回到语音阶段 | 返回第一个阻断收敛的具体对象、类型、顺序范围和错误；已提交事实不回滚。 |

唯一重识别规则：

1. 整张微信画面或完整消息视口摘要只能用来发现“相关消息区是否变化”，不能作为单条文字、语音或图片的长期身份或最终目标证明。
2. 工具栏、输入框、闪烁光标、侧栏和其他会话变化必须从该变化判定中排除。消息视口内的 GIF/动画表情帧、滚动条淡入淡出、鼠标悬停效果、语音播放动画、播放进度、红点和选中外框也必须排除。单条目标证明只能绑定当前会话、消息类型/角色、目标气泡、必要上下邻居、序列位置与 confirmed action receipt。
   Sidecar 必须将两类证据分开：`selected_target_fingerprint` 只包含前述单条目标局部材料；`message_viewport_change_digest` 只表示标准化消息序列是否变化。禁止把后者塞入前者后再用“目标指纹不一致”代替重识别。
   `message_viewport_change_digest` 禁止直接哈希消息区原始 RGB 像素，也禁止包含相对/绝对坐标、OCR 框、气泡尺寸、64 分桶、frame/observation ID。必须对生产解析器输出的有序观察列表做规范化 JSON 后计算摘要；每项严格只包含 `screen_order + sender_role + message_type + normalized_content_signature + media_state`。文字/system 使用规范化正文摘要；已转写语音使用规范化转写正文、时长和终态形成内容签名；已提交图片只允许使用 Worker 正式回执中的图片字节 SHA/结构化结果摘要，未处理图片的内容签名不得由气泡像素伪造。本摘要只回答业务事实是否变化，不得用于媒体身份继承、动作对象映射或同帧归并。当前消息行 bounds 继续随原始观察保存，只供本帧排序、目标内部点击与诊断；产生新截图后旧 bounds 立即失效。
   无法建立消息视口布局时必须返回 `C2_PRE_SEND_LAYOUT_INVALID`；不得回退到整张微信画面哈希。
3. 物理动作尚未触发时发现相关消息区变化：旧 bounds 立即失效；若 action 已建立则结算为 `cancelled_before_trigger` 并烧毁预留号，然后只获取一张最新不可变帧。
4. 在最新帧上只执行一次完整当前会话解析、OCR 和角色判定。Worker 按固定顺序选择当前一条媒体；Sidecar 只在该帧内定位，不做旧目标跨帧映射。不得嵌套 prepare 循环、四次取消、多个 20 秒等待、`recoverable_hold` 或 120 秒定时恢复。
5. 物理触发前画面再次变化时，不按旧坐标点击；返回最新帧重新进入固定选择规则。若实现无法形成有效当前帧或选择结果自相矛盾，按技术错误零 UI 终止，不转人工。
6. 物理动作已触发或可能已触发后发现无结果、多结果、错对象或回执矛盾，必须保留 ActionJournal `identity_unresolved` 并立即按技术故障收口；严禁重复点击，也不得创建 HandoffEvent。
7. 语音唯一一次物理点击后的有界多帧正文等待可以保留；它只能读取画面，不得再次点击，且必须结算为 `committed_completed / committed_failed / identity_unresolved` 之一。图片已触发复制后的结果确认同理；`identity_unresolved` 不再进入无 UI 多轮补证或超时 Handoff。

错误分类硬约束：

| 错误码 | 唯一含义 | 结算 |
|---|---|---|
| `C2_PRE_SEND_FACT_CHECKPOINT_INVALID` | 当前 reply_action 缺少后端冻结 checkpoint，或本地副本与同一 conversation/batch/reply 的内容摘要矛盾 | 在任何微信操作前禁止发送；当前 task/Flow 结算为技术失败并释放 UI 锁，Worker 进入 faulted 且停止新接单；零 HandoffEvent、零媒体动作、零身份猜测。网络暂时取不到 checkpoint 属原可重试网络错误，不得冒充本错误。 |
| `C2_PRE_SEND_TEXT_CONTENT_UNREADABLE` | 最新帧中新文字存在，但正文经完整/增强 OCR 仍不可读 | 当前会话 handoff 一次 |
| `C2_PRE_SEND_MESSAGE_SEQUENCE_ALIGNMENT_FAILED` | 最新完整序列无法与已提交尾部唯一对齐 | 当前会话 handoff 一次 |
| `C2_PRE_SEND_MESSAGE_ROLE_UNCONFIRMED` | 阻断尾部的新对象无法唯一确认为 customer/self/system | 当前会话 handoff 一次 |
| `C2_PRE_SEND_VOICE_TARGET_NOT_FOUND` / `C2_PRE_SEND_IMAGE_TARGET_NOT_FOUND` | 固定当前帧选择规则声明存在待处理媒体，但 Sidecar 在同一最新帧找不到对应物理行 | 客户端合同/实现矛盾；按 `technical_failed + Worker faulted` 结算，零 Handoff、零重复 UI。正常的新消息变化必须重新取得最新帧，不得使用本错误。 |
| `C2_PRE_SEND_VOICE_TARGET_AMBIGUOUS` / `C2_PRE_SEND_IMAGE_TARGET_AMBIGUOUS` | 同一当前帧内归并/选择结果仍自相矛盾，或一个动作出现多个可能结果 | 客户端技术故障；取消旧回复，零发送、零重复媒体动作、零 Handoff、零飞书，task/Flow=`technical_failed`，Worker=`faulted`。两条外观相同但位于不同行的媒体不是 ambiguous，必须按固定顺序逐条处理。 |
| `C2_PRE_SEND_MESSAGE_VIEWPORT_CHANGED_AGAIN` | 动作触发前画面再次变化且实现没有按最新帧重新进入固定选择规则 | 客户端技术故障；零 UI、零 Handoff，保存证据并 fault Worker。 |
| `C2_PRE_SEND_SYSTEM_CONTENT_UNREADABLE` | 已唯一确认为 system 行，但完整/增强 OCR 后正文仍不可读 | 当前会话 handoff 一次 |
| `C2_PRE_SEND_SYSTEM_CLASSIFICATION_UNRESOLVED` | system 正文已读取，但无法唯一归类为普通状态或已定义的拒收/关闭/硬状态 | 当前会话 handoff 一次 |
| `C2_PRE_SEND_LAYOUT_INVALID` | 最新帧无法建立完整标题/消息视口/输入区布局 | 禁止旧回复，记录代码 Bug 证据，当前任务/Flow 技术失败终态，释放 UI 锁，Worker 故障停止新接单；零 HandoffEvent，零自动恢复 |

`C2_VOICE_PREPARE_TARGET_UNSTABLE` 以及文字/图片同类“暂时不稳定”错误从
`pre_send_refresh` 正式链路退出；当前本地实现的生产调用点必须为零，架构复审必须通过静态扫描和定向链路验证该约束。
`C2_REPLY_CONTEXT_RECOVERY_FAILED` 不得包装本表任一文字/语音/图片错误；它只保留给
6.0.4.4 之外真正的会话/批次/授权上下文恢复失败。上报必须同时携带
`source_message_type + min/max_screen_order + candidate_count + before/after_frame_id + evidence_refs`，
不得先改成笼统错误再结算。

后端边界：

- 本节不创建 `recoverable_hold`，不增加新前端 Task 或 Worker 补发任务。
- 新客户事实成功入库时，在不存在其他未关闭硬 `HandoffEvent` 的前提下，同一事务作废旧回复并创建/复用最新 batch。
- 文字正文不可读、角色不可确认、system 无法分类等真实消息事实错误继续按既有具体规则处理；媒体动作无结果、多结果、错对象、目标选择合同矛盾和回执矛盾属于技术故障，后端必须拒绝为这些错误创建 HandoffEvent 或发送飞书。
- `C2_PRE_SEND_LAYOUT_INVALID` 是代码缺陷终态，不是客户业务 Handoff 原因；后端禁止因该错误创建 HandoffEvent 或发飞书。
- 已经因高意向、客户拒收、销售实际接管、当前 customer 媒体确定失败或其他 L3 原因建立的开放 HandoffEvent，不得仅因迟到消息入库而自动关闭或重启 Brain。
- 由旧缺陷错误创建的 `C2_REPLY_CONTEXT_RECOVERY_FAILED` handoff 必须经人工确认无销售接管/拒收/关闭后做一次数据修复；禁止新增“所有迟到消息自动关闭 handoff”的全局兼容旁路。

崩溃恢复不能重启本节的一次重识别预算。动作未触发而已有具体错误终态时，
重启后只重传原结算；动作已触发或可能已触发时，只恢复原 ActionJournal/Outbox 事实，
不得直接发送、再做一次媒体 UI 动作或生成新错误决策。

`C2_PRE_SEND_LAYOUT_INVALID` 的唯一收口为：

```text
禁止旧reply_action发送
-> 持久化错误码、版本、frame_id、原始/增强截图、OCR与布局证据
-> 将当前task/Flow结算为technical_failed
-> 释放UI锁
-> Worker进入faulted，can_pull_tasks=false，后台明确显示客户端故障
```

到此结束。不转人工、不创建 HandoffEvent、不发飞书、不自动移窗/重标定/重试，
不新增人工解锁、清数据或旧 Flow 恢复功能。修复后通过新客户端版本的正常启动流程重新建立标定；
不为这个未修复 Bug 设计运行时补偿状态机。

`0.9.45` 提交前必须通过以下发送前事实比较组合门禁。测试必须从正式 `pre_send_refresh` 生产入口进入，使用真实持久化
checkpoint、Worker 正式比较器、正式 reply_action 结算和后端数据库，不得直接伪造比较结果或只调用内部对齐函数自证成功：

1. checkpoint 为“文字 1、已提交语音 A、文字 2”，当前完整尾部完全相同，且 A 具有原生 ID 或双侧静态连续性证据：结果必须为 `unchanged_sendable + physical_identity_confirmed=true`，零重复入库、零媒体 UI 动作、零新 Brain、零 handoff，并允许原 reply_action 进入 claim-send。
2. checkpoint 以已经 `confirmed_action/prior_confirmed_action` 提交的语音 A 结尾，无原生 ID 且无后侧邻居：当前完整未截断序列的全部前缀唯一一致、action receipt 摘要完整、角色/类型/终态/规范化转写/可获得时的时长全部精确相等时，必须以 `terminal_committed_fact_equivalence + physical_identity_confirmed=false` 得到 `unchanged_sendable`；同时断言零 ID 继承、零 Ledger/Outbox/ingest、零媒体 UI 动作。
3. 将第 2 项换成已提交末尾图片：仅当现有正式身份链唯一关联同一 `worker_stable_id` 且原记录具有 confirmed receipt 时允许继续；仅有精确内容/ROI 摘要、dHash/pHash、截图位置、OCR observation ID 或帧 ID 相同均不能跨帧继承身份。若生产代码仍无法判定是否为已处理图片，必须零发送、零重复图片动作并以具体技术错误进入 `technical_failed + Worker faulted`，不得创建 HandoffEvent。
4. 当前业务时间线在 checkpoint 后追加新文字、未处理语音、未处理图片或其任意组合：无论旧序列仍完整可见，还是旧头部已正常滚出但存在唯一“旧尾部=新头部”重叠，都必须先原子作废旧 reply_action，只处理唯一新后缀，最终按唯一对齐后的权威业务顺序入库并创建/复用新 batch。
5. 两条相同三秒语音、两张相似图片及新媒体占据旧位置必须经过正式串行生产链：Worker 在最新帧选最下方一条，Sidecar 只在该帧操作一次，Worker 根据实际转写/图片字节和动作回执绑定新 ID，完整复读后再处理剩余媒体。任何新对象不得继承旧 `worker_stable_id`；无结果、多结果、错对象或回执矛盾固定按技术故障收口，禁止 Handoff。
6. 当前完整序列在 checkpoint 后追加第二条同正文文字、同转写正文语音或相同图片：必须保留两个有序事实，新后缀获得自己的正式身份，不得被旧事实去重吞掉。
7. 当前视口发生正常头部滚出且能以“旧尾部=新头部”唯一对齐时必须继续，不得报截断。零个/多个重叠解释时只能受限扩展上下文一次；扩展后仍不唯一，或证明历史替换、中间插入、换序、unknown/矛盾时，固定返回 `C2_PRE_SEND_MESSAGE_SEQUENCE_ALIGNMENT_FAILED`，零输入、零发送、零 Handoff。
8. `authorized_read`、`pre_send_refresh` 和 C4 必须共用“当前帧选择 -> 实际动作结果 -> Worker 正式绑定”的唯一媒体动作链；不得恢复跨帧坐标/指纹认对象，也不得另建旁路。崩溃恢复只续传已经持久化的结果和正式事实，禁止重新操作微信。生产代码和静态门禁中不得出现 `tail_media_identity_exception` 或等价身份特殊放行；`terminal_committed_fact_equivalence` 只是发送门的语音事实等价分支。
9. 发送前完全一致路径必须证明历史 `worker_stable_id` 没有被当前观察重新生成、改写或挂载；Sidecar 返回包仍不得携带 Worker 专属身份结论，后端也不得根据截图重判身份。
10. checkpoint 本地副本缺失时必须从同一 batch 的后端冻结快照恢复；缺失、内容摘要矛盾、conversation/batch/reply 绑定错误或试图用最新 identity checkpoint 代替时，必须在零微信操作下返回 `C2_PRE_SEND_FACT_CHECKPOINT_INVALID`。同一 checkpoint 重试和重启结果必须一致。
11. 无媒体 UI 动作的纯文字 `initial_read` 与媒体操作后的 `final_read` 都必须经过正式后端冻结、batch/reply 查询和 Worker 比较；不得只测试 `final_read`，也不得在投影失败时使用整段数据库历史补齐。
12. `friend_welcome` 必须经正式 control batch、Brain、空 checkpoint、Worker pre-send 和 claim-send：当前仍空允许欢迎语发送；发送前首次客户消息到达则原子作废欢迎语，只处理新消息。
13. 普通回复、无原生 ID 的已提交末尾媒体和 `friend_welcome` 空基线都必须使用生产 `build_send_context_guard()` 构造布局无效反例；`ok=false`、快照缺失、数量/序列/摘要任一矛盾都必须结算为 `C2_PRE_SEND_LAYOUT_INVALID`，并断言只读一帧、零 Handoff、零入库、零媒体动作、零 claim-send 和零发送。

#### 6.0.4.4.1 AI 回复成功后的 `recent_ai_sent` 监控读取

`recent_ai_sent` 是 `sent_ack` 已被服务端确认后的后续监控读取，用于发现
客户或销售在 AI 回复之后的新消息。它不是发送前 `pre_send_refresh`，也不是
物理发送回执确认；三个阶段禁止共用一个“复读失败”分支。

本节恢复逻辑只在 `read_reason=recent_ai_sent`，且服务端能提供非空
`reply_action_id + sent_at + reply_text_hash` 时生效。缺少任一字段时不得生成
AI 边界 `recoverable_hold`，也不得把普通 `ai_active/friend_activation_reading`
会话改成 `waiting_user_reply`；应回到该读取原因原有的身份恢复或硬门禁流程。

服务端确认 `sent_ack` 后，Conversation 的唯一业务状态是
`waiting_user_reply`。`ai_active` 只表示 AI 能力可用，不是“AI 已回复后的等待状态”，
不得在发送成功、监控无变化或身份恢复期间把 Conversation 投影为 `ai_active`。

该流程必须使用以下边界：

```text
ai_reply_boundary = 服务端已确认 sent_ack 对应的 reply_action_id
                    + sent_at
                    + reply_text_hash
                    + 已持久化 worker_stable_id（建立成功时）
```

Worker 每次读取时必须先合并服务端 `identity_checkpoint`、本地已确认 AI 回执和
`possible_ai_send / ai_unreconciled` 事实，再判断画面候选是位于边界之前、等于边界，
还是可能位于边界之后。禁止先把全屏身份异常上报为 handoff，再尝试区分新旧。

唯一状态表：

| 观察结果 | 证据条件 | 服务端处理 | 会话状态 |
|---|---|---|---|
| 无新消息 | 没有候选位于 `ai_reply_boundary` 之后 | 结算 `no_change`，更新读取冷却；不创建 batch、Brain 或 handoff | 保持 `waiting_user_reply` |
| AI 本次回复未建立稳定气泡身份 | 候选与 `reply_action_id + reply_text_hash + sent_at` 对应，且没有更新的客户候选 | 保留/merge `ai_unreconciled`，记 warning；不把它当销售人工回复，不创建身份 handoff | 保持 `waiting_user_reply` |
| 历史消息身份不清 | 异常范围可证明不晚于 `ai_reply_boundary` | 记 `historical_warning`，不创建 `recoverable_hold/HandoffEvent` | 保持 `waiting_user_reply` |
| 可能存在新消息，但角色或顺序无法确认 | 候选可能位于边界之后，或缺少顺序证据而无法排除 | 首次只创建 `recoverable_hold`且次数记 0；先纯数据合并，仍不清时最多两次不同 `read_run_id` 的被动稳定重读 | 保持 `waiting_user_reply`，禁止立即投影 `waiting_sales_reply` |
| 恢复耗尽后仍无法排除新客户消息 | 纯数据合并后的两次不同读取轮次被动稳定重读均已失败，或 hold 已超过 120 秒，异常仍切入最新待回复尾部 | 幂等创建一个身份 `HandoffEvent`，通知销售一次 | 进入 `waiting_sales_reply` |
| 确认新客户消息 | 边界之后的 `customer` 事实身份、顺序和内容完整 | 正常入库，创建/合并新 batch 并进入 Brain | 按本轮 Brain/Guard 结果转移 |
| 确认销售人工回复 | 边界之后的 `self` 事实不匹配任何 AI `reply_action/sent_ack` | 按销售已接管处理，不创建新 AI 回复 | 进入 `sales_replied_waiting_user` |

硬约束：

- `MESSAGE_IDENTITY_UNCONFIRMED`、`MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS` 和
  `C2_MESSAGE_HISTORY_GAP` 不得被后端放入“非临时门禁即直接 handoff”的默认分支。
- 上述门禁必须携带 `gate_scope + min/max_screen_order + boundary_relation`。
  `boundary_relation` 只允许 `before_or_equal / after / unknown`；缺少时按 `unknown`
  进入恢复，不得立即 handoff。
- 同一读取包含多个身份/历史门禁时必须整体判定，不得只取列表第一项：任一门禁为
  `after` 则整体按 `after`；否则任一门禁为 `unknown` 则整体按 `unknown`；只有全部
  为 `before_or_equal` 才能按纯历史告警结束。不能用前面的历史告警掩盖后面的最新尾部异常。
- `message_event_ids=[]` 且没有边界之后客户候选时，禁止仅因画面中某个旧槽位不清
  创建 handoff。
- `recent_ai_sent` 只是读取优先级来源，不是操作阶段，不得重复发送、
  重复好友激活或改写已确认 `sent_ack`。
- 若用户在该读取中点击“暂停接单”，按 3.1.1.1 让当前 `inflight_flow_id`
  收口；`pause_requested` 不得改写任何门禁的 `boundary_relation`。

`recoverable_hold` 不得复用 `HandoffEvent`、Conversation.status 或前端 Task。
它固定保存在 `WechatSessionBinding.recovery_hold` 单一 JSON 字段，结构为：

```json
{
  "status": "active|resolved|escalated",
  "gate_key": "sha256",
  "reason_code": "MESSAGE_IDENTITY_UNCONFIRMED",
  "gate_scope": "reply_suffix",
  "boundary_relation": "after|unknown",
  "min_screen_order": 0,
  "max_screen_order": 0,
  "originating_read_run_id": "read_run_id",
  "first_seen_at": "ISO-8601 UTC",
  "last_seen_at": "ISO-8601 UTC",
  "recovery_attempt_count": 1,
  "last_recovery_kind": "checkpoint_merge|stable_reread"
}
```

同一 `gate_key` 的重复 ingest 只更新 `last_seen_at`，不增加恢复次数。
`recovery_attempt_count` 只在纯数据 `checkpoint_merge` 或实际 `stable_reread`
完成且仍失败时递增；最多为 2。值为 2 后，`after` 必须 handoff；`unknown` 若在
稳定权威重读后仍无法排除影响最新待回复尾部，也必须 handoff。只有能证明全部异常
均为 `before_or_equal` 才可结束为历史告警，禁止永久停留在 active hold。
“120 秒”是正常在线处理的最长目标时间，不是绕过第二次恢复的
定时转人工条件；Worker 暂停、离线或无授权时不得仅因超时升级。
权威干净读取证明门禁消失时将同一 `gate_key` 标记 `resolved`；升级成功时
标记 `escalated` 并在 HandoffEvent 证据中引用该 `gate_key`。

`recent_ai_sent` 状态链必须通过以下定向验收：

1. `sent_ack=confirmed + messages=[] + identity_errors=[]`：结算 `no_change`，保持
   `waiting_user_reply`，零 HandoffEvent、零 Brain。
2. AI 已回复，下轮未能为该 AI 气泡建立稳定身份，且无边界之后候选：
   保留 `ai_unreconciled`，保持 `waiting_user_reply`，零 HandoffEvent。
3. 边界之前的历史文字/媒体身份不清：只记 `historical_warning`，不得创建 hold 或 handoff。
4. 边界之后存在新候选，但角色或顺序不清：第一次只创建
   `recoverable_hold`，HandoffEvent 为 0；纯数据合并和两次不同 `read_run_id` 的被动稳定重读均失败，或 hold 超过 120 秒后，才允许一个幂等 handoff。
5. 读到边界之后新 `customer` 文字/语音/图片：正常入库并创建新 batch，
   不得被旧 AI 气泡身份告警阻断。
6. 读到边界之后新 `self` 消息，且不匹配任何 AI 回执：进入
   `sales_replied_waiting_user`，不得误合并为 AI 回复。
7. 构造 `flow_gate_errors=[MESSAGE_IDENTITY_UNCONFIRMED] + message_event_ids=[]`：
   后端不得直接调用 `create_deterministic_handoff_for_ingest`。
8. 在第 1—4 项每个阶段点击暂停：只改变 Worker 接单状态，不改变
   `boundary_relation`、conversation.status 或 HandoffEvent 数量。
9. 非 `recent_ai_sent` 或缺少有效 AI 边界的身份异常：不得进入本节 hold，
   不得伪造 `waiting_user_reply`。
10. 同一请求同时包含历史 `before_or_equal` 和最新 `after/unknown` 门禁：必须按
    `after/unknown` 恢复，不能被第一条历史错误覆盖。
11. 两次真实恢复后仍为 `unknown` 且无法排除最新尾部：必须只创建一个幂等 handoff，
    不得永久保持 active hold。

本节与 3.1.1.1 引入新的机器状态和门禁字段，属于下一个灰度候选
`0.9.8`。已冻结的 `0.9.5` 不得覆盖发包。实施时 PRD、本文、全流程图、
版本记录、`contract_revision`、生成 Schema、Worker、后端和安装包必须一次性统一为
`0.9.8`；不得为 `0.9.5` 增加兼容分支或双字段。

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

语音身份模型固定为“一条物理语音、一个动作对象、一个最终业务身份、多个局部识别别名”。
下列对象不得合并成一个字段，也不得互相代用：

| 对象 | 创建时机 | 作用域 | 是否可生成 source key |
|---|---|---|---|
| `canonical_voice_action_id` | 当前帧候选归并后，Worker 恰好选中一条即将执行的新语音时 | 仅当前一次 Sidecar 动作事务；动作后立即终止 | 否 |
| `anchor_aliases[]` | OmniAuto 观察/点击时 | 仅用于同帧归并、当次点击和当次前后帧跟踪 | 否 |
| `reserved_worker_stable_id` | Worker 确认为本轮新语音、紧邻首次点击前原子预留 | 持久化在 ActionJournal/预留表，永不复用，未提交前不进入正式 catalog | 否 |
| `worker_stable_id` | 本次动作结果完成身份结算后，由预留序号单向提交；或从历史 checkpoint 唯一恢复 | 跨轮业务身份 | 是 |

`identity_phase` 只允许 `historical_restored / sequence_reserved / business_committed /
identity_quarantined`。`sequence_reserved` 不是已结算历史消息，不得用它命中 Ledger、不得过滤最终画面观察。
`reserved_worker_stable_id` 一旦分配永不回收给其他消息；证据不足时进入
`identity_quarantined`，而不是重新利用该序号。

语音动作必须采用唯一的两阶段握手，不能由 Worker 和 OmniAuto 各自再选一次目标：

1. `prepare_voice_action` 阶段只允许截图/OCR，不允许右键、点击或改变微信画面。OmniAuto
   在新鲜的 `pre_frame_id` 中归并物理气泡并恰好选中一条可操作语音，返回
   `selected_pre_observation_id + selected_action_token + selected_target_fingerprint`。
   `selected_action_token` 必须绑定当前会话指纹、正式短码、`pre_frame_id`、选中观察 ID、
   角色和目标结构摘要；它是短期单次令牌，不是跨轮消息身份。
2. Worker 只校验 prepare 响应的合同与后端授权，不读取标题、坐标、时长或 aliases 重新选目标。
   Worker 为该计划创建 `canonical_voice_action_id`、原子预留 `reserved_worker_stable_id`，并将
   完整 prepare 响应与 `pre_action_identity_sequence` 一起写入 ActionJournal。
3. `execute_voice_action` 请求必须原样携带 action ID、预留号、`pre_frame_id`、
   `selected_pre_observation_id` 和 `selected_action_token`。OmniAuto 在任何点击前只确认令牌
   尚未使用、会话/短码未变、当前唯一局部目标仍满足本次 `frame_action_binding`；这只是本帧
   操作票复核，不得输出跨轮“同一条业务消息”结论，不得要求目标已具有长期身份，也不得重新
   选择“当前最下面一条”或把 Worker action ID 盖到另一个现场候选上。
4. 令牌过期、目标消失、会话变化或重验无法唯一时，必须零点击返回
   `action_phase=cancelled_before_trigger + transcript_binding_status=not_attempted`。该 action/预留号
   形成持久化取消终态且预留号作废不复用；Worker 从最新帧重新 prepare，不得把它伪装成客户
   语音失败或永久 handoff。

同一发送方、物理范围高度重叠或 alias 图连通、且时长/父子关系不冲突的观察只能由 OmniAuto
在 prepare 阶段合并为一个 frame-local 候选。如果一个 alias 同时指向多个物理气泡、一个气泡
同时落入多个候选组，或任一时长/角色/父子证据冲突，相关项全部零点击，禁止“取第一个匹配项”或
“取坐标最近项”。ActionJournal 必须以
`canonical_voice_action_id + reserved_worker_stable_id + selected_action_token` 作唯一项键；零项或多项均为合同错误。
一次动作结束后，动作前帧中其他尚未执行的 frame-local 候选全部作废，不得带着旧 action ID/aliases 继续点击；
Worker 必须从动作后最新帧重新归并、恢复历史、选择下一条，并生成新 action ID。

微信点击“语音转文字”后画面自动滚动是正常且必须支持的主路径，不得把“点击后坐标不变”作为
正确性前提。OmniAuto 必须在同一 execute 调用内保留真实 capture 产生的 before/mid/after 帧，
每次只允许对一个已 prepare 的 `selected_action_token` 执行一次物理点击，并返回唯一的
`transcript_binding_status`。不得用 action ID、截图路径摘要或业务序列摘要伪造 capture frame ID。

`transcript_binding_status=confirmed` 必须同时满足：

1. before/after 两帧均确认同一 private 会话和同一正式短码；
2. 本次 Sidecar 调用只有一个实际点击对象，返回的 `canonical_voice_action_id` 与请求完全一致；
3. 点击前菜单证据确认操作对象是语音，`action_phase` 已按 ActionJournal 单调前进；
4. 后帧只有一个同角色转写候选能与被点击对象对齐；对齐只允许三种方法：
   微信/原生稳定源 ID 精确相等、`continuous_target_tracking` 或 `neighbor_scroll_alignment`；
5. `continuous_target_tracking` 必须在 before/post 之间有至少一个中间帧，并返回完整
   `tracking_edges[]`。每一条边必须明确包含 `from_frame_id/from_observation_id ->
   to_frame_id/to_observation_id`、相同角色/消息类型、位移与结构连续证据及
   `edge_candidate_count=1`；后一条边的起点必须严格等于前一条边的终点，首边必须从
   `selected_pre_observation_id` 出发，末边必须到达最终绑定语音观察。同一帧“总共有一个候选”
   或 `[1,1,1]` 计数不能证明三个候选是同一物理对象，禁止作为 confirmed 证据；
   `neighbor_scroll_alignment` 必须用至少两个未变、角色可信的独立观察估计全局滚动，并证明转写候选处于原动作对象投影后的唯一槽位；
6. `binding_candidate_count=1`，不存在第二个满足条件的正文。

任一条不满足时，`transcript_binding_status` 必须为 `ambiguous` 或 `failed`，不得返回正文绑定。
时长相同、anchor 相同、坐标接近、只有一条相同文字，以及“从底部第几条”相同，都不是可独立或组合放宽上述条件的证据。

动作后出现的任何 `voice_state=untranscribed` 观察和任何新文字，一律视为当前帧待重新仲裁的观察；
禁止通过旧 `anchor_aliases`、内容签名、时长或旧坐标继承任何已预留/已提交的 Worker 序号。

动作结果的唯一身份转移为：

```text
historical checkpoint 唯一匹配
-> identity_phase=historical_restored
-> 查 Ledger/Outbox；已结算则零点击

当前帧候选唯一归并
-> OmniAuto prepare零操作选定一条物理语音并返回token
-> canonical_voice_action_id
-> 原子预留 reserved_worker_stable_id
-> identity_phase=sequence_reserved
-> ActionJournal not_attempted
-> execute点击前按token重验同一目标
-> 重验失败且未点击：cancelled_before_trigger，烧毁预留号并从最新帧重新prepare
-> 点击前 trigger_attempted
-> 微信自动滚动 + before/after 对齐
-> binding confirmed：预留序号单向提交为 worker_stable_id
-> binding failed且目标身份已由prepare/trigger证明确立：以预留序号结算无正文failed语音事实，不自动再点击
-> binding ambiguous或无法证明实际操作对象：identity_unresolved，ActionJournal进入终态，不绑定正文、不重点击，立即按客户端技术故障结算
-> 废弃动作前帧的其他未执行候选，从最新帧重新仲裁下一条
```

动作终态固定为：同一 action ID 只允许一次物理动作；只有对象和回执唯一、但微信/Provider 明确内容处理失败时，`failed` 才形成媒体失败事实。
`identity_unresolved/quarantined`是已触发动作后的客户端不变量失败：只允许用已持久化的唯一确定结果做无 UI 结算；结果仍缺失、多解或矛盾时必须立即写入终态并结算 `technical_failed`，不得被动重读、等待 120 秒或创建 handoff。不得永远保持 `not_attempted/trigger_attempted`，也不得阻塞其他短码。所有返回路径都必须满足“已创建 action 的 ActionJournal 终态数量 = 已创建 action 数量”。

Worker C2 读取某个会话时，执行顺序固定为：

```text
获取本地微信 UI 锁
-> 定位目标会话：第一屏 visible 或 remark_code 搜索
-> 调用 OmniAuto messages 做首次读取/消息类型探测
-> 检查initial_read权威观察：没有当前可执行且未结算的未转写语音时跳过整个语音编排器，原样保留initial_read payload
-> 只有存在上述语音时才调用唯一生产语音编排器；OmniAuto prepare 在新鲜帧中合并 frame-local aliases、零操作提出一个唯一物理候选并返回 action token和原始观察
-> 只用本地已提交身份 + 后端 checkpoint 恢复已知历史语音；已结算历史语音零点击
-> Worker 不重新解释 OCR 几何，但必须用完整序列、历史checkpoint、授权和本地事务状态决定是否接受prepare候选；Sidecar不能代替Worker作消息身份结论
-> Worker按最新完整画面固定选择screen_order最大的一条未处理语音后，才创建canonical_voice_action_id、预留reserved_worker_stable_id和ActionJournal；此时仍无正式身份
-> execute请求携带action ID、预留号和动作类型；OmniAuto重新取得最新帧，只在该帧内完成同帧语音归并、选择当前最下方一条未转写语音并用当前bounds点击；不得用旧observation ID、64分桶、旧坐标、邻居或指纹证明跨帧还是原对象
-> 点击后微信自动滚动属于正常路径；OmniAuto只返回本次动作前后画面、唯一新增转写结果及动作回执，不返回same_business_message
-> Worker验证action/reserved、唯一新增转写正文和回执后，才把实际结果绑定到预留ID并提交worker_stable_id；对象唯一且微信明确转写失败才形成committed_failed
-> 已触发但无结果、多结果、错对象或回执矛盾固定形成identity_unresolved并立即technical_failed：零正式消息、零Brain、零Handoff、释放UI锁、Worker=faulted，禁止重复点击
-> 动作后立即作废旧帧其他未执行候选并完整复读；对最新帧的新文字、剩余语音和图片重新按固定顺序编排，禁止继承旧action/anchor/坐标/内容对应的Worker ID
-> 重复“最新帧只选一条”，直到最新帧无可执行语音；随后处理图片；每个已创建action均进入cancelled_before_trigger/committed_completed/committed_failed/identity_unresolved之一
-> 本轮initial_read未被媒体UI动作或并发页面变化失效时，以未被改写的原payload建立统一slots和screen_order，authoritative_frame_source=initial_read
-> 本轮发生任何语音/图片UI动作或并发页面变化使initial_read失效时，以最新稳定完整画面建立统一slots和screen_order，authoritative_frame_source=final_read
-> 将最终画面对象分为frame_observation / pending_media_action / committed_message / quarantine_record；只有committed_message进入正式消费者
-> 历史committed_message复用原ID；new_suffix新文字/系统消息经唯一提交门直接提交；此后才生成source_message_key并查询Ledger/Outbox
-> 初次图片同行头像角色不可信时形成帧级身份门禁，不建立图片动作或消息、不写ignored Ledger
-> new_suffix新图片只建立pending_media_action、预留号和ActionJournal，不生成source key、不查询Ledger/Outbox；历史正式图片和既有Outbox不重复复制或计费
-> 图片动作完成后以实际复制的图片字节SHA、菜单/点击/剪贴板回执验证本次结果；完整时经唯一提交门形成committed_completed/failed，缺失、多结果、错对象或矛盾时形成identity_unresolved并按technical_failed收口，不伪造图片消息、不转人工
-> 图片成功回填customer_image_understanding/visual_bridge_input；操作对象已唯一但复制/Vision失败才形成committed_failed；动作前零触发且对象消失/变化为cancelled_before_trigger；被顶出最终当前屏后从最新画面重新仲裁
-> 在任何身份仲裁、合同失败、授权变化或提前返回前，让每个已创建media action进入cancelled_before_trigger/committed_completed/committed_failed/identity_unresolved之一；未选中的frame observation没有ActionJournal，不伪造终态
-> 最后只按final screen_order收集committed_message并转换为message_event；待处理动作和隔离记录禁止进入ingest/Brain
-> 上报 /api/workers/{worker_id}/wechat/messages/ingest
-> 无需等待batch或batch已终态：释放本地微信 UI 锁
-> 需要等待当前batch：保持原会话和UI锁，继续Brain、pre_send_refresh和发送收口
```

上述顺序是唯一合法流程。禁止在右键前提交正式 Worker 身份，禁止用 voice anchor 直接生成
source key，禁止用旧 observation、64 分桶、邻居、气泡截图或跨帧坐标找回编号；正式身份只能绑定
Worker 已验证的实际动作结果。

目标机器合同 revision 为 `0.9.45`。代码、ActionJournal、Worker 本地临时图片动作槽位、跨帧连续性比较器、后端拒绝技术故障 Handoff 及生成 Schema 已按本文统一实现；规范化 SHA 已真实计算为 `8813425572dad678b86354856dad798c43a9c47192d17319dfb8e84c8877e99e`。旧 SHA `edc5066fac32a371634f8a220710b71b3e3bf4c709561dc8350444a7ed992c27` 仍作废。来源提交已固定且架构复审通过，但车金提交、打包、部署和 Windows UAT 尚未完成，不得把当前工作树写成已发布或已通过 Windows UAT：

| 字段 | 所有者 | 必填规则 |
|---|---|---|
| `voice_action_stage` | Worker 请求，OmniAuto 校验 | 只允许 `prepare / execute`；prepare 零 UI 动作，execute 只能消费 prepare 返回的一次性 token |
| `canonical_voice_action_id` | Worker 生成，OmniAuto 原样回显 | 只为当前帧已选中且即将执行/结算的一条语音生成；同一 `read_run_id` 内永不复用 |
| `reserved_worker_stable_id` | Worker | 新语音第一次物理点击前必填；历史已确认语音不重新预留 |
| `selected_action_token` | OmniAuto prepare 生成，Worker 原样保存/回传 | 必须绑定会话、正式短码、媒体类型和固定选择规则；单次使用；不得把旧 observation 或坐标固化为 execute 身份条件 |
| `selected_pre_observation_id` | OmniAuto | 仅保存 prepare 时看到的审计引用；execute 不要求最新帧存在同 ID，也不得据此跨帧找对象 |
| `selected_target_fingerprint` | OmniAuto | 仅为 prepare 帧排障证据；不得参与 execute 准入、跨帧业务身份、正式回执或 source key |
| `identity_phase` | Worker | 只允许 `historical_restored / sequence_reserved / business_committed / identity_quarantined` |
| `transcript_binding_status` | OmniAuto 生产动作结果证据，Worker 校验并决定是否提交身份 | 只允许 `not_attempted / confirmed / failed / ambiguous`；不是跨轮业务身份结论 |
| `transcript_binding_method` | OmniAuto | `confirmed/failed` 只允许 `native_source_id / unique_action_result_delta`；前者使用真实微信原生 ID，后者证明本次唯一点击后只出现一个新增转写结果。禁止 `continuous_target_tracking / neighbor_scroll_alignment` 作为跨帧身份方法；`not_attempted/ambiguous` 必须为 `none` |
| `binding_candidate_count` | OmniAuto | 非负整数；`confirmed/failed` 且形成正式成功/失败事实时必须为 `1` |
| `pre_frame_id / post_frame_id` | OmniAuto | 发生点击后必须同时存在且不得相等 |
| `native_source_message_id` | OmniAuto | 当且仅当 method=`native_source_id` 必填；before/post 取值必须完全一致 |
| `action_result_evidence` | OmniAuto 产生，Worker 校验 | 语音必须包含唯一新增转写结果、触发前后帧、点击次数和结果候选数；图片必须包含实际剪贴板图片字节 SHA、菜单/点击/剪贴板代次与结果候选数。不得用坐标、邻居或气泡截图替代实际结果 |
| `worker_stable_id` | Worker | 只在 `historical_restored/business_committed` 存在；必须等于已恢复或已提交序号 |

语音动作的临时凭证不得进入正式 `source_message`。Sidecar 在 execute 内部只使用
`frame_action_binding` 承载 action/reserved/token、最新执行帧、本次点击点与实际动作结果；
它不得承载“pre observation 与 post observation 是同一业务消息”的结论。正式返回 Worker 前必须从
observations 中删除该内部对象，只返回类型化 `action_result_evidence`。Worker 验证本次唯一新增转写结果后
才生成 `confirmed_action_mapping` 并进入 ActionJournal 结算和正式身份提交门；临时字段不得进入长期
`source_message`、Ledger、Outbox 或后端消息身份。

两阶段请求/响应唯一结构如下，字段不得改名或拆出兼容版本：

```json
{
  "action": "voice-transcribe",
  "voice_action_stage": "prepare",
  "read_run_id": "read-...",
  "remark_code": "CJ123456",
  "rpa_session_key": "...",
  "conversation_type": "private"
}
```

prepare 成功只返回观察计划，必须满足 `ui_action_performed=false`：

```json
{
  "pre_frame_id": "capture-frame-before-...",
  "selected_pre_observation_id": "voice-observation-...",
  "selected_action_token": "opaque-single-use-token-...",
  "selected_target_fingerprint": "action-local-target-fingerprint-...",
  "candidate_group_count": 1,
  "ui_action_performed": false
}
```

Worker 持久化 prepare 结果后，execute 必须原样回传：

```json
{
  "action": "voice-transcribe",
  "voice_action_stage": "execute",
  "canonical_voice_action_id": "va-...",
  "reserved_worker_stable_id": "worker-message-11",
  "identity_phase": "sequence_reserved",
  "pre_frame_id": "capture-frame-before-...",
  "selected_pre_observation_id": "voice-observation-...",
  "selected_action_token": "opaque-single-use-token-...",
  "selected_target_fingerprint": "action-local-target-fingerprint-...",
  "remark_code": "CJ123456",
  "rpa_session_key": "...",
  "conversation_type": "private"
}
```

execute 必须回显 action ID、预留号和 token；pre frame/selected observation 仅作审计，不要求与最新执行帧
的 observation 相同。Sidecar 必须在 execute 的最新帧中重新应用唯一固定选择规则，并记录点击恰好一次。
`unique_action_result_delta` 只有在动作后出现唯一新增转写结果时成立；零个或多个结果都必须为
`ambiguous + method=none` 并进入技术故障。`failed` 只有在本次菜单、触发和结果回执已唯一证明，且微信
明确返回内容处理失败时，才允许以预留号形成无正文失败事实。

Worker 必须对上述逻辑矛盾失败关闭。例如 `identity_phase=sequence_reserved` 同时存在
`worker_stable_id`、`transcript_binding_status=confirmed` 但候选数不是 1、或 Sidecar 改写 action ID，都必须在任何新微信动作和入库前返回 `C2_VOICE_IDENTITY_CONTRACT_INVALID`。

##### 6.0.4.6.1 灰度 `0.9.5` 实施边界

本表是实施边界，不是可选重构建议。函数后续如改名，仍必须通过 AST/行为门禁证明不存在等价旧逻辑。

| 组件/函数边界 | 必须实现 | 明确禁止 |
|---|---|---|
| `task_runner._prepare_voice_action_frame` | 只归并当前帧 aliases 和恢复 checkpoint 已确认历史身份；恰好选中一条即将执行的新语音后，再创建 action ID 并调 storage 原子预留序号 | 在点击前调 `_reconcile_message_identities` 给全画面新观察写正式 `_worker_stable_id`；为同帧所有候选预先创建 action/Journal；在预留阶段查 Ledger/source key |
| `voice_worker_ids_by_anchor` / `attach_inflight_worker_ids` | 从正式 C2 语音身份链路删除；最终帧只接受本次 binding confirmed 后的显式 action ID -> committed ID 映射 | 用 anchor、`observation_identity_signature`、内容、时长、坐标或单一出现次数将旧 ID 挂到动作后观察 |
| `_reconcile_message_identities` / 文字、图片签名回挂路径 | 收口为唯一 `align_committed_message_sequence`（函数名可调整）；输入 checkpoint/动作前全业务序列、动作后最新序列和显式 action 映射；输出唯一对齐证据与连续 `new_suffix` | 用角色+正文+同类序号、角色+邻近锚点、坐标、“最近一条”或单条内容相等产生/继承正式 ID |
| Worker 所有语音入口 | 首先检查当前权威观察中是否存在可执行未转写语音；只有存在时才调用一个共用生产编排器。初读、图片后续读、continuation 和崩溃恢复只传不同上下文，prepare/execute、状态转移和合同验证完全相同 | 对无未转写语音的纯文字/图片流程调用 prepare；inline 首条语音流程、`_finish_new_visible_voices_in_current_chat` 或恢复路径各保留一份身份判定/点击逻辑 |
| `wechat_win32_ocr_sidecar.voice_transcribe_payload` | 保留 prepare/execute 两阶段；prepare 返回绑定会话、媒体类型和固定选择规则的单次 token 且零操作；execute 消费 token 后重新取得最新帧，同帧归并并选择最下方一条未转写语音，只点击一次，返回唯一实际转写结果证据 | 用 prepare 的旧 observation、坐标、64 分桶、邻居或指纹跨帧找对象；让 Sidecar 决定业务消息身份；零/多结果时伪造成功或转人工 |
| `sidecar_new_message_occurrences` 及内容 multiset 比较 | 只可用于发现“画面可能新增了什么”，结果必须再进入新观察仲裁 | 用来证明正文属于被点击语音，或认定相同内容是旧消息 |
| `storage.py` 消息序号状态 | 原子落盘 action ID、reserved ID、identity phase、trigger phase 和 terminal；预留号单调且永不复用 | 崩溃后回收预留号；新动作重用旧 action ID；`trigger_attempted` 后再点击 |
| `storage.py` 动作前画面状态 | 与 ActionJournal 原子保存 `pre_action_identity_sequence`，覆盖 `committed/selected_action/frame_local_unselected`；动作终态后补齐 `sequence_alignment_evidence` | 只保存已编号项；崩溃后用新截图或相同内容伪造动作前序列 |
| `contracts/c2_contract_v3.json` 及生成 schema | `0.9.45` 实现候选必须将 `contract_revision`、客户端、后端、Sidecar、生成 Schema、样例和 manifest 一次性统一；合同必须表达“五字段业务投影与当前帧几何分离”“当前帧固定选择”“实际动作结果证据”“Worker 结果后绑定正式身份”和“identity_unresolved 技术失败禁止 Handoff”，并继续保留 batch/reply 响应中的只读 `pre_send_fact_checkpoint`、三种 MECE 比较结果、对象分类、四种媒体终态、消费者白名单及 `authoritative_frame_source` | 使用独立合同版本号；在旧 revision 下静默改语义；把几何字段从原始观察删除；用五字段投影或跨帧像素映射媒体身份/动作对象；改写 Brain、语音优先/图片随后顺序、UI 锁、customer 内容处理失败 Handoff 或 C0/C1/C4 状态机；新增同义字段、双字段兼容、HTTP 请求侧 checkpoint 或 Worker/后端兜底重判 |

新流程的唯一落库时点为：预留表/ActionJournal 在点击前落盘；正式 identity catalog、
Ledger、Outbox 和 `source_message_key` 只在 `historical_restored` 或 `business_committed` 后落盘。
这两类落盘不允许在一个无状态区分的 helper 中一次完成。

语音转写不单独创建任务，不进入任务中心。它是 `message_ingest` 前的条件性本地预处理步骤，只有首次 `messages` 读取/探测发现未转写语音时才执行，和 `messages` 读取共享同一把 `Local WeChat UI Lock`。不得出现一边执行 `add_friend/chat_reply/send`，另一边点击语音转文字的并行操作。

目标首次通过 private + 短码确认后，定位结果在本 `read_run_id` 内冻结：后续 `messages`、语音和图片动作都必须在同一个已确认会话内执行，不得再次搜索客户。唯一例外是目标尚未通过首次确认、visible 点击后标题明确不含目标短码且未读取消息区或触发任何会话内动作时，可按 `C2_VISIBLE_TARGET_STALE_AFTER_CLICK` 在同一授权、UI 锁和 `read_run_id` 内丢弃旧坐标并完整重新定位一次。目标一旦确认，或已经读取消息区/触发媒体动作，中途再发现标题、短码或会话指纹不匹配时必须失败退出，不能重新搜索。

Worker 上报语音消息时：

| 字段 | 规则 |
|---|---|
| `message_type` | 使用 `voice`。如果 OmniAuto 只能输出转写后的文本，Worker 仍应根据 `voice_transcription.transcribed_messages` 或 `quality_flags=voice_duration_prefix_removed` 标记为 `voice`。 |
| `content` | 保存微信转写后的文本。 |
| `sender_role_hint` | V3 合同只使用 `customer / self / system / unknown`；销售本人、历史 `sales / sales_candidate` 在合同边界统一归一为 `self`。语音角色继承原语音气泡/父语音证据。 |
| `raw_payload` | 必须保存 OmniAuto 原始消息、当次动作 aliases、前后/跟踪帧、binding 证据、`voice_transcription_meta`、截图引用和质量标记；不得把 anchor 标记为跨轮稳定身份。 |
| `ocr_confidence` | 使用转写文本 OCR 置信度或消息解析置信度；没有则为空。 |

语音转写状态处理：

| OmniAuto 状态 | Worker处理 | 服务端处理 |
|---|---|---|
| `voice_transcribe_completed` | 继续调用 `messages`，把转写文本入库。 | 按正常 `message_event` 处理；客户语音可触发 C3。 |
| `voice_transcribe_partial` | 同一 flow 内已创建的多个 action 中存在成功和失败；每个已创建 `canonical_voice_action_id` 必须逐项输出 completed/failed/quarantined，第一条失败不得阻止从最新帧重新选择后续语音。 | 成功和失败事实均先逐条入库；存在 customer failed 项时，已创建 action 全部结算后按 L1 转人工；self failed 项只告警。 |
| `voice_transcribe_no_new_text` | 已尝试但没有确认出对应文字，将该 `canonical_voice_action_id` 写为 failed；恢复稳定画面后继续处理其他语音。 | 不把时长或疑似文本当正文；customer failed 项完成结算后按 L1 转人工，不生成自动澄清回复。 |
| `voice_transcribe_no_visible_voice` | 继续调用 `messages`；说明当前可见区没有待转写语音。 | 不作为错误。 |
| `voice_transcribe_target_not_found` | 目标会话或目标语音未能确认，零点击并结束该项；不能与 `no_visible_voice` 混为一谈。 | 作为 L2 可恢复暂停；不直接创建长期 handoff，不影响其他短码。 |
| `target_not_confirmed_for_voice_transcribe` | 停止读取该目标，不调用后续微信动作。 | 记录读取失败并等待准入证据改善；不创建客户 handoff。 |
| `voice_transcribe_click_failed` | 将当前项写为 failed，恢复稳定画面后继续下一条；不得退出并遗留其他 `not_attempted`。 | 记录 `VOICE_TRANSCRIBE_CLICK_FAILED`；客户消息完成结算后按 L1 转人工，销售消息只记 warning。 |
| `voice_transcribe_lock_timeout` | 本轮跳过，等待下轮。 | 记录 `VOICE_TRANSCRIBE_LOCK_TIMEOUT`。 |
| `voice_transcription_exception` | 当前项写为 failed；`finally` 结算全部已冻结项。 | 记录 `VOICE_TRANSCRIBE_FAILED`；客户消息完成结算后按 L1 转人工；仅身份或暂态技术异常进入 L2。 |

语音转写失败时，不能把“语音时长 5 秒”当成客户内容，也不能因为没有文本就认为客户沉默。`item_state/flow_state` 是单次 flow 的结果合同，不是新增数据库状态机，也不要求拆出新的语音任务。规则如下：

```text
目标客户已确认，且客户语音可见但转写失败
-> failed事实入库并记录error_code
-> 不调用Brain回答同批文字
-> 不发送“没听清/请改发文字/请重发”等自动澄清回复
-> 创建handoff、通知销售并进入waiting_sales_reply
```

如果目标客户未确认，例如 `target_not_confirmed_for_voice_transcribe`，不得改变会话主状态，只记录读取失败证据，避免把别人的会话误判为当前客户。

服务端判断规则：

- `sender_role=customer` 且 `message_type=voice` 且 `content` 非空：等同客户新消息，可触发 C3。
- `sender_role=self` 且 `message_type=voice`：等同销售侧人工回复，不触发 AI，并按销售接管/AI 暂停处理。
- `sender_role=unknown`：不猜角色，进入 L2 可恢复暂停；只在影响最新待回复尾部且恢复失败后 handoff。
- `sender_role=customer` 且失败事实 `content` 为空：禁止把空内容当正常问题作答；完成事实结算后直接转人工，不生成自动澄清回复。

实机验证要求：

```text
1. 客户发一条语音，Worker 能点击微信语音转文字并入库为 message_type=voice。
2. 客户连续发文字+语音，服务端能按同一 conversation_id 合并上下文。
3. 销售手机端发语音，桌面同步后不得触发 AI 自动回复。
4. 客户语音转写失败时不重复点击；失败项终态入库后直接转人工，不生成请客户改发文字或重发的自动回复。
5. Worker 重启后同一条语音转写文本不重复入库、不重复触发 C3。
6. add_friend / chat_reply / pre_send_refresh 与 voice-transcribe 共享本地 UI 锁，不允许并行操作微信。
7. 同屏两个真实语音、同一气泡同时出现 structural/stable alias、第一条失败时，待处理对象必须恰好为两个，第二条继续执行，最终两项均为 completed/failed 且零 `not_attempted` 残留。
8. 跨轮身份异常发生在语音动作之后时，必须先完成所有语音 ActionJournal/Ledger 终态结算，再进入身份 hold；不得提前 return。
```

语音 flow 的时间保护采用两层安全机制：

| 机制 | 当前口径 | 目的 |
|---|---|---|
| 无进展 watchdog | 240 秒无任何新截图、成功转写或处理进展才停止 | 识别 OCR/微信 UI/sidecar 真正卡死，不打断正常慢流程。 |
| 硬安全上限 | 900 秒 | 仅防止进程永久占锁，是最终保险丝，不是正常业务时限或性能承诺。 |

只要持续取得进展，语音 flow 应继续处理当前屏全部可处理语音；不能套用普通首屏扫描的 10-20 秒建议。触发安全保护时必须显式返回已完成项、失败项、停止原因和证据，不得把已成功语音降级成普通 `text`。

语音处理期间若最终画面出现新的可见未转写语音，不释放当前会话和 UI 锁，也不机械留到下一轮。Worker 应按新的稳定语音身份继续调用 `voice-transcribe`；前一条失败语音只加入本轮排除集合，不得连坐后来出现的其他语音。待处理集合缩小或变化即视为有进展。只有同一稳定待处理集合连续不变、sidecar 明确失败、授权撤销或会话无法确认时才结束，并将明确失败项同批上报门禁。

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
| `disable_reason` | 永久停用或历史替代原因；临时暂停不得填写。正式值至少包括 `customer_hard_opt_out / conversation_closed / remark_code_removed_confirmed / admin_disabled / replaced_binding`。 |
| `disabled_at / disabled_by` | 永久停用时间和操作者/系统来源；缺少来源的旧 `disabled` 不得直接当作有效永久停用。 |
| `replacement_binding_id` | 旧绑定被替代时指向当前绑定；被替代记录必须退出正常扫描候选。 |
| `first_seen_at` | 首次扫描到时间。 |
| `last_seen_at` | 最近扫描到时间。 |
| `last_scan_snapshot` | 最近一次扫描摘要或截图证据引用。 |
| `last_read_dispatched_at` | 最近一次被调度下发时间，只用于租约和公平排序，不代表读取完成。 |
| `last_read_completed_at` | 最近一次完整读取并被后端结算的时间。 |
| `last_read_result` | `new_facts / no_change / failed`。 |
| `no_change_read_count` | 连续完整读取但没有新事实的次数。 |
| `next_read_due_at` | 后端允许下一次状态机定向读取的最早时间。 |

说明：`conversation_type=private/group/unknown` 是 Worker 每次点击后根据顶部标题生成的准入证据，放在扫描/定位原始证据中即可；不为它新增独立数据库状态机字段，也不允许它改变 `conversation_id` 或消息去重身份。

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

V3 结构化证据还必须保存在 `raw_payload`：`source_message_key / row_kind / sender_role_source / item_state / flow_state / worker_stable_id / frame_visual_id`；其中 `worker_stable_id` 是 OCR 跨轮业务身份，`frame_visual_id` 只是本帧诊断证据。语音额外保存稳定 anchor 和 `voice_transcription_meta`；图片只保存白名单投影后的 `customer_image_understanding / visual_bridge_input`。这些字段用于证明“哪条气泡、谁发的、是否完成”，不是新增业务状态机。

说明：`message_events` 保存已准入的消息事实，包括结构完整的失败图片事实；不保存 `duplicated / ignored / discovered / pending`。初次图片角色不可信时尚未形成可入库消息，只上报稳定帧级身份门禁。接口处理结果继续放在 `results[].ingest_result` 和 `error_code` 中。

### 6.2 C2服务端接口契约

C2 消息接口只接收 Worker 上报的微信事实，不接收 Worker 生成的 AI 回复内容，也不直接下发发送动作。消息入库后，后端状态机可以基于同一批事实启动 Brain、创建 `reply_action` 和任务中心 `chat_reply`；当前持锁的 C2 单会话流程再按批次合同领取并执行该任务。

| 接口编号 | 方法 | 路径 | 用途 |
|---|---|---|---|
| `API-C2-01` | POST | `/api/workers/{worker_id}/wechat/sessions/scan-result` | Worker 上报会话扫描结果。 |
| `API-C2-02` | GET | `/api/workers/{worker_id}/wechat/sessions/read-targets` | Worker 拉取需要读取消息的已绑定会话；`limit` 是查询参数，不属于路径名。 |
| `API-C2-03` | GET | `/api/workers/{worker_id}/wechat/conversations/{conversation_id}/read-authorization` | Worker 在实际读取前复核当前授权；批次续读时携带 continuation 参数与 Header。 |
| `API-C2-04` | POST | `/api/workers/{worker_id}/wechat/conversations/{conversation_id}/activation-confirm` | Worker 上报有效短码、private 单聊和可读取会话的首次激活证据。 |
| `API-C2-05` | POST | `/api/workers/{worker_id}/wechat/messages/ingest` | Worker 上报已绑定会话的消息事实或受控空读结算。 |
| `API-C2-06` | GET | `/api/workers/{worker_id}/wechat/message-batches/{batch_id}` | 当前单会话事务查询 Brain 批次、发送任务和续读授权。 |
| `API-C2-ADMIN-01` | GET | `/api/conversations/{conversation_id}/wechat-binding` | 后台查询微信绑定状态。 |
| `API-C2-ADMIN-02` | GET | `/api/conversations/{conversation_id}/messages` | 后台查询消息入库记录。 |
| `API-C2-ADMIN-03` | GET | `/api/leads/{lead_id}/wechat-bindings` | 后台按线索查询微信绑定历史。 |
| `API-C2-ADMIN-04` | POST | `/api/conversations/{conversation_id}/wechat-binding/restore` | 后台对非永久终止会话执行可审计的监听恢复；明确拒绝、关闭或短码移除时拒绝恢复。 |

本节中的查询参数和 Header 是同一接口的调用参数，不产生新的接口名。例如 Worker
代码中的相对路径 `/workers/...` 只是因为其 `base_url` 已包含 `/api`；正式接口仍为
上表从 `/api` 开始的完整路径。

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
| `unread_hint` | boolean | 否 | 当前首屏是否存在未读/红点观察事实，缺省按 `false` 处理。该字段只能触发后端重算 `visible_unread` 读取许可，不能被 Worker 直接当作点击、入库或回复授权。 |
| `last_message_preview` | string | 否 | 列表预览文本。 |
| `last_message_preview_time` | string | 否 | 列表可靠识别到的预览时间文本；无法可靠识别时为空，不得用扫描时间伪造。只作为后端判定新未读代次的语义证据之一。 |
| `last_message_observation_id` | string | 否 | 仅允许上报微信/底层能力对同一物理消息稳定不变、对新物理消息变化的原生观察 ID。无这种能力时必须为空；禁止用 `scan_id`、截图哈希、OCR 框、行号、坐标或随机值伪造。 |
| `ocr_confidence` | number | 否 | OCR 置信度。 |

C2 会话身份只允许一个决策点：OmniAuto Sidecar 负责从微信界面确认标题行、判定
`private / group / unknown`、提取固定八位正式短码，并输出最终准入结果。正式短码生产语法统一为
`CJ[A-Z0-9]{6}`，历史可变长度短码不参与正式 C2 身份链路。

Worker 不得读取 `raw_title`、不得提取短码，也不得重新判定单聊、群聊或
`unknown`。Worker 只校验 Sidecar 的 `c2_remark_code_candidates` 与
`c2_conversation_admission`：仅当结果是 `private + admission_allowed=true + 恰好一个固定八位短码`，且列表短码与准入对象中的 `remark_code` 一致时，才可以用该短码匹配后端授权任务。`group / unknown / admission_allowed=false`
全部不准入；字段缺失、格式错误或彼此矛盾必须失败关闭，记录
`C2_SIDECAR_IDENTITY_CONTRACT_INVALID`，不得尝试“修复” Sidecar 结论。

`sessions[]` 不新增 `conversation_type` 字段：群聊/unknown 的会话行通过清空 `remark_code_candidates` 阻止自动绑定；扫描级 `evidence.c2_conversation_admission` 只保存 private/group/unknown 数量和规则摘要。点击目标后的详细 `raw_title / conversation_type / conversation_type_reason` 保存在 Worker/Sidecar 定位证据中，用于本轮终止判断，不发散新的后端绑定字段。

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
| `bind_status` | `bound / already_bound / unbound / needs_review / binding_failed / disabled`。被替代的旧绑定不作为当前绑定状态返回，以 `recovery_state=retired` 表示。 |
| `error_code` | 未绑定、绑定失败或需要人工检查的原因。 |
| `can_ingest_messages` | 是否允许 Worker 后续读取并上报消息。 |
| `disable_reason` | 当前绑定为永久停用时返回停用来源；临时暂停不得返回该字段。 |
| `recovery_state` | `none / paused_waiting_worker / needs_review / permanently_disabled / retired`，用于说明为什么本次扫描后没有恢复监听。 |

扫描结果处理必须遵守以下边界：

- 当前绑定为明确永久停用时，仍记录本次扫描事实，但不得仅凭重新看见短码自动恢复。
- 当前绑定是历史遗留的 `disabled + paused`，且会话仍有效、无明确停用来源、没有被新绑定替代时，不得在更新扫描时间后直接早退；必须先按 6.3.4 迁移，再返回 `paused_waiting_worker` 或正常绑定结果。
- `deleted_at` 非空或存在 `replacement_binding_id` 的旧绑定只作为历史证据，固定返回 `retired`，不得重新成为当前绑定。
- 同一短码命中多个活动绑定、归属错误或永久停用来源不完整时返回 `needs_review`，不得猜测恢复。

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
| `read_reason` | `friend_acceptance_visible_hit / recall_precheck / visible_unread / recent_ai_sent / waiting_user_reply / waiting_sales_reply`。其中 `visible_unread` 是由最新首屏未读事实触发的临时读取原因，不是会话主状态。 |
| `authorization_revision` | 当前监听授权版本；必填。停止监听或重新授权后会变化，旧版本请求不得入库。 |
| `unread_generation` | 后端必填的未读代次快照。读取票创建时冻结当时代次；无任何待处理未读代次时为 `0`。 |
| `identity_checkpoint` | 本会话服务端身份检查点；包含下一编号下限和最近消息身份，Worker 本地状态丢失后必须先合并。 |
| `next_read_due_at` | 本次目标的服务端到期时间；未到期的长期状态目标原则上不应出现在列表中，Worker 即使收到也不得提前点击。 |

契约要求：

- 正常 `read-targets.targets[]` 必须包含 `conversation_id + remark_code + authorization_revision`。
- `read_reason=visible_unread` 时，后端必须能证明当前绑定的最新成功扫描事实为 `unread_hint=true`，且会话为 `ai_active`。Worker 不得自行把本地 `visible_hit` 改写成该服务端 `read_reason`，也不得因本轮没有 `visible_hit/local_unread_hint` 而否决后端当前授权；首屏未命中必须进入正式短码搜索。
- 所有 `read-targets` 都必须携带创建读取票时冻结的 `unread_generation`；Worker 必须原样保留到 `messages/ingest`。一次由 `waiting_sales_reply`等其他原因触发的完整读取，也可以结算该票已覆盖的未读代次；但后端只能消费票上冻结的值。中途失败、未确认入库或仅写本地 ledger 不得消费。
- 已绑定会话如果缺少 `remark_code`，不得出现在正常 `read-targets` 中，应进入 `needs_review / degraded` 并记录 `C2_TARGET_REMARK_CODE_MISSING`。
- Worker 收到缺少 `remark_code` 的读取目标时，必须跳过读取，不得继续打开微信会话或上报消息。
- Worker 本轮读取去重以 `conversation_id + remark_code` 作为身份键；服务端身份收口必须满足 `conversation_id + remark_code`。
- `authorization_revision` 只代表本轮读取许可，不参与会话身份和消息 `dedupe_key`。
- `identity_checkpoint.recent_messages` 必须覆盖最近 Worker 序号消息，不得因其来源为 `worker_sequence` 而排除；`next_sequence_floor` 必须大于历史最大已用序号。
- 微信定位分两段：第一屏可见目标可用 `rpa_session_key / display_name / remark_code` 快速定位；第一屏未命中或非第一屏目标必须通过微信搜索框搜索 `remark_code`，并在进入会话后再次确认标题/备注包含该短码。
- `rpa_session_key`、`display_name`、`row_fingerprint` 仅作辅助定位和排查证据，不能替代 `remark_code` 做非第一屏定向读取。

`visible_unread` 发布前必须具备的自动化门禁：

1. 新绑定 `ai_active` 会话首次上报 `unread_hint=true`，立即进入 `read-targets`，`read_reason=visible_unread`。
2. 已绑定但无其他待处理状态的 `ai_active` 会话再次发生未读，同样能获得读取许可。
3. 完整入库/结算确认后当前 `unread_generation` 被消费；后续扫描仍观察到同一红点和同一预览证据时，不清空 `next_read_due_at`、不在冷却内重复派发。
4. 定位、类型确认、读取或入库中途失败时不消费，冷却后可安全重试。
5. 后续成功扫描上报 `unread_hint=false` 后不再派发 `visible_unread`，并为下一次可证明的 `false -> true` 重新触发做好状态记忆。
6. 无短码、多短码、同码多会话、绑定冲突、错 Worker、监听暂停/禁用、授权过期和关闭/拒绝会话均不得获得该授权。
7. Worker 本地仅有 `visible_hit` 而服务端没有同一会话的当前 `read-target` 时，必须不点击、不读取、不转写、不入库。
8. 重复上报同一 `scan_id` 不得创建新绑定、改变授权版本或制造第二份未读事实。
9. 不同 `scan_id` 但红点、规范化预览和预览时间均未变时，必须复用原 `unread_generation`；会话行上下移动、OCR bbox 变化和置信度波动不得制造新代次。
10. 读取N期间到达新消息并建立N+1时，N结算后N+1仍能获得读取许可，不得被误消费。
11. 完全相同的新预览无法证明新代次时，冷却内不提前读取，但到 `next_read_due_at` 后必须能完整复核。
12. 被动扫描零点击；结算后的消红点点击最多一次且只是 UI 收尾。点击失败、红点不消失或 Worker 重启后，同一已消费代次仍不得在冷却内重读。

#### 6.2.3 单会话读取前复核授权

```http
GET /api/workers/{worker_id}/wechat/conversations/{conversation_id}/read-authorization
```

该接口是 `API-C2-03`，不是 `read-targets` 的别名。`read-targets` 负责调度候选，
本接口负责 Worker 已定位到具体会话后、每个实际微信 UI 读取动作之前复核当前授权。
普通读取必须与当前 `conversation_id + remark_code + authorization_revision + read_reason`
一致，其中返回的 `read_reason` 只作为 `authorization_read_reason` 保存和匹配，不决定
Worker 当前 `operation_phase`；批次续读可增加查询参数 `continuation_batch_id` 和 Header
`X-C2-Continuation-Token`，两者仍属于 `API-C2-03`。
单会话授权响应应返回后端当前 `unread_generation / consumed_unread_generation` 用于审计，但不得替换 Worker 已领取票据上冻结的 `unread_generation`。复核时已出现N+1不影响Worker完成N；N结算后N+1仍保持待处理。

授权撤销、版本过期、Worker/会话不匹配或 continuation 不匹配时必须返回不允许，
Worker 不得继续点击、转写、图片读取或消息入库。

允许读取时响应还必须返回当前 `identity_checkpoint`。身份碰撞后的 Outbox 恢复可以
通过本接口刷新检查点，不依赖目标再次进入 `read-targets` 的前 20 条，也不得为了刷新
身份重新打开微信。

#### 6.2.4 首次激活确认

```http
POST /api/workers/{worker_id}/wechat/conversations/{conversation_id}/activation-confirm
```

该接口是 `API-C2-04`。仅用于 `invite_sent / already_friend` 首屏绑定后的首次激活读取，
且 Worker 当前必须是 `operation_phase=authorized_read`、授权来源必须是
`authorization_read_reason=friend_acceptance_visible_hit`；发送前复读不得调用。
请求必须携带当前 `authorization_revision`、正确 `remark_code`、
`conversation_type=private`、`chat_surface_ready=true`，并在 `title_evidence` 中证明
短码已确认且单聊准入通过。成功后统一进入
`friend_state=friend_active + status=friend_activation_reading`；重复提交同一有效证据必须
幂等。群聊、未知会话、短码不符、授权过期或好友事实/会话状态组合不一致时不得推进。

#### 6.2.5 上报消息事件

```http
POST /api/workers/{worker_id}/wechat/messages/ingest
```

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `contract_version` | integer | 是 | 协议结构代号固定为 `3`，不是发布版本；当前灰度目标由 `contract_revision=0.9.45` 表达，并同时校验最终实现真实生成的 `contract_sha256` 和 `observation_schema_version`。冻结前旧候选 SHA `864ae406...af89` 不再有效。 |
| `read_run_id` | string | 是 | 本次读取运行 ID。 |
| `conversation_id` | string | 是 | 服务端已绑定会话 ID。 |
| `remark_code` | string | 是 | 本轮已确认的客户短码。 |
| `authorization_revision` | string | 是 | 必须与服务端当前 read-target 授权一致。 |
| `unread_generation` | integer | 是 | 必须原样回传本次 read-target 冻结的代次，无待处理代次时为 `0`。后端仅能消费该值，不得用绑定当前最新值兜底。 |
| `rpa_session_key` | string | 否 | 本机会话定位键；第一屏读取时建议上报，短码搜索读取时可为空或上报搜索后重新识别到的定位键。 |
| `messages` | array | 是 | 本次读取到的消息事实。 |
| `evidence` | object | 否 | 截图、日志、OCR摘要。 |

`sidecar_run_id` 放入 `evidence.sidecar_run_id` 和每条原始证据中，不新增为后端请求顶层必填字段，避免 Worker 与后端产生一组实际未消费的冗余接口字段。

上述 `last_message_preview_time / last_message_observation_id / unread_generation` 已作为 `0.9.5` 机器合同变更一次性同步到客户端、Sidecar、后端、生成 Schema、样例和测试。旧 `0.9.4` 请求必须返回 revision mismatch，不得静默兼容或用旧包冒充。

`0.9.4 -> 0.9.5` 替换前必须暂停旧 Worker，确认消息 Outbox、媒体 Journal 和发送回执均无未结算事实；如仍有 pending 事实，必须先用原 `0.9.4` 后端结算，不得直接删除。然后先升级后端及迁移 `20260814_0027`，再替换 `0.9.5` Worker 并恢复接单，禁止新旧合同混跑。

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
| `canonical_voice_action_id` | 本次读取轮次内的语音动作身份；只用于请求/响应、Journal 和前后帧证据串联。 |
| `worker_stable_id` | 已恢复或已提交的跨轮业务身份；未提交前不得生成 `source_message_key`。 |
| `anchor_aliases` | 本次动作帧内的点击/对齐观察别名；不是稳定身份，不得跨轮去重。 |
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
| `results[]` | 每条消息的处理结果，包含 `dedupe_key`、`ingest_result=ingested/duplicated/ignored`、`message_event_id`、`error_code`；身份碰撞不得伪装成其中任一成功结果。 |
| `next_action` | 兼容字段，固定为 `none`；发送动作不从消息入库响应直接下发。 |
| `message_batch` | 可选。后端因本批事实启动 C3 时返回 `batch_id / batch_status`，Worker 只按该批次继续查询、领取任务和执行发送。 |
| `read_completion` | 完整读取结算；包含 `result=new_facts/no_change/failed`、`completed_at`、`no_change_read_count` 和 `next_read_due_at`。空消息和全重复的完整读取也必须返回。 |

发生身份碰撞时固定返回 HTTP 409，错误码 `MESSAGE_IDENTITY_COLLISION`，并在安全脱敏的
错误数据中返回 `recovery_action=refresh_identity_and_retry`、冲突项来源键和新的
`next_sequence_floor`。该响应不得消费未读事实、不得更新读取完成退避、不得推进
Conversation 或创建 Brain 批次。

`0.9.45` 目标按灰度版本规则统一使用 `contract_revision=0.9.45`；代码、Schema、样例和测试已按“实际动作结果先落 Journal、唯一连续性通过后绑定正式身份、技术故障禁止 Handoff”完成本地实现，新规范化 SHA 已真实计算为 `8813425572dad678b86354856dad798c43a9c47192d17319dfb8e84c8877e99e`。冻结前旧候选 SHA `864ae406...af89` 以及未覆盖正常视口滑动的 `edc5066...92c27` 均已作废。末尾媒体事实等价、同帧 guard 绑定、被动重读布局复查、完整画面与增量消息分离、最终分片完整证据和 unknown 显式身份门禁继续保留；
`API-C2-05` 请求和响应字段保持不变，严禁新增 `ingest_batch_id`、`outbox_batch_key` 或同义字段。
旧 `0.9.31` 请求固定按 `MESSAGE_CONTRACT_REVISION_MISMATCH` 拒绝，不允许双 revision 混跑。
C2-C3 单会话串行链路继续使用可选 `message_batch={batch_id,batch_status}`；派生接口合同只可
细化本手册字段，不得修改 `API-C2-05` 的方法、路径或另建同义消息上报接口。

#### 6.2.6 查询当前消息批次

```http
GET /api/workers/{worker_id}/wechat/message-batches/{batch_id}
```

该接口是 `API-C2-06`，由仍持有当前 C2 单会话事务的 Worker 轮询。响应必须返回
`batch_id / batch_status / processing / terminal / decision / error_code`；需要发送时还可
返回唯一 `reply_action` 和 `chat_reply task`，需要继续读取时返回 continuation 授权。
`decision=hard_opt_out`、`handoff`、`no_action`、失败或取消等非发送终态均必须以
`processing=false + terminal=true` 返回；Worker 将其作为“本次正常结束、不发送”，
不得继续等待、领取发送任务或自行生成回复。

#### 6.2.7 后台恢复微信监听

```http
POST /api/conversations/{conversation_id}/wechat-binding/restore
```

该接口是 `API-C2-ADMIN-04`。它只恢复“业务会话仍有效但绑定被临时暂停或历史错误
禁用”的情况，必须写操作日志、恢复原因、操作者和恢复前后状态。以下任一条件存在时
固定拒绝：

```text
客户已明确要求停止联系
conversation.status=closed/rejected
短码已按双证据确认移除
绑定已被replacement_binding_id替代或deleted_at非空
当前短码对应多个会话或归属其他Worker
```

恢复成功后先进入 `bound + paused + allow_listening=false`，增加
`authorization_revision`；只有当前 Worker 已开始接单且后续扫描再次证明唯一有效短码，
才进入 `bound + listening + allow_listening=true`。恢复接口本身不点击微信、不读取消息、
不启动 Brain。

### 6.3 C2状态流转

#### 6.3.1 微信绑定状态 `wechat_session_binding.bind_status`

| 状态 | 含义 | 进入条件 | 后续动作 |
|---|---|---|---|
| `unbound` | 尚未绑定微信会话。 | 线索已分配但未扫描到有效短码。 | 不读取消息，不自动回复。 |
| `binding_candidate` | 扫描到短码候选，等待服务端校验。 | Worker 上报 `remark_code_candidates`。 | 服务端校验 lead/sales/worker。 |
| `bound` | 已绑定唯一会话。 | 短码唯一匹配 lead/conversation/sales/worker。 | 允许进入消息读取。 |
| `needs_review` | 需要人工检查。 | 短码冲突、低置信度、会话特征异常。 | 不自动回复，可后台查看原因。 |
| `binding_failed` | 本次绑定失败。 | 微信不可控、扫描失败、短码无效等。 | 等待下次扫描或人工处理。 |
| `disabled` | 有明确来源的永久停用，不是临时暂停。 | 客户明确永久拒绝联系、会话关闭、短码双证据确认移除或后台人工永久停用。 | 停止读取，不自动回复；只允许满足安全条件的人工恢复接口处理。 |

#### 6.3.2 微信会话监听状态 `wechat_session_binding.listen_status`

| 状态 | 含义 | 进入条件 | 展示口径 |
|---|---|---|---|
| `not_started` | 未开始监听。 | 未绑定会话。 | 未绑定。 |
| `listening` | 正常监听。 | `wechat_session_binding.bind_status=bound` 且 Worker 在线可控。 | 监听中。 |
| `paused` | 暂停监听。 | Worker 暂停、全局开关关闭、静默规则要求。 | 已暂停。 |
| `degraded` | 降级监听。 | OCR 低置信、读取失败但未完全不可用。 | 监听异常。 |
| `error` | 监听失败。 | 连续扫描/读取失败或微信不可控。 | 失败，需处理。 |
| `disabled` | 永久停止监听。 | `bind_status=disabled` 且具备 `disable_reason/disabled_at/disabled_by`，或属于有替代记录的历史绑定。 | 已停止。 |

#### 6.3.3 消息入库响应结果 `results[].ingest_result`

| 结果 | 含义 |
|---|---|
| `ingested` | 新消息已入库，并生成 `message_event_id`。 |
| `duplicated` | `dedupe_key` 已存在，且已有消息与本次消息的发送方、类型、规范化正文和媒体稳定锚点全部相同，确认为同一条；不新增 `message_event`。 |
| `ignored` | 系统消息、低价值消息或状态不允许处理；不新增 `message_event`。 |

同一 `dedupe_key` 对应的任一身份内容不同都不是重复，而是
`MESSAGE_IDENTITY_COLLISION`。后端必须返回 409 并要求刷新身份后重传，不能把新消息
静默算成 `duplicated`。

#### 6.3.4 历史监听状态迁移

上线前对现有绑定执行一次可审计、可重复运行的数据迁移，逐条按以下顺序分类：

1. `deleted_at` 非空或存在 `replacement_binding_id`：标记为历史绑定，保持不可监听，所有当前绑定查询固定排除。
2. 存在客户明确拒绝、会话关闭/拒绝、短码双证据确认移除或后台人工永久停用来源：保持 `disabled + disabled + allow_listening=false`，补齐可追溯的 `disable_reason / disabled_at / disabled_by`，不得自动恢复。
3. 会话仍可联系、`ai_enabled=true`、线索仍已分配、`close_reason` 为空，同时记录为 `disabled + paused` 且无永久停用来源：判定为历史不一致，迁移为 `bound + paused + allow_listening=false`，授权版本加一。
4. 无法唯一判断当前绑定、短码对应多个会话或证据互相冲突：进入 `needs_review`，禁止自动读取和自动回复。

第 3 类数据迁移后不立即操作微信。只有当前 Worker 已开始接单，并且后续最新扫描再次
证明该短码唯一、会话归属正确时，才能恢复为 `bound + listening +
allow_listening=true`。迁移脚本必须输出每类数量和记录 ID，支持事务回滚；重复执行不得
再次增加授权版本或改变已经正确的记录。

状态原则：

- `bound` 是 C2 后续读取消息的唯一正常入口。
- `needs_review / binding_failed / unbound` 均不得触发 AI 回复。
- `needs_review / binding_failed / unbound / degraded / paused` 只是当前不可读或
  暂不可判定，不是媒体恢复的永久终止证明；存在已触发语音/图片动作或待确认
  Outbox 时，原事务身份可信则直接 `fact_only` 结算，身份暂不可证才
  `retry_later`，两者都不能丢弃本地记录。
- Worker 暂停、接单时段暂停和维护暂停只能写 `bind_status=bound + listen_status=paused`；点击开始接单后，在 Worker 归属、会话有效和授权版本一致时恢复为 `listening`。临时暂停不得写成 `bind_status=disabled`。
- `disabled` 优先级最高，但只有具备明确永久来源时才有效；会话关闭、客户明确永久拒绝、短码移除或后台人工永久停用后，后续扫描不得自动开启监听。
- `bind_status=disabled + listen_status=paused` 是非法组合；`conversation` 仍为可联系状态、`ai_enabled=true`、线索仍 assigned、`close_reason` 为空且绑定无明确停用来源时，属于历史不一致数据，不得继续用 `SESSION_BINDING_DISABLED` 永久早退。
- 历史不一致数据迁移为 `bound + paused + allow_listening=false`，增加授权版本；后续唯一短码扫描和 Worker 运行状态同时满足后才恢复监听。迁移不得修改明确永久停用或已被替代的历史记录。
- 被新绑定替代的旧记录必须具备 `deleted_at + replacement_binding_id`，查询 canonical binding 时固定排除，不能因为旧记录已有消息历史就重新成为当前绑定。
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
| `SESSION_BINDING_DISABLED` | 绑定具备明确的永久停用来源。 | 保持 `bind_status=disabled`，不得因扫描再次看见短码而自动恢复。 |
| `SESSION_BINDING_STATE_INCONSISTENT` | 活动业务会话落入 `disabled + paused`，但没有永久停用来源或状态证据互相冲突。 | 唯一可判定时按 6.3.4 迁移；无法唯一判定时进入 `needs_review`，不得永久早退或直接恢复。 |
| `SESSION_BINDING_RESTORE_FORBIDDEN` | 后台恢复请求命中永久拒绝、关闭、短码移除、历史替代、错 Worker 或多会话冲突。 | 返回 409，不修改绑定，不操作微信。 |
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
| `MESSAGE_INGEST_DUPLICATED` | 去重键已存在且角色、类型、规范化正文和媒体稳定锚点全部相同。 | 返回 duplicated，不算失败。 |
| `MESSAGE_IDENTITY_COLLISION` | 去重键已存在，但角色、类型、正文或媒体稳定锚点至少一项不同。 | 返回 409；保留原 Outbox，刷新服务端身份检查点，重新分配身份后重传，不重新读取微信。 |
| `C2_VISION_NOT_READY` | 新 C2 UI 流程启动前发现真实 Vision Provider 的 API Key、模型或地址缺失。 | 不开始新扫描、不打开会话；已有回执、Outbox 和 `settle_without_ui` 仍先恢复。 |
| `C2_IMAGE_SLOT_RECONFIRM_FAILED` | Worker 已创建 pending action，但 Sidecar 在最新帧无法按“当前类型+当前最下方未处理槽位”生成唯一物理目标。 | 明确零 UI 触发时以 `cancelled_before_trigger` 烧毁预留号，并从最新完整画面重建一次动作计划；新计划仍无法唯一时是客户端技术故障，结算 `technical_failed + Worker faulted`，零 Handoff。不得根据原行号、坐标或气泡指纹重认对象。 |
| `C2_IMAGE_MENU_OPERATION_FAILED` | 右键已执行，但真实菜单边界/类型/安全点击项无法唯一确认。 | 关闭菜单，不读取剪贴板、不调用 Vision；记录 `identity_unresolved`并结算客户端技术故障，不生成 failed 图片消息、不进入 Outbox/Handoff。`reason_detail` 只允许 `menu_panel_unconfirmed / menu_evidence_incomplete / menu_evidence_conflict / menu_copy_item_unsafe`。 |
| `C2_IMAGE_SOURCE_INVALID` | 动作计划声明是图片，但菜单证明为文字/语音，或点击复制后剪贴板稳定不是可解码位图。 | 这是目标/菜单/剪贴板不变量失败，不是已确认的客户图片内容理解失败。固定 `identity_unresolved -> technical_failed + Worker faulted`，零重复 UI、零正式图片消息、零 Handoff。 |
| `VOICE_TRANSCRIBE_FAILED` | 语音转文字整体失败，无法确认有效转写文本。 | 记录 failed 事实；customer 失败完成结算后按 L1 直接转人工，不生成自动澄清回复。 |
| `VOICE_TRANSCRIBE_CLICK_FAILED` | 语音点击未发生、是否发生无法确认，或点击目标与计划矛盾。 | 记录截图、OCR 和 ActionJournal，不重复点击；固定按客户端技术故障结算，零正式消息、零 Handoff。若已有唯一动作回执且微信明确报告转写失败，应使用 `VOICE_TRANSCRIBE_FAILED`，不得混用本码。 |
| `VOICE_TRANSCRIBE_LOCK_TIMEOUT` | 语音转写等待 Local WeChat UI Lock 超时。 | 本轮跳过，保持会话原状态，后续扫描可再尝试。 |
| `VOICE_TRANSCRIBE_EMPTY` | 转写动作完成但未产生新的可用文字。 | 不把语音时长当正文；customer 失败完成结算后直接转人工，不发送“请改发文字”等自动澄清回复。 |
| `VOICE_MESSAGE_UNCONFIRMED` | OCR 只识别到语音形态或时长，未确认发送方/父语音。 | 先进入 L2 自动重建；恢复前不猜内容，恢复失败且影响最新尾部才 handoff。 |
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
  预检。具有可信角色和稳定身份、且为 `current_read_run + not_enqueued` 的当前屏图片必须在同一 Flow 内结束为
  completed/failed，不允许图片 pending/deferred；初次角色不可信走帧级身份门禁，
  不写 ignored Ledger。
- 图片动作前重建最终画面后已经出屏的图片本轮不处理；仍在屏幕但无法唯一确认时形成 failed 事实，不允许反复右键或跨轮 Vision。
- 语音只识别到时长或转写失败时不得把时长当正文；customer failed 项完成结算后按 L1 直接转人工且不生成自动澄清，目标客户未确认、UI 锁超时或微信窗口不可控时进入 L2，恢复前不回复且不直接永久 handoff。
- 同一消息在 Worker 重启、断网恢复、重复扫描时不会重复入库和重复触发后续动作。
- 删除或损坏 Worker 本地身份状态后，Worker 必须先用服务端身份检查点恢复编号下限和最近身份；新消息不得从 1 重新编号，也不得撞上历史消息。
- 相同去重键且角色、类型、正文、媒体锚点都相同才返回 duplicated；故意构造相同键但不同角色或正文时必须返回 `MESSAGE_IDENTITY_COLLISION`，数据库并发冲突补偿分支结果相同。
- 身份碰撞恢复必须复用原 Outbox 和原始消息事实，只刷新身份后重传；不得重新打开微信、重复语音转写或重复调用 Vision。
- 完整读取没有新消息时，服务端按 2 分钟、5 分钟、10 分钟递增下一次读取时间；有新事实时也设置至少 2 分钟成功冷却。只有晚于完成时间的新未读、首次激活、召回前复核、有效业务状态变化或正式 continuation token 才能提前唤醒。
- `waiting_sales_reply` 仍需低频检查销售人工回复，但不能被 30 秒首屏扫描反复打开同一会话；读取调度以服务端 `next_read_due_at` 为准。
- 历史 `disabled + paused` 活动绑定必须按 6.3.4 迁移；明确永久停用、已删除或已被替代的历史绑定必须保持不可恢复。迁移重复执行应幂等，并输出可核对的分类清单。
- 点击开始接单只能恢复 `bound + paused` 且最新扫描唯一确认的当前绑定，不能恢复客户明确拒绝、已关闭、短码移除、多会话冲突或被替代的旧绑定。
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

上述“必须由后续销售消息解除”只适用于 L3 业务接管和人工 `pause`。仅由 L2 身份、历史
缺口或技术恢复失败形成的旧 handoff，在一次新的权威读取证明 `reply_safe_suffix` 完整且
不再命中原错误后，服务端必须以 `auto_recovered_clean_read` 自动关闭并恢复普通 AI，
不得要求销售为了恢复系统而先发一条无业务意义的消息。

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

发送结果的气泡类型和本次发送归属必须分层判断：全屏 OCR 已识别为右侧文字时直接使用该文字事实；若全屏 OCR 漏识别、但结构观察将最底部右侧气泡投影为图片候选，则同排头像确认 `self` 且局部放大增强 OCR 存在可读文字时，必须先恢复为 `text`，不得继续保留为图片。是否属于本次程序发送正文另行比较：先统一 NFKC 全半角、大小写、普通/全角/不换行/零宽空白、中英文句号、引号、横线、省略号、括号和 Emoji 变体等呈现差异；规范化后完全一致直接确认，非完全一致时要求双方有序字符覆盖率和整体相似度均至少 `80%`，并存在足够长的连续匹配片段。OCR 文字重合不足时仍为 `text`，但不得确认本次发送。当前目标未强确认、同排头像角色不明、该结构候选已存在于发送基线、候选不是发送后新增尾部事实、输入框未清空或当前窗口有更新聊天事实时，均不得确认发送，继续使用 `SEND_RESULT_UNKNOWN` 并禁止自动重发。保存的 OCR 原文不得被规范化结果覆盖。

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
- 模型首次技术失败进入 L2 有界恢复，不得用无依据的兜底话术冒充业务答案，也不得直接
  形成永久人工接管；恢复耗尽后可以发送 Guard 通过的“正在确认”边界说明并转人工。
- AI 只输出候选回复和动作建议，不拥有最终发送权。
- AI 文字回复属于 OmniAuto 接入 C3，禁止另行扩展平行自动发送流程。正式链路固定为
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
| `reply_action` | 服务端 AI 编排器 | 表示服务端批准的一次普通回复或 `reply_then_handoff` 边界回复 | Worker 只能发送 `status=queued` 且 claim 成功的 action。 |
| `chat_reply task` | 服务端任务中心 | 让 Worker 执行微信发送动作 | `task_type=chat_reply`，必须绑定唯一 `reply_action_id`。 |
| `sent_ack` | Worker | 证明某个 `reply_action` 已发送或发送失败 | `reply_action_id` 唯一；重复上报只返回既有结果。 |
| `handoff_event` | 服务端 | 记录转人工原因、触发消息、通知结果 | 转人工后进入 `waiting_sales_reply` 并阻断后续自由回复；`reply_then_handoff` 可在同批创建唯一边界回复任务。L2 技术接管允许权威干净读取自动关闭，L3 接管不自动关闭。 |

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
| `generating` | 已冻结本批消息，正在构建上下文、RAG 和模型回复 | `reply_action_created / handoff_created / no_action / rejected / superseded / failed` |
| `reply_action_created` | 已生成可发送回复，并创建 `reply_action`；`reply_then_handoff` 同时关联唯一 `handoff_event` | 终态 |
| `handoff_created` | 已判断需要转人工，并创建 `handoff_event` | 终态 |
| `no_action` | 判断无需回复，例如客户无效闲聊、静默、策略跳过 | 终态 |
| `rejected` | 当前批次内客户消息经结构化证据确认明确要求永久停止联系；会话同时进入 `rejected` | 终态，不创建回复动作，不发送礼貌性确认。 |
| `superseded` | 生成期间来了新消息，本批被新 batch 取代 | 终态 |
| `cancelled` | 会话关闭、拒绝、普通人工接管或短码移除导致取消；`reply_then_handoff` 自带的边界 action 不因同批 handoff 取消 | 终态 |
| `failed` | 上下文、RAG、模型或 Guard 技术异常 | 本次生成终态；会话进入 L2 `retry_wait`，恢复预算耗尽后才转人工。 |

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
| `cancelled` | 会话状态变化或普通人工接管导致取消；同批 `reply_then_handoff` action 除外 | 终态 |
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
| `created` | 服务端已创建转人工事实，尚未开始通知副作用。 |
| `notify_pending` | 内部通知 Outbox 已建立，等待车金后端发送；不是 `Task.task_type`。 |
| `notified` | 已通知销售。 |
| `notify_failed` | 通知失败，AI 仍保持停止。 |
| `sales_replied` | 后续检测到销售已回复。 |
| `auto_recovered` | 仅 L2 技术接管在后续权威干净读取后自动恢复。 |
| `closed` | 销售关闭托管、客户拒绝或会话结束。 |

### 7.3 C3接口定义

下表接口编号、HTTP 方法和从 `/api` 开始的完整路径是 C3 唯一正式名称，不得再按
“现有路由风格”自行调整。中文描述、内部服务函数名和省略 `/api` 的客户端相对路径
只用于解释或实现，不构成别名。

| 接口编号 | 方法 | 路径 | 调用方 |
|---|---|---|---|
| `API-C3-01` | POST | `/api/internal/conversations/{conversation_id}/message-batches/collect` | 后端内部收集消息批次 |
| `API-C3-02` | POST | `/api/internal/message-batches/{batch_id}/generate` | 后端内部运行 Brain/Guard |
| `API-TASK-01` | POST | `/api/tasks/{task_id}/claim` | Worker 领取统一任务中心的 `chat_reply`；复用前文同一接口编号 |
| `API-C3-03` | POST | `/api/reply-actions/{reply_action_id}/claim-send` | Worker 发送前原子领取回复动作 |
| `API-C3-04` | POST | `/api/reply-actions/{reply_action_id}/sent-ack` | Worker 上报唯一发送终态 |

当前消息批次的 Worker 查询固定复用 `API-C2-06`，不再为 C3 创建第二个“批次状态”
接口。

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
| `trace_id` | 单次技术请求或调用尝试的追踪ID；不得作为跨 C0—C4 的业务流程ID。跨阶段业务关联统一使用 15.2 定义的 `process_run_id`。 |

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
| `decision` | `send_reply / reply_then_handoff / handoff / no_action / retry_later / pause / hard_opt_out`。 |
| `reply_action_id` | `decision=send_reply/reply_then_handoff` 时返回。 |
| `handoff_event_id` | `decision=handoff/reply_then_handoff` 时返回。 |
| `error_code` | 失败时返回。 |

`hard_opt_out` 只允许在当前冻结批次内存在可核验的客户消息证据时成立：
`message_event_id` 必须属于当前 batch，消息归属当前会话且 `sender_role=customer`，
`source_message_key` 和规范化后的 `customer_text` 必须与入库事实完全一致。它只用于
“不要再联系我”等明确永久停止要求；“暂时不需要”“现在没空”“价格不合适”等软拒绝
不得进入该终态。证据有效时后端必须在同一事务中取消当前会话未发送的批次、回复动作
和任务，清空召回计划，设置 `conversation.status=rejected`、`ai_enabled=false`、
`message_batch.status=rejected`，且不生成任何礼貌回复。证据缺失或不匹配时固定降级为
`retry_later`，不得拒绝会话，也不得发送。

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
| 失败处理 | 超时、异常或输出不合法时先进入 L2 `retry_later`，按同一 batch 有界重试并可切换已批准 Provider；不得首次失败就永久 handoff。证据不足时区分普通对话、可澄清事实和硬风险：普通对话继续 Brain，可澄清事实使用安全说明/追问，只有必须由权威人员决定且无法安全说明时 handoff。不得用会编造业务事实的本地兜底。 |
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
| `reply_then_handoff` | 创建一次性 Guard 批准的边界/确认回复，同时创建 `handoff_event`；该 `reply_action` 允许在 `waiting_sales_reply` 下完成一次发送，之后停止普通实时 AI。发送失败或未知不影响 handoff 生效，也不得补发。 |
| `handoff` | 创建 `handoff_event`，状态进入 `waiting_sales_reply`，不自动关闭 `ai_enabled` 硬开关，不创建发送任务。 |
| `no_action` | 本轮不回复，记录原因。 |
| `pause` | 暂停会话自动回复，等待人工处理。 |
| `retry_later` | 模型/依赖短暂异常，按配置短延迟重试；超过次数转人工。 |

### 7.6 C3错误码

| error_code | 场景 | 处理 |
|---|---|---|
| `CONVERSATION_NOT_ELIGIBLE` | 会话未绑定、已关闭、已拒绝、人工明确关闭或短码无效 | 不生成回复；普通可恢复技术 handoff 不得永久落入该分支。 |
| `MESSAGE_BATCH_SUPERSEDED` | batch 生成期间被新消息取代 | 旧 batch/action 作废，使用新 batch。 |
| `AI_CONTEXT_BUILD_FAILED` | 上下文构建失败 | 从数据库和最新完整尾部重建；首次进入 L2，不直接 handoff。恢复耗尽且仍有未回复客户消息才转人工。 |
| `AI_ENGINE_UNAVAILABLE` | OmniAuto AI Engine Adapter 不可用 | 同 batch 有界重试/已批准 Provider 切换；恢复耗尽后转人工。 |
| `AI_ENGINE_TIMEOUT` | AI Engine 或当前文本模型 Provider 超时 | 同 batch 有界重试；不得伪装 `no_action`，也不得首次超时永久接管。 |
| `AI_ENGINE_CONTRACT_INVALID` | AI 输出不是合法结构化 JSON 或缺必要字段 | 丢弃非法输出并重试；禁止发送；恢复耗尽后转人工。 |
| `RAG_NO_EVIDENCE` | 无足够知识/车源证据支撑确定性事实 | 寒暄、需求澄清和常识流程可回复；事实问题明确说明需要确认并可 `reply_then_handoff`，不得编造，也不得一律静默。 |
| `GUARD_REWRITE_FAILED` | Guard 要求改写但改写失败 | 尝试一次确定性安全边界改写；仍失败才 handoff。 |
| `GUARD_BLOCKED` | Guard 阻断原候选 | 原文绝不发送；可生成不包含被阻断事实的边界回复并再次 Guard，通过则 `reply_then_handoff`，否则静默 handoff。 |
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
- Brain 技术失败必须进入 `failed/retry_later`，不得伪装成 `no_action`，也不得首次失败直接形成长期 handoff；RAG 证据不足时优先澄清或发送边界说明，Guard 阻断原文后优先安全改写，任何路径都不得编造回复。
- `no_relevant_business_evidence`、`missing_authoritative_evidence` 等软证据提示在进入 Brain、语义审稿、质量修复和 Guard 时必须保持同一语义。审稿请求不得重新暴露为 `must_handoff=true / allowed_auto_reply=false` 的硬授权结论，从而推翻 Brain 已生成的低风险澄清回复。
- Product Master 为空时，`ask_clarifying_question/collect_customer_info` 只要不声明具体车型、价格、库存、车况或政策事实，且风险为低、`facts_claimed` 为空，就允许继续通过确定性证据校验和语义审稿。普通需求澄清不要求先存在商品 ID。
- 如果审稿确认确需人工，修复后的 BrainPlan 必须包含一次客户可见边界承接并映射为 `reply_then_handoff`；如果 Brain 无法安全生成承接，才允许严格无可见回复并进入内部 handoff。任何空回复都不得被当作成功修复结果。
- 等待销售回复、客户拒绝、短码移除或会话关闭时，不创建普通实时 AI 发送任务；销售已回复只进入 `sales_replied_waiting_user`，客户再次回复时交回 AI，客户长期未回复时仍可进入 Brain 召回判断。
- OmniAuto AI Engine 只在服务端生成候选回复；OmniAuto RPA Sidecar 只在 Worker 端发送已批准文本。

| 主题 | 设计 |
|---|---|
| RAG方式 | RAG + 语义检索 + 关键词加权检索。语义检索理解意思，关键词检索抓住泡水、火烧、事故、贷款、定金、底价等关键风险词。 |
| Evidence Pack | 包含 conversation_context、customer_message、retrieved_knowledge、matched_cars、image_intent、risk_flags、allowed_fields。 |
| AI可见车源字段 | 品牌、车系、车型、年份、里程、城市、颜色、燃料、配置摘要、对外可说价格、车辆图片。 |
| AI不可见字段 | 采购价、销售底价、经理价、车主姓名、手机号、身份证、银行卡、内部备注。 |
| 动作输出 | send_reply、reply_then_handoff、handoff、no_action、pause、retry_later；需要发送的动作带 reply_action_id 和 expire_at，需要人工跟进的动作带 handoff_event_id。 |
| 调度边界 | `message_batch` 合并、旧 `reply_action` 作废、发送顺序和幂等判断由服务端会话调度器负责，不交给模型判断。 |
| 固定轮次限制 | 不设置固定 20 条自动停止规则；AI 是否继续由会话状态、风控、客户拒绝、人工接管、关闭托管和召回规则决定。 |

### 7.8 Guard检查

- 检查是否承诺无事故、无泡水、无火烧，是否承诺底价、最低价、贷款包过、定金可退，是否涉及合同、赔偿、投诉、法务，是否暴露系统规则或敏感字段。
- Guard 结果为 `pass`、`rewrite`、`handoff`、`block`；不通过时不发送原文，但除必须静默场景外允许生成一次不包含风险事实的边界候选重新过 Guard，通过后执行 `reply_then_handoff`。

| Guard层 | 说明 |
|---|---|
| 字段隔离 | 服务端构造 evidence pack 时先按白名单过滤，敏感字段不进入模型上下文。 |
| 规则检查 | 发送前使用规则词表检查底价、包过、绝对承诺、投诉法务等明确风险。 |
| 模型复核 | 对候选回复做二次安全判断，输出 `pass/rewrite/handoff/block` 及原因。 |
| 人工接管 | 规则或模型判断 `handoff/block` 时先区分“原文不能发”和“任何安全回复都不能发”。前者优先安全边界回复后接管，后者才静默接管。 |
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

图片流程必须先用可靠的文字、语音和父子结构解释画面，再对剩余区域生成图片候选；
引用文字和“语音条 + 展开转写正文”不得被重复当作图片。动作成功、anchor 匹配和后端
确认只决定事务结算，不决定消息类型。任何已建立的图片动作都必须形成 completed 或
failed 终态并进入 Ledger/Outbox，禁止永久停留在 `C2_IMAGE_FACT_PENDING`。

本章是图片状态、内存/剪贴板、Provider、跨轮上下文、产品权威边界和验收门禁的唯一口径。

### 8.1 唯一职责边界

- 图片与文字、语音属于同一个 C2 单会话 Flow，不另建图片扫描任务、图片上传接口或平行准入链路。
- Vision 的运行代码、Provider 网络请求和临时图片载荷均在 Windows Worker 客户端侧；车金后端不接收原图、不提供图片上传或 Vision 代理接口。正式客户端通过安装包内置的 Vision 客户端专用 Key 直接调用批准的 Provider，新电脑只需完成 Worker ID/Token 绑定。
- Brain 固定在服务端运行，只消费通过共享 schema 的图片文字化结果和服务端权威车辆/知识证据；Brain 不接收原图，也不持有客户端 Vision Key。
- 图片复用有效短码、`conversation_type=private`、`read-targets` 和 `authorization_revision` 门禁。
- OmniAuto 负责先生成结构图片候选、与已解析文字/语音完成类型仲裁，只对最终确认的 `image_bubble` 返回 `bubble_rect`，并执行当前剪贴板图片事务和 Vision 文字化理解。正式入口必须同时携带可信 `sender_role`、有效 `bubble_rect` 和含稳定指纹的 `image_physical_anchor`；任一缺失时必须在鼠标、剪贴板和 Provider 调用之前失败关闭。未上线的无类型 direct-port 兼容入口已删除，不保留旧主机旁路。
- 图片、文字、语音只使用一套 C2 `sender_role` 规则：同行左头像为 `customer`，同行右头像为 `self`；两侧同时成立或都不成立为 `unknown`。Vision 的 `side / visual_side` 只能作诊断证据，不得参与角色定案。
- Worker 负责最终画面统一槽位、`screen_order`、跨轮 `source_message_key/dedupe_key`、本地 ledger、Outbox 和 V3 映射。
- 后端负责授权、消息事实持久化、数据库最终去重、服务端权威车源匹配、跨轮图片上下文、`message_batch`、状态机、Brain/Guard、handoff 和 `chat_reply`。
- Vision 只理解图片，不生成客户可见回复；唯一回复作者是服务端 `customer_service_brain`。
- 会话内不存在图片 `pending/deferred`。全局能力未就绪在进入 C2 前阻断；具有可信角色和稳定身份、且为 `fact_scope=current_read_run + delivery_state=not_enqueued` 的当前屏图片必须在同一 Flow 内结束为 `completed/failed`。`ignored` 只允许在建立业务图片身份前表示已经明确证明不是聊天图片消息。
- 车金正式产品 ID 和车辆事实只由服务端 Product Master 确认；RAG 和 Vision 只能形成检索线索，不能独立认定价格、库存或车型。服务端允许通过 OmniAuto `KnowledgeRuntime` 读取已过 customer-safe projection 的 Product Master 证据；Worker/客户端本地知识不得成为正式事实源。

### 8.1.1 消息类型解释与图片候选仲裁

图片探测器输出的“大矩形、非背景表面、位于消息列、邻近头像”只能叫
`structural_image_candidate`，不能直接成为 `image_bubble` 业务事实。唯一处理顺序为：

1. 先解析文字、语音条、语音转写正文、父语音锚点、角色和画面顺序。
2. 将同一父语音的 `voice_bubble + voice_transcript` 合并为受保护的
   `explained_voice_region`。稳定父锚点优先；锚点暂不完整时，可使用同一角色、空间
   邻接、语音时长或语音菜单证据证明同一语音，但不得仅凭“区域里有文字”排除图片。
3. 在结构图片候选生成前，对与已证明文字/语音区域高重叠的视觉表面做负向排除；
   只对不能被现有消息结构解释的剩余区域生成图片候选。
4. 后置仲裁只作为安全兜底。任一可靠的类型证据已经证明该区域属于文字或语音，
   就必须保护原消息、否决图片候选；不得要求 `action_phase=confirmed`、转写业务成功、
   物理点击 `ok=true`、父锚点和全部 alias 同时满足。
5. `action_phase / effective_success / click.ok / 后端确认 / 精确 alias` 只决定动作是否
   可重放、事实如何结算，不决定消息在画面上是不是语音。
6. 弱几何候选与已解析消息冲突时，不得先用候选范围删除原文字或语音结果。只有完成
   类型仲裁并得到可靠图片证据后，才允许清理真正位于图片内部的 OCR 行。
7. “有 OCR 文字就不是图片”不是合法规则，避免把聊天截图、商品图等含文字真图片
   错误排除。

`structural_image_candidate / explained_voice_region` 是 OmniAuto 内部仲裁对象，不新增
后端接口字段。但本次语音身份生命周期已改变跨进程机器语义，不得继续沿用
灰度 `0.9.8`；权威画面枚举、媒体编排和安全误点恢复必须在同一版本中同步 OmniAuto、Worker、后端 schema、样例和合同测试。

### 8.2 单会话图片处理流程

```text
语音处理完成后的最终有效画面
-> 先建立文字、语音条、语音转写正文及父子关系
-> 合并并保护explained_voice_region，先排除能被强消息结构解释的视觉区域
-> 仅对剩余未解释表面生成structural_image_candidate
-> 以任一可靠文字/语音类型证据否决冲突图片候选；业务成功证据不参与类型定案
-> 完成类型仲裁后建立最终frame observations并按画面自上而下生成screen_order
-> 与checkpoint/动作前完整序列唯一对齐；历史committed图片使用原source key查询Ledger/Outbox，OLD不复制、OUTBOX只重传原JSON
-> 初次图片角色不可信时形成帧级MESSAGE_IDENTITY_UNCONFIRMED，不建立图片动作或消息、不写ignored Ledger
-> 只把唯一new_suffix、角色可信且未匹配历史正式事实/Outbox的图片建立为pending_media_action
-> 原子预留不可复用reserved ID、保存动作前完整序列和ActionJournal；此时不生成source key、不查询或写入Ledger/Outbox
-> 再次确认图片bubble_rect、同行头像角色、当前短码、private和authorization_revision
-> 刷新后的同行头像角色必须与初始C2角色一致；另一角色或unknown且明确零触发时cancelled_before_trigger，烧毁预留号，不形成failed图片消息
-> 页面已经变化时先重建完整final_read；图片已出屏且明确零触发时cancelled_before_trigger；仍可见但无法唯一匹配时同样取消并从最新帧重新仲裁
-> 右键疑似图片并OCR识别完整菜单
-> 从同一次右键截图唯一确认真实menu_panel_bounds；右键点周围的大区域不得充当弹窗边界
-> 只使用bounds完整位于同一menu_panel_bounds、同一纵向菜单列内的菜单项精确分类；仅允许去掉菜单文字末尾省略号，不做包含式或模糊匹配
-> 精确出现“放大阅读”，或“翻译”与“搜一搜”同时出现，确认为文字菜单
-> 精确出现“语音转文字”或“收起文字”，确认为语音菜单
-> 精确出现“复制”，并至少出现“编辑/用窗口打开/另存为/打开方式”之一，才确认为图片菜单
-> “复制/转发/收藏/多选/提醒/引用/删除”均为公共项，不能单独证明消息类型
-> 文字或语音菜单：关闭菜单、零复制、零剪贴板读取、零Vision；说明当前图片动作计划点到了错类型，固定identity_unresolved -> technical_failed，不伪造failed图片消息
-> 只有公共项、证据不足或多类特征冲突：关闭菜单，固定C2_IMAGE_MENU_OPERATION_FAILED -> identity_unresolved -> technical_failed，不点击任何菜单项、不产生业务failed事实
-> 只有确认为图片菜单后，才按已验证的图片菜单口径点击复制
-> 从Windows剪贴板把位图读入当前进程内存
-> 剪贴板稳定确认不是位图时，得到C2_IMAGE_SOURCE_INVALID -> identity_unresolved -> technical_failed，不调用Vision、不生成业务failed图片消息
-> 按原始位图内存上限解码，再缩放和自适应编码到Provider载荷上限
-> 对实际复制出的图片内存字节计算SHA-256并记录剪贴板代次；气泡截图/ROI指纹只留作诊断
-> Worker进程内调用OmniAuto BuiltinVisionPlugin，并使用正式包内置的客户端专用Key直接请求批准的真实Vision Provider
-> 使用共享JSON Schema校验结果，得到media_result=completed / failed
-> 取得动作后最新完整画面和action result receipt，严格校验action/reserved、菜单、点击次数、剪贴板代次、实际图片字节SHA和结果候选数
-> 回执完整且结果唯一：Worker经唯一commit_message_identity提交原reserved ID，形成committed_completed/committed_failed；此后才生成source key并投递Ledger/Outbox
-> 回执缺失、空白、未知、多结果、错对象或矛盾：形成identity_unresolved故障记录，不生成图片消息/source key/Ledger/Outbox，不重复任何图片UI/Vision
-> identity_unresolved立即结算当前task/Flow=technical_failed，释放UI锁并将Worker置为faulted；零Handoff、零飞书，不做无UI多轮补证
-> 释放该图片内存
-> 只把committed图片结果回填原screen_order，不在批次末尾另行追加；待处理/隔离对象不成为业务槽位
-> 按最终screen_order与本轮已committed文字、语音共同调用现有messages/ingest
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
- 正式包的 Vision 预检必须验证“内置凭据存在且可用、Provider/HTTPS 接口/模型/
  request_style 与批准白名单一致”，但输出只能包含 `configured=true/false`、凭据
  来源类型、Provider、模型和稳定错误码，禁止输出 Key、Authorization 头或可还原
  片段。正式包不得因新电脑缺少 Windows 环境变量而进入 `vision_not_ready`；开发
  包才允许通过环境变量替换凭据。正式包禁止只读取
  `CUSTOMER_IMAGE_UNDERSTANDING_API_KEY`；必须满足安装包内置受控凭据和新电脑开箱可用要求。
- Provider 地址必须为 HTTPS（显式本地开发模式除外），请求风格使用白名单。一次
  非 JSON 格式纠正重试属于合法流程，父进程安全预算必须覆盖两次请求，不能用
  `单次timeout + 5秒` 提前杀死第二次请求。

Vision 正式凭据交付规则：

1. Git 仓库、源码压缩包、PR、Actions 日志和普通构建产物中不得出现真实 Key。
2. 正式 Windows 构建从受控 CI Secret 注入一个客户端专用 Key；开发构建不得冒充正式包。
3. 内置 Key 只能访问批准的 Vision Provider、HTTPS 接口、模型和请求类型，并配置单机/全局额度、限流、异常用量告警、立即吊销和版本轮换能力。
4. Key 不得作为包根目录下可编辑的 `.env`、配置说明、`.txt` 或 PowerShell 参数
   交给用户配置；允许作为正式包 `_internal` 内部运行资源由凭据解析器读取并提供给
   Vision 子进程。该资源仍属于客户端可提取边界，不得宣传为加密保险箱；运行日志、
   子进程协议、预检报告、manifest、Actions 日志和故障 ZIP 必须脱敏。
5. 由于客户端必须能够使用该 Key，无法承诺绝对不可提取；本项目不使用虚假“本地加密即绝对安全”的表述，安全目标是降低普通泄露、限制被盗后的权限和损失，并能快速吊销轮换。
6. Key 轮换形成新客户端候选版本、新提交和新安装包 SHA；旧 Key 在迁移窗口结束后吊销。
7. Windows UAT 必须从干净新电脑验证：不预设 Vision 环境变量，只输入 Worker ID/Token，也能通过 Vision 预检并完成真实图片识别。

### 8.4 成功、失败与重试口径

| 场景 | 处理 |
|---|---|
| 新图片识别成功 | 先验证本次 confirmed action receipt 并经唯一提交门形成 `committed_message`，再以 `message_type=image + item_state=completed` 保存文字白名单结果并按原槽位顺序入库。Vision 成功但身份回执缺失/矛盾时进入隔离，不得形成 completed 消息。 |
| 已确认的图片内容处理失败 | 必须先唯一确认 action/reserved、图片菜单、复制点击、剪贴板新代次和实际图片字节 SHA-256，随后批准的 Vision Provider 明确返回处理失败，才允许形成 `committed_failed + C2_IMAGE_UNDERSTANDING_FAILED`。对象或回执不唯一时不得伪造 failed 消息。 |
| 图片候选右键后确认为文字菜单 | 说明当前图片动作计划选错物理类型。关闭菜单，零复制/剪贴板/Vision，固定 `C2_IMAGE_SOURCE_INVALID/text_context_menu_rejected -> identity_unresolved -> technical_failed`，不得形成 committed failed 或 Handoff。 |
| 图片候选右键后确认为语音菜单 | 说明当前图片动作计划选错物理类型。关闭菜单，零复制/剪贴板/Vision，固定 `C2_IMAGE_SOURCE_INVALID/voice_context_menu_rejected -> identity_unresolved -> technical_failed`，不得形成 committed failed 或 Handoff。 |
| 图片候选右键后确认为图片菜单 | 必须同时精确出现“复制”和至少一项“编辑/用窗口打开/另存为/打开方式”，才允许点击复制并继续剪贴板、指纹及 Vision 证明链。 |
| 右键菜单边界未确认、只有公共项、证据不足、分类冲突或复制项坐标不安全 | 关闭菜单，零复制/剪贴板/Vision，固定 `C2_IMAGE_MENU_OPERATION_FAILED -> identity_unresolved -> technical_failed`。不得因已执行右键就伪造 committed failed 图片事实。 |
| 已点复制但剪贴板稳定确认不是位图 | 停止剪贴板轮询且零 Vision，固定 `C2_IMAGE_SOURCE_INVALID/clipboard_current_content_not_bitmap -> identity_unresolved -> technical_failed`；不得进入 Outbox/Handoff，不得等待或重复点击。 |
| 已确认的 Provider 内容失败事实获后端逐 source key 确认 | 本地 ledger/ActionJournal 改为 confirmed 并释放 `C2_IMAGE_FACT_PENDING`。正常 `active_read` 下：customer 失败按 L1 直接转人工且不生成自动回复；self 失败只记 warning。`fact_settlement` 只补录事实，不改变当前状态。客户端技术故障没有 source key，不进入本行。 |
| 展开后的已转写语音与结构图片候选重叠 | 先以语音条、转写正文、父子关系和角色/空间证据形成 `explained_voice_region` 并否决图片候选；不得再次右键、复制或调用 Vision，不得删除原语音/正文。 |
| 语音类型证据已成立，但动作成功、父锚点或 alias 证据不完整 | 保持语音类型，按语音自身失败/恢复规则结算；不得降级成图片。业务结算证据缺失不能反向推翻已经成立的消息类型。 |
| 上述事实因后端暂时无法确认 | 保留 Outbox 并按退避重传，不重复右键、复制或 Vision；这是可观测的临时事务等待，不得因确定性代码错误永久卡住。 |
| 图片在动作前已被顶出最终当前屏 | 重建 final_read 后本轮不建立该图片槽位，不上滚、不追踪、不产生失败事实或 Brain 门禁；后续自然可见时重新观察。 |
| 图片动作前无法按当前帧规则得到唯一目标 | 明确零触发时以 `cancelled_before_trigger` 烧毁预留号，从最新完整画面重建一次计划；仍不唯一就按具体客户端技术故障结算，零 Handoff。不得根据旧行号/坐标/指纹恢复。 |
| 初次图片观察无法确认同行头像角色 | 尚未建立业务图片身份；返回 L2 帧级 `MESSAGE_IDENTITY_UNCONFIRMED`，零点击、零 Vision、零 terminal ledger；先自动恢复，旧区间问题不阻断最新完整尾部。 |
| customer 图片失败 | 同批已确认文字和语音继续入库；失败事实完成逐 source key 结算后按 L1 直接 handoff 并进入 `waiting_sales_reply`，不调用 Brain 回答同批文字，也不发送“请重发/描述”等自动澄清。 |
| self 图片失败 | 作为销售侧上下文 warning 入库，不阻断最新客户消息进入 Brain。 |
| Vision 配置缺失 | 新 C2 UI 流程启动前 `vision_not_ready`；不得开始扫描或打开会话，但不阻断已有回执、Outbox 和无 UI 事实结算。该状态不是图片消息状态，也不使用后端 `capability_paused`。 |
| 同屏语音失败 | 失败语音事实必须结算，但不阻止身份可靠的新图片继续完成媒体处理；若失败语音属于 customer，全部已发现媒体结算后直接转人工，不让同批文字进入 Brain，也不生成自动澄清。身份/历史异常仅按其实际影响范围进入 L2。 |
| 网络或后端未确认 | 完整 JSON 进入 Outbox；下轮只重传，不重复图片 RPA 或 Vision。 |
| 后端返回 duplicated | 不新增数据库记录，但用服务端原样返回的 `source_message_key` 确认本地 ledger，避免下轮重复处理。 |
| 当前图片动作计划或结果绑定不确定 | 不复制、不调用 Vision、不进入 L2 定时恢复；保存具体合同错误和完整证据，固定结算 `technical_failed + Worker faulted`，零 Handoff。 |
| 图片检测成功且数量为 0 | 按当前画面没有图片正常继续。 |
| 图片检测器或当前帧物理行解析异常 | 返回 `C2_IMAGE_OBSERVATION_FAILED` 结构化合同错误，禁止伪装成零图片。已证明只影响 reply-safe boundary 之前的旧区域时只告警；影响最新尾部或范围不明时按客户端技术故障结算，不进入 L2/Handoff。 |
| 图片候选超过内部处理容量 | 不得静默只返回前 8 张；全部观察，或返回 `C2_IMAGE_OBSERVATION_FAILED/observation_truncated=true`。已证明只截断历史旧区间时可不阻断最新尾部；可能覆盖最新待回复内容时固定按客户端技术故障结算，不进入 L2/Handoff。 |
| Vision 返回字段类型、范围或结构非法 | `failed`；字符串 `"false"`、NaN、越界置信度或仅靠默认空字段均不得成为 completed。 |

图片动作后的画面有效性只有一条规则：只要本轮曾对微信执行可能改变聊天画面的操作——包括
右键打开菜单、关闭菜单、点击复制、菜单被拒绝、剪贴板确认非位图或 Vision 前后的任何微信操作——
就必须立即设置 `ui_frame_invalidated=true`。只有 confirmed action receipt 已经通过唯一提交门的图片
failed/completed 事实可以先落 Ledger/Outbox；身份回执不足的 action 只能先落隔离记录，
但在处理下一条媒体、构建最终 `screen_order`、调用 ingest/Brain 或发送前，必须重新取得同一
private 会话、同一正式短码的最新稳定 frame，并执行统一序列对齐。只有“从 prepare 到终态期间
确实零微信 UI 操作”才能复用旧 `current_valid_frame`。禁止使用
`settled_without_refresh`、`action_phase=not_attempted` 或“没有点击复制”推导无需刷新；右键和关菜单
本身已经使旧帧失效。刷新失败只阻断当前会话，不回滚已经落盘的 failed/completed 事实，也不允许
拿动作前旧画面进入 Brain。

图片正式身份只能使用统一序列对齐恢复的历史 `worker_stable_id`
或经本次 confirmed 图片 action receipt 由预留号提交的 ID；新图片即使已确认属于 `new_suffix`，在图片动作确认前也只是 `pending_media_action`。`frame_visual_id` 只供本帧点击和排障。角色、同类序号和邻近稳定消息锚点
只是序列对齐证据，不得单独或组合生成长期身份。`image_hash` 只有复制后才能得到，只能增强证据，
不能作为复制前唯一去重条件；画面坐标、扫描编号、读取编号和扫描时间均不得成为消息身份。

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

### 8.5 双仓来源与发布规则

- OmniAuto 通用能力只允许来自一个固定上游提交；车金专属行为以 `chejin_overlays` 精确登记，不得伪装为上游能力。
- Worker 与内嵌 OmniAuto 的来源元数据、代码目录、机器合同和生成 schema 必须一致；禁止浮动分支、同一能力双份实现或来源字段与代码不符。
- 发布 manifest 必须从最终干净提交动态写入 Worker commit、OmniAuto commit、目录 tree SHA256、机器合同 revision/SHA、schema SHA、客户端版本和安装包 SHA256；不得手工复制旧版本哈希。
- 历史提交、候选状态、测试结果、包哈希和回滚点只写入《版本更新记录》，不进入本技术方案。
- 正式图片事务固定为：frame observation -> pending media action/预留号/Journal -> 动作前在当前帧重新定位目标 -> 精确确认当前弹窗菜单 -> 复制 -> 校验剪贴板新代次和可解码位图 -> 计算实际复制图片字节 SHA-256 并复核菜单/点击/剪贴板 action receipt -> 内存 Vision -> 四种动作终态之一。图片气泡/ROI 截图只作当前帧诊断，不参与结果身份。只有 committed completed/failed 形成图片消息；微信窗口 PID 或剪贴板 owner 不能成为额外硬门禁。

### 8.6 证据层级与不得回退项

图片候选只由 Worker/C2 的最终画面观察、同行头像 `sender_role` 和统一序列对齐决定是否进入待处理动作；
图片业务身份只有在历史 checkpoint 唯一恢复，或本次 confirmed action receipt 经唯一提交门提交后才成立，并由正式 `worker_stable_id` 生成 `source_message_key`。`frame_visual_id` 不得参与跨轮业务身份。
角色、同类序号和邻近锚点仅为对齐证据，不是身份生成器。新图片只有在唯一 `new_suffix` 中、角色可信、
没有匹配任何历史正式消息或既有 Outbox，且已经建立 `pending_media_action` 时才进入 OmniAuto；
`fact_scope/delivery_state` 只属于正式消息，不能作为动作前图片候选的身份证明。历史正式图片和既有 Outbox 图片不得重复执行图片动作。

OmniAuto 当前事务只负责证明：

```text
动作前原slot仍在当前屏
-> 记录sequence_before
-> 右键并识别完整局部菜单
-> 唯一确认真实menu_panel_bounds，菜单外OCR文字全部排除
-> 文字=(“放大阅读”或“翻译+搜一搜”)、语音=(“语音转文字/收起文字”)时否决候选并零复制
-> 图片=(“复制”且至少一个“编辑/用窗口打开/另存为/打开方式”)时才点击“复制”
-> 只有公共项、证据不足或分类冲突时关闭菜单并以菜单操作失败收口
-> sequence_after != sequence_before
-> 当前图片位图可解码
-> 读取前后sequence_after稳定
```

任何右键、关菜单或复制前，Worker 都必须已经原子写入 `pending_media_action`、预留号、
动作前完整序列和 ActionJournal；任何微信 UI 动作后都必须取得 post frame 和本次 action receipt。
现有 `action_phase` 只描述物理动作进度，不能单独证明正式身份，也不能替代四种动作终态。
完整菜单证明文字/语音误判、菜单证据不足/冲突、剪贴板非位图或 Vision 失败，只能先得到
`media_result=failed`；仍须由同一 action receipt 唯一证明操作对象后，才能经正式提交门形成
`committed_failed` 消息。回执缺失、空白、未知、多结果、错对象或矛盾时必须形成 `identity_unresolved`，
不得先把 failed 消息写入 Ledger/Outbox，也不得创建 HandoffEvent。

以上四种动作 terminal 写入是生产者硬义务：`finish_result()` 或等价唯一结果收口器必须原子地写入
`cancelled_before_trigger / committed_completed / committed_failed / identity_unresolved` 之一。
只有两种 committed 终态由唯一协调器幂等投递到 Ledger 和 Outbox；cancelled 只烧毁预留号，
identity_unresolved 只写故障证据并把当前 task/Flow 结算为 technical_failed，Worker 进入 faulted。若进程在
任意两步之间退出，重启只能续传已持久化的确定结果；已经触发或可能触发且结果仍不明确的 action 不得重新
打开微信、重新右键、重新复制或重新调用 Vision，也不得自动转人工。

这里的“后端确认”只适用于已经 committed 的原完整成功/失败消息，并且必须逐 source key 确认，
不能用 `messages=[]` 的空 flow gate 代替业务事实。identity_unresolved 没有 source key，只能作为客户端
技术事故上报并故障停止，不能创建 HandoffEvent，也不能伪造 failed 图片消息。只有 `fact_settlement` 时才固定
只补录已 committed 事实、不改变当前状态。

图片气泡/ROI 指纹只作为当前帧排障材料，不是 action receipt 的身份依据。当前帧 bounds 负责本次目标内点击；
正式结果必须依赖实际复制图片字节 SHA、菜单/点击/剪贴板代次和动作回执。动作前画面变化且明确零触发时取消
本 action，并从最新画面按固定规则选择当前一张；动作已触发后结果缺失、多结果或矛盾时进入
`identity_unresolved -> technical_failed`，不得在同 action、重启或下一轮再次右键、复制或调用 Vision。

必须保留：

- 空 Vision 结果不得成为成功终态，业务终态必须与 ActionJournal 一致。
- finally 剪贴板清理失败必须可见，且不得清除外部程序后来产生的新代次。
- 图片检测、当前帧重新定位、局部菜单和同 Flow 最多一次重新确认；气泡/ROI 指纹仅保留诊断，不参与跨帧身份或动作许可。
- 原始位图与 Provider 载荷大小分离、内存压缩、真实 Vision 和共享 schema。
- 既有短码 + private准入、统一 sender_role、文字、语音、最终顺序、当前屏不主动
  上滚、单会话UI锁、Outbox、授权、停止、Brain回复和召回流程。

不得建立图片专用平行接口、恢复旧图片入口或改变 C1/C2/C3 的职责边界。

### 8.7 自动化、Windows UAT 与后续回归

影响图片、媒体身份或消息顺序的候选必须按分层测试规则通过以下门禁：

本节首先验收正常主流程，不能只用新增安全反例证明候选可用。以下矩阵必须调用同一个生产会话入口，
并同时断言是否调用语音编排器、最终权威来源、ingest 内容与顺序、是否进入 Brain：

| 场景 | 语音编排器 | 权威来源 | 必须证明 |
|---|---|---|---|
| 纯文字 | 零调用 | `initial_read` | 原 observations 原样进入对齐和 ingest；无额外媒体 OCR，正常创建后续批次。 |
| 纯图片 | 零语音调用；只处理新图片 | 实际执行图片 UI 动作后为 `final_read` | 图片只处理一次；最终画面包含同屏新文字并按顺序 ingest。 |
| 纯未转写语音 | 调用唯一 `prepare/execute` | `final_read` | 正文只绑定被点击语音；不得另建 text；正常 ingest 或按明确失败转人工。 |
| 仅含已转写或历史语音 | 零调用 | `initial_read` | 不重复点击、不重复转写、不用 prepare empty 替换原 payload。 |
| 文字 + 未转写语音 | 仅对未转写语音调用 | `final_read` | 文字和语音保持最终画面顺序；两者都不能丢失。 |
| 语音 + 图片 / 文字 + 图片 | 按实际未转写语音调用 | 最后一次媒体 UI 动作后为 `final_read` | 每次 UI 变化作废旧帧；最终刷新后统一对齐再 ingest。 |
| 媒体动作期间新增同文文字或新媒体 | 只处理最新帧可执行新媒体 | `final_read` | 旧编号不重排；相同内容的新事实取得新编号；不漏入最新待回复尾部。 |
| 无 UI 动作的事务恢复 | 零调用 | `action_journal_recovery` | 只补录已落盘事实；不读取微信、不启动 Brain；一旦重新读取 UI 就改用实际 `initial_read/final_read`。 |

上述任一正常路径未 ingest、无新事实时误调用 Brain、纯文字/历史语音误进 voice prepare、
发生媒体动作后仍上报 `initial_read`、或任一模块接受枚举外值，均为发布阻断；不得用其他测试数量抵消。

1. 正式 ClipboardPort 和测试 Fake 均不再包含/依赖
   `claim_copy_ownership`。
2. 图片动作后五字段完整序列不变时，ActionJournal 必须保留本 flow 已终结的临时动作槽位；不重复复制已处理槽位，从当前序列选择下一张未处理图片。
3. 新序列唯一地只在尾部追加新文字/语音/图片时，保留旧已处理临时槽位，将唯一新后缀纳入最新动作计划；语音仍优先。
4. 新旧序列形成唯一“旧尾部=新头部”重叠段时，必须作为正常视口滑动继续；不得因旧头部离开可见区直接报错。零个/多个重叠解时只允许一次受限上下文扩展；扩展后仍不唯一，或证明替换、中间插入、换序、unknown/矛盾时，必须停止后续图片 UI，保留已终结回执并结算 `technical_failed + Worker faulted`；零重复点击、零 Handoff。
5. 两张外观相同且复制后画面不变的图片，必须依次为两个不同临时动作槽位各执行一次；即使实际图片字节 SHA-256 相同，也必须依据不同 action ID/槽位形成两个不同正式身份，不得去重成一条。
6. 空Vision摘要不得成为completed。
7. finally清理失败不得被吞掉；外部新代次不得被清除。
8. 所有实际图片失败原因都有机器合同精确映射。
9. `python3 run_checks.py`、后端C2/C3回归、合同生成、Python编译和
   `git diff --check` 全部通过。
10. 来源元数据能表达唯一上游基础和车金 overlay，打包脚本及 manifest 测试覆盖全部字段。
11. 正式包不依赖 Windows 预设 Vision 环境变量；从最终 ZIP 解压到干净电脑后，
    只输入 Worker ID/Token 即可通过真实 Vision 配置预检。
12. 最终包扫描、运行日志、故障 ZIP 和 Actions 日志均不得出现 Vision Key、
    Authorization 头或可还原片段；Provider/接口/模型白名单、额度和吊销状态可审计。
13. 使用真实复现截图回归：即使矩形探测器仍返回该候选，明确
    文字菜单也必须在复制前拦截，剪贴板读取次数为 0，Vision 调用次数为 0。
14. 剪贴板已稳定证明非位图时，只生成一份 `C2_IMAGE_SOURCE_INVALID -> identity_unresolved` 技术故障；不生成业务 failed 图片事实，不进入 Outbox/Handoff，不百次级轮询、不重新操作微信、不重复 Vision。
15. 只有对象、动作和实际媒体结果回执已唯一确认，但微信或 Provider 明确内容处理失败时，才允许生成正式 failed 事实并获得后端逐 source key 确认。正常 `active_read` 下客户确定失败按 L1 handoff，self 失败只作 warning；角色未知或动作回执不唯一不得伪造业务 failed 事实。
16. 升级前留下的 waiting ledger/ActionJournal 必须先按记录自身的合同版本分类。已具备当前正式身份的记录可原样重传；缺失新版必填序号/回执的旧记录必须进入 `legacy_media_recovery`，只允许“可证迁移并无 UI 结算”或“不可证时记录技术事故、结束旧 Flow 并将 Worker 置为 faulted”；不得把升级数据缺口转成客户 Handoff，不得要求测试人员手工删库、重新绑定或重装，也不得假在线地永久循环。
17. 使用真实连续大表面的展开语音截图回放：两条语音均已转写时结果必须是两条语音、
    零图片、零图片右键、零 Vision；即使某条动作成功或锚点 alias 证据不完整，只要可靠
    语音类型证据成立，也不得重新归类为图片。
18. 使用真实含文字车辆图、聊天截图和普通图片回放，证明前置负向排除没有退化为
    “有文字就不是图片”，真实图片仍能进入图片流程。
19. 不可删除的跨目标端到端门禁必须调用真实生产代码，不得在测试中直接构造 terminal：目标 A 在实际图片字节和动作回执已唯一确认后遇到 Vision Provider 内容处理失败 -> 生产端持久化正式 failed terminal -> Ledger/Outbox -> 后端逐 source key 确认 -> 当前客户按 L1 Handoff -> 全局事务门禁释放 -> 目标 B 必须继续执行。
20. 另一条反向链必须证明：菜单无法确认、复制结果非位图、无结果、多结果、错对象或回执矛盾 -> `identity_unresolved -> technical_failed + Worker faulted`；零 Ledger/Outbox/Handoff，目标 B 不得在 Worker faulted 期间开始。在每个落盘边界注入崩溃并重启，仍必须零重复 UI/Vision，并保留准确的业务失败或技术故障终态。
21. 使用两个真实语音气泡重放，且每个气泡同时产生 structural/stable 等多个 alias：
    首帧只能为已选中的第一条创建一个 action/Journal；第一次动作后废弃旧帧其他候选，从新帧为第二条生成不同 action/Journal。
    第一项失败后第二项继续，最终 completed+failed=2、已创建 action 的 `not_attempted=0`、实际右键次数不超过
    允许处理次数。
22. 在首条语音动作后注入 `MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS`：生产代码必须先
    结算所有已创建 action 的语音终态，再建立 L2 hold；未选中的旧帧候选不得有 Journal，也不得伪造终态。
23. `MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS/C2_MESSAGE_HISTORY_GAP` 必须覆盖四种结果：
    后端检查点自动恢复、旧区间不阻断最新尾部、第一次被动重读后恢复、两次不同读取轮次/120 秒仍影响最新
    尾部才 handoff；历史 L2 handoff 在干净权威读取后自动关闭。
24. customer 语音/图片单条失败时，无论同批文字是否完整，都必须在事实结算后直接
    handoff 且不得生成请求重发/改发文字的自动回复；self 媒体失败不得阻断。高意向和
    其他硬风险仍必须阻断自由回答。
25. 首屏、定向和恢复队列必须共用冷却准入：读取成功后 30 秒扫描不得再次点击；有新
    事实时后端也保留至少 2 分钟 `next_read_due_at`，只有高于已消费代次的新
    `unread_generation` 或正式 continuation token 可提前唤醒。必须固定回放“同一红点连续多轮扫描”：
    已消费代次不得反复清空冷却；并回放“读取N期间到达N+1”，证明N结算不会误消费N+1。
26. 初始帧同时有两条相同 3 秒语音：首帧只为选中的一条生成 action ID/预留号；
    完成后必须从新帧重新仲裁另一条并生成不同 action ID/预留号。不得因时长、相同菜单或相似图形合并，也不得继续使用旧帧未执行候选。
27. 点击旧 3 秒语音后微信自动滚动，旧语音上移、又到达一条新 3 秒语音并占据旧位置：
    新语音必须生成新 action ID/预留号，不得继承旧 `worker_stable_id`，并在后续仲裁中被处理。
28. 媒体动作期间到达新文字“好的”，且历史中已有相同文字：固定回放
    `文字1=10 / 语音1=11 / 图片1=12 / 文字2=13(好的)` -> 图片动作后
    `文字1 / 语音1 / 图片1 / 文字2(好的) / 文字3(好的)`。前四条必须保留 `10..13`，
    文字3必须作为唯一连续 `new_suffix` 获得 `14`；不得用内容相等、坐标或旧 anchor 过滤。
29. 自动滚动后分别覆盖三种合法证据：原生 ID 精确相等；至少三帧对被点击气泡连续且每步唯一跟踪；
    或至少两个可信邻居对齐全局滚动。三者均还必须最终候选数为 1；证据不足或候选不唯一必须 `ambiguous`，
    零正文绑定、零 Brain，只隔离当前客户，不停止其他短码。
30. 在“预留已落盘/点击前”和“`trigger_attempted` 已落盘/结果未结算”两个边界分别注入崩溃：
    重启后不得复用预留号；已尝试的动作不得再点击，必须沿原 action ID 恢复或结算 unknown。
31. 合同反例必须在任何新微信动作/入库前失败关闭：Sidecar 改写 action ID、
    `sequence_reserved` 却携带正式 `worker_stable_id`、`confirmed` 但候选数不为 1、
    声称 `confirmed` 但跟踪法少于三帧，或邻居对齐法少于两个可信邻居，统一返回 `C2_VOICE_IDENTITY_CONTRACT_INVALID`。
    如实返回 `ambiguous` 属正常安全分支，不属合同异常。
32. 静态/AST 门禁必须防止回归：禁止 `attach_inflight_worker_ids` 或等价逻辑用 anchor/签名/
    相同内容跨动作重挂正式 ID；禁止 `_prepare_voice_action_frame` 或等价准备步骤在点击前
    写入正式 identity catalog/source key；禁止一帧为多个未执行候选预创建 action/Journal，
    以及在任意 UI 动作后继续消费旧帧未执行候选。
33. 动作前帧内同时存在已编号文字、已编号媒体和尚未选中的语音/图片时，
    `pre_action_identity_sequence` 必须完整包含三类状态。动作后未选候选不得继承 ID，
    也不得被当成 `new_suffix`；它们必须从最新帧重新仲裁。
34. 反向回放只显示一条“好的”，无法证明是旧文字2还是新文字3；
    必须得到 `MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS`，不得继承 13、分配 14 或启动 Brain。
35. 初次打开无 checkpoint 会话时，完整当前序列可作为初始 `new_suffix`；
    同一画面在后端已有 checkpoint 时必须先唯一对齐，不得因本地数据缺失将整屏重新编号。
36. 在“pre 序列已落盘/动作未触发”和“动作已触发/post 帧未落盘”分别注入崩溃；
    恢复必须使用原 pre 序列、原 action ID 和原预留号，不得用重新截图伪造动作前序列。
37. 反例 A/B/C：三张连续帧中各自只有一个语音候选，但相邻帧结构无法把 A 唯一映射到 B、
    B 唯一映射到 C。即使计数为 `[1,1,1]` 也必须拒绝 confirmed；只有完整 `tracking_edges`
    从 `selected_pre_observation_id` 连到最终绑定观察才可通过。
38. prepare 选中语音 A 后、execute 点击前到达语音 B，且 B 占据 A 原位置：execute 必须依据
    `selected_action_token` 点击 A 或零点击返回 `cancelled_before_trigger`，不得重新选择底部 B，
    不得把 A 的 action ID/预留号写给 B。
39. 对菜单识别为文字、菜单识别为语音、弹窗证据不足、复制后非位图四条图片路径分别注入
    新客户文字：生产链必须在 Brain 前刷新并把新文字纳入最终序列；任何测试不得再断言
    `does_not_require_chat_refresh`。
40. 初读首条语音、图片后新增语音、continuation 和崩溃恢复必须命中同一个生产语音编排器；
    AST 门禁禁止第二套 inline prepare/execute、旧 `_reconcile_message_identities`、
    `reconcile_v16104_identity_transition` 或 anchor/内容/坐标身份回挂代码进入正式链路。
41. 正向测试必须使用真实 Win32 observation 形状：每项明确
    `source_adapter=win32_ocr`、`id/observation_id=win32_ocr:*` 且
    `native_source_message_id` 为空。普通最新尾部新语音即使只有前侧历史文字、普通新图片即使没有
    原生 ID，也必须通过 `frame_action_binding` 各执行一次并在有效回执后提交长期身份；不得为了让
    测试通过省略 `source_adapter` 或人工注入 `native-*`。
42. 同一真实 Win32 数据分别覆盖：未变化画面正常语音恰好一次 execute、未变化画面正常图片恰好
    一次复制/Vision、同秒数新语音占据旧位置零点击、相似新图片替换零点击、媒体期间新增文字由
    Worker 最新帧重新仲裁。正向和反向必须调用同一生产入口，不得只测试内部 helper。
43. 权限反例必须证明：Sidecar 返回 `worker_stable_id/source_message_key/commit_basis` 或
    “same_business_message”结论时 Worker 合同校验在任何持久消费者前拒绝；Worker 未经有效回执
    提交预留号时后端正式 ingest 拒绝；后端不得根据 OCR/坐标自行补齐身份。
44. 测试至少贯穿生产 TaskRunner、真实 SQLite ActionJournal/Ledger/Outbox、正式 Worker 消息
    构建器和后端 ingest 路由。允许在非 Windows 自动化中替换截图/OCR/物理鼠标边界，但替身只能
    返回真实形状的原始观察和动作回执，不能直接返回“身份已确认”或伪造最终业务成功。

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
- 引用文字区域被矩形探测器列为图片候选时，必须由“放大阅读”或“翻译+搜一搜”
  精确确认文字菜单并在复制前拦截；只有“翻译”或只有“搜一搜”不得单独下结论。
  原会话形成一次可查的 failed 事实；当前客户消息按 L1 进入人工接管，self 只告警，
  不得形成永久 `C2_IMAGE_FACT_PENDING`。
- 真实图片菜单必须同时出现“复制”和至少一项“编辑/用窗口打开/另存为/打开方式”
  才允许复制；只有公共项或多类特征冲突时不得点击。
- 菜单内只有“复制”、但菜单外聊天区域存在“编辑/放大阅读/翻译/搜一搜”时，
  菜单外文字必须被排除，结果为证据不足且零点击；测试必须断言所有分类和点击证据
  bounds 均完整位于同一个 `menu_panel_bounds`。
- 真实语音菜单出现“语音转文字”或“收起文字”时必须在图片复制前拦截，且零
  剪贴板读取、零 Vision。
- 在 failed Ledger 已落盘、完整消息 Outbox 尚未落盘的故障点模拟进程退出；重启后
  必须上报包含原失败图片的完整 V3 messages，后端产生对应 MessageEvent 和逐 source
  key 确认后才能清理 Ledger，禁止用空 flow gate 代替。
- 对 customer/self/unknown 三种角色分别验收失败后的状态：customer 转
  `waiting_sales_reply`；self 无更晚客户消息时转 `sales_replied_waiting_user`；unknown
  不建立图片消息。仅 `fact_settlement` 恢复时三者都不得倒推改变当前会话状态。
- 该会话转 `waiting_sales_reply` 后，Worker 必须在同一轮或下一调度周期继续处理
  其他已授权短码；销售回复后按既有状态机进入 `sales_replied_waiting_user`。

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
- 已按唯一判据确认为文字/语音菜单仍点击复制，图片证据不足或冲突仍点击菜单项，
  或明确非位图后仍持续轮询、重复 UI 动作。
- 单个图片 failed 事实未能进入 Outbox/后端确认，导致其他短码长期饿死。
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

浏览器只调用车金后端 API。车金后端负责登录会话校验、输入校验、审计日志，
再调用 OmniAuto 能力；前端不得直接读写 Product Master、KnowledgeRuntime 或数据库。

### 9.4 Brain 查询流程

客户询问“某车有没有”时，Brain 先从 Product Master 查询在售车辆，再按需读取正式
知识并交给 Guard 校验后生成答复：

- 唯一明确命中：可以回答白名单内的真实车辆信息。
- 多车或条件不完整：先追问必要条件，不擅自选车。
- 无匹配：明确说暂未查到，并可转人工；不得根据模型常识编造库存。
- 数据库不可用或结果不可信：停止自动回答车辆事实，记录证据并转人工，不把
  “查询失败”说成“没有车”。

### 9.5 安全、登录与审计

- 所有登录成功的后台账号均可维护车辆；第一期不设置普通销售只读账号或车辆角色权限。
- 敏感字段默认不进入 AI 可见投影；AI 数据边界独立于后台账号权限，不能因账号拥有
  全部后台权限而扩大模型可见字段。
- 数据库凭据、模型密钥和存储凭据只保存在服务端，不下发 Worker 或浏览器。
- 新增、编辑、上下架、批量导入、图片变更均记录操作人、时间、对象、结果和失败原因。
- 采购价、底价、车主隐私、内部备注等字段默认禁止对客回答；需要开放时必须由
  管理层明确调整 AI 对客字段白名单，不通过登录角色隐式开放。

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
| 关键词拦截 | 关键词只触发风险分类，不直接决定静默或永久 handoff。“别联系/不要再发”等明确拒收执行 hard opt-out；投诉、报警、律师、退款、赔偿、诈骗进入边界回复 + 人工接管，原候选事实承诺不得发送。 |
| 人工接管关键词 | 高意向固定通知销售并转人工，停止普通 AI 回复。底价、事故、泡水、贷款、定金、合同、地址、现在定等非高意向词先提升检索和 Guard 强度；已有权威公开证据时 AI 可按允许字段回答，流程咨询可继续澄清；最低价承诺、车况绝对保证、审批结果、合同责任、支付/退款决定等必须由人工确认。 |
| 随机发送延迟 | 配置化，默认待定；仅体验优化，不承诺规避微信风控。 |
| 风险提示检测 | 操作频繁、环境异常、添加受限等出现后暂停任务并上报。 |
| 单会话突发限频 | 配置化，默认待定。 |
| 风险暂停恢复 | 支持人工解除或到期自动解除，默认人工确认更稳。 |

### 10.1 执行顺序

```text
总开关 -> 会话/目标硬准入 -> 黑名单/明确拒收 -> 静默时段/限频 ->
最新待回复尾部完整性 -> 关键词风险分类 -> 权威证据检查 -> Brain -> Guard ->
send_reply / reply_then_handoff / handoff / retry_later / no_action
```

## 11. 模块10：人工接管与飞书通知

- 需要人工决定的事实由销售接管；能安全说明边界的场景可先执行一次 Guard 批准的
  `reply_then_handoff`，高意向和当前客户单条媒体失败直接 `handoff`。
- 当前仍处于人工接管的权威条件必须同时满足：`Conversation.status=waiting_sales_reply`
  且存在 `closed_at IS NULL` 的 `HandoffEvent`。任何模块不得只看状态字段作出接管判断。
- 未关闭 `HandoffEvent` 存在而状态漂移时，服务端必须重投影为 `waiting_sales_reply`；
  重投影不得创建新事件、不得再次通知。仅 L2 技术接管允许在权威干净读取后自动关闭，
  业务硬风险、人工暂停和明确拒收不自动关闭。
- 飞书通知完全由车金后端执行，前端和 Worker 不持有应用凭证、不查询接收人、不补发通知。
- 第一期不做短信、自动重试、人工重发、销售超时二次提醒和交互卡片按钮。

| 触发来源 | 说明 |
|---|---|
| 风控/关键词 | 高意向直接接管；投诉、金融、合同、价格承诺等按风险分类和 Guard 结果决定边界回复后接管或静默接管。 |
| 技术/证据失败 | 当前客户媒体失败直接接管；上下文、模型、RAG 等可恢复错误先有限重建/重试，耗尽后仍无法安全回复才接管。 |
| 销售主动回复 | 检测到销售手机端人工消息后直接进入 `sales_replied_waiting_user`，不再创建“等待销售回复”的 handoff。 |
| 手动操作 | 控制面或 Worker 执行台点击停止 AI/手动接管。 |

- 创建本轮 `HandoffEvent` 并把会话投影为 `waiting_sales_reply` 后，立即触发一次通知副作用；
  `ai_enabled` 只在人工明确关闭全部自动化时设为 `false`。
- 接收人固定按 `conversation.sales_id -> Sales.feishu_user_id` 路由，与 Worker 绑定无关。
- `Sales.feishu_user_id` 只保存车金当前飞书应用返回的 `open_id`；不得混存 `user_id/union_id`，
  不得增加第二个兜底接收人字段。
- 通知包含客户标识、线索短码、手机号后四位、销售、触发原因、最近消息、建议动作和时间。
- 同一 `handoff_event_id` 只允许通知一次；服务重启、重复事件、状态重算和重投影均不得重发。
- 通知成功或失败都记录尝试时间、完成时间、结果、飞书错误码和脱敏错误摘要；失败不回滚
  `waiting_sales_reply`、不恢复 AI、不要求 Worker 补发。

### 11.1 飞书通知轻量实现

| 机制 | 要求 |
|---|---|
| 应用 | 全系统只配置一个车金飞书自建应用机器人；OmniAuto 自带飞书模块保持关闭。 |
| 配置 | `app_id/app_secret` 仅存在服务端 Secret；`tenant_access_token` 由服务端获取和缓存，不落前端、Worker、日志或数据库明文。 |
| 权限与范围 | 应用开启机器人能力，具备按手机号查询用户 ID 和发送消息权限；销售必须属于应用可用范围。 |
| 接收人建立 | 新增或修改销售手机号时，服务端调用飞书通讯录接口查询当前应用下的 `open_id`，成功后写入唯一字段 `Sales.feishu_user_id`。 |
| 触发 | 同一事务创建本轮 `HandoffEvent` 并投影 `waiting_sales_reply`；提交成功后立即发送，或写入仅后端可见的内部通知 Outbox。 |
| 任务边界 | 飞书通知不是运营后台 `Task.task_type`，不得创建 `handoff_notify`，不进入任务中心。 |
| 幂等 | 以 `handoff_event_id` 为唯一幂等键；内部 Outbox 如存在，也必须对该键建立唯一约束。 |
| 记录 | 保存 `notify_attempted_at/notify_completed_at/notify_status/notify_error_code/notify_error_summary`；摘要脱敏。 |
| 失败 | 第一期不自动重试、不人工重发；失败不改变人工接管状态。 |

统一错误码至少包括：`FEISHU_CONFIG_MISSING`、`FEISHU_TOKEN_FAILED`、
`FEISHU_USER_NOT_FOUND`、`FEISHU_USER_NOT_IN_APP_SCOPE`、`FEISHU_OPEN_ID_INVALID`、
`FEISHU_SEND_FAILED`、`FEISHU_RESPONSE_INVALID`。第三方原始错误必须映射到上述稳定错误码，
仅在脱敏摘要中保留排障信息。

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
| P0后台登录 | 指定账号密码登录、Cookie 会话、刷新保持、失效/退出、全部后台接口门禁、所有账号同权限、审计脱敏、CSRF/限速和 Worker Token 双向隔离。 |
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
- 未登录遍历后台接口、伪造/过期 Cookie、停用账号、退出后浏览器后退、Worker Token
  访问后台和后台 Cookie 访问 Worker 接口：均必须被服务端阻断且留下脱敏审计。

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
| 数据安全与权限边界 | 指定账号登录成功即拥有全部后台权限，不做 RBAC；后台会话与 Worker Token 完全隔离；模型Key、数据库/存储凭据、飞书配置不下发Worker；AI只读白名单字段。 |
| Worker兼容性管理 | 记录Windows、微信、Worker版本；每次微信升级前跑核心回归；支持暂停Worker和人工降级。 |
| 异常恢复任务 | 定时扫描 `stale running`、未确认 Outbox 和车辆/知识迁移异常并按安全规则处理；`unknown_send_result` 是已确认终态，只用于防重复与后续气泡自动对账，不生成消息人工待办。 |

## 15. C0—C4统一耗时观测（0.9.8）

本节是 `0.9.8` 的唯一开发口径，目的仅是把现有分散耗时统一成可查询、可比较的报告，
不改变 C0—C4 业务状态机、任务领取、UI 锁、微信动作、授权、重试、暂停接单、
Handoff 或防重复发送规则。`0.9.7` 已发布候选和机器合同不回写、不覆盖；实现完成后再按
灰度版本规则同步 `0.9.8` 客户端、后端、生成物、manifest 和候选包。

### 15.1 范围与边界

统一观测必须覆盖：

| 业务段 | 标准阶段 |
|---|---|
| C0 线索 | `c0.lead_received`、`c0.lead_assigned` |
| C1 加好友 | `c1.add_friend_queued`、`c1.add_friend_execute`、`c1.friend_acceptance_wait` |
| C2 读取 | `c2.scan`、`c2.read_queued`、`c2.target_locate`、`c2.message_read`、`c2.voice_transcription`、`c2.image_vision`、`c2.message_ingest` |
| C3 回复 | `c3.brain_queued`、`c3.brain_generate`、`c3.pre_send_refresh`、`c3.reply_queued`、`c3.reply_send_confirm` |
| C4 召回 | `c4.recall_wait`、`c4.recall_precheck`、`c4.brain_generate`、`c4.reply_queued`、`c4.reply_send_confirm` |
| 横向人工接管分支 | `handoff.event_create`、`handoff.feishu_notify`、`handoff.wait_sales`、`handoff.close` |

C4 只表示自动召回。人工接管不是 C4；它是在 C2、C3 或 C4 触发后沿用当前
`process_run_id` 的横向分支。不是每次业务处理都会经过全部 C0—C4：初次线索从 C0 开始，
客户新消息通常从 C2 开始，召回周期从 C4 开始。

本轮禁止顺带增加新的业务状态、`Task.task_type`、微信 UI 操作、OCR、截图、Brain/Vision
调用、业务重试或性能优化。统一耗时观测不是业务门禁，不能参与是否读取、是否发送、是否
Handoff 或是否释放 UI 锁的判断。

### 15.2 关联ID与时间语义

| 字段 | 唯一含义 |
|---|---|
| `conversation_id` | 同一客户会话的长期业务关联。 |
| `process_run_id` | 一次业务处理的稳定ID。初次线索处理、一次客户消息处理、一次召回周期分别形成独立 run；由服务端在该次处理起点生成，跨后端、Worker、Sidecar 和 Brain 传递，重试或进程重启不得更换。 |
| `stage_run_id` | 一个标准阶段的一次执行尝试；每次重试生成新值。 |
| `parent_stage_run_id` | 可选；只在 Sidecar、Brain 等子阶段需要归属到上级阶段时填写。没有父阶段时为 `null`。 |
| `trace_id` | 单次 HTTP、进程调用或技术尝试的排障ID；允许随请求和重试变化，不得冒充 `process_run_id`。 |

每条阶段记录固定包含：

```text
process_run_id, stage_run_id, parent_stage_run_id, conversation_id,
stage_name, component, attempt, queued_at, started_at, ended_at,
queue_duration_ms, execution_duration_ms, status, error_code, trace_id
```

`status` 只允许 `running / succeeded / failed / cancelled / abandoned`。`attempt` 从 1 开始，
同一阶段重试只能新增 `stage_run_id`，不得覆盖前一次结果。排队耗时和实际执行耗时必须分开：
`queue_duration_ms` 只表示从可执行到真正开始，`execution_duration_ms` 只表示实际执行。
`friend_acceptance_wait` 和 `recall_wait` 属于正常业务等待，报告必须单列，不得计入系统执行性能。

单阶段耗时由执行该阶段的同一进程使用单调时钟计算；UTC 墙上时间只用于排序和展示。
禁止直接用两台电脑的系统时间相减计算耗时。异常退出后无法可靠恢复的开放阶段写
`abandoned`，耗时未知则为 `null`，不得用重启时间补算或编造。

### 15.3 采集、存储与接口

- 后端在现有任务/状态事务提交成功的边界记录服务端阶段；不得为了计时改变原事务提交顺序。
- Worker 在现有生产流程入口和终态记录；不得另建第二套业务编排器。
- Sidecar 不直接访问后端。Sidecar 使用现有结果合同返回标准阶段耗时，Worker 校验后批量上报。
- Brain 已有 `stage_timings/stage_timeline` 映射为上述标准阶段；不得再次执行 Brain 获取耗时。
- 后端使用独立 `process_stage_runs` 表保存观测记录，不写入 `Task`、MessageEvent、业务 Outbox、
  ActionJournal、Ledger 或 HandoffEvent，也不得由观测记录反向修正这些业务事实。
- 观测写入使用数据库 SAVEPOINT 时，SAVEPOINT 的提交或回滚绝不是业务事务提交或回滚，
  不得执行、消费或清除 Handoff 飞书通知等任何业务 `after_commit` 副作用。只有最外层业务
  事务提交成功后才允许派发通知；已提交但尚未尝试的通知由持久化恢复循环继续结算。
- HTTP 请求成功但业务结果为 `handoff_created` 时，`c2.message_ingest` 必须记录业务失败终态
  和真实错误码，不能仅因 HTTP 200 或事实安全落库而伪报 `succeeded`。
- `stage_run_id` 是幂等键；重复开始/结束事件只能补全同一记录，不能生成重复耗时。
- Worker 离线时只写独立的 telemetry 缓冲；该缓冲不得复用业务 Outbox，不得持有或延长微信
  UI 锁。上传失败只记录告警并留待后续批量上报。

正式接口固定为：

| 接口编号 | 方法 | 路径 | 约束 |
|---|---|---|---|
| `API-OBS-01` | POST | `/api/workers/{worker_id}/observability/stage-events` | Worker Token、worker 绑定和事件 schema 校验；批量幂等写入。 |
| `API-OBS-02` | GET | `/api/observability/process-runs/{process_run_id}` | 仅运营后台有效会话可查；返回标准阶段、排队/执行耗时、重试和终态。 |

观测失败必须旁路处理：接口超时、后端拒绝、SQLite 暂不可写、字段缺失或 UI 展示失败，均不得
改变业务状态、触发 Handoff、暂停接单、取消任务、重新操作微信或阻止回复发送。业务线程不得
等待观测接口重试；UI 只读取聚合结果，展示失败不得影响 Worker 运行。

### 15.4 旧计时迁移与删除规则

现有 `flow_timing`、Sidecar `timing_ms`、Brain `stage_timings/stage_timeline`、发送确认 OCR
耗时等不得一次性删除：

1. 先建立唯一映射表，把已有计时映射到 15.1 的标准阶段；禁止同一阶段在不同模块自行改名。
2. 新旧输出在定向测试中并行比对；同一执行的阶段次数、终态和耗时误差满足测试口径后，
   才允许删除旧的重复字段、重复日志和重复上报。
3. 被统一合同直接消费的底层单调计时保留；删除的是重复的输出和第二套汇总，不是先删计时再重写。
4. 仅用于 OCR、窗口定位等故障排查的细粒度诊断耗时可以保留在组件日志中，但不得混入
   C0—C4 业务耗时汇总，也不得再形成另一套运营报告。
5. 迁移结束后生产路径只允许一套标准阶段汇总；永久双写、同义字段和两份运营耗时报告均不允许。

### 15.5 验收与回滚

- 使用同一真实 `process_run_id` 验证后端、Worker、Sidecar 和 Brain 阶段可以按顺序汇总；
  C4 召回与 Handoff 横向分支不得互相混名。
- 验证排队与执行分离、重试不覆盖、重复事件幂等、进程重启产生 `abandoned` 而不编造耗时。
- 强制让观测接口超时、拒绝和本地缓冲失败，原 C0—C4 状态、微信动作次数、UI 锁持有范围、
  Brain/Vision 调用次数、Handoff 和发送结果必须与未启用观测时完全一致。
- 对比旧计时与统一报告后再清理重复输出；没有比对证据不得删除旧字段。
- 功能必须具有独立开关。关闭时只停止新增观测事件，不删除历史记录、不改变任何业务流程；
  出现问题时可关闭观测并回到 `0.9.7` 业务路径，不需要回滚业务数据。

### 15.7 灰度 0.9.10 读取与观测收口

- 已确认发送成功的 AI 文字在后续 `recent_ai_sent` 权威读取中，只允许使用本地回执中
  `reply_action_id + reply_text_hash` 唯一匹配的原文辅助当前画面 ROI OCR。正文仅用于把被结构探测器
  错分为图片的右侧 self 气泡恢复为文字类型；正式消息身份仍只能由统一序列对齐决定，禁止按正文重挂身份。
- 上述类型恢复必须贯穿 `open-chat` 首帧、普通 `messages`、身份重试、语音 prepare/execute 全部画面以及
  图片处理后的最终刷新。无可信本地回执、头像未确认 self、局部文字与已确认回复重合不足时保持原类型并
  失败关闭，不得把任意含字图片转换成聊天文字。
- 暂停接单发生在首屏扫描返回之后时，Worker 必须投影 `scan_cancelled` 终态并清空后台扫描展示；不得让
  已结束扫描继续显示为运行中。该 UI 投影不得改变暂停排空、UI 锁或业务结算语义。
- 时间窗取证继续作为旁路观测能力，独立脱敏导出结构化日志、阶段耗时和本地事实快照；不得打包原始数据库、
  Token、密钥、Cookie、客户原图或完整聊天截图，导出失败不得影响业务主链。

## 16. 下一灰度候选 P0 性能优化（目标 `0.9.11`）

本节是下一灰度候选的唯一 P0 性能优化口径。优化只允许减少安全步骤之间的空等、同一物理
画面内部的重复计算，以及在证据完全等价时重复执行的全窗口 OCR；不得改变 C0—C4 业务
状态机、任务归属、授权、UI 锁、ActionJournal、Outbox、Handoff、Brain、Guard、发送前
复读和发送结果确认语义。`0.9.10` 包、提交和合同保持不可变；形成实现候选时按版本规则把
客户端、后端、OmniAuto、合同 revision、生成 Schema、manifest、PRD、技术方案、全流程图
和版本记录统一升级为 `0.9.11`，不得覆盖 `0.9.10`。

耗时受机器性能、微信窗口内容、消息数量、回复长度、OCR 路径和是否触发回退影响，禁止把某个
固定秒数写成超时、成功条件或业务门禁。验收只在相同环境、相同场景分组下比较优化前后多轮
样本的中位数、P95、OCR 次数和重复步骤；正常长耗时不得被判为故障或强行中断。功能安全优先
于耗时：任何优化使错误会话读取、错误发送、重复发送、漏读新消息、媒体重复操作或发送结果
误确认增加，即使平均耗时下降也必须关闭优化并回到原完整路径。

### 16.1 不得改变的主流程

```text
后端授权
-> 获取并持有当前单会话逻辑 UI 锁
-> 使用当前物理画面定位会话
-> 确认 private + 精确 remark_code
-> 读取并入库消息
-> 保持同一单会话逻辑事务所有权等待 Brain；不得释放 UI 锁处理其他客户
-> Brain 终态后重新取得当前物理画面执行 pre_send_refresh
-> 领取当前 reply_action 和发送许可
-> 新画面建立发送基线 S0
-> 输入回复
-> 新画面执行点击前复核 S1
-> 触发发送
-> 新画面确认发送结果 S2
-> 持久化并上报 sent_ack
-> 事实可靠终结后释放 UI 锁
```

以下定义是硬约束：

1. `pre_send_refresh` 必须发生在 Brain 终态之后并重新截图；禁止复用 Brain 前的读取帧、标题
   结论或消息序列冒充发送前复读。
2. S0、S1、S2 是三个不同时间点的物理事实。不得跨时间复用截图或 OCR；S1 必须能发现输入
   期间到达的新客户消息，S2 必须确认本次物理发送结果。
3. “复用 OCR”只表示同一张不可变截图的多个纯计算校验共享一次 OCR 结果。截图一旦不同、
   微信 UI 动作改变了相关业务表面、HWND/进程、客户区尺寸/DPI、列表或消息区域摘要变化，
   旧 OCR 立即失效。纯窗口屏幕位置变化不改变截图内容和客户区坐标，不单独使 OCR 失效。
4. 快速路径证据不足、低置信、字段矛盾或验证异常时，只能在发生任何新 UI 副作用前回退原
   完整路径；不得猜测、不得把失败当成功，也不得因优化失败创建 Handoff。
5. 优化不得新增第二套业务编排器、第二把 UI 锁、并发 Sidecar 操作或新的消息/发送重试。

### 16.2 P0-1：任务安全唤醒，不抢占当前流程

现有 `CHEJIN_TASK_POLL_INTERVAL=4s` 保留。现场一次 `add_friend` 从创建到 claim 约 25 秒，
该区间可能包含正在执行的 C2 扫描、UI 锁、事务屏障和正常轮询，禁止把 25 秒全部定性为
轮询浪费，也禁止通过缩短到高频忙轮询、并发 pull 或抢占当前 UI Flow 来追求数字。

Worker 只在下列本地安全边界触发现有任务循环立即再检查一次任务：

- 当前 UI Flow 已达到可靠终态并释放 UI 锁；
- Outbox、Ledger、ActionJournal、物理 Journal 和 sent_ack 事务屏障由未结算变为已结算；
- 后端已确认 `run_status=running`；
- Worker 从暂时不可领取恢复为可领取，且当前无 `current_task / inflight_flow / UI lock`。

实现必须使用同一个 TaskRunner 循环的可合并唤醒事件；连续多次 `set` 只能合并为一次检查，
不得启动第二个拉取线程。`pull -> claim` 继续使用现有后端原子 claim、销售/Worker 绑定、lease
和 fencing token；暂停接单、紧急停止、已有 inflight flow、UI 锁占用或事务屏障未清时，唤醒
只能被记录，不能领取或执行任务。新任务在 Worker 完全空闲但尚未收到任何本地唤醒来源时，
仍由原 4 秒轮询发现，不新增 WebSocket、长轮询或服务端推送。

耗时报告只统计“任务已可领取且 Worker 空闲、无锁、无未结算事实”之后的纯调度等待。正在
执行扫描或单会话 Flow 的剩余耗时必须单列，不能伪装成排队浪费，也不能为达标而中断。

### 16.3 P0-2：会话定位的等价证据复用

#### 首屏扫描结果辅助 visible 定位

`sessions` 首屏扫描完成后可以保留一次仅供当前 Worker 使用的候选证据：同一微信 HWND/进程、
客户区尺寸、DPI、会话列表区域像素摘要、截图摘要、OCR items、短码候选、候选行边框、`scan_id`、
`sidecar_run_id` 和本机单调时间。进入 visible 定位时必须重新截取当前微信窗口，并先比较
HWND/进程、客户区尺寸、DPI 和会话列表区域像素摘要：

- 全部完全一致：可以复用该扫描帧的 OCR 和候选行作为点击候选，不再做一次列表全窗口 OCR；
- 任一不同、缺失或比较异常：候选证据立即作废，在任何点击前走原实时首屏完整 OCR；
- 无论是否复用，点击后都必须使用新画面重新确认 `private + 精确 remark_code`；误点仍执行
  6.0.4.1 步骤 11A 的一次有限恢复，绝不能读取误点会话。

仅 TTL 新鲜不能证明列表没有重排，因此禁止只按“距扫描不足 N 秒”直接使用旧坐标。

#### 当前会话快速确认

目标会话已经打开时，允许先对新截图的聊天标题 ROI 执行快速确认。只有同时满足以下条件才
可以跳过会话列表 OCR：

- 后端 read authorization 和 revision 当前有效；
- 微信 HWND、窗口几何、登录态和阻塞弹窗检查通过；
- 标题 ROI 唯一识别出完整目标 `remark_code`，不得使用姓名、消息正文或历史结果补全；
- OmniAuto 在该新帧明确返回 `conversation_type=private`、`allowed=true`，且没有群成员数、
  多短码、unknown、低置信或逻辑矛盾。

任何条件不满足都在零点击、零消息操作时回退现有完整定位路径。Worker 只消费 OmniAuto 的
准入结果，不重判标题、短码或会话类型。

定位后消息读取只能复用同一次 Sidecar 调用中、同一不可变帧已经产生的消息区域 OCR。好友
激活确认、后端往返、媒体动作或任何界面变化发生后必须重新读取，不得为节省 OCR 使用旧帧。

本项验收按 `visible 点击定位 / current 已打开 / 快速路径回退` 分组，分别比较定位阶段的
中位数、P95、全窗口 OCR 次数和 Sidecar 调用次数；不得把不同路径混成一个平均数。

### 16.4 P0-3：发送前复读只做同一新帧内部复用

Brain 终态后必须重新截图建立 `pre_send_refresh_frame`。同一张新截图中的窗口/标题检查、
消息视口读取、顺序对齐和输入区状态可以共享截图和同一次 OCR 结果；优先按标题 ROI、完整消息
视口 ROI 和输入区 ROI 识别，不要求每个校验器分别重新跑全窗口 OCR。

只有同时满足以下条件才接受 ROI 结果：标题精确确认 `private + remark_code`；消息视口边界
完整且序列对齐成功；OCR 未截断、未低置信、未出现 unknown；后端授权仍有效。任一不足必须
对同一张新截图补做原完整 OCR。完整 OCR 仍无法排除最新尾部有新文字、语音或图片时，
必须按 6.0.4.4 使用一张最新不可变帧执行唯一一次完整重识别，同时禁止旧回复发送。
重识别成功才继续收敛；失败必须返回目标消失、多候选、顺序、角色、正文、布局或再次变化的具体错误并按表中终态结算。
本阶段不得创建 `recoverable_hold`、多次被动重读或按固定时间等待“自己变清楚”。

`pre_send_refresh_frame` 不能直接充当 S0：两个阶段之间存在任务领取、claim-send、Sidecar
启动或任何可观察时间间隔时，S0 必须重新截图。不得用“UI 锁仍在”推断客户不可能发新消息。

本项按普通文字、长文字、包含历史媒体、发生新客户消息和 ROI 回退完整 OCR 分组比较；优化
目标是减少同一新帧的重复 OCR 与重复 Sidecar 准备，不是限制该阶段最多运行多少秒。

### 16.5 P0-4：发送三时点不合并，只复用各帧内部计算

发送过程必须保留：

| 时点 | 必须重新取得的事实 | 禁止复用 |
|---|---|---|
| S0 发送基线 | 当前标题、private、目标短码、消息视口基线、输入区状态 | pre_send_refresh 或更早帧 |
| S1 点击前复核 | 输入后的当前标题、消息视口、是否出现新客户消息、待发送输入状态 | S0 的截图/OCR |
| S2 发送结果 | 当前标题、发送后新增 self 气泡、输入框状态、正文归属和结果证据 | S0/S1 的截图/OCR |

每个时点只允许进行一次主截图和一次主 OCR 解析，所得不可变 OCR items 可被该时点的目标、
弹窗、消息、角色、输入区和正文归属校验器共同消费。优先对标题、完整消息视口和输入区使用
ROI；检测到窗口异常、遮挡、结构候选、OCR 缺失、正文匹配不足或任何冲突时，对该时点的同一
截图补做完整/增强 OCR。增强 OCR 仍不明确时保持 `SEND_RESULT_UNKNOWN`，不得降低阈值、猜测
成功或自动补发。

优化不得减少 S0/S1/S2 的物理帧数量，不得改变 ActionJournal 在物理触发前落盘的顺序，不得
删除输入期间新消息检查、发送后新增尾部气泡检查、正文归属、输入框状态或 sent_ack。Sidecar
外围启动、序列化和回传只能做无副作用的代码瘦身；不得常驻复用可能携带上次会话状态的可变
Sidecar 上下文。

发送耗时按回复正文长度区间、是否触发增强 OCR、是否走 ROI 回退、是否出现微信渲染等待分组；
只比较相同分组优化前后的中位数、P95、OCR 调用次数和 OCR 累计耗时，禁止用单一固定秒数约束
所有发送，也禁止父阶段与子阶段重复相加制造优化结果。

### 16.6 开关、观测、验收与回滚

四项优化必须独立开关，固定命名：

```text
CHEJIN_TASK_SAFE_WAKE_ENABLED
CHEJIN_C2_LOCATE_FRAME_REUSE_ENABLED
CHEJIN_C3_PRE_SEND_ROI_REUSE_ENABLED
CHEJIN_C3_SEND_FRAME_LOCAL_REUSE_ENABLED
```

开发和故障回滚时可分别关闭；关闭后必须完全执行 `0.9.10` 原完整路径，不迁移或删除业务数据。
开关不能改变机器合同校验结果。实现候选的合同 revision 仍按统一版本规则升级为 `0.9.11`；
若 Sidecar 结果新增正式机器字段，合同 JSON、生成 Schema、Worker 和后端必须同一次同步，禁止
先兼容双字段或由 Worker 猜默认值。

每次优化尝试至少记录以下字段到现有诊断与 15 章耗时旁路；这些字段不得进入业务决策：

```text
fast_path_attempted, fast_path_used, fallback_reason,
frame_digest_equal, ocr_call_count, ocr_total_duration_ms
```

排队报告必须扣除已有 UI Flow、UI 锁和事务屏障占用，`c3.reply_queued` 与
`pre_send_refresh` 重叠时不得重复累计。

开发阶段只跑受影响路径，形成候选前必须完成以下反向验收：

1. 任务唤醒重复触发、暂停接单、紧急停止、已有 inflight、锁占用、未结算 Outbox/Journal、
   两个 Worker 并发 claim；均不得重复领取、错领或抢占当前流程。
2. 相邻步骤只有在业务界面尚未发生变化时才可复用既有帧；既有 Windows 事件或下一张本来就
   需要的业务帧一旦发现列表重排、客户区尺寸、DPI、HWND 或业务表面变化，必须废弃旧快照并按
   新帧重新识别。禁止为检测这些变化新增点击前截图、OCR、几何查询或实时坐标换算；点击瞬间
   发生列表重排仍由点击后的标题复核拦截并最多重新定位一次。
3. 当前标题正确、错误、空、低置信、群聊、多短码和 unknown 全覆盖；只有明确 private + 精确
   短码进入快速路径。
4. Brain 等待期间客户分别新增文字、语音、图片，以及“语音期间新增文字”、“图片期间新增语音”、
   “文字+语音+图片连续到达”；`pre_send_refresh` 必须从新截图发现并阻止旧回复，以最新完整顺序入库并且只让
   当前有效 batch 生成可发送回复。测试不得把 Brain 前帧注入成成功结果。
5. 新消息导致当前会话气泡上移、滚动、新旧消息正文相同、语音时长相同、图片相似，以及侧栏/其他会话、工具栏/输入框光标变化；
   无关区域变化不得取消动作。相关消息区变化必须取消旧候选、废弃原预留号，并在最新帧对完整消息序列仅重新投影一次。
6. 文字、语音、图片分别覆盖“首次发现 -> 相关区域变化 -> 一次最新帧完整重识别成功”。
   必须断言旧回复 `superseded`、新事实按顺序入库、新 batch/Brain/回复建立、HandoffEvent 为 0、每个媒体物理动作至多一次。
   反向用例必须分别证明目标消失、多候选、序列无法对齐、角色不明、正文不可读、布局无效和再次变化均返回指定错误，不进入通用 hold，不重复识别或点击。
7. 使用生产解析器对同一消息序列构造 GIF/动画帧、滚动条、悬停、语音播放动画/进度、红点、选中外框和光标变化；
   `message_viewport_change_digest` 必须保持一致。分别新增相同正文、相同时长语音和相似图片时，摘要必须因数量/顺序/结构变化而不同。测试必须断言计算材料中不存在原始整视口 RGB 哈希。
8. `C2_PRE_SEND_LAYOUT_INVALID` 必须穿过生产结算链验证：旧回复不可发送，错误码、版本、frame_id、原始/增强截图、OCR 与布局证据完整持久化，当前任务/Flow 进入 `technical_failed`，UI 锁释放，Worker 进入 `faulted` 且 `can_pull_tasks=false`，后台不得显示在线空闲。
   必须断言 HandoffEvent 和飞书通知均为 0，且没有自动移窗、重标定、重试、人工解锁、清数据或恢复旧 Flow 的调用。
9. system 行分别验证：角色未确认使用 `C2_PRE_SEND_MESSAGE_ROLE_UNCONFIRMED`；已确认 system 但正文不可读使用 `C2_PRE_SEND_SYSTEM_CONTENT_UNREADABLE`；正文可读但无法归类使用 `C2_PRE_SEND_SYSTEM_CLASSIFICATION_UNRESOLVED`；序列无法对齐使用统一序列错误。禁止一个模糊 system 错误覆盖全部分支。
10. 客户分别在 S0 前、S0 与 S1 之间、发送触发后到 S2 前新增消息；各时点仍必须独立取帧并按
   原规则处理，测试必须断言 S0/S1/S2 不能共用帧 ID 或截图摘要。
11. 发送成功、发送失败、正文 OCR 格式差异、长文字结构候选和结果 ambiguous/unknown；成功只
   确认一次，unknown 不补发，ActionJournal 和 sent_ack 终态不变。
12. 上述核心组合测试必须调用生产 `TaskRunner` 入口、真实 SQLite/Journal/Outbox、正式 HTTP 路由和后端数据库服务；
   禁止 mock `_read_one_wechat_target`、语音/图片编排函数、messages/ingest、batch 查询或 fail_task 来伪造业务终态。只允许在
   Windows 截图/OCR/鼠标边界用可控替身，且替身必须返回原始帧/OCR items/动作回执，不得直接返回“读取成功”。
13. 合法硬 handoff 后迟到文字/语音/图片只入库不自动重启 Brain；本节一次重识别成功时必须继续入库并以最新尾部重建 Brain。
    两类状态必须同时作反向验收，禁止用“迟到事实一律恢复 AI”的全局补丁。
14. 四个开关逐一关闭及全部关闭；业务结果、UI 动作顺序、错误码、Handoff、Brain 次数和
   ActionJournal/Outbox/sent_ack 必须与原完整路径一致。

Windows 固定环境至少执行 10 轮成功样本，按场景分组报告中位数和 P95，同时比较错误会话读取
数、错误发送数、重复发送数、漏读新消息数、媒体重复操作数和 `SEND_RESULT_UNKNOWN` 数。
样本量不足、场景不同或走了不同回退路径时不得声称提升百分比。安全指标任一劣化即拒绝候选，
不得用平均耗时改善抵消。P0 不包含语音编排、加好友 UI 执行步骤、Brain/Guard、首屏扫描算法
本身或 C4 流程优化；这些继续保持现状并另行评估。

### 16.7 本轮与后续优化版本边界

`0.9.11` 只允许实现 16.2—16.5 定义的四项 P0：任务安全唤醒、会话定位等价证据复用、
发送前新帧内部复用、S0/S1/S2 各帧内部计算复用。客户端工程师不得在同一候选中顺带修改
语音/图片编排、加好友 UI 主链、Brain/Guard、首屏扫描算法、C4 或其他代码瘦身；P0 测试也
不得通过改变这些路径来获得耗时改善。

P1 和 P2 固定进入正式上线后的 `1.1.x` 独立优化系列，不再占用 `0.9.x` 灰度修复序列，也不得
回写 `1.0.x` 正式稳定系列。每个实现候选使用开发时下一个未占用的精确 `1.1.n`，禁止覆盖已经
形成的候选或把多项未经独立验证的优化捆绑发布。

后续 P1 范围固定为：

1. 语音转写性能：只优化 prepare/execute 中等价帧的重复 OCR、右键菜单 ROI 和已满足条件后的
   多余等待；不得减少身份跟踪帧、改变正式 action ID、Journal 终态或歧义失败关闭。
2. 加好友 UI 执行性能：只优化搜索框、资料页、邀请表单和确认后页面的 ROI OCR，以及“状态已
   明确出现即可继续”的有上限等待；不得删除字段复核、唯一联系人确认、最终确认或页面收尾。
3. Brain 修复链：提高首次输出满足现有合同和 Guard 的概率，减少进入修复链的比例；不得删除
   语义复核、Guard、质量修复或修复结果验证，也不得释放 Brain 期间的当前会话逻辑 UI 锁。

后续 P2 范围固定为：

1. 首屏扫描成本：在不改变未读代次、短码/private 准入和扫描只读语义的前提下评估 ROI、稳定
   区域复用和 OCR 调用瘦身；单独的红点仍不能制造新消息事实。
2. 耗时观测清理：完成 15.4 的新旧计时并行比对后，只删除重复输出、重复汇总和错误的父子
   阶段相加；底层单调计时和故障诊断耗时继续保留。
3. 无业务语义的代码瘦身：合并重复截图/OCR辅助函数、重复序列化和重复报告构建，但不得新建
   第二套 Sidecar 生命周期、跨会话可变缓存或改变任何 UI 动作顺序。
4. C4 性能只在取得独立真实召回链路样本后再评估；不得用 C2/C3 样本推算或直接套用优化。

P1/P2 仍不得写死统一秒数。每项开始前必须基于对应真实阶段记录确定场景分组、当前重复步骤、
安全不变量、独立开关和回滚路径，再更新本文并开发；不得以“下一版本优化”作为本轮提前改代码
或扩大测试范围的理由。

### 16.8 0.9.13 灰度集成边界

`0.9.11` 继续固定为四项 P0 性能整改提交 `3ea702b`，不得覆盖或扩大其内容。`0.9.12` 在该候选
之上恢复经审计但此前漏合入灰度主线的运营后台 UI 提交 `542f7bc`；Windows 发布门禁随后证明
Windows 单调时钟的分辨率不足以单独保证连续物理截图 ID 唯一，因此该标签未生成或交付 ZIP。
`0.9.13` 只补充每次物理截图 ID 的独立随机熵，并同步不可变发布版本、机器合同、生成 Schema、
来源清单、构建清单和包名；本次不修改 C0—C4 业务状态机，也不提前实现 16.7 所列 P1/P2 优化。

因此 `0.9.13` 的客户端版本、后端合同 revision、OmniAuto 生成 Schema、构建 manifest 和 ZIP
文件名必须完全一致。P1/P2 顺延到下一个未使用的灰度版本，若没有插入其他修复候选，预计为
`0.9.14`。任何代码、合同、配置或打包内容在 `0.9.13` 产物生成后发生变化，必须再次整体升版，
不得覆盖既有产物。

### 16.9 0.9.14 可靠消息类型单调仲裁

`0.9.14` 只修复结构图片观察覆盖已确认文字/语音类型的缺陷，不改变 C2 授权、媒体动作、
Vision、Brain、Handoff 或发送流程。类型仲裁必须单调：一旦当前权威画面已用可靠同排角色与消息结构
确认 `text` 或 `voice`，弱结构媒体几何证据不得将其重新归类为 `image`，最终合并也不得删除该可靠事实。

真实图片内部的 OCR 文字仍不能单独建立文字类型；它必须先通过同排头像/父语音等可靠聊天行证据才能进入
上述保护。反向测试必须同时保证真实图片中的聊天样式文字仍由 Vision 处理。`0.9.14` 的 Worker、后端
`contract_revision`、OmniAuto 生成 Schema、来源提交、manifest 和 ZIP 文件名必须一致；旧 `0.9.13` 请求必须明确
revision mismatch，不得静默兼容或覆盖旧包。

### 16.10 0.9.15 车金正式知识种子

`0.9.15` 完整继承 `0.9.14` 的可靠消息类型单调仲裁，只新增经审查的车金正式知识种子导入能力。
八条知识不得随部署自动写入或自动激活，必须由后端运维依次执行 dry-run、幂等导入和显式 activate；
导入但未激活的条目不得进入 OmniAuto Knowledge Runtime。两条 `always_include=true` 规则进入每次 Brain
知识上下文，其余六条只在问题匹配时进入；未匹配时不得强行使用，也不得回退到其他租户的历史知识。

静态 registry、schema 和 resolver 属于车金业务 overlay，只定义分类合同，不包含正式业务条目；八条条目仅
由后端导入器写入 PostgreSQL。导入、重复导入、冲突失败、激活、回滚和租户隔离必须自动化验证，且不得
改变 C2 授权、媒体编排、消息身份、Brain 调用次数、Handoff 或 C3 发送流程。`0.9.15` 的 Worker、后端
合同 revision、OmniAuto 生成 Schema、来源清单、manifest 和 ZIP 文件名必须一致；旧 `0.9.14` 请求必须
明确 revision mismatch，既有 `gray-v0.9.14` 标签和 ZIP 不得覆盖。

### 16.11 0.9.35 启动一次布局标定与坐标地图兼容性整改

#### 16.11.1 范围与不变量

本项只替换“微信 UI 区域和点击坐标怎样适配当前 Windows”的物理定位层。C0—C4
业务步骤、重试规则、失败处理、会话授权、消息身份、媒体编排、Brain、
Guard、Handoff、S0/S1/S2、sent_ack 及各自终态必须与 `0.9.20` 一致。禁止借兼容性
整改重写搜索、弹窗、输入、发送、会话确认或媒体业务流程。
已批准的 C1 未变化画面证据复用必须保留：通用情况只合并两次使用之间没有点击、输入、页面切换或
其他 UI 变化的重复截图/OCR。唯一已证明的表单内例外继承 `0.9.20`：同一添加朋友邀请表单中，
申请语和备注两个输入框可共用填写前同一帧已确认的表单几何位置；填写完两个字段后必须立即重新截图/OCR，
核对两个字段内容和当前确认按钮，再允许提交。该例外只复用坐标几何，不得把填写前的 OCR 内容当成填写后证据；
若输入引起窗口、表单尺寸、页面、滚动或控件布局变化，则例外立即失效。正常 C1 画面节点仍为主界面、加号菜单、搜索弹窗、
手机号输入后、搜索结果、邀请表单、表单填写后和提交结果共八类。

`0.9.35` 的唯一方法是：启动时用当前微信客户区真实截图建立一份
`startup_layout_calibration`，将原 `0.9.20` 参考坐标映射到本次运行的实际导航栏、侧栏、
会话区和输入区。OmniAuto 是该标定和坐标的唯一决策者；Worker 只调用、校验并串行执行，
后端不接收、不推断也不保存 UI 坐标决策。不允许 Worker 和各业务模块再各自解释布局。

#### 16.11.2 启动窗口处理与一次标定

1. 客户端启动后只处理一次微信主窗口：确认唯一可见主 HWND、已登录、非最小化、
   非离屏且屏幕工作区可容纳，然后按当前 Windows DPI 和可用工作区选择唯一默认外框档位，
   移到安全位置并记录本次运行几何。禁止继续使用 `0.9.22` “所有机器统一 `980×860`”的默认值。

   | Windows 显示缩放 | 默认微信外框目标 | 口径 |
   |---|---:|---|
   | 100% | `800×852` | 沿用 `0.9.20` 现场可用小窗口基准 |
   | 125% | `1000×1065` | `800×852 × 1.25` |
   | 150% | `1200×1278` | `800×852 × 1.50` |
   | 其他 | `round(800×852 × dpi_scale)` | 按实际 DPI 比例计算 |

   外框档位必须以当前微信 HWND 所在显示器为准：用 `MonitorFromWindow/GetMonitorInfo`
   取该屏工作区，用 `GetDpiForWindow` 取当前 DPI，不得偷用主屏参数或仅凭物理分辨率猜缩放。
   Sidecar 必须在 per-monitor DPI aware 语义下执行并在移动后重读真实结果。
   安全边距 `margin=round(12 × dpi_scale)`，可用宽高分别为当前屏工作区宽高减去两侧边距。
   默认外框放不下时禁止独立裁剪宽或高，必须使用
   `fit_scale=min(1, available_width/target_width, available_height/target_height)` 对宽高等比例缩小，
   最终外框为 `floor(target_width×fit_scale) × floor(target_height×fit_scale)`。默认位置为
   `work_area.left+margin, work_area.top+margin`。移动后实测客户区仍不得小于 `700×720`；
   低于下限时该机器不进入接单，不得继续压缩。
   Windows 实际外框/客户区结果和截图尺寸才是权威事实，不得把请求值伪报为已达到。
   Sidecar 必须在任何 HWND、截图和坐标 API 之前尝试进入 per-monitor DPI aware 上下文，
   并查询当前实际生效的 DPI awareness。`SetProcessDpiAwareness*` 的返回值不得被静默忽略；
   如果设置返回“已由 manifest/早期调用设置”，只有查询证明当前已是 per-monitor aware 才可继续。
   无法查询或有效上下文不符时，使用已有 `WECHAT_UI_STARTUP_CALIBRATION_FAILED`
   并记录 `reason=dpi_awareness_unverified`，在启动标定前停止；不新增业务状态或 Handoff。
2. 移动或必要的最小调整完成后，必须只截取该 HWND 的可见客户区，不得用桌面全屏、
   `PrintWindow`、离屏图像或其他窗口画面建立可执行标定。该帧必须与既有启动主界面/登录状态观察合并，
   不得在无任何 UI 变化时为标定另外重拍一张。若该帧紧接着被 C1 消费且中间无 UI 变化，
   它同时就是正常 C1 的“主界面”帧。
3. 同一张不可变客户区截图同时产生两类结果：原始像素图用于竖线、横线、颜色与区域
   结构；同尺寸增强 OCR 图只用于“搜索”、标题等文字锚点。增强图不得改变尺寸或
   原点；若有裁剪或缩放，必须用明确变换映射回原图，不得直接作为点击坐标。
4. 一次标定必须得到以下唯一且互不矛盾的基准线/区域：客户区原点、左侧导航边界、
   侧栏边界、侧栏顶部操作行/搜索栏、会话列表区、会话标题区、消息视口、工具栏和输入区。
5. 若启动时不是正常微信主会话外壳（如登录页、添加朋友弹窗遮挡或空白渲染），
   客户端可以继续显示和等待，但在取得第一张正常主会话外壳截图并完成标定前，不得开始业务点击。
6. 标定记录固定为：

```text
calibration_id, schema_version, hwnd, process_id,
window_rect, client_rect, client_screen_origin, dpi_scale,
image_width, image_height, capture_mode,
left_nav_bounds, sidebar_bounds, sidebar_header_bounds, session_list_bounds,
chat_header_bounds, message_viewport_bounds, toolbar_bounds, input_bounds,
anchors, confidence, conflicts, calibrated_at, executable
```

#### 16.11.3 坐标地图的生成与消费

1. 原 `0.9.20` 已验证点位作为“参考布局内的点”保留，不再作为当前屏幕绝对坐标。
   映射必须按所属区域分段执行，禁止对整个窗口使用一个全局宽高比例。
   对于 `0.9.20` 中依赖主窗口固定几何的参考点，唯一映射公式为：

```text
u = (x_ref - ref_region.left) / ref_region.width
v = (y_ref - ref_region.top) / ref_region.height
x_current = round(current_region.left + u * current_region.width)
y_current = round(current_region.top  + v * current_region.height)
```

   `ref_region` 必须是 `gray-v0.9.20` 中该点实际所属的参考区域，
   `current_region` 必须是本次启动标定生成的同名区域。映射结果必须仍位于
   `current_region` 和该目标的已知安全子区域内；越界时不点击，不允许回退到旧屏幕绝对坐标。
2. 坐标产生方式必须按下表唯一化；“业务逻辑不变”不等于“坐标代码不变”：

   | 目标/区域 | `0.9.20` 坐标来源 | `0.9.35` 唯一坐标来源 | 必须保持的 `0.9.20` 业务规则 |
   |---|---|---|---|
   | 侧栏顶部“+” | `0.9.20` 参考窗口中的固定参考点 | 用上述公式将该点从 `0.9.20 ref_sidebar_header_bounds` 映射到启动标定的 `sidebar_header_bounds`；不要求 OCR 识别“+” | 点击后必须按原菜单截图/OCR 确认“添加朋友”，确认失败不进入后续页面 |
   | 侧栏搜索框/返回入口 | 主窗口固定参考位置 | 按各自 `0.9.20` 所属的侧栏头部子区域映射到当前 `sidebar_header_bounds` | 搜索文字、搜索状态、结果短码及退出搜索验证全部不变 |
   | 会话行点击 | 固定 X + 当前行 Y | X 从 `0.9.20 ref_session_list_bounds` 映射到当前 `session_list_bounds`；Y 必须来自当前 `sessions/visible/search` 业务帧中的唯一目标行 | 不缓存“第几行”；点击后继续使用短码+私聊标题复核及既有一次重定位 |
   | 会话标题区 | 固定标题裁剪边界 | 使用启动标定的 `chat_header_bounds` 限定当前业务帧的 OCR 区域 | 标题文字、private/群聊及短码判定仍只来自当前业务帧 |
   | 消息视口 | 固定聊天区边界 | 使用启动标定的 `message_viewport_bounds` 限定当前业务帧的观察区域 | 文字、语音、图片、角色和每条消息的具体坐标必须来自当前业务帧，启动地图不生成消息对象 |
   | 输入框/工具栏/发送按钮 | 主窗口固定区域或参考点 | 按各自 `0.9.20` 所属参考区域映射到启动标定的 `toolbar_bounds/input_bounds` 及其安全子区域 | 输入前复核、S0/S1/S2、内容粘贴、发送和 `sent_ack` 顺序不变 |
   | “添加朋友”菜单项 | 点击“+”后的当前菜单帧 | 仍由该当前菜单帧的 OCR 项和完整边界决定；不使用启动地图猜位置 | 菜单确认、点击和后续页面验证不变 |
   | 添加朋友搜索页/搜索结果/邀请表单 | 各自当前页面的必要截图/OCR | 仍使用各自 `0.9.20` 原业务帧产生输入框、按钮、字段和确定目标 | 截图节点、填写顺序、字段验证、重试和结果终态不变 |
   | 语音/图片右键菜单 | 右键后的当前弹出菜单帧 | 仍使用当前菜单 HWND/截图/OCR 和已确认菜单项边界 | 菜单分类、动作回执、ActionJournal、Vision 和失败终态不变 |

   上表为 MECE 边界：启动坐标地图只替换主窗口稳定外壳的固定几何；
   任何会随客户、会话排序、新消息、菜单、弹窗或表单变化的对象，必须继续由
   `0.9.20` 当前必要业务帧生成，禁止从启动地图推测其具体位置。
3. 侧栏搜索、“+”号和返回按钮只使用侧栏头部坐标系。“+”号不要求 OCR 直接识别字符；
   应由启动标定已确认的侧栏边界和顶部搜索操作行，将 `0.9.20` 参考点映射到当前操作行。
   点击后仍必须使用 `0.9.20` 原有菜单出现确认，搜索图标不得冒充“+”。
4. 会话列表只复用标定的 `session_list_bounds`；具体客户行的 Y 坐标每轮必须来自当前
   `sessions/visible/search` 业务帧，不得缓存“第几行”。会话排序变化不会使全局标定失效；
   截图后到点击瞬间的重排仍由点击后短码标题复核拦截，并最多重新定位一次。
5. 会话标题、消息气泡、语音、图片和 customer/self 角色只复用标定的标题区/消息视口；
   具体对象坐标必须来自当前原业务帧，不得用启动截图推断消息位置。
6. 输入框、工具栏和发送按钮使用启动标定的输入区坐标系；S0/S1/S2 仍是原流程三个
   独立时间点，不得因复用布局标定而复用消息画面或省略新消息检查。
7. 添加朋友菜单、搜索页、搜索结果和邀请表单在启动时并不存在，不得伪造启动标定。
   它们继续使用 `0.9.20` 流程原本就会拍摄的对应页面截图定位；禁止为布局分层新增截图、
   改变点击顺序或改写原有失败/重试规则。
8. 坐标只能沿唯一链路生成：

```text
0.9.20参考区域内点/当前业务帧目标点
-> 当前标定区域内点
-> 本次运行固定客户区的屏幕点
-> 物理点击
```

各模块禁止自行相加 `window.left/top`、DPI 或历史偏移。点击前仍只保留原有前台
HWND 处理，不增加截图、OCR 或整套布局重算，也不得将事务入口检查下沉为每次内部点击的新门禁。
9. 窗口选择与激活必须直接继承 `0.9.20` 的生产语义，不得用新的统一前台门禁替代：
   `status/capabilities/sessions` 按 `0.9.20` 保持只读被动探测，`calibration-status` 作为
   `0.9.35` 保留的只读动作同样不得抢前台；其余主动业务动作仍使用 `0.9.20`
   的 `select_primary_visible_main_window(probe)` 选择可见微信主窗口，并调用原
   `activate_window(hwnd)` 后进入原业务分发。不得强制保留或新建
   `activate_calibrated_business_window()`、全局“激活成功”布尔门禁、统一
   `WECHAT_WINDOW_NOT_READY` 失败分支或其他 `0.9.20` 不存在的前台状态机。
10. `0.9.35` 因启动坐标地图新增的唯一主窗口条件是：在一笔新主动 UI 事务开始、消费
   `startup_layout_calibration` 前，用上述 `0.9.20` 可见窗口选择结果确认
   `selected_visible_hwnd == calibration.hwnd`。这是“坐标地图归属检查”，不是
   “前台激活成功门禁”。`main_windows` 中不可见的后台 `Weixin` 窗口只保留为
   诊断信息，不得参与选择或数量门禁。找不到可见窗口时按 `0.9.20`
   原有窗口不可用语义结束；选中 HWND 与标定 HWND 不一致时只能判定旧坐标地图
   失效，不得继续用旧地图点击，按本节已有 `WECHAT_UI_STARTUP_CALIBRATION_FAILED`
   技术失败停止新的 UI 动作。运行期间不自动移窗、恢复、重新标定或继续原业务。
   `pre_send_refresh` 若连微信基本布局都无法建立，固定按 6.0.4.4 的 `C2_PRE_SEND_LAYOUT_INVALID` 代码缺陷终态收口，不是自动移窗/重标定例外。
11. 事务开始后，不存在新的“主 HWND 前台等值检查”。
   事务开始后，微信自身可以打开另一个顶层 HWND，例如“添加朋友”菜单、
   添加朋友页、邀请表单、语音/图片右键菜单。这些页面必须完整继承 `0.9.20`
   的菜单/弹窗识别、目标边界、坐标、点击后验证、重试和失败处理；不得因前台 HWND
   已变为微信自身菜单/弹窗 HWND，再要求它等于微信主 HWND 而拒绝后续动作。
   共享坐标/点击底层只负责既有快照 ID、目标边界和坐标映射校验，禁止在
   `_current_click_snapshot`、`human_window_image_click_in_bounds` 或同类共享函数中
   新增“前台必须等于标定主 HWND”的通用硬门禁。不得为“添加朋友”单独增加
   白名单或绕过开关；必须在共享底层撤销这一越界门禁，使所有链路回到 `0.9.20` 语义。
12. 本节的“主窗口变化”只指已标定的微信主 HWND 消失、被新主 HWND 替代，
   或其客户区尺寸/DPI 已变化。点击后出现的微信菜单、对话框、右键菜单等
   自有子/顶层 HWND 成为前台，不等于主 HWND 更换，不得使启动地图失效。
   事务内操作这些动态菜单/弹窗时不消费主窗口启动坐标地图，而是完全使用
   `0.9.20` 原有当前弹窗帧、菜单边界和点击后验证。

#### 16.11.4 标定复用、失效和恢复

1. `startup_layout_calibration` 在同一客户端运行周期内由 C0—C4 共用。会话行重排、红点、
   新消息、聊天滚动、菜单开关和页面文字变化不改变微信外壳区域，不得因此重建全局标定。
2. 本期不设计普通业务 Flow 运行中的微信窗口变换恢复流程。不得为“看看有没有变”增加点击前截图、OCR、
   外框/DPI 轮询或新的业务状态。运营规程必须告知用户：客户端开始接单后不得拖动、缩放、
   切换 DPI 或重启微信；确需改变时先暂停接单，等当前事务安全结算后人工重启车金客户端。
3. 如果既有 Windows 事件、原流程本来就需要的业务帧或坐标地图归属检查已经确认
   已标定微信主 HWND 消失/被替换，或主窗口客户区尺寸/DPI 已变化，必须废弃旧地图、停止后续新的微信 UI 动作；
   当前业务 Flow 内不自动恢复默认档位、不自动重标定、不增加窗口分支。
   `C2_PRE_SEND_LAYOUT_INVALID` 同样只报告代码缺陷并结束当前 Flow，不开启自动归位/重标定。
   微信自有菜单/弹窗 HWND 出现、消失或成为前台不属于本条的主窗口变化。
   已形成的 ActionJournal、Outbox 和 sent_ack 仍按既有无 UI 事实恢复规则保留/结算，不得重复点击。
4. 只在客户端本次初始启动、尚未开始接单且零点击时，启动标定允许重取一帧。只有当当前客户区尺寸与 `0.9.20` 参考帧完全一致，
   且主结构锚点通过时，才允许直接使用参考坐标地图；这是精确匹配，不是任意分辨率的盲点兜底。
5. 客户端首次启动时仍无法标定，返回唯一启动技术错误 `WECHAT_UI_STARTUP_CALIBRATION_FAILED`，不点击微信，
   不入库消息、不调用 Brain、不创建业务 Handoff。不得按模块新增同义错误码。
6. 已打开非目标会话但尚未读取消息区或触发媒体/发送时，仍只使用既有
   `C2_VISIBLE_TARGET_STALE_AFTER_CLICK` 一次有限重新定位规则，不新增恢复状态。

#### 16.11.5 实现删除项与合同

1. 删除 `0.9.22` “每张新截图都重建整套微信外壳布局”的生产决策和同义旁路；保留每张
   业务帧对当前会话行、消息、弹窗和表单的原有观察。两者不得混为一个“每帧全局布局”。
2. 删除无条件 `980×860` 统一窗口策略，替换为本节按 DPI/工作区确定的唯一档位。保留最小客户区、最小化/离屏判断、
   OCR 置信阈值、已确认目标内部点击余量和诊断证据。
3. 不保留 `0.9.23` 与 `0.9.35` 两套可运行布局/前台决策、开关或双字段兜底。回滚只能整体回到
   不可变 `0.9.20` 分支/包，不改写业务数据。
4. 机器合同统一升为 `contract_revision=0.9.35`，Sidecar/Worker 至少交换
   `calibration_id/schema_version/hwnd/client_rect/dpi_scale/regions/executable`。旧的每帧
   `layout_snapshot_id` 不得继续伪装成全局标定 ID；如果业务帧仍需帧 ID，必须与 `calibration_id`
   分字段、分职责表达，不得一字段双语义。合同 SHA 必须由最终实现实算。

#### 16.11.6 验收矩阵

`0.9.35` 本期支持边界固定为：单显示器、Windows 系统显示缩放 `100%/125%/150%`、
本候选 UAT 锁定的微信版本、浅色模式和系统默认字体缩放。多显示器/混合 DPI、
其他微信版本、深色模式和额外字体缩放本期不承诺兼容，不得用合成图或 mock 宣称已支持。

1. 用真实 `0.9.20` 参考截图和新 Windows 客户区截图调用生产标定入口；禁止 Fake 直接返回
   `executable=true`、预制区域或预制点击点。合成图只能证明算法单元，不能冒充真实 Windows OCR/UAT。
2. 启动只获取一张客户区基线帧，实际生成导航栏、侧栏、顶部操作行、会话列表、标题、
   消息视口、工具栏和输入区；原图几何与同尺寸增强 OCR 各自职责可审计。
3. 验证原 `0.9.20` 的“+”、侧栏搜索、会话点击横坐标、标题区、消息区、输入框和发送按钮
   分别在所属区域内正确映射，禁止只断言坐标字段存在。
4. 会话行重排、新红点和新消息不重建全局标定，但具体会话行和消息对象必须使用当前业务帧；
   列表在截图与点击间重排时，点击后标题复核必须拦截并按原规则最多重定位一次。
5. 弹窗菜单、搜索页和邀请表单必须调用 `0.9.20` 原生产流程和原有业务帧；必须检查
   `Q搜索/O搜索/0搜索`、多竖线、非首行会话、搜索图标不得冒充“+”等已有兼容用例。
6. 公开生产入口必须证明正常 C1/C2/C3/C4 没有因标定新增业务截图/OCR，没有改变 `0.9.20`
   的点击顺序、重试规则、失败处理或状态机，并保留已批准的 C1 八类画面节点复用优化。
   对比必须检查真实生产函数调用次数和动作序列，
   不得只检查源码字符串或自己构造的模拟结果。
   还必须分别证明：只读探测不会抢前台；C1/C2/C3 真实生产入口的窗口选择、
   `activate_window(hwnd)` 调用次序和后续原流程必须与 `0.9.20` 对照一致；`0.9.35`
   只能在消费坐标地图前多一次 `selected_visible_hwnd == calibration.hwnd` 归属断言。
   测试必须断言未调用 `activate_calibrated_business_window`，未新增统一前台失败分支，
   且不得通过永久伪造前台检查结果绕过真实生产链。
   C1 必须从公开入口至少真实经过“点击 `+` -> 微信自身菜单 HWND 成为前台
   -> 点击已识别的添加朋友菜单项”，不得在第一次物理鼠标哨兵处提前结束，
   也不得 mock 掉中间菜单与二次点击。C2 语音菜单、图片菜单也必须分别证明微信自身
   顶层菜单不会被主 HWND 等值门禁误拦截；C3 必须证明 `0.9.20`
   的原激活与发送三时点都未改变。
   C1 还必须证明：邀请表单两个字段只复用填写前表单几何，
   不复用填写前 OCR 内容；两字段填写后实际重新截图/OCR并校验内容，
   且确认按钮点击坐标必须来自该填写后新帧。
7. 定向测试范围固定为窗口启动标定、C1 加好友、C2 会话定位/消息/媒体、C3 输入发送与
   C4 复用同一标定的直接受影响链路；本阶段不跑无关全量。只在正式打包门禁再跑全量。
8. 至少在两台单显示器 Windows 上覆盖 100%/125%/150% DPI、不同分辨率、同一 DPI 档位启动结果稳定不漂移、
   客户区过小时启动最小调整、会话列表重排和页面切换。另需验证 HWND/窗口真实改变时
   业务 Flow 内不自动移窗、不自动重标定、不继续新 UI 动作。`C2_PRE_SEND_LAYOUT_INVALID` 实机用例只验证零发送、零 HandoffEvent、完整 Bug 证据、任务/Flow 技术失败终态、UI 锁释放和 Worker 故障停止新接单；
   不测试自动归位、重标定或恢复拉单，因为这些功能不存在。
   未完成前不得声称已兼容所有分辨率。

`0.9.37` 是本轮修复前的已冻结代码、合同、来源与 ZIP 基线，规范化合同 SHA 为
`3157d37b8047ef3b39c53d4eab323e87ff7568c442372b08afc22cb1e2c9b9dc`，OmniAuto 来源提交为
`1a541c9eb330e83077c7bdffa0bb003a1c47d525`。`0.9.42` 的生产实现新增同帧 ROI 完整回退和非首屏定位复用，来源提交为 `307241810963c2e649ba04483a898687d06ba9f4`；其标签已冻结，但 Windows 门禁因旧测试夹具失败，没有形成 ZIP。`0.9.43` 只修正该测试夹具并同步版本合同，规范化合同 SHA 为 `a87275e55d6f25aeba3185d854f4e613a9209924ba4ac5ac4f6f49a3aeb00cef`，真实 OmniAuto 来源提交为 `27c59c8a0e9c85106a12f05f6f92e0193fefb5af`；车金基线提交 `b6ad192` 和 `gray-v0.9.43` 标签已冻结，只作为本轮父基线，不得写成当前 `0.9.45` 实现状态。

**实现状态补充：** `0.9.43` 仍是生产能力的父基线，其规范化合同 SHA 和 OmniAuto 来源提交只用于校验基线真实性。冻结前的早期 `0.9.44` 本地实现及其历次 SHA 曾因未完整覆盖“正常视口滑动 + 显式空复读 + 动作回执待连续性结算”而不合规；这些问题已在不可变 `gray-v0.9.44` 中关闭。该标签随后因 8 条旧 Windows 门禁测试夹具未迁移而没有生成 ZIP。当前 `0.9.45` 只迁移测试合同、消除 Pillow 弃用告警并同步 Schema，新合同 SHA 为 `8813425572dad678b86354856dad798c43a9c47192d17319dfb8e84c8877e99e`；独立 OmniAuto 来源提交已形成 `53caedad5baece001659aafcb5d7f86d98933e27`，车金提交和发布产物仍须按来源治理顺序真实形成。

## 17. 剩余上线前确认清单

- Windows UAT 阻塞项：按 3.2.4 从当前源码、测试、打包、CI 和运行时彻底删除悬浮球、
  键盘鼠标 Hook、F8 守护、守护进程/文件/错误码及全部兼容尾巴；保留独立逻辑 UI 锁、
  动作前复核、取消回调和 ActionJournal。自动化证明全流程不出现悬浮球、不拦截人工
  输入、主流程正常后，重新构建新候选并进入 Windows 实机测试。
- 发布阻塞项：完成账号/会话数据库迁移、初始账号安全创建、前后端 Cookie 登录联调，
  并关闭旧 Admin Bearer Token 和生产鉴权绕过入口。
- 发布阻塞项：登录、会话失效、退出、所有后台路由门禁、同权限、限速、审计脱敏和
  Worker Token 双向隔离自动化通过。
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
