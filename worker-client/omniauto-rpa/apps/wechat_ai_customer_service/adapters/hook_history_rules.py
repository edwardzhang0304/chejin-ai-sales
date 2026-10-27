"""Finite native-history proof shared by Worker and backend, no SQL execution.

The caller rereads the authorized range; two equal bounded scans certify only
this observation, never future server delivery. Page/callback order is not a
business order. Only the verified one-database text scope is admitted here.
"""
from datetime import datetime, timezone
import re
from . import hook_contract_rules as rules

FIELDS = {'localId','CreateTime','Sequence','MsgServerSeq','MsgSequence','MsgSvrID','Type','IsSender','StrTalker','StrContent'}
NUMBERS = FIELDS - {'StrTalker','StrContent'}
MAX_ROWS = 10000
OUTGOING_ID_PENDING = 'HOOK_HISTORY_OUTGOING_ID_PENDING'
# Only successfully validated coverage can produce these incomplete states.
COVERAGE_PENDING_REASONS = frozenset({
    'HOOK_HISTORY_CROSS_DATABASE_ORDER_UNVERIFIED', 'HOOK_REVOKE_FIELDS_UNCONFIRMED',
    'HOOK_NATIVE_MEDIA_OR_SYSTEM_PENDING', 'HOOK_HISTORY_BOUNDARY_SECOND_UNRESOLVED',
    'HOOK_HISTORY_ORDER_UNRESOLVED', 'HOOK_HISTORY_TEXT_UNRESOLVED', OUTGOING_ID_PENDING,
})



def retained_send_history_ready(result):
    """Only original receipt reconciliation; this never authorizes a new send."""
    return (result.get('history_ready') is True
            or result.get('reason_code') == 'HOOK_NATIVE_SEND_ACTOR_PENDING'
            or (result.get('reconciliation_only') is True
                and result.get('recovery_settlement_allowed') is True
                and result.get('reason_code') in COVERAGE_PENDING_REASONS))


class HistoryIdentityError(ValueError):
    def __init__(self, row, database, *, duplicate):
        super().__init__('HOOK_HISTORY_MESSAGE_ID_UNRESOLVED')
        self.diagnostic_details = {
            'identity_failure': 'duplicate_positive_id' if duplicate else 'zero_id_not_outgoing_text',
            'database': database,
            **{k: row[k] for k in ('localId', 'MsgSvrID', 'Type', 'IsSender')},
        }


def unresolved_outgoing(row):
    # A local text attempt without a server ID is evidence, never a message
    # identity or a successful send. Incoming/media IDs remain strict.
    return row['Type'] == '1' and row['IsSender'] == '1' and row['MsgSvrID'] == '0'


# WeChat 3.9 stores this acceptance notification as an incoming Type=1
# message. Preserve its raw row/receipt, but do not treat it as customer intent.
FRIEND_ACCEPTANCE_NOTICE = '我通过了你的朋友验证请求，现在我们可以开始聊天了'


def is_friend_acceptance_notice(row):
    if row['Type'] != '1' or row['IsSender'] != '0' or not isinstance(row['StrContent'],str):
        return False
    text = re.sub(r'\s+', '', row['StrContent']).rstrip('。.!！').replace(',', '，')
    return text == FRIEND_ACCEPTANCE_NOTICE


def is_friend_verification_required_notice(row):
    """Only WeChat's incoming system template, never quoted customer text."""
    if row['Type'] != '10000' or row['IsSender'] != '0' or not isinstance(row['StrContent'], str):
        return False
    return re.fullmatch(
        r'[^<>\r\n]{1,128}开启了朋友验证，你还不是他（她）(?:的)?朋友。'
        r'请先发送朋友验证请求，对方验证通过后，才能聊天。'
        r'(?:<a href="[^"<>\r\n]{1,512}">发送朋友验证</a>)?',
        row['StrContent'].strip(),
    ) is not None


def is_hash(value):
    return isinstance(value,str) and re.fullmatch(r'[a-f0-9]{64}',value) is not None


def integer(value):
    if not isinstance(value,str) or not re.fullmatch(r'0|[1-9][0-9]{0,19}',value):
        raise ValueError('HOOK_HISTORY_INTEGER_INVALID')
    return int(value)


def utc(value):
    if not isinstance(value,str) or not value.endswith('+00:00'):
        raise ValueError('HOOK_HISTORY_UTC_REQUIRED')
    result=datetime.fromisoformat(value)
    if result.tzinfo!=timezone.utc:raise ValueError('HOOK_HISTORY_UTC_REQUIRED')
    return result


