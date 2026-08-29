# WeChat 发送前两类误判最小修复设计

版本：v0.1.0
日期：2026-08-29
范围：Worker 发送前焦点保护、C2/C3 可见消息连续性校验

## 依据与边界

本开发文档遵循以下架构基线：

- `apps/wechat_ai_customer_service/docs/customer_visible_reply_ownership_baseline.md`
- `apps/wechat_ai_customer_service/docs/customer_service_external_contract_and_optional_plugin_baseline.md`

本次只修复 Worker 的操作正确性，不改变 Brain、客户可见文案、后端接口、会话字段、路由、数据库结构或产品事实来源。语音和识图插件不参与本次修改。

## 已确认的两个错误

### 1. 发送前焦点误判

`send_payload` 在真正输入前调用 `recover_send_window_guard`，但原实现直接返回一次基础校验，忽略了调用方传入的 `max_attempts`。当微信窗口已经存在、当前前台窗口暂时不是微信时，流程直接以 `foreground_not_wechat_target` 失败，`action_phase` 保持 `not_attempted`。

### 2. 上下文一致性误判

OCR 产生的历史文本行没有持久化身份 token。旧画面和新画面的消息数量、顺序、类型、内容签名完全相同时，原实现仍要求强边界 token，因此返回 `equal_facts_without_strong_boundary`，被上层解释成发送前上下文变化。

## 最小修复方案

### 焦点恢复

1. 先执行原有基础焦点校验；已在微信目标窗口时直接通过。
2. 仅当失败原因是 `foreground_not_wechat_target` 或 `foreground_probe_failed` 时，调用现有 `activate_window(hwnd, foreground_only=True)`。
3. 激活后等待 200 至 300 毫秒，再重新执行基础焦点校验。
4. 最多执行两次激活与复检；仍失败则继续 fail-closed，禁止进入截图基线、输入和发送。
5. `window_not_visible` 等非焦点失败不触发恢复，避免把窗口生命周期问题误当成焦点问题。

恢复过程不启用坐标点击、不做激进线程附着、不绕过现有发送前守卫。

### 上下文一致性

在现有 `old_keys == new_keys` 分支增加一个窄条件：当序列非空且每一行的类型都是 `text` 或 `system` 时，直接判定 `business_sequence_equal`，原因记录为 `same_text_sequence`，同时保留逐行匹配关系。

图片、语音及包含媒体的序列仍按原强边界规则处理；无法证明媒体身份时继续要求上下文扩展或阻断。这样只消除已确认的纯文本 OCR 误判，不放宽媒体防串会话保护。

## 验收标准

- 焦点不在微信但微信窗口有效时，最多两次“激活、等待、复检”，恢复成功后才允许进入发送基线。
- 焦点恢复两次仍失败时，发送前停止，`action_phase=not_attempted`，不触发任何物理输入。
- 非焦点错误不激活微信。
- 无强边界 token 的完全相同纯文本序列判定为 `business_sequence_equal`。
- 重复图片、重复语音、媒体替换和不连续序列仍保持原阻断行为。
- 不新增外部字段、不修改既有路由和函数签名。

## 测试与审计

定向测试覆盖：纯文本无边界 token、重复弱媒体、焦点恢复成功、焦点恢复失败上限、非焦点失败不恢复，以及发送未进入物理输入。提交前执行 Worker 全量检查、Python 语法检查、差异空白检查，并核对改动范围不涉及后端、VPS 或凭据。

## 回滚

本次无数据库迁移和配置迁移。若线上验证仍需回退，回滚本 PR 提交即可恢复原有比较器和焦点守卫行为。
