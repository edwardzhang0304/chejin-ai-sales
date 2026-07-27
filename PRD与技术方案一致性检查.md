# PRD 与技术方案一致性检查

日期：2026-07-21

检查对象：

- PRD：`deliverables/AI智能客服售前跟进系统_PRD_运营后台统一版_v0.4.5.md`
- UI/本地静态稿：`website/ops-admin.html`、`website/ops-admin.css`、`website/ops-admin.js`
- 技术方案：`deliverables/AI智能客服售前跟进系统_技术方案手册_v0.8.md`
- UML/状态机：`deliverables/AI智能客服售前跟进系统_UML图_v0.7.md`
- C2-C3 接口合同：`deliverables/C2-C3_OmniAuto_Worker_后端接口合同_v0.1_2026-07-21.md`
- C2 当前客户端基线：V16.104；语音/角色/V3/去重沿用 V16.95 Windows 实机证据，群聊与侧栏 OCR 使用 2026-07-20 专项证据，统一顺序和语音锚点使用 V16.104 实机证据
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
| 车金 Worker 客户端 Windows 单应用第一版 | PRD v0.4.5 保留 C0-C4 产品范围、短码托管和语音事实语义；V3字段与准入细节由技术方案v0.8收口 | Windows Worker 草图不承担C2字段合同 | 技术方案v0.8和UMLv0.7明确V16.104：首屏扫描/授权分离、private短码准入、群聊/unknown终止、V3授权、文字/语音入库、统一顺序和停止语义；三层接口由C2-C3接口合同收口 | 当前C2文字/语音一致；图片和单会话串行合同已定义但尚未实现 |

## 2. 当前阶段做什么

- 销售管理升级。
- Worker 管理。
- 销售与 Worker 一对一绑定。
- 任务中心：任务列表、任务详情、任务状态、执行步骤、失败原因、结果记录。
- 车金 Worker 客户端 Windows 单应用：启动/暂停、自动领取 add_friend 任务、调用内置 OmniAuto RPA 组件控制微信桌面客户端、执行过程可视化、结果和错误码回传；系统自动只写入客户短码，销售后续可在保留短码的前提下追加人工说明。
- OmniAuto C2 会话绑定 / 微信监听：Worker 调用 `sessions / messages / voice-transcribe`。首屏扫描只发现事实；读取必须由当前 `read-targets` 授权。C2 不进入任务中心，不定义 `session_scan/message_ingest/wechat_binding/voice_transcribe` 任务。
- C2 正式准入：有效短码 + 顶部标题明确 `conversation_type=private` + `conversation_id/remark_code` 匹配 + 当前 `authorization_revision`。group/unknown/歧义均为终止态，不再搜索、读取、转写或入库。
- C2 语音：首次messages发现未转写语音才进入同一voice flow；每次页面变化后重新截图；最终转写正文绑定父语音，只入库一条voice，不产生重复text。
- C2 V3合同：`contract_version=3 / authorization_revision / source_message_key / row_kind / sender_role_source / item_state / flow_state`；正式角色为 `customer/self/system/unknown`，销售侧归一为self。
- C2 停止语义：`read-targets=[]` 时可继续首屏事实扫描，但必须清空本地读取队列；停止后不得定位目标、读消息、转写或入库。旧授权请求返回409。
- C2 当前回归状态：V16.95 已完成基础文字、语音、V3、角色、去重和停止 Windows 实测；V16.98 完成群聊终止与侧栏 OCR 收口；V16.104 完成文字/语音统一顺序与语音锚点 Windows 实机回归。图片目标接口已定义，代码和实机回归尚未完成。
- C3 正式执行口径：发送前必须 `pre_send_refresh`，发现客户新消息时旧 `reply_action` 置为 `superseded`，不得发送旧回复。
- C4 正式执行口径：召回到期先进入 `recall_precheck`，读取微信事实后创建 `trigger_type=recall` 批次；Brain/Guard 通过后统一创建 `chat_reply`，不再存在独立 `follow_up` 任务。
- OmniAuto/Worker/后端接口：复用 OmniAuto `sessions/open-chat/messages/voice-transcribe/send`、`customer_image_understanding/visual_bridge_input` 和 `customer_service_brain/brain_plan` 名称；车金只做明确适配，不建立第二套模型输出协议。
- 销售回复状态：`ai_enabled` 只作为人工关闭全部自动化的硬开关；等待销售和销售已回复使用状态门禁，客户再次回复交回 AI，长期未回复仍可由 Brain 召回。

## 3. 当前阶段不做什么

