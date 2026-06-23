import { apiErrorFromResponse, buildOperatorHeaders, buildUrl, request } from "../../shared/api/client";
import type {
  BatchInvalidResult,
  CreateLeadResult,
  InvalidLeadPayload,
  LeadStats,
  LeadCreatePayload,
  LeadDetail,
  LeadListQuery,
  LeadPageResult,
  RevealedContact,
  RetryAssignResult,
} from "./types";

export function listLeads(query: LeadListQuery, signal?: AbortSignal) {
  return request<LeadPageResult>("/leads", { query, signal });
}

export function getLeadDetail(leadId: string, signal?: AbortSignal) {
  return request<LeadDetail>(`/leads/${leadId}`, { signal });
}

export function getLeadStats(signal?: AbortSignal) {
  return request<LeadStats>("/leads/stats", { signal });
}

export function createLead(payload: LeadCreatePayload) {
  return request<CreateLeadResult>("/leads", {
    method: "POST",
    body: payload,
  });
}

export function markLeadInvalid(leadId: string, payload: InvalidLeadPayload) {
  return request<LeadDetail>(`/leads/${leadId}/mark-invalid`, {
    method: "POST",
    body: payload,
  });
}

export function restoreLead(leadId: string) {
  return request<LeadDetail>(`/leads/${leadId}/restore`, {
    method: "POST",
  });
}

export function batchMarkInvalid(leadIds: string[], payload: InvalidLeadPayload) {
  return request<BatchInvalidResult>("/leads/batch-mark-invalid", {
    method: "POST",
    body: {
      ...payload,
      lead_ids: leadIds,
    },
  });
}

export function retryAutoAssign(leadIds: string[]) {
  return request<RetryAssignResult>("/leads/retry-auto-assign", {
    method: "POST",
    body: { lead_ids: leadIds },
  });
}

export async function exportLeads(leadIds: string[]) {
  const response = await fetch(buildUrl("/leads/export"), {
    method: "POST",
    headers: {
      ...buildOperatorHeaders(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ lead_ids: leadIds, fields: [] }),
  });

  if (!response.ok) {
    throw await apiErrorFromResponse(response);
  }

  return response.blob();
}

export function revealContact(leadId: string, contactId: string, reason: string) {
  return request<RevealedContact>(`/leads/${leadId}/contacts/${contactId}/reveal`, {
    method: "POST",
    body: { reason },
  });
}
