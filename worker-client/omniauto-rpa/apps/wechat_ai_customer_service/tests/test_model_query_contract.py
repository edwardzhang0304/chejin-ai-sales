"""v2 adapts only fixed metadata/default order; v1 remains an explicit contract."""
from copy import deepcopy
import pytest
from test_catalog_query import cq, query, Runtime, row

SOURCES = [{'id': 'm1', 'content': '总预算4万，首付1万，优先便宜的'}]


def need(**changes):
    value = query(); value.pop('version'); value.pop('sort'); value['budget'].pop('unit')
    value.update(changes)
    return value


def normalize(value):
    return cq.normalize_model_query(value, SOURCES, contract=cq.MODEL_QUERY_CONTRACT)


@pytest.mark.parametrize('budget,target,sort', [
    ({'state':'specified','mode':'upper','value':4,'source_ids':['m1']},4,'budget_distance'),
    ({'state':'specified','mode':'range','lower':2,'upper':4,'source_ids':['m1']},3,'budget_distance'),
    ({'state':'specified','mode':'around','value':4,'source_ids':['m1']},4,'budget_distance'),
    ({'state':'not_mentioned','source_ids':[]},None,'price_asc'),
    ({'state':'unlimited','source_ids':['m1']},None,'price_asc'),
    ({'state':'uncertain','source_ids':['m1']},None,'price_asc'),
])
def test_fixed_metadata_and_default_follow_validated_business_budget(budget,target,sort):
    raw=need(budget=budget);saved=deepcopy(raw);q=normalize(raw)
    assert raw==saved and q['raw_model_query']==raw
    assert q['version']==1 and q['budget']['unit']=='万元'
    assert q['sort']==sort and q['sort_origin']=='program_default'
    assert (q['budget_bounds']['target'] if q['budget_bounds'] else None)==target
    result=cq.execute_query(q,Runtime([row('a',2),row('b',3),row('c',4),row('d',5)]),scope={},limit=8)
    ids=[v['id'] for v in result['recommendations']]
    if budget['state']=='uncertain': assert not ids and result['requirement_uncertain']
    elif target is None: assert ids==['a','b','c','d']
    elif budget['mode']=='range': assert ids==['b','a','c']
    elif budget['mode']=='around':
        assert q['budget_bounds']['lower']==3 and q['budget_bounds']['upper']==5
        assert ids==['c','b','d']
    else: assert ids==['c','b','a']


@pytest.mark.parametrize('sort',['price_asc','price_desc','budget_distance'])
def test_explicit_preference_is_preserved_with_sources(sort):
    q=normalize(need(sort={'value':sort,'source_ids':['m1']}))
    assert q['sort']==sort and q['sort_origin']=='customer_explicit' and q['sort_source_ids']==['m1']


@pytest.mark.parametrize('sort',[None,{},'price_asc',[],{'value':'price_asc','source_ids':[]},
    {'value':'price_asc','source_ids':['invented']},{'value':['price_asc'],'source_ids':['m1']}])
def test_invalid_explicit_sort_never_becomes_default(sort):
    with pytest.raises(cq.CatalogQueryError):normalize(need(sort=sort))


@pytest.mark.parametrize('path,value', [('version',True),('version',2),('version','1'),('budget.unit','元'),('budget.unit',None)])
def test_conflicting_metadata_is_not_overwritten(path,value):
    raw=need()
    if path.startswith('budget.'):raw['budget']['unit']=value
    else:raw[path]=value
    with pytest.raises(cq.CatalogQueryError,match='metadata_conflict'):normalize(raw)


def test_redundant_matching_metadata_is_recorded_and_legacy_is_not_guessed():
    raw=need(version=1);raw['budget']['unit']='万元'
    assert normalize(raw)['redundant_metadata']==['version','budget.unit']
    old=query()
    assert cq.normalize_model_query(old,SOURCES)==cq.normalize_query(old,SOURCES)
    with pytest.raises(cq.CatalogQueryError):cq.normalize_model_query(need(),SOURCES)
    for contract in [None,'vehicle_need_v3',{},1]:
        with pytest.raises(cq.CatalogQueryError):cq.model_contract({'model_query_contract':contract})


@pytest.mark.parametrize('field',[k for k in cq.QUERY_REQUIRED_FIELDS if k not in {'version','sort'}])
def test_missing_business_fields_not_filled(field):
    raw=need();raw.pop(field)
    with pytest.raises(cq.CatalogQueryError):normalize(raw)


def test_prompt_and_error_template_share_contract_without_v1_metadata_requirement():
    prompt=cq.query_instruction(cq.MODEL_QUERY_CONTRACT)
    assert cq.query_format_instruction(cq.MODEL_QUERY_CONTRACT) in prompt
    assert 'version为整数1' not in prompt and '所有预算状态均须保留unit' not in prompt
    assert '万元' in prompt and '首付' in prompt and 'source_ids' in prompt
    assert cq.query_instruction()==cq.QUERY_INSTRUCTION


@pytest.mark.parametrize('profile',['normal','lean','routine_product_fast','low_authority_fast'])
def test_all_prompt_profiles_preserve_same_frozen_contract(profile):
    from test_brain_completion_integrity import brain
    inputs={'evidence':{'catalog_protocol':{'protocol':cq.PROTOCOL,'model_query_contract':cq.MODEL_QUERY_CONTRACT,
                                          'phase':'not_queried','sources':SOURCES,'known_ids':{}}}}
    pack=brain.build_brain_prompt_pack(settings={'prompt_profile':profile},brain_input=inputs)
    assert cq.query_format_instruction(cq.MODEL_QUERY_CONTRACT) in pack['system']
    assert 'sort即使没有预算也必须填写' not in pack['system']
    import json
    assert cq.MODEL_QUERY_CONTRACT in json.dumps(pack['user'])


@pytest.mark.parametrize('phase',['not_queried','queried'])
def test_actual_structure_repair_uses_phase_contract(monkeypatch,phase):
    import json
    from test_brain_completion_integrity import brain,response
    calls=[]
    monkeypatch.setattr(brain,'resolve_llm_api_key',lambda **kw:'synthetic')
    def transport(**kw):calls.append(kw);return response()
    monkeypatch.setattr(brain,'call_llm_request_with_failover',transport)
    raw='{"can_answer" true}'
    brain.maybe_repair_brain_json_structure(settings={'provider':'openai'},raw_text=raw,stage='test',
        brain_input={'evidence':{'catalog_protocol':{'phase':phase,'model_query_contract':cq.MODEL_QUERY_CONTRACT}}})
    assert len(calls)==1
    user=json.loads(calls[0]['messages'][1]['content'])
    assert user['model_query_contract']==cq.MODEL_QUERY_CONTRACT and user['raw_brain_output']==raw
    assert (cq.query_format_instruction(cq.MODEL_QUERY_CONTRACT) in user['schema'])==(phase=='not_queried')
