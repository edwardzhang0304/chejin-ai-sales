"""Original task callback drives actual Web UI; no screenshots or physical calls."""
from types import SimpleNamespace
from unittest.mock import Mock
import uuid,threading
import pytest
from chejin_worker_client import storage,startup_check_state
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime,publish_current_task
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.models import Binding,Task,WorkerProfile
from test_hook_gateway import lab
from test_web_ui_binding_behavior import _headless_web_ui_module,WebUiBindingBehaviorTest


def window(module):
    w=WebUiBindingBehaviorTest._window(module,Binding('worker','synthetic-token','client','running'))
    w.connection_status='bound';w._publish=Mock()
    w.profile=WorkerProfile('worker','测试销售','running',rpa_component_status='ready',wechat_status='logged_in')
    w.runner.hook_services=SimpleNamespace(startup_check_state=startup_check_state.passed)
    w.runner.on_task=w.on_task
    return w


def test_confirmed_finish_clears_old_card_and_claim_publishes_reply(lab,tmp_path):
    with _headless_web_ui_module() as module:
        w=window(module);runner=w.runner;runner.api=SimpleNamespace(inflight_flow_id=None)
        invite=Task(str(uuid.uuid4()),'add_friend','running')
        publish_current_task(runner,invite)
        assert '添加通讯录邀请' in str(w._running_steps()) and w._screen()=='running'
        storage.begin_runtime_flow(invite.id,'task')
        rt=object.__new__(HookFormalRuntime);rt.lifecycle_runner=runner;rt.credentials=None
        rt.private_root=tmp_path/'runtime';rt.active_flow_id=invite.id;rt.active_task_id=invite.id
        reply={'id':str(uuid.uuid4()),'task_type':'chat_reply','status':'running','current_step':'hook_formal','lease_fencing_token':1}
        def call(*args,**kwargs):
            if args[2].endswith('/finish'):return {'finished':True}
            assert args[2].endswith('/claim');return reply
        rt.client=SimpleNamespace(call=call)
        rt.credentials=SimpleNamespace(worker_id='worker',client_instance_id='client')
        rt.finish(invite.id,invite.id,1)
        assert w.current_task is None and runner.current_task is None and w._screen()=='accepting-wait'
        assert storage.load_runtime_control()['inflight_flow_id'] is None
        rt.claim(reply,reply['id'],'conversation')
        assert w.current_task.id==reply['id'] and runner.current_task.id==reply['id']
        assert 'AI 自动回复' in str(w._running_steps()) and '添加通讯录邀请' not in str(w._running_steps())
        before=w._publish.call_count
        publish_current_task(runner,Task.from_api(reply))
        assert w._publish.call_count==before  # Polling does not duplicate task events.


def test_finish_transport_failure_does_not_fake_finished_task(lab,tmp_path):
    with _headless_web_ui_module() as module:
        w=window(module);runner=w.runner;runner.api=SimpleNamespace(inflight_flow_id='flow')
        task=Task(str(uuid.uuid4()),'add_friend','running');publish_current_task(runner,task)
        rt=object.__new__(HookFormalRuntime);rt.lifecycle_runner=runner;rt.credentials=SimpleNamespace(worker_id='worker',client_instance_id='client')
        rt.private_root=tmp_path;rt.active_flow_id=task.id;rt.active_task_id=task.id
        rt.client=SimpleNamespace(call=Mock(side_effect=RuntimeError('network wait')))
        with pytest.raises(RuntimeError,match='network wait'):rt.finish(task.id,task.id,1)
        assert w.current_task.id==task.id and runner.current_task.id==task.id and rt.active_flow_id==task.id
        assert not (tmp_path/'finished').exists()


def test_recovered_inventory_publishes_actual_task_and_verified_empty_clears(lab,tmp_path):
    with _headless_web_ui_module() as module:
        w=window(module);runner=w.runner;runner.api=SimpleNamespace(inflight_flow_id=None)
        stale=Task(str(uuid.uuid4()),'add_friend','running');publish_current_task(runner,stale)
        reply={'id':str(uuid.uuid4()),'task_type':'chat_reply','status':'running'}
        d=HookBusinessDispatcher(None,tmp_path,lambda *a:None,lambda *a:None,automatic_transcription_disabled=lambda:True)
        d.runtime=SimpleNamespace(active_flow_id=None,active_task_id=None,credentials=SimpleNamespace(worker_id='worker',client_instance_id='client'))
        intent=tmp_path/'flows'/f"{reply['id']}.json";intent.parent.mkdir();intent.write_text('{"flow_id":"'+reply['id']+'"}')
        d._adopt_flow(runner,{'inflight_flow':{'flow_id':reply['id'],'flow_kind':'chat_reply'},'inflight_task':reply})
        assert w.current_task.id==reply['id'] and w.current_task.task_type=='chat_reply'
        d._adopt_flow(runner,{'inflight_flow':{},'retained_flow_terminal':True})
        assert runner.current_task is None and w.current_task is None


def test_whole_dispatch_tick_does_not_reactivate_terminal_invite(lab,tmp_path):
    with _headless_web_ui_module() as module:
        w=window(module);runner=w.runner;runner.api=SimpleNamespace();runner.binding=w.binding
        runner.stop_event=threading.Event();runner._can_start_new_flow=lambda:True
        runner.can_pull_tasks=lambda:True;runner._worker_transaction_barrier_ready=lambda *a,**k:True
        scope=SimpleNamespace(self_wxid='wxid_sales',account_epoch='epoch',process_generation='generation')
        account={k:getattr(scope,k) for k in ('self_wxid','account_epoch','process_generation')}
        prepared=SimpleNamespace(registration=SimpleNamespace(scope=scope),process=SimpleNamespace(generation='generation'),account=None)
        services=SimpleNamespace(connection=SimpleNamespace(prepared=prepared,gateway=None),_credentials=None,startup_window_ready_for=lambda _:True)
        task={'id':str(uuid.uuid4()),'task_type':'add_friend','status':'pending'}
        class Client:
            def call(self,*args,**kwargs):
                if args[2].endswith('native-work'):
                    return {'schema_version':'hook-native-work-v1','observation_query_version':1,**account,'run_status':'running','inflight_flow':{},'inflight_task':None,
                        'bindings':[],'invitations':[],'retained_sends':[]}
                assert args[2].endswith('tasks/pull');return {'task':task,'mode':'pending'}
        class Runtime:
            def __init__(self,*args,**kwargs):
                self.account=account;self.credentials=SimpleNamespace(worker_id='worker',client_instance_id='client');self.scope=scope
                self.clock=SimpleNamespace(observe=lambda _:None);self.active_flow_id=self.active_task_id=None
            def invite(self,task,ui):
                # Model the actual acknowledged finish callback; the dispatcher
                # must not subsequently reactivate its terminal task card.
                publish_current_task(self.lifecycle_runner,None)
                return {'settlement':{'task':{**task,'status':'completed','result_code':'invite_sent'}}}
        d=HookBusinessDispatcher(Client(),tmp_path,lambda *a:None,lambda *a:None,
            automatic_transcription_disabled=lambda:True,runtime_factory=Runtime)
        d._housekeeping_due=float('inf')
        d.tick(runner,services)
        assert w.current_task is None and runner.current_task is None
        assert w.last_task.id==task['id'] and w.last_task.status=='completed' and w._screen()=='accepting-wait'
