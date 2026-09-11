"""Narrow compatibility must never become an old-client new-work whitelist."""
from types import SimpleNamespace
import copy

import pytest

from test_lead_followup_eligibility import isolated_db, fixture_rows
from app.core.database import SessionLocal
from app.models.worker import Worker
from app.models.base import utcnow
from app.contracts.c2 import contract_revision, contract_sha256, c2_contract_v3
from app.contracts import read_recovery
from app.services import read_recovery_service as service
from app.services.worker_service import worker_summary
from app.errors import AppError


@pytest.mark.parametrize('case', ['legacy', 'running', 'wrong_flow', 'wrong_customer', 'wrong_generation', 'wrong_digest', 'wrong_revision', 'new_registered_flow', 'unknown_registered_contract', 'fact_settlement', 'no_flow', 'task_in_progress'])
def test_legacy_admission_is_bound_to_stopped_original_read(case):
    worker, rows = fixture_rows()
    with SessionLocal() as db:
        owner = db.get(Worker, worker['id'])
        owner.run_status = 'faulted'
        owner.inflight_flow_state = {'flow_id': 'old-read', 'flow_kind': 'c2_read', 'conversation_id': rows[0]['conversation_id'],
                                     'status': 'draining', 'registered_at': utcnow().isoformat(), 'unread_generation': 7}
        payload = SimpleNamespace(contract_revision=read_recovery.LEGACY_REVISION, contract_sha256=read_recovery.LEGACY_SHA256,
            read_run_id='old-read', conversation_id=rows[0]['conversation_id'], unread_generation=7, authorization_scope=None)
        if case == 'running': owner.run_status = 'running'
        if case == 'wrong_flow': payload.read_run_id = 'new-read'
        if case == 'wrong_customer': payload.conversation_id = rows[1]['conversation_id']
        if case == 'wrong_generation': payload.unread_generation = 8
        if case == 'wrong_digest': payload.contract_sha256 = '0' * 64
        if case == 'wrong_revision': payload.contract_revision = '0.9.74'
        if case == 'new_registered_flow': owner.inflight_flow_state = {**owner.inflight_flow_state, 'contract_revision': contract_revision(), 'contract_sha256': contract_sha256()}
        if case == 'unknown_registered_contract': owner.inflight_flow_state = {**owner.inflight_flow_state, 'contract_revision': '0.9.74'}
        if case == 'fact_settlement': payload.authorization_scope = 'fact_settlement'
        if case == 'no_flow': owner.inflight_flow_state = {}
        if case == 'task_in_progress': owner.current_task = 'synthetic-task'
        if case == 'legacy':
            assert service.select_settlement_contract(db, owner, payload)['contract_revision'] == '0.9.75'
        else:
            with pytest.raises(AppError) as error:
                service.select_settlement_contract(db, owner, payload)
            assert error.value.code == 'MESSAGE_CONTRACT_REVISION_MISMATCH'


def test_missing_compatibility_resource_disables_recovery_without_breaking_status(monkeypatch):
    worker, _ = fixture_rows()
    def missing(): raise RuntimeError('RECOVERY_CONTRACT_MISSING')
    monkeypatch.setattr(service, 'read_recovery_capability', missing)
    with SessionLocal() as db:
        owner = db.get(Worker, worker['id']); owner.run_status = 'faulted'
        result = worker_summary(db, owner, include_token=False)
        assert result['run_status'] == 'faulted'
        assert result['pending_read_recovery']['ready'] is False
        assert result['pending_read_recovery']['contracts'] == []


def test_future_business_rule_change_is_not_silently_treated_as_compatible(monkeypatch):
    changed = copy.deepcopy(c2_contract_v3())
    changed['unreviewed_new_business_rule'] = True
    read_recovery.legacy_read_contract.cache_clear()
    monkeypatch.setattr(read_recovery, 'c2_contract_v3', lambda: changed)
    try:
        with pytest.raises(RuntimeError, match='RECOVERY_CONTRACT_SEMANTICS_CHANGED'):
            read_recovery.legacy_read_contract()
    finally:
        read_recovery.legacy_read_contract.cache_clear()
