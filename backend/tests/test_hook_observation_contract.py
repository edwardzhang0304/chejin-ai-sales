from unittest.mock import patch
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import MessageBatch,ReplyAction
from app.services import c3_service
from test_hook_business_http import clean,post
from test_hook_history_business_http import bound,coverage,upload

def collected(monkeypatch):
 w,t,p,b=bound();r,_=upload(w,p,b,coverage(b),monkeypatch)
 assert r.status_code==200
 return w,r.json()['data']['message_batch']['batch_id']

def test_observe_only_http_does_not_claim_generate_even_when_backend_still_running(monkeypatch):
 from test_hook_business_http import client
 w,bid=collected(monkeypatch)
 with SessionLocal() as db:before=db.get(MessageBatch,bid).generation_attempt_count
 with patch.object(c3_service,'claim_message_batch_generation',side_effect=AssertionError('Observation started a model')):
  response=client.get(f"/api/workers/{w['id']}/wechat/message-batches/{bid}?observe_only=true",
   headers={'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':w['instance']})
 assert response.status_code==200,response.text
 data=response.json()['data'];assert data['observation_only'] is True
 assert data['authorization']['allowed'] is False and data['continuation']['allowed'] is False
 with SessionLocal() as db:
  assert db.get(MessageBatch,bid).generation_attempt_count==before
  assert not list(db.scalars(select(ReplyAction)))

def test_native_background_requires_client_admission_and_paused_denies_new_attempt(monkeypatch):
 w,bid=collected(monkeypatch)
 with SessionLocal() as db:
  assert c3_service.claim_message_batch_generation(db,batch_id=bid)['run'] is False
  assert c3_service.claim_message_batch_generation(db,batch_id=bid,native_client_admitted=True)['run'] is True
  db.commit()
 assert post(w,f"/workers/{w['id']}/run-status",{'run_status':'paused','worker_token':w['worker_token'],'client_instance_id':w['instance']}).status_code==200
 with SessionLocal() as db:
  assert c3_service.claim_message_batch_generation(db,batch_id=bid,native_client_admitted=True)['run'] is False

def test_already_claimed_model_can_complete_after_pause(monkeypatch):
 w,bid=collected(monkeypatch)
 with SessionLocal() as db:
  claim=c3_service.claim_message_batch_generation(db,batch_id=bid,native_client_admitted=True);db.commit()
 assert claim['run'] is True
 assert post(w,f"/workers/{w['id']}/run-status",{'run_status':'paused','worker_token':w['worker_token'],'client_instance_id':w['instance']}).status_code==200
 with SessionLocal() as db:
  result=c3_service.generate_for_batch(db,batch_id=bid,expected_generation_attempt=claim['attempt']);db.commit()
 assert result['decision']=='send_reply'

def test_observer_wrong_token_cannot_read_the_batch(monkeypatch):
 from test_hook_business_http import client
 w,bid=collected(monkeypatch)
 r=client.get(f"/api/workers/{w['id']}/wechat/message-batches/{bid}?observe_only=true",headers={'X-Worker-Token':'invalid','X-Client-Instance-Id':w['instance']})
 assert r.status_code==401
