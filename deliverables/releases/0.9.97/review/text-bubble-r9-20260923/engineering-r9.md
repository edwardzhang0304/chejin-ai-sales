# TBG r9恢复入口补修与自审（2026-09-23）

## 结论及责任

本轮自审完成，候选待架构师独立复审，未提交、推送、打包或发布。上一轮自审漏了实际恢复回复任务入口，只凭正常发送前/段间结算通过就交审，是覆盖遗漏；本轮不再把测试总数当入口完整性的证明。

修复架构师发现的pending/running回复恢复漏传客户编号，同时全调用核对发现`reply_sequence_runtime.resume_reply_sequence`存在同类漏传。未更改OCR、声波门槛、后端身份校验或恢复规则。原7个无声波旧夹具、共享顶部裁切存量失败仍未计为通过，旧错误历史未自动恢复，Windows实发及现场验收仍待进行。

## 实际修改

- 共用`_settle_chat_reply_context_failure_before_unlock`的`conversation_id`从可选空默认改为必填；所有9个生产调用点均显式传`target.conversation_id`，不猜身份。
- `_execute_c2_reply_recovery`的3个出口及正常批次另外3个出口补显式身份；原正常发送前/段间2处保留；分段恢复模块1处补齐。真正可触发本次新分组失败的恢复读取出口及分段续接出口均新增正式父流程验证。
- 仍复用r8原Flow回执依赖、原失败接口、原补交线程和后端原结算；r9没有新增产品模块、状态机、后端逻辑、合同字段或版本。
- 架构师原`test_recovery_entry.py`逐字节复制固化为`worker-client/tests/test_reply_recovery_grouping_failure.py`，2个反例不改断言；5个直接结算单测因必填参数显式传其原任务客户ID，业务断言未改。
- 四份权威文档在主目录更新为0.9.97/r5：TBG候选状态、实际回执字段、原Flow依赖、pending/running及分段恢复入口、测试/实机边界。候选Markdown同步r5/r4/r3说明，原r2及更早历史逐字保留；未用主目录旧产品源码覆盖候选。PUML仅当前图注释更新及沿用r3历史标签，6图块结构检查通过；本机无Java/PlantUML，未渲染。

## 本轮新鲜验证

| 范围 | 结果及原始记录 |
|---|---|
| 原架构pending/running反例 + 5项直接结算 | 7通过，进程0；`r9-unit.xml`，2.11秒 |
| pending/running正式恢复入口 × 请求未到/提交后丢响应 | 4通过，进程0；`r9-recovered-task.xml`，43.73秒 |
| 分段重启正式tick入口 × 请求未到/提交后丢响应 | 2通过，进程0；`r9-sequence-v4.xml`，31.47秒 |
| 恢复旧漏传的反向包装 | 3通过，进程0；`r9-recovery-reverse.xml`，15.24秒；每项捕获同一正例缺少失败回执的预期失败 |
| 改为必填后原发送前/段间4场景及原2反向包装 | 最终6通过，进程0；`r9-existing-entry-v2.xml`，53.50秒。首次断言6通过但进程退出134，详见下节，不能将首次当干净通过 |

以上22个参数化用例不是22条独立业务流程；不与r8或共享镜像数量相加。未重跑未改动的原图OCR、声波、后端9项身份/发送保护或全仓测试。其实现指纹仍与r8独立通过版本一致，证据按原轮次沿用。

### 真实路径与边界

新6项由正式异步回复生成自动创建任务，测试不手插任务。pending通过真实HTTP拉取；running通过真实claim取得原fencing，再拉取running任务。随后正式`_execute_task`调用`_execute_c2_reply_recovery`，收到受控桌面解析错误，保存SQLite依赖后提交真实HTTP/PostgreSQL失败结算。请求未到或已提交丢响应均保持faulted和同一Flow，未把本地记录改成已结清。

分段例第一段先用正式HTTP许可/sent_ack建立已发事实；正式批次入口自行保存`reply_sequence_flow`标记，到桌面读取边界退出进程。第二进程保留同SQLite和原Flow，等旧UI租约自然过期后由正式`tick_once`完成心跳、恢复核对和`resume_reply_sequence`，不手填后端状态或跳过锁保护。这里是发送前重新读取，失败阶段沿原`pre_send_refresh`。

第三阶段使用同一SQLite真实`runner.start`后台恢复线程补交。断言：恢复阶段零桌面读取、零重复发送；原Task失败及未发余段取消、原Flow清除、Handoff=0；已发首段及sent_ack数量保持。随后使用真实WebChannel `startAccepting`处理函数与后台恢复操作，后端和SQLite均running。Qt渲染、微信窗口/截图、模型是受控边界；running不等于已处理下一客户或Windows物理发送。新6项没有冒称下一客户实发通过，原r6下一客户证据另列。

反向测试仅在子进程用AST恢复实际调用方的漏传参数，同时恢复共用入口空默认；不改正例输入或断言、不改磁盘产品源码。pending/running/分段3例均因`reply_read_failure`未保存而失败。其余身份及发送许可保护保持r8原代码及原独立证据。

## 失败、调试与未验证

- 分段测试最初直接调用续接函数，没有先由正式心跳装载后端Flow；崩溃进程UI锁也需到期。`r9-sequence.xml`、`v2`、`v3`均原样保留为失败，不计通过。最终测试改从正式tick进入、等待原锁自然到期，未删除产品检查，`v4`通过。
- 原入口回归首次输出6项通过后，Python3.13主进程退出时报告`libc++abi ... recursive_mutex lock failed: Invalid argument`，退出134；XML保留为`r9-existing-entry.xml`。相同代码、断言及环境复跑6项，退出0，`r9-existing-entry-v2.xml`。退出异常根因【待确认】，仅确认此次复跑未重现，不声称已修复运行库问题。
- 7个原无声波夹具失败、共享顶部裁切absent/ambiguous基线失败，继续见r8报告；本轮没有改成通过。真实原PNG与语音实际OCR结果沿用相同源码指纹，不冒称本轮再次识别或Windows转写。
- 只使用一次性PostgreSQL容器`codex-tbg-r9-test`和本机HTTP；已停止并自动移除。无生产数据、销售现场文件或API key修改。

## 可复核材料

- 产品：`/private/tmp/cj-ocr-bubble-fix-20260923`，基线`89dbcf097c39076441d5d79caadacfa2ff9848cb`加未提交差异。
- 共享：`/private/tmp/cj-bubble-shared-fix-20260923`，基线`a2026d1570a84abcda02de9a67844c540bf1aead`加原r8差异；r9未再改共享代码。
- 主目录四文档：`/Users/zhangwentao/Documents/车金/deliverables/`的0.9.97技术方案、PRD、流程图和版本记录，当前r5。
- `r9-callsites.json`：9个调用点及身份来源；`r9-fingerprints.json`：最终文件指纹；同目录各XML及对应temp保留进程输出、原SQLite。
- 原独立审计：`/private/tmp/cj-r8-review-20260923-NTZpwC/review_v0.1_20260923.md`。原反例副本与原文件逐字节一致。

本报告只是实现/测试证据，不是第五份业务规范；本轮最终放行待独立复审。未新增分支，未提交推送。之前r8自审结论的范围由本次及原独立P1审计明确收窄。
