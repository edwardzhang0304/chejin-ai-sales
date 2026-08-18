import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { formatBusinessError } from "../../shared/api/client";
import { CloseIcon } from "../../shared/ui/Icons";
import { displayValue as display, formatRelativeHeartbeat as formatHeartbeat, optionalText } from "../../shared/utils/display";
import { postMutationMessage, runPostMutationRefresh } from "../../shared/utils/postMutation";
import { listWorkers } from "../workers/api";
import type { WorkerItem } from "../workers/types";
import { createSales, getSales, listSales, updateSales } from "./api";
import { CreateSalesModal } from "./components/CreateSalesModal";
import type { SalesCreatePayload, SalesItem, SalesUpdatePayload } from "./types";

type SalesFilter = {
  keyword: string;
  status: "all" | "enabled" | "disabled";
  worker: "all" | "bound" | "unbound";
};

type SalesEditForm = {
  sales_name: string;
  phone: string;
  wechat: string;
  enabled: boolean;
  sort_order: string;
  worker_id: string;
  remark: string;
};

type SalesOpenIntent = {
  salesId: string;
  editing?: boolean;
  focusWorker?: boolean;
  nonce: number;
};

const initialFilter: SalesFilter = {
  keyword: "",
  status: "all",
  worker: "all",
};

const PHONE_PATTERN = /^1[3-9]\d{9}$/;

function statusClass(enabled: boolean) {
  return enabled ? "assigned" : "invalid";
}

function formatWorkerRunning(worker?: SalesItem["current_worker"] | null) {
  if (!worker) return "-";
  if (worker.online_status !== "online") return "离线";
  const running = worker.running_status === "idle" ? "空闲" : "忙碌";
  return `在线 / ${running}`;
}

function toEditForm(item: SalesItem): SalesEditForm {
  return {
    sales_name: item.sales_name,
    phone: "",
    wechat: item.wechat ?? "",
    enabled: item.enabled,
    sort_order: item.sort_order === null || item.sort_order === undefined ? "" : String(item.sort_order),
    worker_id: item.worker_id ?? "",
    remark: item.remark ?? "",
  };
}

