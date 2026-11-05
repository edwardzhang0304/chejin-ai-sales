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
        plan={'reply_segments':[POLICY_TEXT]},deterministic_quality={'ok':True})
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


@pytest.mark.parametrize('segments,allowed',[
    (['可以看看6.4万元的合成车4和6.8万元的合成车5。'],True),
    (['可以看看6.4万元的合成车4，还有合成车5报价6.8万元。'],True),
    (['合成车4报价6.4万元，还有6.8万元的合成车5。'],True),
    (['合成车4和合成车5分别报价6.4万元、6.8万元。'],True),
    (['合成车4和合成车5分别报价6.8万元、6.4万元。'],False),
    (['合成车4和合成车5报价6.4万元、6.8万元。'],False),
    (['合成车4和合成车5分别报价6.4万元。'],False),
    (['合成车4和合成车5都报价6.4万元。'],False),
    (['6.8万元的合成车4和6.4万元的合成车5。'],False),
    (['9.9万元的合成车4和6.8万元的合成车5。'],False),
    (['6.4万元的合成车4和6.4万元的合成车5。'],False),
    (['合成车4。该车报价6.4万元。','合成车5。该车报价6.8万元。'],True),
    (['合成车4。报价6.4万元。','合成车5。报价6.8万元。'],True),
    (['合成车4。','该车报价6.4万元。','合成车5报价6.8万元。'],True),
    (['合成车4。','报价6.4万元。','合成车5报价6.8万元。'],True),
    (['合成车4和合成车5。前者报价6.4万元，后者报价6.8万元。'],True),
    (['合成车4和合成车5。','前者报价6.4万元，后者报价6.8万元。'],True),
    (['合成车4和合成车5。它报价6.4万元。'],False),
    (['合成车4和合成车5。','该车报价6.4万元。'],False),
    (['合成车4和合成车5。','报价6.4万元。'],False),
])
def test_actual_numeric_entity_assignment_is_word_order_independent(segments,allowed):
    pack,_,_,_=prepared()
    result=validate_reply_public_facts({'reply_segments':segments},pack)
    assert result['ok']==allowed,result
    if allowed:
        prices={f['source_id']:f['value'] for f in result['facts_claimed'] if f['fact_type']=='price'}
        assert prices=={'4':6.4,'5':6.8}


@pytest.mark.parametrize('text',[
    '合成车1和合成车2都报价6.4万元。',
    '合成车1和合成车2分别报价6.4万元、6.4万元。',
    '6.4万元的合成车1和6.4万元的合成车2。',
])
def test_equal_prices_do_not_require_value_based_entity_guessing(text):
    pack,_,_,_=prepared(rows=[row('1',6.4),row('2',6.4)])
    result=validate_reply_public_facts({'reply_segments':[text]},pack)
    assert result['ok'],result
    assert {f['source_id'] for f in result['facts_claimed'] if f['fact_type']=='price'}=={'1','2'}


@pytest.mark.parametrize('schema_errors,evidence_errors,category',[
    ([],['reply_numeric_fact_mismatch'],'content_review'),
    ([],['answer_known_price_omitted'],'content_review'),
    (['invalid_confidence'],[],'output_format'),
    (['invalid_confidence'],['reply_numeric_fact_mismatch'],'output_format'),
])
def test_plan_rejection_classification_retains_validation_owner(schema_errors,evidence_errors,category):
    validation=brain.merge_plan_evidence_validation({'ok':not schema_errors,'errors':schema_errors},
        {'ok':not evidence_errors,'errors':evidence_errors})
    result=brain.classify_no_visible_reply('brain_plan_validation_failed',payload={'plan_validation':validation})
    assert result['failure_category']==category,result
    assert validation['evidence_errors']==evidence_errors


def test_latest_fact_error_is_not_overridden_by_repaired_earlier_empty_reply():
    current=brain.merge_plan_evidence_validation({'ok':True,'errors':[]},
        {'ok':False,'errors':['reply_numeric_fact_mismatch']})
    result=brain.classify_no_visible_reply('brain_plan_validation_failed',payload={
        'plan_validation':{'ok':False,'errors':['missing_reply_segments']},
        'repaired_plan_validation':current})
    assert result['class']=='fact_validation_failed' and result['failure_category']=='content_review'


