import type { PageResult } from "../../shared/api/types";

export type LeadStatus = "unassigned" | "assigned" | "invalid";
export type AssignStatus = "unassigned" | "assigned" | "assign_failed";

export type LeadListItem = {
  id: string;
  customer_name: string;
  status: LeadStatus;
  source_type: string;
  source_name_snapshot: string;
  primary_phone_masked: string | null;
  primary_wechat_masked: string | null;
  sales_id: string | null;
  sales_name: string | null;
  assign_status: AssignStatus;
  assign_failure_reason: string | null;
  remark_summary: string | null;
  duplicate_count: number;
  last_duplicate_at: string | null;
  created_at: string;
  updated_at: string;
};

export type LeadContact = {
  id: string;
  contact_type: "phone" | "wechat" | "email";
  masked_value: string;
  is_primary: boolean;
};

export type LeadDetail = LeadListItem & {
  remark: string | null;
  custom_fields: Record<string, unknown> | null;
  contacts: LeadContact[];
  notes: Array<{ id: string; note_type: string; content: string; created_at: string }>;
  assignments?: Array<{
    id: string;
    assignment_result: string;
    sales_id: string | null;
    sales_name: string | null;
    assignment_type: string;
    assignment_status: string;
    failure_reason: string | null;
    created_at: string;
  }>;
  duplicate_events: Array<{
    id: string;
    submitted_customer_name: string | null;
    submitted_phone_masked: string | null;
    submitted_remark: string | null;
    created_at: string;
  }>;
  task_nodes: Array<{ key: string; label: string; time: string | null }>;
};

export type LeadListQuery = {
  keyword?: string;
  status?: LeadStatus | "";
  sales_id?: string;
  page?: number;
  page_size?: number;
};

export type LeadCreatePayload = {
  customer_name: string;
  phones: string[];
  wechats?: string[];
  emails?: string[];
  remark?: string;
  custom_fields?: Record<string, unknown>;
};

export type InvalidReason =
  | "empty_number"
  | "wrong_info"
  | "not_target_customer"
  | "test_data"
  | "duplicate_or_mistaken"
  | "other";

export type InvalidLeadPayload = {
  invalid_reason: InvalidReason;
  invalid_remark?: string;
};

export type CreateLeadResult = {
  created: boolean;
  id: string;
  status: LeadStatus;
  assign_status: AssignStatus;
  sales_id: string | null;
  sales_name: string | null;
  assignment: {
    status: string;
    failure_reason: string | null;
  };
};

export type DuplicateLeadErrorData = {
  created: false;
  duplicate_lead: {
    id: string;
    customer_name: string;
    primary_phone_masked: string | null;
    sales_name: string | null;
    created_at: string;
    updated_at: string;
  };
  duplicate_count: number;
  duplicate_dates: string[];
  note_appended: boolean;
};

export type BatchInvalidResult = {
  requested: number;
  succeeded: number;
  skipped: number;
  items: Array<{ lead_id: string; status: "succeeded" | "skipped"; reason?: string }>;
};

export type RetryAssignResult = {
  requested: number;
  succeeded: number;
  failed: number;
  items: Array<{
    lead_id: string;
    status: string;
    sales_id?: string | null;
    failure_reason?: string | null;
  }>;
};

export type LeadStats = {
  today_new_count: number;
  today_assigned_count: number;
  today_unassigned_count: number;
  assignment_success_rate: number | null;
  assigned_count: number;
  unassigned_count: number;
  duplicate_event_count: number;
};

export type RevealedContact = {
  contact_id: string;
  contact_type: string;
  value: string;
  revealed_at: string;
};

export type LeadPageResult = PageResult<LeadListItem>;
