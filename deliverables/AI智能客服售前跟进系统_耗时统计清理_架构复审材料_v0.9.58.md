# AI 智能客服售前跟进系统耗时统计清理架构复审材料 v0.9.58

## 1. 当前状态

- 基线：`gray-v0.9.57` / `f63613a51157c480a442e996ea8b211b3ba484fe`
- 分支：`codex/gray-release-0.9.x`
- 目标版本：`0.9.58`
- 机器合同：`0.9.58 / 47185e439b6fced86dad8d8d607efc1efa4764775f0a19d5e629cb945bca2a10`。
- 状态：架构复审已通过；独立 OmniAuto 已形成真实提交 `2a4a7ed9e459421c53865b0bbc9eeb9d88b3dd09`，`.chejin-source.json` 已按顺序更新；车金仍未提交、未推送、未打包。

## 2. 生产修改

1. 后端和 Worker 从同一机器合同加载 26 个标准阶段名称，删除两份硬编码阶段清单。
2. 修复 Worker telemetry SQLite 成功写入未提交的问题；失败时回滚，所有写入继续 best-effort。
3. Worker 本地库只保留有界待上传标准事件、永久 4xx 隔离证据、process link、stage attempt 和后端权威快照；上传仍为异步旁路。超时/断网/408/425/429/5xx 退避重试，不可重试的其他 4xx 隔离后永久停止重传。
4. C2 删除业务 ingest `evidence.timing` 副本和 `c2_message_read_timing` 重复结构化汇总，保留内存中的原始单调阶段时间并映射一次。
5. C3/C4 标准发送耗时恢复为 Worker 外层单调计时：从 Sidecar 进程启动前到 Sidecar 返回或抛异常后，包含启动和 JSON 通信；Sidecar 内部耗时仅作诊断。
6. 新 UAT 证据包停止生成三份旧 timing 报告，输出后端在正式上传响应中计算的权威报告快照、process link、待上传事件和 4xx 隔离事件。Worker 不在本地重算报告。
7. Worker/后端崩溃后开放观测阶段结算为 `abandoned`，不猜测耗时；观测恢复失败不阻止客户端或后端业务启动。
8. 关闭观测时，生产代码不创建新 telemetry 事件、process link、上传任务或空 telemetry 数据库。
9. C0 线索进入与轮询自动分配已加入关/开直接对比；API、分配调用、审计事件和最终线索/任务事实完全一致。
10. C3/C4 发送的每次真实重试增加 attempt 并分配全新 `stage_run_id`；同一 reply action 的后续尝试不会覆盖第一次耗时，旧 attempt 元数据被有界淘汰后也不会发生 ID 碰撞。
11. `telemetry_process_links` 和 `telemetry_stage_attempts` 各只保留最近 5000 条，并共同服从 64 MiB 总容量门禁。

逐字段写入者、读取者、标准阶段和处理结果见[耗时字段盘点](./AI智能客服售前跟进系统_耗时字段盘点_v0.9.58.md)。

## 3. 明确未修改

- C0—C4 业务步骤和状态机；
- 截图、OCR、语音、图片、Vision、Brain、鼠标键盘及发送调用；
- 消息身份、Ledger、ActionJournal、业务 Outbox、sent_ack；
- Handoff、飞书、UI 锁、租约、业务重试和业务终态；
- 业务使用的租约/超时/冷却/120 秒/召回等待等计时器。

## 4. 静态审计

- 新生产代码不再写 `c2_message_read_timing`。
- 新证据生成器不再写：
  - `timing/flow_timing.json`
  - `timing/brain.json`
  - `timing/send_confirm_ocr.json`
- `ProcessStageRun` 的生产读取仅存在于观测服务和任务响应中的 `process_run_id` 关联展示；未发现接单、重试、发送、Handoff、UI 锁或业务终态读取。
- Worker `pending_stage_events` 只由 telemetry 上传器读取；业务编排不读取事件内容作决策。
- 26 个标准阶段均在正式生产映射点出现，Worker 与后端阶段集合完全一致。

## 5. 自动化证据

### 5.1 观测关闭/开启业务中立矩阵

每条测试内部对同一生产入口先关闭观测、再开启观测，并直接比较前、中、后事实：

- Worker：C1 加好友、C2 读取与 ingest、C3 claim/send/sent_ack，`3 passed`。
- 后端：C0 线索进入/自动分配、C4 生成/领取/发送/结算、Handoff 创建/飞书通知/数据库结算，`3 passed`。
- 每条都直接比较前置任务与会话输入、中间外部调用次数/顺序、后置 SQLite 或后端数据库业务事实，不再以“旧测试分别跑两遍”代替直接对比。

### 5.2 故障注入

已覆盖并通过：

- Worker telemetry 路径不可用、SQLite 被真实写锁占用；
- 上传超时、断网、HTTP 408/425/429 和 5xx 退避重试，其他不可重试 4xx 永久隔离且不再重试；
- 批量中一条坏数据被隔离时，其他合法事件仍成功上传；
- 单事件过大、事件条数上限、总容量上限和权威快照数量上限；
- 后端观测写入异常和真实 SAVEPOINT 回滚；
- 后端启动时观测提交失败；
- Worker/后端在阶段 `running` 时异常退出，重启后只把所属开放阶段改为 `abandoned`，耗时保持 `null`；
- 关闭观测不产生新本地观测文件。

### 5.3 真实贯通链

测试真实经过：

```text
Worker telemetry SQLite
→ Worker 正式上传客户端
→ API-OBS-01 正式 HTTP 路由
→ 后端 process_stage_runs
→ API-OBS-02 聚合查询
```

同一事件重复上传后数据库仍只有一条，`4321ms` 原值在聚合结果中保持 `4321ms`。
上传成功后本地 pending 事件已删除，后端同一响应返回的权威报告被有界缓存，
UAT 证据生成器能导出该后端报告，不依赖已删除的 pending 记录。

### 5.4 回归结果

- 直接业务中立性门禁：Worker C1/C2/C3 `3 passed`，后端 C0/C4/Handoff `3 passed`，共 `6` 个正式场景；每条测试均在同一个断言中比较观测关闭/开启的前、中、后证据。该门禁已显式加入灰度 Fast UAT 工作流。
- Worker 定向 telemetry、证据、合同、打包入口及业务中立性：`104 passed + 49 subtests`。
- 后端定向观测、C3/C4 与 Handoff：`21 passed`。
- C0—C4 与 Handoff 开关前后直接前—中—后硬门禁：Worker `3 passed`、后端 `3 passed`，共 `6` 个正式场景。
- Worker 受影响扩大回归：`492 passed + 107 subtests，2 deselected`；两项 deselected 是基线已存在、与本次观测修改无关的暂停发送断言。
- 后端受影响扩大回归：`364 passed，4 skipped`；需要本机回环 Provider 的测试已在允许 `127.0.0.1` 临时端口的环境原样通过。
- Schema current、Python compile、JSON、`git diff --check`：通过。

TaskRunner 的两条暂停后 Flow 事件断言在候选和原始 `gray-v0.9.57` 上均同样失败：测试期待空 conversation_id，生产基线返回 `conv-1`。本项未修改该逻辑或断言，也未把旧失败冒充为通过。

## 6. 来源治理执行结果

```text
独立 OmniAuto 真实提交：2a4a7ed9e459421c53865b0bbc9eeb9d88b3dd09
→ 仅同步 0.9.58 生成 Schema
→ .chejin-source.json 已登记真实 SHA 与 c2_contract_0_9_58_generated_schema scope
→ 等待形成车金 0.9.58 候选提交、推送、标签和 ZIP
```
