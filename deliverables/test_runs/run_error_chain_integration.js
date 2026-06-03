const fs = require("fs");
const path = require("path");
const { chromium } = require("/Users/zhangwentao/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright");

const chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const root = "/Users/zhangwentao/Documents/车金";
const resultPath = path.join(root, "deliverables", "test_runs", "error_chain_integration_result.json");
const reportPath = path.join(root, "deliverables", "test_runs", "前后端错误链路联调结果_2026-06-03.md");
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
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  return { status: response.status, payload };
}

function hasTrace(payload) {
  return typeof payload?.trace_id === "string" && payload.trace_id.length > 0;
}

function passedApiError(actual, status, code) {
  return actual.status === status && actual.payload?.code === code && typeof actual.payload?.message === "string" && hasTrace(actual.payload);
}

function result(caseId, name, actual, passed, expected) {
  return { case_id: caseId, name, passed, actual, expected };
}

function markdown(results) {
  const rows = results
    .map((item) => `| ${item.case_id} | ${item.name} | ${item.passed ? "通过" : "失败"} | ${item.actual.api?.code || item.actual.api?.payload?.code || "-"} | ${item.actual.api?.trace_id || item.actual.api?.payload?.trace_id || "-"} |`)
    .join("\n");
  return `# 前后端错误链路联调结果

日期：2026-06-03
负责人：前端工程师、后端工程师

## 1. 结论

本轮联调 ${results.every((item) => item.passed) ? "通过" : "未通过"}。覆盖重复手机号 409、参数错误、未找到数据、服务端异常、手机号明文查看审计。

## 2. 结果摘要

| 用例 | 链路 | 结果 | 错误码 | Trace ID |
|---|---|---|---|---|
${rows}

## 3. 说明

- 重复手机号 409 和参数错误通过真实前端表单触发，页面内可见错误码和 Trace ID。
- 未找到数据、服务端异常先验证后端真实 API envelope，再用同结构响应验证前端错误展示路径，避免破坏真实业务数据。
- 手机号明文查看通过真实前端操作触发，并校验操作日志存在 phone_revealed 审计记录。
`;
}

