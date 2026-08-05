import { afterEach, describe, expect, it, vi } from "vitest";

import { getAuthSession, login, logout } from "./auth";

afterEach(() => {
  vi.unstubAllGlobals();
});

function okResponse(data: unknown) {
  return new Response(JSON.stringify({ code: "OK", message: "ok", data }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("后台 Cookie 认证接口", () => {
  it("登录仅提交账号密码，不保存或返回前端 Token", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse({ operator_id: "op-1", operator_name: "运营账号" }));
    vi.stubGlobal("fetch", fetchMock);

    const session = await login("operator", "secret-password");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/auth/login");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(JSON.parse(String(init.body))).toEqual({ username: "operator", password: "secret-password" });
    expect(session).toEqual({ operator_id: "op-1", operator_name: "运营账号" });
    expect(session).not.toHaveProperty("role");
  });

  it("打开后台时通过会话接口确认登录状态", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse({ operator_id: "op-1", operator_name: "运营账号" }));
    vi.stubGlobal("fetch", fetchMock);

    await getAuthSession();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/auth/session");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("include");
  });

  it("退出调用服务端并携带 Cookie 会话", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse({ logged_out: true }));
    vi.stubGlobal("fetch", fetchMock);

    await logout();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/auth/logout");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
  });
});
