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

构建后可直接打开 `preview.html`，不依赖本地 HTTP 服务。

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

## 页面与验收状态

产品页面统一为首次绑定、统一工作台、设置、接单时段设置和本机执行日志。下面的 id 是设计与验收状态，不代表独立产品页面。

- `bind`：首次绑定页
- `paused-empty`：已暂停接单
- `accepting-wait`：接单中，等待下一轮检查
- `schedule-paused`：当前不在接单时段
- `running`：加好友执行中
- `completed`：加好友完成
- `paused-running`：暂停接单 + 有任务执行中
- `paused-empty-2`：暂停接单 + 上一条结果保留
- `offline`：服务端离线 + 当前操作
- `offline-empty`：服务端离线 + 无当前操作
- `automation-unavailable`：自动化组件不可用
- `wechat-disconnected`：微信未连接
- `failed`：加好友失败
- `settings`：设置页
- `schedule-settings`：接单时段设置页
- `logs`：本机执行日志明细页
- `scan-running` / `scan-completed`：未归属到具体客户前的第一屏扫描过程
- `target-read-running` / `target-read-completed`：同一客户处理事务的统一动态链路
- `ai-reply-running` / `ai-reply-completed` / `ai-reply-failed`：保留的运行时兼容状态，界面仍渲染同一条客户处理链路，不另起画面

统一工作台中间区域固定使用“当前运行过程”。第一屏无命中时只展示扫描结果；一旦发现已授权客户，便从“发现待处理客户”开始，按实际发生的节点累积展示定位、读取、语音、图片、回传、服务端判断和 AI 发送，直到本次客户事务终态。未发生的媒体节点不展示。加好友任务仍保持独立链路。

## 修改规则

1. UI 结构、class、token、CSS、状态展示，只改本包。
2. 修改后运行 `npm run build:worker-assets --prefix packages/worker-ui-baseline`。
3. 再运行 `worker-client/run_checks.py`。
4. 打包前确认 `worker-client/web-ui-src/` 不存在，`frontend/src/features/workerClient/` 不存在。
