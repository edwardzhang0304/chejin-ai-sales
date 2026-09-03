const API_BASE =
  new URLSearchParams(window.location.search).get("api") ||
  window.CHEJIN_API_BASE ||
  "http://127.0.0.1:8000/api";

const OPERATOR_HEADERS = {
  "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
  "X-Operator-Role": "admin",
};

const STATIC_DESIGN_PREVIEW = window.location.port === "8790" || window.location.protocol === "file:";

const authParams = new URLSearchParams(window.location.search);
const loginView = document.querySelector("[data-login-view]");
const appShell = document.querySelector("[data-app-shell]");
const loginForm = document.querySelector("[data-login-form]");
const loginUsername = document.querySelector("[data-login-username]");
const loginPassword = document.querySelector("[data-login-password]");
const loginSubmit = document.querySelector("[data-login-submit]");
const loginSubmitLabel = document.querySelector("[data-login-submit-label]");
const loginSpinner = document.querySelector("[data-login-spinner]");
const loginAlert = document.querySelector("[data-login-alert]");
const passwordToggle = document.querySelector("[data-password-toggle]");
const accountButton = document.querySelector("[data-account-button]");
const accountMenu = document.querySelector("[data-account-menu]");

const setAuthenticatedView = (authenticated) => {
  document.body.classList.toggle("is-login", !authenticated);
  loginView.hidden = authenticated;
  appShell.hidden = !authenticated;
  if (authenticated) {
    accountMenu.hidden = true;
    accountButton?.setAttribute("aria-expanded", "false");
  } else {
    loginForm?.reset();
    loginPassword.type = "password";
    passwordToggle?.setAttribute("aria-label", "显示密码");
    passwordToggle?.setAttribute("aria-pressed", "false");
    loginSubmitLabel.textContent = "登录";
    loginSpinner.hidden = true;
    loginSubmit.disabled = true;
  }
};

const loginMessages = {
  invalid: "账号或密码错误",
  disabled: "账号已停用，请联系管理员",
  network: "暂时无法登录，请稍后重试",
  expired: "登录已失效，请重新登录",
};

const setLoginMessage = (stateName) => {
  const message = loginMessages[stateName];
  loginAlert.hidden = !message;
  loginAlert.textContent = message || "";
  loginAlert.classList.toggle("is-session", stateName === "expired");
};

const syncLoginSubmit = () => {
  loginSubmit.disabled = !(loginUsername.value.trim() && loginPassword.value);
};

const initialAuthState = authParams.get("auth");
const showAppInitially = initialAuthState === "app" || (!initialAuthState && authParams.has("module"));
setAuthenticatedView(showAppInitially);
if (!showAppInitially) setLoginMessage(initialAuthState || "login");

loginUsername?.addEventListener("input", syncLoginSubmit);
loginPassword?.addEventListener("input", syncLoginSubmit);

passwordToggle?.addEventListener("click", () => {
  const reveal = loginPassword.type === "password";
  loginPassword.type = reveal ? "text" : "password";
  passwordToggle.setAttribute("aria-label", reveal ? "隐藏密码" : "显示密码");
  passwordToggle.setAttribute("aria-pressed", String(reveal));
});

loginForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  if (loginSubmit.disabled) return;
  loginSubmit.disabled = true;
  loginSubmitLabel.textContent = "登录中";
  loginSpinner.hidden = false;
  loginAlert.hidden = true;
  window.setTimeout(() => {
    loginSubmitLabel.textContent = "登录";
    loginSpinner.hidden = true;
    setAuthenticatedView(true);
  }, 650);
});

accountButton?.addEventListener("click", () => {
  const nextExpanded = accountButton.getAttribute("aria-expanded") !== "true";
  accountButton.setAttribute("aria-expanded", String(nextExpanded));
  accountMenu.hidden = !nextExpanded;
});

const statusMeta = {
  assigned: { label: "已分配", className: "assigned" },
  unassigned: { label: "未分配", className: "unassigned" },
  invalid: { label: "无效", className: "invalid" },
};

const state = {
  apiReady: !STATIC_DESIGN_PREVIEW,
  module: null,
  leadsLoaded: STATIC_DESIGN_PREVIEW,
  leads: [],
  sales: [],
  selectedIds: new Set(),
  activeLeadId: null,
  pendingRestoreLeadId: null,
  invalidMode: "single",
  vehicleEditing: false,
  vehicleImportStep: 1,
  vehicleImportPreview: "valid",
  vehicleImportResult: "success",
  pendingVehicleClose: false,
  activeKnowledgeId: "pricing-rule",
  knowledgeEditing: false,
  pendingKnowledgeClose: false,
  knowledgePublishMode: "edit",
  page: 1,
  pageSize: 20,
  total: 0,
};

const knowledgeItems = {
  "pricing-rule": {
    title: "车辆报价规则",
    status: "published",
    editor: "张三",
    updated: "今天 13:40",
    version: "KR-20260903-12",
    content: "客户询问车辆价格时，应以车辆管理中已上架车辆的公开售价为准，不得使用采购价格或其他内部价格回答客户。",
  },
  "finance-plan": {
    title: "金融方案说明",
    status: "published",
    editor: "李四",
    updated: "昨天 18:20",
    version: "KR-20260902-11",
    content: "客户咨询金融方案时，只说明当前可提供的方案范围和申请流程，不承诺审批结果、最终利率或具体放款时间。",
  },
  "test-drive": {
    title: "预约试驾说明",
    status: "published",
    editor: "张三",
    updated: "09-01 11:16",
    version: "KR-20260901-10",
    content: "客户提出试驾需求时，应先确认意向车辆、到店日期和联系电话，再由销售确认具体可预约时段。",
  },
  "old-campaign": {
    title: "旧活动政策",
    status: "archived",
    editor: "王五",
    updated: "08-21 16:08",
    version: "KR-20260821-10",
    content: "八月到店活动已结束，历史内容仅用于运营追溯，不再用于新的客户回复。",
  },
};

const workspace = document.querySelector("[data-workspace]");
const emptyWorkspace = document.querySelector("[data-empty-workspace]");
const moduleButtons = document.querySelectorAll("[data-module]");
const moduleContents = document.querySelectorAll("[data-module-content]");
const drawer = document.querySelector("[data-drawer]");
const toast = document.querySelector("[data-toast]");
const staticSnapshot = {
  table: document.querySelector("[data-leads-table]")?.innerHTML || "",
  total: document.querySelector("[data-total-count]")?.textContent || "128",
  pagination: document.querySelector("[data-pagination]")?.innerHTML || "",
  selectedText: document.querySelector("[data-selected-count]")?.textContent || "已选 2 条",
};

const staticLeadDetails = {
  "static-lead-1": {
    customer_name: "王先生",
    status: "assigned",
    primary_phone_masked: "138****6678",
    sales_name: "张伟",
    created_at: "2026-06-02T14:30:00+08:00",
    contacts: [
      { id: "static-phone-1", contact_type: "phone", masked_value: "138****6678" },
      { id: "static-wechat-1", contact_type: "wechat", masked_value: "wx_car_2026" },
      { id: "static-email-1", contact_type: "email", masked_value: "未填写" },
    ],
    task_nodes: [
      { label: "手机号去重完成", time: "未新建重复线索，记录可追溯。" },
      { label: "轮询分配完成", time: "指针 A -> B，分配给张伟。" },
      { label: "重复备注已追加", time: "2026-06-02 14:30 运营小陈追加。" },
    ],
  },
  "static-lead-2": {
    customer_name: "李女士",
    status: "unassigned",
    primary_phone_masked: "139****9081",
    sales_name: "暂无",
    created_at: "2026-06-02T13:12:00+08:00",
    contacts: [
      { id: "static-phone-2", contact_type: "phone", masked_value: "139****9081" },
      { id: "static-wechat-2", contact_type: "wechat", masked_value: "wx_li_auto" },
      { id: "static-email-2", contact_type: "email", masked_value: "未填写" },
    ],
    task_nodes: [
      { label: "客户线索已创建", time: "2026-06-02 13:12" },
      { label: "等待可用销售", time: "当前暂无可分配销售。" },
    ],
  },
  "static-lead-3": {
    customer_name: "刘先生",
    status: "assigned",
    primary_phone_masked: "136****4210",
    sales_name: "王敏",
    created_at: "2026-06-02T11:08:00+08:00",
    contacts: [
      { id: "static-phone-3", contact_type: "phone", masked_value: "136****4210" },
      { id: "static-wechat-3", contact_type: "wechat", masked_value: "未填写" },
      { id: "static-email-3", contact_type: "email", masked_value: "未填写" },
    ],
    task_nodes: [
      { label: "手机号去重完成", time: "发现历史备注 1 次。" },
      { label: "轮询分配完成", time: "分配给王敏。" },
    ],
  },
  "static-lead-4": {
    customer_name: "赵女士",
    status: "invalid",
    primary_phone_masked: "137****2331",
    sales_name: "李强",
    created_at: "2026-06-01T18:42:00+08:00",
    contacts: [
      { id: "static-phone-4", contact_type: "phone", masked_value: "137****2331" },
      { id: "static-wechat-4", contact_type: "wechat", masked_value: "wx_zhao_car" },
      { id: "static-email-4", contact_type: "email", masked_value: "未填写" },
    ],
    task_nodes: [
      { label: "客户线索已创建", time: "2026-06-01 18:42" },
      { label: "标记为无效", time: "无效原因：空号。" },
    ],
  },
};

const taskStatusMeta = {
  blocked: { label: "阻塞", className: "blocked" },
  pending: { label: "待处理", className: "pending" },
  running: { label: "处理中", className: "running" },
  completed: { label: "已完成", className: "completed" },
  failed: { label: "失败", className: "failed" },
  cancelled: { label: "已取消", className: "cancelled" },
};

