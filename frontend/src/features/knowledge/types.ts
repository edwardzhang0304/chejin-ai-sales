export type KnowledgeStatus = "draft" | "published" | "archived";
export type KnowledgeFilterStatus = "all" | KnowledgeStatus;
export type KnowledgeOperation = "create" | "update" | "archive" | "rollback";

export type KnowledgeReleaseSummary = {
  id: string;
  version: string;
  status: "published" | "failed";
  action: "bootstrap" | KnowledgeOperation;
  operator_name: string;
  change_summary: string;
  change_set: KnowledgeChange[];
  snapshot_sha256: string;
  retrieval_index_sha256: string;
  published_at: string;
  is_current: boolean;
};

export type KnowledgeSnapshotItem = {
  item_id: string;
  revision_id: string;
  title: string;
  content: string;
  content_sha256: string;
};

export type KnowledgeRelease = KnowledgeReleaseSummary & {
  snapshot: KnowledgeSnapshotItem[];
};

export type KnowledgeItem = {
  id: string;
  title: string;
  content: string;
  status: KnowledgeStatus;
  current_revision_id: string | null;
  draft_revision_id: string | null;
  draft_operation: "create" | "update" | "archive" | null;
  published_title: string | null;
  published_content: string | null;
  last_editor_id: string | null;
  last_editor_name: string;
  published_at: string | null;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
  release_history?: KnowledgeReleaseSummary[];
};

export type KnowledgeListResult = {
  items: KnowledgeItem[];
  page: number;
  page_size: number;
  total: number;
};

export type KnowledgeSummary = {
  current_release: KnowledgeReleaseSummary;
  published_today: number;
  published_today_breakdown: {
    create: number;
    update: number;
    archive: number;
    rollback: number;
  };
  published_count: number;
  draft_count: number;
  archived_count: number;
};

export type KnowledgeChange = {
  type: "create" | "update" | "archive";
  item_id: string;
  title: string;
  before: KnowledgeSnapshotItem | null;
  after: KnowledgeSnapshotItem | null;
};

export type KnowledgeValidationIssue = {
  field: string;
  problem: string;
  suggestion: string;
};

export type KnowledgePreview = {
  preview_id: string;
  operation: KnowledgeOperation;
  item_id: string | null;
  current_version: string;
  target_version: string;
  target_release_id: string | null;
  can_publish: boolean;
  validation_issues: KnowledgeValidationIssue[];
  change_set: KnowledgeChange[];
  content_digest: string;
  expires_at: string;
};

export type KnowledgePublishResult = {
  release: KnowledgeRelease;
  item: KnowledgeItem | null;
  message: string;
};

export type KnowledgeReleaseList = {
  items: KnowledgeReleaseSummary[];
  page: number;
  page_size: number;
  total: number;
};
