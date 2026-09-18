"""R4-A: exact 0.9.87 admission semantics, including its deliberate tolerances."""
import pytest
from apps.wechat_ai_customer_service.adapters import add_friend_layout as layout

GREETING='您好，我是车金顾问'
CODE='CJABCDEF'
BOUNDS={'verify_message':[0,0,300,90], 'remark_name':[0,100,300,200], 'remark_code':[0,100,300,200]}

def item(text, y, confidence=.99):
    return dict(text=text,confidence=confidence,left=10,top=y,right=250,bottom=y+15,center_x=130,center_y=y+7)

CODE_CASES=[
    ([('CJABCDEF',.99)], True), ([('CJABCDE1',.99)], True),
    ([('CJABCDEF',.90)], True), ([('CJABCDEF',.899)], False),
    ([('CJABCDEF|',.99)], True), ([('CJABCDEF9',.99)], False),
    ([('CJABCDEF||',.99)], False), ([('CJA|BCDEF',.99)], False),
    ([('CJABCD',.99)], False), ([('',.99)], False),
    ([('原来的昵称',.99)], False), ([('ＣＪＡＢＣＤＥＦ',.99)], False),
    ([('CJABCDEF',.99),('CJABCDE1',.5)], True),
    ([('CJABCDEF',.5),('CJABCDE1',.90)], True),
    ([('CJABCDEF',.99),('CJABCDE1',.90)], False),
    ([('CJABCDEF',.5),('CJABCDE1',.899)], False),
]

@pytest.mark.parametrize('fragments,accepted',CODE_CASES)
def test_copied_short_code_keeps_legacy_confidence_filter_order(fragments,accepted):
    rows=[item(GREETING,20)]+[item(text,110+20*i,score) for i,(text,score) in enumerate(fragments)]
    rows += [item('CJOUT123',220)]  # a second code outside the actual field is irrelevant
    result=layout.invite_form_field_verification(verify_message=GREETING,remark_name=CODE,remark_code=CODE,
        ocr_items=rows,field_bounds=BOUNDS)
    assert result['ok'] is accepted
    assert result['remark_code']['short_code_candidate_count']==sum(
        1 for text,score in fragments if layout.high_confidence_eight_char_code_visible([item(text,110,score)])['ok'])

@pytest.mark.parametrize('expected,fragments,accepted',[
    (GREETING,[GREETING],True), (GREETING,[GREETING+'，您好'],True),
    (GREETING,['您好，','我是车金顾问'],True), (GREETING,['您好我是车金'],False),
    ('顾问13812345678',['13812345678'],True), ('顾问13812345678',['13812345679'],False),
    ('',[''],False), ('您好',['其他'],False),
])
def test_greeting_legacy_contains_and_nonempty_digits_branch(expected,fragments,accepted):
    rows=[item(text,10+i*20) for i,text in enumerate(fragments)]
    rows.reverse()  # sort by field geometry, never caller/OCR list order
    rows.append(item(expected,250))
    result=layout.field_text_visible(expected,rows,bounds=BOUNDS['verify_message'])
    assert result['ok'] is accepted

@pytest.mark.parametrize('remark,code,observed,accepted',[
    ('客户CJABCDEF','CJABCDEF','客户CJABCDEF',True),
    ('客户CJABCDEF','CJABCDEF','CJABCDEX',False),
])
def test_non_copied_code_path_keeps_original_text_checks(remark,code,observed,accepted):
    assert layout.invite_form_field_verification(verify_message=GREETING,remark_name=remark,remark_code=code,
        ocr_items=[item(GREETING,20),item(observed,110)],field_bounds=BOUNDS)['ok'] is accepted
