import { createElement } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { listOperationLogs } from "./api";
import {
  formatOperationSnapshot,
  LogsPage,
  operationEventLabel,
  operationModuleOptions,
  operationObjectName,
  operationSummary,
} from "./LogsPage";
import type { OperationLogItem } from "./types";

vi.mock("./api", () => ({
  listOperationLogs: vi.fn(),
}));

const listOperationLogsMock = vi.mocked(listOperationLogs);

beforeEach(() => {
  listOperationLogsMock.mockReset();
  document.body.removeAttribute("style");
  document.documentElement.removeAttribute("style");
});

afterEach(() => {
  cleanup();
  document.body.removeAttribute("style");
  document.documentElement.removeAttribute("style");
});

function logItem(patch: Partial<OperationLogItem>): OperationLogItem {
  return {
    id: "log-1",
    event_type: "sales_worker_bound",
    module: "sales",
    operator_id: null,
    operator_name: "运营小陈",
    target_type: "sales",
    target_id: "6ffba552-0d53-4c28-95c4-8d6f4a410999",
    lead_id: null,
    result: "success",
    created_at: "2026-08-07T10:00:00+08:00",
    ...patch,
  };
}

describe("操作日志运营文案", () => {
  it("模块筛选文案不重复", () => {
    const labels = operationModuleOptions.map((item) => item.label);
    expect(new Set(labels).size).toBe(labels.length);
  });

  it("未知操作和 UUID 不直接展示给运营", () => {
    expect(operationEventLabel(logItem({ event_type: "unknown_event" }))).toBe("-");
    expect(operationObjectName(logItem({}))).toBe("-");
  });

  it("旧媒体归属事故显示明确操作类型", () => {
    expect(
      operationEventLabel(
        logItem({ event_type: "worker_legacy_media_owner_unknown" }),
      ),
    ).toBe("旧媒体归属待人工检查");
  });

  it("变更前后转成业务语言并隐藏 Worker UUID", () => {
    expect(formatOperationSnapshot({ worker_id: null, status: "unassigned" })).toBe("未绑定 Worker");
    expect(formatOperationSnapshot({ worker_id: "6ffba552-0d53-4c28-95c4-8d6f4a410999" })).toBe("已绑定 Worker");
    expect(operationSummary(logItem({ summary: "绑定 Worker：6ffba552-0d53-4c28-95c4-8d6f4a410999" }))).toBe("已绑定 Worker");
  });

  it("打开日志详情时不锁定或偏移整个后台页面", async () => {
    listOperationLogsMock.mockResolvedValue({
      items: [logItem({})],
      page: 1,
      page_size: 20,
      total: 1,
    });

    render(createElement(LogsPage));
    fireEvent.click(await screen.findByRole("row", { name: /运营小陈.*更换 Worker/ }));

    expect(await screen.findByRole("complementary", { name: "操作日志详情" })).toBeTruthy();
    expect(document.body.style.position).toBe("");
    expect(document.body.style.top).toBe("");
    expect(document.body.style.overflow).toBe("");
    expect(document.documentElement.style.overflow).toBe("");
  });
});
