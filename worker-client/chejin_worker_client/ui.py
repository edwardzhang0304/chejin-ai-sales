from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from PySide6.QtCore import QByteArray, QPoint, QSize, QTime, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from .api import WorkerApiClient
from .models import Binding, RpaResult, RpaStep, Task, WorkerProfile, task_type_title
from .qt_application import GuardedQApplication
from .rpa_bridge import RpaBridge
from .storage import append_log, clear_binding, load_binding, new_client_instance_id, read_logs, save_binding
from .task_runner import TaskRunner


PageName = Literal["bind", "workbench", "settings", "schedule_settings", "logs"]
StepState = Literal["done", "current", "error", "final"]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
ICON_BACK_PATHS = '<path d="m12 19-7-7 7-7"></path><path d="M19 12H5"></path>'
ICON_SETTINGS_PATHS = (
    '<path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 '
    '2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 '
    '4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 '
    '0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 '
    '2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"></path>'
    '<circle cx="12" cy="12" r="3"></circle>'
)
ICON_WINDOW_MINIMIZE_PATHS = '<path d="M5 12h14"></path>'
ICON_WINDOW_MAXIMIZE_PATHS = '<path d="M5 5h14v14H5z"></path>'
ICON_WINDOW_CLOSE_PATHS = '<path d="M18 6 6 18"></path><path d="m6 6 12 12"></path>'
ICON_CHEVRON_RIGHT_PATHS = '<path d="m9 18 6-6-6-6"></path>'

WINDOW_WIDTH = 316
WINDOW_HEIGHT = 679
PAGE_MARGIN = 12
PAGE_GAP = 10
CONTENT_WIDTH = 290
WORKSPACE_HEAD_HEIGHT = 24
SETTINGS_HEAD_HEIGHT = 50
SETTINGS_ROW_HEIGHT = 61
EMPTY_CARD_SIZE = QSize(CONTENT_WIDTH, 200)
TASK_SUMMARY_SIZE = QSize(CONTENT_WIDTH, 110)
TIMELINE_CARD_SIZE = QSize(CONTENT_WIDTH, 370)
CHAIN_VIEWPORT_SIZE = QSize(266, 346)
CHAIN_BOTTOM_FOCUS_PADDING = 170
DOCK_SIZE = QSize(CONTENT_WIDTH, 50)
DOCK_SHELL_HEIGHT = 74


def _short_task_id(value: str | None) -> str:
    if not value:
        return "暂无"
    if len(value) <= 10:
        return value
    return value[:8]


def _format_time(value: str | None) -> str:
    if not value:
        return "刚刚"
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return dt.strftime("%H:%M:%S")
    except ValueError:
        return value[-8:] if len(value) >= 8 else value


def _icon_from_svg(paths: str, *, size: int = 18, color: str = "#68717d") -> QIcon:
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        f'<g fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{paths}</g>'
        f"</svg>"
    )
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


def _icon_button(
    object_name: str,
    paths: str,
    accessible_name: str,
    *,
    button_size: int = 32,
    icon_size: int = 18,
) -> QPushButton:
    button = QPushButton("")
    button.setObjectName(object_name)
    button.setAccessibleName(accessible_name)
    button.setIcon(_icon_from_svg(paths))
    button.setIconSize(QSize(icon_size, icon_size))
    button.setFixedSize(button_size, button_size)
    return button


class BreathingStatusDot(QWidget):
    COLOR_BY_KIND = {
        "ok": "#33845a",
        "accepting": "#33845a",
        "danger": "#b85c4f",
    }

    def __init__(self, kind: str = "plain") -> None:
        super().__init__()
        self.setObjectName("breathingStatusDot")
        self.setFixedSize(12, 12)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._phase = 0
        self._direction = 1
        self._color = QColor(self.COLOR_BY_KIND["ok"])
        self._timer = QTimer(self)
        self._timer.setInterval(72)
        self._timer.timeout.connect(self._tick)
        self.set_kind(kind)

    def set_kind(self, kind: str) -> None:
        color = self.COLOR_BY_KIND.get(kind)
        if not color:
            self.hide()
            self._timer.stop()
            return
        self._color = QColor(color)
        self.show()
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def _tick(self) -> None:
        self._phase += self._direction
        if self._phase >= 10:
            self._phase = 10
            self._direction = -1
        elif self._phase <= 0:
            self._phase = 0
            self._direction = 1
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        center = self.rect().center()

        halo = QColor(self._color)
        halo.setAlpha(34 + self._phase * 8)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(halo)
        halo_radius = 3.4 + self._phase * 0.28
        painter.drawEllipse(center, halo_radius, halo_radius)

        core = QColor(self._color)
        core.setAlpha(230)
        painter.setBrush(core)
        painter.drawEllipse(center, 3.0, 3.0)
        painter.end()