const staticTaskDetails = {
  "TASK-1831": {
    status: "running",
    basic: {
      任务类型: "添加通讯录邀请",
      执行状态: "处理中",
      业务结果: "-",
      异常原因: "-",
      当前步骤: "正在执行微信加好友",
      创建时间: "2026-06-05 10:12",
      更新时间: "2026-06-05 10:18",
    },
    object: {
      客户: "王先生",
      手机号: "138****6678",
      微信号: "wx_car_2026",
      线索状态: "已分配",
      当前销售: "张伟",
    },
    executor: {
      执行方类型: "Worker",
      执行方: "Mac-01 展厅机",
      执行方状态: "在线 · 忙碌",
      最近心跳: "刚刚",
      领取时间: "2026-06-05 10:18",
    },
    flow: [
      ["任务已创建", "10:12 · 服务端"],
      ["任务已领取", "10:18 · Mac-01"],
      ["正在执行微信加好友", "10:18 · Mac-01"],
    ],
    note: "暂无备注",
    actions: ["查看执行方", "取消任务", "补充备注"],
  },
  "TASK-1830": {
    status: "completed",
    basic: {
      任务类型: "添加通讯录邀请",
      执行状态: "已完成",
      业务结果: "已发送添加通讯录邀请",
      异常原因: "-",
      当前步骤: "-",
      创建时间: "2026-06-05 09:30",
      更新时间: "2026-06-05 09:42",
      完成时间: "2026-06-05 09:42",
    },
    object: {
      客户: "李女士",
      手机号: "139****9081",
      微信号: "wx_li_auto",
      线索状态: "已分配",
      当前销售: "王敏",
    },
    executor: {
      执行方类型: "Worker",
      执行方: "Mac-02 客服机",
      执行方状态: "在线 · 空闲",
      最近心跳: "刚刚",
      领取时间: "2026-06-05 09:35",
    },
    advice: "已发送添加通讯录邀请，不代表客户已同意好友申请。",
    adviceTitle: "结果说明",
    flow: [
      ["任务已创建", "09:30 · 服务端"],
      ["任务已领取", "09:35 · Mac-02"],
      ["已发送添加通讯录邀请", "09:42 · Mac-02"],
    ],
    note: "暂无备注",
    actions: ["查看执行结果", "查看执行方", "补充备注"],
  },
  "TASK-1829": {
    status: "failed",
    basic: {
      任务类型: "添加通讯录邀请",
      执行状态: "失败",
      业务结果: "-",
      异常原因: "手机号未找到客户",
      当前步骤: "搜索手机号",
      创建时间: "2026-06-05 09:10",
      更新时间: "2026-06-05 09:18",
    },
    object: {
      客户: "赵先生",
      手机号: "137****2331",
      微信号: "wx_zhao_car",
      线索状态: "已分配",
      当前销售: "张伟",
    },
    executor: {
      执行方类型: "Worker",
      执行方: "Mac-01 展厅机",
      执行方状态: "在线 · 空闲",
      最近心跳: "刚刚",
      领取时间: "2026-06-05 09:12",
    },
    flow: [
      ["任务已创建", "09:10 · 服务端"],
      ["任务已领取", "09:12 · Mac-01"],
      ["执行微信加好友", "09:13 · Mac-01"],
      ["执行失败：手机号未找到客户", "09:18 · Mac-01"],
    ],
    note: "微信搜索该手机号未找到客户。",
    actions: ["重新创建任务", "查看执行方", "补充备注", "标记线索无效"],
  },
  "TASK-1828": {
    status: "blocked",
    basic: {
      任务类型: "添加通讯录邀请",
      执行状态: "阻塞",
      业务结果: "-",
      异常原因: "销售未绑定 Worker",
      当前步骤: "-",
      创建时间: "2026-06-05 08:52",
      更新时间: "2026-06-05 08:55",
    },
    object: {
      客户: "周先生",
      手机号: "136****4210",
      微信号: "未填写",
      线索状态: "已分配",
      当前销售: "李强",
    },
    executor: {
      执行方类型: "Worker",
      执行方: "未绑定",
      执行方状态: "-",
      最近心跳: "-",
      领取时间: "-",
    },
    advice: "该任务当前不可领取。阻塞原因：销售未绑定 Worker。建议进入销售详情，为该销售绑定可用 Worker。",
    adviceTitle: "处理建议",
    flow: [
      ["任务已创建", "08:52 · 服务端"],
      ["任务阻塞：销售未绑定 Worker", "08:55 · 服务端"],
    ],
    note: "绑定 Worker 后，服务端可将任务恢复为待处理。",
    actions: ["去绑定 Worker", "取消任务", "补充备注"],
  },
  "TASK-1827": {
    status: "completed",
    basic: {
      任务类型: "添加通讯录邀请",
      执行状态: "已完成",
      业务结果: "已是好友",
      异常原因: "-",
      当前步骤: "-",
      创建时间: "2026-06-04 16:20",
      更新时间: "2026-06-04 16:33",
      完成时间: "2026-06-04 16:33",
    },
    object: {
      客户: "刘先生",
      手机号: "136****4604",
      微信号: "wx****04",
      线索状态: "已分配",
      当前销售: "王敏",
    },
    executor: {
      执行方类型: "Worker",
      执行方: "Mac-02 客服机",
      执行方状态: "离线",
      最近心跳: "12 分钟前",
      领取时间: "2026-06-04 16:24",
    },
    advice: "业务结果为已是好友，不代表本次重新发送了添加通讯录邀请。",
    adviceTitle: "结果说明",
    flow: [
      ["任务已创建", "16:20 · 服务端"],
      ["任务已领取", "16:24 · Mac-02"],
      ["确认客户已是好友", "16:33 · Mac-02"],
    ],
    note: "客户与销售微信已存在好友关系。",
    actions: ["查看执行结果", "查看执行方", "补充备注"],
  },
  "TASK-1826": {
    status: "pending",
    basic: {
      任务类型: "添加通讯录邀请",
      执行状态: "待处理",
      业务结果: "-",
      异常原因: "-",
      当前步骤: "-",
      创建时间: "2026-06-04 15:40",
      更新时间: "2026-06-04 15:40",
    },
    object: {
      客户: "陈女士",
      手机号: "138****9518",
      微信号: "未填写",
      线索状态: "已分配",
      当前销售: "张伟",
    },
    executor: {
      执行方类型: "Worker",
      执行方: "待领取",
      执行方状态: "-",
      最近心跳: "-",
      领取时间: "-",
    },
    flow: [["任务已创建，等待执行方领取", "15:40 · 服务端"]],
    note: "等待可用 Worker 领取。",
    actions: ["取消任务", "补充备注"],
  },
  "TASK-1825": {
    status: "cancelled",
    basic: {
      任务类型: "添加通讯录邀请",
      执行状态: "已取消",
      业务结果: "-",
      异常原因: "运营取消任务",
      当前步骤: "-",
      创建时间: "2026-06-04 14:20",
      更新时间: "2026-06-04 14:32",
      取消时间: "2026-06-04 14:32",
    },
    object: {
      客户: "孙先生",
      手机号: "139****6217",
      微信号: "wx_sun_auto",
      线索状态: "已分配",
      当前销售: "李强",
    },
    executor: {
      执行方类型: "Worker",
      执行方: "未领取",
      执行方状态: "-",
      最近心跳: "-",
      领取时间: "-",
    },
    advice: "任务已取消，终态不可继续回传。如仍需处理，应重新创建新任务，原取消记录保留用于追溯。",
    adviceTitle: "取消说明",
    flow: [
      ["任务已创建", "14:20 · 服务端"],
      ["任务已取消", "14:32 · 运营小陈"],
    ],
    note: "运营判断该线索暂不需要发送添加通讯录邀请。",
    actions: ["查看取消信息", "重新创建任务", "补充备注"],
  },
  "TASK-1842": {
    status: "running",
    basic: {
      任务类型: "AI 回复",
      执行状态: "处理中",
      业务结果: "-",
      异常原因: "-",
      当前步骤: "等待服务端生成回复",
      创建时间: "2026-06-05 10:17",
      更新时间: "2026-06-05 10:20",
    },
    object: {
      客户: "林女士",
      手机号: "135****1086",
      线索状态: "已分配",
      当前销售: "张伟",
      客户最新消息: "请问续保价格大概是多少？",
    },
    executor: {
      执行方类型: "Worker",
      执行方: "Mac-01 展厅机",
      执行方状态: "在线 · 忙碌",
      最近心跳: "刚刚",
      领取时间: "2026-06-05 10:18",
    },
    flow: [
      ["任务已创建", "10:17 · 服务端"],
      ["读取客户最新消息", "10:18 · Mac-01"],
      ["等待服务端生成回复", "10:20 · 服务端"],
    ],
    note: "回复生成后将进入发送前复核。",
    actions: ["查看执行方", "取消任务", "补充备注"],
  },
  "TASK-1841": {
    status: "failed",
    basic: {
      任务类型: "AI 回复",
      执行状态: "失败",
      业务结果: "-",
      异常原因: "未能恢复客户会话上下文",
      当前步骤: "读取客户最新消息",
      创建时间: "2026-06-05 10:02",
      更新时间: "2026-06-05 10:08",
    },
    object: {
      客户: "陈先生",
      手机号: "137****6028",
      线索状态: "已分配",
      当前销售: "王敏",
    },
    executor: {
      执行方类型: "Worker",
      执行方: "Mac-02 客服机",
      执行方状态: "离线",
      最近心跳: "12 分钟前",
      领取时间: "2026-06-05 10:03",
    },
    advice: "未能确认目标客户会话，请核对销售绑定和客户识别信息后重新创建任务。",
    adviceTitle: "处理建议",
    flow: [
      ["任务已创建", "10:02 · 服务端"],
      ["读取客户最新消息", "10:03 · Mac-02"],
      ["读取失败：未能恢复客户会话上下文", "10:08 · Mac-02"],
    ],
    note: "未发送任何回复。",
    actions: ["重新创建任务", "查看执行方", "补充备注"],
  },
  "TASK-1840": {
    status: "completed",
    basic: {
      任务类型: "AI 回复",
      执行状态: "已完成",
      业务结果: "回复已发送",
      异常原因: "-",
      当前步骤: "已完成",
      创建时间: "2026-06-05 09:46",
      更新时间: "2026-06-05 09:53",
      完成时间: "2026-06-05 09:53",
    },
    object: {
      客户: "吴先生",
      手机号: "138****4196",
      线索状态: "已分配",
      当前销售: "张伟",
      客户最新消息: "明天下午方便到店看车吗？",
    },
    executor: {
      执行方类型: "Worker",
      执行方: "Mac-01 展厅机",
      执行方状态: "在线 · 空闲",
      最近心跳: "刚刚",
      领取时间: "2026-06-05 09:50",
    },
    advice: "回复已成功发送给客户。",
    adviceTitle: "结果说明",
    flow: [
      ["客户消息已入库", "09:46 · 服务端"],
      ["等待服务端生成回复", "09:47 · 服务端"],
      ["执行发送前复核", "09:49 · 服务端"],
      ["领取发送任务", "09:50 · Mac-01"],
      ["发送微信消息", "09:52 · Mac-01"],
      ["发送回执已记录", "09:53 · Mac-01"],
    ],
    note: "已按审核后的回复内容发送。",
    actions: ["查看执行结果", "查看执行方", "补充备注"],
  },
};

const escapeHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

const formatDate = (value) => {
  if (!value) return "暂无";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `${month}-${day} ${hours}:${minutes}`;
};

const showToast = (message, type = "info") => {
  if (!toast) return;
  toast.textContent = message;
  toast.dataset.type = type;
  toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    toast.hidden = true;
  }, 3200);
};

const getStatus = (status) => statusMeta[status] || { label: status || "未知", className: "unassigned" };

const setButtonBusy = async (button, task) => {
  if (!button) return task();
  const previousText = button.textContent;
  button.disabled = true;
  button.textContent = "处理中...";
  try {
    return await task();
  } finally {
    button.disabled = false;
    button.textContent = previousText;
  }
};

const apiJson = async (path, options = {}) => {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      ...OPERATOR_HEADERS,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok || (payload.code && payload.code !== "OK")) {
    const error = new Error(payload.message || "接口请求失败");
    error.code = payload.code;
    error.data = payload.data;
    throw error;
  }
  return payload.data;
};

const markApiUnavailable = (error) => {
  state.apiReady = false;
  console.warn("[ops-admin] API unavailable, static preview is kept.", error);
  restoreStaticPreview();
  showToast("后端服务未连接，当前保留静态预览。启动 backend 后可实际操作。", "warning");
};

const restoreStaticPreview = () => {
  const tbody = document.querySelector("[data-leads-table]");
  const total = document.querySelector("[data-total-count]");
  const pagination = document.querySelector("[data-pagination]");
  const selected = document.querySelector("[data-selected-count]");
  if (tbody && staticSnapshot.table) tbody.innerHTML = staticSnapshot.table;
  if (total) total.textContent = staticSnapshot.total;
  if (pagination && staticSnapshot.pagination) pagination.innerHTML = staticSnapshot.pagination;
  if (selected) selected.textContent = staticSnapshot.selectedText;
};

const buildLeadQuery = () => {
  const params = new URLSearchParams();
  const keyword = document.querySelector('[data-filter="keyword"]')?.value.trim();
  const status = document.querySelector('[data-filter="status"]')?.value;
  const salesId = document.querySelector('[data-filter="sales_id"]')?.value;
  state.pageSize = Number(document.querySelector("[data-page-size]")?.value || state.pageSize);
  if (keyword) params.set("keyword", keyword);
  if (status) params.set("status", status);
  if (salesId) params.set("sales_id", salesId);
  params.set("page", String(state.page));
  params.set("page_size", String(state.pageSize));
  return params.toString();
};

const renderStats = (stats) => {
  const mapping = {
    today_new_count: ["今日新增", "今日创建"],
    assigned_count: ["已分配", "轮询完成"],
    unassigned_count: ["未分配", "待处理"],
    duplicate_event_count: ["重复录入", "备注已追加"],
  };
  Object.entries(mapping).forEach(([key, [, desc]]) => {
    const card = document.querySelector(`[data-stat="${key}"]`);
    if (!card) return;
    card.querySelector("strong").textContent = stats?.[key] ?? 0;
    card.querySelector("p").textContent = `${desc} ${stats?.[key] ?? 0} 条`;
  });
};

const renderSalesFilter = () => {
  const select = document.querySelector('[data-filter="sales_id"]');
  if (!select) return;
  const current = select.value;
  select.innerHTML = `<option value="">全部销售</option>${state.sales
    .map((sales) => `<option value="${escapeHtml(sales.id)}">${escapeHtml(sales.sales_name)}</option>`)
    .join("")}`;
  select.value = current;
};

const renderLeads = (data) => {
  state.leads = data.items || [];
  state.total = data.total || 0;
  const tbody = document.querySelector("[data-leads-table]");
  const total = document.querySelector("[data-total-count]");
  if (total) total.textContent = state.total;
  if (!tbody) return;

  if (!state.leads.length) {
    tbody.innerHTML = `<tr><td colspan="8">暂无线索，调整筛选条件后重试。</td></tr>`;
    state.selectedIds.clear();
    updateSelectedCount();
    renderPagination();
    return;
  }

  tbody.innerHTML = state.leads
    .map((lead) => {
      const status = getStatus(lead.status);
      const checked = state.selectedIds.has(lead.id) ? "checked" : "";
      const contactSub = lead.primary_wechat_masked || "未填写微信";
      const duplicate =
        lead.duplicate_count > 0
          ? `<button class="link-button" type="button" data-action="open-detail" data-lead-id="${escapeHtml(lead.id)}">${lead.duplicate_count} 次</button>`
          : "0 次";

      return `
        <tr class="${checked ? "selected" : ""}" data-lead-id="${escapeHtml(lead.id)}">
          <td><input type="checkbox" ${checked} aria-label="选择${escapeHtml(lead.customer_name)}" data-select-lead="${escapeHtml(lead.id)}" /></td>
          <td class="lead-cell"><strong>${escapeHtml(lead.customer_name)}</strong></td>
          <td class="contact-cell"><strong>${escapeHtml(lead.primary_phone_masked || "未填写")}</strong><small>${escapeHtml(contactSub)}</small></td>
          <td><span class="status ${status.className}">${status.label}</span></td>
          <td>${escapeHtml(lead.sales_name || "暂无")}</td>
          <td>${duplicate}</td>
          <td>${escapeHtml(lead.remark_summary || "暂无备注")}</td>
          <td>${formatDate(lead.updated_at)}</td>
        </tr>`;
    })
    .join("");
  updateSelectedCount();
  renderPagination();
};

const renderPagination = () => {
  const nav = document.querySelector("[data-pagination]");
  if (!nav) return;
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  const pageButtons = [1, 2, 3].filter((page) => page <= pages);
  const middle = pages > 4 ? `<span>...</span><button type="button" data-page="${pages}">${pages}</button>` : "";
  nav.innerHTML = `
    <button type="button" data-page="${Math.max(1, state.page - 1)}" ${state.page <= 1 ? "disabled" : ""}>上一页</button>
    ${pageButtons
      .map((page) =>
        page === state.page ? `<strong>${page}</strong>` : `<button type="button" data-page="${page}">${page}</button>`
      )
      .join("")}
    ${middle}
    <button type="button" data-page="${Math.min(pages, state.page + 1)}" ${state.page >= pages ? "disabled" : ""}>下一页</button>`;
};

const updateSelectedCount = () => {
  const count = document.querySelector("[data-selected-count]");
  if (count) count.textContent = `已选 ${state.selectedIds.size} 条`;
};

const loadLeads = async () => {
  const tbody = document.querySelector("[data-leads-table]");
  if (tbody) tbody.innerHTML = `<tr><td colspan="8">正在加载线索...</td></tr>`;
  try {
    const [stats, salesData, leadsData] = await Promise.all([
      apiJson("/leads/stats"),
      apiJson("/sales"),
      apiJson(`/leads?${buildLeadQuery()}`),
    ]);
    state.apiReady = true;
    state.sales = salesData.items || [];
    renderStats(stats);
    renderSalesFilter();
    renderLeads(leadsData);
    state.leadsLoaded = true;
  } catch (error) {
    if (!state.leadsLoaded) markApiUnavailable(error);
    else showToast(error.message || "线索列表加载失败", "error");
  }
};

const loadLeadDetail = async (leadId) => {
  if (!leadId) return;
  if (!state.apiReady && staticLeadDetails[leadId]) {
    state.activeLeadId = leadId;
    state.activeLead = staticLeadDetails[leadId];
    renderLeadDetail(staticLeadDetails[leadId]);
    drawer?.classList.remove("closed");
    return;
  }
  try {
    const detail = await apiJson(`/leads/${leadId}`);
    state.activeLeadId = leadId;
    state.activeLead = detail;
    renderLeadDetail(detail);
    drawer?.classList.remove("closed");
  } catch (error) {
    showToast(error.message || "线索详情加载失败", "error");
  }
};

