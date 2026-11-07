"""Current feedback through real Brain/Provider/C3 and native history settlement.

Only model HTTP responses and WeChat history are synthetic. No external model,
production data, or manual approval/state writes. Counterfactuals remove feedback
at the actual prompt boundary; they must never result in a send.
"""
from copy import deepcopy
from datetime import timedelta, timezone
import json
import traceback
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.c3 import HandoffEvent, MessageBatch, ReplyAction
from app.models.task import Task
from app.services import ai_adapter, c3_service
from app.services.c3_recovery import recover_due_message_batches_once
import test_model_query_hook_pipeline as pipeline
from apps.wechat_ai_customer_service import llm_config
from apps.wechat_ai_customer_service.workflows import customer_service_brain as brain
from test_model_query_hook_pipeline import actual_brain
from test_hook_business_http import clean, post
import test_hook_business_http as business
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from test_hook_formal_runtime import calibrated_clock, drive_reply
from chejin_worker_client.hook_backend import HookWorkerCredentials
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from test_program_preparation_pipeline import configure



@pytest.fixture
def formal_worker(identity_client, lab, actual_brain, tmp_path, monkeypatch):
    """Real local coordinator; only the physical Hook and model are simulated."""
    server, gateway, process, scope, *_ = lab
    original_claimed = business.claimed
    monkeypatch.setattr(business, 'claimed', lambda **kw: original_claimed(process_generation=process.generation, **kw))
    tables = []

    def create():
        w, _, account, binding = pipeline.bound()
        active_scope = replace(scope, account_epoch=account['account_epoch'])
        credentials = HookWorkerCredentials(w['id'], w['worker_token'], w['instance'])
        prepared = SimpleNamespace(process=process, registration=SimpleNamespace(scope=active_scope),
            clock=calibrated_clock(identity_client, credentials, gateway, process, active_scope))
        runtime = HookFormalRuntime(HookFormalClient(identity_client.base_url, 5),
            credentials, gateway, prepared, tmp_path / 'coordinator')
        table = sqlite3.connect(':memory:', check_same_thread=False)
        table.row_factory = sqlite3.Row
        tables.append(table)
        table.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
        # Same synthetic customer inputs as the existing model pipeline. The
        # official Worker now reads them itself through the Hook query API.
        columns = ['localId','MsgSvrID','Type','IsSender','CreateTime','Sequence','MsgServerSeq','MsgSequence','StrTalker','StrContent']
        value = pipeline.coverage(binding)
        # New messages arrive after binding, not in its ambiguous boundary second.
        time.sleep(max(0, int(binding['history_since_create_time']) + 1 - time.time()) + .02)
        for row in value['scans'][0]['databases'][0]['pages'][0]:
            row['CreateTime'] = int(time.time())
            row['Sequence'] = row['CreateTime'] * 1000 + int(row['localId'])
            row['MsgSequence'] = int(row['localId'])
            row['MsgServerSeq'] = 0
            table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)', [row[key] for key in columns])
        server.catalog_entries = [{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'repair-runtime-handle'}]
        server.query_response = lambda body: {'data':[dict(row) for row in table.execute(body['sql']).fetchall()]}
        def on_request(path):
            server.raw_override = None
            if path == '/SendTextMsg':
                sent_text = server.requests[-1][1]['msg']
                created = int(time.time())
                seq = table.execute('SELECT MAX(localId) FROM MSG').fetchone()[0] + 1
                table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (seq,str(8000000000000000000+seq),1,1,created,created*1000+seq,0,seq,binding['peer_wxid'],sent_text))
                server.raw_override = b'{"SendTextMsg":"1"}'
        server.on_request = on_request
        history = runtime.sync(binding)
        assert history['history_ready'], history
        bid = history['message_batch']['batch_id']
        return runtime, binding, bid

    def settle(runtime, binding, bid):
        with SessionLocal() as db:
            original = list(db.scalars(select(ReplyAction).where(ReplyAction.batch_id == bid)))
            original_ids = [row.id for row in original]
            original_texts = [row.reply_text for row in original]
            assert original_ids
        result = drive_reply(runtime, binding, bid)
        assert result['sends'] and all(row['status'] == 'sent' for row in result['sends']), result
        physical = [body for path,body,_ in server.requests if path == '/SendTextMsg']
        assert [body['msg'] for body in physical] == original_texts
        with SessionLocal() as db:
            actions = list(db.scalars(select(ReplyAction).where(ReplyAction.batch_id == bid)))
            assert [row.id for row in actions] == original_ids
            assert all(row.status == 'sent' for row in actions)
            tasks = list(db.scalars(select(Task).where(Task.reply_action_id.in_(original_ids))))
            assert tasks and all(row.status == 'completed' for row in tasks)
        # Re-enter the same terminal batch: no physical resend and no new reply.
        again = drive_reply(runtime, binding, bid)
        assert not again['sends']
        assert len([r for r in server.requests if r[0] == '/SendTextMsg']) == len(physical)
        assert list((tmp_path / 'coordinator' / 'finished').glob('*.json'))
        assert not runtime.active_flow_id

    yield SimpleNamespace(create=create, settle=settle, server=server)
    for table in tables:
        table.close()

