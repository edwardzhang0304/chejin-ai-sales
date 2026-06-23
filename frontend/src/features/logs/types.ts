import type { PageResult } from "../../shared/api/types";

export type OperationLogResult = "success" | "failed";

export type OperationLogItem = {
  id: string;
  event_type: string;
  event_label?: string;
  event_name?: string;
  module: string;
  operator_id: string | null;
  operator_name: string | null;
  target_type: string;
  target_id: string | null;
  lead_id: string | null;
  lead_customer_name?: string | null;
  metadata?: Record<string, unknown> | null;
  before_data?: Record<string, unknown> | null;
  after_data?: Record<string, unknown> | null;
  result?: OperationLogResult;
  summary?: string | null;
  ip_address?: string | null;
  user_agent?: string | null;
  request_id?: string | null;
  created_at: string;
};

export type OperationLogQuery = {
  keyword?: string;
  event_type?: string;
  module?: string;
  operator_name?: string;
  target_type?: string;
  result?: OperationLogResult | "";
  created_from?: string;
  created_to?: string;
  page?: number;
  page_size?: number;
};

export type OperationLogPageResult = PageResult<OperationLogItem>;
