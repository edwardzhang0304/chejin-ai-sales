# AI智能客服售前跟进系统 后端开发清单

版本：v0.1

日期：2026-06-02

模块：线索人工录入、线索分配、销售管理、操作日志、导出

## 1. 结论

当前阶段后端 P0 只实现运营后台基础能力：

- 人工新增客户线索。
- 手机号强去重。
- 重复录入记录与重复备注追加。
- 销售轮询自动分配。
- 线索列表、详情、编辑。
- 标记无效与恢复有效。
- 销售基础管理。
- 独立操作日志页面。
- 手机号明文查看审计。
- 选中线索导出。

本阶段不实现：

- 抖音企业号小风车 API 自动同步。
- 飞鱼/巨量线索 Webhook。
- RPA 自动加微信。
- Worker 执行台。
- AI 自动回复。
- 图片识别。
- 自动召回。
- 飞书接管通知。
- 正式批量导入。
- 销售登录后台。

## 2. 关键业务口径

### 2.1 线索状态

| 状态 | 说明 |
|---|---|
| `unassigned` | 暂无可用销售，系统未能自动分配。 |
| `assigned` | 已按轮询规则分配销售。 |
| `invalid` | 无效线索。 |

本阶段不进入 `add_friend_pending`、`ai_chatting`、`watching` 等后续自动化状态。

### 2.2 无效原因枚举

无效原因字段用于“标记无效线索”动作，写入 `leads.invalid_reason` 和操作日志。

| 枚举值 | 前端展示 |
|---|---|
| `empty_number` | 空号 |
| `wrong_info` | 信息错误 |
| `not_target_customer` | 非目标客户 |
| `test_data` | 测试数据 |
| `duplicate_or_mistaken` | 重复/误录 |
| `other` | 其他 |

说明：`重复/误录` 用在运营人工判断某条线索无效时，不等同于系统自动手机号去重。系统自动手机号去重应写入 `lead_duplicate_events`，不自动把原线索标记为无效。

### 2.3 去重规则

- P0 强去重只使用手机号。
- 手机号标准化后写入 `contact_hash`，避免依赖明文做唯一判断。
- 同一条线索内同类联系方式不允许重复。
- 同一条线索内多个手机号，任意一个命中已有有效线索，则本次新增不得创建新线索。
- `invalid` 线索参与潜在重复提示，但不强拦截。
- 微信、邮箱不作为 P0 强去重键，只作为潜在重复提醒。
- 重复手机号必须写入 `lead_duplicate_events`。
- 重复手机号如填写备注，必须追加 `lead_notes.note_type=duplicate_append`。
- 原线索 `duplicate_count + 1`，`last_duplicate_at` 更新为当前时间。

### 2.4 轮询分配规则

- 新增线索保存成功且手机号不重复时，立即触发轮询自动分配。
- 可分配销售条件：`enabled=true`、`deleted_at is null`、`sales_name` 非空、`participate_in_round_robin=true`。
- 可分配销售排序：`sort_order asc nulls last, id asc`。
- 服务端维护全局轮询指针。
- 分配时必须对 `assignment_round_robin_state` 加事务锁。
- 无可用销售时，线索保持 `unassigned`，写入失败分配记录。
- “重新分配线索”只对未分配线索触发自动轮询，不允许手动选择销售。

## 3. 数据库表

### 3.1 leads

