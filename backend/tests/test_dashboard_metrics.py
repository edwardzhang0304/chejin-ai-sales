"""Product statistics, synthetic facts only; no WeChat/provider calls."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Row

import pytest
from app.services.dashboard.calculations import MessageFact, communication, human_response, metric, ratio, vehicle_sales
from app.services.dashboard.facts import invitation_cohort, InvitationFact, message_facts
from app.services.dashboard.service import period

BASE = datetime(2026, 10, 22, 16, tzinfo=timezone.utc)  # Beijing midnight


def fact(mid, minutes, source, *, lead='lead', sales='a', conversation='conversation', action=None):
    return MessageFact(('self', lead, str(mid)), lead, conversation, sales, BASE + timedelta(minutes=minutes),
                       source, int(mid), frozenset([str(mid)]), action)


def test_beijing_half_open_dates_and_today_as_of():
    now = BASE + timedelta(hours=10)
    assert period(BASE.date() + timedelta(days=1), BASE.date() + timedelta(days=1), now) == (BASE, now)
    a, b = period(BASE.date(), BASE.date(), now)
    assert a == BASE - timedelta(days=1) and b == BASE
    with pytest.raises(Exception):
        period(BASE.date() + timedelta(days=2), None, now)


def test_customer_dedup_is_union_not_sum_and_mode_only_filters_outgoing():
    facts = [fact(1, -30, 'customer'), fact(2, 30, 'customer'), fact(3, 60, 'ai'),
             fact(4, 61, 'human', sales='b'), fact(5, 70, 'ai', lead='no-reply')]
    end = BASE + timedelta(days=1)
    assert communication(facts, BASE, end, 'all', 'all') == (2, 1)
    assert communication(facts, BASE, end, 'a', 'ai') == (2, 1)
    assert communication(facts, BASE, end, 'b', 'human') == (1, 1)
    assert ratio(metric(0), metric(0))['value'] is None


@pytest.mark.parametrize('gap, expected', [(1440, 1), (1440.01, 0)])
def test_effective_rolling_24h_inclusive_boundary(gap, expected):
    facts = [fact(1, 10 - gap, 'customer'), fact(2, 10, 'customer'), fact(3, 15, 'ai')]
    assert communication(facts, BASE, BASE + timedelta(days=1), 'all', 'all') == (1, expected)


def test_human_response_ai_and_customer_append_do_not_reset_round():
    facts = [fact(8, 0, 'customer'), fact(9, 3, 'ai'), fact(10, 4, 'customer'), fact(11, 10, 'human'),
             fact(12, 20, 'customer'), fact(13, 25, 'human')]
    response, count, pending = human_response(facts, {}, BASE, BASE + timedelta(days=1), 'all')
    assert response['value'] == 450 and count == 2 and pending == 0


def test_human_response_pending_is_not_zero_and_late_reply_belongs_to_start_date():
    first = fact(1, 1430, 'customer')
    event = Row(trigger_message_event_ids=['1'], closed_at=None)
    handoffs = {'conversation': [event]}
    result = human_response([first], handoffs, BASE, BASE + timedelta(days=1), 'all')
    assert result[0]['value'] is None and result[1:] == (0, 1)
    later = human_response([first, fact(2, 1450, 'human')], handoffs, BASE, BASE + timedelta(days=1), 'all')
    assert later[0]['value'] == 1200 and later[1:] == (1, 0)


def test_old_multiple_ai_turns_without_human_round_origin_are_unavailable():
    facts = [fact(1, 0, 'customer'), fact(2, 2, 'ai', action='a'), fact(3, 1000, 'customer'),
             fact(4, 1001, 'ai', action='b'), fact(5, 1002, 'human')]
    assert human_response(facts, {}, BASE, BASE + timedelta(days=1), 'all')[0]['reason']


def test_invitation_cohort_ties_pass_to_original_task_and_dedups_globally():
    end = BASE + timedelta(days=1)
    facts = [InvitationFact('A', 'customer', 'a', BASE, BASE),
             InvitationFact('A-repeat', 'customer', 'a', BASE, BASE),
             InvitationFact('B', 'customer', 'b', BASE, BASE)]
    assert invitation_cohort(facts, {'B'}, BASE, end, 'a') == (0, 1, None)
    assert invitation_cohort(facts, {'B'}, BASE, end, 'b') == (1, 1, None)
    assert invitation_cohort(facts, {'B'}, BASE, end, 'all') == (1, 1, None)
    # A pass from an invitation outside this cohort never upgrades this cohort.
    assert invitation_cohort(facts, {'older'}, BASE, end, 'all') == (0, 1, None)


def test_legacy_invitation_interval_crossing_midnight_is_not_guessed():
    f = InvitationFact('A', 'customer', 'a', BASE - timedelta(seconds=1), BASE + timedelta(seconds=1))
    assert invitation_cohort([f], set(), BASE, BASE + timedelta(days=1), 'all')[2]


def row(mid, source='customer', **raw):
    return Row(id=str(mid), lead_id='lead', sales_id='a', conversation_id='conv', occurred_at=BASE,
               sender_role='customer' if source == 'customer' else 'self', message_type='voice',
               raw_payload={'transport': 'hook', 'self_wxid': 'self', 'peer_wxid': 'peer', 'native_msg_id':str(mid),
                            'sender_source':source, 'ordering_fields':{'Sequence':str(mid)}, **raw},
               evidence={'native_history_receipt_id':'receipt'})


def test_voice_rebinding_and_withdrawn_original_are_one_fact_not_text_dedup():
    original, replay, another = row(9, native_withdrawn=True), row(9), row(10)
    replay.id, replay.conversation_id = 'rebound-copy', 'new-conversation'
    facts, issues = message_facts([original, replay, another], {}, {}, BASE + timedelta(hours=1))
    assert len(facts) == 2 and not issues
    assert facts[0].event_ids == {'9', 'rebound-copy'}


def test_late_ai_actor_association_cannot_turn_unknown_receipt_into_success():
    outgoing = row(9, 'ai', ai_reply_action_id='action')
    action = Row(conversation_id='conv')
    ack = Row(send_result='unknown', action_phase='trigger_attempted', evidence={'native_msg_id':'9'})
    facts, issues = message_facts([outgoing], {'action':action}, {'action':ack}, BASE + timedelta(hours=1))
    assert not facts and issues
    ack.send_result, ack.action_phase = 'sent', 'confirmed'
    facts, issues = message_facts([outgoing], {'action':action}, {'action':ack}, BASE + timedelta(hours=1))
    assert len(facts) == 1 and not issues
    ack.evidence = {'native_msg_id':'other'}
    assert not message_facts([outgoing], {'action':action}, {'action':ack}, BASE + timedelta(hours=1))[0]


def transition(i, minutes, before, after, price=None):
    return Row(id=str(i), target_id='car', created_at=BASE + timedelta(minutes=minutes),
               before_data={'listing_status':before}, after_data={'listing_status':after},
               extra_metadata={'unlisting_public_price_yuan':price})


def test_vehicle_relisting_reverses_original_day_and_uses_each_sale_snapshot():
    first = transition(1, 0, 'listed', 'unlisted', '128800.00')
    end = BASE + timedelta(days=1)
    assert vehicle_sales([first], BASE, end)[1]['value'] == '128800.00'
    relisted = transition(2, 1441, 'unlisted', 'listed')
    assert vehicle_sales([first, relisted], BASE, end)[0]['value'] == 0
    second = transition(3, 1442, 'listed', 'unlisted', '130000.00')
    assert vehicle_sales([first, relisted, second], end, end + timedelta(days=1))[1]['value'] == '130000.00'
    # A legacy sale may prove count, but never its original price.
    first.extra_metadata = {}
    assert vehicle_sales([first], BASE, end)[1]['reason']


def test_optional_invitation_statistics_time_cannot_invent_a_precise_date():
    from app.services.dashboard.facts import invitation_facts
    task = Row(id='task', lead_id='lead', sales_id='a', worker_id='worker', status='completed', result_code='invite_sent')
    proof = Row(id='proof', task_id='task', worker_id='worker', created_at=BASE + timedelta(seconds=1),
                extra_metadata={'backend_validated':True, 'schema_version':'hook-invite-proof-v1',
                    'invitation_submitted':True, 'shortcode_written':True,
                    'attempted_at':(BASE-timedelta(seconds=1)).isoformat(),
                    'submitted_at':(BASE+timedelta(days=1)).isoformat()})
    facts, accepted = invitation_facts({'task':task}, [proof])
    assert accepted['proof'] is task
    assert invitation_cohort(facts, set(), BASE, BASE+timedelta(days=1), 'all')[2]


def test_handoff_trigger_on_appended_message_never_moves_first_response_start():
    facts = [fact(1, 0, 'customer'), fact(2, 2, 'customer'), fact(3, 3, 'ai'), fact(4, 10, 'human')]
    handoffs = {'conversation':[Row(trigger_message_event_ids=['2'], closed_at=None)]}
    assert human_response(facts, handoffs, BASE, BASE+timedelta(days=1), 'all')[0]['value'] == 600
    pending = human_response(facts[:3], handoffs, BASE, BASE+timedelta(days=1), 'all')
    assert pending[0]['value'] is None and pending[1:] == (0, 1)


def test_new_unanswered_round_uses_formal_message_identity_not_reminder_timestamps():
    facts = [fact(1, 0, 'customer'), fact(2, 10, 'human'), fact(3, 20, 'customer'), fact(4, 21, 'customer')]
    reminders = {'conversation':[Row(first_message_id='3', closed_at=None, started_at=BASE+timedelta(hours=8))]}
    result = human_response(facts, {}, BASE, BASE+timedelta(days=1), 'all', reminders)
    assert result[0]['value'] == 600 and result[1:] == (1, 1)
    facts.append(fact(5, 25, 'human'))
    reminders['conversation'][0].closed_at = BASE+timedelta(hours=9)
    result = human_response(facts, {}, BASE, BASE+timedelta(days=1), 'all', reminders)
    assert result[0]['value'] == 450 and result[1:] == (2, 0)


@pytest.mark.parametrize('ordering', [None, {}, 'legacy-unstructured'])
def test_missing_legacy_order_is_unavailable_not_a_dashboard_crash(ordering):
    facts, issues = message_facts([row(9, ordering_fields=ordering)], {}, {}, BASE+timedelta(hours=1))
    assert len(facts) == 1 and facts[0].order is None
    response, completed, pending = human_response(facts, {}, BASE, BASE+timedelta(days=1), 'all')
    assert response['reason'] and completed is None and pending is None
