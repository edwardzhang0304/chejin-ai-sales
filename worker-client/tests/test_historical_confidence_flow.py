"""Constructed HC vectors through the original Worker and physical guard gates."""
from copy import deepcopy
import pytest
import test_historical_alignment_flow as legacy
from test_c2_identity_gate_receipts import harness
from chejin_worker_client.task_runner import TaskRunner, _bind_worker_continuity_contract_to_send_guard
from chejin_worker_client.pre_send_checkpoint import compare_checkpoint_to_observations
from chejin_worker_client.c2_contract import c2_contract_v3
from chejin_worker_client.shared_rules import text_correspondence
from apps.wechat_ai_customer_service.adapters.send_interruption import _corresponding_sequences


def case(monkeypatch):
    monkeypatch.setattr(legacy,'TEXTS',['唯一开场','这款600Pro适合日常通勤，具体信息可以再看看。','唯一末句'])
    cp,before,current,target=legacy.setup_case()
    current[1]['content_clean']='这款600Pr0适合日常通勤，具体信息可以再看看。'
    cp['historical_match_policy']=c2_contract_v3()['text_correspondence_contract']['historical_match_policy']
    cp['checkpoint_digest']=text_correspondence.checkpoint_digest(cp)
    return cp,before,current,target


def test_initial_presend_sidecar_and_backend_use_the_same_hc_mapping(monkeypatch):
    cp,before,current,target=case(monkeypatch)
    runner=TaskRunner.__new__(TaskRunner)
    aligned,errors=runner._align_initial_identity_frame(target=target,
        sidecar_payload={'ok':True,'frame_id':'current','observations':current},read_run_id='test-read')
    assert not errors
    proof=aligned['sequence_alignment_evidence']['text_correspondence']
    assert proof['version']==2 and proof['candidate_count']==1
    frozen=legacy.frozen_checkpoint(cp)
    result=compare_checkpoint_to_observations(frozen,current,before_frame_id='original',after_frame_id='current',
        current_tail_complete=True,historical_checkpoint=cp)
    assert result['comparison_result']=='checkpoint_equal',result
    assert result['text_correspondence']['pairs']==proof['pairs']
    guard=_bind_worker_continuity_contract_to_send_guard(
        legacy.fixtures.production_send_context_guard(current,layout_ok=True),current,
        checkpoint=frozen,checkpoint_comparison=result,empty_welcome_baseline=False,historical_checkpoint=cp)
    sidecar=legacy.fixtures.production_sidecar_module()
    now=deepcopy(before)
    now.append(legacy.fixtures.TaskRunnerTest._ai_send_observation('new-question',sender_role='customer',content='预算改成3万'))
    now[-1]['bubble_rect']=[100,700,450,750]
    next_guard=legacy.fixtures.production_send_context_guard(now,layout_ok=True)
    check=sidecar.validate_send_context_guard(guard,next_guard,current_observations=now)
    decision=check['worker_continuity_decision']
    assert decision['relation']=='unique_tail_append',check
    assert decision['new_suffix_indexes']==[3]
    snapshot={'observations':now,'message_sequence':[{'observation_id':r['observation_id']} for r in now],
        'send_context_guard':next_guard}
    old,new=_corresponding_sequences(guard,snapshot,decision)
    assert old==new[:3]
    corrupted=deepcopy(snapshot);corrupted['observations'][1]['content_clean']='价格改成3万'
    with pytest.raises(ValueError):_corresponding_sequences(guard,corrupted,decision)


def test_policy_refresh_cannot_change_effective_version_or_policy(monkeypatch):
    from chejin_worker_client.historical_alignment import refreshed_correspondence
    cp,_,current,target=case(monkeypatch)
    runner=TaskRunner.__new__(TaskRunner)
    aligned,errors=runner._align_initial_identity_frame(target=target,
        sidecar_payload={'ok':True,'frame_id':'current','observations':current},read_run_id='test-read')
    assert not errors
    value={'evidence':{'observations':current,'sequence_alignment_evidence':aligned['sequence_alignment_evidence']}}
    updated=deepcopy(cp);updated['text_correspondence_context']['known_entities']=[{'kind':'person','value':'测试员'}]
    updated['checkpoint_digest']=text_correspondence.checkpoint_digest(updated)
    assert refreshed_correspondence(value,updated)
    updated['recent_messages'][1]['effective_text']['version']=1
    updated['recent_messages'][1]['effective_text']['correction_id']='corrected-version'
    updated['recent_messages'][1]['effective_comparison']={k:deepcopy(updated['recent_messages'][1].get(k))
        for k in ('normalized_content_hash','alignment_signature','business_projection')}
    updated['checkpoint_digest']=text_correspondence.checkpoint_digest(updated)
    assert refreshed_correspondence(value,updated) is None


@pytest.mark.parametrize('identity',['original','unproven','untranscribed'])
def test_capability_v2_preserves_completed_voice_legacy_rules(harness,monkeypatch,identity):
    original=legacy.fixtures.identity_checkpoint_for_facts
    def with_policy(*args,**kwargs):
        cp=original(*args,**kwargs)
        cp['historical_match_policy']=c2_contract_v3()['text_correspondence_contract']['historical_match_policy']
        return cp
    monkeypatch.setattr(legacy.fixtures,'identity_checkpoint_for_facts',with_policy)
    legacy.test_completed_voice_uses_same_text_rule_but_keeps_media_identity_gate(harness,identity)


def test_media_final_proof_uses_complete_frame_not_ingest_suffix(monkeypatch):
    from chejin_worker_client.historical_alignment import bind_final_frame_correspondence
    cp,_,current,target=case(monkeypatch)
    alignment={'pre_sequence_source':'action_frame','pre_frame_id':'before','post_frame_id':'after',
        'matched_pairs':[{'original_media_receipt':'unchanged'}]}
    payload={'observations':[], 'authoritative_evidence_observations':current,
        'sequence_alignment_evidence':alignment}
    frozen=deepcopy(payload)
    bind_final_frame_correspondence(target,payload)
    proof=payload['sequence_alignment_evidence']['text_correspondence']
    assert len(proof['pairs'])==3 and proof['pairs'][1]['scores']['score']>=9000
    assert payload['observations']==frozen['observations']
    assert payload['authoritative_evidence_observations']==frozen['authoritative_evidence_observations']
    assert payload['sequence_alignment_evidence']['matched_pairs']==alignment['matched_pairs']


def test_match_review_keeps_original_bodies_and_budget_out_of_logs(monkeypatch,tmp_path):
    import json
    from chejin_worker_client.historical_alignment import save_match_review
    from chejin_worker_client import storage,failure_evidence
    cp,_,current,target=case(monkeypatch)
    budget={'consumed':True,'kind':'text'}
    monkeypatch.setattr(storage,'load_c2_state',lambda key:budget)
    captured=[]
    monkeypatch.setattr(failure_evidence,'record_capture_failure',lambda *a,**k:captured.append(a))
    payload={'artifact_dir':str(tmp_path),'observations':current,'historical_match_diagnostics':{'accepted':True,'best_score':9600}}
    path=save_match_review(target,payload,read_run_id='read-1',result={'ok':True})
    saved=json.loads(open(path).read())
    assert saved['original_messages']==cp['recent_messages'] and saved['observations']==current
    assert saved['recheck_budget']==budget and saved['routing']['ok'] is True
    assert captured==[]
    payload['artifact_dir']=str(tmp_path/'not-created')
    assert save_match_review(target,payload,read_run_id='read-1',result={'ok':True}) is None
    assert len(captured)==1
