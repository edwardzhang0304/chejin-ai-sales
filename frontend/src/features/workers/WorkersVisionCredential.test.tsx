import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WorkersPage } from "./WorkersPage";

const KEY = "FAKE-VISION-BROWSER-0967-SENTINEL";
let configured = true;
let calls: { url: string; method: string; body: Record<string, unknown> }[];
let failCredential = false;
const worker = () => ({ id: "worker-1", worker_name: "Windows A", device_name: null, platform: "windows", enabled: true, online_status: "offline", run_status: "paused", running_status: "idle", current_task: null, last_heartbeat_at: null, client_binding_state: "bound", remark: null, bound_sales_id: null, bound_sales_name: null, vision_configured: configured });
beforeEach(() => {
  vi.stubGlobal("scrollTo", vi.fn());
  calls = []; configured = true; failCredential = false;
  vi.stubGlobal("fetch", vi.fn(async (input, options = {}) => {
    const url = String(input); const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : {};
    calls.push({ url, method, body });
    if (url.endsWith("/vision-credential")) {
      if (failCredential) return new Response(JSON.stringify({ code: "ERROR", message: KEY }), { status: 503 });
      configured = method !== "DELETE";
      return new Response(JSON.stringify({ code: "OK", data: { vision_configured: configured, vision_credential_updated_at: null } }));
    }
    return new Response(JSON.stringify({ code: "OK", data: url.endsWith("/workers") && method === "GET" ? { items: [worker()] } : worker() }));
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear(); });
async function edit() {
  render(<WorkersPage />);
  fireEvent.click(await screen.findByRole("row", { name: /Windows A/ }));
  fireEvent.click(await screen.findByRole("button", { name: "编辑 Worker" }));
  return screen.getByLabelText(/Vision Key/);
}
it("creates with a password field without browser persistence", async () => {
  render(<WorkersPage />);
  fireEvent.click(screen.getByRole("button", { name: "新增 Worker" }));
  const modal = screen.getByRole("form", { name: "新增 Worker" });
  const field = within(modal).getByLabelText(/Vision Key/);
  expect(field).toHaveAttribute("type", "password");
  fireEvent.change(within(modal).getByLabelText(/Worker 名称/), { target: { value: "Windows B" } });
  fireEvent.change(field, { target: { value: KEY } });
  fireEvent.click(within(modal).getByRole("button", { name: "保存" }));
  await waitFor(() => expect(screen.queryByRole("form", { name: "新增 Worker" })).not.toBeInTheDocument());
  expect(calls.find((call) => call.method === "POST")?.body.vision_api_key).toBe(KEY);
  expect(JSON.stringify(localStorage) + JSON.stringify(sessionStorage)).not.toContain(KEY);
  expect(document.body.textContent).not.toContain(KEY);
});
it("blank edit retains the existing credential", async () => {
  expect(await edit()).toHaveValue("");
  expect(screen.getByRole("status")).toHaveTextContent("已配置");
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await screen.findByRole("button", { name: "编辑 Worker" });
  expect(calls.filter((call) => call.url.endsWith("/vision-credential"))).toHaveLength(0);
});
it("replaces via the dedicated endpoint and clears input", async () => {
  fireEvent.change(await edit(), { target: { value: KEY } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(screen.queryByLabelText(/Vision Key/)).not.toBeInTheDocument());
  const writes = calls.filter((call) => call.method === "PUT");
  expect(writes).toHaveLength(2);
  expect(writes[0].body).not.toHaveProperty("vision_api_key");
  expect(writes[1].body).toEqual({ vision_api_key: KEY });
  fireEvent.click(screen.getByRole("button", { name: "编辑 Worker" }));
  expect(screen.getByLabelText(/Vision Key/)).toHaveValue("");
});
it("partial failure hides the server secret and allows retry", async () => {
  failCredential = true;
  fireEvent.change(await edit(), { target: { value: KEY } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await screen.findByText("基础信息已保存，Vision Key 保存失败，请重新填写后重试。");
  expect(screen.getByLabelText(/Vision Key/)).toHaveValue("");
  expect(document.body.textContent).not.toContain(KEY);
  expect(screen.getByRole("button", { name: "保存" })).toBeEnabled();
});
it("requires a key for creation and has no clear action or explanatory text", async () => {
  render(<WorkersPage />);
  fireEvent.click(screen.getByRole("button", { name: "新增 Worker" }));
  const modal = screen.getByRole("form", { name: "新增 Worker" });
  fireEvent.change(within(modal).getByLabelText(/Worker 名称/), { target: { value: "Windows B" } });
  expect(within(modal).getByLabelText(/Vision Key/)).toBeRequired();
  expect(within(modal).getByRole("button", { name: "保存" })).toBeDisabled();
  expect(screen.queryByText("未配置时不能开始新 C2 读取。")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "清除 Vision Key" })).not.toBeInTheDocument();
  fireEvent.change(within(modal).getByLabelText(/Vision Key/), { target: { value: "   " } });
  expect(within(modal).getByRole("button", { name: "保存" })).toBeDisabled();
});
it("requires filling an unconfigured Worker when editing", async () => {
  configured = false;
  const input = await edit();
  expect(input).toBeRequired();
  expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
  fireEvent.change(input, { target: { value: KEY } });
  expect(screen.getByRole("button", { name: "保存" })).toBeEnabled();
});
it("closing the drawer clears transient input", async () => {
  fireEvent.change(await edit(), { target: { value: KEY } });
  fireEvent.click(screen.getByRole("button", { name: "关闭 Worker 详情" }));
  fireEvent.click(screen.getByRole("row", { name: /Windows A/ }));
  expect(screen.getByLabelText(/Vision Key/)).toHaveValue("");
});