const renderLeadDetail = (detail) => {
  const status = getStatus(detail.status);
  document.querySelector("[data-detail-name]").textContent = detail.customer_name || "未命名客户";
  const statusEl = document.querySelector("[data-detail-status]");
  statusEl.textContent = status.label;
  statusEl.className = `status ${status.className}`;
  document.querySelector("[data-detail-phone]").textContent = detail.primary_phone_masked || "未填写";
  document.querySelector("[data-detail-sales]").textContent = detail.sales_name || "暂无";
  document.querySelector("[data-detail-created]").textContent = formatDate(detail.created_at);

  const contacts = document.querySelector("[data-detail-contacts]");
  if (contacts) {
    contacts.innerHTML = `<h3>联系方式</h3>${(detail.contacts || [])
      .map((contact) => {
        const typeLabel = { phone: "手机", wechat: "微信", email: "邮箱" }[contact.contact_type] || contact.contact_type;
        const button =
          contact.contact_type === "phone"
            ? `<button type="button" data-action="reveal-phone" data-contact-id="${escapeHtml(contact.id)}">查看完整</button>`
            : contact.contact_type === "wechat"
              ? `<button type="button" data-action="copy-contact" data-copy-value="${escapeHtml(contact.masked_value)}">复制</button>`
              : "";
        return `<div class="contact-row"><span>${typeLabel}</span><strong>${escapeHtml(contact.masked_value || "未填写")}</strong>${button}</div>`;
      })
      .join("")}`;
  }

  const flow = document.querySelector("[data-detail-flow]");
  if (flow) {
    const nodes = detail.task_nodes || [];
    flow.innerHTML = `<h3>任务链路</h3><ol class="flow-list">${nodes
      .map((node) => `<li><strong>${escapeHtml(node.label)}</strong><span>${formatDate(node.time)}</span></li>`)
      .join("")}</ol>`;
  }

  const actions = document.querySelector("[data-detail-actions]");
  if (actions) {
    actions.innerHTML =
      detail.status === "invalid"
        ? `<h3>操作</h3><button type="button" data-open-modal="restore">恢复有效</button>`
        : `<h3>操作</h3><button type="button" data-open-modal="invalid" data-invalid-mode="single">标记无效</button>`;
  }
};

const renderDefinitionList = (selector, values) => {
  const node = document.querySelector(selector);
  if (!node) return;
  node.innerHTML = Object.entries(values)
    .map(([label, value]) => {
      const safeLabel = escapeHtml(label);
      const safeValue = escapeHtml(value);
      const copyable = label === "异常原因" && value && value !== "-";
      return `<div><dt>${safeLabel}</dt><dd${copyable ? ' class="copyable-value"' : ""}>${copyable ? `<span title="${safeValue}">${safeValue}</span><button type="button" data-action="copy-value" data-copy-value="${safeValue}">复制</button>` : safeValue}</dd></div>`;
    })
    .join("");
};

const decorateTaskIdentifiers = () => {
  document.querySelectorAll(".task-table tbody tr[data-task-id]").forEach((row) => {
    const cell = row.querySelector("td:first-child");
    if (!cell || cell.querySelector(".table-copy")) return;
    const taskId = row.dataset.taskId || cell.textContent.trim();
    cell.innerHTML = `<span class="table-copy"><span>${escapeHtml(taskId)}</span><button type="button" data-action="copy-value" data-copy-value="${escapeHtml(taskId)}">复制</button></span>`;
  });
};

const renderTaskDetail = (taskId) => {
  const detail = staticTaskDetails[taskId] || staticTaskDetails["TASK-1831"];
  const status = taskStatusMeta[detail.status] || taskStatusMeta.pending;
  const drawerEl = document.querySelector("[data-task-drawer]");
  if (!drawerEl) return;

  document.querySelector("[data-task-title]").textContent = taskId;
  const statusEl = document.querySelector("[data-task-status]");
  statusEl.textContent = status.label;
  statusEl.className = `status ${status.className}`;
  renderDefinitionList("[data-task-basic]", detail.basic);
  renderDefinitionList("[data-task-object]", detail.object);
  renderDefinitionList("[data-task-executor]", detail.executor);

  const adviceWrap = document.querySelector("[data-task-advice-wrap]");
  const advice = document.querySelector("[data-task-advice]");
  const adviceTitle = document.querySelector("[data-task-advice-title]");
  if (adviceWrap && advice) {
    adviceWrap.hidden = !detail.advice;
    advice.textContent = detail.advice || "";
    if (adviceTitle) adviceTitle.textContent = detail.adviceTitle || "处理建议";
  }

  const flow = document.querySelector("[data-task-flow]");
  if (flow) {
    flow.innerHTML = detail.flow
      .map(([label, value]) => `<li><strong>${escapeHtml(label)}</strong><span>${escapeHtml(value)}</span></li>`)
      .join("");
  }

  const note = document.querySelector("[data-task-note]");
  if (note) note.textContent = detail.note || "暂无备注";

  const actions = document.querySelector("[data-task-actions]");
  if (actions) {
    const actionMap = {
      查看执行方: "task-jump-worker",
      去绑定Worker: "task-jump-sales",
      "去绑定 Worker": "task-jump-sales",
      标记线索无效: "task-mark-lead-invalid",
    };
    actions.innerHTML = detail.actions
      .map((label) => `<button type="button" data-action="${actionMap[label] || "task-static-action"}">${escapeHtml(label)}</button>`)
      .join("");
  }

  drawerEl.classList.remove("closed");
};

const showModule = async (moduleName) => {
  state.module = moduleName;
  window.scrollTo({ top: 0, behavior: "auto" });
  workspace.classList.remove("is-empty");
  emptyWorkspace.hidden = true;

  moduleButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.module === moduleName);
  });

  moduleContents.forEach((content) => {
    content.hidden = content.dataset.moduleContent !== moduleName;
  });

  document.querySelectorAll(".detail-drawer, .management-drawer").forEach((panel) => panel.classList.add("closed"));
  document.querySelectorAll(".management-table tbody tr, .task-table tbody tr").forEach((row) => row.classList.remove("selected"));

  if (moduleName === "leads" && !state.leadsLoaded) await loadLeads();
};

const setDrawerMode = (scope, mode) => {
  document.querySelectorAll(`[data-${scope}-mode]`).forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset[`${scope}Mode`] === mode);
  });
};

const setSalesEditing = (isEditing) => {
  document.querySelector("[data-sales-drawer]")?.classList.toggle("is-editing", isEditing);
};

const setWorkerEditing = (isEditing) => {
  document.querySelector("[data-worker-drawer]")?.classList.toggle("is-editing", isEditing);
};

const setVehicleEditing = (isEditing) => {
  state.vehicleEditing = isEditing;
  document.querySelector("[data-vehicle-drawer]")?.classList.toggle("is-editing", isEditing);
};

const syncVehicleMainImage = (src) => {
  const main = document.querySelector("[data-vehicle-main-image]");
  const preview = document.querySelector("[data-vehicle-preview-image]");
  if (main && src) main.src = src;
  if (preview && src) preview.src = src;
};

const openVehicleDrawer = (row, editing = false) => {
  const drawerEl = document.querySelector("[data-vehicle-drawer]");
  if (!drawerEl) return;
  const title = row?.querySelector(".lead-cell strong")?.textContent || "新增车辆";
  const image = row?.querySelector("img")?.getAttribute("src") || "./assets/vehicles/vehicle-01.jpg";
  const status = row?.dataset.vehicleStatus || "unlisted";
  const shelfButton = drawerEl.querySelector("[data-vehicle-shelf-button]");
  drawerEl.querySelector("[data-vehicle-drawer-title]").textContent = title;
  syncVehicleMainImage(image);
  shelfButton.dataset.currentStatus = status;
  shelfButton.dataset.incomplete = "false";
  shelfButton.textContent = status === "listed" ? "下架" : "上架";
  const validation = drawerEl.querySelector("[data-vehicle-list-validation]");
  if (validation) validation.hidden = true;
  drawerEl.querySelectorAll("[data-gallery-state]").forEach((panel) => {
    panel.hidden = true;
  });
  const uploadAlert = drawerEl.querySelector("[data-image-upload-alert]");
  if (uploadAlert) uploadAlert.hidden = true;
  drawerEl.querySelectorAll("[data-image-src]").forEach((button) => button.classList.remove("is-dragging"));
  drawerEl.classList.remove("closed");
  setVehicleEditing(editing);
};

const applyVehicleListState = (mode = "default") => {
  const table = document.querySelector(".vehicle-table");
  const pagination = document.querySelector(".vehicle-pagination");
  document.querySelectorAll("[data-vehicle-state]").forEach((panel) => {
    panel.hidden = panel.dataset.vehicleState !== mode;
  });
  const special = ["loading", "empty", "no-results", "error"].includes(mode);
  if (table) table.hidden = special;
  if (pagination) pagination.hidden = mode === "loading" || mode === "empty" || mode === "error";
};

const filterVehicles = () => {
  const keyword = document.querySelector("[data-vehicle-search]")?.value.trim().toLowerCase() || "";
  const status = document.querySelector("[data-vehicle-status-filter]")?.value || "all";
  let visible = 0;
  document.querySelectorAll("tr[data-vehicle-id]").forEach((row) => {
    const matchedKeyword = !keyword || row.textContent.toLowerCase().includes(keyword);
    const matchedStatus = status === "all" || row.dataset.vehicleStatus === status;
    row.hidden = !(matchedKeyword && matchedStatus);
    if (!row.hidden) visible += 1;
  });
  applyVehicleListState(visible ? "default" : "no-results");
};

const setKnowledgeMode = (mode) => {
  document.querySelectorAll("[data-knowledge-mode]").forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.knowledgeMode === mode);
  });
};

