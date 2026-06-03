import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { formatApiError } from "../../shared/api/client";
import { listOperationLogs } from "./api";
import type { OperationLogItem, OperationLogQuery, OperationLogResult } from "./types";

const pageSizeOptions = [20, 50, 100];

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
  { value: "phone_revealed", label: "查看完整手机号" },
  { value: "leads_exported", label: "导出选中线索" },
];

const moduleOptions = [
  { value: "", label: "全部对象" },
  { value: "lead", label: "客户线索" },
  { value: "assignment", label: "分配" },
  { value: "sales", label: "销售" },
  { value: "export", label: "导出" },
];

const resultOptions: Array<{ value: OperationLogResult | ""; label: string }> = [
  { value: "", label: "全部结果" },
  { value: "success", label: "成功" },
  { value: "failed", label: "失败" },
];

function getPaginationItems(currentPage: number, totalPages: number): Array<number | "..."> {
  if (totalPages <= 5) {
    return Array.from({ length: totalPages }, (_, index) => index + 1);
  }
  if (currentPage <= 3) {
    return [1, 2, 3, "...", totalPages];
  }
  if (currentPage >= totalPages - 2) {
    return [1, "...", totalPages - 2, totalPages - 1, totalPages];
  }
  return [1, "...", currentPage - 1, currentPage, currentPage + 1, "...", totalPages];
}

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

function eventLabel(item: OperationLogItem) {
  return eventOptions.find((option) => option.value === item.event_type)?.label || item.event_label || item.event_name || item.event_type;
}

function objectLabel(item: OperationLogItem) {
  const byModule = moduleOptions.find((option) => option.value === item.module)?.label;
  if (byModule && item.module) {
    return byModule;
  }
  if (item.target_type === "contact") {
    return "手机号查看";
  }
  if (item.target_type === "export_task") {
    return "导出任务";
  }
  return item.target_type || "-";
}

