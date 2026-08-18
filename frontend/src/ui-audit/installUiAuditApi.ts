const auditNow = "2026-06-05T10:22:00+08:00";

const sales = [
  { id: "sales-zhang", sales_name: "张伟", phone: "13800000001", wechat: "zhangwei", feishu_user_id: null, worker_id: "wk_20260605_8f3a2c9b", enabled: true, sort_order: 1, remark: null, lead_count: 42, today_assignment_count: 8, blocking_task_count: 0, current_worker: { id: "wk_20260605_8f3a2c9b", worker_name: "Mac-01 展厅机", device_name: "Mac-01", platform: "windows", enabled: true, online_status: "online", running_status: "idle", current_task: null, last_heartbeat_at: auditNow, client_binding_state: "bound", remark: null, bound_sales_id: "sales-zhang", bound_sales_name: "张伟" } },
  { id: "sales-wang", sales_name: "王敏", phone: "13800000002", wechat: "wangmin", feishu_user_id: null, worker_id: "wk_20260605_2a91bd45", enabled: true, sort_order: 2, remark: null, lead_count: 37, today_assignment_count: 6, blocking_task_count: 1, current_worker: { id: "wk_20260605_2a91bd45", worker_name: "Mac-02 客服机", device_name: "Mac-02", platform: "windows", enabled: true, online_status: "offline", running_status: "idle", current_task: null, last_heartbeat_at: "2026-06-05T10:10:00+08:00", client_binding_state: "bound", remark: null, bound_sales_id: "sales-wang", bound_sales_name: "王敏" } },
  { id: "sales-li", sales_name: "李强", phone: "13800000003", wechat: "liqiang", feishu_user_id: null, worker_id: null, enabled: false, sort_order: 3, remark: null, lead_count: 21, today_assignment_count: 0, blocking_task_count: 1, current_worker: null },
];

const workers = [
  { id: "wk_20260605_8f3a2c9b", worker_name: "Mac-01 展厅机", device_name: "Mac-01", platform: "windows", enabled: true, online_status: "online", running_status: "idle", current_task: null, last_heartbeat_at: auditNow, client_binding_state: "bound", remark: null, bound_sales_id: "sales-zhang", bound_sales_name: "张伟", worker_token: "wkt_audit_masked" },
  { id: "wk_20260605_2a91bd45", worker_name: "Mac-02 客服机", device_name: "Mac-02", platform: "windows", enabled: true, online_status: "offline", running_status: "idle", current_task: null, last_heartbeat_at: "2026-06-05T10:10:00+08:00", client_binding_state: "bound", remark: null, bound_sales_id: "sales-wang", bound_sales_name: "王敏", worker_token: "wkt_audit_masked" },
  { id: "wk_20260605_b7349c10", worker_name: "Mac-03 备用机", device_name: "Mac-03", platform: "windows", enabled: false, online_status: "offline", running_status: "idle", current_task: null, last_heartbeat_at: null, client_binding_state: "unbound", remark: null, bound_sales_id: null, bound_sales_name: null, worker_token: "wkt_audit_masked" },
];

const leads = [
  ["static-lead-1", "王先生", "assigned", "138****6678", "wx_car_2026", "sales-zhang", "张伟", 3, "对新能源车型感兴趣", "2026-06-02T14:30:00+08:00"],
  ["static-lead-2", "李女士", "unassigned", "139****9081", "wx_li_auto", null, null, 0, "关注到店试驾", "2026-06-02T13:12:00+08:00"],
  ["static-lead-3", "刘先生", "assigned", "136****4210", null, "sales-wang", "王敏", 1, "预算 20 万左右", "2026-06-02T11:08:00+08:00"],
  ["static-lead-4", "赵女士", "invalid", "137****2331", "wx_zhao_car", "sales-li", "李强", 0, "空号", "2026-06-01T18:42:00+08:00"],
].map(([id, customer_name, status, primary_phone_masked, primary_wechat_masked, sales_id, sales_name, duplicate_count, remark_summary, updated_at]) => ({
  id, customer_name, status, source_type: "manual", source_name_snapshot: "人工录入", primary_phone_masked, primary_wechat_masked,
  sales_id, sales_name, assign_status: status === "assigned" ? "assigned" : "unassigned", assign_failure_reason: null,
  remark_summary, duplicate_count, last_duplicate_at: duplicate_count ? updated_at : null, created_at: updated_at, updated_at,
}));

