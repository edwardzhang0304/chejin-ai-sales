"""Complete inventory -> candidate selection -> final prompt, without live AI.

Only the storage boundary is seeded; ranking, merges, parser and prompt shaping
are real code. Synthetic stock is deliberately bigger than a UI page/top-k.
"""
from copy import deepcopy
from pathlib import Path
import socket
import sys

import pytest

APP = Path(__file__).resolve().parents[1]
for path in (APP.parents[1], APP, APP / 'workflows', APP / 'adapters'):
    sys.path.insert(0, str(path))
from apps.wechat_ai_customer_service.workflows import reply_evidence_builder as evidence
from apps.wechat_ai_customer_service.workflows.catalog_budget import parse_catalog_budget
from apps.wechat_ai_customer_service.workflows.customer_service_brain import build_brain_prompt_pack
from apps.wechat_ai_customer_service.workflows.customer_service_brain_contract import extract_quality_budget_upper


def vehicle(key, price, *, name=None, aliases=(), status='active', specs='燃油 自动挡 轿车'):
    return {'id': key, 'status': status, 'source': {'type': 'chejin_backend'},
            'data': {'name': name or key, 'price': price, 'inventory': 1,
                     'category': '二手车', 'specs': specs, 'aliases': list(aliases)}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('test must not access a live service')
    monkeypatch.setattr(socket.socket, 'connect', denied)


def catalog(monkeypatch, rows):
    calls = []
    class Runtime:
        def list_items(self, category):
            assert category == 'products'
            calls.append(category)
            return deepcopy(rows)
        def get_item(self, *args):
            raise AssertionError('context must use the same full snapshot')
    monkeypatch.setattr(evidence, 'KnowledgeRuntime', Runtime)
    return calls


def prompt(query, *, context=None, retrieval=None, profile='routine_product_fast', limit=4):
    packed = evidence.compact_knowledge_pack(query, {'evidence': {'products': retrieval or []}},
        max_rag_hits=0, max_rag_text_chars=100, max_catalog_candidates=8, context_override=context or {})
    final = build_brain_prompt_pack(settings={'prompt_profile': profile, 'max_prompt_product_items': limit},
                                   brain_input={'evidence': {'knowledge': packed}})
    return packed, final['user']['brain_input']['content_basis']['product_master'], final['system']


@pytest.mark.parametrize('query', ['那请问你们4万块钱左右，你们有什么车', '4万左右有啥车',
    '预算40000元以内有哪些', '三五万的代步车', '3万到5万的车', '四万元左右',
    '不要刚才那个了，改看4万元以内的车'])
@pytest.mark.parametrize('reverse', [False, True])
def test_full_inventory_budget_survives_retrieval_context_and_fast_prompt(monkeypatch, query, reverse):
    rows = [vehicle(f'expensive-{i}', 10+i) for i in range(33)]
    rows += [vehicle('fit-a', 3.58), vehicle('fit-b', 3.78), vehicle('fit-c', 3.98), vehicle('fit-d', 4.58)]
    calls = catalog(monkeypatch, list(reversed(rows)) if reverse else rows)
    stale = evidence.catalog_product_payload(rows[0])
    packed, final, _ = prompt(query, context={'last_product_id': rows[0]['id'],
        'recent_product_ids': [rows[0]['id']], 'last_customer_need_text': '预算20万'}, retrieval=[stale])
    assert calls == ['products']
    budget = parse_catalog_budget(query)
    expected = {v['id'] for v in rows if budget.contains(v['data']['price'])}
    assert {v['id'] for v in final['items']} == expected
    for item in packed['product_master']['items']:
        assert budget.contains(item['price'])
    coverage = final['catalog_selection']
    assert coverage['scan_complete'] and coverage['scanned_count'] == len(rows)
    assert coverage['budget_match_count'] == len(expected)
    assert coverage['prompt_ids'] == [v['id'] for v in final['items']]


@pytest.mark.parametrize('query,lower,upper,target', [
    ('4万块钱左右',3,5,4), ('4万元左右',3,5,4), ('四万块钱左右',3,5,4),
    ('三五万',3,5,4), ('3到5万',3,5,4), ('3万到5万',3,5,4), ('3-5万元',3,5,4),
    ('三到五万',3,5,4), ('三万到五万',3,5,4), ('30000到50000元',3,5,4),
    ('４万以内',0,4,4), ('40000元以内',0,4,4), ('最多4万左右',0,4,4),
    ('4w以下',0,4,4), ('4万以上',4,None,4), ('不低于4万元',4,None,4),
    ('原来10万，现在4万',0,4,4), ('4万以内，里程8万公里',0,4,4),
])
def test_budget_units_bounds_and_current_precedence(query, lower, upper, target):
    budget = parse_catalog_budget(query)
    assert budget is not None
    assert (budget.lower, budget.upper, budget.target) == (lower, upper, target)
    assert extract_quality_budget_upper(query, {'conversation': {'context': {'last_customer_need_text': '预算20万'}}}) == upper


@pytest.mark.parametrize('query', ['里程8万公里', '宝马320Li', '年款2020', '首付3万', '月供3000元', '预算不限'])
def test_non_price_numbers_do_not_create_budget(query):
    assert parse_catalog_budget(query) is None


def test_context_budget_applies_to_elliptical_followup(monkeypatch):
    catalog(monkeypatch, [vehicle('pricey', 18), vehicle('fit', 3.98)])
    _, final, _ = prompt('有什么车', context={'last_customer_need_text': '我想要三五万代步车'})
    assert [i['id'] for i in final['items']] == ['fit']
    assert final['catalog_selection']['budget_source'] == 'conversation_need'


def test_released_budget_does_not_inherit_old_cap(monkeypatch):
    catalog(monkeypatch, [vehicle('pricey', 18), vehicle('fit', 3.98)])
    _, final, _ = prompt('预算不限，你们有什么车', context={'last_customer_need_text': '预算4万以内'})
    assert {i['id'] for i in final['items']} == {'fit', 'pricey'}
    assert final['catalog_selection']['budget'] is None
    assert extract_quality_budget_upper('预算不限', {'conversation': {'context': {'last_customer_need_text': '预算4万以内'}}}) is None


@pytest.mark.parametrize('status', ['archived','draft','inactive'])
def test_unlisted_and_stale_retrieval_do_not_reenter_candidates(monkeypatch, status):
    stale = vehicle('retired', 3.5, status=status)
    catalog(monkeypatch, [stale, vehicle('fit',3.98)])
    _, final, _ = prompt('4万左右有什么车', retrieval=[evidence.catalog_product_payload(stale)],
                         context={'last_product_id': 'retired'})
    assert [i['id'] for i in final['items']] == ['fit']
    assert final['catalog_selection']['eligible_count'] == 1


@pytest.mark.parametrize('profile', ['normal','routine_product_fast','lean','low_authority_fast'])
def test_prompt_clipping_keeps_complete_search_counts(monkeypatch, profile):
    catalog(monkeypatch, [vehicle(f'fit-{i}',3+i/10) for i in range(15)])
    packed, final, system = prompt('4万块钱左右有什么车', profile=profile, limit=3)
    assert len(packed['product_master']['items']) == 8
    assert len(final['items']) == 3
    assert final['catalog_selection']['budget_match_count'] == 15
    assert final['catalog_selection']['truncated'] is True
    assert final['catalog_selection']['prompt_truncated'] is True
    assert '不得从展示的几辆车推断' in system


def test_failure_is_distinct_from_empty_inventory(monkeypatch):
    class Failed:
        def list_items(self, _): raise OSError('private connection string must not escape')
    monkeypatch.setattr(evidence,'KnowledgeRuntime',Failed)
    _, failed, _ = prompt('4万以内有什么车')
    assert failed['catalog_selection']['scan_complete'] is False
    assert failed['catalog_selection']['error_type'] == 'OSError'
    assert 'private' not in str(failed)
    catalog(monkeypatch, [])
    _, empty, _ = prompt('4万以内有什么车')
    assert empty['catalog_selection']['scan_complete'] is True
    assert empty['catalog_selection']['eligible_count'] == 0
    assert empty['catalog_selection']['budget_match_count'] == 0


def test_outside_budget_not_silently_recommended_on_zero_matches(monkeypatch):
    catalog(monkeypatch,[vehicle('pricey',12),vehicle('unknown',None)])
    _, final, _ = prompt('4万以内有什么车')
    assert final['items'] == []
    assert final['catalog_selection']['unknown_price_count'] == 1
    assert final['catalog_selection']['budget_match_count'] == 0


def test_named_vehicle_facts_survive_budget_for_comparison(monkeypatch):
    catalog(monkeypatch,[vehicle('named',12,name='测试星云SUV',aliases=['星云']),vehicle('fit',3.98)])
    _, final, _ = prompt('星云多少钱？4万预算有替代吗？')
    assert final['items'][0]['id'] == 'named'
    assert final['items'][0]['selection_role'] == 'referenced_vehicle_outside_budget'
    assert final['items'][1]['id'] == 'fit'


def test_relative_question_uses_current_snapshot_not_separate_lookup(monkeypatch):
    calls=catalog(monkeypatch,[vehicle('named',12),vehicle('fit',3.98)])
    _, final, _ = prompt('这台车多少钱', context={'recent_product_ids':['named']})
    assert final['items'][0]['id'] == 'named'
    assert calls == ['products']


@pytest.mark.parametrize('query,expected', [('你们最贵的车是哪款','expensive'),('你们最便宜的车是哪款','cheap'),('你们有什么车','cheap')])
def test_price_ordering_scans_every_row(monkeypatch, query, expected):
    catalog(monkeypatch,[vehicle('middle',8),vehicle('cheap',3),vehicle('expensive',15)])
    assert evidence.catalog_product_candidates(query,limit=1)[0]['id'] == expected


def test_preference_search_finds_late_electric_vehicle(monkeypatch):
    rows=[vehicle(f'fuel-{i}',4) for i in range(40)]
    rows.append(vehicle('ev',3.98,specs='纯电 新能源 两厢车',aliases=['纯电']))
    catalog(monkeypatch,rows)
    assert evidence.catalog_product_candidates('4万左右想要纯电的，有什么车',limit=1)[0]['id'] == 'ev'


def test_zero_limit_and_synthetic_ranking_tags_are_not_product_names(monkeypatch):
    catalog(monkeypatch,[vehicle('fit',4)])
    assert evidence.catalog_product_candidates('4万左右有什么车',limit=0) == []
    assert not evidence.is_explicit_catalog_candidate({'matched_aliases':['catalog_price_list'],'match_reason':'catalog_price_list_request'})


def test_brand_matches_cannot_displace_affordable_stock(monkeypatch):
    rows = [vehicle(f'expensive-{i}', 12+i, aliases=['测试品牌']) for i in range(30)]
    rows += [vehicle('fit', 3.98, aliases=['测试品牌'])]
    catalog(monkeypatch, rows)
    _, final, _ = prompt('测试品牌4万左右有哪些车')
    assert final['items'][0]['id'] == 'fit'
    assert final['items'][0]['selection_role'] == 'budget_match'
    assert final['catalog_selection']['budget_match_count'] == 1


def test_sql_catalog_scan_is_unpaged_tenant_scoped_and_reaches_final_prompt(monkeypatch, tmp_path):
    import json
    import sqlite3
    from types import SimpleNamespace
    from apps.wechat_ai_customer_service import product_master
    from apps.wechat_ai_customer_service.storage.postgres_store import PostgresJsonStore
    from apps.wechat_ai_customer_service.workflows.knowledge_runtime import KnowledgeRuntime

    # Execute the production inventory SELECT against an isolated SQL table;
    # only the driver/placeholder and JSON decoding boundary is adapted.
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE knowledge_items (tenant_id, layer, category_id, product_id, item_id, status, payload)')
    rows = [vehicle(f'a-{i:02}', 12+i) for i in range(35)] + [vehicle('z-fit', 3.98)]
    for tenant, item in [('test-tenant', v) for v in rows] + [
        ('other-tenant', vehicle('foreign-fit', 3.98)),
        ('test-tenant', vehicle('retired-fit', 3.98, status='archived')),
    ]:
        connection.execute('INSERT INTO knowledge_items VALUES (?,?,?,?,?,?,?)',
            (tenant, 'product_master', 'products', '', item['id'], item['status'], json.dumps(item)))

    class SqlDriver(PostgresJsonStore):
        def fetchall(self, query, params):
            return [{'payload': json.loads(row[0])} for row in connection.execute(query.replace('%s', '?'), params)]
    db = SqlDriver(SimpleNamespace(postgres_schema='main'), tenant_id='test-tenant')
    monkeypatch.setattr(product_master, 'postgres_store', lambda tenant: db)
    runtime = KnowledgeRuntime(root=tmp_path, tenant_id='test-tenant')
    monkeypatch.setattr(evidence, 'KnowledgeRuntime', lambda: runtime)
    try:
        _, final, _ = prompt('4万以内有什么车')
        assert [i['id'] for i in final['items']] == ['z-fit']
        assert final['catalog_selection']['scanned_count'] == 36
        assert final['catalog_selection']['budget_match_count'] == 1
    finally:
        connection.close()


def test_recorded_prompt_audit_matches_actual_truncated_input(monkeypatch):
    from apps.wechat_ai_customer_service.workflows.customer_service_brain import estimate_prompt_pack
    catalog(monkeypatch, [vehicle(f'fit-{i}', 3+i/10) for i in range(15)])
    packed, _, _ = prompt('4万左右有什么车')
    final = build_brain_prompt_pack(settings={'prompt_profile':'lean','max_prompt_product_items':2},
                                   brain_input={'evidence':{'knowledge':packed}})
    estimate = estimate_prompt_pack(final)
    selection = estimate['catalog_selection']
    assert selection['budget_match_count'] == 15
    assert selection['prompt_count'] == 2
    assert selection['prompt_ids'] == [v['id'] for v in final['user']['brain_input']['content_basis']['product_master']['items']]


@pytest.mark.parametrize('query', ['上面这些太贵了，换几个', '换几台便宜的', '不要刚才那些，看看别的'])
def test_rejected_old_candidates_do_not_displace_inherited_budget(monkeypatch, query):
    old = [vehicle(f'old-{i}', 15+i) for i in range(6)]
    catalog(monkeypatch, old + [vehicle('fit', 3.98)])
    _, final, _ = prompt(query, context={'last_customer_need_text':'预算4万以内',
                         'recent_product_ids':[v['id'] for v in old], 'last_product_id':old[0]['id']})
    assert final['items'][0]['id'] == 'fit'
    assert all(v['selection_role'] == 'budget_match' for v in final['items'])


@pytest.mark.parametrize('query', ['首付三到五万元', '月供3000到5000元', '首付最多4万元',
    '月供大概3000元', '不要十万元的车', '不考虑10万左右的车'])
def test_non_budget_ranges_and_negated_prices_are_not_total_budget(query):
    assert parse_catalog_budget(query) is None


def test_total_budget_survives_deposit_range_and_negative_price_mentions():
    assert parse_catalog_budget('预算10万，首付3到5万元').upper == 10
    assert parse_catalog_budget('预算4万，不要十万元的车').upper == 4


@pytest.mark.parametrize('profile', ['normal', 'routine_product_fast', 'lean', 'low_authority_fast'])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('query', ['预算4万，首付1到2万，有什么车',
    '预算4万，月供2000到3000元，有什么车',
    '原来预算不限，现在预算4万以内，有什么车',
    '预算4万，不要10万的车，有什么合适的',
    '上面这些太贵了，换几个合适的', '不要刚才的，按预算重新推荐', '大众有什么车'])
def test_review_cases_keep_full_inventory_matches_in_actual_prompt(monkeypatch, query, reverse, profile):
    old = [vehicle(f'expensive-{i}', 12+i, aliases=['大众']) for i in range(24)]
    rows = old + [vehicle('fit', 3.98, aliases=['大众'])]
    catalog(monkeypatch, list(reversed(rows)) if reverse else rows)
    context = {'last_customer_need_text':'预算4万以内', 'last_product_id':old[0]['id'],
               'recent_product_ids':[i['id'] for i in old[:4]]}
    _, final, _ = prompt(query, context=context, profile=profile)
    # Business expected value is explicit, not computed by the parser under test.
    assert final['items'][0]['id'] == 'fit'
    assert final['catalog_selection']['budget']['upper'] == 4
    assert final['catalog_selection']['budget_match_count'] == 1
    assert extract_quality_budget_upper(query, {'conversation':{'context':context}}) == 4


def test_explicit_budget_release_supersedes_previous_cap(monkeypatch):
    catalog(monkeypatch, [vehicle('pricey', 12), vehicle('fit',3.98)])
    _, final, _ = prompt('原来预算4万，现在预算不限，有什么车', context={'last_customer_need_text':'预算4万'})
    assert {i['id'] for i in final['items']} == {'pricey', 'fit'}
    assert final['catalog_selection']['budget'] is None
    assert extract_quality_budget_upper('原来预算4万，现在预算不限',{}) is None