def validate_scan(scan, peer, since):
    if not isinstance(scan,dict) or set(scan)!={'catalog_fingerprint','databases','observed_at'}:
        raise ValueError('HOOK_HISTORY_SCAN_INVALID')
    utc(scan['observed_at'])
    if not is_hash(scan['catalog_fingerprint']):raise ValueError('HOOK_HISTORY_CATALOG_INVALID')
    databases=scan['databases']
    if not isinstance(databases,list) or not 1<=len(databases)<=256:
        raise ValueError('HOOK_HISTORY_CATALOG_INVALID')
    names=set(); all_rows=[]; identities={}; active=[]
    for db in databases:
        if not isinstance(db,dict) or set(db)!={'name','generation','upper_local_id','row_count','count_after','pages'}:
            raise ValueError('HOOK_HISTORY_DATABASE_INVALID')
        name=db['name']
        if not isinstance(name,str) or not re.fullmatch(r'MSG(?:0|[1-9][0-9]*)\.db',name) or name in names:
            raise ValueError('HOOK_HISTORY_DATABASE_INVALID')
        names.add(name)
        if not is_hash(db['generation']):raise ValueError('HOOK_HISTORY_GENERATION_INVALID')
        upper,count,after=map(integer,[db['upper_local_id'],db['row_count'],db['count_after']])
        if upper>2**63-1:raise ValueError('HOOK_HISTORY_BOUND_INVALID')
        if count!=after or count>MAX_ROWS or (count==0)!=(upper==0):
            raise ValueError('HOOK_HISTORY_BOUND_CHANGED')
        pages=db['pages']
        if not isinstance(pages,list) or len(pages)>MAX_ROWS or (count>0 and not pages):
            raise ValueError('HOOK_HISTORY_PAGES_INVALID')
        local=seen=0; rows=[]
        for page in pages:
            if not isinstance(page,list) or not 1<=len(page)<=200:
                raise ValueError('HOOK_HISTORY_PAGE_INVALID')
            for row in page:
                if not isinstance(row,dict) or set(row)!=FIELDS:raise ValueError('HOOK_HISTORY_ROW_INVALID')
                values={key:integer(row[key]) for key in NUMBERS}
                if not local<values['localId']<=upper or row['StrTalker']!=peer or values['CreateTime']<since or values['CreateTime']>int(utc(scan['observed_at']).timestamp()):
                    raise ValueError('HOOK_HISTORY_ROW_SCOPE_INVALID')
                if values['IsSender'] not in {0,1} or row['StrContent'] is not None and not isinstance(row['StrContent'],str):
                    raise ValueError('HOOK_HISTORY_ROW_INVALID')
                if row['StrContent'] is not None and len(row['StrContent'])>20000:
                    raise ValueError('HOOK_HISTORY_CONTENT_TOO_LARGE')
                local=values['localId'];seen+=1
                if values['Type'] in {1,3,34,49}:
                    if (not values['MsgSvrID'] and not unresolved_outgoing(row)) or (values['MsgSvrID'] and row['MsgSvrID'] in identities):
                        raise HistoryIdentityError(row, name, duplicate=bool(values['MsgSvrID']))
                    if values['MsgSvrID']:identities[row['MsgSvrID']]=row
                rows.append({**row,'database':name,'database_generation':db['generation']})
        if seen!=count or local!=upper:raise ValueError('HOOK_HISTORY_PAGINATION_INCOMPLETE')
        if rows:active.append(name)
        all_rows.extend(rows)
        if len(all_rows)>MAX_ROWS:raise ValueError('HOOK_HISTORY_RANGE_TOO_LARGE')
    if rules.digest([[d['name'],d['generation']] for d in databases])!=scan['catalog_fingerprint']:
        raise ValueError('HOOK_HISTORY_CATALOG_DIGEST_MISMATCH')
    return all_rows,active