| 字段 | 类型建议 | 说明 |
|---|---|---|
| `id` | uuid/bigint | 线索 ID |
| `customer_name` | varchar(50) | 客户名称 |
| `status` | varchar(32) | `unassigned`、`assigned`、`invalid` |
| `source_type` | varchar(32) | P0 固定 `manual` |
| `source_name_snapshot` | varchar(64) | P0 固定 `人工录入` |
| `sales_id` | uuid/bigint nullable | 当前销售 |
| `assigned_at` | timestamptz nullable | 最近分配时间 |
| `assign_status` | varchar(32) | `unassigned`、`assigned`、`assign_failed` |
| `assign_failure_reason` | varchar(255) nullable | 分配失败原因 |
| `remark` | text nullable | 当前线索备注摘要/主备注 |
| `invalid_reason` | varchar(64) nullable | 无效原因 |
| `invalid_remark` | text nullable | 无效备注 |
| `invalid_at` | timestamptz nullable | 标记无效时间 |
| `invalid_by` | uuid/bigint nullable | 标记无效操作人 |
| `duplicate_count` | int | 重复录入次数，默认 0 |
| `last_duplicate_at` | timestamptz nullable | 最近重复录入时间 |
| `custom_fields` | jsonb | 自定义字段 |
| `created_by` | uuid/bigint | 创建人 |
| `created_at` | timestamptz | 创建时间 |
| `updated_by` | uuid/bigint nullable | 更新人 |
| `updated_at` | timestamptz | 更新时间 |
| `deleted_at` | timestamptz nullable | 预留软删除，本阶段不提供删除入口 |

建议索引：

- `idx_leads_status_created_at(status, created_at desc)`
- `idx_leads_sales_id_status(sales_id, status)`
- `idx_leads_created_by_created_at(created_by, created_at desc)`
- `idx_leads_last_duplicate_at(last_duplicate_at desc)`

### 3.2 lead_contacts

| 字段 | 类型建议 | 说明 |
|---|---|---|
| `id` | uuid/bigint | 联系方式 ID |
| `lead_id` | uuid/bigint | 线索 ID |
| `contact_type` | varchar(16) | `phone`、`wechat`、`email` |
| `contact_value_encrypted` | text | 加密后的原始值 |
| `contact_value_normalized` | varchar(128) | 标准化值 |
| `contact_hash` | varchar(128) | 哈希值 |
| `masked_value` | varchar(128) | 脱敏展示值 |
| `is_primary` | bool | 是否主联系方式 |
| `created_at` | timestamptz | 创建时间 |

建议索引：

- `idx_lead_contacts_lead_id(lead_id)`
- `idx_lead_contacts_type_hash(contact_type, contact_hash)`
- 有效手机号强去重通过服务端事务判断完成；不建议直接对全部手机号建唯一索引，因为 `invalid` 线索不强拦截。

### 3.3 lead_notes

| 字段 | 类型建议 | 说明 |
|---|---|---|
| `id` | uuid/bigint | 备注 ID |
| `lead_id` | uuid/bigint | 线索 ID |
| `note_type` | varchar(32) | `manual`、`duplicate_append`、`system` |
| `content` | text | 备注内容 |
| `operator_id` | uuid/bigint | 操作人 |
| `created_at` | timestamptz | 创建时间 |

### 3.4 lead_duplicate_events

| 字段 | 类型建议 | 说明 |
|---|---|---|
| `id` | uuid/bigint | 重复事件 ID |
| `lead_id` | uuid/bigint | 命中的原线索 ID |
| `matched_contact_hash` | varchar(128) | 命中的手机号哈希 |
| `submitted_customer_name` | varchar(50) | 本次提交客户名 |
| `submitted_phone_masked` | varchar(64) | 本次手机号脱敏值 |
| `submitted_wechat_masked` | text nullable | 本次微信脱敏值 |
| `submitted_email_masked` | text nullable | 本次邮箱脱敏值 |
| `submitted_remark` | text nullable | 本次备注 |
| `submitted_payload` | jsonb | 本次提交的完整脱敏快照 |
| `operator_id` | uuid/bigint | 操作人 |
| `created_at` | timestamptz | 重复录入时间 |

### 3.5 lead_assignments

