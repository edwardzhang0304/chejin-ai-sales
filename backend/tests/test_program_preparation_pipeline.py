"""API inventory -> inbound -> actual Brain/review -> native history settlement.

Boundaries: synthetic model HTTP, JSON-roundtrip provider process, local SQLite
runtime, synthetic Hook history. This is mechanism coverage, not AI quality or
Windows acceptance. All authority/clearance/permit fields come from production.
"""
import json
from copy import deepcopy
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import MessageBatch, ReplyAction
from app.models.vehicle import KnowledgeItem
import test_model_query_hook_pipeline as pipeline
from apps.wechat_ai_customer_service import llm_config
from test_model_query_hook_pipeline import actual_brain
from test_hook_business_http import clean
from test_hook_history_business_http import coverage as original_coverage


def listed(name,price):
    api=pipeline.joined.api
    r=api.client.post('/api/vehicles',headers=api.HEADERS,json={
        'display_name':name,'brand':'合成品牌','series':'国产新能源','public_price':price,
        'energy_type':'electric','customer_description':'合成公开描述'*60,'internal_notes':'PRIVATE_SENTINEL'})
    assert r.status_code==200,r.text
    vid=r.json()['data']['vehicle_code']
    r=api.client.post(f'/api/vehicles/{vid}/images',files={'files':('test.png',api.PNG_1X1,'image/png')},headers=api.HEADERS)
    assert r.status_code==200,r.text
    r=api.client.post(f'/api/vehicles/{vid}/list',headers=api.HEADERS)
    assert r.status_code==200,r.text
    return vid


def publish_policy(content):
    api=pipeline.joined.api
    r=api.client.post('/api/knowledge/items',json={'title':'合成正式说明','content':content},headers=api.HEADERS)
    assert r.status_code==200,r.text
    item=r.json()['data']
    preview=api.client.post('/api/knowledge/releases/preview',json={'operation':'create','item_id':item['id']},headers=api.HEADERS).json()['data']
    r=api.client.post('/api/knowledge/releases',json={'preview_id':preview['preview_id'],'content_digest':preview['content_digest']},headers=api.HEADERS)
    assert r.status_code==200,r.text


def configure(monkeypatch,state,case,mutation,prices_override=None):
    # The shared fixture has one unrelated listed high-price car. Preserve it:
    # the real query must filter it out, not a test-side pre-filter.
    if case=='F01':
        prices=[10.68+i for i in range(24)]+[3.58,3.78,3.98]
        messages=['我想买一台三五万的代步车','那请问你们4万块钱左右，你们有什么车']
    else:
        prices=prices_override or [5.2,5.6,6.0,6.4,6.8,7.2]
        messages=['您好','5—8万有什么车？']
    ids=[listed(f'合成车{i}',price) for i,price in enumerate(prices,1)]
    def coverage(binding):
        value=original_coverage(binding)
        for scan in value['scans']:
            for row,text in zip(scan['databases'][0]['pages'][0],messages):row['StrContent']=text
        return value
    monkeypatch.setattr(pipeline,'coverage',coverage)
    monkeypatch.setattr(pipeline.native,'coverage',coverage)
    state['authority_blocks']=[]
    def transport(**kw):
        state['wire'].append(kw)
        if (mutation=='generation_capacity' and kw['model']!='synthetic-reviewer') or (mutation=='review_capacity' and kw['model']=='synthetic-reviewer'):
            return {'ok':False,'status':400,'error_kind':'input_capacity_exceeded','error':'context_length_exceeded'}
        if kw['model']=='synthetic-reviewer':
            request=json.loads(kw['messages'][1]['content'])['review_request']
            state['reviews'].append(request)
            assert request['authority_evidence_summary']==state['authority_blocks'][-1]
            if mutation=='timeout':return {'ok':False,'status':0,'error_kind':'transport','error':'synthetic_timeout','wall_timeout':True}
            value=pipeline.joined.review('vehicle_facts')
        else:
            # A direct case has a real query receipt before its FIRST model call.
            receipt=state['receipt']
            assert receipt['scan_complete']
            user,_=json.JSONDecoder().raw_decode(kw['messages'][1]['content'])
            block=user['brain_input']['content_basis']
            state['authority_blocks'].append(deepcopy(block))
            assert 'PRIVATE_SENTINEL' not in json.dumps(block,ensure_ascii=False)
            cars=receipt['recommendations'][:2]
            value=pipeline.joined.fixtures.base_plan()
            value.update(answer_mode='recommend_from_catalog',reply_segments=[
                '，'.join(f"{car['name']}报价{4.6 if mutation=='wrong_second_price' and index==1 else car['price']}万元" for index,car in enumerate(cars))+'。'])
            value.pop('facts_claimed',None);value.pop('evidence_used',None)
            if mutation=='degraded':value['reply_segments']=['有合适的车，您更喜欢什么类型呢？']
            state['drafts'].append(deepcopy(value))
        return {'ok':True,'status':200,'response_text':json.dumps(value,ensure_ascii=False),
                'usage':{'prompt_tokens':100,'completion_tokens':50}}
    monkeypatch.setattr(llm_config,'call_llm_request_once_with_wall_timeout',transport)
    return ids


