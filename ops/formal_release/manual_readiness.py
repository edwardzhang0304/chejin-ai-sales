"""Read-only recovery-aware publication check, installed by ops, executed in API container."""
import hashlib
import json
import sys
from datetime import datetime,timezone
from sqlalchemy import text
from app.contracts.c2 import contract_revision,contract_sha256
from app.core.database import SessionLocal
from app.services.release_readiness import release_readiness
from app.services.worker_service import has_unsettled_worker_send
from app.models.worker import Worker

request=json.loads(sys.argv[1])
assert contract_revision()==request['contract_revision'] and contract_sha256()==request['contract_sha256'], 'BACKEND_CONTRACT_MISMATCH'
expected=request['approved_read_flows'];assert isinstance(expected,dict)
with SessionLocal() as db:
 db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
 db.execute(text("SET LOCAL statement_timeout = '10s'"))
 readiness=release_readiness(db)
 assert not readiness['task_blockers'] and not readiness['pending_messages_or_send'], 'UNSETTLED_EXECUTION'
 seen={}
 for w in db.query(Worker).filter(Worker.deleted_at.is_(None)).all():
  assert w.run_status in ('paused','faulted') and w.running_status=='idle' and not w.current_task, 'WORKER_NOT_STOPPED'
  assert not (w.local_lock_summary or {}).get('locked') and not has_unsettled_worker_send(db,w), 'WORKER_SEND_OR_LOCK_PENDING'
  assert w.last_heartbeat_at is None or (datetime.now(timezone.utc)-w.last_heartbeat_at).total_seconds()>120, 'WORKER_RECONNECTED'
  if w.inflight_flow_state:
   seen[w.id]=hashlib.sha256(json.dumps(w.inflight_flow_state,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 assert seen==expected, 'UNREVIEWED_OR_CHANGED_RECOVERY_FLOW'
 assert readiness['worker_blockers']==len(expected),'OTHER_WORKER_BLOCKER'
 db.rollback()
print(json.dumps({'ready':True,'read_only':True,'original_flows_preserved':len(expected)}))
