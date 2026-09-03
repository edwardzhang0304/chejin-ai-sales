# AI智能客服售前跟进系统 版本更新记录

版本：v0.9.63

最后更新：2026-09-03

## 1. 文档职责

本文件只记录版本、提交、验证、发布风险、包哈希和回滚证据，不定义新的产品或架构规则。
项目级权威文档固定为四份：

1. `AI智能客服售前跟进系统_PRD_运营后台统一版_v0.9.63.md`
2. `AI智能客服售前跟进系统_技术方案手册_v0.9.63.md`
3. 本版本更新记录
4. `AI智能客服售前跟进系统_全流程图_v0.9.63.puml`

接口合同、事务恢复架构、场景矩阵、专项测试报告、交接文档、一致性检查、文档目录和子流程图
已合并进上述四份，不再作为独立开发依据。代码目录 README、第三方声明、测试夹具和提示词属于
工程运行材料，不属于项目决策文档。

## 2. 当前候选状态

| 项目 | 当前值 |
|---|---|
| 候选工作区 | 灰度分支 `codex/gray-release-0.9.x`；从未进入自动更新的 `0.9.62` 候选后续修正 |
| 本轮整改父基线 | `0.9.62` 候选提交 `90abd925b7aaff4647be347a0e8765fad4a7bca7`；不改写其标签或已生成包，使用新版号形成唯一新产物 |
| 灰度版本 | 修正候选 `0.9.63`；客户端默认连接 `https://jiangsuchejin.com/api`，并统一客户端、后端、C2 合同、Schema、manifest 和权威文档的精确版本 |
| 正式上线版本系列 | `1.0.x`；只接收通过灰度验收的正式上线能力和上线后的稳定性修复，不混入下一期优化 |
| 下一期优化版本系列 | `1.1.x`；承接本记录第 8 节除已提前到灰度 `0.9.59` 的耗时观测清理和客户端检查更新外的清单，不反向覆盖 `1.0.x` 正式产物 |
| 灰度分支 | `codex/gray-release-0.9.x`；`0.9.63` 只接收生产地址与发布合同统一，不混入 AI 回复效果优化、向量语义检索、Qt 裁剪或其他优化 |
| 当前机器合同 | 已由正式生成器统一为 `contract_revision=0.9.63 / 18a82d17f94118c56e0f1497df6ff94a828a7db9daa3b9920d1dd9b9e136240e` |
| 来源治理 | `.chejin-source.json` 继续锁定独立 OmniAuto 真实提交 `a29ba4e1278d55bc6aed617d045b0a85c10c4aff`；本轮不修改 OmniAuto 生产逻辑 |
| PRD | v0.9.63；无产品功能变更，只记录发布版本与合同统一 |
| 技术方案 | v0.9.63；明确严格单合同和停机同步切换顺序 |
| 全流程图 | v0.9.63；业务流程不变，只更新当前合同修订号与 SHA |
| 发布状态 | `0.9.62` 未进入自动更新且不再发布；`0.9.63` 正在执行测试、正式打包和后端配套发布门禁 |

### 2.0.63 2026-09-03：生产地址与发布合同统一

- `0.9.62` 已生成的候选包保持原哈希不改写，但因客户端版本与 C2 合同修订号不一致，不登记到 `update.jiangsuchejin.com`。
- 修正候选使用新版号 `0.9.63`；Windows 客户端、后端主合同、C2 生成 Schema、manifest、PRD、技术方案、版本记录和全流程图使用同一精确版本。
- 合同字段语义不变，但 revision 和规范化 SHA 已变；后端严格只接受 `0.9.63 / 18a82d17f...6240e`，不增加 `0.9.61` 兼容入口。
- 生产切换必须暂停旧 Worker，确认 Outbox、ActionJournal、Ledger 和发送回执无未结算事实，再替换后端与客户端并恢复接单；严禁新旧合同混跑。

### 2.0.61 2026-09-03：知识管理实现与 Provider 全链验证

- PRD、技术方案和版本记录统一为 `v0.9.61`；`v0.9.60` 技术手册原始内容保留在 Git 历史，不在旧版本文档上追加知识管理规则，也不作为当前交付文件重复保留。
- 知识管理保留三张顶部概览卡片、规则列表和详情抽屉；列表页右上角只放`新增知识`，不增加批量发布入口。
- 新增和编辑必须先明确点击`保存草稿`。草稿可以继续查看和编辑，但不得进入 Brain；保存与发布是两个独立请求，取消或关闭未保存编辑时不自动落库。
- 已保存草稿从详情单独发起`发布`，服务端自动校验并展示差异，操作人再次确认后生成完整不可变快照、构建该版本独立关键词索引并原子切换当前版本。
- 新发布只影响切换后创建的新 `message_batch`；同一批次的普通 Brain、快速 Brain、重试、修复和 Guard 固定复用原 `knowledge_release_id`。
- 车金 PostgreSQL 是草稿、发布快照、索引和当前版本指针的唯一权威源。旧 KnowledgeRuntime 的 FAQ、policy、product_scoped 和经验命中不得绕过冻结版本进入车金 C3 Provider。
- `0.9.61` 实际实现为确定性关键词加权检索：使用同批次冻结的必要历史、当前消息和图片规范化查询，在发布版本内按标题/正文词项召回；不冒充向量语义检索，近义表达优化留待后续 AI 回复优化版本。
- 架构师使用真实 PostgreSQL 空库迁移、正式知识 API、冻结快照、Adapter、普通/快速 Brain 自动路由和 Provider HTTP 请求复核，结果 `11 passed`；旧 product_scoped 毒规则在修复前可被生产运行时选中，修复后最终 Provider 请求中不存在，新发布规则存在。
- Brain 合同独立复核 `209 checks / 0 failures`；该功能属于后端、前端与 Provider 数据链，不依赖 Windows OCR、鼠标或微信实机 UAT。

### 2.0.59 2026-09-03：PRD 同步 1.1.0 知识管理规划

- PRD 升为 `v0.9.60`，现有 C0—C4、Windows Worker、C2 合同、车辆管理和飞书通知业务口径不变。
- 新增 1.1.0 知识管理产品范围：查看、新增/编辑、发布确认、归档、发布记录和版本回滚。
- 明确草稿不影响线上 Brain，发布前自动校验并展示差异，成功后生成不可变版本并原子切换；同一 Brain 批次固定使用同一 `knowledge_release_id`。
- 首期不做待审核队列、批准/驳回、独立审核角色、批量导入、AI 回复优化、经验池或 Prompt 管理。
- 仅记录下一期产品规划，不把知识管理混入 `0.9.60 / 1.0.0` 当前实现或打包范围。

### 2.0.58 2026-09-02：低风险代码瘦身与 Brain 主生成额度修复升为 0.9.60

- 从不可变 `gray-v0.9.59` 开发，不修改已发布 `0.9.59` 技术手册、标签或包；当前唯一实现候选升为 `0.9.60`。
- 项目交付目录只保留当前 PRD、当前技术方案、版本更新记录和唯一全流程图；旧版技术手册及耗时/更新专项复审材料从工作树删除，历史继续由 Git 与本记录追溯，不再作为独立开发入口。
- Worker Web 资源改为同一 TSX 源码的生产压缩构建；正式产物不携带 React 开发版。
- 正式 EXE 和 Fast UAT 运行包排除 OmniAuto 测试目录及仓库开发资料；生产 Sidecar、语音、图片、Brain 和动态插件保留。
- 正式运行包缺少源码测试脚本时，“完整诊断”明确返回 `SOURCE_LEVEL_DIAGNOSTICS_NOT_INCLUDED`，显示“正式客户端不提供源码级诊断”且零子进程，不为兼容该入口重新打包测试。
- 删除旧 `reset-client-bind` 路由及重复处理函数，后台和客户端统一使用 `reset-binding`；旧路由必须返回 404。
- FastAPI 启停收口到 lifespan；Worker/后端消息规范化部署副本逐字一致；只删除已经静态和生产边界验证无调用的兼容函数及两个 OmniAuto 旧插件门面。
- 自审期间发现 `optional_plugins/dispatch.py` 和 `voice/compatibility.py` 仍被独立 OmniAuto 生产入口动态使用，已保留；不以车金单仓扫描代替双仓边界验证。
- 普通 Brain、商品快速 Brain（`routine_product_fast`）、低权限快速 Brain（`low_authority_fast`）三条主生成路径的 `max_tokens` 精确统一为 `8192`；该值是上限，不要求每次调用生成到上限。
- 额度修复不得修改模型、Prompt、超时、总时间预算、重试、备用模型、JSON 修复、质量返修、语义审核、回复长度、Guard、Handoff 或 C0—C4 状态机。
- PR #15 的实际开发父基线是 `0.9.38`，不得原样合并；必须在已发布 `0.9.59 / d460905a879f7721a4c48712222e5de7768c5793` 上重新生成或 rebase 最小补丁，只取三条生产额度修改。
- 三条路径分别经过生产自动路由和最终 Provider HTTP 请求体断言，直接证明请求参数为 `8192`；同时复跑现有 209 项 Brain 快速合同。该小参数变更不牵连 Windows、C0—C4 全量、Worker、OCR、语音或图片测试。
- 双仓顺序固定为：先形成独立 OmniAuto 真实提交，再同步车金内嵌副本和 `.chejin-source.json`，最后形成车金 `0.9.60` 候选提交。Brain 额度不属于 C2 消息合同字段；集成后复跑生成器，C2 合同内容未变时继续使用真实 SHA `30cdc4e…51ea6`。当前仍未提交、推送或打包。

### 2.0.57 2026-09-01：耗时观测清理与客户端检查更新目标候选 0.9.59

- 后端 `process_stage_runs` 固定为唯一运营级耗时权威；Worker telemetry 只作有界断网缓冲、永久 4xx 隔离证据和后端权威报告快照缓存，禁止参与业务决策或本地重算报告。
- C3/C4 标准发送耗时由 Worker 外层单调计时覆盖 Sidecar 启动、通信和返回；Sidecar 内部耗时只作诊断，不再冒充标准阶段。
- telemetry 上传的超时、断网、408/425/429 和 5xx 指数退避重试；其他不可重试 4xx 隔离且不再重传。本地事件、单条负载、总容量和后端权威快照均有硬上限，观测数据不得挤占业务磁盘。
- UAT 证据包导出后端计算的权威报告快照、pending 事件和 quarantined 事件；已上传事件即使从本地删除，仍可从权威快照证明完整耗时。
- C1、C2、C3、C4 和 Handoff 均以单条测试直接对比观测关/开时的前置输入、中间调用次数/顺序和最终业务数据，不再以旧测试重复执行代替直接对比。
- C0 线索进入和轮询自动分配已补入相同的前—中—后直接对比硬门禁；C0—C4 与 Handoff 不再缺项。
- 同一发送阶段的每次真实重试增加 attempt 并分配全新 `stage_run_id`，禁止第二次耗时覆盖第一次；流程关联表和阶段尝试表各最多保留最近 5000 条，并共同服从 64 MiB 总容量门禁。
- 本轮不修改 C0—C4、媒体编排、Brain、Handoff、飞书、UI 锁、业务重试和业务终态；当前未提交、未推送、未打包。
- 客户端检查更新已从 `1.1.x` 提前到同一灰度候选：设置页手动点击“检查更新”，无新版明确提示，有新版则自动下载，当前任务安全结束且本地事务全部结算后安装、重启，启动失败自动回滚。
- `0.9.59` 是首个内置更新器的启动版，现有 `0.9.57` 必须人工安装该版一次；自动更新能力从已安装 `0.9.59` 后的下一版升级开始生效。
- 普通更新严禁删除或覆盖本地绑定、配置、SQLite、Ledger、ActionJournal、Outbox、sent_ack 和故障证据；更新成功也不得自动解除用户暂停或 `faulted`。
- 新客户端必须让正式 UI、TaskRunner、C2 和线程监控跨稳定窗持续存活后才写健康标记；Worker 与 Updater 复用唯一纯健康合同。Windows 门禁至少一次启动真实打包客户端，自制探针只证明目录切换和失败回滚。
- 健康门只读取本次计划中的 120 秒期限，不再另设 10 秒暗门；Updater 结果对账以 PID、创建时间和可执行路径确认进程身份，并使用 180 秒总截止时间，卡死时只安全终止原进程并恢复一次，PID 复用不得终止无关进程。
- Updater 在目录切换后丢失、损坏或错配结果时不再永久等待：有认证健康证据则幂等结算，否则执行一次独立回滚；结果文件异常统一有限失败、清除本次门禁并保持暂停/故障，无法终止原 Updater 时禁止并发恢复。
- 受保护 SQLite 快照改为版本化冻结业务字段；兼容新增默认列不误报，冻结字段缺失或变化仍严格阻断。
- 本条冻结口径已在隔离候选中实现：真实 `0.9.58` 耗时观测提交与客户端检查更新共同形成 `0.9.59` 本地候选；当前只完成代码、合同、Schema 和定向门禁，仍不得在架构复审前提交、推送或打包。

### 2.0.56 2026-09-01：私聊多行文字与文档入口收口为 0.9.57

- 私聊 OCR 长文字统一由 OmniAuto 在同一截图内按唯一头像锚点和既有续行规则合并；Worker 与后端不建立第二套拼接或角色判断。
- 无截图兼容入口保持 `0.9.56` 原行为；正式车金私聊仍必须具备截图和头像锚点，未放宽语音、图片或发送安全门禁。
- 合同为 `0.9.57 / c978438ece0b9e950fe234f1366b4074635200886b5e2872d50f6bb84944c4be`；独立 OmniAuto 来源提交为 `c9d9ac5e80c571db9a35ff05c299c814a9d25999`，车金功能提交为 `f63613a51157c480a442e996ea8b211b3ba484fe`。
- 项目级交付文档重新收口为 PRD、技术方案、版本更新记录和全流程图四份；历史版本仍可从 Git 提交追溯，不在工作树保留平行副本。

### 2.0.55 2026-08-31：共享 LLM 总预算确定性测试升为 0.9.56

- `0.9.55` GitHub Windows 门禁的唯一失败是 `test_llm_total_budget_caps_fallback_to_remaining_time`：预期备用模型 wall timeout `<0.110` 秒，Windows 实际记录 `0.111` 秒；其余 115 条上下文测试通过，失败发生在 209 项合同和打包之前，因此没有 ZIP。
- 根因是测试用真实 `sleep(0.12)` 推进预算，并把 1 毫秒调度/计时粒度当作算法结论，不是生产总预算算法错误。
- `0.9.56` 使用可控单调时钟精确推进主调用耗时，继续断言备用模型只能使用整轮剩余预算；生产 `llm_total_time_budget`、Provider 超时、重试和 Brain 均不修改。合同为 `0.9.56 / a94575d4dd275a59c47fb5c4be60794f6d55f5c6158176b08e3fdbcb9723577c`，独立 OmniAuto 来源提交为 `7b25b0496d7fa82e7a73ea29e5caa38955a704d4`。

### 2.0.54 2026-08-31：Windows Brain 合同报告 UTF-8 输出升为 0.9.55

- `0.9.54` GitHub Windows 灰度门禁已证明隔离 Brain UTF-8 产品修复有效：数据库到 Provider 的 116 条上下文测试全部通过；随后独立 209 项 Brain 合同脚本在打印中文报告时触发 Windows 默认 `charmap` 编码错误，门禁按规则停止且未生成 ZIP。
- `0.9.55` 只在 Windows 灰度门禁的 Brain 合同步骤设置 `PYTHONUTF8=1`，保证测试报告按 UTF-8 输出；不修改 Brain、Provider、历史快照、C0—C4、媒体编排、Handoff 或状态机。
- 产品回归仍在隔离子进程中强制 `PYTHONIOENCODING=ascii`，因此 CI 的 UTF-8 设置不能掩盖产品协议错误。合同为 `0.9.55 / c290881a76f9701fac15221e48898c79101077b20bc07f937d1138a5d334e83f`，独立 OmniAuto 来源提交为 `e65e363116791e43e1dd5da5a55c31c060237e09`。

### 2.0.53 2026-08-31：Windows 隔离 Brain UTF-8 通信升为 0.9.54