def checked_transport(transport):
    def call(**kwargs):
        try:
            return transport(**kwargs)
        except AssertionError:
            # C3 rightly converts production exceptions to a durable failure;
            # test assertions must instead show the exact broken boundary.
            pytest.fail(traceback.format_exc())
    return call


@pytest.mark.parametrize('remove_feedback', [False, True])
def test_review_reason_reaches_repair_and_corrected_reply_reaches_ack(monkeypatch, formal_worker, actual_brain, remove_feedback):
    state = actual_brain
    configure(monkeypatch, state, 'M02', 'normal')
    normal_transport = llm_config.call_llm_request_once_with_wall_timeout
    reason = '客户已明确5—8万元且正在索要车源；已有本轮候选，需列出车辆和实际报价，不应再泛问偏好。'
    error = '未回答本轮查车请求'
    repaired_inputs = []
    first_input = {}
    original_pack = brain.build_brain_repair_prompt_pack
    if remove_feedback:
        def without_reason(**kwargs):
            value = original_pack(**kwargs)
            value['user']['failed_quality_verification'].pop('reason', None)
            return value
        monkeypatch.setattr(brain, 'build_brain_repair_prompt_pack', without_reason)

    def transport(**kw):
        if kw['model'] == 'synthetic-reviewer':
            state['wire'].append(kw)
            request = json.loads(kw['messages'][1]['content'])['review_request']
            state['reviews'].append(request)
            assert request['authority_evidence_summary'] == state['authority_blocks'][-1]
            result = pipeline.joined.review('vehicle_facts', verdict='repair',
                semantic_errors=[error], repair_instruction='按本轮资料修正上述问题。', reason=reason)
            return {'ok': True, 'status': 200, 'response_text': json.dumps(result, ensure_ascii=False)}
        result = normal_transport(**kw)
        user, _ = json.JSONDecoder().raw_decode(kw['messages'][1]['content'])
        is_repair = 'original_brain_plan' in user
        feedback = user.get('failed_quality_verification', {})
        if is_repair:
            repaired_inputs.append(deepcopy(user))
            assert feedback['errors'] == [error, 'answer_incomplete']
            assert 'repair_instruction' not in feedback
            assert user['repair_instruction'] == '按本轮资料修正上述问题。'
            assert user['brain_input']['content_basis'] == state['authority_blocks'][0]
            assert user['brain_input']['conversation'] == first_input['conversation']
            assert user['brain_input']['catalog_protocol']['sources'] == first_input['catalog_protocol']['sources']
            assert 'review_history' not in user['brain_input']
        else:
            first_input.update(deepcopy(user['brain_input']))
        if not is_repair or feedback.get('reason') != reason:
            value = json.loads(result['response_text'])
            value['reply_segments'] = ['有合适的车，您更喜欢什么类型呢？']
            result['response_text'] = json.dumps(value, ensure_ascii=False)
        return result

    monkeypatch.setattr(llm_config, 'call_llm_request_once_with_wall_timeout', checked_transport(transport))
    if not remove_feedback:
        runtime, binding, bid = formal_worker.create()
        result = pipeline.generate(bid)
        assert result['decision'] == 'send_reply', result
        formal_worker.settle(runtime, binding, bid)
        assert len(state['wire']) == 3  # first draft, necessary review, one repair
        assert len(repaired_inputs) == 1 and len(state['receipts']) == 1
        with SessionLocal() as db:
            assert not list(db.scalars(select(HandoffEvent)))
            action = db.scalar(select(ReplyAction))
            assert action.status == 'sent' and '报价' in action.reply_text
    else:
        _, _, _, bid = pipeline.created(monkeypatch)
        result = pipeline.generate(bid)
        assert result['decision'] == 'retry_later', result
        with SessionLocal() as db:
            assert not list(db.scalars(select(ReplyAction)))
    assert repaired_inputs


