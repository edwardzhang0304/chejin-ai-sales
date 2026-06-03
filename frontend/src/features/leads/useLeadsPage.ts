import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, formatApiError } from "../../shared/api/client";
import { getLeadDetail, getLeadStats, listLeads } from "./api";
import type { LeadDetail, LeadListItem, LeadListQuery, LeadStats, LeadStatus } from "./types";

const DEFAULT_QUERY: Required<Pick<LeadListQuery, "page" | "page_size">> = {
  page: 1,
  page_size: 20,
};

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === "AbortError";
}

export function useLeadsPage() {
  const [keyword, setKeyword] = useState("");
  const [status, setStatus] = useState<LeadStatus | "">("");
  const [salesId, setSalesId] = useState("");
  const [page, setPage] = useState(DEFAULT_QUERY.page);
  const [pageSize, setPageSize] = useState(DEFAULT_QUERY.page_size);
  const [items, setItems] = useState<LeadListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stats, setStats] = useState<LeadStats | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [activeLeadId, setActiveLeadId] = useState<string | null>(null);
  const [detail, setDetail] = useState<LeadDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const listRequestIdRef = useRef(0);
  const detailRequestIdRef = useRef(0);

  const query = useMemo<LeadListQuery>(
    () => ({
      keyword: keyword.trim() || undefined,
      status,
      sales_id: salesId || undefined,
      page,
      page_size: pageSize,
    }),
    [keyword, page, pageSize, salesId, status],
  );

  const loadLeads = useCallback(async (signal?: AbortSignal) => {
    const requestId = ++listRequestIdRef.current;
    setLoading(true);
    setError(null);

    try {
      const data = await listLeads(query, signal);
      if (signal?.aborted || requestId !== listRequestIdRef.current) {
        return;
      }
      setItems(data.items);
      setTotal(data.total);
      setSelectedIds(new Set());
      setActiveLeadId((current) => current ?? data.items[0]?.id ?? null);
    } catch (err) {
      if (signal?.aborted || isAbortError(err) || requestId !== listRequestIdRef.current) {
        return;
      }
      if (err instanceof ApiError) {
        setError(formatApiError(err, "线索列表加载失败，请稍后重试。"));
      } else {
        setError("线索列表加载失败，请稍后重试。");
      }
    } finally {
      if (!signal?.aborted && requestId === listRequestIdRef.current) {
        setLoading(false);
      }
    }
  }, [query]);

  const loadStats = useCallback(async (signal?: AbortSignal) => {
    try {
      const data = await getLeadStats(signal);
      if (!signal?.aborted) {
        setStats(data);
      }
    } catch (err) {
      if (!signal?.aborted && !isAbortError(err)) {
        setStats(null);
      }
    }
  }, []);

  const refresh = useCallback(async () => {
    await Promise.all([loadLeads(), loadStats()]);
  }, [loadLeads, loadStats]);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([loadLeads(controller.signal), loadStats(controller.signal)]);
    return () => controller.abort();
  }, [loadLeads, loadStats]);

  const refreshDetail = useCallback(async (leadId = activeLeadId, signal?: AbortSignal) => {
    if (!leadId) {
      detailRequestIdRef.current += 1;
      setDetail(null);
      setDetailError(null);
      setDetailLoading(false);
      return;
    }

    const requestId = ++detailRequestIdRef.current;
    setDetailLoading(true);
    setDetailError(null);

    try {
      const data = await getLeadDetail(leadId, signal);
      if (!signal?.aborted && requestId === detailRequestIdRef.current) {
        setDetail(data);
        setDetailError(null);
      }
    } catch (err) {
      if (!signal?.aborted && !isAbortError(err) && requestId === detailRequestIdRef.current) {
        setDetail(null);
        setDetailError(formatApiError(err, "线索详情加载失败，请稍后重试。"));
      }
    } finally {
      if (!signal?.aborted && requestId === detailRequestIdRef.current) {
        setDetailLoading(false);
      }
    }
  }, [activeLeadId]);

  useEffect(() => {
    if (!activeLeadId) {
      void refreshDetail(null);
      return;
    }

    const controller = new AbortController();
    void refreshDetail(activeLeadId, controller.signal);

    return () => controller.abort();
  }, [activeLeadId, refreshDetail]);

  const toggleSelected = useCallback((leadId: string) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(leadId)) {
        next.delete(leadId);
      } else {
        next.add(leadId);
      }
      return next;
    });
  }, []);

  const toggleAllVisible = useCallback(() => {
    setSelectedIds((current) => {
      if (items.length > 0 && items.every((item) => current.has(item.id))) {
        return new Set();
      }
      return new Set(items.map((item) => item.id));
    });
  }, [items]);

  return {
    query: { keyword, status, salesId, page, page_size: pageSize },
    setKeyword,
    setStatus,
    setSalesId,
    setPage,
    setPageSize,
    items,
    total,
    loading,
    error,
    stats,
    refresh,
    selectedIds,
    toggleSelected,
    toggleAllVisible,
    activeLeadId,
    setActiveLeadId,
    detail,
    detailLoading,
    detailError,
    refreshDetail,
  };
}
