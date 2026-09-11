"""Release policy against real task creation and PostgreSQL state, without mutating queued work."""
import pytest
from sqlalchemy import select, text
from app.core.database import Base, SessionLocal, engine
from app.models.task import Task, TaskEvent
from app.models.worker import Worker
from app.services.release_readiness import release_readiness
from test_worker_client_api import _create_worker, _bind_worker, _heartbeat, _create_sales, _create_lead, _first_task


def setup_function():
    if engine.dialect.name != 'postgresql':
        pytest.skip('Release SQL gate requires PostgreSQL')
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_real_queued_task_is_preserved_and_execution_facts_block():
    worker = _create_worker('Release gate synthetic worker')
    _bind_worker(worker)
    _heartbeat(worker, run_status='paused')
    _create_sales(worker['id'])
    _create_lead('Release gate synthetic lead', '13896676682')
    task = _first_task()
    with SessionLocal() as db:
        before = dict(db.execute(text('select * from tasks where id=:id'), {'id': task['id']}).mappings().one())
        assert before['status'] == 'pending'
        ready = release_readiness(db)
        assert ready['ready'] and ready['preserved_queued_tasks'] == 1
        assert dict(db.execute(text('select * from tasks where id=:id'), {'id': task['id']}).mappings().one()) == before
        row = db.get(Task, task['id'])
        row.status = 'blocked'; row.block_code = 'SALES_WORKER_NOT_BOUND'; db.flush()
        assert release_readiness(db)['ready']
        event = TaskEvent(task_id=row.id, event_type='claimed')
        db.add(event); db.flush()
        assert release_readiness(db)['task_blockers'] == {'TASK_EXECUTION_HISTORY': 1}
        db.delete(event); row.status='completed'; row.lease_owner_client_instance_id='stale-owner'; db.flush()
        assert release_readiness(db)['task_blockers'] == {'TASK_LEASE_REMAINS': 1}
        row.lease_owner_client_instance_id=None; db.flush()
        assert release_readiness(db)['ready']
        current_worker = db.get(Worker, worker['id'])
        current_worker.run_status='faulted'; db.flush()
        assert release_readiness(db)['ready']  # Retain fault state; no intake or execution.
        current_worker.run_status='running'; db.flush()
        assert release_readiness(db)['worker_blockers'] == 1
        current_worker.run_status='faulted'
        current_worker.inflight_flow_state={'flow_id':'synthetic-active'}; db.flush()
        assert release_readiness(db)['worker_blockers'] == 1
