"""Product-centric admin helpers for merchant-friendly product operations."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.wechat_ai_customer_service.llm_config import (
    apply_llm_reasoning_effort,
    llm_urlopen,
    read_secret,
    resolve_deepseek_base_url,
    resolve_deepseek_max_tokens,
    resolve_deepseek_tier_model,
    resolve_deepseek_timeout,
)

from .knowledge_base_store import KnowledgeBaseStore
from .knowledge_compiler import KnowledgeCompiler
from .knowledge_generator import KnowledgeGenerator, extract_price_tiers, normalize_price_tiers, parse_product, parse_product_scoped
from .upload_store import UploadStore


PRODUCT_SCOPED_CATEGORIES = {
    "product_faq": "商品专属问答",
    "product_rules": "商品专属规则",
    "product_explanations": "商品专属解释",
}
LLM_INTENT_TO_PRODUCT_SCOPED_CATEGORY = {
    "create_product_faq": "product_faq",
    "create_product_rules": "product_rules",
    "create_product_explanations": "product_explanations",
}
BASE_REPLY_TEMPLATE_KEYS = {"default", "quote", "discount_policy", "logistics", "after_sales", "notes"}
SCOPED_KEYWORD_STOPWORDS = {
    "客户问",
    "客户问题",
    "问题",
    "回答",
    "回复",
    "标准回复",
    "专属问答",
    "专属规则",
    "专属解释",
    "咨询",
    "可以",
    "是否",
    "能否",
    "请问",
    "商品",
    "这个商品",
    "更新",
    "模板",
    "默认回复",
}
SCOPED_KEYWORD_HINTS = [
    "上门安装",
    "安装费",
    "人工客服",
    "转人工",
    "发货",
    "物流",
    "包邮",
    "质保",
    "保修",
    "退换",
    "最低价",
    "砍价",
    "发票",
    "库存",
    "现货",
    "到货",
]
QUANTITY_UNITS_PATTERN = r"(?:台|个|件|辆|套|只|箱|张|把|条|份|组|批)"


class ProductConsoleService:
    def __init__(self) -> None:
        self.store = KnowledgeBaseStore()
        self.compiler = KnowledgeCompiler()

    def catalog(self, *, include_archived: bool = False) -> dict[str, Any]:
        scoped_counts = self.product_scoped_counts()
        products = [
            self.enrich_product(
                item,
                scoped_counts=scoped_counts.get(str(item.get("id") or ""), {}),
            )
            for item in self.store.list_items("products", include_archived=include_archived)
        ]
        active_count = sum(1 for item in products if item.get("status") == "active")
        in_stock_count = sum(1 for item in products if item.get("stock_state") == "in_stock")
        sold_out_count = sum(1 for item in products if item.get("stock_state") == "sold_out")
        runtime_usable_count = sum(1 for item in products if item.get("runtime_usable"))
        unread_count = sum(1 for item in products if item.get("is_unread"))
        return {
            "ok": True,
            "items": products,
            "counts": {
                "total": len(products),
                "active": active_count,
                "in_stock": in_stock_count,
                "sold_out": sold_out_count,
                "archived": sum(1 for item in products if item.get("status") == "archived"),
                "runtime_usable": runtime_usable_count,
                "unread": unread_count,
            },
        }

    def detail(self, product_id: str) -> dict[str, Any]:
        item = self.get_product_item(product_id, include_archived=True)
        if not item:
            raise FileNotFoundError(product_id)
        scoped = self.product_scoped_knowledge(product_id)
        return {"ok": True, "item": self.enrich_product(item, scoped=scoped), "scoped_knowledge": scoped}

    def product_scoped_knowledge(self, product_id: str) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for category_id in PRODUCT_SCOPED_CATEGORIES:
            result[category_id] = [
                item
                for item in self.store.list_items(category_id, include_archived=False)
                if str((item.get("data") or {}).get("product_id") or "") == product_id
            ]
        return result

    def product_scoped_counts(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for category_id in PRODUCT_SCOPED_CATEGORIES:
            for item in self.store.list_items(category_id, include_archived=False):
                product_id = str((item.get("data") or {}).get("product_id") or "")
                if not product_id:
                    continue
                product_counts = counts.setdefault(product_id, {key: 0 for key in PRODUCT_SCOPED_CATEGORIES})
                product_counts[category_id] = product_counts.get(category_id, 0) + 1
        return counts

    def adjust_inventory(self, product_id: str, *, operation: str, quantity: int | None = None) -> dict[str, Any]:
        item = self.get_product_item(product_id, include_archived=True)
        if not item:
            raise FileNotFoundError(product_id)
        data = dict(item.get("data") or {})
        operation = str(operation or "").strip()
        current = to_int(data.get("inventory"), default=0)
        if operation == "set":
            data["inventory"] = max(0, int(quantity or 0))
            item["status"] = "active"
        elif operation == "increase":
            data["inventory"] = max(0, current + int(quantity or 1))
            item["status"] = "active"
        elif operation in {"decrease", "sell"}:
            data["inventory"] = max(0, current - int(quantity or 1))
            item["status"] = "active"
        elif operation == "archive":
            item["status"] = "archived"
        elif operation == "activate":
            item["status"] = "active"
        else:
            raise ValueError(f"unsupported operation: {operation}")
        item["data"] = data
        return self.save_product_item(item, operation=operation)

    def update_product(self, product_id: str, data_patch: dict[str, Any]) -> dict[str, Any]:
        item = self.get_product_item(product_id, include_archived=True)
        if not item:
            raise FileNotFoundError(product_id)
        data = dict(item.get("data") or {})
        patch = {key: value for key, value in data_patch.items() if value not in (None, "", [], {})}
        if isinstance(patch.get("reply_templates"), dict):
            existing_templates = data.get("reply_templates") if isinstance(data.get("reply_templates"), dict) else {}
            merged_templates = {str(key): value for key, value in existing_templates.items()}
            for key, value in (patch.get("reply_templates") or {}).items():
                scene = str(key or "").strip()
                reply = str(value or "").strip()
                if scene and reply:
                    merged_templates[scene] = reply
            patch["reply_templates"] = merged_templates
        if isinstance(patch.get("additional_details"), dict):
            existing_details = data.get("additional_details") if isinstance(data.get("additional_details"), dict) else {}
            merged_details = {str(key): value for key, value in existing_details.items()}
            for key, value in (patch.get("additional_details") or {}).items():
                detail_key = str(key or "").strip()
                if detail_key and value not in (None, "", [], {}):
                    merged_details[detail_key] = value
            patch["additional_details"] = merged_details
        data.update(patch)
        item["data"] = data
        return self.save_product_item(item, operation="update_product")

    def create_product_scoped_knowledge(
        self,
        *,
        category_id: str,
        target_product_id: str,
        target_product_name: str,
        data_patch: dict[str, Any],
        source_text: str,
    ) -> dict[str, Any]:
        if category_id not in PRODUCT_SCOPED_CATEGORIES:
            raise ValueError(f"unsupported product-scoped category: {category_id}")
        payload = normalize_product_scoped_data_fields(category_id, data_patch)
        payload["product_id"] = target_product_id
        if not payload.get("title"):
            payload["title"] = default_scoped_title(category_id, target_product_name or target_product_id)
        if category_id == "product_faq":
            payload["title"] = normalize_faq_title(
                title=str(payload.get("title") or ""),
                question=str(payload.get("question") or ""),
                product_name=target_product_name or target_product_id,
            )
        payload["keywords"] = auto_scoped_keywords(
            category_id=category_id,
            payload=payload,
            product_name=target_product_name or target_product_id,
        )
        if category_id == "product_explanations":
            content = str(payload.get("content") or "").strip()
            if not content:
                raise ValueError("说明内容不能为空，请补充后再确认。")
            payload["content"] = content
        else:
            answer = str(payload.get("answer") or "").strip()
            if not answer:
                raise ValueError("标准回复不能为空，请补充后再确认。")
            payload["answer"] = answer
        details = payload.get("additional_details") if isinstance(payload.get("additional_details"), dict) else {}
        if source_text.strip():
            details = {**details, "user_original_description": source_text.strip()}
        payload["additional_details"] = details
        item_id = build_scoped_item_id(
            category_id=category_id,
            product_id=target_product_id,
            title=str(payload.get("title") or ""),
            body=str(payload.get("answer") or payload.get("content") or ""),
        )
        item = {
            "id": item_id,
            "category_id": category_id,
            "status": "active",
            "source": {"type": "manual", "from": "product_console_llm"},
            "data": payload,
        }
        saved = self.store.save_item(category_id, item)
        if not saved.get("ok"):
            raise ValueError(saved.get("problems") or saved)
        self.compiler.compile_to_disk()
        return {
            "ok": True,
            "item": saved.get("item"),
            "operation": "create_product_scoped",
            "target_product_id": target_product_id,
            "target_product_name": target_product_name or target_product_id,
            "target_category": category_id,
        }

    def save_product_item(self, item: dict[str, Any], *, operation: str) -> dict[str, Any]:
        saved = self.store.save_item("products", item)
        if not saved.get("ok"):
            raise ValueError(saved)
        self.compiler.compile_to_disk()
        return {"ok": True, "item": self.enrich_product(saved["item"]), "operation": operation}

    def command(self, message: str, *, use_llm: bool = True, dry_run: bool = False) -> dict[str, Any]:
        text = str(message or "").strip()
        if not text:
            raise ValueError("message is required")
        # Explicit "new product" wording should start a draft flow even if the
        # message accidentally fuzzy-matches an existing catalog item.
        if has_any(text, "新增商品", "新商品", "新增车源"):
            if dry_run:
                return {
                    "ok": True,
                    "action": "draft_product",
                    "needs_confirmation": True,
                    "summary": "识别为新增商品请求，建议转入 AI 助手录入的商品草稿流程。",
                }
            session = KnowledgeGenerator().create_session(text, preferred_category_id="products", use_llm=use_llm)
            return {
                "ok": True,
                "action": "draft_product",
                "message": "已整理成商品草稿，请在商品库确认或修改后直接入库。",
                "session": session.get("session"),
            }
        products = self.store.list_items("products", include_archived=True)
        matched = match_product(text, products)
        extracted_patch = extract_product_patch(text)
        missing_patch_fields = detect_missing_update_fields(text, extracted_patch)
        has_mixed_inventory_patch = bool(extracted_patch) and "inventory" in extracted_patch and any(
            key != "inventory" for key in extracted_patch
        )
        llm_result = _call_llm_for_command(text, products, use_llm=use_llm)
        llm_execution = self._execute_llm_command(llm_result, products, command_text=text, dry_run=dry_run) if isinstance(llm_result, dict) else None
        if isinstance(llm_execution, dict):
            llm_action = str(llm_execution.get("action") or "")
            if not (matched and has_mixed_inventory_patch and llm_action in {"set_inventory", "increase_inventory", "decrease_inventory"}):
                return llm_execution
        if matched and extracted_patch and missing_patch_fields:
            raise ValueError(
                "信息还不完整，请补充：" + "、".join(dict.fromkeys(missing_patch_fields)) + "。"
            )
        if matched and has_mixed_inventory_patch:
            if dry_run:
                return build_command_plan(action="update_product", target_item=matched, fields=extracted_patch)
            return {
                "ok": True,
                "action": "update_product",
                **self.update_product(str(matched.get("id") or ""), extracted_patch),
                "updated_fields": sorted(extracted_patch),
            }
        if matched and means_archived_status(text):
            if dry_run:
                return build_command_plan(action="archive", target_item=matched)
            return {"ok": True, "action": "archive", **self.adjust_inventory(str(matched.get("id") or ""), operation="archive")}
        if matched and has_any(text, "卖掉", "卖出", "售出", "成交", "减少", "扣减"):
            quantity = parse_inventory_quantity(text, mode="decrease")
            if dry_run:
                return build_command_plan(action="decrease_inventory", target_item=matched, quantity=quantity or 1)
            return {
                "ok": True,
                "action": "decrease_inventory",
                **self.adjust_inventory(str(matched.get("id") or ""), operation="sell", quantity=quantity or 1),
            }
        if matched and means_inventory_zero(text):
            if dry_run:
                return build_command_plan(action="set_inventory", target_item=matched, quantity=0)
            return {"ok": True, "action": "set_inventory", **self.adjust_inventory(str(matched.get("id") or ""), operation="set", quantity=0)}
        if matched and has_any(text, "库存改", "库存设", "库存为", "还有", "现货"):
            quantity = parse_inventory_quantity(text, mode="set")
            if quantity is None:
                raise ValueError("请写清楚库存数量，例如：凯美瑞库存改成 2 台")
            if dry_run:
                return build_command_plan(action="set_inventory", target_item=matched, quantity=quantity)
            return {
                "ok": True,
                "action": "set_inventory",
                **self.adjust_inventory(str(matched.get("id") or ""), operation="set", quantity=quantity),
            }
        if matched and has_any(text, "增加", "补货", "到货", "入库"):
            quantity = parse_inventory_quantity(text, mode="increase")
            if quantity is None:
                raise ValueError("请写清楚增加数量，例如：凯美瑞补货 2 台")
            if dry_run:
                return build_command_plan(action="increase_inventory", target_item=matched, quantity=quantity)
            return {
                "ok": True,
                "action": "increase_inventory",
                **self.adjust_inventory(str(matched.get("id") or ""), operation="increase", quantity=quantity),
            }
        if matched:
            scoped_fallback = infer_product_scoped_from_text(text, matched)
            if scoped_fallback:
                action = str(scoped_fallback.get("action") or "")
                fields = scoped_fallback.get("fields") if isinstance(scoped_fallback.get("fields"), dict) else {}
                category_id = str(scoped_fallback.get("category_id") or "")
                if dry_run:
                    return build_command_plan(action=action, target_item=matched, fields=fields)
                target_name = str((matched.get("data") or {}).get("name") or matched.get("id") or "")
                result = self.create_product_scoped_knowledge(
                    category_id=category_id,
                    target_product_id=str(matched.get("id") or ""),
                    target_product_name=target_name,
                    data_patch=fields,
                    source_text=text,
                )
                return {
                    "ok": True,
                    "action": action,
                    **result,
                }
        if matched:
            if extracted_patch:
                if dry_run:
                    return build_command_plan(action="update_product", target_item=matched, fields=extracted_patch)
                return {
                    "ok": True,
                    "action": "update_product",
                    **self.update_product(str(matched.get("id") or ""), extracted_patch),
                    "updated_fields": sorted(extracted_patch),
                }
            if missing_patch_fields:
                raise ValueError(
                    "信息还不完整，请补充：" + "、".join(dict.fromkeys(missing_patch_fields)) + "。"
                )
        if has_any(text, "新增", "添加", "上架", "新商品", "新车源"):
            if dry_run:
                return {
                    "ok": True,
                    "action": "draft_product",
                    "needs_confirmation": True,
                    "summary": "识别为新增商品请求，建议转入 AI 助手录入的商品草稿流程。",
                }
            session = KnowledgeGenerator().create_session(text, preferred_category_id="products", use_llm=use_llm)
            return {
                "ok": True,
                "action": "draft_product",
                "message": "已整理成商品草稿，请在商品库确认或修改后直接入库。",
                "session": session.get("session"),
            }
        if not matched and has_update_intent(text):
            suggestions = suggest_products(text, products)
            if suggestions:
                raise ValueError(
                    "还没完全定位到商品，请确认是以下哪个："
                    + "、".join(suggestions)
                    + "。也可以直接补充 SKU。"
                )
            raise ValueError("还没识别到要修改的商品，请补充商品名称、SKU 或别名。")
        if mentions_product_scoped_intent(text):
            if not matched:
                suggestions = suggest_products(text, products)
                if suggestions:
                    raise ValueError(
                        "我理解你在录入商品专属问答/规则，但还没定位到商品。请确认是以下哪个："
                        + "、".join(suggestions)
                        + "。"
                    )
                raise ValueError("我理解你在录入商品专属问答/规则，但当前商品库里没匹配到对应商品。请先补充准确商品名或 SKU。")
            category_id = infer_product_scoped_category(text)
            if category_id == "product_explanations":
                raise ValueError("我理解你在录入商品专属解释。请至少补充“说明内容”。")
            raise ValueError("我理解你在录入商品专属问答/规则。请至少补充“回答内容”；如有客户问法也可以一起写上。")
        if isinstance(llm_result, dict):
            missing = str(llm_result.get("missing_info") or "")
            followup = str(llm_result.get("followup_question") or "").strip()
            confidence = float(llm_result.get("confidence") or 0)
            if followup:
                raise ValueError(followup)
            if missing and confidence >= 0.45:
                raise ValueError(f"LLM 识别到意图但信息不完整：{missing}")
        raise ValueError(build_clarifying_question(text, matched=matched, extracted_patch=extracted_patch, llm_result=llm_result))

    def _execute_llm_command(
        self,
        llm_result: dict[str, Any],
        products: list[dict[str, Any]],
        *,
        command_text: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any] | None:
        confidence = float(llm_result.get("confidence") or 0)
        if confidence < 0.66:
            return None
        intent = str(llm_result.get("intent") or "")
        if intent not in {
            "archive",
            "set_inventory",
            "increase_inventory",
            "decrease_inventory",
            "update_product",
            "create_product_faq",
            "create_product_rules",
            "create_product_explanations",
        }:
            return None

        target_id = str(llm_result.get("target_product_id") or "")
        target_name = str(llm_result.get("target_product_name") or "")
        if not target_id and target_name:
            matched = match_product(target_name, products)
            if matched:
                target_id = str(matched.get("id") or "")
        if target_id and not any(str(item.get("id") or "") == target_id for item in products):
            if target_name:
                matched = match_product(target_name, products)
                if matched:
                    target_id = str(matched.get("id") or "")
            if not any(str(item.get("id") or "") == target_id for item in products):
                return None
        if not target_id:
            return None
        if not target_name:
            target_name = product_name_by_id(products, target_id) or target_id

        quantity = llm_result.get("quantity")
        fields = normalize_llm_update_fields(llm_result.get("fields"))
        scoped_category_id = LLM_INTENT_TO_PRODUCT_SCOPED_CATEGORY.get(intent, "")
        scoped_fields: dict[str, Any] = {}
        if scoped_category_id:
            llm_scoped_fields = llm_result.get("scoped_fields")
            scoped_fields = normalize_product_scoped_data_fields(scoped_category_id, llm_scoped_fields if isinstance(llm_scoped_fields, dict) else llm_result.get("fields"))
            parsed_scoped = normalize_product_scoped_data_fields(scoped_category_id, parse_product_scoped(command_text, scoped_category_id))
            for key, value in parsed_scoped.items():
                if key not in scoped_fields:
                    scoped_fields[key] = value
            scoped_fields["product_id"] = target_id
            if not scoped_fields.get("title"):
                scoped_fields["title"] = default_scoped_title(scoped_category_id, target_name)
        if intent == "archive":
            if dry_run:
                return build_command_plan(action="archive", target_item={"id": target_id, "data": {"name": target_name}})
            result = self.adjust_inventory(target_id, operation="archive")
            return {"ok": True, "action": "archive", "llm_assisted": True, **result}
        if intent == "set_inventory":
            qty = to_int(quantity, default=-1)
            if qty < 0:
                return None
            if dry_run:
                return build_command_plan(action="set_inventory", target_item={"id": target_id, "data": {"name": target_name}}, quantity=qty, llm_assisted=True)
            result = self.adjust_inventory(target_id, operation="set", quantity=qty)
            return {"ok": True, "action": "set_inventory", "llm_assisted": True, **result}
        if intent == "increase_inventory":
            qty = to_int(quantity, default=-1)
            if qty <= 0:
                return None
            if dry_run:
                return build_command_plan(action="increase_inventory", target_item={"id": target_id, "data": {"name": target_name}}, quantity=qty, llm_assisted=True)
            result = self.adjust_inventory(target_id, operation="increase", quantity=qty)
            return {"ok": True, "action": "increase_inventory", "llm_assisted": True, **result}
        if intent == "decrease_inventory":
            qty = to_int(quantity, default=-1)
            if qty <= 0:
                return None
            if dry_run:
                return build_command_plan(action="decrease_inventory", target_item={"id": target_id, "data": {"name": target_name}}, quantity=qty, llm_assisted=True)
            result = self.adjust_inventory(target_id, operation="sell", quantity=qty)
            return {"ok": True, "action": "decrease_inventory", "llm_assisted": True, **result}
        if intent == "update_product" and fields:
            scoped_from_templates = maybe_reroute_reply_template_update(
                command_text=command_text,
                fields=fields,
                target_product_id=target_id,
                target_product_name=target_name,
            )
            if scoped_from_templates:
                reroute_action = str(scoped_from_templates.get("action") or "")
                reroute_category = str(scoped_from_templates.get("category_id") or "")
                reroute_fields = scoped_from_templates.get("fields") if isinstance(scoped_from_templates.get("fields"), dict) else {}
                if dry_run:
                    return build_command_plan(
                        action=reroute_action,
                        target_item={"id": target_id, "data": {"name": target_name}},
                        fields=reroute_fields,
                        llm_assisted=True,
                    )
                result = self.create_product_scoped_knowledge(
                    category_id=reroute_category,
                    target_product_id=target_id,
                    target_product_name=target_name,
                    data_patch=reroute_fields,
                    source_text=command_text,
                )
                return {
                    "ok": True,
                    "action": reroute_action,
                    "llm_assisted": True,
                    "rerouted_from": "update_product.reply_templates",
                    **result,
                }
            if dry_run:
                return build_command_plan(action="update_product", target_item={"id": target_id, "data": {"name": target_name}}, fields=fields, llm_assisted=True)
            result = self.update_product(target_id, fields)
            return {"ok": True, "action": "update_product", "llm_assisted": True, **result, "updated_fields": sorted(fields)}
        if intent in LLM_INTENT_TO_PRODUCT_SCOPED_CATEGORY and scoped_fields:
            if scoped_category_id == "product_explanations":
                if not str(scoped_fields.get("content") or "").strip():
                    return None
            else:
                if not str(scoped_fields.get("answer") or "").strip():
                    return None
            if dry_run:
                return build_command_plan(
                    action=intent,
                    target_item={"id": target_id, "data": {"name": target_name}},
                    fields=scoped_fields,
                    llm_assisted=True,
                )
            result = self.create_product_scoped_knowledge(
                category_id=scoped_category_id,
                target_product_id=target_id,
                target_product_name=target_name,
                data_patch=scoped_fields,
                source_text=command_text,
            )
            return {
                "ok": True,
                "action": intent,
                "llm_assisted": True,
                **result,
            }
        return None

    def upload_product_draft(self, *, filename: str, content: bytes, use_llm: bool = True) -> dict[str, Any]:
        upload = UploadStore().save_upload(filename=filename or "product_upload.txt", content=content, kind="products")
        if not upload.get("ok"):
            raise ValueError(upload.get("message") or upload)
        item = upload.get("item") if isinstance(upload.get("item"), dict) else {}
        path = Path(str(item.get("path") or ""))
        text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
        if not text.strip():
            raise ValueError("商品资料没有可读取的文字内容")
        prompt = "\n".join(
            [
                "请把下面资料只整理成商品库主数据。不要生成客服话术、政策规则或候选知识。",
                "如果商品名称、价格、库存、类目、单位等主字段不完整，请保留草稿并提示用户补充。",
                text[:12000],
            ]
        )
        session = KnowledgeGenerator().create_session(prompt, preferred_category_id="products", use_llm=use_llm).get("session", {})
        return {
            "ok": True,
            "action": "product_upload_draft",
            "upload": item,
            "session": session,
            "ai_advice": product_draft_advice(session),
            "direct_apply_allowed": str(session.get("category_id") or "") == "products" and str(session.get("status") or "") == "ready",
        }

    def get_product_item(self, product_id: str, *, include_archived: bool = False) -> dict[str, Any] | None:
        for item in self.store.list_items("products", include_archived=include_archived):
            if str(item.get("id") or "") == product_id:
                return item
        return None

    def enrich_product(
        self,
        item: dict[str, Any],
        *,
        scoped: dict[str, list[dict[str, Any]]] | None = None,
        scoped_counts: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        product_id = str(item.get("id") or "")
        scoped = scoped if scoped is not None else None
        if scoped_counts is None:
            scoped = scoped if scoped is not None else self.product_scoped_knowledge(product_id)
            scoped_counts = {category_id: len(items) for category_id, items in scoped.items()}
        else:
            scoped_counts = {category_id: int(scoped_counts.get(category_id, 0) or 0) for category_id in PRODUCT_SCOPED_CATEGORIES}
        inventory = data.get("inventory")
        stock_state = product_stock_state(item, inventory)
        review_state = item.get("review_state") if isinstance(item.get("review_state"), dict) else {}
        is_unread = bool(review_state.get("is_new"))
        runtime_usable = str(item.get("status") or "active") != "archived" and not is_unread
        return {
            **item,
            "is_unread": is_unread,
            "runtime_usable": runtime_usable,
            "display": {
                "name": data.get("name") or product_id,
                "sku": data.get("sku") or product_id,
                "category": data.get("category") or "未分类",
                "price": data.get("price"),
                "unit": data.get("unit") or "",
                "inventory": inventory,
                "stock_state": stock_state,
                "stock_label": stock_label(stock_state, inventory),
                "runtime_label": runtime_label(item, is_unread=is_unread),
            },
            "stock_state": stock_state,
            "scoped_counts": scoped_counts,
        }


def match_product(text: str, products: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized = text.lower()
    scored: list[tuple[int, dict[str, Any]]] = []
    generic_terms = {"商品", "产品", "型号", "sku", "库存", "现货", "价格", "售价"}
    for item in products:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        values = [item.get("id"), data.get("name"), data.get("sku"), *(data.get("aliases") or [])]
        score = 0
        for value in values:
            value_text = str(value or "").strip().lower()
            if not value_text:
                continue
            for keyword in build_product_match_keywords(value_text):
                if not keyword or keyword in generic_terms:
                    continue
                if keyword in normalized:
                    bonus = len(keyword) + (20 if keyword == value_text else 0)
                    score = max(score, bonus)
        if score:
            scored.append((score, item))
    if not scored:
        return None
    return sorted(scored, key=lambda pair: pair[0], reverse=True)[0][1]


def build_product_match_keywords(value_text: str) -> list[str]:
    text = str(value_text or "").strip().lower()
    if not text:
        return []
    keywords: list[str] = [text]
    cleaned = re.sub(r"[（(].*?[）)]", "", text).strip()
    if cleaned and cleaned not in keywords:
        keywords.append(cleaned)
    for token in re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9_.-]{2,}", text):
        token = token.strip()
        if token and token not in keywords:
            keywords.append(token)
    return keywords


def suggest_products(text: str, products: list[dict[str, Any]], *, limit: int = 3) -> list[str]:
    normalized = str(text or "").lower()
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9_.-]{2,}", normalized)
    if not tokens:
        return []
    generic_terms = {"商品", "产品", "这个商品", "这个", "那个", "库存", "价格", "售价", "梯度", "更新", "修改", "调整"}
    filtered = [token for token in tokens if token not in generic_terms]
    if not filtered:
        return []
    scored: list[tuple[int, str]] = []
    for item in products:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        display_name = str(data.get("name") or item.get("id") or "")
        if not display_name:
            continue
        values = [display_name, str(data.get("sku") or ""), *(data.get("aliases") or []), str(item.get("id") or "")]
        haystack = " ".join(str(value or "").lower() for value in values)
        score = 0
        for token in filtered:
            if token in haystack:
                score += len(token)
        if score > 0:
            scored.append((score, display_name))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    results: list[str] = []
    for _, name in scored:
        if name not in results:
            results.append(name)
        if len(results) >= max(1, int(limit)):
            break
    return results


def parse_quantity(text: str) -> int | None:
    match = re.search(rf"(\d+)\s*{QUANTITY_UNITS_PATTERN}(?![A-Za-z0-9])", text)
    if match:
        return int(match.group(1))
    chinese_digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    for char, value in chinese_digits.items():
        if re.search(fr"{char}\s*{QUANTITY_UNITS_PATTERN}", text):
            return value
    return None


def parse_inventory_quantity(text: str, *, mode: str) -> int | None:
    mode_patterns = {
        "set": [
            r"库存\s*(?:改成|改为|设为|调整为|改到|设到|为|是|还有)?\s*(\d+)",
            r"(?:现货|余量|剩余)\s*(?:还有|为|是)?\s*(\d+)",
        ],
        "increase": [
            r"(?:补货|到货|入库|增加库存|库存增加|库存加)\s*(\d+)",
            r"(?:增加|加)\s*(\d+)\s*" + QUANTITY_UNITS_PATTERN,
        ],
        "decrease": [
            r"(?:卖掉|卖出|售出|成交|减少|扣减|出库|卖)\s*(\d+)",
            r"(?:减少|扣减)\s*(\d+)\s*" + QUANTITY_UNITS_PATTERN,
        ],
    }
    for pattern in mode_patterns.get(mode, []):
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return parse_quantity(text)


def build_command_plan(
    *,
    action: str,
    target_item: dict[str, Any] | None = None,
    quantity: int | None = None,
    fields: dict[str, Any] | None = None,
    llm_assisted: bool = False,
) -> dict[str, Any]:
    item = target_item or {}
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    target_id = str(item.get("id") or "")
    target_name = str(data.get("name") or target_id)
    preview = ""
    if action == "archive":
        preview = f"将把商品「{target_name}」归档下架。"
    elif action == "set_inventory":
        preview = f"将把商品「{target_name}」库存设置为 {int(quantity or 0)}。"
    elif action == "increase_inventory":
        preview = f"将把商品「{target_name}」库存增加 {int(quantity or 0)}。"
    elif action == "decrease_inventory":
        preview = f"将把商品「{target_name}」库存减少 {int(quantity or 0)}。"
    elif action == "update_product":
        keys = sorted((fields or {}).keys())
        preview = f"将更新商品「{target_name}」字段：{', '.join(keys) if keys else '未识别字段'}。"
    elif action in LLM_INTENT_TO_PRODUCT_SCOPED_CATEGORY:
        category_id = LLM_INTENT_TO_PRODUCT_SCOPED_CATEGORY[action]
        category_label = PRODUCT_SCOPED_CATEGORIES.get(category_id, category_id)
        title = str((fields or {}).get("title") or "未命名")
        preview = f"将为商品「{target_name}」新增{category_label}：{title}。"
    return {
        "ok": True,
        "action": action,
        "target_product_id": target_id,
        "target_product_name": target_name,
        "quantity": quantity,
        "fields": fields or {},
        "llm_assisted": bool(llm_assisted),
        "needs_confirmation": True,
        "summary": preview,
    }


def extract_product_patch(text: str) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    parsed = parse_product(text)
    wants_tier_price = has_any(text, "梯度售价", "阶梯售价", "梯度价", "阶梯价", "批发价", "团购价", "起订", "订单价", "采购量")
    name = extract_renamed_product_name(text)
    if name:
        patch["name"] = name
    sku = extract_regex_value(text, r"(?:SKU|sku|型号)\s*(?:改成|改为|设为|调整为|是|为|:|：)\s*([A-Za-z0-9_.-]+)")
    if sku:
        patch["sku"] = sku
    unit = extract_short_text_after_keywords(text, "计价单位", "单位")
    if unit:
        patch["unit"] = unit
    if has_any(text, "价格", "售价", "报价", "标价", "单价", "基础价", "零售价"):
        price = parse_base_price_value(text)
        if price is None and not wants_tier_price:
            parsed_price = parsed.get("price")
            price = parsed_price if isinstance(parsed_price, (int, float)) else None
        if price is not None:
            patch["price"] = price
    if has_any(text, "库存", "现货", "余量", "剩余"):
        quantity = parse_inventory_quantity(text, mode="set")
        if quantity is not None:
            patch["inventory"] = quantity
    if wants_tier_price:
        tiers = extract_product_price_tiers(text)
        if tiers:
            patch["price_tiers"] = tiers
    category = extract_short_text_after_keywords(text, "类目", "分类")
    if category:
        patch["category"] = category
    specs = extract_short_text_after_keywords(text, "规格", "参数", "尺寸")
    if specs:
        patch["specs"] = specs
    elif has_any(text, "规格", "参数", "尺寸"):
        parsed_specs = str(parsed.get("specs") or "").strip()
        if parsed_specs:
            patch["specs"] = parsed_specs
    shipping = extract_short_text_after_keywords(text, "发货", "物流", "包邮")
    if shipping:
        patch["shipping_policy"] = shipping
    warranty = extract_short_text_after_keywords(text, "售后", "保修", "质保")
    if warranty:
        patch["warranty_policy"] = warranty
    details = extract_text_after_keywords(text, "备注", "说明")
    if details:
        patch["additional_details"] = {"notes": details}
    aliases = extract_short_text_after_keywords(text, "别名", "叫法", "也叫")
    if aliases:
        patch["aliases"] = split_aliases(aliases)
    reply = extract_short_text_after_keywords(text, "回复模板", "标准回复", "默认回复", "话术")
    if reply:
        patch["reply_templates"] = {"default": reply}
    risk_text = extract_short_text_after_keywords(text, "禁用承诺", "风险提醒", "风险规则", "风险")
    if risk_text:
        patch["risk_rules"] = split_aliases(risk_text)
    return patch


def parse_decimal_after_keywords(text: str, *keywords: str) -> float | int | None:
    for keyword in keywords:
        match = re.search(fr"{re.escape(keyword)}\s*(?:改成|改为|设为|调整为|是|为|:|：)?\s*(\d+(?:\.\d+)?)", text)
        if not match:
            continue
        value = float(match.group(1))
        return int(value) if value.is_integer() else value
    return None


def parse_base_price_value(text: str) -> float | int | None:
    tier_markers = ("梯度", "阶梯", "批发", "团购", "订单价", "起订", "采购量")
    patterns = [
        r"(单价|基础价|零售价|标价)\s*(?:改成|改为|设为|调整为|是|为|:|：)?\s*(\d+(?:\.\d+)?)",
        r"(售价|价格|报价)\s*(?:改成|改为|设为|调整为|是|为|:|：)?\s*(\d+(?:\.\d+)?)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            keyword = str(match.group(1) or "")
            amount_text = str(match.group(2) or "")
            left_context = text[max(0, match.start() - 14) : match.start()]
            right_context = text[match.start() : min(len(text), match.end() + 12)]
            context = left_context + right_context
            if any(marker in context for marker in tier_markers):
                continue
            if re.search(r"\d+\s*(?:台|件|个|套|箱)\s*(?:起|以上|及以上|时)", context):
                continue
            if re.search(r"采购量\s*(?:为|是|达|到)?\s*\d+", context):
                continue
            try:
                value = float(amount_text)
            except ValueError:
                continue
            return int(value) if value.is_integer() else value
    return None


def extract_text_after_keywords(text: str, *keywords: str) -> str:
    for keyword in keywords:
        match = re.search(fr"{re.escape(keyword)}\s*(?:改成|改为|设为|调整为|是|为|:|：)?\s*(.+)$", text)
        if match:
            return cleanup_value_text(match.group(1))
    return ""


def extract_short_text_after_keywords(text: str, *keywords: str) -> str:
    for keyword in keywords:
        match = re.search(fr"{re.escape(keyword)}\s*(?:改成|改为|设为|调整为|是|为|:|：)?\s*([^，,。；;\n]+)", text)
        if match:
            return cleanup_value_text(match.group(1))
    return ""


def extract_regex_value(text: str, pattern: str) -> str:
    match = re.search(pattern, text)
    if not match:
        return ""
    return cleanup_value_text(match.group(1))


def extract_renamed_product_name(text: str) -> str:
    patterns = [
        r"(?:改名为|名称改为|商品名改为|商品名称改为|更名为)\s*([^\n，,。；;]+)",
        r"(?:名称|商品名|商品名称)\s*(?:改成|改为|设为|调整为)\s*([^\n，,。；;]+)",
    ]
    for pattern in patterns:
        renamed = extract_regex_value(text, pattern)
        if renamed:
            return renamed
    return ""


def cleanup_value_text(value: str) -> str:
    return str(value or "").strip(" ：:，,。；; \t\r\n")


def split_aliases(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，、/]\s*|\s+", value) if item.strip()]


def extract_product_price_tiers(text: str) -> list[dict[str, float]]:
    tiers: list[dict[str, float]] = list(extract_price_tiers(text))
    extra_patterns = [
        r"(?:采购量|购买量|下单量|数量)\s*(?:为|是|达|到|>=|大于等于)?\s*(\d+(?:\.\d+)?)\s*(?:台|个|件|辆|套|只|箱|张|把|条|份|组|批)?\s*(?:时|以上|起订|起|及以上)\s*[，,、;；]?\s*(?:每台|每件|每个|每套|每箱|每组|每辆)?\s*(?:单价|订单价|批发价|阶梯价|团购价|优惠价)?\s*(?:为|是|:|：)?\s*(\d+(?:\.\d+)?)\s*(?:元|块|rmb|RMB)?",
        r"(?:采购量|购买量|下单量|数量)\s*(?:为|是|达|到|>=|大于等于)?\s*(\d+(?:\.\d+)?)\s*(?:台|个|件|辆|套|只|箱|张|把|条|份|组|批)?\s*(?:每台|每件|每个|每套|每箱|每组|每辆|单价|订单价|批发价|阶梯价|团购价|优惠价)\s*(?:为|是|:|：)?\s*(\d+(?:\.\d+)?)\s*(?:元|块|rmb|RMB)?",
    ]
    for pattern in extra_patterns:
        for quantity, price in re.findall(pattern, text):
            tiers.append({"min_quantity": float(quantity), "unit_price": float(price)})
    return normalize_price_tiers(tiers)


def normalize_llm_update_fields(raw_fields: Any) -> dict[str, Any]:
    if not isinstance(raw_fields, dict):
        return {}
    allowed = {
        "name",
        "sku",
        "specs",
        "unit",
        "price",
        "inventory",
        "price_tiers",
        "category",
        "shipping_policy",
        "warranty_policy",
        "reply_templates",
        "risk_rules",
        "additional_details",
        "aliases",
    }
    fields: dict[str, Any] = {}
    for key, value in raw_fields.items():
        field = str(key or "").strip()
        if field not in allowed:
            continue
        if field == "aliases":
            if isinstance(value, list):
                aliases = [str(item).strip() for item in value if str(item).strip()]
                if aliases:
                    fields[field] = aliases
            elif value not in (None, ""):
                aliases = split_aliases(str(value))
                if aliases:
                    fields[field] = aliases
            continue
        if field == "risk_rules":
            if isinstance(value, list):
                rules = [str(item).strip() for item in value if str(item).strip()]
                if rules:
                    fields[field] = rules
            elif value not in (None, ""):
                rules = split_aliases(str(value))
                if rules:
                    fields[field] = rules
            continue
        if field == "price_tiers":
            if isinstance(value, list):
                normalized = normalize_price_tiers([row for row in value if isinstance(row, dict)])
                if normalized:
                    fields[field] = normalized
            continue
        if field == "reply_templates":
            if isinstance(value, dict):
                cleaned = {str(key).strip(): str(inner).strip() for key, inner in value.items() if str(key).strip() and str(inner).strip()}
                if cleaned:
                    fields[field] = cleaned
            elif value not in (None, ""):
                fields[field] = {"default": str(value).strip()}
            continue
        if field == "additional_details":
            if isinstance(value, dict):
                cleaned = {str(key).strip(): inner for key, inner in value.items() if str(key).strip() and inner not in (None, "", [], {})}
                if cleaned:
                    fields[field] = cleaned
            elif value not in (None, ""):
                fields[field] = {"notes": str(value).strip()}
            continue
        if field in {"price", "inventory"}:
            try:
                number = float(str(value))
            except (TypeError, ValueError):
                continue
            fields[field] = int(number) if number.is_integer() else number
            continue
        if field in {"name", "sku", "category", "specs", "unit", "shipping_policy", "warranty_policy"}:
            text = str(value or "").strip()
            if text:
                fields[field] = text
            continue
        if value in (None, "", [], {}):
            continue
        fields[field] = value
    return fields


def maybe_reroute_reply_template_update(
    *,
    command_text: str,
    fields: dict[str, Any],
    target_product_id: str,
    target_product_name: str,
) -> dict[str, Any] | None:
    if not isinstance(fields, dict):
        return None
    templates = fields.get("reply_templates")
    if not isinstance(templates, dict) or not templates:
        return None
    if any(str(key) != "reply_templates" for key in fields):
        return None
    if explicit_base_reply_template_update_intent(command_text):
        return None
    normalized_templates = {
        str(key or "").strip(): str(value or "").strip()
        for key, value in templates.items()
        if str(key or "").strip() and str(value or "").strip()
    }
    if not normalized_templates:
        return None
    scoped_context = has_scoped_qa_context(command_text)
    if all(scene in BASE_REPLY_TEMPLATE_KEYS for scene in normalized_templates) and not scoped_context:
        return None
    if not looks_like_scene_qa(command_text, normalized_templates):
        return None
    scene, reply = next(iter(normalized_templates.items()))
    parsed_faq = normalize_product_scoped_data_fields("product_faq", parse_product_scoped(command_text, "product_faq"))
    scene_title = str(parsed_faq.get("title") or "").strip()
    scene_question = str(parsed_faq.get("question") or "").strip()
    if scene in BASE_REPLY_TEMPLATE_KEYS and scene_title:
        scene = scene_title
    default_category = infer_product_scoped_category(command_text)
    category_id = "product_rules" if default_category == "product_rules" else "product_faq"
    action = {
        "product_faq": "create_product_faq",
        "product_rules": "create_product_rules",
    }.get(category_id, "create_product_faq")
    data_patch: dict[str, Any] = {
        "product_id": target_product_id,
        "title": scene or default_scoped_title(category_id, target_product_name or target_product_id),
        "keywords": normalize_string_list([scene]) if scene else [],
    }
    if category_id == "product_rules":
        data_patch["answer"] = reply
        if has_any(reply, "人工", "转人工", "联系客服", "请示", "确认后回复"):
            data_patch["requires_handoff"] = True
            data_patch["allow_auto_reply"] = False
            data_patch["handoff_reason"] = "需要人工客服确认"
    else:
        data_patch["question"] = scene_question or scene_to_faq_question(scene)
        data_patch["answer"] = reply
    return {
        "action": action,
        "category_id": category_id,
        "fields": data_patch,
    }


def explicit_base_reply_template_update_intent(text: str) -> bool:
    if not text:
        return False
    patterns = [
        r"(?:基础话术|默认话术|默认回复|回复模板|标准回复|弱触发).{0,12}(?:改成|改为|更新为|设为|替换|覆盖|调整)",
        r"(?:把|将).{0,24}(?:基础话术|默认回复|回复模板).{0,12}(?:改成|改为|设为|替换|覆盖)",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def has_scoped_qa_context(text: str) -> bool:
    return has_any(
        text,
        "专属回答",
        "专属回复",
        "专属问答",
        "客户问",
        "客户问题",
        "问：",
        "问:",
        "回答：",
        "回答:",
        "咨询",
        "场景",
    )


def looks_like_scene_qa(command_text: str, templates: dict[str, str]) -> bool:
    scene_text = " ".join(list(templates.keys())[:4])
    reply_text = " ".join(list(templates.values())[:4])
    combined = f"{command_text}\n{scene_text}\n{reply_text}"
    return has_any(
        combined,
        "咨询",
        "客户问",
        "客户问题",
        "问到",
        "怎么",
        "如何",
        "可以",
        "是否",
        "能否",
        "吗",
        "？",
        "费用",
        "报价",
        "安装",
        "退换",
        "售后",
        "保修",
        "物流",
        "包邮",
        "联系人工",
        "人工客服",
    )


def scene_to_faq_question(scene: str) -> str:
    text = str(scene or "").strip().strip("：:")
    if not text:
        return ""
    if re.search(r"[？?]$", text):
        return text
    if "咨询" in text:
        base = text.replace("咨询", "").strip()
        if base:
            return f"{base}可以吗？"
    if has_any(text, "怎么", "如何", "是否", "能否", "可以"):
        return text + ("？" if not text.endswith("？") else "")
    return f"{text}可以吗？"


def normalize_faq_title(*, title: str, question: str, product_name: str) -> str:
    t = str(title or "").strip()
    q = str(question or "").strip()
    if q:
        core_q = normalize_question_text(q)
        if not t:
            return core_q[:80] or q[:80]
        if len(t) > 36 or has_any(t, "客户问", "回答", "专属问答"):
            return core_q[:80] or q[:80]
    if t and product_name and t.startswith(product_name):
        stripped = t[len(product_name) :].strip(" ：:-")
        if stripped:
            return stripped[:80]
    return t[:80]


def auto_scoped_keywords(*, category_id: str, payload: dict[str, Any], product_name: str) -> list[str]:
    existing = normalize_string_list(payload.get("keywords"))
    if existing:
        return existing[:12]
    seeds: list[str] = []
    question = str(payload.get("question") or "").strip()
    title = str(payload.get("title") or "").strip()
    answer = str(payload.get("answer") or "").strip()
    content = str(payload.get("content") or "").strip()
    if question:
        seeds.append(normalize_question_text(question))
    if title:
        seeds.append(title)
    if category_id == "product_explanations":
        if content:
            seeds.extend(extract_text_keyword_candidates(content))
    else:
        if answer:
            seeds.extend(extract_text_keyword_candidates(answer))
    for hint in SCOPED_KEYWORD_HINTS:
        blob = "\n".join([question, title, answer, content])
        if hint in blob:
            seeds.append(hint)
    cleaned: list[str] = []
    for item in seeds:
        token = normalize_keyword_token(item, product_name=product_name)
        if not token:
            continue
        if token in cleaned:
            continue
        cleaned.append(token)
        if len(cleaned) >= 12:
            break
    return cleaned


def normalize_question_text(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"^(客户问|客户问题|问题|问法)\s*[:：]?\s*", "", value)
    value = re.sub(r"^(请问|想问一下|想问|是否|能否)\s*", "", value)
    value = re.sub(r"[吗么嘛呢？?。!！；;\s]+$", "", value).strip()
    return value


def extract_text_keyword_candidates(text: str) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return []
    fragments: list[str] = []
    for segment in re.split(r"[，,。；;！!？?\n\r]+", value):
        part = str(segment or "").strip()
        if not part:
            continue
        normalized = re.sub(r"^(可以|请问|请|需要|额外|加收)\s*", "", part).strip()
        normalized = re.sub(r"(请联系人工客服.*|请联系客服.*)$", "", normalized).strip()
        if normalized and len(normalized) <= 14 and re.search(r"[\u4e00-\u9fff]", normalized):
            fragments.append(normalized)
        for hint in SCOPED_KEYWORD_HINTS:
            if hint in part:
                fragments.append(hint)
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{1,24}", part):
            fragments.append(token)
        for amount in re.findall(r"\d+(?:\.\d+)?\s*(?:元|天|小时|台|件|单|次)", part):
            fragments.append(amount.replace(" ", ""))
    return fragments


def normalize_keyword_token(token: str, *, product_name: str) -> str:
    value = str(token or "").strip(" ：:，,。；;!?！？")
    if not value:
        return ""
    value = re.sub(r"^(客户问|客户问题|问题|回答|回复|标准回复)\s*[:：]?\s*", "", value)
    value = re.sub(r"[吗么嘛呢？?。!！；;\s]+$", "", value).strip()
    if not value:
        return ""
    if value in SCOPED_KEYWORD_STOPWORDS:
        return ""
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return ""
    if product_name and value == str(product_name).strip():
        return ""
    if len(value) < 2:
        return ""
    if len(value) > 16 and re.fullmatch(r"[\u4e00-\u9fff]+", value):
        value = value[:16]
    return value


def infer_product_scoped_from_text(text: str, matched: dict[str, Any]) -> dict[str, Any] | None:
    if not text:
        return None
    if not mentions_product_scoped_intent(text):
        return None
    category_id = infer_product_scoped_category(text)
    action = {
        "product_faq": "create_product_faq",
        "product_rules": "create_product_rules",
        "product_explanations": "create_product_explanations",
    }.get(category_id, "")
    if not action:
        return None
    fields = normalize_product_scoped_data_fields(category_id, parse_product_scoped(text, category_id))
    product_id = str(matched.get("id") or "")
    if not product_id:
        return None
    fields["product_id"] = product_id
    name = str((matched.get("data") or {}).get("name") or product_id)
    if not fields.get("title"):
        fields["title"] = default_scoped_title(category_id, name)
    if category_id == "product_explanations":
        if not str(fields.get("content") or "").strip():
            return None
    else:
        if not str(fields.get("answer") or "").strip():
            return None
    return {
        "action": action,
        "category_id": category_id,
        "fields": fields,
    }


def mentions_product_scoped_intent(text: str) -> bool:
    return has_any(
        text,
        "专属问答",
        "专属规则",
        "专属解释",
        "商品问答",
        "问答",
        "客户问",
        "客户问题",
        "问到",
        "回答：",
        "回答:",
        "回复：",
        "回复:",
        "标准回复",
        "触发词",
    )


def infer_product_scoped_category(text: str) -> str:
    if has_any(text, "专属规则", "规则", "必须", "禁止", "不能承诺", "转人工", "需要人工", "请示"):
        return "product_rules"
    if has_any(text, "专属解释", "解释", "说明", "原理", "为什么"):
        return "product_explanations"
    return "product_faq"


def normalize_product_scoped_data_fields(category_id: str, raw_fields: Any) -> dict[str, Any]:
    if not isinstance(raw_fields, dict):
        return {}
    data: dict[str, Any] = {}
    product_id = str(raw_fields.get("product_id") or "").strip()
    if product_id:
        data["product_id"] = product_id
    title = str(raw_fields.get("title") or raw_fields.get("name") or "").strip()
    if title:
        data["title"] = title[:80]
    keywords = normalize_string_list(raw_fields.get("keywords") or raw_fields.get("alias_keywords"))
    if keywords:
        data["keywords"] = keywords
    question = str(raw_fields.get("question") or raw_fields.get("customer_message") or "").strip()
    if question and category_id == "product_faq":
        data["question"] = question
    answer = str(raw_fields.get("answer") or raw_fields.get("service_reply") or "").strip()
    if not answer:
        reply_templates = raw_fields.get("reply_templates")
        if isinstance(reply_templates, dict):
            for value in reply_templates.values():
                text = str(value or "").strip()
                if text:
                    answer = text
                    break
    content = str(raw_fields.get("content") or raw_fields.get("description") or "").strip()
    if category_id == "product_explanations":
        if content:
            data["content"] = content
    else:
        if answer:
            data["answer"] = answer
    allow_auto_reply = parse_bool_value(raw_fields.get("allow_auto_reply"))
    requires_handoff = parse_bool_value(raw_fields.get("requires_handoff"))
    handoff_reason = str(raw_fields.get("handoff_reason") or "").strip()
    if category_id == "product_rules":
        if allow_auto_reply is not None:
            data["allow_auto_reply"] = allow_auto_reply
        if requires_handoff is not None:
            data["requires_handoff"] = requires_handoff
        if handoff_reason:
            data["handoff_reason"] = handoff_reason
    details = raw_fields.get("additional_details")
    if isinstance(details, dict):
        cleaned = {str(key).strip(): value for key, value in details.items() if str(key).strip() and value not in (None, "", [], {})}
        if cleaned:
            data["additional_details"] = cleaned
    return data


def normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()][:24]
    if value in (None, ""):
        return []
    return [token for token in split_aliases(str(value)) if token][:24]


def parse_bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "是", "允许", "开启"}:
        return True
    if text in {"0", "false", "no", "n", "否", "不允许", "关闭"}:
        return False
    return None


def default_scoped_title(category_id: str, product_name: str) -> str:
    suffix = {
        "product_faq": "专属问答",
        "product_rules": "专属规则",
        "product_explanations": "专属解释",
    }.get(category_id, "专属知识")
    prefix = str(product_name or "商品").strip()
    return f"{prefix}{suffix}"[:80]


def product_name_by_id(products: list[dict[str, Any]], product_id: str) -> str:
    for item in products:
        if str(item.get("id") or "") != product_id:
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        return str(data.get("name") or "")
    return ""


def build_scoped_item_id(*, category_id: str, product_id: str, title: str, body: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(title or "").strip()).strip("_.-").lower()
    if not base:
        base = "item"
    fingerprint = hashlib.sha1(f"{category_id}|{product_id}|{title}|{body}".encode("utf-8")).hexdigest()[:8]
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{category_id}_{base[:40]}_{timestamp}_{fingerprint}"[:120]


def has_update_intent(text: str) -> bool:
    return has_any(
        text,
        "改",
        "更新",
        "设置",
        "调整",
        "补货",
        "库存",
        "售价",
        "价格",
        "规格",
        "型号",
        "SKU",
        "sku",
        "发货",
        "售后",
        "保修",
        "别名",
        "梯度价",
        "阶梯价",
    )


def detect_missing_update_fields(text: str, patch: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    wants_tier_price = has_any(text, "梯度售价", "阶梯售价", "梯度价", "阶梯价", "批发价", "团购价", "起订", "订单价", "采购量")
    wants_base_price = has_any(text, "价格", "售价", "报价", "标价") and not wants_tier_price
    if wants_base_price and "price" not in patch:
        missing.append("价格数值（例如：售价改为 1999 元）")
    if has_any(text, "库存", "现货", "余量", "剩余") and "inventory" not in patch:
        missing.append("库存数量（例如：库存改为 499）")
    if wants_tier_price and "price_tiers" not in patch:
        missing.append("梯度售价（例如：10 台起每台 1899 元）")
    if mentions_sku_update_request(text) and "sku" not in patch:
        missing.append("型号/SKU（例如：型号改为 100C-Pro）")
    if mentions_unit_update_request(text) and "unit" not in patch:
        missing.append("计价单位（例如：单位改为 台）")
    if mentions_specs_update_request(text) and "specs" not in patch:
        missing.append("规格参数（例如：规格改为 75寸 4K）")
    if mentions_shipping_update_request(text) and "shipping_policy" not in patch:
        missing.append("发货/物流说明")
    if mentions_warranty_update_request(text) and "warranty_policy" not in patch:
        missing.append("售后/保修说明")
    if mentions_aliases_update_request(text) and "aliases" not in patch:
        missing.append("别名列表（例如：别名为 小米大屏、75寸电视）")
    if mentions_reply_template_update_request(text) and "reply_templates" not in patch:
        missing.append("回复模板内容")
    if mentions_risk_rules_update_request(text) and "risk_rules" not in patch:
        missing.append("风险提醒内容")
    return missing


def mentions_sku_update_request(text: str) -> bool:
    return bool(re.search(r"(?:SKU|sku|型号)\s*(?:改成|改为|设为|调整为|更新为|:|：)", text))


def mentions_unit_update_request(text: str) -> bool:
    return bool(re.search(r"(?:计价单位|单位)\s*(?:改成|改为|设为|调整为|更新为|:|：)", text))


def mentions_specs_update_request(text: str) -> bool:
    return bool(re.search(r"(?:规格|参数|尺寸)\s*(?:改成|改为|设为|调整为|更新为|:|：)", text))


def mentions_shipping_update_request(text: str) -> bool:
    return bool(re.search(r"(?:发货|物流|包邮)\s*(?:改成|改为|设为|调整为|更新为|:|：)", text))


def mentions_warranty_update_request(text: str) -> bool:
    return bool(re.search(r"(?:售后|保修|质保)\s*(?:改成|改为|设为|调整为|更新为|:|：)", text))


def mentions_aliases_update_request(text: str) -> bool:
    return bool(re.search(r"(?:别名|叫法|也叫)\s*(?:改成|改为|设为|调整为|更新为|:|：)", text))


def mentions_reply_template_update_request(text: str) -> bool:
    return bool(re.search(r"(?:回复模板|标准回复|默认回复|话术)\s*(?:改成|改为|设为|调整为|更新为|:|：)", text))


def mentions_risk_rules_update_request(text: str) -> bool:
    return bool(re.search(r"(?:风险提醒|风险规则|禁用承诺)\s*(?:改成|改为|设为|调整为|更新为|:|：)", text))


def build_clarifying_question(
    text: str,
    *,
    matched: dict[str, Any] | None,
    extracted_patch: dict[str, Any],
    llm_result: dict[str, Any] | None,
) -> str:
    if matched and has_any(text, "专属知识"):
        product_name = str((matched.get("data") or {}).get("name") or matched.get("id") or "该商品")
        return (
            f"我理解你在给「{product_name}」补充商品专属知识。"
            "请说明要新增的是：专属问答、专属规则，还是专属解释；并补充对应内容。"
        )

    if matched and mentions_product_scoped_intent(text):
        product_name = str((matched.get("data") or {}).get("name") or matched.get("id") or "该商品")
        category_id = infer_product_scoped_category(text)
        if category_id == "product_explanations":
            return f"我理解你在给「{product_name}」补充专属解释。请补充：说明主题（title）和说明内容。"
        if category_id == "product_rules":
            return f"我理解你在给「{product_name}」补充专属规则。请补充：规则标题和标准回复；若需转人工，也请写明原因。"
        return f"我理解你在给「{product_name}」补充专属问答。请补充：客户问法和标准回复（可选再给触发关键词）。"

    if matched and has_update_intent(text):
        if extracted_patch:
            missing = detect_missing_update_fields(text, extracted_patch)
            if missing:
                return "我已经识别到商品，但信息还缺少：" + "、".join(dict.fromkeys(missing)) + "。请继续补充。"
        else:
            product_name = str((matched.get("data") or {}).get("name") or matched.get("id") or "该商品")
            return f"我已识别到商品「{product_name}」，但还不确定你要改哪些字段。请明确是改库存、价格、梯度价、规格、物流、售后或别名。"

    if isinstance(llm_result, dict):
        missing_info = str(llm_result.get("missing_info") or "").strip()
        followup = str(llm_result.get("followup_question") or "").strip()
        if missing_info:
            return "信息还不完整：" + missing_info
        if followup:
            return followup

    if has_any(text, "新增", "添加", "上架", "新商品", "新车源"):
        return "你是在新增商品吗？请补充：商品名称、售价和计价单位（可选：库存、梯度价、物流、售后、别名）。"

    if has_update_intent(text):
        return "我还没完全识别你的修改意图。请补充：要操作的商品名，以及具体要改的字段和值。"

    return "我还没听清你的需求。你可以这样说：新增商品、修改库存、修改价格、设置梯度价，或告诉我要改哪些字段。"


def product_draft_advice(session: dict[str, Any]) -> dict[str, Any]:
    status = str(session.get("status") or "")
    missing = [str(item) for item in session.get("missing_fields", []) or [] if str(item)]
    if str(session.get("category_id") or "") != "products":
        return {
            "label": "需要人工检查",
            "message": "AI 没有把这份资料稳定识别为商品主数据，请修改草稿或换一份商品资料。",
            "missing_fields": missing,
        }
    if status == "ready":
        return {
            "label": "结构完整，可确认入库",
            "message": "AI 判断这份商品资料已满足商品库主字段要求。确认前仍建议核对价格、库存、单位和售后说明。",
            "missing_fields": [],
        }
    return {
        "label": "资料还不完整",
        "message": str(session.get("question") or "请补充缺失字段后再确认入库。"),
        "missing_fields": missing,
    }


def has_any(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def means_archived_status(text: str) -> bool:
    """Map merchant status wording to the only non-active state we support."""
    if has_any(text, "下架", "归档", "不卖", "停售", "停止销售", "不再销售"):
        return True
    if has_any(text, "已售罄", "售罄", "已售完", "售完", "卖完了", "卖光", "售空"):
        return True
    return False


def means_inventory_zero(text: str) -> bool:
    if has_any(text, "库存归零", "库存清零", "没有库存", "库存为0", "库存改成0", "库存设为0"):
        return True
    return False


def to_int(value: Any, *, default: int) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def product_stock_state(item: dict[str, Any], inventory: Any) -> str:
    if item.get("status") == "archived":
        return "archived"
    if inventory in (None, ""):
        return "unknown"
    try:
        return "sold_out" if int(float(str(inventory))) <= 0 else "in_stock"
    except (TypeError, ValueError):
        return "unknown"


def stock_label(stock_state: str, inventory: Any) -> str:
    if stock_state == "archived":
        return "已归档"
    if stock_state == "sold_out":
        return "无库存"
    if stock_state == "in_stock":
        return f"库存 {inventory}"
    return "库存未填写"


def runtime_label(item: dict[str, Any], *, is_unread: bool) -> str:
    if str(item.get("status") or "active") == "archived":
        return "已归档，不参与客服回复"
    if is_unread:
        return "新加入未已阅，暂不参与客服回复"
    return "已阅，可参与客服回复"


def _call_llm_for_command(text: str, products: list[dict[str, Any]], *, use_llm: bool = True) -> dict[str, Any] | None:
    """Use LLM to parse natural-language product commands into structured intent."""
    if not use_llm:
        return None
    api_key = read_secret("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    base_url = resolve_deepseek_base_url(read_secret_fn=read_secret)
    model = resolve_deepseek_tier_model(tier="flash", read_secret_fn=read_secret)

    product_list = []
    for item in products[:60]:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        product_list.append({
            "id": str(item.get("id") or ""),
            "name": str(data.get("name") or ""),
            "sku": str(data.get("sku") or ""),
            "aliases": [str(a) for a in (data.get("aliases") or []) if str(a)],
        })

    system_prompt = (
        "你是商品库智能助手。请把用户的自然语言命令解析成结构化的商品操作意图。"
        "只输出 JSON 对象，不要任何解释。"
        "如果命令里提到的商品名称和可用商品列表中的某个商品高度相关，请匹配该商品。"
        "库存操作支持：set_inventory（设置库存）、increase_inventory（增加库存）、decrease_inventory（减少/卖出）。"
        "如果用户描述的是“客户问答/规则说明/解释说明”等商品专属客服知识，"
        "请使用 create_product_faq、create_product_rules 或 create_product_explanations。"
        "字段更新支持：name（商品名称）、sku（型号/SKU）、category（类目/分类）、aliases（别名/也叫）、specs（规格参数）、price（价格/售价）、unit（计价单位）、"
        "price_tiers（梯度售价）、inventory（库存数值）、shipping_policy（发货/物流）、warranty_policy（售后/保修）、reply_templates（标准回复模板）、risk_rules（风险提醒）、additional_details（备注/说明）。"
    )

    user_prompt = {
        "command": text,
        "available_products": product_list,
        "instructions": (
            "解析用户的命令，识别目标商品和操作意图。"
            "intent 必须是以下之一：archive（下架/归档）、set_inventory（设置库存）、increase_inventory（补货/增加库存）、"
            "decrease_inventory（卖出/减少库存）、update_product（更新商品字段）、"
            "create_product_faq（新增商品专属问答）、create_product_rules（新增商品专属规则）、"
            "create_product_explanations（新增商品专属解释）、unknown（无法识别）。"
            "如果同一句同时包含库存和其他字段更新（尤其是梯度售价），必须使用 update_product，并把 inventory 与 price_tiers 一起放入 fields。"
            "如果 intent 是 update_product，把要修改的字段放入 fields 对象。"
            "如果 intent 是 create_product_faq/create_product_rules/create_product_explanations，把要写入的内容放入 scoped_fields。"
            "FAQ 必须给出 answer，可选 question、keywords；规则必须给出 answer，可选 keywords/allow_auto_reply/requires_handoff/handoff_reason；解释必须给出 content，可选 keywords。"
            "当你无法确定具体分类时，不要直接给 unknown；优先根据语义选择最接近的一类，并在 followup_question 里追问缺失字段。"
            "price_tiers 的格式必须是数组，每项为 {min_quantity, unit_price}。"
            "reply_templates 的格式必须是对象，键是场景名，值是回复文本。"
            "risk_rules 的格式必须是字符串数组。"
            "如果信息不完整无法执行，intent 设为 unknown，并在 missing_info 中说明缺少什么，同时给 followup_question 一句可直接发给用户的追问。"
            "confidence 表示你对解析结果的信心（0.0-1.0）。"
        ),
        "response_format": {
            "intent": "archive|set_inventory|increase_inventory|decrease_inventory|update_product|create_product_faq|create_product_rules|create_product_explanations|unknown",
            "target_product_id": "商品ID或空字符串",
            "target_product_name": "匹配到的商品名称或空字符串",
            "confidence": 0.0,
            "quantity": None,
            "fields": {},
            "scoped_fields": {},
            "reasoning": "简短推理过程",
            "missing_info": "",
            "followup_question": "",
        },
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
        ],
        "temperature": 0.1,
        "max_tokens": resolve_deepseek_max_tokens(1200, read_secret_fn=read_secret),
        "response_format": {"type": "json_object"},
    }
    apply_llm_reasoning_effort(payload, tier="flash", read_secret_fn=read_secret)

    request = urllib.request.Request(
        url=base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )

    try:
        with llm_urlopen(request, timeout=resolve_deepseek_timeout(30, read_secret_fn=read_secret)) as response:
            raw = response.read().decode("utf-8")
        data = json.loads(raw or "{}")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    result = _parse_json_object(str(content or ""))
    if not isinstance(result, dict):
        return None
    return result


def _parse_json_object(text: str) -> dict[str, Any] | None:
    text = str(text or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