| 字段 | 类型建议 | 说明 |
|---|---|---|
| `id` | uuid/bigint | 分配记录 ID |
| `lead_id` | uuid/bigint | 线索 ID |
| `from_sales_id` | uuid/bigint nullable | 原销售，P0 通常为空 |
| `to_sales_id` | uuid/bigint nullable | 新销售，失败时为空 |
| `assignment_type` | varchar(32) | `round_robin`、`retry_round_robin` |
| `assignment_status` | varchar(32) | `succeeded`、`failed` |
| `failure_reason` | varchar(255) nullable | 失败原因 |
| `round_robin_cursor_before` | uuid/bigint nullable | 分配前指针 |
| `round_robin_cursor_after` | uuid/bigint nullable | 分配后指针 |
| `operator_id` | uuid/bigint nullable | 触发人；系统自动分配为空 |
| `remark` | text nullable | 系统备注 |
| `created_at` | timestamptz | 分配时间 |

### 3.6 sales

| 字段 | 类型建议 | 说明 |
|---|---|---|
| `id` | uuid/bigint | 销售 ID |
| `sales_name` | varchar(50) | 销售姓名 |
| `phone` | varchar(64) nullable | 销售手机号 |
| `wechat` | varchar(64) nullable | 销售微信 |
| `feishu_user_id` | varchar(128) nullable | 飞书用户 ID，后续预留 |
| `enabled` | bool | 是否启用 |
| `participate_in_round_robin` | bool | 是否参与轮询 |
| `sort_order` | int nullable | 轮询排序 |
| `remark` | text nullable | 备注 |
| `created_at` | timestamptz | 创建时间 |
| `updated_at` | timestamptz | 更新时间 |
| `deleted_at` | timestamptz nullable | 软删除预留 |

### 3.7 assignment_round_robin_state

| 字段 | 类型建议 | 说明 |
|---|---|---|
| `id` | int | P0 固定 `1` |
| `current_sales_id` | uuid/bigint nullable | 当前指针销售 |
| `updated_at` | timestamptz | 更新时间 |

执行分配时使用 `select ... for update` 锁定该行。

### 3.8 operation_logs

| 字段 | 类型建议 | 说明 |
|---|---|---|
| `id` | uuid/bigint | 日志 ID |
| `event_type` | varchar(64) | 事件类型 |
| `module` | varchar(32) | `lead`、`sales`、`assignment`、`export`、`auth` |
| `target_type` | varchar(32) | `lead`、`sales`、`contact`、`export_task` 等 |
| `target_id` | uuid/bigint nullable | 目标 ID |
| `lead_id` | uuid/bigint nullable | 关联线索，便于详情抽屉查询 |
| `operator_id` | uuid/bigint nullable | 操作人 |
| `operator_name_snapshot` | varchar(64) nullable | 操作人快照 |
| `ip_address` | varchar(64) nullable | 请求 IP |
| `user_agent` | text nullable | UA |
| `request_id` | varchar(64) nullable | 请求追踪 ID |
| `before_data` | jsonb nullable | 变更前脱敏快照 |
| `after_data` | jsonb nullable | 变更后脱敏快照 |
| `metadata` | jsonb | 扩展信息 |
| `created_at` | timestamptz | 操作时间 |

P0 事件类型：

- `lead_created`
- `lead_updated`
- `lead_marked_invalid`
- `lead_restored`
- `lead_auto_assigned`
- `lead_assign_failed`
- `lead_retry_assign`
- `duplicate_detected`
- `duplicate_note_appended`
- `phone_revealed`
- `sales_created`
- `sales_updated`
- `sales_enabled_changed`
- `sales_round_robin_changed`
- `leads_exported`

### 3.9 export_tasks

导出选中线索进入 P0，建议采用同步小文件导出 + 任务表留痕。若预计数据量较小，可接口直接返回文件，同时写 `export_tasks` 和 `operation_logs`。

| 字段 | 类型建议 | 说明 |
|---|---|---|
| `id` | uuid/bigint | 导出任务 ID |
| `export_type` | varchar(32) | `selected_leads` |
| `status` | varchar(32) | `processing`、`completed`、`failed` |
| `selected_count` | int | 选中数量 |
| `file_name` | varchar(255) | 文件名 |
| `file_path` | text nullable | 文件路径或对象存储 key |
| `failure_reason` | text nullable | 失败原因 |
| `operator_id` | uuid/bigint | 操作人 |
| `created_at` | timestamptz | 创建时间 |
| `completed_at` | timestamptz nullable | 完成时间 |