@pytest.mark.parametrize('query_error', ['wrong_type', 'multiple_fields'])
@pytest.mark.parametrize('remove_feedback', [False, True])
def test_query_type_feedback_survives_durable_retry(monkeypatch, formal_worker, actual_brain, remove_feedback, query_error):
    state = actual_brain
    monkeypatch.setattr(get_settings(), 'c3_batch_recovery_max_attempts', 2)
    original_transport = llm_config.call_llm_request_once_with_wall_timeout
    original_instruction = ai_adapter._brain_retry_instruction
    missing_fields = ['filters', 'reference_ids', 'exclude_ids', 'unsupported_requirements', 'interpretation', 'requirement_state']
    final_feedback = []
    if remove_feedback:
        def without_details(batch):
            value = deepcopy(batch)
            previous = value.get('previous_ai_response_snapshot') or {}
            native = (previous.get('raw_payload') or {}).get('omniauto_brain_result') or {}
            (native.get('catalog_stages') or {}).get('validation_error', {}).pop('field_evidence', None)
            return original_instruction(value)
        monkeypatch.setattr(ai_adapter, '_brain_retry_instruction', without_details)

    def transport(**kw):
        result = original_transport(**kw)
        if kw['model'] != 'synthetic-reviewer' and not state.get('receipt'):
            user, _ = json.JSONDecoder().raw_decode(kw['messages'][1]['content'])
            feedback = user['brain_input']['current_message'].get('retry_instruction', '')
            if feedback:
                # Compare the actual wire field, not only an intermediate helper.
                expected = state['invocations'][-1]['target_state']['brain_retry_instruction']
                assert feedback == expected
                final_feedback.append(feedback)
            details = json.JSONDecoder().raw_decode(feedback.split('校验信息：', 1)[1])[0] if feedback else {}
            field_evidence = details.get('field_evidence', [])
            if query_error == 'wrong_type':
                complete_feedback = any(item.get('field') == 'budget.mode' and item.get('value_type') == 'list' for item in field_evidence)
            else:
                complete_feedback = {item.get('field') for item in field_evidence if item.get('present') is False} == set(missing_fields)
            if not complete_feedback:
                value = json.loads(result['response_text'])
                if query_error == 'wrong_type':
                    value['catalog_query']['budget']['mode'] = []
                else:
                    for field in missing_fields:
                        value['catalog_query'].pop(field, None)
                result['response_text'] = json.dumps(value, ensure_ascii=False)
        return result

    monkeypatch.setattr(llm_config, 'call_llm_request_once_with_wall_timeout', checked_transport(transport))
    runtime, b, bid = formal_worker.create()
    result = pipeline.generate(bid)
    assert result['decision'] == 'retry_later', result
    with SessionLocal() as db:
        batch = db.get(MessageBatch, bid)
        failure = batch.ai_response_snapshot['raw_payload']['omniauto_brain_result']['catalog_stages']['validation_error']
        if query_error == 'wrong_type':
            assert failure['field_evidence'][0]['value_type'] == 'list'
        else:
            assert set(failure['fields']) == set(missing_fields)
            assert all(item['present'] is False for item in failure['field_evidence'])
        due = batch.generated_at + timedelta(seconds=get_settings().c3_batch_retry_delay_seconds + 1)
        assert not list(db.scalars(select(ReplyAction)))
    assert not state['receipts']  # no inventory query with invalid parameters
    # Keep backend and real Worker clocks aligned through permit/begin/ACK.
    time.sleep(max(0, due.replace(tzinfo=timezone.utc).timestamp() - time.time()) + .02)
    recovered = recover_due_message_batches_once()
    assert recovered['generated'] == 0 and recovered['failed'] == 0, recovered
    # Native recovery requires the admitted Worker's poll, not the generic
    # server scheduler. Exercise that same production entrance twice.
    for _ in range(2):
        runtime.batch_view(bid)
    assert len(state['invocations']) == 2
    with SessionLocal() as db:
        actions = list(db.scalars(select(ReplyAction)))
        handoffs = list(db.scalars(select(HandoffEvent)))
        assert db.get(MessageBatch, bid).generation_attempt_count == 2
        if remove_feedback:
            assert not actions and len(handoffs) == 1
            assert handoffs[0].handoff_reason_code == 'AI_ENGINE_RETRY_EXHAUSTED'
        else:
            assert len(actions) == 1 and not handoffs
            action_id = actions[0].id
            task_id = db.scalar(select(Task.id).where(Task.reply_action_id == action_id))
            assert task_id
    if not remove_feedback:
        assert len(state['receipts']) == 1
        feedback = state['invocations'][1]['target_state']['brain_retry_instruction']
        assert 'value_sha256' not in feedback  # keep audit hashes out of guidance
        assert final_feedback == [feedback]
        if query_error == 'wrong_type':
            assert '"present": true' in feedback and '"value_type": "list"' in feedback
        else:
            assert len(feedback) > 700  # both former slicing boundaries would corrupt it
            detail, end = json.JSONDecoder().raw_decode(feedback.split('校验信息：', 1)[1])
            assert set(detail['fields']) == set(missing_fields)
            assert {item['field'] for item in detail['field_evidence']} == set(missing_fields)
            assert feedback.endswith(failure['repair_instruction'])
        formal_worker.settle(runtime, b, bid)


