const state = {
  authToken: localStorage.getItem("localAuthToken") || "",
  activeView: "customer_service",
  overview: null,
  customerService: null,
  customerServiceSessions: [],
  customerServiceSessionMeta: null,
  customerServiceRuntime: null,
  customerServiceRuntimeTimer: null,
  customerServiceRuntimeBusy: false,
  customerServiceFloatPosition: loadFloatPosition("customerServiceFloatPositionV1"),
  customerProfiles: [],
  selectedCustomerProfile: null,
  customerProfileMessages: [],
  productCatalog: null,
  selectedProduct: null,
  productDetailMode: "view",
  productDetailScopedKnowledge: {},
  productScopedEditor: null,
  productAcknowledgeLoadingIds: new Set(),
  categories: [],
  activeCategoryId: "",
  categoryItems: [],
  categoryItemsTotal: 0,
  categoryItemsHasMore: false,
  categoryItemsLoading: false,
  categoryItemsLoadingMore: false,
  categoryItemsLoadingOffset: 0,
  categoryItemsLoadingLimit: 20,
  categoryItemsLoadingQuery: "",
  categoryItemsError: "",
  knowledgeListVisibleCount: 20,
  knowledgeSearchTimer: null,
  selectedKnowledge: null,
  knowledgeMode: "view",
  generatorSession: null,
  generatorMessages: [],
  generatorConfirmBusy: false,
  selectedCandidate: null,
  learningInProgress: false,
  uploadInProgress: false,
  autoLearnAfterUpload: localStorage.getItem("uploadAutoLearnAfterSelect") !== "0",
  activeIntakeTab: "uploads",
  recorderSummary: null,
  recorderConversations: [],
  recorderMessages: [],
  selectedRecorderConversation: null,
  recorderMessageCache: {},
  recorderModules: [],
  recorderExportRuns: [],
  recorderRuntimeStatus: null,
  recorderRuntimeBusy: false,
  recorderExportRunBusy: false,
  recorderExportPollingTimer: null,
  activeReferenceTab: "experiences",
  productScopedEditContext: null,
  diagnosticHighlight: null,
  ragStatus: null,
  ragHits: [],
  ragExperiences: [],
  ragExperienceDisplayCounts: null,
  ragExperienceGovernanceCounts: null,
  ragExperienceHiddenCount: 0,
  ragExperienceLoadedCount: 0,
  ragExperienceDiscardedTotal: 0,
  ragExperienceTotal: 0,
  showDiscardedRagExperiences: localStorage.getItem("showDiscardedRagExperiences") !== "0",
  ragExperienceExpanded: loadStringSet("ragExperienceExpanded"),
  ragActionNotice: null,
  ragInterpretationInProgress: false,
  ragInterpretationPendingCount: 0,
  ragInterpretationLastResult: null,
  ragInterpretationLoadingIds: new Set(),
  ragActionLoadingIds: new Map(),
  candidateActionLoadingIds: new Map(),
  ragAnalytics: null,
  auth: null,
  tenants: [],
  activeTenantId: localStorage.getItem("localActiveTenantId") || "",
  syncStatus: null,
  startupSyncTimer: null,
  cloudGateRetryTimer: null,
  loginChallenge: null,
  cloudLoginLocked: false,
  localLoginSubmitting: false,
  initChallenge: null,
  passwordChallenge: null,
  emailChallenge: null,
  security: null,
  llmConfig: null,
  feishuConfig: null,
  workflowOpsBusy: false,
  workflowLastResult: null,
};
const CUSTOMER_SERVICE_FLOAT_POSITION_KEY = "customerServiceFloatPositionV2";
const localDeviceId = getOrCreateDeviceId("localConsoleDeviceId");
const RECORDER_EXPORT_DEFAULT_LIMIT = 10000;
const KNOWLEDGE_LIST_PAGE_SIZE = 20;
const WORKFLOW_DEFAULT_METRICS_GATE = {
  factual_consistency_min: 0.95,
  violation_rate_max: 0.01,
  handoff_precision_min: 0.9,
  continue_chat_rate_min: 0.7,
};
const WORKFLOW_ACTION_BUTTON_IDS = [
  "wf-curation-run",
  "wf-curation-fetch",
  "wf-import-dry-run",
  "wf-import-fetch",
  "wf-import-apply",
  "wf-eval-run",
  "wf-eval-fetch",
  "wf-release-create",
  "wf-release-fetch",
  "wf-release-approve",
  "wf-release-rollback",
];

const titles = {
  customer_service: "微信智能客服",
  knowledge_center: "知识成长中心",
  product_catalog: "商品库",
  overview: "总览",
  knowledge: "正式知识库",
  intake: "资料导入",
  uploads: "资料导入",
  candidates: "待确认知识",
  generator: "手动创建",
  recorder: "AI智能记录员",
  customer_profiles: "客户画像",
  ai_reference: "AI经验池",
  diagnostics: "知识检测",
  settings: "系统设置",
  versions: "备份还原",
  security: "账号安全",
};

const viewAliases = {
  overview: {view: "knowledge_center"},
  generator: {view: "intake", group: "intake", tab: "generator"},
  uploads: {view: "intake", group: "intake", tab: "uploads"},
  candidates: {view: "intake", group: "intake", tab: "candidates"},
  rag: {view: "ai_reference", group: "reference", tab: "sources"},
  rag_experiences: {view: "ai_reference", group: "reference", tab: "experiences"},
  versions: {view: "settings"},
  security: {view: "settings"},
};

const templateLabels = {
  default: "默认回复",
  quote: "报价回复",
  discount_policy: "议价回复",
  logistics: "物流回复",
  after_sales: "售后回复",
  notes: "内部备注",
};

const optionLabels = {
  policy_type: {
    company: "公司信息",
    invoice: "开票",
    payment: "付款",
    logistics: "物流",
    after_sales: "售后",
    discount: "优惠议价",
    sample: "样品",
    installation: "安装",
    contract: "合同",
    manual_required: "必须人工确认",
    other: "其他",
  },
  risk_level: {normal: "普通", warning: "需关注", high: "高风险"},
  record_type: {product: "商品", inventory: "库存", price: "价格", customer: "客户", order: "订单", other: "其他"},
  sync_status: {imported: "已导入", linked: "已关联", ignored: "已忽略", error: "异常"},
  applicability_scope: {global: "本账号通用", product_category: "某类商品适用", specific_product: "指定商品适用"},
};

const fieldLabelOverrides = {
  price_tiers: "批量价格",
  reply_templates: "基础话术（弱触发）",
  risk_rules: "风险提醒",
  policy_type: "规则类别",
  min_quantity: "起订量",
  unit_price: "单价",
  allow_auto_reply: "允许自动回复",
  requires_handoff: "需要人工确认",
  handoff_reason: "人工确认原因",
  operator_alert: "提醒人工客服",
  fields: "字段内容",
  additional_details: "补充信息",
  applicability_scope: "适用范围",
  product_id: "关联商品 ID",
  product_category: "关联商品类目",
  alias_keywords: "别名关键词",
  specs: "规格参数",
  source_title: "来源标题",
  batch_token: "批次标识",
  risk_level: "风险等级",
  customer_message: "客户怎么问",
  service_reply: "AI怎么回",
  intent_tags: "客户意图",
  tone_tags: "表达特点",
  linked_categories: "关联栏目",
  linked_item_ids: "关联知识",
  usable_as_template: "是否可作为话术模板",
};

let helperCardCollapseObserver = null;
const helperCardCollapseSkipIds = new Set([
  "customer-service-session-summary",
  "recorder-module-info",
  "recorder-selected-summary",
  "recorder-export-progress",
]);

function initHelperCardCollapsing() {
  applyHelperCardCollapsing(document);
  if (helperCardCollapseObserver || !document.body) return;
  helperCardCollapseObserver = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
      mutation.addedNodes.forEach((node) => {
        if (!(node instanceof Element)) return;
        applyHelperCardCollapsing(node);
      });
    });
  });
  helperCardCollapseObserver.observe(document.body, {childList: true, subtree: true});
}

function applyHelperCardCollapsing(root) {
  if (!root) return;
  const cards = [];
  if (root instanceof Element && root.classList.contains("helper-card")) cards.push(root);
  if (root.querySelectorAll) cards.push(...root.querySelectorAll(".helper-card"));
  cards.forEach((card) => convertHelperCardToCollapsible(card));
}

function convertHelperCardToCollapsible(card) {
  if (!(card instanceof HTMLElement)) return;
  if (card.dataset.helperCollapseReady === "1") return;
  if (card.tagName.toLowerCase() === "details") return;
  if (card.classList.contains("helper-card-collapsible")) return;
  if (helperCardCollapseSkipIds.has(card.id || "")) return;
  if (card.dataset.helperCollapse === "off") return;
  if (card.querySelector("button, input, select, textarea, form, .button-row, .compact-list, .metric-grid, .list-pane, .detail-pane, table")) return;
  const contentTitle = helperCardContentTitle(card);

  const details = document.createElement("details");
  Array.from(card.attributes || []).forEach((attr) => {
    if (attr.name === "class") return;
    details.setAttribute(attr.name, attr.value);
  });
  details.className = `${card.className} helper-card-collapsible`;
  details.dataset.helperCollapseReady = "1";

  const summary = document.createElement("summary");
  summary.className = "helper-card-summary";
  const title = document.createElement("strong");
  title.textContent = "备注信息";
  const hint = document.createElement("span");
  hint.className = "helper-card-summary-hint";
  hint.textContent = "展开/收起";
  summary.appendChild(title);
  summary.appendChild(hint);
  if (contentTitle) summary.title = contentTitle;

  const content = document.createElement("div");
  content.className = "helper-card-content";
  while (card.firstChild) {
    content.appendChild(card.firstChild);
  }
  details.appendChild(summary);
  details.appendChild(content);
  card.replaceWith(details);
}

function helperCardContentTitle(card) {
  const strong = card.querySelector(":scope > strong") || card.querySelector("strong");
  if (strong) {
    const text = normalizeSpace(strong.textContent);
    if (text) return text;
  }
  const span = card.querySelector(":scope > span") || card.querySelector("span");
  const fallback = normalizeSpace(span?.textContent || "");
  return fallback || "备注信息";
}

function normalizeSpace(text) {
  return String(text || "").replace(/\s+/g, " ").trim();
}

function selectView(view, options = {}) {
  const target = viewAliases[view] || {view};
  if (target.group === "intake") state.activeIntakeTab = target.tab;
  if (target.group === "reference") state.activeReferenceTab = target.tab;
  const activeView = target.view;
  state.activeView = activeView;
  if (activeView !== "knowledge" || !options.keepKnowledgeContext) state.productScopedEditContext = null;
  if (activeView !== "knowledge" || !options.keepDiagnosticHighlight) state.diagnosticHighlight = null;
  const requestedView = view || activeView;
  const navItems = Array.from(document.querySelectorAll(".nav-item"));
  const hasRequestedNavItem = navItems.some((item) => item.dataset.view === requestedView);
  navItems.forEach((item) => {
    item.classList.toggle("is-active", item.dataset.view === requestedView || (!hasRequestedNavItem && item.dataset.view === activeView));
  });
  document.querySelectorAll(".view-panel").forEach((panel) => {
    panel.classList.toggle("is-visible", panel.dataset.panel === activeView);
  });
  document.getElementById("view-title").textContent = titles[requestedView] || titles[activeView] || "总览";
  syncWorkflowTabs();
  if (activeView !== "recorder") stopRecorderExportPolling();
}

function syncWorkflowTabs() {
  document.querySelectorAll('[data-intake-tab]').forEach((section) => {
    section.classList.toggle("is-visible", section.dataset.intakeTab === state.activeIntakeTab);
  });
  document.querySelectorAll('[data-reference-tab]').forEach((section) => {
    section.classList.toggle("is-visible", section.dataset.referenceTab === state.activeReferenceTab);
  });
  document.querySelectorAll('.workflow-tab[data-group="intake"]').forEach((button) => {
    button.classList.toggle("is-active", button.dataset.tab === state.activeIntakeTab);
  });
  document.querySelectorAll('.workflow-tab[data-group="reference"]').forEach((button) => {
    button.classList.toggle("is-active", button.dataset.tab === state.activeReferenceTab);
  });
}

async function refreshHealth() {
  const pill = document.getElementById("health-pill");
  try {
    const payload = await apiGet("/api/health");
    pill.textContent = payload.ok ? "本地已连接" : "异常";
    pill.classList.toggle("is-ok", Boolean(payload.ok));
  } catch (error) {
    pill.textContent = "未连接";
    pill.classList.remove("is-ok");
  }
}

function apiHeaders(extra = {}) {
  const headers = {...extra};
  if (state.activeTenantId) headers["X-Tenant-ID"] = state.activeTenantId;
  if (state.authToken) headers.Authorization = `Bearer ${state.authToken}`;
  return headers;
}

function isImplicitLocalSession(auth = state.auth) {
  return auth?.strict === false && auth?.authenticated === false && auth?.session?.source === "implicit";
}

function hasLocalConsoleAccess() {
  return Boolean(state.authToken || isImplicitLocalSession());
}

async function apiGet(path) {
  const response = await fetch(path, {headers: apiHeaders()});
  if (!response.ok) throw new Error(await responseErrorMessage(response, path));
  return response.json();
}

async function apiJson(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: apiHeaders({"Content-Type": "application/json", ...(options.headers || {})}),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 405) throw new Error("当前本地服务可能还没重启到最新版本，请重启管理台服务后再试。");
    throw new Error(formatApiError(payload, `${path} ${response.status}`));
  }
  return payload;
}

async function responseErrorMessage(response, path) {
  if (response.status === 405) return "当前本地服务可能还没重启到最新版本，请重启管理台服务后再试。";
  const payload = await response.json().catch(() => ({}));
  return formatApiError(payload, `${path} ${response.status}`);
}

function formatApiError(payload, fallback) {
  const detail = payload?.detail;
  if (!detail) return fallback;
  if (typeof detail === "string") return detail;
  return detail.message || JSON.stringify(detail);
}

function initializeLocalLogin() {
  document.body.classList.add("auth-locked");
  const form = document.getElementById("local-login-form");
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const allowed = await prepareCloudGateForLogin({silent: false, force: true});
    if (!allowed) return;
    loginLocal(new FormData(form)).catch((error) => showLoginMessage(error.message));
  });
  document.getElementById("local-login-reset")?.addEventListener("click", resetLocalLoginChallenge);
  document.getElementById("local-init-form")?.addEventListener("submit", (event) => initializeLocalAccount(event).catch((error) => showInitMessage(error.message)));
  document.getElementById("local-init-back")?.addEventListener("click", resetLocalInitialization);
  prepareCloudGateForLogin({silent: true, force: true}).catch(() => {});
  bootstrapImplicitLocalSession().catch((error) => {
    console.warn("implicit local login bootstrap failed", error);
  });
  if (state.authToken) {
    prepareCloudGateForLogin({silent: false, force: true})
      .then((allowed) => {
        if (!allowed) {
          lockLocalConsole();
          return;
        }
        document.body.classList.remove("auth-locked");
        bootstrapAuthenticatedApp().catch((error) => {
          showLoginMessage(error.message || "登录状态已失效，请重新登录。");
          lockLocalConsole();
        });
      })
      .catch((error) => {
        showLoginMessage(error.message || "登录状态已失效，请重新登录。");
        lockLocalConsole();
      });
  }
}

async function bootstrapImplicitLocalSession() {
  if (state.authToken) return;
  const allowed = await prepareCloudGateForLogin({silent: true, force: true});
  if (!allowed || state.authToken) return;
  const auth = await apiGet("/api/auth/me").catch(() => null);
  if (!isImplicitLocalSession(auth?.auth)) return;
  state.auth = auth.auth;
  document.body.classList.remove("auth-locked");
  resetLocalLoginChallenge({silent: true});
  hideLoginMessage();
  await bootstrapAuthenticatedApp();
}

async function loginLocal(form) {
  if (state.localLoginSubmitting) return;
  state.localLoginSubmitting = true;
  const submitButton = document.getElementById("local-login-submit");
  if (submitButton) submitButton.disabled = true;
  try {
    if (state.loginChallenge) {
      if (state.loginChallenge.mode === "bind_email") {
        const response = await fetch("/api/auth/login/bind-email/start", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({challenge_id: state.loginChallenge.challenge_id, email: form.get("bind_email")}),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.ok === false) {
          throw new Error(formatApiError(payload, "邮箱绑定验证发起失败，请检查邮箱。"));
        }
        state.loginChallenge.mode = "verify";
        document.getElementById("local-login-bind-email-field")?.classList.add("is-hidden");
        document.getElementById("local-login-code-field")?.classList.remove("is-hidden");
        document.getElementById("local-login-trust-field")?.classList.remove("is-hidden");
        document.getElementById("local-login-submit").textContent = "验证并登录";
        showLoginMessage(
          payload.debug_code
            ? `验证码已生成：${payload.debug_code}。生产环境会发送到 ${payload.masked_email || "绑定邮箱"}。`
            : `验证码已发送到 ${payload.masked_email || "绑定邮箱"}，请输入后登录。`
        );
        return;
      }
      const response = await fetch("/api/auth/login/verify", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          challenge_id: state.loginChallenge.challenge_id,
          code: form.get("email_code"),
          trust_device: Boolean(form.get("trust_device")),
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false) {
        throw new Error(formatApiError(payload, "验证码错误或已过期，请重新获取。"));
      }
      await completeLocalLogin(payload.session);
      return;
    }
    const response = await fetch("/api/auth/login/start", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        username: form.get("username"),
        password: form.get("password"),
        device_id: localDeviceId,
        device_name: browserDeviceName(),
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) {
      throw new Error(formatApiError(payload, "登录失败，请检查账号和密码。"));
    }
    if (!payload.requires_verification && payload.session) {
      await completeLocalLogin(payload.session);
      return;
    }
    if (payload.requires_initialization) {
      showLocalInitialization(payload);
      return;
    }
    state.loginChallenge = {challenge_id: payload.challenge_id, mode: payload.requires_email_binding ? "bind_email" : "verify"};
    if (payload.requires_email_binding) {
      document.getElementById("local-login-bind-email-field")?.classList.remove("is-hidden");
      document.getElementById("local-login-code-field")?.classList.add("is-hidden");
      document.getElementById("local-login-trust-field")?.classList.add("is-hidden");
      document.getElementById("local-login-submit").textContent = "发送邮箱验证码";
      showLoginMessage(payload.message || "这个账号还没有绑定邮箱，请填写邮箱后获取验证码。");
    } else {
      document.getElementById("local-login-bind-email-field")?.classList.add("is-hidden");
      document.getElementById("local-login-code-field")?.classList.remove("is-hidden");
      document.getElementById("local-login-trust-field")?.classList.remove("is-hidden");
      document.getElementById("local-login-submit").textContent = "验证并登录";
      showLoginMessage(
        payload.debug_code
          ? `验证码已生成：${payload.debug_code}。生产环境会发送到 ${payload.masked_email || "绑定邮箱"}。`
          : `验证码已发送到 ${payload.masked_email || "绑定邮箱"}，请输入后登录。`
      );
    }
    document.getElementById("local-login-reset")?.classList.remove("is-hidden");
  } finally {
    state.localLoginSubmitting = false;
    if (submitButton) submitButton.disabled = false;
  }
}

function showLocalInitialization(payload) {
  state.initChallenge = {challenge_id: payload.challenge_id, role: payload.role || "customer", mode: "start"};
  document.body.classList.add("auth-locked", "auth-initializing");
  const isAdmin = state.initChallenge.role === "admin";
  const intro = document.getElementById("local-init-intro");
  if (intro) {
    intro.textContent = isAdmin
      ? "admin 首次进入客户端前必须修改密码并绑定邮箱。SMTP 发信配置在 VPS 管理控制台统一设置，Local 不保存客户可见的 SMTP 密码。"
      : "首次使用前必须修改密码并绑定邮箱。完成后需要用新密码重新登录。";
  }
  document.getElementById("local-init-code-field")?.classList.add("is-hidden");
  document.getElementById("local-init-submit").textContent = "发送初始化验证码";
  hideLoginMessage();
  showInitMessage(payload.message || "请完成首次初始化。");
}

async function initializeLocalAccount(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  if (!state.initChallenge) throw new Error("初始化会话已失效，请返回登录重新开始。");
  if (state.initChallenge.mode === "verify") {
    const response = await fetch("/api/auth/initialize/verify", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({challenge_id: state.initChallenge.challenge_id, code: form.get("email_code")}),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) throw new Error(formatApiError(payload, "验证码错误或已过期。"));
    formElement.reset();
    resetLocalInitialization({silent: true});
    resetLocalLoginChallenge({silent: true});
    showLoginMessage("初始化已完成，请使用新密码重新登录。登录时仍需要邮箱验证码。");
    return;
  }
  if (form.get("new_password") !== form.get("confirm_password")) {
    throw new Error("两次输入的新密码不一致。");
  }
  const response = await fetch("/api/auth/initialize/start", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      challenge_id: state.initChallenge.challenge_id,
      email: form.get("email"),
      new_password: form.get("new_password"),
    }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.ok === false) throw new Error(formatApiError(payload, "初始化验证码发送失败。"));
  state.initChallenge.mode = "verify";
  document.getElementById("local-init-code-field")?.classList.remove("is-hidden");
  document.getElementById("local-init-submit").textContent = "验证并完成初始化";
  showInitMessage(
    payload.debug_code
      ? `验证码已生成：${payload.debug_code}。生产环境会发送到 ${payload.masked_email || "绑定邮箱"}。`
      : `验证码已发送到 ${payload.masked_email || "绑定邮箱"}，请输入后完成初始化。`
  );
}

function resetLocalInitialization(options = {}) {
  state.initChallenge = null;
  document.body.classList.remove("auth-initializing");
  if (!state.authToken) document.body.classList.add("auth-locked");
  document.getElementById("local-init-code-field")?.classList.add("is-hidden");
  document.getElementById("local-init-submit").textContent = "发送初始化验证码";
  const codeInput = document.getElementById("local-init-form")?.querySelector("[name='email_code']");
  if (codeInput) codeInput.value = "";
  if (!options.silent) hideInitMessage();
}

async function completeLocalLogin(session) {
  state.authToken = session?.token || "";
  if (!state.authToken) throw new Error("登录成功但没有返回会话 token。");
  const storedTenantId = localStorage.getItem("localActiveTenantId") || "";
  state.activeTenantId = session?.user?.role === "admin" && storedTenantId ? storedTenantId : session?.active_tenant_id || state.activeTenantId || "";
  localStorage.setItem("localAuthToken", state.authToken);
  if (state.activeTenantId) localStorage.setItem("localActiveTenantId", state.activeTenantId);
  document.body.classList.remove("auth-locked");
  resetLocalLoginChallenge({silent: true});
  hideLoginMessage();
  await bootstrapAuthenticatedApp();
}

function resetLocalLoginChallenge(options = {}) {
  state.loginChallenge = null;
  const form = document.getElementById("local-login-form");
  document.getElementById("local-login-bind-email-field")?.classList.add("is-hidden");
  document.getElementById("local-login-code-field")?.classList.add("is-hidden");
  document.getElementById("local-login-trust-field")?.classList.add("is-hidden");
  document.getElementById("local-login-reset")?.classList.add("is-hidden");
  document.getElementById("local-login-submit").textContent = "登录";
  const codeInput = form?.querySelector("[name='email_code']");
  if (codeInput) codeInput.value = "";
  const emailInput = form?.querySelector("[name='bind_email']");
  if (emailInput) emailInput.value = "";
  const trustInput = form?.querySelector("[name='trust_device']");
  if (trustInput) trustInput.checked = false;
  if (!options.silent) hideLoginMessage();
}

async function bootstrapAuthenticatedApp() {
  await refreshAccountContext();
  await registerLocalNode().catch((error) => console.warn("register local node failed", error));
  scheduleStartupSync();
  scheduleCustomerServiceRuntimePolling();
  renderGenerator();
  if (hasHashView()) {
    activateHashView();
  } else {
    await loadCustomerService().catch(console.error);
  }
  warmupStartupData();
}

function hasHashView() {
  const view = window.location.hash.replace("#", "");
  return Boolean(titles[view] || viewAliases[view]);
}

function warmupStartupData() {
  window.setTimeout(() => {
    Promise.all([
      loadOverview().catch(console.error),
      refreshRagExperienceBadge().catch(console.error),
    ]).catch(console.error);
  }, 80);
}

async function logoutLocal() {
  if (state.authToken) {
    await fetch("/api/auth/logout", {method: "POST", headers: apiHeaders()}).catch(() => {});
  }
  lockLocalConsole();
}

function lockLocalConsole() {
  if (state.startupSyncTimer) {
    clearTimeout(state.startupSyncTimer);
    state.startupSyncTimer = null;
  }
  if (state.customerServiceRuntimeTimer) {
    clearInterval(state.customerServiceRuntimeTimer);
    state.customerServiceRuntimeTimer = null;
  }
  stopRecorderExportPolling();
  state.recorderExportRunBusy = false;
  state.authToken = "";
  state.auth = null;
  state.security = null;
  state.initChallenge = null;
  state.passwordChallenge = null;
  state.emailChallenge = null;
  state.activeTenantId = "";
  document.getElementById("local-password-code-field")?.classList.add("is-hidden");
  document.getElementById("local-email-code-field")?.classList.add("is-hidden");
  resetLocalLoginChallenge({silent: true});
  resetLocalInitialization({silent: true});
  localStorage.removeItem("localAuthToken");
  localStorage.removeItem("localActiveTenantId");
  document.body.classList.add("auth-locked");
}

function showLoginMessage(text) {
  const element = document.getElementById("login-message");
  if (!element) return;
  element.textContent = text;
  element.classList.remove("is-hidden");
}

function hideLoginMessage() {
  const element = document.getElementById("login-message");
  if (!element) return;
  element.textContent = "";
  element.classList.add("is-hidden");
}

function showInitMessage(text) {
  const element = document.getElementById("local-init-message");
  if (!element) return;
  element.textContent = text;
  element.classList.remove("is-hidden");
}

function hideInitMessage() {
  const element = document.getElementById("local-init-message");
  if (!element) return;
  element.textContent = "";
  element.classList.add("is-hidden");
}

function setLocalLoginEnabled(enabled) {
  const form = document.getElementById("local-login-form");
  if (!form) return;
  form.querySelectorAll("input, button").forEach((element) => {
    element.disabled = !enabled;
  });
}

async function prepareCloudGateForLogin({silent = false, force = true} = {}) {
  try {
    const response = await fetch("/api/auth/cloud-gate/prepare", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        tenant_id: state.activeTenantId || "",
        force: Boolean(force),
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(
        payload?.detail?.message ||
        payload?.detail ||
        payload?.message ||
        "当前客户端未通过云端授权校验。请连接服务端并完成共享行业知识库刷新后再使用。"
      );
    }
    const required = payload.required !== false;
    const ok = payload.ok !== false;
    state.cloudLoginLocked = required && !ok;
    setLocalLoginEnabled(!state.cloudLoginLocked);
    if (state.cloudLoginLocked) {
      showLoginMessage(payload.message || "当前客户端未通过云端授权校验。请连接服务端并完成共享行业知识库刷新后再使用。");
      scheduleCloudGatePrepareRetry();
      return false;
    }
    clearCloudGatePrepareRetry();
    if (!silent && !state.loginChallenge) hideLoginMessage();
    return true;
  } catch (error) {
    state.cloudLoginLocked = true;
    setLocalLoginEnabled(false);
    showLoginMessage(error.message || "当前客户端未通过云端授权校验。请连接服务端并完成共享行业知识库刷新后再使用。");
    scheduleCloudGatePrepareRetry();
    return false;
  }
}

function clearCloudGatePrepareRetry() {
  if (state.cloudGateRetryTimer) {
    clearTimeout(state.cloudGateRetryTimer);
    state.cloudGateRetryTimer = null;
  }
}

function scheduleCloudGatePrepareRetry() {
  if (hasLocalConsoleAccess()) return;
  if (!state.cloudLoginLocked) return;
  if (state.cloudGateRetryTimer) return;
  const retryDelayMs = 5000 + Math.floor(Math.random() * 2000);
  state.cloudGateRetryTimer = setTimeout(() => {
    state.cloudGateRetryTimer = null;
    if (hasLocalConsoleAccess() || !state.cloudLoginLocked) return;
    prepareCloudGateForLogin({silent: true, force: true}).catch(() => {});
  }, retryDelayMs);
}

function getOrCreateDeviceId(key) {
  let value = localStorage.getItem(key);
  if (!value) {
    value = `device_${Date.now()}_${Math.random().toString(36).slice(2)}`;
    localStorage.setItem(key, value);
  }
  return value;
}

function loadStringSet(key) {
  try {
    const values = JSON.parse(localStorage.getItem(key) || "[]");
    if (!Array.isArray(values)) return new Set();
    return new Set(values.map((value) => String(value || "").trim()).filter(Boolean));
  } catch {
    return new Set();
  }
}

function saveStringSet(key, values) {
  localStorage.setItem(key, JSON.stringify([...values].slice(-200)));
}

function loadFloatPosition(key) {
  try {
    const payload = JSON.parse(localStorage.getItem(key) || "null");
    if (!payload || typeof payload !== "object") return null;
    const left = Number(payload.left);
    const top = Number(payload.top);
    if (!Number.isFinite(left) || !Number.isFinite(top)) return null;
    return {left, top};
  } catch {
    return null;
  }
}

function saveFloatPosition(key, position) {
  if (!position) return;
  localStorage.setItem(
    key,
    JSON.stringify({
      left: Math.round(Number(position.left) || 0),
      top: Math.round(Number(position.top) || 0),
    }),
  );
}

function browserDeviceName() {
  const platform = navigator.platform || "Browser";
  const language = navigator.language || "";
  return `${platform} ${language}`.trim();
}

async function registerLocalNode() {
  if (!state.syncStatus?.vps_configured) return;
  const result = await apiJson("/api/sync/register-node", {
    method: "POST",
    body: JSON.stringify({display_name: `${state.auth?.session?.user?.display_name || state.auth?.session?.user?.user_id || "Local"} 客户端`}),
  });
  if (result.ok && result.node) {
    state.syncStatus.node = result.node;
    renderAccountContext();
  }
}

function scheduleStartupSync() {
  if (state.startupSyncTimer) {
    clearTimeout(state.startupSyncTimer);
    state.startupSyncTimer = null;
  }
  if (!state.syncStatus?.vps_configured) return;
  runStartupSync({startup: true}).catch((error) => {
    console.warn("startup sync failed", error);
  }).finally(() => {
    scheduleNextStartupSync();
  });
}

function scheduleNextStartupSync() {
  if (!state.syncStatus?.vps_configured) return;
  if (state.startupSyncTimer) clearTimeout(state.startupSyncTimer);
  state.startupSyncTimer = setTimeout(() => {
    state.startupSyncTimer = null;
    runStartupSync({startup: false}).catch((error) => {
      console.warn("periodic sync failed", error);
    }).finally(() => {
      scheduleNextStartupSync();
    });
  }, nextSharedSyncDelayMs());
}

function nextSharedSyncDelayMs() {
  const cache = state.syncStatus?.shared_cloud_cache || {};
  if (!cache.exists || cache.valid === false) return 60 * 1000;
  const refreshAt = Date.parse(cache.refresh_after_at || "");
  if (Number.isFinite(refreshAt)) {
    return clampSyncDelay(refreshAt - Date.now());
  }
  const refreshSeconds = Number(cache.refresh_after_seconds || 0);
  if (Number.isFinite(refreshSeconds) && refreshSeconds > 0) {
    return clampSyncDelay(refreshSeconds * 1000);
  }
  return 5 * 60 * 1000;
}

function clampSyncDelay(value) {
  const delay = Number.isFinite(value) ? value : 5 * 60 * 1000;
  return Math.max(60 * 1000, Math.min(10 * 60 * 1000, delay));
}

async function runStartupSync({startup = false} = {}) {
  if (!state.syncStatus?.vps_configured) return;
  const commandResult = await pollSyncCommands();
  handleSyncCommandOutcome(commandResult, {startup});
  const results = await Promise.allSettled([syncSharedCloudSnapshot(), checkSyncUpdate(), syncFormalSharedCandidates(), syncRecorderModuleBindings()]);
  if (results[0]?.status === "fulfilled") updateSharedCloudCacheStatus(results[0].value);
  const failed = results.filter((item) => item.status === "rejected");
  if (failed.length) {
    console.warn("some startup sync tasks failed", failed);
  }
}

async function pollSyncCommands() {
  return apiJson("/api/sync/commands/poll", {method: "POST", body: "{}"});
}

async function syncSharedCloudSnapshot({force = false} = {}) {
  return apiJson("/api/sync/shared/cloud-snapshot", {
    method: "POST",
    body: JSON.stringify({force}),
  });
}

function updateSharedCloudCacheStatus(payload = {}) {
  if (!state.syncStatus) state.syncStatus = {};
  const previous = state.syncStatus.shared_cloud_cache || {};
  state.syncStatus.shared_cloud_cache = {
    ...previous,
    exists: Boolean(payload.cached ?? previous.exists),
    valid: payload.cache_valid ?? previous.valid,
    version: payload.snapshot_version || previous.version || "",
    item_count: payload.item_count ?? previous.item_count ?? 0,
    category_count: payload.category_count ?? previous.category_count ?? 0,
    ttl_seconds: payload.ttl_seconds ?? previous.ttl_seconds ?? 0,
    refresh_after_seconds: payload.refresh_after_seconds ?? previous.refresh_after_seconds ?? 0,
    issued_at: payload.issued_at || previous.issued_at || "",
    refresh_after_at: payload.refresh_after_at || previous.refresh_after_at || "",
    expires_at: payload.expires_at || previous.expires_at || "",
    lease_id: payload.lease_id || previous.lease_id || "",
    cache_policy_mode: payload.cache_policy_mode || previous.cache_policy_mode || "",
    requires_cloud_refresh: payload.requires_cloud_refresh ?? previous.requires_cloud_refresh ?? true,
    tenant_industry_id: payload.tenant_industry_id || previous.tenant_industry_id || "",
  };
}

function handleSyncCommandOutcome(payload, {startup = false} = {}) {
  const patchCommands = (payload?.commands || []).filter((item) => item?.type === "pull_shared_patch");
  if (!patchCommands.length) return;
  const results = payload?.results || [];
  const failed = results.filter((item) => item?.accepted === false || item?.result?.ok === false || item?.error);
  const succeeded = results.length - failed.length;
  if (failed.length) {
    console.warn("shared cloud snapshot refresh command failed", {failed, startup});
    return;
  }
  if (succeeded) {
    console.info("shared cloud snapshot refresh command finished", {succeeded, startup});
  }
}

async function checkSyncUpdate() {
  return apiGet("/api/sync/update/check");
}

async function syncFormalSharedCandidates() {
  return apiJson("/api/sync/shared/formal-candidates", {
    method: "POST",
    body: JSON.stringify({use_llm: true, only_unscanned: true, limit: 30}),
  });
}

async function syncRecorderModuleBindings() {
  return apiJson("/api/sync/recorder/module-bindings", {
    method: "POST",
    body: "{}",
  });
}

async function refreshAccountContext() {
  const [auth, tenants, sync, security] = await Promise.all([
    apiGet("/api/auth/me").catch(() => ({ok: false})),
    apiGet("/api/tenants").catch(() => ({ok: false, items: []})),
    apiGet("/api/sync/status").catch(() => ({ok: false, mode: "unknown"})),
    apiGet("/api/auth/security").catch(() => ({ok: false, security: {}})),
  ]);
  state.auth = auth.auth || null;
  state.tenants = tenants.items || [];
  const authTenantId = auth.auth?.tenant_id || auth.auth?.session?.active_tenant_id || tenants.active_tenant_id || "";
  const tenantIds = state.tenants.map((item) => item.tenant_id).filter(Boolean);
  const role = auth.auth?.session?.user?.role || auth.auth?.role || "";
  const storedTenantId = localStorage.getItem("localActiveTenantId") || "";
  if (role === "admin") {
    const selectedTenantId = state.activeTenantId || storedTenantId || authTenantId || "";
    state.activeTenantId = tenantIds.includes(selectedTenantId) ? selectedTenantId : authTenantId || tenantIds[0] || "default";
  } else {
    const userTenantIds = auth.auth?.session?.user?.tenant_ids || [];
    const ownTenantId = authTenantId || userTenantIds[0] || tenantIds[0] || "default";
    state.activeTenantId = tenantIds.includes(ownTenantId) ? ownTenantId : tenantIds[0] || ownTenantId;
  }
  if (state.activeTenantId) localStorage.setItem("localActiveTenantId", state.activeTenantId);
  state.syncStatus = sync;
  state.security = security.security || {};
  renderAccountContext();
  renderLocalSecurity();
}

function renderAccountContext() {
  const user = state.auth?.session?.user || {};
  const role = user.role || state.auth?.role || "local";
  const roleNames = {admin: "管理员", customer: "客户", guest: "访客", local: "本地"};
  const accountName = user.username || user.display_name || user.user_id || "未登录";
  const activeTenant = state.tenants.find((item) => item.tenant_id === state.activeTenantId) || {};
  const displayTenant = activeTenant.display_name || state.activeTenantId || "default";
  const display = document.getElementById("current-account-space");
  const accountLabel = document.getElementById("current-account-label");
  const tenantSelect = document.getElementById("tenant-select");
  if (display) {
    display.classList.toggle("is-admin", role === "admin");
    display.querySelector("span").textContent = role === "admin" ? "客户数据" : "当前账号";
    display.title =
      role === "admin"
        ? "管理员可以切换本机不同客户的数据空间。切换后，知识库、记录员消息、商品库和设置都会随之切换。"
        : "当前登录账号对应的数据空间。一个客户账号只看自己的知识库、原始消息、记录员消息和设置。";
  }
  if (accountLabel) {
    accountLabel.textContent = `${accountName} · ${displayTenant}`;
    accountLabel.classList.toggle("is-hidden", role === "admin");
  }
  if (tenantSelect) {
    tenantSelect.classList.toggle("is-hidden", role !== "admin");
    tenantSelect.innerHTML = (state.tenants.length ? state.tenants : [{tenant_id: state.activeTenantId || "default", display_name: displayTenant}])
      .map((item) => {
        const tenantId = item.tenant_id || "default";
        const name = item.display_name || tenantId;
        return `<option value="${escapeHtml(tenantId)}"${tenantId === state.activeTenantId ? " selected" : ""}>${escapeHtml(name)}</option>`;
      })
      .join("");
  }
  document.getElementById("auth-pill").textContent = `${roleNames[role] || role}：${accountName}`;
  const cloudGate = state.syncStatus?.cloud_gate || {};
  if (cloudGate.required && cloudGate.ok === false) {
    document.getElementById("sync-pill").textContent = "云端未授权（已锁定）";
    return;
  }
  const nodeText = state.syncStatus?.node?.node_id ? "VPS 已连接" : "VPS 已配置";
  document.getElementById("sync-pill").textContent = state.syncStatus?.vps_configured ? nodeText : "本地模式";
}

function renderLocalSecurity() {
  const panel = document.getElementById("local-security-summary");
  if (!panel) return;
  const security = state.security || {};
  panel.innerHTML = `
    <div>
      <span>当前绑定邮箱</span>
      <strong>${escapeHtml(security.masked_email || security.email || "未绑定")}</strong>
    </div>
    <div>
      <span>邮箱验证码</span>
      <strong>${security.otp_required === false ? "未强制" : "已启用"}</strong>
    </div>
    <div>
      <span>信任设备</span>
      <strong>${escapeHtml(String(security.trusted_device_days || 30))} 天</strong>
    </div>
  `;
}

async function loadLlmConfig() {
  const payload = await apiGet("/api/system/llm-config");
  state.llmConfig = payload;
  renderLlmConfig();
}

const LLM_REASONING_EFFORT_LABELS = {
  "": "默认（不覆盖）",
  none: "none（关闭，若支持）",
  minimal: "minimal（最低）",
  low: "low（较低）",
  medium: "medium（平衡）",
  high: "high（深度）",
  xhigh: "xhigh（极深，若中转支持）",
};

function llmRouteElements(target = "primary") {
  const prefix = target === "fallback" ? "llm-fallback" : "llm";
  return {
    providerSelect: document.getElementById(`${prefix}-provider-select`),
    baseUrlInput: document.getElementById(`${prefix}-base-url-input`),
    flashModelSelect: document.getElementById(`${prefix}-flash-model-select`),
    flashModelInput: document.getElementById(`${prefix}-flash-model-input`),
    proModelSelect: document.getElementById(`${prefix}-pro-model-select`),
    proModelInput: document.getElementById(`${prefix}-pro-model-input`),
    flashReasoningInput: document.getElementById(`${prefix}-flash-reasoning-input`),
    proReasoningInput: document.getElementById(`${prefix}-pro-reasoning-input`),
    insecureTlsInput: document.getElementById(`${prefix}-insecure-tls-input`),
    input: document.getElementById(`${prefix}-api-key-input`),
    keyCurrent: document.getElementById(`${prefix}-api-key-current`),
    modelAvailabilityNote: document.getElementById(`${prefix}-model-availability-note`),
    enabledInput: target === "fallback" ? document.getElementById("llm-fallback-enabled-input") : null,
    testButton: document.getElementById(target === "fallback" ? "llm-fallback-test" : "llm-config-test"),
    proTestButton: document.getElementById(target === "fallback" ? "llm-fallback-test-pro" : "llm-config-test-pro"),
  };
}

function llmRoutePayload(config, target = "primary") {
  if (target === "fallback") return config?.fallback || {};
  return config || {};
}

function llmBaseUrlPlaceholder(provider) {
  if (provider === "openai_compatible") return "填写第三方中转 Base URL，例如 https://.../v1";
  if (provider === "anthropic") return "Anthropic 风格 Base URL，例如 https://.../v1";
  return "https://.../v1";
}

function renderLlmRouteForm(config, target = "primary") {
  const routePayload = llmRoutePayload(config, target);
  const editable = !!config?.editable;
  const providers = Array.isArray(config?.providers) ? config.providers : [];
  const elements = llmRouteElements(target);
  const provider = routePayload.provider || (target === "primary" ? config?.provider : "") || providers[0]?.id || "openai_compatible";
  const option = providers.find((item) => item.id === provider) || {};
  const modelOptions = llmVisibleModelOptions(routePayload, option);

  if (elements.enabledInput) {
    elements.enabledInput.checked = !!routePayload.enabled;
    elements.enabledInput.disabled = !editable;
  }
  if (elements.providerSelect) {
    elements.providerSelect.innerHTML = providers.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label || item.id)}</option>`).join("");
    elements.providerSelect.value = provider;
    elements.providerSelect.disabled = !editable;
  }
  if (elements.baseUrlInput) {
    elements.baseUrlInput.value = routePayload.base_url || option.base_url || "";
    elements.baseUrlInput.disabled = !editable;
    elements.baseUrlInput.placeholder = llmBaseUrlPlaceholder(provider);
  }
  if (elements.flashModelInput) {
    elements.flashModelInput.value = routePayload.flash_model || option.flash_model || "";
    elements.flashModelInput.disabled = !editable;
  }
  renderLlmModelSelect(elements.flashModelSelect, elements.flashModelInput?.value || routePayload.flash_model || "", modelOptions, editable);
  if (elements.proModelInput) {
    elements.proModelInput.value = routePayload.pro_model || option.pro_model || "";
    elements.proModelInput.disabled = !editable;
  }
  renderLlmModelSelect(elements.proModelSelect, elements.proModelInput?.value || routePayload.pro_model || "", modelOptions, editable);
  renderLlmReasoningSelect(
    elements.flashReasoningInput,
    routePayload.flash_reasoning_effort || option.flash_reasoning_effort || "",
    config?.reasoning_effort_options,
    editable,
  );
  renderLlmReasoningSelect(
    elements.proReasoningInput,
    routePayload.pro_reasoning_effort || option.pro_reasoning_effort || "",
    config?.reasoning_effort_options,
    editable,
  );
  if (elements.insecureTlsInput) {
    elements.insecureTlsInput.checked = !!routePayload.allow_insecure_tls;
    elements.insecureTlsInput.disabled = !editable;
  }
  if (elements.modelAvailabilityNote) {
    const availableModels = Array.isArray(routePayload.available_models) ? routePayload.available_models : [];
    if (availableModels.length) {
      elements.modelAvailabilityNote.innerHTML = availableModels.map((model) => `<span class="llm-model-chip">${escapeHtml(model)}</span>`).join("");
    } else if (routePayload.available_models_error) {
      elements.modelAvailabilityNote.textContent = `未读取到可用模型：${routePayload.available_models_error}`;
    } else {
      elements.modelAvailabilityNote.textContent = target === "fallback" ? "未读取到备选可用模型。" : "未读取到可用模型。";
    }
  }
  if (elements.input) {
    elements.input.value = "";
    elements.input.placeholder = routePayload.api_key_configured ? "已配置（输入新 Key 可覆盖；留空保留）" : "请输入 API Key";
    elements.input.disabled = !editable;
  }
  if (elements.keyCurrent) {
    elements.keyCurrent.textContent = routePayload.api_key_configured
      ? `当前已保存 Key：${routePayload.api_key_masked || "已配置"}。输入新 Key 会覆盖；留空保存会保留原 Key。`
      : "当前未保存 Key。";
  }
  if (elements.testButton) {
    elements.testButton.textContent = target === "fallback" ? "测试备选浅任务" : "测试浅任务";
    elements.testButton.title = editable ? `${target === "fallback" ? "测试备选链路的浅任务模型" : "测试浅任务入口使用的模型"}` : "当前会话不可测试";
  }
  if (elements.proTestButton) {
    elements.proTestButton.textContent = target === "fallback" ? "测试备选深度思考" : "测试深度思考";
    elements.proTestButton.title = editable ? `${target === "fallback" ? "测试备选链路的深度思考模型" : "测试深度思考入口使用的模型"}` : "当前会话不可测试";
  }
}

function renderLlmConfig() {
  const payload = state.llmConfig || {};
  const summary = document.getElementById("llm-config-summary");
  const providers = Array.isArray(payload.providers) ? payload.providers : [];
  const primaryProvider = payload.provider || providers[0]?.id || "openai_compatible";
  const primaryOption = providers.find((item) => item.id === primaryProvider) || {};
  const fallback = payload.fallback || {};
  if (summary) {
    summary.innerHTML = `
      <div><span>主供应商</span><strong>${escapeHtml(payload.route_display_label || payload.provider_label || primaryOption.label || primaryProvider)}</strong></div>
      <div><span>主 Key</span><strong>${payload.api_key_configured ? "已配置" : "未配置"}</strong></div>
      <div><span>备选开关</span><strong>${fallback.enabled ? "已启用" : "未启用"}</strong></div>
      <div><span>备选供应商</span><strong>${escapeHtml(fallback.route_display_label || fallback.provider_label || fallback.provider || "—")}</strong></div>
      <div><span>备选 Key</span><strong>${fallback.api_key_configured ? "已配置" : "未配置"}</strong></div>
      <div><span>主链路适配</span><strong>${escapeHtml(payload.adapter_label || payload.adapter_profile || "通用适配")}</strong></div>
      <div><span>备选适配</span><strong>${escapeHtml(fallback.adapter_label || fallback.adapter_profile || "—")}</strong></div>
      <div><span>编辑权限</span><strong>${payload.editable ? "所有用户可编辑" : "当前会话只读"}</strong></div>
    `;
  }
  renderLlmRouteForm(payload, "primary");
  renderLlmRouteForm(payload, "fallback");
  updateLlmInfoPanel();
  updateLlmTestButtonState();
  const saveButton = document.getElementById("llm-config-save");
  if (saveButton) {
    saveButton.disabled = !payload.editable;
    saveButton.title = payload.editable ? "保存主用与备选大模型配置" : "当前会话不可编辑";
  }
}

async function saveLlmConfig(event) {
  if (event) event.preventDefault();
  const primary = llmRouteElements("primary");
  const fallback = llmRouteElements("fallback");
  if (!primary.input || !primary.providerSelect) return;
  const payload = await apiJson("/api/system/llm-config", {
    method: "PUT",
    body: JSON.stringify({
      provider: primary.providerSelect.value,
      base_url: primary.baseUrlInput?.value.trim() || "",
      flash_model: primary.flashModelSelect?.value || primary.flashModelInput?.value.trim() || "",
      pro_model: primary.proModelSelect?.value || primary.proModelInput?.value.trim() || "",
      flash_reasoning_effort: primary.flashReasoningInput?.value || "",
      pro_reasoning_effort: primary.proReasoningInput?.value || "",
      allow_insecure_tls: !!primary.insecureTlsInput?.checked,
      api_key: primary.input.value.trim(),
      fallback_enabled: !!fallback.enabledInput?.checked,
      fallback_provider: fallback.providerSelect?.value || "",
      fallback_base_url: fallback.baseUrlInput?.value.trim() || "",
      fallback_flash_model: fallback.flashModelSelect?.value || fallback.flashModelInput?.value.trim() || "",
      fallback_pro_model: fallback.proModelSelect?.value || fallback.proModelInput?.value.trim() || "",
      fallback_flash_reasoning_effort: fallback.flashReasoningInput?.value || "",
      fallback_pro_reasoning_effort: fallback.proReasoningInput?.value || "",
      fallback_allow_insecure_tls: !!fallback.insecureTlsInput?.checked,
      fallback_api_key: fallback.input?.value.trim() || "",
    }),
  });
  if (payload.ok === false) {
    alert(payload.detail || "保存失败");
    return;
  }
  state.llmConfig = payload;
  renderLlmConfig();
  alert("主备大模型配置已保存。");
}

async function testLlmConfig(route = "flash", target = "primary") {
  const isPro = route === "pro";
  const elements = llmRouteElements(target);
  const routePayload = llmRoutePayload(state.llmConfig || {}, target);
  const model = isPro
    ? elements.proModelSelect?.value || elements.proModelInput?.value.trim()
    : elements.flashModelSelect?.value || elements.flashModelInput?.value.trim();
  const reasoningEffort = isPro ? elements.proReasoningInput?.value || "" : elements.flashReasoningInput?.value || "";
  const routeLabel = isPro ? "深度思考入口" : "浅任务入口";
  const targetLabel = target === "fallback" ? "备选链路" : "主用链路";
  const testButton = isPro ? elements.proTestButton : elements.testButton;
  if (testButton) {
    testButton.disabled = true;
    testButton.textContent = "测试中...";
  }
  try {
    const payload = await apiJson("/api/system/llm-config/test", {
      method: "POST",
      body: JSON.stringify({
        target,
        provider: elements.providerSelect?.value || routePayload.provider || "",
        route,
        base_url: elements.baseUrlInput?.value.trim() || "",
        model: model || "",
        reasoning_effort: reasoningEffort,
        allow_insecure_tls: !!elements.insecureTlsInput?.checked,
        api_key: elements.input?.value.trim() || "",
      }),
    });
    if (payload.ok) {
      alert(
        `连接成功\n链路: ${targetLabel}\n入口: ${routeLabel}\n供应商: ${escapeHtml(payload.route_display_label || payload.provider_label || payload.provider || "—")}\n模型: ${escapeHtml(payload.model || "—")}\n思考强度: ${llmReasoningLabel(payload.reasoning_effort || "")}\n协议: ${escapeHtml(payload.request_style || "—")}\nBase URL: ${escapeHtml(payload.base_url || "—")}`,
      );
    } else {
      alert(`连接失败: ${payload.message || "未知错误"}`);
    }
  } catch (error) {
    alert(`测试异常: ${error.message}`);
  } finally {
    if (testButton) {
      testButton.disabled = false;
      testButton.textContent = target === "fallback"
        ? (isPro ? "测试备选深度思考" : "测试备选浅任务")
        : (isPro ? "测试深度思考" : "测试浅任务");
      updateLlmTestButtonState();
    }
  }
}

function updateLlmInfoPanel() {
  const config = state.llmConfig || {};
  const providers = Array.isArray(config.providers) ? config.providers : [];
  const primary = llmRouteElements("primary");
  const fallback = llmRouteElements("fallback");
  const primaryProvider = primary.providerSelect?.value || config.provider || providers[0]?.id || "openai_compatible";
  const primaryOption = providers.find((item) => item.id === primaryProvider) || {};
  const fallbackPayload = llmRoutePayload(config, "fallback");
  const fallbackProvider = fallback.providerSelect?.value || fallbackPayload.provider || "";
  const fallbackOption = providers.find((item) => item.id === fallbackProvider) || {};

  setLlmInfoText("llm-info-provider", config.route_display_label || primaryOption.label || config.provider_label || primaryProvider || "—");
  setLlmInfoText("llm-info-flash-model", primary.flashModelSelect?.value || primary.flashModelInput?.value || primaryOption.flash_model || "—");
  setLlmInfoText("llm-info-flash-effort", llmReasoningLabel(primary.flashReasoningInput?.value || primaryOption.flash_reasoning_effort || ""));
  setLlmInfoText("llm-info-pro-model", primary.proModelSelect?.value || primary.proModelInput?.value || primaryOption.pro_model || "—");
  setLlmInfoText("llm-info-pro-effort", llmReasoningLabel(primary.proReasoningInput?.value || primaryOption.pro_reasoning_effort || ""));
  setLlmInfoText("llm-info-base-url", primary.baseUrlInput?.value || primaryOption.base_url || "—");
  setLlmInfoText("llm-info-tls", primary.insecureTlsInput?.checked ? "允许自签名证书" : "标准证书校验");
  setLlmInfoText("llm-info-adapter", routeAdapterText({
    routePayload: config,
    provider: primaryProvider,
    model: primary.flashModelSelect?.value || primary.flashModelInput?.value || primaryOption.flash_model || "",
    option: primaryOption,
  }));

  setLlmInfoText("llm-info-fallback-enabled", fallback.enabledInput?.checked ? "已启用自动切换" : "未启用");
  setLlmInfoText("llm-info-fallback-provider", fallbackPayload.route_display_label || fallbackOption.label || fallbackPayload.provider_label || fallbackProvider || "—");
  setLlmInfoText("llm-info-fallback-flash-model", fallback.flashModelSelect?.value || fallback.flashModelInput?.value || fallbackOption.flash_model || "—");
  setLlmInfoText("llm-info-fallback-pro-model", fallback.proModelSelect?.value || fallback.proModelInput?.value || fallbackOption.pro_model || "—");
  setLlmInfoText("llm-info-fallback-base-url", fallback.baseUrlInput?.value || fallbackOption.base_url || "—");
  setLlmInfoText("llm-info-fallback-tls", fallback.insecureTlsInput?.checked ? "允许自签名证书" : "标准证书校验");
  setLlmInfoText("llm-info-fallback-adapter", routeAdapterText({
    routePayload: fallbackPayload,
    provider: fallbackProvider,
    model: fallback.flashModelSelect?.value || fallback.flashModelInput?.value || fallbackOption.flash_model || "",
    option: fallbackOption,
  }));
}

function routeAdapterText({routePayload, provider, model, option}) {
  const label = routePayload?.adapter_label || routePayload?.adapter_profile || "";
  const notes = Array.isArray(routePayload?.adapter_notes) ? routePayload.adapter_notes.filter(Boolean) : [];
  if (label) return notes.length ? `${label}：${notes.slice(0, 2).join("；")}` : label;
  const text = `${provider || ""} ${model || option?.flash_model || ""}`.toLowerCase();
  if (text.includes("kimi")) return "Kimi 输出适配：JSON 代码块清洗 + 对象提取";
  if (text.includes("deepseek")) return "DeepSeek 备用适配：OpenAI-compatible + 通用 JSON 清洗";
  return "通用 JSON 适配";
}

function updateLlmRouteTestButtons(target = "primary") {
  const config = state.llmConfig || {};
  const providers = Array.isArray(config.providers) ? config.providers : [];
  const routePayload = llmRoutePayload(config, target);
  const elements = llmRouteElements(target);
  const provider = elements.providerSelect?.value || routePayload.provider || "";
  const option = providers.find((item) => item.id === provider) || {};
  const hasSavedKey = target === "fallback"
    ? !!routePayload.api_key_configured
    : (provider === config.provider ? !!config.api_key_configured : !!option.api_key_configured);
  const hasTypedKey = !!elements.input?.value.trim();
  const disabled = !config.editable || (!hasSavedKey && !hasTypedKey);
  if (elements.testButton) elements.testButton.disabled = disabled;
  if (elements.proTestButton) elements.proTestButton.disabled = disabled;
}

function updateLlmTestButtonState() {
  updateLlmRouteTestButtons("primary");
  updateLlmRouteTestButtons("fallback");
}

function setLlmInfoText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value || "—";
}

function setText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value || "—";
}

function llmReasoningLabel(value) {
  const key = String(value || "");
  return LLM_REASONING_EFFORT_LABELS[key] || key || "默认（不覆盖）";
}

function llmVisibleModelOptions(config, option) {
  const isActiveProvider = !option?.id || option.id === config?.provider;
  const availableModels = isActiveProvider && Array.isArray(config?.available_models) ? config.available_models : [];
  const configuredModels = Array.isArray(option?.model_options) ? option.model_options : [];
  const source = availableModels.length ? availableModels : configuredModels;
  return uniqueStrings(source);
}

function renderLlmModelSelect(select, value, models, editable) {
  if (!select) return;
  const normalizedModels = uniqueStrings([...(Array.isArray(models) ? models : []), value]);
  if (!normalizedModels.length) {
    select.innerHTML = `<option value="">暂无可用模型</option>`;
    select.value = "";
    select.disabled = true;
    return;
  }
  const current = String(value || "").trim();
  select.innerHTML = [
    `<option value="">选择模型</option>`,
    ...normalizedModels.map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`),
  ].join("");
  select.value = normalizedModels.includes(current) ? current : "";
  select.disabled = !editable;
}

function applyLlmModelSelect(route, target = "primary") {
  const isPro = route === "pro";
  const elements = llmRouteElements(target);
  const select = isPro ? elements.proModelSelect : elements.flashModelSelect;
  const input = isPro ? elements.proModelInput : elements.flashModelInput;
  if (!select || !input) return;
  input.value = select.value;
  updateLlmInfoPanel();
}

function syncLlmModelSelect(route, target = "primary") {
  const isPro = route === "pro";
  const elements = llmRouteElements(target);
  const select = isPro ? elements.proModelSelect : elements.flashModelSelect;
  const input = isPro ? elements.proModelInput : elements.flashModelInput;
  if (!select || !input) return;
  const typed = String(input.value || "").trim();
  const hasOption = Array.from(select.options || []).some((option) => option.value === typed);
  select.value = hasOption ? typed : "";
  updateLlmInfoPanel();
}

function renderLlmReasoningSelect(select, value, options, editable) {
  if (!select) return;
  const values = Array.isArray(options) && options.length ? options : ["", "minimal", "low", "medium", "high"];
  const merged = new Set(["", ...values.map((item) => String(item || "").trim()).filter((item) => item || item === ""), String(value || "").trim()]);
  select.innerHTML = Array.from(merged)
    .map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(llmReasoningLabel(item))}</option>`)
    .join("");
  select.value = String(value || "");
  select.disabled = !editable;
}

function uniqueStrings(items) {
  const seen = new Set();
  const result = [];
  (items || []).forEach((item) => {
    const value = String(item || "").trim();
    if (!value || seen.has(value)) return;
    seen.add(value);
    result.push(value);
  });
  return result;
}

function applyLlmProviderPreset(target = "primary") {
  const config = state.llmConfig || {};
  const providers = Array.isArray(config.providers) ? config.providers : [];
  const elements = llmRouteElements(target);
  if (!elements.providerSelect) return;
  const option = providers.find((item) => item.id === elements.providerSelect.value) || {};
  const routePayload = llmRoutePayload(config, target);
  if (elements.baseUrlInput) {
    elements.baseUrlInput.value = option.base_url || "";
    elements.baseUrlInput.placeholder = llmBaseUrlPlaceholder(elements.providerSelect.value);
  }
  const modelOptions = llmVisibleModelOptions(routePayload, option);
  if (elements.flashModelInput) elements.flashModelInput.value = option.flash_model || "";
  renderLlmModelSelect(elements.flashModelSelect, elements.flashModelInput?.value || option.flash_model || "", modelOptions, config.editable);
  if (elements.proModelInput) elements.proModelInput.value = option.pro_model || "";
  renderLlmModelSelect(elements.proModelSelect, elements.proModelInput?.value || option.pro_model || "", modelOptions, config.editable);
  renderLlmReasoningSelect(elements.flashReasoningInput, option.flash_reasoning_effort || "", config.reasoning_effort_options, config.editable);
  renderLlmReasoningSelect(elements.proReasoningInput, option.pro_reasoning_effort || "", config.reasoning_effort_options, config.editable);
  if (elements.insecureTlsInput) elements.insecureTlsInput.checked = !!option.allow_insecure_tls;
  updateLlmInfoPanel();
  updateLlmTestButtonState();
}

function toggleLlmApiKeyVisibility(target = "primary") {
  const input = document.getElementById(target === "fallback" ? "llm-fallback-api-key-input" : "llm-api-key-input");
  const button = document.getElementById(target === "fallback" ? "llm-fallback-toggle" : "llm-config-toggle");
  if (!input || !button) return;
  const isPassword = input.type === "password";
  input.type = isPassword ? "text" : "password";
  button.textContent = isPassword ? "隐藏" : "显示";
}

async function loadFeishuConfig() {
  const payload = await apiGet("/api/system/feishu-config");
  state.feishuConfig = payload;
  renderFeishuConfig();
}

function renderFeishuConfig() {
  const config = state.feishuConfig || {};
  const summary = document.getElementById("feishu-config-summary");
  const enabledInput = document.getElementById("feishu-enabled-input");
  const modeSelect = document.getElementById("feishu-mode-select");
  const receiveTypeSelect = document.getElementById("feishu-receive-id-type-select");
  const webhookUrlInput = document.getElementById("feishu-webhook-url-input");
  const webhookSecretInput = document.getElementById("feishu-webhook-secret-input");
  const appIdInput = document.getElementById("feishu-app-id-input");
  const appSecretInput = document.getElementById("feishu-app-secret-input");
  const defaultReceiveIdsInput = document.getElementById("feishu-default-receive-ids-input");
  const boundAccountsInput = document.getElementById("feishu-bound-accounts-input");
  const notifyHandoffInput = document.getElementById("feishu-notify-handoff-input");
  const notifyLogoutInput = document.getElementById("feishu-notify-logout-input");
  if (summary) {
    summary.innerHTML = `
      <div><span>通知状态</span><strong>${config.enabled ? "已启用" : "未启用"}</strong></div>
      <div><span>推送模式</span><strong>${escapeHtml(feishuModeLabel(config.mode))}</strong></div>
      <div><span>Webhook</span><strong>${config.webhook_url_configured ? "已配置" : "未配置"}</strong></div>
      <div><span>自建应用</span><strong>${config.app_id ? "已填写 App ID" : "未配置"}</strong></div>
    `;
  }
  if (enabledInput) enabledInput.checked = !!config.enabled;
  if (modeSelect) modeSelect.value = config.mode || "webhook";
  if (receiveTypeSelect) receiveTypeSelect.value = config.receive_id_type || "open_id";
  if (webhookUrlInput) {
    webhookUrlInput.value = "";
    webhookUrlInput.placeholder = config.webhook_url_configured ? `已保存：${config.webhook_url_masked || "****"}；留空保留` : "粘贴飞书群机器人 Webhook";
  }
  setText("feishu-webhook-url-current", config.webhook_url_configured ? `当前已保存：${config.webhook_url_masked || "****"}` : "当前未保存 Webhook 地址。");
  if (webhookSecretInput) {
    webhookSecretInput.value = "";
    webhookSecretInput.placeholder = config.webhook_secret_configured ? "已保存签名密钥；留空保留" : "可选；开启签名校验时填写";
  }
  setText("feishu-webhook-secret-current", config.webhook_secret_configured ? `当前已保存：${config.webhook_secret_masked || "****"}` : "当前未保存签名密钥。");
  if (appIdInput) appIdInput.value = config.app_id || "";
  if (appSecretInput) {
    appSecretInput.value = "";
    appSecretInput.placeholder = config.app_secret_configured ? "已保存 App Secret；留空保留" : "填写 App Secret";
  }
  setText("feishu-app-secret-current", config.app_secret_configured ? `当前已保存：${config.app_secret_masked || "****"}` : "当前未保存 App Secret。");
  if (defaultReceiveIdsInput) defaultReceiveIdsInput.value = (config.default_receive_ids || []).join("\n");
  if (boundAccountsInput) boundAccountsInput.value = formatFeishuBoundAccounts(config.bound_accounts || []);
  if (notifyHandoffInput) notifyHandoffInput.checked = config.notify_on_handoff !== false;
  if (notifyLogoutInput) notifyLogoutInput.checked = config.notify_on_logout !== false;
  updateFeishuInfoPanel();
}

function feishuModeLabel(mode) {
  if (mode === "app_bot") return "自建应用机器人";
  return "群机器人 Webhook";
}

function saveFeishuConfig(event) {
  if (event) event.preventDefault();
  return apiJson("/api/system/feishu-config", {
    method: "PUT",
    body: JSON.stringify(collectFeishuConfigPayload()),
  }).then((payload) => {
    if (payload.ok === false) {
      alert(payload.detail || "保存失败");
      return;
    }
    state.feishuConfig = payload;
    renderFeishuConfig();
    alert("飞书转人工通知配置已保存。");
  });
}

function collectFeishuConfigPayload() {
  return {
    enabled: !!document.getElementById("feishu-enabled-input")?.checked,
    mode: document.getElementById("feishu-mode-select")?.value || "webhook",
    receive_id_type: document.getElementById("feishu-receive-id-type-select")?.value || "open_id",
    webhook_url: document.getElementById("feishu-webhook-url-input")?.value.trim() || "",
    webhook_secret: document.getElementById("feishu-webhook-secret-input")?.value.trim() || "",
    app_id: document.getElementById("feishu-app-id-input")?.value.trim() || "",
    app_secret: document.getElementById("feishu-app-secret-input")?.value.trim() || "",
    default_receive_ids: parseLineList(document.getElementById("feishu-default-receive-ids-input")?.value || ""),
    bound_accounts: parseFeishuBoundAccounts(document.getElementById("feishu-bound-accounts-input")?.value || ""),
    notify_on_handoff: !!document.getElementById("feishu-notify-handoff-input")?.checked,
    notify_on_logout: !!document.getElementById("feishu-notify-logout-input")?.checked,
  };
}

async function testFeishuConfig(dryRun = false) {
  if (!dryRun && !confirm("测试连接会向飞书发送一条测试通知，确认继续吗？")) return;
  const button = document.getElementById(dryRun ? "feishu-config-test-dry" : "feishu-config-test");
  if (button) {
    button.disabled = true;
    button.textContent = dryRun ? "校验中..." : "测试中...";
  }
  try {
    const payload = await apiJson("/api/system/feishu-config/test", {
      method: "POST",
      body: JSON.stringify({...collectFeishuConfigPayload(), dry_run: dryRun}),
    });
    if (payload.ok) {
      alert(dryRun ? "飞书配置干跑校验通过。" : "飞书测试消息发送成功。");
    } else {
      alert(`飞书测试失败：${payload.errors?.join(", ") || payload.message || payload.status || payload.error || "未知错误"}`);
    }
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = dryRun ? "干跑校验" : "测试连接";
    }
  }
}

function updateFeishuInfoPanel() {
  const payload = collectFeishuConfigPayload();
  setText("feishu-info-enabled", payload.enabled ? "已启用" : "未启用");
  setText("feishu-info-mode", feishuModeLabel(payload.mode));
  setText(
    "feishu-info-webhook",
    state.feishuConfig?.webhook_url_configured || payload.webhook_url ? "已配置" : "未配置",
  );
  setText("feishu-info-app", payload.app_id ? payload.app_id : "未配置");
  setText("feishu-info-default-targets", String(payload.default_receive_ids.length || 0));
  setText("feishu-info-bound-targets", String(payload.bound_accounts.length || 0));
}

function parseLineList(text) {
  return uniqueStrings(String(text || "").split(/\r?\n|,/).map((line) => line.trim()));
}

function parseFeishuBoundAccounts(text) {
  return String(text || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const parts = line.split("|").map((part) => part.trim()).filter(Boolean);
      if (parts.length >= 4) {
        return {label: parts[0], tenant_id: parts[1], receive_id_type: parts[2], receive_id: parts.slice(3).join("|"), enabled: true};
      }
      if (parts.length === 3) {
        return {label: parts[0], tenant_id: "", receive_id_type: parts[1], receive_id: parts[2], enabled: true};
      }
      if (parts.length === 2) {
        return {label: parts[0], tenant_id: "", receive_id_type: document.getElementById("feishu-receive-id-type-select")?.value || "open_id", receive_id: parts[1], enabled: true};
      }
      return {label: parts[0] || line, tenant_id: "", receive_id_type: document.getElementById("feishu-receive-id-type-select")?.value || "open_id", receive_id: parts[0] || line, enabled: true};
    });
}

function formatFeishuBoundAccounts(items) {
  return (items || [])
    .map((item) => `${item.label || item.receive_id || ""} | ${item.tenant_id || ""} | ${item.receive_id_type || "open_id"} | ${item.receive_id || ""}`)
    .join("\n");
}

async function loadCustomerService() {
  const [settingsPayload, runtimePayload, overviewPayload, sessionsPayload] = await Promise.all([
    apiGet("/api/customer-service/settings"),
    apiGet("/api/customer-service/runtime/status").catch(() => ({item: null})),
    apiGet("/api/knowledge/overview").catch(() => ({counts: {}})),
    apiGet("/api/customer-service/sessions").catch(() => ({items: []})),
  ]);
  state.customerService = settingsPayload.item || {};
  state.customerServiceRuntime = runtimePayload.item || state.customerServiceRuntime || {};
  state.customerServiceSessions = sessionsPayload.items || [];
  state.customerServiceSessionMeta = sessionsPayload || {};
  renderCustomerService(overviewPayload.counts || {});
  renderCustomerServiceRuntime();
}

function renderCustomerService(counts = {}) {
  const item = state.customerService || {};
  const settings = item.settings || {};
  const modeSelect = document.getElementById("customer-service-mode");
  if (modeSelect) {
    modeSelect.innerHTML = (item.reply_modes || [])
      .map((mode) => `<option value="${escapeHtml(mode.id)}">${escapeHtml(mode.label)}</option>`)
      .join("");
    modeSelect.value = settings.reply_mode || "manual_assist";
  }
  const brainModeSelect = document.getElementById("customer-brain-mode");
  if (brainModeSelect) {
    const brainModes = (item.customer_service_brain_modes || []).length
      ? item.customer_service_brain_modes
      : [{id: "brain_first", label: "客服大脑优先：由大模型先思考再回复"}];
    brainModeSelect.innerHTML = brainModes
      .map((mode) => `<option value="${escapeHtml(mode.id)}">${escapeHtml(mode.label)}</option>`)
      .join("");
    brainModeSelect.value = "brain_first";
    brainModeSelect.disabled = true;
    brainModeSelect.title = "旧兼容模式已隐藏，客服回复固定走 Brain First。";
  }
  setChecked("customer-service-enabled", Boolean(settings.enabled));
  setChecked("customer-record-messages", settings.record_messages !== false);
  setChecked("customer-auto-learn", settings.auto_learn !== false);
  setChecked("customer-use-llm", settings.use_llm !== false);
  setChecked("customer-rag-enabled", settings.rag_enabled !== false);
  setChecked("customer-data-capture", settings.data_capture_enabled !== false);
  setChecked("customer-handoff", settings.handoff_enabled !== false);
  setChecked("customer-operator-alert", settings.operator_alert_enabled !== false);
  setChecked("customer-identity-guard", settings.identity_guard_enabled !== false);
  setChecked("customer-style-adapter", settings.style_adapter_enabled !== false);
  setChecked("customer-final-polish", settings.final_visible_llm_polish_enabled !== false);
  setChecked("customer-respond-all-unread", settings.respond_all_unread_sessions === true);
  document.getElementById("customer-service-status").textContent = item.status || "未配置";
  const enabledSessions = (state.customerServiceSessions || []).filter((session) => session.enabled);
  document.getElementById("customer-service-cards").innerHTML = `
    <div class="metric-card"><span>${settings.enabled ? "开" : "关"}</span><label>客服开关</label></div>
    <div class="metric-card"><span>${escapeHtml(modeSelect?.selectedOptions?.[0]?.textContent || "未选择")}</span><label>当前模式</label></div>
    <div class="metric-card"><span>${enabledSessions.length}</span><label>监听会话</label></div>
    <div class="metric-card"><span>${counts.raw_messages ?? 0}</span><label>已记录消息</label></div>
  `;
  renderCustomerServiceNewSessionPolicyButtons();
  renderCustomerServiceSessionSummary();
  renderCustomerServiceSessionList();
  renderCustomerServiceRuntime();
}

function renderCustomerServiceNewSessionPolicyButtons() {
  const settings = (state.customerService || {}).settings || {};
  const respondAllUnread = settings.respond_all_unread_sessions === true;
  const enableButton = document.getElementById("customer-service-select-all");
  const disableButton = document.getElementById("customer-service-clear-selection");
  if (enableButton) {
    enableButton.classList.toggle("customer-policy-active", respondAllUnread);
  }
  if (disableButton) {
    disableButton.classList.toggle("customer-policy-active", !respondAllUnread);
  }
}

function renderCustomerServiceSessionSummary() {
  const panel = document.getElementById("customer-service-session-summary");
  if (!panel) return;
  const settings = (state.customerService || {}).settings || {};
  const sessions = state.customerServiceSessions || [];
  const enabled = sessions.filter((item) => item.enabled);
  const ignored = sessions.filter((item) => !item.enabled);
  const unread = sessions.filter((item) => item.unread_detected);
  const previewNames = enabled
    .slice(0, 6)
    .map((item) => item.display_name || item.name)
    .filter(Boolean);
  const more = enabled.length > previewNames.length ? ` 等 ${enabled.length} 个` : "";
  const defaultText = settings.respond_all_unread_sessions
    ? "已开启“响应所有未读会话”：新会话默认响应。"
    : "未开启“响应所有未读会话”：新会话默认忽略。";
  panel.innerHTML = `
    <strong>当前监听会话：${escapeHtml(String(enabled.length))} 个（忽略 ${escapeHtml(String(ignored.length))} 个）</strong>
    <span>${enabled.length ? `已选择：${escapeHtml(previewNames.join("、"))}${escapeHtml(more)}` : "尚未选择监听会话，请先识别会话后勾选。"} ${escapeHtml(defaultText)}${unread.length ? ` 当前有 ${unread.length} 个会话检测到未读变化。` : ""}</span>
  `;
}

function customerServiceConversationTypeLabel(value) {
  if (value === "group") return "群聊";
  if (value === "private") return "私聊";
  if (value === "file_transfer") return "文件传输";
  if (value === "system") return "系统";
  return "未知";
}

function renderCustomerServiceSessionList() {
  const list = document.getElementById("customer-service-session-list");
  if (!list) return;
  const items = [...(state.customerServiceSessions || [])];
  if (!items.length) {
    list.innerHTML = `<div class="empty-state">尚未识别到会话。点击“识别会话”后，再勾选需要自动回复的对象。</div>`;
    return;
  }
  items.sort((a, b) => {
    const enabledGap = Number(Boolean(b.enabled)) - Number(Boolean(a.enabled));
    if (enabledGap !== 0) return enabledGap;
    const unreadGap = Number(Boolean(b.unread_detected)) - Number(Boolean(a.unread_detected));
    if (unreadGap !== 0) return unreadGap;
    const scoreGap = Number(b.priority_score || 0) - Number(a.priority_score || 0);
    if (scoreGap !== 0) return scoreGap;
    return String(a.display_name || a.name || "").localeCompare(String(b.display_name || b.name || ""), "zh-Hans-CN");
  });
  list.innerHTML = items.map((item) => {
    const name = item.name || "";
    const selected = Boolean(item.enabled);
    const unread = Boolean(item.unread_detected);
    const title = item.display_name || name || "未命名会话";
    const statusLine = unread
      ? `检测到新变化 · ${item.last_message_time || "等待时间"}`
      : `最近时间：${item.last_message_time || "暂无"}`;
    const badges = [
      {key: item.conversation_type || "unknown", label: customerServiceConversationTypeLabel(item.conversation_type), tone: "info"},
      selected ? {key: "enabled", label: "自动回复", tone: "ok"} : {key: "disabled", label: "忽略", tone: "muted"},
      ...(unread ? [{key: "unread", label: "未读变化", tone: "warning"}] : []),
    ];
    return `
      <div class="record-row recorder-row${selected ? " is-selected" : ""}">
        <button class="link-button">
          <strong>${escapeHtml(title)}</strong>
          <span>${escapeHtml(statusLine)}</span>
          ${badgeListHtml(badges)}
        </button>
        <div class="inline-actions">
          <label class="checkbox-line">
            <input class="customer-session-toggle" type="checkbox" data-name="${escapeAttr(name)}" ${selected ? "checked" : ""} />
            自动回复
          </label>
        </div>
      </div>
    `;
  }).join("");
  list.querySelectorAll(".customer-session-toggle").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const name = checkbox.dataset.name || "";
      updateCustomerServiceSession(name, {enabled: checkbox.checked}).catch((error) => alert(error.message));
    });
  });
}

async function refreshCustomerServiceRuntime(options = {}) {
  if (!hasLocalConsoleAccess()) return;
  try {
    const payload = await apiGet("/api/customer-service/runtime/status");
    state.customerServiceRuntime = payload.item || {};
    if (!options.skipRender) renderCustomerServiceRuntime();
  } catch (error) {
    if (!options.silent) console.warn(error);
  }
}

async function refreshRecorderRuntime(options = {}) {
  if (!hasLocalConsoleAccess()) return;
  try {
    const payload = await apiGet("/api/recorder/runtime/status");
    state.recorderRuntimeStatus = payload.item || {};
    if (!options.skipRender) renderCustomerServiceRuntime();
  } catch (error) {
    if (!options.silent) console.warn(error);
  }
}

function renderCustomerServiceRuntime() {
  const customerRuntime = state.customerServiceRuntime || {};
  const customerStateName = customerRuntime.state || "stopped";
  const customerStateMessage = sanitizeRuntimeMessage(customerRuntime.message || "");
  const customerVisualStateName = runtimeVisualStateName(customerStateName, Boolean(customerRuntime.running));
  const customerVisualStateLabel = runtimeVisualStateLabel(customerVisualStateName);
  const customerVisualStateMessage = runtimeVisualStateMessage(customerStateName, customerStateMessage, Boolean(customerRuntime.running));
  const customerDotClasses = `service-state-dot is-${escapeHtml(customerVisualStateName)}`;
  const recorderRuntime = state.recorderRuntimeStatus || {};
  const recorderSettings = state.recorderSummary?.settings || {};
  const recorderRunning = Boolean(recorderRuntime.running);
  const recorderRawStateName = recorderRuntime.state || (recorderRunning ? "idle" : "stopped");
  const recorderVisualStateName = runtimeVisualStateName(recorderRawStateName, recorderRunning);
  const recorderStateLabel = recorderRuntimeFloatLabel(recorderVisualStateName);
  const recorderDotClasses = `service-state-dot is-${escapeHtml(recorderVisualStateName)}`;
  const anyRunning = Boolean(customerRuntime.running || recorderRunning);
  let compositeStateName = "stopped";
  if (customerVisualStateName === "paused" || recorderVisualStateName === "paused") {
    compositeStateName = "paused";
  } else if (anyRunning) {
    compositeStateName = "idle";
  }
  const recorderEnabled = recorderSettings.enabled !== false;
  const panel = document.getElementById("customer-service-runtime-card");
  if (panel) {
    panel.className = `runtime-status-card is-${customerVisualStateName}`;
    panel.innerHTML = `
      <div class="runtime-status-main">
        <span class="${customerDotClasses}"></span>
        <div>
          <strong>${escapeHtml(customerVisualStateLabel)}</strong>
          <p>${escapeHtml(customerVisualStateMessage || "等待状态更新")}</p>
          ${customerRuntime.last_target ? `<small>最近会话：${escapeHtml(customerRuntime.last_target)}${customerRuntime.model_tier ? ` · 模型：${escapeHtml(customerRuntime.model_tier)}` : ""}${customerRuntime.rag_hit_count !== undefined && customerRuntime.rag_hit_count !== null ? ` · 知识命中：${escapeHtml(String(customerRuntime.rag_hit_count))}` : ""}</small>` : ""}
        </div>
      </div>
    `;
  }
  const floating = document.getElementById("customer-service-float");
  if (floating) {
    floating.className = `customer-service-float is-${compositeStateName}`;
    floating.innerHTML = `
      <div class="float-header float-drag-handle" title="按住拖动浮窗位置">
        <span class="float-orb" aria-hidden="true">${customerServiceSpinnerSvg()}</span>
        <strong>运行控制台</strong>
        <span class="float-hotkey-inline">按F8启动/停止</span>
      </div>
      <div class="float-runtime-list">
        <div class="float-runtime-row">
          <div class="float-status-line">
            <span class="${customerDotClasses}"></span>
            <strong>客服 · ${escapeHtml(customerVisualStateLabel)}</strong>
          </div>
          <div class="float-actions">
            <button class="primary-button iconish-button customer-runtime-start" title="启动微信自动客服" ${!hasLocalConsoleAccess() || customerRuntime.running || state.customerServiceRuntimeBusy ? "disabled" : ""}>开</button>
            <button class="secondary-button iconish-button customer-runtime-stop" title="停止微信自动客服" ${!customerRuntime.running || state.customerServiceRuntimeBusy ? "disabled" : ""}>停</button>
          </div>
        </div>
        <div class="float-runtime-row">
          <div class="float-status-line">
            <span class="${recorderDotClasses}"></span>
            <strong>记录 · ${escapeHtml(recorderStateLabel)}</strong>
          </div>
          <div class="float-actions">
            <button class="primary-button iconish-button recorder-runtime-start" title="启动AI智能记录员监听" ${!hasLocalConsoleAccess() || !recorderEnabled || recorderRunning || state.recorderRuntimeBusy ? "disabled" : ""}>开</button>
            <button class="secondary-button iconish-button recorder-runtime-stop" title="停止AI智能记录员监听" ${!recorderRunning || state.recorderRuntimeBusy ? "disabled" : ""}>停</button>
          </div>
        </div>
      </div>
    `;
    floating.querySelector(".customer-runtime-start")?.addEventListener("click", () => startCustomerServiceRuntime().catch((error) => alert(error.message)));
    floating.querySelector(".customer-runtime-stop")?.addEventListener("click", () => stopCustomerServiceRuntime().catch((error) => alert(error.message)));
    floating.querySelector(".recorder-runtime-start")?.addEventListener("click", () => startRecorderRuntime().catch((error) => alert(error.message)));
    floating.querySelector(".recorder-runtime-stop")?.addEventListener("click", () => stopRecorderRuntime().catch((error) => alert(error.message)));
    enableCustomerServiceFloatDragging(floating);
  }
  const otherRunning = (customerRuntime.other_listeners || []).filter((l) => l.tenant_id !== (state.activeTenantId || "default"));
  if (!customerRuntime.running && otherRunning.length > 0) {
    const tenantNames = otherRunning.map((l) => l.tenant_id).join("、");
    const banner = document.getElementById("runtime-tenant-mismatch-banner");
    if (banner) {
      banner.classList.remove("is-hidden");
      banner.textContent = `注意：当前账号没有运行中的监听，但以下账号正在运行：${tenantNames}。如需查看其状态，请切换账号。`;
    }
  } else {
    const banner = document.getElementById("runtime-tenant-mismatch-banner");
    if (banner) banner.classList.add("is-hidden");
  }
}

function clampCustomerServiceFloatPosition(floating, position) {
  const margin = 8;
  const rect = floating.getBoundingClientRect();
  const width = Math.max(220, rect.width || floating.offsetWidth || 0);
  const height = Math.max(72, rect.height || floating.offsetHeight || 0);
  const maxLeft = Math.max(margin, window.innerWidth - width - margin);
  const maxTop = Math.max(margin, window.innerHeight - height - margin);
  const left = Math.min(maxLeft, Math.max(margin, Number(position?.left) || margin));
  const top = Math.min(maxTop, Math.max(margin, Number(position?.top) || margin));
  return {left, top};
}

function applyCustomerServiceFloatPosition(floating, position, {persist = false} = {}) {
  if (!position) return;
  const bounded = clampCustomerServiceFloatPosition(floating, position);
  floating.style.left = `${Math.round(bounded.left)}px`;
  floating.style.top = `${Math.round(bounded.top)}px`;
  floating.style.right = "auto";
  floating.style.bottom = "auto";
  state.customerServiceFloatPosition = bounded;
  if (persist) {
    saveFloatPosition(CUSTOMER_SERVICE_FLOAT_POSITION_KEY, bounded);
  }
}

function clampCustomerServiceFloatInViewport() {
  const floating = document.getElementById("customer-service-float");
  if (!floating || !state.customerServiceFloatPosition) return;
  applyCustomerServiceFloatPosition(floating, state.customerServiceFloatPosition, {persist: true});
}

function enableCustomerServiceFloatDragging(floating) {
  if (!floating) return;
  if (state.customerServiceFloatPosition) {
    applyCustomerServiceFloatPosition(floating, state.customerServiceFloatPosition, {persist: false});
  }
  if (floating.dataset.dragBound === "1") return;
  floating.dataset.dragBound = "1";
  floating.addEventListener("pointerdown", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target || !floating.contains(target)) return;
    if (!target.closest(".float-drag-handle")) return;
    if (target.closest("button, a, input, textarea, select, label, [role='button'], .float-actions")) return;
    if (event.button !== 0) return;
    event.preventDefault();
    const dragRect = floating.getBoundingClientRect();
    const pointerId = event.pointerId;
    const offsetX = event.clientX - dragRect.left;
    const offsetY = event.clientY - dragRect.top;
    floating.classList.add("is-dragging");
    if (floating.setPointerCapture) {
      try {
        floating.setPointerCapture(pointerId);
      } catch {
        // ignored
      }
    }
    const onMove = (moveEvent) => {
      if (moveEvent.pointerId !== pointerId) return;
      const next = {left: moveEvent.clientX - offsetX, top: moveEvent.clientY - offsetY};
      applyCustomerServiceFloatPosition(floating, next, {persist: false});
    };
    const onStop = (stopEvent) => {
      if (stopEvent.pointerId !== pointerId) return;
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onStop);
      document.removeEventListener("pointercancel", onStop);
      floating.classList.remove("is-dragging");
      if (floating.releasePointerCapture) {
        try {
          floating.releasePointerCapture(pointerId);
        } catch {
          // ignored
        }
      }
      if (state.customerServiceFloatPosition) {
        saveFloatPosition(CUSTOMER_SERVICE_FLOAT_POSITION_KEY, state.customerServiceFloatPosition);
      }
    };
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onStop);
    document.addEventListener("pointercancel", onStop);
  });
}

function initCustomerServiceFloat() {
  const floating = document.getElementById("customer-service-float");
  if (!floating) return;
  const dragHandle = floating.querySelector(".float-header, .float-status-line, .float-drag-handle");
  if (dragHandle && !dragHandle.classList.contains("float-drag-handle")) {
    dragHandle.classList.add("float-drag-handle");
    dragHandle.setAttribute("title", "按住拖动浮窗位置");
  }
  enableCustomerServiceFloatDragging(floating);
}

function runtimeVisualStateName(stateName, running) {
  if (stateName === "paused") return "paused";
  if (stateName === "stopped" || !running) return "stopped";
  return "idle";
}

function runtimeVisualStateLabel(stateName) {
  if (stateName === "paused") return "已暂停";
  if (stateName === "stopped") return "已停止";
  return "运行中";
}

function recorderRuntimeFloatLabel(stateName) {
  if (stateName === "paused") return "已暂停";
  if (stateName === "stopped") return "已停止";
  return "监听中";
}

function runtimeVisualStateMessage(rawStateName, message, running) {
  if (rawStateName === "paused") return message || "等待继续";
  if (rawStateName === "stopped" || !running) return message || "未启动";
  return "监听运行中";
}

function sanitizeRuntimeMessage(message) {
  const text = String(message || "").trim();
  if (!text) return "";
  if (text.includes("双击停止指令")) return "已停止";
  if (text.includes("自动客服监听已停止")) return "已停止";
  return text;
}

function customerServiceSpinnerSvg() {
  // Adapted from n3r4zzurr0/svg-spinners ring-resize.svg (MIT, Copyright Utkarsh Verma).
  return `
    <svg class="float-orb-svg" width="24" height="24" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" focusable="false">
      <g class="float-orb-ring">
        <circle cx="12" cy="12" r="9.5" fill="none" stroke-width="3"></circle>
      </g>
    </svg>
  `;
}

async function startCustomerServiceRuntime() {
  state.customerServiceRuntimeBusy = true;
  renderCustomerServiceRuntime();
  try {
    const payload = await apiJson("/api/customer-service/runtime/start", {method: "POST", body: JSON.stringify({})});
    if (payload.ok === false) throw new Error(payload.message || "自动客服启动失败");
    state.customerServiceRuntime = payload.item || {};
  } finally {
    state.customerServiceRuntimeBusy = false;
    await refreshCustomerServiceRuntime({silent: true});
  }
}

async function stopCustomerServiceRuntime() {
  state.customerServiceRuntimeBusy = true;
  renderCustomerServiceRuntime();
  try {
    const payload = await apiJson("/api/customer-service/runtime/stop", {method: "POST", body: JSON.stringify({})});
    if (payload.ok === false) throw new Error(payload.message || "自动客服停止失败");
    state.customerServiceRuntime = payload.item || {};
  } finally {
    state.customerServiceRuntimeBusy = false;
    await refreshCustomerServiceRuntime({silent: true});
  }
}

function scheduleCustomerServiceRuntimePolling() {
  if (state.customerServiceRuntimeTimer) clearInterval(state.customerServiceRuntimeTimer);
  if (!hasLocalConsoleAccess()) return;
  const refreshBothRuntimeStatuses = async () => {
    await Promise.all([
      refreshCustomerServiceRuntime({silent: true, skipRender: true}),
      refreshRecorderRuntime({silent: true, skipRender: true}),
    ]);
    renderCustomerServiceRuntime();
  };
  refreshBothRuntimeStatuses().catch((error) => console.warn(error));
  state.customerServiceRuntimeTimer = setInterval(() => refreshBothRuntimeStatuses().catch((error) => console.warn(error)), 1000);
}

async function saveCustomerServiceSettings() {
  const payload = await apiJson("/api/customer-service/settings", {
    method: "PUT",
    body: JSON.stringify({
      enabled: document.getElementById("customer-service-enabled")?.checked,
      reply_mode: document.getElementById("customer-service-mode")?.value || "manual_assist",
      record_messages: document.getElementById("customer-record-messages")?.checked,
      auto_learn: document.getElementById("customer-auto-learn")?.checked,
      use_llm: document.getElementById("customer-use-llm")?.checked,
      rag_enabled: document.getElementById("customer-rag-enabled")?.checked,
      data_capture_enabled: document.getElementById("customer-data-capture")?.checked,
      handoff_enabled: document.getElementById("customer-handoff")?.checked,
      operator_alert_enabled: document.getElementById("customer-operator-alert")?.checked,
      identity_guard_enabled: document.getElementById("customer-identity-guard")?.checked,
      style_adapter_enabled: document.getElementById("customer-style-adapter")?.checked,
      final_visible_llm_polish_enabled: document.getElementById("customer-final-polish")?.checked,
      customer_service_brain_mode: "brain_first",
      respond_all_unread_sessions: document.getElementById("customer-respond-all-unread")?.checked,
    }),
  });
  state.customerService = payload.item || {};
  await refreshCustomerServiceSessions({silent: true});
  renderCustomerService((state.overview || {}).counts || {});
}

async function refreshCustomerServiceSessions(options = {}) {
  if (!hasLocalConsoleAccess()) return;
  try {
    const payload = await apiGet("/api/customer-service/sessions");
    state.customerServiceSessions = payload.items || [];
    state.customerServiceSessionMeta = payload || {};
    if (typeof payload.respond_all_unread_sessions === "boolean") {
      state.customerService = {
        ...(state.customerService || {}),
        settings: {
          ...((state.customerService || {}).settings || {}),
          respond_all_unread_sessions: payload.respond_all_unread_sessions,
        },
      };
    }
    renderCustomerServiceSessionSummary();
    renderCustomerServiceSessionList();
    renderCustomerServiceNewSessionPolicyButtons();
  } catch (error) {
    if (!options.silent) console.warn(error);
  }
}

async function discoverCustomerServiceSessions() {
  const button = document.getElementById("customer-service-discover");
  if (button) {
    button.disabled = true;
    button.textContent = "识别中...";
  }
  try {
    const payload = await apiJson("/api/customer-service/sessions/discover", {method: "POST", body: "{}"});
    if (payload.ok === false) {
      throw new Error(payload.message || "识别会话失败");
    }
    state.customerServiceSessions = payload.items || [];
    state.customerServiceSessionMeta = payload || {};
    state.customerService = {
      ...(state.customerService || {}),
      settings: {
        ...((state.customerService || {}).settings || {}),
        respond_all_unread_sessions: payload.respond_all_unread_sessions === true,
        session_targets_managed: payload.session_targets_managed === true,
      },
    };
    renderCustomerServiceSessionSummary();
    renderCustomerServiceSessionList();
    const added = Number(payload.added_count || 0);
    const archivedCount = Number(payload.archived_count || 0);
    const warnings = Array.isArray(payload.warnings) ? payload.warnings.filter(Boolean) : [];
    const baseMessage = added > 0
      ? `识别完成，新增 ${added} 个会话，归档旧会话 ${archivedCount} 个。`
      : `识别完成，会话列表已更新，归档旧会话 ${archivedCount} 个。`;
    alert(warnings.length ? `${baseMessage}\n${warnings.join("\n")}` : baseMessage);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "识别会话";
    }
  }
}

async function updateCustomerServiceSession(name, patch) {
  if (!name) return;
  const payload = await apiJson(`/api/customer-service/sessions/${encodeURIComponent(name)}`, {
    method: "PATCH",
    body: JSON.stringify(patch || {}),
  });
  state.customerServiceSessions = payload.items || state.customerServiceSessions || [];
  if (payload.settings) {
    state.customerService = {
      ...(state.customerService || {}),
      settings: payload.settings,
    };
  }
  renderCustomerServiceSessionSummary();
  renderCustomerServiceSessionList();
  const warnings = Array.isArray(payload.warnings) ? payload.warnings.filter(Boolean) : [];
  if (warnings.length) {
    alert(warnings.join("\n"));
  }
}

async function applyCustomerServiceSessionSelection(mode = "all") {
  const enableNewSessionsByDefault = mode === "all";
  const payload = await apiJson("/api/customer-service/settings", {
    method: "PUT",
    body: JSON.stringify({
      respond_all_unread_sessions: enableNewSessionsByDefault,
    }),
  });
  state.customerService = payload.item || state.customerService || {};
  setChecked("customer-respond-all-unread", enableNewSessionsByDefault);
  await refreshCustomerServiceSessions({silent: true});
  renderCustomerService((state.overview || {}).counts || {});
  alert(
    mode === "all"
      ? "已设置为“新会话默认响应”。当前列表里的勾选状态保持不变。"
      : "已设置为“新会话默认忽略”。当前列表里的勾选状态保持不变。"
  );
}

async function loadCustomerProfiles() {
  if (!hasLocalConsoleAccess()) return;
  try {
    const payload = await apiGet("/api/customers");
    state.customerProfiles = payload.items || [];
    renderCustomerProfileList();
    if (state.selectedCustomerProfile) {
      const refreshed = state.customerProfiles.find((p) => p.profile_id === state.selectedCustomerProfile.profile_id);
      if (refreshed) {
        state.selectedCustomerProfile = refreshed;
        renderCustomerProfileDetail();
      }
    }
  } catch (error) {
    console.warn(error);
  }
}

function renderCustomerProfileList() {
  const container = document.getElementById("customer-profile-list");
  if (!container) return;
  const search = (document.getElementById("customer-profile-search")?.value || "").toLowerCase().trim();
  let items = state.customerProfiles || [];
  if (search) {
    items = items.filter((p) => {
      const name = (p.display_name || p.target_name || "").toLowerCase();
      const tags = JSON.stringify(p.tags || {}).toLowerCase();
      return name.includes(search) || tags.includes(search);
    });
  }
  if (!items.length) {
    container.innerHTML = `<div class="status-card info"><strong>暂无客户档案</strong><span>客户首次发消息后会自动创建。</span></div>`;
    return;
  }
  container.innerHTML = items.map((p) => {
    const name = escapeHtml(p.display_name || p.target_name || "未命名");
    const basic = p.basic_info || {};
    const msgCount = basic.total_messages || 0;
    const replyCount = basic.total_replies || 0;
    const tags = p.tags || {};
    const tagPills = Object.entries(tags).slice(0, 3).map(([k, v]) => `<span class="tag-pill">${escapeHtml(String(k))}:${escapeHtml(String(v))}</span>`).join("");
    const isSelected = state.selectedCustomerProfile?.profile_id === p.profile_id;
    return `
      <div class="profile-list-item ${isSelected ? "is-selected" : ""}" data-profile-id="${escapeHtml(p.profile_id || "")}">
        <div class="profile-list-name">${name}</div>
        <div class="profile-list-meta">
          <span>消息 ${msgCount}</span>
          <span>回复 ${replyCount}</span>
        </div>
        <div class="profile-list-tags">${tagPills}</div>
      </div>
    `;
  }).join("");
  container.querySelectorAll(".profile-list-item").forEach((el) => {
    el.addEventListener("click", () => selectCustomerProfile(el.dataset.profileId));
  });
}

async function selectCustomerProfile(profileId) {
  const profile = state.customerProfiles.find((p) => p.profile_id === profileId);
  if (!profile) return;
  state.selectedCustomerProfile = profile;
  state.customerProfileMessages = [];
  renderCustomerProfileList();
  renderCustomerProfileDetail();
  try {
    const msgPayload = await apiGet(`/api/customers/${encodeURIComponent(profileId)}/messages?limit=100`);
    state.customerProfileMessages = msgPayload.items || [];
    renderCustomerProfileMessages();
  } catch (error) {
    console.warn(error);
  }
}

function renderCustomerProfileDetail() {
  const container = document.getElementById("customer-profile-detail");
  if (!container) return;
  const p = state.selectedCustomerProfile;
  if (!p) {
    container.innerHTML = `<div class="status-card info"><strong>选择左侧客户查看详情</strong></div>`;
    return;
  }
  const basic = p.basic_info || {};
  const tags = p.tags || {};
  const tagPills = Object.entries(tags).map(([k, v]) => `
    <span class="tag-pill is-editable" data-tag-key="${escapeHtml(k)}">
      ${escapeHtml(String(k))}: ${escapeHtml(String(v))}
      <button class="tag-remove" data-tag-key="${escapeHtml(k)}" title="删除">×</button>
    </span>
  `).join("");
  container.innerHTML = `
    <div class="profile-detail-card">
      <div class="profile-detail-header">
        <h3>${escapeHtml(p.display_name || p.target_name || "未命名")}</h3>
        <div class="profile-detail-status">${escapeHtml(p.status || "active")}</div>
      </div>
      <div class="profile-detail-section">
        <h4>基础信息</h4>
        <div class="profile-detail-grid">
          <div><label>性别</label><span>${escapeHtml(basic.gender || "未知")} ${basic.gender_confidence ? `(${(basic.gender_confidence * 100).toFixed(0)}%)` : ""}</span></div>
          <div><label>地区</label><span>${escapeHtml(basic.region || "-")}</span></div>
          <div><label>首次联系</label><span>${escapeHtml(basic.first_contact_at || "-")}</span></div>
          <div><label>最近联系</label><span>${escapeHtml(basic.last_contact_at || "-")}</span></div>
          <div><label>总消息数</label><span>${basic.total_messages || 0}</span></div>
          <div><label>总回复数</label><span>${basic.total_replies || 0}</span></div>
        </div>
      </div>
      <div class="profile-detail-section">
        <h4>会话摘要</h4>
        <p class="profile-summary">${escapeHtml(p.conversation_summary || "暂无摘要")}</p>
      </div>
      <div class="profile-detail-section">
        <h4>标签</h4>
        <div class="tag-list">${tagPills || "<span class=\"muted\">暂无标签</span>"}</div>
        <div class="tag-add-row">
          <input id="new-tag-key" type="text" placeholder="标签名" class="small-input" />
          <input id="new-tag-value" type="text" placeholder="标签值" class="small-input" />
          <button class="secondary-button compact-button" id="add-customer-tag">添加</button>
        </div>
      </div>
      <div class="profile-detail-section">
        <h4>最近聊天记录</h4>
        <div class="message-timeline" id="customer-profile-messages">正在加载...</div>
      </div>
    </div>
  `;
  container.querySelectorAll(".tag-remove").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      removeCustomerTag(p.profile_id, btn.dataset.tagKey);
    });
  });
  container.querySelector("#add-customer-tag")?.addEventListener("click", () => {
    const key = document.getElementById("new-tag-key")?.value?.trim();
    const value = document.getElementById("new-tag-value")?.value?.trim();
    if (key) addCustomerTag(p.profile_id, key, value);
  });
  renderCustomerProfileMessages();
}

function renderCustomerProfileMessages() {
  const container = document.getElementById("customer-profile-messages");
  if (!container) return;
  const messages = state.customerProfileMessages || [];
  if (!messages.length) {
    container.innerHTML = `<div class="muted">暂无聊天记录</div>`;
    return;
  }
  container.innerHTML = messages.map((m) => {
    const sender = escapeHtml(m.sender || "客户");
    const content = escapeHtml(m.content || "");
    const time = escapeHtml(m.message_time || m.observed_at || "");
    const isSelf = m.sender_role === "self" || m.sender === "self";
    return `
      <div class="timeline-item ${isSelf ? "is-self" : "is-contact"}">
        <div class="timeline-meta">
          <span class="timeline-sender">${sender}</span>
          <span class="timeline-time">${time}</span>
        </div>
        <div class="timeline-content">${content}</div>
      </div>
    `;
  }).join("");
}

async function addCustomerTag(profileId, key, value) {
  try {
    await apiJson(`/api/customers/${encodeURIComponent(profileId)}/tags`, {
      method: "POST",
      body: JSON.stringify({key, value}),
    });
    await loadCustomerProfiles();
  } catch (error) {
    alert(error.message);
  }
}

async function removeCustomerTag(profileId, key) {
  try {
    await apiJson(`/api/customers/${encodeURIComponent(profileId)}/tags/${encodeURIComponent(key)}`, {method: "DELETE"});
    await loadCustomerProfiles();
  } catch (error) {
    alert(error.message);
  }
}

async function loadOverview() {
  const [knowledge, system] = await Promise.all([
    apiGet("/api/knowledge/overview"),
    apiGet("/api/system/status").catch(() => ({ok: false})),
  ]);
  state.overview = knowledge;
  const counts = knowledge.counts || {};
  document.getElementById("metric-knowledge-total").textContent = counts.knowledge_total ?? "-";
  document.getElementById("metric-candidates").textContent = counts.pending_candidates ?? "-";
  updateCandidateCountBadge(counts.pending_candidates ?? 0);
  document.getElementById("metric-diagnostics").textContent = system.ok ? "正常" : "待查";
  document.getElementById("overview-cards").innerHTML = `
    <div class="metric-card"><span>${counts.knowledge_total ?? 0}</span><label>总知识条数</label></div>
    <div class="metric-card"><span>${counts.formal_knowledge_total ?? 0}</span><label>正式知识</label></div>
    <div class="metric-card"><span>${counts.chats ?? counts.style_examples ?? 0}</span><label>聊天话术与记录</label></div>
    <div class="metric-card"><span>${counts.policies ?? 0}</span><label>规则政策</label></div>
    <div class="metric-card"><span>${counts.product_master ?? counts.products ?? 0}</span><label>商品主数据</label></div>
    <div class="metric-card"><span>${counts.product_pending_review ?? 0}</span><label>商品待确认</label></div>
    <div class="metric-card"><span>${counts.pending_candidates ?? 0}</span><label>待审核候选</label></div>
    <div class="metric-card"><span>${counts.new_knowledge ?? 0}</span><label>新加入知识</label></div>
    <div class="metric-card"><span>${system.ok ? "正常" : "异常"}</span><label>系统状态</label></div>
  `;
}

async function loadKnowledge() {
  state.categoryItemsLoading = true;
  state.categoryItemsError = "";
  renderKnowledgeList();
  try {
    const payload = await apiGet("/api/knowledge/categories");
    state.categories = payload.items || [];
    const selectable = knowledgeCategoryOptions();
    if ((!state.activeCategoryId || !selectable.some((item) => item.id === state.activeCategoryId)) && selectable.length) {
      state.activeCategoryId = selectable[0].id;
    }
    renderCategorySelect();
    renderGeneratorCategorySelect();
    await loadCategoryItems();
  } catch (error) {
    state.categoryItemsLoading = false;
    state.categoryItemsLoadingMore = false;
    state.categoryItemsError = error?.message || String(error || "正式知识加载失败");
    console.warn("knowledge categories load failed", error);
    renderKnowledgeList();
  }
}

function renderCategorySelect() {
  const select = document.getElementById("category-select");
  select.innerHTML = knowledgeCategoryOptions()
    .map((category) => {
      const suffix = category.scope === "tenant_product" ? "（从商品详情进入）" : "";
      return `<option value="${escapeHtml(category.id)}">${escapeHtml(category.name || category.id)}${suffix} (${category.item_count || 0})</option>`;
    })
    .join("");
  select.value = state.activeCategoryId;
}

function visibleKnowledgeCategories() {
  return state.categories.filter((category) => category.scope !== "tenant_product" && category.scope !== "product_master");
}

function knowledgeCategoryOptions() {
  const visible = visibleKnowledgeCategories();
  const active = categoryById(state.activeCategoryId);
  const scopedContextOpen = state.productScopedEditContext?.categoryId === active?.id
    || (state.diagnosticHighlight?.targets || []).some((target) => String(target).startsWith(`${active?.id}/`));
  if (active?.scope === "tenant_product" && scopedContextOpen && !visible.some((item) => item.id === active.id)) {
    return [active, ...visible];
  }
  return visible;
}

function renderGeneratorCategorySelect() {
  const select = document.getElementById("generator-category");
  if (!select) return;
  select.innerHTML = `<option value="">自动判断门类</option>` + visibleKnowledgeCategories()
    .map((category) => `<option value="${escapeHtml(category.id)}">${escapeHtml(category.name || category.id)}</option>`)
    .join("");
}

async function loadCategoryItems(options = {}) {
  if (!state.activeCategoryId) return;
  const append = Boolean(options.append);
  const query = (document.getElementById("knowledge-search")?.value || "").trim();
  const offset = append ? state.categoryItems.length : 0;
  if (append && state.categoryItemsLoadingMore) return;
  state.categoryItemsLoadingOffset = offset;
  state.categoryItemsLoadingLimit = KNOWLEDGE_LIST_PAGE_SIZE;
  state.categoryItemsLoadingQuery = query;
  state.categoryItemsError = "";
  if (!append) {
    state.categoryItemsTotal = query ? 0 : Math.max(0, Number(activeCategory()?.item_count || 0) || 0);
    state.categoryItemsHasMore = false;
  }
  state.categoryItemsLoading = !append;
  state.categoryItemsLoadingMore = append;
  renderKnowledgeList();
  const params = new URLSearchParams({
    limit: String(KNOWLEDGE_LIST_PAGE_SIZE),
    offset: String(offset),
  });
  if (query) params.set("query", query);
  let loaded = false;
  try {
    const payload = await apiGet(`/api/knowledge/categories/${encodeURIComponent(state.activeCategoryId)}/items?${params.toString()}`);
    const nextItems = sortKnowledgeItemsForReview(payload.items || []);
    state.categoryItems = append ? sortKnowledgeItemsForReview([...(state.categoryItems || []), ...nextItems]) : nextItems;
    state.categoryItemsTotal = Math.max(0, Number(payload.total ?? state.categoryItems.length) || 0);
    state.categoryItemsHasMore = Boolean(payload.has_more ?? (state.categoryItems.length < state.categoryItemsTotal));
    loaded = true;
  } catch (error) {
    state.categoryItemsError = knowledgeLoadErrorMessage(error, offset);
    if (!append) {
      state.categoryItems = [];
      state.selectedKnowledge = null;
    }
    console.warn("knowledge category items load failed", error);
  } finally {
    state.categoryItemsLoading = false;
    state.categoryItemsLoadingMore = false;
  }
  if (!loaded) {
    renderKnowledgeList();
    renderKnowledgeDetail();
    return;
  }
  state.knowledgeListVisibleCount = KNOWLEDGE_LIST_PAGE_SIZE;
  if (!append) {
    state.selectedKnowledge = state.categoryItems[0] || null;
    state.knowledgeMode = "view";
  }
  renderKnowledgeList();
  renderKnowledgeDetail();
}

function sortKnowledgeItemsForReview(items = []) {
  return [...items].sort((left, right) => {
    const unreadDiff = (knowledgeItemIsUnread(left) ? 0 : 1) - (knowledgeItemIsUnread(right) ? 0 : 1);
    if (unreadDiff) return unreadDiff;
    return knowledgeReviewTimestamp(right) - knowledgeReviewTimestamp(left);
  });
}

function knowledgeItemIsUnread(item) {
  return Boolean(item?.review_state?.is_new);
}

function knowledgeReviewTimestamp(item) {
  const reviewState = item?.review_state || {};
  const value = knowledgeItemIsUnread(item)
    ? reviewState.marked_at || reviewState.updated_at || item?.updated_at || item?.created_at || ""
    : reviewState.read_at || reviewState.updated_at || reviewState.marked_at || item?.updated_at || item?.created_at || "";
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function activeCategory() {
  return state.categories.find((item) => item.id === state.activeCategoryId) || null;
}

function knownCategoryItemTotal() {
  const query = String(state.categoryItemsLoadingQuery || document.getElementById("knowledge-search")?.value || "").trim();
  if (query) return 0;
  const total = Number(state.categoryItemsTotal || activeCategory()?.item_count || 0);
  return Number.isFinite(total) && total > 0 ? total : 0;
}

function knowledgeLoadingProgressText() {
  const offset = Math.max(0, Number(state.categoryItemsLoadingOffset) || 0);
  const limit = Math.max(1, Number(state.categoryItemsLoadingLimit) || KNOWLEDGE_LIST_PAGE_SIZE);
  const total = knownCategoryItemTotal();
  const start = total ? Math.min(offset + 1, total) : offset + 1;
  const end = total ? Math.min(offset + limit, total) : offset + limit;
  const query = String(state.categoryItemsLoadingQuery || "").trim();
  const prefix = query ? "正在搜索并加载" : "正在加载";
  return `${prefix}第 ${start}-${end} 条${total ? ` / 共 ${total} 条` : ""}${query ? `（关键词：${query}）` : ""}`;
}

function knowledgeNextPageText() {
  const total = Math.max(0, Number(state.categoryItemsTotal || activeCategory()?.item_count || 0) || 0);
  const offset = Math.max(0, state.categoryItems.length);
  const remaining = Math.max(0, total - offset);
  const start = total ? Math.min(offset + 1, total) : offset + 1;
  const end = total ? Math.min(offset + KNOWLEDGE_LIST_PAGE_SIZE, total) : offset + KNOWLEDGE_LIST_PAGE_SIZE;
  return {
    remaining,
    progress: `第 ${start}-${end} 条${total ? ` / 共 ${total} 条` : ""}`,
    button: `再加载 ${Math.min(KNOWLEDGE_LIST_PAGE_SIZE, remaining || KNOWLEDGE_LIST_PAGE_SIZE)} 条`,
  };
}

function knowledgeLoadErrorMessage(error, offset = 0) {
  const rawMessage = error?.message || String(error || "未知错误");
  const progressText = knowledgeLoadingProgressText();
  const retryHint = offset > 0 ? "当前页加载失败，已保留前面已加载的内容。" : "第一页加载失败，暂时没有可展示内容。";
  return `${progressText}失败：${rawMessage}。${retryHint}`;
}

function categoryById(categoryId) {
  return state.categories.find((item) => item.id === categoryId) || null;
}

function isProductScopedCategory(category) {
  const categoryId = typeof category === "string" ? category : category?.id;
  const record = typeof category === "string" ? categoryById(category) : category;
  return record?.scope === "tenant_product" || ["product_faq", "product_rules", "product_explanations"].includes(categoryId);
}

function productDisplayName(productId) {
  const id = String(productId || "");
  if (!id) return "";
  const selected = state.selectedProduct;
  if (selected?.id === id) return selected.display?.name || selected.data?.name || id;
  const product = (state.productCatalog?.items || []).find((item) => item.id === id);
  return product?.display?.name || product?.data?.name || id;
}

function productCatalogItems() {
  return state.productCatalog?.items || [];
}

function productCategoryChoices(currentValue = "") {
  const values = new Set();
  for (const item of productCatalogItems()) {
    const category = item.display?.category || item.data?.category || "";
    if (category && category !== "未分类") values.add(String(category));
  }
  if (currentValue) values.add(String(currentValue));
  return Array.from(values).sort((a, b) => a.localeCompare(b, "zh-CN"));
}

function productIdSelectHtml(field, value, renderOptions = {}) {
  const id = `data-${field.id}`;
  const selectedValue = String(value || "");
  const readonly = renderOptions.readonlyFields?.has?.(field.id);
  const products = productCatalogItems();
  const hasSelected = products.some((item) => String(item.id) === selectedValue);
  const options = [
    `<option value="">自动匹配或不指定商品</option>`,
    ...products.map((item) => {
      const label = `${item.display?.name || item.data?.name || item.id} · ${item.display?.sku || item.id}`;
      return `<option value="${escapeHtml(item.id)}" ${String(item.id) === selectedValue ? "selected" : ""}>${escapeHtml(label)}</option>`;
    }),
    selectedValue && !hasSelected ? `<option value="${escapeHtml(selectedValue)}" selected>未在商品库找到：${escapeHtml(selectedValue)}</option>` : "",
  ].join("");
  return `
    <label class="form-field" data-field="${escapeHtml(field.id)}" data-kind="product_select">
      <span>${escapeHtml(fieldLabel(field))}${field.required ? " *" : ""}</span>
      <select id="${escapeHtml(id)}" ${readonly ? "disabled" : ""}>${options}</select>
      <small>从当前商品库选择，系统保存商品 ID；不用手动输入编号。</small>
    </label>
  `;
}

function productCategorySelectHtml(field, value, renderOptions = {}) {
  const id = `data-${field.id}`;
  const selectedValue = String(value || "");
  const readonly = renderOptions.readonlyFields?.has?.(field.id);
  const options = [
    `<option value="">全部类目或自动匹配</option>`,
    ...productCategoryChoices(selectedValue).map((category) => `<option value="${escapeHtml(category)}" ${category === selectedValue ? "selected" : ""}>${escapeHtml(category)}</option>`),
  ].join("");
  return `
    <label class="form-field" data-field="${escapeHtml(field.id)}" data-kind="product_category_select">
      <span>${escapeHtml(fieldLabel(field))}${field.required ? " *" : ""}</span>
      <select id="${escapeHtml(id)}" ${readonly ? "disabled" : ""}>${options}</select>
      <small>从商品库已有类目里选择；没有类目时先到商品库维护。</small>
    </label>
  `;
}

function knowledgeProductName(item) {
  const productId = item?.data?.product_id || state.productScopedEditContext?.productId || "";
  return productDisplayName(productId) || productId || "未指定商品";
}

function knowledgeScopeBadges(category, item) {
  if (!category || !item) return [];
  const data = item.data || {};
  if (isProductScopedCategory(category)) {
    return [{label: `只用于：${knowledgeProductName(item)}`, tone: "info"}];
  }
  if (category.id === "products") return [{label: "商品主数据", tone: "info"}];
  if (!["chats", "policies"].includes(category.id)) return [];
  const scope = data.applicability_scope || "global";
  if (scope === "specific_product") return [{label: `指定商品：${productDisplayName(data.product_id) || data.product_id || "未填写"}`, tone: "info"}];
  if (scope === "product_category") return [{label: `商品类目：${data.product_category || "未填写"}`, tone: "info"}];
  return [{label: "本账号通用", tone: "ok"}];
}

function knowledgeContextNoticeHtml(category, item, options = {}) {
  if (!category || !item) return "";
  const data = item.data || {};
  if (isProductScopedCategory(category)) {
    return `
      <div class="helper-card context-card">
        <strong>这条内容只属于「${escapeHtml(knowledgeProductName(item))}」。</strong>
        <span>${options.editing ? "商品 ID 已锁定，避免把专属问答误改成别的商品；正文和触发词可以直接修改。" : "客户问到这个商品时，它才会参与客服回复。点击编辑后只需要改标题、触发词和回复内容。"}</span>
      </div>
    `;
  }
  if (category.id === "products") {
    return `
      <div class="helper-card context-card">
        <strong>这是商品主数据，不属于普通正式知识。</strong>
        <span>库存、价格、规格和在售状态请优先在商品库维护；AI经验池和候选知识不能反向写入这里。</span>
      </div>
    `;
  }
  if (["chats", "policies"].includes(category.id)) {
    const badge = knowledgeScopeBadges(category, item)[0]?.label || "本账号通用";
    const tip = data.applicability_scope === "specific_product"
      ? "这条知识只在关联商品被识别出来时参与回复。"
      : data.applicability_scope === "product_category"
        ? "这条知识只在对应商品类目被识别出来时参与回复。"
        : "这条知识只在当前账号内作为通用话术或规则参与回复，不代表VPS公共共享知识。";
    return `
      <div class="helper-card context-card">
        <strong>${escapeHtml(badge)}</strong>
        <span>${escapeHtml(tip)}${options.editing ? " 如果只适用于某个商品，请在下方设置适用范围和关联商品。" : ""}</span>
      </div>
    `;
  }
  return "";
}

function renderKnowledgeList() {
  const query = (document.getElementById("knowledge-search").value || "").trim().toLowerCase();
  const category = activeCategory();
  const titleField = category?.schema?.item_title_field || "title";
  const subtitleField = category?.schema?.item_subtitle_field || "";
  const list = document.getElementById("knowledge-list");
  if (state.categoryItemsLoading && !state.categoryItemsLoadingMore) {
    const progressText = knowledgeLoadingProgressText();
    list.innerHTML = `
      <div class="status-card loading">
        <strong><span class="loading-spinner" aria-hidden="true"></span>正在加载正式知识</strong>
        <span>${escapeHtml(progressText)}。系统每页只拉取 ${KNOWLEDGE_LIST_PAGE_SIZE} 条，数量多时不会一次性加载全部话术。</span>
      </div>
    `;
    return;
  }
  const filtered = sortKnowledgeItemsForReview(state.categoryItems.filter((item) => {
    const text = `${item.id} ${businessSearchText(item.data || {})}`.toLowerCase();
    return !query || text.includes(query);
  }));
  const visibleItems = filtered;
  const errorHtml = state.categoryItemsError ? `
    <div class="status-card error">
      <strong>正式知识加载失败</strong>
      <span>${escapeHtml(state.categoryItemsError)}</span>
      <div class="button-row">
        <button class="secondary-button knowledge-retry-load" type="button">重试加载</button>
      </div>
    </div>
  ` : "";
  const rowsHtml = visibleItems
    .map((item, index) => {
      const title = item.data?.[titleField] || item.id;
      const subtitle = subtitleField ? item.data?.[subtitleField] : item.status;
      const active = state.selectedKnowledge?.id === item.id ? " is-selected" : "";
      const highlighted = diagnosticTargetMatches(category?.id, item.id) ? " diagnostic-highlight" : "";
      const badges = [...(item.display_badges || []), ...knowledgeScopeBadges(category, item)];
      const runtime = item.runtime || {};
      return `
        <button class="product-card knowledge-card knowledge-row${active}${highlighted}" data-index="${index}" aria-label="查看 ${escapeHtml(title)} 的完整知识">
          <div class="product-card-head">
            <div>
              <strong>${escapeHtml(title)}</strong>
              <span>${escapeHtml(category?.name || category?.id || "正式知识")}</span>
            </div>
            <em class="${item.status === "archived" ? "is-muted" : ""}">${item.status === "archived" ? "已归档" : "启用中"}</em>
          </div>
          <div class="product-card-facts">
            <span><b>知识 ID</b>${escapeHtml(item.id)}</span>
            <span><b>回复方式</b>${runtime.requires_handoff ? "需请示" : runtime.allow_auto_reply === false ? "不自动回" : "可自动回"}</span>
          </div>
          <p>${escapeHtml(knowledgeCardSummary(category, item, subtitle))}</p>
          ${badgeListHtml(badges)}
          <small>点击查看完整内容</small>
        </button>
      `;
    })
    .join("");
  list.innerHTML = errorHtml + rowsHtml;
  if (state.categoryItemsHasMore) {
    const nextPage = knowledgeNextPageText();
    list.innerHTML += `
      <div class="helper-card">
        <strong>已显示 ${state.categoryItems.length}/${state.categoryItemsTotal || state.categoryItems.length} 条</strong>
        <span>${state.categoryItemsLoadingMore ? `${escapeHtml(knowledgeLoadingProgressText())}。` : `下一页将加载 ${escapeHtml(nextPage.progress)}。`}数据较多时按页从后台加载，避免一次性拉取 900+ 条导致页面卡顿。</span>
        <div class="button-row">
          <button class="secondary-button knowledge-load-more" ${state.categoryItemsLoadingMore ? "disabled" : ""}>${state.categoryItemsLoadingMore ? `加载中：${escapeHtml(knowledgeLoadingProgressText())}` : nextPage.button}</button>
        </div>
      </div>
    `;
  }
  list.querySelectorAll(".knowledge-row").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedKnowledge = visibleItems[Number(button.dataset.index)];
      state.knowledgeMode = "view";
      renderKnowledgeList();
      renderKnowledgeDetail();
      openKnowledgeDetailModal();
    });
  });
  list.querySelector(".knowledge-retry-load")?.addEventListener("click", () => {
    loadCategoryItems({append: state.categoryItems.length > 0}).catch((error) => alert(error.message));
  });
  list.querySelector(".knowledge-load-more")?.addEventListener("click", () => {
    loadCategoryItems({append: true}).catch((error) => alert(error.message));
  });
  if (!filtered.length && !state.categoryItemsError) {
    list.innerHTML = `<div class="empty-state">没有匹配结果</div>`;
  }
}

function knowledgeCardSummary(category, item, fallback = "") {
  const fields = category?.schema?.fields || [];
  const data = item?.data || {};
  const preferredIds = [
    category?.schema?.item_subtitle_field,
    "content",
    "answer",
    "service_reply",
    "reply",
    "description",
    "summary",
    "question",
    "customer_message",
  ].filter(Boolean);
  for (const fieldId of preferredIds) {
    const value = displayBusinessValue(data[fieldId]);
    if (value) return shortBusinessText(value, 118);
  }
  for (const field of fields) {
    const value = displayBusinessValue(data[field.id]);
    if (value && value !== fallback) return shortBusinessText(value, 118);
  }
  return shortBusinessText(fallback || "这条知识暂无摘要，点开后可查看完整字段。", 118);
}

function renderKnowledgeDetail() {
  const detail = document.getElementById("knowledge-detail");
  const category = activeCategory();
  const item = state.selectedKnowledge;
  updateKnowledgeButtons();
  if (!category) {
    detail.innerHTML = `<div class="empty-state">暂无知识门类</div>`;
    return;
  }
  if (!item) {
    detail.innerHTML = `<div class="empty-state">当前门类暂无条目，点击“新增知识”开始添加。</div>`;
    return;
  }
  detail.innerHTML = state.knowledgeMode === "view" ? knowledgeReadonlyHtml(category, item) : knowledgeFormHtml(category, item);
  bindDynamicEditors(detail);
  detail.querySelector(".knowledge-acknowledge")?.addEventListener("click", () => acknowledgeKnowledgeItem().catch((error) => alert(error.message)));
  detail.querySelectorAll(".knowledge-modal-close").forEach((button) => {
    button.addEventListener("click", closeKnowledgeDetailModal);
  });
  detail.querySelector(".knowledge-edit-inline")?.addEventListener("click", editKnowledgeItem);
  detail.querySelector(".knowledge-archive-inline")?.addEventListener("click", () => archiveKnowledgeItem().catch((error) => alert(error.message)));
  detail.querySelector(".knowledge-save-inline")?.addEventListener("click", () => saveKnowledgeItem().catch((error) => alert(error.message)));
  detail.querySelector(".knowledge-cancel-inline")?.addEventListener("click", cancelKnowledgeEdit);
}

async function loadProductCatalog(options = {}) {
  const payload = await apiGet("/api/product-console/catalog?include_archived=true");
  state.productCatalog = payload;
  const items = payload.items || [];
  if (state.selectedProduct?.id && !items.some((item) => item.id === state.selectedProduct.id)) {
    state.selectedProduct = null;
    state.productDetailMode = "view";
    state.productScopedEditor = null;
  }
  renderProductCatalog();
  if (options.loadDetail === true && state.selectedProduct?.id) {
    await loadProductDetail(state.selectedProduct.id);
  }
}

function renderProductCatalog() {
  const payload = state.productCatalog || {};
  const counts = payload.counts || {};
  document.getElementById("product-catalog-cards").innerHTML = `
    <div class="metric-card"><span>${counts.active ?? 0}</span><label>在售商品</label></div>
    <div class="metric-card"><span>${counts.in_stock ?? 0}</span><label>有库存</label></div>
    <div class="metric-card"><span>${counts.sold_out ?? 0}</span><label>无库存</label></div>
    <div class="metric-card"><span>${counts.unread ?? 0}</span><label>NEW待确认</label></div>
    <div class="metric-card"><span>${counts.archived ?? 0}</span><label>已归档</label></div>
  `;
  renderProductCatalogList();
  renderProductCatalogDetail();
}

function renderProductCatalogList() {
  const list = document.getElementById("product-catalog-list");
  const items = sortProductCatalogItemsForReview(state.productCatalog?.items || []);
  list.innerHTML = items.map((item, index) => {
    const display = item.display || {};
    const data = item.data || {};
    const isNew = productItemIsNew(item);
    const badges = [
      ...(isNew ? [{label: "新商品待确认", tone: "warning"}] : []),
      {label: item.status === "archived" ? "已归档" : "在售", tone: item.status === "archived" ? "muted" : "ok"},
      {label: display.stock_label || "库存未填写", tone: productStockTone(item)},
      {label: `${productScopedTotal(item)} 条专属话术`, tone: "info"},
    ];
    return `
      <button class="product-card product-row" data-index="${index}" aria-label="查看 ${escapeHtml(display.name || data.name || item.id)} 的完整资料">
        ${isNew ? `
          <span class="product-new-float" aria-label="新商品">
            <span class="experience-new-badge">NEW</span>
          </span>
        ` : ""}
        <div class="product-card-head">
          <div>
            <strong>${escapeHtml(display.name || data.name || item.id)}</strong>
            <span>${escapeHtml(display.category || data.category || "未分类")}</span>
          </div>
          <em>${escapeHtml(formatProductPrice(display))}</em>
        </div>
        <div class="product-card-facts">
          <span><b>库存</b>${escapeHtml(display.stock_label || "未填写")}</span>
          <span><b>编号</b>${escapeHtml(display.sku || item.id)}</span>
        </div>
        <p>${escapeHtml(productCardSummary(item))}</p>
        ${badgeListHtml(badges)}
        <small>点击查看完整信息</small>
      </button>
    `;
  }).join("") || `<div class="empty-state">暂无商品。可以点击“新增商品”，或在上方用一句话添加。</div>`;
  list.querySelectorAll(".product-row").forEach((button) => {
    button.addEventListener("click", async () => {
      const item = items[Number(button.dataset.index)];
      state.selectedProduct = item || null;
      state.productDetailMode = "view";
      state.productScopedEditor = null;
      await loadProductDetail(item?.id);
    });
  });
}

async function loadProductDetail(productId, options = {}) {
  if (!productId) {
    renderProductCatalogDetail();
    return;
  }
  const payload = await apiGet(`/api/product-console/products/${encodeURIComponent(productId)}`);
  state.selectedProduct = payload.item || state.selectedProduct;
  state.productDetailScopedKnowledge = payload.scoped_knowledge || {};
  renderProductCatalogList();
  renderProductCatalogDetail();
  if (options.open !== false) openProductDetailModal();
}

function renderProductCatalogDetail(scopedKnowledge = null) {
  const detail = document.getElementById("product-catalog-detail");
  const item = state.selectedProduct;
  if (!item) {
    detail.innerHTML = `<div class="empty-state">请选择一个商品。</div>`;
    return;
  }
  const data = item.data || {};
  const display = item.display || {};
  const isNew = productItemIsNew(item);
  const acknowledgeLoading = state.productAcknowledgeLoadingIds.has(item.id);
  if (scopedKnowledge) state.productDetailScopedKnowledge = scopedKnowledge;
  const scoped = scopedKnowledge || state.productDetailScopedKnowledge || {};
  if (state.productDetailMode === "edit") {
    detail.innerHTML = productEditFormHtml(item, scoped);
    bindProductDetailEditors(detail);
    return;
  }
  detail.innerHTML = `
    <div class="read-head">
      <div>
        <p class="eyebrow">商品详情</p>
        <h2 id="product-detail-title">${escapeHtml(display.name || data.name || item.id)}</h2>
        ${badgeListHtml([
          ...(isNew ? [{label: "NEW 新商品待确认", tone: "warning"}] : []),
          {label: display.stock_label || "库存未填写", tone: item.stock_state === "in_stock" ? "ok" : item.stock_state === "archived" ? "muted" : "warning"},
          {label: item.status === "archived" ? "已归档" : "在售", tone: item.status === "archived" ? "muted" : "ok"},
        ])}
      </div>
      <div class="read-actions">
        <button class="secondary-button product-modal-close" type="button">关闭</button>
        ${isNew ? `<button class="secondary-button product-acknowledge ${acknowledgeLoading ? "is-loading" : ""}" type="button" ${acknowledgeLoading ? "disabled" : ""}>${acknowledgeLoading ? `<span class="loading-spinner button-spinner" aria-hidden="true"></span><span>确认中</span>` : "已阅"}</button>` : ""}
        <button class="secondary-button product-edit-form" type="button">编辑详情</button>
        <button class="secondary-button danger-button product-archive" type="button">${item.status === "archived" ? "重新上架" : "归档"}</button>
      </div>
    </div>
    <div class="summary-table product-summary-table">
      <div><span>价格</span><strong>${escapeHtml(formatProductPrice(display))}</strong></div>
      <div><span>库存/状态</span><strong>${escapeHtml(display.stock_label || "未填写")}</strong></div>
      <div><span>内部编号</span><strong>${escapeHtml(display.sku || item.id)}</strong></div>
      <div><span>类型</span><strong>${escapeHtml(display.category || "未分类")}</strong></div>
    </div>
    <div class="inventory-tools">
      <button class="secondary-button product-stock-decrease" type="button">卖出 1 件</button>
      <button class="secondary-button product-stock-increase" type="button">补货 1 件</button>
      <label class="form-field inline-field"><span>库存改为</span><input id="product-stock-set-value" type="number" min="0" placeholder="数量" /></label>
      <button class="primary-button product-stock-set" type="button">保存库存</button>
    </div>
    <div class="read-grid">
      ${productInfoField("客户常用叫法", userVisibleTags(data.aliases).join("、"))}
      ${productInfoField("核心参数/车况", data.specs)}
      ${productInfoField("阶梯价/优惠", productPriceTierText(data.price_tiers))}
      ${productInfoField("看车/交付说明", data.shipping_policy)}
      ${productInfoField("售后/合同口径", data.warranty_policy)}
      ${productInfoField("需要谨慎确认的点", (data.risk_rules || []).join("、"))}
    </div>
    ${productReplyTemplatesReadonlyHtml(data.reply_templates, display.name || data.name || item.id)}
    <div class="product-scoped-panel">
      <div class="section-heading">
        <div>
          <span>精准触发知识（强触发）</span>
          <strong>这些内容只会在客户问到这个商品、且命中关键词时参与回答；与上方基础话术互补，不互相覆盖。</strong>
        </div>
      </div>
      ${productScopedHtml("商品专属问答", "product_faq", scoped.product_faq || [], "answer", display.name || data.name || item.id)}
      ${productScopedHtml("商品专属规则", "product_rules", scoped.product_rules || [], "answer", display.name || data.name || item.id)}
      ${productScopedHtml("商品专属解释", "product_explanations", scoped.product_explanations || [], "content", display.name || data.name || item.id)}
      ${productScopedEditorHtml()}
    </div>
  `;
  detail.querySelector(".product-stock-decrease")?.addEventListener("click", () => adjustProductInventory("sell", 1));
  detail.querySelector(".product-stock-increase")?.addEventListener("click", () => adjustProductInventory("increase", 1));
  detail.querySelector(".product-stock-set")?.addEventListener("click", () => {
    const quantity = Number(document.getElementById("product-stock-set-value")?.value || 0);
    adjustProductInventory("set", quantity);
  });
  detail.querySelector(".product-modal-close")?.addEventListener("click", closeProductDetailModal);
  detail.querySelector(".product-acknowledge")?.addEventListener("click", () => acknowledgeProductItem().catch((error) => alert(error.message)));
  detail.querySelector(".product-archive")?.addEventListener("click", () => {
    const operation = item.status === "archived" ? "activate" : "archive";
    if (operation === "archive" && !confirm("确认把这个商品归档吗？归档后不会作为在售商品参与客服回答。")) return;
    adjustProductInventory(operation, 0);
  });
  detail.querySelector(".product-edit-form")?.addEventListener("click", () => {
    state.productDetailMode = "edit";
    state.productScopedEditor = null;
    renderProductCatalogDetail();
  });
  detail.querySelectorAll(".product-scoped-edit").forEach((button) => {
    button.addEventListener("click", () => openProductScopedInlineEditor(button.dataset.category, button.dataset.itemId));
  });
  detail.querySelectorAll(".product-scoped-new").forEach((button) => {
    button.addEventListener("click", () => openProductScopedInlineEditor(button.dataset.category, ""));
  });
  bindProductScopedEditor(detail);
}

function productItemIsNew(item) {
  return Boolean(item?.is_unread || item?.review_state?.is_new);
}

function sortProductCatalogItemsForReview(items = []) {
  return [...items].sort((left, right) => {
    const unreadDiff = (productItemIsNew(left) ? 0 : 1) - (productItemIsNew(right) ? 0 : 1);
    if (unreadDiff) return unreadDiff;
    const archivedDiff = (left?.status === "archived" ? 1 : 0) - (right?.status === "archived" ? 1 : 0);
    if (archivedDiff) return archivedDiff;
    const timeDiff = productReviewTimestamp(right) - productReviewTimestamp(left);
    if (timeDiff) return timeDiff;
    const leftName = left?.display?.name || left?.data?.name || left?.id || "";
    const rightName = right?.display?.name || right?.data?.name || right?.id || "";
    return String(leftName).localeCompare(String(rightName), "zh-CN");
  });
}

function productReviewTimestamp(item) {
  const reviewState = item?.review_state || {};
  const value = productItemIsNew(item)
    ? reviewState.marked_at || reviewState.updated_at || item?.updated_at || item?.created_at || ""
    : reviewState.read_at || reviewState.updated_at || reviewState.marked_at || item?.updated_at || item?.created_at || "";
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function productInfoField(label, value) {
  if (value === undefined || value === null || value === "" || (Array.isArray(value) && !value.length)) return "";
  return `<div class="read-field wide-field"><span>${escapeHtml(label)}</span><p>${escapeHtml(value)}</p></div>`;
}

function productStockTone(item) {
  if (item?.stock_state === "in_stock") return "ok";
  if (item?.stock_state === "archived") return "muted";
  return "warning";
}

function productScopedTotal(item) {
  const counts = item?.scoped_counts || {};
  return Number(counts.product_faq || 0) + Number(counts.product_rules || 0) + Number(counts.product_explanations || 0);
}

function productCardSummary(item) {
  const data = item?.data || {};
  const parts = [
    data.specs,
    data.shipping_policy,
    userVisibleTags(data.aliases).slice(0, 4).join("、"),
  ].map((value) => String(value || "").trim()).filter(Boolean);
  return shortBusinessText(parts.join(" · "), 118) || "暂无概括信息，点开后可以补充核心参数、看车/交付说明和售后口径。";
}

function userVisibleTags(value) {
  const tags = Array.isArray(value) ? value : splitTags(String(value || ""));
  return tags
    .map((item) => String(item || "").trim())
    .filter((item) => item && !/^(CHEJIN|LIVEFLOW|LLMSYN|DBG)_[A-Za-z0-9_:-]+$/i.test(item));
}

function productPriceTierText(tiers) {
  if (!Array.isArray(tiers) || !tiers.length) return "";
  return tiers
    .map((tier) => {
      const min = tier?.min_quantity ?? tier?.min ?? "";
      const price = tier?.unit_price ?? tier?.price ?? "";
      if (min === "" || price === "") return "";
      return `${min} 件/台起：${price}`;
    })
    .filter(Boolean)
    .join("；");
}

function openProductDetailModal() {
  const modal = document.getElementById("product-detail-modal");
  if (!modal) return;
  modal.classList.remove("is-hidden");
  modal.setAttribute("aria-hidden", "false");
}

function closeProductDetailModal() {
  const modal = document.getElementById("product-detail-modal");
  if (!modal) return;
  modal.classList.add("is-hidden");
  modal.setAttribute("aria-hidden", "true");
  state.productDetailMode = "view";
  state.productScopedEditor = null;
}

function openKnowledgeDetailModal() {
  const modal = document.getElementById("knowledge-detail-modal");
  if (!modal) return;
  modal.classList.remove("is-hidden");
  modal.setAttribute("aria-hidden", "false");
}

function closeKnowledgeDetailModal() {
  const modal = document.getElementById("knowledge-detail-modal");
  if (!modal) return;
  modal.classList.add("is-hidden");
  modal.setAttribute("aria-hidden", "true");
  if (state.knowledgeMode === "new" && !state.selectedKnowledge?.id) {
    state.selectedKnowledge = state.categoryItems[0] || null;
  }
  state.knowledgeMode = "view";
  renderKnowledgeList();
  renderKnowledgeDetail();
}

function productReplyTemplatesReadonlyHtml(value, productName) {
  const templates = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const entries = Object.entries(templates).filter(([, inner]) => !isEmpty(inner));
  return `
    <div class="read-field wide-field product-template-read">
      <span>基础话术（弱触发）</span>
      <p>这些默认回复绑定在「${escapeHtml(productName || "当前商品")}」上；客户问到这个商品时可作为兜底话术。需要“命中关键词才触发”的内容，请放到下方精准触发知识。</p>
      ${entries.length ? `
        <div class="variable-table">
          <div class="variable-table-head"><strong>场景</strong><strong>回复内容</strong></div>
          ${entries.map(([key, inner]) => `
            <div class="variable-table-row">
              <code>${escapeHtml(templateLabels[key] || key)}</code>
              <span>${escapeHtml(displayBusinessValue(inner))}</span>
            </div>
          `).join("")}
        </div>
      ` : `<div class="empty-state compact-empty">当前商品没有单独填写默认回复，将使用通用话术或 AI 经验。</div>`}
    </div>
  `;
}

function productEditFormHtml(item, scoped) {
  const data = item.data || {};
  const productName = data.name || item.display?.name || item.id;
  return `
    <div class="read-head">
      <div>
        <p class="eyebrow">编辑商品资料</p>
        <h2>${escapeHtml(productName || item.id)}</h2>
        ${badgeListHtml([{label: "在商品库内编辑", tone: "info"}, {label: item.status === "archived" ? "已归档" : "在售", tone: item.status === "archived" ? "muted" : "ok"}])}
      </div>
      <div class="read-actions">
        <button class="primary-button product-detail-save" type="button">保存商品资料</button>
        <button class="secondary-button product-detail-cancel" type="button">取消</button>
      </div>
    </div>
    <div class="helper-card context-card product-edit-help">
      <strong>这里只改当前商品。</strong>
      <span>商品名称、价格、库存、物流、售后和基础话术会同步影响商品库；下方精准触发知识仍然单独维护，但都归属同一件商品。</span>
    </div>
    <div class="form-grid product-detail-form">
      ${productTextInput("product-data-name", "商品名称 *", data.name || "")}
      ${productTextInput("product-data-sku", "内部编号/型号", data.sku || "")}
      ${productTextInput("product-data-category", "类型/类目", data.category || "")}
      ${productTextInput("product-data-unit", "单位", data.unit || "")}
      ${productTextInput("product-data-price", "基础价格", data.price ?? "", "number")}
      ${productTextInput("product-data-inventory", "库存", data.inventory ?? "", "number")}
      ${productTextarea("product-data-aliases", "客户常用叫法", displayTags(userVisibleTags(data.aliases)), "一行一个，或用逗号分隔")}
      ${productTextarea("product-data-specs", "核心参数/车况", data.specs || "")}
      ${productTextarea("product-data-shipping", "看车/交付说明", data.shipping_policy || "")}
      ${productTextarea("product-data-warranty", "售后/合同口径", data.warranty_policy || "")}
      ${productTextarea("product-data-risk", "需要谨慎确认的点", displayTags(data.risk_rules), "一行一个，或用逗号分隔")}
      ${productReplyTemplateEditorHtml(data.reply_templates, productName)}
    </div>
    <div class="product-scoped-panel">
      <div class="section-heading">
        <div>
          <span>精准触发知识（强触发）</span>
          <strong>下面三类是带触发词的专属知识；和“基础话术（弱触发）”同属当前商品，但不会互相覆盖。</strong>
        </div>
      </div>
      ${productScopedHtml("商品专属问答", "product_faq", (scoped || {}).product_faq || [], "answer", productName)}
      ${productScopedHtml("商品专属规则", "product_rules", (scoped || {}).product_rules || [], "answer", productName)}
      ${productScopedHtml("商品专属解释", "product_explanations", (scoped || {}).product_explanations || [], "content", productName)}
      ${productScopedEditorHtml()}
    </div>
  `;
}

function productTextInput(id, label, value, type = "text") {
  return `
    <label class="form-field product-short-field">
      <span>${escapeHtml(label)}</span>
      <input id="${escapeHtml(id)}" type="${escapeHtml(type)}" value="${escapeHtml(value ?? "")}" />
    </label>
  `;
}

function productTextarea(id, label, value, placeholder = "") {
  return `
    <label class="form-field wide-field">
      <span>${escapeHtml(label)}</span>
      <textarea id="${escapeHtml(id)}" placeholder="${escapeHtml(placeholder)}">${escapeHtml(value || "")}</textarea>
    </label>
  `;
}

function productReplyTemplateEditorHtml(value, productName) {
  const templates = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const keys = Array.from(new Set([...Object.keys(templateLabels), ...Object.keys(templates)]));
  return `
    <div class="form-field wide-field reply-template-editor product-reply-template-editor">
      <span>基础话术（弱触发）</span>
      <div class="object-guide">这些是「${escapeHtml(productName || "当前商品")}」的默认商品话术；它们和下方精准触发知识都绑定当前商品。需要“客户问到某个关键词才用”的内容，请放到商品专属问答/规则/解释。</div>
      ${keys.map((key) => `
        <label class="nested-field">
          <span>${escapeHtml(templateLabels[key] || key)}</span>
          <textarea data-product-template-key="${escapeHtml(key)}">${escapeHtml(templates[key] || "")}</textarea>
        </label>
      `).join("")}
    </div>
  `;
}

function bindProductDetailEditors(root) {
  root.querySelector(".product-detail-save")?.addEventListener("click", () => saveProductDetailForm().catch((error) => alert(error.message)));
  root.querySelector(".product-detail-cancel")?.addEventListener("click", () => {
    state.productDetailMode = "view";
    state.productScopedEditor = null;
    renderProductCatalogDetail();
  });
  root.querySelectorAll(".product-scoped-edit").forEach((button) => {
    button.addEventListener("click", () => openProductScopedInlineEditor(button.dataset.category, button.dataset.itemId));
  });
  root.querySelectorAll(".product-scoped-new").forEach((button) => {
    button.addEventListener("click", () => openProductScopedInlineEditor(button.dataset.category, ""));
  });
  bindProductScopedEditor(root);
}

async function saveProductDetailForm() {
  const original = state.selectedProduct;
  if (!original?.id) return;
  const data = {
    ...(original.data || {}),
    name: document.getElementById("product-data-name")?.value.trim() || "",
    sku: document.getElementById("product-data-sku")?.value.trim() || "",
    category: document.getElementById("product-data-category")?.value.trim() || "",
    unit: document.getElementById("product-data-unit")?.value.trim() || "",
    price: numberOrNull(document.getElementById("product-data-price")?.value),
    inventory: numberOrNull(document.getElementById("product-data-inventory")?.value),
    aliases: splitTags(document.getElementById("product-data-aliases")?.value || ""),
    specs: document.getElementById("product-data-specs")?.value.trim() || "",
    shipping_policy: document.getElementById("product-data-shipping")?.value.trim() || "",
    warranty_policy: document.getElementById("product-data-warranty")?.value.trim() || "",
    risk_rules: splitTags(document.getElementById("product-data-risk")?.value || ""),
    reply_templates: collectProductReplyTemplates(),
  };
  if (!data.name) throw new Error("商品名称不能为空。");
  const item = {
    ...original,
    category_id: "products",
    id: original.id,
    status: original.status || "active",
    data,
    runtime: original.runtime || {allow_auto_reply: true, requires_handoff: false, risk_level: "normal"},
  };
  await apiJson(`/api/knowledge/categories/products/items/${encodeURIComponent(original.id)}`, {
    method: "PUT",
    body: JSON.stringify(item),
  });
  state.productDetailMode = "view";
  state.productScopedEditor = null;
  await Promise.all([loadProductCatalog({loadDetail: false}), loadOverview().catch(() => {})]);
  await loadProductDetail(original.id);
}

function collectProductReplyTemplates() {
  const item = {};
  document.querySelectorAll("[data-product-template-key]").forEach((input) => {
    const value = input.value.trim();
    if (value) item[input.dataset.productTemplateKey] = value;
  });
  return item;
}

function productScopedHtml(title, categoryId, items, bodyField, productName) {
  return `
    <section class="product-scoped-section">
      <div class="product-scoped-section-head">
        <h3>${escapeHtml(title)}</h3>
        <button class="secondary-button product-scoped-new" type="button" data-category="${escapeHtml(categoryId)}">新增</button>
      </div>
      ${items.length ? items.map((item) => {
        const data = item.data || {};
        const productId = data.product_id || state.selectedProduct?.id || "";
        return `
          <div class="compact-row product-scoped-row">
            <div>
              <strong>${escapeHtml(data.title || item.id)}</strong>
              <span>归属商品：${escapeHtml(productName || productId || "未指定")} · ${escapeHtml((data.keywords || []).join("、") || "未设置触发词")}</span>
              <p>${escapeHtml(data[bodyField] || data.answer || data.content || "")}</p>
            </div>
            <button class="secondary-button product-scoped-edit" type="button" data-category="${escapeHtml(categoryId)}" data-item-id="${escapeHtml(item.id)}" data-product-id="${escapeHtml(productId)}">编辑</button>
          </div>
        `;
      }).join("") : `<div class="empty-state">暂无${escapeHtml(title)}。</div>`}
    </section>
  `;
}

function openProductScopedInlineEditor(categoryId, itemId) {
  const items = (state.productDetailScopedKnowledge || {})[categoryId] || [];
  const item = itemId ? items.find((entry) => entry.id === itemId) : null;
  state.productScopedEditor = {
    categoryId,
    itemId: item?.id || "",
    item: item || null,
  };
  renderProductCatalogDetail();
}

function productScopedEditorHtml() {
  const editor = state.productScopedEditor;
  const product = state.selectedProduct;
  if (!editor || !product?.id) return "";
  const categoryId = editor.categoryId;
  const item = editor.item || {};
  const data = item.data || {};
  const productName = product.display?.name || product.data?.name || product.id;
  const bodyField = productScopedBodyField(categoryId);
  const bodyLabel = categoryId === "product_explanations" ? "说明内容 *" : "标准回复 *";
  return `
    <div class="product-scoped-editor">
      <div class="section-heading">
        <div>
          <span>${escapeHtml(productScopedCategoryTitle(categoryId))}</span>
          <strong>${editor.itemId ? "编辑" : "新增"}「${escapeHtml(productName)}」的专属内容</strong>
        </div>
      </div>
      <div class="helper-card context-card">
        <strong>这条内容只会绑定当前商品。</strong>
        <span>商品 ID 已固定为 ${escapeHtml(product.id)}；保存后仍显示在商品详情里，客户问到当前商品并命中触发词时才会参与回复。</span>
      </div>
      <div class="form-grid product-scoped-form">
        <label class="form-field">
          <span>标题 *</span>
          <input id="product-scoped-title" value="${escapeHtml(data.title || "")}" />
        </label>
        <label class="form-field">
          <span>归属商品</span>
          <input id="product-scoped-product-id" value="${escapeHtml(product.id)}" readonly />
        </label>
        ${productTextarea("product-scoped-keywords", "触发关键词", displayTags(data.keywords), "一行一个，或用逗号分隔")}
        ${categoryId === "product_faq" ? productTextarea("product-scoped-question", "客户问题", data.question || "") : ""}
        ${productTextarea("product-scoped-body", bodyLabel, data[bodyField] || data.answer || data.content || "")}
        ${categoryId === "product_rules" ? `
          <label class="checkbox-line"><input id="product-scoped-auto" type="checkbox" ${data.allow_auto_reply !== false ? "checked" : ""} /> 允许自动回复</label>
          <label class="checkbox-line"><input id="product-scoped-handoff" type="checkbox" ${data.requires_handoff ? "checked" : ""} /> 必须转人工</label>
          <label class="form-field">
            <span>转人工原因</span>
            <input id="product-scoped-handoff-reason" value="${escapeHtml(data.handoff_reason || "")}" />
          </label>
        ` : ""}
      </div>
      <div class="button-row product-scoped-editor-actions">
        <button class="primary-button product-scoped-save" type="button">保存专属知识</button>
        <button class="secondary-button product-scoped-cancel" type="button">取消</button>
        ${editor.itemId ? `<button class="secondary-button danger-button product-scoped-delete" type="button">归档删除</button>` : ""}
      </div>
    </div>
  `;
}

function bindProductScopedEditor(root) {
  root.querySelector(".product-scoped-save")?.addEventListener("click", () => saveProductScopedInlineEditor().catch((error) => alert(error.message)));
  root.querySelector(".product-scoped-cancel")?.addEventListener("click", () => {
    state.productScopedEditor = null;
    renderProductCatalogDetail();
  });
  root.querySelector(".product-scoped-delete")?.addEventListener("click", () => deleteProductScopedInlineItem().catch((error) => alert(error.message)));
}

async function saveProductScopedInlineEditor() {
  const editor = state.productScopedEditor;
  const product = state.selectedProduct;
  if (!editor?.categoryId || !product?.id) return;
  const categoryId = editor.categoryId;
  const bodyField = productScopedBodyField(categoryId);
  const title = document.getElementById("product-scoped-title")?.value.trim() || "";
  const body = document.getElementById("product-scoped-body")?.value.trim() || "";
  if (!title) throw new Error("标题不能为空。");
  if (!body) throw new Error(categoryId === "product_explanations" ? "说明内容不能为空。" : "标准回复不能为空。");
  const data = {
    ...(editor.item?.data || {}),
    product_id: product.id,
    title,
    keywords: splitTags(document.getElementById("product-scoped-keywords")?.value || ""),
    [bodyField]: body,
  };
  if (categoryId === "product_faq") {
    data.question = document.getElementById("product-scoped-question")?.value.trim() || "";
  }
  if (categoryId === "product_rules") {
    data.allow_auto_reply = Boolean(document.getElementById("product-scoped-auto")?.checked);
    data.requires_handoff = Boolean(document.getElementById("product-scoped-handoff")?.checked);
    data.handoff_reason = document.getElementById("product-scoped-handoff-reason")?.value.trim() || "";
  }
  const itemId = editor.itemId || clientSafeId(`${product.id}-${categoryId}-${title}`, `${categoryId}-${Date.now()}`);
  const item = {
    ...(editor.item || {}),
    schema_version: 1,
    category_id: categoryId,
    id: itemId,
    status: "active",
    source: editor.item?.source || {type: "product_catalog"},
    data,
    runtime: {
      allow_auto_reply: categoryId === "product_rules" ? data.allow_auto_reply !== false : true,
      requires_handoff: categoryId === "product_rules" ? Boolean(data.requires_handoff) : false,
      risk_level: categoryId === "product_rules" && data.requires_handoff ? "high" : "normal",
    },
  };
  const path = editor.itemId
    ? `/api/knowledge/categories/${encodeURIComponent(categoryId)}/items/${encodeURIComponent(itemId)}`
    : `/api/knowledge/categories/${encodeURIComponent(categoryId)}/items`;
  await apiJson(path, {method: editor.itemId ? "PUT" : "POST", body: JSON.stringify(item)});
  state.productScopedEditor = null;
  await Promise.all([loadProductDetail(product.id), loadOverview().catch(() => {})]);
}

async function deleteProductScopedInlineItem() {
  const editor = state.productScopedEditor;
  const product = state.selectedProduct;
  if (!editor?.categoryId || !editor.itemId || !product?.id) return;
  if (!confirm("确认归档这条商品专属知识吗？")) return;
  await apiJson(`/api/knowledge/categories/${encodeURIComponent(editor.categoryId)}/items/${encodeURIComponent(editor.itemId)}`, {method: "DELETE"});
  state.productScopedEditor = null;
  await Promise.all([loadProductDetail(product.id), loadOverview().catch(() => {})]);
}

function productScopedCategoryTitle(categoryId) {
  return {
    product_faq: "商品专属问答",
    product_rules: "商品专属规则",
    product_explanations: "商品专属解释",
  }[categoryId] || categoryId || "商品专属知识";
}

function productScopedBodyField(categoryId) {
  return categoryId === "product_explanations" ? "content" : "answer";
}

function clientSafeId(value, fallback) {
  const normalized = String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_.-]+/g, "_")
    .replace(/^[_ .-]+|[_ .-]+$/g, "")
    .slice(0, 96);
  return normalized || fallback;
}

function formatProductPrice(display) {
  if (!display || display.price === undefined || display.price === null || display.price === "") return "未填写价格";
  const category = String(display.category || "");
  const sku = String(display.sku || "");
  const numeric = Number(display.price);
  const unit = display.unit ? ` / ${display.unit}` : "";
  if (Number.isFinite(numeric) && numeric > 0 && numeric < 1000 && (category.includes("二手车") || /^CHEJIN-/i.test(sku))) {
    return `${display.price}万${unit}`;
  }
  return `${display.price}${unit}`;
}

async function adjustProductInventory(operation, quantity) {
  if (!state.selectedProduct?.id) return;
  const payload = await apiJson(`/api/product-console/products/${encodeURIComponent(state.selectedProduct.id)}/inventory`, {
    method: "POST",
    body: JSON.stringify({operation, quantity}),
  });
  state.selectedProduct = payload.item || state.selectedProduct;
  await Promise.all([loadProductCatalog({loadDetail: false}), loadOverview().catch(() => {})]);
  await loadProductDetail(state.selectedProduct.id);
}

async function runProductCommand() {
  const input = document.getElementById("product-command-input");
  const message = (input?.value || "").trim();
  if (!message) return;
  const payload = await apiJson("/api/product-console/command", {
    method: "POST",
    body: JSON.stringify({message, use_llm: true}),
  });
  if (payload.action === "draft_product" && payload.session) {
    state.generatorSession = payload.session;
    state.generatorMessages = payload.session.history || [];
    state.activeIntakeTab = "generator";
    selectView("generator");
    renderGenerator();
    return;
  }
  if (input) input.value = "";
  await Promise.all([loadProductCatalog({loadDetail: false}), loadOverview().catch(() => {})]);
  if (payload.item?.id) await loadProductDetail(payload.item.id);
  else if (state.selectedProduct?.id) await loadProductDetail(state.selectedProduct.id);
}

function openSelectedProductInFormalKnowledge() {
  if (!state.selectedProduct?.id) return;
  state.activeCategoryId = "products";
  selectView("knowledge");
  loadKnowledge().then(() => {
    state.selectedKnowledge = state.categoryItems.find((item) => item.id === state.selectedProduct.id) || state.selectedKnowledge;
    state.knowledgeMode = "view";
    renderKnowledgeList();
    renderKnowledgeDetail();
  }).catch((error) => alert(error.message));
}

function openProductScopedKnowledge(categoryId, itemId, productId) {
  if (!categoryId || !itemId) return;
  state.productScopedEditContext = {
    categoryId,
    itemId,
    productId: productId || state.selectedProduct?.id || "",
    productName: state.selectedProduct?.display?.name || state.selectedProduct?.data?.name || productId || "",
  };
  state.activeCategoryId = categoryId;
  selectView("knowledge", {keepKnowledgeContext: true});
  loadKnowledge().then(() => {
    state.selectedKnowledge = state.categoryItems.find((item) => item.id === itemId) || state.categoryItems[0] || null;
    state.knowledgeMode = "view";
    renderKnowledgeList();
    renderKnowledgeDetail();
  }).catch((error) => alert(error.message));
}

function updateKnowledgeButtons() {
  setHidden("save-knowledge-item", true);
  setHidden("cancel-knowledge-edit", true);
  setHidden("edit-knowledge-item", true);
  setHidden("archive-knowledge-item", true);
}

function knowledgeReadonlyHtml(category, item) {
  const rows = buildReadonlyRows(category, item);
  const runtime = item.runtime || {};
  const reviewState = item.review_state || {};
  const contextBadges = knowledgeScopeBadges(category, item);
  const highlighted = diagnosticTargetMatches(category?.id, item.id) ? " diagnostic-highlight" : "";
  return `
    <div class="read-head${highlighted}">
      <div>
        <p class="eyebrow">${escapeHtml(category.name || category.id)}</p>
        <h2 id="knowledge-detail-title">${escapeHtml(primaryTitle(category, item))}</h2>
        ${badgeListHtml([...(item.display_badges || []), ...contextBadges])}
      </div>
      <div class="read-actions">
        ${reviewState.is_new ? `<button class="secondary-button knowledge-acknowledge" type="button">已阅</button>` : ""}
        <span class="status-chip ${item.status === "archived" ? "warning" : "ok"}">${item.status === "archived" ? "已归档" : "启用中"}</span>
        <button class="secondary-button knowledge-modal-close" type="button">关闭</button>
        <button class="secondary-button knowledge-edit-inline" type="button">编辑</button>
        <button class="secondary-button danger-button knowledge-archive-inline" type="button">归档</button>
      </div>
    </div>
    ${knowledgeContextNoticeHtml(category, item)}
    <div class="summary-table">
      <div><span>知识 ID</span><strong>${escapeHtml(item.id)}</strong></div>
      <div><span>自动回复</span><strong>${runtime.allow_auto_reply !== false ? "允许" : "关闭"}</strong></div>
      <div><span>转人工</span><strong>${runtime.requires_handoff ? "需要" : "不需要"}</strong></div>
      <div><span>风险等级</span><strong>${escapeHtml(runtime.risk_level || "normal")}</strong></div>
    </div>
    <div class="read-grid">
      ${rows.join("")}
    </div>
  `;
}

function buildReadonlyRows(category, item) {
  const data = item.data || {};
  return (category.schema?.fields || [])
    .filter((field) => !isEmpty(data[field.id]))
    .map((field) => readFieldHtml(field, data[field.id]));
}

function readFieldHtml(field, value) {
  const wide = field.type === "long_text" || field.type === "object" || field.type === "table" || field.type === "tags";
  return `
    <div class="read-field ${wide ? "wide-field" : ""}">
      <span>${escapeHtml(fieldLabel(field))}</span>
      ${fieldValueHtml(field, value)}
    </div>
  `;
}

function fieldValueHtml(field, value) {
  if (field.type === "boolean") {
    return `<p>${value ? "是" : "否"}</p>`;
  }
  if (field.type === "single_select") {
    return `<p>${escapeHtml(optionLabel(field.id, value))}</p>`;
  }
  if (field.type === "tags") {
    return `<div class="chip-list">${(Array.isArray(value) ? value : splitTags(value)).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>`;
  }
  if (field.type === "table") {
    const columns = field.columns || [];
    return `
      <div class="display-table">
        <div class="display-table-head">${columns.map((column) => `<strong>${escapeHtml(column.label || column.id)}</strong>`).join("")}</div>
        ${(Array.isArray(value) ? value : []).map((row) => `
          <div class="display-table-row">${columns.map((column) => `<span>${escapeHtml(row?.[column.id] ?? "")}</span>`).join("")}</div>
        `).join("")}
      </div>
    `;
  }
  if (field.type === "object") {
    return objectValueHtml(value);
  }
  return `<p>${escapeHtml(value)}</p>`;
}

function objectValueHtml(value) {
  const entries = Object.entries(value || {});
  if (!entries.length) return `<p>无</p>`;
  return `
    <div class="variable-table">
      <div class="variable-table-head"><strong>变量名</strong><strong>对应内容</strong></div>
      ${entries.map(([key, inner]) => `
        <div class="variable-table-row">
          <code>${escapeHtml(templateLabels[key] || key)}</code>
          <span>${escapeHtml(displayBusinessValue(inner))}</span>
        </div>
      `).join("")}
    </div>
  `;
}

function primaryTitle(category, item) {
  const titleField = category?.schema?.item_title_field || "title";
  return item.data?.[titleField] || item.id || "未命名知识";
}

function knowledgeFormHtml(category, item) {
  const fields = category.schema?.fields || [];
  const runtime = item.runtime || {};
  const readonlyFields = isProductScopedCategory(category) ? new Set(["product_id"]) : new Set();
  return `
    <div class="read-head">
      <div>
        <p class="eyebrow">${escapeHtml(category.name || category.id)}</p>
        <h2 id="knowledge-detail-title">${state.knowledgeMode === "new" ? "新增知识" : `编辑：${escapeHtml(primaryTitle(category, item))}`}</h2>
      </div>
      <div class="read-actions">
        <button class="secondary-button knowledge-modal-close" type="button">关闭</button>
        <button class="secondary-button knowledge-cancel-inline" type="button">取消</button>
        <button class="primary-button knowledge-save-inline" type="button">保存</button>
      </div>
    </div>
    ${knowledgeContextNoticeHtml(category, item, {editing: true})}
    <div class="form-summary">
      <label class="form-field">
        <span>知识 ID</span>
        <input id="field-id" value="${escapeHtml(item.id || "")}" ${item.id && state.knowledgeMode !== "new" ? "readonly" : ""} />
      </label>
      <label class="form-field">
        <span>状态</span>
        <select id="field-status">
          <option value="active" ${item.status !== "archived" ? "selected" : ""}>启用</option>
          <option value="archived" ${item.status === "archived" ? "selected" : ""}>归档</option>
        </select>
      </label>
      <label class="checkbox-line"><input id="runtime-auto" type="checkbox" ${runtime.allow_auto_reply !== false ? "checked" : ""} /> 允许自动回复</label>
      <label class="checkbox-line"><input id="runtime-handoff" type="checkbox" ${runtime.requires_handoff ? "checked" : ""} /> 必须转人工</label>
    </div>
    <div class="form-grid" id="knowledge-form" data-category="${escapeHtml(category.id)}">
      ${fields.map((field) => fieldHtml(field, item.data?.[field.id], {readonlyFields, categoryId: category.id, productName: knowledgeProductName(item)})).join("")}
    </div>
  `;
}

function fieldHtml(field, value, renderOptions = {}) {
  const id = `data-${field.id}`;
  const label = `${fieldLabel(field)}${field.required ? " *" : ""}`;
  const readonly = renderOptions.readonlyFields?.has?.(field.id);
  if (field.id === "product_id") {
    return productIdSelectHtml(field, value, renderOptions);
  }
  if (field.id === "product_category") {
    return productCategorySelectHtml(field, value, renderOptions);
  }
  if (field.type === "boolean") {
    return `<label class="checkbox-line" data-field="${escapeHtml(field.id)}"><input id="${escapeHtml(id)}" type="checkbox" ${value ? "checked" : ""} ${readonly ? "disabled" : ""} /> ${escapeHtml(label)}</label>`;
  }
  if (field.type === "single_select") {
    const choices = field.options || [];
    return `
      <label class="form-field" data-field="${escapeHtml(field.id)}" data-kind="single_select">
        <span>${escapeHtml(label)}</span>
        <select id="${escapeHtml(id)}" ${readonly ? "disabled" : ""}>${choices.map((option) => `<option value="${escapeHtml(option)}" ${option === value ? "selected" : ""}>${escapeHtml(optionLabel(field.id, option))}</option>`).join("")}</select>
      </label>
    `;
  }
  if (field.type === "tags") {
    return `
      <label class="form-field wide-field" data-field="${escapeHtml(field.id)}" data-kind="tags">
        <span>${escapeHtml(label)}</span>
        <textarea id="${escapeHtml(id)}" placeholder="可用逗号、顿号或换行分隔" ${readonly ? "readonly" : ""}>${escapeHtml(displayTags(value))}</textarea>
      </label>
    `;
  }
  if (field.type === "table") {
    return tableFieldHtml(field, Array.isArray(value) ? value : []);
  }
  if (field.type === "object") {
    return objectFieldHtml(field, value && typeof value === "object" && !Array.isArray(value) ? value : {}, renderOptions);
  }
  if (field.type === "long_text") {
    return `
      <label class="form-field wide-field" data-field="${escapeHtml(field.id)}" data-kind="long_text">
        <span>${escapeHtml(label)}</span>
        <textarea id="${escapeHtml(id)}" ${readonly ? "readonly" : ""}>${escapeHtml(value || "")}</textarea>
      </label>
    `;
  }
  return `
    <label class="form-field" data-field="${escapeHtml(field.id)}" data-kind="${escapeHtml(field.type || "short_text")}">
      <span>${escapeHtml(label)}</span>
      <input id="${escapeHtml(id)}" value="${escapeHtml(value ?? "")}" ${readonly ? "readonly" : ""} />
    </label>
  `;
}

function tableFieldHtml(field, rows) {
  const columns = field.columns || [
    {id: "name", label: "名称", type: "short_text"},
    {id: "value", label: "内容", type: "short_text"},
  ];
  const safeRows = rows.length ? rows : field.id === "price_tiers" ? [{min_quantity: "", unit_price: ""}] : [{}];
  return `
    <div class="form-field wide-field table-editor" data-field="${escapeHtml(field.id)}" data-kind="table">
      <span>${escapeHtml(fieldLabel(field))}${field.required ? " *" : ""}</span>
      <div class="mini-table" data-columns="${escapeHtml(columns.map((column) => column.id).join(","))}">
        <div class="mini-table-head">${columns.map((column) => `<strong>${escapeHtml(column.label || column.id)}</strong>`).join("")}<strong></strong></div>
        <div class="mini-table-body">
          ${safeRows.map((row) => tableRowHtml(columns, row)).join("")}
        </div>
      </div>
      <button class="secondary-button mini-add" type="button">${field.id === "price_tiers" ? "新增价格档" : "新增一行"}</button>
    </div>
  `;
}

function tableRowHtml(columns, row) {
  return `
    <div class="mini-table-row">
      ${columns.map((column) => `<input data-column="${escapeHtml(column.id)}" data-type="${escapeHtml(column.type || "short_text")}" value="${escapeHtml(row?.[column.id] ?? "")}" />`).join("")}
      <button class="secondary-button mini-remove" type="button">删除</button>
    </div>
  `;
}

function objectFieldHtml(field, value, options = {}) {
  if (field.id === "reply_templates") {
    const keys = Array.from(new Set([...Object.keys(templateLabels), ...Object.keys(value)]));
    return `
      <div class="form-field wide-field reply-template-editor" data-field="${escapeHtml(field.id)}" data-kind="object">
        <span>${escapeHtml(fieldLabel(field))}</span>
        <div class="object-guide">这些是「${escapeHtml(options.productName || "当前商品")}」的可选客服回复模板。留空表示使用通用话术或 AI 经验，不会影响商品基础资料。</div>
        ${keys.map((key) => `
          <label class="nested-field">
            <span>${escapeHtml(templateLabels[key] || key)}</span>
            <textarea data-template-key="${escapeHtml(key)}">${escapeHtml(value[key] || "")}</textarea>
          </label>
        `).join("")}
      </div>
    `;
  }
  const entries = Object.entries(value);
  const rows = entries.length ? entries : [["", ""]];
  return `
    <div class="form-field wide-field object-editor" data-field="${escapeHtml(field.id)}" data-kind="object">
      <span>${escapeHtml(fieldLabel(field))}</span>
      <div class="object-guide">左侧变量名用于系统识别，已有变量不可改；右侧填写给客户看的内容。</div>
      <div class="object-rows">
        ${rows.map(([key, val]) => objectRowHtml(key, val, Boolean(key))).join("")}
      </div>
      <button class="secondary-button object-add" type="button">新增字段</button>
    </div>
  `;
}

function objectRowHtml(key, value, locked = false) {
  return `
    <div class="object-row">
      <input data-object-key value="${escapeHtml(key)}" placeholder="变量名" ${locked ? "readonly" : ""} />
      <input data-object-value value="${escapeHtml(value)}" placeholder="内容" />
      <button class="secondary-button object-remove" type="button" ${locked ? "title=\"变量名已固定，可删除该行\"" : ""}>删除</button>
    </div>
  `;
}

function bindDynamicEditors(root) {
  root.querySelectorAll(".mini-add").forEach((button) => {
    button.addEventListener("click", () => {
      const editor = button.closest(".table-editor");
      const columns = (editor.querySelector(".mini-table").dataset.columns || "").split(",").filter(Boolean)
        .map((id) => ({id, label: id, type: id.includes("price") ? "money" : "number"}));
      editor.querySelector(".mini-table-body").insertAdjacentHTML("beforeend", tableRowHtml(columns, {}));
      bindDynamicEditors(editor);
    });
  });
  root.querySelectorAll(".mini-remove").forEach((button) => {
    button.onclick = () => button.closest(".mini-table-row").remove();
  });
  root.querySelectorAll(".object-add").forEach((button) => {
    button.onclick = () => {
      button.closest(".object-editor").querySelector(".object-rows").insertAdjacentHTML("beforeend", objectRowHtml("", ""));
      bindDynamicEditors(button.closest(".object-editor"));
    };
  });
  root.querySelectorAll(".object-remove").forEach((button) => {
    button.onclick = () => button.closest(".object-row").remove();
  });
}

function collectKnowledgeForm() {
  const category = activeCategory();
  if (!category) throw new Error("没有选中门类");
  const existing = state.selectedKnowledge || {data: {}, runtime: {}};
  const data = {};
  for (const field of category.schema?.fields || []) {
    data[field.id] = collectFieldValue(field);
  }
  validateClientKnowledge(category, data);
  return {
    ...existing,
    id: document.getElementById("field-id").value.trim(),
    category_id: category.id,
    status: document.getElementById("field-status").value,
    data,
    runtime: {
      allow_auto_reply: document.getElementById("runtime-auto").checked,
      requires_handoff: document.getElementById("runtime-handoff").checked,
      risk_level: document.getElementById("runtime-handoff").checked ? "high" : existing.runtime?.risk_level || "normal",
    },
  };
}

function collectFieldValue(field, root = document) {
  const scope = root || document;
  const wrapper = scope.querySelector(`[data-field="${cssEscape(field.id)}"]`);
  const element = wrapper?.querySelector(`#${cssEscape(`data-${field.id}`)}`) || scope.querySelector(`#${cssEscape(`data-${field.id}`)}`);
  if (field.type === "boolean") return Boolean(element?.checked);
  if (field.type === "number" || field.type === "money") return numberOrNull(element?.value);
  if (field.type === "tags") return splitTags(element?.value || "");
  if (field.type === "table") return collectTableValue(wrapper);
  if (field.type === "object") return collectObjectValue(wrapper);
  return (element?.value || "").trim();
}

function collectTableValue(wrapper) {
  if (!wrapper) return [];
  return Array.from(wrapper.querySelectorAll(".mini-table-row"))
    .map((row) => {
      const item = {};
      row.querySelectorAll("[data-column]").forEach((input) => {
        const type = input.dataset.type || "short_text";
        item[input.dataset.column] = type === "number" || type === "money" ? numberOrNull(input.value) : input.value.trim();
      });
      return item;
    })
    .filter((row) => Object.values(row).some((value) => value !== "" && value !== null));
}

function collectObjectValue(wrapper) {
  if (!wrapper) return {};
  if (wrapper.classList.contains("reply-template-editor")) {
    const item = {};
    wrapper.querySelectorAll("[data-template-key]").forEach((input) => {
      const value = input.value.trim();
      if (value) item[input.dataset.templateKey] = value;
    });
    return item;
  }
  const item = {};
  wrapper.querySelectorAll(".object-row").forEach((row) => {
    const key = row.querySelector("[data-object-key]").value.trim();
    const value = row.querySelector("[data-object-value]").value.trim();
    if (key && value) item[key] = value;
  });
  return item;
}

function validateClientKnowledge(category, data) {
  for (const field of category.schema?.fields || []) {
    if (field.required && isEmpty(data[field.id])) throw new Error(`${fieldLabel(field)} 不能为空`);
  }
  if (Array.isArray(data.price_tiers)) {
    let previousQuantity = 0;
    let previousPrice = Infinity;
    data.price_tiers.forEach((row, index) => {
      const quantity = Number(row.min_quantity);
      const price = Number(row.unit_price);
      if (!Number.isFinite(quantity) || !Number.isFinite(price)) throw new Error(`第 ${index + 1} 档阶梯价格缺少数量或价格`);
      if (quantity <= previousQuantity) throw new Error(`第 ${index + 1} 档数量必须高于上一档`);
      if (price >= previousPrice) throw new Error(`第 ${index + 1} 档价格必须低于上一档`);
      previousQuantity = quantity;
      previousPrice = price;
    });
  }
}

async function saveKnowledgeItem() {
  const item = collectKnowledgeForm();
  if (!item.id) throw new Error("知识 ID 不能为空");
  const exists = state.knowledgeMode !== "new" && Boolean(state.selectedKnowledge?.id);
  const categoryId = encodeURIComponent(state.activeCategoryId);
  const path = exists
    ? `/api/knowledge/categories/${categoryId}/items/${encodeURIComponent(item.id)}`
    : `/api/knowledge/categories/${categoryId}/items`;
  await apiJson(path, {method: exists ? "PUT" : "POST", body: JSON.stringify(item)});
  state.knowledgeMode = "view";
  await loadKnowledge();
  state.selectedKnowledge = state.categoryItems.find((candidate) => candidate.id === item.id) || state.selectedKnowledge || state.categoryItems[0] || null;
  renderKnowledgeList();
  renderKnowledgeDetail();
  openKnowledgeDetailModal();
  await Promise.all([loadOverview(), refreshProductCatalogIfNeeded(item.category_id).catch(() => {})]);
}

function newKnowledgeItem() {
  const category = activeCategory();
  if (!category) return;
  const data = {};
  for (const field of category.schema?.fields || []) {
    data[field.id] = defaultFieldValue(field);
  }
  state.selectedKnowledge = {
    schema_version: 1,
    category_id: category.id,
    id: "",
    status: "active",
    source: {type: "admin_form"},
    data,
    runtime: {allow_auto_reply: true, requires_handoff: false, risk_level: "normal"},
  };
  state.knowledgeMode = "new";
  renderKnowledgeList();
  renderKnowledgeDetail();
  openKnowledgeDetailModal();
}

function editKnowledgeItem() {
  if (!state.selectedKnowledge) return;
  state.knowledgeMode = "edit";
  renderKnowledgeDetail();
  openKnowledgeDetailModal();
}

function cancelKnowledgeEdit() {
  if (state.knowledgeMode === "new" && !state.selectedKnowledge?.id) {
    state.selectedKnowledge = state.categoryItems[0] || null;
    state.knowledgeMode = "view";
    renderKnowledgeList();
    renderKnowledgeDetail();
    closeKnowledgeDetailModal();
    return;
  }
  state.knowledgeMode = "view";
  renderKnowledgeDetail();
}

async function archiveKnowledgeItem() {
  if (!state.selectedKnowledge?.id) return;
  if (!confirm("确认归档这条知识吗？")) return;
  const categoryId = state.activeCategoryId;
  await apiJson(`/api/knowledge/categories/${encodeURIComponent(state.activeCategoryId)}/items/${encodeURIComponent(state.selectedKnowledge.id)}`, {method: "DELETE"});
  closeKnowledgeDetailModal();
  await Promise.all([loadKnowledge(), loadOverview(), refreshProductCatalogIfNeeded(categoryId).catch(() => {})]);
}

async function refreshProductCatalogIfNeeded(categoryId) {
  if (categoryId === "products" || isProductScopedCategory(categoryId)) {
    await loadProductCatalog({loadDetail: false});
  }
}

async function createCustomCategory() {
  const id = prompt("门类 ID：小写英文、数字、下划线或连字符");
  if (!id) return;
  const name = prompt("门类名称", id) || id;
  const customFields = prompt("自定义字段，可用逗号分隔；留空则使用“标题/内容”模板", "");
  await apiJson("/api/knowledge/categories", {
    method: "POST",
    body: JSON.stringify({
      id,
      name,
      description: "用户自定义知识门类",
      participates_in_reply: true,
      fields: buildCustomCategoryFields(customFields || ""),
    }),
  });
  state.activeCategoryId = id;
  await loadKnowledge();
}

function buildCustomCategoryFields(text) {
  const labels = splitTags(text).slice(0, 12);
  if (!labels.length) return undefined;
  const fields = [
    {id: "title", label: "标题", type: "short_text", required: true, searchable: true, form_order: 10},
  ];
  labels.forEach((label, index) => {
    const id = safeFieldId(label, `field_${index + 1}`);
    if (id === "title" || fields.some((field) => field.id === id)) return;
    fields.push({id, label, type: "long_text", required: false, searchable: true, form_order: 20 + index * 10});
  });
  if (fields.length === 1) {
    fields.push({id: "content", label: "内容", type: "long_text", required: false, searchable: true, form_order: 20});
  }
  return fields;
}

function safeFieldId(label, fallback) {
  const ascii = String(label || "").toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
  return /^[a-z][a-z0-9_]{0,40}$/.test(ascii) ? ascii : fallback;
}

async function sendGeneratorMessage() {
  const input = document.getElementById("generator-input");
  const message = input.value.trim();
  if (!message) return;
  state.generatorMessages.push({role: "user", content: message});
  renderGenerator();
  const preferred = document.getElementById("generator-category").value;
  const payload = state.generatorSession
    ? await apiJson(`/api/generator/sessions/${encodeURIComponent(state.generatorSession.session_id)}/messages`, {method: "POST", body: JSON.stringify({message, use_llm: true})})
    : await apiJson("/api/generator/sessions", {method: "POST", body: JSON.stringify({message, preferred_category_id: preferred, use_llm: true})});
  state.generatorSession = payload.session;
  state.generatorMessages.push({role: "assistant", content: generatorReplyText(payload.session)});
  input.value = "";
  renderGenerator();
}

function generatorReplyText(session) {
  if (!session) return "";
  if (session.status === "ready") return "信息已整理完整，可以确认加入AI经验池。";
  if (session.status === "sent_to_rag_experience") return "这条内容已加入AI经验池，可去“AI经验池”查看AI建议。";
  if (session.status === "saved") return "已经保存到正式知识库。";
  return session.question || "还需要继续补充关键信息。";
}

function generatorLlmAssistHtml(session) {
  const assist = session?.llm_assist || {};
  if (!assist.policy_version) return "";
  const status = assist.status || "";
  const usedModel = status === "model_generated";
  const statusText = {
    model_generated: "已使用大模型整理字段",
    rule_fallback_after_llm: "已尝试大模型，当前为规则兜底",
    rule_only_disabled_by_request: "本次未启用大模型，仅规则整理",
  }[status] || "AI整理状态已记录";
  const reason = assist.reason || (usedModel ? "大模型已参与分类和字段抽取。" : "大模型不可用或未返回合格结构，当前结果来自规则兜底。");
  return `
    <div class="status-card ${usedModel ? "ok" : "warning"}">
      <strong>${escapeHtml(statusText)}</strong>
      <span>${escapeHtml(reason)}</span>
    </div>
  `;
}

function renderGenerator() {
  const chat = document.getElementById("generator-chat");
  const confirmButton = document.getElementById("confirm-generator");
  chat.innerHTML = state.generatorMessages.length
    ? state.generatorMessages.map((msg) => `<div class="chat-bubble ${msg.role}">${escapeHtml(msg.content)}</div>`).join("")
    : `<div class="empty-state">输入一段自然语言，系统会优先用大模型整理成可加入AI经验池的结构化内容。</div>`;
  const summary = document.getElementById("generator-summary");
  const session = state.generatorSession;
  if (!session) {
    summary.innerHTML = "";
    confirmButton.disabled = true;
    confirmButton.textContent = "确认加入AI经验池";
    return;
  }
  const warnings = session.warnings || [];
  summary.innerHTML = `
    <div class="status-card ${session.status === "ready" ? "ok" : "warning"}">
      <strong>${escapeHtml(session.category_name || session.category_id || "待判断")}</strong>
      <span>${escapeHtml(session.provider || "local")} · ${escapeHtml(session.status)}</span>
    </div>
    ${generatorLlmAssistHtml(session)}
    ${warnings.length ? `<div class="warning-list">${warnings.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>` : ""}
    <div class="summary-table generator-table">
      ${(session.summary_rows || []).map((row) => `<div><span>${escapeHtml(row.label)}</span><strong>${escapeHtml(row.value)}</strong></div>`).join("")}
    </div>
    ${generatorDraftEditorHtml(session)}
  `;
  bindDynamicEditors(summary);
  summary.querySelector("#save-generator-draft")?.addEventListener("click", () => updateGeneratorDraft().catch((error) => alert(error.message)));
  const confirmReady = session.status === "ready" && !state.generatorConfirmBusy;
  confirmButton.disabled = !confirmReady;
  confirmButton.innerHTML = state.generatorConfirmBusy
    ? '<span class="loading-spinner button-spinner" aria-hidden="true"></span><span>确认中</span>'
    : "确认加入AI经验池";
}

function generatorDraftEditorHtml(session) {
  const category = categoryById(session.category_id);
  const item = session.draft_item || {};
  if (!category || !item.data || session.status === "saved" || session.status === "sent_to_rag_experience") return "";
  const fields = category.schema?.fields || [];
  return `
    <div class="generator-editor">
      <div class="editor-head">
        <div>
          <strong>可编辑草稿</strong>
          <span>不满意的话术或字段可以先改，保存后系统会重新校验。</span>
        </div>
      </div>
      <div class="form-grid generator-draft-form" id="generator-draft-form" data-category="${escapeHtml(category.id)}">
        ${fields.map((field) => fieldHtml(field, item.data?.[field.id])).join("")}
      </div>
      <button class="secondary-button" id="save-generator-draft" type="button">保存修改并重新校验</button>
    </div>
  `;
}

function resetGenerator() {
  state.generatorSession = null;
  state.generatorMessages = [];
  document.getElementById("generator-input").value = "";
  renderGenerator();
}

async function updateGeneratorDraft() {
  const session = state.generatorSession;
  if (!session?.session_id) return;
  const category = categoryById(session.category_id);
  const form = document.getElementById("generator-draft-form");
  if (!category || !form) throw new Error("没有可编辑的知识草稿");
  const data = {};
  for (const field of category.schema?.fields || []) {
    data[field.id] = collectFieldValue(field, form);
  }
  validateClientKnowledge(category, data);
  const payload = await apiJson(`/api/generator/sessions/${encodeURIComponent(session.session_id)}/draft`, {
    method: "PATCH",
    body: JSON.stringify({data}),
  });
  state.generatorSession = payload.session;
  state.generatorMessages.push({role: "assistant", content: generatorReplyText(payload.session)});
  renderGenerator();
}

async function confirmGenerator() {
  if (!state.generatorSession?.session_id || state.generatorConfirmBusy) return;
  if (!confirm("确认将这条内容加入AI经验池吗？")) return;
  state.generatorConfirmBusy = true;
  renderGenerator();
  try {
    const payload = await apiJson(`/api/generator/sessions/${encodeURIComponent(state.generatorSession.session_id)}/rag-experience`, {
      method: "POST",
      body: JSON.stringify({use_llm: true}),
    });
    state.generatorSession = payload.session;
    const experienceId = payload.session?.rag_experience_id || payload.item?.experience_id || "";
    const tip = experienceId ? `（${experienceId}）` : "";
    state.generatorMessages.push({role: "assistant", content: `已加入AI经验池${tip}。`});
    await Promise.all([
      loadOverview().catch(console.error),
      refreshRagExperienceBadge().catch(console.error),
      loadRagExperiences({fast: true}).catch(console.error),
    ]);
  } finally {
    state.generatorConfirmBusy = false;
    renderGenerator();
  }
}

async function uploadSelectedFile() {
  if (state.uploadInProgress) return;
  const fileInput = document.getElementById("upload-file");
  const files = Array.from(fileInput.files || []);
  if (!files.length) {
    return;
  }
  setUploadBusy(true, files.length);
  const form = new FormData();
  form.append("kind", document.getElementById("upload-kind").value);
  files.forEach((file) => form.append("files", file));
  try {
    const response = await fetch("/api/uploads/batch", {method: "POST", body: form});
    if (!response.ok) throw new Error(await responseErrorMessage(response, "/api/uploads/batch"));
    const payload = await response.json();
    const results = Array.isArray(payload.results) ? payload.results : [];
    const successCount = results.filter((item) => Boolean(item?.ok)).length;
    const failures = results.filter((item) => !item.ok);
    fileInput.value = "";
    await loadUploads();
    if (successCount > 0 && state.autoLearnAfterUpload) {
      await runLearning();
    }
    if (failures.length) {
      alert(`有 ${failures.length} 个文件上传失败：\n${failures.map((item) => `${item.filename || "未命名文件"}：${item.message || "未知错误"}`).join("\n")}`);
    }
  } catch (error) {
    document.getElementById("upload-list").innerHTML = `<div class="status-card error"><strong>上传失败</strong><span>${escapeHtml(error.message || "请稍后重试")}</span></div>`;
    throw error;
  } finally {
    setUploadBusy(false);
  }
}

async function loadUploads() {
  const payload = await apiGet("/api/uploads");
  const list = document.getElementById("upload-list");
  list.innerHTML = (payload.items || [])
    .map((item) => `
      <div class="record-row upload-row">
        <div>
          <strong>${escapeHtml(item.filename)}</strong>
          <span>${escapeHtml(uploadKindText(item))} · ${item.learned ? "已学习" : "未学习"} · ${formatBytes(item.size || 0)}</span>
        </div>
        <button class="secondary-button danger-button upload-delete" data-upload-id="${escapeHtml(item.upload_id)}" data-filename="${escapeHtml(item.filename)}">删除</button>
      </div>
    `)
    .join("") || `<div class="empty-state">暂无上传</div>`;
  list.querySelectorAll(".upload-delete").forEach((button) => {
    button.addEventListener("click", () => deleteUpload(button.dataset.uploadId, button.dataset.filename).catch((error) => alert(error.message)));
  });
}

function setUploadBusy(isBusy, fileCount = 0) {
  state.uploadInProgress = isBusy;
  const fileInput = document.getElementById("upload-file");
  const kindSelect = document.getElementById("upload-kind");
  if (fileInput) fileInput.disabled = isBusy;
  if (kindSelect) kindSelect.disabled = isBusy;
  if (isBusy) {
    document.getElementById("upload-list").innerHTML = `
      <div class="status-card loading">
        <strong><span class="loading-spinner" aria-hidden="true"></span>正在上传</strong>
        <span>已选择 ${fileCount} 个文件，上传完成后会自动出现在下方列表。</span>
      </div>
    `;
  }
}

async function deleteUpload(uploadId, filename) {
  if (!uploadId) return;
  const label = filename || uploadId;
  if (!confirm(`确认删除上传资料「${label}」？\n\n这会删除原始上传文件和上传记录；如果它已经生成候选，候选审核记录不会被自动删除。`)) return;
  await apiJson(`/api/uploads/${encodeURIComponent(uploadId)}`, {method: "DELETE"});
  await loadUploads();
}

async function loadRagStatus() {
  const [payload, analytics] = await Promise.all([
    apiGet("/api/rag/status"),
    apiGet("/api/rag/analytics").catch(() => null),
  ]);
  state.ragStatus = payload;
  state.ragAnalytics = analytics;
  renderRagStatus();
  renderRagAnalytics();
}

function renderRagStatus() {
  const status = state.ragStatus || {};
  const experienceCounts = status.experience_counts || {};
  document.getElementById("rag-status-cards").innerHTML = [
    ["资料源", status.source_count ?? 0],
    ["切片", status.chunk_count ?? 0],
    ["索引", status.index_exists ? "正常" : "未建立"],
    ["对话经验", experienceCounts.active ?? 0],
  ]
    .map(([label, value]) => `<div class="metric-card"><span>${escapeHtml(value)}</span><label>${escapeHtml(label)}</label></div>`)
    .join("");
}

function renderRagAnalytics() {
  const panel = document.getElementById("rag-analytics");
  if (!panel) return;
  const analytics = state.ragAnalytics;
  if (!analytics?.ok) {
    panel.innerHTML = `<div class="empty-state">暂无运营分析数据。</div>`;
    return;
  }
  const audit = analytics.audit || {};
  const counters = audit.counters || {};
  const formalization = analytics.formalization_candidates || [];
  panel.innerHTML = `
    <div class="record-row">
      <strong>运营概览</strong>
      <span>参考资料应答 ${escapeHtml(counters.rag_reply_applied ?? 0)} 次 · 命中证据 ${escapeHtml(counters.rag_evidence_hit ?? 0)} 次 · 记录经验 ${escapeHtml(counters.rag_experience_recorded ?? 0)} 条</span>
      <p>建议转正式知识：${escapeHtml(formalization.length)} 条。参考资料只作为辅助证据，正式规则需走“知识录入与学习”。</p>
    </div>
  `;
}

async function rebuildRag() {
  const payload = await apiJson("/api/rag/rebuild", {method: "POST", body: JSON.stringify({})});
  await loadRagStatus();
  const index = payload.index || {};
  if (String(index.mode || "") === "queued_async_rebuild") {
    const queuedText = index.deduped
      ? `后台已有重建任务在运行（任务ID：${index.job_id || "unknown"}）。`
      : `已提交后台重建任务（任务ID：${index.job_id || "unknown"}）。`;
    document.getElementById("rag-results").innerHTML = `<div class="status-card info"><strong>索引重建已转后台</strong><span>${escapeHtml(queuedText)}</span></div>`;
    setTimeout(() => loadRagStatus().catch(() => {}), 1500);
    setTimeout(() => loadRagStatus().catch(() => {}), 4500);
    return;
  }
  document.getElementById("rag-results").innerHTML = `<div class="status-card ok"><strong>索引已重建</strong><span>当前索引片段数：${escapeHtml(payload.entry_count ?? index.entry_count ?? 0)}</span></div>`;
}

async function searchRag() {
  const query = document.getElementById("rag-query").value.trim();
  if (!query) {
    document.getElementById("rag-results").innerHTML = `<div class="empty-state">请输入要检索的问题。</div>`;
    return;
  }
  const payload = await apiJson("/api/rag/search", {
    method: "POST",
    body: JSON.stringify({
      query,
      product_id: document.getElementById("rag-product-id").value.trim(),
      limit: 8,
    }),
  });
  state.ragHits = payload.hits || [];
  renderRagResults(payload);
}

function renderRagResults(payload) {
  const hits = payload.hits || [];
  document.getElementById("rag-results").innerHTML = hits.length
    ? hits.map((hit) => `
        <div class="record-row rag-hit">
          <div>
            <strong>${escapeHtml(hit.category || hit.source_type || "资料片段")} · ${escapeHtml(hit.score)}</strong>
            <span>${escapeHtml(hit.product_id || "未指定商品")} · ${escapeHtml(hit.chunk_id || "")}</span>
            <p>${escapeHtml(hit.text || "")}</p>
          </div>
        </div>
      `).join("")
    : `<div class="empty-state">没有检索到相关资料片段。</div>`;
}

async function loadRagStatus() {
  const [payload, analytics, sources] = await Promise.all([
    apiGet("/api/rag/status"),
    apiGet("/api/rag/analytics").catch(() => null),
    apiGet("/api/rag/sources?limit=80").catch(() => ({ok: false, sources: [], chunks: []})),
  ]);
  state.ragStatus = payload;
  state.ragAnalytics = analytics;
  state.ragSources = sources.sources || [];
  state.ragChunks = sources.chunks || [];
  renderRagStatus();
  renderRagAnalytics();
  renderRagSources(sources);
}

function renderRagStatus() {
  const status = state.ragStatus || {};
  const experienceCounts = status.experience_counts || {};
  document.getElementById("rag-status-cards").innerHTML = [
    ["资料源", status.source_count ?? 0],
    ["切片", status.chunk_count ?? 0],
    ["索引", status.index_exists ? "正常" : "未建立"],
    ["对话经验", experienceCounts.active ?? 0],
  ]
    .map(([label, value]) => `<div class="metric-card"><span>${escapeHtml(value)}</span><label>${escapeHtml(label)}</label></div>`)
    .join("");
}

function renderRagAnalytics() {
  const panel = document.getElementById("rag-analytics");
  if (!panel) return;
  const analytics = state.ragAnalytics;
  if (!analytics?.ok) {
    panel.innerHTML = `<div class="empty-state">暂无运营分析数据。</div>`;
    return;
  }
  const audit = analytics.audit || {};
  const counters = audit.counters || {};
  const formalization = analytics.formalization_candidates || [];
  panel.innerHTML = `
    <div class="record-row reference-summary">
      <div>
        <strong>运营概览</strong>
        <span>参考资料应答 ${escapeHtml(counters.rag_reply_applied ?? 0)} 次 · 命中证据 ${escapeHtml(counters.rag_evidence_hit ?? 0)} 次 · 记录经验 ${escapeHtml(counters.rag_experience_recorded ?? 0)} 条</span>
        <p>建议转正式知识：${escapeHtml(formalization.length)} 条。参考资料和对话经验只做辅助，正式规则仍走“待确认知识”。</p>
      </div>
    </div>
  `;
}

function renderRagSources(payload = {}) {
  const sources = payload.sources || state.ragSources || [];
  const chunks = payload.chunks || state.ragChunks || [];
  const sourcePanel = document.getElementById("rag-source-list");
  const chunkPanel = document.getElementById("rag-chunk-list");
  if (sourcePanel) {
    sourcePanel.innerHTML = sources.length
      ? `
        <div class="section-mini-title">已导入资料源</div>
        ${sources.map((source) => `
          <div class="record-row rag-source-row">
            <div>
              <strong>${escapeHtml(sourceLabel(source))}</strong>
              <span>${escapeHtml(source.category || "未分类")} · ${escapeHtml(source.product_id || "未指定商品")} · ${escapeHtml(source.chunk_count ?? 0)} 个切片</span>
              <p>${escapeHtml(shortPath(source.source_path || ""))}</p>
            </div>
          </div>
        `).join("")}
      `
      : `<div class="empty-state">暂无已导入的参考资料。上传资料并 AI 整理后，这里会显示资料源和切片概况。</div>`;
  }
  if (chunkPanel) {
    chunkPanel.innerHTML = chunks.length
      ? `
        <div class="section-mini-title">资料切片预览</div>
        ${chunks.slice(0, 12).map((chunk) => `
          <details class="record-row rag-chunk-row">
            <summary>${escapeHtml(chunk.category || chunk.source_type || "资料片段")} · ${escapeHtml(chunk.chunk_id || "")}</summary>
            <p>${escapeHtml(chunk.text || "")}</p>
          </details>
        `).join("")}
      `
      : `<div class="empty-state">暂无资料切片。点击“重建索引”可重新生成。</div>`;
  }
}

async function loadRagExperiences(options = {}) {
  const fast = options.fast !== false;
  const defaultLimit = 80;
  const limit = Number.isFinite(Number(options.limit)) ? Math.max(20, Math.min(500, Number(options.limit))) : defaultLimit;
  const payload = await apiGet(`/api/rag/experiences?status=all&limit=${limit}&fast=${fast ? "true" : "false"}`);
  const rawItems = payload.items || [];
  const filteredItems = rawItems.filter((item) => !shouldHideRagExperience(item));
  const hiddenCount = Math.max(0, rawItems.length - filteredItems.length);
  const displayCounts = normalizeRagExperienceDisplayCounts(payload.display_counts, rawItems);
  const governanceCounts = normalizeRagExperienceGovernanceCounts(payload.governance_counts, rawItems);
  state.ragExperienceHiddenCount = hiddenCount;
  state.ragExperienceLoadedCount = Number(payload.loaded_count ?? rawItems.length) || rawItems.length;
  state.ragExperienceDisplayCounts = displayCounts;
  state.ragExperienceGovernanceCounts = governanceCounts;
  state.ragExperienceDiscardedTotal = displayCounts.discarded;
  state.ragExperienceTotal = displayCounts.total;
  state.ragExperiences = filteredItems;
  updateRagExperienceCountBadge(unreviewedRagExperienceCount(state.ragExperiences));
  renderRagExperiences({
    ...payload,
    items: filteredItems,
    ui_hidden_count: hiddenCount,
    ui_loaded_count: state.ragExperienceLoadedCount,
    display_counts: displayCounts,
    governance_counts: governanceCounts,
    ui_discarded_total: displayCounts.discarded,
    ui_total_count: displayCounts.total,
  });
  if (options.skipInterpret !== true) {
    ensureRagExperienceInterpretations(state.ragExperiences).catch((error) => console.warn("rag experience interpretation failed", error));
  }
}

function normalizeRagExperienceDisplayCounts(serverCounts = null, items = []) {
  const fallback = {total: items.length, pending: 0, kept: 0, promoted: 0, discarded: 0, other: 0};
  for (const item of items) {
    const displayState = ragExperienceDisplayState(item, item?.formal_relation || item?.status);
    if (displayState in fallback) fallback[displayState] += 1;
    else fallback.other += 1;
  }
  const source = serverCounts && typeof serverCounts === "object" ? serverCounts : fallback;
  const counts = {
    total: Math.max(0, Number(source.total ?? fallback.total) || 0),
    pending: Math.max(0, Number(source.pending ?? fallback.pending) || 0),
    kept: Math.max(0, Number(source.kept ?? fallback.kept) || 0),
    promoted: Math.max(0, Number(source.promoted ?? fallback.promoted) || 0),
    discarded: Math.max(0, Number(source.discarded ?? fallback.discarded) || 0),
    other: Math.max(0, Number(source.other ?? fallback.other) || 0),
  };
  counts.accounted_total = counts.pending + counts.kept + counts.promoted + counts.discarded + counts.other;
  counts.consistent = counts.accounted_total === counts.total;
  return counts;
}

function normalizeRagExperienceGovernanceCounts(serverCounts = null, items = []) {
  const fallback = {
    pending_review: 0,
    retrievable_experience: 0,
    kept_experience: 0,
    style_only: 0,
    candidate_suggested: 0,
    candidate_created: 0,
    auto_discarded: 0,
    user_discarded: 0,
    promoted: 0,
    blocked: 0,
    unknown: 0,
    total: items.length,
  };
  for (const item of items) {
    const effective = governanceEffectiveState(item) || "unknown";
    if (effective in fallback) fallback[effective] += 1;
    else fallback.unknown += 1;
  }
  const source = serverCounts && typeof serverCounts === "object" ? serverCounts : fallback;
  const states = [
    "pending_review",
    "retrievable_experience",
    "kept_experience",
    "style_only",
    "candidate_suggested",
    "candidate_created",
    "auto_discarded",
    "user_discarded",
    "promoted",
    "blocked",
    "unknown",
  ];
  const counts = {total: Math.max(0, Number(source.total ?? fallback.total) || 0)};
  for (const key of states) {
    counts[key] = Math.max(0, Number(source[key] ?? fallback[key]) || 0);
  }
  counts.accounted_total = states.reduce((sum, key) => sum + counts[key], 0);
  counts.consistent = counts.accounted_total === counts.total;
  return counts;
}

function ragGovernanceLabel(key) {
  return {
    pending_review: "待处理",
    retrievable_experience: "旧可检索状态",
    kept_experience: "已入AI经验池",
    style_only: "仅话术风格",
    candidate_suggested: "建议待确认",
    candidate_created: "已生成候选",
    auto_discarded: "自动降噪",
    user_discarded: "人工废弃",
    promoted: "已转待确认",
    blocked: "规则阻断",
    unknown: "未知",
  }[key] || key;
}

function ragGovernanceSummaryHtml(counts) {
  if (!counts || !counts.total) return "";
  const keys = [
    "pending_review",
    "retrievable_experience",
    "kept_experience",
    "style_only",
    "candidate_suggested",
    "candidate_created",
    "auto_discarded",
    "user_discarded",
    "promoted",
    "blocked",
    "unknown",
  ];
  const detail = keys
    .filter((key) => Number(counts[key] || 0) > 0 || key === "unknown")
    .map((key) => `${ragGovernanceLabel(key)} ${Number(counts[key] || 0)}`)
    .join(" · ");
  return `
    <div class="status-card ${counts.consistent ? "ok" : "warning"} rag-governance-summary">
      <strong>治理状态对账</strong>
      <span>${escapeHtml(detail || "暂无经验")} · 合计 ${escapeHtml(counts.accounted_total ?? 0)} / 总经验 ${escapeHtml(counts.total ?? 0)}</span>
    </div>
  `;
}

function ragExperienceProcessingNoticeHtml() {
  if (!state.ragInterpretationInProgress) return "";
  const count = Math.max(0, Number(state.ragInterpretationPendingCount) || 0);
  return `
    <div class="status-card loading rag-background-processing">
      <strong><span class="loading-spinner" aria-hidden="true"></span>后台处理中</strong>
      <span>AI 正在整理${count ? ` ${count} 条` : ""}AI经验池，处理完成后会自动刷新清单和统计，不会静默隐藏。</span>
    </div>
  `;
}

function ragActionNoticeHtml() {
  const notice = state.ragActionNotice;
  if (!notice) return "";
  const tone = notice.tone === "warning" ? "warning" : notice.tone === "ok" ? "ok" : "loading";
  return `
    <div class="status-card ${escapeHtml(tone)} rag-action-notice">
      <strong>${escapeHtml(notice.title || "处理结果")}</strong>
      <span>${escapeHtml(notice.message || "")}</span>
    </div>
  `;
}

async function refreshRagExperienceBadge() {
  const payload = await apiGet("/api/rag/experiences/unreviewed-count");
  const count = Math.max(0, Number(payload?.count) || 0);
  updateRagExperienceCountBadge(count);
}

function interpretationNeedsRefresh(item) {
  const ai = item?.ai_interpretation || {};
  return !ai.meaning || !ai.version || !ai.source_fingerprint;
}

async function ensureRagExperienceInterpretations(items = []) {
  if (state.ragInterpretationInProgress) return;
  const ids = items.filter(interpretationNeedsRefresh).map((item) => item.experience_id).filter(Boolean).slice(0, 40);
  if (!ids.length) return;
  state.ragInterpretationInProgress = true;
  state.ragInterpretationPendingCount = ids.length;
  state.ragInterpretationLastResult = null;
  renderRagExperiences({items: state.ragExperiences});
  try {
    const payload = await apiJson("/api/rag/experiences/interpret", {
      method: "POST",
      body: JSON.stringify({experience_ids: ids, force: false, limit: ids.length}),
    });
    state.ragInterpretationLastResult = {
      interpreted_count: Number(payload.interpreted_count || 0),
      model_count: Number(payload.model_count || 0),
      fallback_count: Number(payload.fallback_count || 0),
    };
    mergeInterpretedExperiences(payload.items || []);
  } finally {
    state.ragInterpretationInProgress = false;
    state.ragInterpretationPendingCount = 0;
    await loadRagExperiences({fast: true, skipInterpret: true}).catch(() => renderRagExperiences({items: state.ragExperiences}));
  }
}

async function interpretRagExperience(experienceId, options = {}) {
  if (!experienceId) return;
  if (state.ragInterpretationLoadingIds.has(experienceId)) return;
  state.ragInterpretationLoadingIds.add(experienceId);
  renderRagExperiences({items: state.ragExperiences});
  try {
    const payload = await apiJson(`/api/rag/experiences/${encodeURIComponent(experienceId)}/interpret`, {
      method: "POST",
      body: JSON.stringify({force: options.force !== false}),
    });
    mergeInterpretedExperiences(payload.item ? [payload.item] : []);
    const updated = payload.item || {};
    const displayState = ragExperienceDisplayState(updated, updated?.formal_relation || updated?.status);
    if (displayState === "discarded") {
      if (!state.showDiscardedRagExperiences) {
        state.showDiscardedRagExperiences = true;
        localStorage.setItem("showDiscardedRagExperiences", "1");
        const checkbox = document.getElementById("show-discarded-rag");
        if (checkbox) checkbox.checked = true;
      }
      state.ragActionNotice = {
        tone: "warning",
        title: "AI重新评估后已自动废弃",
        message: "这条经验没有丢失，已归入“已废弃/自动降噪”状态；系统已保持显示全部，方便你继续核对。",
        experienceId,
      };
    } else if (displayState === "kept") {
      state.ragActionNotice = {
        tone: "ok",
        title: "AI重新评估后已吸纳为经验",
        message: "这条经验已保留在AI经验池，不会作为新经验继续提醒。",
        experienceId,
      };
    }
  } finally {
    state.ragInterpretationLoadingIds.delete(experienceId);
    renderRagExperiences({items: state.ragExperiences});
  }
}

function mergeInterpretedExperiences(items = []) {
  if (!items.length) return;
  const byId = new Map(items.map((item) => [item.experience_id, item]));
  state.ragExperiences = (state.ragExperiences || []).map((item) => {
    const updated = byId.get(item.experience_id);
    return updated ? {...item, ...updated, ai_interpretation: updated.ai_interpretation} : item;
  });
  renderRagExperiences({items: state.ragExperiences});
}

function renderRagExperiences(payload = {}) {
  const items = payload.items || state.ragExperiences || [];
  const hiddenCount = Number(payload.ui_hidden_count ?? state.ragExperienceHiddenCount ?? 0);
  const loadedCount = Math.max(0, Number(payload.ui_loaded_count ?? state.ragExperienceLoadedCount ?? items.length) || 0);
  const displayCounts = normalizeRagExperienceDisplayCounts(
    payload.display_counts || state.ragExperienceDisplayCounts,
    items
  );
  const governanceCounts = normalizeRagExperienceGovernanceCounts(
    payload.governance_counts || state.ragExperienceGovernanceCounts,
    items
  );
  const relationCounts = {};
  const qualityCounts = {};
  let activeVisibleCount = 0;
  let promotedVisibleCount = 0;
  let discardedVisibleCount = 0;
  let retrievableCount = 0;
  for (const item of items) {
    const relationRaw = item?.formal_relation || item?.status || "novel";
    const displayState = ragExperienceDisplayState(item, relationRaw);
    const relation = ragExperienceRelationValue(item, relationRaw, displayState);
    relationCounts[relation] = Number(relationCounts[relation] || 0) + 1;
    const band = String((item?.quality || {}).band || "unknown");
    qualityCounts[band] = Number(qualityCounts[band] || 0) + 1;
    if (displayState === "discarded") {
      discardedVisibleCount += 1;
    } else if (displayState === "promoted") {
      promotedVisibleCount += 1;
      activeVisibleCount += 1;
    } else {
      activeVisibleCount += 1;
    }
    if (experienceRetrievalAllowed(item, item?.quality || {})) retrievableCount += 1;
  }
  const cards = document.getElementById("rag-experience-cards");
  if (cards) {
    const processingNotice = ragExperienceProcessingNoticeHtml();
    const actionNotice = ragActionNoticeHtml();
    const governanceNotice = ragGovernanceSummaryHtml(governanceCounts);
    const loadedNotice = displayCounts.total > loadedCount ? `
        <div class="status-card info rag-load-scope">
          <strong>清单分页加载</strong>
          <span>当前已加载 ${escapeHtml(loadedCount)} / ${escapeHtml(displayCounts.total)} 条；上方统计和治理对账按全量口径计算，没有隐藏到统计之外。</span>
        </div>
      ` : "";
    cards.innerHTML = [
      ["待处理", displayCounts.pending],
      ["已入AI经验池", displayCounts.kept],
      ["已转待确认", displayCounts.promoted],
      ["已废弃", displayCounts.discarded],
      ["其他明确状态", displayCounts.other],
      ["总经验", displayCounts.total],
    ]
      .map(([label, value]) => `<div class="metric-card"><span>${escapeHtml(value)}</span><label>${escapeHtml(label)}</label></div>`)
      .join("") + actionNotice + processingNotice + loadedNotice + governanceNotice + (!displayCounts.consistent ? `
        <div class="status-card warning rag-count-warning">
          <strong>统计口径异常</strong>
          <span>分项合计 ${escapeHtml(displayCounts.accounted_total)}，总经验 ${escapeHtml(displayCounts.total)}。请刷新或重新加载经验清单。</span>
        </div>
      ` : "") + (!governanceCounts.consistent ? `
        <div class="status-card warning rag-governance-count-warning">
          <strong>治理对账异常</strong>
          <span>治理状态合计 ${escapeHtml(governanceCounts.accounted_total)}，总经验 ${escapeHtml(governanceCounts.total)}。请刷新或重新加载经验清单。</span>
        </div>
      ` : "");
  }
  const list = document.getElementById("rag-experience-list");
  if (!list) return;
  const sortedItems = sortRagExperiencesForReview(items);
  const hiddenNotice = hiddenCount > 0 && !state.showDiscardedRagExperiences
    ? `<div class="helper-card"><strong>已按你的筛选隐藏 ${hiddenCount} 条已废弃经验。</strong><span>上方统计仍按全量口径计算；勾选“显示已废弃”可查看全部历史废弃记录。</span></div>`
    : "";
  list.innerHTML = sortedItems.length
    ? `${hiddenNotice}${sortedItems.map((item) => {
        const hit = item.rag_hit || {};
        const source = experienceSourceText(item, hit);
        const usageText = experienceUsageText(item);
        const relationRaw = item.formal_relation || item.status || "novel";
        const match = item.formal_match || {};
        const quality = item.quality || {};
        const qualityBand = quality.band || "unknown";
        const qualityReasons = Array.isArray(quality.reasons) ? quality.reasons : [];
        const displayState = ragExperienceDisplayState(item, relationRaw);
        const relation = ragExperienceRelationValue(item, relationRaw, displayState);
        const isHandled = displayState !== "pending";
        const canAct = (item.status || "active") === "active" && !isHandled;
        const overlapCase = canAct && isFormalOverlapCase(item, relation, match);
        let canPromote = canAct && relation !== "covered_by_formal" && relation !== "conflicts_formal";
        const retrievalAllowed = experienceRetrievalAllowed(item, quality);
        const readableSummary = readableExperienceSummary(item, hit);
        const reviewState = item.review_state || {};
        const isNew = Boolean(reviewState.is_new);
        const experienceId = String(item.experience_id || "");
        const isExpanded = state.ragExperienceExpanded.has(experienceId);
        const interpretation = item.ai_interpretation || {};
        const governance = experienceGovernance(item);
        const aiRecommendedPromotion = interpretation.recommended_action === "promote_to_pending" && interpretation.promotion_allowed !== false;
        canPromote = canAct && aiRecommendedPromotion && relation !== "covered_by_formal" && relation !== "conflicts_formal";
        const promoteDisabledReason = "AI 当前没有建议升级为待确认知识。";
        const compactAction = governance.display_label || interpretation.action_label || actionLabelFromValue(interpretation.recommended_action) || (canPromote ? "建议审核是否升级" : "建议人工查看");
        const compactMeaning = interpretation.meaning || "等待AI重新理解后显示这条经验的大概意思。";
        const compactReason = governance.reason || interpretation.action_reason || compactMeaning;
        const isInterpreting = state.ragInterpretationLoadingIds.has(experienceId);
        const activeAction = state.ragActionLoadingIds.get(experienceId) || "";
        const isActionLoading = Boolean(activeAction);
        const isDiscarded = (item.status || "active") === "discarded";
        const overlapKeepFormalLoading = activeAction === "overlap_keep_formal_discard";
        const overlapReplaceFormalLoading = activeAction === "overlap_replace_formal";
        const overlapMergeLoading = activeAction === "overlap_merge";
        return `
          <div class="record-row rag-experience-row readable-experience-row is-experience-${escapeHtml(displayState)} ${isDiscarded ? "is-discarded" : ""}" data-experience-id="${escapeHtml(experienceId)}" data-review-state="${escapeHtml(displayState)}" data-collapsed="${isExpanded ? "false" : "true"}">
            <div class="rag-experience-main">
              ${isNew ? `
                <span class="experience-new-float" aria-label="新经验">
                  <span class="experience-new-badge">NEW</span>
                </span>
              ` : ""}
              <div class="experience-collapse-head">
                <button type="button" class="experience-collapse-toggle rag-experience-toggle" data-id="${escapeHtml(experienceId)}" aria-expanded="${isExpanded ? "true" : "false"}">
                  <span class="collapse-caret" aria-hidden="true"></span>
                  <strong>AI经验：${escapeHtml(readableSummary)}</strong>
                  <span class="toggle-copy">${isExpanded ? "收起" : "展开"}</span>
                </button>
                <span class="relation-chip relation-${escapeHtml(relation)}">${escapeHtml(relationText(relation))}</span>
              </div>
              <div class="quality-line" title="${escapeHtml(qualityReasons.join("；"))}">
                <span class="quality-chip quality-${escapeHtml(qualityBand)}">${escapeHtml(qualityText(qualityBand))}</span>
                <span class="status-chip ${retrievalAllowed ? "ok" : "warning"}">${escapeHtml(experienceParticipationText(item, quality))}</span>
              </div>
              <span class="experience-meta-line">${escapeHtml(source)} · ${escapeHtml(usageText)} · ${escapeHtml(item.updated_at || item.created_at || "")}</span>
              <div class="experience-compact-summary">
                <span class="experience-action-chip">${escapeHtml(compactAction)}</span>
                <p>${escapeHtml(shortBusinessText(compactReason, 140))}</p>
              </div>
              <div class="experience-collapsible-body">
                <div class="experience-readable-form">
                  ${renderExperienceReadableBody(item)}
                </div>
                ${renderExperienceSourceDetails(item, hit, match)}
              </div>
            </div>
            <div class="inline-actions">
              ${canAct ? `<button class="secondary-button rag-experience-interpret ${isInterpreting ? "is-loading" : ""}" data-id="${escapeHtml(item.experience_id || "")}" ${isInterpreting || isActionLoading ? "disabled" : ""}>${isInterpreting ? `<span class="loading-spinner button-spinner" aria-hidden="true"></span><span>整理中</span>` : "AI重新整理"}</button>` : ""}
              ${overlapCase ? `
                <button class="secondary-button rag-overlap-keep-formal ${overlapKeepFormalLoading ? "is-loading" : ""}" data-id="${escapeHtml(item.experience_id || "")}" ${isActionLoading ? "disabled" : ""}>${overlapKeepFormalLoading ? `<span class="loading-spinner button-spinner" aria-hidden="true"></span><span>处理中</span>` : "以正式知识为准"}</button>
                <button class="secondary-button rag-overlap-replace-formal ${overlapReplaceFormalLoading ? "is-loading" : ""}" data-id="${escapeHtml(item.experience_id || "")}" ${isActionLoading ? "disabled" : ""}>${overlapReplaceFormalLoading ? `<span class="loading-spinner button-spinner" aria-hidden="true"></span><span>处理中</span>` : "以新经验为准"}</button>
                <button class="primary-button rag-overlap-merge ${overlapMergeLoading ? "is-loading" : ""}" data-id="${escapeHtml(item.experience_id || "")}" ${isActionLoading ? "disabled" : ""}>${overlapMergeLoading ? `<span class="loading-spinner button-spinner" aria-hidden="true"></span><span>合并中</span>` : "AI分析并合并"}</button>
              ` : ""}
              ${!overlapCase && canAct ? `<button class="primary-button rag-experience-promote ${activeAction === "promote" ? "is-loading" : ""}" data-id="${escapeHtml(item.experience_id || "")}" ${isActionLoading || !canPromote ? "disabled" : ""} ${!canPromote ? `title="${escapeHtml(promoteDisabledReason)}"` : ""}>${activeAction === "promote" ? `<span class="loading-spinner button-spinner" aria-hidden="true"></span><span>升级中</span>` : "升级为待确认知识"}</button>` : ""}
              ${!overlapCase && canAct ? `<button class="secondary-button rag-experience-keep ${activeAction === "keep" ? "is-loading" : ""}" data-id="${escapeHtml(item.experience_id || "")}" ${isActionLoading ? "disabled" : ""}>${activeAction === "keep" ? `<span class="loading-spinner button-spinner" aria-hidden="true"></span><span>保存中</span>` : "保留到AI经验池"}</button>` : ""}
              ${!overlapCase && canAct ? `<button class="secondary-button rag-experience-discard ${activeAction === "discard" ? "is-loading" : ""}" data-id="${escapeHtml(item.experience_id || "")}" ${isActionLoading ? "disabled" : ""}>${activeAction === "discard" ? `<span class="loading-spinner button-spinner" aria-hidden="true"></span><span>废弃中</span>` : "废弃"}</button>` : ""}
              ${isHandled ? `<button class="secondary-button rag-experience-reopen ${activeAction === "reopen" ? "is-loading" : ""}" data-id="${escapeHtml(item.experience_id || "")}" ${isActionLoading ? "disabled" : ""}>${activeAction === "reopen" ? `<span class="loading-spinner button-spinner" aria-hidden="true"></span><span>恢复中</span>` : "重新待处理"}</button>` : ""}
            </div>
          </div>
        `;
      }).join("")}`
    : `<div class="empty-state">${hiddenCount > 0 && !state.showDiscardedRagExperiences ? `当前有 ${hiddenCount} 条经验已被判定为“已废弃/自动降噪”，默认隐藏。勾选“显示已废弃”即可查看。` : "暂无对话经验。系统只有在客服使用参考资料成功回复后，才会在这里生成概括。"}</div>`;
  list.querySelectorAll(".rag-experience-discard").forEach((button) => {
    button.addEventListener("click", () => discardRagExperience(button.dataset.id).catch((error) => alert(error.message)));
  });
  list.querySelectorAll(".rag-experience-promote").forEach((button) => {
    button.addEventListener("click", () => promoteRagExperience(button.dataset.id).catch((error) => alert(error.message)));
  });
  list.querySelectorAll(".rag-experience-keep").forEach((button) => {
    button.addEventListener("click", () => keepRagExperience(button.dataset.id).catch((error) => alert(error.message)));
  });
  list.querySelectorAll(".rag-experience-reopen").forEach((button) => {
    button.addEventListener("click", () => reopenRagExperience(button.dataset.id).catch((error) => alert(error.message)));
  });
  list.querySelectorAll(".rag-experience-save").forEach((button) => {
    button.addEventListener("click", () => saveRagExperiencePoint(button).catch((error) => alert(error.message)));
  });
  list.querySelectorAll(".rag-experience-interpret").forEach((button) => {
    button.addEventListener("click", () => interpretRagExperience(button.dataset.id, {force: true}).catch((error) => alert(error.message)));
  });
  list.querySelectorAll(".rag-overlap-keep-formal").forEach((button) => {
    button.addEventListener("click", () => resolveFormalOverlapExperience(button.dataset.id, "keep_formal_discard_experience").catch((error) => alert(error.message)));
  });
  list.querySelectorAll(".rag-overlap-replace-formal").forEach((button) => {
    button.addEventListener("click", () => resolveFormalOverlapExperience(button.dataset.id, "replace_formal_with_experience").catch((error) => alert(error.message)));
  });
  list.querySelectorAll(".rag-overlap-merge").forEach((button) => {
    button.addEventListener("click", () => resolveFormalOverlapExperience(button.dataset.id, "ai_merge_formal_and_experience").catch((error) => alert(error.message)));
  });
  list.querySelectorAll(".rag-experience-toggle").forEach((button) => {
    button.addEventListener("click", () => toggleRagExperience(button));
  });
}

function toggleRagExperience(button) {
  const row = button.closest(".rag-experience-row");
  if (!row) return;
  const id = button.dataset.id || row.dataset.experienceId || "";
  const nextExpanded = row.dataset.collapsed !== "false";
  row.dataset.collapsed = nextExpanded ? "false" : "true";
  button.setAttribute("aria-expanded", nextExpanded ? "true" : "false");
  const label = button.querySelector(".toggle-copy");
  if (label) label.textContent = nextExpanded ? "收起" : "展开";
  if (id) {
    if (nextExpanded) {
      state.ragExperienceExpanded.add(id);
    } else {
      state.ragExperienceExpanded.delete(id);
    }
    saveStringSet("ragExperienceExpanded", state.ragExperienceExpanded);
  }
}

function isFormalOverlapCase(item, relationValue = "", match = {}) {
  const relation = String(relationValue || item?.formal_relation || "");
  const formalMatch = match && typeof match === "object" ? match : (item?.formal_match || {});
  const hasAnchor = Boolean(formalMatch?.category_id && formalMatch?.item_id);
  if (!hasAnchor) return false;
  if (["covered_by_formal", "supports_formal", "conflicts_formal"].includes(relation)) return true;
  const similarity = Number(formalMatch?.similarity || 0);
  return Number.isFinite(similarity) && similarity >= 0.48;
}

function overlapStrategyLabel(strategy) {
  return {
    keep_formal_discard_experience: "以正式知识为准（废弃本条经验）",
    replace_formal_with_experience: "以新经验为准（废弃原正式知识）",
    ai_merge_formal_and_experience: "AI分析并合并两者",
  }[strategy] || "冲突处理";
}

async function resolveFormalOverlapExperience(experienceId, strategy) {
  if (!experienceId) return;
  if (state.ragActionLoadingIds.has(experienceId)) return;
  const confirmText = {
    keep_formal_discard_experience: "确认以正式知识为准并废弃这条经验吗？",
    replace_formal_with_experience: "确认废弃原正式知识，并以这条新经验为准吗？",
    ai_merge_formal_and_experience: "确认让 AI 分析并合并两者，并直接更新正式知识库吗？",
  }[strategy] || "确认执行相近知识处理吗？";
  if (!confirm(confirmText)) return;
  const actionKey = {
    keep_formal_discard_experience: "overlap_keep_formal_discard",
    replace_formal_with_experience: "overlap_replace_formal",
    ai_merge_formal_and_experience: "overlap_merge",
  }[strategy] || "overlap_action";
  state.ragActionLoadingIds.set(experienceId, actionKey);
  renderRagExperiences({items: state.ragExperiences});
  try {
    const payload = await apiJson(`/api/rag/experiences/${encodeURIComponent(experienceId)}/resolve-overlap`, {
      method: "POST",
      body: JSON.stringify({strategy}),
    });
    const candidateId = payload?.candidate?.candidate_id || "";
    const headline = overlapStrategyLabel(strategy);
    if (candidateId) {
      alert(`${headline}已完成。\n系统返回候选记录：${candidateId}`);
    } else if (payload?.message) {
      alert(payload.message);
    }
    await Promise.all([
      loadRagExperiences({fast: true}),
      loadRagStatus().catch(() => {}),
      loadCandidates().catch(() => {}),
      loadOverview().catch(() => {}),
      loadKnowledge().catch(() => {}),
    ]);
  } finally {
    state.ragActionLoadingIds.delete(experienceId);
    renderRagExperiences({items: state.ragExperiences});
  }
}

async function promoteRagExperience(experienceId) {
  if (!experienceId) return;
  if (state.ragActionLoadingIds.has(experienceId)) return;
  if (!confirm("确认把这条经验转为“待确认知识”？它仍需要人工审核后才会进入正式知识库。")) return;
  state.ragActionLoadingIds.set(experienceId, "promote");
  renderRagExperiences({items: state.ragExperiences});
  try {
    const payload = await apiJson(`/api/rag/experiences/${encodeURIComponent(experienceId)}/promote`, {
      method: "POST",
      body: JSON.stringify({source: "admin_console"}),
    });
    if (!payload.ok) throw new Error(payload.message || "经验升级失败");
    await Promise.all([
      loadRagExperiences(),
      loadRagStatus().catch(() => {}),
      loadCandidates().catch(() => {}),
      loadOverview().catch(() => {}),
    ]);
  } finally {
    state.ragActionLoadingIds.delete(experienceId);
    renderRagExperiences({items: state.ragExperiences});
  }
}

async function discardRagExperience(experienceId) {
  if (!experienceId) return;
  if (state.ragActionLoadingIds.has(experienceId)) return;
  if (!confirm("确认废弃这条对话经验？废弃后不会再参与参考检索。")) return;
  state.ragActionLoadingIds.set(experienceId, "discard");
  renderRagExperiences({items: state.ragExperiences});
  try {
    await apiJson(`/api/rag/experiences/${encodeURIComponent(experienceId)}/discard`, {
      method: "POST",
      body: JSON.stringify({reason: "discarded in admin"}),
    });
    await Promise.all([loadRagExperiences({fast: true}), loadRagStatus().catch(() => {})]);
  } finally {
    state.ragActionLoadingIds.delete(experienceId);
    renderRagExperiences({items: state.ragExperiences});
  }
}

async function keepRagExperience(experienceId) {
  if (!experienceId) return;
  if (state.ragActionLoadingIds.has(experienceId)) return;
  state.ragActionLoadingIds.set(experienceId, "keep");
  renderRagExperiences({items: state.ragExperiences});
  try {
    await apiJson(`/api/rag/experiences/${encodeURIComponent(experienceId)}/keep`, {
      method: "POST",
      body: JSON.stringify({reason: "kept in experience layer"}),
    });
    await Promise.all([loadRagExperiences({fast: true}), loadRagStatus().catch(() => {})]);
  } finally {
    state.ragActionLoadingIds.delete(experienceId);
    renderRagExperiences({items: state.ragExperiences});
  }
}

async function reopenRagExperience(experienceId) {
  if (!experienceId) return;
  if (state.ragActionLoadingIds.has(experienceId)) return;
  state.ragActionLoadingIds.set(experienceId, "reopen");
  renderRagExperiences({items: state.ragExperiences});
  try {
    await apiJson(`/api/rag/experiences/${encodeURIComponent(experienceId)}/reopen`, {
      method: "POST",
      body: JSON.stringify({reason: "reopened in admin"}),
    });
    await Promise.all([loadRagExperiences({fast: true}), loadRagStatus().catch(() => {}), loadCandidates().catch(() => {})]);
  } finally {
    state.ragActionLoadingIds.delete(experienceId);
    renderRagExperiences({items: state.ragExperiences});
  }
}

async function saveRagExperiencePoint(button) {
  const experienceId = button?.dataset?.id || "";
  if (!experienceId) return;
  const row = button.closest(".rag-experience-row");
  const pointInputs = [...(row?.querySelectorAll(".rag-experience-point-input") || [])];
  const points = pointInputs.map((input) => input.value.trim()).filter(Boolean);
  const textarea = row?.querySelector(".rag-experience-reply");
  const replyText = points.length ? points.join("\n") : textarea?.value?.trim() || "";
  if (!replyText) {
    alert("回答要点不能为空。");
    return;
  }
  await apiJson(`/api/rag/experiences/${encodeURIComponent(experienceId)}`, {
    method: "PATCH",
    body: JSON.stringify({reply_text: replyText}),
  });
  await Promise.all([loadRagExperiences(), loadRagStatus().catch(() => {})]);
}

function renderExperienceReadableBody(item) {
  return renderAiInterpretation(item);
}

function renderAiInterpretation(item) {
  const ai = item.ai_interpretation || {};
  if (!ai.meaning) {
    return `
      <div class="ai-interpretation-card is-pending">
        <span>AI重新理解</span>
        <p>系统正在让大模型重新读这条经验，稍后会显示它大概是什么意思，以及建议你怎么处理。</p>
      </div>
    `;
  }
  const providerText = ai.provider === "local_fallback" ? "本地兜底，未调用大模型" : `大模型整理${ai.model ? ` · ${ai.model}` : ""}`;
  return `
    <div class="ai-interpretation-card ${ai.provider === "local_fallback" ? "is-fallback" : "is-model"}">
      <div class="ai-interpretation-head">
        <span>AI重新理解</span>
        <em>${escapeHtml(providerText)}</em>
      </div>
      <p>${escapeHtml(ai.meaning)}</p>
      <div class="interpretation-recommendation">
        <b>${escapeHtml(ai.action_label || actionLabelFromValue(ai.recommended_action))}</b>
        <small>${escapeHtml(ai.action_reason || "")}</small>
      </div>
      ${renderFormalKnowledgeComparison(ai.formal_knowledge_comparison)}
      ${Array.isArray(ai.what_to_check) && ai.what_to_check.length ? `
        <div class="interpretation-mini-list">
          <strong>你主要核对</strong>
          ${ai.what_to_check.map((item) => `<small>${escapeHtml(item)}</small>`).join("")}
        </div>
      ` : ""}
      ${Array.isArray(ai.risk_notes) && ai.risk_notes.length ? `
        <div class="interpretation-mini-list warning">
          <strong>风险提醒</strong>
          ${ai.risk_notes.map((item) => `<small>${escapeHtml(item)}</small>`).join("")}
        </div>
      ` : ""}
    </div>
  `;
}

function renderFormalKnowledgeComparison(comparison = {}) {
  if (!comparison || typeof comparison !== "object") return "";
  const level = comparison.overlap_level || "none";
  const hasMatch = comparison.matched_title || comparison.matched_item_id || level !== "none";
  if (!hasMatch) return "";
  const levelText = {
    high: "和正式知识高度重合",
    medium: "和正式知识部分相近",
    low: "找到弱相关正式知识",
    none: "未发现明显重合",
  }[level] || "正式知识比对";
  const similarity = comparison.similarity !== null && comparison.similarity !== undefined ? `相似度 ${comparison.similarity}` : "";
  return `
    <div class="formal-comparison-card overlap-${escapeHtml(level)}">
      <div class="formal-comparison-head">
        <b>${escapeHtml(levelText)}</b>
        ${similarity ? `<em>${escapeHtml(similarity)}</em>` : ""}
      </div>
      ${comparison.matched_title ? `<p>相近正式知识：${escapeHtml(comparison.matched_title)}${comparison.matched_category ? `（${escapeHtml(comparison.matched_category)}）` : ""}</p>` : ""}
      ${comparison.conclusion ? `<p>${escapeHtml(comparison.conclusion)}</p>` : ""}
      ${Array.isArray(comparison.same_points) && comparison.same_points.length ? `
        <div class="interpretation-mini-list">
          <strong>重合点</strong>
          ${comparison.same_points.map((item) => `<small>${escapeHtml(item)}</small>`).join("")}
        </div>
      ` : ""}
      ${Array.isArray(comparison.differences) && comparison.differences.length ? `
        <div class="interpretation-mini-list warning">
          <strong>差异点</strong>
          ${comparison.differences.map((item) => `<small>${escapeHtml(item)}</small>`).join("")}
        </div>
      ` : ""}
    </div>
  `;
}

function renderExperienceSourceDetails(item, hit = {}, match = {}) {
  const hasReplyExperience = item.source !== "intake";
  return `
    <details class="experience-editor">
      <summary>${hasReplyExperience ? "查看来源 / 修改原始要点" : "查看来源和原始内容"}</summary>
      <div class="experience-point-editor">
        ${renderExperienceReadableSourceContent(item, hit)}
        ${match.item_id ? `<div><span>正式库相近内容</span><p>${escapeHtml(`已有相近正式知识「${match.title || match.item_id || ""}」，位置 ${match.category_id || ""}/${match.item_id || ""}`)}</p></div>` : ""}
        ${hasReplyExperience ? `
          <div>
            <span>手动修改原始回答要点</span>
            ${renderExperiencePointEditor(item.reply_text || "")}
          </div>
          <div class="inline-actions">
            <button class="secondary-button rag-experience-save" data-id="${escapeHtml(item.experience_id || "")}">保存要点</button>
          </div>
        ` : ""}
      </div>
    </details>
  `;
}

function renderExperienceReadableSourceContent(item, hit = {}) {
  const primaryText = String(item.evidence_excerpt || item.reply_text || item.summary || "");
  const sourceData = normalizeExperienceSourceData(readableSourceData(primaryText, item));
  const hitData = readableSourceData(hit.text || "", {});
  const cards = [];
  const mode = experienceSourceMode(item, sourceData);
  const customerMessage = sourceData.customer_message || sourceData.question || item.question || "";
  const serviceReply = sourceData.service_reply || sourceData.reply || sourceData.answer || "";
  const productName = sourceData.name || sourceData.product_name || "";
  const policyTitle = sourceData.title || sourceData.policy_type || sourceData.handoff_reason || "";

  cards.push(sourceReadableCard("来源渠道", experienceSourceChannelText(item)));
  const originDetail = experienceSourceOriginDetail(item);
  if (originDetail) cards.push(sourceReadableCard("来源说明", originDetail));

  if (mode === "dialogue") {
    if (customerMessage) cards.push(sourceReadableCard("客户怎么问的", customerMessage));
    if (serviceReply) cards.push(sourceReadableCard("AI怎么回的", serviceReply));
  } else if (mode === "product") {
    if (productName || sourceData.sku) {
      cards.push(sourceReadableCard("商品对象", readableSourceFieldSummary(sourceData, ["name", "sku", "category", "product_category"])));
    }
    cards.push(sourceReadableCard("商品核心信息", readableSourceFieldSummary(sourceData, ["price", "unit", "inventory", "specs", "shipping_policy", "warranty_policy"])));
    if (!isEmpty(sourceData.alias_keywords) || !isEmpty(sourceData.keywords)) {
      cards.push(sourceReadableCard("关键词/别名", readableSourceFieldSummary(sourceData, ["alias_keywords", "keywords"])));
    }
    if (serviceReply || sourceData.answer) cards.push(sourceReadableCard("建议回复要点", serviceReply || sourceData.answer));
  } else if (mode === "policy") {
    if (policyTitle || sourceData.source_title) cards.push(sourceReadableCard("规则名称", policyTitle || sourceData.source_title));
    if (!isEmpty(sourceData.keywords)) cards.push(sourceReadableCard("触发条件", displayBusinessValue(sourceData.keywords)));
    if (sourceData.answer || serviceReply) cards.push(sourceReadableCard("规则内容", sourceData.answer || serviceReply));
    const runtimeRule = sourceRuntimeRuleSummary(sourceData);
    if (runtimeRule) cards.push(sourceReadableCard("执行边界", runtimeRule));
  } else {
    if (customerMessage) cards.push(sourceReadableCard("客户怎么问的", customerMessage));
    if (serviceReply) cards.push(sourceReadableCard("AI怎么回的", serviceReply));
    if (productName || sourceData.sku || sourceData.price || sourceData.inventory) {
      cards.push(sourceReadableCard("识别到的商品信息", readableSourceFieldSummary(sourceData, ["name", "sku", "category", "price", "unit", "inventory", "shipping_policy", "warranty_policy"])));
    }
    if (policyTitle || sourceData.keywords || sourceData.requires_handoff) {
      cards.push(sourceReadableCard("识别到的规则线索", readableSourceFieldSummary(sourceData, ["title", "policy_type", "answer", "keywords", "requires_handoff", "handoff_reason"])));
    }
  }
  const tags = readableSourceTags(sourceData);
  if (tags) cards.push(sourceReadableCard("系统识别出的标签", tags));
  const scope = readableSourceScope(sourceData);
  if (scope) cards.push(sourceReadableCard("适用范围", scope));
  if (item.ai_interpretation?.meaning) cards.push(sourceReadableCard("AI重新理解", item.ai_interpretation.meaning));
  if (!cards.length && primaryText) cards.push(sourceReadableCard("整理后的来源内容", readableSourcePlainText(primaryText)));

  const hitText = hit.text && hit.text !== primaryText ? readableSourcePlainText(hit.text) : "";
  if (hitText) cards.push(sourceReadableCard("命中的参考资料", hitText));
  if (!cards.length) cards.push(`<div class="empty-state compact-empty">暂无可展示的来源内容。</div>`);

  const rawText = primaryText || hit.text || "";
  const technicalRaw = rawText
    ? `<details class="raw-source-details source-technical-details"><summary>查看技术原文（排查用）</summary><div><span>系统保存的原始记录</span><p>${escapeHtml(shortBusinessText(rawText, 1200))}</p></div></details>`
    : "";
  return `${cards.join("")}${technicalRaw}`;
}

function normalizeExperienceSourceData(data) {
  const normalized = {...(data || {})};
  for (const key of ["keywords", "intent_tags", "tone_tags", "linked_categories", "linked_item_ids", "alias_keywords"]) {
    if (typeof normalized[key] === "string") normalized[key] = splitTags(normalized[key]);
  }
  for (const key of ["allow_auto_reply", "requires_handoff", "operator_alert", "usable_as_template"]) {
    const parsed = parseBooleanLike(normalized[key]);
    if (parsed !== null) normalized[key] = parsed;
  }
  return normalized;
}

function parseBooleanLike(value) {
  if (value === true || value === false) return value;
  const text = String(value || "").trim().toLowerCase();
  if (!text) return null;
  if (["true", "1", "yes", "y", "是", "需要", "允许"].includes(text)) return true;
  if (["false", "0", "no", "n", "否", "不需要", "不允许"].includes(text)) return false;
  return null;
}

function displayYesNo(value) {
  const parsed = parseBooleanLike(value);
  if (parsed === true) return "是";
  if (parsed === false) return "否";
  return "未标注";
}

function experienceSourceMode(item, sourceData = {}) {
  if (item.source !== "intake") return "dialogue";
  const kind = intakeExperienceKind(item, sourceData || {});
  if (kind === "product") return "product";
  if (kind === "policy" || kind === "handoff_rule") return "policy";
  if (kind === "chat_template") return "dialogue";
  return "material";
}

function experienceSourceChannelText(item) {
  if (item.source !== "intake") return "客服对话回复沉淀（AI经验池治理生成）";
  const origin = item.original_source || {};
  const sourceType = String(item.source_type || origin.type || "");
  const labels = {
    raw_upload: "上传资料学习",
    deepseek_upload_learning: "上传资料学习",
    raw_wechat_group: "微信群聊学习",
    raw_wechat_private: "微信私聊学习",
    raw_wechat_file_transfer: "文件传输助手学习",
    wechat_raw_message: "微信转写学习",
    manual_admin_entry: "后台手动录入学习",
    product_doc: "商品文档学习",
    policy_doc: "规则文档学习",
    chat_log: "话术文档学习",
    manual: "手册文档学习",
    demo_material: "演示资料学习",
  };
  return labels[sourceType] || `资料学习（${sourceType || "未标注来源"}）`;
}

function experienceSourceOriginDetail(item) {
  const original = item.original_source && typeof item.original_source === "object" ? item.original_source : {};
  const parts = [
    original.file_name || "",
    original.title || "",
    original.conversation_id || "",
    original.raw_batch_id || "",
    original.session_id || "",
    original.batch_token || "",
  ].filter((value) => String(value || "").trim());
  if (!parts.length) return "";
  return parts.map((value) => shortBusinessText(String(value), 72)).join(" · ");
}

function sourceRuntimeRuleSummary(data = {}) {
  const lines = [];
  if (!isEmpty(data.policy_type)) lines.push(`规则类别：${optionLabel("policy_type", data.policy_type) || data.policy_type}`);
  if (!isEmpty(data.allow_auto_reply)) lines.push(`允许自动回复：${displayYesNo(data.allow_auto_reply)}`);
  if (!isEmpty(data.requires_handoff)) lines.push(`必须转人工：${displayYesNo(data.requires_handoff)}`);
  if (!isEmpty(data.operator_alert)) lines.push(`提醒人工客服：${displayYesNo(data.operator_alert)}`);
  if (!isEmpty(data.risk_level)) lines.push(`风险等级：${optionLabel("risk_level", data.risk_level) || data.risk_level}`);
  if (!isEmpty(data.handoff_reason)) lines.push(`人工确认原因：${data.handoff_reason}`);
  return lines.join("\n");
}

function sourceReadableCard(label, value) {
  const text = shortMultilineBusinessText(displayBusinessValue(value), 900);
  if (!text) return "";
  return `
    <div class="source-readable-card">
      <span>${escapeHtml(label)}</span>
      <p>${escapeHtml(text)}</p>
    </div>
  `;
}

function shortMultilineBusinessText(value, maxLength = 360) {
  const text = String(value || "")
    .split(/\r?\n+/)
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .join("\n");
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength).trim()}...`;
}

function readableSourceData(text, item = {}) {
  const parsed = parseExperiencePayload(text || "");
  let data = {};
  if (Array.isArray(parsed.value)) data = parsed.value.find((entry) => entry && typeof entry === "object") || {};
  else if (parsed.value && typeof parsed.value === "object" && !parsed.value.raw_text) data = parsed.value;
  data = {...data, ...extractJsonLikeSourceFields(String(text || ""))};
  const dialogue = item.source_dialogue && typeof item.source_dialogue === "object" ? item.source_dialogue : {};
  if (!data.customer_message && dialogue.customer_message) data.customer_message = dialogue.customer_message;
  if (!data.service_reply && dialogue.service_reply) data.service_reply = dialogue.service_reply;
  const transcript = extractTranscriptDialogue(String(text || ""));
  if (!data.customer_message && transcript.customer_message) data.customer_message = transcript.customer_message;
  if (!data.service_reply && transcript.service_reply) data.service_reply = transcript.service_reply;
  return data;
}

function extractJsonLikeSourceFields(text) {
  const fields = {};
  const stringKeys = [
    "customer_message", "service_reply", "question", "reply", "answer", "name", "product_name", "sku", "category",
    "unit", "shipping_policy", "warranty_policy", "title", "source_title", "policy_type", "handoff_reason", "applicability_scope",
    "product_id", "product_category", "risk_level", "specs", "batch_token",
  ];
  for (const key of stringKeys) {
    const match = text.match(new RegExp(`"${key}"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)"`));
    if (match) fields[key] = decodeJsonLikeString(match[1]);
  }
  for (const key of ["price", "inventory"]) {
    const match = text.match(new RegExp(`"${key}"\\s*:\\s*(-?\\d+(?:\\.\\d+)?)`));
    if (match) fields[key] = match[1];
  }
  for (const key of ["usable_as_template", "requires_handoff", "allow_auto_reply", "operator_alert"]) {
    const match = text.match(new RegExp(`"${key}"\\s*:\\s*(true|false)`));
    if (match) fields[key] = match[1] === "true";
  }
  for (const key of ["intent_tags", "tone_tags", "linked_categories", "linked_item_ids", "keywords", "alias_keywords"]) {
    const match = text.match(new RegExp(`"${key}"\\s*:\\s*\\[([^\\]]*)\\]`));
    if (!match) continue;
    const values = [];
    for (const item of match[1].matchAll(/"((?:\\.|[^"\\])*)"/g)) {
      values.push(decodeJsonLikeString(item[1]));
    }
    if (values.length) fields[key] = values;
  }
  return fields;
}

function decodeJsonLikeString(value) {
  const text = String(value || "");
  try {
    return JSON.parse(`"${text.replace(/\r?\n/g, "\\n")}"`);
  } catch {
    return text.replace(/\\n/g, "\n").replace(/\\"/g, '"').replace(/\\\\/g, "\\");
  }
}

function extractTranscriptDialogue(text) {
  const customer = [];
  const replies = [];
  for (const rawLine of String(text || "").split(/\r?\n/)) {
    const withoutTime = rawLine.replace(/^\[[^\]]+\]\s*/, "").trim();
    const match = withoutTime.match(/^([^:：]{1,30})[:：]\s*(.+)$/);
    if (!match) continue;
    const sender = match[1].trim();
    const content = match[2].trim();
    if (!content || sender === "system") continue;
    if (sender === "self" || /^\[[^\]]*(?:AI|客服)\]\s*/i.test(content)) replies.push(content.replace(/^\[[^\]]*(?:AI|客服)\]\s*/i, ""));
    else customer.push(content);
  }
  return {
    customer_message: customer.join("\n"),
    service_reply: replies.join("\n"),
  };
}

function readableSourceFieldSummary(data, keys) {
  return keys
    .map((key) => {
      const value = data?.[key];
      if (isEmpty(value)) return "";
      return `${fieldLabel({id: key, label: key})}：${displayBusinessValue(value)}`;
    })
    .filter(Boolean)
    .join("\n");
}

function readableSourceTags(data) {
  const parts = [];
  if (!isEmpty(data.intent_tags)) parts.push(`客户意图：${displayBusinessValue(data.intent_tags)}`);
  if (!isEmpty(data.tone_tags)) parts.push(`表达特点：${displayBusinessValue(data.tone_tags)}`);
  if (!isEmpty(data.linked_categories)) parts.push(`关联栏目：${displayBusinessValue(data.linked_categories)}`);
  if (!isEmpty(data.linked_item_ids)) parts.push(`关联知识：${displayBusinessValue(data.linked_item_ids)}`);
  if (!isEmpty(data.keywords)) parts.push(`触发词：${displayBusinessValue(data.keywords)}`);
  return parts.join("\n");
}

function readableSourceScope(data) {
  const scope = data.applicability_scope ? optionLabel("applicability_scope", data.applicability_scope) : "";
  const product = [data.product_id, data.product_category].filter((value) => String(value || "").trim()).join(" / ");
  return [scope, product ? `关联商品：${product}` : ""].filter(Boolean).join("\n");
}

function readableSourcePlainText(text) {
  const data = readableSourceData(text, {});
  const parts = [];
  if (data.customer_message) parts.push(`客户怎么问：${displayBusinessValue(data.customer_message)}`);
  if (data.service_reply || data.answer) parts.push(`AI怎么回：${displayBusinessValue(data.service_reply || data.answer)}`);
  const tags = readableSourceTags(data);
  if (tags) parts.push(tags);
  if (parts.length) return parts.join("\n");
  return readableExperiencePointText(text, 900);
}

function parseExperiencePayload(value) {
  const text = String(value || "").trim();
  if (!text) return {value: {}, text: ""};
  if (/^[\[{]/.test(text)) {
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === "object") return {value: parsed, text};
    } catch {
      // Fall through to loose field parsing.
    }
  }
  const fields = {};
  const segments = text.split(/\r?\n+|；|;/g).map((item) => item.trim()).filter(Boolean);
  for (const segment of segments) {
    const match = segment.match(/^([^:：]{1,36})[:：]\s*(.+)$/);
    if (!match) continue;
    const key = normalizeExperienceFieldKey(match[1]);
    if (!key) continue;
    fields[key] = match[2].trim();
  }
  return {value: Object.keys(fields).length ? fields : {raw_text: text}, text};
}

function normalizeExperienceFieldKey(key) {
  const text = String(key || "").trim();
  const map = {
    资料来源: "source_title",
    商品资料: "source_title",
    政策规则: "source_title",
    测试批次: "batch_token",
    商品: "name",
    商品名称: "name",
    车辆: "name",
    车源: "name",
    编号: "sku",
    型号: "sku",
    类目: "category",
    商品类目: "category",
    价格: "price",
    报价: "price",
    单位: "unit",
    库存: "inventory",
    关键词: "keywords",
    标签: "intent_tags",
    发货说明: "shipping_policy",
    发货: "shipping_policy",
    物流: "shipping_policy",
    "物流/过户": "shipping_policy",
    看车说明: "shipping_policy",
    售后说明: "warranty_policy",
    售后: "warranty_policy",
    售后风险: "warranty_policy",
    "售后/风险": "warranty_policy",
    车况说明: "warranty_policy",
    规格: "specs",
    规格参数: "specs",
    别名关键词: "alias_keywords",
    推荐话术: "service_reply",
    标准说明: "answer",
    规则: "title",
    客户: "customer_message",
    客服: "service_reply",
    客户问题: "customer_message",
    客户问法: "customer_message",
    客服回复: "service_reply",
    建议回复: "service_reply",
    标准回复: "service_reply",
    标题: "title",
    规则名称: "title",
    规则类型: "policy_type",
    回复内容: "answer",
    答案: "answer",
    规则内容: "answer",
    触发词: "keywords",
    触发关键词: "keywords",
    允许自动回复: "allow_auto_reply",
    必须转人工: "requires_handoff",
    提醒人工客服: "operator_alert",
    风险等级: "risk_level",
  };
  if (map[text]) return map[text];
  return text.replace(/\s+/g, "_");
}

function intakeExperienceKind(item, value) {
  const data = Array.isArray(value) ? value[0] || {} : value || {};
  const text = JSON.stringify(value || {}, null, 0) + " " + String(item.summary || "");
  if (Number(item.candidate_count || 0) === 0 && !hasBusinessFields(data)) return "noise";
  if (truthyDataValue(data.requires_handoff) || data.handoff_reason || /转人工|人工确认|贷款包过|金融|首付|月供|电池检测/.test(text)) return "handoff_rule";
  if (data.name || data.sku || data.price || data.inventory || data.category) return "product";
  if (data.customer_message || data.service_reply) return "chat_template";
  if (data.title || data.policy_type || data.answer || data.keywords) return "policy";
  return "lead";
}

function hasBusinessFields(data) {
  return ["name", "sku", "price", "inventory", "customer_message", "service_reply", "title", "answer", "handoff_reason"].some((key) => !isEmpty(data?.[key]));
}

function truthyDataValue(value) {
  return value === true || value === "true" || value === "是" || value === "需要" || value === 1 || value === "1";
}

function sourceLabel(source) {
  return [source.source_type || "资料源", source.source_id || ""].filter(Boolean).join(" · ");
}

function experienceSourceText(item, hit = {}) {
  if (item.source === "intake") {
    const origin = item.original_source || {};
    const sourceType = item.source_type || origin.type || "intake";
    const sourceLabels = {
      raw_upload: "导入资料",
      deepseek_upload_learning: "导入资料",
      raw_wechat_group: "微信群聊",
      raw_wechat_private: "微信私聊",
      raw_wechat_file_transfer: "文件传输助手",
      wechat_raw_message: "微信转写",
      manual_admin_entry: "后台手动录入",
      product_doc: "商品文档",
      policy_doc: "规则文档",
      chat_log: "对话文档",
      manual: "手册文档",
    };
    const detail = origin.file_name || origin.conversation_id || origin.raw_batch_id || shortPath(item.source_path || origin.path || "");
    return [sourceLabels[sourceType] || sourceType, detail, `${item.candidate_count ?? 0} 条AI线索`].filter(Boolean).join(" · ");
  }
  return [hit.category || hit.source_type || "知识片段", hit.product_id || "未指定商品"].filter(Boolean).join(" · ");
}

function experienceUsageText(item) {
  if (item.source === "intake") return `关联 ${item.candidate_count ?? 0} 条AI线索`;
  return `使用 ${(item.usage || {}).reply_count ?? 1} 次`;
}

function readableExperienceSummary(item, hit = {}) {
  if (item.ai_interpretation?.meaning) return shortBusinessText(item.ai_interpretation.meaning, 150);
  if (item.source === "intake") {
    const source = experienceSourceText(item, hit).split(" · ")[0] || "资料";
    const parsed = parseExperiencePayload(item.reply_text || item.evidence_excerpt || "");
    const kind = intakeExperienceKind(item, parsed.value);
    const data = Array.isArray(parsed.value) ? parsed.value[0] || {} : parsed.value || {};
    if (kind === "product") return `从${source}识别到商品资料：${shortBusinessText(displayBusinessValue(data.name || data.sku || "未命名商品"), 80)}`;
    if (kind === "handoff_rule") return `从${source}识别到转人工规则：${shortBusinessText(displayBusinessValue(data.title || data.handoff_reason || "需人工确认"), 80)}`;
    if (kind === "chat_template") return `从${source}识别到客服话术`;
    if (kind === "policy") return `从${source}识别到政策规则：${shortBusinessText(displayBusinessValue(data.title || data.policy_type || "待命名规则"), 80)}`;
    if (kind === "noise") return `从${source}识别到疑似无效内容`;
    const count = Number(item.candidate_count || 0);
    if (count > 0) return `从${source}整理出 ${count} 条可审核内容`;
    return `从${source}保留到AI经验池`;
  }
  const raw = String(item.summary || "").trim();
  if (!raw) return "未生成概括";
  return shortBusinessText(raw.replace(/^Intake\s*->\s*(AI experience pool item|RAG experience):\s*/i, ""), 160);
}

function readableQualityReason(value) {
  const text = String(value || "").trim();
  const translations = {
    "intake material is stored as AI experience pool item first": "这条内容只是先放进AI经验池，尚未允许参与回答",
    "intake material is stored as RAG experience first": "这条内容只是先放进AI经验池，尚未允许参与回答",
    "formal knowledge still requires pending-candidate review": "要变成正式知识，需要先点“升级为待确认知识”，再人工审核入库",
    "intake experiences are not used for autonomous reply retrieval before review": "未确认前不会作为回答依据，也不会自动回答客户",
    "尚未人工确认保留在经验层": "还没有点击“保留到AI经验池”，不会作为回答依据",
    "暂不参与 AI经验池检索": "当前不作为回答依据",
    "允许参与 AI经验池检索": "已保留在AI经验池，但仍不直接作为回答依据",
  };
  return translations[text] || text;
}

function experienceUsageExplanation(item, quality = {}) {
  if (item.source === "intake") {
    const parsed = parseExperiencePayload(item.reply_text || item.evidence_excerpt || "");
    const kind = intakeExperienceKind(item, parsed.value);
    if (kind === "noise") return "这条内容不会参与客户回答。它看起来不像业务知识，确认无用后可以直接废弃。";
    return "这是从资料或聊天记录整理出的审核线索，不会直接参与客户回答。觉得有价值时，点“升级为待确认知识”，再用表单核对后进入正式知识库。";
  }
  const displayState = ragExperienceDisplayState(item, item?.formal_relation || item?.status);
  if (displayState === "discarded") {
    return "这条经验已被废弃（含系统自动降噪废弃），不会进入AI经验池治理或自动回答。";
  }
  if (displayState === "promoted") {
    return "这条经验已升级为待确认知识，进入后续人工审核流程。";
  }
  if (experienceReviewStatus(item) === "auto_kept") {
    if (experienceRetrievalAllowed(item, quality)) {
      return "系统判断这条经验低风险、可复用，已自动吸纳为AI经验池。它只用于治理、候选分发和风格学习；如果要成为回答依据，需要升级为待确认知识并人工审核。";
    }
    return "系统已自动吸纳这条经验，但当前证据或质量还不够稳定，所以仅保留为AI经验池线索，不作为回答依据。";
  }
  if (experienceReviewStatus(item) !== "kept") {
    return "这条经验还没有人工确认，系统不会拿它自动回答客户。确认无误后，可保留到AI经验池；若要作为回答依据，仍需升级并审核进正式知识库。";
  }
  if (experienceRetrievalAllowed(item, quality)) {
    return "你已确认保留到AI经验池。它不会直接作为回答依据；如果要变成正式结构化知识，还需要点“升级为待确认知识”并人工审核入库。";
  }
  return "你已确认保留到AI经验池，但系统判断证据或质量还不够稳定，所以不会作为回答依据或自动回答。";
}

function readableExperiencePointText(value, maxLength = 420) {
  let text = String(value || "");
  text = text.replace(/；?\s*raw_text\s*[:：].*$/i, "");
  const replacements = [
    [/\bname\s*[:：]/gi, "商品名称："],
    [/\bsku\s*[:：]/gi, "型号/SKU："],
    [/\bcategory\s*[:：]/gi, "商品类目："],
    [/\bprice\s*[:：]/gi, "价格："],
    [/\bunit\s*[:：]/gi, "单位："],
    [/\binventory\s*[:：]/gi, "库存："],
    [/\bshipping_policy\s*[:：]/gi, "发货说明："],
    [/\bwarranty_policy\s*[:：]/gi, "售后说明："],
    [/\bcustomer_message\s*[:：]/gi, "客户问题："],
    [/\bservice_reply\s*[:：]/gi, "客服回复："],
    [/\banswer\s*[:：]/gi, "回复内容："],
    [/\bkeywords\s*[:：]/gi, "触发词："],
    [/\btitle\s*[:：]/gi, "标题："],
  ];
  for (const [pattern, replacement] of replacements) {
    text = text.replace(pattern, replacement);
  }
  return shortBusinessText(text, maxLength);
}

function experiencePointItems(value) {
  const readable = readableExperiencePointText(value, 1200);
  if (!readable) return [];
  let points = readable
    .split(/\r?\n+|[；;]\s*|(?:。|！|!|？|\?)\s*/g)
    .map((line) => line.replace(/^[\s\-*•·、，,.。；;]*(?:\d+|[一二三四五六七八九十]+)?[\s、.)）:-]*/g, "").trim())
    .filter(Boolean);
  if (points.length <= 1 && readable.length > 120) {
    points = readable
      .split(/，|,|、/g)
      .map((line) => line.trim())
      .filter((line) => line.length >= 8);
  }
  if (!points.length) points = [readable];
  const deduped = [];
  for (const point of points) {
    if (deduped.some((existing) => existing === point)) continue;
    deduped.push(shortBusinessText(point, 180));
    if (deduped.length >= 8) break;
  }
  return deduped;
}

function renderExperiencePointList(value) {
  const points = experiencePointItems(value);
  if (!points.length) return `<div class="empty-state compact-empty">暂无明确要点</div>`;
  return `
    <div class="experience-point-list">
      ${points.map((point, index) => `
        <div class="experience-point-item">
          <b>要点 ${index + 1}</b>
          <p>${escapeHtml(point)}</p>
        </div>
      `).join("")}
    </div>
  `;
}

function renderExperiencePointEditor(value) {
  const points = experiencePointItems(value);
  const editablePoints = [...points, "", ""].slice(0, Math.max(points.length + 1, 3));
  return `
    <div class="experience-point-editor-list">
      ${editablePoints.map((point, index) => `
        <label class="form-field experience-point-field">
          <span>要点 ${index + 1}</span>
          <textarea class="rag-experience-point-input" rows="2" placeholder="例如：客户问到付款方式时，先说明支持对公转账。">${escapeHtml(point)}</textarea>
        </label>
      `).join("")}
    </div>
  `;
}

function shortBusinessText(value, maxLength = 360) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength).trim()}...`;
}

function shortPath(value) {
  const text = String(value || "");
  if (!text) return "";
  const parts = text.split(/[\\/]+/);
  return parts.slice(-3).join("/");
}

function qualityText(value) {
  return {
    high: "很可靠",
    medium: "可参考",
    low: "需要人工看看",
    blocked: "已停用",
    unknown: "未评估",
  }[value] || value || "未评估";
}

function experienceReviewStatus(item) {
  return String((item?.experience_review || {}).status || "");
}

function experienceGovernance(item) {
  return item?.governance && typeof item.governance === "object" ? item.governance : {};
}

function governanceEffectiveState(item) {
  return String(experienceGovernance(item).effective_state || "");
}

function governanceDisplayState(item, fallback = "pending") {
  const effective = governanceEffectiveState(item);
  if (["auto_discarded", "user_discarded", "blocked"].includes(effective)) return "discarded";
  if (["promoted", "candidate_created"].includes(effective)) return "promoted";
  if (["retrievable_experience", "kept_experience", "style_only", "candidate_suggested"].includes(effective)) return "kept";
  if (effective === "pending_review") return "pending";
  return fallback;
}

function isAutoKeptReviewStatus(reviewStatus) {
  return reviewStatus === "auto_kept";
}

function autoTriagedAsDiscard(item) {
  if (experienceReviewStatus(item) !== "auto_triaged") return false;
  const action = String((item?.experience_review || {}).auto_triage_action || "");
  return ["discard", "already_covered"].includes(action);
}

function ragExperienceDisplayState(item, relationValue = "") {
  const governance = experienceGovernance(item);
  if (governance.effective_state) return governanceDisplayState(item, "pending");
  const status = String(item?.status || "active");
  const relation = String(relationValue || item?.formal_relation || status || "");
  const reviewStatus = experienceReviewStatus(item);
  if (status === "discarded" || autoTriagedAsDiscard(item)) return "discarded";
  if (status === "promoted" || relation === "promoted") return "promoted";
  if (reviewStatus === "kept" || isAutoKeptReviewStatus(reviewStatus) || relation === "kept_experience" || relation === "auto_kept_experience") return "kept";
  return "pending";
}

function ragExperienceRelationValue(item, relationValue = "", displayState = "") {
  const effective = governanceEffectiveState(item);
  if (effective === "style_only") return "style_only";
  if (effective === "candidate_suggested" || effective === "candidate_created") return "promotion_candidate";
  if (effective === "retrievable_experience" || effective === "kept_experience") return "kept_experience";
  if (["auto_discarded", "user_discarded", "blocked"].includes(effective)) return "discarded";
  if (effective === "promoted") return "promoted";
  const relation = String(relationValue || item?.formal_relation || item?.status || "novel");
  const stateValue = displayState || ragExperienceDisplayState(item, relation);
  if (stateValue === "discarded") return "discarded";
  if (stateValue === "promoted") return "promoted";
  if (stateValue === "kept") {
    return isAutoKeptReviewStatus(experienceReviewStatus(item)) ? "auto_kept_experience" : "kept_experience";
  }
  if (relation === "discarded") return "novel";
  return relation;
}

function ragExperienceIsHandled(item, relationValue = "") {
  return ragExperienceDisplayState(item, relationValue) !== "pending";
}

function ragExperienceTimestamp(item) {
  const value = Date.parse(item?.updated_at || item?.created_at || "");
  return Number.isFinite(value) ? value : 0;
}

function ragExperienceIsUnread(item) {
  const isNew = Boolean(item?.review_state?.is_new);
  if (!isNew) return false;
  return ragExperienceDisplayState(item, item?.formal_relation || item?.status) === "pending";
}

function shouldHideRagExperience(item) {
  if (state.showDiscardedRagExperiences) return false;
  return ragExperienceDisplayState(item, item?.formal_relation || item?.status) === "discarded";
}

function sortRagExperiencesForReview(items = []) {
  const stateRank = {pending: 0, kept: 1, promoted: 2, discarded: 3};
  return [...items].sort((left, right) => {
    const unreadDiff = (ragExperienceIsUnread(left) ? 0 : 1) - (ragExperienceIsUnread(right) ? 0 : 1);
    if (unreadDiff) return unreadDiff;
    const leftState = ragExperienceDisplayState(left, left?.formal_relation || left?.status);
    const rightState = ragExperienceDisplayState(right, right?.formal_relation || right?.status);
    const rankDiff = (stateRank[leftState] ?? 9) - (stateRank[rightState] ?? 9);
    if (rankDiff) return rankDiff;
    return ragExperienceTimestamp(right) - ragExperienceTimestamp(left);
  });
}

function experienceRetrievalAllowed(item, quality = {}) {
  const governance = experienceGovernance(item);
  if (governance.effective_state) {
    return false;
  }
  return false;
}

function relationText(value) {
  return {
    novel: "新经验",
    covered_by_formal: "正式库已有",
    supports_formal: "可补充正式库",
    conflicts_formal: "疑似冲突",
    auto_kept_experience: "已入AI经验池（自动）",
    kept_experience: "已入AI经验池",
    style_only: "仅话术参考",
    promotion_candidate: "建议转待确认",
    promoted: "已转待确认",
    discarded: "已废弃",
  }[value] || value || "未判断";
}

function actionText(value) {
  return {
    keep_as_rag_experience: "保留到AI经验池，用于治理、候选分发或风格学习。",
    keep_low_priority_or_discard: "正式知识已经覆盖，可降低优先级或废弃。",
    keep_as_supporting_expression: "可保留为正式知识的表达补充。",
    manual_review_conflict: "疑似和正式知识冲突，建议人工检查后处理。",
    promote_to_review_candidate: "建议升级为待确认知识，由人工审核后再入库。",
    system_auto_kept_as_experience: "系统已自动吸纳到AI经验池，但不直接作为回答依据。",
    kept_as_experience: "已由人工确认吸纳到AI经验池，不再作为新经验提醒。",
    already_promoted: "已升级为待确认知识。",
    already_discarded: "已废弃。",
  }[value] || value || "保持观察。";
}

function actionLabelFromValue(value) {
  return {
    promote_to_pending: "建议升级为待确认知识",
    keep_as_experience: "建议保留到AI经验池",
    discard: "建议废弃",
    manual_review: "建议人工检查",
    already_covered: "正式知识库可能已覆盖",
    needs_more_info: "需要补充信息后再判断",
  }[value] || "建议人工检查";
}

function experienceStatusText(value) {
  return {promoted: "已升级", discarded: "已废弃", active: "默认采纳"}[value] || value || "默认采纳";
}

function experienceParticipationText(item, quality = {}) {
  const governance = experienceGovernance(item);
  if (governance.display_label) return String(governance.display_label || "");
  if (item.source === "intake") return "审核线索，不直接回答";
  const displayState = ragExperienceDisplayState(item, item?.formal_relation || item?.status);
  if (displayState === "discarded") return "已废弃，不参与回答";
  if (displayState === "promoted") return "已升级为待确认知识，等待人工审核";
  if (isAutoKeptReviewStatus(experienceReviewStatus(item))) {
    return experienceRetrievalAllowed(item, quality) ? "系统已自动吸纳到AI经验池" : "系统已自动吸纳到AI经验池，暂不作为回答依据";
  }
  const kept = experienceReviewStatus(item) === "kept";
  if (!kept) return "未确认，不参与回答";
  return experienceRetrievalAllowed(item, quality) ? "已确认保留到AI经验池" : "已确认，但不作为回答依据";
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function uploadKindText(item) {
  const kind = String(item?.kind || "").trim();
  const requestedKind = String(item?.requested_kind || "").trim();
  const labelMap = {
    products: "商品资料",
    chats: "聊天记录",
    policies: "政策规则",
    erp_exports: "ERP导出",
  };
  const resolved = labelMap[kind] || kind || "未识别";
  if (requestedKind !== "auto") return resolved;
  const reason = String(item?.kind_detect_reason || "").trim();
  return reason ? `自动识别为${resolved}（${reason}）` : `自动识别为${resolved}`;
}

async function runLearning() {
  if (state.learningInProgress) return;
  const uploads = await apiGet("/api/uploads");
  const uploadIds = (uploads.items || []).filter((item) => !item.learned).map((item) => item.upload_id);
  if (!uploadIds.length) {
    document.getElementById("candidate-detail").innerHTML = `<div class="empty-state">没有待整理的上传资料。</div>`;
    selectView("rag_experiences");
    await loadRagExperiences();
    return;
  }
  setLearningBusy(true, uploadIds.length);
  try {
    const payload = await apiJson("/api/learning/jobs", {method: "POST", body: JSON.stringify({upload_ids: uploadIds, use_llm: true})});
    const skipped = Number(payload.job?.skipped_duplicate_count || 0);
    const skippedText = skipped ? `；已自动跳过 ${skipped} 条重复内容` : "";
    const ragCount = payload.job?.rag_experience_count ?? 0;
    renderCandidatePlaceholder("ok", "分析完成", `已整理出 ${ragCount} 条AI经验池${payload.job.candidate_count ? `，包含 ${payload.job.candidate_count} 条AI线索` : ""}${skippedText}。请到AI经验池查看AI建议，再决定是否升级为待确认知识。`);
    selectView("rag_experiences");
    await loadRagExperiences();
  } catch (error) {
    renderCandidatePlaceholder("error", "分析失败", error.message || "请查看后台服务状态后重试。");
    throw error;
  } finally {
    setLearningBusy(false);
  }
}

async function loadCandidates() {
  if (!state.productCatalog) {
    await loadProductCatalog({loadDetail: false}).catch(() => {});
  }
  const payload = await apiGet("/api/candidates?status=pending");
  state.candidates = payload;
  const list = document.getElementById("candidate-list");
  const items = payload.items || [];
  const selectedId = state.selectedCandidate?.candidate_id || "";
  const selectedItem = items.find((item) => item.candidate_id === selectedId) || items[0] || null;
  state.selectedCandidate = selectedItem;
  updateCandidateCountBadge(items.length);
  list.innerHTML = (payload.items || [])
    .map((item, index) => {
      const candidateId = String(item.candidate_id || "");
      const activeAction = state.candidateActionLoadingIds.get(candidateId) || "";
      const isActionLoading = Boolean(activeAction);
      return `
        <div class="record-row candidate-row${state.selectedCandidate?.candidate_id === item.candidate_id ? " is-selected" : ""}" data-index="${index}">
          <button class="link-button candidate-select" data-index="${index}" ${isActionLoading ? "disabled" : ""}>
            <strong>${escapeHtml(candidateTitle(item))}</strong>
            <span>${escapeHtml(item.proposal?.summary || "")}${candidateIsIncomplete(item) ? " · 待补充" : ""}</span>
            <span class="source-line">来源：${escapeHtml(candidateSourceText(item))}</span>
            ${badgeListHtml(item.display_badges || [])}
          </button>
          <div class="inline-actions">
            <button class="secondary-button candidate-reject ${activeAction === "reject" ? "is-loading" : ""}" data-id="${escapeHtml(candidateId)}" ${isActionLoading ? "disabled" : ""}>${activeAction === "reject" ? `<span class="loading-spinner button-spinner" aria-hidden="true"></span><span>拒绝中</span>` : "拒绝"}</button>
            <button class="primary-button candidate-apply ${activeAction === "apply" ? "is-loading" : ""}" data-id="${escapeHtml(candidateId)}" ${item.can_promote === false || candidateIsIncomplete(item) || isActionLoading ? "disabled" : ""}>${activeAction === "apply" ? `<span class="loading-spinner button-spinner" aria-hidden="true"></span><span>入库中</span>` : "应用"}</button>
          </div>
        </div>
      `;
    })
    .join("") || `<div class="empty-state">暂无待审核候选</div>`;
  list.querySelectorAll(".candidate-select").forEach((button) => {
    button.addEventListener("click", () => {
      const item = payload.items[Number(button.dataset.index)];
      state.selectedCandidate = item;
      renderCandidateListSelection();
      renderCandidateDetail(item);
    });
  });
  list.querySelectorAll(".candidate-apply").forEach((button) => {
    button.addEventListener("click", () => applyCandidate(button.dataset.id).catch((error) => alert(error.message)));
  });
  list.querySelectorAll(".candidate-reject").forEach((button) => {
    button.addEventListener("click", () => rejectCandidate(button.dataset.id).catch((error) => alert(error.message)));
  });
  if (state.selectedCandidate) renderCandidateDetail(state.selectedCandidate);
  else clearCandidateDetail("暂无待审核候选");
}

function renderCandidateListSelection() {
  document.querySelectorAll("#candidate-list .candidate-row").forEach((row) => {
    const index = Number(row.dataset.index);
    const item = state.candidates?.items?.[index];
    row.classList.toggle("is-selected", Boolean(item && item.candidate_id === state.selectedCandidate?.candidate_id));
  });
}

function setLearningBusy(isBusy, uploadCount = 0) {
  state.learningInProgress = isBusy;
  const buttons = [document.getElementById("run-learning"), document.getElementById("run-learning-from-candidates")].filter(Boolean);
  for (const button of buttons) {
    button.disabled = isBusy;
    button.textContent = isBusy ? "整理中..." : "整理未处理资料";
  }
  if (isBusy) {
    renderCandidatePlaceholder(
      "loading",
      "正在整理上传资料",
      `正在分析 ${uploadCount} 个文件，整理出的内容会先进入AI经验池。`
    );
  }
}

function renderCandidatePlaceholder(type, title, message) {
  const spinner = type === "loading" ? `<span class="loading-spinner" aria-hidden="true"></span>` : "";
  document.getElementById("candidate-detail").innerHTML = `
    <div class="status-card ${escapeHtml(type)}">
      <strong>${spinner}${escapeHtml(title)}</strong>
      <span>${escapeHtml(message)}</span>
    </div>
  `;
}

function clearCandidateDetail(message = "请选择左侧候选查看详情。") {
  state.selectedCandidate = null;
  document.getElementById("candidate-detail").innerHTML = `<div class="empty-state">${escapeHtml(message)}</div>`;
}

function candidateTitle(item) {
  const patch = item.proposal?.formal_patch || {};
  const categoryId = patch.target_category || item.proposal?.target_category || "";
  const category = categoryById(categoryId);
  const data = patch.item?.data || item.proposal?.suggested_fields || {};
  const titleField = category?.schema?.item_title_field || "title";
  const title = data[titleField] || data.name || data.title || data.customer_message || item.proposal?.summary || item.candidate_id;
  return `${category?.name || categoryId || "知识"}：${title}`;
}

function candidateSourceText(item) {
  const summary = item?.source_summary || {};
  const parts = [summary.module, summary.channel, summary.detail].filter((part) => String(part || "").trim());
  if (parts.length) return parts.join(" · ");
  const sourceType = item?.source?.type || "";
  return sourceType ? sourceType : "未标注";
}

function updateCandidateCountBadge(count) {
  const value = Number(count || 0);
  for (const badge of [document.getElementById("candidate-tab-badge"), document.getElementById("candidate-nav-badge")]) {
    if (!badge) continue;
    badge.textContent = value > 99 ? "99+" : String(value);
    badge.classList.toggle("is-hidden", value <= 0);
  }
}

function updateRagExperienceCountBadge(count) {
  const value = Number(count || 0);
  const badges = [
    ...document.querySelectorAll("[data-rag-experience-count-badge]"),
    document.getElementById("rag-experience-nav-badge"),
  ];
  for (const badge of badges) {
    if (!badge) continue;
    badge.textContent = value > 99 ? "99+" : String(value);
    badge.classList.toggle("is-hidden", value <= 0);
  }
}

function unreviewedRagExperienceCount(items = []) {
  return items.filter((item) => ragExperienceIsUnread(item)).length;
}

function candidateIsIncomplete(item) {
  return item?.intake?.status === "needs_more_info" || item?.review?.completeness_status === "needs_more_info";
}

function renderCandidateDetail(item) {
  const patch = item.proposal?.formal_patch || {};
  const intake = item.intake || {};
  const categoryId = patch?.target_category || item.proposal?.target_category || "";
  const category = categoryById(categoryId);
  const readable = candidateReadableSummary(item, category);
  const candidateId = String(item.candidate_id || "");
  const activeAction = state.candidateActionLoadingIds.get(candidateId) || "";
  const isActionLoading = Boolean(activeAction);
  const detail = document.getElementById("candidate-detail");
  detail.innerHTML = `
    <div class="approval-card ${candidateIsIncomplete(item) ? "warning" : ""}">
      <div>
        <p class="eyebrow">AI建议加入这条知识</p>
        <h2>${escapeHtml(readable.title)}</h2>
        <p>${escapeHtml(readable.summary)}</p>
        ${badgeListHtml(item.display_badges || [])}
      </div>
      <div class="approval-actions">
        <button class="primary-button candidate-apply-detail ${activeAction === "apply" ? "is-loading" : ""}" data-id="${escapeHtml(candidateId)}" ${item.can_promote === false || candidateIsIncomplete(item) || isActionLoading ? "disabled" : ""}>${activeAction === "apply" ? `<span class="loading-spinner button-spinner" aria-hidden="true"></span><span>入库中</span>` : "确认加入知识库"}</button>
        <button class="secondary-button candidate-reject-detail ${activeAction === "reject" ? "is-loading" : ""}" data-id="${escapeHtml(candidateId)}" ${isActionLoading ? "disabled" : ""}>${activeAction === "reject" ? `<span class="loading-spinner button-spinner" aria-hidden="true"></span><span>处理中</span>` : "不要这条"}</button>
      </div>
    </div>
    <div class="plain-fact-grid">
      <div><span>知识类型</span><strong>${escapeHtml(readable.type)}</strong></div>
      <div><span>从哪里来</span><strong>${escapeHtml(candidateSourceText(item))}</strong></div>
      <div><span>当前状态</span><strong>${escapeHtml(candidateIsIncomplete(item) ? "还缺信息" : "内容已完善")}</strong></div>
      <div><span>确认后放到</span><strong>${escapeHtml(readable.target)}</strong></div>
    </div>
    ${candidateMissingHtml(item)}
    ${candidatePreviewHtml(item, category)}
    ${candidateLlmAssistHtml(item)}
    ${candidateRagEvidenceHtml(item)}
    ${candidateSupplementHtml(item, patch)}
  `;
  bindDynamicEditors(detail);
  detail.querySelector(".candidate-apply-detail")?.addEventListener("click", (event) => {
    applyCandidate(event.currentTarget.dataset.id).catch((error) => alert(error.message));
  });
  detail.querySelector(".candidate-reject-detail")?.addEventListener("click", (event) => {
    rejectCandidate(event.currentTarget.dataset.id).catch((error) => alert(error.message));
  });
  detail.querySelector(".candidate-category-change")?.addEventListener("click", () => {
    changeCandidateCategory(item.candidate_id).catch((error) => alert(error.message));
  });
  detail.querySelector(".candidate-supplement-save")?.addEventListener("click", () => {
    saveCandidateSupplement(item.candidate_id, patch.target_category).catch((error) => alert(error.message));
  });
}

function candidateLlmAssistHtml(item) {
  const assist = item.review?.llm_assist || {};
  if (!assist.policy_version) return "";
  const status = assist.status || "";
  const usedModel = status === "model_generated";
  const statusText = {
    model_generated: "已用大模型辅助判断",
    rule_fallback_after_llm: "已尝试大模型，当前为规则兜底",
    rule_only_disabled_by_request: "本次未启用大模型，仅规则兜底",
  }[status] || "AI辅助状态已记录";
  const reason = assist.reason || (usedModel ? "大模型已参与分类、提取和审核建议。" : "大模型不可用或未返回合格结果，系统保留规则结果供人工确认。");
  return `
    <div class="status-card ${usedModel ? "ok" : "warning"}">
      <strong>${escapeHtml(statusText)}</strong>
      <span>${escapeHtml(reason)}</span>
      ${assist.recommended_action ? `<small>AI建议：${escapeHtml(actionLabelFromValue(assist.recommended_action))}</small>` : ""}
    </div>
  `;
}

function candidateRagEvidenceHtml(item) {
  const evidence = item.review?.rag_evidence || {};
  const hits = evidence.hits || [];
  if (!evidence.enabled) return "";
  return `
    <details class="candidate-rag">
      <summary>查看AI参考来源</summary>
      ${hits.length ? hits.map((hit) => `
        <div class="read-field wide-field rag-hit">
          <span>${escapeHtml(hit.category || "资料片段")} · ${escapeHtml(hit.score || "")}</span>
          <p>${escapeHtml(readableSourcePlainText(hit.text || ""))}</p>
        </div>
      `).join("") : `<div class="empty-state">没有可展示的参考资料片段。</div>`}
    </details>
  `;
}

function candidateSupplementHtml(item, patch) {
  const categoryId = patch?.target_category || item.proposal?.target_category || "";
  const category = categoryById(categoryId);
  const data = patch?.item?.data || item.proposal?.suggested_fields || {};
  if (!category?.schema?.fields) {
    return `
      <div class="status-card warning">
        <strong>无法补充</strong>
        <span>当前候选没有匹配到可编辑的知识门类，请先确认目标库是否存在。</span>
      </div>
    `;
  }
  const missing = new Set([...(item.intake?.missing_fields || []), ...(item.review?.missing_fields || [])]);
  const fields = category.schema.fields.map((field) => missing.has(field.id) ? {...field, required: true} : field);
  const readonlyFields = isProductScopedCategory(category) ? new Set(["product_id"]) : new Set();
  const categoryOptions = candidateEditableCategories(categoryId)
    .map((candidateCategory) => `<option value="${escapeHtml(candidateCategory.id)}" ${candidateCategory.id === categoryId ? "selected" : ""}>${escapeHtml(candidateCategory.name || candidateCategory.id)}</option>`)
    .join("");
  return `
    <details class="candidate-supplement-panel candidate-edit-details">
      <summary class="candidate-edit-summary">
        <div>
          <span>修改这条知识</span>
          <strong>有错就直接改；缺信息就补上。保存后系统会重新判断是否可以加入知识库。</strong>
        </div>
        <span class="candidate-edit-toggle">点击展开</span>
      </summary>
      <div class="candidate-edit-body">
        <div class="candidate-category-tools">
          <label class="form-field">
            <span>这条知识的类型</span>
            <select id="candidate-category-target">${categoryOptions}</select>
          </label>
          <button class="secondary-button candidate-category-change" type="button">换成这个类型</button>
          <div class="category-help-line">商品专属问答、规则、解释必须先绑定到某个商品，不能在这里随意切换；请从商品库的商品详情里编辑。</div>
        </div>
        <div id="candidate-supplement-form" class="form-grid" data-candidate-id="${escapeHtml(item.candidate_id)}" data-category="${escapeHtml(categoryId)}">
          ${fields.map((field) => fieldHtml(field, data?.[field.id], {readonlyFields, categoryId: category.id, productName: productDisplayName(data?.product_id) || data?.product_id || ""})).join("")}
        </div>
        <div class="inline-actions candidate-supplement-actions">
          <button class="primary-button candidate-supplement-save" type="button">保存修改</button>
        </div>
      </div>
    </details>
  `;
}

async function changeCandidateCategory(candidateId) {
  const select = document.getElementById("candidate-category-target");
  const targetCategory = select?.value || "";
  if (!targetCategory) throw new Error("请选择目标类型");
  const payload = await apiJson(`/api/candidates/${encodeURIComponent(candidateId)}/category`, {
    method: "POST",
    body: JSON.stringify({target_category: targetCategory}),
  });
  if (!payload.ok) throw new Error(payload.message || "候选类型切换失败");
  state.selectedCandidate = payload.item;
  renderCandidateDetail(payload.item);
  await loadCandidates();
}

async function saveCandidateSupplement(candidateId, categoryId) {
  const category = categoryById(categoryId);
  const form = document.getElementById("candidate-supplement-form");
  if (!category || !form) throw new Error("没有找到候选补充表单");
  const data = {};
  for (const field of category.schema?.fields || []) {
    data[field.id] = collectFieldValue(field, form);
  }
  validateClientKnowledge(category, data);
  const payload = await apiJson(`/api/candidates/${encodeURIComponent(candidateId)}/supplement`, {
    method: "POST",
    body: JSON.stringify({data}),
  });
  if (!payload.ok) throw new Error(payload.message || "候选补充失败");
  state.selectedCandidate = payload.item;
  renderCandidateDetail(payload.item);
  await loadCandidates();
}

async function applyCandidate(candidateId) {
  if (!candidateId) return;
  if (state.candidateActionLoadingIds.has(candidateId)) return;
  if (!confirm(`确认应用候选 ${candidateId}？应用前会自动创建备份。`)) return;
  state.candidateActionLoadingIds.set(candidateId, "apply");
  if (state.selectedCandidate?.candidate_id === candidateId) renderCandidateDetail(state.selectedCandidate);
  await loadCandidates().catch(() => {});
  try {
    const payload = await apiJson(`/api/candidates/${encodeURIComponent(candidateId)}/apply`, {method: "POST"});
    if (!payload.ok) throw new Error(payload.message || "候选应用失败，请查看详情后补充、合并或拒绝。");
    renderDiagnostics(payload);
    clearCandidateDetail("已应用入库，候选已移出待审核列表。");
    await Promise.all([loadCandidates(), loadOverview(), loadKnowledge(), loadVersions()]);
  } finally {
    state.candidateActionLoadingIds.delete(candidateId);
    await loadCandidates().catch(() => {});
  }
}

async function rejectCandidate(candidateId) {
  if (!candidateId) return;
  if (state.candidateActionLoadingIds.has(candidateId)) return;
  const reasonInput = prompt("拒绝原因", "不适合写入正式知识库");
  if (reasonInput === null) return;
  const reason = reasonInput.trim() || "rejected in admin";
  state.candidateActionLoadingIds.set(candidateId, "reject");
  if (state.selectedCandidate?.candidate_id === candidateId) renderCandidateDetail(state.selectedCandidate);
  await loadCandidates().catch(() => {});
  try {
    await apiJson(`/api/candidates/${encodeURIComponent(candidateId)}/reject`, {
      method: "POST",
      body: JSON.stringify({reason}),
    });
    clearCandidateDetail("已拒绝该候选，候选已移出待审核列表。");
    await Promise.all([loadCandidates(), loadOverview()]);
  } finally {
    state.candidateActionLoadingIds.delete(candidateId);
    await loadCandidates().catch(() => {});
  }
}

async function loadRecorder() {
  const [summaryPayload, conversationsPayload, modulesPayload, runsPayload, runtimePayload] = await Promise.all([
    apiGet("/api/recorder/summary"),
    apiGet("/api/recorder/conversations?status=active"),
    apiGet("/api/recorder/modules").catch(() => ({items: []})),
    apiGet("/api/recorder/exports/runs?status=all&limit=30&ensure_worker=1").catch(() => ({items: [], runtime: {}})),
    apiGet("/api/recorder/runtime/status").catch(() => ({item: {}})),
  ]);
  state.recorderSummary = summaryPayload.item || {};
  state.recorderConversations = conversationsPayload.items || [];
  state.recorderModules = modulesPayload.items || [];
  state.recorderExportRuns = runsPayload.items || [];
  state.recorderRuntimeStatus = runtimePayload.item || runsPayload.runtime || state.recorderRuntimeStatus || {};
  if (
    state.selectedRecorderConversation?.conversation_id &&
    !state.recorderConversations.some((item) => item.conversation_id === state.selectedRecorderConversation.conversation_id)
  ) {
    state.selectedRecorderConversation = null;
  }
  state.selectedRecorderConversation = state.selectedRecorderConversation || state.recorderConversations[0] || null;
  await loadRecorderMessages(false);
  renderRecorder();
  syncRecorderExportPolling();
}

async function loadRecorderMessages(shouldRender = true, forceRefresh = false) {
  if (!state.selectedRecorderConversation?.conversation_id) {
    state.recorderMessages = [];
    if (shouldRender) renderRecorderDetail();
    return;
  }
  const convId = state.selectedRecorderConversation.conversation_id;
  const cache = state.recorderMessageCache[convId];
  if (!forceRefresh && cache && Date.now() - cache.loadedAt < 300000) {
    state.recorderMessages = cache.messages;
    if (shouldRender) renderRecorderDetail();
    return;
  }
  const payload = await apiGet(`/api/raw-messages/messages?conversation_id=${encodeURIComponent(convId)}&limit=80`);
  state.recorderMessages = payload.items || [];
  state.recorderMessageCache[convId] = { messages: state.recorderMessages, loadedAt: Date.now() };
  if (shouldRender) renderRecorderDetail();
}

function renderRecorder() {
  const summary = state.recorderSummary || {};
  const raw = summary.raw || {};
  const settings = summary.settings || {};
  const runtime = state.recorderRuntimeStatus || {};
  const enabled = settings.enabled !== false;
  const runtimeRunning = Boolean(runtime.running);
  setChecked("recorder-enabled", enabled);
  document.getElementById("recorder-status").textContent = summary.status || recorderStatusText(settings);
  const runtimeStatus = document.getElementById("recorder-runtime-status");
  if (runtimeStatus) runtimeStatus.textContent = recorderRuntimeStatusText(runtime, settings);
  document.getElementById("recorder-notify").checked = Boolean(settings.notify_on_collect);
  document.getElementById("recorder-auto-learn").checked = settings.auto_learn !== false;
  document.getElementById("recorder-use-llm").checked = settings.use_llm !== false;
  const captureButton = document.getElementById("recorder-capture");
  if (captureButton) captureButton.disabled = !enabled;
  const startButton = document.getElementById("recorder-runtime-start");
  if (startButton) startButton.disabled = !enabled || runtimeRunning || state.recorderRuntimeBusy;
  const stopButton = document.getElementById("recorder-runtime-stop");
  if (stopButton) stopButton.disabled = !runtimeRunning || state.recorderRuntimeBusy;
  document.getElementById("recorder-cards").innerHTML = `
    <div class="metric-card"><span>${raw.group_count ?? 0}</span><label>识别群聊</label></div>
    <div class="metric-card"><span>${summary.selected_conversation_count ?? summary.selected_group_count ?? 0}</span><label>正在记录</label></div>
    <div class="metric-card"><span>${raw.message_count ?? 0}</span><label>原始消息</label></div>
    <div class="metric-card"><span>${raw.pending_batch_count ?? 0}</span><label>待整理批次</label></div>
  `;
  renderRecorderModuleInfo();
  renderRecorderExportProgress();
  renderRecorderExportRuns();
  renderRecorderSelectedSummary();
  renderRecorderGroupList();
  renderRecorderDetail();
  renderCustomerServiceRuntime();
}

function renderRecorderModuleInfo() {
  const info = document.getElementById("recorder-module-info");
  if (!info) return;
  const modules = state.recorderModules || [];
  const latestRun = (state.recorderExportRuns || [])[0] || {};
  const latestModuleKey = String(latestRun.module_key || "");
  const latestModule = modules.find((item) => String(item.module_key || "") === latestModuleKey);
  const activeModule = latestModule || modules.find((item) => String(item.status || "active") === "active");
  const moduleName = activeModule?.module_name || activeModule?.module_key || "未配置";
  const moduleVersion = activeModule?.version || "-";
  info.innerHTML = `
    <strong>当前结构化导出模块：${escapeHtml(moduleName)}</strong>
    <span>模块Key：${escapeHtml(activeModule?.module_key || "-")} · 版本：${escapeHtml(moduleVersion)}。模块决定“规则+LLM抽取逻辑”和结构化 Excel 模板格式。</span>
  `;
}

function recorderRunStatusTone(status) {
  const key = String(status || "queued");
  if (key === "succeeded") return "ok";
  if (key === "failed") return "warning";
  if (["running", "preprocessing", "extracting", "reviewing", "exporting"].includes(key)) return "loading";
  return "info";
}

function recorderRunStatusLabel(status) {
  const key = String(status || "queued");
  if (key === "queued") return "排队中";
  if (key === "running") return "处理中";
  if (key === "preprocessing") return "预处理中";
  if (key === "scanning") return "筛选候选消息";
  if (key === "extracting") return "结构化抽取中";
  if (key === "llm_extracting") return "LLM语义抽取中";
  if (key === "llm_branding") return "品牌推断中";
  if (key === "finalizing") return "整理导出行";
  if (key === "reviewing") return "质量复核中";
  if (key === "exporting") return "生成导出文件";
  if (key === "succeeded") return "已完成";
  if (key === "failed") return "失败";
  return key;
}

function recorderRunIsActive(status) {
  const key = String(status || "queued");
  return ["queued", "running", "preprocessing", "extracting", "reviewing", "exporting"].includes(key);
}

function recorderRunStageLabel(run) {
  const progress = run?.progress || {};
  const stageLabel = String(progress.stage_label || "").trim();
  if (stageLabel) return stageLabel;
  const stage = String(run?.stage || "").trim();
  if (stage) return recorderRunStatusLabel(stage);
  return recorderRunStatusLabel(run?.status);
}

function hasRecorderExportActiveRuns(runs = state.recorderExportRuns || []) {
  return runs.some((run) => recorderRunIsActive(run.status));
}

function formatElapsedFrom(startText) {
  const startMs = Date.parse(String(startText || ""));
  if (!Number.isFinite(startMs)) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
  if (seconds < 60) return `${seconds}秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}分钟`;
  const hours = Math.floor(minutes / 60);
  const remainMinutes = minutes % 60;
  return remainMinutes ? `${hours}小时${remainMinutes}分钟` : `${hours}小时`;
}

function renderRecorderExportProgress() {
  const panel = document.getElementById("recorder-export-progress");
  const createButton = document.getElementById("recorder-export-run-create");
  const dayButton = document.getElementById("recorder-export-run-day");
  const weekButton = document.getElementById("recorder-export-run-week");
  const monthButton = document.getElementById("recorder-export-run-month");
  const customRangeButton = document.getElementById("recorder-export-run-custom");
  const clearDateButton = document.getElementById("recorder-export-date-clear");
  if (createButton) {
    createButton.classList.toggle("is-loading", state.recorderExportRunBusy);
    createButton.disabled = !hasLocalConsoleAccess() || state.recorderExportRunBusy;
    createButton.innerHTML = state.recorderExportRunBusy
      ? '<span class="loading-spinner button-spinner" aria-hidden="true"></span><span>创建中</span>'
      : "导出所有记录（结构化）";
  }
  if (dayButton) dayButton.disabled = !hasLocalConsoleAccess() || state.recorderExportRunBusy;
  if (weekButton) weekButton.disabled = !hasLocalConsoleAccess() || state.recorderExportRunBusy;
  if (monthButton) monthButton.disabled = !hasLocalConsoleAccess() || state.recorderExportRunBusy;
  if (customRangeButton) customRangeButton.disabled = !hasLocalConsoleAccess() || state.recorderExportRunBusy;
  if (clearDateButton) clearDateButton.disabled = !hasLocalConsoleAccess() || state.recorderExportRunBusy;
  if (!panel) return;

  const runs = state.recorderExportRuns || [];
  const latest = runs[0] || null;
  const activeRuns = runs.filter((run) => recorderRunIsActive(run.status));
  const runtime = state.recorderRuntimeStatus || {};
  const workerRunning = runtime.worker_running === true;
  const queue = runtime.queue_summary || {};

  if (state.recorderExportRunBusy) {
    panel.innerHTML = `
      <div class="status-card loading">
        <strong><span class="loading-spinner" aria-hidden="true"></span>正在创建结构化导出任务</strong>
        <span>请求已提交，正在写入后台队列。</span>
      </div>
    `;
    return;
  }

  if (activeRuns.length) {
    const queuedCount = activeRuns.filter((run) => String(run.status || "") === "queued").length;
    const runningCount = activeRuns.filter((run) => String(run.status || "") === "running").length;
    const lead = activeRuns[0] || {};
    const elapsed = formatElapsedFrom(lead.started_at || lead.created_at || "");
    const progress = lead.progress || {};
    const processed = Number(progress.processed_messages ?? 0);
    const total = Number(progress.total_messages ?? 0);
    const stageLabel = recorderRunStageLabel(lead);
    const stageDetail = String(progress.stage_detail || "").trim();
    const unitLabel = String(progress.unit_label || "消息").trim() || "消息";
    const percent = Number(progress.percent ?? 0);
    const percentText = Number.isFinite(percent) ? `${Math.round(Math.max(0, Math.min(percent, 1)) * 100)}%` : "";
    const tone = workerRunning ? "loading" : "warning";
    panel.innerHTML = `
      <div class="status-card ${tone}">
        <strong><span class="loading-spinner" aria-hidden="true"></span>${runningCount > 0 ? "结构化导出处理中" : "结构化导出排队中"} · ${escapeHtml(stageLabel)}</strong>
        <span>任务数：排队 ${queuedCount} · 处理中 ${runningCount}${elapsed ? ` · 已持续 ${escapeHtml(elapsed)}` : ""}</span>
        ${stageDetail ? `<span>${escapeHtml(stageDetail)}</span>` : ""}
        <span>${total > 0 ? `进度：${escapeHtml(unitLabel)} ${escapeHtml(String(processed))}/${escapeHtml(String(total))}（${escapeHtml(percentText)}）` : "进度：正在初始化任务..."}</span>
        <span>${escapeHtml(workerRunning ? "后台导出 worker 在线，系统正在自动处理。" : "后台导出 worker 暂未检测到在线，系统正在自动拉起。")} 队列：待执行 ${escapeHtml(String(queue.pending ?? 0))} · 执行中 ${escapeHtml(String(queue.running ?? 0))}。</span>
      </div>
    `;
    return;
  }

  if (latest && String(latest.status || "") === "succeeded") {
    const rows = Number(latest?.stats?.export_row_count ?? 0);
    panel.innerHTML = `
      <div class="status-card ok">
        <strong>最近一次结构化导出已完成</strong>
        <span>任务 ${escapeHtml(latest.run_id || "-")} · 导出 ${escapeHtml(String(rows))} 行。可直接点击“下载Excel”。</span>
      </div>
    `;
    return;
  }

  if (latest && String(latest.status || "") === "failed") {
    panel.innerHTML = `
      <div class="status-card warning">
        <strong>最近一次结构化导出失败</strong>
        <span>任务 ${escapeHtml(latest.run_id || "-")} · ${escapeHtml(latest.error || "请查看下载报告排查问题。")}</span>
      </div>
    `;
    return;
  }

  panel.innerHTML = `
    <div class="status-card">
      <strong>暂无进行中的结构化导出任务</strong>
      <span>点击“导出所有记录（结构化）”后，这里会实时显示排队和处理进度。</span>
    </div>
  `;
}

function renderRecorderExportRuns() {
  const list = document.getElementById("recorder-export-runs-list");
  if (!list) return;
  const runs = state.recorderExportRuns || [];
  list.innerHTML = `<h3>结构化导出任务（最近30条）</h3>${
    runs.length
      ? runs.map((run) => {
        const status = String(run.status || "queued");
        const statusTone = recorderRunStatusTone(status);
        const stats = run.stats || {};
        const progress = run.progress || {};
        const stageLabel = recorderRunStageLabel(run);
        const processed = Number(progress.processed_messages ?? 0);
        const total = Number(progress.total_messages ?? 0);
        const percent = Number(progress.percent ?? 0);
        const stageDetail = String(progress.stage_detail || "").trim();
        const unitLabel = String(progress.unit_label || "消息").trim() || "消息";
        const progressText = total > 0
          ? `${unitLabel} ${processed}/${total}（${Math.round(Math.max(0, Math.min(percent, 1)) * 100)}%）`
          : "初始化中";
        const elapsed = recorderRunIsActive(status) ? formatElapsedFrom(run.started_at || run.created_at || "") : "";
        return `
          <div class="record-row">
            <div class="row-title">
              <strong>${escapeHtml(run.run_id || "")}</strong>
              <span class="status-chip ${statusTone}">${escapeHtml(recorderRunStatusLabel(status))}</span>
            </div>
            <span>模块：${escapeHtml(run.module_name || run.module_key || "-")} · 版本：${escapeHtml(run.module_version || "-")} · 阶段：${escapeHtml(stageLabel)} · 创建：${escapeHtml(run.created_at || "-")}${elapsed ? ` · 已持续 ${escapeHtml(elapsed)}` : ""}</span>
            ${stageDetail ? `<span>当前步骤：${escapeHtml(stageDetail)}</span>` : ""}
            <span>输入消息：${escapeHtml(String(stats.input_message_count ?? 0))} · 导出行：${escapeHtml(String(stats.export_row_count ?? 0))} · 需复核：${escapeHtml(String(stats.needs_review_count ?? 0))} · 进度：${escapeHtml(progressText)}</span>
            <span>LLM调用：${escapeHtml(String(stats.llm_calls ?? 0))}（分段 ${escapeHtml(String(stats.llm_segment_calls ?? 0))} · 修复 ${escapeHtml(String(stats.llm_repair_calls ?? 0))}）</span>
            ${run.error ? `<span>失败原因：${escapeHtml(run.error)}</span>` : ""}
            <div class="button-row">
              <button class="secondary-button recorder-run-download" data-run-id="${escapeAttr(run.run_id || "")}" ${status !== "succeeded" ? "disabled" : ""}>下载Excel</button>
              <button class="secondary-button recorder-run-report" data-run-id="${escapeAttr(run.run_id || "")}" ${status !== "succeeded" ? "disabled" : ""}>下载报告</button>
              <button class="secondary-button danger-button recorder-run-delete" data-run-id="${escapeAttr(run.run_id || "")}">删除</button>
            </div>
          </div>
        `;
      }).join("")
      : `<div class="empty-state">暂无结构化导出任务。点击“导出所有记录（结构化）”发起新任务。</div>`
  }`;
  list.querySelectorAll(".recorder-run-download").forEach((button) => {
    button.addEventListener("click", () => downloadRecorderRunArtifact(button.dataset.runId, "xlsx").catch((error) => alert(error.message)));
  });
  list.querySelectorAll(".recorder-run-report").forEach((button) => {
    button.addEventListener("click", () => downloadRecorderRunArtifact(button.dataset.runId, "report").catch((error) => alert(error.message)));
  });
  list.querySelectorAll(".recorder-run-delete").forEach((button) => {
    button.addEventListener("click", () => deleteRecorderExportRun(button.dataset.runId).catch((error) => alert(error.message)));
  });
}

function renderRecorderSelectedSummary() {
  const panel = document.getElementById("recorder-selected-summary");
  if (!panel) return;
  const conversations = state.recorderConversations || [];
  const selected = conversations.filter((item) => item.selected_by_user);
  const groupCount = selected.filter((item) => item.conversation_type === "group").length;
  const privateCount = selected.filter((item) => item.conversation_type === "private").length;
  const ftCount = selected.filter((item) => item.conversation_type === "file_transfer").length;
  const previewNames = selected
    .slice(0, 6)
    .map((item) => item.display_name || item.target_name || item.conversation_id)
    .filter(Boolean);
  const more = selected.length > previewNames.length ? ` 等 ${selected.length} 个` : "";
  panel.innerHTML = `
    <strong>当前监听会话：${escapeHtml(String(selected.length))} 个（群聊 ${escapeHtml(String(groupCount))} · 私聊 ${escapeHtml(String(privateCount))} · 文件传输助手 ${escapeHtml(String(ftCount))}）</strong>
    <span>${selected.length ? `已选择：${escapeHtml(previewNames.join("、"))}${escapeHtml(more)}` : "尚未选择监听会话，请在下方列表勾选。"} </span>
  `;
}

function renderRecorderGroupList() {
  const list = document.getElementById("recorder-group-list");
  const items = state.recorderConversations || [];
  list.innerHTML = items.length ? items.map((item, index) => {
    const active = state.selectedRecorderConversation?.conversation_id === item.conversation_id ? " is-selected" : "";
    const selected = Boolean(item.selected_by_user);
    return `
      <div class="record-row recorder-row${active}" data-index="${index}">
        <button class="link-button recorder-select" data-index="${index}">
          <strong>${escapeHtml(item.display_name || item.target_name || item.conversation_id)}</strong>
          <span>${escapeHtml(formatRecorderConversationStatus(item))}</span>
          ${badgeListHtml([{key: item.conversation_type || "unknown", label: recorderConversationTypeLabel(item.conversation_type), tone: "info"}, ...(selected ? [{key: "recording", label: "记录中", tone: "ok"}] : [{key: "paused", label: "未选择", tone: "muted"}])])}
        </button>
        <div class="inline-actions">
          <label class="checkbox-line">
            <input class="recorder-toggle-checkbox" type="checkbox" data-id="${escapeHtml(item.conversation_id)}" ${selected ? "checked" : ""} />
            记录中
          </label>
        </div>
      </div>
    `;
  }).join("") : `<div class="empty-state">尚未识别到会话。点击“识别会话”后，再勾选需要监听的对象。</div>`;
  list.querySelectorAll(".recorder-select").forEach((button) => {
    button.addEventListener("click", async () => {
      const newIndex = Number(button.dataset.index);
      const newConversation = items[newIndex] || null;
      // Update selection styles without rebuilding the whole list
      list.querySelectorAll(".recorder-row").forEach((row, idx) => {
        row.classList.toggle("is-selected", idx === newIndex);
      });
      state.selectedRecorderConversation = newConversation;
      await loadRecorderMessages();
    });
  });
  list.querySelectorAll(".recorder-toggle-checkbox").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const conversationId = checkbox.dataset.id || "";
      updateRecorderConversation(conversationId, {selected_by_user: checkbox.checked}).catch((error) => alert(error.message));
    });
  });
}

async function applyRecorderSelection(mode = "groups") {
  const conversations = state.recorderConversations || [];
  const patchTargets = [];
  for (const item of conversations) {
    let selectedTarget = Boolean(item.selected_by_user);
    if (mode === "groups") selectedTarget = item.conversation_type === "group";
    if (mode === "none") selectedTarget = false;
    if (Boolean(item.selected_by_user) !== selectedTarget) {
      patchTargets.push({conversation_id: item.conversation_id, selected_by_user: selectedTarget});
    }
  }
  if (!patchTargets.length) {
    alert(mode === "none" ? "当前已经是空选择。" : "当前会话选择已是最新状态。");
    return;
  }
  for (const item of patchTargets) {
    await apiJson(`/api/recorder/conversations/${encodeURIComponent(item.conversation_id)}`, {
      method: "PATCH",
      body: JSON.stringify({selected_by_user: item.selected_by_user}),
    });
  }
  await loadRecorder();
  alert(mode === "none" ? "已清空监听会话选择。" : "已自动选择全部群聊。");
}

function candidateReadableSummary(item, category) {
  const patch = item.proposal?.formal_patch || {};
  const data = patch.item?.data || item.proposal?.suggested_fields || {};
  const categoryName = category?.name || patch.target_category || "知识";
  const titleField = category?.schema?.item_title_field || "title";
  const title = data[titleField] || data.name || data.title || data.customer_message || item.proposal?.summary || item.candidate_id;
  const summary = plainCandidateSummary(data, patch.target_category || category?.id || "", item.proposal?.summary || "");
  return {
    title: String(title || item.candidate_id),
    type: categoryName,
    target: categoryName,
    summary,
  };
}

function plainCandidateSummary(data, categoryId, fallback) {
  if (categoryId === "products") {
    const parts = [
      data.price ? `价格 ${data.price}${data.unit ? `/${data.unit}` : ""}` : "",
      data.inventory !== undefined && data.inventory !== "" ? `库存 ${data.inventory}` : "",
      data.category ? `类目 ${data.category}` : "",
    ].filter(Boolean);
    return parts.length ? parts.join("，") : (fallback || "这是一条商品资料。");
  }
  if (categoryId === "policies") return data.answer || fallback || "这是一条规则或政策。";
  if (categoryId === "chats") return data.service_reply || fallback || "这是一条客服话术。";
  if (categoryId === "product_faq" || categoryId === "product_rules") return data.answer || fallback || "这是一条商品专属知识。";
  if (categoryId === "product_explanations") return data.content || fallback || "这是一条商品专属说明。";
  return fallback || "这是一条可加入知识库的内容。";
}

function candidateMissingHtml(item) {
  const intake = item.intake || {};
  const missing = (intake.missing_labels || intake.missing_fields || []).filter(Boolean);
  const warnings = [...(intake.warnings || []), ...(item.proposal?.warnings || [])].filter(Boolean);
  if (!missing.length && !warnings.length && !intake.question) return "";
  return `
    <div class="status-card ${missing.length ? "warning" : ""}">
      <strong>${missing.length ? "还需要补充一点信息" : "需要留意"}</strong>
      <span>${escapeHtml(missing.length ? `缺少：${missing.join("、")}` : warnings.join("、"))}</span>
      ${intake.question ? `<p>${escapeHtml(intake.question)}</p>` : ""}
    </div>
  `;
}

function candidateEditableCategories(currentCategoryId) {
  const normal = state.categories.filter((category) => category.scope !== "tenant_product");
  const current = categoryById(currentCategoryId);
  if (current?.scope === "tenant_product" && !normal.some((category) => category.id === current.id)) {
    return [current, ...normal];
  }
  return normal;
}

function candidatePreviewHtml(item, category) {
  const patch = item.proposal?.formal_patch || {};
  const data = patch.item?.data || item.proposal?.suggested_fields || {};
  const fields = (category?.schema?.fields || []).filter((field) => hasDisplayValue(data[field.id]));
  if (!fields.length) return "";
  return `
    <div class="preview-panel">
      <div class="section-heading"><div><span>这条知识的主要内容</span><strong>请先看这里，判断有没有错误。</strong></div></div>
      <div class="read-grid">
        ${fields.slice(0, 8).map((field) => `<div class="read-field ${field.type === "long_text" ? "wide-field" : ""}"><span>${escapeHtml(field.label || field.id)}</span><p>${escapeHtml(displayValue(data[field.id]))}</p></div>`).join("")}
      </div>
    </div>
  `;
}

function hasDisplayValue(value) {
  if (value === undefined || value === null || value === "") return false;
  if (Array.isArray(value) && !value.length) return false;
  if (typeof value === "object" && !Array.isArray(value) && !Object.keys(value).length) return false;
  return true;
}

function displayValue(value) {
  if (Array.isArray(value)) {
    return value
      .filter((inner) => hasDisplayValue(inner))
      .map((inner) => {
        if (typeof inner === "object" && inner !== null) {
          return Object.entries(inner)
            .filter(([, nested]) => hasDisplayValue(nested))
            .map(([key, nested]) => `${fieldLabel({id: key, label: key})}：${displayValue(nested)}`)
            .join("，");
        }
        return displayValue(inner);
      })
      .join("；");
  }
  if (typeof value === "object" && value !== null) {
    return Object.entries(value)
      .filter(([, inner]) => hasDisplayValue(inner))
      .map(([key, inner]) => `${fieldLabel({id: key, label: key})}：${displayValue(inner)}`)
      .join("；");
  }
  return String(value ?? "");
}

function renderRecorderDetail() {
  const detail = document.getElementById("recorder-detail");
  const conversation = state.selectedRecorderConversation;
  const recorderMessages = state.recorderMessages || [];
  if (!conversation) {
    detail.innerHTML = `<div class="empty-state">请选择一个群查看最近记录。</div>`;
    return;
  }
  detail.innerHTML = `
    <div class="read-head">
      <div>
        <p class="eyebrow">${escapeHtml(recorderConversationTypeLabel(conversation.conversation_type))}记录</p>
        <h2>${escapeHtml(conversation.display_name || conversation.target_name || conversation.conversation_id)}</h2>
        <p class="source-line">共 ${recorderMessages.length} 条原始消息，内容区默认最多展示 20 条高度，超出可在框内滚动查看。</p>
        ${badgeListHtml([{key: conversation.conversation_type || "unknown", label: recorderConversationTypeLabel(conversation.conversation_type), tone: "info"}, ...(conversation.selected_by_user ? [{key: "recording", label: "记录中", tone: "ok"}] : [{key: "paused", label: "未选择", tone: "muted"}])])}
      </div>
      <button class="secondary-button recorder-refresh-messages" type="button">刷新消息</button>
    </div>
    <div class="compact-list compact-list-small recorder-message-scroll" role="region" aria-label="会话原始消息列表">
      ${recorderMessages.map((item) => `
        <div class="compact-row">
          <strong>${escapeHtml(item.sender || item.sender_role || "unknown")}</strong>
          <span>${escapeHtml(item.captured_at || item.message_time || item.observed_at || "")}</span>
          <p>${escapeHtml(item.content_clean || item.content_body || item.content || "")}</p>
          ${recorderMessageQualityHtml(item)}
        </div>
      `).join("") || `<div class="empty-state">暂无原始消息。</div>`}
    </div>
  `;
  detail.querySelector(".recorder-refresh-messages")?.addEventListener("click", () => loadRecorderMessages().catch((error) => alert(error.message)));
}

function recorderMessageQualityHtml(item = {}) {
  const flags = Array.isArray(item.quality_flags) ? item.quality_flags.filter(Boolean) : [];
  const quoted = Array.isArray(item.quoted_fragments) ? item.quoted_fragments : [];
  const excluded = Array.isArray(item.excluded_fragments) ? item.excluded_fragments : [];
  const parts = [];
  if (item.screen_time_text) parts.push(`屏幕时间: ${escapeHtml(item.screen_time_text)}`);
  if (item.speaker_name || item.group_member_name) parts.push(`群成员: ${escapeHtml(item.speaker_name || item.group_member_name)}`);
  if (flags.length) parts.push(`质量: ${escapeHtml(flags.join(", "))}`);
  if (quoted.length) parts.push(`引用已排除: ${escapeHtml(quoted.map((fragment) => fragment?.text || fragment).filter(Boolean).join("；"))}`);
  if (excluded.length) parts.push(`排除片段: ${escapeHtml(excluded.map((fragment) => fragment?.reason || "").filter(Boolean).join(", "))}`);
  return parts.length ? `<small class="muted">${parts.join(" · ")}</small>` : "";
}

function formatRecorderConversationStatus(item) {
  const parts = [item.status || "active"];
  if (item.notify_enabled) parts.push("群内提示");
  if (item.learning_enabled === false) parts.push("仅记录");
  return parts.join(" · ");
}

function recorderConversationTypeLabel(value) {
  const text = String(value || "");
  if (text === "group") return "群聊";
  if (text === "file_transfer") return "文件传输助手";
  if (text === "private") return "私聊";
  if (text === "system") return "系统会话";
  return "未知会话";
}

function recorderStatusText(settings = {}) {
  if (settings.enabled === false) return "已关闭，不会自动采集聊天记录。";
  return "已开启，可按会话配置采集聊天记录。";
}

function recorderRuntimeStatusText(runtime = {}, settings = {}) {
  if (runtime.state === "paused") return runtime.message || "已暂停，等待继续";
  if (runtime.running) {
    const interval = Number(settings.capture_interval_seconds || 30);
    if (runtime.liveness_ok === false) {
      return `监听中（活性异常，最近心跳 ${Number.isFinite(Number(runtime.liveness_age_seconds)) ? `${Number(runtime.liveness_age_seconds)}s前` : "缺失"}）`;
    }
    return `监听中（轮询间隔 ${Number.isFinite(interval) && interval > 0 ? interval : 30}s）`;
  }
  if (settings.enabled === false) return "已停止";
  return "未启动";
}

async function saveRecorderSettings() {
  const payload = await apiJson("/api/recorder/settings", {
    method: "PUT",
    body: JSON.stringify({
      enabled: document.getElementById("recorder-enabled").checked,
      notify_on_collect: document.getElementById("recorder-notify").checked,
      auto_learn: document.getElementById("recorder-auto-learn").checked,
      use_llm: document.getElementById("recorder-use-llm").checked,
    }),
  });
  state.recorderSummary = {...(state.recorderSummary || {}), settings: payload.item || {}};
  renderRecorder();
}

async function discoverRecorderSessions() {
  const button = document.getElementById("recorder-discover");
  const originalText = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "识别中...";
  }
  try {
    const result = await apiJson("/api/recorder/discover", {method: "POST", body: "{}"});
    if (!result.ok) {
      alert(result.message || "未能连接微信主窗口，请确认微信已登录并保持主窗口可见。");
    }
    await loadRecorder();
    if (result.ok) {
      const count = Array.isArray(result.items) ? result.items.length : 0;
      const archivedCount = Number(result.archived_count || 0);
      alert(`会话识别完成：共 ${count} 条，已归档旧会话 ${archivedCount} 条。`);
    }
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = originalText || "识别会话";
    }
  }
}

async function startRecorderRuntime() {
  state.recorderRuntimeBusy = true;
  state.recorderRuntimeStatus = {
    ...(state.recorderRuntimeStatus || {}),
    running: true,
    state: "thinking",
    message: "AI智能记录员正在启动。",
  };
  renderCustomerServiceRuntime();
  try {
    const result = await apiJson("/api/recorder/runtime/start", {method: "POST", body: "{}"});
    state.recorderRuntimeStatus = result.item || state.recorderRuntimeStatus || {};
    await loadRecorder();
  } finally {
    state.recorderRuntimeBusy = false;
    renderCustomerServiceRuntime();
  }
}

async function stopRecorderRuntime() {
  state.recorderRuntimeBusy = true;
  state.recorderRuntimeStatus = {
    ...(state.recorderRuntimeStatus || {}),
    running: false,
    state: "stopped",
    message: "AI智能记录员正在停止。",
    operator_guard_running: false,
    operator_guard_pid: null,
    operator_guard_state: {},
  };
  renderCustomerServiceRuntime();
  try {
    const result = await apiJson("/api/recorder/runtime/stop", {method: "POST", body: "{}"});
    state.recorderRuntimeStatus = result.item || state.recorderRuntimeStatus || {};
    await loadRecorder();
  } finally {
    state.recorderRuntimeBusy = false;
    renderCustomerServiceRuntime();
  }
}

async function captureRecorderNow() {
  const result = await apiJson("/api/recorder/capture", {method: "POST", body: JSON.stringify({send_notifications: true})});
  await loadRecorder();
  if (result.enabled === false) {
    alert(result.message || "AI智能记录员已关闭，请先开启后再执行采集。");
    return;
  }
  alert(`本轮补采完成：新增 ${result.inserted_count || 0} 条消息。`);
}

async function updateRecorderConversation(conversationId, patch) {
  if (!conversationId) return;
  await apiJson(`/api/recorder/conversations/${encodeURIComponent(conversationId)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
  await loadRecorder();
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function formatDateInputValue(date) {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

function parseDateInputValue(value) {
  const text = String(value || "").trim();
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (!year || month < 1 || month > 12 || day < 1 || day > 31) return null;
  const date = new Date(year, month - 1, day);
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) return null;
  return date;
}

function buildRecorderPresetDateRange(preset) {
  const now = new Date();
  if (preset === "day") {
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const dateText = formatDateInputValue(today);
    return {startDate: dateText, endDate: dateText, label: `按日（${dateText}）`};
  }
  if (preset === "week") {
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const weekday = (today.getDay() + 6) % 7;
    const start = new Date(today);
    start.setDate(today.getDate() - weekday);
    const end = new Date(start);
    end.setDate(start.getDate() + 6);
    return {
      startDate: formatDateInputValue(start),
      endDate: formatDateInputValue(end),
      label: `按周（${formatDateInputValue(start)} ~ ${formatDateInputValue(end)}）`,
    };
  }
  if (preset === "month") {
    const start = new Date(now.getFullYear(), now.getMonth(), 1);
    const end = new Date(now.getFullYear(), now.getMonth() + 1, 0);
    return {
      startDate: formatDateInputValue(start),
      endDate: formatDateInputValue(end),
      label: `按月（${formatDateInputValue(start)} ~ ${formatDateInputValue(end)}）`,
    };
  }
  return null;
}

function collectRecorderExportDateRange(options = {}) {
  const mode = String(options.mode || "");
  const preset = String(options.preset || "");
  const startInput = document.getElementById("recorder-export-start-date");
  const endInput = document.getElementById("recorder-export-end-date");
  let startDateText = String(startInput?.value || "").trim();
  let endDateText = String(endInput?.value || "").trim();
  let label = "全部日期";

  if (mode === "all" && !preset) {
    return {startTime: "", endTime: "", dateFrom: "", dateTo: "", quickRange: "", label};
  }

  if (preset) {
    const range = buildRecorderPresetDateRange(preset);
    if (!range) throw new Error(`不支持的日期快捷导出：${preset}`);
    startDateText = range.startDate;
    endDateText = range.endDate;
    label = range.label;
    if (startInput) startInput.value = startDateText;
    if (endInput) endInput.value = endDateText;
  }

  if (!startDateText && !endDateText) {
    if (mode === "custom") {
      throw new Error("请选择开始日期和结束日期后再执行“按日期范围导出”。");
    }
    return {startTime: "", endTime: "", label};
  }
  if (!startDateText || !endDateText) {
    throw new Error("请同时选择开始日期和结束日期。");
  }

  const startDate = parseDateInputValue(startDateText);
  const endDate = parseDateInputValue(endDateText);
  if (!startDate || !endDate) throw new Error("日期格式无效，请按 YYYY-MM-DD 选择。");
  if (startDate.getTime() > endDate.getTime()) throw new Error("开始日期不能晚于结束日期。");
  return {
    startTime: `${startDateText} 00:00:00`,
    endTime: `${endDateText} 23:59:59`,
    dateFrom: startDateText,
    dateTo: endDateText,
    quickRange: preset || "",
    label: `${startDateText} ~ ${endDateText}`,
  };
}

function clearRecorderExportDateRange() {
  const startInput = document.getElementById("recorder-export-start-date");
  const endInput = document.getElementById("recorder-export-end-date");
  if (startInput) startInput.value = "";
  if (endInput) endInput.value = "";
}

async function createRecorderExportRun(options = {}) {
  if (state.recorderExportRunBusy) return;
  const selected = (state.recorderConversations || []).filter((item) => item.selected_by_user);
  if (!selected.length) {
    alert("请先在会话列表中选择至少一个“记录中”会话，再发起导出。");
    return;
  }
  const mode = String(options.mode || "");
  const preset = String(options.preset || "");
  const dateRange = collectRecorderExportDateRange({mode, preset});
  state.recorderExportRunBusy = true;
  renderRecorderExportProgress();
  const payload = {
    target_names: selected.map((item) => item.target_name || item.display_name).filter(Boolean),
    start_time: dateRange.startTime,
    end_time: dateRange.endTime,
    date_from: dateRange.dateFrom || "",
    date_to: dateRange.dateTo || "",
    quick_range: dateRange.quickRange || "",
    limit: RECORDER_EXPORT_DEFAULT_LIMIT,
    ensure_worker: true,
  };
  try {
    const result = await apiJson("/api/recorder/exports/runs", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.recorderRuntimeStatus = result.runtime || state.recorderRuntimeStatus || {};
    const runId = result.item?.run_id || "";
    await refreshRecorderExportRuns({silent: true});
    if (runId) {
      alert(`结构化导出任务已创建：${runId}。\n日期范围：${dateRange.label}。\n页面会自动刷新状态，你可以留在本页观察进度。`);
    }
  } finally {
    state.recorderExportRunBusy = false;
    renderRecorderExportProgress();
    syncRecorderExportPolling();
  }
}

function stopRecorderExportPolling() {
  if (state.recorderExportPollingTimer) clearInterval(state.recorderExportPollingTimer);
  state.recorderExportPollingTimer = null;
}

function syncRecorderExportPolling() {
  const shouldPoll = hasLocalConsoleAccess() && state.activeView === "recorder" && hasRecorderExportActiveRuns();
  if (!shouldPoll) {
    stopRecorderExportPolling();
    return;
  }
  if (state.recorderExportPollingTimer) return;
  state.recorderExportPollingTimer = setInterval(() => {
    if (state.activeView !== "recorder" || !hasLocalConsoleAccess()) {
      stopRecorderExportPolling();
      return;
    }
    refreshRecorderExportRuns({silent: true}).catch(() => {});
  }, 3000);
}

async function refreshRecorderExportRuns(options = {}) {
  const {silent = false} = options;
  try {
    const payload = await apiGet("/api/recorder/exports/runs?status=all&limit=30&ensure_worker=1");
    state.recorderExportRuns = payload.items || [];
    state.recorderRuntimeStatus = payload.runtime || state.recorderRuntimeStatus || {};
    renderRecorderExportProgress();
    renderRecorderExportRuns();
    renderRecorderModuleInfo();
    syncRecorderExportPolling();
  } catch (error) {
    if (!silent) throw error;
    console.warn("refresh recorder export runs failed", error);
  }
}

async function deleteRecorderExportRun(runId) {
  if (!runId) return;
  if (!confirm(`确认删除导出任务 ${runId} 吗？删除后无法恢复。`)) return;
  await apiJson(`/api/recorder/exports/runs/${encodeURIComponent(runId)}`, {method: "DELETE"});
  await refreshRecorderExportRuns();
  alert(`已删除导出任务：${runId}`);
}

async function downloadRecorderRunArtifact(runId, kind = "xlsx") {
  if (!runId) return;
  const endpoint = kind === "report"
    ? `/api/recorder/exports/runs/${encodeURIComponent(runId)}/report`
    : `/api/recorder/exports/runs/${encodeURIComponent(runId)}/download`;
  const response = await fetch(endpoint, {headers: apiHeaders()});
  if (!response.ok) throw new Error(await responseErrorMessage(response, endpoint));
  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") || "";
  const match = disposition.match(/filename="?([^"]+)"?/i);
  const fallback = kind === "report" ? `${runId}.json` : `${runId}.xlsx`;
  const filename = match?.[1] || fallback;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function downloadKnowledgeExport(sortBy) {
  const mode = sortBy === "time" ? "time" : "session";
  const endpoint = `/api/exports/raw-chats/download?mode=${encodeURIComponent(mode)}`;
  const response = await fetch(endpoint, {headers: apiHeaders()});
  if (!response.ok) throw new Error(await responseErrorMessage(response, "/api/exports/raw-chats/download"));
  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") || "";
  const match = disposition.match(/filename="?([^"]+)"?/i);
  const filename = match?.[1] || `wechat_chat_records_${mode}.xlsx`;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function loadDiagnosticsData() {
  if (state.diagnosticsPayload) {
    renderDiagnostics(state.diagnosticsPayload);
  }
}

async function runDiagnostics(mode) {
  const reportEl = document.getElementById("diagnostics-report");
  const quickBtn = document.getElementById("quick-diagnostics");
  const fullBtn = document.getElementById("full-diagnostics");
  const controller = new AbortController();
  const timeoutMs = mode === "full" ? 180000 : 45000;
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);

  if (quickBtn) quickBtn.disabled = true;
  if (fullBtn) fullBtn.disabled = true;

  reportEl.innerHTML = `
    <div class="status-card info">
      <strong>正在检测中...</strong>
      <span>${mode === "full" ? "全量诊断需要 30-120 秒，请稍候" : "快速检测中..."}</span>
      <span class="spinner" style="margin-left:8px;"></span>
    </div>
  `;

  try {
    const payload = await apiJson("/api/diagnostics/run", {
      method: "POST",
      signal: controller.signal,
      body: JSON.stringify({
        mode,
        include_llm_audit: mode === "full",
      }),
    });
    state.diagnosticsPayload = payload;
    renderDiagnostics(payload);
  } catch (error) {
    const message = error?.name === "AbortError"
      ? `检测超过 ${Math.round(timeoutMs / 1000)} 秒仍未完成，已停止等待。请稍后重试，或改用全量检测。`
      : error.message;
    reportEl.innerHTML = `
      <div class="status-card error">
        <strong>检测失败</strong>
        <span>${escapeHtml(message)}</span>
      </div>
    `;
  } finally {
    window.clearTimeout(timeoutId);
    if (quickBtn) quickBtn.disabled = false;
    if (fullBtn) fullBtn.disabled = false;
  }
}

function renderDiagnostics(payload) {
  const issues = payload.issues || payload.validation?.issues || [];
  const status = payload.status || (payload.ok ? "ok" : "error");
  const hasRepairable = issues.some((issue) => issue.repairable || issue.auto_repair);
  const repairButton = payload.run_id && hasRepairable
    ? `<button class="secondary-button diagnostic-repair" data-run-id="${escapeHtml(payload.run_id)}">一键修复</button>`
    : "";
  const ignored = payload.ignored_count || payload.summary?.ignored_count || 0;
  const clearButton = ignored
    ? `<button class="secondary-button diagnostic-clear-notices">清除提示记录</button>`
    : "";
  document.getElementById("diagnostics-report").innerHTML = `
    <div class="status-card ${status}"><strong>${escapeHtml(diagnosticStatusTitle(status, issues))}</strong><span>${escapeHtml(diagnosticSummaryText(payload, issues))}${ignored ? ` · 已忽略 ${ignored} 条` : ""}</span></div>
    ${payload.run_id ? `<div class="issue-meta diagnostic-run-meta"><span>检测编号</span><strong>${escapeHtml(payload.run_id)}</strong></div>` : ""}
    ${repairButton}
    ${clearButton}
    ${issues.length ? issues.map(issueHtml).join("") : `<div class="empty-state">未发现故障</div>`}
  `;
  document.querySelectorAll(".diagnostic-repair").forEach((button) => {
    button.addEventListener("click", () => applyDiagnosticRepair(button.dataset.runId).catch((error) => alert(error.message)));
  });
  document.querySelectorAll(".diagnostic-ignore").forEach((button) => {
    button.addEventListener("click", () => ignoreDiagnosticIssue(button.dataset.fingerprint).catch((error) => alert(error.message)));
  });
  document.querySelectorAll(".diagnostic-open").forEach((button) => {
    button.addEventListener("click", () => openDiagnosticTarget(button.dataset.target, button.dataset.targets).catch((error) => alert(error.message)));
  });
  document.querySelectorAll(".diagnostic-toggle").forEach((button) => {
    button.addEventListener("click", () => toggleDiagnosticDetails(button));
  });
  document.querySelectorAll(".diagnostic-clear-notices").forEach((button) => {
    button.addEventListener("click", () => clearDiagnosticNotices().catch((error) => alert(error.message)));
  });
  document.querySelectorAll(".diagnostic-merge").forEach((button) => {
    button.addEventListener("click", () => openDiagnosticMerge(button.dataset.targets).catch((error) => alert(error.message)));
  });
  document.querySelectorAll(".diagnostic-delete").forEach((button) => {
    button.addEventListener("click", () => deleteDiagnosticTarget(button.dataset.target).catch((error) => alert(error.message)));
  });
}

function issueHtml(issue) {
  const target = issue.target || "";
  const targetLabel = issue.target_label || target || "未指定位置";
  const detailId = safeDomId(`diagnostic-detail-${issue.fingerprint || Math.random().toString(16).slice(2)}`);
  const hasDetails = hasDiagnosticDetails(issue);
  const highlightTargets = diagnosticTargets(issue).join("|");
  const actionType = issue.action_type || "";
  const involvedTargets = (issue.involved_targets || []).join("|");
  return `
    <div class="issue-row ${escapeHtml(issue.severity || "warning")}">
      <div class="issue-main">
        ${hasDetails
          ? `<button class="link-button diagnostic-title-toggle diagnostic-toggle" data-target="${escapeHtml(detailId)}"><strong>${escapeHtml(issue.title || "问题")}</strong><small>点击查看具体原因</small></button>`
          : `<strong>${escapeHtml(issue.title || "问题")}</strong>`}
        <span>${escapeHtml(targetLabel)}</span>
        <p>${escapeHtml(issue.detail || "")}</p>
        ${hasDetails ? diagnosticDetailHtml(issue, detailId) : ""}
      </div>
      <div class="inline-actions vertical-actions">
        ${hasDetails ? `<button class="secondary-button diagnostic-toggle" data-target="${detailId}">展开详情</button>` : ""}
        ${target ? `<button class="secondary-button diagnostic-open" data-target="${escapeHtml(target)}" data-targets="${escapeHtml(highlightTargets)}">查看位置</button>` : ""}
        ${actionType === "merge" && involvedTargets ? `<button class="primary-button diagnostic-merge" data-targets="${escapeHtml(involvedTargets)}">合并</button>` : ""}
        ${actionType === "delete" && target ? `<button class="danger-button diagnostic-delete" data-target="${escapeHtml(target)}">删除</button>` : ""}
        ${issue.code === "knowledge_token_budget_large" ? `<button class="secondary-button diagnostic-clear-notices">彻底消去提示</button>` : ""}
        ${issue.fingerprint ? `<button class="secondary-button diagnostic-ignore" data-fingerprint="${escapeHtml(issue.fingerprint)}">标记忽略</button>` : ""}
      </div>
    </div>
  `;
}

function diagnosticStatusTitle(status, issues) {
  if (issues?.length) return "需要关注";
  if (status === "ok") return "检测通过";
  if (status === "warning") return "需要关注";
  if (status === "error") return "检测异常";
  return statusText(status);
}

function diagnosticSummaryText(payload, issues) {
  if (payload.message) return payload.message;
  if (issues?.length) return `发现 ${issues.length} 个需要关注的问题，下面可以展开详情或直接跳到对应知识。`;
  return "检测完成，未发现需要处理的问题。";
}

function diagnosticTargets(issue) {
  const targets = new Set();
  if (issue.target) targets.add(String(issue.target));
  for (const detail of issue.details || []) {
    const value = String(detail.value || "");
    const match = value.match(/([A-Za-z0-9_-]+\/[A-Za-z0-9_.-]+)/);
    if (match) targets.add(match[1]);
  }
  return Array.from(targets);
}

function parseDiagnosticTargets(value) {
  return String(value || "").split("|").map((item) => item.trim()).filter(Boolean);
}

function diagnosticTargetMatches(categoryId, itemId) {
  const target = `${categoryId || ""}/${itemId || ""}`;
  return Boolean(state.diagnosticHighlight?.targets?.includes(target));
}

function safeDomId(value) {
  return String(value || "diagnostic-detail").replace(/[^A-Za-z0-9_-]/g, "-");
}

function hasDiagnosticDetails(issue) {
  return Boolean((issue.details || []).length || (issue.suggestions || []).length || issue.code || issue.fingerprint);
}

function diagnosticDetailHtml(issue, detailId) {
  const details = issue.details || [];
  const suggestions = issue.suggestions || [];
  return `
    <div class="issue-detail-panel is-hidden" id="${detailId}">
      ${issue.code ? `<div class="issue-meta"><span>检测类型</span><strong>${escapeHtml(issue.code)}</strong></div>` : ""}
      ${issue.fingerprint ? `<div class="issue-meta"><span>问题指纹</span><strong>${escapeHtml(issue.fingerprint)}</strong></div>` : ""}
      ${details.length ? `
        <div class="issue-detail-grid">
          ${details.map((item) => `
            <div class="issue-detail-item ${escapeHtml(item.level || "normal")}">
              <span>${escapeHtml(item.label || "详情")}</span>
              <strong>${escapeHtml(item.value ?? "")}</strong>
            </div>
          `).join("")}
        </div>
      ` : ""}
      ${suggestions.length ? `
        <div class="issue-suggestions">
          ${suggestions.map((item) => `
            <div class="issue-suggestion ${escapeHtml(item.level || "normal")}">
              <strong>${escapeHtml(item.title || "建议")}</strong>
              <p>${escapeHtml(item.detail || "")}</p>
            </div>
          `).join("")}
        </div>
      ` : ""}
    </div>
  `;
}

function toggleDiagnosticDetails(button) {
  const id = button.dataset.target;
  const panel = id ? document.getElementById(id) : null;
  if (!panel) return;
  const hidden = panel.classList.toggle("is-hidden");
  button.textContent = hidden ? "展开详情" : "收起详情";
}

async function openDiagnosticTarget(target, highlightTargets = "") {
  if (!target) return;
  if (target.startsWith("rag_exp_")) {
    state.diagnosticHighlight = {targets: parseDiagnosticTargets(highlightTargets || target)};
    selectView("rag_experiences", {keepDiagnosticHighlight: true});
    await loadRagExperiences();
    return;
  }
  const [categoryId, itemId] = String(target).split("/");
  if (!categoryId) return;
  state.diagnosticHighlight = {targets: parseDiagnosticTargets(highlightTargets || target)};
  selectView("knowledge", {keepDiagnosticHighlight: true});
  if (!state.categories.length) {
    await loadKnowledge();
  }
  if (!state.categories.some((category) => category.id === categoryId)) return;
  state.activeCategoryId = categoryId;
  renderCategorySelect();
  await loadCategoryItems();
  if (itemId) {
    const item = state.categoryItems.find((entry) => entry.id === itemId);
    if (item) {
      state.selectedKnowledge = item;
      state.knowledgeMode = "view";
      renderKnowledgeList();
      renderKnowledgeDetail();
    }
  }
}

async function ignoreDiagnosticIssue(fingerprint) {
  if (!fingerprint) return;
  const reason = prompt("忽略原因", "确认该问题可接受") || "ignored";
  await apiJson("/api/diagnostics/ignore", {method: "POST", body: JSON.stringify({fingerprint, reason})});
  await runDiagnostics("quick");
}

async function applyDiagnosticRepair(runId) {
  if (!runId) return;
  const payload = await apiJson(`/api/diagnostics/runs/${encodeURIComponent(runId)}/apply-suggestion`, {method: "POST", body: JSON.stringify({source: "admin_console"})});
  renderDiagnostics(payload);
}

async function clearDiagnosticNotices() {
  const payload = await apiJson("/api/diagnostics/clear-notices", {method: "POST", body: JSON.stringify({code: "knowledge_token_budget_large"})});
  renderDiagnostics(payload);
}

async function deleteDiagnosticTarget(target) {
  if (!target) return;
  const label = target.startsWith("rag_exp_") ? `AI经验池 ${target}` : `知识条目 ${target}`;
  if (!confirm(`确定要删除/归档 ${label} 吗？`)) return;
  const payload = await apiJson("/api/diagnostics/delete-target", {method: "POST", body: JSON.stringify({target})});
  if (payload.ok) {
    alert(payload.message || "已删除");
    await runDiagnostics("quick");
  } else {
    alert(payload.message || "删除失败");
  }
}

async function openDiagnosticMerge(targetsString) {
  const targets = String(targetsString || "").split("|").map((t) => t.trim()).filter(Boolean);
  if (targets.length < 2) return;
  const primaryTarget = targets[0];
  const secondaryTarget = targets[1];

  // Load items
  const primaryItem = await _loadMergeItem(primaryTarget);
  const secondaryItem = await _loadMergeItem(secondaryTarget);
  if (!primaryItem || !secondaryItem) {
    alert("无法加载合并所需的条目数据");
    return;
  }

  // Show modal
  const modal = document.getElementById("diagnostic-merge-modal");
  if (!modal) return;

  document.getElementById("merge-primary-panel").innerHTML = _mergeItemHtml(primaryItem, "primary");
  document.getElementById("merge-secondary-panel").innerHTML = _mergeItemHtml(secondaryItem, "secondary");
  document.getElementById("merge-result-preview").innerHTML = `<pre class="merge-preview-json">${escapeHtml(JSON.stringify(primaryItem.data || {}, null, 2))}</pre>`;

  modal.classList.remove("is-hidden");

  // Setup confirm handler
  const confirmBtn = document.getElementById("confirm-merge");
  const cancelBtn = document.getElementById("cancel-merge");

  const onConfirm = async () => {
    const mergedData = _buildMergedData(primaryItem, secondaryItem);
    const payload = await apiJson("/api/diagnostics/merge-knowledge", {
      method: "POST",
      body: JSON.stringify({
        primary_target: primaryTarget,
        secondary_targets: [secondaryTarget],
        merged_data: mergedData,
      }),
    });
    modal.classList.add("is-hidden");
    confirmBtn.removeEventListener("click", onConfirm);
    cancelBtn.removeEventListener("click", onCancel);
    if (payload.ok) {
      alert(payload.message || "合并成功");
      await runDiagnostics("quick");
    } else {
      alert(payload.message || "合并失败");
    }
  };

  const onCancel = () => {
    modal.classList.add("is-hidden");
    confirmBtn.removeEventListener("click", onConfirm);
    cancelBtn.removeEventListener("click", onCancel);
  };

  confirmBtn.addEventListener("click", onConfirm);
  cancelBtn.addEventListener("click", onCancel);
}

async function _loadMergeItem(target) {
  if (target.startsWith("rag_exp_")) {
    // AI experience pool items are not mergeable through this flow.
    return null;
  }
  const parts = target.split("/");
  if (parts.length !== 2) return null;
  const [categoryId, itemId] = parts;
  try {
    const payload = await apiGet(`/api/knowledge/categories/${encodeURIComponent(categoryId)}/items/${encodeURIComponent(itemId)}`);
    return payload.item || null;
  } catch {
    return null;
  }
}

function _mergeItemHtml(item, side) {
  const data = item.data || {};
  const label = side === "primary" ? "主条目（保留）" : "次条目（将归档）";
  const rows = Object.entries(data).map(([key, value]) => {
    const strVal = typeof value === "object" ? JSON.stringify(value) : String(value ?? "");
    return `<tr><td>${escapeHtml(key)}</td><td>${escapeHtml(strVal)}</td></tr>`;
  }).join("");
  return `
    <div class="merge-item-panel">
      <h4>${escapeHtml(label)}</h4>
      <table class="merge-item-table">${rows || `<tr><td colspan="2">无数据</td></tr>`}</table>
    </div>
  `;
}

function _buildMergedData(primaryItem, secondaryItem) {
  // Simple merge: prefer primary data, but for text fields, concatenate if different
  const primaryData = {...(primaryItem.data || {})};
  const secondaryData = secondaryItem.data || {};
  for (const [key, value] of Object.entries(secondaryData)) {
    if (value === undefined || value === null || value === "") continue;
    if (!(key in primaryData) || primaryData[key] === "" || primaryData[key] === null) {
      primaryData[key] = value;
    } else if (typeof value === "string" && typeof primaryData[key] === "string" && primaryData[key] !== value) {
      primaryData[key] = `${primaryData[key]}\n\n（合并内容）\n${value}`;
    }
  }
  return primaryData;
}

async function loadVersions() {
  const payload = await apiGet("/api/versions");
  const items = (payload.items || []).slice(0, 20);
  document.getElementById("version-list").innerHTML = items
    .map((item) => `
      <div class="record-row version-row">
        <button class="link-button version-select" data-id="${escapeHtml(item.version_id)}">
          <strong>${escapeHtml(item.reason)}</strong>
          <span>${escapeHtml(item.version_id)} · ${escapeHtml(item.created_at)}</span>
        </button>
        <div class="inline-actions">
          <button class="secondary-button version-download" data-id="${escapeHtml(item.version_id)}">下载</button>
          <button class="secondary-button version-rollback" data-id="${escapeHtml(item.version_id)}">还原</button>
        </div>
      </div>
    `)
    .join("") || `<div class="empty-state">暂无备份快照</div>`;
  document.querySelectorAll(".version-rollback").forEach((button) => {
    button.addEventListener("click", () => rollbackVersion(button.dataset.id).catch((error) => alert(error.message)));
  });
  document.querySelectorAll(".version-download").forEach((button) => {
    button.addEventListener("click", () => downloadVersion(button.dataset.id).catch((error) => alert(error.message)));
  });
}

async function createBackup() {
  if (!confirm("确认立即备份当前知识库状态吗？")) return;
  await apiJson("/api/versions", {method: "POST", body: JSON.stringify({reason: "manual backup from admin console"})});
  await loadVersions();
}

async function downloadVersion(versionId) {
  if (!versionId) return;
  const response = await fetch(`/api/versions/${encodeURIComponent(versionId)}/download`, {headers: apiHeaders()});
  if (!response.ok) throw new Error(await responseErrorMessage(response, "/api/versions/download"));
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${versionId}_complete_backup.zip`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function rollbackVersion(versionId) {
  if (!versionId) return;
  if (!confirm(`确认还原到版本 ${versionId}？当前知识会先自动备份。`)) return;
  const payload = await apiJson(`/api/versions/${encodeURIComponent(versionId)}/rollback`, {method: "POST"});
  renderDiagnostics(payload);
  selectView("diagnostics");
  await Promise.all([loadOverview(), loadKnowledge(), loadVersions()]);
}

async function loadWorkflowGovernance() {
  const gateText = JSON.stringify(WORKFLOW_DEFAULT_METRICS_GATE, null, 2);
  ["wf-eval-metrics-gate", "wf-release-metrics-gate"].forEach((id) => {
    const element = document.getElementById(id);
    if (element && !String(element.value || "").trim()) element.value = gateText;
  });
  if (!state.workflowLastResult) return;
  workflowShowResult("最近一次执行结果", state.workflowLastResult, {preservePayload: true});
}

function resetWorkflowResult() {
  state.workflowLastResult = null;
  const summary = document.getElementById("wf-result-summary");
  const jsonPanel = document.getElementById("wf-result-json");
  const action = document.getElementById("wf-last-action");
  if (summary) {
    summary.innerHTML = '<div class="status-card info"><strong>暂无结果</strong><span>执行任一步后会自动填充 job_id/release_id 并展示摘要。</span></div>';
  }
  if (jsonPanel) jsonPanel.textContent = "等待执行结果";
  if (action) action.textContent = "等待操作";
}

function workflowShowResult(actionLabel, payload, options = {}) {
  if (!options.preservePayload) state.workflowLastResult = payload;
  const subject = workflowSubject(payload);
  workflowFillFieldsFromResult(subject);

  const summary = document.getElementById("wf-result-summary");
  const jsonPanel = document.getElementById("wf-result-json");
  const action = document.getElementById("wf-last-action");
  if (action) action.textContent = `${actionLabel} · ${new Date().toLocaleString("zh-CN")}`;
  if (jsonPanel) jsonPanel.textContent = JSON.stringify(payload, null, 2);
  if (!summary) return;

  const cards = workflowSummaryCards(actionLabel, payload, subject);
  summary.innerHTML = cards.map((card) => `
      <div class="status-card ${escapeHtml(card.tone || "info")}">
        <strong>${escapeHtml(card.title)}</strong>
        <span>${escapeHtml(card.detail)}</span>
      </div>
    `).join("");
}

function workflowSummaryCards(actionLabel, payload, subject) {
  const isOk = payload?.ok !== false;
  const gatePass = payload?.gate_pass;
  const tone = isOk ? (gatePass === false ? "warning" : "ok") : "error";
  const cards = [
    {
      tone,
      title: actionLabel,
      detail: workflowPrimaryDetail(payload, subject),
    },
  ];

  if (payload?.message) {
    cards.push({tone: isOk ? "warning" : "error", title: "接口信息", detail: String(payload.message)});
  }

  if (subject?.summary && typeof subject.summary === "object") {
    cards.push({tone: "info", title: "摘要", detail: workflowObjectSummary(subject.summary)});
  }
  if (payload?.metrics && typeof payload.metrics === "object") {
    cards.push({tone: gatePass === false ? "warning" : "ok", title: "评估指标", detail: workflowObjectSummary(payload.metrics)});
  }
  if (payload?.gate_result && typeof payload.gate_result === "object") {
    cards.push({tone: gatePass === false ? "warning" : "ok", title: "门禁结果", detail: workflowObjectSummary(payload.gate_result)});
  }

  const blockedItems = Array.isArray(subject?.blocked_items) ? subject.blocked_items : [];
  if (blockedItems.length) {
    cards.push({tone: "warning", title: "阻断项", detail: `${blockedItems.length} 条（详情见下方 JSON）`});
  }

  return cards.slice(0, 6);
}

function workflowPrimaryDetail(payload, subject) {
  const id = workflowResultId(subject, payload);
  const status = String(subject?.status || payload?.status || (payload?.ok === false ? "failed" : "completed"));
  const tenant = String(subject?.tenant_id || payload?.tenant_id || workflowTenantId());
  const releaseVersion = String(subject?.release_version || payload?.release_version || "");
  const parts = [`状态 ${status}`, `租户 ${tenant}`];
  if (id) parts.push(`ID ${id}`);
  if (releaseVersion) parts.push(`版本 ${releaseVersion}`);
  return parts.join(" · ");
}

function workflowResultId(subject, payload) {
  const item = subject || {};
  return String(item.job_id || item.eval_job_id || item.report_id || item.release_id || payload?.release_id || "").trim();
}

function workflowObjectSummary(payload, maxFields = 8) {
  const entries = Object.entries(payload || {}).slice(0, maxFields);
  const chunks = entries.map(([key, value]) => `${key}: ${workflowSummaryValue(value)}`);
  if (Object.keys(payload || {}).length > maxFields) chunks.push("...");
  return chunks.join("；") || "无";
}

function workflowSummaryValue(value) {
  if (value === null || value === undefined || value === "") return "空";
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return `${value.length} 项`;
  if (typeof value === "object") return "对象";
  return shortBusinessText(String(value), 80);
}

function workflowSubject(payload) {
  if (!payload || typeof payload !== "object") return {};
  if (payload.item && typeof payload.item === "object") return payload.item;
  if (payload.release && typeof payload.release === "object") return payload.release;
  return payload;
}

function workflowFillFieldsFromResult(subject) {
  if (!subject || typeof subject !== "object") return;
  if (subject.curated_file) workflowSetInputValue("wf-import-input-file", subject.curated_file);
  if (subject.job_id && String(subject.job_id).startsWith("curate_job_")) workflowSetInputValue("wf-curation-job-id", subject.job_id);
  if (subject.job_id && String(subject.job_id).startsWith("import_job_")) workflowSetInputValue("wf-import-job-id", subject.job_id);
  if (subject.source_dry_run_job_id) workflowSetInputValue("wf-import-job-id", subject.source_dry_run_job_id);
  if (subject.eval_job_id) workflowSetInputValue("wf-eval-job-id", subject.eval_job_id);
  if (subject.release_id) workflowSetInputValue("wf-release-id", subject.release_id);
  if (Array.isArray(subject.import_job_ids) && subject.import_job_ids.length) {
    workflowSetInputValue("wf-release-import-job-ids", subject.import_job_ids.join("\n"));
  }
  const releaseVersion = String(subject.release_version || "");
  if (releaseVersion) {
    workflowSetInputValue("wf-import-release-version", releaseVersion);
    workflowSetInputValue("wf-eval-release-version", releaseVersion);
    workflowSetInputValue("wf-release-version", releaseVersion);
  }
  const rollbackTo = String(subject.rollback_to || subject.rolled_back_to || "");
  if (rollbackTo) workflowSetInputValue("wf-release-rollback-to", rollbackTo);
}

function workflowSetInputValue(id, value) {
  const element = document.getElementById(id);
  if (!element || value === undefined || value === null) return;
  element.value = String(value);
}

function workflowSetBusy(busy) {
  state.workflowOpsBusy = Boolean(busy);
  WORKFLOW_ACTION_BUTTON_IDS.forEach((id) => {
    const button = document.getElementById(id);
    if (button) button.disabled = Boolean(busy);
  });
}

function workflowTenantId() {
  return String(state.activeTenantId || "default").trim() || "default";
}

function workflowInputValue(id) {
  const element = document.getElementById(id);
  return String(element?.value || "").trim();
}

function workflowChecked(id) {
  const element = document.getElementById(id);
  return Boolean(element?.checked);
}

function workflowListInput(id) {
  const raw = workflowInputValue(id);
  if (!raw) return [];
  return Array.from(new Set(raw.split(/[\r\n,，;；]+/).map((item) => item.trim()).filter(Boolean)));
}

function workflowJsonInput(id, label) {
  const text = workflowInputValue(id);
  if (!text) return {};
  let payload;
  try {
    payload = JSON.parse(text);
  } catch (error) {
    throw new Error(`${label} 不是合法 JSON：${error.message}`);
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error(`${label} 必须是 JSON 对象`);
  }
  return payload;
}

async function workflowRunAction(actionLabel, fn) {
  if (state.workflowOpsBusy) return null;
  workflowSetBusy(true);
  try {
    const payload = await fn();
    workflowShowResult(actionLabel, payload);
    return payload;
  } catch (error) {
    workflowShowResult(`${actionLabel}失败`, {ok: false, status: "failed", message: error.message || "未知错误"});
    throw error;
  } finally {
    workflowSetBusy(false);
  }
}

async function runWorkflowCuration() {
  const batchId = workflowInputValue("wf-curation-batch-id");
  const sourceFiles = workflowListInput("wf-curation-source-files");
  if (!batchId) throw new Error("请先填写批次 ID（batch_id）");
  if (!sourceFiles.length) throw new Error("请至少填写一个源文件路径");
  return workflowRunAction("数据治理完成", async () => apiJson("/api/workflow/curation/jobs", {
    method: "POST",
    body: JSON.stringify({
      tenant_id: workflowTenantId(),
      industry_id: workflowInputValue("wf-curation-industry-id"),
      batch_id: batchId,
      source_files: sourceFiles,
      strict_mode: workflowChecked("wf-curation-strict-mode"),
    }),
  }));
}

async function fetchWorkflowCurationJob() {
  const jobId = workflowInputValue("wf-curation-job-id");
  if (!jobId) throw new Error("请先填写清洗任务 ID");
  return workflowRunAction("清洗任务查询完成", async () => apiGet(`/api/workflow/curation/jobs/${encodeURIComponent(jobId)}`));
}

async function runWorkflowImportDryRun() {
  const inputFile = workflowInputValue("wf-import-input-file");
  if (!inputFile) throw new Error("请先填写模板文件路径");
  return workflowRunAction("Dry-run 完成", async () => apiJson("/api/workflow/template-import/dry-run", {
    method: "POST",
    body: JSON.stringify({
      tenant_id: workflowTenantId(),
      industry_id: workflowInputValue("wf-import-industry-id"),
      input_file: inputFile,
    }),
  }));
}

async function fetchWorkflowImportJob() {
  const jobId = workflowInputValue("wf-import-job-id");
  if (!jobId) throw new Error("请先填写导入任务 ID");
  return workflowRunAction("导入任务查询完成", async () => apiGet(`/api/workflow/template-import/jobs/${encodeURIComponent(jobId)}`));
}

async function runWorkflowImportApply() {
  const dryRunJobId = workflowInputValue("wf-import-job-id");
  const inputFile = workflowInputValue("wf-import-input-file");
  if (!dryRunJobId && !inputFile) throw new Error("请填写 Dry-run 任务 ID，或提供 input_file 让系统自动 dry-run 后 apply");
  return workflowRunAction("Apply 导入完成", async () => apiJson("/api/workflow/template-import/apply", {
    method: "POST",
    body: JSON.stringify({
      tenant_id: workflowTenantId(),
      industry_id: workflowInputValue("wf-import-industry-id"),
      dry_run_job_id: dryRunJobId,
      input_file: inputFile,
      release_version: workflowInputValue("wf-import-release-version"),
    }),
  }));
}

async function runWorkflowReplayEval() {
  const releaseVersion = workflowInputValue("wf-eval-release-version");
  if (!releaseVersion) throw new Error("请先填写发布版本（release_version）");
  return workflowRunAction("回放评估完成", async () => apiJson("/api/workflow/replay-eval/run", {
    method: "POST",
    body: JSON.stringify({
      tenant_id: workflowTenantId(),
      release_version: releaseVersion,
      suite_id: workflowInputValue("wf-eval-suite-id"),
      suite_file: workflowInputValue("wf-eval-suite-file"),
      metrics_gate: workflowJsonInput("wf-eval-metrics-gate", "评估门禁"),
    }),
  }));
}

async function fetchWorkflowEvalJob() {
  const evalJobId = workflowInputValue("wf-eval-job-id");
  if (!evalJobId) throw new Error("请先填写评估任务 ID");
  return workflowRunAction("评估任务查询完成", async () => apiGet(`/api/workflow/replay-eval/jobs/${encodeURIComponent(evalJobId)}`));
}

async function runWorkflowReleaseCreate() {
  const importJobIds = workflowListInput("wf-release-import-job-ids");
  if (!importJobIds.length) throw new Error("请至少填写一个导入任务 ID");
  return workflowRunAction("发布候选创建完成", async () => apiJson("/api/workflow/releases", {
    method: "POST",
    body: JSON.stringify({
      tenant_id: workflowTenantId(),
      release_version: workflowInputValue("wf-release-version"),
      industry_id: workflowInputValue("wf-release-industry-id"),
      import_job_ids: importJobIds,
      feature_flags: workflowJsonInput("wf-release-feature-flags", "功能开关"),
      metrics_gate: workflowJsonInput("wf-release-metrics-gate", "发布门禁"),
    }),
  }));
}

async function fetchWorkflowRelease() {
  const releaseId = workflowInputValue("wf-release-id");
  if (!releaseId) throw new Error("请先填写发布 ID");
  return workflowRunAction("发布查询完成", async () => apiGet(`/api/workflow/releases/${encodeURIComponent(releaseId)}`));
}

async function runWorkflowReleaseApprove() {
  const releaseId = workflowInputValue("wf-release-id");
  if (!releaseId) throw new Error("请先填写发布 ID");
  return workflowRunAction("发布审批完成", async () => apiJson(`/api/workflow/releases/${encodeURIComponent(releaseId)}/approve`, {
    method: "POST",
    body: JSON.stringify({
      approved_by: workflowInputValue("wf-release-approved-by") || "admin_console",
      approval_note: workflowInputValue("wf-release-approval-note"),
    }),
  }));
}

async function runWorkflowReleaseRollback() {
  const releaseId = workflowInputValue("wf-release-id");
  if (!releaseId) throw new Error("请先填写发布 ID");
  if (!confirm(`确认回滚发布 ${releaseId} 吗？`)) return null;
  return workflowRunAction("发布回滚完成", async () => apiJson(`/api/workflow/releases/${encodeURIComponent(releaseId)}/rollback`, {
    method: "POST",
    body: JSON.stringify({
      rollback_to_version: workflowInputValue("wf-release-rollback-to"),
      reason: workflowInputValue("wf-release-rollback-reason"),
    }),
  }));
}

function statusText(status) {
  if (status === "ok") return "检测通过";
  if (status === "warning") return "需要关注";
  if (status === "error") return "发现故障";
  return status || "完成";
}

function summaryFields(fields) {
  return Object.entries(fields)
    .map(([key, value]) => `${fieldLabel({id: key, label: key})}: ${displayBusinessValue(value)}`)
    .join("；") || "无";
}

function displayBusinessValue(value) {
  if (isEmpty(value)) return "";
  if (Array.isArray(value)) {
    if (value.every((item) => item && typeof item === "object")) {
      return value
        .map((item) => Object.entries(item)
          .filter(([, inner]) => !isEmpty(inner))
          .map(([key, inner]) => `${fieldLabel({id: key, label: key})}: ${displayBusinessValue(inner)}`)
          .join("，"))
        .filter(Boolean)
        .join("；");
    }
    return value.map(displayBusinessValue).filter(Boolean).join("、");
  }
  if (typeof value === "object") {
    return Object.entries(value)
      .filter(([, inner]) => !isEmpty(inner))
      .map(([key, inner]) => `${templateLabels[key] || fieldLabel({id: key, label: key})}: ${displayBusinessValue(inner)}`)
      .join("；");
  }
  return String(value);
}

function optionLabel(fieldId, value) {
  return optionLabels[fieldId]?.[value] || value || "";
}

function fieldLabel(field) {
  return fieldLabelOverrides[field.id] || field.label || field.id;
}

function displayTags(value) {
  return Array.isArray(value) ? value.join("\n") : value || "";
}

function splitTags(value) {
  return value ? value.split(/[,，、\n]+/).map((item) => item.trim()).filter(Boolean) : [];
}

function numberOrNull(value) {
  const text = String(value || "").trim();
  if (!text) return null;
  const number = Number(text);
  return Number.isFinite(number) ? number : null;
}

function defaultFieldValue(field) {
  if (field.default !== undefined) return field.default;
  if (field.type === "tags" || field.type === "table") return [];
  if (field.type === "object") return {};
  if (field.type === "boolean") return false;
  return "";
}

function businessSearchText(data) {
  return Object.values(data).map(displayBusinessValue).join(" ");
}

function isEmpty(value) {
  return value === null || value === undefined || value === "" || (Array.isArray(value) && !value.length) || (typeof value === "object" && !Array.isArray(value) && !Object.keys(value).length);
}

function setHidden(id, hidden) {
  const element = document.getElementById(id);
  if (element) element.classList.toggle("is-hidden", hidden);
}

function setChecked(id, checked) {
  const element = document.getElementById(id);
  if (element) element.checked = Boolean(checked);
}

function cssEscape(value) {
  if (window.CSS && CSS.escape) return CSS.escape(value);
  return String(value).replaceAll('"', '\\"');
}

function badgeListHtml(badges) {
  const items = Array.isArray(badges) ? badges : [];
  if (!items.length) return "";
  return `
    <div class="badge-list">
      ${items.map((item) => `<span class="badge ${escapeHtml(item.tone || "muted")}">${escapeHtml(item.label || item.key || "")}</span>`).join("")}
    </div>
  `;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function acknowledgeKnowledgeItem() {
  if (!state.selectedKnowledge?.id || !state.activeCategoryId) return;
  const selectedId = state.selectedKnowledge.id;
  await apiJson(`/api/knowledge/categories/${encodeURIComponent(state.activeCategoryId)}/items/${encodeURIComponent(state.selectedKnowledge.id)}/acknowledge`, {
    method: "POST",
    body: "{}",
  });
  if (state.selectedKnowledge?.id === selectedId) {
    state.selectedKnowledge = {
      ...state.selectedKnowledge,
      review_state: {
        ...(state.selectedKnowledge.review_state || {}),
        is_new: false,
        read_at: new Date().toISOString(),
      },
    };
    renderKnowledgeList();
    renderKnowledgeDetail();
  }
  await Promise.all([loadCategoryItems(), loadOverview()]);
}

async function acknowledgeProductItem() {
  const selectedId = state.selectedProduct?.id || "";
  if (!selectedId || state.productAcknowledgeLoadingIds.has(selectedId)) return;
  state.productAcknowledgeLoadingIds.add(selectedId);
  renderProductCatalogDetail();
  try {
    await apiJson(`/api/knowledge/categories/products/items/${encodeURIComponent(selectedId)}/acknowledge`, {
      method: "POST",
      body: "{}",
    });
    if (state.selectedProduct?.id === selectedId) {
      state.selectedProduct = {
        ...state.selectedProduct,
        is_unread: false,
        runtime_usable: state.selectedProduct.status !== "archived",
        review_state: {
          ...(state.selectedProduct.review_state || {}),
          is_new: false,
          read_at: new Date().toISOString(),
        },
      };
      renderProductCatalogList();
      renderProductCatalogDetail();
    }
    await Promise.all([loadProductCatalog({loadDetail: false}), loadOverview().catch(() => {})]);
    if (state.selectedProduct?.id === selectedId) {
      await loadProductDetail(selectedId, {open: false});
    }
  } finally {
    state.productAcknowledgeLoadingIds.delete(selectedId);
    renderProductCatalogList();
    renderProductCatalogDetail();
  }
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("'", "&#39;");
}

async function changeLocalPassword(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  if (state.passwordChallenge) {
    await apiJson("/api/auth/change-password/verify", {
      method: "POST",
      body: JSON.stringify({challenge_id: state.passwordChallenge.challenge_id, code: form.get("email_code")}),
    });
    state.passwordChallenge = null;
    formElement.reset();
    document.getElementById("local-password-code-field")?.classList.add("is-hidden");
    formElement.querySelector("button[type='submit']").textContent = "发送验证码并修改";
    alert("密码已修改，请用新密码重新登录。");
    await logoutLocal();
    return;
  }
  if (form.get("new_password") !== form.get("confirm_password")) {
    alert("两次输入的新密码不一致。");
    return;
  }
  const result = await apiJson("/api/auth/change-password/start", {
    method: "POST",
    body: JSON.stringify({
      current_password: form.get("current_password"),
      new_password: form.get("new_password"),
    }),
  });
  state.passwordChallenge = {challenge_id: result.challenge_id};
  document.getElementById("local-password-code-field")?.classList.remove("is-hidden");
  formElement.querySelector("button[type='submit']").textContent = "验证并保存新密码";
  alert(
    result.debug_code
      ? `验证码已生成：${result.debug_code}。生产环境会发送到 ${result.masked_email || "绑定邮箱"}。`
      : `验证码已发送到 ${result.masked_email || "绑定邮箱"}，请输入后保存新密码。`
  );
}

async function bindLocalEmail(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  if (state.emailChallenge) {
    const result = await apiJson("/api/auth/email/verify", {
      method: "POST",
      body: JSON.stringify({challenge_id: state.emailChallenge.challenge_id, code: form.get("email_code")}),
    });
    state.emailChallenge = null;
    formElement.reset();
    document.getElementById("local-email-code-field")?.classList.add("is-hidden");
    formElement.querySelector("button[type='submit']").textContent = "发送绑定验证码";
    state.security = {...(state.security || {}), email: result.email, masked_email: result.masked_email};
    renderLocalSecurity();
    alert("邮箱已绑定。");
    return;
  }
  const result = await apiJson("/api/auth/email/start", {
    method: "POST",
    body: JSON.stringify({email: form.get("email")}),
  });
  state.emailChallenge = {challenge_id: result.challenge_id};
  document.getElementById("local-email-code-field")?.classList.remove("is-hidden");
  formElement.querySelector("button[type='submit']").textContent = "验证并绑定邮箱";
  alert(
    result.debug_code
      ? `验证码已生成：${result.debug_code}。生产环境会发送到 ${result.masked_email || "绑定邮箱"}。`
      : `验证码已发送到 ${result.masked_email || "绑定邮箱"}，请输入后完成绑定。`
  );
}

function emptyPanel(text) {
  return `<div class="empty-state">${escapeHtml(text)}</div>`;
}

function bindNavigation() {
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.addEventListener("click", () => {
      if (window.location.hash !== `#${item.dataset.view}`) {
        window.location.hash = item.dataset.view;
      }
      selectView(item.dataset.view);
      loadViewData(item.dataset.view).catch(console.error);
    });
  });
  document.querySelectorAll(".nav-shortcut").forEach((item) => {
    item.addEventListener("click", () => {
      const view = item.dataset.view;
      if (!view) return;
      window.location.hash = view;
      selectView(view);
      loadViewData(view).catch(console.error);
    });
  });
  document.querySelectorAll(".workflow-tab").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.group === "intake") {
        if (button.dataset.tab === "rag_experiences") {
          selectView("rag_experiences");
          loadViewData("rag_experiences").catch(console.error);
          return;
        }
        state.activeIntakeTab = button.dataset.tab || "generator";
      }
      if (button.dataset.group === "reference") {
        state.activeReferenceTab = button.dataset.tab || "experiences";
      }
      syncWorkflowTabs();
      loadActiveSubsection().catch(console.error);
    });
  });
}

function activateHashView() {
  const view = window.location.hash.replace("#", "");
  if (!titles[view] && !viewAliases[view]) return;
  selectView(view);
  loadViewData(view).catch(console.error);
}

async function loadViewData(view) {
  const activeView = (viewAliases[view] || {view}).view;
  if (activeView === "customer_service") await loadCustomerService();
  if (activeView === "knowledge_center") await loadOverview();
  if (activeView === "product_catalog") await loadProductCatalog();
  if (activeView === "knowledge") await loadKnowledge();
  if (activeView === "intake") {
    renderGeneratorCategorySelect();
    renderGenerator();
    await Promise.all([loadUploads().catch(console.error), loadCandidates().catch(console.error)]);
  }
  if (activeView === "recorder") await loadRecorder();
  if (activeView === "ai_reference") {
    await Promise.all([loadRagStatus().catch(console.error), loadRagExperiences().catch(console.error)]);
  }
  if (activeView === "settings") {
    await Promise.all([
      loadVersions().catch(console.error),
      refreshAccountContext().catch(console.error),
      loadLlmConfig().catch(console.error),
      loadFeishuConfig().catch(console.error),
    ]);
  }
  if (activeView === "versions") await loadVersions();
  if (activeView === "customer_profiles") await loadCustomerProfiles();
  if (activeView === "diagnostics") await loadDiagnosticsData();
}

async function loadActiveSubsection() {
  if (state.activeView === "intake") {
    if (state.activeIntakeTab === "generator") {
      renderGeneratorCategorySelect();
      renderGenerator();
    }
    if (state.activeIntakeTab === "uploads") await loadUploads();
    if (state.activeIntakeTab === "candidates") await loadCandidates();
  }
  if (state.activeView === "ai_reference") {
    if (state.activeReferenceTab === "sources") await loadRagStatus();
    if (state.activeReferenceTab === "experiences") await loadRagExperiences();
  }
  if (state.activeView === "knowledge_center") await loadOverview();
  if (state.activeView === "knowledge") await loadKnowledge();
  if (state.activeView === "recorder") await loadRecorder();
  if (state.activeView === "product_catalog") await loadProductCatalog();
  if (state.activeView === "customer_service") await loadCustomerService();
  if (state.activeView === "customer_profiles") await loadCustomerProfiles();
}

initHelperCardCollapsing();
bindNavigation();
renderCustomerServiceRuntime();
document.getElementById("refresh-overview").addEventListener("click", () => loadOverview().catch(console.error));
document.getElementById("tenant-select")?.addEventListener("change", async (event) => {
  state.activeTenantId = event.target.value || "default";
  localStorage.setItem("localActiveTenantId", state.activeTenantId);
  await Promise.all([
    refreshAccountContext().catch(console.error),
    loadOverview().catch(console.error),
    refreshRagExperienceBadge().catch(console.error),
  ]);
  await syncRecorderModuleBindings().catch((error) => console.warn("tenant switch recorder binding sync failed", error));
  scheduleStartupSync();
  scheduleCustomerServiceRuntimePolling();
  await loadActiveSubsection().catch(console.error);
});
document.getElementById("category-select").addEventListener("change", async (event) => {
  state.activeCategoryId = event.target.value;
  await loadCategoryItems();
});
document.getElementById("knowledge-search").addEventListener("input", () => {
  state.knowledgeListVisibleCount = KNOWLEDGE_LIST_PAGE_SIZE;
  renderKnowledgeList();
  if (state.knowledgeSearchTimer) clearTimeout(state.knowledgeSearchTimer);
  state.knowledgeSearchTimer = setTimeout(() => {
    loadCategoryItems().catch((error) => console.warn("knowledge search reload failed", error));
  }, 250);
});
document.getElementById("create-category").addEventListener("click", () => createCustomCategory().catch((error) => alert(error.message)));
document.getElementById("new-knowledge-item").addEventListener("click", newKnowledgeItem);
document.getElementById("edit-knowledge-item").addEventListener("click", editKnowledgeItem);
document.getElementById("cancel-knowledge-edit").addEventListener("click", cancelKnowledgeEdit);
document.getElementById("save-knowledge-item").addEventListener("click", () => saveKnowledgeItem().catch((error) => alert(error.message)));
document.getElementById("archive-knowledge-item").addEventListener("click", () => archiveKnowledgeItem().catch((error) => alert(error.message)));
document.getElementById("send-generator").addEventListener("click", () => sendGeneratorMessage().catch((error) => alert(error.message)));
document.getElementById("reset-generator").addEventListener("click", resetGenerator);
document.getElementById("confirm-generator").addEventListener("click", () => confirmGenerator().catch((error) => alert(error.message)));
document.getElementById("upload-button").addEventListener("click", () => uploadSelectedFile().catch((error) => alert(error.message)));
document.getElementById("upload-file").addEventListener("change", () => uploadSelectedFile().catch((error) => alert(error.message)));
const uploadAutoLearnCheckbox = document.getElementById("upload-auto-learn");
if (uploadAutoLearnCheckbox) {
  uploadAutoLearnCheckbox.checked = state.autoLearnAfterUpload;
  uploadAutoLearnCheckbox.addEventListener("change", (event) => {
    state.autoLearnAfterUpload = Boolean(event.target.checked);
    localStorage.setItem("uploadAutoLearnAfterSelect", state.autoLearnAfterUpload ? "1" : "0");
  });
}
document.getElementById("refresh-rag").addEventListener("click", () => loadRagStatus().catch((error) => alert(error.message)));
document.getElementById("rebuild-rag").addEventListener("click", () => rebuildRag().catch((error) => alert(error.message)));
document.getElementById("rag-search").addEventListener("click", () => searchRag().catch((error) => alert(error.message)));
document.getElementById("refresh-rag-experiences").addEventListener("click", () => loadRagExperiences().catch((error) => alert(error.message)));
const showDiscardedRagCheckbox = document.getElementById("show-discarded-rag");
if (showDiscardedRagCheckbox) {
  showDiscardedRagCheckbox.checked = state.showDiscardedRagExperiences;
  showDiscardedRagCheckbox.addEventListener("change", (event) => {
    state.showDiscardedRagExperiences = event.target.checked;
    localStorage.setItem("showDiscardedRagExperiences", state.showDiscardedRagExperiences ? "1" : "0");
    loadRagExperiences().catch((error) => alert(error.message));
  });
}
document.getElementById("run-learning").addEventListener("click", () => runLearning().catch((error) => alert(error.message)));
document.getElementById("refresh-candidates")?.addEventListener("click", () => loadCandidates().catch((error) => alert(error.message)));
document.getElementById("refresh-customer-service")?.addEventListener("click", () => loadCustomerService().catch((error) => alert(error.message)));
document.getElementById("customer-save-settings")?.addEventListener("click", () => saveCustomerServiceSettings().catch((error) => alert(error.message)));
document.getElementById("customer-service-discover")?.addEventListener("click", () => discoverCustomerServiceSessions().catch((error) => alert(error.message)));
document.getElementById("customer-service-select-all")?.addEventListener("click", () => applyCustomerServiceSessionSelection("all").catch((error) => alert(error.message)));
document.getElementById("customer-service-clear-selection")?.addEventListener("click", () => applyCustomerServiceSessionSelection("none").catch((error) => alert(error.message)));
document.getElementById("refresh-product-catalog")?.addEventListener("click", () => loadProductCatalog().catch((error) => alert(error.message)));
document.getElementById("run-product-command")?.addEventListener("click", () => runProductCommand().catch((error) => alert(error.message)));
document.getElementById("recorder-discover")?.addEventListener("click", () => discoverRecorderSessions().catch((error) => alert(error.message)));
document.getElementById("recorder-capture")?.addEventListener("click", () => captureRecorderNow().catch((error) => alert(error.message)));
document.getElementById("recorder-save-settings")?.addEventListener("click", () => saveRecorderSettings().catch((error) => alert(error.message)));
document.getElementById("recorder-runtime-start")?.addEventListener("click", () => startRecorderRuntime().catch((error) => alert(error.message)));
document.getElementById("recorder-runtime-stop")?.addEventListener("click", () => stopRecorderRuntime().catch((error) => alert(error.message)));
document.getElementById("recorder-select-groups")?.addEventListener("click", () => applyRecorderSelection("groups").catch((error) => alert(error.message)));
document.getElementById("recorder-clear-selection")?.addEventListener("click", () => applyRecorderSelection("none").catch((error) => alert(error.message)));
document.getElementById("recorder-export-run-create")?.addEventListener("click", () => createRecorderExportRun({mode: "all"}).catch((error) => alert(error.message)));
document.getElementById("recorder-export-run-day")?.addEventListener("click", () => createRecorderExportRun({preset: "day"}).catch((error) => alert(error.message)));
document.getElementById("recorder-export-run-week")?.addEventListener("click", () => createRecorderExportRun({preset: "week"}).catch((error) => alert(error.message)));
document.getElementById("recorder-export-run-month")?.addEventListener("click", () => createRecorderExportRun({preset: "month"}).catch((error) => alert(error.message)));
document.getElementById("recorder-export-run-custom")?.addEventListener("click", () => createRecorderExportRun({mode: "custom"}).catch((error) => alert(error.message)));
document.getElementById("recorder-export-date-clear")?.addEventListener("click", clearRecorderExportDateRange);
document.getElementById("export-knowledge-type")?.addEventListener("click", () => downloadKnowledgeExport("type").catch((error) => alert(error.message)));
document.getElementById("export-knowledge-time")?.addEventListener("click", () => downloadKnowledgeExport("time").catch((error) => alert(error.message)));
document.getElementById("quick-diagnostics").addEventListener("click", () => runDiagnostics("quick").catch((error) => alert(error.message)));
document.getElementById("full-diagnostics").addEventListener("click", () => runDiagnostics("full").catch((error) => alert(error.message)));
document.getElementById("create-backup").addEventListener("click", () => createBackup().catch((error) => alert(error.message)));
document.getElementById("refresh-versions").addEventListener("click", () => loadVersions().catch((error) => alert(error.message)));
document.getElementById("llm-config-form")?.addEventListener("submit", (event) => saveLlmConfig(event).catch((error) => alert(error.message)));
document.getElementById("llm-config-test")?.addEventListener("click", () => testLlmConfig("flash", "primary").catch((error) => alert(error.message)));
document.getElementById("llm-config-test-pro")?.addEventListener("click", () => testLlmConfig("pro", "primary").catch((error) => alert(error.message)));
document.getElementById("llm-fallback-test")?.addEventListener("click", () => testLlmConfig("flash", "fallback").catch((error) => alert(error.message)));
document.getElementById("llm-fallback-test-pro")?.addEventListener("click", () => testLlmConfig("pro", "fallback").catch((error) => alert(error.message)));
document.getElementById("llm-config-toggle")?.addEventListener("click", () => toggleLlmApiKeyVisibility("primary"));
document.getElementById("llm-fallback-toggle")?.addEventListener("click", () => toggleLlmApiKeyVisibility("fallback"));
document.getElementById("llm-provider-select")?.addEventListener("change", () => applyLlmProviderPreset("primary"));
document.getElementById("llm-fallback-provider-select")?.addEventListener("change", () => applyLlmProviderPreset("fallback"));
document.getElementById("llm-base-url-input")?.addEventListener("input", updateLlmInfoPanel);
document.getElementById("llm-fallback-base-url-input")?.addEventListener("input", updateLlmInfoPanel);
document.getElementById("llm-flash-model-select")?.addEventListener("change", () => applyLlmModelSelect("flash", "primary"));
document.getElementById("llm-pro-model-select")?.addEventListener("change", () => applyLlmModelSelect("pro", "primary"));
document.getElementById("llm-fallback-flash-model-select")?.addEventListener("change", () => applyLlmModelSelect("flash", "fallback"));
document.getElementById("llm-fallback-pro-model-select")?.addEventListener("change", () => applyLlmModelSelect("pro", "fallback"));
["llm-flash-reasoning-input", "llm-pro-reasoning-input", "llm-fallback-flash-reasoning-input", "llm-fallback-pro-reasoning-input"].forEach((id) => {
  document.getElementById(id)?.addEventListener("change", updateLlmInfoPanel);
});
document.getElementById("llm-api-key-input")?.addEventListener("input", updateLlmTestButtonState);
document.getElementById("llm-fallback-api-key-input")?.addEventListener("input", updateLlmTestButtonState);
document.getElementById("llm-insecure-tls-input")?.addEventListener("change", updateLlmInfoPanel);
document.getElementById("llm-fallback-insecure-tls-input")?.addEventListener("change", updateLlmInfoPanel);
document.getElementById("llm-fallback-enabled-input")?.addEventListener("change", () => {
  updateLlmInfoPanel();
  updateLlmTestButtonState();
});
document.getElementById("feishu-config-form")?.addEventListener("submit", (event) => saveFeishuConfig(event).catch((error) => alert(error.message)));
document.getElementById("feishu-config-test")?.addEventListener("click", () => testFeishuConfig(false).catch((error) => alert(error.message)));
document.getElementById("feishu-config-test-dry")?.addEventListener("click", () => testFeishuConfig(true).catch((error) => alert(error.message)));
[
  "feishu-enabled-input",
  "feishu-mode-select",
  "feishu-receive-id-type-select",
  "feishu-webhook-url-input",
  "feishu-app-id-input",
  "feishu-default-receive-ids-input",
  "feishu-bound-accounts-input",
].forEach((id) => document.getElementById(id)?.addEventListener("input", updateFeishuInfoPanel));
document.getElementById("local-password-form")?.addEventListener("submit", (event) => changeLocalPassword(event).catch((error) => alert(error.message)));
document.getElementById("local-email-form")?.addEventListener("submit", (event) => bindLocalEmail(event).catch((error) => alert(error.message)));
document.getElementById("local-logout-button")?.addEventListener("click", () => logoutLocal().catch((error) => alert(error.message)));
document.getElementById("refresh-customer-profiles")?.addEventListener("click", () => loadCustomerProfiles().catch((error) => alert(error.message)));
document.getElementById("customer-profile-search")?.addEventListener("input", () => renderCustomerProfileList());

document.getElementById("product-llm-intake-toggle")?.addEventListener("click", toggleProductLlmIntake);
document.getElementById("close-product-llm-intake-panel")?.addEventListener("click", toggleProductLlmIntake);
document.getElementById("product-llm-intake-send")?.addEventListener("click", () => sendProductLlmIntake().catch((error) => alert(error.message)));
document.getElementById("product-llm-intake-input")?.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendProductLlmIntake().catch((error) => alert(error.message)); } });
document.getElementById("product-llm-intake-apply")?.addEventListener("click", () => applyProductLlmIntake().catch((error) => alert(error.message)));
document.getElementById("product-llm-intake-reset")?.addEventListener("click", resetProductLlmIntake);
document.querySelectorAll(".product-detail-close").forEach((node) => node.addEventListener("click", closeProductDetailModal));
document.querySelectorAll(".knowledge-detail-close").forEach((node) => node.addEventListener("click", closeKnowledgeDetailModal));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeProductDetailModal();
    closeKnowledgeDetailModal();
  }
});

document.body.classList.add("auth-locked");
initCustomerServiceFloat();
initializeLocalLogin();
refreshHealth();
window.addEventListener("hashchange", activateHashView);
window.addEventListener("resize", clampCustomerServiceFloatInViewport);

function toggleProductLlmIntake() {
  const panel = document.getElementById("product-llm-intake-panel");
  if (!panel) return;
  panel.classList.toggle("is-hidden");
  if (!panel.classList.contains("is-hidden")) {
    ensureProductLlmGreeting();
    renderProductLlmIntake();
  }
}

let productLlmIntakeSession = null;
let productLlmIntakeApplying = false;
let productLlmIntakeMessages = [];
let productLlmIntakeSnapshot = null;

function appendProductLlmMessage(role, text) {
  const content = String(text || "").trim();
  if (!content) return;
  productLlmIntakeMessages.push({role, content});
  renderProductLlmIntakeChat();
}

function ensureProductLlmGreeting() {
  if (productLlmIntakeMessages.length) return;
  appendProductLlmMessage(
    "assistant",
    "请用自然语言描述要新增或修改的商品信息。我会按标准结构整理，并在关键字段缺失时主动追问。"
  );
}

function renderProductLlmIntake() {
  renderProductLlmIntakeChat();
  renderProductLlmIntakeSummary();
}

function renderProductLlmIntakeChat() {
  const chat = document.getElementById("product-llm-intake-messages");
  if (!chat) return;
  chat.innerHTML = productLlmIntakeMessages.length
    ? productLlmIntakeMessages.map((msg) => `<div class="chat-bubble ${msg.role}">${escapeHtml(msg.content)}</div>`).join("")
    : `<div class="empty-state">在这里描述商品信息，AI 会持续追问并整理结构化结果。</div>`;
  chat.scrollTop = chat.scrollHeight;
}

function renderProductLlmIntakeSummary() {
  const summary = document.getElementById("product-llm-intake-summary");
  if (!summary) return;
  if (!productLlmIntakeSnapshot) {
    summary.innerHTML = "";
    return;
  }
  const payload = productLlmIntakeSnapshot;
  const mode = payload.session?.mode || "";
  const statusValue = payload.status || payload.session?.status || "collecting";
  const preview = payload.assistant_preview || {};
  const warnings = Array.isArray(payload.warnings) ? payload.warnings : [];
  const missing = Array.isArray(payload.missing_fields) ? payload.missing_fields : [];
  const rows = [];
  if (preview.target_product_name) rows.push({label: "目标商品", value: preview.target_product_name});
  if (preview.action) rows.push({label: "执行动作", value: resolveProductAssistantApplyLabel(mode, preview.action).replace("确认", "")});
  if (preview.fields && Object.keys(preview.fields).length) {
    const isCreateScoped = String(preview.action || "").startsWith("create_product_");
    rows.push({label: isCreateScoped ? "计划新增内容" : "计划更新字段", value: summaryFields(preview.fields)});
  }
  const draftRows = mode === "generator"
    ? productDraftSummaryRows(payload.draft_item?.data || {})
    : [];
  const statusTone = statusValue === "ready" ? "ok" : statusValue === "collecting" ? "warning" : "info";
  summary.innerHTML = `
    <div class="status-card ${statusTone}">
      <strong>${escapeHtml(mode === "generator" ? "商品草稿整理中" : "商品修改计划整理中")}</strong>
      <span>状态：${escapeHtml(statusValue)}</span>
    </div>
    ${warnings.length ? `<div class="warning-list">${warnings.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>` : ""}
    ${missing.length ? `<div class="warning-list">${missing.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>` : ""}
    ${(rows.length || draftRows.length) ? `
      <div class="summary-table generator-table">
        ${[...rows, ...draftRows].map((row) => `<div><span>${escapeHtml(row.label)}</span><strong>${escapeHtml(row.value)}</strong></div>`).join("")}
      </div>
    ` : ""}
  `;
}

function productDraftSummaryRows(data) {
  const keys = ["name", "sku", "category", "specs", "price", "unit", "inventory", "price_tiers", "shipping_policy", "warranty_policy", "aliases", "risk_rules"];
  const rows = [];
  keys.forEach((key) => {
    if (isEmpty(data?.[key])) return;
    rows.push({label: fieldLabel({id: key, label: key}), value: displayBusinessValue(data[key])});
  });
  return rows;
}

function setProductLlmStatus(kind, title = "", detail = "") {
  const status = document.getElementById("product-llm-intake-status");
  if (!status) return;
  if (!title && !detail) {
    status.textContent = "";
    status.className = "";
    return;
  }
  status.className = `status-card ${kind}`;
  status.innerHTML = `<strong>${escapeHtml(title)}</strong>${detail ? `<span>${escapeHtml(detail)}</span>` : ""}`;
}

async function sendProductLlmIntake() {
  const input = document.getElementById("product-llm-intake-input");
  const sendBtn = document.getElementById("product-llm-intake-send");
  const actions = document.getElementById("product-llm-intake-actions");
  const applyBtn = document.getElementById("product-llm-intake-apply");
  const text = input?.value?.trim();
  if (!text) return;
  ensureProductLlmGreeting();
  appendProductLlmMessage("user", text);
  if (input) input.value = "";
  setProductLlmStatus("loading", "AI 正在整理", "正在解析你的最新补充...");
  if (sendBtn) sendBtn.disabled = true;
  try {
    const result = await apiJson("/api/product-console/llm-intake", {
      method: "POST",
      body: JSON.stringify({text, session_id: productLlmIntakeSession || "", use_llm: true}),
    });
    productLlmIntakeSession = result.session?.session_id || productLlmIntakeSession;
    productLlmIntakeSnapshot = result;
    const reply = [];
    const mode = result.session?.mode || "";
    const preview = result.assistant_preview || {};
    const previewAction = preview.action || "";
    if (result.question) reply.push(result.question);
    if (preview.summary) reply.push(`执行计划：${preview.summary}`);
    if (result.missing_fields?.length) reply.push("还缺少：" + result.missing_fields.join("、"));
    if (result.warnings?.length) reply.push("注意：" + result.warnings.join("；"));
    if (result.draft_item?.data?.name) reply.push("当前商品名：" + result.draft_item.data.name);
    if (!reply.length) reply.push("已收到，请继续补充或确认执行。");
    appendProductLlmMessage("assistant", reply.join("\n"));
    renderProductLlmIntakeSummary();
    if (result.direct_apply_allowed) {
      if (applyBtn) applyBtn.textContent = resolveProductAssistantApplyLabel(mode, previewAction);
      setProductLlmStatus("ok", mode === "generator" ? "信息已完整，可确认入库" : "信息已完整，可确认执行", "你也可以继续补充，系统会自动刷新计划。");
      if (actions) actions.classList.remove("is-hidden");
    } else {
      if (applyBtn) applyBtn.textContent = "确认执行";
      setProductLlmStatus("warning", "信息还不完整", "请根据上方提示继续补充。");
      if (actions) actions.classList.add("is-hidden");
    }
  } catch (err) {
    appendProductLlmMessage("assistant", "出错：" + (err.message || String(err)));
    setProductLlmStatus("warning", "发送失败", "请稍后重试。");
  } finally {
    if (sendBtn) sendBtn.disabled = false;
  }
}

async function applyProductLlmIntake() {
  if (!productLlmIntakeSession || productLlmIntakeApplying) return;
  const status = document.getElementById("product-llm-intake-status");
  const applyBtn = document.getElementById("product-llm-intake-apply");
  const sendBtn = document.getElementById("product-llm-intake-send");
  const originalLabel = applyBtn?.textContent || "确认执行";
  productLlmIntakeApplying = true;
  if (applyBtn) {
    applyBtn.disabled = true;
    applyBtn.textContent = "确认中...";
  }
  if (sendBtn) sendBtn.disabled = true;
  if (status) status.innerHTML = '<strong><span class="loading-spinner" aria-hidden="true"></span>正在确认执行</strong><span>请稍候，不要重复点击。</span>';
  if (status) status.className = "status-card loading";
  try {
    const result = await apiJson(`/api/product-console/llm-intake/${encodeURIComponent(productLlmIntakeSession)}/apply`, {method: "POST", body: "{}"});
    if (result.action === "product_command_applied") {
      alert("操作已确认并执行。");
    } else {
      alert("商品已通过 AI 对话录入并入库。");
    }
    resetProductLlmIntake();
    await loadProductCatalog();
  } catch (err) {
    setProductLlmStatus("warning", "执行失败", "请检查输入后重试。");
    alert("执行失败：" + (err.message || String(err)));
  } finally {
    productLlmIntakeApplying = false;
    if (sendBtn) sendBtn.disabled = false;
    if (applyBtn) {
      applyBtn.disabled = false;
      if (productLlmIntakeSession) applyBtn.textContent = originalLabel;
    }
  }
}

function resetProductLlmIntake() {
  const status = document.getElementById("product-llm-intake-status");
  const actions = document.getElementById("product-llm-intake-actions");
  const applyBtn = document.getElementById("product-llm-intake-apply");
  productLlmIntakeMessages = [];
  productLlmIntakeSnapshot = null;
  renderProductLlmIntake();
  ensureProductLlmGreeting();
  if (status) {
    status.textContent = "";
    status.className = "";
  }
  if (actions) actions.classList.add("is-hidden");
  if (applyBtn) applyBtn.textContent = "确认执行";
  productLlmIntakeSession = null;
  productLlmIntakeApplying = false;
}

function resolveProductAssistantApplyLabel(mode, action) {
  if (mode === "generator") return "确认入库";
  const mapping = {
    archive: "确认归档",
    set_inventory: "确认改库存",
    increase_inventory: "确认补货",
    decrease_inventory: "确认扣减库存",
    update_product: "确认更新商品",
    create_product_faq: "确认新增专属问答",
    create_product_rules: "确认新增专属规则",
    create_product_explanations: "确认新增专属解释",
  };
  return mapping[action] || "确认执行";
}
