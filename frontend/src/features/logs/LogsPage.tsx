import { useCallback, useEffect, useRef, useState } from "react";

import { formatBusinessError } from "../../shared/api/client";
import { CloseIcon } from "../../shared/ui/Icons";
import { listOperationLogs } from "./api";
import type { OperationLogItem, OperationLogQuery, OperationLogResult } from "./types";

const eventOptions = [
  { value: "", label: "全部类型" },
  { value: "lead_created", label: "新增客户" },
  { value: "lead_updated", label: "编辑客户" },
  { value: "lead_marked_invalid", label: "标记无效" },
  { value: "lead_restored", label: "恢复线索" },
  { value: "duplicate_detected", label: "重复手机号录入" },
  { value: "duplicate_note_appended", label: "追加备注" },
  { value: "lead_auto_assigned", label: "轮询分配" },
  { value: "lead_assign_failed", label: "轮询分配失败" },
  { value: "lead_retry_assign", label: "重新分配线索" },
  { value: "sales_created", label: "新增销售" },
  { value: "sales_updated", label: "编辑销售" },
  { value: "sales_enabled_changed", label: "启用/停用销售" },
  { value: "sales_worker_bound", label: "更换 Worker" },
  { value: "sales_worker_unbound", label: "清空销售 Worker" },
  { value: "worker_created", label: "新增 Worker" },
  { value: "worker_updated", label: "编辑 Worker" },
  { value: "worker_enabled_changed", label: "启用/停用 Worker" },
  { value: "worker_binding_reset", label: "重置绑定" },
  { value: "task_created", label: "创建任务" },
  { value: "task_unblocked", label: "解除阻塞" },
  { value: "task_cancelled", label: "取消任务" },
  { value: "task_retried", label: "重新处理任务" },
  { value: "task_comment_added", label: "补充备注" },
  { value: "phone_revealed", label: "查看完整手机号" },
  { value: "leads_exported", label: "导出选中线索" },
  { value: "vehicle_created", label: "新增车辆" },
  { value: "vehicle_updated", label: "编辑车辆" },
  { value: "vehicle_listed", label: "上架车辆" },
  { value: "vehicle_unlisted", label: "下架车辆" },
  { value: "vehicle_image_uploaded", label: "上传车辆图片" },
  { value: "vehicle_image_reordered", label: "调整车辆图片顺序" },
  { value: "vehicle_image_deleted", label: "删除车辆图片" },
  { value: "vehicle_excel_import_confirmed", label: "确认导入车辆" },
  { value: "vehicle_operation_failed", label: "车辆操作失败" },
  { value: "admin_account_created", label: "创建后台账号" },
  { value: "admin_account_enabled", label: "启用后台账号" },
  { value: "admin_account_disabled", label: "停用后台账号" },
  { value: "admin_password_reset", label: "重置后台账号密码" },
  { value: "admin_login_succeeded", label: "后台登录成功" },
  { value: "admin_login_failed", label: "后台登录失败" },
  { value: "admin_logout", label: "退出后台" },
];

export const operationModuleOptions = [
  { value: "", label: "全部模块" },
  { value: "lead", label: "线索管理" },
  { value: "assignment", label: "线索分配" },
  { value: "sales", label: "销售管理" },
  { value: "worker", label: "Worker 管理" },
  { value: "task", label: "任务中心" },
  { value: "vehicles", label: "车辆管理" },
  { value: "export", label: "线索导出" },
  { value: "auth", label: "账号与登录" },
];

const moduleOptions = operationModuleOptions;

const resultOptions: Array<{ value: OperationLogResult | ""; label: string }> = [
  { value: "", label: "全部结果" },
  { value: "success", label: "成功" },
  { value: "failed", label: "失败" },
];

const objectLabels: Record<string, string> = {
  lead: "线索",
  assignment: "分配",
  sales: "销售",
  worker: "Worker",
  task: "任务",
  vehicles: "车辆",
  export: "导出",
  auth: "后台账号",
};