def test_legacy_repair_projection_keeps_original_contract():
    quality = {'ok': False, 'errors': ['synthetic'], 'repair_instruction': '原有OCR修复指引'}
    pack = brain.build_brain_repair_prompt_pack(settings={}, brain_input={'evidence': {}}, plan={}, quality=quality)
    assert pack['user']['failed_quality_verification']['repair_instruction'] == quality['repair_instruction']


@pytest.mark.parametrize('remove_feedback', [False, True])
def test_wrong_price_repair_keeps_expected_fact_and_settles(monkeypatch, formal_worker, actual_brain, remove_feedback):
    state = actual_brain
    configure(monkeypatch, state, 'M02', 'normal')
    normal_transport = llm_config.call_llm_request_once_with_wall_timeout
    original_pack = brain.build_brain_repair_prompt_pack
    seen = []
    if remove_feedback:
        def without_expected_fact(**kwargs):
            pack = original_pack(**kwargs)
            pack['user']['failed_quality_verification'].pop('validation_details', None)
            return pack
        monkeypatch.setattr(brain, 'build_brain_repair_prompt_pack', without_expected_fact)

    def transport(**kw):
        result = normal_transport(**kw)
        assert kw['model'] != 'synthetic-reviewer'  # definite conflict uses existing local check
        user, _ = json.JSONDecoder().raw_decode(kw['messages'][1]['content'])
        if 'original_brain_plan' in user:
            seen.append(deepcopy(user))
        details = user.get('failed_quality_verification', {}).get('validation_details', {})
        rejected = details.get('rejected_numbers', [])
        car = state['receipt']['recommendations'][1]
        informed = any(x.get('product_id') == car['id'] and x.get('expected') == car['price'] for x in rejected)
        if not informed:
            value = json.loads(result['response_text'])
            value['reply_segments'] = [f"{car['name']}报价99万元。"]
            result['response_text'] = json.dumps(value, ensure_ascii=False)
        return result

    monkeypatch.setattr(llm_config, 'call_llm_request_once_with_wall_timeout', checked_transport(transport))
    if remove_feedback:
        _, _, _, bid = pipeline.created(monkeypatch)
        result = pipeline.generate(bid)
        assert result['decision'] == 'retry_later', result
        with SessionLocal() as db:
            assert not list(db.scalars(select(ReplyAction)))
    else:
        runtime, binding, bid = formal_worker.create()
        result = pipeline.generate(bid)
        assert result['decision'] == 'send_reply', result
        formal_worker.settle(runtime, binding, bid)
        assert len(state['wire']) == 2  # initial wrong draft + corrected draft
        assert len(state['receipts']) == 1 and not state['reviews']
        assert seen[0]['brain_input']['content_basis'] == state['authority_blocks'][0]
    assert seen


