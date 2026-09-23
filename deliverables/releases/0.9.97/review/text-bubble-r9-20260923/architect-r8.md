# 气泡修复r8独立复审

日期：2026-09-23。审计者：架构师4号。唯一业务依据仍为主目录0.9.97/r4 TBG-1—7；本文件仅为测试证据，不增加第五份项目规范。

## 结论

**暂不通过：已提交的发送前／段间断网补交流程通过独立验证，但同一修复漏接“恢复待回复任务”入口，仍有1个P1。** 不需推翻OCR或新结算方案；补齐既有调用参数并覆盖恢复入口即可继续复审。未发现要求重做整体架构的依据。

## P1：恢复回复任务时遗漏conversation_id，失败回执根本没有保存

- 位置：产品 `worker-client/chejin_worker_client/task_runner.py:8203`，`_execute_c2_reply_recovery`的读取失败分支。该函数开头已从原task.c3取得conversation_id，并构造了target；调用 `_settle_chat_reply_context_failure_before_unlock` 时却没有传它。
- 接收函数7872仍将conversation_id默认设为 `""`。遇到新分组错误时，7929将空值交给 `reply_read_failure.save`；后者13—15行按既有身份保护抛出 `REPLY_READ_FAILURE_SCOPE_INVALID`，在任何保存/HTTP之前停止。
- 对照：正常批次的发送前与段间父入口已在18941和19027传入target.conversation_id，因此这两条能通过，不代表恢复任务入口也接好。
- 真实后果：Worker虽faulted，但没有reply_read_failure回执，也没有提交原任务失败。原任务仍非终态；tick_once最终尝试task_terminal时，后端worker_service.py:562—575要求任务必须completed/failed/cancelled，不能把未结清任务直接结束。原自动补交没有这份失败回执可以重放，仍会挡住安全恢复。这与TBG-5统一故障结算要求冲突。
- 可达性：正式tick_once在task_runner.py:7491—7494对chat_reply调用此恢复入口，覆盖后台拉回pending回复和恢复running回复，不是人为构造的闲置方法。

### 独立反例与因果对照

`test_recovery_entry.py` 用真实TaskRunner、SQLite、正式Sidecar异常封装及正式恢复父子调用，API和微信边界受控。pending/running两例都确实读取到分组错误、停单且零发送，最终均没有失败回执、没有调用失败接口；日志准确为 `REPLY_READ_FAILURE_SCOPE_INVALID`。按应保存并补交的断言，两项失败，见recovery-entry.xml和recovery-entry-temp下proof.json。

仅在独立测试内存中给该调用补传已知原conversation_id，其他断言和产品文件不变，两例通过，见recovery-context-control.xml。这是定位原因的受控对照，**不是已修好产品或真实后端验收**。不会以测试替程序补字段的版本作为交付通过凭证。

### 最小整改

1. 此入口传入已有的 `conversation_id` 或 `target.conversation_id`，与正常父入口使用同一保存、HTTP、补交实现；不要在公共层凭当前微信画面或默认值猜客户。
2. 核对该共用结算函数所有调用点。对需要此身份的分支补回归，防止可选默认参数再次掩盖漏传；不新增重试器、表或恢复状态机，不修改OCR与后端身份保护。
3. 保留独立pending/running反例；在已有隔离HTTP/PG测试中补正式 `_execute_c2_reply_recovery` 入口的任务失败保存、断网同库补交、原任务/Flow终态及开始接单。原四场景用的是 `_wait_and_send_current_c3_batch`，不能再拿它们替代此入口。
4. 修改后同步四份权威文档的实现/验收状态及原Flow回执依赖说明，保持0.9.97应用/合同与未发布边界。当前主目录r4仍登记早期整改状态，不能直接当作r8已通过。

## 本轮独立执行结果

| 范围 | 结果 | 原始证据 |
|---|---|---|
| 发送前/段间×请求未到/提交后丢响应，同SQLite重启补交及正式开始处理 | 4通过 | http.xml / http-temp |
| Flow/客户端/客户/阶段/fencing与sending/sent/unknown保护 | 8通过 | http.xml |
| 原running租约过期，只补未发送终态、幂等、不续租 | 1通过 | http.xml |
| 去保存、恢复旧错误码映射的反向包装 | 2通过：包装捕获原正例的预期失败 | http.xml |
| 真实声波像素＋绘制转写文字，真实OCR→parser输出→转写执行衔接及跨帧旧证明拒绝 | 8通过 | voice.xml / voice-temp |
| 恢复任务父入口遗漏身份反例 | 2失败 | recovery-entry.xml |
| 仅补传身份的内存因果对照 | 2通过，不算产品修复通过 | recovery-context-control.xml |

