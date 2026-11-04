"""Mechanism/negative tests; local fixtures do not assert real model quality."""
from copy import deepcopy
import json
import pytest
from test_catalog_query import Runtime, evidence, row
from apps.wechat_ai_customer_service.workflows import catalog_query as cq
from apps.wechat_ai_customer_service.workflows import customer_service_brain as brain
from apps.wechat_ai_customer_service.workflows import customer_service_quality_reviewer as reviewer
from apps.wechat_ai_customer_service.workflows import reply_evidence_builder as ev
from apps.wechat_ai_customer_service.workflows.vehicle_public_facts import validate_reply_public_facts


def prepared(text='5—8万有什么车？', history=(), rows=None, limit=8):
    pack=evidence(text)
    pack['catalog_protocol']['model_query_contract']=cq.MODEL_QUERY_CONTRACT
    pack['conversation']['history']=[{'id':f'h{i}', 'sender':'customer', 'content':t} for i,t in enumerate(history)]
    rows=rows if rows is not None else [row(str(i),p) for i,p in enumerate([5.2,5.6,6.0,6.4,6.8,7.2],1)]
    for item in rows:item['source']={'type':'chejin_backend'}
    runtime=Runtime(rows)
    inp={'evidence':pack, 'target':{'conversation_id':'c1'}, 'current_message':{'message_ids':['m1']}}
    result=cq.prepare_catalog_round(evidence=pack,settings={'max_catalog_candidates':limit},brain_input=inp,runtime_factory=lambda:runtime)
    return pack,result,inp,runtime


@pytest.mark.parametrize('text,budget,first',[
    ('5—8万有什么车？','range','4'),('五到八万左右，有什么合适的车？','range','4'),
    ('8万以内有什么车？','upper','6'),('没有预算限制，看看有哪些车。',None,'1'),
    ('8万以内，便宜的先给我看。','upper','1'),('总价别超过8万，便宜的先推荐。','upper','1'),
])
def test_explicit_requests_need_no_understanding_call(text,budget,first):
    pack,result,_,rt=prepared(text)
    assert result['route']=='direct',result
    receipt=pack['catalog_protocol']['receipt']
    assert receipt['recommendations'][0]['id']==first
    assert receipt['query']['budget'].get('mode')==budget
    assert rt.calls==['products']


@pytest.mark.parametrize('reverse',[False,True])
@pytest.mark.parametrize('limit',[1,4,8])
def test_f01_all_inventory_before_order_and_limit(reverse,limit):
    rows=[row(f'expensive{i}',10.68+i) for i in range(25)]+[row('a',3.58),row('b',3.78),row('c',3.98)]
    if reverse:rows.reverse()
    pack,result,_,_=prepared('那请问你们4万块钱左右，你们有什么车',history=['我想买一台三五万的代步车'],rows=rows,limit=limit)
    assert result['route']=='direct',result
    receipt=pack['catalog_protocol']['receipt']
    assert receipt['scanned_count']==28 and receipt['match_count']==3
    assert [p['id'] for p in receipt['recommendations']]==['c','b','a'][:limit]


@pytest.mark.parametrize('text,history',[
    ('有年份近一点的吗？',['有新能源吗']),('只看纯电车。',['预算8万以内']),
    ('总价4万且首付1—2万，月供2000—3000',[]),('4万，不要10万的',[]),
    ('刚才那辆不要了，8万以内看看别的',[]),('8万以内，要红色无事故的',[]),
])
def test_partial_numeric_recognition_cannot_execute_or_drop_conditions(text,history):
    pack,result,_,rt=prepared(text,history)
    assert result['route']=='semantic'
    assert not rt.calls and not pack['catalog_protocol'].get('receipt')


@pytest.mark.parametrize('profile',['','lean','routine_product_fast','low_authority_fast'])
def test_all_prompt_consumers_receive_same_complete_authority(profile):
    rows=[row(str(i),5+i/5,customer_description='公开描述'*350,internal_notes='PRIVATE_SENTINEL') for i in range(1,7)]
    pack,result,inp,_=prepared(rows=rows)
    generation=brain.build_brain_prompt_pack(settings={'prompt_profile':profile,'prompt_item_text_chars':80},brain_input=inp)
    repair=brain.build_brain_repair_prompt_pack(settings={'prompt_profile':profile},brain_input=inp,plan={},quality={})
    review=reviewer.build_quality_review_request(settings={},brain_input=inp,evidence_pack=pack,plan={'reply_segments':['请看具体车辆']},deterministic_quality={})
    expected=ev.reply_authority_snapshot(pack)
    assert generation['user']['brain_input']['content_basis']==expected
    assert repair['user']['brain_input']['content_basis']==expected
    assert review['authority_evidence_summary']==expected
    assert len(expected['product_master']['items'])==6
    assert expected['product_master']['items'][0]['public_details']['customer_description']=='公开描述'*350
    assert 'PRIVATE_SENTINEL' not in json.dumps(expected,ensure_ascii=False)
    assert 'facts_claimed(list' not in generation['response_schema']


