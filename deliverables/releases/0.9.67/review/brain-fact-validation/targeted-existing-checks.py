import importlib.util
import json
from pathlib import Path
import sys
root = Path(__file__).resolve().parents[5]
path = root / 'worker-client/omniauto-rpa/apps/wechat_ai_customer_service/tests/run_customer_service_brain_contract_checks.py'
spec = importlib.util.spec_from_file_location('chejin_targeted_brain_checks', path)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
names = '''check_rejects_style_only_price_fact
check_requires_fact_claims_for_factual_modes
check_accepts_common_sense_compare_without_fact_claims
check_drops_non_authoritative_advice_boundary_fact_claims
check_drops_chinese_style_only_process_notes_without_authorizing_facts
check_rejects_chinese_style_only_authority_fact
check_current_conversation_can_authorize_product_reference_not_price
check_common_sense_brain_plan_uses_guard_advisor_mode
check_formal_policy_source_id_prefixes_validate_against_evidence
check_semantic_reviewer_authority_summary_reads_evidence_formal_ids
check_semantic_reviewer_keeps_soft_no_evidence_advisory
check_semantic_reviewer_accepts_fact_free_vehicle_need_clarification
check_brain_runner_sends_fact_free_vehicle_need_clarification_without_catalog
check_social_brain_plan_clears_soft_no_evidence_guard
check_social_visible_contract_rejects_empty_or_handoff_plan
check_quality_gate_warns_thin_social_or_common_sense_reply_without_blocking
check_rejects_product_scoped_master_fact
check_rejects_formal_policy_fact_without_source_id
check_semantic_reviewer_relaxes_safe_common_sense_boundary_concern
check_brain_candidate_allows_safe_uncertain_send
check_guard_downgrades_safe_uncertain_handoff_plan
check_safe_uncertain_reply_budget_restatement_needs_no_product_fact
check_uncertainty_boundary_condition_claim_is_not_product_fact
check_guard_allows_detail_topic_clarification_without_fact_claim
check_guard_allows_customer_data_ack_with_conversation_fact
check_guard_allows_customer_data_ack_with_message_id_conversation_fact'''.splitlines()
results=[]
for name in names:
    try:
        result = getattr(m, name)()
        results.append({'name':name,'ok':result.ok})
    except Exception as exc:
        results.append({'name':name,'ok':False,'error':str(exc)})
result={'scope':'26 existing affected offline checks, not the full Brain suite','results':results, 'passed':sum(x['ok'] for x in results),'total':len(results)}
(root/'deliverables/releases/0.9.67/review/brain-fact-validation/targeted-existing-checks.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False))
sys.exit(0 if all(x['ok'] for x in results) else 1)
