"""Checks for classified knowledge runtime and evidence resolution."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
TEST_ROOT = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "test_artifacts" / "knowledge_runtime_checks"
PRIORITY_TENANT_ID = "knowledge_priority_probe"
for path in (PROJECT_ROOT, WORKFLOWS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")

from apps.wechat_ai_customer_service.admin_backend.services.knowledge_base_store import KnowledgeBaseStore  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.formal_review_state import acknowledge_item  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.knowledge_registry import KnowledgeRegistry  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.knowledge_schema_manager import KnowledgeSchemaManager  # noqa: E402
from apps.wechat_ai_customer_service.knowledge_paths import SHARED_KNOWLEDGE_ROOT, default_admin_knowledge_base_root, shared_runtime_cache_root, shared_runtime_snapshot_path, tenant_context, tenant_knowledge_base_root, tenant_root  # noqa: E402
from apps.wechat_ai_customer_service.product_master import ProductMasterStore  # noqa: E402
from evidence_resolver import EvidenceResolver  # noqa: E402
from knowledge_runtime import KnowledgeRuntime  # noqa: E402

SHARED_PRIORITY_ITEM = shared_runtime_cache_root() / "risk_control" / "items" / "shared_priority_sample_fee.json"
WINDOWS_FILE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.25)


def main() -> int:
    cleanup()
    cache_had_previous = prepare_shared_runtime_cache_fixture()
    results = []
    try:
        for check in CHECKS:
            try:
                check()
                results.append({"name": check.__name__, "ok": True})
            except Exception as exc:
                results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
                break
        failures = [item for item in results if not item["ok"]]
        print(json.dumps({"ok": not failures, "count": len(results), "failures": failures, "results": results}, ensure_ascii=False, indent=2))
        return 1 if failures else 0
    finally:
        cleanup()
        restore_shared_runtime_cache_fixture(cache_had_previous)


def check_product_alias_hits_products() -> None:
    pack = resolve("商用冰箱多少钱？")
    assert_has_item(pack, "products", "commercial_fridge_bx_200")
    assert_in("products", pack["matched_categories"], "product category should be matched")
    assert_equal(pack["safety"]["must_handoff"], False, "normal product question should not hand off")


def check_public_product_discount_not_blocked_by_chat_template() -> None:
    pack = resolve("买10台商用冰箱有优惠吗？")
    assert_has_item(pack, "products", "commercial_fridge_bx_200")
    assert_has_item(pack, "chats", "discount_handoff")
    assert_equal(pack["safety"]["must_handoff"], False, "public product tier discount should not be blocked by generic chat template")


def check_invoice_hits_policies() -> None:
    pack = resolve("可以开增值税专用发票吗？")
    assert_has_item(pack, "policies", "invoice")
    assert_equal(pack["safety"]["allowed_auto_reply"], True, "invoice policy should be auto-replyable")


def check_public_bank_account_can_auto_reply() -> None:
    pack = resolve("对公账户和银行账号发我一个")
    assert_has_item(pack, "policies", "bank_account")
    assert_equal(pack["safety"]["must_handoff"], False, "public bank account info should remain auto-replyable")


def check_context_product_shipping() -> None:
    pack = resolve("发江苏南京，包邮吗？", context={"last_product_id": "commercial_fridge_bx_200"})
    item = assert_has_item(pack, "products", "commercial_fridge_bx_200")
    assert_in("conversation_context", item["matched_fields"], "context product should be marked")
    assert_contains(item["reply_excerpt"], "江浙沪", "context product excerpt should include shipping policy")


def check_chat_style_hits_chats() -> None:
    pack = resolve("哈哈我先随便看看，客服辛苦了")
    assert_has_item(pack, "chats", "small_talk_service_pivot")
    assert_equal(pack["safety"]["must_handoff"], False, "small talk can stay auto-replyable")


def check_global_guidelines_are_layered_style_context() -> None:
    pack = resolve("\u4f60\u597d\uff0c\u6211\u5148\u968f\u4fbf\u770b\u770b")
    item = assert_has_item(pack, "global_guidelines", "customer_service_style_guidelines")
    assert_in("always_include", item["matched_fields"], "global guidelines should be layered into normal replies")
    assert_equal(item["requires_handoff"], False, "global guidelines must not force handoff")


def check_contextual_honorific_rule_is_shared_reply_style() -> None:
    pack = resolve("称呼每次都加哥或者老板是不是太假了？")
    item = assert_has_item(pack, "reply_style", "contextual_honorific_usage")
    assert_contains(item["reply_excerpt"], "不要每条回复都在开头加称呼", "shared honorific rule should be retrievable")
    assert_equal(item["requires_handoff"], False, "honorific style rule must not force handoff")


def check_tenant_visible_catalog_policy_drives_used_car_catalog() -> None:
    tenant_id = "knowledge_catalog_policy_probe"
    root = tenant_root(tenant_id)
    if root.exists():
        remove_tree(root)
    try:
        tenant_kb = tenant_knowledge_base_root(tenant_id)
        tenant_kb.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(default_admin_knowledge_base_root(), tenant_kb)
        with tenant_context(tenant_id):
            store = KnowledgeBaseStore()
            store.save_item(
                "policies",
                acknowledge_item(
                    {
                        "schema_version": 1,
                        "category_id": "policies",
                        "id": "visible_catalog_probe_rule",
                        "status": "active",
                        "data": {
                            "title": "车源清单和预算推荐规则",
                            "policy_type": "product_catalog",
                            "keywords": ["车源", "有哪些车", "预算十来万", "家用推荐"],
                            "intent_tags": ["catalog"],
                            "answer": "客户询问车源清单或预算推荐时，先展示已确认在售车辆，再提醒最终车况和价格以人工确认为准。",
                            "allow_auto_reply": True,
                        },
                        "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
                    }
                ),
            )
            for index, (item_id, name, price) in enumerate(
                [
                    ("catalog_probe_camry", "2021款丰田凯美瑞2.0G豪华版", 8.98),
                    ("catalog_probe_qinplus", "2022款比亚迪秦PLUS DM-i 55KM", 8.68),
                    ("catalog_probe_gl8", "2020款别克GL8 ES陆尊653T豪华型", 17.66),
                ],
                start=1,
            ):
                store.save_item(
                    "products",
                    acknowledge_item(
                        {
                            "schema_version": 1,
                            "category_id": "products",
                            "id": item_id,
                            "status": "active",
                            "data": {
                                "name": name,
                                "sku": f"CATALOG-PROBE-{index}",
                                "category": "二手车/演示车源",
                                "aliases": ["车源", "家用推荐", "预算十来万"],
                                "specs": "测试用已确认车源。",
                                "price": price,
                                "unit": "台",
                                "inventory": 1,
                            },
                            "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
                        }
                    ),
                )
            pack = EvidenceResolver().resolve("你们现在有哪些车源？预算十来万家用推荐哪几台？")
    finally:
        if root.exists():
            remove_tree(root)
    assert_in("catalog", pack["intent_tags"], "visible tenant catalog policy should infer catalog intent")
    assert_has_item(pack, "policies", "visible_catalog_probe_rule")
    products = [item for item in pack.get("evidence_items", []) or [] if item.get("category_id") == "products"]
    if len(products) < 3:
        raise AssertionError(f"catalog intent should bring visible tenant products into evidence: {pack}")
    assert_equal(pack["safety"]["must_handoff"], False, "visible tenant catalog rule should allow normal recommendation")


def check_tenant_formal_overrides_conflicting_shared_public() -> None:
    cleanup_priority_fixture()
    try:
        tenant_kb = tenant_knowledge_base_root(PRIORITY_TENANT_ID)
        tenant_kb.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(default_admin_knowledge_base_root(), tenant_kb)
        write_json_file(
            SHARED_PRIORITY_ITEM,
            {
                "schema_version": 1,
                "category_id": "risk_control",
                "id": "shared_priority_sample_fee",
                "status": "active",
                "data": {
                    "title": "星火服务费必须转人工",
                    "keywords": ["星火服务费"],
                    "guideline_text": "涉及星火服务费时，公共规则要求先转人工确认。",
                    "requires_handoff": True,
                    "allow_auto_reply": False,
                    "handoff_reason": "shared_star_fee_risk",
                },
                "runtime": {"allow_auto_reply": False, "requires_handoff": True, "risk_level": "high"},
            },
        )
        write_json_file(
            tenant_kb / "policies" / "items" / "tenant_sample_fee_override.json",
            {
                "schema_version": 1,
                "category_id": "policies",
                "id": "tenant_sample_fee_override",
                "status": "active",
                "data": {
                    "title": "星火服务费抵扣规则",
                    "policy_type": "after_sales",
                    "keywords": ["星火服务费"],
                    "answer": "星火服务费可以按客户专属正式政策处理，按后台记录说明即可自动回复。",
                    "allow_auto_reply": True,
                    "requires_handoff": False,
                },
                "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
            },
        )
        with tenant_context(PRIORITY_TENANT_ID):
            pack = EvidenceResolver().resolve("星火服务费可以处理吗？")
        policy = assert_has_item(pack, "policies", "tenant_sample_fee_override")
        assert_equal(policy.get("knowledge_layer"), "tenant", "tenant formal policy should be marked as tenant layer")
        assert_no_item(pack, "risk_control", "shared_priority_sample_fee")
        assert_equal(pack["safety"]["must_handoff"], False, "tenant formal knowledge should override conflicting shared risk control")
    finally:
        cleanup_priority_fixture()


def check_product_scoped_rule_requires_product_context() -> None:
    pack = resolve("\u667a\u80fd\u6307\u7eb9\u95e8\u9501 FL-920 \u5b89\u88c5\u670d\u52a1\u600e\u4e48\u786e\u8ba4\uff1f")
    assert_has_item(pack, "products", "fl-920")
    scoped = assert_has_item(pack, "product_rules", "door-lock-installation")
    assert_equal(scoped["requires_handoff"], True, "product-specific installation rule should require handoff")

    unrelated = resolve("\u666e\u901a\u5b89\u88c5\u670d\u52a1\u600e\u4e48\u786e\u8ba4\uff1f")
    for item in unrelated.get("evidence_items", []) or []:
        if item.get("category_id") == "product_rules" and item.get("item_id") == "door-lock-installation":
            raise AssertionError(f"product-scoped rule leaked without product context: {unrelated}")


def check_general_knowledge_applicability_scope() -> None:
    root = prepare_custom_root()
    policy_path = root / "policies" / "items" / "admin_scope_specific_policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "category_id": "policies",
                "id": "admin_scope_specific_policy",
                "status": "active",
                "data": {
                    "title": "冰箱专属售后规则",
                    "policy_type": "after_sales",
                    "keywords": ["售后规则", "保修"],
                    "applicability_scope": "specific_product",
                    "product_id": "commercial_fridge_bx_200",
                    "answer": "商用冰箱售后按整机一年、压缩机三年处理。",
                    "allow_auto_reply": True,
                    "requires_handoff": False,
                },
                "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    resolver = EvidenceResolver(KnowledgeRuntime(root=root))
    unrelated = resolver.resolve("售后规则怎么处理？")
    for item in unrelated.get("evidence_items", []) or []:
        if item.get("category_id") == "policies" and item.get("item_id") == "admin_scope_specific_policy":
            raise AssertionError(f"product-specific policy leaked without product context: {unrelated}")
    matched = resolver.resolve("商用冰箱售后规则怎么处理？")
    assert_has_item(matched, "products", "commercial_fridge_bx_200")
    assert_has_item(matched, "policies", "admin_scope_specific_policy")


def check_manual_boundary_requires_handoff() -> None:
    pack = resolve("这个订单能不能月结，合同可以盖章吗？")
    assert_equal(pack["safety"]["must_handoff"], True, "contract/monthly payment needs handoff")
    assert_in("handoff_intent_detected", pack["safety"]["reasons"], "handoff intent should be recorded")
    assert_has_category(pack, "policies")


def check_unknown_business_question_handoffs() -> None:
    pack = resolve("你们老板喜欢什么颜色的咖啡杯？")
    assert_equal(pack["safety"]["must_handoff"], True, "unknown business-adjacent question should hand off")
    assert_in("no_relevant_business_evidence", pack["safety"]["reasons"], "unknown question should record missing evidence")

    packaging_pack = resolve("你们老板喜欢什么颜色的包装？\n[live-regression:test:19:1]")
    assert_equal(
        packaging_pack["safety"]["must_handoff"],
        True,
        "weak policy answer match plus live marker should still hand off",
    )
    assert_in(
        "no_relevant_business_evidence",
        packaging_pack["safety"]["reasons"],
        "weak policy answer match should not count as authoritative evidence",
    )

    product_pack = resolve("包装纸箱多少钱？")
    assert_has_item(product_pack, "products", "packing_carton_ct_50")
    assert_equal(product_pack["safety"]["must_handoff"], False, "normal packaging product question should stay auto-replyable")

    address_pack = resolve("老板，包装纸箱是什么颜色？")
    assert_has_item(address_pack, "products", "packing_carton_ct_50")
    assert_equal(address_pack["safety"]["must_handoff"], False, "addressing the merchant as boss should not be treated as private preference")


def check_custom_category_can_return_evidence() -> None:
    root = prepare_custom_root()
    resolver = EvidenceResolver(KnowledgeRuntime(root=root))
    pack = resolver.resolve("黑金卡权益是什么？")
    assert_has_item(pack, "custom_runtime", "black_card_rules")
    assert_in("custom_runtime", pack["matched_categories"], "custom category should be visible to runtime")


def check_unread_formal_product_is_visible_but_not_runtime_usable() -> None:
    root = prepare_custom_root()
    store = ProductMasterStore()
    item = {
        "schema_version": 1,
        "category_id": "products",
        "id": "runtime_unread_probe_sedan",
        "status": "active",
        "review_state": {"is_new": True},
        "data": {
            "name": "Runtime Unread Probe Sedan",
            "sku": "RUNTIME-UNREAD-PROBE",
            "category": "二手车/测试",
            "price": 12345,
            "unit": "台",
            "inventory": 1,
            "aliases": ["unread probe"],
            "specs": "Only used to verify unread formal products stay out of reply runtime.",
        },
        "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
    }
    path = store.item_path("runtime_unread_probe_sedan")
    try:
        result = store.save_item(item)
        if not result.get("ok"):
            raise AssertionError(f"unread product save failed: {result}")

        runtime = KnowledgeRuntime(root=root)
        visible_ids = {str(record.get("id") or "") for record in runtime.list_items("products", include_unacknowledged=True)}
        active_ids = {str(record.get("id") or "") for record in runtime.list_items("products")}
        assert_in("runtime_unread_probe_sedan", list(visible_ids), "unread product_master item should remain admin-visible")
        if "runtime_unread_probe_sedan" in active_ids:
            raise AssertionError("unread product_master item leaked into reply runtime")
        if runtime.get_item("products", "runtime_unread_probe_sedan") is not None:
            raise AssertionError("unread product_master get_item leaked into reply runtime")

        hidden_pack = EvidenceResolver(runtime).resolve("Runtime Unread Probe Sedan 这辆二手车多少钱？")
        assert_no_item(hidden_pack, "products", "runtime_unread_probe_sedan")

        store.save_item(acknowledge_item(item))
        acknowledged_pack = EvidenceResolver(KnowledgeRuntime(root=root)).resolve("Runtime Unread Probe Sedan 这辆二手车多少钱？")
        assert_has_item(acknowledged_pack, "products", "runtime_unread_probe_sedan")
    finally:
        path.unlink(missing_ok=True)


def resolve(text: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    return EvidenceResolver().resolve(text, context=context or {})


def prepare_custom_root() -> Path:
    cleanup()
    shutil.copytree(default_admin_knowledge_base_root(), TEST_ROOT)
    registry = KnowledgeRegistry(root=TEST_ROOT)
    registry.create_custom_category(
        "custom_runtime",
        "Runtime Custom",
        "Used only by runtime checks.",
        participates_in_reply=True,
    )
    schema_manager = KnowledgeSchemaManager(registry)
    store = KnowledgeBaseStore(registry, schema_manager)
    result = store.save_item(
        "custom_runtime",
        {
            "schema_version": 1,
            "category_id": "custom_runtime",
            "id": "black_card_rules",
            "data": {
                "title": "黑金卡规则",
                "content": "黑金卡权益属于测试自定义知识，可按会员规则介绍，但实际承诺需人工确认。",
            },
            "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
        },
    )
    if not result.get("ok"):
        raise AssertionError(f"custom item save failed: {result}")
    return TEST_ROOT


def cleanup() -> None:
    if TEST_ROOT.exists():
        remove_tree(TEST_ROOT)
    cleanup_priority_fixture()


def prepare_shared_runtime_cache_fixture() -> bool:
    source_root = SHARED_KNOWLEDGE_ROOT
    cache_root = shared_runtime_cache_root()
    backup_root = TEST_ROOT.parent / "knowledge_runtime_previous_shared_runtime_cache"
    had_previous = cache_root.exists()
    if backup_root.exists():
        remove_tree(backup_root)
    backup_root.parent.mkdir(parents=True, exist_ok=True)
    previous_snapshot = None
    if had_previous:
        prev_snapshot_path = cache_root / "snapshot.json"
        if prev_snapshot_path.exists():
            try:
                previous_snapshot = json.loads(prev_snapshot_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        shutil.copytree(cache_root, backup_root)
        remove_tree(cache_root)
    if source_root.exists():
        cache_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_root, cache_root)
        write_cloud_cache_snapshot_fixture(cache_root, previous_snapshot=previous_snapshot)
    return had_previous


def restore_shared_runtime_cache_fixture(had_previous: bool) -> None:
    cache_root = shared_runtime_cache_root()
    backup_root = TEST_ROOT.parent / "knowledge_runtime_previous_shared_runtime_cache"
    if cache_root.exists():
        remove_tree(cache_root)
    if had_previous and backup_root.exists():
        shutil.copytree(backup_root, cache_root)
    if backup_root.exists():
        remove_tree(backup_root)


def cleanup_priority_fixture() -> None:
    root = tenant_root(PRIORITY_TENANT_ID)
    if root.exists():
        remove_tree(root)
    if SHARED_PRIORITY_ITEM.exists():
        remove_file(SHARED_PRIORITY_ITEM)


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        replace_path(temp, path)
    finally:
        if temp.exists():
            remove_file(temp)


def is_transient_windows_file_error(exc: OSError) -> bool:
    return (
        os.name == "nt"
        and (
            int(getattr(exc, "errno", 0) or 0) in {13, 22}
            or int(getattr(exc, "winerror", 0) or 0) in {5, 32, 33}
        )
    )


def replace_path(source: Path, destination: Path) -> None:
    last_error: OSError | None = None
    for index, delay in enumerate(WINDOWS_FILE_RETRY_DELAYS):
        try:
            source.replace(destination)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if not is_transient_windows_file_error(exc):
                raise
            if index < len(WINDOWS_FILE_RETRY_DELAYS) - 1:
                time.sleep(delay)
    if last_error is not None:
        raise last_error


def remove_tree(path: Path) -> None:
    last_error: OSError | None = None
    for index, delay in enumerate(WINDOWS_FILE_RETRY_DELAYS):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if not is_transient_windows_file_error(exc):
                raise
            if index < len(WINDOWS_FILE_RETRY_DELAYS) - 1:
                time.sleep(delay)
    if last_error is not None:
        raise last_error


def remove_file(path: Path) -> None:
    last_error: OSError | None = None
    for index, delay in enumerate(WINDOWS_FILE_RETRY_DELAYS):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if not is_transient_windows_file_error(exc):
                raise
            if index < len(WINDOWS_FILE_RETRY_DELAYS) - 1:
                time.sleep(delay)
    if last_error is not None:
        raise last_error


def write_cloud_cache_snapshot_fixture(cache_root: Path, previous_snapshot: dict[str, Any] | None = None) -> None:
    now = datetime.now(timezone.utc)
    # Preserve policy_bundle from the real cloud snapshot so intent_keywords remain available.
    existing_snapshot: dict[str, Any] = previous_snapshot or {}
    if not existing_snapshot:
        real_snapshot_path = shared_runtime_snapshot_path()
        if real_snapshot_path.exists():
            try:
                existing_snapshot = json.loads(real_snapshot_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
    policy_bundle = existing_snapshot.get("policy_bundle", {})
    # policy_bundle structure: {global: {platform_understanding_rules: {...}}, industry: {...}, merged: {platform_understanding_rules: {...}}}
    pur = (
        policy_bundle.get("merged", {}).get("platform_understanding_rules", {})
        or policy_bundle.get("global", {}).get("platform_understanding_rules", {})
        or {}
    )
    snapshot = {
        "schema_version": 1,
        "source": "cloud_official_shared_library",
        "version": "shared-runtime-fixture",
        "tenant_id": "default",
        "generated_at": now.isoformat(timespec="seconds"),
        "issued_at": now.isoformat(timespec="seconds"),
        "refresh_after_at": (now + timedelta(minutes=5)).isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
        "ttl_seconds": 3600,
        "refresh_after_seconds": 300,
        "lease_id": "shared_lease_runtime_fixture",
        "cache_policy": {
            "mode": "cloud_authoritative_lease",
            "ttl_seconds": 3600,
            "refresh_after_seconds": 300,
            "issued_at": now.isoformat(timespec="seconds"),
            "refresh_after_at": (now + timedelta(minutes=5)).isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
            "lease_id": "shared_lease_runtime_fixture",
            "requires_cloud_refresh": True,
        },
        "categories": [{"category_id": "global_guidelines", "item_count": 1}],
        "items": [],
    }
    if policy_bundle:
        snapshot["policy_bundle"] = policy_bundle
        # Flatten platform_understanding_rules so normalize_platform_understanding_rules can find intent_keywords at top level.
        if pur:
            for key in ("intent_keywords", "intent_groups", "policy_type_to_intent", "policy_tags", "policy_type_tags", "policy_key_tags", "product_knowledge_keywords", "semantic_equivalents", "rag", "risk_keywords", "customer_data_field_labels", "quantity_units"):
                if key in pur:
                    snapshot[key] = pur[key]
    write_json_file(cache_root / "snapshot.json", snapshot)


def assert_has_item(pack: dict[str, Any], category_id: str, item_id: str) -> dict[str, Any]:
    for item in pack.get("evidence_items", []) or []:
        if item.get("category_id") == category_id and item.get("item_id") == item_id:
            return item
    raise AssertionError(f"missing evidence item: {category_id}/{item_id}; pack={pack}")


def assert_no_item(pack: dict[str, Any], category_id: str, item_id: str) -> None:
    for item in pack.get("evidence_items", []) or []:
        if item.get("category_id") == category_id and item.get("item_id") == item_id:
            raise AssertionError(f"unexpected evidence item: {category_id}/{item_id}; pack={pack}")


def assert_has_category(pack: dict[str, Any], category_id: str) -> None:
    if category_id not in (pack.get("matched_categories") or []):
        raise AssertionError(f"missing category: {category_id}; pack={pack}")


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def assert_in(expected: Any, values: list[Any], message: str) -> None:
    if expected not in values:
        raise AssertionError(f"{message}: expected {expected!r} in {values!r}")


def assert_contains(actual: str, expected: str, message: str) -> None:
    if expected not in actual:
        raise AssertionError(f"{message}: expected {expected!r} in {actual!r}")


CHECKS = [
    check_product_alias_hits_products,
    check_public_product_discount_not_blocked_by_chat_template,
    check_invoice_hits_policies,
    check_public_bank_account_can_auto_reply,
    check_context_product_shipping,
    check_chat_style_hits_chats,
    check_global_guidelines_are_layered_style_context,
    check_contextual_honorific_rule_is_shared_reply_style,
    check_tenant_visible_catalog_policy_drives_used_car_catalog,
    check_tenant_formal_overrides_conflicting_shared_public,
    check_product_scoped_rule_requires_product_context,
    check_general_knowledge_applicability_scope,
    check_manual_boundary_requires_handoff,
    check_unknown_business_question_handoffs,
    check_custom_category_can_return_evidence,
    check_unread_formal_product_is_visible_but_not_runtime_usable,
]


if __name__ == "__main__":
    raise SystemExit(main())
