"""Real local HTTP/SQLite and action lock; Windows process/setting facts are fixtures."""
from dataclasses import replace
import json
import time

import pytest

from test_hook_gateway import lab
from test_hook_backend import peer as backend_peer
from test_hook_connection import setup,callback
from chejin_worker_client import storage,ui_lock
from chejin_worker_client.hook_inbox import NativeInbox
from chejin_worker_client.hook_voice import HookVoiceAudio,HookVoiceTranscriber,capture_voice_audio,process_voice_call_unresolved
from chejin_worker_client.hook_gateway import HookLocalError
from chejin_worker_client.update_data_snapshot import protected_update_snapshot,assert_protected_update_snapshot

MID='1178383076014376868'
AUDIO=b'\x02#!SILK_V3'+bytes(range(32))
PEER='wxid_customer_A'


def persist(lab,*,metadata=False,audio=False,mid=MID,length=None,fromid=None,data=AUDIO,scope=None,fields=None):
    scope=scope or lab[3]
    message={'msgsvrid':mid,'msgtype':'34','fromtype':'1','fromid':PEER if fromid is None else fromid,'toid':scope.self_wxid}
    if metadata:message['msg']=f'<msg><voicemsg length="{length if length is not None else len(data)}"/></msg>'
    if audio:message.update(voice_data=data.hex(),voice_len=str(len(data)))
    if fields:message.update(fields)
    event={'ServerPort':str(scope.hook_api_port),'selfwxid':scope.self_wxid,'sendorrecv':'2','msgnumber':'1','msglist':[message]}
    return NativeInbox(max_body_bytes=65536,max_inbox_bytes=131072).persist(json.dumps(event).encode(),scope)


def capture(lab,scope=None):
    return capture_voice_audio(scope or lab[3],PEER,MID,max_audio_bytes=4096)


def voice(lab):
    return HookVoiceAudio(lab[3],PEER,MID,AUDIO)


def transcriber():
    return HookVoiceTranscriber(max_audio_bytes=4096,max_text_bytes=2048)


def test_audio_before_metadata_waits_then_duplicate_evidence_resolves_one_parent(lab):
    persist(lab,audio=True,fromid='')
    assert capture(lab) is None
    persist(lab,metadata=True);persist(lab,audio=True)
    result=capture(lab)
    assert result.native_msg_id==MID and result.audio==AUDIO and result.peer_wxid==PEER
    assert capture(lab,replace(lab[3],account_epoch='different-epoch')) is None


@pytest.mark.parametrize('case,code',[('length','LENGTH_MISMATCH'),('parent','PARENT_IDENTITY_CONFLICT'),('audio','AUDIO_AMBIGUOUS')])
def test_conflicting_parent_or_audio_stops_before_any_voice_call(lab,case,code):
    persist(lab,metadata=True,length=len(AUDIO)+1 if case=='length' else None,fromid='wxid_wrong' if case=='parent' else None)
    persist(lab,audio=True)
    if case=='audio':persist(lab,audio=True,data=AUDIO+b'x')
    with pytest.raises(ValueError,match=code):capture(lab)
    assert not lab[0].requests


def test_raw_evidence_tampering_stops_before_call(lab):
    persist(lab,metadata=True);persist(lab,audio=True)
    with storage.db_connection() as conn:
        conn.execute("UPDATE hook_raw_inbox SET raw_body=? WHERE receive_seq=1",(b'changed',));conn.commit()
    with pytest.raises(ValueError,match='RAW_EVIDENCE_CHANGED'):capture(lab)
    assert not lab[0].requests


def test_success_calls_exact_tested_payload_once_and_retains_parent_on_restart(lab):
    server,gateway,process,*_=lab
    server.voice_response={'status':1,'text':'预算15万元，先不要报价🙂'}
    job=voice(lab)
    result=transcriber().transcribe_once(gateway,process,job,automatic_transcription_disabled=lambda:True)
    assert result['state']=='transcribed' and result['native_msg_id']==MID and result['text']==server.voice_response['text']
    assert not process_voice_call_unresolved(process.generation)
    repeated=transcriber().transcribe_once(gateway,process,job,automatic_transcription_disabled=lambda:False)
    assert repeated==result
    calls=[body for path,body,_ in server.requests if path=='/Voice2Text']
    assert calls==[{'voice_hex':AUDIO.hex().upper(),'msgsvrid':MID}]
    assert all(state['locked'] for state in server.lock_observations)
    assert not ui_lock.lock_summary()['locked']


@pytest.mark.parametrize('case,code',[('hidden','WINDOW_NOT_AVAILABLE'),('minimized','WINDOW_NOT_AVAILABLE'),('setting','SETTING_UNCONFIRMED'),('account','ACCOUNT_MISMATCH')])
def test_native_window_setting_and_account_preconditions_call_nothing(lab,case,code):
    server,gateway,process,*_=lab
    if case=='hidden':server.current_process=replace(process,visible_window_count=0)
    if case=='minimized':server.current_process=replace(process,minimized=True)
    if case=='account':server.wxid='wxid_other_account'
    with pytest.raises(HookLocalError,match=code):
        transcriber().transcribe_once(gateway,process,voice(lab),automatic_transcription_disabled=lambda:case!='setting')
    assert not any(path=='/Voice2Text' for path,*_ in server.requests)
    assert not process_voice_call_unresolved(process.generation)


