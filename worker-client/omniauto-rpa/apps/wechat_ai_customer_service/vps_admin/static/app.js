const state = {
  token: localStorage.getItem("vpsAdminToken") || "",
  view: "overview",
  data: {},
  recorderBindingDraft: {
    userId: localStorage.getItem("vpsRecorderBindingUserId") || "",
  },
  sharedIndustryFilter: (localStorage.getItem("vpsSharedIndustryFilter") || "").trim() || "global",
  loginChallenge: null,
  initChallenge: null,
  passwordChallenge: null,
  emailChallenge: null,
  loginSubmitting: false,
};

const title = document.querySelector("#view-title");
const message = document.querySelector("#message");
const deviceId = getOrCreateDeviceId("vpsAdminDeviceId");

const viewTitles = {
  overview: "运行总览",
  accounts: "客户与权限",
  customerData: "客户数据",
  sharedKnowledge: "共享公共知识",
  nodes: "客户电脑连接",
  backupRestore: "备份与还原",
  updates: "版本更新",
  security: "账号安全",
  audit: "操作审计",
};

document.body.classList.toggle("auth-locked", !state.token);
document.querySelector("#login-form").addEventListener("submit", login);
document.querySelector("#login-reset")?.addEventListener("click", resetLoginChallenge);
document.querySelector("#init-form")?.addEventListener("submit", initializeAccount);
document.querySelector("#init-back")?.addEventListener("click", resetInitialization);
document.querySelector("#logout-button").addEventListener("click", logout);
document.querySelector("#refresh-button").addEventListener("click", refresh);
document.querySelector("#new-user-role").addEventListener("change", renderAccountFormMode);
document.querySelectorAll("[data-action='refresh']").forEach((button) => button.addEventListener("click", refresh));
document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", async () => {
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("is-active"));
    button.classList.add("is-active");
    state.view = button.dataset.view;
    title.textContent = viewTitles[state.view] || "运行总览";
    document.querySelectorAll(".view-panel").forEach((panel) => {
      panel.classList.toggle("is-visible", panel.dataset.panel === state.view);
    });
    await refresh();
  });
});

bindStaticActions();
refreshHealth();
refresh();

async function login(event) {
  event.preventDefault();
  if (state.loginSubmitting) return;
  state.loginSubmitting = true;
  const submitButton = document.querySelector("#login-submit");
  if (submitButton) submitButton.disabled = true;
  const form = new FormData(event.currentTarget);
  try {
    if (state.loginChallenge) {
      if (state.loginChallenge.mode === "bind_email") {
        const response = await fetch("/v1/auth/login/bind-email/start", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({challenge_id: state.loginChallenge.challenge_id, email: form.get("bind_email")}),
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok || body.ok === false) {
          showLoginMessage(body.detail || "邮箱绑定验证发起失败，请检查邮箱。");
          return;
        }
        state.loginChallenge.mode = "verify";
        document.querySelector("#login-bind-email-field")?.classList.add("is-hidden");
        document.querySelector("#login-code-field")?.classList.remove("is-hidden");
        document.querySelector("#login-trust-field")?.classList.remove("is-hidden");
        document.querySelector("#login-submit").textContent = "验证并登录";
        showLoginMessage(
          body.debug_code
            ? `验证码已生成：${body.debug_code}。生产环境会发送到 ${body.masked_email || "绑定邮箱"}。`
            : `验证码已发送到 ${body.masked_email || "绑定邮箱"}，请输入后登录。`
        );
        return;
      }
      const response = await fetch("/v1/auth/login/verify", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          challenge_id: state.loginChallenge.challenge_id,
          code: form.get("email_code"),
          trust_device: Boolean(form.get("trust_device")),
        }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok || body.ok === false) {
        showLoginMessage(body.detail || "验证码错误或已过期，请重新获取。");
        return;
      }
      completeLogin(body.session);
      return;
    }
    const response = await fetch("/v1/auth/login/start", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        username: form.get("username"),
        password: form.get("password"),
        tenant_id: "default",
        device_id: deviceId,
        device_name: browserDeviceName(),
      }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok || body.ok === false) {
      showLoginMessage(body.detail || "登录失败，请检查管理员密码。");
      return;
    }
    if (!body.requires_verification && body.session) {
      completeLogin(body.session);
      return;
    }
    if (body.requires_initialization) {
      showInitialization(body);
      return;
    }
    state.loginChallenge = {challenge_id: body.challenge_id, mode: body.requires_email_binding ? "bind_email" : "verify"};
    if (body.requires_email_binding) {
      document.querySelector("#login-bind-email-field")?.classList.remove("is-hidden");
      document.querySelector("#login-code-field")?.classList.add("is-hidden");
      document.querySelector("#login-trust-field")?.classList.add("is-hidden");
      document.querySelector("#login-submit").textContent = "发送邮箱验证码";
      showLoginMessage(body.message || "这个账号还没有绑定邮箱，请填写邮箱后获取验证码。");
    } else {
      document.querySelector("#login-bind-email-field")?.classList.add("is-hidden");
      document.querySelector("#login-code-field")?.classList.remove("is-hidden");
      document.querySelector("#login-trust-field")?.classList.remove("is-hidden");
      document.querySelector("#login-submit").textContent = "验证并登录";
      showLoginMessage(
        body.debug_code
          ? `验证码已生成：${body.debug_code}。生产环境会发送到 ${body.masked_email || "绑定邮箱"}。`
          : `验证码已发送到 ${body.masked_email || "绑定邮箱"}，请输入后登录。`
      );
    }
    document.querySelector("#login-reset")?.classList.remove("is-hidden");
  } finally {
    state.loginSubmitting = false;
    if (submitButton) submitButton.disabled = false;
  }
}

function showInitialization(payload) {
  state.initChallenge = {challenge_id: payload.challenge_id, role: payload.role || "admin", mode: "start"};
  document.body.classList.add("auth-locked", "auth-initializing");
  const isAdmin = state.initChallenge.role === "admin";
  document.querySelector("#init-smtp-section")?.classList.toggle("is-hidden", !isAdmin);
  document.querySelector("#init-intro").textContent = isAdmin
    ? "admin 首次使用前必须修改密码、绑定邮箱，并设置 SMTP 与邮箱验证码。完成后需要用新密码重新登录。"
    : "首次使用前必须修改密码并绑定邮箱。完成后需要用新密码重新登录。";
  document.querySelector("#init-code-field")?.classList.add("is-hidden");
  document.querySelector("#init-submit").textContent = "发送初始化验证码";
  hideLoginMessage();
  showInitMessage(payload.message || "请完成首次初始化。");
}

async function initializeAccount(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  if (!state.initChallenge) {
    showInitMessage("初始化会话已失效，请返回登录重新开始。");
    return;
  }
  if (state.initChallenge.mode === "verify") {
    const response = await fetch("/v1/auth/initialize/verify", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({challenge_id: state.initChallenge.challenge_id, code: form.get("email_code")}),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok || body.ok === false) {
      showInitMessage(body.detail || "验证码错误或已过期。");
      return;
    }
    formElement.reset();
    resetInitialization({silent: true});
    resetLoginChallenge({silent: true});
    showLoginMessage("初始化已完成，请使用新密码重新登录。登录时仍需要邮箱验证码。");
    return;
  }
  if (form.get("new_password") !== form.get("confirm_password")) {
    showInitMessage("两次输入的新密码不一致。");
    return;
  }
  const payload = {
    challenge_id: state.initChallenge.challenge_id,
    email: form.get("email"),
    new_password: form.get("new_password"),
    smtp_config: state.initChallenge.role === "admin" ? initSmtpPayload(form) : {},
  };
  const response = await fetch("/v1/auth/initialize/start", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || body.ok === false) {
    showInitMessage(body.detail || "初始化验证码发送失败。");
    return;
  }
  state.initChallenge.mode = "verify";
  document.querySelector("#init-code-field")?.classList.remove("is-hidden");
  document.querySelector("#init-submit").textContent = "验证并完成初始化";
  showInitMessage(
    body.debug_code
      ? `验证码已生成：${body.debug_code}。生产环境会发送到 ${body.masked_email || "绑定邮箱"}。`
      : `验证码已发送到 ${body.masked_email || "绑定邮箱"}，请输入后完成初始化。`
  );
}

function initSmtpPayload(form) {
  return {
    server: form.get("smtp_server"),
    port: Number(form.get("smtp_port") || 465),
    username: form.get("smtp_username"),
    password: form.get("smtp_password"),
    from_email: form.get("smtp_from_email") || form.get("smtp_username") || form.get("email"),
    sender_name: form.get("smtp_sender_name") || "OmniAuto",
    otp_required: Boolean(form.get("smtp_otp_required")),
    use_ssl: Boolean(form.get("smtp_use_ssl")),
    use_tls: Boolean(form.get("smtp_use_tls")),
    code_length: Number(form.get("smtp_code_length") || 4),
    ttl_minutes: Number(form.get("smtp_ttl_minutes") || 15),
    trusted_device_days: Number(form.get("smtp_trusted_device_days") || 30),
  };
}

function resetInitialization(options = {}) {
  state.initChallenge = null;
  document.body.classList.remove("auth-initializing");
  if (!state.token) document.body.classList.add("auth-locked");
  document.querySelector("#init-code-field")?.classList.add("is-hidden");
  document.querySelector("#init-submit").textContent = "发送初始化验证码";
  const codeInput = document.querySelector("#init-form")?.querySelector("[name='email_code']");
  if (codeInput) codeInput.value = "";
  if (!options.silent) hideInitMessage();
}

function showInitMessage(text) {
  const element = document.querySelector("#init-message");
  if (!element) return;
  element.textContent = text;
  element.classList.remove("is-hidden");
}

function hideInitMessage() {
  const element = document.querySelector("#init-message");
  if (!element) return;
  element.textContent = "";
  element.classList.add("is-hidden");
}

