"""Three distinct PCs/accounts through real auth/assignment/task APIs; no WeChat calls."""
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select, text
from app.core.database import SessionLocal
from app.models.task import Task
from app.models.worker import Worker
from test_hook_account_http import client, worker, heartbeat, context
from test_hook_business_http import post
import pytest
from app.core.database import Base, engine


@pytest.fixture(autouse=True)
def isolated_gray_database():
    if engine.url.get_backend_name() != "postgresql":
        pytest.skip("Multi-PC concurrency requires the isolated PostgreSQL gray fixture")
    assert engine.url.host == "127.0.0.1" and engine.url.database == "hook_gray_test"
    Base.metadata.drop_all(engine)
    with engine.begin() as db:
        db.execute(text("CREATE SCHEMA IF NOT EXISTS wechat_ai_customer_service"))
    Base.metadata.create_all(engine)


def cohort():
    people = [worker(f'gray-pc-{i}', f'1390000010{i}') for i in range(3)]
    for i, person in enumerate(people):
        data = heartbeat(person, context(f'wxid_gray_sales_{i}')).json()['data']
        assert not data['hook_context']['blocked'] and data['run_status'] == 'paused'
        assert post(person, f"/workers/{person['id']}/run-status",
            {'client_instance_id': person['instance'], 'run_status': 'running'}).status_code == 200
    for i in range(3):
        response = client.post('/api/leads', json={'customer_name': f'gray customer {i}', 'phones': [f'1380000010{i}']})
        assert response.status_code == 200, response.text
    result = []
    for person in people:
        response = client.get(f"/api/workers/{person['id']}/tasks/pull", headers={
            'X-Worker-Token': person['worker_token'], 'X-Client-Instance-Id': person['instance']})
        assert response.status_code == 200, response.text
        task = response.json()['data']['task']
        assert task and task['worker_id'] == person['id']
        result.append((person, task))
    assert len({task['id'] for _, task in result}) == 3
    return result


def test_three_pcs_reject_cross_task_claims_then_claim_own_tasks_concurrently():
    entries = cohort()
    for i, (person, _) in enumerate(entries):
        other = entries[(i + 1) % 3][1]
        response = post(person, f"/tasks/{other['id']}/claim", {'worker_id': person['id']})
        assert response.status_code in (403, 409), response.text
    with SessionLocal() as db:
        assert {t.status for t in db.scalars(select(Task))} == {'pending'}
    for person, task in entries:
        response = post(person, f"/workers/{person['id']}/inflight-flow/start",
                        {'flow_id': task['id'], 'flow_kind': 'task'})
        assert response.status_code == 200, response.text
    def claim(entry):
        person, task = entry
        return post(person, f"/tasks/{task['id']}/claim", {'worker_id': person['id']}, {'X-Inflight-Flow-Id': task['id']})
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(claim, entries))
    for response in results:
        assert response.status_code == 200, response.text
    with SessionLocal() as db:
        for person, task in entries:
            record = db.get(Task, task['id'])
            assert record.worker_id == person['id'] and record.status == 'running'
            assert db.get(Worker, person['id']).inflight_flow_state['flow_id'] == task['id']


def test_one_pc_account_fault_does_not_stop_other_two_accounts():
    entries = cohort()
    first, _ = entries[0]
    result = heartbeat(first, context('wxid_unexpected_account')).json()['data']
    assert result['run_status'] == 'faulted'
    with SessionLocal() as db:
        assert [db.get(Worker, person['id']).run_status for person, _ in entries] == ['faulted', 'running', 'running']
    for person, task in entries[1:]:
        assert post(person, f"/workers/{person['id']}/inflight-flow/start",
                    {'flow_id': task['id'], 'flow_kind': 'task'}).status_code == 200
        assert post(person, f"/tasks/{task['id']}/claim", {'worker_id': person['id']},
                    {'X-Inflight-Flow-Id': task['id']}).status_code == 200
