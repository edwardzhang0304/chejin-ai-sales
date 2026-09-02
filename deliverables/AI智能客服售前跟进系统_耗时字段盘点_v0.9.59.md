# AI 智能客服售前跟进系统耗时字段盘点 v0.9.59

## 1. 范围与结论

本盘点以 `gray-v0.9.57` 为业务基线，只处理运营级耗时统计的重复来源。后端 `process_stage_runs` 和 API-OBS-02 是唯一运营查询权威；Worker 的 `worker_telemetry.sqlite3` 仅是有界断网上传缓冲、永久 4xx 隔离证据和后端权威快照证据缓存。

本项不删除业务计时器，不修改 C0—C4、Handoff、媒体编排、消息身份、重试、UI 锁或业务终态。

## 2. 生产耗时来源盘点

| 来源/字段 | 写入者 | 生产读取者 | 标准阶段 | 处理结果 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `process_stage_runs.queue_duration_ms`、`execution_duration_ms`、`attempt`、`status`、`error_code` | 后端观测服务、API-OBS-01 | API-OBS-02 | 全部标准阶段 | `keep_source` | 唯一运营级权威 |
| `worker_telemetry.sqlite3.telemetry_stage_events` | Worker telemetry | Worker 异步上传器、证据包待上传/隔离快照 | Worker 产生的标准阶段 | `keep_source` | 只作有界上传缓冲与 4xx 隔离，不是报告或业务账本 |
| `telemetry_process_links`、`telemetry_stage_attempts`、`upload_attempt_count`、`next_attempt_at`、`delivery_state` | Worker telemetry | Worker telemetry | 观测关联、退避重试及永久隔离 | `keep_source` | 关联表和尝试表各最多 5000 条并删除最旧记录；共同服从 64 MiB 总容量；超时/断网/408/425/429/5xx 退避；不可重试的其他 4xx 隔离；不得改变业务次数 |
| `telemetry_authority_snapshots.report_json` | 后端 API-OBS-01 上传响应，Worker 原样缓存 | 新 UAT 证据生成器 | 已上传 `process_run_id` 的后端权威报告 | `keep_source` | 最多 500 份，只读证据；Worker 不得本地重算或供业务使用 |
| C0 `lead_received_started`、`assignment_started` 的 `perf_counter` 差值 | `lead_service` | `record_server_stage_best_effort` | `c0.lead_received`、`c0.lead_assigned` | `map_once` | 同一次业务执行只写一次标准阶段 |
| C1 `StageTimer(c1.add_friend_execute)` | TaskRunner | Worker telemetry 上传器 | `c1.add_friend_execute` | `map_once` | Sidecar 只有子步骤诊断，不能相加冒充完整阶段 |
| C2 `sidecar_scan_duration_ms` | TaskRunner 对既有 `list_sessions` 调用计时 | Worker telemetry 上传器 | `c2.scan` | `map_once` | 不新增扫描；仅测量本来就会发生的调用 |
| C2 `flow_timing.phases[].duration_seconds/completed/failed/error_code` | TaskRunner C2 读取流程 | `enqueue_c2_flow_timing_stages` | `c2.target_locate`、`c2.message_read`、`c2.voice_transcription`、`c2.image_vision` | `map_once` | 仅存于本次 Flow 内存，结束时映射一次 |
| C3 `generation_duration_ms` | `c3_service` | `record_server_stage_best_effort` | `c3.brain_generate`、`c4.brain_generate` | `map_once` | 使用实际隔离 Brain 调用的单调时间 |
| C3/C4 `StageTimer(c3.pre_send_refresh)` | TaskRunner | Worker telemetry 上传器 | `c3.pre_send_refresh` | `map_once` | 只覆盖原有发送前复读，不增加复读 |
| Worker `StageTimer(c3/c4.reply_send_confirm)` | TaskRunner，从 Sidecar 进程启动前到返回/异常后 | Worker telemetry 上传器 | `c3.reply_send_confirm`、`c4.reply_send_confirm` | `map_once` | 标准运营耗时，必须包含进程启动和通信 |
| Sidecar `timing.send_payload_duration_seconds` | 正式发送 Sidecar | 事故诊断 | 发送内部子步骤 | `keep_source` | 只作内部诊断，不得替代 C3/C4 标准发送阶段 |
| Handoff `duration_ms` | `feishu_service` 的既有 HTTP 调用单调计时 | `record_server_stage_best_effort` | `handoff.feishu_notify` | `map_once` | 通知失败不影响业务事务原规则 |
| Sidecar `timing`、`timing_ms`、OCR/窗口/点击子步骤耗时 | OmniAuto Sidecar | Worker 映射器、事故诊断 | 所属 C1/C2/C3/C4 父阶段的底层来源 | `keep_source` | 保留原始诊断；子步骤不得与父阶段重复相加 |
| Brain `stage_timings`、`stage_timeline`、`latency_trace`、Provider `elapsed_ms` | OmniAuto Brain/Provider worker | Adapter、事故诊断 | `c3.brain_generate`、`c4.brain_generate` 的底层来源 | `keep_source` | 不重新调用 Provider，不形成第二套运营汇总 |
| Vision `duration_ms`、`total_duration_ms`、步骤 `offset_ms` | OmniAuto Vision | Worker 映射器、事故诊断 | `c2.image_vision` 的底层来源 | `keep_source` | 不增加图片读取或 Vision 调用 |
| 加好友、发送 `timing`、`timing_ms`、`send_timing`、`send_observability` | OmniAuto 对应生产动作 | Worker 映射器、事故诊断 | `c1.add_friend_execute`、`c3/c4.reply_send_confirm` 的底层来源 | `keep_source` | 只保留原始证据，不作为独立运营报告 |
| `evidence.timing` 中复制的整份 `flow_timing` | 旧 TaskRunner | 旧证据/人工分析 | 已由标准 C2 阶段覆盖 | `delete_duplicate` | 新生产载荷不再写入 |
| 结构化日志事件 `c2_message_read_timing` | 旧 TaskRunner | 旧证据生成器 | 已由标准 C2 阶段覆盖 | `delete_duplicate` | 新生产日志不再写入 |
| 新证据包 `timing/flow_timing.json`、`timing/brain.json`、`timing/send_confirm_ocr.json` | 旧证据生成器 | 人工事故分析 | 无独立标准阶段 | `delete_duplicate` | 新证据包改为导出后端权威快照、待上传事件和隔离事件，不重算报告 |
| 已发布旧 ZIP 中的上述 timing 文件 | 已冻结旧版本 | 旧事故工具 | 无 | `history_read_only` | 不改、不删历史包，新代码禁止续写 |