const businessCodeLabels: Record<string, string> = {
  assigned: "已分配",
  unassigned: "未分配",
  invalid: "无效",
  pending: "待处理",
  running: "处理中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  listed: "已上架",
  unlisted: "已下架",
  add_friend: "添加通讯录邀请",
  chat_reply: "AI 回复",
  bound: "已绑定",
  unbound: "未绑定",
  reset_required: "需要重新绑定",
  paused: "暂停接单",
  online: "在线",
  offline: "离线",
  idle: "空闲",
  busy: "忙碌",
  SALES_WORKER_NOT_BOUND: "销售未绑定 Worker",
  DAILY_LIMIT_REACHED: "已达到每日处理上限",
  empty_number: "空号",
  wrong_info: "信息错误",
  not_target_customer: "非目标客户",
  test_data: "测试数据",
  duplicate_or_mistaken: "重复或误录",
  other: "其他",
  "image/jpeg": "JPEG",
  "image/png": "PNG",
  "image/webp": "WebP",
};

const fieldLabels: Record<string, string> = {
  customer_name: "客户名称",
  sales_name: "销售姓名",
  worker_name: "Worker 名称",
  vehicle_code: "车辆编号",
  status: "状态",
  listing_status: "上下架状态",
  enabled: "启用状态",
  sort_order: "轮询排序",
  invalid_reason: "无效原因",
  task_type: "任务类型",
  block_code: "阻塞原因",
  device_name: "设备名称",
  platform: "运行平台",
  client_binding_state: "绑定状态",
  run_status: "接单状态",
  online_status: "在线状态",
  running_status: "运行状态",
  remark: "备注",
  worker_id: "Worker",
  content_type: "图片格式",
  size_bytes: "图片大小",
  image_ids: "图片顺序",
};

const authReasonLabels: Record<string, string> = {
  unknown_account: "账号或密码错误",
  account_disabled: "账号已停用",
  password_mismatch: "账号或密码错误",
  rate_limited: "登录尝试过于频繁",
};

function formatDate(value: string | null | undefined) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString("zh-CN", { hour12: false });
}

function formatTime(value: string | null | undefined) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
}

export function operationEventLabel(item: OperationLogItem) {
  return eventOptions.find((option) => option.value === item.event_type)?.label || "-";
}

const eventLabel = operationEventLabel;

function objectLabel(item: OperationLogItem) {
  const byModule = objectLabels[item.module];
  if (byModule && item.module) {
    return byModule;
  }
  if (item.target_type === "contact") {
    return "手机号查看";
  }
  if (item.target_type === "export_task") {
    return "导出任务";
  }
  if (item.target_type === "vehicle") return "车辆";
  if (item.target_type === "vehicle_import") return "车辆导入";
  if (item.target_type === "admin_account") return "后台账号";
  return "-";
}

