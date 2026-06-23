# 车金 Worker 客户端

这是 P1a Windows 单应用客户端工程，最终交付物是 `车金Worker客户端.exe`。

当前状态：

- 已完成：客户端工程结构、绑定信息本地存储、Worker Shell/UI、组件化 Worker WebView 展示层、接口调用层、任务领取/串行执行 Runner、OmniAuto RPA Bridge、mock add_friend 主链路、PyInstaller 打包脚本、自检脚本和基础单元测试。
- 已接入：真实 add_friend 执行固定调用 OmniAuto `add-friend-entry-click-plan-windows`，由 OmniAuto 负责窗口探测、OCR、点击/输入、发送邀请、结构化步骤/失败结果和证据文件输出。
- 未完成：真实 Windows 微信 RPA 闭环仍需在 Windows + 已登录微信桌面客户端环境持续回归。微信不同版本的控件树、按钮文案和窗口结构可能需要继续调整。
- 验收限制：在 Windows + 已登录微信桌面客户端环境完成实机联调并产出 `.exe` 前，不能按“RPA 已完成”或“客户端可提真实闭环测试”对外结论。

工程边界：

- `chejin_worker_client/web_ui.py`：V14 默认 Worker 客户端展示层，使用 PySide QtWebEngine + QWebChannel 承载 UI 基准组件构建后的 `web_assets`
- `chejin_worker_client/web_assets/`：由 `packages/worker-ui-baseline` 构建出的客户端页面资产，覆盖绑定、等待、执行中、完成、失败、设置和日志页；该目录只作为构建产物，不手工维护
- `chejin_worker_client/ui.py`：V13 PySide 手写 Worker 工作台 UI，仅作为排查 fallback 保留
- `chejin_worker_client/task_runner.py`：任务领取、串行执行、恢复和上报
- `chejin_worker_client/rpa_bridge.py`：把服务端任务转为 OmniAuto 本地 RPA 命令
- `omniauto-rpa/apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py`：正式打包内置的 OmniAuto RPA sidecar

开发启动：

```powershell
cd worker-client
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m chejin_worker_client.main
```

默认启动 V14 组件化 Worker 客户端窗口。旧 Tk demo UI 已删除；如需临时回退 V13 PySide 手写 UI 排查，可设置：

```powershell
$env:CHEJIN_WORKER_UI_MODE="pyside"
.\.venv\Scripts\python -m chejin_worker_client.main
```

打包 exe：

```powershell
cd worker-client
.\scripts\build-windows.ps1
```

打包脚本会执行：

- 安装依赖
- 运行 `python run_checks.py`
- 运行 mock 预检并生成 `dist\reports\preflight-build-report.json`
- 解压 `deliverables\omniauto-add-friend-rpa-pr-candidate-20260618.zip` 作为内置 OmniAuto RPA
- 使用 PyInstaller 生成 Windows 单应用目录
- 校验 `车金Worker客户端.exe` 和内置 OmniAuto sidecar
- 生成 `dist\reports\车金Worker客户端.manifest.json`，包含版本、SHA256、文件数和包体大小

产物：

```text
worker-client\dist\车金Worker客户端\车金Worker客户端.exe
```

校验已有产物：

```powershell
.\scripts\validate-package.ps1
```

默认服务端地址为 `http://127.0.0.1:8000/api`，可通过环境变量覆盖：

```powershell
$env:CHEJIN_API_BASE_URL="https://your-host/api"
```

RPA 模式：

- `CHEJIN_RPA_MODE=real`：调用内置 OmniAuto sidecar，要求 Windows + 已登录微信桌面客户端。
- `CHEJIN_RPA_MODE=mock`：仅用于开发联调，模拟 add_friend 步骤和结果。

本机数据位置：

```text
%LOCALAPPDATA%\CheJinWorker\
```

本地自检：

```powershell
cd worker-client
python run_checks.py
```

自检内容包括单元测试、无 UI 的 Runner + mock RPA 端到端冒烟，以及 Python 编译检查。

Windows 环境预检：

```powershell
cd worker-client
.\scripts\run-preflight.ps1 -RpaMode real -ApiBaseUrl "http://127.0.0.1:8000/api" -ReportPath ".\preflight-report.json"
```

开发机不探测真实微信时可跑：

```powershell
.\scripts\run-preflight.ps1 -RpaMode mock -SkipWechat
```

预检会检查 Windows/RPA 模式、依赖、OmniAuto sidecar 文件、本地数据目录、绑定状态、后端 `/readyz` 和微信桌面客户端探测结果。存在 `error` 级失败时命令返回非 0。

微信实机诊断：

```powershell
cd worker-client
.\scripts\collect-wechat-diagnostics.ps1
```

诊断会在 `%LOCALAPPDATA%\CheJinWorker\evidence\` 下生成微信窗口控件树 JSON；如果当前 pywinauto 能捕获窗口图像，也会生成同名 PNG。该材料用于校准真实微信版本下的搜索框、添加通讯录入口、备注输入框和发送按钮识别。

Windows 实机验收前置项：

- 后端 `/readyz`、Worker 绑定/校验/心跳/领取/步骤/完成/失败接口可用。
- 后台已创建 Worker，并拿到有效 `Worker ID` / `Worker Token`。
- Worker 领取的 `add_friend` 任务必须包含可执行搜索字段：明文 `primary_phone` / `phone_plain` 或 `wechat`。普通后台任务详情仍只展示脱敏号码。
- Worker 领取的 `add_friend` 任务必须包含正式 RPA 字段：`verify_message`、`remark_name`、`remark_code`，且 `remark_name` 必须包含 `remark_code`。缺任一字段时客户端返回 `TASK_PAYLOAD_INVALID`，不触达微信 UI。
- Windows 机器已登录微信桌面客户端，且同一时间只允许一个 Worker 控制微信窗口。
- `CHEJIN_RPA_MODE=real` 下完成 add_friend 全链路：领取任务、调用 OmniAuto `add-friend-entry-click-plan-windows`、搜索客户、发送添加通讯录邀请、上报 `invite_sent` 或 `already_friend`，失败时上报 PRD 约定 `error_code` 和取证。
- 使用 `.\scripts\build-windows.ps1` 产出 `worker-client\dist\车金Worker客户端\车金Worker客户端.exe`，并在干净 Windows 机器启动验证。
