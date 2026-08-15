import { useCallback, useEffect, useMemo, useState } from "react";

import { formatBusinessError } from "../../shared/api/client";
import { ClickToCopyText } from "../../shared/ui/ClickToCopyText";
import { CloseIcon } from "../../shared/ui/Icons";
import { Pagination } from "../../shared/ui/Pagination";
import { displayValue as display } from "../../shared/utils/display";
import { postMutationMessage, runPostMutationRefresh } from "../../shared/utils/postMutation";
import { listSales } from "../sales/api";
import type { SalesItem } from "../sales/types";
import { listWorkers } from "../workers/api";
import type { WorkerItem } from "../workers/types";
import { addTaskComment, cancelTask, getTask, listTaskEvents, listTasks } from "./api";
import type { TaskDetail, TaskEvent, TaskListItem, TaskMetrics, TaskQuery, TaskStatus, TaskType } from "./types";

type Props = {
  onOpenWorker: (workerId: string) => void;
  onOpenSalesWorkerBinding: (salesId: string) => void;
};

const initialQuery: TaskQuery = {
  keyword: "",
  task_type: "all",
  status: "all",
  result_code: "all",
  exception_code: "all",
  sales_id: "all",
  worker_id: "all",
  page: 1,
  page_size: 20,
};

const taskPageSizeOptions = [20, 50] as const;

const statusMeta: Record<TaskStatus, { label: string; className: string }> = {
  blocked: { label: "阻塞", className: "blocked" },
  pending: { label: "待处理", className: "pending" },
  running: { label: "处理中", className: "running" },
  completed: { label: "已完成", className: "completed" },
  failed: { label: "失败", className: "failed" },
  cancelled: { label: "已取消", className: "cancelled" },
};

const taskTypeMeta: Record<string, string> = {
  add_friend: "添加通讯录邀请",
  chat_reply: "AI 回复",
};

const resultCodeMeta: Record<string, string> = {
  invite_sent: "已发送添加通讯录邀请",
  already_friend: "已是好友",
  chat_reply_sent: "回复已发送",
  skipped_by_rule: "规则跳过",
  manual_closed: "人工关闭",
  send_unknown: "自动发送已终止，已转销售接管",
};

const reasonCodeMeta: Record<string, string> = {
  SALES_WORKER_NOT_BOUND: "销售未绑定 Worker",
  PHONE_NOT_FOUND: "手机号未找到客户",
  TASK_PAYLOAD_INVALID: "任务入参缺失或不合法",
  RPA_COMPONENT_NOT_READY: "RPA 组件未就绪",
  RPA_SIDECAR_TIMEOUT: "RPA 执行超时",
  RPA_SIDECAR_PROTOCOL_INVALID: "RPA 返回格式异常",
  RPA_SIDECAR_CRASHED: "RPA 进程异常退出",
  ACCOUNT_RESTRICTED: "微信账号受限",
  WECHAT_WINDOW_NOT_FOUND: "微信窗口未找到",
  OPERATION_TOO_FREQUENT: "微信操作过于频繁",
  WORKER_INTERRUPTED: "Worker 执行中断",
  SEND_RESULT_UNKNOWN: "发送结果未知",
  RPA_SEND_REPLY_FAILED: "回复发送失败",
  REPLY_ACTION_EXPIRED: "回复动作已过期",
  C2_REPLY_CONTEXT_MISSING: "缺少客户会话上下文",
  C2_REPLY_CONTEXT_RECOVERY_FAILED: "未能恢复客户会话上下文",
  C2_REPLY_TARGET_NOT_AUTHORIZED: "客户会话读取授权已失效",
  C3_CONTEXT_CHANGED_BEFORE_SEND: "发送前发现客户会话已变化",
  C3_SEND_CONTEXT_GUARD_MISSING: "发送前复核信息不完整",
  C3_SEND_CONTEXT_GUARD_INVALID: "发送前复核未通过",
  C3_SEND_PRE_CLICK_CONTEXT_UNAVAILABLE: "发送前无法确认当前客户会话",
  C3_REPLY_TASK_MISSING: "回复任务信息不完整",
  C3_BRAIN_NETWORK_NO_PROGRESS_WATCHDOG: "等待服务端回复时网络中断",
  C3_BRAIN_STATE_NO_PROGRESS_WATCHDOG: "服务端生成回复超时",
  C3_REPLACEMENT_BATCH_MISSING: "客户发来新消息，旧回复已终止",
  WECHAT_INPUT_DRAFT_PRESENT: "输入框存在未发送内容",
  SEND_INPUT_NOT_READY: "微信输入框未准备好",
  TASK_LEASE_RENEW_FAILED: "任务执行授权已失效",
  OTHER: "其他执行异常",
};

