# AI智能客服售前跟进系统 UML图（状态机与备注短码重构版）

版本：v1.2

日期：2026-05-27

口径：第一期正式工程版本。取消 AI 固定轮次限制，采用长期跟进状态机；备注短码作为系统托管开关。

## 1. 职责边界组件图

```mermaid
flowchart LR
    subgraph Cloud["云端业务控制面"]
        LeadSvc["线索与销售服务"]
        ConvSvc["会话状态机"]
        Scheduler["服务端定时扫描器"]
        TaskSvc["任务调度服务"]
        RemarkTool["备注短码工具"]
        AISvc["OmniAuto/DeepSeek"]
        RiskSvc["风控/Guard"]
        VehicleSvc["车源索引"]
        NotifySvc["飞书通知"]
        DB[("业务数据库")]
    end

    subgraph WorkerSide["商家侧Windows电脑"]
        Worker["Worker执行台"]
        FactScan["微信事实监听/补偿扫描"]
        Lock["Local WeChat UI Lock"]
        WeChatPC["微信桌面客户端"]
        ImageStore["图片缓存目录"]
    end

    subgraph SalesSide["销售侧"]
        SalesMobile["销售微信手机客户端"]
        Feishu["销售飞书"]
    end

    Worker --> FactScan
    FactScan --> WeChatPC
    Worker --> Lock
    Lock --> WeChatPC
    WeChatPC <--> SalesMobile
    Worker --> ImageStore

    Worker -- "message_event / human_sales_event / remark_code_detected / remark_code_removed / sent_ack / wechat_error" --> ConvSvc
    TaskSvc -- "add_friend / chat_reply / follow_up" --> Worker
    ConvSvc --> DB
    Scheduler --> DB
    Scheduler --> TaskSvc
    Scheduler --> NotifySvc
    RemarkTool --> DB
    RemarkTool --> LeadSvc
    ConvSvc --> AISvc
    AISvc --> RiskSvc
    AISvc --> VehicleSvc
    NotifySvc --> Feishu

    LeadSvc --> DB
    LeadSvc --> TaskSvc
```

## 2. Worker与服务端扫描分工图

```mermaid
flowchart TD
    A["Worker定时/准实时扫描<br/>系统绑定会话"] --> B{发现微信事实?}
    B -- 客户文字/图片 --> C["上报message_event/image_event"]
    B -- 销售人工回复 --> D["上报human_sales_event"]
    B -- 发送结果 --> E["上报sent_ack/failed_ack"]
    B -- 微信异常 --> F["上报wechat_error"]
    B -- 备注新增短码 --> G1["上报remark_code_detected"]
    B -- 备注移除短码 --> G2["上报remark_code_removed"]

    C --> S["服务端状态机"]
    D --> S
    E --> S
    F --> S
    G1 --> S
    G2 --> S

    G["服务端定时扫描数据库"] --> H{命中业务规则?}
    H -- 等待用户超过N天 --> I["创建follow_up_task"]
    H -- 等待销售超过N天 --> J["发送飞书通知销售"]
    H -- Worker离线/卡住 --> K["标记异常/生成待办"]

    I --> W["Worker领取任务并操作微信"]
    J --> L["记录HandoffEvent通知结果"]
    S -- 短码新增 --> M1["绑定线下好友并进入系统跟进"]
    S -- 短码移除 --> M2["status=closed<br/>停止AI/召回/飞书提醒"]
```

## 3. 主业务时序图

```mermaid
sequenceDiagram
    autonumber
    participant C as 客户微信
    participant WX as 微信桌面客户端
    participant W as Worker
    participant CP as 服务端状态机
    participant AI as OmniAuto/DeepSeek
    participant TS as 任务调度
    participant RT as 备注短码工具
    participant FS as 飞书机器人
    participant S as 销售手机端

    C->>WX: 客户发送消息
    W->>WX: 监听系统绑定会话
    W->>CP: 上报message_event
    CP->>CP: 去重并更新last_inbound_at

    alt 可由AI继续接待
        CP->>AI: 生成候选回复
        AI-->>CP: 返回候选回复
        CP->>TS: 创建chat_reply/reply_action
        W->>TS: claim reply_action
        W->>WX: 发送AI回复
        W->>CP: sent_ack
        CP->>CP: status=waiting_user_reply, last_outbound_at=now
    else 需要人工接管
        CP->>CP: status=waiting_human_reply, ai_enabled=false, handoff_at=now
        CP->>FS: 飞书通知销售
        FS->>S: 销售收到接管通知
    end

    opt 线下好友通过备注短码进入系统
        S->>RT: 生成remark_code和推荐备注
        S->>WX: 手动修改微信好友备注，加入短码
        W->>CP: 上报remark_code_detected
        CP->>CP: 创建/绑定Lead与Conversation
        CP->>CP: status=ai_active
    end
```

## 4. 等待用户回复与多轮召回时序图