- `0.9.53` GitHub Windows 灰度门禁证明 Brain 工作流及 Provider 已成功，但隔离子进程在把含中文的 JSON 结果写回父进程时继承 Windows 默认代码页，进程以退出码 1 结束，导致 6 条真实 Provider 输入测试失败且未生成 ZIP。
- `0.9.54` 将隔离进程 stdin/stdout 固定为 UTF-8 字节协议；父进程对退出码、JSON 和 `ok` 的严格校验保持不变，不放宽任何 Brain 或发送安全门禁。
- 新增强制 `PYTHONIOENCODING=ascii` 的反向测试，中文请求和中文 Brain 结果必须经过真实隔离子进程完整往返；数据库到 Provider 的 6 条上下文链继续保留，禁止跳过后强行打包。
- 本轮不修改权威历史、Adapter 桥、Brain 语义、Provider 路由、C0—C4、媒体编排、Handoff 或状态机。合同为 `0.9.54 / 841cc44030d00e06bcd97da82ee2e4392ba12fe18f44bd4d0ae40e5f07be1d86`，独立 OmniAuto 来源提交为 `b052bfa5a7333ad5651dc2de2e13ab10a74fe961`。

### 2.0.52 2026-08-31：Brain 权威历史与语义判断归属升为 0.9.53

- `MessageEvent` 固定为车金唯一历史来源；后端按 batch 冻结不可变 `brain_context_snapshot`，Adapter 通过唯一共享桥把历史、当前批次、机械策略和交互状态交给普通与快速 Brain，车金模式禁止回退 `RawMessageStore`。
- 架构复审取消车金桥的关键词需求解析：客户需求的累积、预算变化、否定、替换、并列和修饰范围统一由 Brain 根据完整 `history_text + current_batch_text` 判断。车金桥不得生成或改写 `last_customer_need_text/last_customer_need_terms`，历史原话保持不可变。独立 OmniAuto 原生流程既有偏好缓存保持原样，不因车金适配而重写。
- 快照构建或桥接失败统一结算为 `failed/retry_later + AI_CONTEXT_BUILD_FAILED`，零 Provider、零回复、零 Handoff、零飞书，不得残留 `generating`。
- 数据库到最终 Provider 输入的生产链测试必须证明全部历史原话按顺序到达、当前消息只出现一次、普通/快速 Brain 同源、语义判断指令存在且车金桥未调用关键词需求更新器；不得用关键词帮助函数或固定 Provider 回包自证 AI 语义正确。另覆盖 RawMessageStore 禁用和快照失败收尾；本轮为纯后端数据链，不要求 Windows OCR/鼠标实机证明。
- 合同为 `0.9.53 / 5b2276513bfeebe759a0d095db3652ee7ba1b5c6b8c11ef0cf7da27a7bf26e4a`。独立 OmniAuto 真实提交为 `d1dc417a5a3c051622104485bc3c1bd3dd1840f3`，`.chejin-source.json` 已更新；车金集成提交、标签、ZIP 和部署仍须按发布流程真实形成。

### 2.0.51 2026-08-30：跨轮恢复计时器与在途 Flow 互斥方案冻结为 0.9.52

- 现场时间线确认：旧读取轮次的 120 秒身份恢复 hold 在新 `unread_generation` 的语音已进入正式处理时仍然到期，并直接创建 Handoff，导致新语音虽转写成功却无法按正常链进入 Brain。
- 根因是旧 hold 只有客户级计时，没有完整绑定 `origin_unread_generation + gate_identity_key`，且轻量读取授权和消息入库前置路径会先执行到期升级，没有检查同客户更新的 C2/C3 Flow、媒体 ActionJournal 或 Outbox 是否仍在途。
- `0.9.52` 固定为：新代次或新 Flow 注册时原子 suspend 旧 hold；当前 Flow 终态前，授权续权、媒体动作、ingest 和心跳均不得执行旧 Handoff。终态后按同一问题恢复原计时、问题消失 resolved、不同问题 superseded 并从 0 新建；不得简单重置 120 秒，也不得让客户持续发消息无限延期真实未解决问题。
- 同类审计发现两处并一并冻结：`retry_required` 的第一次/第二次只允许在同一 `unread_generation` 内累计；C4 `next_recall_at` 到期不得在 C2/C3 Flow 或待处理新未读存在时切换 `recall_precheck`。
- 调度器在路由级检查后必须按 `Binding -> Worker` 锁顺序刷新最终接单状态；并发暂停已生效时立即返回空目标，不得启动 C4、检查 hold 到期或改变客户状态。
- Task lease、ReplyAction TTL、Brain generation attempt、Outbox/send_ack 重试和 UI lock 已各自绑定 task/action/batch/不可变记录/Flow，审计未发现同类客户级定时器越权；本轮不得放宽其现有 fencing 和幂等门禁。
- 定向吸收 PR #13 发现的图片内 OCR 误否决问题，但不采用其“面积比直接决定图片”实现：`80%/30%` 只生成待右键验证候选；文字菜单保留同帧 OCR 文字、零剪贴板/零 Vision，并由 Worker 记录仅当前 `read_run_id` 有效的内存临时文字回执。强制复读和后续图片动作帧先用该回执恢复文字，再走唯一连续性比较器；映射唯一时禁止第二次右键并继续其他图片，缺失/跨轮/碰撞/多解时技术失败。回执不进入 Ledger、Outbox 或后端。图片菜单且真实新位图回执才提交图片；菜单不明时技术失败，不猜测。
- 本地实现已完成：旧 hold 绑定原未读代次与独立问题身份；新代次/同会话 Flow 原子 suspend；新代次即使异常指纹相同也必须新建计时；Worker 非 running 时禁止到期 Handoff，恢复后先权威复读；轻量授权只读；Flow 终态后统一结算；C4 不得抢占。机器合同为 `0.9.52 / 053ec7587763a8360c3d134916501108af4304c38e8e3c69d4e4a80239587940`。SQLite 生产服务/HTTP/Worker 定向回归及隔离 PostgreSQL 双事务行锁测试已通过；当前等待架构复审，尚未提交、推送、打包、部署或完成 Windows UAT。

### 2.0.50 2026-08-30：未读代次有限复读与终态合同冻结为 0.9.51

- 第一次读取不明确固定返回 `retry_required` 并合法结束 Flow 1；同一未读代次第二个不同 `read_run_id` 仍不明确时返回 `technical_failed`、Worker=`faulted` 并合法结束 Flow 2，禁止无限复读。
- 完整画面只有历史消息时为 `no_change` 并消费未读；已入库文字但媒体未确认时保留未读并按服务端时间重读；媒体已形成终态后消费未读。空 Outbox 不得把 `retry_required/technical_failed` 覆盖成 `read_confirmed`。
- 该版本当时没有写死“新未读代次开始后旧恢复计时器必须 suspend”，因此该遗漏由 `0.9.52` 方案补齐。不可变实现为车金提交 `2335db2`、标签 `gray-v0.9.51`、合同 `0.9.51 / 9f831b2978707c1abb1db0b7bf0cabb45d728ff7a7f307fa2d9bb4fc232a54df`。

### 2.0.49 2026-08-29：C3 checkpoint 连续性凭证闭环升为 0.9.50

- Windows UAT 语音事实、Brain 和 checkpoint 均正常，发送前新旧序列 SHA 也完全一致，但 Worker 把 `pre_send_fact_checkpoint_comparison` 放在嵌套 `payload` 内，后续发送入口只读顶层，最终发送门因缺少 checkpoint 边界凭证误报 `C3_CONTEXT_CHANGED_BEFORE_SEND`。
- 比较结果现在只保留一个顶层位置；Worker 根据自己的 `matched_pairs` 绑定边界，Sidecar 调用同一共享比较器验证最终画面，后端不重新猜消息身份。
- 共享纯比较器位于独立 OmniAuto 可直接携带的模块，不导入 Worker；Worker 原调用点只重导出同一实现，不保留第二套算法。独立 `python -I` 检查与真实 Bridge JSON 序列化、Sidecar 解析及最终门检查已纳入回归。
- 缺失或矛盾的连续性绑定固定为 `C3_SEND_CONTEXT_GUARD_INVALID`：零输入、零点击、零发送，Worker=`faulted`；后端记录技术失败，不创建 HandoffEvent，不发飞书。
- 组合验收贯通正式语音入库、后端 checkpoint、Worker 绑定、Sidecar 发送门、正式 claim/send/sent_ack 和数据库；另有技术失败正反测试验证零 Handoff。测试使用 Windows OCR/鼠标的受控替身，不冒充 Windows 实机 UAT。
- 合同为 `0.9.50 / d1500d91da35f92f7b37a73317e47402123b685fddafcb2e65c95fce63b84dd5`；独立 OmniAuto 真实提交已形成为 `f6a7706c20c22d8cd176fefeccfdcf3027eaa753`，车金集成提交待形成。

### 2.0.48 2026-08-29：旧坏 Outbox 全局门禁有限释放升为 0.9.49

- `0.9.48` Windows UAT 中，Worker 点击开始后约 4.3 秒再次自动暂停；后端状态没有抢写，客户端因为旧 `0.9.45` Outbox 持续处于 `capability_paused` 而主动写回暂停。
- 旧代码只结束旧 Flow，却没有把永远无法满足新合同的旧 Outbox 和等待 Ledger 结算为有限终态，因此“draining 已解除”不等于“全局事务门禁已解除”。
- 本轮仅对严格早于当前版本、且错误精确为 `C2_SEQUENCE_ALIGNMENT_EVIDENCE_INVALID` 的持久化载荷启用兼容终态：原始 Outbox 完整保留并转为 `identity_quarantined`；同一客户、同一读取轮次、同一 source key 的 waiting Ledger 在同一 SQLite 事务内转为 `failed + not_required`；不请求后端、不操作微信、不清整库。
- 当前客户保持单独隔离，其他客户和任务拉取恢复；当前版本若自行产生同类坏数据，仍保持 `capability_paused` 和故障关闭。
- 使用现场捕获的真实 SQLite 回放验证：人工开始后保持 `running`，连续 3 次进入 `/tasks/pull`，消息 HTTP 0 次、微信操作 0 次；TaskRunner `382 passed + 54 subtests`，Storage `28 passed + 5 subtests`。
- 合同为 `0.9.49 / 2302479941f86d8f854dd0169716d0eb89b1ba0f8952344728fefb86d4811289`；独立 OmniAuto 来源提交为 `8100c2f8bd0cfcd80874d95d69b76e11c9c51a4f`，仅同步生成 Schema。

### 2.0.47 2026-08-29：灰度工作流作用域修正升为 0.9.48

- `gray-v0.9.47` 已不可变打标，但首次 GitHub Actions 在创建 Windows job 前失败：job 级 `env` 使用了该位置不可用的 `runner.temp` 上下文，因此没有执行测试、打包或产生 ZIP。
- 修复仅把 `CHEJIN_WORKER_HOME` 移到具体 PowerShell 测试 step 内，通过 `$env:RUNNER_TEMP` 设置；新增静态反向门禁，禁止再次把 `runner.temp` 放回 job 级 `env`。
- Worker、Sidecar、后端、Brain、媒体编排、重启恢复和 `faulted` 状态代码均未再修改。
- 合同为 `0.9.48 / 2cdf88e61310157fc058eeeee4a5300b52c53234a964737945e46ecd6c21f41b`；独立 OmniAuto 来源提交为 `3413c591357f6fd790f53b0f967ff6bc9c6de57f`，仅同步生成 Schema。

### 2.0.46 2026-08-29：旧流程恢复和故障状态持久化升为 0.9.47

- 现场故障来自旧语音动作已确认、但失败 Ledger 和 Journal 没有被正确收口，导致客户端保持心跳却无法继续接单；本轮不修改媒体动作，只补重启后的事实结算。
- 已确认物理语音/图片 Journal 仅可修复身份、读取轮次和媒体类型完全一致、且尚未得到后端确认的 `failed + waiting` Ledger；旧失败载荷保留为审计证据，已完成或已确认事实不可覆盖。
- 后端事实已确认后，如果 Windows 临时占用 Journal 文件，Worker 保留旧 Flow 并在下一轮只重试本地归档，零重复 HTTP、零重复语音转写、零重复图片复制。
- 不可裁决冲突先持久化 `faulted`；状态接口断网、旧 Flow 结束失败、重复心跳和中央暂停函数均不得把它改成普通 `paused`。只有明确且成功的人工“开始接单”可以解除。
- 灰度打包门禁调整为“本次受影响测试 + 合同/来源检查 + ZIP 解压启动”，不再每次重跑正式发布的全量 1057 项；正式发布全量门禁保持不变。
- 合同为 `0.9.47 / c3d1b2a8f436182df84d1b692f6be76cb233fb2d7716664a9db0107da8cf0b28`；独立 OmniAuto 来源提交为 `6fc2b12a1c121ec747f9f7ef7123775474dff1d1`，仅同步生成 Schema。

### 2.0.45 2026-08-29：C2 嵌套序列合同与旧 Outbox 有限终态升为 0.9.46

- Windows UAT 的语音转写已经成功，但 Worker 上传时后端返回 `400 VALIDATION_ERROR`；现场坏 Outbox 缺少完整嵌套序列证据，失败后旧 Flow 又停留在 `draining`。
- Worker 现在遍历完整数据流，不再只检查顶层字段：只要载荷存在 messages、slot ledger 或本地 Ledger，序列证据缺失或畸形即在本地保留原载荷并隔离，零 HTTP，旧 Flow=`technical_failed` 并解除 `draining`；重启不得重复发送同一坏载荷。
- 后端逐项核对 observation ID 声明的 `post_index` 是否对应实际位置，并验证新增后缀连续、无断裂。错位、断裂、越界或伪造载荷返回 400，数据库零错误事实。
- 正常文字、语音、图片、混合、大载荷拆包和重启恢复均经过正式 TaskRunner、真实 SQLite、正式 HTTP 路由和后端数据库复核；架构复审独立结果为核心链路 `662 passed + 141 subtests`，两个精确反例 `4 passed`，PostgreSQL 并发专项 `1 skipped` 且未冒充执行。
- Windows 包根目录、可执行文件和启动入口统一使用 ASCII 英文名 `CheJinWorkerClient` / `CheJinWorkerClient.exe`，避免中文解压目录和 PowerShell 路径误用；安装包文件名为 `chejin-worker-v0.9.46-windows-x64.zip`。
- 合同为 `0.9.46 / 5b7cc473f16062dd668a6880379b2375108985abd779f5dcb53deb628da5a371`；独立 OmniAuto 来源提交为 `69c69a60b7e08a31f9d773864b14b35faf86754b`。自动化不代替 Windows OCR、窗口和物理鼠标 UAT。

### 2.0.44 2026-08-29：Windows 完整门禁旧夹具收口升为 0.9.45

- `gray-v0.9.44` 已形成提交 `e8998b9806107c6bd9736e06465e4d46af87eb6a` 和不可变标签；正式 Windows 工作流真实执行 1050 项测试，发现 8 条旧合同夹具仍要求已经废弃的 prepare 帧几何身份或缺少正式动作结果证明，因此在生成 ZIP 前失败。
- 生产代码没有因此放宽或重写。两份旧测试已迁移到 `0.9.44` 已冻结的正式语义：prepare 帧 observation/坐标/指纹只作诊断，execute 使用当前帧唯一目标；语音/图片提交必须带本次实际动作结果证明；媒体终态包含 `technical_failed`。
- 按 Windows 打包脚本原样复跑本机完整门禁时，又发现 1 条 OmniAuto 发送兼容夹具直接构造空 guard、没有经过 Worker 正式连续性绑定。该夹具已改为调用生产 Worker guard 绑定函数并提供同帧原始 observations；197 项 Win32/OCR 兼容检查与随后整套 `run_checks.py` 均通过，没有删除或放宽发送安全门。
- Pillow P2 使用 `get_flattened_data()`，并保留旧 Pillow 的 `getdata()` 回退。图片受影响测试在 `DeprecationWarning` 按错误处理时为零告警，不改像素内容、Vision 结果或图片身份规则。
- 因 `gray-v0.9.44` 标签不可覆盖，修正后的候选顺延为 `0.9.45`；客户端、后端合同、生成 Schema、manifest、CI 和文档统一升版，规范化合同 SHA 为 `8813425572dad678b86354856dad798c43a9c47192d17319dfb8e84c8877e99e`。
- 本轮不得修改 Sidecar/Worker/后端职责、媒体处理顺序、ActionJournal、Ledger/Outbox、Brain、Handoff、S0/S1/S2 或 C0—C4 状态机。

