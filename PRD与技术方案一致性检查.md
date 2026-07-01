# PRD 与技术方案一致性检查

日期：2026-07-01

检查对象：

- PRD：`deliverables/AI智能客服售前跟进系统_PRD_运营后台统一版_v0.4.4.md`
- UI/本地静态稿：`website/ops-admin.html`、`website/ops-admin.css`、`website/ops-admin.js`
- 技术方案：`deliverables/AI智能客服售前跟进系统_技术方案手册_v0.6.md`
- UML/状态机：`deliverables/AI智能客服售前跟进系统_UML图_v0.6.md`
- C2-C4 测试口径：`deliverables/C2-C4_v0.6测试口径调整说明_2026-06-24.md`
- 后端接口说明：`deliverables/销售管理与Worker管理后端接口开发说明_2026-06-05.md`

## 1. 结论

P0 已完成并归档。当前有效阶段是 Worker 客户端 Windows 单应用第一版与运营后台配套能力。

| 阶段 | PRD 口径 | UI 口径 | 技术方案 / UML 口径 | 当前判断 |
|---|---|---|---|---|
| P0 桌面运营后台 | 已完成 | 已完成 | 已完成 | 已归档，不再作为当前活跃排期 |
| 销售管理升级 | 新增销售可选 Worker、销售详情抽屉原地编辑、绑定/更换/清空 Worker、展示阻塞任务 | 静态稿和 Vite 前端均已更新 | 后端销售接口已支持 Worker 绑定 | 一致，已进入维护/回归阶段 |
| Worker 管理 | Worker 列表、详情、Token、启停、心跳、重置绑定 | 静态稿和 Vite 前端均已更新 | 后端 Worker 接口已完成并自测通过 | 一致，已进入维护/回归阶段 |
| 任务中心 | 已明确统一任务列表、详情、状态、操作、事件和日志边界 | 本地静态稿已进入任务中心口径 | UML 已提供状态机参考，后端任务中心接口已实现 | 一致，复测通过，可按模块收口 |
| 车金 Worker 客户端 Windows 单应用第一版 | PRD v0.4.4 已按技术方案统一为 C0/C1/C2/C3/C4 分段口径，并保留 Worker 客户端单应用、客户短码、C2 会话绑定/微信监听口径 | 新增 Windows Worker 客户端草图 | 技术方案 v0.6 定义 Worker 主程序、内置 OmniAuto RPA 组件、微信桌面客户端职责；服务端绑定、心跳、拉取、领取、上报、证据能力已并入技术方案；V15.3 已完成 Windows 实机验收并作为 add_friend 基线；V16.18 已完成 C2 非第一屏 `search_by_remark_code` 定向读取 Windows 实机回归 | 一致，Mac 原型废弃；C2 后续补第一屏 `visible_hit`、`sender_role`、语音/图片事实入库和 C3/C4 联动回归 |

## 2. 当前阶段做什么

- 销售管理升级。
- Worker 管理。
- 销售与 Worker 一对一绑定。
- 任务中心：任务列表、任务详情、任务状态、执行步骤、失败原因、结果记录。
- 车金 Worker 客户端 Windows 单应用：启动/暂停、自动领取 add_friend 任务、调用内置 OmniAuto RPA 组件控制微信桌面客户端、执行过程可视化、结果和错误码回传；系统自动只写入客户短码，销售后续可在保留短码的前提下追加人工说明。
- OmniAuto C2 会话绑定 / 微信监听：Worker 调用 OmniAuto `sessions/messages` 能力读取微信事实、识别客户短码、绑定服务端 `lead/conversation/sales/worker`、读取允许读取的会话消息，并按 `dedupe_key` 去重入库。C2 是 Worker 运行时事实采集能力，不进入任务中心，不定义 `session_scan`、`message_ingest`、`wechat_binding` 这类 `task_type`。
- C2 正式执行口径：第一屏主动扫描、第一屏命中优先读取、状态机定向读取和去重入库。
- C2 定向读取字段口径：read-targets 必须包含 `conversation_id + remark_code`；定向读取确认客户靠服务端会话 ID 和客户短码双重校验，`display_name` 只能辅助定位和展示；缺失短码的已绑定会话进入异常或待复核。
- C2 当前回归状态：V16.18 已验证非第一屏 `search_by_remark_code` 定向读取链路；后续继续补第一屏 `visible_hit`、`sender_role`、语音/图片事实入库和 C3/C4 联动回归。
- C3 正式执行口径：发送前必须 `pre_send_refresh`，发现客户新消息时旧 `reply_action` 置为 `superseded`，不得发送旧回复。
- C4 正式执行口径：召回到期先进入 `recall_precheck`，读取微信事实后再决定是否创建 `follow_up`。