@pytest.mark.parametrize('case',['F01','M02'])
def test_direct_program_preparation_reaches_actual_permit_history_and_ack(monkeypatch,actual_brain,case):
    state=actual_brain
    configure(monkeypatch,state,case,'normal')
    pipeline.native.test_one_native_history_match_settles_original_reply_without_resend('unique',monkeypatch)
    assert len(state['wire'])==2  # one generated draft + one semantic review
    receipt=state['receipts'][-1]
    expected=[3.98,3.78,3.58] if case=='F01' else [6.4,6.8,6.0,7.2,5.6,5.2]
    assert [p['price'] for p in receipt['recommendations']]==expected
    assert receipt['scanned_count']==(28 if case=='F01' else 7)
    with SessionLocal() as db:
        batch=db.scalar(select(MessageBatch));native=batch.ai_response_snapshot['raw_payload']['omniauto_brain_result']
        assert native['catalog_preparation']['route']=='direct'
        assert native['candidate_history'] and native['review_history']
        assert len(native['model_response_history'])==1
        assert native['model_response_history'][0]['response']['response_text']==json.dumps(state['drafts'][0],ensure_ascii=False)
        assert 'api_key' not in native['model_response_history'][0]['request']
        assert native['brain_plan']['facts_claimed']
        assert len(native['brain_plan']['evidence_used']['product_ids'])==2
        assert len(native['model_call_accounting']['http_attempts'])==2


@pytest.mark.parametrize('mutation',['wrong_second_price','degraded','timeout'])
def test_incorrect_text_or_unavailable_review_never_creates_send_action(monkeypatch,actual_brain,mutation):
    state=actual_brain
    configure(monkeypatch,state,'M02',mutation)
    _,_,_,bid=pipeline.created(monkeypatch)
    result=pipeline.generate(bid)
    assert result['decision']=='retry_later',result
    with SessionLocal() as db:
        assert not list(db.scalars(select(ReplyAction)))
        batch=db.get(MessageBatch,bid)
        native=batch.ai_response_snapshot['raw_payload']['omniauto_brain_result']
        assert native['candidate_history']
        if mutation=='timeout':
            assert len(state['wire'])==2
            assert native['no_visible_reply']['class']=='review_timeout'
        else:
            assert not state['reviews']  # independent numeric/goal gate, before semantic pass can mask it


@pytest.mark.parametrize('mutation',['generation_capacity','review_capacity'])
def test_explicit_capacity_failure_uses_single_handoff_without_retry(monkeypatch,actual_brain,mutation):
    state=actual_brain;configure(monkeypatch,state,'M02',mutation)
    _,_,_,bid=pipeline.created(monkeypatch);result=pipeline.generate(bid)
    assert result['decision']=='handoff' and result['error_code']=='AI_ENGINE_INPUT_CAPACITY_EXCEEDED',result
    assert len(state['wire'])==(1 if mutation=='generation_capacity' else 2)
    with SessionLocal() as db:
        batch=db.get(MessageBatch,bid)
        assert not batch.retryable and not list(db.scalars(select(ReplyAction)))
        n=batch.ai_response_snapshot['raw_payload']['omniauto_brain_result']
        assert n['no_visible_reply']['failure_category']=='input_capacity_exceeded'


