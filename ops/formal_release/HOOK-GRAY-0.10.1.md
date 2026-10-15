# Hook 0.10.1 小范围多人灰度交付

状态：2026-10-14 正式 CI 与 Windows 原生构建已完成；0.10.1 后端和运营后台已切换并通过生产 HTTPS、知识库及后台浏览器检查。Worker ZIP 尚未完成真实 Windows 两个快捷方式点击验收，未开放客户端更新或标记完整交付。第一批 2～3 名销售，每人独立 Windows、微信账号、Worker 身份。不能把本机模拟检查等同于 Windows 原生安装验收。

## 部署与身份

- 后端、运营后台、合同及客户端版本统一 0.10.1；Docker + PostgreSQL，复用现有正式 Compose、迁移、健康检查和备份流程。
- 每名销售单独创建 Worker 并绑定销售；在对应电脑用原客户端绑定界面录入其身份。不能复制现有测试机数据库、Token 或 client_instance_id。
- 各销售的飞书通知目标分别核实，不能把测试销售测试收件人批量复制给其他销售。
- 正式服务入口使用已批准的 HTTPS 地址。当前 Mac 局域网的配对转发器只批准给原 Windows，不扩展其访问白名单作为多人部署替代。

## 通用安装入口

由原 build-windows.ps1 -Transport hook 生成**已签名 Worker 包**，原签名、来源凭证、文件清单和更新门禁不变，包含 `Setup-CheJin-Hook.cmd` 和 `start-hook.ps1`。此 ZIP 不含微信安装文件或 Hook DLL，仅适用于已具备并核验对应微信/组件的现有测试机；不得称为新电脑完整安装包。禁止将历史 UAT 增量 ZIP 作为新电脑完整安装包。

新电脑交付时，先由客户端工程师通过受控渠道交付微信 3.9.10.16 x86 安装原件、配套 `version.dll` 原件及两份 SHA-256。运维用 `ops/formal_release/assemble_hook_new_pc_bundle.py` 把已验收 Worker ZIP 和这两份哈希匹配的原件装成外层完整包；脚本从 Worker 内部清单读取产品版本，不重新打包或改动 Worker ZIP。用法：

```text
python ops/formal_release/assemble_hook_new_pc_bundle.py \
  --worker-zip <已验收Worker.zip> \
  --wechat-installer <工程师提供的安装文件> --wechat-installer-sha256 <核验值> \
  --hook-dll <工程师提供的version.dll> --hook-dll-sha256 <核验值> \
  --wechat-file-version 3.9.10.16 --output-dir <受控交付目录>
```

外层包及第三方原件只在受控渠道分发，不进入公开 GitHub 仓库或公共 CI Artifact。组包通过仅证明文件来源哈希与清单一致；每台新 Windows 安装后仍须实际点击两个快捷方式验收版本、架构、参数、DLL 加载、端口归属及 Worker 环境检查。工程师原件和哈希未到手前，完整包状态保持【待交付】，不得以旧包或从测试机随意复制的文件补齐。

运维为每台电脑准备不含 Token/Key 的 profile JSON：schema_version=1、api_base_url、deployment。deployment 复用 HookDeployment 字段：revision、wechat_exe、dll_sha256、hook_api_port、receiver_port。Hash 必须来自验收过的 Hook DLL，不允许占位值或自动信任本机未知文件。

每台电脑须安装 **Windows 微信 3.9.10.16（x86/32 位）**，并把与该版匹配、已核验 SHA-256 的 `version.dll` 放在所配置 `WeChat.exe` 的同一目录。验收时核对实际启动的 EXE 文件版本、EXE 与 DLL 的 x86 架构、DLL 加载及哈希；其他微信版本或不匹配的组件停止交付。`30001` 仅为历史测试端口示例，正式端口取每台机器的部署配置。

现有已激活测试机只更新 Worker，不重装微信或覆盖 Hook DLL；原激活状态先核验再沿用。若出现重新激活提示，停止后按原受控授权流程处理，不能把激活码塞进公开包、profile 或日志。

