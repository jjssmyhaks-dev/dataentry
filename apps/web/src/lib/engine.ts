import { env } from "./env";
import type { Session } from "./session";

/**
 * The only way the web app reaches tenant data: through the engine, as the
 * signed-in organisation. The engine resolves that organisation to exactly one
 * Postgres schema (sections 9 and 12c).
 */

export class EngineError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly code: string | null = null,
  ) {
    super(detail);
  }

  get isAuth() {
    return this.status === 401 || this.status === 403;
  }

  get isOffline() {
    return this.status === 0;
  }
}

function headers(session: Session, json = true): Record<string, string> {
  const h: Record<string, string> = {
    Authorization: `Bearer ${env.engineToken}`,
    "X-Actor-Id": session.userId,
    "X-Actor-Role": session.role,
  };
  if (session.email) h["X-Actor-Email"] = session.email;
  if (session.organizationId) {
    h["X-WorkOS-Organization"] = session.organizationId;
  } else {
    // No WorkOS organisation: only honoured by the engine outside production.
    h["X-Dev-Tenant"] = env.devTenant;
  }
  if (json) h["Content-Type"] = "application/json";
  return h;
}

async function call(
  path: string,
  session: Session,
  init: RequestInit = {},
): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(`${env.engineUrl}${path}`, {
      ...init,
      headers: { ...headers(session, !(init.body instanceof FormData)), ...(init.headers ?? {}) },
      cache: "no-store",
      signal: AbortSignal.timeout(30_000),
    });
  } catch (error) {
    throw new EngineError(
      0,
      `The engine is not reachable at ${env.engineUrl}. Start it with \`python -m engine.api.server\`.`,
      (error as Error).name,
    );
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    let code: string | null = null;
    try {
      const body = (await response.json()) as { detail?: unknown; code?: string };
      if (typeof body.detail === "string") detail = body.detail;
      else if (body.detail) detail = JSON.stringify(body.detail);
      code = body.code ?? null;
    } catch {
      /* non-JSON error body */
    }
    throw new EngineError(response.status, detail, code);
  }
  return response;
}

export async function engineGet<T>(
  path: string,
  session: Session,
  params?: Record<string, string | number | undefined | null>,
): Promise<T> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
  }
  const suffix = query.size ? `?${query.toString()}` : "";
  const response = await call(`${path}${suffix}`, session);
  return (await response.json()) as T;
}

