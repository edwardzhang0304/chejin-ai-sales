import { describe, expect, it } from "vitest";
import { hookConnectionLabel, workerStatus } from "./status";

const worker = { online_status: "online", run_status: "paused", running_status: "idle", current_task: null, hook_context: { blocked: false } };
describe("运营后台 Hook 状态", () => {
  it("空闲不代表正在接单，故障不代表正常暂停", () => {
    expect(workerStatus(worker).label).toBe("暂停接单");
    expect(workerStatus({ ...worker, run_status: "running" }).label).toBe("接单中");
    expect(workerStatus({ ...worker, run_status: "faulted" }).label).toContain("已停止接单");
    expect(workerStatus({ ...worker, online_status: "offline" }).label).toBe("离线");
  });
  it("OCR 保留忙闲展示，不因增加 Hook 而被误判", () => {
    expect(workerStatus({ ...worker, hook_context: undefined }).label).toBe("在线 / 空闲");
    expect(workerStatus({ ...worker, hook_context: undefined, current_task: "task" }).label).toBe("在线 / 忙碌");
  });
  it("账号切换要求重绑，未声明的错误不假装检查通过", () => {
    expect(hookConnectionLabel({ blocked: true, reason_code: "HOOK_ACCOUNT_CHANGED" })).toContain("重新绑定");
    expect(hookConnectionLabel({ blocked: true, reason_code: "unknown" })).toContain("检查未通过");
  });
});
