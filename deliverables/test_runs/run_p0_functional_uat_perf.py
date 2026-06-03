#!/usr/bin/env python3
"""P0 functional, UAT-style, and lightweight performance checks for the leads API."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import StringIO
from urllib import error, parse, request


HEADERS = {
    "Content-Type": "application/json",
    "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
    "X-Operator-Name": "Ops Tester",
    "X-Operator-Role": "admin",
}


class ApiClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None, query: dict | None = None):
        url = self.base_url + path
        if query:
            url += "?" + parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(url, data=data, headers=HEADERS, method=method)
        started = time.perf_counter()
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                elapsed_ms = (time.perf_counter() - started) * 1000
                content_type = resp.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    payload = json.loads(raw.decode("utf-8"))
                else:
                    payload = raw.decode("utf-8-sig")
                return {"status": resp.status, "elapsed_ms": elapsed_ms, "payload": payload, "headers": dict(resp.headers)}
        except error.HTTPError as exc:
            raw = exc.read()
            elapsed_ms = (time.perf_counter() - started) * 1000
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                payload = raw.decode("utf-8", errors="replace")
            return {"status": exc.code, "elapsed_ms": elapsed_ms, "payload": payload, "headers": dict(exc.headers)}


def sales_payload(sales, **overrides):
    payload = {
        "sales_name": sales["sales_name"],
        "enabled": sales["enabled"],
        "sort_order": sales.get("sort_order"),
        "remark": sales.get("remark"),
    }
    if sales.get("wechat"):
        payload["wechat"] = sales["wechat"]
    if sales.get("feishu_user_id"):
        payload["feishu_user_id"] = sales["feishu_user_id"]
    payload.update(overrides)
    return payload


def ok_json(resp, code="OK"):
    return resp["status"] == 200 and isinstance(resp["payload"], dict) and resp["payload"].get("code") == code


def record(results, case_id, name, passed, actual, expected, priority="P0", elapsed_ms=None):
    results.append(
        {
            "case_id": case_id,
            "name": name,
            "passed": bool(passed),
            "actual": actual,
            "expected": expected,
            "priority": priority,
            "elapsed_ms": round(elapsed_ms, 2) if elapsed_ms is not None else None,
        }
    )


def data_of(resp):
    payload = resp["payload"]
    return payload.get("data") if isinstance(payload, dict) else None


def restore_sales(client, original_sales):
    for sales in original_sales:
        client.request("PUT", f"/sales/{sales['id']}", sales_payload(sales))


def run_functional_and_uat(client: ApiClient):
    results = []
    stamp = str(int(time.time() * 1000))[-8:]
    base_phone = "139" + stamp

    health_base = client.base_url[:-4] if client.base_url.endswith("/api") else client.base_url
    health = ApiClient(health_base).request("GET", "/healthz")
    record(results, "ENV-001", "健康检查", health["status"] == 200, health["payload"], "返回 status=ok", elapsed_ms=health["elapsed_ms"])

    sales_resp = client.request("GET", "/sales")
    sales = data_of(sales_resp).get("items", []) if ok_json(sales_resp) else []
    record(results, "TC-014", "销售管理加载", len(sales) >= 1, {"count": len(sales)}, "至少返回 1 个销售", elapsed_ms=sales_resp["elapsed_ms"])

    lead_list_before = client.request("GET", "/leads", query={"page": 1, "page_size": 10})
    before_total = data_of(lead_list_before).get("total", 0) if ok_json(lead_list_before) else -1
    stats_resp = client.request("GET", "/leads/stats")
    stats_data = data_of(stats_resp) if ok_json(stats_resp) else {}
    expected_rate = (
        round(stats_data.get("today_assigned_count", 0) / stats_data.get("today_new_count", 0) * 100, 1)
        if stats_data.get("today_new_count", 0)
        else None
    )
    record(
        results,
        "TC-002",
        "进入线索管理并加载真实列表",
        ok_json(lead_list_before)
        and ok_json(stats_resp)
        and {"today_assigned_count", "today_unassigned_count", "assignment_success_rate"}.issubset(stats_data.keys())
        and stats_data.get("assignment_success_rate") == expected_rate,
        {"list_total": before_total, "stats": stats_data, "expected_rate": expected_rate},
        "列表和指标接口均成功返回，今日轮询成功率=今日已分配/今日新增",
        elapsed_ms=max(lead_list_before["elapsed_ms"], stats_resp["elapsed_ms"]),
    )

    lead_payload = {
        "customer_name": "压测前功能客户" + stamp,
        "phones": [base_phone],
        "wechats": ["wx_" + stamp],
        "remark": "P0 功能测试新增，预算 10 万，周末到店",
        "custom_fields": {"budget": "10 万以内", "car_type": "SUV"},
    }
    created = client.request("POST", "/leads", lead_payload)
    created_data = data_of(created) if ok_json(created) else {}
    lead_id = created_data.get("id")
    record(
        results,
        "TC-003",
        "人工新增客户线索",
        ok_json(created) and created_data.get("created") is True and created_data.get("status") in {"assigned", "unassigned"},
        created_data,
        "创建成功，状态为 assigned 或 unassigned",
        elapsed_ms=created["elapsed_ms"],
    )

    preview = client.request("POST", "/leads/duplicate-preview", {"phones": [base_phone]})
    preview_items = data_of(preview).get("items", []) if ok_json(preview) else []
    record(
        results,
        "TC-004",
        "手机号重复预查",
        bool(preview_items and preview_items[0].get("has_active_duplicate")),
        preview_items,
        "已存在手机号返回 active duplicate",
        elapsed_ms=preview["elapsed_ms"],
    )

    duplicate = client.request("POST", "/leads", {**lead_payload, "customer_name": "重复客户" + stamp, "remark": "重复提交备注"})
    record(
        results,
        "TC-005",
        "保存时重复手机号查重",
        duplicate["status"] == 409 and isinstance(duplicate["payload"], dict) and duplicate["payload"].get("code") == "LEAD_PHONE_DUPLICATED",
        duplicate["payload"],
        "返回 409/LEAD_PHONE_DUPLICATED 且不创建新线索",
        elapsed_ms=duplicate["elapsed_ms"],
    )

    detail = client.request("GET", f"/leads/{lead_id}") if lead_id else {"status": 0, "payload": {}, "elapsed_ms": 0}
    detail_data = data_of(detail) if ok_json(detail) else {}
    has_assignment_node = any(node.get("key") == "round_robin_assigned" for node in detail_data.get("task_nodes", []))
    record(
        results,
        "TC-006",
        "自动轮询分配",
        detail_data.get("assign_status") == "assigned" and bool(detail_data.get("sales_name")) and has_assignment_node,
        {"assign_status": detail_data.get("assign_status"), "sales_name": detail_data.get("sales_name"), "task_nodes": detail_data.get("task_nodes")},
        "有可用销售时自动分配并展示轮询分配完成",
        elapsed_ms=detail["elapsed_ms"],
    )

    assignments = client.request("GET", f"/leads/{lead_id}/assignments") if lead_id else {"status": 0, "payload": {}, "elapsed_ms": 0}
    assignment_items = data_of(assignments).get("items", []) if ok_json(assignments) else []
    record(
        results,
        "TC-006A",
        "分配记录查询",
        bool(assignment_items),
        assignment_items[:2],
        "返回至少 1 条分配记录",
        elapsed_ms=assignments["elapsed_ms"],
    )

    invalid = client.request("POST", f"/leads/{lead_id}/mark-invalid", {"invalid_reason": "test_data", "invalid_remark": "P0 测试标记无效"})
    invalid_data = data_of(invalid) if ok_json(invalid) else {}
    record(
        results,
        "TC-009",
        "标记无效",
        invalid_data.get("status") == "invalid",
        invalid_data,
        "线索状态变为 invalid",
        elapsed_ms=invalid["elapsed_ms"],
    )

    restored = client.request("POST", f"/leads/{lead_id}/restore")
    restored_data = data_of(restored) if ok_json(restored) else {}
    record(
        results,
        "TC-010",
        "恢复有效",
        restored_data.get("status") in {"assigned", "unassigned"},
        restored_data,
        "恢复为 assigned 或 unassigned",
        elapsed_ms=restored["elapsed_ms"],
    )

    second = client.request("POST", "/leads", {"customer_name": "批量无效客户" + stamp, "phones": [str(int(base_phone) + 1)], "remark": "批量无效验证"})
    second_id = data_of(second).get("id") if ok_json(second) else None
    batch = client.request("POST", "/leads/batch-mark-invalid", {"lead_ids": [lead_id, second_id], "invalid_reason": "test_data", "invalid_remark": "批量无效测试"})
    batch_data = data_of(batch) if ok_json(batch) else {}
    record(
        results,
        "TC-011",
        "批量选择与批量标记无效",
        ok_json(batch) and batch_data.get("succeeded", 0) >= 2,
        batch_data,
        "两条线索均成功标记无效",
        elapsed_ms=batch["elapsed_ms"],
    )

    export_resp = client.request("POST", "/leads/export", {"lead_ids": [lead_id], "fields": []})
    export_text = export_resp["payload"] if isinstance(export_resp["payload"], str) else ""
    csv_rows = list(csv.reader(StringIO(export_text))) if export_text else []
    joined_csv = "\n".join(",".join(row) for row in csv_rows)
    record(
        results,
        "TC-012",
        "导出选中线索",
        export_resp["status"] == 200 and base_phone not in joined_csv and "****" in joined_csv,
        {"status": export_resp["status"], "csv_preview": csv_rows[:3]},
        "返回 CSV，手机号脱敏且不包含明文手机号",
        elapsed_ms=export_resp["elapsed_ms"],
    )

    contacts = detail_data.get("contacts", [])
    phone_contact = next((c for c in contacts if c.get("contact_type") == "phone"), None)
    reveal = client.request("POST", f"/leads/{lead_id}/contacts/{phone_contact['id']}/reveal", {"reason": "UAT 电话确认到店时间"}) if phone_contact else {"status": 0, "payload": {}, "elapsed_ms": 0}
    logs = client.request("GET", "/operation-logs", query={"event_type": "phone_revealed", "page_size": 10})
    logs_data = data_of(logs) if ok_json(logs) else {}
    log_items = logs_data.get("items", [])
    newest_meta = log_items[0].get("metadata", {}) if log_items else {}
    record(
        results,
        "TC-013",
        "手机号明文查看与审计",
        ok_json(reveal) and data_of(reveal).get("value") == base_phone and ok_json(logs) and not any(base_phone in json.dumps(item, ensure_ascii=False) for item in log_items[:3]),
        {"reveal": data_of(reveal), "latest_log_metadata": newest_meta},
        "返回明文给前端，审计日志不记录完整手机号",
        elapsed_ms=max(reveal["elapsed_ms"], logs["elapsed_ms"]),
    )

    filter_resp = client.request("GET", "/leads", query={"keyword": stamp[-4:], "status": "invalid", "page": 1, "page_size": 5})
    filter_data = data_of(filter_resp) if ok_json(filter_resp) else {}
    record(
        results,
        "TC-016",
        "筛选与分页",
        ok_json(filter_resp) and filter_data.get("page") == 1 and filter_data.get("page_size") == 5,
        filter_data,
        "支持 keyword/status/page/page_size 并返回分页结构",
        elapsed_ms=filter_resp["elapsed_ms"],
    )

    original_sales = sales
    unassigned_lead_id = None
    try:
        for item in original_sales:
            client.request("PUT", f"/sales/{item['id']}", sales_payload(item, enabled=False))
        no_sales = client.request("POST", "/leads", {"customer_name": "无销售客户" + stamp, "phones": [str(int(base_phone) + 2)], "remark": "无可用销售分配失败验证"})
        no_sales_data = data_of(no_sales) if ok_json(no_sales) else {}
        unassigned_lead_id = no_sales_data.get("id")
        record(
            results,
            "TC-007",
            "无可用销售时分配失败",
            no_sales_data.get("assign_status") == "assign_failed" and no_sales_data.get("assignment", {}).get("failure_reason") == "无可用销售",
            no_sales_data,
            "线索未分配，分配失败原因为无可用销售",
            elapsed_ms=no_sales["elapsed_ms"],
        )
    finally:
        restore_sales(client, original_sales)

    retry = client.request("POST", "/leads/retry-auto-assign", {"lead_ids": [unassigned_lead_id]}) if unassigned_lead_id else {"status": 0, "payload": {}, "elapsed_ms": 0}
    retry_data = data_of(retry) if ok_json(retry) else {}
    record(
        results,
        "TC-008",
        "重新分配线索",
        retry_data.get("succeeded") == 1,
        retry_data,
        "恢复销售配置后重新分配成功",
        elapsed_ms=retry["elapsed_ms"],
    )

    if original_sales:
        target = original_sales[0]
        changed = client.request(
            "PUT",
            f"/sales/{target['id']}",
            sales_payload(target, enabled=not target["enabled"]),
        )
        restore_sales(client, [target])
        sales_logs = client.request("GET", "/operation-logs", query={"event_type": "sales_enabled_changed", "page_size": 5})
        record(
            results,
            "TC-015",
            "销售状态开关",
            ok_json(changed) and ok_json(sales_logs) and data_of(sales_logs).get("total", 0) >= 1,
            {"update": data_of(changed), "log_total": data_of(sales_logs).get("total") if ok_json(sales_logs) else None},
            "启用/停用状态保存且记录 sales_enabled_changed 日志",
            elapsed_ms=max(changed["elapsed_ms"], sales_logs["elapsed_ms"]),
        )

    uat_results = [
        {
            "scenario_id": "UAT-001",
            "role": "运营管理员",
            "business_scenario": "接到客户线索后人工录入，系统完成去重和销售分配",
            "passed": any(r["case_id"] == "TC-003" and r["passed"] for r in results)
            and any(r["case_id"] == "TC-006" and r["passed"] for r in results),
            "acceptance": "线索可创建、可自动分配、详情可追踪任务链路",
        },
        {
            "scenario_id": "UAT-002",
            "role": "运营管理员",
            "business_scenario": "重复客户再次报备时不新建，补充备注进入原线索",
            "passed": any(r["case_id"] == "TC-004" and r["passed"] for r in results)
            and any(r["case_id"] == "TC-005" and r["passed"] for r in results),
            "acceptance": "重复手机号有预查，保存时事务内拒绝重复新建",
        },
        {
            "scenario_id": "UAT-003",
            "role": "销售主管",
            "business_scenario": "所有销售暂停轮询后线索进入待处理，恢复配置后重新分配",
            "passed": any(r["case_id"] == "TC-007" and r["passed"] for r in results)
            and any(r["case_id"] == "TC-008" and r["passed"] for r in results),
            "acceptance": "无可用销售不丢线索，恢复后可重新分配",
        },
        {
            "scenario_id": "UAT-004",
            "role": "运营管理员",
            "business_scenario": "客户信息敏感操作可审计，导出不泄漏手机号明文",
            "passed": any(r["case_id"] == "TC-012" and r["passed"] for r in results)
            and any(r["case_id"] == "TC-013" and r["passed"] for r in results),
            "acceptance": "手机号 reveal 有原因和审计，导出 CSV 保持脱敏",
        },
    ]

    return results, uat_results


def percentile(values, pct):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * pct))
    return ordered[index]


def run_perf(client: ApiClient, users: list[int], seconds: int):
    perf = []

    def hit_once():
        resp = client.request("GET", "/leads", query={"page": 1, "page_size": 20})
        return resp["status"], resp["elapsed_ms"]

    for concurrency in users:
        stop_at = time.perf_counter() + seconds
        samples = []
        statuses = []
        started = time.perf_counter()

        def worker():
            local = []
            while time.perf_counter() < stop_at:
                local.append(hit_once())
            return local

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(worker) for _ in range(concurrency)]
            for fut in as_completed(futures):
                for status, elapsed in fut.result():
                    statuses.append(status)
                    samples.append(elapsed)

        elapsed_s = max(time.perf_counter() - started, 0.001)
        success = sum(1 for s in statuses if 200 <= s < 300)
        total = len(statuses)
        perf.append(
            {
                "scenario": "GET /api/leads?page=1&page_size=20",
                "concurrent_users": concurrency,
                "duration_seconds": seconds,
                "requests": total,
                "qps": round(total / elapsed_s, 2),
                "success": success,
                "error_rate": round((total - success) / total, 4) if total else 1.0,
                "avg_ms": round(statistics.mean(samples), 2) if samples else 0,
                "p95_ms": round(percentile(samples, 0.95), 2),
                "p99_ms": round(percentile(samples, 0.99), 2),
                "max_ms": round(max(samples), 2) if samples else 0,
            }
        )
    return perf


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api")
    parser.add_argument("--perf-users", default="1,5,10,20")
    parser.add_argument("--perf-seconds", type=int, default=15)
    parser.add_argument("--output", default="deliverables/test_runs/p0_test_execution_result.json")
    args = parser.parse_args()

    client = ApiClient(args.base_url)
    functional, uat = run_functional_and_uat(client)
    perf_users = [int(v.strip()) for v in args.perf_users.split(",") if v.strip()]
    perf = run_perf(client, perf_users, args.perf_seconds)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "functional": functional,
        "uat": uat,
        "performance": perf,
        "summary": {
            "functional_total": len(functional),
            "functional_passed": sum(1 for item in functional if item["passed"]),
            "uat_total": len(uat),
            "uat_passed": sum(1 for item in uat if item["passed"]),
        },
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(json.dumps(output["summary"], ensure_ascii=False))
    failed = [item for item in functional if not item["passed"]] + [item for item in uat if not item["passed"]]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