function stringFromMetadata(metadata: Record<string, unknown> | null | undefined, key: string) {
  const value = metadata?.[key];
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

function isUuidLike(value: string) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

export function operationObjectName(item: OperationLogItem) {
  const businessName = (
    item.lead_customer_name ||
    stringFromMetadata(item.metadata, "username_normalized") ||
    stringFromMetadata(item.metadata, "sales_name") ||
    stringFromMetadata(item.metadata, "worker_name") ||
    stringFromMetadata(item.metadata, "vehicle_code") ||
    stringFromMetadata(item.after_data, "sales_name") ||
    stringFromMetadata(item.after_data, "worker_name") ||
    stringFromMetadata(item.after_data, "vehicle_code") ||
    stringFromMetadata(item.after_data, "customer_name") ||
    stringFromMetadata(item.before_data, "sales_name") ||
    stringFromMetadata(item.before_data, "worker_name") ||
    stringFromMetadata(item.before_data, "vehicle_code") ||
    stringFromMetadata(item.before_data, "customer_name") ||
    ""
  );
  if (businessName) return businessName;
  return item.target_id && !isUuidLike(item.target_id) ? item.target_id : "-";
}

const objectName = operationObjectName;

function resultLabel(item: OperationLogItem) {
  const result = item.result || (item.event_type.includes("failed") ? "failed" : "success");
  return result === "failed" ? "失败" : "成功";
}

export function operationSummary(item: OperationLogItem) {
  if (item.event_type === "sales_worker_bound") {
    const workerName = stringFromMetadata(item.after_data, "worker_name");
    return workerName ? `绑定至 ${workerName}` : "已绑定 Worker";
  }
  if (item.event_type === "sales_worker_unbound") return "已清空销售的 Worker 绑定";
  if (item.event_type === "task_unblocked") return "销售已绑定可用 Worker";
  if (item.event_type === "worker_binding_reset") return "新 Token 已生成，旧客户端失效";
  if (item.event_type === "vehicle_operation_failed") return "车辆操作未完成";
  if (item.module === "auth") {
    const reason = stringFromMetadata(item.metadata, "reason");
    return authReasonLabels[reason] || eventLabel(item);
  }
  const summary = item.summary?.trim() || "";
  if (summary && !isUuidLike(summary) && !/[0-9a-f]{8}-[0-9a-f-]{27,}/i.test(summary) && !/[A-Z][A-Z0-9]+_[A-Z0-9_]+/.test(summary)) {
    return summary;
  }
  return eventLabel(item);
}

const summaryText = operationSummary;

function operationDescription(item: OperationLogItem) {
  const summary = item.summary?.trim() || "";
  if (summary && !isUuidLike(summary) && !/[0-9a-f]{8}-[0-9a-f-]{27,}/i.test(summary) && !/[A-Z][A-Z0-9]+_[A-Z0-9_]+/.test(summary)) {
    return summary;
  }
  return operationSummary(item);
}

function businessValue(key: string, value: unknown) {
  if (key === "worker_id") return value ? "已绑定 Worker" : "未绑定 Worker";
  if (key === "enabled" && typeof value === "boolean") return value ? "启用" : "停用";
  if (key === "image_ids" && Array.isArray(value)) return `已调整 ${value.length} 张图片顺序`;
  if (key === "size_bytes" && typeof value === "number") return `${(value / 1024 / 1024).toFixed(2)} MB`;
  if (value === null || value === undefined || value === "") return "未设置";
  if (typeof value === "string") {
    if (isUuidLike(value)) return "";
    if (/^[A-Z][A-Z0-9]+_[A-Z0-9_]+$/.test(value)) return businessCodeLabels[value] || "-";
    return businessCodeLabels[value] || value;
  }
  if (typeof value === "number") return String(value);
  return "";
}

export function formatOperationSnapshot(value: Record<string, unknown> | null | undefined) {
  if (!value) return "-";
  if (Object.hasOwn(value, "worker_id")) {
    const workerName = stringFromMetadata(value, "worker_name");
    return value.worker_id ? (workerName ? `绑定 ${workerName}` : "已绑定 Worker") : "未绑定 Worker";
  }
  const entries = Object.entries(value)
    .filter(([key]) => key in fieldLabels)
    .map(([key, fieldValue]) => {
      const formatted = businessValue(key, fieldValue);
      return formatted ? `${fieldLabels[key]}：${formatted}` : "";
    })
    .filter(Boolean);
  return entries.length ? entries.join("；") : "-";
}

export function LogsPage() {
  const [query, setQuery] = useState<Required<Pick<OperationLogQuery, "page" | "page_size">> & OperationLogQuery>({
    keyword: "",
    event_type: "",
    module: "",
    operator_name: "",
    target_type: "",
    result: "",
    created_from: "",
    created_to: "",
    page: 1,
    page_size: 20,
  });
  const [items, setItems] = useState<OperationLogItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeLog, setActiveLog] = useState<OperationLogItem | null>(null);
  const requestIdRef = useRef(0);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const requestId = ++requestIdRef.current;
    setLoading(true);
    setError(null);
    try {
      const data = await listOperationLogs(query, signal);
      if (signal?.aborted || requestId !== requestIdRef.current) {
        return;
      }
      setItems(data.items);
    } catch (err) {
      if (signal?.aborted || requestId !== requestIdRef.current) {
        return;
      }
      setError(formatBusinessError(err, "操作日志加载失败，请稍后重试。"));
    } finally {
      if (!signal?.aborted && requestId === requestIdRef.current) {
        setLoading(false);
      }
    }
  }, [query]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  function updateQuery(patch: Partial<OperationLogQuery>) {
    setQuery((current) => ({ ...current, ...patch, page: patch.page ?? 1 }));
  }

  return (
    <div className="logs-page">
      <header className="page-header">
        <div>
          <p className="eyebrow">审计追溯</p>
          <h1>操作日志</h1>
        </div>
      </header>

      <div className="management-grid log-management-grid">
        <section className="panel log-panel" aria-label="操作日志列表">
          <div className="panel-header"><div><h2>操作记录</h2></div></div>
          <section className="log-filter-card" aria-label="操作日志筛选">
            <label>
              <span>搜索</span>
              <input value={query.keyword} onChange={(event) => updateQuery({ keyword: event.target.value })} placeholder="搜索操作人、对象名称" />
            </label>
            <label>
              <span>模块</span>
              <select value={query.module} onChange={(event) => updateQuery({ module: event.target.value })}>
                {moduleOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
            </label>
            <label>
              <span>结果</span>
              <select value={query.result} onChange={(event) => updateQuery({ result: event.target.value as OperationLogResult | "" })}>
                {resultOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
            </label>
          </section>

          <div className="management-table-card log-table-card">
          {loading ? (
            <div className="state-box">正在加载操作日志...</div>
          ) : error ? (
            <div className="state-box error">
              <span>{error}</span>
              <button type="button" onClick={() => void refresh()}>
                重试
              </button>
            </div>
          ) : items.length === 0 ? (
            <div className="logs-empty state-box">
              <strong>暂无操作日志</strong>
              <span>新增客户、标记无效、恢复线索、查看完整手机号等操作会记录在这里。</span>
            </div>
          ) : (
            <table className="log-management-table">
              <thead>
                <tr>
                  <th>操作时间</th>
                  <th>操作人</th>
                  <th>操作类型</th>
                  <th>操作对象</th>
                  <th>对象名称</th>
                  <th>结果</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr
                    key={item.id}
                    tabIndex={0}
                    onClick={() => setActiveLog(item)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        setActiveLog(item);
                      }
                    }}
                  >
                    <td title={formatDate(item.created_at)}>{formatTime(item.created_at)}</td>
                    <td>{item.operator_name || "系统"}</td>
                    <td>{eventLabel(item)}</td>
                    <td>{objectLabel(item)}</td>
                    <td>{objectName(item)}</td>
                    <td>
                      <span className={`result-badge ${item.result === "failed" || item.event_type.includes("failed") ? "failed" : "success"}`}>
                        {resultLabel(item)}
                      </span>
                    </td>
                    <td>{summaryText(item)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          </div>
        </section>

        <aside className={`panel management-drawer log-detail-drawer${activeLog ? "" : " closed"}`} aria-label="操作日志详情">
          {activeLog ? <>
            <div className="drawer-head">
              <div>
                <p>日志详情</p>
                <h2>{eventLabel(activeLog)}</h2>
              </div>
              <button type="button" className="icon-button drawer-close-button" aria-label="关闭日志详情" onClick={() => setActiveLog(null)}><CloseIcon /></button>
            </div>
            <section className="drawer-section">
              <h3>操作信息</h3>
              <dl className="drawer-dl">
                <div><dt>操作人</dt><dd>{activeLog.operator_name || "系统"}</dd></div>
                <div><dt>操作时间</dt><dd>{formatDate(activeLog.created_at)}</dd></div>
                <div><dt>操作对象</dt><dd title={`${objectLabel(activeLog)} · ${objectName(activeLog)}`}>{objectLabel(activeLog)} · {objectName(activeLog)}</dd></div>
                <div><dt>操作结果</dt><dd><span className={`status ${activeLog.result === "failed" ? "failed" : "completed"}`}>{resultLabel(activeLog)}</span></dd></div>
              </dl>
            </section>
            <section className="drawer-section">
              <h3>变更内容</h3>
              <dl className="drawer-dl log-change-dl">
                <div><dt>变更前</dt><dd title={formatOperationSnapshot(activeLog.before_data)}>{formatOperationSnapshot(activeLog.before_data)}</dd></div>
                <div><dt>变更后</dt><dd title={formatOperationSnapshot(activeLog.after_data)}>{formatOperationSnapshot(activeLog.after_data)}</dd></div>
                <div><dt>说明</dt><dd title={operationDescription(activeLog)}>{operationDescription(activeLog)}</dd></div>
              </dl>
            </section>
          </> : null}
        </aside>
      </div>
    </div>
  );
}