@pytest.mark.parametrize('question,prefix',[
    ('5—8万有什么车？','5到8万这个区间'),
    ('5—8万有什么车？','您五到八万这个预算'),
    ('5—8万有什么车？','50000到80000元这个范围'),
    ('5—8万有什么车？','预算五万到八万元'),
    ('8万以内有什么车？','8万以内'),
    ('8万以内有什么车？','不超过８万元'),
])
def test_budget_relation_restatement_does_not_require_prefix_keyword(question,prefix):
    pack,_,_,_=prepared(question)
    car=pack['catalog_protocol']['receipt']['recommendations'][0]
    result=validate_reply_public_facts({'reply_segments':[prefix+'可以看'+car['name']+'，报价'+str(car['price'])+'万。']},pack)
    assert result['ok'],result
    assert any(n.get('role')=='customer_requirement' for n in result['occurrences'])


@pytest.mark.parametrize('prefix',[
    '5到9万这个区间','5万以内','8万以上','8万左右','预算8万以内',
    '以前预算5到8万','服务费5到8万','5到8万的服务费',
])
def test_same_endpoint_or_old_budget_does_not_authorize_different_role_or_relation(prefix):
    pack,_,_,_=prepared()
    checked=validate_reply_public_facts({'reply_segments':[prefix+'，合成车4报价6.4万。']},pack)
    assert not checked['ok'],checked


@pytest.mark.parametrize('segments,allowed',[
    (['先给您两台价格低的：合成车4报价6.4万，合成车5报价6.8万。'],True),
    (['先说两台：合成车4报价6.4万，合成车5报价6.8万。'],True),
    (['合成车4报价6.4万。','合成车5报价6.8万。','这两台可供比较。'],True),
    (['合成车4报价6.4万，合成车5报价6.8万，这两台可供比较。'],True),
    (['先给两台：合成车4报价6.4万，合成车5报价6.8万，合成车3报价6万。'],False),
    (['先给两台：合成车4报价6.4万，合成车4报价6.4万。'],False),
    (['这两台可供比较。','合成车4报价6.4万，合成车5报价6.8万。'],False),
    (['库存一共2台。合成车4报价6.4万，合成车5报价6.8万。'],False),
    (['合成车4库存2台，报价6.4万。'],False),
])
def test_display_count_has_its_own_actual_unique_entity_scope(segments,allowed):
    pack,_,_,_=prepared()
    checked=validate_reply_public_facts({'reply_segments':segments},pack)
    assert checked['ok'] is allowed,checked


@pytest.mark.parametrize('profile',['','lean','routine_product_fast','low_authority_fast'])
def test_prepared_stage_and_repair_view_do_not_reintroduce_legacy_forms(profile):
    pack,_,inp,_=prepared('有年份近一点的吗？',history=['有新能源吗'])
    prompt=brain.build_brain_prompt_pack(settings={'prompt_profile':profile},brain_input=inp)
    assert prompt['user']['task']=='理解需求并选择唯一查询或非查询分支。'
    assert '首轮查车必须输出唯一完整外层对象' in prompt['system']
    assert '第二轮收到程序查询回执后' not in prompt['system']
    assert '仅非查询分支' in prompt['response_schema']
    plan={'reply_segments':['原文'], 'facts_claimed':[], 'evidence_used':{}, 'draft_structure_errors':[], 'self_check':{}}
    original=deepcopy(plan)
    repair=brain.build_brain_repair_prompt_pack(settings={},brain_input=inp,plan=plan,quality={})
    view=repair['user']['original_brain_plan']
    assert 'facts_claimed' not in view and 'evidence_used' not in view and 'draft_structure_errors' not in view
    assert plan==original and view['reply_segments']==['原文']


def test_terminal_validation_keeps_last_actual_plan_and_authority_error_category():
    from apps.wechat_ai_customer_service.workflows.customer_service_brain_contract import validate_brain_plan
    bad={'reply_segments':['合成车4报价6.4万。'], 'answer_mode':'recommend_from_catalog','recommended_action':'send_reply',
         'facts_claimed':[{'fact_type':'price','value':6.4,'source_level':'current_conversation','source_id':'4'}]}
    validation=brain.merge_plan_evidence_validation(validate_brain_plan(bad),{'ok':True,'errors':[]})
    assert validation['failure_category']=='content_review'
    original={'reply_segments':['旧稿']}
    payload=brain.build_brain_no_visible_reply_payload({'brain_plan':original,'plan_validation':{'ok':False,'errors':['old']},
        'repaired_brain_plan':bad,'repaired_plan_validation':validation},combined='有什么车',reason='brain_plan_validation_failed')
    assert payload['brain_plan']==bad and payload['initial_brain_plan']==original
    assert payload['plan_validation']==validation
    assert payload['no_visible_reply']['failure_category']=='content_review'
    assert payload['no_visible_reply']['failed_draft']['errors']==validation['errors']
    malformed={**bad,'draft_structure_errors':['invalid_draft_field:confidence']}
    newer=brain.merge_plan_evidence_validation(validate_brain_plan(malformed),{'ok':True,'errors':[]})
    assert newer['failure_category']=='output_format'


