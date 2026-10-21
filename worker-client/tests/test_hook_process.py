import pytest

@pytest.mark.parametrize('case',['old_alive','old_exited','pid_reused','access_denied','enumeration_failed'])
def test_original_generation_exit_requires_complete_readonly_inspection(monkeypatch,case):
    import sys
    from types import SimpleNamespace
    from chejin_worker_client import hook_process as module
    class InspectionError(Exception):pass
    class Gone(InspectionError):pass
    original=module.process_generation(123,100.0,r'C:\\Work\\WeChat.exe')
    def ids():
        if case=='enumeration_failed':raise OSError('snapshot failed')
        return () if case=='old_exited' else (123,)
    def process(pid):
        if case=='access_denied':raise InspectionError('access denied')
        return SimpleNamespace(create_time=lambda:200.0 if case=='pid_reused' else 100.0,
            exe=lambda:r'C:\\Work\\WeChat.exe')
    monkeypatch.setattr(module.sys,'platform','win32')
    monkeypatch.setattr(module,'_wechat_process_ids',ids)
    monkeypatch.setitem(sys.modules,'psutil',SimpleNamespace(Process=process,Error=InspectionError,NoSuchProcess=Gone))
    if case in {'access_denied','enumeration_failed'}:
        with pytest.raises(module.HookProcessError,match='EXIT_UNCONFIRMED'):module.inspect_windows_generation_exited(original)
    else:assert module.inspect_windows_generation_exited(original)==(case!='old_alive')
