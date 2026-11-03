"""Full Brain→query→repair→semantic→Guard regressions with synthetic transports only."""
from copy import deepcopy
import json
import sys
from pathlib import Path
import pytest
from test_catalog_query import cq, ev, Runtime, row, query, evidence, execute
import run_customer_service_brain_contract_checks as fixtures
import reply_evidence_builder as legacy_ev


def review(kind='vehicle_facts', **changes):
    value={'verdict':'pass','catalog_claim_kind':kind,'confidence':0.95,'semantic_errors':[],
           'hard_boundary_concerns':[],'customer_visible_risk':'low','repair_instruction':'','reason':'合成审核'}
    value.update(changes);return value


def run_entry(monkeypatch, *, rows, make_plan, semantic, raw_query=None, before_plan=None, config_changes=None):
    runner=fixtures.brain_module
    text='买车总预算四万，打算先付一万剩下分期，有什么车'
    rt=Runtime(rows);calls=[];receipts=[]
    original_attach=cq.attach_receipt
    def attach(pack, receipt):
        receipts.append(receipt);return original_attach(pack,receipt)
    monkeypatch.setattr(cq,'attach_receipt',attach)
    monkeypatch.setattr(ev,'KnowledgeRuntime',lambda:rt)
    monkeypatch.setattr(legacy_ev,'build_evidence_pack',lambda *a,**k:{'evidence':{'products':[]},'safety':{'allowed_auto_reply':True,'must_handoff':False,'reasons':[]}})
    monkeypatch.setattr(legacy_ev,'catalog_product_candidates',lambda *a,**k:pytest.fail('legacy selection'))
    monkeypatch.setattr(runner,'resolve_llm_api_key',lambda **k:'synthetic')
    def transport(**kwargs):
        calls.append(kwargs)
        assert len(calls)<=8
        if before_plan:before_plan(len(calls),rt)
        payload={'catalog_query':raw_query} if len(calls)==1 and raw_query else make_plan(receipts[-1] if receipts else None)
        return {'ok':True,'status':200,'response_text':json.dumps(payload,ensure_ascii=False)}
    monkeypatch.setattr(runner,'call_llm_request_with_failover',transport)
    config=fixtures.base_config(fixtures.base_plan())
    config['customer_service_brain'].update(mode='brain_first',provider='openai',low_authority_fast_enabled=False,semantic_reviewer_result=semantic)
    config['customer_service_brain'].update(config_changes or {})
    result=runner.maybe_run_customer_service_brain(config=config,target_name='合成客户',target_state={
        'history_authority':ev.CHEJIN_HISTORY_AUTHORITY,'chejin_brain_context':{'conversation_id':'synthetic-full','history_authority':ev.CHEJIN_HISTORY_AUTHORITY,'history':[],'history_text':'','current_batch_text':text}},
        batch=[{'id':'m1','sender':'customer','content':text}],combined=text,decision=fixtures.ReplyDecision('','',False,False,''),reply_text='',intent_assist={},rag_reply={},llm_reply={},product_knowledge={},data_capture={},raw_capture={'conversation':{'conversation_id':'synthetic-full','chat_type':'private'}},customer_profile=None)
    return result,calls


def summary_plan(receipt, declared=True):
    plan=fixtures.base_plan()
    plan.update(answer_mode='direct_answer',reply_segments=['按您四万元以内的条件，目前没有匹配的在售车辆。'],evidence_used={},facts_claimed=[])
    if declared:
        plan['facts_claimed']=[{'fact_type':'catalog_match_count','value':0,'source_level':'catalog_query','source_id':receipt['query_id'] if receipt else 'invented'}]
    return plan


@pytest.mark.parametrize('queried,declared,allowed',[(False,False,False),(False,True,False),(True,False,False),(True,True,True)])
def test_same_absence_claim_requires_complete_query_and_declared_aggregate(monkeypatch,queried,declared,allowed):
    result,calls=run_entry(monkeypatch,rows=[],make_plan=lambda r:summary_plan(r,declared),semantic=review('query_summary'),raw_query=query() if queried else None)
    assert bool(result.get('adoptable') and result.get('guard',{}).get('allowed')) is allowed,json.dumps(result,ensure_ascii=False)


def test_budget_rejection_survives_all_repairs_with_legacy_softpass_enabled(monkeypatch):
    def plan(_):
        p=fixtures.base_plan();p.update(answer_mode='recommend_from_catalog',reply_segments=['可以先看合成车fit，报价0.8万。'],evidence_used={'product_ids':['fit']},facts_claimed=[{'fact_type':'price','value':'0.8万','source_level':'product_master','source_id':'fit'}]);return p
    q=query();q['budget']['value']=1
    result,_=run_entry(monkeypatch,rows=[row('fit',.8),row('four',3.58)],make_plan=plan,raw_query=q,
        semantic=review(verdict='repair',semantic_errors=['一万是首付款，客户总额为四万，需求理解有误'],repair_instruction='重新理解总预算'))
    assert not result.get('adoptable'),json.dumps(result,ensure_ascii=False)
    assert not result.get('repaired_quality_soft_pass',{}).get('ok')