@pytest.mark.parametrize('parts,allowed',[
    (['先给您推荐两台：','合成车4报价6.4万，合成车5报价6.8万。'],True),
    (['先给您推荐三台：','合成车4报价6.4万，合成车5报价6.8万。'],False),
    (['先给您推荐两台：','合成车4报价6.4万。','合成车5报价6.8万。'],True),
    (['先给您推荐两台：','合成车4报价6.4万。','另外一组：合成车5报价6.8万。'],False),
    (['先给您推荐两台：','合成车4报价6.4万。','再补一台：合成车5报价6.8万。'],False),
    (['先给您推荐两台：','合成车4报价6.4万。','预约需要先核实。','合成车5报价6.8万。'],False),
])
def test_explicit_display_list_scope_crosses_segments_but_not_topics(parts,allowed):
    pack,_,_,_=prepared()
    checked=validate_reply_public_facts({'reply_segments':parts},pack)
    assert checked['ok'] is allowed,checked


def test_repair_retains_numeric_rejection_position_and_reply_digest():
    pack,_,_,_=prepared()
    plan={'reply_segments':['合成车4报价4万。']}
    validation=brain.merge_plan_evidence_validation({'ok':True,'errors':[]},brain.validate_plan_against_evidence(plan,pack))
    feedback=brain.compact_quality_verification(brain.plan_validation_repair_feedback(validation,combined='5—8万有什么车'))
    details=feedback['validation_details']
    assert len(details['reply_sha256'])==64
    assert details['rejected_numbers'][0]['text']=='4万'
    assert details['rejected_numbers'][0]['expected']==6.4
    assert details['rejected_numbers'][0]['role']=='price'


@pytest.mark.parametrize('segments,allowed',[
    (['先推荐两台：合成车4报价6.4万，合成车5报价6.8万。合成车4更接近预算。'],True),
    (['先推荐两台：','合成车4报价6.4万。','合成车5报价6.8万。','合成车4更接近预算。'],True),
    (['先推荐两台：合成车4报价6.4万，合成车4报价6.4万。'],False),
    (['先推荐两台：合成车4报价6.4万。合成车4更接近预算。'],False),
    (['先推荐两台：合成车4报价6.4万，合成车5报价6.8万。合成车3报价6万。'],False),
])
def test_list_members_differ_from_later_mentions(segments,allowed):
    pack,_,_,_=prepared()
    checked=validate_reply_public_facts({'reply_segments':segments},pack)
    assert checked['ok'] is allowed,checked


@pytest.mark.parametrize('relation',['不少于','小于','大于','少于','不到','超过','不足'])
def test_unsupported_comparator_cannot_fall_back_to_bare_amount_cap(relation):
    pack,_,_,_=prepared('8万以内有什么车？')
    checked=validate_reply_public_facts({'reply_segments':['预算'+relation+'8万，合成车6报价7.2万。']},pack)
    assert not checked['ok'],checked


@pytest.mark.parametrize('relation',['不少于','小于','大于','少于','不到','超过','不足'])
def test_unconsumed_budget_relation_uses_semantic_understanding(relation):
    from apps.wechat_ai_customer_service.workflows.catalog_budget import budget_statement
    text='预算'+relation+'8万有什么车？'
    parsed=budget_statement(text)
    assert parsed['unconsumed_comparisons']
    pack,result,_,runtime=prepared(text)
    assert result['route']=='semantic' and not runtime.calls


def test_recognized_comparator_does_not_consume_intervening_vehicle_condition():
    from apps.wechat_ai_customer_service.workflows.catalog_budget import budget_statement
    text='不超过红色8万有什么车？'
    parsed=budget_statement(text)
    assert '红色' in parsed['remainder']
    _,result,_,runtime=prepared(text)
    assert result['route']=='semantic' and not runtime.calls


