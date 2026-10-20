"""Retire only old acknowledged raw bytes; preserve pending/unknown journals."""
import hashlib,json,re
from datetime import datetime,timedelta,timezone
from pathlib import Path
import shutil
from . import storage
from .shared_rules import hook_contract_rules as rules

FIELDS=('receive_seq','event_uuid','self_wxid','account_epoch','process_generation','raw_digest','retired_at')


def retire_inbox(private_root,*,retention_days,now=None,limit=100):
    if type(retention_days) is not int or retention_days<1 or not 1<=limit<=1000:raise ValueError('HOOK_RETENTION_POLICY_INVALID')
    now=now or datetime.now(timezone.utc);cutoff=now-timedelta(days=retention_days)
    count=released=0
    with storage.db_connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        names={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'hook_raw_inbox','hook_observation_projection'}.issubset(names):return {'retired_rows':0,'released_bytes':0}
        cursor=conn.execute("SELECT value FROM client_settings WHERE key='hook_retention_scan_v1'").fetchone()
        after=rules.loads(cursor[0]).get('receive_seq',0) if cursor else 0
        if type(after) is not int or after<0:raise ValueError('HOOK_RETENTION_CURSOR_INVALID')
        query='SELECT i.*,p.projection_json,p.projection_digest FROM hook_raw_inbox i JOIN hook_observation_projection p ON p.receive_seq=i.receive_seq WHERE i.received_at<? AND i.receive_seq>? ORDER BY i.receive_seq LIMIT ?'
        rows=conn.execute(query,(cutoff.isoformat(),after,limit)).fetchall()
        if not rows and after:
            rows=conn.execute(query,(cutoff.isoformat(),0,limit)).fetchall()
        if rows:
            conn.execute("INSERT INTO client_settings(key,value,updated_at) VALUES ('hook_retention_scan_v1',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (rules.canonical({'receive_seq':rows[-1]['receive_seq']}),now.isoformat()))
        for row in rows:
            if hashlib.sha256(row['raw_body']).hexdigest()!=row['raw_digest'] or hashlib.sha256(row['projection_json'].encode()).hexdigest()!=row['projection_digest']:
                raise ValueError('HOOK_RETENTION_EVIDENCE_CHANGED')
            projection=rules.loads(row['projection_json'])
            if projection.get('status')!='projected':continue
            observations=projection['observations'];stored=set()
            if 'hook_delivery_outbox' in names:
                for delivered in conn.execute("SELECT payload_json FROM hook_delivery_outbox WHERE receive_seq=? AND status='stored'",(row['receive_seq'],)):
                    envelope=rules.loads(delivered[0])
                    stored.update((envelope['peer_wxid'],m['native_msg_id']) for m in envelope['messages'])
            ready=True
            for item in observations:
                mid=item.get('native_msg_id');kind=item['message_type'];state=item['disposition']
                if item.get('reason_code')=='HOOK_GROUP_EXCLUDED':continue
                if kind=='voice':
                    if 'hook_voice_attempts' not in names:ready=False;break
                    voice=conn.execute('SELECT * FROM hook_voice_attempts WHERE self_wxid=? AND account_epoch=? AND native_msg_id=?',
                        (row['self_wxid'],row['account_epoch'],mid)).fetchone()
                    if voice is None or voice['state'] not in {'transcribed','failed'}:ready=False;break
                    receipt=Path(private_root)/'voice-receipts'/row['self_wxid']/row['account_epoch']/f'{mid}.json'
                    if not receipt.is_file() or receipt.is_symlink():ready=False;break
                    saved=rules.loads(receipt.read_bytes())
                    if saved.get('attempt_digest')!=rules.digest(dict(voice)):ready=False;break
                elif state!='message_candidate' or (item.get('peer_wxid'),mid) not in stored:
                    ready=False;break
            if not ready:continue
            conn.execute('CREATE TABLE IF NOT EXISTS hook_retired_observations (receive_seq INTEGER PRIMARY KEY,event_uuid TEXT NOT NULL,self_wxid TEXT NOT NULL,account_epoch TEXT NOT NULL,process_generation TEXT NOT NULL,raw_digest TEXT NOT NULL,retired_at TEXT NOT NULL)')
            if tuple(x['name'] for x in conn.execute('PRAGMA table_info(hook_retired_observations)'))!=FIELDS:raise ValueError('HOOK_RETENTION_SCHEMA_INVALID')
            conn.execute('INSERT INTO hook_retired_observations VALUES (?,?,?,?,?,?,?)',tuple(row[k] for k in FIELDS[:-1])+(now.isoformat(),))
            if 'hook_delivery_outbox' in names:
                deliveries=conn.execute('SELECT * FROM hook_delivery_outbox WHERE receive_seq=?',(row['receive_seq'],)).fetchall()
                if any(d['status']!='stored' for d in deliveries):raise ValueError('HOOK_RETENTION_PENDING_DELIVERY')
                conn.execute('CREATE TABLE IF NOT EXISTS hook_retired_deliveries (event_uuid TEXT PRIMARY KEY,record_json TEXT NOT NULL,retired_at TEXT NOT NULL)')
                for delivered in deliveries:
                    conn.execute('INSERT INTO hook_retired_deliveries VALUES (?,?,?)',(delivered['event_uuid'],rules.canonical(dict(delivered)),now.isoformat()))
                conn.execute('DELETE FROM hook_delivery_outbox WHERE receive_seq=?',(row['receive_seq'],))
            conn.execute('DELETE FROM hook_observation_projection WHERE receive_seq=?',(row['receive_seq'],))
            conn.execute('DELETE FROM hook_raw_inbox WHERE receive_seq=?',(row['receive_seq'],))
            count+=1;released+=len(row['raw_body'])
        conn.commit()
    return {'retired_rows':count,'released_bytes':released,'pending_records_deleted':False}


def retire_c1_roi(private_root,*,retention_days,now=None,artifact_root=None):
    now=now or datetime.now(timezone.utc);root=Path(private_root);cutoff=(now-timedelta(days=retention_days)).timestamp();removed=0
    areas=[root/'roi']
    if artifact_root is not None:areas.append(Path(artifact_root)/'tasks')
    for folder in (folder for area in areas if area.is_dir() and not area.is_symlink() for folder in area.iterdir()):
        if not folder.is_dir() or folder.is_symlink() or not rules.identifier(folder.name):continue
        files=list(folder.rglob('*'))
        if any(f.is_symlink() for f in files):continue
        if max((f.stat().st_mtime for f in files if f.is_file()),default=folder.stat().st_mtime)>=cutoff:continue
        if not (root/'finished'/f'{folder.name}.json').is_file():continue
        # Only a backend-confirmed C1 has recyclable visual artifacts; its
        # immutable preparation/submit proof and receipt stay outside ROI.
        receipts=list((root/'invites').glob(f'*/{folder.name}/backend-submission-receipt.json'))
        if len(receipts)!=1 or rules.loads(receipts[0].read_bytes()).get('task',{}).get('result_code')!='invite_sent':continue
        shutil.rmtree(folder);removed+=1
    return removed


def retire_history_artifacts(private_root, *, retention_days, now=None, limit=100, settled_send_root=None):
    """Retire acknowledged raw files, keeping receipts and exact file hashes.

    Called by the single dispatcher only when no Flow is active. Unresolved
    physical sends hold all history evidence. Shared captures referenced by a
    recent or unconfirmed sync are protected, even when an older sync is ready.
    Old packages without the receipt digest/index remain conservatively intact.
    """
    if type(retention_days) is not int or retention_days < 1 or not 1 <= limit <= 1000:
        raise ValueError('HOOK_RETENTION_POLICY_INVALID')
    root = Path(private_root)
    index_root = root / 'history-artifact-index'
    result = {'retired_captures': 0, 'released_bytes': 0, 'unresolved_send_hold': False}
    if root.is_symlink() or not index_root.is_dir() or index_root.is_symlink():
        return result
    for directory in ('sends','send-receipts','history-receipts','history-captures','history-coverage','history-artifact-index/retired','history-artifact-index/completed'):
        if (root / directory).is_symlink():
            raise ValueError('HOOK_RETENTION_EVIDENCE_CHANGED')
    if settled_send_root is None:
        for intent in (root / 'sends').glob('*/*/*/intent.json'):
            if intent.is_symlink() or any(parent.is_symlink() for parent in list(intent.parents)[:3]):
                raise ValueError('HOOK_RETENTION_EVIDENCE_CHANGED')
            saved = rules.loads(intent.read_bytes())
            action = saved.get('reply_action_id')
            if not rules.identifier(action):
                raise ValueError('HOOK_RETENTION_EVIDENCE_CHANGED')
            receipt = root / 'send-receipts' / f'{action}.json'
            settled = rules.loads(receipt.read_bytes()) if receipt.is_file() and not receipt.is_symlink() else {}
            if settled.get('local_send_observed') is not True and settled.get('physical_send_attempted') is not False:
                result['unresolved_send_hold'] = True
                return result
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=retention_days)).timestamp()
    eligible, protected = [], set()
    for path in sorted(index_root.glob('*.json')):
        if path.is_symlink() or not rules.identifier(path.stem):
            raise ValueError('HOOK_RETENTION_EVIDENCE_CHANGED')
        index = rules.loads(path.read_bytes())
        retired = index_root / 'retired' / path.name
        complete = index_root / 'completed' / path.name
        if complete.is_file():
            continue
        files = index.get('files')
        if (index.get('schema_version') != 'hook-history-artifacts-v1'
                or index.get('capture_id') != path.stem or not isinstance(files, dict)):
            raise ValueError('HOOK_RETENTION_EVIDENCE_CHANGED')
        for name, digest in files.items():
            relative = Path(name)
            if (len(relative.parts) != 2 or relative.parts[0] not in {'history-captures', 'history-coverage'}
                    or relative.suffix != '.json'
                    or not (rules.identifier(relative.stem) if relative.parts[0] == 'history-coverage' else re.fullmatch(r'[0-9a-f]{64}', relative.stem))
                    or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                    or (root / relative.parts[0]).is_symlink() or (root / relative).is_symlink()):
                raise ValueError('HOOK_RETENTION_EVIDENCE_CHANGED')
        coverage = root / 'history-coverage' / f'{path.stem}.json'
        receipt = root / 'history-receipts' / f'{path.stem}.json'
        ack = rules.loads(receipt.read_bytes()) if receipt.is_file() and not receipt.is_symlink() else {}
        settled=True
        if settled_send_root is not None:
            action=index.get('retained_reply_action_id')
            if not rules.identifier(action):raise ValueError('HOOK_RETENTION_EVIDENCE_CHANGED')
            send_area=Path(settled_send_root)/'send-receipts'
            if send_area.is_symlink():raise ValueError('HOOK_RETENTION_EVIDENCE_CHANGED')
            send_receipt=send_area/f'{action}.json'
            saved=rules.loads(send_receipt.read_bytes()) if send_receipt.is_file() and not send_receipt.is_symlink() else {}
            settled=saved.get('local_send_observed') is True or saved.get('physical_send_attempted') is False
        planned=rules.loads(retired.read_bytes()) if retired.is_file() else None
        if planned and (planned.get('capture_id')!=path.stem or planned.get('index_digest')!=rules.digest(index)):
            raise ValueError('HOOK_RETENTION_EVIDENCE_CHANGED')
        ready = (settled and (ack.get('history_ready') is True or settled_send_root is not None and ack.get('reason_code')=='HOOK_NATIVE_SEND_ACTOR_PENDING') and ack.get('capture_id') == path.stem
            and ack.get('coverage_digest') == index.get('coverage_digest')
            and (bool(planned) or coverage.is_file() and rules.digest(rules.loads(coverage.read_bytes())) == ack.get('coverage_digest'))
            and path.stat().st_mtime < cutoff)
        if ready:
            eligible.append((path, index))
        else:
            protected.update(files)
    # Unprocessed old indexes also protect shared files in this bounded pass.
    for _, index in eligible[limit:]:
        protected.update(index['files'])
    from .hook_formal_c1 import _save
    for path, index in eligible[:limit]:
        intent_path=index_root/'retired'/path.name
        complete_path=index_root/'completed'/path.name
        plan=rules.loads(intent_path.read_bytes()) if intent_path.is_file() else None
        planned_names=plan.get('retired_files') if plan else None
        if plan and (not isinstance(planned_names,list) or any(name not in index['files'] for name in planned_names)):
            raise ValueError('HOOK_RETENTION_EVIDENCE_CHANGED')
        # A partial retirement can finish its original decision; shared files
        # newly in use are rechecked and held, never silently treated as deleted.
        names=planned_names if plan else [name for name in index['files'] if name not in protected
            and (root/name).is_file() and (root/name).stat().st_mtime<cutoff]
        if not names:
            continue
        for name in names:
            target=root/name
            if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()!=index['files'][name]:
                raise ValueError('HOOK_RETENTION_EVIDENCE_CHANGED')
        if not plan:
            _save(intent_path, {'capture_id':index['capture_id'],'index_digest':rules.digest(index),
                'retired_at':now.isoformat(),'retired_files':names})
        held=False
        for name in names:
            target=root/name
            if not target.exists():continue
            if name in protected or target.stat().st_mtime>=cutoff:
                held=True;continue
            result['released_bytes']+=target.stat().st_size
            target.unlink()
        if not held:
            _save(complete_path,{'capture_id':index['capture_id'],'index_digest':rules.digest(index),
                'retirement_complete':True,'completed_at':now.isoformat()})
            result['retired_captures']+=1
    return result


