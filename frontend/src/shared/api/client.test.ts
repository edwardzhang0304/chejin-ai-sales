import { describe, expect, it } from "vitest";

import { ApiError, apiErrorFromResponse, buildOperatorHeaders, formatApiError, runtimeConfig } from "./client";

describe("ApiError", () => {
  it("keeps backend business error metadata for UI handling", () => {
    const error = new ApiError({
      status: 409,
      code: "LEAD_PHONE_DUPLICATED",
      message: "该手机号已存在",
      data: { created: false },
      traceId: "req_test_001",
    });

    expect(error.status).toBe(409);
    expect(error.code).toBe("LEAD_PHONE_DUPLICATED");
    expect(error.data).toEqual({ created: false });
    expect(error.traceId).toBe("req_test_001");
  });

  it("formats backend code and trace id for user visible errors", () => {
    const error = new ApiError({
      status: 500,
      code: "INTERNAL_SERVER_ERROR",
      message: "服务内部错误",
      data: {},
      traceId: "req_test_002",
    });

    expect(formatApiError(error, "提交失败")).toBe("服务内部错误（错误码：INTERNAL_SERVER_ERROR，Trace ID：req_test_002）");
  });
});

describe("admin auth headers", () => {
  it("adds bearer token without replacing operator audit headers", () => {
    runtimeConfig.adminToken = "admin-token-001";

    expect(buildOperatorHeaders()).toMatchObject({
      Authorization: "Bearer admin-token-001",
      "X-Operator-Id": runtimeConfig.operatorId,
      "X-Operator-Name": runtimeConfig.operatorName,
      "X-Operator-Role": runtimeConfig.operatorRole,
    });

    runtimeConfig.adminToken = "";
  });

  it("does not send Authorization when admin token is empty", () => {
    runtimeConfig.adminToken = "";

    expect(buildOperatorHeaders()).not.toHaveProperty("Authorization");
  });
});

describe("auth error parsing", () => {
  it("normalizes 401 responses without a JSON envelope", async () => {
    const error = await apiErrorFromResponse(new Response("", { status: 401 }));

    expect(error.status).toBe(401);
    expect(error.code).toBe("ADMIN_UNAUTHORIZED");
    expect(error.message).toBe("登录已失效，请重新登录。");
  });

  it("normalizes 403 responses without a JSON envelope", async () => {
    const error = await apiErrorFromResponse(new Response("", { status: 403 }));

    expect(error.status).toBe(403);
    expect(error.code).toBe("ADMIN_FORBIDDEN");
    expect(error.message).toBe("当前账号无权限访问该功能。");
  });
});
