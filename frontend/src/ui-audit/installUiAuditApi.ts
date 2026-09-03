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

let knowledgeItems = [
  ["knowledge-price-boundary", "价格咨询回复边界", "客户询问价格时，只能引用车辆管理中已发布的公开售价；具体优惠、底价和最终成交价由销售确认。", "运营小陈", "2026-09-03T09:42:00+08:00"],
  ["knowledge-test-drive", "到店试驾安排", "客户希望试驾时，先确认意向车型、预计到店日期和方便时段；具体车辆与接待安排由销售确认。", "运营小陈", "2026-09-03T09:18:00+08:00"],
  ["knowledge-inventory", "在售与库存说明", "只能使用已上架车辆的公开信息回答；不得承诺实时库存、预留车辆或到店一定可看。", "系统", "2026-09-02T17:26:00+08:00"],
  ["knowledge-finance", "贷款与分期咨询", "可以收集预算、首付和月供偏好，但审批结果、利率和具体方案必须由销售或资方确认。", "运营小陈", "2026-09-02T15:54:00+08:00"],
  ["knowledge-trade-in", "旧车置换说明", "客户提出置换时，收集车型、年份、里程和车况；估值和置换金额不得由 AI 承诺。", "运营小陈", "2026-09-02T14:20:00+08:00"],
  ["knowledge-contract", "合同与定金边界", "涉及合同、定金、付款、发票或法律承诺时停止自动承诺，并进入既有人工确认流程。", "系统", "2026-09-01T18:38:00+08:00"],
  ["knowledge-visit", "门店到访信息", "可以介绍公开门店地址和营业时间；节假日或临时调整以销售确认的信息为准。", "系统", "2026-09-01T16:03:00+08:00"],
  ["knowledge-delivery", "交车与手续说明", "交车时间、过户材料和上牌安排以销售最终确认为准，AI 不得代替销售承诺具体日期。", "运营小陈", "2026-08-31T14:32:00+08:00"],
  ["knowledge-archived", "旧版车辆预留规则", "历史规则：客户看中车辆后可口头预留。", "运营小陈", "2026-08-29T11:16:00+08:00", "archived"],
  ["knowledge-archived-finance", "旧版金融方案说明", "历史规则：AI 可直接承诺固定分期利率。", "运营小陈", "2026-08-21T16:08:00+08:00", "archived"],
].map(([id, title, content, last_editor_name, updated_at, status = "published"]) => ({
  id: String(id),
  title: String(title),
  content: String(content),
  status: String(status),
  current_revision_id: `revision-${String(id)}`,
  last_editor_id: "operator-audit",
  last_editor_name: String(last_editor_name),
  published_at: String(updated_at),
  archived_at: status === "archived" ? String(updated_at) : null,
  created_at: "2026-08-20T10:00:00+08:00",
  updated_at: String(updated_at),
}));

let knowledgeReleases = [
  { id: "knowledge-release-07", version: "KR-20260903-02", status: "published", action: "update", operator_name: "运营小陈", change_summary: "修改 1 条知识变更", change_set: [{ type: "update", item_id: "knowledge-price-boundary", title: "价格咨询回复边界", before: null, after: null }], snapshot_sha256: "3ecf8a8dc2a44f32a4791f8f58276e6d0f49fd761e59ec1c94fd6992486cb948", published_at: "2026-09-03T09:42:00+08:00", is_current: true },
  { id: "knowledge-release-06", version: "KR-20260903-01", status: "published", action: "create", operator_name: "运营小陈", change_summary: "新增 1 条知识变更", change_set: [{ type: "create", item_id: "knowledge-test-drive", title: "到店试驾安排", before: null, after: null }], snapshot_sha256: "5d3bf8d69331d2dc1441318c652007b87f1caab775e5af62d87ec0ffce119f42", published_at: "2026-09-03T09:18:00+08:00", is_current: false },
  { id: "knowledge-release-05", version: "KR-20260902-03", status: "published", action: "archive", operator_name: "运营小陈", change_summary: "归档 1 条知识变更", change_set: [{ type: "archive", item_id: "knowledge-archived", title: "旧版车辆预留规则", before: null, after: null }], snapshot_sha256: "969ccf27a689f99617545dbc439dd2cab55b8dfef55862e521647c6b090d6fe5", published_at: "2026-09-02T17:26:00+08:00", is_current: false },
];

function knowledgeSnapshot() {
  return knowledgeItems.filter((item) => item.status === "published").map((item) => ({
    item_id: item.id,
    revision_id: item.current_revision_id,
    title: item.title,
    content: item.content,
    content_sha256: `audit-${item.id}`,
  }));
}

