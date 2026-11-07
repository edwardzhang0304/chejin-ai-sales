"""Actual Hook batch -> Brain/query/reviewer/Guard -> native permit/history/ACK.

Synthetic model transport and Hook history; real SQLite, APIs and production
normalizers. No customer data, external model, physical WeChat or production DB.
"""
from copy import deepcopy
import json
from datetime import timedelta
import sys
import pytest
from sqlalchemy import select
import test_catalog_brain_joined as joined
import test_hook_native_send_http as native
from test_hook_business_http import clean
from test_hook_history_business_http import bound, coverage, upload
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.c3 import MessageBatch, ReplyAction, HandoffEvent
from app.models.task import Task
from app.models.vehicle import KnowledgeItem
from app.services import c3_service
from app.services.c3_recovery import recover_due_message_batches_once
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from apps.wechat_ai_customer_service import llm_config
from apps.wechat_ai_customer_service.workflows import customer_service_brain as brain
reviewer=sys.modules[brain.review_brain_reply_semantics.__module__]


@pytest.fixture
def actual_brain(monkeypatch):
    joined.api._create_listed_vehicle()
    monkeypatch.setattr(get_settings(),'c3_omniauto_root',str(joined.root/'worker-client/omniauto-rpa'))
    state={'scenario':'normal','invocations':[], 'receipts':[], 'wire':[], 'reviews':[], 'drafts':[]}
    class Runtime:
        def __init__(self):
            with SessionLocal() as db:self.tenant_id=db.scalar(select(KnowledgeItem.tenant_id).where(KnowledgeItem.category_id=='products'))
        def list_items(self,kind):
            with SessionLocal() as db:return [v.payload for v in db.scalars(select(KnowledgeItem).where(KnowledgeItem.category_id==kind))]
    original=joined.cq.attach_receipt
    def attach(pack,receipt):
        state['receipt']=receipt;state['receipts'].append(receipt);return original(pack,receipt)
    monkeypatch.setattr(joined.cq,'attach_receipt',attach)
    monkeypatch.setattr(joined.ev,'KnowledgeRuntime',Runtime)
    monkeypatch.setattr(joined.legacy_ev,'build_evidence_pack',lambda *a,**kw:{'evidence':{'products':[]},'safety':{'allowed_auto_reply':True,'must_handoff':False,'reasons':[]}})
    monkeypatch.setattr(brain,'resolve_llm_api_key',lambda **kw:'synthetic')
    monkeypatch.setattr(reviewer,'resolve_llm_api_key',lambda **kw:'synthetic')
    monkeypatch.setattr(llm_config,'resolve_llm_fallback_settings',lambda **kw:
        {'enabled':True,'provider':'openai','model':'synthetic-brain','api_key':'synthetic','base_url':'https://fallback.synthetic.invalid'}
        if state['scenario']=='failover' else {'enabled':False})
    cfg=joined.fixtures.base_config(joined.fixtures.base_plan())
    cfg['customer_service_brain'].update(mode='brain_first',provider='openai',api_key='synthetic',model='synthetic-brain',
        semantic_reviewer_model='synthetic-reviewer',semantic_reviewer_cache_enabled=False,semantic_reviewer_force=True)
    state['brain_config'] = cfg['customer_service_brain']
    def transport(**kw):
        state['wire'].append(kw);mode=state['scenario']
        if kw['model']=='synthetic-reviewer':
            if mode!='legacy':assert 'sort_origin' in kw['messages'][1]['content']
            request=json.loads(kw['messages'][1]['content'])['review_request']
            state['reviews'].append(request)
            result=joined.review('query_summary')
            if mode not in {'legacy','case_year'}:result=joined.review('vehicle_facts')
            if mode=='case_year':result=joined.review('non_inventory')
            if mode=='case_budget_timeout' and len(state['invocations'])==1:
                if len(state['reviews'])==1:
                    result=joined.review('vehicle_facts',verdict='repair',semantic_errors=['合成首次推荐缺少名称事实声明'],repair_instruction='补齐本稿使用的三个名称来源，不改价格或候选')
                else:return {'ok':False,'status':0,'error_kind':'transport','error':'synthetic_review_timeout','wall_timeout':True}

            if mode=='review_timeout':return {'ok':False,'status':0,'error_kind':'transport','error':'synthetic_timeout','wall_timeout':True}
        elif not state.get('receipt'):
            state['query_calls']=state.get('query_calls',0)+1
            if mode in {'recover','failover'} and state['query_calls']==1:
                return {'ok':False,'status':503,'error_kind':'transport','error':'synthetic_unavailable'}
            mid=state['invocations'][-1]['batch'][-1]['id']
            q=joined.query(source_ids=[mid]);q['budget'].update(source_ids=[mid],value=15)
            if mode!='legacy':q.pop('version');q.pop('sort');q['budget'].pop('unit')
            if mode.startswith('case_budget'):
                q['budget']={'state':'specified','mode':'range','lower':5,'upper':8,'source_ids':[mid]}
                q['interpretation']='客户问5—8万元预算内的车'
            if mode in {'case_energy','case_year'}:
                q['budget']={'state':'not_mentioned','source_ids':[]}
                if mode=='case_energy':state['energy_source_id']=mid
                energy_source=state.get('energy_source_id',mid)
                q['filters']=[{'field':'energy_type','op':'eq','value':'新能源纯电','source_ids':[energy_source]}]
                q['source_ids']=list(dict.fromkeys([energy_source,mid]))
                q['interpretation']='延续新能源需求，当前希望年份更近；不能把未支持条件当已筛选' if mode=='case_year' else '客户明确询问新能源车'
            if mode=='case_year':
                q['unsupported_requirements']=['客户希望年份近一点；现协议未支持年份筛选']
                raw=json.dumps({'catalog_query':q},ensure_ascii=False)
                if state['query_calls']==1:raw=raw.replace('"catalog_query":','"catalog_query"',1)
                return {'ok':True,'status':200,'response_text':raw}
            if mode=='explicit':q['sort']={'value':'price_asc','source_ids':[mid]}
            if mode=='unit_conflict':q['budget']['unit']='元'
            raw=json.dumps({'catalog_query':q},ensure_ascii=False)
            if mode=='recover' and state['query_calls']==2:
                q['source_ids']=[mid]*90
                raw=json.dumps({'catalog_query':q}).replace('"catalog_query":','"catalog_query"',1)
            return {'ok':True,'status':200,'response_text':raw,'usage':{'prompt_tokens':100,'completion_tokens':50}}
        else:
            r=state['receipt'];result=joined.summary_plan(r);result['facts_claimed'][0]['value']=r['match_count']
            result['reply_segments']=[f"按当前已明确的条件，符合条件的在售车辆有{r['match_count']}辆。"]
            # The shared real inbound fixture asks to see a BMW, then gives a
            # budget. A bare count no longer satisfies that request. Preserve
            # all retry/accounting/native recovery assertions with a valid
            # concrete answer; the explicit legacy fixture keeps its old form.
            if mode not in {'legacy','case_year'}:
                cars=r['recommendations']
                result['answer_mode']='recommend_from_catalog'
                result['evidence_used']={'product_ids':[c['id'] for c in cars]}
                result['facts_claimed']=[{'fact_type':'price','value':f"{c['price']}万",'source_level':'product_master','source_id':c['id']} for c in cars]
                # Six prices plus three selected names exercise the original nine-claim boundary.
                if not (mode=='case_budget_timeout' and len(state['invocations'])==1 and not state['reviews']):
                    result['facts_claimed'] += [{'fact_type':'product_name','value':c['name'],'source_level':'product_master','source_id':c['id']} for c in cars[:3]]
                named=[f"{c['name']}报价{c['price']}万" for c in cars[:3]]
                remaining=[f"另有一辆报价{c['price']}万" for c in cars[3:]]
                result['reply_segments']=['，'.join(named+remaining)+'。']
            elif mode=='case_year':
                result['answer_mode']='ask_clarifying_question';result['evidence_used']={};result['facts_claimed']=[]
                result['reply_segments']=['年份这一项目前还没核实，不能直接说已经筛到更近年份的车。您希望哪一年之后的新能源车？']
            if mode=='review_timeout':
                result['reply_segments'].append('预约看车请等门店确认排期后再到店。')
            if mode=='wrong_order':
                assert len(cars) >= 2
                result['reply_segments']=['，'.join(f"{c['name']}报价{c['price']}万" for c in reversed(cars))+'。']
            state['drafts'].append(deepcopy(result))
        return {'ok':True,'status':200,'response_text':json.dumps(result,ensure_ascii=False),'usage':{'prompt_tokens':100,'completion_tokens':50}}
    monkeypatch.setattr(llm_config,'call_llm_request_once_with_wall_timeout',transport)
    class Adapter(RealOmniAutoAIEngineAdapter):
        def _load_config(self):return deepcopy(cfg)
        def _run_brain_isolated(self,*,config,invocation,timeout_seconds):
            request=json.loads(json.dumps({'config':config,'invocation':invocation}))
            state['invocations'].append(request['invocation']);state.pop('receipt',None);state['query_calls']=0
            return json.loads(json.dumps(self._load_brain()(config=request['config'],**request['invocation'])))
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',Adapter)
    return state


