"""Small native evidence index for the original incident exporter.

No raw callback, message body, audio, friend list, database, token or phone is
included. Counts and scope/parent hashes diagnose persistence and isolation.
"""
import hashlib,json,re,math
from .hook_active_calls import CALL_KINDS
from . import storage


def snapshot():
    result={'schema_version':'hook-diagnostic-summary-v1','raw_content_included':False,
        'audio_included':False,'contact_list_included':False,'native_database_included':False}
    with storage.db_connection() as conn:
        names={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'hook_raw_inbox' in names:
            value=conn.execute('SELECT COUNT(*),COALESCE(MAX(receive_seq),0),COALESCE(SUM(length(raw_body)),0) FROM hook_raw_inbox').fetchone()
            result['inbox']={'rows':value[0],'receive_seq_high_water':value[1],'body_bytes':value[2]}
        if 'hook_history_pages' in names:
            rows=conn.execute('SELECT COUNT(*),COALESCE(MAX(history_seq),0),COALESCE(SUM(length(raw_body)),0) FROM hook_history_pages').fetchone()
            result['history']={'pages':rows[0],'history_seq_high_water':rows[1],'body_bytes':rows[2]}
        if 'hook_retired_observations' in names:
            result['retired_raw_rows']=conn.execute('SELECT COUNT(*) FROM hook_retired_observations').fetchone()[0]
        if 'hook_observation_projection' in names:
            result['projection']={'rows':conn.execute('SELECT COUNT(*) FROM hook_observation_projection').fetchone()[0]}
        if 'hook_delivery_outbox' in names:
            result['delivery_states']={row[0]:row[1] for row in conn.execute('SELECT status,COUNT(*) FROM hook_delivery_outbox GROUP BY status')}
        if 'hook_voice_attempts' in names:
            result['voice_states']={row[0]:row[1] for row in conn.execute('SELECT state,COUNT(*) FROM hook_voice_attempts GROUP BY state')}
            result['voice_parents']=[{'parent_sha256':hashlib.sha256(str(row['native_msg_id']).encode()).hexdigest(),
                'scope_sha256':hashlib.sha256((row['self_wxid']+row['account_epoch']+row['process_generation']).encode()).hexdigest(),
                'state':row['state'],'audio_sha256':row['audio_sha256'],'error_code':row['error_code'] if isinstance(row['error_code'],str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',row['error_code']) else None}
                for row in conn.execute('SELECT * FROM hook_voice_attempts ORDER BY attempted_at DESC LIMIT 20')]
        if 'local_logs' in names:
            result['recent_local_faults']=[]
            for row in conn.execute("SELECT error_code,metadata FROM local_logs WHERE event='hook_connection_fault' ORDER BY created_at DESC LIMIT 8"):
                try:meta=json.loads(row['metadata'] or '{}')
                except (ValueError,TypeError):continue
                if not isinstance(meta,dict):continue
                code=row['error_code']
                result['recent_local_faults'].append({
                    'error_code':code if isinstance(code,str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',code) else None,
                    'operation':meta.get('local_operation') if isinstance(meta.get('local_operation'),str) and meta['local_operation'] in CALL_KINDS else None,
                    'transport_kind':meta.get('transport_kind') if isinstance(meta.get('transport_kind'),str) and meta['transport_kind'] in {'timeout','connection_error','request_error'} else None})
            fields = {'capture_seconds','dll_http_seconds','dll_calls',
                      'full_process_inspections','durable_read_sessions',
                      'delivery_receipt_seconds','delivery_drain_seconds','coverage_api_seconds',
                      'history_persist_seconds','total_seconds'}
            result['recent_history_timings'] = []
            for row in conn.execute("SELECT metadata FROM local_logs WHERE event='hook_history_profile' ORDER BY created_at DESC LIMIT 12"):
                try:
                    values=json.loads(row['metadata'] or '{}')
                except (ValueError,TypeError):
                    continue
                if not isinstance(values,dict):continue
                result['recent_history_timings'].append({k:v for k,v in values.items()
                    if k in fields and type(v) in {int,float} and math.isfinite(v) and v>=0})
        if 'hook_active_calls' in names:
            result['call_states']={row[0]:row[1] for row in conn.execute('SELECT state,COUNT(*) FROM hook_active_calls GROUP BY state')}
            result['recent_calls']=[{
                'operation':row['kind'] if row['kind'] in CALL_KINDS else 'unknown',
                'state':row['state'] if row['state'] in {'calling','completed','uncertain'} else 'unknown',
                'process_sha256':hashlib.sha256(str(row['process_generation']).encode()).hexdigest(),
                'action_sha256':hashlib.sha256(json.dumps([row['process_generation'],row['action_id']]).encode()).hexdigest(),
            } for row in conn.execute('SELECT process_generation,action_id,kind,state FROM hook_active_calls ORDER BY attempted_at DESC LIMIT 20')]

    return result