@pytest.mark.parametrize('mutation',['text','plan','authority','clearance','protocol','policy_map'])
def test_serialized_approved_result_cannot_change_content_or_drop_review(monkeypatch,actual_brain,mutation):
    state=actual_brain;configure(monkeypatch,state,'M02','normal')
    original=pipeline.c3_service.get_ai_engine_adapter
    class Changed(original):
        def _run_brain_isolated(self,**kwargs):
            result=super()._run_brain_isolated(**kwargs)
            assert result['adoptable'],result
            if mutation=='text':result['reply_text']+='合成车4报价99万。'
            elif mutation=='plan':result['brain_plan']['facts_claimed']=[]
            elif mutation=='authority':result['reply_authority_snapshot']['content']['product_master']['items']=[]
            elif mutation=='protocol':result.pop('catalog_protocol')
            elif mutation=='policy_map':result['policy_reference_map']['statements']={}
            else:result.pop('_catalog_clearance')
            return result
    monkeypatch.setattr(pipeline.c3_service,'get_ai_engine_adapter',Changed)
    _,_,_,bid=pipeline.created(monkeypatch);result=pipeline.generate(bid)
    assert result['error_code']=='AI_ENGINE_REVIEWED_CONTENT_CHANGED',result
    with SessionLocal() as db:assert not list(db.scalars(select(ReplyAction)))


@pytest.mark.parametrize('mutation',['normal','unknown_knowledge','unknown_statement','omitted','wrong_fee','extra_promise','mixed_false_promise'])
def test_published_policy_paraphrase_through_model_parse_review_and_native_chain(monkeypatch,actual_brain,mutation):
    state=actual_brain
    content='预约看车需提前一天联系，需要核实车源状态和门店排期，确认后再答复客户。'
    publish_policy(content)
    def coverage(binding):
        value=original_coverage(binding)
        for scan in value['scans']:
            for row in scan['databases'][0]['pages'][0]:row['StrContent']='预约看车需要提前多久联系？'
        return value
    monkeypatch.setattr(pipeline,'coverage',coverage);monkeypatch.setattr(pipeline.native,'coverage',coverage)
    text='预约看车请提前1天联系，需要先核实车源状态和门店排期，确认后回复您。'
    if mutation=='wrong_fee':text=text.replace('1天','2天')
    if mutation=='extra_promise':text+='贷款保证通过。'
    if mutation=='mixed_false_promise':text=text.rstrip('。')+'，贷款保证通过。'
    def transport(**kw):
        state['wire'].append(kw)
        if kw['model']=='synthetic-reviewer':
            request=json.loads(kw['messages'][1]['content'])['review_request'];state['reviews'].append(request)
            mapping=request['policy_reference_map']
            assert mapping['knowledge']
            assert next(iter(mapping['knowledge'].values()))['content']==content
            refs={'S1':['K1']}
            if len(mapping['statements'])>1 and mutation!='extra_promise':
                refs={key:['K1'] for key in mapping['statements']}
            if mutation=='unknown_knowledge':refs={'S1':['K99']}
            if mutation=='unknown_statement':refs={'S99':['K1']}
            value=pipeline.joined.review('non_inventory',policy_refs=refs)
            if mutation=='omitted':value.pop('policy_refs')
        else:
            user,_=json.JSONDecoder().raw_decode(kw['messages'][1]['content'])
            assert user['brain_input']['content_basis']['formal_knowledge_sources']
            value=pipeline.joined.fixtures.base_plan()
            value.update(answer_mode='direct_answer',understanding={'user_intent':'预约看车'},reply_segments=[text])
            value.pop('facts_claimed',None);value.pop('evidence_used',None)
        return {'ok':True,'status':200,'response_text':json.dumps(value,ensure_ascii=False)}
    monkeypatch.setattr(llm_config,'call_llm_request_once_with_wall_timeout',transport)
    if mutation=='normal':
        pipeline.native.test_one_native_history_match_settles_original_reply_without_resend('unique',monkeypatch)
        with SessionLocal() as db:
            batch=db.scalar(select(MessageBatch));n=batch.ai_response_snapshot['raw_payload']['omniauto_brain_result']
            assert n['brain_plan']['policy_refs'] and n['brain_plan']['evidence_used']['formal_knowledge_ids']
            assert all(f['fact_type']=='policy' for f in n['brain_plan']['facts_claimed'])
        assert len(state['wire'])==2
    else:
        _,_,_,bid=pipeline.created(monkeypatch);result=pipeline.generate(bid)
        assert result['decision']=='retry_later',result
        with SessionLocal() as db:assert not list(db.scalars(select(ReplyAction)))
        if mutation in {'unknown_knowledge','unknown_statement','omitted'}:
            # A malformed review is a technical failure, not a reason to
            # rewrite an otherwise valid customer-facing answer.
            assert sum(w['model']!='synthetic-reviewer' for w in state['wire'])==1


