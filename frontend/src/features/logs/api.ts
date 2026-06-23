import { request } from "../../shared/api/client";
import type { OperationLogPageResult, OperationLogQuery } from "./types";

export function listOperationLogs(query: OperationLogQuery, signal?: AbortSignal) {
  return request<OperationLogPageResult>("/operation-logs", { query, signal });
}