const setKnowledgeEditing = (isEditing) => {
  state.knowledgeEditing = isEditing;
  document.querySelector("[data-knowledge-drawer]")?.classList.toggle("is-editing", isEditing);
};

const syncKnowledgeDrawer = (id) => {
  const item = knowledgeItems[id];
  const drawerEl = document.querySelector("[data-knowledge-drawer]");
  if (!item || !drawerEl) return;
  state.activeKnowledgeId = id;
  setKnowledgeMode("detail");
  setKnowledgeEditing(false);
  drawerEl.querySelector("[data-knowledge-drawer-title]").textContent = item.title;
  drawerEl.querySelector("[data-knowledge-drawer-content]").textContent = item.content;
  drawerEl.querySelector("[data-knowledge-drawer-version]").textContent = item.version;
  drawerEl.querySelector("[data-knowledge-drawer-editor]").textContent = item.editor;
  drawerEl.querySelector("[data-knowledge-drawer-time]").textContent = item.updated;
  drawerEl.querySelector("[data-knowledge-edit-title]").value = item.title;
  drawerEl.querySelector("[data-knowledge-edit-content]").value = item.content;
  const status = drawerEl.querySelector("[data-knowledge-drawer-status]");
  status.textContent = item.status === "published" ? "已发布" : "已归档";
  status.className = `status ${item.status === "published" ? "assigned" : "unassigned"}`;
  drawerEl.querySelector('[data-action="archive-knowledge"]').hidden = item.status !== "published";
};

const openKnowledgeDrawer = (id) => {
  syncKnowledgeDrawer(id);
  document.querySelector("[data-knowledge-drawer]")?.classList.remove("closed");
};

const filterKnowledge = () => {
  const keyword = document.querySelector("[data-knowledge-search]")?.value.trim().toLowerCase() || "";
  const status = document.querySelector("[data-knowledge-status-filter]")?.value || "all";
  let visible = 0;
  document.querySelectorAll("tr[data-knowledge-id]").forEach((row) => {
    const item = knowledgeItems[row.dataset.knowledgeId];
    const matchedKeyword = !keyword || `${item.title} ${item.content}`.toLowerCase().includes(keyword);
    const matchedStatus = status === "all" || item.status === status;
    row.hidden = !(matchedKeyword && matchedStatus);
    if (!row.hidden) visible += 1;
  });
  const table = document.querySelector(".knowledge-table");
  const empty = document.querySelector('[data-knowledge-state="no-results"]');
  if (table) table.hidden = visible === 0;
  if (empty) empty.hidden = visible !== 0;
};

const prepareKnowledgePublishPreview = (mode) => {
  const item = knowledgeItems[state.activeKnowledgeId];
  const modal = document.querySelector('[data-modal="knowledge-publish-preview"]');
  if (!modal) return;
  state.knowledgePublishMode = mode;
  let title = item?.title || "新增知识";
  let before = item?.content || "当前线上版本中不存在该知识。";
  let after = document.querySelector("[data-knowledge-edit-content]")?.value.trim() || "";
  let changeType = "修改 1 条";
  let heading = `${title} · 修改前后差异`;

  if (mode === "create") {
    title = document.querySelector("[data-new-knowledge-title]")?.value.trim() || "";
    after = document.querySelector("[data-new-knowledge-content]")?.value.trim() || "";
    before = "当前线上版本中不存在该知识。";
    changeType = "新增 1 条";
    heading = `${title || "新增知识"} · 新增内容`;
  }

  if (mode === "archive") {
    after = "归档后，该知识将从新的线上版本中移除，历史内容和发布记录继续保留。";
    changeType = "归档 1 条";
    heading = `${title} · 归档差异`;
  }

  if (mode === "rollback") {
    title = "回滚知识版本";
    before = "当前线上版本：KR-20260903-12";
    after = "目标历史内容：KR-20260902-11。回滚成功后将生成一个新的发布版本。";
    changeType = "回滚版本";
    heading = "当前版本与目标版本差异";
  }

  modal.querySelector("[data-knowledge-preview-title]").textContent = mode === "archive" ? "确认归档知识" : mode === "rollback" ? "确认回滚知识版本" : "确认发布知识";
  modal.querySelector("[data-knowledge-change-type]").textContent = changeType;
  modal.querySelector("[data-knowledge-diff-heading]").textContent = heading;
  modal.querySelector("[data-knowledge-before-content]").textContent = before;
  modal.querySelector("[data-knowledge-after-content]").textContent = after;
  modal.querySelector("[data-knowledge-publish-confirm]").textContent = mode === "archive" ? "确认归档并发布" : mode === "rollback" ? "确认回滚" : "确认发布";
  openModal("knowledge-publish-preview");
};

const applyKnowledgeScenario = (scenario) => {
  const firstRow = document.querySelector('tr[data-knowledge-id="pricing-rule"]');
  if (scenario === "detail" || scenario === "edit" || scenario === "archive-preview") {
    firstRow?.classList.add("selected");
    openKnowledgeDrawer("pricing-rule");
  }
  if (scenario === "edit") setKnowledgeEditing(true);
  if (scenario === "create") openModal("knowledge-create");
  if (scenario === "publish-preview") {
    setKnowledgeEditing(true);
    prepareKnowledgePublishPreview("edit");
  }
  if (scenario === "archive-preview") prepareKnowledgePublishPreview("archive");
  if (scenario === "releases") {
    setKnowledgeMode("releases");
    document.querySelector("[data-knowledge-drawer]")?.classList.remove("closed");
  }
};

const setVehicleImportStep = (step) => {
  state.vehicleImportStep = step;
  document.querySelectorAll("[data-import-stage]").forEach((stage) => {
    stage.classList.toggle("is-active", Number(stage.dataset.importStage) === step);
  });
  document.querySelectorAll("[data-import-step-indicator]").forEach((indicator) => {
    const index = Number(indicator.dataset.importStepIndicator);
    indicator.classList.toggle("is-active", index === step);
    indicator.classList.toggle("is-complete", index < step);
  });
  const cancel = document.querySelector("[data-import-cancel]");
  const back = document.querySelector("[data-import-back]");
  const confirm = document.querySelector("[data-import-confirm]");
  const finish = document.querySelector("[data-import-finish]");
  if (cancel) cancel.hidden = step === 3;
  if (back) back.hidden = step !== 2;
  if (confirm) {
    confirm.hidden = step !== 2;
    confirm.disabled = state.vehicleImportPreview === "invalid";
  }
  if (finish) finish.hidden = step !== 3;
};

const setVehicleImportPreview = (mode) => {
  state.vehicleImportPreview = mode;
  document.querySelectorAll("[data-import-preview]").forEach((panel) => {
    panel.hidden = panel.dataset.importPreview !== mode;
  });
  const errorCount = document.querySelector("[data-import-error-count]");
  if (errorCount) errorCount.textContent = mode === "invalid" ? "3" : "0";
  setVehicleImportStep(2);
};

const setVehicleImportResult = (mode) => {
  state.vehicleImportResult = mode;
  document.querySelectorAll("[data-import-result]").forEach((panel) => {
    panel.hidden = panel.dataset.importResult !== mode;
  });
  setVehicleImportStep(3);
};

const resetVehicleImport = () => {
  state.vehicleImportPreview = "valid";
  state.vehicleImportResult = "success";
  setVehicleImportStep(1);
};

const closeModal = (modal) => {
  const backdrop = modal?.closest(".modal-backdrop") || modal;
  if (backdrop) backdrop.hidden = true;
};

const openModal = (name) => {
  if (name === "vehicle-import") resetVehicleImport();
  if (name === "knowledge-create") {
    const title = document.querySelector("[data-new-knowledge-title]");
    const content = document.querySelector("[data-new-knowledge-content]");
    if (title) title.value = "";
    if (content) content.value = "";
  }
  if (name === "vehicle-image-delete") {
    const copy = document.querySelector("[data-vehicle-image-delete-copy]");
    const confirm = document.querySelector("[data-vehicle-image-delete-confirm]");
    if (copy) copy.textContent = "删除后无法恢复，是否确认删除当前图片？";
    if (confirm) confirm.disabled = false;
  }
  const modal = document.querySelector(`[data-modal="${name}"]`);
  if (modal) modal.hidden = false;
};

const resetLeadForm = () => {
  ["customer_name", "phone", "wechat", "email", "remark"].forEach((field) => {
    const input = document.querySelector(`[data-lead-field="${field}"]`);
    if (input) input.value = "";
  });
  document.querySelectorAll("[data-custom-key], [data-custom-value]").forEach((input) => {
    input.value = "";
  });
  const duplicateNote = document.querySelector("[data-duplicate-note]");
  if (duplicateNote) duplicateNote.hidden = true;
};

const collectLeadPayload = () => {
  const customerName = document.querySelector('[data-lead-field="customer_name"]').value.trim();
  const remark = document.querySelector('[data-lead-field="remark"]').value.trim();
  const phone = document.querySelector('[data-lead-field="phone"]')?.value.trim();
  const wechat = document.querySelector('[data-lead-field="wechat"]')?.value.trim();
  const email = document.querySelector('[data-lead-field="email"]')?.value.trim();
  const contacts = {
    phones: phone ? [phone] : [],
    wechats: wechat ? [wechat] : [],
    emails: email ? [email] : [],
  };

  const customFields = {};
  document.querySelectorAll(".custom-field-row").forEach((row) => {
    const key = row.querySelector("[data-custom-key]")?.value.trim();
    const value = row.querySelector("[data-custom-value]")?.value.trim();
    if (key && value) customFields[key] = value;
  });

  if (!customerName) throw new Error("请填写客户名称");
  if (!contacts.phones.length) throw new Error("请至少填写一个手机号");
  return { customer_name: customerName, ...contacts, remark, custom_fields: customFields };
};

