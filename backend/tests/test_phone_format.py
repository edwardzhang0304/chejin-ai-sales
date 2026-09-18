"""phone_format_v1 at the actual sales and lead API boundaries."""
import pytest
from sqlalchemy import select

from app.core.database import Base, SessionLocal, engine
from app.errors import AppError
from app.models.sales import Sales
from app.services.contact_utils import decrypt_for_p0, normalize_phone
from test_worker_sales_api import client


@pytest.fixture(autouse=True)
def isolated_schema():
    assert 'test' in str(engine.url.database)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


@pytest.mark.parametrize('phone', [
    '13900000001', '139 0000 0001', '139\u30000000\u30000001',
    '139\u00a00000\u00a00001', '139\t0000\r\n0001', ' \t13900000001\r\n',
])
def test_sales_create_edit_and_lead_dedupe_use_same_phone(phone, monkeypatch):
    from app.services import feishu_service
    from test_feishu_handoff import FakeFeishuAdapter
    provider = FakeFeishuAdapter()
    monkeypatch.setattr(feishu_service, 'get_feishu_adapter', lambda: provider)
    created = client.post('/api/sales', json={'sales_name': '格式测试', 'phone': phone})
    assert created.status_code == 200, created.text
    identity = created.json()['data']['id']
    updated = client.put(f'/api/sales/{identity}', json={'phone': phone})
    assert updated.status_code == 200, updated.text
    with SessionLocal() as db:
        assert db.get(Sales, identity).phone == '13900000001'
    assert provider.lookup_calls == ['13900000001']
    changed = client.put(f'/api/sales/{identity}', json={'phone': '138 0000 0002'})
    assert changed.status_code == 200, changed.text
    assert provider.lookup_calls == ['13900000001', '13800000002']
    first = client.post('/api/leads', json={'customer_name': '格式测试', 'phones': [phone]})
    assert first.status_code == 200, first.text
    again = client.post('/api/leads', json={'customer_name': '格式测试', 'phones': ['13900000001']})
    assert again.status_code == 409, again.text
    assert again.json()['code'] == 'LEAD_PHONE_DUPLICATED'
    assert again.json()['data']['duplicate_count'] == 1
    normalized = normalize_phone(phone)
    assert normalized.contact_hash == normalize_phone('13900000001').contact_hash
    assert normalized.masked == '139****0001'
    assert decrypt_for_p0(normalized.encrypted) == '13900000001'


def test_lead_edit_and_duplicate_preview_keep_normalized_hash():
    from app.services.lead_service import check_duplicate_phone
    response = client.post('/api/leads', json={'customer_name': '格式测试', 'phones': ['13900000001']})
    identity = response.json()['data']['id']
    changed = client.put(f'/api/leads/{identity}', json={'phones': ['138\u30000000\u00a00002']})
    assert changed.status_code == 200, changed.text
    with SessionLocal() as db:
        checked = check_duplicate_phone(db, '138\t0000\n0002')
        assert checked['has_active_duplicate'] is True
        assert checked['lead_id'] == identity
        assert checked['phone_masked'] == '138****0002'
        assert check_duplicate_phone(db, '13900000001')['has_active_duplicate'] is False
    duplicate = client.post('/api/leads', json={'customer_name': '格式测试2', 'phones': ['13800000002']})
    assert duplicate.status_code == 409, duplicate.text


@pytest.mark.parametrize('phone', [
    '1390000000', '139000000011', '139a00000001', '139-0000-0001',
    '+8613900000001', '１３９０００００００１', '1390000000１',
    '139\u200b00000001', '139\v00000001', '139\u202f00000001',
])
def test_invalid_characters_never_repair_into_a_valid_phone(phone):
    with pytest.raises(AppError):
        normalize_phone(phone)
    response = client.post('/api/sales', json={'sales_name': '格式测试', 'phone': phone})
    assert response.status_code == 422, response.text
    response = client.post('/api/leads', json={'customer_name': '格式测试', 'phones': [phone]})
    assert response.status_code == 400, response.text
    with SessionLocal() as db:
        assert db.scalar(select(Sales)) is None


def test_invalid_sales_update_does_not_change_saved_number():
    response = client.post('/api/sales', json={'sales_name': '格式测试', 'phone': '13900000001'})
    identity = response.json()['data']['id']
    rejected = client.put(f'/api/sales/{identity}', json={'phone': '139x00000002'})
    assert rejected.status_code == 422, rejected.text
    with SessionLocal() as db:
        assert db.get(Sales, identity).phone == '13900000001'
