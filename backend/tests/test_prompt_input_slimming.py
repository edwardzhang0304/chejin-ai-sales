"""Prompt-only projection keeps facts, conditions, history and original audit intact."""
from copy import deepcopy
import pytest
import test_catalog_brain_joined  # runtime/fixture import paths
from test_program_catalog_preparation import prepared
from test_catalog_query import cq, row, query
from apps.wechat_ai_customer_service.workflows import customer_service_brain as brain
from apps.wechat_ai_customer_service.workflows import customer_service_quality_reviewer as reviewer
from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import reply_authority_snapshot


@pytest.mark.parametrize('case', ['budget','exterior','interior','registration','combined','zero','missing','unsupported'])
def test_full_query_semantics_and_public_facts_survive_all_prompt_stages(case):
    details = dict(exterior_color='白',interior_color='黑',first_registration='2022-02',customer_description='合成公开资料'*300)
    rows=[row('fit',3.8,**details),row('other',8,exterior_color='黑')]
    filters=[]
    for kind, field, value, op in [('exterior','exterior_color','白色','eq'),('interior','interior_color','黑色','eq'),('registration','first_registration','2021-01','gte')]:
        if case in (kind,'combined','zero','missing'):
            filters.append(dict(field=field,value=value,op=op,source_ids=['m1']))
    if case=='zero':rows=[rows[1]]
    if case=='missing': rows.append(row('unknown',3.7))
    pack,_,inp,rt=prepared('总预算四万元以内，首付一万元；希望白色、黑色内饰、2021年以后上牌。',rows=rows)
    q=query(filters=filters,unsupported_requirements=['无大修'] if case=='unsupported' else [])
    q.pop("version"); q.pop("sort"); q["budget"].pop("unit")
    sources=cq.message_sources(pack)
    r=cq.execute_query(cq.normalize_model_query(q,sources,contract=cq.MODEL_QUERY_CONTRACT),rt,scope={'conversation_id':'c1'},sources=sources)
    cq.attach_receipt(pack,r)
    inp['conversation']={'history_text':'客户：不要之前那辆，保留四万元总预算。','current_batch_text':pack['current_message']}
    frozen=deepcopy(reply_authority_snapshot(pack)); before=deepcopy(pack)
    generation=brain.build_brain_prompt_pack(settings={},brain_input=inp)['user']['brain_input']
    repair=brain.build_brain_repair_prompt_pack(settings={},brain_input=inp,plan={},quality={})['user']['brain_input']
    review=reviewer.build_quality_review_request(settings={},brain_input=inp,evidence_pack=pack,plan={},deterministic_quality={})
    for projection, basis in [(generation['catalog_protocol'],generation['content_basis']),
                              (repair['catalog_protocol'],repair['content_basis']),
                              (review['catalog_protocol'],review['authority_evidence_summary'])]:
        assert projection["sources"] == frozen["sources"]
        receipt=projection['receipt']
        for key in ('scan_complete','scanned_count','match_count','evaluation_state','pending_conditions',
                    'unsupported_requirements','known_condition_scope','reply_target'):
            assert receipt.get(key)==frozen['catalog_protocol']['receipt'].get(key),key
        accepted=frozen['catalog_protocol']['receipt']['query']
        for key in ('budget','filters','sort','exclude_ids','reference_ids','source_evidence','sort_origin','sort_source_ids'):
            assert receipt['query'].get(key)==accepted.get(key),key
        for key in ('recommendations','references','known_condition_facts'):
            assert receipt.get(key,[])==[p['id'] for p in frozen['catalog_protocol']['receipt'].get(key,[])]
        assert basis['product_master']['items']==frozen['product_master']['items']
        assert basis['formal_knowledge']==frozen['formal_knowledge']
        assert basis['formal_knowledge_sources']==frozen['formal_knowledge_sources']
        assert 'catalog_protocol' not in basis and 'catalog_selection' not in basis['product_master']
        assert 'request' not in receipt['query']
    assert review['catalog_protocol']['receipt']['query']['raw_model_query']==r['query']['raw_model_query']
    assert 'raw_model_query' not in generation['catalog_protocol']['receipt']['query']
    assert generation['conversation']['history_text']==inp['conversation']['history_text']
    assert generation['conversation']['current_batch_text']==pack['current_message']
    assert 'catalog_selection' in brain.estimate_prompt_pack(brain.build_brain_prompt_pack(settings={},brain_input=inp))
    # The existing reviewer adds its policy citation cache; it must not change
    # any original evidence or the frozen authority snapshot.
    assert {key: pack[key] for key in before} == before
    assert set(pack) - set(before) <= {"policy_reference_map"}
    assert reply_authority_snapshot(pack)==frozen


def test_understanding_retains_all_source_ids_and_ocr_projection_stays_legacy():
    pack,_,inp,_=prepared('首付四万，不是总预算；内饰要黑色。',history=['之前那辆白车不要了'])
    slim=brain.slim_brain_input_for_prompt(inp,settings={})
    assert slim['catalog_protocol']['sources']==cq.message_sources(pack)
    assert slim['catalog_protocol']['phase']=='not_queried'
    assert slim['content_basis']['product_master']['items']==[]
    legacy=deepcopy(inp);legacy['evidence'].pop('catalog_protocol')
    old=brain.slim_brain_input_for_prompt(legacy,settings={})
    assert 'audit_summary' in old and 'legacy_candidate_policy' in old
    assert 'reading_guide' not in old


def test_uncited_old_requirements_and_semantic_guidance_survive_repair():
    history = ['总预算四万元，白色；不要把首付当总价。', '合成历史内容。' * 200]
    pack, _, inp, rt = prepared('内饰改成黑色，其他条件不变。', history=history)
    raw = query()
    receipt = cq.execute_query(cq.normalize_query(raw, cq.message_sources(pack)), rt,
                               scope={'conversation_id': 'c1'}, sources=cq.message_sources(pack))
    cq.attach_receipt(pack, receipt)
    guidance = '客户后续明确修改才替换旧条件，未修改的条件继续保留。'
    inp['conversation'] = {'history_text': '\n'.join(history), 'semantic_instruction': guidance}
    for prompt in (brain.build_brain_prompt_pack(settings={}, brain_input=inp),
                   brain.build_brain_repair_prompt_pack(settings={}, brain_input=inp, plan={}, quality={})):
        assert guidance in prompt['system']
        projected = prompt['user']['brain_input']
        assert 'semantic_instruction' not in projected['conversation']
        assert projected['catalog_protocol']['sources'] == cq.message_sources(pack)
    review = reviewer.build_quality_review_request(settings={}, brain_input=inp, evidence_pack=pack,
                                                   plan={}, deterministic_quality={})
    assert review['catalog_protocol']['sources'][0]['content'] == history[0]
    assert review['catalog_protocol']['sources'] == cq.message_sources(pack)
