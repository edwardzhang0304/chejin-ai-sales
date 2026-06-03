const fs = require("fs");
const path = require("path");
const { chromium } = require("/Users/zhangwentao/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright");

const chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const root = "/Users/zhangwentao/Documents/车金";
const outputPath = path.join(root, "deliverables", "test_runs", "vite_frontend_smoke_result.json");
const appUrl = "http://127.0.0.1:5173/";
const apiBase = "http://127.0.0.1:8000/api";
const headers = {
  "Content-Type": "application/json",
  "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
  "X-Operator-Name": "Ops Tester",
  "X-Operator-Role": "admin",
};

async function api(pathname, init = {}) {
  const response = await fetch(`${apiBase}${pathname}`, {
    ...init,
    headers: { ...headers, ...(init.headers || {}) },
  });
  const payload = await response.json();
  return { status: response.status, payload };
}

(async () => {
  const stamp = String(Date.now()).slice(-8);
  const phone = `139${stamp}`;
  const batchPrefix = `Vite批量无效${stamp}`;
  const seed = await api("/leads", {
    method: "POST",
    body: JSON.stringify({
      customer_name: `Vite重复回归种子${stamp}`,
      phones: [phone],
      remark: "Vite 前端重复 409 回归种子",
    }),
  });
  const batchSeedA = await api("/leads", {
    method: "POST",
    body: JSON.stringify({
      customer_name: `${batchPrefix}A`,
      phones: [`138${stamp}`],
      remark: "Vite 前端批量操作种子 A",
    }),
  });
  const batchSeedB = await api("/leads", {
    method: "POST",
    body: JSON.stringify({
      customer_name: `${batchPrefix}B`,
      phones: [`137${stamp}`],
      remark: "Vite 前端批量操作种子 B",
    }),
  });

  const browser = await chromium.launch({ headless: true, executablePath: chrome });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const browserErrors = [];
  page.on("console", (msg) => {
    if (["error", "warning"].includes(msg.type())) browserErrors.push({ type: msg.type(), text: msg.text() });
  });
  page.on("pageerror", (err) => browserErrors.push({ type: "pageerror", text: err.message }));

  const results = [];
  await page.goto(appUrl, { waitUntil: "networkidle" });

  const emptyState = await page.evaluate(() => ({
    hasEmptyText: document.body.innerText.includes("选择左侧模块"),
    activeModuleCount: document.querySelectorAll(".module-item.active").length,
    hasPlaceholderModules: document.body.innerText.includes("批量导入") || document.body.innerText.includes("AI 跟进"),
    hasStaticModuleBadges: [...document.querySelectorAll(".module-item strong")].some((node) => ["12", "4"].includes(node.textContent?.trim() || "")),
  }));
  results.push({
    case_id: "VITE-001",
    name: "默认空白工作区",
    passed: emptyState.hasEmptyText && emptyState.activeModuleCount === 0 && !emptyState.hasPlaceholderModules && !emptyState.hasStaticModuleBadges,
    actual: emptyState,
    expected: "首次进入不默认选中模块，展示空白工作区，不展示未实现占位入口和静态数字 badge。",
  });

  await page.getByRole("button", { name: /线索管理/ }).click();
  await page.waitForTimeout(3000);
  const listState = await page.evaluate(() => ({
    rowCount: document.querySelectorAll("tbody tr").length,
    totalText: [...document.querySelectorAll(".pagination-total span")].map((el) => el.textContent).join(" "),
    pageButtonCount: document.querySelectorAll(".pagination-row nav button, .pagination-row nav strong").length,
    hasCreateButton: document.body.innerText.includes("新增客户"),
    hasTodaySuccessRate: document.body.innerText.includes("今日轮询成功率"),
    hasListError: document.body.innerText.includes("线索列表加载失败"),
  }));
  results.push({
    case_id: "VITE-002",
    name: "线索列表加载",
    passed: listState.rowCount > 0 && listState.pageButtonCount >= 4 && listState.hasCreateButton && listState.hasTodaySuccessRate && !listState.hasListError,
    actual: listState,
    expected: "点击线索管理后从后端加载列表、分页页码组、今日轮询成功率并展示新增入口。",
  });

  const salesFilterBefore = await page.evaluate(() => ({
    options: [...document.querySelectorAll("select[aria-label='筛选销售'] option")].map((option) => ({
      value: option.value,
      text: option.textContent?.trim(),
    })),
  }));
  const firstSalesOption = salesFilterBefore.options.find((option) => option.value);
  if (firstSalesOption) {
    await page.locator("select[aria-label='筛选销售']").selectOption(firstSalesOption.value);
    await page.waitForTimeout(1200);
  }
  const salesFilterState = await page.evaluate((selectedName) => ({
    selected: document.querySelector("select[aria-label='筛选销售']")?.value || "",
    rowCount: document.querySelectorAll("tbody tr").length,
    salesCells: [...document.querySelectorAll("tbody tr td:nth-child(5)")].map((cell) => cell.textContent?.trim()).slice(0, 10),
    allVisibleMatch: [...document.querySelectorAll("tbody tr td:nth-child(5)")].every((cell) => cell.textContent?.trim() === selectedName),
  }), firstSalesOption?.text || "");
  results.push({
    case_id: "VITE-010",
    name: "销售筛选可操作",
    passed: Boolean(firstSalesOption) && salesFilterState.selected === firstSalesOption.value && salesFilterState.rowCount > 0 && salesFilterState.allVisibleMatch,
    actual: { salesFilterBefore, selectedOption: firstSalesOption, salesFilterState },
    expected: "销售筛选下拉加载真实销售，选择后列表仅展示该销售线索。",
  });

  await page.locator("select[aria-label='筛选销售']").selectOption("");
  await page.waitForTimeout(1200);
  await page.locator("select[aria-label='每页条数']").selectOption("50");
  await page.waitForTimeout(1200);
  const pageSizeState = await page.evaluate(() => ({
    selected: document.querySelector("select[aria-label='每页条数']")?.value || "",
    rowCount: document.querySelectorAll("tbody tr").length,
    totalText: document.querySelector(".pagination-total")?.textContent?.replace(/\s+/g, " ").trim() || "",
  }));
  results.push({
    case_id: "VITE-011",
    name: "每页条数可操作",
    passed: pageSizeState.selected === "50" && pageSizeState.rowCount > 20 && pageSizeState.rowCount <= 50 && pageSizeState.totalText.includes("50条/页"),
    actual: pageSizeState,
    expected: "每页条数可切换为 50，列表刷新为最多 50 行。",
  });

  await page.locator("select[aria-label='每页条数']").selectOption("20");
  await page.waitForTimeout(1200);

  let detailState = { drawerTitle: "", hasContactSection: false, hasTaskNodes: false, skipped: false };
  if (listState.rowCount > 0) {
    await page.locator("tbody tr").first().click();
    await page.waitForFunction(
      () => {
        const title = document.querySelector(".detail-drawer h2")?.textContent?.trim();
        const drawerText = document.querySelector(".detail-drawer")?.textContent || "";
        return Boolean(title && title !== "加载中" && drawerText.includes("任务链路"));
      },
      { timeout: 10000 },
    );
    detailState = await page.evaluate(() => ({
      drawerTitle: document.querySelector(".detail-drawer h2")?.textContent?.trim() || "",
      hasContactSection: document.body.innerText.includes("联系方式"),
      hasTaskNodes: document.body.innerText.includes("任务链路"),
      skipped: false,
    }));
  } else {
    detailState = { drawerTitle: "", hasContactSection: false, hasTaskNodes: false, skipped: true };
  }
  results.push({
    case_id: "VITE-003",
    name: "详情抽屉加载",
    passed: Boolean(detailState.drawerTitle) && detailState.drawerTitle !== "加载中" && detailState.hasContactSection && detailState.hasTaskNodes,
    actual: detailState,
    expected: "点击列表行后详情抽屉展示客户、联系方式和任务链路。",
  });

  const tabNames = ["备注", "重复记录", "分配记录", "概览"];
  const tabStates = [];
  for (const tabName of tabNames) {
    await page.locator(".detail-drawer").getByRole("tab", { name: tabName, exact: true }).click();
    await page.waitForTimeout(300);
    tabStates.push(await page.evaluate((name) => ({
      clicked: name,
      selectedText: document.querySelector(".tabs [aria-selected='true']")?.textContent?.trim() || "",
      drawerText: document.querySelector(".drawer-body")?.textContent?.replace(/\s+/g, " ").trim().slice(0, 240) || "",
    }), tabName));
  }
  results.push({
    case_id: "VITE-012",
    name: "详情 Tabs 可操作",
    passed: tabStates.every((state) => state.selectedText === state.clicked),
    actual: tabStates,
    expected: "详情抽屉概览、备注、重复记录、分配记录 tabs 点击后可切换 active 和内容。",
  });

  await page.getByRole("button", { name: "新增客户" }).click();
  await page.getByLabel("客户名称 *").fill(`Vite重复客户${stamp}`);
  await page.getByLabel("手机 *").fill(phone);
  await page.getByLabel("备注内容").fill("通过 Vite 前端提交重复手机号");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.waitForSelector(".duplicate-alert", { timeout: 10000 });
  const duplicateState = await page.evaluate(() => ({
    alertText: document.querySelector(".duplicate-alert")?.textContent?.trim(),
    hasOpenOriginal: document.body.innerText.includes("查看原线索"),
    modalStillOpen: Boolean(document.querySelector("form[aria-label='新增客户']")),
    hasErrorCode: document.body.innerText.includes("错误码：LEAD_PHONE_DUPLICATED"),
    hasTraceId: document.body.innerText.includes("Trace ID："),
    alertRect: (() => {
      const rect = document.querySelector(".duplicate-alert")?.getBoundingClientRect();
      return rect ? { top: rect.top, bottom: rect.bottom } : null;
    })(),
    firstSectionRect: (() => {
      const rect = document.querySelector(".create-lead-modal .form-section")?.getBoundingClientRect();
      return rect ? { top: rect.top, bottom: rect.bottom } : null;
    })(),
  }));
  results.push({
    case_id: "VITE-004",
    name: "重复手机号 409 提示",
    passed:
      duplicateState.modalStillOpen &&
      duplicateState.alertText?.includes("该手机号已存在，不能重复新建") &&
      duplicateState.alertText?.includes("本次备注将追加到原线索") &&
      duplicateState.hasOpenOriginal &&
      duplicateState.hasErrorCode &&
      duplicateState.hasTraceId &&
      duplicateState.alertRect &&
      duplicateState.firstSectionRect &&
      duplicateState.alertRect.bottom <= duplicateState.firstSectionRect.top,
    actual: duplicateState,
    expected: "重复手机号提交后弹窗保留，展示后端 409 业务错误和原线索信息，提示区域不遮挡基础信息。",
  });

  await page.getByRole("button", { name: "取消" }).click();

  page.once("dialog", (dialog) => dialog.accept("Vite 冒烟查看手机号明文"));
  await page.getByRole("button", { name: "查看完整" }).first().click();
  await page.waitForFunction(() => document.body.innerText.includes("手机号明文："), { timeout: 10000 });
  const revealState = await page.evaluate(() => ({
    hasRevealMessage: document.body.innerText.includes("手机号明文："),
    hasAuditMessage: document.body.innerText.includes("审计日志"),
    contactValues: [...document.querySelectorAll(".detail-drawer .contact-row strong")].map((node) => node.textContent?.trim() || ""),
    hasFullPhoneInContactRow: [...document.querySelectorAll(".detail-drawer .contact-row strong")].some((node) => /^1\d{10}$/.test(node.textContent?.trim() || "")),
  }));
  results.push({
    case_id: "VITE-005",
    name: "手机号明文查看",
    passed: revealState.hasRevealMessage && revealState.hasAuditMessage && revealState.hasFullPhoneInContactRow,
    actual: revealState,
    expected: "点击查看完整手机号后要求填写原因，并在联系方式区域展示 11 位明文手机号，同时展示审计提示。",
  });

  await page.getByLabel("搜索").fill(batchPrefix);
  await page.waitForFunction(() => document.querySelectorAll("tbody tr").length >= 2, { timeout: 10000 });
  await page.getByLabel("选择当前页线索").check();
  const [download] = await Promise.all([
    page.waitForEvent("download", { timeout: 10000 }),
    page.getByRole("button", { name: "导出", exact: true }).click(),
  ]);
  const exportState = {
    suggestedFilename: download.suggestedFilename(),
    hasExportMessage: await page.evaluate(() => document.body.innerText.includes("已导出")),
  };
  results.push({
    case_id: "VITE-006",
    name: "导出选中线索",
    passed: exportState.suggestedFilename.endsWith(".csv") && exportState.hasExportMessage,
    actual: exportState,
    expected: "勾选线索后可导出 CSV，并展示导出成功提示。",
  });

  await page.locator(".bulk-row").getByRole("button", { name: "标记无效", exact: true }).click();
  await page.getByLabel("无效原因").selectOption("test_data");
  await page.getByLabel("补充说明").fill("Vite 冒烟批量标记无效");
  await page.getByRole("button", { name: "确认标记无效" }).click();
  await page.waitForFunction(() => document.body.innerText.includes("已标记 2 条线索为无效"), { timeout: 10000 });
  const batchInvalidState = await page.evaluate(() => ({
    hasSuccessMessage: document.body.innerText.includes("已标记 2 条线索为无效"),
    selectedText: [...document.querySelectorAll(".bulk-row span")].map((el) => el.textContent).join(" "),
  }));
  results.push({
    case_id: "VITE-007",
    name: "批量标记无效",
    passed: batchInvalidState.hasSuccessMessage,
    actual: batchInvalidState,
    expected: "勾选多条线索后可批量标记无效并展示成功提示。",
  });

  await page.getByLabel("搜索").fill(`Vite重复回归种子${stamp}`);
  await page.waitForFunction(() => document.querySelectorAll("tbody tr").length >= 1, { timeout: 10000 });
  await page.locator("tbody tr").first().click();
  await page.waitForFunction(
    () => {
      const title = document.querySelector(".detail-drawer h2")?.textContent?.trim();
      return Boolean(title && title !== "加载中" && document.querySelector(".detail-drawer")?.textContent?.includes("标记无效"));
    },
    { timeout: 10000 },
  );
  await page.locator(".detail-drawer").getByRole("button", { name: "标记无效", exact: true }).click();
  await page.getByLabel("无效原因").selectOption("test_data");
  await page.getByLabel("补充说明").fill("Vite 冒烟单条标记无效");
  await page.getByRole("button", { name: "确认标记无效" }).click();
  await page.waitForFunction(() => document.body.innerText.includes("已标记 1 条线索为无效"), { timeout: 10000 });
  const hasInvalidMessage = await page.evaluate(() => document.body.innerText.includes("已标记 1 条线索为无效"));
  await page.locator(".detail-drawer").getByRole("button", { name: "恢复有效", exact: true }).click();
  await page.waitForFunction(() => document.body.innerText.includes("恢复为有效线索"), { timeout: 10000 });
  const hasRestoreConfirm = await page.evaluate(() => document.body.innerText.includes("确认恢复有效"));
  await page.getByRole("button", { name: "确认恢复有效" }).click();
  await page.waitForFunction(() => document.body.innerText.includes("已恢复为有效线索"), { timeout: 10000 });
  const invalidRestoreState = await page.evaluate(
    ({ invalidMessageSeen, restoreConfirmSeen }) => ({
      hasInvalidMessage: invalidMessageSeen,
      hasRestoreConfirm: restoreConfirmSeen,
      hasRestoreMessage: document.body.innerText.includes("已恢复为有效线索"),
    }),
    { invalidMessageSeen: hasInvalidMessage, restoreConfirmSeen: hasRestoreConfirm },
  );
  results.push({
    case_id: "VITE-008",
    name: "单条无效与恢复",
    passed: invalidRestoreState.hasInvalidMessage && invalidRestoreState.hasRestoreConfirm && invalidRestoreState.hasRestoreMessage,
    actual: invalidRestoreState,
    expected: "详情抽屉中可标记无效，并能恢复为有效线索。",
  });

  await page.getByRole("button", { name: /销售管理/ }).click();
  await page.waitForFunction(() => document.querySelectorAll(".sales-card").length > 0, { timeout: 10000 });
  const salesName = `Vite冒烟销售${stamp}`;
  await page.getByRole("button", { name: "新增销售", exact: true }).click();
  await page.waitForSelector("form[aria-label='新增销售']", { timeout: 10000 });
  await page.getByLabel("销售姓名 *").fill(salesName);
  await page.getByLabel("手机号").fill(`136${stamp}`);
  await page.getByLabel("微信").fill(`sales_${stamp}`);
  await page.getByLabel("排序").fill("99");
  await page.locator("form[aria-label='新增销售']").getByLabel("启用销售").uncheck();
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.waitForFunction((name) => document.body.innerText.includes(`${name} 已新增。`), salesName, { timeout: 10000 });
  await page.waitForFunction(
    (name) => [...document.querySelectorAll(".sales-card")].some((node) => node.textContent?.includes(name)),
    salesName,
    { timeout: 10000 },
  );
  const createSalesState = await page.evaluate((name) => {
    const card = [...document.querySelectorAll(".sales-card")].find((node) => node.textContent?.includes(name));
    const checkbox = card?.querySelector("input[type='checkbox']");
    const badge = card?.querySelector(".status-badge")?.getBoundingClientRect();
    return {
      hasMessage: document.body.innerText.includes(`${name} 已新增。`),
      hasCard: Boolean(card),
      cardText: card?.textContent?.replace(/\s+/g, " ").trim() || "",
      statusCheckboxValid: Boolean(checkbox && !checkbox.disabled && !checkbox.checked),
      badgeSingleLine: badge ? badge.width >= 40 && badge.height <= 30 : false,
    };
  }, salesName);
  results.push({
    case_id: "VITE-009A",
    name: "新增销售",
    passed:
      createSalesState.hasMessage &&
      createSalesState.hasCard &&
      createSalesState.cardText.includes("停用") &&
      createSalesState.cardText.includes("不参与轮询") &&
      createSalesState.statusCheckboxValid &&
      createSalesState.badgeSingleLine,
    actual: createSalesState,
    expected: "点击新增销售后弹窗可打开，保存后销售卡片出现在列表中；销售状态开关控制启用/停用，停用即不参与轮询。",
  });

  const toggle = page.locator(".sales-card input[type='checkbox']:not(:disabled)").first();
  const beforeChecked = await toggle.isChecked();
  await toggle.click();
  await page.waitForFunction(() => document.body.innerText.includes("已启用") || document.body.innerText.includes("已停用"), { timeout: 10000 });
  await toggle.click();
  await page.waitForTimeout(1000);
  const salesState = await page.evaluate(
    (expectedChecked) => ({
      cardCount: document.querySelectorAll(".sales-card").length,
      hasMessage: document.body.innerText.includes("已启用") || document.body.innerText.includes("已停用"),
      restoredChecked: document.querySelector(".sales-card input[type='checkbox']:not(:disabled)")?.checked === expectedChecked,
      disabledSalesTextValid: [...document.querySelectorAll(".sales-card.disabled")].every((card) => {
        const checkbox = card.querySelector("input[type='checkbox']");
        return card.textContent?.includes("不参与轮询") && checkbox && !checkbox.checked && !checkbox.disabled;
      }),
    }),
    beforeChecked,
  );
  results.push({
    case_id: "VITE-009",
    name: "销售状态开关",
    passed: salesState.cardCount > 0 && salesState.hasMessage && salesState.restoredChecked && salesState.disabledSalesTextValid,
    actual: salesState,
    expected: "销售管理展示销售卡片，并可切换后恢复销售启用/停用状态。",
  });

  await page.getByRole("button", { name: /操作日志/ }).click();
  await page.waitForFunction(() => document.body.innerText.includes("日志审计"), { timeout: 10000 });
  await page.getByLabel("操作类型").selectOption("phone_revealed");
  await page.waitForFunction(() => document.querySelectorAll(".logs-table tbody tr").length > 0, { timeout: 10000 });
  const logListState = await page.evaluate(() => ({
    rowCount: document.querySelectorAll(".logs-table tbody tr").length,
    hasHeaders:
      document.body.innerText.includes("操作时间") &&
      document.body.innerText.includes("操作人") &&
      document.body.innerText.includes("操作类型") &&
      document.body.innerText.includes("操作对象") &&
      document.body.innerText.includes("结果"),
    hasPhoneReveal: document.body.innerText.includes("查看完整手机号") || document.body.innerText.includes("查看手机号明文"),
    hasPagination: document.querySelector(".logs-pagination-row")?.textContent?.includes("共") || false,
    hasError: document.body.innerText.includes("操作日志加载失败"),
  }));
  await page.locator(".logs-table tbody tr").first().click();
  await page.waitForFunction(() => document.body.innerText.includes("日志详情"), { timeout: 10000 });
  const logDetailState = await page.evaluate(() => ({
    hasDetail: document.body.innerText.includes("日志详情"),
    hasOperator: document.body.innerText.includes("操作人"),
    hasObjectId: document.body.innerText.includes("对象 ID"),
    hasResult: document.body.innerText.includes("操作结果"),
    hasCloseButton: Boolean(document.querySelector(".logs-detail-modal .modal-actions button")),
  }));
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  results.push({
    case_id: "VITE-013",
    name: "操作日志列表与详情",
    passed:
      logListState.rowCount > 0 &&
      logListState.hasHeaders &&
      logListState.hasPhoneReveal &&
      logListState.hasPagination &&
      !logListState.hasError &&
      logDetailState.hasDetail &&
      logDetailState.hasOperator &&
      logDetailState.hasObjectId &&
      logDetailState.hasResult &&
      logDetailState.hasCloseButton,
    actual: { logListState, logDetailState },
    expected: "点击操作日志后展示日志列表，可按操作类型筛选手机号明文查看日志，并可打开日志详情。",
  });

  await browser.close();
  const output = {
    generated_at: new Date().toISOString(),
    app_url: appUrl,
    seed: {
      phone,
      status: seed.status,
      code: seed.payload.code,
      lead_id: seed.payload.data?.id,
      batch_lead_ids: [batchSeedA.payload.data?.id, batchSeedB.payload.data?.id],
    },
    results,
    browser_errors: browserErrors,
    summary: {
      total: results.length,
      passed: results.filter((item) => item.passed).length,
      browser_error_count: browserErrors.length,
    },
  };
  fs.writeFileSync(outputPath, JSON.stringify(output, null, 2));
  console.log(JSON.stringify(output.summary));
  process.exit(results.every((item) => item.passed) ? 0 : 1);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
