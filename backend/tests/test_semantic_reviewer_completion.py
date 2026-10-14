"""Real HTTP -> shared reviewer -> original semantic guard; invented payloads only."""
import sys,json,threading,copy
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest
R=Path(__file__).resolve().parents[2]/'worker-client/omniauto-rpa'
for p in (R,R/'apps/wechat_ai_customer_service',R/'apps/wechat_ai_customer_service/workflows',R/'apps/wechat_ai_customer_service/adapters'):sys.path.insert(0,str(p))
import customer_service_quality_reviewer as reviewer
from apps.wechat_ai_customer_service import llm_config
from apps.wechat_ai_customer_service.workflows.customer_service_brain import effective_brain_settings
FULL={'verdict':'pass','confidence':.95,'semantic_errors':[],'hard_boundary_concerns':[],'repair_instruction':'','customer_visible_risk':'low','reason':'候选内容是正常需求澄清，没有承诺价格或库存。'}
@pytest.fixture
def endpoint(monkeypatch):
 state={'content':json.dumps(FULL,ensure_ascii=False),'reason':'stop','requests':[]}
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*_):pass
  def do_POST(self):
   state['requests'].append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
   body=json.dumps({'choices':[{'message':{'content':state['content'],'reasoning_content':state.get('thinking','')},'finish_reason':state['reason']}],'usage':{'completion_tokens':state.get('tokens',200)}}).encode()
   self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 monkeypatch.setattr(reviewer,'read_secret',lambda name:'invented-test-key' if name=='OPENAI_COMPATIBLE_API_KEY' else '')
 monkeypatch.setenv('LLM_FALLBACK_ENABLED','false');monkeypatch.setattr(llm_config,'_LLM_CONFIG_PATH',Path('/private/tmp/nonexistent-reviewer-test-llm-config'))
 settings=reviewer.effective_reviewer_settings(effective_brain_settings({'customer_service_brain':{'provider':'openai_compatible','model':'synthetic-model','base_url':f'http://127.0.0.1:{server.server_port}/v1','semantic_reviewer_mode':'always','semantic_reviewer_cache_enabled':False,'semantic_reviewer_soft_pass_low_risk':False}}))
 yield state,settings
 server.shutdown();server.server_close();thread.join()

def call(settings):return reviewer.run_quality_reviewer_llm(settings=settings,request={'current_user_messages':['虚构测试：家用SUV，先不报价。'],'draft_reply':'明白，您更看重空间还是油耗？'})

def test_complete_review_has_sufficient_budget_and_original_pass(endpoint):
 state,settings=endpoint;out=call(settings)
 assert state['requests'][0]['max_tokens']==16384
 assert state['requests'][0]['response_format']=={'type':'json_object'}
 assert out['status']=='ok' and out['verdict']=='pass'
 assert reviewer.normalize_quality_review_result(out)['ok'] is True

@pytest.mark.parametrize('content',[json.dumps(FULL),json.dumps(FULL)[:-6],''])
def test_any_token_cutoff_cannot_pass_even_when_json_looks_closed(endpoint,content):
 state,settings=endpoint;state.update(content=content,reason='length',tokens=350,thinking='synthetic reasoning')
 out=call(settings)
 assert out['status']=='error' and out['error']=='semantic_reviewer_response_incomplete'
 assert reviewer.normalize_quality_review_result(out)['ok'] is False

@pytest.mark.parametrize('field',list(FULL))
def test_missing_review_contract_field_never_defaults_to_pass(endpoint,field):
 state,settings=endpoint;value=copy.deepcopy(FULL);value.pop(field);state['content']=json.dumps(value)
 out=call(settings)
 assert out['status']=='error' and out['error']=='semantic_reviewer_response_invalid_contract'

@pytest.mark.parametrize('verdict',['repair','block','handoff_suggest'])
def test_valid_negative_semantics_still_prevents_pass(endpoint,verdict):
 state,settings=endpoint;value={**FULL,'verdict':verdict,'hard_boundary_concerns':['虚构候选无依据保证车辆无事故。'],'repair_instruction':'去掉无依据承诺。','customer_visible_risk':'high'};state['content']=json.dumps(value)
 out=call(settings)
 assert out['status']=='ok' and out['verdict']==verdict
 assert reviewer.normalize_quality_review_result(out)['ok'] is False

@pytest.mark.parametrize('field,value',[('confidence',True),('confidence',1.2),('semantic_errors','wrong type'),('hard_boundary_concerns',None),('customer_visible_risk','invalid'),('reason',None),('repair_instruction',42),('verdict','unknown'),('verdict',[]),('customer_visible_risk',{})])
def test_invalid_review_contract_is_not_accepted(endpoint,field,value):
 state,settings=endpoint;state['content']=json.dumps({**FULL,field:value});out=call(settings)
 assert out['status']=='error' and out['error']=='semantic_reviewer_response_invalid_contract'