def test_timeout_is_durable_and_blocks_all_active_calls_in_same_process(lab):
    server,gateway,process,*_=lab
    server.voice_response={'status':1,'text':'late result'}
    gateway.timeout_seconds=.1
    server.on_request=lambda path:time.sleep(.25) if path=='/Voice2Text' else None
    job=voice(lab)
    with pytest.raises(HookLocalError,match='TRANSPORT_FAILED') as caught:
        transcriber().transcribe_once(gateway,process,job,automatic_transcription_disabled=lambda:True)
    assert caught.value.operation=='Voice2Text' and caught.value.transport_kind=='timeout'
    saved=transcriber().transcribe_once(gateway,process,job,automatic_transcription_disabled=lambda:True)
    assert saved['state']=='uncertain' and saved['text'] is None
    before=len(server.requests)
    with pytest.raises(HookLocalError,match='QUARANTINED'):gateway.login_status(process)
    with pytest.raises(HookLocalError,match='QUARANTINED'):gateway.friend_list(process,job.scope)
    with pytest.raises(HookLocalError,match='QUARANTINED'):
        with gateway._locked(process,'hook_formal_c1'):pytest.fail('Unresolved voice allowed C1')
    assert len(server.requests)==before
    assert [path for path,*_ in server.requests].count('/Voice2Text')==1
    new=replace(process,started_at=process.started_at+1);server.current_process=new
    server.on_request=None
    assert gateway.login_status(new).logged_in
    # New process permits other work, never replays the same unknown audio.
    assert transcriber().transcribe_once(gateway,new,job,automatic_transcription_disabled=lambda:True)==saved


def test_explicit_failure_does_not_isolate_the_whole_account_or_retry_audio(lab):
    server,gateway,process,*_=lab
    server.voice_response={'status':0,'text':''}
    result=transcriber().transcribe_once(gateway,process,voice(lab),automatic_transcription_disabled=lambda:True)
    assert result['state']=='failed' and not process_voice_call_unresolved(process.generation)
    assert gateway.login_status(process).logged_in
    assert transcriber().transcribe_once(gateway,process,voice(lab),automatic_transcription_disabled=lambda:True)==result
    assert [path for path,*_ in server.requests].count('/Voice2Text')==1


def test_unexpected_response_is_unknown_not_guessed_or_an_account_wide_handoff(lab):
    server,gateway,process,*_=lab
    server.voice_response={'status':1,'text':''}
    with pytest.raises(HookLocalError,match='VOICE_RESPONSE_INVALID'):
        transcriber().transcribe_once(gateway,process,voice(lab),automatic_transcription_disabled=lambda:True)
    assert process_voice_call_unresolved(process.generation)
    assert transcriber()._read(voice(lab))['text'] is None


def test_failed_result_save_keeps_transcribing_journal_as_stop_gate(lab,monkeypatch):
    server,gateway,process,*_=lab
    server.voice_response={'status':1,'text':'真实转写'}
    instance=transcriber()
    def disk_failed(*args,**kwargs):raise OSError('synthetic disk failure')
    monkeypatch.setattr(instance,'_finish',disk_failed)
    with pytest.raises(OSError):instance.transcribe_once(gateway,process,voice(lab),automatic_transcription_disabled=lambda:True)
    assert process_voice_call_unresolved(process.generation)
    assert instance._read(voice(lab))['state']=='transcribing'
    with pytest.raises(HookLocalError,match='QUARANTINED'):gateway.login_status(process)


def test_quarantine_keeps_same_approved_receiver_saving_callbacks(lab,backend_peer):
    manager,credentials,state,_=setup(lab,backend_peer)
    try:
        prepared=manager.prepare(credentials,state)
        job=replace(voice(lab),scope=prepared.registration.scope)
        transcriber()._reserve(job)  # Simulate interruption after durable begin.
        with pytest.raises(HookLocalError,match='QUARANTINED'):manager.prepare(credentials,state)
        assert manager.prepared is prepared
        callback(manager)
        with storage.db_connection() as conn:
            assert conn.execute('SELECT COUNT(*) FROM hook_raw_inbox').fetchone()[0]==1
    finally:manager.close()


def test_update_snapshot_protects_transcript_and_unknown_call_stop_gate(lab):
    server,gateway,process,*_=lab
    server.voice_response={'status':1,'text':'原转写'}
    transcriber().transcribe_once(gateway,process,voice(lab),automatic_transcription_disabled=lambda:True)
    baseline=protected_update_snapshot(data_dir=storage.APP_DIR,digest_key='synthetic-private-update-key')
    assert baseline['hook_voice_v1']['rows']==1
    assert_protected_update_snapshot(baseline,data_dir=storage.APP_DIR,digest_key='synthetic-private-update-key')
    assert '原转写' not in json.dumps(baseline,ensure_ascii=False)
    with storage.db_connection() as conn:
        conn.execute("UPDATE hook_voice_attempts SET text='changed'");conn.commit()
    with pytest.raises(RuntimeError,match='PROTECTED'):assert_protected_update_snapshot(baseline,data_dir=storage.APP_DIR,digest_key='synthetic-private-update-key')


