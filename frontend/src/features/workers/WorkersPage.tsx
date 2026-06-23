import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import { formatApiError } from "../../shared/api/client";
import { useLockBodyScroll } from "../../shared/hooks/useLockBodyScroll";
import { createWorker, getWorker, listWorkers, resetWorkerBinding, updateWorker } from "./api";
import type { WorkerCreatePayload, WorkerItem, WorkerUpdatePayload } from "./types";

type WorkerFilter = {
  keyword: string;
  status: "all" | "enabled" | "disabled";
  binding: "all" | "bound" | "unbound";
};

type WorkerEditForm = {
  worker_name: string;
  device_name: string;
  enabled: boolean;
  remark: string;
};

type WorkerOpenIntent = {
  workerId: string;
  nonce: number;
};

const initialFilter: WorkerFilter = {
  keyword: "",
  status: "all",
  binding: "all",
};

function optionalText(value: string) {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function display(value: string | number | null | undefined, fallback = "-") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function formatHeartbeat(value?: string | null) {
  if (!value) return "暂无";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const diff = Date.now() - date.getTime();
  if (diff < 90_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.max(1, Math.round(diff / 60_000))} 分钟前`;
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `${month}-${day} ${hours}:${minutes}`;
}

function statusClass(active: boolean) {
  return active ? "assigned" : "invalid";
}

function onlineClass(status: string) {
  return status === "online" ? "assigned" : "unassigned";
}

function runningMeta(status: string, currentTask?: string | null) {
  if (currentTask || ["running", "busy", "executing"].includes(status)) {
    return { label: "忙碌", className: "unassigned" };
  }
  return { label: "空闲", className: "assigned" };
}

function bindingMeta(worker: WorkerItem) {
  if (worker.client_binding_state === "reset_required") return { label: "待重绑", className: "unassigned" };
  if (worker.client_binding_state || worker.last_heartbeat_at) return { label: "已绑定", className: "assigned" };
  return { label: "未绑定", className: "unassigned" };
}

function toEditForm(worker: WorkerItem): WorkerEditForm {
  return {
    worker_name: worker.worker_name,
    device_name: worker.device_name ?? "",
    enabled: worker.enabled,
    remark: worker.remark ?? "",
  };
}

function CreateWorkerModal({
  submitting,
  error,
  onClose,
  onSubmit,
}: {
  submitting: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (payload: WorkerCreatePayload) => Promise<boolean>;
}) {
  useLockBodyScroll();

  const [workerName, setWorkerName] = useState("");
  const [enabled, setEnabled] = useState(true);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const saved = await onSubmit({
      worker_name: workerName.trim(),
      platform: "mac",
      enabled,
      device_name: null,
      remark: null,
    });
    if (saved) onClose();
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <form className="modal worker-modal" aria-label="新增 Worker" onSubmit={(event) => void handleSubmit(event)}>
        <header>
          <h2>新增 Worker</h2>
        </header>
        <div className="form-stack">
          {error ? <div className="inline-alert error">{error}</div> : null}
          <section className="form-section">
            <div className="drawer-form">
              <label>
                <span>Worker 名称 <b>*</b></span>
                <input value={workerName} onChange={(event) => setWorkerName(event.target.value)} placeholder="请输入 Worker 名称" required />
              </label>
              <label>
                <span>状态</span>
                <select value={enabled ? "enabled" : "disabled"} onChange={(event) => setEnabled(event.target.value === "enabled")}>
                  <option value="enabled">启用</option>
                  <option value="disabled">停用</option>
                </select>
              </label>
            </div>
            <div className="generated-note">
              <strong>保存后系统生成</strong>
              <span>Worker ID</span>
              <span>Worker Token</span>
            </div>
          </section>
        </div>
        <footer>
          <button type="button" onClick={onClose}>取消</button>
          <button className="primary-button" type="submit" disabled={submitting}>{submitting ? "保存中..." : "保存"}</button>
        </footer>
      </form>
    </div>
  );
}

export function WorkersPage({ openIntent }: { openIntent?: WorkerOpenIntent | null }) {
  const [items, setItems] = useState<WorkerItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<WorkerItem | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(true);
  const [filter, setFilter] = useState<WorkerFilter>(initialFilter);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editForm, setEditForm] = useState<WorkerEditForm | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  useLockBodyScroll(resetOpen);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const data = await listWorkers(signal);
      setItems(data.items);
      setSelectedId((current) => {
        if (current && data.items.some((item) => item.id === current)) return current;
        return data.items[0]?.id ?? null;
      });
      setDrawerOpen(Boolean(data.items[0]));
    } catch (err) {
      if (!signal?.aborted) setError(formatApiError(err, "Worker 列表加载失败，请稍后重试。"));
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
    setSelectedId(openIntent.workerId);
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
    void getWorker(selectedId, controller.signal)
      .then((data) => {
        setDetail(data);
        setEditForm(toEditForm(data));
        setEditing(false);
      })
      .catch((err) => {
        if (!controller.signal.aborted) setSaveError(formatApiError(err, "Worker 详情加载失败，请稍后重试。"));
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false);
      });
    return () => controller.abort();
  }, [selectedId]);

  const filteredItems = useMemo(() => {
    const keyword = filter.keyword.trim().toLowerCase();
    return items.filter((item) => {
      const matchesKeyword =
        !keyword ||
        [item.worker_name, item.id, item.device_name, item.bound_sales_name]
          .filter(Boolean)
          .some((value) => String(value).toLowerCase().includes(keyword));
      const matchesStatus = filter.status === "all" || (filter.status === "enabled" ? item.enabled : !item.enabled);
      const isBound = bindingMeta(item).label !== "未绑定";
      const matchesBinding = filter.binding === "all" || (filter.binding === "bound" ? isBound : !isBound);
      return matchesKeyword && matchesStatus && matchesBinding;
    });
  }, [filter, items]);

  const metrics = useMemo(() => {
    const enabledCount = items.filter((item) => item.enabled).length;
    const boundCount = items.filter((item) => bindingMeta(item).label !== "未绑定").length;
    const onlineCount = items.filter((item) => item.online_status === "online").length;
    const busyCount = items.filter((item) => runningMeta(item.running_status, item.current_task).label === "忙碌").length;
    return { enabledCount, boundCount, onlineCount, busyCount };
  }, [items]);

  function selectRow(item: WorkerItem) {
    setSelectedId(item.id);
    setDrawerOpen(true);
  }

  async function handleCreateWorker(payload: WorkerCreatePayload) {
    setSubmitting(true);
    setCreateError(null);
    setMessage(null);
    try {
      const created = await createWorker(payload);
      setMessage(`${created.worker_name} 已新增，Worker Token 已在详情抽屉展示。`);
      await refresh();
      setSelectedId(created.id);
      setDetail(created);
      setEditForm(toEditForm(created));
      setDrawerOpen(true);
      return true;
    } catch (err) {
      setCreateError(formatApiError(err, "新增 Worker 失败，请稍后重试。"));
      return false;
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail || !editForm) return;
    const payload: WorkerUpdatePayload = {
      worker_name: editForm.worker_name.trim(),
      device_name: optionalText(editForm.device_name),
      enabled: editForm.enabled,
      remark: optionalText(editForm.remark),
    };

    setSubmitting(true);
    setSaveError(null);
    setMessage(null);
    try {
      const updated = await updateWorker(detail.id, payload);
      setMessage(`${updated.worker_name} 已保存。`);
      await refresh();
      const nextDetail = await getWorker(detail.id);
      setDetail(nextDetail);
      setEditForm(toEditForm(nextDetail));
      setEditing(false);
    } catch (err) {
      setSaveError(formatApiError(err, "Worker 保存失败，请稍后重试。"));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleResetBinding() {
    if (!detail) return;
    setSubmitting(true);
    setSaveError(null);
    setMessage(null);
    try {
      const result = await resetWorkerBinding(detail.id);
      setDetail(result);
      setEditForm(toEditForm(result));
      setMessage(result.warning || "已重置客户端绑定，新的 Worker Token 已生成。");
      setResetOpen(false);
      await refresh();
    } catch (err) {
      setSaveError(formatApiError(err, "重置绑定失败，请稍后重试。"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="workers-page screen-workers">
      <header className="page-header">
        <div>
          <p className="eyebrow">Worker 运营</p>
          <h1>Worker 管理</h1>
        </div>
        <button type="button" className="primary-button" onClick={() => setCreateOpen(true)}>
          新增 Worker
        </button>
      </header>

      <section className="metric-grid management-metrics" aria-label="Worker 管理指标">
        <article>
          <span>Worker 总数</span>
          <strong>{items.length}</strong>
          <p>启用 {metrics.enabledCount} 台</p>
        </article>
        <article>
          <span>客户端已绑定</span>
          <strong>{metrics.boundCount}</strong>
          <p>未绑定 {Math.max(0, items.length - metrics.boundCount)} 台</p>
        </article>
        <article>
          <span>在线</span>
          <strong>{metrics.onlineCount}</strong>
          <p>离线 {Math.max(0, items.length - metrics.onlineCount)} 台</p>
        </article>
        <article>
          <span>忙碌</span>
          <strong className="warning-text">{metrics.busyCount}</strong>
          <p>当前任务 {metrics.busyCount} 个</p>
        </article>
      </section>

      {message ? <div className="inline-alert success">{message}</div> : null}
      {error ? <div className="inline-alert error">{error}</div> : null}

      <div className="management-grid">
        <section className="panel management-list-panel">
          <div className="panel-header">
            <div>
              <h2>Worker 列表</h2>
              <p>Worker Token 不在列表展示，只在详情抽屉中查看。</p>
            </div>
          </div>

          <div className="management-filter-card">
            <label>
              <span>搜索</span>
              <input
                type="search"
                value={filter.keyword}
                onChange={(event) => setFilter((current) => ({ ...current, keyword: event.target.value }))}
                aria-label="搜索 Worker 名称或 ID"
                placeholder="Worker 名称或 ID"
              />
            </label>
            <label>
              <span>状态</span>
              <select value={filter.status} onChange={(event) => setFilter((current) => ({ ...current, status: event.target.value as WorkerFilter["status"] }))} aria-label="筛选 Worker 状态">
                <option value="all">全部状态</option>
                <option value="enabled">启用</option>
                <option value="disabled">停用</option>
              </select>
            </label>
            <label>
              <span>绑定状态</span>
              <select value={filter.binding} onChange={(event) => setFilter((current) => ({ ...current, binding: event.target.value as WorkerFilter["binding"] }))} aria-label="筛选客户端绑定状态">
                <option value="all">全部</option>
                <option value="bound">已绑定</option>
                <option value="unbound">未绑定</option>
              </select>
            </label>
          </div>

          <div className="management-table-card">
            <table className="management-table worker-table">
              <thead>
                <tr>
                  <th>Worker 名称</th>
                  <th>Worker ID</th>
                  <th>状态</th>
                  <th>客户端</th>
                  <th>在线</th>
                  <th>运行</th>
                  <th>当前任务</th>
                  <th>绑定销售</th>
                  <th>最近心跳</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr><td colSpan={9}>正在加载 Worker 列表...</td></tr>
                ) : filteredItems.length === 0 ? (
                  <tr><td colSpan={9}>暂无 Worker，调整筛选条件后重试。</td></tr>
                ) : (
                  filteredItems.map((item) => {
                    const binding = bindingMeta(item);
                    const running = runningMeta(item.running_status, item.current_task);
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
                        <td className="lead-cell"><strong>{item.worker_name}</strong><small>{display(item.device_name, "Mac 客户端")}</small></td>
                        <td>{item.id}</td>
                        <td className="status-cell"><span className={`status ${statusClass(item.enabled)}`}>{item.enabled ? "启用" : "停用"}</span></td>
                        <td className="status-cell"><span className={`status ${binding.className}`}>{binding.label}</span></td>
                        <td className="status-cell"><span className={`status ${onlineClass(item.online_status)}`}>{item.online_status === "online" ? "在线" : "离线"}</span></td>
                        <td className="status-cell"><span className={`status ${running.className}`}>{running.label}</span></td>
                        <td>{display(item.current_task)}</td>
                        <td>{display(item.bound_sales_name, "未绑定")}</td>
                        <td>{formatHeartbeat(item.last_heartbeat_at)}</td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </section>

        <aside className={`panel management-drawer worker-detail-drawer ${editing ? "is-editing" : ""} ${drawerOpen ? "" : "closed"}`}>
          {!drawerOpen ? (
            <div className="state-box">点击 Worker 行查看详情。</div>
          ) : detailLoading || !detail || !editForm ? (
            <div className="state-box">正在加载 Worker 详情...</div>
          ) : (
            <form className="drawer-mode is-active" onSubmit={(event) => void handleSave(event)}>
              <div className="drawer-head">
                <div>
                  <p><span className="read-value">Worker 详情</span><span className="edit-value">Worker 详情 · 编辑中</span></p>
                  <h2>{detail.worker_name}</h2>
                </div>
                <button className="icon-button" type="button" onClick={() => setDrawerOpen(false)} aria-label="关闭 Worker 详情">×</button>
              </div>

              {saveError ? <div className="inline-alert error">{saveError}</div> : null}

              <section className="drawer-section">
                <h3>基础信息</h3>
                <dl className="drawer-dl">
                  <div>
                    <dt>Worker 名称</dt>
                    <dd><span className="read-value">{detail.worker_name}</span><input className="edit-value" value={editForm.worker_name} onChange={(event) => setEditForm({ ...editForm, worker_name: event.target.value })} required /></dd>
                  </div>
                  <div><dt>Worker ID</dt><dd>{detail.id}</dd></div>
                  <div><dt>Worker Token</dt><dd className="token-text">{display(detail.worker_token, "详情加载后展示")}</dd></div>
                  <div>
                    <dt>状态</dt>
                    <dd>
                      <span className={`read-value status ${statusClass(detail.enabled)}`}>{detail.enabled ? "启用" : "停用"}</span>
                      <select className="edit-value" value={editForm.enabled ? "enabled" : "disabled"} onChange={(event) => setEditForm({ ...editForm, enabled: event.target.value === "enabled" })}>
                        <option value="enabled">启用</option>
                        <option value="disabled">停用</option>
                      </select>
                    </dd>
                  </div>
                </dl>
              </section>

              <section className="drawer-section">
                <h3>客户端绑定</h3>
                <dl className="drawer-dl">
                  <div><dt>绑定状态</dt><dd>{bindingMeta(detail).label}</dd></div>
                  <div>
                    <dt>客户端实例</dt>
                    <dd><span className="read-value">{display(detail.device_name, "暂无")}</span><input className="edit-value" value={editForm.device_name} onChange={(event) => setEditForm({ ...editForm, device_name: event.target.value })} /></dd>
                  </div>
                  <div><dt>最近心跳</dt><dd>{formatHeartbeat(detail.last_heartbeat_at)}</dd></div>
                  <div><dt>运行状态</dt><dd>{runningMeta(detail.running_status, detail.current_task).label}</dd></div>
                </dl>
              </section>

              <section className="drawer-section">
                <h3>绑定销售</h3>
                <dl className="drawer-dl">
                  <div><dt>当前销售</dt><dd>{display(detail.bound_sales_name, "未绑定")}</dd></div>
                </dl>
                <p className="drawer-hint">如需绑定或更换销售，请前往销售管理 &gt; 销售详情 &gt; 编辑销售处理。</p>
              </section>

              <section className="drawer-action-section">
                <h3>操作</h3>
                <div className="drawer-actions">
                  <button className="read-value" type="button" onClick={() => setEditing(true)}>编辑 Worker</button>
                  <button className="read-value" type="button" onClick={() => setResetOpen(true)}>重置绑定</button>
                  <button className="edit-value" type="button" onClick={() => { setEditForm(toEditForm(detail)); setEditing(false); setSaveError(null); }}>取消</button>
                  <button className="primary-button edit-value" type="submit" disabled={submitting || !editForm.worker_name.trim()}>{submitting ? "保存中..." : "保存"}</button>
                </div>
              </section>
            </form>
          )}
        </aside>
      </div>

      {createOpen ? (
        <CreateWorkerModal
          submitting={submitting}
          error={createError}
          onClose={() => {
            setCreateOpen(false);
            setCreateError(null);
          }}
          onSubmit={handleCreateWorker}
        />
      ) : null}

      {resetOpen && detail ? (
        <div className="modal-backdrop" role="presentation">
          <section className="modal small-modal" aria-label="重置客户端绑定">
            <header>
              <h2>重置客户端绑定</h2>
            </header>
            <p className="modal-copy">
              重置后将生成新的 Worker Token，旧 Token 立即失效。若该 Worker 有进行中任务，客户端需要重新绑定后才能继续上报。
            </p>
            <footer>
              <button type="button" onClick={() => setResetOpen(false)}>取消</button>
              <button className="danger-button" type="button" onClick={() => void handleResetBinding()} disabled={submitting}>
                {submitting ? "重置中..." : "确认重置绑定"}
              </button>
            </footer>
          </section>
        </div>
      ) : null}
    </div>
  );
}
