# AI 智能客服售前跟进系统 运营后台接口契约 v0.1

> 版本日期：2026-06-02  
> 适用范围：P0 运营后台，人工新增客户线索、去重、自动分配、销售轮询、手机号明文查看、选中线索导出、操作审计。  
> 文档性质：技术栈无关接口契约草案。最终路径、字段、错误码、鉴权方式由后端确认。  
> 当前仓库状态：`backend/` 已存在一版 FastAPI 风格实现，本文按业务契约描述，不强绑定具体后端技术栈。

## 1. 结论

当前 P0 不做正式 Excel/CSV 批量导入，核心入口应命名为**人工新增客户线索**。前端页面如出现“线索导入”，仅表示人工录入进入线索池，不代表文件上传能力。

接口文档最终由后端主责维护，但前端需要先确认页面依赖的接口、字段、状态、权限和审计规则。本契约用于前后端联调前对齐。

P0 必须包含两个敏感操作接口：

- `POST /api/leads/{id}/contacts/{contact_id}/reveal`：手机号明文查看，必须鉴权并写审计。
- `POST /api/leads/export`：导出选中线索，必须限制条数、字段白名单、脱敏导出并写审计。

重复手机号可提供前端预查能力，但保存接口 `POST /api/leads` 必须在后端事务内再次查重，不能只依赖前端预查。

销售轮询配置不单独做全局配置接口。P0 中销售启用、参与轮询、排序统一通过 `POST /api/sales` 和 `PUT /api/sales/{id}` 维护。后续如增加门店、权重、容量等策略，再扩展独立配置接口。

## 2. 通用约定

### 2.1 Base URL

```text
/api
```

### 2.2 统一 JSON 响应

成功：

```json
{
  "code": "OK",
  "message": "success",
  "data": {}
}
```

失败：

```json
{
  "code": "VALIDATION_ERROR",
  "message": "参数错误",
  "data": {}
}
```

### 2.3 分页

请求参数：

```text
page: number，默认 1，从 1 开始
page_size: number，默认 20，最大 100
```

返回结构：

```json
{
  "items": [],
  "page": 1,
  "page_size": 20,
  "total": 128
}
```

### 2.4 鉴权与操作人

【待确认】真实鉴权方式：Cookie Session、JWT、网关 Header 或企业微信/飞书登录态。

后端所有写操作和敏感读操作需要能拿到当前操作人：

```json
{
  "operator_id": "op_001",
  "operator_name": "运营小陈",
  "roles": ["ops_admin"]
}
```

敏感操作至少包括：

- 新增、编辑线索
- 标记无效、恢复有效
- 重新分配线索
- 查看手机号明文
- 导出线索
- 新增/编辑销售

### 2.5 时间格式

所有时间字段建议使用 ISO 8601：

```text
2026-06-02T14:30:00+08:00
```

前端展示时再格式化为 `06-02 14:30` 或 `2026-06-02 14:30`。

## 3. 状态枚举

### 3.1 线索状态 lead.status

```text
unassigned  未分配
assigned    已分配
invalid     无效
```

P0 不保留 `archived` 归档状态，前端不得展示“归档线索默认隐藏”。

### 3.2 分配状态 lead.assign_status

```text
unassigned     未分配
assigned       已分配
assign_failed  分配失败
```

### 3.3 联系方式类型 contact.contact_type

```text
phone   手机
wechat  微信
email   邮箱
```

### 3.4 无效原因 invalid_reason

```text
empty_number             空号
wrong_info               信息错误
not_target_customer      非目标客户
test_data                测试数据
duplicate_or_mistaken    重复/误录
other                    其他
```

### 3.5 分配记录

assignment_type：

```text
round_robin        首次轮询
retry_round_robin  人工重新分配
```

assignment_result：

```text
succeeded  成功
failed     失败
```

## 4. 线索接口

### 4.1 获取线索列表

```text
GET /api/leads
```

查询参数：

```text
keyword: string，可选，客户名称、手机号后四位、微信、备注
status: unassigned | assigned | invalid，可选
sales_id: string，可选
created_by: string，可选
has_duplicate: boolean，可选
page: number
page_size: number
```

