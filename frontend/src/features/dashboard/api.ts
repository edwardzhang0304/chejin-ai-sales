import { request } from "../../shared/api/client";

export type Metric = { value: number | string | null; reason: string | null };
export type MetricKey =
  | "new_customers"
  | "total_customers"
  | "friend_passes"
  | "friend_pass_rate"
  | "communication_customers"
  | "effective_customers"
  | "effective_rate"
  | "human_response_seconds";
export type Metrics = Record<MetricKey, Metric> & {
  human_replied_rounds: number | null;
  human_pending_rounds: number | null;
};
export type DashboardData = {
  as_of: string;
  timezone: string;
  start: string;
  end: string;
  metrics: Metrics;
  sales: Array<{
    sales_id: string;
    name: string;
    enabled: boolean;
    metrics: Metrics;
  }>;
  sales_options: Array<{ id: string; name: string; enabled: boolean }>;
  realtime: {
    counts: Record<
      "running" | "paused" | "faulted" | "offline" | "unbound",
      number
    >;
    unavailable: number;
  };
  vehicles: { listed: Metric; sold: Metric; amount: Metric };
  trend: Array<{
    date: string;
    communication: number | null;
    effective: number | null;
  }>;
  coverage: string[];
};
export type DashboardQuery = {
  start: string;
  end: string;
  vehicle_start: string;
  vehicle_end: string;
  sales_id: string;
  mode: string;
  status_sales_id: string;
};
export function getDashboard(query: DashboardQuery, signal?: AbortSignal) {
  return request<DashboardData>("/dashboard", { query, signal });
}
