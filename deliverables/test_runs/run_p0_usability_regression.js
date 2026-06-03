const fs = require("fs");
const path = require("path");
const { chromium } = require("/Users/zhangwentao/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright");

const chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const root = "/Users/zhangwentao/Documents/车金";
const outputDir = path.join(root, "deliverables", "test_runs", "p0_usability_regression_2026-06-03");
const outputPath = path.join(outputDir, "p0_usability_regression_result.json");
const appUrl = "http://127.0.0.1:5173/";

function add(results, caseId, name, passed, actual, expected) {
  results.push({ case_id: caseId, name, passed: Boolean(passed), actual, expected });
}

function textIncludes(text, value) {
  return String(text || "").includes(value);
}

async function clickIfPresent(locator, timeout = 5000) {
  if ((await locator.count()) === 0) {
    return false;
  }
  await locator.click({ timeout });
  return true;
}

(async () => {
  fs.mkdirSync(outputDir, { recursive: true });
  const stamp = String(Date.now()).slice(-8);
  const phone = `135${stamp}`;
  const customerName = `可用性回测客户${stamp}`;
  const browser = await chromium.launch({ headless: true, executablePath: chrome });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const browserErrors = [];
  page.on("console", (msg) => {
    if (["error", "warning"].includes(msg.type())) browserErrors.push({ type: msg.type(), text: msg.text() });
  });
  page.on("pageerror", (err) => browserErrors.push({ type: "pageerror", text: err.message }));

  const results = [];
  await page.goto(appUrl, { waitUntil: "networkidle" });

  const defaultState = await page.evaluate(() => ({
    hasEmptyText: document.body.innerText.includes("请选择左侧模块"),
    activeModuleCount: document.querySelectorAll(".module-item.active, .module-link.active").length,
  }));
  add(results, "USE-001", "默认进入空白工作区", defaultState.hasEmptyText && defaultState.activeModuleCount === 0, defaultState, "首次进入不默认打开业务模块。");

  await page.getByRole("button", { name: /线索管理/ }).click();
  await page.waitForTimeout(2000);
  const listLoaded = await page.evaluate(() => ({
    rowCount: document.querySelectorAll("tbody tr").length,
    totalText: document.querySelector(".pagination-total")?.textContent?.replace(/\s+/g, " ").trim(),
    hasDrawer: Boolean(document.querySelector(".detail-drawer")),
    hasError: document.body.innerText.includes("线索列表加载失败"),
  }));
  add(results, "USE-002", "进入线索管理可正常加载", listLoaded.rowCount > 0 && listLoaded.hasDrawer && !listLoaded.hasError, listLoaded, "列表、分页、详情抽屉加载成功。");

  await page.getByRole("button", { name: "新增客户" }).click();
  await page.waitForSelector("form[aria-label='新增客户']", { timeout: 10000 });
  const createForm = page.locator("form[aria-label='新增客户']");
  const addFieldClicked = await clickIfPresent(createForm.getByRole("button", { name: "添加字段", exact: true }));
  const addContactState = await page.evaluate(() => {
    const modal = document.querySelector("form[aria-label='新增客户']");
    return {
      modalOpen: Boolean(modal),
      hasPhone: modal?.textContent?.includes("手机 *") || false,
      hasWechat: modal?.textContent?.includes("微信") || false,
      hasEmail: modal?.textContent?.includes("邮箱") || false,
      customNameInputs: modal?.querySelectorAll("input[aria-label^='字段名称']").length || 0,
      customValueInputs: modal?.querySelectorAll("input[aria-label^='字段内容']").length || 0,
      buttons: [...(modal?.querySelectorAll("button") || [])].map((button) => button.textContent?.trim() || button.getAttribute("aria-label")),
    };
  });
  add(results, "USE-003", "新增客户弹窗字段可用", addFieldClicked && addContactState.hasPhone && addContactState.hasWechat && addContactState.hasEmail && addContactState.customNameInputs >= 3 && addContactState.customValueInputs >= 3, addContactState, "手机、微信、邮箱字段可见，添加字段按钮可点击并新增自定义字段行。");

  await createForm.getByLabel("客户名称 *").fill(customerName);
  await createForm.getByLabel("手机 *").fill(phone);
  await createForm.getByLabel("微信").fill(`wx_use_${stamp}`);
  await createForm.getByLabel("邮箱").fill(`use${stamp}@example.com`);
  await createForm.locator("input[aria-label='字段名称 1']").fill("意向车型");
  await createForm.locator("input[aria-label='字段内容 1']").fill("SUV");
  await createForm.locator("input[aria-label='字段名称 2']").fill("购车预算");
  await createForm.locator("input[aria-label='字段内容 2']").fill("10万以内");
  await createForm.getByLabel("备注内容").fill("P0 正常使用标准回测");
  await createForm.getByRole("button", { name: "保存并继续新增" }).click();
  await page.waitForTimeout(1800);
  const saveContinue = await page.evaluate(() => {
    const modal = document.querySelector("form[aria-label='新增客户']");
    return {
      modalStillOpen: Boolean(modal),
      customerNameValue: modal?.querySelector("input")?.value || "",
      hasSuccess: document.body.innerText.includes("已保存，可继续新增客户"),
    };
  });
  add(results, "USE-004", "新增客户保存并继续新增可用", saveContinue.modalStillOpen && saveContinue.customerNameValue === "", saveContinue, "保存成功后弹窗保留并清空表单。");
  await page.locator("form[aria-label='新增客户']").getByRole("button", { name: "取消", exact: true }).click();

  await page.getByLabel("搜索").fill(customerName);
  await page.waitForTimeout(1200);
  const searchState = await page.evaluate((name) => ({
    rowCount: document.querySelectorAll("tbody tr").length,
    firstCustomer: document.querySelector("tbody tr td:nth-child(2)")?.textContent?.replace(/\s+/g, " ").trim() || "",
    totalText: document.querySelector(".pagination-total")?.textContent?.replace(/\s+/g, " ").trim() || "",
    bodyText: document.body.innerText.includes(name),
  }), customerName);
  add(results, "USE-005", "搜索可正常过滤", searchState.rowCount >= 1 && searchState.bodyText, searchState, "输入客户名称后列表展示对应客户。");

  await page.getByLabel("搜索").fill("");
  await page.waitForTimeout(1200);
  await page.locator(".filter-card select").first().selectOption("invalid");
  await page.waitForTimeout(1200);
  const invalidFilter = await page.evaluate(() => ({
    rowCount: document.querySelectorAll("tbody tr").length,
    statusCells: [...document.querySelectorAll("tbody tr td:nth-child(4)")].map((td) => td.textContent?.trim()).slice(0, 10),
  }));
  add(results, "USE-006", "状态筛选可正常使用", invalidFilter.rowCount === 0 || invalidFilter.statusCells.every((status) => status === "无效"), invalidFilter, "选择无效后，可见行状态均为无效或为空结果。");

  await page.locator(".filter-card select").first().selectOption("");
  await page.waitForTimeout(1200);
  const salesOptions = await page.evaluate(() => [...document.querySelectorAll("select[aria-label='筛选销售'] option")].map((option) => ({ value: option.value, text: option.textContent?.trim() })));
  const firstSales = salesOptions.find((option) => option.value);
  if (firstSales) {
    await page.locator("select[aria-label='筛选销售']").selectOption(firstSales.value);
    await page.waitForTimeout(1200);
  }
  const salesFilter = await page.evaluate((salesName) => ({
    selected: document.querySelector("select[aria-label='筛选销售']")?.value || "",
    rowCount: document.querySelectorAll("tbody tr").length,
    salesCells: [...document.querySelectorAll("tbody tr td:nth-child(5)")].map((td) => td.textContent?.trim()).slice(0, 10),
    allVisibleMatch: [...document.querySelectorAll("tbody tr td:nth-child(5)")].every((td) => td.textContent?.trim() === salesName),
  }), firstSales?.text || "");
  add(results, "USE-007", "销售筛选可正常使用", Boolean(firstSales) && salesFilter.rowCount > 0 && salesFilter.allVisibleMatch, { salesOptions, firstSales, salesFilter }, "选择销售后，列表仅展示该销售线索。");

  await page.locator("select[aria-label='筛选销售']").selectOption("");
  await page.waitForTimeout(1200);
  await page.locator("select[aria-label='每页条数']").selectOption("50");
  await page.waitForTimeout(1200);
  const pageSize = await page.evaluate(() => ({
    selected: document.querySelector("select[aria-label='每页条数']")?.value || "",
    rowCount: document.querySelectorAll("tbody tr").length,
    totalText: document.querySelector(".pagination-total")?.textContent?.replace(/\s+/g, " ").trim() || "",
  }));
  add(results, "USE-008", "每页条数可正常切换", pageSize.selected === "50" && pageSize.rowCount > 20 && pageSize.rowCount <= 50, pageSize, "每页条数切换为 50 后列表刷新。");

  const nextEnabled = await page.getByRole("button", { name: "下一页" }).isEnabled();
  if (nextEnabled) {
    await page.getByRole("button", { name: "下一页" }).click();
    await page.waitForTimeout(1200);
  }
  const nextPage = await page.evaluate(() => ({
    currentPage: document.querySelector(".pagination-row nav strong")?.textContent?.trim() || "",
    rowCount: document.querySelectorAll("tbody tr").length,
  }));
  add(results, "USE-009", "下一页可正常翻页", !nextEnabled || nextPage.currentPage === "2", { nextEnabled, nextPage }, "点击下一页后当前页变为 2；若无下一页则按钮禁用。");

  await page.locator("tbody tr").first().click();
  await page.waitForTimeout(1000);
  const tabNames = ["备注", "重复记录", "分配记录", "概览"];
  const tabStates = [];
  for (const tabName of tabNames) {
    await page.locator(".detail-drawer").getByRole("tab", { name: tabName, exact: true }).click();
    await page.waitForTimeout(400);
    tabStates.push(await page.evaluate((name) => ({
      clicked: name,
      selectedText: document.querySelector(".tabs [aria-selected='true']")?.textContent?.trim() || "",
      text: document.querySelector(".drawer-body")?.textContent?.replace(/\s+/g, " ").trim().slice(0, 240) || "",
    }), tabName));
  }
  add(results, "USE-010", "详情 tabs 可正常切换", tabStates.every((state) => state.selectedText === state.clicked), tabStates, "详情 tabs 点击后 active 和内容同步切换。");

  await page.getByLabel("搜索").fill(customerName);
  await page.waitForTimeout(1200);
  await page.locator("tbody tr").first().click();
  await page.waitForFunction(
    (name) => {
      const drawerText = document.querySelector(".detail-drawer")?.textContent || "";
      return drawerText.includes(name) && drawerText.includes("联系方式");
    },
    customerName,
    { timeout: 10000 },
  );
  await page.locator(".detail-drawer").getByRole("tab", { name: "概览", exact: true }).click();
  page.once("dialog", (dialog) => dialog.accept("P0 正常使用回测查看手机号"));
  await page.locator(".detail-drawer").getByRole("button", { name: "查看完整", exact: true }).first().click();
  await page.waitForFunction(() => document.body.innerText.includes("手机号明文："), { timeout: 10000 });
  const revealPhone = await page.evaluate(() => ({
    hasRevealMessage: document.body.innerText.includes("手机号明文："),
    hasAuditMessage: document.body.innerText.includes("审计日志"),
    contactValues: [...document.querySelectorAll(".detail-drawer .contact-row strong")].map((node) => node.textContent?.trim() || ""),
    hasFullPhoneInContactRow: [...document.querySelectorAll(".detail-drawer .contact-row strong")].some((node) => /^1\d{10}$/.test(node.textContent?.trim() || "")),
  }));
  add(results, "USE-010A", "手机号明文查看后联系方式展示完整手机号", revealPhone.hasRevealMessage && revealPhone.hasAuditMessage && revealPhone.hasFullPhoneInContactRow, revealPhone, "点击查看完整后，联系方式行展示 11 位明文手机号并产生审计提示。");

  await page.getByRole("button", { name: "新增客户" }).click();
  const duplicateForm = page.locator("form[aria-label='新增客户']");
  await duplicateForm.getByLabel("客户名称 *").fill(`重复${customerName}`);
  await duplicateForm.getByLabel("手机 *").fill(phone);
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.waitForSelector(".duplicate-alert", { timeout: 10000 });
  const duplicate = await page.evaluate(() => ({
    modalStillOpen: Boolean(document.querySelector("form[aria-label='新增客户']")),
    alertText: document.querySelector(".duplicate-alert")?.textContent?.replace(/\s+/g, " ").trim() || "",
    hasOpenOriginal: document.body.innerText.includes("查看原线索"),
    alertRect: (() => {
      const rect = document.querySelector(".duplicate-alert")?.getBoundingClientRect();
      return rect ? { top: rect.top, bottom: rect.bottom } : null;
    })(),
    firstSectionRect: (() => {
      const rect = document.querySelector(".create-lead-modal .form-section")?.getBoundingClientRect();
      return rect ? { top: rect.top, bottom: rect.bottom } : null;
    })(),
  }));
  add(
    results,
    "USE-011",
    "重复手机号提示和查看原线索入口可用",
    duplicate.modalStillOpen &&
      duplicate.alertText.includes("该手机号已存在") &&
      duplicate.hasOpenOriginal &&
      duplicate.alertRect &&
      duplicate.firstSectionRect &&
      duplicate.alertRect.bottom <= duplicate.firstSectionRect.top,
    duplicate,
    "重复保存保留弹窗，展示查看原线索入口，且提示区域不遮挡基础信息。",
  );
  await page.locator("form[aria-label='新增客户']").getByRole("button", { name: "取消", exact: true }).click();

  await page.getByLabel("搜索").fill(customerName);
  await page.waitForTimeout(1200);
  await page.locator("tbody tr").first().click();
  await page.waitForTimeout(800);
  await page.locator(".detail-drawer").getByRole("button", { name: "标记无效", exact: true }).click();
  await page.waitForTimeout(500);
  await page.locator("form[aria-label='标记无效线索'] select").selectOption("test_data");
  await page.locator("form[aria-label='标记无效线索'] textarea").fill("P0 可用性回测标记无效");
  await page.getByRole("button", { name: "确认标记无效" }).click();
  await page.waitForTimeout(1500);
  const invalid = await page.evaluate(() => ({
    hasMessage: document.body.innerText.includes("已标记"),
    hasRestore: document.querySelector(".detail-drawer")?.textContent?.includes("恢复有效") || false,
  }));
  add(results, "USE-012", "标记无效可正常提交", invalid.hasMessage && invalid.hasRestore, invalid, "提交后状态变为无效并出现恢复入口。");

  await page.locator(".detail-drawer").getByRole("button", { name: "恢复有效", exact: true }).click();
  await page.waitForTimeout(500);
  const restoreModal = await page.evaluate(() => ({
    exists: Boolean(document.querySelector("[aria-label='恢复为有效线索']")),
    hasConfirm: document.body.innerText.includes("确认恢复有效"),
  }));
  await page.getByRole("button", { name: "确认恢复有效" }).click();
  await page.waitForTimeout(1500);
  const restore = await page.evaluate((modalState) => ({
    modalAppeared: modalState.exists,
    hasConfirm: modalState.hasConfirm,
    hasMessage: document.body.innerText.includes("已恢复为有效线索"),
  }), restoreModal);
  add(results, "USE-013", "恢复有效确认后可正常提交", restore.modalAppeared && restore.hasConfirm && restore.hasMessage, restore, "恢复有效先弹确认，确认后恢复成功。");

  await page.getByLabel("搜索").fill("");
  await page.waitForTimeout(1200);
  await page.getByLabel("选择当前页线索").check();
  const [download] = await Promise.all([
    page.waitForEvent("download", { timeout: 10000 }),
    page.getByRole("button", { name: "导出", exact: true }).click(),
  ]);
  const exportState = {
    filename: download.suggestedFilename(),
    hasMessage: await page.evaluate(() => document.body.innerText.includes("已导出")),
  };
  add(results, "USE-014", "批量选择后导出可用", exportState.filename.endsWith(".csv") && exportState.hasMessage, exportState, "勾选当前页后可导出 CSV。");

  await page.getByRole("button", { name: /销售管理/ }).click();
  await page.waitForTimeout(1500);
  const salesName = `可用性回测销售${stamp}`;
  await page.getByRole("button", { name: "新增销售", exact: true }).click();
  await page.waitForSelector("form[aria-label='新增销售']", { timeout: 10000 });
  await page.getByLabel("销售姓名 *").fill(salesName);
  await page.getByLabel("手机号").fill(`136${stamp}`);
  await page.getByLabel("微信").fill(`use_sales_${stamp}`);
  await page.getByLabel("排序").fill("99");
  await page.locator("form[aria-label='新增销售']").getByLabel("启用销售").uncheck();
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.waitForFunction((name) => document.body.innerText.includes(`${name} 已新增。`), salesName, { timeout: 10000 });
  await page.waitForFunction(
    (name) => [...document.querySelectorAll(".sales-card")].some((node) => node.textContent?.includes(name)),
    salesName,
    { timeout: 10000 },
  );
  const createSales = await page.evaluate((name) => {
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
  add(
    results,
    "USE-015A",
    "新增销售可正常保存并展示",
    createSales.hasMessage &&
      createSales.hasCard &&
      createSales.cardText.includes("停用") &&
      createSales.cardText.includes("不参与轮询") &&
      createSales.statusCheckboxValid &&
      createSales.badgeSingleLine,
    createSales,
    "新增销售弹窗可打开，保存后列表出现销售卡片；销售状态开关控制启用/停用，停用即不参与轮询。",
  );

  const toggle = page.locator(".sales-card input[type='checkbox']:not(:disabled)").first();
  const beforeChecked = await toggle.isChecked();
  await toggle.click();
  await page.waitForTimeout(1200);
  await toggle.click();
  await page.waitForTimeout(1200);
  const salesToggle = await page.evaluate((expectedChecked) => ({
    hasMessage: document.body.innerText.includes("已启用") || document.body.innerText.includes("已停用"),
    restoredChecked: document.querySelector(".sales-card input[type='checkbox']:not(:disabled)")?.checked === expectedChecked,
    disabledSalesValid: [...document.querySelectorAll(".sales-card.disabled")].every((card) => {
      const checkbox = card.querySelector("input[type='checkbox']");
      return card.textContent?.includes("不参与轮询") && checkbox && !checkbox.disabled && !checkbox.checked;
    }),
  }), beforeChecked);
  add(results, "USE-015", "销售状态开关可正常切换并恢复", salesToggle.hasMessage && salesToggle.restoredChecked && salesToggle.disabledSalesValid, salesToggle, "销售状态可切换启用/停用；停用销售显示不参与轮询，历史线索保留。");

  await browser.close();
  const summary = { total: results.length, passed: results.filter((item) => item.passed).length, failed: results.filter((item) => !item.passed).length };
  fs.writeFileSync(outputPath, JSON.stringify({ generated_at: new Date().toISOString(), app_url: appUrl, results, browser_errors: browserErrors, summary }, null, 2));
  console.log(JSON.stringify(summary));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