function completeLogin(session) {
  state.token = session?.token || "";
  if (!state.token) {
    showLoginMessage("登录成功但没有返回会话 token。");
    return;
  }
  localStorage.setItem("vpsAdminToken", state.token);
  document.body.classList.remove("auth-locked");
  resetLoginChallenge({silent: true});
  hideMessage();
  hideLoginMessage();
  refresh();
}

function resetLoginChallenge(options = {}) {
  state.loginChallenge = null;
  const form = document.querySelector("#login-form");
  document.querySelector("#login-bind-email-field")?.classList.add("is-hidden");
  document.querySelector("#login-code-field")?.classList.add("is-hidden");
  document.querySelector("#login-trust-field")?.classList.add("is-hidden");
  document.querySelector("#login-reset")?.classList.add("is-hidden");
  document.querySelector("#login-submit").textContent = "登录服务端";
  const codeInput = form?.querySelector("[name='email_code']");
  if (codeInput) codeInput.value = "";
  const emailInput = form?.querySelector("[name='bind_email']");
  if (emailInput) emailInput.value = "";
  const trustInput = form?.querySelector("[name='trust_device']");
  if (trustInput) trustInput.checked = false;
  if (!options.silent) hideLoginMessage();
}

function logout() {
  state.token = "";
  state.data = {};
  state.passwordChallenge = null;
  state.emailChallenge = null;
  resetLoginChallenge({silent: true});
  resetInitialization({silent: true});
  localStorage.removeItem("vpsAdminToken");
  document.body.classList.add("auth-locked");
  document.querySelector("#login-pill").textContent = "未登录";
  document.querySelector("#login-pill").classList.remove("is-ok");
  hideMessage();
}

function showLoginMessage(text) {
  const element = document.querySelector("#login-message");
  element.textContent = text;
  element.classList.remove("is-hidden");
}

function hideLoginMessage() {
  const element = document.querySelector("#login-message");
  if (!element) return;
  element.textContent = "";
  element.classList.add("is-hidden");
}

function getOrCreateDeviceId(key) {
  let value = localStorage.getItem(key);
  if (!value) {
    value = `device_${Date.now()}_${Math.random().toString(36).slice(2)}`;
    localStorage.setItem(key, value);
  }
  return value;
}

function browserDeviceName() {
  const platform = navigator.platform || "Browser";
  const language = navigator.language || "";
  return `${platform} ${language}`.trim();
}

async function refresh() {
  if (!state.token) {
    renderSignedOut();
    return;
  }
  try {
    const [
      overview,
      tenants,
      users,
      industries,
      customerData,
      shared,
      sharedLibrary,
      nodes,
      commands,
      backups,
      restores,
      proposals,
      patches,
      releases,
      recorderModules,
      recorderBindings,
      security,
      smtp,
      audit,
    ] = await Promise.all([
      api("/v1/admin/overview"),
      api("/v1/admin/tenants"),
      api("/v1/admin/users"),
      api("/v1/admin/industries"),
      api("/v1/admin/customer-data"),
      api("/v1/admin/shared/overview"),
      api(`/v1/admin/shared/library?include_inactive=true${state.sharedIndustryFilter ? `&industry_id=${encodeURIComponent(state.sharedIndustryFilter)}` : ""}`),
      api("/v1/admin/nodes"),
      api("/v1/admin/commands"),
      api("/v1/admin/backups"),
      api("/v1/admin/restores"),
      api("/v1/admin/shared/proposals"),
      api("/v1/admin/shared/patches"),
      api("/v1/admin/releases"),
      api("/v1/admin/recorder-modules?include_inactive=true"),
      api("/v1/admin/recorder-module-bindings"),
      api("/v1/auth/security"),
      api("/v1/admin/security/smtp"),
      api("/v1/admin/audit"),
    ]);
    state.data = {
      overview,
      tenants: tenants.tenants || [],
      users: users.users || [],
      industries: industries.industries || [],
      customerData: customerData.packages || [],
      shared,
      sharedLibrary: sharedLibrary.items || [],
      nodes: nodes.nodes || [],
      commands: commands.commands || [],
      backups: backups.items || [],
      restores: restores.items || [],
      proposals: proposals.proposals || [],
      patches: patches.patches || [],
      releases: releases.releases || [],
      recorderModules: recorderModules.items || [],
      recorderBindings: recorderBindings.items || [],
      security: security.security || {},
      smtp: smtp.smtp || {},
      audit: audit.events || [],
    };
    document.querySelector("#login-pill").textContent = "admin 已登录";
    document.querySelector("#login-pill").classList.add("is-ok");
    renderAll();
  } catch (error) {
    if (String(error.message || "").includes("401")) logout();
    showMessage(error.message || "刷新失败");
  }
}

async function refreshHealth() {
  const pill = document.querySelector("#health-pill");
  try {
    const payload = await fetch("/v1/health").then((response) => response.json());
    pill.textContent = payload.ok ? "服务端正常" : "服务端异常";
    pill.classList.toggle("is-ok", Boolean(payload.ok));
  } catch {
    pill.textContent = "服务端未连接";
    pill.classList.remove("is-ok");
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {"Content-Type": "application/json", Authorization: `Bearer ${state.token}`, ...(options.headers || {})},
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || body.ok === false) throw new Error(`${response.status} ${body.detail || body.error || "请求失败"}`);
  return body;
}

function renderSignedOut() {
  renderInto("overview-cards", metricCards([["需要登录", "admin"], ["客户账号", "-"], ["共享知识", "-"]]));
  renderInto("overview-recommendations", `<div class="record-row"><strong>请先登录 VPS 管理控制台</strong><span>admin 是唯一隐藏管理员账号，客户不会看到这个账号。</span></div>`);
}

function renderAll() {
  const counts = state.data.overview?.counts || {};
  document.querySelector("#metric-customers").textContent = customerUsers().length;
  document.querySelector("#metric-nodes").textContent = counts.nodes ?? 0;
  document.querySelector("#metric-pending").textContent = counts.shared_pending_proposals ?? counts.pending_commands ?? 0;
  renderOverview();
  renderAccounts();
  renderRecorderModuleAssignments();
  renderCustomerData();
  renderSharedKnowledge();
  renderNodes();
  renderBackupRestore();
  renderUpdates();
  renderSecurity();
  renderAudit();
}

function renderOverview() {
  const counts = state.data.overview?.counts || {};
  renderInto("overview-cards", metricCards([
    ["客户账号", customerUsers().length],
    ["访客账号", guestUsers().length],
    ["客户电脑连接", counts.nodes || 0],
    ["客户数据包", counts.customer_data_packages || 0],
    ["正式共享知识", counts.shared_library_items || state.data.sharedLibrary.length || 0],
    ["待审候选", counts.shared_pending_proposals || 0],
  ]));
  const rows = (state.data.overview?.recommendations || []).map((item) => `
    <div class="record-row">
      <div class="row-title"><strong>${escapeHtml(item.title)}</strong><span class="status-chip ${item.status === "ok" ? "ok" : "warning"}">${item.status === "ok" ? "已确认" : "需注意"}</span></div>
      <span>${escapeHtml(item.detail)}</span>
    </div>
  `).join("");
  renderInto("overview-recommendations", rows || empty("暂无建议"));
}

function renderAccounts() {
  renderAccountFormMode();
  renderIndustrySelect();
  renderPackageUserSelect();
  renderAuthorizedCustomerSelect();
  renderInto("user-list", `<h3>账号列表</h3>${state.data.users.map((item) => `
    <div class="record-row">
      <div class="row-title"><strong>${escapeHtml(item.username)}</strong><span class="status-chip ${item.role === "customer" ? "ok" : "warning"}">${roleLabel(item.role)}</span></div>
      <span>${accountAccessText(item)}</span>
      <span>${customerIndustryText(item)}</span>
      <span>登录邮箱：${escapeHtml(item.email || "未设置，启用验证码后将无法登录")}</span>
      <div class="button-row">
        <button class="secondary-button danger-button" data-action="delete-user" data-user-id="${escapeAttr(item.user_id)}">删除账号</button>
      </div>
    </div>
  `).join("") || empty("暂无 customer/guest 账号")}`);
}

function renderRecorderModuleAssignments() {
  renderRecorderModuleCards();
  renderRecorderBindingFormOptions();
  renderRecorderBindingList();
}

function renderRecorderModuleCards() {
  const modules = state.data.recorderModules || [];
  const rows = modules.map((item) => `
    <div class="record-row">
      <div class="row-title">
        <strong>${escapeHtml(item.module_name || item.module_key)}</strong>
        <span class="status-chip ${String(item.status || "active") === "active" ? "ok" : "warning"}">${escapeHtml(item.status || "active")}</span>
      </div>
      <span>模块Key：${escapeHtml(item.module_key || "")} · 版本：${escapeHtml(item.version || "-")}</span>
      <span>${escapeHtml(item.config?.description || "无描述")}</span>
    </div>
  `).join("");
  renderInto("recorder-module-cards", rows || empty("暂无可用记录员模块"));
}

function renderRecorderBindingFormOptions() {
  const activeModules = (state.data.recorderModules || []).filter((item) => String(item.status || "active") === "active");
  const moduleOptions = activeModules.length
    ? activeModules.map((item) => `<option value="${escapeAttr(item.module_key)}">${escapeHtml(item.module_name || item.module_key)} (${escapeHtml(item.version || "-")})</option>`).join("")
    : `<option value="">暂无可用模块</option>`;

  const userSelect = document.querySelector("#recorder-user-binding-user");
  const currentUserValue = state.recorderBindingDraft.userId || userSelect?.value || "";
  if (userSelect) {
    const users = customerUsers();
    userSelect.innerHTML = users.length
      ? users.map((item) => `<option value="${escapeAttr(item.user_id || "")}">${escapeHtml(item.username || item.user_id || "")}</option>`).join("")
      : `<option value="">暂无客户账号</option>`;
    const userIds = users.map((item) => String(item.user_id || ""));
    if (currentUserValue && userIds.includes(currentUserValue)) {
      userSelect.value = currentUserValue;
    } else {
      userSelect.value = String(userSelect.value || "");
    }
    state.recorderBindingDraft.userId = String(userSelect.value || "");
    if (state.recorderBindingDraft.userId) localStorage.setItem("vpsRecorderBindingUserId", state.recorderBindingDraft.userId);
  }

  ["#recorder-user-binding-module", "#recorder-global-binding-module"].forEach((selector) => {
    const select = document.querySelector(selector);
    if (select) select.innerHTML = moduleOptions;
  });

  syncRecorderBindingFormsFromState();
}

