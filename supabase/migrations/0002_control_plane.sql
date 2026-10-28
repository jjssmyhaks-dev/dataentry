-- 0002_control_plane.sql
-- The control plane lives in `public` and is shared by every tenant: who the
-- tenants are, which WorkOS organisation owns them, which Postgres schema holds
-- their data, which packs they run, their autonomy settings, and their agents.
--
-- Tenant business data (documents, records, rules results, vouchers) lives in a
-- per-tenant schema created from supabase/tenant_schema.sql — see
-- public.provision_tenant() below.

create schema if not exists engine;

-- ─────────────────────────────────────────────────────────────
-- Shared helpers
-- ─────────────────────────────────────────────────────────────

-- The active tenant for the current transaction. Set only on the server, from
-- the signed WorkOS session, never from anything the browser sends (section 12c).
create or replace function engine.current_tenant()
returns uuid
language plpgsql
stable
as $$
declare
  v text;
begin
  v := current_setting('engine.tenant_id', true);
  if v is null or v = '' then
    return null;
  end if;
  return v::uuid;
exception when others then
  return null;
end
$$;

create or replace function engine.set_tenant(p_tenant uuid)
returns void
language plpgsql
as $$
begin
  perform set_config('engine.tenant_id', coalesce(p_tenant::text, ''), true);
end
$$;

create or replace function engine.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end
$$;

-- ─────────────────────────────────────────────────────────────
-- Tenants
-- ─────────────────────────────────────────────────────────────

create table if not exists public.tenants (
  id                      uuid primary key default gen_random_uuid(),
  slug                    text not null unique,
  name                    text not null,
  industry                text not null default 'manufacturing',
  workos_organization_id  text unique,
  schema_name             text not null unique,
  status                  text not null default 'active'
                            check (status in ('onboarding', 'active', 'suspended', 'churned')),

  -- Active pack binding (section 7). A tenant may layer a custom pack over a
  -- standard one; the resolution order is tenant_packs.
  pack_key                text not null default 'manufacturing',

  -- Autonomy (section 12a). stage 0 = foundations/shadow, 1 = shadow,
  -- 2 = autonomous on low-risk, 3 = autonomous on standard, 4 = full autonomy.
  autonomy_stage          smallint not null default 0 check (autonomy_stage between 0 and 4),
  posting_mode            text not null default 'shadow'
                            check (posting_mode in ('shadow', 'draft', 'autonomous')),
  kill_switch             boolean not null default true,   -- moves tenant to draft mode at once
  confidence_threshold    numeric(5,4) not null default 0.9300,
  autopost_value_cap      numeric(14,2) not null default 50000,
  daily_autopost_cap      integer not null default 200,
  error_rate_limit        numeric(6,4) not null default 0.0050,
  auto_demote_on_breach   boolean not null default true,

  settings                jsonb not null default '{}'::jsonb,
  created_at              timestamptz not null default now(),
  updated_at              timestamptz not null default now()
);

drop trigger if exists trg_tenants_updated on public.tenants;
create trigger trg_tenants_updated before update on public.tenants
  for each row execute function engine.touch_updated_at();

-- Reviewer access limits per client (section 12c open question): a CA-firm staff
-- member may only see some of the firm's clients.
create table if not exists public.tenant_members (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references public.tenants(id) on delete cascade,
  workos_user_id  text not null,
  email           text,
  full_name       text,
  role            text not null default 'reviewer'
                    check (role in ('owner', 'reviewer', 'viewer')),
  client_scope    jsonb not null default '[]'::jsonb,  -- [] = all clients of the tenant
  approval_limit  numeric(14,2),                        -- single-reviewer approval cap
  status          text not null default 'active' check (status in ('active', 'invited', 'disabled')),
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (tenant_id, workos_user_id)
);

drop trigger if exists trg_tenant_members_updated on public.tenant_members;
create trigger trg_tenant_members_updated before update on public.tenant_members
  for each row execute function engine.touch_updated_at();

create index if not exists idx_tenant_members_user on public.tenant_members (workos_user_id);

-- ─────────────────────────────────────────────────────────────
-- Activity log (section 10: full audit trail)
-- ─────────────────────────────────────────────────────────────

create table if not exists public.activity_log (
  id           bigserial primary key,
  tenant_id    uuid,
  actor_type   text not null default 'user' check (actor_type in ('user', 'agent', 'system', 'api')),
  actor_id     text,
  actor_email  text,
  action       text not null,
  target_type  text,
  target_id    text,
  detail       jsonb not null default '{}'::jsonb,
  severity     text not null default 'info' check (severity in ('debug', 'info', 'warn', 'error')),
  created_at   timestamptz not null default now()
);

create index if not exists idx_activity_tenant_time on public.activity_log (tenant_id, created_at desc);
create index if not exists idx_activity_target on public.activity_log (target_type, target_id);

-- ─────────────────────────────────────────────────────────────
-- Industry packs (section 7)
-- ─────────────────────────────────────────────────────────────

create table if not exists public.packs (
  key         text primary key,
  name        text not null,
  industry    text not null,
  description text,
  created_at  timestamptz not null default now()
);

