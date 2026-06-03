# 后端 Rules

版本：v0.1

日期：2026-06-03

适用对象：后端状态判断、接口、Docker、健康检查、pytest、镜像、接口契约。

## 1. 后端必查文件

每次涉及后端状态，必须检查：

1. `deliverables/AI智能客服售前跟进系统_后端开发清单_v0.1_线索导入与分配.md`
2. `deliverables/AI智能客服售前跟进系统_运营后台接口契约_v0.1.md`
3. `backend/README.md`
4. `backend/app/main.py`
5. `backend/docker-compose.yml`
6. `backend/Dockerfile`
7. `backend/pyproject.toml` 或 `backend/requirements.txt`
8. `backend/tests/`
9. `deliverables/test_runs/AI智能客服售前跟进系统_P0测试执行报告_2026-06-02.md`
10. `deliverables/test_runs/BUG-001_重复手机号409修复回归记录_2026-06-02.md`
11. `deliverables/test_runs/P0回归测试报告_BUG-001修复后_2026-06-02.md`
12. `deliverables/test_runs/P0运营后台完整回归测试报告_2026-06-03.md`

## 2. 后端核查标准

| 检查项 | 标准 |
|---|---|
| P0 接口覆盖 | 人工新增、去重、分配、列表、详情、无效/恢复、批量无效、重新分配、手机号 reveal、导出、销售管理、操作日志 |
| 健康检查 | `/healthz` 为存活检查，`/readyz` 为数据库就绪检查；Docker 正式健康检查应使用 `/readyz` |
| BUG-001 | 重复手机号必须返回 `409 / LEAD_PHONE_DUPLICATED`，不得返回 500 |
| CORS | `CORS_ORIGINS` 必须支持逗号字符串和 JSON 数组 |
| PYTHONPATH | Docker 镜像必须包含 `PYTHONPATH=/app` |
| 测试依赖 | 本地或容器 pytest 如未安装，必须写明“未执行”，不能说后端测试全绿 |
| 容器验证 | 正式交付前必须确认 `docker compose up --build` 后 `/readyz`、P0 API/UAT 通过 |

## 3. 当前后端口径

1. BUG-001 重复手机号 409 已回归通过。
2. P0 API 自动化已有 17/17 通过证据。
3. Docker `api/db` healthy 和 `/readyz` 通过已有证据。
4. 后端容器内 pytest 未执行，因为容器内未安装 `pytest`；这只能作为测试环境问题记录，不能写成后端单测通过。
5. 正式镜像仓库、生产迁移、正式鉴权仍【待确认】。

## 4. 后端提交说明

后端提交开发完成/自测说明必须包含：

1. P0 接口清单。
2. Docker 启动状态。
3. `/healthz` 和 `/readyz` 结果。
4. BUG 修复说明。
5. CORS、PYTHONPATH、环境变量说明。
6. pytest 是否执行；未执行必须说明原因。
7. 已知问题和不包含范围。