### 2.0.43 2026-08-28：跨帧几何误判与媒体动作结果后绑定升为 0.9.44

- 现场确认：同一消息气泡边框仅变化 1px 时，旧 64 分桶坐标可能跨桶，发送安全门因此错误认定客户新增消息并阻止正常发送。
- 本轮从不可变标签 `gray-v0.9.43`（`b6ad192`）建立最小候选。审计确认仅删除跨帧像素判断会失去“实际动作结果如何取得正式身份”的闭环，因此最终冻结为最小职责调整，不重写媒体编排。
- 跨帧业务投影固定为顺序、角色、类型、规范化内容和媒体状态五字段。坐标、OCR 框、气泡尺寸、分桶、截图像素和 frame/observation ID 继续保留为当前帧定位、点击及诊断证据，但不得参与 checkpoint、pre_send_refresh 或 S0/S1/S2 的业务相等判断。
- Sidecar 只负责当前帧 UI 观察、同帧物理行归并、目标边界和动作证据；Worker 唯一负责业务连续性、动作准入和长期身份；后端只冻结 checkpoint、验真、去重和结算。共享投影是纯函数，不得成为第四个身份或动作决策者。
- 同帧语音顺序写死为“Sidecar 唯一归并 OCR/visual 物理行 -> 输出已归并 observations -> 五字段共享投影 -> Worker 只验重复/冲突合同与决定身份”；共享层和 Worker 均不得再次归并，测试不得替生产 Sidecar 预先合并输入。
- 当前帧几何继续负责物理行解析和点击；媒体正式身份只在实际动作结果后由 Worker 绑定。语音使用唯一新增转写和动作回执，图片使用实际复制图片字节 SHA 及菜单/点击/剪贴板回执。两条相同三秒语音或相似图片按“当前画面最下方一条 -> 动作结果 -> 完整复读 -> 剩余一条”串行处理，不在动作前猜 A/B。
- 图片复制后画面可以不变：ActionJournal 新增仅在同一 `read_run_id` 生效的临时动作槽位，记录动作计划版本、序列 occurrence 和动作前五字段完整序列摘要。新画面必须与旧序列相同，或唯一地仅在尾部追加新消息，才可保留“已处理”标记并选下一张。该槽位不是消息身份，不进入 `worker_stable_id/source_message_key`；截断、替换、插入、换序或多解时技术失败，不得按行号、坐标或气泡指纹继承。
- 动作无结果、多结果、错对象、结果无法绑定或 Sidecar/Worker 合同冲突属于客户端技术故障：零正式消息、零 Brain、零 Handoff、零飞书、零重复 UI，当前 task/Flow=`technical_failed`、Worker=`faulted`。只有对象和回执已经唯一证明、但微信/Provider 明确内容处理失败时，才沿用 customer 媒体失败 Handoff。
- 必须证明跨 64 分桶边界及多像素轻微抖动不再阻止发送，同时真实新增、缺失、替换、换序和角色/类型/正文/媒体状态变化仍阻止旧回复；两条相同时长语音和两张相似图片分别只动作一次并绑定不同正式身份；C4 复用同一 C2/C3 链路。
- 自审追加了不直接注入“转写成功”的生产 Sidecar 动作门测试：真实调用 `prepare_voice_action_payload -> execute_voice_action_payload -> ActionJournal`，Windows 截图/OCR/鼠标只使用受控替身，两条 3 秒语音各点击一次、各形成独立动作结果。该测试与 Worker 两语音串联测试联合执行时，曾真实暴露“第一条语音结算后，下一帧恢复漏传同一 `origin_read_run_id`”的 `C2_CONTINUITY_MAPPING_INVALID`。生产代码已仅在语音 prepare 复读和语音动作结果复读两个同 Flow 入口补传现有 Flow 编号，未新增连续性规则，也未改 Sidecar、后端或媒体顺序。移除该接线时串联测试会立即失败。
- 冻结前本地实现和历次 SHA 均未完整覆盖最终图片动作槽位、正常视口滑动及技术故障终态，不得作为合规候选。客户端、后端、ActionJournal、机器合同与生成 Schema 已按本节形成本地实现并通过定向验证，当前规范化 SHA 为 `7b951e1433d61266b3fd038c0705cf8ecb53d6e659579278d4bb019f27191bc4`；现交架构复审，复审通过后才可形成真实 OmniAuto/车金候选提交。

### 2.0.42 2026-08-27：Windows 门禁测试夹具修正升为 0.9.43

- `0.9.42` Windows 门禁执行 1073 项，仅失败 1 项：生产 `open_chat()` 正确收到 `sidecar_run_id` 与 `allow_merged_remark_search`，旧测试仍断言旧参数列表。
- 本轮只更新该测试的正式参数期望；不修改 Sidecar、Worker、后端、Brain、OCR 判断、点击、Enter、状态机或业务流程。
- 因 `gray-v0.9.42` 已推送为不可变标签且未生成 ZIP，修正后的候选必须顺延为 `0.9.43`，不得覆盖旧标签。
- 当前合同为 `0.9.43 / a87275e55d6f25aeba3185d854f4e613a9209924ba4ac5ac4f6f49a3aeb00cef`；真实 OmniAuto 来源提交为 `27c59c8a0e9c85106a12f05f6f92e0193fefb5af`。

### 2.0.41 2026-08-27：同帧 OCR 完整回退与非首屏定位复用升为 0.9.42

- 性能优化候选曾只在 `pre_send_refresh` 对标题 ROI 失败执行同帧整窗 OCR；真实发送 S0/S1/S2 在标题成功但消息数量、角色、正文、顺序或发送回执漏识别时会直接停止正常回复。`0.9.42` 将这些证据要求统一接入一个发送帧构建入口：先 ROI，任一不足只对同一张内存截图补一次整窗 OCR；整窗仍不足时，S0/S1 零 Enter 停止，S2 不得再次按 Enter并记录发送结果 unknown。
- S0、S1、S2 继续是三个不同时间点的真实截图，不增加截图、不复用跨时间画面、不放宽上下文比较、不改变 ActionJournal、Enter、sent_ack 或发送状态机。
- 非首屏定位合并为同一次 Sidecar 事务：首屏未命中后使用已取得画面建立搜索入口，搜索结果只用侧栏 ROI；登录、遮挡和中央弹窗仍用整窗安全证据，最终 private、短码、标题确认保持不变。
- 三个正式开关为唯一运行决策；全部关闭时恢复原完整路径。不得保留默认开启的内部第二套开关。
- 独立 OmniAuto 的 Sidecar 直接依赖 `window_layout.input_text_detection_bounds`，该函数必须与本轮来源提交一并存在；禁止继续同步无关依赖形成未经评审的整体升级。
- 当前合同为 `0.9.42 / 9dcab8759b3f2a0611027cccda0044cd24e182e50418c33b88d997007a5c4305`，真实 OmniAuto 来源提交为 `307241810963c2e649ba04483a898687d06ba9f4`。代码架构复审已经通过；本地定向测试仍不代表 Windows 实机耗时或整体 UAT。

### 2.0.40 2026-08-27：DeepSeek Brain 固定路由与共享预算升为 0.9.41

- 正式后端固定使用 DeepSeek，禁用跨供应商 fallback；一次 Brain 回复的生成、备用线路、同帧重试、JSON 修复、质量返修和语义审核共用一份总预算，并保留无密钥逐阶段诊断。
- 已形成提交 `f9b16c3` 和标签 `gray-v0.9.41`，后续候选不得同名覆盖。

### 2.0.39 2026-08-26：Windows 发布门禁收口升为 0.9.40

- 在 `0.9.39` 功能基础上只同步版本、合同、生成 Schema、CI/打包入口和 Windows 门禁，形成提交 `63aec67` 与标签 `gray-v0.9.40`；不得用后续不同代码覆盖。

### 2.0.38 2026-08-26：同帧语音物理行归并升为 0.9.39

- Sidecar 只按同一发送人、纵向真实相交的物理消息行归并 OCR 与图形语音观察；相邻行即使只隔 1px 也保持两条，角色、时长、父子或转写状态冲突时零点击报错。Worker 不再按锚点类型二次归并。
- 同时纳入 PR #11 的低权威快速短句购车意图判断。独立 OmniAuto 来源固定为 `88a10cb6160e3552ef6abd02b3fb4d517cbfcab9`，车金提交为 `5e267a9`，标签为 `gray-v0.9.39`。

### 2.0.37 2026-08-26：完整画面证据与增量入库闭环升为 0.9.38

- 第二轮会话现场出现“当前画面 5 条、只新增 2 条”，旧 Worker 在过滤已确认历史消息时同时删除了完整画面 observations，导致后端冻结的 checkpoint 只有 2 条，发送前与当前 5 条画面必然对齐失败。
- 正确职责固定为：`messages` 只携带本轮未结算事实，避免重复入库；`evidence.observations` 与 `slot_ledger_states` 必须保留同一张完整权威画面，供后端冻结 checkpoint 和 Worker 发送前完整 N 对 N 比较。严禁通过放宽比较或补数据库整段历史解决。
- 大载荷拆包时，非末尾分片可只运输本片 observation，但最后一片必须携带完整权威 observations；若完整证据本身超过运输上限，Worker 在发送任何分片前明确失败，后端零入库、零残缺 checkpoint。
- 后端对不随 `messages` 重复提交的 historical observation 必须查询正式 `MessageEvent` 校验会话、source key、角色、类型和正文，伪造历史事实返回 409。
- `fact_scope=unknown` 只有被明确身份异常 flow gate 覆盖时才能进入既有恢复/转人工流程；缺门禁或错误使用 `C2_MESSAGE_HISTORY_GAP` 均返回 409，不能在合同校验前静默丢失，也不能借普通历史缺口绕过身份门禁。
- 本轮不修改 Sidecar OCR、鼠标、语音/图片动作、Brain、回复作废、媒体处理顺序、UI 锁或 C0—C4 状态机。正常第二轮保持“完整 5 条证据、只入库新增 2 条、发送前 5 对 5”。
- 架构复审确认无功能性 P0/P1；关键验证经过生产 Worker 过滤/拆包、真实 SQLite、正式 HTTP 路由、后端数据库、checkpoint 生成和 Worker 比较函数，不是伪造最终成功。后端 C2 路由 `183 passed + 1 skipped`、Worker 定向 `36 passed`、C3 checkpoint/预发送 `16 passed`，编译和 `git diff --check` 通过。
- `gray-v0.9.37` 与其 ZIP 不得覆盖。本轮合同为 `0.9.38 / 6e1b0ab219c7effdb380170c452cd0e18c42522cd95870f24034af69c6b51143`；OmniAuto 生成 Schema 来源提交为 `715450bc55117cb5ac7c3fc4f574f4721c00b538`。Windows 整体 UAT 和配套后端部署仍按发布流程执行。

### 2.0.36 2026-08-25：发送前不可变事实 checkpoint 方案升为 0.9.37

- 现场复现确认：已成功转写并提交的语音或图片处于聊天末尾时，旧 `pre_send_refresh` 仍按跨轮弱媒体身份规则要求“前后双锚点”，因天然没有后侧消息而产生多个解释，错误拒绝原回复；在尾部补一条文字后立即恢复，证明故障位于发送前身份复判而不是 Brain、OCR、入库或媒体动作。
- 根因是职责混用：已提交消息的长期身份本应已经确定，发送前却再次调用历史身份对齐算法决定“它还是不是原消息”。`0.9.37` 禁止重判或改写历史 `worker_stable_id`，改为比较本 reply_action 实际使用的不可变 `pre_send_fact_checkpoint` 与当前最新完整尾部。
- 比较结果固定且 MECE：完全相等为 `checkpoint_equal`，允许原回复继续；checkpoint 是当前序列唯一完整前缀且有非空后缀时，原子作废旧回复并只处理新后缀；缺行、替换、滚动截断、unknown 或零个/多个前缀解释时禁止发送，只允许一次完整被动重读，仍不唯一返回具体序列错误。
- 该方案不是“末尾语音/图片例外”。普通 `authorized_read`、媒体动作正式提交、历史恢复和跨轮弱媒体身份仍执行原强证据规则；禁止全局放宽 `_compatible()`、按“最后一条”继承身份或新增等价特殊分支。
- 权限边界固定为：Sidecar 只返回有序观察、完整尾部/截断和布局证据；Worker 独占 checkpoint 比较和发送门决策；后端只保存正式事实、幂等 supersede 和批次结算，不根据截图重新猜身份。
- checkpoint 的唯一来源固定为后端按本次 Brain 实际使用的有序正式事实写入 `MessageBatch.ai_request_snapshot` 的不可变快照；Worker 只读接收并持久化副本，重启时只按同一 batch 取回，禁止从当前截图或最新 identity checkpoint 重建。该对象只加入现有 batch/reply 响应，不进入 Sidecar 或 `messages/ingest` 请求，也不新增数据库同义列。
- 后端同时接受未被媒体 UI 动作失效的合法 `initial_read` 和动作后的 `final_read`，并通过帧内 Ledger source key/正式稳定 ID 唯一投影 Brain 使用事实；严禁投影失败时退回整段历史。`friend_welcome` 使用显式完整空基线，当前一旦出现客户消息即作废欢迎语。
- 真实 Win32 无原生 ID、已提交末尾媒体无后侧邻居且前后画面证据完全相同时，系统无法证明物理上是否仍为原消息，不得伪造该身份结论。冻结方案改为区分“物理身份”与“回复依赖的业务事实”：只有正式 action receipt、完整未截断序列、唯一前缀及精确回复事实全部一致时，允许 `terminal_committed_fact_equivalence + physical_identity_confirmed=false` 通过本 reply_action 发送门；不继承旧 ID，不进入 Ledger/Outbox/ingest，不放宽普通身份门禁。
- 提交门禁必须真实经过正式 `pre_send_refresh`、持久化 checkpoint、reply_action 与后端数据库，覆盖末尾语音、末尾图片、完全相等、追加重复内容、同位置替换、截断/多解释和普通读取不得被放宽等正反场景；禁止只测内部比较函数或伪造最终成功。

- **实现状态更新：** `terminal_committed_fact_equivalence`、checkpoint revision 3、不继承旧身份门禁及语音/图片的真实后端到 Worker 正反组合测试已补齐，guard 与 observations 同帧绑定和被动重读布局复查也已关闭；合同为 `0.9.37 / 3157d37b8047ef3b39c53d4eab323e87ff7568c442372b08afc22cb1e2c9b9dc`。OmniAuto 功能提交固定为 `1a541c9eb330e83077c7bdffa0bb003a1c47d525`，车金提交固定为 `a3c7d86`，标签 `gray-v0.9.37` 和 GitHub Windows Fast UAT ZIP 已形成；后续现场暴露完整画面证据被增量过滤的问题，因此该 ZIP 仅作为 `0.9.38` 回退与故障复现基线，不得覆盖。
- **布局门禁整改：** 发送前事实比较和 claim-send 前两处生产检查均必须验证 `send_context_guard.ok=true`及布局快照、消息数量、顺序、摘要和末项一致性；Sidecar 与 Worker 现共用唯一纯投影规则，Worker 必须从同一响应的 observations 复算并逐项匹配 guard，禁止“合法旧 guard + 合法新 observations”跨帧拼接；唯一一次被动重读后必须再次执行完整布局检查。无效时直接按 `C2_PRE_SEND_LAYOUT_INVALID` 技术故障结算，不消耗重读次数，不转人工且零微信动作。普通回复、末尾媒体、空欢迎语、跨帧混配和重读布局失效反例均已通过生产 `build_send_context_guard()` 固化。

### 2.0.35 2026-08-25：Brain 强制超时分阶段证据升为 0.9.36

