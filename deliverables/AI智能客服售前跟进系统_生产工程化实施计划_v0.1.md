# AI 智能客服售前跟进系统 生产工程化实施计划 v0.1

> 日期：2026-06-02  
> 当前阶段：P0 联调可测版 -> 生产工程化版  
> 原则：测试继续验证 P0 功能闭环，研发并行补齐上线必需的工程能力。  

## 1. 当前结论

当前版本已经可以给测试工程师继续做 P0 功能测试，但距离生产工程标准仍需补齐鉴权、迁移、工程化前端、测试体系、安全审计、CI/CD、监控与运维。

实施策略：

- 不打断当前测试。
- 后端先做低风险生产硬化。
- 前端下一步迁移到正式工程结构。
- 每一项生产改造都保留可回归测试入口。

## 2. 已开始实施的生产硬化

本轮已实施：

- 后端新增 `ENVIRONMENT` 配置。
- 后端新增 `AUTO_CREATE_TABLES`，生产可关闭自动建表。
- 后端新增 `DOCS_ENABLED`，生产可关闭 `/docs` 和 `/openapi.json`。
- 后端 CORS 从固定 `*` 改为 `CORS_ORIGINS` 配置。
- 后端新增 `/readyz`，会真实检查数据库连接。
- Docker Compose healthcheck 从 `/healthz` 改为 `/readyz`。
- `.env.example` 补齐生产相关配置说明。

## 3. 生产上线前必须完成

### P0 必须项

| 编号 | 工作项 | 说明 | 责任 |
| --- | --- | --- | --- |
| P0-01 | 真实鉴权 | 替换 Header 模拟操作人，接入登录态 | 后端主责，前端配合 |
| P0-02 | 权限控制 | 运营、管理员、只读角色权限；手机号 reveal 和导出需强权限 | 后端主责，前端配合 |
| P0-03 | 数据库迁移 | 引入 Alembic 或等价迁移机制，生产关闭自动建表 | 后端 |
| P0-04 | 密钥治理 | `PHONE_HASH_SECRET`、`CONTACT_ENCRYPTION_SECRET` 必须来自安全配置 | 后端/运维 |
| P0-05 | CORS 白名单 | 生产只允许正式前端域名 | 后端/运维 |
| P0-06 | 前端工程化 | 静态 HTML 改为正式 Vite/React 或 Vue 工程 | 前端 |
| P0-07 | E2E 冒烟 | 覆盖新增、重复、分配、无效/恢复、导出、reveal | 前端/测试 |
| P0-08 | 审计可追溯 | reveal、export、无效、恢复、销售配置变更必须可查 | 后端/测试 |
| P0-09 | 生产部署脚本 | 镜像构建、环境变量、部署、回滚流程 | 后端/运维 |
| P0-10 | 数据备份 | PostgreSQL 备份与恢复演练 | 运维 |

### P1 强烈建议

| 编号 | 工作项 | 说明 | 责任 |
| --- | --- | --- | --- |
| P1-01 | 前端表单增强 | 动态联系方式增删、自定义字段增删、完整校验 | 前端 |
| P1-02 | 错误状态标准化 | loading、empty、error、retry 全面组件化 | 前端 |
| P1-03 | 后端接口测试 | pytest 覆盖主要接口和边界 | 后端 |
| P1-04 | 并发分配测试 | 验证轮询指针并发安全 | 后端 |
| P1-05 | 操作日志筛选页 | 前端补操作日志页面 | 前端 |
| P1-06 | 导出任务化 | 大数据量导出异步化，避免请求阻塞 | 后端 |
| P1-07 | 限流 | reveal、export、创建接口限流 | 后端/网关 |
| P1-08 | 结构化日志 | 请求 ID、耗时、状态码、用户、目标资源 | 后端/运维 |
| P1-09 | 指标监控 | 请求错误率、接口耗时、DB 连接、导出次数 | 后端/运维 |
| P1-10 | CI | lint、类型检查、单测、构建、镜像扫描 | 全栈 |

## 4. 后端配合需求

### 4.1 鉴权与权限

需要后端确认并实现：

