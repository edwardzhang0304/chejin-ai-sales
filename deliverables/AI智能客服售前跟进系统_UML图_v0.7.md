# AI智能客服售前跟进系统 UML图（正式工程版）

版本：v0.7

日期：2026-07-20

最近修订：2026-07-20，按技术方案 v0.8 和 Worker V16.98 调整 C2：首屏事实扫描与 read-target 授权分离；只准入有效短码 private 单聊；群聊/unknown 终止；消息使用 V3 合同和 authorization_revision；语音在同一读取 flow 内条件性转写；图片识别待定。

口径：正式工程完整合并版。取消 AI 固定轮次限制，采用长期跟进状态机；备注短码作为系统托管开关；销售未绑定Worker时，add_friend任务进入blocked并在后续绑定Worker后恢复pending；统一任务中心采用 `task.status + task.result_code`。C2 是 Worker 运行时事实采集和消息入库能力，不进入统一任务中心；短码只是候选入口，读取还必须满足 private、当前 read-target 和 authorization_revision。图片 Vision 尚未进入本版架构。

独立图文件：
- 主业务时序图：`AI智能客服售前跟进系统_主业务时序图_C0-C4_v0.7.puml`
- 会话状态机图：`AI智能客服售前跟进系统_会话状态机图_v0.6.puml`

说明：状态机以本文档和 PUML 源文件为准。PNG 仅作为本地临时预览图，不作为当前有效文档或开发验收依据。

## 1. 职责边界组件图

```mermaid
flowchart LR
    subgraph Cloud["云端业务控制面"]
        LeadSvc["线索与销售服务"]
        BindingSvc["销售/Worker绑定服务"]
        ConvSvc["会话状态机"]
        Scheduler["服务端定时扫描器"]
        TaskSvc["任务调度服务"]
        RemarkTool["备注短码工具"]
        VehicleSvc["车源索引"]
        NotifySvc["飞书通知"]
        DB[("业务数据库")]

        subgraph AIEngine["服务端AI大脑<br/>同一后端服务内的职责模块"]
            ContextSvc["会话上下文构建"]
            KnowledgeSvc["知识库管理"]
            RAGSvc["OmniAuto RAG检索"]
            EvidenceSvc["Evidence Pack"]
            BrainSvc["OmniAuto customer_service_brain<br/>回复生成/策略判断"]
            GuardSvc["Guard风控"]
            ReplyActionSvc["ReplyAction生成"]
            DeepSeek["DeepSeek API"]
        end
    end

    subgraph WorkerSide["商家侧Windows电脑"]
        Worker["Worker主进程/执行台"]
        Bridge["OmniAuto Bridge"]
        Sidecar["OmniAuto RPA Sidecar子进程"]
        SessionMsg["sessions/messages<br/>会话扫描与消息读取"]
        Lock["Local WeChat UI Lock"]
        WeChatPC["微信桌面客户端"]
    end

    subgraph SalesSide["销售侧"]
        SalesMobile["销售微信手机客户端"]
        Feishu["销售飞书"]
    end

    Worker --> Bridge
    Bridge --> Sidecar
    Sidecar --> SessionMsg
    SessionMsg --> WeChatPC
    Worker --> Lock
    Lock --> Sidecar
    Sidecar --> WeChatPC
    SessionMsg -. "读取消息如需切换/点击微信窗口也必须拿锁" .-> Lock
    WeChatPC <--> SalesMobile

    Worker -- "session_scan_result / message_event / sales_reply_event / sent_ack / wechat_error" --> ConvSvc
    TaskSvc -- "add_friend / chat_reply / follow_up" --> Worker
    ConvSvc --> DB
    Scheduler --> DB
    Scheduler --> TaskSvc
    Scheduler --> NotifySvc
    RemarkTool --> DB
    RemarkTool --> LeadSvc
    ConvSvc -- "message_batch" --> ContextSvc
    ContextSvc --> RAGSvc
    ContextSvc --> BrainSvc
    KnowledgeSvc --> RAGSvc
    RAGSvc --> EvidenceSvc
    VehicleSvc --> EvidenceSvc
    EvidenceSvc --> BrainSvc
    BrainSvc --> DeepSeek
    BrainSvc --> GuardSvc
    GuardSvc -- "send_reply / handoff / no_action / pause / retry_later" --> ReplyActionSvc
    ReplyActionSvc -- "创建chat_reply任务" --> TaskSvc
    NotifySvc --> Feishu

    LeadSvc --> DB
    LeadSvc --> TaskSvc
    BindingSvc --> DB
    BindingSvc -- "绑定Worker后解除SALES_WORKER_NOT_BOUND阻塞" --> TaskSvc
```

