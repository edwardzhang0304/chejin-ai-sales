import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, request } from "./shared/api/client";

const authApi = vi.hoisted(() => ({
  getAuthSession: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
}));

vi.mock("./shared/api/auth", () => ({
  getAuthSession: authApi.getAuthSession,
  login: authApi.login,
  logout: authApi.logout,
}));

vi.mock("./features/leads/LeadsPage", () => ({ LeadsPage: () => <div>线索模块内容</div> }));
vi.mock("./features/vehicles/VehiclesPage", () => ({ VehiclesPage: () => <div>车辆模块可操作</div> }));
vi.mock("./features/sales/SalesPage", () => ({ SalesPage: () => <div>销售模块内容</div> }));
vi.mock("./features/workers/WorkersPage", () => ({ WorkersPage: () => <div>Worker 模块内容</div> }));
vi.mock("./features/tasks/TasksPage", () => ({ TasksPage: () => <div>任务模块内容</div> }));
vi.mock("./features/logs/LogsPage", () => ({ LogsPage: () => <div>日志模块内容</div> }));

import { App } from "./App";

const session = { operator_id: "operator-1", operator_name: "运营小王" };

beforeEach(() => {
  authApi.getAuthSession.mockReset();
  authApi.login.mockReset();
  authApi.logout.mockReset();
  authApi.logout.mockResolvedValue({ logged_out: true });
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function renderLoggedIn() {
  authApi.getAuthSession.mockResolvedValue(session);
  render(<App />);
  expect(await screen.findByRole("heading", { name: "请选择左侧模块" })).toBeTruthy();
}

describe("运营后台登录门禁", () => {
  it("未登录时只展示登录页，不渲染后台模块", async () => {
    window.localStorage.setItem("chejin_admin_token", "legacy-token");
    authApi.getAuthSession.mockRejectedValue(new ApiError({ status: 401, code: "ADMIN_UNAUTHORIZED", message: "登录已失效", data: {} }));
    render(<App />);

    expect(await screen.findByRole("heading", { name: "登录运营后台" })).toBeTruthy();
    expect(window.localStorage.getItem("chejin_admin_token")).toBeNull();
    expect(screen.queryByRole("button", { name: "线索管理" })).toBeNull();
    expect(screen.queryByText("线索模块内容")).toBeNull();
  });

  it("登录成功后进入默认空白工作区", async () => {
    authApi.getAuthSession.mockRejectedValue(new Error("not signed in"));
    authApi.login.mockResolvedValue(session);
    render(<App />);

    fireEvent.change(await screen.findByPlaceholderText("请输入账号"), { target: { value: "operator" } });
    fireEvent.change(screen.getByPlaceholderText("请输入密码"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByRole("heading", { name: "请选择左侧模块" })).toBeTruthy();
    expect(authApi.login).toHaveBeenCalledWith("operator", "secret");
    expect(screen.queryByText("车辆模块可操作")).toBeNull();
  });

  it("登录失败显示统一中文提示，并恢复可提交状态", async () => {
    authApi.getAuthSession.mockRejectedValue(new Error("not signed in"));
    authApi.login.mockRejectedValue(new ApiError({ status: 401, code: "ADMIN_UNAUTHORIZED", message: "invalid", data: {} }));
    render(<App />);

    fireEvent.change(await screen.findByPlaceholderText("请输入账号"), { target: { value: "wrong" } });
    fireEvent.change(screen.getByPlaceholderText("请输入密码"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    expect((await screen.findByRole("alert")).textContent).toContain("账号或密码错误");
    expect((screen.getByRole("button", { name: "登录" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("登录中禁用表单，重复点击不会重复提交", async () => {
    authApi.getAuthSession.mockRejectedValue(new Error("not signed in"));
    authApi.login.mockImplementation(() => new Promise(() => undefined));
    render(<App />);

    fireEvent.change(await screen.findByPlaceholderText("请输入账号"), { target: { value: "operator" } });
    fireEvent.change(screen.getByPlaceholderText("请输入密码"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    const submittingButton = screen.getByRole("button", { name: "登录中" }) as HTMLButtonElement;
    expect(submittingButton.disabled).toBe(true);
    fireEvent.click(submittingButton);
    expect(authApi.login).toHaveBeenCalledTimes(1);
  });

  it("会话接口返回 401 后立即回到登录页并清空后台", async () => {
    await renderLoggedIn();
    fireEvent.click(screen.getByRole("button", { name: "车辆管理" }));
    expect(screen.getByText("车辆模块可操作")).toBeTruthy();

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 401 })));
    await expect(request("/expired")).rejects.toMatchObject({ status: 401 });

    expect((await screen.findByRole("alert")).textContent).toContain("登录已失效，请重新登录");
    expect(screen.queryByText("车辆模块可操作")).toBeNull();
  });

  it("退出后清空页面状态，无法继续查看后台", async () => {
    await renderLoggedIn();
    fireEvent.click(screen.getByRole("button", { name: "车辆管理" }));
    fireEvent.click(screen.getByRole("button", { name: "运营小王 全部功能权限" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "退出登录" }));

    await waitFor(() => expect(authApi.logout).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole("heading", { name: "登录运营后台" })).toBeTruthy();
    expect(screen.queryByText("车辆模块可操作")).toBeNull();
  });

  it("退出失败时保留当前会话和页面，并显示中文错误", async () => {
    authApi.logout.mockRejectedValue(new Error("network down"));
    await renderLoggedIn();
    fireEvent.click(screen.getByRole("button", { name: "车辆管理" }));
    fireEvent.click(screen.getByRole("button", { name: "运营小王 全部功能权限" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "退出登录" }));

    expect((await screen.findByRole("alert")).textContent).toContain("退出失败，请重试");
    expect(screen.getByText("车辆模块可操作")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "登录运营后台" })).toBeNull();
  });

  it("退出接口确认会话已失效时进入登录页", async () => {
    authApi.logout.mockRejectedValue(new ApiError({ status: 401, code: "ADMIN_UNAUTHORIZED", message: "会话已失效", data: {} }));
    await renderLoggedIn();
    fireEvent.click(screen.getByRole("button", { name: "运营小王 全部功能权限" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "退出登录" }));

    expect(await screen.findByRole("heading", { name: "登录运营后台" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("任意已登录账号都能进入并操作车辆模块", async () => {
    await renderLoggedIn();
    fireEvent.click(screen.getByRole("button", { name: "车辆管理" }));

    expect(screen.getByText("车辆模块可操作")).toBeTruthy();
    expect(screen.queryByText("只读角色")).toBeNull();
  });
});
