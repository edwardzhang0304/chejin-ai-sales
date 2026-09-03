# 车金 Worker 客户端

这是 P1a Windows 单应用客户端工程，最终交付物是 `CheJinWorkerClient.exe`。

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
- 直接打包当前源码树中的 `omniauto-rpa`
- 使用 PyInstaller 生成 Windows 单应用目录
- 校验当前 OmniAuto 完整源码树与安装包内完整目录一致
- 生成 `dist\reports\CheJinWorkerClient.manifest.json`，记录 Worker 提交、分支、合同版本和 SHA、OmniAuto 基础提交、选择性来源、车金集成提交和 tree SHA、测试/预检结果及安装包 SHA

正式打包要求 Git 工作区干净，且不允许跳过测试或预检。调试包必须显式执行：

```powershell
.\scripts\build-windows.ps1 -DevelopmentBuild
```

调试包 manifest 会标记 `formal_release=false`，不得作为正式发布包。

正式 Windows 包的 Vision 凭据只允许由 CI Secret
`CHEJIN_VISION_CLIENT_API_KEY` 注入，不得写入源码、提交记录、构建日志、
manifest 或故障证据。正式包固定 Provider、HTTPS 接口地址、模型和请求格式，
并忽略普通环境变量对这些配置及凭据的覆盖；只有源码开发包可以使用
`CUSTOMER_IMAGE_UNDERSTANDING_*` 环境变量联调。新 Windows 电脑不需要安装
Python，也不需要手工配置 Vision 环境变量。

正式包启动预检会使用一张内存中的 32×32 合成白图请求固定 Vision 服务，
确认凭据、网络、接口与固定模型均真实可用。该探针不读取或上传客户图片；报告
只记录“能力可用/不可用”、HTTP 状态和固定模型信息，不保存 Provider 响应正文、
异常原文或 Key。

随客户端分发的凭据无法做到绝对不可提取。因此生产环境必须使用独立、低权限、
仅允许指定 Vision 模型的客户端 Key，并在 Provider 侧设置额度、限流、监控、
吊销和轮换。CI Secret 是否已采用这些外部控制属于正式发布门禁，客户端代码本身
不能替代 Provider 侧权限配置。

产物：

```text
worker-client\dist\CheJinWorkerClient\CheJinWorkerClient.exe
```

校验已有产物：

```powershell
.\scripts\validate-package.ps1
```

正式客户端默认连接 `https://jiangsuchejin.com/api`，完整解压 ZIP 后可以直接双击 `CheJinWorkerClient.exe`，不需要先执行命令配置后端地址。

正式 UAT 包完整解压后，必须用包内启动脚本显式指定本次测试的后端 API。启动脚本会先检查后端、微信和运行依赖，把 JSON 报告保存到 `%LOCALAPPDATA%\CheJinWorker\diagnostics\`；预检失败时不会启动客户端：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-uat.ps1 -ApiBaseUrl "https://本次-UAT-后端/api"
```

本地开发和独立 UAT 仍可通过环境变量覆盖：

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

截图证据只会在 `%LOCALAPPDATA%\CheJinWorker\artifacts\` 内清理。成功流程默认保留 7 天，失败或发送结果未知的关键证据默认保留 30 天，默认最大占用 2GB；正在执行的 flow 会被保护。以下环境变量可覆盖默认值：

```powershell
$env:CHEJIN_ARTIFACT_SUCCESS_RETENTION_DAYS="7"
$env:CHEJIN_ARTIFACT_CRITICAL_RETENTION_DAYS="30"
$env:CHEJIN_ARTIFACT_MAX_BYTES="2147483648"
$env:CHEJIN_ARTIFACT_CLEANUP_INTERVAL_SECONDS="86400"
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

预检会检查 Windows/RPA 模式、依赖、OmniAuto sidecar 文件、内置 Vision
真实能力、本地数据目录、绑定状态、后端 `/readyz` 和微信桌面客户端探测结果。
报告只显示 Vision 安全状态，不输出凭据或 Provider 响应正文。存在 `error` 级失败时
命令返回非 0。

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
- 使用 `.\scripts\build-windows.ps1` 产出 `worker-client\dist\CheJinWorkerClient\CheJinWorkerClient.exe`，并在干净 Windows 机器启动验证。
