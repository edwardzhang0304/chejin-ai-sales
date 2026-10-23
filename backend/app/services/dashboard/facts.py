"""Translate retained domain evidence into report facts without running business actions."""
from dataclasses import dataclass
from datetime import datetime

from app.services.dashboard.calculations import MessageFact, aware


@dataclass(frozen=True)
class CoverageIssue:
    reason: str
    sales_id: str | None
    at: datetime | None

    def applies(self, start, end, sales_id):
        return (sales_id == 'all' or (self.sales_id or 'unassigned') == sales_id) and (
            self.at is None or start <= self.at < end
        )


def message_facts(rows, actions, acks, now):
    """Native identity dedup survives rebinding and voice transcript updates.

    Prefer complete copies, but conflicting customer/source/time attribution is
    unavailable rather than choosing whichever database row happened to arrive first.
    """
    groups, issues = {}, []
    for row in rows:
        raw, evidence = row.raw_payload or {}, row.evidence or {}
        if raw.get('native_system_notice') or row.message_type not in {'text', 'voice', 'image'}:
            continue
        at = aware(row.occurred_at)
        if at and at >= now:
            continue
        identity = tuple(str(raw.get(k) or '') for k in ('self_wxid', 'peer_wxid', 'native_msg_id'))
        reason = None
        if raw.get('transport') != 'hook':
            reason = '历史消息缺少统一原生身份及来源证明'
        elif not row.lead_id or not at or not all(identity) or identity[-1] == '0':
            reason = '部分消息缺少可靠身份、客户归属或发生时间'
        source = 'customer' if row.sender_role == 'customer' else raw.get('sender_source')
        if not reason and source == 'ai':
            action_id = raw.get('ai_reply_action_id')
            action, ack = actions.get(action_id), acks.get(action_id)
            if (not action or not ack or ack.send_result != 'sent' or ack.action_phase != 'confirmed'
                    or str((ack.evidence or {}).get('native_msg_id')) != identity[-1]
                    or action.conversation_id != row.conversation_id):
                reason = '存在发送结果尚未确认的消息，未计作成功沟通'
        elif not reason and source == 'human':
            if not evidence.get('native_history_receipt_id'):
                reason = '人工消息缺少可靠历史确认'
        elif not reason and source != 'customer':
            reason = '部分发出消息来源或结果尚未确认'
        if reason:
            issues.append(CoverageIssue(reason, row.sales_id, at))
            continue
        ordering = raw.get('ordering_fields')
        sequence = str(ordering.get('Sequence') or '') if isinstance(ordering, dict) else ''
        order = int(sequence) if sequence.isascii() and sequence.isdigit() and int(sequence) > 0 else None
        fact = MessageFact(identity, row.lead_id, row.conversation_id, row.sales_id, at, source,
                           order, frozenset([row.id]),
                           getattr(actions.get(raw.get('ai_reply_action_id')), 'batch_id', None) or raw.get('ai_reply_action_id'))
        previous = groups.get(identity)
        if previous:
            if (previous.lead_id, previous.sales_id, previous.at, previous.source) != (
                    fact.lead_id, fact.sales_id, fact.at, fact.source):
                issues.append(CoverageIssue('同一原生消息的客户、销售或来源记录不一致', row.sales_id, at))
                continue
            from dataclasses import replace
            fact = replace(previous, event_ids=previous.event_ids | fact.event_ids)
        groups[identity] = fact
    return list(groups.values()), issues


@dataclass(frozen=True)
class InvitationFact:
    task_id: str
    lead_id: str
    sales_id: str | None
    earliest: datetime
    latest: datetime

    def within(self, start, end):
        return start <= self.earliest <= self.latest < end

    def overlaps_boundary(self, start, end):
        return self.earliest < end and self.latest >= start and not self.within(start, end)


def invitation_facts(tasks, proofs):
    facts, accepted = [], {}
    for proof in proofs:
        data, task = proof.extra_metadata or {}, tasks.get(proof.task_id)
        if (not task or task.status != 'completed' or task.result_code != 'invite_sent'
                or proof.worker_id != task.worker_id or data.get('backend_validated') is not True
                or data.get('schema_version') != 'hook-invite-proof-v1'
                or data.get('invitation_submitted') is not True or data.get('shortcode_written') is not True):
            continue
        try:
            attempted = aware(datetime.fromisoformat(data['attempted_at']))
            saved = aware(proof.created_at)
            if attempted > saved:
                continue
            try:
                submitted = aware(datetime.fromisoformat(data['submitted_at']))
            except (ValueError, KeyError, TypeError):
                submitted = None
            # Optional reporting time never changes the accepted business proof.
            # If it cannot be trusted, retain the original bounded interval.
            if submitted is not None and not attempted <= submitted <= saved:
                submitted = None
            latest = submitted or saved
            earliest = submitted or attempted
        except (ValueError, KeyError, TypeError):
            continue
        facts.append(InvitationFact(task.id, task.lead_id, task.sales_id, earliest, latest))
        accepted[proof.id] = task
    return facts, accepted


def friend_pass_facts(bindings, accepted):
    first, passed_tasks, issues = {}, set(), set()
    for binding in bindings:
        ctx = binding.native_context or {}
        task = accepted.get(ctx.get('invite_evidence_id'))
        if (not task or ctx.get('source_task_id') != task.id or task.lead_id != binding.lead_id
                or task.sales_id != binding.sales_id or task.worker_id != binding.worker_id
                or not ctx.get('binding_evidence_digest')):
            if binding.lead_id and binding.bind_status in {'bound', 'disabled', 'replaced'}:
                issues.add((binding.lead_id, binding.sales_id, aware(binding.created_at)))
            continue
        passed_tasks.add(task.id)
        at = aware(binding.created_at)
        value = (at, task.sales_id, task.id)
        if binding.lead_id not in first or at < first[binding.lead_id][0]:
            first[binding.lead_id] = value
    return first, passed_tasks, issues


def invitation_cohort(facts, passed_tasks, start, end, sales_id):
    selected = [f for f in facts if sales_id == 'all' or (f.sales_id or 'unassigned') == sales_id]
    if any(f.overlaps_boundary(start, end) for f in selected):
        return None, None, '旧邀请只保留操作前后时间，跨越所选日期边界，无法精确归期'
    included = [f for f in selected if f.within(start, end)]
    return len({f.lead_id for f in included if f.task_id in passed_tasks}), len({f.lead_id for f in included}), None
