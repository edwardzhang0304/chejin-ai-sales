"""Populate and verify the Jiangsu Chejin used-car demo tenant.

This script is intentionally data-heavy. It gives the customer account enough
formal knowledge, RAG material, raw recorder messages, and promotion candidates
to demonstrate the full "raw -> RAG experience -> candidate -> formal" flow.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

os.environ.setdefault("WECHAT_STORAGE_BACKEND", "file")
os.environ["WECHAT_VPS_BASE_URL"] = ""
os.environ["WECHAT_VPS_AUTH_REQUIRED"] = "0"
os.environ["WECHAT_VPS_AUTO_DISCOVER"] = "0"

APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
ADAPTERS_ROOT = APP_ROOT / "adapters"
for path in (PROJECT_ROOT, WORKFLOWS_ROOT, APP_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from apps.wechat_ai_customer_service.admin_backend.services.formal_review_state import mark_item_new  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.knowledge_base_store import KnowledgeBaseStore  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.raw_message_learning_service import RawMessageLearningService  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.raw_message_store import RawMessageStore  # noqa: E402
from apps.wechat_ai_customer_service.knowledge_paths import (  # noqa: E402
    tenant_context,
    tenant_rag_sources_root,
    tenant_review_candidates_root,
    tenant_runtime_root,
)
from apps.wechat_ai_customer_service.sync import VpsLocalSyncService  # noqa: E402
from apps.wechat_ai_customer_service.workflows.knowledge_index import KnowledgeIndex  # noqa: E402
from apps.wechat_ai_customer_service.workflows.knowledge_runtime import KnowledgeRuntime  # noqa: E402
from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore  # noqa: E402
from apps.wechat_ai_customer_service.workflows.rag_layer import RagService  # noqa: E402
from run_jiangsu_chejin_used_car_checks import (  # noqa: E402
    BASE_ARTIFACT_ROOT,
    DISPLAY_NAME,
    EMAIL,
    PASSWORD,
    TENANT_ID,
    assert_true,
    check_customer_service_matrix,
    check_recorder_offline_matrix,
    check_tenant_scoped_backend,
    ensure_customer_account,
    login_client,
)


@dataclass(frozen=True)
class ProductFixture:
    item_id: str
    name: str
    sku: str
    category: str
    aliases: list[str]
    specs: str
    price: float
    inventory: int
    shipping_policy: str
    warranty_policy: str
    recommendation: str
    risk_rules: list[str]
    details: dict[str, Any]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true", help="Only verify the enriched demo tenant.")
    parser.add_argument("--skip-llm", action="store_true", help="Disable LLM-assisted candidate generation for deterministic local runs.")
    parser.add_argument("--shared-scan-limit", type=int, default=120)
    args = parser.parse_args()

    token = "CHEJIN_DEMO_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    with tenant_context(TENANT_ID):
        ensure_customer_account()
        if args.verify_only:
            result = {"ok": True, "tenant_id": TENANT_ID, "verification": verify_demo_dataset(token)}
        else:
            saved = seed_formal_demo_knowledge(token)
            materials = write_and_ingest_demo_materials(token)
            raw = seed_raw_wechat_demo_messages(token, use_llm=not args.skip_llm)
            shared = check_shared_candidate_filter(use_llm=not args.skip_llm, limit=args.shared_scan_limit)
            verification = verify_demo_dataset(token)
            result = {
                "ok": True,
                "tenant_id": TENANT_ID,
                "username": TENANT_ID,
                "password": PASSWORD,
                "display_name": DISPLAY_NAME,
                "email": EMAIL,
                "batch_token": token,
                "formal": saved,
                "materials": materials,
                "raw_learning": raw,
                "shared_candidate_scan": shared,
                "verification": verification,
            }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def seed_formal_demo_knowledge(token: str) -> dict[str, Any]:
    store = KnowledgeBaseStore()
    saved: list[dict[str, Any]] = []
    for product in product_fixtures(token):
        save_item(store, "products", product_item(product, token), saved)
    for item in policy_items(token):
        save_item(store, "policies", item, saved)
    for item in chat_items(token):
        save_item(store, "chats", item, saved)
    for item in product_scoped_items(token):
        save_item(store, item["category_id"], item, saved)
    return {
        "ok": True,
        "saved_count": len(saved),
        "by_category": count_by(saved, "category"),
        "saved_sample": saved[:12],
    }


def product_item(product: ProductFixture, token: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "category_id": "products",
        "id": product.item_id,
        "status": "active",
        "source": {"type": "demo_fixture", "batch_token": token, "domain": "used_car"},
        "data": {
            "name": product.name,
            "sku": product.sku,
            "category": product.category,
            "aliases": unique_list([*product.aliases, product.name, product.sku, token]),
            "specs": product.specs,
            "price": product.price,
            "unit": "台",
            "price_tiers": [{"min_quantity": 1, "unit_price": product.price}],
            "inventory": product.inventory,
            "shipping_policy": product.shipping_policy,
            "warranty_policy": product.warranty_policy,
            "reply_templates": {
                "默认回复": product.recommendation,
                "报价回复": f"这台车当前演示价约 {product.price:.2f} 万，最终成交价、金融方案和费用明细需要人工确认。",
                "议价回复": "二手车一车一况，AI 可以说明参考价和看车建议，最终优惠、订金和成交条件必须由销售确认。",
                "物流回复": product.shipping_policy,
                "售后回复": product.warranty_policy,
                "内部备注": "演示资料：用于覆盖二手车常见咨询、风险边界和商品专属知识。",
            },
            "risk_rules": product.risk_rules,
            "additional_details": product.details,
        },
        "runtime": {"allow_auto_reply": True, "requires_handoff": False, "risk_level": "normal"},
    }


def product_fixtures(token: str) -> list[ProductFixture]:
    base_shipping = "南京门店可看车，异地客户可协助提档、物流和临牌事项，具体费用与迁入政策需人工确认。"
    report_policy = "车况以检测报告、合同和实车复核为准；事故、水泡、火烧、调表、抵押查封等风险问题必须人工确认。"
    return [
        ProductFixture(
            "chejin_camry_2021_20g",
            "2021款丰田凯美瑞2.0G豪华版",
            "CHEJIN-CAMRY-2021G",
            "二手车/中级轿车",
            ["凯美瑞", "丰田凯美瑞", "8万预算", "家用通勤", "省油自动挡"],
            "2021年上牌，表显4.8万公里，2.0L自动挡，白色，南京现车，一手家用车。",
            8.98,
            1,
            base_shipping,
            report_policy,
            "适合8到10万预算、家用通勤、省心省油的客户，建议先确认预算、到店时间和是否置换。",
            ["事故/水泡/火烧承诺转人工", "异地迁入政策转人工"],
            {"上牌时间": "2021-06", "表显里程": "4.8万公里", "排放": "国六", "车身颜色": "白色", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_civic_2020_220turbo",
            "2020款本田思域220TURBO劲动版",
            "CHEJIN-CIVIC-2020T",
            "二手车/紧凑型轿车",
            ["思域", "本田思域", "年轻客户", "动力好", "首付三成"],
            "2020年上牌，表显6.1万公里，1.5T自动挡，蓝色，外观轻微补漆。",
            7.58,
            1,
            base_shipping,
            "外观补漆、改装件、贷款审批结果以人工和检测报告确认为准。",
            "适合年轻客户、预算7到8万、关注动力和外观的客户。",
            ["贷款包过禁用", "改装合法性转人工"],
            {"上牌时间": "2020-09", "表显里程": "6.1万公里", "排放": "国六", "车身颜色": "蓝色", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_qinplus_2022_dmi55",
            "2022款比亚迪秦PLUS DM-i 55KM",
            "CHEJIN-QINPLUS-2022DMI",
            "二手车/新能源轿车",
            ["秦PLUS", "比亚迪秦", "新能源", "绿牌", "插混", "省油"],
            "2022年上牌，表显3.2万公里，插电混动，白色，低油耗通勤。",
            8.68,
            1,
            "新能源车需人工确认当地迁入政策、电池检测报告和充电条件。",
            "三电质保、动力电池检测和厂家权益以报告和厂家政策为准。",
            "适合通勤、省油、绿牌需求和网约车咨询，但三电与迁入政策需人工复核。",
            ["电池健康度转人工", "当地迁入政策转人工", "网约车营运合规转人工"],
            {"上牌时间": "2022-04", "表显里程": "3.2万公里", "能源类型": "插电混动", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_gl8_2020_es653t",
            "2020款别克GL8 ES陆尊653T豪华型",
            "CHEJIN-GL8-2020ES653T",
            "二手车/MPV",
            ["GL8", "别克GL8", "商务接待", "七座", "MPV"],
            "2020年上牌，表显7.4万公里，2.0T自动挡，七座商务MPV。",
            17.60,
            1,
            "商务客户可预约试乘，付款方式、门店和时间需人工确认。",
            "内饰磨损、保养记录、商务用途痕迹以实车与检测报告为准。",
            "适合商务接待、多人家庭和七座刚需，建议优先安排到店看内饰和试乘。",
            ["试乘安排转人工", "商务用途磨损解释转人工"],
            {"上牌时间": "2020-11", "表显里程": "7.4万公里", "座位数": "7座", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_bmw320_2019_m",
            "2019款宝马320Li M运动套装",
            "CHEJIN-BMW320-2019M",
            "二手车/豪华轿车",
            ["宝马320", "宝马3系", "320Li", "豪华品牌", "试驾"],
            "2019年上牌，表显5.6万公里，2.0T自动挡，白色，一手车源。",
            12.80,
            1,
            "高意向试驾、金融首付和置换方案需人工确认。",
            "精品车况以检测报告为准，事故、水泡、火烧承诺必须人工确认。",
            "适合12到14万预算、关注品牌和驾驶感的客户。",
            ["试驾转人工", "金融月供转人工", "事故赔付承诺禁用"],
            {"上牌时间": "2019-08", "表显里程": "5.6万公里", "排放": "国六", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_model3_2021_std",
            "2021款特斯拉Model 3 标准续航后驱",
            "CHEJIN-MODEL3-2021STD",
            "二手车/新能源轿车",
            ["Model 3", "特斯拉", "新能源", "电车", "辅助驾驶"],
            "2021年上牌，表显5.2万公里，纯电后驱，白色，支持第三方电池检测。",
            13.98,
            1,
            "外地客户需人工确认充电、提档、当地新能源指标和运输方式。",
            "电池健康、辅助驾驶功能和官方权益以检测报告及账号核验为准。",
            "适合想体验纯电、智能化和低用车成本的客户，电池和权益必须人工确认。",
            ["电池健康转人工", "辅助驾驶权益转人工", "账号权益转人工"],
            {"上牌时间": "2021-10", "表显里程": "5.2万公里", "能源类型": "纯电", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_a4l_2020_40tfsi",
            "2020款奥迪A4L 40TFSI时尚动感型",
            "CHEJIN-A4L-2020",
            "二手车/豪华轿车",
            ["奥迪A4L", "A4L", "豪华轿车", "婚庆", "商务"],
            "2020年上牌，表显6.8万公里，2.0T自动挡，黑色，保养记录较完整。",
            15.88,
            1,
            base_shipping,
            "保养记录、漆面修复和金融方案需人工复核。",
            "适合商务通勤、品牌升级和预算15到17万的客户。",
            ["金融方案转人工", "漆面修复说明转人工"],
            {"上牌时间": "2020-05", "表显里程": "6.8万公里", "排放": "国六", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_crv_2020_240turbo",
            "2020款本田CR-V 240TURBO两驱都市版",
            "CHEJIN-CRV-2020",
            "二手车/SUV",
            ["CR-V", "本田CRV", "家用SUV", "空间大"],
            "2020年上牌，表显5.9万公里，1.5T自动挡，灰色，空间适合家庭。",
            11.98,
            2,
            base_shipping,
            "底盘、轮胎、保养和事故排查以检测报告为准。",
            "适合家庭SUV、空间需求、预算12万左右的客户。",
            ["底盘异响转人工", "保养记录转人工"],
            {"上牌时间": "2020-07", "表显里程": "5.9万公里", "库存说明": "两台相近配置", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_sylphy_2021_xl",
            "2021款日产轩逸1.6L XL CVT悦享版",
            "CHEJIN-SYLPHY-2021",
            "二手车/紧凑型轿车",
            ["轩逸", "日产轩逸", "代步车", "省油", "练手车"],
            "2021年上牌，表显4.2万公里，1.6L CVT，银色，家用代步。",
            6.88,
            1,
            base_shipping,
            "CVT变速箱状态、保养和车况以检测报告为准。",
            "适合6到7万预算、练手、家用代步和省油需求。",
            ["变速箱状态转人工"],
            {"上牌时间": "2021-03", "表显里程": "4.2万公里", "排放": "国六", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_lavida_2019_auto",
            "2019款大众朗逸1.5L自动舒适版",
            "CHEJIN-LAVIDA-2019",
            "二手车/紧凑型轿车",
            ["朗逸", "大众朗逸", "德系代步", "置换"],
            "2019年上牌，表显6.9万公里，1.5L自动挡，白色，通勤耐用。",
            5.98,
            1,
            base_shipping,
            "易损件和保养历史以检测报告与门店实车为准。",
            "适合5到6万预算、刚需代步、品牌接受度高的客户。",
            ["易损件状态转人工"],
            {"上牌时间": "2019-12", "表显里程": "6.9万公里", "排放": "国六", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_highlander_2018_7seat",
            "2018款丰田汉兰达2.0T四驱豪华7座",
            "CHEJIN-HIGHLANDER-2018",
            "二手车/SUV",
            ["汉兰达", "丰田汉兰达", "七座SUV", "保值"],
            "2018年上牌，表显8.8万公里，2.0T四驱，七座，黑色。",
            18.68,
            1,
            "七座SUV异地看车建议提前预约，过户和提档费用人工确认。",
            "四驱系统、底盘和保养记录需人工结合检测报告说明。",
            "适合七座家庭、长途自驾、预算18到20万的客户。",
            ["四驱系统转人工", "七座年检政策转人工"],
            {"上牌时间": "2018-06", "表显里程": "8.8万公里", "座位数": "7座", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_songplus_2023_dmi",
            "2023款比亚迪宋PLUS DM-i冠军版110KM",
            "CHEJIN-SONGPLUS-2023DMI",
            "二手车/新能源SUV",
            ["宋PLUS", "比亚迪宋", "混动SUV", "新能源SUV"],
            "2023年上牌，表显1.9万公里，插混SUV，灰色，配置较新。",
            13.28,
            1,
            "新能源SUV需确认电池检测、充电场景、当地迁入政策。",
            "三电质保、厂家权益和营运属性以人工核验为准。",
            "适合家庭SUV、省油和新能源牌照需求。",
            ["三电质保转人工", "营运属性转人工"],
            {"上牌时间": "2023-08", "表显里程": "1.9万公里", "能源类型": "插电混动", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_miniev_2022_macaron",
            "2022款五菱宏光MINIEV马卡龙臻享款",
            "CHEJIN-MINIEV-2022",
            "二手车/微型电动车",
            ["宏光MINIEV", "五菱mini", "代步电车", "练手车"],
            "2022年上牌，表显1.6万公里，纯电微型车，粉色，城市通勤。",
            2.88,
            1,
            "微型电动车跨城运输和充电条件需人工确认。",
            "续航、电池和低温表现以检测报告和使用场景说明为准。",
            "适合短途通勤、练手、接送孩子，长途或高速需求不推荐。",
            ["续航承诺禁用", "高速长途需求转人工"],
            {"上牌时间": "2022-05", "表显里程": "1.6万公里", "能源类型": "纯电", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_accord_2020_260turbo",
            "2020款本田雅阁260TURBO精英版",
            "CHEJIN-ACCORD-2020",
            "二手车/中级轿车",
            ["雅阁", "本田雅阁", "中级车", "家用商务"],
            "2020年上牌，表显5.5万公里，1.5T自动挡，黑色。",
            10.88,
            1,
            base_shipping,
            "发动机、变速箱和漆面修复以检测报告为准。",
            "适合家用商务兼顾、预算10到12万的客户。",
            ["漆面修复说明转人工"],
            {"上牌时间": "2020-02", "表显里程": "5.5万公里", "排放": "国六", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_rx5_2021_plus",
            "2021款荣威RX5 PLUS 330TGI自动国潮智尊版",
            "CHEJIN-RX5-2021",
            "二手车/SUV",
            ["荣威RX5", "RX5", "国产SUV", "大屏"],
            "2021年上牌，表显3.9万公里，1.5T自动挡，蓝色，配置丰富。",
            5.68,
            1,
            base_shipping,
            "电子配置、车机系统和保养以实车演示与检测报告为准。",
            "适合预算5到6万、想要SUV空间和配置的客户。",
            ["车机功能转人工"],
            {"上牌时间": "2021-11", "表显里程": "3.9万公里", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_macan_2018_20t",
            "2018款保时捷Macan 2.0T",
            "CHEJIN-MACAN-2018",
            "二手车/豪华SUV",
            ["Macan", "保时捷Macan", "豪华SUV", "高端车"],
            "2018年上牌，表显7.2万公里，2.0T自动挡，黑色，需预约看车。",
            23.80,
            1,
            "高端车看车、试驾、订金和付款方案必须人工预约确认。",
            "保养、选配、事故记录、按揭和产权状态必须人工核验。",
            "适合预算20万以上、品牌和外观优先的客户，高端车必须转人工推进。",
            ["高端车订金转人工", "产权状态转人工", "选配核验转人工"],
            {"上牌时间": "2018-04", "表显里程": "7.2万公里", "演示状态": "在售", "高风险推进": True},
        ),
        ProductFixture(
            "chejin_han_2021_ev",
            "2021款比亚迪汉EV超长续航豪华型",
            "CHEJIN-HANEV-2021",
            "二手车/新能源轿车",
            ["汉EV", "比亚迪汉", "纯电轿车", "长续航"],
            "2021年上牌，表显4.6万公里，纯电轿车，红色，长续航版本。",
            13.60,
            1,
            "需人工确认电池检测、充电条件、异地新能源政策。",
            "三电质保、续航衰减和厂家权益以报告和厂家政策为准。",
            "适合想要大空间纯电轿车和低用车成本的客户。",
            ["续航承诺禁用", "三电权益转人工"],
            {"上牌时间": "2021-12", "表显里程": "4.6万公里", "能源类型": "纯电", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_es6_2020_420",
            "2020款蔚来ES6 420KM运动版",
            "CHEJIN-ES6-2020",
            "二手车/新能源SUV",
            ["蔚来ES6", "ES6", "换电", "新能源SUV"],
            "2020年上牌，表显6.4万公里，纯电SUV，灰色，需核验电池租用或买断。",
            12.98,
            1,
            "换电权益、电池租用、账号权益和当地政策必须人工确认。",
            "BaaS、电池状态、官方权益和过户手续以人工核验为准。",
            "适合关注换电体验和纯电SUV的客户，但权益核验必须转人工。",
            ["BaaS权益转人工", "账号权益转人工", "电池买断/租用转人工"],
            {"上牌时间": "2020-10", "表显里程": "6.4万公里", "能源类型": "纯电", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_corolla_2020_12t",
            "2020款丰田卡罗拉1.2T S-CVT精英版",
            "CHEJIN-COROLLA-2020",
            "二手车/紧凑型轿车",
            ["卡罗拉", "丰田卡罗拉", "省油", "练手代步"],
            "2020年上牌，表显5.0万公里，1.2T自动挡，白色。",
            6.98,
            1,
            base_shipping,
            report_policy,
            "适合省油代步、保值、预算7万左右的客户。",
            ["异地迁入政策转人工"],
            {"上牌时间": "2020-01", "表显里程": "5.0万公里", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_mazda3_2020_20",
            "2020款马自达3昂克赛拉2.0L质雅版",
            "CHEJIN-MAZDA3-2020",
            "二手车/紧凑型轿车",
            ["昂克赛拉", "马自达3", "操控", "年轻客户"],
            "2020年上牌，表显4.7万公里，2.0L自动挡，红色。",
            8.38,
            1,
            base_shipping,
            "漆面、底盘、轮胎和保养以检测报告为准。",
            "适合年轻客户、关注操控和外观的客户。",
            ["漆面色差说明转人工"],
            {"上牌时间": "2020-06", "表显里程": "4.7万公里", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_teana_2019_20l",
            "2019款日产天籁2.0L XL舒适版",
            "CHEJIN-TEANA-2019",
            "二手车/中级轿车",
            ["天籁", "日产天籁", "舒适", "家用"],
            "2019年上牌，表显6.2万公里，2.0L CVT，黑色，乘坐舒适。",
            8.28,
            1,
            base_shipping,
            "CVT变速箱状态和保养以检测报告为准。",
            "适合舒适家用、预算8到9万的客户。",
            ["变速箱状态转人工"],
            {"上牌时间": "2019-09", "表显里程": "6.2万公里", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_idealone_2021",
            "2021款理想ONE增程6座版",
            "CHEJIN-IDEALONE-2021",
            "二手车/新能源SUV",
            ["理想ONE", "理想", "六座", "增程SUV"],
            "2021年上牌，表显5.8万公里，增程式SUV，六座，黑色。",
            17.98,
            1,
            "家庭客户看车建议提前预约，增程系统和权益人工确认。",
            "电池、增程器、官方权益和保养记录以检测报告和人工核验为准。",
            "适合家庭六座、长途和新能源体验需求。",
            ["官方权益转人工", "增程系统检测转人工"],
            {"上牌时间": "2021-07", "表显里程": "5.8万公里", "座位数": "6座", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_cs75plus_2021",
            "2021款长安CS75 PLUS 1.5T自动尊贵型",
            "CHEJIN-CS75PLUS-2021",
            "二手车/SUV",
            ["CS75PLUS", "长安CS75", "国产SUV", "家用SUV"],
            "2021年上牌，表显4.4万公里，1.5T自动挡，白色，配置高。",
            6.88,
            2,
            base_shipping,
            "车机、电子配置、底盘和保养以检测报告为准。",
            "适合预算7万左右、空间和配置优先的家庭客户。",
            ["电子配置转人工"],
            {"上牌时间": "2021-05", "表显里程": "4.4万公里", "库存说明": "两台相近车况", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_lacrosse_2018_28t",
            "2018款别克君越28T豪华型",
            "CHEJIN-LACROSSE-2018",
            "二手车/中级轿车",
            ["君越", "别克君越", "舒适商务", "大车"],
            "2018年上牌，表显8.1万公里，2.0T自动挡，黑色。",
            7.88,
            1,
            base_shipping,
            "油耗、保养、底盘和车况以检测报告与实车为准。",
            "适合预算8万左右、想要舒适和商务感的客户。",
            ["油耗承诺禁用"],
            {"上牌时间": "2018-03", "表显里程": "8.1万公里", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_passat_2020_330",
            "2020款大众帕萨特330TSI精英版",
            "CHEJIN-PASSAT-2020",
            "二手车/中级轿车",
            ["帕萨特", "大众帕萨特", "商务", "家用"],
            "2020年上牌，表显5.7万公里，2.0T自动挡，黑色。",
            10.28,
            1,
            base_shipping,
            "碰撞记录、保养和配置以检测报告与实车为准。",
            "适合商务家用兼顾、预算10万左右的客户。",
            ["碰撞记录转人工"],
            {"上牌时间": "2020-12", "表显里程": "5.7万公里", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_jetta_vs5_2021",
            "2021款捷达VS5 280TSI自动荣耀型",
            "CHEJIN-JETTAVS5-2021",
            "二手车/SUV",
            ["捷达VS5", "VS5", "德系SUV", "预算SUV"],
            "2021年上牌，表显3.8万公里，1.4T自动挡，白色。",
            6.38,
            1,
            base_shipping,
            "涡轮、变速箱和保养以检测报告为准。",
            "适合预算6到7万、想买合资SUV的客户。",
            ["变速箱状态转人工"],
            {"上牌时间": "2021-08", "表显里程": "3.8万公里", "演示状态": "在售"},
        ),
        ProductFixture(
            "chejin_archived_focus_2017",
            "2017款福特福克斯1.6L自动风尚型",
            "CHEJIN-FOCUS-2017-SOLD",
            "二手车/紧凑型轿车",
            ["福克斯", "已售案例", "归档示例"],
            "2017年上牌，表显8.6万公里，已售演示样本。",
            3.98,
            0,
            "该车已售，仅保留为历史成交和归档演示。",
            "已售车辆不参与推荐，类似车源可由人工重新匹配。",
            "这台是已售归档样本，客户咨询时应推荐相近库存。",
            ["已售不可推荐"],
            {"上牌时间": "2017-05", "表显里程": "8.6万公里", "演示状态": "已售归档", "display_archive_hint": True},
        ),
    ]


def policy_items(token: str) -> list[dict[str, Any]]:
    rows = [
        ("chejin_company_intro", "江苏车金门店介绍", "company", ["江苏车金", "门店", "地址", "营业时间"], "江苏车金二手车演示账号默认说明：客户可以预约南京门店看车，AI 只做初步接待、资料收集和风险提醒，成交条件由人工确认。", True, False, "global", "", ""),
        ("chejin_appointment_rule", "看车预约与到店规则", "company", ["看车", "到店", "试驾", "试乘", "预约"], "客户提出到店、试驾或试乘时，AI 可以收集姓名、电话、目标车型和时间段，但具体车辆是否在店、钥匙、试驾路线和陪同人员必须转人工确认。", False, True, "global", "", "used_car_visit_appointment"),
        ("chejin_deposit_boundary", "订金和定金表达边界", "contract", ["订金", "定金", "留车", "锁车", "退不退"], "AI 不承诺订金/定金是否可退，不替客户解释合同法律效力；只提示以正式合同、收据和人工确认为准。", False, True, "global", "", "deposit_contract_boundary"),
        ("chejin_transfer_documents", "二手车过户材料提醒", "contract", ["过户", "转让登记", "身份证", "登记证书", "行驶证", "号牌"], "客户咨询过户时，可提醒通常需要买卖双方身份证明、机动车登记证书、行驶证、号牌等材料；具体材料、迁入限制和办理时间以车管所及人工确认为准。", True, False, "global", "", ""),
        ("chejin_invoice_rule", "二手车销售统一发票说明", "invoice", ["发票", "二手车发票", "开票", "税"], "二手车交易通常会涉及二手车销售统一发票，是否可开、开票主体、金额和抬头必须由人工结合交易方式确认。", False, True, "global", "", "invoice_amount_entity"),
        ("chejin_payment_rule", "付款与金融审批边界", "payment", ["首付", "月供", "贷款", "金融", "按揭", "包过"], "AI 可以解释需要人工测算首付和月供，但不得承诺贷款包过、固定利率、固定月供或不查征信。", False, True, "global", "", "finance_approval_boundary"),
        ("chejin_trade_in_rule", "置换资料收集规则", "company", ["置换", "卖车", "旧车", "评估", "收车"], "客户咨询置换时，AI 先收集旧车品牌车型、上牌时间、公里数、车况、城市、是否贷款和期望价；估价和收车价必须人工复核。", True, False, "global", "", ""),
        ("chejin_condition_report_rule", "车况检测报告解释规则", "after_sales", ["检测报告", "第三方检测", "车况", "保养记录"], "AI 可以提示车况以检测报告和合同为准，但不能口头承诺无任何瑕疵；客户要求详细解释检测报告时转人工。", False, True, "global", "", "condition_report_detail"),
        ("chejin_accident_flood_fire_rule", "事故水泡火烧高风险规则", "manual_required", ["事故车", "水泡车", "火烧车", "大事故", "赔偿"], "涉及事故、水泡、火烧、结构件、赔偿承诺等内容必须转人工，AI 不得做最终承诺。", False, True, "global", "", "high_risk_condition_claim"),
        ("chejin_odometer_rule", "调表与里程解释边界", "manual_required", ["调表", "真实里程", "里程准不准", "表显"], "AI 可说明页面展示为表显里程，真实使用情况以检测和维保记录综合判断；客户要求保证未调表时必须转人工。", False, True, "global", "", "odometer_claim"),
        ("chejin_mortgage_seizure_rule", "抵押查封产权风险规则", "contract", ["抵押", "查封", "产权", "绿本", "登记证书"], "客户询问抵押、查封、绿本或产权状态时，必须人工核验车辆登记状态，AI 只做风险提醒。", False, True, "global", "", "title_status_check"),
        ("chejin_emission_relocation_rule", "异地迁入和排放政策规则", "logistics", ["异地", "提档", "迁入", "排放", "国五", "国六"], "异地客户需人工确认迁入城市排放政策、提档资料、临牌、物流方式和费用，AI 不承诺一定能迁入。", False, True, "global", "", "relocation_policy"),
        ("chejin_new_energy_battery_rule", "新能源电池与三电规则", "manual_required", ["电池", "三电", "续航", "充电", "电池健康"], "新能源车辆涉及电池健康、三电质保、续航和官方权益时必须结合检测报告和厂家政策人工确认。", False, True, "product_category", "", "new_energy_policy"),
        ("chejin_test_drive_rule", "试驾试乘安全规则", "manual_required", ["试驾", "试乘", "开一圈", "高速"], "客户提出试驾/试乘，AI 只能记录需求并转人工；是否可试、路线、保险和驾驶资质由门店决定。", False, True, "global", "", "test_drive_safety"),
        ("chejin_after_sale_rule", "售后边界说明", "after_sales", ["售后", "质保", "保修", "退车", "退款"], "二手车售后以合同、检测报告和实际约定为准；退车退款、赔付和保修范围必须转人工确认。", False, True, "global", "", "after_sale_claim"),
        ("chejin_no_private_transfer_rule", "禁止私下转账提醒", "payment", ["私下转账", "个人收款", "定金给你", "微信转账"], "客户提出私下转账、个人收款或绕过合同支付时，AI 必须提醒走公司确认流程并转人工。", False, True, "global", "", "private_payment_risk"),
        ("chejin_personal_info_rule", "客户隐私与资料收集规则", "company", ["身份证", "手机号", "征信", "个人信息"], "AI 只收集完成咨询所需的基本联系方式和购车需求；身份证、征信、银行卡等敏感信息必须由人工通过正式流程处理。", False, True, "global", "", "privacy_sensitive_data"),
        ("chejin_live_lead_rule", "直播线索首轮接待规则", "company", ["抖音", "直播", "刚加", "主播", "车源链接"], "直播线索可以先问预算、用途、城市、想看的车型、是否置换和到店时间，再给出1到3个匹配方向。", True, False, "global", "", ""),
        ("chejin_sleep_customer_rule", "沉睡客户唤醒规则", "company", ["之前问过", "还在吗", "新车源", "没买"], "老客户重新咨询时，AI 先确认预算、用途和车型是否变化，再推荐新车源或转人工补充跟进。", True, False, "global", "", ""),
        ("chejin_price_negotiation_rule", "议价边界规则", "discount", ["最低价", "便宜点", "优惠", "砍价", "一口价"], "AI 可以说明二手车一车一况、价格需结合到店看车和成交方式确认，不承诺最低价或保底优惠。", False, True, "global", "", "price_negotiation"),
        ("chejin_vehicle_hold_rule", "留车时效规则", "contract", ["帮我留着", "锁车", "别卖", "保留"], "留车、锁车、订金时效和违约规则必须由人工确认，AI 只能记录客户意向。", False, True, "global", "", "vehicle_hold"),
        ("chejin_complaint_rule", "投诉与纠纷升级规则", "manual_required", ["投诉", "举报", "欺诈", "纠纷", "315"], "客户出现投诉、纠纷、举报、欺诈等表达时，AI 必须安抚、记录并立即转人工，不做责任判断。", False, True, "global", "", "complaint_escalation"),
        ("chejin_operating_vehicle_rule", "营运与非营运属性规则", "contract", ["营运", "非营运", "网约车", "出租", "客运"], "车辆营运属性、网约车使用、报废年限和保险差异必须人工核验，AI 不做最终结论。", False, True, "global", "", "operation_attribute"),
        ("chejin_maintenance_record_rule", "保养记录解释规则", "after_sales", ["保养记录", "4S店", "维保", "出险记录"], "AI 可提示是否有保养/出险记录需要以查询结果为准，客户要求明细时转人工发送报告。", False, True, "global", "", "maintenance_record_detail"),
        ("chejin_commercial_mpv_rule", "MPV商务客户跟进规则", "company", ["商务接待", "公司买车", "七座", "MPV", "GL8"], "MPV商务客户通常关注内饰、座椅、发票、付款和试乘；AI 先记录需求，再安排人工跟进。", True, False, "product_category", "", ""),
        ("chejin_archived_stock_rule", "已售库存处理规则", "company", ["已售", "卖掉", "没有了", "库存0"], "客户问到已售车辆时，AI 不再推荐该车，应说明已售并收集预算、用途，推荐相近车源或转人工找车。", True, False, "global", "", ""),
        ("chejin_generic_manual_transfer", "通用人工转接说明", "manual_required", ["人工客服", "转人工", "销售联系", "真人"], "当客户明确要求人工客服时，应说明已经为客户记录并转接人工，请客户稍等片刻。", True, False, "global", "", ""),
    ]
    items = []
    for item_id, title, policy_type, keywords, answer, allow, handoff, scope, product_category, reason in rows:
        data = {
            "title": title,
            "policy_type": policy_type,
            "keywords": unique_list([*keywords, token]),
            "applicability_scope": scope,
            "product_category": product_category or ("二手车/新能源" if scope == "product_category" and "新能源" in title else ""),
            "answer": answer,
            "allow_auto_reply": allow,
            "requires_handoff": handoff,
            "handoff_reason": reason,
            "operator_alert": bool(handoff),
            "risk_level": "high" if handoff else "normal",
            "additional_details": {"演示批次": token, "面向客户解释": "前台可直接按自然语言理解，不需要客户理解技术字段。"},
        }
        items.append(base_item("policies", item_id, token, data, allow=allow, handoff=handoff, risk="high" if handoff else "normal"))
    return items


def chat_items(token: str) -> list[dict[str, Any]]:
    rows = [
        ("chejin_chat_budget_8w_commute", "8万左右想买省油自动挡代步", "8万左右可以优先看凯美瑞、轩逸、卡罗拉或秦PLUS。燃油车更省心，插混更省油；我先帮您确认城市、到店时间、是否贷款和置换。", ["预算推荐", "代步"], "", ""),
        ("chejin_chat_new_energy_compare", "秦PLUS和Model 3怎么选", "秦PLUS偏低油耗和家用成本，Model 3偏纯电驾驶和智能化。新能源车我会先提醒电池检测、官方权益和当地政策需要人工确认。", ["车型对比", "新能源"], "", "二手车/新能源轿车"),
        ("chejin_chat_trade_in_collect", "我有一台老朗逸想置换", "可以置换。麻烦补充旧车品牌车型、上牌时间、公里数、车况、所在城市、是否还有贷款和期望价，估价由人工复核。", ["置换", "线索采集"], "", ""),
        ("chejin_chat_appointment", "今天下午能去看车吗", "可以先帮您记录意向。麻烦发一下姓名、电话、想看的车和大概到店时间，我转人工确认车辆是否在店和接待安排。", ["预约", "转人工"], "", ""),
        ("chejin_chat_finance", "首付三成月供多少，能不能包过", "首付和月供需要根据车型、征信和金融方案人工测算；我不能承诺贷款包过，可以先帮您记录预算和车型。", ["金融", "边界"], "", ""),
        ("chejin_chat_accident_claim", "你保证无事故吗，不对就赔我", "事故、水泡、火烧和赔付承诺必须以检测报告、合同和人工确认为准。我先帮您转人工详细说明。", ["事故风险", "转人工"], "", ""),
        ("chejin_chat_invoice", "公司买车能开发票吗", "二手车发票、开票主体和金额需要结合交易方式人工确认。您是公司户还是个人户？我先记录后转人工。", ["发票", "公司购车"], "", ""),
        ("chejin_chat_relocation", "我是外地的，能迁到苏州吗", "异地迁入要看当地排放、车管所政策和车辆档案状态，我可以记录城市和车型，具体能否迁入由人工确认。", ["异地", "过户"], "", ""),
        ("chejin_chat_battery", "特斯拉电池还剩多少健康度", "电池健康度、官方权益和账号状态需要检测报告和人工核验，AI 不做最终承诺。我可以先帮您预约检测说明。", ["电池", "新能源"], "chejin_model3_2021_std", ""),
        ("chejin_chat_gl8_business", "GL8适合公司接待吗", "GL8适合商务接待和七座刚需，建议重点看内饰磨损、座椅、保养记录和试乘体验，我可以转人工预约看车。", ["MPV", "商务"], "chejin_gl8_2020_es653t", ""),
        ("chejin_chat_archived", "那台福克斯还在吗", "这台福克斯是已售归档样本，不再推荐。您可以告诉我预算和用途，我帮您找相近库存。", ["已售", "替代推荐"], "chejin_archived_focus_2017", ""),
        ("chejin_chat_low_budget", "3万以内有车吗", "3万以内可以看看微型电动车或年份较早的代步车，但车况差异会比较大，我建议先确认是否接受新能源和手动挡。", ["低预算", "筛选"], "", ""),
        ("chejin_chat_high_end", "Macan能不能今天试驾，最低多少钱", "高端车试驾、订金、最低价和付款方式必须人工确认。我先记录您的到店时间和预算，再请销售联系您。", ["高端车", "议价"], "chejin_macan_2018_20t", ""),
        ("chejin_chat_family_suv", "家里两个孩子，想要SUV空间大一点", "可以优先看CR-V、宋PLUS、CS75 PLUS或汉兰达。您预算大概多少，是否需要七座或新能源？", ["家用SUV", "需求澄清"], "", "二手车/SUV"),
        ("chejin_chat_noise_decline", "今天天气怎么样顺便讲个笑话", "我主要负责二手车咨询。如果您想看车，可以告诉我预算、用途、车型和城市，我再帮您推荐。", ["越界问题"], "", ""),
        ("chejin_chat_complaint", "你们是不是骗人，我要投诉", "很抱歉让您有这样的感受。我先记录您的问题，并立即转人工处理，请您先不要重复提交敏感资料。", ["投诉", "转人工"], "", ""),
        ("chejin_chat_operation_vehicle", "这车以前跑过网约车吗", "营运属性、出险和使用性质必须人工核验登记与报告。我先记录车型，销售会结合资料回复。", ["营运属性"], "", ""),
        ("chejin_chat_hold", "我先给你转500帮我留车", "留车和订金需要走公司确认流程，我不能收款或承诺保留。我先帮您转人工确认正式流程。", ["订金", "支付风险"], "", ""),
        ("chejin_chat_after_sale", "买回去发现有问题能退吗", "售后、退车和赔付要以合同、检测报告和实际约定为准，这类问题我会转人工说明。", ["售后", "合同"], "", ""),
        ("chejin_chat_file_transfer_doc", "产品资料：2023款比亚迪宋PLUS DM-i，家庭SUV，重点说明电池和迁入政策", "这类资料会先进入 AI 经验池，再由人工确认是否升级为正式知识。", ["资料导入", "记录员"], "chejin_songplus_2023_dmi", ""),
        ("chejin_chat_sleep_wakeup", "之前那台没买，现在还有新车源吗", "有的。我先按您之前的预算和用途筛一遍；如果预算或车型变了，直接告诉我，我会重新匹配。", ["沉睡客户", "唤醒"], "", ""),
        ("chejin_chat_customer_profile", "我南京上班，预算10万，家用偶尔商务", "10万左右可看雅阁、帕萨特、天籁或A4L低年份车源。您更看重品牌、空间还是油耗？", ["客户画像", "推荐"], "", ""),
        ("chejin_chat_stock_zero", "这台库存是不是还有2台", "库存会随成交变化，需要以系统和人工确认为准。我可以先帮您查当前可看车源。", ["库存确认"], "", ""),
        ("chejin_chat_contract_fee", "过户费保险费都包不包", "过户、保险、服务费和物流费需要按成交方式列明，AI 不做包干承诺，建议转人工确认明细。", ["费用明细", "合同"], "", ""),
        ("chejin_chat_old_car_risk", "年份老的车能买吗", "年份较老的车建议重点看车况、保养、易损件和排放迁入，预算低可以筛选但不要只看价格。", ["购买建议"], "", ""),
        ("chejin_chat_warranty", "你们保修多久", "保修范围和时长要看车辆、合同和是否购买延保，AI 不做统一承诺，可以帮您转人工确认。", ["质保"], "", ""),
        ("chejin_chat_report_request", "能把检测报告发我吗", "可以先记录车型和联系方式，检测报告明细需要销售核实后发送，涉及解释项也建议人工说明。", ["检测报告"], "", ""),
        ("chejin_chat_compare_camry_accord", "凯美瑞和雅阁哪个更适合家用", "两台都适合家用。凯美瑞偏省心稳定，雅阁空间和动力感更明显；最终建议结合预算、车况报告和试乘体验。", ["车型对比"], "", "二手车/中级轿车"),
        ("chejin_chat_plate", "车牌能一起过户吗", "通常车辆交易和号牌政策需要按当地规定处理，号牌是否能保留或随车不能由 AI 承诺，需要人工确认。", ["车牌", "过户"], "", ""),
        ("chejin_chat_private_payment", "我直接微信转你个人可以吗", "不建议私下转账。订金、车款和合同都应走公司确认流程，我会转人工说明正式支付方式。", ["支付风险"], "", ""),
        ("chejin_chat_duplicate_lead", "我刚才发过电话了怎么还问", "抱歉，我会先查看已记录信息，避免重复打扰；如果信息不完整，只补缺少的字段即可。", ["重复线索", "安抚"], "", ""),
        ("chejin_chat_out_of_hours", "晚上11点还能看车吗", "我先记录您的需求。门店接待时间和是否能夜间看车需要人工确认，销售上线后会联系您。", ["非营业时间"], "", ""),
        ("chejin_chat_photo_video", "能拍个底盘视频吗", "可以先记录您想看的细节，底盘视频、漆面细节和内饰磨损需要人工到车边拍摄确认。", ["视频资料"], "", ""),
        ("chejin_chat_universal_handoff", "直接给我人工客服", "好的，我已记录您的需求并转接人工客服，请您稍等片刻。", ["人工转接"], "", ""),
    ]
    items = []
    for item_id, question, reply, tags, product_id, product_category in rows:
        data = {
            "customer_message": question,
            "service_reply": reply,
            "intent_tags": unique_list([*tags, token]),
            "tone_tags": ["专业", "克制", "不乱承诺"],
            "linked_categories": ["products", "policies"],
            "linked_item_ids": [product_id] if product_id else [],
            "applicability_scope": "specific_product" if product_id else ("product_category" if product_category else "global"),
            "product_id": product_id,
            "product_category": product_category,
            "usable_as_template": True,
            "additional_details": {"演示批次": token, "客户可读摘要": "真实客服可直接参考的问答样例。"},
        }
        handoff = any(word in reply for word in ("转人工", "人工确认", "人工核验"))
        items.append(base_item("chats", item_id, token, data, allow=not handoff or "可以先" in reply, handoff=handoff, risk="warning" if handoff else "normal"))
    return items


def product_scoped_items(token: str) -> list[dict[str, Any]]:
    products = product_fixtures(token)
    key_products = products[:18]
    items: list[dict[str, Any]] = []
    for product in key_products:
        product_id = product.item_id
        short_name = product.name.split("款", 1)[-1] if "款" in product.name else product.name
        faq = {
            "product_id": product_id,
            "title": f"{short_name}适合什么客户",
            "keywords": unique_list([product.name, *product.aliases[:3], "适合", token]),
            "question": f"{product.name}适合什么客户？",
            "answer": product.recommendation,
            "additional_details": {"关联商品": product.name, "演示批次": token},
        }
        items.append(base_item("product_faq", f"{product_id}_fit_faq", token, faq, allow=True, handoff=False))
        rule = {
            "product_id": product_id,
            "title": f"{short_name}风险边界",
            "keywords": unique_list([product.name, *product.risk_rules, "保证", "赔付", token]),
            "answer": f"{product.name} 的风险点包括：{'；'.join(product.risk_rules)}。客户要求最终承诺时必须人工确认。",
            "allow_auto_reply": False,
            "requires_handoff": True,
            "handoff_reason": "product_specific_risk_boundary",
            "additional_details": {"关联商品": product.name, "演示批次": token},
        }
        items.append(base_item("product_rules", f"{product_id}_risk_rule", token, rule, allow=False, handoff=True, risk="high"))
        explanation = {
            "product_id": product_id,
            "title": f"{short_name}讲解重点",
            "keywords": unique_list([product.name, "讲解", "卖点", "看车重点", token]),
            "content": f"讲解 {product.name} 时，先讲适合人群，再讲价格、里程和车况；客户追问合同、检测、金融、异地迁入时转人工。车况摘要：{product.specs}",
            "additional_details": {"关联商品": product.name, "演示批次": token},
        }
        items.append(base_item("product_explanations", f"{product_id}_explanation", token, explanation, allow=True, handoff=False))
    return items


def base_item(
    category_id: str,
    item_id: str,
    token: str,
    data: dict[str, Any],
    *,
    allow: bool = True,
    handoff: bool = False,
    risk: str = "normal",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "category_id": category_id,
        "id": item_id,
        "status": "active",
        "source": {"type": "demo_fixture", "batch_token": token, "domain": "used_car"},
        "data": data,
        "runtime": {
            "allow_auto_reply": allow,
            "requires_handoff": handoff,
            "risk_level": risk,
            "operator_alert": bool(handoff),
        },
    }


def save_item(store: KnowledgeBaseStore, category_id: str, item: dict[str, Any], saved: list[dict[str, Any]]) -> None:
    item = mark_item_new(
        item,
        {
            "source_module": "jiangsu_chejin_demo_seed",
            "target_category": category_id,
            "item_id": item["id"],
        },
    )
    result = store.save_item(category_id, item)
    if not result.get("ok"):
        raise AssertionError(f"save failed for {category_id}/{item.get('id')}: {result}")
    saved.append({"category": category_id, "id": str(item.get("id") or "")})


def write_and_ingest_demo_materials(token: str) -> dict[str, Any]:
    root = BASE_ARTIFACT_ROOT / "demo_materials" / token
    root.mkdir(parents=True, exist_ok=True)
    material_specs = [
        ("车辆库存总表", "products", "product_doc", render_vehicle_material(token)),
        ("二手车交易规则手册", "policies", "policy_doc", render_policy_material(token)),
        ("销售对话话术库", "chats", "chat_log", render_chat_material(token)),
        ("风险边界与边界测试矩阵", "manual", "manual", render_boundary_material(token)),
    ]
    rag = RagService()
    experiences = RagExperienceStore()
    ingested = []
    for title, category, source_type, text in material_specs:
        path = root / f"{category}_{token}.md"
        path.write_text(f"# {title}\n\n{text}\n", encoding="utf-8")
        ingest = rag.ingest_file(path, source_type=source_type, category=category, rebuild_index=True)
        assert_true(ingest.get("ok"), f"RAG ingest failed: {path} {ingest}")
        exp = experiences.record_intake(
            source_type=source_type,
            source_path=str(path),
            category=category,
            evidence_excerpt=text[:1400],
            rag_ingest=ingest,
            candidate_ids=[],
            original_source={"type": "demo_material", "title": title, "batch_token": token},
        )
        ingested.append(
            {
                "title": title,
                "path": str(path),
                "source_id": ingest.get("source_id"),
                "chunk_count": ingest.get("chunk_count"),
                "rag_experience_id": exp.get("experience_id"),
            }
        )
    return {"ok": True, "material_count": len(ingested), "items": ingested}


def seed_raw_wechat_demo_messages(token: str, *, use_llm: bool) -> dict[str, Any]:
    store = RawMessageStore(tenant_id=TENANT_ID)
    base_time = datetime.now() - timedelta(minutes=90)
    conversations = [
        (
            {
                "conversation_id": f"chejin_demo_group_{token.lower()}",
                "target_name": "偷数据测试",
                "display_name": "偷数据测试",
                "conversation_type": "group",
                "selected_by_user": True,
                "learning_enabled": True,
                "notify_enabled": False,
                "source": {"type": "demo_seed", "batch_token": token},
            },
            [
                "群资料：新增2024款理想L6 Pro，预算23万内，适合家庭，增程SUV，库存1台，客户问权益时转人工。",
                "群提醒：二手车过户通常要核身份证明、机动车登记证书、行驶证和号牌，异地迁入先查当地政策。",
                "客户王总问GL8商务接待，重点看内饰磨损、二排座椅、保养记录和试乘。",
                "新能源客户追问电池健康度、三电质保、续航衰减，一律让销售结合检测报告回复。",
                f"边界噪音 {token}：今晚大家记得下班关灯，这条不应该成为正式知识。",
                "售后话术：客户问退车赔偿，回复以合同、检测报告和人工说明为准，不做口头承诺。",
            ],
        ),
        (
            {
                "conversation_id": f"chejin_demo_file_transfer_{token.lower()}",
                "target_name": "文件传输助手",
                "display_name": "文件传输助手",
                "conversation_type": "file_transfer",
                "selected_by_user": True,
                "learning_enabled": True,
                "notify_enabled": False,
                "source": {"type": "demo_seed", "batch_token": token},
            },
            [
                "手动粘贴：2022款大众途观L 330TSI两驱舒享版，表显4.3万公里，价格12.38万，SUV，客户问异地提档要人工确认。",
                "手动粘贴：客户问最低价时，AI只能说明二手车一车一况，最终优惠由销售结合到店和付款方式确认。",
                "手动粘贴：置换客户必须收集旧车品牌车型、上牌时间、公里数、是否贷款、城市、期望价。",
                "手动粘贴：发票和过户费用不能统一承诺包干，开票主体、金额、费用明细都要人工核对。",
                "手动粘贴：客户要求人工客服，统一回复已经转接人工，请稍等片刻。",
            ],
        ),
        (
            {
                "conversation_id": f"chejin_demo_private_zhang_{token.lower()}",
                "target_name": "抖音线索-张先生",
                "display_name": "抖音线索-张先生",
                "conversation_type": "private",
                "selected_by_user": True,
                "learning_enabled": True,
                "notify_enabled": False,
                "source": {"type": "demo_seed", "batch_token": token},
            },
            [
                "张先生：我从直播间来的，预算8万，想买省油自动挡，最好南京能看车。",
                "客服：8万左右可以先看凯美瑞、轩逸、卡罗拉或秦PLUS，您是想燃油还是新能源？",
                "张先生：可以贷款吗，首付三成月供多少？",
                "客服：首付月供需要金融专员测算，我先记录预算、车型和征信大致情况，不能承诺包过。",
                "张先生：我明天下午能来看凯美瑞吗？",
                "客服：可以，我先记录到店意向并转人工确认车辆是否在店和接待时间。",
            ],
        ),
        (
            {
                "conversation_id": f"chejin_demo_private_li_{token.lower()}",
                "target_name": "置换线索-李女士",
                "display_name": "置换线索-李女士",
                "conversation_type": "private",
                "selected_by_user": True,
                "learning_enabled": True,
                "notify_enabled": False,
                "source": {"type": "demo_seed", "batch_token": token},
            },
            [
                "李女士：我有一台2018年朗逸想置换宋PLUS，车在苏州，6.5万公里。",
                "客服：可以，麻烦补充是否有贷款、事故水泡火烧情况、期望价和车牌所在地，估价需要人工复核。",
                "李女士：新能源车电池是不是保八年？",
                "客服：三电质保要结合厂家政策、车辆权益和检测报告，具体由人工核验。",
                "李女士：如果迁不回苏州怎么办？",
                "客服：异地迁入要先查当地政策和车辆档案，我帮您转人工确认。",
            ],
        ),
    ]
    upserts = []
    for index, (conversation, texts) in enumerate(conversations):
        messages = [
            {
                "id": f"{conversation['conversation_id']}_{i}",
                "sender": "客户" if "客服：" not in text else "self",
                "sender_role": "contact" if "客服：" not in text else "self",
                "content": f"{text}\n演示批次：{token}",
                "message_time": (base_time + timedelta(minutes=index * 10 + i)).isoformat(timespec="seconds"),
                "type": "text",
                "dedupe_key": f"{token}:{conversation['conversation_id']}:{i}",
            }
            for i, text in enumerate(texts)
        ]
        upserts.append(
            store.upsert_messages(
                conversation,
                messages,
                source_module="ai_recorder_demo_seed",
                learning_enabled=True,
                create_batch=True,
                batch_reason="demo_used_car_material",
            )
        )
    learning = RawMessageLearningService(tenant_id=TENANT_ID).process_pending(limit=20, use_llm=use_llm)
    return {
        "ok": True,
        "use_llm": use_llm,
        "conversation_count": len(conversations),
        "inserted_count": sum(int(item.get("inserted_count") or 0) for item in upserts),
        "duplicate_count": sum(int(item.get("duplicate_count") or 0) for item in upserts),
        "batch_ids": [((item.get("batch") or {}).get("batch_id")) for item in upserts if item.get("batch")],
        "learning": learning,
    }


def check_shared_candidate_filter(*, use_llm: bool, limit: int) -> dict[str, Any]:
    class FakeVps:
        configured = True

        def __init__(self) -> None:
            self.posts: list[dict[str, Any]] = []

        def post_json(self, path: str, payload: dict[str, Any], *, token: str = "", headers: dict[str, str] | None = None) -> dict[str, Any]:
            self.posts.append({"path": path, "payload": payload, "token": token, "headers": headers})
            return {"ok": True, "proposal": {"proposal_id": payload.get("proposal_id"), "status": "pending_review"}}

    service = VpsLocalSyncService(vps_base_url="")
    fake = FakeVps()
    service.vps = fake  # type: ignore[assignment]
    result = service.upload_formal_knowledge_candidates(
        tenant_id=TENANT_ID,
        use_llm=use_llm,
        limit=limit,
        only_unscanned=False,
        token="demo-local-token",
    )
    private_terms = ["张先生", "李女士", "139", "江苏车金南京门店客户", "手机号"]
    payload_texts = [json.dumps(post["payload"], ensure_ascii=False) for post in fake.posts]
    assert_true(all(not any(term in text for term in private_terms) for text in payload_texts), "shared candidates must not leak private customer details")
    return {
        "ok": True,
        "use_llm": use_llm,
        "checked_count": result.get("checked_count"),
        "uploaded_count": len(result.get("uploaded", []) or []),
        "skipped_count": len(result.get("skipped", []) or []),
        "payload_count": len(fake.posts),
        "payload_titles": [str(post["payload"].get("title") or "") for post in fake.posts[:10]],
    }


def verify_demo_dataset(token: str) -> dict[str, Any]:
    runtime = KnowledgeRuntime(tenant_id=TENANT_ID)
    counts = {
        "products": len(runtime.list_items("products")),
        "policies": len(runtime.list_items("policies")),
        "chats": len(runtime.list_items("chats")),
        "product_faq": len(runtime.list_items("product_faq")),
        "product_rules": len(runtime.list_items("product_rules")),
        "product_explanations": len(runtime.list_items("product_explanations")),
        "rag_sources": len(RagService(tenant_id=TENANT_ID).list_sources()),
        "rag_experiences": len(RagExperienceStore(tenant_id=TENANT_ID).list(status="all", limit=500)),
        "raw_messages": RawMessageStore(tenant_id=TENANT_ID).summary().get("message_count", 0),
        "pending_candidates": count_candidate_files("pending"),
    }
    assert_true(counts["products"] >= 24, f"expected rich product catalog, got {counts['products']}")
    assert_true(counts["policies"] >= 24, f"expected rich policy library, got {counts['policies']}")
    assert_true(counts["chats"] >= 30, f"expected rich chat library, got {counts['chats']}")
    assert_true(counts["product_faq"] >= 18 and counts["product_rules"] >= 18 and counts["product_explanations"] >= 18, f"expected product scoped knowledge, got {counts}")
    assert_true(counts["rag_sources"] >= 4, f"expected RAG sources, got {counts['rag_sources']}")
    assert_true(counts["raw_messages"] >= 20, f"expected raw recorder messages, got {counts['raw_messages']}")
    index = KnowledgeIndex(KnowledgeRuntime(tenant_id=TENANT_ID))
    search_cases = [
        ("8万预算自动挡省油", "products"),
        ("秦PLUS 电池 三电", "product_rules"),
        ("人工客服 转人工", "policies"),
        ("GL8 商务接待 七座", "products"),
        ("异地 过户 登记证书 行驶证", "policies"),
        ("福克斯 已售", "products"),
    ]
    hits = []
    for query, expected_category in search_cases:
        result_hits = index.search(query, limit=8)
        hits.append(
            {
                "query": query,
                "hit_count": len(result_hits),
                "top": [
                    {"category": hit.category_id, "id": hit.item_id, "title": hit.title, "confidence": round(hit.confidence, 3)}
                    for hit in result_hits[:3]
                ],
            }
        )
        assert_true(any(hit.category_id == expected_category for hit in result_hits), f"search should hit {expected_category}: {query}")
    client, headers = login_client()
    backend = check_tenant_scoped_backend(client, headers, token)
    customer_service = check_customer_service_matrix(token)
    recorder = check_recorder_offline_matrix(token)
    return {
        "ok": True,
        "counts": counts,
        "search_hits": hits,
        "backend": backend,
        "customer_service": compact_result(customer_service),
        "recorder": compact_result(recorder),
    }


def render_vehicle_material(token: str) -> str:
    lines = [f"演示批次：{token}", "用途：客户演示用二手车库存，覆盖轿车、SUV、MPV、新能源、高端车、已售归档等场景。"]
    for product in product_fixtures(token):
        lines.append(
            "\n".join(
                [
                    f"商品名称：{product.name}",
                    f"型号/SKU：{product.sku}",
                    f"类目：{product.category}",
                    f"关键词：{'、'.join(product.aliases)}",
                    f"规格：{product.specs}",
                    f"价格：{product.price:.2f}万，库存：{product.inventory}台",
                    f"物流/过户：{product.shipping_policy}",
                    f"售后/风险：{product.warranty_policy}",
                    f"推荐话术：{product.recommendation}",
                ]
            )
        )
    return "\n\n".join(lines)


def render_policy_material(token: str) -> str:
    lines = [f"演示批次：{token}", "二手车行业演示规则：所有成交、过户、金融、售后和车况承诺均以合同、检测报告和人工确认为准。"]
    for item in policy_items(token):
        data = item["data"]
        lines.append(f"规则：{data['title']}\n触发词：{'、'.join(data.get('keywords', [])[:8])}\n标准说明：{data['answer']}")
    return "\n\n".join(lines)


def render_chat_material(token: str) -> str:
    lines = [f"演示批次：{token}", "真实客服话术样例："]
    for item in chat_items(token):
        data = item["data"]
        lines.append(f"客户：{data['customer_message']}\n客服：{data['service_reply']}\n标签：{'、'.join(data.get('intent_tags', [])[:6])}")
    return "\n\n".join(lines)


def render_boundary_material(token: str) -> str:
    return f"""
