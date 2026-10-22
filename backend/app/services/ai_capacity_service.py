"""Cross-process and cross-account capacity immediately around a Provider call.

No Python semaphore or age-based lease expiration can grant a second call.
Only the matching call token releases a slot. Same-host orphan recovery needs
proof of process death/PID reuse; foreign-host or uninspectable ownership stays
held. This never treats a slow but living call as finished.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import os
import socket
import uuid

import psutil
from sqlalchemy import select,update,delete,func
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.errors import AppError
from app.models.ai_capacity import AIGenerationGate,AIGenerationSlot

GATE="brain-global-v1"
def _host_scope():
    # Distinguish PID namespaces/hosts before using local PID evidence.
    values=[socket.gethostname(),str(uuid.getnode())]
    try:
        values.append(os.readlink('/proc/self/ns/pid'))
    except OSError:
        pass
    return hashlib.sha256('|'.join(values).encode()).hexdigest()

HOST=_host_scope()


@dataclass(frozen=True)
class GenerationCallSlot:
    token: str
    conversation_id: str
    batch_id: str
    generation_attempt: int


def _lock(db):
    dialect=db.get_bind().dialect.name
    insert={"sqlite":sqlite_insert,"postgresql":pg_insert}.get(dialect)
    if insert is None:
        raise AppError("AI_CAPACITY_DATABASE_UNSUPPORTED","AI 调度数据库不受支持",503)
    db.execute(insert(AIGenerationGate).values(id=GATE,revision=0).on_conflict_do_nothing(index_elements=["id"]))
    # SQLite's no-op SELECT FOR UPDATE cannot serialize writers. An actual
    # UPDATE takes its writer lock; PostgreSQL locks the same singleton row.
    db.execute(update(AIGenerationGate).where(AIGenerationGate.id==GATE).values(revision=AIGenerationGate.revision+1))


def _owner_finished(row):
    if row.owner_host!=HOST:
        return False
    try:
        process=psutil.Process(row.owner_pid)
        return (not process.is_running() or process.status()==psutil.STATUS_ZOMBIE
                or repr(process.create_time())!=row.owner_process_started)
    except psutil.NoSuchProcess:
        return True
    except (psutil.AccessDenied,psutil.Error,OSError):
        return False


def acquire_call_slot(engine,*,conversation_id,batch_id,generation_attempt,limit):
    if type(limit) is not int or limit<1:
        raise ValueError("AI_CAPACITY_LIMIT_INVALID")
    with Session(bind=engine,autoflush=False) as db:
        _lock(db)
        for row in db.scalars(select(AIGenerationSlot)):
            if _owner_finished(row):
                db.delete(row)
        db.flush()
        if db.scalar(select(AIGenerationSlot.token).where(AIGenerationSlot.conversation_id==conversation_id)):
            db.commit()
            return None
        if db.scalar(select(func.count()).select_from(AIGenerationSlot))>=limit:
            db.commit()
            return None
        token=str(uuid.uuid4())
        db.add(AIGenerationSlot(token=token,conversation_id=conversation_id,batch_id=batch_id,
            generation_attempt=generation_attempt,owner_host=HOST,owner_pid=os.getpid(),
            owner_process_started=repr(psutil.Process(os.getpid()).create_time())))
        db.commit()
        return GenerationCallSlot(token,conversation_id,batch_id,generation_attempt)


def release_call_slot(engine,slot):
    with Session(bind=engine,autoflush=False) as db:
        _lock(db)
        removed=db.execute(delete(AIGenerationSlot).where(AIGenerationSlot.token==slot.token,
            AIGenerationSlot.conversation_id==slot.conversation_id,AIGenerationSlot.batch_id==slot.batch_id,
            AIGenerationSlot.generation_attempt==slot.generation_attempt)).rowcount
        db.commit()
        return removed==1
