"""Confirmed sent history uses HC in real Worker/SQLite; desktop/API controlled."""
from copy import deepcopy
import hashlib

import pytest

from test_c2_identity_gate_receipts import harness
from test_task_runner import (FakeApi, FakeBridge, identity_checkpoint_for_facts,
    pre_send_fact, pre_send_fact_checkpoint_response)
from chejin_worker_client import storage
from chejin_worker_client.c2_contract import c2_contract_v3
from chejin_worker_client.models import Binding, RpaResult, WechatReadTarget
from chejin_worker_client.pre_send_checkpoint import canonical_sha256
from chejin_worker_client.shared_rules import text_correspondence


def setup_case(harness, *, phase='authorized_read', noisy_history=False,
               segments=1, scrolled=False, corrected_at='ocr', short_receipt=False):
    history = [('self', '这是之前打招呼的内容'), ('customer', '您好，我想了解一下车型'),
        ('self', '这款600Pro适合日常通勤，具体信息可以再看看。'), ('customer', '预算五万左右')]
    receipts = [('self', f'第{i+1}段回复，我核实一下现有车源再回复您。') for i in range(segments)]
    if short_receipt:receipts=[('self','核实一下')]
    checkpoint = identity_checkpoint_for_facts('receipt-recheck', [
        {'sender_role':role, 'content':text} for role,text in history])
    checkpoint.update(conversation_id='receipt-recheck',
        text_correspondence_context={'version':1,'known_entities':[]},
        historical_match_policy=c2_contract_v3()['text_correspondence_contract']['historical_match_policy'])
    for entry, (_,text) in zip(checkpoint['recent_messages'], history):
        entry['effective_text']={'text':text,'version':0,'sha256':hashlib.sha256(text.encode()).hexdigest()}
    checkpoint['checkpoint_digest']=text_correspondence.checkpoint_digest(checkpoint)
    target=WechatReadTarget(conversation_id='receipt-recheck',display_name='CJTEST01',remark_code='CJTEST01',
        rpa_session_key='',authorization_revision='receipt-recheck-auth',unread_generation=1,
        raw={'identity_checkpoint':checkpoint})

    class Bridge(FakeBridge):
        captures=0
        def frame(self, fixed=False):
            texts=deepcopy(history+receipts)
            if noisy_history:
                texts[2]=('self',texts[2][1].replace('600Pro','600Pr0'))
            if not fixed:
                texts[-1]=('self',texts[-1][1].replace('核实一下','核实下'))
            if scrolled:
                texts=texts[1:]
            # Customer repeating old text is still a new occurrence.
            texts.append(('customer', '预算五万左右'))
            return self._contractual_message_payload({'ok':True, 'messages':[
                {'id':f'frame-{self.captures}-{i}','sender_role':r,'type':'text','content':t}
                for i,(r,t) in enumerate(texts)], 'frame_observation':{'frame_id':f'frame-{self.captures}'},
                'sidecar_run_id':f'frame-{self.captures}','tail_complete':True})
        def get_messages(self, **kwargs):
            self.captures+=1
            self.get_messages_payloads=[self.frame(corrected_at=='first' or
                (corrected_at=='capture' and self.captures>1))]
            return super().get_messages(**kwargs)
        def recheck_text_bubbles(self, **kwargs):
            self.stages.append(kwargs['stage'])
            self.selections.append(kwargs['observation_ids'])
            assert kwargs['observation_ids']==[f'frame-{self.captures}-{len(history)+segments-1-int(scrolled)}']
            if kwargs['stage']=='validate':
                return {'ok':True}
            return self.frame(corrected_at=='ocr')

    api,bridge=FakeApi(None),Bridge(RpaResult(ok=True,result_code='unused'))
    bridge.stages=[];bridge.selections=[]
    runner,_=harness.make_runner(api,bridge)
    binding=Binding('worker-test','test-token','instance-test',run_status='running')
    runner.binding=binding; storage.save_binding(binding)
    local=[{'reply_action_id':f'confirmed-segment-{i}','reply_text':t,
            'reply_text_hash':runner._reply_text_hash(t),'worker_stable_id':f'worker-message-{len(history)+i+1}',
            'confirmed_at':'2026-09-21T04:11:43+00:00'} for i,(_,t) in enumerate(receipts)]
    storage.save_c2_state('message_identity:'+target.conversation_id,
        {'version':4,'next_sequence':len(history)+segments+1,'ai_reply_receipts':local})
    for e in checkpoint['recent_messages']:
        storage.save_c2_ledger_terminal(conversation_id=target.conversation_id,
            source_message_key=e['source_message_key'],origin_read_run_id='old-read',dedupe_key=None,
            message_type='text',terminal_state='completed',ingest_state='confirmed')
    if phase=='pre_send_refresh':
        # A formal pre-send fact checkpoint contains ingested facts, not
        # unsubmitted local receipts. Model that prerequisite faithfully.
        from apps.wechat_ai_customer_service.adapters.confirmed_sent_history import extend_checkpoint
        extended=extend_checkpoint(checkpoint,local)
        checkpoint['recent_messages']=extended['recent_messages']
        checkpoint['checkpoint_digest']=text_correspondence.checkpoint_digest(checkpoint)
        for e in checkpoint['recent_messages']:
            storage.save_c2_ledger_terminal(conversation_id=target.conversation_id,
                source_message_key=e['source_message_key'],origin_read_run_id='old-read',dedupe_key=None,
                message_type='text',terminal_state='completed',ingest_state='confirmed')
        response=pre_send_fact_checkpoint_response(conversation_id=target.conversation_id,
            batch_id='receipt-batch',reply_action_id='next-action',facts=[pre_send_fact(
                f'worker-message-{i+1}',sender_role=r,message_type='text',content=t)
                for i,(r,t) in enumerate(history+receipts)])
        frozen=response['pre_send_fact_checkpoint']
        for f,e in zip(frozen['committed_tail'],checkpoint['recent_messages']):
            f.update(deepcopy(e),commit_basis=e['message_identity_commit_record']['commit_basis'])
        response['pre_send_fact_checkpoint_binding']['checkpoint_digest']=canonical_sha256(frozen)
        target.raw['pre_send_fact_checkpoint_context']={'schema_version':1,'checkpoint':frozen,
            'binding':response['pre_send_fact_checkpoint_binding']}
    api.read_targets=[target]
    return runner,binding,target,api,bridge


