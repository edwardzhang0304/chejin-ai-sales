import { request } from "../../shared/api/client";
import type { WorkerCreatePayload, WorkerItem, WorkerResetResult, WorkerUpdatePayload } from "./types";

export function listWorkers(signal?: AbortSignal) {
  return request<{ items: WorkerItem[] }>("/workers", { signal });
}

export function getWorker(workerId: string, signal?: AbortSignal) {
  return request<WorkerItem>(`/workers/${workerId}`, { signal });
}

export function createWorker(payload: WorkerCreatePayload) {
  return request<WorkerItem>("/workers", {
    method: "POST",
    body: payload,
  });
}

export function updateWorker(workerId: string, payload: WorkerUpdatePayload) {
  return request<WorkerItem>(`/workers/${workerId}`, {
    method: "PUT",
    body: payload,
  });
}

export function resetWorkerBinding(workerId: string) {
  return request<WorkerResetResult>(`/workers/${workerId}/reset-binding`, {
    method: "POST",
    body: { force: true },
  });
}