def test_year_followup_retains_energy_and_repairs_format_before_final_clarification(monkeypatch,actual_brain):
    state=actual_brain;state['scenario']='case_year'
    def coverage(binding):
        value=original_coverage(binding)
        for scan in value['scans']:
            for row,text in zip(scan['databases'][0]['pages'][0],['有新能源吗','有年份近一点的吗']):row['StrContent']=text
        return value
    monkeypatch.setattr(pipeline,'coverage',coverage);monkeypatch.setattr(pipeline.native,'coverage',coverage)
    pipeline.native.test_one_native_history_match_settles_original_reply_without_resend('unique',monkeypatch)
    receipt=state['receipts'][-1]
    assert receipt['query']['filters'][0]['field']=='energy_type'
    assert receipt['unsupported_requirements'] and len(state['wire'])>=3
    with SessionLocal() as db:
        n=db.scalar(select(MessageBatch)).ai_response_snapshot['raw_payload']['omniauto_brain_result']
        assert '哪一年之后的新能源车' in n['reply_text']
        assert n['catalog_stages']['understanding']['json_structure_repaired']
        assert n['candidate_history'] and n['review_history']


@pytest.mark.parametrize('separator',['comma','sentence','segment','same_vehicle_clause'])
@pytest.mark.parametrize('source,tail,allowed',[
    ('预约服务费为0.2万元，最终以门店确认为准。','预约服务费0.2万元，最终以门店确认为准。',True),
    ('预约服务费为6.8万元，最终以门店确认为准。','预约服务费6.8万元，最终以门店确认为准。',True),
    ('预约服务费为0.2万元，最终以门店确认为准。','实际售价0.2万元。',False),
    ('预约服务费为0.2万元，最终以门店确认为准。','报价0.2万元。',False),
    (None,'预约服务费6.8万元，最终以门店确认为准。',False),
    ('预约定金为0.2万元，最终以门店确认为准。','预约服务费0.2万元，最终以门店确认为准。',False),
])
def test_vehicle_and_policy_amount_roles_reach_same_final_guard(monkeypatch,actual_brain,separator,source,tail,allowed):
    state=actual_brain
    configure(monkeypatch,state,'M02','normal')
    if source:publish_policy(source)
    underlying=llm_config.call_llm_request_once_with_wall_timeout
    def transport(**kw):
        response=underlying(**kw)
        value=json.loads(response['response_text'])
        if kw['model']=='synthetic-reviewer':
            request=json.loads(kw['messages'][1]['content'])['review_request']
            mapping=request['policy_reference_map']
            # Deliberately wrong semantic pass/citation for the negative cases:
            # the actual fact owner must still reject a price/fee substitution.
            value['policy_refs']={sid:[next(iter(mapping['knowledge']))]
                for sid,s in mapping['statements'].items()
                if mapping['knowledge'] and ('服务费' in s['text'] or '售价' in s['text'] or s['text'].startswith('报价'))}
        else:
            first=value['reply_segments'][0]
            if separator=='same_vehicle_clause':
                value['reply_segments']=[first.rstrip('。')+'，'+state['receipt']['recommendations'][1]['name']+tail]
            else:
                value['reply_segments']=([first,tail] if separator=='segment' else
                    [first.rstrip('。')+('，' if separator=='comma' else '。')+tail])
        response['response_text']=json.dumps(value,ensure_ascii=False)
        return response
    monkeypatch.setattr(llm_config,'call_llm_request_once_with_wall_timeout',transport)
    if allowed:
        pipeline.native.test_one_native_history_match_settles_original_reply_without_resend('unique',monkeypatch)
        assert len(state['wire'])==2
    else:
        _,_,_,bid=pipeline.created(monkeypatch);result=pipeline.generate(bid)
        assert result['decision']=='retry_later',result
        with SessionLocal() as db:
            assert not list(db.scalars(select(ReplyAction)))
            n=db.get(MessageBatch,bid).ai_response_snapshot['raw_payload']['omniauto_brain_result']
            assert not n.get('_catalog_clearance')


