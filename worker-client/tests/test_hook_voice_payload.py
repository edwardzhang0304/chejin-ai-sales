"""Synthetic audio only; field compatibility must not weaken validation."""
import pytest
from chejin_worker_client.hook_voice_payload import voice_audio_bytes
from test_hook_facts import callback, incoming

AUDIO=b'\x02#!SILK_V3'+bytes(range(32))


@pytest.mark.parametrize('fields',[
    {'voice_hex':AUDIO.hex()}, {'voice_data':AUDIO.hex()},
    {'voice_hex':AUDIO.hex().upper(),'voice_data':AUDIO.hex()},
    {'voice_hex':AUDIO.hex(),'voice_data':''},
])
def test_both_formats_resolve_the_same_validated_audio(fields):
    message={**fields,'voice_len':str(len(AUDIO))}
    assert voice_audio_bytes(message)==AUDIO
    observed=callback(incoming(msgtype='34',msg=f'<msg><voicemsg length="{len(AUDIO)}"/></msg>',**message))[0]
    assert observed.disposition=='message_candidate'
    assert observed.media_stage=='audio_observed' and observed.audio_bytes==len(AUDIO)
    assert observed.content is None


@pytest.mark.parametrize('fields,code',[
    ({'voice_hex':AUDIO.hex(),'voice_data':(AUDIO[:-1]+b'x').hex()},'AMBIGUOUS'),
    ({'voice_hex':'bad','voice_data':AUDIO.hex()},'INVALID'),
    ({'voice_hex':False,'voice_data':AUDIO.hex()},'INVALID'),
    ({'voice_hex':[]},'INVALID'),
    ({'voice_hex':AUDIO.hex(),'voice_len':'0'},'INVALID'),
    ({'voice_hex':AUDIO.hex(),'voice_len':True},'INVALID'),
    ({'voice_hex':b'not_silk'.hex(),'voice_len':'8'},'INVALID'),
])
def test_bad_primary_or_conflicting_copy_is_never_hidden_by_fallback(fields,code):
    with pytest.raises(ValueError,match='HOOK_VOICE_AUDIO_'+code):
        voice_audio_bytes({'voice_len':str(len(AUDIO)),**fields})


def test_empty_audio_remains_pending_and_size_is_bounded_before_decode():
    assert voice_audio_bytes({'voice_hex':'','voice_data':None,'voice_len':'0'}) is None
    with pytest.raises(ValueError,match='TOO_LARGE'):
        voice_audio_bytes({'voice_hex':AUDIO.hex(),'voice_len':str(len(AUDIO))},max_audio_bytes=10)
