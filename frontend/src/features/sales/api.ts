import { request } from "../../shared/api/client";
import type { SalesItem, SalesUpsertPayload } from "./types";

export function listSales(signal?: AbortSignal) {
  return request<{ items: SalesItem[] }>("/sales", { signal });
}

export function createSales(payload: SalesUpsertPayload) {
  return request<{ id: string }>("/sales", {
    method: "POST",
    body: payload,
  });
}

export function updateSales(salesId: string, payload: SalesUpsertPayload) {
  return request<{ id: string }>(`/sales/${salesId}`, {
    method: "PUT",
    body: payload,
  });
}