@pytest.mark.parametrize('amount',['6.4万元','６．４万元','六点四万元','64000元','六万四千元'])
def test_actual_quote_verified_without_model_source_declarations(amount):
    pack,_,_,_=prepared()
    result=validate_reply_public_facts({'reply_segments':['合成车4报价'+amount+'。']},pack)
    assert result['ok'],result
    assert result['product_ids']==['4']
    assert any(f['fact_type']=='price' and f['value']==6.4 for f in result['facts_claimed'])


@pytest.mark.parametrize('text',[
    '合成车4报价6.4万元，合成车5报价4.6万元。',
    '合成车4报价6.4元。','合成车1报价5.2万元。','有多辆车，您想要什么类型呢？',
])
def test_wrong_second_price_unit_order_or_degraded_answer_cannot_pass(text):
    pack,_,_,_=prepared()
    assert not validate_reply_public_facts({'reply_segments':[text]},pack)['ok']


def test_summary_request_can_answer_count_without_forced_recommendation():
    pack,result,_,_=prepared('5—8万有多少台车？')
    assert result['route']=='direct',result
    validated=validate_reply_public_facts({'reply_segments':['这个范围有6台车。']},pack)
    assert validated['ok'],validated


def test_correct_id_does_not_hide_conflicting_model_price_declaration():
    pack,_,_,_=prepared()
    result=validate_reply_public_facts({'reply_segments':['合成车4报价6.4万元。'],
        'facts_claimed':[{'fact_type':'price','value':'9万','source_level':'product_master','source_id':'4'}]},pack)
    assert 'model_declared_fact_conflict' in result['errors']


def test_review_contradiction_is_not_pass():
    result=reviewer.normalize_quality_review_result({'verdict':'pass','semantic_errors':['后半句错价'],'hard_boundary_concerns':[]})
    assert not result['ok']


def policy_pack():
    pack,_,inp,_=prepared('你好')
    pack['knowledge_release']={'release_id':'release-1','version':'v1'}
    pack['knowledge']['formal_knowledge']={'faq':[
        {'source_id':'knowledge:appointment@r1','knowledge_release_id':'release-1','knowledge_revision_id':'r1',
         'answer':'预约看车需提前一天联系，门店核实车源和排期后确认。'},
        {'source_id':'knowledge:finance@r2','knowledge_release_id':'release-1','knowledge_revision_id':'r2',
         'answer':'贷款可以提交申请，审批结果以资方审核为准，不承诺通过。'}]}
    return pack,inp


POLICY_TEXT = '看车请提前1天联系，门店确认车源和时间后再安排。'


def policy_review():
    from test_catalog_query_entry import review
    return review('non_inventory', policy_refs={'S1':['K1']})


def clear_policy(pack,raw,text=POLICY_TEXT):
    from test_catalog_query_entry import fixtures
    p=fixtures.base_plan()
    p.update(answer_mode='direct_answer',reply_segments=[text],facts_claimed=[],evidence_used={})
    out=cq.record_semantic_clearance(pack,p,raw,reviewer.normalize_quality_review_result(raw))
    return p,out


def test_policy_paraphrase_derives_only_actual_used_versioned_source():
    pack,_=policy_pack();raw=policy_review()
    before=validate_reply_public_facts({'reply_segments':[POLICY_TEXT]},pack)
    assert before['ok'] and before['occurrences'][0]['pending_policy_review']
    plan,result=clear_policy(pack,raw)
    assert result['ok'],result
    assert plan['evidence_used']['formal_knowledge_ids']==['knowledge:appointment@r1']
    assert plan['facts_claimed']==[{'fact_type':'policy','value':POLICY_TEXT,
        'source_level':'formal_knowledge','source_id':'knowledge:appointment@r1'}]
    assert plan['policy_refs']=={'S1':['K1']}
    mapping=pack['policy_reference_map']
    assert mapping['statements']['S1']['text']==POLICY_TEXT
    assert mapping['knowledge']['K1']['source_id']=='knowledge:appointment@r1'


@pytest.mark.parametrize('mutation',['unknown_knowledge','wrong_actual_source','invalid_field','missing','uncertain',
    'wrong_number','omit_false_claim','unknown_statement','partial_coverage'])
