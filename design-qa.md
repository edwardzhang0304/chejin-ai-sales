# Worker 当前运行过程设计 QA

- source visual truth path: `/var/folders/dz/myvzpynj777g46pgwjst4zr00000gn/T/codex-clipboard-66506e88-e602-4dad-af4b-2d630def29d3.png`
- implementation screenshot path: `/private/tmp/chejin-worker-ui-qa/final-full.png`
- focused comparison path: `/private/tmp/chejin-worker-ui-qa/comparison-final.png`
- viewport: 1280 x 1236 CSS px，device scale factor 1
- source pixels: 86 x 350；按原始像素展示
- implementation pixels: 1280 x 1236；Worker 窗口为 316 x 628 CSS px，时间线可见区为 266 x 280 CSS px
- state: `target-read-running`，同一客户动态链路已进入“服务端正在判断处理方式”，滚动区位于底部

## Full-view comparison evidence

- Worker 窗口保持原有 316 x 628 紧凑布局，顶部状态、当前运行过程和底部接单按钮均无裁切。
- 同一客户从读取、语音、图片、回传到服务端判断持续累积，未出现定向读取与 AI 回复的视觉断屏。
- 完整页面截图中未见溢出、重叠、文字截断或持久控件丢失。

## Focused region comparison evidence

- 附件参考与实现滑块已合并到同一张对比图：`/private/tmp/chejin-worker-ui-qa/comparison-final.png`。
- 实现滑块为 6 px 可见宽度、全圆角、半透明中性灰，轨道透明；高度按可见区与内容比例动态计算。
- 实现继承原产品浅色设计系统，没有照搬参考图深色背景；这是符合现有设计 token 的有意差异。

## Required fidelity surfaces

- Fonts and typography: 沿用 `-apple-system / SF Pro Text / PingFang SC / system-ui`，节点标题字重、行高和原 Worker UI 一致，无新字体或错误换行。
- Spacing and layout rhythm: 时间线右侧为滑块预留 12 px，节点内容不被覆盖；窗口尺寸、卡片间距和底部按钮位置未变。
- Colors and visual tokens: 滑块使用 `rgba(105, 111, 132, 0.62)`，hover/focus 使用更深的 `rgba(82, 88, 108, 0.78)`，与现有蓝灰体系协调。
- Image quality and asset fidelity: 本次无新图片资产；用户附件只作形态参考，未被拉伸或写入产品资产。
- Copy and content: 节点文案按业务顺序收口为“发现待处理客户→定位→读取→媒体→回传→服务端判断→AI 发送”，媒体节点只在实际发生时展示。

## Interaction and runtime checks

- 键盘 `End` 可将滚动区从顶部移动到 `scrollTop=205 / max=205`。
- 拖动自绘滑块可将滚动位置从 205 移到 78，滑块尺寸与位置随滚动同步。
- 滚轮与原生键盘滚动仍由可聚焦的真实容器提供。
- Browser console errors: 0。

## Comparison history

1. First pass finding: `[P2]` 只使用原生 CSS scrollbar 时，macOS 预览会自动隐藏滑块，无法稳定呈现附件中的细长灰色形态。
2. Fix: 替换为跨平台自绘滑块，动态计算长度与位置，并支持拖动；原生容器继续承担滚轮、触控板和键盘操作。
3. Post-fix evidence: `/private/tmp/chejin-worker-ui-qa/final-full.png` 和 `/private/tmp/chejin-worker-ui-qa/comparison-final.png`，可见滑块稳定显示，无新 P0/P1/P2。

## Findings

- 无剩余 P0/P1/P2。

## Follow-up polish

- P3：可在 Windows 真实 Qt WebEngine 上再核对鼠标指针形态；不影响本次灰度 UI 功能验收。

final result: passed
