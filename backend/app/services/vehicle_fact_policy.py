"""Server-owned eligibility for ignoring only catalog-image presentation order."""
import re
from app.contracts.shared_rules import shared_adapter

POLICY = {'version': 2, 'mode': 'text_no_image_order_v1', 'image_order_independent': True}


def is_image_order_independent(payload):
    return isinstance(payload,dict) and payload.get('vehicle_fact_policy') == POLICY


def policy_for_generated_group(context, payload, segments):
    """Fail closed on missing runtime evidence or any media/reference dependency."""
    raw=payload.get('raw_payload')
    result=raw.get('omniauto_brain_result') if isinstance(raw,dict) else None
    if not isinstance(result,dict):
        return {}
    summary=result.get('brain_input_summary')
    proof=summary.get('input_dependency_evidence') if isinstance(summary,dict) else None
    plan=result.get('brain_plan')
    if not isinstance(proof,dict) or not isinstance(plan,dict):
        return {}
    snapshot=context.get('brain_context_snapshot') or {}
    rows=[*(context.get('messages') or []),*(snapshot.get('prior_messages') or [])]
    if (type(proof.get('version')) is not int or proof['version'] != 1
            or proof.get('complete') is not True or proof.get('image_dependency') is not False
            or not re.fullmatch('[0-9a-f]{64}',str(proof.get('input_sha256') or ''))
            or proof.get('conversation_id') != (context.get('conversation') or {}).get('conversation_id')
            or proof.get('message_ids') != [str(row['id']) for row in context.get('messages') or []]
            or snapshot.get('history_window_complete') is not True
            or not isinstance(plan.get('evidence_used'),dict) or not isinstance(plan.get('evidence_refs'),list)
            or not isinstance(plan.get('facts_claimed'),list)
            or not segments or any(not isinstance(part,str) or not part.strip() for part in segments)
            or any(row.get('message_type') != 'text' for row in rows)):
        return {}
    # All actions in this generated group use these exact text segments. Image
    # evidence/plan/reference in the full normalized plan still disqualifies it.
    rules=shared_adapter('image_order_dependencies')
    if rules.has_image_dependency({'messages':[row.get('content') for row in rows],'plan':plan,'segments':segments,'evidence_refs':payload.get('evidence_refs')}):
        return {}
    if any(not isinstance(ref,str) or not ref.startswith('product_master:') for ref in plan['evidence_refs']):
        return {}
    if any(not isinstance(fact,dict) or fact.get('source_level') != 'product_master' for fact in plan['facts_claimed']):
        return {}
    return dict(POLICY)
