import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { request } from "../../../shared/api/client";
import { HookReinviteAction } from "./HookReinviteAction";
vi.mock("../../../shared/api/client", () => ({ request: vi.fn() }));
const api = vi.mocked(request);
const target = { id: "binding-one", authorization_revision: 4, remark_code: "CJTEST1", reinvite_available: true };
beforeEach(() => api.mockReset());
afterEach(cleanup);
it("requires confirmation, submits the selected revision once and shows the next step", async () => {
  api.mockResolvedValueOnce({ items: [target] }).mockResolvedValueOnce({ candidate: null, needs_review: false });
  let resolve: (value: unknown) => void = () => {};
  api.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
  render(<HookReinviteAction leadId="lead-one" />);
  fireEvent.click(await screen.findByRole("button", { name: "重新邀请" }));
  expect(screen.getByRole("alertdialog").textContent).toContain("旧消息保留原记录继续核对，不会补发");
  expect(api).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole("button", { name: "结束旧托管并重新邀请" }));
  expect(screen.getByRole("button", { name: "处理中..." })).toBeDisabled();
  expect(api).toHaveBeenLastCalledWith("/leads/lead-one/wechat/reinvite", { method: "POST", body: {
    binding_id: "binding-one", binding_revision: 4, confirm_end_previous: true, reason: "运营确认结束旧托管并重新邀请",
  } });
  resolve({ created: true });
  await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  expect(screen.getByRole("status").textContent).toContain("重新邀请任务已创建");
  expect(screen.queryByRole("button", { name: "重新邀请" })).not.toBeInTheDocument();
});
it("cancel keeps authorization; stale failure stays visible and allows refresh", async () => {
  api.mockResolvedValueOnce({ items: [target] }).mockResolvedValueOnce({ candidate: null, needs_review: false }).mockRejectedValueOnce(new Error("旧托管状态已变化"))
    .mockResolvedValueOnce({ items: [] }).mockResolvedValueOnce({ candidate: null, needs_review: false });
  render(<HookReinviteAction leadId="lead-one" />);
  fireEvent.click(await screen.findByRole("button", { name: "重新邀请" }));
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(api).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole("button", { name: "重新邀请" }));
  fireEvent.click(screen.getByRole("button", { name: "结束旧托管并重新邀请" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("旧托管状态已变化");
  fireEvent.click(screen.getByRole("button", { name: "刷新托管状态" }));
  expect(await screen.findByText("当前没有可重新邀请的托管记录。")).toBeVisible();
});
it("does not choose an ambiguous binding", async () => {
  api.mockResolvedValueOnce({ items: [target, { ...target, id: "another" }] }).mockResolvedValueOnce({ candidate: null, needs_review: false });
  render(<HookReinviteAction leadId="lead-one" />);
  await screen.findByText("当前没有可重新邀请的托管记录。");
  expect(screen.queryByRole("button", { name: "重新邀请" })).not.toBeInTheDocument();
});

it("shows both records before transfer and never invokes reinvite", async () => {
  api.mockResolvedValueOnce({ items: [] }).mockResolvedValueOnce({ needs_review: true, candidate: {
    binding_id: "old-binding", binding_revision: 2, source_task_id: "new-task", previous_lead_id: "old-lead",
    previous_name: "旧测试客户", previous_remark_code: "CJOLD01", target_name: "新测试客户",
  } }).mockResolvedValueOnce({ authorized: true });
  render(<HookReinviteAction leadId="new-lead" />);
  fireEvent.click(await screen.findByRole("button", { name: "接续已有微信" }));
  expect(screen.getByRole("alertdialog")).toHaveTextContent("旧测试客户（CJOLD01）");
  expect(screen.getByRole("alertdialog")).toHaveTextContent("新测试客户");
  expect(api).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole("button", { name: "确认以后由新档案处理" }));
  await screen.findByText(/已确认档案接续/);
  expect(api).toHaveBeenLastCalledWith("/leads/new-lead/wechat/transfer", { method: "POST", body: {
    binding_id: "old-binding", binding_revision: 2, source_task_id: "new-task", confirm_end_previous: true,
    reason: "运营确认同一微信以后由当前新档案处理",
  } });
  expect(api.mock.calls.some(([url]) => url.endsWith("/reinvite"))).toBe(false);
  expect(screen.queryByRole("button", { name: "接续已有微信" })).not.toBeInTheDocument();
});

it("refresh keeps an authorized transfer pending without another confirm button", async () => {
  api.mockResolvedValueOnce({ items: [] }).mockResolvedValueOnce({ candidate: null, needs_review: true, pending_verification: true });
  render(<HookReinviteAction leadId="new-lead" />);
  expect(await screen.findByText(/已确认档案接续。等待 Worker 核验原微信身份/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "接续已有微信" })).not.toBeInTheDocument();
});

it("prompts a newly created customer immediately but cancel never grants transfer", async () => {
  api.mockResolvedValueOnce({ items: [] }).mockResolvedValueOnce({ needs_review: true, candidate: {
    binding_id: "old-binding", binding_revision: 2, source_task_id: "new-task", previous_lead_id: "old-lead",
    previous_name: "旧测试客户", previous_remark_code: "CJOLD01", target_name: "新测试客户",
  } });
  render(<HookReinviteAction leadId="new-lead" promptTransfer />);
  expect(await screen.findByRole("alertdialog")).toHaveTextContent("旧测试客户（CJOLD01）");
  expect(api).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "接续已有微信" })).toBeVisible();
  expect(api.mock.calls.every(([, options]) => !options?.method || options.method === "GET")).toBe(true);
});
it("keeps ambiguous old identities for review without prompting a guessed choice", async () => {
  api.mockResolvedValueOnce({ items: [] }).mockResolvedValueOnce({ candidate: null, needs_review: true });
  render(<HookReinviteAction leadId="new-lead" promptTransfer />);
  expect(await screen.findByText("存在旧档案关联，请先核对客户身份与原任务。")).toBeVisible();
  expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
});
