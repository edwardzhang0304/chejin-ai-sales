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
  api.mockResolvedValueOnce({ items: [target] });
  let resolve: (value: unknown) => void = () => {};
  api.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
  render(<HookReinviteAction leadId="lead-one" />);
  fireEvent.click(await screen.findByRole("button", { name: "重新邀请" }));
  expect(screen.getByRole("alertdialog").textContent).toContain("旧消息保留原记录继续核对，不会补发");
  expect(api).toHaveBeenCalledTimes(1);
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
  api.mockResolvedValueOnce({ items: [target] }).mockRejectedValueOnce(new Error("旧托管状态已变化"))
    .mockResolvedValueOnce({ items: [] });
  render(<HookReinviteAction leadId="lead-one" />);
  fireEvent.click(await screen.findByRole("button", { name: "重新邀请" }));
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(api).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "重新邀请" }));
  fireEvent.click(screen.getByRole("button", { name: "结束旧托管并重新邀请" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("旧托管状态已变化");
  fireEvent.click(screen.getByRole("button", { name: "刷新托管状态" }));
  expect(await screen.findByText("当前没有可重新邀请的托管记录。")).toBeVisible();
});
it("does not choose an ambiguous binding", async () => {
  api.mockResolvedValueOnce({ items: [target, { ...target, id: "another" }] });
  render(<HookReinviteAction leadId="lead-one" />);
  await screen.findByText("当前没有可重新邀请的托管记录。");
  expect(screen.queryByRole("button", { name: "重新邀请" })).not.toBeInTheDocument();
});
