"""Synthetic type-arbitration boundaries; no Windows or model claims."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'omniauto-rpa'))
from apps.wechat_ai_customer_service.optional_plugins.vision.capture import surface

@pytest.mark.parametrize('role',['self','customer'])
@pytest.mark.parametrize('overlap',[.12,.90])
def test_weak_opposite_lane_does_not_override_typed_text(role,overlap):
    candidate={'bounds':[460,220,760,520],'side':'customer' if role=='self' else 'self','text_overlap_ratio':overlap}
    text={'id':'typed','type':'text','message_type':'text','sender_role':role,'sender_role_source':'same_row_avatar',
          'sender_role_evidence':['avatar_row_structure_confirmed'],'content':'合成文字','bubble_rect':[480,240,740,500]}
    result=surface.image_candidates_without_reliable_typed_message_conflicts([candidate],[text],[])
    if overlap==.12:
        assert len(result)==1 and result[0]['candidate_verification_required']
    else: assert result==[]
    # An independent image beside the text must survive, regardless of role.
    away={**candidate,'bounds':[460,530,760,730]}
    assert surface.image_candidates_without_reliable_typed_message_conflicts([away],[text],[])==[away]


def test_untrusted_text_cannot_hide_image():
    candidate={'bounds':[460,220,760,520],'side':'self','text_overlap_ratio':.9}
    text={'type':'text','sender_role':'customer','content':'未确认文字','bubble_rect':[480,240,740,500]}
    assert surface.image_candidates_without_reliable_typed_message_conflicts([candidate],[text],[])==[candidate]
