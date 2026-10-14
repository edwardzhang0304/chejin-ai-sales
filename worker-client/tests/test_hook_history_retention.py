from pathlib import Path
import uuid,hashlib,json,os
from datetime import datetime,timedelta,timezone
import pytest
from chejin_worker_client.shared_rules import hook_contract_rules as rules
from chejin_worker_client.hook_formal_c1 import _save
from chejin_worker_client.hook_retention import retire_history_artifacts


def record(root,*,ready=True,shared=None,age=40,raw="synthetic private message"):
    cid=str(uuid.uuid4());payload={'raw_utf8':raw,'query':{},'generation':'test'}
    name=shared or 'history-captures/'+rules.digest(payload)+'.json'
    if not (root/name).exists():_save(root/name,payload)
    coverage={'synthetic':cid};cname=f'history-coverage/{cid}.json';_save(root/cname,coverage)
    index={'schema_version':'hook-history-artifacts-v1','capture_id':cid,'coverage_digest':rules.digest(coverage),'files':{x:hashlib.sha256((root/x).read_bytes()).hexdigest() for x in (name,cname)}}
    ip=root/'history-artifact-index'/f'{cid}.json';_save(ip,index)
    ack={'capture_id':cid,'history_ready':ready,'coverage_digest':rules.digest(coverage)}
    _save(root/'history-receipts'/f'{cid}.json',ack)
    stamp=(datetime.now(timezone.utc)-timedelta(days=age)).timestamp()
    for p in (root/name,root/cname,ip):os.utime(p,(stamp,stamp))
    return cid,name,ip


def test_acked_old_artifacts_retire_but_original_ack_and_hashes_survive(tmp_path):
    cid,name,ip=record(tmp_path);before=ip.read_bytes();ack=tmp_path/'history-receipts'/f'{cid}.json'
    result=retire_history_artifacts(tmp_path,retention_days=30)
    assert result['retired_captures']==1 and result['released_bytes']>0
    assert not (tmp_path/name).exists() and not (tmp_path/'history-coverage'/f'{cid}.json').exists()
    assert ip.read_bytes()==before and ack.is_file()
    assert retire_history_artifacts(tmp_path,retention_days=30)['retired_captures']==0

@pytest.mark.parametrize('hold',['unconfirmed','old_without_digest','unknown_send','recent_reference','symlink','changed'])
def test_unresolved_recent_and_changed_evidence_is_never_deleted(tmp_path,hold):
    cid,name,ip=record(tmp_path,ready=hold!='unconfirmed')
    ack=tmp_path/'history-receipts'/f'{cid}.json'
    if hold=='old_without_digest':
        value=rules.loads(ack.read_bytes());value.pop('coverage_digest');ack.write_text(rules.canonical(value))
    elif hold=='unknown_send':
        action=str(uuid.uuid4());_save(tmp_path/'sends'/'worker'/'epoch'/action/'intent.json',{'reply_action_id':action})
        _save(tmp_path/'send-receipts'/f'{action}.json',{'local_send_observed':False})
    elif hold=='recent_reference':record(tmp_path,shared=name,age=0)
    elif hold=='changed':
        target=tmp_path/name;stamp=target.stat().st_mtime;target.write_text('changed');os.utime(target,(stamp,stamp))
    elif hold=='symlink':
        outside=tmp_path/'outside.json';outside.write_text('safe');(tmp_path/name).unlink();(tmp_path/name).symlink_to(outside)
    if hold in {'changed','symlink'}:
        with pytest.raises(ValueError,match='HOOK_RETENTION_EVIDENCE_CHANGED'):retire_history_artifacts(tmp_path,retention_days=30)
    else:retire_history_artifacts(tmp_path,retention_days=30)
    assert (tmp_path/name).exists() and ack.is_file() and ip.is_file()


def test_unprocessed_index_protects_shared_capture_in_bounded_pass(tmp_path):
    _,name,_=record(tmp_path);record(tmp_path,shared=name)
    result=retire_history_artifacts(tmp_path,retention_days=30,limit=1)
    assert result['retired_captures']==1 and (tmp_path/name).exists()


def test_history_capacity_refuses_new_raw_but_keeps_settlement_metadata(tmp_path):
    from chejin_worker_client.hook_retention import HistoryArtifactBudget
    budget=HistoryArtifactBudget(tmp_path,maximum_bytes=32,retention_days=30)
    budget.begin_capture(allow_retire=False)
    target=tmp_path/'history-captures'/('a'*64+'.json')
    with pytest.raises(ValueError,match='HOOK_HISTORY_STORAGE_CAPACITY_EXCEEDED'):
        budget.save(target,{'raw':'x'*50})
    assert not target.exists() and not budget.ready
    receipt=tmp_path/'history-receipts'/(str(uuid.uuid4())+'.json')
    budget.save(receipt,{'confirmed_settlement':'saved'},settlement=True)
    assert receipt.is_file() and not budget.ready