def created(monkeypatch):
    w,t,p,b=bound();value=coverage(b);r,_=upload(w,p,b,value,monkeypatch)
    assert r.status_code==200,r.text
    bid=r.json()['data']['message_batch']['batch_id']
    return w,p,b,bid


def generate(bid):
    with SessionLocal() as db:
        claim=c3_service.claim_message_batch_generation(db,batch_id=bid,native_client_admitted=True);db.commit()
        assert claim['run'],claim
        value=c3_service.generate_for_batch(db,batch_id=bid,expected_generation_attempt=claim['attempt'],native_client_admitted=True);db.commit();return value


@pytest.mark.parametrize('scenario',['normal','explicit','recover','failover','unit_conflict','review_timeout','wrong_order','legacy'])
def test_contract_freeze_and_recovery_reach_persisted_result(monkeypatch,actual_brain,scenario):
    state=actual_brain;state['scenario']=scenario
    if scenario=='explicit':
        # Explicit model metadata needs an actual customer preference as its
        # source; the shared fixture only states a budget.
        original_coverage=coverage
        def sorted_coverage(binding):
            value=original_coverage(binding)
            for scan in value['scans']:
                scan['databases'][0]['pages'][0][-1]['StrContent'] += '，按价格从低到高给我看。'
            return value
        monkeypatch.setattr(sys.modules[__name__],'coverage',sorted_coverage)
    if scenario=='wrong_order':
        from test_program_preparation_pipeline import listed
        listed('第二辆排序测试车', 10.0)
    w,p,b,bid=created(monkeypatch)
    with SessionLocal() as db:
        batch=db.get(MessageBatch,bid)
        assert batch.ai_request_snapshot['model_query_contract']=='vehicle_need_v2'
        if scenario=='legacy':
            # Deliberate old persisted record fixture, not a new program-produced field.
            batch.ai_request_snapshot={k:v for k,v in batch.ai_request_snapshot.items() if k!='model_query_contract'};db.commit()
    result=generate(bid)
    with SessionLocal() as db:
        batch=db.get(MessageBatch,bid);snap=batch.ai_response_snapshot
        key=batch.ai_request_snapshot.get('model_query_contract')
        assert key==(None if scenario=='legacy' else 'vehicle_need_v2')
        assert state['invocations'][-1]['target_state'].get('model_query_contract')==key
        model=snap['raw_payload']['omniauto_brain_result']
        accounting=model['model_call_accounting']
        assert accounting['http_attempt_count']==len(state['wire'])
        assert accounting['logical_call_count']==len(state['wire'])-int(scenario=='failover')
        assert accounting['identity']['batch_id']==bid
        # A blocked draft may still suggest send_reply; only C3's decision is final.
        assert 'final_action' not in accounting
        assert accounting['plan_recommended_action']==(model.get('brain_plan') or {}).get('recommended_action')
        assert accounting['model_query_contract']==(key or 'vehicle_query_v1')
        assert len({x['call_id'] for x in accounting['logical_calls']})==accounting['logical_call_count']
        assert 'synthetic' not in json.dumps(accounting['effective_settings'])
        totals=snap['generation_accounting_totals'];assert totals['recorded_attempt_count']==1
        assert totals['logical_call_count']==accounting['logical_call_count'] and totals['call_counts_complete']
        actions=list(db.scalars(select(ReplyAction)))
    if scenario in {'unit_conflict','review_timeout','wrong_order'}:
        assert result['decision']=='retry_later' and not actions
        assert accounting['failure_category']=={'unit_conflict':'query_contract','review_timeout':'service_unavailable','wrong_order':'content_review'}[scenario]
        if scenario=='unit_conflict':assert not state['receipts']
        if scenario=='review_timeout':assert not any('repair' in x['stage'] for x in accounting['logical_calls'])
    else:
        assert result['decision']=='send_reply' and len(actions)==1
        assert accounting['failure_category']=='success'
        q=state['receipts'][-1]['query']
        if scenario!='legacy':assert q['sort_origin']==('customer_explicit' if scenario=='explicit' else 'program_default')
        if scenario in {'recover','failover'}:assert accounting['completion_outcome']=='recovered'
        if scenario=='normal':assert accounting['completion_outcome']=='first_pass'
        if scenario=='recover':
            assert accounting['http_attempt_count']==4 and not state['reviews']
            assert '503' in json.dumps(model['catalog_stages']['understanding'])
            assert any(x['failure_category']=='service_unavailable' for x in accounting['http_attempts'])