前五行合计15项HTTP/数据库检查与8项语音检查；不是把工程师369项再相加。独立HTTP15项干净退出0，用时65.27秒；语音8项退出0，用时71.86秒。保留框架弃用与pytest XML属性警告，未混报成业务失败。

四个恢复正例使用正式异步生成回复，不由测试插入任务；故障前创建正常绑定，重启读取原faulted SQLite，不重写状态。发送前失败回执在HTTP之前保存，后端响应丢失后重交同一回执，原OperationLog核对幂等；未发余段结束、Handoff为0。段间前置的第一段由测试走正式HTTP许可/回执建立已发状态，并非Windows物理发送。重启期间未重复桌面读取/发送；开始使用真实WebChannel startAccepting和后台恢复处理，后端及SQLite running。**此四例止于恢复running，没有重新完成下一客户任务；原r6下一客户调度结果有独立证据，但不冒称r8此四例也都发送了下一条。**

语音测试没有补写parser丢失的声波证明或转写正文，门槛产品代码未变；执行阶段通过测试夹具喂入已经真实OCR解析的结果，菜单、截图、准备态/选择票及动作边界受控。它证明识别结果能被后续执行消费，不等于在Windows点菜单后实际读出微信转写。已说明的7个无声波旧夹具失败和共享顶部absent/ambiguous存量失败不能算通过；本轮不要求放宽规则去使错误夹具变绿。

## 分工、统一规则与代码质量

- OmniAuto r6解析/整行识字未变；没有Worker或后端拼字，历史HC与声波门槛未改变。
- 新Worker小模块把回执作为原Flow finish的依赖，复用原重试与重启线程；后端小模块校验原身份/发送状态并进入原取消未发余段逻辑，使用原OperationLog。不因为两个模块各自验证职责就判成两套恢复状态机。
- 常规发送前/段间现在保持新错误语义，不再走旧转人工码；后端不删除旧通用码的其他业务含义。缺陷是恢复调用方没传同一身份，不是后端应放宽校验。
- 外层读取finalizer改为保留原receipt字段，防止覆盖补交依赖；未见本轮新增无上限重试、重复物理发送或盲目修历史。持久化与接口重试的实际通过范围按上表，不推定所有出口已闭环。
- 本轮只审受影响解析、失败结算、恢复和语音夹具，不以大文件体积为缺陷，不扩展其他业务重构。建议把身份参数漏传列入共用入口回归，属于小范围代码质量整改。

## 来源、边界与清理

候选 `/private/tmp/cj-ocr-bubble-fix-20260923`，HEAD89dbcf097c39076441d5d79caadacfa2ff9848cb加r8未提交差异。关键实测SHA：

- task_runner.py：5d722545240427e4a9fd8bc0cbe7809b4b696ebeb09ea00ede2cd43ecc5b24c4
- Worker reply_read_failure.py：06ac7be80fa077f02e44542bee60e48ad0b4a2ac94beee9989a0db54835eef54
- 后端reply_read_failure.py：35d66d9337d1229a2276487b6dcad8639c71b14358a1ac6175fc43f3bfd8647b
- Sidecar：9b6cc1cfbe679423b02abb4ee8c20502b7c6925bc565d82c9a2f732bdc3b99c3（与r6独立原图通过基线相同）

git diff --check通过。未改产品、权威文档、现场历史或发布状态；未提交推送。两张事故原PNG真实OCR结果沿用相同Sidecar/OCR引擎指纹的独立证据，不重复报为本轮新鲜OCR。Windows正式包、实发与旧错误历史更正仍待验/待授权。

专用PostgreSQL容器cj-r8-independent-review-20260923（精确ID9c5eea12b486e856ecd1cc4d82961e6cdd02629d729f6f64f95b83b98ea6690c）已核对标签后删除，包括可重建临时测试卷；未动业务/工程师容器。XML、日志、SQLite和失败证据留在本目录。
