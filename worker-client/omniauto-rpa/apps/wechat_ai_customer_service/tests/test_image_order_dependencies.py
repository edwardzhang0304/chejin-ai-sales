from copy import deepcopy
import pytest
from apps.wechat_ai_customer_service.adapters.image_order_dependencies import input_dependency_evidence


def brain_input():
    return {'target':{'conversation_id':'one'},'current_message':{'clean_text':'这款车现在多少钱','message_ids':['m1']},
            'conversation':{'history_text':'客户：我想买车'},'evidence':{'knowledge':{'product_master':{'price':8.88}}}}


def test_complete_input_has_versioned_bound_evidence_without_mutation():
    value=brain_input();before=deepcopy(value)
    proof=input_dependency_evidence(value)
    assert proof['complete'] and not proof['image_dependency'] and proof['message_ids']==['m1']
    assert proof['conversation_id']=='one' and len(proof['input_sha256'])==64 and value==before


@pytest.mark.parametrize('place',['current','history','knowledge','nested_visual','rag'])
def test_image_reference_or_evidence_anywhere_disqualifies(place):
    value=brain_input()
    if place=='current':value['current_message']['clean_text']='第一张的车多少钱'
    if place=='history':value['conversation']['history_text']='客户：上图是哪台车'
    if place=='knowledge':value['evidence']['knowledge']['product_master']['images']=['opaque-id']
    if place=='nested_visual':value['evidence']['knowledge']['detail']={'visual_source':{'id':'opaque-id'}}
    if place=='rag':value['evidence']['rag']={'hits':[{'content':'请见照片中车况'}]}
    assert input_dependency_evidence(value)['image_dependency'] is True


def test_missing_input_is_not_complete_evidence():
    assert input_dependency_evidence({})=={'version':1,'complete':False}
