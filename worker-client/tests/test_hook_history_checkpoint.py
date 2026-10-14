"""An immediate pre-claim reuse is not a physical-send authorization."""
from dataclasses import replace
from types import SimpleNamespace
import uuid
import pytest
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime, history_read_cycle
from chejin_worker_client.hook_inbox import HookReceiveScope
import chejin_worker_client.hook_formal_runtime as module


@pytest.fixture
def checkpoint(monkeypatch):
    runtime=HookFormalRuntime.__new__(HookFormalRuntime)
    runtime.credentials=SimpleNamespace(worker_id=str(uuid.uuid4()),client_instance_id='client')
    runtime.process=SimpleNamespace(generation='generation')
    runtime.scope=HookReceiveScope('wxid_sales',str(uuid.uuid4()),'generation',1234)
    binding=dict(binding_id=str(uuid.uuid4()),binding_revision=1,peer_wxid='wxid_customer',
                 conversation_id='conversation',history_since_create_time='1')
    runtime._history_checkpoint=None
    barrier=[(3,0)];clock=[10.0];reads=[]
    runtime.barrier=lambda b:barrier[0]
    runtime.sync=lambda b,**kw:reads.append(b.copy()) or {'fresh_read':True}
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    result={'history_ready':True,'history_state_digest':'digest','nested':{'ok':1}}
    with history_read_cycle(runtime):
        runtime._remember_history(binding,result,(3,0),10.0)
        yield runtime,binding,barrier,clock,reads,result


def test_immediate_reuse_is_immutable_and_consumed_once(checkpoint):
    rt,b,_,_,reads,result=checkpoint
    result['nested']['ok']=99
    assert rt._history_before_claim(b)['nested']['ok']==1 and not reads
    assert rt._history_before_claim(b)=={'fresh_read':True} and len(reads)==1


@pytest.mark.parametrize('change',['age','clock_back','new_callback','pending','revision','peer','binding','conversation','since','process','account','worker','client'])
def test_changed_context_or_age_requires_fresh_read(checkpoint,change):
    rt,b,barrier,clock,reads,_=checkpoint
    if change=='age':clock[0]=12.001
    elif change=='clock_back':clock[0]=9.99
    elif change=='new_callback':barrier[0]=(4,0)
    elif change=='pending':barrier[0]=(3,1)
    elif change=='revision':b['binding_revision']=2
    elif change=='peer':b['peer_wxid']='wxid_other'
    elif change=='binding':b['binding_id']=str(uuid.uuid4())
    elif change=='conversation':b['conversation_id']='other'
    elif change=='since':b['history_since_create_time']='2'
    elif change=='process':rt.process.generation='new'
    elif change=='account':rt.scope=replace(rt.scope,account_epoch=str(uuid.uuid4()))
    elif change=='worker':rt.credentials.worker_id=str(uuid.uuid4())
    elif change=='client':rt.credentials.client_instance_id='other'
    assert rt._history_before_claim(b)=={'fresh_read':True} and len(reads)==1


@pytest.mark.parametrize('result',[{}, {'history_ready':False}, {'history_ready':True},
    {'history_ready':False,'history_state_digest':'digest'}])
def test_incomplete_capture_cannot_be_remembered(checkpoint,result):
    rt,b,_,_,reads,_=checkpoint;rt._history_checkpoint=None
    rt._remember_history(b,result,(3,0),10)
    rt._history_before_claim(b)
    assert len(reads)==1


def test_fault_during_barrier_never_returns_saved_evidence(checkpoint):
    rt,b,*_=checkpoint
    def fault(_):raise ValueError('HOOK_LOCAL_PERSISTENCE_FAULT')
    rt.barrier=fault
    with pytest.raises(ValueError,match='PERSISTENCE'):rt._history_before_claim(b)
    assert rt._history_checkpoint is None


@pytest.mark.parametrize('exit_kind',['return','backend_error','pause_resume'])
def test_checkpoint_is_cleared_on_dispatch_exit(checkpoint,exit_kind):
    rt,b,barrier,clock,reads,result=checkpoint
    # Finish the fixture's current cycle, then exercise independent dispatches.
    rt._history_cycle_token=None
    def dispatch():
        with history_read_cycle(rt):
            rt._remember_history(b,result,(3,0),10)
            if exit_kind=='backend_error':raise RuntimeError('backend unavailable')
            return
    if exit_kind=='backend_error':
        with pytest.raises(RuntimeError):dispatch()
    else:dispatch()
    assert rt._history_checkpoint is None
    with history_read_cycle(rt):
        assert rt._history_before_claim(b)=={'fresh_read':True}
    assert len(reads)==1


def test_fallback_capture_cannot_seed_a_later_claim(checkpoint):
    rt,b,_,_,_,result=checkpoint;rt._history_checkpoint=None
    def sync(binding,**kwargs):
        rt._remember_history(binding,result,(3,0),10)
        return result
    rt.sync=sync
    assert rt._history_before_claim(b)==result
    assert rt._history_checkpoint is None


def test_pause_and_resume_revision_invalidates_same_cycle(checkpoint):
    rt,b,_,_,reads,result=checkpoint
    rt.lifecycle_runner=SimpleNamespace(_run_status_revision=1,binding=SimpleNamespace(run_status='running'))
    rt._remember_history(b,result,(3,0),10)
    # A quick pause/resume can end in running again; its revision still differs.
    rt.lifecycle_runner._run_status_revision=3
    assert rt._history_before_claim(b)=={'fresh_read':True} and len(reads)==1