@pytest.mark.parametrize('marker',[None,'vehicle_need_v3',[],True])
def test_unknown_explicit_frozen_contract_never_calls_model(monkeypatch,actual_brain,marker):
    _,_,_,bid=created(monkeypatch)
    with SessionLocal() as db:
        batch=db.get(MessageBatch,bid);batch.ai_request_snapshot={'model_query_contract':marker};db.commit()
    result=generate(bid)
    assert result['decision']=='retry_later' and not actual_brain['wire']
    assert result['error_code']=='AI_ENGINE_CATALOG_QUERY_INVALID'


@pytest.mark.parametrize('kind',['unique','missing','ambiguous','timeout_but_observed'])
def test_real_query_and_review_to_native_settlement(monkeypatch,actual_brain,kind):
    # Keep every original native send/unknown/duplicate assertion unchanged.
    native.test_one_native_history_match_settles_original_reply_without_resend(kind,monkeypatch)
    assert actual_brain['receipts'][-1]['query']['model_query_contract']=='vehicle_need_v2'


@pytest.mark.parametrize('recover_success',[False,True])
def test_durable_outer_recovery_preserves_contract_and_accumulates_all_attempts(monkeypatch,actual_brain,recover_success):
    actual_brain['scenario']='review_timeout'
    w,_,_,bid=created(monkeypatch)
    result=generate(bid);assert result['decision']=='retry_later'
    first_count=len(actual_brain['wire'])
    with SessionLocal() as db:
        batch=db.get(MessageBatch,bid)
        due=batch.generated_at+timedelta(seconds=get_settings().c3_batch_retry_delay_seconds+1)
        assert batch.generation_attempt_count==1
        assert batch.ai_request_snapshot['model_query_contract']=='vehicle_need_v2'
    if recover_success:actual_brain['scenario']='normal'
    monkeypatch.setattr(c3_service,'utcnow',lambda:due)
    # Hook requires the admitted Worker's current poll; the generic scheduler
    # intentionally cannot revive a paused/unadmitted native client.
    recover_due_message_batches_once()
    with SessionLocal() as db:assert db.get(MessageBatch,bid).generation_attempt_count==1
    headers={'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':w['instance']}
    for _ in range(2):
        polled=joined.api.client.get(f"/api/workers/{w['id']}/wechat/message-batches/{bid}",headers=headers)
        assert polled.status_code==200,polled.text
    with SessionLocal() as db:
        batch=db.get(MessageBatch,bid);snap=batch.ai_response_snapshot
        assert batch.generation_attempt_count==2
        assert batch.ai_request_snapshot['model_query_contract']=='vehicle_need_v2'
        assert snap['generation_accounting_totals']['recorded_attempt_count']==2
        assert snap['generation_accounting_totals']['logical_call_count']==len(actual_brain['wire'])>first_count
        assert snap['generation_accounting_totals']['accounted_attempt_ranges']==[[1,2]]
        assert len(snap['generation_attempt_history'])==2
        assert len(list(db.scalars(select(ReplyAction))))==int(recover_success)
        assert len(list(db.scalars(select(HandoffEvent))))==int(not recover_success)
        totals=deepcopy(snap['generation_accounting_totals'])
    recover_due_message_batches_once()
    with SessionLocal() as db:
        assert db.get(MessageBatch,bid).ai_response_snapshot['generation_accounting_totals']==totals


def test_real_v2_brain_permission_still_cancels_stale_unsent_without_handoff(monkeypatch,actual_brain):
    native.test_native_context_change_before_any_trigger_cancels_without_handoff(monkeypatch)
    assert actual_brain['receipts'][-1]['query']['model_query_contract']=='vehicle_need_v2'