function renderRecorderBindingList() {
  const rawBindings = state.data.recorderBindings || [];
  const bindings = rawBindings.filter((item) => {
    const scopeType = String(item?.scope_type || "");
    return scopeType === "user" || scopeType === "global";
  });
  const modules = state.data.recorderModules || [];
  const users = state.data.users || [];
  const moduleNameByKey = Object.fromEntries(modules.map((item) => [String(item.module_key || ""), String(item.module_name || item.module_key || "")]));
  const userNameById = Object.fromEntries(users.map((item) => [String(item.user_id || ""), String(item.username || item.user_id || "")]));
  const hiddenLegacyTenantCount = Math.max(0, rawBindings.length - bindings.length);

  const rows = bindings.map((item) => {
    const scopeType = String(item.scope_type || "");
    const scopeId = String(item.scope_id || "");
    const ownerLabel = scopeType === "user"
      ? (userNameById[scopeId] || scopeId)
      : "全局默认";
    return `
      <div class="record-row">
        <div class="row-title">
          <strong>${escapeHtml(scopeLabel(scopeType))}：${escapeHtml(ownerLabel)}</strong>
          <span class="status-chip ${item.enabled === false ? "warning" : "ok"}">${item.enabled === false ? "停用" : "启用"}</span>
        </div>
        <span>模块：${escapeHtml(moduleNameByKey[String(item.module_key || "")] || String(item.module_key || ""))}</span>
        <span>更新时间：${escapeHtml(item.updated_at || "-")}</span>
        <div class="button-row">
          <button class="secondary-button danger-button" data-action="delete-recorder-binding" data-binding-id="${escapeAttr(item.binding_id || "")}">删除绑定</button>
        </div>
      </div>
    `;
  }).join("");
  const legacyTip = hiddenLegacyTenantCount > 0
    ? `<div class="helper-card"><strong>已隐藏 ${hiddenLegacyTenantCount} 条历史“租户兜底”记录。</strong><span>当前策略仅使用“账号独立绑定 + 全局兜底”。</span></div>`
    : "";
  renderInto(
    "recorder-binding-list",
    `<h3>当前绑定关系（账号独立绑定 + 全局兜底）</h3>${legacyTip}${rows || empty("暂无模块绑定。")}`
  );
}

function scopeLabel(scopeType) {
  if (scopeType === "user") return "账号绑定";
  if (scopeType === "global") return "全局兜底";
  return "未知";
}

function recorderBindingByScope(scopeType, scopeId) {
  return (state.data.recorderBindings || []).find(
    (item) => String(item?.scope_type || "") === String(scopeType || "")
      && String(item?.scope_id || "") === String(scopeId || "")
  ) || null;
}

function setSelectIfExists(select, value) {
  if (!select) return;
  const target = String(value || "");
  if (!target) return;
  const hasOption = [...select.options].some((option) => String(option.value || "") === target);
  if (hasOption) select.value = target;
}

function syncRecorderBindingFormsFromState() {
  const userId = String(document.querySelector("#recorder-user-binding-user")?.value || state.recorderBindingDraft.userId || "");

  const userBinding = userId ? recorderBindingByScope("user", userId) : null;
  setSelectIfExists(document.querySelector("#recorder-user-binding-module"), userBinding?.module_key || "");
  const userEnabled = document.querySelector("#recorder-user-binding-form input[name='enabled']");
  if (userEnabled) userEnabled.checked = userBinding ? userBinding.enabled !== false : true;

  const globalBinding = recorderBindingByScope("global", "*");
  setSelectIfExists(document.querySelector("#recorder-global-binding-module"), globalBinding?.module_key || "");
  const globalEnabled = document.querySelector("#recorder-global-binding-form input[name='enabled']");
  if (globalEnabled) globalEnabled.checked = globalBinding ? globalBinding.enabled !== false : true;
}

function renderAccountFormMode() {
  const role = document.querySelector("#new-user-role")?.value || "customer";
  document.querySelector("#authorized-customer-field")?.classList.toggle("is-hidden", role !== "guest");
  document.querySelector("#industry-select-field")?.classList.toggle("is-hidden", role === "guest");
}

function renderIndustrySelect() {
  const select = document.querySelector("#new-user-industry");
  if (!select) return;
  const options = (state.data.industries || []);
  if (!options.length) {
    select.innerHTML = `<option value="home_appliance">家电</option>`;
    return;
  }
  const previousValue = select.value;
  select.innerHTML = options.map((item) => `
    <option value="${escapeAttr(item.industry_id)}">${escapeHtml(item.name || item.industry_id)}</option>
  `).join("");
  const fallback = options[0]?.industry_id || "home_appliance";
  select.value = options.some((item) => item.industry_id === previousValue) ? previousValue : fallback;
}

function renderPackageUserSelect() {
  const select = document.querySelector("#package-user-select");
  if (!select) return;
  select.innerHTML = customerUsers().map((user) => `<option value="${escapeAttr(user.username)}">${escapeHtml(user.username)}</option>`).join("") || `<option value="">暂无客户账号</option>`;
}

function renderAuthorizedCustomerSelect() {
  const select = document.querySelector("#authorized-customer-select");
  if (!select) return;
  select.innerHTML = customerUsers().map((user) => `<option value="${escapeAttr(user.username)}">${escapeHtml(user.username)}</option>`).join("") || `<option value="">请先创建客户账号</option>`;
}

function renderCustomerData() {
  const rows = state.data.customerData.map((item) => {
    const formal = item.summary?.formal_knowledge || {};
    const product = item.summary?.product_item_knowledge || {};
    const rag = item.summary?.rag || {};
    return `
      <div class="record-row">
        <div class="row-title">
          <strong>${escapeHtml(displayCustomerName(item))}</strong>
          <span class="status-chip ok">${formatBytes(item.bytes || 0)}</span>
        </div>
        <span>创建时间：${escapeHtml(item.created_at || "-")} · 备份编号：${escapeHtml(item.backup_id || "-")}</span>
        <div class="chip-list">
          <span>正式知识 ${formal.item_count ?? 0} 条</span>
          <span>知识分类 ${formal.category_count ?? 0} 个</span>
          <span>商品专属 ${product.file_count ?? 0} 个文件</span>
          <span>RAG资料 ${rag.sources?.json_file_count ?? 0} 个 JSON</span>
        </div>
        <div class="button-row">
          <button class="secondary-button" data-action="view-package" data-package-id="${escapeAttr(item.package_id)}">查看详情</button>
          <button class="secondary-button" data-action="download-readable-package" data-package-id="${escapeAttr(item.package_id)}">下载可读知识表</button>
          <button class="secondary-button" data-action="download-package" data-package-id="${escapeAttr(item.package_id)}">下载</button>
          <button class="secondary-button danger-button" data-action="delete-package" data-package-id="${escapeAttr(item.package_id)}">删除</button>
        </div>
      </div>
    `;
  }).join("");
  renderInto("customer-data-list", rows || empty("暂无客户数据包。请选择客户后点击“打包所选客户数据”。"));
}

function renderSharedKnowledge() {
  const snapshots = state.data.shared?.snapshots || [];
  const proposals = state.data.proposals || [];
  const pendingProposals = proposals.filter((item) => item.status === "pending_review");
  const archivedProposals = proposals.filter((item) => ["rejected", "void"].includes(item.status));
  const industries = state.data.industries || [];
  const currentFilter = state.sharedIndustryFilter || "";
  const filterHtml = industries.length
    ? `<div class="filter-bar"><label>行业筛选：</label><select id="shared-industry-filter"><option value="global" ${currentFilter === "global" ? "selected" : ""}>全行业通用</option>${industries.map((ind) => `<option value="${escapeHtml(ind.industry_id)}" ${ind.industry_id === currentFilter ? "selected" : ""}>${escapeHtml(ind.name || ind.industry_id)}</option>`).join("")}</select></div>`
    : "";
  renderInto("shared-cards", metricCards([["候选待审", pendingProposals.length], ["正式库条目", state.data.sharedLibrary.length || 0], ["已发布补丁", state.data.patches.length || 0], ["已归档候选", archivedProposals.length]]));
  renderInto("shared-library-list", `${filterHtml}<h3>正式共享库</h3>${state.data.sharedLibrary.map((item) => `
    <details class="record-row collapsible-record">
      <summary class="collapse-summary">
        <div class="row-title"><strong>${escapeHtml(item.title || item.item_id)}</strong><span class="status-chip ${item.status === "active" ? "ok" : "warning"}">${escapeHtml(item.status || "active")}</span></div>
      </summary>
      <div class="record-body">
        <span>行业：${escapeHtml(industryLabel(item.industry_id))} · 分类：${escapeHtml(sharedCategoryLabel(item.category_id))} · 关键词：${escapeHtml((item.keywords || []).join("、") || "-")}</span>
        <span>${escapeHtml(trimText(sharedItemContent(item), 140))}</span>
        <div class="button-row">
          <button class="secondary-button" data-action="view-library" data-item-id="${escapeAttr(item.item_id)}">查看和编辑</button>
        </div>
      </div>
    </details>
  `).join("") || empty("正式共享库暂无条目")}`);
  const archivedHtml = archivedProposals.length
    ? `<details class="archive-section">
        <summary><strong>已拒绝 / 已作废归档</strong><span class="status-chip">${archivedProposals.length} 条</span></summary>
        <div class="archive-body">${archivedProposals.map((item) => sharedProposalCard(item, {archived: true})).join("")}</div>
      </details>`
    : "";
  renderInto("shared-proposal-list", `<h3>候选待审核</h3>${pendingProposals.map((item) => sharedProposalCard(item)).join("") || empty("暂无待审核候选")}${archivedHtml}`);
  const recentPatches = (state.data.patches || []).slice(0, 10);
  renderInto("shared-patch-list", `<h3>已发布补丁（最近 10 条）</h3>${recentPatches.map((item) => `
    <div class="record-row">
      <div class="row-title">
        <strong>${escapeHtml(item.version || item.patch_id)}</strong>
        <span class="status-chip ${patchSignatureClass(item)}">${escapeHtml(item.signature_label || (item.signature ? "已签名" : "本地模式未签名"))}</span>
      </div>
      <span>补丁编号：${escapeHtml(item.patch_id)} · 来源候选：${escapeHtml(item.source_proposal_id || "-")} · 条目：${escapeHtml((item.library_item_ids || []).join("、") || "-")}</span>
      <span>操作数量：${escapeHtml(String((item.operations || []).length))} · 发布时间：${escapeHtml(item.created_at || "-")}</span>
      ${patchDeliveryHtml(item.delivery)}
      <div class="button-row">
        <button class="primary-button" data-action="push-shared-patch" data-patch-id="${escapeAttr(item.patch_id)}">推送到客户电脑</button>
      </div>
    </div>
  `).join("") || empty("暂无已发布补丁。采纳候选后会自动生成补丁。")}`);
  const filterEl = document.getElementById("shared-industry-filter");
  if (filterEl) {
    filterEl.addEventListener("change", (e) => {
      state.sharedIndustryFilter = e.target.value;
      localStorage.setItem("vpsSharedIndustryFilter", e.target.value);
      refresh();
    });
  }
}