## 2. Worker与服务端扫描/定向读取分工图

```mermaid
flowchart TD
    A["Worker调用OmniAuto sessions<br/>只扫描微信当前第一屏"] --> Row["普通/增强OCR按视觉行聚合<br/>唯一合法短码优先"]
    Row --> B["上报session_scan_result<br/>短码候选 / conversation_type证据"]
    B --> C{服务端短码绑定结果}
    C -- 短码唯一匹配 --> D["wechat_binding.status=bound<br/>listen_status=listening"]
    C -- 未识别/冲突/低置信 --> E["unbound / needs_review / binding_failed<br/>next_action=none"]

    D --> RT["服务端read-targets<br/>conversation_id + remark_code<br/>authorization_revision"]
    RT --> HasAuth{targets是否为空?}
    HasAuth -- 是 --> Clear["清空visible_hit_queue<br/>不读/不转写/不入库<br/>首屏事实扫描仍可继续"]
    HasAuth -- 否 --> Filter["Worker按当前授权过滤并去重<br/>第一屏命中优先"]
    Filter --> ST["state_target_queue<br/>recent_ai_sent / waiting_user_reply / waiting_sales_reply / recall_precheck"]
    ST --> ChooseRead{目标是否仍在第一屏可见?}
    ChooseRead -- 是 --> SM1["点击首屏唯一短码会话"]
    ChooseRead -- 否 --> SM2["search_by_remark_code<br/>唯一命中才点击"]
    SM1 --> Gate{"顶部标题同步确认<br/>有效短码 + private?"}
    SM2 --> Gate
    Gate -- group/unknown/歧义 --> Stop["本轮终止<br/>不再搜索/读取/转写/入库"]
    Gate -- private --> Initial["首次messages<br/>当前屏文字/语音观察"]
    Initial --> Voice{存在未转写语音?}
    Voice -- 否 --> Build["构建V3消息合同"]
    Voice -- 是 --> VT["同一flow执行voice-transcribe<br/>每次页面变化后重新截图"]
    VT --> Final["最终messages新截图<br/>同时复核目标和读取消息"]
    Final --> Build
    Build --> H["上报messages/ingest<br/>V3 + authorization_revision<br/>source_message_key + dedupe_key"]

    H --> Auth["服务端复核当前授权版本<br/>旧revision返回409"]
    Auth --> I["数据库最终去重<br/>unique(conversation_id,dedupe_key)"]
    I --> J{ingest_result}
    J -- ingested且customer消息 --> MB["收集message_batch<br/>进入C3 AI/转人工判断"]
    J -- duplicated/ignored --> N["不触发AI<br/>不创建发送任务"]

    MB --> S["服务端状态机"]
    E --> S
    S --> S1["记录绑定/监听状态<br/>C2不创建Task"]
    S1 -. "C3需要发送AI回复时" .-> T["创建chat_reply Task"]

    ScanDB["服务端定时扫描数据库"] --> RuleHit{命中业务规则?}
    RuleHit -- 等待用户超过N天 --> Precheck["生成recall_precheck read-target<br/>不直接创建follow_up"]
    Precheck --> ST
    I -- precheck读到新客户消息 --> CancelRecall["取消召回<br/>进入AI/转人工判断"]
    I -- precheck未读到新客户消息 --> FollowTask["创建follow_up任务"]
    RuleHit -- waiting_sales_reply销售超时 --> FeishuNotify["发送飞书通知销售"]
    RuleHit -- Worker离线/卡住 --> WorkerTodo["标记异常/生成待办"]

    FollowTask --> W["Worker领取follow_up并操作微信"]
    FeishuNotify --> L["记录HandoffEvent通知结果"]
    S -- 短码新增 --> M1["绑定线下好友并进入系统跟进"]
    S -- 短码移除 --> M2["status=closed<br/>停止AI/召回/飞书提醒"]
```

