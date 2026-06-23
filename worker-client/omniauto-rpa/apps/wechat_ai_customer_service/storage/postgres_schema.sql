CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.tenants (
  tenant_id text PRIMARY KEY,
  display_name text NOT NULL DEFAULT '',
  payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {schema}.knowledge_categories (
  tenant_id text NOT NULL,
  layer text NOT NULL,
  category_id text NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  sort_order integer NOT NULL DEFAULT 999,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, layer, category_id)
);

CREATE TABLE IF NOT EXISTS {schema}.knowledge_items (
  tenant_id text NOT NULL,
  layer text NOT NULL,
  category_id text NOT NULL,
  product_id text NOT NULL DEFAULT '',
  item_id text NOT NULL,
  status text NOT NULL DEFAULT 'active',
  search_text text NOT NULL DEFAULT '',
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, layer, category_id, product_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_items_category
  ON {schema}.knowledge_items (tenant_id, category_id, status);

CREATE INDEX IF NOT EXISTS idx_knowledge_items_product
  ON {schema}.knowledge_items (tenant_id, product_id, status);

CREATE INDEX IF NOT EXISTS idx_knowledge_items_payload
  ON {schema}.knowledge_items USING gin (payload jsonb_path_ops);

CREATE TABLE IF NOT EXISTS {schema}.review_candidates (
  tenant_id text NOT NULL,
  candidate_id text PRIMARY KEY,
  status text NOT NULL DEFAULT 'pending',
  target_category text NOT NULL DEFAULT '',
  dedupe_key text NOT NULL DEFAULT '',
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_review_candidates_tenant_status
  ON {schema}.review_candidates (tenant_id, status);

CREATE TABLE IF NOT EXISTS {schema}.uploads (
  tenant_id text NOT NULL,
  upload_id text PRIMARY KEY,
  kind text NOT NULL,
  filename text NOT NULL,
  stored_path text NOT NULL,
  sha256 text NOT NULL,
  learned boolean NOT NULL DEFAULT false,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_uploads_tenant_kind
  ON {schema}.uploads (tenant_id, kind);

CREATE TABLE IF NOT EXISTS {schema}.raw_conversations (
  tenant_id text NOT NULL,
  conversation_id text PRIMARY KEY,
  conversation_type text NOT NULL DEFAULT 'unknown',
  target_name text NOT NULL DEFAULT '',
  display_name text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'active',
  learning_enabled boolean NOT NULL DEFAULT true,
  payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_raw_conversations_tenant_type
  ON {schema}.raw_conversations (tenant_id, conversation_type, status);

CREATE TABLE IF NOT EXISTS {schema}.raw_messages (
  tenant_id text NOT NULL,
  raw_message_id text PRIMARY KEY,
  conversation_id text NOT NULL,
  dedupe_key text NOT NULL,
  message_id text NOT NULL DEFAULT '',
  sender text NOT NULL DEFAULT '',
  sender_role text NOT NULL DEFAULT 'unknown',
  content_type text NOT NULL DEFAULT 'text',
  content text NOT NULL DEFAULT '',
  message_time text NOT NULL DEFAULT '',
  learning_enabled boolean NOT NULL DEFAULT true,
  payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  observed_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_raw_messages_conversation_time
  ON {schema}.raw_messages (tenant_id, conversation_id, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_raw_messages_content
  ON {schema}.raw_messages USING gin (payload jsonb_path_ops);

CREATE TABLE IF NOT EXISTS {schema}.raw_message_batches (
  tenant_id text NOT NULL,
  batch_id text PRIMARY KEY,
  conversation_id text NOT NULL DEFAULT '',
  reason text NOT NULL DEFAULT '',
  message_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_raw_message_batches_conversation
  ON {schema}.raw_message_batches (tenant_id, conversation_id, created_at DESC);

CREATE TABLE IF NOT EXISTS {schema}.audit_events (
  event_id bigserial PRIMARY KEY,
  tenant_id text NOT NULL,
  action text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_action
  ON {schema}.audit_events (tenant_id, action, created_at DESC);

CREATE TABLE IF NOT EXISTS {schema}.version_snapshots (
  tenant_id text NOT NULL,
  version_id text PRIMARY KEY,
  reason text NOT NULL DEFAULT '',
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {schema}.rag_sources (
  tenant_id text NOT NULL,
  source_id text PRIMARY KEY,
  source_type text NOT NULL DEFAULT '',
  category text NOT NULL DEFAULT '',
  product_id text NOT NULL DEFAULT '',
  source_path text NOT NULL DEFAULT '',
  content_hash text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'active',
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rag_sources_tenant_status
  ON {schema}.rag_sources (tenant_id, status);

CREATE TABLE IF NOT EXISTS {schema}.rag_chunks (
  tenant_id text NOT NULL,
  chunk_id text PRIMARY KEY,
  source_id text NOT NULL,
  source_type text NOT NULL DEFAULT '',
  category text NOT NULL DEFAULT '',
  product_id text NOT NULL DEFAULT '',
  chunk_index integer NOT NULL DEFAULT 0,
  text text NOT NULL,
  status text NOT NULL DEFAULT 'active',
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_source
  ON {schema}.rag_chunks (tenant_id, source_id, status);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_product
  ON {schema}.rag_chunks (tenant_id, product_id, status);

CREATE TABLE IF NOT EXISTS {schema}.rag_index_entries (
  tenant_id text NOT NULL,
  chunk_id text PRIMARY KEY,
  source_id text NOT NULL DEFAULT '',
  terms jsonb NOT NULL DEFAULT '[]'::jsonb,
  semantic_terms jsonb NOT NULL DEFAULT '[]'::jsonb,
  risk_terms jsonb NOT NULL DEFAULT '[]'::jsonb,
  payload jsonb NOT NULL,
  built_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rag_index_tenant
  ON {schema}.rag_index_entries (tenant_id);

CREATE TABLE IF NOT EXISTS {schema}.rag_experiences (
  tenant_id text NOT NULL,
  experience_id text PRIMARY KEY,
  status text NOT NULL DEFAULT 'active',
  summary text NOT NULL DEFAULT '',
  question text NOT NULL DEFAULT '',
  reply_text text NOT NULL DEFAULT '',
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rag_experiences_tenant_status
  ON {schema}.rag_experiences (tenant_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS {schema}.app_kv (
  tenant_id text NOT NULL,
  namespace text NOT NULL,
  key text NOT NULL,
  payload jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, namespace, key)
);

CREATE TABLE IF NOT EXISTS {schema}.work_queue_jobs (
  tenant_id text NOT NULL,
  job_id text PRIMARY KEY,
  queue text NOT NULL DEFAULT 'default',
  kind text NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  priority integer NOT NULL DEFAULT 5,
  dedupe_key text NOT NULL DEFAULT '',
  attempts integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 3,
  available_at timestamptz NOT NULL DEFAULT now(),
  locked_until timestamptz,
  locked_by text NOT NULL DEFAULT '',
  payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  result jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  error text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_work_queue_jobs_tenant_status
  ON {schema}.work_queue_jobs (tenant_id, status, queue, priority, available_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_work_queue_jobs_active_dedupe
  ON {schema}.work_queue_jobs (tenant_id, queue, dedupe_key)
  WHERE dedupe_key <> '' AND status IN ('pending', 'running');

CREATE TABLE IF NOT EXISTS {schema}.handoff_cases (
  tenant_id text NOT NULL,
  case_id text PRIMARY KEY,
  target text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'open',
  priority integer NOT NULL DEFAULT 1,
  reason text NOT NULL DEFAULT '',
  message_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  message_contents jsonb NOT NULL DEFAULT '[]'::jsonb,
  reply_text text NOT NULL DEFAULT '',
  operator_alert jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  product_context jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  resolution jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_handoff_cases_tenant_status
  ON {schema}.handoff_cases (tenant_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS {schema}.runtime_heartbeats (
  tenant_id text NOT NULL,
  component_id text NOT NULL,
  status text NOT NULL DEFAULT 'ok',
  message text NOT NULL DEFAULT '',
  payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, component_id)
);

CREATE INDEX IF NOT EXISTS idx_runtime_heartbeats_tenant
  ON {schema}.runtime_heartbeats (tenant_id, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS {schema}.customers (
  tenant_id text NOT NULL,
  profile_id text PRIMARY KEY,
  target_name text NOT NULL DEFAULT '',
  display_name text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'active',
  tags jsonb NOT NULL DEFAULT '{}'::jsonb,
  basic_info jsonb NOT NULL DEFAULT '{}'::jsonb,
  conversation_summary text NOT NULL DEFAULT '',
  greeting_preference jsonb NOT NULL DEFAULT '{}'::jsonb,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_customers_tenant_status
  ON {schema}.customers (tenant_id, status);

CREATE INDEX IF NOT EXISTS idx_customers_tenant_target
  ON {schema}.customers (tenant_id, target_name);

CREATE INDEX IF NOT EXISTS idx_customers_payload
  ON {schema}.customers USING gin (payload jsonb_path_ops);

CREATE TABLE IF NOT EXISTS {schema}.customer_conversations (
  tenant_id text NOT NULL,
  conversation_id text PRIMARY KEY,
  profile_id text NOT NULL DEFAULT '',
  target_name text NOT NULL DEFAULT '',
  summary text NOT NULL DEFAULT '',
  last_message_at timestamptz NOT NULL DEFAULT now(),
  message_count integer NOT NULL DEFAULT 0,
  reply_count integer NOT NULL DEFAULT 0,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_customer_conversations_tenant_profile
  ON {schema}.customer_conversations (tenant_id, profile_id, updated_at DESC);
