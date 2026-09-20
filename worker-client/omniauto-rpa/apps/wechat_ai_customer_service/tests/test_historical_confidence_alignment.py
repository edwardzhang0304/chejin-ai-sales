"""Constructed HC sequence vectors. Integration and original PNGs are separate."""
from copy import deepcopy
import time

import pytest

from apps.wechat_ai_customer_service.tests.test_historical_text_alignment import checkpoint, frame
from apps.wechat_ai_customer_service.tests.test_historical_confidence_scores import policy
from apps.wechat_ai_customer_service.adapters.historical_text_alignment import build_correspondence, verify_correspondence
from apps.wechat_ai_customer_service.adapters.business_viewport_continuity import boundary_tokens_for_observations
from apps.wechat_ai_customer_service.adapters.text_correspondence import checkpoint_digest


def scenario(old=None, new=None):
    old = old or ['唯一开场', '这款600Pro适合日常通勤，具体信息可以再看看。', '唯一末句']
    new = new or ['唯一开场', '这款600Pr0适合日常通勤，具体信息可以再看看。', '唯一末句', '新的客户问题']
    cp, rows = checkpoint(old), frame(new)
    cp['historical_match_policy'] = policy()
    cp['checkpoint_digest'] = checkpoint_digest(cp)
    return cp, rows


def build(cp, rows, **kwargs):
    return build_correspondence(cp, rows, pre_frame_id='checkpoint:test', post_frame_id='current',
        new_boundary_tokens=boundary_tokens_for_observations(rows, committed_only=False), **kwargs)


def test_all_candidates_compete_without_filtering_low_scores_or_changing_facts():
    cp, rows = scenario()
    frozen = deepcopy((cp, rows));report = {}
    found = build(cp, rows, diagnostics=report)
    assert found and found['proof']['version'] == 2
    assert found['proof']['candidate_count'] == len(report['candidates']) == 1
    assert found['proof']['best_score'] >= 9000
    assert found['proof']['runner_up_score'] is None
    assert found['continuity']['new_suffix_indexes'] == [3]
    assert (cp, rows) == frozen
    assert verify_correspondence(found['proof'], cp, rows, pre_frame_id='checkpoint:test',
        post_frame_id='current', new_boundary_tokens=boundary_tokens_for_observations(rows, committed_only=False)) == found['continuity']


def test_multiple_nonexact_rows_and_short_model_text_are_not_v1_filtered():
    cp, rows = scenario(['支点甲','这款600Pro适合日常通勤，具体信息可以再看看。','支点乙','一般两厢适合通勤的车'],
        ['支点甲','这款600Pr0适合日常通勤，具体信息可以再看看。','支点乙','般两厢适合通勤的车','不要电车'])
    result = build(cp, rows)
    assert result and len([p for p in result['proof']['pairs'] if p['matched_by'] == 'confidence']) == 2
    assert result['continuity']['new_suffix_indexes'] == [4]


@pytest.mark.parametrize('suffix', ['这款600Pro适合日常通勤，具体信息可以再看看。', '600Plus', '预算3万', '不要了', '唯一末句'])
def test_new_suffix_is_never_removed_by_similarity(suffix):
    cp, rows = scenario(new=['唯一开场','这款600Pr0适合日常通勤，具体信息可以再看看。','唯一末句',suffix])
    result = build(cp, rows)
    if result is not None:
        assert result['continuity']['new_suffix_indexes'] == [3]
        assert all(p['new_index'] < 3 for p in result['proof']['pairs'])
    # An exact anchor repeated in the suffix may reduce confidence; refusal is
    # valid, silently treating the new occurrence as the old ID is not.


@pytest.mark.parametrize('change', ['score','anchor','drop_runner','shape','policy','id','frame','version'])
def test_verifier_recomputes_every_claim(change):
    cp, rows = scenario();proof = build(cp, rows)['proof']
    if change == 'score':proof['pairs'][1]['scores']['score'] += 1
    if change == 'anchor':proof['pairs'][1]['anchor_pairs'] = []
    if change == 'drop_runner':proof.update(candidate_count=2,runner_up_score=0,margin=proof['best_score'])
    if change == 'shape':proof['pairs'][1]['scores']['geometry'] = 10000
    if change == 'policy':proof['policy_digest'] = 'a'*64
    if change == 'id':proof['pairs'][1]['source_message_key'] = 'another-customer'
    if change == 'frame':proof['post_frame_id'] = 'another-frame'
    if change == 'version':proof['pairs'][1]['effective_text_version'] += 1
    with pytest.raises(ValueError):
        verify_correspondence(proof, cp, rows, pre_frame_id='checkpoint:test', post_frame_id='current',
            new_boundary_tokens=boundary_tokens_for_observations(rows, committed_only=False))


