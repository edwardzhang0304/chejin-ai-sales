"""One native boundary for profile succession and legacy owner migration.

The first complete validated snapshot is a conservative fence, not a timestamp
conversion. Ambiguous messages remain archived even after an operator resolves
the review. Resolving a review never sends, grants ownership or edits receipts.
"""
from sqlalchemy import select
from app.errors import AppError
from app.models.base import utcnow
from app.models.history_review import HistoryReview
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.lead import Lead
from app.contracts.shared_rules import shared_adapter


def needs_review(message):
    return bool((message.raw_payload or {}).get('history_review_id'))


def _reopen(review):
    review.revision += 1
    review.status = 'pending'
    review.note = review.resolved_by = review.resolved_at = None


def record_archived_change(db, message, *, evidence_key, receipt_id):
    review_id = (message.raw_payload or {}).get('history_review_id')
    review = db.scalar(select(HistoryReview).where(HistoryReview.id == review_id,
        HistoryReview.binding_id == message.binding_id).with_for_update())
    if review is None or message.id not in review.message_ids:
        raise AppError('HISTORY_REVIEW_NOT_FOUND', '原消息历史核对记录不存在', 409)
    # A new immutable receipt changes what the operator is reviewing. Duplicate
    # receipt delivery returns upstream before this function and stays idempotent.
    _reopen(review)
    review.evidence = {**review.evidence, evidence_key: receipt_id}


def _record(db, binding, ctx, rows, receipt, reason):
    key = shared_adapter('hook_contract_rules').digest([
        binding.id, ctx.get('account_epoch'), ctx.get('source_task_id'), 'native_boundary_v1'])
    review = db.scalar(select(HistoryReview).where(HistoryReview.binding_id == binding.id,
        HistoryReview.boundary_key == key).with_for_update())
    if review is None:
        review = HistoryReview(lead_id=binding.lead_id, binding_id=binding.id, boundary_key=key,
            reason=reason, evidence={'account_epoch': ctx.get('account_epoch'),
                'binding_revision': binding.authorization_revision,
                'previous_binding_id': ctx.get('previous_binding_id'),
                'source_task_id': ctx.get('source_task_id'), 'first_receipt_id': receipt.id},
            message_ids=[], resolutions=[])
        db.add(review); db.flush()
    existing_ids = set(review.message_ids)
    additions = [m.id for m in rows if m.id not in existing_ids]
    if additions:
        review.message_ids = list(review.message_ids) + additions
        # Previous signed resolutions are retained when late old facts arrive.
        _reopen(review)
    review.evidence = {**review.evidence, 'latest_receipt_id': receipt.id,
        'native_anchor': ctx.get('native_admission_anchor')}
    for message in rows:
        message.raw_payload = {**(message.raw_payload or {}), 'history_review_id': review.id}
    ctx['history_review_id'] = review.id
    return review


