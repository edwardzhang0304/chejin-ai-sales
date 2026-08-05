import { describe, expect, it, vi } from "vitest";

import { POST_MUTATION_REFRESH_FAILED_MESSAGE, postMutationMessage, runPostMutationRefresh } from "./postMutation";

describe("runPostMutationRefresh", () => {
  it("刷新成功时返回 true", async () => {
    await expect(runPostMutationRefresh(() => true)).resolves.toBe(true);
  });

  it("刷新明确失败时返回 false", async () => {
    await expect(runPostMutationRefresh(() => false)).resolves.toBe(false);
  });

  it("刷新抛错时降级为 false，不把错误冒充成写操作失败", async () => {
    const refresh = vi.fn().mockRejectedValue(new Error("refresh failed"));

    await expect(runPostMutationRefresh(refresh)).resolves.toBe(false);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("刷新失败时统一显示操作已成功并要求手动刷新", () => {
    expect(postMutationMessage("车辆资料已保存。", false)).toBe(POST_MUTATION_REFRESH_FAILED_MESSAGE);
    expect(postMutationMessage("车辆资料已保存。", true)).toBe("车辆资料已保存。");
  });
});
