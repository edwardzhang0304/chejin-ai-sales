"""Toolhelp contract tests use synthetic process tables, never Windows or WeChat."""
import ctypes
from types import SimpleNamespace
import pytest
from chejin_worker_client.hook_process import _wechat_process_ids, HookProcessError


class Function:
    def __init__(self, fn): self.fn = fn
    def __call__(self, *args): return self.fn(*args)


class Kernel:
    def __init__(self, rows, *, end_error=18, handle=7):
        self.rows = iter(rows)
        self.closed = []
        self.entries = 0
        self.CreateToolhelp32Snapshot = Function(lambda flags, pid: handle)
        self.Process32FirstW = Function(self.read)
        self.Process32NextW = Function(self.read)
        self.GetLastError = Function(lambda: end_error)
        self.CloseHandle = Function(lambda h: self.closed.append(h) or 1)
    def read(self, handle, pointer):
        row = next(self.rows, None)
        if row is None: return 0
        self.entries += 1
        entry = pointer._obj
        assert entry.dwSize == ctypes.sizeof(entry)
        entry.th32ProcessID, entry.szExeFile = row
        return 1


def test_names_are_filtered_without_constructing_unrelated_processes():
    rows = [(i, "other.exe") for i in range(1, 202)] + [(500, "WeChat.exe"), (501, "WECHAT.EXE")]
    kernel = Kernel(rows)
    assert _wechat_process_ids(kernel) == (500, 501)
    assert kernel.entries == 203 and kernel.closed == [7]


@pytest.mark.parametrize("rows", [[], [(1, "WeChat.exe")], [(1, "other.exe")]])
def test_incomplete_enumeration_never_returns_partial_result(rows):
    kernel = Kernel(rows, end_error=5)
    with pytest.raises(HookProcessError, match="INSPECTION_FAILED"):
        _wechat_process_ids(kernel)
    assert kernel.closed == [7]


@pytest.mark.parametrize("handle", [None, ctypes.c_void_p(-1).value])
def test_failed_snapshot_does_not_close_invalid_handle(handle):
    kernel = Kernel([], handle=handle)
    with pytest.raises(HookProcessError, match="INSPECTION_FAILED"):
        _wechat_process_ids(kernel)
    assert kernel.closed == []


def test_snapshot_is_closed_even_if_row_decode_raises():
    kernel = Kernel([])
    def fail(*_): raise ValueError("synthetic")
    kernel.Process32NextW = Function(fail)
    kernel.rows = iter([(1, "WeChat.exe")])
    with pytest.raises(ValueError): _wechat_process_ids(kernel)
    assert kernel.closed == [7]


def test_new_snapshot_observes_process_added_after_previous_check():
    assert _wechat_process_ids(Kernel([(1, "WeChat.exe")])) == (1,)
    assert _wechat_process_ids(Kernel([(1, "WeChat.exe"), (2, "WeChat.exe")])) == (1, 2)


@pytest.fixture
def inspection(monkeypatch, tmp_path):
    import hashlib
    import sys
    from chejin_worker_client import hook_process as module
    from chejin_worker_client.hook_config import HookDeployment
    dll = tmp_path / "component.dll"
    dll.write_bytes(b"synthetic DLL bytes")
    deployment = HookDeployment("test", r"C:\WeChat\WeChat.exe",
                                hashlib.sha256(dll.read_bytes()).hexdigest(), 8000, 8001)
    state = SimpleNamespace(pids=[10], created=[], loaded=True, owners=[10],
                            version=(3 << 16 | 9, 10 << 16 | 16), running=True,
                            started=[1.0, 1.0], arguments=deployment.launch_arguments,
                            exe=deployment.wechat_exe)
    class Process:
        def __init__(self, pid): self.pid = pid; state.created.append(pid)
        def exe(self): return state.exe
        def create_time(self): return state.started.pop(0) if len(state.started)>1 else state.started[0]
        def memory_maps(self): return [SimpleNamespace(path=str(dll))] if state.loaded else []
        def cmdline(self): return [state.exe, *state.arguments]
        def is_running(self): return state.running
    class ProcessError(Exception): pass
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=Process, Error=ProcessError,
        CONN_LISTEN="LISTEN", net_connections=lambda **_: [SimpleNamespace(status="LISTEN",
        laddr=SimpleNamespace(port=8000), pid=pid) for pid in state.owners]))
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(GetFileVersionInfo=lambda *_:
        {"FileVersionMS": state.version[0], "FileVersionLS": state.version[1]}))
    monkeypatch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=ProcessError))
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(EnumWindows=lambda callback, arg: None))
    monkeypatch.setitem(sys.modules, "win32process", SimpleNamespace())
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(module, "_wechat_process_ids", lambda: tuple(state.pids))
    monkeypatch.setattr(module, "Path", lambda _: dll)
    monkeypatch.setattr(module, "pe_machine", lambda _: 0x14C)
    return module, deployment, state, dll


def test_inspection_constructs_only_matching_process_and_still_rehashes_dll(inspection):
    module, deployment, state, dll = inspection
    assert module.inspect_windows_process(deployment).pid == 10
    assert state.created == [10]
    dll.write_bytes(b"changed")
    with pytest.raises(HookProcessError, match="HASH_OR_LOAD"):
        module.inspect_windows_process(deployment)


@pytest.mark.parametrize("pids", [[], [10, 20]])
def test_missing_or_second_wechat_remains_blocked(inspection, pids):
    module, deployment, state, _ = inspection
    state.pids = pids
    with pytest.raises(HookProcessError, match="MISSING_OR_AMBIGUOUS"):
        module.inspect_windows_process(deployment)


@pytest.mark.parametrize("field,value,code", [
    ("loaded", False, "HASH_OR_LOAD"), ("owners", [20], "PORT_OWNER"),
    ("owners", [], "PORT_OWNER"), ("arguments", [], "ARGUMENTS"),
    ("running", False, "CHANGED_DURING"), ("started", [1.0, 2.0], "CHANGED_DURING"),
    ("version", (4 << 16, 0), "VERSION_MISMATCH"),
])
def test_full_process_guards_survive_enumeration_optimization(inspection, field, value, code):
    module, deployment, state, _ = inspection
    setattr(state, field, value)
    with pytest.raises(HookProcessError, match=code):
        module.inspect_windows_process(deployment)


@pytest.mark.parametrize('change',['none','pid_reused','exe','stopped','denied'])
def test_live_generation_checks_only_target_process(inspection,monkeypatch,change):
    module,deployment,state,dll=inspection
    snapshot=module.inspect_windows_process(deployment)
    state.created.clear()
    if change=='pid_reused':state.started=[2.0]
    elif change=='exe':state.exe=r'C:\Other\WeChat.exe'
    elif change=='stopped':state.running=False
    elif change=='denied':
        import sys
        psutil=sys.modules['psutil']
        def denied(pid):raise psutil.Error('denied')
        monkeypatch.setattr(psutil,'Process',denied)
    if change=='none':
        module.inspect_windows_generation(snapshot)
        assert state.created==[snapshot.pid]
    else:
        with pytest.raises(HookProcessError,match='GENERATION_CHANGED'):
            module.inspect_windows_generation(snapshot)