## 2.1 微信UI操作串行锁

```mermaid
flowchart TD
    Q["Worker本地任务队列"] --> P{任务是否需要操作微信UI?}
    P -- 否 --> N["非UI逻辑可并行<br/>等待AI/写日志/上报状态"]
    P -- 是 --> L["申请Local WeChat UI Lock<br/>本地锁，不是服务端任务租约"]
    L --> Lease["写入ui_lock.json<br/>lock_id / fencing_token / lease_expires_at"]
    Lease --> Renew["持锁期间定时续租<br/>renew_ui_lock"]
    Renew --> A["add_friend<br/>手机号搜索/发送邀请/写备注"]
    Renew --> C["chat_reply<br/>打开会话/输入/发送"]
    Renew --> S["session_scan/message_ingest<br/>C2扫描/读取消息"]
    Renew --> F["follow_up<br/>发送召回"]
    Renew -. "图片方案确认前禁用" .-> I["save_image历史预留"]
    Renew --> R["remark_code<br/>确认短码新增/移除"]
    A --> U["释放UI锁"]
    C --> U
    S --> U
    F --> U
    I --> U
    R --> U
    Renew --> T["锁过期/续租失败/步骤超时"]
    T --> E["截图取证<br/>释放或恢复stale lock<br/>上报错误码"]
    E --> U
    U --> Q
```

## 3. 主业务时序图（线索进入到结束，按C0/C1/C2/C3/C4分段）

说明：本图采用 PlantUML/PUML 维护，源文件为 `AI智能客服售前跟进系统_主业务时序图_C0-C4_v0.7.puml`。C2 是 Worker 运行时事实采集和消息入库能力，不进入统一任务中心。