运维先为这台电脑核对现有微信路径、组件校验值和两个端口，把经检查的 `profile.json` 放在解压后的 `CheJinWorkerClient` 文件夹。销售不需要输入命令或填写端口；首次设置只需双击同目录的 `Setup-CheJin-Hook.cmd`，按提示确认已关闭微信“语音自动转文字”。入口缺少配置时明确停止，不猜路径、端口或凭据。需要诊断时，运维仍可直接运行同一启动器：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-hook.ps1 -Configuration .\profile.json -ConfirmAutoTextDisabled -CreateShortcuts
```

首次配置只写部署设置、生成桌面 `Work WeChat`（工作微信）和 `CheJin Worker` 快捷方式，不绑定客户、不领取任务、不发送好友申请。已有配置必须完全相同，否则停止；原 OCR 或 UAT 绑定不得通过这个入口自动迁移或重置。给销售的步骤是：解压运维准备的包 → 双击 `Setup-CheJin-Hook.cmd` → 确认设置成功 → 双击桌面 `Work WeChat` → 双击 `CheJin Worker` 并在界面绑定自己的身份；验收通过前保持暂停。激活码和密钥不放在公开 Worker 包或 profile 中。

随后使用桌面 `CheJin Worker` 快捷方式，或由运维诊断时执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-hook.ps1
```

数据目录：%LOCALAPPDATA%\CheJinWorkerHook。微信快捷方式与启动器共用数据库中的 HookDeployment，包含 StartPort=<配置值>、ConnectType=http、RecvType=0；已有微信只核验，不强制重启。任务必须等用户点击“开始接单”。Hook 包不使用 OCR 默认服务地址；直接运行 EXE 也必须先有本机部署配置。

旧 /uat/enrol 桥接仅为已发测试包保留，不进入此通用入口。旧测试线索重建时，旧桥接的联系人引用必须同步校验并通过真实鉴权入口检查；正式灰度启动不依赖客户存在。

## 灰度前验收

1. 最终冻结源码、来源凭证、架构师复审、Windows Python 3.12 / Qt 6.6.3.1 原生构建和签名全部对应同一候选。
2. 一台干净 Windows 上实际安装并点击两个快捷方式；核对启动参数、DLL Hash、端口归属、原绑定界面及开始/暂停按钮。
3. 三 Worker PostgreSQL 并发：各领自己的任务；跨身份领取拒绝；一台换号或故障不会停掉其余两台。
4. 故障矩阵：后台短暂断开后自动恢复原接单意图；暂停者不自动接单；发送结果未知不重发；语音未知保留证据并交接；每名销售实际收到自己的飞书通知。
5. 同 Hook 通道升级及失败回退演练，保留本机数据库、在途记录和未知调用；不允许把 Hook 数据目录直接交给 OCR 老包。

## 停止扩大与回退

出现跨销售数据、错发、重复物理发送、未知结果被重试、转人工通知丢失或持续无法恢复：立即停止扩大范围，按原暂停/故障收尾流程停新任务。不能清空数据库或删未知记录来解除故障。

普通界面更新仅关闭 Worker；组件/启动参数发生变化或当前微信进程确需隔离，才重启微信。回退限已验证的同 Hook 通道版本，沿原升级器和数据快照恢复规则；有新业务写入的 PostgreSQL 不直接覆盖成旧备份。

## 尚需人工/平台证据

Windows 原生构建、真实快捷方式点击、2～3 台机器分别绑定及飞书收件确认、多人持续运行记录，必须单独留证。未完成前不注册发布、不扩大灰度、不宣称全部收尾通过。

## 本轮本地验证与复审记录

