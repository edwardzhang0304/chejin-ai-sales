export type TaskStatus = "blocked" | "pending" | "running" | "completed" | "failed" | "cancelled";

export type TaskType = "add_friend" | "chat_reply" | string;

export type TaskQuery = {
  keyword: string;
  task_type: string;
  status: TaskStatus | "all";
  result_code: string;
  exception_code: string;
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
  primary_phone_masked?: string | null;
  primary_phone?: string | null;
  wechat?: string | null;
  phone_suffix?: string | null;
  lead_phone?: string | null;
  verify_message?: string | null;
  remark_name?: string | null;
  remark_code?: string | null;
  remark_code_valid?: boolean | null;
  sales_id: string | null;
  sales_name: string | null;
  worker_id: string | null;
  executor_type: string | null;
  executor_id: string | null;
  last_heartbeat_at: string | null;
  completed_at: string | null;
  cancelled_at: string | null;
  claimed_at?: string | null;
  failed_at?: string | null;
  result_remark: string | null;
  block_reason: string | null;
  cancel_reason: string | null;
  remark: string | null;
  original_task_id: string | null;
  reply_action_id?: string | null;
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

export type TaskC3Summary = {
  message_batch: {
    id: string;
    conversation_id: string;
    status: string;
    active: boolean;
    message_count: number;
    generation_no: number;
    decision: string | null;
    error_code: string | null;
    suggested_action: string | null;
    superseded_by_batch_id: string | null;
    generated_at: string | null;
    created_at: string;
    updated_at: string;
  } | null;
  reply_action: {
    id: string;
    batch_id: string;
    conversation_id: string;
    status: string;
    current: boolean;
    generation_no: number;
    decision: string;
    reply_text_hash: string | null;
    confidence: number | null;
    risk_flags: string[];
    guard_result: string | null;
    handoff_reason_code: string | null;
    error_code: string | null;
    suggested_action: string | null;
    expire_at: string | null;
    claimed_by_worker_id: string | null;
    claimed_task_id: string | null;
    sending_claimed_at: string | null;
    sent_at: string | null;
    created_at: string;
    updated_at: string;
  } | null;
  sent_ack: {
    id: string;
    reply_action_id: string;
    task_id: string;
    worker_id: string;
    client_instance_id: string | null;
    send_result: "sent" | "failed" | "unknown" | string;
    reply_text_hash: string | null;
    sidecar_run_id: string | null;
    error_code: string | null;
    remark: string | null;
    sent_at: string | null;
    created_at: string;
  } | null;
  handoff_event: {
    id: string;
    conversation_id: string;
    batch_id: string | null;
    status: string;
    handoff_reason_code: string;
    reason_detail: string | null;
    risk_flags: string[];
    evidence_refs: string[];
    notify_error_code: string | null;
    closed_at: string | null;
    created_at: string;
    updated_at: string;
  } | null;
} | null;

export type TaskEvent = {
  id: string;
  task_id: string;
  event_type: string;
  from_status: TaskStatus | null;
  to_status: TaskStatus | null;
  current_step: string | null;
  operator_name: string | null;
  worker_id?: string | null;
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
  c3?: TaskC3Summary;
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
