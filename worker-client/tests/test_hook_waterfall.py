from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import os,json,importlib.util
import pytest
from chejin_worker_client.hook_progress import HookProgressTimeline,emit_progress,PHASES
from chejin_worker_client import startup_check_state as checks
from test_hook_native_sender import setup
from test_hook_gateway import lab
from test_web_ui_binding_behavior import _headless_web_ui_module,WebUiBindingBehaviorTest
from chejin_worker_client.models import Binding,WorkerProfile


def event(phase,state='current',context='task',note=None):
    return dict(event='hook_progress',context=context,phase=phase,state=state,time='14:00:00',note=note)


def test_waterfall_preserves_previous_tasks_and_does_not_auto_mark_done():
    t=HookProgressTimeline();t.apply(event('confirm_once','done',note='recipient'))
    t.apply(event('bind','waiting',note='recipient'));t.apply(event('decision',context='batch'))
    assert [x['state'] for x in t.model()]==['done','waiting','current']
    t.apply(event('bind','done'));assert [x['state'] for x in t.model()]==['done','done','current']
    for _ in range(1000):t.apply(event('decision',context='batch'))
    assert len(t.model())==3
    t.apply(event('decision','done',context='batch'));t.apply(event('decision',context='batch'))
    assert t.model()[-1]['state']=='done'
    t.apply(event('decision',context='batch-next'));assert len(t.model())==4


def test_error_retry_keeps_failed_row_and_bounded_history():
    t=HookProgressTimeline(max_steps=5)
    t.apply(event('history','error'));t.apply(event('history'))
    assert [x['state'] for x in t.model()]==['error','current']
    for i in range(10):t.apply(event('history','done',context=str(i)))
    assert len(t.model())==5 and len(t.latest)<=5


def test_untrusted_payload_and_presentation_failure_do_not_change_actions():
    t=HookProgressTimeline();t.apply(event('raw-token-should-not-render','current'))
    assert t.model()==[]
    t.apply(event('decision',note='raw-token-should-not-render'))
    assert 'raw-token' not in json.dumps(t.model())
    runner=SimpleNamespace(on_runtime_process=Mock(side_effect=RuntimeError('display unavailable')))
    emit_progress(runner,'batch','decision');runner.on_runtime_process.assert_called_once()


def test_network_wait_completes_only_after_real_reconnect_event():
    t=HookProgressTimeline();t.apply(event('connection_wait','done',context='connection'))
    assert not t.model()
    t.apply(event('connection_wait','waiting',context='connection',note='connection_wait'))
    t.apply(event('ready','done',context='startup'))
    assert t.model()[0]['state']=='waiting'
    t.apply(event('connection_wait','done',context='connection'))
    assert [x['title'] for x in t.model()]==['运行环境检查通过']


def test_real_native_send_progress_does_not_imply_delivery_and_no_extra_call(lab,tmp_path):
    server,gateway,process,scope,sender,permit,client,refresh,waits=setup(lab,tmp_path)
    events=[];sender.progress=lambda *args:events.append(args)
    sender.execute(permit,process,scope,refresh)
    assert [(x[0],x[1]) for x in events]==[('send_delay','current'),('send_delay','done'),('pre_send','current'),('pre_send','done'),('send_submit','current'),('send_submit','done')]
    assert not any(x[0]=='send_confirm' for x in events)
    assert sum(x[0]=='/SendTextMsg' for x in server.requests)==1 and waits==[1.25]


def c1_module():
    p=Path(__file__).resolve().parents[1]/'omniauto-rpa/apps/wechat_ai_customer_service/tests/test_hook_c1_flow.py'
    spec=importlib.util.spec_from_file_location('waterfall_c1_replay',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


@pytest.mark.parametrize('mode',['normal','unknown','display_failure'])
def test_actual_c1_replay_progress_matches_actions_and_preserves_capture_count(tmp_path,mode):
    m=c1_module();ui=m.ReplayUI();events=[]
    if mode=='unknown':ui.fail='observe'
    ui.on_progress=Mock(side_effect=RuntimeError('display failure')) if mode=='display_failure' else lambda *args:events.append(args)
    result=m.f.run_flow(ui,phone='13800000003',verify_message='您好我是测试销售',remark_name='测试 CJH5N28',remark_code='CJH5N28',journal=m.f.SubmitJournal(tmp_path/'submit.json'))
    assert ui.actions.count('confirm_invite_once')==1 and len(ui.captures)==7
    if mode=='unknown':assert not any(p=='confirm_once' and s=='done' for p,s,*_ in events)
    else:assert result['status']=='submitted_pending_recipient'
    if mode=='normal':
        phases=[p for p,s,*_ in events if s=='current']
        assert phases==['search_empty','query_verified_and_network_lookup','contact_result','invite_form_fields','verify_remark_shortcode','confirm_once','cleanup']


def test_actual_worker_waterfall_state_artifacts(monkeypatch):
    with _headless_web_ui_module() as module:
        monkeypatch.setattr(module,'_log_rows',lambda:[]);monkeypatch.setattr(module,'latest_incident',lambda:None);monkeypatch.setattr(module,'lock_summary',lambda:{})
        w=WebUiBindingBehaviorTest._window(module,Binding('worker','synthetic-token','client','running'))
        w.connection_status='bound';w._publish=Mock()
        w.profile=WorkerProfile('worker','测试销售','running',rpa_component_status='ready',wechat_status='logged_in')
        w.runner.hook_services=SimpleNamespace(startup_check_state=checks.passed)
        states=[]
        for phase in ['environment','ready','invite_received','invite_prepare','search_empty','query_verified_and_network_lookup','contact_result','invite_form_fields','verify_remark_shortcode','confirm_once','cleanup','invite_receipt','bind','history']:
            w.on_runtime_process(event(phase,'done'))
        w.on_runtime_process(event('decision',note='model_wait'));states.append(w._state())
        w.on_runtime_process(event('decision','done'))
        for phase in ['reply_ready','send_conditions','send_delay','pre_send','send_submit','send_confirm','finished']:w.on_runtime_process(event(phase,'done'))
        states.append(w._state())
        w.binding.run_status='paused';states.append(w._state())
        w.binding.run_status='faulted';w.runner.hook_services.startup_check_state=lambda:checks.failed('HOOK_BACKEND_NETWORK_FAILED');states.append(w._state())
        assert len(states[0]['model']['hookProcessSteps'])==15 and len(states[1]['model']['hookProcessSteps'])==22
        assert states[0]['model']['hookProcessSteps'][-1]['state']=='current'
        assert all(s['state']=='done' for s in states[1]['model']['hookProcessSteps'])
        target=os.environ.get('CHEJIN_WATERFALL_UI_STATES')
        if target:Path(target).write_text(json.dumps(states,ensure_ascii=False,indent=2))