function patchSignatureClass(item) {
  if (item.signature_status === "signed" || item.signature) return "ok";
  return item.signature_status === "unsigned_required" ? "danger" : "warning";
}

function patchDeliveryHtml(delivery) {
  if (!delivery || !delivery.counts) {
    return `<div class="patch-delivery muted">尚未推送到客户电脑。</div>`;
  }
  const counts = delivery.counts || {};
  const statusText = patchDeliveryOverallLabel(delivery.overall_status);
  const targets = delivery.targets || [];
  const targetHtml = targets.slice(0, 8).map((target) => `
    <div class="delivery-target ${target.delivery_status || "queued"}">
      <div>
        <strong>${escapeHtml(target.tenant_name || target.tenant_id || "-")}</strong>
        <span>${escapeHtml(target.node_name || target.node_id || "客户电脑")} · ${escapeHtml(target.node_last_seen_at || "未上报在线时间")}</span>
      </div>
      <span class="status-chip ${deliveryStatusClass(target.delivery_status)}">${deliveryStatusLabel(target.delivery_status)}</span>
    </div>
  `).join("");
  return `
    <div class="patch-delivery">
      <div class="delivery-summary">
        <strong>${escapeHtml(statusText)}</strong>
        <span>共 ${counts.total || 0} 台：已生效 ${counts.applied || 0}、等待 ${Number(counts.queued || 0) + Number(counts.sent || 0)}、失败 ${counts.failed || 0}</span>
      </div>
      ${targetHtml ? `<div class="delivery-target-list">${targetHtml}</div>` : `<span class="muted">尚未推送到客户电脑。</span>`}
    </div>
  `;
}

function patchDeliveryOverallLabel(value) {
  return {
    not_pushed: "尚未推送",
    pending: "已发起，等待客户电脑领取或生效",
    applied: "已全部生效",
    failed: "部分客户电脑失败",
  }[value] || value || "尚未推送";
}

function deliveryStatusLabel(value) {
  return {
    queued: "未领取",
    sent: "已领取待回报",
    applied: "已生效",
    failed: "失败",
  }[value] || value || "-";
}

function deliveryStatusClass(value) {
  return value === "applied" ? "ok" : value === "failed" ? "danger" : "warning";
}

function sharedPatchPushMessage(payload) {
  const delivery = payload.delivery || payload.patch?.delivery || {};
  const counts = delivery.counts || {};
  const queued = Number(counts.queued || 0) + Number(counts.sent || 0);
  return `已创建 ${payload.commands?.length || counts.total || 0} 条推送命令。已生效 ${counts.applied || 0} 台，等待客户电脑轮询/重启 ${queued} 台，失败 ${counts.failed || 0} 台。详情已显示在该补丁下方。`;
}

function sharedProposalCard(item, {archived = false} = {}) {
  const actionable = item.status === "pending_review";
  return `
    <details class="record-row collapsible-record ${actionable ? "is-pending" : "is-reviewed"}">
      <summary class="collapse-summary">
        <div class="row-title"><strong>${escapeHtml(item.title || item.proposal_id)}</strong><span class="status-chip ${proposalStatusChipClass(item.status)}">${proposalStatus(item.status)}</span></div>
      </summary>
      <div class="record-body">
        <span>来源客户：${escapeHtml(customerNameForTenant(item.tenant_id) || item.tenant_id || "-")} · ${escapeHtml(item.created_at || "")}</span>
        <span>${escapeHtml(proposalPreview(item))}</span>
        ${proposalReviewAssistSummaryHtml(item)}
        <div class="button-row">
          <button class="secondary-button" data-action="view-proposal" data-proposal-id="${escapeAttr(item.proposal_id)}">${archived ? "查看归档详情" : "查看详情"}</button>
          ${archived ? "" : `
            <button class="secondary-button" data-action="accept-proposal" data-proposal-id="${escapeAttr(item.proposal_id)}" ${!actionable ? "disabled" : ""}>采纳</button>
            <button class="secondary-button" data-action="reject-proposal" data-proposal-id="${escapeAttr(item.proposal_id)}" ${!actionable ? "disabled" : ""}>拒绝</button>
            <button class="secondary-button danger-button" data-action="void-proposal" data-proposal-id="${escapeAttr(item.proposal_id)}" ${!actionable ? "disabled" : ""}>作废</button>
          `}
        </div>
      </div>
    </details>
  `;
}

function renderNodes() {
  renderInto("node-list", `<h3>客户电脑</h3>${state.data.nodes.map((item) => `
    <div class="record-row">
      <div class="row-title"><strong>${escapeHtml(item.display_name || item.node_id)}</strong><span class="status-chip ${item.status === "online" ? "ok" : "warning"}">${escapeHtml(item.status || "-")}</span></div>
      <span>客户：${escapeHtml((item.tenant_ids || []).map(customerNameForTenant).join("，") || "-")} · 版本：${escapeHtml(item.version || "-")} · 最后在线：${escapeHtml(item.last_seen_at || "-")}</span>
      <span>节点编号：${escapeHtml(item.node_id)}</span>
    </div>
  `).join("") || empty("暂无客户电脑连接。请在 Local 客户端登录一次，Local 会自动向 VPS 报到。")}`);
  renderInto("command-list", `<h3>命令队列</h3>${state.data.commands.slice(0, 12).map(commandRow).join("") || empty("暂无命令")}`);
}

function renderBackupRestore() {
  renderInto("backup-list", `<h3>备份记录</h3>${state.data.backups.map((item) => `
    <div class="record-row">
      <div class="row-title"><strong>${escapeHtml(item.request_id)}</strong><span class="status-chip ${item.status === "succeeded" ? "ok" : "warning"}">${backupStatus(item.status)}</span></div>
      <span>范围：${scopeLabel(item.scope)} · 客户：${escapeHtml(customerNameForTenant(item.tenant_id) || "-")} · 备份：${escapeHtml(item.backup_id || item.command_id || "-")}</span>
      <span>${item.package_path ? `文件：${escapeHtml(item.package_path)} · ${formatBytes(item.bytes || 0)}` : "远程命令备份，等待客户电脑回传结果"}</span>
      <div class="button-row">
        <button class="secondary-button danger-button" data-action="delete-backup" data-request-id="${escapeAttr(item.request_id)}">删除备份记录</button>
      </div>
    </div>
  `).join("") || empty("暂无备份记录")}`);
  renderInto("restore-list", `<h3>还原记录</h3>${state.data.restores.map((item) => `
    <div class="record-row">
      <div class="row-title"><strong>${escapeHtml(item.request_id)}</strong><span class="status-chip warning">${item.dry_run ? "演练" : "真实还原"}</span></div>
      <span>客户：${escapeHtml(customerNameForTenant(item.tenant_id) || "-")} · 备份：${escapeHtml(item.backup_id || "-")} · 命令：${escapeHtml(item.command_id || "-")}</span>
    </div>
  `).join("") || empty("暂无还原记录")}`);
}

function renderUpdates() {
  renderInto("release-list", `<h3>版本记录</h3>${state.data.releases.map((item) => `
    <div class="record-row">
      <div class="row-title"><strong>${escapeHtml(item.version)} · ${channelLabel(item.channel)}</strong><span class="status-chip ${item.sha256 ? "ok" : "warning"}">${item.sha256 ? "有校验码" : "待补校验"}</span></div>
      <span>${escapeHtml(item.title || "未命名版本")} · ${escapeHtml(item.artifact_url || "未填写更新包地址")}</span>
      <span>SHA256：${escapeHtml(trimText(item.sha256 || "-", 24))} · 签名：${escapeHtml(trimText(item.signature || "-", 24))}</span>
      <span>${escapeHtml(item.notes || "")}</span>
      <div class="button-row">
        <button class="secondary-button" data-action="push-release-check" data-release-id="${escapeAttr(item.release_id)}">通知客户电脑检查更新</button>
      </div>
    </div>
  `).join("") || empty("暂无版本记录")}`);
}

