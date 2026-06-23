# OmniAuto

> **影刀 RPA 的开源替代方案** —— 一个以 Python 为核心、以 AI 为编排层、以开源为信仰的通用自动化框架。

[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## 1. 项目定位

OmniAuto 的目标是做出**类似「影刀 RPA」的自动操作功能**，自己从头编写，**扩展性和与 Python、AI 结合的相性**是最高优先级。

与市面闭源 RPA 工具不同，OmniAuto：
- **完全开源**：所有核心代码用 Python 3.13+ 编写，可自由扩展
- **AI 原生**：自然语言描述即可生成原子化自动化脚本
- **三层架构**：交互层 → AI 决策层 → 确定性状态机执行层
- **双引擎**：浏览器自动化（Playwright/CDP）+ 桌面视觉自动化（pyauto-desktop）
- **断点续传**：基于 SQLite 的持久化状态机，任务崩溃后可恢复

---

## 2. 快速安装

### 环境要求
- Python 3.13+
- Windows（桌面视觉自动化主要支持 Windows，浏览器自动化跨平台）

### 安装步骤

```bash
# 1. 克隆仓库并进入目录
cd AI_RPA

# 2. 使用 uv 安装依赖（推荐）
uv sync

# 3. 安装 Playwright Chromium 浏览器
uv run python -m playwright install chromium

# 4. 验证安装
uv run omni --version
```

> 如果没有 `uv`，也可以使用 `pip install -e .` 安装。

---

## 3. 快速开始

### 3.1 运行内置 Demo

无需编写任何脚本，一键体验自动化：

```bash
uv run omni demo --headless
```

该命令会自动：
1. 打开 https://httpbin.org/html
2. 提取页面 `<h1>` 标题
3. 输出结果：`Herman Melville - Moby-Dick`

### 3.2 用自然语言生成脚本

```bash
uv run omni generate "访问百度并搜索关键词" --output workflows/generated/browser/my_task.py
```

生成的脚本位于 `workflows/generated/browser/my_task.py`，可直接查看和修改。
示例脚本（如百度、Hacker News、豆瓣等）存放在 `workflows/examples/`。

### 3.3 校验脚本安全性

```bash
uv run omni validate workflows/generated/browser/my_task.py
```

校验器会扫描 `eval`、`exec`、`subprocess` 等危险操作，以及硬编码密码/API Key。

### 3.4 执行脚本

```bash
uv run omni run --script workflows/generated/browser/my_task.py --headless
```

执行完成后，你会看到工作流的最终状态和输出数据。

### 3.5 查看异常队列

```bash
uv run omni queue --show-pending
```

用于查看被 Guardian（人工审核节点）阻塞或执行失败的任务。

---

## 4. 核心架构

OmniAuto 采用**三层架构**，核心原则是：**AI 绝不直接操作 UI，只生成配置；确定性状态机负责严格执行。**

```
┌─────────────────────────────────────────────────────────────┐
│                    交互层 (Presentation)                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │ 自然语言接口  │  │ 任务监控面板  │  │ 异常处理工作台       │ │
│  │ (CLI / Web)  │  │ (Prefect UI) │  │ (人工审核队列)       │ │
│  └──────────────┘  └──────────────┘  └──────────────────────┘ │
└──────────────────────────┬────────────────────────────────────┘
                           │ API / 事件总线
┌─────────────────────────────────────────────────────────────┐
│                    决策层 (Orchestration)                     │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │              AI 策略引擎 (Claude / GPT / Ollama)         │ │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │ │
│  │  │ 脚本生成器   │  │ 异常决策器   │  │ 视觉理解器   │    │ │
│  │  │ (Smolagents) │  │ (LLM)        │  │ (OmniParser) │    │ │
│  │  └──────────────┘  └──────────────┘  └──────────────┘    │ │
│  └─────────────────────────────────────────────────────────┘ │
└──────────────────────────┬────────────────────────────────────┘
                           │ 确定性指令
┌─────────────────────────────────────────────────────────────┐
│                    执行层 (Execution)                       │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │              状态机工作流引擎 (State Machine)            │ │
│  │         ┌──────────┐  ┌──────────┐  ┌──────────┐       │ │
│  │         │ 状态持久 │→│ 步骤执行 │→│ 异常捕获 │       │ │
│  │         │ 化存储   │  │ 器       │  │ 上报     │       │ │
│  │         └──────────┘  └──────────┘  └──────────┘       │ │
│  └─────────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │              多引擎自动化驱动                            │ │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐ │ │
│  │  │ CDP 真实浏览器│ │ Stagehand AI │ │ 视觉自动化引擎   │ │ │
│  │  │ (Playwright) │ │ (预留)       │ │ (pyauto-desktop) │ │ │
│  │  └──────────────┘ └──────────────┘ └──────────────────┘ │ │
│  └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### 关键设计

- **AtomicStep（原子步骤）**：不可再拆分的最小执行单元，封装 `action` + `validator` + `retry`
- **Workflow（工作流）**：按顺序执行 AtomicStep，支持 Guardian 节点、断点续传、异常上报
- **StateStore（状态存储）**：基于 SQLite，每一步完成后立即 `COMMIT`，支持崩溃恢复
- **StealthBrowser（隐形浏览器）**：基于 Playwright，注入反检测脚本，支持真实 Chrome Profile 与动态 Profile 旋转
- **AuthManager（认证管理）**：检测登录页与验证码，自动弹出系统提示引导用户手动完成验证（见截图示例）
- **VisualEngine（视觉引擎）**：基于 pyauto-desktop，支持跨分辨率自动缩放、图像识别、OCR

---

## 5. 开发自定义原子步骤

你可以轻松编写自己的原子步骤并组合成工作流。

```python
# my_workflow.py
from omniauto.core.state_machine import Workflow, AtomicStep
from omniauto.core.context import TaskContext, StepResult
from omniauto.engines.browser import StealthBrowser

async def my_custom_step(ctx: TaskContext) -> StepResult:
    browser = ctx.browser_state["browser"]
    await browser.goto("https://example.com")
    title = await browser.extract_text("h1")
    return StepResult(success=True, data=title)

workflow = Workflow(task_id="my_task")
workflow.add_step(AtomicStep(
    step_id="get_title",
    action=my_custom_step,
    validator=lambda r: r.success,
))
```

运行：

```bash
uv run omni run --script my_workflow.py --headless
```

### 内置原子步骤清单

| 步骤类 | 功能 |
|--------|------|
| `NavigateStep(url)` | 浏览器导航 |
| `ClickStep(selector)` | 点击元素 |
| `TypeStep(selector, text)` | 输入文本（模拟人类打字节奏） |
| `ExtractTextStep(selector)` | 提取文本 |
| `ExtractAttributeStep(selector, attr)` | 提取属性 |
| `ScreenshotStep(output_dir)` | 全页截图 |
| `WaitStep(seconds)` | 固定等待 |
| `ScrollToBottomStep()` | 滚动到页面底部 |
| `HotkeyStep(*keys)` | 浏览器热键 |
| `VisualClickStep(image_path)` | 基于图像识别的桌面点击 |

---

## 6. CLI 命令参考

```bash
# 生成脚本
omni generate "任务描述" --output ./workflows/generated/browser/task.py

# 校验脚本
omni validate ./workflows/generated/browser/task.py

# 执行脚本（支持断点续传）
omni run --script ./workflows/generated/browser/task.py --headless --task-id my_task_001

# 运行内置 Demo
omni demo --headless

# 查看异常队列
omni queue --show-pending
```

---

## 7. 测试

项目包含三层测试：

```bash
# 运行全部测试
uv run pytest platform/tests/ -v
```

- **单元测试** (`platform/tests/unit/`)：StateMachine、Validator、Mouse 工具函数
- **集成测试** (`platform/tests/integration/`)：StealthBrowser、VisualEngine
- **端到端测试** (`platform/tests/e2e/`)：完整工作流链路

当前测试状态：**29/29 通过**

---

## 8. 技术栈与开源致谢

OmniAuto 的开发遵循**开源工具优先**原则，在引入任何自研模块前，优先调研并集成高质量开源库：

| 领域 | 选用开源工具 |
|------|-------------|
| **浏览器自动化** | [Playwright](https://playwright.dev/) |
| **桌面视觉自动化** | [pyauto-desktop](https://pypi.org/project/pyauto-desktop/) |
| **视觉识别** | [OpenCV](https://opencv.org/) + [OmniParser](https://github.com/microsoft/OmniParser) |
| **代码生成 Agent** | [Smolagents](https://github.com/huggingface/smolagents) |
| **工作流编排参考** | [Prefect](https://www.prefect.io/) |
| **CLI 框架** | [Click](https://click.palletsprojects.com/) |
| **结构化日志** | [structlog](https://www.structlog.org/) |

同时持续关注：`browser-use`、`Stagehand`、`Skyvern`、`agent-browser`、`Lightpanda`、`Robocorp`、`BotCity`。

---

## 9. 开发路线图

### Phase 1: 基础引擎 ✅
- [x] 集成 Playwright，完成基础浏览器控制
- [x] 实现 CDP 连接真实 Chrome 能力
- [x] 完成反检测脚本库（StealthConfig）
- [x] 集成 pyauto-desktop，完成跨分辨率桌面自动化

### Phase 2: 状态机与编排 ✅
- [x] 实现 StateMachine 核心类（持久化、重试、异常上报）
- [x] 开发 AI 脚本生成器（基于模板 + TaskPlanner）与 Deterministic RPA Workflow 模板系统

### Phase 3: 视觉兜底与原子步骤库 ✅
- [x] 集成视觉识别与浏览器 → 视觉降级逻辑
- [x] 开发 10+ 常用原子步骤

### Phase 4: CLI 与工具链 ✅
- [x] 开发 CLI 工具链（generate / validate / run / demo / queue）
- [x] 编写 Claude Code / Kimi Code 可用的 Skill 上下文（`agents.md`）

### Phase 5: 生产化（进行中）
- [ ] Web IDE 低代码编排界面
- [ ] 任务市场（共享脚本模板）
- [ ] 安全审计模块与沙箱执行
- [ ] 日志监控（Prometheus / Grafana）

---

## 10. 项目文档

- [`platform/docs/requirements.md`](./platform/docs/requirements.md) — 需求规格说明书
- [`platform/docs/development.md`](./platform/docs/development.md) — 开发架构与实施指南
- [`platform/docs/agents.md`](./platform/docs/agents.md) — AI 开发助手行为规范
- [`platform/docs/AI_AGENT_INTEGRATION.md`](./platform/docs/AI_AGENT_INTEGRATION.md) — AI Agent 平台集成指南

---

## License

MIT License