## 3. 明确不属于本次清理的业务时间

以下时间会直接决定业务行为，绝不是运营耗时统计，必须保持 `0.9.57` 原实现：

- Worker 租约、UI 锁、任务领取和心跳过期时间；
- 未读复读、身份恢复、故障冷却和 120 秒业务门禁；
- C4 召回等待、冷却和最近联系时间；
- LLM 总截止时间、Provider 超时及重试剩余时间；
- 微信界面状态等待、菜单等待、剪贴板等待和发送确认上限；
- Handoff 通知重试时间及业务 Outbox 重传时间。

这些字段不得迁入 `process_stage_runs` 后再反向供业务判断，也不得因“重复耗时清理”被删除。

## 4. 删除前消费者检查

已删除的新生产输出只有三类：C2 重复结构化日志、C2 入库证据中的重复 timing 副本、新证据包的三份派生 timing 报告。它们均已有标准阶段映射，且生产代码不存在新的业务消费者。

保留的底层诊断字段仍由其原模块写入；运营查询、新证据包和后端聚合不再把它们组织成第二份耗时报告。

## 5. 回滚边界

Worker 的 `CHEJIN_OBSERVABILITY_ENABLED=false` 与后端的 `OBSERVABILITY_ENABLED=false` 只停止新增标准观测记录和上传，不删除 Worker 缓冲、后端历史或任何业务数据。关闭前后业务动作次数、顺序、状态、错误码和终态必须完全一致。
