"""HC r6 formula and policy; identity/HTTP/PNG tests are separate."""
import hashlib
import json
import time
import pytest
from apps.wechat_ai_customer_service.adapters.text_correspondence import historical_confidence_scores, validate_historical_match_policy

def policy():
    p={'policy_id':'historical_text_identity_v2','score_scale':10000,'text_metric':'normalized_levenshtein_v1',
       'accept_threshold':9000,'minimum_margin':500,'auxiliary_mode':'diagnostic_only'}
    p['policy_digest']=hashlib.sha256(json.dumps(p,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return p

@pytest.mark.parametrize('a,b,expected',[
    ('600Pro','600Pr0',8333),('甲'*70+'600Pro','甲'*70+'600Pr0',9868),
    ('一般家用代步车型','般家用代步车型',8750),('这款车报价12.8万','这款车报价128万',9000),
    ('不买这款车可以再看看','要买这款车可以再看看',9000),
    ('ＡＢＣ １２３','abc123',10000),('这是第一行\n这是第二行','这是第一行这是第二行',10000),
    ('a'*10000,'b'*1001+'a'*8999,8999),('a'*10000,'b'*1000+'a'*9000,9000),
    ('a'*10000,'b'*999+'a'*9001,9001)])
def test_complete_message_score(a,b,expected):
    metrics={}
    assert historical_confidence_scores(a,b,policy=policy(),diagnostics=metrics)=={'text':expected,'score':expected}
    assert metrics['text']==expected and metrics['length']==max(len(a.replace(' ', '').replace('\n','')),len(b))

@pytest.mark.parametrize('a,b',[('','a'),('a',''),(' ','\n')])
def test_empty_cannot_be_a_fuzzy_old_message(a,b):
    with pytest.raises(ValueError):historical_confidence_scores(a,b,policy=policy())

@pytest.mark.parametrize('key,value',[('policy_id','historical_confidence_v1'),('accept_threshold',True),
    ('accept_threshold',9000.0),('minimum_margin','500'),('auxiliary_mode','voting'),('weights',{})])
def test_forged_policy_rejected_even_with_rehashed_digest(key,value):
    p=policy();p[key]=value
    p['policy_digest']=hashlib.sha256(json.dumps({k:v for k,v in p.items() if k!='policy_digest'},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with pytest.raises(ValueError):validate_historical_match_policy(p)

def test_long_edit_obeys_original_deadline():
    with pytest.raises(TimeoutError):historical_confidence_scores('甲'*1000,'乙'*1000,policy=policy(),deadline=time.monotonic()-1)