- 现场证据确认：第一次 Brain 生成在隔离子进程的 `180118 ms` 硬超时点被父进程终止；5 秒后第二次总耗时约 `62.7 秒`并成功。旧实现将子进程 stdout/stderr 只保留在内存，硬超时终止后丢弃内部进度，因此旧事故无法继续确认是主模型、备用模型还是语义审核卡住。
- `0.9.36` 为主模型、备用模型、同帧重试、JSON 修复、质量返修和语义审核记录开始/结束事件；父进程在正常完成、子进程异常和硬超时强杀时都会读取已形成的事件和最后阶段。
- 进度事件只允许固定白名单字段和本次 `progress_id`；禁止落盘提示词、回复正文、API Key 或服务地址。诊断写入失败不得改变 Brain 业务结果。
- 证据通过正式隔离子进程、父进程解析器和 MessageBatch `generation_attempt_history` 落库；成功和失败均可回溯，不依赖子进程正常退出。
- Brain 运行期间新消息使旧批次 `superseded` 时，旧回复仍严格丢弃，但硬超时或已完成调用的诊断进度会在 stale 返回前幂等追加到旧批次历史；不改写 `superseded` 状态、错误码和当前快照投影。诊断文件仅执行 `flush`，不再逐条 `fsync` 阻塞 Brain。
- 本轮不修改 Brain 生成、备用模型、语义审核、Guard、重试次数、超时时间或 C0—C4 状态机。合同已固定为 `0.9.36 / 8154dafe…8f377f`；OmniAuto 真实功能提交为 `d3e5993045226f68481a09a39d6bc4b38595d483`，车金冻结提交为 `061b641`、标签为 `gray-v0.9.36`，来源绑定和灰度分支推送均已完成。

### 2.0.34 2026-08-25：Windows 完整发布门禁收口升为 0.9.35

- `gray-v0.9.34` 的 GitHub Windows 完整门禁共执行 1027 项测试，结果为 `7 failures + 5 errors`；因此旧标签和旧 ZIP 只能作为失败证据，不得继续作为发布候选。
- CI/测试基础设施修正包括：未安装 `pytest` 时跳过仅依赖外部截图的可选证据模块；测试 Journal 使用 Windows 合法文件名；临时 SQLite 删除前显式停止事故上报线程；遥测 SQLite 连接离开事务后强制关闭。
- 受影响夹具统一使用真实生产形状：显式声明 `source_adapter=win32_ocr`，补齐本帧动作绑定、当前允许的新图片集合和已提交历史媒体范围，并严格分开 Sidecar 原始观察与 Worker 增强身份。
- 错误码职责不变：Sidecar 原始返回泄漏 Worker 身份字段时仍返回 `C2_SIDECAR_IDENTITY_CONTRACT_INVALID`；Sidecar 合法、但 Worker 图片身份对象非法时仍返回 `C2_IMAGE_IDENTITY_CONTRACT_INVALID`。测试不得跨层注入字段制造不真实合同冲突。
- 本轮不修改 C0—C4、媒体动作顺序、Brain/Guard、Handoff、UI 锁、后端接口或发送回执流程；真实功能提交已固定为 `8474780697a8566468ba33472f01295327e7c751` 并完成来源绑定，来源治理提交已固定为 `f6ddc1fc049287ba61906a8d9fd1c1c88da27b2f`；推送、标签、ZIP、GitHub Windows 完整门禁和 Windows 实机 UAT 均未完成。

### 2.0.33 2026-08-25：Sidecar 身份输出边界修正升为 0.9.34

- 现场 `C2_SIDECAR_IDENTITY_CONTRACT_INVALID` 的根因是 Sidecar 新版 `observations` 已遵守权限边界，但同一响应中为旧入口保留的 `messages[].message_envelope` 仍带有 `source_message_key` 等 Worker 专属身份字段，Worker 递归合同校验因此在入库前正确拒绝整个返回。
- 修复只在 Sidecar 公开输出边界递归删除 `same_business_message`、`worker_stable_id`、`source_message_key` 和 `commit_basis` 四个禁止字段；保留合法的 `reserved_worker_stable_id` 及原始观察、动作回执和错误证据。
- 普通 CLI、常驻 daemon、冻结 Windows 客户端入口和旧 `main()` 兼容入口分别调用真实生产输出链进行永久回归，禁止仅用单个帮助函数自证通过。
- 本轮不修改消息选择、语音/图片操作、长期身份提交、Outbox、Brain、Handoff、发送或后端接口；机器合同仅升版并实算 SHA。
- 真实 OmniAuto 功能提交已固定为 `11d43d5bb9dd83831e0bbba8ed84b5eba700cb2c`，`.chejin-source.json` 已完成绑定；尚未形成来源治理提交、推送、标签或 ZIP。不得使用 `0.9.33` 标签或旧 ZIP 验证本次修复。

### 2.0.32 2026-08-24：本帧动作绑定与长期身份权限边界升为 0.9.33

- `0.9.32` 复审确认：为防固定容量替换而新增的连续性门禁，把跨轮长期身份算法直接作为首次媒体点击条件；真实 Win32 OCR 不提供原生消息 ID，新语音/图片在第一次动作前也不可能已有本次 confirmed action，导致正常未变化主链同样返回 ambiguous 并零点击。
- 根因不是单一判断条件，而是方案未明确区分 `frame_action_binding` 与 `committed_message_identity`：Sidecar 曾根据角色、类型、时长和位置越权判断“是不是同一条消息”；收权后又反向要求 Worker 在动作前取得只有动作后才可能形成的长期身份证明，形成循环依赖。
- `0.9.33` 固定三层权限：Sidecar 只产生本帧观察、一次性 token、局部目标证据和动作回执，可以拒绝不安全点击但不得生成消息身份；Worker 唯一决定顺序、连续性、动作准入及 `worker_stable_id` 提交；后端只校验、保存、去重和结算 Worker 已提交身份，不根据截图重新猜测。
- 首次语音/图片动作明确支持 `source_adapter=win32_ocr + native_source_message_id为空`。动作前只建立不可持久消费的操作票和预留号；本帧目标唯一且相关序列未变化时允许一次动作，动作回执有效后才由 Worker 经唯一提交门形成长期身份。
- 相关消息序列变化、目标消失或多候选时，Sidecar 零点击返回原始证据，Worker按 operation phase 重新仲裁或返回具体错误。固定容量替换仍必须零点击；本次修正不恢复坐标、时长、正文或弱摘要作为长期身份。
- 新语音/图片不批量预留：所有未选媒体保持 `frame_local_unselected`，只有 Worker 在最新帧当前唯一选中的一条才创建 action ID、预留号和 ActionJournal；本条终结后从新帧重新选择下一条。
- 媒体 UI 动作顺序固定为语音优先、图片随后；图片期间出现新语音时，当前图片终态结算后先回到语音阶段。最终入库仍按最后权威完整画面的 `screen_order`，不按 UI 动作先后排序。
- 验收必须使用真实 Win32 observation 形状且不注入原生 ID，分别证明正常尾部新语音执行一次、正常新图片执行一次、同秒数替换/相似图片替换零点击、媒体期间新增文字进入最新序列；正反用例必须调用同一生产入口和真实 SQLite/Journal/Outbox，禁止省略 `source_adapter` 使模拟 ID 冒充原生 ID。
- 代码层架构复审已通过；真实功能提交固定于 `61606d645de2c575ad3113de577e0f90d87a41f0`，机器合同、生成 Schema 和 manifest 已统一为 `0.9.33 / 44fd0532…b2b8e`。当前只完成来源记录更新，来源治理提交、推送、标签、ZIP/EXE 和 Windows UAT 尚未完成。

### 2.0.31 2026-08-23：发送前文字/语音/图片统一仲裁目标升为 0.9.32

- 现场确认 `pre_send_refresh` 遇到并发新语音时，单条语音目标指纹错误包含整张微信画面，无关区域变化导致四次取消和约 107 秒耗时；暂时错误又被统一改写为 `C2_REPLY_CONTEXT_RECOVERY_FAILED` 并立即 handoff，迟到语音虽入库但因开放 handoff 不再进入 Brain。
- 根因是方案内部冲突与代码边界过重：通用 L2 规定技术不确定先有限恢复，发送前专章却规定任何失败立即 handoff；同时测试绕过生产读取入口，只验证了伪造结果。
- `0.9.32` 不修语音特例：文字、语音、图片和混合到达共用完整消息序列对齐和旧回复发送门。媒体每次只操作一条，每次动作后重读完整视口并吸收新到达对象。
- 整张微信画面或完整消息视口摘要只能用作相关区域变化检测，禁止作为单条消息的身份/稳定性指纹。`message_viewport_change_digest` 必须基于生产解析器的有序规范化 observation 列表，禁止直接哈希原始 RGB 像素；输入框光标、工具栏、侧栏、其他会话、GIF/动画帧、滚动条、悬停/播放效果和红点必须排除。
- 当前会话新消息使气泡移位时，必须先取消旧候选与原预留号，然后只对一张最新不可变帧完整重识别一次。成功则以新位置继续；失败必须返回目标消失、多候选、序列、角色、正文、布局或再次变化的具体错误。
- `pre_send_refresh` 不创建 `recoverable_hold`，不执行两次/120 秒或四次 prepare 时间重试，不将具体文字/语音/图片错误包装为 `C2_REPLY_CONTEXT_RECOVERY_FAILED`。具体消息错误对当前会话幂等 handoff 一次；布局无效不属于客户业务异常，固定按客户端代码 Bug 收口。
- `C2_PRE_SEND_LAYOUT_INVALID` 必须禁止旧回复、保存错误码与完整画面/OCR/布局证据、将当前任务/Flow 结算为 `technical_failed`、释放 UI 锁，并将 Worker 置为 `faulted + can_pull_tasks=false`。不得创建 HandoffEvent/飞书通知，不得自动移窗、重标定或重试，也不设计人工解锁、清数据或旧 Flow 恢复分支；修复代码后由新客户端版本正常启动。
- system 行已唯一但正文不可读固定为 `C2_PRE_SEND_SYSTEM_CONTENT_UNREADABLE`；正文可读但不能归类固定为 `C2_PRE_SEND_SYSTEM_CLASSIFICATION_UNRESOLVED`。工程师不得自创或借用文字/角色错误码。
- 已触发或可能已触发的媒体 UI 动作仍使用 ActionJournal 事务恢复且绝不重复点击；这是动作结果恢复，不得与动作前“相关消息区变化”混为同一等待状态。
- 合法开放 handoff 后迟到消息不得自动重启 Brain；一次重识别成功时必须作废旧回复并以最新尾部重建 batch。
- 核心组合验收必须调用生产 TaskRunner、真实 SQLite/Journal/Outbox、正式 HTTP 路由和后端数据库；禁止 mock `_read_one_wechat_target`、媒体编排器、ingest/batch/fail_task 伪造最终成功。Windows 边界可控替身只能返回原始帧/OCR items/动作回执。

### 2.0.30 2026-08-23：旧格式媒体恢复有限出口目标升为 0.9.31

- 现场旧 SQLite 证明 `0.9.30` 仍会将“历史记录缺少新版 `worker-message-N`”解释为当前合同错误，进入全局暂停后每个心跳重新检查，没有迁移、handoff、技术终态或人工处理出口。该缺陷定性为技术方案的跨版本状态遗漏，`0.9.30` 发布批准撤回。
- `0.9.31` 必须在当前合同状态机之前增加唯一 `legacy_media_recovery` 分类门：旧记录只能进入明确未触发取消、可证原身份迁移、唯一客户幂等 handoff，或 Worker 级待审核事故四个有限出口。任何出口均不得猜测序号、生成伪消息、重复点击、调用 Brain 或继续每 4 秒重新分类。
- 分类结果和 `legacy_record_digest` 必须先持久化；后端临时不可用时只重传同一决定。后端幂等确认后归档旧记录并清除旧 Flow，自动恢复 `/tasks/pull`。只有发送结果可能已发生但不可确认继续使用原硬门禁。
- 结算异常已分类：连接失败、超时、HTTP 5xx 或明确 `retryable=true` 才保持原运行状态并退避重试；HTTP 4xx、幂等冲突或确认内容矛盾会持久化 `manual_review_required`，停止自动重试并明确暂停。`LEGACY_MEDIA_OWNER_UNKNOWN` 使用独立失败审计事件，后台显示“旧媒体归属待人工检查”，不再伪装为普通成功。
- 验收必须直接使用上一发布灰度版本产生的原始 SQLite/ActionJournal，不得用测试帮助函数重造成当前格式。必须证明零重复 UI/Vision、零伪消息、幂等终结、旧 Flow 清除和继续 `/tasks/pull`。
- 本地实现已用 `0.9.30` 旧代码实际生成且未改写的 SQLite、现场原 ActionJournal 副本及旧版本实际生成的语音/图片 Journal 走生产 `tick_once()` 验证。客户端重启/legacy 分支 `45 passed + 7 subtests`，存储反向门禁 `4 passed`，客户端 HTTP 请求合同 `4 passed`，后端正式路由与真实数据库 `5 passed`，版本/合同/打包元数据 `59 passed + 33 subtests`。这些证据不代替 Windows 原故障库重启验收。

### 2.0.29 2026-08-22：重启在途 Flow 全状态对账修复升为 0.9.30

- `0.9.29` 只覆盖了“本地和后端仍保存同一旧 Flow、且本地已有结束凭证”的恢复分支。Windows 现场为“本地保留旧 `c2_read` Flow、后端权威 `inflight_flow_state` 已空”；客户端因此既不能续行旧 Flow，又被本地旧 ID 禁止拉新任务，持续 `running/idle` 心跳但零 `/tasks/pull`。
- `0.9.30` 在首次心跳后对账启动时捕获的本地 Flow 与后端权威状态。仅本地存在的旧 `c2_read` 必须先恢复物理 ActionJournal，并确认同 Flow 的 Outbox、C2 ActionJournal、Ledger、物理 Journal 和 sent_ack 均无待结算项，随后只清除本地过期 Flow 并继续拉单；不得调用不存在的后端 Flow finish，也不得丢弃本地事实。
- 本地与后端同 ID 时继续使用现有 finish 校验；缺少结束凭证但存在唯一持久化客户归属时，按已有 durable owner 重建结束凭证。后端单边存在、两边 ID 不同、后端状态非法、本地任务 Flow 单边存在、同一 Flow 关联多个客户或缺少可验证归属时，统一明确暂停并记录结构化错误，禁止猜测、覆盖或继续假在线。
- 启动对账只处理进程启动前已持久化的旧 Flow；当前进程正常新建的 Flow 仍由原业务链拥有，恢复器不得接管。后端 HTTP 字段、Brain、MessageBatch、回复作废、发送前复读、UI 锁和 C0—C4 状态机不变。
- 恢复多条媒体时只按 `worker-message-N` 数字 N 统一排序；两条语音以及语音+图片即使 Journal 文件名顺序相反，仍以同一有序载荷入库。序号缺失、重复或冲突时明确暂停，禁止按文件名、创建时间或字符串猜测。
- 恢复授权的连接失败、超时等临时网络错误保留旧 Flow 和原接单状态并自动重试；身份冲突、合同矛盾或证据不足才显式暂停。
- 定向测试经过生产 `tick_once()`、真实 SQLite、真实 ActionJournal、Worker Outbox 和后端正式 API/数据库：全部 restart 分支 `28 passed`，四个核心反向场景 `4 passed`，后端事实结算 `3 passed`，合同 `21 passed + 33 subtests`，Python 编译、JSON 和 `git diff --check` 通过。Windows 原故障 SQLite 和物理微信仍由实机 UAT 验收。
- 功能候选固定为 `5c94a91cd956f37776be73f872b4c34ccaccd810`；后端 HTTP 字段、Brain、MessageBatch、发送前复读、UI 锁和 C0—C4 状态机未修改。

### 2.0.28 2026-08-22：重启旧 Flow 媒体 Journal 恢复接线升为 0.9.29

