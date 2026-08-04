import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { ApiError, apiErrorFromResponse, buildOperatorHeaders, formatApiError, runtimeConfig } from "../src/shared/api/client.ts";

const duplicated = new ApiError({
  status: 409,
  code: "LEAD_PHONE_DUPLICATED",
  message: "该手机号已存在",
  data: { created: false },
  traceId: "req_test_001",
});

assert.equal(duplicated.status, 409);
assert.equal(duplicated.code, "LEAD_PHONE_DUPLICATED");
assert.deepEqual(duplicated.data, { created: false });
assert.equal(duplicated.traceId, "req_test_001");

const internal = new ApiError({
  status: 500,
  code: "INTERNAL_SERVER_ERROR",
  message: "服务内部错误",
  data: {},
  traceId: "req_test_002",
});

assert.equal(
  formatApiError(internal, "提交失败"),
  "服务内部错误（错误码：INTERNAL_SERVER_ERROR，Trace ID：req_test_002）",
);

runtimeConfig.adminToken = "admin-token-001";
const headers = buildOperatorHeaders();
assert.equal(headers.Authorization, "Bearer admin-token-001");
assert.equal(headers["X-Operator-Id"], runtimeConfig.operatorId);
assert.equal(headers["X-Operator-Name"], runtimeConfig.operatorName);
assert.equal(headers["X-Operator-Role"], runtimeConfig.operatorRole);

runtimeConfig.adminToken = "";
assert.equal("Authorization" in buildOperatorHeaders(), false);

const unauthorized = await apiErrorFromResponse(new Response("", { status: 401 }));
assert.equal(unauthorized.status, 401);
assert.equal(unauthorized.code, "ADMIN_UNAUTHORIZED");
assert.equal(unauthorized.message, "登录已失效，请重新登录。");

const forbidden = await apiErrorFromResponse(new Response("", { status: 403 }));
assert.equal(forbidden.status, 403);
assert.equal(forbidden.code, "ADMIN_FORBIDDEN");
assert.equal(forbidden.message, "当前账号无权限访问该功能。");

const tasksPageSource = await readFile(
  new URL("../src/features/tasks/TasksPage.tsx", import.meta.url),
  "utf8",
);
assert.equal(tasksPageSource.includes("需人工确认"), false);
assert.equal(tasksPageSource.includes("需人工处理"), false);
assert.equal(tasksPageSource.includes("禁止补发；会话已转销售正常接管"), true);

console.log("frontend unit tests passed");
