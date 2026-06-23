"""Normalize customer-visible handoff templates to concealed phrasing.

This script rewrites tenant chat templates that are marked as handoff-required
so replies avoid explicit "transfer to human" wording and instead use
"请示/核实后回复" style.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


EXPLICIT_HANDOFF_PATTERNS = (
    r"转人工",
    r"人工客服",
    r"真人客服",
    r"线路切换",
    r"请联系顾问",
    r"(销售|顾问|同事|专员).{0,6}(联系|跟进|对接|接管)",
    r"(转给|交给).{0,6}(同事|顾问|专员|客服)",
)
PRICE_HARD_BOUNDARY_TERMS = ("价格", "报价", "最低", "优惠", "折扣", "少点", "便宜")
APPOINTMENT_TERMS = ("试驾", "到店", "看车", "订金", "定金", "留车", "锁车", "预约")
AFTER_SALES_TERMS = ("赔偿", "退款", "纠纷", "投诉", "事故", "水泡", "火烧", "过户", "上牌")


def has_explicit_handoff_phrase(text: str) -> bool:
    clean = re.sub(r"\s+", "", str(text or ""))
    if not clean:
        return False
    return any(re.search(pattern, clean, re.I) for pattern in EXPLICIT_HANDOFF_PATTERNS)


def concealed_handoff_reply(message: str) -> str:
    context = str(message or "")
    if any(term in context for term in PRICE_HARD_BOUNDARY_TERMS):
        return "这个价格我需要请示负责人确认一下，确认后第一时间给您准确答复。"
    if any(term in context for term in APPOINTMENT_TERMS):
        return "您这个安排我先帮您记录并确认排期，核实后第一时间回您。"
    if any(term in context for term in AFTER_SALES_TERMS):
        return "这类问题我需要先核实关键细节，再给您准确处理意见，请稍等我回复您。"
    return "这个问题我需要请示负责人确认一下。我先把您的需求记下，确认后第一时间回复您。"


def normalize_item(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}

    source_reply = str(data.get("service_reply") or "").strip()
    customer_message = str(data.get("customer_message") or "").strip()
    requires_handoff = bool(runtime.get("requires_handoff"))
    explicit_hit = has_explicit_handoff_phrase(source_reply)

    changed = False
    if (requires_handoff or explicit_hit) and (source_reply or explicit_hit):
        next_reply = concealed_handoff_reply(customer_message)
        if next_reply and next_reply != source_reply:
            data["service_reply"] = next_reply
            details = data.get("additional_details") if isinstance(data.get("additional_details"), dict) else {}
            workflow_import = str(details.get("workflow_import") or "").strip()
            if workflow_import and (workflow_import == source_reply or has_explicit_handoff_phrase(workflow_import)):
                details["workflow_import"] = next_reply
            data["additional_details"] = details
            payload["data"] = data
            metadata["updated_at"] = datetime.now().isoformat(timespec="seconds")
            metadata["updated_by"] = "handoff_concealment_normalizer"
            payload["metadata"] = metadata
            changed = True

    if changed:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "changed": changed,
        "requires_handoff": requires_handoff,
        "explicit_hit": explicit_hit,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, help="Tenant id under data/tenants.")
    args = parser.parse_args()

    root = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "tenants"
        / args.tenant_id
        / "knowledge_bases"
        / "chats"
        / "items"
    )
    if not root.exists():
        raise FileNotFoundError(f"chat items path not found: {root}")

    stats = {"total": 0, "requires_handoff": 0, "explicit_hit": 0, "changed": 0}
    for file_path in sorted(root.glob("*.json")):
        stats["total"] += 1
        result = normalize_item(file_path)
        if result["requires_handoff"]:
            stats["requires_handoff"] += 1
        if result["explicit_hit"]:
            stats["explicit_hit"] += 1
        if result["changed"]:
            stats["changed"] += 1

    print(json.dumps(stats, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
