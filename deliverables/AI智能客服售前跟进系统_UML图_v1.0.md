# AI智能客服售前跟进系统 UML图

版本：v1.0

日期：2026-05-26

口径：第一期正式工程版本。本文只描述一期工程范围，不包含二期 SaaS 化、多商户、复杂权限、计费体系。

## 1. 系统组件图

```mermaid
flowchart LR
    subgraph Douyin["抖音/小风车线索来源"]
        LeadSource["手机号线索<br/>Excel/CSV/API适配器"]
    end

    subgraph Cloud["云端业务控制面"]
        LeadSvc["线索与销售服务"]
        TaskSvc["任务调度服务"]
        ConvSvc["会话状态服务"]
        RiskSvc["风控策略中心"]
        AISvc["OmniAuto AI编排服务"]
        RagSvc["RAG/知识库检索"]
        VisionSvc["千问视觉适配"]
        VehicleSvc["车源索引服务"]
        NotifySvc["飞书通知服务"]
        AuditSvc["日志审计服务"]
        DB[("业务数据库")]
    end

    subgraph LocalPC["商家侧Windows电脑"]
        Worker["Worker执行台"]
        Lock["Local WeChat UI Lock"]
        WeChatPC["微信桌面客户端"]
        ImageStore["本地图片缓存目录"]
    end

    subgraph SalesSide["销售侧"]
        SalesMobile["销售微信手机客户端"]
        Feishu["销售飞书"]
    end

    subgraph External["外部服务"]
        DeepSeek["DeepSeek文本模型"]
        QwenVL["千问视觉模型"]
        DFC["大风车API"]
        FeishuBot["飞书机器人"]
    end

    LeadSource --> LeadSvc
    LeadSvc --> DB
    LeadSvc --> TaskSvc
    TaskSvc --> DB
    ConvSvc --> DB
    RiskSvc --> DB
    AuditSvc --> DB

    Worker <--> TaskSvc
    Worker <--> ConvSvc
    Worker --> Lock
    Lock --> WeChatPC
    WeChatPC <--> SalesMobile
    Worker --> ImageStore

    AISvc --> DeepSeek
    AISvc --> RagSvc
    AISvc --> VehicleSvc
    AISvc --> RiskSvc
    VisionSvc --> QwenVL
    VehicleSvc <--> DFC
    NotifySvc --> FeishuBot
    FeishuBot --> Feishu
```

## 2. 主业务流程时序图

```mermaid
sequenceDiagram
    autonumber
    participant Source as 小风车/线索导入
    participant CP as 云端业务控制面
    participant TS as 任务调度服务
    participant W as Worker执行台
    participant WX as 微信桌面客户端
    participant C as 客户微信
    participant AI as OmniAuto/DeepSeek
    participant RAG as RAG/车源索引
    participant Risk as 风控/Guard
    participant FS as 飞书机器人
    participant S as 销售手机端

    Source->>CP: 导入手机号线索
    CP->>CP: 去重、脱敏、检查rejected/黑名单
    CP->>TS: 创建add_friend任务
    W->>TS: 拉取任务
    W->>WX: 获取Local WeChat UI Lock
    W->>WX: 手机号搜索、发送好友申请、写初始备注
    W->>TS: 回传add_friend结果
    CP->>CP: 更新线索/任务状态

    C->>WX: 通过好友并发送消息
    W->>WX: 监听会话消息
    W->>CP: 上报message_event(dedupe_key)
    CP->>CP: 绑定conversation，合并message_batch
    CP->>Risk: 前置风控检查
    CP->>AI: 请求候选回复(evidence_pack)
    AI->>RAG: 检索知识库和车源索引
    RAG-->>AI: 返回证据
    AI-->>CP: 返回候选回复/动作建议
    CP->>Risk: Guard发送前检查

    alt 可安全自动回复
        CP->>TS: 创建reply_action(status=queued)
        W->>TS: claim reply_action queued->sending
        W->>WX: 获取UI锁并发送回复
        W->>TS: sent_ack
        TS->>CP: reply_action=sent
    else 需要人工接管
        CP->>CP: ai_enabled=false, status=handoff_required
        CP->>FS: 创建并发送飞书通知
        FS->>S: 通知销售接管
    end

    S->>C: 销售手机端人工回复
    WX-->>W: 桌面端同步我方消息
    W->>CP: 上报human_sales消息
    CP->>CP: status=human_active, ai_enabled=false
```

