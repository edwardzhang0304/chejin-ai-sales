"""Shared receipt comparison contract; no send result is inferred here."""
from copy import deepcopy

import pytest

from apps.wechat_ai_customer_service.adapters.confirmed_sent_history import extend_checkpoint, validate_receipts
from apps.wechat_ai_customer_service.adapters.message_contract import reply_text_hash
from apps.wechat_ai_customer_service.adapters.historical_text_alignment import (
    checkpoint_for_proof, checkpoint_for_proof_version, comparison_projection,
    validated_projection_continuity, verify_correspondence)
from apps.wechat_ai_customer_service.adapters.business_viewport_continuity import boundary_tokens_for_observations
from apps.wechat_ai_customer_service.adapters.text_correspondence import checkpoint_digest
from apps.wechat_ai_customer_service.adapters.message_viewport_projection import normalized_business_message_sequence
from apps.wechat_ai_customer_service.tests.test_historical_confidence_alignment import scenario, build
from apps.wechat_ai_customer_service.tests.test_historical_text_alignment import checkpoint, frame, build as old_build
from apps.wechat_ai_customer_service.tests.test_historical_confidence_scores import policy


def receipt(number=4):
    text=f'第{number}段回复，我核实一下现有车源再回复您。'
    return dict(reply_action_id=f'action-{number}', reply_text=text, reply_text_hash=reply_text_hash(text),
                worker_stable_id=f'worker-message-{number}', confirmed_at='2026-09-21T04:11:43+00:00')


def test_full_confirmed_sequence_keeps_offscreen_receipts_and_new_repeated_text():
    cp,_=scenario(); offscreen,first,second=receipt(),receipt(5),receipt(6)
    first['reply_text']='欢迎您周末到店看车，我可以帮您提前安排。'
    first['reply_text_hash']=reply_text_hash(first['reply_text'])
    frozen=deepcopy(cp)
    extended=extend_checkpoint(cp,[second,offscreen,first])
    assert cp==frozen
    assert extended['confirmed_sent_receipts']==[offscreen,first,second]
    rows=frame([first['reply_text'],second['reply_text'].replace('核实一下','核实下'),offscreen['reply_text']])
    rows[0]['sender_role']=rows[1]['sender_role']='self'
    found=build(extended,rows)
    assert found and found['continuity']['new_suffix_indexes']==[2]
    assert [(p['old_index'],p['new_index']) for p in found['proof']['pairs']]==[(4,0),(5,1)]
    proof=found['proof']
    assert proof['confirmed_sent_receipts']==[offscreen,first,second]
    restored=checkpoint_for_proof(cp,proof)
    assert restored==extended
    assert verify_correspondence(proof,restored,rows,pre_frame_id='checkpoint:test',post_frame_id='current',
        new_boundary_tokens=boundary_tokens_for_observations(rows,committed_only=False))==found['continuity']


@pytest.mark.parametrize('damage',['body','hash','missing','extra','stable','repeat_action','repeat_id'])
def test_receipt_wire_shape_cannot_manufacture_or_renumber_a_sent_fact(damage):
    value=receipt(); values=[value]
    if damage=='body':value['reply_text']='另一句话'
    if damage=='hash':value['reply_text_hash']='0'*64
    if damage=='missing':value.pop('confirmed_at')
    if damage=='extra':value['approved']=True
    if damage=='stable':value['worker_stable_id']='worker-message-0'
    if damage=='repeat_action':
        another=receipt(5);another['reply_action_id']=value['reply_action_id'];values.append(another)
    if damage=='repeat_id':
        another=receipt(5);another['worker_stable_id']=value['worker_stable_id'];values.append(another)
    with pytest.raises(ValueError,match='PROOF_INVALID'):validate_receipts(values)


