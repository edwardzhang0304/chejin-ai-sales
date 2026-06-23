from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.services.workflow_service import (
    WorkflowService,
    build_template_item,
    compact_text,
    contains_metadata_pollution,
    digest,
    extract_dialogue_candidates,
    iter_source_records,
    looks_like_noise,
    normalize_candidate,
    sanitize_text,
    tenant_context,
)
from apps.wechat_ai_customer_service.knowledge_paths import tenant_root
from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore, write_json_with_retry
from apps.wechat_ai_customer_service.workflows.rag_layer import RagService
from apps.wechat_ai_customer_service.workflows.real_chat_learning import (
    formal_chat_item_to_experience,
    formal_chat_item_to_style_example,
    merge_records_by_id,
    read_json as read_json_default,
    write_jsonl as write_learning_jsonl,
)


FINANCE_TERMS = ("贷款", "分期", "首付", "月供", "征信", "按揭", "利率", "零首付")
DEPOSIT_TERMS = ("定金", "订金", "留车", "留车意向", "先付")
TRANSFER_TERMS = ("过户", "上牌", "迁入", "提档")
CONDITION_TERMS = ("事故", "水泡", "火烧", "检测", "车况", "查博士")
PRICE_TERMS = ("最低价", "底价", "便宜", "优惠", "砍价", "少点", "多少能出", "多少钱")

RISK_PROMISE_TERMS = ("零首付", "包过", "包上牌", "绝对", "保证", "全网最低", "最低价")
EXPLICIT_HANDOFF_TERMS = ("转人工", "人工跟进", "请人工", "请联系顾问", "人工核实", "人工顾问", "一对一核实")

SCENARIO_SAFE_CAP = {
    "日常沟通": 160,
    "车辆推荐": 180,
    "约看车": 100,
    "车况解释": 70,
    "定金/成交": 90,
    "需求探询": 80,
    "开场破冰": 70,
    "价格谈判": 80,
    "售后服务": 40,
    "跟进催单": 20,
}


@dataclass
class Classified:
    kind: str
    reason: str
    reply: str


def contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def rewrite_finance_handoff() -> str:
    return "可以做分期，我先记录您的预算和车型；首付、利率和月供需要金融专员一对一核实后给您正式方案，请联系顾问。"


def rewrite_deposit_handoff() -> str:
    return "可以先登记留车意向，定金金额、保留时效和退订规则需要人工顾问确认，我先转人工跟进。"


def rewrite_transfer_handoff() -> str:
    return "过户和上牌我们可以协助办理，具体费用和当地迁入政策需要人工顾问核实后确认，请联系顾问。"


def rewrite_price_neutral() -> str:
    return "价格会结合车况、付款方式和是否置换综合评估，我先按您的预算推荐2到3台，具体优惠请联系顾问一对一核实。"


def rewrite_condition_safe() -> str:
    return "车况以检测报告和合同为准，支持第三方复检；如涉及事故赔付等问题需要人工核实。"