## 3. 多客户并发消息处理时序图

```mermaid
sequenceDiagram
    autonumber
    participant WX as 微信桌面客户端
    participant W as Worker
    participant CP as 会话调度器
    participant AI as AI服务
    participant TS as 任务调度服务

    WX->>W: 客户A消息A1
    W->>CP: message_event A1
    CP->>CP: 创建A active_batch
    CP->>AI: 生成A1回复

    WX->>W: 客户B消息B1
    W->>CP: message_event B1
    CP->>CP: 创建B active_batch并排队

    WX->>W: 客户C消息C1
    W->>CP: message_event C1
    CP->>CP: 创建C active_batch并排队

    WX->>W: 客户A消息A2
    W->>CP: message_event A2
    CP->>CP: 合并到A active_batch，batch_version+1
    CP->>CP: 标记A1旧reply_action为superseded/cancelled
    CP->>AI: 基于A1+A2重新生成

    AI-->>CP: A最新候选回复
    CP->>TS: 创建A current reply_action
    W->>TS: claim A reply_action
    W->>WX: 发送A回复
    W->>TS: A sent_ack

    CP->>AI: 处理B1
    AI-->>CP: B候选回复
    CP->>TS: 创建B reply_action

    CP->>AI: 处理C1
    AI-->>CP: C候选回复
    CP->>TS: 创建C reply_action
```

## 4. 会话主状态机图

```mermaid
stateDiagram-v2
    [*] --> new: 线索入库
    new --> assigned: 分配销售/绑定Worker
    assigned --> add_friend_pending: 创建add_friend任务
    add_friend_pending --> add_friend_sent: 好友申请已提交
    add_friend_pending --> friend_added: 已是好友
    add_friend_pending --> failed: 加好友失败
    add_friend_sent --> friend_added: 客户通过/人工确认
    add_friend_sent --> failed: 申请失败/超时/受限
    failed --> add_friend_pending: 人工重试

    friend_added --> ai_chatting: 会话绑定成功且AI开启
    friend_added --> watching: 会话绑定但暂不自动回复

    ai_chatting --> ai_chatting: 正常文字/图片自动回复
    ai_chatting --> watching: 达到20条上限/客户观望
    ai_chatting --> handoff_required: 高意向/高风险/模型失败/低置信
    ai_chatting --> human_active: 检测到销售人工回复
    ai_chatting --> rejected: 客户明确拒绝

    watching --> ai_chatting: 人工恢复AI/客户重新咨询且允许自动回复
    watching --> handoff_required: 召回后高意向/风险
    watching --> human_active: 销售人工介入
    watching --> rejected: 客户拒绝
    watching --> closed: 人工关闭

    handoff_required --> human_active: 销售接管/销售发言
    handoff_required --> closed: 人工关闭

    human_active --> closed: 人工确认结束
    human_active --> watching: 人工确认回到观望

    rejected --> [*]: 不再自动处理
    closed --> [*]: 归档
```

## 5. Worker任务活动图

```mermaid
flowchart TD
    Start([Worker启动]) --> Heartbeat["上报heartbeat和环境状态"]
    Heartbeat --> Pull["拉取任务队列"]
    Pull --> HasTask{是否有任务}
    HasTask -- 否 --> Wait["等待下一轮轮询"]
    Wait --> Heartbeat
    HasTask -- 是 --> Type{任务类型}

    Type -- chat_reply --> ChatPre["上报/确认message_batch与reply_action"]
    ChatPre --> NeedAI{是否等待AI}
    NeedAI -- 是 --> Release["不占用UI锁，等待服务端结果"]
    Release --> Pull
    NeedAI -- 否 --> Claim["claim最新reply_action queued->sending"]

    Type -- add_friend --> ClaimAdd["领取add_friend任务"]
    Type -- follow_up --> ClaimFollow["领取follow_up任务"]

    Claim --> Lock
    ClaimAdd --> Lock
    ClaimFollow --> Lock

    Lock["申请Local WeChat UI Lock"] --> Locked{获取成功}
    Locked -- 否 --> RetryLater["释放任务/稍后重试/上报告警"]
    RetryLater --> Pull

    Locked -- 是 --> Execute["执行微信UI步骤"]
    Execute --> StepOK{步骤成功}
    StepOK -- 否 --> Screenshot["截图、记录窗口标题、错误原因"]
    Screenshot --> RiskPause{是否微信风险提示}
    RiskPause -- 是 --> PauseWorker["暂停Worker或对应任务类型"]
    RiskPause -- 否 --> Fail["任务失败/可人工重试"]
    PauseWorker --> Unlock
    Fail --> Unlock

    StepOK -- 是 --> Ack["回传执行结果/sent_ack"]
    Ack --> Unlock["释放UI锁"]
    Unlock --> Pull
```