class HistoryArtifactBudget:
    """One dispatcher owns writes; settlement metadata remains writable at cap."""
    DIRECTORIES=('history-captures','history-coverage','history-receipts','history-artifact-index','history-retries')

    def __init__(self, root, *, maximum_bytes, retention_days):
        if type(maximum_bytes) is not int or maximum_bytes <= 0:
            raise ValueError('HOOK_RETENTION_POLICY_INVALID')
        self.root=Path(root)
        self.maximum_bytes=maximum_bytes
        self.retention_days=retention_days
        self.used=None
        self.ready=True

    def _size(self):
        total=0
        for name in self.DIRECTORIES:
            area=self.root/name
            if area.is_symlink():
                raise ValueError('HOOK_RETENTION_EVIDENCE_CHANGED')
            for path in area.rglob('*.json'):
                if path.is_symlink():
                    raise ValueError('HOOK_RETENTION_EVIDENCE_CHANGED')
                total+=path.stat().st_size
        return total

    def begin_capture(self, *, allow_retire):
        # Retirement runs before any page is used by this capture, never in the
        # middle of publishing its index. Current/unknown sends still hold it.
        margin=min(self.maximum_bytes//10,2*1024*1024)
        if self.used is None or self.used>=self.maximum_bytes-margin:
            self.used=self._size()
            if allow_retire and self.used>=self.maximum_bytes-margin:
                retire_history_artifacts(self.root,retention_days=self.retention_days)
                self.used=self._size()
        self.ready=self.used<self.maximum_bytes

    def save(self, path, value, *, settlement=False):
        from .hook_formal_c1 import _save
        path=Path(path)
        relative=path.relative_to(self.root)
        if len(relative.parts)!=2 or relative.parts[0] not in self.DIRECTORIES:
            raise ValueError('HOOK_RETENTION_EVIDENCE_CHANGED')
        size=len(rules.canonical(value).encode('utf-8'))
        if self.used is None:
            self.used=self._size()
        if not settlement and self.used+size>self.maximum_bytes:
            self.used=self._size()
            if self.used+size>self.maximum_bytes:
                self.ready=False
                raise ValueError('HOOK_HISTORY_STORAGE_CAPACITY_EXCEEDED')
        _save(path,value)
        self.used+=size
        self.ready=self.used<self.maximum_bytes


RECONCILIATION_RESERVE_BYTES=32*1024*1024


def reconciliation_budget(private_root, *, retention_days):
    root=Path(private_root)
    reserve=root/'history-reconcile'
    retire_history_artifacts(reserve,retention_days=retention_days,settled_send_root=root)
    return HistoryArtifactBudget(reserve,maximum_bytes=RECONCILIATION_RESERVE_BYTES,
        retention_days=retention_days)