- 根因定性为客户端代码实现遗漏：技术方案已定义有限恢复、隔离当前客户和结束旧流程的出口，但重启链只执行了事务屏障检查与尝试结束，没有在此前调用完整的语音/图片 ActionJournal 恢复，图片 Journal 又会反过来阻断普通屏障，形成永久“假在线但不拉单”。
- 客户端现在于心跳后、普通新工作屏障前处理重启旧 Flow。纯未点击 Journal 按取消结算；已形成的图片事实使用现有轻量授权和事实结算端点无界面上报；无法确认的语音/图片动作在后端门禁确认后进入有限隔离终态，保留不重复执行的审计 Journal，然后结束旧 Flow 并继续拉取其他任务。
- 重启恢复路径只读本地 Journal/SQLite 与现有后端授权/结算结果，明确断言不定位会话、不读取微信、不重复语音转写、不重复图片 Vision、不发送消息。未得到后端确认时继续保留旧 Flow，不会伪造恢复成功。
- 定向测试经过生产心跳入口、真实本地 ActionJournal 文件和 SQLite：恢复链 `25 passed`，版本/合同/打包 `59 passed + 33 subtests`，后端正式合同链 `3 passed`，Python 编译、生成 Schema current 检查和 `git diff --check` 通过。Windows 截图、OCR 和物理鼠标仍属实机 UAT，不由本地测试伪装为已验收。
- 功能候选固定为 `abc28876354761dc038ee4e99fa4f95e85ab342b`；后端 HTTP 字段、Brain、MessageBatch、发送前复读、UI 锁、暂停和 C0—C4 状态机未修改。

### 2.0.27 2026-08-22：同一会话事务多次入库幂等修复目标升为 0.9.28

- 根因确认：当前实现直接用外层 `read_run_id` 生成消息 Outbox ID；同一会话 Flow 在首次读取入库后，发送前复读若发现新的客户语音、图片或文字，仍沿用该 `read_run_id`，导致第二组不同事实命中已确认旧 Outbox。旧不可变载荷被保留，新事实没有真正上报，旧回复存在被错误继续使用的风险。
- 本版采用最小边界修复，不拆分现有 Flow/read_run，不改变 HTTP 请求/响应、后端 Schema、UI 锁、暂停语义、授权、媒体编排、Brain、回复作废或发送状态机。Worker 本地新增 `outbox_batch_key`，只表示一组不可变待投递事实；相同事实重试复用同一本地 ID，不同 `source_message_key` 集合必须得到不同本地 ID，该键绝不发送给后端。
- 新消息 Outbox 主键改为 `c2-outbox:{read_run_id}:batch-{outbox_batch_key}`；拆包子项只追加 `part-{part_index}`，后端分片 `group_id` 仍为 `read_run_id`。发送前新增事实先以新本地 ID 可靠落盘，再按原合同上报；后端原事务继续负责入库新消息、作废旧 `reply_action` 并创建或复用新 MessageBatch。未确认时禁止发送旧回复。
- 实现已在 Worker `storage.py` 收口为唯一公共 Outbox ID 生成函数，enqueue、恢复、授权刷新、运输准备和拆包均使用同一确定性身份及不可变事实摘要。相同 source key 但正文、角色、类型、状态或稳定媒体结果不一致时本地拒绝覆盖且不调用后端。
- 测试走正式 Worker 构建器、真实 SQLite Outbox、正式 FastAPI 路由、真实后端服务和数据库，证明同一 read_run 的新语音使用新 Outbox 入库并作废旧 MessageBatch；两条 SQLite 反向测试证明授权刷新和运输准备不能把正文 A 改成 B。功能候选固定于 `f66a57d51583d8225d32e9bc2cdb881798e3af49`，Windows 物理边界仍需实机 UAT。

### 2.0.26 2026-08-22：Windows 门禁测试确定性修复升为 0.9.27

- `gray-v0.9.26` 推送后，Windows `run_checks.py` 在搜索框绿色焦点边框用例失败，ZIP 未生成。根因是测试创建截图后未显式绑定生产布局快照，过去偶尔依赖 Python 对象 ID 复用旧缓存通过；这是测试质量问题，不是微信搜索、语音或图片生产流程回归。
- 测试现使用与其他兼容用例相同的 `_register_compat_image_layout`，分别将有焦点和无焦点截图绑定到真实生产布局快照，再调用生产焦点判断函数；本地完整 Win32/OCR 兼容检查 `192/192` 通过。
- 因 `gray-v0.9.26` 已推送为不可变标签，即使没有生成 ZIP 也不移动标签；客户端、合同、Schema 和 Windows 工作流顺延为 `0.9.27`。合同 SHA 为 `ceac5e078db5516d173f33eb1af3c461b07251e667f3a0be0524268771ed2bd6`，功能候选固定为 `b12591b54630ed3949a997cfece3f8c6fe164700`。

### 2.0.25 2026-08-22：C2 媒体链路与后台状态修复升为 0.9.26

- 语音菜单 HWND 查询异常不再被猜成“已关闭”，而是明确返回 `popup_window_state_unknown`；点击校验成功或失败统一进入同一套最多 24 帧或 120 秒的被动正文等待，全程不重复点击。唯一正文通过正式 action binding 入库，达到上限仍无正文才隔离。
- 图片复制动作在剪贴板确认证据形成前持续保留“菜单可能仍打开”的状态；无进展、异常和重试出口都会安全关闭菜单，旧剪贴板内容不能形成新图片事实。
- Worker 语音、图片阶段按真实失败码上报；后端拒绝“成功但带错误码”和“失败但无错误码”，并允许迟到失败纠正同一阶段先前的成功投影。历史错误记录不会自动重写，仍需按现场证据单独重放或修正。
- 客户端、Sidecar、合同、生成 Schema 和 Windows 工作流统一升为 `0.9.26`；规范化合同 SHA 为 `1c939d25ea1e1ec22eb5dac985eb02632b9ed2bf5fed8df2a65a8791ef1fe486`，功能候选固定为 `780f3c00ee53a3c3fce26be21053fa3515a08b81`。
- 架构复审确认没有新的功能性 P0/P1。定向复跑：媒体及状态链 `239 passed + 36 subtests`，后台 `15 passed`，版本/合同/打包入口 `27 passed + 33 subtests`；自动测试中的 Windows 截图、OCR 和鼠标仍为受控替身，不能代替实机语音 UAT。
- 旧 `gray-v0.9.25` 标签和 `chejin-worker-fast-uat-v0.9.25-65dec89b1353.zip` 永久保留，禁止移动标签或覆盖旧 ZIP。只有新的 `0.9.26` Fast UAT ZIP 可用于本轮实机验证。

### 2.0.24 2026-08-21：C3 输入工具栏误判修复升为 0.9.25

- Windows UAT 的 `WECHAT_INPUT_DRAFT_PRESENT` 不是输入框真实草稿，而是动态 `input_bounds` 同时覆盖文字编辑区与底部工具栏，工具栏图形像素被草稿安全门禁误判；物理点击和键盘发送均未发生，因此没有错误外发。
- `0.9.25` 保留完整 `input_bounds` 作为输入框物理点击面，只将草稿 OCR/像素检测改为按当前标定区域比例内缩、明确排除底部工具栏的文字 ROI。工具栏图形不再证明有草稿，文字 ROI 内的短真实草稿仍会阻止自动发送。
- 机器合同新增点击面与文字检测面分离规则，revision、Worker、Sidecar 标定 Schema、生成 Schema、Windows Fast UAT/正式工作流和测试统一升为 `0.9.25`；规范化合同 SHA 为 `53e2ed19dbf4ee62677660689a7385066a7e660d5d70c74a0254b1e94d753bc7`。
- 功能候选固定为 `15db96575a23f57beb9bab54be6a21e5de0ac748`。本地复跑发送安全 `63/63`、Win32/OCR `192/192`、交互证据 `6/6`、启动标定 50 项、合同/打包 `58/58`、UAT 证据与后端合同边界各 `1/1`，并使用 5 张随机尺寸真实微信截图辅助验证空输入允许、文字 ROI 内真草稿阻止。该截图回放不是 Windows 物理发送 UAT。
- 旧 `gray-v0.9.24` 标签和 `chejin-worker-fast-uat-v0.9.24-f5a6417460eb.zip` 永久保留为旧候选，禁止移动标签、覆盖 ZIP 或用于验证本次修复。只有新 `0.9.25` Fast UAT ZIP 可进入下一次 Windows 实际发送验收。

### 2.0.19 2026-08-21：0.9.23 启动一次布局标定方案收口

- 确认原固定坐标来自真实微信截图的 OCR/图像定位，不是任意手工假设；兼容性整改因此改为每次客户端启动后对当前真实微信客户区重做一次全局标定。
- 微信窗口按 DPI/可用工作区选择唯一默认外框档位：100% 约 `800×852`、125% 约 `1000×1065`、150% 约 `1200×1278`；放不下时宽高必须等比例缩小，不得单边裁剪使布局变形。实际客户区与截图是权威事实，不再所有机器统一放大到 `980×860`。
- 启动标定由同一张客户区原图几何和同尺寸增强 OCR 共同生成导航栏、侧栏、顶部操作行、会话列表、标题、消息视口、工具栏和输入区；C0—C4 共用该区域地图。
- 原 `0.9.20` 坐标按各自所属区域分段映射；“+”由侧栏边界+搜索操作行映射，不强求 OCR 直接识别符号，点击后仍由原菜单确认拦截。
- 会话行、消息、弹窗和表单仍只使用 `0.9.20` 原流程的当前必要业务帧；不新增截图/OCR，不改点击顺序、重试、失败处理、状态机和媒体编排；同时保留已批准的 C1 八类画面节点复用优化。
- 会话列表重排、红点、新消息或聊天滚动不使全局标定失效；只有 HWND/进程/客户区/DPI/微信主外壳真实改变时才重新标定。
- 前台语义继续使用 `0.9.20` 的动作分级：`status/capabilities/calibration-status/sessions` 被动探测不抢前台；`add_friend/open-chat/messages/voice-transcribe/recover-render/send` 在首个物理动作前只激活并复核已标定的唯一微信主 HWND。激活不等于窗口规范化，禁止借机移动、缩放、重新标定或增加截图/OCR；有限激活失败必须零 UI 操作返回技术失败。
- 版本整体升为 `0.9.23`；当前不伪填合同 SHA、不伪填 OmniAuto 来源提交、不声称代码已实现或 Windows 已验收。

### 2.0.20 2026-08-21：0.9.23 前台激活、Sidecar 启动与窗口边距复审通过

- C1/C2/C3/C4 共用的真实入口会读取启动标定的唯一微信 HWND，按“其他窗口 → 有限激活微信 → 复核微信前台 → 原业务分发”执行；激活失败在业务函数前返回 `WECHAT_WINDOW_NOT_READY`，不执行鼠标、键盘、剪贴板、截图、OCR、移窗或重新标定。
- `status/capabilities/calibration-status/sessions` 保持被动且不抢前台；`add_friend/open-chat/messages/voice-transcribe/recover-render/send` 保持主动业务模式，不改 C0—C4 状态机、点击顺序和失败处理。
- 冻结安装包的 oneshot 和 daemon 统一执行 `worker.exe --omniauto-sidecar`；非冻结源码环境继续执行 Python 与 Sidecar 脚本，Worker 自身已审核的 Sidecar 命令不变。
- 默认微信外框档位保持 `100%=800×852`、`125%=1000×1065`、`150%=1200×1278`；可用工作区改为四边各扣除 `round(12 × dpi_scale)`，放不下时等比缩放并向下取整。
- “+”不保存屏幕绝对坐标，也不强制 OCR 识别字符；它使用当前 `sidebar_header_bounds` 内经验证的区域内相对位置，换算点必须位于当前操作区内，点击后仍由原 `0.9.20` 菜单识别确认“添加朋友”，失败即停止后续输入。
- 架构复审未发现新的功能性 P0/P1。复跑结果为：前台切换 3 组生产边界测试共 44 个断言通过（不冒充 44 条端到端测试），冻结/源码命令 `7 tests passed`，窗口规划 `6/6`，窗口激活 `4/4`，Win32/OCR 受影响兼容 `192/192`，启动标定 38 项通过，`git diff --check` 通过。这些证据证明前台门禁位于真实业务分发之前，Windows C1—C4 完整实机验收仍属 UAT 范围。
- PR28 受保护基线仅更新本轮实际改动的 4 个文件；Vision 九项边界门禁 `9/9`、PR28 清单一致性 `5/5` 通过。独立 OmniAuto 固定提交为 `98861fa81cfcc81e81943eb8c33ec2aa5f7c83ed`。
- 提交前完整 Worker 发布检查退出码为 `0`：Worker `929/929`、Worker UI `4/4`、加好友包烟测 `47/47`、Win32/OCR `192/192`、环境配置 `5/5`、交互证据 `6/6`、人性化输入 `6/6`、启动标定 38 项，C1 smoke 终态为 `invite_sent`；后端 C2/C3 合同与接口为 `236 passed, 1 skipped`。
- P2 文档治理已收口：本轮客户端自审与复审结论只并入本版本记录，不再提交独立“客户端自审证据”文档。

### 2.0.21 2026-08-21：0.9.23 可见微信窗口选择回归修复

- Windows UAT 复现一个可见 `微信` 主窗口与一个不可见 `Weixin` 后台窗口同时存在；0.9.23 新增门禁错误统计全部 `main_windows`，导致 C1 在零点击下返回 `WECHAT_WINDOW_NOT_READY / legal_wechat_main_window_not_unique`。
- 修复恢复 `gray-v0.9.20` 的可见窗口选择：只从 `visible_main_windows` 选择当前可操作窗口，再核对所选 HWND 与启动标定 HWND；隐藏窗口只保留诊断，不参与数量门禁。
- 公开 `run_action` 入口使用上述真实窗口形态，实际经过窗口选择、标定绑定、前台激活和 C1 加号定位直至物理鼠标边界；反例验证可见 HWND 与标定 HWND 不一致时零点击停止。C1 生产入口模块 `8/8`、前台门禁 3 组共 44 个断言、Vision `9/9`、PR28 清单 `5/5`、有效运行 `2/2` 通过。
- 旧的 PR28 additive audit 在本轮父提交 `98861fa81cfcc81e81943eb8c33ec2aa5f7c83ed` 已固定存在 3 个失败，本次结果完全相同，不冒充全绿；本次修复绑定独立 OmniAuto 提交 `befefc6fa17be11a763fa77ee09453bb28e02432`。

### 2.0.22 2026-08-21：0.9.23 撤销新增前台状态机并回归 0.9.20 语义

- Windows UAT 确认：点击“+”后，微信自身“添加朋友”顶层菜单成为前台 HWND；0.9.23 新增的共享点击门禁仍要求前台必须等于微信主 HWND，因此在 OCR、目标和坐标均正确时误报 `WECHAT_FOREGROUND_TARGET_MISMATCH`。
- 根因是兼容性改造越界：把“事务开始前确认并激活标定微信主窗口”错误下沉成“每次事务内部点击都要求前台等于主 HWND”，破坏了 `0.9.20` 正常的微信多 HWND 菜单/弹窗交互。
- 权威口径改为：主动业务入口直接复用 `0.9.20` 的 `select_primary_visible_main_window(probe) -> activate_window(hwnd) -> 原业务分发`；不强制保留 `activate_calibrated_business_window()`、全局激活成功门禁或新的统一前台失败分支。
- `0.9.23` 唯一必要新增是坐标地图归属检查：消费地图前确认 `selected_visible_hwnd == calibration.hwnd`；不一致时将旧地图判定为失效，使用已有 `WECHAT_UI_STARTUP_CALIBRATION_FAILED` 停止新 UI 动作并提示人工重启车金客户端，不自动移窗、重标定、新增前台错误码或 Handoff。
- 共享点击底层仅保留快照 ID、目标边界和坐标映射校验，必须删除逐点击主 HWND 前台等值门禁；不得只为“添加朋友”添加特例白名单或开关。
- 回归必须走完 C1“点击 + -> 菜单 HWND 成为前台 -> 点击添加朋友”生产中间链，不得在第一次物理鼠标哨兵处结束或 mock 后半段；并横向验证 C2 语音/图片菜单和 C3 发送入口。

### 2.0.23 2026-08-21：灰度候选升为 0.9.24 并锁定坐标替换边界

