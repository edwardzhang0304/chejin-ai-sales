# 车金运营后台后端

P0 范围：人工新增客户线索、手机号去重、销售轮询分配、线索列表详情、无效/恢复、操作日志、手机号明文审计、选中线索导出。

## Docker 启动，推荐

```bash
cd backend
cp .env.example .env
docker compose up --build
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

P0 后端已接入 Alembic 迁移体系。开发环境可临时使用 `AUTO_CREATE_TABLES=true` 自动建表；生产环境必须关闭自动建表并执行迁移。

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

生成新迁移草稿：

```bash
cd backend
alembic revision --autogenerate -m "change description"
```

生产环境要求：

- `ENVIRONMENT=production`
- `AUTO_CREATE_TABLES=false`
- `PHONE_HASH_SECRET` 和 `CONTACT_ENCRYPTION_SECRET` 必须配置为安全随机值，禁止使用示例或开发默认值。

## 操作人 Header

P0 临时通过 Header 模拟登录态：

```text
X-Operator-Id: 00000000-0000-0000-0000-000000000001
X-Operator-Name: 运营小陈
X-Operator-Role: admin
```

后续接入真实鉴权后，由认证中间件填充操作人上下文。

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
PUT  /api/sales/{id}

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