const addCustomField = () => {
  const list = document.querySelector('[data-modal="lead"] .custom-field-list');
  if (!list) return;
  const index = list.querySelectorAll(".custom-field-row").length + 1;
  const row = document.createElement("div");
  row.className = "custom-field-row";
  row.innerHTML = `
    <label>
      <span>字段名称</span>
      <input type="text" aria-label="自定义字段名称 ${index}" data-custom-key />
    </label>
    <label>
      <span>字段内容</span>
      <input type="text" aria-label="自定义字段内容 ${index}" data-custom-value />
    </label>
    <button class="ghost-button" type="button" data-action="remove-custom-field">删除</button>`;
  list.appendChild(row);
};

const removeCustomField = (button) => {
  button.closest(".custom-field-row")?.remove();
};

const saveLead = async (shouldContinue, button) => {
  await setButtonBusy(button, async () => {
    const payload = collectLeadPayload();
    try {
      const data = await apiJson("/leads", { method: "POST", body: JSON.stringify(payload) });
      showToast("客户线索已新增", "success");
      state.page = 1;
      state.leadsLoaded = false;
      await loadLeads();
      await loadLeadDetail(data.id || data.lead?.id);
      if (!shouldContinue) closeModal(document.querySelector('[data-modal="lead"]'));
      if (shouldContinue) resetLeadForm();
    } catch (error) {
      if (error.code === "LEAD_PHONE_DUPLICATED") {
        const note = document.querySelector("[data-duplicate-note]");
        if (note) {
          note.hidden = false;
          const text = note.querySelector("[data-duplicate-summary]");
          if (text) text.textContent = error.message;
        }
      }
      showToast(error.message || "保存失败", "error");
    }
  });
};

const duplicatePreview = async () => {
  const phone = document.querySelector('[data-lead-field="phone"]')?.value.trim();
  const phones = phone ? [phone] : [];
  if (!phones.length) return;
  try {
    const data = await apiJson("/leads/duplicate-preview", { method: "POST", body: JSON.stringify({ phones }) });
    const hit = (data.items || []).find((item) => item.duplicated);
    const note = document.querySelector("[data-duplicate-note]");
    if (note) {
      note.hidden = !hit;
      const text = note.querySelector("[data-duplicate-summary]");
      if (text) text.textContent = hit
        ? `发现重复手机号：${hit.phone_masked} · 已重复录入 ${hit.duplicate_count ?? 3} 次 · 最近重复日期 ${formatDate(hit.latest_duplicate_at || hit.updated_at || "2026-06-02T14:30:00")}`
        : "";
    }
  } catch (error) {
    console.warn("[ops-admin] duplicate preview failed", error);
  }
};

const confirmInvalid = async (button) => {
  const leadIds = state.invalidMode === "batch" ? [...state.selectedIds] : [state.activeLeadId].filter(Boolean);
  if (!leadIds.length) {
    showToast("请先选择线索", "warning");
    return;
  }
  const payload = {
    invalid_reason: document.querySelector("[data-invalid-reason]").value,
    invalid_remark: document.querySelector("[data-invalid-remark]").value.trim(),
  };
  await setButtonBusy(button, async () => {
    try {
      if (leadIds.length > 1 || state.invalidMode === "batch") {
        await apiJson("/leads/batch-mark-invalid", {
          method: "POST",
          body: JSON.stringify({ ...payload, lead_ids: leadIds }),
        });
      } else {
        await apiJson(`/leads/${leadIds[0]}/mark-invalid`, { method: "POST", body: JSON.stringify(payload) });
      }
      showToast("已标记为无效线索", "success");
      state.selectedIds.clear();
      closeModal(document.querySelector('[data-modal="invalid"]'));
      await loadLeads();
    } catch (error) {
      showToast(error.message || "标记失败", "error");
    }
  });
};

const confirmRestore = async (button) => {
  const leadId = state.pendingRestoreLeadId || state.activeLeadId;
  if (!leadId) return;
  await setButtonBusy(button, async () => {
    try {
      await apiJson(`/leads/${leadId}/restore`, { method: "POST" });
      showToast("已恢复为有效线索", "success");
      closeModal(document.querySelector('[data-modal="restore"]'));
      await loadLeads();
    } catch (error) {
      showToast(error.message || "恢复失败", "error");
    }
  });
};

const retryAssign = async (button) => {
  const leadIds = state.selectedIds.size ? [...state.selectedIds] : state.leads.filter((lead) => lead.status === "unassigned").map((lead) => lead.id);
  if (!leadIds.length) {
    showToast("请选择未分配线索，或在当前页保留未分配线索后重试。", "warning");
    return;
  }
  await setButtonBusy(button, async () => {
    try {
      await apiJson("/leads/retry-auto-assign", { method: "POST", body: JSON.stringify({ lead_ids: leadIds }) });
      showToast("已触发重新分配", "success");
      await loadLeads();
    } catch (error) {
      showToast(error.message || "重新分配失败", "error");
    }
  });
};

const exportSelected = async (button) => {
  const leadIds = [...state.selectedIds];
  if (!leadIds.length) {
    showToast("请选择要导出的线索", "warning");
    return;
  }
  await setButtonBusy(button, async () => {
    try {
      const response = await fetch(`${API_BASE}/leads/export`, {
        method: "POST",
        headers: { ...OPERATOR_HEADERS, "Content-Type": "application/json" },
        body: JSON.stringify({ lead_ids: leadIds, fields: [] }),
      });
      if (!response.ok) throw new Error("导出失败");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `leads_export_${Date.now()}.csv`;
      link.click();
      URL.revokeObjectURL(url);
      showToast("已导出选中线索", "success");
    } catch (error) {
      showToast(error.message || "导出失败", "error");
    }
  });
};

const revealPhone = async (contactId, button) => {
  const reason = window.prompt("请输入查看手机号明文的原因", "电话确认到店时间");
  if (!reason) return;
  await setButtonBusy(button, async () => {
    try {
      const data = await apiJson(`/leads/${state.activeLeadId}/contacts/${contactId}/reveal`, {
        method: "POST",
        body: JSON.stringify({ reason }),
      });
      button.closest(".contact-row").querySelector("strong").textContent = data.value;
      showToast("手机号明文已显示，并已写入审计日志", "success");
    } catch (error) {
      showToast(error.message || "查看失败", "error");
    }
  });
};

moduleButtons.forEach((button) => {
  button.addEventListener("click", () => showModule(button.dataset.module));
});

let filterTimer;
document.addEventListener("input", (event) => {
  if (event.target.matches("[data-vehicle-search]")) {
    filterVehicles();
  }
  if (event.target.matches("[data-knowledge-search]")) {
    filterKnowledge();
  }
  if (event.target.matches("[data-filter]")) {
    window.clearTimeout(filterTimer);
    filterTimer = window.setTimeout(() => {
      state.page = 1;
      loadLeads();
    }, 350);
  }
});

document.addEventListener(
  "change",
  (event) => {
    if (event.target.matches("[data-vehicle-status-filter]")) {
      filterVehicles();
    }
    if (event.target.matches("[data-knowledge-status-filter]")) {
      filterKnowledge();
    }
    if (event.target.matches("[data-filter], [data-page-size]")) {
      state.page = 1;
      loadLeads();
    }
    if (event.target.matches("[data-select-lead]")) {
      const id = event.target.dataset.selectLead;
      if (event.target.checked) state.selectedIds.add(id);
      else state.selectedIds.delete(id);
      event.target.closest("tr")?.classList.toggle("selected", event.target.checked);
      updateSelectedCount();
    }
    if (event.target.matches("[data-select-all-leads]")) {
      if (event.target.checked) {
        state.leads.forEach((lead) => state.selectedIds.add(lead.id));
      } else {
        state.selectedIds.clear();
      }
      document.querySelectorAll("[data-select-lead]").forEach((input) => {
        input.checked = event.target.checked;
        input.closest("tr")?.classList.toggle("selected", event.target.checked);
      });
      updateSelectedCount();
    }
    if (event.target.matches('[data-lead-field="phone"]')) {
      duplicatePreview();
    }
  },
  true
);