(async () => {
  const stamp = String(Date.now()).slice(-8);
  const phone = `139${stamp}`;
  const missingLeadId = "00000000-0000-0000-0000-000000000404";
  const results = [];

  const seed = await api("/leads", {
    method: "POST",
    body: JSON.stringify({
      customer_name: `错误链路种子${stamp}`,
      phones: [phone],
      remark: "错误链路联调种子",
    }),
  });

  const duplicateApi = await api("/leads", {
    method: "POST",
    body: JSON.stringify({
      customer_name: `错误链路重复${stamp}`,
      phones: [phone],
      remark: "错误链路重复提交备注",
    }),
  });
  results.push(result(
    "ERR-API-001",
    "重复手机号 409 API",
    { api: { status: duplicateApi.status, ...duplicateApi.payload } },
    passedApiError(duplicateApi, 409, "LEAD_PHONE_DUPLICATED"),
    "后端返回 409 / LEAD_PHONE_DUPLICATED / trace_id。",
  ));

  const invalidApi = await api("/leads", {
    method: "POST",
    body: JSON.stringify({
      customer_name: `错误链路参数${stamp}`,
      phones: ["123"],
    }),
  });
  results.push(result(
    "ERR-API-002",
    "参数错误 API",
    { api: { status: invalidApi.status, ...invalidApi.payload } },
    passedApiError(invalidApi, 400, "LEAD_PHONE_INVALID"),
    "后端返回参数错误 code/message/trace_id。",
  ));

  const missingApi = await api(`/leads/${missingLeadId}`);
  results.push(result(
    "ERR-API-003",
    "未找到数据 API",
    { api: { status: missingApi.status, ...missingApi.payload } },
    passedApiError(missingApi, 404, "LEAD_NOT_FOUND"),
    "后端返回 404 / LEAD_NOT_FOUND / trace_id。",
  ));

  const internalApi = await api("/_debug/raise-internal-error");
  results.push(result(
    "ERR-API-004",
    "服务端异常 API",
    { api: { status: internalApi.status, ...internalApi.payload } },
    passedApiError(internalApi, 500, "INTERNAL_SERVER_ERROR"),
    "开发环境调试端点返回 500 / INTERNAL_SERVER_ERROR / trace_id。",
  ));

  const browser = await chromium.launch({ headless: true, executablePath: chrome });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.goto(appUrl, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: /线索管理/ }).click();
  await page.waitForFunction(() => document.querySelectorAll("tbody tr").length > 0, { timeout: 10000 });

  await page.getByRole("button", { name: "新增客户" }).click();
  await page.getByLabel("客户名称 *").fill(`错误链路重复前端${stamp}`);
  await page.getByLabel("手机 *").fill(phone);
  await page.getByLabel("备注内容").fill("错误链路前端重复提交");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.waitForSelector(".duplicate-alert", { timeout: 10000 });
  const duplicateUi = await page.evaluate(() => ({
    text: document.querySelector(".duplicate-alert")?.textContent?.replace(/\s+/g, " ").trim() || "",
    hasCode: document.body.innerText.includes("错误码：LEAD_PHONE_DUPLICATED"),
    hasTraceId: document.body.innerText.includes("Trace ID："),
  }));
  results.push(result(
    "ERR-UI-001",
    "重复手机号 409 页面展示",
    { api: duplicateApi.payload, ui: duplicateUi },
    duplicateUi.hasCode && duplicateUi.hasTraceId && duplicateUi.text.includes("该手机号已存在"),
    "新增客户弹窗展示明确错误信息、错误码和 Trace ID。",
  ));
  await page.getByRole("button", { name: "取消" }).click();

  await page.getByRole("button", { name: "新增客户" }).click();
  await page.getByLabel("客户名称 *").fill(`错误链路参数前端${stamp}`);
  await page.getByLabel("手机 *").fill("123");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.waitForFunction(() => document.body.innerText.includes("错误码：LEAD_PHONE_INVALID"), { timeout: 10000 });
  const invalidUi = await page.evaluate(() => ({
    text: document.querySelector(".create-lead-modal .inline-alert")?.textContent?.replace(/\s+/g, " ").trim() || "",
    hasCode: document.body.innerText.includes("错误码：LEAD_PHONE_INVALID"),
    hasTraceId: document.body.innerText.includes("Trace ID："),
  }));
  results.push(result(
    "ERR-UI-002",
    "参数错误页面展示",
    { api: invalidApi.payload, ui: invalidUi },
    invalidUi.hasCode && invalidUi.hasTraceId && invalidUi.text.includes("手机号"),
    "新增客户弹窗展示参数错误、错误码和 Trace ID。",
  ));
  await page.getByRole("button", { name: "取消" }).click();

  await page.route("**/api/leads/*", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({
          code: "LEAD_NOT_FOUND",
          message: "线索不存在",
          data: {},
          trace_id: missingApi.payload.trace_id,
        }),
      });
      return;
    }
    await route.continue();
  });
  const detailRowCount = await page.locator("tbody tr").count();
  await page.locator("tbody tr").nth(detailRowCount > 1 ? 1 : 0).click();
  await page.waitForFunction(() => document.body.innerText.includes("错误码：LEAD_NOT_FOUND"), { timeout: 10000 });
  const missingUi = await page.evaluate(() => ({
    text: document.querySelector(".detail-drawer")?.textContent?.replace(/\s+/g, " ").trim() || "",
    hasCode: document.body.innerText.includes("错误码：LEAD_NOT_FOUND"),
    hasTraceId: document.body.innerText.includes("Trace ID："),
  }));
  results.push(result(
    "ERR-UI-003",
    "未找到数据页面展示",
    { api: missingApi.payload, ui: missingUi },
    missingUi.hasCode && missingUi.hasTraceId && missingUi.text.includes("线索不存在"),
    "详情抽屉展示未找到错误、错误码和 Trace ID。",
  ));
  await page.unroute("**/api/leads/*");

  await page.route("**/api/leads?**", async (route) => {
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({
        code: "INTERNAL_SERVER_ERROR",
        message: "服务内部错误，请联系管理员并提供 trace_id",
        data: {},
        trace_id: internalApi.payload.trace_id,
      }),
    });
  });
  await page.getByLabel("搜索").fill(`异常${stamp}`);
  await page.waitForFunction(() => document.body.innerText.includes("错误码：INTERNAL_SERVER_ERROR"), { timeout: 10000 });
  const internalUi = await page.evaluate(() => ({
    text: document.querySelector(".list-region .state-box")?.textContent?.replace(/\s+/g, " ").trim() || "",
    hasCode: document.body.innerText.includes("错误码：INTERNAL_SERVER_ERROR"),
    hasTraceId: document.body.innerText.includes("Trace ID："),
  }));
  results.push(result(
    "ERR-UI-004",
    "服务端异常页面展示",
    { api: internalApi.payload, ui: internalUi },
    internalUi.hasCode && internalUi.hasTraceId && internalUi.text.includes("服务内部错误"),
    "列表错误态展示服务端异常、错误码和 Trace ID。",
  ));
  await page.unroute("**/api/leads?**");

  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("button", { name: /线索管理/ }).click();
  await page.waitForFunction(() => document.querySelectorAll("tbody tr").length > 0, { timeout: 10000 });
  await page.locator("tbody tr").first().click();
  await page.waitForFunction(() => document.querySelector(".detail-drawer")?.textContent?.includes("查看完整"), { timeout: 10000 });
  page.once("dialog", (dialog) => dialog.accept("错误链路手机号审计"));
  await page.getByRole("button", { name: "查看完整" }).first().click();
  await page.waitForFunction(() => document.body.innerText.includes("手机号明文："), { timeout: 10000 });
  const auditApi = await api("/operation-logs?event_type=phone_revealed&page_size=5");
  const auditItem = auditApi.payload?.data?.items?.find((item) => item.metadata?.reason === "错误链路手机号审计");
  const revealUi = await page.evaluate(() => ({
    hasRevealMessage: document.body.innerText.includes("手机号明文："),
    hasAuditMessage: document.body.innerText.includes("审计日志"),
  }));
  results.push(result(
    "ERR-AUDIT-001",
    "手机号明文查看审计",
    { api: { status: auditApi.status, code: auditApi.payload?.code, trace_id: auditApi.payload?.trace_id, audit_item: auditItem }, ui: revealUi },
    auditApi.status === 200 &&
      auditApi.payload?.code === "OK" &&
      hasTrace(auditApi.payload) &&
      Boolean(auditItem) &&
      auditItem.event_type === "phone_revealed" &&
      auditItem.metadata?.reason === "错误链路手机号审计" &&
      revealUi.hasRevealMessage &&
      revealUi.hasAuditMessage,
    "前端显示手机号明文和审计提示，操作日志记录 phone_revealed。",
  ));

  await browser.close();

  const output = {
    generated_at: new Date().toISOString(),
    app_url: appUrl,
    api_base: apiBase,
    seed: { phone, lead_id: seed.payload?.data?.id },
    results,
    summary: {
      total: results.length,
      passed: results.filter((item) => item.passed).length,
    },
  };
  fs.writeFileSync(resultPath, JSON.stringify(output, null, 2));
  fs.writeFileSync(reportPath, markdown(results));
  console.log(JSON.stringify(output.summary));
  process.exit(results.every((item) => item.passed) ? 0 : 1);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
