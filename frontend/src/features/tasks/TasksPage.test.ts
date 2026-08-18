import { describe, expect, it } from "vitest";

import { buildActions, formatTaskBusinessCode, taskEventTitle } from "./TasksPage";
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
