const fs = require("fs");
const path = require("path");
const { chromium } = require("/Users/zhangwentao/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright");

const chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const root = "/Users/zhangwentao/Documents/车金";
const pagePath = path.join(root, "website", "ops-admin.html");
const outputPath = path.join(root, "deliverables", "test_runs", "ui_smoke_result.json");

function fileUrl(filePath, query = "") {
  return `file://${filePath}${query}`;
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: chrome });
  const results = [];
  const errors = [];
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });

  page.on("console", (msg) => {
    if (["error", "warning"].includes(msg.type())) errors.push({ type: msg.type(), text: msg.text() });
  });
  page.on("pageerror", (err) => errors.push({ type: "pageerror", text: err.message }));

  await page.goto(fileUrl(pagePath), { waitUntil: "domcontentloaded" });
  const emptyState = await page.evaluate(() => ({
    workspaceEmpty: document.querySelector("[data-workspace]")?.classList.contains("is-empty"),
    emptyHidden: document.querySelector("[data-empty-workspace]")?.hidden,
    activeModules: [...document.querySelectorAll("[data-module].active")].map((el) => el.dataset.module),
    heading: document.querySelector("[data-empty-workspace] h1")?.textContent?.trim(),
  }));
  results.push({
    case_id: "TC-001",
    name: "默认进入空白工作区",
    passed: emptyState.workspaceEmpty === true && emptyState.emptyHidden === false && emptyState.activeModules.length === 0 && emptyState.heading === "请选择左侧模块",
    actual: emptyState,
    expected: "不带 module 参数时未选中左侧模块，右侧展示“请选择左侧模块”。",
  });

  await page.goto(fileUrl(pagePath, "?module=leads"), { waitUntil: "networkidle" });
  await page.waitForSelector("[data-leads-table] tr", { timeout: 10000 });
  const leadsState = await page.evaluate(() => ({
    workspaceEmpty: document.querySelector("[data-workspace]")?.classList.contains("is-empty"),
    leadsHidden: document.querySelector('[data-module-content="leads"]')?.hidden,
    activeModules: [...document.querySelectorAll("[data-module].active")].map((el) => el.dataset.module),
    totalText: document.querySelector("[data-total-count]")?.textContent?.trim(),
    rowCount: document.querySelectorAll("[data-leads-table] tr").length,
    firstCustomer: document.querySelector("[data-leads-table] tr .lead-cell strong")?.textContent?.trim(),
    drawerClosed: document.querySelector("[data-drawer]")?.classList.contains("closed"),
    toastVisible: !document.querySelector("[data-toast]")?.hidden,
    toastText: document.querySelector("[data-toast]")?.textContent?.trim(),
  }));
  results.push({
    case_id: "TC-002-UI",
    name: "进入线索管理并加载真实列表",
    passed: leadsState.workspaceEmpty === false && leadsState.leadsHidden === false && leadsState.activeModules.includes("leads") && Number(leadsState.totalText) > 0 && leadsState.rowCount > 0 && leadsState.drawerClosed === false,
    actual: leadsState,
    expected: "带 module=leads 时选中线索管理，列表总数大于 0，第一条详情抽屉自动显示。",
  });

  await page.goto(fileUrl(pagePath, "?module=leads&api=http://127.0.0.1:65535/api"), { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1500);
  const fallbackState = await page.evaluate(() => ({
    workspaceEmpty: document.querySelector("[data-workspace]")?.classList.contains("is-empty"),
    leadsHidden: document.querySelector('[data-module-content="leads"]')?.hidden,
    totalText: document.querySelector("[data-total-count]")?.textContent?.trim(),
    rowCount: document.querySelectorAll("[data-leads-table] tr").length,
    toastVisible: !document.querySelector("[data-toast]")?.hidden,
    toastText: document.querySelector("[data-toast]")?.textContent?.trim(),
  }));
  results.push({
    case_id: "TC-017",
    name: "后端未启动兜底",
    passed: fallbackState.workspaceEmpty === false && fallbackState.leadsHidden === false && fallbackState.rowCount > 0 && fallbackState.toastVisible === true && fallbackState.toastText.includes("后端服务未连接"),
    actual: fallbackState,
    expected: "后端不可连接时页面不白屏，保留静态预览并提示后端服务未连接。",
  });

  await browser.close();
  const output = {
    generated_at: new Date().toISOString(),
    url: fileUrl(pagePath),
    results,
    browser_errors: errors,
    summary: {
      total: results.length,
      passed: results.filter((item) => item.passed).length,
      browser_error_count: errors.length,
    },
  };
  fs.writeFileSync(outputPath, JSON.stringify(output, null, 2));
  console.log(JSON.stringify(output.summary));
  process.exit(results.every((item) => item.passed) ? 0 : 1);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
