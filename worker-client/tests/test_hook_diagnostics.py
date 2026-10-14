import json
from test_hook_gateway import lab
from test_hook_voice import persist,transcriber,voice
from chejin_worker_client.hook_diagnostics import snapshot


def test_native_diagnostics_include_scope_and_phase_without_private_content(lab):
    persist(lab,metadata=True);persist(lab,audio=True)
    lab[0].voice_response={'status':1,'text':'Private secret message content 13800000003'}
    transcriber().transcribe_once(lab[1],lab[2],voice(lab),automatic_transcription_disabled=lambda:True)
    value=snapshot();body=json.dumps(value)
    assert value['inbox']['rows']==2 and value['voice_states']=={'transcribed':1}
    assert value['voice_parents'][0]['audio_sha256']
    assert 'Private secret message content' not in body and '13800000003' not in body and lab[3].self_wxid not in body
    assert not value['audio_included'] and not value['contact_list_included']


def test_history_timings_export_only_finite_numeric_allowlisted_fields(lab):
    from chejin_worker_client import storage
    storage.append_log('INFO','hook_history_profile','test',metadata={
        'capture_seconds':1.25,'dll_calls':52,'total_seconds':True,
        'dll_http_seconds':-1,'full_process_inspections':'Private secret',
        'phone':'13800000003','reply_text':'Private secret',
        'durable_read_sessions':float('inf')})
    value=snapshot()
    assert value['recent_history_timings']==[{'capture_seconds':1.25,'dll_calls':52}]
    assert 'Private secret' not in json.dumps(value) and '13800000003' not in json.dumps(value)


def test_local_transport_diagnostics_export_categories_only(lab):
    from chejin_worker_client import storage
    storage.append_log('ERROR','hook_connection_fault','test',error_code='HOOK_LOCAL_TRANSPORT_FAILED',
        metadata={'local_operation':'Voice2Text','transport_kind':'timeout','exception':'private url or text'})
    storage.append_log('ERROR','hook_connection_fault','test',error_code='private content',
        metadata={'local_operation':'private url','transport_kind':'private message'})
    rows=snapshot()['recent_local_faults']
    assert rows[0]=={'error_code':None,'operation':None,'transport_kind':None}
    assert rows[1]=={'error_code':'HOOK_LOCAL_TRANSPORT_FAILED','operation':'Voice2Text','transport_kind':'timeout'}
    assert 'private' not in json.dumps(rows)