def test_policy_review_pass_cannot_hide_bad_sources_unsupported_or_omitted_claims(mutation):
    pack,_=policy_pack();raw=policy_review();text=POLICY_TEXT
    if mutation=='unknown_knowledge':raw['policy_refs']={'S1':['K999']}
    if mutation=='wrong_actual_source':raw['policy_refs']={'S1':['K2']}
    if mutation=='invalid_field':raw['policy_refs']={'S1':{'invented':'value'}}
    if mutation=='missing':raw.pop('policy_refs')
    if mutation=='uncertain':raw.update(verdict='repair',semantic_errors=['政策适用性不明确'])
    if mutation=='wrong_number':text=text.replace('1天','2天')
    if mutation in ('omit_false_claim','partial_coverage'):text+='贷款保证通过。'
    if mutation=='unknown_statement':raw['policy_refs']={'S99':['K1']}
    _,result=clear_policy(pack,raw,text)
    assert not result['ok'],result


def test_multiple_policy_sources_combine_without_inventing_independent_proofs():
    pack,_=policy_pack();raw=policy_review();raw['policy_refs']={'S1':['K1','K2','K1']}
    text='预约看车要提前一天联系并由门店确认，贷款可申请但不能保证通过。'
    plan,result=clear_policy(pack,raw,text)
    assert result['ok'],result
    assert plan['evidence_used']['formal_knowledge_ids']==['knowledge:appointment@r1','knowledge:finance@r2']
    assert plan['policy_refs']=={'S1':['K1','K2']}
    assert len(plan['facts_claimed'])==1
    assert plan['facts_claimed'][0]['source_id']=='knowledge:appointment@r1|knowledge:finance@r2'


def test_missing_policy_refs_is_protocol_failure_for_http_and_config_replay():
    pack,inp=policy_pack();raw=policy_review();raw.pop('policy_refs')
    assert not reviewer.quality_reviewer_contract_is_complete(raw,policy_refs_required=True)
    result=reviewer.review_brain_reply_semantics(settings={'semantic_reviewer_enabled':True,
        'semantic_reviewer_result':raw},brain_input=inp,evidence_pack=pack,
        plan={'reply_segments':['您好']},deterministic_quality={'ok':True})
    assert not result['ok'] and result['error_kind']=='protocol',result
    assert result['contract_errors']==['policy_refs:expected_object']


def test_mapping_keeps_every_original_unit_and_rejects_duplicate_response_keys():
    from apps.wechat_ai_customer_service.workflows.llm_output_adapter import parse_complete_llm_json_response
    pack,_=policy_pack();segments=['预约请提前一天联系。门店确认后安排！','贷款可申请；结果以审批为准。']
    mapping=ev.prepare_policy_reference_map({'reply_segments':segments},pack)
    for index,original in enumerate(segments):
        assert ''.join(row['text'] for row in mapping['statements'].values() if row['segment_index']==index)==original
    assert parse_complete_llm_json_response({'response_text':'{"policy_refs":{"S1":["K1"],"S1":["K2"]}}'}) is None


@pytest.mark.parametrize('field,value',[('answer_mode','nonsense'),('recommended_action','force_send'),
    ('can_answer','true'),('schema_version','wrong'),('confidence',float('nan')),('risk',{'needs_handoff':'false'})])
def test_draft_invalid_types_are_reported_before_normalization_can_hide_them(field,value):
    from test_catalog_query_entry import fixtures
    from apps.wechat_ai_customer_service.workflows.customer_service_brain_contract import normalize_brain_plan,validate_brain_plan
    pack,_,_,_=prepared();raw=fixtures.base_plan();raw[field]=value
    p=normalize_brain_plan(raw,evidence_pack=pack)
    assert not validate_brain_plan(p)['ok']


def test_customer_budget_after_vehicle_is_not_a_second_price_and_stale_budget_rejected():
    pack,_,_,_=prepared('5—8万有什么车？',history=['预算10万'])
    assert validate_reply_public_facts({'reply_segments':['合成车4报价6.4万元，您的预算是5—8万元。']},pack)['ok']
    assert not validate_reply_public_facts({'reply_segments':['合成车4报价6.4万元，您的预算是10万元。']},pack)['ok']


def test_message_and_serialized_plan_preserve_tail():
    from apps.wechat_ai_customer_service.workflows.customer_service_brain_contract import compact_brain_plan
    text='合成内容'*180+'不要丰田'
    assert ev.compact_message({'content':text})['content']==text
    assert compact_brain_plan({'reply_segments':[text]})['reply_segments']==[text]


