# 旧／新消息统一规则分支复审

版本：v0.1；日期：2026-09-22。范围：当前灰度工作区的 OmniAuto → Worker → 后端历史消息判定及其消费者；追加核对 0.9.95/r5 的最小补修。不代表整个项目、任意环境或已安装二进制均无缺陷。

## 结论

1. **除已知的可见尾帧下标 P1 外，本轮未确认新的、正常生产场景可触发的统一历史规则冲突。** 已追踪提前成功、提前失败、确定性兜底、证明传递及后置消费者；不是只检查是否 import 了公共函数。
2. **已知 P1 的 r5 最小补修在本轮源码复核中通过。** 原独立反例不改断言已通过；另一项 HC 96 分可见后缀检查通过；16 项正式入口及边界测试通过。内存撤去映射后，4 个后缀正例重新失败、5 个完整历史对照仍通过。此结论关闭本轮已核验的下标混用问题，不等于整版发布或 Windows 现场验收通过。
3. **另记录一项大包分片／HC 证明的条件性兼容风险，不计为已确认现场 Bug，不定 P1/P2。** 生产拆包与校验函数确实可表现出冲突，但本轮依赖人工填充字段才达到拆包门槛，尚未证明正常生产者能生成该输入。不得据此扩张改造或宣称普通聊天也必然失败。
4. r5 旧故障恢复测试确实复用先前 Worker 自己产生故障的 SQLite，恢复阶段未用测试重写 binding／账本／Outbox／暂停状态来补成功。但测试禁止领取新任务，只证明原记录结清后，经正式操作入口恢复 running；**不证明高磊现场已经恢复，也不证明下一单已完成。**

本轮未改产品、权威技术文档、生产数据或 Git 分支；未提交、推送、打包、部署；未向客户端工程师发送指令。仅新增本目录审计脚本及证据。没有发现需要另立一套历史匹配流程的设计变更，本轮未修改权威文档。

## 审阅基线与变化

- 车金：`/private/tmp/chejin-customer-interrupt-20260916`，`codex/gray-release-0.9.x`，HEAD `d13f52f10a13a6a444e71a799ce1c4a9c6a4c3b2`，含未提交改动。
- 共享：`/private/tmp/omniauto-customer-interrupt-20260916`，`codex/gray-release-0.9.x-source`，HEAD `8412703c7a0961eca5523d542143e189dda0a538`，含未提交改动。
- 主审检索指纹覆盖 backend/app、Worker 业务包、bundled adapters 共 241 个 Python 文件，得到 54 个含目标调用的函数记录。**这是检索地图，不是 241 个文件逐行审计或 54 条独立场景全覆盖的声明。** 起止生产文件未变化，见 [start.json](/private/tmp/cj-history-branch-audit-20260922-MhyvZf/start.json)、[end.json](/private/tmp/cj-history-branch-audit-20260922-MhyvZf/end.json)。
- 并行核对期间，独立共享仓的历史模块、工程师测试及文档有更新；最终按 r5 当前源码和调用参数核验。未把缺少新参数的旧内部调用当成现有产品缺陷。
- r5 清单 18 份源码／测试／文档哈希、6 批 XML 哈希及计数均相符；选定的 8 个历史规则／Sidecar 核心副本逐字一致。**不声称两仓所有文件一致**；扩展镜像盘点有 18 个其他文件不同，差异本身不是已证明的历史规则冲突。见 [r5-artifact-check.json](/private/tmp/cj-history-branch-audit-20260922-MhyvZf/r5-artifact-check.json) 及 [扩展指纹](/private/tmp/cj-history-branch-audit-20260922-MhyvZf/sidecar-JXSETY/hash-comparison.json)。

## 真实入口及后续消费者复核

| 路径 | 核对内容及本轮结果 |
| --- | --- |
| 普通读消息、当前屏补读 | 粗签名仅产生候选；有历史时仍进入统一正文判定。正文有差异却没有合格证明时拒绝，不因去标点后的签名相同而提前通过。 |
| 发送前、分段之间 | 调用相同 checkpoint 比较；r5 用冻结帧已有消息身份映射完整历史。后续新消息检查只处理新后缀，不拿旧文字重新按另一标准拦截。 |
| OmniAuto 输入前、输入后、输入框扩大 | 都进入相同历史 guard；裁切重试仍走此入口。客户插话的 Worker／后端消费者重算相同证明，不自行把历史文字还原成另一套严格判断。 |
| 发送后确认、客户马上回话、AI 归属 | 旧重叠使用统一历史规则；本次真正发出的新正文和原回执真实性单独核验。已确认 AI 回执成为历史后，归入同一历史比较。 |
| 图片、语音动作前后 | 各粗比较后追到统一 reconcile；最终提交从权威历史和最终完整帧重建证明。媒体目标和动作回执严格核验是身份保护，不是第二套旧文字相似度。 |
| 后端入库、普通重复上报、唯一约束冲突重查 | 都使用经过共享校验器独立验证的 historical pairs；未发现一个分支放行、另一个仍按旧原文相等拒绝的现实反例。 |
| 故障补交、重启、授权刷新 | 冻结原包不重新读微信；授权刷新不改原观测正文。r5 两例原故障库补交与响应丢失重试证据成立，仍需显式恢复接单。 |