const stepMeta: Record<string, string> = {
  add_friend_starting: "正在执行微信加好友",
  checking_rpa: "检查自动化组件",
  rpa_sidecar_starting: "启动自动化组件",
  wechat_preflight_starting: "检查微信连接",
  operator_guard_starting: "检查当前操作环境",
  preflight_window_ready: "确认微信窗口可用",
  window_layout_calibration: "确认微信窗口布局",
  payload_validation: "校验任务资料",
  searching_contact: "搜索手机号",
  searching_phone: "搜索手机号",
  add_friend_menu_click: "打开添加朋友入口",
  add_contact_entry_clicked: "打开添加通讯录页面",
  invite_form: "填写添加通讯录邀请",
  invite_fields_review: "复核邀请信息",
  invite_confirming: "确认发送添加通讯录邀请",
  invite_confirm_clicked: "已提交添加通讯录邀请",
  opening_wechat: "打开微信",
  filling_request: "填写申请说明",
  sending_invite: "发送邀请",
  c2_reply_context_recovering: "恢复客户会话上下文",
  state_target_message_read: "读取客户最新消息",
  visible_hit_message_read: "读取客户最新消息",
  target_chat_locating: "定位客户会话",
  target_chat_reconfirming: "确认客户会话",
  pre_send_refresh: "执行发送前复核",
  c3_brain_waiting: "等待服务端生成回复",
  image_understanding_current_chat: "识别客户发送的图片",
  image_post_vision_final_read: "复核图片识别结果",
  voice_transcribe_current_chat: "识别客户发送的语音",
  task_completed: "已完成",
};

const eventTypeMeta: Record<string, string> = {
  created: "任务已创建",
  blocked: "任务阻塞",
  unblocked: "解除阻塞",
  resolved_block: "解除阻塞",
  comment_added: "补充备注",
  claimed: "任务已领取",
  step_updated: "步骤更新",
  completed: "任务已完成",
  failed: "任务失败",
  cancelled: "任务已取消",
  retried: "历史重试记录",
  commented: "补充备注",
};

const c3CodeMeta: Record<string, string> = {
  pending: "待处理",
  running: "处理中",
  completed: "已完成",
  failed: "失败",
  reply_action_created: "已生成回复动作",
  superseded: "已被新消息替换",
  skipped: "已跳过",
  queued: "待发送",
  claimed: "已领取发送",
  sending: "发送中",
  sent: "已发送",
  failed_to_send: "发送失败",
  unknown_send_result: "发送结果未知",
  handoff: "已转人工",
  open: "待处理",
  closed: "已关闭",
  none: "无需动作",
  online: "在线",
  offline: "离线",
  idle: "空闲",
  worker: "Worker 客户端",
  sales_replied_waiting_user: "销售已回复，等待客户消息",
  assigned: "已分配",
  unassigned: "未分配",
  invalid: "无效",
  busy: "忙碌",
};

function formatTaskType(type: TaskType) {
  return taskTypeMeta[type] ?? "-";
}

function formatStatus(status: TaskStatus) {
  return statusMeta[status] ?? { label: "-", className: "pending" };
}

export function formatTaskBusinessCode(code?: string | null) {
  if (!code) return "-";
  return resultCodeMeta[code] ?? reasonCodeMeta[code] ?? stepMeta[code] ?? eventTypeMeta[code] ?? c3CodeMeta[code] ?? "-";
}

const formatCode = formatTaskBusinessCode;

function formatMetric(value?: number | null) {
  return value === null || value === undefined ? "-" : String(value);
}

function formatDate(value?: string | null) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  if (sameDay) return `${hours}:${minutes}`;
  return `${month}-${day} ${hours}:${minutes}`;
}

function formatDateTime(value?: string | null) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `${year}-${month}-${day} ${hours}:${minutes}`;
}