document.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : event.target.parentElement;
  if (!target) return;
  const actionEl = target.closest("[data-action]");
  const openModalButton = target.closest("[data-open-modal]");
  const leadRow = target.closest("tr[data-lead-id]");
  const taskRow = target.closest("tr[data-task-id]");
  const vehicleRow = target.closest("tr[data-vehicle-id]");
  const knowledgeRow = target.closest("tr[data-knowledge-id]");
  const vehicleImageButton = target.closest("[data-image-src]");

  if (openModalButton) {
    const modalName = openModalButton.dataset.openModal;
    if (modalName === "lead" && openModalButton.dataset.action === "reset-lead-form") resetLeadForm();
    if (modalName === "invalid") state.invalidMode = openModalButton.dataset.invalidMode || "single";
    if (modalName === "restore") state.pendingRestoreLeadId = openModalButton.dataset.leadId || state.activeLeadId;
    openModal(modalName);
  }

  if (target.closest("[data-close-modal]")) {
    closeModal(target.closest(".modal-backdrop"));
  }

  if (target.closest("[data-close-drawer]")) {
    target.closest(".detail-drawer, .management-drawer")?.classList.add("closed");
  }

  if (target.classList.contains("modal-backdrop")) {
    target.hidden = true;
  }

  if (target.matches("[data-page]")) {
    state.page = Number(target.dataset.page);
    loadLeads();
  }

  if (leadRow && !target.closest("button, input, select, textarea, a")) {
    loadLeadDetail(leadRow.dataset.leadId);
  }

  if (taskRow && !target.closest("button, input, select, textarea, a")) {
    taskRow.parentElement?.querySelectorAll("tr").forEach((row) => row.classList.toggle("selected", row === taskRow));
    renderTaskDetail(taskRow.dataset.taskId);
  }

  if (vehicleRow && !target.closest("button, input, select, textarea, a")) {
    vehicleRow.parentElement?.querySelectorAll("tr").forEach((row) => row.classList.toggle("selected", row === vehicleRow));
    openVehicleDrawer(vehicleRow);
  }

  if (knowledgeRow && !target.closest("button, input, select, textarea, a")) {
    knowledgeRow.parentElement?.querySelectorAll("tr").forEach((row) => row.classList.toggle("selected", row === knowledgeRow));
    openKnowledgeDrawer(knowledgeRow.dataset.knowledgeId);
  }

  if (vehicleImageButton) {
    document.querySelectorAll("[data-image-src]").forEach((button) => button.classList.toggle("is-current", button === vehicleImageButton));
    syncVehicleMainImage(vehicleImageButton.dataset.imageSrc);
  }

  if (target.closest("[data-vehicle-main-image]")) {
    syncVehicleMainImage(target.closest("[data-vehicle-main-image]").getAttribute("src"));
    openModal("vehicle-image-preview");
  }

  if (!target.closest(".account-area") && accountMenu && !accountMenu.hidden) {
    accountMenu.hidden = true;
    accountButton?.setAttribute("aria-expanded", "false");
  }

  if (!actionEl) return;
  const action = actionEl.dataset.action;
  const managementRow = actionEl.closest(".management-table tbody tr");
  if (managementRow) {
    managementRow.parentElement?.querySelectorAll("tr").forEach((row) => row.classList.toggle("selected", row === managementRow));
  }
  if (action === "logout") {
    setAuthenticatedView(false);
    setLoginMessage("login");
  }
  if (action === "open-detail") loadLeadDetail(actionEl.dataset.leadId);
  if (action === "save-lead") saveLead(false, actionEl);
  if (action === "save-lead-continue") saveLead(true, actionEl);
  if (action === "add-custom-field") addCustomField();
  if (action === "remove-custom-field") removeCustomField(actionEl);
  if (action === "confirm-invalid") confirmInvalid(actionEl);
  if (action === "confirm-restore") confirmRestore(actionEl);
  if (action === "retry-assign") retryAssign(actionEl);
  if (action === "export-selected") exportSelected(actionEl);
  if (action === "reveal-phone") revealPhone(actionEl.dataset.contactId, actionEl);
  if (action === "copy-contact") {
    navigator.clipboard?.writeText(actionEl.dataset.copyValue || "");
    showToast("已复制", "success");
  }
  if (action === "copy-value") {
    navigator.clipboard?.writeText(actionEl.dataset.copyValue || "");
    showToast("已复制", "success");
  }
  if (action === "copy-task-id") {
    navigator.clipboard?.writeText(document.querySelector("[data-task-title]")?.textContent || "");
    showToast("任务 ID 已复制", "success");
  }
  if (action === "show-sales-detail") {
    document.querySelector("[data-sales-drawer]")?.classList.remove("closed");
    setSalesEditing(false);
  }
  if (action === "show-sales-edit") setSalesEditing(true);
  if (action === "close-sales-drawer") document.querySelector("[data-sales-drawer]")?.classList.add("closed");
  if (action === "show-worker-detail") {
    document.querySelector("[data-worker-drawer]")?.classList.remove("closed");
    setDrawerMode("worker", "detail");
    setWorkerEditing(false);
  }
  if (action === "show-worker-edit") setWorkerEditing(true);
  if (action === "close-worker-drawer") document.querySelector("[data-worker-drawer]")?.classList.add("closed");
  if (action === "close-task-drawer") document.querySelector("[data-task-drawer]")?.classList.add("closed");
  if (action === "open-knowledge-releases") {
    setKnowledgeEditing(false);
    setKnowledgeMode("releases");
    document.querySelector("[data-knowledge-drawer]")?.classList.remove("closed");
  }
  if (action === "close-knowledge-drawer") {
    if (state.knowledgeEditing) {
      state.pendingKnowledgeClose = true;
      openModal("knowledge-unsaved");
    } else {
      document.querySelector("[data-knowledge-drawer]")?.classList.add("closed");
    }
  }
  if (action === "edit-knowledge") setKnowledgeEditing(true);
  if (action === "cancel-knowledge-edit") {
    state.pendingKnowledgeClose = false;
    openModal("knowledge-unsaved");
  }
  if (action === "discard-knowledge-edit") {
    closeModal(actionEl.closest(".modal-backdrop"));
    setKnowledgeEditing(false);
    syncKnowledgeDrawer(state.activeKnowledgeId);
    if (state.pendingKnowledgeClose) document.querySelector("[data-knowledge-drawer]")?.classList.add("closed");
    state.pendingKnowledgeClose = false;
  }
  if (action === "preview-knowledge-publish") {
    const title = document.querySelector("[data-knowledge-edit-title]")?.value.trim();
    const content = document.querySelector("[data-knowledge-edit-content]")?.value.trim();
    if (!title || !content) showToast("请填写标题和规则正文", "error");
    else prepareKnowledgePublishPreview("edit");
  }
  if (action === "preview-new-knowledge") {
    const title = document.querySelector("[data-new-knowledge-title]")?.value.trim();
    const content = document.querySelector("[data-new-knowledge-content]")?.value.trim();
    if (!title || !content) showToast("请填写标题和规则正文", "error");
    else prepareKnowledgePublishPreview("create");
  }
  if (action === "archive-knowledge") prepareKnowledgePublishPreview("archive");
  if (action === "confirm-knowledge-publish") {
    const mode = state.knowledgePublishMode;
    const item = knowledgeItems[state.activeKnowledgeId];
    if (mode === "edit" && item) {
      item.title = document.querySelector("[data-knowledge-edit-title]")?.value.trim() || item.title;
      item.content = document.querySelector("[data-knowledge-edit-content]")?.value.trim() || item.content;
      item.updated = "刚刚";
      const row = document.querySelector(`tr[data-knowledge-id="${state.activeKnowledgeId}"]`);
      if (row) {
        row.querySelector(".lead-cell strong").textContent = item.title;
        row.lastElementChild.textContent = "刚刚";
      }
      syncKnowledgeDrawer(state.activeKnowledgeId);
    }
    if (mode === "archive" && item) {
      item.status = "archived";
      item.updated = "刚刚";
      const row = document.querySelector(`tr[data-knowledge-id="${state.activeKnowledgeId}"]`);
      if (row) {
        row.dataset.knowledgeStatus = "archived";
        const badge = row.querySelector(".status");
        badge.className = "status unassigned";
        badge.textContent = "已归档";
        row.lastElementChild.textContent = "刚刚";
      }
      syncKnowledgeDrawer(state.activeKnowledgeId);
    }
    closeModal(actionEl.closest(".modal-backdrop"));
    document.querySelector('[data-modal="knowledge-create"]')?.setAttribute("hidden", "");
    setKnowledgeEditing(false);
    filterKnowledge();
    showToast(mode === "archive" ? "知识已归档，新线上版本已发布" : mode === "rollback" ? "已回滚并生成新的知识版本" : "知识发布成功，新创建的 AI 对话批次将使用此版本", "success");
  }
  if (action === "clear-knowledge-filter") {
    const keyword = document.querySelector("[data-knowledge-search]");
    const status = document.querySelector("[data-knowledge-status-filter]");
    if (keyword) keyword.value = "";
    if (status) status.value = "all";
    filterKnowledge();
  }
  if (action === "show-release-detail") {
    document.querySelector("[data-release-detail-title]").textContent = actionEl.dataset.release || "知识发布记录";
    openModal("knowledge-release-detail");
  }
  if (action === "preview-knowledge-rollback") {
    closeModal(actionEl.closest(".modal-backdrop"));
    prepareKnowledgePublishPreview("rollback");
  }
  if (action === "save-worker-design") {
    closeModal(actionEl.closest(".modal-backdrop"));
    document.querySelector("[data-worker-drawer]")?.classList.remove("closed");
    setWorkerEditing(false);
    showToast("Worker 已创建，ID 和 Token 已生成", "success");
  }
  if (action === "show-vehicle-edit") setVehicleEditing(true);
  if (action === "cancel-vehicle-edit") {
    state.pendingVehicleClose = false;
    openModal("vehicle-unsaved");
  }
  if (action === "close-vehicle-drawer") {
    if (state.vehicleEditing) {
      state.pendingVehicleClose = true;
      openModal("vehicle-unsaved");
    } else {
      document.querySelector("[data-vehicle-drawer]")?.classList.add("closed");
    }
  }
  if (action === "discard-vehicle-edit") {
    closeModal(actionEl.closest(".modal-backdrop"));
    setVehicleEditing(false);
    if (state.pendingVehicleClose) document.querySelector("[data-vehicle-drawer]")?.classList.add("closed");
    state.pendingVehicleClose = false;
  }
  if (action === "save-vehicle-edit") {
    setVehicleEditing(false);
    showToast("车辆资料已保存", "success");
  }
  if (action === "save-new-vehicle") {
    const name = document.querySelector("[data-new-vehicle-name]")?.value.trim();
    if (!name) {
      showToast("请填写车辆展示名称", "error");
    } else {
      closeModal(actionEl.closest(".modal-backdrop"));
      openVehicleDrawer(null, true);
      document.querySelector("[data-vehicle-drawer-title]").textContent = name;
      const systemId = document.querySelector("[data-vehicle-system-id]");
      const emptyGallery = document.querySelector('[data-gallery-state="empty"]');
      if (systemId) systemId.textContent = "VEH-202608-0127";
      if (emptyGallery) emptyGallery.hidden = false;
      showToast("车辆已创建，编号已生成，请继续补充资料", "success");
    }
  }
  if (action === "toggle-vehicle-shelf") {
    if (actionEl.dataset.currentStatus === "listed") {
      openModal("vehicle-unlist");
    } else if (actionEl.dataset.incomplete === "true") {
      const validation = document.querySelector("[data-vehicle-list-validation]");
      if (validation) validation.hidden = false;
    } else {
      openModal("vehicle-list-confirm");
    }
  }
  if (action === "confirm-vehicle-list") {
    closeModal(actionEl.closest(".modal-backdrop"));
    const button = document.querySelector("[data-vehicle-shelf-button]");
    button.dataset.currentStatus = "listed";
    button.textContent = "下架";
    const status = document.querySelector("[data-vehicle-system-status]");
    if (status) {
      status.className = "status assigned";
      status.textContent = "已上架";
    }
    showToast("车辆已上架", "success");
  }
  if (action === "confirm-vehicle-unlist") {
    closeModal(actionEl.closest(".modal-backdrop"));
    const button = document.querySelector("[data-vehicle-shelf-button]");
    button.dataset.currentStatus = "unlisted";
    button.textContent = "上架";
    const status = document.querySelector("[data-vehicle-system-status]");
    if (status) {
      status.className = "status unassigned";
      status.textContent = "已下架";
    }
    showToast("车辆已下架", "success");
  }
  if (action === "simulate-image-upload") {
    const alert = document.querySelector("[data-image-upload-alert]");
    if (alert) alert.hidden = false;
  }
  if (action === "select-vehicle-import") {
    const mode = new URLSearchParams(window.location.search).get("importPreview") === "invalid" ? "invalid" : "valid";
    setVehicleImportPreview(mode);
  }
  if (action === "back-import-step") setVehicleImportStep(1);
  if (action === "confirm-vehicle-import") {
    const mode = new URLSearchParams(window.location.search).get("importResult") === "failed" ? "failed" : "success";
    setVehicleImportResult(mode);
  }
  if (action === "finish-vehicle-import") {
    closeModal(actionEl.closest(".modal-backdrop"));
    showToast("已返回车辆列表", "success");
  }
  if (action === "download-vehicle-template") showToast("已下载最新车辆导入模板", "success");
  if (action === "clear-vehicle-filter") {
    const keyword = document.querySelector("[data-vehicle-search]");
    const status = document.querySelector("[data-vehicle-status-filter]");
    if (keyword) keyword.value = "";
    if (status) status.value = "all";
    filterVehicles();
  }
  if (action === "retry-vehicle-list") {
    applyVehicleListState("loading");
    window.setTimeout(() => applyVehicleListState("default"), 600);
  }
  if (action === "show-log-detail") {
    document.querySelector("[data-log-drawer]")?.classList.remove("closed");
  }
  if (action === "task-jump-worker") {
    showModule("workers").then(() => {
      document.querySelector("[data-worker-drawer]")?.classList.remove("closed");
      setWorkerEditing(false);
    });
  }
  if (action === "task-jump-sales") {
    showModule("sales").then(() => {
      const salesDrawer = document.querySelector("[data-sales-drawer]");
      salesDrawer?.classList.remove("closed");
      setSalesEditing(true);
      salesDrawer?.querySelector("[data-sales-worker-select]")?.focus();
    });
  }
  if (action === "task-mark-lead-invalid") {
    state.invalidMode = "single";
    openModal("invalid");
  }
  if (action === "task-static-action") showToast("这是任务中心设计稿示意，具体操作由前端按 PRD 接口实现。", "info");
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  document.querySelectorAll(".modal-backdrop:not([hidden])").forEach((modal) => {
    modal.hidden = true;
  });
  drawer.classList.add("closed");
  document.querySelectorAll(".management-drawer").forEach((panel) => panel.classList.add("closed"));
});

