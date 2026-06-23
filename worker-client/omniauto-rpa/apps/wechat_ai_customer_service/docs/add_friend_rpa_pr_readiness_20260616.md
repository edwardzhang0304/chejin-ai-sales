# add_friend RPA PR readiness

更新时间：2026-06-16

> 阅读说明（2026-06-17）：本文主体是朋友 PR #17 / commit `7199023` 的历史 readiness 快照，用于追溯原始实现和当时回归口径。当前 add_friend 后续开发的源-of-truth 是 [`add_friend_cli_contract_stability_plan_20260617.md`](add_friend_cli_contract_stability_plan_20260617.md) 和 [`add_friend_rpa_adaptive_refactor_plan_20260617.md`](add_friend_rpa_adaptive_refactor_plan_20260617.md)。本文中出现的早期 `runtime/add_friend_entry_click_plan` 和旧脚本口径代表 PR 原始/参考路线，不代表当前正式主路线；当前对外稳定 CLI 名称重新收口为 `add-friend-entry-click-plan`。

## 2026-06-17 Windows 多分辨率适配标记

当前 OmniAuto 微信操控栈是 Windows 适配：Win32 窗口枚举、截图、OCR 区域、鼠标点击、键盘输入、发送、监听、加好友链路都按 Windows 微信桌面版实现。不同显示分辨率、DPI、窗口尺寸会改变微信窗口内布局，不能直接复用固定坐标。

本 PR 原始实现曾以 `add-friend-entry-click-plan` 承载 Windows 1920x1080 固定布局假设。当前该 CLI 名称已恢复为 Worker-facing 稳定主入口，并在 Windows 上路由到自适应实现；`add-friend-entry-click-plan-windows` 保留为显式 Windows 别名，固定 1920x1080 对照实现应使用显式 reference route。

```powershell
.\apps\wechat_ai_customer_service\scripts\run_wechat_add_friend_entry_click_plan_windows.ps1
```

PR 原始候选包：

```text
/Users/zhangwentao/Documents/车金/deliverables/omniauto-add-friend-rpa-pr-candidate-20260616.zip
```

## Scope

本阶段只包含 OmniAuto 仓库可独立 PR 的 add_friend RPA 能力，不接 Worker，不新增或修改车金服务端接口。

PR 原始正式主链路固定为：

```text
add-friend-entry-click-plan
```

本轮开发过程中的临时入口已从正式包移除：

```text
add-friend
add-friend-plan
add-friend-entry-plan
```

## Formal Payload

主链路必须传入：

```text
phone_or_wechat（由 phone 或 wechat 提供，至少一个必填）
verify_message
remark_name
remark_code
```

字段语义：

```text
phone_or_wechat -> 搜索目标用户，phone 优先，wechat 作为目标 ID
verify_message -> 微信申请语输入框
remark_name    -> 微信备注名输入框
remark_code    -> 外部系统生成的短码，OmniAuto 只消费不生成
```

强校验规则：

```text
缺 phone/wechat    -> TASK_PAYLOAD_INVALID
缺 verify_message -> TASK_PAYLOAD_INVALID
缺 remark_name    -> TASK_PAYLOAD_INVALID
缺 remark_code    -> TASK_PAYLOAD_INVALID
remark_name 不包含 remark_code -> TASK_PAYLOAD_INVALID
```

校验失败必须发生在窗口探测和微信 UI 操作之前，报告中应包含：

```text
wechat_ui_action_attempted=false
window_probe.skipped=true
validation_errors
```

旧字段限制：

```text
sales_name 不再用于主链路生成申请语
remark 不再用于主链路兜底备注名
```

## Result Contract

成功：

```text
task_status=completed
result_code=invite_sent
```

发送邀请后的链路不产出 `already_friend`。

失败：

```text
TASK_PAYLOAD_INVALID
PHONE_NOT_FOUND
ACCOUNT_RESTRICTED
ADD_CONTACT_ENTRY_NOT_FOUND
INVITE_FORM_WINDOW_NOT_FOUND
INVITE_CONFIRM_CLICK_FAILED
OTHER
```

## Diagnostics

每个关键步骤以统一 step event 输出：

```text
step_id
title
status
state_before
state_after
ocr_items
targets
selected_target
artifacts
timing_ms
result
```

PR 原始 HTML/JSON 报告由 step event 生成，并保留：

```text
runtime/add_friend_entry_click_plan/<timestamp>/
runtime/add_friend_entry_click_plan/latest/
```

完整主链路的报告最终事件应为：

```text
invite_confirm_after_click · 点击确定后结果复核
```

`final_popup_detection` 是早期入口弹出菜单诊断事件，完整 add_friend 主链路报告中不再追加，避免误导成“发送邀请后还要回到添加朋友列表确认”。

## Module Boundary

第一阶段已拆分边界：

```text
add_friend_contract.py        字段契约和校验
add_friend_routes.py          正式主链路路由清单，仅保留 add-friend-entry-click-plan
add_friend_flow.py            add_friend 主 Flow 编排
add_friend_flow_context.py    运行上下文、timing、event、报告收口
add_friend_flow_events.py     payload/result 到 step event 的映射
add_friend_payloads.py        task payload builder
add_friend_result_mapping.py  result_code/error_code 映射
add_friend_locator.py         统一定位结构
add_friend_ocr.py             OCR 文本标准化和匹配
add_friend_screenshot.py      截图 artifact 元数据
add_friend_actions.py         点击/输入动作结果元数据
add_friend_pacing.py          分级等待策略
```

