import threading
import time
import pytest
from test_hook_gateway import lab
from test_hook_voice import voice,transcriber
from chejin_worker_client.hook_gateway import HookLocalError
from chejin_worker_client import ui_lock

@pytest.mark.parametrize('kind',['login','voice'])
def test_live_call_does_not_become_unknown_before_original_lease_wait(lab,kind):
 server,gateway,process,*_=lab
 entered=threading.Event();release=threading.Event();results=[]
 server.voice_response={'status':1,'text':'test transcript'}
 route='/Voice2Text' if kind=='voice' else '/IsLoginStatus'
 def hold(path):
  if path==route and not entered.is_set():
   entered.set();assert release.wait(3)
 server.on_request=hold
 def first():
  try:
   results.append(transcriber().transcribe_once(gateway,process,voice(lab),automatic_transcription_disabled=lambda:True) if kind=='voice' else gateway.login_status(process))
  except BaseException as exc:results.append(exc)
 t=threading.Thread(target=first);t.start()
 try:
  assert entered.wait(2)
  # Busy is a normal existing lease: the lifecycle already treats it as waiting.
  with pytest.raises(ui_lock.UiLockError):gateway.login_status(process)
 finally:
  release.set();t.join(3)
 assert not t.is_alive() and len(results)==1 and not isinstance(results[0],BaseException)
 assert gateway.login_status(process).logged_in


def test_waiting_identity_check_runs_only_after_transcription_completes(lab):
 server,gateway,process,*_=lab
 gateway.lock_timeout_seconds=2
 server.voice_response={'status':1,'text':'one result'}
 entered=threading.Event();release=threading.Event();results=[]
 def hold(path):
  if path=='/Voice2Text':
   entered.set();assert release.wait(3)
 server.on_request=hold
 def first():
  try:results.append(transcriber().transcribe_once(gateway,process,voice(lab),automatic_transcription_disabled=lambda:True))
  except BaseException as exc:results.append(exc)
 def second():
  try:results.append(gateway.login_status(process))
  except BaseException as exc:results.append(exc)
 a=threading.Thread(target=first);a.start();assert entered.wait(2)
 b=threading.Thread(target=second);b.start()
 try:
  time.sleep(.1)
  assert len(results)==0
  assert server.requests[-1][0]=='/Voice2Text'
 finally:
  release.set();a.join(3);b.join(3)
 assert not a.is_alive() and not b.is_alive()
 assert len(results)==2 and all(not isinstance(x,BaseException) for x in results)
 assert [p for p,*_ in server.requests].count('/Voice2Text')==1
 assert server.requests[-1][0]=='/IsLoginStatus'


def test_abandoned_calling_journal_still_blocks_after_lease_is_free(lab):
 from chejin_worker_client.hook_active_calls import reserve
 server,gateway,process,*_=lab
 reserve(process.generation,'abandoned-read','QueryDB')
 with pytest.raises(HookLocalError,match='QUARANTINED'):gateway.login_status(process)
 assert server.requests==[]
 assert not ui_lock.lock_summary()['locked']