let draggedVehicleImage = null;
document.querySelectorAll("[data-image-src]").forEach((button) => {
  button.draggable = true;
  button.addEventListener("dragstart", () => {
    draggedVehicleImage = button;
    button.classList.add("is-dragging");
  });
  button.addEventListener("dragend", () => {
    button.classList.remove("is-dragging");
    draggedVehicleImage = null;
    const first = document.querySelector(".vehicle-image-strip [data-image-src]");
    if (first) {
      document.querySelectorAll("[data-image-src]").forEach((item) => item.classList.toggle("is-current", item === first));
      syncVehicleMainImage(first.dataset.imageSrc);
      showToast("图片顺序已更新，第一张已作为主图", "success");
    }
  });
});

document.querySelector(".vehicle-image-strip")?.addEventListener("dragover", (event) => {
  event.preventDefault();
  const target = event.target.closest("[data-image-src]");
  if (!draggedVehicleImage || !target || target === draggedVehicleImage) return;
  const rect = target.getBoundingClientRect();
  target.parentElement.insertBefore(draggedVehicleImage, event.clientX < rect.left + rect.width / 2 ? target : target.nextSibling);
});

const applyVehicleScenario = (scenario) => {
  const screen = document.querySelector('[data-module-content="vehicles"]');
  const firstRow = document.querySelector("tr[data-vehicle-id]");
  if (!screen) return;
  screen.classList.toggle("is-readonly", scenario === "readonly");
  document.querySelector("[data-vehicle-drawer]")?.classList.toggle("is-readonly", scenario === "readonly");
  applyVehicleListState(["loading", "empty", "no-results", "error"].includes(scenario) ? scenario : "default");

  const drawerScenarios = [
    "detail",
    "edit",
    "image-empty",
    "image-uploading",
    "image-single-failure",
    "image-partial-failure",
    "image-drag",
    "readonly",
    "list-missing",
  ];
  if (drawerScenarios.includes(scenario)) {
    const editing = ["edit", "image-empty", "image-uploading", "image-single-failure", "image-partial-failure", "image-drag"].includes(scenario);
    openVehicleDrawer(firstRow, editing);
    if (scenario === "image-empty" || scenario === "image-uploading") {
      const panel = document.querySelector(`[data-gallery-state="${scenario.replace("image-", "")}"]`);
      if (panel) panel.hidden = false;
    }
    if (scenario === "image-single-failure" || scenario === "image-partial-failure") {
      const alert = document.querySelector("[data-image-upload-alert]");
      if (alert) {
        alert.hidden = false;
        alert.querySelector("strong").textContent = scenario === "image-single-failure" ? "图片上传失败" : "3 张上传成功，1 张失败";
        alert.querySelector("p").textContent = "door-detail.tiff：不支持该文件格式，请上传 JPEG、PNG 或 WebP。";
      }
    }
    if (scenario === "image-drag") {
      document.querySelectorAll("[data-image-src]")[1]?.classList.add("is-dragging");
    }
    if (scenario === "list-missing") {
      const shelfButton = document.querySelector("[data-vehicle-shelf-button]");
      if (shelfButton) {
        shelfButton.dataset.currentStatus = "unlisted";
        shelfButton.dataset.incomplete = "true";
        shelfButton.textContent = "上架";
      }
      const validation = document.querySelector("[data-vehicle-list-validation]");
      if (validation) validation.hidden = false;
    }
  }
  if (scenario === "list-confirm") {
    openVehicleDrawer(document.querySelector('tr[data-vehicle-status="unlisted"]'));
    openModal("vehicle-list-confirm");
  }
  if (scenario === "unlist-confirm") {
    openVehicleDrawer(firstRow);
    openModal("vehicle-unlist");
  }
  if (scenario === "create") openModal("vehicle-create");
  if (scenario === "unsaved") {
    openVehicleDrawer(firstRow, true);
    openModal("vehicle-unsaved");
  }
  if (scenario === "image-delete") {
    openVehicleDrawer(firstRow, true);
    openModal("vehicle-image-delete");
  }
  if (scenario === "image-delete-last") {
    openVehicleDrawer(firstRow, true);
    openModal("vehicle-image-delete");
    const copy = document.querySelector("[data-vehicle-image-delete-copy]");
    const confirm = document.querySelector("[data-vehicle-image-delete-confirm]");
    if (copy) copy.textContent = "当前车辆已上架，且仅剩这一张有效图片。请先上传其他图片或下架车辆后再删除。";
    if (confirm) confirm.disabled = true;
  }
  if (scenario === "image-preview") {
    openVehicleDrawer(firstRow);
    openModal("vehicle-image-preview");
  }
  if (scenario.startsWith("import-")) {
    openModal("vehicle-import");
    if (scenario === "import-valid") setVehicleImportPreview("valid");
    if (scenario === "import-invalid") setVehicleImportPreview("invalid");
    if (scenario === "import-success") setVehicleImportResult("success");
    if (scenario === "import-failed") setVehicleImportResult("failed");
  }
  if (scenario === "no-permission") {
    document.querySelector('[data-module="vehicles"]')?.setAttribute("hidden", "");
    screen.hidden = true;
    workspace.classList.add("is-empty");
    emptyWorkspace.hidden = false;
    moduleButtons.forEach((button) => button.classList.remove("active"));
  }
};

const initialParams = new URLSearchParams(window.location.search);
const initialModule = initialParams.get("module");
decorateTaskIdentifiers();
if ([...moduleButtons].some((button) => button.dataset.module === initialModule)) {
  showModule(initialModule).then(() => {
    if (initialModule === "vehicles") applyVehicleScenario(initialParams.get("vehicleState") || "default");
    if (initialModule === "knowledge") applyKnowledgeScenario(initialParams.get("knowledgeState") || "default");
  });
}
