"""Precision checks for product-console NL parsing edge cases."""

from __future__ import annotations

import json
import os
import sys

APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROJECT_ROOT = os.path.abspath(os.path.join(APP_ROOT, "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from apps.wechat_ai_customer_service.admin_backend.services.knowledge_generator import extract_price_tiers
from apps.wechat_ai_customer_service.admin_backend.services.product_console_service import ProductConsoleService
from apps.wechat_ai_customer_service.admin_backend.services import product_console_service as product_console_module


def main() -> int:
    results: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    try:
        check_price_tier_extraction()
        results.append({"name": "price_tier_extraction_order_price", "ok": True})
    except Exception as exc:  # pragma: no cover - script style
        failure = {"name": "price_tier_extraction_order_price", "ok": False, "error": repr(exc)}
        results.append(failure)
        failures.append(failure)

    try:
        check_inventory_command_with_numeric_product_name()
        results.append({"name": "inventory_command_numeric_product_name", "ok": True})
    except Exception as exc:  # pragma: no cover - script style
        failure = {"name": "inventory_command_numeric_product_name", "ok": False, "error": repr(exc)}
        results.append(failure)
        failures.append(failure)

    try:
        check_mixed_inventory_and_price_tier_command()
        results.append({"name": "mixed_inventory_and_price_tier_command", "ok": True})
    except Exception as exc:  # pragma: no cover - script style
        failure = {"name": "mixed_inventory_and_price_tier_command", "ok": False, "error": repr(exc)}
        results.append(failure)
        failures.append(failure)

    try:
        check_standard_field_update_extraction()
        results.append({"name": "standard_field_update_extraction", "ok": True})
    except Exception as exc:  # pragma: no cover - script style
        failure = {"name": "standard_field_update_extraction", "ok": False, "error": repr(exc)}
        results.append(failure)
        failures.append(failure)

    try:
        check_missing_tier_price_prompt()
        results.append({"name": "missing_tier_price_prompt", "ok": True})
    except Exception as exc:  # pragma: no cover - script style
        failure = {"name": "missing_tier_price_prompt", "ok": False, "error": repr(exc)}
        results.append(failure)
        failures.append(failure)

    try:
        check_uncertain_update_prompts_clarification()
        results.append({"name": "uncertain_update_prompts_clarification", "ok": True})
    except Exception as exc:  # pragma: no cover - script style
        failure = {"name": "uncertain_update_prompts_clarification", "ok": False, "error": repr(exc)}
        results.append(failure)
        failures.append(failure)

    try:
        check_llm_scoped_faq_dry_run_plan()
        results.append({"name": "llm_scoped_faq_dry_run_plan", "ok": True})
    except Exception as exc:  # pragma: no cover - script style
        failure = {"name": "llm_scoped_faq_dry_run_plan", "ok": False, "error": repr(exc)}
        results.append(failure)
        failures.append(failure)

    try:
        check_llm_followup_question_preferred()
        results.append({"name": "llm_followup_question_preferred", "ok": True})
    except Exception as exc:  # pragma: no cover - script style
        failure = {"name": "llm_followup_question_preferred", "ok": False, "error": repr(exc)}
        results.append(failure)
        failures.append(failure)

    try:
        check_llm_scoped_rule_apply()
        results.append({"name": "llm_scoped_rule_apply", "ok": True})
    except Exception as exc:  # pragma: no cover - script style
        failure = {"name": "llm_scoped_rule_apply", "ok": False, "error": repr(exc)}
        results.append(failure)
        failures.append(failure)

    try:
        check_reply_templates_merge_without_overwrite()
        results.append({"name": "reply_templates_merge_without_overwrite", "ok": True})
    except Exception as exc:  # pragma: no cover - script style
        failure = {"name": "reply_templates_merge_without_overwrite", "ok": False, "error": repr(exc)}
        results.append(failure)
        failures.append(failure)

    try:
        check_llm_reply_template_scene_reroute_to_faq()
        results.append({"name": "llm_reply_template_scene_reroute_to_faq", "ok": True})
    except Exception as exc:  # pragma: no cover - script style
        failure = {"name": "llm_reply_template_scene_reroute_to_faq", "ok": False, "error": repr(exc)}
        results.append(failure)
        failures.append(failure)

    try:
        check_scoped_intent_without_product_has_clear_prompt()
        results.append({"name": "scoped_intent_without_product_has_clear_prompt", "ok": True})
    except Exception as exc:  # pragma: no cover - script style
        failure = {"name": "scoped_intent_without_product_has_clear_prompt", "ok": False, "error": repr(exc)}
        results.append(failure)
        failures.append(failure)

    try:
        check_llm_default_reply_template_reroute_with_scoped_context()
        results.append({"name": "llm_default_reply_template_reroute_with_scoped_context", "ok": True})
    except Exception as exc:  # pragma: no cover - script style
        failure = {"name": "llm_default_reply_template_reroute_with_scoped_context", "ok": False, "error": repr(exc)}
        results.append(failure)
        failures.append(failure)

    try:
        check_scoped_faq_auto_keywords_generated()
        results.append({"name": "scoped_faq_auto_keywords_generated", "ok": True})
    except Exception as exc:  # pragma: no cover - script style
        failure = {"name": "scoped_faq_auto_keywords_generated", "ok": False, "error": repr(exc)}
        results.append(failure)
        failures.append(failure)

    print(json.dumps({"ok": not failures, "count": len(results), "results": results, "failures": failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def check_price_tier_extraction() -> None:
    text = "添加商品：小米电视75寸，型号100C，单价1999元，10台起订单价1899元。江浙沪包邮"
    tiers = extract_price_tiers(text)
    assert_true(bool(tiers), "should extract price tiers from '10台起订单价1899元'")
    first = tiers[0]
    assert_equal(int(first.get("min_quantity") or 0), 10, "tier min_quantity should be 10")
    assert_equal(float(first.get("unit_price") or 0.0), 1899.0, "tier unit_price should be 1899")


def check_inventory_command_with_numeric_product_name() -> None:
    service = ProductConsoleService()
    products = [
        {
            "id": "mi_tv_75",
            "status": "active",
            "data": {"name": "小米电视75寸", "sku": "100C", "inventory": 3},
        }
    ]

    service.store.list_items = lambda category_id, include_archived=False: products if category_id == "products" else []  # type: ignore[assignment]

    def fake_adjust(product_id: str, *, operation: str, quantity: int | None = None) -> dict[str, object]:
        if product_id != "mi_tv_75":
            raise AssertionError(f"unexpected product id: {product_id}")
        if operation == "set":
            products[0]["data"]["inventory"] = int(quantity or 0)
        elif operation == "increase":
            products[0]["data"]["inventory"] = int(products[0]["data"]["inventory"]) + int(quantity or 1)
        elif operation in {"sell", "decrease"}:
            products[0]["data"]["inventory"] = int(products[0]["data"]["inventory"]) - int(quantity or 1)
        else:
            raise AssertionError(f"unexpected operation: {operation}")
        return {"ok": True, "item": products[0], "operation": operation}

    service.adjust_inventory = fake_adjust  # type: ignore[assignment]
    service.update_product = lambda product_id, data_patch: {"ok": True, "item": products[0], "operation": "update_product", "patch": data_patch}  # type: ignore[assignment]

    set_result = service.command("小米电视75寸型号100C库存改成20台", use_llm=False)
    assert_equal(str(set_result.get("action") or ""), "set_inventory", "should parse inventory-set action")
    assert_equal(int(products[0]["data"]["inventory"]), 20, "inventory should be set to 20 (not 75/100)")

    plus_result = service.command("小米电视75寸补货10台", use_llm=False)
    assert_equal(str(plus_result.get("action") or ""), "increase_inventory", "should parse inventory-increase action")
    assert_equal(int(products[0]["data"]["inventory"]), 30, "inventory should increase by 10")

    partial_result = service.command("小米电视库存改成21台", use_llm=False)
    assert_equal(str(partial_result.get("action") or ""), "set_inventory", "should match product by partial name")
    assert_equal(int(products[0]["data"]["inventory"]), 21, "inventory should be set by partial name matching")

    minus_result = service.command("小米电视75寸卖出2台", use_llm=False)
    assert_equal(str(minus_result.get("action") or ""), "decrease_inventory", "should parse inventory-decrease action")
    assert_equal(int(products[0]["data"]["inventory"]), 19, "inventory should decrease by 2")


def check_mixed_inventory_and_price_tier_command() -> None:
    service = ProductConsoleService()
    products = [
        {
            "id": "mi_tv_75",
            "status": "active",
            "data": {
                "name": "小米电视75寸",
                "sku": "100C",
                "inventory": 3,
                "price_tiers": [],
            },
        }
    ]
    service.store.list_items = lambda category_id, include_archived=False: products if category_id == "products" else []  # type: ignore[assignment]

    def fake_update(product_id: str, data_patch: dict[str, object]) -> dict[str, object]:
        if product_id != "mi_tv_75":
            raise AssertionError(f"unexpected product id: {product_id}")
        products[0]["data"].update(data_patch)
        return {"ok": True, "item": products[0], "operation": "update_product"}

    service.update_product = fake_update  # type: ignore[assignment]
    service.adjust_inventory = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not call adjust_inventory for mixed update"))  # type: ignore[assignment]

    text = "把“小米电视75寸”，库存改为499；添加梯度售价，当采购量为10台时，每台单价1899元"
    result = service.command(text, use_llm=False)
    assert_equal(str(result.get("action") or ""), "update_product", "mixed command should use update_product")
    updated_fields = result.get("updated_fields") if isinstance(result, dict) else []
    assert_true("inventory" in (updated_fields or []), "updated_fields should include inventory")
    assert_true("price_tiers" in (updated_fields or []), "updated_fields should include price_tiers")
    assert_true("price" not in (updated_fields or []), "mixed tier-only sentence should not overwrite base price")
    assert_equal(int(products[0]["data"]["inventory"]), 499, "inventory should be updated to 499")
    tiers = products[0]["data"].get("price_tiers")
    assert_true(isinstance(tiers, list) and bool(tiers), "price tiers should be populated")
    first = tiers[0]
    assert_equal(int(first.get("min_quantity") or 0), 10, "tier min_quantity should be 10")
    assert_equal(float(first.get("unit_price") or 0.0), 1899.0, "tier unit_price should be 1899")


def check_standard_field_update_extraction() -> None:
    service = ProductConsoleService()
    products = [
        {
            "id": "mi_tv_75",
            "status": "active",
            "data": {"name": "小米电视75寸", "sku": "100C", "inventory": 30},
        }
    ]
    service.store.list_items = lambda category_id, include_archived=False: products if category_id == "products" else []  # type: ignore[assignment]

    command = "把小米电视75寸型号改为100C-Pro，单位改为台，规格改为75寸4K，发货改为江浙沪次日达，售后改为整机2年保修，别名改为小米大屏/75寸电视"
    plan = service.command(command, use_llm=False, dry_run=True)
    assert_equal(str(plan.get("action") or ""), "update_product", "should resolve as update_product")
    fields = plan.get("fields") if isinstance(plan, dict) else {}
    assert_true(isinstance(fields, dict), "fields should be object")
    assert_equal(str(fields.get("sku") or ""), "100C-Pro", "sku should be extracted")
    assert_equal(str(fields.get("unit") or ""), "台", "unit should be extracted")
    assert_equal(str(fields.get("specs") or ""), "75寸4K", "specs should be extracted")
    assert_equal(str(fields.get("shipping_policy") or ""), "江浙沪次日达", "shipping policy should be extracted")
    assert_equal(str(fields.get("warranty_policy") or ""), "整机2年保修", "warranty policy should be extracted")
    aliases = fields.get("aliases")
    assert_true(isinstance(aliases, list) and "小米大屏" in aliases and "75寸电视" in aliases, "aliases should be extracted")


def check_missing_tier_price_prompt() -> None:
    service = ProductConsoleService()
    products = [
        {
            "id": "mi_tv_75",
            "status": "active",
            "data": {"name": "小米电视75寸", "sku": "100C", "inventory": 30},
        }
    ]
    service.store.list_items = lambda category_id, include_archived=False: products if category_id == "products" else []  # type: ignore[assignment]
    try:
        service.command("把小米电视75寸添加梯度售价", use_llm=False, dry_run=True)
    except ValueError as exc:
        message = str(exc)
        assert_true("梯度售价" in message, "missing-tier prompt should mention 梯度售价")
        return
    raise AssertionError("should raise missing-tier prompt")


def check_uncertain_update_prompts_clarification() -> None:
    service = ProductConsoleService()
    products = [
        {
            "id": "mi_tv_75",
            "status": "active",
            "data": {"name": "小米电视75寸", "sku": "100C", "inventory": 30},
        }
    ]
    service.store.list_items = lambda category_id, include_archived=False: products if category_id == "products" else []  # type: ignore[assignment]
    try:
        service.command("把这个商品调整一下", use_llm=False, dry_run=True)
    except ValueError as exc:
        message = str(exc)
        assert_true(("商品名称" in message) or ("SKU" in message), "uncertain update should ask for product clarification")
        return
    raise AssertionError("should raise clarification prompt")


def check_llm_scoped_faq_dry_run_plan() -> None:
    service = ProductConsoleService()
    products = [
        {
            "id": "mi_tv_75",
            "status": "active",
            "data": {"name": "小米电视75寸", "sku": "100C", "inventory": 30},
        }
    ]
    service.store.list_items = lambda category_id, include_archived=False: products if category_id == "products" else []  # type: ignore[assignment]
    original_llm = product_console_module._call_llm_for_command
    product_console_module._call_llm_for_command = lambda text, products, use_llm=True: {  # type: ignore[assignment]
        "intent": "create_product_faq",
        "target_product_id": "mi_tv_75",
        "target_product_name": "小米电视75寸",
        "confidence": 0.92,
        "scoped_fields": {
            "title": "七天无理由退换",
            "question": "可以七天无理由退换吗？",
            "answer": "支持七天无理由退换，保持配件齐全即可。",
            "keywords": ["七天无理由", "退换"],
        },
    }
    try:
        plan = service.command("小米电视75寸 客户问可以七天无理由退换吗？回答可以。", use_llm=True, dry_run=True)
    finally:
        product_console_module._call_llm_for_command = original_llm  # type: ignore[assignment]
    assert_equal(str(plan.get("action") or ""), "create_product_faq", "llm should classify as product scoped faq")
    assert_equal(str(plan.get("target_product_id") or ""), "mi_tv_75", "faq action should keep product binding")
    fields = plan.get("fields") if isinstance(plan, dict) else {}
    assert_true(isinstance(fields, dict), "faq plan fields should be object")
    assert_equal(str(fields.get("title") or ""), "七天无理由退换", "faq title should be present")
    assert_equal(str(fields.get("answer") or ""), "支持七天无理由退换，保持配件齐全即可。", "faq answer should be present")


def check_llm_followup_question_preferred() -> None:
    service = ProductConsoleService()
    products = [
        {
            "id": "mi_tv_75",
            "status": "active",
            "data": {"name": "小米电视75寸", "sku": "100C", "inventory": 30},
        }
    ]
    service.store.list_items = lambda category_id, include_archived=False: products if category_id == "products" else []  # type: ignore[assignment]
    original_llm = product_console_module._call_llm_for_command
    product_console_module._call_llm_for_command = lambda text, products, use_llm=True: {  # type: ignore[assignment]
        "intent": "unknown",
        "target_product_id": "mi_tv_75",
        "target_product_name": "小米电视75寸",
        "confidence": 0.32,
        "missing_info": "缺少标准回复",
        "followup_question": "我理解你在加商品专属知识，请补充“客户问法”和“标准回复”各一句。",
    }
    try:
        try:
            service.command("小米电视这个需求怎么处理", use_llm=True, dry_run=True)
        except ValueError as exc:
            message = str(exc)
            assert_true("客户问法" in message and "标准回复" in message, "should prioritize llm follow-up question")
            return
    finally:
        product_console_module._call_llm_for_command = original_llm  # type: ignore[assignment]
    raise AssertionError("should raise llm follow-up question")


def check_llm_scoped_rule_apply() -> None:
    service = ProductConsoleService()
    products = [
        {
            "id": "mi_tv_75",
            "status": "active",
            "data": {"name": "小米电视75寸", "sku": "100C", "inventory": 30},
        }
    ]
    saved_payload: dict[str, object] = {}
    service.store.list_items = lambda category_id, include_archived=False: products if category_id == "products" else []  # type: ignore[assignment]

    def fake_save(category_id: str, item: dict[str, object]) -> dict[str, object]:
        saved_payload["category_id"] = category_id
        saved_payload["item"] = item
        return {"ok": True, "item": item}

    service.store.save_item = fake_save  # type: ignore[assignment]
    service.compiler.compile_to_disk = lambda: None  # type: ignore[assignment]
    original_llm = product_console_module._call_llm_for_command
    product_console_module._call_llm_for_command = lambda text, products, use_llm=True: {  # type: ignore[assignment]
        "intent": "create_product_rules",
        "target_product_id": "mi_tv_75",
        "target_product_name": "小米电视75寸",
        "confidence": 0.93,
        "scoped_fields": {
            "title": "超低价承诺转人工",
            "answer": "如果客户要求低于成本价，统一转人工处理。",
            "keywords": ["最低价", "砍价", "底价"],
            "requires_handoff": True,
            "handoff_reason": "涉及超权限价格承诺",
        },
    }
    try:
        result = service.command("给小米电视加一条专属规则：客户问最低价时必须转人工。", use_llm=True, dry_run=False)
    finally:
        product_console_module._call_llm_for_command = original_llm  # type: ignore[assignment]
    assert_equal(str(result.get("action") or ""), "create_product_rules", "llm rule should execute scoped creation")
    assert_equal(str(saved_payload.get("category_id") or ""), "product_rules", "scoped rule should save under product_rules")
    item = saved_payload.get("item")
    assert_true(isinstance(item, dict), "saved scoped rule item should be object")
    data = item.get("data") if isinstance(item, dict) else {}
    assert_equal(str((data or {}).get("product_id") or ""), "mi_tv_75", "scoped rule should keep product_id")
    assert_equal(str((data or {}).get("handoff_reason") or ""), "涉及超权限价格承诺", "handoff reason should be stored")


def check_reply_templates_merge_without_overwrite() -> None:
    service = ProductConsoleService()
    existing_item = {
        "id": "mi_tv_75",
        "status": "active",
        "data": {
            "name": "小米电视75寸",
            "reply_templates": {
                "default": "原默认回复",
                "after_sales": "原售后回复",
            },
        },
    }
    service.get_product_item = lambda product_id, include_archived=True: existing_item  # type: ignore[assignment]
    captured: dict[str, object] = {}

    def fake_save(item: dict[str, object], *, operation: str) -> dict[str, object]:
        captured["item"] = item
        captured["operation"] = operation
        return {"ok": True, "item": item, "operation": operation}

    service.save_product_item = fake_save  # type: ignore[assignment]
    result = service.update_product("mi_tv_75", {"reply_templates": {"上门安装咨询": "需要额外安装费150元"}})
    assert_equal(str(result.get("operation") or ""), "update_product", "operation should be update_product")
    saved_item = captured.get("item")
    assert_true(isinstance(saved_item, dict), "saved item should be object")
    templates = (saved_item.get("data") or {}).get("reply_templates") if isinstance(saved_item, dict) else {}
    assert_true(isinstance(templates, dict), "reply_templates should be object")
    assert_equal(str((templates or {}).get("default") or ""), "原默认回复", "existing default template should be preserved")
    assert_equal(str((templates or {}).get("after_sales") or ""), "原售后回复", "existing after_sales template should be preserved")
    assert_equal(str((templates or {}).get("上门安装咨询") or ""), "需要额外安装费150元", "new scene template should be merged")


def check_llm_reply_template_scene_reroute_to_faq() -> None:
    service = ProductConsoleService()
    products = [
        {
            "id": "mi_tv_75",
            "status": "active",
            "data": {"name": "小米电视75寸", "sku": "100C", "inventory": 30},
        }
    ]
    service.store.list_items = lambda category_id, include_archived=False: products if category_id == "products" else []  # type: ignore[assignment]
    original_llm = product_console_module._call_llm_for_command
    product_console_module._call_llm_for_command = lambda text, products, use_llm=True: {  # type: ignore[assignment]
        "intent": "update_product",
        "target_product_id": "mi_tv_75",
        "target_product_name": "小米电视75寸",
        "confidence": 0.93,
        "fields": {
            "reply_templates": {
                "上门安装咨询": "可以，需要增加额外安装费用150元，请联系人工客服，确定上门时间、地点",
            }
        },
    }
    try:
        plan = service.command("小米电视75寸 上门安装咨询：可以，需要增加额外安装费用150元，请联系人工客服。", use_llm=True, dry_run=True)
    finally:
        product_console_module._call_llm_for_command = original_llm  # type: ignore[assignment]
    assert_equal(str(plan.get("action") or ""), "create_product_faq", "scene-like reply_templates should reroute to product_faq")
    fields = plan.get("fields") if isinstance(plan, dict) else {}
    assert_true(isinstance(fields, dict), "rerouted plan fields should be object")
    assert_equal(str(fields.get("title") or ""), "上门安装咨询", "faq title should use scene name")
    assert_true("安装" in str(fields.get("question") or ""), "faq question should be derived from scene name")
    assert_true("150元" in str(fields.get("answer") or ""), "faq answer should carry original reply text")


def check_scoped_intent_without_product_has_clear_prompt() -> None:
    service = ProductConsoleService()
    service.store.list_items = lambda category_id, include_archived=False: []  # type: ignore[assignment]
    original_llm = product_console_module._call_llm_for_command
    product_console_module._call_llm_for_command = lambda text, products, use_llm=True: {  # type: ignore[assignment]
        "intent": "unknown",
        "target_product_id": "",
        "target_product_name": "",
        "confidence": 0.9,
        "missing_info": "没有明确的操作指令（如设置库存、更新价格等）。",
        "followup_question": "",
    }
    try:
        try:
            service.command("小米电视 客户问：可以上门安装吗？ 回答：可以，上门安装额外加收150元安装费。", use_llm=True, dry_run=True)
        except ValueError as exc:
            message = str(exc)
            assert_true("专属问答/规则" in message and ("商品名" in message or "SKU" in message), "should prompt product binding clearly")
            return
    finally:
        product_console_module._call_llm_for_command = original_llm  # type: ignore[assignment]
    raise AssertionError("should raise clear product-binding prompt")


def check_llm_default_reply_template_reroute_with_scoped_context() -> None:
    service = ProductConsoleService()
    products = [
        {
            "id": "xiaomi-tv-75-100c",
            "status": "active",
            "data": {"name": "小米电视75寸", "sku": "100C", "inventory": 30},
        }
    ]
    service.store.list_items = lambda category_id, include_archived=False: products if category_id == "products" else []  # type: ignore[assignment]
    original_llm = product_console_module._call_llm_for_command
    product_console_module._call_llm_for_command = lambda text, products, use_llm=True: {  # type: ignore[assignment]
        "intent": "update_product",
        "target_product_id": "xiaomi-tv-75-100c",
        "target_product_name": "小米电视75寸",
        "confidence": 0.93,
        "fields": {
            "reply_templates": {
                "default": "可以，上门安装额外加收150元安装费。请联系人工客服登记上门时间和地点",
            }
        },
    }
    text = "小米电视 客户问：可以上门安装吗？\n回答：可以，上门安装额外加收150元安装费。请联系人工客服登记上门时间和地点\n更新专属回答"
    try:
        plan = service.command(text, use_llm=True, dry_run=True)
    finally:
        product_console_module._call_llm_for_command = original_llm  # type: ignore[assignment]
    assert_equal(str(plan.get("action") or ""), "create_product_faq", "default reply_templates should reroute when scoped QA context exists")
    fields = plan.get("fields") if isinstance(plan, dict) else {}
    assert_true(isinstance(fields, dict), "rerouted faq fields should be object")
    assert_true("上门安装" in str(fields.get("title") or ""), "rerouted faq title should reflect scene/question")
    assert_true("上门安装" in str(fields.get("question") or ""), "rerouted faq question should be extracted from context")
    assert_true("150元" in str(fields.get("answer") or ""), "rerouted faq answer should preserve reply text")


def check_scoped_faq_auto_keywords_generated() -> None:
    service = ProductConsoleService()
    captured: dict[str, object] = {}

    def fake_save(category_id: str, item: dict[str, object]) -> dict[str, object]:
        captured["category_id"] = category_id
        captured["item"] = item
        return {"ok": True, "item": item}

    service.store.save_item = fake_save  # type: ignore[assignment]
    service.compiler.compile_to_disk = lambda: None  # type: ignore[assignment]
    service.create_product_scoped_knowledge(
        category_id="product_faq",
        target_product_id="xiaomi-tv-75-100c",
        target_product_name="小米电视75寸",
        data_patch={
            "question": "可以上门安装吗？",
            "answer": "可以，上门安装额外加收150元安装费。请联系人工客服登记上门时间和地点",
        },
        source_text="小米电视 客户问：可以上门安装吗？ 回答：可以，上门安装额外加收150元安装费。请联系人工客服登记上门时间和地点",
    )
    item = captured.get("item")
    assert_true(isinstance(item, dict), "saved scoped faq item should be object")
    data = item.get("data") if isinstance(item, dict) else {}
    keywords = (data or {}).get("keywords")
    assert_true(isinstance(keywords, list) and bool(keywords), "scoped faq should auto-generate keywords")
    joined = " ".join(str(part) for part in keywords)
    assert_true("上门安装" in joined, "auto keywords should include scene term")


def assert_equal(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


if __name__ == "__main__":
    raise SystemExit(main())
