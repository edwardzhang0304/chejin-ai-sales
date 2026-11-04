"""Isolated subprocess actor: real main/Qt/export, synthetic UI/allocations only."""
import json
import os
from pathlib import Path
import sys
import time
import tracemalloc
from types import SimpleNamespace
from dataclasses import replace

mode, home = sys.argv[1:3]
root = Path(home)
os.environ['CHEJIN_WORKER_HOME'] = str(root)
os.environ['CHEJIN_WORKER_UI_MODE'] = 'pyside'
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


def held_allocation():
    return bytearray(b'synthetic-no-customer-data' * 40000)


if mode == 'worker':
    from chejin_worker_client import main, performance_evidence as pe, release_transport, hook_installation, config
    from chejin_worker_client.performance_contract import POLICY
    release_transport.packaged_transport = lambda: 'hook'
    hook_installation.launch_information = lambda: {'api_base_url': config.CONFIG.api_base_url}
    original_install = pe.install
    # Accelerate resource sampling only; actual desktop capture stays 30 seconds.
    pe.install = lambda path: original_install(path, policy=replace(POLICY, sample_seconds=.5, snapshot_seconds=.1))
    def app():
        application = QApplication.instance() or QApplication([])
        ui_timer = pe.attach_ui(application)
        ui_timer.setInterval(50)
        allocations = []
        captured = None
        allocated = released = False
        timer = QTimer()
        def tick():
            nonlocal captured, allocated, released
            if (root / 'stop-actor').exists():application.quit(); return
            pe.emit('loop', name='synthetic_local_loop')
            pressure = pe._ACTIVE.capture_pressure()
            if pressure is None and not (root / 'actor-ready.json').exists():
                (root / 'actor-ready.json').write_text(json.dumps({'pid': os.getpid()}))
            if tracemalloc.is_tracing() and captured is None:captured = time.monotonic()
            if captured is not None:
                elapsed = time.monotonic() - captured
                if elapsed > 2 and not allocated:
                    allocations.append(held_allocation()); allocated = True
                if elapsed > 15 and not released:
                    allocations.clear(); released = True  # no forced GC
        timer.timeout.connect(tick); timer.start(50)
        QTimer.singleShot(90000, application.quit)
        code = application.exec()
        (root / 'actor-result.json').write_text(json.dumps({'pid': os.getpid(), 'allocated': allocated, 'released': released}))
        return code
    sys.modules['chejin_worker_client.ui'] = SimpleNamespace(run_app=app)
    sys.argv = ['worker']
    raise SystemExit(main.main())
elif mode == 'diagnostic':
    from chejin_worker_client import diagnostic_ui as du
    du.default_data_root = lambda: root
    du.DiagnosticDialog.open_folder = lambda self: None
    previous = du.DiagnosticDialog.__init__
    def initialize(self, *args, **kwargs):
        previous(self, *args, **kwargs)
        ticks = []
        timer = QTimer(self)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start(20)
        def finish(value, error=False):
            gaps = [b-a for a,b in zip(ticks,ticks[1:])]
            (root / f'diagnostic-result-{os.getpid()}.json').write_text(json.dumps({
                'pid': os.getpid(), 'error': error, 'result': value, 'ticks': len(ticks),
                'max_ui_gap': max(gaps, default=0)}))
            QApplication.instance().quit()
        self.job.completed.connect(finish)
        self.job.failed.connect(lambda value: finish(value, True))
        QTimer.singleShot(85000, QApplication.instance().quit)
    du.DiagnosticDialog.__init__ = initialize
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packaging'))
    import chejin_worker_client_entry
    sys.argv = ['worker', '--export-diagnostics']
    raise SystemExit(chejin_worker_client_entry.run())