function aggregateResult(task: Pick<TaskListItem, "status" | "result_code" | "error_code" | "block_code">) {
  if (task.status === "completed") return formatCode(task.result_code);
  if (task.status === "failed" && task.error_code === "SEND_RESULT_UNKNOWN") {
    return "自动发送已终止，禁止补发；会话已转销售正常接管";
  }
  if (task.status === "failed") return formatCode(task.error_code);
  if (task.status === "blocked") return formatCode(task.block_code);
  if (task.status === "cancelled") return "运营取消任务";
  return "-";
}

function taskCustomerName(task: TaskListItem | TaskDetail) {
  return task.business_object?.lead?.customer_name ?? null;
}

function taskCustomerPhone(task: TaskListItem | TaskDetail) {
  return task.business_object?.lead?.primary_phone_masked ?? task.primary_phone_masked ?? null;
}

function taskLeadStatus(task: TaskListItem | TaskDetail) {
  return task.business_object?.lead?.status ?? null;
}

function taskWorkerName(task: TaskListItem | TaskDetail) {
  return task.execution?.worker?.worker_name ?? null;
}

function taskWorkerStatus(task: TaskListItem | TaskDetail) {
  const worker = task.execution?.worker;
  if (!worker) return null;
  const online = worker.online_status ? formatCode(worker.online_status) : null;
  if (worker.online_status === "offline") return online;
  const running = worker.running_status ? formatCode(worker.running_status) : null;
  return [online, running].filter(Boolean).join(" / ") || null;
}

function taskLastHeartbeat(task: TaskListItem | TaskDetail) {
  return task.execution?.worker?.last_heartbeat_at ?? task.last_heartbeat_at ?? null;
}

function taskClaimedAt(task: TaskListItem | TaskDetail) {
  return task.execution?.claimed_at ?? task.claimed_at ?? null;
}

function isLeadQualityFailure(task: Pick<TaskListItem, "task_type" | "error_code">) {
  return task.task_type === "add_friend" && task.error_code === "PHONE_NOT_FOUND";
}

function isSendUnknownTask(task: Pick<TaskListItem, "task_type" | "error_code"> & { c3?: TaskDetail["c3"] }) {
  return (
    task.task_type === "chat_reply" &&
    (task.error_code === "SEND_RESULT_UNKNOWN" ||
      task.c3?.reply_action?.status === "unknown_send_result" ||
      task.c3?.sent_ack?.send_result === "unknown")
  );
}

export function buildActions(task: TaskDetail | TaskListItem) {
  const actions: string[] = [];
  if (task.status === "blocked") actions.push("处理阻塞", "取消任务", "补充备注");
  if (task.status === "pending") actions.push("取消任务", "补充备注");
  if (task.status === "running") actions.push("查看执行方", "取消任务", "补充备注");
  if (task.status === "completed") actions.push("查看执行结果", "查看执行方", "补充备注");
  if (task.status === "failed") actions.push("查看执行方", "补充备注");
  if (task.status === "cancelled") actions.push("查看取消信息", "补充备注");
  if (isLeadQualityFailure(task)) actions.push("标记线索无效");
  return actions;
}

function adviceForTask(task: TaskDetail | null) {
  if (!task) return null;
  if (task.status === "blocked" && task.block_code === "SALES_WORKER_NOT_BOUND") {
    return {
      title: "处理建议",
      text: "该任务当前不可领取。阻塞原因：销售未绑定 Worker。建议进入销售详情，为该销售绑定可用 Worker。",
    };
  }
  if (task.status === "completed" && task.result_code) {
    return {
      title: "结果说明",
      text:
        task.result_code === "invite_sent"
          ? "已发送添加通讯录邀请，不代表客户已同意好友申请。"
          : formatCode(task.result_code),
    };
  }
  if (task.status === "failed" && task.error_code) {
    if (isSendUnknownTask(task)) {
      return {
        title: "处理建议",
        text: "自动发送已终止，禁止补发；会话已转销售正常接管。",
      };
    }
    return {
      title: "处理建议",
      text: task.error_code === "C2_REPLY_CONTEXT_RECOVERY_FAILED"
        ? "未能确认目标客户会话，请核对销售绑定和客户识别信息后重新创建任务。"
        : `${formatCode(task.error_code)}。请核对客户资料和执行方状态后重新创建任务。`,
    };
  }
  if (task.status === "cancelled") {
    return { title: "取消说明", text: task.cancel_reason || task.remark || "该任务已被取消。" };
  }
  return null;
}