@pytest.mark.parametrize('feature',[None,{},'invalid',{'dpi_milli':1000}, {'ink64_bits_base64':'not-an-image'}])
def test_optional_old_features_never_vote_or_block(feature):
    cp,rows=scenario();expected=build(cp,rows)
    for e in cp['recent_messages']:e['historical_identity_features']=feature
    for r in rows:r['historical_identity_features']=feature
    cp['checkpoint_digest']=checkpoint_digest(cp)
    actual=build(cp,rows)
    assert actual['proof']['best_score']==expected['proof']['best_score']
    assert actual['continuity']==expected['continuity']


def test_deadline_cannot_accept_a_partially_enumerated_winner():
    cp, rows = scenario()
    with pytest.raises(TimeoutError):build(cp, rows, deadline=time.monotonic()-1)


def test_same_exact_ambiguous_boundary_is_not_rescued_by_hc():
    cp, rows = scenario(['好的','好的'], ['好的','好的','好的'])
    assert build(cp, rows) is None


def test_role_conflict_cannot_be_outweighed_by_matching_text_and_pixels():
    cp, rows = scenario()
    for row in rows:row['sender_role'] = 'self'
    assert build(cp, rows) is None


def test_v1_frozen_proof_keeps_original_interpretation_after_capability_upgrade():
    from apps.wechat_ai_customer_service.adapters.historical_text_alignment import checkpoint_for_proof_version
    from apps.wechat_ai_customer_service.tests.test_historical_text_alignment import build as old_build
    cp=checkpoint(['开场锚点','一般两厢，平时接送孩子','结束锚点'])
    rows=frame(['开场锚点','般两厢，平时接送孩子','结束锚点'])
    original=deepcopy(cp);proof=old_build(cp,rows)['proof']
    cp['historical_match_policy']=policy()
    for entry in cp['recent_messages']:entry['historical_identity_features']=None
    cp['checkpoint_digest']=checkpoint_digest(cp)
    projected=checkpoint_for_proof_version(cp,1)
    assert projected==original
    assert old_build(projected,rows)['proof']==proof
    assert checkpoint_for_proof_version(cp,2) is cp


@pytest.mark.parametrize('difference',[999,1000,1001])
def test_real_competing_boundaries_keep_low_runner_and_margin(difference):
    from apps.wechat_ai_customer_service.adapters.message_viewport_projection import normalized_business_message_sequence
    # Two legal old suffixes. Candidate zero scores 9500; candidate two scores
    # 9001/9000/8999. No fake scorer or constructed aggregate score is injected.
    x='x'*500+'a'*9500;y='y'*500+'a'*9500;z='z'*500+'a'*9500
    a='b'*difference+'q'*(10000-difference);b='c'*difference+'q'*(10000-difference)
    cp,rows=scenario([x,a,y,b],[y,a,z,b,'新的客户问题'])
    old_rows=frame([x,a,y,b])
    for i,r in enumerate(old_rows):r['sender_role']='customer' if i%2==0 else 'self'
    for i,r in enumerate(rows):r['sender_role']='customer' if i%2==0 else 'self'
    projections=normalized_business_message_sequence(old_rows,message_viewport_bounds=None)
    tokens=boundary_tokens_for_observations(old_rows,committed_only=False)
    for i,e in enumerate(cp['recent_messages']):
        e.update(sender_role=old_rows[i]['sender_role'],business_projection=projections[i],strong_boundary_tokens=list(tokens[i]))
    cp['checkpoint_digest']=checkpoint_digest(cp)
    report={};result=build(cp,rows,diagnostics=report)
    assert len(report['candidates'])==2,report
    assert report['best_score']==9500 and report['runner_up_score']==10000-difference
    assert report['margin']==difference-500
    assert bool(result)==(difference>=1000)
    if result:assert result['continuity']['new_suffix_indexes']==[4]


