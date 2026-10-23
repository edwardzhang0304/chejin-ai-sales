import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DashboardData } from "./api";
const api = vi.hoisted(() => ({ getDashboard: vi.fn() }));
vi.mock("./api", () => api);
vi.mock("recharts", () => ({
  ResponsiveContainer: () => <div aria-label="趋势图" />,
  AreaChart: () => null,
  Area: () => null,
  CartesianGrid: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  Legend: () => null,
}));
import { DashboardPage, dateRange, formatMetric } from "./DashboardPage";

function data(count = 0): DashboardData {
  const metric = (value: number | null) => ({ value, reason: null });
  const metrics = {
    new_customers: metric(count),
    total_customers: metric(count),
    friend_passes: metric(0),
    friend_pass_rate: metric(null),
    communication_customers: metric(0),
    effective_customers: metric(0),
    effective_rate: metric(null),
    human_response_seconds: metric(null),
    human_replied_rounds: 0,
    human_pending_rounds: 0,
  };
  return {
    as_of: "2026-10-23T02:00:00Z",
    timezone: "Asia/Shanghai",
    start: "",
    end: "",
    metrics,
    sales: [],
    sales_options: [{ id: "sales-a", name: "合成销售", enabled: true }],
    realtime: {
      counts: { running: 0, paused: 0, faulted: 0, offline: 0, unbound: 0 },
      unavailable: 0,
    },
    vehicles: { listed: metric(0), sold: metric(0), amount: metric(0) },
    trend: [],
    coverage: [],
  };
}
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("真实看板请求及缺失数据展示", () => {
  it("日期快捷项含今日，昨日和跨月按北京时间日期计算", () => {
    expect(dateRange("近7天", ["", ""], "2026-03-03")).toEqual([
      "2026-02-25",
      "2026-03-03",
    ]);
    expect(dateRange("昨日", ["", ""], "2026-03-01")).toEqual([
      "2026-02-28",
      "2026-02-28",
    ]);
    expect(
      formatMetric({ value: 0, reason: null }, "communication_customers"),
    ).toBe("0");
    expect(formatMetric({ value: null, reason: null }, "effective_rate")).toBe(
      "—",
    );
    expect(
      formatMetric({ value: 9, reason: "覆盖不足" }, "total_customers"),
    ).toBe("—");
    expect(
      formatMetric({ value: 600, reason: null }, "human_response_seconds"),
    ).toBe("10分 0秒");
  });
  it("服务失败不展示示例数字或伪零，并允许重试", async () => {
    api.getDashboard
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(data());
    render(<DashboardPage />);
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByLabelText("趋势图")).toBeNull();
    expect(screen.queryByText("暂无符合条件的数据")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(await screen.findByText("暂无符合条件的数据")).toBeTruthy();
    expect(api.getDashboard).toHaveBeenCalledTimes(2);
  });
  it("切换筛选取消旧请求，迟到响应不能覆盖新数据", async () => {
    let resolveOld!: (value: DashboardData) => void;
    api.getDashboard.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    );
    api.getDashboard.mockResolvedValueOnce(data(17));
    const { unmount } = render(<DashboardPage />);
    const firstSignal = api.getDashboard.mock.calls[0][1] as AbortSignal;
    fireEvent.click(screen.getByRole("button", { name: "AI", exact: true }));
    await waitFor(() => expect(api.getDashboard).toHaveBeenCalledTimes(2));
    expect(firstSignal.aborted).toBe(true);
    expect(api.getDashboard.mock.calls[1][0].mode).toBe("ai");
    await screen.findAllByText("17");
    resolveOld(data(999));
    await waitFor(() => expect(screen.queryByText("999")).toBeNull());
    unmount();
    expect((api.getDashboard.mock.calls[1][1] as AbortSignal).aborted).toBe(
      true,
    );
  });
});