@pytest.mark.parametrize('wrong',[2,10])
def test_semantic_query_cannot_replace_cited_explicit_total_with_other_amount(wrong):
    pack,route,_,_=prepared('总价8万以内，首付2万，月供希望1500左右。')
    assert route['route']=='semantic'
    q=deepcopy(cq.V2_QUERY_EXAMPLE['catalog_query'])
    q.update(source_ids=['m1'],budget={'state':'specified','mode':'upper','value':wrong,'source_ids':['m1']})
    with pytest.raises(cq.CatalogQueryError,match='budget_conflicts_with_explicit_source'):
        cq.normalize_model_query(q,cq.message_sources(pack),contract=cq.MODEL_QUERY_CONTRACT)


def test_declared_unknown_product_id_is_not_hidden_by_program_derivation():
    pack,_,_,_=prepared()
    out=brain.validate_plan_against_evidence({'reply_segments':['合成车4报价6.4万元。'],
        'evidence_used':{'product_ids':['invented']}},pack)
    assert 'product_fact_source_not_in_evidence:invented' in out['errors']


def test_customer_downpayment_preference_is_not_seller_loan_promise():
    from test_catalog_query_entry import review
    pack,_,_,_=prepared('总价8万以内，首付2万，月供希望1500左右。')
    _,out=clear_policy(pack,review('non_inventory'),text='您计划首付2万元。')
    assert out['ok'],out
    _,out=clear_policy(pack,review('non_inventory'),text='您计划首付2万元就能买。')
    assert not out['ok']


def test_provider_explicit_capacity_error_preserves_http_status_and_blocks_failover(monkeypatch):
    import io,urllib.error
    from apps.wechat_ai_customer_service import llm_config
    def fail(*args,**kwargs):
        raise urllib.error.HTTPError('https://synthetic.invalid',400,'Bad Request',{},
            io.BytesIO(b'{"error":{"code":"context_length_exceeded","message":"input too long"}}'))
    monkeypatch.setattr(llm_config.urllib.request,'urlopen',fail)
    result=llm_config.call_llm_request_once(provider='openai',api_key='synthetic',base_url='https://synthetic.invalid',
        model='synthetic',messages=[],timeout=1,max_tokens=100)
    assert result['status']==400 and result['error_kind']=='input_capacity_exceeded'
    assert not llm_config.is_failoverable_llm_failure(result)
    assert llm_config.ai_failure_category(result)=='input_capacity_exceeded'


@pytest.mark.parametrize('text,blocked',[
    ('预约需门店确认，贷款保证通过。', True),
    ('预约确认后回复您，贷款保证通过。', True),
    ('贷款不能保证通过，以资方审批为准。', False),
    ('无法保证贷款通过，需要资方审核。', False),
    ('可以协助申请贷款，结果以资方审批为准。', False),
])
def test_finance_guarantee_cannot_borrow_caution_from_other_clause(text,blocked):
    from apps.wechat_ai_customer_service.workflows.llm_reply_guard import has_unqualified_commitment
    assert has_unqualified_commitment(text)==blocked


def test_format_repair_capacity_stops_retry_and_keeps_full_failed_attempt(monkeypatch):
    pack,_,inp,_=prepared()
    original='invalid synthetic JSON '*200+'TAIL_CONDITION'
    calls=[]
    def transport(**request):
        calls.append(request)
        if len(calls)==1:
            return {'ok':True,'status':200,'response_text':original}
        return {'ok':False,'status':400,'error_kind':'input_capacity_exceeded','error':'context_length_exceeded'}
    monkeypatch.setattr(brain,'read_secret',lambda *args,**kwargs:None)
    monkeypatch.setattr(brain,'resolve_llm_api_key',lambda **kwargs:'synthetic-key')
    monkeypatch.setattr(brain,'call_llm_request_with_failover',transport)
    result=brain.run_brain_llm(settings={'provider':'openai','model':'synthetic','base_url':'https://synthetic.invalid'},brain_input=inp)
    assert result['error_kind']=='input_capacity_exceeded'
    assert len(calls)==2
    history=pack['model_response_history']
    assert len(history)==2 and history[0]['response']['response_text']==original
    assert 'synthetic-key' not in json.dumps(history)


@pytest.mark.parametrize('mutation',['none','missing_plan','plan','text','mapping'])
def test_final_fact_guard_rechecks_exact_approved_plan_and_mapping(mutation):
    from apps.wechat_ai_customer_service.workflows.llm_reply_guard import validate_reply_fact_authority
    pack,_=policy_pack();plan,result=clear_policy(pack,policy_review())
    assert result['ok']
    text=POLICY_TEXT
    if mutation=='missing_plan':plan=None
    if mutation=='plan':plan['policy_refs']={'S1':['K2']}
    if mutation=='text':text=text.replace('1天','2天')
    if mutation=='mapping':pack['policy_reference_map']['knowledge']['K1']['content']='改过的内容'
    final=validate_reply_fact_authority(text,pack,reviewed_plan=plan)
    assert final['ok']==(mutation=='none'),final
