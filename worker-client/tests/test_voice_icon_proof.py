"""Real icon and incident pixels; no mock of classification or image matching."""
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import voice_icons as icons
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar

FIXTURES = Path(__file__).parent / 'fixtures/voice_icons'


def row(text, box, role='customer'):
    a,b,c,d = box
    return dict(text=text,left=a,top=b,right=c,bottom=d,center_x=(a+c)/2,center_y=(b+d)/2,
                confidence=.999,avatar_alignment={'role':role})


def sample(role, scale=1):
    image = Image.open(FIXTURES/('customer_9s.png' if role=='customer' else 'self_2s.png')).convert('RGB')
    item = row('9"', [89,42,120,65], role) if role=='customer' else row('2"',[82,41,111,67],role)
    image = image.resize((round(image.width*scale),round(image.height*scale)))
    for key in ('left','top','right','bottom','center_x','center_y'):item[key]*=scale
    return image,item


@pytest.mark.parametrize('role',['customer','self'])
@pytest.mark.parametrize('scale',[.75,1,1.25,1.5,2])
def test_real_voice_icon_at_both_sides_and_scales(role,scale):
    image,item=sample(role,scale)
    annotated=icons.annotate_duration_rows([item],image,[0,0,*image.size])[0]
    proof=annotated['_voice_visual_evidence']
    assert proof['state']=='confirmed',proof
    assert proof['icon_score']>=icons.ICON_THRESHOLD
    assert proof['seconds']==(9 if role=='customer' else 2)
    assert proof['has_quote']
    assert sidecar.voice_duration_item_like(annotated)
    assert sidecar.message_group_is_untranscribed_voice_placeholder([annotated])


@pytest.mark.parametrize('text',['15w','15W','15万','15','007','9"','2','300','车型15W'])
def test_digits_or_seconds_quote_without_wave_are_text(text):
    image=Image.new('RGB',(300,100),(250,250,250));draw=ImageDraw.Draw(image)
    draw.rounded_rectangle((30,16,260,85),radius=10,fill=(238,238,240))
    font=ImageFont.load_default(size=24)
    draw.text((60,30),text,fill=(25,25,25),font=font)
    item=row(text,[60,30,220,63])
    annotated=icons.annotate_duration_rows([item],image,[0,0,*image.size])[0]
    assert not sidecar.voice_duration_item_like(annotated)
    assert icons.inspect_bubble(image,[30,16,261,86],'customer',[item])['state']!='confirmed'
    assert not sidecar.message_group_is_untranscribed_voice_placeholder([annotated])
    assert sidecar.strip_voice_duration_prefix_from_message_content(text+'\n油车',[annotated,row('油车',[60,90,120,112])])==(text+'\n油车',False)


def test_incident_15w_original_pixels_are_not_voice():
    image=Image.open(FIXTURES/'text_15w.png').convert('RGB')
    item=row('15w',[17,12,50,31])
    assert icons.inspect_bubble(image,[0,0,*image.size],'customer',[item])['state']=='not_voice'
    assert not sidecar.voice_duration_item_like(item)
    assert not sidecar.voice_duration_text_like('15w')


@pytest.mark.parametrize('change',['no_icon','wrong_role','wrong_side','other_bubble','no_avatar','ambiguous_avatar','stale_proof'])
def test_duration_cannot_borrow_unproven_icon(change):
    image,item=sample('customer')
    if change=='no_icon':ImageDraw.Draw(image).rectangle((45,30,80,75),fill=(238,238,240))
    elif change=='wrong_role':item['avatar_alignment']['role']='self'
    elif change=='wrong_side':item.update(left=23,right=45,center_x=34)
    elif change=='other_bubble':
        image2=Image.new('RGB',(400,200),(250,250,250));image2.paste(image,(0,0));image=image2
        ImageDraw.Draw(image).rounded_rectangle((20,120,250,185),radius=8,fill=(238,238,240))
        item.update(left=90,top=140,right=120,bottom=164,center_x=105,center_y=152)
    elif change=='no_avatar':item.pop('avatar_alignment')
    elif change=='ambiguous_avatar':item['avatar_alignment']['ambiguous']=True
    elif change=='stale_proof':
        item['_voice_visual_evidence']=icons.annotate_duration_rows([item],image,[0,0,*image.size])[0]['_voice_visual_evidence']
        image=Image.new('RGB',image.size,(250,250,250))
    annotated=icons.annotate_duration_rows([item],image,[0,0,*image.size])[0]
    assert not sidecar.voice_duration_item_like(annotated)


def test_ocr_confidence_and_legacy_marker_cannot_override_pixels():
    item=row('15',[10,10,35,30]);item['_voice_duration_region']=True
    assert not sidecar.voice_duration_item_like(item)
    image=Image.new('RGB',(80,80),'white')
    assert not sidecar.voice_duration_item_like(icons.annotate_duration_rows([item],image,[0,0,*image.size])[0])


def test_missing_quote_still_requires_actual_icon():
    image,item=sample('customer');item['text']='9'
    actual=icons.annotate_duration_rows([item],image,[0,0,*image.size])[0]
    assert sidecar.voice_duration_item_like(actual)
    assert not actual['_voice_visual_evidence']['has_quote']