@pytest.mark.parametrize('phase',['authorized_read','pre_send_refresh','reply_sequence_read'])
@pytest.mark.parametrize('noisy_history',[False,True],ids=['exact-history','accepted-hc-history'])
@pytest.mark.parametrize('segments,scrolled',[(1,False),(2,True)],ids=['single','segmented-slide'])
def test_receipt_tail_uses_same_hc_without_redundant_ocr(
        harness,phase,noisy_history,segments,scrolled):
    runner,binding,target,api,bridge=setup_case(harness,phase=phase,noisy_history=noisy_history,
        segments=segments,scrolled=scrolled,corrected_at='never')
    result=runner._read_one_wechat_target(binding,target,enforce_read_targets=True,wait_for_brain=False,
        current_step='pre_send_refresh' if phase=='pre_send_refresh' else 'message_read',
        operation_phase=phase,current_only=phase!='authorized_read')
    assert result['ok'],result
    assert bridge.captures==1
    assert bridge.stages==[]
    assert not bridge.sent_replies
    assert not storage.load_runtime_control()['inflight_flow_id']
    assert runner.current_ui_lock is None
    assert binding.run_status=='running'
    assert result['new_customer_message_count']==1
    assert [m['content'] for p in api.message_payloads for m in p['messages']
        if m.get('sender_role_hint')=='customer']==['预算五万左右']
    records=[r['metadata']['text_recheck_evidence'] for r in storage.read_logs(limit=300)
        if r['event']=='c2_text_recheck_completed']
    assert records==[]