@pytest.mark.parametrize('question,answer,allowed',[
    ('五到八万左右有什么合适的车？','summary',False),
    ('五到八万左右有什么合适的？','summary',False),
    ('五到八万左右有什么合适的？','recommend',True),
    ('五到八万左右有哪些可以看看？','summary',False),
    ('五到八万左右有哪些可以看看？','recommend',True),
    ('5—8万总共多少台？','summary',True),
    ('5—8万有几辆，推荐两台。','summary',False),
    ('5—8万有几辆，推荐两台。','recommend',True),
    ('5—8万只告诉我数量，不用推荐。','summary',True),
    ('5—8万的情况呢？','summary',False),
])
def test_answer_goal_survives_semantic_query_and_complete_consumers(monkeypatch,actual_brain,question,answer,allowed):
    state=actual_brain
    configure(monkeypatch,state,'M02','normal')
    def coverage(binding):
        value=original_coverage(binding)
        for scan in value['scans']:
            for row,text in zip(scan['databases'][0]['pages'][0],['您好',question]):row['StrContent']=text
        return value
    monkeypatch.setattr(pipeline,'coverage',coverage);monkeypatch.setattr(pipeline.native,'coverage',coverage)
    def transport(**kw):
        state['wire'].append(kw)
        if kw['model']=='synthetic-reviewer':
            value=pipeline.joined.review('query_summary' if answer=='summary' else 'vehicle_facts')
        elif not state.get('receipt'):
            mid=state['invocations'][-1]['batch'][-1]['id']
            q=pipeline.joined.query(source_ids=[mid]);q.pop('version',None);q.pop('sort',None)
            q['budget']={'state':'specified','mode':'range','lower':5,'upper':8,'source_ids':[mid]}
            q['interpretation']='客户询问五到八万元的车辆'
            value={'catalog_query':q}
        else:
            value=pipeline.joined.fixtures.base_plan()
            text=f"符合条件的车有{state['receipt']['match_count']}辆。"
            if answer=='recommend':
                text+='，'.join(f"{p['name']}报价{p['price']}万元" for p in state['receipt']['recommendations'][:2])+'。'
            value.update(answer_mode='direct_answer',understanding={'user_intent':'查车'},reply_segments=[text])
            value.pop('facts_claimed',None);value.pop('evidence_used',None)
        return {'ok':True,'status':200,'response_text':json.dumps(value,ensure_ascii=False)}
    monkeypatch.setattr(llm_config,'call_llm_request_once_with_wall_timeout',transport)
    if allowed:
        pipeline.native.test_one_native_history_match_settles_original_reply_without_resend('unique',monkeypatch)
    else:
        _,_,_,bid=pipeline.created(monkeypatch);result=pipeline.generate(bid)
        assert result['decision']=='retry_later',result
        with SessionLocal() as db:assert not list(db.scalars(select(ReplyAction)))


