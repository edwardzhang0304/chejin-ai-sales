# 车金运营后台后端

当前范围：人工新增客户线索、手机号去重、销售轮询分配、线索列表详情、无效/恢复、操作日志、手机号明文审计、选中线索导出、销售管理、Worker 管理。

## Docker 启动，推荐

```bash
cd backend
cp .env.example .env
docker compose up --build
```

首次启动或模型变更后执行数据库迁移：

```bash
cd backend
docker compose exec api alembic upgrade head
```

健康检查：

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
```

说明：

- `/healthz`：服务存活检查。
- `/readyz`：数据库就绪检查，Docker healthcheck 使用此接口。

初始化演示销售数据：

```bash
cd backend
docker compose exec api python scripts/seed_dev.py
```

前端联调地址：

```text
http://localhost:8000/api/leads
http://localhost:8000/api/sales
http://localhost:8000/api/operation-logs
```

## 本地 Python 启动

```bash
cd backend
python3 -m pip install -e '.[test]'
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

本地默认使用 SQLite：`sqlite:///./chejin_leads.db`。联调/部署建议使用 Docker Compose 内的 PostgreSQL。

## 数据库迁移

P0 后端已接入 Alembic 迁移体系。默认使用 `AUTO_CREATE_TABLES=false`，通过 Alembic 管理表结构；生产环境必须关闭自动建表并执行迁移。

如果是早期开发环境已经通过 `AUTO_CREATE_TABLES=true` 自动建过 0001 表结构、但没有 `alembic_version`，需要先执行：

```bash
cd backend
docker compose exec api alembic stamp 20260603_0001
docker compose exec api alembic upgrade head
```

执行迁移：

```bash
cd backend
alembic upgrade head
```

查看当前迁移版本：

```bash
cd backend
alembic current
```

### 数据迁移回滚安全

`20260804_0019` 引入或接管 OmniAuto Product Master、KnowledgeRuntime、RAG、车辆和图片元数据；
`20260805_0020` 引入后台账号、会话与登录限速数据。这两份迁移包含正式业务数据，禁止执行自动
`downgrade`。当前 `20260806_0021` 同样设置了链路保护，防止 Alembic 在到达前述保护点之前先删除
车辆回复事实表。

需要回退应用版本时，数据库默认保持向前兼容的最新 schema。确需变更数据库结构时必须：

1. 完成 PostgreSQL 和车辆图片存储备份，并实际验证可以恢复。
2. 盘点车辆、知识库、账号、会话和待发送回复数据的保留及转换方案。
3. 编写独立的前向迁移，经过代码审查和预发布恢复演练后再执行。

禁止通过修改 Alembic 版本号、手工删除 schema，或临时恢复旧 `downgrade` 代码绕过保护。

生成新迁移草稿：

```bash
cd backend
alembic revision --autogenerate -m "change description"
```

生产环境要求：

- `ENVIRONMENT=production`
- `AUTO_CREATE_TABLES=false`
- `PHONE_HASH_SECRET` 和 `CONTACT_ENCRYPTION_SECRET` 必须配置为安全随机值，禁止使用示例或开发默认值。

## 后台账号与会话

后台使用服务端预建账号和可撤销 HttpOnly Cookie 会话，不接受浏览器 Bearer Token。先执行迁移，再通过服务端命令创建账号：

```bash
python scripts/admin_accounts.py create --username ops --display-name "运营人员"
python scripts/admin_accounts.py reset-password --username ops
python scripts/admin_accounts.py disable --username ops
python scripts/admin_accounts.py enable --username ops
```

命令默认通过终端安全读取密码；自动化场景可使用 `--password-stdin`。不要把密码放入命令参数、Git、镜像或 `.env.example`。后台 Cookie 与 Worker Token 双向隔离，所有登录账号拥有相同后台权限。

## 已实现接口

```text
GET  /api/leads
GET  /api/leads/stats
POST /api/leads/duplicate-preview
POST /api/leads
GET  /api/leads/{id}
PUT  /api/leads/{id}
POST /api/leads/{id}/mark-invalid
POST /api/leads/{id}/restore
POST /api/leads/batch-mark-invalid
POST /api/leads/retry-auto-assign
GET  /api/leads/{id}/assignments
GET  /api/leads/{id}/duplicate-events
GET  /api/leads/{id}/notes
POST /api/leads/{id}/contacts/{contact_id}/reveal
POST /api/leads/export

GET  /api/sales
POST /api/sales
GET  /api/sales/{id}
PUT  /api/sales/{id}
POST /api/sales/{id}/worker-binding
DELETE /api/sales/{id}/worker-binding

GET  /api/workers
POST /api/workers
GET  /api/workers/{id}
PUT  /api/workers/{id}
POST /api/workers/{id}/enable
POST /api/workers/{id}/disable
POST /api/workers/{id}/heartbeat
POST /api/workers/{id}/reset-binding

GET  /api/operation-logs
GET  /api/leads/{id}/operation-logs

GET  /api/assignment/round-robin-state
```

## 验证

```bash
python3 -m compileall app
pytest
alembic upgrade head
```