```plantuml
@startuml
title AI智能客服售前跟进系统 主业务时序图 C0-C4

skinparam monochrome true
skinparam shadowing false
skinparam sequence {
  ArrowColor #222222
  LifeLineBorderColor #999999
  LifeLineBackgroundColor #FFFFFF
  ParticipantBorderColor #222222
  ParticipantBackgroundColor #FFFFFF
  BoxBorderColor #777777
  BoxBackgroundColor #FFFFFF
  GroupBorderColor #777777
  GroupBackgroundColor #FFFFFF
}

autonumber

participant "线索来源/人工导入" as L
participant "线索与销售服务" as LS
participant "销售/Worker绑定" as BS
participant "统一任务中心" as TS
participant "Worker主进程" as W
participant "OmniAuto RPA Sidecar" as OA
participant "销售微信桌面客户端" as WX
participant "会话状态机" as CP
participant "服务端AI大脑\nOmniAuto AI Engine/DeepSeek/Guard" as AI
participant "服务端定时扫描器" as Scan
participant "飞书机器人" as FS
participant "销售手机微信" as S
participant "客户微信" as C

group C0 线索入库、分配、生成加好友任务
  L -> LS: 手机号线索进入系统
  LS -> LS: 去重、脱敏、创建 lead/conversation
  LS -> BS: 查询销售与 Worker 绑定关系
  alt 销售已绑定 Worker
    LS -> TS: 创建 add_friend 任务\nstatus=pending
  else 销售未绑定 Worker
    LS -> TS: 创建 add_friend 任务\nstatus=blocked\nblock_code=SALES_WORKER_NOT_BOUND
    BS --> TS: 后续绑定 Worker 后解除阻塞
    TS -> TS: blocked -> pending
  end
end

group C1 add_friend：搜索手机号、申请好友、写客户短码
  W -> TS: 拉取/领取 add_friend 任务
  W -> OA: 调用 add-friend-entry-click-plan-windows\nphone_or_wechat / verify_message / remark_code
  OA -> WX: 手机号搜索、提交好友申请、写入客户短码
  alt 邀请发送成功
    OA --> W: completed + result_code=invite_sent
    W -> TS: 上报 task 完成
    TS -> CP: conversation.status=add_friend_sent
  else 已经是好友
    OA --> W: completed + result_code=already_friend
    W -> TS: 上报 task 完成
    TS -> CP: conversation.status=friend_added
  else 加好友失败/风控
    OA --> W: failed + error_code
    W -> TS: 上报失败和证据
    TS -> CP: 保持当前状态或转人工待处理
  end
end

group C2 V16.98：首屏扫描、授权准入、文字/语音入库；不进入任务中心
  loop Worker 运行中
    W -> OA: sessions 扫描微信当前第一屏
    OA -> WX: 普通/增强OCR按视觉行聚合\n识别短码和群聊人数证据
    OA --> W: remark_code_candidates / conversation_type evidence
    W -> CP: 上报 session_scan_result
    alt 短码唯一绑定
      CP -> CP: wechat_binding.status=bound\nlisten_status=listening
    else 未识别/短码冲突/绑定冲突
      CP -> CP: bind_status=unbound/needs_review/binding_failed
      CP --> W: next_action=none
    end

    W -> CP: 拉取 read-targets
    CP --> W: conversation_id + remark_code + authorization_revision
    alt read-targets为空
      W -> W: 清空visible_hit_queue\n不读/不转写/不入库
    else 当前授权目标
      W -> OA: 首屏唯一命中则visible\n未命中才search_by_remark_code
      OA -> WX: 点击目标并读取顶部标题
      OA --> W: remark_code + conversation_type
      alt group / unknown / 歧义
        W -> W: 本轮终止\n不再搜索/读取/转写/入库
      else 有效短码 + private
        W -> OA: 首次messages读取当前屏
        OA --> W: V3 observations
        alt 发现未转写语音
          W -> OA: 同一flow执行voice-transcribe
          OA -> WX: 逐条调用微信自带转文字\n页面变化后重新截图
          W -> OA: 最终messages新截图\n同时复核目标和读取消息
          OA --> W: 文字 + 已绑定父语音的转写结果
        end
        W -> CP: messages/ingest\ncontract_version=3 + authorization_revision\nsource_message_key + dedupe_key
        CP -> CP: 复核授权版本\nunique(conversation_id,dedupe_key)去重
        CP -> CP: 新customer消息才进入C3判断\nnext_action=none
      end
    end
  end
end

group C3 AI文字回复：服务端生成 reply_action，Worker 只执行已批准发送
  C -> WX: 客户通过好友并发送消息
  W -> CP: C2链路上报 message_event
  CP -> CP: 判断状态、风控、黑名单、静默、是否已接管
  alt 允许 AI 继续接待
    CP -> AI: 生成候选回复/evidence_pack/Guard
    AI --> CP: send_reply / handoff / no_action
    alt Guard 通过且可自动回复
      CP -> TS: 创建 chat_reply 任务和 reply_action
      W -> TS: claim reply_action
      W -> OA: pre_send_refresh 定向读取目标会话
      OA -> WX: 第一屏可见快速读取；否则搜索框粘贴remark_code并二次确认
      W -> CP: 上报 refresh message_event 或无新增
      alt 没有新客户消息
        W -> OA: send 已批准回复
        OA -> WX: 打开会话、输入、发送
        OA --> W: sent_ack
        W -> CP: 上报 sent_ack
        CP -> CP: status=waiting_user_reply
last_outbound_at=now
      else 有新客户消息
        CP -> CP: reply_action=superseded
不发送旧回复
        CP -> AI: 新message_batch重新生成回复
      end
    else 高意向/高风险/模型失败/低置信
      CP -> CP: status=waiting_sales_reply
ai_enabled=false
      CP -> FS: 飞书通知销售接管
      FS -> S: 销售收到通知
    end
  else 不允许 AI 回复
    CP -> CP: 保持等待/转人工/关闭/拒绝状态
  end
end

group C4 召回与人工跟进：召回前先precheck，Worker只执行已批准任务
  loop 服务端定时扫描
    Scan -> CP: 检查 waiting_user_reply / recalled_waiting_user / sales_replied_waiting_user
    alt 客户 N 天未回复且未拒绝/未关闭/未黑名单
      Scan -> CP: status=recall_precheck
生成 recall_precheck read-target
      W -> CP: 拉取 recall_precheck read-target
      W -> OA: messages 定向读取该会话
      OA -> WX: 读取最新消息
      W -> CP: 上报 precheck message_event 或无新增
      alt precheck读到新客户消息
        CP -> CP: 取消召回
进入AI回复/转人工判断
      else precheck确认无新客户消息
        CP -> TS: 创建 follow_up 任务
        W -> TS: 领取 follow_up
        W -> OA: 发送固定召回文案
        OA -> WX: 发送召回
        W -> CP: 上报 follow_up sent_ack
        CP -> CP: status=recalled_waiting_user
recall_count+1
      else precheck失败/目标不确认
        CP -> CP: 不发送召回
记录RECALL_PRECHECK_FAILED
      end
    else waiting_sales_reply 销售超时未回复
      Scan -> FS: 飞书提醒销售
      FS -> S: 销售收到超时提醒
    else 未命中规则
      Scan -> CP: 不生成任务
    end
  end

  alt 客户再次回复
    C -> WX: 客户回复
    W -> CP: C2链路上报 message_event
    CP -> CP: 回到 AI 判断或销售处理
  else 销售手机端人工回复
    S -> C: 销售在手机微信回复客户
    WX --> W: 桌面端同步我方消息
    W -> CP: 上报 sales_reply_event
    CP -> CP: status=sales_replied_waiting_user
AI停止
  else 客户明确拒绝
    C -> WX: 明确拒绝/拉黑/不需要
    W -> CP: 上报拒绝消息事实
    CP -> CP: status=rejected
停止加好友、AI、召回
  else 销售移除客户短码或人工关闭
    S -> WX: 修改备注移除短码/线下成交后关闭
    W -> CP: 上报 remark_code_removed
    CP -> CP: status=closed
停止系统托管
  end
end

@enduml
```

