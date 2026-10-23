"""HTTP projections over real service-produced facts, isolated local database."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func

from app.core.database import SessionLocal
from app.models.audit import OperationLog
from app.models.c3 import HandoffEvent
from app.models.lead import Lead, LeadAssignment
from app.models.sales import Sales
from app.models.task import TaskEvidence
from app.models.worker import Worker
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.services.dashboard.service import dashboard
from test_hook_business_http import clean, claimed, submitted, proof, post, body, client
from test_vehicles_api import _create_vehicle, PNG_1X1, ADMIN_HEADERS


def test_creation_invitation_binding_and_dashboard_have_no_business_side_effects():
    w, t, p, h, prep = claimed()
    request = proof(p, prep)
    request['submitted_at'] = datetime.now(timezone.utc).isoformat()
    path = f"/tasks/{t['id']}/hook-invite/submitted"
    assert post(w, path, request, h).status_code == 200
    # Exact persisted proof is replayable; old optional-less proofs are covered by existing tests.
    assert post(w, path, request, h).status_code == 200
    assert post(w, f"/workers/{w['id']}/wechat/native-bindings", body(t, p, prep)).status_code == 200
    with SessionLocal() as db:
        proof_before = db.scalar(select(TaskEvidence).where(TaskEvidence.evidence_type == 'hook_invite_submission')).extra_metadata
        worker_before = db.get(Worker, w['id']).inflight_flow_state
        counts = [db.scalar(select(func.count()).select_from(model)) for model in (TaskEvidence, HandoffEvent, OperationLog)]
        seller = db.scalar(select(Sales).where(Sales.worker_id == w['id']))
        seller_id = seller.id
    response = client.get('/api/dashboard')
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['metrics']['new_customers']['value'] == 1
    assert data['metrics']['friend_passes']['value'] == 1
    assert data['metrics']['friend_pass_rate']['value'] == 100
    seller_row = next(r for r in data['sales'] if r['sales_id'] == seller_id)
    assert seller_row['metrics']['new_customers']['value'] == 1
    assert data['realtime']['counts']['running'] == 1  # busy counts as accepting
    with SessionLocal() as db:
        assert db.scalar(select(TaskEvidence).where(TaskEvidence.evidence_type == 'hook_invite_submission')).extra_metadata == proof_before
        assert db.get(Worker, w['id']).inflight_flow_state == worker_before
        assert [db.scalar(select(func.count()).select_from(model)) for model in (TaskEvidence, HandoffEvent, OperationLog)] == counts


def test_initial_sales_snapshot_is_not_rewritten_by_later_assignment_or_disabling():
    w, t, *_ = claimed()
    with SessionLocal() as db:
        original = db.scalar(select(Sales).where(Sales.worker_id == w['id']))
        original.enabled = False
        old_id = original.id
        other = Sales(sales_name='合成第二销售', phone='13800000004')
        db.add(other); db.flush()
        lead = db.get(Lead, t['business_object']['lead']['id'])
        db.add(LeadAssignment(lead_id=lead.id, from_sales_id=old_id, to_sales_id=other.id,
                             assignment_type='manual', assignment_status='succeeded'))
        lead.sales_id = other.id
        db.commit()
    data = client.get('/api/dashboard').json()['data']
    old = next(r for r in data['sales'] if r['sales_id'] == old_id)
    assert old['metrics']['new_customers']['value'] == 1
    assert old['metrics']['total_customers']['value'] == 0
    assert data['realtime']['counts']['running'] == 1  # agreed: actual running even if seller disabled
    for mode in ('ai', 'human'):
        filtered = client.get('/api/dashboard', params={'mode':mode}).json()['data']
        assert filtered['metrics']['new_customers'] == data['metrics']['new_customers']
        assert filtered['realtime'] == data['realtime']
        assert filtered['vehicles'] == data['vehicles']


def test_vehicle_sale_snapshot_reversal_and_price_change_through_real_api():
    v = _create_vehicle(public_price='12.88')
    vid = v['vehicle_code']
    assert client.post(f'/api/vehicles/{vid}/images', files=[('files', ('car.png', PNG_1X1, 'image/png'))], headers=ADMIN_HEADERS).status_code == 200
    assert client.post(f'/api/vehicles/{vid}/list', headers=ADMIN_HEADERS).status_code == 200
    assert client.post(f'/api/vehicles/{vid}/unlist', headers=ADMIN_HEADERS).status_code == 200
    first = client.get('/api/dashboard').json()['data']['vehicles']
    assert first['sold']['value'] == 1 and first['amount']['value'] == '128800.00'
    assert client.put(f'/api/vehicles/{vid}', json={'public_price':'13.00'}, headers=ADMIN_HEADERS).status_code == 200
    assert client.post(f'/api/vehicles/{vid}/unlist', headers=ADMIN_HEADERS).status_code == 200
    assert client.get('/api/dashboard').json()['data']['vehicles']['amount'] == first['amount']
    assert client.post(f'/api/vehicles/{vid}/list', headers=ADMIN_HEADERS).status_code == 200
    assert client.get('/api/dashboard').json()['data']['vehicles']['sold']['value'] == 0
    assert client.post(f'/api/vehicles/{vid}/unlist', headers=ADMIN_HEADERS).status_code == 200
    assert client.get('/api/dashboard').json()['data']['vehicles']['amount']['value'] == '130000.00'
    with SessionLocal() as db:
        events = list(db.scalars(select(OperationLog).where(OperationLog.event_type == 'vehicle_unlisted').order_by(OperationLog.created_at)))
        assert len(events) == 2 and events[0].extra_metadata['unlisting_public_price_yuan'] == '128800.00'


def test_human_native_message_times_cross_day_and_no_transcript_double_count():
    w, t, p, h, prep = submitted()
    assert post(w, f"/workers/{w['id']}/wechat/native-bindings", body(t,p,prep)).status_code == 200
    base = datetime(2026, 10, 22, 15, 55, tzinfo=timezone.utc)
    with SessionLocal() as db:
        binding = db.scalar(select(WechatSessionBinding))
        for mid, minutes, source, kind in [('9',0,'customer','voice'), ('10',1,'customer','text'), ('11',10,'human','text')]:
            db.add(MessageEvent(id='event-'+mid, conversation_id=binding.conversation_id, binding_id=binding.id,
                worker_id=w['id'], lead_id=binding.lead_id, sales_id=binding.sales_id, rpa_session_key='hook:synthetic',
                read_run_id='synthetic', dedupe_key=mid, source_message_key=mid, sender_role='customer' if source=='customer' else 'self',
                message_type=kind, content='合成同一正文', occurred_at=base+timedelta(minutes=minutes),
                raw_payload={'transport':'hook','self_wxid':'self','peer_wxid':'peer','native_msg_id':mid,
                    'sender_source':source,'ordering_fields':{'Sequence':mid}}, evidence={'native_history_receipt_id':'synthetic'}))
        db.commit()
        data = dashboard(db,start=base.date(),end=base.date(),now=datetime.now(timezone.utc))
        assert data['metrics']['human_response_seconds']['value'] == 600
        assert data['metrics']['human_replied_rounds'] == 1
        # The reply belongs to the next Beijing day for communication, not the human round's day.
        assert data['metrics']['communication_customers']['value'] == 0
        next_day = (base+timedelta(days=1)).date()
        data = dashboard(db,start=next_day,end=next_day,now=datetime.now(timezone.utc))
        assert data['metrics']['communication_customers']['value'] == 1
        assert data['metrics']['effective_customers']['value'] == 0  # condition achieved previous day
        assert not db.new and not db.dirty and not db.deleted


def test_invalid_filters_are_explicit_errors_not_empty_success():
    assert client.get('/api/dashboard?mode=typo').status_code == 422
    assert client.get('/api/dashboard?sales_id=missing').status_code == 422
    assert client.get('/api/dashboard?start=2099-01-01').status_code == 422


def test_old_unknown_binding_does_not_poison_unrelated_new_customer_period():
    w, t, p, h, prep = submitted()
    assert post(w, f"/workers/{w['id']}/wechat/native-bindings", body(t,p,prep)).status_code == 200
    with SessionLocal() as db:
        binding = db.scalar(select(WechatSessionBinding))
        binding.native_context = {}
        binding.created_at = datetime.now(timezone.utc) - timedelta(days=3)
        db.commit()
    data = client.get('/api/dashboard').json()['data']
    assert data['metrics']['friend_passes']['value'] == 0
    with SessionLocal() as db:
        binding = db.scalar(select(WechatSessionBinding))
        binding.created_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert client.get('/api/dashboard').json()['data']['metrics']['friend_passes']['reason']


def test_optional_statistics_revision_does_not_block_vehicle_transition():
    from app.models.vehicle import KnowledgeItem
    v = _create_vehicle(public_price='12.88')
    vid = v['vehicle_code']
    assert client.post(f'/api/vehicles/{vid}/images', files=[('files', ('car.png', PNG_1X1, 'image/png'))], headers=ADMIN_HEADERS).status_code == 200
    with SessionLocal() as db:
        vehicle = db.scalar(select(KnowledgeItem).where(KnowledgeItem.item_id == vid))
        vehicle.payload = {**vehicle.payload, 'metadata':{**vehicle.payload['metadata'], 'listing_revision':'bad-legacy-value'}}
        db.commit()
    assert client.post(f'/api/vehicles/{vid}/list', headers=ADMIN_HEADERS).status_code == 200
    assert client.post(f'/api/vehicles/{vid}/unlist', headers=ADMIN_HEADERS).status_code == 200
    assert client.get('/api/dashboard').json()['data']['vehicles']['amount']['value'] == '128800.00'


def test_client_credential_reset_does_not_unassign_sales_in_realtime_dashboard():
    w, *_ = claimed()
    with SessionLocal() as db:
        worker = db.get(Worker, w['id'])
        worker.client_binding_state, worker.client_instance_id = 'unbound', None
        db.commit()
    data = client.get('/api/dashboard').json()['data']['realtime']
    assert data['counts']['unbound'] == 0
    assert data['counts']['running'] == 0 and data['unavailable'] == 1
