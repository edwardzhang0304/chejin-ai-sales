"""Actual send producer/retry consumer/HTTP settlement, controlled desktop/model."""
import json
import pytest

from test_dynamic_composer_http import _drive_composer_http, http_api
from test_customer_interrupt_http import observe_async_generation
import dynamic_composer_desktop as fixture
from apps.wechat_ai_customer_service.adapters.pre_send_read_failure import ReadCallFailed


@pytest.mark.parametrize('scenario', ['once', 'twice', 'clear_error'])
def test_read_cleanup_retry_and_terminal_settlement(tmp_path, request, monkeypatch, scenario):
    from chejin_worker_client import storage, pre_send_read_recovery as recovery
    import test_c3_api as backend
    from app.models.wechat import WechatSessionBinding
    monkeypatch.setattr(storage,'APP_DIR',tmp_path/'worker')
    monkeypatch.setattr(storage,'DB_FILE',tmp_path/'worker/worker_client.sqlite3')
    native_frames=fixture.derived_frames
    monkeypatch.setattr(fixture,'REPLY','好的，我帮您看看')
    def frames(**kwargs):
        kwargs['new_kind']=''
        return native_frames(**kwargs,reply=fixture.REPLY,movement=0,reduction=0)
    monkeypatch.setattr(fixture,'derived_frames',frames)
    native_desktop=fixture.Desktop;desktops=[]
    class Desktop(native_desktop):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs,reply=fixture.REPLY)
            desktops.append(self)
    monkeypatch.setattr(fixture,'Desktop',Desktop)
    read=fixture.sidecar.build_send_fact_snapshot_from_frame;injected=[]
    def capture(*args,**kwargs):
        if kwargs.get('label')=='send_pre_trigger_context_reused' and (not injected or scenario=='twice'):
            injected.append(kwargs['label'])
            # Fault only the post-read-failure cleanup, never focus/typing.
            desktops[-1].cleanup_fails=scenario=='clear_error'
            raise ReadCallFailed(operation='read',reason='controlled pre-trigger read outage')
        return read(*args,**kwargs)
    monkeypatch.setattr(fixture.sidecar,'build_send_fact_snapshot_from_frame',capture)
    async_events=observe_async_generation(monkeypatch)
    runner,desktop,record=_drive_composer_http(tmp_path,request,monkeypatch,
        'sent' if scenario=='once' else 'failed',expected_send_calls=1 if scenario=='clear_error' else 2)
    pending=recovery.input_pending_records()
    record.update(scenario=scenario,injected=injected,async_events=async_events,
                  input_pending=pending,run_status=runner.binding.run_status)
    (tmp_path/'cleanup-http.json').write_text(json.dumps(record,ensure_ascii=False,indent=2,default=str))
    assert len(injected)==(2 if scenario=='twice' else 1)
    assert len(async_events['scheduled'])==len(async_events['executed'])==1
    assert runner.binding.run_status==('running' if scenario=='once' else 'faulted')
    assert bool(pending)==(scenario=='clear_error')
    assert all(x['label']!='send_program_draft_cleanup' for x in desktop.captures)
    with backend.SessionLocal() as db:
        assert db.query(backend.HandoffEvent).count()==0
        assert db.query(backend.SentAck).count()==1
        if scenario!='once':
            binding=db.query(WechatSessionBinding).one()
            assert binding.last_scan_snapshot['pre_send_read_pending']['status']=='pending'
    runner.stop_for_update(timeout_seconds=5)