## 3.1 销售未绑定Worker时加好友任务阻塞/恢复时序图

```mermaid
sequenceDiagram
    autonumber
    participant LS as 线索服务
    participant BS as 销售/Worker绑定服务
    participant TS as 任务调度
    participant W as Worker
    participant DB as 业务数据库

    LS->>DB: 线索轮询分配给销售
    LS->>TS: 创建add_friend任务

    alt 销售已绑定Worker
        TS->>DB: task.status=pending, worker_id=已绑定Worker
        W->>TS: Worker上线后领取pending任务
    else 销售未绑定Worker
        TS->>DB: task.status=blocked
        TS->>DB: block_code=SALES_WORKER_NOT_BOUND
        TS->>DB: block_reason=销售未绑定Worker，无法自动加好友
    end

    BS->>DB: 销售后续绑定Worker
    BS->>TS: 恢复该销售名下被SALES_WORKER_NOT_BOUND阻塞的add_friend任务
    TS->>DB: task.status=blocked -> pending
    TS->>DB: task.worker_id=新绑定Worker, block_code=null
    W->>TS: Worker下一轮领取pending任务
```

## 3.2 统一任务中心状态映射图

```mermaid
flowchart TD
    A["Task.status<br/>blocked / pending / running / completed / failed / cancelled"] --> B{任务是否完成?}
    B -- "completed" --> C["写入Task.result_code"]
    C -- "add_friend: invite_sent" --> D["Conversation.status=add_friend_sent"]
    C -- "add_friend: already_friend" --> E["Conversation.status=friend_added"]
    C -- "chat_reply: chat_reply_sent" --> F["Conversation.status=waiting_user_reply"]
    C -- "follow_up: follow_up_sent" --> G["Conversation.status=recalled_waiting_user"]
    B -- "failed" --> H["写入Task.error_code<br/>服务端判断重试/转人工/暂停/拒绝"]
    B -- "blocked" --> I["写入Task.block_code"]
    I -- "SALES_WORKER_NOT_BOUND" --> J["Conversation.status=add_friend_blocked"]
    X["C2 session_scan / message_ingest"] --> Y["不是Task<br/>不上任务中心<br/>只写wechat_session_bindings / message_events"]
    Y --> Z["message_event入库后<br/>由会话状态机决定后续是否进入C3"]
    Z -. "C3需要发送AI回复时才创建" .-> K["chat_reply Task"]
```

## 4. 等待用户回复与多轮召回时序图