@pytest.mark.parametrize('status',[2,True,'1'])
def test_undocumented_or_wrong_type_status_remains_unknown(lab,status):
    server,gateway,process,*_=lab
    server.voice_response={'status':status,'text':'不能据此认定成功'}
    with pytest.raises(HookLocalError,match='VOICE_RESPONSE_INVALID'):
        transcriber().transcribe_once(gateway,process,voice(lab),automatic_transcription_disabled=lambda:True)
    assert process_voice_call_unresolved(process.generation)
    assert transcriber()._read(voice(lab))['state']=='uncertain'


@pytest.mark.parametrize('fromtype',['',None,'1',1])
def test_tested_empty_audio_identity_resolves_only_through_scoped_parent(lab,fromtype):
    real_size_audio=b'\x02#!SILK_V3'+b'x'*(16750-10)
    persist(lab,audio=True,data=real_size_audio,fields={'fromid':'','toid':'','fromtype':fromtype})
    assert capture_voice_audio(lab[3],PEER,MID,max_audio_bytes=32768) is None
    persist(lab,metadata=True,data=real_size_audio)
    result=capture_voice_audio(lab[3],PEER,MID,max_audio_bytes=32768)
    assert result.audio==real_size_audio and len(result.audio)==16750
    assert result.peer_wxid==PEER and result.native_msg_id==MID
    assert capture_voice_audio(replace(lab[3],account_epoch='other'),PEER,MID,max_audio_bytes=32768) is None
    assert capture_voice_audio(replace(lab[3],process_generation='other'),PEER,MID,max_audio_bytes=32768) is None
    assert not lab[0].requests


@pytest.mark.parametrize('fields',[
    {'fromtype':'2'}, {'fromtype':'0'}, {'fromtype':True},
    {'fromid':'wxid_other'}, {'toid':'wxid_other'},
    {'fromgid':'123@chatroom'}
])
def test_blank_supplement_never_relaxes_other_identity_or_group_conflicts(lab,fields):
    persist(lab,metadata=True)
    persist(lab,audio=True,fields={'fromid':'','toid':'','fromtype':'',**fields})
    with pytest.raises(ValueError,match='AUDIO_IDENTITY_CONFLICT'):capture(lab)
    assert not lab[0].requests


def test_empty_audio_identity_retains_length_and_duplicate_conflict_checks(lab):
    persist(lab,metadata=True,length=len(AUDIO)+1)
    persist(lab,audio=True,fields={'fromid':'','toid':'','fromtype':''})
    with pytest.raises(ValueError,match='LENGTH_MISMATCH'):capture(lab)
    assert not lab[0].requests


def test_empty_audio_fields_can_use_authenticated_inventory_metadata(lab):
    persist(lab,audio=True,fields={'fromid':'','toid':'','fromtype':''})
    xml=f'<msg><voicemsg length="{len(AUDIO)}"/></msg>'
    result=capture_voice_audio(lab[3],PEER,MID,max_audio_bytes=4096,verified_metadata_xml=xml)
    assert result.audio==AUDIO
    assert capture_voice_audio(lab[3],PEER,'888',max_audio_bytes=4096,verified_metadata_xml=xml) is None


@pytest.mark.parametrize('metadata_source',['callback','authenticated_inventory'])
def test_real_supplement_shape_flows_to_one_transcript_without_replaying_audio(lab,metadata_source):
    server,gateway,process,scope,*_=lab
    server.voice_response={'status':1,'text':'我想了解二手SUV，预算十五万元，先不要报价。'}
    sample=b'\x02#!SILK_V3'+b'x'*(16750-10)
    persist(lab,audio=True,data=sample,fields={'fromid':'','toid':'','fromtype':''})
    xml=None
    if metadata_source=='callback':persist(lab,metadata=True,data=sample)
    else:xml='<msg><voicemsg length="16750"/></msg>'
    audio=capture_voice_audio(scope,PEER,MID,max_audio_bytes=32768,verified_metadata_xml=xml)
    instance=HookVoiceTranscriber(max_audio_bytes=32768,max_text_bytes=2048)
    result=instance.transcribe_once(gateway,process,audio,automatic_transcription_disabled=lambda:True)
    assert result['state']=='transcribed' and result['native_msg_id']==MID
    assert result['text']==server.voice_response['text']
    assert instance.transcribe_once(gateway,process,audio,automatic_transcription_disabled=lambda:False)==result
    calls=[body for path,body,*_ in server.requests if path=='/Voice2Text']
    assert calls==[{'voice_hex':sample.hex().upper(),'msgsvrid':MID}]
    assert gateway.login_status(process).logged_in
