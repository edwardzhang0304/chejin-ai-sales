"""Strict optional cleanup facts; protocol checks, separate from pixel integration."""
from copy import deepcopy

import pytest
from apps.wechat_ai_customer_service.adapters.pre_send_read_failure import (
    read_failure_valid, replacement_input_ready, validate_proof, PROOF_SCHEMA,
)


def evidence():
    context={'reply_action_id':'action','conversation_id':'conversation','reply_text_hash':'a'*64,
             'task_id':'task','flow_id':'flow','authorization_revision':'auth'}
    phase={'source':'action_journal','ok':True,'action_phase':'not_attempted'}
    failure={'stage':'before_trigger','operation':'read','call_status':'failed','attempt_id':'one',
             'error_code':'C3_SEND_PRE_CLICK_CONTEXT_UNAVAILABLE','failure_reason':'capture failed',
             'physical_send_triggered':False,'action_phase':'not_attempted','phase_proof':phase,
             'input_state':'unverified','input_progress':'may_have_started',
             'frame_id':None,'no_frame_reason':'capture failed',
             'program_draft_cleanup':{**{k:context[k] for k in ('reply_action_id','conversation_id','reply_text_hash')},
                 'target':'CJTEST01','cleanup':{'ok':True,'cleared':False,'clear_attempted':True,
                     'method':'select_all_backspace','reason':'confirmed_program_draft_clear_requested',
                     'focus_check':{'ok':True,'expected_length':8,'observed_length':8}}}}
    proof={'version':1,**context,'first_failure':failure,'recheck':{'budget_state':'consumed',
        'started':True,'failure':{**deepcopy(failure),'attempt_id':'two'}},'outcome':'exhausted',
        'terminal_phase_proof':phase,'input_state':'unverified','input_progress':'may_have_started'}
    return context,failure,proof


def test_cleanup_fact_does_not_claim_empty_and_fits_existing_open_receipt():
    import jsonschema
    context,failure,proof=evidence()
    assert read_failure_valid(failure)
    assert replacement_input_ready(failure,context=context,target='CJTEST01')
    assert failure['input_state']=='unverified' and failure['program_draft_cleanup']['cleanup']['cleared'] is False
    assert validate_proof(proof)==proof
    jsonschema.validate(proof,PROOF_SCHEMA)


@pytest.mark.parametrize('bad', ['clear_error','no_operation','wrong_method','wrong_reason',
    'claimed_empty','no_focus','different_length','bool_length','missing_hash','wrong_action',
    'wrong_conversation','wrong_text','wrong_target','triggered','phase','before_input','legacy_unverified'])
def test_incomplete_or_foreign_clear_is_not_a_reentry_permission(bad):
    context,failure,proof=evidence();fact=failure['program_draft_cleanup'];cleanup=fact['cleanup']
    if bad=='clear_error':cleanup['ok']=False
    if bad=='no_operation':cleanup['clear_attempted']=False
    if bad=='wrong_method':cleanup['method']='unknown'
    if bad=='wrong_reason':cleanup['reason']='unknown'
    if bad=='claimed_empty':cleanup['cleared']=True
    if bad=='no_focus':cleanup['focus_check']['ok']=False
    if bad=='different_length':cleanup['focus_check']['observed_length']=7
    if bad=='bool_length':cleanup['focus_check'].update(expected_length=True,observed_length=True)
    if bad=='missing_hash':fact.pop('reply_text_hash')
    if bad=='wrong_action':fact['reply_action_id']='other'
    if bad=='wrong_conversation':fact['conversation_id']='other'
    if bad=='wrong_text':fact['reply_text_hash']='b'*64
    if bad=='wrong_target':fact['target']='OTHER'
    if bad=='triggered':failure['physical_send_triggered']=True
    if bad=='phase':failure['phase_proof']['action_phase']='trigger_attempted'
    if bad=='before_input':failure['stage']='before_input'
    if bad=='legacy_unverified':failure.pop('program_draft_cleanup')
    assert not replacement_input_ready(failure,context=context,target='CJTEST01')
    if bad not in {'wrong_target','legacy_unverified'}:
        with pytest.raises(ValueError,match='PROOF_INVALID'):validate_proof(proof)


def test_legacy_failed_read_receipts_remain_valid():
    _,_,proof=evidence()
    for item in [proof['first_failure'],proof['recheck']['failure']]:item.pop('program_draft_cleanup')
    assert validate_proof(proof)==proof