def admit(db, binding, conversation, ctx, checked, receipt, rows, *, facts_ready):
    legacy = (conversation.reply_ownership or {}).get('owner') == 'legacy'
    reason = ctx.get('native_boundary_review_reason') or ('legacy_reply_owner' if legacy else None)
    if not reason:
        return rows, None
    ctx['native_boundary_review_reason'] = reason
    anchor = ctx.get('native_admission_anchor')
    if anchor is None:
        if facts_ready and checked['semantic_rows']:
            # Only one active native database is ordered by the shared validator.
            # Freeze all observed rows; native Sequence and MsgSequence establish
            # later admission, never localId, callback time or server wall time.
            anchor = {'catalog_fingerprint': checked['catalog_fingerprint'],
                'account_epoch': ctx.get('account_epoch'), 'receipt_id': receipt.id,
                'sequence': max((int(r['Sequence']) for r in checked['semantic_rows']), default=0),
                'msg_sequence': max((int(r['MsgSequence']) for r in checked['semantic_rows']), default=0)}
            ctx['native_admission_anchor'] = anchor
        _record(db, binding, ctx, rows, receipt, reason)
        return [], None if facts_ready else 'HOOK_HISTORY_BOUNDARY_UNCONFIRMED'
    if (anchor['catalog_fingerprint'] != checked['catalog_fingerprint']
            or anchor['account_epoch'] != ctx.get('account_epoch')):
        _record(db, binding, ctx, rows, receipt, reason)
        return [], 'HOOK_HISTORY_BOUNDARY_GENERATION_CHANGED'
    if not facts_ready:
        return [], None
    positions = {r['MsgSvrID']: r for r in checked['semantic_rows']}
    accepted, uncertain = [], []
    for message in rows:
        row = positions[message.raw_payload['native_msg_id']]
        if (not needs_review(message) and facts_ready
                and int(row['Sequence']) > anchor['sequence']
                and int(row['MsgSequence']) > anchor['msg_sequence']):
            accepted.append(message)
        else:
            uncertain.append(message)
    if uncertain:
        _record(db, binding, ctx, uncertain, receipt, reason)
    return accepted, None


def list_reviews(db, lead_id):
    lead = db.get(Lead, lead_id)
    if lead is None or lead.deleted_at:
        raise AppError('LEAD_NOT_FOUND', '客户不存在', 404)
    reviews = list(db.scalars(select(HistoryReview).where(HistoryReview.lead_id == lead_id)
        .order_by(HistoryReview.created_at.desc())))
    result = []
    for review in reviews:
        messages = {m.id: m for m in db.scalars(select(MessageEvent).where(
            MessageEvent.id.in_(review.message_ids), MessageEvent.binding_id == review.binding_id))}
        result.append({'id': review.id, 'reason': review.reason, 'status': review.status,
            'revision': review.revision, 'created_at': review.created_at,
            'note': review.note, 'resolved_at': review.resolved_at,
            'messages': [{'id': mid, 'content': messages[mid].content,
                'message_type': messages[mid].message_type, 'sender_role': messages[mid].sender_role,
                'withdrawn': bool((messages[mid].raw_payload or {}).get('native_withdrawn'))
                    and not bool((messages[mid].raw_payload or {}).get('native_system_notice')),
                'system_notice': bool((messages[mid].raw_payload or {}).get('native_system_notice')),
                'occurred_at': messages[mid].occurred_at} for mid in review.message_ids if mid in messages]})
    return result


def resolve(db, lead_id, review_id, payload, actor):
    from app.services.followup_eligibility import lock_leads
    from app.services.audit_service import write_log
    lock_leads(db, [lead_id])
    review = db.scalar(select(HistoryReview).where(HistoryReview.id == review_id,
        HistoryReview.lead_id == lead_id).with_for_update())
    if review is None:
        raise AppError('HISTORY_REVIEW_NOT_FOUND', '历史核对事项不存在', 404)
    note = payload.note.strip()
    if not note:
        raise AppError('HISTORY_REVIEW_NOTE_REQUIRED', '请填写核对说明', 422)
    if review.status == 'processed' and review.revision == payload.revision and review.note == note:
        return {'processed': True}
    if review.status != 'pending' or review.revision != payload.revision:
        raise AppError('HISTORY_REVIEW_CHANGED', '待核对内容已更新，请刷新后核对', 409)
    now = utcnow()
    review.status, review.note = 'processed', note
    review.resolved_by, review.resolved_at = str(actor.operator_id), now
    review.resolutions = [*review.resolutions, {'revision': review.revision,
        'note': note, 'operator_id': str(actor.operator_id), 'at': now.isoformat(),
        'message_ids': list(review.message_ids)}]
    write_log(db, actor, event_type='history_review_processed', module='wechat',
        target_type='history_review', target_id=review.id, lead_id=lead_id,
        after_data={'revision': review.revision, 'status': 'processed'}, metadata={'note': note})
    db.flush()
    return {'processed': True}
