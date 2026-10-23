import type { WorkerItem } from "./types";

type WorkerState = Pick<WorkerItem, "operational_status" | "online_status" | "run_status" | "running_status" | "current_task" | "hook_context">;

export function workerIsBusy(worker: Pick<WorkerState, "current_task" | "running_status">) {
  return Boolean(worker.current_task) || worker.running_status === "running";
}

export function workerStatus(worker: WorkerState) {
  if (worker.hook_context && worker.operational_status) {
    const states = {
      running: { label: "接单中", className: "assigned" },
      paused: { label: "暂停接单", className: "unassigned" },
      faulted: { label: "已停止接单（客户端故障）", className: "invalid" },
      offline: { label: "离线", className: "unassigned" },
      unbound: { label: "客户端未绑定", className: "unassigned" },
      unavailable: { label: "状态待确认", className: "unassigned" },
    };
    return states[worker.operational_status];
  }
  if (worker.online_status !== "online") return { label: "离线", className: "unassigned" };
  if (worker.run_status === "faulted") return { label: "已停止接单（客户端故障）", className: "invalid" };
  if (worker.hook_context) {
    if (worker.run_status === "paused") return { label: "暂停接单", className: "unassigned" };
    if (worker.run_status === "running") return { label: "接单中", className: "assigned" };
    return { label: "状态待确认", className: "unassigned" };
  }
  const busy = workerIsBusy(worker);
  return { label: `在线 / ${busy ? "忙碌" : "空闲"}`, className: busy ? "unassigned" : "assigned" };
}

const hookReasons: Record<string, string> = {
  HOOK_ACCOUNT_CHANGED: "工作微信账号已变更，需要重新绑定",
  HOOK_SALES_BINDING_CHANGED: "绑定销售已变更，需要重新绑定",
  HOOK_ACCOUNT_EPOCH_MISMATCH: "微信账号绑定记录不一致",
  HOOK_ACCOUNT_ALREADY_BOUND: "此微信账号已绑定其他 Worker",
  HOOK_CONTEXT_REQUIRED: "未收到微信连接检查信息",
  HOOK_CLIENT_BINDING_REQUIRED: "客户端尚未完成绑定",
  HOOK_SALES_BINDING_REQUIRED: "尚未绑定可用销售",
  HOOK_LOGIN_REQUIRED: "微信尚未登录",
};

export function hookConnectionLabel(context: NonNullable<WorkerItem["hook_context"]>) {
  if (!context.blocked) return "检查通过";
  return hookReasons[context.reason_code || ""] || "检查未通过，请查看客户端故障提示";
}
