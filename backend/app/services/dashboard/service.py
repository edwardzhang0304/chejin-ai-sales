"""One read-only dashboard endpoint. Dates and eligibility never live in the UI."""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, or_
from types import SimpleNamespace

from app.errors import AppError
from app.models.audit import OperationLog
from app.models.base import utcnow
from app.models.c3 import HandoffEvent, HumanReplyReminder, ReplyAction, SentAck
from app.models.lead import Lead, LeadAssignment
from app.models.sales import Sales
from app.models.task import Task, TaskEvidence
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.models.vehicle import KnowledgeItem
from app.services.dashboard.calculations import aware, communication, communication_counts, communication_population, human_response, metric, ratio, vehicle_sales
from app.services.dashboard.facts import friend_pass_facts, invitation_cohort, invitation_facts, message_facts
from app.services.vehicle_service import _vehicle_query
from app.services.worker_service import operational_status

ZONE = ZoneInfo('Asia/Shanghai')
MAX_FACTS = 100000


def period(start: date | None, end: date | None, now):
    today = now.astimezone(ZONE).date()
    start, end = start or today, end or today
    if start > end or end > today:
        raise AppError('DASHBOARD_DATE_INVALID', '请选择有效日期，结束日期不能晚于今天', 422)
    left = datetime.combine(start, time.min, ZONE).astimezone(timezone.utc)
    right = min(datetime.combine(end + timedelta(days=1), time.min, ZONE).astimezone(timezone.utc), now)
    return left, right


def bounded(db, query):
    # Old handoffs and reversible sale events require retained predecessors.
    # A capacity limit is explicit, never a silently truncated population.
    values = list(db.scalars(query.limit(MAX_FACTS + 1)))
    if len(values) > MAX_FACTS:
        raise AppError('DASHBOARD_CAPACITY_EXCEEDED', '历史统计容量已达到上限，请联系维护人员扩展统计投影', 422)
    return values


def bounded_rows(db, query):
    rows = list(db.execute(query.limit(MAX_FACTS + 1)))
    if len(rows) > MAX_FACTS:
        raise AppError('DASHBOARD_CAPACITY_EXCEEDED', '历史统计容量已达到上限，请联系维护人员扩展统计投影', 422)
    return rows


def owner_at(lead, assignments, at):
    rows = [a for a in assignments if aware(a.created_at) < at]
    if rows:
        return rows[-1].to_sales_id, True
    if assignments:
        return assignments[0].from_sales_id, True
    return None, lead.sales_id is None


