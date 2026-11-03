"""Disposable cache and real SQLite callback consumers; no Windows/DLL claims."""
import json
from types import SimpleNamespace
import pytest
from chejin_worker_client import storage
from chejin_worker_client.hook_history_cache import (
    HistoryReceiptCache, MAX_ENTRIES, MAX_BYTES, MAX_ENTRY_BYTES,
)
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.hook_inbox import HookReceiveScope, NativeInbox
from chejin_worker_client.hook_projection import NativeProjectionStore
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime


def test_cache_bounded_over_many_customers_and_byte_heavy_receipts():
    now=[100.0]; cache=HistoryReceiptCache(clock=lambda:now[0])
    for size in (100, 200_000):
        for i in range(200):
            receipt={'history_ready':True,'synthetic':'x'*size}
            cache[(i,1)]=((i,0),now[0],receipt)
            assert cache[(i,1)][2]==receipt
            assert len(cache)<=MAX_ENTRIES and cache.payload_bytes<=MAX_BYTES
        cache.clear()
    cache['oversize']=((1,0),now[0],{'synthetic':'x'*(MAX_ENTRY_BYTES+1)})
    assert not cache and cache.payload_bytes==0


def test_cache_expiration_retirement_and_object_ownership():
    now=[100.0]; cache=HistoryReceiptCache(clock=lambda:now[0])
    receipt={'nested':{'state':'ready'}}
    cache['A']=((1,0),now[0],receipt)
    receipt['nested']['state']='changed by caller'
    fetched=cache['A']; fetched[2]['nested']['state']='changed by consumer'
    assert cache['A'][2]['nested']['state']=='ready'
    cache['B']=((2,0),now[0],{'history_ready':False})
    cache.prune({'B'})
    assert list(cache)==['B']
    now[0]+=30
    assert cache.get('B') is None and cache.payload_bytes==0


def test_real_history_read_preserves_result_when_cache_bypassed_or_failed(tmp_path):
    dispatcher=HookBusinessDispatcher(None,tmp_path,None,None,automatic_transcription_disabled=lambda:True)
    binding={'binding_id':'synthetic-A','binding_revision':1}
    reply={'history_ready':True,'synthetic':'x'*(MAX_ENTRY_BYTES+1)}
    dispatcher.runtime=SimpleNamespace(scope=SimpleNamespace(process_generation='generation-A'),
        barrier=lambda _: (1,0),sync=lambda _:reply)
    assert dispatcher._read_history(binding) is reply
    assert not dispatcher._sync_cache
    dispatcher.runtime.sync=lambda _: {'history_ready':True}
    dispatcher._read_history(binding)
    assert dispatcher._sync_cache
    def fail(_):raise ValueError('HOOK_HISTORY_CALLBACK_BARRIER_CHANGED')
    dispatcher.runtime.sync=fail
    with pytest.raises(ValueError,match='BARRIER_CHANGED'):dispatcher._read_history(binding)
    assert not dispatcher._sync_cache
    # Another customer is still read normally; cache admission is not a global gate.
    dispatcher.runtime.sync=lambda _: {'history_ready':True}
    assert dispatcher._read_history({**binding,'binding_id':'synthetic-B'})['history_ready']


@pytest.fixture
def callbacks(tmp_path,monkeypatch):
    monkeypatch.setattr(storage,'APP_DIR',tmp_path)
    monkeypatch.setattr(storage,'DB_FILE',tmp_path/'worker_client.sqlite3')
    scope=HookReceiveScope('wxid_synthetic','synthetic-epoch','synthetic-generation',12345)
    inbox=NativeInbox(max_body_bytes=256*1024,max_inbox_bytes=16*1024*1024)
    projector=NativeProjectionStore(max_projection_bytes=32*1024*1024,max_entries=100)
    rt=HookFormalRuntime.__new__(HookFormalRuntime);rt.scope=scope;rt.fault_check=lambda:False
    def put(*,peer='wxid_peer',mid='101',audio=False,text='synthetic'):
        message={'msgsvrid':mid,'msgtype':'34' if audio else '1','msg':text,
                 'fromid':peer,'toid':scope.self_wxid,'fromtype':'1'}
        if audio:message.update(fromid='',toid='',msg='',voice_data='02',voice_len='1')
        body={'ServerPort':str(scope.hook_api_port),'selfwxid':scope.self_wxid,
              'sendorrecv':'2','msgnumber':'1','msglist':[message]}
        return inbox.persist(json.dumps(body).encode(),scope)
    def project():
        while projector.project_next(scope) is not None:pass
    return rt,put,project


@pytest.mark.parametrize('projected',[False,True])
def test_streamed_barrier_includes_audio_before_parent_and_excludes_other_peers(callbacks,projected):
    rt,put,project=callbacks
    assert rt.barrier()==(0,0)
    put(audio=True);put();put(peer='wxid_other',mid='102');put(audio=True,mid='999')
    if projected:project()
    assert rt.barrier()==(4,0 if projected else 4)
    assert rt.barrier({'peer_wxid':'wxid_peer','voice_jobs':[]})==(2,0 if projected else 2)
    # Original durable bytes survive the scan and a new runtime (restart).
    restarted=HookFormalRuntime.__new__(HookFormalRuntime)
    restarted.scope=rt.scope;restarted.fault_check=lambda:False
    assert restarted.barrier()==rt.barrier()


def test_unrelated_corrupt_record_is_still_rejected_after_streaming(callbacks):
    rt,put,project=callbacks
    put();put(peer='wxid_other',mid='102');project()
    with storage.db_connection() as db:
        db.execute("UPDATE hook_raw_inbox SET raw_body=? WHERE receive_seq=2",(b'{}',));db.commit()
    with pytest.raises(ValueError,match='RAW_EVIDENCE_CHANGED'):
        rt.barrier({'peer_wxid':'wxid_peer','voice_jobs':[]})


def test_display_retirement_drops_finished_rounds_but_keeps_work_and_open_handoff(tmp_path):
    from chejin_worker_client.hook_progress import register_progress,forget_completed_progress_group
    dispatcher=HookBusinessDispatcher(None,tmp_path,None,None,automatic_transcription_disabled=lambda:True)
    runner=SimpleNamespace(on_runtime_process=lambda event:None)
    for i in range(200):
        bid=f'synthetic-round-{i}'
        dispatcher._mark_batch_presented(bid)
        dispatcher._batch_stages[bid]='completed'
        register_progress(runner,bid,group=bid,kind='batch')
        forget_completed_progress_group(runner,bid)
    for bid in ('visible','running','handoff','retained-send'):
        dispatcher._mark_batch_presented(bid);dispatcher._batch_stages[bid]='generating'
    register_progress(runner,'visible',group='visible',kind='batch')
    dispatcher._open_handoffs_by_binding['A']={'handoff-id':'handoff'}
    dispatcher._reply_owners_by_binding.update(A=('human',1,True),retired=('human',1,True))
    dispatcher._friend_holds_by_binding.update(A='hold',retired='old-hold')
    dispatcher._prune_presentation_state(runner,{'bindings':[{'binding_id':'A','batch_ids':['running']}],
                                                'inflight_batch_id':'retained-send'})
    assert dispatcher._presented_batches=={'visible','running','handoff','retained-send'}
    assert set(dispatcher._batch_stages)==dispatcher._presented_batches
    assert dispatcher._ever_presented_batch
    assert set(dispatcher._reply_owners_by_binding)==set(dispatcher._friend_holds_by_binding)=={'A'}
