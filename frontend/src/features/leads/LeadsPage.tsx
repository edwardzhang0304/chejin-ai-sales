import { useEffect, useState } from "react";

import { ApiError, formatApiError } from "../../shared/api/client";
import { batchMarkInvalid, createLead, exportLeads, markLeadInvalid, restoreLead as restoreLeadApi, retryAutoAssign, revealContact } from "./api";
import { CreateLeadModal } from "./components/CreateLeadModal";
import { InvalidLeadModal } from "./components/InvalidLeadModal";
import { LeadDetailDrawer } from "./components/LeadDetailDrawer";
import { LeadsTable } from "./components/LeadsTable";
import { RestoreLeadModal } from "./components/RestoreLeadModal";
import { useLeadsPage } from "./useLeadsPage";
import type { DuplicateLeadErrorData, InvalidLeadPayload, LeadCreatePayload, LeadStatus } from "./types";
import { listSales } from "../sales/api";
import type { SalesItem } from "../sales/types";

const statusOptions: Array<{ value: LeadStatus | ""; label: string }> = [
  { value: "", label: "全部状态" },
  { value: "unassigned", label: "未分配" },
  { value: "assigned", label: "已分配" },
  { value: "invalid", label: "无效" },
];

const pageSizeOptions = [20, 50, 100];

function statValue(value: number | undefined) {
  return value === undefined ? "-" : value.toLocaleString("zh-CN");
}

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