def partial_year_pack(*, changes=None, rows=None):
    pack, preparation, inp, runtime = prepared('有年份近一点的吗', history=['有新能源吗'], rows=rows)
    raw = {'operation':'search', 'requirement_state':'uncertain',
           'budget':{'state':'not_mentioned','source_ids':[]},
           'filters':[{'field':'energy_type','op':'in','value':['hybrid','range_extended','electric'],'source_ids':['h0']}],
           'name_queries':[], 'reference_ids':[], 'exclude_ids':[], 'source_ids':['h0','m1'],
           'unsupported_requirements':['车辆年份较近（车龄/上牌年份）需按实际车源核验'],
           'interpretation':'先按新能源类型查全库，年份再据实际车源核验。'}
    if changes:raw.update(changes)
    if rows is None:
        for item in runtime.rows:item['data']['additional_details']['energy_type']='electric'
    query = cq.normalize_model_query(raw,cq.message_sources(pack),contract=cq.MODEL_QUERY_CONTRACT,
                                    known_ids={k:[{'source_id':'m1'}] for k in raw['reference_ids']+raw['exclude_ids']})
    receipt = cq.execute_query(query,runtime,scope={'conversation_id':'c1'},limit=8)
    cq.attach_receipt(pack,receipt)
    return pack,inp,receipt


def test_partial_year_preserves_candidates_and_null_through_all_consumers():
    pack,inp,receipt=partial_year_pack()
    assert receipt['query']['requirement_state']=='uncertain'
    assert receipt['evaluation_state']=='clarification_required'
    assert receipt['match_count'] is None and receipt['price_min'] is None and receipt['price_max'] is None
    assert receipt['known_condition_match_count']==6
    assert receipt['recommendations']==[]
    assert [p['id'] for p in receipt['known_condition_facts']]==['1','2','3','4','5','6']
    assert len(pack['knowledge']['product_master']['items'])==6
    expected=ev.reply_authority_snapshot(pack)
    draft={'answer_mode':'ask_clarifying_question','reply_segments':['合成车1报价5.2万，年份还需核实，您希望哪一年之后的车？']}
    for consumer in (
        brain.build_brain_prompt_pack(settings={},brain_input=inp)['user']['brain_input']['content_basis'],
        brain.build_brain_repair_prompt_pack(settings={},brain_input=inp,plan=draft,quality={})['user']['brain_input']['content_basis'],
        reviewer.build_quality_review_request(settings={},brain_input=inp,evidence_pack=pack,plan=draft,deterministic_quality={})['authority_evidence_summary'],
    ):
        assert consumer==expected
        restored=json.loads(json.dumps(consumer))
        assert restored['catalog_protocol']['receipt']['match_count'] is None
        assert restored['product_master']['catalog_selection']['known_condition_match_count']==6
    assert validate_reply_public_facts(draft,pack)['ok']


@pytest.mark.parametrize('changes,reason',[
    ({'budget':{'state':'uncertain','source_ids':['m1']}},'budget_uncertain'),
    ({'unsupported_requirements':[]},'uncertainty_scope_unresolved'),
    ({'reference_ids':['gone']},'missing_reference_ids'),
    ({'exclude_ids':['gone']},'missing_exclude_ids'),
    ({'name_queries':[{'text':'不存在的车','kind':'reference','source_ids':['m1']}]},'name_unresolved'),
    ({'filters':[{'field':'energy_type','op':'eq','value':v,'source_ids':['h0']} for v in ('electric','hybrid')]},'conflicting_filter:energy_type'),
])
def test_critical_uncertainty_is_clarification_not_partial_recommendation(changes,reason):
    pack,_,receipt=partial_year_pack(changes=changes)
    assert receipt['evaluation_state']=='clarification_required'
    assert receipt['match_count'] is None
    assert receipt['known_condition_match_count']==(6 if reason=='uncertainty_scope_unresolved' else None)
    assert receipt['recommendations']==[] and reason in receipt['clarification_reasons']
    assert not cq.aggregate_ready(receipt)


@pytest.mark.parametrize('text,error',[
    ('我按新能源类型查了目前这6台在售车源，暂时没有匹配到的车。年份需要核实。','inventory_absence_not_verified'),
    ('目前没有新能源车。','inventory_absence_not_verified'),
    ('符合全部条件的有6台。','unverified_reply_numeric_fact'),
    ('符合条件最低价是5.2万。','unverified_reply_numeric_fact'),
    ('我查了目前这5台在售车源，合成车1报价5.2万，年份待核实。','reply_scanned_count_mismatch'),
    ('先给您两台：合成车1报价5.2万。年份待核实。','reply_display_count_mismatch'),
])
def test_partial_scope_does_not_authorize_false_zero_totals_prices_or_counts(text,error):
    pack,_,_=partial_year_pack()
    result=validate_reply_public_facts({'reply_segments':[text]},pack)
    assert error in result['errors'],result