```mermaid
sequenceDiagram
    autonumber
    participant CP as 服务端状态机
    participant Scan as 服务端定时扫描器
    participant TS as 任务调度
    participant W as Worker
    participant OA as OmniAuto RPA
    participant WX as 微信桌面客户端
    participant C as 客户

    CP->>CP: AI/人工/召回发送成功
    CP->>CP: status=waiting_user_reply或sales_replied_waiting_user
    CP->>CP: last_outbound_at=now

    loop 定时扫描数据库
        Scan->>CP: 检查等待用户回复类状态
        alt now-last_outbound_at>=N天 且客户未回复
            Scan->>CP: status=recall_precheck
            CP->>W: read-targets返回recall_precheck
            W->>OA: messages定向读取该会话
            OA->>WX: 第一屏可见快速读取；否则搜索框粘贴remark_code并二次确认
            W->>CP: 上报precheck message_event或无新增
            alt 读到客户新消息
                CP->>CP: 取消召回，进入AI/转人工判断
            else 确认无新客户消息
                CP->>TS: 创建follow_up任务
                W->>TS: 领取follow_up
                W->>WX: 发送AI召回内容
                W->>CP: sent_ack
                CP->>CP: status=recalled_waiting_user, recall_count+1, last_recall_at=now
            else precheck失败
                CP->>CP: 不发送召回，记录RECALL_PRECHECK_FAILED
            end
        else 客户已回复/拒绝/关闭
            Scan->>CP: 不生成召回
        end
    end

    C->>WX: 客户回复
    W->>CP: 上报message_event
    CP->>CP: 更新last_inbound_at，召回条件失效
```

## 5. 转人工后销售超时提醒时序图

```mermaid
sequenceDiagram
    autonumber
    participant CP as 服务端状态机
    participant Scan as 服务端定时扫描器
    participant FS as 飞书机器人
    participant S as 销售手机端
    participant WX as 微信桌面客户端
    participant W as Worker

    CP->>CP: 进入waiting_sales_reply, handoff_at=now

    alt 销售及时回复
        S->>WX: 销售手机端回复客户
        W->>WX: 桌面端同步我方消息
        W->>CP: 上报sales_reply_event
        CP->>CP: sales_first_reply_at=now, status=sales_replied_waiting_user
    else 销售超过N天未回复
        Scan->>CP: 检查waiting_sales_reply超时
        CP->>FS: 飞书通知销售
        FS-->>CP: sent/failed
        CP->>CP: HandoffEvent记录通知结果和错误摘要
    end
```

## 6. 会话状态机图（客户可视简化版）

```mermaid
stateDiagram-v2
    direction TB

    [*] --> new: 线索入库
    new --> assigned: 轮询分配到销售
    assigned --> add_friend_pending: 销售已绑定Worker，创建可执行加好友任务
    assigned --> add_friend_blocked: 销售未绑定Worker，创建阻塞任务
    add_friend_blocked --> add_friend_pending: 后续绑定Worker后解除阻塞
    add_friend_pending --> add_friend_sent: 已发送添加通讯录邀请
    add_friend_pending --> friend_added: 已是好友
    add_friend_sent --> friend_added: Worker从第一屏扫描/定向读取识别到新会话

    friend_added --> ai_active: C2绑定成功，允许后续AI接待
    [*] --> ai_active: 线下好友备注新增有效短码

    ai_active --> waiting_user_reply: AI回复成功
    waiting_user_reply --> ai_active: 客户回复且AI仍负责
    recalled_waiting_user --> ai_active: 客户回复且AI仍负责

    waiting_user_reply --> recall_precheck: N天未回复，召回前定向读取确认
    recalled_waiting_user --> recall_precheck: 下一召回周期到期，召回前确认
    sales_replied_waiting_user --> recall_precheck: 销售回复后客户N天未回，召回前确认
    recall_precheck --> ai_active: precheck读到客户新消息且AI仍负责
    recall_precheck --> waiting_sales_reply: precheck读到高意向/高风险/需人工
    recall_precheck --> recalled_waiting_user: precheck确认无新客户消息且follow_up发送成功
    recall_precheck --> waiting_user_reply: precheck读取失败/目标不确认，暂不召回

    ai_active --> waiting_sales_reply: 高意向/高风险/模型失败/低置信/pause
    ai_active --> ai_active: no_action / retry_later
    waiting_user_reply --> waiting_sales_reply: 客户回复后需人工处理
    recalled_waiting_user --> waiting_sales_reply: 召回后客户高意向/风险
    waiting_sales_reply --> sales_replied_waiting_user: 销售人工回复
    waiting_sales_reply --> waiting_sales_reply: 销售超时飞书通知
    sales_replied_waiting_user --> waiting_sales_reply: 客户回复后继续由销售处理
```