def test_ordinary_chat_has_one_generation_and_required_review(monkeypatch):
    def plan(_):
        p=fixtures.base_plan();p.update(answer_mode='direct_answer',reply_segments=['好的，您方便时再聊。'],evidence_used={},facts_claimed=[]);return p
    result,calls=run_entry(monkeypatch,rows=[],make_plan=plan,semantic=review('non_inventory'))
    assert result['adoptable'] and result['guard']['allowed'],json.dumps(result,ensure_ascii=False)
    assert len(calls)==1


@pytest.mark.parametrize('field,op,value',[('energy_type','eq','新能源纯电'),('drive_type','eq','前驱'),('battery_capacity_kwh','gte',50),('displacement','eq','1.5T')])
def test_real_vehicle_producer_query_field_contract(field,op,value):
    root=Path(__file__).resolve().parents[5]
    sys.path.insert(0,str(root/'backend'))
    from app.schemas.vehicle import VehicleCreate
    from app.services.vehicle_service import _build_product_payload
    v=VehicleCreate(display_name='合成电车',brand='合成',series='国产新能源',model='合成A',energy_type='electric',battery_capacity_kwh='60',drive_type='front_wheel_drive',public_price='3.58',displacement='1.5T')
    payload=_build_product_payload('synthetic',v.model_dump(),listed=True)
    r=execute(query(filters=[{'field':field,'op':op,'value':value,'source_ids':['m1']}]),[payload])
    assert r['match_count']==1 and not r['unknown_fields']


@pytest.mark.parametrize('field,op,value',[('energy_type','eq','electric-ish'),('displacement','lte',2)])
def test_unsupported_vehicle_representation_is_not_zero_inventory(field,op,value):
    with pytest.raises(cq.CatalogQueryError):execute(query(filters=[{'field':field,'op':op,'value':value,'source_ids':['m1']}]),[])


def name(text,kind='reference'):
    return {'text':text,'kind':kind,'source_ids':['m1']}


def test_named_comparison_before_budget_and_display_limit():
    rows=[row(str(i),i+10) for i in range(40)]
    rows[-1]['data']['name']='2024 合成 1.5T';rows[-2]['data']['name']='2024 合成 2.0T'
    r=execute(query(operation='compare',name_queries=[name('２０２４合成１.５Ｔ'),name('2024合成2.0T')]),rows)
    assert [x['status'] for x in r['name_results']]==['unique','unique']
    assert {p['id'] for p in r['references']}=={'38','39'} and not r['recommendations']
    assert all(p['within_budget'] is False for p in r['references'])


def test_ambiguous_exclusion_cannot_silently_ignore_or_exclude_everything():
    rows=[row(str(i),3,model='合成A') for i in range(35)]
    r=execute(query(name_queries=[name('合成A','exclude')]),rows)
    match=r['name_results'][0]
    assert match['status']=='ambiguous' and match['match_count']==35 and len(match['matched_ids'])==35 and match['truncated']
    assert r['requirement_uncertain'] and not r['recommendations'] and not cq.aggregate_ready(r)


def test_missing_name_not_no_inventory_and_reference_exclusion_conflict():
    r=execute(query(operation='details',name_queries=[name('unknown')]),[row('one',3)])
    assert r['name_results'][0]['status']=='not_found' and r['scanned_count']==1 and not cq.aggregate_ready(r)
    with pytest.raises(cq.CatalogQueryError,match='conflict'):
        execute(query(name_queries=[name('合成车one'),name('合成车one','exclude')]),[row('one',3)])


def test_untrusted_ids_and_changed_reply_cannot_reuse_clearance():
    with pytest.raises(cq.CatalogQueryError,match='trusted'):
        cq.normalize_query(query(reference_ids=['invented']),cq.message_sources(evidence()))
    pack=evidence();receipt=execute(query(),[]);cq.attach_receipt(pack,receipt)
    p=summary_plan(receipt)
    assert cq.record_semantic_clearance(pack,p,review('query_summary'),{'ok':True})['ok']
    assert cq.clearance_kind(p['reply_segments'][0],pack)=='query_summary'
    assert cq.clearance_kind('店里什么车都没有',pack) is None
    pack['catalog_protocol']['receipt']['match_count']=3
    assert cq.clearance_kind(p['reply_segments'][0],pack) is None


def test_snapshot_change_during_semantic_review_cannot_reach_send(monkeypatch):
    saved=cq.record_semantic_clearance
    rows=[]
    def change(pack,plan,raw,result):
        value=saved(pack,plan,raw,result)
        rows.append(row('new',3.5))
        return value
    monkeypatch.setattr(cq,'record_semantic_clearance',change)
    result,_=run_entry(monkeypatch,rows=rows,make_plan=summary_plan,semantic=review('query_summary'),raw_query=query())
    assert not result.get('adoptable'),result
    assert rows, 'semantic review was not reached'


