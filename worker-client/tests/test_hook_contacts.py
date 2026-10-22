import pytest
from chejin_worker_client.hook_contacts import resolve_friend_shortcode

CODE='CJMTX7YQ'
SELF='wxid_sales'

def resolve(data):return resolve_friend_shortcode(data,CODE,SELF)

def test_actual_friend_map_shape_uses_markname_on_same_object():
    result=resolve({'count_friend':603,'friend':{'wxid_target':{'account':'alias',
        'markname':'测试 '+CODE,'nickname':'客户','wxid':'wxid_target'}}})
    assert result.matching_objects==1 and result.candidate_wxid=='wxid_target'
    assert result.matched_remark_fields==('markname',)

@pytest.mark.parametrize('key',['Remark','remarks','remark_name','markname','MarkName'])
def test_known_remark_aliases(key):
    assert resolve({key:'测试 '+CODE,'wxid':'wxid_target'}).candidate_wxid=='wxid_target'

@pytest.mark.parametrize('data',[
    {'nickname':CODE,'wxid':'wxid_target'},
    {'markname':'X'+CODE+'Y','wxid':'wxid_target'},
    {'markname':CODE,'wxid':SELF},
    {'markname':CODE,'wxid':'123@chatroom'},
    {'markname':CODE},
    {'markname':CODE,'wxid':'wxid_target','username':'wxid_other'},
    {'markname':CODE,'nested':{'wxid':'wxid_target'}},
    [{'markname':CODE,'wxid':'wxid_target'},{'markname':CODE,'wxid':'wxid_target'}],
])
def test_ambiguous_non_remark_or_invalid_identity_cannot_be_candidate(data):
    assert resolve(data).candidate_wxid is None

def test_two_remark_fields_on_same_object_are_one_match():
    result=resolve({'markname':CODE,'remark':CODE,'wxid':'wxid_target'})
    assert result.matching_objects==1 and result.candidate_wxid=='wxid_target'

def test_identity_cannot_be_joined_from_separate_object():
    assert resolve([{'markname':CODE},{'wxid':'wxid_target'}]).candidate_wxid is None

@pytest.mark.parametrize('data',[None,'raw string',{'code':-1,'friend':{}}])
def test_response_error_or_shape_is_not_empty_success(data):
    with pytest.raises(ValueError):resolve(data)

def test_bounded_nesting():
    data={}
    for _ in range(66):data={'x':data}
    with pytest.raises(ValueError,match='LIMIT'):resolve(data)


@pytest.mark.parametrize('data,expected',[
 ({'count_friend':603,'friend':{'wxid_target':{'wxid':'wxid_target','markname':CODE}}},'matched'),
 ({'friend':{'wxid_target':{'wxid':'wxid_target','markname':'changed'}}},'remark_changed'),
 ({'friend':[{'wxid':'wxid_target','markname':''}]},'remark_changed'),
 ({'count_friend':0,'friend':{}},'unconfirmed'),
 ({'count_friend':1,'friend':{'wxid_other':{'wxid':'wxid_other','markname':CODE}}},'unconfirmed'),
 ([{'wxid':'wxid_target','nickname':CODE}],'unconfirmed'),
 ([{'wxid':'wxid_target','username':'wxid_other','markname':'changed'}],'unconfirmed'),
 ([{'wxid':'wxid_target','markname':CODE},{'wxid':'wxid_target','markname':'changed'}],'unconfirmed'),
 ([{'wxid':'wxid_target','markname':CODE},{'wxid':'wxid_other','markname':CODE}],'unconfirmed'),
 ([{'wxid':'wxid_target','markname':None}],'unconfirmed'),
])
def test_bound_contact_observation_never_infers_removal(data,expected):
 from chejin_worker_client.shared_rules import hook_contact_rules
 assert hook_contact_rules.inspect_bound_contact(data,CODE,SELF,'wxid_target')==expected
