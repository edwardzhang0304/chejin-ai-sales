import { request } from "../../shared/api/client";
import type { TaskCancelPayload, TaskCommentPayload, TaskDetail, TaskEvent, TaskListResponse, TaskQuery } from "./types";

const blockReasonCodes = new Set(["SALES_WORKER_NOT_BOUND"]);

function queryParams(query: TaskQuery) {
  const reasonCode = query.reason_code === "all" ? "" : query.reason_code;
  return {
    keyword: query.keyword,
    task_type: query.task_type === "all" ? "" : query.task_type,
    status: query.status === "all" ? "" : query.status,
    result_code: query.result_code === "all" ? "" : query.result_code,
    error_code: reasonCode && !blockReasonCodes.has(reasonCode) ? reasonCode : "",
    block_code: reasonCode && blockReasonCodes.has(reasonCode) ? reasonCode : "",
    sales_id: query.sales_id === "all" ? "" : query.sales_id,
    worker_id: query.worker_id === "all" ? "" : query.worker_id,
    page: query.page,
    page_size: query.page_size,
  };
}

export function listTasks(query: TaskQuery, signal?: AbortSignal) {
  return request<TaskListResponse>("/tasks", { query: queryParams(query), signal });
}

export function getTask(taskId: string, signal?: AbortSignal) {
  return request<TaskDetail>(`/tasks/${taskId}`, { signal });
}

export function listTaskEvents(taskId: string, signal?: AbortSignal) {
  return request<{ items: TaskEvent[] }>(`/tasks/${taskId}/events`, { signal });
}

export function cancelTask(taskId: string, payload: TaskCancelPayload) {
  return request<TaskDetail>(`/tasks/${taskId}/cancel`, { method: "POST", body: payload });
}

export function retryTask(taskId: string, remark?: string) {
  return request<TaskDetail>(`/tasks/${taskId}/retry`, { method: "POST", body: { remark } });
}

export function addTaskComment(taskId: string, payload: TaskCommentPayload) {
  return request<TaskDetail>(`/tasks/${taskId}/comments`, { method: "POST", body: payload });
}
