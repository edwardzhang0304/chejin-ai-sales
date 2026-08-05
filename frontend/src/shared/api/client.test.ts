import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiErrorFromResponse, formatApiError, request, requestBlob, requestForm } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

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

describe("Cookie admin session requests", () => {
  it("includes cookies and never sends legacy bearer or browser-asserted roles", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: "OK", data: {} }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await request("/test", {
      headers: {
        Authorization: "Bearer legacy-token",
        "X-Operator-Role": "admin",
      },
    });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.credentials).toBe("include");
    const headers = new Headers(init.headers);
    expect(headers.has("Authorization")).toBe(false);
    expect(headers.has("X-Operator-Id")).toBe(false);
    expect(headers.has("X-Operator-Name")).toBe(false);
    expect(headers.has("X-Operator-Role")).toBe(false);
  });

  it("includes cookies for form and blob requests", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ code: "OK", data: {} }), { status: 200 }))
      .mockResolvedValueOnce(new Response(new Blob(["ok"]), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await requestForm("/form", new FormData());
    await requestBlob("/blob");

    expect((fetchMock.mock.calls[0][1] as RequestInit).credentials).toBe("include");
    expect((fetchMock.mock.calls[1][1] as RequestInit).credentials).toBe("include");
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