说明：`recall_precheck` 是召回前确认状态，不代表已经发送召回。只有定向读取确认没有新客户消息后，才允许创建并发送 `follow_up`。

## 6.1 全局退出规则

```mermaid
flowchart LR
    Any["任意非终态<br/>ai_active / waiting_user_reply / recall_precheck / recalled_waiting_user / waiting_sales_reply / sales_replied_waiting_user"]
    Any -- "客户明确拒绝" --> Rejected["rejected<br/>停止自动动作，不再召回"]
    Any -- "备注短码移除 / 人工关闭" --> Closed["closed<br/>系统停止自动跟进"]
```

## 6.2 C2绑定与监听状态图

说明：`wechat_binding.status` 和 `conversation.listen_status` 是 C2 运行态，不混入 `conversation.status`。`conversation.status` 只表达客户/会话业务生命周期。

```mermaid
stateDiagram-v2
    direction LR

    [*] --> unbound: 未扫描到有效短码
    unbound --> binding_candidate: session_scan_result包含短码候选
    binding_candidate --> bound: 短码唯一匹配lead/conversation/sales/worker
    binding_candidate --> needs_review: 短码冲突/低置信/绑定冲突
    binding_candidate --> binding_failed: 短码非法/微信不可控/扫描失败
    bound --> disabled: 客户拒绝/会话关闭/短码移除
    needs_review --> binding_candidate: 后续扫描重新识别
    binding_failed --> binding_candidate: 下次扫描重试

    [*] --> not_started: 未绑定会话
    not_started --> listening: binding.status=bound且Worker在线
    listening --> paused: 全局暂停/静默规则/Worker暂停
    listening --> degraded: OCR低置信/读取失败但可恢复
    degraded --> listening: 后续读取恢复
    paused --> listening: 恢复监听
    listening --> error: 连续读取失败/微信不可控
    error --> listening: 人工处理后恢复
    listening --> disabled: binding.status=disabled
```

## 6.3 状态含义速查

| 状态 | 客户能理解的含义 |
|---|---|
| `assigned` | 线索已分配给销售；若销售未绑定Worker，自动加好友任务会阻塞等待绑定。 |
| `add_friend_blocked` | 加好友任务已创建但不可执行，原因是销售未绑定Worker；绑定Worker后自动恢复为待执行。 |
| `add_friend_pending` | 加好友任务可执行，等待Worker领取或Worker上线执行。 |
| `add_friend_sent` | 已发送添加通讯录邀请；不代表客户已同意或好友已添加成功。 |
| `friend_added` | Worker从会话列表识别到新会话，或发现已是好友后完成会话绑定。 |
| `ai_active` | AI正在负责接待，客户来消息后AI可继续回复。 |
| `waiting_user_reply` | AI已回复，正在等客户回。 |
| `recall_precheck` | 召回前确认读取中；还没有发送召回。 |
| `recalled_waiting_user` | 系统已做过召回，继续等客户回；到下一周期可再次召回。 |
| `waiting_sales_reply` | 已转销售，正在等销售回复客户。 |
| `sales_replied_waiting_user` | 销售已回复，正在等客户回。 |
| `rejected` | 客户明确拒绝，停止自动动作。 |
| `closed` | 销售移除短码或人工关闭，系统停止自动跟进。 |

## 7. 核心数据类图