class StatusTile(QFrame):
    def __init__(
        self,
        label: str,
        value: str = "-",
        *,
        kind: str = "plain",
        tile_role: str = "bottom",
        value_role: str = "plain",
        show_label: bool = True,
        show_dot: bool = True,
    ) -> None:
        super().__init__()
        self.setObjectName("statusTile")
        self.setProperty("tileRole", tile_role)
        self.value_role = value_role
        self.show_dot = show_dot
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(58 if tile_role.startswith("top") else 64)
        self.setMaximumHeight(58 if tile_role.startswith("top") else 64)
        self.label = QLabel(label)
        self.label.setObjectName("tileLabelStatus" if value_role == "pill" else "tileLabel")
        self.label.setSizePolicy(
            QSizePolicy.Policy.Preferred if value_role == "pill" else QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        if value_role == "pill":
            self.label.setMinimumWidth(50)
        self.label.setVisible(show_label)
        value_row = QHBoxLayout()
        value_row.setContentsMargins(0, 0, 0, 0)
        value_row.setSpacing(5 if value_role != "pill" else 4)
        self.status_dot = BreathingStatusDot(kind)
        self.value = QLabel(self._display_value(value))
        self.value.setObjectName(self._value_object_name(kind))
        self.value.setSizePolicy(
            QSizePolicy.Policy.Preferred if value_role == "pill" else QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        self.value.setMinimumWidth(42 if value_role == "pill" else 0)
        self.value.setMaximumWidth(106 if value_role == "hero" else 120)
        self.value.setToolTip(value)
        self.value.setWordWrap(False)
        self.status_dot.setVisible(show_dot)
        if value_role == "pill":
            layout = QHBoxLayout(self)
            layout.setContentsMargins(9, 12, 9, 12)
            layout.setSpacing(6)
            self.label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            value_row.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
            value_row.addWidget(self.value, 0, Qt.AlignmentFlag.AlignVCenter)
            layout.addStretch(1)
            layout.addWidget(self.label, 0, Qt.AlignmentFlag.AlignVCenter)
            layout.addLayout(value_row)
            layout.addStretch(1)
            return

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18 if tile_role.startswith("top") else 9, 10 if tile_role.startswith("top") else 11, 18 if tile_role.startswith("top") else 9, 10 if tile_role.startswith("top") else 11)
        layout.setSpacing(6 if tile_role.startswith("top") else 5)
        if value_role == "chip" and tile_role.endswith("Right"):
            value_row.addStretch()
        value_row.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        value_row.addWidget(self.value, 0 if value_role in {"chip", "pill"} else 1, Qt.AlignmentFlag.AlignVCenter)
        if value_role == "chip" and not tile_role.endswith("Right"):
            value_row.addStretch()
        layout.addWidget(self.label)
        layout.addLayout(value_row)

    def set_value(self, value: str, *, kind: str = "plain") -> None:
        display_value = self._display_value(value)
        self.value.setText(display_value)
        self.value.setToolTip(value)
        self.value.setObjectName(self._value_object_name(kind))
        self.status_dot.set_kind(kind)
        self.status_dot.setVisible(self.show_dot)
        self.value.style().unpolish(self.value)
        self.value.style().polish(self.value)

    def _value_object_name(self, kind: str) -> str:
        if self.value_role == "hero":
            text = self.value.text() if hasattr(self, "value") else ""
            return "tileValueHeroCompact" if len(text) > 4 else "tileValueHero"
        if self.value_role == "chip":
            return f"statusChip-{kind}"
        if self.value_role == "pill":
            return f"statusPill-{kind}"
        return f"tileValue-{kind}"

    def _display_value(self, value: str) -> str:
        if self.value_role != "hero" or len(value) <= 6:
            return value
        return value[:6] + "…"


class StepRow(QFrame):
    def __init__(self, title: str, remark: str, *, state: StepState = "done", evidence_path: str | None = None) -> None:
        super().__init__()
        self.setObjectName(f"stepRow-{state}")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        if state in {"current", "final", "error"}:
            layout.setContentsMargins(7, 8, 7, 8)
        else:
            layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)
        icon = QLabel({"done": "✓", "final": "✓", "current": "⌕", "error": "×"}.get(state, "•"))
        icon.setObjectName(f"stepIcon-{state}")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedSize(18, 18)
        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("stepTitle")
        title_label.setWordWrap(True)
        remark_label = QLabel(remark or "已记录执行步骤。")
        remark_label.setObjectName("stepRemark")
        remark_label.setWordWrap(True)
        text_box.addWidget(title_label)
        text_box.addWidget(remark_label)
        if evidence_path:
            self._add_evidence_preview(text_box, evidence_path)
        elif state == "current":
            text_box.addWidget(StepScreenshot("13812346678", "正在搜索"))
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(text_box, 1)

    def _add_evidence_preview(self, layout: QVBoxLayout, evidence_path: str) -> None:
        path = Path(evidence_path)
        if path.suffix.lower() not in IMAGE_SUFFIXES or not path.exists():
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return
        preview = EvidencePreview(path, pixmap)
        preview.setObjectName("stepShot")
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setMinimumHeight(78)
        preview.setPixmap(pixmap.scaled(220, 124, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(preview)


class StepScreenshot(QFrame):
    def __init__(self, search_text: str, result_text: str) -> None:
        super().__init__()
        self.setObjectName("stepScreenshot")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 9, 0, 0)
        layout.setSpacing(5)
        shot = QFrame()
        shot.setObjectName("shotWindow")
        shot.setFixedSize(237, 88)
        shot_layout = QVBoxLayout(shot)
        shot_layout.setContentsMargins(84, 34, 12, 8)
        shot_layout.setSpacing(5)
        search = QLabel(search_text)
        search.setObjectName("shotSearch")
        search.setMinimumHeight(24)
        search.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        empty = QLabel(result_text)
        empty.setObjectName("shotEmpty")
        shot_layout.addWidget(search)
        shot_layout.addWidget(empty)
        caption = QLabel("当前截图 · 微信搜索结果")
        caption.setObjectName("shotCaption")
        layout.addWidget(shot)
        layout.addWidget(caption)


class EvidencePreview(QLabel):
    def __init__(self, path: Path, pixmap: QPixmap) -> None:
        super().__init__()
        self.path = path
        self.full_pixmap = pixmap
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("点击查看大图")

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        dialog = QDialog(self)
        dialog.setObjectName("evidenceDialog")
        dialog.setWindowTitle("截图预览")
        dialog.setModal(True)
        dialog.resize(760, 560)
        shell = QVBoxLayout(dialog)
        shell.setContentsMargins(16, 16, 16, 16)
        shell.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("截图预览")
        title.setObjectName("dialogTitle")
        close_button = _icon_button(
            "dialogClose",
            ICON_WINDOW_CLOSE_PATHS,
            "关闭截图预览",
            button_size=30,
            icon_size=16,
        )
        close_button.clicked.connect(dialog.accept)
        header.addWidget(title, 1)
        header.addWidget(close_button, 0, Qt.AlignmentFlag.AlignRight)
        shell.addLayout(header)

        image = QLabel()
        image.setObjectName("dialogShot")
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setPixmap(
            self.full_pixmap.scaled(
                720,
                480,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        shell.addWidget(image, 1)
        dialog.exec()


class WorkerWindow(QMainWindow):
    profile_signal = Signal(object)
    status_signal = Signal(str)
    task_signal = Signal(object)
    step_signal = Signal(object)
    result_signal = Signal(object)
    error_signal = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("车金 Worker 客户端")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)

        self.api = WorkerApiClient()
        self.bridge = RpaBridge()
        self.binding = load_binding()
        self.profile: WorkerProfile | None = None
        self.current_task: Task | None = None
        self.last_task: Task | None = None
        self.last_result: RpaResult | None = None
        self.connection_status = "connecting"
        self.step_history: list[tuple[str, str, StepState, str | None]] = []
        self._drag_offset: QPoint | None = None
        self._focused_step_widget: StepRow | None = None

        self.runner = TaskRunner(
            self.api,
            self.bridge,
            on_profile=lambda value: self.profile_signal.emit(value),
            on_status=lambda value: self.status_signal.emit(value),
            on_task=lambda value: self.task_signal.emit(value),
            on_step=lambda value: self.step_signal.emit(value),
            on_result=lambda value: self.result_signal.emit(value),
            on_error=lambda value: self.error_signal.emit(value),
        )

        self.stack = QStackedWidget()
        self._build_root()
        self._build_bind_page()
        self._build_workbench_page()
        self._build_settings_page()
        self._build_schedule_settings_page()
        self._build_logs_page()
        self._wire_signals()
        self._apply_style()

        if self.binding:
            self.show_page("workbench")
            self.runner.start(self.binding)
        else:
            self.show_page("bind")
        self.refresh_view()

    def _build_root(self) -> None:
        root = QWidget()
        root.setObjectName("windowRoot")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.app_window = QFrame()
        self.app_window.setObjectName("appWindow")
        app_layout = QVBoxLayout(self.app_window)
        app_layout.setContentsMargins(0, 0, 0, 0)
        app_layout.setSpacing(0)

        titlebar = QFrame()
        titlebar.setObjectName("titlebar")
        titlebar.setMouseTracking(True)
        titlebar.mousePressEvent = self._titlebar_mouse_press  # type: ignore[method-assign]
        titlebar.mouseMoveEvent = self._titlebar_mouse_move  # type: ignore[method-assign]
        titlebar.mouseReleaseEvent = self._titlebar_mouse_release  # type: ignore[method-assign]
        title_layout = QHBoxLayout(titlebar)
        title_layout.setContentsMargins(8, 0, 0, 0)
        title_layout.setSpacing(0)
        brand = QFrame()
        brand.setObjectName("titlebarBrand")
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(7)
        brand_mark = QLabel("车")
        brand_mark.setObjectName("brandMark")
        brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_mark.setFixedSize(18, 18)
        brand_text = QLabel("车金 Worker 客户端")
        brand_text.setObjectName("brandText")
        brand_layout.addWidget(brand_mark)
        brand_layout.addWidget(brand_text)
        brand_layout.addStretch()
        controls = QFrame()
        controls.setObjectName("windowControls")
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(0)
        self.minimize_button = _icon_button("windowControl", ICON_WINDOW_MINIMIZE_PATHS, "最小化", button_size=30, icon_size=15)
        self.maximize_button = _icon_button("windowControl", ICON_WINDOW_MAXIMIZE_PATHS, "最大化", button_size=30, icon_size=15)
        self.close_button = _icon_button("windowControlClose", ICON_WINDOW_CLOSE_PATHS, "关闭", button_size=30, icon_size=15)
        self.minimize_button.clicked.connect(self.showMinimized)
        self.maximize_button.clicked.connect(self._toggle_maximized)
        self.close_button.clicked.connect(self.close)
        controls_layout.addWidget(self.minimize_button)
        controls_layout.addWidget(self.maximize_button)
        controls_layout.addWidget(self.close_button)
        title_layout.addWidget(brand, 1)
        title_layout.addWidget(controls)

        app_body = QFrame()
        app_body.setObjectName("appBody")
        body_layout = QVBoxLayout(app_body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        nav = QFrame()
        nav.setObjectName("clientBar")
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(8, 0, 8, 0)
        nav_layout.setSpacing(6)
        self.back_button = _icon_button("clientMenu", ICON_BACK_PATHS, "返回")
        self.back_button.clicked.connect(self.handle_back)
        self.client_title = QLabel("Worker 工作台")
        self.client_title.setObjectName("clientTitle")
        self.client_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.settings_button = _icon_button("clientSettings", ICON_SETTINGS_PATHS, "打开设置")
        self.settings_button.clicked.connect(lambda: self.show_page("settings"))
        nav_layout.addWidget(self.back_button)
        nav_layout.addWidget(self.client_title, 1)
        nav_layout.addWidget(self.settings_button)

        body_layout.addWidget(nav)
        body_layout.addWidget(self.stack, 1)
        app_layout.addWidget(titlebar)
        app_layout.addWidget(app_body, 1)
        layout.addWidget(self.app_window, 1)

    def _titlebar_mouse_press(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self.isMaximized():
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def _titlebar_mouse_move(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def _titlebar_mouse_release(self, event) -> None:
        self._drag_offset = None
        event.accept()

    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
            self.app_window.setObjectName("appWindow")
        else:
            self.showMaximized()
            self.app_window.setObjectName("appWindowMaximized")
        self.app_window.style().unpolish(self.app_window)
        self.app_window.style().polish(self.app_window)

    def _wire_signals(self) -> None:
        self.profile_signal.connect(self.on_profile)
        self.status_signal.connect(self.on_connection_status)
        self.task_signal.connect(self.on_task)
        self.step_signal.connect(self.on_step)
        self.result_signal.connect(self.on_result)
        self.error_signal.connect(self.on_error)

    def _page_layout(
        self,
        page: QWidget,
        *,
        top: int = PAGE_MARGIN,
        bottom: int = PAGE_MARGIN,
        spacing: int = PAGE_GAP,
    ) -> QVBoxLayout:
        layout = QVBoxLayout(page)
        layout.setContentsMargins(PAGE_MARGIN, top, PAGE_MARGIN, bottom)
        layout.setSpacing(spacing)
        return layout

    def _empty_state_card(self) -> tuple[QFrame, QLabel, QLabel]:
        card = QFrame()
        card.setObjectName("emptyCard")
        card.setFixedSize(EMPTY_CARD_SIZE)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(6)
        title = QLabel("暂无可领取任务")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text = QLabel("暂停接单后不会领取新的任务。")
        text.setObjectName("helperText")
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text.setWordWrap(True)
        layout.addStretch()
        layout.addWidget(title)
        layout.addWidget(text)
        layout.addStretch()
        return card, title, text

    def _build_bind_page(self) -> None:
        self.bind_page = QWidget()
        layout = self._page_layout(self.bind_page, top=34, spacing=0)

        form = QFrame()
        form.setObjectName("formPanel")
        form_layout = QVBoxLayout(form)
        form_layout.setContentsMargins(16, 16, 16, 16)
        form_layout.setSpacing(12)
        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(4)
        title = QLabel("绑定本机 Worker")
        title.setObjectName("pageTitle")
        subtitle = QLabel("输入后台生成的 Worker ID 和 Token。")
        subtitle.setObjectName("helperText")
        subtitle.setWordWrap(True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        self.worker_id_input = QLineEdit()
        self.worker_id_input.setPlaceholderText("Worker ID")
        self.worker_token_input = QLineEdit()
        self.worker_token_input.setPlaceholderText("Worker Token")
        self.worker_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        bind_button = QPushButton("绑定 Worker")
        bind_button.setObjectName("primary")
        bind_button.clicked.connect(self.bind_worker)
        self.bind_error = QLabel("")
        self.bind_error.setObjectName("errorText")
        self.bind_error.setWordWrap(True)
        worker_id_label = QLabel("Worker ID")
        worker_id_label.setObjectName("formLabel")
        token_label = QLabel("Worker Token")
        token_label.setObjectName("formLabel")
        form_layout.addLayout(title_box)
        form_layout.addWidget(worker_id_label)
        form_layout.addWidget(self.worker_id_input)
        form_layout.addWidget(token_label)
        form_layout.addWidget(self.worker_token_input)
        form_layout.addWidget(bind_button)
        form_layout.addWidget(self.bind_error)

        layout.addWidget(form)
        layout.addStretch()
        self.stack.addWidget(self.bind_page)

    def _build_workbench_page(self) -> None:
        self.workbench_page = QWidget()
        page_layout = QVBoxLayout(self.workbench_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName("pageScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_content = QWidget()
        self.workbench_layout = QVBoxLayout(scroll_content)
        self.workbench_layout.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, 24)
        self.workbench_layout.setSpacing(PAGE_GAP)
        scroll.setWidget(scroll_content)
        self.page_scroll = scroll

        header = QFrame()
        header.setObjectName("workspaceHead")
        header.setFixedHeight(WORKSPACE_HEAD_HEIGHT)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 7)
        header_layout.setSpacing(8)
        header_text = QVBoxLayout()
        header_text.setSpacing(1)
        self.headline_label = QLabel("")
        self.headline_label.hide()
        self.connection_label = QLabel("正在连接 · 心跳 暂无")
        self.connection_label.setObjectName("connectionLine")
        header_text.addWidget(self.connection_label)
        self.run_button = QPushButton("开始接单")
        self.run_button.setObjectName("primarySmall")
        self.run_button.clicked.connect(self.toggle_run_status)
        self.run_button.hide()
        header_layout.addLayout(header_text, 1)
        self.workbench_layout.addWidget(header)

        self.notice_label = QLabel("")
        self.notice_label.setObjectName("noticeCard")
        self.notice_label.setWordWrap(True)
        self.notice_label.hide()
        self.workbench_layout.addWidget(self.notice_label)

        self.status_panel = QFrame()
        self.status_panel.setObjectName("statusGrid")
        status_grid_layout = QVBoxLayout(self.status_panel)
        status_grid_layout.setContentsMargins(0, 0, 0, 0)
        status_grid_layout.setSpacing(0)
        self.status_top_row = QFrame()
        self.status_top_row.setObjectName("statusTopRow")
        status_top_layout = QHBoxLayout(self.status_top_row)
        status_top_layout.setContentsMargins(0, 0, 0, 0)
        status_top_layout.setSpacing(0)
        self.status_bottom_row = QFrame()
        self.status_bottom_row.setObjectName("statusBottomRow")
        status_bottom_layout = QHBoxLayout(self.status_bottom_row)
        status_bottom_layout.setContentsMargins(0, 0, 0, 0)
        status_bottom_layout.setSpacing(0)
        self.sales_tile = StatusTile("当前绑定销售", "未绑定", tile_role="topLeft", value_role="hero", show_label=False, show_dot=False)
        self.run_status_tile = StatusTile("Worker 状态", "暂停接单", kind="paused", tile_role="topRight", value_role="chip", show_label=False, show_dot=False)
        self.rpa_tile = StatusTile("自动化组件", "检测中", tile_role="bottomLeft", value_role="pill")
        self.wechat_tile = StatusTile("微信状态", "检测中", tile_role="bottomRight", value_role="pill")
        status_top_layout.addWidget(self.sales_tile, 1)
        status_top_layout.addWidget(self.run_status_tile, 1)
        status_bottom_layout.addWidget(self.rpa_tile, 1)
        status_bottom_layout.addWidget(self.wechat_tile, 1)
        status_grid_layout.addWidget(self.status_top_row)
        status_grid_layout.addWidget(self.status_bottom_row)
        self.workbench_layout.addWidget(self.status_panel)

        self.task_summary = QFrame()
        self.task_summary.setObjectName("taskSummary")
        self.task_summary.setFixedSize(TASK_SUMMARY_SIZE)
        task_summary_layout = QVBoxLayout(self.task_summary)
        task_summary_layout.setContentsMargins(11, 10, 11, 10)
        task_summary_layout.setSpacing(6)
        task_title_row = QHBoxLayout()
        task_title_box = QVBoxLayout()
        task_title_box.setSpacing(1)
        self.task_id_label = QLabel("暂无")
        self.task_id_label.setObjectName("taskId")
        self.task_title_label = QLabel("Worker 任务")
        self.task_title_label.setObjectName("cardTitle")
        task_title_box.addWidget(self.task_id_label)
        task_title_box.addWidget(self.task_title_label)
        self.task_chip = QLabel("暂停接单")
        self.task_chip.setObjectName("chipPaused")
        task_title_row.addLayout(task_title_box, 1)
        task_title_row.addWidget(self.task_chip, 0, Qt.AlignmentFlag.AlignTop)
        self.task_meta_label = QLabel("暂无任务。")
        self.task_meta_label.setObjectName("taskMeta")
        self.task_meta_label.setWordWrap(True)
        task_summary_layout.addLayout(task_title_row)
        task_summary_layout.addWidget(self.task_meta_label)
        self.workbench_layout.addWidget(self.task_summary)

        self.empty_card, self.empty_title, self.empty_text = self._empty_state_card()
        self.workbench_layout.addWidget(self.empty_card)

        self.timeline_card = QFrame()
        self.timeline_card.setObjectName("timelineCard")
        self.timeline_card.setFixedSize(TIMELINE_CARD_SIZE)
        timeline_layout = QVBoxLayout(self.timeline_card)
        timeline_layout.setContentsMargins(11, 11, 11, 11)
        timeline_layout.setSpacing(8)
        timeline_title = QLabel("任务链路")
        timeline_title.setObjectName("cardTitle")
        timeline_layout.addWidget(timeline_title)
        self.steps_area = QScrollArea()
        self.steps_area.setObjectName("chainViewport")
        self.steps_area.setWidgetResizable(True)
        self.steps_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.steps_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.steps_area.setFixedSize(CHAIN_VIEWPORT_SIZE)
        steps_content = QWidget()
        self.steps_layout = QVBoxLayout(steps_content)
        self.steps_layout.setContentsMargins(0, 8, 0, CHAIN_BOTTOM_FOCUS_PADDING)
        self.steps_layout.setSpacing(11)
        self.steps_area.setWidget(steps_content)
        timeline_layout.addWidget(self.steps_area, 1)
        self.workbench_layout.addWidget(self.timeline_card)

        page_layout.addWidget(scroll, 1)
        self.dock_shell = QFrame()
        self.dock_shell.setObjectName("dockShell")
        self.dock_shell.setFixedHeight(DOCK_SHELL_HEIGHT)
        dock_shell_layout = QVBoxLayout(self.dock_shell)
        dock_shell_layout.setContentsMargins(12, 12, 12, 12)
        dock_shell_layout.setSpacing(0)
        self.dock = QFrame()
        self.dock.setObjectName("dockAction")
        self.dock.setFixedSize(DOCK_SIZE)
        dock_layout = QHBoxLayout(self.dock)
        dock_layout.setContentsMargins(10, 8, 10, 8)
        dock_layout.setSpacing(10)
        self.dock_status = QLabel("暂停接单")
        self.dock_status.setObjectName("dockStatus")
        self.dock_button = QPushButton("开始接单")
        self.dock_button.setObjectName("primary")
        self.dock_button.clicked.connect(self.toggle_run_status)
        dock_layout.addWidget(self.dock_status)
        dock_layout.addStretch()
        dock_layout.addWidget(self.dock_button)
        dock_shell_layout.addWidget(self.dock)
        page_layout.addWidget(self.dock_shell)
        self.stack.addWidget(self.workbench_page)

    def _build_settings_page(self) -> None:
        self.settings_page = QWidget()
        layout = self._page_layout(self.settings_page, spacing=14)

        header = self._section_header("设置", "客户端设置")
        layout.addWidget(header)

        settings_list = QFrame()
        settings_list.setObjectName("settingsList")
        settings_list.setFixedWidth(CONTENT_WIDTH)
        settings_layout = QVBoxLayout(settings_list)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(0)
        self.schedule_row = self._settings_row("自动接单时段", "关闭 · 09:00 至 21:00")
        self.schedule_row.clicked.connect(lambda: self.show_page("schedule_settings"))
        logs_row = self._settings_row("本机执行日志", "查看最近 30 天，最多 1000 条本机日志")
        logs_row.clicked.connect(self.open_logs)
        version_row = self._settings_row("客户端版本号", "V14 · Worker 组件化客户端", enabled=False)
        settings_layout.addWidget(self.schedule_row)
        settings_layout.addWidget(logs_row)
        settings_layout.addWidget(version_row)
        layout.addWidget(settings_list)
        layout.addStretch()
        self.stack.addWidget(self.settings_page)

    def _build_schedule_settings_page(self) -> None:
        self.schedule_settings_page = QWidget()
        layout = self._page_layout(self.schedule_settings_page, spacing=14)
        layout.addWidget(self._section_header("设置 / 接单时段设置", "接单时段"))

        settings_list = QFrame()
        settings_list.setObjectName("scheduleDetail")
        settings_list.setFixedWidth(CONTENT_WIDTH)
        settings_layout = QVBoxLayout(settings_list)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(0)

        toggle_row = QFrame()
        toggle_row.setObjectName("scheduleToggleRow")
        toggle_layout = QHBoxLayout(toggle_row)
        toggle_layout.setContentsMargins(12, 12, 12, 12)
        toggle_layout.setSpacing(12)
        toggle_text = QVBoxLayout()
        toggle_text.setContentsMargins(0, 0, 0, 0)
        toggle_text.setSpacing(2)
        toggle_title = QLabel("自动接单")
        toggle_title.setObjectName("settingsTitle")
        toggle_subtitle = QLabel("仅在设定时间内领取新任务")
        toggle_subtitle.setObjectName("settingsSubtitle")
        toggle_text.addWidget(toggle_title)
        toggle_text.addWidget(toggle_subtitle)
        self.auto_accept_checkbox = QPushButton("关闭")
        self.auto_accept_checkbox.setObjectName("switchControl")
        self.auto_accept_checkbox.setCheckable(True)
        self.auto_accept_checkbox.setChecked(False)
        self.auto_accept_checkbox.toggled.connect(
            lambda value: self.auto_accept_checkbox.setText("开启" if value else "关闭")
        )
        toggle_layout.addLayout(toggle_text, 1)
        toggle_layout.addWidget(self.auto_accept_checkbox)

        time_row = QFrame()
        time_row.setObjectName("scheduleTimeRow")
        time_layout = QVBoxLayout(time_row)
        time_layout.setContentsMargins(12, 12, 12, 12)
        time_layout.setSpacing(10)
        time_title = QLabel("接单时间")
        time_title.setObjectName("settingsTitle")
        time_subtitle = QLabel("每天一个时间段，支持跨天")
        time_subtitle.setObjectName("settingsSubtitle")
        time_controls = QHBoxLayout()
        time_controls.setSpacing(8)
        start_box = QVBoxLayout()
        start_label = QLabel("开始")
        start_label.setObjectName("settingsSubtitle")
        self.accept_start_time = QTimeEdit()
        self.accept_start_time.setDisplayFormat("HH:mm")
        self.accept_start_time.setTime(QTime(9, 0))
        end_box = QVBoxLayout()
        end_label = QLabel("结束")
        end_label.setObjectName("settingsSubtitle")
        self.accept_end_time = QTimeEdit()
        self.accept_end_time.setDisplayFormat("HH:mm")
        self.accept_end_time.setTime(QTime(21, 0))
        start_box.addWidget(start_label)
        start_box.addWidget(self.accept_start_time)
        end_box.addWidget(end_label)
        end_box.addWidget(self.accept_end_time)
        time_controls.addLayout(start_box)
        time_controls.addWidget(QLabel("至"), 0, Qt.AlignmentFlag.AlignBottom)
        time_controls.addLayout(end_box)
        time_layout.addWidget(time_title)
        time_layout.addWidget(time_subtitle)
        time_layout.addLayout(time_controls)

        note = QLabel("非接单时段保持连接，但不领取新任务；执行中的任务会先完成。")
        note.setObjectName("settingsNote")
        note.setWordWrap(True)

        settings_layout.addWidget(toggle_row)
        settings_layout.addWidget(time_row)
        settings_layout.addWidget(note)
        layout.addWidget(settings_list)
        layout.addStretch()
        self.stack.addWidget(self.schedule_settings_page)

    def _build_logs_page(self) -> None:
        self.logs_page = QWidget()
        layout = self._page_layout(self.logs_page, spacing=14)
        layout.addWidget(self._section_header("设置 / 本机执行日志", "本机执行日志明细"))

        self.logs_table = QTableWidget(0, 4)
        self.logs_table.setObjectName("logTable")
        self.logs_table.setHorizontalHeaderLabels(["时间", "级别", "任务", "内容"])
        self.logs_table.verticalHeader().setVisible(False)
        self.logs_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.logs_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.logs_table.setWordWrap(True)
        self.logs_table.horizontalHeader().setStretchLastSection(True)
        self.logs_table.setColumnWidth(0, 70)
        self.logs_table.setColumnWidth(1, 48)
        self.logs_table.setColumnWidth(2, 64)
        layout.addWidget(self.logs_table, 1)
        self.stack.addWidget(self.logs_page)

    def _section_header(self, eyebrow: str, title: str) -> QFrame:
        header = QFrame()
        header.setObjectName("workspaceHead")
        header.setFixedHeight(SETTINGS_HEAD_HEIGHT)
        layout = QVBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 7)
        layout.setSpacing(2)
        eyebrow_label = QLabel(eyebrow)
        eyebrow_label.setObjectName("eyebrow")
        title_label = QLabel(title)
        title_label.setObjectName("headline")
        layout.addWidget(eyebrow_label)
        layout.addWidget(title_label)
        return header

    def _settings_row(self, title: str, subtitle: str, *, enabled: bool = True) -> QPushButton:
        row = QPushButton()
        row.setObjectName("settingsRow" if enabled else "settingsRowStatic")
        row.setEnabled(enabled)
        row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.setFixedHeight(SETTINGS_ROW_HEIGHT)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(8)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(4)
        title_label = QLabel(title)
        title_label.setObjectName("settingsTitle")
        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("settingsSubtitle")
        subtitle_label.setWordWrap(True)
        text.addWidget(title_label)
        text.addWidget(subtitle_label)
        chevron = QLabel()
        chevron.setObjectName("chevron")
        if enabled:
            chevron.setPixmap(_icon_from_svg(ICON_CHEVRON_RIGHT_PATHS).pixmap(QSize(18, 18)))
        layout.addLayout(text, 1)
        layout.addWidget(chevron)
        return row

    def bind_worker(self) -> None:
        worker_id = self.worker_id_input.text().strip()
        token = self.worker_token_input.text().strip()
        if not worker_id or not token:
            self.bind_error.setText("Worker ID 和 Worker Token 必填。")
            return
        client_instance_id = self.binding.client_instance_id if self.binding else new_client_instance_id()
        try:
            profile = self.api.bind(worker_id, token, client_instance_id)
            self.binding = Binding(worker_id=worker_id, worker_token=token, client_instance_id=client_instance_id, run_status="paused")
            save_binding(self.binding)
            append_log("INFO", "worker_bound", "绑定 Worker 成功。")
            self.on_profile(profile)
            self.show_page("workbench")
            self.runner.start(self.binding)
        except Exception as exc:
            self.bind_error.setText(str(exc))
            append_log("ERROR", "worker_bind_failed", str(exc))

    def toggle_run_status(self) -> None:
        if not self.binding:
            return
        if self.binding.run_status == "faulted":
            self.on_error("客户端处于故障状态，本次运行禁止继续接单。")
            return
        next_status = "paused" if self.binding.run_status == "running" else "running"
        self.runner.set_run_status(next_status)
        self.refresh_view()

    def show_page(self, page: PageName) -> None:
        page_map = {
            "bind": self.bind_page,
            "workbench": self.workbench_page,
            "settings": self.settings_page,
            "schedule_settings": self.schedule_settings_page,
            "logs": self.logs_page,
        }
        self.stack.setCurrentWidget(page_map[page])
        self.client_title.setText(
            {
                "bind": "Worker 工作台",
                "workbench": "Worker 工作台",
                "settings": "设置",
                "schedule_settings": "接单时段设置",
                "logs": "本机执行日志",
            }[page]
        )
        self.back_button.setVisible(page in {"settings", "schedule_settings", "logs"})
        self.settings_button.setVisible(page not in {"settings", "schedule_settings", "logs", "bind"})
        if page == "logs":
            self.refresh_logs()
        self.refresh_view()

    def handle_back(self) -> None:
        if self.stack.currentWidget() in {self.logs_page, self.schedule_settings_page}:
            self.show_page("settings")
            return
        self.show_page("workbench")

    def open_logs(self) -> None:
        self.show_page("logs")

    def on_profile(self, profile: WorkerProfile) -> None:
        self.profile = profile
        if self.binding:
            self.binding.run_status = profile.run_status
            save_binding(self.binding)
        self.refresh_view()

    def on_connection_status(self, status: str) -> None:
        self.connection_status = status
        if status == "invalid":
            clear_binding()
            self.binding = None
            self.runner.stop()
            self.bind_error.setText("绑定已失效，请重新绑定。")
            self.show_page("bind")
            return
        if (
            status == "online"
            and self.notice_label.text()
            and not self.runner.run_status_sync_error
        ):
            self.notice_label.setText("")
            self.notice_label.hide()
        self.refresh_view()

    def on_task(self, task: Task | None) -> None:
        self.current_task = task
        if task:
            previous_task_id = self.last_task.id if self.last_task else None
            if previous_task_id != task.id or self.last_result:
                self.step_history.clear()
            self.last_task = task
            self.last_result = None
            if not self.step_history:
                self.step_history.append(
                    (
                        "任务已领取",
                        f"Worker 已领取 {task_type_title(task.task_type)}任务。",
                        "done",
                        None,
                    )
                )
        self.refresh_view()

    def on_step(self, step: RpaStep) -> None:
        self.step_history.append((step.title, step.remark or step.current_step, "done", step.evidence_path))
        self.refresh_view()

    def on_result(self, result: RpaResult | None) -> None:
        if not result:
            self.last_result = None
            return
        self.last_result = result
        if result.ok:
            title = "回传执行结果"
            if self.last_task and self.last_task.task_type == "chat_reply":
                remark = "AI 回复已发送并回传。"
            elif result.result_code == "already_friend":
                remark = "客户已是好友，任务已回传完成。"
            else:
                remark = "已发送添加通讯录邀请，该结果不代表客户已同意好友申请。"
            self.step_history.append((title, remark, "final", result.evidence_path))
        else:
            self.step_history.append(("任务执行失败", f"{result.error_code or 'OTHER'} · {result.message}", "error", result.evidence_path))
        self.refresh_view()

    def on_error(self, message: str) -> None:
        self.notice_label.setText(message)
        self.notice_label.setVisible(bool(message))
        self.refresh_view()

    def refresh_view(self) -> None:
        run_status = self.binding.run_status if self.binding else "paused"
        is_running = run_status == "running"
        is_faulted = run_status == "faulted"
        online = self.connection_status == "online"
        offline = self.connection_status == "offline"
        profile = self.profile
        active_task = self.current_task
        display_task = active_task or self.last_task
        result = self.last_result
        if is_faulted:
            headline = "客户端故障，已停止接单"
        elif self.runner.run_status_sync_error and not is_running:
            headline = "已在本机暂停，后端同步失败"
        elif offline:
            headline = "服务端不可达"
        elif active_task and not is_running:
            headline = "暂停接单，当前任务继续执行"
        elif active_task:
            headline = "正在执行任务"
        elif result and result.ok:
            headline = "任务执行完成"
        elif result and not result.ok:
            headline = "任务执行失败"
        elif is_running:
            headline = "正在等待可执行任务"
        else:
            headline = "当前暂停接单"
        self.headline_label.setText(headline)

        if profile and profile.last_heartbeat_at:
            heartbeat = _format_time(profile.last_heartbeat_at)
        else:
            heartbeat = "暂无"
        self.connection_label.setText("连接异常 · 最近心跳 " + heartbeat if offline else "连接正常 · 最近心跳 " + heartbeat if online else "正在连接 · 心跳 " + heartbeat)
        self.connection_label.setObjectName("connectionLineDanger" if offline else "connectionLine")
        self.connection_label.style().unpolish(self.connection_label)
        self.connection_label.style().polish(self.connection_label)

        self.sales_tile.set_value(profile.bound_sales_name if profile and profile.bound_sales_name else "未绑定")
        run_status_kind = "paused" if offline else ("danger" if is_faulted else "accepting" if is_running else "paused")
        self.run_status_tile.set_value(
            (
                "客户端故障"
                if is_faulted
                else "接单中"
                if is_running
                else "暂停接单 · 同步失败"
                if self.runner.run_status_sync_error
                else "暂停接单"
            ),
            kind=run_status_kind,
        )
        self.rpa_tile.set_value(
            "可用" if profile and profile.rpa_component_status == "ready" else "不可用",
            kind="ok" if profile and profile.rpa_component_status == "ready" else "danger",
        )
        self.wechat_tile.set_value("已连接" if profile and profile.wechat_status == "logged_in" else "未检测到", kind="ok" if profile and profile.wechat_status == "logged_in" else "danger")

        run_text = "暂停接单" if is_running else "开始接单"
        self.run_button.setText(run_text)
        self.dock_button.setText(run_text)
        self.dock_status.setText("接单中" if is_running else "暂停接单")
        self.run_button.setObjectName("secondarySmall" if is_running else "primarySmall")
        self.dock_button.setObjectName("secondary" if is_running else "primary")
        for button in (self.run_button, self.dock_button):
            button.style().unpolish(button)
            button.style().polish(button)

        has_task_context = bool(display_task or result)
        self.task_summary.setVisible(has_task_context)
        self.timeline_card.setVisible(bool(active_task or self.step_history or result))
        self.empty_card.setVisible(not has_task_context)
        self.status_panel.setVisible((not has_task_context) or offline)
        self._sync_workbench_sections(has_task_context=has_task_context, offline=offline)

        if display_task:
            self.task_id_label.setText(_short_task_id(display_task.id))
            self.task_title_label.setText(task_type_title(display_task.task_type))
            self.task_meta_label.setText(self._task_meta(display_task))
        elif result:
            self.task_id_label.setText("最近任务")
            self.task_title_label.setText(
                task_type_title(self.last_task.task_type if self.last_task else None)
            )
            self.task_meta_label.setText(result.message or result.result_code or result.error_code or "结果已回传。")

        if result:
            if result.ok:
                self.task_chip.setText("已完成")
                self.task_chip.setObjectName("chipAccepting")
            else:
                self.task_chip.setText("失败")
                self.task_chip.setObjectName("chipOffline")
        else:
            self.task_chip.setText("接单中" if is_running else "暂停接单")
            self.task_chip.setObjectName("chipAccepting" if is_running else "chipPaused")
        self.task_chip.style().unpolish(self.task_chip)
        self.task_chip.style().polish(self.task_chip)

        if not has_task_context:
            self.empty_card.setProperty("emptyState", "waiting" if is_running else "idle")
            self.empty_title.setText("接单中，等待服务端分配任务" if is_running else "暂无可领取任务")
            self.empty_text.setText("有可执行任务时，Worker 会领取并进入任务链路页面。" if is_running else "暂停接单后不会领取新的任务。")
            self.empty_card.style().unpolish(self.empty_card)
            self.empty_card.style().polish(self.empty_card)

        if offline:
            self.dock_status.setText("离线")
            self.dock_button.setEnabled(False)
            self.dock_button.setText("开始接单")
            self.dock_button.setObjectName("secondary")
            self.dock.setProperty("dockState", "offline")
        else:
            self.dock_button.setEnabled(True)
            self.dock.setProperty("dockState", "normal")
        self.dock.style().unpolish(self.dock)
        self.dock.style().polish(self.dock)
        self.dock_button.style().unpolish(self.dock_button)
        self.dock_button.style().polish(self.dock_button)

        self._refresh_steps(active_task=bool(active_task), result=result)

    def _sync_workbench_sections(self, *, has_task_context: bool, offline: bool) -> None:
        for widget in (self.status_panel, self.task_summary, self.empty_card, self.timeline_card):
            self.workbench_layout.removeWidget(widget)
        if has_task_context:
            self.workbench_layout.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, 0)
            self.workbench_layout.setSpacing(8)
            self.page_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        else:
            self.workbench_layout.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, 24)
            self.workbench_layout.setSpacing(PAGE_GAP)
            self.page_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        if offline and has_task_context:
            order = (self.task_summary, self.status_panel)
        elif has_task_context:
            order = (self.task_summary, self.timeline_card)
        else:
            order = (self.status_panel, self.empty_card)
        for widget in order:
            self.workbench_layout.addWidget(widget)

    def _task_meta(self, task: Task) -> str:
        contact = task.phone or task.wechat or "未返回完整联系方式"
        customer = task.customer_name or "客户"
        sales = task.sales_name or (self.profile.bound_sales_name if self.profile else None) or "-"
        remark_code = task.remark_code or "-"
        return f"{customer} · {contact} · {sales} · 备注短码：{remark_code}"

    def _refresh_steps(self, *, active_task: bool, result: RpaResult | None) -> None:
        self._focused_step_widget = None
        while self.steps_layout.count():
            item = self.steps_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        rows = list(self.step_history)
        if active_task and (not rows or rows[-1][2] != "current"):
            rows.append(("正在执行当前步骤", "请保持微信桌面客户端可见，执行过程中不要手动操作微信。", "current", None))
        if result and result.ok and not rows:
            rows.append(("任务执行完成", result.message, "final", result.evidence_path))
        if result and not result.ok and not rows:
            rows.append(("任务执行失败", f"{result.error_code or 'OTHER'} · {result.message}", "error", result.evidence_path))

        for title, remark, state, evidence_path in rows:
            row = StepRow(title, remark, state=state, evidence_path=evidence_path)
            self.steps_layout.addWidget(row)
            if state in {"current", "final", "error"}:
                self._focused_step_widget = row
        if self._focused_step_widget:
            QTimer.singleShot(0, self._center_focused_step)

    def _center_focused_step(self) -> None:
        if not self._focused_step_widget:
            return
        scrollbar = self.steps_area.verticalScrollBar()
        row_top = self._focused_step_widget.y()
        checkpoint_center_y = row_top + 17
        target = checkpoint_center_y - self.steps_area.viewport().height() // 2
        scrollbar.setValue(max(scrollbar.minimum(), min(scrollbar.maximum(), target)))

    def refresh_logs(self) -> None:
        logs = read_logs(limit=200)
        self.logs_table.setRowCount(len(logs))
        for row, item in enumerate(logs):
            values = [
                _format_time(item.get("created_at")),
                str(item.get("level") or "-"),
                _short_task_id(item.get("task_id")),
                f"{item.get('event') or ''} · {item.get('message') or ''}".strip(" ·"),
            ]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.logs_table.setItem(row, col, cell)
        self.logs_table.resizeRowsToContents()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.runner.stop()
        super().closeEvent(event)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                font-family: -apple-system, "SF Pro Text", "PingFang SC", "Microsoft YaHei UI", system-ui, sans-serif;
                font-size: 14px;
                color: #171b22;
                background: #ffffff;
            }
            QWidget#windowRoot {
                background: transparent;
            }
            QFrame#appWindow {
                background: #ffffff;
                border: 1px solid #cfd6df;
                border-radius: 10px;
            }
            QFrame#appWindowMaximized {
                background: #ffffff;
                border: none;
                border-radius: 0;
            }
            QFrame#titlebar {
                min-height: 28px;
                max-height: 28px;
                background: #f7f8fa;
                border-bottom: 1px solid #e8edf3;
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
            }
            QFrame#titlebarBrand, QFrame#windowControls, QFrame#appBody {
                background: transparent;
                border: none;
            }
            QLabel#brandMark {
                color: #ffffff;
                background: #f2a65a;
                border-radius: 6px;
                font-size: 11px;
                font-weight: 900;
            }
            QLabel#brandText {
                color: #171b22;
                font-size: 11px;
                font-weight: 900;
            }
            QLabel {
                background: transparent;
            }
            QWidget#breathingStatusDot {
                background: transparent;
                border: none;
            }
            QLabel#clientTitle {
                font-size: 12px;
                font-weight: 900;
            }
            QFrame#clientBar {
                min-height: 42px;
                max-height: 42px;
                background: #ffffff;
                border-bottom: 1px solid #e8edf3;
            }
            QPushButton {
                min-height: 34px;
                padding: 0 14px;
                color: #34517d;
                background: #ffffff;
                border: 1px solid #d8dee6;
                border-radius: 8px;
                font-weight: 800;
            }
            QPushButton#primary, QPushButton#primarySmall {
                color: #ffffff;
                background: #4a6ea5;
                border-color: #4a6ea5;
            }
            QPushButton#secondary, QPushButton#secondarySmall {
                color: #34517d;
                background: #ffffff;
                border-color: #d8dee6;
            }
            QPushButton#primarySmall, QPushButton#secondarySmall {
                min-height: 32px;
                padding: 0 12px;
            }
            QPushButton#clientMenu, QPushButton#clientSettings {
                min-width: 32px;
                min-height: 32px;
                padding: 0;
                color: #68717d;
                background: transparent;
                border: none;
                border-radius: 6px;
            }
            QPushButton#windowControl, QPushButton#windowControlClose {
                min-width: 30px;
                min-height: 28px;
                padding: 0;
                background: transparent;
                border: none;
                border-radius: 0;
            }
            QPushButton#windowControl:hover {
                background: #eef1f5;
            }
            QPushButton#windowControlClose:hover {
                background: #f3d5cf;
            }
            QLineEdit {
                min-height: 40px;
                padding: 0 12px;
                border: 1px solid #d8dee6;
                border-radius: 8px;
                background: #ffffff;
            }
            QLineEdit:focus {
                border-color: #4a6ea5;
            }
            QScrollArea#pageScroll, QScrollArea#chainViewport {
                border: none;
                background: #ffffff;
            }
            QScrollBar:vertical {
                width: 8px;
                margin: 4px 2px 4px 2px;
                background: transparent;
                border: none;
            }
            QScrollBar::handle:vertical {
                min-height: 36px;
                background: #cfd6df;
                border-radius: 3px;
            }
            QScrollBar::handle:vertical:hover {
                background: #aeb8c5;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical,
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                width: 0;
                height: 0;
                background: transparent;
                border: none;
            }
            QScrollBar:horizontal {
                height: 8px;
                margin: 2px 4px 2px 4px;
                background: transparent;
                border: none;
            }
            QScrollBar::handle:horizontal {
                min-width: 36px;
                background: #cfd6df;
                border-radius: 3px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #aeb8c5;
            }
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal,
            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {
                width: 0;
                height: 0;
                background: transparent;
                border: none;
            }
            QLabel#eyebrow {
                color: #4a6ea5;
                font-size: 12px;
                font-weight: 900;
            }
            QLabel#pageTitle, QLabel#headline {
                font-size: 18px;
                line-height: 25px;
                font-weight: 700;
            }
            QLabel#helperText, QLabel#taskMeta, QLabel#stepRemark {
                color: #68717d;
                font-size: 12px;
                line-height: 18px;
            }
            QLabel#settingsSubtitle {
                color: #68717d;
                font-size: 12px;
                line-height: 17px;
                font-weight: 600;
            }
            QLabel#formLabel {
                color: #8f98a4;
                font-size: 13px;
                font-weight: 900;
            }
            QLabel#errorText {
                color: #b85c4f;
                font-size: 12px;
            }
            QFrame#formPanel {
                background: #f6f7f9;
                border: 1px solid #e8edf3;
                border-radius: 10px;
            }
            QFrame#statusGrid {
                background: rgba(255, 255, 255, 220);
                border: 1px solid rgba(203, 211, 224, 235);
                border-radius: 12px;
            }
            QFrame#statusTile {
                background: transparent;
                border: none;
                border-radius: 0;
            }
            QFrame#statusTopRow {
                border-bottom: 1px solid #e8edf3;
            }
            QFrame#statusTile[tileRole="topLeft"],
            QFrame#statusTile[tileRole="topRight"] {
                min-height: 58px;
                max-height: 58px;
            }
            QFrame#statusTile[tileRole="bottomLeft"],
            QFrame#statusTile[tileRole="bottomRight"] {
                min-height: 64px;
                max-height: 64px;
            }
            QFrame#workspaceHead {
                border-bottom: 1px solid #e8edf3;
            }
            QLabel#connectionLine {
                color: #68717d;
                font-size: 11px;
                font-weight: 800;
            }
            QLabel#connectionLineDanger, QLabel#noticeCard {
                color: #b85c4f;
                font-size: 11px;
                font-weight: 800;
            }
            QLabel#noticeCard {
                padding: 9px;
                background: #fff0ec;
                border: 1px solid #f3d5cf;
                border-radius: 10px;
            }
            QLabel#tileLabel {
                color: #8f98a4;
                font-size: 12px;
                font-weight: 900;
            }
            QLabel#tileLabelStatus {
                color: #7b8491;
                font-size: 11px;
                font-weight: 720;
            }
            QLabel#tileValueHero {
                color: #171b22;
                font-size: 22px;
                font-weight: 820;
                line-height: 28px;
            }
            QLabel#tileValueHeroCompact {
                color: #171b22;
                font-size: 16px;
                font-weight: 900;
                line-height: 22px;
            }
            QLabel#tileValue-plain {
                font-size: 13px;
                font-weight: 900;
            }
            QLabel#tileValue-paused {
                color: #34517d;
                font-size: 13px;
                font-weight: 900;
            }
            QLabel#tileValue-accepting, QLabel#tileValue-ok {
                color: #33845a;
                font-size: 13px;
                font-weight: 900;
            }
            QLabel#tileValue-danger {
                color: #b85c4f;
                font-size: 13px;
                font-weight: 900;
            }
            QLabel#statusChip-paused, QLabel#statusChip-accepting {
                min-height: 25px;
                padding: 3px 11px;
                border-radius: 13px;
                font-size: 12px;
                font-weight: 900;
                line-height: 18px;
            }
            QLabel#statusChip-paused {
                color: #34517d;
                background: #e9eef6;
            }
            QLabel#statusChip-accepting {
                color: #33845a;
                background: #eaf6ef;
            }
            QLabel#statusPill-ok, QLabel#statusPill-accepting {
                min-height: 22px;
                padding: 2px 9px;
                color: #33845a;
                background: rgba(51, 132, 90, 30);
                border-radius: 11px;
                font-size: 11px;
                font-weight: 900;
                line-height: 16px;
            }
            QLabel#statusPill-danger, QLabel#statusPill-paused {
                min-height: 22px;
                padding: 2px 9px;
                color: #b85c4f;
                background: #fff0ec;
                border-radius: 11px;
                font-size: 11px;
                font-weight: 900;
                line-height: 16px;
            }
            QFrame#emptyCard, QFrame#taskSummary, QFrame#timelineCard, QFrame#settingsList {
                background: #ffffff;
                border: 1px solid #d8dee6;
                border-radius: 10px;
            }
            QFrame#taskSummary {
                min-width: 290px;
                max-width: 290px;
                min-height: 110px;
                max-height: 110px;
            }
            QFrame#timelineCard {
                min-width: 290px;
                max-width: 290px;
                min-height: 370px;
                max-height: 370px;
            }
            QScrollArea#chainViewport {
                min-width: 266px;
                max-width: 266px;
                min-height: 346px;
                max-height: 346px;
                padding-right: 2px;
            }
            QFrame#emptyCard {
                min-height: 180px;
            }
            QFrame#emptyCard[emptyState="waiting"] {
                background: #fbfdfb;
            }
            QLabel#emptyTitle, QLabel#cardTitle {
                font-size: 16px;
                font-weight: 900;
            }
            QLabel#taskId {
                color: #4a6ea5;
                font-size: 12px;
                font-weight: 900;
            }
            QLabel#chipPaused, QLabel#chipAccepting, QLabel#chipOffline {
                min-height: 22px;
                padding: 2px 9px;
                border-radius: 11px;
                font-size: 12px;
                font-weight: 900;
            }
            QLabel#chipPaused {
                color: #34517d;
                background: #e9eef6;
            }
            QLabel#chipAccepting {
                color: #33845a;
                background: #eaf6ef;
            }
            QLabel#chipOffline {
                color: #b85c4f;
                background: #fff0ec;
            }
            QFrame#stepRow-current, QFrame#stepRow-final, QFrame#stepRow-error {
                border-radius: 8px;
                padding: 0;
            }
            QFrame#stepRow-current {
                background: #fff2e2;
                border: 1px solid rgba(242, 166, 90, 0.32);
            }
            QFrame#stepRow-final {
                background: #eaf6ef;
                border: 1px solid rgba(51, 132, 90, 0.26);
            }
            QFrame#stepRow-error {
                background: #fff0ec;
                border: 1px solid #f3d5cf;
            }
            QFrame#stepRow-done {
                background: transparent;
                border: none;
                padding: 0;
            }
            QLabel#stepIcon-done, QLabel#stepIcon-current, QLabel#stepIcon-error, QLabel#stepIcon-final {
                color: #ffffff;
                min-width: 18px;
                max-width: 18px;
                min-height: 18px;
                max-height: 18px;
                border-radius: 9px;
                font-size: 12px;
                font-weight: 900;
            }
            QLabel#stepIcon-done, QLabel#stepIcon-final {
                background: #4a6ea5;
            }
            QLabel#stepIcon-current {
                background: #f2a65a;
            }
            QLabel#stepIcon-error {
                background: #b85c4f;
            }
            QLabel#stepTitle {
                font-size: 13px;
                font-weight: 900;
            }
            QLabel#settingsTitle {
                color: #171b22;
                font-size: 14px;
                line-height: 20px;
                font-weight: 900;
            }
            QFrame#stepScreenshot {
                background: transparent;
                border: none;
            }
            QFrame#shotWindow {
                min-width: 237px;
                max-width: 237px;
                min-height: 88px;
                max-height: 88px;
                background: #f8fafc;
                border: 1px solid #d8dee6;
                border-radius: 7px;
            }
            QLabel#shotSearch {
                min-height: 24px;
                padding: 0 8px;
                color: #68717d;
                background: #f6f7f9;
                border: 1px solid #e8edf3;
                border-radius: 6px;
                font-size: 11px;
                font-weight: 800;
            }
            QLabel#shotEmpty, QLabel#shotCaption {
                color: #8f98a4;
                font-size: 11px;
                font-weight: 800;
            }
            QFrame#scheduleDetail {
                background: transparent;
                border: none;
                border-radius: 0;
            }
            QFrame#scheduleToggleRow, QFrame#scheduleTimeRow {
                background: #ffffff;
                border: 1px solid #d8dee6;
                border-radius: 10px;
            }
            QLabel#settingsNote {
                padding: 0 2px;
                color: #68717d;
                background: transparent;
                border: none;
                font-size: 11px;
                font-weight: 600;
                line-height: 17px;
            }
            QPushButton#switchControl {
                min-height: 28px;
                padding: 0 10px;
                color: #68717d;
                background: #f6f7f9;
                border: 1px solid #d8dee6;
                border-radius: 8px;
                font-size: 12px;
                font-weight: 900;
            }
            QPushButton#switchControl:checked {
                color: #33845a;
                background: #eaf6ef;
                border-color: rgba(51, 132, 90, 0.26);
            }
            QTimeEdit {
                min-height: 38px;
                padding: 0 10px;
                border: 1px solid #d8dee6;
                border-radius: 8px;
                background: #ffffff;
                font-size: 12px;
                font-weight: 800;
            }
            QLabel#stepShot {
                margin-top: 8px;
                padding: 6px;
                color: #68717d;
                background: #f6f7f9;
                border: 1px solid #d8dee6;
                border-radius: 7px;
                font-size: 11px;
                font-weight: 800;
            }
            QDialog#evidenceDialog {
                background: #ffffff;
                border: 1px solid #d8dee6;
                border-radius: 12px;
            }
            QLabel#dialogTitle {
                font-size: 15px;
                font-weight: 900;
                color: #151a21;
            }
            QLabel#dialogShot {
                background: #f6f7f9;
                border: 1px solid #e8edf3;
                border-radius: 10px;
            }
            QPushButton#dialogClose {
                min-width: 30px;
                min-height: 30px;
                padding: 0;
                color: #68717d;
                background: transparent;
                border: none;
                border-radius: 6px;
            }
            QPushButton#dialogClose:hover {
                background: #eef1f5;
            }
            QFrame#dockShell {
                min-height: 74px;
                max-height: 74px;
                background: #ffffff;
                border-top: 1px solid #e8edf3;
                border-bottom-left-radius: 10px;
                border-bottom-right-radius: 10px;
            }
            QFrame#dockAction {
                min-width: 290px;
                max-width: 290px;
                min-height: 50px;
                max-height: 50px;
                background: #f6f7f9;
                border: 1px solid #e8edf3;
                border-radius: 10px;
            }
            QFrame#dockAction[dockState="offline"] {
                background: #f6f7f9;
            }
            QLabel#dockStatus {
                color: #68717d;
                font-size: 12px;
                font-weight: 900;
            }
            QPushButton#settingsRow, QPushButton#settingsRowStatic {
                min-height: 61px;
                max-height: 61px;
                padding: 0;
                text-align: left;
                border-radius: 0;
                border: none;
                border-bottom: 1px solid #e8edf3;
                background: #ffffff;
                color: #171b22;
            }
            QPushButton#settingsRow:hover {
                background: #f6f7f9;
            }
            QPushButton#settingsRowStatic {
                color: #171b22;
            }
            QLabel#chevron {
                color: #68717d;
                min-width: 18px;
                min-height: 18px;
            }
            QTableWidget#logTable {
                border: 1px solid #d8dee6;
                border-radius: 10px;
                gridline-color: #e8edf3;
                background: #ffffff;
                font-size: 11px;
                margin-top: 20px;
            }
            QTableWidget#logTable::item {
                min-height: 44px;
                padding: 0 6px;
            }
            QHeaderView::section {
                color: #68717d;
                background: #f6f7f9;
                border: none;
                border-bottom: 1px solid #d8dee6;
                padding: 6px;
                font-size: 11px;
                font-weight: 900;
            }
            """
        )


def run_app() -> int:
    app = GuardedQApplication([])
    window = WorkerWindow()
    window.show()
    QTimer.singleShot(0, lambda: append_log("INFO", "ui_started", "Worker UI 启动。"))
    return app.exec()
