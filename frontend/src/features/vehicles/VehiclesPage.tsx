import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { formatBusinessError } from "../../shared/api/client";
import { ConfirmModal } from "../../shared/ui/ConfirmModal";
import { Toast } from "../../shared/ui/Toast";
import { postMutationMessage, runPostMutationRefresh } from "../../shared/utils/postMutation";
import { createVehicle, getVehicle, listVehicles } from "./api";
import { AuthenticatedVehicleImage } from "./components/AuthenticatedVehicleImage";
import { CreateVehicleModal } from "./components/CreateVehicleModal";
import { VehicleDetailDrawer } from "./components/VehicleDetailDrawer";
import { VehicleImportModal } from "./components/VehicleImportModal";
import type { VehicleItem, VehicleListingFilter } from "./types";

type PendingDirtyAction =
  | { kind: "switch"; code: string }
  | { kind: "create" }
  | { kind: "import" }
  | { kind: "close" };

const pageSizeOptions = [20, 50];

function getPaginationItems(currentPage: number, totalPages: number): Array<number | "..."> {
  if (totalPages <= 5) return Array.from({ length: totalPages }, (_, index) => index + 1);
  if (currentPage <= 3) return [1, 2, 3, "...", totalPages];
  if (currentPage >= totalPages - 2) return [1, "...", totalPages - 2, totalPages - 1, totalPages];
  return [1, "...", currentPage - 1, currentPage, currentPage + 1, "...", totalPages];
}

