-- 0004_platform_policies.sql
-- The control-plane RLS pass in 0003 created a single policy that each role
-- overwrote in turn, leaving only the last role covered. Give every platform
-- role its own named policy instead so the set is explicit and auditable.

do $$
declare
  r text;
  roles text[] := array['postgres', 'service_role', 'authenticator', 'supabase_admin'];
begin
  foreach r in array roles loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('drop policy if exists %I on public.tenants', 'platform_access_' || r);
      execute format(
        'create policy %I on public.tenants to %I using (true) with check (true)',
        'platform_access_' || r, r);
    end if;
  end loop;
end
$$;

-- Drop the ambiguous single policy from 0003 if it is still around.
drop policy if exists platform_access on public.tenants;
