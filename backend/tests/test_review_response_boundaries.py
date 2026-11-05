"""Actual backend ingest -> Brain/query/review/Guard -> send permission; synthetic model only."""
from copy import deepcopy
from pathlib import Path
import json,sys
import pytest
from sqlalchemy import select
import test_catalog_brain_joined as joined
from conftest import authenticated_admin_dependency
from apps.wechat_ai_customer_service.workflows import customer_service_brain as brain
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.vehicle import KnowledgeItem
from app.models.c3 import MessageBatch,ReplyAction
from app.models.task import Task
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
reviewer=sys.modules[brain.review_brain_reply_semantics.__module__]

@pytest.mark.parametrize('shape',['one_pass','one_block','fenced_pass','wrapped_pass'])
def test_ambiguous_review_never_reaches_send_permission(monkeypatch,shape):
    joined.api.setup_function();joined.api._create_listed_vehicle()
    monkeypatch.setattr(get_settings(),'c3_omniauto_root',str(joined.root/'worker-client/omniauto-rpa'))
    state={};calls=[]
    class Runtime:
        def __init__(self):
            with SessionLocal() as db:self.tenant_id=db.scalar(select(KnowledgeItem.tenant_id).where(KnowledgeItem.category_id=='products'))
        def list_items(self,kind):
            with SessionLocal() as db:return [x.payload for x in db.scalars(select(KnowledgeItem).where(KnowledgeItem.category_id==kind))]
    original=joined.cq.attach_receipt
    def attach(pack,receipt):state['receipt']=receipt;return original(pack,receipt)
    monkeypatch.setattr(joined.cq,'attach_receipt',attach)
    monkeypatch.setattr(joined.ev,'KnowledgeRuntime',Runtime)
    monkeypatch.setattr(joined.legacy_ev,'build_evidence_pack',lambda *a,**kw:{'evidence':{'products':[]},'safety':{'allowed_auto_reply':True,'must_handoff':False,'reasons':[]}})
    monkeypatch.setattr(brain,'resolve_llm_api_key',lambda **kw:'synthetic')
    monkeypatch.setattr(reviewer,'resolve_llm_api_key',lambda **kw:'synthetic')
    def transport(**kw):
        stage=kw['progress_stage'];calls.append(stage)
        if stage=='semantic_reviewer':
            good=joined.review('query_summary');bad=joined.review('query_summary',verdict='block',semantic_errors=['synthetic reject'],repair_instruction='不得发送')
            raw={'one_pass':json.dumps(good),'one_block':json.dumps(bad),'fenced_pass':'```json\n'+json.dumps(good)+'\n```','wrapped_pass':'结果：'+json.dumps(good)+'\n结束'}[shape]
        elif not state.get('receipt'):
            mid=str(state['invocation']['batch'][-1]['id']);q=joined.query(source_ids=[mid]);q['budget']['source_ids']=[mid];q['budget']['value']=200
            raw=json.dumps({'catalog_query':q})
        else:
            receipt=state['receipt'];p=joined.summary_plan(receipt);p['facts_claimed'][0]['value']=receipt['match_count'];p['reply_segments']=[f"按本轮预算条件，目前符合条件的在售车辆有{receipt['match_count']}辆。"]
            raw=json.dumps(p,ensure_ascii=False)
        return {'ok':True,'status':200,'response_text':raw,'response_diagnostics':{'finish_reason':'stop'}}
    monkeypatch.setattr(brain,'call_llm_request_with_failover',transport);monkeypatch.setattr(reviewer,'call_llm_request_with_failover',transport)
    config=joined.fixtures.base_config(joined.fixtures.base_plan());config['customer_service_brain'].update(mode='brain_first',provider='openai',api_key='synthetic',low_authority_fast_enabled=False,semantic_reviewer_cache_enabled=False)
    class Adapter(RealOmniAutoAIEngineAdapter):
        def _load_config(self):return deepcopy(config)
        def _run_brain_isolated(self,*,config,invocation,timeout_seconds):
            request=json.loads(json.dumps({'config':config,'invocation':invocation}));state['invocation']=request['invocation']
            return json.loads(json.dumps(self._load_brain()(config=request['config'],**request['invocation'])))
    worker,binding,result=joined.flow.generate(monkeypatch,Adapter())
    granted=False
    if result['decision']=='send_reply':
        permit=joined.flow.claim_send(worker,result,joined.flow.claim(worker,binding,result))
        granted=permit.status_code==200
    with SessionLocal() as db:
        actions=list(db.scalars(select(ReplyAction)));tasks=list(db.scalars(select(Task).where(Task.task_type=='chat_reply')))
        native=db.get(MessageBatch,result['batch']['id']).ai_response_snapshot['raw_payload']['omniauto_brain_result']
        summary={'shape':shape,'decision':result['decision'],'action_count':len(actions),'send_task_count':len(tasks),'send_permission_granted':granted,
          'brain_adoptable':native.get('adoptable'),'review':native.get('quality_gate_v2'),'calls':calls}
    assert granted==(shape!='one_block'),summary