## 6. 核心数据类图

```mermaid
classDiagram
    class Lead {
        +string lead_id
        +string phone_hash
        +string phone_masked
        +string source
        +string sales_id
        +string worker_id
        +string status
        +string remark_code
        +datetime last_contact_at
        +bool reject_flag
        +int recall_count
    }

    class Sales {
        +string sales_id
        +string sales_name
        +string wechat_account
        +string worker_id
        +string feishu_user_id
        +bool enabled
        +int daily_add_friend_limit
    }

    class Worker {
        +string worker_id
        +string device_name
        +string sales_id
        +string wechat_version
        +string worker_version
        +string status
        +datetime last_heartbeat_at
        +string current_task_type
    }

    class Task {
        +string task_id
        +string task_type
        +string lead_id
        +string conversation_id
        +string worker_id
        +string status
        +int retry_count
        +string failure_reason
        +datetime lease_expires_at
    }

    class Conversation {
        +string conversation_id
        +string lead_id
        +string sales_id
        +bool ai_enabled
        +string status
        +int reply_count
        +datetime last_customer_msg_at
        +datetime last_sales_msg_at
        +string handoff_reason
    }

    class MessageEvent {
        +string message_id
        +string conversation_id
        +string dedupe_key
        +string sender_type
        +string content_type
        +string content
        +datetime received_at
    }

    class MessageBatch {
        +string batch_id
        +string conversation_id
        +int batch_version
        +string status
        +datetime first_msg_at
        +datetime last_msg_at
    }

    class ReplyAction {
        +string reply_action_id
        +string batch_id
        +string conversation_id
        +string status
        +string reply_text_hash
        +datetime expire_at
    }

    class SendReceipt {
        +string receipt_id
        +string reply_action_id
        +string worker_id
        +string status
        +datetime sent_at
    }

    class HandoffEvent {
        +string handoff_event_id
        +string conversation_id
        +string reason
        +string notify_status
        +string notify_error
        +datetime created_at
    }

    class FollowUpTask {
        +string follow_up_task_id
        +string lead_id
        +string rule_id
        +int recall_round
        +string status
        +datetime scheduled_at
    }

    Lead "1" --> "0..1" Sales
    Sales "1" --> "0..1" Worker
    Lead "1" --> "0..*" Task
    Lead "1" --> "0..1" Conversation
    Conversation "1" --> "0..*" MessageEvent
    Conversation "1" --> "0..*" MessageBatch
    MessageBatch "1" --> "0..*" ReplyAction
    ReplyAction "1" --> "0..1" SendReceipt
    Conversation "1" --> "0..*" HandoffEvent
    Lead "1" --> "0..*" FollowUpTask
```

## 7. 关键幂等约束图

```mermaid
flowchart TD
    M["message_event<br/>unique(worker_id, conversation_id, dedupe_key)"]
    B["message_batch<br/>同conversation最多一个active_batch"]
    R["reply_action<br/>reply_action_id全局唯一"]
    C["claim发送<br/>queued -> sending 原子更新"]
    S["send_receipt<br/>unique(reply_action_id)"]
    F["follow_up_task<br/>unique(lead_id, rule_id, recall_round)"]
    H["handoff_event<br/>unique(conversation_id, reason_group, active_period)"]

    M --> B
    B --> R
    R --> C
    C --> S
    F --> S
    H --> Notify["飞书机器人一次通知<br/>记录sent/failed和错误日志"]
```