def classify_and_rewrite(customer: str, reply: str) -> Classified:
    joined = f"{customer} {reply}"
    finance_topic = contains_any(joined, FINANCE_TERMS)
    deposit_topic = contains_any(joined, DEPOSIT_TERMS)
    transfer_topic = contains_any(joined, TRANSFER_TERMS)
    condition_topic = contains_any(joined, CONDITION_TERMS)
    price_topic = contains_any(joined, PRICE_TERMS)

    reply_has_handoff = contains_any(reply, EXPLICIT_HANDOFF_TERMS)
    reply_has_risk_promise = contains_any(reply, RISK_PROMISE_TERMS)

    finance_question_condition_answer = (
        contains_any(customer, FINANCE_TERMS)
        and contains_any(reply, CONDITION_TERMS)
        and not contains_any(reply, FINANCE_TERMS)
    )
    if finance_question_condition_answer:
        return Classified(kind="review", reason="intent_mismatch_finance_vs_condition", reply=reply)

    if finance_topic and (reply_has_risk_promise or "零首付" in reply or "首付" in customer):
        return Classified(kind="handoff", reason="finance_high_risk_rewrite", reply=rewrite_finance_handoff())

    if deposit_topic and not reply_has_handoff:
        return Classified(kind="handoff", reason="deposit_requires_human_rewrite", reply=rewrite_deposit_handoff())

    if transfer_topic and ("包过" in reply or "包上牌" in reply):
        return Classified(kind="handoff", reason="transfer_overpromise_rewrite", reply=rewrite_transfer_handoff())

    if price_topic and any(token in reply for token in ("全网最低", "最低价", "绝对")):
        return Classified(kind="safe", reason="price_absolute_softened", reply=rewrite_price_neutral())

    if condition_topic and any(token in reply for token in ("绝对", "保证无事故", "100%")):
        return Classified(kind="handoff", reason="condition_overpromise_rewrite", reply=rewrite_condition_safe())

    if reply_has_handoff:
        return Classified(kind="handoff", reason="explicit_handoff", reply=reply)

    if reply_has_risk_promise:
        if finance_topic:
            return Classified(kind="handoff", reason="finance_risk_promise", reply=rewrite_finance_handoff())
        if transfer_topic:
            return Classified(kind="handoff", reason="transfer_risk_promise", reply=rewrite_transfer_handoff())
        if price_topic:
            return Classified(kind="safe", reason="price_risk_softened", reply=rewrite_price_neutral())
        return Classified(kind="review", reason="unresolved_risk_promise", reply=reply)

    return Classified(kind="safe", reason="normal", reply=reply)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clean and integrate chejin real chat template pack.")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--industry-id", default="used_car")
    parser.add_argument("--input-file", required=True)
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--apply-rag", action="store_true", help="write cleaned real-chat samples to RAG/style learning layers")
    parser.add_argument("--apply-import", action="store_true")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    tenant_id = str(args.tenant_id).strip()
    industry_id = str(args.industry_id).strip() or "used_car"
    input_file = Path(str(args.input_file)).expanduser().resolve()
    if not input_file.exists():
        print(json.dumps({"ok": False, "message": f"input_file not found: {input_file}"}, ensure_ascii=True))
        return 2

    batch_id = str(args.batch_id).strip() or f"chejin_realchat_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    workflow = WorkflowService(tenant_id=tenant_id)

    with tenant_context(tenant_id):
        tenant_learning_root = Path("apps/wechat_ai_customer_service/data/tenants") / tenant_id / "learning_packs"
        curated_root = tenant_learning_root / "curated_templates"
        report_root = tenant_learning_root / "curation_reports"
        curated_path = curated_root / f"templates_{batch_id}_cleaned.jsonl"
        handoff_path = curated_root / f"templates_{batch_id}_handoff.jsonl"
        review_path = report_root / f"templates_{batch_id}_review.jsonl"
        report_path = report_root / f"curation_report_{batch_id}_cleaned.json"

        aggregate: dict[str, dict[str, Any]] = {}
        review_rows: list[dict[str, Any]] = []
        raw_count = 0
        accepted_count = 0
        rejected_count = 0
        reason_counter: dict[str, int] = defaultdict(int)

        for record in iter_source_records(input_file):
            for candidate in extract_dialogue_candidates(record):
                raw_count += 1
                normalized = normalize_candidate(candidate)
                if normalized is None:
                    rejected_count += 1
                    reason_counter["normalize_candidate_failed"] += 1
                    continue

                customer_raw = str(normalized.get("customer_message") or "")
                reply_raw = str(normalized.get("service_reply") or "")
                if looks_like_noise(customer_raw, reply_raw):
                    rejected_count += 1
                    reason_counter["noise_or_system_text"] += 1
                    continue
                if contains_metadata_pollution(reply_raw):
                    rejected_count += 1
                    reason_counter["metadata_pollution"] += 1
                    continue

                customer, _, _ = sanitize_text(customer_raw)
                reply, _, _ = sanitize_text(reply_raw)
                customer = compact_text(customer, limit=180)
                reply = compact_text(reply, limit=260)
                if len(customer) < 2 or len(reply) < 2:
                    rejected_count += 1
                    reason_counter["too_short_after_sanitize"] += 1
                    continue

                classified = classify_and_rewrite(customer, reply)
                reason_counter[classified.reason] += 1
                if classified.kind == "review":
                    review_rows.append(
                        {
                            "reason": classified.reason,
                            "scenario": str(normalized.get("scenario") or "").strip(),
                            "customer_message": customer,
                            "service_reply": reply,
                            "source_hint_id": str(normalized.get("id") or ""),
                            "hit_count": int(normalized.get("hit_count") or 1),
                        }
                    )
                    continue

                cleaned_reply = compact_text(classified.reply, limit=260)
                key = digest(f"{customer}||{cleaned_reply}||{classified.kind}", length=20)
                slot = aggregate.get(key)
                if slot is None:
                    slot = {
                        "kind": classified.kind,
                        "scenario": str(normalized.get("scenario") or "").strip() or "未分类",
                        "customer_message": customer,
                        "service_reply": cleaned_reply,
                        "intent_tags": list(normalized.get("intent_tags") or []),
                        "tone_tags": list(normalized.get("tone_tags") or []),
                        "hit_count": 0,
                        "source_hint_id": str(normalized.get("id") or ""),
                        "source_samples": [],
                    }
                    aggregate[key] = slot
                slot["hit_count"] += max(1, int(normalized.get("hit_count") or 1))
                for sample in list(normalized.get("source_samples") or []):
                    sample_text = compact_text(str(sample or ""), limit=140)
                    if sample_text and sample_text not in slot["source_samples"]:
                        slot["source_samples"].append(sample_text)
                accepted_count += 1

        safe_rows = [item for item in aggregate.values() if item["kind"] == "safe"]
        handoff_rows = [item for item in aggregate.values() if item["kind"] == "handoff"]

        grouped_safe: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in safe_rows:
            grouped_safe[row["scenario"]].append(row)
        selected_safe: list[dict[str, Any]] = []
        for scenario, rows in grouped_safe.items():
            rows.sort(key=lambda item: (int(item.get("hit_count") or 0), len(str(item.get("customer_message") or ""))), reverse=True)
            cap = SCENARIO_SAFE_CAP.get(scenario, 40)
            selected_safe.extend(rows[:cap])

        selected_handoff = sorted(
            handoff_rows,
            key=lambda item: (int(item.get("hit_count") or 0), len(str(item.get("customer_message") or ""))),
            reverse=True,
        )

        ready_items: list[dict[str, Any]] = []
        handoff_ready_items: list[dict[str, Any]] = []

        for row in selected_safe + selected_handoff:
            row_kind = str(row.get("kind") or "safe")
            reply_text = str(row.get("service_reply") or "")
            if row_kind == "handoff" and not contains_any(reply_text, EXPLICIT_HANDOFF_TERMS):
                reply_text = f"{reply_text} 请联系顾问。"

            item_id_seed = f"{row.get('scenario','')}-{row.get('customer_message','')}-{reply_text}-{row_kind}"
            hint_id = f"chejin_real_{digest(item_id_seed, length=14)}"
            item = build_template_item(
                category_id="chats",
                customer_message=str(row.get("customer_message") or ""),
                service_reply=reply_text,
                batch_token=batch_id,
                industry_id=industry_id,
                hint_id=hint_id,
                intent_tags=list(row.get("intent_tags") or []),
                tone_tags=list(row.get("tone_tags") or []),
                scenario=str(row.get("scenario") or ""),
                hit_count=int(row.get("hit_count") or 1),
                source_samples=list(row.get("source_samples") or []),
            )
            item["data"]["additional_details"]["source_hint_id"] = str(row.get("source_hint_id") or "")
            item["data"]["additional_details"]["cleaning_kind"] = row_kind
            if row_kind == "handoff":
                item["runtime"]["allow_auto_reply"] = False
                item["runtime"]["requires_handoff"] = True
                item["runtime"]["risk_level"] = "high"
                handoff_ready_items.append(item)
            else:
                item["runtime"]["allow_auto_reply"] = True
                item["runtime"]["requires_handoff"] = False
                item["runtime"]["risk_level"] = "normal"
            ready_items.append(item)

        ready_items.sort(
            key=lambda item: (
                str(((item.get("data") or {}).get("additional_details") or {}).get("scenario") or ""),
                -int((((item.get("data") or {}).get("additional_details") or {}).get("hit_count") or 0)),
            )
        )
        handoff_ready_items.sort(
            key=lambda item: -int((((item.get("data") or {}).get("additional_details") or {}).get("hit_count") or 0))
        )
        review_rows.sort(key=lambda row: int(row.get("hit_count") or 0), reverse=True)

        write_jsonl(curated_path, ready_items)
        write_jsonl(handoff_path, handoff_ready_items)
        write_jsonl(review_path, review_rows)

        summary = {
            "ok": True,
            "tenant_id": tenant_id,
            "industry_id": industry_id,
            "batch_id": batch_id,
            "input_file": str(input_file),
            "raw_candidates": raw_count,
            "accepted_after_cleaning": accepted_count,
            "rejected": rejected_count,
            "safe_selected": len(selected_safe),
            "handoff_selected": len(selected_handoff),
            "review_count": len(review_rows),
            "ready_item_count": len(ready_items),
            "reason_counter": dict(sorted(reason_counter.items(), key=lambda kv: kv[0])),
            "outputs": {
                "curated_file": str(curated_path),
                "handoff_file": str(handoff_path),
                "review_file": str(review_path),
            },
            "import": {},
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

        dry_run = workflow.dry_run_import(
            tenant_id=tenant_id,
            industry_id=industry_id,
            input_file=str(curated_path),
        )
        summary["import"]["dry_run"] = {
            "ok": bool(dry_run.get("ok")),
            "job_id": dry_run.get("job_id"),
            "summary": dry_run.get("summary"),
            "blocked_items": len(list(dry_run.get("blocked_items") or [])),
        }

        apply_rag = bool(args.apply_rag or args.apply_import)
        if apply_rag:
            summary["learning_apply"] = apply_real_chat_learning_layers(
                tenant_id=tenant_id,
                batch_id=batch_id,
                source_file=str(curated_path),
                ready_items=ready_items,
            )
            if args.apply_import:
                summary["import"]["apply"] = {
                    "ok": False,
                    "status": "redirected_to_rag_first",
                    "reason": "--apply-import is deprecated for real-chat packs; samples were written to RAG/style learning layers instead.",
                }
            else:
                summary["import"]["apply"] = {
                    "ok": False,
                    "status": "skipped",
                    "reason": "real_chat_rag_first_policy",
                }
        else:
            summary["import"]["apply"] = {
                "ok": False,
                "status": "skipped",
                "reason": "apply_not_requested; use --apply-rag for learning-layer ingest",
            }

        write_json(report_path, summary)
        print(json.dumps(summary, ensure_ascii=True))
    return 0

def apply_real_chat_learning_layers(
    *,
    tenant_id: str,
    batch_id: str,
    source_file: str,
    ready_items: list[dict[str, Any]],
) -> dict[str, Any]:
    migration_id = f"{batch_id}_rag_first_script"
    experiences = [
        record
        for item in ready_items
        if (record := formal_chat_item_to_experience(item, tenant_id=tenant_id, migration_id=migration_id, source_file=source_file))
    ]
    style_examples = [
        record
        for item in ready_items
        if (record := formal_chat_item_to_style_example(item, tenant_id=tenant_id, migration_id=migration_id, source_file=source_file))
    ]

    store = RagExperienceStore(tenant_id=tenant_id)
    existing_experiences = read_json_default(store.path, [])
    if not isinstance(existing_experiences, list):
        existing_experiences = []
    merged_experiences = merge_records_by_id(existing_experiences, experiences, "experience_id")
    write_json_with_retry(store.path, merged_experiences)

    style_path = tenant_root(tenant_id) / "style_memory" / "examples.jsonl"
    existing_style = read_jsonl_rows(style_path)
    merged_style = merge_records_by_id(existing_style, style_examples, "id")
    write_learning_jsonl(style_path, merged_style)
    index = RagService(tenant_id=tenant_id).rebuild_index()
    return {
        "ok": True,
        "policy": "real_chat_rag_first",
        "migration_id": migration_id,
        "rag_experience_written": len(experiences),
        "rag_experience_total": len(merged_experiences),
        "style_examples_written": len(style_examples),
        "style_examples_total": len(merged_style),
        "style_path": str(style_path),
        "rag_index": index,
    }


def read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
