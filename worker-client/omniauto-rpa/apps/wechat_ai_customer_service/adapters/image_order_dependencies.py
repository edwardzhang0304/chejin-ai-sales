"""Conservative dependency evidence from runtime inputs, never an LLM assertion."""
import hashlib
import json
import re

_MEDIA_KEY = re.compile(r"image|photo|picture|vision|visual|media|attachment", re.I)
_MEDIA_TEXT = re.compile(r"图|照片|首张|第[一二三四五六七八九十0-9]+张|image|photo|picture|\.(?:png|jpe?g|webp|gif)\b", re.I)


def has_image_dependency(value):
    if isinstance(value, dict):
        return any((bool(item) and _MEDIA_KEY.search(str(key))) or has_image_dependency(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(has_image_dependency(item) for item in value)
    return bool(_MEDIA_TEXT.search(value)) if isinstance(value, str) else False


def input_dependency_evidence(brain_input):
    current, conversation, evidence = (brain_input.get(key) for key in ('current_message','conversation','evidence'))
    if not all(isinstance(value,dict) for value in (current,conversation,evidence)):
        return {'version':1, 'complete':False}
    # Inspect factual input channels; generic runtime/policy instructions are
    # not claims about a particular photograph. No truncation is used here.
    facts = {
        'current':{key:current.get(key) for key in ('clean_text','raw_text','referenced_context')},
        'conversation':{key:conversation.get(key) for key in ('context','history_text','summary','current_batch_text')},
        'evidence':{key:value for key,value in evidence.items()
                    if key not in {'safety','common_sense','audit_summary','authority_order'}},
    }
    encoded=json.dumps(facts,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str).encode()
    return {'version':1,'complete':True,'input_sha256':hashlib.sha256(encoded).hexdigest(),
            'conversation_id':str((brain_input.get('target') or {}).get('conversation_id') or ''),
            'message_ids':list(current.get('message_ids') or []),
            'image_dependency':has_image_dependency(facts)}