function formatMoney(value: number | string | null) {
  if (value === null || value === "") return "-";
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} 万` : String(value);
}

function formatDate(value: string | null | undefined) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date).replaceAll("/", "-");
}

function formatMileage(value: number | null) {
  if (value === null) return "-";
  if (value >= 10_000) return `${Number((value / 10_000).toFixed(1))} 万公里`;
  return `${value.toLocaleString("zh-CN")} 公里`;
}

export function VehiclesPage() {
  const [keyword, setKeyword] = useState("");
  const [debouncedKeyword, setDebouncedKeyword] = useState("");
  const [status, setStatus] = useState<VehicleListingFilter>("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [items, setItems] = useState<VehicleItem[]>([]);
  const [total, setTotal] = useState(0);
  const [counts, setCounts] = useState({ all: 0, listed: 0, unlisted: 0 });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeCode, setActiveCode] = useState<string | null>(null);
  const [detail, setDetail] = useState<VehicleItem | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [drawerDirty, setDrawerDirty] = useState(false);
  const [pendingDirtyAction, setPendingDirtyAction] = useState<PendingDirtyAction | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [toast, setToast] = useState<{ message: string; tone: "success" | "error" } | null>(null);
  const listRequestId = useRef(0);
  const detailRequestId = useRef(0);

  const notify = useCallback((message: string, tone: "success" | "error" = "success") => setToast({ message, tone }), []);
  const dismissToast = useCallback(() => setToast(null), []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedKeyword(keyword.trim());
      setPage(1);
    }, 320);
    return () => window.clearTimeout(timer);
  }, [keyword]);

  const refreshList = useCallback(async (signal?: AbortSignal) => {
    const requestId = ++listRequestId.current;
    setLoading(true);
    setError(null);
    try {
      const [result, all, listed, unlisted] = await Promise.all([
        listVehicles({ keyword: debouncedKeyword, listing_status: status, page, page_size: pageSize }, signal),
        listVehicles({ listing_status: "all", page: 1, page_size: 1 }, signal),
        listVehicles({ listing_status: "listed", page: 1, page_size: 1 }, signal),
        listVehicles({ listing_status: "unlisted", page: 1, page_size: 1 }, signal),
      ]);
      if (signal?.aborted || requestId !== listRequestId.current) return false;
      setItems(result.items);
      setTotal(result.total);
      setCounts({ all: all.total, listed: listed.total, unlisted: unlisted.total });
      const maxPage = Math.max(1, Math.ceil(result.total / pageSize));
      if (page > maxPage) setPage(maxPage);
      return true;
    } catch (err) {
      if (signal?.aborted || requestId !== listRequestId.current) return false;
      setError(formatBusinessError(err, "车辆列表加载失败，请稍后重试。"));
      return false;
    } finally {
      if (!signal?.aborted && requestId === listRequestId.current) setLoading(false);
    }
  }, [debouncedKeyword, page, pageSize, status]);

  useEffect(() => {
    const controller = new AbortController();
    void refreshList(controller.signal);
    return () => controller.abort();
  }, [refreshList]);

  const refreshDetail = useCallback(async (code = activeCode, signal?: AbortSignal) => {
    if (!code) return true;
    const requestId = ++detailRequestId.current;
    setDetailLoading(true);
    setDetailError(null);
    try {
      const data = await getVehicle(code, signal);
      if (signal?.aborted || requestId !== detailRequestId.current) return false;
      setDetail(data);
      return true;
    } catch (err) {
      if (signal?.aborted || requestId !== detailRequestId.current) return false;
      setDetailError(formatBusinessError(err, "车辆详情加载失败，请稍后重试。"));
      return false;
    } finally {
      if (!signal?.aborted && requestId === detailRequestId.current) setDetailLoading(false);
    }
  }, [activeCode]);

  useEffect(() => {
    if (!activeCode) {
      setDetail(null);
      setDetailError(null);
      return;
    }
    const controller = new AbortController();
    void refreshDetail(activeCode, controller.signal);
    return () => controller.abort();
  }, [activeCode, refreshDetail]);

  async function syncVehicle(updated?: VehicleItem) {
    if (updated) setDetail(updated);
    const detailRefreshed = updated ? true : await runPostMutationRefresh(() => refreshDetail());
    const listRefreshed = await runPostMutationRefresh(() => refreshList());
    return detailRefreshed && listRefreshed;
  }

  function selectVehicle(code: string) {
    if (code === activeCode) return;
    if (drawerDirty) {
      setPendingDirtyAction({ kind: "switch", code });
      return;
    }
    setActiveCode(code);
  }

  function openCreateVehicle() {
    if (drawerDirty) {
      setPendingDirtyAction({ kind: "create" });
      return;
    }
    setCreateError(null);
    setCreateOpen(true);
  }

  function openImportVehicles() {
    if (drawerDirty) {
      setPendingDirtyAction({ kind: "import" });
      return;
    }
    setImportOpen(true);
  }

  function closeVehicleDetail() {
    if (drawerDirty) {
      setPendingDirtyAction({ kind: "close" });
      return;
    }
    setActiveCode(null);
    setDetail(null);
    setDrawerDirty(false);
  }

  function confirmDirtyAction() {
    const action = pendingDirtyAction;
    setPendingDirtyAction(null);
    setDrawerDirty(false);
    if (!action) return;
    if (action.kind === "switch") {
      setActiveCode(action.code);
      return;
    }
    if (action.kind === "create") {
      setCreateError(null);
      setCreateOpen(true);
      return;
    }
    if (action.kind === "import") {
      setImportOpen(true);
      return;
    }
    setActiveCode(null);
    setDetail(null);
  }

  async function handleCreate(displayName: string) {
    setCreateBusy(true);
    setCreateError(null);
    let created: VehicleItem;
    try {
      created = await createVehicle(displayName);
    } catch (err) {
      setCreateError(formatBusinessError(err, "新增车辆失败，请重试。"));
      setCreateBusy(false);
      return;
    }

    setCreateOpen(false);
    setDetail(created);
    setActiveCode(created.vehicle_code);
    const refreshed = await runPostMutationRefresh(() => refreshList());
    notify(postMutationMessage("车辆已新增，当前默认已下架。", refreshed), "success");
    setCreateBusy(false);
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const paginationItems = useMemo(() => getPaginationItems(page, totalPages), [page, totalPages]);
  const hasFilter = Boolean(debouncedKeyword || status !== "all");

  return (
    <div className="vehicles-page">
      <Toast message={toast?.message || null} tone={toast?.tone} onDismiss={dismissToast} />
      <header className="page-header">
        <div><p className="eyebrow">车辆资料运营</p><h1>车辆管理</h1></div>
        <div className="page-actions"><button type="button" className="secondary-button" onClick={openImportVehicles}>导入车辆</button><button type="button" className="primary-button" onClick={openCreateVehicle}>新增车辆</button></div>
      </header>

      <section className="metric-grid" aria-label="车辆管理指标">
        <article><span>车辆总数</span><strong>{counts.all}</strong><p>当前车辆资料总量</p></article>
        <article><span>已上架</span><strong>{counts.listed}</strong><p>可用于客服查询与推荐</p></article>
        <article><span>已下架</span><strong>{counts.unlisted}</strong><p>仍可在后台维护</p></article>
        <article><span>待补充资料</span><strong className="warning-text">—</strong><p>缺价格或有效图片</p></article>
      </section>

      <div className="management-grid vehicle-management-grid">
        <section className="panel management-list-panel vehicle-list-panel">
          <div className="panel-header"><div><h2>车辆列表</h2></div></div>
          <div className="vehicle-filter-card">
            <label className="vehicle-search"><span>搜索</span><input type="search" value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="车辆名称、编号、品牌、车系" aria-label="搜索车辆" /></label>
            <label><span>状态</span><select aria-label="筛选车辆状态" value={status} onChange={(event) => { setStatus(event.target.value as VehicleListingFilter); setPage(1); }}><option value="all">全部状态</option><option value="listed">已上架</option><option value="unlisted">已下架</option></select></label>
          </div>

          <div className="vehicle-table-card">
            {loading ? <div className="state-box">正在加载车辆列表...</div> : error ? <div className="state-box error"><span>{error}</span><button type="button" onClick={() => void refreshList()}>重试</button></div> : items.length === 0 ? (
              <div className="vehicle-empty state-box">
                <strong>{hasFilter ? "没有找到符合条件的车辆" : "暂无车辆"}</strong>
                <span>{hasFilter ? "请调整搜索词或状态筛选后重试。" : "可新增单辆车辆，或通过最新 Excel 模板批量导入。"}</span>
                {!hasFilter ? <div><button type="button" onClick={openImportVehicles}>导入车辆</button><button type="button" className="primary-button" onClick={openCreateVehicle}>新增车辆</button></div> : null}
              </div>
            ) : (
              <table className="vehicle-table">
                <thead><tr><th>主图</th><th>车辆展示名称</th><th>车辆编号</th><th>品牌 / 车系</th><th>首次上牌</th><th>表显里程</th><th>公开售价</th><th>状态</th><th>更新时间</th></tr></thead>
                <tbody>{items.map((item) => <tr key={item.vehicle_code} tabIndex={0} className={activeCode === item.vehicle_code ? "selected" : undefined} onClick={() => selectVehicle(item.vehicle_code)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectVehicle(item.vehicle_code); } }}>
                  <td className="vehicle-thumbnail-cell">{item.main_image ? <AuthenticatedVehicleImage imageId={item.main_image.id} alt={`${item.display_name}主图`} className="vehicle-thumbnail" /> : <span className="vehicle-thumbnail-empty">暂无图片</span>}</td>
                  <td title={item.display_name}><strong>{item.display_name}</strong></td>
                  <td title={item.vehicle_code}>{item.vehicle_code}</td>
                  <td title={[item.brand, item.series].filter(Boolean).join(" / ")}>{[item.brand, item.series].filter(Boolean).join(" / ") || "-"}</td>
                  <td>{item.first_registration || "-"}</td>
                  <td>{formatMileage(item.mileage_km)}</td>
                  <td><strong>{formatMoney(item.public_price)}</strong></td>
                  <td className="status-cell"><span className={`status ${item.listing_status === "listed" ? "assigned" : "unassigned"}`}>{item.listing_status === "listed" ? "已上架" : "已下架"}</span></td>
                  <td>{formatDate(item.updated_at)}</td>
                </tr>)}</tbody>
              </table>
            )}
          </div>

          <footer className="pagination-row vehicle-pagination-row">
            <div className="pagination-total"><span>共 <strong>{total}</strong> 辆</span><select aria-label="车辆每页条数" value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1); }}>{pageSizeOptions.map((value) => <option key={value} value={value}>{value} 条/页</option>)}</select></div>
            <nav aria-label="车辆分页"><button type="button" disabled={page <= 1 || loading} onClick={() => setPage(page - 1)}>上一页</button>{paginationItems.map((item, index) => item === "..." ? <span className="pagination-ellipsis" key={`ellipsis-${index}`}>…</span> : item === page ? <strong key={item}>{item}</strong> : <button type="button" key={item} onClick={() => setPage(item)}>{item}</button>)}<button type="button" disabled={page >= totalPages || loading} onClick={() => setPage(page + 1)}>下一页</button></nav>
          </footer>
        </section>

        {activeCode ? <VehicleDetailDrawer vehicle={detail} loading={detailLoading} error={detailError} onRetry={() => void refreshDetail()} onClose={closeVehicleDetail} onDirtyChange={setDrawerDirty} onVehicleChanged={syncVehicle} onNotify={notify} /> : null}
      </div>

      <CreateVehicleModal open={createOpen} busy={createBusy} error={createError} onCancel={() => setCreateOpen(false)} onSubmit={(value) => void handleCreate(value)} />
      <VehicleImportModal
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onImported={async () => {
          const refreshed = await runPostMutationRefresh(() => refreshList());
          notify(postMutationMessage("车辆导入完成，列表已刷新。", refreshed), "success");
          return refreshed;
        }}
      />
      <ConfirmModal
        open={Boolean(pendingDirtyAction)}
        title="放弃未保存的修改？"
        description={pendingDirtyAction?.kind === "switch" ? "切换车辆会丢失当前详情中尚未保存的修改。" : pendingDirtyAction?.kind === "create" ? "打开新增车辆会丢失当前详情中尚未保存的修改。" : pendingDirtyAction?.kind === "import" ? "打开车辆导入会丢失当前详情中尚未保存的修改。" : "关闭详情会丢失当前尚未保存的车辆修改。"}
        confirmLabel={pendingDirtyAction?.kind === "switch" ? "放弃并切换" : pendingDirtyAction?.kind === "create" ? "放弃并新增" : pendingDirtyAction?.kind === "import" ? "放弃并导入" : "放弃并关闭"}
        dangerous
        onCancel={() => setPendingDirtyAction(null)}
        onConfirm={confirmDirtyAction}
      />
    </div>
  );
}