@pytest.mark.parametrize('extra', [0, 1])
def test_feedback_capacity_preserves_whole_instruction_or_refuses_provider(monkeypatch, extra):
    feedback = '多字段校验信息：' + '甲' * (brain.MAX_RETRY_FEEDBACK_CHARS - len('多字段校验信息：') - 4 + extra) + '完整结尾'
    current = brain.build_brain_input(settings={}, target_name='合成客户',
        target_state={'brain_retry_instruction': feedback}, batch=[], combined='查车',
        raw_capture={}, evidence_pack={})
    assert current['current_message']['retry_instruction'] == feedback
    pack, content, _ = brain.build_sized_brain_prompt(settings={'lean_prompt_threshold_chars': 1}, brain_input=current)
    assert pack['user']['brain_input']['current_message']['retry_instruction'] == feedback
    calls = []
    def provider(**request):
        calls.append(request)
        return {'ok': True}
    monkeypatch.setattr(brain, 'call_llm_request_with_failover', provider)
    response = brain.call_recorded_brain_llm(brain_input=current,
        messages=[{'role':'system','content':pack['system']},{'role':'user','content':content}])
    if extra:
        assert not calls
        assert response['error_kind'] == 'input_capacity_exceeded'
        assert response['error'] == 'retry_feedback_capacity_exceeded'
    else:
        assert response['ok'] and len(calls) == 1
        wire, _ = json.JSONDecoder().raw_decode(calls[0]['messages'][1]['content'])
        assert wire['brain_input']['current_message']['retry_instruction'] == feedback


def test_oversize_retry_hands_off_without_another_model_call(monkeypatch, actual_brain):
    state = actual_brain
    transport = llm_config.call_llm_request_once_with_wall_timeout
    instruction = ai_adapter._brain_retry_instruction
    def bad_query(**kw):
        result = transport(**kw)
        value = json.loads(result['response_text'])
        value['catalog_query']['budget']['mode'] = []
        result['response_text'] = json.dumps(value)
        return result
    monkeypatch.setattr(llm_config, 'call_llm_request_once_with_wall_timeout', checked_transport(bad_query))
    _, _, _, bid = pipeline.created(monkeypatch)
    assert pipeline.generate(bid)['decision'] == 'retry_later'
    monkeypatch.setattr(ai_adapter, '_brain_retry_instruction',
        lambda batch: instruction(batch) + '过量的合成错误说明' * brain.MAX_RETRY_FEEDBACK_CHARS)
    with SessionLocal() as db:
        batch = db.get(MessageBatch, bid)
        due = batch.generated_at + timedelta(seconds=get_settings().c3_batch_retry_delay_seconds + 1)
    monkeypatch.setattr(c3_service, 'utcnow', lambda: due)
    before = len(state['wire'])
    result = pipeline.generate(bid)
    assert result['decision'] == 'handoff', result
    assert len(state['wire']) == before
    with SessionLocal() as db:
        assert not list(db.scalars(select(ReplyAction)))
        handoffs = list(db.scalars(select(HandoffEvent)))
        assert len(handoffs) == 1
        assert handoffs[0].handoff_reason_code == 'AI_ENGINE_INPUT_CAPACITY_EXCEEDED'