## 3. 当前阶段不做什么

| 不做项 | 原因 |
|---|---|
| Mac Worker | 正式工程环境为商家侧 Windows 电脑，Mac 原型废弃 |
| AI 自动回复发送 | 不进入当前 C2 checkpoint；后续 C3 正式开发 / 验收按 PRD v0.4.4 与技术方案 v0.6 的 `pre_send_refresh`、Guard、`reply_action` 和 Worker 发送链路执行 |
| 飞书通知 | 依赖接管状态、销售飞书用户、通知配置 |
| 自动召回 | 不进入当前 C2 checkpoint；后续 C4 正式开发 / 验收按 PRD v0.4.4 与技术方案 v0.6 的 `recall_precheck` 和 `follow_up` 放行规则执行 |
| 抖音 API / 巨量引擎 / 小风车 | 已确认下一期再做 |
| 批量导入 | 当前跳过，不作为下一里程碑 |
| 销售移动端 | 当前无范围和设计 |

## 4. 关键一致性结论

| 检查项 | 结论 |
|---|---|
| PRD 与架构状态机 | 一致。PRD 已承接 UML v0.6 的任务状态、会话状态、`recall_precheck`、`pre_send_refresh` 和 `superseded` 口径 |
| PRD 与销售管理 UI | 一致。销售列表、Worker 绑定、详情抽屉编辑、阻塞任务均已在静态稿和 Vite 前端体现 |
| PRD 与 Worker 管理 UI | 一致。Worker 列表、详情、Token、启停、心跳、重置绑定均已体现 |
| PRD 与后端销售/Worker接口 | 一致。后端销售 + Worker 管理接口、任务中心基础链路、Windows Worker 客户端服务端能力、C2/C3/C4 相关接口和事件模型需以当前提测分支及开发自测说明为准；本文件只校验 PRD / 技术方案 / UML 口径一致性，不作为正式测试通过结论 |
| 技术方案与当前阶段范围 | 一致。v0.6 是当前完整技术方案，PRD v0.4.4 已按 C0/C1/C2/C3/C4 分段承接；当前阶段从 C1 add_friend 进入 C2 会话绑定/微信监听；PRD 已明确 C2 按第一屏主动扫描、第一屏命中优先读取、状态机定向读取和去重入库推进，并要求 read-targets 带 `conversation_id + remark_code`，不进入任务中心，不直接进入 C3 AI 自动回复发送。V16.18 只更新 C2 非第一屏定向读取验证状态，不改变 C3/C4 范围边界 |

## 5. 当前风险

| 风险 | 说明 | 处理建议 |
|---|---|---|
| Worker 客户端定位变化 | 旧 Mac/人工传值草稿已废弃，团队可能仍按旧口径理解 | 统一对外说明：商家只安装和启动一个车金 Worker 客户端；OmniAuto 不作为商家侧独立产品出现，服务端复用 AI 大脑、RAG、Guard 和回复编排，Worker 端复用 RPA Sidecar 操作微信 |
| 后续开发版本容易分叉 | V15.3 已作为当前统一验收基线，后续如果继续从 v15/v15.1/v15.2 分叉会造成回归和排障混乱 | 后续新增需求或问题修复统一从 V16 起递增 |
| 跳过会话绑定直接做AI回复 | 未确认客户归属和消息去重前直接自动发送，可能导致回错客户、重复回复或销售接管后仍回复 | 先验收 C2 会话绑定/微信监听，再进入 C3 AI 回复发送 |
| 暂停语义需确认 | Worker 暂停时，是执行完当前任务后暂停，还是立即停止领取且不中断当前任务 | 产品、后端、RPA 联合确认 |
| P0 / 旧版文档干扰当前阶段 | P0 验收、自测、测试报告和旧版 PRD / 技术方案 / UML 不应作为当前阶段通过依据 | 旧版文档已从活跃目录清理；当前只按 PRD v0.4.4、技术方案 v0.6、UML v0.6 判断 |

## 6. 下一步

1. 产品经理：按 PRD v0.4.4 维护当前范围，C2 不把 AI 自动回复发送并入本 checkpoint，也不把 C2 做成任务中心任务。
2. 前端/RPA：前端完成并提测后，按 v0.6 口径复核 `sessions/messages` 调用、读取结果展示、错误码和证据上传。
3. 后端 / Worker：继续围绕 C2 第一屏 `visible_hit`、`sender_role`、语音/图片事实入库和 C3/C4 联动回归补证据。
4. 测试：基于 V16.18 结果继续补后续 C2 回归项；C3 真实 AI 联调仍不进入当前 checkpoint。
