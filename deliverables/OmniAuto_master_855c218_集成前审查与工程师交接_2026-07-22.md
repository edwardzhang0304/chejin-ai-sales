# OmniAuto master 855c218 集成前审查与工程师交接

日期：2026-07-22
范围：计划第 1-3 步，仅冻结基线、同步仓库、审查并测试 OmniAuto；尚未修改车金客户端代码，尚未打 V16.105。

## 1. V16.104 回滚基线

| 项目 | 已冻结值 |
| --- | --- |
| 车金分支 | `codex/c2-v1695` |
| 车金提交 | `259976e24e5c83eefc720b43ad2910865051c875` |
| OmniAuto 旧分支 | `codex/wechat-c2-v1695-ocr` |
| OmniAuto 旧提交 | `8f832dd7e2ed78ff5535924b12818475de066a27` |
| 安装包 | `deliverables/chejin-worker-client-20260721-v16.104.zip` |
| 安装包 SHA256 | `e0c022ecf0b4838a5f4ecd99b6520e43be17f5bd61d60880924a2183e5d6bf24` |
| 安装包规模 | 3,116,296 bytes，418 个文件 |
| manifest SHA256 | `64b0f0fe5ff7c040e20e06cf0e2b7fe957abf1d15ab6b6296abdb09f301f4cbb` |

旧分支、旧安装包和 manifest 均保留，发生集成问题时可直接退回 V16.104。

## 2. 仓库同步结果

| 位置 | 当前提交 |
| --- | --- |
| `meta-xucong/omniauto master` | `855c21881641cdb2f9fe69d3f2e1caa05e37d04d` |
| `edwardzhang0304/omniauto master` | `855c21881641cdb2f9fe69d3f2e1caa05e37d04d` |
| 本地回归分支 | `codex/upstream-master-regression-20260722` |
| 本地回归分支 HEAD | `855c21881641cdb2f9fe69d3f2e1caa05e37d04d` |

Edward 的 `master` 原先落后 37 个提交，已通过纯 fast-forward 同步；没有强推，没有覆盖 Edward 独有提交。PR #31 的 `210bc5e` 不在本次 `master` 回归分支中。

`master` 已包含：

- PR #28 及最终语音锚点提交 `8f832dd`；
- PR #29 集成；
- 内部语音绑定与平台合同加固；
- Sidecar/Connector 旧图片入口清理；
- 许聪后续 Vision、Brain 和多会话 Scheduler 代码。

## 3. 离线回归结果

测试使用隔离目录 `/private/tmp/omniauto-regression-deps` 补齐仓库声明的 `psutil==7.2.2`、`openpyxl==3.1.5`，未修改项目或系统 Python 环境。

| 测试组 | 结果 |
| --- | --- |
| 外部合同兼容 | 3/3 |
| Vision 绝对边界 | 7/7 |
| 可选插件隔离 | 7/7 |
| 图片捕获/旧入口合同 | 8/8 |
| PR #28 additive audit | 4/4 |
| PR #28 runtime adapter | 5/5 |
| Win32/OCR 兼容，含文字、语音和角色 | 241/241 |
| Win32 捕获 | 14/14 |
| 窗口激活 | 3/3 |
| 会话定位 | 6/6 |
| 发送风险 | 4/4 |
| RPA 验收 | 10/10 |
| Vision worker | 3/3 |
| Scheduler 当前图片桥 | 2/2 |
| Vision 结构触发恢复 | 12/12 |
| 多模态历史 | 7/7 |
| 本地会话真值/公平性 | 13/13 |
| 多会话 Scheduler | 189/189 |
| 图片方向 | 7/7 |
| 图片 Router | 7/7 |

合计 552 项有效检查通过。另有 2 项 PR effective-runtime 包装检查通过，内部重复执行 OCR 241 项和窗口动作规划 28 项，不重复计入合计。代码工作区保持干净，`git diff --check` 通过。

## 4. 架构判断

### 可以作为客户端集成输入

1. PR #28 已进入许聪正式 `master`，文字/OCR/语音解析不再需要从旧 PR 分支单独取代码。
2. V16.104 的语音结构锚点修复已进入 `master`，并通过最新 OCR 241 项回归。
3. `master` 删除了 Sidecar 中旧的图片观察门面，也删除了旧图片保存、落盘和历史路径入口；当前图片能力集中到独立 `optional_plugins.vision`。
4. 同时长语音不再使用“唯一但距离很远也强行绑定”的回退，当前行为更保守，避免错误父语音绑定。

### 不能直接启用或整包照搬

1. 许聪的 Scheduler 默认支持多会话和 Planner/Polish 并发，默认并发值不是车金已确认的单会话串行模型。
2. 车金目标流程要求：打开一个会话后保持原会话和 UI 锁，处理文字、语音、图片，等待该批 Brain 终态并完成发送或 no_action，之后才能切换会话。
3. 因此不能把许聪 Scheduler 的“LLM 运行时继续捕获其他会话”直接接入车金。
4. Vision 测试通过只证明许聪独立模块内部合同成立，不代表车金图片 C2-C3 流程已经开发或验收完成。
5. 群聊继续冻结。车金仍只允许“有效短码 + `conversation_type=private`”进入读取、入库和 Brain。
6. 车金 V3 的 `same_row_avatar`、`parent_voice`、`source_message_key`、`dedupe_key`、`authorization_revision` 门禁不得被 OmniAuto 内部 Scheduler 绕过。

## 5. 给客户端工程师的下一步边界

1. 从车金 V16.104 基线新建客户端集成分支，不直接修改旧分支。
2. OmniAuto 来源固定为 Edward `master@855c218`，不再使用旧 PR 分支，也不带入 PR #31。
3. 先同步并验证 OCR、语音、Sidecar runner、运行时适配和旧图片入口清理；不要先启用许聪的通用多会话 Scheduler。
4. 保留车金 `worker-client/chejin_worker_client/wechat_c2.py` 和 V3 合同边界，不新增同义接口、角色字段或消息身份规则。
5. 图片阶段只接入已确认的新 Vision 能力入口；在车金单会话串行、UI 锁、Brain batch 合同完成前，不得宣称图片流程已接入。
6. 完成代码同步和自动测试后再提交架构复核；通过后才能升级版本并打 V16.105 Windows 包。

## 6. 当前结论

计划第 1-3 步完成。许聪和 Edward 的 `master` 已统一，本地主分支回归通过，可交给客户端工程师开始“车金客户端集成分支”的工作。当前还不能直接打 V16.105，也不能直接把整个 OmniAuto Scheduler/Vision 运行时作为车金正式流程启用。