@pytest.mark.parametrize('text',[
    '我查了目前这6台在售车源，合成车1报价5.2万，年份还需核实。',
    '先给您两台：合成车1报价5.2万，合成车2报价5.6万。年份还需核实。',
    '先给您两台在售车源：合成车1报价5.2万，合成车2报价5.6万。年份还需核实。',
])
def test_partial_scope_stock_scan_and_display_counts_keep_distinct_roles(text):
    pack,_,_=partial_year_pack()
    result=validate_reply_public_facts({'answer_mode':'ask_clarifying_question','reply_segments':[text]},pack)
    assert result['ok'],result
    assert cq.query_plan_errors({'facts_claimed':result['facts_claimed']},pack)==[]


def test_legacy_false_zero_is_sanitized_without_rewriting_history():
    _,_,receipt=partial_year_pack()
    legacy=deepcopy(receipt)
    for key in ('evaluation_version','evaluation_state','clarification_reasons','known_condition_match_count','known_condition_scope','pending_conditions','known_conditions_evaluated','known_condition_facts'):
        legacy.pop(key,None)
    legacy.update(match_count=0,price_min=0,recommendations=[])
    original=deepcopy(legacy)
    view=cq.receipt_view(json.loads(json.dumps(legacy)))
    assert view['match_count'] is None and view['price_min'] is None
    assert view['evaluation_state']=='clarification_required'
    assert legacy==original and not cq.aggregate_ready(legacy)
    assert cq.query_plan_errors({'facts_claimed':[{'source_level':'catalog_query','fact_type':'catalog_match_count','source_id':legacy['query_id'],'value':0}]},
                                {'catalog_protocol':{'receipt':legacy}})==['invalid_catalog_aggregate_source']


def test_partial_missing_fields_do_not_become_definite_zero():
    _,_,receipt=partial_year_pack(changes={'requirement_state':'clear','unsupported_requirements':[]},rows=[row('1',5.2),row('2',5.6,energy_type='electric')])
    assert receipt['evaluation_state']=='partial'
    assert receipt['match_count'] is None and receipt['known_condition_match_count']==1
    assert receipt['unknown_fields']=={'1':['energy_type']}
    assert [p['id'] for p in receipt['recommendations']]==['2']


@pytest.mark.parametrize('unsupported',[
    ['年份需核实；预算可能是首付而非总价'],
    ['年份较近；客户说的刚才那辆尚未确定，不要它'],
    ['年份近一点，但其他需求含糊，无法确定全部影响范围'],
])
def test_nonempty_model_unknown_description_never_grants_recommendation(unsupported):
    pack,_,receipt=partial_year_pack(changes={'unsupported_requirements':unsupported})
    assert receipt['evaluation_state']=='clarification_required'
    assert receipt['match_count'] is None and not receipt['recommendations']
    assert receipt['query']['budget']['state']=='not_mentioned'
    assert receipt['known_condition_facts']  # Materials are not recommendation permission.
    wrong={'answer_mode':'recommend_from_catalog','reply_segments':['合成车1报价5.2万，符合您的要求。']}
    assert 'catalog_clarification_required' in validate_reply_public_facts(wrong,pack)['errors']


def test_partial_scan_never_recovers_full_authority_on_reload():
    _,_,receipt=partial_year_pack(changes={'requirement_state':'clear','unsupported_requirements':[]})
    receipt['scan_complete']=False
    original=deepcopy(receipt)
    result=cq.receipt_view(json.loads(json.dumps(receipt)))
    assert result['evaluation_state']=='unavailable'
    assert result['match_count'] is None and result['known_condition_match_count'] is None
    assert not result['recommendations'] and not result['known_condition_facts']
    assert receipt==original


@pytest.mark.parametrize('scope,count,allowed',[
    ('新能源条件',6,True),('新能源条件',8,False),('全部条件',6,False),
    ('燃油条件',6,False),('已核实的条件',6,True),
])
def test_known_count_requires_matching_scope_not_only_equal_value(scope,count,allowed):
    rows=[row(str(i),5+i/10,energy_type='electric') for i in range(1,7)]+[row('f1',3,energy_type='fuel'),row('f2',4,energy_type='fuel')]
    pack,_,receipt=partial_year_pack(rows=rows)
    assert receipt['scanned_count']==8 and receipt['known_condition_match_count']==6
    result=validate_reply_public_facts({'answer_mode':'ask_clarifying_question','reply_segments':[f'按{scope}查到{count}台车，年份还需核实。您希望哪一年之后的车？']},pack)
    assert result['ok']==allowed,result
    if allowed:assert cq.query_plan_errors({'facts_claimed':result['facts_claimed']},pack)==[]


