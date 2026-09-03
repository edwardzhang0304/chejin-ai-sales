# 知识管理模块设计 QA

- 日期：2026-09-03
- 审批稿：`http://127.0.0.1:8791/website/ops-admin.html?auth=app&module=knowledge`
- 实现预览：`http://127.0.0.1:5174/?ui-audit=1&module=knowledge`
- 参考视口：1440 × 900
- 窄窗口验证：1024 × 768

## 视觉对照

- 使用同一 1440 × 900 视口将审批稿和实现截图并排比较。
- 保持了现有运营后台的侧栏宽度、工作区间距、颜色、圆角、表格、按钮、抽屉和弹窗通用组件，没有用审批稿的独立尺寸覆盖现有后台规范。
- 导航顺序为：线索管理、车辆管理、知识管理、销售管理、Worker 管理、任务中心、操作日志。
- 主页为三张概览卡、右上角新增知识、自动搜索/状态筛选、无操作列表格和分页；进入模块后不自动打开首条。
- 演示环境和审批稿一致展示 8 条已发布、2 条已归档，只用于视觉验收；生产页面使用后端数据库。

## 交互验证

- 点击表格行打开右侧详情抽屉。
- 详情抽屉可进入编辑；编辑态底部只有“取消 / 发布”。
- 新增弹窗底部只有“取消 / 发布”。
- 发布前会展示自动校验、当前/目标版本和前后差异，再二次确认。
- 编辑或新增有未发布内容时，取消/关闭会要求确认放弃。
- 点击“当前线上版本”整张卡片打开发布记录，再点击版本行打开版本详情和回滚入口。
- 搜索、状态筛选、分页、无结果、加载与错误状态均有对应显示。

## 布局与浏览器结果

- 1440 × 900：无水平溢出，新增按钮、表格和分页完整可见。
- 1024 × 768：无页面水平溢出，360px 通用抽屉和发布弹窗仍可滚动操作。
- 主表格行可用 Enter/Space 打开详情；抽屉和弹窗有明确的可访问名称。
- 浏览器控制台：0 条 error，0 条 warning。

## 通用组件复核

- 首轮实现错误使用了知识管理私有尺寸：概览卡为 `364 × 104`、列表面板为 `1120 × 616`、筛选区为 `1080 × 72`、表格为 `1080 × 350`，与车辆管理不一致。
- 已改为复用运营后台通用组件：`metric-grid`、`management-list-panel`、`two-field-management-filter`、`paginated-management-table-card`、`paginated-management-pagination-row`、`management-drawer` 和通用状态标签。
- 在同一 `1280 × 720` 浏览器视口逐项测量，知识管理与车辆管理现在完全一致：概览卡 `248 × 110`、列表面板 `1028 × 740`、筛选区 `986 × 86`、表格 `986 × 448`，筛选列均为 `754px + 190px`。
- 打开知识详情后，列表区与详情区使用通用 `652px + 360px` 双栏结构，两栏高度均为 `740px`。
- 知识详情与车辆详情现在共用 `standard-management-drawer`、通用标题区、滚动区、信息分组和底部操作栏；标题区 `358 × 59`、滚动区 `358 × 574.7`、操作栏 `358 × 94.3`，逐项一致。
- 知识详情和发布记录抽屉允许滚动链传递：抽屉内容可滚时优先滚动抽屉，到达边界或内容不足时继续滚动外层页面，不再吞掉鼠标滚轮。
- 当前线上版本的长版本号使用卡片内部单行收口，不改变通用卡片高度，也不会溢出卡片。
- 搜索字段已与车辆管理共用 `management-search-field`：标签均为“搜索”，输入类型均为 `search`，尺寸均为 `754 × 38`，边框、圆角、字体和内边距逐项一致。
- 知识抽屉按钮已恢复运营后台通用按钮规范：高度 `34px`、左右内边距 `16px`、按钮间距 `8px`、圆角 `8px`，宽度随文案自适应，不再平分或拉伸整行；浏览器实测“编辑知识”为 `90 × 34px`，“归档”为 `62 × 34px`。

## 证据

- `design-evidence/knowledge-comparison-2880x900.png`
- `design-evidence/knowledge-comparison-all-1440x2250.png`
- `design-evidence/knowledge-implementation-narrow-1024x768.png`
- `design-evidence/knowledge-common-components-1280x720.png`
- `design-evidence/knowledge-common-drawer-1280x720.png`
- `design-evidence/knowledge-shared-search-field-1280x720.png`
- `design-evidence/knowledge-title-only-list-1280x720.png`
- `design-evidence/knowledge-shared-drawer-1280x720.png`
- `design-evidence/knowledge-correct-drawer-actions-1280x720.png`
- `design-evidence/knowledge-common-button-sizing-1280x720.png`
- `design-evidence/knowledge-full-width-metrics-1280x720.png`

## 结论

设计稿的信息架构和主交互已进入真实运营后台，并优先服从现有后台通用组件和位置尺寸规范。

final result: passed

## 真实运行栈复核

- 首次交付后发现 `5173` 新前端连接的仍是旧后端镜像，知识接口返回 404；已重建本机后端并执行 `20260903_0032` 迁移。
- 真实 PostgreSQL 首次初始化暴露版本指针早于版本记录写入的外键顺序问题；已改为显式按“知识条目与版本 → 修订 → 当前版本指针”顺序 flush，仍保持单事务提交。
- PostgreSQL 已成功建立 `KR-20260903-01`，载入 8 条正式知识。
- 已在真实 PostgreSQL 完成一次“预览 → 发布 → flush → 整体 rollback”门禁，临时知识残留数量为 0。