| 不做项 | 原因 |
|---|---|
| Mac Worker | 正式工程环境为商家侧 Windows 电脑，Mac 原型废弃 |
| AI 自动回复发送 | 不进入当前 C2 checkpoint；后续 C3 正式开发 / 验收按 PRD v0.4.5 与技术方案 v0.8 的 `pre_send_refresh`、Guard、`reply_action` 和 Worker 发送链路执行 |
| AI 语音回复 / 语音智能总结 / 外部 ASR | 本轮只做 C2 语音事实采集，复用微信自带转文字，不做 AI 语音回复、不做智能总结、不接外部 ASR |
| 飞书通知 | 依赖接管状态、销售飞书用户、通知配置 |
| 自动召回 | 不进入当前 C2 checkpoint；后续 C4 正式开发 / 验收按 `recall_precheck -> trigger_type=recall -> chat_reply` 唯一链路执行 |
| 抖音 API / 巨量引擎 / 小风车 | 已确认下一期再做 |
| 批量导入 | 当前跳过，不作为下一里程碑 |
| 销售移动端 | 当前无范围和设计 |

## 4. 关键一致性结论

| 检查项 | 结论 |
|---|---|
| PRD 与架构状态机 | 一致。PRD 保留业务状态语义；UML v0.7 新增的是C2准入和接口门禁，不新增会话主状态 |
| PRD 与销售管理 UI | 一致。销售列表、Worker 绑定、详情抽屉编辑、阻塞任务均已在静态稿和 Vite 前端体现 |
| PRD 与 Worker 管理 UI | 一致。Worker 列表、详情、Token、启停、心跳、重置绑定均已体现 |
| PRD 与后端销售/Worker接口 | 一致。后端销售 + Worker 管理接口、任务中心基础链路、Windows Worker 客户端服务端能力、C2/C3/C4 相关接口和事件模型需以当前提测分支及开发自测说明为准；本文件只校验 PRD / 技术方案 / UML 口径一致性，不作为正式测试通过结论 |
| 技术方案与当前阶段范围 | 一致。v0.8 是当前完整技术方案；V16.104 冻结 C2 文字/语音、安全准入、统一顺序与语音锚点，不进入任务中心、不直接执行 C3/C4。图片和单会话串行链路已有目标接口合同，但未被写成已实现或已验收能力 |

## 5. 当前风险

| 风险 | 说明 | 处理建议 |
|---|---|---|
| Worker 客户端定位变化 | 旧 Mac/人工传值草稿已废弃，团队可能仍按旧口径理解 | 统一对外说明：商家只安装和启动一个车金 Worker 客户端；OmniAuto 不作为商家侧独立产品出现，服务端复用 AI 大脑、RAG、Guard 和回复编排，Worker 端复用 RPA Sidecar 操作微信 |
| 后续开发版本容易分叉 | V16.104 已作为当前 C2 统一验收基线，若从更早安装包继续分叉会造成回归和排障混乱 | 后续图片与 C2-C3 串行开发统一从“同步新版 OmniAuto 后的 V16.104 后继分支”开始 |
| 跳过会话绑定直接做AI回复 | 未确认客户归属和消息去重前直接自动发送，可能导致回错客户、重复回复或销售接管后仍回复 | 先验收 C2 会话绑定/微信监听，再进入 C3 AI 回复发送 |
| 图片实现仍未完成 | 旧图片另存、本地路径、后端 `image_recognition` WIP 和 OmniAuto Vision 正式名称容易混用 | 按 C2-C3 接口合同只保留当前剪贴板内存事务、`customer_image_understanding/visual_bridge_input` 和 C2 统一角色；完成合同升版、代码和实机回归前 V16.104 继续跳过图片 |
| C2-C3 单会话串行尚未实现 | 当前 C3 仍是 mock Adapter 和全局任务拉取，无法保证 Worker 按 batch_id 保持原会话等待 Brain | 先完成接口合同 P0 差异，再开发真实 OmniAuto Brain Adapter、批次终态查询和 Worker 串行等待 |
| P0 / 旧版文档干扰当前阶段 | 旧版技术方案/UML不应作为当前阶段通过依据 | 当前只按PRD v0.4.5产品范围、技术方案v0.8、UMLv0.7和当前专项报告判断 |

## 6. 下一步

1. 架构/产品：接口命名和职责边界已冻结；后续合同变化先改 C2-C3 接口合同和机器合同。
2. 客户端/RPA：先解除 Sidecar 对车金合同指纹的责任、删除 ingest 顶层重复 `sidecar_run_id`；V16.104 继续保持图片跳过，直到图片合同 revision 升版。
3. 后端：统一 V3 schema 必填性，移除 `image_recognition/image_local_path` WIP 双轨，实现真实 OmniAuto Brain 映射和 batch 终态查询；不得因 handoff/销售回复自动关闭 `ai_enabled`。
4. 测试：按接口合同分别建立三层 contract test；图片和 C2-C3 串行完成后再做 Windows 实机回归。