function requestBody(init?: RequestInit) {
  if (typeof init?.body !== "string") return {} as Record<string, unknown>;
  try {
    return JSON.parse(init.body) as Record<string, unknown>;
  } catch {
    return {} as Record<string, unknown>;
  }
}

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
  { id: "log-knowledge-1", event_type: "knowledge_published", module: "knowledge", operator_id: "operator-audit", operator_name: "运营小陈", target_type: "knowledge_item", target_id: "knowledge-price-boundary", lead_id: null, metadata: { operation: "update", target_version: "KR-20260903-02", title: "价格咨询回复边界" }, before_data: { release_id: "knowledge-release-06", version: "KR-20260903-01" }, after_data: { release_id: "knowledge-release-07", version: "KR-20260903-02" }, result: "success", summary: "修改价格咨询回复边界并发布新版本", created_at: "2026-09-03T09:42:00+08:00" },
  { id: "log-knowledge-2", event_type: "knowledge_rollback_previewed", module: "knowledge", operator_id: "operator-audit", operator_name: "运营小陈", target_type: "knowledge_release", target_id: "knowledge-release-05", lead_id: null, metadata: { target_version: "KR-20260903-03", change_count: 2 }, before_data: { version: "KR-20260903-02" }, after_data: { version: "KR-20260902-03" }, result: "success", summary: "预览回滚至 KR-20260902-03 的完整差异", created_at: "2026-09-03T09:36:00+08:00" },
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
  if (path === "/knowledge/summary") {
    return json({
      current_release: knowledgeReleases[0],
      published_today: 2,
      published_today_breakdown: { create: 1, update: 1, archive: 0, rollback: 0 },
      published_count: knowledgeItems.filter((item) => item.status === "published").length,
      archived_count: knowledgeItems.filter((item) => item.status === "archived").length,
    });
  }
  if (path === "/knowledge/items") {
    const keyword = String(url.searchParams.get("keyword") || "").trim().toLowerCase();
    const status = String(url.searchParams.get("status") || "all");
    const page = Number(url.searchParams.get("page") || 1);
    const pageSize = Number(url.searchParams.get("page_size") || 20);
    const filtered = knowledgeItems.filter((item) => {
      const matchesStatus = status === "all" || !status || item.status === status;
      const matchesKeyword = !keyword || `${item.title}\n${item.content}`.toLowerCase().includes(keyword);
      return matchesStatus && matchesKeyword;
    });
    return json({ items: filtered.slice((page - 1) * pageSize, page * pageSize), page, page_size: pageSize, total: filtered.length });
  }
  const knowledgeItemMatch = path.match(/^\/knowledge\/items\/([^/]+)$/);
  if (knowledgeItemMatch) {
    const item = knowledgeItems.find((row) => row.id === decodeURIComponent(knowledgeItemMatch[1])) ?? knowledgeItems[0];
    return json({ ...item, release_history: knowledgeReleases.filter((release) => release.change_set.some((change) => change.item_id === item.id)) });
  }
  if (path === "/knowledge/releases/preview" && method === "POST") {
    const body = requestBody(init);
    const operation = String(body.operation || "create");
    const itemId = String(body.item_id || `knowledge-audit-${Date.now()}`);
    const existing = knowledgeItems.find((item) => item.id === itemId);
    const title = String(body.title || existing?.title || "");
    const content = String(body.content || existing?.content || "");
    const issues = !title || !content ? [{ field: !title ? "title" : "content", problem: "知识标题和规则正文不能为空", suggestion: "填写完整内容后再发布" }] : [];
    return json({
      preview_id: `preview-${Date.now()}`,
      operation,
      item_id: itemId,
      current_version: knowledgeReleases[0].version,
      target_version: "KR-20260903-03",
      target_release_id: null,
      can_publish: issues.length === 0,
      validation_issues: issues,
      change_set: [{ type: operation, item_id: itemId, title, before: existing ? { item_id: existing.id, revision_id: existing.current_revision_id, title: existing.title, content: existing.content, content_sha256: `audit-${existing.id}` } : null, after: operation === "archive" ? null : { item_id: itemId, revision_id: `revision-${Date.now()}`, title, content, content_sha256: `audit-${itemId}` } }],
      content_digest: "a".repeat(64),
      expires_at: "2026-09-03T11:00:00+08:00",
    });
  }
  if (path === "/knowledge/releases/rollback/preview" && method === "POST") {
    const body = requestBody(init);
    const target = knowledgeReleases.find((release) => release.id === body.target_release_id) ?? knowledgeReleases[1];
    return json({ preview_id: `rollback-preview-${Date.now()}`, operation: "rollback", item_id: null, current_version: knowledgeReleases[0].version, target_version: "KR-20260903-03", target_release_id: target.id, can_publish: true, validation_issues: [], change_set: target.change_set, content_digest: "b".repeat(64), expires_at: "2026-09-03T11:00:00+08:00" });
  }
  if (path === "/knowledge/releases" && method === "POST") {
    return json({ release: { ...knowledgeReleases[0], snapshot: knowledgeSnapshot() }, item: null, message: "新创建的 AI 对话批次将使用此版本" });
  }
  if (path === "/knowledge/releases") return json({ items: knowledgeReleases, page: 1, page_size: 20, total: knowledgeReleases.length });
  const knowledgeReleaseMatch = path.match(/^\/knowledge\/releases\/([^/]+)$/);
  if (knowledgeReleaseMatch) {
    const release = knowledgeReleases.find((item) => item.id === decodeURIComponent(knowledgeReleaseMatch[1])) ?? knowledgeReleases[0];
    return json({ ...release, snapshot: knowledgeSnapshot() });
  }
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
