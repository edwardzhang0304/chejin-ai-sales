import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { request } from "../../shared/api/client";
import { LeadsPage } from "./LeadsPage";
import { useLeadsPage } from "./useLeadsPage";
vi.mock("../../shared/api/client", async (original) => ({
  ...await original<typeof import("../../shared/api/client")>(), request: vi.fn(),
}));
const api = vi.mocked(request);
const detail = {
  id: "new-lead", customer_name: "新测试客户", status: "assigned", assign_status: "assigned",
  created_at: "2026-10-26T00:00:00Z", sales_name: "测试销售", contacts: [],
  notes: [], duplicate_events: [], assignments: [], task_nodes: [], custom_fields: {},
};
let review = true;
let failRefresh = false;
let saved = false;
let slowRefresh = false;
let failDetail = false;
beforeEach(() => {
  vi.stubGlobal("scrollTo", vi.fn());
  review = true; failRefresh = false; saved = false; slowRefresh = false; failDetail = false; api.mockReset();
  api.mockImplementation(async (path, options) => {
    if (path === "/leads" && options?.method === "POST") {
      saved = true;
      return { created: true, id: detail.id, next_action: review ? "review_wechat_transfer" : null };
    }
    if (path === "/leads") {
      if (saved && slowRefresh) return new Promise(() => {});
      if (saved && failRefresh) throw new Error("列表刷新失败");
      // A filter may hide the new record; the confirmation must still open.
      return { items: [], total: 0 };
    }
    if (path === "/leads/stats") return {};
    if (path === "/sales") return { items: [] };
    if (path === "/leads/new-lead") {
      if (failDetail) throw new Error("详情加载失败");
      return detail;
    }
    if (path.endsWith("/wechat-bindings")) return { items: [] };
    if (path.endsWith("/wechat/transfer") && options?.method === "POST") return { authorized: true };
    if (path.endsWith("/wechat/transfer")) return { needs_review: true, candidate: {
      binding_id: "old-binding", binding_revision: 2, source_task_id: "new-task", previous_lead_id: "old-lead",
      previous_name: "旧测试客户", previous_remark_code: "CJOLD01", target_name: "新测试客户",
    } };
    throw new Error(`Unexpected request ${path}`);
  });
});
afterEach(cleanup);
async function create(label: string) {
  render(<LeadsPage />);
  fireEvent.click(screen.getByRole("button", { name: "新增客户" }));
  fireEvent.change(screen.getByLabelText("客户名称 *"), { target: { value: "新测试客户" } });
  fireEvent.change(screen.getByLabelText("手机号 *"), { target: { value: "13800000003" } });
  fireEvent.click(screen.getByRole("button", { name: label }));
}
it.each(["保存", "保存并继续新增"])("%s opens the shared transfer confirmation and only grants on explicit confirmation", async (label) => {
  await create(label);
  expect(await screen.findByRole("alertdialog")).toHaveTextContent("旧测试客户（CJOLD01）");
  expect(api.mock.calls.filter(([path, options]) => path === "/leads" && options?.method === "POST")).toHaveLength(1);
  expect(api.mock.calls.some(([path, options]) => path.endsWith("/wechat/transfer") && options?.method === "POST")).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "确认以后由新档案处理" }));
  expect(await screen.findByText(/已确认档案接续/)).toBeVisible();
  expect(api.mock.calls.filter(([path, options]) => path.endsWith("/wechat/transfer") && options?.method === "POST")).toHaveLength(1);
  expect(api.mock.calls.some(([path]) => path.endsWith("/reinvite"))).toBe(false);
});
it("keeps the review entry even when the post-save list refresh fails", async () => {
  failRefresh = true;
  await create("保存");
  expect(await screen.findByRole("alertdialog")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(screen.getByRole("button", { name: "接续已有微信" })).toBeVisible();
});
it("ordinary creation keeps the existing continue-adding flow", async () => {
  review = false;
  await create("保存并继续新增");
  await waitFor(() => expect(screen.getByLabelText("客户名称 *")).toHaveValue(""));
  expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  expect(api.mock.calls.some(([path]) => path.endsWith("/wechat/transfer"))).toBe(false);
});

it("opens confirmation without waiting for a slow list refresh", async () => {
  slowRefresh = true;
  await create("保存");
  expect(await screen.findByRole("alertdialog")).toBeVisible();
});
it("detail failure offers retry without creating a second customer", async () => {
  failDetail = true;
  await create("保存");
  expect(await screen.findByRole("button", { name: "关闭详情" })).toBeVisible();
  failDetail = false;
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByRole("alertdialog")).toBeVisible();
  expect(api.mock.calls.filter(([path, options]) => path === "/leads" && options?.method === "POST")).toHaveLength(1);
});
it("detail failure can close and leaves authorization unchanged", async () => {
  failDetail = true;
  await create("保存");
  fireEvent.click(await screen.findByRole("button", { name: "关闭详情" }));
  await waitFor(() => expect(screen.queryByRole("complementary", { name: "线索详情" })).not.toBeInTheDocument());
  expect(api.mock.calls.some(([path, options]) => path.endsWith("/wechat/transfer") && options?.method === "POST")).toBe(false);
});

it.each(["保存", "保存并继续新增"])("%s keeps required review when an older list response arrives", async (label) => {
  const normal = api.getMockImplementation()!;
  let finishList: (value: unknown) => void = () => {};
  let firstList = true;
  api.mockImplementation((path, options) => {
    if (path === "/leads" && options?.method !== "POST" && firstList) {
      firstList = false;
      return new Promise((resolve) => { finishList = resolve; });
    }
    return normal(path, options);
  });
  await create(label);
  expect(await screen.findByRole("alertdialog")).toBeVisible();
  await act(async () => { finishList({ items: [], total: 0 }); });
  expect(screen.getByRole("alertdialog")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(screen.getByRole("button", { name: "接续已有微信" })).toBeVisible();
  expect(api.mock.calls.some(([path, options]) => path.endsWith("/wechat/transfer") && options?.method === "POST")).toBe(false);
});

it("retains only required review selection; ordinary filtered-out details still close", async () => {
  const { result } = renderHook(() => useLeadsPage(false));
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => { result.current.setActiveLeadId("new-lead"); });
  await act(async () => { await result.current.refresh(); });
  expect(result.current.activeLeadId).toBeNull();
  await act(async () => { result.current.setActiveLeadId("new-lead", { retainUntilClose: true }); });
  await act(async () => { await result.current.refresh(); });
  expect(result.current.activeLeadId).toBe("new-lead");
  await act(async () => { result.current.setActiveLeadId(null); });
  expect(result.current.activeLeadId).toBeNull();
});