- `0.9.23` 的统一前台/逐点击门禁已经改变了 `0.9.20` 业务语义，不得在原版号下继续覆盖；新候选整体升为 `0.9.24`。
- `0.9.24` 只允许三类变化：启动时固定微信窗口与建立一次区域坐标地图；将 `0.9.20` 中依赖主窗口固定几何的参考点/边界映射到当前同名区域；复用两次使用之间完全没有 UI 变化的已批准重复截图/OCR。
- “+”、侧栏搜索/返回、会话行点击 X、标题/消息视口边界、输入框/工具栏/发送区的固定几何改为所属区域内的归一化映射；其中“+”不要求 OCR 识别字符，点击后仍使用 `0.9.20` 菜单证据确认。
- C1 邀请表单继承 `0.9.20` 已验证语义：申请语和备注两个输入框共用填写前同一表单帧的几何坐标；两字段填写完后必须新截图/OCR核对内容和确认按钮后才可提交，不得将填写前 OCR 内容当成填写后证据。
- 会话行 Y、标题文字、文字/语音/图片对象、菜单项、添加朋友搜索页/结果/邀请表单等动态目标，必须继续使用 `0.9.20` 当前必要业务帧和原判断，禁止从启动地图推测。
- 运行中不处理主窗口移动、缩放、DPI 切换或微信重启；只有已标定微信主 HWND 消失/被替换或客户区/DPI 变化才使地图失效。微信自有菜单/弹窗 HWND 成为前台不属于主窗口变化。真正失效时停止新 UI 动作并提示人工重启车金客户端，不自动移窗、重标定或增加 C0—C4 分支。
- 本期只承诺单显示器、100%/125%/150% 显示缩放、UAT 锁定微信版本、浅色模式和默认字体缩放；多显示器/混合 DPI、其他微信版本、深色模式和额外字体缩放不宣称支持。DPI awareness 必须在标定前设置并查询实际生效状态，设置失败或无法验证时不得静默继续。
- `0.9.24` 已形成整改后代码候选：OmniAuto 独立本地提交为 `85205428914b4a1587d6cb21458fb001c8f1c6e3`，Worker/合同本地集成提交为 `dc0e7464fe566308d493a95b20a3b5d24665eef2`；机器合同、生成 Schema、Worker 和 Windows 工作流版本已同步为 `0.9.24`，规范化合同 SHA 为 `290001afed30a8a68a1c8b48c48bf5f0af8b4d8dce1cbf87ddba666ffc6aea69`。受影响的 C1/C2/C3/C4、窗口标定、合同/打包契约定向测试已通过；Windows 实机 UAT、正式全量门禁和可交付包仍未执行，不得声称已可交付。
- 架构师复审确认邀请表单继续复用填写前同一张表单画面定位两个输入框，填写完成后使用新画面复核并点击确认；该行为保持 `0.9.20` 原流程，不属于缺陷。本轮关闭两个代码 P1：DPI awareness 必须查询并确认 per-monitor aware 后才允许标定；Worker 和 Sidecar 不再为每笔事务重复比较窗口位置、尺寸和 DPI，业务入口只确认当前可见微信主 HWND 属于启动地图。同时完成 `.chejin-source.json` 新候选绑定。架构复审批准提交和推送，并允许生成 Fast UAT ZIP；该批准不等于 Windows C0—C4 实机 UAT 已通过。

### 2.0.6 2026-08-18：0.9.21 微信动态布局与统一坐标方案确认

- Windows 4K/150% DPI 实机暴露固定 `300—370px` 侧栏边界导致加好友“+”号真实位置落在搜索区外；相关固定边界同时影响会话行、标题、消息视口、角色和发送区域。
- 现有 Sidecar 仍会调整窗口尺寸，但 Worker 直接启动 Sidecar 的路径未统一继承 Connector 的固定原点策略，因此窗口规范化入口并不一致；本次按所有车金 UI 入口统一前置门禁整改。
- 每个 HWND 的每张新截图分别生成不可变 `layout_snapshot`，新截图、新 HWND 或任意 UI 动作立即废弃旧快照；禁止整条 Flow 共用一张布局。
- 所有模块只消费 OmniAuto 的当前布局快照，点击统一经过截图到屏幕、屏幕到客户区的唯一转换器；固定像素仅作最小尺寸、OCR 边距、目标内部余量和诊断参考。
- 布局只确定区域，不替代 private/短码、头像/气泡角色、媒体回执和 S0/S1/S2 门禁；布局失败最多零点击重取一帧，仍不明确则技术失败且不创建业务 Handoff。
- 方案复审后删除运行时动态布局关闭开关、旧设备 profile 准入和同包旧坐标路径；`0.9.21` 未正式上线，不承担这些兼容成本。回滚只能整体回退不可变版本并暂停该机器接单。
- 本条记录方案刚确认时的状态：当时代码和机器合同仍是 `0.9.20`。当前实现状态以下一条“0.9.21 代码候选形成”为准；真实 Windows 三档验收和正式打包仍未完成，不得写成已通过。

### 2.0.7 2026-08-18：0.9.21 代码候选形成

- 已实现统一窗口规范化、每帧独立不可变 `layout_snapshot`、截图到屏幕再到客户区的唯一坐标转换，以及 UI 变化后的快照失效。
- `contracts/c2_contract_v3.json`、生成 Schema、Worker、OmniAuto 来源清单和 Windows 打包工作流统一升为 `0.9.21`，规范化合同 SHA 为 `c552dee933d305ad55c17388e55b9590e72a28d6a4965282f0670f23b2111a36`。
- 已通过布局快照、坐标转换、加好友、窗口规划、兼容性、合同/Schema、Python 编译和 diff 检查的定向自动化；这些测试包含生产构建器/转换器，但布局图像输入仍为合成数据。
- 真实微信截图回放、原测试机与故障机 Windows UAT、三档分辨率/DPI、窗口移动/缩放/边框变化和点击前后证据仍是未完成的兼容性验收项。正式 ZIP 工作流的人工批准和执行记录必须与本候选提交绑定。

### 2.0.8 2026-08-20：0.9.21 架构复审收口

- 修复结构布局候选选择：相同导航边界后的聊天面板附加竖线不再冒充侧栏边界；导航边界本身近似冲突时零点击失败关闭。
- 明确截图/OCR职责：可执行布局只来自当前精确微信 HWND 的可见窗口截图；增强 OCR 仅裁剪同一帧 ROI 并映射回原图，PrintWindow 和桌面全屏只作诊断。
- 删除运行时旧坐标兼容开关、旧设备 profile 比对、未使用固定几何 locator 与无边界裸屏点击入口；生产只保留动态布局和统一坐标转换。
- 自动化的三档分辨率/DPI 输入目前仍是合成图，只证明生产算法及换算，不冒充真实微信截图或 Windows UAT。
- 修正绝对 Vision 哈希门禁的报告逻辑：一次列出全部受保护文件差异，不再在首个差异处停止并误报为唯一失败；受保护文件完整 diff 审核后才允许更新基线。

### 2.0.9 2026-08-20：0.9.22 Windows 候选门禁修复

- `0.9.21` 推送后的 Windows Fast UAT 在窗口规范化用例中失败：测试使用无效伪 HWND，但只 mock 了窗口几何，未 mock 客户区几何；生产代码按设计失败关闭，不是生产动态布局缺陷。
- 测试夹具已同步 mock 窗口和客户区几何，不改动 `WECHAT_UI_WINDOW_NORMALIZATION_FAILED` 及任何点击前失败关闭规则。
- 按“任何内容变化必须升版”的不可覆盖规则，候选整体升为 `0.9.22`；合同 SHA 为 `1a07dde94d270676cde4e6f1e0af3dcc071f4efedbb66c866c06cd64b36a5d39`，本次复审后 OmniAuto 固定提交为 `f0949fc3eea8abd32a7521ffec1cdba7368c8382`。
- 旧 1920 参考点、`300—370px` 固定侧栏边界和旧输入/发送点仍为零生产引用；诊断只保留实际分辨率、DPI、窗口/客户区、动态边界、置信度、冲突和最终点击点。

### 2.0.10 2026-08-20：0.9.22 C1 加好友实机失败修复

- Windows UAT 在 C1 点击前暴露布局算法未找到低对比度侧栏竖边，随后将空区域传入 OCR 并抛出 `ValueError`；该异常又被 Worker 当成 `OTHER` 环境故障自动停单。
- 加好友入口改为动态识别当前截图侧栏两条竖边，以唯一“搜索” OCR 文字高度构建同行操作带，只接受操作带内唯一视觉“+”；不依赖选中会话上方横线，也不依赖聊天区和输入区布局。
- 1920、2K、4K 默认窗口统一为约 `980×860`，125%/150% DPI 只用于坐标和诊断，不再放大窗口；小屏仅按实际工作区安全裁剪。
- Windows 实机暴露启动规范化错误沿用当前约 `800×852` 窗口；本候选改为默认提升到标准 `980×860`，并在预检报告中记录调整前、目标、调整后窗口/客户区、工作区和 DPI。
- 空 OCR 区域现在返回类型化 `WECHAT_UI_LAYOUT_UNRESOLVED`，点击前布局失败不再自动停单；仍保持零点击失败关闭。
- 四张明亮主题真实微信截图（包含非首条会话选中）本地回放均得到唯一“+”候选；原图只作本地验证，不进入仓库和安装包。
- 完整 OCR 后先补全当前布局快照，再执行加好友点击前检查和校准；窗口启动规范化只有“未找到微信”可自动重试，其他失败锁定且后续心跳只读。
- 搜索锚点统一由动态侧栏最上方操作行选择，会话预览中的“搜索”不参与竞争；加号识别只消费布局快照的唯一锚点，不再二次解释 OCR。
- 四张真实截图只作为像素布局和视觉“+”识别证据，不冒充 Windows 真实 OCR 主链；完整 OCR 仍以 Windows UAT 为准。

### 2.0.11 2026-08-20：恢复启动与业务布局识别分层

- 恢复 0.9.20 的流程边界：客户端启动只规范微信窗口并由随后状态探测确认登录，不再把完整布局识别作为启动门禁。
- 加好友、C2、C3 的完整截图、OCR、动态布局、坐标转换和点击前安全检查仍在真实业务动作前执行；识别失败继续零点击关闭。
- 启动规范化成功后，业务预检统一使用 `window-policy=verify`，只复核 HWND、窗口/客户区和 DPI，不重复移动或缩放微信。
- Worker 失败证据新增嵌套诊断摘要，保留真实 `state/reason/error`、OCR 数量、布局置信度、冲突和零点击状态；预检报告按 Sidecar 真实嵌套结构记录调整前后几何。
- 本次保持合同和版本 `0.9.22` 不变；独立 OmniAuto 固定提交为 `411ff5be9c3872cac645689ac251ad053923837d`。

### 2.0.12 2026-08-20：加好友头像伪竖边兼容修复

- Windows UAT 原始计划已识别 `Q搜索`（置信度约 `0.956`）；真实失败不是 OCR 漏字，而是多行对齐头像产生的竖边分数高于导航分隔线，导致搜索文字被错误判到侧栏外。
- 动态布局继续要求顶部搜索 OCR 锚点，并用其当前文字边界筛除穿过搜索文字的头像伪竖边；最终导航、侧栏和点击坐标仍只来自当前截图像素，不恢复固定坐标或无搜索锚点兜底。
- 生产入口反例从完整加好友点击计划进入，使用真实布局构建器和真实“+”视觉识别，仅替换 Windows 截图、OCR 引擎与物理鼠标边界；验证选中首行、头像对齐时仍唯一命中真实“+”。
- 本次保持合同和版本 `0.9.22` 不变；独立 OmniAuto 固定提交为 `54e13519ec32776cf7a08c730f62992ca947f5d7`。

### 2.0.13 2026-08-20：邀请表单稳定快照与短码复核修复

- 问候语和备注属于同一稳定邀请表单，复用填写前已经确认的同一布局快照；两项填写之间不重复截图，两项完成后统一截图一次复核，再使用复核后的新快照点击确认。
- 页面切换、弹窗、窗口变化和其他普通点击仍按原规则使旧快照失效；本次只为邀请表单首字段开放受限的快照保留，不扩大到 C0—C4 其他动作。
- 当备注由程序复制粘贴且为八位字母数字短码时，字段内唯一、OCR 置信度不低于 `0.90` 的八位码即可通过，避免 `V/W` 等单字符 OCR 混淆误拦截；低置信、多个候选或非八位内容仍失败关闭。
- 本次保持合同和版本 `0.9.22` 不变；独立 OmniAuto 固定提交为 `6b3bc7d54b921ddc948cd2b5b9cdcb475edb08eb`。

### 2.0.14 2026-08-21：C1 未变化画面证据复用

- 不改变 0.9.20 的加好友业务步骤、搜索/弹窗/输入/发送规则，只合并相邻步骤间没有任何 UI 变化的重复截图和 OCR。
- 证据复用必须同时匹配 `HWND + frame_id + layout_snapshot_id`，且两次使用之间没有点击、输入、窗口移动或页面切换；任一条件不满足立即重新截图并重新定位。
- 正常 C1 画面节点收敛为主界面、加号菜单、搜索弹窗、手机号输入后、搜索结果、邀请表单、表单填写后、提交结果；C0、C2、C3、C4 流程不变。
- 版本和合同保持 `0.9.22`，独立 OmniAuto 固定提交更新为 `03a7dc2e360fbc0c208ca4f3634811d04b6af21b`。

### 2.0.15 2026-08-21：C1 证据帧公开接口兼容修复

- 修复调用方、公开 Sidecar 门面和模块化实现三层签名漏同步：公开门面现在接受并透传可选 `frame_seed`，不再在手机号搜索前抛出 `unexpected keyword argument 'frame_seed'`。
- 新增跨层回归，从公开 Sidecar 入口进入真实模块化实现并注册真实布局快照；只替换 Windows 截图、OCR 和物理鼠标边界，同时验证没有发生重复截图。
- 不改变加好友步骤、窗口规范化、布局识别、输入、复核或发送规则；版本和合同保持 `0.9.22`，独立 OmniAuto 固定提交更新为 `8d05ac478ec4edbd6e9100c4dfcf289fa6225fa2`。

### 2.0.16 2026-08-21：C3 动态输入边界贯穿修复

- 修复会话输入定位已经找到动态点击点和安全边界，但调用粘贴层时只传点击点、丢失边界，导致统一坐标转换以 `target_bounds_missing` 在输入前失败关闭的问题。
- 当前输入框点位与安全边界作为同一份当前布局证据贯穿定位、粘贴、焦点复核和发送；任一边界缺失、越界或快照失效仍保持零点击，未恢复固定输入点。
- 新增从公开发送入口经过定位、发送事务、粘贴、统一坐标转换、焦点复核直到物理键盘边界的真实中间链测试；只替换 Windows 鼠标、键盘、剪贴板、截图和窗口系统边界，不替换中间业务函数。
- 版本和合同保持 `0.9.22`，独立 OmniAuto 固定提交更新为 `b9e919bac200efed00b2d4522b3a405767db07d8`。

### 2.0.17 2026-08-21：C2 完整布局头像伪边界与空扫描修复

- Windows C2 首屏证据已经识别 `Q搜索` 和多个真实会话标题，但完整布局错误选择多行头像形成的 `x=144` 伪竖边，搜索锚点因此落到侧栏之外，最终把布局失败误报为成功的零会话扫描。
- C1 加好友入口与 C2/C3 完整布局现在共用同一搜索锚点边界过滤器：用当前帧顶部搜索文字边界排除穿过或紧贴搜索文字的头像伪边界，再从剩余当前像素竖边中确定导航和侧栏；不恢复固定坐标或历史窗口模型。
- OCR 有结果但当前布局无效时，C2 `sessions` 明确返回 `WECHAT_UI_LAYOUT_UNRESOLVED / sessions_layout_unresolved`，携带真实置信度和冲突，不再上报成功空列表；仍保持零点击失败关闭。
- 同一 Windows 截图与 36 条原始 OCR 结果通过生产函数重放后，导航/侧栏边界恢复为 `84/382`、布局置信度 `0.849`、冲突为空并解析出 9 个会话；另一个真实桌面截图明确显示目标短码 `CJATKDE5` 已在微信侧栏出现，两份证据按各自物理帧分别记录，不冒充同帧 OCR。
- 版本和合同保持 `0.9.22`，独立 OmniAuto 固定提交更新为 `8b4e4b05fa7cbe7dc135a0c3dd34a707d90c7006`。