- Python 3.12、PySide6 6.6.3.1：安装、版本一致性、通道与更新回退检查 81 项通过；账号、消息发送、语音未知结果及恢复相关回归 144 项通过。这些是分组执行数，含重复覆盖，不作为不重复用例总数。
- 与正式 Compose 一致的 PostgreSQL 15：三独立 Worker 的跨身份拒绝、并发领取和单账号故障隔离 2 项通过；AI 容量、跨进程互斥与异常恢复 10 项通过。仅测试库，无真实微信调用。
- 架构师 5 号在 2026-10-14 通过源码准备复审。环境还原、Hook/OCR 数据目录隔离、构建公钥映射和非零退出码测试缺口已处理；此结论不是灰度发布批准。
- Windows PowerShell 5.1 启动器 12 个模拟用例已加入构建门禁，仅静态复审，尚未原生执行。真实 EXE、微信与 Worker 快捷方式点击、升级重启和包级复审仍待验。
- 当前产品版本（包括启动失败诊断）统一 0.10.1；构建批次与协议结构号分别记录，不能以改产品版本规避一致性检查。
- 本轮证据位于 output/hook-gray-preparation-20261014，manifest.json 列出最终源码文件和通过报告摘要。早期环境缺件、SQLite 并发限制和测试命令错误报告保留供审计，不计入通过证据。

## 本次恢复修正的验收边界

加好友技术故障复用原分类、持久停止和结果回执入口；找不到客户属于单任务结果。失败/已是好友回报断网、回执丢失或租约到期，只补交原任务结果，不重复 UI 操作。任务终态允许故障中结束原 Flow：仅后端已确认 faulted 时调用原 finish，由后端取消 queued 尾段；sending 未核对仍阻止收尾。暂停及仅本地故障不取消待发尾段，新任务仍受原门禁约束。

加好友 prepare 请求前保存原任务/Flow/fence/Worker/账号的 pre-ui 记录，构造界面前先持久化 ui-started 标记。只有身份匹配且确定未进入界面时，才能复用原 WORKER_INTERRUPTED 分类和失败回执结算；缺少旧记录或已进入界面均不推断为未操作，不重跑界面。

客户作废后，native-work 的 reconciliation_binding 只随原 retained_sends 返回，不加入可服务 bindings。历史 coverage 的可选 reconciliation 附原 action/task/flow/fence/账号代次/进程/send token/journal 摘要；同时核验当前账号。仅用于旧发送结果核对，不投递普通消息 outbox，不更改业务检查点，不建立新批次、不启动模型、语音或发送。正常 coverage、claim/begin 和客户作废门禁保持。

隔离调用缺少同一次调用关联证据时显示通用原因，不沿用未核实的旧故障说明；原故障审计日志保留。本修正不改产品版本，仍为 0.10.1。源码与模拟自测通过不代表正式包、真实快捷方式或多机验收通过。

本轮恢复自测：HTTP 69 项、PostgreSQL 15 恢复 46 项、Worker/OCR 共用回执 106 项、分段边界 4 项通过，分组有重合。架构师4号独立复跑 52/1/41/3 四组，架构师5号独立复跑 48 项，均通过；源码对应 output/hook-gray-recovery-fixes-20261014 的冻结清单。后续4个构建工具文件也经双审，发布工具161项通过；对应 output/hook-gray-build-preparation-20261014。源代码通过与正式 Windows 成品验收分开，不以本地预检生成可信 CI 凭证。

## 交接与公开仓库边界

按项目负责人 2026-10-14 的确认，工程侧完成源码自测、双架构复审和 GitHub 推送；正式 Windows 构建、签名、成品验收与发布由交付负责人操作。推送代码不会自动触发 Hook 正式打包，`hook-windows-build.yml` 仅保留人工入口。自动源码/工具检查不等于成品构建或发布。

公开仓库保留模拟数据驱动的回归测试。实机 OCR 截图、昵称、测试手机号、数据库、联调日志和本地输出不推送；模拟像素与 OCR 行仅证明定位及状态逻辑，不能代替真实 Windows 识别验收。旧截图夹具本地保留，使用 `.gitignore` 排除。

旧 OCR `avatars_20260904/send_*.png` 原始聊天截图同样仅本地保留。依赖这些图片的 OCR 事故回放仍保留原哈希与断言，执行者需提供有权限的私有证据，不自动跳过或以合成测试替代。历史脱敏改写后，旧 CI 身份与新 Git 提交不得混用；正式交付必须重新执行对应源码与工具门禁。