function renderSecurity() {
  const security = state.data.security || {};
  const smtp = state.data.smtp || {};
  const accountPanel = document.querySelector("#security-account-summary");
  if (accountPanel) {
    accountPanel.innerHTML = `
      <div class="record-row">
        <div class="row-title"><strong>当前账号</strong><span class="status-chip ok">${escapeHtml(security.role || "admin")}</span></div>
        <span>登录邮箱：${escapeHtml(security.masked_email || security.email || "未设置")}</span>
        <span>邮箱验证码：${security.otp_required ? "已启用" : "未启用"} · 信任设备：${escapeHtml(String(security.trusted_device_days || 30))} 天</span>
      </div>
    `;
  }
  const smtpForm = document.querySelector("#smtp-form");
  if (smtpForm && !smtpForm.dataset.loaded) {
    smtpForm.elements.server.value = smtp.server || "";
    smtpForm.elements.port.value = smtp.port || 465;
    smtpForm.elements.username.value = smtp.username || "";
    smtpForm.elements.from_email.value = smtp.from_email || "";
    smtpForm.elements.sender_name.value = smtp.sender_name || "OmniAuto";
    smtpForm.elements.otp_required.checked = smtp.otp_required !== false;
    smtpForm.elements.use_ssl.checked = smtp.use_ssl !== false;
    smtpForm.elements.use_tls.checked = Boolean(smtp.use_tls);
    smtpForm.elements.code_length.value = smtp.code_length || 4;
    smtpForm.elements.ttl_minutes.value = smtp.ttl_minutes || 15;
    smtpForm.elements.trusted_device_days.value = smtp.trusted_device_days || 30;
    smtpForm.dataset.loaded = "1";
  }
  const smtpStatus = document.querySelector("#smtp-status");
  if (smtpStatus) {
    smtpStatus.textContent = smtp.smtp_configured
      ? `SMTP 已配置，发件人 ${smtp.from_email || smtp.username || "-"}`
      : "SMTP 未配置完整；本地测试时验证码会写入开发 outbox。";
  }
  const emailForm = document.querySelector("#email-form");
  if (emailForm && !emailForm.dataset.loaded) {
    emailForm.elements.email.value = security.email || "";
    emailForm.dataset.loaded = "1";
  }
}

function renderAudit() {
  renderInto("audit-list", state.data.audit.slice(0, 20).map((item) => `
    <div class="record-row">
      <strong>${escapeHtml(actionLabel(item.action))}</strong>
      <span>${escapeHtml(item.actor_id)} · ${escapeHtml(item.target_type)}:${escapeHtml(item.target_id)} · ${escapeHtml(item.created_at)}</span>
    </div>
  `).join("") || empty("暂无审计记录"));
}

function bindStaticActions() {
  document.querySelector("#sync-shared").addEventListener("click", async () => {
    await runAction(
      () => api("/v1/admin/shared/sync-local", {method: "POST", body: "{}"}),
      (result) => {
        const summary = result.snapshot?.summary || {};
        return `已保存云端正式共享库快照：${summary.item_count || 0} 条知识、${summary.category_count || 0} 个分类。这个操作只记录云端正式库状态，不会直接修改客户电脑。`;
      }
    );
  });
  document.querySelector("#generate-shared-candidates")?.addEventListener("click", async () => {
    await runAction(
      () => api("/v1/admin/shared/proposals/generate-from-formal", {method: "POST", body: JSON.stringify({use_llm: true, limit: 80, only_unscanned: true})}),
      "已检查未处理的客户正式知识，并把适合的内容提交到候选共享公共知识库。"
    );
  });
  document.querySelector("#backup-all").addEventListener("click", async () => {
    await runAction(() => api("/v1/admin/backups/local-now", {method: "POST", body: JSON.stringify({scope: "all", tenant_id: "default"})}), "已完成一键全量备份。");
  });
  document.querySelector("#restore-latest").addEventListener("click", async () => {
    if (!confirm("将从最新全量备份创建还原演练命令，不会直接覆盖本地数据。继续吗？")) return;
    await runAction(() => api("/v1/admin/restores/latest", {method: "POST", body: JSON.stringify({scope: "all", tenant_id: "default", dry_run: true})}), "已创建一键还原演练命令。");
  });
  document.body.addEventListener("click", handlePageAction);
  bindForm("#user-form", "/v1/admin/users", accountFormPayload, "账号已创建。");
  bindPasswordForm();
  bindEmailForm();
  bindSmtpForm();
  bindForm("#customer-package-form", "/v1/admin/customer-data/package-customer", (form) => ({account_username: form.get("account_username")}), "已打包所选客户数据。");
  bindForm("#library-form", "/v1/admin/shared/library", (form) => ({
    item_id: form.get("item_id"),
    industry_id: form.get("industry_id") || "global",
    category_id: form.get("category_id"),
    title: form.get("title"),
    keywords: splitKeywords(form.get("keywords")),
    applies_to: form.get("applies_to"),
    content: form.get("content"),
    notes: form.get("notes"),
    data: {
      title: form.get("title"),
      industry_id: form.get("industry_id") || "global",
      keywords: splitKeywords(form.get("keywords")),
      applies_to: form.get("applies_to"),
      guideline_text: form.get("content"),
      notes: form.get("notes"),
    },
    source: "admin_console",
  }), "正式共享知识已新增。");
  bindForm("#release-form", "/v1/admin/releases", (form) => ({
    version: form.get("version"),
    channel: form.get("channel"),
    title: form.get("title"),
    artifact_url: form.get("artifact_url"),
    sha256: form.get("sha256"),
    signature: form.get("signature"),
    notes: form.get("notes"),
  }), "版本记录已创建。");
  bindRecorderModuleForms();
  bindSharedAiDraft();
}

function bindRecorderModuleForms() {
  const userSelect = document.querySelector("#recorder-user-binding-user");
  if (userSelect) {
    userSelect.addEventListener("change", () => {
      state.recorderBindingDraft.userId = String(userSelect.value || "");
      if (state.recorderBindingDraft.userId) localStorage.setItem("vpsRecorderBindingUserId", state.recorderBindingDraft.userId);
      syncRecorderBindingFormsFromState();
    });
  }

  const userForm = document.querySelector("#recorder-user-binding-form");
  if (userForm) {
    userForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = new FormData(userForm);
      const userId = String(form.get("scope_user_id") || "");
      const moduleKey = String(form.get("module_key") || "");
      if (!userId || !moduleKey) {
        showMessage("请选择账号和模块。");
        return;
      }
      state.recorderBindingDraft.userId = userId;
      localStorage.setItem("vpsRecorderBindingUserId", userId);
      const userName = (customerUsers().find((item) => String(item.user_id || "") === userId)?.username) || userId;
      await runAction(
        () => api("/v1/admin/recorder-module-bindings", {
          method: "POST",
          body: JSON.stringify({
            binding_id: `user_${userId}`,
            scope_type: "user",
            scope_id: userId,
            user_id: userId,
            module_key: moduleKey,
            enabled: Boolean(form.get("enabled")),
          }),
        }),
        `账号「${userName}」的独立模块绑定已保存。`
      );
    });
  }

  const globalForm = document.querySelector("#recorder-global-binding-form");
  if (globalForm) {
    globalForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = new FormData(globalForm);
      const moduleKey = String(form.get("module_key") || "");
      if (!moduleKey) {
        showMessage("请选择全局默认模块。");
        return;
      }
      await runAction(
        () => api("/v1/admin/recorder-module-bindings", {
          method: "POST",
          body: JSON.stringify({
            binding_id: "global_default",
            scope_type: "global",
            scope_id: "*",
            module_key: moduleKey,
            enabled: Boolean(form.get("enabled")),
          }),
        }),
        "全局兜底模块已保存（仅账号未配置时生效）。"
      );
    });
  }
}

function bindSharedAiDraft() {
  const toggle = document.querySelector("#shared-ai-draft-toggle");
  const panel = document.querySelector("#shared-ai-draft-panel");
  const input = document.querySelector("#shared-ai-draft-input");
  const sendBtn = document.querySelector("#shared-ai-draft-send");
  const messages = document.querySelector("#shared-ai-draft-messages");
  const status = document.querySelector("#shared-ai-draft-status");
  const actions = document.querySelector("#shared-ai-draft-actions");
  const applyBtn = document.querySelector("#shared-ai-draft-apply");
  const resetBtn = document.querySelector("#shared-ai-draft-reset");
  if (!toggle || !panel) return;

  let currentDraft = null;

  toggle.addEventListener("click", () => {
    panel.classList.toggle("is-hidden");
    toggle.textContent = panel.classList.contains("is-hidden") ? "用自然语言创建（AI 对话）" : "收起 AI 对话";
  });

  function appendMessage(role, text) {
    const div = document.createElement("div");
    div.style.cssText = "margin:6px 0;padding:8px 12px;border-radius:8px;max-width:90%;" + (role === "user" ? "background:#e8f0fe;margin-left:auto;" : "background:#f1f3f4;");
    div.textContent = text;
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
  }

  async function sendDescription() {
    const text = input.value.trim();
    if (!text) return;
    appendMessage("user", text);
    input.value = "";
    status.textContent = "AI 正在整理...";
    sendBtn.disabled = true;
    try {
      const result = await api("/v1/admin/shared/library/draft", {
        method: "POST",
        body: JSON.stringify({description: text, use_llm: true}),
      });
      currentDraft = result.draft || null;
      if (currentDraft) {
        const info = [];
        info.push(`标题：${currentDraft.title || "（未生成）"}`);
        info.push(`类别：${currentDraft.category_id || "（未生成）"}`);
        info.push(`内容：${(currentDraft.content || "").slice(0, 120)}${(currentDraft.content || "").length > 120 ? "..." : ""}`);
        if (currentDraft.missing_fields?.length) info.push(`缺失字段：${currentDraft.missing_fields.join(", ")}`);
        if (currentDraft.warnings?.length) info.push(`警告：${currentDraft.warnings.join("；")}`);
        if (currentDraft.followup_question) info.push(`追问：${currentDraft.followup_question}`);
        appendMessage("assistant", info.join("\n"));
        if (result.ready) {
          status.textContent = "草稿已就绪，可以确认填入表单。";
          status.className = "status-card ok";
          actions.classList.remove("is-hidden");
        } else {
          status.textContent = currentDraft.followup_question || "草稿不完整，请补充信息后重试。";
          status.className = "status-card warning";
          actions.classList.add("is-hidden");
        }
      } else {
        appendMessage("assistant", result.message || "未能生成草稿。");
        status.textContent = "";
        actions.classList.add("is-hidden");
      }
    } catch (err) {
      appendMessage("assistant", "出错：" + (err.message || String(err)));
      status.textContent = "";
    } finally {
      sendBtn.disabled = false;
    }
  }

  sendBtn?.addEventListener("click", sendDescription);
  input?.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendDescription(); } });

  applyBtn?.addEventListener("click", () => {
    if (!currentDraft) return;
    const form = document.querySelector("#library-form");
    if (!form) return;
    const setVal = (name, value) => { const el = form.querySelector(`[name="${name}"]`); if (el) el.value = value || ""; };
    setVal("title", currentDraft.title);
    setVal("category_id", currentDraft.category_id);
    setVal("industry_id", currentDraft.industry_id);
    setVal("keywords", (currentDraft.keywords || []).join(", "));
    setVal("applies_to", currentDraft.applies_to);
    setVal("content", currentDraft.content);
    setVal("notes", currentDraft.notes);
    showMessage("AI 草稿已填入表单，请核对后提交。", "ok");
    panel.classList.add("is-hidden");
    toggle.textContent = "用自然语言创建（AI 对话）";
  });

  resetBtn?.addEventListener("click", () => {
    messages.innerHTML = "";
    status.textContent = "";
    status.className = "";
    actions.classList.add("is-hidden");
    currentDraft = null;
    input.value = "";
    input.focus();
  });
}