@pytest.mark.parametrize('text,allowed',[
    ('按预算8万以内的新能源条件查到6台车，年份尚未核实。',True),
    ('按预算6万以内的新能源条件查到6台车，年份尚未核实。',False),
    ('按新能源条件查到6台车，年份尚未核实。',False),
])
def test_known_count_does_not_drop_budget_scope(text,allowed):
    pack,_,_=partial_year_pack(changes={'budget':{'state':'specified','mode':'upper','value':8,'source_ids':['m1']}})
    result=validate_reply_public_facts({'answer_mode':'ask_clarifying_question','reply_segments':[text]},pack)
    assert result['ok']==allowed,result


@pytest.mark.parametrize('text,allowed', [
    ('您希望是2022年以后，还是2023年以后的？', True),
    ('您说的年份近一点，是指近三年还是近五年？', True),
    ('您倾向哪档：2020年以后，或者2022年以后？', True),
    ('举个例子，您偏向2019年以后的吗？', True),
    ('您倾向2022年以后的？合成车1是2023年上牌的，您喜欢吗？', False),
    ('这辆车的上牌年份是2023年，您希望是2022年以后吗？', False),
])
def test_years_in_a_preference_do_not_authorize_an_actual_registration_year(text, allowed):
    pack, _, _ = partial_year_pack()
    plan = {'answer_mode':'ask_clarifying_question','reply_segments':[text]}
    assert validate_reply_public_facts(plan, pack)['ok'] is allowed


@pytest.mark.parametrize('last_type,claim,valid', [
    ('hybrid','有纯电车',False), ('hybrid','有新能源车',True),
    ('hybrid','有纯电车，您想看看吗？',False), ('hybrid','没有纯电车。',True),
    ('electric','没有纯电车。',False), (None,'全店没有纯电车。',False),
    ('electric','有纯电车',True), (None,'有纯电车吗？',True),
])
def test_claim_scope_uses_all_matching_rows_not_only_displayed_examples(last_type, claim, valid):
    rows = [row(str(i), i + 1, energy_type='hybrid') for i in range(10)]
    rows[-1]['data']['additional_details']['energy_type'] = last_type
    pack, _, receipt = partial_year_pack(rows=rows)
    assert len(receipt['known_condition_facts']) < 10
    assert receipt['known_energy_counts']['electric'] == int(last_type == 'electric')
    assert validate_reply_public_facts({'answer_mode':'ask_clarifying_question','reply_segments':[claim]},pack)['ok'] == valid


@pytest.mark.parametrize('declared', [None,'price_asc','budget_distance'])
def test_frozen_customer_order_survives_the_semantic_supplement(declared):
    pack, prep, inp, runtime = prepared('总价8万以内，便宜的先推荐。', history=['只看纯电车'])
    assert prep['route'] == 'semantic'
    raw = {'operation':'search','requirement_state':'clear','budget':{'state':'specified','mode':'upper','value':8,'source_ids':['m1']},
           'filters':[{'field':'energy_type','op':'eq','value':'electric','source_ids':['h0']}],
           'name_queries':[], 'reference_ids':[], 'exclude_ids':[], 'source_ids':['h0','m1'],
           'unsupported_requirements':[],'interpretation':'纯电，总价不超过八万'}
    if declared:
        raw['sort'] = {'value':declared,'source_ids':['m1']}
    query = cq.normalize_model_query(raw,cq.message_sources(pack),contract=cq.MODEL_QUERY_CONTRACT)
    assert query['raw_model_query'] == raw
    assert (query['sort'],query['sort_origin'],query['sort_source_ids']) == ('price_asc','customer_explicit',['m1'])


@pytest.mark.parametrize('latest,expected,source', [
    ('只看纯电车。', 'price_asc', 'h0'),
    ('不用便宜的先推荐了，只看纯电车。', 'budget_distance', None),
    ('只看纯电车，价格从高到低。', 'price_desc', 'm1'),
    ('只看纯电车，挑最接近预算的。', 'budget_distance', 'm1'),
])
def test_order_is_inherited_until_the_customer_replaces_or_withdraws_it(latest,expected,source):
    pack, _, _, _ = prepared(latest, history=['总价8万以内，便宜的先推荐。'])
    raw={'operation':'search','requirement_state':'clear','budget':{'state':'specified','mode':'upper','value':8,'source_ids':['h0']},
         'filters':[{'field':'energy_type','op':'eq','value':'electric','source_ids':['m1']}],
         'name_queries':[], 'reference_ids':[], 'exclude_ids':[], 'source_ids':['h0','m1'],
         'unsupported_requirements':[],'interpretation':'新能源筛选保留历史总价上限'}
    q=cq.normalize_model_query(raw,cq.message_sources(pack),contract=cq.MODEL_QUERY_CONTRACT)
    assert q['sort']==expected and q['sort_source_ids']==([source] if source else [])
    assert q['sort_origin']==('customer_explicit' if source else 'program_default')
    assert q['raw_model_query']==raw


