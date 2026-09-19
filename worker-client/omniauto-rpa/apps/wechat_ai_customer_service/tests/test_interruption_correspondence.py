"""Portable synthetic proof vectors; no Worker, model, or desktop dependency."""
from copy import deepcopy
import pytest
from test_send_interruption import receipt
from test_historical_text_alignment import checkpoint, frame
from apps.wechat_ai_customer_service.adapters.historical_text_alignment import comparison_projection
from apps.wechat_ai_customer_service.adapters.message_viewport_projection import normalized_business_message_sequence
from apps.wechat_ai_customer_service.adapters.business_viewport_continuity import compare_business_viewport_continuity, boundary_tokens_for_observations
from apps.wechat_ai_customer_service.adapters.send_interruption import confirmed_customer_interruption


@pytest.mark.parametrize('damage',[None,'missing_checkpoint','proof','observation','unsafe_draft','negation'])
def test_reuses_verified_d1_without_overwriting_original_text(damage):
    texts=['周末带家人出去看看','一般两厢，平时接送孩子','顺便看看后备箱空间']
    historical=checkpoint(texts);before=frame(texts)
    current=frame([texts[0],'般两厢，平时接送孩子',texts[2],'那周末出游够用吗'])
    old,op=comparison_projection(historical,before,pre_frame_id='checkpoint:send-guard',post_frame_id='send-guard:baseline')
    new,np=comparison_projection(historical,current,pre_frame_id='checkpoint:send-guard',post_frame_id='send-guard:current')
    tokens=boundary_tokens_for_observations(before,committed_only=False)
    decision=compare_business_viewport_continuity(old,new,old_boundary_tokens=tokens,
        new_boundary_tokens=boundary_tokens_for_observations(current,committed_only=False),allow_history_suffix=True)
    decision['text_correspondence']={'baseline':op,'current':np}
    assert decision['relation']=='unique_tail_append'
    data=receipt();guard=data['evidence']['guard'];check=guard['visual']['context_check']
    baseline=guard['send_baseline']['send_context_guard']
    baseline.update(sequence=normalized_business_message_sequence(before,message_viewport_bounds=None),
        worker_continuity_contract={'old_boundary_tokens':{str(k):list(v) for k,v in tokens.items()},
            'historical_alignment':{'checkpoint':historical,'baseline_observations':before}})
    check['worker_continuity_decision']=decision
    check['snapshot']['message_sequence']=current
    check['snapshot']['send_context_guard']['sequence']=normalized_business_message_sequence(current,message_viewport_bounds=None)
    if damage=='missing_checkpoint': baseline['worker_continuity_contract']['historical_alignment'].pop('checkpoint')
    if damage=='proof': decision['text_correspondence']['current']={}
    if damage=='observation': current[1]['content_clean']='价格九万元'
    if damage=='negation': current[1]['content_clean']='不一般两厢，平时接送孩子'
    if damage=='unsafe_draft': guard['visual']['draft_clear']['cleared']=False
    frozen=deepcopy(data)
    assert confirmed_customer_interruption(**data) is (damage is None)
    assert data==frozen