function accountFormPayload(form) {
  const role = String(form.get("role") || "customer");
  const payload = {username: form.get("username"), password: form.get("password"), email: form.get("email"), role};
  if (role === "guest") {
    const customer = form.get("authorized_customer");
    payload.authorized_customer = customer;
    payload.tenant_ids = tenantIdsForCustomer(customer);
  } else {
    payload.industry_id = String(form.get("industry_id") || "");
  }
  return payload;
}

function bindPasswordForm() {
  const form = document.querySelector("#password-form");
  if (!form) return;
  const codeField = document.querySelector("#password-code-field");
  const submitButton = form.querySelector("button[type='submit']");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    if (state.passwordChallenge) {
      try {
        await api("/v1/auth/change-password/verify", {
          method: "POST",
          body: JSON.stringify({challenge_id: state.passwordChallenge.challenge_id, code: data.get("email_code")}),
        });
        state.passwordChallenge = null;
        form.reset();
        codeField?.classList.add("is-hidden");
        if (submitButton) submitButton.textContent = "发送验证码并修改";
        showMessage("密码已修改。请用新密码重新登录。", "ok");
        logout();
      } catch (error) {
        showMessage(error.message || "验证码错误或已过期");
      }
      return;
    }
    if (data.get("new_password") !== data.get("confirm_password")) {
      showMessage("两次输入的新密码不一致。");
      return;
    }
    try {
      const payload = await api("/v1/auth/change-password/start", {
        method: "POST",
        body: JSON.stringify({
          current_password: data.get("current_password"),
          new_password: data.get("new_password"),
        }),
      });
      state.passwordChallenge = {challenge_id: payload.challenge_id};
      codeField?.classList.remove("is-hidden");
      if (submitButton) submitButton.textContent = "验证并保存新密码";
      showMessage(
        payload.debug_code
          ? `验证码已生成：${payload.debug_code}。生产环境会发送到 ${payload.masked_email || "绑定邮箱"}。`
          : `验证码已发送到 ${payload.masked_email || "绑定邮箱"}，请输入后保存新密码。`,
        "ok"
      );
    } catch (error) {
      showMessage(error.message || "密码修改失败");
    }
  });
}

function bindEmailForm() {
  const form = document.querySelector("#email-form");
  if (!form) return;
  const codeField = document.querySelector("#email-code-field");
  const submitButton = form.querySelector("button[type='submit']");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    try {
      if (state.emailChallenge) {
        const result = await api("/v1/auth/email/verify", {
          method: "POST",
          body: JSON.stringify({challenge_id: state.emailChallenge.challenge_id, code: data.get("email_code")}),
        });
        state.emailChallenge = null;
        codeField?.classList.add("is-hidden");
        if (submitButton) submitButton.textContent = "发送绑定验证码";
        state.data.security = {...(state.data.security || {}), email: result.email, masked_email: result.masked_email};
        form.dataset.loaded = "";
        renderSecurity();
        showMessage("管理员邮箱已更新。下次登录将向这个邮箱发送验证码。", "ok");
        return;
      }
      const result = await api("/v1/auth/email/start", {
        method: "POST",
        body: JSON.stringify({email: data.get("email")}),
      });
      state.emailChallenge = {challenge_id: result.challenge_id};
      codeField?.classList.remove("is-hidden");
      if (submitButton) submitButton.textContent = "验证并绑定邮箱";
      showMessage(
        result.debug_code
          ? `验证码已生成：${result.debug_code}。生产环境会发送到 ${result.masked_email || "绑定邮箱"}。`
          : `验证码已发送到 ${result.masked_email || "绑定邮箱"}，请输入后完成绑定。`,
        "ok"
      );
    } catch (error) {
      showMessage(error.message || "邮箱绑定失败");
    }
  });
}

function bindSmtpForm() {
  const form = document.querySelector("#smtp-form");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const payload = {
      server: data.get("server"),
      port: Number(data.get("port") || 465),
      username: data.get("username"),
      password: data.get("password"),
      from_email: data.get("from_email"),
      sender_name: data.get("sender_name"),
      otp_required: Boolean(data.get("otp_required")),
      use_ssl: Boolean(data.get("use_ssl")),
      use_tls: Boolean(data.get("use_tls")),
      code_length: Number(data.get("code_length") || 4),
      ttl_minutes: Number(data.get("ttl_minutes") || 15),
      trusted_device_days: Number(data.get("trusted_device_days") || 30),
    };
    try {
      const result = await api("/v1/admin/security/smtp", {method: "PATCH", body: JSON.stringify(payload)});
      state.data.smtp = result.smtp || {};
      form.elements.password.value = "";
      form.dataset.loaded = "";
      renderSecurity();
      showMessage("SMTP 与邮箱验证设置已保存。", "ok");
    } catch (error) {
      showMessage(error.message || "SMTP 设置保存失败");
    }
  });
  document.querySelector("#smtp-test")?.addEventListener("click", async () => {
    const data = new FormData(form);
    try {
      await api("/v1/admin/security/smtp/test", {method: "POST", body: JSON.stringify({to_email: data.get("test_email") || data.get("from_email")})});
      showMessage("测试邮件已发送；未配置完整 SMTP 时会写入开发 outbox。", "ok");
    } catch (error) {
      showMessage(error.message || "测试邮件发送失败");
    }
  });
}

async function handlePageAction(event) {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.action;
  if (action === "refresh") return;
  try {
    if (action === "delete-user") {
      if (!confirm("确认删除这个账号吗？")) return;
      await api(`/v1/admin/users/${encodeURIComponent(button.dataset.userId)}`, {method: "DELETE"});
      showMessage("账号已删除。", "ok");
    } else if (action === "delete-recorder-binding") {
      if (!confirm("确认删除这条记录员模块绑定吗？")) return;
      await api(`/v1/admin/recorder-module-bindings/${encodeURIComponent(button.dataset.bindingId)}`, {method: "DELETE"});
      showMessage("记录员模块绑定已删除。", "ok");
    } else if (action === "view-package") {
      await showPackageDetail(button.dataset.packageId);
      return;
    } else if (action === "download-package") {
      await downloadPackage(button.dataset.packageId);
      return;
    } else if (action === "download-readable-package") {
      await downloadReadablePackage(button.dataset.packageId);
      return;
    } else if (action === "delete-package") {
      if (!confirm("确认删除这个客户数据包吗？服务端文件也会被清理。")) return;
      await api(`/v1/admin/customer-data/${encodeURIComponent(button.dataset.packageId)}`, {method: "DELETE"});
      showMessage("客户数据包已删除。", "ok");
    } else if (action === "view-library") {
      await showLibraryDetail(button.dataset.itemId);
      return;
    } else if (action === "view-proposal") {
      showProposalDetail(button.dataset.proposalId);
      return;
    } else if (action === "edit-library") {
      await editLibraryItem(button.dataset.itemId);
      return;
    } else if (action === "delete-library") {
      if (!confirm("确认删除这条正式共享知识吗？")) return;
      await api(`/v1/admin/shared/library/${encodeURIComponent(button.dataset.itemId)}`, {method: "DELETE"});
      showMessage("共享知识已删除。", "ok");
    } else if (action === "accept-proposal") {
      await reviewProposal(button.dataset.proposalId, "accept", "候选已采纳，并写入正式共享库。");
    } else if (action === "reject-proposal") {
      await reviewProposal(button.dataset.proposalId, "reject", "候选已拒绝。");
    } else if (action === "void-proposal") {
      await reviewProposal(button.dataset.proposalId, "void", "候选已作废。");
    } else if (action === "refresh-proposal-assist") {
      button.disabled = true;
      button.textContent = "AI复核中...";
      await api(`/v1/admin/shared/proposals/${encodeURIComponent(button.dataset.proposalId)}/review-assist`, {
        method: "POST",
        body: JSON.stringify({use_llm: true}),
      });
      showMessage("AI 已重新复核这条共享候选。", "ok");
    } else if (action === "push-shared-patch") {
      if (!confirm("确认把这个共享知识补丁推送给所有已连接的客户电脑吗？")) return;
      const pushed = await api(`/v1/admin/shared/patches/${encodeURIComponent(button.dataset.patchId)}/push`, {method: "POST", body: JSON.stringify({})});
      showMessage(sharedPatchPushMessage(pushed), "ok");
    } else if (action === "push-release-check") {
      if (!confirm("确认通知所有客户电脑检查这个版本更新吗？不会自动安装。")) return;
      await api(`/v1/admin/releases/${encodeURIComponent(button.dataset.releaseId)}/push`, {method: "POST", body: JSON.stringify({mode: "check_update"})});
      showMessage("已创建检查更新命令。客户电脑轮询后会看到版本记录。", "ok");
    } else if (action === "delete-backup") {
      if (!confirm("确认删除这条备份记录吗？如果它关联了服务端备份文件，也会一并清理。")) return;
      await api(`/v1/admin/backups/${encodeURIComponent(button.dataset.requestId)}`, {method: "DELETE"});
      showMessage("备份记录已删除。", "ok");
    }
    await refresh();
  } catch (error) {
    showMessage(error.message || "操作失败");
  }
}

