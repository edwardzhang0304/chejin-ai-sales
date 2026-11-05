"""R8 through actual producer/Provider/C3/native APIs; model/Hook are synthetic."""
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
from test_program_preparation_pipeline import listed


def configure_partial(monkeypatch,state,mutation='normal'):
    api=pipeline.joined.api
    with SessionLocal() as db:
        existing=[v.item_id for v in db.scalars(select(KnowledgeItem).where(KnowledgeItem.category_id=='products'))]
    for key in existing:
        response=api.client.post(f'/api/vehicles/{key}/unlist',headers=api.HEADERS)
        assert response.status_code==200,response.text
    for i,price in enumerate([5.2,5.6,6.0,6.4,6.8,7.2],1):listed(f'合成车{i}',price)
    def coverage(binding):
        result=original_coverage(binding)
        for scan in result['scans']:
            for row,text in zip(scan['databases'][0]['pages'][0],['有新能源吗','有年份近一点的吗']):row['StrContent']=text
        return result
    monkeypatch.setattr(pipeline,'coverage',coverage);monkeypatch.setattr(pipeline.native,'coverage',coverage)
    state['authority_blocks']=[]
    def transport(**kw):
        state['wire'].append(kw)
        if kw['model']=='synthetic-reviewer':
            request=json.loads(kw['messages'][1]['content'])['review_request'];state['reviews'].append(request)
            assert request['authority_evidence_summary']==state['authority_blocks'][-1]
            kind='query_summary' if mutation=='true_zero' else 'non_inventory' if mutation in {'normal','budget_uncertain','generic_uncertain'} else 'vehicle_facts'
            value=pipeline.joined.review(kind)
            if mutation=='false_year':value=pipeline.joined.review(kind,verdict='repair',semantic_errors=['年份未核实，不能说全部符合'],repair_instruction='具体车辆真实价格可保留，说明年份尚待核实')
        elif not state.get('receipt'):
            messages=state['invocations'][-1]['batch'];first,last=messages[0]['id'],messages[-1]['id']
            q={'operation':'search','requirement_state':'uncertain','budget':{'state':'not_mentioned','source_ids':[]},
               'filters':[{'field':'energy_type','op':'in','value':['hybrid','range_extended','electric'],'source_ids':[first]}],
               'name_queries':[],'reference_ids':[],'exclude_ids':[],'source_ids':[first,last],
               'unsupported_requirements':['车辆年份较近（车龄/上牌年份）需按实际车源核验'],
               'interpretation':'客户要新能源车，并希望年份近一些；车源库无年份筛选字段，先按新能源类型查全库，年份再据实际车源核验。'}
            if mutation=='budget_uncertain':q['budget']={'state':'uncertain','source_ids':[last]}
            if mutation=='generic_uncertain':q['unsupported_requirements']=[]
            if mutation=='true_zero':
                q.update(requirement_state='clear',unsupported_requirements=[]);q['filters'][0]['value']=['hybrid']
            value={'catalog_query':q}
            if mutation=='extra_envelope':value['type']='json_object'
        else:
            receipt=state['receipt'];user,_=json.JSONDecoder().raw_decode(kw['messages'][1]['content'])
            authority=user['brain_input']['content_basis'];state['authority_blocks'].append(deepcopy(authority))
            assert authority['catalog_protocol']['receipt']['match_count']==pipeline.joined.cq.receipt_view(receipt)['match_count']
            value=pipeline.joined.fixtures.base_plan();value.pop('facts_claimed',None);value.pop('evidence_used',None)
            text='年份还需按具体车源核实，先给您两台：'+'，'.join(f"{c['name']}报价{c['price']}万" for c in receipt.get('known_condition_facts',[])[:2])+'。'
            if mutation in {'scan_count','facts_clarification','false_year'}:value['answer_mode']='ask_clarifying_question';text+='您希望哪一年之后的车？'
            if mutation=='scan_count':text='我查了目前这6台在售车源。'+text
            if mutation=='false_zero':text='我按新能源类型查了目前这6台在售车源，暂时没有匹配到的车。年份还需核实。'
            if mutation=='false_year':text='这些车都符合您要的近年份。'+text.replace('年份还需按具体车源核实，','')
            if mutation=='false_total':text='符合全部条件的有6台，最低价5.2万。'+text
            if mutation=='wrong_count':text=text.replace('两台','三台')
            if mutation in {'normal','budget_uncertain','generic_uncertain'}:
                text='您希望哪一年之后的车呢？';value['answer_mode']='ask_clarifying_question'
            if mutation=='true_zero':text='按当前已明确的条件，暂无符合的车源。'
            value['reply_segments']=[text];state['drafts'].append(deepcopy(value))
        return {'ok':True,'status':200,'response_text':json.dumps(value,ensure_ascii=False)}
    monkeypatch.setattr(llm_config,'call_llm_request_once_with_wall_timeout',transport)