export function LeadsPage() {
  const leads = useLeadsPage();
  const [createOpen, setCreateOpen] = useState(false);
  const [duplicateData, setDuplicateData] = useState<DuplicateLeadErrorData | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [invalidLeadIds, setInvalidLeadIds] = useState<string[] | null>(null);
  const [invalidError, setInvalidError] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [restoreLeadId, setRestoreLeadId] = useState<string | null>(null);
  const [restoreError, setRestoreError] = useState<string | null>(null);
  const [salesOptions, setSalesOptions] = useState<SalesItem[]>([]);
  const [revealedPhones, setRevealedPhones] = useState<Record<string, string>>({});

  useEffect(() => {
    const controller = new AbortController();
    void listSales(controller.signal)
      .then((data) => setSalesOptions(data.items))
      .catch(() => setSalesOptions([]));
    return () => controller.abort();
  }, []);

  async function handleCreate(payload: LeadCreatePayload, options?: { continueAdding?: boolean }) {
    setSubmitting(true);
    setCreateError(null);
    setDuplicateData(null);

    try {
      await createLead(payload);
      if (!options?.continueAdding) {
        setCreateOpen(false);
      }
      setActionMessage(options?.continueAdding ? "已保存，可继续新增客户。" : "已新增客户。");
      await leads.refresh();
      return true;
    } catch (err) {
      if (err instanceof ApiError && err.code === "LEAD_PHONE_DUPLICATED") {
        setDuplicateData(err.data as DuplicateLeadErrorData);
        setCreateError(formatApiError(err, "新增客户失败，请稍后重试。"));
      } else if (err instanceof ApiError) {
        setCreateError(formatApiError(err, "新增客户失败，请稍后重试。"));
      } else {
        setCreateError("新增客户失败，请稍后重试。");
      }
      return false;
    } finally {
      setSubmitting(false);
    }
  }

  function selectedLeadIds() {
    return [...leads.selectedIds];
  }

  function openBatchInvalid() {
    const leadIds = selectedLeadIds();
    if (leadIds.length === 0) {
      setActionError("请先选择要标记无效的线索。");
      return;
    }
    setInvalidError(null);
    setInvalidLeadIds(leadIds);
  }

  async function handleInvalid(payload: InvalidLeadPayload) {
    if (!invalidLeadIds?.length) {
      return;
    }

    const leadIds = invalidLeadIds;
    setActionBusy("invalid");
    setInvalidError(null);
    try {
      if (leadIds.length === 1) {
        await markLeadInvalid(leadIds[0], payload);
      } else {
        await batchMarkInvalid(leadIds, payload);
      }
      setInvalidLeadIds(null);
      setActionMessage(`已标记 ${leadIds.length} 条线索为无效。`);
      await leads.refresh();
      if (leads.activeLeadId && leadIds.includes(leads.activeLeadId)) {
        await leads.refreshDetail(leads.activeLeadId);
      }
    } catch (err) {
      setInvalidError(formatApiError(err, "标记无效失败，请稍后重试。"));
    } finally {
      setActionBusy(null);
    }
  }

  async function handleRestore(leadId: string) {
    setActionBusy(`restore:${leadId}`);
    setActionError(null);
    setRestoreError(null);
    try {
      await restoreLeadApi(leadId);
      setRestoreLeadId(null);
      setActionMessage("已恢复为有效线索。");
      await leads.refresh();
      await leads.refreshDetail(leadId);
    } catch (err) {
      setRestoreError(formatApiError(err, "恢复有效失败，请稍后重试。"));
    } finally {
      setActionBusy(null);
    }
  }

  async function handleRetryAssign(leadIds: string[]) {
    const ids = leadIds.length ? leadIds : leads.items.filter((item) => item.status === "unassigned").map((item) => item.id);
    if (ids.length === 0) {
      setActionError("请选择要重新分配的线索，或在当前页保留未分配线索。");
      return;
    }

    setActionBusy("retry-assign");
    setActionError(null);
    try {
      const result = await retryAutoAssign(ids);
      setActionMessage(`重新分配完成：成功 ${result.succeeded} 条，失败 ${result.failed} 条。`);
      await leads.refresh();
      if (leads.activeLeadId && ids.includes(leads.activeLeadId)) {
        await leads.refreshDetail(leads.activeLeadId);
      }
    } catch (err) {
      setActionError(formatApiError(err, "重新分配失败，请稍后重试。"));
    } finally {
      setActionBusy(null);
    }
  }

  async function handleExportSelected() {
    const leadIds = selectedLeadIds();
    if (leadIds.length === 0) {
      setActionError("请先选择要导出的线索。");
      return;
    }

    setActionBusy("export");
    setActionError(null);
    try {
      const blob = await exportLeads(leadIds);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `leads_export_${Date.now()}.csv`;
      link.click();
      URL.revokeObjectURL(url);
      setActionMessage(`已导出 ${leadIds.length} 条线索。`);
    } catch (err) {
      setActionError(formatApiError(err, err instanceof Error ? err.message : "导出失败，请稍后重试。"));
    } finally {
      setActionBusy(null);
    }
  }

  async function handleRevealPhone(contactId: string) {
    if (!leads.activeLeadId) {
      return;
    }
    const reason = window.prompt("请输入查看手机号明文的原因", "电话确认到店时间");
    if (!reason?.trim()) {
      return;
    }

    setActionBusy(`reveal:${contactId}`);
    setActionError(null);
    try {
      const revealed = await revealContact(leads.activeLeadId, contactId, reason.trim());
      setRevealedPhones((current) => ({ ...current, [revealed.contact_id]: revealed.value }));
      setActionMessage(`手机号明文：${revealed.value}。本次查看已写入审计日志。`);
    } catch (err) {
      setActionError(formatApiError(err, "手机号明文查看失败，请稍后重试。"));
    } finally {
      setActionBusy(null);
    }
  }

  const selectedCount = leads.selectedIds.size;
  const assignedCount = leads.stats?.assigned_count ?? 0;
  const unassignedCount = leads.stats?.unassigned_count ?? 0;
  const successRate = leads.stats?.assignment_success_rate === null || leads.stats?.assignment_success_rate === undefined
    ? "-"
    : `${leads.stats.assignment_success_rate.toFixed(1)}%`;
  const totalPages = Math.max(1, Math.ceil(leads.total / leads.query.page_size));
  const paginationItems = getPaginationItems(leads.query.page, totalPages);
  const restoreLeadItem = restoreLeadId
    ? leads.items.find((item) => item.id === restoreLeadId) || (leads.detail?.id === restoreLeadId ? leads.detail : null)
    : null;

  return (
    <div className="leads-page">
      <header className="page-header">
        <div>
          <p className="eyebrow">线索运营</p>
          <h1>客户线索导入与分配</h1>
        </div>
        <button type="button" className="primary-button" onClick={() => setCreateOpen(true)}>
          新增客户
        </button>
      </header>

      <section className="metric-grid" aria-label="线索指标">
        <article>
          <span>今日新增</span>
          <strong>{statValue(leads.stats?.today_new_count)}</strong>
          <p>人工录入 {statValue(leads.stats?.today_new_count)} 条</p>
        </article>
        <article>
          <span>已分配</span>
          <strong>{statValue(leads.stats?.assigned_count)}</strong>
          <p>今日轮询成功率 {successRate}</p>
        </article>
        <article>
          <span>未分配</span>
          <strong className="warning-text">{statValue(leads.stats?.unassigned_count)}</strong>
          <p>无可用销售 {statValue(leads.stats?.unassigned_count)} 条</p>
        </article>
        <article>
          <span>重复录入</span>
          <strong>{statValue(leads.stats?.duplicate_event_count)}</strong>
          <p>备注已追加到原线索</p>
        </article>
      </section>

      {actionMessage ? <div className="inline-alert success">{actionMessage}</div> : null}
      {actionError ? <div className="inline-alert error">{actionError}</div> : null}

      <div className="content-grid">
        <section className="list-region" aria-label="线索列表">
          <div className="panel-header">
            <div>
              <h2>线索列表</h2>
              <p>手机号默认脱敏，无效线索可按状态筛选。</p>
            </div>
            <button
              className="text-button icon-text-button"
              type="button"
              aria-label="重新分配线索"
              disabled={Boolean(actionBusy)}
              onClick={() => void handleRetryAssign(selectedLeadIds())}
            >
              <svg viewBox="0 0 16 16" aria-hidden="true">
                <path d="M13.2 6.5a5.3 5.3 0 0 0-9.7-2.1L2.2 6M2.2 3v3h3M2.8 9.5a5.3 5.3 0 0 0 9.7 2.1l1.3-1.6M13.8 13v-3h-3" />
              </svg>
              <span>重新分配线索</span>
            </button>
          </div>

          <section className="filter-card" aria-label="线索筛选">
            <label className="search-field">
              <span>搜索</span>
              <input
                value={leads.query.keyword}
                onChange={(event) => {
                  leads.setKeyword(event.target.value);
                  leads.setPage(1);
                }}
                placeholder="客户名称、手机号后四位、微信、备注"
              />
            </label>

            <label className="select-field">
              <span>状态</span>
              <select
                value={leads.query.status}
                onChange={(event) => {
                  leads.setStatus(event.target.value as LeadStatus | "");
                  leads.setPage(1);
                }}
              >
                {statusOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>

            <label className="select-field">
              <span>销售</span>
              <select
                aria-label="筛选销售"
                value={leads.query.salesId}
                onChange={(event) => {
                  leads.setSalesId(event.target.value);
                  leads.setPage(1);
                }}
              >
                <option value="">全部销售</option>
                {salesOptions.map((sales) => (
                  <option key={sales.id} value={sales.id}>
                    {sales.sales_name}
                  </option>
                ))}
              </select>
            </label>
          </section>

          <div className="bulk-row">
            <span>已选 {selectedCount} 条</span>
            <div>
              <button type="button" className="ghost-button" disabled={selectedCount === 0 || Boolean(actionBusy)} onClick={openBatchInvalid}>
                标记无效
              </button>
              <button
                type="button"
                className="ghost-button"
                disabled={selectedCount === 0 || Boolean(actionBusy)}
                onClick={() => void handleExportSelected()}
              >
                导出
              </button>
            </div>
          </div>

          <LeadsTable
            items={leads.items}
            loading={leads.loading}
            error={leads.error}
            selectedIds={leads.selectedIds}
            activeLeadId={leads.activeLeadId}
            onRetry={() => void leads.refresh()}
            onToggleSelected={leads.toggleSelected}
            onToggleAllVisible={leads.toggleAllVisible}
            onOpenDetail={leads.setActiveLeadId}
          />

          <footer className="pagination-row">
            <div className="pagination-total">
              <span>
                共 <strong>{leads.total}</strong> 条
              </span>
              <select
                aria-label="每页条数"
                value={leads.query.page_size}
                onChange={(event) => {
                  leads.setPageSize(Number(event.target.value));
                  leads.setPage(1);
                }}
              >
                {pageSizeOptions.map((pageSize) => (
                  <option key={pageSize} value={pageSize}>
                    {pageSize}条/页
                  </option>
                ))}
              </select>
            </div>
            <nav aria-label="线索分页">
              <button type="button" disabled={leads.query.page <= 1} onClick={() => leads.setPage(leads.query.page - 1)}>
                上一页
              </button>
              {paginationItems.map((item, index) =>
                item === "..." ? (
                  <span className="pagination-ellipsis" key={`ellipsis-${index}`}>
                    ...
                  </span>
                ) : item === leads.query.page ? (
                  <strong key={item}>{item}</strong>
                ) : (
                  <button type="button" key={item} onClick={() => leads.setPage(item)}>
                    {item}
                  </button>
                ),
              )}
              <button
                type="button"
                disabled={leads.query.page * leads.query.page_size >= leads.total}
                onClick={() => leads.setPage(leads.query.page + 1)}
              >
                下一页
              </button>
            </nav>
          </footer>
        </section>

        <LeadDetailDrawer
          detail={leads.detail}
          loading={leads.detailLoading}
          error={leads.detailError}
          onClose={() => leads.setActiveLeadId(null)}
          onRetry={() => void leads.refreshDetail()}
          onMarkInvalid={(leadId) => {
            setInvalidError(null);
            setInvalidLeadIds([leadId]);
          }}
          onRestore={(leadId) => {
            setRestoreError(null);
            setRestoreLeadId(leadId);
          }}
          revealedPhones={revealedPhones}
          onRevealPhone={(contactId) => void handleRevealPhone(contactId)}
        />
      </div>

      {createOpen ? (
        <CreateLeadModal
          submitting={submitting}
          error={createError}
          duplicateData={duplicateData}
          onClose={() => setCreateOpen(false)}
          onOpenDuplicateLead={(leadId) => {
            setCreateOpen(false);
            leads.setActiveLeadId(leadId);
          }}
          onSubmit={handleCreate}
        />
      ) : null}

      {invalidLeadIds ? (
        <InvalidLeadModal
          count={invalidLeadIds.length}
          submitting={actionBusy === "invalid"}
          error={invalidError}
          onClose={() => setInvalidLeadIds(null)}
          onSubmit={(payload) => void handleInvalid(payload)}
        />
      ) : null}

      {restoreLeadId ? (
        <RestoreLeadModal
          lead={restoreLeadItem}
          submitting={actionBusy === `restore:${restoreLeadId}`}
          error={restoreError}
          onClose={() => {
            setRestoreError(null);
            setRestoreLeadId(null);
          }}
          onConfirm={() => void handleRestore(restoreLeadId)}
        />
      ) : null}
    </div>
  );
}