def validate_coverage(payload, peer, since):
    base={'schema_version','since_create_time','scans','local_pending_count'}
    if not isinstance(payload,dict) or set(payload) not in (base,base|{'excluded_pre_invite_metadata'}):
        raise ValueError('HOOK_HISTORY_COVERAGE_INVALID')
    if payload['schema_version'] not in {'hook-history-coverage-v1','hook-history-coverage-v2'} or payload['since_create_time']!=str(since):
        raise ValueError('HOOK_HISTORY_RANGE_MISMATCH')
    if type(payload['local_pending_count']) is not int or payload['local_pending_count']!=0:
        raise ValueError('HOOK_HISTORY_LOCAL_PENDING')
    scans=payload['scans']
    if not isinstance(scans,list) or len(scans)!=2:raise ValueError('HOOK_HISTORY_TWO_SCANS_REQUIRED')
    first,active=validate_scan(scans[0],peer,since); second,other=validate_scan(scans[1],peer,since)
    if utc(scans[1]['observed_at'])<utc(scans[0]['observed_at']):raise ValueError('HOOK_HISTORY_OBSERVATION_ORDER_INVALID')
    if scans[0]['catalog_fingerprint']!=scans[1]['catalog_fingerprint'] or first!=second or active!=other:
        raise ValueError('HOOK_HISTORY_CHANGED_DURING_READ')
    ready=True; reason=None
    if len(active)>1:ready=False;reason='HOOK_HISTORY_CROSS_DATABASE_ORDER_UNVERIFIED'
    semantic=[]; media=[]; all_semantic=[];revoked_ids=[];quote_links={};notice_ids=[]; last_sequence=last_msg_sequence=last_time=-1
    media_enabled=payload['schema_version']=='hook-history-coverage-v2'
    # localId is only pagination. Reconstruct text order with the verified
    # Sequence field, then require agreement with MsgSequence and CreateTime.
    ordered=sorted(second,key=lambda r:integer(r['Sequence'])) if len(active)<=1 else second
    for row in ordered:
        kind=integer(row['Type'])
        if unresolved_outgoing(row):
            continue  # Preserve in rows; never admit to semantic/actor/AI facts.
        if is_friend_acceptance_notice(row):
            notice_ids.append(row['MsgSvrID'])
            continue
        if media_enabled and kind in {10000,10002}:
            from .hook_event_rules import revoked
            body=row['StrContent']
            if isinstance(body,str) and body.lstrip().startswith('<'):
                target=revoked(body)
                if target:revoked_ids.append(target)
            if kind==10000:
                if integer(row['MsgSvrID'])>0:notice_ids.append(row['MsgSvrID'])
                continue
            if not isinstance(body,str) or not body.lstrip().startswith('<') or not revoked(body):
                ready=False;reason=reason or 'HOOK_REVOKE_FIELDS_UNCONFIRMED'
            continue
        if kind==10000:continue
        if media_enabled and kind==49:
            from .hook_event_rules import quote
            parsed=quote(row['StrContent'])
            if parsed is None:
                ready=False;reason=reason or 'HOOK_NATIVE_MEDIA_OR_SYSTEM_PENDING';continue
            quote_links[row['MsgSvrID']]=parsed
        if kind!=1 and not (media_enabled and kind in {3,34,49}):
            ready=False;reason=reason or 'HOOK_NATIVE_MEDIA_OR_SYSTEM_PENDING';continue
        if integer(row['CreateTime'])==since:
            ready=False;reason=reason or 'HOOK_HISTORY_BOUNDARY_SECOND_UNRESOLVED'
        sequence,msg_sequence,created=map(integer,[row['Sequence'],row['MsgSequence'],row['CreateTime']])
        if not (sequence>0 and msg_sequence>0 and sequence>last_sequence and msg_sequence>last_msg_sequence and created>=last_time):
            ready=False;reason=reason or 'HOOK_HISTORY_ORDER_UNRESOLVED'
        last_sequence,last_msg_sequence,last_time=sequence,msg_sequence,created
        if kind in {1,49}:
            if not isinstance(row['StrContent'],str) or not row['StrContent']:
                ready=False;reason=reason or 'HOOK_HISTORY_TEXT_UNRESOLVED'
            semantic.append(row)
        else:media.append(row)
        all_semantic.append(row)
    # Systemized former rows and explicit revoke targets are removed from the
    # active context, while the raw capture and earlier MessageEvent survive.
    revoked_ids=list(dict.fromkeys(revoked_ids))
    all_semantic=[row for row in all_semantic if row['MsgSvrID'] not in revoked_ids]
    semantic=[row for row in semantic if row['MsgSvrID'] not in revoked_ids]
    media=[row for row in media if row['MsgSvrID'] not in revoked_ids]
    excluded=[]
    metadata=payload.get('excluded_pre_invite_metadata',[])
    if not isinstance(metadata,list) or len(metadata)>MAX_ROWS:raise ValueError('HOOK_HISTORY_EXCLUSION_INVALID')
    generations={d['name']:d['generation'] for d in scans[1]['databases']}
    included={r['MsgSvrID'] for r in all_semantic}
    for item in metadata:
        if (not media_enabled or not isinstance(item,dict)
                or set(item)!={'native_msg_id','create_time','database','database_generation'}
                or not integer(item['native_msg_id']) or integer(item['create_time'])>=since
                or generations.get(item['database'])!=item['database_generation']
                or item['native_msg_id'] in included or item['native_msg_id'] in excluded):
            raise ValueError('HOOK_HISTORY_EXCLUSION_INVALID')
        excluded.append(item['native_msg_id'])
    pending_outgoing = [row for row in second if unresolved_outgoing(row)]
    if pending_outgoing:
        ready=False;reason=reason or OUTGOING_ID_PENDING
    return {'rows':second,'text_rows':semantic,'media_rows':media,'semantic_rows':all_semantic,'ready':ready,'reason_code':reason,
            'unresolved_outgoing_rows':pending_outgoing,
            'catalog_fingerprint':scans[1]['catalog_fingerprint'],'observed_at':scans[1]['observed_at'],
            'friend_verification_notices':[r for r in second if is_friend_verification_required_notice(r)],
            'excluded_native_ids':excluded,'revoked_native_ids':revoked_ids,'system_notice_native_ids':notice_ids,'quote_links':quote_links}