def test_review_retains_complete_compared_history_after_receipt_consumption(harness, tmp_path):
    import json
    from chejin_worker_client.historical_alignment import save_match_review
    runner,binding,target,api,bridge=setup_case(harness)
    payload,errors=runner._align_initial_identity_frame(target=target,
        sidecar_payload=bridge.frame(),read_run_id='review-read')
    assert not errors,errors
    payload['artifact_dir']=str(tmp_path)
    storage.save_c2_state('message_identity:'+target.conversation_id,{'ai_reply_receipts':[]})
    path=save_match_review(target,payload,read_run_id='review-read',result={'ok':True})
    saved=json.loads(open(path).read())
    assert len(saved['original_messages'])==5
    assert saved['confirmed_sent_receipts'][0]['worker_stable_id']=='worker-message-5'
    assert saved['observations']==payload['observations']
    assert saved['checkpoint_digest']==payload['sequence_alignment_evidence']['text_correspondence']['checkpoint_digest']


@pytest.mark.parametrize('state',['unknown','identity_commit_conflict_possible_sent','pending'])
def test_unconfirmed_send_never_enters_historical_confidence_scope(harness,state):
    from chejin_worker_client.historical_alignment import checkpoint_for_target
    runner,binding,target,api,bridge=setup_case(harness)
    key='message_identity:'+target.conversation_id
    stored=storage.load_c2_state(key)
    stored['ai_reply_receipts'][0]['reconciliation_state']=state
    storage.save_c2_state(key,stored)
    cp=checkpoint_for_target(target)
    assert cp==target.raw['identity_checkpoint']
    assert not cp.get('confirmed_sent_receipts')


@pytest.mark.parametrize('damage', ['old_receipt', 'old_checkpoint', 'new_customer',
    'voice_body', 'image_retyped', 'role', 'new_repeat'])
def test_media_continuity_only_projects_proven_old_text(harness, damage):
    from chejin_worker_client.historical_alignment import checkpoint_for_target, reconcile_viewports
    from chejin_worker_client.message_viewport_projection import (
        normalized_business_message_sequence, compare_business_viewport_continuity, boundary_tokens_for_observations)
    runner,binding,target,api,bridge=setup_case(harness)
    before=bridge.frame(fixed=True)['observations']
    # A completed voice/image belongs to this current action, outside the old
    # text checkpoint. Its identity and content must never gain a text score.
    voice=deepcopy(before[-1]);voice.update(observation_id='voice-result',message_type='voice',
        row_kind='voice_transcript',voice_state='transcribed',voice_duration='5',
        content_clean='语音的新内容',native_source_message_id='actual-voice')
    picture=deepcopy(before[-1]);picture.update(observation_id='image-result',message_type='image',
        row_kind='image_bubble',item_state='completed',native_source_message_id='actual-image')
    picture.pop('content_clean',None)
    before.extend([voice,picture])
    for i,row in enumerate(before):
        row['bubble_rect']=[100,100+60*i,500,145+60*i]
    after=deepcopy(before)
    if damage=='old_receipt':after[4]['content_clean']=after[4]['content_clean'].replace('核实一下','核实下')
    if damage=='old_checkpoint':after[2]['content_clean']=after[2]['content_clean'].replace('600Pro','600Pr0')
    if damage=='new_customer':after[5]['content_clean']='预算五万元左右'
    if damage=='voice_body':after[-2]['content_clean']='语音的另一个新内容'
    if damage=='image_retyped':after[-1].update(message_type='voice',row_kind='voice_bubble',voice_duration='5')
    if damage=='role':after[4]['sender_role']='customer'
    if damage=='new_repeat':
        after[4]['content_clean']=after[4]['content_clean'].replace('核实一下','核实下')
        repeated=deepcopy(after[3]);repeated['observation_id']='new-repeat'
        repeated['bubble_rect']=[100,100+60*len(after),500,145+60*len(after)]
        after.append(repeated)
    old_tokens=boundary_tokens_for_observations(before,committed_only=False)
    strict=compare_business_viewport_continuity(
        normalized_business_message_sequence(before,message_viewport_bounds=None),
        normalized_business_message_sequence(after,message_viewport_bounds=None),
        old_boundary_tokens=old_tokens,new_boundary_tokens=boundary_tokens_for_observations(after,committed_only=False))
    accepted={'business_sequence_equal','unique_tail_append','unique_viewport_slide_with_tail_append'}
    assert strict['relation'] not in accepted
    snapshots=deepcopy((before,after))
    result=reconcile_viewports(checkpoint_for_target(target),before,after,strict,old_boundary_tokens=old_tokens)
    assert (before,after)==snapshots
    if damage in {'old_receipt','old_checkpoint','new_repeat'}:
        assert result['relation'] in accepted,result
        assert result['new_suffix_indexes']==([len(before)] if damage=='new_repeat' else [])
        assert result['matched_pairs']==[{'old_index':i,'new_index':i} for i in range(len(before))]
    else:
        assert result['relation'] not in accepted,result