function stringFromMetadata(metadata: Record<string, unknown> | null | undefined, key: string) {
  const value = metadata?.[key];
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

function objectName(item: OperationLogItem) {
  return (
    item.lead_customer_name ||
    stringFromMetadata(item.metadata, "sales_name") ||
    stringFromMetadata(item.after_data, "sales_name") ||
    stringFromMetadata(item.after_data, "customer_name") ||
    stringFromMetadata(item.before_data, "sales_name") ||
    stringFromMetadata(item.before_data, "customer_name") ||
    item.target_id ||
    "-"
  );
}

function phoneSuffix(item: OperationLogItem) {
  const suffix = stringFromMetadata(item.metadata, "phone_suffix");
  if (suffix) {
    return suffix;
  }
  const masked = stringFromMetadata(item.metadata, "submitted_phone_masked");
  const match = masked.match(/\d{4}(?!.*\d)/);
  return match?.[0] || "-";
}

function resultLabel(item: OperationLogItem) {
  const result = item.result || (item.event_type.includes("failed") ? "failed" : "success");
  return result === "failed" ? "失败" : "成功";
}

function summaryText(item: OperationLogItem) {
  return item.summary || stringFromMetadata(item.metadata, "reason") || eventLabel(item);
}

function compactJson(value: Record<string, unknown> | null | undefined) {
  if (!value || Object.keys(value).length === 0) {
    return "无";
  }
  return JSON.stringify(value, null, 2);
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
  const [total, setTotal] = useState(0);
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
      setTotal(data.total);
    } catch (err) {
      if (signal?.aborted || requestId !== requestIdRef.current) {
        return;
      }
      setError(formatApiError(err, "操作日志加载失败，请稍后重试。"));
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

  const totalPages = Math.max(1, Math.ceil(total / query.page_size));
  const paginationItems = useMemo(() => getPaginationItems(query.page, totalPages), [query.page, totalPages]);

  return (
    <div className="logs-page">
      <header className="page-header">
        <div>
          <p className="eyebrow">日志审计</p>
          <h1>操作日志</h1>
        </div>
      </header>

      <section className="logs-panel" aria-label="操作日志列表">
        <div className="logs-panel-head">
          <div>
            <h2>操作日志</h2>
            <p>记录客户、销售、分配、手机号查看和导出等关键操作。</p>
          </div>
          <button type="button" className="text-button icon-text-button" disabled={loading} onClick={() => void refresh()} aria-label="刷新操作日志">
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="M13.2 6.5a5.3 5.3 0 0 0-9.7-2.1L2.2 6M2.2 3v3h3M2.8 9.5a5.3 5.3 0 0 0 9.7 2.1l1.3-1.6M13.8 13v-3h-3" />
            </svg>
            <span>刷新</span>
          </button>
        </div>

        <section className="logs-filter-card" aria-label="操作日志筛选">
          <label className="search-field">
            <span>关键词</span>
            <input
              value={query.keyword}
              onChange={(event) => updateQuery({ keyword: event.target.value })}
              placeholder="客户、销售、手机号后四位、说明"
            />
          </label>
          <label className="operator-field">
            <span>操作人</span>
            <input value={query.operator_name} onChange={(event) => updateQuery({ operator_name: event.target.value })} placeholder="姓名" />
          </label>
          <label className="select-field">
            <span>操作类型</span>
            <select value={query.event_type} onChange={(event) => updateQuery({ event_type: event.target.value })}>
              {eventOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="select-field">
            <span>操作对象</span>
            <select value={query.module} onChange={(event) => updateQuery({ module: event.target.value })}>
              {moduleOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="select-field">
            <span>结果</span>
            <select value={query.result} onChange={(event) => updateQuery({ result: event.target.value as OperationLogResult | "" })}>
              {resultOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="datetime-field">
            <span>开始时间</span>
            <input value={query.created_from} type="datetime-local" onChange={(event) => updateQuery({ created_from: event.target.value })} />
          </label>
          <label className="datetime-field">
            <span>结束时间</span>
            <input value={query.created_to} type="datetime-local" onChange={(event) => updateQuery({ created_to: event.target.value })} />
          </label>
        </section>

        <div className="logs-table-card">
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
              <span>新增客户、编辑线索、标记无效、恢复线索、查看完整手机号等操作会记录在这里。</span>
            </div>
          ) : (
            <table className="logs-table">
              <thead>
                <tr>
                  <th>操作时间</th>
                  <th>操作人</th>
                  <th>操作类型</th>
                  <th>操作对象</th>
                  <th>对象名称</th>
                  <th>手机号后四位</th>
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
                    <td>{formatDate(item.created_at)}</td>
                    <td>{item.operator_name || "系统"}</td>
                    <td>{eventLabel(item)}</td>
                    <td>{objectLabel(item)}</td>
                    <td>{objectName(item)}</td>
                    <td>{phoneSuffix(item)}</td>
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

        <footer className="pagination-row logs-pagination-row">
          <div className="pagination-total">
            <span>
              共 <strong>{total}</strong> 条
            </span>
            <select aria-label="日志每页条数" value={query.page_size} onChange={(event) => updateQuery({ page_size: Number(event.target.value) })}>
              {pageSizeOptions.map((pageSize) => (
                <option key={pageSize} value={pageSize}>
                  {pageSize}条/页
                </option>
              ))}
            </select>
          </div>
          <nav aria-label="操作日志分页">
            <button type="button" disabled={query.page <= 1} onClick={() => updateQuery({ page: query.page - 1 })}>
              上一页
            </button>
            {paginationItems.map((item, index) =>
              item === "..." ? (
                <span className="pagination-ellipsis" key={`ellipsis-${index}`}>
                  ...
                </span>
              ) : item === query.page ? (
                <strong key={item}>{item}</strong>
              ) : (
                <button type="button" key={item} onClick={() => updateQuery({ page: item })}>
                  {item}
                </button>
              ),
            )}
            <button type="button" disabled={query.page * query.page_size >= total} onClick={() => updateQuery({ page: query.page + 1 })}>
              下一页
            </button>
          </nav>
        </footer>
      </section>

      {activeLog ? (
        <div className="modal-backdrop" role="presentation">
          <section className="modal logs-detail-modal" role="dialog" aria-modal="true" aria-label="操作日志详情">
            <div className="modal-head">
              <div>
                <p>日志详情</p>
                <h2>{eventLabel(activeLog)}</h2>
              </div>
            </div>
            <div className="modal-body logs-detail-body">
              <dl>
                <dt>操作时间</dt>
                <dd>{formatDate(activeLog.created_at)}</dd>
                <dt>操作人</dt>
                <dd>{activeLog.operator_name || "系统"}</dd>
                <dt>操作类型</dt>
                <dd>{eventLabel(activeLog)}</dd>
                <dt>操作对象</dt>
                <dd>{objectLabel(activeLog)}</dd>
                <dt>对象 ID</dt>
                <dd>{activeLog.target_id || activeLog.lead_id || "-"}</dd>
                <dt>操作结果</dt>
                <dd>{resultLabel(activeLog)}</dd>
                <dt>失败原因</dt>
                <dd>{activeLog.result === "failed" ? summaryText(activeLog) : "无"}</dd>
                <dt>IP / 设备信息</dt>
                <dd>{[activeLog.ip_address, activeLog.user_agent].filter(Boolean).join(" / ") || "未记录"}</dd>
              </dl>
              <section>
                <h3>操作前摘要</h3>
                <pre>{compactJson(activeLog.before_data)}</pre>
              </section>
              <section>
                <h3>操作后摘要</h3>
                <pre>{compactJson(activeLog.after_data)}</pre>
              </section>
              <section>
                <h3>说明</h3>
                <pre>{compactJson({ summary: summaryText(activeLog), metadata: activeLog.metadata || {} })}</pre>
              </section>
            </div>
            <footer className="modal-actions">
              <span>{activeLog.request_id ? `Request ID：${activeLog.request_id}` : ""}</span>
              <button type="button" className="primary-button" onClick={() => setActiveLog(null)}>
                关闭
              </button>
            </footer>
          </section>
        </div>
      ) : null}
    </div>
  );
}
