-- 0003_control_plane_views.sql
-- Monitoring views used by the web app's dashboard and the autonomy gates.

create or replace view public.v_tenant_autonomy as
select
  t.id                                   as tenant_id,
  t.slug,
  t.name,
  t.industry,
  t.autonomy_stage,
  t.posting_mode,
  t.kill_switch,
  t.confidence_threshold,
  t.autopost_value_cap,
  t.daily_autopost_cap,
  t.error_rate_limit,
  t.status,
  (select count(*) from public.agents a
     where a.tenant_id = t.id and a.status = 'active')             as active_agents,
  (select count(*) from public.agent_commands c
     where c.tenant_id = t.id and c.status in ('queued', 'sent'))  as open_commands,
  (select max(h.created_at) from public.agent_heartbeats h
     where h.tenant_id = t.id)                                     as last_heartbeat_at,
  (select max(e.created_at) from public.autonomy_events e
     where e.tenant_id = t.id)                                     as last_autonomy_event_at
from public.tenants t;

create or replace view public.v_agent_health as
select
  a.id            as agent_id,
  a.tenant_id,
  t.slug          as tenant_slug,
  a.name,
  a.status,
  a.mode,
  a.tally_company,
  a.app_version,
  a.host_name,
  a.last_heartbeat_at,
  case
    when a.status = 'revoked' then 'revoked'
    when a.last_heartbeat_at is null then 'never_connected'
    when a.last_heartbeat_at > now() - interval '5 minutes' then 'connected'
    else 'offline'
  end             as connectivity,
  h.tally_running,
  h.company_open,
  h.period_open,
  h.queue_depth,
  h.last_post_at
from public.agents a
join public.tenants t on t.id = a.tenant_id
left join lateral (
  select * from public.agent_heartbeats hb
  where hb.agent_id = a.id
  order by hb.created_at desc
  limit 1
) h on true;

create or replace view public.v_command_queue as
select
  c.id,
  c.tenant_id,
  c.agent_id,
  c.kind,
  c.status,
  c.attempts,
  c.expires_at,
  c.created_at,
  c.sent_at,
  c.acked_at,
  c.error_text,
  c.idempotency_key,
  (c.status = 'queued' and c.expires_at < now()) as expired_in_place
from public.agent_commands c;

-- ─────────────────────────────────────────────────────────────
-- Second isolation layer on the control plane
-- ─────────────────────────────────────────────────────────────

do $$
declare
  t text;
  tables text[] := array[
    'tenant_members', 'tenant_packs', 'agents', 'agent_heartbeats',
    'agent_commands', 'autonomy_events', 'activity_log'
  ];
begin
  foreach t in array tables loop
    execute format('alter table public.%I enable row level security', t);
    execute format('drop policy if exists tenant_isolation on public.%I', t);
    execute format($f$
      create policy tenant_isolation on public.%I
        using (tenant_id = engine.current_tenant())
        with check (tenant_id = engine.current_tenant())
    $f$, t);
  end loop;
end
$$;

alter table public.tenants enable row level security;
drop policy if exists tenant_isolation on public.tenants;
create policy tenant_isolation on public.tenants
  using (id = engine.current_tenant())
  with check (id = engine.current_tenant());

-- Platform roles keep full access; the engine connects as one of them and always
-- sets engine.tenant_id, so both layers agree.
do $$
declare
  r text;
  roles text[] := array['postgres', 'service_role', 'authenticator', 'supabase_admin'];
begin
  foreach r in array roles loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('drop policy if exists platform_access on public.tenants');
      execute format('create policy platform_access on public.tenants to %I using (true) with check (true)', r);
    end if;
  end loop;
end
$$;