```mermaid
sequenceDiagram
    autonumber
    participant CP as 服务端状态机
    participant Scan as 服务端定时扫描器
    participant TS as 任务调度
    participant W as Worker
    participant WX as 微信桌面客户端
    participant C as 客户

    CP->>CP: AI/人工/召回发送成功
    CP->>CP: status=waiting_user_reply或human_replied_waiting_user
    CP->>CP: last_outbound_at=now

    loop 定时扫描数据库
        Scan->>CP: 检查等待用户回复类状态
        alt now-last_outbound_at>=N天 且客户未回复
            Scan->>TS: 创建follow_up_task
            W->>TS: 领取follow_up
            W->>WX: 发送AI召回内容
            W->>CP: sent_ack
            CP->>CP: status=recalled_waiting_user, recall_count+1, last_recall_at=now
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

    CP->>CP: 进入waiting_human_reply, handoff_at=now

    alt 销售及时回复
        S->>WX: 销售手机端回复客户
        W->>WX: 桌面端同步我方消息
        W->>CP: 上报human_sales_event
        CP->>CP: sales_first_reply_at=now, status=human_replied_waiting_user
    else 销售超过N天未回复
        Scan->>CP: 检查waiting_human_reply超时
        CP->>FS: 飞书通知销售
        FS-->>CP: sent/failed
        CP->>CP: HandoffEvent记录通知结果和错误摘要
    end
```

## 6. 会话状态机图

```mermaid
stateDiagram-v2
    [*] --> new: 线索入库
    new --> assigned: 分配销售/绑定Worker
    assigned --> add_friend_pending: 创建加好友任务
    add_friend_pending --> add_friend_sent: 好友申请已发
    add_friend_pending --> friend_added: 已是好友
    add_friend_sent --> friend_added: 客户通过

    friend_added --> ai_active: 会话绑定成功
    [*] --> ai_active: 线下好友备注新增有效短码

    ai_active --> waiting_user_reply: AI回复成功
    waiting_user_reply --> ai_active: 客户回复且AI仍负责
    waiting_user_reply --> recalled_waiting_user: N天未回复后召回成功
    recalled_waiting_user --> ai_active: 客户回复且AI仍负责
    recalled_waiting_user --> recalled_waiting_user: 下一周期继续召回

    ai_active --> waiting_human_reply: 高意向/高风险/模型失败/低置信
    waiting_user_reply --> waiting_human_reply: 客户回复后需人工处理
    recalled_waiting_user --> waiting_human_reply: 召回后客户高意向/风险

    waiting_human_reply --> human_replied_waiting_user: 销售人工回复
    waiting_human_reply --> waiting_human_reply: 销售超时飞书通知
    human_replied_waiting_user --> waiting_human_reply: 客户回复后继续由销售处理
    human_replied_waiting_user --> recalled_waiting_user: N天未回复后AI召回

    ai_active --> rejected: 客户拒绝
    waiting_user_reply --> rejected: 客户拒绝
    recalled_waiting_user --> rejected: 客户拒绝
    waiting_human_reply --> rejected: 客户拒绝
    human_replied_waiting_user --> rejected: 客户拒绝

    ai_active --> closed: 备注短码移除/人工关闭
    waiting_user_reply --> closed: 备注短码移除/人工关闭
    recalled_waiting_user --> closed: 备注短码移除/人工关闭
    waiting_human_reply --> closed: 备注短码移除/人工关闭
    human_replied_waiting_user --> closed: 备注短码移除/人工关闭
    rejected --> [*]
    closed --> [*]
```

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
        +string dedupe_key
        +string sender_type
        +string content_type
        +datetime received_at
    }

    class ReplyAction {
        +string reply_action_id
        +string conversation_id
        +string batch_id
        +string action_type
        +string status
        +datetime expire_at
    }

    class FollowUpTask {
        +string follow_up_task_id
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
    Conversation "1" --> "0..*" ReplyAction
    Conversation "1" --> "0..*" FollowUpTask
    Conversation "1" --> "0..*" HandoffEvent
    Conversation "0..1" --> "1" RemarkCode
    Worker "1" --> "0..*" MessageEvent
```

## 8. 幂等约束图

```mermaid
flowchart TD
    M["message_event<br/>unique(worker_id, conversation_id, dedupe_key)"]
    B["message_batch<br/>单会话一个active_batch"]
    R["reply_action<br/>当前有效action才可发送"]
    S["send_receipt<br/>unique(reply_action_id)"]
    F["follow_up_task<br/>unique(conversation_id, rule_id, recall_round)"]
    H["handoff_notify<br/>同一销售超时周期只通知一次"]
    C0["remark_code<br/>全局唯一，一个有效短码只绑定一个会话"]

    M --> B
    B --> R
    R --> S
    F --> R
    H --> Notify["飞书一次通知<br/>记录sent/failed"]
    C0 --> Bind["线下好友绑定/短码移除关闭自动跟进"]
```
