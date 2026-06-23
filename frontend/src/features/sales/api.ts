import { request } from "../../shared/api/client";
import type { SalesItem, SalesUpdatePayload, SalesUpsertPayload } from "./types";

export function listSales(signal?: AbortSignal) {
  return request<{ items: SalesItem[] }>("/sales", { signal });
}

export function createSales(payload: SalesUpsertPayload) {
  return request<{ id: string }>("/sales", {
    method: "POST",
    body: payload,
  });
}

export function getSales(salesId: string, signal?: AbortSignal) {
  return request<SalesItem>(`/sales/${salesId}`, { signal });
}

export function updateSales(salesId: string, payload: SalesUpdatePayload) {
  return request<{ id: string }>(`/sales/${salesId}`, {
    method: "PUT",
    body: payload,
  });
}
