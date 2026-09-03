import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const knowledgeState = vi.hoisted(() => ({
  items: [
    {
      id: "knowledge-1",
      title: "价格咨询回复边界",
      content: "只能引用已发布的公开售价，具体优惠由销售确认。",
      status: "published",
      current_revision_id: "revision-1",
      draft_revision_id: null,
      draft_operation: null,
      published_title: "价格咨询回复边界",
      published_content: "只能引用已发布的公开售价，具体优惠由销售确认。",
      last_editor_id: "operator-1",
      last_editor_name: "运营小陈",
      published_at: "2026-09-03T09:42:00+08:00",
      archived_at: null,
      created_at: "2026-09-01T09:42:00+08:00",
      updated_at: "2026-09-03T09:42:00+08:00",
    },
  ],
  previewCalls: [] as Array<Record<string, unknown>>,
  confirmCalls: [] as Array<Record<string, unknown>>,
  draftCalls: [] as Array<Record<string, unknown>>,
}));

const release = {
  id: "release-1",
  version: "KR-20260903-02",
  status: "published" as const,
  action: "update" as const,
  operator_name: "运营小陈",
  change_summary: "修改 1 条知识变更",
  change_set: [],
  snapshot_sha256: "a".repeat(64),
  retrieval_index_sha256: "c".repeat(64),
  published_at: "2026-09-03T09:42:00+08:00",
  is_current: true,
};

vi.mock("./api", () => ({
  getKnowledgeSummary: vi.fn(async () => ({ current_release: release, published_today: 2, published_today_breakdown: { create: 1, update: 1, archive: 0, rollback: 0 }, published_count: knowledgeState.items.length, draft_count: 0, archived_count: 0 })),
  listKnowledgeItems: vi.fn(async () => ({ items: knowledgeState.items, page: 1, page_size: 20, total: knowledgeState.items.length })),
  getKnowledgeItem: vi.fn(async () => ({ ...knowledgeState.items[0], release_history: [release] })),
  createKnowledgeDraft: vi.fn(async (payload: Record<string, unknown>) => {
    knowledgeState.draftCalls.push({ operation: "create", ...payload });
    return {
      ...knowledgeState.items[0],
      id: "knowledge-new",
      title: payload.title,
      content: payload.content,
      status: "draft",
      current_revision_id: null,
      draft_revision_id: "draft-new",
      draft_operation: "create",
      published_title: null,
      published_content: null,
      published_at: null,
    };
  }),
  updateKnowledgeDraft: vi.fn(async (itemId: string, payload: Record<string, unknown>) => {
    knowledgeState.draftCalls.push({ operation: "update", itemId, ...payload });
    return {
      ...knowledgeState.items[0],
      title: payload.title,
      content: payload.content,
      draft_revision_id: "draft-update",
      draft_operation: "update",
    };
  }),
  stageKnowledgeArchive: vi.fn(),
  previewKnowledgeRelease: vi.fn(async (payload: Record<string, unknown>) => {
    knowledgeState.previewCalls.push(payload);
    return {
      preview_id: "preview-1",
      operation: payload.operation,
      item_id: payload.item_id || "knowledge-new",
      current_version: release.version,
      target_version: "KR-20260903-03",
      target_release_id: null,
      can_publish: true,
      validation_issues: [],
      change_set: [{ type: payload.operation, item_id: payload.item_id || "knowledge-new", title: payload.title || "新知识", before: null, after: { title: payload.title || "新知识", content: payload.content || "新内容" } }],
      content_digest: "b".repeat(64),
      expires_at: "2026-09-03T10:00:00+08:00",
    };
  }),
  confirmKnowledgeRelease: vi.fn(async (previewId: string, contentDigest: string) => {
    knowledgeState.confirmCalls.push({ previewId, contentDigest });
    return { release: { ...release, id: "release-2", version: "KR-20260903-03", snapshot: [] }, item: null, message: "发布成功" };
  }),
  listKnowledgeReleases: vi.fn(async () => ({ items: [release], page: 1, page_size: 20, total: 1 })),
  getKnowledgeRelease: vi.fn(async () => ({ ...release, snapshot: [] })),
  previewKnowledgeRollback: vi.fn(),
}));

import { KnowledgePage } from "./KnowledgePage";

afterEach(() => {
  cleanup();
  knowledgeState.previewCalls.length = 0;
  knowledgeState.confirmCalls.length = 0;
  knowledgeState.draftCalls.length = 0;
});

describe("知识管理主流程", () => {
  it("默认显示列表但不自动打开首条详情", async () => {
    render(<KnowledgePage />);
    expect(await screen.findByText("价格咨询回复边界")).toBeTruthy();
    expect(screen.queryByRole("complementary", { name: "知识详情" })).toBeNull();
    expect(screen.queryByRole("button", { name: "发布变更" })).toBeNull();
  });

  it("编辑先保存草稿，再从详情发布", async () => {
    render(<KnowledgePage />);
    const title = await screen.findByText("价格咨询回复边界");
    fireEvent.click(title.closest("tr") as HTMLTableRowElement);
    expect(await screen.findByRole("complementary", { name: "知识详情" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "编辑知识" }));
    expect(screen.getByRole("button", { name: "取消" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "保存草稿" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "发布" })).toBeNull();
    fireEvent.change(screen.getByPlaceholderText("例如：客户询问价格时的回复规则"), { target: { value: "价格咨询新规则" } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(knowledgeState.draftCalls).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "发布" }));
    expect(await screen.findByRole("dialog", { name: "确认发布知识" })).toBeTruthy();
    expect(knowledgeState.previewCalls).toEqual([expect.objectContaining({ operation: "update", item_id: "knowledge-1" })]);
  });

  it("新增内容只保存为草稿，不会自动发布", async () => {
    render(<KnowledgePage />);
    await screen.findByText("价格咨询回复边界");
    fireEvent.click(screen.getByRole("button", { name: "新增知识" }));
    fireEvent.change(screen.getByPlaceholderText("例如：客户询问价格时的回复规则"), { target: { value: "新知识" } });
    fireEvent.change(screen.getByPlaceholderText("填写 Brain 可检索和引用的正式业务知识"), { target: { value: "这是正式业务知识。" } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(knowledgeState.draftCalls).toEqual([expect.objectContaining({ operation: "create", title: "新知识" })]));
    expect(knowledgeState.previewCalls).toEqual([]);
    expect(knowledgeState.confirmCalls).toEqual([]);
  });

  it("当前版本卡可进入发布记录", async () => {
    render(<KnowledgePage />);
    await screen.findByText("价格咨询回复边界");
    fireEvent.click(screen.getByRole("button", { name: /\u5f53\u524d\u7ebf\u4e0a\u7248\u672c/ }));
    expect(await screen.findByRole("complementary", { name: "知识发布记录" })).toBeTruthy();
    expect(screen.getAllByText("KR-20260903-02").length).toBeGreaterThan(1);
  });
});