export function SalesPage({ openIntent }: { openIntent?: SalesOpenIntent | null }) {
  const [items, setItems] = useState<SalesItem[]>([]);
  const [workers, setWorkers] = useState<WorkerItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SalesItem | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [filter, setFilter] = useState<SalesFilter>(initialFilter);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editForm, setEditForm] = useState<SalesEditForm | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const workerSelectRef = useRef<HTMLSelectElement | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const [salesData, workerData] = await Promise.all([listSales(signal), listWorkers(signal)]);
      setItems(salesData.items);
      setWorkers(workerData.items);
      setSelectedId((current) => {
        if (current && salesData.items.some((item) => item.id === current)) return current;
        return null;
      });
      return true;
    } catch (err) {
      if (!signal?.aborted) setError(formatBusinessError(err, "销售列表加载失败，请稍后重试。"));
      return false;
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    if (!openIntent) return;
    setSelectedId(openIntent.salesId);
    setDrawerOpen(true);
  }, [openIntent]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }

    const controller = new AbortController();
    setDetailLoading(true);
    setSaveError(null);
    void getSales(selectedId, controller.signal)
      .then((data) => {
        setDetail(data);
        setEditForm(toEditForm(data));
        setEditing(false);
      })
      .catch((err) => {
        if (!controller.signal.aborted) setSaveError(formatBusinessError(err, "销售详情加载失败，请稍后重试。"));
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false);
      });

    return () => controller.abort();
  }, [selectedId]);

  useEffect(() => {
    if (!openIntent || !detail || !editForm || detail.id !== openIntent.salesId) return;
    if (openIntent.editing) setEditing(true);
  }, [detail, editForm, openIntent]);

  useEffect(() => {
    if (!openIntent?.focusWorker || !editing || detail?.id !== openIntent.salesId) return;
    const focusTimer = window.setTimeout(() => workerSelectRef.current?.focus(), 50);
    return () => window.clearTimeout(focusTimer);
  }, [detail?.id, editing, openIntent]);

  const filteredItems = useMemo(() => {
    const keyword = filter.keyword.trim().toLowerCase();
    return items.filter((item) => {
      const matchesKeyword =
        !keyword ||
        [item.sales_name, item.phone, item.wechat, item.current_worker?.worker_name]
          .filter(Boolean)
          .some((value) => String(value).toLowerCase().includes(keyword));
      const matchesStatus = filter.status === "all" || (filter.status === "enabled" ? item.enabled : !item.enabled);
      const matchesWorker = filter.worker === "all" || (filter.worker === "bound" ? Boolean(item.worker_id) : !item.worker_id);
      return matchesKeyword && matchesStatus && matchesWorker;
    });
  }, [filter, items]);

  const workerOptions = useMemo(
    () => workers.filter((worker) => worker.enabled && (!worker.bound_sales_id || worker.bound_sales_id === detail?.id)),
    [detail?.id, workers],
  );

  const metrics = useMemo(() => {
    const enabledCount = items.filter((item) => item.enabled).length;
    const boundCount = items.filter((item) => item.worker_id).length;
    const today = items.reduce((sum, item) => sum + (item.today_assignment_count ?? 0), 0);
    const blocked = items.reduce((sum, item) => sum + (item.blocking_task_count ?? 0), 0);
    return { enabledCount, boundCount, today, blocked };
  }, [items]);

  async function handleCreateSales(payload: SalesCreatePayload) {
    setSubmitting(true);
    setCreateError(null);
    setMessage(null);
    let result: Awaited<ReturnType<typeof createSales>>;
    try {
      result = await createSales(payload);
    } catch (err) {
      setCreateError(formatBusinessError(err, "新增销售失败，请稍后重试。"));
      setSubmitting(false);
      return false;
    }

    setSelectedId(result.id);
    setDrawerOpen(true);
    const refreshed = await runPostMutationRefresh(() => refresh());
    setMessage(postMutationMessage(`${payload.sales_name} 已新增。`, refreshed));
    setSubmitting(false);
    return true;
  }

  function selectRow(item: SalesItem) {
    setSelectedId(item.id);
    setDrawerOpen(true);
  }

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail || !editForm) return;

    const payload: SalesUpdatePayload = {
      sales_name: editForm.sales_name.trim(),
      wechat: optionalText(editForm.wechat),
      enabled: editForm.enabled,
      sort_order: editForm.sort_order.trim() ? Number(editForm.sort_order) : null,
      worker_id: optionalText(editForm.worker_id),
      remark: optionalText(editForm.remark),
    };

    const nextPhone = editForm.phone.trim();
    if (nextPhone) {
      if (!PHONE_PATTERN.test(nextPhone)) {
        setSaveError("请输入 11 位有效手机号；不修改请留空。");
        return;
      }
      payload.phone = nextPhone;
    }

    setSubmitting(true);
    setSaveError(null);
    setMessage(null);
    try {
      await updateSales(detail.id, payload);
    } catch (err) {
      setSaveError(formatBusinessError(err, "销售保存失败，请稍后重试。"));
      setSubmitting(false);
      return;
    }

    setEditing(false);
    const listRefreshed = await runPostMutationRefresh(() => refresh());
    const detailRefreshed = await runPostMutationRefresh(async () => {
      const nextDetail = await getSales(detail.id);
      setDetail(nextDetail);
      setEditForm(toEditForm(nextDetail));
    });
    const savedName = payload.sales_name || detail.sales_name;
    setMessage(postMutationMessage(`${savedName} 已保存。`, listRefreshed && detailRefreshed));
    setSubmitting(false);
  }

  return (
    <div className="sales-page screen-sales">
      <header className="page-header">
        <div>
          <p className="eyebrow">销售运营</p>
          <h1>销售管理</h1>
        </div>
        <button type="button" className="primary-button" onClick={() => setCreateOpen(true)}>
          新增销售
        </button>
      </header>

      <section className="metric-grid management-metrics" aria-label="销售管理指标">
        <article>
          <span>销售总数</span>
          <strong>{items.length}</strong>
          <p>启用 {metrics.enabledCount} 人</p>
        </article>
        <article>
          <span>已绑定 Worker</span>
          <strong>{metrics.boundCount}</strong>
          <p>未绑定 {Math.max(0, items.length - metrics.boundCount)} 人</p>
        </article>
        <article>
          <span>今日分配</span>
          <strong>{metrics.today}</strong>
          <p>轮询自动分配</p>
        </article>
        <article>
          <span>阻塞任务</span>
          <strong className="warning-text">{metrics.blocked}</strong>
          <p>需运营处理</p>
        </article>
      </section>

      {message ? <div className="inline-alert success">{message}</div> : null}
      {error ? <div className="inline-alert error">{error}</div> : null}

      <div className="management-grid">
        <section className="panel management-list-panel">
          <div className="panel-header">
            <div>
              <h2>销售列表</h2>
            </div>
          </div>

          <div className="management-filter-card">
            <label>
              <span>搜索</span>
              <input
                type="search"
                value={filter.keyword}
                onChange={(event) => setFilter((current) => ({ ...current, keyword: event.target.value }))}
                aria-label="搜索销售姓名、手机号、微信号"
                placeholder="销售姓名、手机号、微信号"
              />
            </label>
            <label>
              <span>状态</span>
              <select
                value={filter.status}
                onChange={(event) => setFilter((current) => ({ ...current, status: event.target.value as SalesFilter["status"] }))}
                aria-label="筛选销售状态"
              >
                <option value="all">全部状态</option>
                <option value="enabled">启用</option>
                <option value="disabled">停用</option>
              </select>
            </label>
            <label>
              <span>Worker</span>
              <select
                value={filter.worker}
                onChange={(event) => setFilter((current) => ({ ...current, worker: event.target.value as SalesFilter["worker"] }))}
                aria-label="筛选绑定 Worker"
              >
                <option value="all">全部 Worker</option>
                <option value="bound">已绑定</option>
                <option value="unbound">未绑定</option>
              </select>
            </label>
          </div>

          <div className="management-table-card">
            <table className="management-table sales-table">
              <thead>
                <tr>
                  <th>销售姓名</th>
                  <th>状态</th>
                  <th>当前 Worker</th>
                  <th>Worker 状态</th>
                  <th>轮询排序</th>
                  <th>今日分配</th>
                  <th>阻塞任务</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr>
                    <td colSpan={7}>正在加载销售列表...</td>
                  </tr>
                ) : filteredItems.length === 0 ? (
                  <tr>
                    <td colSpan={7}>暂无销售，调整筛选条件后重试。</td>
                  </tr>
                ) : (
                  filteredItems.map((item) => (
                    <tr
                      key={item.id}
                      className={selectedId === item.id && drawerOpen ? "selected" : undefined}
                      tabIndex={0}
                      onClick={() => selectRow(item)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") selectRow(item);
                      }}
                    >
                      <td className="lead-cell"><strong>{item.sales_name}</strong></td>
                      <td className="status-cell"><span className={`status ${statusClass(item.enabled)}`}>{item.enabled ? "启用" : "停用"}</span></td>
                      <td>{display(item.current_worker?.worker_name, "未绑定")}</td>
                      <td>{formatWorkerRunning(item.current_worker)}</td>
                      <td>{display(item.sort_order)}</td>
                      <td>{item.today_assignment_count ?? 0}</td>
                      <td>{item.blocking_task_count ?? 0}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>

        <aside className={`panel management-drawer sales-detail-drawer ${editing ? "is-editing" : ""} ${drawerOpen ? "" : "closed"}`}>
          {!drawerOpen ? (
            <div className="state-box">点击销售行查看详情。</div>
          ) : detailLoading || !detail || !editForm ? (
            <div className="state-box">正在加载销售详情...</div>
          ) : (
            <form className="drawer-mode is-active" onSubmit={(event) => void handleSave(event)}>
              <div className="drawer-head">
                <div>
                  <p><span className="read-value">销售详情</span><span className="edit-value">销售详情 · 编辑中</span></p>
                  <h2>{detail.sales_name}</h2>
                </div>
                <button className="icon-button drawer-close-button" type="button" onClick={() => setDrawerOpen(false)} aria-label="关闭销售详情"><CloseIcon /></button>
              </div>

              {saveError ? <div className="inline-alert error" role="alert">{saveError}</div> : null}

              <section className="drawer-section">
                <h3>基础信息</h3>
                <dl className="drawer-dl">
                  <div>
                    <dt>销售姓名</dt>
                    <dd><span className="read-value">{detail.sales_name}</span><input className="edit-value" value={editForm.sales_name} onChange={(event) => setEditForm({ ...editForm, sales_name: event.target.value })} required /></dd>
                  </div>
                  <div>
                    <dt>手机号</dt>
                    <dd>
                      <span className="read-value">{display(detail.phone)}</span>
                      <input
                        className="edit-value"
                        value={editForm.phone}
                        onChange={(event) => setEditForm({ ...editForm, phone: event.target.value })}
                        inputMode="tel"
                        autoComplete="tel"
                        maxLength={11}
                        pattern="1[3-9][0-9]{9}"
                        placeholder="不修改请留空；修改请输入完整手机号"
                      />
                    </dd>
                  </div>
                  <div>
                    <dt>飞书匹配</dt>
                    <dd>{detail.feishu_binding_status === "matched" ? "飞书已匹配" : "飞书未匹配"}</dd>
                  </div>
                  <div>
                    <dt>微信号</dt>
                    <dd><span className="read-value">{display(detail.wechat)}</span><input className="edit-value" value={editForm.wechat} onChange={(event) => setEditForm({ ...editForm, wechat: event.target.value })} /></dd>
                  </div>
                  <div>
                    <dt>
                      <span className="field-label">状态 <span className="tip-icon" tabIndex={0} title="启用/关闭分配线索" aria-label="状态说明">!</span></span>
                    </dt>
                    <dd>
                      <span className={`read-value status ${statusClass(detail.enabled)}`}>{detail.enabled ? "启用" : "停用"}</span>
                      <select className="edit-value" value={editForm.enabled ? "enabled" : "disabled"} onChange={(event) => setEditForm({ ...editForm, enabled: event.target.value === "enabled" })}>
                        <option value="enabled">启用</option>
                        <option value="disabled">停用</option>
                      </select>
                    </dd>
                  </div>
                  <div>
                    <dt>轮询排序</dt>
                    <dd><span className="read-value">{display(detail.sort_order)}</span><input className="edit-value" value={editForm.sort_order} onChange={(event) => setEditForm({ ...editForm, sort_order: event.target.value })} type="number" min="0" /></dd>
                  </div>
                </dl>
              </section>

              <section className="drawer-section">
                <h3>Worker 信息</h3>
                <dl className="drawer-dl">
                  <div>
                    <dt>当前 Worker</dt>
                    <dd>
                      <span className="read-value">{display(detail.current_worker?.worker_name, "未绑定")}</span>
                      <select
                        ref={workerSelectRef}
                        className="edit-value"
                        value={editForm.worker_id}
                        onChange={(event) => setEditForm({ ...editForm, worker_id: event.target.value })}
                      >
                        <option value="">清空 Worker</option>
                        {workerOptions.map((worker) => (
                          <option key={worker.id} value={worker.id}>
                            {worker.worker_name}（{worker.online_status === "online" ? "在线" : "离线"} / {worker.enabled ? "启用" : "停用"}）
                          </option>
                        ))}
                      </select>
                    </dd>
                  </div>
                  <div><dt>Worker 状态</dt><dd>{formatWorkerRunning(detail.current_worker)}</dd></div>
                  <div><dt>最近心跳</dt><dd>{formatHeartbeat(detail.current_worker?.last_heartbeat_at)}</dd></div>
                </dl>
              </section>

              <section className="drawer-section">
                <h3>分配统计</h3>
                <dl className="drawer-dl">
                  <div><dt>今日分配</dt><dd>{detail.today_assignment_count ?? 0}</dd></div>
                  <div><dt>阻塞任务</dt><dd>{detail.blocking_task_count ?? 0}</dd></div>
                </dl>
              </section>

              <section className="drawer-action-section">
                <h3>操作</h3>
                <div className="drawer-actions">
                  <button className="read-value" type="button" onClick={() => setEditing(true)}>编辑销售</button>
                  <button className="edit-value" type="button" onClick={() => { setEditForm(toEditForm(detail)); setEditing(false); setSaveError(null); }}>取消</button>
                  <button className="primary-button edit-value" type="submit" disabled={submitting || !editForm.sales_name.trim()}>{submitting ? "保存中..." : "保存"}</button>
                </div>
              </section>
            </form>
          )}
        </aside>
      </div>

      {createOpen ? (
        <CreateSalesModal
          submitting={submitting}
          error={createError}
          workerOptions={workers.filter((worker) => worker.enabled && !worker.bound_sales_id)}
          onClose={() => {
            setCreateOpen(false);
            setCreateError(null);
          }}
          onSubmit={handleCreateSales}
        />
      ) : null}
    </div>
  );
}