@pytest.mark.parametrize('corrected_at',['capture','ocr','never'])
def test_low_score_confirmed_receipt_keeps_the_one_recheck_budget(harness, corrected_at):
    runner,binding,target,api,bridge=setup_case(harness,short_receipt=True,corrected_at=corrected_at)
    result=runner._read_one_wechat_target(binding,target,enforce_read_targets=True,wait_for_brain=False)
    assert result['ok'] is (corrected_at!='never'),result
    assert bridge.captures==2
    assert bridge.stages==(['validate'] if corrected_at=='capture' else ['validate','ocr'])
    assert not bridge.sent_replies


@pytest.mark.parametrize('expanded', [False, True])
def test_final_media_reread_keeps_hc_old_text_without_spending_recheck(harness, monkeypatch, expanded):
    from chejin_worker_client.task_runner import FlowOutcomeAccumulator
    from chejin_worker_client.ui_lock import acquire_ui_lock
    runner,binding,target,api,bridge=setup_case(harness,corrected_at='never')
    read_id='confirmed-receipt-final-read'
    for entry in target.raw['identity_checkpoint']['recent_messages']:
        entry['origin_read_run_id']='old-read'
    target.raw['identity_checkpoint']['checkpoint_digest']=text_correspondence.checkpoint_digest(target.raw['identity_checkpoint'])
    baseline,errors=runner._align_initial_identity_frame(target=target,
        sidecar_payload=bridge.frame(fixed=True),read_run_id=read_id)
    assert not errors,errors
    baseline['authoritative_frame_source']='initial_read'
    baseline['observations']=runner._assign_sequence_new_suffix_identities(target=target,
        observations=baseline['observations'],evidence=baseline['sequence_alignment_evidence'],read_run_id=read_id)
    plan=runner._build_final_slot_incremental_plan(target=target,sidecar_payload=baseline,read_run_id=read_id)
    assert not plan['identity_errors'],plan['identity_errors']
    if expanded:
        # First frame lacks usable text; the original read-only expansion
        # restores context, but its mandatory fresh bottom frame still has
        # the ordinary one-character OCR loss. Do not fake a match decision.
        original_read=bridge.get_messages
        def read(**kwargs):
            value=original_read(**kwargs)
            if bridge.captures==1:
                value['observations'][-2]['content_clean']='无法还原的破损文本'
            if kwargs.get('history_mode')=='anchor_until_found':
                value=bridge.frame(fixed=True)
                value['history_load']={'ok':True,'anchor_found':True,'restored_to_latest':True}
            return value
        monkeypatch.setattr(bridge,'get_messages',read)
        monkeypatch.setattr(bridge,'recheck_text_bubbles',lambda **kwargs:{'ok':False,'reason':'no_complete_text_bubble'})
    lease=acquire_ui_lock(operation_type='c2_read',owner=read_id)
    try:
        result=runner._converge_current_screen_after_images(binding=binding,target=target,
            target_label=target.remark_code,sidecar_payload=baseline,lease=lease,
            action_cancel_requested=lease.cancel_requested,enforce_read_targets=True,
            flow_outcomes=FlowOutcomeAccumulator(origin_read_run_id=read_id))
    finally:
        lease.release()
    assert result['ok'],result
    assert bridge.captures==(3 if expanded else 1) and bridge.stages==[]
    assert [r['content_clean'] for r in result['payload']['observations']][-2:]==[
        '第1段回复，我核实下现有车源再回复您。','预算五万左右']
    assert not bridge.sent_replies