@pytest.mark.parametrize('answer,expected', [
    ('合成车4报价6.4万元。',True),
    ('合成车1报价5.2万元。',False),
    ('合成车4报价5.2万元。',False),
    ('合成车4适合城市通勤。',False),
])
def test_named_price_uses_referenced_vehicle_even_outside_the_display_page(answer,expected):
    pack,_,_,runtime=prepared('请介绍合成车4，先只看这一辆，价格多少？')
    raw={'operation':'search','requirement_state':'clear','budget':{'state':'not_mentioned','source_ids':[]},
         'filters':[],'reference_ids':[],'exclude_ids':[],'source_ids':['m1'],'unsupported_requirements':[],
         'name_queries':[{'kind':'reference','text':'合成车1','source_ids':['m1']}],'interpretation':'查询客户点名车辆'}
    query=cq.normalize_model_query(raw,cq.message_sources(pack),contract=cq.MODEL_QUERY_CONTRACT)
    receipt=cq.execute_query(query,runtime,scope={'conversation_id':'c1','current_message_ids':['m1']},limit=1,sources=cq.message_sources(pack))
    cq.attach_receipt(pack,receipt)
    assert pack['catalog_protocol']['receipt']['reply_target']['vehicle_ids']==['4']
    assert [p['id'] for p in receipt['recommendations']]==['1']
    result=validate_reply_public_facts({'answer_mode':'direct_answer','reply_segments':[answer]},pack)
    assert result['ok']==expected,result


@pytest.mark.parametrize('text,allowed', [
    ('新能源这边我查到6台现车，上牌年份还未核实。',True),
    ('新能源这边我查到2台现车，上牌年份还未核实。',False),
    ('符合近年份的新能源我查到6台现车。',False),
    ('白色纯电我查到6台现车，上牌年份还需核实。',False),
    ('新能源这边我查到6台现车，价格从5.2万到7.2万，上牌年份仍待核实。',True),
    ('新能源这边我查到6台现车，价格从7.2万到5.2万，上牌年份仍待核实。',False),
])
def test_scoped_stock_totals_and_range_endpoints(text,allowed):
    pack,_,_=partial_year_pack()
    check=validate_reply_public_facts({'answer_mode':'ask_clarifying_question','reply_segments':[text]},pack)
    assert check['ok']==allowed,check


@pytest.mark.parametrize('text,allowed', [
    ('首付2万不等于车辆总价，月供还需要核实。您大概想买什么总价的车？',True),
    ('您的首付3万，您大概想买什么总价的车？',False),
    ('首付2万，月供1500是您的期望吗？',True),
    ('门店规定首付2万就能办。',False),
])
def test_customer_payment_source_is_not_a_finance_policy(text,allowed):
    pack,_,_,_=prepared('首付2万，月供1500左右，能介绍下吗？')
    from test_catalog_query_entry import fixtures
    plan=fixtures.base_plan();plan.update(answer_mode='ask_clarifying_question',reply_segments=[text],
                                         facts_claimed=[],evidence_used={})
    result=cq.prepared_review_route(pack,plan)
    assert (result.get('ok') is True)==allowed,result


@pytest.mark.parametrize('car_ids,question,declared,expected', [
    (['4'], '刚才那辆不要了。', [], ['4']),
    (['4','5'], '刚才那辆不要了。', [], None),
    ([], '刚才那辆不要了。', [], None),
    (['4'], '刚才那辆不要排除。', ['4'], 'revoked'),
])
def test_previous_vehicle_exclusion_depends_on_current_messages_and_unique_sent_history(car_ids,question,declared,expected):
    sources = [{'id':'one','sender':'customer','content':question},
               {'id':'two','sender':'customer','content':'8万以内。'}]
    raw = {'operation':'search','requirement_state':'clear',
           'budget':{'state':'specified','mode':'upper','value':8,'source_ids':['two']},
           'filters':[], 'name_queries':[], 'reference_ids':[], 'exclude_ids':declared,
           'source_ids':['one','two'], 'unsupported_requirements':[], 'interpretation':'看上一轮和预算'}
    known = {key:[{'kind':'confirmed_native_reply', 'source_id':'prior'}] for key in car_ids}
    args = dict(known_ids=known, contract=cq.MODEL_QUERY_CONTRACT, current_ids=['one','two'])
    if expected == 'revoked':
        with pytest.raises(cq.CatalogQueryError, match='previous_vehicle_exclusion_revoked'):
            cq.normalize_model_query(raw, sources, **args)
        return
    normalized = cq.normalize_model_query(raw, sources, **args)
    assert normalized['exclude_ids'] == (expected or [])
    assert bool(normalized.get('unresolved_previous_exclusion')) == (expected is None)