## 4. API 设计

统一响应建议：

```json
{
  "code": "OK",
  "message": "success",
  "data": {}
}
```

错误响应建议：

```json
{
  "code": "LEAD_PHONE_DUPLICATED",
  "message": "该手机号已存在，不能重复新建",
  "data": {
    "duplicate_lead": {}
  }
}
```

### 4.1 线索列表

`GET /api/leads`

查询参数：

| 参数 | 说明 |
|---|---|
| `keyword` | 客户名称、手机号后四位、微信、备注 |
| `status` | `assigned`、`unassigned`、`invalid` |
| `sales_id` | 销售 ID |
| `created_by` | 创建人 |
| `created_at_from` | 创建开始时间 |
| `created_at_to` | 创建结束时间 |
| `has_duplicate` | 是否有重复录入 |
| `page` | 页码 |
| `page_size` | 每页条数 |

返回字段：

```json
{
  "items": [
    {
      "id": "lead_1",
      "customer_name": "王先生",
      "status": "assigned",
      "source_type": "manual",
      "source_name_snapshot": "人工录入",
      "primary_phone_masked": "138****1234",
      "primary_wechat_masked": "wx_****89",
      "sales_id": "sales_1",
      "sales_name": "李销售",
      "assign_status": "assigned",
      "assign_failure_reason": null,
      "remark_summary": "想看 SUV",
      "duplicate_count": 2,
      "last_duplicate_at": "2026-06-02T14:30:00+08:00",
      "created_by_name": "运营小陈",
      "created_at": "2026-06-02T10:00:00+08:00",
      "updated_at": "2026-06-02T14:30:00+08:00"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

### 4.2 线索概览

`GET /api/leads/stats`

返回：

```json
{
  "today_new_count": 12,
  "assigned_count": 100,
  "unassigned_count": 5,
  "duplicate_event_count": 8
}
```

### 4.3 新增线索

`POST /api/leads`

请求：

```json
{
  "customer_name": "王先生",
  "phones": ["13800138000"],
  "wechats": ["wx_test"],
  "emails": ["test@example.com"],
  "remark": "客户想看 SUV，预算 10 万左右",
  "custom_fields": {
    "意向车型": "SUV",
    "预算": "10万"
  }
}
```

成功创建返回：

```json
{
  "created": true,
  "lead": {
    "id": "lead_1",
    "status": "assigned",
    "sales_id": "sales_1",
    "sales_name": "李销售"
  },
  "assignment": {
    "status": "succeeded",
    "failure_reason": null
  },
  "potential_duplicates": {
    "wechat": [],
    "email": []
  }
}
```

手机号重复返回 `409`：

```json
{
  "code": "LEAD_PHONE_DUPLICATED",
  "message": "该手机号已存在，不能重复新建。已重复录入 3 次，日期：2026-06-01、2026-06-02。本次备注已追加到原线索。",
  "data": {
    "created": false,
    "duplicate_lead": {
      "id": "lead_1",
      "customer_name": "王先生",
      "primary_phone_masked": "138****8000",
      "sales_id": "sales_1",
      "sales_name": "李销售",
      "created_at": "2026-06-01T10:00:00+08:00",
      "updated_at": "2026-06-02T14:30:00+08:00"
    },
    "duplicate_count": 3,
    "duplicate_dates": ["2026-06-01", "2026-06-02"],
    "note_appended": true
  }
}
```

### 4.4 线索详情

`GET /api/leads/{id}`

返回内容包含：

- 基础信息。
- 联系方式脱敏值。
- 分配信息。
- 分配记录。
- 重复录入记录。
- 备注记录。
- 任务链路节点。
- 操作日志摘要。

### 4.5 编辑线索

`PUT /api/leads/{id}`

规则：

- 可修改客户名称、联系方式、备注、自定义字段。
- 修改手机号时仍需执行手机号强去重。
- 如果新增手机号命中其他有效线索，拒绝更新。
- 写 `operation_logs.event_type=lead_updated`。

### 4.6 标记无效

`POST /api/leads/{id}/mark-invalid`

请求：

```json
{
  "invalid_reason": "duplicate_or_mistaken",
  "invalid_remark": "运营确认重复录入"
}
```

规则：

- `invalid_reason` 必填。
- 状态更新为 `invalid`。
- 不清空 `sales_id`。
- 写操作日志 `lead_marked_invalid`。

### 4.7 恢复有效

`POST /api/leads/{id}/restore`

规则：

- 如果 `sales_id` 存在，恢复为 `assigned`。
- 如果 `sales_id` 不存在，恢复为 `unassigned`。
- 清空 `invalid_reason`、`invalid_remark`、`invalid_at`、`invalid_by`。
- 写操作日志 `lead_restored`。

### 4.8 重新分配线索

`POST /api/leads/retry-auto-assign`

请求：

```json
{
  "lead_ids": ["lead_1", "lead_2"]
}
```

规则：

- 只处理 `status=unassigned` 的线索。
- 不处理 `invalid` 和已分配线索。
- 每条线索都写分配记录。
- 返回成功、失败、跳过数量和逐条原因。

### 4.9 销售管理

`GET /api/sales`

支持返回 `lead_count`，供销售管理页面展示名下线索数量。

`POST /api/sales`

`PUT /api/sales/{id}`

请求字段：

```json
{
  "sales_name": "李销售",
  "phone": "13800138000",
  "wechat": "sales_wechat",
  "feishu_user_id": null,
  "enabled": true,
  "participate_in_round_robin": true,
  "sort_order": 10,
  "remark": "杭州门店"
}
```

### 4.10 备注与重复记录

`GET /api/leads/{id}/notes`

`GET /api/leads/{id}/duplicate-events`

### 4.11 操作日志独立页

`GET /api/operation-logs`

查询参数：

| 参数 | 说明 |
|---|---|
| `keyword` | 客户名称、销售名称、操作人、目标 ID |
| `event_type` | 事件类型 |
| `module` | 模块 |
| `operator_id` | 操作人 |
| `target_type` | 目标类型 |
| `target_id` | 目标 ID |
| `created_at_from` | 开始时间 |
| `created_at_to` | 结束时间 |
| `page` | 页码 |
| `page_size` | 每页条数 |

返回：

```json
{
  "items": [
    {
      "id": "log_1",
      "event_type": "phone_revealed",
      "event_name": "查看手机号明文",
      "module": "lead",
      "target_type": "contact",
      "target_id": "contact_1",
      "lead_id": "lead_1",
      "lead_customer_name": "王先生",
      "operator_id": "user_1",
      "operator_name": "运营小陈",
      "ip_address": "127.0.0.1",
      "created_at": "2026-06-02T14:30:00+08:00",
      "metadata": {
        "reason": "运营查看客户联系方式"
      }
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

`GET /api/leads/{id}/operation-logs`

供线索详情抽屉展示该线索相关日志。

### 4.12 手机号明文查看

`POST /api/leads/{lead_id}/contacts/{contact_id}/reveal`

请求：

```json
{
  "reason": "联系客户"
}
```

规则：

- 仅允许有权限的管理员或运营查看。
- 每次查看必须写 `operation_logs.event_type=phone_revealed`。
- 日志中不得保存完整手机号，只保存 contact_id、手机号后四位、原因、IP、UA。
- 返回完整手机号。

返回：

```json
{
  "contact_id": "contact_1",
  "contact_type": "phone",
  "contact_value": "13800138000"
}
```

### 4.13 导出选中线索

`POST /api/leads/export`

请求：

```json
{
  "lead_ids": ["lead_1", "lead_2"],
  "fields": [
    "customer_name",
    "primary_phone_masked",
    "primary_wechat_masked",
    "status",
    "sales_name",
    "remark",
    "created_at"
  ]
}
```

P0 默认导出脱敏联系方式，不导出手机号明文。

如后续要求导出明文手机号，必须单独权限控制，并对每个导出任务写审计日志，记录导出数量、字段、操作人和原因。

返回方式：

- 数据量小：直接返回 CSV/XLSX 文件流。
- 同时写 `export_tasks` 和 `operation_logs.event_type=leads_exported`。

## 5. 核心事务流程

### 5.1 新增线索

```text
开始事务
-> 参数校验
-> 标准化手机号/微信/邮箱
-> 检查同类联系方式是否重复
-> 查询有效线索手机号是否命中
-> 如果重复：
   -> 写 lead_duplicate_events
   -> 如有备注，写 duplicate_append note
   -> 更新原线索 duplicate_count / last_duplicate_at
   -> 写 operation_logs duplicate_detected / duplicate_note_appended
   -> 提交事务
   -> 返回 409 和原线索摘要
-> 如果不重复：
   -> 创建 leads
   -> 创建 lead_contacts
   -> 如有备注，写 lead_notes manual
   -> 写 operation_logs lead_created
   -> 执行轮询分配
   -> 写 lead_assignments
   -> 写 operation_logs lead_auto_assigned 或 lead_assign_failed
-> 提交事务
```

### 5.2 轮询分配

```text
开始事务
-> select assignment_round_robin_state where id=1 for update
-> 查询可分配销售列表 order by sort_order asc nulls last, id asc
-> 无可用销售：
   -> lead.status=unassigned
   -> lead.assign_status=assign_failed
   -> 写 failed assignment
-> 有可用销售：
   -> 根据 current_sales_id 找到当前指针位置
   -> 分配给当前或下一位可用销售
   -> lead.status=assigned
   -> lead.sales_id=to_sales_id
   -> lead.assigned_at=now
   -> lead.assign_status=assigned
   -> 更新 current_sales_id 为下一位销售
   -> 写 succeeded assignment
-> 提交事务
```

## 6. 错误码

| 错误码 | HTTP | 说明 |
|---|---:|---|
| `VALIDATION_ERROR` | 400 | 参数校验失败 |
| `LEAD_CUSTOMER_NAME_REQUIRED` | 400 | 客户名称必填 |
| `LEAD_PHONE_REQUIRED` | 400 | 手机号必填 |
| `LEAD_PHONE_INVALID` | 400 | 手机号格式错误 |
| `LEAD_CONTACT_DUPLICATED_IN_REQUEST` | 400 | 同类联系方式在本次提交中重复 |
| `LEAD_PHONE_DUPLICATED` | 409 | 手机号命中已有有效线索 |
| `LEAD_NOT_FOUND` | 404 | 线索不存在 |
| `LEAD_INVALID_REASON_REQUIRED` | 400 | 标记无效原因必填 |
| `LEAD_STATUS_NOT_ASSIGNABLE` | 409 | 当前状态不可重新分配 |
| `SALES_NOT_FOUND` | 404 | 销售不存在 |
| `SALES_NAME_REQUIRED` | 400 | 销售姓名必填 |
| `ASSIGNMENT_NO_AVAILABLE_SALES` | 200 | 无可用销售，业务失败但请求成功 |
| `ASSIGNMENT_LOCK_FAILED` | 409 | 轮询锁获取失败 |
| `CONTACT_REVEAL_FORBIDDEN` | 403 | 无权查看联系方式明文 |
| `EXPORT_EMPTY_SELECTION` | 400 | 导出未选择线索 |
| `EXPORT_TOO_MANY_ROWS` | 400 | 导出数量超过限制 |

## 7. 权限与安全

- 手机号默认脱敏返回。
- 明文手机号仅通过 reveal 接口返回。
- reveal 必须记录 `phone_revealed` 操作日志。
- 日志、导出记录、重复事件中不得保存完整明文手机号。
- 导出选中线索 P0 默认只导出脱敏联系方式。
- 管理员拥有全部权限。
- 运营可新增、编辑、查看线索、查看分配结果、查看脱敏联系方式。
- 只读角色只能查看，不可新增、编辑、导出、查看明文。
- 销售本阶段不登录后台。

## 8. 测试用例

### 8.1 线索新增

- 客户名称为空，返回 `LEAD_CUSTOMER_NAME_REQUIRED`。
- 手机为空，返回 `LEAD_PHONE_REQUIRED`。
- 手机号格式错误，返回 `LEAD_PHONE_INVALID`。
- 同一请求内手机号重复，返回 `LEAD_CONTACT_DUPLICATED_IN_REQUEST`。
- 新增有效线索成功，写入 leads、contacts、notes、operation_logs。

### 8.2 去重

- 手机号命中已有 assigned 线索，不创建新线索。
- 手机号命中已有 unassigned 线索，不创建新线索。
- 手机号命中 invalid 线索，不强拦截，但返回潜在重复提示。
- 重复录入时写入 duplicate event。
- 重复录入且有备注时追加 duplicate note。
- 原线索 duplicate_count 增加，last_duplicate_at 更新。

### 8.3 轮询分配

- 一个启用销售时，所有新线索分配给该销售。
- 三个销售 A/B/C 时，新线索按 A/B/C/A 分配。
- 停用销售不参与新分配。
- 不参与轮询销售不参与新分配。
- 无可用销售时，线索为 unassigned，写失败记录。
- 并发新增多条线索，轮询指针不丢失。

### 8.4 无效与恢复

- 标记无效必须传 invalid_reason。
- 标记无效后 status=invalid，不清空 sales_id。
- 有 sales_id 的无效线索恢复为 assigned。
- 无 sales_id 的无效线索恢复为 unassigned。
- 标记无效与恢复都写操作日志。

### 8.5 操作日志与审计

- 新增、编辑、分配、分配失败、重复、无效、恢复均写日志。
- 手机号明文查看写 phone_revealed 日志。
- 操作日志独立页可按事件、操作人、时间筛选。
- 日志中不保存完整明文手机号。

### 8.6 导出

- 未选择线索导出返回 `EXPORT_EMPTY_SELECTION`。
- 导出选中线索成功生成文件。
- 导出默认手机号脱敏。
- 导出写 `export_tasks` 和 `leads_exported` 操作日志。

## 9. 前端联调顺序

建议联调顺序：

1. `GET /api/sales`、`POST /api/sales`、`PUT /api/sales/{id}`。
2. `POST /api/leads` 新增线索，覆盖成功、重复、无销售失败。
3. `GET /api/leads`、`GET /api/leads/stats`。
4. `GET /api/leads/{id}` 详情抽屉。
5. `POST /api/leads/{id}/mark-invalid`、`POST /api/leads/{id}/restore`。
6. `POST /api/leads/retry-auto-assign`。
7. `GET /api/operation-logs`、`GET /api/leads/{id}/operation-logs`。
8. `POST /api/leads/{lead_id}/contacts/{contact_id}/reveal`。
9. `POST /api/leads/export`。

## 10. 待确认

| 问题 | 建议 |
|---|---|
| 后端工程是否已存在 | 当前项目目录未发现服务端工程代码，需要确认是否在其他仓库。 |
| 导出文件格式 | P0 建议先 CSV；如前端/客户要求 Excel，再实现 XLSX。 |
| 明文手机号加密方案 | 建议使用应用层加密或数据库加密，密钥不入库。 |
| 当前登录用户来源 | 需要确认是否已有后台账号体系；若无，P0 可用临时操作人上下文。 |
| 导出数量上限 | 建议 P0 限制单次最多 1000 条，避免同步导出拖慢接口。 |