function eventLine(event: TaskEvent) {
  const actor = event.operator_name || "服务端";
  return `${formatDate(event.created_at)} · ${actor}`;
}

export function taskEventTitle(event: TaskEvent) {
  const step = formatCode(event.current_step);
  const reason = formatCode(event.error_code || event.block_code);
  if (event.event_type === "failed" && reason !== "-") {
    return step === "-" ? `执行失败：${reason}` : `${step}失败：${reason}`;
  }
  return step !== "-" ? step : formatCode(event.event_type);
}

export function TasksPage({ onOpenWorker, onOpenSalesWorkerBinding }: Props) {
  const [items, setItems] = useState<TaskListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [metrics, setMetrics] = useState<Partial<TaskMetrics> | null>(null);
  const [query, setQuery] = useState<TaskQuery>(initialQuery);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [salesOptions, setSalesOptions] = useState<SalesItem[]>([]);
  const [workerOptions, setWorkerOptions] = useState<WorkerItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState<string | null>(null);

  const pageCount = Math.max(1, Math.ceil(total / query.page_size));

  const refresh = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const [taskData, salesData, workerData] = await Promise.all([
        listTasks(query, signal),
        listSales(signal).catch(() => ({ items: [] })),
        listWorkers(signal).catch(() => ({ items: [] })),
      ]);
      setItems(taskData.items);
      setTotal(taskData.total);
      setSalesOptions(salesData.items);
      setWorkerOptions(workerData.items);
      setMetrics(taskData.metrics ?? null);
      setSelectedId((current) => {
        if (current && taskData.items.some((item) => item.id === current)) return current;
        return null;
      });
      return true;
    } catch (err) {
      if (!signal?.aborted) {
        setItems([]);
        setTotal(0);
        setSelectedId(null);
        setDetail(null);
        setError(formatBusinessError(err, "任务列表加载失败，请稍后重试。"));
      }
      return false;
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [query]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      setEvents([]);
      return;
    }

    const controller = new AbortController();
    setDetailLoading(true);
    setSaveError(null);
    void Promise.all([
      getTask(selectedId, controller.signal),
      listTaskEvents(selectedId, controller.signal),
    ])
      .then(([task, eventData]) => {
        setDetail(task);
        setEvents(eventData.items.length ? eventData.items : task.events ?? []);
      })
      .catch((err) => {
        if (!controller.signal.aborted) setSaveError(formatBusinessError(err, "任务详情加载失败，请稍后重试。"));
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false);
      });

    return () => controller.abort();
  }, [selectedId]);

  function updateQuery(patch: Partial<TaskQuery>) {
    setQuery((current) => ({ ...current, ...patch, page: patch.page ?? 1 }));
  }

  function selectRow(task: TaskListItem) {
    setSelectedId(task.id);
    setDrawerOpen(true);
  }

  async function runTaskAction(label: string) {
    if (!detail) return;
    setActionBusy(label);
    setSaveError(null);
    setMessage(null);
    let updatedAfterMutation: TaskDetail | null = null;
    let successMessage: string | null = null;
    try {
      if (label === "查看执行方") {
        const workerId = detail.execution?.worker?.id ?? null;
        if (!workerId) {
          setMessage("当前任务暂无可跳转的 Worker 执行方。");
          return;
        }
        onOpenWorker(workerId);
        return;
      }
      if (label === "处理阻塞") {
        if (detail.block_code === "SALES_WORKER_NOT_BOUND" && detail.sales_id) {
          onOpenSalesWorkerBinding(detail.sales_id);
          return;
        }
        setMessage("当前阻塞原因暂不支持前端跳转处理。");
        return;
      }
      if (label === "取消任务") {
        const remark = window.prompt("请输入取消原因，可留空。");
        if (remark === null) return;
        updatedAfterMutation = await cancelTask(detail.id, { reason: remark.trim() || undefined });
        successMessage = `${detail.id} 已取消。`;
      }
      if (label === "补充备注") {
        const remark = window.prompt("请输入任务备注");
        if (!remark?.trim()) return;
        updatedAfterMutation = await addTaskComment(detail.id, { content: remark.trim() });
        successMessage = "任务备注已补充。";
      }
      if (label === "查看执行结果") {
        setMessage(`执行结果：${formatCode(detail.result_code)}。`);
        return;
      }
      if (label === "查看取消信息") {
        setMessage(`取消信息：${display(detail.cancel_reason || detail.remark, "暂无取消备注")}。`);
        return;
      }
      if (label === "标记线索无效") {
        setMessage("请进入线索管理，对关联线索执行标记无效。");
        return;
      }
      if (!updatedAfterMutation || !successMessage) return;

      setDetail(updatedAfterMutation);
      setEvents(updatedAfterMutation.events ?? events);
      const [listRefreshed, detailRefreshed] = await Promise.all([
        runPostMutationRefresh(() => refresh()),
        runPostMutationRefresh(async () => {
          const [nextDetail, eventData] = await Promise.all([getTask(detail.id), listTaskEvents(detail.id)]);
          setDetail(nextDetail);
          setEvents(eventData.items.length ? eventData.items : nextDetail.events ?? []);
        }),
      ]);
      setMessage(postMutationMessage(successMessage, listRefreshed && detailRefreshed));
    } catch (err) {
      setSaveError(formatBusinessError(err, `${label}失败，请稍后重试。`));
    } finally {
      setActionBusy(null);
    }
  }

  const detailAdvice = adviceForTask(detail);
  const detailActions = detail ? buildActions(detail) : [];

  return (
    <div className="tasks-page screen-tasks">
      <header className="page-header">
        <div>
          <p className="eyebrow">任务运营</p>
          <h1>任务中心</h1>
        </div>
      </header>

      <section className="metric-grid task-metrics" aria-label="任务中心指标">
        <article>
          <span>阻塞任务</span>
          <strong className="warning-text">{formatMetric(metrics?.blocked)}</strong>
          <p>需运营处理</p>
        </article>
        <article>
          <span>待处理任务</span>
          <strong>{formatMetric(metrics?.pending)}</strong>
          <p>等待执行方领取</p>
        </article>
        <article>
          <span>处理中任务</span>
          <strong>{formatMetric(metrics?.running)}</strong>
          <p>执行方正在处理</p>
        </article>
        <article>
          <span>今日已完成</span>
          <strong>{formatMetric(metrics?.completed_today)}</strong>
          <p>邀请 / 回复 / 召回</p>
        </article>
        <article>
          <span>今日失败</span>
          <strong className="warning-text">{formatMetric(metrics?.failed_today)}</strong>
          <p>需复盘处理</p>
        </article>
      </section>

      {message ? <div className="inline-alert success">{message}</div> : null}
      {error ? <div className="inline-alert error">{error}</div> : null}

      <div className="management-grid task-management-grid">
        <section className="panel management-list-panel task-list-panel">
          <div className="panel-header">
            <div>
              <h2>任务列表</h2>
              <p>任务状态由服务端维护，Worker / RPA / 人工只上报执行事实。</p>
            </div>
          </div>

          <div className="task-filter-card">
            <label className="task-search">
              <span>搜索</span>
              <input
                type="search"
                value={query.keyword}
                onChange={(event) => updateQuery({ keyword: event.target.value })}
                placeholder="搜索任务ID、客户姓名、手机号后四位"
                aria-label="搜索任务ID、客户姓名、手机号后四位"
              />
            </label>
            <label>
              <span>任务类型</span>
              <select value={query.task_type} onChange={(event) => updateQuery({ task_type: event.target.value })} aria-label="筛选任务类型">
                <option value="all">全部类型</option>
                <option value="add_friend">添加通讯录邀请</option>
                <option value="chat_reply">AI 回复</option>
              </select>
            </label>
            <label>
              <span>执行状态</span>
              <select value={query.status} onChange={(event) => updateQuery({ status: event.target.value as TaskQuery["status"] })} aria-label="筛选执行状态">
                <option value="all">全部状态</option>
                {Object.entries(statusMeta).map(([value, meta]) => (
                  <option key={value} value={value}>{meta.label}</option>
                ))}
              </select>
            </label>
            <label>
              <span>业务结果</span>
              <select value={query.result_code} onChange={(event) => updateQuery({ result_code: event.target.value })} aria-label="筛选业务结果">
                <option value="all">全部结果</option>
                <option value="invite_sent">已发送添加通讯录邀请</option>
                <option value="already_friend">已是好友</option>
                <option value="chat_reply_sent">回复已发送</option>
                <option value="send_unknown">自动发送已终止，已转销售接管</option>
              </select>
            </label>
            <label>
              <span>异常原因</span>
              <select value={query.exception_code} onChange={(event) => updateQuery({ exception_code: event.target.value })} aria-label="筛选异常原因">
                <option value="all">全部原因</option>
                <option value="SALES_WORKER_NOT_BOUND">销售未绑定 Worker</option>
                <option value="PHONE_NOT_FOUND">手机号未找到客户</option>
                <option value="WECHAT_WINDOW_NOT_FOUND">微信窗口未找到</option>
                <option value="WORKER_INTERRUPTED">Worker 执行中断</option>
                <option value="C2_REPLY_CONTEXT_RECOVERY_FAILED">未能恢复客户会话上下文</option>
                <option value="SEND_RESULT_UNKNOWN">发送结果未知</option>
              </select>
            </label>
            <label>
              <span>销售</span>
              <select value={query.sales_id} onChange={(event) => updateQuery({ sales_id: event.target.value })} aria-label="筛选任务销售">
                <option value="all">全部销售</option>
                {salesOptions.map((sales) => (
                  <option key={sales.id} value={sales.id}>{sales.sales_name}</option>
                ))}
              </select>
            </label>
            <label>
              <span>Worker</span>
              <select value={query.worker_id} onChange={(event) => updateQuery({ worker_id: event.target.value })} aria-label="筛选任务 Worker">
                <option value="all">全部 Worker</option>
                {workerOptions.map((worker) => (
                  <option key={worker.id} value={worker.id}>{worker.worker_name}</option>
                ))}
              </select>
            </label>
          </div>

          <div className="management-table-card task-table-card">
            <table className="task-table">
              <thead>
                <tr>
                  <th>任务ID</th>
                  <th>任务类型</th>
                  <th>客户</th>
                  <th>销售</th>
                  <th>执行方</th>
                  <th>执行状态</th>
                  <th>业务结果 / 异常原因</th>
                  <th>当前步骤</th>
                  <th>更新时间</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr><td colSpan={9}>正在加载任务列表...</td></tr>
                ) : items.length === 0 ? (
                  <tr><td colSpan={9}>暂无任务，调整筛选条件或等待后端任务数据写入后重试。</td></tr>
                ) : (
                  items.map((item) => {
                    const status = formatStatus(item.status);
                    return (
                      <tr
                        key={item.id}
                        className={selectedId === item.id && drawerOpen ? "selected" : undefined}
                        tabIndex={0}
                        onClick={() => selectRow(item)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") selectRow(item);
                        }}
                      >
                        <td><ClickToCopyText label="任务 ID" value={item.id} /></td>
                        <td>{formatTaskType(item.task_type)}</td>
                        <td className="lead-cell"><strong>{display(taskCustomerName(item))}</strong><small>{display(taskCustomerPhone(item))}</small></td>
                        <td>{display(item.sales_name)}</td>
                        <td>{display(taskWorkerName(item), item.worker_id ? "Worker" : "未绑定")}</td>
                        <td className="status-cell"><span className={`status ${status.className}`}>{status.label}</span></td>
                        <td><ClickToCopyText label="业务结果或异常原因" value={aggregateResult(item)} /></td>
                        <td>{formatCode(item.current_step)}</td>
                        <td>{formatDate(item.updated_at)}</td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>

          <Pagination
            as="div"
            ariaLabel="任务分页"
            className="task-pagination"
            currentPage={query.page}
            disabled={loading}
            pageSize={query.page_size}
            pageSizeAriaLabel="每页条数"
            pageSizeOptions={taskPageSizeOptions}
            splitTotal
            total={total}
            totalPages={pageCount}
            totalUnit="条"
            onPageChange={(value) => updateQuery({ page: value })}
            onPageSizeChange={(value) => updateQuery({ page_size: value, page: 1 })}
          />
        </section>

        <aside className={`panel management-drawer task-detail-drawer ${drawerOpen ? "" : "closed"}`}>
          {!drawerOpen ? (
            <div className="state-box">点击任务行查看详情。</div>
          ) : detailLoading || !detail ? (
            <div className="state-box">正在加载任务详情...</div>
          ) : (
            <>
              <div className="drawer-head">
                <div>
                  <p>任务详情</p>
                  <h2><ClickToCopyText label="任务 ID" value={detail.id} /></h2>
                </div>
                <button className="icon-button drawer-close-button" type="button" onClick={() => setDrawerOpen(false)} aria-label="关闭任务详情"><CloseIcon /></button>
              </div>

              <div className="task-status-row">
                <span className={`status ${formatStatus(detail.status).className}`}>{formatStatus(detail.status).label}</span>
              </div>

              {saveError ? <div className="inline-alert error">{saveError}</div> : null}

              <section className="drawer-section">
                <h3>基础信息</h3>
                <dl className="drawer-dl">
                  <div><dt>任务类型</dt><dd>{formatTaskType(detail.task_type)}</dd></div>
                  <div><dt>执行状态</dt><dd>{formatStatus(detail.status).label}</dd></div>
                  <div><dt>业务结果</dt><dd><ClickToCopyText label="业务结果" value={formatCode(detail.result_code)} /></dd></div>
                  <div><dt>异常原因</dt><dd><ClickToCopyText label="异常原因" value={formatCode(detail.error_code || detail.block_code)} /></dd></div>
                  <div><dt>当前步骤</dt><dd>{formatCode(detail.current_step)}</dd></div>
                  <div><dt>创建时间</dt><dd>{formatDateTime(detail.created_at)}</dd></div>
                  <div><dt>更新时间</dt><dd>{formatDateTime(detail.updated_at)}</dd></div>
                </dl>
              </section>

              <section className="drawer-section">
                <h3>业务对象</h3>
                <dl className="drawer-dl">
                  <div><dt>客户</dt><dd>{display(taskCustomerName(detail))}</dd></div>
                  <div><dt>手机号</dt><dd>{display(taskCustomerPhone(detail))}</dd></div>
                  <div><dt>线索状态</dt><dd>{formatCode(taskLeadStatus(detail))}</dd></div>
                  <div><dt>当前销售</dt><dd>{display(detail.sales_name)}</dd></div>
                </dl>
              </section>

              <section className="drawer-section">
                <h3>执行信息</h3>
                <dl className="drawer-dl">
                  <div><dt>执行方类型</dt><dd>{formatCode(detail.executor_type || "worker")}</dd></div>
                  <div><dt>执行方</dt><dd>{display(taskWorkerName(detail), detail.worker_id || detail.execution?.worker?.id ? "Worker" : "未绑定")}</dd></div>
                  <div><dt>执行方状态</dt><dd>{display(taskWorkerStatus(detail))}</dd></div>
                  <div><dt>最近心跳</dt><dd>{formatDate(taskLastHeartbeat(detail))}</dd></div>
                  <div><dt>领取时间</dt><dd>{formatDateTime(taskClaimedAt(detail))}</dd></div>
                </dl>
              </section>

              {detailAdvice ? (
                <section className="drawer-section">
                  <h3>{detailAdvice.title}</h3>
                  <p className="drawer-hint">{detailAdvice.text}</p>
                </section>
              ) : null}

              <section className="drawer-section">
                <h3>任务链路</h3>
                <ol className="flow-list task-flow-list">
                  {events.map((event) => (
                    <li key={event.id}>
                      <strong>{taskEventTitle(event)}</strong>
                      <span>{eventLine(event)}</span>
                    </li>
                  ))}
                </ol>
                {events.length === 0 ? <p className="task-note">暂无任务链路</p> : null}
              </section>

              <section className="drawer-section">
                <h3>备注</h3>
                {detail.notes?.length ? (
                  <ol className="flow-list task-flow-list">
                    {detail.notes.map((note) => (
                      <li key={note.id}>
                        <strong>{display(note.operator_name, "运营")}</strong>
                        <span>{formatDate(note.created_at)} · {note.content}</span>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p className="task-note">{display(detail.result_remark || detail.remark || detail.failure_remark, "暂无备注")}</p>
                )}
              </section>

              <section className="drawer-action-section">
                <h3>操作</h3>
                <div className="drawer-actions">
                  {detailActions.map((label) => (
                    <button key={label} type="button" disabled={Boolean(actionBusy)} onClick={() => void runTaskAction(label)}>
                      {actionBusy === label ? "处理中..." : label}
                    </button>
                  ))}
                </div>
              </section>
            </>
          )}
        </aside>
      </div>
    </div>
  );
}
