"""R4-B formal confirm + real HTTP/PG/Worker/SQLite; desktop/model are controlled."""
import json
import pytest
import test_post_send_customer_read_http as post
import test_reply_sequence_worker as sequence
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation

ORIGINAL_CONFIRM="""  confirmed=sidecar.confirm_reply_sent(1,target='C3TEST01',text=kwargs['text'],exact=True,baseline_match_count=0,
    baseline_message_sequence=baseline['message_sequence'],initial_snapshot=current,max_attempts=1)"""
LAYERED_CONFIRM=r'''
  states={'missing':{'state':'unavailable','reason':'send_status_gutter_geometry_missing'},
          'sending':{'state':'blocked','reason':'possible_sending'},'clear':{'state':'clear'}}
  frames=[]
  for state in os.environ['TEST_SEND_STATUS_SEQUENCE'].split(','):
   captured=copy.deepcopy(current)
   next(row for row in captured['message_sequence'] if row['observation_id']==sent_id)['send_status_evidence']=states[state]
   frames.append(captured)
  capture_count=[0]
  capture_original=sidecar.capture_send_fact_snapshot
  def capture(*a,**kw):
   capture_count[0]+=1
   return copy.deepcopy(frames[min(capture_count[0],len(frames)-1)])
  sidecar.capture_send_fact_snapshot=capture
  try:
   confirmed=sidecar.confirm_reply_sent(1,target='C3TEST01',text=kwargs['text'],exact=True,baseline_match_count=0,
     baseline_message_sequence=baseline['message_sequence'],initial_snapshot=frames[0])
  finally:
   sidecar.capture_send_fact_snapshot=capture_original
  Path(__file__).with_name('confirmation-'+kwargs['reply_action_id']+'.json').write_text(json.dumps(confirmed,ensure_ascii=False))
'''

def boundary():
    assert post.SEND_BOUNDARY.count(ORIGINAL_CONFIRM)==1
    return post.SEND_BOUNDARY.replace(ORIGINAL_CONFIRM,LAYERED_CONFIRM)

@pytest.mark.parametrize('segmented',[False,True])
@pytest.mark.parametrize('states',['missing','sending,clear'])
def test_layered_status_keeps_immediate_customer_reply_and_receipt(http_api,monkeypatch,async_generation,tmp_path,segmented,states):
    monkeypatch.setenv('TEST_SEND_STATUS_SEQUENCE',states)
    monkeypatch.setattr(post,'SEND_BOUNDARY',boundary())
    post._run(http_api,monkeypatch,async_generation,tmp_path,segmented,'text')
    records=[json.loads(p.read_text()) for p in tmp_path.glob('confirmation-*.json')]
    assert len(records)==2 and all(r['ok'] and r['attempt']==len(states.split(',')) for r in records)

@pytest.mark.parametrize('loss',['lost_request','lost_response'])
def test_missing_indicator_confirmed_receipt_survives_same_sqlite_restart(http_api,monkeypatch,async_generation,tmp_path,loss):
    monkeypatch.setenv('TEST_SEND_STATUS_SEQUENCE','missing')
    monkeypatch.setattr(post,'SEND_BOUNDARY',boundary())
    post._run(http_api,monkeypatch,async_generation,tmp_path,True,'text',loss)

@pytest.mark.parametrize('states',['missing','sending,clear'])
def test_three_segments_use_formal_confirmation_and_fresh_baseline(http_api,monkeypatch,async_generation,tmp_path,states):
    monkeypatch.setenv('TEST_SEND_STATUS_SEQUENCE',states)
    body=boundary()
    start=body.index("  if not any(m['id']=='customer-interruption'")
    end=body.index("  frame=self._contractual_message_payload",start)
    body=body[:start]+body[end:]  # desktop has no immediate customer response
    script=sequence.WORKER
    start=script.index("  self.messages.append({'id':'sent-'")
    end=script.index("  return result\n def prepare_voice_action",start)
    script=script[:start]+body+script[end:]
    monkeypatch.setattr(sequence,'WORKER',script)
    sequence.test_worker_sends_and_settles_three_segments_without_test_intervention(
        http_api,monkeypatch,async_generation,tmp_path,'normal')
    records=[json.loads(p.read_text()) for p in tmp_path.glob('confirmation-*.json')]
    assert len(records)==3 and all(r['ok'] and r['attempt']==len(states.split(',')) for r in records)
    assert sorted(sum(row['sender_role']=='self' for row in r['snapshot']['message_sequence']) for r in records)==[1,2,3]
