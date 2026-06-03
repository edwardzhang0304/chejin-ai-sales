import { useCallback, useEffect, useRef, useState } from "react";

import { formatApiError } from "../../shared/api/client";
import { createSales, listSales, updateSales } from "./api";
import { CreateSalesModal } from "./components/CreateSalesModal";
import type { SalesItem, SalesUpsertPayload } from "./types";

function toUpdatePayload(item: SalesItem, patch: Partial<SalesItem>) {
  const next = { ...item, ...patch };
  return {
    sales_name: next.sales_name,
    phone: next.phone,
    wechat: next.wechat,
    feishu_user_id: next.feishu_user_id,
    enabled: next.enabled,
    sort_order: next.sort_order,
    remark: next.remark,
  };
}

export function SalesPage() {
  const [items, setItems] = useState<SalesItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busySalesId, setBusySalesId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const requestIdRef = useRef(0);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const requestId = ++requestIdRef.current;
    setLoading(true);
    setError(null);

    try {
      const data = await listSales(signal);
      if (signal?.aborted || requestId !== requestIdRef.current) {
        return;
      }
      setItems(data.items);
    } catch (err) {
      if (signal?.aborted || requestId !== requestIdRef.current) {
        return;
      }
      setError(formatApiError(err, "销售列表加载失败，请稍后重试。"));
    } finally {
      if (!signal?.aborted && requestId === requestIdRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  async function handleToggleEnabled(item: SalesItem, checked: boolean) {
    setBusySalesId(item.id);
    setError(null);
    setMessage(null);
    try {
      await updateSales(item.id, toUpdatePayload(item, { enabled: checked }));
      setMessage(`${item.sales_name} 已${checked ? "启用" : "停用"}。`);
      await refresh();
    } catch (err) {
      setError(formatApiError(err, "销售状态更新失败，请稍后重试。"));
    } finally {
      setBusySalesId(null);
    }
  }

  async function handleCreateSales(payload: SalesUpsertPayload) {
    setSubmitting(true);
    setCreateError(null);
    setMessage(null);
    try {
      await createSales(payload);
      setMessage(`${payload.sales_name} 已新增。`);
      await refresh();
      return true;
    } catch (err) {
      setCreateError(formatApiError(err, "新增销售失败，请稍后重试。"));
      return false;
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="sales-page">
      <header className="page-header">
        <div>
          <p className="eyebrow">销售运营</p>
          <h1>销售管理</h1>
        </div>
        <button type="button" className="primary-button" onClick={() => setCreateOpen(true)} disabled={loading}>
          新增销售
        </button>
      </header>

      {message ? <div className="inline-alert success">{message}</div> : null}
      {error ? <div className="inline-alert error">{error}</div> : null}

      {loading ? (
        <div className="state-box">正在加载销售列表...</div>
      ) : items.length === 0 ? (
        <div className="state-box">暂无销售，请先由后端初始化或新增销售。</div>
      ) : (
        <section className="sales-config-panel" aria-label="销售状态配置">
          <h2>销售状态配置</h2>
          <p>启用销售参与新线索轮询分配；停用销售不参与新线索分配，历史已分配线索不变。</p>
          <div className="sales-grid">
            {items.map((item) => {
              const statusText = item.enabled ? "启用" : "停用";
              const assignmentText = item.enabled ? "参与轮询" : "不参与轮询";

              return (
                <article key={item.id} className={item.enabled ? "sales-card" : "sales-card disabled"}>
                  <div className="sales-card-head">
                    <div>
                      <h2>{item.sales_name}</h2>
                      <p>
                        {assignmentText} · 排序 {item.sort_order ?? "-"} · 名下线索 {item.lead_count ?? 0}
                        {!item.enabled ? " · 历史线索保留" : ""}
                      </p>
                    </div>
                    <span className={`status-badge ${item.enabled ? "assigned" : "invalid"}`}>{statusText}</span>
                  </div>

                  <label className="toggle-row">
                    <input
                      type="checkbox"
                      checked={item.enabled}
                      disabled={busySalesId === item.id}
                      onChange={(event) => void handleToggleEnabled(item, event.target.checked)}
                    />
                    启用销售
                  </label>
                </article>
              );
            })}
          </div>
        </section>
      )}

      {createOpen ? (
        <CreateSalesModal
          submitting={submitting}
          error={createError}
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
