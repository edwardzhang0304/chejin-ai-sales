"""Constructed D1 sequence proof vectors; not a real-HTTP/Windows claim."""
from copy import deepcopy
import hashlib

import pytest

from apps.wechat_ai_customer_service.adapters.historical_text_alignment import build_correspondence, verify_correspondence
from apps.wechat_ai_customer_service.adapters.text_correspondence import checkpoint_digest
from apps.wechat_ai_customer_service.adapters.message_viewport_projection import normalized_business_message_sequence
from apps.wechat_ai_customer_service.adapters.business_viewport_continuity import boundary_tokens_for_observations


def frame(texts):
    return [{'observation_id': f'now-{i}', 'sender_role': 'customer', 'message_type': 'text',
        'row_kind': 'text_bubble', 'content_clean': text, 'bubble_rect': [10, 100+i*40, 300, 120+i*40],
        'item_state': 'completed', 'contract_errors': []} for i, text in enumerate(texts)]


def checkpoint(texts):
    rows = frame(texts)
    projections = normalized_business_message_sequence(rows, message_viewport_bounds=None)
    tokens = boundary_tokens_for_observations(rows, committed_only=False)
    entries = [{'stable_id': f'worker-message-{i+1}', 'source_message_key': f'source-{i}',
        'sender_role':'customer', 'message_type':'text', 'business_projection': projections[i],
        'effective_text': {'version':0,'text':text,'sha256':hashlib.sha256(text.encode()).hexdigest()},
        'strong_boundary_tokens': list(tokens.get(i, []))} for i,text in enumerate(texts)]
    result = {'version':3, 'conversation_id':'conv-test', 'recent_messages':entries,
        'text_correspondence_context': {'version':1,'known_entities':[]}}
    result['checkpoint_digest'] = checkpoint_digest(result)
    return result


def build(old, new, **kwargs):
    return build_correspondence(old, new, pre_frame_id='checkpoint-test', post_frame_id='frame-test',
        new_boundary_tokens=boundary_tokens_for_observations(new, committed_only=False), **kwargs)


@pytest.mark.parametrize('changed', [0, 1, 4])
def test_five_old_rows_plus_new_question_have_one_proven_correspondence(changed):
    texts = ['旧锚点甲', '旧锚点乙', '旧锚点丙', '旧锚点丁', '旧锚点戊']
    texts[changed] = '一般两厢，平时接送孩子'
    old = checkpoint(texts)
    current = list(texts);current[changed] = '般两厢，平时接送孩子';current.append('那周末出游够用吗')
    rows = frame(current); frozen = deepcopy((old, rows))
    result = build(old, rows)
    assert result, (old, rows)
    proof = result['proof']
    assert len([p for p in proof['pairs'] if p['matched_by'] == 'context_ocr']) == 1
    assert result['continuity']['new_suffix_indexes'] == [5]
    assert (old, rows) == frozen
    checked = verify_correspondence(proof, old, rows, pre_frame_id='checkpoint-test', post_frame_id='frame-test',
        new_boundary_tokens=boundary_tokens_for_observations(rows, committed_only=False))
    assert checked == result['continuity']
    assert build(old, rows, enabled=False) is None


@pytest.mark.parametrize('case', ['too_short','two_rows','two_errors','repeat_anchors','no_boundary','known_name','negation'])
def test_insufficient_or_protected_context_cannot_supply_a_proof(case):
    texts = ['唯一旧锚点甲', '一般两厢，平时接送孩子', '唯一旧锚点乙']
    current = ['唯一旧锚点甲', '般两厢，平时接送孩子', '唯一旧锚点乙']
    if case == 'too_short': texts[1],current[1]='二手车','手车'
    if case == 'two_rows': texts=texts[:2];current=current[:2]
    if case == 'two_errors': texts.append('周末陪伴孩子出去踏青');current.append('周末陪伴孩纸出去踏青')
    if case == 'repeat_anchors': texts[0]=texts[2]='好的';current[0]=current[2]='好的'
    if case == 'known_name': texts[1],current[1]='客户王梓轩平时接送孩子','客户王子轩平时接送孩子'
    if case == 'negation': texts[1],current[1]='我想看看车然后带孩子出去旅行','我看看车然后带孩子出去旅行'
    old=checkpoint(texts)
    if case == 'no_boundary':
        for entry in old['recent_messages']: entry['strong_boundary_tokens']=[]
    if case == 'known_name': old['text_correspondence_context']['known_entities']=[{'kind':'person','value':'王梓轩'}]
    old['checkpoint_digest']=checkpoint_digest(old)
    assert build(old,frame(current)) is None


@pytest.mark.parametrize('tamper', ['score','old_index','source','hash','anchor','observation','frame','unknown_field'])
def test_verifier_recomputes_instead_of_trusting_client_booleans(tamper):
    old=checkpoint(['锚点甲','一般两厢，平时接送孩子','锚点乙'])
    rows=frame(['锚点甲','般两厢，平时接送孩子','锚点乙'])
    proof=build(old,rows)['proof']
    if tamper=='score': proof['pairs'][1]['similarity']=1.0
    if tamper=='old_index': proof['pairs'][1]['old_index']=0
    if tamper=='source': proof['pairs'][1]['source_message_key']='foreign'
    if tamper=='hash': proof['pairs'][1]['canonical_text_sha256']='0'*64
    if tamper=='anchor': proof['pairs'][1]['exact_anchor_pairs']=[]
    if tamper=='observation': proof['pairs'][1]['observation_id']='foreign'
    if tamper=='frame': proof['post_frame_id']='other'
    if tamper=='unknown_field': proof['approved']=True
    with pytest.raises(ValueError,match='PROOF_INVALID'):
        verify_correspondence(proof,old,rows,pre_frame_id='checkpoint-test',post_frame_id='frame-test',
            new_boundary_tokens=boundary_tokens_for_observations(rows,committed_only=False))


def test_changed_entity_context_expires_old_proof_and_missing_is_legacy_exact():
    old=checkpoint(['锚点甲','一般两厢，平时接送孩子','锚点乙'])
    rows=frame(['锚点甲','般两厢，平时接送孩子','锚点乙'])
    proof=build(old,rows)['proof']
    old['text_correspondence_context']['known_entities']=[{'kind':'vehicle','value':'两厢'}]
    with pytest.raises(ValueError,match='CHECKPOINT_INVALID'): build(old,rows)
    old['checkpoint_digest']=checkpoint_digest(old)
    with pytest.raises(ValueError,match='CHECKPOINT_EXPIRED'):
        verify_correspondence(proof,old,rows,pre_frame_id='checkpoint-test',post_frame_id='frame-test',new_boundary_tokens={})
    old.pop('text_correspondence_context')
    assert build(old,rows) is None
