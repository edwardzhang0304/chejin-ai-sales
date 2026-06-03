const API_BASE =
  new URLSearchParams(window.location.search).get("api") ||
  window.CHEJIN_API_BASE ||
  "http://127.0.0.1:8000/api";

const OPERATOR_HEADERS = {
  "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
  "X-Operator-Role": "admin",
};

const statusMeta = {
  assigned: { label: "已分配", className: "assigned" },
  unassigned: { label: "未分配", className: "unassigned" },
  invalid: { label: "无效", className: "invalid" },
};

const state = {
  apiReady: true,
  module: null,
  leadsLoaded: false,
  salesLoaded: false,
  leads: [],
  sales: [],
  selectedIds: new Set(),
  activeLeadId: null,
  pendingRestoreLeadId: null,
  invalidMode: "single",
  editingLeadId: null,
  page: 1,
  pageSize: 20,
  total: 0,
};

const workspace = document.querySelector("[data-workspace]");
const emptyWorkspace = document.querySelector("[data-empty-workspace]");
const moduleButtons = document.querySelectorAll("[data-module]");
const moduleContents = document.querySelectorAll("[data-module-content]");
const drawer = document.querySelector("[data-drawer]");
const toast = document.querySelector("[data-toast]");
const staticSnapshot = {
  table: document.querySelector("[data-leads-table]")?.innerHTML || "",
  sales: document.querySelector("[data-sales-grid]")?.innerHTML || "",
  total: document.querySelector("[data-total-count]")?.textContent || "128",
  pagination: document.querySelector("[data-pagination]")?.innerHTML || "",
  selectedText: document.querySelector("[data-selected-count]")?.textContent || "已选 2 条",
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
  const salesGrid = document.querySelector("[data-sales-grid]");
  const total = document.querySelector("[data-total-count]");
  const pagination = document.querySelector("[data-pagination]");
  const selected = document.querySelector("[data-selected-count]");
  if (tbody && staticSnapshot.table) tbody.innerHTML = staticSnapshot.table;
  if (salesGrid && staticSnapshot.sales) salesGrid.innerHTML = staticSnapshot.sales;
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
    today_new_count: ["今日新增", "人工录入"],
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
    tbody.innerHTML = `<tr><td colspan="9">暂无线索，调整筛选条件后重试。</td></tr>`;
    state.selectedIds.clear();
    updateSelectedCount();
    renderPagination();
    return;
  }

  tbody.innerHTML = state.leads
    .map((lead) => {
      const status = getStatus(lead.status);
      const checked = state.selectedIds.has(lead.id) ? "checked" : "";
      const source = lead.source_name_snapshot || (lead.source_type === "manual" ? "人工录入" : lead.source_type || "未知来源");
      const contactSub = lead.primary_wechat_masked || "未填写微信";
      const duplicate =
        lead.duplicate_count > 0
          ? `<button class="link-button" type="button" data-action="open-detail" data-lead-id="${escapeHtml(lead.id)}">${lead.duplicate_count} 次</button>`
          : "0 次";
      const action =
        lead.status === "invalid"
          ? `<button class="link-button" type="button" data-open-modal="restore" data-lead-id="${escapeHtml(lead.id)}">恢复有效</button>`
          : `<button class="link-button" type="button" data-action="open-detail" data-lead-id="${escapeHtml(lead.id)}">查看详情</button>`;

      return `
        <tr class="${checked ? "selected" : ""}">
          <td><input type="checkbox" ${checked} aria-label="选择${escapeHtml(lead.customer_name)}" data-select-lead="${escapeHtml(lead.id)}" /></td>
          <td class="lead-cell"><strong>${escapeHtml(lead.customer_name)}</strong><small>${escapeHtml(source)} · ${escapeHtml(lead.created_by_name || "运营")}</small></td>
          <td class="contact-cell"><strong>${escapeHtml(lead.primary_phone_masked || "未填写")}</strong><small>${escapeHtml(contactSub)}</small></td>
          <td><span class="status ${status.className}">${status.label}</span></td>
          <td>${escapeHtml(lead.sales_name || "暂无")}</td>
          <td>${duplicate}</td>
          <td>${escapeHtml(lead.remark_summary || "暂无备注")}</td>
          <td>${formatDate(lead.updated_at)}</td>
          <td>${action}</td>
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
  if (tbody) tbody.innerHTML = `<tr><td colspan="9">正在加载线索...</td></tr>`;
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
    if (state.leads[0]) await loadLeadDetail(state.activeLeadId || state.leads[0].id);
  } catch (error) {
    if (!state.leadsLoaded) markApiUnavailable(error);
    else showToast(error.message || "线索列表加载失败", "error");
  }
};

const loadLeadDetail = async (leadId) => {
  if (!leadId) return;
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
};

const renderSales = (items) => {
  state.sales = items || [];
  const grid = document.querySelector("[data-sales-grid]");
  if (!grid) return;
  if (!state.sales.length) {
    grid.innerHTML = `<article><header><h3>暂无销售</h3></header><p>新增销售后可参与轮询分配。</p></article>`;
    return;
  }
  grid.innerHTML = state.sales
    .map((sales) => {
      const enabled = sales.enabled;
      const participates = sales.participate_in_round_robin;
      const statusClass = enabled ? "assigned" : "invalid";
      const statusLabel = enabled ? "启用" : "停用";
      const roundRobinLabel = participates ? "参与轮询" : "不参与轮询";
      return `
        <article>
          <header>
            <h3>${escapeHtml(sales.sales_name)}</h3>
            <span class="status ${statusClass}">${statusLabel}</span>
          </header>
          <p>${roundRobinLabel} · 排序 ${sales.sort_order ?? "-"} · 名下线索 ${sales.lead_count ?? 0}</p>
          <label aria-label="${escapeHtml(sales.sales_name)}${roundRobinLabel}">
            <input type="checkbox" ${participates ? "checked" : ""} ${enabled ? "" : "disabled"} data-action="toggle-sales-round-robin" data-sales-id="${escapeHtml(sales.id)}" />
            ${roundRobinLabel}
          </label>
        </article>`;
    })
    .join("");
};

const loadSales = async () => {
  const grid = document.querySelector("[data-sales-grid]");
  if (grid) grid.innerHTML = `<article><header><h3>正在加载销售...</h3></header><p>请稍候。</p></article>`;
  try {
    const data = await apiJson("/sales");
    state.apiReady = true;
    renderSales(data.items || []);
    state.salesLoaded = true;
  } catch (error) {
    if (!state.salesLoaded) markApiUnavailable(error);
    else showToast(error.message || "销售列表加载失败", "error");
  }
};

const showModule = async (moduleName) => {
  state.module = moduleName;
  workspace.classList.remove("is-empty");
  emptyWorkspace.hidden = true;

  moduleButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.module === moduleName);
  });

  moduleContents.forEach((content) => {
    content.hidden = content.dataset.moduleContent !== moduleName;
  });

  if (moduleName === "leads" && !state.leadsLoaded) await loadLeads();
  if (moduleName === "sales" && !state.salesLoaded) await loadSales();
};

const closeModal = (modal) => {
  const backdrop = modal?.closest(".modal-backdrop") || modal;
  if (backdrop) backdrop.hidden = true;
};

const openModal = (name) => {
  const modal = document.querySelector(`[data-modal="${name}"]`);
  if (modal) modal.hidden = false;
};

const resetLeadForm = () => {
  state.editingLeadId = null;
  document.querySelector('[data-lead-field="customer_name"]').value = "";
  document.querySelector('[data-lead-field="car_type"]').value = "";
  document.querySelector('[data-lead-field="remark"]').value = "";
  document.querySelectorAll("[data-contact-value]").forEach((input, index) => {
    input.value = index === 0 ? "" : "";
  });
  document.querySelectorAll("[data-custom-value]").forEach((input) => {
    input.value = "";
  });
  const note = document.querySelector("[data-duplicate-note] p");
  if (note) note.textContent = "输入手机号后会自动预查重复；最终保存仍由后端事务内查重。";
};

const fillLeadForm = (lead) => {
  state.editingLeadId = lead.id;
  document.querySelector('[data-lead-field="customer_name"]').value = lead.customer_name || "";
  document.querySelector('[data-lead-field="car_type"]').value = lead.custom_fields?.car_type || "";
  document.querySelector('[data-lead-field="remark"]').value = lead.remark || "";
  const contactRows = [...document.querySelectorAll(".contact-editor")];
  contactRows.forEach((row, index) => {
    const contact = (lead.contacts || [])[index];
    if (!contact) return;
    row.querySelector("[data-contact-type]").value = contact.contact_type;
    row.querySelector("[data-contact-value]").value = contact.masked_value;
  });
};

const collectLeadPayload = () => {
  const customerName = document.querySelector('[data-lead-field="customer_name"]').value.trim();
  const remark = document.querySelector('[data-lead-field="remark"]').value.trim();
  const carType = document.querySelector('[data-lead-field="car_type"]').value.trim();
  const contacts = { phones: [], wechats: [], emails: [] };

  document.querySelectorAll(".contact-editor").forEach((row) => {
    const type = row.querySelector("[data-contact-type]")?.value;
    const value = row.querySelector("[data-contact-value]")?.value.trim();
    if (!value) return;
    if (type === "phone") contacts.phones.push(value);
    if (type === "wechat") contacts.wechats.push(value);
    if (type === "email") contacts.emails.push(value);
  });

  const customFields = {};
  if (carType) customFields.car_type = carType;
  document.querySelectorAll(".custom-field-row").forEach((row) => {
    const key = row.querySelector("[data-custom-key]")?.value.trim();
    const value = row.querySelector("[data-custom-value]")?.value.trim();
    if (key && value) customFields[key] = value;
  });

  if (!customerName) throw new Error("请填写客户名称");
  if (!contacts.phones.length) throw new Error("请至少填写一个手机号");
  return { customer_name: customerName, ...contacts, remark, custom_fields: customFields };
};

const saveLead = async (shouldContinue, button) => {
  await setButtonBusy(button, async () => {
    const payload = collectLeadPayload();
    const path = state.editingLeadId ? `/leads/${state.editingLeadId}` : "/leads";
    const method = state.editingLeadId ? "PUT" : "POST";
    try {
      const data = await apiJson(path, { method, body: JSON.stringify(payload) });
      showToast(state.editingLeadId ? "线索已更新" : "客户线索已新增", "success");
      state.page = 1;
      state.leadsLoaded = false;
      await loadLeads();
      await loadLeadDetail(data.id || data.lead?.id || state.editingLeadId);
      if (!shouldContinue) closeModal(document.querySelector('[data-modal="lead"]'));
      if (shouldContinue) resetLeadForm();
    } catch (error) {
      if (error.code === "LEAD_PHONE_DUPLICATED") {
        const note = document.querySelector("[data-duplicate-note] p");
        if (note) note.textContent = error.message;
      }
      showToast(error.message || "保存失败", "error");
    }
  });
};

const duplicatePreview = async () => {
  const phones = [...document.querySelectorAll(".contact-editor")]
    .filter((row) => row.querySelector("[data-contact-type]")?.value === "phone")
    .map((row) => row.querySelector("[data-contact-value]")?.value.trim())
    .filter(Boolean);
  if (!phones.length) return;
  try {
    const data = await apiJson("/leads/duplicate-preview", { method: "POST", body: JSON.stringify({ phones }) });
    const hit = (data.items || []).find((item) => item.duplicated);
    const note = document.querySelector("[data-duplicate-note] p");
    if (note) {
      note.textContent = hit
        ? `发现重复手机号：${hit.phone_masked}，原线索 ${hit.customer_name || hit.lead_id}，保存时会追加备注。`
        : "未发现活跃重复手机号。保存时后端仍会再次查重。";
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

const toggleSalesRoundRobin = async (input) => {
  const sales = state.sales.find((item) => item.id === input.dataset.salesId);
  if (!sales) return;
  const next = { ...sales, participate_in_round_robin: input.checked };
  try {
    await apiJson(`/sales/${sales.id}`, {
      method: "PUT",
      body: JSON.stringify({
        sales_name: next.sales_name,
        enabled: next.enabled,
        participate_in_round_robin: next.participate_in_round_robin,
        sort_order: next.sort_order,
      }),
    });
    showToast("销售轮询配置已更新", "success");
    await loadSales();
  } catch (error) {
    input.checked = !input.checked;
    showToast(error.message || "更新销售失败", "error");
  }
};

const createSales = async () => {
  const salesName = window.prompt("请输入销售姓名");
  if (!salesName) return;
  const sortOrder = Number(window.prompt("请输入轮询排序", "10") || "10");
  try {
    await apiJson("/sales", {
      method: "POST",
      body: JSON.stringify({
        sales_name: salesName,
        enabled: true,
        participate_in_round_robin: true,
        sort_order: Number.isNaN(sortOrder) ? 10 : sortOrder,
      }),
    });
    showToast("销售已新增", "success");
    await loadSales();
  } catch (error) {
    showToast(error.message || "新增销售失败", "error");
  }
};

moduleButtons.forEach((button) => {
  button.addEventListener("click", () => showModule(button.dataset.module));
});

let filterTimer;
document.addEventListener("input", (event) => {
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
    if (event.target.matches("[data-contact-value], [data-contact-type]")) {
      duplicatePreview();
    }
    if (event.target.matches('[data-action="toggle-sales-round-robin"]')) {
      toggleSalesRoundRobin(event.target);
    }
  },
  true
);

document.addEventListener("click", (event) => {
  const actionEl = event.target.closest("[data-action]");
  const openModalButton = event.target.closest("[data-open-modal]");

  if (openModalButton) {
    const modalName = openModalButton.dataset.openModal;
    if (modalName === "lead" && openModalButton.dataset.action === "reset-lead-form") resetLeadForm();
    if (modalName === "invalid") state.invalidMode = openModalButton.dataset.invalidMode || "single";
    if (modalName === "restore") state.pendingRestoreLeadId = openModalButton.dataset.leadId || state.activeLeadId;
    openModal(modalName);
  }

  if (event.target.closest("[data-close-modal]")) {
    closeModal(event.target.closest(".modal-backdrop"));
  }

  if (event.target.closest("[data-close-drawer]")) {
    drawer.classList.add("closed");
  }

  if (event.target.classList.contains("modal-backdrop")) {
    event.target.hidden = true;
  }

  if (event.target.matches("[data-page]")) {
    state.page = Number(event.target.dataset.page);
    loadLeads();
  }

  if (!actionEl) return;
  const action = actionEl.dataset.action;
  if (action === "open-detail") loadLeadDetail(actionEl.dataset.leadId);
  if (action === "save-lead") saveLead(false, actionEl);
  if (action === "save-lead-continue") saveLead(true, actionEl);
  if (action === "confirm-invalid") confirmInvalid(actionEl);
  if (action === "confirm-restore") confirmRestore(actionEl);
  if (action === "retry-assign") retryAssign(actionEl);
  if (action === "export-selected") exportSelected(actionEl);
  if (action === "reveal-phone") revealPhone(actionEl.dataset.contactId, actionEl);
  if (action === "copy-contact") {
    navigator.clipboard?.writeText(actionEl.dataset.copyValue || "");
    showToast("已复制", "success");
  }
  if (action === "edit-active-lead" && state.activeLead) {
    fillLeadForm(state.activeLead);
    openModal("lead");
  }
  if (action === "new-sales") createSales();
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  document.querySelectorAll(".modal-backdrop:not([hidden])").forEach((modal) => {
    modal.hidden = true;
  });
  drawer.classList.add("closed");
});

const initialModule = new URLSearchParams(window.location.search).get("module");
if ([...moduleButtons].some((button) => button.dataset.module === initialModule)) {
  showModule(initialModule);
}