def test_already_ingested_receipt_is_not_a_second_history_entry():
    cp,_=scenario();r=receipt(3)
    assert extend_checkpoint(cp,[r]) is cp
    old=checkpoint(['唯一旧消息'])
    assert extend_checkpoint(old,[receipt()]) is old
    damaged=deepcopy(cp);damaged['conversation_id']='another'
    with pytest.raises(ValueError,match='CHECKPOINT_INVALID'):extend_checkpoint(damaged,[receipt()])


def test_frozen_v1_does_not_acquire_new_receipt_scope():
    cp=checkpoint(['唯一开始','一般两厢，平时接送孩子','唯一结束'])
    rows=frame(['唯一开始','般两厢，平时接送孩子','唯一结束'])
    proof=old_build(cp,rows)['proof'];original=deepcopy(cp)
    cp['historical_match_policy']=policy();cp['checkpoint_digest']=checkpoint_digest(cp)
    current=extend_checkpoint(cp,[receipt()])
    assert checkpoint_for_proof_version(current,1)==original
    assert checkpoint_for_proof(current,proof)==original
    assert old_build(checkpoint_for_proof(current,proof),rows)['proof']==proof


def test_completed_voice_keeps_frozen_rule_with_confirmed_self_tail():
    cp=checkpoint(['唯一开始','一般两厢，平时接送孩子','唯一结束'])
    before=frame(['唯一开始','一般两厢，平时接送孩子','唯一结束'])
    before[1].update(row_kind='voice_transcript',message_type='voice',voice_state='transcribed',
                     voice_duration='5',native_source_message_id='same-voice')
    projections=normalized_business_message_sequence(before,message_viewport_bounds=None)
    tokens=boundary_tokens_for_observations(before,committed_only=False)
    for i,e in enumerate(cp['recent_messages']):
        e.update(message_type=before[i]['message_type'],business_projection=projections[i],
                 strong_boundary_tokens=list(tokens.get(i,[])),native_source_message_id=before[i].get('native_source_message_id'))
    cp['historical_match_policy']=policy();cp['checkpoint_digest']=checkpoint_digest(cp)
    cp=extend_checkpoint(cp,[receipt()])
    rows=deepcopy(before);rows[1]['content_clean']='般两厢，平时接送孩子'
    tail=frame([receipt()['reply_text']])[0];tail.update(sender_role='self',observation_id='sent-tail',bubble_rect=[10,240,300,260])
    before.append(deepcopy(tail));rows.append(tail)
    built=build(cp,rows)
    assert built and built['proof']['version']==1
    projected,proof=comparison_projection(cp,rows,pre_frame_id='checkpoint:test',post_frame_id='current')
    assert projected[1]['normalized_content_signature']==projections[1]['normalized_content_signature']
    result=validated_projection_continuity(cp,rows,
        old_projection=normalized_business_message_sequence(before,message_viewport_bounds=None),
        old_boundary_tokens=boundary_tokens_for_observations(before,committed_only=False),
        pre_frame_id='checkpoint:test',post_frame_id='current')
    assert result and result['relation']=='business_sequence_equal'
    assert len(result['matched_pairs'])==4
    rows[-1]['content_clean']='完全不同的新回复'
    assert validated_projection_continuity(cp,rows,
        old_projection=normalized_business_message_sequence(before,message_viewport_bounds=None),
        old_boundary_tokens=boundary_tokens_for_observations(before,committed_only=False),
        pre_frame_id='checkpoint:test',post_frame_id='current') is None


def test_full_server_window_does_not_exclude_its_newly_confirmed_send():
    cp,_=scenario(old=[f'唯一旧记录{i}尾部' for i in range(200)])
    sent=receipt(201)
    cp=extend_checkpoint(cp,[sent])
    rows=frame(['唯一旧记录199尾部',sent['reply_text'].replace('核实一下','核实下'),'新的客户提问'])
    rows[1]['sender_role']='self'
    found=build(cp,rows)
    assert found and found['continuity']['new_suffix_indexes']==[2]
    assert [(p['old_index'],p['new_index']) for p in found['proof']['pairs']]==[(199,0),(200,1)]