def test_all_brain_query_completion_and_repair_calls_share_deadline(monkeypatch):
    from apps.wechat_ai_customer_service import llm_config
    clock=[100.0];remaining=[]
    monkeypatch.setattr(llm_config.time,'monotonic',lambda:clock[0])
    def advance(stage,rt):
        remaining.append(llm_config.llm_total_time_budget_remaining_seconds())
        clock[0]+=3
    result,_=run_entry(monkeypatch,rows=[],make_plan=lambda r:summary_plan(r,False),semantic=review('query_summary'),raw_query=query(),before_plan=advance,config_changes={'total_time_budget_seconds':30})
    assert not result.get('adoptable')
    assert len(remaining)>=3 and all(v is not None for v in remaining)
    assert remaining==[30-3*i for i in range(len(remaining))],remaining
    assert llm_config.llm_total_time_budget_remaining_seconds() is None


def test_summary_authority_does_not_authorize_arbitrary_price():
    from apps.wechat_ai_customer_service.workflows.llm_reply_guard import validate_reply_fact_authority
    pack=evidence();r=execute(query(budget={'state':'not_mentioned','unit':'万元','source_ids':[]}),[row('a',3),row('b',8)])
    cq.attach_receipt(pack,r)
    for amount,ok in [(8,True),(9,False)]:
        plan=summary_plan(r);plan['reply_segments']=[f'符合条件的车最高{amount}万。']
        plan['facts_claimed']=[{'fact_type':'catalog_price_max','value':8,'source_level':'catalog_query','source_id':r['query_id']}]
        cq.record_semantic_clearance(pack,plan,review('query_summary'),{'ok':True})
        assert validate_reply_fact_authority(plan['reply_segments'][0],pack)['ok'] is ok


def test_missing_empty_budget_source_normalizes_only_not_mentioned():
    r=execute(query(budget={'state':'not_mentioned','unit':'万元'}),[row('a',3)])
    assert r['query']['budget']['source_ids']==[] and r['match_count']==1
    for state in ('specified','unlimited','uncertain'):
        with pytest.raises(cq.CatalogQueryError):execute(query(budget={'state':state,'unit':'万元'}),[])


@pytest.mark.parametrize('initial_failure',['confidence','price'])
@pytest.mark.parametrize('final_review',['pass','reject','unavailable','changed_reply','changed_receipt'])
def test_second_guard_repair_gets_fresh_semantic_review(monkeypatch, initial_failure, final_review):
    runner=fixtures.brain_module
    original=runner.review_brain_reply_semantics
    observed=[]
    final_text='合成车fit报价3.58万，您可以先了解一下。'
    def audited(**kwargs):
        final=kwargs['plan']['reply_segments']==[final_text]
        if final and final_review in ('reject','unavailable'):
            kwargs['settings']={**kwargs['settings'],'semantic_reviewer_result':
                review(verdict='repair',semantic_errors=['合成审核拒绝']) if final_review=='reject' else {'status':'error','error':'synthetic timeout'}}
        result=original(**kwargs)
        observed.append((kwargs['plan']['reply_segments'][:], result.get('ok')))
        if final and final_review=='changed_reply':
            kwargs['plan']['reply_segments']=['合成车fit报价3.58万，欢迎了解。']
        if final and final_review=='changed_receipt':
            kwargs['evidence_pack']['catalog_protocol']['receipt']['match_count']+=1
        return result
    monkeypatch.setattr(runner,'review_brain_reply_semantics',audited)
    produced=[]
    def plan(_):
        n=len(produced)
        p=fixtures.base_plan()
        text=final_text if n>=2 else ('可以先看合成车fit，报价3.58万，建议' if n==1 else
              '可以先看合成车fit，报价9万。' if initial_failure=='price' else '可以先看合成车fit，报价3.58万。')
        p.update(answer_mode='recommend_from_catalog',confidence=.1 if n==0 and initial_failure=='confidence' else .95,
                 reply_segments=[text],evidence_used={'product_ids':['fit']},
                 facts_claimed=[{'fact_type':'price','value':'3.58万','source_level':'product_master','source_id':'fit'}])
        produced.append(p);return deepcopy(p)
    result,calls=run_entry(monkeypatch, rows=[row('fit',3.58)],make_plan=plan,raw_query=query(),semantic=review())
    assert len(produced)==3 and len(calls)==4
    assert any(text==[final_text] for text,ok in observed),observed
    assert bool(result.get('adoptable')) is (final_review=='pass'),result
    if final_review in ('reject','unavailable'):
        assert result['post_guard_quality_gate_v2']['ok'] is False
    elif final_review.startswith('changed_'):
        assert result['guard_repaired_guard']['reason']=='catalog_semantic_clearance_missing_or_stale'
