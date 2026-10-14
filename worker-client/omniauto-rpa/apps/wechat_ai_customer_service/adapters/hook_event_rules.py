"""Strict provider XML interpretation; quoted old prose is never new input."""
import re
import xml.etree.ElementTree as ET


def root(value):
    if (not isinstance(value,str) or len(value.encode())>2097152
            or '<!DOCTYPE' in value.upper() or '<!ENTITY' in value.upper()):
        raise ValueError('HOOK_EVENT_XML_UNCONFIRMED')
    try:return ET.fromstring(value)
    except ET.ParseError:raise ValueError('HOOK_EVENT_XML_UNCONFIRMED') from None


def quote(value):
    node=root(value)
    app=node if node.tag=='appmsg' else node.find('appmsg')
    if app is None or app.findtext('type')!='57':return None
    text=app.findtext('title')
    reference=app.findtext('refermsg/svrid')
    if not text or not text.strip() or len(text)>20000 or not reference or not re.fullmatch(r'[1-9][0-9]{0,19}',reference):
        raise ValueError('HOOK_QUOTE_FIELDS_UNCONFIRMED')
    return {'new_text':text,'referenced_native_msg_id':reference}


def revoked(value):
    node=root(value)
    if node.tag!='sysmsg' or node.get('type')!='revokemsg':return None
    target=node.findtext('revokemsg/newmsgid')
    if not target or not re.fullmatch(r'[1-9][0-9]{0,19}',target):
        raise ValueError('HOOK_REVOKE_FIELDS_UNCONFIRMED')
    return target
