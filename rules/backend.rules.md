# 后端 Rules

版本：v0.4

日期：2026-08-05

适用对象：后端状态判断、接口、Docker、健康检查、pytest、镜像、接口契约。

## 1. 后端必查文件

每次涉及后端状态，必须检查：

1. `deliverables/销售管理与Worker管理后端接口开发说明_2026-06-05.md`
2. `backend/README.md`
3. `backend/app/main.py`
4. `backend/app/api/routes/sales.py`
5. `backend/app/api/routes/workers.py`
6. `backend/app/models/sales.py`
7. `backend/app/models/worker.py`
8. `backend/app/services/sales_service.py`
9. `backend/app/services/worker_service.py`
10. `backend/tests/test_worker_sales_api.py`
11. `deliverables/任务中心后端接口开发说明_2026-06-08.md`
12. `backend/app/api/routes/tasks.py`
13. `backend/app/services/task_service.py`
14. `backend/tests/test_tasks_api.py`
15. `backend/docker-compose.yml`
16. `backend/Dockerfile`
17. `backend/pyproject.toml` 或 `backend/requirements.txt`
18. `deliverables/后台鉴权后端变更说明_2026-06-22.md`
19. `deliverables/C2会话绑定与微信监听后端开发说明_2026-06-22.md`

除非用户明确要求追溯历史、清理旧版或比对旧版，后端不得打开、引用或依据旧版/废弃文档判断当前接口范围、开发状态、自测结论或验收标准。

## 2. 当前 P1a 后端核查标准

| 检查项 | 标准 |
|---|---|
| 销售管理 | 销售列表、新增、详情、编辑、启用/停用、Worker 绑定/更换/清空 |
| Worker 管理 | Worker 列表、新增、详情、编辑、启用/停用、心跳、重置绑定 |
| 绑定约束 | Worker 只能绑定一个销售；已停用 Worker 不可绑定；离线但启用 Worker 可绑定 |
| Token | Worker Token 不在列表返回；新增/详情/重置后展示；旧 Token 重置后失效 |
| 审计 | 销售变更、Worker 变更、绑定/解绑、重置绑定均写日志 |
| 错误响应 | 所有错误响应包含 `trace_id`，响应头透传 `X-Request-Id` |
| 鉴权边界 | 运营后台使用服务端预建账号 + 密码 + 可撤销 HttpOnly Cookie 会话，登录账号统一拥有全部后台权限；Worker 客户端使用 Worker Token + Client Instance；两类凭证双向不得互认 |
| 测试 | 销售/Worker 和任务中心测试均需以最新 pytest/报告为准 |
| 任务中心 | 已实现 add_friend 任务基础链路，状态、事件、备注、取消、重试、领取、步骤、完成/失败需按 PRD v0.3 校验 |
| Windows Worker | 服务端已补客户端绑定、Worker Token 鉴权、接单状态、任务拉取/领取、RPA 步骤上报、结果/错误码回传、证据上传和心跳闭环；后续重点是客户端/RPA 联调和缺陷修复 |

## 3. 当前后端口径

1. 销售管理 + Worker 管理后端接口已完成开发和自测。
2. 自测证据来自 `销售管理与Worker管理后端接口开发说明_2026-06-05.md`。
3. 任务中心后端已实现 P1a add_friend 基础链路，复测通过；后续按缺陷或验收反馈修改。
4. 当前后端已有C3 Brain/任务中心候选实现；本轮只同步图片机器合同revision/SHA
   并执行C2/C3完整回归，不新增AI架构、图片接口或状态机。飞书、召回扩展、
   抖音API、批量导入和Mac Worker不进入本轮。
5. Windows Worker 客户端服务端能力已完成开发和自测，能力已并入技术方案 v0.8.1；不得再把阶段开发说明作为单独当前依据。
6. Windows Worker + 内置 OmniAuto RPA 是 P1a 剩余核心链路，不能再按 Mac/人工传值原型设计接口。
7. 技术方案 v0.8.1 已固定正式后台登录：服务端预建账号、Argon2id 密码哈希、可撤销
   HttpOnly Cookie 会话、登录即全部权限、不做 RBAC；所有后台业务接口必须校验会话。
8. 当前静态 Admin Bearer Token、`OPERATOR_API_CREDENTIALS`、`X-Operator-*` 身份兜底、
   viewer/只读门禁和生产 `AUTH_ENFORCEMENT=false` 绕过均是待删除旧实现，不能作为
   登录功能完成依据。
9. 后台 Cookie 与 Worker Token 使用独立验证器；后台会话不能调用 Worker 身份接口，
   Worker Token 不能访问后台管理接口。

## 4. 后端提交说明

后端提交开发完成/自测说明必须包含：

1. 开发范围。
2. 接口清单。
3. 数据库迁移。
4. Docker 启动状态。
5. `/healthz` 和 `/readyz` 结果。
6. pytest 是否执行及结果。
7. 错误码和审计说明。
8. 已知问题和不包含范围。