```mermaid
classDiagram
    class Conversation {
        +string conversation_id
        +string lead_id
        +string sales_id
        +string status
        +string owner
        +bool ai_enabled
        +datetime last_inbound_at
        +datetime last_outbound_at
        +datetime last_ai_reply_at
        +datetime last_recall_at
        +datetime last_sales_reply_at
        +datetime sales_first_reply_at
        +datetime handoff_at
        +int recall_count
        +string remark_code
        +string close_reason
    }

    class MessageEvent {
        +string message_id
        +string conversation_id
        +string worker_id
        +string rpa_session_key
        +int contract_version
        +string read_run_id
        +string source_message_key
        +string dedupe_key
        +string sender_role
        +string message_type
        +string content
        +string item_state
        +string flow_state
        +float ocr_confidence
        +json raw_payload_V3
        +datetime occurred_at
        +datetime ingested_at
    }

    class WechatSessionBinding {
        +string binding_id
        +string lead_id
        +string conversation_id
        +string sales_id
        +string worker_id
        +string remark_code
        +string display_name
        +string rpa_session_key
        +string row_fingerprint
        +string bind_status
        +datetime first_seen_at
        +datetime last_seen_at
        +json last_scan_snapshot
    }

    class SessionScanRun {
        +string scan_id
        +string worker_id
        +string sidecar_run_id
        +datetime started_at
        +datetime finished_at
        +int accepted_count
        +int bound_count
        +int needs_review_count
    }

    class MessageReadRun {
        +string read_run_id
        +string worker_id
        +string conversation_id
        +string rpa_session_key
        +datetime started_at
        +datetime finished_at
        +int ingested_count
        +int duplicated_count
        +int ignored_count
    }

    class ListenState {
        +string conversation_id
        +string listen_status
        +datetime last_scan_at
        +datetime last_read_at
        +string last_error_code
        +string last_trace_id
    }

    class ReplyAction {
        +string reply_action_id
        +string conversation_id
        +string batch_id
        +string action_type
        +string status
        +datetime expire_at
    }

    class Task {
        +string task_id
        +string task_type
        +string status
        +string result_code
        +string error_code
        +string sales_id
        +string worker_id
        +string block_code
        +string block_reason
        +datetime scheduled_at
        +datetime lease_expires_at
    }

    class FollowUpTask {
        +string follow_up_id
        +string conversation_id
        +string rule_id
        +int recall_round
        +string status
        +datetime scheduled_at
    }

    class HandoffEvent {
        +string handoff_event_id
        +string conversation_id
        +string reason
        +datetime handoff_at
        +string notify_status
        +string notify_error
    }

    class Worker {
        +string worker_id
        +string status
        +datetime last_heartbeat_at
        +datetime last_sync_at
        +string current_task_type
    }

    class RemarkCode {
        +string remark_code
        +string lead_id
        +string sales_id
        +string phone_suffix
        +string status
        +datetime created_at
        +datetime bound_at
    }

    Conversation "1" --> "0..*" MessageEvent
    Conversation "1" --> "0..1" WechatSessionBinding
    Conversation "1" --> "0..1" ListenState
    Conversation "1" --> "0..*" ReplyAction
    Conversation "1" --> "0..*" FollowUpTask
    Conversation "1" --> "0..*" HandoffEvent
    Conversation "1" --> "0..*" Task
    Conversation "0..1" --> "1" RemarkCode
    Worker "1" --> "0..*" MessageEvent
    Worker "1" --> "0..*" WechatSessionBinding
    Worker "1" --> "0..*" SessionScanRun
    Worker "1" --> "0..*" MessageReadRun
    Worker "1" --> "0..*" Task
    SessionScanRun "1" --> "0..*" WechatSessionBinding
    MessageReadRun "1" --> "0..*" MessageEvent
```

## 8. 幂等约束图

```mermaid
flowchart TD
    Scan["session_scan_run<br/>unique(worker_id, scan_id)"]
    Bind["wechat_session_binding<br/>unique(worker_id, rpa_session_key)<br/>unique(active remark_code)"]
    Read["message_read_run<br/>unique(worker_id, read_run_id)"]
    M["message_event<br/>unique(worker_id, conversation_id, dedupe_key)"]
    B["message_batch<br/>单会话一个active_batch"]
    R["reply_action<br/>当前有效action才可发送"]
    S["send_receipt<br/>unique(reply_action_id)"]
    F["follow_up<br/>unique(conversation_id, rule_id, recall_round)"]
    H["handoff_notify<br/>同一销售超时周期只通知一次"]
    C0["remark_code<br/>全局唯一，一个有效短码只绑定一个会话"]

    Scan --> Bind
    Bind --> Read
    Read --> M
    M --> B
    B --> R
    R --> S
    F --> R
    H --> Notify["飞书一次通知<br/>记录sent/failed"]
    C0 --> RemarkBindingEffect["线下好友绑定/短码移除关闭自动跟进"]
```