演示批次：{token}
边界矩阵：
1. 客户只问车型推荐：可自动回复，需给出1到3个选择并追问预算、用途、城市。
2. 客户问试驾、订金、最低价、金融包过：必须转人工。
3. 客户问事故、水泡、火烧、调表、赔偿：必须转人工，不做保证。
4. 客户问过户、异地迁入、发票、保险、费用：先解释大原则，再转人工确认明细。
5. 客户发来商品资料或群聊内部规则：先入原始消息库，再形成AI经验池，再进入候选知识，最后人工确认。
6. 客户发噪音、闲聊、和二手车无关的问题：可礼貌收束，不应升级为正式知识。
7. 客户要求人工客服：直接记录并转人工，这类通用话术可被提炼为共享公共知识候选。
""".strip()


def count_candidate_files(status: str) -> int:
    root = tenant_review_candidates_root(TENANT_ID) / status
    return len(list(root.glob("*.json"))) if root.exists() else 0


def compact_result(result: dict[str, Any]) -> dict[str, Any]:
    return {"name": result.get("name"), "ok": result.get("ok"), "case_count": len(result.get("cases", []) or []), **{k: v for k, v in result.items() if k in {"raw_message_ids", "rag_checks"}}}


def count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return counts


def unique_list(values: list[Any]) -> list[Any]:
    result = []
    seen = set()
    for value in values:
        if value in (None, "", [], {}):
            continue
        key = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
