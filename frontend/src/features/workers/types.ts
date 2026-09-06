export type WorkerItem = {
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
  bound_sales_id: string | null;
  bound_sales_name: string | null;
  created_at?: string;
  updated_at?: string;
  worker_token?: string;
  vision_configured?: boolean;
  vision_credential_updated_at?: string | null;
};

export type WorkerVisionStatus = {
  vision_configured: boolean;
  vision_credential_updated_at: string | null;
};

export type WorkerCreatePayload = {
  vision_api_key?: string;
  worker_name: string;
  device_name?: string | null;
  platform?: string;
  enabled: boolean;
  remark?: string | null;
};

export type WorkerUpdatePayload = Partial<Omit<WorkerCreatePayload, "vision_api_key">>;

export type WorkerResetResult = WorkerItem & {
  worker_token: string;
  has_running_task: boolean;
  warning: string | null;
  reset_allowed: boolean;
};
