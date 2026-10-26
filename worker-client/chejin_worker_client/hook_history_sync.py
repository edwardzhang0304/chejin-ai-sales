"""Reread the authorized invitation range using fixed local templates.

Each capture must be durably saved before it is used. No active cursor advances
past unseen data. A changed catalog, count, page or callback barrier is a hold.
"""
from datetime import datetime, timezone
import uuid
import time
from .hook_history import HookHistoryQuery, schema_has_send_status
from .shared_rules import hook_history_rules as rules


class HookHistorySynchronizer:
    def __init__(self,gateway,save_capture,barrier,*,max_rows=rules.MAX_ROWS,now=None,media_enabled=False,send_diagnostics=False):
        self.send_diagnostics=send_diagnostics
        if type(max_rows) is not int or not 1<=max_rows<=rules.MAX_ROWS:raise ValueError('HOOK_HISTORY_LIMIT_INVALID')
        self.media_enabled=media_enabled
        self.gateway,self.save_capture,self.barrier,self.max_rows=gateway,save_capture,barrier,max_rows
        self.now=now or (lambda:datetime.now(timezone.utc))

    def scan(self,process,scope,delivery,since,*,reader):
        catalog=reader.history_catalog(process,scope); databases=[];total=0
        for database in catalog.databases:
            include_status=False
            def query(kind,lower='0',upper=None):
                q=HookHistoryQuery(database,kind,delivery if kind!='schema' else None,
                    after_local_id=lower,upper_local_id=upper,since_create_time=since if kind!='schema' else None,
                    include_send_status=include_status and kind=='page')
                capture=reader.history_capture(process,catalog,q)
                self.save_capture(capture)
                return q.validate_response(capture.payload)
            schema=query('schema')
            include_status=self.send_diagnostics and schema_has_send_status(schema)
            bound=query('bound')['data'][0];upper=bound['upper_local_id'];count=int(bound['row_count'])
            total+=count
            if total>self.max_rows:raise ValueError('HOOK_HISTORY_RANGE_TOO_LARGE')
            lower='0';pages=[];seen=0
            while lower!=upper:
                rows=query('page',lower,upper)['data']
                pages.append(rows);seen+=len(rows);lower=rows[-1]['localId']
                if seen>count:raise ValueError('HOOK_HISTORY_BOUND_CHANGED')
            if seen!=count:raise ValueError('HOOK_HISTORY_PAGINATION_INCOMPLETE')
            after=query('count','0',upper)['data'][0]['row_count']
            databases.append({'name':database.name,'generation':database.generation,'upper_local_id':upper,
                'row_count':bound['row_count'],'count_after':after,'pages':pages})
        if reader.history_catalog(process,scope)!=catalog:raise ValueError('HOOK_HISTORY_CATALOG_CHANGED')
        return {'catalog_fingerprint':catalog.fingerprint,'databases':databases,'observed_at':self.now().isoformat()}

    def capture(self,process,scope,delivery,since,*,callback_ids=()):
        rules.integer(since)
        started=time.perf_counter()
        start=self.barrier()
        if not isinstance(start,tuple) or len(start)!=2 or start[1]!=0:raise ValueError('HOOK_HISTORY_LOCAL_PENDING')
        with self.gateway.history_session(process,scope) as reader:
            scans=[self.scan(process,scope,delivery,since,reader=reader),self.scan(process,scope,delivery,since,reader=reader)]
            exclusions=[]
            if self.media_enabled and callback_ids:
                catalog=reader.history_catalog(process,scope)
                if catalog.fingerprint!=scans[1]['catalog_fingerprint']:raise ValueError('HOOK_HISTORY_CATALOG_CHANGED')
                included={r['MsgSvrID'] for d in scans[1]['databases'] for page in d['pages'] for r in page}
                from .shared_rules import hook_event_rules
                for database in scans[1]['databases']:
                    for page in database['pages']:
                        for row in page:
                            if row['Type'] in {'10000','10002'} and isinstance(row['StrContent'],str) and row['StrContent'].lstrip().startswith('<'):
                                target=hook_event_rules.revoked(row['StrContent'])
                                if target:included.add(target)
                missing=set(callback_ids)-included
                if len(missing)>self.max_rows:raise ValueError('HOOK_HISTORY_RANGE_TOO_LARGE')
                for mid in sorted(missing):
                    matches=[]
                    for database in catalog.databases:
                        query=HookHistoryQuery(database,'boundary_meta',delivery,since_create_time=since,native_msg_id=mid)
                        capture=reader.history_capture(process,catalog,query);self.save_capture(capture)
                        matches.extend({**row,'database':database.name,'database_generation':database.generation} for row in capture.payload.get('data',[]))
                    if len(matches)!=1:raise ValueError('HOOK_HISTORY_CALLBACK_NOT_LANDED')
                    exclusions.extend(matches)
        end=self.barrier()
        if end!=start:raise ValueError('HOOK_HISTORY_CALLBACK_BARRIER_CHANGED')
        coverage={'schema_version':'hook-history-coverage-v2' if self.media_enabled else 'hook-history-coverage-v1','since_create_time':since,
            'scans':scans,'local_pending_count':end[1]}
        if self.media_enabled:coverage['excluded_pre_invite_metadata']=exclusions
        rules.validate_coverage(coverage,delivery.peer_wxid,int(since))
        self.profile = {'capture_seconds': round(time.perf_counter()-started, 6),
                        'dll_http_seconds': round(reader.api_seconds, 6),
                        'dll_calls': reader.api_calls, 'full_process_inspections': 2,
                        'durable_read_sessions': 1}
        self.completed_barrier = end
        return str(uuid.uuid4()),coverage
