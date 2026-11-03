"""Synthetic integration: real HTTP ingest/context/adapter/Brain/query/Guard/claim/ACK.
Boundaries: local SQLite, in-process provider instead of subprocess, synthetic model,
empty base knowledge pack, database-backed inventory runtime, simulated Worker ACK.
"""
import sys,json
from pathlib import Path
import pytest
from sqlalchemy import select
root=Path(__file__).resolve().parents[2]; app=root/'worker-client/omniauto-rpa/apps/wechat_ai_customer_service'
sys.path.insert(0,str(root/'backend/tests'));sys.path.insert(0,str(app/'tests'))
from conftest import authenticated_admin_dependency
import test_catalog_fact_references as flow
import test_c3_api as api
from app.core.database import SessionLocal
from app.models.vehicle import KnowledgeItem
from app.models.c3 import ReplyAction,MessageBatch,Conversation
from app.models.task import Task
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from test_catalog_query_entry import fixtures,review,summary_plan,query,cq,ev,legacy_ev

@pytest.mark.parametrize('mutation',[None,'price','new_listing','zero','zero_no_query','zero_incomplete','zero_uncertain','zero_unknown','zero_undeclared','zero_rejected','zero_changed','zero_query_failed'])
def test_joined(monkeypatch,mutation):
 api.setup_function();vid=api._create_listed_vehicle();receipt_box=[];invocations=[];decisions=[];brains=[];calls=[]
 from app.core.config import get_settings
 monkeypatch.setenv('C3_OMNIAUTO_ROOT',str(root/'worker-client/omniauto-rpa'))
 monkeypatch.setattr(get_settings(),'c3_omniauto_root',str(root/'worker-client/omniauto-rpa'))
 class DBRuntime:
  def __init__(self):
   with SessionLocal() as db:self.tenant_id=db.scalar(select(KnowledgeItem.tenant_id).where(KnowledgeItem.category_id=="products"))
  def list_items(self,kind):
   assert kind=='products'
   if mutation=='zero_query_failed':raise OSError('synthetic inventory read failure')
   with SessionLocal() as db:return [x.payload for x in db.scalars(select(KnowledgeItem).where(KnowledgeItem.category_id=='products'))]
 config=fixtures.base_config(fixtures.base_plan());config['customer_service_brain'].update(mode='brain_first',provider='openai',api_key='synthetic',low_authority_fast_enabled=False,semantic_reviewer_result=review('query_summary'))
 if mutation=='zero_rejected':config['customer_service_brain']['semantic_reviewer_result']=review('query_summary',verdict='repair',semantic_errors=['synthetic rejection'])
 import importlib
 runner=importlib.import_module('apps.wechat_ai_customer_service.workflows.customer_service_brain');attach=cq.attach_receipt
 def record(pack,receipt):
  if mutation=='zero_incomplete':receipt['scan_complete']=False
  if mutation=='zero_unknown':receipt['unknown_field_count']=1
  receipt_box.append(receipt);return attach(pack,receipt)
 monkeypatch.setattr(cq,'attach_receipt',record)
 monkeypatch.setattr(ev,'KnowledgeRuntime',DBRuntime)
 monkeypatch.setattr(legacy_ev,'build_evidence_pack',lambda *a,**k:{'evidence':{'products':[]},'safety':{'allowed_auto_reply':True,'must_handoff':False,'reasons':[]}})
 monkeypatch.setattr(runner,'resolve_llm_api_key',lambda **k:'synthetic')
 def transport(**kw):
  calls.append(kw.get('progress_stage'))
  if mutation=='zero_no_query':
   payload=summary_plan({'query_id':'invented'})
  elif not receipt_box:
   mid=str(invocations[-1]['batch'][-1]['id']);q=query(source_ids=[mid]);q['budget']['source_ids']=[mid];q['budget']['value']=4 if str(mutation).startswith('zero') else 200;q['requirement_state']='uncertain' if mutation=='zero_uncertain' else 'clear';payload={'catalog_query':q}
  else:
   receipt=receipt_box[-1];payload=summary_plan(receipt);payload['facts_claimed'][0]['value']=receipt['match_count'];payload['reply_segments']=[f"按您本轮预算条件，目前符合条件的在售车辆有{receipt['match_count']}辆。"]
  if mutation=='zero_undeclared' and receipt_box:payload['facts_claimed']=[]
  if mutation=='zero_changed' and receipt_box:api._create_listed_vehicle()
  return {'ok':True,'status':200,'response_text':json.dumps(payload,ensure_ascii=False)}
 monkeypatch.setattr(runner,'call_llm_request_with_failover',transport)
 class Adapter(RealOmniAutoAIEngineAdapter):
  def _load_config(self):return config
  def _run_brain_isolated(self,*,config,invocation,timeout_seconds):
   # Preserve real JSON serialization boundary, then invoke the actual production Brain.
   request=json.loads(json.dumps({'config':config,'invocation':invocation}));invocations.append(request['invocation'])
   result=self._load_brain()(config=request['config'],**request['invocation']);brains.append(result);return json.loads(json.dumps(result))
  def generate_reply_decision(self,**kwargs):
   result=super().generate_reply_decision(**kwargs);decisions.append(result.decision);return result
 original_ingest=api._ingest
 def ingest(w,cid,key,text,**kw):return original_ingest(w,cid,key,'总预算四万元以内有什么车' if str(mutation).startswith('zero') else '预算两百万元以内有什么车',**kw)
 monkeypatch.setattr(api,'_ingest',ingest)
 w,b,r=flow.generate(monkeypatch,Adapter())
 trace={'mutation':mutation,'api_result':r,'decisions':decisions,'brain_results':brains,'receipts':receipt_box,'calls':calls}
 if str(mutation).startswith('zero_'):
  assert r['decision']=='retry_later',trace
  with SessionLocal() as db:assert not list(db.scalars(select(ReplyAction)))
  return
 if mutation=='zero':
  assert receipt_box[-1]['scan_complete'] and receipt_box[-1]['match_count']==0
  assert invocations[-1]['target_state'].get('chejin_knowledge_release'), 'real frozen knowledge missing'
 assert r['decision']=='send_reply',trace
 claimed=flow.claim(w,b,r)
 if mutation=='price':assert api.client.put(f'/api/vehicles/{vid}',json={'public_price':9.99},headers=api.HEADERS).status_code==200
 elif mutation=='new_listing':api._create_listed_vehicle()
 response=flow.claim_send(w,r,claimed);trace['claim_send_status']=response.status_code
 assert response.status_code==(409 if mutation in ('price','new_listing') else 200),response.text
 with SessionLocal() as db:
  a=db.get(ReplyAction,r['reply_action_id']);trace['stored_catalog_snapshot']=a.ai_payload.get('vehicle_catalog_snapshot')
  assert trace['stored_catalog_snapshot']['vehicle_ids']==[vid]
 if mutation in (None,'zero'):
  send=response.json()['data'];ack=api.client.post(f"/api/reply-actions/{r['reply_action_id']}/sent-ack",json={'worker_id':w['id'],'task_id':r['task_id'],'client_instance_id':'client-c3','send_token':send['send_token'],'send_result':'sent','action_phase':'confirmed','reply_text_hash':send['reply_text_hash'],'sidecar_run_id':'private-audit-synthetic-ack'},headers=api._worker_headers(w))
  assert ack.status_code==200,ack.text
  with SessionLocal() as db:
   trace['final_status']={'action':db.get(ReplyAction,r['reply_action_id']).status,'task':db.get(Task,r['task_id']).status,'conversation':db.get(Conversation,b['conversation_id']).status}
   assert trace['final_status']=={'action':'sent','task':'completed','conversation':'waiting_user_reply'}