可定位的主要生产入口：

- [普通读取 gate](/private/tmp/chejin-customer-interrupt-20260916/worker-client/chejin_worker_client/task_runner.py:10511)、[发送前调用及身份参数](/private/tmp/chejin-customer-interrupt-20260916/worker-client/chejin_worker_client/pre_send_checkpoint.py:553)、[段间复用](/private/tmp/chejin-customer-interrupt-20260916/worker-client/chejin_worker_client/reply_sequence_runtime.py:32)。
- [统一历史比较及基线映射](/private/tmp/chejin-customer-interrupt-20260916/worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/historical_text_alignment.py:53)、[Sidecar guard](/private/tmp/chejin-customer-interrupt-20260916/worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py:17693)、[发送后历史规则](/private/tmp/chejin-customer-interrupt-20260916/worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/text_correspondence.py:378)。
- [后端证明复核](/private/tmp/chejin-customer-interrupt-20260916/backend/app/services/historical_text_alignment.py:76)、[普通重复处理](/private/tmp/chejin-customer-interrupt-20260916/backend/app/services/wechat_service.py:4751)、[唯一约束冲突重查](/private/tmp/chejin-customer-interrupt-20260916/backend/app/services/wechat_service.py:4905)。

完整分支及排除理由：[Worker](/private/tmp/cj-history-branch-audit-20260922-MhyvZf/worker-A6YoUa/review.md)、[Sidecar／公共规则](/private/tmp/cj-history-branch-audit-20260922-MhyvZf/sidecar-JXSETY/review.md)、[后端](/private/tmp/cj-history-branch-audit-20260922-MhyvZf/backend-fQWUyR/report.md)。主审已读取并核对这些报告，不把它们的静态检查写成本轮动态测试。

### 保留严格检查，不混为“旧规则没统一”

客户／会话身份、语音图片归属、同一截图中原始证据与其序列化内容的一致性、实际新发送正文、不可篡改的原成功回执及未知发送不能重发，职责与历史 OCR 对应不同。它们不能套用 90 分相似度放宽。独立产品 Connector／向上翻页 helper 不在车金当前定向单帧发送路径，不因存在旧函数就认定生产调用冲突。

两帧公共比较返回 None 的确定性兜底、旧 self 气泡结构重分类等分支也已追踪，但未形成当前可达的有效反例；未列为产品 Bug。相似度用于旧消息身份对应，不保证新消息第一次 OCR 不出错。

## r5 最小补修与实际验证

现在后台完整历史 A/B/C、屏幕只剩 B/C 时：Worker 提交 B/C 的已有身份；共享规则先映射为完整历史第 2、3 条，再比较同一正文。本地连续性结果仍使用可见帧下标 0/1；发送给后端的证明保留完整历史下标 1/2 和原检查点摘要。不截短历史或重签检查点，90 分／5 分差值未变。

| 本轮独立执行 | 结果 | 边界 |
| --- | --- | --- |
| 先前独立可见后缀反例，断言不变 | 1 通过 | 实际公开 pre-send 消费者，构造合法输入；不是 HTTP 或 UI |
| 另一项 96 分 HC 后缀、公共验证器及实际消费者 | 1 通过 | 全局证明与局部结果分开，原输入不变 |
| 正式 test_history_punctuation_handoff.py | 16 通过 | 含五类入口、正常／标点后缀、追加新消息、低分及五种身份／投影保护 |
| 在独立进程内禁用映射，同样断言 | 4 后缀正例失败，5 完整历史对照通过 | 预期反向失败；只替换内存函数，未改源码 |

XML：[原反例](/private/tmp/cj-history-branch-audit-20260922-MhyvZf/known-suffix-current.xml)、[HC 后缀](/private/tmp/cj-history-branch-audit-20260922-MhyvZf/hc-suffix-current.xml)、[16 项](/private/tmp/cj-history-branch-audit-20260922-MhyvZf/r5-entry-boundaries.xml)、[反向对照](/private/tmp/cj-history-branch-audit-20260922-MhyvZf/r5-baseline-negative.xml)。各批有重叠，不累计成完整流程覆盖数。

