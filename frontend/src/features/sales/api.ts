import { request } from "../../shared/api/client";
import type { SalesCreatePayload, SalesItem, SalesUpdatePayload } from "./types";

export function listSales(signal?: AbortSignal) {
  return request<{ items: SalesItem[] }>("/sales", { signal });
}

export function createSales(payload: SalesCreatePayload) {
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
