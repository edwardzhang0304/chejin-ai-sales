"""Real HTTP and PostgreSQL uniqueness branch, with one hidden first lookup.

The injected race hides a real existing row once; PostgreSQL itself raises the
unique constraint failure. No collision validator, response or AI callback is
replaced. System fixtures enter through the ordinary HTTP ingest route too.
"""
from copy import deepcopy

import pytest
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

import test_historical_confidence_http as hc
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
from app.models.wechat import MessageEvent
from app.core.database import engine


@pytest.mark.parametrize('race', [False, True])
@pytest.mark.parametrize('kind', ['text', 'system'])
def test_historical_redelivery_keeps_hc_decision_on_both_insert_paths(
    http_api, monkeypatch, async_generation, race, kind,
):
    assert engine.dialect.name == 'postgresql'
    hidden = []; constraint_errors = []
    original_scalar, original_flush = Session.scalar, Session.flush

    def scalar(db, statement, *args, **kwargs):
        result = original_scalar(db, statement, *args, **kwargs)
        if (race and not hidden and isinstance(result, MessageEvent)
                and 'message_events.dedupe_key =' in str(statement)):
            hidden.append(result.id)
            return None
        return result

    def flush(db, *args, **kwargs):
        try:
            return original_flush(db, *args, **kwargs)
        except IntegrityError as exc:
            constraint_errors.append(getattr(exc.orig, 'sqlstate', None))
            raise

    monkeypatch.setattr(Session, 'scalar', scalar)
    monkeypatch.setattr(Session, 'flush', flush)
    base = hc.FrameInputHTTP

    class Frames(base):
        def post(self, path, **kwargs):
            if kind == 'system' and path.endswith('/wechat/messages/ingest') and len(self.visible) == 1:
                value = deepcopy(kwargs['json'])
                message = value['messages'][0]
                raw = message['raw_payload']; row = raw['observation']
                message.update(sender_role_hint='system', message_type='system')
                row.update(sender_role='system', sender_role_source='system',
                           message_type='system', row_kind='system_message')
                row['source_message'].update(sender_role='system', type='system')
                record = hc.api.committed_identity_record(
                    worker_stable_id=row['_worker_stable_id'],
                    commit_basis=hc.api.MessageCommitBasis.NEW_SUFFIX,
                    observation_id=row['observation_id'], sender_role='system', message_type='system',
                    proof={'alignment_status': 'not_required', 'old_tail_fully_consumed': True,
                           'new_suffix_observation_id': row['observation_id']})
                row['_worker_committed_message'] = record
                raw['message_identity_commit_record'] = record
                raw['strong_boundary_tokens'] = sorted(hc.api.boundary_tokens_for_observations(
                    [row], committed_only=True).get(0, set()))
                value['evidence']['slot_ledger_states'][0]['row_kind'] = 'system_message'
                kwargs['json'] = value
            return super().post(path, **kwargs)

    monkeypatch.setattr(hc, 'FrameInputHTTP', Frames)
    hc.test_hc_http_recomputes_then_continues_original_async_once(
        http_api, monkeypatch, async_generation, 'redelivery')
    assert len(hidden) == int(race)
    assert constraint_errors == (['23505'] if race else [])
