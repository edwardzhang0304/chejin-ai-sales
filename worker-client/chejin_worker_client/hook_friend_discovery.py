"""Durable, bounded notification-driven invitation discovery; no Hook I/O here.

Callbacks are hints, never binding authority. One startup sweep covers offline
acceptances. Each later batch consumes new private incoming observations only.
A batch is persisted before I/O so a backend retry can reuse the same capture.
"""
import hashlib
import uuid
from time import monotonic
from dataclasses import replace

from . import storage
from .hook_callback_contract import MAX_CALLBACK_ENTRIES
from .hook_facts import callback_observations
from .models import utc_now_iso
from .shared_rules import hook_contract_rules as rules

SCAN_ROWS = 32
RECENT_EVENTS = 4096
CONTACT_REFRESH_DELAYS = (2.0, 5.0)


class FriendDiscovery:
    def __init__(self, scope):
        self.scope = scope
        self.session = uuid.uuid4().hex
        self.key = rules.digest([scope.self_wxid, scope.account_epoch])

    def _load(self, db):
        db.execute('''CREATE TABLE IF NOT EXISTS hook_friend_discovery (
            scope_key TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL)''')
        row = db.execute('SELECT state_json FROM hook_friend_discovery WHERE scope_key=?', (self.key,)).fetchone()
        return rules.loads(row[0]) if row else {'cursor': 0, 'seen': [], 'pending': None, 'session': None}

    def _save(self, db, state):
        db.execute('''INSERT INTO hook_friend_discovery VALUES (?,?,?)
            ON CONFLICT(scope_key) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at''',
            (self.key, rules.canonical(state), utc_now_iso()))

    def prepare(self, invitations, bound_peers):
        with storage.db_connection() as db:
            state = self._load(db)
            pending = state['pending']
            # Return even an old process's round so runtime archives its end
            # before starting a fresh capture; it must never become authority.
            if pending:
                # Resuming a saved round is this startup's reconciliation, not
                # permission to start another sweep when that round completes.
                if pending['generation'] == self.scope.process_generation:
                    state['session'] = self.session
                    self._save(db, state)
                    db.commit()
                return pending
            startup = state['session'] != self.session
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='hook_raw_inbox'").fetchone()
            events = []
            seen = dict.fromkeys(state['seen'])
            if exists and startup:
                # One shared startup capture covers the whole retained backlog;
                # do not turn each old page into another full contact read.
                state['cursor'] = db.execute('SELECT COALESCE(MAX(receive_seq),0) FROM hook_raw_inbox WHERE self_wxid=? AND account_epoch=?',
                    (self.scope.self_wxid, self.scope.account_epoch)).fetchone()[0]
            if exists and not startup and invitations:
                query = '''SELECT * FROM hook_raw_inbox WHERE self_wxid=? AND account_epoch=?
                           AND receive_seq>? ORDER BY receive_seq LIMIT ?'''
                for row in db.execute(query, (self.scope.self_wxid, self.scope.account_epoch,
                                             state['cursor'], SCAN_ROWS)):
                    state['cursor'] = row['receive_seq']
                    raw = row['raw_body']
                    if hashlib.sha256(raw).hexdigest() != row['raw_digest']:
                        raise ValueError('HOOK_RAW_EVIDENCE_CHANGED')
                    original = rules.loads(raw)
                    # Validate using the original persisted process, never relabel
                    # an old callback as arriving in the current process.
                    original_scope = replace(self.scope, process_generation=row['process_generation'],
                                             hook_api_port=int(original['ServerPort']))
                    try:
                        observations = callback_observations(raw, original_scope, event_uuid=row['event_uuid'],
                            receive_seq=row['receive_seq'], max_entries=MAX_CALLBACK_ENTRIES)
                    except ValueError:
                        continue  # The original projection owns quarantine reporting.
                    for item in observations:
                        if (item.direction != 'incoming' or item.disposition != 'message_candidate'
                                or not item.peer_wxid or not item.native_msg_id
                                or item.peer_wxid in bound_peers):
                            continue
                        key = rules.digest([item.peer_wxid, item.native_msg_id])
                        if key in seen:
                            continue
                        seen[key] = None
                        events.append({'receive_seq': row['receive_seq'], 'raw_digest': row['raw_digest'],
                                       'peer_wxid': item.peer_wxid, 'native_msg_id': item.native_msg_id})
            state['seen'] = list(seen)[-RECENT_EVENTS:]
            # An operator-confirmed profile transfer can reuse an old invitation
            # without another friend-acceptance message. Check that grant once.
            active = {i['source_task_id'] for i in invitations}
            grants = set(state.get('grants', [])) & active
            new_grants = {i['source_task_id'] for i in invitations
                          if i.get('discovery_after_authorization') is True} - grants
            state['grants'] = sorted(grants | new_grants)
            state['session'] = self.session
            if invitations and (startup or events or new_grants):
                pending = {'round_id': uuid.uuid4().hex, 'generation': self.scope.process_generation,
                           'reason': 'startup' if startup else 'authorization' if new_grants else 'incoming_message',
                           'events': events, 'all_peers': startup,
                           'authorized_tasks': sorted(new_grants),
                           'invitations': invitations, 'stage': 'prepared', 'results': {}, 'snapshot': None}
                state['pending'] = pending
            else:
                state['pending'] = None
                pending = None
            self._save(db, state)
            db.commit()
            return pending

    def update(self, pending):
        with storage.db_connection() as db:
            state = self._load(db)
            if not state['pending'] or state['pending']['round_id'] != pending['round_id']:
                raise ValueError('HOOK_FRIEND_DISCOVERY_ROUND_CHANGED')
            state['pending'] = pending
            self._save(db, state)
            db.commit()

    def finish(self, pending):
        with storage.db_connection() as db:
            state = self._load(db)
            if state['pending'] and state['pending']['round_id'] == pending['round_id']:
                state['last_round'] = {k: v for k, v in pending.items() if k not in {'events', 'invitations'}}
                state['pending'] = None
                self._save(db, state)
                db.commit()
