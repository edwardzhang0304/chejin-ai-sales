import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiErrorFromResponse, buildUrl, formatApiError, request, requestBlob, requestForm, runtimeConfig } from "./client";

const originalBaseUrl = runtimeConfig.baseUrl;

afterEach(() => {
  vi.unstubAllGlobals();
  runtimeConfig.baseUrl = originalBaseUrl;
});

describe("production same-origin API URLs", () => {
  it("sends login, form and download requests to the page origin with cookies", async () => {
    runtimeConfig.baseUrl = "/api";
    vi.stubGlobal("location", { origin: "https://jiangsuchejin.com" });
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(
      new Response(JSON.stringify({ code: "OK", data: {} }), { status: 200 }),
    ));
    vi.stubGlobal("fetch", fetchMock);
    await request("/auth/login", { method: "POST", body: { username: "fixture", password: "synthetic" } });
    await requestForm("/import", new FormData());
    await requestBlob("/export");
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "https://jiangsuchejin.com/api/auth/login",
      "https://jiangsuchejin.com/api/import",
      "https://jiangsuchejin.com/api/export",
    ]);
    for (const [, init] of fetchMock.mock.calls) expect(init.credentials).toBe("include");
  });

  it("preserves explicit Fast UAT API addresses and encoded query parameters", () => {
    runtimeConfig.baseUrl = "http://127.0.0.1:8000/api";
    vi.stubGlobal("location", { origin: "http://127.0.0.1:5173" });
    const url = new URL(buildUrl("/leads", { search: "a&b", page: 1 }));
    expect(url.origin).toBe("http://127.0.0.1:8000");
    expect(url.searchParams.get("search")).toBe("a&b");
    expect(url.searchParams.get("page")).toBe("1");
  });
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