@pytest.mark.parametrize('scope,count,description,allowed', [
    ('那辆先放一边，8万以内我这边看到', 5, '适合城市通勤。', True),
    ('那辆先放一边，8万以内还剩', 5, '适合城市通勤。', True),
    ('那辆先放一边，8万以内还剩', 6, '适合城市通勤。', False),
    ('那辆先放一边，8万以内余下', '五', '适合城市通勤。', True),
    ('那辆先放一边，8万以内余下', '六', '适合城市通勤。', False),
    ('那辆先放一边，8万以内仅剩', '五', '适合城市通勤。', True),
    ('那辆先放一边，8万以内尚余', '六', '适合城市通勤。', False),
    ('那辆先放一边，8万以内还剩', '五', '', False),
    ('那辆先放一边，8万以内我这边看到', 3, '适合城市通勤。', False),
    ('那辆先放一边，8万以内我这边看到', 5, '', False),
    ('那辆先放一边，8万以内我这边看到', 5, '不适合通勤。', False),
    ('那辆先放一边，全店只有', 5, '适合城市通勤。', False),
    ('那辆先放一边，全店一共有', 6, '适合城市通勤。', True),
    ('算上刚才那辆，8万以内我这边看到', 5, '适合城市通勤。', False),
])
def test_count_uses_all_members_of_the_stated_stock_scope(scope, count, description, allowed):
    customer = '刚才那辆不要了，8万以内看看别的。'
    pack = evidence(customer)
    rows = [row(str(i), 4+i/2, energy_type='electric', customer_description='适合城市通勤。')
            for i in range(1,7)]
    # This car is outside the three displayed recommendations but still in
    # the counted set. Its public description must support the stated use.
    rows[0]['data']['additional_details']['customer_description'] = description
    for item in rows:
        item['source'] = {'type':'chejin_backend'}
    raw = {'operation':'search','requirement_state':'clear','source_ids':['m1'],
           'budget':{'state':'specified','mode':'upper','value':8,'source_ids':['m1']},
           'name_queries':[], 'filters':[], 'exclude_ids':['4'], 'reference_ids':[],
           'unsupported_requirements':[], 'interpretation':'已确认排除上一辆'}
    links = {'4':[{'source_id':'previous','conversation_id':'c1','kind':'confirmed_native_reply'}]}
    q = cq.normalize_model_query(raw,cq.message_sources(pack),contract=cq.MODEL_QUERY_CONTRACT,known_ids=links)
    receipt = cq.execute_query(q,Runtime(rows),scope={'conversation_id':'c1','current_message_ids':['m1']},limit=3)
    cq.attach_receipt(pack,receipt)
    assert len(receipt['recommendations']) == 3 and receipt['known_condition_match_count'] == 5
    intro = f'{scope}{count}台纯电通勤车。'
    names = '，'.join(f"{p['name']}报价{p['price']}万" for p in receipt['recommendations'])
    result = validate_reply_public_facts({'answer_mode':'recommend_from_catalog','reply_segments':[intro,names]},pack)
    assert result['ok'] is allowed, result


@pytest.mark.parametrize('first,allowed', [
    ('现在有6台纯电，价格5.2到7.2万。',True),
    ('现在有3台纯电，价格5.2到7.2万。',False),
    ('现在有6台纯电，价格4.2到7.2万。',False),
    ('现在有6台纯电，价格5.2到7.8万。',False),
    ('现在有六台纯电，价格五点二到七点二万。',True),
    ('现在有六台纯电，价格四点二到七点二万。',False),
    ('现在有六台纯电，价格五点二到七点八万。',False),
    ('现在有6台符合年份的纯电，价格5.2到7.2万。',False),
])
def test_known_inventory_count_and_shared_unit_range(first,allowed):
    pack,_,_=partial_year_pack()
    plan={'answer_mode':'ask_clarifying_question','reply_segments':[first,'年份还需核实，您希望哪年以后？']}
    result=validate_reply_public_facts(plan,pack)
    assert result['ok'] is allowed,result
