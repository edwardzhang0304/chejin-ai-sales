import { request } from "../../shared/api/client";
import type {
  KnowledgeFilterStatus,
  KnowledgeItem,
  KnowledgeListResult,
  KnowledgePreview,
  KnowledgePublishResult,
  KnowledgeRelease,
  KnowledgeReleaseList,
  KnowledgeSummary,
} from "./types";

export function getKnowledgeSummary(signal?: AbortSignal) {
  return request<KnowledgeSummary>("/knowledge/summary", { signal });
}

export function listKnowledgeItems(query: { keyword?: string; status?: KnowledgeFilterStatus; page?: number; page_size?: number }, signal?: AbortSignal) {
  return request<KnowledgeListResult>("/knowledge/items", { query, signal });
}

export function getKnowledgeItem(itemId: string, signal?: AbortSignal) {
  return request<KnowledgeItem>(`/knowledge/items/${encodeURIComponent(itemId)}`, { signal });
}

export function createKnowledgeDraft(payload: { title: string; content: string }) {
  return request<KnowledgeItem>("/knowledge/items", { method: "POST", body: payload });
}

export function updateKnowledgeDraft(itemId: string, payload: { title: string; content: string; expected_updated_at: string }) {
  return request<KnowledgeItem>(`/knowledge/items/${encodeURIComponent(itemId)}/draft`, {
    method: "PUT",
    body: payload,
  });
}

export function stageKnowledgeArchive(itemId: string) {
  return request<KnowledgeItem>(`/knowledge/items/${encodeURIComponent(itemId)}/archive`, { method: "POST" });
}

export function previewKnowledgeRelease(payload: {
  operation: "create" | "update" | "archive";
  item_id: string;
  expected_updated_at?: string;
}) {
  return request<KnowledgePreview>("/knowledge/releases/preview", { method: "POST", body: payload });
}

export function confirmKnowledgeRelease(previewId: string, contentDigest: string) {
  return request<KnowledgePublishResult>("/knowledge/releases", {
    method: "POST",
    body: { preview_id: previewId, content_digest: contentDigest },
  });
}

export function listKnowledgeReleases(page = 1, pageSize = 20, signal?: AbortSignal) {
  return request<KnowledgeReleaseList>("/knowledge/releases", { query: { page, page_size: pageSize }, signal });
}

export function getKnowledgeRelease(releaseId: string, signal?: AbortSignal) {
  return request<KnowledgeRelease>(`/knowledge/releases/${encodeURIComponent(releaseId)}`, { signal });
}

export function previewKnowledgeRollback(targetReleaseId: string) {
  return request<KnowledgePreview>("/knowledge/releases/rollback/preview", {
    method: "POST",
    body: { target_release_id: targetReleaseId },
  });
}
