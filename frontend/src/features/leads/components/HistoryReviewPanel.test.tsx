import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { request } from "../../../shared/api/client";
import { HistoryReviewPanel } from "./HistoryReviewPanel";
vi.mock("../../../shared/api/client", () => ({ request: vi.fn() }));
const api = vi.mocked(request);
const row = { id: "review", reason: "profile_succession", status: "pending", revision: 2, note: null,
  messages: [{ id: "message", content: "旧测试消息", sender_role: "customer", message_type: "text", occurred_at: null }] };
beforeEach(() => api.mockReset());
afterEach(cleanup);
it("requires a note and submits only the review revision, without changing reply ownership", async () => {
  api.mockResolvedValueOnce([row]).mockResolvedValueOnce({ processed: true }).mockResolvedValueOnce([{ ...row, status: "processed", note: "核对完成" }]);
  render(<HistoryReviewPanel leadId="lead" />);
  expect(await screen.findByText("旧测试消息")).toBeVisible();
  expect(screen.getByRole("button", { name: "标记已处理" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("核对说明"), { target: { value: "核对完成" } });
  fireEvent.click(screen.getByRole("button", { name: "标记已处理" }));
  await screen.findByText("核对说明：核对完成");
  expect(api.mock.calls.filter(([, options]) => options?.method === "POST")).toEqual([
    ["/leads/lead/wechat/history-reviews/review/resolve", { method: "POST", body: { revision: 2, note: "核对完成" } }],
  ]);
});
it("keeps stale-revision errors visible without marking a review done", async () => {
  api.mockResolvedValueOnce([row]).mockRejectedValueOnce(new Error("待核对内容已更新"));
  render(<HistoryReviewPanel leadId="lead" />);
  fireEvent.change(await screen.findByLabelText("核对说明"), { target: { value: "核对" } });
  fireEvent.click(screen.getByRole("button", { name: "标记已处理" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("待核对内容已更新");
  expect(screen.getByText(/待核对 ·/)).toBeVisible();
});
it("does not replace another customer's results with a late load", async () => {
  let finish: (value: unknown) => void = () => {};
  api.mockImplementationOnce(() => new Promise((done) => { finish = done; })).mockResolvedValueOnce([]);
  const view = render(<HistoryReviewPanel leadId="old" />);
  view.rerender(<HistoryReviewPanel leadId="new" />);
  await screen.findByText("暂无历史核对事项");
  finish([row]);
  await waitFor(() => expect(screen.queryByText("旧测试消息")).not.toBeInTheDocument());
});
it("shows withdrawn evidence while retaining its original content", async () => {
  api.mockResolvedValueOnce([{ ...row, messages: [{ ...row.messages[0], withdrawn: true }] }]);
  render(<HistoryReviewPanel leadId="lead" />);
  expect(await screen.findByText("已撤回（原记录保留）")).toBeVisible();
  expect(screen.getByText("旧测试消息")).toBeVisible();
});

it("distinguishes an old system notice from a withdrawn customer message", async () => {
  api.mockResolvedValueOnce([{ ...row, messages: [{ ...row.messages[0], system_notice: true, withdrawn: false }] }]);
  render(<HistoryReviewPanel leadId="lead" />);
  expect(await screen.findByText("系统提示（原记录保留）")).toBeVisible();
  expect(screen.queryByText("已撤回（原记录保留）")).not.toBeInTheDocument();
});
