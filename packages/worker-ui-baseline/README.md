# 车金 Worker UI 基准组件包

结论：`packages/worker-ui-baseline/` 是 Worker 客户端 UI 的唯一源头。

## 边界

- `frontend/` 只做运营后台，不维护 Worker 客户端 UI 预览或 UI 副本。
- `worker-client/` 只负责 Windows 桌面客户端业务逻辑和运行时桥接。
- `worker-client/chejin_worker_client/web_assets/` 是构建产物目录，只能由本包生成，不能手工改。
- 本包只负责 Worker 客户端展示层：结构、样式、组件、状态展示和 mock 数据。
- 本包不改后端接口、不改任务状态机、不改 RPA 执行逻辑。

## 结构

```text
packages/worker-ui-baseline/
  preview.html
  preview.tsx
  package.json
  scripts/
    build-preview.mjs
    build-worker-assets.mjs
  static/
    index.html
  src/
    WorkerClientBaseline.tsx
    WorkerClientRuntimeApp.tsx
    index.ts
    mockData.ts
    types.ts
    worker-ui.css
    worker-ui.tokens.css
```

## 常用命令

构建 preview：

```bash
npm run build:preview --prefix packages/worker-ui-baseline
```

生成 Worker 客户端运行产物：

```bash
npm run build:worker-assets --prefix packages/worker-ui-baseline
```

生成后会覆盖：

```text
worker-client/chejin_worker_client/web_assets/
  index.html
  worker-web-app.js
  worker-ui.css
  worker-ui.tokens.css
```

## 覆盖页面

- `bind`：首次绑定页
- `paused-empty`：暂停接单 + 无任务
- `accepting-wait`：接单中 + 等待任务
- `schedule-paused`：非接单时段 + 无任务
- `running`：执行任务中
- `completed`：任务执行完成
- `paused-running`：暂停接单 + 有任务执行中
- `paused-empty-2`：上一条任务结束后的暂停接单 + 无任务
- `offline`：服务端不可达 / 离线
- `failed`：任务执行失败
- `settings`：设置页
- `schedule-settings`：接单时段设置页
- `logs`：本机执行日志明细页

## 修改规则

1. UI 结构、class、token、CSS、状态展示，只改本包。
2. 修改后运行 `npm run build:worker-assets --prefix packages/worker-ui-baseline`。
3. 再运行 `worker-client/run_checks.py`。
4. 打包前确认 `worker-client/web-ui-src/` 不存在，`frontend/src/features/workerClient/` 不存在。