运行解释器：`/private/tmp/chejin-contract-audit-20260919-UdTyUH/venv/bin/python`；Worker／测试／bundled adapter 从候选工作区导入，OCR 依赖目录只用于模块导入；`CHEJIN_API_BASE_URL=http://127.0.0.1:1`，Worker 数据目录隔离在本证据目录。pytest 禁用缓存写入候选工作区，源码哈希起止相符。

保留探索失败：[hc-suffix.xml](/private/tmp/cj-history-branch-audit-20260922-MhyvZf/hc-suffix.xml)。首次直接调用内部 helper 时未传刚加入的 old_identities，但当时正式公开消费者已传参且通过；纠正独立探针后再次通过。**此失败不算产品缺陷，也不算反向控制成功。**

### 旧故障库恢复，不只看新流程

本轮额外核对 r5 原始测试和日志，未新启动数据库或重跑 HTTP 批次；上轮独立原库恢复证据仍保留在 r4 审计目录。不能把工程师 64 项 HTTP 结果写成这次主审新跑的 64 项。

- [故障产生及恢复调用](/private/tmp/chejin-customer-interrupt-20260916/backend/tests/test_historical_confidence_worker_http.py:265)：先让正式 Worker 走旧门禁行为，收到真实 HTTP409，并自然留下故障 SQLite；随后恢复进程复用同一 Worker 数据目录。这是当前源码模拟旧判断，不是旧安装包或销售现场原库。
- [existing_database 分支](/private/tmp/chejin-customer-interrupt-20260916/backend/tests/test_contract_equivalent_recovery.py:199)：只读原 binding／Outbox，断言 faulted、原包未变、原错误码、attempt_count=1；预置写入在未执行的 else 内。普通其他测试仍可走 else，不混称所有测试都无预写。
- [正式开始接单入口](/private/tmp/chejin-customer-interrupt-20260916/backend/tests/test_contract_equivalent_recovery.py:259)：先确认补交和账本归零、仍 faulted，再调用 set_run_status('running')，等待内存及持久化状态均 running；外层还核对后端 running、无重复消息或转人工。
- [正常日志](/private/tmp/cj-punctuation-fix-20260922/suffix_http_regression-tmp/test_confidence_read_reaches_a5/closed-recovery.log)一次 HTTP200；[丢回执日志](/private/tmp/cj-punctuation-fix-20260922/suffix_http_regression-tmp/test_confidence_read_reaches_a6/closed-recovery.log)两次 HTTP200，损失注入发生在服务器真实提交之后；均原包不变、零物理操作。can_pull_tasks=False，随后停止 Runner，**不证明下一单执行或界面实际点击。**

## 条件性风险：大包分片与整帧证明不配套

实际代码：Worker [拆包调用](/private/tmp/chejin-customer-interrupt-20260916/worker-client/chejin_worker_client/task_runner.py:14392) → [非末分包裁掉部分 observations](/private/tmp/chejin-customer-interrupt-20260916/worker-client/chejin_worker_client/c2_outbox_recovery.py:225)，仍携带整帧 HC 证明 → [后端 verified_pairs](/private/tmp/chejin-customer-interrupt-20260916/backend/app/services/historical_text_alignment.py:76)依据该分包现有 observations 重算，可能得到 `TEXT_CORRESPONDENCE_PROOF_INVALID`。这不是换了分数门槛，而是传给同一规则的证据范围不同。

主审重跑的有界探针结果：[partition-result.json](/private/tmp/cj-history-branch-audit-20260922-MhyvZf/partition-result.json)。整包 2,022,095 字节，96 分证明通过；按原 1.5MiB 目标拆分，第一包裁到 15 条观测而保留原证明，复核拒绝；最后一包保留完整 23 条观测，通过。

限制明确：探针给 20 条语音元数据各填充约 100KB 的非生产字段才达到门槛；未降低真实拆包阈值。使用正式 builder／splitter 及原样提取的 backend verified_pairs，权威查询替身，**没有 HTTP／数据库／完整 Schema 准入**。目前没证明生产端能产生这样的合规大包。只登记兼容风险；若后续有真实大包，再按同一帧完整证据合同处理，不能简单关闭后端校验。本轮不要求工程师立即修这一候选。

## 最终边界

本轮主要是代码分支审查，少量有针对性的独立正反例；未逐场景铺设全部测试，符合用户“不做不真实场景的过度审计”要求。没有重新跑真实 OCR、Windows 微信发送、真实模型或生产恢复。已知下标 P1 源码复核通过、新增确定冲突为零，不等于保证以后不会再出 Bug，也不代替正式联合版本验收。
