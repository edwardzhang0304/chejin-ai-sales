"""Dashboard consumes actual native-history/ownership producers; all identities synthetic."""
from datetime import datetime, timezone
from unittest.mock import patch
import uuid

import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import Conversation, HandoffEvent, HumanReplyReminder
from app.models.wechat import MessageEvent
from app.services import c3_service, hook_history_service
from app.services.dashboard.service import dashboard
from app.contracts.shared_rules import shared_adapter
from test_hook_business_http import clean, post
from test_hook_history_business_http import bound, coverage


def history(w, p, binding, messages):
    value = coverage(binding)
    since = int(binding['history_since_create_time'])
    for scan in value['scans']:
        database = scan['databases'][0]
        seed = database['pages'][0][0]
        rows = [{**seed, 'localId': str(7800+i), 'CreateTime': str(since+at),
                 'Sequence': str((since+at)*1000+i), 'MsgSequence': str(780010+i),
                 'MsgSvrID': str(8000000000000000000+i), 'IsSender': str(sender), 'StrContent': text}
                for i, (at, sender, text) in enumerate(messages, 1)]
        database.update(pages=[rows], row_count=str(len(rows)), count_after=str(len(rows)), upper_local_id=str(7800+len(rows)))
        scan['observed_at'] = datetime.fromtimestamp(since+max(m[0] for m in messages)+10, timezone.utc).isoformat()
    now = datetime.fromtimestamp(since+max(m[0] for m in messages)+20, timezone.utc)
    payload = {**p, 'binding_id': binding['binding_id'], 'binding_revision': binding['binding_revision'],
               'capture_id': str(uuid.uuid4()), 'coverage': value,
               'coverage_digest': shared_adapter('hook_contract_rules').digest(value)}
    with patch.object(hook_history_service, 'utcnow', return_value=now):
        response = post(w, f"/workers/{w['id']}/wechat/native-history/coverage", payload)
    assert response.status_code == 200 and response.json()['data']['history_ready'], response.text
    return now


def test_formal_human_reply_new_customer_round_then_reply_and_replay():
    w, _, p, binding = bound()
    messages = [(1, 0, '合成问题'), (5, 1, '合成人工回复'), (10, 0, '合成新问题')]
    now = history(w, p, binding, messages)
    for _ in range(2):
        with SessionLocal() as db:
            conversation = db.get(Conversation, binding['conversation_id'])
            assert conversation.status == 'waiting_sales_reply' and conversation.reply_ownership['owner'] == 'human'
            assert len(list(db.scalars(select(HumanReplyReminder).where(HumanReplyReminder.closed_at.is_(None))))) == 1
            assert not list(db.scalars(select(HandoffEvent)))
            stats = dashboard(db, now=now)['metrics']
            assert stats['human_pending_rounds'] == 1
            assert stats['human_response_seconds']['value'] == 4
            assert not db.new and not db.dirty
        now = history(w, p, binding, messages)
    messages.append((18, 1, '合成第二轮回复'))
    now = history(w, p, binding, messages)
    with SessionLocal() as db:
        stats = dashboard(db, now=now)['metrics']
        assert stats['human_pending_rounds'] == 0 and stats['human_replied_rounds'] == 2
        assert stats['human_response_seconds']['value'] == 6


@pytest.mark.parametrize('second_time', [1, 3])
def test_formal_later_handoff_preserves_first_message_including_same_second(second_time):
    w, _, p, binding = bound()
    messages = [(1, 0, '合成最早问题'), (second_time, 0, '合成追加问题')]
    now = history(w, p, binding, messages)
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding['conversation_id'])
        rows = list(db.scalars(select(MessageEvent)))
        appended = next(m for m in rows if m.content == '合成追加问题')
        c3_service._create_or_reuse_open_handoff(db, conversation=conversation, batch_id=None,
            handoff_reason_code='customer_requested_human', reason_detail='synthetic',
            trigger_message_event_ids=[appended.id], risk_flags=[], evidence_refs=[], ai_payload={})
        db.commit()
        assert dashboard(db, now=now)['metrics']['human_pending_rounds'] == 1
    messages.append((11, 1, '合成人工首答'))
    now = history(w, p, binding, messages)
    with SessionLocal() as db:
        stats = dashboard(db, now=now)['metrics']
        assert stats['human_response_seconds']['value'] == 10
        assert stats['human_replied_rounds'] == 1 and stats['human_pending_rounds'] == 0
