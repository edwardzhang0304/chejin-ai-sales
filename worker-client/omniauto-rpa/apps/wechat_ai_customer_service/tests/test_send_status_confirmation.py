"""Formal confirm entry with controlled captured facts. No mouse/Windows claim."""
from copy import deepcopy
import pytest
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar

TEXT='好的，我帮您找一台适合日常代步的车。'

def row(identity,role,text):
    return dict(observation_id=identity,row_kind='text_bubble',sender_role=role,content_normalized=text)

BASE=[row('c1','customer','我想看看车')]
STATUS={
    'clear': {'state':'clear'},
    'missing': {'state':'unavailable','reason':'send_status_gutter_geometry_missing'},
    'low': {'state':'unavailable','reason':'send_status_marks_unclassified'},
    'failed': {'state':'blocked','reason':'red_failure'},
    'sending': {'state':'blocked','reason':'possible_sending'},
}

def snapshot(kind,*,baseline=BASE,text=TEXT,reply=False,damage=None):
    new=row('s1','self',text);new['send_status_evidence']=deepcopy(STATUS[kind])
    sequence=deepcopy(baseline)+[new]
    if reply:sequence.append(row('c2','customer','好的，谢谢'))
    result=dict(ok=True,validation={'confirmed_target':'CJABCDEF'},input_region={'has_visible_text':False},
                message_sequence=sequence,observations=deepcopy(sequence))
    if damage=='wrong_customer':result['ok']=False;result['validation']['confirmed_target']='CJANOTHER'
    if damage=='frame_failed':result['ok']=False
    if damage=='missing_input':result.pop('input_region')
    if damage=='draft':result['input_region']['has_visible_text']=True
    if damage=='old_bubble':result['message_sequence']=deepcopy(baseline)
    if damage=='wrong_text':new['content_normalized']='无关的消息'
    if damage=='two_self':sequence.append(row('s2','self',text))
    return result

def confirm(monkeypatch,frames,*,baseline=BASE,text=TEXT):
    captures=[]
    def capture(*a,**kw):
        captures.append(kw['label'])
        frame=frames[min(len(captures),len(frames)-1)]
        if isinstance(frame,Exception):raise frame
        return deepcopy(frame)
    monkeypatch.setattr(sidecar,'capture_send_fact_snapshot',capture)
    monkeypatch.setattr(sidecar.time,'sleep',lambda *_:None)
    monkeypatch.setattr(sidecar,'safe_send_trigger',lambda *a,**k:pytest.fail('confirmation cannot resend'))
    result=sidecar.confirm_reply_sent(1,target='CJABCDEF',text=text,exact=True,baseline_match_count=0,
        baseline_message_sequence=baseline,initial_snapshot=deepcopy(frames[0]))
    return result,captures

@pytest.mark.parametrize('kinds,accepted,attempt',[
    (['clear'],True,1), (['missing'],True,1), (['low'],True,1),
    (['sending','clear'],True,2), (['sending','missing','clear'],True,3),
    (['sending'],False,6), (['sending','missing'],False,6),
    (['failed','missing'],False,6), (['failed','clear'],False,6),
])
@pytest.mark.parametrize('reply',[False,True])
def test_status_layers_with_and_without_immediate_customer_reply(monkeypatch,kinds,accepted,attempt,reply):
    result,captures=confirm(monkeypatch,[snapshot(kind,reply=reply) for kind in kinds])
    assert result['ok'] is accepted
    assert len(captures)==attempt-1
    if accepted:
        assert result['attempt']==attempt
        assert result['confirmed_message']['following_customer_observation_ids']==(['c2'] if reply else [])
        assert result['confirmed_message']['send_status_evidence']==STATUS[kinds[-1]]
    else:
        assert result['error_code']=='SEND_RESULT_UNKNOWN' and len(result['attempts'])==6

@pytest.mark.parametrize('damage',['wrong_customer','frame_failed','draft','missing_input','old_bubble','wrong_text','two_self'])
@pytest.mark.parametrize('kind',['missing','clear'])
def test_status_never_substitutes_other_success_evidence(monkeypatch,damage,kind):
    result,captures=confirm(monkeypatch,[snapshot(kind,damage=damage)])
    assert not result['ok'] and result['error_code']=='SEND_RESULT_UNKNOWN'
    assert len(captures)==5

@pytest.mark.parametrize('first',['sending','failed'])
def test_capture_error_does_not_erase_counterevidence(monkeypatch,first):
    result,captures=confirm(monkeypatch,[snapshot(first),OSError('capture unavailable'),snapshot('missing')])
    assert not result['ok'] and len(captures)==5

@pytest.mark.parametrize('kinds',[['missing'],['sending','clear']])
def test_three_segments_each_use_new_baseline(monkeypatch,kinds):
    baseline=deepcopy(BASE)
    for n in range(3):
        text=f'这是第{n+1}段独立回复，我给您介绍一下。'
        frames=[snapshot(kind,baseline=baseline,text=text,reply=n==2) for kind in kinds]
        for frame in frames:
            frame['message_sequence'][len(baseline)]['observation_id']=f's{n+1}'
        result,captures=confirm(monkeypatch,frames,baseline=baseline,text=text)
        assert result['ok'] and result['attempt']==len(kinds)
        baseline=deepcopy(result['snapshot']['message_sequence'])
        assert sum(r['sender_role']=='self' for r in baseline)==n+1


def test_nonunique_alignment_rule_is_unchanged(monkeypatch):
    baseline=[row('a','customer','同一段'),row('b','customer','同一段')]
    result,_=confirm(monkeypatch,[snapshot('missing',baseline=baseline)],baseline=baseline)
    assert not result['ok']