function bindForm(selector, path, toPayload, successText) {
  const form = document.querySelector(selector);
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    await runAction(() => api(path, {method: "POST", body: JSON.stringify(toPayload(new FormData(form)))}), successText);
    form.reset();
    renderAccountFormMode();
  });
}

async function runAction(action, successText) {
  try {
    const result = await action();
    await refresh();
    showMessage(typeof successText === "function" ? successText(result) : successText, "ok");
  } catch (error) {
    showMessage(error.message || "操作失败");
  }
}

async function showPackageDetail(packageId) {
  const payload = await api(`/v1/admin/customer-data/${encodeURIComponent(packageId)}`);
  const item = payload.package || {};
  const detail = document.querySelector("#customer-data-detail");
  const summary = item.summary || {};
  const manifest = item.manifest || {};
  detail.classList.remove("is-hidden");
  detail.innerHTML = `
    <div class="package-detail-shell">
      <div class="package-detail-head">
        <div>
          <p class="eyebrow">客户数据包详情</p>
          <h3>${escapeHtml(displayCustomerName(item))}</h3>
        </div>
        <span class="status-chip ok">${formatBytes(item.bytes || 0)}</span>
      </div>
      <div class="package-detail-meta">
        <span><b>创建时间</b>${escapeHtml(item.created_at || "-")}</span>
        <span><b>备份编号</b>${escapeHtml(item.backup_id || "-")}</span>
        <span><b>文件位置</b>${escapeHtml(item.package_path || "-")}</span>
      </div>
    </div>
    <div class="metric-grid package-detail-metrics">${metricCards(packageMetrics(summary))}</div>
    <div class="package-detail-note">
      <div>
        <strong>这些数据是什么？</strong>
        <span>正式知识、商品专属知识、运行时知识检索资料和 AI经验池是客户本地客服系统使用的数据层。想看正文，请点“下载可读知识表”，会导出 Excel。</span>
        <span>底部的 path、sha256、bytes 是备份包技术清单，用于校验和还原，平时不需要阅读。</span>
      </div>
      <div class="button-row">
        <button class="primary-button" data-action="download-readable-package" data-package-id="${escapeAttr(item.package_id)}">下载可读知识表</button>
        <button class="secondary-button" data-action="download-package" data-package-id="${escapeAttr(item.package_id)}">下载原始备份包</button>
      </div>
    </div>
    <details class="record-row package-technical-list">
      <summary><strong>技术校验清单</strong><span>共 ${manifest.files?.length || 0} 个文件，平时不需要阅读。</span></summary>
      <div class="compact-table">${manifestRows(manifest)}</div>
    </details>
  `;
}

function showProposalDetail(proposalId) {
  const item = (state.data.proposals || []).find((proposal) => proposal.proposal_id === proposalId);
  if (!item) {
    showMessage("没有找到这条共享知识候选。");
    return;
  }
  const detail = document.querySelector("#shared-proposal-detail");
  const operations = item.operations || [];
  detail.classList.remove("is-hidden");
  detail.innerHTML = `
    <h3>共享知识候选详情</h3>
    <div class="record-row">
      <div class="row-title"><strong>${escapeHtml(item.title || item.proposal_id)}</strong><span class="status-chip ${proposalStatusChipClass(item.status)}">${proposalStatus(item.status)}</span></div>
      <span>来源客户：${escapeHtml(customerNameForTenant(item.tenant_id) || item.tenant_id || "-")} · 提交时间：${escapeHtml(item.created_at || "-")}</span>
      <span>${escapeHtml(item.summary || "没有额外说明")}</span>
      <p>${escapeHtml(proposalPreview(item))}</p>
      ${proposalReviewAssistDetailHtml(item)}
      <div class="button-row">
        <button class="secondary-button" data-action="refresh-proposal-assist" data-proposal-id="${escapeAttr(item.proposal_id)}" ${item.status !== "pending_review" ? "disabled" : ""}>AI复核建议</button>
        <button class="secondary-button" data-action="accept-proposal" data-proposal-id="${escapeAttr(item.proposal_id)}" ${item.status !== "pending_review" ? "disabled" : ""}>采纳并生成补丁</button>
        <button class="secondary-button" data-action="reject-proposal" data-proposal-id="${escapeAttr(item.proposal_id)}" ${item.status !== "pending_review" ? "disabled" : ""}>拒绝</button>
        <button class="secondary-button danger-button" data-action="void-proposal" data-proposal-id="${escapeAttr(item.proposal_id)}" ${item.status !== "pending_review" ? "disabled" : ""}>作废</button>
      </div>
    </div>
    <details class="record-row">
      <summary><strong>将生成的补丁内容</strong><span>共 ${operations.length} 个操作，平时只需确认路径和正文。</span></summary>
      ${operations.map((operation) => operationPreviewHtml(operation)).join("") || empty("没有可展示的补丁操作")}
    </details>
  `;
}

