"""Decode both observed native audio fields without assigning message identity."""
import re

VOICE_AUDIO_FIELDS = ('voice_hex', 'voice_data')
MAX_AUDIO_BYTES = 8 * 1024 * 1024


def has_voice_audio(message):
    return any(message.get(name) not in (None, '') for name in VOICE_AUDIO_FIELDS)


def voice_audio_bytes(message, *, max_audio_bytes=MAX_AUDIO_BYTES):
    """Empty fields mean pending; every populated copy must be valid and agree."""
    if type(max_audio_bytes) is not int or not 0 < max_audio_bytes <= MAX_AUDIO_BYTES:
        raise ValueError('HOOK_VOICE_LIMIT_INVALID')
    audio = None
    for name in VOICE_AUDIO_FIELDS:
        encoded = message.get(name)
        if encoded in (None, ''):
            continue
        if not isinstance(encoded, str):
            raise ValueError('HOOK_VOICE_AUDIO_INVALID')
        if len(encoded) > max_audio_bytes * 2:
            raise ValueError('HOOK_VOICE_AUDIO_TOO_LARGE')
        if not re.fullmatch(r'(?:[0-9a-fA-F]{2})+', encoded):
            raise ValueError('HOOK_VOICE_AUDIO_INVALID')
        current = bytes.fromhex(encoded)
        length = message.get('voice_len')
        if (type(length) not in (str, int) or str(length) != str(len(current))
                or not current.startswith(b'\x02#!SILK_V3')):
            raise ValueError('HOOK_VOICE_AUDIO_INVALID')
        if audio is not None and audio != current:
            raise ValueError('HOOK_VOICE_AUDIO_AMBIGUOUS')
        audio = current
    return audio