### 2.0.18 2026-08-21：窗口规范化与点击安全门禁减重

- 确认原“每个 UI Flow 全量复核窗口外框、客户区和 DPI”口径过重；改为客户端启动只规范化微信一次，正常 C0—C4 子步骤不再为窗口位置和 DPI 重复截图、OCR 或移窗。
- 微信 UI 事务的必要业务帧记录 HWND、客户区尺寸、DPI 和坐标系仅用于坐标换算与诊断；物理点击时只确认当前前台 HWND，禁止额外查询或比较位置、外框、客户区尺寸、DPI 和进程 ID。
- 不改造现有坐标链路：继续根据业务截图的捕获屏幕原点和帧内目标点生成本帧屏幕点，禁止新增客户区点持久化或点击时 `ClientToScreen`。窗口尺寸、DPI 或业务表面变化只通过既有 Windows 事件或下一张本来就需要的业务帧被动发现。
- 相邻步骤间无 UI 变化时必须复用同一帧/OCR；输入后已产生的验证帧同时用于内容复核和下一按钮定位。页面、菜单、列表、客户区尺寸或 DPI 真实变化仍必须建立新帧。
- 本次不改 C0—C4 业务状态机、授权、媒体身份、Brain、Handoff、S0/S1/S2 和 sent_ack 语义；版本与合同仍保持 `0.9.22`。

### 2.0.5 2026-08-17：0.9.20 已是好友页面收尾

- `already_friend` 不再在识别后直接结束：复用唯一安全关窗函数，对已经由添加朋友流程证明的 HWND 只点击右上角一次，并验证窗口销毁或隐藏。
- 邀请发送成功路径同步复用该关窗函数，保留既有精确标题与存活 HWND 两类证据；正文、普通微信窗口或未知窗口不能授权关窗。
- 关窗失败不改变“已经是好友/邀请已发送”的业务完成事实，但必须记录 `unclosed`，不得重试点击或伪报已关闭。
- 独立 OmniAuto 固定提交为 `1591942b872ef6d9db10e1922d441aff30c2c414`；车金来源清单、合同、生成 Schema、Worker、后端和打包清单统一为 `0.9.20`。

### 2.0.4 2026-08-16：0.9.19 发布门禁迁移

- `0.9.18` 已形成不可变提交和标签；随后 Windows 发布门禁发现旧测试夹具未携带 `selected_action_token/pre_observation_id`、发布测试依赖未声明的 pytest，以及 Windows 临时 SQLite 清理竞态。
- 上述变更只迁移测试、合同样例和发布门禁，不改变 v0.9.18 已审生产语义；根据不可覆盖版本规则，最终候选整体升为 `0.9.19`。
- `0.9.19` 的 Worker、后端合同、Sidecar 生成 Schema、manifest、包名、客户端显示和来源记录必须使用同一精确版本及实算合同 SHA。

### 2.0.3 2026-08-16：0.9.18 语音帧内动作证据闭环

- `0.9.17` 的正式 `source_message` 白名单正确禁止临时动作字段，但执行时机过早；真实 observation 构建删除动作编号和预留号后，execute 又从 `source_message` 读取它们，正常语音被确定性误判为 `C2_VOICE_RESULT_AMBIGUOUS`。
- `0.9.18` 保持正式白名单不变，新增独立、仅限 execute 内存生命周期的 `frame_action_binding`；真实 observation 构建保留本次 action ID、预留号、action token 和前后 observation 映射，生成 confirmed mapping 后立即从正式 observations 删除。
- Worker 同时校验 action ID、预留号、action token、pre/post observation 和唯一候选；任一缺失、矛盾、零候选或多候选仍失败关闭，不重复点击、不入库、不调用 Brain。
- 成功主链测试不得替换语音绑定、真实 observation 构建或 post observation 选择函数；只允许替换截图、OCR 原始结果、物理点击和等待等 Windows 外部边界。

### 2.0.2 2026-08-16：0.9.17 C2 身份与媒体编排 MECE 收口

- 架构复核确认此前技术方案与全流程图顺序冲突：详细规则禁止新图片在 action receipt 前生成正式身份，但旧全流程图先为全部新槽位生成 source key 并查询 Ledger，导致实现可以按局部口径提前正式化。
- C2 身份统一为四类互斥对象：`frame_observation / pending_media_action / committed_message / quarantine_record`。只有 `committed_message` 可生成 source key、查询或写入 Ledger/Outbox、构建 V3 message、上报后端或进入 Brain。
- 新文字/系统消息可由唯一完整 `new_suffix` 直接提交；新语音/图片必须先预留不可复用编号并落 ActionJournal，只有本次 confirmed action 映射经唯一提交门验证后才能提交正式身份。属于 new suffix、已有预留号、正文/时长/坐标相同或 Vision 成功均不构成提交依据。
- 媒体 action 终态固定为 `cancelled_before_trigger / committed_completed / committed_failed / identity_unresolved`。identity unresolved 不生成业务消息、不重复 UI 动作；只允许使用持久化证据和最多两次无 UI 稳定重读，仍不能证明时幂等 handoff，不能永久挂起。
- `identity_state / identity_phase / _worker_identity_scope` 分别降级为帧内对齐、旧 Journal 迁移和过渡期内部字段，任何正式消费者不得直接读取这些字段自行放行。缺失、空白、未知和矛盾状态统一失败关闭。
- 语音与图片必须经过同一个正式消费者白名单，关闭“图片要求完整回执、语音只检查 ID 非空”的媒体不对称旁路。ActionJournal、continuation、媒体后续读、Outbox 恢复和崩溃恢复均复用同一正式提交门。
- 既有 `0.9.16` revision/SHA 冻结为不完整候选，不得覆盖。实现已统一升至 `0.9.17`，代码、合同、生成 Schema、样例、静态旁路门禁、MECE 消费者矩阵和正向主流程回归已同步；规范化合同 SHA 为 `a529986901e9e23e2cbfc57472229f680903487c67747668212b68930b782a5d`。架构复审和双仓来源治理完成前仍禁止打包。

### 2.0.1 2026-08-16：跨轮身份合同收口

- `frame_visual_id` 可包含气泡坐标，只用于本帧定位、点击确认和排障，不再作强锚点、矛盾证据或长期 source key。
- OCR 文字、语音、图片及 AI 发送回执的跨轮业务身份统一为已提交 `worker_stable_id`；真实原生 ID 与 confirmed action 映射仍可作强证据。
- 无原生 ID/无 confirmed action 的历史语音或图片，必须被前后两个已唯一对齐的历史上下文夹住才能继承身份；单侧文字不得为媒体背书。
- 本轮媒体的临时号与历史正式身份分层；只有原生 ID、双侧历史边界或 confirmed action 凭证能够提交/恢复正式身份。AI 已发回执只锚定最新未回复尾部，不恢复其上方未证明媒体。
- 旧媒体消失而新媒体占据原尾部时必须失败关闭，不得继承旧编号、生成旧 source key 或被历史 Ledger 跳过。
- 旧 `canonical_visual_id/canonical_input_id`、坐标、时长、anchor、正文和同类序号均不得恢复或重挂长期身份。
- 回归必须调用真实帧 ID 生成器模拟滚动，并覆盖已转写旧语音上移、新语音进入编排器、图片不重复 Vision 以及 AI 回执不因坐标变化失效。

### 2.0 2026-08-15：跨轮身份换行兼容与观测事务隔离

- Worker 与后端共用消息身份正文规范化：仅删除中日韩文字或标点旁的 OCR 视觉换行；普通水平空白和英文词间换行保留一个空格，未换行的 `0.9.8` 历史正文哈希保持不变。
- 已确认 AI 文字即使在当前画面被 OCR 自动换行，也能继续与后端 checkpoint 唯一对齐；其后的新增语音进入唯一语音编排器，不再误报 `MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS`。
- 观测 SAVEPOINT 的提交或回滚不再触发最外层业务 `after_commit/after_rollback`；耗时写入失败不能消费或清除 Handoff 飞书通知。
- 已提交但从未尝试的飞书通知由 C3 持久化恢复循环补偿结算；相同 HandoffEvent 仍通过原子 claim 保证至多一次发送权。
- Handoff 型 ingest 的耗时终态记录真实业务失败和错误码，不再因 HTTP 成功而伪报 `succeeded`。

### 2.1 2026-08-15：C0—C4 统一耗时旁路观测

- 后端使用独立 `process_stage_runs` 表保存 C0—C4、Brain、发送、Handoff 和飞书通知阶段；业务表和业务 Outbox 不反向依赖观测结果。
- `process_run_id` 表示一次业务处理，`stage_run_id` 表示单阶段单次尝试，`trace_id` 仅用于单次技术请求排障；重试新建阶段记录而不覆盖旧记录。
- Worker 使用独立 telemetry SQLite 缓冲，不复用 Ledger、Journal 或业务 Outbox；观测超时、拒绝或本地不可写时，业务仍正常完成。
- 排队耗时与执行耗时分开记录；无法用同一进程单调时钟证明的耗时保持 `null`，异常退出的开放阶段记为 `abandoned`，禁止猜测。
- 本轮未新增微信点击、OCR、截图、Brain/Vision 调用或业务重试；新迁移头为 `20260815_0029`。

### 2.2 2026-08-15：AI 发送回执的文字类型与正文归属分层

- 实机证据确认：物理发送已完成、输入框已清空、右侧长文字气泡存在；全屏 OCR 把气泡投影为结构图片，局部增强 OCR 实际完整识别正文，仅存在 `？/?` 等呈现差异，旧的“全文逐字一致”门禁仍错误返回 `SEND_RESULT_UNKNOWN`。
- 消息类型和本次发送归属改为两个独立判断：最底部右侧结构候选由同排头像确认 `self`，且局部增强 OCR 存在可读文字时，必须投影为 `text`，不得继续作为图片；是否属于本次 AI 回复再依据格式规范化后的有序正文重合度判断。
- 格式规范化覆盖全半角、大小写、普通/全角/不换行/零宽空白、中英文句号、直弯引号、不同横线、省略号、括号和 Emoji 变体标记；保存的 OCR 原文不被覆盖。
- 非全文一致路径要求 AI 正文与 OCR 正文的双向覆盖率、整体相似度均至少 `80%`，同时存在足够长的连续匹配片段；仍必须满足当前目标强确认、右侧 `self`、发送后新增尾部气泡、旧结构候选排除和输入框为空。
- OCR 文字与 AI 正文重合不足时仍保持 `text`，但不得确认本次发送；没有可读文字、目标或角色证据不足、没有新增尾部气泡时继续使用 `SEND_RESULT_UNKNOWN` 并禁止自动补发。
- `gray-v0.9.6` 已生成并用于灰度，本轮代码、合同、Schema、manifest、文档和包统一发布为新的不可变 `0.9.7`。

### 2.3 2026-08-14：发送焦点有限恢复与未读代次

- 发送前只因前台窗口不是目标微信而失败时，允许两次有限恢复，分别等待 300ms 和 700ms；仍无法确认则保持 `not_attempted`，零输入、零发送。目标、标题、private 或几何门禁失败不进入该重试。
- 扫描层的 `unread_hint` 仅表示物理红点观察；后端使用单调 `unread_generation / consumed_unread_generation` 表示可结算的新消息事件。
- 新代次只使用短码、规范化预览、预览时间、可靠原生观察 ID 或明确 `false -> true` 证据；行坐标、红点边框、整份 `row_fingerprint`、新 `scan_id` 和 OCR 置信度波动禁止制造新代次。
- 读取票冻结启动时代次；N 读取期间建立 N+1 时，N 结算只消费 N，N+1 仍保持待处理。同一红点/预览不清空冷却；无法仅凭预览证明的重复同文消息到正常到期后仍会被读取。
- 人工接管仍优先遵守首次 2 分钟及后续 5/10 分钟监听冷却，不会被旧红点代次改回高频完整读取。
- `0.9.7` 替换顺序固定为：暂停 `0.9.6` Worker -> 确认无未结算 Outbox/Journal/回执 -> 升级后端并确认迁移头 `20260814_0028` -> 替换 `0.9.7` Worker -> 恢复接单。存在未结算事实时禁止直接清库。

### 2.4 2026-08-14：AI 回复气泡的发送结果补强确认

- 实机证据确认 AI 回复已真实发送且输入框清空，但六次全屏 OCR 都漏掉绿色长文字气泡，结构图片探测器因缺少文字类型证据将其投影为图片，导致 `SEND_RESULT_UNKNOWN`。
- 该历史版本要求 OCR 全文与本次程序发送正文完全一致；此限制已被 `0.9.7` 的“文字类型与发送归属分层”规则取代。
- 当前目标未强确认、头像角色不明、OCR 不一致或旧结构候选已存在于发送基线时，仍保持 `SEND_RESULT_UNKNOWN` 并禁止自动重发。
- `gray-v0.9.3` 已生成并用于灰度，本轮代码、合同、Schema、manifest、文档和包统一发布为新的不可变 `0.9.4`。

### 2.5 2026-08-14：无车源时的 Brain 澄清与转人工承接收口

- 实机批次证明第二次 Brain 已生成低风险购车澄清，但语义审稿重新读取原始 `must_handoff=true / allowed_auto_reply=false`，把软证据提示升级成硬阻断并清空可见回复。
- 正式口径统一为：缺少 Product Master 只禁止未经授权的具体车型、价格、库存、车况和业务承诺；不声明这些事实的需求澄清可以回复，不要求先存在商品 ID。
- Brain、语义审稿、质量修复和 Guard 必须共享同一套软/硬证据解释；控制层不得以原始字段重新推翻 Brain 的安全澄清计划。
- 确需转人工时，由 Brain 生成一次 Guard 批准的客户可见承接并执行 `reply_then_handoff`；只有 Brain 无法安全生成承接时才允许静默 handoff。
- `gray-v0.9.2` 已生成并用于灰度，本轮任何代码、合同、Schema、manifest、文档或包内容变化均发布为新的不可变 `0.9.3`。

### 2.6 2026-08-14：加好友收尾与 Brain 可见回复恢复

- 加好友最终“确定”点击成功后等待短暂稳定时间；若顶部标题 OCR 精确确认仍残留“添加朋友”页面，则点击其右上角 X 完成 UI 收尾。正文、联系人资料数量和已通过好友后的资料内容均不参与页面判定，收尾失败也不得篡改已经确认的邀请发送事实。
- Brain 第一次返回 `AI_ENGINE_NO_VISIBLE_REPLY` 时，第二次仍复用同一消息批次，但增加聚焦恢复指令：没有 Product Master 证据时不得编造车型、价格或库存，应输出可见的简短澄清问题；硬风险仍必须转人工。
- 每一次 Brain 原始响应及适配结果均按尝试序号单调追加到现有批次快照；重试成功、最终转人工、硬拒绝或暂停均不得覆盖首轮失败证据。
- 因 `gray-v0.9.1` 已生成并用于灰度，本轮任何代码、合同、Schema、manifest、文档或包内容变化均发布为新的不可变 `0.9.2`。

### 2.7 2026-08-13：Worker 当前过程连续展示

- “当前运行过程”按同一次客户事务持续追加已发生节点，不再把首屏扫描、定向读取和 AI 回复拆成三套互相跳转的画面。
- 纯首屏扫描无命中仍独立结束；命中客户后从发现、定位、读取、媒体、服务端判断、Brain、发送到回执保持同一展示链路。
- 新增的运行过程投影只影响本地 UI，不改变后端接口、RPA 执行顺序、授权、Ledger、Outbox 或业务状态机；展示异常与业务线程隔离。
- 当前过程滚动改为窄圆角中性灰滑块，同时保留滚轮、键盘与拖动操作。
- 因 `gray-v0.9.0` 已生成并用于灰度，任何内容变化必须发布为新的不可变 `0.9.1`，合同、Schema、manifest、文档和包名同步升级。

