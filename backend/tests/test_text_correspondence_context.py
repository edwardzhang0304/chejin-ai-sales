"""Authoritative field extraction/HTTP context, not full D1 alignment acceptance."""
import copy
import json

import pytest
from sqlalchemy import select, event

import test_c3_api as fixtures
import test_vehicles_api as vehicles
from test_lead_followup_eligibility import isolated_db
from app.core.database import SessionLocal, engine
from app.models.lead import Lead
from app.models.sales import Sales
from app.models.vehicle import KnowledgeItem
from app.models.wechat import WechatSessionBinding
from app.services import text_correspondence_context as context_service
from app.services.wechat_service import _identity_checkpoint
from app.contracts.shared_rules import shared_adapter


def test_checkpoint_uses_only_registered_fields_and_includes_off_sale_vehicles():
    worker, target = fixtures._setup_bound_conversation()
    vehicle = vehicles._create_vehicle(display_name='  ＢＺ７  ', brand='测试品牌', model='七座版本', location='南京',
        vin='LTEST123456789012', purchase_price=7.5, internal_notes='不可下发的内部成本')
    with SessionLocal() as db:
        binding = db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id == target['conversation_id']))
        lead = db.get(Lead, binding.lead_id)
        lead.customer_name = ' 王梓轩 '
        lead.custom_fields = {**(lead.custom_fields or {}), 'entity': '自由文本不能充当保护词'}
        db.get(Sales, binding.sales_id).sales_name = '高磊'
        item = db.scalar(select(KnowledgeItem).where(KnowledgeItem.item_id == vehicle['vehicle_code']))
        payload = copy.deepcopy(item.payload)
        payload['data']['aliases'] = ['ＢＺ７', ' 丰田BZ7 ', {'private': 'ignore'}]
        item.payload = payload
        assert item.status == 'archived'
        db.commit()
    reads = []
    def counted(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT') and 'knowledge_items' in statement:
            reads.append(statement)
    event.listen(engine, 'before_cursor_execute', counted)
    try:
        response = fixtures.client.get(f"/api/workers/{worker['id']}/wechat/conversations/{target['conversation_id']}/read-authorization",
                                       headers=fixtures._worker_headers(worker))
    finally:
        event.remove(engine, 'before_cursor_execute', counted)
    assert response.status_code == 200, response.text
    checkpoint = response.json()['data']['identity_checkpoint']
    context = checkpoint['text_correspondence_context']
    entities = shared_adapter('text_correspondence').validate_entity_context(context)
    assert {'王梓轩', '高磊', 'bz7', '丰田bz7', '测试品牌', '七座版本', '南京'}.issubset(entities)
    assert len(reads) == 1, reads
    encoded = json.dumps(context, ensure_ascii=False)
    assert 'LTEST' not in encoded and '内部成本' not in encoded and '自由文本' not in encoded
    assert checkpoint['checkpoint_digest'] == shared_adapter('text_correspondence').checkpoint_digest(checkpoint)


def test_entity_update_invalidates_digest_and_empty_list_is_not_missing():
    worker, target = fixtures._setup_bound_conversation()
    rules = shared_adapter('text_correspondence')
    with SessionLocal() as db:
        binding = db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id == target['conversation_id']))
        before = _identity_checkpoint(db, conversation_id=target['conversation_id'])
        db.get(Lead, binding.lead_id).customer_name = '新名字'
        db.flush()
        changed = _identity_checkpoint(db, conversation_id=target['conversation_id'])
        assert before['checkpoint_digest'] != changed['checkpoint_digest']
        db.get(Lead, binding.lead_id).customer_name = ''
        db.get(Sales, binding.sales_id).sales_name = ''
        db.flush()
        empty = context_service.build_context(db, binding)
    assert empty == {'version': 1, 'known_entities': []}
    assert rules.validate_entity_context(empty) == ()
    with pytest.raises(ValueError): rules.validate_entity_context(None)
    with pytest.raises(ValueError): rules.validate_entity_context({'version': 1})
    with pytest.raises(ValueError): rules.validate_entity_context({**empty, 'entities_fully_covered': True})


def test_query_failure_is_not_reinterpreted_as_an_empty_authoritative_list(monkeypatch):
    _, target = fixtures._setup_bound_conversation()
    with SessionLocal() as db:
        binding = db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id == target['conversation_id']))
        def failed(*args, **kwargs): raise RuntimeError('controlled query unavailable')
        monkeypatch.setattr(db, 'scalars', failed)
        with pytest.raises(RuntimeError, match='controlled query unavailable'):
            context_service.build_context(db, binding)
