from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class UiContractTest(unittest.TestCase):
    def test_component_workbench_is_the_default_ui_entry(self):
        text = (ROOT / "chejin_worker_client" / "main.py").read_text(encoding="utf-8")

        self.assertIn("from .web_ui import run_app", text)
        self.assertIn("from .ui import run_app", text)
        self.assertIn("CHEJIN_WORKER_UI_MODE", text)
        self.assertNotIn("--ui", text)
        self.assertNotIn("tk_ui", text)

    def test_legacy_tk_demo_ui_is_deleted(self):
        self.assertFalse((ROOT / "chejin_worker_client" / "tk_ui.py").exists())

    def test_formal_worker_workbench_copy_is_present(self):
        text = (ROOT / "chejin_worker_client" / "ui.py").read_text(encoding="utf-8")

        self.assertIn("本机执行日志明细", text)
        self.assertIn("Worker 工作台", text)
        self.assertIn("该结果不代表客户已同意好友申请", text)

    def test_settings_page_matches_design_scope(self):
        text = (ROOT / "chejin_worker_client" / "ui.py").read_text(encoding="utf-8")

        self.assertIn("自动接单时段", text)
        self.assertIn("本机执行日志", text)
        self.assertIn("客户端版本号", text)
        self.assertIn("设置 / 接单时段设置", text)
        self.assertIn("自动接单", text)
        self.assertIn("接单时间", text)
        self.assertIn("开始", text)
        self.assertIn("结束", text)
        self.assertNotIn("RPA 模式", text)
        self.assertNotIn("OmniAuto", text)
        self.assertNotIn("重新绑定 Worker", text)

    def test_workbench_matches_static_design_shell(self):
        text = (ROOT / "chejin_worker_client" / "ui.py").read_text(encoding="utf-8")

        self.assertIn("WINDOW_WIDTH = 316", text)
        self.assertIn("WINDOW_HEIGHT = 679", text)
        self.assertIn("PAGE_MARGIN = 12", text)
        self.assertIn("CONTENT_WIDTH = 290", text)
        self.assertIn("TASK_SUMMARY_SIZE = QSize(CONTENT_WIDTH, 110)", text)
        self.assertIn("TIMELINE_CARD_SIZE = QSize(CONTENT_WIDTH, 370)", text)
        self.assertIn("CHAIN_VIEWPORT_SIZE = QSize(266, 346)", text)
        self.assertIn("DOCK_SIZE = QSize(CONTENT_WIDTH, 50)", text)
        self.assertIn("self.setMinimumSize(WINDOW_WIDTH, WINDOW_HEIGHT)", text)
        self.assertIn("self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)", text)
        self.assertIn("self.run_button.hide()", text)
        self.assertIn("self._sync_workbench_sections", text)
        self.assertIn("order = (self.task_summary, self.timeline_card)", text)
        self.assertIn("order = (self.status_panel, self.empty_card)", text)
        self.assertIn("self.task_summary.setFixedSize(TASK_SUMMARY_SIZE)", text)
        self.assertIn("self.timeline_card.setFixedSize(TIMELINE_CARD_SIZE)", text)
        self.assertIn("self.steps_area.setFixedSize(CHAIN_VIEWPORT_SIZE)", text)
        self.assertIn("self.workbench_layout.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, 0)", text)
        self.assertIn("QLabel {\n                background: transparent;", text)
        self.assertIn('self.status_panel.setObjectName("statusGrid")', text)
        self.assertIn('self.status_top_row.setObjectName("statusTopRow")', text)
        self.assertIn('self.status_bottom_row.setObjectName("statusBottomRow")', text)
        self.assertIn('tile_role="topLeft"', text)
        self.assertIn('tile_role="topRight"', text)
        self.assertIn("QFrame#statusGrid", text)
        self.assertIn("QFrame#statusTopRow", text)
        self.assertIn("QLabel#tileValueHero", text)
        self.assertIn("QLabel#tileValueHeroCompact", text)
        self.assertIn('return value[:6] + "…"', text)
        self.assertIn("self.value.setMaximumWidth(106 if value_role == \"hero\" else 120)", text)
        self.assertIn("QLabel#statusChip-accepting", text)
        self.assertIn("QLabel#statusPill-ok", text)
        self.assertIn('self.label.setObjectName("tileLabelStatus" if value_role == "pill" else "tileLabel")', text)
        self.assertIn("QLabel#tileLabelStatus", text)
        self.assertIn("EMPTY_CARD_SIZE = QSize(CONTENT_WIDTH, 200)", text)
        self.assertIn("card.setFixedSize(EMPTY_CARD_SIZE)", text)
        self.assertNotIn("tileLabelPill", text)
        self.assertNotIn("self.status_dot.setFixedSize(10, 10)", text)
        self.assertIn("value_row = QHBoxLayout()", text)
        self.assertIn('if value_role == "pill":', text)
        self.assertIn("self.label.setMinimumWidth(50)", text)
        self.assertIn('self.value.setMinimumWidth(42 if value_role == "pill" else 0)', text)
        self.assertIn("layout = QHBoxLayout(self)", text)
        self.assertIn("self.label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)", text)
        self.assertIn("status_bottom_layout.setSpacing(0)", text)
        self.assertNotIn("QGridLayout(self.status_panel)", text)
        self.assertNotIn("border-right: 1px solid #e8edf3", text)
        self.assertIn("self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)", text)
        self.assertIn("QSizePolicy.Policy.Ignored", text)
        self.assertIn("checkpoint_center_y = row_top + 17", text)

    def test_bottom_dock_matches_static_design_action_bar(self):
        text = (ROOT / "chejin_worker_client" / "ui.py").read_text(encoding="utf-8")

        self.assertIn('self.dock_shell.setObjectName("dockShell")', text)
        self.assertIn('self.dock.setObjectName("dockAction")', text)
        self.assertIn("self.dock_shell.setFixedHeight(DOCK_SHELL_HEIGHT)", text)
        self.assertIn("self.dock.setFixedSize(DOCK_SIZE)", text)
        self.assertIn("dock_shell_layout.setContentsMargins(12, 12, 12, 12)", text)
        self.assertIn("QFrame#dockShell", text)
        self.assertIn("QFrame#dockAction", text)
        self.assertIn("border-bottom-left-radius: 10px;", text)
        self.assertIn("border-bottom-right-radius: 10px;", text)
        self.assertIn("min-height: 50px;", text)
        self.assertIn("background: #f6f7f9;", text)
        self.assertIn("border: 1px solid #e8edf3;", text)
        self.assertIn("border-radius: 10px;", text)

    def test_all_static_design_pages_have_matching_pyside_structure(self):
        text = (ROOT / "chejin_worker_client" / "ui.py").read_text(encoding="utf-8")

        self.assertIn("def _page_layout(", text)
        self.assertIn("def _empty_state_card(", text)
        self.assertIn("layout = self._page_layout(self.settings_page, spacing=14)", text)
        self.assertIn("layout = self._page_layout(self.schedule_settings_page, spacing=14)", text)
        self.assertIn("layout = self._page_layout(self.logs_page, spacing=14)", text)
        self.assertIn("self.empty_card, self.empty_title, self.empty_text = self._empty_state_card()", text)
        self.assertIn("输入后台生成的 Worker ID 和 Token。", text)
        self.assertIn('worker_id_label.setObjectName("formLabel")', text)
        self.assertIn('self.empty_card.setProperty("emptyState", "waiting"', text)
        self.assertIn('settings_list.setObjectName("scheduleDetail")', text)
        self.assertIn("SETTINGS_HEAD_HEIGHT = 50", text)
        self.assertIn("SETTINGS_ROW_HEIGHT = 61", text)
        self.assertIn("header.setFixedHeight(SETTINGS_HEAD_HEIGHT)", text)
        self.assertIn("row.setFixedHeight(SETTINGS_ROW_HEIGHT)", text)
        self.assertIn("settings_list.setFixedWidth(CONTENT_WIDTH)", text)
        self.assertIn("QLabel#settingsTitle", text)
        self.assertIn("QLabel#settingsSubtitle", text)
        self.assertIn('toggle_row.setObjectName("scheduleToggleRow")', text)
        self.assertIn('time_row.setObjectName("scheduleTimeRow")', text)
        self.assertIn('self.auto_accept_checkbox = QPushButton("关闭")', text)
        self.assertIn("ICON_CHEVRON_RIGHT_PATHS", text)
        self.assertIn('self.logs_table.setObjectName("logTable")', text)
        self.assertIn("V14 · Worker 组件化客户端", text)
        self.assertNotIn("首次使用", text)
        self.assertNotIn("QCheckBox", text)

    def test_v16_component_ui_assets_are_packaged(self):
        web_ui = (ROOT / "chejin_worker_client" / "web_ui.py").read_text(encoding="utf-8")
        app_js = ROOT / "chejin_worker_client" / "web_assets" / "worker-web-app.js"

        self.assertIn("QWebEngineView", web_ui)
        self.assertIn("QWebChannel", web_ui)
        self.assertIn("CLIENT_VERSION = \"V16.111 · Worker C2/C3 客户端\"", web_ui)
        self.assertFalse((ROOT / "web-ui-src").exists())
        self.assertTrue((ROOT / "chejin_worker_client" / "web_assets" / "index.html").exists())
        self.assertTrue((ROOT / "chejin_worker_client" / "web_assets" / "worker-ui.css").exists())
        self.assertTrue((ROOT / "chejin_worker_client" / "web_assets" / "worker-ui.tokens.css").exists())
        self.assertTrue(app_js.exists())

    def test_window_shell_matches_static_design_chrome(self):
        text = (ROOT / "chejin_worker_client" / "ui.py").read_text(encoding="utf-8")

        self.assertIn("FramelessWindowHint", text)
        self.assertIn("WA_TranslucentBackground", text)
        self.assertIn('setObjectName("appWindow")', text)
        self.assertIn('setObjectName("titlebar")', text)
        self.assertIn('setObjectName("titlebarBrand")', text)
        self.assertIn('setObjectName("brandMark")', text)
        self.assertIn('setObjectName("brandText")', text)
        self.assertIn('setObjectName("windowControls")', text)
        self.assertIn("ICON_WINDOW_MINIMIZE_PATHS", text)
        self.assertIn("ICON_WINDOW_MAXIMIZE_PATHS", text)
        self.assertIn("ICON_WINDOW_CLOSE_PATHS", text)
        self.assertIn('border-radius: 10px;', text)
        self.assertIn("self._titlebar_mouse_press", text)
        self.assertIn("self._toggle_maximized", text)

    def test_header_icons_use_static_design_svg_paths(self):
        text = (ROOT / "chejin_worker_client" / "ui.py").read_text(encoding="utf-8")

        self.assertIn("QSvgRenderer", text)
        self.assertIn('font-family: -apple-system, "SF Pro Text", "PingFang SC", "Microsoft YaHei UI", system-ui, sans-serif;', text)
        self.assertIn("font-size: 14px;", text)
        self.assertIn("ICON_BACK_PATHS", text)
        self.assertIn("ICON_SETTINGS_PATHS", text)
        self.assertIn('d="m12 19-7-7 7-7"', text)
        self.assertIn('d="M19 12H5"', text)
        self.assertIn("M9.671 4.136", text)
        self.assertIn('<circle cx="12" cy="12" r="3"></circle>', text)
        self.assertIn('self.back_button = _icon_button("clientMenu"', text)
        self.assertIn('self.settings_button = _icon_button("clientSettings"', text)
        self.assertNotIn('QPushButton("⚙")', text)
        self.assertNotIn('QPushButton("‹")', text)

    def test_task_timeline_has_visual_evidence_preview(self):
        text = (ROOT / "chejin_worker_client" / "ui.py").read_text(encoding="utf-8")

        self.assertIn('StepState = Literal["done", "current", "error", "final"]', text)
        self.assertIn("CHAIN_BOTTOM_FOCUS_PADDING = 170", text)
        self.assertIn("self.steps_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)", text)
        self.assertIn("self.steps_layout.setContentsMargins(0, 8, 0, CHAIN_BOTTOM_FOCUS_PADDING)", text)
        self.assertIn("self.steps_layout.setSpacing(11)", text)
        self.assertIn('self.step_history.append((title, remark, "final", result.evidence_path))', text)
        self.assertIn("self._focused_step_widget = row", text)
        self.assertIn("QTimer.singleShot(0, self._center_focused_step)", text)
        self.assertIn("def _center_focused_step", text)
        self.assertNotIn("self.steps_layout.addStretch()", text)
        self.assertIn("QPixmap", text)
        self.assertIn("stepShot", text)
        self.assertIn("class StepScreenshot", text)
        self.assertIn('setObjectName("shotWindow")', text)
        self.assertIn("shot.setFixedSize(237, 88)", text)
        self.assertIn("IMAGE_SUFFIXES", text)
        self.assertIn("class EvidencePreview", text)
        self.assertIn("QDialog", text)
        self.assertIn("点击查看大图", text)
        self.assertNotIn("打开证据", text)
        self.assertNotIn("QDesktopServices", text)

    def test_scrollbar_uses_design_style(self):
        text = (ROOT / "chejin_worker_client" / "ui.py").read_text(encoding="utf-8")

        self.assertIn("QScrollBar:vertical", text)
        self.assertIn("QScrollBar::handle:vertical", text)
        self.assertIn("border-radius: 3px", text)
        self.assertIn("QScrollBar::add-line:vertical", text)
        self.assertIn("QScrollBar::sub-line:vertical", text)
        self.assertIn("padding-right: 2px;", text)

    def test_status_dots_have_breathing_animation(self):
        text = (ROOT / "chejin_worker_client" / "ui.py").read_text(encoding="utf-8")

        self.assertIn("class BreathingStatusDot", text)
        self.assertIn('setObjectName("breathingStatusDot")', text)
        self.assertIn("self._timer = QTimer(self)", text)
        self.assertIn("self._timer.timeout.connect(self._tick)", text)
        self.assertIn("def paintEvent", text)
        self.assertIn("halo.setAlpha", text)
        self.assertIn("self.status_dot.set_kind(kind)", text)
        self.assertNotIn('"● 可用"', text)
        self.assertNotIn('"● 不可用"', text)
        self.assertNotIn('"● 已连接"', text)
        self.assertNotIn('"● 未检测到"', text)


if __name__ == "__main__":
    unittest.main()
