"""Production slot bodies without Qt; UI rendering is outside this test scope."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("file,klass", [("web_ui.py", "WorkerWebWindow"), ("ui.py", "WorkerWindow")])
@pytest.mark.parametrize("stopped", [True, False])
def test_both_ui_slots_report_shutdown_before_closing_and_handle_failure(file, klass, stopped):
    source = Path(__file__).resolve().parents[1] / "chejin_worker_client" / file
    cls = next(n for n in ast.parse(source.read_text(encoding="utf-8")).body if isinstance(n, ast.ClassDef) and n.name == klass)
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_quit_for_update")
    fn.decorator_list = []
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), str(source), "exec"), namespace)
    events = []
    def stop():
        events.append("stop")
        if not stopped:
            raise RuntimeError("UPDATE_WRITERS_NOT_STOPPED")
    ui = SimpleNamespace(runner=SimpleNamespace(stop_for_update=stop),
                         update_coordinator=SimpleNamespace(report_normal_exit_result=lambda **v: events.append(v)),
                         close=lambda: events.append("close"))
    namespace["_quit_for_update"](ui)
    assert events == ["stop", {"stopped": stopped}] + (["close"] if stopped else [])