export async function enginePost<T>(
  path: string,
  session: Session,
  body?: unknown,
): Promise<T> {
  const response = await call(path, session, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return (await response.json()) as T;
}

export async function enginePostText(
  path: string,
  session: Session,
  body?: unknown,
): Promise<{ content: string; mediaType: string }> {
  const response = await call(path, session, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return {
    content: await response.text(),
    mediaType: response.headers.get("content-type") ?? "text/plain",
  };
}

export async function uploadDocument(
  session: Session,
  file: File,
  channel = "web_upload",
  sender?: string,
): Promise<IngestResult> {
  const form = new FormData();
  form.set("file", file);
  form.set("channel", channel);
  if (sender) form.set("sender", sender);
  const response = await call("/v1/documents/upload", session, { method: "POST", body: form });
  return (await response.json()) as IngestResult;
}

// ── response shapes ──────────────────────────────────────────────────────────

export interface IngestResult {
  document_id: string;
  status: string;
  doc_type: string | null;
  doc_type_confidence: number | null;
  segments: number;
  records: number;
  review_item_ids: string[];
  voucher_ids: string[];
  posted_autonomously: number;
  queued: number;
  needs_review: number;
  duplicate_of: string | null;
  warnings: string[];
}

export type Outcome = "pass" | "warn" | "fail" | "skipped";

export interface ReviewReason {
  code: string;
  message: string;
  fields?: string[];
  rule_key?: string | null;
}

export interface QueueRow {
  id: string;
  record_id: string;
  document_id: string;
  status: string;
  priority: number;
  priority_band: "rule_failure" | "low_confidence" | "routine";
  reasons: ReviewReason[];
  flagged_fields: string[];
  failed_rules: string[];
  warned_rules: string[];
  amount_total: string | number | null;
  doc_type: string | null;
  vendor_name: string | null;
  requires_supervisor: boolean;
  assignee: string | null;
  due_at: string | null;
  created_at: string;
  overall_confidence: string | number | null;
  record_status: string;
  file_name: string | null;
  document_doc_type: string | null;
  received_at: string;
  voucher_count: number;
}

export interface QueueCounts {
  [status: string]: { count: number; rule_failures: number; value: number };
}

export interface QueueResponse {
  items: QueueRow[];
  counts: QueueCounts;
}

export interface FieldRow {
  id: string;
  path: string;
  label: string | null;
  value_text: string | null;
  value_num: string | number | null;
  value_date: string | null;
  value_json: unknown;
  confidence: string | number;
  page_no: number | null;
  bbox: { x: number; y: number; w: number; h: number } | null;
  source_span: string | null;
  mandatory: boolean;
  corrected: boolean;
}

export interface RuleRow {
  id: string;
  rule_key: string;
  title: string | null;
  severity: "fail" | "warn" | "info";
  outcome: Outcome;
  message: string | null;
  details: Record<string, unknown> | null;
  fields: string[];
  degraded: boolean;
  checked_at: string;
}

export interface DocumentRow {
  id: string;
  file_name: string | null;
  mime_type: string | null;
  intake_channel: string;
  sender: string | null;
  status: string;
  doc_type: string | null;
  received_at: string;
  page_count: number | null;
  exception_reason: string | null;
  duplicate_of: string | null;
}

export interface PageRow {
  page_no: number;
  has_text_layer: boolean;
  text_digest: string | null;
  width: number | null;
  height: number | null;
}

export interface VoucherLine {
  ledger: string | null;
  stock_item: string | null;
  amount: string | number;
  is_debit: boolean;
  description: string | null;
  quantity: string | number | null;
  unit: string | null;
  rate: string | number | null;
  gst_rate: string | number | null;
  hsn: string | null;
}

export interface VoucherDraft {
  voucher_type: string;
  voucher_date: string | null;
  party_ledger: string | null;
  narration: string | null;
  place_of_supply: string | null;
  lines: VoucherLine[];
  amount_total: string | number;
  gst_cgst: string | number;
  gst_sgst: string | number;
  gst_igst: string | number;
  roundoff: string | number;
  idempotency_key: string;
  tally_company: string | null;
  warnings: string[];
  proposals: Record<string, unknown>[];
}

export interface ReviewItemDetail {
  item: QueueRow;
  record: {
    id: string;
    doc_type: string;
    payload: Record<string, unknown>;
    overall_confidence: string | number;
    min_field_confidence: string | number;
    amount_total: string | number | null;
    vendor_name: string | null;
    vendor_gstin: string | null;
    document_number: string | null;
    document_date: string | null;
    status: string;
  };
  fields: FieldRow[];
  rules: RuleRow[];
  document: DocumentRow;
  pages: PageRow[];
  voucher: {
    id: string;
    status: string;
    voucher_type: string;
    tally_voucher_number: string | null;
    error_text: string | null;
  } | null;
  voucher_preview: {
    voucher: VoucherDraft;
    warnings: string[];
    proposals: { kind: string; name: string; reason: string }[];
    balanced: boolean;
  } | null;
  pack: {
    key: string;
    version: string;
    doc_type_label: string | null;
    mandatory_fields: string[];
  };
  actions: {
    action: string;
    actor: string;
    actor_role: string | null;
    field_path: string | null;
    after_value: string | null;
    note: string | null;
    created_at: string;
  }[];
}

export interface GateCheck {
  key: string;
  label: string;
  actual: number | null;
  target: number;
  comparison: string;
  passed: boolean;
}

export interface AutonomyState {
  stage: {
    stage: number;
    name: string;
    description: string;
    gate_to_next: { key: string; label: string; metric: string; comparison: string; target: number }[];
  };
  posting_mode: string;
  configured_mode: string;
  kill_switch: boolean;
  gate: {
    stage: number;
    stage_name: string;
    next_stage: number;
    passed: boolean;
    checks: GateCheck[];
  };
  should_demote: boolean;
  demote_reason: string;
  evaluation: { cases: number; source: string; caveat: string };
  limits?: Record<string, number | boolean>;
  metrics: Record<string, number | null>;
  error_rate: number;
  top_flags: FlagRow[];
  vendors: VendorRow[];
}

export interface FlagRow {
  code: string;
  message: string;
  n: number;
}

export interface VendorRow {
  vendor_gstin: string;
  vendor_name: string | null;
  documents: number;
  auto_posted: number;
  problems: number;
  mean_confidence: number | null;
}

export interface Dashboard {
  window_days: number;
  documents: {
    documents: number;
    exceptions: number;
    duplicates: number;
    in_review: number;
    posted: number;
  };
  metrics: Record<string, number | null>;
  error_rate: number;
  queue: QueueCounts;
  queued_vouchers: { n: number; value: number };
  open_agent_commands: number;
  top_flags: FlagRow[];
  vendors: VendorRow[];
}

export interface ActivityEntry {
  id: string;
  action: string;
  actor_type: string;
  actor_id: string | null;
  target_type: string | null;
  target_id: string | null;
  detail: Record<string, unknown> | null;
  severity: string | null;
  created_at: string;
}

export interface ExceptionRow {
  id: string;
  file_name: string | null;
  intake_channel: string;
  sender: string | null;
  doc_type: string | null;
  status: string;
  exception_reason: string | null;
  received_at: string;
  record_count: number;
}

export interface RuleSettingRow {
  key: string;
  title: string;
  severity: string;
  applies_to: string[];
  enabled: boolean;
  severity_override: string | null;
  settings: Record<string, unknown>;
}

export interface MasterProposal {
  id: string;
  kind: string;
  name: string;
  reason: string;
  status: string;
  created_at: string;
}