`wechat_win32_ocr_sidecar.py` 仍保留 Win32/OCR glue 和部分底层函数；后续迭代再逐步迁移，避免一次性搬动已验证链路。

## Local Verification

源码 smoke：

```bash
python3 -m py_compile apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py
python3 apps/wechat_ai_customer_service/tests/run_add_friend_package_smoke.py
```

打包：

```bash
bash scripts/package_wechat_add_friend_live.sh /Users/zhangwentao/Documents/车金/deliverables/omniauto-add-friend-rpa-pr-candidate-20260616.zip
```

包内 smoke：

```bash
cd /tmp
unzip -q /Users/zhangwentao/Documents/车金/deliverables/omniauto-add-friend-rpa-pr-candidate-20260616.zip
cd omniauto-add-friend-rpa
python3 apps/wechat_ai_customer_service/tests/run_add_friend_package_smoke.py
```

PR 原始 Windows 回归命令：

```powershell
.\apps\wechat_ai_customer_service\scripts\run_wechat_add_friend_entry_click_plan.ps1 `
  -Phone "17368746889" `
  -VerifyMessage "我是车金二手车张伟" `
  -RemarkName "CJ-张伟-CJ8K2P-6889" `
  -RemarkCode "CJ8K2P"
```

预期：

```text
task_status=completed
result_code=invite_sent
error_code 为空
```

打开 latest 报告：

```powershell
Start-Process -FilePath ".\runtime\add_friend_entry_click_plan\latest\add_friend_entry_click_review.html"
```

自动检查 latest JSON：

```powershell
.\apps\wechat_ai_customer_service\scripts\check_wechat_add_friend_entry_click_latest.ps1 `
  -ExpectedVerifyMessage "我是车金二手车张伟" `
  -ExpectedRemarkName "CJ-张伟-CJ8K2P-6889" `
  -ExpectedRemarkCode "CJ8K2P"
```

Windows 2026-06-16 实机回归结论：

```text
包内 smoke 已通过：All 25 add_friend package smoke checks passed.
三字段主链路已通过：completed + invite_sent。
latest JSON 自动检查已通过：add_friend latest report check: OK。
HTML 报告人工确认：申请语、备注名、短码、确定点击事件正确。
缺 verify_message 已通过：TASK_PAYLOAD_INVALID，wechat_ui_action_attempted=false。
缺 remark_name 已通过：TASK_PAYLOAD_INVALID，wechat_ui_action_attempted=false。
缺 remark_code 已通过：TASK_PAYLOAD_INVALID，wechat_ui_action_attempted=false。
remark_name 不包含 remark_code 已通过：TASK_PAYLOAD_INVALID，remark_code_valid=false，wechat_ui_action_attempted=false。
搜索不到 PHONE_NOT_FOUND 未在本轮强制复测，避免继续操作微信搜索流程。
风控 ACCOUNT_RESTRICTED 未主动触发，避免为了回归制造账号限制。
```

## Package Contents

PR 包应至少包含：

```text
apps/wechat_ai_customer_service/README.md
apps/wechat_ai_customer_service/docs/add_friend_rpa_pr_readiness_20260616.md
apps/wechat_ai_customer_service/requirements-add-friend.txt
apps/wechat_ai_customer_service/adapters/add_friend_*.py
apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py
apps/wechat_ai_customer_service/adapters/wechat_connector.py
apps/wechat_ai_customer_service/scripts/run_wechat_add_friend_entry_click_plan.ps1
apps/wechat_ai_customer_service/scripts/check_wechat_add_friend_entry_click_latest.ps1
apps/wechat_ai_customer_service/tests/run_add_friend_package_smoke.py
scripts/package_wechat_add_friend_live.sh
```

`run_add_friend_package_smoke.py` 会校验正式字段、路由隔离、payload builder、step event、locator、分级等待、README 口径和包内必需文件。

## PR Description Draft

```text
Summary
- Promote add-friend-entry-click-plan as the only formal add_friend RPA main route.
- Require phone_or_wechat, verify_message, remark_name, and remark_code for the formal route; reject missing or mismatched payloads before touching WeChat UI.
- Remove the temporary add-friend/add-friend-plan/add-friend-entry-plan actions from the formal package.
- Split add_friend contracts, routes, flow context, diagnostics, locator, result mapping, payload builders, pacing, screenshot/action metadata, and flow orchestration into focused modules.
- Generate HTML/JSON reports from unified step events with timestamp and latest artifact directories.

Result contract
- Success after invite confirm reports completed + invite_sent.
- Post-confirm add_friend flow does not emit already_friend.
- Failure mapping includes TASK_PAYLOAD_INVALID, PHONE_NOT_FOUND, ACCOUNT_RESTRICTED, ADD_CONTACT_ENTRY_NOT_FOUND, INVITE_FORM_WINDOW_NOT_FOUND, and INVITE_CONFIRM_CLICK_FAILED.

Verification
- python3 -m py_compile apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py
- python3 apps/wechat_ai_customer_service/tests/run_add_friend_package_smoke.py
- Package smoke after scripts/package_wechat_add_friend_live.sh
- Windows real-machine regression completed for the formal happy path and formal field-contract failures.
- PHONE_NOT_FOUND and ACCOUNT_RESTRICTED remain supported by result mapping and smoke contracts, but should be re-run only when a safe real-account test condition is available.
```
