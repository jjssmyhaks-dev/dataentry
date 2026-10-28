-- tenant_schema.sql
-- Applied once per tenant by public.provision_tenant(). The literal text
-- __SCHEMA__ is replaced with the tenant's schema name (tenant_<slug>) before
-- execution. Every statement is idempotent so provisioning can be re-run.
--
-- Tenant isolation is schema-level (section 10): the engine sets search_path to
-- the tenant schema for the request, chosen on the server from the WorkOS
-- organisation in the signed session. An additional tenant_id filter and RLS
-- policy are kept as the second layer, so a bug in one query cannot cross
-- tenants.

create schema if not exists __SCHEMA__;

-- ─────────────────────────────────────────────────────────────
-- 6.1 Intake
-- ─────────────────────────────────────────────────────────────

create table if not exists __SCHEMA__.documents (
  id                  uuid primary key default gen_random_uuid(),
  tenant_id           uuid not null default engine.current_tenant(),
  intake_channel      text not null default 'web_upload'
                        check (intake_channel in ('whatsapp', 'email', 'web_upload',
                                                  'bulk_upload', 'api', 'webhook', 'agent')),
  source_ref          text,                  -- message id, email id, upload id
  sender              text,                  -- phone number or email address
  file_name           text,
  mime_type           text,
  file_size           bigint,
  page_count          integer,
  file_hash           text,                  -- sha256 of the file, for dedupe
  content_signature   vector(1536),          -- pgvector content similarity (6.1)
  storage_path        text,                  -- Supabase Storage object path

  -- Classification (6.1) against the tenant's active packs
  doc_type            text,
  doc_type_confidence numeric(5,4),
  pack_key            text,
  pack_version        text,

  status              text not null default 'received'
                        check (status in ('received', 'deduplicated', 'classifying',
                                          'extracting', 'validating', 'routing',
                                          'posted', 'in_review', 'rejected', 'exception',
                                          'failed')),
  exception_reason    text,
  duplicate_of        uuid references __SCHEMA__.documents(id) on delete set null,
  split_from          uuid,                  -- parent, when one file holds several documents
  split_index         integer,

  received_at         timestamptz not null default now(),
  processed_at        timestamptz,
  raw_meta            jsonb not null default '{}'::jsonb,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

create index if not exists idx_documents_status on __SCHEMA__.documents (status, received_at desc);
create index if not exists idx_documents_type on __SCHEMA__.documents (doc_type, received_at desc);
create index if not exists idx_documents_hash on __SCHEMA__.documents (file_hash)
  where file_hash is not null;
create index if not exists idx_documents_sender on __SCHEMA__.documents (sender);

drop trigger if exists trg_documents_updated on __SCHEMA__.documents;
create trigger trg_documents_updated before update on __SCHEMA__.documents
  for each row execute function engine.touch_updated_at();

create table if not exists __SCHEMA__.document_pages (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null default engine.current_tenant(),
  document_id uuid not null references __SCHEMA__.documents(id) on delete cascade,
  page_no     integer not null,
  width       integer,
  height      integer,
  has_text_layer boolean not null default false,
  image_hash  text,
  text_digest text,
  meta        jsonb not null default '{}'::jsonb,
  unique (document_id, page_no)
);

-- ─────────────────────────────────────────────────────────────
-- 6.2 Extraction
-- ─────────────────────────────────────────────────────────────

create table if not exists __SCHEMA__.extraction_runs (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null default engine.current_tenant(),
  document_id    uuid not null references __SCHEMA__.documents(id) on delete cascade,
  kind           text not null default 'extract' check (kind in ('classify', 'extract', 'recheck')),
  pack_key       text,
  pack_version   text,
  model          text,
  prompt_version text,
  attempt        integer not null default 1,
  status         text not null default 'running'
                   check (status in ('running', 'succeeded', 'schema_failed',
                                     'escalated', 'error')),
  schema_ok      boolean,
  error_text     text,
  tokens_in      integer,
  tokens_out     integer,
  cost_usd       numeric(10,6),
  latency_ms     integer,
  trace_id       text,                        -- Langfuse trace (section 9)
  created_at     timestamptz not null default now(),
  finished_at    timestamptz
);

create index if not exists idx_runs_document on __SCHEMA__.extraction_runs (document_id, created_at desc);

-- One row per logical document found. A bundle of invoices in a single PDF is
-- split before extraction (Appendix A), so one file can yield several records.
create table if not exists __SCHEMA__.extracted_records (
  id                 uuid primary key default gen_random_uuid(),
  tenant_id          uuid not null default engine.current_tenant(),
  document_id        uuid not null references __SCHEMA__.documents(id) on delete cascade,
  run_id             uuid references __SCHEMA__.extraction_runs(id) on delete set null,
  doc_type           text not null,
  pack_key           text,
  pack_version       text,
  order_index        integer not null default 0,
  page_from          integer,
  page_to            integer,
  payload            jsonb not null default '{}'::jsonb,  -- the record, conforming to the pack schema
  overall_confidence numeric(5,4),
  min_field_confidence numeric(5,4),
  field_count        integer,
  low_confidence_fields text[] not null default '{}',
  status             text not null default 'extracted'
                       check (status in ('extracted', 'validating', 'auto_posted', 'in_review',
                                         'approved', 'corrected', 'rejected', 'posted',
                                         'posted_draft', 'held', 'reversed', 'failed')),
  amount_total       numeric(14,2),
  vendor_gstin       text,
  vendor_name        text,
  document_number    text,
  document_date      date,
  business_key       text,                    -- vendor+number+date, for duplicate detection
  embedding          vector(1536),
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create index if not exists idx_records_document on __SCHEMA__.extracted_records (document_id, order_index);
create index if not exists idx_records_status on __SCHEMA__.extracted_records (status, created_at desc);
create index if not exists idx_records_type on __SCHEMA__.extracted_records (doc_type, created_at desc);
create index if not exists idx_records_business_key on __SCHEMA__.extracted_records (business_key);
create index if not exists idx_records_vendor on __SCHEMA__.extracted_records (vendor_gstin, vendor_name);

drop trigger if exists trg_records_updated on __SCHEMA__.extracted_records;
create trigger trg_records_updated before update on __SCHEMA__.extracted_records
  for each row execute function engine.touch_updated_at();

-- Every field carries a value, a confidence score, and a pointer to where it was
-- found on the page (Appendix A shared schema rules).
create table if not exists __SCHEMA__.field_values (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null default engine.current_tenant(),
  record_id   uuid not null references __SCHEMA__.extracted_records(id) on delete cascade,
  path        text not null,                 -- e.g. line_items.0.hsn
  label       text,
  value_text  text,
  value_num   numeric(16,4),
  value_date  date,
  value_json  jsonb,
  confidence  numeric(5,4) not null default 0,
  page_no     integer,
  bbox        jsonb,                         -- {x,y,w,h} normalised 0..1
  source_span text,
  mandatory   boolean not null default false,
  corrected   boolean not null default false,
  corrected_by text,
  corrected_at timestamptz,
  created_at  timestamptz not null default now(),
  unique (record_id, path)
);

create index if not exists idx_field_values_record on __SCHEMA__.field_values (record_id);
create index if not exists idx_field_values_low_conf on __SCHEMA__.field_values (record_id, confidence)
  where confidence < 0.93;

-- ─────────────────────────────────────────────────────────────
-- 6.3 / Appendix B Validation
-- ─────────────────────────────────────────────────────────────

create table if not exists __SCHEMA__.rule_results (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null default engine.current_tenant(),
  record_id    uuid not null references __SCHEMA__.extracted_records(id) on delete cascade,
  rule_key     text not null,
  rule_version text,
  pack_key     text,
  pack_version text,
  severity     text not null default 'warn' check (severity in ('fail', 'warn', 'info')),
  outcome      text not null check (outcome in ('pass', 'warn', 'fail', 'skipped')),
  title        text,
  message      text,                          -- plain language, shown to the reviewer
  details      jsonb not null default '{}'::jsonb,
  fields       text[] not null default '{}',  -- which fields triggered it
  degraded     boolean not null default false,  -- external lookup unavailable
  recheck_at   timestamptz,
  checked_at   timestamptz not null default now(),
  unique (record_id, rule_key)
);

create index if not exists idx_rule_results_record on __SCHEMA__.rule_results (record_id, outcome);
create index if not exists idx_rule_results_key on __SCHEMA__.rule_results (rule_key, outcome, checked_at desc);

-- Tenant-level tolerances and thresholds for the declarative rules.
create table if not exists __SCHEMA__.rule_settings (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null default engine.current_tenant(),
  rule_key       text not null,
  doc_type       text,                        -- null = all document types
  enabled        boolean not null default true,
  severity_override text check (severity_override in ('fail', 'warn', 'info')),
  settings       jsonb not null default '{}'::jsonb,  -- e.g. {"tolerance": 2.00}
  updated_by     text,
  updated_at     timestamptz not null default now(),
  unique (rule_key, doc_type)
);

-- ─────────────────────────────────────────────────────────────
-- 6.4 / Appendix D Review queue
-- ─────────────────────────────────────────────────────────────

create table if not exists __SCHEMA__.review_items (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null default engine.current_tenant(),
  record_id     uuid not null references __SCHEMA__.extracted_records(id) on delete cascade,
  document_id   uuid not null references __SCHEMA__.documents(id) on delete cascade,
  status        text not null default 'pending'
                  check (status in ('pending', 'approved', 'corrected', 'rejected',
                                    'held', 'clarification_requested', 'auto_posted',
                                    'failed')),
  priority      integer not null default 100,   -- lower = sooner
  priority_band text not null default 'low_confidence'
                  check (priority_band in ('rule_failure', 'low_confidence', 'routine')),
  reasons       jsonb not null default '[]'::jsonb,  -- plain-language flags
  flagged_fields text[] not null default '{}',
  failed_rules  text[] not null default '{}',
  warned_rules  text[] not null default '{}',
  amount_total  numeric(14,2),
  doc_type      text,
  vendor_name   text,
  due_at        timestamptz,                    -- GST cut-offs / payment dates
  assignee      text,
  assigned_to_role text,
  requires_supervisor boolean not null default false,
  resolution    text,
  resolution_reason text,
  resolution_note text,
  resolved_by   text,
  resolved_at   timestamptz,
  created_at    timestamptz not null default now()
);

create index if not exists idx_review_status on __SCHEMA__.review_items (status, priority, created_at);
create index if not exists idx_review_doc_type on __SCHEMA__.review_items (doc_type, status);
create index if not exists idx_review_assignee on __SCHEMA__.review_items (assignee, status);
create index if not exists idx_review_amount on __SCHEMA__.review_items (amount_total desc nulls last);

create table if not exists __SCHEMA__.review_actions (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null default engine.current_tenant(),
  review_item_id uuid not null references __SCHEMA__.review_items(id) on delete cascade,
  record_id      uuid not null,
  actor          text not null,
  actor_role     text,
  action         text not null
                   check (action in ('approve', 'edit_and_approve', 'correct', 'reject',
                                     'hold', 'request_document', 'reassign', 'unhold',
                                     'auto_post', 'reopen')),
  field_path     text,
  before_value   text,
  after_value    text,
  note           text,
  is_one_off     boolean not null default false,
  created_at     timestamptz not null default now()
);

create index if not exists idx_review_actions_item on __SCHEMA__.review_actions (review_item_id, created_at);
create index if not exists idx_review_actions_actor on __SCHEMA__.review_actions (actor, created_at desc);

-- Clarification requests sent back to the sender over WhatsApp or email.
create table if not exists __SCHEMA__.clarification_requests (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null default engine.current_tenant(),
  review_item_id uuid references __SCHEMA__.review_items(id) on delete set null,
  document_id   uuid not null references __SCHEMA__.documents(id) on delete cascade,
  channel       text not null default 'email' check (channel in ('email', 'whatsapp')),
  to_address    text,
  template_key  text not null,
  body          text,
  status        text not null default 'sent' check (status in ('queued', 'sent', 'answered', 'expired')),
  sent_at       timestamptz not null default now(),
  answered_at   timestamptz,
  response_document_id uuid
);

-- ─────────────────────────────────────────────────────────────
-- 6.6 Learning loop
-- ─────────────────────────────────────────────────────────────

create table if not exists __SCHEMA__.corrections (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null default engine.current_tenant(),
  record_id    uuid not null references __SCHEMA__.extracted_records(id) on delete cascade,
  document_id  uuid,
  field_path   text not null,
  before_value text,
  after_value  text,
  vendor_gstin text,
  vendor_name  text,
  doc_type     text,
  actor        text,
  is_one_off   boolean not null default false,
  applied_to   jsonb not null default '{}'::jsonb,  -- which learner consumed it
  created_at   timestamptz not null default now()
);

create index if not exists idx_corrections_vendor on __SCHEMA__.corrections (vendor_gstin, field_path);
create index if not exists idx_corrections_created on __SCHEMA__.corrections (created_at desc);

-- Repeated identical corrections prompt a suggestion to turn them into a tenant
-- rule, which a supervisor must confirm.
create table if not exists __SCHEMA__.rule_suggestions (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null default engine.current_tenant(),
  kind          text not null default 'ledger_mapping'
                  check (kind in ('ledger_mapping', 'field_hint', 'validation_rule', 'layout')),
  vendor_gstin  text,
  doc_type      text,
  field_path    text,
  proposal      jsonb not null default '{}'::jsonb,
  occurrence_count integer not null default 1,
  status        text not null default 'suggested'
                  check (status in ('suggested', 'accepted', 'dismissed')),
  decided_by    text,
  decided_at    timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- Vendor-specific layouts and few-shot examples learned per tenant only.
create table if not exists __SCHEMA__.few_shot_examples (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null default engine.current_tenant(),
  doc_type     text not null,
  vendor_gstin text,
  pack_key     text,
  input_excerpt text,
  output_payload jsonb not null default '{}'::jsonb,
  quality      numeric(5,4) not null default 1.0,
  embedding    vector(1536),
  source_record_id uuid,
  created_at   timestamptz not null default now()
);

create index if not exists idx_few_shot_lookup on __SCHEMA__.few_shot_examples (doc_type, vendor_gstin);

-- ─────────────────────────────────────────────────────────────
-- Appendix C Posting
-- ─────────────────────────────────────────────────────────────

-- Master data read back from Tally so extraction and validation match the
-- customer's existing masters.
create table if not exists __SCHEMA__.master_snapshots (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null default engine.current_tenant(),
  agent_id    uuid,
  kind        text not null check (kind in ('ledgers', 'parties', 'stock_items',
                                            'tax_ledgers', 'companies', 'periods',
                                            'cost_centres', 'godowns')),
  company     text,
  captured_at timestamptz not null default now(),
  item_count  integer,
  payload     jsonb not null default '[]'::jsonb
);

create index if not exists idx_master_kind on __SCHEMA__.master_snapshots (kind, captured_at desc);

create table if not exists __SCHEMA__.ledger_mappings (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null default engine.current_tenant(),
  party_gstin    text,
  party_name     text,
  party_name_norm text,
  target_party_ledger text,
  purchase_ledger text,
  expense_ledger text,
  cgst_ledger    text,
  sgst_ledger    text,
  igst_ledger    text,
  roundoff_ledger text,
  tds_ledger     text,
  default_stock_item text,
  confidence     numeric(5,4) not null default 1.0,
  source         text not null default 'manual'
                   check (source in ('manual', 'tally_sync', 'learned', 'pack_default', 'approved_proposal')),
  use_count      integer not null default 0,
  last_used_at   timestamptz,
  approved_by    text,
  approved_at    timestamptz,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (party_gstin, party_name_norm)
);

drop trigger if exists trg_ledger_mappings_updated on __SCHEMA__.ledger_mappings;
create trigger trg_ledger_mappings_updated before update on __SCHEMA__.ledger_mappings
  for each row execute function engine.touch_updated_at();

-- Proposed new ledgers wait here for approval; the connector never creates or
-- renames a ledger on its own (Appendix C master data handling).
create table if not exists __SCHEMA__.master_proposals (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null default engine.current_tenant(),
  kind          text not null check (kind in ('party', 'ledger', 'stock_item', 'cost_centre')),
  proposed_name text not null,
  parent_group  text,
  gstin         text,
  reason        text,
  evidence      jsonb not null default '{}'::jsonb,
  status        text not null default 'pending'
                  check (status in ('pending', 'approved', 'rejected')),
  decided_by    text,
  decided_at    timestamptz,
  created_at    timestamptz not null default now()
);

create table if not exists __SCHEMA__.vouchers (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null default engine.current_tenant(),
  record_id         uuid not null references __SCHEMA__.extracted_records(id) on delete cascade,
  document_id       uuid,
  voucher_type      text not null,        -- Purchase, Payment, Receipt, Debit Note, ...
  voucher_date      date,
  party_ledger      text,
  narration         text,                 -- carries the engine record reference
  payload           jsonb not null default '{}'::jsonb,   -- exactly as Tally will receive it
  amount_total      numeric(14,2),
  gst_cgst          numeric(14,2) not null default 0,
  gst_sgst          numeric(14,2) not null default 0,
  gst_igst          numeric(14,2) not null default 0,
  roundoff          numeric(14,2) not null default 0,
  place_of_supply   text,
  tally_company     text,
  tally_voucher_number text,
  tally_voucher_id  text,
  idempotency_key   text not null,
  autonomy_stage    smallint,
  posting_mode      text check (posting_mode in ('shadow', 'draft', 'autonomous')),
  status            text not null default 'planned'
                      check (status in ('planned', 'staged', 'queued', 'posting', 'posted',
                                        'draft_staged', 'failed', 'reversed', 'cancelled',
                                        'shadow')),
  error_text        text,
  staged_at         timestamptz,
  queued_at         timestamptz,
  posted_at         timestamptz,
  reversed_at       timestamptz,
  reversal_voucher_id uuid references __SCHEMA__.vouchers(id) on delete set null,
  reverses_voucher_id uuid references __SCHEMA__.vouchers(id) on delete set null,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create unique index if not exists uq_vouchers_idem on __SCHEMA__.vouchers (idempotency_key);
create index if not exists idx_vouchers_record on __SCHEMA__.vouchers (record_id);
create index if not exists idx_vouchers_status on __SCHEMA__.vouchers (status, created_at desc);

drop trigger if exists trg_vouchers_updated on __SCHEMA__.vouchers;
create trigger trg_vouchers_updated before update on __SCHEMA__.vouchers
  for each row execute function engine.touch_updated_at();

create table if not exists __SCHEMA__.posting_attempts (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null default engine.current_tenant(),
  voucher_id   uuid not null references __SCHEMA__.vouchers(id) on delete cascade,
  command_id   uuid,
  attempt      integer not null default 1,
  mode         text,                     -- shadow / draft / autonomous / file_import
  connector    text,                     -- tally_bridge / tally_file / csv / webhook
  request      jsonb,
  response     jsonb,
  outcome      text not null default 'pending'
                 check (outcome in ('pending', 'success', 'tally_rejected', 'transport_error',
                                    'expired', 'skipped')),
  error_code   text,
  error_text   text,                     -- Tally's error, in plain language for the queue
  started_at   timestamptz not null default now(),
  finished_at  timestamptz
);

create index if not exists idx_posting_attempts_voucher on __SCHEMA__.posting_attempts (voucher_id, attempt);

-- Shadow-mode comparison: what the engine would have posted versus what the
-- human actually entered (section 12a stage 1 gate).
create table if not exists __SCHEMA__.shadow_comparisons (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null default engine.current_tenant(),
  record_id     uuid not null references __SCHEMA__.extracted_records(id) on delete cascade,
  voucher_id    uuid references __SCHEMA__.vouchers(id) on delete set null,
  human_payload jsonb not null default '{}'::jsonb,
  field_diffs   jsonb not null default '[]'::jsonb,
  match_score   numeric(5,4),
  wrong_ledger  boolean not null default false,
  wrong_tax_split boolean not null default false,
  reviewed      boolean not null default false,
  reviewed_by   text,
  created_at    timestamptz not null default now()
);

-- ─────────────────────────────────────────────────────────────
-- Appendix A: matching + compliance + bank documents
-- ─────────────────────────────────────────────────────────────

create table if not exists __SCHEMA__.purchase_orders (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null default engine.current_tenant(),
  record_id     uuid references __SCHEMA__.extracted_records(id) on delete set null,
  po_number     text,
  po_date       date,
  vendor_gstin  text,
  vendor_name   text,
  lines         jsonb not null default '[]'::jsonb,
  agreed_rate_total numeric(14,2),
  delivery_date date,
  payment_terms text,
  status        text not null default 'open' check (status in ('open', 'matched', 'closed', 'cancelled')),
  created_at    timestamptz not null default now()
);

create index if not exists idx_po_number on __SCHEMA__.purchase_orders (po_number);

create table if not exists __SCHEMA__.goods_receipts (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null default engine.current_tenant(),
  record_id      uuid references __SCHEMA__.extracted_records(id) on delete set null,
  challan_number text,
  challan_date   date,
  vendor_gstin   text,
  vendor_name    text,
  po_id          uuid references __SCHEMA__.purchase_orders(id) on delete set null,
  po_number      text,
  lines          jsonb not null default '[]'::jsonb,
  quantity_received numeric(16,4),
  vehicle_number text,
  status         text not null default 'open' check (status in ('open', 'matched', 'closed')),
  created_at     timestamptz not null default now()
);

create index if not exists idx_grn_challan on __SCHEMA__.goods_receipts (challan_number);

create table if not exists __SCHEMA__.compliance_records (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null default engine.current_tenant(),
  record_id        uuid references __SCHEMA__.extracted_records(id) on delete set null,
  voucher_id       uuid references __SCHEMA__.vouchers(id) on delete set null,
  kind             text not null check (kind in ('e_invoice', 'e_way_bill', 'gstr2b', 'tds')),
  number           text,                  -- IRN or e-way bill number
  document_number  text,
  generated_on     date,
  acknowledgement_no text,
  acknowledgement_date date,
  qr_data          text,
  supplier_gstin   text,
  recipient_gstin  text,
  hsn              text,
  taxable_value    numeric(14,2),
  total_value      numeric(14,2),
  tax_amount       numeric(14,2),
  vehicle_number   text,
  valid_upto       date,
  match_status     text check (match_status in ('matched', 'mismatch', 'missing', 'not_applicable')),
  match_detail     jsonb not null default '{}'::jsonb,
  period           text,                  -- 2026-03 for GSTR-2B
  created_at       timestamptz not null default now()
);

create index if not exists idx_compliance_kind on __SCHEMA__.compliance_records (kind, created_at desc);
create index if not exists idx_compliance_number on __SCHEMA__.compliance_records (number);

create table if not exists __SCHEMA__.bank_statement_lines (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null default engine.current_tenant(),
  document_id     uuid references __SCHEMA__.documents(id) on delete cascade,
  record_id       uuid references __SCHEMA__.extracted_records(id) on delete set null,
  account_ref     text,
  txn_date        date,
  narration       text,
  debit           numeric(14,2),
  credit          numeric(14,2),
  direction       text check (direction in ('debit', 'credit')),
  balance         numeric(14,2),
  reference       text,
  category        text,
  matched_party_gstin text,
  matched_party_name text,
  suggested_ledger text,
  match_confidence numeric(5,4),
  status          text not null default 'unmatched'
                    check (status in ('unmatched', 'suggested', 'matched', 'posted', 'ignored')),
  voucher_id      uuid references __SCHEMA__.vouchers(id) on delete set null,
  created_at      timestamptz not null default now()
);

create index if not exists idx_bank_lines on __SCHEMA__.bank_statement_lines (status, txn_date desc);

-- ─────────────────────────────────────────────────────────────
-- Second isolation layer: tenant_id must match the session tenant
-- ─────────────────────────────────────────────────────────────

do $$
declare
  t text;
  tables text[] := array[
    'documents','document_pages','extraction_runs','extracted_records','field_values',
    'rule_results','rule_settings','review_items','review_actions','clarification_requests',
    'corrections','rule_suggestions','few_shot_examples','master_snapshots','ledger_mappings',
    'master_proposals','vouchers','posting_attempts','shadow_comparisons','purchase_orders',
    'goods_receipts','compliance_records','bank_statement_lines'
  ];
begin
  foreach t in array tables loop
    execute format('alter table __SCHEMA__.%I enable row level security', t);
    execute format('drop policy if exists tenant_isolation on __SCHEMA__.%I', t);
    execute format($f$
      create policy tenant_isolation on __SCHEMA__.%I
        using (tenant_id = engine.current_tenant())
        with check (tenant_id = engine.current_tenant())
    $f$, t);
  end loop;
end
$$;

-- The engine connects as `postgres` (bypassrls) and always sets engine.tenant_id
-- before touching tenant tables, so both layers agree.
