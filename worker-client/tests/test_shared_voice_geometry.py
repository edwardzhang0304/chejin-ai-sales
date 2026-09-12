"""Unchanged voice geometry through both retained public entrypoints."""
import pytest
from test_chat_viewport_boundary import s as sidecar
component={'left':100,'top':100,'right':200,'bottom':130}
layout={'valid':True,'message_viewport_bounds':[50,50,950,700]}
item={'left':120,'top':105,'right':150,'bottom':125,'center_x':135,'center_y':115,'text':'客户的问题'}
cases=[
    ('text_overlaps',component,[item],layout,True),
    ('no_text',component,[],layout,False),
    ('wechat_voice_duration',component,[{**item,'text':'12"'}],layout,False),
    ('seconds_in_plain_text',component,[{**item,'text':'12秒'}],layout,True),
    ('transcribe_button',component,[{**item,'text':'转文字'}],layout,False),
    ('outside_component',component,[{**item,'center_x':400}],layout,False),
    ('inside_expansion',component,[{**item,'center_x':207}],layout,True),
    ('outside_expansion',component,[{**item,'center_x':209}],layout,False),
    ('outside_viewport',component,[item],{**layout,'message_viewport_bounds':[400,50,950,700]},False),
    ('invalid_layout',component,[item],{**layout,'valid':False},False),
    ('invalid_component',{**component,'right':90},[item],layout,False),
    ('empty_text',component,[{**item,'text':''}],layout,False),
]

@pytest.mark.parametrize('name,comp,items,snapshot,expected',cases)
def test_voice_geometry_shared_by_customer_and_self(name,comp,items,snapshot,expected):
    actual=[fn(comp,items,(1000,800),layout_snapshot=snapshot) for fn in
            (sidecar.visual_customer_voice_component_overlaps_text,sidecar.visual_self_voice_component_overlaps_text)]
    assert actual == [expected,expected], (name,actual)
