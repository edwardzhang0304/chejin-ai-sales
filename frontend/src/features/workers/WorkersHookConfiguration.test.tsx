import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WorkersPage } from "./WorkersPage";

let configured = false;
let hookContext: Record<string, unknown> | undefined;
let calls: { url: string; method: string; body: Record<string, unknown> }[];
let failWrite = false;
const worker = () => ({ id: "worker-1", worker_name: "Windows A", device_name: null, platform: "windows", enabled: true, online_status: "offline", run_status: "paused", running_status: "idle", current_task: null, last_heartbeat_at: null, client_binding_state: "unbound", remark: null, bound_sales_id: null, bound_sales_name: null, vision_configured: configured, hook_context: hookContext });
beforeEach(() => {
  vi.stubGlobal("scrollTo", vi.fn());
  calls = []; configured = false; hookContext = undefined; failWrite = false;
  vi.stubGlobal("fetch", vi.fn(async (input, options = {}) => {
    const url = String(input); const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : {};
    calls.push({ url, method, body });
    if (url.endsWith("/vision-credential")) throw new Error("Hook must not request Vision credentials");
    if (failWrite && method !== "GET") return new Response(JSON.stringify({ code: "ERROR", message: "保存暂时不可用" }), { status: 503 });
    return new Response(JSON.stringify({ code: "OK", data: url.endsWith("/workers") && method === "GET" ? { items: [worker()] } : worker() }));
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("creates a never-bound Worker without a Vision field or credential payload", async () => {
  render(<WorkersPage />);
  fireEvent.click(screen.getByRole("button", { name: "新增 Worker" }));
  const modal = screen.getByRole("form", { name: "新增 Worker" });
  expect(within(modal).queryByLabelText(/Vision/)).not.toBeInTheDocument();
  expect(within(modal).getByRole("button", { name: "保存" })).toBeDisabled();
  fireEvent.change(within(modal).getByLabelText(/Worker 名称/), { target: { value: "Windows B" } });
  expect(within(modal).getByRole("button", { name: "保存" })).toBeEnabled();
  fireEvent.click(within(modal).getByRole("button", { name: "保存" }));
  await waitFor(() => expect(screen.queryByRole("form", { name: "新增 Worker" })).not.toBeInTheDocument());
  expect(calls.filter((call) => call.method === "POST")).toEqual([
    expect.objectContaining({ body: { worker_name: "Windows B", platform: "windows", enabled: true, device_name: null, remark: null } }),
  ]);
  expect(document.body.textContent).not.toContain("Vision");
});

it.each([false, true])("edits Worker with existing credential configured=%s without reading, replacing or clearing it", async (value) => {
  configured = value;
  render(<WorkersPage />);
  fireEvent.click(await screen.findByRole("row", { name: /Windows A/ }));
  fireEvent.click(await screen.findByRole("button", { name: "编辑 Worker" }));
  expect(screen.queryByLabelText(/Vision/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "保存" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await screen.findByRole("button", { name: "编辑 Worker" });
  expect(calls.filter((call) => call.method === "PUT")).toEqual([
    expect.objectContaining({ body: { worker_name: "Windows A", device_name: null, enabled: true, remark: null } }),
  ]);
  expect(calls.some((call) => call.url.includes("vision-credential"))).toBe(false);
  expect(document.body.textContent).not.toContain("Vision");
});

it("shows Hook connection information without a missing Vision warning", async () => {
  hookContext = { self_wxid: "synthetic-hook-account", blocked: false };
  render(<WorkersPage />);
  fireEvent.click(await screen.findByRole("row", { name: /Windows A/ }));
  expect(await screen.findByText("synthetic-hook-account")).toBeInTheDocument();
  expect(document.body.textContent).not.toContain("Vision");
  expect(screen.getByRole("button", { name: "重置绑定" })).toBeEnabled();
});

it("keeps creation errors retryable without requesting a Vision Key", async () => {
  failWrite = true;
  render(<WorkersPage />);
  fireEvent.click(screen.getByRole("button", { name: "新增 Worker" }));
  const modal = screen.getByRole("form", { name: "新增 Worker" });
  fireEvent.change(within(modal).getByLabelText(/Worker 名称/), { target: { value: "Windows B" } });
  fireEvent.click(within(modal).getByRole("button", { name: "保存" }));
  await waitFor(() => expect(within(modal).getByRole("button", { name: "保存" })).toBeEnabled());
  expect(document.body.textContent).not.toContain("Vision");
  failWrite = false;
  fireEvent.click(within(modal).getByRole("button", { name: "保存" }));
  await waitFor(() => expect(screen.queryByRole("form", { name: "新增 Worker" })).not.toBeInTheDocument());
});