def dashboard(db, *, start=None, end=None, vehicle_start=None, vehicle_end=None,
              sales_id='all', mode='all', status_sales_id='all', now=None):
    now = aware(now or utcnow())
    left, right = period(start, end, now)
    vehicle_left, vehicle_right = period(vehicle_start, vehicle_end, now)
    if mode not in {'all', 'ai', 'human'}:
        raise AppError('DASHBOARD_MODE_INVALID', '沟通方式无效', 422)
    sales = bounded(db, select(Sales).where(Sales.created_at < now).order_by(Sales.sales_name, Sales.id))
    valid_sales = {s.id for s in sales}
    if sales_id not in valid_sales | {'all', 'unassigned'} or status_sales_id not in valid_sales | {'all'}:
        raise AppError('DASHBOARD_SALES_INVALID', '销售筛选无效', 422)
    leads = bounded(db, select(Lead).where(Lead.created_at < right))
    assignments = defaultdict(list)
    for a in bounded(db, select(LeadAssignment).where(LeadAssignment.assignment_status == 'succeeded',
            LeadAssignment.created_at < now).order_by(LeadAssignment.created_at, LeadAssignment.id)):
        assignments[a.lead_id].append(a)
    creation = {log.lead_id: log.extra_metadata for log in bounded(db, select(OperationLog).where(
        OperationLog.event_type == 'lead_created', OperationLog.created_at >= left, OperationLog.created_at < now))
        if (log.extra_metadata or {}).get('dashboard_fact_version') == 1}
    tasks = {t.id: t for t in bounded(db, select(Task).where(Task.task_type == 'add_friend', Task.created_at < now))}
    proofs = bounded(db, select(TaskEvidence).where(TaskEvidence.evidence_type == 'hook_invite_submission', TaskEvidence.created_at < now))
    invitations, accepted = invitation_facts(tasks, proofs)
    bindings = bounded(db, select(WechatSessionBinding).where(WechatSessionBinding.created_at < now))
    first_pass, passed_tasks, pass_issues = friend_pass_facts(bindings, accepted)
    missing_invitations = {t.id for t in tasks.values() if t.status == 'completed' and t.result_code == 'invite_sent'} - {f.task_id for f in invitations}
    # Retained per-conversation history is needed to establish the first human round;
    # ingestion time is never substituted for an unknown native occurrence time.
    trend_left, _ = period(now.astimezone(ZONE).date() - timedelta(days=6), None, now)
    message_start = min(left, trend_left) - timedelta(hours=24)
    relevant_conversations = select(MessageEvent.conversation_id).where(
        MessageEvent.ingested_at < now,
        or_(MessageEvent.occurred_at.is_(None), MessageEvent.occurred_at >= message_start),
    ).distinct()
    raw_keys = ('transport', 'self_wxid', 'peer_wxid', 'native_msg_id', 'sender_source',
                'native_system_notice', 'ordering_fields', 'ai_reply_action_id')
    query = select(MessageEvent.id, MessageEvent.conversation_id, MessageEvent.lead_id,
                   MessageEvent.sales_id, MessageEvent.occurred_at, MessageEvent.sender_role,
                   MessageEvent.message_type,
                   *[MessageEvent.raw_payload[key].label(key) for key in raw_keys],
                   MessageEvent.evidence['native_history_receipt_id'].label('receipt')).where(
        MessageEvent.ingested_at < now, MessageEvent.conversation_id.in_(relevant_conversations))
    # Never materialize chat bodies, audio HEX or full AI payloads for a counter.
    records = list(db.execute(query.limit(MAX_FACTS + 1)).mappings())
    if len(records) > MAX_FACTS:
        raise AppError('DASHBOARD_CAPACITY_EXCEEDED', '历史统计容量已达到上限，请联系维护人员扩展统计投影', 422)
    rows = [SimpleNamespace(**{key: value for key, value in record.items() if key not in raw_keys and key != 'receipt'},
            raw_payload={key: record[key] for key in raw_keys}, evidence={'native_history_receipt_id': record['receipt']})
            for record in records]
    action_ids = select(MessageEvent.raw_payload['ai_reply_action_id'].as_string()).where(
        MessageEvent.ingested_at < now, MessageEvent.conversation_id.in_(relevant_conversations)
    ).distinct()
    actions = {row.id: row for row in db.execute(select(ReplyAction.id, ReplyAction.conversation_id, ReplyAction.batch_id)
               .where(ReplyAction.id.in_(action_ids), ReplyAction.created_at < now))}
    acks = {row.reply_action_id: SimpleNamespace(reply_action_id=row.reply_action_id, send_result=row.send_result,
             action_phase=row.action_phase, evidence={'native_msg_id': row.native_msg_id})
            for row in db.execute(select(SentAck.reply_action_id, SentAck.send_result, SentAck.action_phase,
                 SentAck.evidence['native_msg_id'].label('native_msg_id')).where(
                 SentAck.reply_action_id.in_(action_ids), SentAck.created_at < now))}
    facts, message_issues = message_facts(rows, actions, acks, now)
    handoffs, reminders = defaultdict(list), defaultdict(list)
    for h in bounded_rows(db, select(HandoffEvent.conversation_id, HandoffEvent.trigger_message_event_ids,
                               HandoffEvent.closed_at).where(HandoffEvent.created_at < now,
                               HandoffEvent.conversation_id.in_(relevant_conversations))):
        handoffs[h.conversation_id].append(h)
    for r in bounded_rows(db, select(HumanReplyReminder.conversation_id, HumanReplyReminder.first_message_id,
                               HumanReplyReminder.closed_at).where(HumanReplyReminder.created_at < now,
                               HumanReplyReminder.conversation_id.in_(relevant_conversations))):
        reminders[r.conversation_id].append(r)

    def message_reason(selected, a, b):
        # Incoming lookback is necessary for the rolling-24h effective condition.
        reasons = {issue.reason for issue in message_issues if issue.applies(a - timedelta(hours=24), b, selected)}
        return '；'.join(sorted(reasons)) or None

    communication_sets = communication_population(facts, left, right, mode)
    conversation_facts, sales_conversations = defaultdict(list), defaultdict(set)
    for message in facts:
        conversation_facts[message.conversation_id].append(message)
        sales_conversations[message.sales_id or 'unassigned'].add(message.conversation_id)
    first_fact_at = min((m.at for m in facts), default=left)

    def summary(selected):
        def matches(owner):
            return selected == 'all' or (owner or 'unassigned') == selected
        new, total, new_missing, total_missing = 0, 0, False, False
        for lead in leads:
            owner, known = owner_at(lead, assignments[lead.id], right)
            total_missing |= selected != 'all' and not known
            total += int(matches(owner))
            if left <= aware(lead.created_at) < right:
                snapshot = creation.get(lead.id)
                if selected == 'all':
                    new += 1
                elif snapshot is None:
                    new_missing = True
                else:
                    new += int(matches(snapshot.get('initial_sales_id')))
        owner_reason = '缺少创建完成时的历史销售归属，不能用当前销售补算'
        passed = {lead for lead, (at, owner, _) in first_pass.items() if left <= at < right and matches(owner)}
        confirmed, cohort, invite_reason = invitation_cohort(invitations, passed_tasks, left, right, selected)
        if any(matches(tasks[tid].sales_id) for tid in missing_invitations):
            invite_reason = '部分已提交邀请缺少可靠时间及原始凭证'
        pass_missing = any(
            (left <= at < right and matches(owner)) or
            (lead_id in first_pass and left <= first_pass[lead_id][0] < right
             and at <= first_pass[lead_id][0] and matches(first_pass[lead_id][1]))
            for lead_id, owner, at in pass_issues
        )
        pass_reason = '存在缺少正式邀请来源的旧绑定，不能确认首次通过' if pass_missing else None
        comm_count, effective_count = communication_counts(communication_sets, selected)
        reason = message_reason(selected, left, right)
        comm, effective = metric(comm_count, reason), metric(effective_count, reason)
        # Mode deliberately does not filter the human-response or customer metrics.
        # Keep full chronological conversations when ownership changed, but do
        # not re-sort unrelated sales' message histories for every table row.
        response_facts = facts if selected == 'all' else [
            m for cid in sales_conversations[selected] for m in conversation_facts[cid]]
        response, completed, pending = human_response(response_facts, handoffs, left, right, selected, reminders)
        response_reason = message_reason(selected, first_fact_at, now)
        if response_reason:
            response, completed, pending = metric(reason=response_reason), None, None
        return {
            'new_customers': metric(new, owner_reason if new_missing else None),
            'total_customers': metric(total, '缺少客户历史销售归属' if total_missing else None),
            'friend_passes': metric(len(passed), pass_reason),
            'friend_pass_rate': ratio(metric(confirmed, invite_reason), metric(cohort, invite_reason)),
            'communication_customers': comm, 'effective_customers': effective,
            'effective_rate': ratio(effective, comm), 'human_response_seconds': response,
            'human_replied_rounds': completed, 'human_pending_rounds': pending,
        }

    overall = summary(sales_id)
    sales_rows = [{'sales_id': s.id, 'name': s.sales_name, 'enabled': s.enabled, 'metrics': summary(s.id)}
                  for s in sales if sales_id in {'all', s.id}]
    if sales_id in {'all', 'unassigned'}:
        sales_rows.append({'sales_id': 'unassigned', 'name': '未分配', 'enabled': True, 'metrics': summary('unassigned')})
    workers = {w.id: w for w in bounded(db, select(Worker))}
    states = dict.fromkeys(['running', 'paused', 'faulted', 'offline', 'unbound'], 0)
    unknown = 0
    for seller in sales:
        if seller.deleted_at or status_sales_id not in {'all', seller.id}:
            continue
        state = operational_status(workers.get(seller.worker_id), now=now) if seller.worker_id else 'unbound'
        if state in states:
            states[state] += 1
        else:
            unknown += 1
    vehicles = bounded(db, _vehicle_query())
    events = bounded(db, select(OperationLog).where(OperationLog.event_type.in_(['vehicle_created', 'vehicle_listed', 'vehicle_unlisted']),
        OperationLog.target_type == 'vehicle', OperationLog.target_id.in_(_vehicle_query().with_only_columns(KnowledgeItem.item_id)),
        OperationLog.created_at < now))
    sold, amount = vehicle_sales(events, vehicle_left, vehicle_right)
    created_ids = {e.target_id for e in events if e.event_type == 'vehicle_created'}
    if any(v.item_id not in created_ids and not (v.payload or {}).get('metadata', {}).get('dashboard_origin') for v in vehicles):
        sold = amount = metric(reason='部分旧车辆缺少初始状态及完整上下架历史')
    trend = []
    today = now.astimezone(ZONE).date()
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        a, b = period(day, day, now)
        comm, effective = communication(facts, a, b, sales_id, mode)
        reason = message_reason(sales_id, a, b)
        trend.append({'date': day.isoformat(), 'communication': None if reason else comm,
                      'effective': None if reason else effective})
    return {
        'as_of': now.isoformat(), 'timezone': 'Asia/Shanghai', 'start': left.isoformat(), 'end': right.isoformat(),
        'metrics': overall, 'sales': sales_rows,
        'sales_options': [{'id': s.id, 'name': s.sales_name, 'enabled': s.enabled} for s in sales],
        'realtime': {'counts': states, 'unavailable': unknown},
        'vehicles': {'listed': metric(sum(v.status != 'archived' for v in vehicles)), 'sold': sold, 'amount': amount},
        'trend': trend,
        'coverage': ['仅统计系统授权并可靠采集到的消息；不覆盖销售全部微信聊天',
                     '历史好友通过仅覆盖留有正式邀请及绑定证据的 Hook 客户'],
    }