@pytest.mark.parametrize('mutation',['normal','facts_clarification','scan_count','budget_uncertain','generic_uncertain','true_zero'])
def test_receipt_scope_survives_provider_c3_native_permit_and_ack(monkeypatch,actual_brain,mutation):
    state=actual_brain;configure_partial(monkeypatch,state,mutation)
    pipeline.native.test_one_native_history_match_settles_original_reply_without_resend('unique',monkeypatch)
    receipt=state['receipts'][-1]
    assert receipt['scanned_count']==6 and receipt['match_count']==(0 if mutation=='true_zero' else None)
    with SessionLocal() as db:
        native=db.scalar(select(MessageBatch)).ai_response_snapshot['raw_payload']['omniauto_brain_result']
        persisted=native['catalog_protocol']['receipt']
        assert persisted['match_count']==receipt['match_count'] and persisted['evaluation_state']==receipt['evaluation_state']
        assert native['_catalog_clearance'] and native['reply_authority_snapshot']
        if mutation in {'facts_clarification','scan_count'}:
            assert persisted['known_condition_match_count']==6 and len(persisted['known_condition_facts'])==6
            assert persisted['recommendations']==[]
            assert len(native['brain_plan']['evidence_used']['product_ids'])==2
        else:assert not persisted['recommendations']


@pytest.mark.parametrize('mutation',['false_zero','false_year','false_total','wrong_count','extra_envelope'])
def test_incorrect_partial_claims_or_mixed_envelope_never_create_send_action(monkeypatch,actual_brain,mutation):
    state=actual_brain;configure_partial(monkeypatch,state,mutation)
    _,_,_,bid=pipeline.created(monkeypatch);result=pipeline.generate(bid)
    assert result['decision']=='retry_later',result
    with SessionLocal() as db:
        assert not list(db.scalars(select(ReplyAction)))
        native=db.get(MessageBatch,bid).ai_response_snapshot['raw_payload']['omniauto_brain_result']
        assert native['model_call_accounting']['failure_category']==('query_contract' if mutation=='extra_envelope' else 'content_review')
        assert not native.get('_catalog_clearance')
    if mutation=='extra_envelope':assert not state['receipts']


@pytest.mark.parametrize('kind',['missing','ambiguous','timeout_but_observed'])
def test_partial_candidates_preserve_unknown_result_and_repeat_ack_protection(monkeypatch,actual_brain,kind):
    configure_partial(monkeypatch,actual_brain)
    pipeline.native.test_one_native_history_match_settles_original_reply_without_resend(kind,monkeypatch)


def test_partial_candidates_preserve_new_customer_message_cancellation(monkeypatch,actual_brain):
    configure_partial(monkeypatch,actual_brain)
    pipeline.native.test_native_context_change_before_any_trigger_cancels_without_handoff(monkeypatch)


@pytest.mark.parametrize('change',['price','unlist','new_listing'])
def test_partial_context_inventory_change_during_review_cannot_send(monkeypatch,actual_brain,change):
    state=actual_brain;configure_partial(monkeypatch,state,'facts_clarification')
    # Inject the change after the content decision and before the real final
    # consumer. A routine draft no longer visits the model review transport.
    import apps.wechat_ai_customer_service.workflows.customer_service_brain as brain
    original=brain.guard_synthesized_reply
    changed=[]
    def consume(**kw):
        if not changed:
            changed.append(True)
            key=state['receipt']['known_condition_facts'][0]['id'];api=pipeline.joined.api
            if change=='new_listing':listed('后来上架的合成车',4.8)
            else:
                response=(api.client.put(f'/api/vehicles/{key}',json={'public_price':9.99},headers=api.HEADERS)
                          if change=='price' else api.client.post(f'/api/vehicles/{key}/unlist',headers=api.HEADERS))
                assert response.status_code==200,response.text
        return original(**kw)
    monkeypatch.setattr(brain,'guard_synthesized_reply',consume)
    _,_,_,bid=pipeline.created(monkeypatch);result=pipeline.generate(bid)
    assert changed and result['decision']!='send_reply',result
    with SessionLocal() as db:
        assert not list(db.scalars(select(ReplyAction)))
        assert db.get(MessageBatch,bid).error_code


