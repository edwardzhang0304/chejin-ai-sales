"""Synthetic full inventory queries; no network, sends or production data."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import pytest
APP=Path(__file__).resolve().parents[1]
for path in (APP.parents[1],APP,APP/'workflows',APP/'adapters'):sys.path.insert(0,str(path))
from apps.wechat_ai_customer_service.workflows import catalog_query as cq
from apps.wechat_ai_customer_service.workflows import customer_service_brain as brain
from apps.wechat_ai_customer_service.workflows import reply_evidence_builder as ev
from apps.wechat_ai_customer_service.workflows import customer_service_quality_reviewer as reviewer


def query(**changes):
    q={'version':1,'operation':'search','requirement_state':'clear',
       'budget':{'state':'specified','unit':'万元','mode':'upper','value':4,'source_ids':['m1']},
       'filters':[],'reference_ids':[],'exclude_ids':[],'sort':'price_asc',
       'source_ids':['m1'],'unsupported_requirements':[],'interpretation':'总预算四万，首付不是总价'}
    q.update(changes);return q


def row(key, price, **details):
    return {'id':key,'status':'active','data':{'name':'合成车'+key,'price':price,'inventory':1,'additional_details':details}}


class Runtime:
    tenant_id='synthetic-tenant'
    def __init__(self,rows):self.rows=rows;self.calls=[]
    def list_items(self,kind):self.calls.append(kind);return deepcopy(self.rows)


def evidence(text='总预算四万，首付一万'):
    return {'current_message':text,'current_batch':[{'id':'m1','sender':'customer','content':text}],
            'conversation':{'history':[]},'knowledge':{'evidence':{},'product_master':{'items':[]}},
            'catalog_protocol':{'protocol':cq.PROTOCOL,'phase':'not_queried'}}


def execute(raw,rows):
    return cq.execute_query(cq.normalize_query(raw,cq.message_sources(evidence())),Runtime(rows),scope={'conversation_id':'c1'},limit=4)


@pytest.mark.parametrize('reverse',[False,True])
def test_filters_before_any_limit_across_pages_and_old_references(reverse):
    rows=[row(f'h{i}',10+i,brand='甲') for i in range(65)]+[row('a',3.58,brand='乙'),row('b',3.98,brand='甲')]
    result=execute(query(reference_ids=['h0'],filters=[{'field':'brand','op':'eq','value':'乙','source_ids':['m1']}]),rows[::-1] if reverse else rows)
    assert result['scanned_count']==67 and result['match_count']==1
    assert [p['id'] for p in result['recommendations']]==['a']
    assert result['references'][0]['within_budget'] is False
    assert result['tenant_id']=='synthetic-tenant'


@pytest.mark.parametrize('state',['unlimited','not_mentioned','uncertain'])
def test_budget_states_are_distinct(state):
    r=execute(query(budget={'state':state,'unit':'万元','source_ids':['m1']}),[row('a',3),row('b',10)])
    assert r['match_count']==(0 if state=='uncertain' else 2)
    assert r['requirement_uncertain']==(state=='uncertain')


def test_unknown_fields_unsupported_conditions_and_empty_inventory_differ():
    q=query(filters=[{'field':'mileage_km','op':'lte','value':60000,'source_ids':['m1']}],unsupported_requirements=['无大修'])
    r=execute(q,[row('a',3),row('b',3,mileage_km=50000)])
    assert r['unknown_fields']=={'a':['mileage_km']} and r['match_count']==1
    assert r['unsupported_requirements']==['无大修']
    empty=execute(q,[]);assert empty['scan_complete'] and empty['scanned_count']==0 and not empty['unknown_fields']


@pytest.mark.parametrize('change',[
    {'tenant_id':'other'}, {'version':True},{'source_ids':['not-current']},
    {'budget':{'state':'specified','unit':'元','mode':'upper','value':4,'source_ids':['m1']}},
    {'budget':{'state':'specified','unit':'万元','mode':'upper','value':True,'source_ids':['m1']}},
    {'budget':{'state':'specified','unit':'万元','mode':'range','lower':5,'upper':3,'source_ids':['m1']}},
    {'filters':[{'field':'sql','op':'eq','value':'SELECT *','source_ids':['m1']}]},
    {'operation':'compare'},
])
def test_invalid_contract_cannot_query(change):
    with pytest.raises(cq.CatalogQueryError):cq.normalize_query(query(**change),cq.message_sources(evidence()))


@pytest.mark.parametrize('profile',['','routine_product_fast','low_authority_fast'])
def test_query_receipt_reaches_every_prompt_and_guard(profile):
    pack=evidence();receipt=execute(query(reference_ids=['h']),[row('a',3),row('h',12)])
    cq.attach_receipt(pack,receipt)
    prompt=brain.build_brain_prompt_pack(settings={'prompt_profile':profile},brain_input={'evidence':pack})
    text=json.dumps(prompt,ensure_ascii=False)
    assert receipt['query_id'] in text and 'budget_bounds' in text and 'current_message' in text
    assert 'unknown_fields' not in json.dumps(cq.prompt_protocol(pack))
    assert cq.query_plan_errors({'catalog_receipt':receipt},pack)
    assert cq.query_plan_errors({'evidence_used':{'product_ids':['a']}},evidence())==['current_inventory_not_queried']


def test_storage_failure_not_empty_inventory_and_second_query_rejected():
    pack=evidence();inp={'evidence':pack,'target':{'conversation_id':'c'},'current_message':{'message_ids':['m1']}}
    class Broken(Runtime):
        def list_items(self,kind):raise OSError('private error must not be surfaced')
    first={'ok':True,'brain_plan':{'catalog_query':query()}}
    bad=cq.run_catalog_round(first_result=first,settings={},brain_input=inp,run_llm=lambda **_:pytest.fail('must not call'),runtime_factory=lambda:Broken([]))
    assert bad['error']=='catalog_query_unavailable' and not pack['catalog_protocol'].get('receipt')
    repeat=cq.run_catalog_round(first_result=first,settings={},brain_input=inp,run_llm=lambda **_:first,runtime_factory=lambda:Runtime([row('a',3)]))
    assert repeat['error']=='catalog_second_query_or_forged_receipt'


def test_catalog_changed_during_reply_never_passes():
    rt=Runtime([row('a',3)]);pack=evidence()
    def model(**kwargs):rt.rows[0]['data']['price']=20;return {'ok':True,'brain_plan':{}}
    r=cq.run_catalog_round(first_result={'ok':True,'brain_plan':{'catalog_query':query()}},settings={},brain_input={'evidence':pack,'target':{'conversation_id':'c'},'current_message':{'message_ids':['m1']}},run_llm=model,runtime_factory=lambda:rt)
    assert r['error']=='catalog_changed_during_generation'


def test_normal_chat_does_not_query_or_add_model_call():
    first={'ok':True,'brain_plan':{'reply_segments':['你好']}}
    assert cq.run_catalog_round(first_result=first,settings={},brain_input={'evidence':evidence()},run_llm=lambda **_:pytest.fail('extra call'),runtime_factory=lambda:pytest.fail('extra query')) is first


@pytest.mark.parametrize('result',[{}, {'verdict':'pass'}, {'verdict':'repair','confidence':0.9,'semantic_errors':['首付被当成总价'],'hard_boundary_concerns':[],'customer_visible_risk':'low','repair_instruction':'重新理解预算','reason':'需求错了'}])
def test_semantic_rejection_and_incomplete_pass_cannot_soft_pass(result):
    pack=evidence();cq.attach_receipt(pack,execute(query(),[row('a',3)]))
    r=reviewer.review_brain_reply_semantics(settings={'semantic_reviewer_result':result},brain_input={'current_message':{}},evidence_pack=pack,plan={},deterministic_quality={})
    assert not r['ok']


def test_hook_knowledge_defers_legacy_budget_parser(monkeypatch):
    monkeypatch.setattr(ev,'catalog_product_candidates',lambda *a,**k:pytest.fail('legacy parser called'))
    pack=ev.compact_knowledge_pack('预算四万首付一万',{'evidence':{'products':[{'id':'stale'}]}},max_rag_hits=0,max_rag_text_chars=100,max_catalog_candidates=8,defer_catalog=True)
    assert not pack['product_master']['items']
    assert not pack['product_master']['catalog_selection']['scan_complete']


def test_real_brain_entry_queries_then_replies_without_legacy_selection(monkeypatch):
    import run_customer_service_brain_contract_checks as fixtures
    # Actual evidence construction, prompts, normalization, semantic/Guard path.
    # Only external storage and model transport are replaced.
    runner=fixtures.brain_module
    import reply_evidence_builder as legacy_ev
    text='买车总预算四万，打算先付一万剩下分期，有什么车'
    rt=Runtime([row(f'h{i}',10+i) for i in range(32)]+[row('fit',3.58)])
    monkeypatch.setattr(ev,'KnowledgeRuntime',lambda:rt)
    monkeypatch.setattr(legacy_ev,'build_evidence_pack',lambda *a,**k:{'evidence':{'products':[]},'safety':{'allowed_auto_reply':True,'must_handoff':False,'reasons':[]}})
    monkeypatch.setattr(legacy_ev,'catalog_product_candidates',lambda *a,**k:pytest.fail('legacy semantic selection'))
    monkeypatch.setattr(runner,'resolve_llm_api_key',lambda **k:'synthetic-only')
    plan=fixtures.base_plan();plan.update(answer_mode='recommend_from_catalog',reply_segments=['四万预算内，可以先看合成车fit，报价3.58万。'],evidence_used={'product_ids':['fit']},facts_claimed=[{'fact_type':'price','value':'3.58万','source_level':'product_master','source_id':'fit'}])
    calls=[]
    def transport(**kwargs):
        calls.append(kwargs)
        assert len(calls)<=2
        if len(calls)==1:payload={'catalog_query':query()}
        else:
            prompt=kwargs['messages'][1]['content']
            assert 'scan_complete' in json.dumps(prompt)
            assert '合成车fit' in json.dumps(prompt,ensure_ascii=False)
            payload=plan
        return {'ok':True,'status':200,'response_text':json.dumps(payload,ensure_ascii=False)}
    monkeypatch.setattr(runner,'call_llm_request_with_failover',transport)
    config=fixtures.base_config(plan)
    config['customer_service_brain'].update(mode='brain_first',provider='openai',low_authority_fast_enabled=False,
        semantic_reviewer_result={'verdict':'pass','confidence':0.9,'semantic_errors':[],'hard_boundary_concerns':[], 'customer_visible_risk':'low','repair_instruction':'','reason':'四万总预算，价格有据'})
    result=runner.maybe_run_customer_service_brain(config=config,target_name='合成客户',target_state={
        'history_authority':ev.CHEJIN_HISTORY_AUTHORITY,'chejin_brain_context':{'history_authority':ev.CHEJIN_HISTORY_AUTHORITY,'history':[],'history_text':'','current_batch_text':text}},
        batch=[{'id':'m1','sender':'customer','content':text}],combined=text,decision=fixtures.ReplyDecision('','',False,False,''),reply_text='',intent_assist={},rag_reply={},llm_reply={},product_knowledge={},data_capture={},raw_capture={'conversation':{'conversation_id':'synthetic-full','chat_type':'private'}},customer_profile=None)
    assert result['adoptable'] and result['guard']['allowed'], json.dumps(result,ensure_ascii=False)
    assert len(calls)==2 and result['reply_text']==plan['reply_segments'][0]
    assert result['audit_summary']['catalog_selection']['scanned_count']==33