- 登录方式：【待确认】企业微信、飞书、账号密码、SSO。
- 登录态传递方式：【待确认】Cookie Session 或 JWT。
- 权限角色：
  - `admin`：全部操作。
  - `operator`：线索录入、查看、标记无效、恢复、重新分配。
  - `viewer`：只读，不允许 reveal、export、写操作。
- 接口权限矩阵：
  - `POST /api/leads/{id}/contacts/{contact_id}/reveal` 仅 `admin/operator`。
  - `POST /api/leads/export` 仅 `admin/operator`。
  - `POST/PUT /api/sales` 建议仅 `admin`。

前端需要后端返回当前用户信息：

```text
GET /api/me
```

建议响应：

```json
{
  "id": "op_001",
  "name": "运营小陈",
  "roles": ["admin"],
  "permissions": ["lead:create", "lead:reveal_phone", "lead:export"]
}
```

### 4.2 数据库迁移

需要后端实现：

- Alembic 初始化。
- 当前模型生成首个 migration。
- Docker entrypoint 支持启动时执行 migration。
- 生产设置 `AUTO_CREATE_TABLES=false`。

建议命令：

```bash
alembic upgrade head
```

### 4.3 安全与审计

需要后端确认：

- reveal 是否需要二次确认或审批。
- reveal reason 是否最小长度限制。
- reveal 是否需要频控，例如同一操作人每分钟最多 N 次。
- 导出最大条数是否仍为 1000。
- 导出字段白名单是否固定。
- 操作日志保留周期。
- 是否需要导出任务下载链接过期机制。

### 4.4 API 稳定性

需要后端补齐：

- 统一错误码文档。
- 接口超时策略。
- 请求 ID 透传。
- 分页最大值保护。
- 大量关键词搜索时的索引策略。
- 轮询分配事务锁或等价互斥机制的测试证明。

### 4.5 部署与运维

需要后端/运维给出：

- 生产 Docker 镜像命名规范。
- 环境变量来源，例如 K8s Secret、平台变量、Vault。
- PostgreSQL 生产实例地址和备份策略。
- 日志采集方案。
- 监控告警方案。
- 回滚策略。

## 5. 前端继续实施计划

### 阶段一：保持当前可测页面，增强稳定性

- 保留静态 HTML 页面继续供测试使用。
- 修复测试反馈问题。
- 补齐真实接口错误提示和按钮禁用态。
- 增强新增客户弹窗的动态联系方式/自定义字段。

### 阶段二：前端正式工程化

建议技术栈：

```text
Vite + React + TypeScript
```

模块拆分：

```text
src/
  api/
    client.ts
    leads.ts
    sales.ts
    operationLogs.ts
  components/
    StatusBadge.tsx
    Modal.tsx
    Toast.tsx
    Pagination.tsx
  features/
    leads/
      LeadList.tsx
      LeadDetailDrawer.tsx
      LeadFormModal.tsx
      InvalidLeadModal.tsx
    sales/
      SalesManagement.tsx
  pages/
    OpsAdminPage.tsx
```

阶段目标：

- 类型安全。
- 请求层统一。
- 状态清晰。
- 表单校验可维护。
- E2E 测试可跑。
- 生产构建产物可部署。

### 阶段三：前端生产体验

- 接入真实登录态。
- 根据权限隐藏/禁用按钮。
- 操作日志页面。
- 全局错误边界。
- 导出下载状态。
- 大列表分页与筛选稳定性。

## 6. 测试继续执行说明

测试工程师继续按以下文档执行：

- [AI智能客服售前跟进系统_测试交接说明_v0.1.md](/Users/zhangwentao/Documents/车金/deliverables/AI智能客服售前跟进系统_测试交接说明_v0.1.md)

测试发现的问题按优先级分流：

- P0/P1：立即修复当前联调版。
- P2：进入生产工程化任务池。
- P3：统一归到体验优化。

## 7. 当前不建议立刻做的事

- 不建议现在就删除静态联调页，否则测试会失去稳定入口。
- 不建议在没有真实鉴权方案前过度设计前端权限。
- 不建议把批量导入纳入当前 P0，否则会扩大范围。
- 不建议生产继续使用 `AUTO_CREATE_TABLES=true`。