@pytest.mark.parametrize('text,kind,allowed',[
    ('不是没有车，只是年份还需核实。您希望哪一年之后的新能源车？','non_inventory',True),
    ('并非没有车，但是年份还需核实。您希望哪一年之后的车？','non_inventory',True),
    ('不能说没有车，但是目前没有新能源车。您希望哪一年之后的车？','non_inventory',False),
    ('不是没有车但目前没有新能源车。您希望哪一年之后的车？','non_inventory',False),
    ('没有新能源车您要看看别的吗？','non_inventory',False),
    ('目前这8台在售车源的年份还需核实。您希望哪一年之后的车？','query_summary',True),
    ('目前这6台在售车源的年份还需核实。您希望哪一年之后的车？','query_summary',False),
    ('按新能源条件查到6台车，年份还需核实。您希望哪一年之后的车？','query_summary',True),
    ('按新能源条件查到8台车，年份还需核实。您希望哪一年之后的车？','query_summary',False),
    ('按已核实的条件查到6台车，年份还需核实。您希望哪一年之后的车？','query_summary',True),
    ('全部条件都符合的有6台车，年份还需核实。','query_summary',False),
    ('按新能源条件查到2台车，年份还需核实。','query_summary',False),
    ('先给您两台在售车源：合成车1报价5.2万，合成车2报价5.6万。年份待核实，您希望哪一年之后的车？','vehicle_facts',True),
    ('先给您六台在售车源：合成车1报价5.2万，合成车2报价5.6万。年份待核实，您希望哪一年之后的车？','vehicle_facts',False),
])
def test_stock_known_scope_and_display_have_independent_authority(monkeypatch,actual_brain,text,kind,allowed):
    state=actual_brain;configure_partial(monkeypatch,state,'normal')
    api=pipeline.joined.api
    for i in range(2):
        key=listed(f'合成燃油车{i}',3.0+i)
        result=api.client.put(f'/api/vehicles/{key}',json={'energy_type':'fuel'},headers=api.HEADERS)
        assert result.status_code==200,result.text
    original=llm_config.call_llm_request_once_with_wall_timeout
    def transport(**kw):
        response=original(**kw);value=json.loads(response['response_text'])
        if kw['model']=='synthetic-reviewer':value=pipeline.joined.review(kind)
        elif 'catalog_query' not in value:
            value.update(answer_mode='ask_clarifying_question',reply_segments=[text]);state['drafts'][-1]=value
        response['response_text']=json.dumps(value,ensure_ascii=False)
        return response
    monkeypatch.setattr(llm_config,'call_llm_request_once_with_wall_timeout',transport)
    if allowed:
        pipeline.native.test_one_native_history_match_settles_original_reply_without_resend('unique',monkeypatch)
    else:
        _,_,_,bid=pipeline.created(monkeypatch);result=pipeline.generate(bid)
        assert result['decision']=='retry_later',result
        with SessionLocal() as db:assert not list(db.scalars(select(ReplyAction)))
    receipt=state['receipts'][-1]
    assert receipt['scanned_count']==8 and receipt['known_condition_match_count']==6 and receipt['match_count'] is None


@pytest.mark.parametrize('segments,allowed',[
    (['目前没有车辆的上牌年份资料，您希望哪一年之后的新能源车？'],True),
    (['目前没有车款年份信息，您希望哪一年之后的新能源车？'],True),
    (['车款年份信息目前还没有，您希望哪一年之后的新能源车？'],True),
    (['目前没有车辆的年份资料。','您希望哪一年之后的新能源车？'],True),
    (['没有车辆的年份资料，但是目前没有新能源车。您希望哪一年之后的车？'],False),
    (['目前没有车款年份信息。','目前没有新能源车。您希望哪一年之后的车？'],False),
    (['目前没有新能源车辆，您希望哪一年之后的车？'],False),
    (['目前没有新能源车可供推荐，您希望哪一年之后的车？'],False),
])
def test_missing_vehicle_information_is_not_inventory_absence(monkeypatch,actual_brain,segments,allowed):
    state=actual_brain;configure_partial(monkeypatch,state,'normal')
    original=llm_config.call_llm_request_once_with_wall_timeout
    def transport(**kw):
        response=original(**kw);value=json.loads(response['response_text'])
        if kw['model']=='synthetic-reviewer':value=pipeline.joined.review('non_inventory')
        elif 'catalog_query' not in value:
            value.update(answer_mode='ask_clarifying_question',reply_segments=segments);state['drafts'][-1]=value
        response['response_text']=json.dumps(value,ensure_ascii=False);return response
    monkeypatch.setattr(llm_config,'call_llm_request_once_with_wall_timeout',transport)
    _,_,_,bid=pipeline.created(monkeypatch);result=pipeline.generate(bid)
    with SessionLocal() as db:assert bool(list(db.scalars(select(ReplyAction))))==allowed,result