const vehicles = [
  ["VEH-202608-0126", "2022 款 2.0T 运动版", "雪佛兰", "科迈罗", "2022-06", 28000, 22.8, "listed", "vehicle-01.jpg", "2026-08-05T10:18:00+08:00"],
  ["VEH-202608-0125", "2021 款 3.0T 豪华版", "保时捷", "Panamera", "2021-09", 41000, 68.6, "listed", "vehicle-02.jpg", "2026-08-05T09:42:00+08:00"],
  ["VEH-202608-0124", "2020 款 1.4L 城市版", "菲亚特", "500", "2020-03", 56000, 8.9, "unlisted", "vehicle-03.jpg", "2026-08-04T18:26:00+08:00"],
  ["VEH-202608-0123", "2019 款 4.0T 性能版", "梅赛德斯-AMG", "GT", "2019-11", 32000, 98.8, "unlisted", "vehicle-04.jpg", "2026-08-04T16:12:00+08:00"],
].map(([vehicle_code, display_name, brand, series, first_registration, mileage_km, public_price, listing_status, filename, updated_at], index) => {
  const image = { id: `vehicle-image-${index + 1}`, url: `http://127.0.0.1:8790/website/assets/vehicles/${filename}`, original_filename: String(filename), content_type: "image/jpeg", size_bytes: 102400, sha256: `audit-${index + 1}`, sort_order: 0, is_main: true, created_at: String(updated_at) };
  return { vehicle_code, display_name, brand, series, model: null, public_price, first_registration, mileage_km, exterior_color: null, interior_color: null, location: null, customer_description: null, vin: null, plate_number: null, purchase_price: null, internal_notes: null, listing_status, images: [image], main_image: image, created_at: String(updated_at), updated_at };
});

type AuditTask = Record<string, unknown> & { id: string; events: Array<Record<string, unknown>> };
const taskRows = [
  ["TASK-1831", "add_friend", "running", "王先生", "138****6678", "张伟", "sales-zhang", "Mac-01 展厅机", "wk_20260605_8f3a2c9b", null, null, "add_friend_starting", "10:18"],
  ["TASK-1842", "chat_reply", "running", "林女士", "135****1086", "张伟", "sales-zhang", "Mac-01 展厅机", "wk_20260605_8f3a2c9b", null, null, "c3_brain_waiting", "10:20"],
  ["TASK-1841", "chat_reply", "failed", "陈先生", "137****6028", "王敏", "sales-wang", "Mac-02 客服机", "wk_20260605_2a91bd45", null, "C2_REPLY_CONTEXT_RECOVERY_FAILED", "state_target_message_read", "10:08"],
  ["TASK-1840", "chat_reply", "completed", "吴先生", "138****4196", "张伟", "sales-zhang", "Mac-01 展厅机", "wk_20260605_8f3a2c9b", "chat_reply_sent", null, "task_completed", "09:53"],
  ["TASK-1830", "add_friend", "completed", "李女士", "139****9081", "王敏", "sales-wang", "Mac-02 客服机", "wk_20260605_2a91bd45", "invite_sent", null, null, "09:42"],
  ["TASK-1829", "add_friend", "failed", "赵先生", "137****2331", "张伟", "sales-zhang", "Mac-01 展厅机", "wk_20260605_8f3a2c9b", null, "PHONE_NOT_FOUND", "searching_phone", "09:18"],
  ["TASK-1828", "add_friend", "blocked", "周先生", "136****4210", "李强", "sales-li", null, null, null, null, null, "08:55", "SALES_WORKER_NOT_BOUND"],
  ["TASK-1826", "add_friend", "pending", "陈女士", "138****9518", "张伟", "sales-zhang", null, null, null, null, null, "15:40"],
].map(([id, task_type, status, customer, phone, sales_name, sales_id, worker_name, worker_id, result_code, error_code, current_step, time, block_code]) => {
  const at = `2026-06-05T${time}:00+08:00`;
  const baseEvents: Array<Record<string, unknown>> = [
    { id: `${id}-created`, task_id: id, event_type: "created", from_status: null, to_status: "pending", current_step: null, operator_name: "服务端", result_code: null, error_code: null, block_code: null, remark: null, created_at: at },
  ];
  if (current_step) baseEvents.push({ id: `${id}-step`, task_id: id, event_type: status === "failed" ? "failed" : "claimed", from_status: "pending", to_status: status, current_step, operator_name: worker_name || "服务端", result_code, error_code, block_code, remark: null, created_at: at });
  return { id, task_type, status, result_code, error_code, block_code: block_code || null, current_step, lead_id: `lead-${id}`, primary_phone_masked: phone, sales_id, sales_name, worker_id, executor_type: "worker", executor_id: worker_id, last_heartbeat_at: worker_id ? auditNow : null, completed_at: status === "completed" ? at : null, cancelled_at: null, result_remark: null, block_reason: null, cancel_reason: null, remark: null, original_task_id: null, business_object: { type: "lead", lead: { id: `lead-${id}`, customer_name: customer, status: "assigned", primary_phone_masked: phone, phone_suffix: String(phone).slice(-4), remark: null } }, execution: { sales: { id: sales_id, sales_name, wechat: null, enabled: true, worker_id }, worker: worker_id ? { id: worker_id, worker_name, device_name: worker_name, enabled: true, online_status: worker_name === "Mac-02 客服机" ? "offline" : "online", running_status: status === "running" ? "busy" : "idle", current_task: status === "running" ? id : null, last_heartbeat_at: auditNow } : null, current_step, claimed_at: worker_id ? at : null, completed_at: status === "completed" ? at : null, failed_at: status === "failed" ? at : null, cancelled_at: null }, created_at: at, updated_at: at, events: baseEvents, notes: [], comments: [] } as AuditTask;
});