@pytest.mark.parametrize('case',['same','voice_space','voice_bounded','wrong_voice','voice_unfinished','wrong_image','new_same_voice','new_same_image','role','media_order'])
def test_mixed_media_uses_original_identity_sequence_not_text_score(case):
    from apps.wechat_ai_customer_service.adapters.message_viewport_projection import normalized_business_message_sequence
    old=frame(['唯一开始支点','一般两厢，平时接送孩子','唯一中间支点','[图片]',
        '这款600Pro适合日常通勤，具体信息可以再看看。','唯一末尾支点'])
    old[1].update(message_type='voice',row_kind='voice_transcript',voice_state='transcribed',
        voice_duration='5',native_source_message_id='voice-original')
    old[3].update(message_type='image',row_kind='image_bubble',native_source_message_id='image-original',item_state='completed')
    cp=checkpoint([r['content_clean'] for r in old]);cp['historical_match_policy']=policy()
    projection=normalized_business_message_sequence(old,message_viewport_bounds=None)
    tokens=boundary_tokens_for_observations(old,committed_only=False)
    for i,e in enumerate(cp['recent_messages']):
        e.update(message_type=old[i]['message_type'],business_projection=projection[i],
            native_source_message_id=old[i].get('native_source_message_id'),strong_boundary_tokens=list(tokens.get(i,[])))
    rows=deepcopy(old);rows[4]['content_clean']=rows[4]['content_clean'].replace('Pro','Pr0')
    if case=='voice_space':rows[1]['content_clean']='一般两厢，平时\n 接送孩子'
    if case=='voice_bounded':rows[1]['content_clean']='般两厢，平时接送孩子'
    if case=='wrong_voice':rows[1]['native_source_message_id']='another-voice'
    if case=='wrong_image':rows[3]['native_source_message_id']='another-image'
    if case=='voice_unfinished':rows[1].update(row_kind='voice_bubble',voice_state='untranscribed',content_clean='[语音]')
    if case=='role':rows[1]['sender_role']='self'
    if case=='media_order':
        rows[1],rows[3]=rows[3],rows[1]
        rows[1]['bubble_rect'],rows[3]['bubble_rect']=rows[3]['bubble_rect'],rows[1]['bubble_rect']
    if case in {'new_same_voice','new_same_image'}:
        n=deepcopy(rows[1 if case=='new_same_voice' else 3]);n['observation_id']='new-media'
        n['native_source_message_id']='a-new-message-even-with-same-content'
        n['bubble_rect']=[10,400,300,440];rows.append(n)
    else:rows+=frame(['客户新话'])
    rows[-1]['observation_id']='last-new';rows[-1]['bubble_rect']=[10,440,300,470]
    cp['checkpoint_digest']=checkpoint_digest(cp)
    frozen=deepcopy((cp,rows));result=build(cp,rows)
    if case in {'wrong_voice','wrong_image','voice_unfinished','role','media_order'}:
        assert result is None
    else:
        assert result,result
        assert result['continuity']['new_suffix_indexes']==[6]
        assert [p['old_index'] for p in result['proof']['pairs']]==[0,2,4,5]
        assert verify_correspondence(result['proof'],cp,rows,pre_frame_id='checkpoint:test',post_frame_id='current',
            new_boundary_tokens=boundary_tokens_for_observations(rows,committed_only=False))==result['continuity']
    assert (cp,rows)==frozen


@pytest.mark.parametrize('difference',[999,1000,1001])
def test_threshold_is_applied_per_message_with_only_one_anchor(difference):
    old='a'*10000;new='b'*difference+'a'*(10000-difference)
    cp,rows=scenario(['唯一支点',old],['唯一支点',new,'客户新话'])
    report={};result=build(cp,rows,diagnostics=report)
    assert report['best_score']==10000-difference
    assert bool(result)==(difference<=1000)
    metrics=report['candidates'][0]['text_metrics'][0]
    assert (metrics['length'],metrics['edit_distance'],metrics['text'])==(10000,difference,10000-difference)
    if result:assert result['continuity']['new_suffix_indexes']==[2]


def test_score_without_any_old_identity_basis_is_insufficient():
    cp,rows=scenario(['abcdefghij'],['abcdefghiX','客户新话'])
    assert build(cp,rows) is None