def test_capacity_retires_only_before_capture_and_retains_immutable_receipts(tmp_path):
    from chejin_worker_client.hook_retention import HistoryArtifactBudget
    cid,name,index=record(tmp_path,raw="x"*4000)
    maximum=sum(p.stat().st_size for p in tmp_path.rglob('*.json'))
    budget=HistoryArtifactBudget(tmp_path,maximum_bytes=maximum,retention_days=30)
    budget.begin_capture(allow_retire=False)
    assert not budget.ready and (tmp_path/name).is_file()
    budget.begin_capture(allow_retire=True)
    assert not (tmp_path/name).exists() and (tmp_path/'history-receipts'/f'{cid}.json').is_file()
    assert budget.ready


def test_storage_capacity_joins_existing_start_gate_without_auto_resume(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from chejin_worker_client.hook_worker_services import HookWorkerServices
    from chejin_worker_client.hook_retention import HistoryArtifactBudget
    from chejin_worker_client import startup_check_state
    budget=HistoryArtifactBudget(tmp_path,maximum_bytes=10,retention_days=30);budget.ready=False
    service=HookWorkerServices(SimpleNamespace(),None,intake_interval_seconds=1,projection_batch_size=1,
        dispatcher=SimpleNamespace(history_storage_ready=lambda:budget.ready))
    service._startup_check=startup_check_state.passed()
    assert not service.startup_check_state()['canStart']
    assert service.startup_check_state()['title']=='本机记录容量不足'
    budget.ready=True
    assert service.startup_check_state()['canStart']


@pytest.mark.parametrize('partial',[False,True])
def test_interrupted_retirement_revalidates_and_resumes_without_losing_ack(tmp_path,monkeypatch,partial):
    cid,name,index=record(tmp_path,raw='x'*4000)
    ack=tmp_path/'history-receipts'/f'{cid}.json';saved=ack.read_bytes()
    original=Path.unlink;seen=[]
    def fail_once(path,*args,**kwargs):
        if path.parent.name in {'history-captures','history-coverage'}:
            seen.append(path)
            if len(seen)==(2 if partial else 1):raise OSError('synthetic interruption')
        return original(path,*args,**kwargs)
    with monkeypatch.context() as m:
        m.setattr(Path,'unlink',fail_once)
        with pytest.raises(OSError):retire_history_artifacts(tmp_path,retention_days=30)
    assert not (index.parent/'completed'/index.name).exists()
    result=retire_history_artifacts(tmp_path,retention_days=30)
    assert result['retired_captures']==1
    assert not (tmp_path/name).exists() and not (tmp_path/'history-coverage'/f'{cid}.json').exists()
    assert ack.read_bytes()==saved and (index.parent/'completed'/index.name).is_file()
    assert retire_history_artifacts(tmp_path,retention_days=30)['retired_captures']==0


def test_resumed_retirement_holds_shared_file_newly_referenced(tmp_path,monkeypatch):
    cid,name,index=record(tmp_path,raw='x'*4000)
    original=Path.unlink
    with monkeypatch.context() as m:
        m.setattr(Path,'unlink',lambda path,*a,**kw: (_ for _ in ()).throw(OSError('interrupt')) if path==tmp_path/name else original(path,*a,**kw))
        with pytest.raises(OSError):retire_history_artifacts(tmp_path,retention_days=30)
    record(tmp_path,shared=name,age=0)
    retire_history_artifacts(tmp_path,retention_days=30)
    assert (tmp_path/name).is_file() and not (index.parent/'completed'/index.name).exists()


def test_reserved_reconciliation_proof_retires_only_after_confirmed_action(tmp_path):
    from chejin_worker_client.hook_retention import reconciliation_budget
    reserve=tmp_path/'history-reconcile';reserve.mkdir()
    cid,name,index=record(reserve,raw='x'*4000)
    action=str(uuid.uuid4());value=rules.loads(index.read_bytes());value['retained_reply_action_id']=action
    stamp=index.stat().st_mtime;index.write_text(rules.canonical(value));os.utime(index,(stamp,stamp))
    reconciliation_budget(tmp_path,retention_days=30)
    assert (reserve/name).is_file()
    _save(tmp_path/'send-receipts'/f'{action}.json',{'local_send_observed':True})
    reconciliation_budget(tmp_path,retention_days=30)
    assert not (reserve/name).exists() and (reserve/'history-receipts'/f'{cid}.json').is_file()
