import { describe, expect, it } from "vitest";

import { ApiError, formatApiError } from "./client";

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
