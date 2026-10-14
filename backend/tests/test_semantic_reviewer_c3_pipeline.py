"""API -> isolated DB -> fresh provider subprocess -> full Brain + Guard."""
import copy,json,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from sqlalchemy import select
import pytest
import test_brain_fact_guidance_regression as regression

@pytest.mark.parametrize('review_case',['complete','closed_json_cutoff','missing_safety_field'])
def test_review_result_controls_original_c3_task_creation(monkeypatch,review_case):
 import test_c3_api as api
 from app.core.config import get_settings
 from app.models.c3 import MessageBatch,ReplyAction
 from app.models.task import Task
 from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
 api.setup_function()
 monkeypatch.setenv('C3_OMNIAUTO_ROOT',str(regression.RUNTIME));monkeypatch.setenv('C3_AI_ADAPTER_MODE','real');monkeypatch.setenv('OPENAI_COMPATIBLE_API_KEY','INVENTED-REVIEWER-PIPELINE-KEY');get_settings.cache_clear()
 requests=[];plan=regression.make_plan()
 class Provider(BaseHTTPRequestHandler):
  def log_message(self,*_):pass
  def do_POST(self):
   request=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(request)
   review=request['model']=='synthetic-reviewer'
   if not review:
    user_text=next(x['content'] for x in request['messages'] if x['role']=='user')
    user,_=json.JSONDecoder().raw_decode(user_text.lstrip())
    knowledge=user['brain_input']['content_basis']['formal_knowledge']['faq']
    plan['evidence_used']['formal_knowledge_ids']=[x['source_id'] for x in knowledge]
   body_result={'verdict':'pass','confidence':.95,'semantic_errors':[],'hard_boundary_concerns':[],'repair_instruction':'','customer_visible_risk':'low','reason':'虚构正常购车需求澄清，无事实承诺。'} if review else copy.deepcopy(plan)
   if review and review_case=='missing_safety_field':body_result.pop('hard_boundary_concerns')
   body=json.dumps({'choices':[{'message':{'content':json.dumps(body_result,ensure_ascii=False)},'finish_reason':'length' if review and review_case=='closed_json_cutoff' else 'stop'}]}).encode()
   self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 server=ThreadingHTTPServer(('127.0.0.1',0),Provider);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 config={'customer_service_brain':{'enabled':True,'mode':'brain_first','provider':'openai_compatible','model':'synthetic-brain','base_url':f'http://127.0.0.1:{server.server_port}/v1','min_confidence':.2,'require_evidence':True,'require_fact_claims':True,'quality_verifier_enabled':True,'semantic_reviewer_enabled':True,'semantic_reviewer_force':True,'semantic_reviewer_cache_enabled':False,'semantic_reviewer_model':'synthetic-reviewer','semantic_reviewer_soft_pass_low_risk':False,'semantic_reviewer_post_repair_soft_pass_enabled':False,'semantic_reviewer_brain_handoff_soft_pass_enabled':False,'require_final_visible_polish':False,'fallback_to_legacy_on_error':False},'llm_reply_synthesis':{'enabled':True,'provider':'openai_compatible','require_evidence':True},'raw_message_store':{'enabled':False},'final_visible_llm_polish':{'enabled':False}}
 monkeypatch.setattr(RealOmniAutoAIEngineAdapter,'_load_config',staticmethod(lambda:copy.deepcopy(config)))
 try:
  for title,content in regression.GUIDANCE:api._publish_managed_knowledge(title,content)
  worker,binding=api._setup_bound_conversation()
  eid=api._ingest(worker,binding['conversation_id'],'synthetic-completion-case','您好，我想了解购车需求。')
  batch=api._collect(binding['conversation_id'],eid);result=api._generate(batch['batch_id'])
  reviews=[r for r in requests if r['model']=='synthetic-reviewer']
  assert reviews and all(r['max_tokens']==16384 for r in reviews)
  with api.SessionLocal() as db:
   replies=list(db.scalars(select(ReplyAction)));tasks=list(db.scalars(select(Task).where(Task.task_type=='chat_reply')))
   saved=db.get(MessageBatch,batch['batch_id'])
   if review_case=='complete':
    assert result['decision']=='send_reply'
    assert len(replies)==len(tasks)==1
    repeated=api._generate(batch['batch_id']);assert repeated['task_id']==result['task_id']
    native=saved.ai_response_snapshot['raw_payload']['omniauto_brain_result']
    assert native['quality_gate_v2']['status']=='ok' and native['quality_gate_v2']['ok'] is True
   else:
    assert result['decision'] in {'retry_later','handoff'}
    assert len(replies)==len(tasks)==0
    native=saved.ai_response_snapshot.get('raw_payload',{}).get('omniauto_brain_result',{})
    if native:assert native['quality_gate_v2']['status']=='error'
 finally:
  server.shutdown();server.server_close();thread.join();get_settings.cache_clear()
