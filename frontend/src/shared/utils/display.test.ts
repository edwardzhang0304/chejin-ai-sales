import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { displayValue, formatRelativeHeartbeat, optionalText } from "./display";

describe("display utils", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-08T03:00:00.000Z"));
  });

  afterEach(() => vi.useRealTimers());

  it("统一处理空值和可选表单文字", () => {
    expect(displayValue(null)).toBe("-");
    expect(displayValue("", "暂无")).toBe("暂无");
    expect(displayValue(0)).toBe("0");
    expect(optionalText("  wx_test  ")).toBe("wx_test");
    expect(optionalText("   ")).toBeNull();
  });

  it("统一销售和 Worker 的最近心跳文案", () => {
    expect(formatRelativeHeartbeat("2026-08-08T02:59:00.000Z")).toBe("刚刚");
    expect(formatRelativeHeartbeat("2026-08-08T02:58:00.000Z")).toBe("2 分钟前");
    expect(formatRelativeHeartbeat("not-a-date")).toBe("not-a-date");
  });
});
