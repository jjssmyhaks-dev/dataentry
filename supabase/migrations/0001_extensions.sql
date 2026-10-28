-- 0001_extensions.sql
-- Extensions the platform relies on. Supabase ships pgcrypto, uuid-ossp and
-- pg_stat_statements in the `extensions` schema; pgvector is added here because
-- duplicate detection and few-shot example retrieval depend on it (section 9).

create schema if not exists extensions;

do $$
begin
  create extension if not exists vector with schema extensions;
exception when others then
  raise notice 'pgvector not available on this instance: %', sqlerrm;
end
$$;

create extension if not exists pgcrypto with schema extensions;
create extension if not exists "uuid-ossp" with schema extensions;

-- Search path for our own objects: extensions first so vector/crypto resolve,
-- then public.
do $$
begin
  execute 'alter database ' || quote_ident(current_database()) ||
          ' set search_path to extensions, public';
end
$$;