const logs = [
  { id: "log-1", event_type: "sales_worker_bound", module: "sales", operator_id: "operator-audit", operator_name: "运营小陈", target_type: "sales", target_id: "sales-zhang", lead_id: null, metadata: { sales_name: "张伟" }, before_data: { worker_id: null }, after_data: { worker_id: "wk_20260605_8f3a2c9b", worker_name: "Mac-01 展厅机" }, result: "success", summary: "销售后续可参与自动任务执行", created_at: "2026-06-05T10:22:00+08:00" },
  { id: "log-2", event_type: "task_created", module: "task", operator_id: null, operator_name: "系统", target_type: "task", target_id: "TASK-1831", lead_id: null, metadata: {}, before_data: null, after_data: { task_type: "add_friend", status: "pending" }, result: "success", summary: "已创建添加通讯录邀请任务", created_at: "2026-06-05T10:18:00+08:00" },
  { id: "log-3", event_type: "worker_binding_reset", module: "worker", operator_id: "operator-audit", operator_name: "运营小陈", target_type: "worker", target_id: "wk_20260605_2a91bd45", lead_id: null, metadata: { worker_name: "Mac-02 客服机" }, before_data: { client_binding_state: "bound" }, after_data: { client_binding_state: "reset_required" }, result: "success", summary: "新 Token 已生成，旧客户端失效", created_at: "2026-06-05T09:58:00+08:00" },
  { id: "log-4", event_type: "task_comment_added", module: "task", operator_id: "operator-audit", operator_name: "运营小陈", target_type: "task", target_id: "TASK-1830", lead_id: null, metadata: {}, before_data: null, after_data: { remark: "补充邀请发送结果说明" }, result: "success", summary: "补充邀请发送结果说明", created_at: "2026-06-05T09:42:00+08:00" },
  { id: "log-5", event_type: "task_unblocked", module: "task", operator_id: "operator-audit", operator_name: "运营小陈", target_type: "task", target_id: "TASK-1828", lead_id: null, metadata: {}, before_data: { status: "blocked", block_code: "SALES_WORKER_NOT_BOUND" }, after_data: { status: "pending" }, result: "success", summary: "销售已绑定可用 Worker", created_at: "2026-06-05T09:15:00+08:00" },
];

