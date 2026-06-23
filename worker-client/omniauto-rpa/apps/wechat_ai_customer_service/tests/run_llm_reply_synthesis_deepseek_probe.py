"""Small live DeepSeek probe for the guarded reply synthesis layer.

This does not connect to WeChat. It makes one low-volume model call with a
synthetic used-car evidence pack that includes both formal product evidence and
RAG experience, then verifies the candidate is schema-shaped and guardable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
ADAPTERS_ROOT = APP_ROOT / "adapters"
for path in (PROJECT_ROOT, WORKFLOWS_ROOT, APP_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from llm_reply_guard import guard_synthesized_reply  # noqa: E402
from llm_reply_synthesis import synthesize_reply  # noqa: E402


def main() -> int:
    result = run_probe()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def run_probe() -> dict[str, Any]:
    settings = {
        "provider": "deepseek",
        "min_confidence": 0.35,
        "require_evidence": True,
        "require_structured_for_authority": True,
        "max_reply_chars": 520,
        "max_tokens": 3200,
        "timeout_seconds": 120,
    }
    evidence_pack = synthetic_used_car_pack()
    result = synthesize_reply(settings=settings, evidence_pack=evidence_pack)
    if not result.get("ok"):
        return {"ok": False, "stage": "llm_call", "result": redact_result(result)}
    candidate = result.get("candidate", {}) or {}
    guard = guard_synthesized_reply(candidate=candidate, evidence_pack=evidence_pack, settings=settings)
    ok = bool(guard.get("allowed")) and guard.get("action") in {"send_reply", "handoff"}
    return {
        "ok": ok,
        "stage": "guard",
        "provider": result.get("provider"),
        "model": result.get("model"),
        "candidate_summary": {
            "recommended_action": candidate.get("recommended_action"),
            "needs_handoff": candidate.get("needs_handoff"),
            "confidence": candidate.get("confidence"),
            "rag_used": candidate.get("rag_used"),
            "structured_used": candidate.get("structured_used"),
            "used_evidence": candidate.get("used_evidence", []),
            "reply": str(candidate.get("reply") or "")[:260],
        },
        "guard": {key: guard.get(key) for key in ("allowed", "action", "reason", "authority_tags")},
    }


def synthetic_used_car_pack() -> dict[str, Any]:
    rag_hit = {
        "chunk_id": "rag_chunk_used_car_family_scene",
        "source_id": "rag_source_family_scene",
        "score": 0.82,
        "category": "chats",
        "source_type": "rag_experience",
        "product_id": "chejin_camry_2021_20g",
        "text": "家庭客户关注接娃、油耗、省心和检测报告时，可以先解释凯美瑞空间、油耗、保养透明度，再提醒最终车况以检测报告为准。",
    }
    return {
        "schema_version": 1,
        "target": "文件传输助手",
        "current_message": "我老婆接娃开，别太费油，也别老出毛病，你看哪台合适？",
        "conversation": {
            "history": [
                {"sender": "customer", "content": "预算十来万，家用为主。"},
                {"sender": "self", "content": "可以先看凯美瑞、思域或秦PLUS。"},
            ],
            "history_count": 2,
        },
        "existing_reply": {
            "decision": {"rule_name": "no_rule_matched", "matched": False, "need_handoff": False},
            "reply_text": "这个问题我先帮您记录。",
        },
        "knowledge": {
            "intent_tags": ["scene_product", "spec"],
            "evidence": {
                "products": [
                    {
                        "id": "chejin_camry_2021_20g",
                        "name": "2021款丰田凯美瑞2.0G豪华版",
                        "category": "二手车/中级轿车",
                        "price": 13.98,
                        "stock": 1,
                        "warranty": "车况以检测报告为准，事故、水泡、火烧承诺必须人工确认。",
                    }
                ],
                "faq": [{"intent": "family_car", "answer": "适合家庭通勤，空间和保养成本较均衡。"}],
                "policies": {},
                "product_scoped": [],
                "style_examples": [],
            },
            "rag_evidence": {"hits": [rag_hit], "confidence": 0.82, "rag_can_authorize": False, "structured_priority": True},
            "safety": {"must_handoff": False, "allowed_auto_reply": True, "reasons": []},
        },
        "intent_tags": ["scene_product", "spec"],
        "safety": {"must_handoff": False, "allowed_auto_reply": True, "reasons": []},
        "rag": {"hits": [rag_hit], "confidence": 0.82},
        "evidence_ids": ["product:chejin_camry_2021_20g", "faq:family_car", "rag:rag_chunk_used_car_family_scene"],
        "audit_summary": {"structured_evidence_count": 2, "rag_hit_count": 1},
    }


def redact_result(result: dict[str, Any]) -> dict[str, Any]:
    output = dict(result)
    if "response_text" in output:
        output["response_text"] = str(output["response_text"] or "")[:300]
    return output


if __name__ == "__main__":
    raise SystemExit(main())
