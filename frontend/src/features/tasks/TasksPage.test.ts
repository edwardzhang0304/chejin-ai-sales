import { describe, expect, it } from "vitest";

import { aggregateResult, buildActions, formatTaskBusinessCode, taskEventTitle } from "./TasksPage";
import type { TaskEvent, TaskListItem } from "./types";

function taskEvent(patch: Partial<TaskEvent>): TaskEvent {
  return {
    id: "event-1",
    task_id: "task-1",
    event_type: "step_updated",
    from_status: "running",
    to_status: "running",
    current_step: null,
    operator_name: "Mac-01",
    result_code: null,
    error_code: null,
    block_code: null,
    remark: null,
    created_at: "2026-08-07T10:00:00+08:00",
    ...patch,
  };
}

describe("任务中心中文业务映射", () => {
  it.each([
    ["原生历史事实或顺序已更新", "原生历史事实或顺序已更新"],
    ["SEND_ACK_TIMEOUT", "发送结果确认超时，已转销售接管"],
    ["运营确认取消本次任务", "运营确认取消本次任务"],
    [null, "任务已取消（未记录原因）"],
    [" ", "任务已取消（未记录原因）"],
  ])("取消任务按实际原因展示，不推断操作人：%s", (reason, expected) => {
    expect(aggregateResult({
      status: "cancelled", cancel_reason: reason,
      result_code: null, error_code: null, block_code: null,
    })).toBe(expected);
  });

  it.each(["SEND_ACK_TIMEOUT", "SEND_RESULT_UNKNOWN"])("未知发送保留禁止补发提示：%s", (code) => {
    expect(aggregateResult({
      status: "failed", error_code: code,
      result_code: null, block_code: null, cancel_reason: null,
    })).toBe("自动发送已终止，禁止补发；会话已转销售正常接管");
  });

  it("未知工程码显示短横线，不创造兜底业务状态", () => {
    expect(formatTaskBusinessCode("UNDECLARED_ENGINEERING_CODE")).toBe("-");
  });

  it("映射正式会话恢复失败和读取步骤", () => {
    expect(formatTaskBusinessCode("C2_REPLY_CONTEXT_RECOVERY_FAILED")).toBe("未能恢复客户会话上下文");
    expect(formatTaskBusinessCode("state_target_message_read")).toBe("读取客户最新消息");
  });

  it("任务链路只使用事件中真实到达的步骤和失败原因", () => {
    expect(taskEventTitle(taskEvent({ current_step: "state_target_message_read" }))).toBe("读取客户最新消息");
    expect(taskEventTitle(taskEvent({
      event_type: "failed",
      current_step: "state_target_message_read",
      error_code: "C2_REPLY_CONTEXT_RECOVERY_FAILED",
      to_status: "failed",
    }))).toBe("读取客户最新消息失败：未能恢复客户会话上下文");
  });

  it("失败任务不再展示重复的查看失败原因操作", () => {
    const actions = buildActions({ status: "failed" } as TaskListItem);

    expect(actions).toEqual(["查看执行方", "补充备注"]);
    expect(actions).not.toContain("查看失败原因");
  });
});
