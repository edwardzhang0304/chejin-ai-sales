export type TaskStatus = "blocked" | "pending" | "running" | "completed" | "failed" | "cancelled";

export type TaskType = "add_friend" | "chat_reply" | "follow_up" | string;

export type TaskQuery = {
  keyword: string;
  task_type: string;
  status: TaskStatus | "all";
  result_code: string;
  reason_code: string;
  sales_id: string;
  worker_id: string;
  page: number;
  page_size: number;
};

export type TaskMetrics = {
  blocked: number;
  pending: number;
  running: number;
  completed_today: number;
  failed_today: number;
};

export type TaskListItem = {
  id: string;
  task_type: TaskType;
  status: TaskStatus;
  result_code: string | null;
  error_code: string | null;
  block_code: string | null;
  current_step: string | null;
  lead_id: string | null;
  customer_name?: string | null;
  primary_phone_masked?: string | null;
  primary_phone?: string | null;
  wechat?: string | null;
  phone_suffix?: string | null;
  lead_name?: string | null;
  lead_phone?: string | null;
  lead_wechat?: string | null;
  verify_message?: string | null;
  remark_name?: string | null;
  remark_code?: string | null;
  remark_code_valid?: boolean | null;
  lead_status?: string | null;
  sales_id: string | null;
  sales_name: string | null;
  worker_id: string | null;
  worker_name: string | null;
  executor_type: string | null;
  executor_id: string | null;
  executor_name: string | null;
  executor_status: string | null;
  last_heartbeat_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  cancelled_at: string | null;
  claimed_at?: string | null;
  failed_at?: string | null;
  result_at: string | null;
  result_remark: string | null;
  block_reason: string | null;
  cancel_reason: string | null;
  remark: string | null;
  original_task_id: string | null;
  available_actions?: Array<{ code: string; label: string; enabled: boolean; target?: Record<string, unknown> }>;
  business_object?: {
    type: string;
    lead?: {
      id: string;
      customer_name: string | null;
      status: string | null;
      primary_phone_masked: string | null;
      phone_suffix: string | null;
      remark: string | null;
    } | null;
  } | null;
  execution?: {
    sales?: {
      id: string;
      sales_name: string;
      wechat: string | null;
      enabled: boolean;
      worker_id: string | null;
    } | null;
    worker?: {
      id: string;
      worker_name: string;
      device_name: string | null;
      enabled: boolean;
      online_status: string | null;
      running_status: string | null;
      current_task: string | null;
      last_heartbeat_at: string | null;
    } | null;
    current_step: string | null;
    claimed_at: string | null;
    completed_at: string | null;
    failed_at: string | null;
    cancelled_at: string | null;
  } | null;
  created_at: string;
  updated_at: string;
};

export type TaskEvent = {
  id: string;
  task_id: string;
  event_type: string;
  from_status: TaskStatus | null;
  to_status: TaskStatus | null;
  current_step: string | null;
  operator_name: string | null;
  worker_id?: string | null;
  executor_name: string | null;
  result_code: string | null;
  error_code: string | null;
  block_code: string | null;
  remark: string | null;
  created_at: string;
};

export type TaskListResponse = {
  items: TaskListItem[];
  total: number;
  page: number;
  page_size: number;
  metrics?: Partial<TaskMetrics>;
};

export type TaskDetail = TaskListItem & {
  failure_step?: string | null;
  failure_remark?: string | null;
  status_flow?: TaskEvent[];
  events?: TaskEvent[];
  notes?: Array<{ id: string; content: string; operator_name: string | null; created_at: string }>;
  comments?: Array<{ id: string; remark: string; operator_name: string | null; created_at: string }>;
};

export type TaskCommentPayload = {
  content: string;
};

export type TaskCancelPayload = {
  reason?: string;
};
