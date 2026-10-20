"""Native diagnostic dialog: no WebEngine, Worker, Hook or network startup."""
from datetime import datetime
from pathlib import Path
import threading

from PySide6.QtCore import QObject, Signal, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QVBoxLayout, QProgressBar

from .diagnostic_bundle import export_bundle
from .evidence_paths import artifacts, default_data_root


class ExportJob(QObject):
    progress = Signal(object)
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.root = Path(root)
        self.running = False

    def start(self):
        if self.running:
            return False
        self.running = True
        destination = artifacts(self.root) / "exports" / f"车金故障证据-{datetime.now():%Y%m%d-%H%M%S-%f}.zip"

        def collect():
            try:
                result = export_bundle(self.root, destination, self.progress.emit)
                outcome = (True, str(result))
            except Exception as exc:
                # Exception text can contain credentials or customer data.
                outcome = (False, type(exc).__name__)
            self.running = False
            (self.completed if outcome[0] else self.failed).emit(outcome[1])
        threading.Thread(target=collect, name="diagnostic-export", daemon=True).start()
        return True


class DiagnosticDialog(QDialog):
    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.setWindowTitle("车金故障诊断")
        self.setModal(False)
        # The standalone entry exits explicitly after collection. A normal application
        # quit must still be allowed to close this window without a veto.
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.resize(420, 180)
        layout = QVBoxLayout(self)
        self.label = QLabel("导出故障证据后，将 ZIP 发给管理员。")
        self.label.setWordWrap(True)
        self.bar = QProgressBar()
        self.button = QPushButton("导出故障证据")
        self.folder_button = QPushButton("打开文件所在位置")
        self.folder_button.setEnabled(False)
        for widget in (self.label, self.bar, self.button, self.folder_button):
            layout.addWidget(widget)
        self.job = ExportJob(root, self)
        self.job.progress.connect(self._progress)
        self.job.completed.connect(self._completed)
        self.job.failed.connect(self._failed)
        self.button.clicked.connect(self.start)
        self.folder_button.clicked.connect(self.open_folder)
        self.result_path = None
        self.partial = False
        self.close_requested = False

    def start(self):
        if self.job.start():
            self.partial = False
            self.button.setEnabled(False)
            self.folder_button.setEnabled(False)
            self.label.setText("正在导出，不影响正常接单……")
            self.bar.setValue(0)

    def _progress(self, value):
        self.bar.setValue(value["percent"])
        self.label.setText(value["stage"])
        if value["percent"] == 100:
            self.partial = "缺失" in value["stage"]

    def _completed(self, path):
        self.result_path = Path(path)
        self.bar.setValue(100)
        self.label.setText(("导出完成，部分证据缺失，详情见 ZIP 内收集清单。\n" if self.partial else "导出完成。\n")
                           + "请将以下 ZIP 发给管理员：\n" + str(self.result_path))
        self.button.setEnabled(True)
        self.folder_button.setEnabled(True)
        if not self.close_requested:
            self.open_folder()
        self._finish_close()

    def _failed(self, kind):
        self.label.setText(f"导出未完成（{kind}），请检查磁盘空间和目录权限后重试。")
        self.button.setEnabled(True)
        self._finish_close()

    def open_folder(self):
        if self.result_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.result_path.parent)))

    def showEvent(self, event):
        self.close_requested = False
        super().showEvent(event)

    def _finish_close(self):
        if self.close_requested and self.parent() is None and not self.job.running:
            QApplication.instance().quit()

    def reject(self):
        # Escape follows the same deferred close as the window close button.
        self.close()

    def closeEvent(self, event):
        # Keep QObject ownership alive while collecting; embedded close never quits Worker.
        event.accept()
        self.close_requested = True
        self.hide()
        self._finish_close()


def main():
    app = QApplication.instance() or QApplication([])
    dialog = DiagnosticDialog(default_data_root())
    dialog.show()
    dialog.start()
    return app.exec()
