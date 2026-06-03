const fs = require("fs");
const path = require("path");
const { chromium } = require("/Users/zhangwentao/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright");

const chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const root = "/Users/zhangwentao/Documents/车金";
const outDir = path.join(root, "deliverables", "test_runs", "visual_review");
const outputPath = path.join(outDir, "visual_review_metrics.json");
const staticPath = path.join(root, "website", "ops-admin.html");

fs.mkdirSync(outDir, { recursive: true });

const targets = [
  {
    id: "vite-leads-desktop",
    url: "http://127.0.0.1:5173/",
    viewport: { width: 1440, height: 1000 },
    setup: async (page) => {
      await page.getByRole("button", { name: /线索管理/ }).click();
      await page.waitForFunction(() => document.querySelectorAll("tbody tr").length > 0, { timeout: 10000 });
    },
  },
  {
    id: "vite-leads-mobile",
    url: "http://127.0.0.1:5173/",
    viewport: { width: 390, height: 844 },
    setup: async (page) => {
      await page.getByRole("button", { name: /线索管理/ }).click();
      await page.waitForFunction(() => document.querySelectorAll("tbody tr").length > 0, { timeout: 10000 });
    },
  },
  {
    id: "vite-sales-desktop",
    url: "http://127.0.0.1:5173/",
    viewport: { width: 1440, height: 1000 },
    setup: async (page) => {
      await page.getByRole("button", { name: /销售管理/ }).click();
      await page.waitForFunction(() => document.querySelectorAll(".sales-card").length > 0, { timeout: 10000 });
    },
  },
  {
    id: "static-leads-desktop",
    url: `file://${staticPath}?module=leads`,
    viewport: { width: 1440, height: 1000 },
    setup: async (page) => {
      await page.waitForSelector("[data-leads-table] tr", { timeout: 10000 });
    },
  },
  {
    id: "static-leads-mobile",
    url: `file://${staticPath}?module=leads`,
    viewport: { width: 390, height: 844 },
    setup: async (page) => {
      await page.waitForSelector("[data-leads-table] tr", { timeout: 10000 });
    },
  },
];

function rectOf(element) {
  if (!element) return null;
  const rect = element.getBoundingClientRect();
  return {
    x: Math.round(rect.x),
    y: Math.round(rect.y),
    width: Math.round(rect.width),
    height: Math.round(rect.height),
  };
}

async function collectMetrics(page) {
  return page.evaluate((rectFnSource) => {
    const rectOf = new Function("element", `return (${rectFnSource})(element);`);
    const style = (selector) => {
      const el = document.querySelector(selector);
      if (!el) return null;
      const computed = getComputedStyle(el);
      return {
        color: computed.color,
        backgroundColor: computed.backgroundColor,
        borderRadius: computed.borderRadius,
        fontFamily: computed.fontFamily,
        fontSize: computed.fontSize,
        lineHeight: computed.lineHeight,
      };
    };
    const doc = document.documentElement;
    const tableWrap =
      document.querySelector(".table-wrap") ||
      document.querySelector(".table-card") ||
      document.querySelector("[data-leads-table]")?.closest("div");
    const sidebar = document.querySelector(".sidebar");
    const workspace = document.querySelector(".workspace") || document.querySelector("[data-workspace]");
    const listPanel = document.querySelector(".list-region") || document.querySelector(".lead-list-panel");
    const detailDrawer = document.querySelector(".detail-drawer") || document.querySelector("[data-drawer]");
    const metricGrid = document.querySelector(".metric-grid");
    const salesGrid = document.querySelector(".sales-grid") || document.querySelector("[data-sales-grid]");

    return {
      page: {
        clientWidth: doc.clientWidth,
        scrollWidth: doc.scrollWidth,
        bodyScrollWidth: document.body.scrollWidth,
        hasHorizontalOverflow: doc.scrollWidth > doc.clientWidth + 1,
        backgroundColor: getComputedStyle(document.body).backgroundColor,
      },
      counts: {
        metricCards: document.querySelectorAll(".metric-grid article").length,
        tableRows: document.querySelectorAll("tbody tr, [data-leads-table] tr").length,
        salesCards: document.querySelectorAll(".sales-card, [data-sales-grid] article").length,
        statusBadges: document.querySelectorAll(".status-badge, .status").length,
      },
      rects: {
        sidebar: rectOf(sidebar),
        workspace: rectOf(workspace),
        metricGrid: rectOf(metricGrid),
        listPanel: rectOf(listPanel),
        detailDrawer: rectOf(detailDrawer),
        tableWrap: rectOf(tableWrap),
        salesGrid: rectOf(salesGrid),
      },
      styles: {
        sidebar: style(".sidebar"),
        workspace: style(".workspace") || style("[data-workspace]"),
        primaryButton: style(".primary-button"),
        panel: style(".list-region") || style(".lead-list-panel"),
        detailDrawer: style(".detail-drawer") || style("[data-drawer]"),
        metricCard: style(".metric-grid article"),
        salesCard: style(".sales-card") || style("[data-sales-grid] article"),
      },
      table: {
        clientWidth: tableWrap?.clientWidth || 0,
        scrollWidth: tableWrap?.scrollWidth || 0,
        scrollsInside: tableWrap ? tableWrap.scrollWidth > tableWrap.clientWidth + 1 : false,
      },
      textSignals: {
        hasLeadTitle: document.body.innerText.includes("线索管理") || document.body.innerText.includes("客户线索导入与分配"),
        hasSalesTitle: document.body.innerText.includes("销售管理"),
        hasStats: document.body.innerText.includes("今日新增") && document.body.innerText.includes("重复"),
        hasDrawer: document.body.innerText.includes("线索详情") || document.body.innerText.includes("任务节点"),
      },
    };
  }, rectOf.toString());
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: chrome });
  const results = [];

  for (const target of targets) {
    const page = await browser.newPage({ viewport: target.viewport });
    const browserErrors = [];
    page.on("console", (msg) => {
      if (["error", "warning"].includes(msg.type())) browserErrors.push({ type: msg.type(), text: msg.text() });
    });
    page.on("pageerror", (err) => browserErrors.push({ type: "pageerror", text: err.message }));

    await page.goto(target.url, { waitUntil: "networkidle" });
    await target.setup(page);
    await page.screenshot({ path: path.join(outDir, `${target.id}.png`), fullPage: true });
    const metrics = await collectMetrics(page);
    results.push({
      id: target.id,
      url: target.url,
      viewport: target.viewport,
      screenshot: path.join(outDir, `${target.id}.png`),
      browserErrors,
      metrics,
    });
    await page.close();
  }

  await browser.close();

  const output = {
    generated_at: new Date().toISOString(),
    results,
  };
  fs.writeFileSync(outputPath, JSON.stringify(output, null, 2));
  console.log(JSON.stringify({ total: results.length, outputPath }, null, 2));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
