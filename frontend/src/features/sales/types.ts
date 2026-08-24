export type SalesItem = {
  id: string;
  sales_name: string;
  phone: string;
  wechat: string | null;
  feishu_binding_status: "matched" | "unmatched";
  worker_id: string | null;
  current_worker?: SalesWorkerSummary | null;
  enabled: boolean;
  sort_order: number | null;
  remark: string | null;
  lead_count: number;
  today_assignment_count?: number;
  blocking_task_count?: number;
  created_at?: string;
  updated_at?: string;
};

export type SalesCreatePayload = {
  sales_name: string;
  phone: string;
  wechat?: string | null;
  worker_id?: string | null;
  enabled: boolean;
  sort_order?: number | null;
  remark?: string | null;
};

export type SalesUpdatePayload = Partial<Omit<SalesCreatePayload, "phone">> & {
  sales_name?: string;
  phone?: string;
};

export type SalesWorkerSummary = {
  id: string;
  worker_name: string;
  device_name: string | null;
  platform: string;
  enabled: boolean;
  online_status: string;
  run_status: string;
  running_status: string;
  current_task: string | null;
  last_heartbeat_at: string | null;
  client_binding_state: string | null;
  remark: string | null;
  bound_sales_id?: string | null;
  bound_sales_name?: string | null;
};
