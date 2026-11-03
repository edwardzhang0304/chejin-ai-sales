"""Synthetic event-query boundary; does not claim Windows event-log UAT."""
import json
import subprocess
import zipfile
from types import SimpleNamespace

import pytest
from chejin_worker_client import windows_exit_evidence as events
from chejin_worker_client.diagnostic_bundle import Collector


def record(app='WeChat.exe', **fields):
    return {'log':'Application','provider':'Application Error','id':1000,
            'record_id':12,'time_utc':'2026-11-03T10:31:00Z',
            'fields':{'AppName':app,'ModuleName':r'C:\Users\PRIVATE\version.dll',
                      'ExceptionCode':'c0000005', 'AppPath':r'C:\Users\PRIVATE\WeChat.exe',
                      'Message':'PRIVATE CHAT', **fields},'message':'PRIVATE CHAT'}


def test_projection_retains_fault_identity_without_message_path_or_unrelated_app():
    raw={'queries':[{'log':'Application','status':'collected','inspected':5,'message':'PRIVATE'}],
         'events':[record(),record('Unrelated.exe'),
                   {'log':'System','provider':'Microsoft-Windows-Resource-Exhaustion-Detector','id':2004,
                    'fields':{'SystemCommitCharge':'1234','UserName':'PRIVATE'}}]}
    result=events.project(raw)
    assert len(result['events'])==2
    assert result['events'][0]['fields']=={'AppName':'WeChat.exe','ModuleName':'version.dll','ExceptionCode':'c0000005'}
    assert result['events'][1]['fields']=={'SystemCommitCharge':'1234'}
    assert 'PRIVATE' not in json.dumps(result)


@pytest.mark.parametrize('outcome',['success','permission','timeout','oversize','invalid'])
def test_collection_is_bounded_and_records_failures(tmp_path,monkeypatch,outcome):
    collector=Collector(tmp_path,tmp_path/'out.zip')
    monkeypatch.setattr(events.sys,'platform','win32')
    calls=[]
    def run(args,**kwargs):
        calls.append((args,kwargs))
        if outcome=='timeout':raise subprocess.TimeoutExpired(args,kwargs['timeout'])
        data={'queries':[{'log':'Application','status':'read_failed' if outcome=='permission' else 'collected',
                          'inspected':200,'limit_reached':True}], 'events':[record()]}
        raw=json.dumps(data).encode() if outcome not in ('oversize','invalid') else (b'x'*(events.MAX_OUTPUT+1) if outcome=='oversize' else b'?')
        return SimpleNamespace(returncode=0,stdout=raw)
    monkeypatch.setattr(events.subprocess,'run',run)
    with zipfile.ZipFile(collector.destination,'w') as archive:events.collect(collector,archive)
    assert len(calls)==1 and 0<calls[0][1]['timeout']<=8
    assert calls[0][1]['creationflags']==0x08000000
    assert '-NonInteractive' in calls[0][0]
    with zipfile.ZipFile(collector.destination) as archive:
        if outcome in ('success','permission'):
            assert 'windows/events.json' in archive.namelist()
            assert b'PRIVATE' not in archive.read('windows/events.json')
        else:assert not archive.namelist()
    if outcome=='success':assert any(e.get('reason')=='event_scan_limit' for e in collector.entries)
    else:assert any(e['status']=='read_failed' for e in collector.entries)


def test_expired_global_budget_does_not_start_event_query(tmp_path,monkeypatch):
    monkeypatch.setattr(events.sys,'platform','win32')
    monkeypatch.setattr(events.subprocess,'run',lambda *a,**k:pytest.fail('query after deadline'))
    collector=Collector(tmp_path,tmp_path/'out.zip',max_seconds=0)
    with zipfile.ZipFile(collector.destination,'w') as archive:events.collect(collector,archive)
    assert collector.entries[-1]['reason']=='export_time_or_file_budget'
