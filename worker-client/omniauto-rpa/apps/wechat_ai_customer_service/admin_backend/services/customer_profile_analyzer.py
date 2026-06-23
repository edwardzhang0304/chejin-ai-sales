"""LLM-powered customer profile analyzer.

Reads recent conversation messages and uses a lightweight LLM call to infer
customer tags, intent, and summary. Runs asynchronously via the work queue.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from apps.wechat_ai_customer_service.admin_backend.services.customer_profile_store import CustomerProfileStore
from apps.wechat_ai_customer_service.admin_backend.services.raw_message_store import RawMessageStore
from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id
from apps.wechat_ai_customer_service.llm_config import (
    apply_llm_reasoning_effort,
    llm_urlopen,
    read_secret,
    resolve_deepseek_base_url,
    resolve_deepseek_model,
    resolve_deepseek_timeout,
)


DEFAULT_MAX_MESSAGES = 20
DEFAULT_MODEL = "deepseek-v4-flash"


class CustomerProfileAnalyzer:
    """Analyze customer conversations and update profile tags."""

    def __init__(
        self,
        *,
        tenant_id: str | None = None,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self.tenant_id = active_tenant_id(tenant_id)
        self.max_messages = max(1, max_messages)
        self.model = model
        self.profile_store = CustomerProfileStore(tenant_id=self.tenant_id)
        self.message_store = RawMessageStore(tenant_id=self.tenant_id)

    def analyze(self, target_name: str) -> dict[str, Any]:
        """Analyze conversation for a target and update their profile."""
        profile = self.profile_store.get_or_create(target_name=target_name, display_name=target_name)
        profile_id = str(profile.get("profile_id") or "")

        # Find conversation by target_name
        conversation_id = ""
        conversations = self.message_store.list_conversations(status="all")
        for conv in conversations:
            if str(conv.get("target_name") or "") == target_name:
                conversation_id = str(conv.get("conversation_id") or "")
                break

        # Load recent messages
        messages = []
        if conversation_id:
            messages = self.message_store.list_messages(
                conversation_id=conversation_id,
                limit=self.max_messages,
            )
        if not messages:
            return {"ok": True, "status": "skipped", "reason": "no_messages_found"}

        # Build conversation text
        conversation_text = self._format_messages(messages)
        existing_summary = str(profile.get("conversation_summary") or "").strip()
        existing_tags = profile.get("tags") if isinstance(profile.get("tags"), dict) else {}

        # Call LLM for analysis
        llm_result = self._call_llm(
            conversation_text=conversation_text,
            existing_summary=existing_summary,
            existing_tags=existing_tags,
        )
        if not llm_result.get("ok"):
            return {"ok": False, "error": llm_result.get("error", "llm_failed")}

        analysis = llm_result.get("analysis", {})
        if not analysis:
            return {"ok": True, "status": "skipped", "reason": "empty_analysis"}

        # Merge tags
        new_tags = dict(existing_tags)
        for key in ("intent_score", "budget_tier", "purchase_stage", "price_range_preference"):
            if key in analysis and analysis[key]:
                new_tags[key] = analysis[key]
        custom_tags = analysis.get("custom_tags")
        if isinstance(custom_tags, list):
            existing_custom = set(new_tags.get("custom_tags", [])) if isinstance(new_tags.get("custom_tags"), list) else set()
            existing_custom.update(str(t) for t in custom_tags if t)
            new_tags["custom_tags"] = sorted(existing_custom)
        preferred = analysis.get("preferred_vehicle_types")
        if isinstance(preferred, list):
            new_tags["preferred_vehicle_types"] = [str(p) for p in preferred if p]

        # Infer gender from messages if not already set with high confidence
        basic = dict(profile.get("basic_info") or {})
        current_gender = str(basic.get("gender") or "").strip()
        current_confidence = float(basic.get("gender_confidence") or 0.0)
        if (not current_gender or current_confidence < 0.7) and analysis.get("inferred_gender"):
            basic["gender"] = str(analysis["inferred_gender"])
            basic["gender_confidence"] = float(analysis.get("gender_confidence", 0.6))

        # Update summary
        new_summary = str(analysis.get("conversation_summary") or existing_summary).strip()

        # Update profile
        updated = self.profile_store.upsert_profile({
            "profile_id": profile_id,
            "target_name": target_name,
            "tags": new_tags,
            "basic_info": basic,
            "conversation_summary": new_summary,
        })

        return {
            "ok": True,
            "profile_id": profile_id,
            "updated_fields": list(analysis.keys()),
            "tag_count": len(new_tags),
        }

    def _format_messages(self, messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for msg in sorted(
            [m for m in messages if isinstance(m, dict)],
            key=lambda m: str(m.get("observed_at") or m.get("message_time") or ""),
        ):
            sender = str(msg.get("sender") or "客户")
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            lines.append(f"[{sender}] {content}")
        return "\n".join(lines)

    def _call_llm(
        self,
        *,
        conversation_text: str,
        existing_summary: str,
        existing_tags: dict[str, Any],
    ) -> dict[str, Any]:
        api_key = read_secret("DEEPSEEK_API_KEY") or ""
        base_url = resolve_deepseek_base_url()
        model = resolve_deepseek_model(explicit_model=self.model)
        timeout = resolve_deepseek_timeout(default=30)
        if not api_key:
            return {"ok": False, "error": "missing_deepseek_api_key"}

        system_prompt = (
            "你是一位专业的二手车销售客户画像分析师。"
            "请根据提供的微信聊天记录，分析客户的购买意向、预算、偏好等，"
            "输出JSON格式的分析结果。只输出JSON，不要任何解释。"
        )

        user_prompt = (
            f"【现有标签】{json.dumps(existing_tags, ensure_ascii=False)}\n"
            f"【现有摘要】{existing_summary}\n\n"
            f"【最近聊天记录】\n{conversation_text[:4000]}\n\n"
            "请输出JSON，字段如下:\n"
            "- intent_score: 0-100 整数，购买意向评分\n"
            "- budget_tier: 'low'/'mid'/'high'/'unknown'\n"
            "- purchase_stage: 'inquiry'/'comparison'/'decision'/'purchased'/'lost'\n"
            "- price_range_preference: 字符串，如'10-20万'\n"
            "- preferred_vehicle_types: 字符串数组，如['轿车','SUV']\n"
            "- custom_tags: 字符串数组，如['关注凯美瑞','问过分期']\n"
            "- inferred_gender: 'male'/'female'/'unknown'\n"
            "- gender_confidence: 0.0-1.0\n"
            "- conversation_summary: 一句话总结客户需求和状态\n"
        )

        url = base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 800,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        apply_llm_reasoning_effort(payload, tier="flash", read_secret_fn=read_secret)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with llm_urlopen(request, timeout=max(1, timeout)) as response:
                raw = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                analysis = json.loads(content) if content else {}
                return {"ok": True, "analysis": analysis, "usage": data.get("usage", {})}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return {"ok": False, "error": f"http_{exc.code}: {body[:500]}"}
        except Exception as exc:
            return {"ok": False, "error": repr(exc)}
