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


def app_message(value):
    """Interpret a type-49 body without upgrading content failure to transport.

    The caller already owns identity/order validation. Unknown content is never
    empty text, a fabricated file, or a completed incoming customer message.
    """
    try:
        node = root(value)
        app = node if node.tag == 'appmsg' else node.find('appmsg')
        subtype = app.findtext('type') if app is not None else None
        if not subtype or not re.fullmatch(r'[0-9]{1,8}', subtype):
            return {'kind':'unconfirmed', 'reason_code':'HOOK_APP_CONTENT_UNCONFIRMED'}
        if subtype == '57':
            return {'kind':'quote', 'quote':quote(value), 'subtype':subtype}
        return {'kind':'file' if subtype == '6' else 'unsupported', 'subtype':subtype}
    except ValueError as exc:
        if str(exc) not in {'HOOK_EVENT_XML_UNCONFIRMED','HOOK_QUOTE_FIELDS_UNCONFIRMED'}:
            raise
        return {'kind':'unconfirmed', 'reason_code':'HOOK_APP_CONTENT_UNCONFIRMED',
                'parse_error':str(exc)}


def revoked(value):
    node=root(value)
    if node.tag!='sysmsg' or node.get('type')!='revokemsg':return None
    target=node.findtext('revokemsg/newmsgid')
    if not target or not re.fullmatch(r'[1-9][0-9]{0,19}',target):
        raise ValueError('HOOK_REVOKE_FIELDS_UNCONFIRMED')
    return target
