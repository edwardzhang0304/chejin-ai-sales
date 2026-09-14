"""Top exterior evidence across pixel positions and DPI; not Windows UAT."""
from pathlib import Path
import sys

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'omniauto-rpa'))
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import frame_avatars as a


def scene(scale, gap, role, defect=''):
    left, top, right, bottom = [round(v*scale) for v in (100,80,600,480)]
    size=round(36*scale)
    x=left+round(20*scale) if role=='customer' else right-round(20*scale)-size
    y=top+gap
    if defect=='side_margin':
        x=left+1 if role=='customer' else right-size-1
    elif defect=='bottom_margin':
        y=bottom-size-1
    image=Image.new('RGB',(round(700*scale),round(550*scale)),(250,250,250))
    d=ImageDraw.Draw(image)
    box=(x,y,x+size-1,y+size-1)
    if defect=='open_shape':
        d.line((x,y,x,y+size-1,x+size-1,y+size-1), fill=(60,90,130), width=1)
    else:
        d.rectangle(box,fill=(60,90,130))
        d.rectangle((x+size//3,y+size//3,x+size//2,y+size//2),fill='white')
    bx=x+size+round(16*scale) if role=='customer' else x-round(156*scale)
    by=y+round(8*scale)
    bubble=[bx,by,bx+round(140*scale),by+round(24*scale)]
    d.rectangle((bubble[0],bubble[1],bubble[2]-1,bubble[3]-1),fill=(232,232,232))
    if defect=='attached':
        line_y=y+round(18*scale)
        endpoints=(x+size-1,bubble[0]) if role=='customer' else (bubble[2]-1,x)
        d.line((endpoints[0],line_y,endpoints[1],line_y),fill=(60,90,130),width=1)
    elif defect=='top_noise':
        # A separate pixel outside the avatar width cannot become a top
        # fragment, but invalidates a clean top exterior strip.
        d.point((x-2,top),fill=(60,90,130))
    layout={'valid':True,'dpi_scale':scale,'layout_snapshot_id':'margin',
            'frame_id':f'{scale}-{gap}-{role}-{defect}',
            'message_viewport_bounds':[left,top,right,bottom]}
    return image,layout,[x,y,x+size,y+size],bubble


@pytest.mark.parametrize('scale',[1,1.25,1.5,2,2.5])
@pytest.mark.parametrize('gap',range(7))
@pytest.mark.parametrize('role',['customer','self'])
def test_complete_top_avatar_and_touching_prefix(scale,gap,role):
    image,layout,bounds,bubble=scene(scale,gap,role)
    table=a.avatar_table(image,layout)
    evidence=a.role_details(image,layout,bubble)
    if gap==0:
        assert table['top_fragments']
        assert evidence['reason']=='partial_top_message_prefix'
        assert not evidence['role']
    else:
        assert not table['top_fragments']
        assert not table['unresolved'],table
        assert evidence['role']==role and evidence['state']=='confirmed',evidence
        assert evidence[role]['component_bounds']==bounds


@pytest.mark.parametrize('scale',[1,1.5,2])
@pytest.mark.parametrize('role',['customer','self'])
@pytest.mark.parametrize('defect',['attached','side_margin','bottom_margin','open_shape','top_noise'])
def test_uncertain_top_or_other_edges_still_reject(scale,role,defect):
    image,layout,bounds,bubble=scene(scale,1,role,defect)
    table=a.avatar_table(image,layout)
    assert not any(c['bounds']==bounds for c in table['components']),table
    evidence=a.role_details(image,layout,bubble)
    assert evidence['state']=='ambiguous' and not evidence['role'],evidence


@pytest.mark.parametrize('scale',[1,1.5,2])
@pytest.mark.parametrize('gap',[-10,-1,0])
def test_clipped_top_keeps_separate_complete_next_avatar(scale,gap):
    image,layout,_,_=scene(scale,gap,'customer')
    d=ImageDraw.Draw(image);size=round(36*scale)
    x=round(120*scale);y=round(240*scale)
    d.rectangle((x,y,x+size-1,y+size-1),fill=(60,90,130))
    bubble=[x+size+round(16*scale),y+round(8*scale),x+size+round(156*scale),y+round(32*scale)]
    table=a.avatar_table(image,layout)
    assert table['top_fragments']
    assert a.role_details(image,layout,bubble)['role']=='customer'
