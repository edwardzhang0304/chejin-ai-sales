"""Narrow evidence policy checks, separate from the full HTTP/Worker cases."""
import pytest
from app.contracts.shared_rules import shared_adapter


def evidence():
    sequence = [dict(sender_role='customer',message_type='text',
                     normalized_content_signature=f'message-{i}',media_state='') for i in range(2)]
    frame = {'frame_id':'frame-after-typing'}
    target = {'ok':True,'confirmed_target':'CJTEST01','conversation_type':'private'}
    return {'guard':{**target,'send_baseline':{'send_context_guard':{
        'sequence': [sequence[0].copy()], 'sequence_sha256':'a'*64}},'visual':{
        'error_code':'C3_CONTEXT_CHANGED_BEFORE_SEND',
        'physical_send_triggered':False,
        'draft_clear':{'ok':True,'cleared':True,'reason':'confirmed_program_draft_cleared',
            'input_region':{'has_visible_text':False},
            'focus_check':{'ok':True,'expected_length':8,'observed_length':8}},
        'context_check':{
            'ok':False,'error_code':'C3_CONTEXT_CHANGED_BEFORE_SEND','continuity_relation':'unique_tail_append',
            'worker_continuity_decision':{'relation':'unique_tail_append','old_count':1,'new_count':2,'new_suffix_indexes':[1],
                'overlap_candidates':[{'has_unique_strong_boundary':True}],
                'matched_pairs':[{'old_index':0,'new_index':0}]},
            'expected_sequence_sha256':'a'*64,'current_sequence_sha256':'b'*64,
            'frame_observation':frame,
            'snapshot':{'ok':True,'validation':target.copy(),'frame_observation':frame.copy(),
                'send_context_guard':{'ok':True,'tail_complete':True,'sequence':sequence,'sequence_sha256':'b'*64},
                'message_sequence':[{'sender_role':'customer','observation_id':'old'},{'sender_role':'customer','observation_id':'new'}]},
        }}}}


def decide(value,phase='not_attempted',code='C3_CONTEXT_CHANGED_BEFORE_SEND'):
    return shared_adapter('reply_sequence').confirmed_customer_interruption(error_code=code,action_phase=phase,evidence=value)


def test_only_customer_append_with_cleared_owned_draft_can_continue():
    assert decide(evidence()) == {'frame_id':'frame-after-typing','observation_ids':['new']}


@pytest.mark.parametrize('changed',[
    'possibly_sent','cleanup_failed','not_owned','focus_unknown','ambiguous',
    'different_frame','partial_tail','no_new_message','new_sales','bad_observation_id',
    'bad_digest','wrong_code','unknown_phase','malformed',
])
def test_uncertain_or_non_customer_change_keeps_original_failure_handling(changed):
    value=evidence();visual=value['guard']['visual'];check=visual['context_check'];snap=check['snapshot']
    if changed=='possibly_sent':visual['physical_send_triggered']=True
    elif changed=='cleanup_failed':visual['draft_clear']['cleared']=False
    elif changed=='not_owned':visual['draft_clear']['reason']='draft_owner_unknown'
    elif changed=='focus_unknown':visual['draft_clear']['focus_check']['ok']=False
    elif changed=='ambiguous':check['worker_continuity_decision']['relation']='business_sequence_unresolved'
    elif changed=='different_frame':snap['frame_observation']['frame_id']='stale-frame'
    elif changed=='partial_tail':snap['send_context_guard']['tail_complete']=False
    elif changed=='no_new_message':check['worker_continuity_decision']['new_suffix_indexes']=[]
    elif changed=='new_sales':snap['send_context_guard']['sequence'][1]['sender_role']='self'
    elif changed=='bad_observation_id':snap['message_sequence'][1]['observation_id']=['bad']
    elif changed=='bad_digest':check['current_sequence_sha256']='c'*64
    elif changed=='malformed':value={'guard':['bad']}
    assert decide(value,phase='trigger_attempted' if changed=='unknown_phase' else 'not_attempted',code='RPA_SEND_REPLY_FAILED' if changed=='wrong_code' else 'C3_CONTEXT_CHANGED_BEFORE_SEND') is None


def test_image_pending_receipt_is_not_a_business_completion_or_relaxed_terminal_rule():
    from chejin_worker_client.task_runner import TaskRunner
    from chejin_worker_client.transaction_outcomes import classify_action_result
    value={'state':'completed','action_phase':'confirmed','business_state':'confirmed_result_pending_continuity','business_result_confirmed':False,'_image_identity_receipt_confirmed':True}
    normalized=TaskRunner._normalize_one_image_slot_result(value)
    assert normalized['result']==value and normalized['action_outcome']=={}
    assert classify_action_result('image',value)['result']=='failed'
    invalid={**value,'_image_identity_receipt_confirmed':False}
    assert TaskRunner._normalize_one_image_slot_result(invalid)['result']['state']=='failed'


def before_input_evidence():
    value = evidence()
    check = value['guard']['visual']['context_check']
    snapshot = check.pop('snapshot')
    snapshot['input_region'] = {'has_visible_text': False}
    snapshot['screenshot_path'] = '/controlled/current-frame.png'
    check['expected_context_guard'] = value['guard']['send_baseline']['send_context_guard']
    check.pop('frame_observation')
    return {'state': 'send_context_changed_before_input', 'guard': {**snapshot['validation'], 'screenshot_path': snapshot['screenshot_path']},
        'send_baseline': snapshot, 'context_validation': check, 'action_journal': {'ok': True, 'action_phase': 'not_attempted'}}


def test_before_input_customer_append_needs_no_fabricated_cleanup():
    assert decide(before_input_evidence()) == {'frame_id': 'frame-after-typing', 'observation_ids': ['new']}


@pytest.mark.parametrize('change', ['journal_missing', 'possibly_sent', 'draft_present', 'draft_unknown',
    'different_capture', 'frame_missing', 'unknown_change', 'new_sales', 'bad_digest', 'wrong_state'])
def test_before_input_unproven_state_cannot_cancel_as_customer_interruption(change):
    value = before_input_evidence()
    snapshot, check = value['send_baseline'], value['context_validation']
    if change == 'journal_missing': value.pop('action_journal')
    elif change == 'possibly_sent': value['action_journal']['action_phase'] = 'trigger_attempted'
    elif change == 'draft_present': snapshot['input_region']['has_visible_text'] = True
    elif change == 'draft_unknown': snapshot['input_region'] = {}
    elif change == 'different_capture': value['guard']['screenshot_path'] = '/different.png'
    elif change == 'frame_missing': snapshot['frame_observation'] = {}
    elif change == 'unknown_change': check['worker_continuity_decision']['relation'] = 'business_sequence_unresolved'
    elif change == 'new_sales': snapshot['send_context_guard']['sequence'][1]['sender_role'] = 'self'
    elif change == 'bad_digest': check['current_sequence_sha256'] = 'different'
    elif change == 'wrong_state': value['state'] = 'send_result_unknown'
    assert decide(value) is None
