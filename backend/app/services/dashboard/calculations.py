"""Read-only dashboard calculations; never infer delivery from an attempted send."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def metric(value=None, reason=None):
    return {"value": None if reason else value, "reason": reason,
            "availability": "unavailable" if reason else "complete"}


def ratio(numerator, denominator):
    if numerator["reason"] or denominator["reason"]:
        return metric(reason=numerator["reason"] or denominator["reason"])
    return metric(round(100 * numerator["value"] / denominator["value"], 2) if denominator["value"] else None)


@dataclass(frozen=True)
class MessageFact:
    identity: tuple[str, str, str]
    lead_id: str
    conversation_id: str
    sales_id: str | None
    at: datetime
    source: str  # customer, ai, human; only confirmed, non-system messages
    order: int | None = None
    event_ids: frozenset[str] = frozenset()
    action_id: str | None = None


def communication_population(facts, start, end, mode):
    """Build customer sets once per period; sales rows reuse the same population."""
    by_sales = defaultdict(set)
    for message in facts:
        if start <= message.at < end and message.source in ({"ai", "human"} if mode == "all" else {mode}):
            by_sales[message.sales_id or 'unassigned'].add(message.lead_id)
    previous, effective = {}, set()
    for message in sorted((m for m in facts if m.source == "customer"), key=lambda m: (m.at, m.identity)):
        prior = previous.get(message.lead_id)
        if prior is not None and start <= message.at < end and message.at - prior <= timedelta(hours=24):
            effective.add(message.lead_id)
        previous[message.lead_id] = message.at
    overall = set().union(*by_sales.values()) if by_sales else set()
    return by_sales, overall, effective


def communication_counts(population, sales_id):
    by_sales, overall, effective = population
    outgoing = overall if sales_id == 'all' else by_sales.get(sales_id, set())
    return len(outgoing), len(outgoing & effective)


def communication(facts, start, end, sales_id, mode):
    return communication_counts(communication_population(facts, start, end, mode), sales_id)


def human_response(facts, handoffs, start, end, sales_id, reminders=None):
    """Statistics only: never borrow reminder close-time or change reply ownership.

    Handoff/reminder message IDs prove that human handling is required, not
    when the round began. The earliest unanswered customer fact stays the
    start even if a later message triggered the handoff. Reminder timestamps
    are never used as message or reply times.
    """
    reminders = reminders or {}
    by_conversation = defaultdict(list)
    for message in facts:
        by_conversation[message.conversation_id].append(message)
    seconds, pending, ambiguous = [], 0, False
    for conversation_id, messages in by_conversation.items():
        relevant = any(start <= m.at < end and (sales_id == 'all' or (m.sales_id or 'unassigned') == sales_id) for m in messages)
        if any(m.order is None for m in messages):
            ambiguous |= relevant
            continue
        messages.sort(key=lambda m: (m.at, m.order))
        if len({(m.at, m.order) for m in messages}) != len(messages):
            ambiguous |= relevant
            continue
        first, prior_human, actions, unanswered_ids = None, False, set(), set()
        triggers = {mid for h in handoffs.get(conversation_id, []) for mid in h.trigger_message_event_ids}
        triggers.update(r.first_message_id for r in reminders.get(conversation_id, []))
        open_required = {mid for h in handoffs.get(conversation_id, []) if h.closed_at is None
                         for mid in h.trigger_message_event_ids}
        open_required.update(r.first_message_id for r in reminders.get(conversation_id, []) if r.closed_at is None)
        for message in messages:
            if message.source == 'customer':
                if first is None:
                    first = message
                unanswered_ids.update(message.event_ids)
            elif message.source == 'ai':
                if first:
                    actions.add(message.action_id or message.identity)
            elif message.source == 'human':
                if first and start <= first.at < end and (sales_id == 'all' or (first.sales_id or 'unassigned') == sales_id):
                    if not prior_human and not first.event_ids & triggers and len(actions) > 1:
                        ambiguous = True
                    else:
                        seconds.append((message.at - first.at).total_seconds())
                first, prior_human, actions, unanswered_ids = None, True, set(), set()
        if first and start <= first.at < end and (sales_id == 'all' or (first.sales_id or 'unassigned') == sales_id):
            if unanswered_ids & open_required:
                if not prior_human and not first.event_ids & triggers and len(actions) > 1:
                    ambiguous = True
                else:
                    pending += 1
    if ambiguous:
        return metric(reason='部分人工响应轮缺少可靠起点或原生顺序'), None, None
    return metric(round(sum(seconds) / len(seconds), 2) if seconds else None), len(seconds), pending


def vehicle_sales(events, start, end):
    """Replay transitions. A later listing removes the sale from its original day."""
    active = {}
    times = set()
    for event in sorted(events, key=lambda e: (aware(e.created_at), (e.extra_metadata or {}).get('listing_revision', 0))):
        key = (event.target_id, aware(event.created_at), (event.extra_metadata or {}).get('listing_revision', 0))
        if key in times:
            return metric(reason='车辆上下架先后顺序缺少可靠证据'), metric(reason='车辆上下架先后顺序缺少可靠证据')
        times.add(key)
        before = (event.before_data or {}).get("listing_status")
        after = (event.after_data or {}).get("listing_status")
        if before == "listed" and after == "unlisted":
            active[event.target_id] = event
        elif before == "unlisted" and after == "listed":
            active.pop(event.target_id, None)
    sales = [event for event in active.values() if start <= aware(event.created_at) < end]
    amount = Decimal("0")
    for event in sales:
        price = (event.extra_metadata or {}).get("unlisting_public_price_yuan")
        if price is None:
            return metric(len(sales)), metric(reason="所选期间存在缺少下架时售价快照的历史记录")
        try:
            value = Decimal(str(price))
            if not value.is_finite() or value < 0:
                raise ValueError('invalid snapshot')
            amount += value
        except (ValueError, InvalidOperation):
            return metric(len(sales)), metric(reason='存在格式不正确的下架售价快照')
    return metric(len(sales)), metric(str(amount.quantize(Decimal("0.01"))))