async function downloadPackage(packageId) {
  const response = await fetch(`/v1/admin/customer-data/${encodeURIComponent(packageId)}/download`, {headers: {Authorization: `Bearer ${state.token}`}});
  if (!response.ok) throw new Error("下载失败");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${packageId}.zip`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function downloadReadablePackage(packageId) {
  const response = await fetch(`/v1/admin/customer-data/${encodeURIComponent(packageId)}/readable-download`, {headers: {Authorization: `Bearer ${state.token}`}});
  if (!response.ok) throw new Error("可读知识表下载失败");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${packageId}_readable.xlsx`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function showLibraryDetail(itemId) {
  const payload = await api(`/v1/admin/shared/library/${encodeURIComponent(itemId)}`);
  const item = payload.item || {};
  const detail = document.querySelector("#shared-detail");
  detail.classList.remove("is-hidden");
  detail.innerHTML = `
    <h3>正式共享知识详情</h3>
    <div class="record-row">
      <div class="row-title"><strong>${escapeHtml(item.title || item.item_id)}</strong><span class="status-chip ${item.status === "active" ? "ok" : "warning"}">${escapeHtml(item.status || "-")}</span></div>
      <span>行业：${escapeHtml(industryLabel(item.industry_id))} · 分类：${escapeHtml(sharedCategoryLabel(item.category_id))} · 来源：${escapeHtml(item.source || "-")} · 更新：${escapeHtml(item.updated_at || "-")}</span>
      <span>适用场景：${escapeHtml(item.applies_to || "-")}</span>
      <span>关键词：${escapeHtml((item.keywords || []).join("、") || "-")}</span>
      <p>${escapeHtml(sharedItemContent(item))}</p>
      ${item.notes ? `<span>管理员备注：${escapeHtml(item.notes)}</span>` : ""}
      <div class="button-row">
        <button class="secondary-button" data-action="edit-library" data-item-id="${escapeAttr(item.item_id)}">编辑</button>
        <button class="secondary-button danger-button" data-action="delete-library" data-item-id="${escapeAttr(item.item_id)}">删除</button>
      </div>
    </div>
  `;
}

async function editLibraryItem(itemId) {
  const payload = await api(`/v1/admin/shared/library/${encodeURIComponent(itemId)}`);
  const item = payload.item || {};
  const titleValue = prompt("标题", item.title || "");
  if (titleValue === null) return;
  const contentValue = prompt("正文", sharedItemContent(item) || "");
  if (contentValue === null) return;
  await api(`/v1/admin/shared/library/${encodeURIComponent(itemId)}`, {method: "PATCH", body: JSON.stringify({title: titleValue, content: contentValue, status: item.status || "active"})});
  showMessage("共享知识已更新。", "ok");
  await refresh();
}

async function reviewProposal(proposalId, action, messageText) {
  await api(`/v1/admin/shared/proposals/${encodeURIComponent(proposalId)}/review`, {method: "POST", body: JSON.stringify({action})});
  document.querySelector("#shared-proposal-detail")?.classList.add("is-hidden");
  showMessage(messageText, "ok");
}

function operationPreviewHtml(operation) {
  const content = operation?.content || {};
  const data = content.data && typeof content.data === "object" ? content.data : content;
  const body = data.guideline_text || data.content || data.answer || data.body || JSON.stringify(content);
  return `
    <div class="record-row compact-row">
      <strong>${escapeHtml(operation?.path || "-")}</strong>
      <span>动作：${escapeHtml(operation?.op || "-")} · 标题：${escapeHtml(data.title || content.title || content.id || "-")}</span>
      <span>${escapeHtml(trimText(body, 220))}</span>
    </div>
  `;
}

function packageMetrics(summary) {
  const formal = summary.formal_knowledge || {};
  const product = summary.product_item_knowledge || {};
  const rag = summary.rag || {};
  return [
    ["正式知识", `${formal.item_count ?? 0} 条`],
    ["知识分类", `${formal.category_count ?? 0} 个`],
    ["商品专属文件", `${product.file_count ?? 0} 个`],
    ["RAG 原始资料", `${rag.sources?.json_file_count ?? 0} 个 JSON`],
    ["RAG 切片", `${rag.chunks?.json_file_count ?? 0} 个 JSON`],
    ["索引文件", `${rag.index?.file_count ?? 0} 个`],
  ];
}

function manifestRows(manifest) {
  const files = (manifest.files || []).slice(0, 80);
  if (!files.length) return empty("暂无技术清单");
  return files.map((file) => `
    <div class="record-row">
      <strong>${escapeHtml(file.path || "-")}</strong>
      <span>大小：${formatBytes(file.bytes || 0)} · 校验码：${escapeHtml(trimText(file.sha256 || "-", 24))}</span>
    </div>
  `).join("");
}

function proposalPreview(item) {
  const operation = (item.operations || []).find((value) => value && typeof value === "object") || {};
  const content = operation.content || {};
  const data = content.data || content;
  const title = data.title || content.title || content.id || item.summary || "";
  const body = data.guideline_text || data.content || content.content || "";
  return [title, body].filter(Boolean).join("：") || item.summary || "候选内容等待管理员查看";
}

function proposalReviewAssistSummaryHtml(item) {
  const assist = item.review_assist || {};
  if (!assist.summary) return `<span class="status-chip warning">等待 AI 审核建议</span>`;
  const chipClass = assist.recommendation === "accept" ? "ok" : assist.recommendation === "reject" ? "danger" : "warning";
  return `<span class="assist-line"><span class="status-chip ${chipClass}">${escapeHtml(assistRecommendationLabel(assist))}</span><span>${escapeHtml(assist.summary)}</span></span>`;
}

function proposalReviewAssistDetailHtml(item) {
  const assist = item.review_assist || {};
  if (!assist.summary) {
    return `
      <div class="assist-card">
        <strong>AI审核建议</strong>
        <span>还没有复核结果。点击“AI复核建议”后，系统会从通用性、重复性和风险三个角度给出建议。</span>
      </div>
    `;
  }
  const chipClass = assist.recommendation === "accept" ? "ok" : assist.recommendation === "reject" ? "danger" : "warning";
  const reasons = (assist.reasons || []).map((text) => `<li>${escapeHtml(text)}</li>`).join("");
  const risks = (assist.risks || []).map((text) => `<li>${escapeHtml(text)}</li>`).join("");
  const checklist = (assist.admin_checklist || []).map((text) => `<li>${escapeHtml(text)}</li>`).join("");
  const matches = (assist.existing_matches || []).map((match) => `
    <div class="mini-match">
      <strong>${escapeHtml(match.title || match.item_id || "已有共享知识")}</strong>
      <span>${escapeHtml(match.reason || "可能重复")} · ${escapeHtml(match.level || assist.duplicate_level || "possible")}</span>
    </div>
  `).join("");
  return `
    <div class="assist-card">
      <div class="row-title">
        <strong>AI审核建议</strong>
        <span class="status-chip ${chipClass}">${escapeHtml(assistRecommendationLabel(assist))}</span>
      </div>
      <span>${escapeHtml(assist.summary)}</span>
      <span>通用性评分：${escapeHtml(String(assist.universal_score ?? "-"))} · 重复程度：${escapeHtml(duplicateLevelLabel(assist.duplicate_level))} · ${assist.llm_used ? "已调用大模型" : "规则预判"}</span>
      ${reasons ? `<ul>${reasons}</ul>` : ""}
      ${risks ? `<strong>风险提醒</strong><ul>${risks}</ul>` : ""}
      ${matches ? `<strong>可能重合的正式共享知识</strong>${matches}` : ""}
      ${checklist ? `<strong>admin 确认清单</strong><ul>${checklist}</ul>` : ""}
    </div>
  `;
}

function assistRecommendationLabel(assist) {
  const prefix = assist?.llm_used ? "AI建议" : "系统预判";
  const action = {accept: "采纳", reject: "不采纳", revise: "先修改"}[assist?.recommendation] || "先修改";
  return `${prefix}：${action}`;
}

function duplicateLevelLabel(value) {
  return {none: "未发现明显重复", possible: "可能重复", high: "高度重复"}[value] || value || "-";
}

function proposalSourceLabel(item) {
  const source = String(item?.source || "");
  if (source.includes("formal_knowledge_universal_llm")) return "AI 从客户正式知识库提炼";
  if (source.includes("formal_knowledge_universal_heuristic")) return "系统从客户正式知识库预筛";
  if (source.includes("formal_knowledge_universal")) return "客户正式知识库提炼";
  if (source.includes("local")) return "客户端上传候选";
  return source || "候选共享公共知识";
}

function splitKeywords(value) {
  return String(value || "")
    .split(/[,，、;；\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function sharedItemContent(item) {
  const text = String(item?.content || "").trim();
  if (!text.startsWith("{")) return text;
  try {
    const payload = JSON.parse(text);
    const data = payload.data && typeof payload.data === "object" ? payload.data : payload;
    return data.guideline_text || data.content || data.answer || data.body || text;
  } catch {
    return text;
  }
}

function sharedCategoryLabel(value) {
  return {
    global_guidelines: "通用客服规则",
    reply_style: "回复口吻",
    after_sale: "售后规则",
    payment: "付款/开票",
    logistics: "物流发货",
    risk_control: "风险/转人工",
    product_common: "通用商品说明",
  }[value] || value || "-";
}

function commandRow(item) {
  return `
    <div class="record-row">
      <div class="row-title"><strong>${escapeHtml(commandTypeLabel(item.type))}</strong><span class="status-chip ${item.status === "succeeded" ? "ok" : "warning"}">${commandStatus(item.status)}</span></div>
      <span>${escapeHtml(item.command_id)} · 客户：${escapeHtml(customerNameForTenant(item.tenant_id) || "-")} · 节点：${escapeHtml(item.node_id || "任意")}</span>
    </div>
  `;
}

function customerUsers() {
  return (state.data.users || []).filter((user) => user.role === "customer");
}

function guestUsers() {
  return (state.data.users || []).filter((user) => user.role === "guest");
}

function tenantIdsForCustomer(username) {
  const user = customerUsers().find((item) => item.username === username);
  return user?.tenant_ids?.length ? user.tenant_ids : [username].filter(Boolean);
}

function customerNameForTenant(tenantId) {
  const tenant = String(tenantId || "");
  const user = customerUsers().find((item) => (item.tenant_ids || []).includes(tenant));
  return user?.username || tenant;
}

function accountAccessText(item) {
  if (item.role === "customer") return "权限：只能访问和修改自己的客户数据";
  const names = item.authorized_customers || (item.tenant_ids || []).map(customerNameForTenant);
  return `权限：只能查看 ${escapeHtml(names.join("，") || "-")}，不能修改`;
}

function customerIndustryText(item) {
  if (item.role !== "customer") return "行业：继承被授权客户";
  const tenantIndustries = item.tenant_industries || {};
  const labels = (item.tenant_ids || []).map((tenantId) => industryLabel(tenantIndustries[tenantId])).filter(Boolean);
  const unique = [...new Set(labels)];
  return `行业：${escapeHtml(unique.join("，") || "-")}`;
}

function industryLabel(industryId) {
  const value = String(industryId || "").trim();
  if (!value) return "-";
  if (value === "global") return "全行业通用";
  const match = (state.data.industries || []).find((item) => item.industry_id === value);
  return match?.name || value;
}

function displayCustomerName(item) {
  return item.account_username || customerNameForTenant(item.tenant_id) || item.tenant_id || "未绑定客户";
}

function metricCards(items) {
  return items.map(([label, value]) => `<div class="metric-card"><span>${escapeHtml(value)}</span><label>${escapeHtml(label)}</label></div>`).join("");
}

function renderInto(id, html) {
  const element = document.getElementById(id);
  if (element) element.innerHTML = html;
}

function empty(text) {
  return `<div class="empty-state">${escapeHtml(text)}</div>`;
}

function showMessage(text, type = "warning") {
  message.textContent = text;
  message.className = `status-card ${type === "ok" ? "ok" : "warning"}`;
  message.classList.remove("is-hidden");
}

function hideMessage() {
  message.textContent = "";
  message.className = "status-card warning is-hidden";
}

function roleLabel(value) {
  return value === "customer" ? "客户" : value === "guest" ? "访客" : value || "-";
}

function scopeLabel(value) {
  return {tenant: "客户数据", shared: "共享知识", all: "全部数据"}[value] || value || "-";
}

function backupStatus(value) {
  return {queued: "已排队", sent: "已下发", succeeded: "已完成", failed: "失败"}[value] || value || "-";
}

function commandStatus(value) {
  return {queued: "待执行", sent: "已下发", succeeded: "已完成", failed: "失败"}[value] || value || "-";
}

function commandTypeLabel(value) {
  return {backup_all: "备份全部数据", backup_tenant: "备份客户数据", restore_backup: "还原备份", pull_shared_patch: "刷新云端共享知识", check_update: "检查更新", push_update: "推送更新"}[value] || value || "-";
}

function channelLabel(value) {
  return {stable: "稳定版", canary: "灰度版", dev: "开发版"}[value] || value || "-";
}

function proposalStatus(value) {
  return {pending_review: "待审核", accepted: "已采纳", rejected: "已拒绝", void: "已作废"}[value] || value || "-";
}

function proposalStatusChipClass(value) {
  return {pending_review: "warning", accepted: "ok", rejected: "muted", void: "muted"}[value] || "warning";
}

function actionLabel(value) {
  return {
    login: "登录",
    start_email_login: "发送登录验证码",
    verify_email_login: "验证登录邮箱",
    change_password: "修改密码",
    create_tenant: "创建客户范围",
    update_tenant: "更新客户范围",
    create_user: "创建账号",
    update_user: "更新账号",
    delete_user: "删除账号",
    bootstrap_test01_customer: "生成 test01 客户",
    package_customer_data: "打包客户数据",
    delete_customer_data_package: "删除客户数据包",
    register_node: "注册客户电脑",
    create_command: "创建命令",
    command_result: "命令回传",
    sync_shared_knowledge: "同步共享知识快照",
    submit_shared_proposal: "提交共享候选",
    accept_shared_proposal: "采纳共享候选",
    reject_shared_proposal: "拒绝共享候选",
    void_shared_proposal: "作废共享候选",
    create_shared_library_item: "新增正式共享知识",
    update_shared_library_item: "更新正式共享知识",
    delete_shared_library_item: "删除正式共享知识",
    local_backup_now: "立即备份",
    request_backup: "请求备份",
    delete_backup_request: "删除备份记录",
    request_restore: "请求还原",
    create_release: "登记版本",
  }[value] || value || "-";
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function trimText(value, length) {
  const text = String(value ?? "");
  return text.length > length ? `${text.slice(0, length)}...` : text;
}

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("'", "&#39;");
}