-- Pack files are the source of truth and are versioned in git; syncing a pack
-- writes a row here with a checksum so a GST rule change ships as a pack
-- version, not an engine release.
create table if not exists public.pack_versions (
  id                  uuid primary key default gen_random_uuid(),
  pack_key            text not null references public.packs(key) on delete cascade,
  version             text not null,
  checksum            text not null,
  status              text not null default 'draft'
                        check (status in ('draft', 'canary', 'active', 'retired')),
  document_catalogue  jsonb not null default '[]'::jsonb,
  extraction_hints    jsonb not null default '{}'::jsonb,
  validation_rules    jsonb not null default '[]'::jsonb,
  connectors          jsonb not null default '{}'::jsonb,
  evaluation_set      jsonb not null default '[]'::jsonb,
  released_at         timestamptz,
  notes               text,
  created_at          timestamptz not null default now(),
  unique (pack_key, version)
);

create index if not exists idx_pack_versions_active on public.pack_versions (pack_key, status);

-- Pack registry per tenant. `base_version_id` is the standard pack; an optional
-- tenant-specific layer sits on top.
create table if not exists public.tenant_packs (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references public.tenants(id) on delete cascade,
  pack_version_id  uuid not null references public.pack_versions(id) on delete restrict,
  layer            text not null default 'base' check (layer in ('base', 'tenant')),
  priority         smallint not null default 0,
  status           text not null default 'active' check (status in ('active', 'rolled_back')),
  activated_at     timestamptz not null default now(),
  rolled_back_at   timestamptz,
  activated_by     text,
  unique (tenant_id, pack_version_id)
);

create index if not exists idx_tenant_packs on public.tenant_packs (tenant_id, status, priority);

-- ─────────────────────────────────────────────────────────────
-- GSTN lookup cache (Appendix B: rules degrade to a warning when the
-- service is down, and the record is rechecked later)
-- ─────────────────────────────────────────────────────────────

create table if not exists public.gstin_lookup_cache (
  gstin            text primary key,
  legal_name       text,
  trade_name       text,
  status           text,                  -- Active / Cancelled / Suspended / Unknown
  taxpayer_type    text,
  registration_date date,
  source           text not null default 'gsp',
  degraded         boolean not null default false,
  raw              jsonb not null default '{}'::jsonb,
  checked_at       timestamptz not null default now(),
  expires_at       timestamptz not null default now() + interval '1 day'
);

-- ─────────────────────────────────────────────────────────────
-- Bridge agents (Appendix E) — machine identity, not WorkOS users
-- ─────────────────────────────────────────────────────────────

create table if not exists public.agents (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null references public.tenants(id) on delete cascade,
  name              text not null,
  credential_id     text not null unique,        -- short public identifier of the agent
  public_key        text not null,               -- Ed25519, issued at install time
  credential_issued_at timestamptz not null default now(),
  credential_rotated_at timestamptz,
  tally_company     text,
  tally_company_guid text,
  mode              text not null default 'draft' check (mode in ('draft', 'autonomous', 'read_only')),
  status            text not null default 'active' check (status in ('active', 'paused', 'revoked')),
  app_version       text,
  host_name         text,
  last_heartbeat_at timestamptz,
  revoked_at        timestamptz,
  revoked_by        text,
  metadata          jsonb not null default '{}'::jsonb,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

drop trigger if exists trg_agents_updated on public.agents;
create trigger trg_agents_updated before update on public.agents
  for each row execute function engine.touch_updated_at();

create index if not exists idx_agents_tenant on public.agents (tenant_id, status);

create table if not exists public.agent_heartbeats (
  id             bigserial primary key,
  agent_id       uuid not null references public.agents(id) on delete cascade,
  tenant_id      uuid not null,
  tally_running  boolean,
  company_open   text,
  period_open    boolean,
  queue_depth    integer,
  last_post_at   timestamptz,
  app_version    text,
  payload        jsonb not null default '{}'::jsonb,
  created_at     timestamptz not null default now()
);

create index if not exists idx_heartbeats_agent on public.agent_heartbeats (agent_id, created_at desc);

-- The queue of signed commands travelling to the agent over its outbound
-- connection. Commands expire rather than posting late.
create table if not exists public.agent_commands (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references public.tenants(id) on delete cascade,
  agent_id         uuid references public.agents(id) on delete set null,
  kind             text not null
                     check (kind in ('post_voucher', 'reverse_voucher', 'sync_masters',
                                     'pause', 'health_check')),
  idempotency_key  text not null unique,
  payload          jsonb not null default '{}'::jsonb,
  signature        text,
  status           text not null default 'queued'
                     check (status in ('queued', 'sent', 'acked', 'failed', 'expired', 'cancelled')),
  attempts         integer not null default 0,
  expires_at       timestamptz not null default now() + interval '15 minutes',
  created_at       timestamptz not null default now(),
  sent_at          timestamptz,
  acked_at         timestamptz,
  result           jsonb,
  error_text       text
);

create index if not exists idx_commands_queue on public.agent_commands (tenant_id, status, created_at);
create index if not exists idx_commands_agent on public.agent_commands (agent_id, status);

-- ─────────────────────────────────────────────────────────────
-- Autonomy stage log (section 12a: gates and demotions are recorded)
-- ─────────────────────────────────────────────────────────────

create table if not exists public.autonomy_events (
  id          bigserial primary key,
  tenant_id   uuid not null references public.tenants(id) on delete cascade,
  from_stage  smallint,
  to_stage    smallint,
  from_mode   text,
  to_mode     text,
  reason      text,
  actor       text,
  metrics     jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);

create index if not exists idx_autonomy_events on public.autonomy_events (tenant_id, created_at desc);