function leadDetail(id: string) {
  const lead = leads.find((item) => item.id === id) ?? leads[0];
  return { ...lead, remark: lead.remark_summary, custom_fields: null, contacts: [
    { id: `${id}-phone`, contact_type: "phone", masked_value: lead.primary_phone_masked, is_primary: true },
    { id: `${id}-wechat`, contact_type: "wechat", masked_value: lead.primary_wechat_masked || "未填写", is_primary: false },
  ], notes: [], assignments: [], duplicate_events: [], task_nodes: [{ key: "created", label: "客户线索已创建", time: lead.created_at }] };
}

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify({ code: status === 200 ? "OK" : "ERROR", message: status === 200 ? "成功" : "请求失败", data, trace_id: "ui-audit" }), { status, headers: { "Content-Type": "application/json" } });
}

function auditResponse(url: URL, init?: RequestInit) {
  const path = url.pathname.replace(/^\/api/, "");
  const method = String(init?.method || "GET").toUpperCase();
  if (path === "/auth/session") return json({ operator_id: "operator-audit", operator_name: "运营小陈" });
  if (path === "/auth/logout" && method === "POST") return json({ logged_out: true });
  if (path === "/leads/stats") return json({ today_new_count: 28, today_assigned_count: 24, today_unassigned_count: 4, assignment_success_rate: 85.7, assigned_count: 24, unassigned_count: 4, duplicate_event_count: 7 });
  if (path === "/leads") return json({ items: leads, page: 1, page_size: 20, total: 128 });
  if (/^\/leads\/[^/]+$/.test(path)) return json(leadDetail(decodeURIComponent(path.split("/")[2])));
  if (path === "/vehicles") {
    const listingStatus = url.searchParams.get("listing_status");
    const filtered = listingStatus && listingStatus !== "all" ? vehicles.filter((item) => item.listing_status === listingStatus) : vehicles;
    const pageSize = Number(url.searchParams.get("page_size") || 20);
    return json({ items: filtered.slice(0, pageSize), page: 1, page_size: pageSize, total: filtered.length });
  }
  if (/^\/vehicles\/[^/]+$/.test(path)) return json(vehicles.find((item) => item.vehicle_code === decodeURIComponent(path.split("/")[2])) ?? vehicles[0]);
  if (path === "/sales") return json({ items: sales });
  if (/^\/sales\/[^/]+$/.test(path)) return json(sales.find((item) => item.id === path.split("/")[2]) ?? sales[0]);
  if (path === "/workers") return json({ items: workers });
  if (/^\/workers\/[^/]+$/.test(path)) return json(workers.find((item) => item.id === path.split("/")[2]) ?? workers[0]);
  if (path === "/tasks") return json({ items: taskRows, total: 128, page: 1, page_size: 20, metrics: { blocked: 3, pending: 12, running: 2, completed_today: 28, failed_today: 4 } });
  const taskEventsMatch = path.match(/^\/tasks\/([^/]+)\/events$/);
  if (taskEventsMatch) return json({ items: taskRows.find((item) => item.id === taskEventsMatch[1])?.events ?? [] });
  const taskMatch = path.match(/^\/tasks\/([^/]+)$/);
  if (taskMatch) return json(taskRows.find((item) => item.id === taskMatch[1]) ?? taskRows[0]);
  if (path === "/operation-logs") return json({ items: logs, page: 1, page_size: 20, total: logs.length });
  return json({}, 404);
}

export function installUiAuditApi() {
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const requestUrl = new URL(input instanceof Request ? input.url : String(input), window.location.href);
    const imageMatch = requestUrl.pathname.match(/^\/api\/vehicles\/images\/vehicle-image-(\d+)$/);
    if (imageMatch) {
      const imageNumber = Math.min(4, Math.max(1, Number(imageMatch[1])));
      return originalFetch(`/@fs/Users/zhangwentao/Documents/车金/website/assets/vehicles/vehicle-0${imageNumber}.jpg`);
    }
    if (requestUrl.pathname.startsWith("/api/")) return Promise.resolve(auditResponse(requestUrl, init));
    return originalFetch(input, init);
  };
}