### 2.8 2026-08-12：微信列表重排安全误点恢复

- 实机证据确认：首屏 OCR 正确定位服务端下发的目标短码，点击前其他会话收到新消息导致列表重排，旧坐标打开了非目标会话。点击后标题校验安全拦截，未读取消息区、未入库且未发送。
- 历史 `3.13.2` 只对非终局定位失败继续短码搜索；实机误点可被投影为终局会话类型异常，因此安全停止后没有继续定位当前客户。
- 历史整改候选 `3.13.3` 已新增 `C2_VISIBLE_TARGET_STALE_AFTER_CLICK`；该行为完整并入灰度 `0.9.0`：只有在点击后标题明确不含目标短码，且未读取消息区、未触发媒体、未输入或发送时，才允许在同一授权/UI 锁/`read_run_id` 内丢弃旧坐标，重新截取安全基线并直接按目标短码精确搜索一次。
- 目标短码已在标题中但会话为群聊或类型不明时仍终局失败；不因恢复能力降低 private 与短码门禁。

## 3. 2026-08-11：目标合同 3.13.2

### 3.1 阻断原因

- `3.12.9` 在语音动作前把预留号当成正式身份，并用 anchor、正文或位置跨动作重挂。
- 未发布 `3.13.0` 没有把 OmniAuto 动作前选中的物理语音写入请求合同。
- `[1,1,1]` 只能说明三张图各有一个候选，不能证明三张图中是同一条语音。
- failed/ambiguous 动作可能无法有限结算，形成永久 pending 或重复处理风险。
- 部分图片菜单拒绝、非位图路径实际操作了微信，却允许跳过最新画面刷新。
- 初读、续读和恢复仍存在多个语音编排入口，容易修一处漏一处。
- `3.13.1` 整改候选在首次读取后无条件进入语音编排器；纯文字、纯图片或仅含历史/已转写语音时，
  `prepare empty` 可能覆盖原 `initial_read` payload，导致正常消息不 ingest 或增加无意义全窗口 OCR。
- 语音成功路径产生临时值 `voice_execute_final`，而 Worker 消费端只接受
  `initial_read/final_read/action_journal_recovery`；合同只要求字段存在，未限定枚举，造成生产者与消费者各自解释状态。

### 3.2 目标整改

- OmniAuto `prepare` 零 UI 操作选中物理语音，返回真实 pre frame、selected observation、
  单次 token 和动作局部指纹；Worker 只校验和持久化，不重新选择。
- `execute` 原样消费 token，点击前重验同一会话和同一物理目标；失败则零点击取消。
- 连续跟踪改为逐相邻帧 `tracking_edges[]`，删除 `tracking_candidate_counts[]`。
- 每个 action 必须进入 completed、failed、quarantined 或 cancelled_before_trigger 有限终态。
- 任一右键、关菜单或复制都使旧 UI 帧失效；下一媒体、ingest/Brain 前强制刷新和统一对齐。
- 初读、图片后新增语音、continuation 和崩溃恢复统一调用一个生产语音编排器。
- 语音编排器增加唯一进入条件：当前权威观察中存在可执行、未转写且未由历史终态结算的语音；
  无候选时零 `prepare/execute`、零额外语音 OCR，并原样保留 `initial_read` payload。
- `authoritative_frame_source` 固定为 `initial_read/final_read/action_journal_recovery`；任何语音/图片 UI
  动作或使初始帧失效的并发页面变化后统一使用 `final_read`，删除 `voice_execute_final` 等临时值。合同、生成 schema、Worker、后端和测试同步校验。
- 正常主流程矩阵成为发布硬门禁：纯文字、纯图片、纯语音、已转写/历史语音、文字+语音、语音+图片、
  媒体期间新增同文消息和无 UI 动作恢复必须逐条证明 ingest、顺序、Brain 准入及零多余媒体动作。

### 3.3 必须提供的定向证据

- A/B/C 三帧各一个候选但不是同一语音，必须拒绝 confirmed。
- prepare 选中 A 后新增 B 占据原位置，A 的 action ID 不得绑定 B。
- failed/ambiguous 有限结束，不重复点击、不永久 pending、不阻塞其他短码。
- 图片菜单拒绝和非位图期间到达新文字，刷新后新文字必须进入最终上下文。
- 初读、续读、图片后和恢复均命中同一个生产语音编排器。
- 静态门禁禁止旧身份回挂算法和第二套编排入口恢复。
- 纯文字/纯图片不得调用 voice prepare；语音成功后必须输出 `final_read`；枚举外值必须在任何 ingest/Brain 前失败关闭。

## 4. 已确认历史基线

| 日期 | 版本/提交 | 结论 | 说明 |
|---|---|---|---|
| 2026-08-03 | `v16.130.0 / 8ee53e1 / 3.12.4` | Windows C2 实机通过 | 覆盖 private 准入、图片理解、统一顺序、跨轮去重、多目标串行和停止监听；安装包 SHA256 为 `4c62183370e1915a463e5771a52377a05753ac41a61a43cc6e48fc9832e44179` |
| 2026-08-04 | `v16.132.0 / 37139bfd` | 受影响范围回归通过 | 双仓统一后的历史维护基线 |
| 2026-08-10 | `v16.145.0 / 9872dad…` | 快速 UAT 发现新 P0 | 多 anchor 身份、媒体结算和后续身份生命周期问题，不能作为发布基线 |
| 2026-08-11 | `3.12.9` 候选 | 阻断 | 点击前提交正式身份，自动滚动后错误重挂 |
| 2026-08-11 | `3.13.0` 候选 | 阻断 | 缺少 selected-target 握手、真实 tracking edges、有限终态和完整 UI 刷新 |
| 2026-08-11 | `3.13.1` 整改候选 | 阻断 | 正常无语音流程仍无条件调用 voice prepare，且生产端 `voice_execute_final` 与消费端枚举不一致；此前定向安全测试不能证明主流程可用 |

历史通过只证明对应不可变提交和包，不证明其后的候选通过。

## 5. 版本与合同规则

- 灰度系列固定为 `0.9.x`。`0.9.0` 至 `0.9.55` 已冻结且不得同名覆盖，当前技术方案目标候选为 `0.9.56`；后续每个内容不同且进入测试的
  不可变候选继续顺序升版，不得跳回旧号或覆盖同号内容。
- 发生产品变化时 PRD 与技术产物一并升版；只有技术缺陷修复时 PRD 保持最后一个有效产品版本。技术方案、全流程图、版本记录、客户端、后端、OmniAuto 合同 `contract_revision`、生成 Schema、manifest 和安装包必须使用同一个精确灰度版本，不再各自维护版本号。
- `0.9.X` 只表示版本范围，禁止写入合同、代码、Schema、manifest、安装包名称或运行日志；
  任一可执行候选必须写明具体版本，例如 `0.9.0`。
- `contract_version=3`、`observation_schema_version=3` 和 V3 只表示协议结构代号，不属于
  灰度发布版本，也不得对外简称为“版本 3”；运行兼容校验使用同一 `contract_revision + SHA`。
- 任何业务、架构、代码、合同、配置或打包内容变化，只要要生成新的灰度包，就必须整体升
  一个 patch 号并同步所有组件；纯文档错别字且不重新发包可只记录内部修订。
- 每个候选包必须绑定唯一 Git commit、OmniAuto 固定提交、精确灰度版本、合同 SHA、
  构建时间和安装包 SHA256。旧包不得覆盖上传或继续使用相同文件名冒充新构建。
- 灰度稳定分支固定为 `codex/gray-release-0.9.x`；每个不可变候选使用
  `gray-v0.9.0、gray-v0.9.1、gray-v0.9.2、gray-v0.9.3、gray-v0.9.4、gray-v0.9.5、gray-v0.9.6、gray-v0.9.7、gray-v0.9.8、gray-v0.9.9、gray-v0.9.10……` 标签。`main` 只接收完成灰度验收的确切标签提交。

## 6. 分层测试与发包规则

### 开发整改阶段

- 只运行故障复现、直接修改模块、上下游合同和相邻状态转移的定向测试。
- 不因每次小改动重复运行全部 Worker、后端、前端和打包门禁。
- 定向测试必须走真实生产入口；不得由 Fake 直接伪造 terminal、binding 或刷新结果。

### 提交候选阶段

- 运行受影响模块回归、共享 schema/生成物检查、Python 编译和 `git diff --check`。
- 跨数据库并发、Windows UI 或真实模型只在本次改动触及对应边界时执行。
- 工作区不干净、来源提交不明或合同/schema 不一致时不得形成候选。

### 打包与正式发布阶段

- 只有候选通过架构复审后才运行完整发布门禁和生成 ZIP/EXE。
- 消息身份、授权、发送、防重复、恢复或安装更新发生实质变化时执行完整 Windows UAT；
  局部展示或不影响运行合同的变更只做受影响范围 Windows 冒烟。
- UAT 后任何生产代码变化都必须产生新提交、新版本和新包哈希，原 UAT 结论不得沿用。

## 7. 回滚规则

- 回滚目标必须是已验证的不可变提交和包，不回滚到 dirty 工作区或临时 ZIP。
- 回滚应用前先保留数据库迁移、Outbox、ActionJournal 和 sent_ack 事实；不得为了启动成功清空事务。
- 已发生但发送结果未知的消息仍禁止补发；回滚不能改变这一安全终态。
- 数据库不可逆迁移必须在升级前备份，并在技术方案中写明应用回滚与数据回滚是否分离。

## 8. `1.1.x` 下一期优化版本清单

`1.1.x` 是正式上线 `1.0.x` 之后的独立优化系列。字面 `1.1.x` 只表示系列；每个可执行候选必须
使用 `1.1.0、1.1.1……` 等精确版本，并保证客户端、后端、OmniAuto、合同、Schema、manifest
和安装包按实际影响范围同步。“客户端检查更新”和“耗时观测清理”已提前进入灰度 `0.9.59`；
“运营后台知识管理”已提前进入灰度 `0.9.61`，均不再列为待开发项。仍属于 `1.1.x` 的清单如下：

1. 人工接管受限监听：`waiting_sales_reply` 期间只读取足以证明销售是否实际回复的会话证据，不转写
   客户语音、不复制客户图片、不调用 Vision、不调用 Brain；销售回复必须发生在 HandoffEvent 之后，
   关闭 handoff 后才恢复普通 C2。该能力不得与当前低频完整读取并行形成两套权威状态。
2. C2 定位性能：复用首屏负向结果，使 visible 到短码搜索处于同一 Flow，并评估搜索框和标题 ROI OCR；
   不得删除后端授权、private/短码确认、唯一候选或最终标题确认。
3. 语音转写性能：只减少 prepare/execute 的等价帧重复 OCR、右键菜单 ROI 和已满足条件后的多余等待；
   不减少身份跟踪帧，不改变 action ID、Journal 终态或歧义失败关闭。
4. 加好友 UI 性能：优化搜索框、资料页、邀请表单及确认后页面 ROI OCR 和有上限等待；不删除字段复核、
   唯一联系人确认、最终确认或页面收尾。
5. Brain 首次输出质量：提高首次结果满足现有合同和 Guard 的概率，减少修复链比例；不删除语义复核、
   Guard、质量修复或结果验证，Brain 期间继续持有当前会话逻辑 UI 锁。
6. 首屏扫描成本：在不改变未读代次、短码/private 准入和扫描只读语义的前提下，评估 ROI、稳定区域
   复用和 OCR 调用瘦身；单独红点仍不能制造新消息事实。
7. 无业务语义代码瘦身：合并重复截图/OCR 辅助函数、重复序列化和重复报告构建；不得改变 UI 动作顺序，
    不得新增第二套 Sidecar 生命周期或跨会话可变缓存。
8. C4 性能优化：只在获得独立真实召回链路样本后评估，不使用 C2/C3 样本推算。
9. 抖音自动接入：抖音 API、企业私信 Webhook/OAuth、小风车或巨量引擎同步统一作为一个渠道接入专题，
    复用现有线索去重、分配和审计，不在客户端增加第二套线索权威源。
10. 人工输入保护条件评估：悬浮球、键鼠 Hook 或快捷键只有在证明可独立关闭、不改变业务状态、不持有
    UI 锁且不会使任务永久 executing 后才允许重新设计；未通过独立 Windows 安全验收不得实现。

上述每项优化必须使用独立开关、受影响范围测试和真实耗时/安全指标，可单项关闭回到 `1.0.x` 稳定路径。
任何优化都不得以降低错误会话读取、错误发送、重复发送、漏读或媒体重复操作门禁为代价。

## 9. 本次文档收口

2026-08-11 起，项目级有效内容只进入 PRD、技术方案、版本更新记录和全流程图。
被合并文件删除后不得重新创建同义“最终版”“专项版”“封版版”或个人交接文档；需要补充内容时，
按内容性质直接修改四份文件中的对应一份。

## 10. 主要版本演进摘要

| 版本阶段 | 主要变化 |
|---|---|
| PRD v0.1—v0.4 / 技术方案早期版本 | 建立 C0 线索分配、C1 Windows Worker 加好友、C2 会话绑定、C3 AI 回复和 C4 召回的总体分工。 |
| PRD v0.4.5—v0.4.6 / 技术方案 v0.8 | C2 收口为固定短码、private 单聊、服务端授权下的文字/语音/图片统一事实链；C3/C4 共用 `chat_reply`。 |
| PRD v0.5—v0.5.3 | 增加车辆 Product Master、运营后台登录、指定账号全权限和 Worker 统一工作台。 |
| PRD v0.5.4 | 飞书通知改为车金统一自建应用；服务端按手机号取得当前应用 `open_id`，创建 `HandoffEvent` 并进入 `waiting_sales_reply` 后立即且至多通知一次。 |
| 技术方案 v0.8.6—v0.8.10 / 合同 3.12.8—3.13.2 | 明确读取轮次、媒体事实归属、语音 prepare/execute、逐帧目标跟踪、有限终态、UI 变化后刷新、媒体编排进入条件、权威画面唯一枚举和正常主流程门禁。 |
| PRD v0.5.5 / 技术方案 v0.8.9 / 本记录 v1.1 | 删除 PRD 与技术方案中的变更列表、旧候选、历史基线和实现过程；两份正文只保留现行方案，历史与发布证据统一归档到本记录。 |
| PRD v0.5.6 / 技术方案 v0.8.10 / 全流程图 v0.3 / 本记录 v1.2 | PRD 仅同步当前技术方案引用；技术侧补齐正常流程不得误入语音编排器、无媒体动作保留 `initial_read`、媒体动作后统一 `final_read` 及三值枚举合同；目标机器合同升为 `3.13.2`。 |
| PRD v0.5.7 / 技术方案 v0.8.11 / 全流程图 v0.4 / 本记录 v1.3 | 区分“列表重排后点到不含目标短码的其他会话”与“目标短码已确认但会话类型不安全”；前者允许零消息副作用前完整重新定位一次，后者仍失败关闭。机器合同 `3.13.3` 已同步实现并通过定向复审，待双仓来源治理、提交和发布验证。 |
| 灰度统一版本 v0.9.0 | 将上述现行产品、架构、流程和合同能力统一纳入同一个灰度编号；PRD、技术方案、全流程图、版本记录、客户端、后端、OmniAuto 合同 revision、Schema、manifest 和安装包不再分别编号。旧编号仅供本记录追溯。 |

版本摘要只用于追溯，不得覆盖 PRD 和技术方案中的当前规则。需要了解某个历史提交、测试、包哈希或
阻断原因时查本记录；工程实施不得从历史版本摘要反推当前行为。
### 0.9.21 架构复审补充（2026-08-20）

- 删除未上线的 Vision 无类型 direct-port 旧主机兼容入口；缺少可信角色、图片气泡边界或稳定物理锚点时，在任何 UI、剪贴板和 Provider 动作前失败关闭。
- PR28 受保护文件检查改为一次报告全部不一致项，不再遇到首个哈希差异就提前结束；经授权完成动态布局完整差异与九项语义门禁复核后，独立 OmniAuto 保护基线同步到当前候选。
- 本次只执行 Vision、动态布局、截图/OCR 和点击门禁的定向测试；全量回归仍只在正式打包门禁执行。