@pytest.mark.parametrize('segments,allowed',[
    (['可以看看6.4万元的合成车4和6.8万元的合成车5。'],True),
    (['合成车4报价6.4万元，合成车5报价6.8万元。'],True),
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
    (['合成车4报价6.4万元。','合成车5报价6.8万元。'],True),
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
def test_numeric_entity_assignment_through_review_guard_and_native_permit(monkeypatch,actual_brain,segments,allowed):
    state=actual_brain
    configure(monkeypatch,state,'M02','normal')
    underlying=llm_config.call_llm_request_once_with_wall_timeout
    def transport(**kw):
        response=underlying(**kw)
        if kw['model']!='synthetic-reviewer':
            value=json.loads(response['response_text']);value['reply_segments']=segments
            response['response_text']=json.dumps(value,ensure_ascii=False)
        return response
    monkeypatch.setattr(llm_config,'call_llm_request_once_with_wall_timeout',transport)
    if allowed:
        pipeline.native.test_one_native_history_match_settles_original_reply_without_resend('unique',monkeypatch)
        assert len(state['wire'])==2
    else:
        _,_,_,bid=pipeline.created(monkeypatch);result=pipeline.generate(bid)
        assert result['decision']=='retry_later',result
        with SessionLocal() as db:
            assert not list(db.scalars(select(ReplyAction)))
            n=db.get(MessageBatch,bid).ai_response_snapshot['raw_payload']['omniauto_brain_result']
            assert n['model_call_accounting']['failure_category']=='content_review',n
            assert n['no_visible_reply']['class']=='fact_validation_failed',n
        assert not state['reviews']


@pytest.mark.parametrize('text,allowed',[
    ('合成车4和合成车5都报价6.4万元。',True),
    ('合成车4和合成车5分别报价6.4万元、6.4万元。',True),
    ('6.4万元的合成车4和6.4万元的合成车5。',True),
    ('6.4万元的合成车4和6.8万元的合成车5。',False),
])
def test_equal_price_entities_keep_independent_authority_in_final_pipeline(monkeypatch,actual_brain,text,allowed):
    state=actual_brain
    configure(monkeypatch,state,'M02','normal',prices_override=[5.2,5.6,6.0,6.4,6.4,7.2])
    underlying=llm_config.call_llm_request_once_with_wall_timeout
    def transport(**kw):
        response=underlying(**kw)
        if kw['model']!='synthetic-reviewer':
            value=json.loads(response['response_text'])
            a,b=state['receipt']['recommendations'][:2]
            assert a['price']==b['price']==6.4
            # Equal-price tie order uses actual stable IDs, not display-name order.
            body=text.replace('合成车4','{first}').replace('合成车5','{second}').format(first=a['name'],second=b['name'])
            value['reply_segments']=[body]
            response['response_text']=json.dumps(value,ensure_ascii=False)
        return response
    monkeypatch.setattr(llm_config,'call_llm_request_once_with_wall_timeout',transport)
    if allowed:
        pipeline.native.test_one_native_history_match_settles_original_reply_without_resend('unique',monkeypatch)
        assert len(state['wire'])==2
    else:
        _,_,_,bid=pipeline.created(monkeypatch);result=pipeline.generate(bid)
        assert result['decision']=='retry_later',result
        with SessionLocal() as db:assert not list(db.scalars(select(ReplyAction)))


@pytest.mark.parametrize('segments,allowed',[
    (['5到8万这个区间，先给您两台：合成车4报价6.4万，合成车5报价6.8万。'],True),
    (['您五到八万这个预算，合成车4报价6.4万。','合成车5报价6.8万。','这两台可以比较。'],True),
    (['50000到80000元这个范围，合成车4报价6.4万元。'],True),
    (['先给您推荐两台：','合成车4报价6.4万，合成车5报价6.8万。'],True),
    (['先给您推荐三台：','合成车4报价6.4万，合成车5报价6.8万。'],False),
    (['5到8万的服务费，合成车4报价6.4万。'],False),
    (['8万以上可以看合成车4报价6.4万。'],False),
    (['预算8万以内可以看合成车4报价6.4万。'],False),
    (['5到8万这个区间，合成车4报价8万。'],False),
    (['先看两台：合成车4报价6.4万，合成车4报价6.4万。'],False),
    (['再给您补两台：合成车4报价6.4万，合成车5报价6.8万，合成车3报价6万。'],False),
])
def test_budget_and_display_scope_through_actual_downstream(monkeypatch,actual_brain,segments,allowed):
    test_numeric_entity_assignment_through_review_guard_and_native_permit(monkeypatch,actual_brain,segments,allowed)