响应：

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "items": [
      {
        "id": "lead_001",
        "customer_name": "王先生",
        "status": "assigned",
        "source_type": "manual",
        "source_name_snapshot": "人工录入",
        "primary_phone_masked": "138****6678",
        "primary_wechat_masked": "wx_car_2026",
        "sales_id": "sales_001",
        "sales_name": "张伟",
        "assign_status": "assigned",
        "assign_failure_reason": null,
        "remark_summary": "预算 10 万，想看 SUV",
        "duplicate_count": 3,
        "last_duplicate_at": "2026-06-02T14:30:00+08:00",
        "created_by_name": "运营小陈",
        "created_at": "2026-06-02T14:30:00+08:00",
        "updated_at": "2026-06-02T14:30:00+08:00"
      }
    ],
    "page": 1,
    "page_size": 20,
    "total": 128
  }
}
```

前端状态处理：

- loading：列表骨架屏或表格 loading。
- empty：无搜索结果时展示空状态，提供清空筛选。
- error：展示错误提示和重试按钮。
- retry：保留当前筛选和分页重试。

### 4.2 获取线索指标

```text
GET /api/leads/stats
```

响应：

```json
{
  "today_new_count": 28,
  "assigned_count": 24,
  "unassigned_count": 4,
  "duplicate_event_count": 7
}
```

【待确认】指标是否受当前筛选条件影响。建议 P0 先返回全局运营指标。

### 4.3 人工新增客户线索

```text
POST /api/leads
```

请求：

```json
{
  "customer_name": "王先生",
  "phones": ["13896676678"],
  "wechats": ["wx_car_2026"],
  "emails": [],
  "remark": "预算 10 万，想看 SUV，周末到店。",
  "custom_fields": {
    "budget": "10 万以内",
    "car_type": "SUV"
  }
}
```

校验规则：

- `customer_name` 必填，最大 50 字。
- `phones` 必填，1 到 5 个。
- `wechats` 可选，最多 5 个。
- `emails` 可选，最多 5 个。
- `remark` 可选，最大 1000 字。
- 同一次请求内联系方式不得重复。

后端事务规则：

- 保存时必须在事务内按手机号 hash 再次查重。
- 如手机号已存在，不创建新线索。
- 后端应追加重复录入记录和备注记录。
- 返回 `409 LEAD_PHONE_DUPLICATED`，并携带原线索摘要、重复次数、重复日期。
- 前端预查重复只能作为用户提示，不能替代保存时查重。

成功响应：

```json
{
  "id": "lead_001",
  "status": "assigned",
  "assign_status": "assigned",
  "sales_id": "sales_001",
  "sales_name": "张伟",
  "potential_duplicates": {
    "wechat": [],
    "email": []
  }
}
```

重复响应：

```json
{
  "code": "LEAD_PHONE_DUPLICATED",
  "message": "该手机号已存在，不能重复新建。已重复录入 3 次，日期：2026-06-02。",
  "data": {
    "duplicate_lead": {
      "id": "lead_001",
      "customer_name": "王先生",
      "status": "assigned",
      "sales_name": "张伟",
      "primary_phone_masked": "138****6678"
    },
    "duplicate_count": 3,
    "duplicate_dates": ["2026-06-02"]
  }
}
```

### 4.4 获取线索详情

```text
GET /api/leads/{id}
```

响应建议：

```json
{
  "id": "lead_001",
  "customer_name": "王先生",
  "status": "assigned",
  "contacts": [
    {
      "id": "contact_001",
      "contact_type": "phone",
      "masked_value": "138****6678",
      "is_primary": true
    }
  ],
  "sales": {
    "id": "sales_001",
    "sales_name": "张伟"
  },
  "custom_fields": {
    "budget": "10 万以内"
  },
  "task_nodes": [
    {
      "key": "round_robin_assigned",
      "label": "轮询分配完成",
      "time": "2026-06-02T14:30:00+08:00"
    }
  ],
  "created_at": "2026-06-02T14:30:00+08:00",
  "updated_at": "2026-06-02T14:30:00+08:00"
}
```

### 4.5 编辑线索

```text
PUT /api/leads/{id}
```

请求字段同 `POST /api/leads`，但均可选。

规则：

- 更新手机号时同样必须做事务内查重。
- 不能把当前线索手机号改成其他线索已有手机号。
- 编辑后需要写操作日志。

### 4.6 标记为无效线索

```text
POST /api/leads/{id}/mark-invalid
```

请求：

```json
{
  "invalid_reason": "empty_number",
  "invalid_remark": "拨打为空号"
}
```

响应：

```json
{
  "id": "lead_001",
  "status": "invalid",
  "invalid_reason": "empty_number",
  "invalid_remark": "拨打为空号"
}
```

规则：

- 标记后列表仍可通过状态筛选查看。
- 不再进入自动分配池。
- 需要写操作日志。

### 4.7 恢复为有效线索

```text
POST /api/leads/{id}/restore
```

响应：

```json
{
  "id": "lead_001",
  "status": "assigned",
  "assign_status": "assigned",
  "sales_id": "sales_001"
}
```

规则：

- 如果线索仍有当前销售，恢复为 `assigned`。
- 如果没有当前销售，恢复为 `unassigned`，可再触发重新分配。
- 需要写操作日志。

### 4.8 重新分配线索

```text
POST /api/leads/retry-auto-assign
```

请求：

```json
{
  "lead_ids": ["lead_001", "lead_002"]
}
```

响应：

```json
{
  "items": [
    {
      "lead_id": "lead_001",
      "result": "succeeded",
      "sales_id": "sales_002",
      "sales_name": "王敏",
      "failure_reason": null
    }
  ]
}
```

规则：

- 只处理有效线索。
- 如果没有可参与轮询的销售，应返回失败原因。
- 需要记录分配记录和操作日志。

### 4.9 获取分配记录

```text
GET /api/leads/{id}/assignments
```

响应：

```json
{
  "items": [
    {
      "id": "assign_001",
      "assignment_type": "round_robin",
      "assignment_result": "succeeded",
      "sales_id": "sales_001",
      "sales_name": "张伟",
      "failure_reason": null,
      "round_robin_cursor_before": "sales_000",
      "round_robin_cursor_after": "sales_001",
      "created_at": "2026-06-02T14:30:00+08:00"
    }
  ]
}
```

### 4.10 获取重复记录

```text
GET /api/leads/{id}/duplicate-events
```

响应：

```json
{
  "items": [
    {
      "id": "dup_001",
      "submitted_phone_masked": "138****6678",
      "operator_name": "运营小陈",
      "remark_appended": true,
      "created_at": "2026-06-02T14:30:00+08:00"
    }
  ]
}
```

### 4.11 获取备注记录

```text
GET /api/leads/{id}/notes
```

响应：

```json
{
  "items": [
    {
      "id": "note_001",
      "note_type": "duplicate_append",
      "content": "预算 10 万，想看 SUV。",
      "operator_name": "运营小陈",
      "created_at": "2026-06-02T14:30:00+08:00"
    }
  ]
}
```

### 4.12 查看手机号明文

```text
POST /api/leads/{id}/contacts/{contact_id}/reveal
```

请求：

```json
{
  "reason": "电话确认到店时间"
}
```

响应：

```json
{
  "contact_id": "contact_001",
  "contact_type": "phone",
  "value": "13896676678",
  "revealed_at": "2026-06-02T14:30:00+08:00"
}
```

权限与审计：

- 仅允许有权限的运营角色查看。
- P0 只允许查看手机号明文，不允许查看微信/邮箱明文，除非后端另行授权。
- 请求必须填写 `reason`，最大 200 字。
- 后端必须写操作日志，事件建议为 `phone_revealed`。
- 日志 metadata 至少包含 `lead_id`、`contact_id`、`reason`，不得记录完整手机号。

错误：

```text
403 CONTACT_REVEAL_FORBIDDEN  无权查看联系方式明文
400 VALIDATION_ERROR           当前只允许查看手机号明文
404 LEAD_NOT_FOUND             联系方式不存在
```

### 4.13 导出选中线索

```text
POST /api/leads/export
```

请求：

```json
{
  "lead_ids": ["lead_001", "lead_002"],
  "fields": ["customer_name", "primary_phone_masked", "sales_name", "status", "remark_summary"]
}
```

响应：

```text
Content-Type: text/csv; charset=utf-8
Content-Disposition: attachment; filename="leads_export_20260602_143000.csv"
```

规则：

- `lead_ids` 必填，不能为空。
- 后端必须限制单次最大导出条数，建议默认 1000。
- `fields` 必须走后端白名单，不允许任意字段导出。
- P0 默认导出脱敏手机号，不导出完整手机号。
- 需要创建导出任务记录并写操作日志，事件建议为 `leads_exported`。

错误：

```text
400 EXPORT_EMPTY_SELECTION  请选择要导出的线索
400 EXPORT_TOO_MANY_ROWS    单次导出超过限制
400 VALIDATION_ERROR        不支持的导出字段
```

### 4.14 重复手机号预查，可选

```text
POST /api/leads/duplicate-preview
```

【待确认】P0 是否提供。该接口只作为前端输入手机号后的提前提示，不作为最终查重依据。

请求：

```json
{
  "phones": ["13896676678"]
}
```

响应：

```json
{
  "items": [
    {
      "phone_masked": "138****6678",
      "duplicated": true,
      "lead_id": "lead_001",
      "customer_name": "王先生",
      "sales_name": "张伟",
      "duplicate_count": 3
    }
  ]
}
```

## 5. 销售接口

### 5.1 获取销售列表

```text
GET /api/sales
```

响应：

```json
{
  "items": [
    {
      "id": "sales_001",
      "sales_name": "张伟",
      "phone": "138****1001",
      "wechat": "zhangwei_car",
      "feishu_user_id": "ou_xxx",
      "enabled": true,
      "participate_in_round_robin": true,
      "sort_order": 10,
      "remark": null,
      "lead_count": 46
    }
  ]
}
```

### 5.2 新增销售

```text
POST /api/sales
```

请求：

```json
{
  "sales_name": "张伟",
  "phone": "13800001001",
  "wechat": "zhangwei_car",
  "feishu_user_id": "ou_xxx",
  "enabled": true,
  "participate_in_round_robin": true,
  "sort_order": 10,
  "remark": "一店销售"
}
```

响应：

```json
{
  "id": "sales_001"
}
```

### 5.3 编辑销售与轮询配置

```text
PUT /api/sales/{id}
```

请求同新增销售。

规则：

- P0 中 `enabled`、`participate_in_round_robin`、`sort_order` 均通过此接口维护。
- 后端更新轮询相关字段时需要写操作日志，事件建议为 `sales_round_robin_changed`。
- 如果销售停用或不参与轮询，不影响其历史名下线索，只影响新线索分配。

## 6. 操作日志接口

### 6.1 获取操作日志

```text
GET /api/operation-logs
```

查询参数：

```text
keyword: string，可选
event_type: string，可选
module: string，可选
operator_id: string，可选
target_type: string，可选
target_id: string，可选
page: number
page_size: number
```

响应：

```json
{
  "items": [
    {
      "id": "log_001",
      "event_type": "phone_revealed",
      "event_label": "查看手机号明文",
      "module": "leads",
      "operator_id": "op_001",
      "operator_name": "运营小陈",
      "target_type": "lead",
      "target_id": "lead_001",
      "lead_id": "lead_001",
      "metadata": {
        "reason": "电话确认到店时间"
      },
      "created_at": "2026-06-02T14:30:00+08:00"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

### 6.2 获取单条线索操作日志

```text
GET /api/leads/{id}/operation-logs
```

返回结构同 `GET /api/operation-logs`。

## 7. 轮询状态接口

### 7.1 获取轮询状态

```text
GET /api/assignment/round-robin-state
```

响应建议：

```json
{
  "current_cursor_sales_id": "sales_001",
  "current_cursor_sales_name": "张伟",
  "enabled_sales_count": 3,
  "round_robin_sales": [
    {
      "id": "sales_001",
      "sales_name": "张伟",
      "sort_order": 10,
      "lead_count": 46
    }
  ]
}
```

【待确认】字段名以真实后端实现为准。

## 8. 错误码建议

```text
OK                              成功
VALIDATION_ERROR                参数错误
LEAD_NOT_FOUND                  线索不存在
SALES_NOT_FOUND                 销售不存在
LEAD_PHONE_REQUIRED             请输入手机号
LEAD_PHONE_INVALID              手机号格式不正确
LEAD_PHONE_DUPLICATED           手机号已存在
LEAD_CONTACT_DUPLICATED_IN_REQUEST  同一请求内联系方式重复
CONTACT_REVEAL_FORBIDDEN        无权查看联系方式明文
CONTACT_DECRYPT_FAILED          联系方式解密失败
EXPORT_EMPTY_SELECTION          请选择要导出的线索
EXPORT_TOO_MANY_ROWS            单次导出超过限制
```

## 9. 前端联调关注点

前端实现接口层时必须覆盖：

- loading：列表、详情、销售列表、日志列表、弹窗提交。
- empty：无列表数据、无备注、无重复记录、无分配记录。
- error：网络错误、权限错误、参数错误、导出失败。
- retry：列表、详情和日志重试保留当前筛选条件。
- form validation：客户名称、手机号、无效原因、明文查看 reason。
- optimistic update：【待确认】P0 建议不做乐观更新，等待后端返回后刷新列表和详情。
- pagination：`page/page_size` 与后端一致。
- status mapping：前端展示文案从枚举统一映射，不硬编码接口中文。
- audit：明文查看、导出、标记无效、恢复有效、重新分配必须能在操作日志查到。

## 10. 待后端确认清单

- 真实鉴权方式和操作人上下文来源。
- `GET /api/leads/stats` 是否受筛选条件影响。
- 是否提供 `POST /api/leads/duplicate-preview` 前端预查接口。
- `GET /api/leads/{id}` 详情字段是否一次性包含 contacts、task_nodes、notes、duplicate_events、assignments，还是由子接口分开请求。
- 导出字段白名单和最大导出条数。
- 手机号明文查看是否需要二次确认、原因必填、权限角色名。
- 错误码是否保持本文命名，或由后端统一调整。
- 轮询状态接口字段命名。

